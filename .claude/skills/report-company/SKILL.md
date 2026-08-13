---
name: report-company
description: Use when producing or updating one company's progress report — "report on <company>", "make the <company> HTML", "something I can send <client>", or after a sizing run changes their numbers.
---

# Report on one company

```bash
python3 scripts/gen_report.py <slug>
```

Writes `companies/<slug>/reports/<date>.html` — self-contained (inline
CSS/SVG, no CDN), micro1-branded, dark, **clean enough to share externally
with that client**. Commit it: `git add companies/<slug>/reports && git commit
-m "reports: <slug> <date>"`.

All math (declared vs uncompressed, %, delta/ETA from the two most recent
runs, stall ≥3 days, flags) is in `scripts/reconcile.py` — never recompute it
by hand or "adjust" numbers in prose.

## Before sharing with a client, review the generated report for

- **Unconfirmed manifest badge** — if expected sizes were never confirmed at
  onboarding, do not send externally; confirm the manifest first.
- **Timestamp prefixes** (e.g. `20260707T180401Z`) shown as sources — real
  sources are one level deeper; consider whether the client needs the re-split
  before it goes out (see size-company's references/sizing-lore.md).
- **Overshoot (>100%)** — often a wrong manifest, which matters commercially.
  Raise with the user before the client sees it.
- **Zero-declared-has-data / not-in-manifest flags** — these are conversation
  starters with the client, not errors; be ready to explain them.
- A `copied-forward` run date — the report says "unchanged since last full
  sizing"; that's accurate, but tell the user if the numbers are stale by
  more than a few days.

If the report looks wrong (empty sources, absurd ratios), stop and check the
latest sizing run's `errors.by_type` before regenerating — a broken run needs
re-sizing, not re-reporting.
