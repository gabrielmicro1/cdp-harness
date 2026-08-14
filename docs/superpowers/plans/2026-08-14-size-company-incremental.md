# size-company Incremental Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make repeat sizing runs incremental via a persistent ETag-validated per-blob cache, parallelize the sizer's HTTP work, and detect declared services embedded inside other sources' blobs (paths + zip central-directory entry names).

**Architecture:** The sizer (`corpus_sizer_rest.py`, single stdlib-only file) gains a cache seeded from `companies/<slug>/blob-index.tsv.gz`, prefix-parallel listing, a worker pool for zip/gz range-reads, and a detection layer. `phases.py` plumbs cache/seed/expected-services env vars at launch and moves the fresh index into the company dir at harvest (instead of deleting per-blob detail). `reconcile.py` turns detection data into a `found-embedded` flag and notes. Spec: `docs/superpowers/specs/2026-08-14-size-company-incremental-design.md`.

**Tech Stack:** Python 3 stdlib only (no pip installs, no venv). Tests are the existing monolithic `python3 tests/test_harness.py` (its own `check()` convention — NOT pytest).

## Global Constraints

- All scripts stdlib-only python3; `corpus_sizer_rest.py` must stay a single self-contained file (it is launched by absolute path with `cwd=REPO_ROOT` and must never import sibling modules).
- Read-only against client storage: SAS stays `rl`, 1-day expiry. The cache changes nothing about Azure access.
- Sizes stored as raw bytes (ints); presentation is decimal GB/TB (÷10⁹) — untouched by this work.
- `sizing-runs/` files are append-only, never overwritten. New run-JSON fields are ADDITIVE; consumers must treat their absence in old run files as unknown, not zero (democo fixture stays old-shape on purpose).
- `--dry-run` paths must not touch the filesystem or Azure (launch's dry-run early-returns BEFORE any file operations — preserve that).
- Every company-reading script takes `--root` (tests run against a temp copy of `tests/fixtures/companies/`).
- Nothing in the harness runs git at runtime. Test command is always `python3 tests/test_harness.py` from the repo root.
- Existing behavior parity: `sources` (top-level prefix split), `methods`, `errors`, `zero_byte_blobs`, TSV columns 1–5 (`name, clen, uncomp, ratio, method`) all keep their current meaning; new TSV columns go at the END.
- Commit after each task with the exact `git add` paths listed (never `git add -A` — the working tree has unrelated modified files).

## File Structure

- `scripts/corpus_sizer_rest.py` — all sizer changes (import-safety, listing/etag, cache, detection, concurrency, summary/index outputs). Stays one file by design.
- `scripts/phases.py` — launch env plumbing, harvest index move, `summary_to_run` / `write_copied_forward_run` new fields.
- `scripts/fleet_size.py`, `scripts/size_company.py` — `--no-cache` flag only.
- `scripts/reconcile.py` — `found-embedded` flag, `detection_notes()`.
- `scripts/gen_report.py` — one new badge mapping.
- `tests/test_harness.py` — new sections; existing checks must keep passing unmodified.
- `CLAUDE.md`, `.claude/skills/size-company/SKILL.md`, `.claude/skills/size-company/references/sizing-lore.md` — docs.

---

### Task 1: Import-safe sizer config + ETag-aware listing parser

**Files:**
- Modify: `scripts/corpus_sizer_rest.py:43-51` (module-level env reads)
- Test: `tests/test_harness.py` (new section)

**Interfaces:**
- Produces: `_init_from_env()` (reads env into module globals; `main()` calls it; importing the module requires NO env vars), `parse_list_page(xml_bytes) -> (blobs, prefixes, next_marker)` where `blobs` is `list[(name: str, content_length: int, etag: str)]` and `prefixes` is `list[str]`, `blob_kind(name) -> "zip" | "gz" | "stored"`. New module globals (set by `_init_from_env`): `SIZER_WORKERS` (env `SIZER_WORKERS`, default 16), `LIST_WORKERS` (env `LIST_WORKERS`, default 8), `CACHE_FILE` (env `CACHE_FILE`, default ""), `SEED_TSV` (env `SEED_TSV`, default ""), `EXPECTED_SERVICES` (env `EXPECTED_SERVICES`, comma-separated, default `()`), `INDEX` (path `f"{BASE}.index.tsv.gz"`).
- Consumed by: every later task.

- [ ] **Step 1: Write the failing tests**

In `tests/test_harness.py`, insert a new section after the `— skip-check decision —` block (before `— local sizing end-to-end —`):

```python
    print("\n— sizer: import-safety + listing parse —")
    # Import must not require env vars (module was previously KeyError-on-import).
    import corpus_sizer_rest as sizer  # noqa: E402
    page = (b"<EnumerationResults><Blobs>"
            b"<Blob><Name>gdrive/a.zip</Name><Properties>"
            b"<Content-Length>123</Content-Length><Etag>0xAB</Etag>"
            b"</Properties></Blob>"
            b"<BlobPrefix><Name>gdrive/</Name></BlobPrefix>"
            b"</Blobs><NextMarker>tok</NextMarker></EnumerationResults>")
    blobs, prefixes, marker = sizer.parse_list_page(page)
    check("parse_list_page blobs incl etag",
          blobs == [("gdrive/a.zip", 123, "0xAB")], str(blobs))
    check("parse_list_page prefixes", prefixes == ["gdrive/"], str(prefixes))
    check("parse_list_page marker", marker == "tok")
    check("blob_kind", sizer.blob_kind("A.ZIP") == "zip"
          and sizer.blob_kind("x.tar.gz") == "gz"
          and sizer.blob_kind("x.tgz") == "gz"
          and sizer.blob_kind("x.bin") == "stored")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: crash at `import corpus_sizer_rest` with `KeyError: 'SA'` (module reads env at import today).

- [ ] **Step 3: Implement**

In `scripts/corpus_sizer_rest.py`, replace lines 43–51 (the module-level env reads) with import-safe defaults plus `_init_from_env()`:

```python
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
```

Add `_init_from_env()` as the FIRST line of `main()`. Add the two helpers (near `blob_base`):

```python
def blob_kind(name):
    lname = name.lower()
    if lname.endswith(".zip"):
        return "zip"
    if lname.endswith((".tar.gz", ".tgz", ".gz")):
        return "gz"
    return "stored"


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
```

Also update the docstring's Env list to mention the new variables (`SIZER_WORKERS`, `LIST_WORKERS`, `CACHE_FILE`, `SEED_TSV`, `EXPECTED_SERVICES`) and the new output `<TAG>.index.tsv.gz`. Do NOT rewire `enumerate_and_size` yet (Task 5) — it keeps working because module main() initializes globals before it runs; the old listing loop is untouched for now.

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py`
Expected: all checks pass, including all pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add scripts/corpus_sizer_rest.py tests/test_harness.py
git commit -m "sizer: import-safe env config, ETag-aware listing parser, blob_kind"
```

---

### Task 2: Cache load / lookup / seed / index-write functions

**Files:**
- Modify: `scripts/corpus_sizer_rest.py` (new functions; add `import gzip`)
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: `blob_kind` (Task 1).
- Produces: `CACHEABLE_KINDS = ("zip", "gz")`; `load_cache(path) -> dict[str, tuple]` mapping `name -> (etag, clen, uncomp, method, det_json)`; `load_seed_tsv(path) -> same shape`; `cache_lookup(cache, name, etag, clen) -> tuple | None`; `write_index(path, rows)` where rows are `(name, etag, clen, uncomp, method, det_json)` tuples, gzip TSV, atomic via `.tmp` + `os.replace`.
- Cache row contract (all later tasks rely on it): index/seed rows exist ONLY for zip/gz blobs whose method is not `err:*`; `det_json` is the zip-entry service-hits JSON (`{"svc": [bytes, entries]}`) or empty string — path-layer detection is recomputed live each run (names are free), so it is never cached.

- [ ] **Step 1: Write the failing tests**

Append to the sizer section in `tests/test_harness.py`:

```python
    print("\n— sizer: cache roundtrip —")
    idx_path = str(tmp / "test-index.tsv.gz")
    rows = [("gdrive/a.zip", "0xAB", 123, 456, "zip:3entries",
             '{"hubspot":[100,2]}'),
            ("slack/b.gz", "0xCD", 10, 30, "gz-trailer", "")]
    sizer.write_index(idx_path, rows)
    cache = sizer.load_cache(idx_path)
    check("cache roundtrip", cache["gdrive/a.zip"] ==
          ("0xAB", 123, 456, "zip:3entries", '{"hubspot":[100,2]}')
          and cache["slack/b.gz"] == ("0xCD", 10, 30, "gz-trailer", ""),
          str(cache))
    check("cache hit needs etag+clen match",
          sizer.cache_lookup(cache, "gdrive/a.zip", "0xAB", 123) is not None
          and sizer.cache_lookup(cache, "gdrive/a.zip", "0xZZ", 123) is None
          and sizer.cache_lookup(cache, "gdrive/a.zip", "0xAB", 999) is None
          and sizer.cache_lookup(cache, "gdrive/a.zip", "", 123) is None
          and sizer.cache_lookup(cache, "nope", "0xAB", 123) is None)
    check("missing/corrupt cache → empty (fail-safe)",
          sizer.load_cache("") == {} and
          sizer.load_cache(str(tmp / "no-such-file.tsv.gz")) == {})
    seed_path = tmp / "test-seed.tsv"
    seed_path.write_text(
        "gdrive/a.zip\t123\t456\t3.707\tzip:3entries\t0xAB\t\n"      # good
        "old/no-etag.zip\t5\t5\t1.0\tzip:1entries\n"                  # old format
        "bad/err.zip\t9\t9\t1.0\terr:BadZipFile\t0xEE\t\n"            # error row
        "plain/file.txt\t7\t7\t1.0\tstored\t0xFF\t\n")                # not cacheable
    seed = sizer.load_seed_tsv(str(seed_path))
    check("seed: keeps good zip row only", list(seed) == ["gdrive/a.zip"]
          and seed["gdrive/a.zip"] == ("0xAB", 123, 456, "zip:3entries", ""),
          str(seed))
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — `AttributeError: module 'corpus_sizer_rest' has no attribute 'write_index'`.

- [ ] **Step 3: Implement**

Add `import gzip` to the imports of `corpus_sizer_rest.py`, then below `blob_kind`:

```python
# ── per-blob cache (blob-index) ──────────────────────────────────────────────
# Rows exist only for zip/gz blobs (stored blobs cost nothing to size — the
# listing already carries their size). Error rows are never cached, so
# transient failures retry next run. A cache can only cost time, never
# correctness: any doubt → miss.
CACHEABLE_KINDS = ("zip", "gz")


def load_cache(path):
    """blob-index.tsv.gz → {name: (etag, clen, uncomp, method, det_json)}."""
    out = {}
    if not path:
        return out
    try:
        with gzip.open(path, "rt", newline="") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 6:
                    continue
                name, etag, clen, uncomp, method, det = parts[:6]
                if method.startswith("err:") or not etag:
                    continue
                out[name] = (etag, int(clen), int(uncomp), method, det)
    except Exception:  # noqa: BLE001 — corrupt cache = no cache
        return {}
    return out


def load_seed_tsv(path):
    """A crashed run's partial sizes.tsv as a second seed. TSV columns:
    name, clen, uncomp, ratio, method, etag, det_json (rows from older TSVs
    without the etag column are skipped)."""
    out = {}
    if not path:
        return out
    try:
        with open(path, newline="") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 7:
                    continue
                name, clen, uncomp, _ratio, method, etag, det = parts[:7]
                if (method.startswith("err:") or not etag
                        or blob_kind(name) not in CACHEABLE_KINDS):
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


def write_index(path, rows):
    tmp_path = str(path) + ".tmp"
    with gzip.open(tmp_path, "wt", newline="") as f:
        for r in rows:
            f.write("\t".join(map(str, r)) + "\n")
    os.replace(tmp_path, path)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py` — all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/corpus_sizer_rest.py tests/test_harness.py
git commit -m "sizer: blob-index cache load/lookup/seed/write"
```

---

### Task 3: Service catalog, path matcher, second-level rollup

**Files:**
- Modify: `scripts/corpus_sizer_rest.py` (add `import re`)
- Test: `tests/test_harness.py`

**Interfaces:**
- Produces: `SERVICE_CATALOG: dict[str, tuple[str, ...]]` (canonical → normalized aliases), `norm_seg(s) -> str` (casefold, alnum-only — same convention as `reconcile.norm`, duplicated because the sizer must stay standalone), `build_matcher(declared=()) -> dict[str, str]` (normalized alias → canonical; declared manifest names are added mapping to the manifest's own spelling and WIN over catalog aliases), `match_segment(seg, matcher) -> str | None`, `match_path(path, matcher, max_depth=3) -> str | None` (deepest-wins), `l2_key(name) -> str`, `rollup_l2(l2, cap=40) -> dict`.
- Matching rules (later tasks and docs depend on these exactly):
  - A segment matches if its normalized form is in the matcher, OR any `[a-z0-9]+` token of its lowercased form is in the matcher (so `slack-export.zip` matches `slack`).
  - `match_path` considers the first `max_depth` path segments plus the final segment (filename) when the path is deeper; the DEEPEST matching candidate wins, so one path attributes to exactly one service — per-service byte attribution never double-counts.
  - `l2_key`: `"top/second"` when the name has ≥3 segments, `"top/(files)"` for 2 segments, `"(root)"` for names without `/`.

- [ ] **Step 1: Write the failing tests**

Append to the sizer section:

```python
    print("\n— sizer: service detection matching —")
    m = sizer.build_matcher(("HubSpot", "My CRM"))
    check("catalog alias matches", sizer.match_segment("Slack", m) == "slack")
    check("token match in filename",
          sizer.match_segment("slack-export-2026.zip", m) == "slack")
    check("declared name wins with manifest spelling",
          sizer.match_segment("hubspot", m) == "HubSpot"
          and sizer.match_segment("my_crm", m) == "My CRM")
    check("no false positive", sizer.match_segment("miscellaneous", m) is None)
    check("deepest segment wins",
          sizer.match_path("gdrive/hubspot/x.csv", m) == "HubSpot")
    check("filename considered when deep",
          sizer.match_path("a/b/c/d/slack-log.txt", m) == "slack")
    check("depth cap: deep dir beyond 3 not matched",
          sizer.match_path("a/b/c/hubspot/x.csv", m) is None)
    check("l2_key shapes", sizer.l2_key("a/b/c.txt") == "a/b"
          and sizer.l2_key("a/c.txt") == "a/(files)"
          and sizer.l2_key("c.txt") == "(root)")
    big = {f"top/d{i}": [1, 10, 100 + i] for i in range(45)}
    rolled = sizer.rollup_l2(big, cap=40)
    check("rollup keeps 40 + (other)", len(rolled) == 41
          and "(other)" in rolled and rolled["(other)"][0] == 5
          and rolled["(other)"][2] == sum(100 + i for i in range(5)),
          str(rolled.get("(other)")))
```

Note on the depth-cap check: `a/b/c/hubspot/x.csv` has candidates `a, b, c` (first 3) + `x.csv` (filename) — `hubspot` at depth 4 is intentionally out of range.

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py` — FAIL with `AttributeError: ... 'build_matcher'`.

- [ ] **Step 3: Implement**

Add `import re` to imports. Add below the cache functions:

```python
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
    "linear": ("linear",),
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py` — all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/corpus_sizer_rest.py tests/test_harness.py
git commit -m "sizer: service catalog, deepest-wins path matcher, L2 rollup"
```

---

### Task 4: Zip central-directory entry names → embedded-service detection

**Files:**
- Modify: `scripts/corpus_sizer_rest.py:98-152` (`_parse_cd`, `zip_uncompressed`)
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: `match_path`, `build_matcher` (Task 3).
- Produces: `_parse_cd(cd, total_entries, matcher=None) -> (total_uncomp, n, svc)` where `svc` is `{canonical: [bytes, entry_count]}` (empty dict when matcher is None or nothing matched); `zip_uncompressed(name, clen, matcher=None) -> (uncomp, note, svc)` — note the arity change from 2 to 3. Entry names decode utf-8-first, cp437 fallback (zip spec default), `errors="replace"` so decoding can never fail a blob.
- Attribution: per matched entry, its exact CD `usize` — zero extra HTTP requests; this is CPU on bytes we already fetch.

- [ ] **Step 1: Write the failing tests**

Append to the sizer section (needs `import io, struct, zipfile` at the top of `test_harness.py`):

```python
    print("\n— sizer: zip entry detection —")

    def make_zip(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for ename, size in entries.items():
                z.writestr(ename, b"x" * size)
        return buf.getvalue()

    zb = make_zip({"hubspot/contacts.csv": 100, "misc/y.txt": 50})
    eidx = zb.rfind(struct.pack("<I", 0x06054b50))
    (_s, _d, _c1, _c2, n_ent, cd_size, cd_off, _cl) = struct.unpack(
        "<IHHHHIIH", zb[eidx:eidx + 22])
    tot, n, svc = sizer._parse_cd(zb[cd_off:cd_off + cd_size], n_ent,
                                  sizer.build_matcher())
    check("cd totals with names", tot == 150 and n == 2, f"{tot},{n}")
    check("cd svc attribution", svc == {"hubspot": [100, 1]}, str(svc))
    tot, n, svc = sizer._parse_cd(zb[cd_off:cd_off + cd_size], n_ent, None)
    check("cd no matcher → no svc", tot == 150 and svc == {})
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — `_parse_cd` returns 2 values, unpack of 3 raises `ValueError`.

- [ ] **Step 3: Implement**

Rework `_parse_cd` (keep the existing struct walk; add name decode + match):

```python
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
```

Update `zip_uncompressed` to accept and thread the matcher, returning 3 values (`return clen, "zip-no-eocd", {}` etc. on the early paths, and `return total_uncomp, note, svc` at the end):

```python
def zip_uncompressed(name, clen, matcher=None):
    """Return (uncompressed_bytes, note, svc). Range-reads only EOCD + CD."""
```

Fix the ONE existing call site in `enumerate_and_size` (still the old loop until Task 5):

```python
                    if lname.endswith(".zip"):
                        kind = "zip"
                        uncomp, method, _svc = zip_uncompressed(name, clen)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py` — all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/corpus_sizer_rest.py tests/test_harness.py
git commit -m "sizer: central-directory entry names feed embedded-service detection"
```

---

### Task 5: Concurrent pipeline + aggregator + new summary/index outputs

This is the core rework: replaces the serial `enumerate_and_size` loop with prefix-parallel listing, a worker pool for zip/gz reads, cache short-circuiting, and a single-threaded aggregator. Verified end-to-end offline against a fake in-memory container, twice (cold, then cache-warm).

**Files:**
- Modify: `scripts/corpus_sizer_rest.py` (`enumerate_and_size`, `write_summary`, `main`; add imports `queue`, `threading`, `from concurrent.futures import ThreadPoolExecutor, as_completed`)
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces:
  - `Aggregator` class: fields `per` (top-prefix → `[files, comp, unc]`), `l2`, `detected`, `methods`, `err_types`, `index_rows`, `n`, `zero`, `errors`, `cache_hits`, `cache_misses`; methods `add(row_dict)`, `close()`. Row dict keys: `name, clen, uncomp, method, kind, etag, svc, cached, err`.
  - `size_blob(name, clen, etag, kind, matcher) -> row dict` — never raises.
  - `list_url(prefix=None, delimiter=None, marker="") -> str`, `discover_prefixes() -> (prefixes, root_blobs)`.
  - `enumerate_and_size(cache, matcher) -> Aggregator` — raises on listing failure (fatal, per current contract) AFTER draining workers, leaving the partial TSV as next run's seed.
  - `write_summary(agg, dur_s)` — new signature (was 7 positional args). `summary.json` gains `"cache": {"hits": int, "misses": int}`, `"detected_services"`, `"sources_l2"`; all pre-existing keys (`sa, container, blobs, comp, unc, zero, errors, err_types, methods, dur_s, src`) unchanged.
  - `detected_services` value shape (consumed by phases/reconcile/report): `{service: {"bytes": B, "blob_count": n, "entry_count": m, "path_bytes": pb, "zip_entry_bytes": zb, "sources": {top_prefix: bytes}}}`, sorted by bytes desc. Attribution rules: path-layer attributes a blob's full `uncomp` to its (single, deepest-wins) path-matched service; zip-entry layer attributes per-entry usize, EXCEPT entries whose service equals the blob's own path match (already counted).
  - TSV row format (writer): `name, clen, uncomp, ratio(:.3f), method, etag, det_json`.
- Cache semantics: `misses` counts zip/gz blobs that needed a fetch; stored blobs count as neither.

- [ ] **Step 1: Write the failing tests**

Append to the sizer section (add `import gzip` and `import urllib.parse` to the imports at the top of `test_harness.py`; `io`, `struct`, `zipfile` were added in Task 4):

```python
    print("\n— sizer: offline end-to-end (fake container, cold then cached) —")
    container = {
        "gdrive/export.zip": make_zip({"docs/a.txt": 1000,
                                       "hubspot/contacts.csv": 5000}),
        "gdrive/plain.txt": b"y" * 200,
        "slack/logs/day1.gz": gzip.compress(b"z" * 3000),
        "rootfile.bin": b"r" * 50,
    }
    etags = {n: f"0x{i:02d}" for i, n in enumerate(sorted(container))}
    counters = {"range_gets": 0}

    def make_listing_xml(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        prefix = q.get("prefix", [""])[0]
        delim = q.get("delimiter", [""])[0]
        blobs, prefs, seen = [], [], set()
        for n in sorted(n for n in container if n.startswith(prefix)):
            rest = n[len(prefix):]
            if delim and delim in rest:
                p = prefix + rest.split(delim)[0] + delim
                if p not in seen:
                    seen.add(p)
                    prefs.append(p)
                continue
            blobs.append(n)
        parts = ["<EnumerationResults><Blobs>"]
        for n in blobs:
            parts.append(f"<Blob><Name>{n}</Name><Properties>"
                         f"<Content-Length>{len(container[n])}</Content-Length>"
                         f"<Etag>{etags[n]}</Etag></Properties></Blob>")
        for p in prefs:
            parts.append(f"<BlobPrefix><Name>{p}</Name></BlobPrefix>")
        parts.append("</Blobs><NextMarker/></EnumerationResults>")
        return "".join(parts).encode()

    def fake_http(url, extra_headers=None):
        if "comp=list" in url:
            return make_listing_xml(url)
        name = urllib.parse.unquote(
            url.split("/fake-raw/", 1)[1].split("?", 1)[0])
        data = container[name]
        rng = (extra_headers or {}).get("Range")
        if rng:
            counters["range_gets"] += 1
            a, b = rng[len("bytes="):].split("-")
            return data[int(a):int(b) + 1]
        return data

    swork = tmp / "sizer-work"
    swork.mkdir()
    sizer_env = {"SA": "fakesa", "CONTAINER": "fake-raw", "SAS": "sig=x",
                 "TAG": "fakeco-sizer", "OUT_DIR": str(swork),
                 "EXPECTED_SERVICES": "gdrive,hubspot,slack",
                 "SIZER_WORKERS": "4", "LIST_WORKERS": "2"}
    os.environ.update(sizer_env)
    os.environ.pop("CACHE_FILE", None)
    os.environ.pop("SEED_TSV", None)
    real_http = sizer.http_get
    try:
        sizer.http_get = fake_http
        sizer.main()
        s1 = json.loads((swork / "fakeco-sizer.summary.json").read_text())
        check("cold run totals", s1["blobs"] == 4
              and s1["unc"] == 6000 + 200 + 3000 + 50
              and s1["zero"] == 0 and s1["errors"] == 0,
              json.dumps(s1)[:300])
        check("cold run sources", set(s1["src"]) == {"gdrive", "slack", "(root)"})
        check("sources_l2 keys", set(s1["sources_l2"]) ==
              {"gdrive/(files)", "slack/logs", "(root)"}, str(s1["sources_l2"]))
        det = s1["detected_services"]
        check("hubspot detected inside zip",
              det["hubspot"]["bytes"] == 5000
              and det["hubspot"]["entry_count"] == 1
              and det["hubspot"]["zip_entry_bytes"] == 5000
              and det["hubspot"]["sources"] == {"gdrive": 5000}, str(det))
        check("gdrive path-detected (zip 6000 + plain 200)",
              det["gdrive"]["bytes"] == 6200
              and det["gdrive"]["path_bytes"] == 6200, str(det.get("gdrive")))
        check("slack path-detected", det["slack"]["bytes"] == 3000)
        check("cold cache stats", s1["cache"] == {"hits": 0, "misses": 2},
              str(s1["cache"]))
        check(".done and index written",
              (swork / "fakeco-sizer.done").exists()
              and (swork / "fakeco-sizer.index.tsv.gz").exists())
        cold_ranges = counters["range_gets"]
        check("cold run did range reads", cold_ranges > 0)

        # ── warm run: seed CACHE_FILE from the produced index ──
        cache_copy = tmp / "fakeco-index.tsv.gz"
        shutil.copy(swork / "fakeco-sizer.index.tsv.gz", cache_copy)
        for f in swork.glob("fakeco-sizer.*"):
            f.unlink()
        os.environ["CACHE_FILE"] = str(cache_copy)
        counters["range_gets"] = 0
        sizer.main()
        s2 = json.loads((swork / "fakeco-sizer.summary.json").read_text())
        check("warm run identical totals",
              s2["unc"] == s1["unc"] and s2["src"] == s1["src"])
        check("warm run all hits, zero range reads",
              s2["cache"] == {"hits": 2, "misses": 0}
              and counters["range_gets"] == 0,
              f'{s2["cache"]} ranges={counters["range_gets"]}')
        check("warm run keeps zip-entry detection (from cached det_json)",
              s2["detected_services"]["hubspot"]["bytes"] == 5000,
              str(s2["detected_services"].get("hubspot")))
        tsv = (swork / "fakeco-sizer.sizes.tsv").read_text().strip().split("\n")
        check("tsv has 4 rows x 7 cols", len(tsv) == 4
              and all(len(r.split("\t")) == 7 for r in tsv), tsv[0])
    finally:
        sizer.http_get = real_http
        for k in list(sizer_env) + ["CACHE_FILE", "SEED_TSV"]:
            os.environ.pop(k, None)
```

Expected-value notes for the executor: `gdrive/export.zip` uncompresses to 6000 (1000+5000); the gz ISIZE trailer gives 3000; `slack/logs/day1.gz` has 3 segments so its l2 key is `slack/logs`; `docs/a.txt` inside the zip matches nothing. `hubspot` entries inside the zip are attributed because the zip's own path match (`gdrive`) differs from `hubspot`.

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — old `enumerate_and_size()` takes no arguments / summary lacks `cache` key (first failing check depends on interpreter path, either is the correct failure).

- [ ] **Step 3: Implement**

Add imports to `corpus_sizer_rest.py`:

```python
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
```

Add a module-level log lock and make `logmsg` thread-safe:

```python
_LOG_LOCK = threading.Lock()


def logmsg(m):
    with _LOG_LOCK, open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [{TAG}] {m}\n")
```

Replace `enumerate_and_size` and `write_summary` entirely, and update `main`:

```python
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
    """Delimiter listing → (top-level prefixes, root-level blobs)."""
    prefixes, root_blobs, marker = [], [], ""
    while True:
        blobs, prefs, marker = parse_list_page(
            http_get(list_url(delimiter="/", marker=marker)))
        prefixes += prefs
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

    def __init__(self, tsv_path, matcher):
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
        for agg in (self.per[top], self.l2[l2_key(name)]):
            agg[0] += 1
            agg[1] += clen
            agg[2] += uncomp
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
    TSV stays behind as next launch's seed. Per-blob failures are just rows."""
    agg = Aggregator(OUT, matcher)
    results = queue.Queue(maxsize=10000)          # backpressure bound
    inflight = threading.BoundedSemaphore(SIZER_WORKERS * 4)
    stop = threading.Event()
    size_pool = ThreadPoolExecutor(max_workers=SIZER_WORKERS)

    def consume():
        while True:
            r = results.get()
            if r is None:
                return
            agg.add(r)

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
    cache = load_cache(CACHE_FILE)
    cache.update(load_seed_tsv(SEED_TSV))
    matcher = build_matcher(EXPECTED_SERVICES)
    logmsg(f"start SA={SA} CONTAINER={CONTAINER} cache={len(cache)} rows "
           f"workers={SIZER_WORKERS} (stdlib+SAS)")
    agg = enumerate_and_size(cache, matcher)
    write_summary(agg, time.time() - t0)
    write_index(INDEX, agg.index_rows)
    logmsg(f"size done blobs={agg.n} errors={agg.errors} "
           f"cache hits={agg.cache_hits} misses={agg.cache_misses}")
    open(DONE, "w").close()
    print(open(SUMMARY).read())
```

Delete the now-unused old bodies. Update the module docstring's "Writes in OUT_DIR" list (add `.index.tsv.gz`) and the TSV column description.

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py`
Expected: all pass, including both fake-container runs. Verify specifically: `warm run all hits, zero range reads` — that check IS the caching feature working.

- [ ] **Step 5: Commit**

```bash
git add scripts/corpus_sizer_rest.py tests/test_harness.py
git commit -m "sizer: prefix-parallel listing, pooled reads, cache short-circuit, detection outputs"
```

---

### Task 6: Harness integration — launch env, harvest index move, `--no-cache`

**Files:**
- Modify: `scripts/phases.py` (`launch`, `harvest_one`, `summary_to_run`, `write_copied_forward_run`)
- Modify: `scripts/fleet_size.py:183-196` (argparse), `scripts/fleet_size.py:90-101` (`cmd_launch_all` launch call)
- Modify: `scripts/size_company.py:30-38` (argparse)
- Test: `tests/test_harness.py` (extend the existing `— local sizing end-to-end —` fake-sizer section)

**Interfaces:**
- Consumes: sizer env contract from Tasks 1–5 (`CACHE_FILE`, `SEED_TSV`, `EXPECTED_SERVICES`), sizer outputs `<tag>.index.tsv.gz` and summary keys `cache`/`detected_services`/`sources_l2`.
- Produces: `phases.launch(root, slug, cfg, dry_run=False, use_cache=True)` (new kwarg); company blob index lives at `companies/<slug>/blob-index.tsv.gz`; run-JSON gains `"cache"` (dict or None), `"detected_services"` (dict, default `{}`), `"sources_l2"` (dict, default `{}`); copied-forward runs carry `detected_services`/`sources_l2` from the previous run and `"cache": None`; `--no-cache` CLI flag on both CLIs → `use_cache=False`.

- [ ] **Step 1: Write the failing tests**

In the existing `— local sizing end-to-end —` section, make these changes:

1. Extend the `summary` dict (line ~217) with new-style fields:

```python
    summary = {"sa": "stdemoco", "container": "democo-raw", "blobs": 5,
               "comp": 10, "unc": 20, "zero": 0, "errors": 1,
               "err_types": {"BadZipFile": 1}, "methods": {"zip": 5},
               "dur_s": 3, "src": {"a": [5, 10, 20]},
               "cache": {"hits": 3, "misses": 2},
               "detected_services": {"hubspot": {
                   "bytes": 7, "blob_count": 0, "entry_count": 1,
                   "path_bytes": 0, "zip_entry_bytes": 7,
                   "sources": {"a": 7}}},
               "sources_l2": {"a/(files)": [5, 10, 20]}}
```

2. After the existing `summary_to_run` check, add:

```python
    check("summary_to_run new fields", run["cache"] == {"hits": 3, "misses": 2}
          and run["detected_services"]["hubspot"]["bytes"] == 7
          and run["sources_l2"] == {"a/(files)": [5, 10, 20]})
    old_run = phases.summary_to_run("democo",
                                    {k: v for k, v in summary.items()
                                     if k not in ("cache", "detected_services",
                                                  "sources_l2")},
                                    {"metric": 1, "metric_at": "t"}, [])
    check("summary_to_run tolerates old summaries",
          old_run["cache"] is None and old_run["detected_services"] == {}
          and old_run["sources_l2"] == {})
```

3. Replace the `fake_sizer.write_text(...)` body so the fake sizer also dumps its env and writes an index file:

```python
    fake_sizer.write_text(
        "import json, os, time\n"
        "out, tag = os.environ['OUT_DIR'], os.environ['TAG']\n"
        "assert os.environ['AZURE_STORAGE_SAS'] == 'sig=fake'\n"
        "base = os.path.join(out, tag)\n"
        "open(base + '.log', 'w').write('start\\n')\n"
        "json.dump({k: os.environ.get(k) for k in\n"
        "           ('CACHE_FILE', 'SEED_TSV', 'EXPECTED_SERVICES')},\n"
        "          open(base + '.envdump.json', 'w'))\n"
        "time.sleep(1)\n"
        "import gzip\n"
        "gf = gzip.open(base + '.index.tsv.gz', 'wt')\n"
        "gf.write('a/x.zip\\t0xAA\\t10\\t20\\tzip:1entries\\t\\n')\n"
        "gf.close()\n"
        f"json.dump({json.dumps(summary)}, open(base + '.summary.json', 'w'))\n"
        "open(base + '.done', 'w').close()\n")
```

4. Before the `phases.launch(root, "democo", cfg)` call, pre-seed cache state:

```python
        wd = phases.work_dir(root)
        wd.mkdir(parents=True, exist_ok=True)
        import gzip as _gz
        prior_index = root / "democo" / "blob-index.tsv.gz"
        with _gz.open(prior_index, "wt") as f:
            f.write("old/x.zip\t0x01\t1\t2\tzip:1entries\t\n")
        (wd / "democo-sizer.sizes.tsv").write_text(
            "crash/y.zip\t3\t4\t1.3\tzip:1entries\t0x02\t\n")
```

5. The env dump must be read BEFORE `harvest_one` (cleanup deletes work files at harvest). Place this right after the `reaches Succeeded via .done` check:

```python
        env_dump = json.loads((wd / "democo-sizer.envdump.json").read_text())
        check("launch passed CACHE_FILE (prior index)",
              env_dump["CACHE_FILE"] == str(prior_index), str(env_dump))
        check("launch renamed stale tsv to seed and passed SEED_TSV",
              env_dump["SEED_TSV"] == str(wd / "democo-sizer.seed.tsv")
              and Path(env_dump["SEED_TSV"]).exists())
        check("launch passed EXPECTED_SERVICES from manifest",
              "hubspot" in (env_dump["EXPECTED_SERVICES"] or ""),
              str(env_dump))
```

(`Path` is already imported in test_harness.py.) Then after the existing harvest checks, add:

```python
        check("harvest run has detected_services",
              harvested["detected_services"]["hubspot"]["bytes"] == 7)
        check("harvest moved index into company dir",
              prior_index.exists() and _gz.open(prior_index, "rt").read()
              .startswith("a/x.zip"))
        check("cleanup removed seed + work files",
              not list(wd.glob("democo-sizer.*")))
        # --no-cache: fresh launch must not pass cache env
        st2 = phases.launch(root, "democo", cfg, use_cache=False)
        deadline = time.time() + 15
        res2 = phases.poll_one(root, "democo", st2)
        while res2["state"] not in phases.TERMINAL_STATES and time.time() < deadline:
            time.sleep(0.3)
            res2 = phases.poll_one(root, "democo", st2)
        env_dump2 = json.loads((wd / "democo-sizer.envdump.json").read_text())
        check("--no-cache: no CACHE_FILE/SEED_TSV",
              not env_dump2["CACHE_FILE"] and not env_dump2["SEED_TSV"],
              str(env_dump2))
        phases.harvest_one(root, "democo", cfg,
                           {**st2, "metric": 1, "metric_at": "t"})
```

6. In the `— copied-forward + status transitions —` section, the democo fixture has no `detected_services` (old-shape) — add one check after the existing `copied_from set` check:

```python
    check("copied-forward tolerates old runs (empty detection)",
          cf.get("detected_services", {}) == {} and cf.get("cache") is None)
```

And add a copied-forward carry test in the fake-sizer section, after the `--no-cache` harvest (the latest democo run now HAS detected_services):

```python
        cf2 = common.read_json(phases.write_copied_forward_run(
            root, "democo", 999, "t2"))
        check("copied-forward carries detected_services",
              cf2["detected_services"]["hubspot"]["bytes"] == 7
              and cf2["sources_l2"] == {"a/(files)": [5, 10, 20]}
              and cf2["cache"] is None)
```

7. In the `— fleet_size --dry-run —` section add one line after the existing checks:

```python
    proc = run_script("fleet_size.py", "launch-all", "--root", root,
                      "--dry-run", "--no-cache", "--slugs", "democo")
    check("--no-cache flag accepted", proc.returncode == 0)
```

Note: the fixture `tests/fixtures/companies/democo/expected-data-sizes.json` must declare a `hubspot` service for check 5's EXPECTED_SERVICES assertion — it already does (the reconcile section asserts a `declared-empty` flag on hubspot). Do not modify fixtures.

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — `summary_to_run new fields` (missing keys), then launch/env checks.

- [ ] **Step 3: Implement**

`scripts/phases.py`:

1. `summary_to_run` — after the `"errors"` entry add:

```python
        "cache": summary.get("cache"),
        "detected_services": summary.get("detected_services", {}),
        "sources_l2": summary.get("sources_l2", {}),
```

2. `write_copied_forward_run` — in the `run` dict, after `"errors"`:

```python
        "cache": None,
        "detected_services": prev.get("detected_services", {}),
        "sources_l2": prev.get("sources_l2", {}),
```

3. `launch` — new signature `def launch(root, slug, cfg, dry_run=False, use_cache=True):`. Keep the dry-run early return exactly where it is (before any file ops). Replace the stale-file wipe + env setup with:

```python
    wd.mkdir(parents=True, exist_ok=True)
    seed = wd / f"{tag}.seed.tsv"
    stale_tsv = wd / f"{tag}.sizes.tsv"
    if use_cache and stale_tsv.exists():
        os.replace(stale_tsv, seed)  # crashed-run progress becomes a seed
    for stale in wd.glob(f"{tag}.*"):  # a stale .done would fake completion
        if stale != seed:
            stale.unlink()
    env = os.environ.copy()
    env.pop("CACHE_FILE", None)   # never inherit these from our own process
    env.pop("SEED_TSV", None)
    env.pop("EXPECTED_SERVICES", None)
    env.update({"SA": cfg["storage_account"], "CONTAINER": cfg["container"],
                "TAG": tag, "OUT_DIR": str(wd), "AZURE_STORAGE_SAS": sas})
    index = common.company_dir(root, slug) / "blob-index.tsv.gz"
    if use_cache and index.exists():
        env["CACHE_FILE"] = str(index)
    if use_cache and seed.exists():
        env["SEED_TSV"] = str(seed)
    expected = common.load_expected(root, slug)
    services = ",".join((expected or {}).get("services", {}).keys())
    if services:
        env["EXPECTED_SERVICES"] = services
```

Also update the phases-module docstring (phase 1 line) to mention cache seeding.

4. `harvest_one` — after `common.write_json(path, run)` and before `cleanup(...)`:

```python
    idx = wd / f"{tag}.index.tsv.gz"
    if idx.exists():  # per-blob detail survives harvest as the company's cache
        os.replace(idx, common.company_dir(root, slug) / "blob-index.tsv.gz")
```

(`cleanup` then removes the seed and remaining work files via the existing glob — no cleanup changes needed. Update `cleanup`'s docstring: the per-blob detail now lives on in `blob-index.tsv.gz`.)

5. `scripts/fleet_size.py` — add to argparse:

```python
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the per-company blob index (full re-size)")
```

and in `cmd_launch_all` change the launch call:

```python
            launched = phases.launch(root, slug, cfg, dry_run=args.dry_run,
                                     use_cache=not getattr(args, "no_cache",
                                                           False))
```

6. `scripts/size_company.py` — add the identical `--no-cache` argparse line (it forwards the whole `args` object to `fleet_size` functions, so nothing else changes).

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py` — all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/phases.py scripts/fleet_size.py scripts/size_company.py tests/test_harness.py
git commit -m "harness: cache/seed/services env at launch, index survives harvest, --no-cache"
```

---

### Task 7: Reconcile `found-embedded` + detection notes + report badge

**Files:**
- Modify: `scripts/reconcile.py` (`service_rows`, new `detection_notes`, `company_summary`)
- Modify: `scripts/gen_report.py:101-106` (`FLAG_BADGES`)
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: run-JSON `detected_services` (Task 6 shape).
- Produces: rows from `service_rows` may carry `"embedded_bytes": int` and `"embedded_in": list[str]` and the flag `"found-embedded"` (which REPLACES `declared-empty` when detection found the service's data elsewhere); `detection_notes(rows, expected, run) -> list[str]`; `company_summary`'s `"notes"` = `lore_notes(latest) + detection_notes(rows, expected, latest)`. Note thresholds: undeclared detected services are only surfaced at ≥1 GB (`1e9` bytes) to keep noise out of client-facing reports.
- Old-run compatibility: `run.get("detected_services")` absent → `{}` → zero new flags/notes (democo fixture proves it — its existing `declared-empty` check on hubspot must keep passing).

- [ ] **Step 1: Write the failing tests**

Add a new section after the `passco` block in `tests/test_harness.py`:

```python
    print("\n— reconcile: embedded-service detection —")
    embedco = root / "embedco"
    (embedco / "sizing-runs").mkdir(parents=True)
    common.write_json(embedco / "config.json", {
        "slug": "embedco", "subscription": "m1 corpus", "subscription_id": "x",
        "resource_group": "rg-embedco", "storage_account": "stembedco",
        "container": "embedco-raw",
        "vm": {"name": None, "resource_group": "rg-embedco", "exists": False},
        "onboarded_at": "2026-08-01T00:00:00Z"})
    common.write_json(embedco / "expected-data-sizes.json", {
        "slug": "embedco", "manifest_total_bytes": 2_000_000_000_000,
        "services": {"gdrive": {"bytes": 1_500_000_000_000},
                     "hubspot": {"bytes": 500_000_000_000}},
        "source": "test", "confirmed_by_user": True,
        "created_at": "2026-08-01T00:00:00Z"})
    common.write_json(embedco / "status.json", {
        "slug": "embedco", "stage": "pushing",
        "last_run": {"timestamp": "2026-08-13T09:00:00Z", "outcome": "sized",
                     "reason": None},
        "last_change_detected_at": "2026-08-13T09:00:00Z"})
    common.write_json(embedco / "sizing-runs" / "20260813T100000Z.json", {
        "slug": "embedco", "timestamp": "2026-08-13T10:00:00Z",
        "method": "sized", "copied_from": None,
        "used_capacity_bytes": 1_000_000_000_000,
        "used_capacity_at": "2026-08-13T09:00:00Z", "duration_seconds": 60,
        "totals": {"blob_count": 10, "compressed_bytes": 1_000_000_000_000,
                   "uncompressed_bytes": 1_200_000_000_000,
                   "zero_byte_blobs": 0},
        "sources": {"workspace-export": {
            "blob_count": 10, "compressed_bytes": 1_000_000_000_000,
            "uncompressed_bytes": 1_200_000_000_000}},
        "methods": {"zip": 10}, "errors": {"total": 0, "by_type": {}},
        "cache": {"hits": 0, "misses": 10},
        "detected_services": {
            "hubspot": {"bytes": 400_000_000_000, "blob_count": 0,
                        "entry_count": 12000, "path_bytes": 0,
                        "zip_entry_bytes": 400_000_000_000,
                        "sources": {"workspace-export": 400_000_000_000}},
            "stripe": {"bytes": 5_000_000_000, "blob_count": 0,
                       "entry_count": 40, "path_bytes": 0,
                       "zip_entry_bytes": 5_000_000_000,
                       "sources": {"workspace-export": 5_000_000_000}}},
        "sources_l2": {"workspace-export/hubspot": [4, 0, 400_000_000_000]},
        "notes": []})
    es = reconcile.company_summary(root, "embedco")
    eflags = {r["service"]: r["flags"] for r in es["service_rows"]}
    erows = {r["service"]: r for r in es["service_rows"]}
    check("hubspot found-embedded (not declared-empty)",
          eflags["hubspot"] == ["found-embedded"], str(eflags))
    check("embedded bytes + location recorded",
          erows["hubspot"]["embedded_bytes"] == 400_000_000_000
          and erows["hubspot"]["embedded_in"] == ["workspace-export"])
    enotes = " ".join(es["notes"])
    check("embedded note names hubspot + host",
          "hubspot" in enotes and "workspace-export" in enotes, enotes)
    check("undeclared stripe surfaced (≥1GB)", "stripe" in enotes, enotes)
    proc = run_script("gen_report.py", "embedco", "--root", root)
    ehtml = Path(proc.stdout.strip()).read_text()
    check("report renders found-embedded badge",
          "embedded in another source" in ehtml)
```

Also confirm the pre-existing democo check `hubspot declared-empty flagged` still passes — that IS the old-run-compat test.

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — `hubspot found-embedded` (flags are `["declared-empty"]`).

- [ ] **Step 3: Implement**

`scripts/reconcile.py`:

1. In `service_rows`, read detection once at the top:

```python
    detected = (run or {}).get("detected_services", {})
```

add a helper above `service_rows`:

```python
def _find_detected(service: str, detected: dict) -> dict | None:
    n = norm(service)
    for svc, d in detected.items():
        if norm(svc) == n:
            return d
    return None
```

and replace the `declared-empty` branch:

```python
            if row["actual_bytes"] == 0:
                d = _find_detected(svc, detected)
                if d and d.get("bytes", 0) > 0:
                    # data exists, just inside another source's blobs
                    row["flags"].append("found-embedded")
                    row["embedded_bytes"] = d["bytes"]
                    row["embedded_in"] = sorted(
                        d.get("sources", {}),
                        key=lambda s: -d["sources"][s])
                else:
                    row["flags"].append("declared-empty")
```

2. Add `detection_notes` after `lore_notes`:

```python
UNDECLARED_NOTE_FLOOR = 1_000_000_000  # surface undeclared finds ≥1 GB only


def detection_notes(rows: list[dict], expected: dict | None,
                    run: dict | None) -> list[str]:
    """Notes from the sizer's embedded-service detection: declared services
    found inside other sources, and material undeclared discoveries."""
    notes = []
    for r in rows:
        if "found-embedded" in r["flags"]:
            hosts = ", ".join(r["embedded_in"][:3])
            notes.append(
                f"{r['service']} shows no top-level data, but "
                f"~{r['embedded_bytes'] / 1e9:.1f} GB of {r['service']} data "
                f"was detected inside {hosts} — likely exported within "
                f"another service's archive.")
    declared = {norm(s) for s in (expected or {}).get("services", {})}
    tops = {norm(s) for s in (run or {}).get("sources", {})}
    for svc, d in (run or {}).get("detected_services", {}).items():
        if norm(svc) in declared or norm(svc) in tops:
            continue
        if d.get("bytes", 0) >= UNDECLARED_NOTE_FLOOR:
            hosts = ", ".join(sorted(d.get("sources", {}),
                                     key=lambda s: -d["sources"][s])[:3])
            notes.append(
                f"Detected ~{d['bytes'] / 1e9:.1f} GB of {svc} data under "
                f"{hosts}; {svc} is not declared in the manifest.")
    return notes
```

3. In `company_summary`, change the notes line:

```python
        "notes": lore_notes(latest) + detection_notes(rows, expected, latest),
```

4. `scripts/gen_report.py` — add to `FLAG_BADGES`:

```python
    "found-embedded": ("embedded in another source", "info"),
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py` — all pass (democo's `declared-empty` untouched, embedco's new flags present).

- [ ] **Step 5: Commit**

```bash
git add scripts/reconcile.py scripts/gen_report.py tests/test_harness.py
git commit -m "reconcile: found-embedded flag + detection notes; report badge"
```

---

### Task 8: Documentation (CLAUDE.md, SKILL.md, sizing-lore.md) + final validation

**Files:**
- Modify: `CLAUDE.md` (repo map, sizing-run schema, local-sizing section)
- Modify: `.claude/skills/size-company/SKILL.md`
- Modify: `.claude/skills/size-company/references/sizing-lore.md`

**Interfaces:** none produced — this task documents the contracts from Tasks 1–7 verbatim.

- [ ] **Step 1: Update CLAUDE.md**

1. Repo map: under `companies/<slug>/`, after the `reports/` line add:

```
    blob-index.tsv.gz        # per-blob sizing cache (zip/gz rows, ETag-keyed);
                             # rebuilt by each harvest — the incremental-run seed
```

2. Sizing-run schema block: after the `"errors"` line add:

```json
  "cache": {"hits": 800, "misses": 6},   // null in copied-forward and pre-cache runs
  "detected_services": {                 // path + zip-entry service detection (additive
    "hubspot": {                         // lens — NEVER feeds the headline %)
      "bytes": 400000000000, "blob_count": 0, "entry_count": 12000,
      "path_bytes": 0, "zip_entry_bytes": 400000000000,
      "sources": {"workspace-export": 400000000000}
    }
  },
  "sources_l2": {"workspace-export/hubspot": [4, 0, 400000000000]}, // top-40 second-level
                                         // [files, comp, unc] triples + "(other)" rollup
```

3. "Local sizing execution" section: update the Launch/Harvest bullets to say: launch seeds the sizer from `companies/<slug>/blob-index.tsv.gz` (`CACHE_FILE`) plus any crashed run's partial TSV (renamed to `<tag>.seed.tsv`, passed as `SEED_TSV`), and passes declared service names as `EXPECTED_SERVICES`; harvest moves the fresh `<tag>.index.tsv.gz` to `companies/<slug>/blob-index.tsv.gz` before cleanup — per-blob detail now SURVIVES harvest. Add: cache hits are ETag-validated (name+etag+size), error rows are never cached, `--no-cache` forces a full re-size; the sizer runs prefix-parallel listing (`LIST_WORKERS`, default 8) and pooled zip/gz reads (`SIZER_WORKERS`, default 16).

4. "Learned the hard way": add one bullet:

```
- **Detection is a lens, not a ledger:** `detected_services` (deepest-wins path
  match + zip central-directory entry names) attributes bytes to services even
  inside wrapper exports — but the headline % and per-source reconciliation
  still run on `sources`. A declared service found only inside another
  source's archives is flagged `found-embedded`, not `declared-empty`.
```

- [ ] **Step 2: Update SKILL.md**

In `.claude/skills/size-company/SKILL.md`:

1. In the intro paragraph, after the read-only sentence, add: "Repeat runs are incremental: per-blob results are cached in `companies/<slug>/blob-index.tsv.gz` (ETag-validated), so an unchanged container costs only its listing time."
2. In "Run it", after the poll/harvest block, add:

```markdown
`--no-cache` forces a full re-size (use when numbers look suspicious and you
want zero reuse). First run for a company has no cache — budget the full
time; repeat runs skip the per-blob reads for every unchanged blob.
```

3. In "Interpreting outcomes" under `sized`, add: "Check `cache.hits`/`cache.misses` in the run file — a warm run with unexpected mass misses means the client re-uploaded (overwrote) blobs, which is itself worth mentioning to the user. Check `detected_services`: declared services found embedded inside another source (e.g. CRM exports inside a Workspace/Takeout archive) are flagged `found-embedded` in reports — present them as found, with their host prefix, not as missing."
4. In "Judgment calls", REPLACE the bullet about `sizes.tsv` being cleaned up ("Per-blob detail ... copy it out before harvesting if the user wants it") with: "Per-blob detail survives harvest in `companies/<slug>/blob-index.tsv.gz` (zip/gz rows: name, etag, sizes, method, embedded-service hits). `zcat` it for on-demand per-blob answers. Deleting the file is always safe — the next run just does a full re-size."

- [ ] **Step 3: Update sizing-lore.md**

In "Reading a sizing run", update the timestamp-prefix bullet to add: "`sources_l2` in the run file now records the second-level split automatically — read it before manually re-splitting." Add two bullets:

```markdown
- **Embedded services are real bytes:** zip central directories carry entry
  names + exact uncompressed sizes; `detected_services` attributes them
  (deepest path segment wins, so nothing double-counts within a service).
  `found-embedded` on a declared service = the data arrived, wrapped in
  another export.
- **Cache hits are ETag-proof:** a warm run reuses per-blob numbers only when
  name+ETag+size all match — overwritten blobs always re-size. Mass cache
  misses on a warm run = the client re-uploaded, not a bug.
```

In "Reconciling vs declared", add: "- `found-embedded` replaces `declared-empty` when detection locates a declared service inside another source; undeclared detections ≥1 GB get a note — both are talking points, not alarms."

- [ ] **Step 4: Full validation run**

Run: `python3 tests/test_harness.py`
Expected: all checks pass.
Run: `PATH="/opt/homebrew/bin:$PATH" python3 scripts/fleet_size.py launch-all --dry-run --root tests/fixtures/companies --slugs democo`
Expected: exits 0, prints the local detached launch command (no file writes — dry-run).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .claude/skills/size-company/SKILL.md .claude/skills/size-company/references/sizing-lore.md
git commit -m "docs: incremental sizing cache, detection lens, updated skill guidance"
```

---

## Post-plan verification (executor note)

After all tasks: run the live integration on croplabel when the user is ready (NOT part of automated execution — needs Azure):

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/size_company.py croplabel            # cold: populates blob-index
python3 scripts/size_company.py croplabel --no-cache # baseline comparison, optional
python3 scripts/size_company.py croplabel            # warm: expect cache.hits > 0, same totals
```

Confirm: warm run's totals match cold run's; `companies/croplabel/blob-index.tsv.gz` exists; run file has `cache.hits > 0`.
