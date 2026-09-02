# Slack read protocol (shared by slack-kickoff, slack-inbox, harvest-voice)

The engine never talks to Slack. Every skill starts by reading the channel
through the Claude Slack connector and handing the engine a normalized
snapshot. Do this in order, every run:

1. `python3 scripts/slack_engine.py read-plan <slug>` — prints `channel_id`,
   `channel_hwm_ts` (null on the first run = full read), and `threads`
   (known parent ts + last-seen reply ts). No registration = stop and run
   `register` first (see slack-kickoff).
2. Read the channel: `slack_read_channel` with `channel_id`, `oldest` =
   `channel_hwm_ts` when present, `response_format: concise`, paginating
   with `cursor` until `next_cursor` is empty. Then `slack_read_thread` for:
   - every top-level message whose `latest_reply_ts` is newer than the
     thread's `last_reply_ts` in the plan (or that is not in the plan at
     all and has replies);
   - every new top-level message with replies.
   Stop early only if the channel is enormous; then set `complete: false`.
3. Write `companies/<slug>/slack/snapshot.new.json` in exactly this shape
   (every field required; `thread_ts`/`latest_reply_ts`/`oldest_ts` may be
   null):

   ```json
   {"channel_id": "C…", "taken_at": "<ISO UTC>", "oldest_ts": "<ts or null>",
    "complete": true,
    "messages": [
      {"ts": "1788111650.238059", "thread_ts": null, "user_id": "U…",
       "user_name": "Display Name", "is_bot": false, "text": "…",
       "reply_count": 0, "latest_reply_ts": null, "reactions": ["eyes"]}
    ]}
   ```

   Include the parent message itself and every reply you read. `user_name`
   is the display name the connector shows; `is_bot` is true for app/bot
   authors (the "Company Transfer Setup" workflow bot included);
   `reactions` are emoji names without colons. Transcribe text verbatim;
   never summarize.
4. `python3 scripts/slack_engine.py ingest <slug> companies/<slug>/slack/snapshot.new.json`
   — validates, merges into the stored snapshot, advances high-water marks,
   discovers parents, and deletes the `.new` file. A validation error means
   the transcription is wrong; fix the file, never the engine.

Writing the snapshot from a large first read is token-heavy; that is the
one-time cost. Later runs only carry what changed since the high-water mark.
