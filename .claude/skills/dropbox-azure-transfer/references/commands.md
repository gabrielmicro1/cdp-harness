# dropbox-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/dropbox_transfer.py` (thin CLI over
`scripts/transfer_engine.py`) runs under the hood. Reference it for
debugging and manual rescue; never bypass the script for normal operation.

## Script subcommand map

```bash
export PATH="/opt/homebrew/bin:$PATH"
S=python3\ scripts/dropbox_transfer.py   # all take <slug>; JSON on stdout

$S discover <slug>                       # phase + hints; run FIRST, always
$S plan <slug> [--path "Folder/x"]       # resolve dest + region; no changes
$S create-vm <slug> [--path ...]         # VM + bootstrap (rclone, tmux)
$S allow-network <slug>                  # service endpoint + vnet-rule
$S write-azure-remote <slug>             # mint SAS, install [azure] remote
$S check-azure <slug>                    # rclone lsd sanity check, classified
$S write-dropbox-remote <slug> < token   # token on STDIN; verifies + sizes
#   add --oauth-client-json companies/.oauth-client-dropbox.json when the
#   token was minted with our own Dropbox App (they must match to refresh)
$S transfer <slug>                       # start tmux rclone copy
$S status <slug>                         # liveness + parsed stats
$S verify <slug>                         # rclone check --one-way, parsed
$S teardown <slug> --confirmed           # delete everything (gated)
```

Common flags: `--dry-run` (print commands, redact secrets), `--root`,
`--rg`, `--vm-size`, `--dest-prefix`, `--sas-days`, `--transfers`,
`--checkers`, `--tpslimit` (source-API transactions/sec, burst = limit,
applies to transfer AND verify; dropbox default 12, 0 = uncapped). Exit codes: 0 ok, 1 hard error, 2 refusal / check-failed
(JSON `cause` says why). Empty/omitted `--path` = the whole Dropbox root
the token can see.

## Underlying az templates

```bash
# VM create (region = storage account's location). Default size D8s_v7:
# D8s_v5 is capacity-restricted in eastus (learned 2026-08); on
# SkuNotAvailable the engine lists unrestricted same-family sizes.
az vm create -g <rg> -n xfer-dbx-<slug> --image Ubuntu2204 \
  --size Standard_D8s_v7 --public-ip-sku Standard \
  --accelerated-networking true --admin-username azureuser \
  --generate-ssh-keys --os-disk-delete-option Delete \
  --nic-delete-option Delete --location <region> \
  --tags purpose=dropbox-transfer engagement=<slug> dropbox_path=<path> \
         dest_container=<slug>-raw dest_prefix=dropbox-export

# Container SAS (local; value goes straight to the VM over ssh stdin)
az storage container generate-sas --account-name <sa> -n <slug>-raw \
  --permissions racwl --expiry <now+21d> --https-only -o tsv

# Same-region access (REQUIRED — IP rules never match same-region VM
# traffic). Engine-run by `allow-network`; teardown removes only our rule:
az network vnet subnet update -g <rg> --vnet-name xfer-dbx-<slug>VNET \
  -n xfer-dbx-<slug>Subnet --service-endpoints Microsoft.Storage
az storage account network-rule add -g <rg> --account-name <sa> \
  --vnet-name xfer-dbx-<slug>VNET --subnet xfer-dbx-<slug>Subnet

# Teardown (NIC + OS disk die with the VM via delete-options; PIP + NSG +
# VNET use az vm create's default names and need explicit deletes)
az vm delete -g <rg> -n xfer-dbx-<slug> --yes
az network public-ip delete -g <rg> -n xfer-dbx-<slug>PublicIP
az network nsg delete -g <rg> -n xfer-dbx-<slug>NSG
az network vnet delete -g <rg> -n xfer-dbx-<slug>VNET
```

## Underlying VM-side templates

```bash
# rclone.conf sections (~/.config/rclone/rclone.conf, mode 600)
[azure]
type = azureblob
sas_url = https://<sa>.blob.core.windows.net/<slug>-raw?<sas>

[dropbox]
type = dropbox
token = {"access_token":"sl.…","token_type":"bearer","refresh_token":"…","expiry":"…"}

# The copy (inside tmux session `transfer`) — note the Dropbox-specific
# throttle (modest parallelism or Dropbox 429s), --fast-list (whole tree
# in a few recursive list calls, not one per directory) and --order-by
# (large bandwidth-bound files interleaved with small tps-bound ones).
rclone copy 'dropbox:<path>' azure:<slug>-raw/dropbox-export \
  --transfers 8 --checkers 16 --tpslimit 12 --tpslimit-burst 12 \
  --retries 5 --log-file=/home/azureuser/transfer.log --log-level INFO \
  --stats 1m --stats-log-level NOTICE --fast-list --order-by size,mixed

# Manual rescue on the VM
ssh azureuser@<ip>
tmux attach -t transfer          # watch live (detach: Ctrl-b d)
tail -f ~/transfer.log
rclone check 'dropbox:<path>' azure:<slug>-raw/dropbox-export \
  --tpslimit 12 --tpslimit-burst 12 --one-way
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 AuthorizationFailure` on `azure:` | Storage firewall | check-azure classifies it: `vm_vnet_rule_present` true = propagation (wait ~10s, retry); IP listed but no vnet-rule = the same-region trap (IP rules never match same-region VMs — needs service endpoint + vnet-rule, templates above); neither = the UI entry didn't land. NOT a SAS problem — never re-mint for a 403. |
| `AuthenticationFailed` / signature error on `azure:` | SAS expired, malformed, or wrong container | Re-run `write-azure-remote` (new SAS). |
| `401` / `invalid_access_token` on `dropbox:` | Token revoked, or pasted incompletely | Fresh `rclone authorize "dropbox"` from the owner; re-run `write-dropbox-remote`. |
| `path/not_found` on `dropbox:` | `--path` typo, or the folder isn't visible to the token's account | `rclone lsd dropbox:` on the VM to see what the token actually sees. |
| Constant `too_many_requests` / 429 | Dropbox rate limiting | Expected in small doses (rclone honors Retry-After). A wall of them: lower `--tpslimit` (or `--transfers`, e.g. 4). The structural fix is our own App ID (limits are per app + per user; rclone's default app is shared worldwide) — see below. |
| Throughput ~100–300 MiB/s, no errors | Normal Dropbox pace (API-bound, not pipe-bound) | Don't chase it; unlike GCS you won't see GiB/s. Our own App ID + `--tpslimit 20`–`24` is the sanctioned way to push higher. |
| Small-file corpus crawling despite low CPU/network | Per-file API calls are the ceiling (~tpslimit files/sec) | Custom App ID + raise `--tpslimit`; `--fast-list` is already on so listing isn't the drain. Do NOT run a second rclone on the same token — it shares the per-user budget and thrashes. |
| verify shows a few differences after a long copy | Dropbox is live — files changed mid-copy | Re-run `transfer` (incremental), re-verify. |
| verify output says "sizes only" | Dropbox and Azure share no common checksum | Expected — size comparison still catches missing/truncated files. |
| ssh times out | VM booting, your local IP changed, or VM deallocated | Wait ~60s after create; check `discover`; never deallocate. |
| `az vm create` traceback "content already consumed" | azure-cli bug masking a preflight error (usually SkuNotAvailable) | The engine now surfaces the real cause + unrestricted sizes; re-run with the suggested `--vm-size`. |
| VM exists but tags empty | VM predates this harness or was made by hand | Pass `--path`/`--dest-prefix` explicitly to each command. |

## Custom Dropbox App ID (per-app rate-limit budget)

Dropbox rate limits are per app + per user; rclone's built-in App ID is
shared by every rclone user on the internet. Our own app gets its own
budget — the biggest single throughput lever.

- Create once at https://www.dropbox.com/developers/apps : **Scoped
  access** + **Full Dropbox** (App-folder access would see none of the
  client's data), permissions `files.metadata.read` +
  `files.content.read` only, submit permissions BEFORE any token is
  minted (scopes bake into the token). No redirect URI setup, no Dropbox
  review needed (<500 linked users).
- Save App key/secret as `companies/.oauth-client-dropbox.json`
  (gitignored): `{"client_id": "<app key>", "client_secret": "<app secret>"}`.
- The owner authorizes with `rclone authorize "dropbox" "<app key>"
  "<app secret>"`, and `write-dropbox-remote` gets
  `--oauth-client-json companies/.oauth-client-dropbox.json`. Mismatched
  token/client fails at refresh (~4h in), not at install — always pair
  them.
- New apps can start with modest limits that relax with normal usage.

## Secrets hygiene invariants (grep-testable)

- SAS and token never appear in: argv, az `--tags`, any file in this repo,
  any printed output (dry-run shows `(stdin: N bytes, redacted)`).
- They exist only in the VM's `rclone.conf` (600) and die at teardown.
- The pasted token transits: user chat → heredoc stdin → ssh stdin →
  rclone.conf. Never store it in scratch files.
