# teams-azure-transfer — Microsoft Teams messages + metadata → `<slug>-raw`

**Status:** approved design, pre-implementation
**Date:** 2026-08-28
**First engagement:** saxon (tenant 505f352c-…, Entra app "Relay Export 2")

## Why this tool exists

Saxon's vendor tool ("Relay") exported Teams **per user account**: every
account's folder carried a full copy of the shared channel files of every team
that account belonged to. Result: 13.4 TB pushed, ~0.62 TB distinct (~95%
redundant), root posts only (every message had `"replies": null`), no 1:1/group
chats. The correct unit of enumeration is the **team**, not the user.

This engine pulls what only Teams has — channel messages with replies fully
expanded, hosted content, and team/channel/membership metadata — exactly once
per team. It deliberately does **NOT** pull channel files: Teams channel files
physically live in each team's SharePoint site document library, and the
sharepoint-completion effort owns those bytes (verified 2026-08-28: only ~130 GB
of the 618.8 GB distinct channel files exist under `sharepoint/` today; the
missing team sites are part of the 1,652 never-transferred site collections).
One prefix per system of record; duplication is prevented by construction, not
by dedup logic.

The "per user view" requirement is satisfied by the **membership index**
(`_meta/`): team→members and channel→members mappings reconstruct any user's
view of Teams from single-copy data.

## Decision record

- **Approach:** zoho/figma-shape VM puller (user-selected; the local
  qwilr-shape was offered and declined). Engine VM lifecycle reused verbatim,
  own REST pull layer on the VM.
- **Scope boundary:** messages + metadata only. Channel files excluded (see
  above). 1:1/group/meeting chats excluded (app lacks `Chat.Read.All`; the
  `TeamsMessagesData` mailbox fallback via `Mail.Read` is a recorded future
  option, not part of this tool).
- **Dest layout is guid-led** (figma's mutable-names lesson): team names and
  channel display names live in `_meta/name-map.json`, never in blob paths.

## Components

```
scripts/teams_transfer.py          # laptop CLI over transfer_engine lifecycle
scripts/teams_vm_pull.py           # VM-side REST puller (pushed like zoho_vm_pull.py)
.claude/skills/teams-azure-transfer/SKILL.md
.claude/skills/teams-azure-transfer/references/commands.md
```

- `teams_transfer.py` reuses `transfer_engine.py` functions verbatim for
  create / allow-network / check-azure / teardown and shares
  `mint_container_sas` (racwl, 21-day default). VM name `xfer-teams-<slug>`,
  company RG, SA region, engine-default size and OS disk (staging is GBs).
- `teams_vm_pull.py` runs in one tmux session on the VM, stages under
  `~/xfer-teams/dest/`, azcopies per unit to `<slug>-raw/teams-export/`.
- VM tags carry `dest_prefix=teams-export` (base prefix only) and the usual
  engine discovery tags. **No state file** — VM + tags are the truth;
  `status.json` / `.fleet-state.json` are never touched.

## Auth

Three stdin lines, in order: **tenant id, client id, client secret** (zoom's
3-line convention). Laptop → ssh stdin → 600 env file on the VM; never in
argv, tags, logs, or files on the laptop. A `TokenBox` on BOTH sides (laptop
probe and VM puller — the deliberate github/zoho duplication) mints the 1-hour
app-only token from
`https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` with scope
`https://graph.microsoft.com/.default` and refreshes on expiry.

Required application permissions (verified present on saxon's app):
`Team.ReadBasic.All`, `Channel.ReadBasic.All`, `Group.Read.All`,
`User.Read.All`, `ChannelMessage.Read.All`. (`Sites.Read.All`,
`Files.Read.All`, `Mail.Read` are present on saxon's app but unused here.)

## The day-one stall: the protected-API gate

App-only channel-message reads are Microsoft **protected APIs**: the
permission alone is not sufficient — the app needs Microsoft's protected-API
approval, or the metered `model=A|B` billing path. `probe` (laptop-side) is
the gate:

1. Mint a token; enumerate teams (`/groups?$filter=resourceProvisioningOptions/Any(x:x eq 'Team')`),
   channels, and users; report counts.
2. Read ONE message page from one channel and classify:
   - **200** → gate open. (Saxon: Relay's message JSON in the container
     already proves this app's message path works.)
   - **402 / metered-model error body** → metered billing demanded; client
     conversation about linking an Azure subscription. Not retried.
   - **403 on the messages family** → protected-API approval missing; client
     files the request form (days-to-weeks lead time). Not retried.
3. Report a wall-clock estimate. Graph has NO message-count endpoint, so the
   estimate derives from the channel count times a sampled depth (probe pages
   a handful of channels) and is labeled rough. **Counts only, never bytes**
   — Graph publishes no message sizes (figma precedent); the run manifest's
   staged bytes is the engagement's first real byte number. (For saxon
   specifically, Relay's `_ChannelMessages` JSONs in the container give real
   root-post counts per channel — the skill notes this as a free
   cross-check.)
4. Record (not fail on) the absence of `Chat.Read.All` — chats are out of
   scope by design; probe output says so explicitly so the client
   conversation covers it.

## Units and dest layout

```
teams-export/
  _meta/
    teams.jsonl              # one line per team: group + /teams/{id} settings
    channels.jsonl           # all channels incl. private + shared, keyed team-guid
    users.jsonl              # full user roster (select: id, upn, displayName,
                             #   mail, accountEnabled, userType)
    team-members.jsonl       # group membership, keyed team-guid
    channel-members.jsonl    # per-channel membership for private/shared channels
    name-map.json            # guid → display names (teams and channels)
    manifest.json            # run metadata + per-unit counts/bytes (--overwrite=true)
  teams/<team-guid>/
    <channel-id>/messages.jsonl
    <channel-id>/hosted/<message-id>_<hostedContentId>.<ext>
```

- **`_meta` is the one REQUIRED unit** (zoho's settings-are-fatal rule): any
  failure inside it is fatal to the pass. Everything else is per-unit
  skippable-with-record.
- **Messages:** `GET /teams/{id}/channels/{cid}/messages?$top=50&$expand=replies`,
  following `@odata.nextLink`. If a message's expanded replies carry their own
  `nextLink` (>~1000 replies), its `/replies` is paginated to completion
  BEFORE the line is written — **a JSONL line is always a complete thread**
  (root message with all replies nested inline). Reactions, edit/delete
  timestamps, and attachment reference objects ride the JSON for free.
- **Hosted contents** (inline images/media): fetched per referenced
  `hostedContents/{id}/$value` with the Bearer header — hence staged on the
  VM, no server-side-copy cleverness (tiny bytes). Extension derived from the
  returned content type; content type recorded in the manifest.
- **Attachment bytes are NOT fetched.** Attachment objects in message JSON
  reference document-library files; the sharepoint completion owns them.

## Pacing and error classification

- **Proactive pacing with 429 backstop** (figma's shape): Teams messaging
  endpoints are Graph's slow lane and per-app limits are endpoint-class
  specific, so the token bucket defaults conservatively (~4 rps on the
  messages family, higher on directory reads), honoring `Retry-After` on 429
  as the backstop. `--rate` overrides. No daily credit clock exists (unlike
  zoho): the per-minute pace IS the wall clock, and probe's estimate is
  read to the client before launch.
- **Classification keys on status + endpoint family** (figma's rule, not
  zoho's body-code-first — Graph error bodies are secondary evidence):
  - 403/402 on the messages family → fatal (the day-one stall; see probe).
  - 403/404 on a single team or channel (archived team, deleted channel,
    membership quirk) → per-unit recorded skip, never fatal.
  - 429 → sleep `Retry-After`, never fatal.
  - 5xx / timeouts → bounded retry with backoff, then per-unit skip.
  - 401 → one TokenBox re-mint, then fatal.

## Resume

Per-channel `.cdp-complete` markers plus `.cdp-cursor.json` holding the last
`@odata.nextLink` and the JSONL line count. Resume truncates `messages.jsonl`
to the last whole page and continues from the cursor; a rejected or expired
`nextLink` invalidates the cursor and **re-walks that channel** (channels are
small — zoho-grade cursor surgery is not warranted here). Hosted-content
fetches are keyed by deterministic dest name, so re-runs skip existing files.

Units upload **as they complete** via azcopy `--overwrite=false` (client-side
no-overwrite — the s3/github honesty, not `If-None-Match`); run metadata
(`manifest.json`) uploads `--overwrite=true` (the github pilot-poisons-verify
fix, shipped from day one). A mid-run VM loss costs at most one channel.

## Lifecycle and verify

One-shot laptop CLI subcommands, driven by the skill:

```
probe → create → allow-network → write-creds → launch → status … → verify → teardown
```

- `launch` pushes `teams_vm_pull.py`, starts the tmux session; `status` tails
  progress (per-unit counters) without touching Azure state.
- **Verify runs on the LAPTOP** (VM normally gone): `phases.ip_rule_ensure` +
  a 1-day `rl` SAS, comparing the uploaded `manifest.json` against a
  `teams-export/` dest listing — per-unit counts + bytes, name+size exact.
  Certifies **staged→container only**; no source-size claim is possible or
  made (Graph publishes no message bytes).
- Teardown removes exactly the vnet-rule we added; pre-existing rules never
  touched. Secrets die with the VM.

## Reconciliation

`expected-data-sizes.json` takes
`"microsoft_teams": {"prefix": "teams-export", ...}` — a plain pin, no
`source_split` (single product, single prefix). For saxon specifically, the
legacy `Microsoft-Teams/` prefix keeps its separate `duplicate_prefixes`
treatment until the client deletes it; the two prefixes never mix.

## Out of scope (said out loud in the skill)

1:1/group/meeting chats (no `Chat.Read.All`; `TeamsMessagesData` mailbox
fallback recorded as a future option), calendars, mail, channel tabs and app
content, Planner, Wiki (deprecated; its content migrated to channel files),
meeting recordings (they are channel files in the doc library), deleted
teams/channels recovery, and document-library files of any kind.

## Docs and tests

- CLAUDE.md: repo-map entries for the four new files; an ingest-section
  paragraph ("Teams rides the engine's VM with its own REST pull layer")
  covering the protected-API stall, the guid-led layout, the
  messages-vs-files boundary with sharepoint, and the no-byte-claim rule;
  skill added to the skills list.
- No offline tests in `test_harness.py` (engine convention — integration
  tested live; saxon is the first engagement). `--dry-run` prints az commands
  on every Azure-touching subcommand, per harness convention.

## Success criteria

1. `probe` on saxon reports teams/channels/users counts consistent with the
   container census (322 teams known) and confirms the message gate is open.
2. A full saxon run lands one copy per team: messages with replies expanded
   (spot-check: threads that show `"replies": null` in Relay's export carry
   full reply arrays in ours), hosted content fetched, membership index
   complete.
3. `verify` passes staged→container; re-running the pull is a no-op mop-up.
4. Zero writes outside `teams-export/`; the sizing path still sees the
   container read-only.
