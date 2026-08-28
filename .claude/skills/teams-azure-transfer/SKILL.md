---
name: teams-azure-transfer
description: Use when transferring a company's Microsoft Teams data — channel messages, thread replies, hosted content, team/channel/membership metadata — into their Azure -raw container — "transfer <company>'s teams", "pull their teams messages", "teams to azure", or any probe, status check, verification, or teardown of an existing teams transfer engagement.
---

# Microsoft Teams → Azure transfer

Pulls a company's Microsoft Teams messaging corpus into
`<slug>-raw/teams-export/` via a temporary Azure VM (`xfer-teams-<slug>`) in
the storage account's region — the **eighth VM ingest**, following the
github/zoho/figma precedent. Per team it stages the channel directory and
membership index, then per channel every message thread (root + replies
fully expanded) and the hostedContents embedded in those messages' HTML
bodies.

It has to use a VM, and the reason is worth stating precisely: Teams has
**no rclone backend and no bulk export** — the corpus is every team's every
channel's every message thread plus replies, walked page by page against
Microsoft Graph's per-app throttling, an app-only client-credentials pull
(no signed-in user, no delegated/browser flow anywhere) that is genuinely a
multi-hour-to-multi-day job for a real tenant. `scripts/teams_vm_pull.py`
(pushed to the VM, run in tmux) does the whole walk there and azcopies each
unit up as it completes. Nothing downloads to this machine. The company must
already be onboarded — `companies/<slug>/config.json` supplies the
destination.

All az/ssh/Graph/azcopy mechanics live in `scripts/teams_transfer.py`
(engine lifecycle reused from `scripts/transfer_engine.py`, VM-side puller
`scripts/teams_vm_pull.py`) — never hand-roll them. Your job: orchestration,
judgment, the pause point, the probe gate, and the confirmation gates. Full
command templates + troubleshooting: [references/commands.md](references/commands.md).

## Scope boundary — say it out loud

**This pulls messages and metadata only, one copy per team.** A Teams
channel's file tab, and any attachment object referenced from a message, is
backed by that team's **SharePoint document library**, not Teams storage —
those bytes belong to a future sharepoint completion effort, not this tool.
Attachment objects are left as references inside the message JSON and never
fetched; only `hostedContents` (the inline images/files embedded directly in
a message's HTML body) are staged as bytes. This boundary is not
hypothetical — it is the same split saxon's Microsoft-Teams evidence already
showed (see the `saxon-teams-per-account-duplication` memory): channel files
live in SharePoint, and conflating the two overstates what a Teams-only pull
covers.

Out of scope entirely: 1:1 and group chats and meeting chats (no
`Chat.Read.All` — probe checks `/chats` and flags a WARNING if it is
somehow readable; a `TeamsMessagesData` export-mailbox fallback for chats is
a recorded future option, not built here), calendars, mail, tabs/apps,
Planner, Wiki, meeting recordings (they are doc-library files), and
attachment bytes (references only, never downloaded).

**The membership index answers "per user" questions without per-user
copies.** `_meta/team-members.jsonl` and `_meta/channel-members.jsonl` are
the one place membership lives — there is no reason, and no mechanism, to
stage a second copy of the corpus per person.

## Client asks

An Entra ID (Azure AD) app registration with these **application** (not
delegated) permissions, **admin-consented**:

- `Team.ReadBasic.All` (or `Group.Read.All`) — team directory
- `Channel.ReadBasic.All` — channel directory
- `Group.Read.All` — group/team membership
- `User.Read.All` — tenant roster
- `ChannelMessage.Read.All` — the actual message content

`Chat.Read.All` is deliberately **not** requested — it is out of scope
unless the client says otherwise (see the scope boundary above). Hand over
three things over a secure channel: the tenant id (Entra "Directory (tenant)
ID" GUID), the app's client id, and its client secret. The client rotates
the secret after verify — the same clean-end convention as zoho/github.

## The day-one stalls

Both are caught by `probe`, before any billable VM.

1. **The protected-API gate.** A tenant's app can mint a token and read the
   directory (groups/users/channels) fine and still get refused reading
   actual channel MESSAGES. `probe`'s `message_gate` field takes one of
   three values:
   - `open` — messages are readable, transfer can proceed.
   - `metered-model-required` (Graph 402) — the tenant needs an Azure
     subscription linked for Teams' metered API billing (model A/B); the
     client sets that up in the Teams admin center, then re-run probe.
   - `protected-api-approval-missing` (Graph 403) — the tenant has not been
     approved for Microsoft's protected Teams messaging APIs
     (aka.ms/GraphTeamsProtectedApis); the client files that request and
     approval takes days to weeks. This is a client conversation, not a
     retry.
2. **Admin consent missing** — a 403 on the very first directory read
   (`/groups`). The app registration exists but nobody granted admin
   consent for its application permissions; `probe` raises immediately with
   that diagnosis rather than limping through a partial census.

## Workflow

```
discover → probe → create-vm → allow-network → write-dest → check-azure
  → write-creds → transfer (--limit-teams 2 pilot first) → status
  → verify → teardown --confirmed
```

Each step's exact command (including the two 3-line stdin heredocs) is in
[references/commands.md](references/commands.md). Two gates: show the plan
and a clean probe result and get explicit confirmation before `create-vm`;
`teardown` always requires `--confirmed`.

`transfer`'s manifest uploads with `--overwrite=true` (run metadata, not
corpus blobs), so a `--limit-teams 2` pilot can never poison verify the way
an unguarded pilot manifest did on an earlier engagement (see the
`github-pilot-poisons-verify` memory) — the full run's manifest always
replaces it.

## Reconciliation

Pin `"microsoft_teams": {"bytes": <declared>, "prefix": "teams-export"}` in
`expected-data-sizes.json`. If this engagement ever runs against **saxon**
specifically: their container already has a legacy `Microsoft-Teams/`
prefix (capitalized, client self-pushed, ~95% cross-account duplicate — see
the `saxon-teams-per-account-duplication` memory) with its own
`duplicate_prefixes` treatment. That prefix and this tool's lowercase
`teams-export/` are unrelated and must not be merged into one declaration —
keep the legacy dedup entry exactly as it is and add the new pin alongside
it.

## Nudge / report guidance

`probe` reports team/channel/user **counts** and a rough, sampled
wall-clock estimate — **never bytes**: Graph publishes no message or
attachment byte size anywhere. The manifest's `total_staged_bytes` (surfaced
by `status` and `verify`) is the first honest byte number in the
engagement's life — say so plainly rather than implying probe's estimate was
ever a size. If this produces a client-facing nudge draft, keep it in the
skill house style: no AI signature, no em dashes, casual Slack voice (see
CLAUDE.md conventions).

## Judgment notes

- **A messages.jsonl line is always a complete thread.** Replies are paged
  to completion before a root message is ever written to disk — a crash
  mid-reply-page never leaves a half-written thread ahead of its cursor.
- **Resume = re-run.** Per-channel `.cdp-complete` markers plus
  `.cdp-cursor.json` cursors mean re-running `transfer` is always safe; a
  cursor whose `next_link` now 404s (channel deleted/archived mid-run) is
  not trusted forward — that one channel is cleared and re-walked from
  scratch, which is cheap because channels are small.
- **`classify()` is status + endpoint family**, the figma/zoho precedent: a
  `messages`-family 402/403 is fatal (the day-one stall), a `_meta`
  (required) refusal of any kind is fatal, everything else 403/404 is a
  recorded skip (archived team, deleted channel), 429 always sleeps, 401
  always re-mints.
- **The tenant guard.** `write-creds` refuses to write credentials whose
  stdin tenant id disagrees with `--tenant-id` (or, absent that, the VM's
  `teams_tenant_id` tag) — a client secret is only valid in the tenant it
  was issued for, and writing a mismatched one would silently point the
  pull at the wrong tenant.
- **`--rps-messages` and `--limit-teams` are pilot/tuning knobs**, not
  correctness controls — the puller's default pace (4 messages req/s) is
  deliberately conservative because Teams messaging is Graph's slow lane.
- **Verify is laptop-side**, same as figma/zoho: it lists the dest prefix
  (rl SAS via `phases.ip_rule_ensure`) and compares against the uploaded
  `_meta/manifest.json`. It certifies staged→container only and makes no
  source-size claim. `skipped_units` are deliberate (an archived team, a
  private channel the app can't see) and never a failure — read the reasons
  rather than treating the count as a gap.
