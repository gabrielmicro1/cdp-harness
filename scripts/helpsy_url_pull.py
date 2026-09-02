#!/usr/bin/env python3
"""helpsy AWS pull -- ONE-OFF (helpsy only, slug-guarded).

Helpsy delivered their AWS corpus as two gzipped URL LISTS uploaded into their
own container instead of an IAM key for the s3-azure-transfer family:

  aws/presigned-urls.txt.gz   1,263 objects, 4 buckets, SigV4 presigned with
                              X-Amz-Expires=172800 -- a 48 h clock
  aws/public-urls.txt.gz      1,721,773 objects, 3 buckets, anonymous, no expiry

Both lists are "FILE: <url>" per line. No sizes, no keys, no manifest.

NO VM. Azure's Put Blob From URL is a SERVER-SIDE copy -- the storage fabric
fetches each object from S3 itself, so the driver issues one small control call
per object and the bytes never transit it. That is the vimeo/zoom/slack
transport, proven at scale here (checkmate: 242.6M objects via s3_flat.py). A
VM would not make Azure's fetch faster; it only buys unattended runtime, which
a 1-3 h job does not need. So this is a standalone laptop-local ingest in the
qwilr/vimeo/zoom family: firewall via phases.ip_rule_ensure (our external IP;
allow-network is the VM path and does not apply), racwl container SAS held only
in this process, create-only writes, no state file in Azure.

Dest is `aws/<bucket>/<key>` -- INSIDE the client's own aws/ prefix, next to
their two lists. That is the saxon SharePoint situation, so the same discipline
applies: create-only is API-ENFORCED by If-None-Match: *, and ZERO bookkeeping
blobs go into the container. Every byte of run state lives locally under
companies/helpsy/aws-pull/ (gitignored).

Subcommands:
  plan    read both lists, report per-bucket counts, derived names, exclusions,
          and the presigned deadline. No writes anywhere.
  probe   day-one gate: sample-fetch per bucket (HEAD for public; Range GET for
          presigned -- a presigned GET signature is method-bound, so HEAD 403s),
          check for redirects (Azure does NOT follow them), report the presigned
          window. Exit 2 if that window has closed.
  split   build the local chunk manifests + sorted expected-name files.
  pull    the copy. The presigned set ALWAYS runs first (it is the one with a
          clock). Resumable: create-only means a re-run costs one 409 per
          already-landed blob, and chunk markers keep the replay to one chunk.
  verify  per-bucket streaming merge-join of the expected names against the
          container listing. Asserts PRESENCE, not source bytes: the lists
          declare no sizes, so no source-size claim is made (the
          github/zoho/figma honesty level). Measured bytes come from the dest.
  status  chunk progress, for checking a detached run.

One JSON object on stdout; exit 0 ok / 1 hard error / 2 refusal.
"""
from __future__ import annotations

import base64
import concurrent.futures
import gzip
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import common
import phases
# The keep-alive transport is lifted verbatim from the flat-S3 engine: one
# persistent HTTPS connection per thread is what makes this loop latency-bound
# instead of burning a core per worker on TLS handshakes (measured live,
# checkmate 2026-08-22). Import-time is stdlib-only there (boto3 is lazy).
from s3_flat import _conn, _drop_conn

ONLY_SLUG = "helpsy"          # one-off-ness enforced in code, not just the name
DEFAULT_DEST_PREFIX = "aws"   # the client's own prefix -- see the docstring
LIST_BLOBS = {
    "presigned": "aws/presigned-urls.txt.gz",
    "public": "aws/public-urls.txt.gz",
}
LINE_PREFIX = "FILE: "

X_MS_VERSION = "2021-08-06"   # >= 2020-04-08: Put Blob From URL
MIB = 1024 * 1024
SINGLE_SHOT_MAX = 256 * MIB   # the family's live-proven ceiling (slack, s3_flat)
BLOCK_SIZE = 256 * MIB
MAX_BLOCKS = 50_000           # Azure's committed-block cap per blob
CHUNK_ROWS = 50_000           # a crash replays at most this many cheap 409s
CONSEC_SOURCE_403_ABORT = 25  # a revoked bucket policy must not scroll past
CONSEC_DEST_403_REGRANT = 10  # rg-corpus-*-prod provisioners strip IP rules
MAX_REGRANTS = 3
PROBE_SAMPLE = 40

_sleep = time.sleep            # seam so tests can record/skip waits


# ── pure logic (unit-tested offline; no network, no Azure) ───────────────────

def parse_line(line: str) -> str | None:
    """One list line -> the URL, or None for a line that carries none."""
    line = line.strip()
    if not line:
        return None
    if line.startswith(LINE_PREFIX):
        line = line[len(LINE_PREFIX):].strip()
    if not line.lower().startswith("https://"):
        return None
    return line


def bucket_of(host: str) -> str:
    """Virtual-hosted S3 host -> bucket name.

    Handles both the global endpoint (<bucket>.s3.amazonaws.com) and the
    regional one (<bucket>.s3.<region>.amazonaws.com). A host we do not
    recognise returns "" so the caller can record it rather than guess a
    destination path for it."""
    h = host.lower().split(":")[0]
    m = re.match(r"^(?P<b>.+?)\.s3[.-][a-z0-9-]*\.?amazonaws\.com$", h)
    if m:
        return m.group("b")
    m = re.match(r"^(?P<b>.+?)\.s3\.amazonaws\.com$", h)
    return m.group("b") if m else ""


def blob_name(url: str, dest_prefix: str = DEFAULT_DEST_PREFIX):
    """URL -> (blob_name, skip_reason, note).

    The blob name is `<dest_prefix>/<bucket>/<key>` where <key> is the
    PERCENT-DECODED url path minus its single leading '/'. The copy SOURCE is
    always the line verbatim (it is already correctly encoded); only the name
    is decoded, so the blob is named after the real S3 key.

    Bucket-scoping guarantees no collision across the seven source buckets and
    none of the derived names can collide with the client's own
    aws/presigned-urls.txt.gz / aws/public-urls.txt.gz.

    Real edge cases in this corpus, all legal because the account has no
    hierarchical namespace (isHnsEnabled off -- verified 2026-08-31):
      * 8 presigned keys end in '/' (S3 folder placeholders, zero bytes). A
        blob name may not end in '/', and they carry nothing -> skipped.
      * 175 public keys ARE urls ("https%3A//helpsy-images-public.s3..."). All
        175 probed 200 -- they are real objects with strange names, so the
        decoded name keeps the embedded "https://".
      * 1 public key has a leading slash (".../s3.amazonaws.com//measurmentsV2/
        DEV45.jpg", probed 200) -> "aws/<bucket>//measurmentsV2/DEV45.jpg".
      * 159 public keys contain a literal BACKSLASH ("visiON%5C<uuid>"). Azure
        treats '\\' as a path separator: the blob is addressable under either
        form but List Blobs returns the '/' form, so the name is normalised
        HERE. Without it the copy still lands correctly and verify then reports
        every one of them as missing-and-extra at once -- observed live,
        2026-08-31. Normalising can in principle collide two distinct S3 keys
        into one blob; it does not in this corpus (checked: 0 collisions across
        all 1,721,773 keys), and the note makes any future one visible.
    A note (never a drop) is also recorded when re-encoding the decoded key
    does not reproduce the path -- the anomaly detector for a literal '%'."""
    parts = urllib.parse.urlsplit(url)
    bucket = bucket_of(parts.netloc)
    if not bucket:
        return "", "unrecognised-host", parts.netloc
    raw_key = parts.path[1:] if parts.path.startswith("/") else parts.path
    if not raw_key:
        return "", "empty-key", ""
    key = urllib.parse.unquote(raw_key)
    if key.endswith("/"):
        return "", "folder-placeholder", ""
    if "\t" in key or "\n" in key or "\r" in key:
        return "", "unsafe-key-char", repr(key)
    notes = []
    if urllib.parse.quote(key, safe="/") != raw_key:
        notes.append("reencode-differs")
    if "\\" in key:
        key = key.replace("\\", "/")
        notes.append("backslash-normalised")
    return f"{dest_prefix}/{bucket}/{key}", "", "+".join(notes)


def presigned_deadline(url: str) -> datetime | None:
    """X-Amz-Date + X-Amz-Expires -> the moment this URL stops working."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    d = (q.get("X-Amz-Date") or [None])[0]
    e = (q.get("X-Amz-Expires") or [None])[0]
    if not d or not e:
        return None
    try:
        base = datetime.strptime(d, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return base + timedelta(seconds=int(e))
    except (ValueError, TypeError):
        return None


def chunk_rows(rows, per_chunk: int):
    """Pure generator: (url, name) pairs -> (chunk_index, text) manifests.
    Rows are "url\\tname"; O(per_chunk) memory."""
    buf: list[str] = []
    idx = 0
    for url, name in rows:
        buf.append(f"{url}\t{name}\n")
        if len(buf) >= per_chunk:
            yield idx, "".join(buf)
            idx += 1
            buf = []
    if buf:
        yield idx, "".join(buf)


def classify(azure_status: int, azure_code: str, source_status: str) -> str:
    """PURE. What to do about one failed server-side copy.

    Keyed on the COPY-SOURCE status (Azure surfaces S3's answer in
    `x-ms-copy-source-status-code`) -- the slack rule, because two different
    servers can refuse us:

      ok           2xx, or a 409 that IS BlobAlreadyExists (If-None-Match
                   did its job). A 409 carrying any OTHER code is NOT ok --
                   with no census, an oversized source is exactly the kind of
                   refusal Azure can answer 409 to, and reading that as
                   "already landed" would silently drop the object.
      sleep        429 from either side
      source-auth  S3 said 401/403 -- a presigned URL past its window, or a
                   bucket policy that stopped being public. The caller aborts
                   the SET after CONSEC_SOURCE_403_ABORT in a row: one odd
                   object must not kill a 1.7M-object run, and 1.7M logged
                   failures must not scroll past a revoked policy.
      skip         S3 said 404 -- deleted since the list was generated
      regrant      dest 403 -- the IP rule went away (helpsy sits in a
                   rg-corpus-*-prod RG, where the provisioner is known to
                   strip rules); re-ensure it and retry
      retry        5xx / 408 from either side
      size-probe   Azure refused while the SOURCE was healthy. With no census
                   this IS the large-object routing rule: probe that one
                   object's size and, above SINGLE_SHOT_MAX, redo it
                   block-staged. Matching on "source fine, Azure said no"
                   avoids depending on Azure's exact oversized-source code.
    """
    src = int(source_status) if str(source_status).strip().isdigit() else 0
    if azure_status in (200, 201, 202):
        return "ok"
    if azure_status == 409 and azure_code in ("", "BlobAlreadyExists"):
        return "ok"
    if src == 429 or azure_status == 429:
        return "sleep"
    if src in (401, 403):
        return "source-auth"
    if src == 404:
        return "skip"
    if azure_status == 403:
        return "regrant"
    if src >= 500 or azure_status >= 500 or azure_status == 408:
        return "retry"
    return "size-probe"


# ── local state (nothing of ours is ever written into the container) ─────────

def guard_slug(slug: str) -> None:
    if slug != ONLY_SLUG:
        raise common.HarnessError(
            f"this is a ONE-OFF tool for {ONLY_SLUG!r} only (its source lists, "
            f"dest paths and clock are that engagement's); got {slug!r}")


def work_dir(root: Path) -> Path:
    return root / ONLY_SLUG / "aws-pull"


def chunks_dir(root: Path) -> Path:
    return work_dir(root) / "chunks"


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(text)
        f.flush()


def log(root: Path, msg: str) -> None:
    _append(work_dir(root) / "pull.log",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + msg + "\n")


# ── Azure REST ──────────────────────────────────────────────────────────────

class CopyError(Exception):
    def __init__(self, code: str, status: int = 0, source_status: str = ""):
        super().__init__(code)
        self.code = code
        self.status = status
        self.source_status = source_status


def container_url(cfg: dict) -> str:
    return (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")


def blob_url(cfg: dict, name: str) -> str:
    return container_url(cfg) + "/" + urllib.parse.quote(name, safe="/")


def mint_write_sas(cfg: dict, days: int, dry_run: bool) -> tuple[str, str]:
    """Container SAS, racwl -- the sanctioned ingest write path (CLAUDE.md
    principle 3). 2 days by default: a ~400 GB server-side copy is realistically
    multi-hour and a resume must not straddle the expiry mid-block-loop. Held
    only in this process's memory; never written to disk, argv or a log."""
    expiry = common.iso(common.utc_now() + timedelta(days=days))
    proc = common.run_az(["storage", "container", "generate-sas",
                          "--account-name", cfg["storage_account"],
                          "-n", cfg["container"],
                          "--permissions", "racwl",
                          "--expiry", expiry, "--https-only",
                          "-o", "tsv"], dry_run=dry_run, timeout=120)
    return ("<sas-redacted>" if dry_run else proc.stdout.strip()), expiry


def azure_put(url: str, headers: dict, body: bytes = b"") -> tuple[int, str, str]:
    """One PUT over this thread's keep-alive connection.

    Returns (status, azure_error_code, copy_source_status). Never retries --
    the caller owns the retry policy so that classify() sees every answer.
    Adapted from s3_flat._azure_put, which reuses the same _conn pool."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path + ("?" + parts.query if parts.query else "")
    try:
        c = _conn(parts.netloc)
        c.request("PUT", path, body=body, headers=headers)
        r = c.getresponse()
        payload = r.read()  # drain so the connection can be reused
        if r.status in (200, 201, 202):
            return r.status, "", ""
        code = r.getheader("x-ms-error-code") or ""
        if not code:
            m = re.search(rb"<Code>([^<]+)</Code>", payload)
            code = m.group(1).decode() if m else str(r.status)
        src = (r.getheader("x-ms-copy-source-status-code") or "").strip()
        if not src:
            m = re.search(rb"<CopySourceStatusCode>([^<]+)</CopySourceStatusCode>",
                          payload)
            src = m.group(1).decode() if m else ""
        return r.status, code, src
    except Exception as exc:  # noqa: BLE001 -- a dropped keep-alive is normal churn
        _drop_conn()
        raise CopyError(type(exc).__name__) from exc


def source_size(url: str, presigned: bool, timeout: int = 60) -> int:
    """One object's size, straight from S3. HEAD for the public buckets; a
    Range GET for the presigned ones, whose SigV4 signature is method-bound
    (a HEAD against a presigned GET url answers 403)."""
    if presigned:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cr = r.headers.get("Content-Range") or ""
            return int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(r.headers.get("Content-Length") or 0)


def _block_id(i: int) -> str:
    return base64.b64encode(f"helpsyaws{i:08d}".encode()).decode()


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.completed = self.skipped = self.failed = self.missing = 0
        self.bytes = 0
        self.blocked = 0          # objects that needed the block-staged path
        self.throttles = 0
        self.errors: dict[str, int] = {}

    def add(self, outcome: str, size: int = 0, err: str = "") -> None:
        with self.lock:
            setattr(self, outcome, getattr(self, outcome) + 1)
            if outcome == "completed":
                self.bytes += size
            if err:
                self.errors[err] = self.errors.get(err, 0) + 1

    def bump(self, field: str) -> None:
        with self.lock:
            setattr(self, field, getattr(self, field) + 1)

    def as_dict(self) -> dict:
        # `bytes` is deliberately NOT reported as a run total: a single-shot
        # Put Blob From URL never tells us the size and we do no census, so
        # the only bytes this loop can count are the block-staged ones. The
        # honest total comes from `verify`, which reads it off the container.
        return {"completed": self.completed, "skipped": self.skipped,
                "missing": self.missing, "failed": self.failed,
                "block_staged": self.blocked,
                "block_staged_bytes": self.bytes,
                "throttles": self.throttles, "errors": dict(self.errors)}


class Guard:
    """Run-wide abort + firewall-regrant state, shared by every worker."""

    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.lock = threading.Lock()
        self.consec_source_403 = 0
        self.consec_dest_403 = 0
        self.regrants = 0
        self.abort_cause = ""

    @property
    def aborted(self) -> bool:
        return bool(self.abort_cause)

    def ok(self) -> None:
        with self.lock:
            self.consec_source_403 = 0
            self.consec_dest_403 = 0

    def source_403(self, set_name: str) -> None:
        with self.lock:
            self.consec_source_403 += 1
            if self.consec_source_403 >= CONSEC_SOURCE_403_ABORT and not self.abort_cause:
                self.abort_cause = (
                    "presigned-expired-or-invalid" if set_name == "presigned"
                    else "public-access-revoked")

    def dest_403(self) -> None:
        """A dest 403 burst means our IP rule is gone. Re-ensure it once per
        burst (bounded), which is the automated form of CLAUDE.md's 'if a 403
        reappears mid-transfer, re-run allow-network'."""
        with self.lock:
            self.consec_dest_403 += 1
            if self.consec_dest_403 < CONSEC_DEST_403_REGRANT:
                return
            if self.regrants >= MAX_REGRANTS:
                if not self.abort_cause:
                    self.abort_cause = "dest-403-firewall-unrecoverable"
                return
            self.regrants += 1
            self.consec_dest_403 = 0
        phases.ip_rule_ensure(self.cfg, dry_run=self.dry_run)


# ── the copy ────────────────────────────────────────────────────────────────

def _dest_headers() -> dict:
    # If-None-Match: * makes create-only API-ENFORCED, not a client-side flag.
    # It is why writing into the client's OWN aws/ prefix is safe, and why
    # resume is free: an already-landed blob answers 409 immediately.
    return {"x-ms-version": X_MS_VERSION, "If-None-Match": "*"}


def copy_blocks(cfg: dict, sas: str, name: str, src: str, size: int) -> None:
    """Stage the object as server-side blocks, then commit. Only reached for
    an object above SINGLE_SHOT_MAX, which the size-probe fallback discovered."""
    n_blocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    if n_blocks > MAX_BLOCKS:
        raise CopyError(f"needs {n_blocks} blocks (> {MAX_BLOCKS})")
    base = blob_url(cfg, name)
    ids = []
    for i in range(n_blocks):
        start = i * BLOCK_SIZE
        end = min(size, start + BLOCK_SIZE) - 1
        bid = _block_id(i)
        status, code, src_status = azure_put(
            f"{base}?comp=block&blockid={urllib.parse.quote(bid)}&{sas}",
            {"x-ms-version": X_MS_VERSION, "x-ms-copy-source": src,
             "x-ms-source-range": f"bytes={start}-{end}"})
        if status not in (200, 201, 202):
            raise CopyError(code or str(status), status, src_status)
        ids.append(bid)
    body = ("<?xml version='1.0' encoding='utf-8'?><BlockList>"
            + "".join(f"<Uncommitted>{b}</Uncommitted>" for b in ids)
            + "</BlockList>").encode()
    # If-None-Match rides the COMMIT: uncommitted blocks are invisible, so this
    # is the moment the blob comes into existence (the vimeo rule).
    status, code, src_status = azure_put(f"{base}?comp=blocklist&{sas}",
                                         _dest_headers(), body=body)
    if status not in (200, 201, 202) and status != 409:
        raise CopyError(code or str(status), status, src_status)


def copy_one(cfg: dict, sas: str, url: str, name: str, presigned: bool,
             counters: Counters, guard: Guard, tries: int = 5) -> None:
    """One object, single-shot Put Blob From URL with the lazy large-object
    fallback. Zero HEADs on the happy path -- an object is only sized when
    Azure refuses it while S3 itself answered fine."""
    base = blob_url(cfg, name)
    delay = 1.0
    for attempt in range(tries):
        if guard.aborted:
            counters.add("failed", err="aborted")
            return
        try:
            h = _dest_headers()
            h.update({"x-ms-blob-type": "BlockBlob", "x-ms-copy-source": url})
            status, code, src_status = azure_put(f"{base}?{sas}", h)
        except CopyError as exc:
            if attempt == tries - 1:
                counters.add("failed", err=exc.code)
                return
            _sleep(delay)
            delay = min(delay * 2, 30)
            continue

        action = classify(status, code, src_status)
        if action == "ok":
            guard.ok()
            counters.add("skipped" if status == 409 else "completed")
            return  # a 409 here is BlobAlreadyExists -- resume working
        if action == "skip":
            guard.ok()
            counters.add("missing", err="source-404")
            return
        if action == "source-auth":
            guard.source_403("presigned" if presigned else "public")
            counters.add("failed", err=f"source-{src_status or '403'}")
            return
        if action == "sleep":
            counters.bump("throttles")
            _sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if action == "regrant":
            counters.bump("throttles")
            guard.dest_403()
            _sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if action == "retry":
            if attempt == tries - 1:
                counters.add("failed", err=code or str(status))
                return
            _sleep(delay)
            delay = min(delay * 2, 30)
            continue

        # size-probe: Azure refused while S3 was healthy. THIS is the
        # large-object routing rule in a no-census run.
        try:
            size = source_size(url, presigned)
        except Exception as exc:  # noqa: BLE001 -- the probe is best-effort
            counters.add("failed", err=f"{code or status}/probe-{type(exc).__name__}")
            return
        if size <= SINGLE_SHOT_MAX:
            counters.add("failed", err=code or str(status))
            return
        try:
            copy_blocks(cfg, sas, name, url, size)
        except CopyError as exc:
            counters.add("failed", err=f"block/{exc.code}")
            return
        guard.ok()
        counters.bump("blocked")
        counters.add("completed", size)
        return
    counters.add("failed", err="retries-exhausted")


def is_presigned(url: str) -> bool:
    """A property of the ROW, not the chunk: a mop-up chunk can legitimately
    mix both sets, and the presigned/public split decides how an object is
    sized (Range GET vs HEAD) and how a source 403 is named."""
    return "X-Amz-Signature=" in url


def run_chunk(cfg: dict, sas: str, path: Path, concurrency: int,
              counters: Counters, guard: Guard) -> None:
    rows = []
    with path.open() as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln:
                continue
            url, _, name = ln.partition("\t")
            if name:
                rows.append((url, name))
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(copy_one, cfg, sas, u, n, is_presigned(u),
                          counters, guard)
                for u, n in rows]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()


# ── the client's two lists ──────────────────────────────────────────────────

def azure_get(url: str, tries: int = 4) -> bytes:
    """GET with retries. A 403 early in a run is usually IP-rule propagation
    (CLAUDE.md lore) -- wait and retry, never re-mint the SAS for it."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url,
                                         headers={"x-ms-version": X_MS_VERSION})
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < tries - 1:
                _sleep(5 * (attempt + 1))
    raise common.HarnessError(f"could not read {url.split('?')[0]}: {last}")


def fetch_lists(root: Path, cfg: dict, refresh: bool = False,
                dry_run: bool = False) -> dict:
    """Cache both list blobs locally. Read-only: rl account SAS, and
    phases.ip_rule_ensure (the laptop path -- our IP is normally already a
    pre-existing rule on this SA, so nothing is added and nothing removed)."""
    wd = work_dir(root)
    wd.mkdir(parents=True, exist_ok=True)
    paths = {s: wd / f"{s}-urls.txt.gz" for s in LIST_BLOBS}
    need = [s for s, p in paths.items() if refresh or not p.exists()]
    if not need or dry_run:
        return paths
    we_added, ip = phases.ip_rule_ensure(cfg, dry_run=dry_run)
    try:
        sas = phases.mint_sas(cfg, dry_run=dry_run, days=1)
        for s in need:
            data = azure_get(f"{container_url(cfg)}/{LIST_BLOBS[s]}?{sas}")
            paths[s].write_bytes(data)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, dry_run=dry_run)
    return paths


def iter_urls(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for ln in f:
            url = parse_line(ln)
            if url:
                yield url


def survey(paths: dict, dest_prefix: str) -> dict:
    """Both lists -> per-set counts, per-bucket counts, exclusions, notes and
    the presigned deadline. Pure over already-fetched files."""
    out = {}
    for s, p in paths.items():
        buckets: dict[str, int] = {}
        skips: dict[str, int] = {}
        notes: dict[str, int] = {}
        total = copyable = 0
        deadline = None
        for url in iter_urls(p):
            total += 1
            if s == "presigned" and deadline is None:
                deadline = presigned_deadline(url)
            name, skip, note = blob_name(url, dest_prefix)
            if skip:
                skips[skip] = skips.get(skip, 0) + 1
                continue
            copyable += 1
            b = name.split("/")[1]
            buckets[b] = buckets.get(b, 0) + 1
            if note:
                notes[note] = notes.get(note, 0) + 1
        out[s] = {"urls": total, "copyable": copyable,
                  "buckets": dict(sorted(buckets.items(), key=lambda kv: -kv[1])),
                  "excluded": skips, "name_notes": notes,
                  "expires_at": common.iso(deadline) if deadline else None}
    return out


# ── verbs ───────────────────────────────────────────────────────────────────

def load_cfg(root: Path, slug: str) -> dict:
    guard_slug(slug)
    try:
        return common.load_config(root, slug)
    except FileNotFoundError:
        raise common.HarnessError(
            f"{slug} is not onboarded (no companies/{slug}/config.json)")


def cmd_plan(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    common.ensure_subscription(dry_run=args.dry_run)
    paths = fetch_lists(root, cfg, refresh=args.refresh_lists,
                        dry_run=args.dry_run)
    s = survey(paths, args.dest_prefix)
    return {"ok": True, "command": "plan", "slug": args.slug,
            "container": cfg["container"], "dest_prefix": args.dest_prefix,
            "sets": s,
            "total_copyable": sum(v["copyable"] for v in s.values()),
            "naming": f"{args.dest_prefix}/<bucket>/<percent-decoded s3 key>",
            "writes": "create-only (If-None-Match: *); no bookkeeping blobs "
                      "go into the container -- run state is local under "
                      f"{work_dir(root)}"}


def cmd_probe(root: Path, args) -> dict:
    """Day-one gate: is every source still fetchable, does anything redirect
    (Azure does NOT follow redirects), and how long do the presigned URLs
    have left? No Azure writes."""
    import random
    cfg = load_cfg(root, args.slug)
    common.ensure_subscription(dry_run=args.dry_run)
    paths = fetch_lists(root, cfg, refresh=args.refresh_lists,
                        dry_run=args.dry_run)
    now = common.utc_now()
    result = {"ok": True, "command": "probe", "slug": args.slug, "sets": {}}
    refusal = None
    for s, p in paths.items():
        presigned = s == "presigned"
        by_bucket: dict[str, list[str]] = {}
        deadline = None
        for url in iter_urls(p):
            if presigned and deadline is None:
                deadline = presigned_deadline(url)
            name, skip, _ = blob_name(url, args.dest_prefix)
            if skip:
                continue
            by_bucket.setdefault(name.split("/")[1], []).append(url)
        rng = random.Random(17)
        per = {}
        for b, urls in by_bucket.items():
            sample = rng.sample(urls, min(PROBE_SAMPLE, len(urls)))
            statuses: dict[str, int] = {}
            redirects = 0
            sizes = []
            for u in sample:
                st, sz, loc = _probe_one(u, presigned)
                statuses[st] = statuses.get(st, 0) + 1
                if loc:
                    redirects += 1
                if sz:
                    sizes.append(sz)
            per[b] = {"objects": len(urls), "sampled": len(sample),
                      "statuses": statuses, "redirects": redirects,
                      "mean_sampled_bytes": int(sum(sizes) / len(sizes)) if sizes else None,
                      "projected_bytes": int(sum(sizes) / len(sizes) * len(urls))
                      if sizes else None}
        entry = {"buckets": per,
                 "projected_bytes": sum(v["projected_bytes"] or 0
                                        for v in per.values())}
        if presigned:
            entry["expires_at"] = common.iso(deadline) if deadline else None
            if deadline:
                left = (deadline - now).total_seconds()
                entry["hours_left"] = round(left / 3600, 1)
                if left <= 0:
                    refusal = ("presigned window CLOSED -- the client must "
                               "regenerate aws/presigned-urls.txt.gz")
        result["sets"][s] = entry
    if refusal:
        result["ok"] = False
        result["cause"] = refusal
    return result


def _probe_one(url: str, presigned: bool) -> tuple[str, int, str]:
    """(status, size, redirect-location). Never follows a redirect: Azure's
    Put Blob From URL does not either, so a 3xx here is a real hazard."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path + ("?" + parts.query if parts.query else "")
    import http.client
    conn = http.client.HTTPSConnection(parts.netloc, timeout=30)
    try:
        method = "GET" if presigned else "HEAD"
        headers = {"Range": "bytes=0-0"} if presigned else {}
        conn.request(method, path, headers=headers)
        r = conn.getresponse()
        r.read()
        size = 0
        if presigned:
            cr = r.headers.get("Content-Range") or ""
            size = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
        else:
            size = int(r.headers.get("Content-Length") or 0)
        return str(r.status), size, (r.getheader("Location") or "")
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, 0, ""
    finally:
        conn.close()


def cmd_split(root: Path, args) -> dict:
    """Local only. Chunk manifests (url\\tname) in list order, plus one
    globally sorted expected-name file that verify streams. The presigned set
    is chunked first so pull always drains the set with the clock first."""
    cfg = load_cfg(root, args.slug)
    common.ensure_subscription(dry_run=args.dry_run)
    paths = fetch_lists(root, cfg, refresh=args.refresh_lists,
                        dry_run=args.dry_run)
    wd = work_dir(root)
    cd = chunks_dir(root)
    import shutil
    shutil.rmtree(cd, ignore_errors=True)
    cd.mkdir(parents=True, exist_ok=True)
    queue: list[str] = []
    names: list[str] = []
    excluded: dict[str, int] = {}
    notes_path = wd / "name-notes.jsonl"
    if notes_path.exists():
        notes_path.unlink()
    counts = {}
    set_buckets: dict[str, set] = {}
    for s in ("presigned", "public"):   # presigned first: it is the one on a clock
        set_buckets[s] = set()

        def rows():
            for url in iter_urls(paths[s]):
                name, skip, note = blob_name(url, args.dest_prefix)
                if skip:
                    excluded[skip] = excluded.get(skip, 0) + 1
                    _append(notes_path, json.dumps(
                        {"set": s, "url": _redact(url), "excluded": skip}) + "\n")
                    continue
                if note:
                    _append(notes_path, json.dumps(
                        {"set": s, "url": _redact(url), "name": name,
                         "note": note}) + "\n")
                names.append(name)
                set_buckets[s].add(name.split("/")[1])
                yield url, name
        n = 0
        for idx, text in chunk_rows(rows(), args.chunk_rows):
            cname = f"{s}-{idx:05d}"
            (cd / cname).write_text(text)
            queue.append(cname)
            n += text.count("\n")
        counts[s] = n
    # UTF-8 preserves code-point order, so plain sorted() matches the byte
    # order List Blobs returns -- the one assumption the merge-join rests on.
    names.sort()
    (wd / "expected.txt").write_text("".join(nm + "\n" for nm in names))
    (wd / "queue.txt").write_text("".join(c + "\n" for c in queue))
    (wd / "sets.json").write_text(json.dumps(
        {k: sorted(v) for k, v in set_buckets.items()}, indent=2))
    for f in ("done.txt", "failed.jsonl", "missing.txt"):
        if (wd / f).exists():
            (wd / f).unlink()
    return {"ok": True, "command": "split", "slug": args.slug,
            "chunks": len(queue), "rows": counts,
            "total_rows": sum(counts.values()), "excluded": excluded,
            "buckets": {k: sorted(v) for k, v in set_buckets.items()},
            "chunk_rows": args.chunk_rows, "work_dir": str(wd)}


def _redact(url: str) -> str:
    """Never persist an S3 signature. The signed URLs already live in the
    client's container, but writing copies of a credential into our own state
    is not hygiene worth shipping (the slack ledger rule)."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, "<redacted>" if k in ("X-Amz-Signature", "X-Amz-Credential")
          else v) for k, v in q]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(q, safe="/"), parts.fragment))


def cmd_pull(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    common.ensure_subscription(dry_run=args.dry_run)
    wd = work_dir(root)
    cd = chunks_dir(root)
    if not (wd / "queue.txt").exists():
        raise common.HarnessError("no chunk manifests -- run `split` first")
    queue = _read_lines(wd / "queue.txt")
    done = set(_read_lines(wd / "done.txt"))
    if args.set != "both":
        queue = [c for c in queue if c.startswith(args.set + "-")]
    if args.mop_up:
        queue = [_build_mopup(root, cd, wd)]
        done = set()
    todo = [c for c in queue if c not in done]
    if not todo:
        return {"ok": True, "command": "pull", "slug": args.slug,
                "note": "every chunk already done", "chunks_done": len(done)}

    we_added, ip = phases.ip_rule_ensure(cfg, dry_run=args.dry_run)
    guard = Guard(cfg, dry_run=args.dry_run)
    totals = Counters()
    started = time.time()
    chunks_run = 0
    try:
        sas, sas_expiry = mint_write_sas(cfg, args.sas_days, args.dry_run)
        if args.dry_run:
            return {"ok": True, "command": "pull", "dry_run": True,
                    "chunks": todo[:5], "chunks_total": len(todo),
                    "sas_expiry": sas_expiry,
                    "note": "would PUT <blob>?<sas> per row with "
                            "x-ms-copy-source: <s3 url>, x-ms-blob-type: "
                            "BlockBlob, If-None-Match: *"}
        log(root, f"pull start: {len(todo)} chunks, concurrency "
                  f"{args.concurrency}, sas expires {sas_expiry}")
        for cname in todo:
            if guard.aborted:
                break
            c = Counters()
            run_chunk(cfg, sas, cd / cname, args.concurrency, c, guard)
            chunks_run += 1
            for k in ("completed", "skipped", "missing", "failed",
                      "bytes", "blocked", "throttles"):
                setattr(totals, k, getattr(totals, k) + getattr(c, k))
            for e, n in c.errors.items():
                totals.errors[e] = totals.errors.get(e, 0) + n
            if c.failed == 0 and not guard.aborted:
                _append(wd / "done.txt", cname + "\n")
            else:
                _append(wd / "failed.jsonl", json.dumps(
                    {"chunk": cname, **c.as_dict()}) + "\n")
            log(root, f"chunk {cname}: {json.dumps(c.as_dict())}")
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, dry_run=args.dry_run)

    out = {"ok": not guard.aborted, "command": "pull", "slug": args.slug,
           "set": args.set, "chunks_run": chunks_run,
           "chunks_remaining": len(todo) - chunks_run,
           "elapsed_seconds": int(time.time() - started),
           **totals.as_dict(),
           "bytes_note": "run bytes are not counted here (no census, and a "
                         "single-shot server-side copy reports no size) -- "
                         "`verify` reads the real total off the container"}
    if guard.aborted:
        out["cause"] = guard.abort_cause
    log(root, "pull end: " + json.dumps(out))
    return out


def _build_mopup(root: Path, cd: Path, wd: Path) -> str:
    """missing.txt (names, from verify) -> one fresh chunk, by streaming the
    existing chunks for those names. O(missing) memory; a plain re-run would
    also work (create-only) but costs one 409 per landed blob."""
    want = set(_read_lines(wd / "missing.txt"))
    if not want:
        raise common.HarnessError("no missing.txt -- run `verify` first")
    rows = []
    for p in sorted(cd.glob("*")):
        if p.name.startswith("mopup"):
            continue
        with p.open() as f:
            for ln in f:
                url, _, name = ln.rstrip("\n").partition("\t")
                if name in want:
                    rows.append(ln)
    cname = "mopup-00000"
    (cd / cname).write_text("".join(rows))
    return cname


# ── verify ──────────────────────────────────────────────────────────────────

def iter_container(cfg: dict, sas: str, prefix: str):
    """Complete List Blobs walk of one prefix, yielding (name, size) in the
    service's lexical order."""
    marker = ""
    while True:
        q = ("restype=container&comp=list&maxresults=5000&prefix="
             + urllib.parse.quote(prefix))
        if marker:
            q += "&marker=" + urllib.parse.quote(marker)
        data = azure_get(f"{container_url(cfg)}?{q}&{sas}")
        root = ET.fromstring(data)
        for blob in root.iter("Blob"):
            name = blob.findtext("Name") or ""
            size = int(blob.find("Properties").findtext("Content-Length") or 0)
            yield name, size
        marker = root.findtext("NextMarker") or ""
        if not marker:
            break


def merge_names(expected, actual, missing_out, cap: int = 200):
    """Stream two lexically sorted iterators. `expected` yields names,
    `actual` yields (name, size). Returns totals + a capped sample of each
    mismatch class; every MISSING name goes to missing_out uncapped so a
    mop-up copies exactly the shortfall."""
    stats = {"expected": 0, "present": 0, "missing": 0, "extra": 0,
             "bytes": 0}
    miss_sample, extra_sample = [], []
    e = next(expected, None)
    a = next(actual, None)
    while e is not None or a is not None:
        if a is None or (e is not None and e < a[0]):
            stats["expected"] += 1
            stats["missing"] += 1
            missing_out.write(e + "\n")
            if len(miss_sample) < cap:
                miss_sample.append(e)
            e = next(expected, None)
        elif e is None or a[0] < e:
            stats["extra"] += 1
            stats["bytes"] += a[1]
            if len(extra_sample) < cap:
                extra_sample.append(a[0])
            a = next(actual, None)
        else:
            stats["expected"] += 1
            stats["present"] += 1
            stats["bytes"] += a[1]
            e = next(expected, None)
            a = next(actual, None)
    return stats, miss_sample, extra_sample


def blob_size(cfg: dict, sas: str, name: str) -> int:
    """HEAD one blob -> its committed length."""
    req = urllib.request.Request(f"{blob_url(cfg, name)}?{sas}", method="HEAD",
                                 headers={"x-ms-version": X_MS_VERSION})
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers.get("Content-Length") or 0)


def sample_rows(cd: Path, n: int, buckets: set, seed: int = 23):
    """Reservoir-sample n (url, name) rows from the chunk manifests. Streaming
    and O(n) memory: the manifests are 1.7M rows."""
    import random
    rng = random.Random(seed)
    keep: list = []
    seen = 0
    for p in sorted(cd.glob("*")):
        with p.open() as f:
            for ln in f:
                url, _, name = ln.rstrip("\n").partition("\t")
                if not name or name.split("/")[1] not in buckets:
                    continue
                seen += 1
                if len(keep) < n:
                    keep.append((url, name))
                else:
                    j = rng.randrange(seen)
                    if j < n:
                        keep[j] = (url, name)
    return keep


def sample_bytes_check(cfg: dict, sas: str, rows, workers: int = 24) -> dict:
    """Source size vs committed blob size for a sample.

    The only byte-level assurance a no-census run can offer, and it is worth
    stating plainly: presence proves we wrote a blob for every url, this
    proves the blobs we spot-checked hold the whole object. Server-side copy
    either completes or fails, so a size split here would mean something is
    badly wrong -- which is exactly why it is cheap and worth running."""
    out = {"sampled": len(rows), "matched": 0, "mismatched": 0,
           "unreadable": 0, "bytes": 0, "mismatch_sample": []}
    lock = threading.Lock()

    def one(row):
        url, name = row
        try:
            src = source_size(url, is_presigned(url))
            dst = blob_size(cfg, sas, name)
        except Exception as exc:  # noqa: BLE001 -- a probe failure is not a copy failure
            with lock:
                out["unreadable"] += 1
                if len(out["mismatch_sample"]) < 10:
                    out["mismatch_sample"].append(
                        {"name": name, "error": type(exc).__name__})
            return
        with lock:
            if src == dst:
                out["matched"] += 1
                out["bytes"] += dst
            else:
                out["mismatched"] += 1
                if len(out["mismatch_sample"]) < 10:
                    out["mismatch_sample"].append(
                        {"name": name, "source": src, "blob": dst})

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, rows))
    return out


def cmd_verify(root: Path, args) -> dict:
    """Per-bucket presence check. Read-only: rl SAS, no writes.

    Asserts PRESENCE, not source bytes. The client's lists declare no sizes,
    so no source-size claim is made -- this certifies that every URL they gave
    us has a committed blob, and reports the bytes the container actually
    holds under those prefixes."""
    cfg = load_cfg(root, args.slug)
    common.ensure_subscription(dry_run=args.dry_run)
    wd = work_dir(root)
    exp_path = wd / "expected.txt"
    if not exp_path.exists():
        raise common.HarnessError("no expected.txt -- run `split` first")
    if not (wd / "sets.json").exists():
        raise common.HarnessError("no sets.json -- re-run `split`")
    by_set = json.loads((wd / "sets.json").read_text())
    wanted = (set().union(*(set(v) for v in by_set.values()))
              if args.set == "both" else set(by_set.get(args.set, [])))
    buckets = sorted(wanted)
    if not buckets:
        raise common.HarnessError(f"no buckets recorded for set {args.set!r}")

    def expected_iter():
        with exp_path.open() as f:
            for ln in f:
                ln = ln.rstrip("\n")
                if ln and ln.split("/")[1] in wanted:
                    yield ln

    def actual_iter(sas):
        # Buckets in sorted order, each a distinct "<prefix>/<bucket>/" range,
        # so concatenating their listings is globally sorted (a bucket name can
        # contain no '/', so no range can straddle another).
        for b in buckets:
            for name, size in iter_container(
                    cfg, sas, f"{args.dest_prefix}/{b}/"):
                yield name, size

    we_added, ip = phases.ip_rule_ensure(cfg, dry_run=args.dry_run)
    try:
        sas = phases.mint_sas(cfg, dry_run=args.dry_run, days=1)
        if args.dry_run:
            return {"ok": True, "command": "verify", "dry_run": True,
                    "buckets": buckets}
        tmp = wd / "missing.txt.tmp"
        with tmp.open("w") as mf:
            stats, miss, extra = merge_names(expected_iter(),
                                             actual_iter(sas), mf)
        tmp.replace(wd / "missing.txt")
        sampled = (sample_bytes_check(
            cfg, sas, sample_rows(chunks_dir(root), args.sample, wanted))
            if args.sample else None)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, dry_run=args.dry_run)

    ok = stats["missing"] == 0 and (not sampled or sampled["mismatched"] == 0)
    return {"ok": ok, "command": "verify", "slug": args.slug,
            "buckets": buckets, **stats, "sampled_bytes_check": sampled,
            "bytes_human": common.human_bytes(stats["bytes"]),
            "missing_sample": miss[:20], "extra_sample": extra[:20],
            "missing_file": str(wd / "missing.txt") if stats["missing"] else None,
            "claim": "presence of every listed URL under "
                     f"{args.dest_prefix}/<bucket>/, plus a SAMPLED "
                     "source-vs-blob size check; the container's own bytes are "
                     "the total (the lists declare no sizes, so no whole-corpus "
                     "source-size claim is made)"}


def cmd_status(root: Path, args) -> dict:
    guard_slug(args.slug)
    wd = work_dir(root)
    queue = _read_lines(wd / "queue.txt")
    done = _read_lines(wd / "done.txt")
    failed = [json.loads(ln) for ln in _read_lines(wd / "failed.jsonl")]
    tail = ""
    if (wd / "pull.log").exists():
        lines = (wd / "pull.log").read_text().splitlines()
        tail = lines[-1] if lines else ""
    agg = {"completed": 0, "skipped": 0, "missing": 0, "failed": 0,
           "block_staged": 0, "block_staged_bytes": 0}
    for ln in _read_lines(wd / "pull.log"):
        m = re.search(r"chunk \S+: (\{.*\})$", ln)
        if m:
            d = json.loads(m.group(1))
            for k in agg:
                agg[k] += d.get(k, 0)
    return {"ok": True, "command": "status", "slug": args.slug,
            "chunks_total": len(queue), "chunks_done": len(done),
            "chunks_failed": len(failed),
            "pct": round(100 * len(done) / len(queue), 1) if queue else 0.0,
            "totals_from_log": agg,
            "objects_done": agg["completed"] + agg["skipped"],
            "last_log_line": tail}


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",
                   choices=["plan", "probe", "split", "pull", "verify", "status"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--dest-prefix", default=DEFAULT_DEST_PREFIX,
                   help=f"prefix inside the container (default "
                        f"{DEFAULT_DEST_PREFIX} -- the client's own folder)")
    p.add_argument("--set", choices=["presigned", "public", "both"],
                   default="both", help="which list to act on")
    p.add_argument("--concurrency", type=int, default=256,
                   help="parallel server-side copies (default 256)")
    p.add_argument("--chunk-rows", type=int, default=CHUNK_ROWS,
                   help=f"rows per chunk manifest (default {CHUNK_ROWS})")
    p.add_argument("--sas-days", type=int, default=2,
                   help="write-SAS expiry (default 2; a resume re-mints)")
    p.add_argument("--refresh-lists", action="store_true",
                   help="re-download the two url lists from the container")
    p.add_argument("--sample", type=int, default=300,
                   help="verify: spot-check this many objects source-vs-blob "
                        "byte size (default 300; 0 disables)")
    p.add_argument("--mop-up", action="store_true",
                   help="pull only what verify recorded in missing.txt")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    root = Path(args.root)
    fn = {"plan": cmd_plan, "probe": cmd_probe, "split": cmd_split,
          "pull": cmd_pull, "verify": cmd_verify, "status": cmd_status}[args.command]
    try:
        result = fn(root, args)
    except common.HarnessError as e:
        print(json.dumps({"ok": False, "command": args.command,
                          "error": str(e)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
