---
name: s3-azure-transfer
description: Use when transferring a company's AWS S3 bucket into their Azure -raw container — "transfer <company>'s s3", "s3 to azure", "pull their s3 bucket", "copy the images bucket", or any probe, status check, verification, or teardown of an existing s3 transfer engagement.
---

# S3 → Azure transfer

Copies an AWS S3 bucket into `<slug>-raw/s3-export/` via a temporary Azure
VM (`xfer-s3-<slug>`) in the storage account's region — the **fourth VM
ingest** (after gcs/dropbox/gdrive), sharing transfer_engine.py's VM
lifecycle but NOT its copy layer: the copy is **azcopy server-to-server**
(presigned S3 GETs driving Put Blob/Block From URL), so the storage fabric
pulls from S3 directly and the media bytes never transit the VM — only
control calls do. That is the whole point: rclone (like any streaming
copy, including a client's own) pushes every byte through one machine and
is request- AND bandwidth-bound; azcopy S2S is request-bound only, which
on a many-million-small-object bucket is the difference between months and
days. The company must already be onboarded — `companies/<slug>/config.json`
supplies the destination; the user supplies the slug and the bucket name.

All az/ssh/azcopy mechanics live in `scripts/s3_transfer.py` (engine
lifecycle reused from `scripts/transfer_engine.py`, azcopy job
orchestration of its own, VM-side worker `scripts/azcopy-runner.sh`) —
never hand-roll them. Your job: orchestration, judgment, the pause point,
the probe gate, and the confirmation gates. Full command templates +
troubleshooting: [references/commands.md](references/commands.md).

**What server-side copy cannot give (say so up front):** objects in
**GLACIER or DEEP_ARCHIVE** storage classes — the copy fails
`InvalidObjectState` until the client restores them; out of scope until
restored (GLACIER_IR is fine — it reads in real time). Probe samples the
tier histogram day one. **Requester-pays buckets**: probe detects them,
but azcopy's requester-pays support is unverified — pilot one prefix
before promising a timeline, or ask the client to flip the payer setting.
**No content-hash verification at scale**: S3 multipart ETags aren't
MD5s, so byte-exact size (which azcopy's `--check-length` commits per
object) is the honest invariant — never imply hash validation. Only
current object versions are copied; delete markers and noncurrent
versions are not.

## HARD CONSTRAINTS — never violate

1. **Network rules go through `allow-network` only.** The engine grants
   access itself (Microsoft.Storage service endpoint on the VM's subnet +
   a vnet-rule on the SA — IP rules never match same-region VM traffic)
   and teardown removes exactly the rule it added. Never hand-roll
   `network-rule add`, and NEVER remove a rule the engine didn't add.
   (The sizing path's `ip_rule_ensure` is a different mechanism — do not
   borrow it.)
2. **Secrets hygiene.** The read-only AWS key arrives from the client via
   a secure channel and goes to the script as **2 stdin lines** (key id,
   then secret) — never argv, tags, logs, or laptop files. On the VM it
   lives in `~/.config/xfer/aws.env` (600); the dest SAS lives in
   `~/.config/xfer/dest.env` (600) and rclone.conf; all die with the VM.
   One honest caveat: the composed SAS URL appears in VM-local process
   argv while an azcopy job runs (visible to `ps` on the VM only — the
   same trust domain as rclone.conf; azcopy redacts `sig=` in its logs).
3. **Static public IP, never deallocate.** Standard-SKU static IP; never
   stop/deallocate before final teardown.
4. **The write invariant, stated honestly.** The SAS is racwl — **no
   delete permission, server-enforced**. No-overwrite comes from
   `--overwrite=false`, pinned in the runner — **client-side**, not the
   API-enforced `If-None-Match: *` the qwilr/vimeo/zoom pulls use. Never
   claim create-only semantics the storage API isn't enforcing here; the
   honest sentence is "the SAS cannot delete, and the pinned copy command
   never overwrites."
5. **Confirmation gates.** Show the exact plan and get explicit user
   confirmation before (a) VM creation and (b) teardown. Teardown is
   ALWAYS manual-confirm, even after clean verification (the script
   refuses without `--confirmed`).
6. **This is the sanctioned WRITE path** into `<slug>-raw` (racwl SAS,
   21-day default) — the one exception to the harness's read-only rule.
   It only adds blobs under the dest prefix; it never modifies or deletes
   existing data.

## No state file

Azure is the source of truth: VM name `xfer-s3-<slug>` + tags
(`purpose=s3-transfer`, `engagement`, `s3_bucket`, `dest_prefix`). The
job queue and progress ledger (`~/xfer-jobs/` on the VM) are working
state that dies with the VM. Sessions span days — at the START of any
invocation, always run discovery first and report where things stand:

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/s3_transfer.py discover <slug>
```

Phases: `pre-setup` (no VM) → `mid-setup` (creds/dest incomplete) →
`setup-complete` → `jobs-planned` → `transfer-running` /
`verify-running` → `transfer-stopped` (resume or verify). Transfer state
never touches `companies/<slug>/status.json`.

## The six operations

### 1. setup `<slug>` `<bucket>`

```bash
python3 scripts/s3_transfer.py plan <slug> --bucket <bucket>
```

1. Show the plan (VM name/size/region/RG, dest, SAS expiry) → **GATE:
   confirm before creating billable resources.** Optional flags:
   `--vm-size` (default Standard_D8s_v7 — the workload is control-plane
   only, but K parallel azcopy jobs want the 32 GB of memory),
   `--os-disk-gb` (default 512 — azcopy job-plan files run ~0.5–1 KB per
   object; the stock 30 GB disk would wedge a big run), `--rg`,
   `--dest-prefix`, `--sas-days`.
2. `create-vm <slug> --bucket <bucket>` — VM + bootstrap (rclone, azcopy,
   tmux). Takes a few minutes.
3. `allow-network <slug>` — service endpoint + vnet-rule (engine-run, no
   pause).
4. `write-dest <slug>` — mints the container SAS locally (racwl, 21
   days) and installs it twice on the VM: the rclone `[azure]` remote
   (listing/verify) and `dest.env` (azcopy's dest URL).
5. `check-azure <slug>`. On a 403 it reads the ruleset (read-only):
   vnet-rule present = propagation — wait ~10s and retry; missing =
   re-run allow-network. Never a SAS problem — do not re-mint.
6. **PAUSE — the read-only AWS key.** Give the user this snippet for the
   client (secure channel):

   ```
   1. In AWS IAM, create a new user (no console access) with this
      policy, replacing BUCKET with the bucket name:
      {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Action": ["s3:GetObject", "s3:ListBucket"],
        "Resource": ["arn:aws:s3:::BUCKET",
                     "arn:aws:s3:::BUCKET/*"]}]}
   2. Create an access key for that user.
   3. Send the Access Key ID and Secret Access Key via a secure
      channel.
   4. You can delete the key the moment we confirm verification —
      we'll tell you when.
   ```

   Wait for the paste. Then pipe both lines via stdin — never argv:

   ```bash
   python3 scripts/s3_transfer.py write-s3-creds <slug> <<'EOF'
   <Access Key ID>
   <Secret Access Key>
   EOF
   ```

   The script detects the bucket's region, installs a SECRETLESS rclone
   `[s3]` remote (env_auth — one secret file, two consumers), and smoke-
   tests the listing.
7. `probe <slug>` — the day-one gate: listing works, requester-pays
   detection, storage-class histogram of the first ~20k objects
   (GLACIER/DEEP_ARCHIVE = a client conversation, not a retry), and a
   top-level prefix survey. Report the findings and the caveats from the
   probe's `notes` before promising anything.

### 2. plan-jobs `<slug>`

```bash
python3 scripts/s3_transfer.py plan-jobs <slug>
```

Splits the bucket into per-prefix azcopy jobs (one plan file per job
stays bounded; a single job over 300M objects would build a 100+ GB
plan). Auto-deepens to second-level prefixes when the top level is too
coarse (`--split-depth`/`--min-jobs`/`--max-jobs` to override); every
split level also gets a shallow job for its loose files, so nothing is
orphaned. Writes `jobs.txt` (immutable master, verify iterates it) and
`queue.txt` (the consumable copy) on the VM. Refuses to clobber recorded
progress without `--rebuild`.

**FLAT buckets** (files at bucket root, no folder prefixes — probe's
`flat_suspected`, auto-detected here from a 1000-entry root sample;
`--flat`/`--no-flat` to force): prefix-splitting is impossible, so
plan-jobs becomes a **launch → poll → collect** loop over repeated
invocations. First call launches a range-sharded parallel S3 listing
(boto3, 256 `StartAfter` ranges, ~64 threads — ~30 min at 300M keys)
detached in tmux window `plan`. Re-running plan-jobs polls it
(`keys_listed_so_far`). Once the listing lands, the same invocation
splits `listing.txt` into chunk manifests (`--chunk-objects`, default
250k — azcopy list-of-files degrades well above this) and writes the
queue: `L` jobs (azcopy `--list-of-files` server-side copy — same S2S
transport as R) plus `Q` jobs for keys whose names azcopy's list files
can't be trusted with (spaces/`+`/unicode; streamed by rclone
`--files-from`, expected ~empty on hex-hash buckets). The listing is
also the **cutoff manifest** verify runs against. `--rebuild` re-splits
from the existing listing; `--relist` discards the listing and starts
over; both refuse while the `plan` window is alive.

### 3. transfer `<slug>` — pilot first, then scale

```bash
python3 scripts/s3_transfer.py transfer <slug> --pilot
```

**On a new engagement always pilot first**: one worker runs exactly one
job, then exits — read `status` for that job's objects/s and multiply by
the planned `--windows` before quoting any timeline to the client. Then:

```bash
python3 scripts/s3_transfer.py transfer <slug>
```

K worker windows (default 8, `--concurrency` 200 per job) drain the
queue inside tmux session `transfer`. Hands back immediately — do NOT
block waiting. Interrupted, or some jobs failed? `transfer` again
resumes safely (`--overwrite=false` skips landed blobs; skip counts on
re-runs are normal, not errors); `--requeue-failed` moves failed jobs
back into the queue first. Refuses while workers are already running.

### 4. status `<slug>` — the most-used command

```bash
python3 scripts/s3_transfer.py status <slug>
```

One ssh round trip: jobs pending/inflight/done/failed, objects + bytes
landed, recent objects/s, ETA, per-worker heartbeats, recent failure
rows, and `workdir_free_gb` (plan-file disk — watch it; finished jobs
are reclaimed with `azcopy jobs rm` automatically). Report tight and
scannable. Queue drained + workers dead → suggest verify.

### 5. verify `<slug>`

```bash
python3 scripts/s3_transfer.py verify <slug>          # rollup (default)
python3 scripts/s3_transfer.py verify <slug> --deep   # + per-object size check
python3 scripts/s3_transfer.py verify <slug> --prefix <p>  # spot check now
```

Tier 1 is a per-job object-count + byte rollup, S3 vs Azure — listing-
only but hours at 300M objects, so it runs in a tmux window writing
`verify.tsv` incrementally; re-run `verify` to collect. Mismatches → rc
2 → re-run transfer (`--requeue-failed` for failed jobs), then re-verify.
Clean → suggest teardown (still gated). After a clean verify,
size-company picks the data up as the `s3-export` source — pin it in
`expected-data-sizes.json` with `"prefix": "s3-export"`.

**Flat engagements auto-switch** (the runner detects `L` jobs): instead
of per-job rollups, one streamed merge-join of the cutoff manifest
(`listing.txt`, name+size captured at plan time) against a complete
Azure dest-prefix listing — per-object name+size at two-listing cost,
so `--deep` is implied and ignored. Manifest-based verify is
**drift-immune**: objects the client (or anyone) writes to the bucket
after the plan listing are simply not in scope, and dest blobs absent
from the manifest surface as `EXTRA-DEST` rows — reported, never
touched. Align the cutoff semantics with the client up front: post-
cutoff bucket writes need a fresh engagement pass (`plan-jobs
--relist`), not a bigger verify. `MISSING-DEST` → re-run transfer;
`SIZE-DIFF` → investigate before re-copying (`--overwrite=false` will
NOT fix a bad existing blob — that's a deliberate, targeted
remediation conversation).

### 6. teardown `<slug>`

1. Refuses while the transfer tmux session is alive (`--force` only on
   an explicit user override).
2. Show the deletion plan: VM + NIC + OS disk (delete-option-tied) +
   public IP + NSG + VNET, in RG `<rg>` → **GATE: always confirm**, then
   re-run with `--confirmed`.
3. Relay the script's reminder checklist verbatim — including telling
   the client the read-only IAM key can be deleted now.

## Judgment notes

- **Why this path and not the alternatives** (the client conversation):
  a streaming copy (rclone, or the client's own tooling) moves every
  byte through one machine — months at 300M small objects. Azure Data
  Factory would be fast too, but it is the same fabric-side copy engine
  under a new service + billing model; azcopy S2S on our own VM gets the
  identical transport with the harness's scripted teardown and secrets
  story. Ballpark before the pilot: 8 workers ≈ 2–5k objects/s
  aggregate → 300M objects ≈ 17 h optimistic / ~28 h nominal / ~2.5
  days conservative. Quote the client a date only AFTER the pilot.
- **`SlowDown` (S3) or 503 (Azure) climbing in status = turn DOWN
  `--windows` or `--concurrency`, not a retry storm.** S3 sustains
  ~5,500 GET/s per prefix (jobs hit distinct prefixes, so this rarely
  binds); the SA's default limit is ~20k req/s (raisable via support
  ticket if the pilot proves it's the ceiling).
- **Glacier objects fail the copy, not the engagement.** Failed jobs
  whose errors say `InvalidObjectState` are the cold tail the probe
  sample missed — quantify from failed.txt, tell the client what needs
  restoring, and re-queue after the restore lands.
- Bucket names containing dots break virtual-host TLS
  (`<bucket.with.dots>.s3.amazonaws.com`) — surface it; the runner's URL
  style would need path-style addressing. A client conversation, not a
  silent hack.
- Re-planning jobs after progress exists orphans the ledger — that's why
  plan-jobs refuses without `--rebuild`. Resume wants `transfer`, not a
  new plan.
- The engagement has no expiry clock of its own (unlike takeout
  buckets), but the 21-day SAS and the client's key-deletion promise do —
  long stalls mean re-running `write-dest` and asking the client to keep
  the key alive.
- `--dry-run` on every subcommand prints the az/ssh commands instead of
  running them (secrets redacted) — use it to show the user exactly what
  will run.
- **Flat-mode pilot checklist** — before quoting any timeline on a flat
  engagement, `transfer --pilot` one L job and confirm ALL of: (1) the
  azcopy `--list-of-files` S2S copy actually moved objects (a silent
  `Transfers Completed: 0` is the blocker failure mode); (2) done.txt
  row parses (comp/fail/skip/bytes are numbers, not `?`); (3) re-running
  the same chunk yields `CompletedWithSkipped` with `Failed: 0`, cheap;
  (4) ~100 sampled dest blob names byte-match their source keys and
  Content-Type survived; (5) steady-state objects/s at the pilot's
  concurrency, with zero sustained 503/SlowDown in the logs; (6) the
  sharded listing rate looked sane in the plan-jobs polls. Expect
  3.5–8k obj/s aggregate at 8×200 nominal — 300M objects ≈ 1–3 days.
- **File the SA request-rate support ticket day one** on a monster flat
  engagement: the account default (~20k req/s) is the hard ceiling
  (~2 Azure ops per object ⇒ ~10k obj/s), tickets take days, and
  headroom also kills the 503 retry-amplification regime. Ramp
  discipline: start at 8 windows × 150–200, raise windows until 503s
  appear, back off 20%.
