---
name: update-all
description: Use when refreshing sizing for the whole fleet — "update all companies", "size everyone", "fleet sizing run", or as the first step of the morning routine.
---

# Update the whole fleet

Thin orchestrator over `scripts/fleet_size.py`, which runs **fleet-wide
phases, not per-company loops**: skip-check everyone (parallel ARM metric
reads), launch all non-skipped as detached local sizer processes (they run
concurrently — total wall time ≈ slowest company, not the sum), poll their
`.done` files, then harvest. Never reimplement per-company logic here — that
lives in `scripts/phases.py` shared with size-company.

## Run it

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/fleet_size.py run --max-wait 480
```

If companies are still in flight when it returns (big containers: blob count
drives runtime, and local sizing is slower than the old in-region numbers),
keep polling across turns until settled, then harvest:

```bash
python3 scripts/fleet_size.py poll-all --max-wait 480
python3 scripts/fleet_size.py harvest
```

State persists in `companies/.fleet-state.json`, so this is resumable at any
point (`fleet_size.py status` shows where things stand). The detached sizers
survive the harness dying — never assume an in-flight run is dead without
checking `companies/.sizer-work/` (see "Manual rescue" in CLAUDE.md).

## Reading the outcome table

The final JSON has one outcome per company — **never let one failure hide the
rest; report all of them**:

- `sized` / `skipped-unchanged` — healthy. Most days most companies skip
  (UsedCapacity unchanged); a full listing should only happen for companies
  that actually pushed.
- `failed` — triage per the size-company skill's outcome guide (403 =
  IP-rule propagation, not the SAS; process-gone = sleep/network/crash, check
  the work-dir log). Retry a single company with
  `python3 scripts/size_company.py <slug>` — do NOT relaunch the whole fleet
  for one failure.
- `still in flight` after repeated polling — check the work-dir log tail for
  progress before assuming it's stuck; millions of blobs legitimately take
  hours locally.

Fleet-run practicalities:

- Launches sequential, sizers concurrent. Each SA whose firewall needs our IP
  added costs a one-time ~60s propagation wait during launch.
- For a monster container, remind the user: lid open, on power —
  `caffeinate -i` only blocks idle sleep.
