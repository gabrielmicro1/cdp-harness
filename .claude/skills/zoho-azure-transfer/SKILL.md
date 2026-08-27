---
name: zoho-azure-transfer
description: Use when transferring a company's Zoho data — CRM records and attachments, Zoho Learn courses, or WorkDrive files — into their Azure -raw container — "transfer <company>'s zoho", "pull their zoho crm", "zoho to azure", "zoho learn export", "zoho crm attachments", or any probe, status check, verification, or teardown of an existing zoho transfer engagement.
---

# Zoho → Azure transfer

Pulls a company's Zoho tenant into `<slug>-raw/zoho-export/<product>/` via
a temporary Azure VM (`xfer-zoho-<slug>`) in the storage account's region
— the **sixth VM ingest**, and the first **multi-product** one: one skill
and one script cover Zoho **CRM**, **Zoho Learn** and **WorkDrive** behind
a `--product crm|learn|workdrive` dimension, one tmux window each.

It has to use a VM, and the reason is worth stating precisely because it
justifies every other rule below: Zoho's download endpoints authenticate
with `Authorization: Zoho-oauthtoken <tok>`, and Azure's
`x-ms-copy-source-authorization` header only speaks `Bearer`. The
vimeo/zoom **server-side-copy transport is structurally unavailable**, so
attachment bytes must be staged somewhere — and 133 GB through a laptop is
the wrong trade. `scripts/zoho_vm_pull.py` (pushed to the VM, run in tmux)
therefore does the whole pull there and azcopies each unit up as it
completes. Nothing downloads to this machine. Cousin of the gcs/dropbox/
gdrive skills but NOT the same copy layer (`transfer_engine.py` supplies
only the VM lifecycle; rclone has no Zoho backend). The company must
already be onboarded — `companies/<slug>/config.json` supplies the
destination.

All az/ssh/REST/azcopy mechanics live in `scripts/zoho_transfer.py`
(engine lifecycle reused from `scripts/transfer_engine.py`, VM-side puller
`scripts/zoho_vm_pull.py`) — never hand-roll them. Your job: orchestration,
judgment, the pause point, the probe gate, and the confirmation gates. Full
command templates + troubleshooting:
[references/commands.md](references/commands.md).

**What the API cannot give (say so up front):** Zoho exposes **no byte
size for a CRM record**, so `probe` reports record COUNTS (exact, from
`/crm/v8/<Module>/actions/count`) and never invents record bytes.
Attachments are the opposite: `/crm/v8/Attachments` is directly listable
and its `Size` is an exact byte integer, so the attachment census IS real —
and since attachments are almost always where the declared volume actually
lives, that census is usually the whole size answer. The
**knowledge-base half of Zoho Learn** (manuals, spaces, articles) has **no
documented API** and, on a real tenant, no reachable one either: `/manual`
and `/space` exist as routes but reject a bare GET, the documented manuals
listing hangs off a **custom portal** (`/customportal/<id>/manual`) which
returns Access Denied unless one is configured, and `/search` is
`INTERNAL_IP_ACCESS_ONLY`. What does answer is `/course`, `/quiz` and
`/tag`. Probe ATTEMPTS all of it and records what answered, so Learn scope
is discovered, never promised — read `_meta/discovery.json` before quoting
it. Bulk Read
excludes Notes, Attachments, Emails and related lists, which is why those
are pulled separately by their own units. Out of scope entirely: Zoho
Books, Desk, People, Mail, Projects, Campaigns and Analytics (separate
products, separate APIs), and within CRM: sandboxes, the recycle bin, the
audit log, and territory/portal configuration.

## HARD CONSTRAINTS — never violate

1. **Network rules go through `allow-network` only** for the VM (service
   endpoint + vnet-rule; IP rules never match same-region VM traffic), and
   teardown removes exactly the rule it added. The ONE sanctioned
   exception in this skill: `verify` runs on the LAPTOP (the VM is
   normally gone by then) and uses `phases.ip_rule_ensure` — the
   sizing-family laptop path — inside the script. Never hand-roll either
   mechanism, and NEVER remove a rule the harness didn't add.
2. **Credential hygiene.** FOUR values arrive from the client via a secure
   channel and go to the script as **4 stdin lines, in this order — data
   center, Client ID, Client Secret, Refresh Token** — never argv, tags,
   logs, or laptop files (probe holds them in process memory only). On the
   VM they live in `~/.config/xfer/zoho.env` (600) and reach the puller
   only through its process environment; the minted access token is never
   logged. They die with the VM; the client revokes the refresh token and
   **deletes the Self Client** after verification (tell them when — the
   scopes are account-wide, so this is not optional).
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

Azure is the source of truth. The VM name (`xfer-zoho-<slug>`) and its tags
carry the engagement: `purpose=zoho-transfer`, `engagement=<slug>`,
`zoho_dc`, `zoho_product`, `zoho_portal`, `dest_container`, `dest_prefix`.
Note the `dest_prefix` tag holds the **base** (`zoho-export`) with no
product suffix — one VM serves all three products, and each gets its own
window, its own staging dir and its own manifest. Transfer state never
touches `status.json`.

`discover` reconstructs the phase (`pre-setup` → `mid-setup` →
`setup-complete` → `transfer-running` → `transfer-stopped`). **Always run
it first** on an engagement you did not just create.

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/zoho_transfer.py discover <slug>
```

## The six operations

### 1. PAUSE — credentials from the client

This comes first, before any billable resource. The client's **Zoho admin**
does the whole Self Client dance themselves and hands us the **refresh
token**, not a grant token: Zoho grant tokens expire in **3–10 minutes**, so
any flow where we do the exchange is a race that stalls a call.

Snippet for the client's Zoho admin (secure channel):

```
1. Go to api-console.zoho.com and click ADD CLIENT → Self Client
   (not "Server-based" — Self Client is the one with no redirect URL).
2. Open the Self Client, go to the Generate Code tab, and paste this
   scope list in one line (validated against the console 2026-08-25 —
   every name here is accepted):
     ZohoCRM.modules.ALL,ZohoCRM.settings.READ,ZohoCRM.users.READ,
     ZohoCRM.org.READ,ZohoCRM.bulk.READ,ZohoCRM.coql.READ,
     ZohoLearn.course.READ,ZohoLearn.lesson.READ,ZohoLearn.manual.READ,
     ZohoLearn.space.READ,ZohoLearn.article.READ,
     ZohoLearn.articleimage.READ,ZohoLearn.attachment.READ,
     ZohoLearn.comment.READ,ZohoLearn.commentlike.READ,
     ZohoLearn.quiz.READ,ZohoLearn.questionbank.READ,ZohoLearn.tag.READ,
     ZohoLearn.template.READ,ZohoLearn.customportal.READ,
     ZohoLearn.hubMember.READ,ZohoLearn.network.READ,
     ZohoLearn.profile.READ,ZohoLearn.activity.READ,
     ZohoLearn.favorite.READ,ZohoLearn.notification.READ
   Set a description and a 10-minute duration, then Create.
   NOTE: ZohoLearn.lesson.READ is REQUIRED for lesson/video content and is
   MISSING from Zoho's own documented scope list. ZohoLearn.member.READ and
   ZohoLearn.lessondiscussion.READ ARE in that list but are REJECTED.
3. Copy the generated code. IT EXPIRES IN 10 MINUTES — do step 4 now.
4. Exchange it for a refresh token (terminal, one command):
     curl -X POST "https://accounts.zoho.<DC>/oauth/v2/token" \
       -d grant_type=authorization_code \
       -d client_id=<CLIENT_ID> \
       -d client_secret=<CLIENT_SECRET> \
       -d code=<THE_CODE_FROM_STEP_3>
   Replace <DC> with your Zoho domain suffix (see step 5).
5. Send us FOUR things:
     - your data center: the suffix of the URL you see when signed in to
       Zoho (crm.zoho.com -> com, crm.zoho.eu -> eu, .in, .com.au,
       .jp, .ca, .sa)
     - Client ID
     - Client Secret
     - the refresh_token value from step 4's response
     - the `scope` field from step 4's response, verbatim. The console
       silently DROPS invalid scopes, so this is the only way to know
       what the token can actually read.
6. Once we confirm the export is verified, revoke the refresh token and
   DELETE the Self Client from the same console. These scopes are
   account-wide, so this step matters.
```

**Wait for the paste.** If step 4 returns `"error":"invalid_code"`, the
code expired — regenerate at step 2, it is not a scope problem. If the
client cannot see **Self Client** as an option, they are not the API
console owner; that is a Zoho admin permission conversation, not a retry.

### 2. probe `<slug>` — the data-center gate

```bash
python3 scripts/zoho_transfer.py probe <slug> --dc <dc> --product crm <<'EOF'
<data center>
<client id>
<client secret>
<refresh token>
EOF
```

Touches NO Azure, needs no VM. Answers, before anything is promised:
do the credentials mint a token, is the **data center right**, which CRM
modules are `api_supported` and not deleted, does a sample page return, and
(best-effort) a COQL `count(id)` census per module.

**The data center is the day-one stall here** — the zoho analogue of
github's unapproved PAT, because a wrong `.com`/`.eu` looks exactly like a
generic auth failure. It is caught three independent ways: the mint error
body, an `api_domain` cross-check against the declared DC (Zoho itself
tells us which DC the token belongs to), and a stdin-vs-VM-tag guard at
`write-creds`. If the error names both causes, believe it and check the
URL the client sees when signed in — do not re-request the token first.

For Learn, run a second probe with `--product learn --portal <networkurl>`
(the portal segment of their Learn URL, e.g.
`learn.zoho.com/portal/zylker-network` → `zylker-network`). Read
`kb_reachable` and `kb_note`: if nothing answered, **this pull covers
courses only** and that is a client conversation about a manual export, not
a retry.

**Read the census, then still pilot.** Probe gives exact record counts and
an exact attachment byte total — quote size off those. It gives no
records-per-second, so pilot a single module (step 4) before quoting a
*timeline*.

Then **GATE: show plan + probe, confirm before creating the VM.**

### 3. setup

`plan` (read-only, offline — shows the VM, region, dest and SAS approach)
is the confirmation gate; then, in order:

```bash
python3 scripts/zoho_transfer.py plan        <slug> --dc <dc> --product crm
python3 scripts/zoho_transfer.py create-vm   <slug> --dc <dc> --product crm
python3 scripts/zoho_transfer.py allow-network <slug>
python3 scripts/zoho_transfer.py write-dest  <slug> --product crm
python3 scripts/zoho_transfer.py check-azure <slug>
python3 scripts/zoho_transfer.py write-creds <slug> <<'EOF'
<data center>
<client id>
<client secret>
<refresh token>
EOF
```

`write-dest` is **per product** — it points `dest.env` at
`zoho-export/<product>`, so re-run it when you switch products. VM billing
starts at `create-vm`.

### 4. transfer `<slug> --product <p>`

Run via Bash `run_in_background` and watch the heartbeat; never block on it.

```bash
python3 scripts/zoho_transfer.py transfer <slug> --product crm --only records/Leads
python3 scripts/zoho_transfer.py transfer <slug> --product crm
```

**Pilot first.** `--only records/Leads` is an end-to-end smoke (credentials
+ firewall + SAS + azcopy) that also measures records/s and credit burn —
that is the only honest input to a timeline. Then run the full product.

The puller walks units in order: `settings`, then per module
`records/<M>` (the ledger) and `bulk/<M>` (the archival Bulk Read ZIP),
then `notes`, then per module `emails/<M>` and `attachments/<M>`. Each unit
azcopies **as soon as it is whole**, so a mid-run VM loss costs one unit,
not the pass. CRM and Learn use separate tmux windows and may run
concurrently; refusal is per window, so starting Learn while CRM runs is
fine.

Exit 0 even with per-unit skips; exit 2 when a unit failed (verify is the
completeness authority). Re-running is always safe: `.cdp-complete` markers
skip finished units, `.cdp-cursor.json` resumes a partial module **mid-walk**,
and `--overwrite=false` skips landed blobs.

### 5. status `<slug>`

```bash
python3 scripts/zoho_transfer.py status <slug> --product crm
```

Per-product progress, manifest summary, log tail, disk free. **A
`rate-limited; sleeping Ns` line is normal pacing against Zoho's API
credits, not a hang** — say so rather than intervening.

### 6. verify `<slug> --product <p>` — then teardown

```bash
python3 scripts/zoho_transfer.py verify <slug> --product crm
python3 scripts/zoho_transfer.py verify <slug> --product learn
```

Laptop-side, needs **no Zoho credentials** — it lists the product's dest
prefix (rl SAS via the laptop IP-rule path) and compares it against the
uploaded `manifest.json`. Per unit: the `.cdp-complete` marker must be in
the container and the byte rollup must be ≥ what was staged.

- `failed_units` / `missing_markers` / `short_uploads` → rc 2 → re-run
  transfer, then verify again.
- `stale_extra` alone is informational: no-overwrite kept an earlier pass's
  longer file.
- `skipped_units` are **deliberate and never a failure** — read their
  reasons out loud (`endpoint-absent` = the feature isn't enabled;
  `permission-denied` = the API user's CRM profile lacks that module).

It certifies **staged → container** only. It makes no source-size claim,
and here that is forced rather than chosen: Zoho publishes no record byte
size. The `record_census` it reports is the puller's own count.

Then wrap up: remind the user to have the client **revoke the refresh token
and delete the Self Client**; the SAS lapses on its own within 21 days; the
laptop IP rule is already gone. Teardown last:

```bash
python3 scripts/zoho_transfer.py teardown <slug> --confirmed
```

## Judgment notes

- **The data center is the day-one stall.** Three detections exist because
  the symptom otherwise looks like a generic auth failure and burns an
  afternoon. When a mint fails with `invalid_code`, the message names both
  causes — wrong DC *or* revoked token — on purpose; check the DC first,
  it is the cheaper of the two to rule out.
- **Required vs optional is a fixed table, not a per-call judgment.** Only
  `settings` and `records/*` are required; a scope or permission failure on
  anything else becomes a recorded skip. So a pull can legitimately finish
  green with no emails and no attachments — always report the
  `skipped_units` reasons rather than just the rc.
- **HTTP 204 means an empty module, not a broken one.** Treating 204's
  empty body as a parse error is the classic Zoho client bug; the puller
  records `records: 0` and marks the unit complete.
- **403 `NO_PERMISSION` is a profile problem, not a scope problem.** The
  fix is the client's CRM admin granting the API user's profile access to
  that module — regenerating the token will not help.
- **Credits are a clock, not a bug.** If a pass ends on
  `credits-exhausted`, that is the daily org budget, not a defect. Re-run
  tomorrow; cursors and markers make it a resumption, not a restart. Say
  this plainly instead of debugging it.
- **Bulk Read results expire after a day** and downloads are capped at
  10/minute. An expired result is re-submitted as a fresh job, never
  resumed — the ZIP is only a cross-check, and its row count differing
  from the ledger by a few records is snapshot skew, not corruption.
- **Learn's knowledge base is discovered, never promised.** Report
  `_meta/discovery.json` before quoting Learn scope. Courses-only is a
  legitimate, common outcome.
- **WorkDrive is "whatever the token can see."** `_meta/boundary.json`
  records what was reachable AND what was not; the boundary is a client
  scope conversation, never a silent omission.
- **A wide CRM module costs extra calls, by design.** When a module has
  more fields than one request can carry, the extra chunks are re-fetched
  per page by record id and merged — a page_token is never reused across
  different queries, because that is undefined and would silently drop
  columns.
- **Measured rates (song-division, 2026-08-25) — use these to plan.** Record
  walk ~**56 records/s** (a wide module costs ~5 calls/page because v8 caps
  `fields` at **50** per request, so >50 fields means chunked follow-ups by
  record id). Attachment census via `/crm/v8/Attachments` ~**300 rows/s** —
  the whole tenant in under a minute. The per-record **email sweep is
  ~1.3 records/s**, which is the one leg that can turn a job into days.
- **Scope the email sweep with `--email-modules`.** It is ONE call per
  record, so on a 251k-record org it is ~54 h unscoped versus ~12 h for the
  four modules that actually carry mail (Contacts, Leads, Deals, Accounts).
  Tasks and Notes usually dominate the record count and carry none — check
  a sample before committing, and remember the units are independent, so
  emails can be a later pass over a finished corpus.
- **`fields` is capped at 50.** Above it, v8 returns
  `400 LIMIT_EXCEEDED {"param_name":"fields"}` and the unit is FATAL by
  design — better a loud stop than a ledger silently missing columns.
- **A Zoho 200 can still be a refusal.** Learn especially answers Access
  Denied / INVALID_METHOD / EXTRA_PARAM_FOUND as HTTP 200 with a
  `{"result":"failure"}` body. The client raises on those, but if you are
  ever reading raw output, do not read HTTP 200 as success.
- **Never use `--since` on a first pass.** It is a Modified_Time top-up and
  would produce a partial ledger that looks complete.
- One cycle **per product**, and verify per product. `write-dest` must be
  re-run when switching, or the new product's blobs land under the old
  prefix.
- All three products share the `zoho-export` base, so
  `expected-data-sizes.json` needs `"source_split": ["zoho-export"]` plus a
  `"prefix": "zoho-export/<product>"` pin on each declared service —
  otherwise three manifest lines collide on one prefix. A product that was
  never run reads as `declared-empty`; that is expected, not a failure.
- `--dry-run` works on every subcommand and prints the exact az/ssh/REST
  calls without making them.
