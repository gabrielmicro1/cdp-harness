#!/usr/bin/env python3
"""Google Takeout download-link pull — a ONE-OFF for wallaroo-media, not an
engine family (the slug guard below enforces it).

wallaroo-media delivered a personal-account Takeout (measured 442.30 GB
across 12 files) as "send download link" rather than "export to Drive", so
there is no bucket to rclone and no OAuth token to mint: the archives exist only behind
authenticated takeout.google.com URLs. The client copies those requests out
of Chrome DevTools as cURL commands; this tool hands them to a temporary
in-region VM which downloads and uploads them at datacenter speed into
<slug>-raw/workspace-export/personal-takeout/.

Reuses transfer_engine.py's VM lifecycle verbatim (create-vm / allow-network /
check-azure / teardown — the saxon/github/teams precedent) on VM
`xfer-takeout-wallaroo-media`. Only the copy layer is new; see
scripts/wallaroo_takeout_vm_pull.py.

SECRETS — read this before touching the code. A Chrome "Copy as cURL" of a
Google download carries the client's full Google SESSION COOKIES (SID/HSID/
SSID/APISID/SAPISID/__Secure-*PSID). That is the whole account, not a scoped
token: heavier than any other credential this harness handles. It therefore
travels stdin -> ssh stdin -> a 600 file on the VM and dies with the VM —
never argv, VM tags, logs, or a file on this machine. Both this script and
the VM puller strip Cookie/Authorization on any redirect that leaves
google.com (deliberate duplication, the zoho/teams TokenBox precedent — the
two run in different processes on different hosts). The client instructions
scope the credential further by asking for an Incognito window, whose session
dies when they close it.

Workflow:
  probe --probe-all -> create-vm -> allow-network -> write-dest ->
  check-azure -> write-links -> transfer --limit 1 (pilot) -> transfer ->
  status -> verify -> teardown --confirmed

`probe` is the day-one gate and runs BEFORE any billable resource: one
1-byte ranged GET per link reports the part's real size, its filename, and
whether the link is still alive.

GROUND TRUTH, measured live on wallaroo's real links 2026-08-31 — the code
is shaped around these, not around guesses:
- `i=` indexes the ARCHIVES of a Takeout job, NOT the parts of one archive.
  The server keys entirely off `j=` + `i=` and rewrites the filename, so the
  name in the URL path is ignored: i=0 asked for `…-5-001.zip` and i=1
  returned `…-1-001.zip`. ONE cURL therefore enumerates the whole job.
- Past the last index Google answers HTTP **500**, not 404. That is how the
  end of the list is found, and why probe reports it as the end rather than
  as a fault.
- The client may paste the POST-redirect
  `takeout-download.usercontent.google.com/download/<name>` URL rather than
  the `takeout.google.com/settings/takeout/download` one. Both work; the
  former needs no redirect followed. Both answer 206, so resume works.
- Filenames are heterogeneous and NOT all zips: alongside
  `takeout-<ts>-<batch>-<part>.zip` a job can serve a raw
  `All mail Including Spam and Trash-002.mbox` — spaces in the name, and an
  mbox rather than an archive. Hence the space-tolerant name whitelist and
  the extension-preserving fallback in safe_blob_name.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import phases  # noqa: E402
import transfer_engine as eng  # noqa: E402

SLUG = "wallaroo-media"  # one-off guard — see the module docstring

SPEC = eng.Spec(
    source_name="takeout",
    vm_prefix="xfer-takeout-",
    purpose="takeout-link-transfer",
    loc_tag="takeout_parts",   # a COUNT, never the links (they are secrets)
    loc_argname="parts",
    loc_required=False,
    default_dest_prefix="workspace-export/personal-takeout",
    authorize_target="",       # no OAuth flow — cURL commands on stdin
    remote_type="",            # no rclone source remote
    # Measured 2026-08-31: 442.30 GB across 12 files, the largest a single
    # 137.79 GB .mbox. Peak staging at --parallel 4 is the four biggest
    # concurrently (~298 GB), but 1 TB also holds the WHOLE export at once,
    # so delete-after-upload stays an optimization, not load-bearing.
    default_os_disk_gb=1024,
)

PULLER_PY = Path(__file__).resolve().parent / "wallaroo_takeout_vm_pull.py"
XFER_DIR = f"/home/{eng.ADMIN_USER}/xfer-takeout"
STAGE_DIR = f"{XFER_DIR}/stage"
LOG_FILE = f"{XFER_DIR}/pull.log"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/xfer"
LINKS_JSON = f"{ENV_DIR}/takeout-links.json"
DEST_ENV = f"{ENV_DIR}/dest.env"
X_MS_VERSION = "2021-08-06"
UA = "cdp-harness-takeout-pull/1.0"


def _guard(slug: str) -> None:
    if slug != SLUG:
        raise common.HarnessError(
            f"this tool is a one-off for {SLUG} only (got '{slug}'). It is "
            "not a transfer family — if another client ships Takeout "
            "download links, lift this guard deliberately and give it a "
            "name that is not wallaroo's.")


# ── cURL parsing (pure — the offline test targets) ───────────────────────────

# Cookies only ever ride to Google. A redirect anywhere else gets them
# stripped; a pasted link to anywhere else is refused outright.
def host_allowed(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return (host == "google.com" or host.endswith(".google.com")
            or host.endswith(".googleusercontent.com"))


# Flags that consume the NEXT token. Anything here is dropped along with its
# value unless it maps to a header we want to keep.
_VALUE_FLAGS_DROP = {
    "-o", "--output", "-d", "--data", "--data-raw", "--data-binary",
    "--data-urlencode", "-X", "--request", "-x", "--proxy", "-u", "--user",
    "--max-time", "-m", "--connect-timeout", "--retry", "-w", "--write-out",
    "--cert", "--key", "--cacert", "-F", "--form", "-T", "--upload-file",
}
_VALUE_FLAGS_HEADER = {   # flag -> header name it is shorthand for
    "-A": "user-agent", "--user-agent": "user-agent",
    "-e": "referer", "--referer": "referer",
}
# Headers we refuse to forward: they would either lie about the body we get
# (accept-encoding => a Content-Length for COMPRESSED bytes, which breaks the
# on-disk size check) or fight the request we build ourselves (range).
_DROP_HEADERS = {"accept-encoding", "range", "host", "content-length",
                 "connection", "if-none-match", "if-modified-since",
                 "if-range", "te", "transfer-encoding", "expect"}


def parse_curls(text: str) -> list[dict]:
    """Chrome 'Copy as cURL (bash)' blob -> [{url, headers, index}].

    Handles one-per-line and backslash-continued multi-line commands, in any
    mix. Only the URL and request headers survive: flags that would change
    WHAT we fetch or WHERE it goes (-o, --data, -X, proxies) are dropped, as
    is --compressed, so the bytes on the wire are the bytes on disk.
    """
    text = re.sub(r"\\\r?\n", " ", text or "")
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise common.HarnessError(
            f"could not tokenize the cURL input ({e}) — this usually means a "
            "quote was lost in copy/paste. Re-copy with 'Copy as cURL "
            "(bash)', not PowerShell, and paste it unmodified.")
    groups: list[list[str]] = []
    for tok in tokens:
        if tok == "curl" or tok.endswith("/curl"):
            groups.append([])
        elif groups:
            groups[-1].append(tok)
    if not groups:
        raise common.HarnessError(
            "no 'curl ...' command found on stdin — paste the output of "
            "Chrome DevTools -> Network -> right-click the download request "
            "-> Copy -> Copy as cURL (bash).")

    jobs: list[dict] = []
    for ordinal, toks in enumerate(groups):
        url = None
        headers: dict[str, str] = {}
        i = 0
        while i < len(toks):
            tok = toks[i]
            if tok in ("-H", "--header") and i + 1 < len(toks):
                raw = toks[i + 1]
                i += 2
                if ":" not in raw:
                    continue
                k, v = raw.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k and v and k not in _DROP_HEADERS and not k.startswith(":"):
                    headers[k] = v
                continue
            if tok in ("-b", "--cookie") and i + 1 < len(toks):
                val = toks[i + 1]
                i += 2
                # `-b name=value; ...` is a cookie string; `-b file.txt` is a
                # jar we cannot ship. Only the inline form is usable.
                if "=" in val:
                    headers["cookie"] = val
                continue
            if tok in _VALUE_FLAGS_HEADER and i + 1 < len(toks):
                headers.setdefault(_VALUE_FLAGS_HEADER[tok],
                                   toks[i + 1].strip())
                i += 2
                continue
            if tok in _VALUE_FLAGS_DROP:
                i += 2
                continue
            if tok == "--url" and i + 1 < len(toks):
                url = toks[i + 1]
                i += 2
                continue
            if tok.startswith("-"):
                i += 1   # boolean flag (--compressed, -L, -s, -k, ...)
                continue
            if url is None:
                url = tok
            i += 1
        if not url:
            raise common.HarnessError(
                f"cURL command #{ordinal + 1} has no URL in it")
        if not host_allowed(url):
            host = urllib.parse.urlsplit(url).hostname or "?"
            raise common.HarnessError(
                f"refusing a link to '{host}' — this tool only fetches from "
                "google.com / googleusercontent.com. Check you copied the "
                "download request and not some other row in the Network tab.")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        try:
            index = int(qs.get("i", [""])[0])
        except (ValueError, IndexError):
            index = ordinal
        jobs.append({"url": url, "headers": headers, "index": index})
    return jobs


def expand_parts(job: dict, n: int, start: int | None = None) -> list[dict]:
    """Clone one cURL across the job's `i=` archive index range.

    Takeout's download URLs differ only in `i=`, so one pasted command covers
    the whole job whichever file the client happened to click. Indexing is
    0-based, so expansion starts at 0 unless --expand-start says otherwise;
    probe --probe-all proves every generated link before a VM exists.
    """
    if n < 1:
        raise common.HarnessError("--expand-parts must be >= 1")
    parts = urllib.parse.urlsplit(job["url"])
    qs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "i" for k, _ in qs):
        raise common.HarnessError(
            "cannot expand: this URL has no 'i=' part index. Send one cURL "
            "per part instead, or check the link was copied whole.")
    # Takeout jobs are 0-indexed — measured on wallaroo 2026-08-31, where
    # i=0..11 all resolved and i=12 answered 500. So expansion ALWAYS starts
    # at 0 regardless of which row the client happened to copy. Deriving the
    # base from the pasted link's own i= (as this did originally) silently
    # shifted the whole range when they copied the second file: it walked
    # 1..12, missing part 0 and inventing a part 12.
    if start is None:
        start = 0
    out = []
    for k in range(start, start + n):
        newqs = [(qk, str(k) if qk == "i" else qv) for qk, qv in qs]
        url = urllib.parse.urlunsplit(
            parts._replace(query=urllib.parse.urlencode(newqs)))
        out.append({"url": url, "headers": dict(job["headers"]), "index": k})
    return out


def read_jobs(args) -> list[dict]:
    """Parse stdin (never a file on this machine), then optionally expand."""
    if sys.stdin.isatty():
        raise common.HarnessError(
            "no cURL commands on stdin — pipe the file the client sent:\n"
            f"  python3 scripts/wallaroo_takeout_pull.py {args.command} "
            f"{SLUG} < curls.txt")
    jobs = parse_curls(sys.stdin.read())
    if args.expand_parts:
        if len(jobs) != 1:
            raise common.HarnessError(
                f"--expand-parts needs exactly 1 cURL on stdin, got "
                f"{len(jobs)} — either paste one, or drop the flag and let "
                "all of them through.")
        jobs = expand_parts(jobs[0], args.expand_parts, args.expand_start)
    seen = set()
    for j in jobs:
        if j["index"] in seen:
            raise common.HarnessError(
                f"two cURL commands carry the same part index i={j['index']} "
                "— the same part was copied twice, so at least one part is "
                "missing. Re-copy the set.")
        seen.add(j["index"])
    return sorted(jobs, key=lambda j: j["index"])


# ── HTTP: the cookie-guarded opener (duplicated in the VM puller) ────────────

class _CookieGuardRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the client's Google session cookie
    off google.com. Takeout's download 302s to *.usercontent.google.com,
    which is in scope; anything else gets the credential stripped before the
    request is made. Also records the chain so probe can say WHERE a link
    actually landed (a bounce to accounts.google.com is an expired session,
    not a broken link)."""

    def __init__(self, chain: list):
        self.chain = chain

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append(urllib.parse.urlsplit(newurl).netloc)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not host_allowed(newurl):
            for store in (new.headers, new.unredirected_hdrs):
                for k in [h for h in store
                          if h.lower() in ("cookie", "authorization")]:
                    del store[k]
        return new


def open_url(url: str, headers: dict, extra: dict | None = None,
             timeout: int = 120):
    """(response, redirect_chain). Caller must close the response."""
    chain: list[str] = []
    opener = urllib.request.build_opener(_CookieGuardRedirect(chain))
    req = urllib.request.Request(url)
    for k, v in headers.items():
        req.add_header(k, v)
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    req.add_header("user-agent", headers.get("user-agent", UA))
    return opener.open(req, timeout=timeout), chain


_CD_STAR = re.compile(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", re.I)
_CD_PLAIN = re.compile(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)', re.I)
# Google's own names carry spaces and parentheses ("All mail Including Spam
# and Trash-002.mbox"), so those are allowed; path separators and control
# characters are not. Azure accepts these verbatim and Dest.url() percent-
# encodes on the way out.
_SAFE_NAME = re.compile(r"[A-Za-z0-9 ._+=,()&'\[\]-]{1,180}")
_KNOWN_EXT = (".zip", ".tgz", ".tar.gz", ".mbox", ".json", ".csv", ".vcf")
_RESERVED = {"manifest.json", "progress.json"}


def content_disposition_name(cd: str | None) -> str | None:
    if not cd:
        return None
    m = _CD_STAR.search(cd)
    if m:
        try:
            return urllib.parse.unquote(m.group(1).strip())
        except (ValueError, UnicodeDecodeError):
            pass
    m = _CD_PLAIN.search(cd)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    return None


def safe_blob_name(raw: str | None, index: int) -> str:
    """Server-supplied filename -> a blob name we are willing to create.

    Whitelist, not blacklist: path separators, traversal, leading dots and
    anything outside [A-Za-z0-9._+=-] fall back to a deterministic name. Our
    own bookkeeping names are reserved so a hostile Content-Disposition can
    never overwrite the manifest."""
    base = (raw or "").strip()
    # A path separator anywhere is refused outright rather than basename()d:
    # blob names are flat so it could not traverse, but a Content-Disposition
    # carrying one is not a name we should honour.
    if (base and "/" not in base and "\\" not in base
            and not base.startswith(".")
            and base.lower() not in _RESERVED
            and _SAFE_NAME.fullmatch(base)):
        return base
    # Falling back: keep the real extension if we can see one, so an .mbox
    # never lands named .zip (which would send the sizer looking for a zip
    # central directory that does not exist).
    ext = next((e for e in _KNOWN_EXT if base.lower().endswith(e)), ".zip")
    return f"takeout-part-{index:03d}{ext}"


def probe_one(job: dict, timeout: int = 60) -> dict:
    """Exactly ONE 1-byte ranged GET. Never reads the body: a server that
    ignores Range answers 200 with the whole 40 GB archive, and we hang up
    on it rather than download it here."""
    out = {"index": job["index"]}
    try:
        resp, chain = open_url(job["url"], job["headers"],
                               {"range": "bytes=0-0"}, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()
        out.update({"http_code": e.code})
        if e.code in (401, 403):
            out["state"] = "auth-expired"
        elif e.code in (404, 410):
            out["state"] = "link-expired"
        else:
            out["state"] = f"http-{e.code}"
        return out
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {**out, "state": "unreachable", "error": str(e)[:200]}
    try:
        code = resp.status
        h = resp.headers
        final_host = urllib.parse.urlsplit(resp.geturl()).netloc
        name = content_disposition_name(h.get("Content-Disposition"))
        total = None
        cr = h.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if code == 206 and m:
            total = int(m.group(1))
        elif h.get("Content-Length") is not None:
            total = int(h["Content-Length"])
        out.update({
            "http_code": code,
            "final_host": final_host,
            "redirect_chain": chain[:6],
            "filename": name,
            "blob_name": safe_blob_name(name, job["index"]),
            "bytes": total,
            "bytes_human": common.human_bytes(total),
            "resumable": code == 206,
        })
        if "accounts.google.com" in " ".join(chain) or "signin" in resp.geturl():
            out["state"] = "auth-expired"
        elif code == 206:
            out["state"] = "open"
        elif code == 200:
            out["state"] = "no-range-support"
        else:
            out["state"] = f"http-{code}"
        return out
    finally:
        resp.close()   # never read the body


# ── azure listing (verify only; laptop path) ─────────────────────────────────
# Same shape as github_transfer.azure_get / azure_list_blobs — the VM-family
# laptop-side verify pair. Kept local so the one-off has no cross-CLI import.

def _container_url(cfg: dict) -> str:
    return (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")


def azure_get(url: str) -> bytes:
    import time
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url,
                                     headers={"x-ms-version": X_MS_VERSION})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                time.sleep(15 * (attempt + 1))  # propagation, not a bad SAS
                continue
            if e.code >= 500:
                time.sleep(1 + attempt)
                continue
            raise common.HarnessError(f"blob GET failed: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(1 + attempt)
    raise common.HarnessError(f"blob GET failed after retries: {last}")


def azure_list_blobs(cfg: dict, sas: str, prefix: str,
                     dry_run: bool) -> dict[str, int]:
    """{blob name: committed Content-Length} under the prefix."""
    if dry_run:
        print(f"DRY-RUN: GET {_container_url(cfg)}"
              f"?restype=container&comp=list&prefix={prefix}/&<sas-redacted>")
        return {}
    blobs: dict[str, int] = {}
    marker = ""
    while True:
        url = (f"{_container_url(cfg)}?restype=container&comp=list"
               f"&maxresults=5000"
               f"&prefix={urllib.parse.quote(prefix + '/', safe='')}")
        if marker:
            url += f"&marker={urllib.parse.quote(marker, safe='')}"
        url += "&" + sas
        root = ET.fromstring(azure_get(url))
        for blob in root.iter("Blob"):
            name = blob.findtext("Name")
            props = blob.find("Properties")
            if name:
                blobs[name] = int((props.findtext("Content-Length") or 0)
                                  if props is not None else 0)
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return blobs


def compare_manifest_to_blobs(manifest: dict, blobs: dict[str, int],
                              prefix: str) -> dict:
    """Pure. Unlike the github/zoho family this makes a REAL source-truth
    claim: Google declared each part's byte length in Content-Length, the
    puller only uploads a file whose on-disk size matched that declaration,
    and here the COMMITTED blob length is compared against the same number.
    Declared == staged == committed, end to end.

    Completeness is the part COUNT: manifest.part_count is what the links
    said existed, so a part that never made it is a hole, not an absence."""
    parts = manifest.get("parts") or []
    missing, short, extra_size = [], [], []
    for p in parts:
        if p.get("status") != "ok":
            continue
        name = f"{prefix}/{p['blob_name']}"
        if name not in blobs:
            missing.append(p["blob_name"])
            continue
        declared = p.get("declared_bytes")
        if declared is not None and blobs[name] != declared:
            (short if blobs[name] < declared else extra_size).append(
                {"blob": p["blob_name"], "declared": declared,
                 "committed": blobs[name]})
    failed = manifest.get("failed_parts") or []
    ok_parts = [p for p in parts if p.get("status") == "ok"]
    expected = manifest.get("part_count")
    incomplete = (expected is not None and len(ok_parts) != expected)
    ok = not failed and not missing and not short and not extra_size \
        and not incomplete
    return {
        "ok": ok,
        "part_count_expected": expected,
        "parts_ok": len(ok_parts),
        "declared_bytes": manifest.get("total_declared_bytes"),
        "declared_human": common.human_bytes(
            manifest.get("total_declared_bytes")),
        "committed_bytes": sum(
            blobs.get(f"{prefix}/{p['blob_name']}", 0) for p in ok_parts),
        "failed_parts": failed,
        "missing_blobs": missing,
        "short_blobs": short,
        "size_mismatches": extra_size,
        "hint": None if ok else
        "failed/missing/short parts: re-run transfer (a part whose blob "
        "already exists is skipped, so a re-run only mops up the holes), "
        "then re-verify. If the links have expired the client must "
        "re-export — check with probe before rebuilding a VM.",
    }


# ── VM-side plumbing ─────────────────────────────────────────────────────────

def _write_secret(ip: str, path: str, content: str, dry_run: bool) -> None:
    """600 file on the VM; content rides ssh stdin only (never argv/logs)."""
    eng.run_ssh(ip, f"umask 077 && mkdir -p {ENV_DIR} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _push_puller(ip: str, dry_run: bool) -> None:
    """Fresh copy every transfer so harness fixes propagate."""
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > "
                    f"{XFER_DIR}/wallaroo_takeout_vm_pull.py",
                stdin_data=PULLER_PY.read_text(), dry_run=dry_run)


def _dest_prefix(vm: dict, args) -> str:
    return (getattr(args, "dest_prefix", None)
            or vm["tags"].get("dest_prefix") or SPEC.default_dest_prefix)


# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_probe(root: Path, args) -> dict:
    """Day-one gate, BEFORE any billable resource. Laptop-side, one 1-byte
    ranged GET per link. The cURLs are read into process memory and
    discarded — nothing is written to this machine."""
    _guard(args.slug)
    eng.load_cfg(root, args.slug)   # onboarding check only
    jobs = read_jobs(args)
    if args.dry_run:
        for j in jobs:
            print(f"DRY-RUN: GET {j['url'].split('?')[0]}?... "
                  f"(Range: bytes=0-0, {len(j['headers'])} headers "
                  "including a redacted cookie)")
        return {"ok": True, "dry_run": True, "parts_parsed": len(jobs)}
    targets = jobs if args.probe_all else jobs[:1]
    results = [probe_one(j, args.timeout) for j in targets]
    sized = [r for r in results if r.get("bytes")]
    total = sum(r["bytes"] for r in sized)
    bad = [r for r in results if r.get("state") != "open"]
    notes = []
    if not args.probe_all:
        notes.append("only part 1 was probed — re-run with --probe-all to "
                     "confirm every link and get the real total. With a "
                     "known part count that is the check worth doing.")
    if any(r.get("state") == "no-range-support" for r in results):
        notes.append("a link answered 200 instead of 206: Range is not "
                     "supported, so an interrupted part restarts from zero "
                     "rather than resuming. Budget for it.")
    if any(r.get("state") == "auth-expired" for r in results):
        notes.append(
            "auth-expired: the copied Google session is no longer accepted "
            "(the response redirects to accounts.google.com). MEASURED on "
            "wallaroo 2026-08-31: a set of cookies that probed fine was "
            "being refused ~20 minutes later, from the ORIGINAL machine as "
            "well as the VM — so this is the credential going stale, not an "
            "IP block. Copied Google sessions have a short and "
            "unpredictable life; the client must re-copy, and the run "
            "should start immediately after. A retry will not fix it.")
    if any(r.get("state") == "link-expired" for r in results):
        notes.append("link-expired: Takeout archives lapse about a week "
                     "after export. This needs a fresh export, which is "
                     "days of client time — say so early.")
    # Which ROW of the Network tab did they copy? The pre-redirect
    # takeout.google.com request re-mints a fresh signed download URL every
    # time we follow it; the post-redirect usercontent.google.com one is a
    # single already-minted grant. Both authenticate with the same cookie,
    # but only the former can be retried later in a long run.
    hosts = {urllib.parse.urlsplit(j["url"]).hostname or "" for j in jobs}
    if any(h.startswith("takeout-download.") or "usercontent" in h
           for h in hosts):
        notes.append(
            "these are POST-REDIRECT usercontent.google.com links. They "
            "work, but they are a single already-issued grant rather than "
            "something we can re-mint: ask the client for the FIRST row in "
            "the Network tab instead — the takeout.google.com/settings/"
            "takeout/download?j=... request, which 302s. Following that "
            "ourselves gets a fresh download URL per attempt, which is what "
            "a multi-hour run needs.")
    names = [r.get("blob_name") for r in results if r.get("blob_name")]
    if len(set(names)) != len(names):
        notes.append("two parts resolved to the SAME blob name — the "
                     "expanded index range is probably wrong (the server is "
                     "handing back one archive repeatedly). Do not transfer.")
    tail_500 = [r["index"] for r in results
                if str(r.get("state", "")).endswith("-500")]
    if tail_500 and any(r.get("state") == "open" for r in results):
        first_500 = min(tail_500)
        if all(i >= first_500 for i in tail_500):
            notes.append(
                f"index {first_500} and up answered HTTP 500 while every "
                f"lower index is open: that is the END of this job's file "
                f"list, not a fault — Google returns 500 past the last "
                f"index rather than 404. The job holds {first_500} file(s).")
    notes.append("each part cost exactly one 1-byte ranged request; the "
                 "body was never read.")
    return {"ok": not bad, "parts_probed": len(results),
            "parts_parsed": len(jobs),
            "total_bytes": total if len(sized) == len(results) else None,
            "total_human": common.human_bytes(total)
            if len(sized) == len(results) else None,
            "parts": results, "notes": notes,
            "hint": None if not bad else
            "not every link is fetchable — see the per-part state before "
            "creating a VM."}


def cmd_write_dest(root: Path, args) -> dict:
    """racwl container SAS -> 600 dest.env on the VM. DEST_URL is the BARE
    container URL: the puller appends DEST_PREFIX itself, so the prefix
    stays a real, consumed setting rather than being baked into the URL
    (the teams convention)."""
    _guard(args.slug)
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    prefix = _dest_prefix(vm, args)
    sas, expiry = eng.mint_container_sas(cfg, args.sas_days, args.dry_run)
    # single-quoted: a SAS contains '&', and sourcing an unquoted VAR=a&b
    # line backgrounds the assignment at the '&' so the var lands EMPTY and
    # azcopy hits Azure unauthenticated (401, found live on checkmate).
    # The puller uses azcopy + blob REST, not rclone — but the engine's
    # check-azure probes an rclone [azure] remote, and its 403 triage
    # (vnet-rule missing vs still propagating vs bad SAS) is worth keeping.
    # So write both, exactly as github_transfer.cmd_write_dest does.
    eng.write_conf_section(vm["public_ip"], "azure",
                           f"[azure]\ntype = azureblob\n"
                           f"sas_url = {_container_url(cfg)}?{sas}\n",
                           dry_run=args.dry_run)
    env = (f"AZURE_DEST_URL='{_container_url(cfg)}'\n"
           f"AZURE_DEST_SAS='{sas}'\n"
           f"AZURE_DEST_CONTAINER='{cfg['container']}'\n"
           f"AZURE_DEST_PREFIX='{prefix}'\n")
    _write_secret(vm["public_ip"], DEST_ENV, env, args.dry_run)
    return {"container": cfg["container"], "dest_prefix": prefix,
            "sas_expiry": expiry,
            "written_to": [f"{vm['name']}:{eng.RCLONE_CONF}",
                           f"{vm['name']}:{DEST_ENV}"]}


_SMOKE_PY = r"""
import json, os, sys, urllib.parse, urllib.request, urllib.error
jobs = json.load(open(os.path.expanduser("~/.config/xfer/takeout-links.json")))
j = jobs["jobs"][0]
req = urllib.request.Request(j["url"])
for k, v in j["headers"].items():
    req.add_header(k, v)
req.add_header("range", "bytes=0-0")
out = {"index": j["index"]}
try:
    r = urllib.request.urlopen(req, timeout=60)
    out["http_code"] = r.status
    out["final_host"] = urllib.parse.urlsplit(r.geturl()).netloc
    out["content_range"] = r.headers.get("Content-Range")
    out["content_type"] = r.headers.get("Content-Type")
    out["content_disposition"] = r.headers.get("Content-Disposition")
    r.close()
except urllib.error.HTTPError as e:
    out["http_code"] = e.code
    e.close()
except Exception as e:
    out["error"] = str(e)[:200]
print(json.dumps(out))
"""


def cmd_write_links(root: Path, args) -> dict:
    """cURLs on stdin -> 600 takeout-links.json on the VM, then a VM-side
    1-byte smoke test.

    The smoke test is NOT redundant with probe: probe ran from this laptop's
    IP, the transfer runs from an Azure datacenter IP, and Google can treat
    a session cookie replayed from a new origin differently. Finding that
    out here costs one request; finding it out during transfer costs a
    confusing half-failed run."""
    _guard(args.slug)
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    jobs = read_jobs(args)
    # Is this the SAME credential we already tried? A Takeout link carries a
    # one-time `rapt` re-auth token, so a resent copy of an earlier message
    # can never work — but it LOOKS like a fresh link, and re-testing it
    # burns a client round trip. Hit on wallaroo 2026-09-01.
    if not args.allow_resend and not args.dry_run:
        prev = eng.run_ssh(vm["public_ip"], f"cat {LINKS_JSON} 2>/dev/null",
                           check=False, timeout=60)
        try:
            old_jobs = json.loads(prev.stdout or "")["jobs"]
        except (ValueError, KeyError, TypeError):
            old_jobs = None
        if old_jobs and old_jobs[0] == jobs[0]:
            return {"ok": False, "cause": "credential-resent",
                    "hint": "this cURL is byte-identical to the one already "
                            "on the VM, which means it is a resend of an "
                            "earlier message rather than a fresh capture. "
                            "Takeout links carry a one-time `rapt` re-auth "
                            "token, so the same text can never work twice — "
                            "the client must click Download again and copy "
                            "the NEW request. Nothing was changed. "
                            "(--allow-resend overrides, e.g. if they signed "
                            "back in and you want to re-test anyway.)"}
    payload = json.dumps({"jobs": jobs}, separators=(",", ":"))
    if args.dry_run:
        payload = json.dumps({"jobs": [{"url": "<redacted>", "headers": {},
                                        "index": j["index"]} for j in jobs]})
    _write_secret(vm["public_ip"], LINKS_JSON, payload + "\n", args.dry_run)
    # A COUNT is not a secret; the links never touch a tag. The value is
    # INNER-QUOTED because az parses a bare 12 as an int and the ARM tags
    # API only accepts strings — the same trap as vnet_rule_added=engine
    # (bacancy). Found live here: the bare form failed silently under
    # check=False and left the tag empty.
    common.run_az(["vm", "update", "-g", args.rg or cfg["resource_group"],
                   "-n", SPEC.vm_name(args.slug), "--set",
                   f"tags.takeout_parts='{len(jobs)}'", "-o", "none"],
                  dry_run=args.dry_run, check=False, timeout=300)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "parts": len(jobs)}
    proc = eng.run_ssh(vm["public_ip"], "python3 -", stdin_data=_SMOKE_PY,
                       check=False, timeout=120)
    try:
        smoke = json.loads((proc.stdout or "").strip() or "null")
    except json.JSONDecodeError:
        smoke = {"error": (proc.stdout or proc.stderr or "")[-300:]}
    # A 200 is NOT success here: Google serves an invalid session the
    # sign-in PAGE with status 200. The smoke test must prove it got a FILE
    # — right host, a Content-Disposition, and not HTML — or it waves a
    # broken run through, which is exactly what happened 2026-08-31.
    sm = smoke or {}
    code = sm.get("http_code")
    fhost = (sm.get("final_host") or "").lower()
    ctype = (sm.get("content_type") or "").lower()
    reason = None
    if code not in (200, 206):
        reason = f"HTTP {code}"
    elif "accounts.google.com" in fhost:
        reason = ("redirected to the Google sign-in page — the session is "
                  "not valid for downloading")
    elif ctype.startswith("text/html"):
        reason = f"got HTML (Content-Type: {ctype}), not an archive"
    elif not sm.get("content_disposition"):
        reason = "no Content-Disposition, so this is not a file download"
    ok = reason is None
    return {"ok": ok, "parts": len(jobs),
            "indices": [j["index"] for j in jobs],
            "written_to": f"{vm['name']}:{LINKS_JSON}", "smoke_test": smoke,
            "smoke_failure": reason,
            "hint": None if ok else
            f"the VM did not get a real archive back ({reason}). Either the "
            "copied session has gone stale (they go stale in well under an "
            "hour) or Google is refusing it from the datacenter IP. Have "
            "the client re-copy the cURL and re-run write-links — do NOT "
            "start the transfer."}


def cmd_transfer(root: Path, args) -> dict:
    _guard(args.slug)
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    if eng._tmux_alive(ip, args.dry_run):
        return {"ok": False, "cause": "already-running",
                "hint": "tmux session 'transfer' is alive — use status."}
    _push_puller(ip, args.dry_run)
    flags = ""
    if args.limit:
        flags += f" --limit {args.limit}"
    if args.only_index is not None:
        flags += f" --only-index {args.only_index}"
    if args.parallel:
        flags += f" --parallel {args.parallel}"
    if args.skip_upload:
        flags += " --skip-upload"
    if args.ignore_prior:
        flags += " --ignore-prior"
    inner = (f"set -a; . {DEST_ENV}; set +a; "
             f"python3 {XFER_DIR}/wallaroo_takeout_vm_pull.py "
             f"--links {LINKS_JSON} --stage {STAGE_DIR}{flags} "
             f">> {LOG_FILE} 2>&1")
    launch = (f"mkdir -p {XFER_DIR} && tmux new-session -d -s "
              f"{eng.TMUX_SESSION} -n pull \"bash -c '{inner}'\"")
    eng.run_ssh(ip, launch, dry_run=args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True}
    eng.run_ssh(ip, "sleep 5", check=False)
    alive = eng._tmux_alive(ip, False)
    tail = eng.run_ssh(ip, f"tail -5 {LOG_FILE} 2>/dev/null", check=False)
    return {"ok": alive, "session": eng.TMUX_SESSION,
            "pilot_limit": args.limit or None,
            "log_tail": (tail.stdout or "").strip().splitlines(),
            "note": ("re-running transfer is safe — a part whose blob "
                     "already exists is skipped without touching Google"
                     if alive else None),
            "hint": None if alive else
            f"the puller died immediately — tail {LOG_FILE} on the VM "
            "(missing dest.env? links file never written?)"}


_STATUS_PY = r"""
import json, os, shutil
base = os.path.expanduser("~/xfer-takeout")
out = {}
try:
    out["progress"] = json.load(open(os.path.join(base, "stage",
                                                  "progress.json")))
except (OSError, ValueError):
    out["progress"] = None
try:
    m = json.load(open(os.path.join(base, "stage", "manifest.json")))
    out["manifest"] = dict(part_count=m.get("part_count"),
                           parts_ok=len([p for p in m.get("parts", [])
                                         if p.get("status") == "ok"]),
                           total_declared_bytes=m.get("total_declared_bytes"),
                           failed_parts=m.get("failed_parts"),
                           finished_utc=m.get("finished_utc"))
except (OSError, ValueError):
    out["manifest"] = None
try:
    out["log_tail"] = open(os.path.join(base,
                                        "pull.log")).read().splitlines()[-6:]
except OSError:
    out["log_tail"] = []
try:
    du = shutil.disk_usage(base)
    out["disk_free_gb"] = round(du.free / 1e9, 1)
    out["disk_total_gb"] = round(du.total / 1e9, 1)
except OSError:
    pass
print(json.dumps(out))
"""


def cmd_status(root: Path, args) -> dict:
    _guard(args.slug)
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    alive = eng._tmux_alive(vm["public_ip"], args.dry_run)
    proc = eng.run_ssh(vm["public_ip"], "python3 -", stdin_data=_STATUS_PY,
                       dry_run=args.dry_run, check=False, timeout=120)
    if args.dry_run:
        return {"ok": True, "dry_run": True}
    try:
        detail = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        detail = {"aggregator_error": (proc.stdout or proc.stderr)[-300:]}
    hint = None
    if not alive:
        hint = ("a pass finished — run verify (laptop-side)"
                if detail.get("manifest") else
                "not running and no manifest — it never finished a pass; "
                f"tail {LOG_FILE} on the VM, then re-run transfer")
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "transfer_running": alive, "hint": hint, **detail}


def cmd_verify(root: Path, args) -> dict:
    """Laptop-side; the VM may already be gone. Lists the dest prefix and
    compares it against the manifest the pull uploaded."""
    _guard(args.slug)
    cfg = eng.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    prefix = args.dest_prefix or SPEC.default_dest_prefix
    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)   # rl — the READ path
        blobs = azure_list_blobs(cfg, sas, prefix, args.dry_run)
        if args.dry_run:
            print(f"DRY-RUN: GET {_container_url(cfg)}/{prefix}/"
                  "manifest.json?<sas-redacted>")
            return {"ok": True, "dry_run": True, "prefix": prefix}
        mname = f"{prefix}/manifest.json"
        if mname not in blobs:
            return {"ok": False, "cause": "no-manifest", "prefix": prefix,
                    "blobs_under_prefix": len(blobs),
                    "hint": "no manifest.json under the prefix — the pull "
                            "never finished a pass (or ran --skip-upload). "
                            "Run status on the VM first."}
        manifest = json.loads(azure_get(
            f"{_container_url(cfg)}/{urllib.parse.quote(mname, safe='/')}"
            f"?{sas}"))
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)
    result = compare_manifest_to_blobs(manifest, blobs, prefix)
    result.update({"prefix": prefix, "blobs_under_prefix": len(blobs),
                   "finished_utc": manifest.get("finished_utc")})
    if result["ok"]:
        result["note"] = (
            "declared == staged == committed on every part: Google's "
            "Content-Length, the bytes written to VM disk, and the blob's "
            "committed length all agree. Next: size-company + gen_report — "
            f"the bytes land as a '{prefix}' row in sources_l2 and roll "
            "into the existing gdrive/gmail reconciliation with no config "
            "change.")
    return result


def cmd_teardown(root: Path, args) -> dict:
    _guard(args.slug)
    result = eng.cmd_teardown(SPEC, root, args)
    if result.get("ok") and "reminders" in result:
        result["reminders"][-1] = (
            "Tell the client they can close the Incognito window now. That "
            "is what kills the Google session the cURL was carrying — it is "
            "the clean end of the engagement, and it is on THEIR side, so "
            "it needs an actual message.")
    return result


def cmd_discover(root: Path, args) -> dict:
    _guard(args.slug)
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    if args.dry_run:
        eng.get_vm(SPEC, cfg, args.slug, True)
        return {"phase": "unknown (dry-run)",
                "note": "dry-run prints the discovery commands only"}
    vm = eng.get_vm(SPEC, cfg, args.slug, False)
    if vm is None:
        return {"phase": "pre-setup", "vm": None,
                "hint": "no transfer VM — run probe (needs only the cURLs), "
                        "then create-vm"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base,
                "hint": "VM exists but has no public IP (deallocated?)"}
    probe = eng.run_ssh(
        vm["public_ip"],
        f"test -f {LINKS_JSON} && echo links; "
        f"test -f {DEST_ENV} && echo dest-env; "
        f"test -f {STAGE_DIR}/manifest.json && echo manifest; "
        f"tmux has-session -t {eng.TMUX_SESSION} 2>/dev/null "
        "&& echo tmux-alive", check=False)
    if probe.returncode != 0 and not (probe.stdout or "").strip():
        return {"phase": "vm-unreachable", **base,
                "hint": "ssh failed — VM booting, or your key changed. "
                        f"Try: ssh {eng.ADMIN_USER}@{vm['public_ip']}"}
    out = probe.stdout or ""
    if "tmux-alive" in out:
        return {"phase": "transfer-running", **base,
                "hint": "use status for progress"}
    if "manifest" in out:
        return {"phase": "transfer-stopped", **base,
                "hint": "a pass finished — run status, then verify "
                        "(laptop-side); failed parts mean re-run transfer"}
    if not ("links" in out and "dest-env" in out):
        return {"phase": "mid-setup", **base,
                "hint": "VM up but links/dest incomplete — resume at the "
                        "missing write-dest / write-links step."}
    return {"phase": "setup-complete", **base,
            "hint": "links + dest in place — run transfer --limit 1 (pilot)"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[
        "discover", "plan", "probe", "create-vm", "allow-network",
        "write-dest", "check-azure", "write-links", "transfer", "status",
        "verify", "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--parts", default=None,
                   help="informational part count (rides the takeout_parts "
                        "VM tag; write-links sets it from the real links)")
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None,
                   help="create-vm: OS disk GB (default: the Spec's 512 — "
                        "holds the whole 442 GB export, never mind the "
                        "4-at-a-time staging it actually needs)")
    p.add_argument("--dest-prefix", default=None,
                   help=f"prefix inside <slug>-raw (default "
                        f"{SPEC.default_dest_prefix})")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--expand-parts", dest="expand_parts", type=int,
                   default=None, metavar="N",
                   help="probe/write-links: clone the ONE cURL on stdin "
                        "across N consecutive i= part indices")
    p.add_argument("--expand-start", dest="expand_start", type=int,
                   default=None,
                   help="--expand-parts base index (default: the pasted "
                        "link's own i=, which must be 0 or 1)")
    p.add_argument("--allow-resend", dest="allow_resend",
                   action="store_true",
                   help="write-links: accept a cURL identical to the one "
                        "already on the VM (normally refused — a resent "
                        "link carries a spent one-time rapt token)")
    p.add_argument("--probe-all", dest="probe_all", action="store_true",
                   help="probe: check EVERY link, not just the first. With "
                        "a known part count this is the check worth doing.")
    p.add_argument("--timeout", type=int, default=60,
                   help="probe: per-request timeout in seconds")
    p.add_argument("--limit", type=int, default=0,
                   help="transfer: only the first N parts (pilot)")
    p.add_argument("--only-index", dest="only_index", type=int, default=None,
                   help="transfer: only the part with this i= index")
    p.add_argument("--parallel", type=int, default=None,
                   help="transfer: concurrent parts (VM default 4)")
    p.add_argument("--skip-upload", dest="skip_upload", action="store_true",
                   help="transfer: download to VM disk but don't upload")
    p.add_argument("--ignore-prior", dest="ignore_prior", action="store_true",
                   help="transfer: do not trust the destination's "
                        "manifest.json as the resume record — required "
                        "while the 2026-08-31 sign-in-page blobs are still "
                        "in the container, since their names resolve and "
                        "would make resume skip real parts")
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-transfer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    engine_cmds = {"plan": eng.cmd_plan, "create-vm": eng.cmd_create_vm,
                   "allow-network": eng.cmd_allow_network,
                   "check-azure": eng.cmd_check_azure}
    own_cmds = {"discover": cmd_discover, "probe": cmd_probe,
                "write-dest": cmd_write_dest, "write-links": cmd_write_links,
                "transfer": cmd_transfer, "status": cmd_status,
                "verify": cmd_verify, "teardown": cmd_teardown}
    try:
        if args.command in engine_cmds:
            _guard(args.slug)
            result = engine_cmds[args.command](SPEC, root, args)
        else:
            result = own_cmds[args.command](root, args)
    except common.HarnessError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    except subprocess.TimeoutExpired as e:
        print(json.dumps({"ok": False,
                          "error": f"timeout: {str(e)[:200]}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
