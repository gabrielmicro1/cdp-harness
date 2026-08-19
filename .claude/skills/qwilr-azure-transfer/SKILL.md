---
name: qwilr-azure-transfer
description: Use when pulling a company's Qwilr account (pages, proposals, saved blocks) into their Azure -raw container — "transfer <company>'s qwilr", "qwilr export", "pull their qwilr pages", "qwilr to azure", or any verification of an existing qwilr pull.
---

# Qwilr → Azure transfer

Pulls a company's entire Qwilr account via Qwilr's REST API
(`api.qwilr.com/v1`, bearer token) into `<slug>-raw/qwilr-export/`. The
harness's **first VM-less ingest**: the corpus is small JSON (page documents,
account settings — typically tens of MB), so it runs locally on this laptop.
No VM, no rclone, no tmux — a synchronous, resumable process. Cousin of the
gcs/dropbox/gdrive transfer skills but NOT the same engine
(`scripts/qwilr_transfer.py` is standalone; transfer_engine.py is
rclone/VM-shaped). The company must already be onboarded; API access requires
a Qwilr Enterprise / API-enabled plan (their own docs are inconsistent —
confirm per account).

All REST/az mechanics live in `scripts/qwilr_transfer.py` — never hand-roll
them. Your job: orchestration, judgment, the pause point, and the
confirmation gate. Full command templates + troubleshooting:
[references/commands.md](references/commands.md).

**What the API cannot give (say so up front):** no PDF/HTML renders (PDF is
a per-page dashboard action; bulk via help@qwilr.com), no bulk audit-trail /
analytics / acceptance-history export. Embedded CDN assets (images/video
inside blocks) are **manifested, not downloaded** — URLs land in
`_meta/assets-manifest-<ts>.json`.

## HARD CONSTRAINTS — never violate

1. **Firewall is the laptop IP-rule path, run by the script itself**
   (`phases.ip_rule_ensure` semantics: add our public IP only if needed,
   ~60s propagation, remove at the end only if we added it). NEVER remove
   a rule the run didn't add — pre-existing rules are the client's own
   push path. `allow-network` is the VM transfers' mechanism and does not
   apply here (IP rules work fine for laptop traffic).
2. **Token hygiene.** The Qwilr access token is account-wide — there is NO
   read-only scope — and is shown once at creation. It arrives from the
   client via a secure channel, goes to the script via stdin heredoc ONLY
   (never argv/env/files), and is never echoed. After the engagement, the
   client must revoke it (Qwilr → Settings → API).
3. **Create-only writes** under `qwilr-export/` — every PUT sends
   `If-None-Match: *`, so the storage API itself refuses to modify an
   existing blob. Nothing else in the container is ever touched.
4. **GATE before pull.** Show the `plan` output and get explicit user
   confirmation before running `pull` — it is the first write into client
   storage (additive, but still a write).
5. **Run `pull` in the background** (Bash `run_in_background`) and watch
   the stderr heartbeat — the 60s firewall propagation plus ~2 API calls
   per page can exceed a foreground Bash timeout on a large account.
   Never block on it.

## No state file

The container is the source of truth: `pull` starts by listing
`qwilr-export/` and skips every blob that already exists, so resume =
re-run. A `_meta/pages-index-<ts>.json` blob marks each completed run.
Transfer state never touches `status.json`. To see where things stand:

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/qwilr_transfer.py plan <slug>          # config + dest
python3 scripts/qwilr_transfer.py verify <slug> <<'EOF'  # full ground truth
<token>
EOF
```

## The four steps

### 1. plan `<slug>`

```bash
python3 scripts/qwilr_transfer.py plan <slug>
```

Read-only: dest container/prefix, SAS + firewall approach, exact blob
layout. Show it → **GATE: confirm before the first write to client
storage.** Flags for later steps: `--dest-prefix` (default
`qwilr-export`), `--sas-days` (default 1), `--page-limit N` (smoke run).

### 2. PAUSE #1 — token from the client

Snippet for the client's Qwilr admin (secure channel; the token is
full-account, shown once):

```
1. In Qwilr, open Settings → API (app.qwilr.com/#/settings/api).
2. Create an access token and copy it — Qwilr shows it only once.
3. Send it via a secure channel (not plain email/chat).
   Note: the token grants full access to the account; we'll ask you to
   revoke it from the same page once the export is verified.
```

Wait for the paste. If token creation isn't visible, their plan may not
include API access — that's a Qwilr support conversation, not a retry.

### 3. pull `<slug>`

```bash
python3 scripts/qwilr_transfer.py pull <slug> <<'EOF'
<pasted token>
EOF
```

Run via Bash `run_in_background`. The script: validates the token (before
the 60s firewall wait), adds the IP rule if needed, mints a 1-day racwl
container SAS (held in-process only), lists what already landed, then
walks `GET /pages` (cursor) → `GET /pages/{id}` → create-only PUT per
page, plus the account endpoints (users, saved-blocks, taxes,
payment-gateways, webhooks) and the `_meta` index/assets blobs. Heartbeat
on stderr every 25 pages. First live run: consider `--page-limit 5` as an
end-to-end smoke (auth + firewall + SAS + PUT) before the full pull.

Exit 0 even with per-page errors (they're counted in `page_errors`; verify
is the completeness authority). The IP rule is removed on the way out even
on failure.

### 4. verify `<slug>`

```bash
python3 scripts/qwilr_transfer.py verify <slug> <<'EOF'
<pasted token>
EOF
```

Fresh `/pages` listing vs blob names under `qwilr-export/pages/` (read
side uses the standard rl SAS). `missing` non-empty → rc 2 → re-run pull
(resume skips everything landed) and verify again. `extra` = pages deleted
in Qwilr since the pull — informational, not a failure. Then wrap up:
remind the user to have the client revoke the token; the SAS lapses on its
own within a day; the IP rule is already gone.

## Judgment notes

- Occasional 429s are normal — the script honors Retry-After. A wall of
  them: let the backoff work; don't parallelize by hand.
- A 401 mid-run = the token was revoked or expired. Get a fresh one and
  re-run; resume skips every page already landed.
- `page_errors > 0` after a pull: re-run once before escalating — most are
  transient. The same ids failing twice → inspect those pages with the
  client (deleted mid-run? permission quirk?).
- A `403 AuthorizationFailure` on the first blob operations right after
  launch = IP-rule propagation, NOT a bad SAS — the script waits and
  retries; never re-mint for it.
- When reporting scope to anyone, always name the three things the pull
  does NOT contain: PDF renders, audit trail/analytics, downloaded assets
  (manifested only).
- The `qwilr-export` prefix won't name-match a manifest service declared
  as "qwilr" — pin it in `expected-data-sizes.json` with
  `"prefix": "qwilr-export"` (same pattern as the other `*-export`
  prefixes).
- `--dry-run` on every subcommand prints the az commands and REST calls
  (secrets redacted).
