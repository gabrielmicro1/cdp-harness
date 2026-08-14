# gcs-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/gcs_transfer.py` runs under the hood.
Reference it for debugging and manual rescue; never bypass the script for
normal operation (one code path per behavior).

## Script subcommand map

```bash
export PATH="/opt/homebrew/bin:$PATH"
S=python3\ scripts/gcs_transfer.py    # all take <slug>; JSON on stdout

$S discover <slug>                    # phase + hints; run FIRST, always
$S plan <slug> --bucket <b>           # resolve dest + region; no changes
$S create-vm <slug> --bucket <b>      # VM + bootstrap (rclone, tmux)
$S write-azure-remote <slug>          # mint SAS, install [azure] remote
$S check-azure <slug>                 # rclone lsd sanity check, classified
$S write-gcs-remote <slug> < token    # token on STDIN; verifies + sizes
$S transfer <slug>                    # start tmux rclone copy
$S status <slug>                      # liveness + parsed stats
$S verify <slug>                      # rclone check --one-way, parsed
$S teardown <slug> --confirmed        # delete everything (gated)
```

Common flags: `--dry-run` (print commands, redact secrets), `--root`
(fixtures for tests), `--rg`, `--vm-size`, `--dest-prefix`, `--sas-days`,
`--transfers`, `--checkers`. Exit codes: 0 ok, 1 hard error, 2 refusal /
check-failed (JSON `cause` says why).

## Underlying az templates

```bash
# VM create (region = storage account's location). Default size D8s_v7:
# D8s_v5 is capacity-restricted in eastus (learned 2026-08); on
# SkuNotAvailable the engine lists unrestricted same-family sizes.
az vm create -g <rg> -n xfer-<slug> --image Ubuntu2204 \
  --size Standard_D8s_v7 --public-ip-sku Standard \
  --accelerated-networking true --admin-username azureuser \
  --generate-ssh-keys --os-disk-delete-option Delete \
  --nic-delete-option Delete --location <region> \
  --tags purpose=gcs-transfer engagement=<slug> gcs_bucket=<bucket> \
         dest_container=<slug>-raw dest_prefix=workspace-export

# Container SAS (local; value goes straight to the VM over ssh stdin)
az storage container generate-sas --account-name <sa> -n <slug>-raw \
  --permissions racwl --expiry <now+21d> --https-only -o tsv
# racwl = read/add/create/write/list in service-canonical order ("rwlc"
# from the spec, plus harmless add; delete deliberately absent)

# Same-region access (REQUIRED — IP rules never match same-region VM
# traffic; human/UI-first, az only on explicit user override)
az network vnet subnet update -g <rg> --vnet-name xfer-<slug>VNET \
  -n xfer-<slug>Subnet --service-endpoints Microsoft.Storage
az storage account network-rule add -g <rg> --account-name <sa> \
  --vnet-name xfer-<slug>VNET --subnet xfer-<slug>Subnet

# Teardown (NIC + OS disk die with the VM via delete-options; PIP + NSG +
# VNET use az vm create's default names and need explicit deletes)
az vm delete -g <rg> -n xfer-<slug> --yes
az network public-ip delete -g <rg> -n xfer-<slug>PublicIP
az network nsg delete -g <rg> -n xfer-<slug>NSG
az network vnet delete -g <rg> -n xfer-<slug>VNET
```

## Underlying VM-side templates

```bash
# rclone.conf sections (~/.config/rclone/rclone.conf, mode 600)
[azure]
type = azureblob
sas_url = https://<sa>.blob.core.windows.net/<slug>-raw?<sas>

[gcs]
type = google cloud storage
token = {"access_token":"...","token_type":"Bearer","refresh_token":"...","expiry":"..."}

# The copy (inside tmux session `transfer`)
rclone copy gcs:<bucket> azure:<slug>-raw/workspace-export \
  --transfers 32 --checkers 64 --retries 5 \
  --log-file=/home/azureuser/transfer.log --log-level INFO \
  --stats 1m --stats-log-level NOTICE

# Manual rescue on the VM
ssh azureuser@<ip>
tmux attach -t transfer          # watch live (detach: Ctrl-b d)
tail -f ~/transfer.log
rclone check gcs:<bucket> azure:<slug>-raw/workspace-export --one-way
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 AuthorizationFailure` on `azure:` | VM IP not in the storage firewall, or rule not propagated | check-azure reports `vm_ip_in_ruleset`: true = propagation, wait ~10s and retry; false = the internal-UI entry didn't land — re-check account name + IP with the user. NOT a SAS problem — never re-mint for a 403. |
| 403 persists with the IP **in** the ruleset | Same-region VM: Azure IP rules don't match same-region traffic (arrives via backbone with a private source) | Needs the `Microsoft.Storage` service endpoint on the VM's subnet + a vnet-rule on the SA (templates above). Confirmed on song-division 2026-08: IP rule alone never clears; vnet rule clears in ~30s. |
| `AuthenticationFailed` / signature error on `azure:` | SAS expired, malformed, or wrong container | Re-run `write-azure-remote` (new SAS). |
| `403` / `AccessDenied` on `gcs:` | Token from the wrong Google account (not the Workspace super admin) | Admin re-runs `rclone authorize` signed into the right account. |
| `oauth2: token expired` / `invalid_grant` on `gcs:` | Token revoked or stale refresh token | Get a fresh token block from the admin; re-run `write-gcs-remote`. |
| `404` / bucket not found on `gcs:` | Bucket name typo, or the export expired (~60 days; early packets sooner) | Check the name; if expired, the customer must re-run the export. |
| ssh times out | VM still booting (right after create), your local IP changed, or VM deallocated | Wait ~60s after create; check `discover` power state; never deallocate. |
| tmux session died instantly | rclone config error — bad remote name or conf | `tail ~/transfer.log` and `rclone listremotes` on the VM. |
| Throughput low, zero errors | Many small objects; Google-side per-object overhead | Normal for Workspace exports. Raising `--transfers` helps modestly. |
| Errors climbing in status | Transient Google 429/5xx are retried; persistent = expiring packets | Distinct messages are in `status` output; if `expired`/`404`, verify early and salvage what remains. |
| verify shows differences | Export still being written during copy, or packets expired | Re-run `transfer` (resume is safe), re-verify. |
| VM exists but tags empty | VM predates this harness or was made by hand | Pass `--bucket`/`--dest-prefix` explicitly to each command. |

## Secrets hygiene invariants (grep-testable)

- SAS and token never appear in: argv, az `--tags`, any file in this repo,
  any printed output (dry-run shows `(stdin: N bytes, redacted)`).
- They exist only in the VM's `rclone.conf` (600) and die at teardown.
- The pasted token transits: user chat → heredoc stdin → ssh stdin →
  rclone.conf. Never store it in scratch files.
