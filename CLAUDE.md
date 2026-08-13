# cdp-harness — FDE data-transfer agent harness

This repo tracks client companies pushing corpus data into Azure blob containers
we provision for them (one storage account + `<slug>-raw` container per company,
in its own resource group, in the **"m1 corpus"** subscription). It measures what
each company has pushed, compares it to what they declared in their manifest, and
produces reports. The single morning entry point is the **daily-brief** skill.

`SIZING-SKILL.md` at the repo root is the original battle-tested sizing skill
(croplabel / webspiders / latchel runs, 2026-07). It is **source material — leave
it in place**. Its operational knowledge is restructured into
`.claude/skills/size-company/` and this file.

---

## Architecture principles (read before changing anything)

1. **Primitives vs. orchestrators.** Primitives act on ONE company
   (`onboard-company`, `size-company`, `report-company`, `verify-completion`).
   Orchestrators are thin fleet-wide loops (`update-all`, `report-all`,
   `daily-brief`) that compose primitives. Orchestrators NEVER reimplement
   primitive logic — if an orchestrator needs new behavior, add it to the
   primitive (or the shared phase module) and call it. *Why:* one code path per
   behavior means a bug fixed for one company is fixed for the fleet, and a
   single company can always be debugged in isolation.

2. **Deterministic logic in scripts, judgment in skills.** Anything that must be
   identical every run (az calls, JSON writing, math, HTML generation) lives in
   Python under `scripts/`. SKILL.md files explain when/why to run scripts, how
   to interpret results, how to handle edge cases, and how to talk to the user.
   The agent orchestrates and interprets; it does NOT improvise sizing math or
   hand-roll az pipelines that a script already owns. *Why:* numbers that feed
   commercial conversations must be reproducible; prose instructions drift,
   scripts don't.

3. **Read-only against client data.** SAS tokens are minted with `rl`
   permissions only (1-day expiry). We NEVER write to client storage accounts —
   no blobs, no tags, no metadata, no container properties. All state lives in
   this repo's filesystem under `companies/`, which is **gitignored** — runtime
   state stays local, never in version control. *Why:* the client's container is
   the commercial artifact being bought; any write from us contaminates the
   audit story ("did micro1 modify the data?"). Provable read-only access is
   non-negotiable.

4. **Failure isolation.** One broken company must never kill a fleet run. Every
   fleet operation wraps per-company work in try/except, collects a per-company
   outcome (`sized | skipped-unchanged | no-vm | failed(reason)`), and reports
   all of them at the end. Scripts exit nonzero if *any* company failed, but
   only after processing every company. *Why:* the fleet run is a morning
   routine; a webspiders timeout must not hide croplabel's result.

---

## Repo map

```
CLAUDE.md                    # this file — the spec; keep it current
SIZING-SKILL.md              # original sizing skill; source material, do not edit
companies/                   # ALL runtime state; gitignored (local only)
  <slug>/
    config.json              # cached Azure discovery (see schema)
    expected-data-sizes.json # manifest declarations (see schema)
    status.json              # lifecycle + last-run outcome (see schema)
    sizing-runs/             # one JSON per run, <YYYYMMDDTHHMMSSZ>.json
    reports/                 # per-company HTML reports + nudge drafts
  .fleet-state.json          # transient in-flight fleet state; gitignored
.claude/skills/              # the judgment layer
  onboard-company/SKILL.md
  size-company/SKILL.md
  size-company/references/sizing-lore.md   # interpretation knowledge (shared)
  update-all/SKILL.md
  report-company/SKILL.md
  report-all/SKILL.md
  verify-completion/SKILL.md
  daily-brief/SKILL.md
scripts/                     # the deterministic layer (python3, stdlib only)
  common.py                  # paths, az runner, JSON IO, time, units
  phases.py                  # shared sizing phases: skip/launch/poll/harvest/cleanup
  reconcile.py               # declared-vs-actual math: %, deltas, ETA, flags, lore notes
  corpus_sizer_rest.py       # VM-side sizer (stdlib+SAS); pushed via run-command
  discover_company.py        # az discovery for onboarding → config.json
  size_company.py            # single-company sizing CLI (fleet of one)
  fleet_size.py              # fleet sizing CLI (launch-all / poll-all / harvest)
  gen_report.py              # per-company HTML report
  gen_dashboard.py           # fleet index.html
  verify_completion.py       # completion checklist
reports/
  index.html                 # generated fleet dashboard (my working view)
tests/
  fixtures/companies/democo/ # fake company for offline validation
  test_harness.py            # runs all offline validation; python3 tests/test_harness.py
```

Ownership: `size_company.py` and `fleet_size.py` are thin CLIs over
`phases.py` — the fleet is a loop over the same phase functions a single
company uses. Never duplicate a phase into a CLI.

---

## JSON schemas

All timestamps are ISO-8601 UTC (`2026-08-13T14:00:00Z`). Sizing-run filenames
use the basic form `20260813T140000Z.json` (filesystem/Windows-safe). All sizes
are stored as **raw bytes** (integers); only the presentation layer converts to
human units.

### `companies/<slug>/config.json` — written by `discover_company.py` at onboarding

Cached so daily runs skip discovery entirely.

```json
{
  "slug": "croplabel",                  // company slug; dir name; lowercase, hyphens
  "subscription": "m1 corpus",          // subscription name (az account set -s)
  "subscription_id": "600b01d1-...",    // resolved id, sanity check only
  "resource_group": "rg-croplabel",     // the SA's RG (VMs live here too)
  "storage_account": "stcroplabel",     // discovered: RG name contains slug
  "container": "croplabel-raw",         // always <slug>-raw; -scrubbed & insights-logs-* ignored
  "vm": {
    "name": "verify-vm-croplabel",      // preferred verify-vm-*, else any VM in RG
    "resource_group": "rg-croplabel",
    "exists": true                      // false = no VM found (~40% of companies!) — sizing impossible until resolved
  },
  "onboarded_at": "2026-08-13T14:00:00Z"
}
```

### `companies/<slug>/expected-data-sizes.json` — from the manifest, human-confirmed

```json
{
  "slug": "croplabel",
  "manifest_total_bytes": 3500000000000, // the manifest's HEADLINE total. Stored separately
                                         // because it can legitimately exceed the itemized
                                         // sum (record-declared services + rounding).
                                         // Headline %-complete uses THIS number.
  "services": {
    "gdrive":  {"bytes": 1200000000000}, // byte-declared service
    "slack":   {"records": 1500000},     // record-count declaration: EXCLUDED from byte
                                         // reconciliation, but listed + flagged in reports
    "zendesk": {"bytes": 0}              // declared 0 B: flagged if data actually appears
  },
  "source": "manifest screenshot, 2026-08-13",
  "confirmed_by_user": true,             // MUST be true before any report trusts it —
                                         // a mis-OCR'd number poisons every downstream report
  "created_at": "2026-08-13T14:00:00Z"
}
```

### `companies/<slug>/sizing-runs/<ts>.json` — one per run, written by harvest

```json
{
  "slug": "croplabel",
  "timestamp": "2026-08-13T14:00:00Z",
  "method": "sized",                     // "sized" = VM run happened;
                                         // "copied-forward" = UsedCapacity unchanged, numbers
                                         // copied from the previous run (no VM launch)
  "copied_from": null,                   // when copied-forward: source run's timestamp
  "used_capacity_bytes": 3210987654321,  // az monitor UsedCapacity value observed pre-run
  "used_capacity_at": "2026-08-13T13:00:00Z", // that metric datapoint's timestamp
  "duration_seconds": 61,                // VM-side sizer wall time (0 for copied-forward)
  "totals": {
    "blob_count": 806,
    "compressed_bytes": 3210987654321,
    "uncompressed_bytes": 3300000000000,
    "zero_byte_blobs": 2                 // used by verify-completion
  },
  "sources": {                           // keyed by top-level blob prefix ("(root)" for none)
    "gdrive": {"blob_count": 400, "compressed_bytes": 1, "uncompressed_bytes": 2}
  },
  "methods": {"zip": 500, "gz": 6, "stored": 300}, // per sizing method blob counts —
                                         // gz>0 triggers the tar.gz-undercount caveat in reports
  "errors": {"total": 3, "by_type": {"BadZipFile": 3}},
  "notes": []                            // free-form strings (e.g. truncation fallback used)
}
```

### `companies/<slug>/status.json`

```json
{
  "slug": "croplabel",
  "stage": "pushing",                    // onboarding | pushing | stalled | verifying | complete
  "last_run": {
    "timestamp": "2026-08-13T14:00:00Z",
    "outcome": "sized",                  // sized | skipped-unchanged | no-vm | failed
    "reason": null                       // human-readable failure reason when outcome=failed
  },
  "last_change_detected_at": "2026-08-12T14:00:00Z" // last time total bytes grew
}
```

**Stage transitions** (implemented in `phases.update_status`; do not fork this
logic): sizing runs auto-flip `pushing → stalled` (no byte growth for ≥3 days
while <100% of manifest headline) and `stalled → pushing` (growth resumed).
`onboarding`, `verifying`, and `complete` are only ever set by humans/skills
(onboard sets `pushing` when done; verify-completion sets `complete` on pass);
automated runs never override them.

---

## Azure operational model

Environment: `az` lives at `/opt/homebrew/bin/az` (NOT on the default
non-interactive PATH — every shell/subprocess must prefix
`export PATH="/opt/homebrew/bin:$PATH"`; `common.py` does this for scripts).
Login is cached in `~/.azure`. Subscription is **"m1 corpus"** (selected by
name; the id `600b01d1…` is a sanity-check hint). Claude drives az end-to-end
itself — no handing commands to the user unless they ask.

### Managed Run Commands (the launch/poll mechanism)

We use **managed run commands** (`az vm run-command create/show/delete`), not
the legacy action API (`az vm run-command invoke`), for launching and polling
the sizer:

- **Launch:** `az vm run-command create -g $VM_RG --vm-name $VM
  --run-command-name sizer-$SLUG --script "$SCRIPT" --async-execution`.
  The script base64-decodes `corpus_sizer_rest.py` onto the VM, launches it
  under `nohup` writing to `/var/tmp/$TAG.*` (belt-and-suspenders: if the
  run-command agent dies, the sizer survives and the log files remain as a
  manual rescue path), then `wait`s on the pid so the run-command's
  `executionState` tracks sizer completion, and finally
  `cat /var/tmp/$TAG.summary.json` so the summary rides home in instance-view
  output.
- **Poll:** `az vm run-command show -g $VM_RG --vm-name $VM --run-command-name
  sizer-$SLUG --instance-view --query "instanceView.{state:executionState,out:output,err:error}"`.
  This is a **pure ARM management-plane read**: no execution slot consumed, no
  `Conflict` errors, instantly repeatable, parallel-safe across the whole
  fleet. This replaces the old invoke-based VM-side polling entirely.
- **4KB output cap:** instance-view output+error is truncated at ~4KB. The
  sizer's `summary.json` is deliberately compact (per-source arrays) to fit.
  Harvest detects truncation (unparseable JSON) and falls back to ONE
  `az vm run-command invoke` that `cat`s `/var/tmp/$TAG.summary.json`.
- **Cleanup:** `az vm run-command delete -g $VM_RG --vm-name $VM
  --run-command-name sizer-$SLUG --yes`. Run-command resources **persist on the
  VM resource**, and a stale same-name resource breaks the next `create` — so
  harvest always deletes, and launch defensively deletes any stale resource
  first. Cleanup also removes `/var/tmp/$TAG.*` temp files (via one invoke) and
  conditionally removes the firewall rule (below).

### The one-invoke-at-a-time rule

`az vm run-command invoke` (legacy action API) allows **ONE execution at a time
per VM** — a second concurrent invoke gets `Conflict`. Managed run-command
*show* polling is exempt (it's an ARM read). Invoke is still used in exactly
three places, all strictly after the managed run command reaches a terminal
state: (1) truncation-fallback `cat` of summary.json, (2) optional chunked
fetch of the per-blob `sizes.tsv`, (3) temp-file cleanup. Never run two invokes
against the same VM concurrently; never leave a VM-side sleep loop running (it
jams the slot). A `Conflict` means another invoke is still finishing — wait
~15s and retry; any background sizer keeps running regardless.

### Firewall (conditional — only touch if needed)

The VM's subnet must be allowed on the storage account. **Check first; only add
if missing; only remove a rule WE added — NEVER delete a pre-existing rule**
(transfer VMs' subnets are usually already whitelisted; verify VMs' often
aren't; a pre-existing rule may be what the client's own push path depends on).
After adding: ensure the subnet has the `Microsoft.Storage` service endpoint,
then `az storage account network-rule add`, then **sleep ~60s for
propagation**. A `403 AuthorizationFailure` on the sizer's first read =
firewall (subnet not allowed), **not** a bad SAS — if it appears right after
adding a rule, it's propagation: wait and retry, don't re-mint the SAS.

### SAS policy

Account SAS, `--services b --resource-types sco`, **permissions `rl` only**
(read+list — all a SIZE job needs), `--https-only`, **1-day expiry** (long
enough for the biggest containers, short enough to be self-cleaning; we never
revoke, we let them lapse). The SAS and the sizer script are both base64'd
through the run-command script to survive shell quoting.

### The UsedCapacity skip check (Phase 0)

Before launching, read the storage account's `UsedCapacity` metric via
`az monitor metrics list` — an ARM read, no blob listing, costs nothing. If the
latest datapoint equals the `used_capacity_bytes` recorded in the last sizing
run, write a new run file with `method: "copied-forward"` copying the last
run's numbers, and skip the launch. Webspiders' 45-minute listing should only
happen on days webspiders actually pushed. Caveats: the metric is
**account-level** (includes `-scrubbed` etc. — a scrub run can force one
redundant re-size; harmless) and lags up to ~an hour; both errors are in the
safe direction (a false "changed" just re-sizes; a same-hour push is caught the
next morning). No previous sized run → always launch.

### The no-verify-VM reality

**~40% of companies have NO `verify-vm-<slug>`.** They have a `vm-*-extract`,
`vm-dwt-transfer`, a leftover `vm-sizer-*`, or nothing. Discovery prefers
`verify-vm-*`, falls back to ANY VM in the RG, else records
`vm.exists: false`. A no-VM company gets outcome `no-vm` on every sizing
attempt: **flag the user, don't guess** — starting a stopped VM or
provisioning a sizer VM is their call. The VM must be *running*; check
PowerState before launch.

---

## Learned the hard way (from real croplabel / webspiders / latchel runs)

Preserve these. They are why the code looks the way it does.

- **Store-mode zips:** ratio ≈ 1.0 across everything = zips used as bundles,
  not compression (croplabel). The tell: total uncompressed slightly *below*
  compressed. It's real data, not a bug.
- **Real compression ratios seen in the wild:** latchel slack 11×, latchel
  hubspot 19×, webspiders code ~2× (declared-vs-actual ~8×). Genuinely
  compressed exports — the zip central directory reports true uncompressed
  sizes.
- **Timestamp prefixes:** the top-level blob prefix may be an export
  *timestamp* (latchel `20260707T180401Z`, a Google Takeout run), not a source
  name — real sources are one level deeper. Reports must say so; optionally
  re-split on the 2nd path segment.
- **tar.gz is trailer-floored:** the sizer reads the 4-byte gzip ISIZE trailer
  — exact below 4 GiB, floored at compressed size above (ISIZE is mod 2³²). So
  multi-GB DB-backup tarballs read as ~stored size: a small, known undercount,
  the price of not streaming them for hours.
- **BadZipFile errors** = corrupt/mislabeled `.zip` files (common in scraped/
  backup trees); counted at stored size; negligible.
- **Blob COUNT drives runtime, not bytes:** ~3–4k blobs/sec. webspiders =
  10.9M blobs ≈ 45 min; croplabel = 806 ≈ 1 min; latchel = 2,289 ≈ 2 min.
  Budget polling accordingly.
- **Units:** decimal GB (÷10⁹) everywhere in this harness. The older canonical
  `size_corpus.py` used GiB (÷1024³, ~7% lower) — account for that if
  comparing against pre-harness reports. Headline totals in TB when ≥1 TB;
  per-source breakdowns in GB (sub-TB sources read cleaner than `0.89 TB`).
- **Declared vs actual = declared vs UNCOMPRESSED:** the manifest declares the
  client's *logical* data size, so uncompressed is the like-for-like column
  (store-mode makes it moot; heavy compression makes it matter).
- **Manifest totals exceed itemized sums** legitimately (record-declared
  services + rounding) — headline % uses the manifest total.
- **Sources spanning orders of magnitude** (latchel: 476 MB → 2.6 TB) need a
  log x-axis or the small ones vanish.

---

## Conventions

- **Company directories are NOT committed** (`companies/*/` is gitignored;
  only `companies/.gitkeep` is tracked). The audit trail is the append-only
  `sizing-runs/` files on disk — run files and reports are never overwritten,
  only added, so the numbers behind commercial conversations stay
  reconstructible locally. Nothing in the harness runs git at runtime.
- **Timestamps ISO-8601 UTC** in JSON; basic format in filenames.
- **Bytes in storage, human units at presentation** (decimal GB/TB, ÷10⁹).
- **Scripts are idempotent** — re-running harvest or report generation is
  always safe — and **exit nonzero on failure**, with per-company outcomes on
  stdout as JSON (one summary object; machine-parseable by the skills).
- **Scripts are stdlib-only python3** (the VM sizer strictly so; local scripts
  too, so nothing needs a venv).
- **`--dry-run`** on anything that would touch Azure prints the az commands
  instead of running them.
- **`--root`** on every company-reading script (default `companies/`) so tests
  run against `tests/fixtures/companies/` without touching real state.
- Skills never sign nudge drafts as AI, never use em dashes in them, and keep
  them in the user's casual Slack voice.

## Recipes

**Add a new skill:** create `.claude/skills/<name>/SKILL.md` with `name` +
`description` frontmatter (description = "Use when …" triggers only, no
workflow summary). Put anything deterministic in `scripts/` and have the skill
call it. If it's a fleet operation, it must loop a primitive and collect
per-company outcomes. Update the repo map here.

**Add a report metric:** compute it in `gen_report.py` (or `gen_dashboard.py`)
from sizing-run JSON — never in the skill layer. If it needs new per-run data,
add the field to the sizing-run schema (documented above), emit it from
`corpus_sizer_rest.py`'s summary.json, and handle old run files that lack the
field (treat as unknown, not zero).

**Add a lifecycle stage:** add it to the `stage` enum in this file, decide who
may set it (automated transitions go in `phases.update_status`; human-set
stages get set by a skill), teach `gen_dashboard.py` its badge, and grep for
existing stage checks (`stage ==`) to audit every consumer.

## Manual rescue (stuck sizer)

The nohup design means a sizer can outlive a dead run-command agent. On the VM
(`/var/tmp/`, TAG = `<slug>-sizer`):

- `$TAG.log` — timestamped progress (`progress N blobs, errors=M` every 5k)
- `$TAG.sizes.tsv` — per-blob rows so far (`wc -l` ≈ blobs done; ~3–4k/sec)
- `$TAG.stdout` — sizer stdout/stderr
- `$TAG.summary` / `$TAG.summary.json` — written at the end
- `$TAG.done` — exists only on clean completion

Fallback poll (the old invoke-based path — one at a time per VM!):

```bash
az vm run-command invoke -g $VM_RG -n $VM --command-id RunShellScript \
  --scripts "ls /var/tmp/$TAG.done 2>/dev/null && echo DONE || echo NOT-YET; tail -1 /var/tmp/$TAG.log; echo rows=\$(wc -l < /var/tmp/$TAG.sizes.tsv 2>/dev/null)" \
  --query "value[0].message" -o tsv
```

If the managed run command shows `Failed` but `$TAG.done` exists, the sizer
finished fine — harvest from the files directly (invoke `cat
/var/tmp/$TAG.summary.json`). If neither progresses, check the log tail for
`403 AuthorizationFailure` (firewall — see above) before suspecting the SAS.

## Offline validation

`python3 tests/test_harness.py` exercises everything that doesn't need Azure:
report + dashboard generation, verify-completion, the copied-forward path, and
status transitions, against `tests/fixtures/companies/democo/`.
`fleet_size.py --dry-run launch-all` prints the az commands it would run. Real
az calls are integration-tested live on a small company (croplabel-sized).
