---
name: figma-azure-transfer
description: Use when transferring a company's Figma workspace — design files, FigJam boards, comments, version history, image fills, page renders, published libraries — into their Azure -raw container — "transfer <company>'s figma", "pull their figma", "figma to azure", "figma export", "export their design files", or any probe, status check, verification, or teardown of an existing figma transfer engagement.
---

# Figma → Azure transfer

Pulls a company's Figma workspace into `<slug>-raw/figma-export/` via a
temporary Azure VM (`xfer-figma-<slug>`) in the storage account's region —
the **seventh VM ingest**. Per file it stages the node-tree JSON (the
design itself), comments, the version-history list, the embedded bitmap
image fills, one PNG render per page, and per team the published
component/style library metadata.

It has to use a VM, and the reason is worth stating precisely because it
justifies every other rule below: Figma has **no bulk export** — the REST
API serves everything one file at a time behind a metered rate limit whose
**Tier-1 bucket (file JSON, node JSON and image renders SHARE it) caps at
20 requests/min even on Enterprise**. A real workspace is therefore a
multi-hour-to-multi-day walk that wants tmux detachment and per-unit
durability, and the corpus majority (document JSON) must be token-fetched
and staged regardless. `scripts/figma_vm_pull.py` (pushed to the VM, run
in tmux) does the whole pull there and azcopies each unit up as it
completes. Nothing downloads to this machine. Honesty note: the presigned
CDN URLs for fills/renders WOULD work with the vimeo/zoom server-side-copy
transport — it is deliberately not used, so verify has one story. The
company must already be onboarded — `companies/<slug>/config.json`
supplies the destination.

All az/ssh/REST/azcopy mechanics live in `scripts/figma_transfer.py`
(engine lifecycle reused from `scripts/transfer_engine.py`, VM-side puller
`scripts/figma_vm_pull.py`) — never hand-roll them. Your job:
orchestration, judgment, the pause point, the probe gate, and the
confirmation gates. Full command templates + troubleshooting:
[references/commands.md](references/commands.md).

**What the API cannot give (say so up front):** there is **no `.fig`
source-file export endpoint** — none — so this corpus (API JSON + rendered
and downloaded assets) is a **derivative, not a restorable backup**; it
cannot be re-imported into Figma as editable files. Say that before the
client assumes otherwise (the only `.fig` route is a manual "Save local
copy" per file, which is a separate client conversation). Figma also
publishes **no byte sizes for anything** — no file size, no asset size —
so `probe` reports counts and a wall-clock estimate, never bytes; the
first honest byte number in the engagement is the transfer manifest's
`total_staged_bytes`. There is **no API that lists an org's teams**: the
client must hand over every team id, and a team they forget is invisible,
silently. Unpublished library components exist only inside each file's own
JSON (captured); variables need an Enterprise full seat and are not
pulled. Out of scope entirely: comment reactions (one Tier-2 call per
comment), per-version file JSON (would multiply the Tier-1 walk; the
version LIST is captured), Dev Mode dev resources, webhooks, analytics,
and drafts in personal spaces the token owner cannot see.

## HARD CONSTRAINTS — never violate

1. **Network rules go through `allow-network` only** for the VM (service
   endpoint + vnet-rule; IP rules never match same-region VM traffic), and
   teardown removes exactly the rule it added. The ONE sanctioned
   exception in this skill: `verify` runs on the LAPTOP (the VM is
   normally gone by then) and uses `phases.ip_rule_ensure` — the
   sizing-family laptop path — inside the script. Never hand-roll either
   mechanism, and NEVER remove a rule the harness didn't add.
2. **Credential hygiene.** ONE secret arrives from the client via a secure
   channel: the personal access token, passed to the script as **1 stdin
   line** — never argv, tags, logs, or laptop files (probe holds it in
   process memory only). On the VM it lives in `~/.config/xfer/figma.env`
   (600) and reaches the puller only through its process environment; it
   goes out on exactly one header to `api.figma.com` and **never to the
   presigned CDN URLs** (sending a credential to a third-party host would
   leak it). Team ids are NOT secret (they are in every URL) and ride the
   normal `--team-ids` → VM-tag path. The token dies with the VM; the
   client revokes it after verification.
3. **Static public IP, never deallocate** before final teardown.
4. **The write invariant, stated honestly.** The SAS is racwl — **no
   delete permission, server-enforced**. No-overwrite comes from
   `--overwrite=false` in the puller's azcopy call — **client-side**, not
   the API-enforced `If-None-Match: *` of the qwilr/vimeo/zoom pulls. The
   honest sentence is "the SAS cannot delete, and the pinned copy command
   never overwrites."
5. **Confirmation gates.** Show the plan AND a clean probe result and get
   explicit user confirmation before (a) VM creation and (b) teardown.
   Teardown is ALWAYS manual-confirm (the script refuses without
   `--confirmed`).
6. **This is the sanctioned WRITE path** into `<slug>-raw` (racwl SAS,
   21-day default) — additive only, under the dest prefix; it never
   modifies or deletes existing data.

## No state file

Azure is the source of truth. The VM name (`xfer-figma-<slug>`) and its
tags carry the engagement: `purpose=figma-transfer`, `engagement=<slug>`,
`figma_team_ids` (the comma-separated list — one tag value),
`figma_plan`, `dest_container`, `dest_prefix`. Transfer state never
touches `status.json`.

`discover` reconstructs the phase (`pre-setup` → `mid-setup` →
`setup-complete` → `transfer-running` → `transfer-stopped`). **Always run
it first** on an engagement you did not just create.

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/figma_transfer.py discover <slug>
```

## The six operations

### 1. PAUSE — credentials from the client

This comes first, before any billable resource. Two things are needed: a
**personal access token** from a **Full or Dev seat** (this matters — a
View/Collab seat's token gets **6 file-read calls per MONTH** and is
unusable), and **every in-scope team id** (no API lists teams; what they
don't name, we cannot see).

Snippet for the client (secure channel):

```
1. Sign in to Figma as a user who holds a FULL or DEV seat on the plan
   that owns the teams. (A View or Collab seat's token is rate-limited
   to 6 file reads per MONTH and cannot run an export.)
2. Click your account menu (top-left avatar) -> Settings -> the Security
   tab -> Personal access tokens -> Generate new token.
3. Name it (e.g. "micro1 corpus export") and set the expiration to the
   longest option, up to Figma's 90-day maximum. If the engagement
   outlives it we will ask for a fresh token.
4. In the scopes list, enable EXACTLY these, all read-only:
     - File content            (file_content:read)
     - File metadata           (file_metadata:read)
     - File versions           (file_versions:read)
     - Comments                (file_comments:read)
     - Folders                 (folders:read AND folder_metadata:read)
     - Library content         (library_content:read)
     - Team library content    (team_library_content:read)
     - Library assets          (library_assets:read)
     - Current user            (current_user:read)
   Do not look for "files:read" or "projects:read" — those are the old
   deprecated scopes and are no longer offered. "Variables"
   (file_variables:read) needs an Enterprise full seat; include it only
   if you are on Enterprise and want variables exported.
5. Click Generate and COPY THE TOKEN NOW — Figma shows it exactly once.
6. Collect your team id(s): open the Figma file browser, click each team
   in the left sidebar, and copy the number from the URL:
     figma.com/files/team/<TEAM_ID>/...
   Send EVERY team that is in scope — there is no API that lists teams,
   so we can only export the ones you name.
7. Send us three things over this channel: the token, the team id list,
   and your Figma plan name (Starter / Professional / Organization /
   Enterprise — it sets our API speed limit and therefore the timeline).
8. Once we confirm the export is verified, revoke the token
   (Settings -> Security -> Personal access tokens -> Revoke).
```

**Wait for the paste.** If the client cannot see the named scopes when
generating, the token UI predates granular scoping or they lack the seat —
a conversation, not a retry. On Organization/Enterprise plans an org admin
can alternatively mint a **plan access token** (figma.com/developers/tokens,
365-day expiry, org-wide resource allowlist) — accepted by the same
`X-Figma-Token` header, but it cannot call the variables endpoints; the
PAT flow above is the default.

### 2. probe `<slug>` — the day-one gate

```bash
python3 scripts/figma_transfer.py probe <slug> --team-ids 123,456 --plan org <<'EOF'
<personal access token>
EOF
```

Touches NO Azure, needs no VM. Answers, before anything is promised: does
the token work (`/v1/me`), can it **see every named team**, which endpoint
families answer (Figma documents no response code for a missing scope, so
probe fail-fasts each family once rather than discovering a gap 8 hours
into the walk), a full walk census (teams / folders / files / branches), an
editorType sample (FigJam/Slides presence is DISCOVERED — listings don't
carry it), and the **Tier-1 call estimate with wall-clock per plan** — the
only honest input to a timeline.

**The two day-one stalls live here.** (a) *Wrong seat*: a View/Collab
token authenticates fine on `/v1/me` and then rate-walls — the tell is a
429 with `X-Figma-Rate-Limit-Type: low`, and probe aborts with the seat
conversation. (b) *Token owner not in a team*: the team walk 403/404s —
and Figma does not distinguish "no access" from "no such team", so check
the id against the client's URL before re-requesting a token.

**Read the census back to the client before quoting scope** — a team they
forgot to name is invisible, silently. Probe gives **no byte figure, on
purpose** (Figma publishes no sizes); quote the timeline off
`estimate.hours_by_plan` at their actual plan, and still pilot first.

Then **GATE: show plan + probe, confirm before creating the VM.**

### 3. setup

`plan` (read-only, offline — shows the VM, region, dest and SAS approach)
is the confirmation gate; then, in order:

```bash
python3 scripts/figma_transfer.py plan        <slug> --team-ids 123,456 --plan org
python3 scripts/figma_transfer.py create-vm   <slug> --team-ids 123,456 --plan org
python3 scripts/figma_transfer.py allow-network <slug>
python3 scripts/figma_transfer.py write-dest  <slug>
python3 scripts/figma_transfer.py check-azure <slug>
python3 scripts/figma_transfer.py write-creds <slug> <<'EOF'
<personal access token>
EOF
```

`write-creds` smoke-tests the sourced token from the VM: `/v1/me` (does
it work) and the first tagged team's folder listing (can it see the
engagement — the second day-one stall, caught again here). VM billing
starts at `create-vm`.

### 4. transfer `<slug>`

Run via Bash `run_in_background` and watch the heartbeat; never block on it.

```bash
python3 scripts/figma_transfer.py transfer <slug> --limit 2
python3 scripts/figma_transfer.py transfer <slug>
```

**Pilot first.** `--limit 2` is an end-to-end smoke (token + firewall +
SAS + azcopy + CDN downloads) that also measures real seconds-per-file —
the only honest calibration of probe's estimate. Then run the full pull.

The puller walks units in order: `meta` (the discovery ledger — the one
REQUIRED unit), `library/<team>` per team, then one unit per file
(document JSON, comments, versions list, fills, renders, branches — the
file key, not the mutable name, keys the unit). Each unit azcopies **as
soon as it is whole**, so a mid-run VM loss costs one file, not the pass.
Renders are ON by default; `--no-render-pages` roughly halves the wall
clock if the client only needs the data. An oversized file that the API
refuses whole is **decomposed automatically** (depth-1 + per-node JSON) —
complete, just shaped differently, and recorded.

Exit 0 even with per-unit skips; exit 2 when a unit failed (verify is the
completeness authority). Re-running is always safe: `.cdp-complete`
markers skip finished files, the library cursor resumes mid-walk, fill
downloads resume by file existence, and `--overwrite=false` skips landed
blobs.

### 5. status `<slug>`

```bash
python3 scripts/figma_transfer.py status <slug>
```

Progress heartbeat, manifest summary, log tail, disk free. **A
`rate-limited; sleeping Ns` or pacing-sleep line is normal metering
against Figma's per-minute tiers, not a hang** — say so rather than
intervening. The Tier-1 clock means a big workspace legitimately runs for
days.

### 6. verify `<slug>` — then teardown

```bash
python3 scripts/figma_transfer.py verify <slug>
```

Laptop-side, needs **no Figma credentials** — it lists the dest prefix
(rl SAS via the laptop IP-rule path) and compares it against the uploaded
`manifest.json`. Per unit: the `.cdp-complete` marker must be in the
container and the byte rollup must be ≥ what was staged.

- `failed_units` / `missing_markers` / `short_uploads` → rc 2 → re-run
  transfer, then verify again.
- `stale_extra` alone is informational: no-overwrite kept an earlier
  pass's longer file.
- `skipped_units` are **deliberate and never a failure** — read their
  reasons out loud (`no-access-or-missing` = Figma does not say whether
  the file was invisible to the token or deleted).
- `decomposed_files`, `fill_errors` and `render_nulls` are informational
  quality signals — report them, don't fail on them.

It certifies **staged → container** only, and makes **no source-size
claim** — here that is total, not just forced: Figma publishes no byte
size for anything. When reporting, repeat the derivative caveat: no
`.fig` export exists, so this corpus is not restorable into Figma.

Then wrap up: remind the user to have the client **revoke the personal
access token**; the SAS lapses on its own within 21 days; the laptop IP
rule is already gone. Teardown last:

```bash
python3 scripts/figma_transfer.py teardown <slug> --confirmed
```

## Judgment notes

- **The rate limit IS the clock.** Tier 1 (document JSON, node JSON, page
  renders — one shared bucket) paces the whole job: 10,000 files ≈ 8.5 h
  at 20/min (Org/Enterprise) *before* renders, roughly double with them,
  and double again on Starter. Quote timelines off probe's estimate at
  the detected plan, calibrated by a pilot. All scripts sharing the PAT
  share ONE budget — never run probe while a transfer is running.
- **Two day-one stalls, both caught before billable resources**: the
  wrong-seat token (429 + `X-Figma-Rate-Limit-Type: low` = View/Collab =
  6 Tier-1 calls/month) and the not-a-member team (403/404 on the walk).
  Probe catches both; `write-creds` re-checks them from the VM.
- **403 vs 404 is not disambiguated** — a skipped file means "no access
  OR deleted", recorded as `no-access-or-missing`, never fatal. Report
  skip counts; don't over-diagnose.
- **A dead token is 403, not 401** — and Figma does not say whether it
  was never valid or has expired. PATs expire within ~90 days, so a run
  that was succeeding and then goes all-403 mid-pass is **token expiry**,
  not scopes; the systemic breaker names it. Fresh token → `write-creds`
  → re-run (markers make it a resumption).
- **The `.fig` caveat, out loud**: no export endpoint for source files
  exists anywhere in the API. This corpus is a derivative. Say it before
  the client assumes a backup.
- **Renders are on by default** (the engagement decision); each is a
  Tier-1 call competing with document pulls, a 200 can carry null
  per-node results (retried once, then recorded as `render_nulls`), and
  render URLs expire in 30 days — the puller downloads immediately.
  Image-fill URLs expire in **≤14 days**, which is why the fill URL map
  is always re-fetched fresh and never cursored.
- **FigJam/Slides coverage is recorded, never promised** — folder
  listings carry no editor type, so what the workspace contains is only
  knowable per file; `meta/files.jsonl` plus the manifest's
  `editor_types` census are the truth.
- **Decomposed files are complete, not broken.** A monster file answers
  400 "try a smaller request" / 5xx; the puller re-pulls it as depth-1 +
  per-node subtrees. `decomposed_files` in verify is informational.
- **expected-data-sizes pin**: a plain
  `"figma": {"bytes": <declared>, "prefix": "figma-export"}` — no
  `source_split` (single prefix, unlike zoho). The declared number is the
  client's; nothing Figma-side can confirm it pre-run, and the manifest's
  `total_staged_bytes` is the first measured value — expect them to
  differ and say why (JSON + renders is a different unit than whatever
  the client eyeballed).
- **Multi-batch engagements**: a team list too long for one VM tag (the
  script refuses past ~240 chars) runs as one cycle per batch with the
  same dest prefix — units are keyed per team, so batches never collide.
- `--dry-run` works on every subcommand and prints the exact az/ssh/REST
  calls without making them.
