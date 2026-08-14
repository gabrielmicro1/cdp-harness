---
name: gcs-azure-transfer
description: Use when transferring a Google Workspace Data Export / takeout bucket (GCS, dwt-takeout-export-*) into a company's Azure -raw container — "transfer <company>'s export", "spin up a transfer VM", "gcs to azure copy", or any status check, verification, or teardown of an existing transfer engagement.
---

# GCS → Azure transfer

Copies a Google Workspace Data Export bucket (GCS) into
`<slug>-raw/workspace-export/` via a temporary Azure VM (`xfer-<slug>`) in the
storage account's region, running rclone inside tmux. The company must already
be onboarded — `companies/<slug>/config.json` supplies the destination
(storage account, container, RG, subscription); the user supplies only the
slug and the GCS bucket name.

All az/ssh/rclone mechanics live in `scripts/gcs_transfer.py` (thin CLI
over `scripts/transfer_engine.py`, shared with dropbox-azure-transfer) —
never hand-roll them. Your job: orchestration, judgment, the pause points, and the
confirmation gates. Full command templates and the troubleshooting table:
[references/commands.md](references/commands.md).

## HARD CONSTRAINTS — never violate

1. **Network rules are human-only.** NEVER run
   `az storage account network-rule add` (or any vnet change) for this path.
   Company infrastructure strips rules not added through the internal UI.
   Surface the VM's IP and PAUSE for the user. (The sizing path's
   `ip_rule_ensure` is a different, pre-existing mechanism — do not borrow it
   here.)
2. **Google auth is human-in-the-loop.** Never automate Google sign-in. The
   customer admin runs `rclone authorize` on their own machine; you wait for
   the user to paste the token.
3. **Static public IP, never deallocate.** The VM is created with a
   Standard-SKU static IP. Never stop/deallocate it before final teardown — a
   changed IP silently breaks the manually-added allowlist entry.
4. **Secrets hygiene.** The SAS URL and OAuth token live ONLY in the VM's
   `~/.config/rclone/rclone.conf` (mode 600) and die with the VM. Never echo
   them, never write them to files/tags/logs here. The script pipes them over
   ssh stdin; keep it that way.
5. **Confirmation gates.** Show the exact plan and get explicit user
   confirmation before (a) VM creation and (b) teardown. Teardown is ALWAYS
   manual-confirm, even after clean verification (the script refuses without
   `--confirmed`).
6. **This is the sanctioned WRITE path** into `<slug>-raw` (rwlc SAS, 21-day
   default) — the one exception to the harness's read-only rule. It only adds
   blobs under the dest prefix; it never modifies or deletes existing data.

## No state file

Azure is the source of truth: VM name `xfer-<slug>` + tags
(`purpose=gcs-transfer`, `engagement`, `gcs_bucket`, `dest_prefix`). Sessions
span days — at the START of any invocation, always run discovery first and
report where things stand before doing anything:

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/gcs_transfer.py discover <slug>
```

Phases: `pre-setup` (no VM) → `mid-setup` (remotes incomplete) →
`setup-complete` → `transfer-running` → `transfer-stopped` (verify or
resume). Transfer state never touches `companies/<slug>/status.json`.

## The five operations

### 1. setup `<slug>` `<gcs-bucket>`

```bash
python3 scripts/gcs_transfer.py plan <slug> --bucket <gcs-bucket>
```

1. Show the plan (VM name/size/region/RG, dest, SAS expiry) → **GATE:
   confirm before creating billable resources.** Optional flags:
   `--vm-size` (default Standard_D8s_v7 — D8s_v5 is capacity-restricted
   in eastus; on SkuNotAvailable the script lists unrestricted
   alternatives), `--rg`, `--dest-prefix`, `--sas-days`.
2. `create-vm <slug> --bucket <bucket>` — creates the VM (static Standard
   IP, accelerated networking, Ubuntu LTS, ssh keys) and bootstraps
   rclone + tmux. Takes a few minutes.
3. `write-azure-remote <slug>` — mints the container SAS locally (rwlc,
   21 days) and installs the `azure` remote on the VM.
4. **PAUSE #1 — network rule.** Print the VM's public IP prominently:
   "Add this IP to storage account `<name>` via the internal network-rules
   UI, then tell me when done." Wait. **Same-region caveat (learned
   2026-08, song-division):** Azure IP rules do NOT match traffic from a
   VM in the storage account's own region — the working config is the
   `Microsoft.Storage` service endpoint on the VM's subnet plus a
   vnet-rule on the SA. Surface that need to the user; run the az
   commands yourself only on an explicit user override (see
   references/commands.md).
5. On confirmation: `check-azure <slug>`. On a 403 it reads the ruleset
   (read-only) and tells you which case you're in: `vm_ip_in_ruleset: true`
   = propagation — wait ~10s and retry; `false` = the internal-UI entry
   never landed (wrong account or typo) — back to the user, don't wait.
   Either way it's never a SAS problem — do not re-mint.
6. **PAUSE #2 — Google token.** Give the user this snippet for their
   customer admin (secure channel; the token grants read access to the
   export bucket and can be revoked afterward from the admin's Google
   account security page):

   ```
   1. Install rclone on your machine:  https://rclone.org/downloads/
   2. Run:  rclone authorize "google cloud storage"
   3. Sign in with your Google Workspace super admin account when the
      browser opens.
   4. Send back the token block it prints (the JSON between the --->
      markers), via a secure channel.
   ```

   Wait for the user to paste the token.
7. Pipe the pasted token via stdin — never argv:

   ```bash
   python3 scripts/gcs_transfer.py write-gcs-remote <slug> <<'EOF'
   <pasted token JSON>
   EOF
   ```

   The script verifies the bucket listing and reports total size + object
   count. Give a rough transfer-time estimate (assume ~0.5–1 Gbit/s
   effective from Google egress; e.g. 1 TB ≈ 2.5–5 h) and note the export's
   ~60-day expiry clock. Then: setup is complete, run transfer when ready.

### 2. transfer `<slug>`

```bash
python3 scripts/gcs_transfer.py transfer <slug>
```

Starts rclone copy inside tmux session `transfer` (32 transfers /
64 checkers / 5 retries, log to `~/transfer.log`). Confirms the session is
alive, reports the initial log tail, and hands back — do NOT block waiting
for completion. Interrupted earlier? The same command resumes safely (rclone
skips files already copied). Refuses if a transfer is already running.

### 3. status `<slug>` — the most-used command

```bash
python3 scripts/gcs_transfer.py status <slug>
```

Report tight and scannable: running or not, bytes done / total, %,
throughput, ETA, error count. If errors are accumulating, show the recent
distinct error messages the script surfaces. `looks_finished: true` (tmux
dead + final stats in log) → suggest verify.

### 4. verify `<slug>`

```bash
python3 scripts/gcs_transfer.py verify <slug>
```

`rclone check --one-way`. Clean = 0 differences → suggest teardown (still
gated). Differences → likely the export was still being written, or early
packets expired; offer to re-run transfer then re-verify. After a clean
verify, size-company will pick the new data up as the `workspace-export`
source on its next run.

### 5. teardown `<slug>`

1. Refuses while the transfer tmux session is alive (`--force` only on an
   explicit user override).
2. Show the deletion plan: VM + NIC + OS disk (delete-option-tied) +
   public IP + NSG, in RG `<rg>` → **GATE: always confirm**, then re-run
   with `--confirmed`.
3. Relay the script's reminder checklist verbatim: remove the IP from the
   storage account via the internal UI; note the SAS expiry date; the
   customer admin can revoke the OAuth grant.

## Judgment notes

- Discovery says `vm-no-public-ip` → someone deallocated the VM; the static
  IP association may be gone and the allowlist entry stale. Surface loudly.
- `vm-unreachable` right after create-vm = cloud-init still booting; wait
  ~60s and retry before diagnosing.
- Throughput well below ~100 MiB/s on a D8s_v5 with no errors is usually
  Google-side throttling of many small objects — normal for Workspace
  exports; don't chase it.
- The export bucket expires ~60 days after export start (early packets
  sooner) — if setup stalls for days waiting on the token, remind the user
  of the clock.
- `--dry-run` on every subcommand prints the az/ssh commands instead of
  running them (secrets redacted) — use it to show the user exactly what
  will run.
