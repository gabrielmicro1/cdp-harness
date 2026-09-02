---
name: slack-kickoff
description: Use when a company's Slack channel needs its service threads opened — "kick off <company> on Slack", "draft the kickoff messages", "populate the service threads", after onboarding when the user gives a channel link, or to re-plan a channel whose threads changed.
---

# Slack kickoff

One private draft per service thread in the company's channel, rendered
from the committed kickoff library, plus an in-chat loop to write copy for
services the library does not cover yet. **Never sends.** The only Slack
write anywhere in this harness is `slack_send_message_draft`; the draft
lands in the user's Slack Drafts and they send it.

Deterministic work is `scripts/slack_engine.py` (registration, discovery,
matching, rendering, receipts); this skill does the connector calls and the
copy conversation. Design: `docs/superpowers/specs/2026-09-01-slack-engine-design.md`.

## Steps

1. **Register if needed.** If `read-plan <slug>` says no registration, get
   the channel link, resolve the user's Slack id with
   `slack_read_user_profile` (self), read the channel once with
   `slack_read_channel` to get its name and whether external members are
   present (external domain ⇒ `slack_connect`), then:
   ```bash
   python3 scripts/slack_engine.py register <slug> --channel-url <url> \
     --owner-user-id <Uxxxx> --company-name "<Name>" --channel-type slack_connect \
     --teammates <Uxxxx>,<Uxxxx>
   ```
   The channel name `edp-NNN-<company>` gives the company name. A channel
   already registered to another slug is refused; surface it, don't force.
   `--teammates` is the micro1 colleagues who also answer in the channel
   (their ids come from the channel read: internal-domain members who are
   not the user). Without it their replies count as client messages and
   `inbox` lists conversations they already handled. The list is edited
   later, never by re-registering:
   ```bash
   python3 scripts/slack_engine.py set-teammates <slug> --teammates <Uxxxx>,<Uxxxx>
   ```
2. **Record the EDP Instructions canvas** when the channel has one: its file
   id usually appears in the welcome message ("work through F0B…"); confirm
   the title with `slack_read_canvas`; permalink is
   `https://<workspace>.slack.com/docs/<team-id>/<FILE_ID>`. Then
   `record-canvas <slug> --canvas-id --title --permalink` (`--replace` if a
   different one is recorded). No canvas ⇒ the onboarding item stays
   `blocked`; say so.
3. **Read protocol** — follow `references/read-protocol.md` exactly.
4. **Show the reconcile and the plan.**
   ```bash
   python3 scripts/slack_engine.py kickoff-plan <slug>
   ```
   Present a table: thread label | status | library entry | undeclared?
   Then the `declared_without_thread` list (declared in the manifest, no
   thread — flag, never draft) and `ambiguous` labels (duplicate parents —
   ask which is real; nothing is drafted for them).
5. **Iterate on `missing` items.** For each, propose an opening in chat:
   facts from the `corpus-transfer-playbook:service-knowledge-base` skill
   (never invent provider steps), tone from
   `voice-select --intent kickoff --service <id>`, and the style of the
   closest approved library entry. Rules for the copy: lead with the
   recommended route and the one thing the client must do; defer the full
   runbook; credentials go through the secure CDP platform, never Slack or
   email; no em dashes; `{company_name}` allowed. Route sections are bold
   bullets, exactly `- **You push it**, Instructions: …` and
   `- **We pull it**, Instructions: …`, never with a (partial)/(full)
   qualifier; `validate-library` rejects any other shape. The user edits; on
   approval write `knowledge/kickoff-copy/<service_id>.json` (see schema in
   the spec), `status: approved` (or `draft` to keep iterating later), add
   the thread label as an alias if it differs, run `validate-library`, and
   re-run `kickoff-plan`. Items the user skips draft nothing.
6. **Create the drafts.** For every `planned` item, one
   `slack_send_message_draft` call with `channel_id`, `thread_ts`, and the
   `message` verbatim. After each call record the outcome:
   ```bash
   python3 scripts/slack_engine.py record-draft <slug> --kind kickoff \
     --thread-ts <ts> --service <id> --text-file <path-or-->  [--outcome already_exists|error --reason …]
   ```
   Drive the loop from a python heredoc with `subprocess.run` (zsh does not
   word-split unquoted variables; a `for` loop over pairs breaks argparse).
   `draft_already_exists` ⇒ `--outcome already_exists`. Any other connector
   error ⇒ `--outcome error --reason <code>` and continue with the rest.
   Never retry a call whose outcome is unknown; report it.
7. **Summarize**: drafted, already existed, skipped, blocked, missing
   threads, errors. Remind the user the drafts are in Slack ▸ Drafts.

## Edge cases

- Re-running is safe: `already-drafted` items are skipped unless `--force`.
  Use `--force` only when the user wants fresh drafts (e.g. the library
  copy changed and the old draft was deleted).
- A label like "Outlook/Exchange" that matches nothing: add it as an alias
  to the right entry rather than creating a near-duplicate entry.
- The connector's auto-mode classifier occasionally blocks a draft whose
  text looks credential-shaped; it is non-deterministic — retry that one
  call verbatim once, then report.
- Library edits are ordinary commits; there is no plugin cache to sync.
