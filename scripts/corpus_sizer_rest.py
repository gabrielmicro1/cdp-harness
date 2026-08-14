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

Sizing (no bulk download — reads only indexes/trailers):
  .zip            -> End-of-Central-Directory + Central Directory via Range GETs
                     (ZIP64-aware); sum entry uncompressed sizes. NON-recursive.
  .gz/.tgz/.tar.gz-> 4-byte ISIZE trailer, floored at compressed size (exact
                     <4 GiB, sane lower bound above). Never streams — so it
                     stays fast on multi-GB backup tarballs.
  everything else -> uncompressed = content-length (accurate for loose files).
Read-only; writes nothing back to the blob.
"""
import gzip
import hashlib
import json
import os
import queue
import re
import struct
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── config (import-safe: real values come from _init_from_env in main) ───────
SA = CONTAINER = SAS = TAG = ""
MAX_ZIP_ENTRIES = 5_000_000
SIZER_WORKERS = 16      # zip/gz range-read worker threads
LIST_WORKERS = 8        # concurrent top-level-prefix listers
CACHE_FILE = SEED_TSV = ""
EXPECTED_SERVICES: tuple = ()
BASE = LOG = OUT = SUMMARY = SUMMARY_JSON = DONE = INDEX = ""


def _init_from_env():
    global SA, CONTAINER, SAS, TAG, MAX_ZIP_ENTRIES, SIZER_WORKERS, LIST_WORKERS
    global CACHE_FILE, SEED_TSV, EXPECTED_SERVICES
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
    return "stored"


# ── per-blob cache (blob-index) ──────────────────────────────────────────────
# Rows exist only for zip/gz blobs (stored blobs cost nothing to size — the
# listing already carries their size). Error rows are never cached, so
# transient failures retry next run. A cache can only cost time, never
# correctness: any doubt → miss.
CACHEABLE_KINDS = ("zip", "gz")


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
EOCD64_STRUCT = "<IQHHIIQQQQ"
CD_SIG = 0x02014b50


def _parse_cd(cd, total_entries, matcher=None):
    """Walk the central directory: sum uncompressed sizes and, when a matcher
    is given, attribute matched entry paths to services (exact per-entry
    usize — zero extra requests). Returns (total_uncomp, entries_seen, svc)
    with svc = {service: [bytes, entry_count]}."""
    total_uncomp = 0
    n = 0
    p = 0
    svc = {}
    cap = min(total_entries, MAX_ZIP_ENTRIES)
    sig = struct.pack("<I", CD_SIG)
    while p + 46 <= len(cd) and n < cap:
        if cd[p:p + 4] != sig:
            break
        (_s, _vm, _vn, _fl, _meth, _mt, _md, _crc, csize, usize,
         name_len, extra_len, cmt_len, _dn, _ia, _ea, _lho) = struct.unpack(
            "<IHHHHHHIIIHHHHHII", cd[p:p + 46])
        name_end = p + 46 + name_len
        extra_end = name_end + extra_len
        if usize == 0xFFFFFFFF:  # ZIP64 — real usize is in the extra field
            ex = cd[name_end:extra_end]
            xp = 0
            while xp + 4 <= len(ex):
                xid, xlen = struct.unpack("<HH", ex[xp:xp + 4])
                if xid == 0x0001:
                    zdata = ex[xp + 4:xp + 4 + xlen]
                    usize = struct.unpack("<Q", zdata[0:8])[0]
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
        total_uncomp += usize
        n += 1
        p = extra_end + cmt_len
    return total_uncomp, n, svc


def zip_uncompressed(name, clen, matcher=None):
    """Return (uncompressed_bytes, note, svc). Range-reads only EOCD + CD."""
    tail_size = min(clen, 65557)
    tail = fetch_range(name, clen - tail_size, clen - 1)
    idx = tail.rfind(struct.pack("<I", EOCD_SIG))
    if idx < 0:
        return clen, "zip-no-eocd", {}
    (_sig, _disk, _cds, _etd, total_entries, cd_size, cd_off, _cl) = struct.unpack(
        EOCD_STRUCT, tail[idx:idx + EOCD_SIZE])
    if total_entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        loc_start = idx - LOC64_SIZE
        if loc_start < 0:
            return clen, "zip-loc64-split", {}
        (lsig, _ld, eocd64_off, _nd) = struct.unpack(LOC64_STRUCT, tail[loc_start:idx])
        if lsig != LOC64_SIG:
            return clen, "zip-loc64-bad", {}
        e64 = fetch_range(name, eocd64_off, eocd64_off + 55)
        (_s64, _sz64, _v, _vn, _d64, _cds64, _etd64, total_entries,
         cd_size, cd_off) = struct.unpack(EOCD64_STRUCT, e64[:56])
    cd = fetch_range(name, cd_off, cd_off + cd_size - 1)
    total_uncomp, n, svc = _parse_cd(cd, total_entries, matcher)
    note = f"zip:{n}entries" if n == total_entries else f"zip:partial{n}/{total_entries}"
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


def size_blob(name, clen, etag, kind, matcher):
    """Worker-pool job. Never raises — failures become err:* rows (counted,
    floored to stored size, never cached)."""
    uncomp, method, svc, err = clen, "stored", {}, None
    try:
        if kind == "zip":
            uncomp, method, svc = zip_uncompressed(name, clen, matcher)
        elif kind == "gz":
            uncomp, method = gz_uncompressed(name, clen)
    except Exception as exc:  # noqa: BLE001
        err = type(exc).__name__
        method, uncomp = f"err:{err}", clen
        logmsg(f"ERROR {name}: {err}: {str(exc)[:160]}")
    return {"name": name, "clen": clen, "uncomp": uncomp, "method": method,
            "kind": kind, "etag": etag, "svc": svc, "cached": False, "err": err}


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
        elif r["kind"] in CACHEABLE_KINDS:
            self.cache_misses += 1
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
        if hit:
            _etag, _clen, uncomp, method, det = hit
            results.put({"name": name, "clen": clen, "uncomp": uncomp,
                         "method": method, "kind": kind, "etag": etag,
                         "svc": json.loads(det) if det else {},
                         "cached": True, "err": None})
            return
        inflight.acquire()
        fut = size_pool.submit(size_blob, name, clen, etag, kind, matcher)

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
        "detected_services": {k: v for k, v in sorted(
            agg.detected.items(), key=lambda kv: -kv[1]["bytes"])},
        "sources_l2": rollup_l2(agg.l2),
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
