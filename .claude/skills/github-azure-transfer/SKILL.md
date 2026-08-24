---
name: github-azure-transfer
description: Use when transferring a company's GitHub org or user account (repos, wikis, issues, PRs) into their Azure -raw container — "transfer <company>'s github", "pull their repos", "github to azure", "clone their org", or any probe, status check, verification, or teardown of an existing github transfer engagement.
---

# GitHub → Azure transfer

Pulls a GitHub org (or user account) into `<slug>-raw/github-export/` via
a temporary Azure VM (`xfer-gh-<slug>`) in the storage account's region —
the **fifth VM ingest** (after gcs/dropbox/gdrive/s3), sharing
transfer_engine.py's VM lifecycle but NOT its copy layer: rclone has no
GitHub backend, so `scripts/github_vm_pull.py` (pushed to the VM, run in
tmux) does the whole pull there — per repo a `git clone --mirror` (code +
full history), the wiki repo when one exists, `git lfs fetch --all` when
the repo declares LFS, and four paginated JSONL exports (issues, pulls,
issue_comments, review_comments) — then azcopies the staged tree to the
container. Nothing downloads to this machine. The company must already be
onboarded — `companies/<slug>/config.json` supplies the destination; the
user supplies the slug and the GitHub login.

All az/ssh/git/azcopy mechanics live in `scripts/github_transfer.py`
(engine lifecycle reused from `scripts/transfer_engine.py`, VM-side
puller `scripts/github_vm_pull.py`) — never hand-roll them. Your job:
orchestration, judgment, the pause point, the probe gate, and the
confirmation gates. Full command templates + troubleshooting:
[references/commands.md](references/commands.md).

**Out of scope (say so up front):** GitHub Actions logs/artifacts,
Packages, Projects, Discussions, and releases' binary assets are not
pulled. **Wikis ARE in scope** (separate `.wiki.git` repos, cloned when
enabled). LFS objects are fetched automatically when a repo's
`.gitattributes` declares `filter=lfs`. When the company is already
onboarded to the data.micro1.ai portal's GitHub App path, that pull gives
strictly better credential custody (short-lived installation tokens) —
this skill is for companies NOT on that path, or when an operator needs
direct control (docs/github-transfer-handoff.md §0).

## HARD CONSTRAINTS — never violate

1. **Network rules go through `allow-network` only** for the VM
   (service endpoint + vnet-rule; IP rules never match same-region VM
   traffic), and teardown removes exactly the rule it added. The ONE
   sanctioned exception in this skill: `verify` runs on the LAPTOP (the
   VM is normally gone by then) and uses `phases.ip_rule_ensure` — the
   sizing-family laptop path — inside the script. Never hand-roll either
   mechanism, and NEVER remove a rule the harness didn't add.
2. **PAT hygiene.** The fine-grained PAT arrives from the client via a
   secure channel and goes to the script as **1 stdin line** — never
   argv, tags, logs, or laptop files (probe holds it in process memory
   only). On the VM it lives in `~/.config/xfer/github.env` (600), and
   git authenticates through a 0700 GIT_ASKPASS helper — the token is
   never in a clone URL or on a git command line. It dies with the VM;
   the client revokes it after verification (tell them when).
3. **Static public IP, never deallocate** before final teardown.
4. **The write invariant, stated honestly.** The SAS is racwl — **no
   delete permission, server-enforced**. No-overwrite comes from
   `--overwrite=false` in the puller's azcopy call — **client-side**,
   not the API-enforced `If-None-Match: *` of the qwilr/vimeo/zoom
   pulls. The honest sentence is "the SAS cannot delete, and the pinned
   copy command never overwrites."
5. **Confirmation gates.** Show the plan AND a clean probe result and
   get explicit user confirmation before (a) VM creation and (b)
   teardown. Teardown is ALWAYS manual-confirm (the script refuses
   without `--confirmed`).
6. **This is the sanctioned WRITE path** into `<slug>-raw` (racwl SAS,
   21-day default) — additive only, under the dest prefix; it never
   modifies or deletes existing data.

## No state file

Azure is the source of truth: VM name `xfer-gh-<slug>` + tags
(`purpose=github-transfer`, `engagement`, `gh_login`, `gh_owner_type`,
`dest_prefix`). Staging (`~/xfer-gh/dest/` on the VM) is working state
that dies with the VM — but only after its manifest.json landed in the
container, which is what verify reads. Sessions span days — at the START
of any invocation, always run discovery first and report where things
stand:

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/github_transfer.py discover <slug>
```

Phases: `pre-setup` (no VM) → `mid-setup` (creds/dest incomplete) →
`setup-complete` → `transfer-running` → `transfer-stopped` (verify or
re-run). Transfer state never touches `companies/<slug>/status.json`.

## The six operations

### 1. probe `<slug>` — the day-one gate, BEFORE any billable resource

Probe is laptop-side GitHub API JSON only — no VM, no Azure access, no
corpus data — so it runs the moment the client sends the PAT, before
anything bills. First, **PAUSE — the fine-grained PAT.** Give the user
this snippet for the client (secure channel):

```
1. GitHub → Settings → Developer settings → Personal access tokens
   → Fine-grained tokens → Generate new token.
2. Resource owner: select the ORGANIZATION (not your personal
   account).
3. Repository access: All repositories (or the specific set in
   scope).
4. Repository permissions: Contents = Read-only, Issues = Read-only,
   Pull requests = Read-only. (Metadata is included automatically.)
5. Expiration: set it past our agreed engagement window.
6. If your org requires approval for fine-grained tokens, an org
   owner must approve it (org Settings → Third-party Access →
   pending requests) — until then it won't work at all.
7. Send the token via a secure channel. You can revoke it the moment
   we confirm verification — we'll tell you when.
8. Also: does the org use Git LFS? (Org → Billing → Git LFS shows
   usage.)
```

Wait for the paste. Then pipe it via stdin — never argv:

```bash
python3 scripts/github_transfer.py probe <slug> --login <org> <<'EOF'
<the fine-grained PAT>
EOF
```

Probe validates the token (`/rate_limit`), checks org visibility (a
403/404 here = wrong login/owner-type OR **the org hasn't approved the
PAT yet** — the known day-one stall; a client conversation, not a
retry), enumerates repos (count, private/archived/forks, summed API
`size` → `estimated_clone_bytes`), and samples root `.gitattributes`
files for `filter=lfs`. Report the numbers and every `notes` caveat —
the estimate is a **floor** (excludes LFS objects and the JSON exports).
`--owner-type user` for personal accounts.

### 2. setup `<slug>`

```bash
python3 scripts/github_transfer.py plan <slug> --login <org>
```

1. Show the plan (VM name/size/region/RG, dest, SAS expiry) **plus the
   clean probe result** → **GATE: confirm before creating billable
   resources.** Optional flags: `--vm-size` (default Standard_D8s_v7),
   `--os-disk-gb` (default 512 — staging holds the WHOLE corpus, clones
   + LFS + JSONL, before azcopy runs), `--rg`, `--dest-prefix`,
   `--sas-days`, `--owner-type`.
2. `create-vm <slug> --login <org>` — VM + bootstrap (git, git-lfs,
   azcopy, tmux). Takes a few minutes.
3. `allow-network <slug>` — service endpoint + vnet-rule (engine-run).
4. `write-dest <slug>` — mints the container SAS locally (racwl, 21
   days) and installs it twice on the VM: the rclone `[azure]` remote
   (check-azure) and `dest.env` (the puller's azcopy dest).
5. `check-azure <slug>`. On a 403 it reads the ruleset (read-only):
   vnet-rule present = propagation — wait ~10s and retry; missing =
   re-run allow-network. Never a SAS problem — do not re-mint.
6. `write-token <slug>` — the PAT again on stdin; lands in the 600
   `github.env` and is smoke-tested from the VM (`/rate_limit`).

### 3. transfer `<slug>` — pilot first, then scale

```bash
python3 scripts/github_transfer.py transfer <slug> --limit 2
```

**On a new engagement always pilot first**: two repos end-to-end (clone
+ wiki + JSONL + azcopy), then check `status` and spot-check the blobs
landed under the prefix before the full run:

```bash
python3 scripts/github_transfer.py transfer <slug>
```

The puller runs in tmux session `transfer`, logging to
`~/xfer-gh/pull.log`. Hands back immediately — do NOT block waiting.
Interrupted, or some repos failed? `transfer` again resumes safely:
per-repo `.cdp-complete` markers skip finished repos, and azcopy
`--overwrite=false` skips landed blobs. **Upload-what-succeeded**: a
pass with failed repos still uploads everything that completed; the
failed set rides manifest.json until a re-run clears it. `--only <repo>`
targets one repo; `--refresh` re-fetches despite markers.

### 4. status `<slug>`

```bash
python3 scripts/github_transfer.py status <slug>
```

One ssh round trip: tmux alive, progress.json (phase + repo i/N +
current repo), manifest summary when a pass finished (repo count, staged
bytes, failed repos), pull.log tail, disk free. A "stuck" pull whose log
tail shows a rate-limit sleep is NORMAL — the API budget is 5k req/h
and comment-heavy repos sleep through resets (see judgment notes).
Pull finished (tmux dead + manifest present) → suggest verify.

### 5. verify `<slug>` — laptop-side

```bash
python3 scripts/github_transfer.py verify <slug>
```

Runs on the laptop — the VM can already be gone. Lists the dest prefix
(rl SAS, `ip_rule_ensure` firewall) and compares it against the uploaded
manifest.json: every successful leg's `.cdp-complete` marker blob must
exist, and each repo's landed byte sum must cover its staged bytes.
**What it honestly asserts:** staged→container completeness. GitHub→
staged completeness is the puller's own exit code + `failed_repos`
(surfaced verbatim, rc 2 → re-run transfer, then re-verify). No
source-size claim — git packing differs from the API's `diskUsage`.
`stale_extra` alone is informational (a re-clone made different
packfiles; create-only kept the old ones). Clean → suggest teardown
(still gated), and pin the service in `expected-data-sizes.json` so
size-company picks it up:

```json
"github": {"bytes": <declared>, "prefix": "github-export"}
```

(the manifest will say "github", which won't name-match the
`github-export` prefix without the pin).

### 6. teardown `<slug>`

1. Refuses while the transfer tmux session is alive (`--force` only on
   an explicit user override).
2. Show the deletion plan: VM + NIC + OS disk (delete-option-tied) +
   public IP + NSG + VNET, in RG `<rg>` → **GATE: always confirm**, then
   re-run with `--confirmed`.
3. Relay the script's reminder checklist verbatim — including telling
   the client the fine-grained PAT can be revoked now.

## Judgment notes

- **The failure taxonomy** (baked into the puller — recognize it in
  pull.log): 401 = token invalid/expired, fatal. **403 with the rate
  limit intact = missing scope or unapproved PAT, fatal — never a
  retry.** 403/429 with `Retry-After` or an exhausted
  `X-RateLimit-Remaining` = throttling — the puller sleeps to the reset
  (capped 65 min) and continues. 404 on a JSON endpoint = the feature is
  disabled on that repo — a normal skip, not a failure. A git clone
  whose stderr says 403/Authentication failed = the PAT lacks
  Contents: read, fatal.
- **Rate-limit budget**: 5k requests/hour per token. Clones don't count
  against it; the JSONL exports do (1 request per 100 items) — an org
  with hundreds of thousands of comments spends hours sleeping through
  resets. That's a calm log line, not a hang.
- **Org approval is the known day-one stall**: a fine-grained PAT with
  an org resource owner does nothing until an org owner approves it —
  every call 403s. Probe diagnoses it; the fix is on the client's side.
- **Probe's size estimate is a floor**: the API's per-repo `size`
  excludes LFS objects and our JSONL exports. If probe flags LFS repos,
  ask the client about LFS usage (Org → Billing → Git LFS) before
  quoting disk or timeline.
- **Multi-org companies**: one full cycle per org — same VM, but pass
  `--login <org2> --dest-prefix github-export/<org2-login>` explicitly
  to transfer (flags override the VM tags), and verify each prefix
  separately (the zoom multi-account convention). The top-level
  `github-export` still satisfies one `"prefix": "github-export"` pin.
- **A repo created after the pull started** is simply not in that pass's
  enumeration — cutoff semantics. A follow-up `transfer` picks it up
  (markers skip everything already done).
- `--dry-run` on every subcommand prints the az/ssh commands instead of
  running them (secrets redacted) — use it to show the user exactly what
  will run.
