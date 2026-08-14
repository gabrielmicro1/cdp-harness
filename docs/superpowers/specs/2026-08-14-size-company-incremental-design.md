# size-company: incremental sizing, concurrency, and embedded-service detection

**Date:** 2026-08-14
**Status:** Approved design, pre-implementation
**Scope:** `scripts/corpus_sizer_rest.py`, `scripts/phases.py`, `scripts/reconcile.py`,
`.claude/skills/size-company/`, CLAUDE.md schemas, `tests/test_harness.py`

## Problem

1. **Repeat runs redo everything.** The sizer lists every blob and, for every
   `.zip`/`.gz`, pays 1–3 HTTPS range-reads — sequentially. A zip-heavy 20 TB
   container costs hours *every* run, even when almost nothing changed.
   Harvest's cleanup deletes `sizes.tsv`, the one file an incremental run
   could be seeded from.
2. **Service detection is shallow.** Sources are the top-level path prefix
   only. A Google Workspace export can contain other declared services'
   data (e.g. CRM logs inside Drive), which today reads as "declared-empty"
   for that service and inflates the wrapper prefix.
3. **First runs are single-threaded.** Even with caching, a new 20 TB company
   pays millions of sequential round trips on day one.

## Non-goals

- Making the *listing* incremental. Azure blob inventory / change feed would
  require enabling features and writing output on the client's storage
  account — violates the read-only principle (CLAUDE.md #3). Every run
  remains a full listing; that is also what keeps downstream math honest.
- Content peeking inside non-archive blobs (range-reading file headers to
  classify content). YAGNI; revisit only if path+zip-entry detection misses
  real cases.
- Changing the meaning of `sources` (top-level prefix split) — old and new
  runs must stay delta-comparable.

## Design

### 1. Persistent blob index (the cache)

New per-company runtime file: `companies/<slug>/blob-index.tsv.gz`
(gitignored with the rest of `companies/`).

- **Rows:** only method-bearing blobs (zip/gz). Stored blobs need no per-blob
  read — their uncompressed size comes free with the listing.
- **Columns:** `name, etag, compressed_bytes, uncompressed_bytes, method,
  detection_json` (detection_json empty when no service hits).
- **Hit rule:** name AND etag AND compressed size all match → reuse
  uncompressed size, method, and detection. Any mismatch → re-read that blob.
- **Error rows are never cached** (`err:*` methods): transient failures retry
  on the next run instead of freezing in.
- **Rebuild, not merge:** the index is rebuilt from each run's complete TSV,
  so deleted blobs drop out naturally and the index always mirrors the last
  full measurement.
- **Fail-safe:** missing/corrupt index → full re-size. Cache can only ever
  cost time, never correctness.
- **Memory:** loaded as a dict, ~300 B/entry; a pathological million-zip
  container stays under ~1 GB.

### 2. Sizer changes (`corpus_sizer_rest.py`, stays stdlib-only)

- **Listing** captures `Etag` alongside `Name`/`Content-Length`.
- **Prefix-parallel listing:** discover top-level prefixes via a delimiter
  listing (`&delimiter=/`), then list prefixes concurrently (root blobs
  listed separately). Marker pagination is serial *within* a prefix, so a
  container entirely under one prefix gains nothing — documented limitation.
- **Concurrent per-blob reads:** zip/gz cache misses go to a
  `ThreadPoolExecutor` (default 16 workers, `SIZER_WORKERS` env).
- **Cache inputs via env:** `CACHE_FILE` (the company's blob index) and
  optionally `SEED_TSV` (a stale partial `sizes.tsv` from a crashed run —
  it now carries ETags, so it is a safe second seed). `--no-cache` at the
  CLI layer simply omits these.
- **Outputs:** `sizes.tsv` gains `etag` and `detection` columns;
  `<TAG>.index.tsv.gz` is written at completion (the next index);
  `summary.json` gains `cache: {hits, misses}`, `detected_services`,
  `sources_l2`.
- TSV rows are written for cache hits too, so the TSV remains the complete
  per-blob record of the run.

### 3. Service detection (two additive layers)

Matching uses the existing `norm()` convention (casefold, alnum-only) against:
(a) a built-in catalog of common corpus services (slack, gmail, gdrive/drive,
hubspot, salesforce, zendesk, notion, jira, confluence, github, gitlab,
figma, asana, intercom, stripe, quickbooks, dropbox, box, sharepoint,
onedrive, teams, zoom, …), and (b) the company's declared service names,
passed via env (`EXPECTED_SERVICES`, launch reads `expected-data-sizes.json`).

- **Path layer:** blob path segments down to depth 3 are matched; also emits
  `sources_l2`, a second-level breakdown (top 40 by uncompressed bytes plus
  an `(other)` rollup) so timestamp/wrapper prefixes become self-describing.
- **Zip-entry layer:** the central directory already fetched contains every
  entry's filename and exact uncompressed size (currently discarded after
  summing). Entry path segments get the same matching; bytes are attributed
  per entry. Pure CPU — zero extra requests. This catches "CRM logs inside
  the Drive export zip."
- **Run JSON:** `detected_services` maps service → `{bytes, blob_count,
  entry_count, path_bytes, zip_entry_bytes, sources}`, splitting the byte
  total by detection layer (`path_bytes` from path-segment matches,
  `zip_entry_bytes` from zip central-directory entry matches) plus a
  `sources` map of hosting top-level prefix → bytes, rather than a single
  `via` provenance field.
- **Reconciliation (`reconcile.py`):** a declared service with 0 bytes at
  prefix level but present in `detected_services` gets a new
  `found-embedded` flag (with bytes and location) instead of a misleading
  `declared-empty`. Detected catalog services with material bytes that are
  NOT declared are surfaced as a note alongside `unexpected_sources`.
  Detection never feeds the headline % — it is a lens, not a ledger.

### 4. Harvest, cleanup, schema

- Harvest writes the run file with the new fields, then **atomically
  replaces** `companies/<slug>/blob-index.tsv.gz` with the run's
  `<TAG>.index.tsv.gz` (move, not copy), then cleans the work dir as today.
- Launch renames a stale partial `sizes.tsv` to a seed file (instead of
  deleting it) before clearing other stale work files.
- Sizing-run schema additions (CLAUDE.md to be updated):
  `cache: {hits, misses}`, `detected_services`, `sources_l2`. Consumers
  treat missing fields in old run files as unknown, not zero.
- `--no-cache` flag on `size_company.py` / `fleet_size.py` forces a full
  re-size (suspicious numbers, poisoned-cache paranoia).
- Copied-forward path (Phase 0 UsedCapacity skip) is untouched.

### 5. Skill & docs

- `size-company/SKILL.md`: new runtime expectations (first run slow, repeat
  runs ≈ listing time only), `--no-cache` judgment guidance, how to present
  embedded-service findings, note that per-blob detail now survives harvest
  in the blob index.
- `sizing-lore.md`: embedded-services lore; `sources_l2` supersedes the
  manual "re-split on the 2nd segment" advice.
- CLAUDE.md: schema additions, blob-index file in the repo map, updated
  "learned the hard way" notes.

### 6. Error handling

- Cache hit requires etag+size match; overwritten blobs always change ETag —
  no staleness path exists.
- Per-blob read errors: counted as today, floored to stored size, never
  cached.
- A network drop mid-listing is still fatal to the run, but the partial TSV
  seeds the relaunch, so progress is no longer lost.
- Worker-pool exceptions are contained per blob (same contract as today's
  per-blob try/except).

### 7. Testing

- Refactor cache load/save, hit logic, detection matching, and `sources_l2`
  rollup into importable pure functions; unit-test them offline in
  `tests/test_harness.py`.
- Fixture coverage: `found-embedded` reconciliation flag; old run files
  without the new fields (democo fixture unchanged as the "old" shape).
- Live integration on croplabel (small, real) as usual: first run populates
  the index, second run should show `cache.hits > 0` and matching totals.

## Downstream-safety analysis

Every run remains a full listing, so totals, 24 h deltas, ETAs, stall
detection, and copied-forward behave identically. The cache only substitutes
ETag-verified per-blob numbers for re-fetched ones. `sources` keeps its
meaning; all new data is additive fields old consumers ignore.

## Expected impact

- Repeat run on an unchanged/mostly-unchanged container: listing time only
  (minutes, not hours) — per-blob reads drop to the changed set.
- First run on a zip-heavy container: ~10–16× faster from concurrency.
- Declared services hidden inside wrapper exports become visible with real
  byte attribution, replacing false "declared-empty" flags.
