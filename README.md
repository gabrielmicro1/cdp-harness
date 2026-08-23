# cdp-harness

An agentic harness for tracking client companies as they push corpus data into
the Azure blob containers we provision for them (one storage account +
`<slug>-raw` container per company, in the **"m1 corpus"** subscription). It
measures what each company has pushed, reconciles it against what their
manifest declared, and produces client-shareable reports plus a fleet
dashboard — with one command for the morning routine.

Built as a set of **Claude Code skills** (the judgment layer) over
**deterministic Python scripts** (stdlib-only, no venv needed). Claude
orchestrates and interprets; the scripts own every number.

## Quick start

```
daily brief
```

Said to Claude in this repo, that runs the whole morning: size the fleet,
regenerate all reports and the dashboard, draft nudges for stalled companies,
and summarize what needs attention. Then open `reports/index.html`.

## Capabilities

### Skills (say these to Claude)

| Skill | What it does |
|---|---|
| **onboard-company** | Discovers a new company's Azure resources (SA, RG, container, VM info) from just a slug, parses the manifest screenshot into per-service expected sizes, echoes the table back for confirmation before writing (a mis-read number poisons every downstream report), and sets up `companies/<slug>/`. |
| **size-company** | Sizes one company's `-raw` container: compressed + uncompressed bytes per source, blob counts, error breakdown. Runs locally as a detached background process — no VM required. Skips automatically (copy-forward) when the account's `UsedCapacity` metric shows nothing changed. |
| **update-all** | The fleet sizing run: skip-checks every company in parallel, launches all non-skipped sizers concurrently, polls to completion, harvests. One broken company never kills the run — every company gets an outcome (`sized \| skipped-unchanged \| failed`). Resumable across sessions. |
| **report-company** | Generates a self-contained, micro1-branded HTML progress report for one company: declared vs received per source (log-scale bars when sources span orders of magnitude), % complete vs the manifest headline, per-service flags, growth rate + projected completion date, and interpretation notes. Clean enough to send to the client. |
| **report-all** | Regenerates every company report plus `reports/index.html` — the internal fleet dashboard: progress bars, 24h deltas, ETAs, stage badges, stall/failure flags, links to the latest per-company reports. |
| **verify-completion** | The sign-off checklist for a company that looks done: headline and per-service totals within tolerance (default 98%), zero-byte blob scan, unexpected-source scan, error review — plus a human-judgment pass over record-count services and overshoots before `stage: complete` is set. |
| **daily-brief** | Composes all of the above into one conversational morning summary: fleet %, who moved in the last 24h, newly stalled companies, action items, ETAs at risk, top 3 things needing attention — and drafts (never sends) a Slack-voice nudge for each stalled company. |
| **gcs-azure-transfer** | The ingest path: copies a Google Workspace Data Export bucket (GCS) into `<slug>-raw/workspace-export/` via a temporary same-region Azure VM running rclone in tmux. Five operations (setup / transfer / status / verify / teardown) that work standalone across days — Azure itself is the state (VM name + tags). Two human-in-the-loop pauses: the storage-firewall entry (internal UI only; same-region VMs need the service-endpoint vnet rule, not an IP rule) and the customer admin's Google OAuth token. |
| **dropbox-azure-transfer** | Sibling of gcs-azure-transfer on the same engine: copies a Dropbox account (or folder) into `<slug>-raw/dropbox-export/` via VM `xfer-dbx-<slug>` — can run alongside a GCS transfer for the same company. Same five operations and pauses; Dropbox-tuned rclone defaults (rate-limit-friendly), no source-expiry clock. |
| **gdrive-azure-transfer** | Third sibling on the same engine: copies a Google Drive — My Drive or a Shared Drive (`--team-drive <id>`) — into `<slug>-raw/gdrive-export/` via VM `xfer-gdr-<slug>`. Drive-API-throttled defaults; knows the Drive quirks (native Docs/Sheets/Slides export as docx/xlsx/pptx with no fixed size, duplicate filenames, per-file download quotas). |
| **s3-azure-transfer** | Fourth VM ingest, same lifecycle but a different copy layer: moves an AWS S3 bucket into `<slug>-raw/s3-export/` via VM `xfer-s3-<slug>` running **azcopy server-to-server copy** (the storage fabric pulls from presigned S3 URLs — bytes never transit the VM), which is what makes a 300M-small-object bucket days instead of months. Prefix-split job queue drained by parallel tmux workers, read-only AWS key on stdin, `probe` day-one gate (requester-pays, Glacier tiers), pilot-first calibration, count+byte rollup verify. |
| **qwilr-azure-transfer** | The first **VM-less** ingest: pulls a whole Qwilr account (page/proposal documents, saved blocks, account settings) from `api.qwilr.com/v1` straight to blob REST into `<slug>-raw/qwilr-export/` — small JSON, so it runs locally, no VM/rclone/tmux. Account-wide bearer token via stdin only (Qwilr has no read-only scope; the client revokes it after), laptop IP-rule firewall, create-only writes (`If-None-Match: *`), resume = re-run. Says up front what the API can't give: no PDF/HTML renders, no audit-trail or analytics export, embedded CDN assets manifested rather than downloaded. |
| **vimeo-azure-transfer** | Second VM-less ingest, different transport: rclone has no Vimeo backend and a video library is too big to proxy through the laptop, so it resolves each download link to a signed CDN URL and drives **Azure server-side copy** (Put Blob / Put Block From URL) into `<slug>-raw/vimeo-export/` — the storage fabric pulls from Vimeo's CDN; video bytes never transit this machine. `probe` is the day-one gate: the `video_files` scope is **plan-gated** (Standard/Pro+), so a lower plan means there is nothing to pull. Blocks staged at 256 MiB with mid-copy CDN re-resolve, create-only commits, resume keyed on `videos/<id>/`. |
| **zoom-azure-transfer** | Third VM-less ingest, on vimeo's transport: pulls cloud recordings (MP4, M4A, transcripts, chat, captions, polls, timelines, AI summaries + per-meeting metadata) into `<slug>-raw/zoom-export/` by server-side copy. Auth is a **Server-to-Server OAuth** app (`recording:read:admin`, three secrets on stdin) that the account owner must *activate* — an unactivated app authenticates and then 400s, which is a client conversation, not a retry. Listing is walked in ~1-month windows and fully materialized per month (page tokens expire during long copies); retention auto-delete gives the engagement a clock. Account-bound, so a multi-account org gets one app and one cycle each. |

### What the sizing actually measures

The sizer (`scripts/corpus_sizer_rest.py`, stdlib + SAS only) never bulk
downloads. It reads blob-list pages, **zip central directories** (ZIP64-aware
range reads — true uncompressed sizes without downloading archives), and
**gzip ISIZE trailers** (exact under 4 GiB, floored above) — except budgeted
gz exact-streaming: at most `GZ_STREAM_BUDGET` compressed bytes per run
(default 50 GB, 0 disables), each blob paid once, cached by ETag. Kilobytes
per blob otherwise, grouped by top-level prefix into per-source totals.
Runtime scales with blob *count*, not bytes.

### Progress intelligence

- **Skip check:** an ARM read of the `UsedCapacity` metric before every run —
  containers only get re-listed on days the client actually pushed.
- **Stall detection:** no byte growth for ≥3 days while under 100% flips a
  company to `stalled` automatically (and back to `pushing` when growth
  resumes).
- **Delta + ETA:** growth rate from the two most recent runs, projected
  completion date, 24h movement on the dashboard.
- **Reconciliation flags:** services declared but empty, services with data
  but declared 0 B, services not in the manifest, record-count declarations
  (listed, never byte-compared), and overshoot >100% (often a wrong manifest —
  commercially significant).
- **Interpretation notes**, applied automatically when the data shows the
  pattern: store-mode zips, export-timestamp prefixes, gz-tiered accuracy
  (exact for budgeted-streamed blobs, quantified `gz.uncertain` /
  `gz.uncertain_bytes` for what the budget didn't reach), BadZipFile noise.
  Hard-won knowledge from the croplabel / webspiders / latchel runs — see
  `.claude/skills/size-company/references/sizing-lore.md`.

## Safety guarantees

- **Read-only against client data.** SAS tokens are `rl` (read+list) only,
  https-only, 1-day expiry, passed via process env and never written to disk.
  The harness never writes a blob, tag, or property to client storage.
- **Firewall discipline.** If a storage account's firewall needs our public IP
  added for a run, it's added, waited on (~60s propagation), and removed at
  cleanup. Pre-existing rules — the client's own push path — are never
  touched.
- **No VMs provisioned, ever.** Sizing runs on this machine as a detached
  process (survives the agent dying; work files in `companies/.sizer-work/`
  are the manual-rescue path).
- **Failure isolation.** Fleet operations report a per-company outcome and
  exit nonzero only after processing everyone.
- **Runtime state stays local.** `companies/*/` and generated reports are
  gitignored; sizing-run files are append-only, so history remains
  reconstructible on disk.

## Under the hood

```
CLAUDE.md                    # the spec: architecture, schemas, operational model
docs/sizing-internals.md     # mechanism-level walkthrough of the sizer
.claude/skills/              # judgment layer (14 skills, see table above)
scripts/
  common.py                  # paths, az runner, JSON IO, time, units
  phases.py                  # skip-check / launch / poll / harvest / cleanup + stage transitions
  reconcile.py               # declared-vs-actual math: %, deltas, ETA, flags, notes
  corpus_sizer_rest.py       # the portable sizer (stdlib + SAS)
  discover_company.py        # Azure discovery for onboarding
  size_company.py            # size ONE company (a fleet of one)
  fleet_size.py              # fleet phases: launch-all / poll-all / harvest / run / status
  gen_report.py              # per-company HTML report
  gen_dashboard.py           # fleet dashboard → reports/index.html
  verify_completion.py       # completion checklist (tolerance parameterized)
companies/<slug>/            # runtime state per company (gitignored):
                             #   config.json, expected-data-sizes.json,
                             #   status.json, sizing-runs/, reports/
tests/test_harness.py        # offline validation — no Azure, no network
```

Scripts are idempotent, take `--root` for fixture testing, `--dry-run` to
print az commands instead of running them, and print machine-readable JSON
outcomes. `size_company.py` and `fleet_size.py` are thin CLIs over the same
`phases.py` functions — single company and fleet can never drift.

Company lifecycle: `onboarding → pushing ⇄ stalled → verifying → complete`
(the `pushing ⇄ stalled` flip is automatic; the rest are human/skill
decisions).

## Requirements

- `az` CLI at `/opt/homebrew/bin/az`, logged in (`~/.azure`), with access to
  the "m1 corpus" subscription
- python3 (stdlib only — no packages, no venv)
- macOS: long runs are wrapped in `caffeinate -i` automatically; keep the lid
  open for monster containers (it doesn't block lid-close sleep)

## Validation

```bash
python3 tests/test_harness.py
```

Exercises everything that doesn't need Azure against fixture companies:
reconciliation math, report + dashboard generation, verify-completion, the
copied-forward path, stall transitions, and the full local launch → poll →
harvest cycle with a fake sizer. `fleet_size.py launch-all --dry-run` shows
the exact az commands a real run would execute.

For the full operational model (firewall rules, SAS policy, the UsedCapacity
skip check, manual rescue of a stuck sizer, JSON schemas, extension recipes),
read **CLAUDE.md** — it's the spec.
