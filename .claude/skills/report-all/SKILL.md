---
name: report-all
description: Use when regenerating every company's report plus the fleet dashboard — "refresh all reports", "rebuild the dashboard", or after a fleet sizing run.
---

# Report on the whole fleet

Loops the report-company primitive, then builds the dashboard:

```bash
for slug in $(ls companies | grep -v '^\.'); do
  python3 scripts/gen_report.py "$slug" || echo "FAILED: $slug"
done
python3 scripts/gen_dashboard.py
git add companies/*/reports reports/index.html
git commit -m "reports: $(date -u +%Y-%m-%d)"
```

- `reports/index.html` is **the user's working view** (progress bar, %, 24h
  delta, ETA, stage, stall/failure badges, link to each latest report);
  per-company reports are the client-shareable ones.
- One company failing to report must not stop the loop — note the failure,
  continue, and include it in your summary (failure isolation).
- `gen_dashboard.py` prints a fleet summary JSON — use it to tell the user
  what changed rather than re-deriving numbers.
- Companies with a `summary failed` row on the dashboard usually have a
  malformed or missing file — inspect `companies/<slug>/` against the schemas
  in CLAUDE.md before touching the scripts.
