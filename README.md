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
| **offboard-company** | The reverse of onboarding, without losing anything: moves `companies/<slug>/` into `companies/.archive/<slug>/`, a dot-prefixed dir the fleet enumeration skips — the company disappears from the dashboard and every fleet loop while its full state (config, expected sizes, sizing-run history, the blob-index cache that seeds an incremental re-size) stays intact. Local bookkeeping only — Azure and the client's data are never touched. Refuses mid-sizing-run, idempotent both ways; `restore <slug>` brings a company back whenever a later sizing is needed. |
| **size-company** | Sizes one company's `-raw` container: compressed + uncompressed bytes per source, blob counts, error breakdown. Runs locally as a detached background process — no VM required. Skips automatically (copy-forward) when the account's `UsedCapacity` metric shows nothing changed. |
| **update-all** | The fleet sizing run: skip-checks every company in parallel, launches all non-skipped sizers concurrently, polls to completion, harvests. One broken company never kills the run — every company gets an outcome (`sized \| skipped-unchanged \| failed`). Resumable across sessions. |
| **report-company** | Generates a self-contained, micro1-branded HTML progress report for one company: declared vs received per source (log-scale bars when sources span orders of magnitude), % complete vs the manifest headline, per-service flags, growth rate + projected completion date, and interpretation notes. Clean enough to send to the client. |
| **report-all** | Regenerates every company report plus `reports/index.html` — the internal fleet dashboard: progress bars, 24h deltas, ETAs, stage badges, stall/failure flags, links to the latest per-company reports. |
| **verify-completion** | The sign-off checklist for a company that looks done: headline and per-service totals within tolerance (default 98%), zero-byte blob scan, unexpected-source scan, error review — plus a human-judgment pass over record-count services and overshoots before `stage: complete` is set. |
| **deep-verify** | The certification pass, run at a commercial milestone (typically right before verify-completion): stream-decompresses **every** compressed blob (zip/gz/bz2/xz) so the totals are measurements rather than zip-central-directory and gz-trailer assertions. The one sizing operation that uses a VM — a temporary in-region `deepv-<slug>` where egress is free and archives move ~4-5 TB/h (the laptop would pay ~$85/TB and days of wall clock); still strictly read-only (`rl` SAS), auto-torn-down at harvest. Lands as a normal sizing run plus a `verification` block splitting bytes into measured / metadata-trusted / unmeasurable-format, with a count of zips whose central directory lied. Every measurement caches by ETag, so later shallow daily runs replay the exact numbers at zero HTTP. |
| **daily-brief** | Composes all of the above into one conversational morning summary: fleet %, who moved in the last 24h, newly stalled companies, action items, ETAs at risk, top 3 things needing attention — and drafts (never sends) a Slack-voice nudge for each stalled company. |
| **mint-sas** | Mints the credential a client pushes with: an **account-key `racwl`** (no-delete) container SAS, 14-day default, delivered as a password-protected zip in `companies/<slug>/` with the password on stdout only — zip and password go over separate channels, and the SAS URL never lands in a file, report, or Slack draft. Account-key signing sidesteps the storage firewall entirely and is the only way past user-delegation's 7-day cap. Every mint is recorded in `companies/.sas-ledger.json` (metadata only — **never the token**) and can be rendered as `reports/sas-tokens.html` to show what is outstanding. `--read-only` issues an `rl` token instead, which is what a data *buyer* gets. |
| **gcs-azure-transfer** | The ingest path: copies a Google Workspace Data Export bucket (GCS) into `<slug>-raw/workspace-export/` via a temporary same-region Azure VM running rclone in tmux. Five operations (setup / transfer / status / verify / teardown) that work standalone across days — Azure itself is the state (VM name + tags). Two human-in-the-loop pauses: the storage-firewall entry (internal UI only; same-region VMs need the service-endpoint vnet rule, not an IP rule) and the customer admin's Google OAuth token. |
| **dropbox-azure-transfer** | Sibling of gcs-azure-transfer on the same engine: copies a Dropbox account (or folder) into `<slug>-raw/dropbox-export/` via VM `xfer-dbx-<slug>` — can run alongside a GCS transfer for the same company. Same five operations and pauses; Dropbox-tuned rclone defaults (rate-limit-friendly), no source-expiry clock. |
| **gdrive-azure-transfer** | Third sibling on the same engine: copies a Google Drive — My Drive or a Shared Drive (`--team-drive <id>`) — into `<slug>-raw/gdrive-export/` via VM `xfer-gdr-<slug>`. Drive-API-throttled defaults; knows the Drive quirks (native Docs/Sheets/Slides export as docx/xlsx/pptx with no fixed size, duplicate filenames, per-file download quotas). |
| **s3-azure-transfer** | Fourth VM ingest, same lifecycle but a different copy layer: moves an AWS S3 bucket into `<slug>-raw/s3-export/` via VM `xfer-s3-<slug>` running **azcopy server-to-server copy** (the storage fabric pulls from presigned S3 URLs — bytes never transit the VM), which is what makes a 300M-small-object bucket days instead of months. Prefix-split job queue drained by parallel tmux workers, read-only AWS key on stdin, `probe` day-one gate (requester-pays, Glacier tiers), pilot-first calibration, count+byte rollup verify. |
| **github-azure-transfer** | Fifth VM ingest — engine lifecycle, hand-written copy layer: rclone has no GitHub backend, so `scripts/github_vm_pull.py` runs on VM `xfer-gh-<slug>` and does the whole pull there — per repo a `git clone --mirror` (code + full history), the `.wiki.git` clone when a wiki exists, `git lfs fetch --all` when `.gitattributes` declares LFS, and four paginated JSONL exports (issues, pulls, issue_comments, review_comments) — staged on the VM's disk, then azcopied to `<slug>-raw/github-export/`. Nothing downloads to this machine. Auth is a client-made **fine-grained PAT** an org owner must approve (the day-one stall, caught by `probe`), one stdin line, never in a clone URL or argv. Upload-what-succeeded: a pass with failed repos still ships what completed and a re-run mops up (per-repo resume markers). Verify runs on the *laptop* against the uploaded manifest, since the VM is normally gone by then. Out of scope: Actions logs/artifacts, Packages, Projects, Discussions, release assets — wikis are in. |
| **zoho-azure-transfer** | Sixth VM ingest, and the first **multi-product** one: one skill and one script cover Zoho **CRM, Learn and WorkDrive** behind a `--product` dimension, landing in `<slug>-raw/zoho-export/{crm,learn,workdrive}/`. It has to use a VM: Zoho's downloads authenticate with `Zoho-oauthtoken` and Azure's copy-from-URL only speaks `Bearer`, so the server-side-copy transport is structurally unavailable and attachment bytes must be staged — `scripts/zoho_vm_pull.py` does the whole pull on `xfer-zoho-<slug>` and azcopies each unit as it completes, so a mid-run VM loss costs one unit, not the pass. Auth is a client-made **Self Client** (data center + client id + secret + long-lived refresh token, four stdin lines; they do the grant exchange because Zoho grant tokens expire in 3–10 minutes). The **data center is the day-one stall** — a wrong `.com`/`.eu` looks like a generic auth failure — so `probe` catches it three ways, laptop-side, before anything bills. CRM's ledger is REST JSON per module (v8 makes `fields` mandatory, so field metadata is fetched first and a too-wide module re-fetches its extra chunks by record id), cross-checked against a per-module Bulk Read ZIP — which deliberately excludes Notes, Attachments and Emails, so those are pulled separately. Learn's knowledge base has no documented API: the probe attempts the undocumented paths and the run records what answered, never assuming. Per-unit resume markers **plus page cursors**, so a 133 GB module resumes mid-walk instead of restarting. No byte estimate is ever offered — Zoho publishes no record size. |
| **figma-azure-transfer** | **Seventh VM ingest** — engine lifecycle, VM-side REST puller: Figma has no rclone backend and **no bulk export of any kind** (and no `.fig` source-file endpoint — the export is API JSON + assets, a *derivative*, not a restorable backup), so `scripts/figma_vm_pull.py` runs on VM `xfer-figma-<slug>` and walks teams → folders → files, staging per file the node-tree JSON, comments, the version-history list, the embedded image fills (whose presigned URLs expire in ≤14 days), one PNG render per page, and per team the published component/style libraries — azcopied to `<slug>-raw/figma-export/`, each unit as it completes. The **rate limit is the clock**: file JSON, node JSON and renders share one Tier-1 bucket capped at 20/min even on Enterprise, so the puller paces itself to the documented per-plan caps (429 stays the backstop) and a real workspace runs for hours-to-days — per-file resume markers and a library cursor make every re-run a resumption. Auth is a client-made **personal access token** from a **Full/Dev seat** (a View/Collab token gets 6 Tier-1 calls per *month* — the day-one stall, caught by `probe` along with team visibility), one stdin line, granular read scopes, ≤90-day expiry. **Team ids are the day-one prerequisite** — no API lists teams; the client copies them from their file-browser URLs, and a team they forget is invisible. Verify runs on the *laptop* against the uploaded manifest; no byte estimate is ever offered pre-run — Figma publishes no sizes. Out of scope: `.fig` files, comment reactions, per-version file JSON, variables (Enterprise-seat-gated), Dev Mode resources. |
| **teams-azure-transfer** | **Eighth VM ingest** — engine lifecycle, VM-side REST puller: Microsoft Teams has no rclone backend and no bulk export, so `scripts/teams_vm_pull.py` runs on VM `xfer-teams-<slug>` and walks the tenant page by page against Microsoft Graph — team/channel/user/membership JSONL, then per channel every message thread with its replies fully expanded plus the `hostedContents` embedded in message bodies — azcopied to `<slug>-raw/teams-export/`, each unit as it completes. **The SharePoint boundary is the design:** a channel's file tab, and any attachment referenced from a message, lives in that team's SharePoint document library rather than Teams storage, so those bytes belong to a future SharePoint pull and stay references here — the split that kills saxon-style per-account duplication. Auth is a client-made **Entra ID app registration** with *application* (not delegated) permissions, admin-consented, three secrets on stdin. The day-one stall is Microsoft's own message-content gate rather than a bad token: `probe` tries exactly one message page before any VM exists and classifies the answer as `open`, `metered-model-required` (402 — the tenant must link an Azure subscription for Teams' metered API billing) or `protected-api-approval-missing` (403 — aka.ms/GraphTeamsProtectedApis, days to weeks, a client conversation). Per-channel resume markers plus `next_link` cursors; verify runs on the *laptop*. No byte estimate is ever offered — Graph publishes no message or attachment size. Out of scope: 1:1/group/meeting chats, calendars, mail, tabs, Planner, meeting recordings. |
| **slack-azure-transfer** | **Ninth VM ingest**, and the only one whose source is already sitting *inside* the destination. A Business+ / Enterprise compliance export is a complete transcript and **zero file bytes** — every attachment, image, video, canvas and huddle transcript survives only as an authenticated `files.slack.com` link inside the JSON. On a real 2.35 GB export that is 63,636 unique files, 62,056 of them Slack-hosted and worth **81.1 GB — about 34× the export**, so a company whose Slack "landed" has delivered the index and none of the content. `discover-export` finds the export in `<slug>-raw` (a `.zip`, confirmed by two ranged reads of its central directory rather than a download, or an already-extracted `channels.json` + `users.json` tree) and **asks rather than guessing** when it finds none or several. The export **carries its own auth** — one workspace-wide `xoxe-` token in every URL — so there is normally no client credential to ask for; `write-creds` is an override for links that were stripped or expired, and that expiry is the day-one stall (export links die *with* the export, so a dead token means a fresh export, which `probe` settles with one live Slack request before anything bills). Transport is **Azure server-side copy** (vimeo's), so file bytes never touch the VM and create-only is API-enforced. Verify is stronger than its siblings': the export declares a byte `size` for every file, so declared-vs-committed is a real source-truth claim rather than staged→container. A ledger links every file back to the conversation and message that referenced it. External (Google Drive) files are recorded, never fetched — Slack holds no bytes for them, only a link. |
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
- **No VMs for daily sizing.** Sizing runs on this machine as a detached
  process (survives the agent dying; work files in `companies/.sizer-work/`
  are the manual-rescue path). The one exception is **deep-verify**, which is
  a genuine bulk-download job and so runs in-region on a temporary VM that is
  torn down at harvest — still with a read-only `rl` SAS.
- **Failure isolation.** Fleet operations report a per-company outcome and
  exit nonzero only after processing everyone.
- **Runtime state stays local.** `companies/*/` and generated reports are
  gitignored; sizing-run files are append-only, so history remains
  reconstructible on disk.

## Under the hood

```
CLAUDE.md                    # the spec: architecture, schemas, operational model
docs/sizing-internals.md     # mechanism-level walkthrough of the sizer
docs/github-transfer-handoff.md  # provenance + scope of the GitHub pull layer
.claude/skills/              # judgment layer (22 skills, see table above)
scripts/
  common.py                  # paths, az runner, JSON IO, time, units
  phases.py                  # skip-check / launch / poll / harvest / cleanup + stage transitions
  reconcile.py               # declared-vs-actual math: %, deltas, ETA, flags, notes
  corpus_sizer_rest.py       # the portable sizer (stdlib + SAS)
  discover_company.py        # Azure discovery for onboarding
  offboard_company.py        # offboard / restore / list — archive a company
                             #   out of the fleet (local move; Azure untouched)
  size_company.py            # size ONE company (a fleet of one)
  fleet_size.py              # fleet phases: launch-all / poll-all / harvest / run / status
  gen_report.py              # per-company HTML report
  gen_dashboard.py           # fleet dashboard → reports/index.html
  verify_completion.py       # completion checklist (tolerance parameterized)
  deep_verify.py             # deep-verify step machine: the sizer with DEEP_VERIFY=1
                             #   on a temporary in-region VM, auto-teardown at harvest
  # the ingest layer — cloud → <slug>-raw (the sanctioned write path)
  transfer_engine.py         # THE VM engine: create / allow-network / tmux / teardown
  bootstrap-vm.sh            # transfer-VM bootstrap (rclone + azcopy + tmux), ssh-piped
  gcs_transfer.py            # Google Workspace export bucket  ] thin Spec-only CLIs
  dropbox_transfer.py        # Dropbox account or folder       ] over transfer_engine
  gdrive_transfer.py         # My Drive or a Shared Drive      ] (rclone copies)
  s3_transfer.py             # S3: engine VM, azcopy server-side copy, prefix-split jobs
  azcopy-runner.sh           # VM-side azcopy job-queue worker (ssh-piped by s3_transfer)
  s3_flat.py                 # VM-side engine for FLAT buckets: sharded listing → chunked
                             #   Put-Blob-From-URL copy → streamed manifest verify
  github_transfer.py         # GitHub: engine VM, own pull layer, laptop-side verify
  github_vm_pull.py          # VM-side puller: mirror + wiki clones, LFS, 4 JSONL exports
  zoho_transfer.py           # Zoho CRM/Learn/WorkDrive: engine VM, REST pull layer,
                             #   one --product dimension, laptop-side verify
  zoho_vm_pull.py            # VM-side puller: CRM ledger + Bulk Read ZIPs +
                             #   attachments, Learn courses/KB, WorkDrive
  figma_transfer.py          # Figma: engine VM, REST pull layer, rate-tier
                             #   pacing, laptop-side verify
  figma_vm_pull.py           # VM-side puller: file JSON + comments/versions +
                             #   image fills + page renders + team libraries
  teams_transfer.py          # Teams: engine VM, Graph REST pull layer,
                             #   laptop-side verify
  teams_vm_pull.py           # VM-side puller: team/channel/user/membership
                             #   JSONL + per-channel threads + hostedContents
  slack_transfer.py          # Slack: finds the client's export INSIDE
                             #   <slug>-raw, then engine VM + server-side copy
  slack_vm_pull.py           # VM-side puller: the ledger (file → conversation
                             #   → blob) + Put-Blob-From-URL copy of every link
  qwilr_transfer.py          # Qwilr REST → blob REST      ] VM-less, standalone: run
  vimeo_transfer.py          # Vimeo CDN → server-side copy ] locally, laptop IP-rule
  zoom_transfer.py           # Zoom recordings, same        ] firewall, create-only writes
companies/<slug>/            # runtime state per company (gitignored):
                             #   config.json, expected-data-sizes.json,
                             #   status.json, sizing-runs/, reports/
tests/test_harness.py        # offline validation — no Azure, no network
tests/fixtures/              # democo (a fake company) + a SYNTHETIC Slack
                             #   export built to the real schema
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
reconciliation math, report + dashboard generation, verify-completion,
offboard/restore archiving, the copied-forward path, stall transitions, the
full local launch → poll → harvest cycle with a fake sizer, and — for the
Slack ingest — the whole copy phase against an in-memory Azure and Slack
(server-side copy, block staging, a since-deleted file, a fallback to
streaming, a dead export token aborting the run, and a resume that re-copies
nothing). `fleet_size.py launch-all --dry-run` shows the exact az commands a
real run would execute.

For the full operational model (firewall rules, SAS policy, the UsedCapacity
skip check, manual rescue of a stuck sizer, JSON schemas, extension recipes),
read **CLAUDE.md** — it's the spec.
