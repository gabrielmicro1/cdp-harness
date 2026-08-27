#!/usr/bin/env python3
"""corpus_sizer_rest.py — dependency-free per-source corpus sizer (portable).

Ported from the proven corpus-transfer-engine sizer (croplabel / webspiders /
latchel runs). Uses ONLY the Python standard library plus a SAS token — no
azure-storage-blob, no managed identity, no SDKs. It only talks to the storage
account, so it runs anywhere with HTTPS reach: this laptop (the harness
default — phases.py launches it as a detached local process) or any VM.

Env:
  SA                 storage account name
  CONTAINER          container name
  AZURE_STORAGE_SAS  a SAS with read+list (rl) on the account/container
  TAG                short label for output files (default: CONTAINER)
  OUT_DIR            directory for output files (default /var/tmp)
  MAX_ZIP_ENTRIES    safety cap on entries parsed per zip (default 5_000_000)
  SIZER_WORKERS      zip/gz range-read worker threads (default 16)
  LIST_WORKERS       concurrent top-level-prefix listers (default 8)
  CACHE_FILE         optional cache file path (default "")
  SEED_TSV           optional seed TSV path (default "")
  EXPECTED_SERVICES  comma-separated service names (default "")
  GZ_STREAM_THRESHOLD compressed-byte size at/above which a gz blob is always
                      exact-streamed, regardless of trailer plausibility
                      (default 256_000_000)
  GZ_STREAM_FLOOR_MIN minimum compressed size before a floored/garbage-trailer
                      gz blob is worth exact-streaming (default 8_000_000)
  GZ_STREAM_BUDGET   per-run cap on compressed bytes spent exact-streaming gz
                      blobs (default 50_000_000_000); 0 disables streaming
                      entirely (and cache staleness re-checks with it)
  DEEP_VERIFY        "1" = deep-verify mode: stream-decompress EVERY compressed
                      blob (zip, gz, bz2, xz) and measure exact uncompressed
                      sizes instead of trusting zip central directories / gz
                      trailers. Forces GZ_STREAM_THRESHOLD=0, FLOOR_MIN=0 and
                      an uncapped budget. Emits a "verification" coverage block
                      in summary.json. Intended to run on an in-region VM
                      (bulk download); results cache by ETag so repeats are
                      listing-only. Default "0" (shallow — daily behavior).

Writes in OUT_DIR:
  <TAG>.log           progress
  <TAG>.sizes.tsv     line 1: `#matcher\t<fingerprint>` header; then rows of
                      name, clen, uncomp, ratio, method, etag, det_json
  <TAG>.summary       human per-datasource table + totals (decimal GB, /1e9)
  <TAG>.summary.json  compact machine summary (what harvest reads)
  <TAG>.index.tsv.gz  incremental index (cache seed for the next run; same
                      `#matcher` header, used to fail-safe-invalidate the
                      cache when EXPECTED_SERVICES changes)
  <TAG>.done          written on clean completion

Sizing (no bulk download except budgeted gz exact-streaming — otherwise
reads only indexes/trailers):
  .zip            -> End-of-Central-Directory + Central Directory via Range GETs
                     (ZIP64-aware); sum entry uncompressed sizes. NON-recursive.
  .gz/.tgz/.tar.gz-> 4-byte ISIZE trailer, floored at compressed size (exact
                     <4 GiB, sane lower bound above). Methods: gz-tiny (no
                     trailer to read), gz-trailer (plausible), gz-floor
                     (ISIZE < clen: wrap/multi-member/incompressible),
                     gz-bad-trailer (ISIZE implausible vs DEFLATE's ~1032:1
                     bound — corrupt/misnamed). Big or floored/garbage-trailer
                     blobs are exact-streamed (decompressed member-by-member,
                     counting output bytes) under a per-run byte budget —
                     method becomes gz-exact; a failed stream falls back to
                     the trailer value, counted uncertain. See GZ_STREAM_*.
  .bz2/.tbz2      -> shallow: content-length (no cheap trailer exists); deep:
  .xz/.txz           exact multi-stream decompress (stdlib bz2/lzma). Methods:
                     bz2-stored/xz-stored (untouched), bz2-exact/xz-exact
                     (streamed), bz2-truncated/xz-truncated (ends mid-stream;
                     exact partial count).
  .parquet        -> footer FileMetaData (thrift compact) via 1–2 Range GETs;
                     sum column chunks' total_uncompressed_size (decompressed
                     PAGE bytes — codecs undone, dict/RLE encodings intact).
                     Same trust class as a zip CD, in BOTH modes: no stdlib
                     codec for the pages, so deep verify can't stream them.
                     Methods: parquet-footer, parquet-tiny, parquet-encrypted,
                     parquet-bad-magic, parquet-bad-footer (last three floored
                     to stored size).
  .7z/.rar/.zst   -> stored size (no stdlib codec); deep verify counts them
                     as an "unmeasurable format" residual, never silently.
  everything else -> uncompressed = content-length (accurate for loose files).
Read-only; writes nothing back to the blob.
"""
import bz2
import gzip
import hashlib
import json
import lzma
import os
import queue
import re
import struct
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── config (import-safe: real values come from _init_from_env in main) ───────
SA = CONTAINER = SAS = TAG = ""
MAX_ZIP_ENTRIES = 5_000_000
SIZER_WORKERS = 16      # zip/gz range-read worker threads
LIST_WORKERS = 8        # concurrent top-level-prefix listers
CACHE_FILE = SEED_TSV = ""
EXPECTED_SERVICES: tuple = ()
GZ_STREAM_THRESHOLD = 256_000_000
GZ_STREAM_FLOOR_MIN = 8_000_000
GZ_STREAM_BUDGET = 50_000_000_000
DEEP_VERIFY = False
BASE = LOG = OUT = SUMMARY = SUMMARY_JSON = DONE = INDEX = ""


def _init_from_env():
    global SA, CONTAINER, SAS, TAG, MAX_ZIP_ENTRIES, SIZER_WORKERS, LIST_WORKERS
    global CACHE_FILE, SEED_TSV, EXPECTED_SERVICES
    global GZ_STREAM_THRESHOLD, GZ_STREAM_FLOOR_MIN, GZ_STREAM_BUDGET
    global DEEP_VERIFY
    global BASE, LOG, OUT, SUMMARY, SUMMARY_JSON, DONE, INDEX
    SA = os.environ["SA"]
    CONTAINER = os.environ["CONTAINER"]
    SAS = os.environ.get("AZURE_STORAGE_SAS") or os.environ["SAS"]
    TAG = os.environ.get("TAG", CONTAINER)
    MAX_ZIP_ENTRIES = int(os.environ.get("MAX_ZIP_ENTRIES", "5000000"))
    SIZER_WORKERS = int(os.environ.get("SIZER_WORKERS", "16"))
    LIST_WORKERS = int(os.environ.get("LIST_WORKERS", "8"))
    CACHE_FILE = os.environ.get("CACHE_FILE", "")
    SEED_TSV = os.environ.get("SEED_TSV", "")
    EXPECTED_SERVICES = tuple(
        s.strip() for s in os.environ.get("EXPECTED_SERVICES", "").split(",")
        if s.strip())
    GZ_STREAM_THRESHOLD = int(os.environ.get("GZ_STREAM_THRESHOLD", "256000000"))
    GZ_STREAM_FLOOR_MIN = int(os.environ.get("GZ_STREAM_FLOOR_MIN", "8000000"))
    GZ_STREAM_BUDGET = int(os.environ.get("GZ_STREAM_BUDGET", "50000000000"))
    DEEP_VERIFY = os.environ.get("DEEP_VERIFY", "0").lower() not in ("", "0", "false")
    if DEEP_VERIFY:
        # deep mode measures everything: gz knobs forced so the existing gz
        # streaming machinery covers every gz blob; budget effectively uncapped
        # (StreamBudget stays in place purely as the egress-accounting ledger)
        GZ_STREAM_THRESHOLD = 0
        GZ_STREAM_FLOOR_MIN = 0
        GZ_STREAM_BUDGET = 1 << 63
    BASE = os.path.join(os.environ.get("OUT_DIR", "/var/tmp"), TAG)
    LOG, OUT = f"{BASE}.log", f"{BASE}.sizes.tsv"
    SUMMARY, SUMMARY_JSON = f"{BASE}.summary", f"{BASE}.summary.json"
    DONE, INDEX = f"{BASE}.done", f"{BASE}.index.tsv.gz"


_LOG_LOCK = threading.Lock()


def logmsg(m):
    with _LOG_LOCK, open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [{TAG}] {m}\n")


def _auth(url):
    return url + ("&" if "?" in url else "?") + SAS


def http_get(url, extra_headers=None):
    hdrs = {"x-ms-version": "2021-08-06"}
    if extra_headers:
        hdrs.update(extra_headers)
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(_auth(url), headers=hdrs)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1 + attempt)
    raise last


def blob_base(name):
    return f"https://{SA}.blob.core.windows.net/{CONTAINER}/{urllib.parse.quote(name, safe='/')}"


def blob_kind(name):
    lname = name.lower()
    if lname.endswith(".zip"):
        return "zip"
    if lname.endswith((".tar.gz", ".tgz", ".gz")):
        return "gz"
    if lname.endswith((".tar.bz2", ".tbz2", ".bz2")):
        return "bz2"
    if lname.endswith((".tar.xz", ".txz", ".xz")):
        return "xz"
    if lname.endswith(".parquet"):
        return "parquet"
    return "stored"


# Compressed formats with no stdlib codec: sized at stored bytes in both
# modes, but deep verify buckets them as an "unmeasurable format" residual in
# the verification stats so the shortfall is quantified, never silent.
UNMEASURABLE_EXTS = (".7z", ".rar", ".zst", ".tzst")


def unmeasurable_ext(name):
    lname = name.lower()
    for ext in UNMEASURABLE_EXTS:
        if lname.endswith(ext):
            return ext
    return None


# ── per-blob cache (blob-index) ──────────────────────────────────────────────
# Rows exist for compressed-kind blobs (plain stored blobs cost nothing to
# size — the listing already carries their size). Error rows are never cached,
# so transient failures retry next run. A cache can only cost time, never
# correctness: any doubt → miss. bz2/xz rows exist so a deep run's exact
# measurements replay on later shallow runs.
CACHEABLE_KINDS = ("zip", "gz", "bz2", "xz", "parquet")


def _check_header(line, fingerprint):
    """First line of a cache/seed file may be a `#matcher\\t<fp>` header.
    Returns (is_header, ok): ok is False when a fingerprint was demanded and
    the header is missing or doesn't match — the caller must then treat the
    WHOLE file as a miss (fail-safe: stale zip-entry detection is worse than
    a slower re-read)."""
    if line.startswith("#matcher\t"):
        if fingerprint is not None and line.split("\t", 1)[1] != fingerprint:
            return True, False
        return True, True
    if fingerprint is not None:
        return False, False  # no header at all but a fingerprint was demanded
    return False, True  # headerless + no fingerprint demanded → back-compat


def _det_valid(det):
    """Validate a cached det_json field at LOAD time. Empty is valid (no
    embedded-service hits); non-empty must parse as a JSON object mapping
    service name -> a 2-element [bytes, count] list/tuple of ints. A cache
    can only cost time, never correctness: any parse or shape failure means
    the row is treated as a miss here, rather than being cached and later
    raising in the consumer (which would fail the whole run and, since the
    poisoned CACHE_FILE gets reused on every relaunch, keep failing it)."""
    if not det:
        return True
    try:
        obj = json.loads(det)
    except (TypeError, ValueError):
        return False
    if not isinstance(obj, dict):
        return False
    for k, v in obj.items():
        if not isinstance(k, str):
            return False
        if (not isinstance(v, (list, tuple)) or len(v) != 2
                or any(isinstance(x, bool) or not isinstance(x, int)
                       for x in v)):
            return False
    return True


def load_cache(path, fingerprint=None):
    """blob-index.tsv.gz → {name: (etag, clen, uncomp, method, det_json)}.
    See `_check_header` for the optional `#matcher` freshness check."""
    out = {}
    if not path:
        return out
    try:
        with gzip.open(path, "rt", newline="") as f:
            for i, raw in enumerate(f):
                line = raw.rstrip("\n")
                if i == 0:
                    is_header, ok = _check_header(line, fingerprint)
                    if not ok:
                        return {}
                    if is_header:
                        continue
                if line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 6:
                    continue
                name, etag, clen, uncomp, method, det = parts[:6]
                if method.startswith("err:") or not etag:
                    continue
                if not _det_valid(det):
                    continue
                out[name] = (etag, int(clen), int(uncomp), method, det)
    except Exception:  # noqa: BLE001 — corrupt cache = no cache
        return {}
    return out


def load_seed_tsv(path, fingerprint=None):
    """A crashed run's partial sizes.tsv as a second seed. TSV columns:
    name, clen, uncomp, ratio, method, etag, det_json (rows from older TSVs
    without the etag column are skipped). See `_check_header` for the
    optional `#matcher` freshness check."""
    out = {}
    if not path:
        return out
    try:
        with open(path, newline="") as f:
            for i, raw in enumerate(f):
                line = raw.rstrip("\n")
                if i == 0:
                    is_header, ok = _check_header(line, fingerprint)
                    if not ok:
                        return {}
                    if is_header:
                        continue
                if line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                name, clen, uncomp, _ratio, method, etag, det = parts[:7]
                if (method.startswith("err:") or not etag
                        or blob_kind(name) not in CACHEABLE_KINDS):
                    continue
                if not _det_valid(det):
                    continue
                out[name] = (etag, int(clen), int(uncomp), method, det)
    except Exception:  # noqa: BLE001
        return {}
    return out


def cache_lookup(cache, name, etag, clen):
    """Hit only when name AND etag AND compressed size all match."""
    row = cache.get(name)
    if row and etag and row[0] == etag and row[1] == clen:
        return row
    return None


def write_index(path, rows, fingerprint):
    tmp_path = str(path) + ".tmp"
    with gzip.open(tmp_path, "wt", newline="") as f:
        f.write(f"#matcher\t{fingerprint}\n")
        for r in rows:
            f.write("\t".join(map(str, r)) + "\n")
    os.replace(tmp_path, path)


def parse_list_page(xml_bytes):
    """One list-blobs response page → (blobs, prefixes, next_marker).
    blobs are (name, content_length, etag); prefixes only appear on
    delimiter listings."""
    root = ET.fromstring(xml_bytes)
    blobs = []
    for b in root.findall(".//Blob"):
        blobs.append((b.findtext("Name") or "",
                      int(b.findtext(".//Content-Length") or "0"),
                      b.findtext(".//Etag") or ""))
    prefixes = [p.findtext("Name") or "" for p in root.findall(".//BlobPrefix")]
    return blobs, prefixes, root.findtext("NextMarker") or ""


# ── service detection ────────────────────────────────────────────────────────
# Canonical service → normalized aliases. Aliases are deliberately specific
# (no "mail", "code", "box" — too generic); a company's DECLARED services are
# always added on top via build_matcher, so company-specific names match
# regardless of this catalog.
SERVICE_CATALOG = {
    "gdrive": ("gdrive", "googledrive", "drive", "takeout"),
    "gmail": ("gmail", "googlemail"),
    "gcal": ("gcal", "googlecalendar"),
    "slack": ("slack",),
    "hubspot": ("hubspot",),
    "salesforce": ("salesforce", "sfdc"),
    "zendesk": ("zendesk",),
    "notion": ("notion",),
    "jira": ("jira",),
    "confluence": ("confluence",),
    "github": ("github",),
    "gitlab": ("gitlab",),
    "figma": ("figma",),
    "asana": ("asana",),
    "intercom": ("intercom",),
    "stripe": ("stripe",),
    "quickbooks": ("quickbooks",),
    "dropbox": ("dropbox",),
    "sharepoint": ("sharepoint",),
    "onedrive": ("onedrive",),
    "teams": ("msteams", "microsoftteams"),
    "zoom": ("zoom",),
    "airtable": ("airtable",),
}

_NORM_RE = re.compile(r"[^a-z0-9]")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def norm_seg(s):
    """Same normalization convention as reconcile.norm (duplicated: the sizer
    is standalone by design)."""
    return _NORM_RE.sub("", s.lower())


def build_matcher(declared=()):
    """normalized alias → canonical name. Declared manifest services map to
    the manifest's own spelling and override catalog aliases, so reconcile's
    norm()-matching lines up exactly."""
    m = {}
    for canon, aliases in SERVICE_CATALOG.items():
        for a in aliases:
            m[a] = canon
    for svc in declared:
        n = norm_seg(svc)
        if n:
            m[n] = svc
    return m


def matcher_fingerprint(matcher):
    """Short stable hash of a built matcher. Changes whenever EXPECTED_SERVICES
    (or the service catalog) changes the matcher's content, so cached
    zip-entry detection can be fail-safe-invalidated instead of silently
    going stale when a company's declared services change."""
    payload = json.dumps(sorted(matcher.items()), separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def match_segment(seg, matcher):
    n = norm_seg(seg)
    if n in matcher:
        return matcher[n]
    for tok in _TOKEN_RE.split(seg.lower()):
        if tok and tok in matcher:
            return matcher[tok]
    return None


def match_path(path, matcher, max_depth=3):
    """Deepest matching candidate wins — one service per path, so per-service
    attribution stays disjoint. Candidates: first max_depth segments, plus
    the filename when the path is deeper than max_depth."""
    segs = [s for s in path.split("/") if s]
    candidates = segs[:max_depth]
    if len(segs) > max_depth:
        candidates = candidates + [segs[-1]]
    for seg in reversed(candidates):
        hit = match_segment(seg, matcher)
        if hit:
            return hit
    return None


def l2_key(name):
    segs = name.split("/")
    if len(segs) >= 3:
        return f"{segs[0]}/{segs[1]}"
    if len(segs) == 2:
        return f"{segs[0]}/(files)"
    return "(root)"


def rollup_l2(l2, cap=40):
    """Top cap second-level prefixes by uncompressed bytes; rest → '(other)'.
    Values are [files, comp, unc] triples."""
    items = sorted(l2.items(), key=lambda kv: -kv[1][2])
    out = {k: list(v) for k, v in items[:cap]}
    rest = items[cap:]
    if rest:
        other = [0, 0, 0]
        for _, v in rest:
            other[0] += v[0]
            other[1] += v[1]
            other[2] += v[2]
        out["(other)"] = other
    return out


def fetch_range(name, start, end):
    return http_get(blob_base(name), {"Range": f"bytes={start}-{end}"})


# ── ZIP central-directory parsing (ZIP64-aware) — ported from size_corpus.py ──
EOCD_SIG = 0x06054b50
EOCD_STRUCT = "<IHHHHIIH"
EOCD_SIZE = 22
LOC64_SIG = 0x07064b50
LOC64_STRUCT = "<IIQI"
LOC64_SIZE = 20
EOCD64_SIG = 0x06064b50
EOCD64_STRUCT = "<IQHHIIQQQQ"
EOCD64_SIZE = 56
ZIP_TAIL_RETRY = 8_000_000
CD_SIG = 0x02014b50


def _parse_cd(cd, total_entries, matcher=None, entries_out=None):
    """Walk the central directory: sum uncompressed sizes and, when a matcher
    is given, attribute matched entry paths to services (exact per-entry
    usize — zero extra requests). Returns (total_uncomp, entries_seen, svc)
    with svc = {service: [bytes, entry_count]}. When entries_out is a list,
    appends (local_header_offset, flags, method, csize, usize) per entry —
    the map zip_stream_exact walks (deep verify); zip64 saturation of csize
    and the offset is resolved from the extra field (the zip64 record holds
    values only for saturated fields, in usize/csize/offset order)."""
    total_uncomp = 0
    n = 0
    p = 0
    svc = {}
    # total_entries None = count unknown (saturated 0xFFFF EOCD without zip64
    # records — Takeout does this past 65,535 entries): walk the whole buffer
    cap = (MAX_ZIP_ENTRIES if total_entries is None
           else min(total_entries, MAX_ZIP_ENTRIES))
    sig = struct.pack("<I", CD_SIG)
    while p + 46 <= len(cd) and n < cap:
        if cd[p:p + 4] != sig:
            break
        (_s, _vm, _vn, flags, meth, _mt, _md, _crc, csize, usize,
         name_len, extra_len, cmt_len, _dn, _ia, _ea, lho) = struct.unpack(
            "<IHHHHHHIIIHHHHHII", cd[p:p + 46])
        name_end = p + 46 + name_len
        extra_end = name_end + extra_len
        if (usize == 0xFFFFFFFF or (entries_out is not None
                                    and (csize == 0xFFFFFFFF
                                         or lho == 0xFFFFFFFF))):
            ex = cd[name_end:extra_end]
            xp = 0
            while xp + 4 <= len(ex):
                xid, xlen = struct.unpack("<HH", ex[xp:xp + 4])
                if xid == 0x0001:
                    zdata = ex[xp + 4:xp + 4 + xlen]
                    zoff = 0
                    if usize == 0xFFFFFFFF and zoff + 8 <= len(zdata):
                        usize = struct.unpack("<Q", zdata[zoff:zoff + 8])[0]
                        zoff += 8
                    if csize == 0xFFFFFFFF and zoff + 8 <= len(zdata):
                        csize = struct.unpack("<Q", zdata[zoff:zoff + 8])[0]
                        zoff += 8
                    if lho == 0xFFFFFFFF and zoff + 8 <= len(zdata):
                        lho = struct.unpack("<Q", zdata[zoff:zoff + 8])[0]
                        zoff += 8
                    break
                xp += 4 + xlen
        if matcher is not None:
            raw = cd[p + 46:name_end]
            try:
                ename = raw.decode("utf-8")
            except UnicodeDecodeError:
                ename = raw.decode("cp437", errors="replace")
            hit = match_path(ename, matcher)
            if hit:
                rec = svc.setdefault(hit, [0, 0])
                rec[0] += usize
                rec[1] += 1
        if entries_out is not None:
            entries_out.append((lho, flags, meth, csize, usize))
        total_uncomp += usize
        n += 1
        p = extra_end + cmt_len
    return total_uncomp, n, svc


def _eocd64_scan(tail, idx, clen):
    """Locate the EOCD64 record by its own signature in the tail (fallback for
    absent/corrupt zip64 locators — seen on Takeout part files). Scans only
    bytes before the EOCD at idx; returns (entries, cd_size, cd_off) or None."""
    sig = struct.pack("<I", EOCD64_SIG)
    p = tail.rfind(sig, 0, idx)
    while p >= 0:
        if p + EOCD64_SIZE <= len(tail):
            (_s, _sz, _v, _vn, _d, _cds, _etd, entries, cd_size,
             cd_off) = struct.unpack(EOCD64_STRUCT, tail[p:p + EOCD64_SIZE])
            if 0 < entries and cd_size and cd_off + cd_size <= clen:
                return entries, cd_size, cd_off
        p = tail.rfind(sig, 0, p)
    return None


def zip_uncompressed(name, clen, matcher=None, entries_out=None):
    """Return (uncompressed_bytes, note, svc). Range-reads only EOCD + CD.
    entries_out (a list, deep verify) receives the per-entry stream map from
    _parse_cd; the fallback paths that never reach the CD leave it empty."""
    if clen < EOCD_SIZE:
        return clen, "zip-tiny", {}  # empty/placeholder blob — nothing to read
    tail_sizes = [min(clen, 65557)]
    if clen > 65557:
        tail_sizes.append(min(clen, ZIP_TAIL_RETRY))  # oversized-comment/junk tails
    idx = -1
    for tail_size in tail_sizes:
        tail = fetch_range(name, clen - tail_size, clen - 1)
        idx = tail.rfind(struct.pack("<I", EOCD_SIG))
        if idx >= 0:
            break
    if idx < 0:
        return clen, "zip-no-eocd", {}
    (_sig, _disk, _cds, _etd, total_entries, cd_size, cd_off, _cl) = struct.unpack(
        EOCD_STRUCT, tail[idx:idx + EOCD_SIZE])
    if total_entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        loc_start = idx - LOC64_SIZE
        rec = None
        if loc_start >= 0:
            (lsig, _ld, eocd64_off, _nd) = struct.unpack(LOC64_STRUCT, tail[loc_start:idx])
            if lsig == LOC64_SIG:
                e64 = fetch_range(name, eocd64_off, eocd64_off + EOCD64_SIZE - 1)
                (_s64, _sz64, _v, _vn, _d64, _cds64, _etd64, entries64,
                 cd_size64, cd_off64) = struct.unpack(EOCD64_STRUCT, e64[:EOCD64_SIZE])
                if _s64 == EOCD64_SIG:
                    rec = (entries64, cd_size64, cd_off64)
        if rec is None:
            rec = _eocd64_scan(tail, idx, clen)
        if (rec is None and cd_size != 0xFFFFFFFF and cd_off != 0xFFFFFFFF
                and cd_size and cd_off + cd_size <= clen):
            # only the entry COUNT is saturated (>65,535 entries, no zip64
            # records — Takeout's style): the CD location is real, walk it
            rec = (None, cd_size, cd_off)
        if rec is None:
            return clen, ("zip-loc64-split" if loc_start < 0 else "zip-loc64-bad"), {}
        total_entries, cd_size, cd_off = rec
    if not cd_size:
        return 0, "zip:0entries", {}  # empty archive — no CD to fetch
    cd = fetch_range(name, cd_off, cd_off + cd_size - 1)
    total_uncomp, n, svc = _parse_cd(cd, total_entries, matcher, entries_out)
    note = (f"zip:{n}entries" if total_entries is None or n == total_entries
            else f"zip:partial{n}/{total_entries}")
    return total_uncomp, note, svc


DEFLATE_MAX_RATIO = 1032  # DEFLATE's hard compression bound — above = garbage


def gz_uncompressed(name, clen):
    """4-byte ISIZE trailer (uncompressed length mod 2^32). Methods:
      gz-trailer     plausible ISIZE — exact for single-member gzips < 4 GiB
      gz-floor       ISIZE < clen: a >=4GiB wrap, a multi-member gzip (trailer
                     covers only the LAST member; bgzip ends with an empty
                     one), or incompressible data — floored to clen
      gz-bad-trailer ISIZE > clen*1032 (DEFLATE cannot exceed ~1032:1) — a
                     misnamed/corrupt .gz; floored to clen instead of
                     reporting up to 4.29 GB of garbage
      gz-tiny        clen < 4 — no trailer to read
    Never streams; Task-3 streaming resolves floor/garbage cases exactly."""
    if clen < 4:
        return clen, "gz-tiny"
    trailer = fetch_range(name, clen - 4, clen - 1)
    isize = struct.unpack("<I", trailer[-4:])[0]
    if isize > clen * DEFLATE_MAX_RATIO:
        return clen, "gz-bad-trailer"
    if isize < clen:
        return clen, "gz-floor"
    return isize, "gz-trailer"


# ── Parquet footer parsing (thrift compact protocol) ─────────────────────────
# A parquet file ends [FileMetaData thrift blob][4-byte LE footer length]
# ["PAR1"]. The footer's ColumnMetaData records carry total_uncompressed_size —
# the decompressed size of every column chunk's pages. Structurally this is
# the zip-CD play: metadata read via ranged GETs, trusted rather than
# measured. Unlike zip there is no stdlib codec for the data pages (snappy),
# so deep verify cannot upgrade these rows into measurements in ANY mode —
# they are terminal on first read and permanently "trusted" in coverage.
# Semantics caveat (matters for declared-vs-actual): the total is decompressed
# PAGE bytes — compression codecs undone, but dictionary/RLE encodings intact.
# A warehouse's logical size (e.g. the BigQuery console, which prices decoded
# in-memory widths) is generally HIGHER for the same tables.
PARQUET_TAIL = 65536  # one tail GET usually covers len+magic AND the footer


class _ThriftError(ValueError):
    pass


def _tc_varint(buf, p):
    out = shift = 0
    while True:
        if p >= len(buf) or shift > 63:
            raise _ThriftError("bad varint")
        b = buf[p]
        p += 1
        out |= (b & 0x7F) << shift
        if not b & 0x80:
            return out, p
        shift += 7


def _tc_zigzag(buf, p):
    n, p = _tc_varint(buf, p)
    return (n >> 1) ^ -(n & 1), p


def _tc_value(buf, p, ttype, depth):
    """Decode one thrift-compact value of wire type ttype at buf[p]."""
    if ttype == 3:  # i8: one signed byte
        if p >= len(buf):
            raise _ThriftError("i8 past end")
        v = buf[p]
        return (v - 256 if v > 127 else v), p + 1
    if ttype in (4, 5, 6):  # i16/i32/i64: zigzag varint
        return _tc_zigzag(buf, p)
    if ttype == 7:  # double: 8 raw bytes (endianness irrelevant here)
        if p + 8 > len(buf):
            raise _ThriftError("double past end")
        return struct.unpack("<d", buf[p:p + 8])[0], p + 8
    if ttype == 8:  # binary/string: varint length + bytes
        ln, p = _tc_varint(buf, p)
        if p + ln > len(buf):
            raise _ThriftError("binary past end")
        return buf[p:p + ln], p + ln
    if ttype in (9, 10):  # list/set: (size<<4|etype), size 15 → varint
        if p >= len(buf):
            raise _ThriftError("list header past end")
        hdr = buf[p]
        p += 1
        size, etype = hdr >> 4, hdr & 0x0F
        if size == 15:
            size, p = _tc_varint(buf, p)
        if size > len(buf):  # each element costs ≥1 byte — cheap sanity bound
            raise _ThriftError("list size implausible")
        vals = []
        for _ in range(size):
            if etype in (1, 2):  # bool elements: one byte each
                if p >= len(buf):
                    raise _ThriftError("bool past end")
                vals.append(buf[p] == 1)
                p += 1
            else:
                v, p = _tc_value(buf, p, etype, depth)
                vals.append(v)
        return vals, p
    if ttype == 11:  # map: varint size, then (ktype<<4|vtype), then pairs
        size, p = _tc_varint(buf, p)
        if size == 0:
            return {}, p
        if p >= len(buf) or size > len(buf):
            raise _ThriftError("map header past end")
        kt, vt = buf[p] >> 4, buf[p] & 0x0F
        p += 1
        out = {}
        for _ in range(size):
            k, p = _tc_value(buf, p, kt, depth)
            v, p = _tc_value(buf, p, vt, depth)
            out[k if not isinstance(k, (bytes, dict, list)) else str(k)] = v
        return out, p
    if ttype == 12:  # struct
        return _tc_struct(buf, p, depth + 1)
    raise _ThriftError(f"wire type {ttype}")


def _tc_struct(buf, p, depth=0):
    """Parse a thrift-compact struct into {field_id: value}. Field header
    byte: (id delta << 4) | type; delta 0 → long form (zigzag id follows);
    type 0 = STOP. Bool field values live in the type nibble itself."""
    if depth > 16:
        raise _ThriftError("nesting too deep")
    out = {}
    fid = 0
    while True:
        if p >= len(buf):
            raise _ThriftError("struct past end")
        byte = buf[p]
        p += 1
        if byte == 0:
            return out, p
        delta, ttype = byte >> 4, byte & 0x0F
        if delta:
            fid += delta
        else:
            fid, p = _tc_zigzag(buf, p)
        if ttype == 1:
            out[fid] = True
        elif ttype == 2:
            out[fid] = False
        else:
            out[fid], p = _tc_value(buf, p, ttype, depth)


def parquet_uncompressed(name, clen):
    """Return (uncompressed_bytes, method). Reads the footer only: one tail
    range GET; a second exact GET only when the footer outgrows the tail.
    Sums ColumnMetaData.total_uncompressed_size (field 6) across every row
    group's column chunks, falling back to RowGroup.total_byte_size (field 2)
    for a row group whose column metadata is absent or malformed. Methods:
      parquet-footer     footer parsed — decompressed page bytes
      parquet-tiny       clen < 12: can't be a parquet file — stored size
      parquet-encrypted  encrypted footer (PARE magic) — stored size
      parquet-bad-magic  tail magic isn't PAR1: misnamed file — stored size
      parquet-bad-footer footer length insane or thrift unparseable — stored"""
    if clen < 12:  # magic + len + magic minimum
        return clen, "parquet-tiny"
    tail = fetch_range(name, max(0, clen - PARQUET_TAIL), clen - 1)
    magic = tail[-4:]
    if magic == b"PARE":
        return clen, "parquet-encrypted"
    if magic != b"PAR1":
        return clen, "parquet-bad-magic"
    flen = struct.unpack("<I", tail[-8:-4])[0]
    if not flen or flen + 8 > clen:
        return clen, "parquet-bad-footer"
    if flen + 8 <= len(tail):
        footer = tail[-(flen + 8):-8]
    else:
        footer = fetch_range(name, clen - 8 - flen, clen - 9)
    try:
        meta, _ = _tc_struct(footer, 0)
        row_groups = meta.get(4, [])
        if not isinstance(row_groups, list):
            raise _ThriftError("row_groups not a list")
        total = 0
        for rg in row_groups:
            if not isinstance(rg, dict):
                raise _ThriftError("row group not a struct")
            cols = rg.get(1)
            col_total, cols_ok = 0, isinstance(cols, list) and bool(cols)
            if cols_ok:
                for col in cols:
                    md = col.get(3) if isinstance(col, dict) else None
                    sz = md.get(6) if isinstance(md, dict) else None
                    if type(sz) is not int or sz < 0:
                        cols_ok = False
                        break
                    col_total += sz
            if cols_ok:
                total += col_total
            else:
                tbs = rg.get(2)
                if type(tbs) is not int or tbs < 0:
                    raise _ThriftError("row group has no usable size")
                total += tbs
        return total, "parquet-footer"
    except (ValueError, struct.error, IndexError, OverflowError) as exc:
        logmsg(f"parquet footer unparseable {name}: "
               f"{type(exc).__name__}: {str(exc)[:80]}")
        return clen, "parquet-bad-footer"


def stream_blob_chunks(name, chunk=1 << 20):
    """Chunked GET of a whole blob. Deliberately separate from http_get
    (which buffers entire bodies) — streaming holds one chunk in memory."""
    req = urllib.request.Request(_auth(blob_base(name)),
                                 headers={"x-ms-version": "2021-08-06"})
    with urllib.request.urlopen(req, timeout=90) as r:
        while True:
            b = r.read(chunk)
            if not b:
                return
            yield b


_DECOMP_STEP = 64 << 20  # bound per decompress call — caps transient memory


class TruncatedStream(ValueError):
    """The blob ends mid-member/mid-stream (a truncated upload). .partial
    carries the exact byte count decompressed before the cut — the true
    logical size of the content that actually exists in storage."""

    def __init__(self, partial):
        super().__init__("truncated compressed stream")
        self.partial = partial


TruncatedGzStream = TruncatedStream  # back-compat alias (pre-deep-verify name)


class StreamDecodeError(ValueError):
    """Deterministic decode failure (corrupt/misnamed bytes, or a blob whose
    layout contradicts its own index). ValueError subclass so the retry
    helper's decode-vs-transport split treats it as terminal."""


def gz_stream_exact(name):
    """Exact uncompressed size: stream-decompress every gzip member,
    counting output bytes only (bounded memory: <= _DECOMP_STEP per step,
    regardless of the input chunk's compression ratio). Handles the two
    cases the trailer cannot: >=4GiB wraps and multi-member/concatenated
    gzips. Network cost = compressed size — paid once; the result is
    cached by ETag as method gz-exact. Raises on truncated or non-gzip
    input."""
    total = 0
    d = zlib.decompressobj(wbits=31)  # 31 = gzip container
    for chunk in stream_blob_chunks(name):
        while chunk:
            if d.eof:  # previous member finished — start the next
                d = zlib.decompressobj(wbits=31)
            total += len(d.decompress(chunk, _DECOMP_STEP))
            while d.unconsumed_tail and not d.eof:
                total += len(d.decompress(d.unconsumed_tail, _DECOMP_STEP))
            chunk = d.unused_data if d.eof else b""
    if not d.eof:
        raise TruncatedStream(total)
    return total


def _stream_exact_multi(name, new_decomp):
    """Exact uncompressed size for bz2/xz: stream-decompress every stream in
    the blob (concatenated streams are legal in both formats), counting
    output bytes only, memory bounded by _DECOMP_STEP. `new_decomp` is
    bz2.BZ2Decompressor or lzma.LZMADecompressor — they share the
    decompress(data, max_length)/.eof/.unused_data/.needs_input API (which
    differs from zlib's unconsumed_tail, hence a sibling of gz_stream_exact
    rather than a replacement). Decode failures raise StreamDecodeError;
    a blob that ends mid-stream raises TruncatedStream(partial)."""
    total = 0
    d = new_decomp()
    started = False
    for chunk in stream_blob_chunks(name):
        data = chunk
        while data:
            if d.eof:
                # xz stream padding (null bytes between/after streams) is
                # legal; nulls can never start a real bz2/xz stream
                data = data.lstrip(b"\x00")
                if not data:
                    break
                d = new_decomp()
            try:
                out = d.decompress(data, _DECOMP_STEP)
                total += len(out)
                while not d.eof and not d.needs_input:
                    step = d.decompress(b"", _DECOMP_STEP)
                    if not step:
                        break
                    total += len(step)
            except Exception as exc:
                raise StreamDecodeError(f"{type(exc).__name__}: {exc}") from exc
            started = True
            data = d.unused_data if d.eof else b""
    if started and not d.eof:
        raise TruncatedStream(total)
    if not started:
        raise StreamDecodeError("empty blob — nothing to decompress")
    return total


class _ByteCursor:
    """Forward-only cursor over a chunk generator with absolute positions —
    the transport zip_stream_exact walks. Unexpected EOF (the blob is shorter
    than its central directory claims) raises StreamDecodeError, which the
    retry helper treats as terminal."""

    def __init__(self, gen):
        self.gen = gen
        self.buf = b""
        self.off = 0   # consumed bytes within buf
        self.pos = 0   # absolute bytes consumed so far

    def _fill(self):
        try:
            self.buf = next(self.gen)
            self.off = 0
        except StopIteration:
            raise StreamDecodeError("blob shorter than its central directory "
                                    "claims") from None

    def skip_to(self, abs_pos):
        if abs_pos < self.pos:
            raise StreamDecodeError("central directory offsets run backwards")
        self.skip(abs_pos - self.pos)

    def skip(self, n):
        while n:
            if self.off >= len(self.buf):
                self._fill()
            take = min(n, len(self.buf) - self.off)
            self.off += take
            self.pos += take
            n -= take

    def read(self, n):
        return b"".join(self.iter_read(n))

    def iter_read(self, n):
        while n:
            if self.off >= len(self.buf):
                self._fill()
            take = min(n, len(self.buf) - self.off)
            piece = self.buf[self.off:self.off + take]
            self.off += take
            self.pos += take
            n -= take
            yield piece

    def close(self):
        self.gen.close()


LOCAL_HDR_SIG = struct.pack("<I", 0x04034b50)


def zip_stream_exact(name, entries):
    """Deep verify: measure a zip by decompressing its entries in ONE
    sequential GET, walking the local entries in offset order (gap-skipping
    makes data descriptors and archive-decoration bytes irrelevant).
    Returns (measured_total, cd_trusted_entries): deflate entries are
    inflated and counted; stored entries count their csize (measured by
    construction); encrypted / unsupported-method / per-entry decode
    failures fall back to the CD's usize for that entry and count as
    cd-trusted. Blob-level layout contradictions raise StreamDecodeError
    (the caller falls back to the CD total)."""
    cur = _ByteCursor(stream_blob_chunks(name))
    total = 0
    trusted = 0
    try:
        for lho, flags, meth, csize, usize in sorted(entries):
            cur.skip_to(lho)
            hdr = cur.read(30)
            if hdr[:4] != LOCAL_HDR_SIG:
                raise StreamDecodeError("bad local header signature")
            nlen, xlen = struct.unpack("<HH", hdr[26:30])
            cur.skip(nlen + xlen)
            if flags & 0x1 or meth not in (0, 8):
                cur.skip(csize)     # encrypted or unsupported compression
                total += usize
                trusted += 1
                continue
            if meth == 0:           # stored — the csize bytes ARE the content
                cur.skip(csize)
                total += csize
                continue
            d = zlib.decompressobj(wbits=-15)  # raw deflate
            out = 0
            try:
                for piece in cur.iter_read(csize):
                    out += len(d.decompress(piece, _DECOMP_STEP))
                    while d.unconsumed_tail and not d.eof:
                        out += len(d.decompress(d.unconsumed_tail,
                                                _DECOMP_STEP))
                out += len(d.flush())
            except zlib.error:
                # this entry is corrupt: trust its CD usize, realign at the
                # next entry's offset (skip_to handles the leftover bytes)
                total += usize
                trusted += 1
                continue
            total += out
    finally:
        cur.close()  # the CD tail is deliberately left unread
    return total, trusted


class StreamBudget:
    """Per-run cap on compressed bytes downloaded for exact gz sizing.
    reserve() BEFORE downloading so concurrent workers cannot overshoot;
    no refunds on failure (conservative — bandwidth was likely spent)."""

    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def reserve(self, n):
        with self._lock:
            if self.used + n > self.limit:
                return False
            self.used += n
            return True


def gz_stream_candidate(method, clen):
    """Should this gz blob be stream-measured exactly? Big blobs always
    (wrap risk); floored/garbage trailers above a small floor (multi-member
    risk without letting thousands of tiny bgzip files eat the budget).
    clen < 4 (gz-tiny) can never decompress — excluded even in deep mode,
    where the forced threshold of 0 would otherwise sweep it in."""
    if GZ_STREAM_BUDGET <= 0 or clen < 4:
        return False
    if clen >= GZ_STREAM_THRESHOLD:
        return True
    return method in ("gz-floor", "gz-bad-trailer") and clen >= GZ_STREAM_FLOOR_MIN


def gz_cached_row_stale(method, clen, uncomp):
    """Cache-migration rule: a cached gz row that is NOT gz-exact but that
    the CURRENT trigger would stream must be re-measured (a one-time miss).
    Pre-taxonomy rows say gz-trailer even when floored — uncomp == clen is
    the tell. When streaming is disabled the row is never stale (re-reading
    the trailer every run would gain nothing)."""
    if GZ_STREAM_BUDGET <= 0 or method in ("gz-exact", "gz-truncated"):
        return False  # gz-truncated is content-exact until the ETag changes
    if clen < 4:
        return False  # gz-tiny — nothing to stream
    if clen >= GZ_STREAM_THRESHOLD:
        return True
    floored = method in ("gz-floor", "gz-bad-trailer") or uncomp == clen
    return floored and clen >= GZ_STREAM_FLOOR_MIN


def _method_terminal(method):
    """Methods whose value can never be improved by re-measuring the same
    bytes: exact stream measurements, truncated-stream exact partials, and
    trivial cases. Terminal rows are never stale in ANY mode — this is what
    lets a shallow daily run replay deep-verify results at zero HTTP without
    ever 're-shallowing' them."""
    return (method in ("gz-exact", "gz-truncated", "bz2-exact", "bz2-truncated",
                       "xz-exact", "xz-truncated", "zip-exact",
                       "gz-tiny", "zip-tiny", "zip:0entries")
            or method.startswith("zip-exact-mismatch")
            # every parquet outcome is terminal: the footer is the best any
            # mode can do (no stdlib codec for the data pages), and the
            # fallbacks are deterministic parses of the same bytes
            or method.startswith("parquet"))


def cached_row_stale(kind, method, clen, uncomp):
    """Mode-aware staleness for a cached row. Shallow: only the existing gz
    migration rule applies (zip CD rows and bz2/xz placeholders replay as
    hits). Deep: everything non-terminal is stale — one re-measure converts
    metadata-trusted rows into measurements; a repeat deep run on an
    unchanged container is then listing-only."""
    if not DEEP_VERIFY:
        if kind == "gz":
            return gz_cached_row_stale(method, clen, uncomp)
        return False
    return not _method_terminal(method)


def gz_uncertain_row(kind, method, clen):
    """Rows whose logical size is not reliably measurable: floored/garbage
    trailers, and plausible trailers big enough that a silent >=4GiB wrap is
    possible. gz-exact and small plausible trailers are certain."""
    if kind != "gz":
        return False
    if method in ("gz-floor", "gz-bad-trailer"):
        return True
    return method == "gz-trailer" and clen >= GZ_STREAM_THRESHOLD


def _stream_with_retry(stream_fn, name, clen, budget, attempts=3):
    """Retry only transport-layer failures (network blips) — a decode
    failure (zlib.error: non-gzip/corrupt bytes; ValueError: truncated
    stream / StreamDecodeError) is deterministic and re-reading the same
    bytes can never succeed, so it's raised immediately with no retry or
    sleep. The first attempt spends the reservation `size_blob` already
    made; each RETRY must reserve its own re-download budget — if that
    reservation fails, stop and raise the last error (the caller's
    fallback handles it). `stream_fn(name)` does one full streaming pass
    (gz_stream_exact, a _stream_exact_multi wrapper, or zip_stream_exact)."""
    last = None
    for i in range(attempts):
        if i > 0 and not budget.reserve(clen):
            raise last
        try:
            return stream_fn(name)
        except (zlib.error, ValueError):
            raise  # deterministic decode failure — retrying can't help
        except Exception as exc:  # noqa: BLE001 — transport-layer, retry
            last = exc
            if i < attempts - 1:
                time.sleep(1 + i)
    raise last


def list_url(prefix=None, delimiter=None, marker=""):
    url = (f"https://{SA}.blob.core.windows.net/{CONTAINER}"
           f"?restype=container&comp=list&maxresults=5000")
    if prefix:
        url += "&prefix=" + urllib.parse.quote(prefix, safe="")
    if delimiter:
        url += "&delimiter=" + urllib.parse.quote(delimiter, safe="")
    if marker:
        url += "&marker=" + urllib.parse.quote(marker, safe="")
    return url


def discover_prefixes():
    """Delimiter listing → (top-level prefixes, root-level blobs). Dedupes
    BlobPrefix names across pages (order-preserving) — a repeat would
    otherwise double-count an entire prefix."""
    prefixes, root_blobs, marker = [], [], ""
    seen = set()
    while True:
        blobs, prefs, marker = parse_list_page(
            http_get(list_url(delimiter="/", marker=marker)))
        for p in prefs:
            if p not in seen:
                seen.add(p)
                prefixes.append(p)
        root_blobs += blobs
        if not marker:
            return prefixes, root_blobs


def size_blob(name, clen, etag, kind, matcher, budget=None):
    """Worker-pool job. Never raises — failures become err:* rows, a failed
    gz exact-stream falls back to the trailer value (counted uncertain), and
    a failed deep zip/bz2/xz stream falls back to the metadata-trusted value
    (CD total / stored size — counted in the verification trusted bucket)."""
    uncomp, method, svc, err = clen, "stored", {}, None
    try:
        if kind == "zip":
            entries = [] if DEEP_VERIFY else None
            uncomp, method, svc = zip_uncompressed(name, clen, matcher,
                                                   entries_out=entries)
            if (DEEP_VERIFY and budget is not None and entries
                    and method == f"zip:{len(entries)}entries"
                    and budget.reserve(clen)):
                cd_total = uncomp
                try:
                    streamed, trusted_n = _stream_with_retry(
                        lambda nm: zip_stream_exact(nm, entries),
                        name, clen, budget)
                    if trusted_n:
                        uncomp = streamed
                        method = f"zip-partial({trusted_n}/{len(entries)}cd)"
                    elif streamed == cd_total:
                        uncomp, method = streamed, "zip-exact"
                    else:
                        # measurement beats metadata (silently: quantified in
                        # verification.cd_mismatches, no run-level note)
                        uncomp = streamed
                        method = f"zip-exact-mismatch(cd={cd_total})"
                        logmsg(f"cd mismatch {name}: cd={cd_total} "
                               f"streamed={streamed}")
                except Exception as exc:  # noqa: BLE001 — keep the CD value
                    logmsg(f"zip stream fallback {name}: "
                           f"{type(exc).__name__}: {str(exc)[:120]}")
        elif kind == "gz":
            uncomp, method = gz_uncompressed(name, clen)
            if (budget is not None and gz_stream_candidate(method, clen)
                    and budget.reserve(clen)):
                try:
                    uncomp, method = (_stream_with_retry(gz_stream_exact,
                                                         name, clen, budget),
                                       "gz-exact")
                except TruncatedStream as exc:
                    # blob ends mid-member: the partial count IS the exact
                    # logical size of what exists (a garbage trailer would
                    # over- or undercount arbitrarily)
                    uncomp, method = exc.partial, "gz-truncated"
                    logmsg(f"truncated gz {name}: exact partial {exc.partial}")
                except Exception as exc:  # noqa: BLE001 — keep trailer value
                    logmsg(f"stream fallback {name}: {type(exc).__name__}: "
                           f"{str(exc)[:120]}")
        elif kind == "parquet":
            # footer metadata in BOTH modes — no stdlib codec for the data
            # pages, so deep verify has nothing better to stream
            uncomp, method = parquet_uncompressed(name, clen)
        elif kind in ("bz2", "xz"):
            # only reached in deep mode — shallow enqueues these directly
            # (content-length, no HTTP) without a pool submission
            method = f"{kind}-stored"
            decomp = (bz2.BZ2Decompressor if kind == "bz2"
                      else lzma.LZMADecompressor)
            if (DEEP_VERIFY and budget is not None and clen
                    and budget.reserve(clen)):
                try:
                    uncomp = _stream_with_retry(
                        lambda nm: _stream_exact_multi(nm, decomp),
                        name, clen, budget)
                    method = f"{kind}-exact"
                except TruncatedStream as exc:
                    uncomp, method = exc.partial, f"{kind}-truncated"
                    logmsg(f"truncated {kind} {name}: exact partial "
                           f"{exc.partial}")
                except Exception as exc:  # noqa: BLE001 — keep stored value
                    logmsg(f"stream fallback {name}: {type(exc).__name__}: "
                           f"{str(exc)[:120]}")
    except Exception as exc:  # noqa: BLE001
        err = type(exc).__name__
        method, uncomp = f"err:{err}", clen
        logmsg(f"ERROR {name}: {err}: {str(exc)[:160]}")
    return {"name": name, "clen": clen, "uncomp": uncomp, "method": method,
            "kind": kind, "etag": etag, "svc": svc, "cached": False, "err": err}


def coverage_class(kind, method, name):
    """Verification bucket for a row — computed from (kind, method, name)
    alone so cached replays classify identically to fresh measurements.
    measured  = the number is a byte count we (or the listing) observed:
                exact streams, truncated-stream partials, trivial blobs, and
                loose stored files (content-length IS their logical size).
    trusted   = the number comes from metadata we did not verify: zip CDs,
                gz trailers, bz2/xz stored placeholders, err:* floors.
    unmeasurable = compressed formats with no stdlib codec (.7z/.rar/.zst) —
                counted at stored size, surfaced per-format."""
    if method.startswith("err:"):
        return "trusted"
    if kind == "stored":
        return "unmeasurable" if unmeasurable_ext(name) else "measured"
    if kind == "zip":
        if (method in ("zip-exact", "zip-tiny", "zip:0entries")
                or method.startswith("zip-exact-mismatch")):
            return "measured"
        return "trusted"
    if kind == "gz":
        return ("measured" if method in ("gz-exact", "gz-truncated", "gz-tiny")
                else "trusted")
    if kind == "parquet":
        # footer totals are metadata we cannot stream-verify (no snappy
        # codec in the stdlib) — permanently trusted, even in deep mode
        return "measured" if method == "parquet-tiny" else "trusted"
    # bz2 / xz
    return ("measured" if method.endswith(("-exact", "-truncated"))
            else "trusted")


def _method_streamed(method):
    """Rows whose value came from a full streaming pass this run — the
    egress ledger (verification.stream_compressed_bytes) counts their clen
    when not served from cache."""
    return (method in ("gz-exact", "gz-truncated", "bz2-exact",
                       "bz2-truncated", "xz-exact", "xz-truncated",
                       "zip-exact")
            or method.startswith(("zip-exact-mismatch", "zip-partial(")))


class Aggregator:
    """Single-threaded consumer of sized-blob rows: writes the TSV and owns
    every aggregate, so nothing here needs a lock."""

    def __init__(self, tsv_path, matcher, fingerprint):
        self.per = defaultdict(lambda: [0, 0, 0])   # top prefix
        self.l2 = defaultdict(lambda: [0, 0, 0])    # second level
        self.detected = {}
        self.err_types = defaultdict(int)
        self.methods = defaultdict(int)
        self.index_rows = []
        self.n = self.zero = self.errors = 0
        self.cache_hits = self.cache_misses = 0
        self.gz_streamed = self.gz_streamed_bytes = 0
        self.gz_uncertain = self.gz_uncertain_bytes = 0
        self.ver = {"measured": [0, 0], "trusted": [0, 0],
                    "unmeasurable": [0, 0]}         # [blobs, unc_bytes]
        self.unmeasurable_fmt = defaultdict(lambda: [0, 0])  # ext → [blobs, bytes]
        self.cd_mismatches = 0
        self.stream_comp_bytes = 0
        self.matcher = matcher
        self.tsv = open(tsv_path, "w", buffering=1)
        self.tsv.write(f"#matcher\t{fingerprint}\n")

    def _svc(self, svc):
        return self.detected.setdefault(svc, {
            "bytes": 0, "blob_count": 0, "entry_count": 0,
            "path_bytes": 0, "zip_entry_bytes": 0, "sources": {}})

    def add(self, r):
        name, clen, uncomp = r["name"], r["clen"], r["uncomp"]
        det_json = (json.dumps(r["svc"], separators=(",", ":"))
                    if r["svc"] else "")
        ratio = (uncomp / clen) if clen else 0.0
        self.tsv.write(f"{name}\t{clen}\t{uncomp}\t{ratio:.3f}"
                       f"\t{r['method']}\t{r['etag']}\t{det_json}\n")
        top = name.split("/", 1)[0] if "/" in name else "(root)"
        for acc in (self.per[top], self.l2[l2_key(name)]):
            acc[0] += 1
            acc[1] += clen
            acc[2] += uncomp
        self.methods[r["kind"]] += 1
        self.n += 1
        if clen == 0:
            self.zero += 1
        if r["err"]:
            self.errors += 1
            self.err_types[r["err"]] += 1
        if r["cached"]:
            self.cache_hits += 1
        elif r["kind"] in CACHEABLE_KINDS and not r.get("no_http"):
            # shallow bz2/xz placeholder rows did no fetch: neither hit nor
            # miss, so "mass misses on a warm run = re-upload" stays honest
            self.cache_misses += 1
        cls = coverage_class(r["kind"], r["method"], name)
        v = self.ver[cls]
        v[0] += 1
        v[1] += uncomp
        if cls == "unmeasurable":
            fmt = self.unmeasurable_fmt[unmeasurable_ext(name) or "?"]
            fmt[0] += 1
            fmt[1] += clen
        if r["method"].startswith("zip-exact-mismatch"):
            self.cd_mismatches += 1
        if not r["cached"] and _method_streamed(r["method"]):
            self.stream_comp_bytes += clen
        if r["kind"] == "gz":
            if r["method"] == "gz-exact" and not r["cached"]:
                self.gz_streamed += 1
                self.gz_streamed_bytes += clen
            if gz_uncertain_row(r["kind"], r["method"], clen):
                self.gz_uncertain += 1
                self.gz_uncertain_bytes += clen
        if r["kind"] in CACHEABLE_KINDS and not r["err"]:
            self.index_rows.append(
                (name, r["etag"], clen, uncomp, r["method"], det_json))
        # detection — path layer attributes the whole blob to its (single,
        # deepest-wins) match; zip-entry layer attributes per-entry bytes,
        # skipping the blob's own path service (those bytes are already in).
        path_hit = match_path(name, self.matcher)
        if path_hit:
            d = self._svc(path_hit)
            d["bytes"] += uncomp
            d["path_bytes"] += uncomp
            d["blob_count"] += 1
            d["sources"][top] = d["sources"].get(top, 0) + uncomp
        for svc, (b, cnt) in (r["svc"] or {}).items():
            if svc == path_hit:
                continue
            d = self._svc(svc)
            d["bytes"] += b
            d["zip_entry_bytes"] += b
            d["entry_count"] += cnt
            d["sources"][top] = d["sources"].get(top, 0) + b
        if clen >= 1_000_000_000:
            logmsg(f"  big: {name} comp={clen/1e9:.2f}GB "
                   f"unc={uncomp/1e9:.2f}GB ({r['method']})")
        if self.n % 5000 == 0:
            logmsg(f"progress {self.n} blobs, errors={self.errors}, "
                   f"cache_hits={self.cache_hits}")

    def close(self):
        self.tsv.close()


def enumerate_and_size(cache, matcher):
    """Prefix-parallel listing + pooled zip/gz reads + cache short-circuit.
    A listing failure is fatal (raised after draining workers) — the partial
    TSV stays behind as next launch's seed. Per-blob failures are just rows.
    A consumer (aggregation) failure is ALSO fatal — e.g. a poisoned cached
    det_json of the wrong shape — so main() never writes .done over a
    truncated summary; the consumer keeps draining to the sentinel first so
    producers (workers/listers) never block on a full queue."""
    fp = matcher_fingerprint(matcher)
    agg = Aggregator(OUT, matcher, fp)
    results = queue.Queue(maxsize=10000)          # backpressure bound
    inflight = threading.BoundedSemaphore(SIZER_WORKERS * 4)
    stop = threading.Event()
    budget = StreamBudget(GZ_STREAM_BUDGET)
    size_pool = ThreadPoolExecutor(max_workers=SIZER_WORKERS)
    consumer_exc = []

    def consume():
        while True:
            r = results.get()
            if r is None:
                return
            try:
                agg.add(r)
            except Exception as exc:  # noqa: BLE001 — never wedge producers
                consumer_exc.append(exc)
                stop.set()  # doomed run — stop paying for more listing/sizing
                logmsg(f"ERROR consumer: {type(exc).__name__}: "
                       f"{str(exc)[:160]}")

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()

    def handle_blob(name, clen, etag):
        kind = blob_kind(name)
        if kind == "stored":
            results.put({"name": name, "clen": clen, "uncomp": clen,
                         "method": "stored", "kind": kind, "etag": etag,
                         "svc": {}, "cached": False, "err": None})
            return
        hit = cache_lookup(cache, name, etag, clen)
        if hit and cached_row_stale(kind, hit[3], clen, hit[2]):
            hit = None  # one-time re-measure under the current trigger/mode
        if hit:
            _etag, _clen, uncomp, method, det = hit
            results.put({"name": name, "clen": clen, "uncomp": uncomp,
                         "method": method, "kind": kind, "etag": etag,
                         "svc": json.loads(det) if det else {},
                         "cached": True, "err": None})
            return
        if kind in ("bz2", "xz") and not DEEP_VERIFY:
            # shallow: no cheap trailer exists — content-length placeholder,
            # zero HTTP, no pool. Cached (so deep-exact rows replay later and
            # these replay tomorrow) but flagged no_http for the miss counter.
            results.put({"name": name, "clen": clen, "uncomp": clen,
                         "method": f"{kind}-stored", "kind": kind,
                         "etag": etag, "svc": {}, "cached": False,
                         "err": None, "no_http": True})
            return
        inflight.acquire()
        fut = size_pool.submit(size_blob, name, clen, etag, kind, matcher, budget)

        def done(f):
            inflight.release()
            results.put(f.result())

        fut.add_done_callback(done)

    def list_prefix(prefix):
        marker = ""
        while not stop.is_set():
            blobs, _, marker = parse_list_page(
                http_get(list_url(prefix=prefix, marker=marker)))
            for name, clen, etag in blobs:
                handle_blob(name, clen, etag)
            if not marker:
                return

    fatal = None
    try:
        prefixes, root_blobs = discover_prefixes()
        for name, clen, etag in root_blobs:
            handle_blob(name, clen, etag)
        if prefixes:
            logmsg(f"listing {len(prefixes)} top-level prefixes, "
                   f"{LIST_WORKERS} listers, {SIZER_WORKERS} sizers, "
                   f"cache={len(cache)} rows")
            with ThreadPoolExecutor(
                    max_workers=min(LIST_WORKERS, len(prefixes))) as listers:
                futs = [listers.submit(list_prefix, p) for p in prefixes]
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception as exc:  # noqa: BLE001
                        stop.set()
                        fatal = fatal or exc
    except Exception as exc:  # noqa: BLE001 — discover_prefixes failed
        stop.set()
        fatal = fatal or exc
    size_pool.shutdown(wait=True)
    results.put(None)
    consumer.join()
    agg.close()
    if fatal is None and consumer_exc:
        fatal = consumer_exc[0]
    if fatal is not None:
        raise fatal
    return agg


def write_summary(agg, dur_s):
    gb = lambda x: x / 1e9  # noqa: E731
    tc = sum(v[1] for v in agg.per.values())
    tu = sum(v[2] for v in agg.per.values())
    lines = [
        f"SA: {SA}",
        f"Container: {CONTAINER}",
        f"Blobs: {agg.n}",
        f"Compressed:   {tc} bytes ({gb(tc):.2f} GB)",
        f"Uncompressed: {tu} bytes ({gb(tu):.2f} GB)",
        (f"Ratio: {tu / tc:.3f}" if tc else "Ratio: n/a"),
        f"Errors: {agg.errors}",
        f"Cache: {agg.cache_hits} hits, {agg.cache_misses} misses",
        "",
        f"{'datasource':<22}{'files':>10}{'compressed_GB':>16}"
        f"{'uncompressed_GB':>18}{'ratio':>9}",
    ]
    if agg.gz_streamed or agg.gz_uncertain:
        lines.insert(lines.index(""),
                     f"gz: {agg.gz_streamed} streamed exact "
                     f"({agg.gz_streamed_bytes/1e9:.2f} GB), "
                     f"{agg.gz_uncertain} uncertain "
                     f"({agg.gz_uncertain_bytes/1e9:.2f} GB compressed)")
    if DEEP_VERIFY:
        m_blobs, m_bytes = agg.ver["measured"]
        t_blobs, t_bytes = agg.ver["trusted"]
        u_blobs, u_bytes = agg.ver["unmeasurable"]
        pct = (m_bytes / tu * 100) if tu else 100.0
        lines.insert(lines.index(""),
                     f"verification: {pct:.1f}% of bytes measured exact "
                     f"({m_blobs} blobs); trusted {t_blobs} blobs "
                     f"({t_bytes/1e9:.2f} GB); unmeasurable {u_blobs} blobs "
                     f"({u_bytes/1e9:.2f} GB); cd mismatches "
                     f"{agg.cd_mismatches}; streamed "
                     f"{agg.stream_comp_bytes/1e9:.2f} GB compressed")
    for k in sorted(agg.per, key=lambda k: -agg.per[k][2]):
        f_, c_, u_ = agg.per[k]
        r_ = (u_ / c_) if c_ else 0.0
        lines.append(f"{k:<22}{f_:>10}{gb(c_):>16.2f}{gb(u_):>18.2f}{r_:>9.3f}")
    if agg.detected:
        lines.append("")
        lines.append("detected services (path + zip-entry layers):")
        for k, d in sorted(agg.detected.items(), key=lambda kv: -kv[1]["bytes"]):
            lines.append(f"  {k:<20}{gb(d['bytes']):>10.2f} GB "
                         f"(path {gb(d['path_bytes']):.2f} / "
                         f"zip-entries {gb(d['zip_entry_bytes']):.2f})")
    with open(SUMMARY, "w") as f:
        f.write("\n".join(lines) + "\n")
    machine = {
        "sa": SA, "container": CONTAINER, "blobs": agg.n, "comp": tc, "unc": tu,
        "zero": agg.zero, "errors": agg.errors,
        "err_types": dict(agg.err_types), "methods": dict(agg.methods),
        "dur_s": round(dur_s),
        "src": {k: v for k, v in sorted(agg.per.items(),
                                        key=lambda kv: -kv[1][2])},
        "cache": {"hits": agg.cache_hits, "misses": agg.cache_misses},
        "gz": {"streamed": agg.gz_streamed,
               "streamed_bytes": agg.gz_streamed_bytes,
               "uncertain": agg.gz_uncertain,
               "uncertain_bytes": agg.gz_uncertain_bytes},
        "detected_services": {k: v for k, v in sorted(
            agg.detected.items(), key=lambda kv: -kv[1]["bytes"])},
        "sources_l2": rollup_l2(agg.l2),
    }
    if DEEP_VERIFY:
        machine["verification"] = {
            "deep": True,
            "measured_blobs": agg.ver["measured"][0],
            "measured_bytes": agg.ver["measured"][1],
            "trusted_blobs": agg.ver["trusted"][0],
            "trusted_bytes": agg.ver["trusted"][1],
            "unmeasurable_blobs": agg.ver["unmeasurable"][0],
            "unmeasurable_bytes": agg.ver["unmeasurable"][1],
            "unmeasurable_by_format": {k: list(v) for k, v in
                                       sorted(agg.unmeasurable_fmt.items())},
            "cd_mismatches": agg.cd_mismatches,
            "stream_compressed_bytes": agg.stream_comp_bytes,
        }
    with open(SUMMARY_JSON, "w") as f:
        json.dump(machine, f, separators=(",", ":"))
        f.write("\n")


def main():
    _init_from_env()
    open(LOG, "w").close()
    t0 = time.time()
    matcher = build_matcher(EXPECTED_SERVICES)
    fp = matcher_fingerprint(matcher)
    cache = load_cache(CACHE_FILE, fp)
    cache.update(load_seed_tsv(SEED_TSV, fp))
    logmsg(f"start SA={SA} CONTAINER={CONTAINER} cache={len(cache)} rows "
           f"workers={SIZER_WORKERS} matcher_fp={fp} (stdlib+SAS)")
    agg = enumerate_and_size(cache, matcher)
    write_summary(agg, time.time() - t0)
    write_index(INDEX, agg.index_rows, fp)
    logmsg(f"size done blobs={agg.n} errors={agg.errors} "
           f"cache hits={agg.cache_hits} misses={agg.cache_misses}")
    open(DONE, "w").close()
    print(open(SUMMARY).read())


if __name__ == "__main__":
    main()
