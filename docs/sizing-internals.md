# Sizing internals — how the sizer actually works

Audience: an agent (or human) who needs to modify, debug, or reason about the
sizing pipeline without re-deriving it from code. This is the mechanism-level
companion to CLAUDE.md (operational rules) and
`.claude/skills/size-company/SKILL.md` (how to run it). When this document and
the code disagree, the code wins — fix this file.

Accurate as of the deep-verify change (2026-08-23). Key files:

| File | Role |
|---|---|
| `scripts/corpus_sizer_rest.py` | The sizer. Single stdlib-only file, runs as a detached process. |
| `scripts/phases.py` | Harness side: skip-check / launch / poll / harvest / cleanup / status. |
| `scripts/size_company.py`, `scripts/fleet_size.py` | Thin CLIs over `phases.py` (one company = fleet of one). |
| `scripts/deep_verify.py` | Deep-verify step machine: the sizer with `DEEP_VERIFY=1` on an in-region VM (§13). |
| `scripts/reconcile.py` | Turns run files into flags/notes/percentages for reports. |
| `tests/test_harness.py` | Offline verification incl. a fake in-memory Azure container. |

---

## 1. The big picture

Sizing answers: *how much data has this company actually pushed into
`<slug>-raw`, per source, compressed and uncompressed?* The answer must be
reproducible (it feeds commercial conversations) and must never write to the
client's storage account (read-only `rl` SAS, 1-day expiry).

The trick that makes it cheap: **nothing is ever downloaded in bulk.** The
sizer reads only:

- **blob-list pages** (5,000 blobs each) — gives name, `Content-Length`, ETag
  for free;
- for `.zip`: the End-of-Central-Directory record + the Central Directory,
  via 2–3 HTTP Range requests — the CD contains every entry's exact
  uncompressed size (and filename, which detection uses);
- for `.gz`/`.tgz`/`.tar.gz`: the last 4 bytes (the gzip ISIZE trailer) first;
  large or floored/implausible blobs get an exact streaming decompress
  instead, under a per-run byte budget (§8, §9);
- for `.parquet`: a 64 KB tail Range GET — the last 8 bytes give the footer
  length + `PAR1` magic, and the footer (a thrift-compact `FileMetaData`,
  parsed by a stdlib decoder) almost always fits in the same tail; sum of
  the column chunks' `total_uncompressed_size` (fallback: row groups'
  `total_byte_size`). This is decompressed PAGE bytes — codecs undone,
  dictionary/RLE encodings intact — the parquet analog of "uncompressed",
  which understates a warehouse console's logical size (BigQuery prices
  decoded widths). Same trust class as a zip CD, in BOTH modes: the stdlib
  has no snappy codec, so deep verify cannot stream the pages, every
  `parquet-*` method is terminal, and coverage classes them "trusted"
  forever (`parquet-tiny` excepted). Methods: `parquet-footer`, and floored
  fallbacks `parquet-tiny`/`parquet-encrypted`/`parquet-bad-magic`/
  `parquet-bad-footer`;
- everything else ("stored"): nothing — uncompressed = `Content-Length`.

So cost scales with **blob count** (HTTP round trips), not bytes. The two big
levers on top of that are the **cache** (skip repeat zip/gz reads for
unchanged blobs, §5) and **concurrency** (§4).

An invariant worth internalizing: **every run is a complete listing of the
container.** The cache never skips listing; it only skips per-blob range
reads. That is why totals, deltas, ETAs, and stall detection stay honest, and
why deleted blobs age out of everything automatically.

---

## 2. Lifecycle: the four phases

`phases.py` owns these; both CLIs are loops over them. State between CLI
invocations lives in `companies/.fleet-state.json`.

### Phase 0 — skip check (`skip_check`)

Reads the storage account's `UsedCapacity` metric (ARM read, ~free, interval
PT1H, offset 4h). If the latest datapoint equals `used_capacity_bytes` from
the previous run file → write a `method: "copied-forward"` run that copies the
previous run's numbers (including `detected_services` and `sources_l2`, with
`cache: null`) and skip the launch entirely. Caveats: the metric is
account-level (a `-scrubbed` container write forces one redundant re-size —
harmless) and lags up to ~1h (a same-hour push is caught next morning). No
prior run, or no metric → always launch (safe direction).

### Phase 1 — launch (`launch`)

1. **Firewall** (`ip_rule_ensure`): if the SA is `defaultAction: Deny` and our
   public IP isn't allowlisted, add an IP rule, sleep 60s for propagation, and
   remember `we_added_ip` so cleanup removes only OUR rule. Never touch
   pre-existing rules — they're the client's push path.
2. **SAS** (`mint_sas`): account SAS, `rl` only, 1-day expiry, https-only.
   Passed via process env only; never written to disk or logs.
3. **Seed handling**: if a stale `<tag>.sizes.tsv` exists (a crashed run's
   partial progress) and `use_cache`, it is renamed to `<tag>.seed.tsv`; all
   other stale `<tag>.*` work files are deleted (a stale `.done` would fake
   completion).
4. **Env contract** — the entire harness→sizer interface is environment
   variables (the launch code first *pops* `CACHE_FILE`/`SEED_TSV`/
   `EXPECTED_SERVICES` so nothing leaks from the harness's own process):

   | Var | Set when | Value |
   |---|---|---|
   | `SA`, `CONTAINER`, `TAG`, `OUT_DIR` | always | account, container, `<slug>-sizer`, `companies/.sizer-work/` |
   | `AZURE_STORAGE_SAS` | always | the minted SAS |
   | `CACHE_FILE` | `use_cache` and `companies/<slug>/blob-index.tsv.gz` exists | that path |
   | `SEED_TSV` | `use_cache` and a seed was renamed into place | `<tag>.seed.tsv` path |
   | `EXPECTED_SERVICES` | company has declared services | comma-joined keys of `expected-data-sizes.json` `services` |

   `--no-cache` on either CLI simply means `use_cache=False`: no `CACHE_FILE`,
   no seed rename (the stale TSV is deleted with the rest).
5. **Detach**: `subprocess.Popen` with `start_new_session=True`, stdin closed,
   stdout→`<tag>.stdout`, wrapped in `caffeinate -i` on macOS (blocks idle
   sleep, NOT lid-close). The sizer outlives the harness/agent by design; its
   work files are the manual-rescue path.

Dry-run returns before any file operation.

### Phase 2 — poll (`poll_one`)

Purely local, instant: `<tag>.done` exists → `Succeeded`; else tracked pid
alive → `Running`; else `Failed` with the stdout tail as reason. Gotcha: the
tracked pid may be `caffeinate`'s, so "Failed but `.done` exists" means the
run actually finished — just harvest.

### Phase 3 — harvest (`harvest_one` → `cleanup`)

1. Read `<tag>.summary.json`; map it to a sizing-run file via
   `summary_to_run` (see §7 for the field mapping) and write it to
   `companies/<slug>/sizing-runs/<ts>.json` (append-only; filename bumps by a
   second on collision).
2. **Move** `<tag>.index.tsv.gz` → `companies/<slug>/blob-index.tsv.gz`
   (`os.replace`, same filesystem). This is how per-blob detail survives
   harvest and becomes the next run's cache.
3. `cleanup`: delete all remaining `<tag>.*` work files (including the seed
   and `sizes.tsv` — only *stored*-blob rows are truly lost; zip/gz rows live
   on in the index) and remove the IP rule iff we added it. The SAS just
   expires.

`update_status` then records the outcome and applies the only automated stage
transitions (`pushing↔stalled`); it never touches human-set stages.

---

## 3. Inside the sizer: setup

`corpus_sizer_rest.py` is import-safe: importing it needs no env vars (all
config globals default to placeholders); `main()` calls `_init_from_env()`
first, which reads the env into module globals. Tests exploit this by
importing the module, setting env, calling `_init_from_env()`, and
monkeypatching `http_get`.

`main()` order matters:

```
_init_from_env()
matcher = build_matcher(EXPECTED_SERVICES)     # §6
fp      = matcher_fingerprint(matcher)         # §5
cache   = load_cache(CACHE_FILE, fp)           # prior harvest's index
cache.update(load_seed_tsv(SEED_TSV, fp))      # crashed-run partial TSV wins on conflict
agg     = enumerate_and_size(cache, matcher)   # §4 — raises on fatal
write_summary(agg, dur)                        # .summary + .summary.json
write_index(INDEX, agg.index_rows, fp)         # next run's cache
touch .done                                    # ONLY on full success
```

`.done` is the success contract: it is written only after the summary and
index exist, and any fatal error (listing failure, consumer failure) raises
before reaching it. Poll and harvest key off this.

`http_get` retries any exception 4× with linear backoff (1,2,3,4s), 90s
timeout per attempt, then re-raises. The SAS is appended to every URL by
`_auth`.

---

## 4. Inside the sizer: the concurrent pipeline

`enumerate_and_size(cache, matcher)` is the heart. Thread roles:

```
main thread ──► discover_prefixes()          delimiter listing (&delimiter=/):
                                             top-level prefixes + root blobs;
                                             prefixes deduped across pages
      │  handle_blob() for each root blob
      ▼
lister pool (≤ LIST_WORKERS=8, one prefix each)
      │  pages through &prefix=<p>&marker=…, calls handle_blob() per blob
      ▼
handle_blob(name, clen, etag):
      stored blob ────────────────► results queue          (no HTTP, no pool)
      zip/gz + cache hit ─────────► results queue          (no HTTP, no pool)
      zip/gz miss ── semaphore ──► size pool (SIZER_WORKERS=16)
                                     size_blob(): range reads; NEVER raises —
                                     failures become err:* rows
                                     done-callback: release semaphore,
                                     then put row on results queue
      ▼
results queue (maxsize 10 000 — backpressure)
      ▼
consumer thread (exactly one): agg.add(row) for every row   ── Aggregator
```

Concurrency invariants — preserve these when editing:

- **All aggregate mutation happens in the consumer thread.** The `Aggregator`
  (per-prefix totals, L2 totals, detection, methods, errors, cache stats,
  index rows, the TSV file) needs no locks *because* only `consume()` calls
  `add()`. Don't add another caller.
- **Backpressure is two-layered**: the bounded queue (10k rows) blocks
  producers when the consumer lags; the `BoundedSemaphore(SIZER_WORKERS*4)`
  caps in-flight submissions so the futures backlog can't grow unboundedly.
  The done-callback releases the semaphore *before* the potentially-blocking
  queue put — swapping those lines deadlocks under load.
- **Termination protocol**: listers finish (or `stop` is set) →
  `size_pool.shutdown(wait=True)` (all callbacks have enqueued their rows) →
  `results.put(None)` sentinel → `consumer.join()` → `agg.close()`. The
  sentinel is provably the last item.
- **Failure taxonomy** (this is the part that protects the numbers):
  - *Per-blob* failure (corrupt zip, timeout after retries): caught inside
    `size_blob`, becomes an `err:<ExcName>` row — counted, floored to stored
    size, **never cached**. The run continues.
  - *Listing* failure (network drop, 403): fatal. `stop` halts other listers,
    workers drain, and the exception re-raises → no `.done`. The partial
    `sizes.tsv` (with ETags) becomes the next launch's seed, so progress
    isn't lost.
  - *Consumer* failure (anything `agg.add` throws — e.g. disk full writing
    the TSV): caught, recorded, `stop.set()` (fail fast — don't pay for a
    doomed listing), but the loop **keeps draining to the sentinel** so
    producers never wedge on a full queue; the exception re-raises after
    join → no `.done`. Truncated numbers can never masquerade as success.
- `logmsg` takes `_LOG_LOCK` — it's called from workers, listers, and the
  consumer.

Listing notes: `discover_prefixes` uses `&delimiter=/` so Azure returns
`BlobPrefix` elements (with trailing `/`) plus root-level blobs; each prefix
is then marker-paged independently by a lister. Consequences: a container
that's entirely under one prefix gains nothing from prefix parallelism
(pagination is serial within a prefix), and a wide-flat container (thousands
of tiny top-level prefixes) pays one extra list request per prefix vs. the
old single marker walk.

---

## 5. The cache (incremental sizing)

Purpose: a repeat run should pay for the listing plus only the *changed*
zip/gz blobs. Stored blobs are never cached — their size is free with the
listing. Error rows are never cached — transient failures retry next run.

**Persistent form** — `companies/<slug>/blob-index.tsv.gz`, written fresh by
every run (`write_index`) and moved into place by harvest:

```
#matcher\t<12-hex fingerprint>
<name>\t<etag>\t<compressed>\t<uncompressed>\t<method>\t<det_json>
...
```

Rows are rebuilt from the run's complete results (cache hits are re-emitted),
so the index always mirrors the last full measurement and deleted blobs drop
out — rebuild, not merge.

**Seed form** — a crashed run's partial `sizes.tsv` (renamed to
`<tag>.seed.tsv` by launch). Same idea, TSV columns
`name, clen, uncomp, ratio, method, etag, det_json` with the same `#matcher`
header. Loaded *after* the index (`dict.update`), so fresher partial results
win.

**Hit rule** (`cache_lookup`): name AND etag AND compressed size must all
match, and the etag must be non-empty. Azure changes a blob's ETag on any
overwrite, so there is no staleness path for sizes. On a hit, the stored
uncompressed size, method string, and zip-entry detection are replayed with
zero HTTP.

**Fail-safe layers** — the governing rule is *a cache may only ever cost
time, never correctness*:

1. Missing/unreadable/corrupt-gzip file → `load_cache` returns `{}` → full
   re-size.
2. Row-level: short rows, `err:*` methods, empty etags, non-zip/gz names (in
   the seed) are skipped.
3. `_det_valid`: a det_json that isn't a JSON object of
   `str → [int, int]` (bools rejected) makes that ROW a miss at load time.
   Without this, a poisoned row would crash the consumer, fail the run, and —
   since the same `CACHE_FILE` is passed on every relaunch — fail every
   subsequent run until someone deletes the file. (The consumer guard in §4
   remains the backstop for everything else.)
4. **Matcher fingerprint**: `matcher_fingerprint` hashes the *entire built
   matcher* (sorted alias→canonical pairs; sha1, 12 hex chars). `load_cache`/
   `load_seed_tsv` demand the file's `#matcher` header equal the current
   fingerprint; missing or mismatched header → the whole file is a miss.
   This exists because cached det_json was computed with the *previous* run's
   matcher — without invalidation, changing a company's declared services
   would leave unchanged zips carrying stale detection forever. Two
   consequences to know: editing a company's `expected-data-sizes.json`
   services auto-triggers a full re-size for that company (no `--no-cache`
   needed), and editing `SERVICE_CATALOG` in the sizer invalidates EVERY
   company's cache at once — the first fleet run after such a harness upgrade
   does full re-sizes fleet-wide. Both intentional; the second is worth
   remembering when a morning run is mysteriously slow.
5. **Staleness** (`cached_row_stale`, mode-aware): exact/terminal methods
   (`*-exact`, `*-truncated`, `zip-exact-mismatch(*)`, `*-tiny`,
   `zip:0entries` — see `_method_terminal`) are NEVER stale in any mode:
   this is what lets a shallow daily run replay deep-verify measurements at
   zero HTTP without re-shallowing them. Shallow mode otherwise applies only
   the gz rule (`gz_cached_row_stale`): a cached gz row that is not
   `gz-exact` but that the *current* streaming trigger would now stream
   (threshold/floor-min in effect this run) is re-measured once — a
   deliberate one-time miss, not a bug. Deep mode treats every non-terminal
   cached row as stale (one re-measure converts trust into measurement; a
   repeat deep run on an unchanged container is then listing-only, except
   non-terminal residuals like a garbage `.xz`, deliberately re-attempted). Pre-taxonomy cache rows report
   `gz-trailer` even when floored; `uncompressed == compressed` is the tell
   that catches those too. Disabled entirely when streaming is off
   (`GZ_STREAM_BUDGET <= 0`) — re-reading the trailer every run would gain
   nothing.

Cache stats: `hits` = zip/gz served from cache; `misses` = zip/gz that needed
a fetch (error rows count as misses); stored blobs count as neither. Mass
misses on a warm run with an unchanged matcher = the client re-uploaded
(overwrote) blobs — itself worth reporting.

Memory: the cache dict and `index_rows` each cost ~300 B per zip/gz blob —
fine up to ~1M archives, would need streaming beyond that.

---

## 6. Service detection (the "lens")

Goal: attribute bytes to *services* even when they hide inside wrapper
exports (CRM logs inside a Google Workspace/Takeout archive). Cardinal rule,
stated in three docs and enforced in code: **detection is a lens, not a
ledger** — it never feeds `sources`, totals, or the headline %.

**The matcher** (`build_matcher`): normalized-alias → canonical-name dict,
built from `SERVICE_CATALOG` (~23 services; aliases deliberately specific —
generic English words like "mail"/"code"/"box"/"linear" are excluded because
the zip-entry layer token-matches every entry filename and false positives
land in client-facing report notes) plus the company's declared service
names. Declared names map to the *manifest's own spelling* and override
catalog aliases, so downstream `reconcile.norm()` matching lines up exactly.

**Matching** (`match_segment` / `match_path`): a segment matches if its
normalized form (lowercase, alnum-only) is an alias, or any `[a-z0-9]+` token
of it is (so `slack-export-2026.zip` → slack). A path's candidates are its
first 3 segments plus the filename (only when deeper than 3); the **deepest
match wins**, so each path attributes to exactly one service and per-service
byte totals never double-count. `gdrive/hubspot/x.csv` is hubspot, not both.

**Two additive layers**, aggregated per blob in `Aggregator.add`:

- *Path layer*: if the blob's own path matches service S, the blob's full
  uncompressed size goes to S (`path_bytes`, `blob_count`).
- *Zip-entry layer*: `_parse_cd` decodes every CD entry filename (utf-8 first,
  cp437 `errors="replace"` fallback — decoding can never fail a blob) and
  matches it; matched entries contribute their exact per-entry usize
  (`zip_entry_bytes`, `entry_count`). Entries matching the blob's own
  path-service are skipped — those bytes are already counted. Zero extra
  HTTP: the CD was fetched for sizing anyway. Per-blob results are the
  `det_json` that gets cached, which is why the fingerprint (§5) exists.

Each service also records `sources`: hosting top-level prefix → bytes, which
is how reports can say *where* embedded data lives.

**`sources_l2`** rides along: a `top/second`-level breakdown (`(files)` for
blobs directly under a top prefix, `(root)` for root blobs), capped at the
top 40 by uncompressed bytes with an `(other)` rollup. It makes
timestamp/wrapper prefixes self-describing without the old manual re-split.

---

## 7. Data contracts and downstream consumers

`summary.json` (sizer) → sizing-run file (harvest's `summary_to_run`):

| summary.json | run file | Notes |
|---|---|---|
| `blobs, comp, unc, zero` | `totals.{blob_count, compressed_bytes, uncompressed_bytes, zero_byte_blobs}` | |
| `src` (name → `[files, comp, unc]`) | `sources.<name>.{blob_count, compressed_bytes, uncompressed_bytes}` | top-level prefixes; `(root)` for none |
| `methods` | `methods` | counts by KIND (`zip`/`gz`/`stored`), not method string |
| `errors, err_types` | `errors.{total, by_type}` | |
| `cache` | `cache` | `null` for copied-forward and pre-cache runs |
| `detected_services` | `detected_services` | `{}` default; shape in §6 |
| `sources_l2` | `sources_l2` | `{}` default |
| `dur_s` | `duration_seconds` | |

Old run files simply lack the last three — every consumer treats absence as
unknown, never zero. Old and new runs stay delta-comparable because `sources`
and `totals` never changed meaning.

Consumers (all via `reconcile.company_summary` — nobody re-derives math):

- **`reconcile.service_rows`**: matches declared services to top-level
  `sources` by `norm()`. New behavior: a declared byte-service with 0 actual
  at prefix level checks `detected_services` first — found there ⇒ flag
  `found-embedded` (with `embedded_bytes` and `embedded_in`, hosts sorted by
  bytes) **instead of** `declared-empty`. Known blind spot (deliberate): a
  service declared `0 B` gets the `zero-declared-has-data` treatment only at
  prefix level, never the embedded lens.
- **`reconcile.detection_notes`**: prose notes for found-embedded rows, plus
  "detected but undeclared" notes gated at ≥1 GB (`UNDECLARED_NOTE_FLOOR`) —
  the floor keeps token-match noise out of client-facing reports.
- **`gen_report.py`**: renders the flag as badge "embedded in another source"
  (info); notes render verbatim into client-shareable HTML — which is exactly
  why the catalog is conservative about generic aliases.
- **`verify_completion.py`**: unaware of detection; a found-embedded service
  still counts against completion (its bytes aren't at prefix level). That is
  intentional — verification asks "did the declared layout arrive".
- **Headline % / ETA / stall**: computed from `totals.uncompressed_bytes` vs
  the manifest headline; completely detection-blind.

---

## 8. Failure modes, quick reference

| Event | Behavior |
|---|---|
| Corrupt/mislabeled zip (`BadZipFile` etc.) | `err:*` row: counted, floored to stored size, never cached; run continues |
| Network blip on one blob | 4 retries w/ backoff inside `http_get`; then err row as above |
| Network drop during listing | Fatal: no `.done`, run `Failed`; partial TSV seeds the relaunch |
| 403 AuthorizationFailure in sizer log | Firewall (IP rule not propagated), NOT the SAS — wait ~60s, relaunch; never re-mint |
| Laptop lid closed mid-run | Process suspended (caffeinate doesn't cover lid) → poll shows Failed w/ pid gone after wake, or still-running; relaunch is idempotent |
| Consumer/aggregation error (e.g. ENOSPC) | Fatal, fail-fast (`stop`), no `.done`; never silent truncation |
| Poisoned cache det_json | Row-level miss at load (`_det_valid`); run succeeds, blob re-read |
| Declared services edited | Fingerprint mismatch → whole cache miss → full re-size (intended) |
| `SERVICE_CATALOG` edited | Same, but fleet-wide on next run (intended; explains a slow morning) |
| Blob overwritten by client | ETag changed → miss → re-read (and mass misses signal re-upload) |
| Scrub-side write to the account | UsedCapacity moves → one redundant re-size (account-level metric) |
| Harness/agent dies mid-run | Sizer keeps running (detached); state file + work files allow resume; see CLAUDE.md "Manual rescue" |
| `.done` present but state says Failed | Tracked pid was probably caffeinate's; just harvest |

Sharp edges that are *by design* (don't "fix" without understanding):

- **gz trailer taxonomy**: `gz-trailer` (plausible ISIZE, exact below 4 GiB),
  `gz-floor` (ISIZE < compressed size — a ≥4 GiB wrap or a multi-member/bgzip
  file whose trailer only covers the last member), `gz-bad-trailer` (ISIZE
  implies >1032:1, DEFLATE's hard bound — garbage, floored to compressed
  size, no more silent overcounts), `gz-tiny` (compressed size < 4 bytes —
  no trailer to read, counted at stored size). Large or floored/bad blobs
  get an exact streaming decompress under a per-run byte budget and become
  `gz-exact`;
  transport failures retry (re-reserving budget), decode failures are
  terminal and fall back to the floored value — except a truncated upload
  (`ValueError` mid-stream), which becomes `gz-truncated` at the exact byte
  count decompressed before the cut: that is the true logical size of the
  content that exists, and unlike a garbage trailer it can't overcount
  (seen on bacancy: a duplicity volume claiming 4.29 GB via trailer that
  actually holds ~66 MB). `gz-truncated` is treated as terminal like
  `gz-exact` — re-streaming can't improve it until the ETag changes.
  Whatever stays unmeasured after the budget runs out is quantified, never
  silent, in the run's `gz.uncertain` / `gz.uncertain_bytes` fields.
- **Zips are non-recursive**: a zip inside a zip contributes its compressed
  size via the outer CD; nested contents are not expanded.
- **`MAX_ZIP_ENTRIES` cap** (5M): a monster CD parses partially
  (`zip:partialN/M` method) rather than stalling.
- **`zip-no-eocd` / `zip-loc64-*` methods**: unparseable zip structures fall
  back to compressed size, not errors. Before flooring, three recoveries are
  tried: a saturated entry count (0xFFFF with real 32-bit CD offsets and NO
  zip64 records — Takeout writes this past 65,535 entries) parses the CD by
  walking it to the end; a corrupt/absent zip64 locator falls back to
  scanning the tail for the EOCD64 record's own signature; a missing EOCD
  retries once with an 8 MB tail (`ZIP_TAIL_RETRY`) in case of trailing
  junk. What still floors after all three is genuinely truncated or not a
  zip at all (misnamed AppleDouble/PDF/RAR files are common in Drive
  exports). `zip-tiny` (< 22 bytes, incl. zero-byte placeholders) is counted
  at stored size with no range read — a 0-byte blob used to surface as a
  spurious HTTPError 400.

---

## 9. Performance model and knobs

Runtime ≈ listing time + (zip/gz misses × round-trip cost ÷ SIZER_WORKERS).

- Listing: 5,000 blobs/page, serial per prefix, parallel across prefixes
  (`LIST_WORKERS`, default 8). 10.9M blobs ≈ 45 min in-region historically;
  local/internet is comparable for listing.
- Zip miss: 2–3 range GETs (tail; +ZIP64 EOCD if needed; CD). Gz miss: 1.
  Over the internet each is a full round trip — this is what made zip-heavy
  containers take hours pre-cache, and what `SIZER_WORKERS` (default 16) and
  the cache attack from both sides.
- Warm run on unchanged container: listing time only; the offline test
  asserts literally zero range GETs.
- Warm-run cost of a dump-heavy container = listing + budgeted streams, then
  cached: the first run after a gz-heavy push pays for `gz-exact` streaming
  up to `GZ_STREAM_BUDGET`; every subsequent run replays those rows from
  cache at zero HTTP (until an ETag changes or the staleness rule in §5
  re-triggers one).
- Knobs (env): `SIZER_WORKERS`, `LIST_WORKERS`, `MAX_ZIP_ENTRIES`,
  `GZ_STREAM_THRESHOLD` (compressed-byte size at/above which a gz blob is
  always exact-streamed; default 256 MB), `GZ_STREAM_FLOOR_MIN` (minimum
  compressed size before a floored/bad-trailer blob is worth streaming;
  default 8 MB), `GZ_STREAM_BUDGET` (per-run cap on compressed bytes spent
  exact-streaming gz; default 50 GB; `0` disables streaming entirely — every
  floored/bad gz stays uncertain). Flags: `--no-cache` (full re-size). None
  normally need touching; raise `GZ_STREAM_BUDGET` for a dump-heavy container
  whose run shows a large `gz.uncertain_bytes`.

---

## 10. History: the retired VM path

Sizing originally ran on in-region VMs via `az vm run-command` (the
`SIZING-SKILL.md` runbook, retired 2026-08 and preserved in git history along
with the VM implementation — commit `c3ba27c` and earlier). A VM sizing path
EXISTS again since deep verify (§13) — but it is built on the transfer
engines' ssh+tmux lifecycle, NOT on run-command; if a container is ever too
slow to size shallow over the internet, extend `deep_verify.py` rather than
resurrecting run-command. For the record, run-command's sharp edges were: run-command allows ONE invocation at a time per VM (a
VM-side sleep loop jams the slot — poll with instant scripts, wait locally);
its instance-view output is capped at 4KB; the script and SAS travel
base64-piped through the command; and an in-region VM cannot be allowed by
an IP rule — it needs the `Microsoft.Storage` service endpoint on its subnet
plus a vnet-rule on the storage account. ~40% of companies have no VM at all,
so discovery (`config.json`'s `vm` block) never assumes one.

## 11. Verifying and debugging

- **Offline suite**: `python3 tests/test_harness.py` (161 checks, no Azure).
  The sizer sections monkeypatch `corpus_sizer_rest.http_get` with a fake
  in-memory container (real zips built via `zipfile`) and drive real
  `main()` end-to-end: cold run, warm run (asserts hits=all, zero range
  GETs), fingerprint-staleness run, poisoned-cache run, consumer-guard
  fault injection, prefix-dedupe. When changing the sizer, extend these —
  they are the only fast feedback loop.
- **Live smoke test**: size croplabel (806 blobs ≈ 1 min) twice; the second
  run's file should show `cache.hits > 0` and identical totals.
- **A stuck run**: see CLAUDE.md "Manual rescue" — `<tag>.log` tail,
  `wc -l` on `sizes.tsv` (minus 1 for the `#matcher` header),
  `pgrep -f corpus_sizer_rest.py`, and the `.done`-exists-but-Failed case.
- **Suspicious numbers**: check `errors.by_type` and the log tail first
  (URLError bursts = connection wobble → sizes floored to stored → re-size);
  `--no-cache` for cache paranoia; delete `blob-index.tsv.gz` any time — the
  only cost is one full re-size.

## 12. Invariants checklist for future modifications

1. Sizer stays single-file, stdlib-only, import-safe (env read only in
   `_init_from_env`).
2. Read-only against client storage: `rl` SAS, no writes, ever.
3. Every run lists everything; the cache only short-circuits per-blob reads.
4. `.done` only after a fully-successful run; all fatal paths raise first.
5. Only the consumer thread mutates the `Aggregator`; semaphore released
   before queue put in the done-callback.
6. Cache fail-safety: any doubt (shape, header, fingerprint, etag) = miss.
7. Detection never feeds `sources`/totals/headline %; deepest-wins keeps
   per-service attribution disjoint; catalog aliases stay non-generic.
8. Run-file fields are additive; absence in old files = unknown, not zero.
9. `sizing-runs/` is append-only; bytes are ints; presentation is decimal
   (÷10⁹) at the edges only.
10. Fleet operations isolate per-company failures and report every outcome.

## 13. Deep verify (DEEP_VERIFY=1 on an in-region VM)

Deep verify converts the sizer's metadata-trusted numbers into measurements:
`DEEP_VERIFY=1` makes `size_blob` stream-decompress every compressed blob —
the totals then come from counted output bytes, not zip central directories
or gz trailers. It exists because the numbers feed data-sale contracts. Run
via `scripts/deep_verify.py` (a step machine over `transfer_engine.py`'s VM
lifecycle — create `deepv-<slug>` → allow-network → push sizer + cache +
`rl` SAS env → tmux → poll → harvest + auto-teardown; no state file, VM
tags carry the pre-run UsedCapacity). Daily shallow runs are untouched.

**Kinds.** `blob_kind` knows `bz2` (`.bz2/.tbz2/.tar.bz2`) and `xz`
(`.xz/.txz/.tar.xz`) in BOTH modes (so deep cache rows replay on shallow
runs); shallow sizes them at content-length with zero HTTP (`bz2-stored` /
`xz-stored` rows — cached, but counted as neither hit nor miss so the
"mass misses = re-upload" signal stays honest). Formats with no stdlib
codec (`UNMEASURABLE_EXTS`: .7z/.rar/.zst/.tzst) stay kind `stored` and are
bucketed per-format in the verification stats.

**Deep gz** is the existing streaming machinery with forced knobs
(`_init_from_env`: threshold 0, floor-min 0, budget 2^63 — `StreamBudget`
survives purely as the egress ledger). **Deep zip**: `_parse_cd` collects a
per-entry map (`entries_out`: lho/flags/method/csize/usize, zip64-resolved)
and `zip_stream_exact` walks the local entries in ONE sequential GET
(`_ByteCursor` gap-skips headers/descriptors), inflating deflate entries
(raw `wbits=-15`) and counting stored entries' csize; encrypted/unsupported/
corrupt entries fall back to their CD usize and count as cd-trusted
(`zip-partial(k/Ncd)`). A clean walk is `zip-exact`, or
`zip-exact-mismatch(cd=N)` when the CD lied — the STREAMED value goes into
totals (silent at run level by policy; quantified in
`verification.cd_mismatches`). **Deep bz2/xz**: `_stream_exact_multi`
(shared bz2/lzma decompressor API: `needs_input`/`unused_data`, xz stream
padding stripped between streams); truncation → `{kind}-truncated` at the
exact partial, decode failure → `{kind}-stored` fallback (trusted bucket).
All streams route through `_stream_with_retry`: transport errors retry with
budget re-reservation; decode errors (`StreamDecodeError`, `TruncatedStream`
— both ValueError) are terminal.

**Coverage** (`coverage_class`, applied per row in the consumer so cached
replays classify identically): measured (streams, truncated partials,
trivial blobs, loose stored files) / trusted (CDs, trailers, `*-stored`
placeholders, `err:*` floors) / unmeasurable (no-codec formats,
per-extension). Emitted as summary.json's `verification` block only when
deep (shallow summaries are byte-identical to before); `phases.summary_to_run`
passes it through, copied-forward carries it, `reconcile.company_summary`
exposes `verification` + `deep_verified_at`, reports badge it, and
`verify_completion` shows an informational never-gating check.
`stream_compressed_bytes` counts successful measurement egress per run
(failed attempts and cache replays excluded), so warm deep runs report ~0.

**Tests**: `tests/test_harness.py` "deep verify" sections — the
shallow→deep→warm→shallow cycle (staleness both ways, CD-tampering, bz2/xz
fixtures), consumer rendering, and the deep_verify.py dry-run + fake-ssh
harvest.
