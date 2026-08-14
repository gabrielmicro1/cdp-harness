# gz size accuracy: trailer guard, quantified uncertainty, exact streaming

**Date:** 2026-08-14
**Status:** Approved approach (A+B+C), spec for review
**Scope:** `scripts/corpus_sizer_rest.py`, `scripts/phases.py` (schema
passthrough), `scripts/reconcile.py`, docs, `tests/test_harness.py`

## Problem

gz sizing reads the 4-byte ISIZE trailer, which is uncompressed length
**mod 2³²**. Three failure modes:

1. **Wrap undercount**: ≥4 GiB-logical gzips lose whole multiples of
   4.29 GB; the `max(isize, clen)` floor hides only some wraps (a wrapped
   ISIZE that lands above clen looks plausible and is silently wrong).
2. **Multi-member gzips** (concatenated streams, bgzip): trailer describes
   the LAST member only — undercounts even below 4 GiB; bgzip's empty EOF
   member floors the whole file to compressed size.
3. **Garbage trailers overcount**: no gzip validation exists; a misnamed
   `.gz` yields ~2 GB average (up to 4.29 GB) from 4 random bytes. DEFLATE's
   hard bound is 1032:1 — anything above is provably garbage, unchecked today.

Exposure: healthtap has 4,800 gz blobs today (small, plausible), and their
manifest declares ~18 TB of redshift/bigquery/mysql/infrastructure — the
multi-GB gz-dump zone where wrap undercounts would misread %-complete low,
trip stall flags, and prompt wrong nudges.

## Design

### A. Impossible-ratio guard (zero extra requests)

In `gz_uncompressed`: if `isize > clen * 1032` (DEFLATE bound), the trailer
is garbage — return `(clen, "gz-bad-trailer")`. Also split the existing
floor case into its own method for observability: trailer `isize < clen` →
`(clen, "gz-floor")` (wrap / multi-member / incompressible tell);
plausible trailer stays `"gz-trailer"`. `"gz-tiny"` unchanged. All remain
cacheable (a blob's trailer never changes under the same ETag).

### B. Quantified uncertainty (zero extra requests)

The run must say how much logical size is *not measurable from trailers*:

- Aggregator tracks gz rows whose final method is `gz-floor`,
  `gz-bad-trailer`, or `gz-trailer`-with-`clen ≥ GZ_STREAM_THRESHOLD` that
  streaming did NOT resolve (budget exhausted / disabled / stream failed).
- `summary.json` and the run file gain:
  `"gz": {"streamed": n, "streamed_bytes": b, "uncertain": N,
  "uncertain_bytes": X}` (bytes are compressed). Old run files lack the
  key → consumers treat as unknown.
- `reconcile.lore_notes`: when `run["gz"]["uncertain"] > 0`, emit a
  quantified note ("N gz blobs (X GB compressed) could not be measured
  from trailers; true logical size may exceed the measured total").
  Old runs without the field keep the existing qualitative
  `methods.gz > 0` note; new runs with `uncertain == 0` emit neither.

### C. Exact streaming for at-risk gz blobs (bounded, cached)

- **Trigger** (per gz blob, decided in the worker after the trailer read):
  `clen >= GZ_STREAM_THRESHOLD` (env, default 256 MB), OR the trailer
  floored/garbaged (`gz-floor`/`gz-bad-trailer`) with
  `clen >= GZ_STREAM_FLOOR_MIN` (env, default 8 MB — keeps thousands of
  tiny bgzip files from eating the budget).
- **Budget**: `GZ_STREAM_BUDGET` compressed bytes per run (env, default
  50 GB; `0` disables streaming entirely). A thread-safe reserve-then-stream
  counter; candidates that miss the budget keep their trailer/floor value
  and count as uncertain. Budget is *reserved by clen* before the download
  so concurrent workers cannot overshoot.
- **Mechanics**: a streaming HTTP GET (new `http_stream` — chunked read;
  the existing `http_get` buffers whole bodies and must not be used) feeding
  `zlib.decompressobj(wbits=31)`; on decompressor EOF with `unused_data`,
  start the next member (exact multi-member handling). Only output *length*
  is accumulated — constant memory. Network cost = compressed size, once
  ever: the result is cached by ETag as method `"gz-exact"`.
- **Failure**: up to 2 retries of the whole stream; persistent failure falls
  back to the trailer/floor value (method unchanged, counted uncertain) —
  streaming can never make a run fail that would otherwise succeed.
- **Concurrency**: streaming runs on the existing `SIZER_WORKERS` pool; the
  budget bounds total transfer.
- **Cache migration**: `gz-exact` rows are permanent hits. Non-exact cached
  gz rows (`gz-trailer`/`gz-floor`/old runs' rows) that meet the CURRENT
  trigger are treated as misses at lookup time, so raising/lowering the
  threshold or enabling streaming re-measures exactly the affected blobs
  once. All other cache semantics (ETag+size match, fingerprint header,
  det-json validation) unchanged.
- **Read-only principle**: unchanged — streaming is still a GET with the
  same `rl` SAS; it bends only the "no bulk download" heuristic, bounded by
  threshold + budget and amortized to once-per-blob by the cache.

### Docs & knobs

- New env knobs documented in the sizer docstring, CLAUDE.md, and
  `docs/sizing-internals.md` (§"sharp edges" updated: trailer floor is now
  the *fallback*, not the method; History section untouched):
  `GZ_STREAM_THRESHOLD` (256 MB), `GZ_STREAM_FLOOR_MIN` (8 MB),
  `GZ_STREAM_BUDGET` (50 GB, 0=off).
- SKILL.md: interpreting `gz.uncertain` in run files; raising the budget
  for a dump-heavy day; healthtap-shaped warning.
- sizing-lore.md: replace the "tar.gz is trailer-floored" bullet with the
  new tiered reality (exact when streamed, quantified-uncertain otherwise).

## Error handling summary

| Case | Result |
|---|---|
| ISIZE ratio > 1032 | `gz-bad-trailer`, floored to clen, stream candidate |
| ISIZE < clen | `gz-floor`, floored, stream candidate if ≥ 8 MB |
| Stream succeeds | exact bytes, `gz-exact`, cached forever |
| Stream fails after retries | trailer/floor value kept, counted uncertain |
| Budget exhausted | remaining candidates keep trailer value, uncertain |
| Old cached gz rows meeting trigger | miss → re-measured once |

## Testing

Offline (fake container, existing harness): a >1032× garbage-trailer blob →
`gz-bad-trailer` + floored; a multi-member gzip (two real members built with
`gzip.compress` concatenated) small enough to trigger via floor →
streamed → exact sum of both members, method `gz-exact`; budget=0 → same
blob stays floored and counts uncertain; budget reservation respected;
cached `gz-exact` row → hit (no stream on warm run); cached `gz-trailer` row
meeting trigger → miss → streamed. `phases.summary_to_run` passes `gz`
through; old summaries → absent key; reconcile note fires on uncertain>0,
old-style note preserved for old runs. Live: healthtap re-size once dumps
land.
