# zoho-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/zoho_transfer.py` (engine lifecycle from
`scripts/transfer_engine.py`, VM-side puller `scripts/zoho_vm_pull.py`) runs
under the hood. Reference it for debugging and manual rescue; never bypass
the script for normal operation.

## Script subcommand map

```bash
S="python3 scripts/zoho_transfer.py"

$S discover      <slug>                      # reconstruct the phase from Azure
$S plan          <slug> --dc <dc> --product <p>   # offline; the confirm gate
$S create-vm     <slug> --dc <dc> --product <p>   # billing starts here
$S allow-network <slug>                      # service endpoint + vnet-rule
$S write-dest    <slug> --product <p>        # racwl SAS -> rclone.conf + dest.env
$S check-azure   <slug>                      # prove the VM can reach the container
$S write-creds   <slug>                      # 4 stdin lines -> 600 zoho.env
$S probe         <slug> --dc <dc> --product <p>   # laptop-only gate, no Azure
$S transfer      <slug> --product <p>        # push puller, tmux window per product
$S status        <slug> [--product <p>]      # progress + manifest + log tail
$S verify        <slug> --product <p>        # laptop-side, rl SAS, no Zoho creds
$S teardown      <slug> --confirmed          # refuses without --confirmed
```

Common flags: `--root` (companies dir), `--dc` (com / eu / in / com.au / jp /
ca / sa / com.cn — required for plan/create-vm/probe, later read from the
`zoho_dc` VM tag), `--product crm|learn|workdrive` (required for plan/probe/
transfer/verify), `--portal <networkurl>` (required with `--product learn`),
`--dest-prefix` (the BASE; the product is appended), `--sas-days` (21),
`--dry-run` on everything.

Transfer flags: `--only records/Leads` (pilot one unit), `--limit N` (first N
modules), `--modules A,B` / `--skip-modules A,B`, `--refresh` (ignore markers
AND cursors), `--skip-upload`, `--no-attachments` / `--no-bulk` /
`--no-emails`, `--email-modules A,B` (scope the 1-call-per-record email
sweep — the difference between ~12 h and ~54 h on a large org),
`--attachment-workers N` (default 3, hard cap 5),
`--page-size N` (default and max 200), `--rate-sleep-max S` (default 3900),
`--since <ISO8601>` (Modified_Time top-up — **never on a first pass**).

Probe flags: `--sample-records N`, `--no-kb-probe` (skip Learn's undocumented
paths), `--no-count-probe` (skip the record-count + attachment census).

Exit codes: **0** ok, **1** hard error, **2** refusal or check-failed
(unconfirmed teardown, verify found missing/short units, a transfer pass that
finished with failed units).

Re-running `transfer` is always safe: per-unit `.cdp-complete` markers skip
finished units, `.cdp-cursor.json` resumes a partial module mid-walk, and
azcopy `--overwrite=false` skips landed blobs.

## Stdin heredocs

Both `probe` and `write-creds` take exactly four lines, in this order:

```bash
python3 scripts/zoho_transfer.py write-creds <slug> <<'EOF'
com                 # 1. data center suffix
1000.ABC...         # 2. Client ID
9f8e...             # 3. Client Secret
1000.def...abc      # 4. Refresh Token
EOF
```

## Underlying az templates

```bash
# write-dest — the ingest write path (racwl, 21-day default)
az storage container generate-sas --account-name <sa> -n <slug>-raw \
  --permissions racwl --expiry <+21d> --https-only -o tsv

# verify — the READ path (rl, 1 day), from this laptop's external IP
az storage account network-rule add -g <rg> --account-name <sa> \
  --ip-address <our-ip>          # removed again on the way out if we added it
az storage account generate-sas --services b --resource-types sco \
  --permissions rl --expiry <+1d> --https-only -o tsv

# allow-network — the VM path; IP rules never match same-region VM traffic
az network vnet subnet update -g <rg> --vnet-name <vnet> -n <subnet> \
  --service-endpoints Microsoft.Storage
az storage account network-rule add -g <rg> --account-name <sa> \
  --subnet <subnet-id>

# create-vm tags (dest_prefix is the BASE — no product suffix)
--tags purpose=zoho-transfer engagement=<slug> zoho_dc=<dc> \
       dest_container=<slug>-raw dest_prefix=zoho-export \
       zoho_product=<p> zoho_portal=<networkurl>
```

## Underlying Zoho calls

```
# auth (the access token lives ~1h and is never logged)
POST https://accounts.zoho.<dc>/oauth/v2/token
     grant_type=refresh_token&client_id=..&client_secret=..&refresh_token=..
  -> {"access_token": "..", "expires_in": 3600, "api_domain": "https://www.zohoapis.<dc>"}
     api_domain is Zoho telling us which DC the token belongs to — cross-checked.
     NOTE: OAuth failures come back as HTTP 200 with an error body.

# CRM — all calls carry `Authorization: Zoho-oauthtoken <token>`
GET  /crm/v8/org
GET  /crm/v8/settings/modules              # api_supported + deleted filter
GET  /crm/v8/settings/fields?module=<M>    # v8 makes `fields` MANDATORY below
GET  /crm/v8/settings/layouts?module=<M>
GET  /crm/v8/users | /crm/v8/settings/roles | /crm/v8/settings/profiles
GET  /crm/v8/<M>?fields=id,a,b&per_page=200[&page_token=..]
       info.more_records + info.next_page_token drive pagination
GET  /crm/v8/<M>?fields=<chunk>&ids=<id,id,..>   # wide modules: extra field
       chunks re-fetched per page (max 100 ids) and merged by id — a
       page_token is NEVER reused across different queries
POST /crm/bulk/v8/read                     # archival cross-check ZIP
GET  /crm/bulk/v8/read/<job_id>            # poll: ADDED -> IN PROGRESS -> COMPLETED
GET  /crm/bulk/v8/read/<job_id>/result     # {job_id}.zip containing a CSV
       10 downloads/min; the result EXPIRES after 1 day (re-submit, don't resume)
       EXCLUDES Notes, Attachments, Emails and related/cross modules
GET  /crm/v8/Notes?per_page=200
GET  /crm/v8/Attachments?fields=id,File_Name,Size,Parent_Id&per_page=200
       THE attachment path: the module is directly listable at ~300 rows/s,
       Size is an EXACT byte integer, and Parent_Id carries module + record
       id. Do NOT sweep /crm/v8/<M>/<recordId>/Attachments per record —
       that is 1 call per record (~2.2/s ⇒ ~32 h on a 251k-record org).
GET  /crm/v8/<pmod>/<pid>/Attachments/<attId>   # streamed to disk in chunks
GET  /crm/v8/<M>/<recordId>/Emails         # 404 = feature not enabled = skip
       1 call per record (~1.3/s) — scope it with --email-modules
GET  /crm/v8/<M>/actions/count             # exact per-module record count.
       NOT COQL: COQL makes `where` mandatory AND requires `group by` for
       count(id), so a plain per-module count is impossible through it.

# Learn — documented: courses. pageIndex is 0-based, limit max 99.
GET  https://learn.zoho.<dc>/learn/api/v1/portal/<networkurl>/course
       ?pageIndex=0&limit=99
# Learn KB — UNDOCUMENTED. Attempted, never assumed; outcome recorded in
# _meta/discovery.json. What actually answers (verified live 2026-08-25):
GET  /learn/api/v1/portal/<networkurl>/tag       # answers; ignores `limit`
GET  /learn/api/v1/portal/<networkurl>/quiz      # answers
GET  /learn/api/v1/portal/<networkurl>/customportal
       the DOCUMENTED manuals listing hangs off this
       (/customportal/<id>/manual) but it returns HTTP 200 + Access Denied
       (9001) unless a custom portal exists and the token may read it
# Dead ends, recorded so nobody re-derives them:
#   /manual, /space      -> route exists, rejects a bare GET (INVALID_METHOD)
#   /manuals, /articles  -> URL_RULE_NOT_CONFIGURED (no such route)
#   /search              -> INTERNAL_IP_ACCESS_ONLY (Zoho-internal)
#   there is NO portal-listing endpoint — the networkurl must be supplied

# WorkDrive — "whatever the token can see"; boundary recorded
GET  /workdrive/api/v1/users/me
GET  /workdrive/api/v1/users/<id>/teams
GET  /workdrive/api/v1/teams/<id>/teamfolders
GET  /workdrive/api/v1/files/<id>/files
GET  /workdrive/api/v1/download/<id>
```

Blob layout produced:

```
<slug>-raw/zoho-export/
├── crm/
│   ├── manifest.json          # verify's authority
│   ├── progress.json
│   ├── _meta/{org,modules-selected}.json
│   ├── settings/              .cdp-complete · modules/roles/profiles/users/
│   │                          currencies.json · fields/<M>.json · layouts/<M>.json
│   ├── records/<M>/           .cdp-complete · .cdp-cursor.json ·
│   │                          fields.json · records.jsonl   ← THE ledger
│   ├── bulk/<M>/              .cdp-complete · <job_id>.zip · job.json
│   ├── notes/                 .cdp-complete · notes.jsonl
│   ├── emails/<M>/            .cdp-complete | .cdp-skipped.json · emails.jsonl
│   └── attachments/            ONE unit (a whole-module walk), not per module
│                              .cdp-complete · index.jsonl ·
│                              <ParentModule>/<recordId>/<attId>__<safe-name>
├── learn/
│   ├── manifest.json · progress.json · _meta/discovery.json
│   ├── courses/               .cdp-complete · courses.jsonl
│   └── kb/<collection>/       ONLY if discovery found it; else kb/.cdp-skipped.json
└── workdrive/
    ├── manifest.json · progress.json · _meta/boundary.json
    └── files/<folderId>/      .cdp-complete · listing.jsonl · <fileId>__<name>
```

## VM-side layout & manual rescue

```
~/.config/xfer/zoho.env        # 600: ZOHO_DC / CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN
~/.config/xfer/dest.env        # 600: AZURE_DEST_URL / AZURE_DEST_SAS / ...
~/xfer-zoho/zoho_vm_pull.py    # re-pushed fresh on every transfer
~/xfer-zoho/pull-<product>.log # the heartbeat status tails
~/xfer-zoho/dest/<product>/    # staging; the container tree mirrors this
```

```bash
ssh azureuser@<ip>
tmux ls; tmux attach -t transfer          # one window per product
tail -f ~/xfer-zoho/pull-crm.log
cat ~/xfer-zoho/dest/crm/progress.json
python3 -m json.tool ~/xfer-zoho/dest/crm/manifest.json | head -40
# exactly where a module stopped:
cat ~/xfer-zoho/dest/crm/records/Leads/.cdp-cursor.json
# re-run the upload by hand:
set -a; . ~/.config/xfer/dest.env; set +a
azcopy copy "$HOME/xfer-zoho/dest/crm/*" "$AZURE_DEST_URL?$AZURE_DEST_SAS" \
  --recursive --overwrite=false
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| mint returns `invalid_code` / `invalid_grant` | **wrong data center**, or the refresh token was revoked / Self Client deleted | check the URL the client sees signed in (crm.zoho.eu → `--dc eu`) BEFORE re-requesting a token |
| `data-center mismatch: the refresh token belongs to https://www.zohoapis.eu` | Zoho's own `api_domain` contradicts `--dc` | re-run with the DC it names |
| `write-creds` refuses: stdin DC vs `zoho_dc` tag | the VM was created for a different DC | tear down and re-create with the right `--dc` |
| mint returns `invalid_client` | wrong Client ID/Secret, or the Self Client was deleted | client re-copies both from api-console.zoho.com |
| `401 OAUTH_SCOPE_MISMATCH` on settings/records | required scope missing | client regenerates with the full scope list — fatal, never retried |
| `401 OAUTH_SCOPE_MISMATCH` on emails/attachments/KB | optional scope missing | recorded skip; decide with the client whether it matters |
| `400 LIMIT_EXCEEDED` with `param_name: fields` | more than 50 field names in one request | v8 caps `fields` at 50; `FIELD_CHUNK` is 50 so this should not recur — fatal by design, never a silent short ledger |
| a Learn call "succeeds" but writes no data | Zoho returned HTTP 200 with a `{"result":"failure"}` body (Access Denied, INVALID_METHOD) | `body_failure()` turns these into real errors; re-run so discovery.json records the refusal honestly |
| verify certifies an old pass | `manifest.json` was uploaded no-overwrite | run metadata now uploads with `--overwrite=true`; corpus blobs stay no-overwrite |
| `403 NO_PERMISSION` on one module | the API user's CRM **profile** lacks module access | a CRM admin grants it; a new token will NOT help |
| unit completes with `records: 0` | HTTP 204 — the module is genuinely empty | normal, not an error |
| `rate-limited; sleeping Ns` in the log | Zoho throttling; Retry-After honored | normal pacing — do not intervene |
| pass ends with `credits-exhausted` | the daily org API-credit budget | re-run tomorrow; cursors make it a resumption |
| bulk unit `job-not-completed` / result 404 | Bulk Read result expired (1-day TTL) | re-run transfer; a fresh job is submitted |
| bulk `delta` non-zero vs the ledger | snapshot skew (walk and job ran minutes apart) | informational; the JSON ledger is the authority |
| Learn `kb` skipped `endpoint-absent` | the undocumented KB paths did not answer | expected; courses-only, raise a manual-export conversation |
| `check-azure` 403 | vnet-rule missing or propagating | re-run `allow-network`; company infra may strip rules not added through their UI |
| `verify: no-manifest` | the pull never finished a pass, or ran `--skip-upload` | run `status`, then `transfer` |
| `verify: short_uploads` | a partial azcopy | re-run `transfer` (no-overwrite skips what landed), verify again |
| `verify: stale_extra` | a `--refresh` pass wrote a longer file, no-overwrite kept the old one | informational only |

## Secrets hygiene invariants (grep-testable)

- The four credentials transit: client secure channel → user chat → heredoc
  stdin → ssh stdin → `~/.config/xfer/zoho.env` (600) on the VM → the
  puller's process env. Never argv (ps-visible), never VM tags, never
  laptop files, never echoed (dry-run prints `<token-redacted>` and the
  sentinels never appear in stdout).
- The minted access token is never logged, never in a URL we print, and
  lives only in `TokenBox`'s process memory.
- The SAS appears only in `dest.env` and `rclone.conf` on the VM; raw
  azcopy output is never echoed wholesale because a URL line leaks `sig=`.
- `verify` takes **no Zoho credentials at all** — it is a blob listing plus
  a manifest read.
- The credentials die with the VM at teardown; the client revokes the
  refresh token and deletes the Self Client afterwards.
