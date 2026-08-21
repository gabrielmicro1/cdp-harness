---
name: zoom-azure-transfer
description: Use when transferring a company's Zoom cloud recordings into their Azure -raw container — "transfer <company>'s zoom", "zoom to azure", "pull their recordings", "zoom export", or any probe or verification of an existing zoom transfer.
---

# Zoom → Azure transfer

Pulls a company's entire Zoom cloud-recording library (MP4 video, M4A
audio, transcripts, in-meeting chat, closed captions, poll CSVs,
timelines, AI summaries — plus per-meeting metadata) via the Zoom API
(`api.zoom.us`, Server-to-Server OAuth) into `<slug>-raw/zoom-export/`.
The harness's **third VM-less ingest** (after qwilr and vimeo), on
vimeo's transport: rclone has no Zoom backend and a recording library is
tens to hundreds of GB, so the script resolves each recording file's
download URL (fresh OAuth token appended, redirects walked manually) and
drives **Azure server-side copy** (Put Blob From URL / Put Block From
URL) — the storage fabric pulls from Zoom directly; the media bytes
never transit this machine. Cousin of the gcs/dropbox/gdrive transfer
skills but NOT the same engine (`scripts/zoom_transfer.py` is
standalone; transfer_engine.py is rclone/VM-shaped). The company must
already be onboarded.

All REST/az mechanics live in `scripts/zoom_transfer.py` — never
hand-roll them. Your job: orchestration, judgment, the pause point, the
probe gate, and the confirmation gate. Full command templates +
troubleshooting: [references/commands.md](references/commands.md).

**What the API cannot give (say so up front):** recordings already
**auto-deleted past the account's retention window** — only what is
still in the cloud is pullable, so the engagement has a clock (probe
surfaces the retention setting when readable). Recordings still
processing appear as placeholder rows (empty `file_type`) and are
skipped by design — a re-run in a day or two picks them up. Zoom
declares **no per-file checksum** — verify is byte-exact on size, which
is what the server-side copy commits. Out of scope entirely: Zoom Team
Chat history (a different compliance API), Zoom Phone recordings,
Whiteboards. Trash (deleted-but-recoverable, ~30 days) is surfaced by
probe as a count, not pulled.

## HARD CONSTRAINTS — never violate

1. **Firewall is the laptop IP-rule path, run by the script itself**
   (`phases.ip_rule_ensure` semantics: add our public IP only if needed,
   ~60s propagation, remove at the end only if we added it). NEVER remove
   a rule the run didn't add — pre-existing rules are the client's own
   push path. `allow-network` is the VM transfers' mechanism and does not
   apply here. (The server-side copy still works with this: Azure's
   fabric fetches from Zoom; only OUR control calls cross the firewall.)
2. **Credential hygiene.** THREE secrets — the S2S OAuth app's Account
   ID, Client ID and Client Secret (scope `recording:read:admin`) —
   arrive from the client via a secure channel and go to the script via
   stdin heredoc ONLY (3 lines, in that order; never argv/env/files),
   and are never echoed. After the engagement, the client must
   deactivate or delete the app (marketplace.zoom.us → Manage → Built
   Apps).
3. **Create-only writes** under `zoom-export/` — every commit sends
   `If-None-Match: *` (including the Put Block List that materializes a
   large recording), so the storage API itself refuses to modify an
   existing blob. Nothing else in the container is ever touched.
4. **GATE before pull.** Show the `plan` output AND a clean `probe`
   result and get explicit user confirmation before running `pull` — it
   is the first write into client storage (additive, but still a write).
5. **Run `pull` in the background** (Bash `run_in_background`) and watch
   the stderr heartbeat — a multi-year account is a multi-hour run
   (server-side copies are host-throughput-bound; Lemonlight-scale is
   35k files / 1.7 TB). Never block on it.

## No state file

The container is the source of truth: `pull` starts by listing
`zoom-export/` and skips every file whose blob already exists (keyed by
the EXACT blob name — names are deterministic,
`<start>_<TYPE>_<fileid>.<ext>`, unlike vimeo's mutable titles), so
resume = re-run. A `_meta/recordings-index-<ts>.json` blob marks each
completed run. Transfer state never touches `status.json`. To see where
things stand:

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/zoom_transfer.py plan <slug>            # config + dest
python3 scripts/zoom_transfer.py verify <slug> <<'EOF'  # full ground truth
<account id>
<client id>
<client secret>
EOF
```

## The five steps

### 1. plan `<slug>`

```bash
python3 scripts/zoom_transfer.py plan <slug>
```

Read-only, offline: dest container/prefix, declared zoom bytes from the
manifest, SAS + firewall approach, the month-windowed date range, exact
blob layout. Show it. Flags for later steps: `--dest-prefix` (default
`zoom-export`), `--sas-days` (default 2), `--from-date` (default
2015-01-01), `--to-date` (windowed re-runs), `--meeting-limit N` (smoke
run).

### 2. PAUSE — credentials from the client

One set of credentials per Zoom ACCOUNT — S2S apps are account-bound,
and some orgs run several Zoom accounts (song-division does): each
account needs its own app, its own three values, and its own
probe/pull/verify cycle (see the multi-account judgment note). Snippet
for the client's Zoom **account owner or admin** (secure channel):

```
1. A Zoom account OWNER or ADMIN goes to marketplace.zoom.us →
   Develop → Build App → Server-to-Server OAuth. (If that app type
   isn't offered, see the note below the snippet — it's a role
   permission, not a wrong page.)
2. In Scopes, add: recording:read:admin ("View all user recordings").
3. ACTIVATE the app on its Activation tab. This is the step everyone
   misses — an un-activated app authenticates fine and then every
   listing call fails.
4. Send us three values from the app's credentials page via a secure
   channel (not plain email/chat): Account ID, Client ID, Client
   Secret.
5. Once we confirm the export is verified, deactivate or delete the
   app from the same page.
```

Wait for the paste.

**If the client "can't see Server-to-Server OAuth"** (the #2 stall,
seen live on song-division 2026-08): the app type is hidden unless the
signed-in user has the Server-to-Server OAuth role permission. The
account OWNER has it by default; an admin needs the owner to enable it
at zoom.us (the admin portal, not the marketplace): Admin → User
Management → Roles → their role → Role Settings → Advanced Features →
"Zoom for developers" → "Server-to-Server OAuth app". Also confirm they
are signed into the company account, not a personal one, and that the
account is on a paid plan. Fastest unblock: have the owner create the
app themselves.

### 3. probe `<slug>` — the activation gate

```bash
python3 scripts/zoom_transfer.py probe <slug> <<'EOF'
<account id>
<client id>
<client secret>
EOF
```

Touches NO Azure. Answers, before anything is promised: do the three
secrets mint a token (`token_ok`), and does the account-wide recordings
LISTING actually work (`listing_ok`) — an S2S app that was created but
never **activated** authenticates and then 400s exactly here, the #1
stall. `listing_ok: false` (rc 2) is a client conversation ("activate
the app / add the scope"), **not a retry**. Also: a sample recording
file and whether its download host honors Range requests
(`range_probe: 206-ok`, required for large-file block copies), file
types seen, placeholder rows, this month's recording count, the
retention setting (when readable) and a trash count. Note: an old
knowledge-base entry claims recording scopes don't work with S2S apps —
they do (proven in production); don't be talked out of the scope. Then
**GATE: show plan + probe, confirm before the first write.**

### 4. pull `<slug>`

```bash
python3 scripts/zoom_transfer.py pull <slug> <<'EOF'
<account id>
<client id>
<client secret>
EOF
```

Run via Bash `run_in_background`. The script: mints the token and
validates the LISTING (before the 60s firewall wait — the unactivated-
app 400 fails fast), adds the IP rule if needed, mints a 2-day racwl
container SAS (held in-process only), lists what already landed, then
walks the account month by month (Zoom caps the listing to ~1-month
windows), **fully materializing each month's listing before copying
anything** (page tokens expire during multi-minute copies), and per
recording file: skips placeholders and already-landed names, resolves
the download URL's redirect with a fresh token, then has Azure copy the
file server-side (one Put Blob From URL for files ≤1 GiB; 256 MiB Put
Block From URL staging + a create-only Put Block List commit above
that). Per-meeting metadata JSON and the run index + account context
land alongside under `meetings/` and `_meta/`. Heartbeat on stderr
every month and every 25 files. First live run: `--meeting-limit 2` as
an end-to-end smoke (auth + firewall + SAS + server-side copy) before
the full pull.

Exit 0 even with per-file or per-month errors (they're counted in
`file_errors` / `month_errors`; verify is the completeness authority).
The IP rule is removed on the way out even on failure.

### 5. verify `<slug>`

```bash
python3 scripts/zoom_transfer.py verify <slug> <<'EOF'
<account id>
<client id>
<client secret>
EOF
```

Fresh month-windowed listing vs blobs under `zoom-export/meetings/`
(read side uses the standard rl SAS; a month-listing failure here is
FATAL — verify must never under-report). Checks presence per file AND
byte-exact size — Azure's committed Content-Length must equal Zoom's
declared `file_size`. `missing` or `size_mismatch` non-empty → rc 2 →
re-run pull (resume skips everything landed) and verify again. `extra`
= blobs whose recording no longer appears in the API — **expected**
under Zoom's retention auto-delete (the export legitimately holds more
than the live account); informational, never delete.
`placeholders_api_side` = recordings still processing — they become
pullable later. Then wrap up: remind the user to have the client
deactivate/delete the app; the SAS lapses on its own within 2 days; the
IP rule is already gone.

## Judgment notes

- **Multiple Zoom accounts in one org** (song-division, 2026-08): an
  S2S app is account-bound, so each account needs its own app + three
  secrets and its own full probe/pull/verify cycle. Give each account
  its own sub-prefix — `--dest-prefix zoom-export/<account-label>` on
  every subcommand for that account — so verify's ground truth stays
  per-account (with a shared prefix, every other account's blobs would
  look like `extra`). The top-level prefix stays `zoom-export`, so a
  single `"prefix": "zoom-export"` pin in `expected-data-sizes.json`
  still covers the whole service.
- **A 400 on the recordings listing is never retried** — it means the
  S2S app is unactivated or mis-scoped (or someone used a literal
  accountId, which needs the master scope — the script always uses the
  literal `me`). Go back to the client about the app, not the run.
- **Mid-copy source-URL expiry is normal, not an error.** The OAuth
  token embedded in the download URL lives ~1h; Azure surfaces the
  stale URL as `CannotVerifyCopySource` and the script re-resolves with
  a fresh token in place (logged as `source URL expired, re-resolving
  (n/20)`), retrying the same block.
- A 401 mid-run is auto-handled once (token re-mint — calls can
  straddle the hourly expiry); a second 401 means the credentials were
  revoked or the app deactivated. Get fresh ones and re-run; resume
  skips everything landed.
- `placeholders_skipped > 0` = recordings still processing on Zoom's
  side. Re-run pull in a day or two to pick them up; say so when
  reporting.
- **Don't sit on a green probe.** Zoom's retention auto-delete means
  recordings can vanish between probe and pull — schedule the pull
  promptly and say so if the client's retention window is short.
- A `403 AuthorizationFailure` on the first blob operations right after
  launch = IP-rule propagation, NOT a bad SAS — the script waits and
  retries; never re-mint for it.
- When reporting scope to anyone, always name the things the pull does
  NOT contain: Team Chat history, Zoom Phone, Whiteboards, trashed and
  retention-deleted recordings, still-processing placeholders.
- The `zoom-export` prefix won't name-match a manifest service declared
  as "zoom" — pin it in `expected-data-sizes.json` with
  `"prefix": "zoom-export"` (same pattern as the other `*-export`
  prefixes).
- Occasional 429s are normal — the script honors Retry-After. Zoom rate
  limits are account-wide: never parallelize by hand.
- `--dry-run` on every subcommand prints the az commands and REST calls
  (secrets redacted).
