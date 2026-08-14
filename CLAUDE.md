# cdp-harness — FDE data-transfer agent harness

This repo tracks client companies pushing corpus data into Azure blob containers
we provision for them (one storage account + `<slug>-raw` container per company,
in its own resource group, in the **"m1 corpus"** subscription). It measures what
each company has pushed, compares it to what they declared in their manifest, and
produces reports. The single morning entry point is the **daily-brief** skill.

`docs/sizing-internals.md` is the mechanism-level walkthrough of the sizing
pipeline — read it before modifying the sizer. The original battle-tested
sizing runbook (`SIZING-SKILL.md`, croplabel / webspiders / latchel runs,
2026-07) is retired: its knowledge lives in `.claude/skills/size-company/`,
this file, and `docs/sizing-internals.md`; the runbook itself is preserved in
git history.

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
   non-negotiable. **One sanctioned exception:** the `*-azure-transfer`
   skills (gcs, dropbox) *populate* `<slug>-raw` from a cloud source (rwlc
   SAS, 21-day default) — they are the ingest path, not an audit path. They
   only add blobs under their dest prefix and never modify or delete
   existing data; everything else in the harness stays strictly read-only.

4. **Failure isolation.** One broken company must never kill a fleet run. Every
   fleet operation wraps per-company work in try/except, collects a per-company
   outcome (`sized | skipped-unchanged | failed(reason)`), and reports
   all of them at the end. Scripts exit nonzero if *any* company failed, but
   only after processing every company. *Why:* the fleet run is a morning
   routine; a webspiders timeout must not hide croplabel's result.

---

## Repo map

```
CLAUDE.md                    # this file — the spec; keep it current
docs/sizing-internals.md     # mechanism-level walkthrough of the sizer (cache,
                             # concurrency, detection) — read before modifying it
companies/                   # ALL runtime state; gitignored (local only)
  <slug>/
    config.json              # cached Azure discovery (see schema)
    expected-data-sizes.json # manifest declarations (see schema)
    status.json              # lifecycle + last-run outcome (see schema)
    sizing-runs/             # one JSON per run, <YYYYMMDDTHHMMSSZ>.json
    reports/                 # per-company HTML reports + nudge drafts
    blob-index.tsv.gz        # per-blob sizing cache (zip/gz rows, ETag-keyed);
                             # rebuilt by each harvest — the incremental-run seed
  .fleet-state.json          # transient in-flight fleet state; gitignored
  .sizer-work/               # local sizer work files (<slug>-sizer.*); gitignored
.claude/skills/              # the judgment layer
  onboard-company/SKILL.md
  size-company/SKILL.md
  size-company/references/sizing-lore.md   # interpretation knowledge (shared)
  update-all/SKILL.md
  report-company/SKILL.md
  report-all/SKILL.md
  verify-completion/SKILL.md
  daily-brief/SKILL.md
  gcs-azure-transfer/SKILL.md              # GCS export → <slug>-raw via transfer VM
  gcs-azure-transfer/references/commands.md
  dropbox-azure-transfer/SKILL.md          # Dropbox → <slug>-raw (same engine)
  dropbox-azure-transfer/references/commands.md
  gdrive-azure-transfer/SKILL.md           # Google Drive → <slug>-raw (same engine)
  gdrive-azure-transfer/references/commands.md
scripts/                     # the deterministic layer (python3, stdlib only)
  common.py                  # paths, az runner, JSON IO, time, units
  phases.py                  # shared sizing phases: skip/launch/poll/harvest/cleanup
  reconcile.py               # declared-vs-actual math: %, deltas, ETA, flags, lore notes
  corpus_sizer_rest.py       # portable stdlib+SAS sizer; runs locally, detached
  discover_company.py        # az discovery for onboarding → config.json
  size_company.py            # single-company sizing CLI (fleet of one)
  fleet_size.py              # fleet sizing CLI (launch-all / poll-all / harvest)
  transfer_engine.py         # cloud→Azure transfer engine (VM, rclone, tmux)
  gcs_transfer.py            # thin GCS CLI over transfer_engine (Spec only)
  dropbox_transfer.py        # thin Dropbox CLI over transfer_engine (Spec only)
  gdrive_transfer.py         # thin Google Drive CLI over transfer_engine (Spec only)
  bootstrap-vm.sh            # transfer-VM bootstrap (rclone+tmux), ssh-piped
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
    "exists": true                      // INFORMATIONAL — sizing runs locally; false is normal (most companies have no VM)
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
                                         // now tiered: gz-trailer/gz-floor/gz-bad-trailer/gz-exact
                                         // (see "gz" below for the quantified undercount)
  "errors": {"total": 3, "by_type": {"BadZipFile": 3}},
  "cache": {"hits": 800, "misses": 6},   // null in copied-forward and pre-cache runs
  "gz": {"streamed": 2, "streamed_bytes": 900000000000,   // exact-streamed gz blobs
         "uncertain": 1, "uncertain_bytes": 5000000000},  // trailer-unmeasurable
                                         // (compressed bytes); null in old runs
  "detected_services": {                 // path + zip-entry service detection (additive
    "hubspot": {                         // lens — NEVER feeds the headline %)
      "bytes": 400000000000, "blob_count": 0, "entry_count": 12000,
      "path_bytes": 0, "zip_entry_bytes": 400000000000,
      "sources": {"workspace-export": 400000000000}
    }
  },
  "sources_l2": {"workspace-export/hubspot": [4, 0, 400000000000]}, // top-40 second-level
                                         // [files, comp, unc] triples + "(other)" rollup
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
    "outcome": "sized",                  // sized | skipped-unchanged | failed
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

### Local sizing execution (the launch/poll mechanism)

Sizing runs **on this machine** — no VMs are provisioned or required. This is
safe because a SIZE job never bulk-downloads: the sizer reads blob-list pages,
zip central directories, and gzip trailers — kilobytes per blob. (The old
in-region-VM rule existed for the EXTRACT path and for latency, not volume.)

- **Launch:** `phases.launch` starts `scripts/corpus_sizer_rest.py` as a
  **detached local process** (new session, stdin closed, output to
  `<tag>.stdout` — nohup-equivalent), wrapped in `caffeinate -i` on macOS so
  the machine won't idle-sleep mid-run. It seeds the sizer from
  `companies/<slug>/blob-index.tsv.gz` (`CACHE_FILE`) plus any crashed run's
  partial TSV (renamed to `<tag>.seed.tsv`, passed as `SEED_TSV`), and passes
  the declared service names as `EXPECTED_SERVICES`. Work files go to
  `companies/.sizer-work/<slug>-sizer.*` (gitignored). Belt-and-suspenders:
  if the harness/agent dies, the sizer keeps running and its work files remain
  as the manual rescue path. Stale work files are cleared before each launch
  (a stale `.done` would fake completion).
- **Poll:** `<tag>.done` exists → Succeeded; else pid alive → Running; else
  Failed (with the stdout tail as the reason). Instant, local, no az calls.
- **Harvest:** read `<tag>.summary.json` directly, write the sizing-run file,
  move the fresh `<tag>.index.tsv.gz` to `companies/<slug>/blob-index.tsv.gz`
  (per-blob detail now SURVIVES harvest), then cleanup (remaining work files +
  only-ours firewall rule). No output caps, no truncation fallbacks.
- **Cache:** hits are ETag-validated (name+etag+size all must match — error
  rows are never cached), `--no-cache` forces a full re-size. The index and
  `sizes.tsv` both carry a `#matcher\t<fingerprint>` header line: the
  fingerprint is a hash of the ENTIRE built matcher (built-in
  `SERVICE_CATALOG` aliases plus the company's declared service names), not
  just the declared names — so editing a company's `expected-data-sizes.json`
  services invalidates only that company's cache, but editing
  `SERVICE_CATALOG` in a harness upgrade invalidates EVERY company's cache at
  once (full re-size fleet-wide on the next run). Both are intentional, so
  `detected_services` never goes stale against a changed matcher — but the
  fleet-wide case is worth knowing about before a slow morning needs
  explaining. Cached gz rows that the current streaming trigger covers but
  that aren't `gz-exact` are re-measured once (a deliberate one-time miss).
- **Listing:** the sizer runs prefix-parallel listing (`LIST_WORKERS`, default
  8) and pooled zip/gz reads (`SIZER_WORKERS`, default 16). Prefix-parallel
  listing only pays off when data spans multiple top-level prefixes — for a
  wide-flat container (many top-level prefixes, each with few blobs) it costs
  one extra listing request per prefix versus the old single marker walk.
- **Caveats:** `caffeinate -i` does NOT survive a closed lid — keep the laptop
  open/awake for monster containers. A network drop mid-listing kills the run
  (per-blob read errors are merely counted; a listing error is fatal) —
  re-running is idempotent and safe.
- **Legacy:** the harness previously ran the sizer on in-region VMs via
  managed run commands (create/show/delete, 4KB instance-view cap,
  one-invoke-at-a-time). If a VM path is ever needed again (e.g. a
  webspiders-scale container is too slow over the internet), recover it from
  git history (commit c3ba27c and earlier; the retired SIZING-SKILL.md
  runbook is preserved there too).

### Firewall (IP rules — conditional, only touch if needed)

The storage accounts are `defaultAction: Deny` with an **IP allowlist** (the
client's own push locations — e.g. am-city-inc has five client IPs). Before
launching, `phases.ip_rule_ensure` checks the SA's networkRuleSet:
`defaultAction: Allow` → nothing to do; our current public IP already listed →
pre-existing, **NEVER remove it**; otherwise `az storage account network-rule
add --ip-address <our-ip>`, **sleep ~60s for propagation**, and remove it
again at cleanup. **Only remove a rule WE added this run — a pre-existing IP
rule may be the client's own push path; deleting one breaks their transfer.**
Our public IP comes from api.ipify.org (fallback checkip.amazonaws.com) and
changes with location (office/home/VPN) — which is why rules are added
per-run rather than cached. A `403 AuthorizationFailure` on the sizer's first
read = firewall (IP not allowed/propagated), **not** a bad SAS — if it appears
right after adding the rule it's propagation: wait and retry, don't re-mint.

### SAS policy

Account SAS, `--services b --resource-types sco`, **permissions `rl` only**
(read+list — all a SIZE job needs), `--https-only`, **1-day expiry** (long
enough for the biggest containers, short enough to be self-cleaning; we never
revoke, we let them lapse). The SAS is passed to the local sizer via its
process environment (`AZURE_STORAGE_SAS`), never written to disk or logs.

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

### The vm block is informational

Sizing no longer uses VMs, but discovery still records what it finds (prefer
`verify-vm-*`, else any VM in the RG, else `exists: false`) because it's
useful context — most companies have NO VM at all (am-city-inc's RG holds
only the storage account), some have `vm-*-extract` / `vm-dwt-transfer`
leftovers. `vm.exists: false` is normal and blocks nothing.

---

### Cloud → Azure transfers (the ingest path)

Some companies' corpora arrive in a cloud source we must pull ourselves:
Google Workspace Data Export buckets (`dwt-takeout-export-<digits>`, browser
OAuth by the customer's super admin only — no service accounts, no HMAC
keys), a Dropbox account, or a Google Drive (My Drive or Shared Drive).
rclone-with-a-token on a temporary Azure VM is the viable path for all of
them. `scripts/transfer_engine.py` is the ONE engine; `gcs_transfer.py` /
`dropbox_transfer.py` / `gdrive_transfer.py` are thin Spec-only CLIs over
it, driven by the matching `*-azure-transfer` skills. The workflow: VM
`xfer-<slug>` (Dropbox: `xfer-dbx-<slug>`, Drive: `xfer-gdr-<slug>`, so
they can run concurrently) in the company's RG and the SA's region, static
Standard-SKU public IP (never deallocated before teardown), rclone copy in
a tmux session into `<slug>-raw/workspace-export/` (Dropbox:
`dropbox-export/`, Drive: `gdrive-export/`). Rules that differ from the
sizing path — do not cross-contaminate:

- **Network rules are HUMAN-ONLY for this path.** The transfer VM's IP is
  added via the internal network-rules UI by the user; the harness never runs
  `network-rule add` here (rules added outside the UI get stripped).
  `phases.ip_rule_ensure` belongs to the sizing path only.
- **SAS is `racwl`, 21-day default** (write path — the read-only `rl` policy
  above is for sizing). Never revoked; lapses on its own.
- **Secrets** (SAS URL, Google OAuth token) live only in the VM's
  `~/.config/rclone/rclone.conf` (600) and die with the VM — never in files,
  tags, logs, or argv on this machine.
- **No state file** — Azure is the source of truth (VM name + tags carry
  bucket/prefix); discovery reconstructs the phase. Transfer state never
  touches `status.json`.
- Export buckets expire ~60 days after export start (early packets sooner) —
  the engagement has a clock.
- **Same-region VMs can't be allowed by IP rule** (learned on song-division,
  2026-08): Azure storage IP rules never match traffic from a VM in the SA's
  own region — it arrives over the backbone with a private source address.
  The transfer VM needs the `Microsoft.Storage` service endpoint on its
  subnet + a vnet-rule on the SA. The laptop-based sizing path is unaffected
  (external IP, IP rules work).

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
- **gz trailers lie two ways:** ISIZE is mod 2³² (≥4 GiB logical wraps —
  sometimes undetectably) and covers only the LAST member of concatenated/
  bgzip files; garbage trailers on misnamed .gz would overcount up to
  4.29 GB each (DEFLATE's 1032:1 bound now rejects them). Large or floored
  gz blobs are stream-measured exactly (GZ_STREAM_THRESHOLD 256 MB /
  GZ_STREAM_FLOOR_MIN 8 MB, GZ_STREAM_BUDGET 50 GB compressed per run,
  0=off), cached forever by ETag; whatever stays unmeasured is quantified
  in the run's `gz.uncertain*` fields, never silent.
- **BadZipFile errors** = corrupt/mislabeled `.zip` files (common in scraped/
  backup trees); counted at stored size; negligible.
- **Blob COUNT drives runtime, not bytes:** ~3–4k blobs/sec *in-region*.
  webspiders = 10.9M blobs ≈ 45 min; croplabel = 806 ≈ 1 min; latchel =
  2,289 ≈ 2 min. Local sizing adds internet latency: listing stays comparable,
  but zip-heavy containers pay 2–3 round trips per blob — budget hours, not
  minutes, for millions of blobs. Budget polling accordingly.
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
- **Detection is a lens, not a ledger:** `detected_services` (deepest-wins path
  match + zip central-directory entry names) attributes bytes to services even
  inside wrapper exports — but the headline % and per-source reconciliation
  still run on `sources`. A declared service found only inside another
  source's archives is flagged `found-embedded`, not `declared-empty`.

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

The detached-process design means a sizer can outlive a dead harness/agent.
In `companies/.sizer-work/` (TAG = `<slug>-sizer`):

- `$TAG.log` — timestamped progress (`progress N blobs, errors=M` every 5k)
- `$TAG.sizes.tsv` — per-blob rows so far (`wc -l` ≈ blobs done **+ 1**: line 1
  is the `#matcher\t<fingerprint>` header, not a data row)
- `$TAG.stdout` — sizer stdout/stderr
- `$TAG.summary` / `$TAG.summary.json` — written at the end
- `$TAG.done` — exists only on clean completion

Checking by hand:

```bash
ls companies/.sizer-work/$TAG.done && echo DONE || echo NOT-YET
tail -1 companies/.sizer-work/$TAG.log
wc -l < companies/.sizer-work/$TAG.sizes.tsv
pgrep -f corpus_sizer_rest.py
```

If the state file says `Failed` but `$TAG.done` exists, the sizer finished
fine (the tracked pid was probably `caffeinate`'s) — just run harvest; it
reads `$TAG.summary.json` directly. If nothing progresses, check the log tail
for `403 AuthorizationFailure` (firewall/IP propagation — see above) before
suspecting the SAS. Harvest's cleanup deletes `$TAG.sizes.tsv`, but zip/gz
per-blob detail (the cacheable rows) survives it in
`companies/<slug>/blob-index.tsv.gz` — only stored-blob rows (never cached,
since their size comes free from the listing) are lost with the TSV, so copy
`$TAG.sizes.tsv` first only if stored-blob-level detail matters.

## Offline validation

`python3 tests/test_harness.py` exercises everything that doesn't need Azure:
report + dashboard generation, verify-completion, the copied-forward path, and
status transitions, against `tests/fixtures/companies/democo/`.
`fleet_size.py --dry-run launch-all` prints the az commands it would run. Real
az calls are integration-tested live on a small company (croplabel-sized).
