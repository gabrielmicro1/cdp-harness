# s3-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/s3_transfer.py` runs under the hood
(VM lifecycle via `scripts/transfer_engine.py`, copy layer via the
VM-side `scripts/azcopy-runner.sh`). Reference it for debugging and
manual rescue; never bypass the script for normal operation (one code
path per behavior).

## Script subcommand map

```bash
export PATH="/opt/homebrew/bin:$PATH"
S=python3\ scripts/s3_transfer.py     # all take <slug>; JSON on stdout

$S discover <slug>                    # phase + hints; run FIRST, always
$S plan <slug> --bucket <b>           # resolve dest + region; no changes
$S create-vm <slug> --bucket <b>      # VM + bootstrap (rclone, azcopy, tmux)
$S allow-network <slug>               # service endpoint + vnet-rule
$S write-dest <slug>                  # mint SAS -> [azure] remote + dest.env
$S check-azure <slug>                 # rclone lsd sanity check, classified
$S write-s3-creds <slug> < 2-lines    # key id + secret on STDIN; region
                                      # detect + secretless [s3] remote
$S probe <slug>                       # day-one gate: listing, requester-
                                      # pays, tier histogram, prefix survey
$S plan-jobs <slug>                   # split bucket -> jobs.txt/queue.txt
                                      # FLAT bucket: launch/poll/collect —
                                      # 1st call starts the sharded listing
                                      # (tmux window 'plan'), re-runs poll,
                                      # then auto-split into L/Q chunk jobs
$S transfer <slug> --pilot            # ONE job in one window: calibrate
$S transfer <slug>                    # K workers drain the queue in tmux
$S status <slug>                      # jobs/objects/bytes/rate/ETA/disk
$S verify <slug> [--deep|--prefix p]  # rollup S3 vs Azure (tmux) / spot
$S teardown <slug> --confirmed        # delete everything (gated)
```

Common flags: `--dry-run` (print commands, redact secrets), `--root`
(fixtures for tests), `--rg`, `--vm-size`, `--os-disk-gb`,
`--dest-prefix`, `--sas-days`, `--split-depth`/`--min-jobs`/`--max-jobs`
(plan-jobs), `--flat`/`--no-flat`/`--chunk-objects`/`--relist`
(plan-jobs, flat buckets), `--windows`/`--concurrency`/`--requeue-failed`
(transfer).
Exit codes: 0 ok, 1 hard error, 2 refusal / check-failed (JSON `cause`
says why).

## Underlying az templates

```bash
# VM create — engine template plus the one s3 difference: a 512 GB OS
# disk (azcopy job-plan files run ~0.5-1 KB/object; the stock 30 GB
# disk would fill and wedge a many-million-object run)
az vm create -g <rg> -n xfer-s3-<slug> --image Ubuntu2204 \
  --size Standard_D8s_v7 --public-ip-sku Standard \
  --accelerated-networking true --admin-username azureuser \
  --generate-ssh-keys --os-disk-delete-option Delete \
  --nic-delete-option Delete --os-disk-size-gb 512 --location <region> \
  --tags purpose=s3-transfer engagement=<slug> s3_bucket=<bucket> \
         dest_container=<slug>-raw dest_prefix=s3-export

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
az vm delete -g <rg> -n xfer-s3-<slug> --yes
az network public-ip delete -g <rg> -n xfer-s3-<slug>PublicIP
az network nsg delete -g <rg> -n xfer-s3-<slug>NSG
az network vnet delete -g <rg> -n xfer-s3-<slug>VNET
```

## Underlying VM-side layout & templates

```bash
# Secrets (both 600, both die with the VM)
~/.config/xfer/aws.env    # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
                          # AWS_REGION / S3_BUCKET / S3_SRC_URL
~/.config/xfer/dest.env   # AZURE_DEST_URL / AZURE_DEST_SAS /
                          # AZURE_DEST_CONTAINER / AZURE_DEST_PREFIX
~/.config/rclone/rclone.conf
  [azure]  type = azureblob / sas_url = https://<sa>...?<sas>
  [s3]     type = s3 / provider = AWS / env_auth = true / region = <r>
           # SECRETLESS — rclone reads the same aws.env azcopy does

# Work tree (~/xfer-jobs/)
jobs.txt        # immutable master job list ("R\t<prefix>" / "S\t<prefix>"
                #   / flat: "L\t<chunk>" / "Q\t<quarantine manifest>")
queue.txt       # consumable copy the workers drain (flock-popped)
done.txt        # TSV ledger: ts type prefix jobid completed failed
failed.txt      #   skipped bytes seconds — one row per finished job
inflight/wN     # the job worker N is on right now (transfer sweeps
                #   orphaned markers back into the queue at start)
wN.log          # per-worker heartbeat
plans/ logs/    # AZCOPY_JOB_PLAN_LOCATION / AZCOPY_LOG_LOCATION
verify.tsv      # rollup rows; last line "#done <ts> ok=N bad=M" (flat
                #   verify adds s3/az count+byte kv and #progress rows)
runner.sh       # scripts/azcopy-runner.sh, pushed at transfer/verify time
s3_flat.py      # scripts/s3_flat.py, pushed alongside (flat-bucket
                #   listing / split / manifest verify)
listing.txt     # flat: the cutoff manifest ("key\tsize", sorted);
listing.done    #   sentinel JSON {keys, unsafe}; plan.log = heartbeats
chunks/         # flat: chunk-NNNNN + quarantine-NNNNN manifests

# The copy each L (flat chunk) job runs — server-side Put Blob From URL
# per key (s3_flat.py; presigned S3 GET source, If-None-Match: * commit;
# azcopy list-of-files enumerates sequentially and is not used here):
python3 "$BASE/s3_flat.py" copy-chunk <chunk> <concurrency>

# The copy each R job runs (server-side; bytes go S3 -> Azure fabric)
azcopy copy "$S3_SRC_URL/<prefix>/" \
  "$AZURE_DEST_URL/<prefix>?$AZURE_DEST_SAS" \
  --recursive --overwrite=false --log-level ERROR
# S jobs (loose files at a split level) stream via rclone --max-depth 1

# Manual rescue on the VM
ssh azureuser@<ip>
tmux attach -t transfer            # watch live (detach: Ctrl-b d)
wc -l ~/xfer-jobs/queue.txt ~/xfer-jobs/done.txt ~/xfer-jobs/failed.txt
tail -2 ~/xfer-jobs/w*.log
azcopy jobs list                   # anything InProgress?
df -h ~/xfer-jobs                  # plan-file disk headroom
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 AuthorizationFailure` on `azure:` | vnet-rule missing or propagating | check-azure classifies it: `vm_vnet_rule_present` true = propagation, wait ~10s; false = re-run allow-network. NOT a SAS problem — never re-mint for a 403. |
| 403 reappears mid-run | External reconciler stripped the rule (rules added outside the internal UI can be) | Re-run `allow-network`; workers retry and continue. |
| `AuthenticationFailed` / signature error on `azure:` | SAS expired or malformed | Re-run `write-dest` (new SAS into both consumers). |
| `AccessDenied` listing `s3:` | Wrong/inactive key, policy missing `s3:ListBucket`, or requester-pays | `probe` separates requester-pays from bad-key. Key/policy problems are a client conversation, not a retry. |
| Probe flags requester-pays | Bucket owner charges the requester | azcopy support UNVERIFIED — pilot one prefix; if it fails, client flips the payer setting or the (slow) rclone `--s3-requester-pays` path. |
| Jobs failing `InvalidObjectState` | GLACIER / DEEP_ARCHIVE objects — server-side copy can't read cold storage | Quantify from failed.txt, client restores, then `transfer --requeue-failed`. GLACIER_IR is NOT affected. |
| `CannotVerifyCopySource` mid-job | Presigned S3 URL expired during a long copy | azcopy re-signs and retries on its own; only chase it if a job hard-fails on it repeatedly. |
| `SlowDown` (S3) / 503 (Azure) climbing | Request-rate ceiling | Turn DOWN `--windows` or `--concurrency`; do not add retries. SA default ~20k req/s is support-ticket-raisable. |
| `workdir_free_gb` shrinking fast | Plan files piling up — `azcopy jobs rm` not reclaiming | `azcopy jobs list`; remove finished jobs by hand; worst case `rm -rf ~/xfer-jobs/plans/*` while no worker runs. |
| Workers die instantly | aws.env/dest.env missing (setup incomplete) or queue empty | `discover` names the missing step; `transfer` refuses on an empty queue with the plan-jobs hint. |
| Huge skip counts on re-run | `--overwrite=false` skipping already-landed blobs | Normal — that IS resume. Skips are not errors. |
| verify MISMATCH rows | Failed/interrupted jobs, or the bucket changed mid-copy | `transfer --requeue-failed` (or plain transfer), then re-verify. |
| Bucket name contains dots | Virtual-host TLS breaks (`a.b.s3.amazonaws.com` wildcard mismatch) | Surface to the user — needs path-style URLs, a deliberate change, not a silent hack. |
| VM exists but tags empty | VM predates the harness or was made by hand | Pass `--bucket`/`--dest-prefix` explicitly to each command. |

## Secrets hygiene invariants (grep-testable)

- The AWS key pair and SAS never appear in: argv on the laptop, az
  `--tags`, any file in this repo, any printed output (dry-run shows
  `(stdin: N bytes, redacted)`).
- The key transits: client secure channel → heredoc stdin → ssh stdin →
  `aws.env` (600). Never store it in scratch files.
- rclone's `[s3]` remote is secretless (`env_auth = true`) — one secret
  file, two consumers; deleting the VM deletes every copy.
- On the VM only, the composed dest URL (SAS included) is visible in the
  azcopy process argv (`ps`) for the duration of a job — same trust
  domain as rclone.conf, and azcopy redacts `sig=` in its own logs.
