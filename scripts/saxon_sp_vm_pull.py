#!/usr/bin/env python3
"""VM-side SharePoint COMPLETION puller for saxon — a ONE-OFF, not an engine
family. Finishes the client's own partial `sharepoint/` push by copying only
the files Graph says a site has but the container doesn't; it never starts a
site the client didn't (the mapping.json pushed from the laptop is the
frozen scope) and never overwrites a client byte (create-only writes,
If-None-Match: * — API-enforced, the vimeo/zoom transport, NOT azcopy's
client-side --overwrite=false).

Runs on xfer-sp-saxon inside tmux, launched by scripts/saxon_sp_complete.py
with ~/.config/xfer/{sp.env,dest-sp.env} sourced:
  SP_TENANT_ID / SP_CLIENT_ID / SP_CLIENT_SECRET
                      — the client's read-only Entra app (app-only
                        client-credentials; Sites.Read.All + Files.Read.All)
  DEST_URL            — https://ACCT.blob.core.windows.net/<container> (BARE —
                        this script appends DEST_PREFIX itself, the teams
                        convention)
  DEST_SAS            — racwl container SAS
  DEST_PREFIX         — default "sharepoint" (the client's own prefix — the
                        whole point of this tool; also why every write is
                        create-only)
  RPS_GRAPH           — shared proactive Graph pace (default 8/s — the 08-27
                        census sustained ~6/s with zero 429s)
  WORKERS             — site-level worker threads (default 4)
  DIFF_ONLY           — "1": walk + diff + calibration gate, ZERO copies
  ONLY_SITES          — comma-separated dest folder names (pilot)
  LIMIT_SITES         — first N sites of the plan (pilot)
  REFRESH_SITES       — "1": re-walk sites state.json already marks ok
  ALLOW_NO_CALIBRATION— "1": copy even with no calibration rows (see gate)

The dest-path convention is the client tool's, matched EXACTLY and never
extended:  <DEST_PREFIX>/<SiteFolder>/<drive.name>/<folder path>/<name>
(the folder path is parentReference.path after "root:"). Two eras exist in
the container: the old July/Aug push wrote one `.meta.json` sidecar per file,
the new 08-29 push wrote none — sidecars are counted separately in the diff
and NEVER written by this tool.

THE CALIBRATION GATE (why no byte moves before it passes): the 08-27 census
walked sites with $select=id,size,file only, so per-file PATHS were never
validated against the container — only aggregates were. If this script's
expected_relpath() disagreed with the client tool's layout, every file would
read as "missing" and a full run would DUPLICATE the corpus into the client's
own prefix. So mapping.json marks a few sites believed complete
("calibrate": true, chosen by the laptop plan step); they are walked FIRST,
sequentially, and each must diff to <=1% missing by file count. Any breach
stops the run (exit 1) before a single copy, dumping sample missing paths
next to their nearest dest neighbors for a human to compare conventions.
Zero calibration rows also refuses to copy unless ALLOW_NO_CALIBRATION=1.

Comparison semantics (the honest ones):
  skip     = dest blob exists at the exact expected path, size == Graph size
  missing  = path absent -> copied (the only thing ever copied)
  mismatch = path present, size differs -> recorded, NEVER touched (create-
             only couldn't overwrite it anyway, and shouldn't: it's the
             client's byte; a user conversation at report time)
  dest-only= dest paths Graph doesn't know (deleted-since-push files) ->
             counted, never deleted
  gone-at-source = walked item 404s at copy time -> recorded skip

Bookkeeping NEVER lands in the container: no markers, no manifests, no
progress blobs — reconciliation sums everything under sharepoint/ and it is
the client's corpus. All state lives under ~/xfer-sp/ (state.json,
results.jsonl, manifests/<Folder>.tsv.gz, run-summary.json) and the laptop
`harvest` subcommand pulls it home; the dest itself is the resume
(deterministic names + fresh diff + 409-is-benign), so a dead VM costs a
re-walk, never re-copied bytes.

Transport: Azure server-side copy from @microsoft.graph.downloadUrl (pre-
authenticated tempauth URL, no Bearer header — the zoho structural blocker
does not apply). <=1 GiB: one Put Blob From URL. Larger: 256 MiB Put Block
From URL loop + Put Block List (If-None-Match rides the commit). downloadUrls
expire within ~1 h, so they are fetched per file at copy time, redirects
resolved here first (Azure never follows them; hosts pinned to SharePoint's),
and a source-side CopyError re-resolves via a fresh item GET
(RERESOLVE_BUDGET per file, the vimeo rule). A file whose source refuses
server-side copy streams through the VM to disk instead and is PUT with the
same If-None-Match: * (why the OS disk has headroom beyond the image
default).

Secrets never touch argv, logs, or laptop files; the token lives in
TokenBox memory only. Stdlib-only, zero repo imports, import-safe (the pure
functions — classify, expected_relpath, diff_folder, block_plan,
plan_sites — are unit-tested offline). Pushed to the VM alone.

Exit codes: 0 = every planned site ok, 1 = fatal setup/calibration failure,
2 = finished but one or more sites failed or had copy errors.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
TOKEN_PATH_FMT = "{login}/{tenant}/oauth2/v2.0/token"
TOKEN_REFRESH_MARGIN = 120
API_RETRIES = 4
MAX_SLEEPS_PER_CALL = 50
DEFAULT_RPS_GRAPH = 8.0
DEFAULT_WORKERS = 4
DEFAULT_DEST_PREFIX = "sharepoint"
DELTA_PAGE = 999

X_MS_VERSION = "2021-08-06"   # >= 2020-04-08 (Put Blob From URL)
MIB = 1024 * 1024
BLOCK_SIZE = 256 * MIB
SINGLE_SHOT_MAX = 1024 * MIB  # vimeo's threshold; above it, block loop
MAX_BLOCKS = 50_000
RERESOLVE_BUDGET = 20         # per file; tempauth URLs expire ~hourly
CALIBRATION_MAX_MISSING = 0.01  # >1% missing on a believed-complete site
                                # = convention bug, stop everything
GATE_SAMPLE = 20

# downloadUrl redirect chains must stay on SharePoint infrastructure — a
# resolved URL is handed to Azure as x-ms-copy-source, so an off-host hop
# would exfiltrate nothing (no Bearer rides it) but copy the wrong bytes.
SOURCE_HOST_SUFFIXES = (".sharepoint.com", ".sharepointonline.com", ".svc.ms")

_log_lock = threading.Lock()


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _log_lock:
        print(f"[{ts}] {msg}", flush=True)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return f"{n} B"


def atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── pure: paths, planning, diff, classification ──────────────────────────────

def sitecoll(url: str) -> str:
    """PURE. Site-collection key of a web URL — ported verbatim from the
    08-27 census's walk_files.py so folder->collection grouping cannot
    drift from the numbers already reported."""
    m = re.match(r"https://[^/]+(/(sites|teams)/[^/]+)?", url or "")
    return (m.group(0) if m else url).lower()


def expected_relpath(drive_name: str, parent_path: str,
                     name: str) -> str | None:
    """PURE. The client tool's dest path for one driveItem, relative to the
    site folder: <drive.name>/<path after root:>/<name>. None = the
    parentReference.path carries no 'root:' anchor (never seen on a file
    item; recorded as a walk error by the caller, never guessed)."""
    if not name or "root:" not in (parent_path or ""):
        return None
    rel = parent_path.split("root:", 1)[1].strip("/")
    parts = [p for p in (drive_name or "").split("/") if p]  # defensive
    base = "/".join(parts) if parts else "_no-library-name"
    return f"{base}/{rel}/{name}" if rel else f"{base}/{name}"


def plan_sites(mapping: dict, only_sites: set[str] | None,
               limit_sites: int) -> list[dict]:
    """PURE. The ordered work list from an approved mapping: action ==
    "complete" rows only — calibration rows first (they gate the run),
    then rows flagged "priority" (the 76 originally-transferred
    collections, user request 2026-08-29), then biggest expected data
    first (the census walker's ordering, so the long pole starts
    early)."""
    rows = [r for r in mapping.get("folders", [])
            if r.get("action") == "complete"]
    if only_sites:
        rows = [r for r in rows if r["folder"] in only_sites]
    rows.sort(key=lambda r: (not r.get("calibrate"),
                             not r.get("priority"),
                             -(r.get("order_bytes") or 0)))
    if limit_sites:
        # keep every calibration row — the gate must run even in a pilot
        cal = [r for r in rows if r.get("calibrate")]
        rest = [r for r in rows if not r.get("calibrate")]
        rows = cal + rest[:max(0, limit_sites - len(cal))]
    return rows


def diff_folder(expected: dict[str, tuple], dest: dict[str, int]) -> dict:
    """PURE. Per-file name+size diff of one site folder.

    expected: relpath -> (size, drive_id, item_id) from the Graph walk.
    dest:     relpath -> size from the container listing.

    Sidecars (the old push era's `X.meta.json`, one per file) are counted
    apart from dest_only so a sidecar-era site doesn't read as thousands of
    mystery extras; they are never candidates for anything."""
    missing, mismatched = [], []
    matched = matched_bytes = 0
    for rel, meta in expected.items():
        size = meta[0]
        have = dest.get(rel)
        if have is None:
            missing.append(rel)
        elif have != size:
            mismatched.append({"path": rel, "dest_size": have,
                               "src_size": size})
        else:
            matched += 1
            matched_bytes += size
    sidecars = dest_only = dest_only_bytes = 0
    for rel, size in dest.items():
        if rel in expected:
            continue
        if rel.endswith(".meta.json"):
            sidecars += 1
        else:
            dest_only += 1
            dest_only_bytes += size
    return {"missing": missing, "mismatched": mismatched,
            "matched": matched, "matched_bytes": matched_bytes,
            "dest_only": dest_only, "dest_only_bytes": dest_only_bytes,
            "sidecars": sidecars}


def mapping_suspect(diff: dict, expected_files: int) -> bool:
    """PURE. True when a folder's existing content barely overlaps the
    walked site's paths — the wrong-twin guard. Saxon's tenant is full of
    doppelganger sites (two 'Power BI Team's, two 'HR's, …), so a folder
    mis-mapped to its twin would diff as almost-all-missing and a copy
    would pull a site the client never pushed. A folder with >=50 real
    files whose path overlap with the walk is under 2% (and under 5 files)
    is suspect: no copy, human review. Sliver folders stay below the
    threshold — nothing there to infer from."""
    dest_real = (diff["matched"] + len(diff["mismatched"])
                 + diff["dest_only"])
    overlap = diff["matched"] + len(diff["mismatched"])
    return (expected_files > 0 and dest_real >= 50
            and overlap < max(5, 0.02 * dest_real))


def classify(status: int, family: str) -> str:
    """PURE. Status + endpoint family (figma's rule; Graph error bodies are
    secondary evidence). Families here — 'sites' (site/drive lookups),
    'delta' (per-drive walk pages), 'download' (per-item metadata GET at
    copy time) — share one classification table because NOTHING in this
    puller is run-fatal on a 4xx: a refused site is a recorded site
    failure, a 404'd item is gone-at-source, and only the token layer
    (TokenBox) can kill the run. The family still matters to the CALLER
    (which failure bucket the terminal status lands in), and having it in
    the signature keeps the classify tests honest about that mapping."""
    if status in (200, 201, 204):
        return "ok"
    if status == 429:
        return "sleep"
    if status == 401:
        return "remint"
    if status >= 500 or status == 408:
        return "retry"
    return "skip"    # terminal non-2xx: caller records it per family


def _block_id(i: int, id_bytes: int = 8) -> str:
    """Deterministic, uniform-length block id. `id_bytes` is the RAW
    width before base64 — normally 8, but Azure requires every block id
    on a blob to share one length, and saxon's container carries
    ORPHANED uncommitted blocks from the client's own abandoned uploads
    (WebsitesBackup's 12 GB zips: 476 stale 8 MiB blocks under 48-char
    ids, found live 2026-08-30). Staging a 12-char id against those
    returns 400 InvalidBlobOrBlock — so the caller probes the existing
    width and passes it here. Committing our own Latest list discards the
    orphans."""
    return base64.b64encode(b"%0*d" % (id_bytes, i)).decode("ascii")


def block_plan(size: int, block_size: int,
               id_bytes: int = 8) -> list[tuple[str, int, int]]:
    """PURE. [(block_id, start, end_inclusive)] — vimeo's plan, widened
    by id_bytes so a blob carrying foreign uncommitted blocks can still
    be written."""
    n_blocks = (size + block_size - 1) // block_size
    if n_blocks > MAX_BLOCKS:
        raise CopyError(f"file needs {n_blocks} blocks (> {MAX_BLOCKS})")
    return [(_block_id(i, id_bytes), start, min(start + block_size, size) - 1)
            for i, start in enumerate(range(0, size, block_size))]


def existing_block_id_bytes(base_url: str, sas: str, name: str) -> int:
    """Raw id width to use for this blob: the width of any UNCOMMITTED
    blocks already staged on it (another tool's abandoned upload), else
    the default 8. Azure enforces one id length per blob, so this is the
    only way to write a blob someone else left half-staged. Any error
    falls back to the default — a probe must never be the thing that
    fails a copy."""
    url = _blob_url(base_url, name) + "?comp=blocklist&blocklisttype=uncommitted&" + sas
    try:
        req = urllib.request.Request(
            url, headers={"x-ms-version": X_MS_VERSION})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except (urllib.error.HTTPError, urllib.error.URLError,
            TimeoutError, OSError):
        return 8
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return 8
    widths = {len(n.text or "") for n in root.iter("Name")}
    widths.discard(0)
    if len(widths) != 1:
        return 8
    b64_len = widths.pop()
    if b64_len % 4:
        return 8
    raw = (b64_len // 4) * 3          # un-padded base64 -> raw bytes
    return raw if 1 <= raw <= 64 else 8


def source_host_ok(url: str) -> bool:
    """PURE. Redirect-chain host pin for resolved download URLs."""
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(host == s.lstrip(".") or host.endswith(s)
               for s in SOURCE_HOST_SUFFIXES)


# ── Graph client (teams_vm_pull idioms, made thread-safe) ────────────────────

class TokenBox:
    """App-only AAD client-credentials token, auto-refreshed, thread-safe
    (site workers share it — the census's lock, teams' error taxonomy)."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self._tenant = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._value = None
        self._exp = 0.0
        self._lock = threading.Lock()
        self.mints = 0

    def get(self) -> str:
        with self._lock:
            if self._value and time.time() < self._exp - TOKEN_REFRESH_MARGIN:
                return self._value
            return self._mint_locked()

    def invalidate(self) -> None:
        with self._lock:
            self._value = None

    def mint(self) -> str:
        with self._lock:
            return self._mint_locked()

    def _mint_locked(self) -> str:
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }).encode()
        url = TOKEN_PATH_FMT.format(login=LOGIN, tenant=self._tenant)
        last = None
        for attempt in range(1, API_RETRIES + 2):
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    body = json.loads(r.read().decode())
                tok = body.get("access_token")
                if not tok:
                    raise SystemExit(f"token mint returned no access_token:"
                                     f" {str(body)[:300]}")
                self._value = tok
                self._exp = time.time() + int(body.get("expires_in", 3599))
                self.mints += 1
                return tok
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", "replace")[:300]
                if e.code in (400, 401):
                    raise SystemExit(
                        f"token mint refused ({e.code}): {err} — check the "
                        "3 stdin values (tenant, client id, secret)")
                last = f"{e.code}: {err}"
            except (urllib.error.URLError, TimeoutError) as e:
                last = str(e)
            time.sleep(min(60, 5 * attempt))
        raise SystemExit(f"token mint failed after retries: {last}")


class PaceBucket:
    """ADAPTIVE proactive pacing (AIMD), thread-safe: every SharePoint-
    bound operation — Graph call, server-side copy fetch, streamed
    download — takes one slot from this shared bucket, so the aggregate
    external rate is bounded no matter how many threads run. The rate
    floats: any observed throttle (SP 429ing us directly, or 429ing
    AZURE's copy fetch) halves it; a clean minute creeps it back up by
    +0.5/s toward max_rps. Guessing a fixed number bounced off SharePoint's
    dynamic throttle live (2026-08-29: ~24/s effective earned a
    tenant-wide 429 storm ~2h in) — discovery beats guessing."""

    MIN_RPS = 2.0
    BUMP_PER_CLEAN_MINUTE = 0.5
    # One congestion EVENT may halve the rate once. Up to 32 operations
    # are in flight (workers x copy_threads), so a single SharePoint
    # throttle burst returns many 429s within a second or two; halving
    # per-response collapsed 12 rps to the 2.0 floor in 20 seconds
    # (observed live 2026-08-29) and then needed ~28 clean minutes to
    # climb back. Everything inside this window is the SAME signal.
    THROTTLE_COOLDOWN_S = 20.0

    def __init__(self, rps: float, max_rps: float | None = None):
        self._rate = max(self.MIN_RPS, rps) if rps > 0 else 0.0
        self._max = max(self._rate, max_rps or self._rate)
        self._next = 0.0
        self._lock = threading.Lock()
        self._last_throttle = 0.0
        self._last_bump = time.monotonic()
        self.throttles = 0

    @property
    def rate(self) -> float:
        return self._rate

    def wait(self) -> None:
        if not self._rate:
            return
        with self._lock:
            now = time.monotonic()
            if (now - self._last_bump >= 60
                    and now - self._last_throttle >= 60
                    and self._rate < self._max):
                self._rate = min(self._max,
                                 self._rate + self.BUMP_PER_CLEAN_MINUTE)
                self._last_bump = now
            slot = max(now, self._next)
            self._next = slot + 1.0 / self._rate
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def throttled(self) -> None:
        """Halve the rate — called on ANY observed 429, ours or Azure's.
        Coalesced: concurrent 429s from one burst are one signal, so the
        rate halves at most once per THROTTLE_COOLDOWN_S (the recovery
        clock still resets on every 429, so a sustained storm keeps the
        rate pinned rather than creeping up mid-storm)."""
        with self._lock:
            now = time.monotonic()
            self.throttles += 1
            recent = now - self._last_throttle < self.THROTTLE_COOLDOWN_S
            self._last_throttle = now
            self._last_bump = now
            if recent:
                return          # same congestion event — already halved
            old = self._rate
            self._rate = max(self.MIN_RPS, self._rate / 2.0)
            if old != self._rate:
                log(f"pace: throttle observed — rate {old:.1f} -> "
                    f"{self._rate:.1f} rps (recovers +"
                    f"{self.BUMP_PER_CLEAN_MINUTE}/clean minute)")


# Documented SharePoint-throttling courtesy: decorated traffic gets a
# gentler lane than anonymous traffic (NONISV|company|app/version form).
USER_AGENT = "NONISV|micro1|cdp-harness-sp-completion/1.0"


class GraphAPI:
    """teams_vm_pull's client, one shared ADAPTIVE bucket for every family
    (the limit that matters is the tenant-wide app throttle, not a
    per-surface tier), counters under a lock for the worker threads."""

    def __init__(self, box: TokenBox, rps: float = DEFAULT_RPS_GRAPH,
                 max_rps: float | None = None):
        self.box = box
        self._bucket = PaceBucket(rps, max_rps)
        self._lock = threading.Lock()
        self.calls = 0
        self.sleeps = 0

    def _count(self, what: str) -> None:
        with self._lock:
            setattr(self, what, getattr(self, what) + 1)

    def get_raw(self, url: str, family: str):
        """-> (status, body, content_type). Terminal non-2xx RETURNS (the
        caller records it — nothing here is run-fatal); 429 sleeps on
        Retry-After without charging API_RETRIES; 401 re-mints once."""
        attempt = 1
        sleeps_this_call = 0
        reminted = False
        status, body = 599, b""
        while True:
            self._bucket.wait()
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.box.get()}",
                              "User-Agent": USER_AGENT})
            self._count("calls")
            retry_after_hdr = None
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return (r.status, r.read(),
                            r.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
                retry_after_hdr = e.headers.get("Retry-After")
            except (urllib.error.URLError, TimeoutError):
                status, body = 599, b""
            action = classify(status, family)
            if action == "sleep":
                retry_after = 30
                try:
                    retry_after = int(retry_after_hdr)
                except (TypeError, ValueError):
                    pass
                self._bucket.throttled()
                self._count("sleeps")
                sleeps_this_call += 1
                if sleeps_this_call > MAX_SLEEPS_PER_CALL:
                    raise SystemExit(
                        f"429 sleep budget ({MAX_SLEEPS_PER_CALL}) exhausted "
                        f"on {family} {url.split('?')[0]} — sustained "
                        "throttling that never let up")
                time.sleep(min(300, max(1, retry_after)))
                continue
            elif action == "remint":
                self.box.invalidate()
                if reminted:
                    raise SystemExit("401 persists after re-mint — token or "
                                     "permission problem")
                reminted = True
            elif action == "retry":
                time.sleep(min(120, 10 * attempt))
            else:            # skip — terminal, caller records it
                return (status, body, "")
            attempt += 1
            if attempt > API_RETRIES + 1:
                return (status, body, "")

    def get(self, path_or_url: str, family: str, params: dict | None = None):
        url = (path_or_url if path_or_url.startswith("http")
               else GRAPH + path_or_url)
        if params:
            url += "?" + urllib.parse.urlencode(params, safe="$()/'! ,=")
        status, body, _ = self.get_raw(url, family)
        if status in (200, 201) and body:
            return status, json.loads(body.decode())
        return status, None


# ── Azure blob REST (vimeo transport, URL+SAS flavored) ──────────────────────

class CopyError(Exception):
    """Server-side copy failure — counted per file, never fatal.
    source_side means Azure could not fetch the source URL (usually: it
    expired) — the caller re-resolves and retries the same block."""

    def __init__(self, msg: str, azure_code: str = "",
                 source_status: str = ""):
        super().__init__(msg)
        self.azure_code = azure_code
        self.source_status = source_status

    @property
    def source_side(self) -> bool:
        return self.azure_code in ("CannotVerifyCopySource",
                                   "CopySourceNotFound") \
            or bool(self.source_status)

    @property
    def throttled(self) -> bool:
        """SharePoint 429ing AZURE's fetch (surfaces as
        CannotVerifyCopySource with 'Too Many Requests' in the body, no
        source-status header — seen live 2026-08-29). Backpressure, not
        failure: sleep and retry the SAME url, never re-resolve or fall
        back to streaming for it (the streamed GET hits the same
        throttle)."""
        return ("429" in (self.source_status or "")
                or "Too Many Requests" in str(self))


def _blob_url(base_url: str, name: str) -> str:
    return base_url.rstrip("/") + "/" + urllib.parse.quote(name, safe="/")


def _azure_err(e: urllib.error.HTTPError) -> tuple[str, str, str]:
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    m = re.search(r"<Code>([^<]+)</Code>", body)
    code = m.group(1) if m else ""
    src_status = (e.headers.get("x-ms-copy-source-status-code") or "").strip()
    return code, src_status, body[:200]


def azure_get(url: str) -> bytes:
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url,
                                     headers={"x-ms-version": X_MS_VERSION})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                # vnet-rule propagation — or saxon's provisioner stripped
                # the rule (it reverts manual SA rules in ~90s; the vnet
                # path normally survives, but if this persists ~2 min,
                # re-run allow-network from the laptop). Retry, never
                # re-mint.
                log("azure: 403 — propagation or a stripped network rule; "
                    "retrying (if persistent, re-run allow-network)")
                time.sleep(20 * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(1 + attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"blob GET failed after retries: {last}")


def build_dest_index(base_url: str, sas: str,
                     prefix: str) -> dict[str, dict[str, int]]:
    """One marker-paginated listing of <prefix>/ -> {folder: {relpath:
    size}}. ~1.4M blobs at 5000/page = ~300 requests, minutes in-region.
    Rebuilt every run — the dest is the resume, so fresh truth is the
    whole point."""
    index: dict[str, dict[str, int]] = {}
    marker = ""
    total = 0
    plen = len(prefix) + 1
    while True:
        url = (f"{base_url}?restype=container&comp=list&maxresults=5000"
               f"&prefix={urllib.parse.quote(prefix + '/', safe='')}")
        if marker:
            url += f"&marker={urllib.parse.quote(marker, safe='')}"
        url += "&" + sas
        root = ET.fromstring(azure_get(url))
        for blob in root.iter("Blob"):
            name = blob.findtext("Name") or ""
            props = blob.find("Properties")
            size = int((props.findtext("Content-Length") or 0)
                       if props is not None else 0)
            rest = name[plen:]
            folder, sep, rel = rest.partition("/")
            if not sep or not rel:
                continue      # a loose blob at the prefix root — not a site
            index.setdefault(folder, {})[rel] = size
            total += 1
            if total % 200_000 == 0:
                log(f"dest index: {total} blobs listed...")
        marker = root.findtext("NextMarker") or ""
        if not marker:
            log(f"dest index: {total} blobs across {len(index)} folders")
            return index


def resolve_download(url: str) -> tuple[str, int | None]:
    """Follow the downloadUrl's redirect chain manually (Azure copy-from-URL
    never follows redirects) -> (final URL, wire size from a 0-byte range
    probe). Every hop must stay on SharePoint infrastructure. The URL
    expires within ~1 h — never cache it across files."""
    cur = url
    for _hop in range(6):
        if not source_host_ok(cur):
            raise CopyError(f"resolved URL left SharePoint hosts: "
                            f"{urllib.parse.urlparse(cur).netloc}")
        req = urllib.request.Request(cur, headers={"Range": "bytes=0-0"})
        try:
            resp = _NR_OPENER.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise CopyError("redirect without Location")
                cur = urllib.parse.urljoin(cur, loc)
                continue
            if e.code == 429:
                time.sleep(5)
                continue
            raise CopyError(f"resolving downloadUrl: HTTP {e.code}",
                            source_status=str(e.code))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CopyError(f"resolving downloadUrl: {e}")
        total = None
        content_range = resp.headers.get("Content-Range") or ""
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                total = int(tail)
        elif getattr(resp, "status", None) == 200 \
                and resp.headers.get("Content-Length"):
            total = int(resp.headers["Content-Length"])
        resp.close()
        return cur, total
    raise CopyError("redirect loop resolving downloadUrl")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NR_OPENER = urllib.request.build_opener(_NoRedirect)


def azure_put_bytes(base_url: str, sas: str, name: str, body: bytes,
                    content_type: str) -> int:
    """PUT one small blob, create-only. 1 = created, 0 = already existed
    (409 is If-None-Match doing its job — the benign resume signal)."""
    url = _blob_url(base_url, name) + "?" + sas
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="PUT", headers={
            "x-ms-version": X_MS_VERSION,
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": content_type,
            "If-None-Match": "*",
        })
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                r.read()
                return 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 0
            last = e
            if e.code == 403:
                time.sleep(20 * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(1 + attempt)
                continue
            code, _s, excerpt = _azure_err(e)
            raise CopyError(f"PUT {name}: HTTP {e.code} {code} {excerpt}",
                            azure_code=code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(1 + attempt)
    raise CopyError(f"PUT {name} failed after retries: {last}")


def put_blob_from_url(base_url: str, sas: str, name: str, src_url: str,
                      content_type: str) -> int:
    """Single-request server-side copy. 1 = copied, 0 = already existed.
    Dest-side 403/5xx retried; source-side failures raise CopyError
    immediately so the caller can re-resolve the expired URL."""
    headers = {
        "x-ms-version": X_MS_VERSION,
        "x-ms-blob-type": "BlockBlob",
        "x-ms-copy-source": src_url,
        "x-ms-blob-content-type": content_type,
        # never inherit the source's Content-Disposition (vimeo lore: a
        # source filename header can be an invalid blob property)
        "x-ms-blob-content-disposition": "attachment",
        "If-None-Match": "*",
    }
    url = _blob_url(base_url, name) + "?" + sas
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=b"", method="PUT",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                r.read()
                return 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 0
            code, src_status, excerpt = _azure_err(e)
            err = CopyError(f"copy {name}: HTTP {e.code} {code} (source "
                            f"status {src_status or '-'}) {excerpt}",
                            azure_code=code, source_status=src_status)
            if err.source_side:
                raise err
            last = err
            if e.code == 403:
                time.sleep(20 * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(2 + 2 * attempt)
                continue
            raise err
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 + 2 * attempt)
    raise CopyError(f"copy {name} failed after retries: {last}")


def put_block_from_url(base_url: str, sas: str, name: str, block_id: str,
                       src_url: str, start: int, end: int) -> None:
    url = (_blob_url(base_url, name) + "?comp=block&blockid="
           + urllib.parse.quote(block_id) + "&" + sas)
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=b"", method="PUT", headers={
            "x-ms-version": X_MS_VERSION,
            "x-ms-copy-source": src_url,
            "x-ms-source-range": f"bytes={start}-{end}",
        })
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                r.read()
                return
        except urllib.error.HTTPError as e:
            code, src_status, excerpt = _azure_err(e)
            err = CopyError(f"block {block_id} of {name}: HTTP {e.code} "
                            f"{code} (source status {src_status or '-'}) "
                            f"{excerpt}",
                            azure_code=code, source_status=src_status)
            if err.source_side:
                raise err
            last = err
            if e.code == 403:
                time.sleep(20 * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(2 + 2 * attempt)
                continue
            raise err
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 + 2 * attempt)
    raise CopyError(f"block {block_id} of {name} failed after retries: {last}")


def put_block_list(base_url: str, sas: str, name: str, block_ids: list[str],
                   content_type: str) -> int:
    """Commit staged blocks — the blob comes into existence HERE, which is
    why If-None-Match rides this call (create-only invariant). 1 =
    committed, 0 = already existed."""
    body = ("<?xml version='1.0' encoding='utf-8'?><BlockList>"
            + "".join(f"<Latest>{bid}</Latest>" for bid in block_ids)
            + "</BlockList>").encode("utf-8")
    url = _blob_url(base_url, name) + "?comp=blocklist&" + sas
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="PUT", headers={
            "x-ms-version": X_MS_VERSION,
            "Content-Type": "application/xml",
            "x-ms-blob-content-type": content_type,
            "If-None-Match": "*",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
                return 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 0
            code, _s, excerpt = _azure_err(e)
            last = CopyError(f"commit {name}: HTTP {e.code} {code} {excerpt}",
                             azure_code=code)
            if e.code == 403:
                time.sleep(20 * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(2 + 2 * attempt)
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 + 2 * attempt)
    raise CopyError(f"commit {name} failed after retries: {last}")


# ── the walk ─────────────────────────────────────────────────────────────────

def walk_collection(api: GraphAPI, site_ids: list[str]) -> dict:
    """Delta-walk every drive of every web in the collection -> expected
    file map. A drive whose walk is refused mid-page fails the whole
    site's walk (never a silently truncated manifest); a web whose drive
    LISTING is refused is recorded the same way."""
    expected: dict[str, tuple] = {}
    collided: set[str] = set()
    drives_walked = 0
    errors: list[str] = []
    for sid in site_ids:
        status, body = api.get(f"/sites/{sid}/drives", "sites",
                               {"$top": "50"})
        if status not in (200, 201) or body is None:
            errors.append(f"drives-listing:{sid}:status-{status}")
            continue
        drives = list(body.get("value", []))
        nxt = body.get("@odata.nextLink")
        while nxt:
            status, body2, _ = api.get_raw(nxt, "sites")
            if status != 200 or not body2:
                errors.append(f"drives-paging:{sid}:status-{status}")
                break
            page = json.loads(body2.decode())
            drives += page.get("value", [])
            nxt = page.get("@odata.nextLink")
        for drive in drives:
            did = drive.get("id")
            dname = drive.get("name") or ""
            url = (f"{GRAPH}/drives/{did}/root/delta"
                   f"?$select=id,name,size,file,folder,parentReference,deleted"
                   f"&$top={DELTA_PAGE}")
            drive_failed = False
            while url:
                status, raw, _ = api.get_raw(url, "delta")
                if status != 200 or not raw:
                    errors.append(f"delta:{did}({dname}):status-{status}")
                    drive_failed = True
                    break
                page = json.loads(raw.decode())
                for it in page.get("value", []):
                    if "file" not in it or "deleted" in it:
                        continue
                    rel = expected_relpath(
                        dname, (it.get("parentReference") or {}).get("path")
                        or "", it.get("name") or "")
                    if rel is None:
                        errors.append(f"no-root-anchor:{did}:"
                                      f"{(it.get('name') or '')[:60]}")
                        continue
                    size = int(it.get("size") or 0)
                    if rel in collided:
                        continue
                    prev = expected.get(rel)
                    if prev is not None and prev[0] != size:
                        # two drives map the same relpath with different
                        # bytes — never guess which one the client wrote
                        collided.add(rel)
                        expected.pop(rel, None)
                        continue
                    mime = ((it.get("file") or {}).get("mimeType")
                            or "application/octet-stream")
                    expected[rel] = (size, did, it.get("id"), mime)
                url = page.get("@odata.nextLink")
            if not drive_failed:
                drives_walked += 1
    return {"expected": expected, "collisions": sorted(collided),
            "drives_walked": drives_walked, "errors": errors}


def write_manifest(manifests_dir: Path, folder: str, site_url: str,
                   expected: dict[str, tuple]) -> Path:
    """manifests/<Folder>.tsv.gz — verify's authority on the laptop.
    Written atomically on walk completion; a crashed walk leaves no
    half-manifest."""
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifests_dir / f"{folder}.tsv.gz"
    tmp = path.with_suffix(".gz.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write(f"#site\t{folder}\t{site_url}\n")
        for rel in sorted(expected):
            size, drive_id, item_id, _mime = expected[rel]
            fh.write(f"{rel}\t{size}\t{drive_id}\t{item_id}\n")
    os.replace(tmp, path)
    return path


# ── the copy ─────────────────────────────────────────────────────────────────

def copy_file(api: GraphAPI, dest_base: str, sas: str, blob_name: str,
              drive_id: str, item_id: str, size: int,
              mime: str) -> tuple[str, int]:
    """Copy one missing file -> ('copied'|'already'|'gone', bytes).
    Fresh downloadUrl per file (they expire ~hourly), redirects resolved
    here, server-side copy first, re-resolve on source-side failure —
    and ANY server-side CopyError falls back to streaming the bytes
    through the VM (found live on WebsitesBackup: SharePoint refuses
    Azure's fetch for some files with a bare InvalidBlobOrBlock and no
    source status, while a direct GET of the same URL serves a clean
    206). Both paths stay If-None-Match: * create-only."""
    status, item = api.get(f"/drives/{drive_id}/items/{item_id}", "download")
    if status == 404:
        return "gone", 0
    if status not in (200, 201) or item is None:
        raise CopyError(f"item GET status {status}")
    dl = item.get("@microsoft.graph.downloadUrl")
    if not dl:
        raise CopyError("item has no downloadUrl (plan/permission quirk)")
    if size == 0:
        return ("copied" if azure_put_bytes(dest_base, sas, blob_name, b"",
                                            mime) else "already"), 0

    resolves = 0

    def fresh_url() -> str:
        """The RAW downloadUrl, re-fetched from Graph on demand. NO
        redirect pre-resolve: probe proved saxon's downloadUrls serve
        bytes directly (206, no redirect chain), so the per-file resolve
        round-trip was pure SharePoint load — a 3xx from Azure's fetch
        falls back to resolve_download below, per file, only when it
        actually happens."""
        nonlocal resolves, dl
        if resolves >= RERESOLVE_BUDGET:
            raise CopyError(f"re-resolve budget ({RERESOLVE_BUDGET}) spent")
        resolves += 1
        if resolves > 1:
            s2, it2 = api.get(f"/drives/{drive_id}/items/{item_id}",
                              "download")
            if s2 not in (200, 201) or not it2 \
                    or not it2.get("@microsoft.graph.downloadUrl"):
                raise CopyError(f"re-resolve item GET status {s2}")
            dl = it2["@microsoft.graph.downloadUrl"]
        if not source_host_ok(dl):
            raise CopyError(f"downloadUrl off SharePoint hosts: "
                            f"{urllib.parse.urlparse(dl).netloc}")
        return dl

    # every SharePoint-bound call below (resolve inside fresh_url, the
    # copy-from-URL control calls whose real work is an SP fetch) waits on
    # the SHARED bucket — the copy layer must not outrun the pace the
    # walk honors, or SharePoint's dynamic throttle 429s Azure's fetches
    # (seen live ~2h into the first copy pass)
    bucket = api._bucket

    def _paced_fresh_url() -> str:
        bucket.wait()
        return fresh_url()

    def _throttle_wait(e: CopyError, sleeps: int) -> int:
        if sleeps >= 12:
            raise CopyError(f"source throttling persisted: {e}")
        bucket.throttled()   # feed AIMD — the whole run slows, not just us
        delay = min(180, 30 * (sleeps + 1))
        log(f"copy: source 429 on {blob_name.rsplit('/', 1)[-1][:40]} — "
            f"sleeping {delay}s (throttle, not failure)")
        time.sleep(delay)
        return sleeps + 1

    def _chase_redirect(url: str) -> str:
        """Azure reported a 3xx from the source — resolve the chain once
        (paced) and hand back the final URL."""
        bucket.wait()
        return resolve_download(url)[0]

    try:
        if size <= SINGLE_SHOT_MAX:
            src = _paced_fresh_url()
            tries = sleeps = 0
            last: CopyError | None = None
            while True:
                try:
                    bucket.wait()
                    got = put_blob_from_url(dest_base, sas, blob_name,
                                            src, mime)
                    return ("copied" if got else "already"), size
                except CopyError as e:
                    last = e
                    if e.throttled:
                        sleeps = _throttle_wait(e, sleeps)
                        continue          # same URL — it hasn't expired
                    if not e.source_side:
                        raise
                    tries += 1
                    if tries >= 3:
                        raise CopyError(f"single-shot source-side failures "
                                        f"persisted (last: {last})")
                    if (e.source_status or "").startswith("3"):
                        src = _chase_redirect(src)
                    else:
                        src = _paced_fresh_url()
        else:
            src = _paced_fresh_url()
            block_ids = []
            id_bytes = existing_block_id_bytes(dest_base, sas, blob_name)
            if id_bytes != 8:
                log(f"copy: {blob_name.rsplit('/', 1)[-1][:40]} carries "
                    f"foreign uncommitted blocks — matching their id width "
                    f"({id_bytes} raw bytes)")
            for bid, start, end in block_plan(size, BLOCK_SIZE, id_bytes):
                tries = sleeps = 0
                while True:
                    try:
                        bucket.wait()
                        put_block_from_url(dest_base, sas, blob_name, bid,
                                           src, start, end)
                        break
                    except CopyError as e:
                        if e.throttled:
                            sleeps = _throttle_wait(e, sleeps)
                            continue
                        if not e.source_side:
                            raise
                        tries += 1
                        if tries >= 3:
                            raise CopyError("block source-side failures "
                                            f"persisted (last: {e})")
                        if (e.source_status or "").startswith("3"):
                            src = _chase_redirect(src)
                        else:
                            src = _paced_fresh_url()   # expired mid-copy
                block_ids.append(bid)
            got = put_block_list(dest_base, sas, blob_name, block_ids, mime)
            return ("copied" if got else "already"), size
    except CopyError as e:
        # SharePoint refusing Azure's fetch, an expired-URL storm, or a
        # bare InvalidBlobOrBlock — every server-side failure gets ONE
        # streamed attempt (a dest-side outage will just fail again there
        # and be recorded; a re-run retries).
        log(f"copy: server-side copy failed for {blob_name} "
            f"({human_bytes(size)}) — streaming through the VM: {e}")
        return stream_fallback(api, dest_base, sas, blob_name, drive_id,
                               item_id, size, mime)


def _read_exact(resp, want: int) -> bytes:
    """Read up to `want` bytes, looping over short reads (an HTTP body
    read may return less than requested before EOF)."""
    parts = []
    got = 0
    while got < want:
        chunk = resp.read(want - got)
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def stream_fallback(api: GraphAPI, dest_base: str, sas: str, blob_name: str,
                    drive_id: str, item_id: str, size: int,
                    mime: str) -> tuple[str, int]:
    """Stream the bytes Graph -> VM -> blob with NO disk staging: 64 MiB
    memory blocks, plain Put Block per chunk, Put Block List commit (the
    If-None-Match: * create-only invariant rides the commit / the single
    PUT). Diskless deliberately: 6 workers x a 12 GB temp file would
    exhaust the 64 GB OS disk; 6 x 64 MiB of RAM cannot."""
    status, item = api.get(f"/drives/{drive_id}/items/{item_id}", "download")
    if status == 404:
        return "gone", 0
    if status not in (200, 201) or not item \
            or not item.get("@microsoft.graph.downloadUrl"):
        raise CopyError(f"fallback item GET status {status}")
    chunk_size = 64 * MIB
    api._bucket.wait()   # the streamed GET is an SP hit like any other
    req = urllib.request.Request(item["@microsoft.graph.downloadUrl"])
    n = 0
    block_ids: list[str] = []
    stage = "source download"   # explicit, not inferred from block_ids:
                                # inferring mislabelled a FIRST block PUT
                                # failure as a download failure (the list
                                # is still empty then) and sent a
                                # diagnosis down the wrong path
    try:
        try:
            r0 = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            # Defender has quarantined the item: Graph answers the
            # download (never the metadata) with 403 malwareDetected and
            # will NEVER serve those bytes. That is a source-side FACT,
            # not our failure and not retryable — record it and move on.
            # Bypassing a malware control to push the file into the
            # client's corpus is not ours to decide.
            if e.code == 403:
                body = e.read().decode("utf-8", "replace")
                if "malwareDetected" in body:
                    log(f"copy: {blob_name.rsplit('/', 1)[-1][:40]} is "
                        "quarantined at source (malwareDetected) — "
                        "recorded, not copied")
                    return "quarantined", 0
                raise urllib.error.HTTPError(
                    e.url, e.code, e.reason, e.headers, None)
            raise
        with r0 as r:
            first = _read_exact(r, chunk_size)
            if len(first) < chunk_size:   # whole file fits one chunk
                got = azure_put_bytes(dest_base, sas, blob_name, first, mime)
                return ("copied" if got else "already"), len(first)
            id_bytes = existing_block_id_bytes(dest_base, sas, blob_name)
            i = 0
            chunk = first
            while chunk:
                stage = f"block PUT #{i}"
                bid = _block_id(i, id_bytes)
                url = (_blob_url(dest_base, blob_name) + "?comp=block"
                       "&blockid=" + urllib.parse.quote(bid) + "&" + sas)
                req2 = urllib.request.Request(
                    url, data=chunk, method="PUT",
                    headers={"x-ms-version": X_MS_VERSION})
                with urllib.request.urlopen(req2, timeout=900) as r2:
                    r2.read()
                block_ids.append(bid)
                n += len(chunk)
                i += 1
                if i % 16 == 0:
                    log(f"stream: {blob_name.rsplit('/', 1)[-1][:40]} "
                        f"{human_bytes(n)}/{human_bytes(size)}")
                stage = "source download"
                chunk = _read_exact(r, chunk_size)
        stage = "block list commit"
        got = put_block_list(dest_base, sas, blob_name, block_ids, mime)
        return ("copied" if got else "already"), n
    except urllib.error.HTTPError as e:
        # name the STAGE — a bare code cost a diagnosis cycle (2026-08-30)
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:160]
        except Exception:  # noqa: BLE001
            pass
        raise CopyError(f"streamed fallback failed at {stage}: "
                        f"HTTP {e.code} {body}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CopyError(f"streamed fallback failed: {e}")


# ── per-site unit ────────────────────────────────────────────────────────────

def pull_site(api: GraphAPI, row: dict, dest_folder: dict[str, int],
              ctx: dict) -> dict:
    """One site folder: walk -> manifest -> diff -> (unless DIFF_ONLY /
    gate pending) copy each missing file. Returns the site result row."""
    folder = row["folder"]
    walk = walk_collection(api, row.get("site_ids") or [])
    expected = walk["expected"]
    write_manifest(ctx["manifests_dir"], folder, row.get("site_url") or "",
                   expected)
    diff = diff_folder(expected, dest_folder)
    result = {
        "folder": folder, "site_url": row.get("site_url"),
        "calibrate": bool(row.get("calibrate")),
        "webs": len(row.get("site_ids") or []),
        "drives_walked": walk["drives_walked"],
        "expected_files": len(expected),
        "expected_bytes": sum(v[0] for v in expected.values()),
        "matched": diff["matched"], "matched_bytes": diff["matched_bytes"],
        "missing_before": len(diff["missing"]),
        "missing_bytes": sum(expected[m][0] for m in diff["missing"]),
        "mismatched": diff["mismatched"][:200],
        "mismatched_count": len(diff["mismatched"]),
        "dest_only": diff["dest_only"],
        "dest_only_bytes": diff["dest_only_bytes"],
        "sidecars": diff["sidecars"],
        "collisions": walk["collisions"][:50],
        "walk_errors": walk["errors"][:20],
        "copied": 0, "copied_bytes": 0, "already_existed": 0,
        "gone_at_source": 0, "quarantined": 0, "quarantined_paths": [],
        "copy_errors": [],
    }
    if walk["errors"]:
        # an incomplete walk must never masquerade as a clean diff — the
        # site is failed (re-run re-walks it), nothing is copied from a
        # partial expected-map
        result["status"] = "failed"
        result["reason"] = "walk-errors"
        return result
    if mapping_suspect(diff, len(expected)):
        result["status"] = "failed"
        result["reason"] = "mapping-suspect"
        result["note"] = ("existing folder content barely overlaps the "
                          "walked site's paths — likely mapped to a "
                          "doppelganger site; NOTHING was copied. Fix the "
                          "row in mapping.json and re-approve.")
        return result
    if ctx["diff_only"]:
        result["status"] = "diff-only"
        return result

    # Within-site copy POOL: one worker's round-trip latency must not
    # bound a monster site (VSS1: 650k missing files sequentially would
    # crawl for days) — the shared adaptive bucket is the real rate cap,
    # these threads just keep it saturated.
    copy_errors: list[dict] = []
    clock = threading.Lock()
    cq: queue.Queue = queue.Queue()
    for rel in diff["missing"]:
        cq.put(rel)

    def copy_worker():
        while True:
            try:
                rel = cq.get_nowait()
            except queue.Empty:
                return
            size, drive_id, item_id, mime = expected[rel]
            blob_name = f"{ctx['dest_prefix']}/{folder}/{rel}"
            try:
                outcome, nbytes = copy_file(
                    api, ctx["dest_base"], ctx["sas"], blob_name, drive_id,
                    item_id, size, mime)
            except CopyError as e:
                with clock:
                    copy_errors.append({"path": rel, "error": str(e)[:200]})
                continue
            except Exception as e:  # noqa: BLE001 — never kills a site
                with clock:
                    copy_errors.append(
                        {"path": rel,
                         "error": f"{type(e).__name__}: {e}"[:200]})
                continue
            with clock:
                if outcome == "copied":
                    result["copied"] += 1
                    result["copied_bytes"] += nbytes
                elif outcome == "already":
                    result["already_existed"] += 1
                elif outcome == "gone":
                    result["gone_at_source"] += 1
                elif outcome == "quarantined":
                    result["quarantined"] += 1
                    if len(result["quarantined_paths"]) < 50:
                        result["quarantined_paths"].append(rel)
                done = (result["copied"] + result["already_existed"]
                        + result["gone_at_source"] + result["quarantined"]
                        + len(copy_errors))
            if done % 500 == 0:
                log(f"{folder}: {done}/{len(diff['missing'])} missing "
                    f"handled ({human_bytes(result['copied_bytes'])} "
                    "copied)")

    n_threads = min(ctx.get("copy_threads", 8),
                    max(1, len(diff["missing"])))
    pool = [threading.Thread(target=copy_worker, daemon=True)
            for _ in range(n_threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    result["copy_errors"] = copy_errors[:100]
    result["copy_error_count"] = len(copy_errors)
    result["status"] = "failed" if copy_errors else "ok"
    if copy_errors:
        result["reason"] = "copy-errors"
    return result


def gate_check(result: dict) -> str | None:
    """PURE. None = gate passed for this calibration site; else the human-
    readable breach reason."""
    exp = result.get("expected_files") or 0
    if not exp:
        return None    # an empty site can't calibrate anything, but also
                       # can't prove a convention bug
    frac = (result.get("missing_before") or 0) / exp
    if frac > CALIBRATION_MAX_MISSING:
        return (f"calibration site {result['folder']}: "
                f"{result['missing_before']}/{exp} files "
                f"({100 * frac:.1f}%) read as missing — the path convention "
                "is suspect; NOTHING was copied")
    return None


def dump_gate_samples(result: dict, expected: dict, dest: dict) -> None:
    """Sample missing paths next to dest neighbors so a human can compare
    conventions from the log alone."""
    missing = sorted(expected.keys() - dest.keys())[:GATE_SAMPLE]
    log("gate samples — EXPECTED (from Graph) but not in dest:")
    for m in missing:
        log(f"  graph: {m}")
        lib = m.split("/", 1)[0]
        near = [d for d in dest if d.startswith(lib + "/")][:2] \
            or list(dest)[:2]
        for n in near:
            log(f"   dest: {n}")


# ── state ────────────────────────────────────────────────────────────────────

class RunState:
    """state.json (latest per-folder outcome) + results.jsonl (append-only
    log), both under one lock — workers report concurrently."""

    def __init__(self, base: Path):
        self.base = base
        self.lock = threading.Lock()
        self.path = base / "state.json"
        try:
            self.state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.state = {}

    def is_ok(self, folder: str) -> bool:
        return (self.state.get(folder) or {}).get("status") == "ok"

    def record(self, result: dict) -> None:
        with self.lock:
            with open(self.base / "results.jsonl", "a") as fh:
                fh.write(json.dumps(result) + "\n")
            self.state[result["folder"]] = {
                "status": result["status"],
                "missing_before": result.get("missing_before"),
                "copied": result.get("copied"),
                "copy_errors": result.get("copy_error_count", 0),
                "finished_utc": utcnow_iso(),
            }
            atomic_write_json(self.path, self.state)


def write_progress(base: Path, phase: str, done: int, total: int,
                   message: str) -> None:
    try:
        (base / "progress.json").write_text(json.dumps(
            {"source": "saxon-sp", "phase": phase, "done": done,
             "total": total, "message": message, "utc": utcnow_iso()}))
    except OSError:
        pass


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    tenant = os.environ.get("SP_TENANT_ID", "").strip()
    client_id = os.environ.get("SP_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SP_CLIENT_SECRET", "").strip()
    if not (tenant and client_id and client_secret):
        log("FATAL: SP_TENANT_ID/SP_CLIENT_ID/SP_CLIENT_SECRET not all in "
            "environment (sp.env not sourced?)")
        return 1
    dest_url = os.environ.get("DEST_URL", "").strip().rstrip("/")
    dest_sas = os.environ.get("DEST_SAS", "").strip()
    if not (dest_url and dest_sas):
        log("FATAL: DEST_URL/DEST_SAS not in environment "
            "(dest-sp.env not sourced?)")
        return 1
    dest_prefix = (os.environ.get("DEST_PREFIX", "").strip()
                   or DEFAULT_DEST_PREFIX).strip("/")

    def _env_float(name, default):
        raw = os.environ.get(name, "").strip()
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    rps = _env_float("RPS_GRAPH", DEFAULT_RPS_GRAPH)
    max_rps = _env_float("MAX_RPS", 16.0)
    copy_threads = int(_env_float("COPY_THREADS", 8))
    workers = int(_env_float("WORKERS", DEFAULT_WORKERS))
    diff_only = os.environ.get("DIFF_ONLY", "").strip() == "1"
    refresh = os.environ.get("REFRESH_SITES", "").strip() == "1"
    allow_no_cal = os.environ.get("ALLOW_NO_CALIBRATION", "").strip() == "1"
    only_raw = os.environ.get("ONLY_SITES", "").strip()
    only_sites = {s.strip() for s in only_raw.split(",") if s.strip()} or None
    limit_raw = os.environ.get("LIMIT_SITES", "").strip()
    limit_sites = int(limit_raw) if limit_raw.isdigit() else 0

    base = Path(os.path.expanduser(
        os.environ.get("XFER_BASE", "~/xfer-sp")))
    base.mkdir(parents=True, exist_ok=True)
    mapping_path = base / "mapping.json"
    try:
        mapping = json.loads(mapping_path.read_text())
    except (OSError, ValueError) as e:
        log(f"FATAL: cannot read {mapping_path}: {e} — push-mapping first")
        return 1
    if not mapping.get("approved_utc"):
        log("FATAL: mapping.json is not approved — run approve-mapping on "
            "the laptop, then push-mapping")
        return 1

    # a stale summary from an earlier pass must never read as this run's
    # terminal state (status/monitors key on its existence)
    try:
        (base / "run-summary.json").unlink()
    except OSError:
        pass

    started = utcnow_iso()
    box = TokenBox(tenant, client_id, client_secret)
    box.mint()   # proves credentials before anything else
    api = GraphAPI(box, rps=rps, max_rps=max_rps)

    plan = plan_sites(mapping, only_sites, limit_sites)
    cal_rows = [r for r in plan if r.get("calibrate")]
    rest_rows = [r for r in plan if not r.get("calibrate")]
    log(f"plan: {len(plan)} site(s) ({len(cal_rows)} calibration), "
        f"workers={workers}, copy_threads={copy_threads}, rps={rps} "
        f"(adaptive to {max_rps}), diff_only={diff_only}")
    if not plan:
        log("FATAL: nothing to do (mapping has no action=complete rows "
            "matching the filters)")
        return 1
    if not diff_only and not cal_rows and not allow_no_cal:
        log("FATAL: no calibration rows in the plan and DIFF_ONLY is off — "
            "refusing to copy into the client's prefix without the path-"
            "convention gate (set ALLOW_NO_CALIBRATION=1 only after a "
            "diff-only pass proved the convention)")
        return 1

    write_progress(base, "dest-index", 0, 0, "listing container prefix")
    log(f"building dest index of {dest_prefix}/ ...")
    dest_index = build_dest_index(dest_url, dest_sas, dest_prefix)

    ctx = {"dest_base": dest_url, "sas": dest_sas,
           "dest_prefix": dest_prefix, "diff_only": diff_only,
           "copy_threads": copy_threads,
           "manifests_dir": base / "manifests"}
    state = RunState(base)
    results: list[dict] = []
    results_lock = threading.Lock()

    # ── phase 1: calibration, sequential, before any other site copies ──
    for i, row in enumerate(cal_rows, 1):
        write_progress(base, "calibrate", i, len(cal_rows), row["folder"])
        log(f"[cal {i}/{len(cal_rows)}] {row['folder']}")
        # calibration diffs first; its own missing tail is copied only
        # after the gate passes for ALL calibration sites
        cal_ctx = dict(ctx, diff_only=True)
        res = pull_site(api, row, dest_index.get(row["folder"], {}), cal_ctx)
        res["status"] = "calibrated" if res["status"] == "diff-only" \
            else res["status"]
        breach = None if res["status"] == "failed" else gate_check(res)
        if res["status"] == "failed":
            log(f"FATAL: calibration site {row['folder']} failed its walk "
                f"({res.get('walk_errors')}) — fix and re-run")
            state.record(res)
            return 1
        if breach:
            log(f"FATAL: {breach}")
            # re-derive the expected map for samples (cheap: manifest is
            # on disk)
            exp = {}
            mpath = ctx["manifests_dir"] / f"{row['folder']}.tsv.gz"
            with gzip.open(mpath, "rt", encoding="utf-8") as fh:
                for ln in fh:
                    if ln.startswith("#"):
                        continue
                    p = ln.rstrip("\n").split("\t")
                    if len(p) >= 2:
                        exp[p[0]] = int(p[1])
            dump_gate_samples(res, exp, dest_index.get(row["folder"], {}))
            state.record(res)
            return 1
        log(f"[cal] {row['folder']}: PASSED "
            f"({res['missing_before']}/{res['expected_files']} missing)")
        state.record(res)
        with results_lock:
            results.append(res)
    if cal_rows and not diff_only:
        log("calibration gate PASSED — copying begins")

    # ── phase 2: everything (calibration sites rejoin for their own
    #    missing tails; their fresh diff makes the re-walk cheap in
    #    outcome even if not in time) ──
    todo = (cal_rows if not diff_only else []) + rest_rows
    todo = [r for r in todo
            if refresh or not state.is_ok(r["folder"])
            or diff_only]
    skipped_complete = len(plan) - len(todo) - (len(cal_rows) if diff_only
                                               else 0)
    if skipped_complete > 0:
        log(f"{skipped_complete} site(s) already ok in state.json — "
            "skipped (REFRESH_SITES=1 re-walks them)")

    q: queue.Queue = queue.Queue()
    for r in todo:
        q.put(r)
    done_count = [0]

    def worker():
        while True:
            try:
                row = q.get_nowait()
            except queue.Empty:
                return
            folder = row["folder"]
            try:
                res = pull_site(api, row, dest_index.get(folder, {}), ctx)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 — failure isolation
                res = {"folder": folder, "site_url": row.get("site_url"),
                       "status": "failed",
                       "reason": f"{type(e).__name__}: {e}"[:300]}
            state.record(res)
            with results_lock:
                # a calibration site rejoining phase 2 replaces its
                # phase-1 row — one row per folder in the summary
                results[:] = [x for x in results
                              if x["folder"] != res["folder"]]
                results.append(res)
                done_count[0] += 1
                n = done_count[0]
            log(f"[{n}/{len(todo)}] {folder}: {res['status']} "
                f"(missing {res.get('missing_before', '?')}, copied "
                f"{res.get('copied', 0)}, errors "
                f"{res.get('copy_error_count', 0)})")
            write_progress(base, "diff" if diff_only else "pull", n,
                           len(todo), folder)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results if r["status"] in ("ok", "calibrated",
                                                "diff-only")]
    failed = [r for r in results if r["status"] == "failed"]
    copied = sum(r.get("copied", 0) for r in results)
    copied_bytes = sum(r.get("copied_bytes", 0) for r in results)
    missing_total = sum(r.get("missing_before", 0) for r in results)
    summary = {
        "source": "saxon-sp-completion",
        "started_utc": started, "finished_utc": utcnow_iso(),
        "diff_only": diff_only,
        "dest_prefix": dest_prefix,
        "sites_planned": len(plan), "sites_processed": len(results),
        "sites_ok": len(ok), "sites_failed": len(failed),
        "failed_folders": [r["folder"] for r in failed],
        "missing_files_seen": missing_total,
        "files_copied": copied, "bytes_copied": copied_bytes,
        "api_calls": api.calls, "api_sleeps": api.sleeps,
        "token_mints": box.mints,
    }
    atomic_write_json(base / "run-summary.json", summary)
    log(f"SUMMARY: {len(results)} sites processed, {missing_total} missing "
        f"files seen, {copied} copied ({human_bytes(copied_bytes)}), "
        f"{len(failed)} failed {[r['folder'] for r in failed][:10]}, "
        f"{api.calls} api calls, {api.sleeps} sleeps")
    write_progress(base, "done", len(results), len(results),
                   f"{len(failed)} failed")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
