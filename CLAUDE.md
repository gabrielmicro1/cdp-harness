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
   skills (gcs, dropbox, gdrive, s3, github, zoho, figma, teams, slack, qwilr,
   vimeo, zoom) *populate* `<slug>-raw`
   from a cloud source (rwlc SAS — 21-day default on the VM paths, 1–2-day on
   the local qwilr/vimeo/zoom pulls). The pulls that write through the blob
   REST API — local qwilr/vimeo/zoom, plus the VM-based **slack** file pull,
   which shares vimeo's server-side-copy transport — are additionally
   create-only via `If-None-Match: *`, i.e. no-overwrite is API-ENFORCED
   there rather than an azcopy flag. They are the ingest path, not an audit
   path. They
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
    slack/                   # Slack engine state: channel.json (registration,
                             #   parents, high-water marks), snapshot.json (merged
                             #   normalized channel read), drafts.json (receipts)
  .fleet-state.json          # transient in-flight fleet state; gitignored
  .sas-ledger.json           # client push-SAS ledger (metadata only, never
                             # tokens); written by mint_push_sas.py; gitignored
  .sizer-work/               # local sizer work files (<slug>-sizer.*); gitignored
  .voice/                    # harvested sent-message corpus (messages.jsonl),
                             #   reviewed style.md, harvest-state.json; gitignored
  .archive/<slug>/           # offboarded companies (full state, intact); the dot
                             # prefix hides them from list_companies -> dashboard
                             # and every fleet loop; restore moves them back
.claude/skills/              # the judgment layer
  onboard-company/SKILL.md
  offboard-company/SKILL.md
  size-company/SKILL.md
  size-company/references/sizing-lore.md   # interpretation knowledge (shared)
  update-all/SKILL.md
  report-company/SKILL.md
  report-all/SKILL.md
  verify-completion/SKILL.md
  deep-verify/SKILL.md                     # stream-measure every archive on an in-region VM
  daily-brief/SKILL.md
  slack-kickoff/SKILL.md                   # one private draft per service thread (connector-only)
  slack-kickoff/references/read-protocol.md  # the shared connector→snapshot read steps
  slack-inbox/SKILL.md                     # what needs a reply + draft it (per company or --all)
  harvest-voice/SKILL.md                   # build/refresh the user's sent-message voice store
  mint-sas/SKILL.md                        # client push SAS (racwl, 14-day default) + ledger + tokens page
  gcs-azure-transfer/SKILL.md              # GCS export → <slug>-raw via transfer VM
  gcs-azure-transfer/references/commands.md
  dropbox-azure-transfer/SKILL.md          # Dropbox → <slug>-raw (same engine)
  dropbox-azure-transfer/references/commands.md
  gdrive-azure-transfer/SKILL.md           # Google Drive → <slug>-raw (same engine)
  gdrive-azure-transfer/references/commands.md
  s3-azure-transfer/SKILL.md               # AWS S3 → <slug>-raw (engine VM lifecycle, azcopy server-side copy)
  s3-azure-transfer/references/commands.md
  github-azure-transfer/SKILL.md           # GitHub org → <slug>-raw (engine VM lifecycle, VM-side git+API puller)
  github-azure-transfer/references/commands.md
  zoho-azure-transfer/SKILL.md             # Zoho CRM/Learn/WorkDrive → <slug>-raw (engine VM lifecycle, VM-side REST puller)
  zoho-azure-transfer/references/commands.md
  figma-azure-transfer/SKILL.md            # Figma workspace → <slug>-raw (engine VM lifecycle, VM-side REST puller)
  figma-azure-transfer/references/commands.md
  teams-azure-transfer/SKILL.md            # Microsoft Teams messages/metadata → <slug>-raw (engine VM lifecycle, VM-side REST puller)
  teams-azure-transfer/references/commands.md
  slack-azure-transfer/SKILL.md            # Slack export's FILES → <slug>-raw (engine VM lifecycle,
                                           #   source is the export already in the container)
  slack-azure-transfer/references/commands.md
  qwilr-azure-transfer/SKILL.md            # Qwilr REST → <slug>-raw (local, no VM)
  qwilr-azure-transfer/references/commands.md
  vimeo-azure-transfer/SKILL.md            # Vimeo API → <slug>-raw (local, Azure server-side copy)
  vimeo-azure-transfer/references/commands.md
  zoom-azure-transfer/SKILL.md             # Zoom S2S API → <slug>-raw (local, Azure server-side copy)
  zoom-azure-transfer/references/commands.md
scripts/                     # the deterministic layer (python3, stdlib only)
  common.py                  # paths, az runner, JSON IO, time, units
  phases.py                  # shared sizing phases: skip/launch/poll/harvest/cleanup
  reconcile.py               # declared-vs-actual math: %, deltas, ETA, flags, lore notes
  corpus_sizer_rest.py       # portable stdlib+SAS sizer; runs locally, detached
  discover_company.py        # az discovery for onboarding → config.json
  mint_push_sas.py           # client push SAS: account-key racwl mint (14-day
                             # default), .sas-ledger.json, reports/sas-tokens.html;
                             # the zip bundles sas-credentials.txt + client_push.sh
  templates/client_push.sh   # the client's azcopy upload helper, shipped inside every
                             # push zip. VENDORED byte-identical from cdp-platform
                             # app/templates/client_push.sh (the source of truth) —
                             # re-copy, never edit here. It parses the credentials
                             # file's "SAS URL:" line + the next line, so that field
                             # is a contract, not prose
  offboard_company.py        # offboard/restore/list: move a company to/from
                             #   companies/.archive/ (local only; Azure untouched)
  size_company.py            # single-company sizing CLI (fleet of one)
  fleet_size.py              # fleet sizing CLI (launch-all / poll-all / harvest)
  deep_verify.py             # deep-verify step machine: sizer w/ DEEP_VERIFY=1 on an
                             # in-region VM (engine lifecycle), auto-teardown at harvest
  transfer_engine.py         # cloud→Azure transfer engine (VM, rclone, tmux)
  gcs_transfer.py            # thin GCS CLI over transfer_engine (Spec only)
  dropbox_transfer.py        # thin Dropbox CLI over transfer_engine (Spec only)
  gdrive_transfer.py         # thin Google Drive CLI over transfer_engine (Spec only)
  s3_transfer.py             # S3 → blob via azcopy server-side copy on the VM; engine lifecycle + own copy layer
  s3_flat.py                 # VM-side flat-bucket engine: sharded listing, chunk split, manifest verify (pushed with the runner)
  azcopy-runner.sh           # VM-side azcopy job-queue worker (ssh-piped by s3_transfer.py)
  github_transfer.py         # GitHub → blob via VM mirror-clone + azcopy; engine lifecycle + own pull layer
  github_vm_pull.py          # VM-side puller: mirror+wiki clones, 4 JSONL exports, LFS, stage-then-azcopy (pushed like s3_flat.py)
  zoho_transfer.py           # Zoho (CRM/Learn/WorkDrive behind one --product dimension) → blob
                             #   via VM REST puller + azcopy; engine lifecycle + own pull layer
  zoho_vm_pull.py            # VM-side puller: CRM JSON ledger + Bulk Read ZIPs + attachments,
                             #   Learn courses/KB, WorkDrive (pushed like github_vm_pull.py)
  figma_transfer.py          # Figma workspace → blob via VM REST puller + azcopy;
                             #   engine lifecycle + own pull layer
  figma_vm_pull.py           # VM-side puller: file JSON + comments/versions + image
                             #   fills + page renders + team libraries (pushed like zoho_vm_pull.py)
  teams_transfer.py          # Microsoft Teams → blob via VM REST puller + azcopy;
                             #   engine lifecycle + own pull layer
  teams_vm_pull.py           # VM-side puller: team/channel/user/membership JSONL,
                             #   per-channel messages+replies+hostedContents (pushed like figma_vm_pull.py)
  slack_transfer.py          # Slack export → blob: finds the export INSIDE <slug>-raw, then
                             #   engine VM lifecycle + Azure server-side-copy of every file link
  slack_vm_pull.py           # VM-side puller: ledger (files-index/objects/shares/external/
                             #   unavailable JSONL) + Put Blob-From-URL copy (pushed like
                             #   teams_vm_pull.py)
  saxon_sp_complete.py       # ONE-OFF (saxon only, slug-guarded): SharePoint completion CLI —
                             #   engine VM lifecycle + mapping-approval gate + laptop plan/harvest/verify
  saxon_sp_vm_pull.py        # VM-side one-off: Graph delta walk → per-file diff → create-only
                             #   server-side copy of MISSING files into the client's own sharepoint/
                             #   prefix (calibration-gated; pushed like teams_vm_pull.py)
  helpsy_url_pull.py         # ONE-OFF (helpsy only, slug-guarded): copies an S3
                             #   corpus from two client-supplied URL LISTS already
                             #   sitting in the container; laptop-local, Put Blob
                             #   From URL, no VM
  wallaroo_takeout_pull.py   # ONE-OFF (wallaroo-media only, slug-guarded): Google
                             #   Takeout DOWNLOAD LINKS → blob; engine VM lifecycle +
                             #   cURL parsing, probe, laptop-side verify
  wallaroo_takeout_vm_pull.py # VM-side one-off: resumable urllib download of each
                             #   archive part → size check → azcopy → delete local
                             #   (pushed like saxon_sp_vm_pull.py)
  qwilr_transfer.py          # Qwilr REST → blob REST ingest; local, standalone
  qwilr_csv_pull.py          # Qwilr support-CSV → blob ingest (API-less fallback):
                             #   fetches each page's public/collaborator HTML +
                             #   triggers Qwilr's async PDF renders; local, standalone
  vimeo_transfer.py          # Vimeo → blob via Put-Block-From-URL server-side copy; local, standalone
  zoom_transfer.py           # Zoom recordings → blob via Put-Block-From-URL server-side copy; local, standalone
  bootstrap-vm.sh            # transfer-VM bootstrap (rclone+azcopy+tmux), ssh-piped
  slack_engine.py            # Slack engine (connector-only, snapshot-driven): registration,
                             #   snapshot ingest, parent discovery, manifest reconcile,
                             #   kickoff plan, draft receipts, inbox, voice store; never
                             #   talks to Slack — see docs/superpowers/specs/2026-09-01-slack-engine-design.md
  import_slack_operator.py   # ONE-OFF: seed knowledge/kickoff-copy/ from the
                             #   corpus-transfer-slack-operator plugin catalog + import
                             #   its live registrations into companies/<slug>/slack/
  gen_report.py              # per-company HTML report
  gen_dashboard.py           # fleet index.html
  verify_completion.py       # completion checklist
knowledge/
  kickoff-copy/              # COMMITTED kickoff library: one JSON per service
                             #   (aliases, direction, message, status, notes);
                             #   onboarding.json + progress-check.json are the fixed kinds
reports/
  index.html                 # generated fleet dashboard (my working view)
  sas-tokens.html            # generated push-SAS tokens page (mint_push_sas.py page)
tests/
  fixtures/companies/democo/ # fake company for offline validation
  fixtures/slack-export-mini.zip        # SYNTHETIC Slack export (schema, never client data);
  fixtures/make_slack_export_mini.py    #   regenerate with the generator beside it
  fixtures/slack/            # synthetic channel snapshot for the Slack engine tests
  test_harness.py            # runs all offline validation; python3 tests/test_harness.py
  test_slack_engine.py       # Slack engine unit tests (also run by test_harness.py)
  test_import_slack_operator.py  # one-off seed/import tests (also run by test_harness.py)
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
                                         // reconciliation, but listed + flagged in reports.
                                         // Charts a single ACTUAL bar (no declared bar —
                                         // there is no byte declaration to compare against)
                                         // with the count as the declared-side label.
    "gusto":   {"records": 4, "record_unit": "W2 employees"},
                                         // optional "record_unit": the count's OWN unit when
                                         // it isn't "records" (W2 employees, transactions,
                                         // seats). Rendered verbatim in the table and chart
                                         // label; absent = the plain "N records" default.
                                         // A count is only honest with its unit attached —
                                         // "4 records" for 4 W2 employees misstates the
                                         // declaration in a client-facing report.
    "zendesk": {"bytes": 0},             // declared 0 B: flagged if data actually appears
    "workspace": {"bytes": 1, "prefix": "workspace-export"},
                                         // optional "prefix" (string or list) pins a
                                         // declaration to actual top-level prefix(es) when
                                         // the manifest name doesn't match what the client
                                         // pushed (e.g. a merged Workspace export). Without
                                         // it, matching is by normalized name and sums ALL
                                         // case/punctuation variants (zoom/ + Zoom/).
    "bigquery": {"bytes": 10300000000000, "prefix": "google_bigquery",
                 "equivalent_bytes": 9100000000000,
                 "equivalent_note": "decoded-sample basis ..."}
                                         // optional "equivalent_bytes" (+ mandatory
                                         // "equivalent_note" naming the measurement basis):
                                         // the ACTUAL data re-expressed in the manifest's
                                         // own unit when the two are known to differ —
                                         // healthtap's bigquery: the sizer counts parquet
                                         // decompressed PAGE bytes (dict/RLE encodings
                                         // intact) but the client declared BigQuery console
                                         // LOGICAL bytes (fully decoded); a decoded-sample
                                         // measurement bridges them. The service's % then
                                         // compares like for like, the row keeps the
                                         // measured actual_bytes, gets flagged
                                         // "unit-adjusted", and the note ALWAYS renders
                                         // (reconcile.equivalent_unit_notes — the
                                         // duplicate_prefixes never-silent discipline).
                                         // The headline is published TWICE: pct_complete /
                                         // uncompressed_total stay the pure measurement (run
                                         // totals), and pct_complete_adjusted /
                                         // uncompressed_total_adjusted add
                                         // (equivalent_bytes - actual_bytes) so the headline
                                         // agrees with the row. gen_report renders both in the
                                         // KPI card ("unit-adjusted", with the raw % beneath);
                                         // gen_dashboard uses the adjusted figure and marks the
                                         // row "unit-adj". BEFORE 2026-08-30 the headline used
                                         // run totals ONLY, which let checkmate show BigQuery at
                                         // 119% in its row while the headline counted the same
                                         // service 37.39 TB lower (66.0% vs 94.7%) with the gap
                                         // stated nowhere. ONLY set from a real measurement,
                                         // never a client assertion.
  },
  "source_split": ["gdrive-export2"],    // optional: top-level prefixes whose SECOND level
                                         // holds the real services (swiftlaw pushed every
                                         // service inside one gdrive-export2/ folder).
                                         // reconcile.effective_sources() replaces each
                                         // listed prefix with its sources_l2 children, keyed
                                         // "parent/child"; declarations then match by leaf
                                         // ("Notion" finds "gdrive-export2/Notion") or by an
                                         // explicit "prefix" (full path or leaf). Bytes the
                                         // children don't cover land in "parent/(unaccounted)"
                                         // — sources_l2 is a top-40 rollup, so a wide export
                                         // can be truncated and must not vanish silently.
                                         // Headline % is unaffected: it uses run totals.
                                         // ALSO the shape when ONE ingest writes several
                                         // declared services under one prefix: the zoho
                                         // ingest lands zoho-export/{crm,learn,workdrive},
                                         // so source_split ["zoho-export"] plus a
                                         // "prefix": "zoho-export/crm" pin per service is
                                         // what makes the three declarations reconcile
                                         // separately (a product never run reads as
                                         // declared-empty, which is expected, not a fault).
  "duplicate_prefixes": ["MarketingTeam", {"prefix": "onedrive",
                        "redundant_bytes": 134100000000}],
                                         // optional: top-level prefixes that are
                                         // redundant COPIES of data already counted under
                                         // another prefix (saxon pushed 76 root folders that
                                         // are byte-exact copies of sharepoint/ children).
                                         // A bare string = the whole prefix is redundant and
                                         // is dropped from the per-source view; an object with
                                         // "redundant_bytes" = only that many bytes duplicate
                                         // (a partial/aborted export still holding unique
                                         // data — that prefix STAYS in the per-source view,
                                         // showing only its unique bytes: apply_duplicates()
                                         // subtracts the redundant share, so per-source rows
                                         // and the chart match the deduplicated headline;
                                         // affected service rows carry a "deduplicated" flag).
                                         // reconcile.duplicate_sources() subtracts these from
                                         // the headline total and %-complete, and always
                                         // emits a note so the subtraction is never silent.
                                         // ONLY list prefixes VERIFIED against the container
                                         // (delimiter listing), never ones a client asserted.
                                         // Matching runs on the POST-SPLIT per-source view:
                                         // with source_split, declare duplicates at their
                                         // "parent/child" keys (swiftlaw's redundant copies
                                         // live under gdrive-export2/'s second level).
  "excluded_prefixes": [{"prefix": "2026",
                        "reason": "blob inventory policy output"}],
                                         // optional: top-level prefixes that are NON-CORPUS
                                         // operational data (e.g. a portal-configured blob
                                         // inventory policy writing daily reports into the
                                         // container — bacancy, 2026-08). Bare string or
                                         // {"prefix", "reason"}. The whole prefix is dropped
                                         // from the headline total, %-complete, and
                                         // per-source rows; reconcile.excluded_sources()
                                         // always emits a note so it's never silent. Distinct
                                         // from duplicate_prefixes: that is for redundant
                                         // copies of REAL corpus data; this is for writes
                                         // that were never part of the corpus at all.
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
  "methods": {"zip": 500, "gz": 6, "stored": 300}, // per sizing KIND blob counts (zip/gz/
                                         // bz2/xz/parquet/stored — bz2 and xz are kinds in BOTH modes so
                                         // deep-verify cache rows replay on shallow runs; shallow
                                         // sizes them at content-length, zero HTTP; parquet is
                                         // footer-sized in both modes — thrift FileMetaData via
                                         // ranged GETs, the zip-CD play: decompressed PAGE bytes,
                                         // permanently "trusted" in deep coverage since the stdlib
                                         // has no snappy codec to stream the pages) —
                                         // gz>0 triggers the gz-accuracy notes; the per-blob gz
                                         // method taxonomy (gz-trailer/gz-floor/gz-bad-trailer/
                                         // gz-tiny/gz-exact/gz-truncated) lives in the
                                         // blob-index/TSV, rolled up in "gz" below
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
  "verification": {                      // present ONLY on deep-verify runs (scripts/
    "deep": true,                        // deep_verify.py); null/absent on shallow + old runs.
    "measured_blobs": 9812,              // measured = stream-decompressed or trivially exact
    "measured_bytes": 3298000000000,     // (uncompressed bytes — the commercial column);
    "trusted_blobs": 3,                  // trusted = still metadata (zip CDs for unstreamable
    "trusted_bytes": 1200000000,         // entries, stream failures, err:* floors);
    "unmeasurable_blobs": 2,             // unmeasurable = no stdlib codec (.7z/.rar/.zst),
    "unmeasurable_bytes": 40000000000,   // counted at stored size, broken out per-format:
    "unmeasurable_by_format": {".7z": [2, 40000000000]},   // ext → [blobs, stored bytes]
    "cd_mismatches": 1,                  // zips whose CD lied — the STREAMED value is in the
                                         // totals (silent by policy; per-blob detail in the
                                         // blob-index method zip-exact-mismatch(cd=N))
    "stream_compressed_bytes": 3100000000000  // per-run egress ledger (successful streams)
  },                                     // copied-forward carries it (container unchanged ⇒
                                         // certification holds); verify_completion surfaces it
                                         // as an INFORMATIONAL check — it never gates
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
  "last_change_detected_at": "2026-08-12T14:00:00Z", // last time total bytes grew
  "offboarded_at": "2026-08-28T14:00:00Z" // OPTIONAL — present only while the company
                                          // sits in companies/.archive/ (stamped by
                                          // offboard_company.py offboard, removed by
                                          // restore); active companies never carry it
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
zip central directories, and gzip trailers — kilobytes per blob — except
budgeted gz exact-streaming: at most `GZ_STREAM_BUDGET` compressed bytes per
run (default 50 GB, 0 disables), each blob paid once, cached by ETag. (The old
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
happen on days webspiders actually pushed. No previous sized run → always
launch. The metric is **account-level** (includes `-scrubbed` etc. — a scrub
run can force one redundant re-size; harmless).

**UsedCapacity alone is NOT a safe change detector, and its staleness is
invisible.** It is a capacity SNAPSHOT that ARM re-emits every hour carrying
the *last known* value, so `metric_at` always reads as current no matter how
old the number is — you cannot detect staleness from the timestamp. Refresh is
best-effort and irregular: helpsy's moved every 2-3 h while oneorb's froze for
8+ h. The failure this produces is silent and self-perpetuating: a frozen value
gets stamped into the run file as `used_capacity_bytes`, and the next run
compares the same frozen value against itself, matches, and copies forward —
indistinguishable from a healthy skip. On 2026-09-01 oneorb's metric froze at
236 MB at 16:00Z; a 670 GB S3 ingest landed at ~19:53Z; the 23:09Z fleet run
skipped it and the daily brief published **2.1% complete for a container
holding 678 GB (196.5%)**.

So a skip now requires the unchanged metric **plus two independent guards**
(`phases.skip_check`); any one failing means launch:

1. **The hard invariant.** UsedCapacity is account-level and the container is a
   subset of the account, so the metric can never honestly sit *below* the
   `totals.compressed_bytes` we ourselves listed in the last `method: "sized"`
   run. If it does, it is stale (or data was deleted) and we must look either
   way. This is free — the number is already in the run file.
2. **Ingress since we last looked.** `Ingress` is a TRANSACTION metric (PT1M
   granularity, emitted as writes happen), so it sees what the capacity
   snapshot misses: on oneorb it recorded 8.37 GB at 15:22Z and 422.8 + 247.8
   GB at 18:22Z/19:22Z while UsedCapacity sat at 0.236 GB. The window starts at
   the last **measured** run (`_last_measured_run`), never the last
   copied-forward one, so a chain of skips cannot slide the window past writes
   nobody ever looked at. Below `INGRESS_FLOOR_BYTES` (1 MB) counts as no
   writes: a genuinely idle account still logs a few KB (monterey-financial,
   19 days at zero blobs, logged 35 KB over 49 h), while active accounts run
   5-6 orders of magnitude above it (bacancy 2.42 GB/25 h, helpsy 12.4 TB/49 h).
   An unreadable Ingress metric fails safe (size).

Both `az monitor metrics list` calls for Ingress pass `--start-time` AND
`--end-time`: with `--end-time` omitted az collapses the whole window into a
single bucket, which would silently zero the sum.

Errors remain in the safe direction — a false "changed" just costs one
listing — but "safe" now means *we looked*, not *the metric agreed with
itself*.

### The vm block is informational

Sizing no longer uses VMs, but discovery still records what it finds (prefer
`verify-vm-*`, else any VM in the RG, else `exists: false`) because it's
useful context — most companies have NO VM at all (am-city-inc's RG holds
only the storage account), some have `vm-*-extract` / `vm-dwt-transfer`
leftovers. `vm.exists: false` is normal and blocks nothing.

### Deep verify (the sanctioned sizing-VM path)

`scripts/deep_verify.py` (driven by the deep-verify skill) is the ONE
sizing operation that uses a VM: it runs the same `corpus_sizer_rest.py`
with `DEEP_VERIFY=1` on a temporary in-region VM (`deepv-<slug>`), which
stream-decompresses EVERY compressed blob (zip/gz/bz2/xz) so the totals are
measurements, not zip-CD/gz-trailer trust. This is a genuine bulk-download
job — exactly the case the local-only rule's rationale carves out — so the
in-region rule applies again: free egress, ~4–5 TB/h. It borrows rules from
BOTH families and they must not cross-contaminate further:

- From **sizing**: the SAS stays account-level **`rl` read-only** (1-day
  default; `--sas-days 2` allowed for >1-day streams). Deep verify never
  gains a write path; the vnet-rule grant is account-config plumbing (same
  sanction as the transfer engines), not a data write.
- From the **transfer engines** (`transfer_engine.py` reused verbatim):
  VM lifecycle, `allow-network` (service endpoint + vnet-rule — IP rules
  never match same-region VMs), secrets over ssh stdin into a 600 env file,
  **no state file** (VM + tags are the truth; `.fleet-state.json` is never
  touched), teardown removes exactly our rule.
- Lifecycle is **one-shot**: `step <slug>` advances one phase per call
  (create → grant+push+launch → poll → harvest + auto-teardown); the
  pre-run UsedCapacity metric rides VM tags so harvest can stamp the run
  file. Results land as a normal `sizing-runs/` file (method `sized`) with
  the `verification` block, and the pulled blob-index makes every
  measurement replay in later shallow daily runs (exact methods are
  terminal in the cache — dailies never "re-shallow" them; a repeat deep
  run on an unchanged container is listing-only).

---

### Cloud → Azure transfers (the ingest path)

Some companies' corpora arrive in a cloud source we must pull ourselves:
Google Workspace Data Export buckets (`dwt-takeout-export-<digits>`, browser
OAuth by the customer's super admin only — no service accounts, no HMAC
keys), a Dropbox account, a Google Drive (My Drive or Shared Drive), or an
AWS S3 bucket. A temporary Azure VM is the viable path for all of them.
`scripts/transfer_engine.py` is the ONE engine; `gcs_transfer.py` /
`dropbox_transfer.py` / `gdrive_transfer.py` are thin Spec-only CLIs over
it (rclone-with-a-token copies), and `s3_transfer.py` / `github_transfer.py`
reuse the engine's VM lifecycle but swap the copy layer (azcopy
server-side copy for S3, a VM-side git+API puller for GitHub — see
below) — all driven by the matching `*-azure-transfer` skills. The
workflow: VM `xfer-<slug>` (Dropbox: `xfer-dbx-<slug>`, Drive:
`xfer-gdr-<slug>`, S3: `xfer-s3-<slug>`, GitHub: `xfer-gh-<slug>`,
Zoho: `xfer-zoho-<slug>`, Figma: `xfer-figma-<slug>`, Teams:
`xfer-teams-<slug>`, so
they can run concurrently) in the company's RG and
the SA's region, static Standard-SKU public IP (never deallocated before
teardown), the copy in a tmux session into `<slug>-raw/workspace-export/`
(Dropbox: `dropbox-export/`, Drive: `gdrive-export/`, S3: `s3-export/`,
GitHub: `github-export/`, Zoho: `zoho-export/<product>/`, Figma:
`figma-export/`, Teams: `teams-export/`, Slack: `slack-export-files/`).
Slack is the odd one out and worth flagging here: its VM is
`xfer-slack-<slug>` and its SOURCE is the client's own Slack export already
sitting in `<slug>-raw`, so that container is both the read side and the
write side of the same job (see the Slack paragraph below).
Rules that differ from the sizing path — do not cross-contaminate:

- **Network rules are engine-run via `allow-network`** (policy change,
  2026-08: the harness grants access itself — the Microsoft.Storage service
  endpoint on the VM's subnet + a vnet-rule on the SA; IP rules never match
  same-region VM traffic). Teardown removes exactly the rule we added;
  pre-existing rules (the client's own push path) are never touched. Caveat:
  company infrastructure may strip rules not added through the internal UI —
  if a 403 reappears mid-transfer, re-run `allow-network`.
  `phases.ip_rule_ensure` belongs to the laptop-origin paths only
  (sizing, and the local qwilr/vimeo/zoom pulls below).
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

**S3 rides the engine's VM but not its rclone.** An S3 corpus can be
hundreds of millions of small objects (bacancy-scale listing is nothing
next to a 300M-object images bucket), and any streaming copy — rclone
included, whose cross-provider copies always pass through the VM — is
months of wall clock at that shape. `scripts/s3_transfer.py` therefore
reuses transfer_engine's VM lifecycle (create/allow-network/check-azure/
teardown run the engine functions verbatim; `mint_container_sas` is
shared) but copies with **azcopy S3→Blob server-to-server** (presigned S3
GETs driving Put Blob/Block From URL — the storage fabric pulls from S3
directly; the VM issues only control calls; days, not months). The bucket
is split into per-prefix azcopy jobs (`plan-jobs`; one job's plan file at
300M objects would be 100+ GB, hence also the 512 GB OS disk via the
Spec's `default_os_disk_gb`) drained by K tmux worker windows running
`scripts/azcopy-runner.sh`; loose objects at split levels get shallow
rclone jobs so nothing is orphaned. The read-only AWS key (client-made
IAM user: `s3:GetObject` + `s3:ListBucket` on the bucket) arrives as 2
stdin lines → 600 env file on the VM; rclone's `[s3]` remote is
secretless (`env_auth = true`, same env file) and serves listing/probe/
verify. `probe` is the day-one gate: requester-pays detection and a
storage-class sample — GLACIER/DEEP_ARCHIVE objects fail server-side
copy (`InvalidObjectState`) until restored. Write invariant, stated
honestly: no-delete is server-enforced (racwl SAS); no-overwrite is the
runner's pinned `--overwrite=false` (client-side, NOT the API-enforced
`If-None-Match: *` of the local pulls). Verify is a per-job count+byte
rollup (S3 multipart ETags aren't MD5s — size-match is the honest
invariant); resume = re-run transfer (skips are normal). Always pilot
one job (`transfer --pilot`) to measure objects/s before quoting a
timeline. **FLAT buckets** (all keys at root, no prefixes — e.g. a
300M-hex-hash images bucket) can't be prefix-split: `plan-jobs`
auto-detects the shape and switches to `scripts/s3_flat.py`'s chunked
mode — a range-sharded parallel listing (boto3 on the VM, detached in
tmux; the laptop side stays stdlib) becomes the **cutoff manifest**,
split into ~250k-key `key\tsize` chunk files that s3_flat.py copies
itself via **Put Blob From URL** from locally presigned S3 GETs (`L`
jobs — the vimeo/zoom transport; manifest sizes mean zero per-object
HEADs, and `If-None-Match: *` makes create-only API-enforced on this
path; azcopy's `--list-of-files` was measured at ~15 obj/s sequential
enumeration and is not used for flat chunks). Unsafe-named keys ride
rclone `--files-from` as `Q` jobs. Flat verify is a streamed
merge-join of that manifest against the Azure dest listing —
per-object name+size, drift-immune, `--deep` implied. Driven by the
`s3-azure-transfer` skill.

**GitHub rides the engine's VM with its own pull layer.** rclone has no
GitHub backend, so `scripts/github_transfer.py` reuses the engine's VM
lifecycle (like s3) and pushes `scripts/github_vm_pull.py` to the VM
(a harness-idiom rewrite of cdp-platform's `github_pull.py` @ dc320d1 —
see `docs/github-transfer-handoff.md`), which pulls everything VM-side
in tmux: per repo a `git clone --mirror`, the `.wiki.git` clone when a
wiki exists (absent wiki = skip, not failure), `git lfs fetch --all`
when `.gitattributes` declares `filter=lfs` (bootstrap installs
git+git-lfs), and 4 paginated JSONL exports (issues, pulls,
issue_comments, review_comments; 404 = feature disabled = skip; 403
with the rate limit INTACT = fatal scope error, never retried), staged
under `~/xfer-gh/dest/` (512 GB OS disk — staging holds the whole
corpus) then azcopied to `github-export/` with `--overwrite=false`
(client-side no-overwrite, the s3-path honesty — not `If-None-Match`).
Auth is a client-made **fine-grained PAT** (Contents/Issues/Pull
requests = read; org resource owner, so an org owner must APPROVE it —
the known day-one stall, gated by the laptop-side `probe`): 1 stdin
line → ssh stdin → 600 env file on the VM → a 0700 GIT_ASKPASS helper,
never a clone URL/argv/tags; client revokes it after verify.
**Upload-what-succeeded**: a pass with failed repos still uploads what
completed (per-repo `.cdp-complete` resume markers; re-run mops up);
manifest.json rides the same azcopy job, and **verify runs on the
laptop** (the VM is normally gone) comparing that manifest against a
dest listing via `phases.ip_rule_ensure` + rl SAS — the one VM-family
verify on the laptop path. Certifies staged→container only; no
source-size claim (git packing ≠ API diskUsage). Out of scope: Actions
logs/artifacts, Packages, Projects, Discussions, release assets. Wikis
ARE in scope. Multi-org = one cycle per org with `--dest-prefix
github-export/<org-login>` (the zoom multi-account convention); pin
`"prefix": "github-export"` on the github service in
`expected-data-sizes.json`. Driven by the `github-azure-transfer` skill.

**Zoho rides the engine's VM with its own REST pull layer.** Zoho's
download endpoints need `Authorization: Zoho-oauthtoken <tok>` and Azure's
`x-ms-copy-source-authorization` only speaks `Bearer` — so the vimeo/zoom
server-side-copy transport is STRUCTURALLY unavailable and CRM attachment
bytes must be staged. `scripts/zoho_transfer.py` therefore reuses the
engine's VM lifecycle (like s3/github) on `xfer-zoho-<slug>` and pushes
`scripts/zoho_vm_pull.py`, the first **multi-product** ingest: one skill,
one script, three products behind `--product crm|learn|workdrive` landing
in `zoho-export/{crm,learn,workdrive}/`, one tmux window each (CRM and
Learn hit disjoint APIs and may run concurrently). The `dest_prefix` VM tag
stays the BASE (`zoho-export`) — tagging it `zoho-export/crm` at create
time would silently poison the other two. Auth is a client-made **Self
Client** app: FOUR values on stdin in order — data center (`com`/`eu`/`in`/
`com.au`/`jp`/`ca`/`sa`/`com.cn`), `client_id`, `client_secret`, long-lived
`refresh_token`. **No grant-token exchange on our side** (Zoho grant tokens
expire in 3–10 minutes, so any flow where we do the exchange is a race); a
`TokenBox` on BOTH sides (laptop probe, VM puller — deliberately duplicated,
the github_transfer/github_vm_pull precedent) mints the 1 h access token and
cross-checks the response's `api_domain` against the declared DC. **A wrong
data center is the day-one stall here — the zoho analogue of github's
unapproved PAT** — caught three independent ways: the mint error body, the
`api_domain` cross-check, and a stdin-vs-VM-tag guard at `write-creds`;
Zoho answers OAuth failures with HTTP 200 and an error body, so the error
branch lives in the token acceptor, not an except. CRM's authority is a
**REST JSON ledger per module**: v8's `GET /crm/v8/<Module>` requires an
explicit `fields` param, so `/settings/fields?module=X` is fetched first and
every api_name is requested, paginated by `page_token` at 200/page into
`records.jsonl`; a module too wide for one request has its extra field
chunks re-fetched **per page by record id and merged** — a `page_token` is
never reused across different queries, which is undefined and would silently
drop columns. **Plus** a per-module Bulk Read job whose `{job_id}.zip` lands
as an archival blob and whose CSV row count is a recorded cross-check —
Bulk Read excludes Notes, Attachments, Emails and related/cross modules, so
the JSON is the ledger and the ZIP is the check, never the reverse; its
results expire after a day and downloads cap at 10/min, so an expired result
is re-submitted, not resumed. Also pulled: `/settings/{modules,fields,
layouts}`, users/roles/profiles, Notes, Emails where reachable, and record
Attachments. **Attachments are ONE unit driven by a direct walk of the
`Attachments` MODULE**, not a per-record sweep: the obvious
`/crm/v8/<M>/<id>/Attachments` loop is one call per record (~2.2 calls/s ⇒
~32 h on a 251k-record org, and on song-division 80 sampled records across
the four biggest modules had none at all), whereas `/crm/v8/Attachments`
is directly listable at ~300 rows/s — the whole tenant censused in ~28 s —
and every row carries `Parent_Id.module.api_name` + `Parent_Id.id`, which
is exactly what the per-attachment download URL needs. That endpoint's
`Size` is an **EXACT byte integer** (the docs' "rounded string" describes a
different field), so the attachments unit records `expected_bytes` and
`probe` can give a REAL size census — the one honest pre-run size number
Zoho offers. Files stream to disk in chunks, never `resp.read()`. **Zoho Learn** is
documented only for courses (`learn.zoho.<dc>/learn/api/v1/portal/
<networkurl>/course`, scope `ZohoLearn.course.READ`, `pageIndex` 0-based +
`limit` ≤ 99); the knowledge-base half has **no documented endpoints**, so
the run ATTEMPTS the undocumented sibling paths and records what answered in
`_meta/discovery.json` — Learn KB is discovered, never assumed. **WorkDrive**
is "whatever the token can see": the reachable boundary AND what was
explicitly unreachable go to `_meta/boundary.json`. Pacing: CRM meters on
API credits plus a concurrency ceiling, so pages are always 200 (credits
scale with CALLS), record calls are single-threaded, attachments use ≤5
workers, and every 429 is slept through, never fatal — a retry horizon past
`--rate-sleep-max` is read as the daily credit budget, which is a clock, not
a bug (the pass ends at rc 2 with a manifest naming the wall and cursors make
tomorrow a resumption). **Fatal vs counted keys on the BODY error code, not
the status** — github's 404-vs-403 rule does not port: 401
`OAUTH_SCOPE_MISMATCH` on a required unit (settings, records) is fatal, on an
optional one a recorded skip; 401 `INVALID_TOKEN` re-mints once then dies;
403 `NO_PERMISSION` is the API user's CRM PROFILE, not a scope, and is always
a per-module skip; 404 on an optional related list is "feature not enabled";
HTTP **204 is an empty module, not an error**. Resume is per-unit
`.cdp-complete` markers **plus cursors** — a CRM module is millions of
records, so github's delete-and-re-clone is unacceptable:
`.cdp-cursor.json` carries `{page_token, bytes, records, fields_sha}`, a
resume truncates the JSONL to the last whole page, and a `fields_sha` change
invalidates the cursor and re-walks (a ledger with different schemas per
range is worse than one re-walk); attachments cursor on an index into the
STAGED ledger so their order is deterministic. Units upload **as they
complete** (`--overwrite=false`, client-side no-overwrite — the s3/github
honesty, NOT `If-None-Match`), so a mid-run VM loss costs one unit rather
than the pass, and **verify runs on the LAPTOP** against the uploaded
per-product `manifest.json` via `phases.ip_rule_ensure` + an `rl` SAS,
certifying staged→container only. No source-size claim is made for the
RECORD ledger (Zoho publishes no record byte size, so `probe` gives counts,
never record bytes) — but attachment bytes ARE exact and are reported. The
pre-run census comes from `GET /crm/v8/<Module>/actions/count`, **not
COQL**: COQL makes `where` mandatory and additionally demands a `group by`
for `count(id)`, so a plain per-module count is impossible through it. All three products share
one dest prefix, so `expected-data-sizes.json` needs
`"source_split": ["zoho-export"]` plus a `"prefix": "zoho-export/<product>"`
pin per declared service. Out of scope: Books, Desk, People, Mail, Projects,
Campaigns, Analytics (separate products, separate APIs), and CRM sandboxes,
recycle bin, audit log and territory/portal config. Driven by the
`zoho-azure-transfer` skill.

**Figma rides the engine's VM with its own REST pull layer.** Figma has no
rclone backend and **no bulk export**: the REST API serves file JSON,
comments, versions, presigned asset URLs and published library metadata one
file at a time, behind the post-Nov-2025 per-user rate tiers whose
**Tier-1 bucket (file JSON, node JSON and image renders SHARE it) caps at
20/min even on Enterprise** — a real workspace is a multi-hour-to-multi-day
metered walk, which is what forces the VM+tmux shape (and there is **no
`.fig` source-file endpoint at all**, so the corpus is a *derivative*, not
a restorable backup — said out loud in the skill). `scripts/figma_transfer.py`
reuses the engine's VM lifecycle on `xfer-figma-<slug>` and pushes
`scripts/figma_vm_pull.py`, which stages under `figma-export/`: a `meta`
discovery ledger (the one REQUIRED unit — `/v2/teams/:id/folders` recursed
through nested subfolders to `files?branch_data=true`, with the deprecated
v1 projects pair as a recorded fallback), one cursored `library/<team>`
unit per team (published components/styles, Tier-3, `page_size` 1000), and
**one unit per FILE** — document JSON (an oversized file that 400s "try a
smaller request" is auto-**decomposed** into depth-1 + per-node JSON,
recorded, informational), comments, the versions LIST (pagination followed
by `next_page` URL, host-checked), the embedded **image fills** (presigned
CDN URLs that expire in **≤14 days**, so the URL map is always re-fetched
fresh and never cursored; downloads are a separate CDN pool that NEVER
carries the token), one **PNG render per page** (batched ids per Tier-1
call; a 200 can carry null per-node values — retried once, then recorded),
and branch node-trees inside the parent unit. The file KEY leads the unit
path and folder paths stay out of it (names and folders are mutable;
renames must not orphan markers). Pacing is **proactive** — a TierBucket
paces to 0.9× the documented per-plan caps and 429's Retry-After is the
backstop — because Figma publishes the numbers; there is no daily credit
clock, the per-minute limit IS the wall clock. `classify()` keys on
**status + endpoint family, the inverse of zoho's body-code-first rule**
(Figma has no body codes); 403 and 404 are deliberately identical
(`no-access-or-missing` — Figma does not disambiguate), and a dead PAT is
**403, not 401**. Auth is a client-made **personal access token** from a
**Full/Dev seat** (1 stdin line → 600 env file; a View/Collab seat's token
gets 6 Tier-1 calls per MONTH — the wrong-seat day-one stall, caught by
`probe` via `X-Figma-Rate-Limit-Type: low`); the second day-one stall is
**team visibility**: there is NO team-listing API, the client supplies
every team id from `figma.com/files/team/<ID>/...`, and a forgotten team
is silently invisible — probe's census is read back to the client.
`probe` reports counts and a Tier-1 wall-clock estimate, **never bytes**
(Figma publishes no sizes anywhere; the manifest's `total_staged_bytes` is
the engagement's first real byte number). Per-unit upload as units
complete (`--overwrite=false`, run metadata `--overwrite=true` — the
github pilot-poisons-verify fix shipped from day one), **verify on the
LAPTOP** via `phases.ip_rule_ensure` + `rl` SAS against the uploaded
manifest, staged→container only. `expected-data-sizes.json` takes a plain
`"figma": {"bytes": N, "prefix": "figma-export"}` pin — no `source_split`.
Out of scope: `.fig` files, comment reactions (one Tier-2 call per
comment), per-version file JSON, variables (Enterprise-full-seat-gated),
Dev Mode resources, webhooks, analytics. Driven by the
`figma-azure-transfer` skill.

**Teams rides the engine's VM with its own REST pull layer.** Microsoft
Teams has no rclone backend and **no bulk export**: the corpus is every
team's every channel's every message thread plus replies, walked page by
page against Graph, an app-only client-credentials pull (no signed-in user,
no delegated/browser flow anywhere) that is genuinely a multi-hour-to-
multi-day job for a real tenant — the github/zoho/figma precedent.
`scripts/teams_transfer.py` reuses the engine's VM lifecycle on
`xfer-teams-<slug>` and pushes `scripts/teams_vm_pull.py`, which stages
under `teams-export/`: a guid-led layout with a `_meta/` discovery ledger
(teams/channels/users/membership JSONL + `name-map.json` + the run
`manifest.json` — the one REQUIRED unit) and one unit per channel at
`teams/<team-guid>/<channel-id>/messages.jsonl` + a `hosted/` dir. **The
sharepoint boundary is the point of this design:** a channel's file tab,
and any attachment object referenced from a message, is backed by that
team's SharePoint document library, not Teams storage, so those bytes
belong to a future sharepoint pull, never this one — the boundary that
kills saxon-style per-account Teams duplication. Only `hostedContents`
(inline images/files embedded in a message's HTML body, fetched
Bearer-only on the VM) are staged as bytes; attachment objects stay
references in the message JSON, never fetched. A `messages.jsonl` line is
ALWAYS a complete thread — replies are paged to completion before a root
message is ever written. The day-one stall is Microsoft's own
message-content gate, not a bad token or wrong seat: `probe` mints a token
and tries exactly one message page before any VM exists, classifying the
answer as `open` (200), `metered-model-required` (402 — the tenant needs
an Azure subscription linked for Teams' metered API billing) or
`protected-api-approval-missing` (403 — the tenant hasn't been approved
for Microsoft's protected Teams messaging APIs,
aka.ms/GraphTeamsProtectedApis, a days-to-weeks client process, not a
retry); a second stall is missing admin consent on the app registration's
application permissions, caught as an immediate `/groups` 403. Auth is a
client-made Entra ID (Azure AD) app registration, application (not
delegated) permissions, admin-consented — THREE secrets on stdin, in
order: tenant id, client id, client secret; a `TokenBox` on both sides
(laptop probe, VM puller — the github/zoho deliberate duplication) mints
the app-only token, and `write-creds` refuses a stdin tenant that
disagrees with `--tenant-id` (or the VM's `teams_tenant_id` tag) — the
zoho wrong-DC guard adapted to Teams. `classify()` keys on status +
endpoint family (figma's rule): the `_meta` (required) unit is fatal on
any refusal, the `messages` family is fatal on 402/403 (the day-one stall,
not a per-unit quirk), a single team/channel's 403/404 is a recorded skip
(archived team, deleted channel), 429 always sleeps, 401 always re-mints.
Pacing is proactive (`PaceBucket`, default ~4 req/s for the messages
family, ~10 req/s for directory reads) with the 429 Retry-After as
backstop — there is no daily credit clock the way Zoho has one; the
per-minute pace IS the wall clock. `probe` reports team/channel/user
counts and a sampled wall-clock estimate, **never bytes** — Graph
publishes no message or attachment byte size anywhere, so the manifest's
`total_staged_bytes` is the engagement's first real byte number. Resume is
per-channel `.cdp-complete` markers plus `.cdp-cursor.json` cursors
(`next_link` + line count): a torn trailing JSONL line is truncated back
to the last whole page, and a cursor whose `next_link` now refuses is not
trusted forward — that channel is cleared and re-walked from scratch
(cheap; channels are small, unlike a CRM module). Units upload as they
complete (`--overwrite=false`, the s3/github/zoho/figma honesty), and
`_meta/manifest.json` rides `--overwrite=true` so a `--limit-teams` pilot
can never poison verify (the github pilot-poisons-verify fix, inherited
from day one). **DEST_URL is the bare container URL, not pre-prefixed**
like figma/zoho's — `teams_vm_pull.py` appends `DEST_PREFIX` itself, so
the prefix rides the env file as a real, consumed setting instead of being
baked into the URL. Verify runs on the **LAPTOP** via
`phases.ip_rule_ensure` + a 1-day `rl` account SAS against the uploaded
`_meta/manifest.json`, certifying staged→container only; no source-size
claim is made. `expected-data-sizes.json` takes a plain `"microsoft_teams":
{"prefix": "teams-export"}` pin — no `source_split`. Out of scope: 1:1,
group and meeting chats (no `Chat.Read.All`; a `TeamsMessagesData`
export-mailbox fallback is a recorded future option, not built here),
calendars, mail, tabs/apps, Planner, Wiki, meeting recordings
(doc-library files), document-library files generally, and attachment
bytes. Driven by the `teams-azure-transfer` skill.

**Slack is the ingest whose source is already in the destination.** A
Business+ / Enterprise compliance export is a complete transcript and ZERO
file bytes: every attachment, image, video, canvas and huddle transcript
survives only as an authenticated `files.slack.com` link inside the JSON.
Measured on a real 2.35 GB export (123k day files, 1.1M messages): **63,636
unique files, 62,056 of them Slack-hosted and worth 81.1 GB — about 34x the
export itself** (996 more are Drive links Slack holds no bytes for, 584 are
deleted or retention-aged), so a company
whose Slack "landed" has in fact delivered the index and none of the
content. `scripts/slack_transfer.py` reuses the engine's VM lifecycle on
`xfer-slack-<slug>` and pushes `scripts/slack_vm_pull.py`, which reads the
export **out of `<slug>-raw`** and writes the files back into the same
container under `slack-export-files/`. Four things differ from every sibling.
**(1) Discovery is a real step**: `discover-export` lists the container over
the READ (`rl`) SAS and recognises a `.zip` blob (confirmed by two ranged
GETs of its central directory — never a download) or an already-extracted
tree (a `channels.json` + `users.json` pair); zero or several candidates is
a QUESTION for the user, never a guess, because the wrong archive builds a
ledger for the wrong workspace — several usually means a two-phase export
(public channels first, private/DMs later), which is two runs into two
`--dest-prefix` values, never merged. The choice rides one `slack_export`
VM tag as `zip:<blob>` / `tree:<prefix>`; no state file. **(2) The export
carries its own auth** — every URL embeds one workspace-wide `xoxe-` token,
so there is normally NO client credential to ask for; `write-creds` is an
override (one stdin line, a `files:read` `xoxp-`/`xoxb-` token used as
`Authorization: Bearer`, which Azure's `x-ms-copy-source-authorization`
also speaks) for an export whose links were stripped or expired. That
expiry IS the day-one stall — export links die WITH the export and cannot
be refreshed, so a dead token means a FRESH export (days of client work),
which is why `probe` reads the export over the `rl` SAS and makes exactly
one live Slack request before any VM is billed, classifying `link_gate` as
open / token-expired / no-file-links / files-missing / no-range-support /
redirect-unresolvable. **(3) Transport is Azure server-side copy** (the
vimeo/zoom transport, NOT the stage-then-azcopy of github/zoho/figma/teams):
the VM resolves each redirect chain itself and hands the final signed URL to
Put Blob From URL (or Put Block From URL + Put Block List above 256 MiB,
re-resolving on expiry), so file bytes never touch VM disk and create-only
is API-enforced by `If-None-Match: *`; a per-file stream-through fallback
covers what Azure will not copy. This family uses **no azcopy at all** — it
is REST end to end. `classify()` keys on the copy-SOURCE status
(`x-ms-copy-source-status-code`), the inverse of teams' endpoint-family
rule, because two servers can refuse us: Slack 401/403 is fatal only while
nothing has copied (a dead token) and a per-file skip afterwards, Slack 404
is a file deleted since the export, 429 from either side always sleeps.
**(4) Verify makes a REAL source-truth claim** — the export declares a byte
`size` for every file, so `_meta/objects.jsonl` (one row per blob, streamed
by verify) is compared declared-vs-committed rather than staged→container
only; renditions carry no declared size, so only presence is asserted for
them and the two claims are reported separately, never merged. Ground truth
that drives specific code, all measured on the sample: Slack writes UTF-8
zip member names with the UTF-8 flag bit UNSET (7 of 3,352 conversation dirs
arrive as cp437 mojibake — `zip_member_name` re-decodes); `token=` on
files-pri URLs but `t=` on files-tmb transcodes, and `attachments[].files[]`
entries carry NO token and need one appended (a naive `message.files[]` walk
misses them entirely); root `canvases.json` / `lists.json` /
`huddle_transcripts.json` hold assets no message walk reaches, each with its
own `*_history_download`; and renditions (thumbnails, mp4/hls, vtt) are
409,782 against 62,056 originals — 471,838 objects in all, a 7.6x
multiplier — and are ON by
default behind `--no-renditions` with probe reporting both counts. Blob
paths are **id-led** (`files/<fid[:4]>/<file_id>/<name>`) because file and
channel names are mutable and a two-phase export makes a rename between
passes a live hazard; the ledger is the join table that keeps the corpus
LINKED (`_meta/files-index.jsonl` one row per file with its conversation and
message ts, `_meta/file-shares.jsonl` every additional sighting). Ledger
rows store URLs token-STRIPPED (keeping the parameter name so it can be
re-attached) — the export already holds that credential but writing 470k
copies of it into a new prefix is not hygiene worth shipping. Resume is
stronger than the sibling ingests': the blob's existence is the record
(create-only at the API), shard markers only note which fixed-size slices of
the ledger were walked. Out of scope, recorded not fetched: `mode: external`
(gdrive) files — Slack holds a LINK and no bytes, so no Slack token can ever
retrieve them; they land in `_meta/external-references.jsonl` (996 files /
3.97 GB on the sample) for the gdrive ingest to reconcile against, and that
is a quantified exclusion, never a shortfall. `tombstone` and
`hidden_by_limit` entries have no URL at all and go to
`_meta/unavailable.jsonl` with their reason. `expected-data-sizes.json`
pins BOTH halves: `"slack": {"bytes": N, "prefix": ["<the client's export
prefix>", "slack-export-files"]}` — and remember the container grows by
~34x the export size from OUR writes, not client push, which any report or
nudge must say out loud. Driven by the `slack-azure-transfer` skill.

**The saxon SharePoint completion (one-off, not a family).**
`scripts/saxon_sp_complete.py` + `scripts/saxon_sp_vm_pull.py` finish the
client's OWN partial `sharepoint/` push (saxon only — slug-guarded): the
laptop `plan` step freezes the in-scope folder set (a snapshot of what's
under `sharepoint/`), auto-maps each folder to its Graph site collection
(exact slug → site-id GUID → sanitized display name; anything else is
skip-ambiguous and excluded until a human resolves it in `mapping.json` +
`approve-mapping`), the VM (`xfer-sp-saxon`, engine lifecycle verbatim)
delta-walks only the mapped collections and copies ONLY files whose exact
dest path is absent — Azure Put Blob/Block From URL from the pre-authed
`@microsoft.graph.downloadUrl` (the vimeo transport, so `If-None-Match: *`
create-only is API-enforced while writing inside the client's prefix), with
zero bookkeeping blobs in the container (all state VM-side, pulled home by
`harvest` — a hard gate before teardown). Because the 08-27 census never
validated per-file paths, a **calibration gate** walks a few
believed-complete sites first and aborts before any copy if >1% of a
complete site reads as missing (a path-convention bug would otherwise
duplicate the corpus); `transfer --diff-only` is the mandated first pass.
Size mismatches and dest-only files are recorded, never touched. The
census artifacts live in `companies/saxon/reports/sharepoint-census-20260827/`.

**The helpsy URL-list pull (one-off, not a family).**
`scripts/helpsy_url_pull.py` copies an S3 corpus the client delivered as two
gzipped URL LISTS uploaded into their own container (helpsy only,
slug-guarded) — `aws/presigned-urls.txt.gz` (1,263 SigV4 urls,
`X-Amz-Expires=172800`, four buckets) and `aws/public-urls.txt.gz` (1,721,773
anonymous urls, three buckets), each line `FILE: <url>`, no sizes and no
manifest. **No VM, and the reason generalises:** `Put Blob From URL` is a
SERVER-SIDE copy, so whoever drives it only issues one small control call per
object and the bytes never transit them — a VM buys unattended runtime, never
throughput. So this rides the qwilr/vimeo/zoom laptop shape
(`phases.ip_rule_ensure`, racwl container SAS held in-process, create-only
`If-None-Match: *`, no state file), reusing `s3_flat.py`'s per-thread
keep-alive connection pool as the transport (measured live: ~1,140 objects/s
at concurrency 256, so 1.72M objects is ~26 min). Dest is
`aws/<bucket>/<percent-decoded key>` — INSIDE the client's own prefix, so the
saxon discipline applies: **zero bookkeeping blobs in the container**, every
byte of run state local under `companies/helpsy/aws-pull/`. The copy SOURCE is
always the list line verbatim (already encoded); only the NAME is decoded.
Because there is no census, the large-object route is lazy: a refusal from
Azure while S3 itself answered fine (`x-ms-copy-source-status-code` 200/206)
triggers ONE size probe for that object and, above 256 MiB, a block-staged
redo — which is also why a 409 counts as "already landed" ONLY when its code
is `BlobAlreadyExists` (an oversized source can answer 409 too, and reading
that as success would silently drop the object). `classify()` keys on the
copy-source status (the slack rule — two servers can refuse us): a source 403
is `presigned-expired-or-invalid` / `public-access-revoked` and aborts the SET
after 25 CONSECUTIVE, never on the first (one odd object must not kill a
1.7M-object run, and 1.7M logged failures must not scroll past a revoked
policy); a source 404 is a recorded gap; OUR 403 is the firewall and re-runs
`ip_rule_ensure` (helpsy sits in a `rg-corpus-*-prod` RG, where the
provisioner is known to strip rules). Verify is a streaming merge-join of the
sorted expected names against the container listing and asserts **presence,
not source bytes** — the lists declare no sizes, so the byte figure is the
CONTAINER's. Ground truth from the real lists, all live-probed: 8 presigned
keys are S3 folder placeholders (excluded — a blob name may not end in `/`);
175 public keys ARE urls (`https%3A//helpsy-images-public.s3...`) and 1 has a
leading slash, all of which resolve 200 and are named verbatim (legal only
because the account has no hierarchical namespace); zero name collisions
across the seven buckets; and no redirects anywhere, which matters because
Azure's copy does not follow them. `expected-data-sizes.json` needs no change:
the `"aws"` service already matches the `aws` top-level prefix by name.

**The wallaroo-media Takeout link pull (one-off, not a family).**
`scripts/wallaroo_takeout_pull.py` + `scripts/wallaroo_takeout_vm_pull.py`
ingest a personal-account Google Takeout the client requested as **"send
download link"** rather than "export to Drive" (wallaroo-media only,
slug-guarded): **442.30 GB across 12 files** (measured 2026-08-31; the client
had said 412 GB / 11 — probe caught both) existing only behind authenticated
Takeout download URLs, so there is no bucket to rclone and no OAuth token to
mint. **Takeout ground truth, all measured live and each driving code:**
`i=` indexes the ARCHIVES of a job, NOT the parts of one archive — the server
keys off `j=`+`i=` and REWRITES the filename, so the name in the URL path is
ignored (i=0 asked for `…-5-001.zip`, i=1 returned `…-1-001.zip`) and ONE
cURL enumerates the whole job; past the last index Google answers HTTP
**500**, not 404, which is how the end of the list is found; the client may
paste the POST-redirect `takeout-download.usercontent.google.com/download/…`
URL instead of the `takeout.google.com/settings/…` one and both work, both
answering 206 so resume is real; and the files are NOT all zips — a job can
serve a raw `All mail Including Spam and Trash-002.mbox` (137.79 GB on
wallaroo, 31% of the corpus), **spaces in the name and no archive at all**,
so the name whitelist tolerates spaces and the fallback preserves the real
extension (naming an mbox `.zip` would send the sizer hunting a central
directory that does not exist). The client copies the download
requests out of Chrome DevTools as cURL commands; the VM
(`xfer-takeout-wallaroo-media`, engine lifecycle verbatim) downloads and
uploads them into `workspace-export/personal-takeout/` — a second-level
folder inside the existing Workspace-export prefix, so it reconciles against
the declared gdrive/gmail services with **no `expected-data-sizes.json`
change** while staying its own `sources_l2` row. **The credential is the
sharp edge:** a Chrome "Copy as cURL" carries the client's full Google
SESSION COOKIES, i.e. the whole account, not a scoped token — heavier than
anything else this harness handles. It rides stdin → ssh stdin → a 600 file
on the VM and dies with the VM (never argv, tags, logs, or a laptop file);
both sides strip `Cookie`/`Authorization` on any redirect leaving
`google.com` (deliberate duplication, the zoho/teams TokenBox precedent);
and the client instructions scope it further with an **Incognito window**
whose session dies when they close it — which makes "close the window" the
real end of the engagement, and therefore an actual message rather than a
teardown side effect. **The failure mode that cost real blobs (2026-08-31, first live run):**
Google answers an invalid session with **HTTP 200 and the sign-in PAGE**, not
an error status — so a status-only check reads it as success. The VM smoke
test passed on a 200, the puller never inspected what came back, and twelve
~1.2 MB sign-in HTML documents were committed as `takeout-part-NNN.zip`
before anything noticed. Every layer now proves it received a FILE: the
final host must not be `accounts.google.com`, the Content-Type must not be
`text/html`, and a response with no `Content-Disposition` is refused before
a byte is written. Two further lessons from the same run: the
`takeout.google.com` URL carries a **`rapt=` re-auth proof token** (Takeout
downloads sit behind a "verify it's you" gate) which expires fast, while the
post-redirect `usercontent` URL is already past that gate but lives only
~20-30 minutes — so **either form gives roughly a 20-30 minute window per
client interaction**, and a 442 GB corpus is therefore a MULTI-ROUND
engagement, not one shot. And a prior `manifest.json` in the destination is
the resume record, so a pass that committed the WRONG bytes poisons the next
one (its names still resolve): `--ignore-prior` exists for exactly that, and
is required until such blobs are removed.

`probe` is the day-one gate and runs before any
billable resource: exactly ONE 1-byte ranged GET per link, body never read,
reporting each part's real size, its `Content-Disposition` filename, and
whether the link is `open` / `auth-expired` (dead session; the client must
re-copy, a retry cannot fix it) / `link-expired` (archives lapse ~a week
after export, so a fresh one is days of client time) / `no-range-support`
(resume unavailable, an interrupted part restarts from zero). Since the URLs
differ only in `i=`, `--expand-parts N` clones one cURL across the index
range, but the base is never guessed — it must be the FIRST part (i=0 or 1)
or an explicit `--expand-start` — and `--probe-all` proves every generated
link before a VM exists. `write-links` repeats the 1-byte test **from the
VM**, which is not redundant: probe ran from the laptop's IP, and Google can
treat the same cookie differently from a datacenter IP. **Transport is
stage-then-azcopy, deliberately not a stream-through and not server-side
copy**: Azure's `x-ms-copy-source-authorization` only speaks `Bearer` so it
cannot carry a Cookie (the zoho wall), and staging decouples the two legs so
an upload hiccup never costs a *download* — downloads are the scarce,
expiring, allowance-limited resource. Per part: resumable `urllib` download
(Range on every attempt; a 200 answer means Range was ignored, so the partial
is discarded rather than concatenated), on-disk size checked against Google's
declared `Content-Length`, azcopy `--overwrite=false` (client-side
no-overwrite, the s3/github honesty — NOT `If-None-Match`), committed blob
length confirmed, then the local copy deleted. **The blob's existence is the
resume record** (azcopy commits atomically); because Takeout filenames come
from `Content-Disposition` and are not derivable, the previous run's uploaded
`manifest.json` is read at startup as the index→name map, and failing that
`download()` aborts the instant the response headers reveal the part already
landed — so a resume reads zero body bytes and needs no local state. Disk:
`default_os_disk_gb=1024`, because the image default ~30 GB would not hold
the 137.79 GB mbox; peak staging is the `parallel` biggest files at once
(~298 GB at the default 4) and 1 TB holds the whole 442 GB export anyway, so
deletion is an optimization rather than load-bearing. Verify runs on the **LAPTOP** via
`phases.ip_rule_ensure` + an `rl` SAS and makes a **real source-truth claim**
(unlike the github/zoho staged→container-only certification): Google declared
each part's byte length, only a size-matching file was uploaded, and the
committed blob length is compared against that same number — declared ==
staged == committed, with the link set's part COUNT as the completeness bar,
so a `--limit` pilot can never read as complete. `manifest.json` rides
`--overwrite=true` for exactly that reason (the github pilot-poisons-verify
fix). Out of scope: unzipping (parts land as `.zip`, which the sizer measures
exactly via central directories) and any second export.

**The VM-less ingests: qwilr, vimeo and zoom.** A Qwilr corpus is small JSON pulled from
Qwilr's REST API (`api.qwilr.com/v1`, account-wide bearer token — no
read-only scope exists; client revokes it after the engagement), so
`scripts/qwilr_transfer.py` (standalone — NOT a transfer_engine Spec) runs
the pull locally and PUTs straight to blob REST into
`<slug>-raw/qwilr-export/`. Because it runs from the laptop (external IP),
storage access uses `phases.ip_rule_ensure`/`ip_rule_remove_if_ours` — the
same mechanism as sizing; `allow-network` stays VM-only. racwl container
SAS but 1-day expiry (held in-process; no VM to outlive), writes are
create-only (`If-None-Match: *`), resume = re-run (one dest-prefix listing
skips landed blobs), no state file, token via stdin only. The API has no
PDF/HTML export and no bulk audit-trail/analytics export; embedded CDN
assets are manifested (`_meta/assets-manifest-*.json`), not downloaded.
Driven by the `qwilr-azure-transfer` skill. **When the account's API is
unavailable**, Qwilr support can export a back-office CSV of every page
(metadata + analytics + acceptance columns the API cannot give, plus
per-page public / collaborator / PDF-download links).
`scripts/qwilr_csv_pull.py` (same laptop-local shape: ip_rule_ensure,
racwl SAS, create-only writes, resume = re-run) drives that CSV instead:
rendered HTML per page (the collaborator link covers Drafts, which 401
publicly), plus Qwilr's async PDF render per published page —
`GET /pdf/<token>` starts a FRESH server-side render each call (never
re-trigger to poll) and the PDF appears at the loader-embedded
`download.qwilr.com/<uuid>.pdf` minutes later (S3 403 = not ready yet).
The CSV itself lands as the ledger blob; its collaborator links embed
secrets, so the CSV is sensitive — it lives under `companies/<slug>/`
(gitignored), never in version control.

**Vimeo** has no rclone backend either, but its corpus is hundreds of GB of
video — too big to proxy through the laptop. `scripts/vimeo_transfer.py`
(standalone, same shape as qwilr) therefore never streams video bytes: it
resolves each Vimeo download link's redirect to a signed CDN URL and drives
Azure **Put Blob From URL / Put Block From URL** server-side copy, so the
storage fabric pulls from Vimeo's CDN directly (laptop egress = API JSON +
control calls + tiny caption files). Same laptop IP-rule firewall
(`phases.ip_rule_ensure`), create-only commits (`If-None-Match: *` rides
the Put Block List), no state file, resume = re-run (media skip is keyed by
the `videos/<id>/` directory — video titles are mutable). Token: a Vimeo
personal access token (`public private video_files` scopes) via stdin only;
`video_files` is PLAN-gated (Standard/Pro+) so the `probe` subcommand is
the day-one gate, and it also detects the accounts that only expose file
arrays under API version 3.2. racwl container SAS at 2 days (server-side
copies are multi-hour); files ≤1 GiB are single-shot with Azure validating
Vimeo's declared md5, larger ones stage 256 MiB blocks and re-resolve the
CDN URL whenever it expires mid-copy (normal, budgeted). Thumbnails are
manifested, not downloaded; no analytics/comments/version-history export
exists. Driven by the `vimeo-azure-transfer` skill.

**Zoom** rides vimeo's transport (`scripts/zoom_transfer.py`, standalone,
same server-side Put Blob/Block From URL copy, same laptop IP-rule
firewall, create-only commits, no state file, racwl 2-day SAS) with
Zoom-specific ground truth: auth is a Server-to-Server OAuth app
(scope `recording:read:admin` — it DOES work with S2S despite a stale KB
note) whose THREE secrets (Account ID, Client ID, Client Secret) arrive
on stdin as 3 lines; the app must be **activated** by the account owner —
an unactivated app authenticates and then 400s on the listing, which is
the `probe` gate and a client conversation, never a retry. The listing
(`/accounts/me/recordings`, literal `me` — a real accountId needs the
master scope, 400 code 4711) is capped to ~1-month windows, walked from
`--from-date` (default 2015-01-01) and **fully materialized per month
before any copy** (a `next_page_token` expires during multi-minute
copies — crashed a real run). Placeholder rows (empty `file_type` =
still processing) are skipped; the 1 h token auto-refreshes (`TokenBox`)
and feeds mid-copy CDN re-resolves on the vimeo re-resolve budget; blob
names are deterministic (`meetings/<uuid>/<start>_<TYPE>_<fileid>.<ext>`)
so resume is keyed on the exact name; Zoom declares no md5, so verify is
byte-exact on size. Retention auto-delete gives the engagement a clock
(probe surfaces it); Team Chat, Zoom Phone, Whiteboards and trash are out
of scope. S2S apps are account-bound — an org with several Zoom accounts
(song-division) gets one app + one full cycle per account, sub-prefixed
`--dest-prefix zoom-export/<label>` so verify stays per-account. Driven
by the `zoom-azure-transfer` skill.

## Slack (connector-only, draft-only)

The client-Slack side lives in this repo: `scripts/slack_engine.py` plus the
`slack-kickoff`, `slack-inbox`, and `harvest-voice` skills. It replaced the
harness's handoff to the external `corpus-transfer-slack-operator` plugin
(2026-09-01); that plugin stays installed but nothing here reads or writes
its state. Rules, in priority order:

1. **The only Slack write is `slack_send_message_draft`.** No skill calls
   send, schedule, edit, delete, react, canvas, or conversation tools. Drafts
   land in the user's Slack Drafts; the user sends.
2. **Python never talks to Slack.** There are no Slack tokens in this repo.
   Skills read through the Claude Slack connector, transcribe a normalized
   snapshot (`companies/<slug>/slack/snapshot.new.json`, shape in
   `.claude/skills/slack-kickoff/references/read-protocol.md`), and the
   engine does everything after that: validation, merge, high-water marks,
   parent discovery (bot-authored exact `[Label]` posts from the "Company
   Transfer Setup" workflow), reconcile against `expected-data-sizes.json`
   (an optional `"slack_label"` on a declaration pins a thread whose label
   differs from the manifest name), kickoff planning, receipts, inbox, and
   the voice store. Discovery only: the engine never creates parents.
   `inbox` knows three kinds of author: the owner, registered teammates
   (`teammate_user_ids` in `channel.json`, set at `register --teammates`
   or later with `set-teammates`), and everyone else (clients). A
   teammate-last conversation is not waiting on the owner and teammate
   messages are never "unanswered", but only the owner's own reply or an
   ack clears earlier client messages (2026-09-02: a fleet inbox listed
   ~20 of 52 items a colleague had already answered).
3. **Kickoff copy is data**: `knowledge/kickoff-copy/<service_id>.json`,
   committed, one message per service, `status: approved|draft`; only
   approved entries are planned. Load-time gate (blocking): required
   fields, no em dash, no credential-shaped text, under 40k chars, unique
   aliases. Seeded 2026-09-01 from the plugin's PR #25 catalog (53
   services) plus the two fixed kinds. Editing copy is an ordinary commit.
4. **Voice examples never supply facts.** `companies/.voice/` (gitignored)
   holds the user's harvested sent messages with the message each answered,
   tagged by intent/service, and a reviewed `style.md`; `voice-select`
   returns tone examples only. Facts come from the thread, harness state,
   the library, and the service knowledge base.
5. **Never retry an unknown draft outcome** (a second attempt can duplicate
   a draft). `draft_already_exists` and `not_in_channel` are recorded
   outcomes; failure isolation applies per thread.

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
- **Takeout archives split by child folder:** inside a Google Takeout zip the
  wrapper `Takeout/` segment defers to `TAKEOUT_CHILDREN` (`Mail` → gmail,
  `Drive` → gdrive, `Chat` → gchat, …), and the path layer subtracts
  entry-attributed bytes so gmail-vs-gdrive splits are real (wallaroo-media,
  2026-08: 100% of a 12.4 TB Workspace export previously read as gdrive).
  The map is part of the matcher fingerprint — editing it forces a
  fleet-wide full re-size, like editing `SERVICE_CATALOG`.

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
