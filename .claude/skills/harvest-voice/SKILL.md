---
name: harvest-voice
description: Use when building or refreshing the store of the user's own sent Slack messages that drafts imitate — "harvest my messages", "update my voice", "learn my slack style", "refresh the style guide", or after a batch of channels has new replies from the user.
---

# Harvest voice

Collect the user's sent messages from registered channels into
`companies/.voice/messages.jsonl` (gitignored), tag them, and keep a short
reviewed `style.md`. The store shapes tone in every draft; it never supplies
service facts. Only the engine reads or writes the store; this skill never
opens the jsonl directly.

## Steps

1. **Read protocol** for the requested company, or every registered company
   (`.claude/skills/slack-kickoff/references/read-protocol.md`).
2. **Harvest.**
   ```bash
   python3 scripts/slack_engine.py voice-harvest <slug>
   ```
   Idempotent and incremental (per-channel high-water mark). The engine
   redacts credential-shaped strings and resolves `<@U…>` mentions to names.
3. **Tag the new rows.** List what still needs tags, then assign each row
   one intent — `kickoff`, `answer`, `nudge`, `status`, `clarification`,
   `scheduling` — plus the service id when obvious, plus `exclude` for
   throwaways (thanks, ok, one-word replies):
   ```bash
   python3 scripts/slack_engine.py voice-untagged --limit 50
   python3 scripts/slack_engine.py voice-tag --channel-id <C…> --ts <ts> --tags answer,zoom
   ```
   Each untagged row shows the message and the client message it answered.
   Batch the tag calls from a python heredoc; repeat until `untagged_total`
   is 0. Judgment lives here, not in the engine.
4. **Distill the style guide** when asked, or when more than ~25 rows were
   added since `style.md` last changed. Read a broad sample of tagged rows
   (via `voice-select` per intent) and propose `companies/.voice/style.md`:
   how the user opens, sentence length, when bullets appear, how asks are
   phrased, sign-offs, and what never appears. Show it; write it only on
   approval.
5. **Summarize**: rows added, rows tagged, style guide updated or not.

## Rules

- Never copy a harvested message into a customer draft verbatim; it is an
  example of voice, not a template.
- Never store anything outside `companies/.voice/`; the corpus carries
  client context and stays local.
