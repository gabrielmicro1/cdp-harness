---
name: dropbox-azure-transfer
description: Use when transferring a company's Dropbox data into their Azure -raw container — "transfer <company>'s dropbox", "dropbox to azure copy", "pull their dropbox", or any status check, verification, or teardown of an existing dropbox transfer engagement.
---

# Dropbox → Azure transfer

Copies a Dropbox account (or a folder within it) into
`<slug>-raw/dropbox-export/` via a temporary Azure VM (`xfer-dbx-<slug>`) in
the storage account's region, running rclone inside tmux. Sibling of
gcs-azure-transfer — same engine, same five operations, same pauses. The
company must already be onboarded — `companies/<slug>/config.json` supplies
the destination; the user supplies only the slug (and optionally a Dropbox
folder path — default is everything the token can see).

All az/ssh/rclone mechanics live in `scripts/dropbox_transfer.py` (thin CLI
over `scripts/transfer_engine.py`) — never hand-roll them. Your job:
orchestration, judgment, the pause points, and the confirmation gates. Full
command templates + troubleshooting:
[references/commands.md](references/commands.md).

## HARD CONSTRAINTS — never violate

1. **Network rules are human-only.** NEVER run
   `az storage account network-rule add` (or any vnet change) unprompted.
   Surface what's needed and PAUSE for the user; run the az commands
   yourself only on an explicit user override. **Same-region caveat
   (learned 2026-08, song-division):** IP rules do NOT match traffic from
   a VM in the storage account's own region — the working config is the
   `Microsoft.Storage` service endpoint on the VM's subnet plus a
   vnet-rule on the SA. Say so at the pause, don't let the user chase IP
   rules.
2. **Dropbox auth is human-in-the-loop.** Never automate the Dropbox
   sign-in. The account owner/admin runs `rclone authorize "dropbox"` on
   their own machine; you wait for the user to paste the token.
3. **Static public IP, never deallocate.** Standard-SKU static IP; never
   stop/deallocate before final teardown.
4. **Secrets hygiene.** The SAS URL and Dropbox OAuth token live ONLY in
   the VM's `~/.config/rclone/rclone.conf` (mode 600) and die with the VM.
   The engine pipes them over ssh stdin; never echo them, never write them
   to files/tags/logs here.
5. **Confirmation gates.** Exact plan + explicit user confirmation before
   (a) VM creation and (b) teardown. Teardown is ALWAYS manual-confirm
   (the script refuses without `--confirmed`).
6. **Sanctioned WRITE path** into `<slug>-raw` (rwlc SAS, 21-day default)
   — only adds blobs under the dest prefix, never modifies existing data.

## No state file

Azure is the source of truth: VM name `xfer-dbx-<slug>` + tags
(`purpose=dropbox-transfer`, `engagement`, `dropbox_path`, `dest_prefix`).
At the START of any invocation, always run discovery first and report where
things stand:

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/dropbox_transfer.py discover <slug>
```

Phases: `pre-setup` → `mid-setup` → `setup-complete` → `transfer-running` →
`transfer-stopped`. Transfer state never touches `status.json`. The
`xfer-dbx-` prefix means a Dropbox and a GCS transfer can run side by side
for the same company.

## The five operations

### 1. setup `<slug>` `[dropbox path]`

```bash
python3 scripts/dropbox_transfer.py plan <slug> [--path "Team Folder/x"]
```

1. Show the plan (VM name/size/region/RG, source, dest, SAS expiry) →
   **GATE: confirm before creating billable resources.** Flags:
   `--vm-size` (default Standard_D8s_v7 — D8s_v5 is capacity-restricted in
   eastus; on SkuNotAvailable the script lists unrestricted alternatives),
   `--rg`, `--dest-prefix`, `--sas-days`.
2. `create-vm <slug> [--path ...]` — VM + bootstrap (rclone, tmux). A few
   minutes.
3. `write-azure-remote <slug>` — mints the container SAS locally (rwlc,
   21 days) and installs the `azure` remote on the VM.
4. **PAUSE #1 — network access.** Print the VM's public IP prominently and
   state BOTH needs: the IP entry via the internal network-rules UI, AND —
   because the VM is same-region — the `Microsoft.Storage` service
   endpoint + vnet-rule (internal UI if it supports it; az only on
   explicit user override). Wait.
5. On confirmation: `check-azure <slug>`. On 403 it inspects the ruleset
   (read-only) and reports `vm_vnet_rule_present` / `vm_ip_in_ruleset`:
   vnet-rule present = propagation, wait ~10s and retry; IP-only = the
   same-region trap, relay it; neither = the entry didn't land. Never a
   SAS problem — do not re-mint.
6. **PAUSE #2 — Dropbox token.** Give the user this snippet for whoever
   owns the Dropbox account (secure channel; the token grants access to
   the files it can see and can be revoked afterward under Dropbox
   Settings → Security → Connected apps):

   ```
   1. Install rclone on your machine:  https://rclone.org/downloads/
   2. Run:  rclone authorize "dropbox"
   3. Sign in with the Dropbox account that owns the data when the
      browser opens.
   4. Send back the token block it prints (the JSON between the --->
      markers), via a secure channel.
   ```

   For Dropbox Business, a member token sees that member's view (team
   folders included); rclone impersonation of other members is out of
   scope — get a token from an account that can see everything in scope.
   Wait for the paste.
7. Pipe the pasted token via stdin — never argv:

   ```bash
   python3 scripts/dropbox_transfer.py write-dropbox-remote <slug> <<'EOF'
   <pasted token JSON>
   EOF
   ```

   The script verifies the listing and reports total size + object count.
   Give a transfer-time estimate — Dropbox is rate-limited, so unlike GCS
   expect ~100–300 MiB/s, not GiB/s; many small files slow it further.
   No expiry clock on the source (unlike GCS exports).

### 2. transfer `<slug>`

```bash
python3 scripts/dropbox_transfer.py transfer <slug>
```

Starts rclone copy in tmux session `transfer` (8 transfers / 16 checkers /
5 retries / `--tpslimit 12` — Dropbox 429s aggressively; raising
`--transfers` usually makes it slower, not faster). Confirms the session is
alive and hands back — do NOT block. Re-running after an interruption
resumes safely. Refuses if already running.

### 3. status `<slug>` — the most-used command

```bash
python3 scripts/dropbox_transfer.py status <slug>
```

Report tight and scannable: running or not, bytes done / total, %,
throughput, ETA, error count, recent distinct errors. A steady trickle of
`too_many_requests` retries is normal; a wall of them means lower
`--transfers`. `looks_finished: true` → suggest verify.

### 4. verify `<slug>`

```bash
python3 scripts/dropbox_transfer.py verify <slug>
```

`rclone check --one-way`. Note: Dropbox and Azure share no common hash, so
this compares sizes (rclone says so in its output) — still catches missing
or truncated files. Clean → suggest teardown (still gated). Differences →
files changed mid-copy (Dropbox is live, not a frozen export); offer to
re-run transfer then re-verify. After a clean verify, size-company picks
the data up as the `dropbox-export` source.

### 5. teardown `<slug>`

1. Refuses while the transfer tmux session is alive (`--force` only on an
   explicit user override).
2. Show the deletion plan: VM + NIC + OS disk (delete-option-tied) +
   public IP + NSG + VNET, in RG `<rg>` → **GATE: always confirm**, then
   re-run with `--confirmed`.
3. Relay the reminder checklist verbatim: remove the IP rule AND the
   (now-stale) vnet-rule from the SA; note the SAS expiry; the account
   owner can revoke the rclone app under Dropbox connected apps.

## Judgment notes

- Discovery `vm-no-public-ip` → someone deallocated the VM; surface loudly.
- `vm-unreachable` right after create-vm = cloud-init still booting; wait
  ~60s and retry before diagnosing.
- Dropbox is a LIVE source — users keep editing while you copy. A verify
  with a handful of differences right after a long transfer is usually
  churn, not loss: re-run transfer (cheap, incremental) and re-verify.
- Millions of small files? Listing and per-file overhead dominate; budget
  hours and consider `--path` scoping per top-level folder.
- `--dry-run` on every subcommand prints the az/ssh commands instead of
  running them (secrets redacted).
