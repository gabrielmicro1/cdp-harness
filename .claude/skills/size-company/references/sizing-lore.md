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
- **`.tar.gz` is trailer-floored** (fast): exact <4 GiB, floored at compressed
  size above (gzip ISIZE is mod 2³²) — multi-GB DB-backup tarballs read as
  ~stored, a small undercount. The price of not streaming them for hours.
- **`BadZipFile` errors** = corrupt/mislabeled `.zip` files (common in
  scraped/backup trees); counted at stored size; negligible.
- **Blob COUNT drives runtime, not bytes** (~3–4k blobs/sec): webspiders
  10.9M blobs ≈ 45 min; croplabel 806 ≈ 1 min; latchel 2,289 ≈ 2 min.

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

## Units and presentation

- Decimal GB (÷10⁹). The older canonical `size_corpus.py` used GiB (÷1024³,
  ~7% lower) — account for this when comparing to pre-harness reports.
- Headline totals in TB when ≥1 TB; per-source breakdown in GB.
- Sources spanning orders of magnitude (latchel: 476 MB → 2.6 TB) need a log
  x-axis or the small ones vanish.
