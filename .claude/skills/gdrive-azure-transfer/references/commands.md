# gdrive-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/gdrive_transfer.py` (thin CLI over
`scripts/transfer_engine.py`) runs under the hood. Reference it for
debugging and manual rescue; never bypass the script for normal operation.

## Script subcommand map

```bash
export PATH="/opt/homebrew/bin:$PATH"
S=python3\ scripts/gdrive_transfer.py    # all take <slug>; JSON on stdout

$S discover <slug>                       # phase + hints; run FIRST, always
$S plan <slug> [--path "Folder/x"] [--team-drive <id>]   # no changes
$S create-vm <slug> [--path ...] [--team-drive <id>]     # VM + bootstrap
$S allow-network <slug>                  # service endpoint + vnet-rule
$S write-azure-remote <slug>             # mint SAS, install [azure] remote
$S check-azure <slug>                    # rclone lsd sanity check, classified
$S write-gdrive-remote <slug> < token    # token on STDIN; verifies + sizes
#   add --oauth-client-json <path> when the token was minted with our own
#   Google OAuth app (client-secret JSON, Google Cloud download format) —
#   the token only refreshes against the client that minted it
$S transfer <slug>                       # start tmux rclone copy
#   --include <pattern> (repeatable) filters the copy — verify takes the
#   SAME flag so the check matches what was copied
$S status <slug>                         # liveness + parsed stats
$S verify <slug>                         # rclone check --one-way, parsed
$S teardown <slug> --confirmed           # delete everything (gated)
```

Common flags: `--dry-run`, `--root`, `--rg`, `--vm-size`, `--dest-prefix`,
`--sas-days`, `--transfers`, `--checkers`, `--include`. gdrive extras:
`--team-drive <id>` (Shared Drive) and `--shared-with-me true` (folder
shared TO the token's account — 'Shared with me' needs
`shared_with_me = true` in the conf or paths won't resolve). Exit codes: 0 ok, 1 hard error,
2 refusal / check-failed (JSON `cause` says why). Empty/omitted `--path` =
the root of My Drive, or of the Shared Drive when `--team-drive` is set.
Both values persist as VM tags; later commands need no flags.

## Underlying az templates

```bash
# VM create (region = storage account's location). Default size D8s_v7:
# D8s_v5 is capacity-restricted in eastus (learned 2026-08); on
# SkuNotAvailable the engine lists unrestricted same-family sizes.
az vm create -g <rg> -n xfer-gdr-<slug> --image Ubuntu2204 \
  --size Standard_D8s_v7 --public-ip-sku Standard \
  --accelerated-networking true --admin-username azureuser \
  --generate-ssh-keys --os-disk-delete-option Delete \
  --nic-delete-option Delete --location <region> \
  --tags purpose=gdrive-transfer engagement=<slug> gdrive_path=<path> \
         gdrive_team_drive=<id> dest_container=<slug>-raw \
         dest_prefix=gdrive-export

# Container SAS (local; value goes straight to the VM over ssh stdin)
az storage container generate-sas --account-name <sa> -n <slug>-raw \
  --permissions racwl --expiry <now+21d> --https-only -o tsv

# Same-region access (REQUIRED — IP rules never match same-region VM
# traffic). Engine-run by `allow-network`; teardown removes only our rule:
az network vnet subnet update -g <rg> --vnet-name xfer-gdr-<slug>VNET \
  -n xfer-gdr-<slug>Subnet --service-endpoints Microsoft.Storage
az storage account network-rule add -g <rg> --account-name <sa> \
  --vnet-name xfer-gdr-<slug>VNET --subnet xfer-gdr-<slug>Subnet

# Teardown (NIC + OS disk die with the VM via delete-options; PIP + NSG +
# VNET need explicit deletes)
az vm delete -g <rg> -n xfer-gdr-<slug> --yes
az network public-ip delete -g <rg> -n xfer-gdr-<slug>PublicIP
az network nsg delete -g <rg> -n xfer-gdr-<slug>NSG
az network vnet delete -g <rg> -n xfer-gdr-<slug>VNET
```

## Underlying VM-side templates

```bash
# rclone.conf sections (~/.config/rclone/rclone.conf, mode 600)
[azure]
type = azureblob
sas_url = https://<sa>.blob.core.windows.net/<slug>-raw?<sas>

[gdrive]
type = drive
scope = drive
token = {"access_token":"ya29.…","token_type":"Bearer","refresh_token":"…","expiry":"…"}
client_id = <id>.apps.googleusercontent.com   # only with --oauth-client-json:
client_secret = GOCSPX-…                      # our own app = our own API quota
team_drive = <id>            # only when a Shared Drive is targeted

# The copy (inside tmux session `transfer`) — Drive-API-friendly throttle
rclone copy 'gdrive:<path>' azure:<slug>-raw/gdrive-export \
  --transfers 8 --checkers 16 --retries 5 \
  --log-file=/home/azureuser/transfer.log --log-level INFO \
  --tpslimit 10 --tpslimit-burst 10 \
  --stats 1m --stats-log-level NOTICE

# Manual rescue / exploration on the VM
ssh azureuser@<ip>
tmux attach -t transfer                  # watch live (detach: Ctrl-b d)
tail -f ~/transfer.log
rclone backend drives gdrive:            # list Shared Drives the token sees
rclone lsd gdrive:                       # top-level of the configured drive
rclone check 'gdrive:<path>' azure:<slug>-raw/gdrive-export --one-way
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 AuthorizationFailure` on `azure:` | Storage firewall | check-azure classifies it: `vm_vnet_rule_present` true = propagation (wait ~10s, retry); IP listed but no vnet-rule = the same-region trap (needs service endpoint + vnet-rule, templates above); neither = the UI entry didn't land. NOT a SAS problem — never re-mint for a 403. |
| `AuthenticationFailed` / signature error on `azure:` | SAS expired/malformed/wrong container | Re-run `write-azure-remote`. |
| `invalid_grant` / `unauthorized` on `gdrive:` | Token revoked or from the wrong Google account — or minted with a different OAuth client than rclone.conf carries | Fresh `rclone authorize "drive"` from the owner (custom app: `rclone authorize "drive" "<client_id>" "<client_secret>"`); re-run `write-gdrive-remote` with the matching `--oauth-client-json`. |
| Listing near-empty but the Drive "has TBs" | Data lives in a Shared Drive, not My Drive | `rclone backend drives gdrive:` on the VM to list Shared Drive ids; re-run setup steps with `--team-drive <id>` (or pass it to `write-gdrive-remote`). |
| `userRateLimitExceeded` / 403 bursts | Drive API rate limiting | Expected in small doses (rclone pacer retries). A wall of them: lower `--tpslimit` or `--transfers` (e.g. 4). |
| `downloadQuotaExceeded` on specific files | Google per-file download quota (hot/large shared files) | Wait 24h and re-run transfer (resume skips completed files); persistent ones need the owner to copy the file. |
| "identified as malware or spam" errors | Drive abuse flag | Requires `--drive-acknowledge-abuse` — ask the user first, then add it to the rclone command via manual rescue. |
| `Duplicate object found in source` warnings | Drive allows same-name files in one folder | rclone copies the first, skips dupes. Surface the count; if dupes matter the owner must rename. |
| verify flags only .docx/.xlsx/.pptx files | Native Google Docs export on the fly — no fixed source size | Expected noise, not loss. Real binaries missing → re-run transfer, re-verify. |
| `rclone size` far below the Drive UI's number | Native Docs/Sheets/Slides report no size until exported | Say so in the estimate; the copied bytes will exceed `rclone size`. |
| ssh times out | VM booting, local IP changed, or VM deallocated | Wait ~60s after create; check `discover`; never deallocate. |
| `az vm create` traceback "content already consumed" | azure-cli bug masking a preflight error (usually SkuNotAvailable) | The engine surfaces the real cause + unrestricted sizes; re-run with the suggested `--vm-size`. |
| VM exists but tags empty | VM predates this harness or was made by hand | Pass `--path`/`--team-drive`/`--dest-prefix` explicitly to each command. |

## Secrets hygiene invariants (grep-testable)

- SAS and token never appear in: argv, az `--tags` (the team-drive ID is
  not a secret; the token is and never lands in tags), any file in this
  repo, any printed output (dry-run shows `(stdin: N bytes, redacted)`).
- They exist only in the VM's `rclone.conf` (600) and die at teardown.
- The pasted token transits: user chat → heredoc stdin → ssh stdin →
  rclone.conf. Never store it in scratch files.
