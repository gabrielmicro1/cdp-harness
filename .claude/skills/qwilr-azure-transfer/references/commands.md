# qwilr-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/qwilr_transfer.py` (standalone — NOT built
on transfer_engine.py) runs under the hood. Reference it for debugging and
manual rescue; never bypass the script for normal operation.

## Script subcommand map

```bash
export PATH="/opt/homebrew/bin:$PATH"
S=python3\ scripts/qwilr_transfer.py     # all take <slug>; JSON on stdout

$S plan <slug>                           # read-only: dest, layout, approach
$S pull <slug> <<'EOF'                   # token on STDIN; resumable
<qwilr access token>
EOF
#   --page-limit N   first-run smoke (auth+firewall+SAS+PUT end to end)
#   --sas-days D     write-SAS expiry (default 1 — lives only in-process)
$S verify <slug> <<'EOF'                 # fresh API listing vs container
<qwilr access token>
EOF
```

Common flags: `--dry-run`, `--root`, `--dest-prefix` (default
`qwilr-export`). Exit codes: 0 ok, 1 hard error, 2 refusal / check-failed
(verify with missing pages). Progress heartbeat goes to stderr
(`progress N/M pages, errors=K` every 25 pages); stdout stays one JSON
object. Re-running `pull` is always safe — it lists the dest prefix first
and skips every blob that already exists.

## Underlying az templates

```bash
# Write SAS (pull) — container-scoped, racwl, 1 day; held in-process only
az storage container generate-sas --account-name <sa> -n <slug>-raw \
  --permissions racwl --expiry <now+1d> --https-only -o tsv

# Read SAS (verify) — the standard sizing-path mint (rl, account SAS, 1 day)
az storage account generate-sas --account-name <sa> --services b \
  --resource-types sco --permissions rl --expiry <now+1d> --https-only -o tsv

# Firewall (run by the script itself; laptop external IP, so IP rules work —
# unlike same-region VMs). Added only if defaultAction=Deny and our IP is
# missing; ~60s propagation; removed at the end ONLY if we added it.
az storage account show -n <sa> -g <rg> --query networkRuleSet
az storage account network-rule add -g <rg> --account-name <sa> --ip-address <our-ip>
az storage account network-rule remove -g <rg> --account-name <sa> --ip-address <our-ip>
```

## Underlying REST calls

```
# Qwilr API (Authorization: Bearer <token>)
GET https://api.qwilr.com/v1/pages?limit=100[&cursor=...]   # summaries, cursor loop
GET https://api.qwilr.com/v1/pages/<pageId>                 # full page (blocks,
                                                            # acceptance, payment)
GET https://api.qwilr.com/v1/users            # → account/users.json
GET https://api.qwilr.com/v1/blocks/saved     # → account/saved-blocks.json
GET https://api.qwilr.com/v1/taxes            # → account/taxes.json
GET https://api.qwilr.com/v1/payment-gateways # → account/payment-gateways.json
GET https://api.qwilr.com/v1/webhooks         # → account/webhooks.json

# Azure blob REST (x-ms-version: 2021-08-06; SAS as query string)
GET https://<sa>.blob.core.windows.net/<slug>-raw?restype=container&comp=list
    &prefix=qwilr-export/&maxresults=5000[&marker=...]      # resume seed
PUT https://<sa>.blob.core.windows.net/<slug>-raw/qwilr-export/pages/<id>.json
    x-ms-blob-type: BlockBlob
    If-None-Match: *          # create-only: 409 = already landed (benign)
```

Blob layout produced:

```
qwilr-export/
  pages/<pageId>.json               # full page JSON, pretty-printed
  account/{users,saved-blocks,taxes,payment-gateways,webhooks}.json
  _meta/pages-index-<ts>.json       # all summaries + run stats (one per run)
  _meta/assets-manifest-<ts>.json   # {pageId: [CDN urls]} — NOT downloaded
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Qwilr API 401/403 ... token invalid` | Token wrong, revoked, or account lacks API access | Fresh token from the client admin (Settings → API). If they can't create one, their plan may not include API — Qwilr support conversation, not a retry. |
| Token validation ok, then 401 mid-run | Token revoked while running | Fresh token, re-run pull — resume skips everything landed. |
| Persistent 429s | Qwilr rate limiting (limits unpublished) | The script honors Retry-After / backs off exponentially. If a run dies on them, wait a few minutes and re-run. Never parallelize by hand. |
| `403 AuthorizationFailure` on list/PUT right after start | IP-rule propagation (~60s) | The script waits and retries on its own. NOT a SAS problem — never re-mint for a 403. |
| `first 5 page uploads all failed — systemic` | SAS/firewall/API broken, not per-page flakiness | Check the error text: 403 = firewall (wait, re-run), signature error = re-run pull (fresh mint is automatic). |
| `409 BlobAlreadyExists` mentions in errors | Create-only PUT hit a blob from a crashed run | Benign — counted as skipped, never an error. |
| verify `missing` non-empty (rc 2) | Pages failed or were added since the pull | Re-run pull (resume), verify again. Same ids missing twice → inspect those pages with the client. |
| verify `extra` non-empty | Pages deleted in Qwilr after the pull | Informational — the export legitimately has more than the live account. |
| `/pages` returns empty but the account "has proposals" | Token from the wrong account/workspace | Confirm with the client which account the token came from. |
| Summaries missing an id field (`(no-id)` error) | API response shape drifted | Inspect one summary by hand (`GET /pages?limit=1`), update `_page_id` in the script. |

## Secrets hygiene invariants (grep-testable)

- The Qwilr token transits: client secure channel → user chat → heredoc
  stdin → this process's memory. Never argv (ps-visible), never env, never
  files, never echoed (dry-run prints `Bearer <token-redacted>`).
- The SAS is minted in-process, rides only as a URL query string on live
  requests, and is never printed (dry-run shows `<sas-redacted>`). 1-day
  expiry, never revoked — it lapses on its own.
- After the engagement the client revokes the token (Qwilr → Settings →
  API); nothing persists on our side.
