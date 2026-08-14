# gz Size Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make gz sizing trustworthy: reject provably-garbage trailers, quantify what trailers cannot measure, and stream-measure large/suspect gz blobs exactly (bounded by a per-run budget, cached forever by ETag).

**Architecture:** All sizer changes live in `scripts/corpus_sizer_rest.py` (single stdlib-only file): a method taxonomy split (`gz-trailer`/`gz-floor`/`gz-bad-trailer`/`gz-exact`), a multi-member-aware streaming decompressor with a thread-safe byte budget, a cache-staleness rule so old trailer rows re-measure once when streaming would cover them, and `gz` stats in summary.json. `phases.py` passes the new field through; `reconcile.py` emits a quantified note. Spec: `docs/superpowers/specs/2026-08-14-gz-accuracy-design.md`.

**Tech Stack:** Python 3 stdlib only (`zlib` is the one new import). Tests are the existing monolithic `python3 tests/test_harness.py` (its `check()` convention — NOT pytest).

**Spec:** `docs/superpowers/specs/2026-08-14-gz-accuracy-design.md`

## Global Constraints

- Stdlib-only; `corpus_sizer_rest.py` stays a single self-contained file, import-safe (env read only in `_init_from_env`).
- Read-only against client storage: same `rl` SAS; streaming is a plain GET.
- Fail-safe direction is sacred: streaming failure falls back to the trailer/floor value and counts as uncertain — streaming can NEVER fail a run that would otherwise succeed. A cache can only cost time, never correctness.
- New run-JSON field `gz` is ADDITIVE: `{"streamed": n, "streamed_bytes": b, "uncertain": N, "uncertain_bytes": X}` (bytes = compressed). Old run files lack it → consumers treat as unknown, not zero.
- Env knobs with exact defaults (decimal, repo convention): `GZ_STREAM_THRESHOLD=256_000_000`, `GZ_STREAM_FLOOR_MIN=8_000_000`, `GZ_STREAM_BUDGET=50_000_000_000` (`0` disables streaming).
- All existing test-harness checks must keep passing. Do NOT hardcode total check counts in assertions or reports beyond "0 FAIL" — the working tree may carry the user's in-flight gdrive work which adds checks.
- Commit with EXACTLY the `git add` paths listed per task — never `git add -A`. EXECUTION PREREQUISITE: `tests/test_harness.py` currently carries uncommitted third-party (gdrive-transfer) hunks; the controller must get those committed (or confirmed separated) BEFORE Task 1's first commit, exactly as was done for the previous plan.
- Threading invariants from `docs/sizing-internals.md` §12 hold: only the consumer thread mutates the Aggregator; `size_blob` never raises.

## File Structure

- `scripts/corpus_sizer_rest.py` — Tasks 1–3 (taxonomy, streaming, integration).
- `scripts/phases.py`, `scripts/reconcile.py` — Task 4 (passthrough + note).
- `CLAUDE.md`, `docs/sizing-internals.md`, `.claude/skills/size-company/SKILL.md`, `.claude/skills/size-company/references/sizing-lore.md` — Task 4 docs.
- `tests/test_harness.py` — every task adds to the existing sizer sections.

---

### Task 1: Trailer method taxonomy + impossible-ratio guard

**Files:**
- Modify: `scripts/corpus_sizer_rest.py` (`gz_uncompressed`, currently returning only `gz-tiny`/`gz-trailer`)
- Test: `tests/test_harness.py`

**Interfaces:**
- Produces: `gz_uncompressed(name, clen) -> (uncomp, method)` with methods:
  `"gz-tiny"` (clen < 4, unchanged), `"gz-bad-trailer"` (ISIZE > clen×1032, DEFLATE's hard bound — provably garbage; returns clen), `"gz-floor"` (ISIZE < clen — wrap/multi-member/incompressible tell; returns clen), `"gz-trailer"` (plausible; returns ISIZE). Tasks 2–3 branch on these exact strings.
- The 1032 constant is `DEFLATE_MAX_RATIO = 1032` at module level (near the gz function).

- [ ] **Step 1: Write the failing tests**

In `tests/test_harness.py`, append to the sizer section (after the zip-entry-detection block; `import corpus_sizer_rest as sizer` and `struct` are already in scope). Monkeypatch `fetch_range` to serve crafted trailers:

```python
    print("\n— sizer: gz trailer taxonomy —")
    real_fetch = sizer.fetch_range
    try:
        trailers = {}
        sizer.fetch_range = lambda name, s, e: trailers[name]
        trailers["a.gz"] = struct.pack("<I", 3000)
        check("plausible trailer", sizer.gz_uncompressed("a.gz", 1000)
              == (3000, "gz-trailer"))
        trailers["b.gz"] = struct.pack("<I", 500)
        check("floored trailer", sizer.gz_uncompressed("b.gz", 1000)
              == (1000, "gz-floor"))
        trailers["c.gz"] = struct.pack("<I", 0xFFFFFFFF)
        check("garbage trailer (ratio > 1032x)",
              sizer.gz_uncompressed("c.gz", 1000) == (1000, "gz-bad-trailer"))
        trailers["d.gz"] = struct.pack("<I", 1032 * 1000)  # exactly at bound
        check("exactly 1032x is allowed",
              sizer.gz_uncompressed("d.gz", 1000) == (1032000, "gz-trailer"))
        check("tiny", sizer.gz_uncompressed("e.gz", 3) == (3, "gz-tiny"))
    finally:
        sizer.fetch_range = real_fetch
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — `floored trailer` gets `(1000, "gz-trailer")` (old code returns the floor under the old method name).

- [ ] **Step 3: Implement**

Replace `gz_uncompressed` in `scripts/corpus_sizer_rest.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py`
Expected: 0 FAIL. Note: pre-existing offline end-to-end checks use compressible fake gz blobs whose ISIZE ≥ clen, so their method stays `gz-trailer` and nothing else shifts — if an existing check DOES fail, inspect whether its fixture blob is incompressible (now `gz-floor`) before touching any assertion, and report it in your task report rather than silently editing unrelated checks.

- [ ] **Step 5: Commit**

```bash
git add scripts/corpus_sizer_rest.py tests/test_harness.py
git commit -m "sizer: gz trailer taxonomy + DEFLATE 1032x garbage-trailer guard"
```

---

### Task 2: Streaming machinery — exact multi-member decompression + budget

**Files:**
- Modify: `scripts/corpus_sizer_rest.py` (new functions; add `import zlib`)
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: `blob_base`, `_auth` (existing).
- Produces (Task 3 depends on these exact signatures):
  - `stream_blob_chunks(name, chunk=1 << 20)` — generator yielding raw bytes of the blob via a chunked GET (its own urlopen; the buffering `http_get` must NOT be used). Tests monkeypatch THIS function.
  - `gz_stream_exact(name) -> int` — total uncompressed bytes across ALL gzip members; raises `ValueError("truncated gzip stream")` on mid-member EOF; raises `zlib.error` on non-gzip bytes.
  - `StreamBudget(limit)` with `reserve(n) -> bool` — thread-safe, reserve-before-download, no refunds (conservative), and attribute `used`.

- [ ] **Step 1: Write the failing tests**

Append to the sizer section (`gzip` is already imported at the top of the test file):

```python
    print("\n— sizer: gz exact streaming primitives —")
    real_stream = sizer.stream_blob_chunks
    try:
        blobs = {}

        def fake_stream(name, chunk=7):  # tiny chunks: exercise boundaries
            data = blobs[name]
            for i in range(0, len(data), chunk):
                yield data[i:i + chunk]

        sizer.stream_blob_chunks = fake_stream
        blobs["multi.gz"] = gzip.compress(b"a" * 5000) + gzip.compress(b"b" * 7000)
        check("multi-member exact sum", sizer.gz_stream_exact("multi.gz") == 12000)
        blobs["bgzip.gz"] = gzip.compress(b"x" * 9000) + gzip.compress(b"")
        check("bgzip-style empty EOF member", sizer.gz_stream_exact("bgzip.gz") == 9000)
        blobs["trunc.gz"] = gzip.compress(b"y" * 5000)[:-8]
        try:
            sizer.gz_stream_exact("trunc.gz")
            check("truncated stream raises", False)
        except ValueError:
            check("truncated stream raises", True)
        blobs["junk.gz"] = b"\x00" * 64
        try:
            sizer.gz_stream_exact("junk.gz")
            check("non-gzip bytes raise", False)
        except Exception:
            check("non-gzip bytes raise", True)
    finally:
        sizer.stream_blob_chunks = real_stream
    b = sizer.StreamBudget(100)
    check("budget reserve/deny", b.reserve(60) and not b.reserve(50)
          and b.reserve(40) and not b.reserve(1) and b.used == 100)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — `AttributeError: module 'corpus_sizer_rest' has no attribute 'stream_blob_chunks'`.

- [ ] **Step 3: Implement**

Add `import zlib` to the imports. Add below `gz_uncompressed`:

```python
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


def gz_stream_exact(name):
    """Exact uncompressed size: stream-decompress every gzip member,
    counting output bytes only (constant memory). Handles the two cases the
    trailer cannot: >=4GiB wraps and multi-member/concatenated gzips.
    Network cost = compressed size — paid once; the result is cached by
    ETag as method gz-exact. Raises on truncated or non-gzip input."""
    total = 0
    d = zlib.decompressobj(wbits=31)  # 31 = gzip container
    for chunk in stream_blob_chunks(name):
        while chunk:
            if d.eof:  # previous member finished — start the next
                d = zlib.decompressobj(wbits=31)
            total += len(d.decompress(chunk))
            chunk = d.unused_data if d.eof else b""
    if not d.eof:
        raise ValueError("truncated gzip stream")
    return total


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
```

Walkthrough of `gz_stream_exact`'s inner loop (for the reviewer): a fresh decompressobj has `eof=False`; when a member ends mid-chunk, `d.eof` flips and `unused_data` carries the remaining bytes, which the `while chunk` loop feeds to a fresh decompressobj. At stream end, `d.eof` is True iff the final member completed cleanly — a truncated member leaves it False. Bytes after a member that aren't gzip raise `zlib.error` from `decompress`, which propagates to the caller (Task 3 treats any exception as stream failure → fallback).

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py` — 0 FAIL.

- [ ] **Step 5: Commit**

```bash
git add scripts/corpus_sizer_rest.py tests/test_harness.py
git commit -m "sizer: multi-member gz streaming decompressor + thread-safe byte budget"
```

---

### Task 3: Pipeline integration — trigger, fallback, cache staleness, gz stats

**Files:**
- Modify: `scripts/corpus_sizer_rest.py` (`_init_from_env`, `size_blob`, `handle_blob` inside `enumerate_and_size`, `Aggregator`, `write_summary`, module docstring)
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: Task 1 method strings; Task 2 `gz_stream_exact`/`stream_blob_chunks`/`StreamBudget`.
- Produces:
  - Env-backed globals (set in `_init_from_env`): `GZ_STREAM_THRESHOLD` (default `256_000_000`), `GZ_STREAM_FLOOR_MIN` (default `8_000_000`), `GZ_STREAM_BUDGET` (default `50_000_000_000`; `0` disables).
  - `gz_stream_candidate(method, clen) -> bool` (pure): False when `GZ_STREAM_BUDGET <= 0`; True when `clen >= GZ_STREAM_THRESHOLD`; else True when `method in ("gz-floor", "gz-bad-trailer") and clen >= GZ_STREAM_FLOOR_MIN`.
  - `gz_cached_row_stale(method, clen, uncomp) -> bool` (pure): the cache-migration rule — a non-`gz-exact` gz row is stale iff streaming is enabled AND (`clen >= GZ_STREAM_THRESHOLD`, or it is floored — `method in ("gz-floor","gz-bad-trailer")` or `uncomp == clen` (covers pre-taxonomy `gz-trailer` rows) — with `clen >= GZ_STREAM_FLOOR_MIN`).
  - `gz_uncertain_row(kind, method, clen) -> bool` (pure): True for gz rows with method `gz-floor`/`gz-bad-trailer`, or `gz-trailer` with `clen >= GZ_STREAM_THRESHOLD`.
  - `size_blob(name, clen, etag, kind, matcher, budget=None)` — new kwarg; on a gz stream-candidate with reserved budget, retries `gz_stream_exact` 3 attempts (backoff 1s, 2s) then falls back to the trailer value on failure.
  - `summary.json` gains `"gz": {"streamed": n, "streamed_bytes": b, "uncertain": N, "uncertain_bytes": X}` (always present in new summaries, even all-zero).

- [ ] **Step 1: Write the failing tests**

Append a new self-contained end-to-end block to the sizer section, after the existing offline end-to-end (which must remain untouched). It reuses the `make_zip`-style monkeypatch approach but with its own container and env; `os`, `json`, `shutil`, `gzip`, `struct`, `urllib.parse`, `Path` are in scope. Note `SIZER_WORKERS=1` and a single prefix make budget-ordering deterministic.

```python
    print("\n— sizer: gz streaming end-to-end (trigger, budget, cache) —")
    gzc = {
        # floored (bgzip-style) 9000-logical, small clen -> floor-min trigger
        "gz/a-multi.gz": gzip.compress(b"x" * 9000) + gzip.compress(b""),
        # floored, second in listing order — budget will exclude it
        "gz/b-multi.gz": gzip.compress(b"y" * 4000) + gzip.compress(b""),
        # garbage trailer: gzip magic + junk + huge ISIZE; stream must FAIL -> fallback
        "gz/c-junk.gz": b"\x1f\x8b" + b"\x00" * 60 + struct.pack("<I", 0xFFFFFFFF),
        # plausible trailer, clen >= threshold -> streamed exact
        # (30000 -> clen ~64, ratio ~469: safely under the 1032x bound)
        "gz/d-big.gz": gzip.compress(b"z" * 30000),
        # plausible trailer, below every trigger -> stays gz-trailer, certain
        "gz/e-small.gz": gzip.compress(b"w" * 2000),
    }
    getags = {n: f"0xGZ{i:02d}" for i, n in enumerate(sorted(gzc))}
    stream_calls = []

    def gz_listing_xml(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        prefix = q.get("prefix", [""])[0]
        delim = q.get("delimiter", [""])[0]
        parts, seen = ["<EnumerationResults><Blobs>"], set()
        for n in sorted(k for k in gzc if k.startswith(prefix)):
            rest = n[len(prefix):]
            if delim and delim in rest:
                p = prefix + rest.split(delim)[0] + delim
                if p not in seen:
                    seen.add(p)
                    parts.append(f"<BlobPrefix><Name>{p}</Name></BlobPrefix>")
                continue
            parts.append(f"<Blob><Name>{n}</Name><Properties>"
                         f"<Content-Length>{len(gzc[n])}</Content-Length>"
                         f"<Etag>{getags[n]}</Etag></Properties></Blob>")
        parts.append("</Blobs><NextMarker/></EnumerationResults>")
        return "".join(parts).encode()

    def gz_http(url, extra_headers=None):
        if "comp=list" in url:
            return gz_listing_xml(url)
        name = urllib.parse.unquote(url.split("/gz-raw/", 1)[1].split("?", 1)[0])
        data = gzc[name]
        rng = (extra_headers or {}).get("Range")
        if rng:
            a, bb = rng[len("bytes="):].split("-")
            return data[int(a):int(bb) + 1]
        return data

    def gz_fake_stream(name, chunk=1 << 20):
        stream_calls.append(name)
        data = gzc[name]
        for i in range(0, len(data), 13):
            yield data[i:i + 13]

    gzwork = tmp / "gz-work"
    gzwork.mkdir()
    candidates_clen = (len(gzc["gz/a-multi.gz"]) + len(gzc["gz/b-multi.gz"])
                       + len(gzc["gz/c-junk.gz"]) + len(gzc["gz/d-big.gz"]))
    gz_env = {"SA": "gzsa", "CONTAINER": "gz-raw", "SAS": "sig=g",
              "TAG": "gzco-sizer", "OUT_DIR": str(gzwork),
              "SIZER_WORKERS": "1", "LIST_WORKERS": "1",
              "GZ_STREAM_THRESHOLD": str(len(gzc["gz/d-big.gz"])),
              "GZ_STREAM_FLOOR_MIN": "1",
              "GZ_STREAM_BUDGET": str(candidates_clen)}  # run 1: fits ALL
    os.environ.update(gz_env)
    for k in ("CACHE_FILE", "SEED_TSV", "EXPECTED_SERVICES"):
        os.environ.pop(k, None)
    real_http2, real_stream2 = sizer.http_get, sizer.stream_blob_chunks
    try:
        sizer.http_get = gz_http
        sizer.stream_blob_chunks = gz_fake_stream

        # ── run 1 (cold, budget fits all candidates) ──
        # a-multi, b-multi: gz-floor (empty last member) -> streamed exact
        # c-junk: gz-bad-trailer -> stream attempted 3x, zlib.error -> fallback
        # d-big: gz-trailer with clen == threshold -> streamed exact
        # e-small: gz-trailer below threshold -> untouched, certain
        sizer.main()
        g1 = json.loads((gzwork / "gzco-sizer.summary.json").read_text())
        tsv = {ln.split("\t")[0]: ln.split("\t")
               for ln in (gzwork / "gzco-sizer.sizes.tsv").read_text()
               .rstrip("\n").split("\n")[1:]}
        check("a-multi gz-exact 9000", tsv["gz/a-multi.gz"][4] == "gz-exact"
              and tsv["gz/a-multi.gz"][2] == "9000")
        check("b-multi gz-exact 4000", tsv["gz/b-multi.gz"][4] == "gz-exact"
              and tsv["gz/b-multi.gz"][2] == "4000")
        check("c-junk: 3 stream attempts then fallback",
              stream_calls.count("gz/c-junk.gz") == 3
              and tsv["gz/c-junk.gz"][4] == "gz-bad-trailer"
              and tsv["gz/c-junk.gz"][2] == str(len(gzc["gz/c-junk.gz"])))
        check("d-big gz-exact 30000", tsv["gz/d-big.gz"][4] == "gz-exact"
              and tsv["gz/d-big.gz"][2] == "30000")
        check("e-small stays gz-trailer, never streamed",
              tsv["gz/e-small.gz"][4] == "gz-trailer"
              and "gz/e-small.gz" not in stream_calls)
        check("run1 gz stats", g1["gz"]["streamed"] == 3
              and g1["gz"]["uncertain"] == 1
              and g1["gz"]["uncertain_bytes"] == len(gzc["gz/c-junk.gz"]))

        # ── warm run: gz-exact rows are permanent hits; the garbage blob is
        # stale (bad-trailer meeting the trigger) and gets re-attempted ──
        shutil.copy(gzwork / "gzco-sizer.index.tsv.gz", tmp / "gz-index.tsv.gz")
        for f in gzwork.glob("gzco-sizer.*"):
            f.unlink()
        os.environ["CACHE_FILE"] = str(tmp / "gz-index.tsv.gz")
        stream_calls.clear()
        sizer.main()
        g2 = json.loads((gzwork / "gzco-sizer.summary.json").read_text())
        check("warm: only the garbage blob re-attempted",
              set(stream_calls) == {"gz/c-junk.gz"}
              and g2["cache"]["hits"] >= 3 and g2["gz"]["streamed"] == 0)

        # ── budget starvation (cold, budget fits only a-multi; single worker
        # makes reservation order = listing order a, b, c, d) ──
        for f in gzwork.glob("gzco-sizer.*"):
            f.unlink()
        os.environ.pop("CACHE_FILE", None)
        os.environ["GZ_STREAM_BUDGET"] = str(len(gzc["gz/a-multi.gz"]))
        stream_calls.clear()
        sizer.main()
        g3 = json.loads((gzwork / "gzco-sizer.summary.json").read_text())
        check("starved: one streamed, rest uncertain (b floor, c bad, d big)",
              g3["gz"]["streamed"] == 1 and g3["gz"]["uncertain"] == 3)

        # ── budget=0: streaming AND staleness off (no perpetual misses) ──
        for f in gzwork.glob("gzco-sizer.*"):
            f.unlink()
        os.environ["CACHE_FILE"] = str(tmp / "gz-index.tsv.gz")  # run-1 index
        os.environ["GZ_STREAM_BUDGET"] = "0"
        stream_calls.clear()
        sizer.main()
        g4 = json.loads((gzwork / "gzco-sizer.summary.json").read_text())
        check("budget=0: no streams, exact+bad rows all hit",
              stream_calls == [] and g4["gz"]["streamed"] == 0
              and g4["cache"]["hits"] >= 4)
    finally:
        sizer.http_get, sizer.stream_blob_chunks = real_http2, real_stream2
        for k in list(gz_env) + ["CACHE_FILE", "SEED_TSV"]:
            os.environ.pop(k, None)

    # ── pure predicates (set globals directly; any later sizer.main() call
    # re-runs _init_from_env, which resets them from env) ──
    sizer.GZ_STREAM_BUDGET, sizer.GZ_STREAM_THRESHOLD, sizer.GZ_STREAM_FLOOR_MIN = 1, 100, 10
    check("candidate: threshold", sizer.gz_stream_candidate("gz-trailer", 100))
    check("candidate: floored above min", sizer.gz_stream_candidate("gz-floor", 10))
    check("candidate: floored below min",
          not sizer.gz_stream_candidate("gz-floor", 9))
    check("candidate: plausible below threshold",
          not sizer.gz_stream_candidate("gz-trailer", 99))
    sizer.GZ_STREAM_BUDGET = 0
    check("candidate: disabled", not sizer.gz_stream_candidate("gz-trailer", 100))
    check("stale: disabled", not sizer.gz_cached_row_stale("gz-trailer", 500, 500))
    sizer.GZ_STREAM_BUDGET = 1
    check("stale: old-gen floored trailer row",
          sizer.gz_cached_row_stale("gz-trailer", 50, 50))
    check("stale: exact never", not sizer.gz_cached_row_stale("gz-exact", 500, 500))
    check("stale: big plausible", sizer.gz_cached_row_stale("gz-trailer", 100, 300))
    check("uncertain rows", sizer.gz_uncertain_row("gz", "gz-floor", 5)
          and sizer.gz_uncertain_row("gz", "gz-trailer", 100)
          and not sizer.gz_uncertain_row("gz", "gz-trailer", 99)
          and not sizer.gz_uncertain_row("zip", "zip:3entries", 500))
```

Expected-value notes for the executor: run 1's streamed set is {a-multi,
b-multi, d-big} (3) and c-junk is the single uncertain row. The warm run's
`g2["gz"]["streamed"] == 0` is because `streamed` counts only non-cached
`gz-exact` rows and c-junk's re-attempt fails again (its 3 retries cost ~3s
of test sleep each run — expected). In the starved run, the single worker
processes submissions in listing order, so a-multi reserves the whole budget
and b/c/d are denied deterministically. In the budget=0 run the staleness
rule is off, so even the `gz-bad-trailer` cached row is a hit (4 hits: a, b,
c, d; e-small has a `gz-trailer` row in the index too — hence `>= 4`).

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — `AttributeError` on `gz_stream_candidate` (or missing `gz` key in summary), depending on interpreter order.

- [ ] **Step 3: Implement**

In `scripts/corpus_sizer_rest.py`:

1. Config: add to the module-global block `GZ_STREAM_THRESHOLD = 256_000_000`,
   `GZ_STREAM_FLOOR_MIN = 8_000_000`, `GZ_STREAM_BUDGET = 50_000_000_000`, and
   to `_init_from_env` (inside the existing `global` statement too):

```python
    GZ_STREAM_THRESHOLD = int(os.environ.get("GZ_STREAM_THRESHOLD", "256000000"))
    GZ_STREAM_FLOOR_MIN = int(os.environ.get("GZ_STREAM_FLOOR_MIN", "8000000"))
    GZ_STREAM_BUDGET = int(os.environ.get("GZ_STREAM_BUDGET", "50000000000"))
```

2. Pure predicates, below `StreamBudget`:

```python
def gz_stream_candidate(method, clen):
    """Should this gz blob be stream-measured exactly? Big blobs always
    (wrap risk); floored/garbage trailers above a small floor (multi-member
    risk without letting thousands of tiny bgzip files eat the budget)."""
    if GZ_STREAM_BUDGET <= 0:
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
    if GZ_STREAM_BUDGET <= 0 or method == "gz-exact":
        return False
    if clen >= GZ_STREAM_THRESHOLD:
        return True
    floored = method in ("gz-floor", "gz-bad-trailer") or uncomp == clen
    return floored and clen >= GZ_STREAM_FLOOR_MIN


def gz_uncertain_row(kind, method, clen):
    """Rows whose logical size is not reliably measurable: floored/garbage
    trailers, and plausible trailers big enough that a silent >=4GiB wrap is
    possible. gz-exact and small plausible trailers are certain."""
    if kind != "gz":
        return False
    if method in ("gz-floor", "gz-bad-trailer"):
        return True
    return method == "gz-trailer" and clen >= GZ_STREAM_THRESHOLD


def _gz_stream_with_retry(name, attempts=3):
    last = None
    for i in range(attempts):
        try:
            return gz_stream_exact(name)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1 + i)
    raise last
```

3. `size_blob` — new signature and gz branch (zip/stored paths unchanged):

```python
def size_blob(name, clen, etag, kind, matcher, budget=None):
    """Worker-pool job. Never raises — failures become err:* rows, and a
    failed exact-stream falls back to the trailer value (counted uncertain)."""
    uncomp, method, svc, err = clen, "stored", {}, None
    try:
        if kind == "zip":
            uncomp, method, svc = zip_uncompressed(name, clen, matcher)
        elif kind == "gz":
            uncomp, method = gz_uncompressed(name, clen)
            if (budget is not None and gz_stream_candidate(method, clen)
                    and budget.reserve(clen)):
                try:
                    uncomp, method = _gz_stream_with_retry(name), "gz-exact"
                except Exception as exc:  # noqa: BLE001 — keep trailer value
                    logmsg(f"stream fallback {name}: {type(exc).__name__}: "
                           f"{str(exc)[:120]}")
    except Exception as exc:  # noqa: BLE001
        err = type(exc).__name__
        method, uncomp = f"err:{err}", clen
        logmsg(f"ERROR {name}: {err}: {str(exc)[:160]}")
    return {"name": name, "clen": clen, "uncomp": uncomp, "method": method,
            "kind": kind, "etag": etag, "svc": svc, "cached": False, "err": err}
```

4. `enumerate_and_size`: create `budget = StreamBudget(GZ_STREAM_BUDGET)`
   right after the `stop` event; pass it in the submit
   (`size_pool.submit(size_blob, name, clen, etag, kind, matcher, budget)`);
   and in `handle_blob`, after `hit = cache_lookup(...)`, add the staleness
   gate BEFORE using the hit:

```python
        if hit and kind == "gz" and gz_cached_row_stale(hit[3], clen, hit[2]):
            hit = None  # one-time re-measure under the current trigger
```

5. `Aggregator`: init `self.gz_streamed = self.gz_streamed_bytes = 0` and
   `self.gz_uncertain = self.gz_uncertain_bytes = 0`; in `add`, after the
   cache-stats block:

```python
        if r["kind"] == "gz":
            if r["method"] == "gz-exact" and not r["cached"]:
                self.gz_streamed += 1
                self.gz_streamed_bytes += clen
            if gz_uncertain_row(r["kind"], r["method"], clen):
                self.gz_uncertain += 1
                self.gz_uncertain_bytes += clen
```

6. `write_summary`: add to `machine`:

```python
        "gz": {"streamed": agg.gz_streamed,
               "streamed_bytes": agg.gz_streamed_bytes,
               "uncertain": agg.gz_uncertain,
               "uncertain_bytes": agg.gz_uncertain_bytes},
```

   and a human line after the Cache line, only when either is nonzero:

```python
    if agg.gz_streamed or agg.gz_uncertain:
        lines.insert(8, f"gz: {agg.gz_streamed} streamed exact "
                        f"({agg.gz_streamed_bytes/1e9:.2f} GB), "
                        f"{agg.gz_uncertain} uncertain "
                        f"({agg.gz_uncertain_bytes/1e9:.2f} GB compressed)")
```

7. Module docstring: document the three new env vars and the method
   taxonomy in the "Sizing" paragraph (trailer methods + gz-exact).

- [ ] **Step 4: Run to verify pass**

Run: `python3 tests/test_harness.py` — 0 FAIL. Debug transcription vs.
expectation carefully: the fake stream serves 13-byte chunks precisely to
stress member boundaries. Do not weaken assertions.

- [ ] **Step 5: Commit**

```bash
git add scripts/corpus_sizer_rest.py tests/test_harness.py
git commit -m "sizer: gz exact-stream trigger/budget/fallback, cache staleness, gz stats"
```

---

### Task 4: Harness passthrough, reconcile note, docs

**Files:**
- Modify: `scripts/phases.py` (`summary_to_run`, `write_copied_forward_run`)
- Modify: `scripts/reconcile.py` (`lore_notes` gz branch)
- Modify: `CLAUDE.md`, `docs/sizing-internals.md`, `.claude/skills/size-company/SKILL.md`, `.claude/skills/size-company/references/sizing-lore.md`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: summary `"gz"` field (Task 3 shape).
- Produces: run-JSON `"gz"` (verbatim passthrough; `None`-able for old
  summaries); copied-forward carries `prev.get("gz")`; `lore_notes` emits the
  quantified note for new runs with `uncertain > 0`, the legacy qualitative
  note only for runs WITHOUT a `gz` key, and no gz note for new runs with
  `uncertain == 0`.

- [ ] **Step 1: Write the failing tests**

In the `— local sizing end-to-end —` section, extend the fake-sizer `summary`
dict with `"gz": {"streamed": 2, "streamed_bytes": 900, "uncertain": 1,
"uncertain_bytes": 100}` and add after the existing `summary_to_run new
fields` check:

```python
    check("summary_to_run gz passthrough",
          run["gz"] == {"streamed": 2, "streamed_bytes": 900,
                        "uncertain": 1, "uncertain_bytes": 100})
    check("summary_to_run tolerates missing gz", old_run.get("gz") is None)
```

(`old_run` already exists in that section — extend its key-strip list with
`"gz"`.) After the existing copied-forward carry check
(`cf2["detected_services"]...`), add:

```python
        check("copied-forward carries gz", cf2["gz"] == {
            "streamed": 2, "streamed_bytes": 900,
            "uncertain": 1, "uncertain_bytes": 100})
```

In the reconcile section (anywhere after `import reconcile`), add pure
`lore_notes` checks:

```python
    print("\n— reconcile: gz uncertainty notes —")
    base_run = {"totals": {"compressed_bytes": 10, "uncompressed_bytes": 50},
                "sources": {}, "methods": {"gz": 5},
                "errors": {"total": 0, "by_type": {}}}
    old_style = " ".join(reconcile.lore_notes(dict(base_run)))
    check("old run: legacy qualitative gz note", "trailer" in old_style)
    new_certain = dict(base_run, gz={"streamed": 5, "streamed_bytes": 1000,
                                     "uncertain": 0, "uncertain_bytes": 0})
    check("new run, all certain: no gz note",
          "trailer" not in " ".join(reconcile.lore_notes(new_certain))
          and "measur" not in " ".join(reconcile.lore_notes(new_certain)))
    new_unc = dict(base_run, gz={"streamed": 1, "streamed_bytes": 10,
                                 "uncertain": 3,
                                 "uncertain_bytes": 7_500_000_000})
    nn = " ".join(reconcile.lore_notes(new_unc))
    check("new run: quantified note", "3" in nn and "7.5" in nn
          and "measur" in nn)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 tests/test_harness.py`
Expected: FAIL — `summary_to_run gz passthrough` (missing key), then the
lore-note checks (`new run, all certain` fails: legacy note still fires on
`methods.gz > 0`).

- [ ] **Step 3: Implement**

`scripts/phases.py` — in `summary_to_run` after the `"cache"` line:

```python
        "gz": summary.get("gz"),
```

and in `write_copied_forward_run` after its `"cache": None` line:

```python
        "gz": prev.get("gz"),
```

`scripts/reconcile.py` — replace the current gz block in `lore_notes`
(`if run.get("methods", {}).get("gz", 0) > 0: ...`) with:

```python
    gzinfo = run.get("gz")
    if gzinfo is not None:
        if gzinfo.get("uncertain", 0) > 0:
            notes.append(
                f"{gzinfo['uncertain']} gz blob(s) "
                f"({gzinfo['uncertain_bytes'] / 1e9:.1f} GB compressed) could "
                f"not be measured from gzip trailers (>=4 GiB wrap or "
                f"multi-member archives) — the true logical size may exceed "
                f"the measured total.")
    elif run.get("methods", {}).get("gz", 0) > 0:
        notes.append(
            ".tar.gz files are sized from the gzip trailer: exact below 4 GiB, "
            "floored at stored size above — multi-GB tarballs are a small, "
            "known undercount (the price of not streaming them for hours).")
```

Docs (concrete edits):

1. `CLAUDE.md` sizing-run schema block — after the `"cache"` line add:

```json
  "gz": {"streamed": 2, "streamed_bytes": 900000000000,   // exact-streamed gz blobs
         "uncertain": 1, "uncertain_bytes": 5000000000},  // trailer-unmeasurable
                                         // (compressed bytes); null in old runs
```

2. `CLAUDE.md` "Learned the hard way" — replace the `tar.gz is
   trailer-floored` bullet with:

```
- **gz trailers lie two ways:** ISIZE is mod 2³² (≥4 GiB logical wraps —
  sometimes undetectably) and covers only the LAST member of concatenated/
  bgzip files; garbage trailers on misnamed .gz would overcount up to
  4.29 GB each (DEFLATE's 1032:1 bound now rejects them). Large or floored
  gz blobs are stream-measured exactly (GZ_STREAM_THRESHOLD 256 MB /
  GZ_STREAM_FLOOR_MIN 8 MB, GZ_STREAM_BUDGET 50 GB compressed per run,
  0=off), cached forever by ETag; whatever stays unmeasured is quantified
  in the run's `gz.uncertain*` fields, never silent.
```

3. `CLAUDE.md` "Local sizing execution" Cache bullet — append: cached gz
   rows that the current streaming trigger covers but that aren't `gz-exact`
   are re-measured once (a deliberate one-time miss).
4. `docs/sizing-internals.md`: in §1, update the gz line of the "what the
   sizer reads" list (trailer first, exact streaming for large/floored
   blobs); in §5 add the gz staleness rule beside the fingerprint bullet;
   in §8 replace the "tar.gz trailer floor" sharp-edge row with the new
   taxonomy + note that `gz.uncertain` quantifies the residual; in §9 add
   the three env knobs and "warm-run cost of a dump-heavy container =
   listing + budgeted streams, then cached".
5. `.claude/skills/size-company/SKILL.md` — in "Interpreting outcomes"
   under `sized`, add: "Check `gz.uncertain` in the run file — nonzero
   means some gz logical sizes are floors, not measurements; for a
   dump-heavy container raise `GZ_STREAM_BUDGET` (compressed bytes; the
   streams are one-time, cached by ETag) and re-size."
6. `sizing-lore.md` — replace the `.tar.gz is trailer-floored` bullet with:

```markdown
- **gz sizes are tiered:** `gz-exact` (streamed, exact — including
  multi-member/bgzip files whose trailer only covers the last member),
  `gz-trailer` (exact below 4 GiB for single-member files), `gz-floor` /
  `gz-bad-trailer` (floored to compressed size; counted in the run's
  `gz.uncertain*` fields and called out in reports). A misnamed `.gz` can
  no longer overcount: trailers implying >1032× (DEFLATE's hard bound) are
  rejected.
```

- [ ] **Step 4: Run full validation**

Run: `python3 tests/test_harness.py` — 0 FAIL.
Run: `PATH="/opt/homebrew/bin:$PATH" python3 scripts/fleet_size.py launch-all --dry-run --root tests/fixtures/companies --slugs democo` — exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/phases.py scripts/reconcile.py tests/test_harness.py
git commit -m "harness: gz stats passthrough + quantified uncertainty note"
git add CLAUDE.md docs/sizing-internals.md .claude/skills/size-company/SKILL.md .claude/skills/size-company/references/sizing-lore.md
git commit -m "docs: gz accuracy tiers, streaming knobs, uncertainty guidance"
```

---

## Post-plan verification (executor note)

Live check when convenient (needs Azure): re-size healthtap (4,800 gz blobs
today, all small — expect `gz.streamed == 0` or tiny, `uncertain` small, and
identical totals). The real payoff test arrives when their redshift/bigquery
dumps land: those runs should show `gz-exact` methods and a sane
declared-vs-actual %.
