---
name: slack-inbox
description: Use when the user wants to know what needs a reply in a company's Slack channel or across all channels — "what do I need to respond to", "check <company>'s slack", "slack inbox", "draft a reply to <client>", "anything waiting on me".
---

# Slack inbox

Find every conversation in a registered channel whose newest message is
from a client and unanswered by the user, then draft replies. **Never
sends**; every reply is a `slack_send_message_draft` in the thread. Fleet
mode is a thin loop over the same per-company function.

## Steps

1. **Read protocol** for the company (or each registered company when the
   user asks fleet-wide): `.claude/skills/slack-kickoff/references/read-protocol.md`.
2. **List what is waiting.**
   ```bash
   python3 scripts/slack_engine.py inbox <slug>        # or: inbox --all
   ```
   Show a table: company (fleet mode) | thread label | last author | waiting
   since (age) | mentioned? | unanswered count. Mentions come first, then
   oldest waiting. If `partial` is true say the read was incomplete. The
   user picks items or "all".
3. **Draft each picked reply** from three sources, in this priority:
   - Facts in the thread (`context` in the item) and harness state: the
     latest `sizing-runs/` file, `status.json`, `expected-data-sizes.json`.
     A "how much has landed" question gets the real number.
   - Service facts from `knowledge/kickoff-copy/<service_id>.json` and the
     `corpus-transfer-playbook:service-knowledge-base` skill.
   - Tone from the voice store:
     ```bash
     python3 scripts/slack_engine.py voice-select --intent answer \
       --service <id> --context-file <file with the client's last message>
     ```
     Intents: `answer`, `status`, `clarification`, `nudge`, `scheduling`.
     Voice examples shape wording only; they never supply facts.
   Propose the reply in chat. No em dashes; never ask for credentials in
   Slack; the user's casual voice.
4. **On approval**: `slack_send_message_draft` with `channel_id`, the item's
   `thread_ts`, and the text verbatim; then
   ```bash
   python3 scripts/slack_engine.py record-draft <slug> --kind reply --thread-ts <ts> --text-file -
   python3 scripts/slack_engine.py ack <slug> --thread-ts <ts>
   ```
   Items the user handled elsewhere or wants to ignore: `ack` only.
5. **Summarize**: drafted, acked, left open.

## Notes

- A top-level client message with no replies is its own conversation
  (`kind: top_level`, `thread_ts` = its own ts); the draft goes in a thread
  under it.
- Threads whose parent carries a `white_check_mark` reaction are complete
  and never listed.
- An acked conversation reappears as soon as the client writes again.
- Registered teammates (`teammate_user_ids` in `channel.json`) count as
  "us" for who spoke last: a conversation a colleague answered last is not
  listed, and their messages are never counted as unanswered. They do not
  count as the user's own reply: after client, colleague, client the item
  is listed with both client messages unanswered and any @-mention of the
  user still set, until the user replies or acks. If a listed item's
  `last_author` is a micro1 colleague, add them with
  `python3 scripts/slack_engine.py set-teammates <slug> --teammates <Uxxxx>`
  instead of acking thread by thread.
- Do not draft into a thread that already has one of your drafts pending;
  the connector reports `draft_already_exists` — tell the user to edit or
  send the existing one.
