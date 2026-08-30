---
name: slack-azure-transfer
description: Use when a company's Slack export is missing its files — "pull the slack files", "the slack export has no attachments", "slack files to azure", "download the slack file links", "transfer <company>'s slack files" — or any probe, discovery, status check, verification, or teardown of an existing slack file-pull engagement.
---

# Slack export files → Azure

Pulls the FILES a Slack export only points at into
`<slug>-raw/slack-export-files/`, via a temporary Azure VM
(`xfer-slack-<slug>`) in the storage account's region — the **ninth VM
ingest**. The export itself is already in the container (the client pushed
it); this reads it there, builds a ledger linking every file to the
conversation and message that referenced it, and copies the bytes in.

## Why this exists — say the number out loud

A Business+ / Enterprise compliance export is a **complete transcript and
zero file bytes**. Every attachment, image, video, canvas and huddle
transcript survives only as an authenticated `files.slack.com` link inside
the JSON. On a real 2.35 GB sample export (123k day files, 1.1M messages)
that was **63,636 unique files, 62,056 of them Slack-hosted and worth
81.1 GB — about 34× the export**. (Of the rest, 996 are Google Drive links
Slack holds no bytes for and 584 are deleted or aged out of retention.)

So a company whose Slack shows as "delivered" because their export landed
has, in reality, delivered the index and none of the content. That is the
gap this skill closes, and it is worth stating plainly in any progress
conversation.

All az/ssh/Slack/blob mechanics live in `scripts/slack_transfer.py` (engine
lifecycle reused from `scripts/transfer_engine.py`, VM-side puller
`scripts/slack_vm_pull.py`) — never hand-roll them. Your job: orchestration,
judgment, the discovery question, the probe gate, and the confirmation
gates. Full command templates + troubleshooting:
[references/commands.md](references/commands.md).

## What makes this family different

1. **The source is inside the destination.** Every other ingest pulls from a
   remote SaaS API; this one reads the client's own export out of
   `<slug>-raw` and writes back into the same container. `discover-export`
   is therefore a real step, not a formality.
2. **The export carries its own auth.** Every file URL embeds one
   workspace-wide `xoxe-` token. In the normal case **there is no client
   credential to ask for at all** — `write-creds` is an override, not a
   setup step.
3. **Transport is Azure server-side copy** (the vimeo/zoom transport, not
   the stage-then-azcopy of github/zoho/figma/teams): the VM resolves each
   URL's redirect chain and hands the final signed URL to Put Blob / Put
   Block From URL. File bytes never touch the VM disk, and create-only is
   **API-enforced** by `If-None-Match: *` rather than a client-side flag.
4. **Verify can make a real source-truth claim.** The export declares a byte
   `size` for every file, so verify compares declared against what Azure
   committed — not staged→container only, the way teams/figma/zoho must.

## Scope boundary — say it out loud

**In scope:** every Slack-hosted file referenced by a message
(`files[]` *and* the `attachments[].files[]` entries a naive walk misses),
plus the root `canvases.json` / `lists.json` / `huddle_transcripts.json`
assets that no message walk reaches — including each canvas's and list's
full edit-history download. Thumbnails and transcodes (`thumb_64`…
`thumb_1024`, `mp4`/`mp4_low`/`hls`, `vtt` captions) are **on by default**
and multiply object count **7.6×** (409,782 renditions against 62,056
originals — 471,838 objects in all). `probe` reports both counts;
`--no-renditions` turns them off.

**Recorded, never fetched:** `mode: external` files. These are Google Drive
documents that Slack holds a *link* to and no bytes for — no Slack token can
retrieve them, and no amount of retrying changes that. They land in
`_meta/external-references.jsonl` with their ids, titles, URLs and the size
Slack recorded (996 files / 3.97 GB on the sample) so the **gdrive** ingest
can reconcile against them. Report this as a known, quantified exclusion,
never as a shortfall, and never as something a re-run would fix.

**Recorded as gaps:** `tombstone` and `hidden_by_limit` entries have no URL
at all — deleted files, or files beyond the workspace's retention window.
They go to `_meta/unavailable.jsonl` with their reason.

## Client asks

Usually **nothing**. Ask only if `probe` says so:

- `token-expired` → the client must run a **fresh Slack export**. Export
  download links expire *with the export*; there is no way to refresh them.
  Alternatively they can issue a Slack token (`xoxp-`/`xoxb-`) with
  `files:read`, which `write-creds` installs as an override.
- `no-file-links` → the export was produced without file links at all
  (Standard-plan, or a metadata-only export). A fresh, correctly scoped
  export is the only fix.
- Discovery found nothing → the export is not in the container yet. That is
  a push conversation, not a tooling problem.

If a Slack token was issued, tell the client to revoke it after verify —
the same clean-end convention as zoho/github. The export's own embedded
token needs nothing from us; it lapses on its own.

## The day-one stalls

Both are caught before any billable VM.

1. **Which export?** `discover-export` recognises two shapes — a `.zip`
   blob (confirmed by reading its central directory, two ranged GETs, never
   a download) and an already-extracted tree (a `channels.json` +
   `users.json` pair). Zero candidates or several is a **question for the
   user, never a guess**: picking the wrong archive builds a ledger for the
   wrong workspace and copies tens of thousands of files under it. Several
   candidates usually means a **two-phase export** (public channels shipped
   first, private/DMs later) — those are separate runs with separate
   `--dest-prefix` values, never merged.
2. **Do the links still work?** `probe` reads the export over a read-only
   SAS and makes exactly one live request to Slack. `link_gate` is one of
   `open`, `token-expired`, `no-file-links`, `files-missing`,
   `no-range-support`, `redirect-unresolvable`. Only `open` means proceed.

## Workflow

```
discover-export → probe → plan → create-vm → allow-network → write-dest
  → check-azure → [write-creds only if probe demanded it]
  → transfer (--limit-files 200 pilot first) → status → verify
  → transfer (full) → verify → teardown --confirmed
```

Each step's exact command is in
[references/commands.md](references/commands.md). Two gates: show the plan
and a clean `probe` (read the census back to the user — object count,
declared bytes, the rendition multiplier, the external-reference gap) and
get explicit confirmation before `create-vm`; `teardown` always requires
`--confirmed`.

Everything under `_meta/` uploads with overwrite allowed, so a
`--limit-files` pilot can never poison verify the way an unguarded pilot
manifest did on an earlier engagement (see the `github-pilot-poisons-verify`
memory).

## Reconciliation

A Slack declaration covers **both halves** — the client's pushed export and
the files we pulled — so pin both prefixes:

```json
"slack": {"bytes": <declared>,
          "prefix": ["<the client's export prefix>", "slack-export-files"]}
```

Two things the daily brief will otherwise misread:

1. **The container grows by roughly 34× the export size, and that growth is
   US, not client push.** Say so in any report or nudge; otherwise it reads
   as a burst of client progress, and the UsedCapacity skip check will
   re-size the company for days afterwards.
2. Slack manifests very often declare **record counts** rather than bytes
   (`{"records": N}`). Those are excluded from byte reconciliation by design
   and charted as a single actual bar — that is correct behaviour, not a
   missing declaration.

## Nudge / report guidance

`probe` reports **real bytes** on day one — the export declares a `size` for
every file, so `declared_bytes.hosted` is a measurement of the client's own
export rather than an estimate. That is unusual for this family and worth
using, but be precise about what it means: it is what Slack recorded when the
export was produced, not a promise those bytes are still fetchable. Only
`verify` proves what actually landed. Keep client-facing drafts in the house
style: no AI signature, no em dashes, casual Slack voice (CLAUDE.md
conventions).

## Judgment notes

- **The ledger is the deliverable as much as the bytes are.**
  `_meta/files-index.jsonl` is one row per unique file carrying the
  conversation, message ts and thread ts that referenced it;
  `_meta/file-shares.jsonl` carries every additional sighting of a file
  shared into several places. Without them the container is 60k anonymous
  blobs. `_meta/objects.jsonl` is the separate machine authority verify
  reads.
- **Paths are id-led, never name-led.** Slack file ids are immutable, file
  and channel names are not, and a two-phase export makes a rename between
  passes a live hazard rather than a hypothetical. This is the same lesson
  the vimeo and kidinme engagements taught.
- **Resume is stronger here than in the sibling ingests.** Every write is
  create-only at the API, so the blob's own existence is the resume record;
  shard markers only record which slices were walked. Re-running `transfer`
  is always safe and only mops up.
- **A long quiet stretch at phase `ledger` is normal.** The whole export is
  walked once before a single file is copied (13 seconds for the 2.35 GB
  sample on a laptop; longer for a big workspace read from blob). `status`
  shows the phase — don't read it as a hang.
- **`classify()` keys on the copy-SOURCE status**, the inverse of teams'
  endpoint-family rule, because two different servers can refuse us: Slack's
  401/403 is fatal only while nothing has copied yet (a dead token), and a
  per-file skip afterwards; Slack's 404 is a file deleted since the export;
  429 from either side always sleeps and never charges the retry budget.
- **`--rps-files` and `--copy-workers` are pacing knobs, not correctness
  controls.** It is Azure's fabric that hits Slack on this transport, so our
  only lever is how fast we issue copy requests; the defaults are
  deliberately conservative.
- **Verify's two classes of claim are not the same strength.** Originals get
  a declared-vs-landed byte equality check. Renditions carry no declared
  size, so only presence is asserted for them. Report them separately rather
  than as one number.
