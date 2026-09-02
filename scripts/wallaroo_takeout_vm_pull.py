#!/usr/bin/env python3
"""VM-side Takeout link puller — pushed to the transfer VM by
scripts/wallaroo_takeout_pull.py and run there in tmux. Stdlib only.

Per archive part: download to local disk with resume, check the bytes on
disk against Google's declared Content-Length, azcopy the file into
<container>/<prefix>/, confirm the committed blob length, then DELETE the
local copy. Downloading and uploading are deliberately separate legs —
Takeout archives expire and have a limited download allowance, so an upload
hiccup must never cost a re-download. Staging is what buys that.

The blob's existence IS the resume record: azcopy commits a block blob
atomically, so a blob that is present was fully uploaded from a file whose
size had already been checked. A re-run HEADs each destination and skips
what is already there without touching Google at all.

Failure isolation (CLAUDE.md principle 4): one bad part is recorded in
failed_parts and the pass continues. Every other part still lands and still
uploads, so a mid-run VM loss costs one part rather than the run.

Cookie hygiene is duplicated from the laptop CLI on purpose (the zoho/teams
TokenBox precedent — different host, different process, no shared import):
redirects that leave google.com get Cookie/Authorization stripped before the
request goes out.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

X_MS_VERSION = "2021-08-06"
CHUNK = 8 * 1024 * 1024
MAX_ATTEMPTS = 6
DISK_SLACK = 5 * 10**9      # keep this much free beyond the part itself
DISK_WAIT_MAX = 3 * 3600    # give up waiting for a drain after 3h
UA = "cdp-harness-takeout-vm-pull/1.0"

_print_lock = threading.Lock()
_state_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}",
              flush=True)


def utc_now() -> str:
    return f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


class PartError(Exception):
    """A part failed for a reason worth recording verbatim."""


# ── cookie-guarded HTTP (duplicated from the laptop CLI on purpose) ──────────

def host_allowed(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return (host == "google.com" or host.endswith(".google.com")
            or host.endswith(".googleusercontent.com"))


class _CookieGuardRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not host_allowed(newurl):
            for store in (new.headers, new.unredirected_hdrs):
                for k in [h for h in store
                          if h.lower() in ("cookie", "authorization")]:
                    del store[k]
        return new


def open_source(url: str, headers: dict, offset: int, timeout: int = 180):
    opener = urllib.request.build_opener(_CookieGuardRedirect())
    req = urllib.request.Request(url)
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("user-agent", headers.get("user-agent", UA))
    req.add_header("range", f"bytes={offset}-")
    return opener.open(req, timeout=timeout)


_CD_STAR = re.compile(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", re.I)
_CD_PLAIN = re.compile(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)', re.I)
# Google's own names carry spaces and parentheses ("All mail Including Spam
# and Trash-002.mbox"), so those are allowed; path separators and control
# characters are not. Azure accepts these verbatim and Dest.url() percent-
# encodes on the way out.
_SAFE_NAME = re.compile(r"[A-Za-z0-9 ._+=,()&'\[\]-]{1,180}")
_KNOWN_EXT = (".zip", ".tgz", ".tar.gz", ".mbox", ".json", ".csv", ".vcf")
_RESERVED = {"manifest.json", "progress.json"}


def content_disposition_name(cd):
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


def safe_blob_name(raw, index: int) -> str:
    """Whitelist, not blacklist — a hostile Content-Disposition must not be
    able to escape the prefix or shadow our own manifest."""
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


# ── azure blob (HEAD only — azcopy does the writing) ─────────────────────────

class Dest:
    def __init__(self):
        self.base = os.environ.get("AZURE_DEST_URL", "").rstrip("/")
        self.sas = os.environ.get("AZURE_DEST_SAS", "")
        self.prefix = os.environ.get("AZURE_DEST_PREFIX", "").strip("/")
        if not self.base or not self.sas:
            raise SystemExit("AZURE_DEST_URL / AZURE_DEST_SAS missing — "
                             "dest.env was never sourced (run write-dest)")

    def url(self, name: str) -> str:
        p = f"{self.prefix}/{name}" if self.prefix else name
        return f"{self.base}/{urllib.parse.quote(p, safe='/')}"

    def head(self, name: str):
        """Committed Content-Length, or None if the blob is absent. Azcopy
        commits atomically, so 'present' means 'complete'."""
        req = urllib.request.Request(self.url(name) + "?" + self.sas,
                                     method="HEAD",
                                     headers={"x-ms-version": X_MS_VERSION})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return int(r.headers.get("Content-Length") or 0)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 403:   # IP/vnet rule propagation, not a bad SAS
                    time.sleep(15 * (attempt + 1))
                    continue
                if e.code >= 500:
                    time.sleep(1 + attempt)
                    continue
                raise PartError(f"blob HEAD failed: HTTP {e.code}")
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(2 + attempt)
        raise PartError("blob HEAD failed after retries")

    def get_json(self, name: str):
        """Read a small JSON blob, or None if absent. Used once at startup
        to recover the previous run's index -> blob_name map, which is what
        makes resume survive a VM rebuild (the names come from Google's
        Content-Disposition and are not derivable otherwise)."""
        req = urllib.request.Request(self.url(name) + "?" + self.sas,
                                     headers={"x-ms-version": X_MS_VERSION})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError,
                TimeoutError, OSError):
            return None

    def azcopy(self, local: str, name: str, overwrite: bool,
               work: str) -> None:
        env = dict(os.environ)
        env["AZCOPY_LOG_LOCATION"] = f"{work}/.azcopy-logs"
        env["AZCOPY_JOB_PLAN_LOCATION"] = f"{work}/.azcopy-plans"
        os.makedirs(env["AZCOPY_LOG_LOCATION"], exist_ok=True)
        os.makedirs(env["AZCOPY_JOB_PLAN_LOCATION"], exist_ok=True)
        cmd = ["azcopy", "copy", local, self.url(name) + "?" + self.sas,
               f"--overwrite={'true' if overwrite else 'false'}",
               "--block-size-mb", "256", "--log-level", "ERROR"]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=env, timeout=6 * 3600)
        if proc.returncode != 0:
            # never echo the command: the SAS is in it
            tail = (proc.stdout or proc.stderr or "").strip()[-400:]
            raise PartError(f"azcopy rc={proc.returncode}: {tail}")


# ── progress / manifest ──────────────────────────────────────────────────────

class Progress:
    def __init__(self, path: str, total_parts: int):
        self.path = path
        self.data = {"started_utc": utc_now(), "total_parts": total_parts,
                     "parts": {}}

    def set(self, index: int, **fields) -> None:
        with _state_lock:
            self.data["parts"].setdefault(str(index), {}).update(fields)
            self.data["updated_utc"] = utc_now()
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)


# ── the copy loop ────────────────────────────────────────────────────────────

def await_disk(stage: str, need: int) -> None:
    waited = 0
    while shutil.disk_usage(stage).free < need:
        if waited >= DISK_WAIT_MAX:
            raise PartError(
                f"insufficient disk: need {need / 1e9:.1f} GB free, have "
                f"{shutil.disk_usage(stage).free / 1e9:.1f} GB after "
                f"{waited // 60} min of waiting for other parts to drain")
        if waited == 0:
            log(f"  waiting for disk: need {need / 1e9:.1f} GB")
        time.sleep(30)
        waited += 30


class AlreadyPresent(Exception):
    """The destination blob for this part is already committed at the right
    size. Raised from inside download() the instant the response headers
    name the file, so a resumed run reads ZERO body bytes and needs no
    local state to recognise finished work."""

    def __init__(self, name: str, size: int):
        super().__init__(name)
        self.name, self.size = name, size


def download(job: dict, path: str, stage: str, prog: Progress,
             already) -> tuple[int, str]:
    """Resume-aware download. Returns (declared_total, filename-or-None).

    Range is requested every attempt; a server that answers 200 anyway is
    handing back the whole archive, so the partial file is discarded and the
    write restarts from zero rather than silently concatenating."""
    index = job["index"]
    last_err = None
    declared = None
    fname = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        offset = os.path.getsize(path) if os.path.exists(path) else 0
        if declared is not None and offset == declared:
            return declared, fname
        try:
            resp = open_source(job["url"], job["headers"], offset)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (401, 403):
                raise PartError(
                    f"HTTP {e.code} from Google — the copied session is dead "
                    "(client signed out, or the cookie stopped working from "
                    "this IP). Not a retry: the client must re-copy the cURL")
            if e.code in (404, 410):
                raise PartError(
                    f"HTTP {e.code} from Google — this archive link has "
                    "expired. Takeout archives lapse about a week after "
                    "export; a fresh export is the only fix")
            last_err = f"HTTP {e.code}"
            time.sleep(min(60, 5 * attempt))
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)[:200]
            time.sleep(min(60, 5 * attempt))
            continue

        try:
            code = resp.status
            # Is this actually the archive? Google answers an invalid session
            # with 200 + the sign-in PAGE, not an error status, so status
            # alone proves nothing. Committing that as a .zip is exactly what
            # happened on the first live run (2026-08-31): 12 blobs of
            # sign-in HTML named takeout-part-NNN.zip. Three independent
            # tells, checked before a single byte is written.
            final = resp.geturl()
            fhost = (urllib.parse.urlsplit(final).hostname or "").lower()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            cdisp = resp.headers.get("Content-Disposition")
            if "accounts.google.com" in fhost or "/signin" in final:
                raise PartError(
                    "redirected to the Google sign-in page — the copied "
                    "session is no longer valid. Not a retry: the client "
                    "must re-copy the cURL from a live session")
            if ctype.startswith("text/html"):
                raise PartError(
                    f"server returned HTML (Content-Type: {ctype}), not an "
                    "archive — almost always a sign-in or error page")
            if not cdisp:
                raise PartError(
                    "response carries no Content-Disposition, so it is not "
                    f"a file download (status {code}, Content-Type "
                    f"{ctype or 'unset'})")
            fname = fname or content_disposition_name(cdisp)
            cr = resp.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)\s*$", cr)
            clen = resp.headers.get("Content-Length")
            if code == 206 and m:
                declared = int(m.group(1))          # the WHOLE file
            elif clen is not None and code == 206:
                declared = offset + int(clen)       # no Content-Range: add
            elif clen is not None:
                declared = int(clen)                # 200: the whole body
            # Headers are enough to know whether this part is already done.
            # Checking here (not before the request) means resume needs no
            # local state and still reads no body bytes.
            if offset == 0 and declared:
                landed = already(safe_blob_name(fname, index), declared)
                if landed is not None:
                    raise AlreadyPresent(landed[0], landed[1])
            if code != 206 and offset:
                # Range ignored: the body is the WHOLE file, so the partial
                # on disk is not a prefix of what is arriving.
                log(f"[{index}] server ignored Range (HTTP {code}) — "
                    "restarting this part from zero")
                offset = 0
            if declared:
                need = (declared - offset) + DISK_SLACK
                if shutil.disk_usage(stage).free < need:
                    resp.close()
                    await_disk(stage, need)
                    continue
            mode = "ab" if (code == 206 and offset) else "wb"
            written = offset if mode == "ab" else 0
            prog.set(index, state="downloading", bytes_done=written,
                     declared_bytes=declared, attempt=attempt)
            with open(path, mode) as f:
                while True:
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    written += len(buf)
                    if written % (512 * 1024 * 1024) < CHUNK:
                        prog.set(index, bytes_done=written)
            prog.set(index, bytes_done=written)
            if declared is None:
                declared = written        # server never said; trust the wire
            if written == declared:
                return declared, fname
            last_err = (f"short read: {written} of {declared} bytes")
            log(f"[{index}] {last_err} — resuming (attempt {attempt})")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)[:200]
            log(f"[{index}] transport error: {last_err} — resuming "
                f"(attempt {attempt})")
            time.sleep(min(60, 5 * attempt))
        finally:
            resp.close()
    raise PartError(f"download failed after {MAX_ATTEMPTS} attempts: "
                    f"{last_err}")


def process_part(job: dict, dest: Dest, stage: str, prog: Progress,
                 prior: dict, args) -> dict:
    """One archive part, end to end. Never raises: a failure is recorded and
    the pass continues (CLAUDE.md principle 4)."""
    index = job["index"]
    rec = {"index": index, "status": "failed", "blob_name": None,
           "declared_bytes": None, "committed_bytes": None, "error": None}
    tmp = os.path.join(stage, f"part-{index:03d}.download")

    def already(name: str, declared: int):
        """(name, size) if this part is already committed, else None. Tries
        the name the previous run recorded before the one these headers
        imply — Takeout filenames come from Content-Disposition, so the
        prior manifest is the only way to recognise finished work without
        re-deriving the name."""
        for cand in dict.fromkeys(filter(None, [prior.get(index), name])):
            size = dest.head(cand)
            if size is not None and (declared is None or size == declared):
                return cand, size
        return None

    try:
        # 1. Free skip: the previous run's manifest names the blob, so a
        #    HEAD settles it without a single byte from Google.
        if prior.get(index):
            size = dest.head(prior[index])
            if size is not None:
                log(f"[{index}] already committed as {prior[index]} "
                    f"({size / 1e9:.2f} GB) — skipping")
                prog.set(index, state="already-present",
                         blob_name=prior[index])
                return {**rec, "status": "ok", "blob_name": prior[index],
                        "declared_bytes": size, "committed_bytes": size,
                        "skipped": "already-present"}

        # 2. Download (resumable). Bails out for free if the response
        #    headers reveal the part already landed under a new name.
        try:
            declared, fname = download(job, tmp, stage, prog, already)
        except AlreadyPresent as e:
            log(f"[{index}] already committed as {e.name} "
                f"({e.size / 1e9:.2f} GB) — skipping")
            prog.set(index, state="already-present", blob_name=e.name)
            return {**rec, "status": "ok", "blob_name": e.name,
                    "declared_bytes": e.size, "committed_bytes": e.size,
                    "skipped": "already-present"}

        on_disk = os.path.getsize(tmp)
        if on_disk != declared:
            raise PartError(f"size check failed: {on_disk} on disk vs "
                            f"{declared} declared by Google")
        name = safe_blob_name(fname, index)
        rec.update({"blob_name": name, "declared_bytes": declared})
        prog.set(index, state="downloaded", blob_name=name,
                 declared_bytes=declared, bytes_done=on_disk)

        if args.skip_upload:
            prog.set(index, state="downloaded-not-uploaded")
            return {**rec, "status": "ok", "skipped": "upload-skipped"}

        # Two parts resolving to one blob name would mean --overwrite=false
        # silently "succeeds" while one part's data never lands.
        existing = dest.head(name)
        if existing is not None and existing != declared:
            raise PartError(
                f"blob '{name}' already exists at {existing} bytes but this "
                f"part is {declared} — two parts resolved to the same name. "
                "Refusing to touch it; re-check the link set with probe.")

        # 3. Upload, then confirm what Azure actually committed.
        final = os.path.join(stage, name)
        os.replace(tmp, final)
        prog.set(index, state="uploading")
        dest.azcopy(final, name, overwrite=False, work=stage)
        committed = dest.head(name)
        if committed != declared:
            raise PartError(f"committed {committed} bytes, expected "
                            f"{declared} — upload incomplete; the blob is "
                            "left in place for inspection")
        rec.update({"status": "ok", "committed_bytes": committed})

        # 4. Only now is the local copy expendable.
        os.remove(final)
        prog.set(index, state="done", committed_bytes=committed)
        log(f"[{index}] {name}: {committed / 1e9:.2f} GB committed, local "
            "copy removed")
        return rec
    except PartError as e:
        rec["error"] = str(e)
        log(f"[{index}] FAILED: {e}")
        prog.set(index, state="failed", error=str(e)[:300])
        return rec
    except Exception as e:                      # noqa: BLE001 — isolation
        rec["error"] = f"{type(e).__name__}: {str(e)[:250]}"
        log(f"[{index}] FAILED: {rec['error']}")
        prog.set(index, state="failed", error=rec["error"])
        return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--links", required=True)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-index", dest="only_index", type=int, default=None)
    ap.add_argument("--skip-upload", dest="skip_upload", action="store_true")
    ap.add_argument("--ignore-prior", dest="ignore_prior", action="store_true",
                    help="do not trust the destination's manifest.json as "
                         "the resume record. Needed when a previous pass "
                         "committed blobs that are NOT the archives it "
                         "claims (the 2026-08-31 sign-in-page run): those "
                         "names still resolve, so resume would skip real "
                         "work. Parts are then re-fetched and judged on "
                         "what the server actually returns.")
    args = ap.parse_args()

    with open(args.links) as f:
        jobs = json.load(f)["jobs"]
    jobs.sort(key=lambda j: j["index"])
    part_count = len(jobs)          # what the LINKS said exists — the
    if args.only_index is not None:  # completeness bar verify checks against
        jobs = [j for j in jobs if j["index"] == args.only_index]
    if args.limit:
        jobs = jobs[:args.limit]
    if not jobs:
        log("no parts selected — nothing to do")
        return 1

    os.makedirs(args.stage, exist_ok=True)
    dest = Dest()
    prog = Progress(os.path.join(args.stage, "progress.json"), part_count)
    prior: dict = {}
    if args.ignore_prior:
        log("--ignore-prior: the destination manifest is NOT trusted as a "
            "resume record; every selected part is re-fetched")
    else:
        prior_manifest = dest.get_json("manifest.json") or {}
        prior = {p["index"]: p["blob_name"]
                 for p in (prior_manifest.get("parts") or [])
                 if p.get("status") == "ok" and p.get("blob_name")}
        if prior:
            log(f"prior manifest names {len(prior)} committed part(s) — "
                "those are HEAD-checked and skipped without touching Google")
    du = shutil.disk_usage(args.stage)
    log(f"start: {len(jobs)} of {part_count} parts, parallel={args.parallel}, "
        f"disk {du.free / 1e9:.0f} GB free of {du.total / 1e9:.0f} GB, "
        f"dest {dest.prefix}/")

    results: list[dict] = []
    queue = list(jobs)
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not queue:
                    return
                job = queue.pop(0)
            rec = process_part(job, dest, args.stage, prog, prior, args)
            with qlock:
                results.append(rec)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, min(args.parallel, len(jobs))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    results.sort(key=lambda r: r["index"])
    failed = [r["index"] for r in results if r["status"] != "ok"]
    ok = [r for r in results if r["status"] == "ok"]
    manifest = {
        "slug": "wallaroo-media",
        "dest_prefix": dest.prefix,
        "started_utc": prog.data["started_utc"],
        "finished_utc": utc_now(),
        "part_count": part_count,
        "parts_attempted": len(jobs),
        "partial_run": len(jobs) != part_count,
        "total_declared_bytes": sum(r["declared_bytes"] or 0 for r in ok),
        "total_committed_bytes": sum(r["committed_bytes"] or 0 for r in ok),
        "parts": results,
        "failed_parts": failed,
    }
    mpath = os.path.join(args.stage, "manifest.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2)
    if not args.skip_upload:
        try:
            # --overwrite=true: a --limit pilot's manifest must never be the
            # one verify reads (the github pilot-poisons-verify lesson).
            dest.azcopy(mpath, "manifest.json", overwrite=True,
                        work=args.stage)
        except PartError as e:
            log(f"manifest upload failed: {e}")
            return 2
    log(f"done: {len(ok)} ok, {len(failed)} failed, "
        f"{manifest['total_committed_bytes'] / 1e9:.1f} GB committed"
        + (f"; failed parts: {failed}" if failed else ""))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
