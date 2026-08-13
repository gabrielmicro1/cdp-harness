---
name: daily-brief
description: Use when the user starts their day or asks for the morning rundown — "daily brief", "morning routine", "what's the state of the fleet", "catch me up on the companies".
---

# Daily brief — the single morning entry point

Composes the orchestrators, then summarizes. Never reimplement their logic —
compose them:

1. **update-all** (fleet sizing; resumable polling across turns — see that
   skill for triage). Wait for every company to settle before reporting.
2. **report-all** (all reports + dashboard; capture gen_dashboard.py's fleet
   summary JSON — it is your data source; don't re-derive numbers).
3. **Nudge drafts** for each stalled company (below).
4. **One conversational summary** (below). End by telling the user to open
   `reports/index.html`.

## The summary (one message, conversational, no wall of tables)

Cover, in order:
- Fleet-wide % complete (and received vs declared totals).
- **Who moved in the last 24h**, with their deltas (from `delta_24h`).
- **Newly stalled** companies (stage flipped to stalled today) vs already
  stalled ones — distinguish them.
- **Action items:** failures with reasons, no-vm companies, VMs not running.
- **ETAs at risk:** ETA more than ~2 weeks out, rate trending down, or no ETA
  because nothing has moved.
- **Top 3 things needing attention today** — your judgment call; a stall at a
  big-revenue company beats a small failed run.

## Nudge drafts (never send — draft only)

For each stalled company, write a short Slack-style nudge to
`companies/<slug>/reports/nudge-<date>.md` (local runtime state, not
committed). Voice rules (the user sends these from their own Slack):

- Casual, friendly, direct. First person. No greeting-card openers, no
  signature (it's Slack), no corporate filler, and **no em dashes**.
- Must contain their specific numbers: what they've pushed, % of manifest,
  how long since it last grew, and what's missing (name the lagging services).
- One concrete ask: a push date, or a quick call to unblock.
- Nothing that sounds like AI wrote it. Read it back; if it sounds like a
  template, rewrite it.

Example shape (numbers made up):

> Hey! Quick check on the data push. We're seeing 1.8 TB of the 3.5 TB from
> the manifest, and nothing new has landed since last Thursday. Slack and
> Zendesk look complete, but gdrive is still at about 40%. Is something stuck
> on your side? If you can kick off the rest this week we stay on track for
> the end of the month. Happy to hop on a call if the uploader is being
> painful.

## Failure isolation

A company that fails to size or report still appears in the brief, as an
action item with its reason. The brief must never be blocked or aborted by
one broken company.
