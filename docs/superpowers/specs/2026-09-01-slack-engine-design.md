# Slack engine design

Date: 2026-09-01
Status: approved design, not yet implemented

## Goal

Bring the client-Slack side of an engagement into this repo: an in-house
engine that reads a company's Slack channel through the Claude Slack
connector and produces private drafts for the user to review and send.
It replaces the harness's dependency on the external
`corpus-transfer-slack-operator:company-kickoff` plugin skill for the
onboarding handoff, and serves several skills from one code path:

- `slack-kickoff`: one opening draft per service thread, iterating with the
  user on copy when the library has none.
- `slack-inbox`: what needs a reply, with a drafted answer.
- `harvest-voice`: build and refresh a store of the user's sent messages so
  drafts can mimic their style.

## Decisions made during brainstorming

| Question | Decision |
| --- | --- |
| Slack access surface | Connector only. No tokens, no Slack Web API from Python. |
| Thread parents | Discovery only. The "Company Transfer Setup" Slack workflow still posts them; the engine never posts. |
| Kickoff copy library | Seeded from the plugin's origin/main `support-cases.json`, simplified to one message per service. |
| Draft-vs-sent feedback loop | None. The library changes only by explicit edit in session. |
| Style mimicry | A harvested, tagged corpus of the user's sent messages plus a distilled, user-reviewed style guide. |
| Engine shape | Snapshot-driven: skills transcribe a normalized channel read to disk, Python does everything after that. |

## Invariants

1. **The only Slack write is `slack_send_message_draft`.** No skill may call
   send, schedule, edit, delete, react, canvas, or conversation-creating
   tools. Drafts land in the user's Slack "Drafts & Sent" and are sent by
   the user.
2. **Voice examples never supply service facts.** Facts come from the
   thread, harness state, the kickoff library, and the service knowledge
   base plugin. The voice store shapes tone only.
3. **Deterministic logic in `scripts/`, judgment in skills** (repo
   principle 2). Skills do connector calls and conversation; the engine
   does discovery, matching, rendering, inbox, harvest, selection.
4. **Failure isolation** (repo principle 4). One failed thread never stops
   the rest of a kickoff or inbox run.
5. **Never retry an unknown draft outcome.** A second attempt could create a
   duplicate draft; report and stop that item.
6. **Runtime state is local.** Everything under `companies/` is gitignored;
   only the kickoff library under `knowledge/` is committed.

## Layout

```
scripts/
  slack_engine.py          # the engine + thin CLI; stdlib only; --root aware
  import_slack_operator.py # ONE-OFF: seed knowledge/kickoff-copy/ from the
                           #   plugin catalog + import live registrations
knowledge/
  kickoff-copy/            # COMMITTED: one JSON per service (+ onboarding,
    zoom.json              #   progress-check); aliases, direction, message,
    onboarding.json        #   status, notes, source
    progress-check.json
companies/<slug>/slack/    # gitignored (companies/*/ rule)
  channel.json             # registration + discovered parents + high-water marks
  snapshot.json            # latest normalized read of channel + threads
  drafts.json              # receipts for every draft we created
companies/.voice/          # gitignored (companies/*/ matches dot-dirs)
  messages.jsonl           # harvested sent messages + what each answered
  style.md                 # distilled style guide, user-reviewed
  harvest-state.json       # per-channel harvest high-water marks
.claude/skills/
  slack-kickoff/SKILL.md
  slack-inbox/SKILL.md
  harvest-voice/SKILL.md
tests/fixtures/slack/      # synthetic snapshot + democo channel.json
```

The installed plugin stays untouched. Nothing here reads or writes
`~/.corpus-transfer-slack-operator/` except the one-off import.

## Engine CLI

All commands take `--root` (default `companies/`), print one JSON object on
stdout, and exit nonzero with a one-line JSON error on bad input.

| Command | Purpose |
| --- | --- |
| `register <slug> --channel-url --owner-user-id --company-name --channel-type public\|private\|slack_connect` | Create `channel.json`. Refuses if the channel id is already registered to another slug. Idempotent for the same slug. |
| `record-canvas <slug> --canvas-id --title --permalink [--replace]` | Record the EDP Instructions canvas reference (title, file id, permalink only). |
| `read-plan <slug>` | What the skill must read: channel id, channel high-water mark, known threads with their last-seen reply ts. |
| `ingest <slug> <snapshot-file>` | Validate, merge into stored snapshot, advance high-water marks, run parent discovery, print the reconcile result. |
| `reconcile <slug>` | Declared-vs-thread sets without a new read. |
| `kickoff-plan <slug> [--force]` | Per parent: rendered message from the library, or `missing`, `already-drafted`, `blocked`. |
| `record-draft <slug> --kind kickoff\|reply --thread-ts --text-file [--service] --outcome drafted\|already_exists\|error --reason` | Append a receipt to `drafts.json`. |
| `inbox <slug>` / `inbox --all` | Conversations waiting on the user, with context. |
| `ack <slug> --thread-ts` | Mark a conversation handled without a draft. |
| `validate-library` | Load every kickoff entry and run the content checks. |
| `voice-harvest <slug>` | Append the owner's messages from the snapshot to the voice store. |
| `voice-tag --ts --channel-id --tags` | Write tags onto a harvested row. |
| `voice-select --intent --service --context-file [--limit 5]` | Style guide + best examples for a draft. |

## Data contracts

Timestamps are ISO-8601 UTC except Slack `ts` values, which stay in Slack's
`seconds.micros` string form.

### `companies/<slug>/slack/channel.json`

```json
{
  "slug": "helpsy",
  "company_name": "Helpsy",
  "workspace_domain": "micro1-companies",
  "channel_id": "C0BSRM7D4F5",
  "channel_url": "https://micro1-companies.slack.com/archives/C0BSRM7D4F5",
  "channel_type": "slack_connect",
  "owner_user_id": "U0BEK4RNLE5",
  "instructions_canvas": {"canvas_id": "F0BUAGREL64", "title": "EDP Instructions",
                          "permalink": "https://micro1-companies.slack.com/docs/T0B8VHRABR7/F0BUAGREL64"},
  "parents": [
    {"ts": "1787875877.371599", "kind": "onboarding", "label": "Onboarding",
     "service_id": "onboarding", "author_user_id": "U0BT0NY588Y"},
    {"ts": "1787875878.815469", "kind": "service", "label": "AWS",
     "service_id": "aws-s3", "author_user_id": "U0BT0NY588Y"}
  ],
  "channel_hwm_ts": "1788111659.181069",
  "thread_hwm": {"1787875877.371599": "1788024112.538909"},
  "acked": {"1787875878.815469": "1788024112.538909"},
  "registered_at": "2026-09-01T00:00:00Z",
  "updated_at": "2026-09-01T00:00:00Z"
}
```

`service_id` on a parent is the matched kickoff-library id, or null when
nothing matched. `acked` maps a conversation's parent ts (or a top-level
message's own ts) to the newest message ts the user has handled.

### `companies/<slug>/slack/snapshot.json`

Written by the skill, validated and merged by `ingest`.

```json
{
  "channel_id": "C0BSRM7D4F5",
  "taken_at": "2026-09-01T14:00:00Z",
  "oldest_ts": "1788111659.181069",
  "complete": true,
  "messages": [
    {"ts": "1787875877.371599", "thread_ts": null, "user_id": "U0BT0NY588Y",
     "user_name": "Company Transfer Setup", "is_bot": true,
     "text": "[Onboarding]", "reply_count": 3, "latest_reply_ts": "1788024112.538909",
     "reactions": ["white_check_mark"]},
    {"ts": "1788024112.538909", "thread_ts": "1787875877.371599",
     "user_id": "U0BEK4RNLE5", "user_name": "Gabe", "is_bot": false,
     "text": "...", "reply_count": 0, "latest_reply_ts": null, "reactions": []}
  ]
}
```

Every field is required; `thread_ts`, `latest_reply_ts` may be null.
`ingest` rejects any other shape. Merge is keyed by `ts`: a message already
stored is replaced (edits), new ones are added, nothing is deleted. The
stored snapshot is the union of every ingest, so the engine can always see
whole threads. `complete: false` marks inbox results as partial.

### `companies/<slug>/slack/drafts.json`

```json
{"drafts": [
  {"kind": "kickoff", "thread_ts": "1787875878.815469", "service_id": "aws-s3",
   "text_sha256": "…", "library_entry_updated_at": "2026-09-01T00:00:00Z",
   "outcome": "drafted", "reason": null, "created_at": "2026-09-01T14:05:00Z"}
]}
```

Draft text itself is not stored, only its hash, so a later library change
shows up as "drafted from older copy" without keeping message bodies.

### `knowledge/kickoff-copy/<service_id>.json`

```json
{
  "service_id": "zoom",
  "display_name": "Zoom",
  "aliases": ["zoom recordings", "zoom cloud recordings"],
  "direction": "pull",
  "status": "approved",
  "message": "Yes, we can pull the Zoom recordings directly for you.\n\nPlease:\n1. ...",
  "notes": ["recording:read:admin works with S2S", "app must be activated by the owner"],
  "source": "imported from cdp-corpus-transfer-plugin support-cases 2026-08-30.1",
  "updated_at": "2026-09-01T00:00:00Z"
}
```

- `direction`: `pull`, `push`, `adaptive`, or null for the two fixed kinds.
- `status`: `approved` or `draft`. Only `approved` entries are planned.
- `message` may use `{company_name}` and `{instructions_url}`.
- `onboarding.json` and `progress-check.json` are fixed entries with
  `service_id` equal to their kind.
- Load-time checks, all blocking: required fields present, status valid,
  no em dash, no secret-shaped string (the plugin's pattern: Slack tokens,
  AWS keys, bearer tokens, signed query strings, Stripe keys,
  `password:`/`api key:` style pairs), message under 40,000 characters,
  aliases unique across the library.

### `companies/.voice/messages.jsonl`

One row per harvested message:

```json
{"slug": "helpsy", "channel_id": "C0BSRM7D4F5", "ts": "1788024112.538909",
 "thread_ts": "1787875877.371599", "sent_at": "2026-08-28T21:21:52Z",
 "text": "…", "replied_to": {"ts": "…", "user_name": "Alex Chavez", "text": "…"},
 "tags": ["answer", "google-workspace"], "harvested_at": "2026-09-01T14:10:00Z"}
```

`replied_to` is the newest message in the same thread before this one that
is not by the owner, or the parent for a first reply, or null for a
top-level message. Rows are unique on `(channel_id, ts)`.

`style.md` is free-form markdown. `harvest-state.json` maps channel id to
the newest owner message ts harvested.

## Read protocol

Every skill starts the same way:

1. `slack_engine.py read-plan <slug>`. Stops if there is no registration.
2. `slack_read_channel` with `oldest` = the channel high-water mark,
   paginating until exhausted. For each known thread whose
   `latest_reply_ts` moved past `thread_hwm`, and for each new top-level
   message with replies, `slack_read_thread`. Use the concise response
   format where the connector offers it.
3. Write the read as `companies/<slug>/slack/snapshot.new.json`, then
   `slack_engine.py ingest <slug> companies/<slug>/slack/snapshot.new.json`.
   The engine merges it into the stored `snapshot.json` and deletes the
   `.new` file; the skill never edits `snapshot.json` directly.

First run on a company reads the full channel once. Later runs are bounded
by what changed. The skill sets `complete: false` if it stopped paginating
early; the engine then labels inbox output as partial.

## Parent discovery and reconcile

Runs inside `ingest`. A parent is a top-level message from a bot user whose
text is exactly `[Label]`. `[Onboarding]` and `[Progress Check]` map to the
fixed kinds; anything else is a service parent. Two parents with the same
label are an error surfaced in the ingest result, never silently picked.

Service matching ladder, in order: exact `service_id`, exact alias, single
unambiguous close match (difflib ratio at or above 0.85 with no runner-up
within 0.05). The normalized name lowercases, strips punctuation, and
collapses whitespace. Declared services from `expected-data-sizes.json` are
matched with the same ladder, and additionally by an optional
`"slack_label"` field a declaration may carry when the manifest name and
the thread label differ.

Reconcile output:

- `matched`: declared service, thread ts, library entry id.
- `declared_without_thread`: declared services with no parent.
- `threads_without_declaration`: parents with no declared service (still
  planned if a library entry matches, flagged `undeclared`).
- `unmatched_threads`: parents matching neither declaration nor library.

## Kickoff flow (`slack-kickoff`)

`kickoff-plan` returns one item per parent:

- `planned`: thread ts, service id, rendered message, `undeclared` flag.
- `missing`: no approved library entry (a `draft` status entry is reported
  with its text so the skill can resume iterating).
- `already-drafted`: a `drafted` or `already_exists` receipt exists;
  `--force` re-plans it.
- `blocked`: the entry needs `{instructions_url}` and no canvas is
  recorded.

Skill steps:

1. Read protocol. Show the reconcile table and the plan. Services declared
   without a thread are listed, never drafted.
2. For each `missing` item, iterate in chat: propose an opening using
   facts from the service knowledge base plugin and tone from
   `voice-select --intent kickoff`; the user edits; on approval write the
   entry with `status: approved` (or `draft` if the user wants to keep
   iterating later), run `validate-library`, re-plan. Skipped items draft
   nothing.
3. For each `planned` item, call `slack_send_message_draft` with the
   channel id, the parent ts as `thread_ts`, and the verbatim message.
   Record the outcome with `record-draft`. `draft_already_exists` is an
   `already_exists` receipt. Any other connector error is an `error`
   receipt and the skill moves on.
4. Summarize: drafted, already existed, skipped, missing threads, errors.

Library edits are ordinary commits in this repo.

## Inbox flow (`slack-inbox`)

A conversation is a parent thread (parent plus replies) or a top-level
message with no replies. `inbox` flags a conversation when its newest
message is human, not the owner, and newer than both the owner's last
message in that conversation and its `acked` mark. Conversations whose
parent carries a `white_check_mark` reaction are excluded. Each item
carries: parent ts, service label and id when it is a service thread,
last author, age, `mentioned` (the owner's `<@id>` appears in any
unanswered message), and the last five messages as context. Sort:
mentioned first, then oldest waiting. `inbox --all` loops registered
companies and prints one list with the slug on each item; it is the hook
the daily brief can call later.

Skill steps:

1. Read protocol, run `inbox`, show the list. The user picks items or
   "all".
2. Per item, assemble the draft from, in priority: facts in the thread and
   harness state (latest sizing run, `status.json`, the manifest); service
   facts from the library entry and the service knowledge base plugin;
   tone from `voice-select --intent <answer|status|clarification>
   --service <id> --context-file <last client message>`.
3. Propose in chat. On approval, `slack_send_message_draft` into the
   thread, `record-draft --kind reply`, then `ack`.
4. Summarize: drafted, acked, left open.

## Voice store (`harvest-voice`)

1. Read protocol on one or all registered companies.
2. `voice-harvest <slug>`: every snapshot message by `owner_user_id` newer
   than the harvest high-water mark becomes a row with `replied_to`
   resolved, secret-shaped strings replaced by `[redacted]`, and
   `<@Uxxx>` mentions replaced by the user name seen in the snapshot.
3. The skill tags new rows in batches with `voice-tag`: one intent
   (`kickoff`, `answer`, `nudge`, `status`, `clarification`,
   `scheduling`), an optional service id, and `exclude` for throwaways
   (acknowledgements, one-word replies).
4. On request, or when the corpus has grown by more than 25 rows since the
   last distillation, the skill proposes a new `style.md` from the corpus:
   openings, sentence length, bullet habits, how asks are phrased, what
   never appears. The user approves before it is written.

`voice-select` scoring, deterministic: intent match +3, service match +2,
Jaccard word overlap between the context file and the row's `replied_to`
text scaled 0 to 3, +1 if sent within 90 days; `exclude` rows never
returned; ties broken by newest first; top `--limit` rows plus the full
`style.md`.

## One-off import (`import_slack_operator.py`)

- `--catalog <path to origin/main support-cases.json>`: for each case that
  is a service's `default_case_id`, write `knowledge/kickoff-copy/
  <service_id>.json` with `message` = the `kickoff_draft` lines joined by
  newline (falling back to `recommendation` + numbered `steps` when
  `kickoff_draft` is empty), `notes` = `limitations`, and `status:
  approved`. Existing files are not overwritten without `--overwrite`.
- `--registrations <path>` `--map plugin-slug=harness-slug ...`: for each
  plugin registration whose mapped slug has a `companies/<slug>/`
  directory, write `channel.json` with the channel, owner, canvas, and
  parents (ts, label, author), leaving high-water marks empty so the first
  read is a full read. Registrations without a mapped company directory
  are listed and skipped.

## Onboarding change

Step 5 of `onboard-company` becomes: ask for the channel link; if given,
run `slack_engine.py register`, resolve the user's Slack id with
`slack_read_user_profile`, then invoke `slack-kickoff`. The plugin skill is
no longer referenced.

## Error handling

- Bad input to any command: nonzero exit, `{"error": "...", "error_type":
  "contract_error"}` on stderr.
- Snapshot validation rejects missing or extra fields; it never guesses.
- Registration conflict (channel id owned by another slug): refuse.
- Duplicate parent labels: reported in ingest output; both are stored as
  `ambiguous` and excluded from planning until the user resolves them.
- Connector errors during kickoff or inbox: recorded per item, run
  continues; `draft_already_exists` and `not_in_channel` are recorded
  outcomes.
- Unknown draft outcome (timeout, no receipt): recorded as `error`, never
  retried automatically.

## Testing

`tests/test_harness.py` gains a Slack section using
`tests/fixtures/slack/`: a synthetic snapshot (bot parents including a
duplicate label, client replies, owner replies, a mention, a
`white_check_mark` parent) and a democo `channel.json`. Covered:

- register idempotency and conflict; canvas record/replace
- snapshot validation (reject bad shape), merge by ts, high-water marks
- parent discovery, duplicate-label ambiguity, service matching ladder
- reconcile against democo's `expected-data-sizes.json`, including
  `slack_label`
- kickoff-plan: planned, missing, already-drafted, blocked, `--force`
- record-draft receipts and hashing
- inbox: ordering, mention priority, owner-replied exclusion, ack,
  check-mark exclusion, `complete: false` partial flag, `--all`
- validate-library: every blocking check, including the seeded entries
- voice: harvest idempotency, `replied_to` resolution, redaction, tag
  write, select scoring and exclusion

No test touches Slack or the network.

## Out of scope

Scheduled or unattended runs, nudges, sending, canvas edits, reactions,
the plugin's worker/lease/task concepts, and any draft-vs-sent learning
loop.

## CLAUDE.md updates

Add `scripts/slack_engine.py`, `scripts/import_slack_operator.py`,
`knowledge/kickoff-copy/`, `companies/<slug>/slack/`, `companies/.voice/`,
and the three skills to the repo map; add a "Slack (connector-only)"
section stating the invariants above; rewrite onboarding step 5.
