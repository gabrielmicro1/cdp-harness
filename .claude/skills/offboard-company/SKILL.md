---
name: offboard-company
description: Use when removing a company from the active fleet while keeping their state for a possible later re-size — "offboard <company>", "archive <company>", "take <company> off the dashboard", "we're done with <company>", or restoring / listing previously offboarded companies.
---

# Offboard a company

Input: a company slug. Output: `companies/<slug>/` moved to
`companies/.archive/<slug>/` — a dot-prefixed dir that `common.list_companies()`
skips, so the company disappears from the dashboard and every fleet loop
(update-all, report-all, daily-brief) while ALL of its state survives intact
for restore: config.json, expected-data-sizes.json, status.json, the
append-only sizing-runs/ history, reports, and the blob-index.tsv.gz cache
that makes a future re-size incremental.

**Offboarding is local bookkeeping only — Azure is never touched.** The
storage account, container, and client data all remain exactly as they are.
If the user wants Azure resources decommissioned, that is a separate
conversation (and not this harness's job); say so explicitly.

## Steps

1. **Offboard** (deterministic — never hand-roll the move):
   ```bash
   python3 scripts/offboard_company.py offboard <slug>
   ```
   Stamps `offboarded_at` into status.json, then moves the dir. Guards:
   - **In-flight sizing run** (`.fleet-state.json` phase `launched`) → the
     script refuses. Poll/harvest the run first so it isn't orphaned;
     `--force` only if the user explicitly wants to abandon it.
   - **Archive collision** (`.archive/<slug>` already exists) → refuse;
     surface it to the user rather than merging or overwriting.
   - `stale_work_files` in the output lists leftover
     `companies/.sizer-work/<slug>-sizer.*` files — transient rescue
     artifacts, safe to delete once offboarded.

2. **Regenerate the dashboard** so the row actually disappears:
   ```bash
   python3 scripts/gen_dashboard.py
   ```
   (Per-company reports live inside the company dir and moved with it —
   no report-all needed.)

3. **Confirm to the user**: where the state went, that Azure is untouched,
   and that `restore <slug>` brings it back whenever a sizing is needed.

## Restore / list

```bash
python3 scripts/offboard_company.py restore <slug>
python3 scripts/offboard_company.py list
```

Restore moves the dir back and clears `offboarded_at`. After a long gap,
suggest refreshing discovery (`python3 scripts/discover_company.py <slug>`) —
the client's Azure layout may have changed — before the next sizing run. The
blob-index cache survives archival, so the first re-size is incremental, not
cold.

## Edge cases

- Both moves are idempotent: re-running reports `already-offboarded` /
  `already-active` at rc 0 — never an error.
- Offboarding a company with no sizing runs yet is fine (nothing special
  to preserve beyond config).
- Nothing here runs git: `companies/*/` (including `.archive/`) is
  gitignored runtime state, same as always.
