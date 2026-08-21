#!/usr/bin/env python3
"""Vimeo -> Azure ingest: pull a company's Vimeo library (source video files,
metadata, captions, folder/showcase structure) via the Vimeo API and land it
in <slug>-raw/vimeo-export/ -- WITHOUT the video bytes ever transiting this
laptop.

The harness's second VM-less ingest (after qwilr), but a different transport:
rclone has no Vimeo backend and ~400 GB of video is too big to proxy locally,
so the script resolves each Vimeo download link's redirect to a signed CDN
URL and drives Azure server-side copy (Put Blob From URL for small files,
Put Block From URL + Put Block List for large ones) -- the storage fabric
pulls from Vimeo's CDN directly. Laptop egress = API JSON + control calls +
tiny caption files. Standalone over common.py + phases (NOT built on
transfer_engine.py, which is rclone/VM-shaped), but keeps its CLI contract:
one JSON object on stdout, exit 0 ok / 1 hard error / 2 refusal.

Subcommands:
  plan <slug>     read-only, offline: what would be pulled and where it lands
  probe <slug>    token on stdin, NO Azure: does the account's plan expose
                  download links at all (video_files is plan-gated), which
                  API version works (3.4 vs the 3.2 fallback), does the CDN
                  honor Range (Put Block From URL needs it)
  pull <slug>     the transfer (token on stdin; resumable -- re-running
                  skips videos whose media blob already exists)
  verify <slug>   completeness: fresh /me/videos listing vs blobs under the
                  prefix, with byte-size checks against Vimeo's declared sizes

Hard rules (see the vimeo-azure-transfer SKILL.md):
- The Vimeo personal access token arrives on STDIN only -- never
  argv/env/files/logs. Scopes: public, private, video_files.
- Writes are create-only (If-None-Match: * on every commit, including
  Put Block List) under the dest prefix.
- Firewall via phases.ip_rule_ensure -- the laptop's external IP, same
  mechanism as sizing. allow-network is the VM path and does not apply.
- Vimeo download links expire (~24h) and the 302-resolved CDN URL dies even
  sooner (hours) -- resolved URLs are never cached; every retry re-resolves.
- Thumbnails are manifested, not downloaded; version history is not pulled;
  analytics/comments have no export (v1).
"""
from __future__ import annotations

import base64
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

VIMEO_BASE = "https://api.vimeo.com"
ACCEPT_34 = "application/vnd.vimeo.*+json;version=3.4"
ACCEPT_32 = "application/vnd.vimeo.*+json;version=3.2"
X_MS_VERSION = "2021-08-06"  # >= 2020-04-08 (Put Blob From URL), >= 2019-12-12 (4000 MiB blocks)
DEFAULT_DEST_PREFIX = "vimeo-export"
MIB = 1024 * 1024
MAX_BLOCKS = 50_000          # Azure's committed-block cap per blob
RERESOLVE_BUDGET = 20        # CDN URLs expire hourly; a multi-hour copy of one
                             # video legitimately needs several fresh resolves

# always pass fields= -- unfiltered Vimeo requests are rate-limited harder
VIDEO_FIELDS = ("uri,name,description,created_time,modified_time,duration,"
                "width,height,privacy,tags,parent_folder,download,files,play,"
                "pictures.sizes,metadata.connections.texttracks")
SAMPLE_FIELDS = "uri,name,download,files,play"

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


class VimeoHTTPError(Exception):
    """Non-auth Vimeo API failure after retries -- counted, not fatal."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class PutError(Exception):
    """Small-blob upload failure after retries -- counted, not fatal."""


class CopyError(Exception):
    """Server-side copy failure -- counted per video, not fatal. source_side
    means Azure could not fetch from the CDN URL (usually: it expired) --
    the caller re-resolves and retries the same block."""

    def __init__(self, msg: str, azure_code: str = "", source_status: str = ""):
        super().__init__(msg)
        self.azure_code = azure_code
        self.source_status = source_status

    @property
    def source_side(self) -> bool:
        return self.azure_code in ("CannotVerifyCopySource",
                                   "CopySourceNotFound") \
            or bool(self.source_status)


# -- Vimeo API ----------------------------------------------------------------

def vimeo_get(token: str, path: str, params: dict | None = None,
              accept: str = ACCEPT_34):
    """GET a Vimeo API path (or a ready-made paging.next path+query). 429
    honors Retry-After (else exponential, cap 32s); 5xx/network get linear
    backoff; 401/403 abort immediately -- a bad token cannot be retried
    away."""
    url = VIMEO_BASE + path
    if params:
        url += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
    last: Exception | None = None
    for attempt in range(6):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
        })
        try:
            with _http(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise common.HarnessError(
                    f"Vimeo API {e.code} on {path} -- token invalid, revoked "
                    "or missing scopes (need public+private+video_files); "
                    "get a fresh token (retrying cannot help)")
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
            raise VimeoHTTPError(e.code, f"HTTP {e.code} on {path}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    status = getattr(last, "code", 0) or 0
    raise VimeoHTTPError(status, f"gave up on {path} after retries: {last}")


def vimeo_list(token: str, path: str, params: dict | None = None,
               accept: str = ACCEPT_34, limit: int | None = None,
               label: str = "items") -> list[dict]:
    """Walk a Vimeo collection via paging.next (Vimeo's own recommendation
    over computing page numbers)."""
    items: list[dict] = []
    data = vimeo_get(token, path, dict(params or {}, per_page=100),
                     accept=accept)
    while True:
        items.extend(data.get("data") or [])
        print(f"progress listed {len(items)}"
              f"/{data.get('total', '?')} {label}",
              file=sys.stderr, flush=True)
        if limit and len(items) >= limit:
            return items[:limit]
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            return items
        data = vimeo_get(token, nxt, accept=accept)


def list_videos(token: str, video_limit: int | None = None,
                accept: str = ACCEPT_34) -> list[dict]:
    """All videos with the full field set. A listing failure is FATAL (same
    philosophy as the sizer: per-item errors are counted, but a broken
    enumeration must never masquerade as a complete run)."""
    try:
        return vimeo_list(token, "/me/videos",
                          {"fields": VIDEO_FIELDS, "sort": "date",
                           "direction": "asc"},
                          accept=accept, limit=video_limit, label="videos")
    except VimeoHTTPError as e:
        raise common.HarnessError(f"Vimeo video listing failed: {e}")


def _video_id(video: dict) -> str | None:
    uri = video.get("uri") or ""
    tail = uri.rsplit("/", 1)[-1]
    tail = tail.split(":")[0]  # unlisted-hash suffix is not part of the id
    return tail or None


_EXT_BY_MIME = {"video/mp4": ".mp4", "video/webm": ".webm",
                "video/quicktime": ".mov", "source": ".mp4"}


def _ext_of(entry: dict) -> str:
    path = urllib.parse.urlparse(entry.get("link") or "").path
    if "." in path.rsplit("/", 1)[-1]:
        ext = "." + path.rsplit(".", 1)[-1].lower()
        if 2 <= len(ext) <= 6 and ext[1:].isalnum():
            return ext
    return _EXT_BY_MIME.get(entry.get("type") or "", ".mp4")


def _norm_entry(entry: dict, kind: str) -> dict:
    return {
        "kind": kind,
        "quality": (entry.get("quality") or entry.get("rendition")
                    or entry.get("type") or "unknown"),
        "link": entry["link"],
        "size": entry.get("size") or 0,
        "md5": entry.get("md5"),
        "type": entry.get("type") if (entry.get("type") or "").startswith(
            "video/") else "video/mp4",
        "ext": _ext_of(entry),
    }


def choose_file(video: dict | None) -> dict | None:
    """Best file to archive. Precedence: download[] quality==source (the
    original upload) > largest download[] > play.progressive type==source
    (the documented original marker) > largest files[]/progressive
    transcode. None = the account/plan exposes no file links for this video
    (or the video has none)."""
    if not video:
        return None
    downloads = [d for d in (video.get("download") or []) if d.get("link")]
    src = [d for d in downloads if d.get("quality") == "source"]
    if src:
        return _norm_entry(src[0], "download")
    if downloads:
        return _norm_entry(max(downloads, key=lambda d: d.get("size") or 0),
                           "download")
    prog = [p for p in ((video.get("play") or {}).get("progressive") or [])
            if p.get("link")]
    psrc = [p for p in prog if p.get("type") == "source"]
    if psrc:
        return _norm_entry(psrc[0], "play")
    pool = [f for f in (video.get("files") or [])
            if f.get("link") and f.get("quality") != "hls"] + prog
    if pool:
        return _norm_entry(max(pool, key=lambda f: f.get("size") or 0),
                           "files")
    return None


def _candidate_sizes(video: dict) -> set[int]:
    """Every byte size any of the video's file entries declares -- verify
    accepts a landed blob matching ANY of them (pull may have picked a
    different quality than today's choose_file would)."""
    sizes: set[int] = set()
    for entry in ((video.get("download") or [])
                  + (video.get("files") or [])
                  + ((video.get("play") or {}).get("progressive") or [])):
        if entry.get("size"):
            sizes.add(int(entry["size"]))
    return sizes


def safe_filename(video: dict, chosen: dict) -> str:
    base = (video.get("name") or "").strip()
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", base)
    base = re.sub(r"\s+", " ", base).strip().strip(".")
    return (base[:150] or "video") + chosen["ext"]


def resolve_cdn_url(link: str) -> str:
    """Follow the download link's redirect chain manually and return the
    final signed CDN URL. Azure copy-from-URL never follows redirects, so
    resolution happens here. The result expires within HOURS -- never cache
    it; on any source-side failure call again (resolve_fresh)."""
    url = link
    for _hop in range(6):
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        try:
            resp = _http_nr(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise VimeoHTTPError(e.code, "redirect without Location")
                url = urllib.parse.urljoin(url, loc)
                continue
            if e.code == 429:
                _sleep(5)
                continue
            raise VimeoHTTPError(
                e.code, f"resolving download link: HTTP {e.code} "
                        "(expired link? re-fetch the video's file entries)")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise VimeoHTTPError(0, f"resolving download link: {e}")
        resp.close()
        return url
    raise VimeoHTTPError(0, "redirect loop resolving download link")


def resolve_fresh(token: str, video_id: str, chosen: dict) -> str:
    """Two-stage re-resolve for an expired CDN URL: re-follow the stored API
    link; if that link itself has expired (~24h), re-fetch the video's file
    entries and resolve the equivalent one. Updates chosen['link'] so later
    retries start from the fresh link."""
    try:
        return resolve_cdn_url(chosen["link"])
    except VimeoHTTPError:
        video = vimeo_get(token, f"/videos/{video_id}",
                          {"fields": "uri,download,files,play"})
        fresh = choose_file(video)
        if not fresh:
            raise CopyError(f"re-resolve: video {video_id} no longer offers "
                            "file links")
        chosen["link"] = fresh["link"]
        return resolve_cdn_url(fresh["link"])


def probe_range(url: str) -> bool:
    """Put Block From URL requires the source to honor Range requests."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with _http(req, timeout=60) as r:
            return getattr(r, "status", None) == 206
    except Exception:
        return False


def md5_hex_to_b64(hex_str: str | None) -> str | None:
    """Vimeo reports md5 as hex; Azure's md5 headers want base64."""
    if not hex_str:
        return None
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        return None
    return base64.b64encode(raw).decode("ascii") if len(raw) == 16 else None


def _download_small(url: str) -> bytes:
    """Fetch a SMALL file (caption tracks -- KBs) through the laptop. Video
    bytes never take this path."""
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url)
        try:
            with _http(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code >= 500 or e.code == 429:
                _sleep(1 + attempt)
                continue
            raise VimeoHTTPError(e.code, f"caption download: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise VimeoHTTPError(0, f"caption download failed after retries: {last}")


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
    expiry is 2 days -- longer than qwilr's 1 because a ~400 GB server-side
    copy is realistically multi-hour and an overnight resume must not
    straddle the expiry mid-block-loop. Still short, still self-lapsing,
    still held only in this process's memory; a resume re-mints."""
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
    """One marker-paginated listing of the dest prefix -> {name: {size,
    md5}}. Serves as both the resume seed and verify's ground truth
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
                    "md5": ((props.findtext("Content-MD5") or None)
                            if props is not None else None),
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
    """PUT one SMALL blob (JSON/VTT -- KBs), create-only. Returns bytes
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
    x 256 MiB = ~13 TiB ceiling -- orders of magnitude clear of any video."""
    n_blocks = (size + block_size - 1) // block_size
    if n_blocks > MAX_BLOCKS:
        raise CopyError(f"file needs {n_blocks} blocks (> {MAX_BLOCKS}); "
                        "raise --block-size-mb")
    return [(_block_id(i), start, min(start + block_size, size) - 1)
            for i, start in enumerate(range(0, size, block_size))]


def put_blob_from_url(cfg: dict, sas: str, name: str, src_url: str,
                      content_type: str, md5_b64: str | None,
                      dry_run: bool) -> int:
    """Single-request server-side copy (files <= --single-shot-max-mb).
    Azure fetches the bytes from the CDN itself; with
    x-ms-source-content-md5 set it also validates them against Vimeo's
    declared md5 (Md5Mismatch = real integrity failure) -- genuine
    end-to-end verification. Returns bytes-ish (1 on success when size
    unknown); 0 = blob already existed (409). Dest-side 403/5xx retried
    here; a source-side failure raises CopyError immediately so the caller
    can re-resolve the expired CDN URL."""
    headers = {
        "x-ms-version": X_MS_VERSION,
        "x-ms-blob-type": "BlockBlob",
        "x-ms-copy-source": src_url,
        "x-ms-blob-content-type": content_type,
        "If-None-Match": "*",
    }
    if md5_b64:
        headers["x-ms-source-content-md5"] = md5_b64
        headers["x-ms-blob-content-md5"] = md5_b64
    if dry_run:
        print(f"DRY-RUN: PUT {_blob_url(cfg, name)}?<sas-redacted>  "
              f"(x-ms-copy-source: <cdn-url>, x-ms-blob-type: BlockBlob, "
              f"If-None-Match: *, x-ms-source-content-md5: <md5>)")
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
            if err.source_side or code == "Md5Mismatch":
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
    """Stage one block server-side (Azure fetches bytes start..end from the
    CDN). Staged blocks are invisible until Put Block List and self-expire
    in ~7 days if never committed. Dest-side 403/5xx retried here; a
    source-side failure (expired CDN URL surfaces as CannotVerifyCopySource)
    raises immediately so the caller re-resolves and retries the SAME block
    id."""
    if dry_run:
        print(f"DRY-RUN: PUT {_blob_url(cfg, name)}"
              f"?comp=block&blockid={block_id}&<sas-redacted>  "
              f"(x-ms-copy-source: <cdn-url>, "
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
                   content_type: str, md5_b64: str | None,
                   dry_run: bool) -> int:
    """Commit the staged blocks -- the moment the blob comes into existence,
    which is why If-None-Match: * rides HERE (create-only invariant).
    Returns 1 committed, 0 = blob already existed (409, benign)."""
    body = ("<?xml version='1.0' encoding='utf-8'?><BlockList>"
            + "".join(f"<Latest>{bid}</Latest>" for bid in block_ids)
            + "</BlockList>").encode("utf-8")
    if dry_run:
        print(f"DRY-RUN: PUT {_blob_url(cfg, name)}"
              f"?comp=blocklist&<sas-redacted>  "
              f"(If-None-Match: *, x-ms-blob-content-md5: <md5>, "
              f"{len(block_ids)} blocks)")
        return 1
    url = _blob_url(cfg, name) + "?comp=blocklist&" + sas
    headers = {
        "x-ms-version": X_MS_VERSION,
        "Content-Type": "application/xml",
        "x-ms-blob-content-type": content_type,
        "If-None-Match": "*",
    }
    if md5_b64:
        headers["x-ms-blob-content-md5"] = md5_b64
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


def copy_video_to_blob(cfg: dict, sas: str, token: str, video_id: str,
                       name: str, chosen: dict, args) -> int:
    """Server-side copy of one video file. Small files (or unknown size):
    one Put Blob From URL with source-md5 validation. Large files: Put Block
    From URL per block + Put Block List. Expired CDN URLs (source_side
    CopyError) are re-resolved in place -- an hourly-expiring URL over a
    multi-hour copy is NORMAL, not an error -- up to RERESOLVE_BUDGET times.
    Returns bytes copied server-side (0 = blob already existed)."""
    size = int(chosen.get("size") or 0)
    md5_b64 = md5_hex_to_b64(chosen.get("md5"))
    ct = chosen.get("type") or "video/mp4"
    src = "<cdn-url>" if args.dry_run else resolve_cdn_url(chosen["link"])
    single_max = args.single_shot_max_mb * MIB
    reresolves = 0

    def _reresolve(e: CopyError) -> str:
        nonlocal reresolves
        if args.dry_run or not e.source_side or reresolves >= RERESOLVE_BUDGET:
            raise e
        reresolves += 1
        print(f"progress   {video_id}: CDN URL expired, re-resolving "
              f"({reresolves}/{RERESOLVE_BUDGET})", file=sys.stderr,
              flush=True)
        return resolve_fresh(token, video_id, chosen)

    if size <= single_max:  # includes size-unknown (0): single-shot caps 5000 MB
        while True:
            try:
                got = put_blob_from_url(cfg, sas, name, src, ct, md5_b64,
                                        args.dry_run)
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
            print(f"progress   {video_id}: block {i}/{len(blocks)}",
                  file=sys.stderr, flush=True)
    committed = put_block_list(cfg, sas, name, [b[0] for b in blocks], ct,
                               md5_b64, args.dry_run)
    return size if committed else 0


# -- token --------------------------------------------------------------------

def read_token(dry_run: bool) -> str:
    """The Vimeo personal access token arrives on stdin ONLY (heredoc) --
    argv is world-readable via ps, env leaks into child processes, files
    persist."""
    data = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    if data:
        return data
    if dry_run:
        return "<token>"
    raise common.HarnessError(
        "no Vimeo token on stdin -- pipe it: pull <slug> <<'EOF' ... EOF")


def _tt_safe(track: dict) -> str:
    lang = (track.get("language") or "und").strip() or "und"
    name = (track.get("name") or "").strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\s]+', "-", name).strip("-.")
    tid = (track.get("uri") or "").rsplit("/", 1)[-1] or "track"
    stem = f"{lang}-{name}" if name else f"{lang}-{tid}"
    return stem[:100] + ".vtt"


def _index_by_video(names, prefix: str) -> dict[str, set[str]]:
    """{video_id: {blob-name tails under videos/<id>/}} from a listing."""
    out: dict[str, set[str]] = {}
    p = f"{prefix}/videos/"
    for n in names:
        if n.startswith(p):
            vid, _, tail = n[len(p):].partition("/")
            if vid and tail:
                out.setdefault(vid, set()).add(tail)
    return out


def _media_tails(tails: set[str]) -> list[str]:
    return [t for t in tails
            if t != "metadata.json" and not t.startswith("texttracks/")]


# -- subcommands --------------------------------------------------------------

def cmd_plan(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    declared = None
    try:
        expected = common.load_expected(root, args.slug)
        declared = (expected.get("services", {}).get("vimeo") or {}).get("bytes")
    except FileNotFoundError:
        pass
    return {
        "ok": True,
        "slug": args.slug,
        "mode": "local orchestration, Azure server-side copy (no VM, no "
                "rclone; video bytes never transit this laptop)",
        "storage_account": cfg["storage_account"],
        "container": cfg["container"],
        "dest": f"{cfg['container']}/{args.dest_prefix}",
        "declared_vimeo_bytes": declared,
        "source": "Vimeo API (api.vimeo.com); personal access token with "
                  "public+private+video_files scopes on stdin at probe/pull "
                  "time. video_files is PLAN-GATED (Standard/Pro+): run "
                  "probe before promising anything.",
        "sas": "container SAS, racwl, minted at pull time",
        "sas_days": args.sas_days,
        "firewall": "per-run IP rule for this laptop's public IP, added and "
                    "removed by pull itself (phases.ip_rule_ensure)",
        "will_write": [
            f"{args.dest_prefix}/videos/<id>/<name>.<ext> (source quality, "
            "else best available; Azure pulls it from Vimeo's CDN "
            "server-side)",
            f"{args.dest_prefix}/videos/<id>/metadata.json",
            f"{args.dest_prefix}/videos/<id>/texttracks/<lang>-<name>.vtt",
            f"{args.dest_prefix}/_meta/{{videos-index,folders,showcases,"
            "account,thumbnails-manifest}-<ts>.json",
        ],
        "note": "writes are create-only (If-None-Match: *); re-running pull "
                "skips videos already landed. Thumbnails are manifested, "
                "not downloaded; no analytics/comments/version-history "
                "export exists.",
    }


def cmd_probe(root: Path, args) -> dict:
    """Day-one gate, NO Azure: is this account's plan download-capable at
    all, and under which API version?"""
    load_cfg(root, args.slug)  # onboarding check only; no network from it
    token = read_token(args.dry_run)
    if args.dry_run:
        print("DRY-RUN: GET https://api.vimeo.com/me?fields=name,account,"
              "upload_quota (Authorization: Bearer <token-redacted>)")
        print(f"DRY-RUN: GET https://api.vimeo.com/me/videos?per_page=1"
              f"&fields={SAMPLE_FIELDS} (versions 3.4 then 3.2)")
        print("DRY-RUN: GET <download link> (Range: bytes=0-0, follow "
              "redirects to the CDN)")
        return {"ok": True, "note": "dry-run: no requests made"}

    try:
        me = vimeo_get(token, "/me",
                       {"fields": "name,link,account,upload_quota"})
        data = vimeo_get(token, "/me/videos",
                         {"per_page": 1, "fields": SAMPLE_FIELDS})
    except VimeoHTTPError as e:
        raise common.HarnessError(f"Vimeo probe failed: {e}")
    total = data.get("total") or 0
    sample = (data.get("data") or [None])[0]
    api_version = "3.4"
    if sample and not (sample.get("download") or sample.get("files")
                       or (sample.get("play") or {}).get("progressive")):
        # vimeo.php#194: some accounts only expose file arrays under 3.2
        try:
            d32 = vimeo_get(token, "/me/videos",
                            {"per_page": 1, "fields": SAMPLE_FIELDS},
                            accept=ACCEPT_32)
            s32 = (d32.get("data") or [None])[0]
            if s32 and (s32.get("download") or s32.get("files")
                        or (s32.get("play") or {}).get("progressive")):
                sample, api_version = s32, "3.2"
        except VimeoHTTPError:
            pass

    chosen = choose_file(sample)
    range_probe = None
    if chosen:
        try:
            cdn = resolve_cdn_url(chosen["link"])
            range_probe = "206-ok" if probe_range(cdn) else "no-range-support"
        except VimeoHTTPError as e:
            range_probe = f"resolve-failed: {e}"

    quota = (me.get("upload_quota") or {}).get("lifetime") or {}
    capable = bool(chosen) if total else None
    result = {
        "ok": bool(chosen) or total == 0,
        "account_name": me.get("name"),
        "account_tier": me.get("account"),
        "quota_used_bytes": quota.get("used"),
        "videos_total": total,
        "api_version_used": api_version,
        "download_capable": capable,
        "sample": None if not sample else {
            "uri": sample.get("uri"),
            "has_download": bool(sample.get("download")),
            "has_files": bool(sample.get("files")),
            "has_progressive": bool(
                (sample.get("play") or {}).get("progressive")),
            "chosen_kind": chosen and chosen["kind"],
            "chosen_quality": chosen and chosen["quality"],
            "source_available": bool(chosen and chosen["quality"] == "source"),
        },
        "range_probe": range_probe,
    }
    if total and not chosen:
        result["hint"] = (
            "no download/files/play links on the sample video under 3.4 or "
            "3.2 -- either the token lacks the video_files scope or the "
            "Vimeo plan doesn't include file access (needs Standard/"
            "Advanced/Pro+). This is a plan/scope conversation with the "
            "client, not a retry.")
    if range_probe == "no-range-support":
        result["hint"] = (
            "CDN refused a Range request -- files above --single-shot-max-mb "
            "cannot be block-copied. Raise --single-shot-max-mb (hard cap "
            "5000 MB) or investigate before the full pull.")
    if api_version == "3.2":
        result["note"] = "pass --api-version 3.2 to pull and verify"
    return result


def cmd_pull(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    token = read_token(args.dry_run)
    prefix = args.dest_prefix
    accept = ACCEPT_32 if args.api_version == "3.2" else ACCEPT_34

    # Validate the token BEFORE touching the firewall -- fail fast rather
    # than after the 60s propagation sleep.
    if args.dry_run:
        print("DRY-RUN: GET https://api.vimeo.com/me?fields=uri "
              "(Authorization: Bearer <token-redacted>)")
    else:
        try:
            vimeo_get(token, "/me", {"fields": "uri"}, accept=accept)
        except VimeoHTTPError as e:
            raise common.HarnessError(f"Vimeo token validation failed: {e}")

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas, sas_expiry = mint_write_sas(cfg, args.sas_days, args.dry_run)
        existing = azure_list_blobs(cfg, sas, prefix, args.dry_run)
        landed = _index_by_video(existing, prefix)

        if args.dry_run:
            print(f"DRY-RUN: GET https://api.vimeo.com/me/videos"
                  f"?per_page=100&fields={VIDEO_FIELDS} "
                  "(paging.next loop, Authorization: Bearer <token-redacted>)")
            print("DRY-RUN: GET <download link> -> 302 -> signed CDN URL "
                  "(re-resolved on every retry; never cached)")
            videos = []
        else:
            videos = list_videos(token, args.video_limit, accept=accept)

        copied = skipped = 0
        bytes_copied = bytes_local = 0
        errors: dict[str, str] = {}
        no_file: list[str] = []
        quality_fallbacks: list[str] = []
        tt_written = tt_errors = 0
        outcomes: dict[str, dict] = {}
        thumbs: dict[str, list[str]] = {}
        consecutive_failures = 0

        for i, video in enumerate(videos, 1):
            vid = _video_id(video)
            if vid is None:
                errors["(no-id)"] = (f"video without a uri: "
                                     f"{str(video)[:120]}")
                continue
            tails = landed.get(vid, set())

            urls = [s.get("link") for s in
                    ((video.get("pictures") or {}).get("sizes") or [])
                    if s.get("link")]
            if urls:
                thumbs[vid] = urls

            try:
                meta_name = f"{prefix}/videos/{vid}/metadata.json"
                if "metadata.json" not in tails:
                    bytes_local += azure_put_json(cfg, sas, meta_name, video,
                                                  args.dry_run)

                chosen = choose_file(video)
                if chosen is None:
                    no_file.append(vid)
                    outcomes[vid] = {"outcome": "no-file"}
                elif _media_tails(tails):
                    skipped += 1
                    outcomes[vid] = {"outcome": "skipped-existing"}
                else:
                    fname = safe_filename(video, chosen)
                    n = copy_video_to_blob(
                        cfg, sas, token, vid,
                        f"{prefix}/videos/{vid}/{fname}", chosen, args)
                    if n == 0:
                        skipped += 1
                        outcomes[vid] = {"outcome": "skipped-existing"}
                    else:
                        copied += 1
                        bytes_copied += n
                        outcomes[vid] = {"outcome": "copied", "file": fname,
                                         "kind": chosen["kind"],
                                         "quality": chosen["quality"],
                                         "size": chosen["size"],
                                         "md5": chosen["md5"]}
                        if chosen["quality"] != "source":
                            quality_fallbacks.append(vid)

                tt_total = ((((video.get("metadata") or {}).get("connections")
                              or {}).get("texttracks") or {}).get("total")
                            or 0)
                if tt_total and not args.dry_run:
                    try:
                        tracks = vimeo_get(token, f"/videos/{vid}/texttracks",
                                           accept=accept).get("data") or []
                        for track in tracks:
                            if not track.get("link"):
                                continue
                            tt_tail = f"texttracks/{_tt_safe(track)}"
                            tt_name = f"{prefix}/videos/{vid}/{tt_tail}"
                            if tt_tail in tails:
                                continue
                            body = _download_small(track["link"])
                            if azure_put_bytes(cfg, sas, tt_name, body,
                                               "text/vtt", args.dry_run):
                                tt_written += 1
                            bytes_local += len(body)
                    except (VimeoHTTPError, PutError):
                        tt_errors += 1

                consecutive_failures = 0
            except (VimeoHTTPError, CopyError, PutError) as e:
                errors[vid] = str(e)
                outcomes[vid] = {"outcome": f"error: {e}"}
                consecutive_failures += 1
                if copied == 0 and consecutive_failures >= 5:
                    raise common.HarnessError(
                        "first 5 video copies all failed -- systemic "
                        f"(SAS/firewall/CDN/API), aborting: last error: {e}")
            print(f"progress {i}/{len(videos)} videos, copied={copied}, "
                  f"skipped={skipped}, no_file={len(no_file)}, "
                  f"errors={len(errors)}, "
                  f"gb_copied={bytes_copied / 1e9:.1f}",
                  file=sys.stderr, flush=True)

        # dry-run: placeholder requests so the plan of record shows the
        # exact shapes (server-side copy headers, create-only commit)
        if args.dry_run:
            ph = f"{prefix}/videos/<id>/<name>.mp4"
            azure_put_json(cfg, sas, f"{prefix}/videos/<id>/metadata.json",
                           {"placeholder": True}, True)
            put_blob_from_url(cfg, sas, ph, "<cdn-url>", "video/mp4",
                              None, True)
            put_block_from_url(cfg, sas, ph, _block_id(0), "<cdn-url>",
                               0, args.block_size_mb * MIB - 1, True)
            put_block_list(cfg, sas, ph, [_block_id(0)], "video/mp4",
                           None, True)

        # account-level structure -> _meta (small JSON, guarded like
        # qwilr's account endpoints: failure is noted, never fatal)
        meta_results: dict[str, str] = {}
        folders: list[dict] = []
        showcases: list[dict] = []
        account: dict = {}
        if args.dry_run:
            print("DRY-RUN: GET https://api.vimeo.com/me/projects + "
                  "/projects/<id>/videos (folder map)")
            print("DRY-RUN: GET https://api.vimeo.com/me/albums (showcases)")
            print("DRY-RUN: GET https://api.vimeo.com/me?fields=...,"
                  "upload_quota (account)")
            meta_results = {k: "dry-run" for k in
                            ("folders", "showcases", "account")}
        else:
            try:
                projects = vimeo_list(
                    token, "/me/projects",
                    {"fields": "uri,name,created_time,modified_time"},
                    accept=accept, label="folders")
                for proj in projects:
                    pid = (proj.get("uri") or "").rsplit("/", 1)[-1]
                    try:
                        vids = vimeo_list(token, f"/projects/{pid}/videos",
                                          {"fields": "uri"}, accept=accept,
                                          label=f"folder {pid} videos")
                        proj["video_ids"] = [v for v in
                                             (_video_id(x) for x in vids) if v]
                    except VimeoHTTPError as e:
                        proj["video_ids_error"] = str(e)
                    folders.append(proj)
                meta_results["folders"] = f"{len(folders)} folders"
            except VimeoHTTPError as e:
                meta_results["folders"] = f"error: {e}"
            try:
                showcases = vimeo_list(
                    token, "/me/albums",
                    {"fields": "uri,name,description,created_time,"
                               "modified_time"},
                    accept=accept, label="showcases")
                meta_results["showcases"] = f"{len(showcases)} showcases"
            except VimeoHTTPError as e:
                meta_results["showcases"] = f"error: {e}"
            try:
                account = vimeo_get(token, "/me",
                                    {"fields": "uri,name,link,account,"
                                               "created_time,upload_quota"},
                                    accept=accept)
                meta_results["account"] = "written"
            except VimeoHTTPError as e:
                meta_results["account"] = f"error: {e}"

        ts = common.ts_basic()
        index = {
            "slug": args.slug,
            "pulled_at": common.iso_now(),
            "api_version": args.api_version,
            "videos_total": len(videos),
            "copied": copied,
            "skipped_existing": skipped,
            "no_file": no_file,
            "quality_fallbacks": quality_fallbacks,
            "video_errors": errors,
            "videos": outcomes,
        }
        bytes_local += azure_put_json(
            cfg, sas, f"{prefix}/_meta/videos-index-{ts}.json", index,
            args.dry_run)
        if folders or args.dry_run:
            bytes_local += azure_put_json(
                cfg, sas, f"{prefix}/_meta/folders-{ts}.json", folders,
                args.dry_run)
        if showcases:
            bytes_local += azure_put_json(
                cfg, sas, f"{prefix}/_meta/showcases-{ts}.json", showcases,
                args.dry_run)
        if account:
            bytes_local += azure_put_json(
                cfg, sas, f"{prefix}/_meta/account-{ts}.json", account,
                args.dry_run)
        if thumbs:
            bytes_local += azure_put_json(
                cfg, sas, f"{prefix}/_meta/thumbnails-manifest-{ts}.json",
                thumbs, args.dry_run)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    result = {
        "ok": True,  # verify is the completeness authority, not pull
        "dest": f"{cfg['container']}/{prefix}",
        "videos_total": len(videos),
        "copied": copied,
        "skipped_existing": skipped,
        "no_file": {"count": len(no_file), "ids": no_file[:50]},
        "video_errors": {"count": len(errors), "ids": sorted(errors)[:50]},
        "quality_fallbacks": {"count": len(quality_fallbacks),
                              "ids": quality_fallbacks[:50]},
        "texttracks": {"written": tt_written, "errors": tt_errors},
        "bytes_copied_serverside": bytes_copied,
        "bytes_uploaded_local": bytes_local,
        "meta": meta_results,
        "sas_expiry": sas_expiry,
        "ip_rule": "added-and-removed" if we_added else "not-needed",
    }
    if errors:
        result["resume_hint"] = ("re-run pull -- videos that landed are "
                                 "skipped; only the failed ones re-copy")
    if quality_fallbacks:
        result["note"] = ("some videos had no source-quality file; the best "
                          "available transcode was archived instead -- "
                          "landed bytes will undershoot declared bytes")
    if thumbs:
        result.setdefault("note", "")
        result["note"] = (result["note"] + " thumbnails were manifested "
                          "(_meta/thumbnails-manifest), NOT "
                          "downloaded").strip()
    return result


def cmd_verify(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    token = read_token(args.dry_run)
    prefix = args.dest_prefix
    accept = ACCEPT_32 if args.api_version == "3.2" else ACCEPT_34

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl -- the READ path
        blobs = azure_list_blobs(cfg, sas, prefix, args.dry_run)
        if args.dry_run:
            print(f"DRY-RUN: GET https://api.vimeo.com/me/videos"
                  f"?per_page=100&fields={VIDEO_FIELDS} "
                  "(paging.next loop, Authorization: Bearer <token-redacted>)")
            videos = []
        else:
            videos = list_videos(token, None, accept=accept)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    by_vid = _index_by_video(blobs, prefix)
    missing: list[str] = []
    size_mismatch: list[dict] = []
    no_file_api: list[str] = []
    metadata_missing = 0
    md5_matches = md5_checked = 0
    tt_expected = 0
    api_ids: set[str] = set()

    for video in videos:
        vid = _video_id(video)
        if not vid:
            continue
        api_ids.add(vid)
        tt_expected += ((((video.get("metadata") or {}).get("connections")
                          or {}).get("texttracks") or {}).get("total") or 0)
        tails = by_vid.get(vid, set())
        if "metadata.json" not in tails:
            metadata_missing += 1
        chosen = choose_file(video)
        media = _media_tails(tails)
        if chosen is None:
            no_file_api.append(vid)  # nothing to expect -- informational
            continue
        if not media:
            missing.append(vid)
            continue
        blob = blobs[f"{prefix}/videos/{vid}/{media[0]}"]
        candidates = _candidate_sizes(video)
        if candidates and blob["size"] not in candidates:
            size_mismatch.append({"id": vid, "actual": blob["size"],
                                  "expected_one_of": sorted(candidates)[:5]})
        want_md5 = md5_hex_to_b64(chosen.get("md5"))
        if want_md5 and blob.get("md5"):
            md5_checked += 1
            if blob["md5"] == want_md5:
                md5_matches += 1

    tt_present = sum(1 for tails in by_vid.values()
                     for t in tails if t.startswith("texttracks/"))
    extra = sorted(set(by_vid) - api_ids)
    meta_blobs = sum(1 for n in blobs if n.startswith(f"{prefix}/_meta/"))

    result = {
        "ok": not missing and not size_mismatch,
        "videos_in_vimeo": len(api_ids),
        "videos_in_container": len(by_vid),
        "missing_count": len(missing),
        "missing": missing[:50],
        "size_mismatch": size_mismatch[:50],
        "no_file_api_side": {"count": len(no_file_api),
                             "ids": no_file_api[:50]},
        "metadata_missing": metadata_missing,
        "extra": extra[:50],  # informational: deleted in Vimeo since pull
        "texttracks": {"expected": tt_expected, "present": tt_present},
        "md5": {"checked": md5_checked, "matched": md5_matches,
                "note": "informational -- the property was set from Vimeo's "
                        "value at pull time; size is the load-bearing check "
                        "(and <=1 GiB files were source-md5-validated by "
                        "Azure at copy time)"},
        "meta_blobs": meta_blobs,
    }
    if missing or size_mismatch:
        result["hint"] = ("re-run pull -- resume skips what already landed; "
                          "only missing/failed videos re-copy. A "
                          "size_mismatch usually means an interrupted "
                          "single-shot copy or the file was replaced in "
                          "Vimeo; delete nothing -- investigate first.")
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
    p.add_argument("--video-limit", type=int, default=None,
                   help="pull only the first N videos (live smoke tests)")
    p.add_argument("--block-size-mb", type=int, default=256,
                   help="Put Block From URL block size in MiB (default 256)")
    p.add_argument("--single-shot-max-mb", type=int, default=1024,
                   help="files up to this many MiB use one Put Blob From URL "
                        "with source-md5 validation (default 1024; Azure "
                        "hard cap 5000 MB)")
    p.add_argument("--api-version", choices=["3.4", "3.2"], default="3.4",
                   help="Vimeo API version (probe reports if 3.2 is needed)")
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
