# Sizing lore — interpretation knowledge from real runs

Grounded in the croplabel / webspiders / latchel runs (2026-07). Shared by
size-company and report-company. `reconcile.py` auto-applies these as report
notes where detectable; this file is for explaining them to the user.

## Reading a sizing run

- **Ratio ≈ 1.0 across everything** = store-mode zips (bundled, not
  compressed — croplabel). The tell: total uncompressed slightly *below*
  compressed. Real, not a bug.
- **High per-source ratios** (latchel slack 11×, hubspot 19×; webspiders code
  ~2×, declared-vs-actual ~8×) = genuinely compressed exports. Also real — the
  zip central directory reports true uncompressed sizes.
- **Top-level prefix may be an export timestamp** (latchel `20260707T180401Z`,
  a Google Takeout run) rather than a source name — real sources are one level
  deeper. Say so when presenting; optionally re-split on the 2nd path segment.
  `sources_l2` in the run file now records the second-level split
  automatically — read it before manually re-splitting.
- **gz sizes are tiered:** `gz-exact` (streamed, exact — including
  multi-member/bgzip files whose trailer only covers the last member),
  `gz-trailer` (exact below 4 GiB for single-member files), `gz-floor` /
  `gz-bad-trailer` (floored to compressed size; counted in the run's
  `gz.uncertain*` fields and called out in reports). A misnamed `.gz` can
  no longer overcount: trailers implying >1032× (DEFLATE's hard bound) are
  rejected.
- **`BadZipFile` errors** = corrupt/mislabeled `.zip` files (common in
  scraped/backup trees); counted at stored size; negligible.
- **Blob COUNT drives runtime, not bytes** (~3–4k blobs/sec): webspiders
  10.9M blobs ≈ 45 min; croplabel 806 ≈ 1 min; latchel 2,289 ≈ 2 min.
- **Embedded services are real bytes:** zip central directories carry entry
  names + exact uncompressed sizes; `detected_services` attributes them
  (deepest path segment wins, so nothing double-counts within a service).
  `found-embedded` on a declared service = the data arrived, wrapped in
  another export.
- **Cache hits are ETag-proof:** a warm run reuses per-blob numbers only when
  name+ETag+size all match — overwritten blobs always re-size. Mass cache
  misses on a warm run = the client re-uploaded, not a bug.

## Reconciling vs declared

- Compare declared vs the **uncompressed** column — the manifest declares the
  client's *logical* data size (store-mode makes it moot; heavy compression
  makes it matter).
- **Skip record-count services** in byte reconciliation (not size-comparable);
  list and flag them instead.
- **Flag services declared 0 B that actually hold data.**
- The manifest's **headline total can exceed the itemized sum** (record
  services + rounding) — headline % uses the manifest total.
- **Overshoot (>100%)** often means a wrong manifest — commercially
  significant; raise it, don't bury it.
- `found-embedded` replaces `declared-empty` when detection locates a
  declared service inside another source; undeclared detections ≥1 GB get a
  note — both are talking points, not alarms.

## Units and presentation

- Decimal GB (÷10⁹). The older canonical `size_corpus.py` used GiB (÷1024³,
  ~7% lower) — account for this when comparing to pre-harness reports.
- Headline totals in TB when ≥1 TB; per-source breakdown in GB.
- Sources spanning orders of magnitude (latchel: 476 MB → 2.6 TB) need a log
  x-axis or the small ones vanish.
