---
name: vimeo-azure-transfer
description: Use when transferring a company's Vimeo library into their Azure -raw container — "transfer <company>'s vimeo", "vimeo to azure", "pull their videos", "vimeo export", or any probe or verification of an existing vimeo transfer.
---

# Vimeo → Azure transfer

Pulls a company's entire Vimeo library (source video files, metadata,
captions, folder/showcase structure) via the Vimeo API
(`api.vimeo.com`, personal access token) into `<slug>-raw/vimeo-export/`.
The harness's **second VM-less ingest** (after qwilr), with a different
transport: rclone has no Vimeo backend and hundreds of GB of video are too
big to proxy through this laptop, so the script resolves each Vimeo
download link's redirect to a signed CDN URL and drives **Azure
server-side copy** (Put Blob From URL / Put Block From URL) — the storage
fabric pulls from Vimeo's CDN directly; the video bytes never transit this
machine. Cousin of the gcs/dropbox/gdrive transfer skills but NOT the same
engine (`scripts/vimeo_transfer.py` is standalone; transfer_engine.py is
rclone/VM-shaped). The company must already be onboarded.

All REST/az mechanics live in `scripts/vimeo_transfer.py` — never
hand-roll them. Your job: orchestration, judgment, the pause point, the
probe gate, and the confirmation gate. Full command templates +
troubleshooting: [references/commands.md](references/commands.md).

**What the API cannot give (say so up front):** file links are
**plan-gated** — the `video_files` scope only works on a Vimeo
Standard/Advanced/Pro (or higher) plan; below that there is nothing to
pull and no workaround we'd use on a client engagement (`probe` is the
day-one gate). No analytics/stats export, no comments/likes export, no
version history (current file only). Thumbnails are **manifested, not
downloaded** — URLs land in `_meta/thumbnails-manifest-<ts>.json`.

## HARD CONSTRAINTS — never violate

1. **Firewall is the laptop IP-rule path, run by the script itself**
   (`phases.ip_rule_ensure` semantics: add our public IP only if needed,
   ~60s propagation, remove at the end only if we added it). NEVER remove
   a rule the run didn't add — pre-existing rules are the client's own
   push path. `allow-network` is the VM transfers' mechanism and does not
   apply here. (The server-side copy still works with this: Azure's fabric
   fetches from Vimeo's CDN; only OUR control calls cross the firewall.)
2. **Token hygiene.** The Vimeo personal access token (scopes: public,
   private, video_files) arrives from the client via a secure channel,
   goes to the script via stdin heredoc ONLY (never argv/env/files), and
   is never echoed. After the engagement, the client must delete it
   (developer.vimeo.com → My Apps).
3. **Create-only writes** under `vimeo-export/` — every commit sends
   `If-None-Match: *` (including the Put Block List that materializes a
   large video), so the storage API itself refuses to modify an existing
   blob. Nothing else in the container is ever touched.
4. **GATE before pull.** Show the `plan` output AND a clean `probe`
   result and get explicit user confirmation before running `pull` — it
   is the first write into client storage (additive, but still a write).
5. **Run `pull` in the background** (Bash `run_in_background`) and watch
   the stderr heartbeat — a full library is a multi-hour run (server-side
   copies are CDN-throughput-bound). Never block on it.

## No state file

The container is the source of truth: `pull` starts by listing
`vimeo-export/` and skips every video whose media blob already exists
(keyed by the `videos/<id>/` directory, not the filename — video titles
are mutable), so resume = re-run. A `_meta/videos-index-<ts>.json` blob
marks each completed run. Transfer state never touches `status.json`. To
see where things stand:

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/vimeo_transfer.py plan <slug>            # config + dest
python3 scripts/vimeo_transfer.py verify <slug> <<'EOF'  # full ground truth
<token>
EOF
```

## The five steps

### 1. plan `<slug>`

```bash
python3 scripts/vimeo_transfer.py plan <slug>
```

Read-only, offline: dest container/prefix, declared vimeo bytes from the
manifest, SAS + firewall approach, exact blob layout. Show it. Flags for
later steps: `--dest-prefix` (default `vimeo-export`), `--sas-days`
(default 2), `--video-limit N` (smoke run), `--api-version 3.2` (only if
probe says so).

### 2. PAUSE — token from the client

Snippet for the client's Vimeo admin (secure channel):

```
1. Go to developer.vimeo.com → My Apps → create (or open) an app.
2. Generate a personal access token with scopes: public, private,
   video_files ("Authenticated" access).
3. If the video_files scope isn't offered, your Vimeo plan doesn't
   include API file access (needs Standard/Advanced/Pro or higher) —
   tell us before generating anything.
4. Send the token via a secure channel (not plain email/chat). We'll
   ask you to delete it from the same page once the export is verified.
```

Wait for the paste.

### 3. probe `<slug>` — the plan gate

```bash
python3 scripts/vimeo_transfer.py probe <slug> <<'EOF'
<pasted token>
EOF
```

Touches NO Azure. Answers, before anything is promised: does the
account's plan expose download links at all (`download_capable`), which
API version works (`api_version_used` — some accounts only expose file
arrays under 3.2, a known Vimeo quirk; the script falls back
automatically), does the CDN honor Range requests (`range_probe:
206-ok`, required for large-file block copies), account tier, video
count, and Vimeo's own `quota_used_bytes` (a preview of total size).
`download_capable: false` (rc 2) is a plan/scope conversation with the
client, **not a retry**. Then **GATE: show plan + probe, confirm before
the first write.**

### 4. pull `<slug>`

```bash
python3 scripts/vimeo_transfer.py pull <slug> <<'EOF'
<pasted token>
EOF
```

Run via Bash `run_in_background`. The script: validates the token
(before the 60s firewall wait), adds the IP rule if needed, mints a
2-day racwl container SAS (held in-process only), lists what already
landed, walks `/me/videos` (paging.next), and per video: PUTs the
metadata JSON, resolves the download link's redirect to a signed CDN
URL, then has Azure copy the file server-side (one Put Blob From URL
with source-md5 validation for files ≤1 GiB; 256 MiB Put Block From URL
staging + a create-only Put Block List commit above that), then pulls
caption tracks (KBs, the one thing fetched locally). Folder map,
showcases, account info, thumbnails manifest and the run index land
under `_meta/`. Heartbeat on stderr every video (and every 10 blocks
inside a big copy). First live run: `--video-limit 2` as an end-to-end
smoke (auth + firewall + SAS + server-side copy) before the full pull.

Exit 0 even with per-video errors (they're counted in `video_errors`;
verify is the completeness authority). The IP rule is removed on the way
out even on failure.

### 5. verify `<slug>`

```bash
python3 scripts/vimeo_transfer.py verify <slug> <<'EOF'
<pasted token>
EOF
```

Fresh `/me/videos` listing vs blobs under `vimeo-export/videos/` (read
side uses the standard rl SAS). Checks presence per video AND byte size —
Azure's committed Content-Length must match one of the file sizes Vimeo
declares for that video. `missing` or `size_mismatch` non-empty → rc 2 →
re-run pull (resume skips everything landed) and verify again. `extra` =
videos deleted in Vimeo since the pull — informational, not a failure.
`no_file_api_side` = videos Vimeo exposes no file links for —
informational. Then wrap up: remind the user to have the client delete
the token; the SAS lapses on its own within 2 days; the IP rule is
already gone.

## Judgment notes

- Occasional 429s are normal — the script honors Retry-After. A wall of
  them: let the backoff work; don't parallelize by hand.
- **Mid-copy CDN-link expiry is normal, not an error.** Vimeo's signed
  CDN URLs die after a few hours; Azure surfaces the stale URL as
  `CannotVerifyCopySource` and the script re-resolves in place (logged as
  `CDN URL expired, re-resolving (n/20)`), retrying the same block. A
  wall of them right after ~24h means the API links themselves expired —
  also auto-handled by re-fetching the video's file entries.
- `Md5Mismatch` on a small-file copy is a REAL integrity failure (Azure
  compared the fetched bytes against Vimeo's declared md5) — re-run pull
  for that video; if it repeats, the file may be corrupt on Vimeo's side.
- `quality_fallbacks > 0` in the pull output means some videos had no
  source-quality file and the best transcode was archived — landed bytes
  will undershoot the declared manifest bytes. Say so when reporting.
- A 401 mid-run = the token was deleted or expired. Get a fresh one and
  re-run; resume skips every video already landed.
- A `403 AuthorizationFailure` on the first blob operations right after
  launch = IP-rule propagation, NOT a bad SAS — the script waits and
  retries; never re-mint for it.
- When reporting scope to anyone, always name the things the pull does
  NOT contain: analytics/stats, comments/likes, version history,
  downloaded thumbnails (manifested only) — and whether any videos were
  `no_file` or quality fallbacks.
- The `vimeo-export` prefix won't name-match a manifest service declared
  as "vimeo" — pin it in `expected-data-sizes.json` with
  `"prefix": "vimeo-export"` (same pattern as the other `*-export`
  prefixes).
- `--dry-run` on every subcommand prints the az commands and REST calls
  (secrets redacted).
