---
name: update-all
description: Use when refreshing sizing for the whole fleet — "update all companies", "size everyone", "fleet sizing run", or as the first step of the morning routine.
---

# Update the whole fleet

Thin orchestrator over `scripts/fleet_size.py`, which runs **fleet-wide
phases, not per-company loops**: skip-check everyone (parallel ARM reads),
launch all non-skipped (sequential, ~30s each), round-robin poll via
instance-view reads (parallel, cheap — total wall time ≈ slowest company, not
the sum), then harvest. Never reimplement per-company logic here —
that lives in `scripts/phases.py` shared with size-company.

## Run it

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/fleet_size.py run --max-wait 480
```

If companies are still in flight when it returns (big containers: blob count
drives runtime, ~3–4k blobs/sec), keep polling across turns until settled,
then harvest:

```bash
python3 scripts/fleet_size.py poll-all --max-wait 480
python3 scripts/fleet_size.py harvest
```

State persists in `companies/.fleet-state.json`, so this is resumable at any
point (`fleet_size.py status` shows where things stand).

## Reading the outcome table

The final JSON has one outcome per company — **never let one failure hide the
rest; report all of them**:

- `sized` / `skipped-unchanged` — healthy. Most days most companies skip
  (UsedCapacity unchanged); webspiders' 45-minute listing should only happen
  on days webspiders actually pushed.
- `no-vm` — collect these and flag the user as action items. Don't guess,
  don't provision, don't start VMs unasked.
- `failed` — triage per the size-company skill's outcome guide (403 =
  firewall propagation, not SAS; Conflict = invoke slot busy, wait ~15s;
  VM not running = ask the user). Retry a single company with
  `python3 scripts/size_company.py <slug>` — do NOT relaunch the whole fleet
  for one failure.
- `still in flight` after repeated polling — check "Manual rescue" in
  CLAUDE.md before assuming it's dead; the nohup'd sizer survives run-command
  agent death.

If any company ever shares a VM with another, launches must serialize per VM —
sequential launching already guarantees this today; keep it sequential.
