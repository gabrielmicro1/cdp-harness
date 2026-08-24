# github-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/github_transfer.py` runs under the hood
(VM lifecycle via `scripts/transfer_engine.py`, pull layer via the
VM-side `scripts/github_vm_pull.py`). Reference it for debugging and
manual rescue; never bypass the script for normal operation (one code
path per behavior).

## Script subcommand map

```bash
export PATH="/opt/homebrew/bin:$PATH"
S=python3\ scripts/github_transfer.py  # all take <slug>; JSON on stdout

$S discover <slug>                     # phase + hints; run FIRST, always
$S probe <slug> --login <org> < PAT    # day-one gate, laptop-side, no VM:
                                       # token, org visibility/approval,
                                       # repo census, size floor, LFS scan
$S plan <slug> --login <org>           # resolve dest + region; no changes
$S create-vm <slug> --login <org>      # VM + bootstrap (git, git-lfs,
                                       # azcopy, tmux); 512 GB OS disk
$S allow-network <slug>                # service endpoint + vnet-rule
$S write-dest <slug>                   # mint SAS -> [azure] remote + dest.env
$S check-azure <slug>                  # rclone lsd sanity check, classified
$S write-token <slug> < 1-line         # PAT on STDIN -> 600 github.env
                                       # + /rate_limit smoke test from VM
$S transfer <slug> --limit 2           # pilot: 2 repos end-to-end
$S transfer <slug>                     # full pull in tmux; resume-safe
$S status <slug>                       # progress/manifest/log tail/disk
$S verify <slug>                       # LAPTOP-side: manifest vs blobs
$S teardown <slug> --confirmed         # delete everything (gated)
```

Stdin heredocs for the secret-taking subcommands (never argv):

```bash
python3 scripts/github_transfer.py probe <slug> --login <org> <<'EOF'
<the fine-grained PAT>
EOF
python3 scripts/github_transfer.py write-token <slug> <<'EOF'
<the fine-grained PAT>
EOF
```

Common flags: `--dry-run` (print commands, redact secrets), `--root`
(fixtures for tests), `--rg`, `--vm-size`, `--os-disk-gb`,
`--dest-prefix`, `--sas-days`, `--owner-type org|user`,
`--lfs-check-limit` (probe), `--limit`/`--only`/`--refresh`/
`--skip-upload` (transfer).
Exit codes: 0 ok, 1 hard error, 2 refusal / check-failed (JSON `cause`
says why). Re-running transfer is always safe: per-repo `.cdp-complete`
markers + azcopy `--overwrite=false` make resume the default.

## Underlying az templates

```bash
# VM create — engine template plus the one github difference: a 512 GB
# OS disk (staging holds the entire corpus — clones + LFS + JSONL —
# before azcopy uploads it)
az vm create -g <rg> -n xfer-gh-<slug> --image Ubuntu2204 \
  --size Standard_D8s_v7 --public-ip-sku Standard \
  --accelerated-networking true --admin-username azureuser \
  --generate-ssh-keys --os-disk-delete-option Delete \
  --nic-delete-option Delete --os-disk-size-gb 512 --location <region> \
  --tags purpose=github-transfer engagement=<slug> gh_login=<org> \
         gh_owner_type=org dest_container=<slug>-raw \
         dest_prefix=github-export

# Container SAS (minted locally; the value rides ssh stdin to the VM)
az storage container generate-sas --account-name <sa> -n <slug>-raw \
  --permissions racwl --expiry <now+21d> --https-only -o tsv
# racwl = read/add/create/write/list; delete deliberately absent — the
# server-enforced half of the write invariant

# Same-region access (engine-run by `allow-network`; teardown removes
# only our rule — IP rules never match same-region VM traffic)
az network vnet subnet update --ids <vm-subnet-id> \
  --service-endpoints <existing...> Microsoft.Storage
az storage account network-rule add -g <rg> --account-name <sa> \
  --subnet <vm-subnet-id>

# Teardown (NIC + OS disk die with the VM via delete-options)
az vm delete -g <rg> -n xfer-gh-<slug> --yes
az network public-ip delete -g <rg> -n xfer-gh-<slug>PublicIP
az network nsg delete -g <rg> -n xfer-gh-<slug>NSG
az network vnet delete -g <rg> -n xfer-gh-<slug>VNET
```

Verify is the one laptop-side leg (the VM is normally gone): it uses the
sizing-family firewall + SAS —

```bash
az storage account network-rule add -g <rg> --account-name <sa> \
  --ip-address <laptop-ip>          # only if not already allowed; ~60s
az storage account generate-sas --services b --resource-types sco \
  --permissions rl --expiry <now+1d> --https-only -o tsv
# rule removed after — only if we added it this run
```

## VM-side layout

```
/home/azureuser/
  .config/xfer/github.env      # GITHUB_TOKEN='...' (600)
  .config/xfer/dest.env        # AZURE_DEST_URL/SAS/CONTAINER/PREFIX (600)
  .config/rclone/rclone.conf   # [azure] sas_url (600; check-azure only)
  xfer-gh/
    github_vm_pull.py          # pushed fresh on every transfer
    pull.log                   # the puller's timestamped log
    dest/                      # staging root == container layout
      repos/<name>.git/        # mirror clones (+ .cdp-complete marker)
      wikis/<name>.wiki.git/   # wiki clones, when the wiki exists
      json/<name>/{issues,pulls,issue_comments,review_comments}.jsonl
      manifest.json            # written BEFORE upload — rides the same
                               # azcopy job; verify reads it from blob
      progress.json            # {phase, done, total, message} heartbeat
```

Container layout: the same tree under `<slug>-raw/github-export/`
(azcopy's trailing `/*` copies dir contents, so the prefix isn't
nested).

## Manual rescue (VM alive, script unavailable)

```bash
ssh azureuser@<vm-ip>
tail -50 ~/xfer-gh/pull.log            # what the puller is doing
cat ~/xfer-gh/dest/progress.json       # phase + repo i/N
python3 - <<'PY'                       # manifest summary
import json; m = json.load(open("/home/azureuser/xfer-gh/dest/manifest.json"))
print(m["repo_count"], m["total_clone_bytes"], m["failed_repos"])
PY
# re-run azcopy by hand (idempotent, --overwrite=false skips landed):
set -a; . ~/.config/xfer/dest.env; set +a
azcopy copy ~/xfer-gh/dest/* "$AZURE_DEST_URL?$AZURE_DEST_SAS" \
  --recursive --overwrite=false --log-level ERROR
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| probe: 401 | PAT invalid/expired | client re-issues the token |
| probe/pull: 403 with rate limit intact | PAT missing Contents/Issues/Pull requests: read, OR org hasn't approved it | client fixes scopes / org owner approves (Settings → Third-party Access) — never retry |
| probe: owner-visibility 404 | wrong `--login` or `--owner-type`, or unapproved PAT | confirm the org login with the client |
| pull.log: "rate-limited; sleeping Ns" | normal 5k req/h throttling on comment-heavy repos | nothing — it resumes itself; not a hang |
| clone FAILED, stderr has 403/Authentication failed | PAT lacks Contents: read | fatal by design; fix token, re-run transfer |
| json 404 on one endpoint | issues/PRs disabled on that repo | normal skip — nothing to do |
| wiki "absent" | has_wiki on but wiki never created | normal skip — nothing to do |
| write-token smoke test != 200 | token bad, or VM can't reach api.github.com | fix and re-run write-token (it replaces the env file) |
| check-azure 403 | vnet-rule propagating or stripped | rule present = wait ~10s; missing = re-run allow-network; never re-mint the SAS |
| transfer: puller died immediately | env files missing / whoami failed | `tail ~/xfer-gh/pull.log`; resume setup at the missing step |
| upload FAILED in pull.log | azcopy job failed mid-run | re-run transfer — markers + `--overwrite=false` make it cheap |
| verify: no-manifest | pull never finished a pass (or `--skip-upload`) | status → transfer, then re-verify |
| verify: failed_repos / short_uploads | repos failed, or a partial upload | re-run transfer, then re-verify |
| verify: stale_extra only | re-clone made different packfiles; create-only kept old ones | informational — no action needed |
| verify 403 on listing | laptop IP-rule propagation | the script retries; never re-mint the SAS |

## Secrets hygiene invariants (grep-testable)

- The PAT is read from stdin only; it never appears in argv, VM tags,
  logs, or any file on this machine.
- On the VM the PAT lives only in `~/.config/xfer/github.env` (600) and
  the puller's process environment; git sees it only via the 0700
  GIT_ASKPASS helper — clone URLs are the public `clone_url`, tokenless.
- The dest SAS lives only in `dest.env` + `rclone.conf` (600) on the VM;
  the puller never echoes raw azcopy output (a URL line would leak
  `sig=`).
- Everything dies with the VM at teardown; the SAS lapses on its own;
  the client revokes the PAT after verification.
