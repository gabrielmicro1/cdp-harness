#!/usr/bin/env python3
"""Zoom -> Azure ingest: pull a company's Zoom cloud recordings (video,
audio, transcripts, in-meeting chat, captions, poll CSVs, timelines, AI
summaries) via the Zoom API and land them in <slug>-raw/zoom-export/ --
WITHOUT the media bytes ever transiting this laptop.

The harness's third VM-less ingest (after qwilr and vimeo), vimeo's
transport verbatim: rclone has no Zoom backend and recordings are tens to
hundreds of GB, so the script resolves each recording file's download URL
(access token appended, redirects walked manually) and drives Azure
server-side copy (Put Blob From URL for small files, Put Block From URL +
Put Block List for large ones) -- the storage fabric pulls from Zoom
directly. Laptop egress = API JSON + control calls. Standalone over
common.py + phases (NOT built on transfer_engine.py, which is
rclone/VM-shaped), but keeps its CLI contract: one JSON object on stdout,
exit 0 ok / 1 hard error / 2 refusal.

Subcommands:
  plan <slug>     read-only, offline: what would be pulled and where it lands
  probe <slug>    credentials on stdin, NO Azure: do the three secrets mint
                  a token, does the LISTING work (an authenticated-but-
                  unactivated S2S app 400s exactly here -- the #1 stall),
                  sample file + Range support, retention/trash context
  pull <slug>     the transfer (credentials on stdin; resumable --
                  re-running skips blobs that already exist)
  verify <slug>   completeness: fresh month-windowed listing vs blobs under
                  the prefix, byte-exact against Zoom's declared file_size

Hard rules (see the zoom-azure-transfer SKILL.md):
- The THREE Server-to-Server OAuth secrets (Account ID, Client ID, Client
  Secret -- scope recording:read:admin) arrive on STDIN only, one per
  line, in that order -- never argv/env/files/logs.
- Writes are create-only (If-None-Match: * on every commit, including
  Put Block List) under the dest prefix.
- Firewall via phases.ip_rule_ensure -- the laptop's external IP, same
  mechanism as sizing. allow-network is the VM path and does not apply.
- The account access token lives ~1h (TokenBox auto-refreshes); download
  URLs are resolved with a fresh token per attempt and never cached.
- The recordings listing is capped to ~1-month windows and each month is
  fully MATERIALIZED before any copy (a next_page_token expires during
  multi-minute copies -- crashed a real Lemonlight run at its first
  2-page month).
- Recording entries with an empty file_type are in-progress placeholder
  rows -- skipped (their download 404s or hangs; wedged a real run 80+
  min).
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path

import common
import phases

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_API = "https://api.zoom.us/v2"
X_MS_VERSION = "2021-08-06"  # >= 2020-04-08 (Put Blob From URL), >= 2019-12-12 (4000 MiB blocks)
DEFAULT_DEST_PREFIX = "zoom-export"
DEFAULT_FROM_DATE = "2015-01-01"
MIB = 1024 * 1024
MAX_BLOCKS = 50_000          # Azure's committed-block cap per blob
RERESOLVE_BUDGET = 20        # the embedded access token lives ~1h; a
                             # multi-hour copy of one file legitimately
                             # needs several fresh resolves
TOKEN_REFRESH_MARGIN = 300   # re-mint 5 min before the 1h expiry
PROBE_SAMPLE_MONTHS = 24     # how far back probe hunts for a sample file

# Zoom's file_type -> extension (fallback: the entry's file_extension)
EXT_BY_FILE_TYPE = {"MP4": "mp4", "M4A": "m4a", "TRANSCRIPT": "vtt",
                    "CHAT": "txt", "CC": "vtt", "CSV": "csv",
                    "TIMELINE": "json", "SUMMARY": "json"}
CT_BY_FILE_TYPE = {"MP4": "video/mp4", "M4A": "audio/mp4",
                   "TRANSCRIPT": "text/vtt", "CHAT": "text/plain",
                   "CC": "text/vtt", "CSV": "text/csv",
                   "TIMELINE": "application/json",
                   "SUMMARY": "application/json"}

_sleep = time.sleep  # seam so tests can record/skip waits


def _http(req: urllib.request.Request, timeout: int = 90):
    """Single transport seam (tests stub this; production never branches)."""
    return urllib.request.urlopen(req, timeout=timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface 3xx as HTTPError so we can read Location


_nr_opener = urllib.request.build_opener(_NoRedirect)


def _http_nr(req: urllib.request.Request, timeout: int = 60):
    """No-redirect transport seam -- 3xx surfaces as HTTPError."""
    return _nr_opener.open(req, timeout=timeout)


class ZoomHTTPError(Exception):
    """Non-auth Zoom API failure -- counted per month/file, not fatal
    (except a 400 on a listing endpoint, which callers convert into the
    activation/scope HarnessError)."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class PutError(Exception):
    """Small-blob upload failure after retries -- counted, not fatal."""


class CopyError(Exception):
    """Server-side copy failure -- counted per file, not fatal. source_side
    means Azure could not fetch from the download URL (usually: the
    embedded access token or signed URL expired) -- the caller re-resolves
    with a fresh token and retries the same block."""

    def __init__(self, msg: str, azure_code: str = "", source_status: str = ""):
        super().__init__(msg)
        self.azure_code = azure_code
        self.source_status = source_status

    @property
    def source_side(self) -> bool:
        return self.azure_code in ("CannotVerifyCopySource",
                                   "CopySourceNotFound") \
            or bool(self.source_status)


def _err_body(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")[:200]
    except Exception:
        return ""


# -- Zoom auth + API ----------------------------------------------------------

class TokenBox:
    """Holds the three S2S OAuth secrets and the short-lived account access
    token they mint. Zoom account tokens live ~1h; get() re-mints
    TOKEN_REFRESH_MARGIN seconds before expiry so a long run never presents
    a dead token. Secrets and token live only in this object (process
    memory) -- never argv/env/files/logs, never printed."""

    def __init__(self, account_id: str, client_id: str, client_secret: str,
                 dry_run: bool = False):
        self._account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._dry_run = dry_run
        self._value: str | None = None
        self._exp = 0.0

    def get(self) -> str:
        if self._dry_run:
            return "<token>"
        if self._value and time.time() < self._exp - TOKEN_REFRESH_MARGIN:
            return self._value
        return self.mint()

    def invalidate(self) -> None:
        """Drop the cached token so the next get() re-mints (used on a 401
        mid-run -- a call can straddle the hourly expiry)."""
        self._value = None

    def mint(self) -> str:
        if self._dry_run:
            return "<token>"
        qs = urllib.parse.urlencode({"grant_type": "account_credentials",
                                     "account_id": self._account_id})
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()).decode()
        last: Exception | None = None
        for attempt in range(6):
            req = urllib.request.Request(
                f"{ZOOM_OAUTH_URL}?{qs}", data=b"", method="POST",
                headers={"Authorization": f"Basic {basic}"})
            try:
                with _http(req, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self._value = data["access_token"]
                self._exp = time.time() + int(data.get("expires_in", 3600))
                print("progress minted zoom access token", file=sys.stderr,
                      flush=True)
                return self._value
            except urllib.error.HTTPError as e:
                if e.code in (400, 401):
                    raise common.HarnessError(
                        f"Zoom OAuth token mint failed (HTTP {e.code}: "
                        f"{_err_body(e)}) -- Account ID / Client ID / "
                        "Client Secret wrong, or the app was deleted; "
                        "retrying cannot help")
                last = e
                if e.code == 429:
                    retry_after = (e.headers.get("Retry-After") or "").strip()
                    _sleep(int(retry_after) if retry_after.isdigit()
                           else min(2 ** attempt, 32))
                    continue
                if e.code >= 500:
                    _sleep(1 + attempt)
                    continue
                raise common.HarnessError(
                    f"Zoom OAuth token mint failed: HTTP {e.code} "
                    f"{_err_body(e)}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                _sleep(1 + attempt)
        raise common.HarnessError(
            f"Zoom OAuth token mint failed after retries: {last}")


def zoom_get(box: TokenBox, path: str, params: dict | None = None) -> dict:
    """GET a Zoom API path. 429 honors Retry-After (else exponential, cap
    32s); 5xx/network get linear backoff; 400 raises ZoomHTTPError
    IMMEDIATELY, never retried (on the listing endpoints it means the S2S
    app is unactivated or mis-scoped -- a client conversation); 401
    re-mints the token ONCE (calls can straddle the hourly expiry) then
    aborts; 403 aborts (a missing scope cannot be retried away)."""
    url = ZOOM_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last: Exception | None = None
    reminted = False
    for attempt in range(6):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {box.get()}"})
        try:
            with _http(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                if not reminted:
                    reminted = True
                    box.invalidate()
                    continue
                raise common.HarnessError(
                    f"Zoom API 401 on {path} even with a freshly minted "
                    "token -- credentials revoked or app deactivated "
                    "(retrying cannot help)")
            if e.code == 403:
                raise common.HarnessError(
                    f"Zoom API 403 on {path} -- the app lacks the "
                    "recording:read:admin scope (retrying cannot help)")
            if e.code == 400:
                raise ZoomHTTPError(400, f"HTTP 400 on {path}: {_err_body(e)}")
            last = e
            if e.code == 429:
                retry_after = (e.headers.get("Retry-After") or "").strip()
                delay = (int(retry_after) if retry_after.isdigit()
                         else min(2 ** attempt, 32))
                _sleep(delay)
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise ZoomHTTPError(e.code, f"HTTP {e.code} on {path}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    status = getattr(last, "code", 0) or 0
    raise ZoomHTTPError(status, f"gave up on {path} after retries: {last}")


def _activation_hint(e: Exception) -> str:
    return ("Zoom recordings listing returned 400 -- the Server-to-Server "
            "OAuth app is not ACTIVATED, or lacks the recording:read:admin "
            "scope (Zoom code 4711 = the master-scope accountId path; this "
            "script always uses the literal 'me'). This is a client "
            f"conversation, not a retry: {e}")


def month_windows(start_iso: str, end_iso: str) -> list[tuple[str, str]]:
    """[(from, to)] ISO date pairs covering [start, end] -- Zoom caps the
    recordings listing to a ~1-month window, so the range is walked month
    by month."""
    start = _dt.date.fromisoformat(start_iso)
    end = _dt.date.fromisoformat(end_iso)
    out: list[tuple[str, str]] = []
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        frm = max(cur, start)
        to = min(nxt - _dt.timedelta(days=1), end)
        out.append((frm.isoformat(), to.isoformat()))
        cur = nxt
    return out


def list_month(box: TokenBox, frm: str, to: str,
               page_size: int = 300) -> list[dict]:
    """One month's recording 'meetings', fully MATERIALIZED: every page is
    fetched back-to-back before any copy starts, because a next_page_token
    expires during multi-minute file copies (400ing the page-2 fetch --
    crashed a real run at its first >300-meeting month). Account-level
    listing uses the literal `me`: a literal accountId in the path needs
    the `:master` scope (master/ISV accounts) and 400s code 4711
    otherwise."""
    meetings: list[dict] = []
    npt = ""
    while True:
        params: dict = {"from": frm, "to": to, "page_size": page_size}
        if npt:
            params["next_page_token"] = npt
        page = zoom_get(box, "/accounts/me/recordings", params)
        meetings.extend(page.get("meetings") or [])
        print(f"progress listed {len(meetings)} meetings in {frm[:7]}",
              file=sys.stderr, flush=True)
        npt = page.get("next_page_token") or ""
        if not npt:
            return meetings


def double_encode(uuid: str) -> str:
    """Meeting UUIDs can contain '/' and '+' and Zoom requires them
    DOUBLE-URL-encoded in API paths."""
    return urllib.parse.quote(urllib.parse.quote(uuid, safe=""), safe="")


def refetch_meeting_files(box: TokenBox, meeting_uuid: str) -> dict:
    """Re-fetch one meeting's recording set -- the re-resolve escape hatch
    when a stored download_url has died."""
    return zoom_get(box, f"/meetings/{double_encode(meeting_uuid)}/recordings")


def should_pull_file(f: dict) -> bool:
    """Whether a recording_files entry is a real, pullable file. Entries
    with no download_url or an EMPTY file_type are in-progress placeholder
    rows carrying no real media -- their download 404s or hangs (wedged a
    real run for 80+ min)."""
    return bool(f.get("download_url")) and bool(f.get("file_type"))


def safe_uuid(mtg: dict) -> str:
    """Blob-directory-safe meeting id. UUIDs are base64-ish ('/', '+', '=')
    -- mapped to filename-safe equivalents, deterministically, so resume
    keys stay stable across runs."""
    raw = str(mtg.get("uuid") or mtg.get("id") or "unknown")
    s = raw.replace("/", "_").replace("+", "-")
    s = re.sub(r"[^A-Za-z0-9_=-]", "", s)
    return s or "unknown"


def blob_name(prefix: str, mtg: dict, f: dict) -> str:
    """Deterministic blob name -- {start}_{TYPE}_{fileid} under the
    meeting's directory. Unlike vimeo (mutable titles), every component is
    immutable, so resume is keyed on the EXACT name."""
    mid = safe_uuid(mtg)
    start = (f.get("recording_start") or mtg.get("start_time") or "")
    start = start.replace(":", "").replace("-", "")
    ftype = (f.get("file_type") or "FILE").upper()
    ext = (EXT_BY_FILE_TYPE.get(ftype)
           or (f.get("file_extension") or "").lower() or "bin")
    fid = str(f.get("id") or "")[:12]
    return f"{prefix}/meetings/{mid}/{start}_{ftype}_{fid}.{ext}"


def resolve_download_url(box: TokenBox, link: str) -> str:
    """Append a FRESH access token to the download URL and follow its
    redirect chain manually to the final signed URL. Azure copy-from-URL
    never follows redirects, so resolution happens here. Both the token
    (~1h) and the signed URL expire -- never cache the result; on any
    source-side failure call again (resolve_fresh)."""
    sep = "&" if "?" in link else "?"
    url = f"{link}{sep}access_token={box.get()}"
    for _hop in range(6):
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        try:
            resp = _http_nr(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise ZoomHTTPError(e.code, "redirect without Location")
                url = urllib.parse.urljoin(url, loc)
                continue
            if e.code == 429:
                _sleep(5)
                continue
            raise ZoomHTTPError(
                e.code, f"resolving download url: HTTP {e.code} "
                        "(expired token/link? re-fetch the meeting's files)")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ZoomHTTPError(0, f"resolving download url: {e}")
        resp.close()
        return url
    raise ZoomHTTPError(0, "redirect loop resolving download url")


def resolve_fresh(box: TokenBox, meeting_uuid: str, f: dict) -> str:
    """Two-stage re-resolve for an expired source URL: re-resolve the
    stored download_url with a fresh token; if the URL itself has died,
    re-fetch the meeting's recording files and resolve the matching entry.
    Updates f['download_url'] so later retries start from the fresh
    link."""
    try:
        return resolve_download_url(box, f["download_url"])
    except ZoomHTTPError:
        mtg = refetch_meeting_files(box, meeting_uuid)
        fresh = None
        for cand in mtg.get("recording_files") or []:
            if not cand.get("download_url"):
                continue
            if str(cand.get("id") or "") == str(f.get("id") or "") and (
                    cand.get("id") or f.get("id")):
                fresh = cand
                break
            if (cand.get("file_type") == f.get("file_type")
                    and cand.get("recording_start")
                    == f.get("recording_start")):
                fresh = fresh or cand  # id-less types (e.g. TIMELINE)
        if not fresh:
            raise CopyError(
                f"re-resolve: file {f.get('id') or f.get('file_type')} of "
                f"meeting {meeting_uuid} no longer offered -- deleted or "
                "past retention")
        f["download_url"] = fresh["download_url"]
        return resolve_download_url(box, fresh["download_url"])


def probe_range(url: str) -> bool:
    """Put Block From URL requires the source to honor Range requests."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with _http(req, timeout=60) as r:
            return getattr(r, "status", None) == 206
    except Exception:
        return False


# -- Azure blob REST ----------------------------------------------------------

def load_cfg(root: Path, slug: str) -> dict:
    try:
        return common.load_config(root, slug)
    except FileNotFoundError:
        raise common.HarnessError(
            f"{slug} is not onboarded (no companies/{slug}/config.json) -- "
            "run onboard-company first")


def mint_write_sas(cfg: dict, days: int, dry_run: bool) -> tuple[str, str]:
    """Container SAS, racwl (the sanctioned ingest write path). Default
    expiry is 2 days -- same reasoning as vimeo: server-side copies of a
    multi-hundred-GB recording library are realistically multi-hour and an
    overnight resume must not straddle the expiry mid-block-loop. Still
    short, still self-lapsing, still held only in this process's memory; a
    resume re-mints."""
    expiry = common.iso(common.utc_now() + timedelta(days=days))
    proc = common.run_az(["storage", "container", "generate-sas",
                          "--account-name", cfg["storage_account"],
                          "-n", cfg["container"],
                          "--permissions", "racwl",
                          "--expiry", expiry, "--https-only",
                          "-o", "tsv"], dry_run=dry_run, timeout=120)
    return ("<sas-redacted>" if dry_run else proc.stdout.strip()), expiry


def _container_url(cfg: dict) -> str:
    return (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")


def _blob_url(cfg: dict, name: str) -> str:
    return _container_url(cfg) + "/" + urllib.parse.quote(name, safe="/")


def azure_get(url: str) -> bytes:
    """GET with retries. A 403 early in a run is usually IP-rule propagation
    (CLAUDE.md lore) -- wait and retry, never re-mint the SAS for it."""
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"x-ms-version": X_MS_VERSION})
        try:
            with _http(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                _sleep(15 * (attempt + 1))  # firewall propagation, not a bad SAS
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise common.HarnessError(f"blob GET failed: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise common.HarnessError(f"blob GET failed after retries: {last}")


def azure_list_blobs(cfg: dict, sas: str, prefix: str,
                     dry_run: bool) -> dict[str, dict]:
    """One marker-paginated listing of the dest prefix -> {name: {size}}.
    Serves as both the resume seed and verify's ground truth
    (Content-Length is what Azure actually committed)."""
    if dry_run:
        print(f"DRY-RUN: GET {_container_url(cfg)}"
              f"?restype=container&comp=list&prefix={prefix}/&<sas-redacted>")
        return {}
    blobs: dict[str, dict] = {}
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
                blobs[name] = {
                    "size": int((props.findtext("Content-Length") or 0)
                                if props is not None else 0),
                }
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return blobs


def _azure_err(e: urllib.error.HTTPError) -> tuple[str, str, str]:
    """(azure error code, copy-source status header, body excerpt)."""
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    m = re.search(r"<Code>([^<]+)</Code>", body)
    code = m.group(1) if m else ""
    src_status = (e.headers.get("x-ms-copy-source-status-code") or "").strip()
    return code, src_status, body[:200]


def azure_put_bytes(cfg: dict, sas: str, name: str, body: bytes,
                    content_type: str, dry_run: bool) -> int:
    """PUT one SMALL blob (JSON -- KBs), create-only. Returns bytes
    uploaded; 0 = already existed (the 409 is If-None-Match doing its
    job)."""
    if dry_run:
        print(f"DRY-RUN: PUT {_blob_url(cfg, name)}?<sas-redacted>  "
              f"(x-ms-blob-type: BlockBlob, If-None-Match: *, "
              f"{content_type}, {len(body)} bytes)")
        return len(body)
    url = _blob_url(cfg, name) + "?" + sas
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="PUT", headers={
            "x-ms-version": X_MS_VERSION,
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": content_type,
            "If-None-Match": "*",  # create-only: the storage API enforces the
        })                         # "never modify existing data" invariant
        try:
            with _http(req, timeout=90) as r:
                r.read()
                return len(body)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 0  # BlobAlreadyExists -- benign, resume artifact
            last = e
            if e.code == 403:
                _sleep(15 * (attempt + 1))  # propagation, never re-mint
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise PutError(f"PUT {name}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise PutError(f"PUT {name} failed after retries: {last}")


def azure_put_json(cfg: dict, sas: str, name: str, obj,
                   dry_run: bool) -> int:
    body = (json.dumps(obj, indent=2) + "\n").encode("utf-8")
    return azure_put_bytes(cfg, sas, name, body, "application/json", dry_run)


def _block_id(i: int) -> str:
    """Deterministic, uniform-length (pre-encoding) block ids -- the same
    index always maps to the same id, so a retried block overwrites its own
    uncommitted predecessor."""
    return base64.b64encode(b"%08d" % i).decode("ascii")


def block_plan(size: int, block_size: int) -> list[tuple[str, int, int]]:
    """[(block_id, start, end_inclusive)] covering size bytes. 50,000 blocks
    x 256 MiB = ~13 TiB ceiling -- orders of magnitude clear of any
    recording."""
    n_blocks = (size + block_size - 1) // block_size
    if n_blocks > MAX_BLOCKS:
        raise CopyError(f"file needs {n_blocks} blocks (> {MAX_BLOCKS}); "
                        "raise --block-size-mb")
    return [(_block_id(i), start, min(start + block_size, size) - 1)
            for i, start in enumerate(range(0, size, block_size))]


def put_blob_from_url(cfg: dict, sas: str, name: str, src_url: str,
                      content_type: str, dry_run: bool) -> int:
    """Single-request server-side copy (files <= --single-shot-max-mb).
    Azure fetches the bytes from Zoom itself. Zoom declares no per-file
    md5, so unlike vimeo there is no source-content-md5 validation --
    verify's byte-exact size check is the integrity authority. Returns
    1 on success; 0 = blob already existed (409). Dest-side 403/5xx
    retried here; a source-side failure raises CopyError immediately so
    the caller can re-resolve the expired URL."""
    headers = {
        "x-ms-version": X_MS_VERSION,
        "x-ms-blob-type": "BlockBlob",
        "x-ms-copy-source": src_url,
        "x-ms-blob-content-type": content_type,
        "If-None-Match": "*",
    }
    if dry_run:
        print(f"DRY-RUN: PUT {_blob_url(cfg, name)}?<sas-redacted>  "
              f"(x-ms-copy-source: <zoom-url>, x-ms-blob-type: BlockBlob, "
              f"If-None-Match: *)")
        return 1
    url = _blob_url(cfg, name) + "?" + sas
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=b"", method="PUT",
                                     headers=headers)
        try:
            with _http(req, timeout=900) as r:
                r.read()
                return 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 0  # BlobAlreadyExists -- benign, resume artifact
            code, src_status, excerpt = _azure_err(e)
            err = CopyError(f"copy {name}: HTTP {e.code} {code} "
                            f"(source status {src_status or '-'}) {excerpt}",
                            azure_code=code, source_status=src_status)
            if err.source_side:
                raise err
            last = err
            if e.code == 403:
                _sleep(15 * (attempt + 1))  # propagation, never re-mint
                continue
            if e.code >= 500:
                _sleep(2 + 2 * attempt)
                continue
            raise err
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(2 + 2 * attempt)
    raise CopyError(f"copy {name} failed after retries: {last}")


def put_block_from_url(cfg: dict, sas: str, name: str, block_id: str,
                       src_url: str, start: int, end: int,
                       dry_run: bool) -> None:
    """Stage one block server-side (Azure fetches bytes start..end from
    Zoom). Staged blocks are invisible until Put Block List and self-expire
    in ~7 days if never committed. Dest-side 403/5xx retried here; a
    source-side failure (expired URL surfaces as CannotVerifyCopySource)
    raises immediately so the caller re-resolves and retries the SAME block
    id."""
    if dry_run:
        print(f"DRY-RUN: PUT {_blob_url(cfg, name)}"
              f"?comp=block&blockid={block_id}&<sas-redacted>  "
              f"(x-ms-copy-source: <zoom-url>, "
              f"x-ms-source-range: bytes={start}-{end})")
        return
    url = (_blob_url(cfg, name) + "?comp=block&blockid="
           + urllib.parse.quote(block_id) + "&" + sas)
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=b"", method="PUT", headers={
            "x-ms-version": X_MS_VERSION,
            "x-ms-copy-source": src_url,
            "x-ms-source-range": f"bytes={start}-{end}",
        })
        try:
            with _http(req, timeout=900) as r:
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
                _sleep(15 * (attempt + 1))
                continue
            if e.code >= 500:
                _sleep(2 + 2 * attempt)
                continue
            raise err
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(2 + 2 * attempt)
    raise CopyError(f"block {block_id} of {name} failed after retries: {last}")


def put_block_list(cfg: dict, sas: str, name: str, block_ids: list[str],
                   content_type: str, dry_run: bool) -> int:
    """Commit the staged blocks -- the moment the blob comes into existence,
    which is why If-None-Match: * rides HERE (create-only invariant).
    Returns 1 committed, 0 = blob already existed (409, benign)."""
    body = ("<?xml version='1.0' encoding='utf-8'?><BlockList>"
            + "".join(f"<Latest>{bid}</Latest>" for bid in block_ids)
            + "</BlockList>").encode("utf-8")
    if dry_run:
        print(f"DRY-RUN: PUT {_blob_url(cfg, name)}"
              f"?comp=blocklist&<sas-redacted>  "
              f"(If-None-Match: *, {len(block_ids)} blocks)")
        return 1
    url = _blob_url(cfg, name) + "?comp=blocklist&" + sas
    headers = {
        "x-ms-version": X_MS_VERSION,
        "Content-Type": "application/xml",
        "x-ms-blob-content-type": content_type,
        "If-None-Match": "*",
    }
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="PUT",
                                     headers=headers)
        try:
            with _http(req, timeout=120) as r:
                r.read()
                return 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 0
            code, _src, excerpt = _azure_err(e)
            last = CopyError(f"commit {name}: HTTP {e.code} {code} {excerpt}",
                             azure_code=code)
            if e.code == 403:
                _sleep(15 * (attempt + 1))
                continue
            if e.code >= 500:
                _sleep(2 + 2 * attempt)
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(2 + 2 * attempt)
    raise CopyError(f"commit {name} failed after retries: {last}")


def copy_file_to_blob(cfg: dict, sas: str, box: TokenBox, meeting_uuid: str,
                      name: str, f: dict, args) -> int:
    """Server-side copy of one recording file. Small files (or unknown
    size): one Put Blob From URL. Large files: Put Block From URL per
    block + Put Block List. Expired source URLs (source_side CopyError)
    are re-resolved in place with a FRESH token -- an hourly-expiring
    token over a multi-hour copy is NORMAL, not an error -- up to
    RERESOLVE_BUDGET times. Returns bytes copied server-side (0 = blob
    already existed)."""
    size = int(f.get("file_size") or 0)
    ct = CT_BY_FILE_TYPE.get((f.get("file_type") or "").upper(),
                             "application/octet-stream")
    src = ("<zoom-url>" if args.dry_run
           else resolve_download_url(box, f["download_url"]))
    single_max = args.single_shot_max_mb * MIB
    label = name.rsplit("/", 1)[-1]
    reresolves = 0

    def _reresolve(e: CopyError) -> str:
        nonlocal reresolves
        if args.dry_run or not e.source_side or reresolves >= RERESOLVE_BUDGET:
            raise e
        reresolves += 1
        print(f"progress   {label}: source URL expired, re-resolving "
              f"({reresolves}/{RERESOLVE_BUDGET})", file=sys.stderr,
              flush=True)
        return resolve_fresh(box, meeting_uuid, f)

    if size <= single_max:  # includes size-unknown (0): single-shot caps 5000 MB
        while True:
            try:
                got = put_blob_from_url(cfg, sas, name, src, ct, args.dry_run)
                return size if got else 0
            except CopyError as e:
                src = _reresolve(e)

    blocks = block_plan(size, args.block_size_mb * MIB)
    for i, (bid, start, end) in enumerate(blocks, 1):
        while True:
            try:
                put_block_from_url(cfg, sas, name, bid, src, start, end,
                                   args.dry_run)
                break
            except CopyError as e:
                src = _reresolve(e)
        if i % 10 == 0 or i == len(blocks):
            print(f"progress   {label}: block {i}/{len(blocks)}",
                  file=sys.stderr, flush=True)
    committed = put_block_list(cfg, sas, name, [b[0] for b in blocks], ct,
                               args.dry_run)
    return size if committed else 0


# -- credentials --------------------------------------------------------------

def read_credentials(dry_run: bool) -> tuple[str, str, str]:
    """The three S2S OAuth secrets arrive on stdin ONLY (heredoc), one per
    line: Account ID, Client ID, Client Secret -- argv is world-readable
    via ps, env leaks into child processes, files persist."""
    data = "" if sys.stdin.isatty() else sys.stdin.read()
    lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
    if len(lines) == 3:
        return lines[0], lines[1], lines[2]
    if dry_run and not lines:
        return "<account-id>", "<client-id>", "<client-secret>"
    raise common.HarnessError(
        "expected exactly 3 non-empty lines on stdin -- Account ID, "
        "Client ID, Client Secret (in that order): "
        "pull <slug> <<'EOF' ... EOF")


def _current_month(today: _dt.date) -> tuple[str, str]:
    return today.replace(day=1).isoformat(), today.isoformat()


def _to_date(args) -> str:
    return args.to_date or common.utc_now().date().isoformat()


# -- subcommands --------------------------------------------------------------

def cmd_plan(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    declared = None
    try:
        expected = common.load_expected(root, args.slug)
        declared = (expected.get("services", {}).get("zoom") or {}).get("bytes")
    except FileNotFoundError:
        pass
    return {
        "ok": True,
        "slug": args.slug,
        "mode": "local orchestration, Azure server-side copy (no VM, no "
                "rclone; recording bytes never transit this laptop)",
        "storage_account": cfg["storage_account"],
        "container": cfg["container"],
        "dest": f"{cfg['container']}/{args.dest_prefix}",
        "declared_zoom_bytes": declared,
        "source": "Zoom API (api.zoom.us); Server-to-Server OAuth app with "
                  "the recording:read:admin scope -- Account ID + Client ID "
                  "+ Client Secret on stdin (3 lines) at probe/pull time. "
                  "The app must be ACTIVATED by the account owner (an "
                  "unactivated app authenticates but every listing 400s): "
                  "run probe before promising anything.",
        "date_range": f"{args.from_date} .. {_to_date(args)} "
                      "(walked in ~1-month windows -- Zoom caps the "
                      "listing endpoint)",
        "sas": "container SAS, racwl, minted at pull time",
        "sas_days": args.sas_days,
        "firewall": "per-run IP rule for this laptop's public IP, added and "
                    "removed by pull itself (phases.ip_rule_ensure)",
        "will_write": [
            f"{args.dest_prefix}/meetings/<uuid>/<start>_<TYPE>_<fileid>"
            ".<ext> (MP4/M4A media + TRANSCRIPT/CHAT/CC/CSV/TIMELINE/"
            "SUMMARY sidecars; Azure pulls each from Zoom server-side)",
            f"{args.dest_prefix}/meetings/<uuid>/metadata.json",
            f"{args.dest_prefix}/_meta/{{recordings-index,account}}-<ts>"
            ".json",
        ],
        "note": "writes are create-only (If-None-Match: *); re-running pull "
                "skips files already landed. Zoom auto-deletes recordings "
                "past the account retention window -- only what is still "
                "in the cloud is pullable (probe surfaces retention). Team "
                "Chat history, Zoom Phone and Whiteboards are out of "
                "scope.",
    }


def cmd_probe(root: Path, args) -> dict:
    """Day-one gate, NO Azure: do the three secrets mint a token, and does
    the account-wide recordings LISTING actually work? An S2S app that was
    created but never ACTIVATED authenticates fine and then 400s on the
    listing -- the #1 stall, and a client conversation, not a retry."""
    load_cfg(root, args.slug)  # onboarding check only; no network from it
    creds = read_credentials(args.dry_run)
    if args.dry_run:
        print("DRY-RUN: POST https://zoom.us/oauth/token?grant_type="
              "account_credentials&account_id=<redacted> "
              "(Authorization: Basic <credentials-redacted>)")
        print("DRY-RUN: GET https://api.zoom.us/v2/users/me "
              "(Authorization: Bearer <token-redacted>)")
        print("DRY-RUN: GET https://api.zoom.us/v2/accounts/me/recordings"
              "?from=<month-start>&to=<today>&page_size=1")
        print("DRY-RUN: GET <download_url>?access_token=<token-redacted> "
              "(Range: bytes=0-0, follow redirects)")
        return {"ok": True, "note": "dry-run: no requests made"}

    box = TokenBox(*creds)
    box.mint()  # proves the three secrets; HarnessError if not

    account = None
    try:
        me = zoom_get(box, "/users/me")
        account = {"email": me.get("email"), "type": me.get("type"),
                   "account_id": None}  # never echo the client's account id
    except (ZoomHTTPError, common.HarnessError):
        pass  # /users/me may be outside the granted scopes -- not the gate

    today = common.utc_now().date()
    frm, to = _current_month(today)
    try:
        page = zoom_get(box, "/accounts/me/recordings",
                        {"from": frm, "to": to, "page_size": 1})
    except ZoomHTTPError as e:
        if e.status == 400:
            return {"ok": False, "token_ok": True, "listing_ok": False,
                    "account": account, "error": str(e),
                    "hint": _activation_hint(e)}
        raise common.HarnessError(f"Zoom probe listing failed: {e}")

    # Hunt backwards for a sample pullable file (recent months first).
    sample = None
    sample_month = None
    range_probe = None
    file_types: dict[str, int] = {}
    placeholders = 0
    windows = month_windows(args.from_date, today.isoformat())
    for w_frm, w_to in reversed(windows[-PROBE_SAMPLE_MONTHS:]):
        try:
            mtgs = list_month(box, w_frm, w_to)
        except ZoomHTTPError as e:
            range_probe = f"sample-listing-failed: {e}"
            break
        for mtg in mtgs:
            for f in mtg.get("recording_files") or []:
                if not should_pull_file(f):
                    placeholders += 1
                    continue
                ftype = f.get("file_type") or "?"
                file_types[ftype] = file_types.get(ftype, 0) + 1
                if sample is None:
                    sample = {"file_type": ftype,
                              "file_size": f.get("file_size"),
                              "recording_start": f.get("recording_start")}
                    sample_month = w_frm[:7]
                    try:
                        url = resolve_download_url(box, f["download_url"])
                        range_probe = ("206-ok" if probe_range(url)
                                       else "no-range-support")
                    except ZoomHTTPError as e:
                        range_probe = f"resolve-failed: {e}"
        if sample:
            break

    result = {
        "ok": True,
        "token_ok": True,
        "listing_ok": True,
        "account": account,
        "recordings_this_month": page.get("total_records"),
        "sample_month": sample_month,
        "sample": sample,
        "file_types_seen": file_types,
        "placeholders_seen": placeholders,
        "range_probe": range_probe,
    }
    if sample is None:
        result["note"] = (
            f"no pullable recording found in the last "
            f"{PROBE_SAMPLE_MONTHS} months -- the account may hold only "
            "older recordings (pull still walks the full --from-date "
            "range) or none at all; confirm scope with the client")

    # Retention + trash context -- both best-effort (the recording scope
    # may not cover settings), never the gate.
    try:
        rec = (zoom_get(box, "/users/me/settings") or {}).get("recording") or {}
        if rec.get("auto_delete_cmr") is not None:
            result["retention"] = {
                "auto_delete": rec.get("auto_delete_cmr"),
                "auto_delete_days": rec.get("auto_delete_cmr_days"),
                "note": "Zoom deletes cloud recordings past this window -- "
                        "the engagement has a clock",
            }
        else:
            result["retention"] = ("unknown (settings readable but no "
                                   "auto-delete fields)")
    except (ZoomHTTPError, common.HarnessError):
        result["retention"] = ("unknown (settings not readable with this "
                               "app's scopes)")
    try:
        trash = zoom_get(box, "/accounts/me/recordings",
                         {"from": frm, "to": to, "page_size": 1,
                          "trash": "true",
                          "trash_type": "meeting_recordings"})
        result["trash_note"] = (
            f"{trash.get('total_records', 0)} recording meeting(s) in "
            "trash this month (recoverable ~30 days; informational -- "
            "pull takes live recordings only)")
    except (ZoomHTTPError, common.HarnessError):
        result["trash_note"] = "trash listing not available -- informational"
    if range_probe == "no-range-support":
        result["hint"] = (
            "the download host refused a Range request -- files above "
            "--single-shot-max-mb cannot be block-copied. Raise "
            "--single-shot-max-mb (hard cap 5000 MB) or investigate "
            "before the full pull.")
    return result


def cmd_pull(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    creds = read_credentials(args.dry_run)
    box = TokenBox(*creds, dry_run=args.dry_run)
    prefix = args.dest_prefix
    to_date = _to_date(args)

    # Validate the credentials AND the listing BEFORE touching the firewall
    # -- fail fast (especially on the unactivated-app 400) rather than
    # after the 60s propagation sleep.
    if args.dry_run:
        print("DRY-RUN: POST https://zoom.us/oauth/token?grant_type="
              "account_credentials&account_id=<redacted> "
              "(Authorization: Basic <credentials-redacted>)")
        print("DRY-RUN: GET https://api.zoom.us/v2/accounts/me/recordings"
              "?from=<month-start>&to=<today>&page_size=1 (listing gate)")
    else:
        box.mint()
        frm, to = _current_month(common.utc_now().date())
        try:
            zoom_get(box, "/accounts/me/recordings",
                     {"from": frm, "to": to, "page_size": 1})
        except ZoomHTTPError as e:
            if e.status == 400:
                raise common.HarnessError(_activation_hint(e))
            raise common.HarnessError(f"Zoom listing validation failed: {e}")

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas, sas_expiry = mint_write_sas(cfg, args.sas_days, args.dry_run)
        existing = azure_list_blobs(cfg, sas, prefix, args.dry_run)

        if args.dry_run:
            print("DRY-RUN: GET https://api.zoom.us/v2/accounts/me/"
                  "recordings?from=<YYYY-MM-01>&to=<YYYY-MM-31>"
                  "&page_size=300 (one ~1-month window at a time, "
                  "next_page_token loop, each month fully materialized "
                  "before any copy)")
            print("DRY-RUN: GET <download_url>?access_token="
                  "<token-redacted> -> 302 -> signed URL (re-resolved with "
                  "a fresh token on every retry; never cached)")
            windows: list[tuple[str, str]] = []
        else:
            windows = month_windows(args.from_date, to_date)

        months_listed = 0
        meetings_total = files_total = 0
        copied = skipped = placeholders = 0
        bytes_copied = bytes_local = 0
        errors: dict[str, str] = {}
        month_errors: dict[str, str] = {}
        outcomes: dict[str, dict] = {}
        consecutive_failures = 0
        limit_hit = False

        for frm, to in windows:
            try:
                mtgs = list_month(box, frm, to)
            except ZoomHTTPError as e:
                if e.status == 400:
                    raise common.HarnessError(_activation_hint(e))
                # per-month failure isolation: counted, skipped, the
                # idempotent re-run fills the gap
                month_errors[frm[:7]] = str(e)
                continue
            months_listed += 1
            for mtg in mtgs:
                if (args.meeting_limit
                        and meetings_total >= args.meeting_limit):
                    limit_hit = True
                    break
                meetings_total += 1
                mid = safe_uuid(mtg)
                raw_uuid = str(mtg.get("uuid") or mtg.get("id") or "")
                meta_name = f"{prefix}/meetings/{mid}/metadata.json"
                try:
                    if meta_name not in existing:
                        bytes_local += azure_put_json(cfg, sas, meta_name,
                                                      mtg, args.dry_run)
                except PutError as e:
                    errors[meta_name] = str(e)
                for f in mtg.get("recording_files") or []:
                    if not should_pull_file(f):
                        placeholders += 1
                        continue
                    files_total += 1
                    name = blob_name(prefix, mtg, f)
                    if name in existing:
                        skipped += 1
                        outcomes[name] = {"outcome": "skipped-existing"}
                        continue
                    try:
                        n = copy_file_to_blob(cfg, sas, box, raw_uuid,
                                              name, f, args)
                        if n == 0:
                            skipped += 1
                            outcomes[name] = {"outcome": "skipped-existing"}
                        else:
                            copied += 1
                            bytes_copied += n
                            outcomes[name] = {
                                "outcome": "copied",
                                "file_type": f.get("file_type"),
                                "size": f.get("file_size")}
                        consecutive_failures = 0
                    except (ZoomHTTPError, CopyError, PutError) as e:
                        errors[name] = str(e)
                        outcomes[name] = {"outcome": f"error: {e}"}
                        consecutive_failures += 1
                        if copied == 0 and consecutive_failures >= 5:
                            raise common.HarnessError(
                                "first 5 file copies all failed -- "
                                "systemic (SAS/firewall/token/API), "
                                f"aborting: last error: {e}")
                    if files_total % 25 == 0:
                        print(f"progress {files_total} files, "
                              f"copied={copied}, skipped={skipped}, "
                              f"placeholders={placeholders}, "
                              f"errors={len(errors)}, "
                              f"gb_copied={bytes_copied / 1e9:.1f}",
                              file=sys.stderr, flush=True)
            print(f"progress {frm[:7]}: meetings={meetings_total}, "
                  f"files={files_total}, copied={copied}, "
                  f"skipped={skipped}, errors={len(errors)}, "
                  f"gb_copied={bytes_copied / 1e9:.1f}",
                  file=sys.stderr, flush=True)
            if limit_hit:
                break

        # dry-run: placeholder requests so the plan of record shows the
        # exact shapes (server-side copy headers, create-only commit)
        if args.dry_run:
            ph = f"{prefix}/meetings/<uuid>/<start>_MP4_<fileid>.mp4"
            azure_put_json(cfg, sas, f"{prefix}/meetings/<uuid>/"
                           "metadata.json", {"placeholder": True}, True)
            put_blob_from_url(cfg, sas, ph, "<zoom-url>", "video/mp4", True)
            put_block_from_url(cfg, sas, ph, _block_id(0), "<zoom-url>",
                               0, args.block_size_mb * MIB - 1, True)
            put_block_list(cfg, sas, ph, [_block_id(0)], "video/mp4", True)

        # account context -> _meta (small JSON, guarded: failure is noted,
        # never fatal)
        meta_results: dict[str, str] = {}
        account: dict = {}
        if args.dry_run:
            print("DRY-RUN: GET https://api.zoom.us/v2/users/me (account "
                  "context -> _meta)")
            meta_results = {"account": "dry-run"}
        else:
            try:
                account = zoom_get(box, "/users/me")
                meta_results["account"] = "written"
            except (ZoomHTTPError, common.HarnessError) as e:
                meta_results["account"] = f"error: {e}"

        ts = common.ts_basic()
        index = {
            "slug": args.slug,
            "pulled_at": common.iso_now(),
            "from_date": args.from_date,
            "to_date": to_date,
            "months_listed": months_listed,
            "month_errors": month_errors,
            "meetings_total": meetings_total,
            "files_total": files_total,
            "copied": copied,
            "skipped_existing": skipped,
            "placeholders_skipped": placeholders,
            "file_errors": errors,
            "files": outcomes,
        }
        bytes_local += azure_put_json(
            cfg, sas, f"{prefix}/_meta/recordings-index-{ts}.json", index,
            args.dry_run)
        if account:
            bytes_local += azure_put_json(
                cfg, sas, f"{prefix}/_meta/account-{ts}.json", account,
                args.dry_run)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    result = {
        "ok": True,  # verify is the completeness authority, not pull
        "dest": f"{cfg['container']}/{prefix}",
        "date_range": f"{args.from_date} .. {to_date}",
        "months_listed": months_listed,
        "month_errors": month_errors,
        "meetings_total": meetings_total,
        "files_total": files_total,
        "copied": copied,
        "skipped_existing": skipped,
        "placeholders_skipped": placeholders,
        "file_errors": {"count": len(errors), "names": sorted(errors)[:50]},
        "bytes_copied_serverside": bytes_copied,
        "bytes_uploaded_local": bytes_local,
        "meta": meta_results,
        "sas_expiry": sas_expiry,
        "ip_rule": "added-and-removed" if we_added else "not-needed",
    }
    if limit_hit:
        result["note"] = (f"stopped at --meeting-limit "
                          f"{args.meeting_limit} (smoke run) -- the full "
                          "pull re-runs from the top and skips what landed")
    if errors or month_errors:
        result["resume_hint"] = ("re-run pull -- files that landed are "
                                 "skipped; only failed files and failed "
                                 "months re-copy")
    if placeholders:
        result.setdefault("note", "")
        result["note"] = (result["note"] + f" {placeholders} placeholder "
                          "row(s) skipped (recordings still processing on "
                          "Zoom's side) -- re-run in a day or two to pick "
                          "them up").strip()
    return result


def cmd_verify(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    creds = read_credentials(args.dry_run)
    box = TokenBox(*creds, dry_run=args.dry_run)
    prefix = args.dest_prefix
    to_date = _to_date(args)

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl -- the READ path
        blobs = azure_list_blobs(cfg, sas, prefix, args.dry_run)
        meetings: list[dict] = []
        if args.dry_run:
            print("DRY-RUN: GET https://api.zoom.us/v2/accounts/me/"
                  "recordings?from=<m>&to=<m>&page_size=300 (every month "
                  "window, materialized; Authorization: Bearer "
                  "<token-redacted>)")
        else:
            box.mint()
            for frm, to in month_windows(args.from_date, to_date):
                try:
                    meetings.extend(list_month(box, frm, to))
                except ZoomHTTPError as e:
                    if e.status == 400:
                        raise common.HarnessError(_activation_hint(e))
                    # unlike pull, a verify listing failure is FATAL --
                    # verify must never under-report expectations
                    raise common.HarnessError(
                        f"verify: month {frm[:7]} listing failed: {e} -- "
                        "re-run verify (it must see every month)")
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    expected: dict[str, int] = {}
    meta_missing = 0
    placeholders_api = 0
    for mtg in meetings:
        mid = safe_uuid(mtg)
        if f"{prefix}/meetings/{mid}/metadata.json" not in blobs:
            meta_missing += 1
        for f in mtg.get("recording_files") or []:
            if not should_pull_file(f):
                placeholders_api += 1
                continue
            expected[blob_name(prefix, mtg, f)] = int(f.get("file_size") or 0)

    missing = sorted(n for n in expected if n not in blobs)
    size_mismatch = [
        {"name": n, "actual": blobs[n]["size"], "declared": s}
        for n, s in sorted(expected.items())
        if n in blobs and s and blobs[n]["size"] != s]
    media_in_container = [
        n for n in blobs
        if n.startswith(f"{prefix}/meetings/")
        and not n.endswith("/metadata.json")]
    extra = sorted(n for n in media_in_container if n not in expected)
    meta_blobs = sum(1 for n in blobs if n.startswith(f"{prefix}/_meta/"))

    result = {
        "ok": not missing and not size_mismatch,
        "date_range": f"{args.from_date} .. {to_date}",
        "meetings_in_zoom": len(meetings),
        "files_expected": len(expected),
        "files_in_container": len(media_in_container),
        "missing_count": len(missing),
        "missing": missing[:50],
        "size_mismatch": size_mismatch[:50],
        "placeholders_api_side": placeholders_api,
        "metadata_missing": meta_missing,
        "extra": extra[:50],
        "extra_note": "blobs whose recording no longer appears in the API "
                      "-- EXPECTED under Zoom's retention auto-delete (the "
                      "export legitimately holds more than the live "
                      "account); informational, never delete",
        "meta_blobs": meta_blobs,
        "size_note": "byte-exact against Zoom's declared file_size (Zoom "
                     "declares no md5; the server-side copy commits "
                     "exactly what Zoom served)",
    }
    if missing or size_mismatch:
        result["hint"] = ("re-run pull -- resume skips what already "
                          "landed; only missing/failed files re-copy. "
                          "placeholders_api_side entries become pullable "
                          "once Zoom finishes processing them. Delete "
                          "nothing -- investigate first.")
    return result


# -- CLI ----------------------------------------------------------------------

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["plan", "probe", "pull", "verify"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--dest-prefix", default=DEFAULT_DEST_PREFIX,
                   help=f"prefix inside <slug>-raw (default "
                        f"{DEFAULT_DEST_PREFIX})")
    p.add_argument("--sas-days", type=int, default=2,
                   help="write-SAS expiry (default 2 -- multi-hour "
                        "server-side copies plus an overnight resume; lives "
                        "only in this process, a resume re-mints)")
    p.add_argument("--from-date", default=DEFAULT_FROM_DATE,
                   help=f"earliest recording date to walk (default "
                        f"{DEFAULT_FROM_DATE})")
    p.add_argument("--to-date", default=None,
                   help="latest recording date (default: today) -- "
                        "windowed re-runs and re-verifies")
    p.add_argument("--meeting-limit", type=int, default=None,
                   help="stop after N meetings (live smoke tests)")
    p.add_argument("--block-size-mb", type=int, default=256,
                   help="Put Block From URL block size in MiB (default 256)")
    p.add_argument("--single-shot-max-mb", type=int, default=1024,
                   help="files up to this many MiB use one Put Blob From "
                        "URL (default 1024; Azure hard cap 5000 MB)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    root = Path(args.root)
    fn = {"plan": cmd_plan, "probe": cmd_probe,
          "pull": cmd_pull, "verify": cmd_verify}[args.command]
    try:
        result = fn(root, args)
    except common.HarnessError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
