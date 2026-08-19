---
name: gdrive-azure-transfer
description: Use when transferring a company's Google Drive (My Drive or a Shared Drive) into their Azure -raw container — "transfer <company>'s google drive", "gdrive to azure copy", "pull their drive/shared drive", or any status check, verification, or teardown of an existing gdrive transfer engagement.
---

# Google Drive → Azure transfer

Copies a Google Drive (My Drive, a Shared Drive, or a folder within either)
into `<slug>-raw/gdrive-export/` via a temporary Azure VM (`xfer-gdr-<slug>`)
in the storage account's region, running rclone inside tmux. Sibling of
gcs-azure-transfer and dropbox-azure-transfer — same engine, same five
operations, same pauses. The company must already be onboarded; the user
supplies only the slug (plus optionally `--path` and/or `--team-drive`).

All az/ssh/rclone mechanics live in `scripts/gdrive_transfer.py` (thin CLI
over `scripts/transfer_engine.py`) — never hand-roll them. Your job:
orchestration, judgment, the pause points, and the confirmation gates. Full
command templates + troubleshooting:
[references/commands.md](references/commands.md).

## HARD CONSTRAINTS — never violate

1. **Network rules go through `allow-network` only.** The engine grants
   access itself (Microsoft.Storage service endpoint on the VM's subnet +
   a vnet-rule on the SA — IP rules never match same-region VM traffic;
   learned 2026-08, song-division) and teardown removes exactly the rule
   it added. Never hand-roll `network-rule add`, and NEVER remove a rule
   the engine didn't add — pre-existing rules are the client's own push
   path. If a 403 reappears mid-run, company infra may have stripped the
   rule: re-run `allow-network`.
2. **Google auth is human-in-the-loop.** Never automate the Google
   sign-in. The Drive owner/admin runs `rclone authorize "drive"` on
   their own machine; you wait for the user to paste the token.
3. **Static public IP, never deallocate.** Standard-SKU static IP; never
   stop/deallocate before final teardown.
4. **Secrets hygiene.** The SAS URL and Google OAuth token live ONLY in
   the VM's `~/.config/rclone/rclone.conf` (mode 600) and die with the
   VM. The engine pipes them over ssh stdin; never echo them, never write
   them to files/tags/logs here.
5. **Confirmation gates.** Exact plan + explicit user confirmation before
   (a) VM creation and (b) teardown. Teardown is ALWAYS manual-confirm
   (the script refuses without `--confirmed`).
6. **Sanctioned WRITE path** into `<slug>-raw` (rwlc SAS, 21-day default)
   — only adds blobs under the dest prefix, never modifies existing data.

## No state file

Azure is the source of truth: VM name `xfer-gdr-<slug>` + tags
(`purpose=gdrive-transfer`, `engagement`, `gdrive_path`,
`gdrive_team_drive`, `dest_prefix`). At the START of any invocation, always
run discovery first and report where things stand:

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/gdrive_transfer.py discover <slug>
```

Phases: `pre-setup` → `mid-setup` → `setup-complete` → `transfer-running` →
`transfer-stopped`. Transfer state never touches `status.json`.

## The five operations

### 1. setup `<slug>` `[--path "Folder/Sub"] [--team-drive <id>]`

```bash
python3 scripts/gdrive_transfer.py plan <slug> [--path ...] [--team-drive <id>]
```

1. Show the plan → **GATE: confirm before creating billable resources.**
   Flags: `--vm-size` (default Standard_D8s_v7; on SkuNotAvailable the
   script lists unrestricted alternatives), `--rg`, `--dest-prefix`,
   `--sas-days`. **Ask which drive is in scope up front**: My Drive
   (default) vs a Shared Drive (`--team-drive <id>` — the id is the long
   string in the Shared Drive's URL). A token only sees its own account's
   view; corp corpora usually live in Shared Drives.
2. `create-vm <slug> [...]` — VM + bootstrap. Path/team-drive land in VM
   tags for stateless rediscovery.
3. `write-azure-remote <slug>` — mints the container SAS (rwlc, 21 days),
   installs the `azure` remote.
4. `allow-network <slug>` — grants the VM storage access (service
   endpoint + vnet-rule). No pause: the engine runs this itself.
5. `check-azure <slug>` — classifies 403s (`vm_vnet_rule_present` /
   `vm_ip_in_ruleset`; propagation ≈ ~10s; missing rule = re-run
   allow-network). Never a SAS problem — do not re-mint.
6. **PAUSE #2 — Google token.** Snippet for the Drive owner (secure
   channel; revocable afterward at myaccount.google.com → Security →
   Third-party access):

   ```
   1. Install rclone on your machine:  https://rclone.org/downloads/
   2. Run:  rclone authorize "drive"
   3. Sign in with the Google account that owns (or can see) the Drive
      data when the browser opens.
   4. Send back the token block it prints (the JSON between the --->
      markers), via a secure channel.
   ```

   The token grants full Drive access for that account — secure channel
   only. Wait for the paste.
7. Pipe the pasted token via stdin — never argv:

   ```bash
   python3 scripts/gdrive_transfer.py write-gdrive-remote <slug> <<'EOF'
   <pasted token JSON>
   EOF
   ```

   **Custom OAuth app (faster — our own API quota):** if the user supplies
   their own Google OAuth client (the client-secret JSON downloaded from
   Google Cloud), the owner's authorize command becomes
   `rclone authorize "drive" "<client_id>" "<client_secret>"` (include both
   values in the snippet), and write-gdrive-remote needs
   `--oauth-client-json <path>` so the conf carries the matching
   client_id/client_secret — a token only refreshes against the client
   that minted it.

   Verifies the listing and reports total size + object count. **Sizing
   caveat:** native Google Docs/Sheets/Slides have no binary size — they
   are exported as docx/xlsx/pptx during copy, so `rclone size` undercounts
   a Docs-heavy Drive; say so when estimating. Expect Drive-API-bound
   throughput (~100–300 MiB/s at best, much less for many small files).

### 2. transfer `<slug>`

```bash
python3 scripts/gdrive_transfer.py transfer <slug>
```

rclone copy in tmux (8 transfers / 16 checkers / 5 retries /
`--tpslimit 10` + matching burst (a `--tpslimit` CLI flag overrides;
0 = uncapped) — the Drive API 403s under aggressive parallelism; raising
`--transfers` usually slows it down). Confirms alive and hands back — do
NOT block. Re-running resumes safely. Refuses if already running.

### 3. status `<slug>` — the most-used command

```bash
python3 scripts/gdrive_transfer.py status <slug>
```

Tight and scannable: running, done/total, %, throughput, ETA, errors,
recent distinct messages. A trickle of `userRateLimitExceeded` retries is
normal; a wall of them → lower `--transfers`. `looks_finished: true` →
suggest verify.

### 4. verify `<slug>`

```bash
python3 scripts/gdrive_transfer.py verify <slug>
```

`rclone check --one-way` (size-based — no common hash with Azure).
**Expect noise from native Google Docs**: exported files have no fixed
source size, so rclone can't fully check them — differences confined to
Docs/Sheets/Slides exports are expected, not data loss. Judgment: clean
apart from native-doc noise → treat as verified, suggest teardown (still
gated). Real binary files missing → re-run transfer, re-verify.

### 5. teardown `<slug>`

1. Refuses while the transfer tmux session is alive (`--force` only on an
   explicit user override).
2. Show the deletion plan: VM + NIC + OS disk + public IP + NSG + VNET →
   **GATE: always confirm**, then re-run with `--confirmed`.
3. Relay the reminder checklist verbatim: the vnet-rule is removed
   automatically (only ours); any UI-added IP rule must go via the UI;
   note the SAS expiry; the Drive owner can revoke rclone at
   myaccount.google.com → Security.

## Judgment notes

- Drive is a LIVE source — churn during a long copy shows up in verify;
  re-run transfer (incremental) before suspecting loss.
- Duplicate filenames in one Drive folder are legal; rclone warns
  (`Duplicate object found`) and skips dupes — surface the warning count,
  don't silently ignore it.
- "This file has been identified as malware or spam" errors → rclone
  needs `--drive-acknowledge-abuse`; ask the user before adding it (see
  commands.md manual rescue).
- `--team-drive` unset but the listing looks near-empty → the corpus is
  probably in a Shared Drive the token can see but My Drive doesn't show;
  list Shared Drives from the VM (commands.md) and re-run with the id.
- Millions of small files → API-bound, budget hours/days; consider
  per-folder `--path` scoping with per-folder `--dest-prefix`.
- `--dry-run` on every subcommand prints commands (secrets redacted).
