---
name: onboard-company
description: Use when adding a new client company to the harness — "onboard <company>", a new manifest screenshot arrives, or a company needs its config/expected-sizes set up before sizing can start.
---

# Onboard a company

Input: a company slug + an image (screenshot) of the manifest's expected
per-service sizes. Output: `companies/<slug>/` with config.json,
expected-data-sizes.json, status.json (runtime state — gitignored, local only).

## Steps

1. **Discover Azure resources** (deterministic — never hand-roll az here):
   ```bash
   export PATH="/opt/homebrew/bin:$PATH"
   python3 scripts/discover_company.py <slug>
   ```
   This resolves the SA (RG name contains slug), container (`<slug>-raw`;
   `-scrubbed` and `insights-logs-*` ignored), and VM info, and writes
   config.json.
   - The `vm` block is **informational only** — sizing runs locally and needs
     no VM; `vm.exists: false` is normal (most companies) and blocks nothing.
   - A `container_warning` in the output means `<slug>-raw` doesn't exist yet —
     tell the user; the company may not be fully provisioned.

2. **Extract the expected-sizes table from the manifest image.** Read the image
   directly. Convert every size to raw bytes (decimal: 1 GB = 10⁹). Handle:
   - **Record-count declarations** ("1.5M records") → `{"records": N}` —
     excluded from byte reconciliation, flagged in reports.
   - **`0 B` declarations** → `{"bytes": 0}` — kept, so reports can flag data
     appearing in a declared-empty service.
   - **The manifest's headline total** goes in `manifest_total_bytes`
     separately — it can legitimately exceed the itemized sum (record-declared
     services + rounding). Never "fix" that discrepancy.

3. **STOP: echo the parsed table back and wait for explicit confirmation
   before writing.** A mis-OCR'd number poisons every downstream report. Show
   a markdown table: service | declared (as shown in image) | parsed bytes/records,
   plus the headline total. Only after the user confirms, write
   `expected-data-sizes.json` with `confirmed_by_user: true`. If they correct
   anything, re-echo the corrected table and confirm again.

4. **Create the remaining structure** (discover_company.py already made
   `sizing-runs/` and `reports/`): write `status.json` with
   `stage: "pushing"`, `last_run` nulls, `last_change_detected_at: null`.
   Schemas are in CLAUDE.md — follow them exactly. Do NOT git-commit any of
   it: `companies/*/` is gitignored runtime state.

5. **Offer the Slack kickoff** (optional). Ask the user for the company's
   Slack channel link; declining just skips this step. If they provide one,
   invoke the `slack-kickoff` skill with the slug and the link. It registers
   the channel (`scripts/slack_engine.py register`), records the EDP
   Instructions canvas, discovers the workflow-created service threads,
   reconciles them against the declared services you just confirmed, and
   creates one private draft per thread from `knowledge/kickoff-copy/`
   (never a send). Follow that skill's SKILL.md as the source of truth; do
   not restate its steps here.

## Edge cases

- Re-onboarding an existing company: discover_company.py refreshes config
  (keeping `onboarded_at`); do not overwrite a confirmed
  expected-data-sizes.json without asking.
- Manifest image unreadable/ambiguous: ask, don't guess. Ambiguous units
  (GB vs GiB) → assume decimal GB and say so in the confirmation echo.
- No manifest image yet: onboard config-only, leave expected sizes absent,
  and note that reports will show no percentages until the manifest lands.
