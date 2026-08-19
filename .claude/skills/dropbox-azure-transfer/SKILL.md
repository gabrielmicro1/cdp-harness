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

1. **Network rules go through `allow-network` only.** The engine grants
   access itself (Microsoft.Storage service endpoint on the VM's subnet +
   a vnet-rule on the SA — IP rules never match same-region VM traffic;
   learned 2026-08, song-division) and teardown removes exactly the rule
   it added. Never hand-roll `network-rule add`, and NEVER remove a rule
   the engine didn't add — pre-existing rules are the client's own push
   path. If a 403 reappears mid-run, company infra may have stripped the
   rule: re-run `allow-network`.
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
4. `allow-network <slug>` — grants the VM storage access (service
   endpoint + vnet-rule). No pause: the engine runs this itself.
5. `check-azure <slug>`. On 403 it inspects the ruleset (read-only):
   vnet-rule present = propagation, wait ~10s and retry; missing =
   re-run allow-network. Never a SAS problem — do not re-mint.
6. **PAUSE #2 — Dropbox token.** First check for our own Dropbox App ID:
   Dropbox rate limits are per app + per user, and rclone's default App ID
   is shared by every rclone user on the internet — our own app gets its
   own rate-limit budget and is the single biggest throughput lever. If
   `companies/.oauth-client-dropbox.json` exists (flat
   `{"client_id": "<app key>", "client_secret": "<app secret>"}`,
   gitignored — never commit it), use the custom-app variant of step 2 in
   the snippet; otherwise ask the user whether to create one (Dropbox App
   Console: scoped app, Full Dropbox access, `files.metadata.read` +
   `files.content.read` only) or proceed on the shared default.

   Give the user this snippet for whoever owns the Dropbox account
   (secure channel; the token grants access to the files it can see and
   can be revoked afterward under Dropbox Settings → Security →
   Connected apps):

   ```
   1. Install rclone on your machine:  https://rclone.org/downloads/
   2. Run:  rclone authorize "dropbox"
      (with our own app: rclone authorize "dropbox" "<app key>" "<app secret>")
   3. Sign in with the Dropbox account that owns the data when the
      browser opens.
   4. Send back the token block it prints (the JSON between the --->
      markers), via a secure channel.
   ```

   For Dropbox Business, a member token sees that member's view (team
   folders included); rclone impersonation of other members is out of
   scope — get a token from an account that can see everything in scope.
   Wait for the paste.
7. Pipe the pasted token via stdin — never argv. Token and app must
   match: a token minted with our app key/secret MUST be installed with
   `--oauth-client-json`, or refresh will fail mid-transfer (and vice
   versa — a default-app token takes no flag):

   ```bash
   python3 scripts/dropbox_transfer.py write-dropbox-remote <slug> \
     --oauth-client-json companies/.oauth-client-dropbox.json <<'EOF'
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
5 retries / `--tpslimit 12` + matching burst / `--fast-list` /
`--order-by size,mixed` — Dropbox 429s aggressively; raising `--transfers`
usually makes it slower, not faster). `--fast-list` pages the whole tree in
a handful of calls instead of one list per directory, so the tps budget
goes to downloads; `--order-by size,mixed` keeps big bandwidth-bound files
flowing while small tps-bound files queue. `--tpslimit N` overrides the
cap (0 = uncapped): with our own App ID, 20–24 is worth trying — watch
status for 15 min and step back if 429 retries dominate. An occasional 429
is fine (rclone honors Retry-After); a wall of them is thrash. Confirms the
session is alive and hands back — do NOT block. Re-running after an
interruption resumes safely. Refuses if already running.

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
- Millions of small files? Per-file download calls dominate — the ceiling
  is ~`tpslimit` files/sec regardless of bandwidth; budget days, not
  hours, on the default cap, and push for the custom App ID + a higher
  `--tpslimit` before considering `--path` scoping.
- Brand-new Dropbox apps can start with modest limits that relax with
  normal usage — don't judge the custom App ID by its first hour.
- A second rclone/VM on the SAME token shares the per-user budget — it
  thrashes, never speeds up. Don't parallelize that way.
- `--dry-run` on every subcommand prints the az/ssh commands instead of
  running them (secrets redacted).
