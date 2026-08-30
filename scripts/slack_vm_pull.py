#!/usr/bin/env python3
"""VM-side puller for the Slack export file ingest (pushed by slack_transfer.py).

A Business+ / Enterprise Slack compliance export is a complete transcript and
ZERO file bytes: every attachment, image, video, canvas and huddle transcript
survives only as an authenticated `files.slack.com` link inside the JSON. On a
real sample export (2.35 GB of JSON, 123k day files, 1.1M messages) that is
63,636 unique files, of which 62,056 are Slack-hosted and worth 81.1 GB --
roughly 34x the export itself. This script
closes that gap: it reads the export the client already pushed into
`<slug>-raw`, builds a ledger that links every file back to the conversation
and message that referenced it, and copies the bytes into the same container.

Transport is Azure SERVER-SIDE COPY (the vimeo/zoom transport, not the
github/zoho/figma stage-then-azcopy one): this VM resolves each Slack URL's
redirect chain itself -- Azure never follows 3xx -- and then hands the final
signed URL to Put Blob From URL (or Put Block From URL + Put Block List above
SINGLE_SHOT_MAX). The storage fabric pulls from Slack directly, so file bytes
never touch this VM's disk and `If-None-Match: *` makes create-only
API-ENFORCED rather than a client-side flag. A per-file stream-through fallback
covers whatever Azure cannot copy, and is recorded as `transport: streamed`.

Four facts measured on the sample export drive specific code here:

1. The export CARRIES ITS OWN AUTH. Every file URL embeds one workspace-wide
   token: `?token=xoxe-...` on files-pri/ URLs and `?t=xoxe-...` on the
   files-tmb/ transcoded-video URLs. In the normal case there is no client
   credential at all -- a first for this family. SLACK_TOKEN is an OVERRIDE for
   the case where the links were stripped or the export's token has expired.
2. Some URLs carry NO token: hosted file objects nested in
   `attachments[].files[]` (bot/unfurl messages). Rule -- use a URL verbatim
   when it already carries a token, otherwise append the harvested one.
3. ZIP member names are cp437-mangled: Slack writes UTF-8 names with the UTF-8
   general-purpose flag bit UNSET, so zipfile mis-decodes them (7 of 3,352
   conversation dirs on the sample). `zip_member_name` re-decodes them.
4. Canvases, Lists and Huddle transcripts are INVISIBLE to a message walk --
   they live in root canvases.json / lists.json / huddle_transcripts.json with
   their own url_private_download plus a *_history_download link.

Scope boundary, stated out loud: `mode: external` files (gdrive) have no bytes
at Slack at all -- Slack stores a link and metadata only, so no Slack token can
fetch them. They are recorded in `_meta/external-references.jsonl` for the
gdrive ingest to reconcile against, never fetched. `tombstone` and
`hidden_by_limit` files have no URL at all and go to `_meta/unavailable.jsonl`.

Fully env-driven -- no argv at all (slack_transfer.py launches it with zero
arguments inside tmux, both env files pre-sourced).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

X_MS_VERSION = "2021-08-06"
DEFAULT_DEST_PREFIX = "slack-export-files"

# Pacing: it is AZURE's fabric that hits Slack on the server-side-copy path,
# so our only lever is how fast we issue copy requests. Conservative by
# default; --rps-files / --copy-workers on the CLI override per run.
DEFAULT_RPS_FILES = 12.0
DEFAULT_COPY_WORKERS = 8

SHARD_SIZE = 2000              # files per resumable unit
SINGLE_SHOT_MAX = 256 * 1024 * 1024
BLOCK_SIZE = 256 * 1024 * 1024
META_BLOCK_SIZE = 64 * 1024 * 1024   # ledger uploads above SINGLE_PUT_MAX
SINGLE_PUT_MAX = 200 * 1024 * 1024
STREAM_FALLBACK_MAX = 256 * 1024 * 1024   # never buffer more than this in RAM
STREAM_FALLBACK_SLOTS = 2  # ...and never more than this many at once
RESOLVE_HOPS = 6
API_RETRIES = 4
MAX_SLEEPS_PER_CALL = 50       # a 429 never counts against API_RETRIES
AUTH_FATAL_THRESHOLD = 10      # first N files all source-401/403 => token dead
RESOLVE_BUDGET = 3             # re-resolves of an expired signed URL per object
FAILED_SAMPLE_CAP = 200        # manifest carries a sample, not a novel

# The two members that identify a directory (or archive) as a Slack export.
EXPORT_SIGNATURE = ("channels.json", "users.json")
# Conversation metadata files -> the conversation "kind" they describe.
CONV_META = {"channels.json": "public", "groups.json": "private",
             "mpims.json": "mpim", "file_conversations.json": "file_conversation"}
# Root asset files -> the asset kind. Invisible to a message walk (fact 4).
ASSET_JSONS = {"canvases.json": "canvas", "lists.json": "list",
               "huddle_transcripts.json": "huddle_transcript"}
# The history link each asset kind carries alongside url_private_download.
ASSET_HISTORY = {"canvases.json": "canvas_history_download",
                 "lists.json": "list_history_download"}
ROOT_JSONS = tuple(CONV_META) + ("dms.json", "users.json",
                                 ".slack-manifest.json",
                                 "integration_logs.json") + tuple(ASSET_JSONS)

# Keys whose value is the ORIGINAL file, in preference order.
ORIGINAL_URL_KEYS = ("url_private_download", "url_private")
# Keys that are never a rendition even though they are files.slack.com URLs.
NOT_RENDITIONS = set(ORIGINAL_URL_KEYS) | {
    "permalink", "permalink_public", "canvas_history_download",
    "list_history_download"}
# Query parameters Slack uses to carry the export token (fact 1).
TOKEN_PARAMS = ("token", "t")

SLACK_FILE_HOSTS = ("files.slack.com",)

_sleep = time.sleep  # seam so tests can record/skip waits
# The copy pool is `--copy-workers` wide; without this the
# worst case is every worker buffering a fallback at once.
_stream_slots = threading.BoundedSemaphore(STREAM_FALLBACK_SLOTS)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return f"{n} B"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── pure helpers (imported by slack_transfer.py for probe/verify) ────────────

def zip_member_name(info: zipfile.ZipInfo) -> str:
    """PURE. The member's REAL name.

    Slack writes UTF-8 member names but leaves the general-purpose UTF-8 flag
    bit (0x800) unset, so zipfile decodes them as cp437 and every non-ASCII
    channel name arrives as mojibake -- measured: 7 of 3,352 conversation dirs
    on the sample export, e.g. 'FC:F0B3W1HS5P0:...' whose real name starts
    with an emoji. Re-decoding is exact and lossless when it round-trips; if
    it does not (a genuinely cp437-encoded archive), the original stands.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def url_token(url: str) -> str | None:
    """PURE. The export token embedded in a Slack file URL, or None.

    Slack uses `token=` on files-pri/ URLs and `t=` on the files-tmb/
    transcoded-video URLs -- both carry the same workspace-wide value.
    """
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    except ValueError:
        return None
    for key in TOKEN_PARAMS:
        vals = qs.get(key)
        if vals and vals[0].startswith("xox"):
            return vals[0]
    return None


def strip_token(url: str) -> tuple[str, str | None]:
    """PURE. (url without its token, the parameter name it used).

    The ledger stores URLs token-FREE and re-attaches the token at copy time.
    Two reasons, both real: a 470k-object ledger would otherwise carry 470k
    copies of a live workspace credential into the container (the export
    already holds it, but duplicating a secret into a new prefix is not
    hygiene we want to ship), and stripping it cut objects.jsonl from 175 MB
    to well under half that on the sample export.

    The parameter NAME is preserved because Slack is not consistent about it:
    files-pri/ URLs use `token=` and files-tmb/ transcodes use `t=` (fact 1),
    and re-attaching under the wrong name produces a 401.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url, None
    kept, used = [], None
    for key, val in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if key in TOKEN_PARAMS and val.startswith("xox"):
            used = key
            continue
        kept.append((key, val))
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)), used


def restore_token(url: str, param: str | None, token: str | None) -> str:
    """PURE. Inverse of strip_token, and the single place auth is attached.

    `param` None covers both a URL that never had a token (the
    attachments[].files[] case, fact 2) and one whose token was already
    present under the default name -- either way `token=` is what Slack's
    files-pri endpoint wants.
    """
    if not url or not token:
        return url
    return url_with_token(url, token) if param in (None, "token") else (
        f"{url}{'&' if urllib.parse.urlsplit(url).query else '?'}"
        f"{param}={urllib.parse.quote(token, safe='')}")


def url_with_token(url: str, token: str | None) -> str:
    """PURE. A fetchable URL (fact 2).

    A URL that already carries a token is used VERBATIM -- Slack signs the
    files-tmb/ ones differently and rewriting them is how you break them.
    A URL with no token gets the harvested workspace token appended; that is
    exactly the `attachments[].files[]` case. With no token available at all
    the URL is returned unchanged so the caller's Bearer header can carry the
    auth instead.
    """
    if not url:
        return url
    if url_token(url) or not token:
        return url
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{sep}token={urllib.parse.quote(token, safe='')}"


def is_slack_file_url(value) -> bool:
    """PURE. True for an http(s) URL served by Slack's file host."""
    if not isinstance(value, str) or not value.startswith("http"):
        return False
    host = urllib.parse.urlsplit(value).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in SLACK_FILE_HOSTS)


def safe_component(name: str, limit: int = 120) -> str:
    """PURE. Blob-safe, deterministic path component.

    Callers put the immutable Slack file id FIRST and this second, so two
    files sharing a display name never collide and the path is identical
    across runs. Names are mutable, ids are not -- the harness has been bitten
    by keying resume on mutable titles before.
    """
    out = []
    for ch in str(name or ""):
        out.append(ch if (ch.isascii() and (ch.isalnum() or ch in "._-"))
                   else "_")
    s = "".join(out).strip("._-") or "unnamed"
    return s[:limit]


def fan(file_id: str) -> str:
    """PURE. Fan-out segment: the first 4 characters of the file id.

    Keeps any one virtual directory off half a million entries (with
    renditions on, the sample export alone would be ~476k objects) and gives
    the sizer's prefix-parallel listing something to parallelise over.
    """
    fid = str(file_id or "unknown")
    return safe_component(fid[:4], limit=8)


def blob_path(file_id: str, name: str, ext_hint: str = "") -> str:
    """PURE. Where an ORIGINAL file's bytes land, relative to the dest prefix."""
    base = safe_component(name)
    if base == "unnamed" and ext_hint:
        base = f"{safe_component(file_id)}.{safe_component(ext_hint)}"
    return f"files/{fan(file_id)}/{safe_component(file_id)}/{base}"


def rendition_blob_path(file_id: str, field: str, url: str) -> str:
    """PURE. Where one rendition lands. The field name leads the basename so
    thumb_360 and thumb_720 of the same image never collide."""
    tail = urllib.parse.unquote(
        urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    return (f"renditions/{fan(file_id)}/{safe_component(file_id)}/"
            f"{safe_component(field, 40)}__{safe_component(tail)}")


def iter_file_objects(message: dict):
    """PURE. Every file object a message carries, with its provenance.

    Yields (file_object, where). `message.files[]` is the obvious one;
    `attachments[].files[]` is the one a naive walk misses -- bot and unfurl
    messages nest real hosted file objects there, and those are exactly the
    entries whose url_private carries NO token (fact 2).
    """
    for f in (message.get("files") or []):
        if isinstance(f, dict) and f.get("id"):
            yield f, "files"
    for att in (message.get("attachments") or []):
        if not isinstance(att, dict):
            continue
        for f in (att.get("files") or []):
            if isinstance(f, dict) and f.get("id"):
                yield f, "attachments"


def file_disposition(f: dict) -> str:
    """PURE. What can actually be done with this file object.

    - "hosted"      -> Slack serves the bytes; copy them.
    - "external"    -> mode=external; the bytes live in Google Drive (or
                       similar) and Slack holds only a link. Recorded, never
                       fetched -- no Slack token can retrieve them.
    - "unavailable" -> tombstone / hidden_by_limit / no URL at all. Recorded
                       with its reason so the gap is quantified, not silent.
    """
    mode = (f.get("mode") or "").strip()
    if mode == "external" or f.get("is_external"):
        return "external"
    if mode in ("tombstone", "hidden_by_limit"):
        return "unavailable"
    if not any(f.get(k) for k in ORIGINAL_URL_KEYS):
        return "unavailable"
    return "hosted"


def original_url(f: dict) -> str:
    for key in ORIGINAL_URL_KEYS:
        if f.get(key):
            return f[key]
    return ""


def rendition_urls(f: dict) -> dict:
    """PURE. Every Slack-hosted derivative of this file: thumb_64..thumb_1024,
    mp4 / mp4_low / hls transcodes, vtt captions, thumb_pdf, deanimate.

    Measured on the sample export: 409,782 renditions against 62,056
    originals -- 471,838 objects in total, a 7.6x object-count multiplier,
    which is why the puller has a --no-renditions switch and probe reports
    both counts side by side.
    """
    out = {}
    for key, val in f.items():
        if key in NOT_RENDITIONS or not is_slack_file_url(val):
            continue
        out[key] = val
    return out


def conversation_kind(dir_name: str) -> str:
    """PURE. Fallback classification when a dir matches no metadata entry."""
    if dir_name.startswith("FC:"):
        return "file_conversation"
    if re.fullmatch(r"D[A-Z0-9]{7,}", dir_name):
        return "dm"
    return "unknown"


def build_conversation_index(root_docs: dict) -> dict:
    """PURE. dir-name -> {id, name, kind}.

    Verified against the sample export: a conversation directory is keyed by
    the conversation's `name` when it has one and by its `id` when it does not
    (DMs), and all 3,352 dirs resolve this way. Both keys are registered so
    either form of directory naming resolves.
    """
    index: dict[str, dict] = {}

    def add(key, entry, kind):
        if key:
            index[key] = {"id": entry.get("id"), "name": entry.get("name"),
                          "kind": kind}
    for fname, kind in CONV_META.items():
        for entry in (root_docs.get(fname) or []):
            if not isinstance(entry, dict):
                continue
            add(entry.get("name"), entry, kind)
            add(entry.get("id"), entry, kind)
    for entry in (root_docs.get("dms.json") or []):
        if isinstance(entry, dict):
            add(entry.get("id"), entry, "dm")
    return index


def classify(azure_status: int, azure_code: str, source_status: str) -> str:
    """PURE. What to do about one failed server-side copy.

    This family keys on the COPY-SOURCE status (Azure surfaces Slack's answer
    in `x-ms-copy-source-status-code`) -- the inverse of teams' status +
    endpoint-family rule, because here there is only one endpoint family and
    two different servers can refuse us.

      ok            2xx, or 409 (If-None-Match: the blob already landed)
      sleep         429 from either side -- never charges the retry budget
      source-auth   Slack said 401/403. The caller decides: fatal when nothing
                    has copied yet (the export token is dead), a per-file skip
                    once the run has proven the token works.
      skip          Slack said 404 -- the file was deleted since the export
      resolve-again the signed URL expired mid-copy (normal, budgeted)
      retry         5xx either side, or a dest 403 (vnet-rule propagation --
                    CLAUDE.md lore: wait, never re-mint)
      fallback      anything else -- try streaming this one file through the VM
    """
    src = int(source_status) if str(source_status).strip().isdigit() else 0
    if azure_status in (200, 201, 202) or azure_status == 409:
        return "ok"
    if src == 429 or azure_status == 429:
        return "sleep"
    if src in (401, 403):
        return "source-auth"
    if src == 404:
        return "skip"
    if azure_code in ("CannotVerifyCopySource", "CopySourceNotFound"):
        return "resolve-again"
    if src >= 500:
        return "retry"
    if azure_status == 403 or azure_status >= 500 or azure_status == 408:
        return "retry"
    return "fallback"


def looks_like_export_root(names) -> bool:
    """PURE. Do these member/blob basenames identify a Slack export root?"""
    have = set(names)
    return all(sig in have for sig in EXPORT_SIGNATURE)


# ── blob REST (this family never uses azcopy: the transport is already REST) ─

class CopyError(Exception):
    def __init__(self, msg: str, azure_status: int = 0, azure_code: str = "",
                 source_status: str = ""):
        super().__init__(msg)
        self.azure_status = azure_status
        self.azure_code = azure_code
        self.source_status = source_status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface 3xx as HTTPError so we can read Location


_nr_opener = urllib.request.build_opener(_NoRedirect)


def _http(req: urllib.request.Request, timeout: int = 120):
    """Transport seam (tests stub this; production never branches)."""
    return urllib.request.urlopen(req, timeout=timeout)


def _http_nr(req: urllib.request.Request, timeout: int = 60):
    """No-redirect transport seam -- 3xx surfaces as HTTPError."""
    return _nr_opener.open(req, timeout=timeout)


def _azure_err(e: urllib.error.HTTPError) -> tuple[str, str, str]:
    """(azure error code, copy-source status header, body excerpt)."""
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    m = re.search(r"<Code>([^<]+)</Code>", body)
    src = (e.headers.get("x-ms-copy-source-status-code") or "").strip()
    return (m.group(1) if m else ""), src, body[:200]


class Blob:
    """The container, addressed by REST with the racwl SAS.

    Both sides of this job live here: the export is READ from the container
    and the files are WRITTEN to it. Every write is create-only
    (`If-None-Match: *`) except the run metadata, which must be replaceable
    or a `--limit-files` pilot would poison verify forever (the
    github-pilot-poisons-verify lesson, inherited from day one).
    """

    def __init__(self, base_url: str, sas: str, dry_run: bool = False):
        self.base = base_url.rstrip("/")
        self.sas = sas
        self.dry_run = dry_run
        self.sleeps = 0

    def url(self, name: str) -> str:
        return f"{self.base}/{urllib.parse.quote(name, safe='/')}"

    # -- reads ---------------------------------------------------------------

    def get(self, name: str, byte_range: tuple[int, int] | None = None) -> bytes:
        headers = {"x-ms-version": X_MS_VERSION}
        if byte_range:
            headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        last: Exception | None = None
        for attempt in range(API_RETRIES):
            req = urllib.request.Request(f"{self.url(name)}?{self.sas}",
                                         headers=headers)
            try:
                with _http(req, timeout=300) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 403:
                    # vnet-rule propagation, not a bad SAS -- never re-mint
                    _sleep(15 * (attempt + 1))
                    continue
                if e.code >= 500:
                    _sleep(1 + attempt)
                    continue
                raise CopyError(f"blob GET {name}: HTTP {e.code}",
                                azure_status=e.code)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                _sleep(1 + attempt)
        raise CopyError(f"blob GET {name} failed after retries: {last}")

    def download(self, name: str, dest: Path) -> int:
        """Stream one (potentially multi-GB) blob to disk. Used once, for the
        export archive itself -- never for corpus files, whose bytes go
        straight from Slack to the container."""
        req = urllib.request.Request(f"{self.url(name)}?{self.sas}",
                                     headers={"x-ms-version": X_MS_VERSION})
        total = 0
        with _http(req, timeout=3600) as r, open(dest, "wb") as fh:
            while True:
                chunk = r.read(8 * 1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
        return total

    def list(self, prefix: str) -> dict:
        """Marker-paginated listing -> {name: size}. Content-Length is what
        Azure actually committed: the resume truth and verify's ground."""
        blobs: dict[str, int] = {}
        marker = ""
        while True:
            url = (f"{self.base}?restype=container&comp=list&maxresults=5000"
                   f"&prefix={urllib.parse.quote(prefix, safe='')}")
            if marker:
                url += f"&marker={urllib.parse.quote(marker, safe='')}"
            raw = self._list_get(url + "&" + self.sas)
            root = ET.fromstring(raw)
            for blob in root.iter("Blob"):
                name = blob.findtext("Name")
                props = blob.find("Properties")
                if name:
                    blobs[name] = int((props.findtext("Content-Length") or 0)
                                      if props is not None else 0)
            marker = root.findtext("NextMarker") or ""
            if not marker:
                return blobs

    def _list_get(self, url: str) -> bytes:
        last: Exception | None = None
        for attempt in range(API_RETRIES):
            req = urllib.request.Request(
                url, headers={"x-ms-version": X_MS_VERSION})
            try:
                with _http(req, timeout=180) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 403:
                    _sleep(15 * (attempt + 1))
                    continue
                if e.code >= 500:
                    _sleep(1 + attempt)
                    continue
                raise CopyError(f"container listing: HTTP {e.code}",
                                azure_status=e.code)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                _sleep(1 + attempt)
        raise CopyError(f"container listing failed after retries: {last}")

    # -- writes --------------------------------------------------------------

    def put_bytes(self, name: str, body: bytes, content_type: str,
                  create_only: bool = True) -> int:
        """PUT one small blob. Returns bytes written; 0 = it already existed
        (the 409 is If-None-Match doing its job)."""
        if self.dry_run:
            print(f"DRY-RUN: PUT {self.url(name)}?<sas-redacted> "
                  f"(BlockBlob, {'If-None-Match: *, ' if create_only else ''}"
                  f"{content_type}, {len(body)} bytes)")
            return len(body)
        headers = {"x-ms-version": X_MS_VERSION,
                   "x-ms-blob-type": "BlockBlob",
                   "Content-Type": content_type}
        if create_only:
            headers["If-None-Match"] = "*"
        last: Exception | None = None
        for attempt in range(API_RETRIES):
            req = urllib.request.Request(f"{self.url(name)}?{self.sas}",
                                         data=body, method="PUT",
                                         headers=headers)
            try:
                with _http(req, timeout=300) as r:
                    r.read()
                    return len(body)
            except urllib.error.HTTPError as e:
                if e.code == 409 and create_only:
                    return 0
                code, _src, excerpt = _azure_err(e)
                last = CopyError(f"PUT {name}: HTTP {e.code} {code} {excerpt}",
                                 azure_status=e.code, azure_code=code)
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
        raise CopyError(f"PUT {name} failed after retries: {last}")

    def put_large(self, name: str, body: bytes, content_type: str) -> int:
        """PUT a blob too big for one request, as staged blocks.

        Azure caps a single Put Blob at 5000 MiB. objects.jsonl is one row
        per object -- ~100 MB for a 60k-file workspace -- so an enterprise
        corpus can push the ledger past that. Without this, a multi-day run
        would finish and then fail to upload its own authority.
        """
        blocks = []
        for i in range(0, len(body), META_BLOCK_SIZE):
            bid = _block_id(i // META_BLOCK_SIZE)
            chunk = body[i:i + META_BLOCK_SIZE]
            url = (f"{self.url(name)}?comp=block&blockid="
                   f"{urllib.parse.quote(bid)}&{self.sas}")
            if self.dry_run:
                print(f"DRY-RUN: PUT {self.url(name)}?comp=block "
                      f"({len(chunk)} bytes)")
            else:
                req = urllib.request.Request(
                    url, data=chunk, method="PUT",
                    headers={"x-ms-version": X_MS_VERSION})
                with _http(req, timeout=600) as r:
                    r.read()
            blocks.append(bid)
        # run metadata is replaceable by design, so no If-None-Match here
        if self.dry_run:
            return len(body)
        body_xml = ("<?xml version='1.0' encoding='utf-8'?><BlockList>"
                    + "".join(f"<Latest>{b}</Latest>" for b in blocks)
                    + "</BlockList>").encode()
        req = urllib.request.Request(
            f"{self.url(name)}?comp=blocklist&{self.sas}", data=body_xml,
            method="PUT", headers={"x-ms-version": X_MS_VERSION,
                                   "Content-Type": "application/xml",
                                   "x-ms-blob-content-type": content_type})
        with _http(req, timeout=300) as r:
            r.read()
        return len(body)

    def put_from_url(self, name: str, src_url: str, content_type: str,
                     bearer: str | None = None) -> int:
        """Single-request server-side copy: Azure fetches the bytes itself.

        Returns 1 on success, 0 when the blob already existed (409). Raises
        CopyError carrying both HTTP statuses so classify() can decide.
        """
        headers = {
            "x-ms-version": X_MS_VERSION,
            "x-ms-blob-type": "BlockBlob",
            "x-ms-copy-source": src_url,
            "x-ms-blob-content-type": content_type or
            "application/octet-stream",
            # Without an override Azure copies the source's headers onto the
            # blob, and Slack's Content-Disposition filename can be a property
            # Azure rejects (mojibake + quoting) -- the vimeo lesson.
            "x-ms-blob-content-disposition": "attachment",
            "If-None-Match": "*",
        }
        if bearer:
            headers["x-ms-copy-source-authorization"] = f"Bearer {bearer}"
        if self.dry_run:
            print(f"DRY-RUN: PUT {self.url(name)}?<sas-redacted> "
                  "(x-ms-copy-source: <slack-url>, If-None-Match: *)")
            return 1
        return self._copy_request(f"{self.url(name)}?{self.sas}", headers,
                                  f"copy {name}")

    def put_block_from_url(self, name: str, block_id: str, src_url: str,
                           start: int, end: int,
                           bearer: str | None = None) -> None:
        headers = {"x-ms-version": X_MS_VERSION,
                   "x-ms-copy-source": src_url,
                   "x-ms-source-range": f"bytes={start}-{end}"}
        if bearer:
            headers["x-ms-copy-source-authorization"] = f"Bearer {bearer}"
        if self.dry_run:
            print(f"DRY-RUN: PUT {self.url(name)}?comp=block&blockid=... "
                  f"(x-ms-source-range: bytes={start}-{end})")
            return
        url = (f"{self.url(name)}?comp=block&blockid="
               f"{urllib.parse.quote(block_id)}&{self.sas}")
        self._copy_request(url, headers, f"block {block_id} of {name}")

    def put_block_list(self, name: str, block_ids: list, content_type: str) -> int:
        """Commit staged blocks -- the moment the blob comes into existence,
        which is why If-None-Match: * rides HERE."""
        body = ("<?xml version='1.0' encoding='utf-8'?><BlockList>"
                + "".join(f"<Latest>{b}</Latest>" for b in block_ids)
                + "</BlockList>").encode()
        if self.dry_run:
            print(f"DRY-RUN: PUT {self.url(name)}?comp=blocklist "
                  f"(If-None-Match: *, {len(block_ids)} blocks)")
            return 1
        headers = {"x-ms-version": X_MS_VERSION,
                   "Content-Type": "application/xml",
                   "x-ms-blob-content-type": content_type or
                   "application/octet-stream",
                   "If-None-Match": "*"}
        url = f"{self.url(name)}?comp=blocklist&{self.sas}"
        last: Exception | None = None
        for attempt in range(API_RETRIES):
            req = urllib.request.Request(url, data=body, method="PUT",
                                         headers=headers)
            try:
                with _http(req, timeout=300) as r:
                    r.read()
                    return 1
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    return 0
                code, src, excerpt = _azure_err(e)
                last = CopyError(f"commit {name}: HTTP {e.code} {code} "
                                 f"{excerpt}", azure_status=e.code,
                                 azure_code=code, source_status=src)
                if e.code in (403, 408) or e.code >= 500:
                    _sleep(2 + 2 * attempt)
                    continue
                raise last
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                _sleep(2 + 2 * attempt)
        raise CopyError(f"commit {name} failed after retries: {last}")

    def _copy_request(self, url: str, headers: dict, what: str) -> int:
        """One server-side-copy PUT. Retries only what classify() calls
        retryable; everything else is raised for the caller to classify."""
        last: Exception | None = None
        sleeps = 0
        attempt = 0
        while attempt < API_RETRIES:
            req = urllib.request.Request(url, data=b"", method="PUT",
                                         headers=headers)
            try:
                with _http(req, timeout=1800) as r:
                    r.read()
                    return 1
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    return 0
                code, src, excerpt = _azure_err(e)
                err = CopyError(f"{what}: HTTP {e.code} {code} "
                                f"(source {src or '-'}) {excerpt}",
                                azure_status=e.code, azure_code=code,
                                source_status=src)
                verdict = classify(e.code, code, src)
                if verdict == "sleep" and sleeps < MAX_SLEEPS_PER_CALL:
                    sleeps += 1
                    self.sleeps += 1
                    _sleep(min(60, 5 * (sleeps + 1)))
                    continue          # 429 never charges the retry budget
                if verdict == "retry":
                    attempt += 1
                    last = err
                    _sleep(2 + 2 * attempt)
                    continue
                raise err
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                attempt += 1
                last = e
                _sleep(2 + 2 * attempt)
        raise CopyError(f"{what} failed after retries: {last}")


# ── Slack source ────────────────────────────────────────────────────────────

class PaceBucket:
    """Proactive pacing with the 429 backstop handled by the caller.
    Thread-safe: the copy pool shares one bucket."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if delay:
            _sleep(delay)


def resolve_slack(url: str, token: str | None, bearer: str | None = None
                  ) -> tuple[str, int | None, bool]:
    """Follow the Slack URL's redirect chain -> (final URL, wire size, ranged).

    Azure's copy-from-URL never follows 3xx, so resolution happens here (the
    vimeo shape). The size comes from the 0-byte probe's Content-Range total
    because the export's declared `size` can disagree with what is actually
    served. `ranged` reports whether the final host answered 206 -- Put Block
    From URL requires Range support, so a large file whose host refuses it is
    routed to the stream-through fallback instead of failing.

    A signed URL that Slack hands back expires within hours; it is never
    cached, only re-resolved on demand.
    """
    current = url_with_token(url, token) if not bearer else url
    headers = {"Range": "bytes=0-0"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    for _hop in range(RESOLVE_HOPS):
        req = urllib.request.Request(current, headers=headers)
        try:
            resp = _http_nr(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise CopyError("redirect without Location",
                                    source_status=str(e.code))
                current = urllib.parse.urljoin(current, loc)
                # The signed URL Slack redirects to carries its own auth;
                # forwarding ours would be both useless and a token leak to
                # whatever CDN host it names.
                headers = {"Range": "bytes=0-0"}
                continue
            if e.code == 429:
                _sleep(5)
                continue
            raise CopyError(f"resolving slack url: HTTP {e.code}",
                            source_status=str(e.code))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CopyError(f"resolving slack url: {e}")
        status = getattr(resp, "status", None)
        total = None
        content_range = resp.headers.get("Content-Range") or ""
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                total = int(tail)
        elif resp.headers.get("Content-Length") and status == 200:
            total = int(resp.headers["Content-Length"])
        resp.close()
        return current, total, status == 206
    raise CopyError("redirect loop resolving slack url")


def stream_through(blob: Blob, name: str, src_url: str, content_type: str,
                   bearer: str | None) -> int:
    """Per-file fallback when Azure will not copy from the source: pull the
    bytes through this VM and PUT them. Bounded by STREAM_FALLBACK_MAX so one
    pathological file can never exhaust the VM's memory; anything larger is
    recorded as a failure for a human to look at rather than silently skipped.
    """
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(src_url, headers=headers)
    with _stream_slots:
        with _http(req, timeout=1800) as r:
            declared = r.headers.get("Content-Length")
            if declared and int(declared) > STREAM_FALLBACK_MAX:
                raise CopyError(
                    f"stream fallback refused: {human_bytes(int(declared))} "
                    f"exceeds the {human_bytes(STREAM_FALLBACK_MAX)} "
                    "in-memory cap")
            body = r.read(STREAM_FALLBACK_MAX + 1)
        if len(body) > STREAM_FALLBACK_MAX:
            raise CopyError("stream fallback refused: source exceeded the "
                            "in-memory cap without declaring a length")
        return blob.put_bytes(name, body, content_type, create_only=True)


# ── export readers (the source is a blob in the client's own container) ──────

def detect_export_root(names) -> str | None:
    """PURE. The prefix under which the export's root JSONs live, or None.

    Clients re-zip exports inside a wrapper folder often enough that the root
    cannot be assumed to be "". The shallowest `channels.json` wins, and its
    siblings must satisfy EXPORT_SIGNATURE before the prefix is accepted.
    """
    best: str | None = None
    for n in names:
        parts = n.split("/")
        if parts[-1] != "channels.json":
            continue
        prefix = "/".join(parts[:-1])
        prefix = prefix + "/" if prefix else ""
        if best is None or len(prefix) < len(best):
            best = prefix
    if best is None:
        return None
    siblings = {n[len(best):] for n in names
                if n.startswith(best) and "/" not in n[len(best):]}
    return best if looks_like_export_root(siblings) else None


class ZipExport:
    """A Slack export delivered as one .zip blob.

    Downloaded to VM disk once (in-region, sequential, free) rather than
    range-read member by member: 123k members would be ~250k ranged GETs
    against the container, and the archive is JSON only -- tens of GB even
    for a large workspace, against a corpus that is measured in TB.
    """
    kind = "zip"

    def __init__(self, path: Path):
        self.path = path
        self.z = zipfile.ZipFile(path)
        members = {}
        for info in self.z.infolist():
            if info.is_dir():
                continue
            members[zip_member_name(info)] = info   # fact 3: cp437 fallback
        self.root = detect_export_root(members)
        if self.root is None:
            raise CopyError(
                f"{path.name} does not look like a Slack export: no "
                "channels.json + users.json pair at any single level")
        self._members = {n[len(self.root):]: i for n, i in members.items()
                         if n.startswith(self.root)}

    def names(self) -> list:
        return sorted(self._members)

    def read(self, rel: str) -> bytes:
        return self.z.read(self._members[rel])

    def read_many(self, rels):
        for rel in rels:
            yield rel, self.read(rel)

    def describe(self) -> dict:
        return {"kind": "zip", "blob": self.path.name,
                "root": self.root, "members": len(self._members)}


class TreeExport:
    """A Slack export the client unzipped before pushing: the day JSONs are
    individual blobs. Reads are pooled -- 123k sequential blob GETs would
    dominate the run, and these are kilobyte objects."""
    kind = "tree"

    def __init__(self, blob: Blob, prefix: str, workers: int = 16):
        self.blob = blob
        self.workers = workers
        listing = blob.list(prefix.rstrip("/") + "/" if prefix else "")
        rel_of = {}
        base = prefix.rstrip("/") + "/" if prefix else ""
        for name in listing:
            if name.startswith(base):
                rel_of[name[len(base):]] = name
        self.root = detect_export_root(rel_of)
        if self.root is None:
            raise CopyError(
                f"prefix {prefix!r} does not look like a Slack export: no "
                "channels.json + users.json pair at any single level")
        self._blobs = {r[len(self.root):]: b for r, b in rel_of.items()
                       if r.startswith(self.root)}
        self.prefix = base

    def names(self) -> list:
        return sorted(self._blobs)

    def read(self, rel: str) -> bytes:
        return self.blob.get(self._blobs[rel])

    def read_many(self, rels):
        rels = list(rels)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for rel, data in zip(rels, pool.map(self.read, rels)):
                yield rel, data

    def describe(self) -> dict:
        return {"kind": "tree", "prefix": self.prefix + self.root,
                "root": self.root, "members": len(self._blobs)}


def day_file_names(names) -> list:
    """PURE. The per-conversation day files: `<conversation>/<date>.json`.

    Anything at the export root is metadata, not a conversation; anything
    deeper than one level is not a Slack day file. Sorted so the ledger is
    built in a deterministic order -- that is what makes each file's blob path
    reproducible across runs.
    """
    out = [n for n in names
           if n.endswith(".json") and n.count("/") == 1]
    return sorted(out)


# ── ledger (phase B) ────────────────────────────────────────────────────────

class JsonlWriter:
    """Small append-only writer so every ledger file is opened exactly once."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fh = open(path, "w", encoding="utf-8")
        self.count = 0

    def write(self, obj) -> None:
        self.fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.count += 1

    def close(self) -> None:
        self.fh.close()


def asset_file_objects(kind: str, entries, history_key: str | None):
    """PURE. Root canvases/lists/huddle_transcripts -> file-object shape.

    These never appear in a message walk (fact 4) but they are Slack-hosted
    corpus with their own url_private_download. The *_history_download link
    (full edit history of a canvas or list) rides along as a rendition, so
    the richer artifact is captured rather than only the current snapshot.
    """
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        obj = dict(entry)
        obj.setdefault("mode", kind)
        obj.setdefault("name", entry.get("title") or kind)
        if history_key and entry.get(history_key):
            obj[f"{kind}_history"] = entry[history_key]
        yield obj


def build_ledger(export, dest: Path, renditions: bool,
                 progress=None) -> dict:
    """Phase B: one pass over the export -> the ledger files under _meta/.

    Two ledgers, deliberately separate:
      files-index.jsonl -- one row per UNIQUE file, carrying the conversation
        and message that first referenced it. This is the join table that
        makes the corpus "linked": the export's own JSON references file ids,
        and this maps each id to the blob its bytes landed in.
      objects.jsonl -- one row per BLOB we intend to write (originals plus,
        when enabled, every rendition). This is verify's authority, and the
        reason verify can make a real byte-exact claim: the export declares a
        `size` for every original.

    Ordering is deterministic (root assets by id, then conversations and day
    files sorted, then message order), so the "first sighting owns the blob
    path" rule produces identical paths on every re-run.
    """
    meta = dest / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    names = export.names()
    root_docs = {}
    for fname in ROOT_JSONS:
        if fname in names:
            try:
                root_docs[fname] = json.loads(export.read(fname))
            except (ValueError, KeyError) as exc:
                log(f"ledger: WARNING {fname} unreadable ({exc}) — continuing")
    conv_index = build_conversation_index(root_docs)

    w_conv = JsonlWriter(meta / "conversations.jsonl")
    for key, entry in sorted(conv_index.items()):
        w_conv.write({"key": key, **entry})
    w_conv.close()

    w_files = JsonlWriter(meta / "files-index.jsonl")
    w_shares = JsonlWriter(meta / "file-shares.jsonl")
    w_ext = JsonlWriter(meta / "external-references.jsonl")
    w_unavail = JsonlWriter(meta / "unavailable.jsonl")
    w_obj = JsonlWriter(meta / "objects.jsonl")

    seen: set = set()
    counts = {"messages": 0, "file_refs": 0, "hosted": 0, "external": 0,
              "unavailable": 0, "renditions": 0, "day_files": 0}
    declared = {"hosted": 0, "external": 0}
    token_seen: str | None = None

    def record(f: dict, conv: dict, message_ts, thread_ts, where: str):
        nonlocal token_seen
        fid = f.get("id")
        counts["file_refs"] += 1
        if fid in seen:
            w_shares.write({"file_id": fid, "conversation": conv,
                            "message_ts": message_ts, "where": where})
            return
        seen.add(fid)
        disposition = file_disposition(f)
        url = original_url(f)
        token_seen = token_seen or url_token(url)
        row = {
            "file_id": fid,
            "mode": f.get("mode"),
            "disposition": disposition,
            "filetype": f.get("filetype"),
            "mimetype": f.get("mimetype"),
            "name": f.get("name") or f.get("title"),
            "title": f.get("title"),
            "size": f.get("size") or 0,
            "user": f.get("user"),
            "created": f.get("created"),
            "conversation": conv,
            "message_ts": message_ts,
            "thread_ts": thread_ts,
            "where": where,
            "permalink": f.get("permalink"),
        }
        if disposition == "external":
            counts["external"] += 1
            declared["external"] += row["size"]
            row["external_type"] = f.get("external_type")
            row["external_url"] = url
            row["status"] = "external-reference"
            w_ext.write(row)
            w_files.write(row)
            return
        if disposition == "unavailable":
            counts["unavailable"] += 1
            row["reason"] = f.get("mode") or "no-url"
            row["status"] = "unavailable"
            w_unavail.write(row)
            w_files.write(row)
            return
        counts["hosted"] += 1
        declared["hosted"] += row["size"]
        path = blob_path(fid, row["name"] or "", row.get("filetype") or "")
        row["blob"] = path
        row["status"] = "pending"
        bare, param = strip_token(url)
        w_obj.write({"blob": path, "file_id": fid, "kind": "original",
                     "url": bare, "tp": param, "size": row["size"],
                     "content_type": row.get("mimetype") or "",
                     "status": "pending"})
        if renditions:
            rend = {}
            for field, rurl in sorted(rendition_urls(f).items()):
                rpath = rendition_blob_path(fid, field, rurl)
                rend[field] = rpath
                counts["renditions"] += 1
                rbare, rparam = strip_token(rurl)
                w_obj.write({"blob": rpath, "file_id": fid,
                             "kind": f"rendition:{field}", "url": rbare,
                             "tp": rparam, "size": 0, "content_type": "",
                             "status": "pending"})
            if rend:
                row["renditions"] = rend
        w_files.write(row)

    # Root assets first: they carry the richest metadata (title, permalink,
    # history link) and processing them first makes their blob paths stable
    # regardless of which channel happens to mention them.
    for fname, kind in ASSET_JSONS.items():
        entries = root_docs.get(fname) or []
        conv = {"dir": None, "id": None, "name": fname, "kind": f"root:{kind}"}
        for obj in asset_file_objects(kind, entries,
                                      ASSET_HISTORY.get(fname)):
            record(obj, conv, None, None, fname)

    day_files = day_file_names(names)
    for rel, data in export.read_many(day_files):
        counts["day_files"] += 1
        conv_dir = rel.split("/")[0]
        info = conv_index.get(conv_dir) or {
            "id": None, "name": conv_dir, "kind": conversation_kind(conv_dir)}
        conv = {"dir": conv_dir, "id": info.get("id"),
                "name": info.get("name"), "kind": info.get("kind")}
        try:
            messages = json.loads(data)
        except ValueError:
            log(f"ledger: WARNING {rel} is not valid JSON — skipped")
            continue
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            counts["messages"] += 1
            for f, where in iter_file_objects(msg):
                record(f, conv, msg.get("ts"), msg.get("thread_ts"), where)
        if progress and counts["day_files"] % 5000 == 0:
            progress(counts["day_files"], len(day_files))

    for w in (w_files, w_shares, w_ext, w_unavail, w_obj):
        w.close()
    counts["unique_files"] = len(seen)
    counts["conversations"] = len(conv_index)
    counts["objects"] = w_obj.count
    return {"counts": counts, "declared_bytes": declared,
            "export_token_present": bool(token_seen),
            "export_token": token_seen,
            "slack_manifest": root_docs.get(".slack-manifest.json")}


# ── copy (phase C) ──────────────────────────────────────────────────────────

def _block_id(i: int) -> str:
    import base64
    return base64.b64encode(f"{i:08d}".encode()).decode()


class Copier:
    """One object at a time, server-side first, stream-through as a fallback.

    The auth story in one place: a URL that already carries the export token
    goes to Azure verbatim; a tokenless one gets the harvested workspace token
    appended; and when the run is using a client-supplied Slack token instead,
    that rides `x-ms-copy-source-authorization: Bearer` -- but ONLY when the
    final URL is still on a Slack host. Once Slack has redirected us to a
    signed CDN URL, forwarding our credential would be both pointless and a
    leak to that host.
    """

    def __init__(self, blob: Blob, prefix: str, token: str | None,
                 bearer: str | None, pace: PaceBucket, existing: set):
        self.blob = blob
        self.prefix = prefix.rstrip("/")
        self.token = token
        self.bearer = bearer
        self.pace = pace
        self.existing = existing
        self.lock = threading.Lock()
        self.copied = 0
        self.streamed = 0
        self.present = 0
        self.source_auth_failures = 0
        self.copied_bytes = 0

    def dest_name(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def _bearer_for(self, final_url: str) -> str | None:
        return self.bearer if (self.bearer and is_slack_file_url(final_url)) \
            else None

    def copy(self, row: dict) -> dict:
        name = self.dest_name(row["blob"])
        if name in self.existing:
            with self.lock:
                self.present += 1
            return {"blob": row["blob"], "status": "present"}
        content_type = row.get("content_type") or "application/octet-stream"
        last_err = ""
        for attempt in range(RESOLVE_BUDGET + 1):
            self.pace.wait()
            src = restore_token(row["url"], row.get("tp"), self.token)
            try:
                final, wire, ranged = resolve_slack(src, self.token,
                                                    self.bearer)
            except CopyError as e:
                verdict = classify(0, "", e.source_status)
                return self._terminal(row, verdict, str(e))
            size = wire if wire is not None else int(row.get("size") or 0)
            bearer = self._bearer_for(final)
            try:
                if size > SINGLE_SHOT_MAX and ranged:
                    self._blocked_copy(name, final, size, content_type,
                                       bearer, row)
                else:
                    self.blob.put_from_url(name, final, content_type, bearer)
                with self.lock:
                    self.copied += 1
                    self.copied_bytes += max(size, 0)
                return {"blob": row["blob"], "status": "copied",
                        "transport": "server-side", "bytes": max(size, 0)}
            except CopyError as e:
                last_err = str(e)
                verdict = classify(e.azure_status, e.azure_code,
                                   e.source_status)
                if verdict == "resolve-again" and attempt < RESOLVE_BUDGET:
                    continue          # signed URL expired mid-copy: normal
                if verdict == "fallback":
                    try:
                        stream_through(self.blob, name, final,
                                       content_type, bearer)
                        with self.lock:
                            self.streamed += 1
                            self.copied_bytes += max(size, 0)
                        return {"blob": row["blob"], "status": "copied",
                                "transport": "streamed",
                                "bytes": max(size, 0)}
                    except (CopyError, urllib.error.HTTPError,
                            urllib.error.URLError, OSError) as se:
                        return self._terminal(row, "failed",
                                              f"{last_err}; fallback: {se}")
                return self._terminal(row, verdict, last_err)
        return self._terminal(row, "failed",
                              last_err or "resolve budget exhausted")

    def _blocked_copy(self, name: str, final: str, size: int,
                      content_type: str, bearer: str | None,
                      row: dict) -> None:
        """Files above SINGLE_SHOT_MAX: stage 256 MiB blocks server-side, then
        commit. A block whose signed URL expired mid-flight is retried against
        a freshly resolved URL under the same block id."""
        blocks = []
        total = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
        for i in range(total):
            start = i * BLOCK_SIZE
            end = min(size, start + BLOCK_SIZE) - 1
            bid = _block_id(i)
            for attempt in range(RESOLVE_BUDGET + 1):
                try:
                    self.blob.put_block_from_url(name, bid, final, start, end,
                                                 bearer)
                    break
                except CopyError as e:
                    verdict = classify(e.azure_status, e.azure_code,
                                       e.source_status)
                    if verdict == "resolve-again" and attempt < RESOLVE_BUDGET:
                        final, _wire, _r = resolve_slack(
                            restore_token(row["url"], row.get("tp"),
                                          self.token),
                            self.token, self.bearer)
                        bearer = self._bearer_for(final)
                        continue
                    raise
            blocks.append(bid)
        self.blob.put_block_list(name, blocks, content_type)

    def _terminal(self, row: dict, verdict: str, detail: str) -> dict:
        if verdict == "source-auth":
            with self.lock:
                self.source_auth_failures += 1
                dead = (self.copied == 0 and self.streamed == 0
                        and self.source_auth_failures >= AUTH_FATAL_THRESHOLD)
            status = "token-dead" if dead else "forbidden"
        elif verdict == "skip":
            status = "gone"
        else:
            status = "failed"
        return {"blob": row["blob"], "status": status, "detail": detail[:300]}


class TokenDead(Exception):
    """Raised once it is established that the export's links no longer
    authenticate -- continuing would just burn hours producing nothing."""


def iter_shards(objects_path: Path, size: int):
    """Stream objects.jsonl in fixed-size shards so a multi-million-object
    ledger never has to be held in memory at once."""
    batch, index = [], 0
    with open(objects_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            batch.append(json.loads(line))
            if len(batch) >= size:
                yield index, batch
                index += 1
                batch = []
    if batch:
        yield index, batch


def run_copy(blob: Blob, prefix: str, dest: Path, objects_path: Path,
             token: str | None, bearer: str | None, args: dict) -> dict:
    """Phase C. Shards are fixed-size slices of the ledger, not per-
    conversation units: file counts per conversation are wildly skewed (3,559
    in one channel of the sample against a handful in most), and
    per-conversation units would make resume lumpy.

    Resume truth here is STRONGER than the cursor files its sibling ingests
    use: the blob's existence is the record, enforced by `If-None-Match: *`
    on every write. Shard markers only record "this slice was fully walked";
    the up-front dest listing and a 409 are what actually prevent re-copying.
    """
    shard_size = int(args.get("shard_size") or SHARD_SIZE)
    workers = int(args.get("copy_workers") or DEFAULT_COPY_WORKERS)
    limit = int(args.get("limit_files") or 0)
    pace = PaceBucket(float(args.get("rps_files") or DEFAULT_RPS_FILES))

    log("copy: listing the destination prefix to resume cheaply")
    existing = set()
    for sub in ("files/", "renditions/"):
        existing |= set(blob.list(f"{prefix}/{sub}"))
    markers = set(blob.list(f"{prefix}/_meta/shards/"))
    log(f"copy: {len(existing)} objects already in the container, "
        f"{len(markers)} shards already complete")

    copier = Copier(blob, prefix, token, bearer, pace, existing)
    status_by_blob: dict[str, str] = {}
    failures: list = []
    results: list = []
    processed = 0
    stop = False

    for index, batch in iter_shards(objects_path, shard_size):
        unit = f"_meta/shards/{index:05d}"
        marker = f"{prefix}/{unit}.cdp-complete"
        if marker in markers:
            # Certified complete by an earlier pass. Its objects' statuses
            # come from the listing we already hold, never a blanket label:
            # a shard can legitimately contain files that were 404 at the
            # time, and telling verify to expect those blobs would invent a
            # gap that never existed.
            present = 0
            for row in batch:
                if copier.dest_name(row["blob"]) in existing:
                    status_by_blob[row["blob"]] = "present"
                    present += 1
                else:
                    status_by_blob[row["blob"]] = "recorded-absent"
            # Roll these into the run totals. Without it a resume of an
            # already-complete corpus reports copied=0 / present=0, which
            # reads as "nothing happened" in status and the manifest when
            # what actually happened is "everything was already there".
            copier.present += present
            results.append({"unit": unit, "kind": "shard",
                            "status": "skipped-complete",
                            "objects": len(batch), "present": present})
            continue
        if limit and processed >= limit:
            log(f"copy: --limit-files {limit} reached — stopping cleanly")
            break
        if limit:
            batch = batch[:max(0, limit - processed)]
        tally = {"copied": 0, "present": 0, "failed": 0, "gone": 0}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for out in pool.map(copier.copy, batch):
                status_by_blob[out["blob"]] = out["status"]
                if out["status"] == "token-dead":
                    stop = True
                if out["status"] in tally:
                    tally[out["status"]] += 1
                else:
                    tally["failed"] += 1
                    if len(failures) < FAILED_SAMPLE_CAP:
                        failures.append(out)
        processed += len(batch)
        complete = tally["failed"] == 0 and not stop
        if complete:
            blob.put_bytes(f"{prefix}/{unit}.cdp-complete", b"",
                           "text/plain", create_only=False)
        results.append({"unit": unit, "kind": "shard",
                        "status": "ok" if complete else "incomplete",
                        "objects": len(batch), **tally})
        write_progress(dest, blob, prefix, "copy", processed,
                       f"shard {index}: {tally}")
        log(f"copy: shard {index:05d} {tally} "
            f"({human_bytes(copier.copied_bytes)} so far)")
        if stop:
            raise TokenDead(
                "Slack refused the first "
                f"{AUTH_FATAL_THRESHOLD} files with 401/403 and nothing has "
                "copied — the export's download token is dead. Export links "
                "expire with the export: the client must run a FRESH Slack "
                "export (or supply a Slack token via write-creds). This is a "
                "client conversation, not a retry.")

    return {"results": results, "failures": failures,
            "status_by_blob": status_by_blob,
            "totals": {"copied": copier.copied, "streamed": copier.streamed,
                       "already_present": copier.present,
                       "copied_bytes": copier.copied_bytes,
                       "failed": len(failures)}}


# ── manifest, progress, upload ──────────────────────────────────────────────

def write_progress(dest: Path, blob: Blob | None, prefix: str, phase: str,
                   done: int, message: str) -> None:
    """Heartbeat slack_transfer.py's status subcommand reads. Never fatal."""
    payload = {"source": "slack", "phase": phase, "done": done,
               "message": message, "updated_utc": utc_now_iso()}
    try:
        (dest / "progress.json").write_text(json.dumps(payload))
    except OSError:
        pass
    if blob is not None:
        try:
            blob.put_bytes(f"{prefix}/_meta/progress.json",
                           json.dumps(payload).encode(), "application/json",
                           create_only=False)
        except (CopyError, OSError):
            pass


def patch_statuses(path: Path, status_by_blob: dict) -> None:
    """Rewrite a ledger file in place, stamping each row's final status.

    Streamed rather than held in memory: a large workspace's objects.jsonl is
    millions of rows, and only the compact blob->status map needs to be
    resident.
    """
    if not path.exists():
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(path, encoding="utf-8") as src, \
            open(tmp, "w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            blob_name = row.get("blob")
            if blob_name and blob_name in status_by_blob:
                row["status"] = status_by_blob[blob_name]
            dst.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def build_manifest(context: dict, ledger: dict, copy_out: dict,
                   started: str, finished: str) -> dict:
    totals = copy_out["totals"]
    failed_units = [r["unit"] for r in copy_out["results"]
                    if r["status"] == "incomplete"]
    return {
        "source": "slack",
        "context": context,
        "started_utc": started,
        "finished_utc": finished,
        "counts": ledger["counts"],
        "declared_bytes": ledger["declared_bytes"],
        "shard_count": len(copy_out["results"]),
        "unit_count": len(copy_out["results"]),
        "total_staged_bytes": totals["copied_bytes"],
        "totals": totals,
        "failed_units": failed_units,
        "skipped_units": [],
        "failed_sample": copy_out["failures"],
        "results": copy_out["results"],
        "external_references": ledger["counts"]["external"],
        "external_bytes": ledger["declared_bytes"]["external"],
        "note": ("external (gdrive) files are recorded in "
                 "_meta/external-references.jsonl and never fetched — Slack "
                 "holds no bytes for them, only a link. Their bytes belong "
                 "to the gdrive ingest, and their absence here is a known, "
                 "quantified exclusion, not a shortfall."),
    }


def upload_meta(blob: Blob, prefix: str, dest: Path) -> bool:
    """Every _meta file goes up with overwrite ALLOWED.

    These are run bookkeeping, not client corpus data, and manifest.json plus
    objects.jsonl are exactly what verify treats as authoritative. Uploading
    them create-only would mean a `--limit-files` pilot's manifest is never
    replaced and verify certifies against the pilot forever (the
    github-pilot-poisons-verify bug; every ingest since ships the fix).
    """
    ok = True
    meta = dest / "_meta"
    if not meta.exists():
        return False
    for path in sorted(meta.iterdir()):
        if not path.is_file():
            continue
        ctype = ("application/json" if path.suffix == ".json"
                 else "application/x-ndjson")
        try:
            body = path.read_bytes()
            if len(body) > SINGLE_PUT_MAX:
                blob.put_large(f"{prefix}/_meta/{path.name}", body, ctype)
            else:
                blob.put_bytes(f"{prefix}/_meta/{path.name}", body, ctype,
                               create_only=False)
            log(f"upload: _meta/{path.name} "
                f"({human_bytes(path.stat().st_size)})")
        except (CopyError, OSError) as e:
            log(f"upload: FAILED _meta/{path.name}: {e}")
            ok = False
    return ok


# ── main ────────────────────────────────────────────────────────────────────

def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def main() -> int:
    """Fully env-driven -- no argv, per the secrets-never-touch-argv rule and
    because slack_transfer.py launches this with no arguments at all."""
    dest_url = os.environ.get("DEST_URL", "").strip()
    dest_sas = os.environ.get("DEST_SAS", "").strip()
    prefix = (os.environ.get("DEST_PREFIX", "").strip()
              or DEFAULT_DEST_PREFIX).strip("/")
    if not (dest_url and dest_sas):
        log("FATAL: DEST_URL/DEST_SAS not in environment "
            "(dest-slack.env not sourced?)")
        return 1
    kind = (os.environ.get("SLACK_EXPORT_KIND") or "").strip().lower()
    export_blob = os.environ.get("SLACK_EXPORT_BLOB", "").strip()
    export_prefix = os.environ.get("SLACK_EXPORT_PREFIX", "").strip()
    if kind not in ("zip", "tree"):
        log("FATAL: SLACK_EXPORT_KIND must be 'zip' or 'tree' "
            "(run discover-export, then write-dest)")
        return 1
    bearer = os.environ.get("SLACK_TOKEN", "").strip() or None
    renditions = _env_flag("RENDITIONS", True)
    args = {"shard_size": os.environ.get("SHARD_SIZE"),
            "copy_workers": os.environ.get("COPY_WORKERS"),
            "limit_files": os.environ.get("LIMIT_FILES"),
            "rps_files": os.environ.get("RPS_FILES")}

    base = Path(os.path.expanduser("~/xfer-slack"))
    dest = base / "dest"
    dest.mkdir(parents=True, exist_ok=True)
    blob = Blob(dest_url, dest_sas)
    started = utc_now_iso()

    # -- phase A: materialize the export ------------------------------------
    write_progress(dest, blob, prefix, "export", 0, "opening the export")
    try:
        if kind == "zip":
            local = base / "export.zip"
            if not local.exists() or local.stat().st_size == 0:
                log(f"export: downloading {export_blob}")
                got = blob.download(export_blob, local)
                log(f"export: {human_bytes(got)} on disk")
            else:
                log(f"export: reusing {local} "
                    f"({human_bytes(local.stat().st_size)})")
            export = ZipExport(local)
        else:
            log(f"export: reading the extracted tree under {export_prefix!r}")
            export = TreeExport(blob, export_prefix)
    except (CopyError, zipfile.BadZipFile, OSError) as e:
        log(f"FATAL: cannot open the export: {e}")
        return 1
    log(f"export: {export.describe()}")

    # -- phase B: ledger -----------------------------------------------------
    index_path = dest / "_meta" / "objects.jsonl"
    ledger_meta = base / "ledger.json"   # VM scratch, never uploaded
    if index_path.exists() and ledger_meta.exists():
        log("ledger: reusing the ledger from a previous pass on this VM")
        ledger = json.loads(ledger_meta.read_text())
    else:
        log("ledger: walking the export (this is the slow, one-time pass)")
        ledger = build_ledger(
            export, dest, renditions,
            progress=lambda d, t: write_progress(
                dest, blob, prefix, "ledger", d, f"{d}/{t} day files"))
        ledger_meta.write_text(json.dumps(ledger, indent=2))
    counts = ledger["counts"]
    log(f"ledger: {counts['unique_files']} unique files "
        f"({counts['hosted']} hosted, {counts['external']} external, "
        f"{counts['unavailable']} unavailable), "
        f"{counts['renditions']} renditions, "
        f"{counts['objects']} objects to copy, "
        f"{human_bytes(ledger['declared_bytes']['hosted'])} declared")

    token = ledger.get("export_token")
    if not token and not bearer:
        log("FATAL: the export carries no download token on any file URL and "
            "no SLACK_TOKEN override was written. Either the export was "
            "produced without file links, or its links were stripped. Run "
            "probe on the laptop for the diagnosis.")
        return 1
    (dest / "_meta" / "export-source.json").write_text(json.dumps({
        "export": export.describe(),
        "slack_manifest": ledger.get("slack_manifest"),
        "auth": "client-token" if bearer else "export-embedded-token",
        "renditions": renditions,
        "read_utc": utc_now_iso()}, indent=2))

    # -- phase C: copy -------------------------------------------------------
    context = {"export": export.describe(), "dest_prefix": prefix,
               "renditions": renditions,
               "limit_files": int(args["limit_files"] or 0) or None,
               "auth": "client-token" if bearer else "export-embedded-token"}
    rc = 0
    try:
        copy_out = run_copy(blob, prefix, dest, index_path, token, bearer,
                            args)
    except TokenDead as e:
        log(f"FATAL: {e}")
        write_progress(dest, blob, prefix, "failed", 0, str(e))
        return 1
    except CopyError as e:
        log(f"FATAL: {e}")
        return 1

    patch_statuses(index_path, copy_out["status_by_blob"])
    patch_statuses(dest / "_meta" / "files-index.jsonl",
                   copy_out["status_by_blob"])
    manifest = build_manifest(context, ledger, copy_out, started,
                              utc_now_iso())
    (dest / "_meta" / "manifest.json").write_text(
        json.dumps(manifest, indent=2))
    if not upload_meta(blob, prefix, dest):
        rc = 1
    totals = copy_out["totals"]
    log(f"done: copied={totals['copied']} streamed={totals['streamed']} "
        f"present={totals['already_present']} failed={totals['failed']} "
        f"bytes={human_bytes(totals['copied_bytes'])}")
    write_progress(dest, blob, prefix, "done", totals["copied"],
                   "pass finished — run verify on the laptop")
    if manifest["failed_units"]:
        log(f"{len(manifest['failed_units'])} shard(s) incomplete — re-run "
            "transfer to mop up (writes are create-only, so it is safe)")
        rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
