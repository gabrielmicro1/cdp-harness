# zoom-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/zoom_transfer.py` (standalone — NOT built
on transfer_engine.py) runs under the hood. Reference it for debugging and
manual rescue; never bypass the script for normal operation.

## Script subcommand map

```bash
export PATH="/opt/homebrew/bin:$PATH"
S=python3\ scripts/zoom_transfer.py      # all take <slug>; JSON on stdout

$S plan <slug>                           # read-only, offline: dest, layout
$S probe <slug> <<'EOF'                  # 3 secrets on STDIN; NO Azure calls
<account id>
<client id>
<client secret>
EOF
#   the activation gate: token_ok / listing_ok / range_probe / retention
$S pull <slug> <<'EOF'                   # 3 secrets on STDIN; resumable
<account id>
<client id>
<client secret>
EOF
#   --meeting-limit N      first-run smoke (auth+firewall+SAS+copy e2e)
#   --sas-days D           write-SAS expiry (default 2 — multi-hour copies)
#   --from-date / --to-date  month-window range (default 2015-01-01..today)
#   --block-size-mb N      Put Block From URL block size (default 256 MiB)
#   --single-shot-max-mb N single-request copy threshold (default 1024;
#                          Azure hard cap 5000 MB)
$S verify <slug> <<'EOF'                 # fresh listing + exact sizes vs container
<account id>
<client id>
<client secret>
EOF
```

Common flags: `--dry-run`, `--root`, `--dest-prefix` (default
`zoom-export`; for orgs with SEVERAL Zoom accounts, run the whole cycle
once per account with `--dest-prefix zoom-export/<account-label>` —
S2S apps and their credentials are account-bound, and per-account
sub-prefixes keep verify's ground truth per-account). Exit codes: 0 ok, 1 hard error, 2 refusal / check-failed
(probe with `listing_ok: false`; verify with missing/size-mismatched
files). Progress heartbeat goes to stderr (`progress <YYYY-MM>:
meetings=…, files=…, gb_copied=…` per month, every 25 files, and
`progress <file>: block i/n` inside big copies); stdout stays one JSON
object. Re-running `pull` is always safe — it lists the dest prefix first
and skips every blob that already exists (names are deterministic:
`<start>_<TYPE>_<fileid>.<ext>`).

## Underlying az templates

```bash
# Write SAS (pull) — container-scoped, racwl, 2 days; held in-process only
az storage container generate-sas --account-name <sa> -n <slug>-raw \
  --permissions racwl --expiry <now+2d> --https-only -o tsv

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
# Zoom OAuth (Server-to-Server, account_credentials grant; token lives ~1h,
# auto-re-minted 5 min before expiry)
POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=<id>
     Authorization: Basic base64(<client id>:<client secret>)

# Zoom API (Authorization: Bearer <token>)
GET https://api.zoom.us/v2/accounts/me/recordings?from=<YYYY-MM-DD>
    &to=<YYYY-MM-DD>&page_size=300[&next_page_token=...]
    # THE listing. Literal `me` — a real accountId needs the `:master`
    # scope and 400s code 4711 otherwise. The endpoint caps the window to
    # ~1 month, so the range is walked month by month, and each month is
    # fully MATERIALIZED (all pages back-to-back) before any copy — a
    # next_page_token expires during multi-minute copies.
GET https://api.zoom.us/v2/meetings/<double-URL-encoded uuid>/recordings
    # re-resolve escape hatch when a stored download_url has died
    # (meeting UUIDs contain '/' and '+' → encoded TWICE)
GET https://api.zoom.us/v2/users/me           # account context → _meta
GET https://api.zoom.us/v2/users/me/settings  # retention (probe, guarded)
GET <download_url>?access_token=<token>  # 302 → signed URL. Resolved
                                   # locally (Range: bytes=0-0), because
                                   # Azure copy-from-URL never follows
                                   # redirects. Token + URL expire in
                                   # ~1h — re-resolved with a FRESH token
                                   # on every retry, never cached.

# Azure blob REST (x-ms-version: 2021-08-06; SAS as query string)
GET https://<sa>.blob.core.windows.net/<slug>-raw?restype=container&comp=list
    &prefix=zoom-export/&maxresults=5000[&marker=...]   # resume seed + verify
PUT .../<slug>-raw/zoom-export/meetings/<uuid>/metadata.json  # small JSON
    x-ms-blob-type: BlockBlob
    If-None-Match: *          # create-only: 409 = already landed (benign)
PUT .../meetings/<uuid>/<start>_MP4_<fileid>.mp4   # file ≤ single-shot max:
    x-ms-copy-source: <signed zoom URL>            # Azure fetches server-side
    If-None-Match: *                               # (Zoom declares no md5 —
                                                   # verify is size-exact)
PUT .../<...>.mp4?comp=block&blockid=<b64>         # file > threshold:
    x-ms-copy-source: <signed zoom URL>            # one block per
    x-ms-source-range: bytes=<start>-<end>         # 256 MiB range
PUT .../<...>.mp4?comp=blocklist                   # the commit — the moment
    If-None-Match: *                               # the blob exists, hence
                                                   # create-only rides HERE
```

Blob layout produced:

```
zoom-export/
  meetings/<safe-uuid>/<start>_<TYPE>_<fileid>.<ext>
      # MP4 video, M4A audio, TRANSCRIPT/CC .vtt, CHAT .txt, CSV polls,
      # TIMELINE/SUMMARY .json — every pullable recording_files entry
  meetings/<safe-uuid>/metadata.json    # the meeting's raw listing JSON
  _meta/recordings-index-<ts>.json      # per-file outcomes + run stats
  _meta/account-<ts>.json               # /users/me context
```

(`<safe-uuid>` = the meeting UUID with `/`→`_`, `+`→`-` — deterministic,
so resume keys are stable.)

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Client can't see "Server-to-Server OAuth" in Build App | The app type is hidden without the S2S role permission (owner has it by default), or they're in a personal/free account | Owner enables it: zoom.us → Admin → User Management → Roles → their role → Role Settings → Advanced Features → "Zoom for developers" → "Server-to-Server OAuth app" — or the owner creates the app themselves. Confirm company account + paid plan. |
| `Zoom OAuth token mint failed (HTTP 400/401)` | Account ID / Client ID / Client Secret wrong, or the app was deleted | Re-check the three values with the client (they often arrive as one multi-line blob — split carefully). NOT a retry. |
| probe `listing_ok: false` (rc 2), token_ok true | The S2S app was created but never ACTIVATED, or lacks `recording:read:admin` | Client conversation: activate the app on its Activation tab / add the scope. The #1 stall. NOT a retry. |
| 400 code 4711 on the listing | A literal accountId in the path (needs the `:master` scope) | The script always uses the literal `me` — if you see this you bypassed the script. Don't. |
| `Zoom API 401 ... even with a freshly minted token` | Credentials revoked / app deactivated mid-run | Fresh credentials from the client; re-run (resume skips landed files). One 401 is auto-handled — this error means the SECOND one. |
| Persistent 429s | Zoom account-wide rate limits | The script honors Retry-After / backs off. Wait and re-run if a run dies on them. Never parallelize by hand. |
| `CannotVerifyCopySource` in copy errors | The token/URL embedded in the copy source expired (~1h life) | NORMAL — the script re-mints and re-resolves, retrying the same block (up to 20×/file). Only escalate if one file exhausts the budget. |
| A month logged in `month_errors` | Transient listing failure (5xx/network) for that window | Re-run pull — resume skips landed files; the failed month re-lists. |
| `403 AuthorizationFailure` on list/PUT right after start | IP-rule propagation (~60s) | The script waits and retries on its own. NOT a SAS problem — never re-mint for a 403. |
| `first 5 file copies all failed — systemic` | SAS/firewall/token/API broken, not per-file flakiness | Check the error text: 403 = firewall (wait, re-run); `CannotVerifyCopySource` on everything = resolve issue (probe again); signature error = re-run pull (fresh mint is automatic). |
| `409 BlobAlreadyExists` mentions | Create-only commit hit a blob from a crashed run | Benign — counted as skipped, never an error. |
| `placeholders_skipped` / `placeholders_api_side` > 0 | Recordings still processing on Zoom's side (empty `file_type` rows) | Informational. Re-run pull in a day or two to pick them up. |
| verify `missing` / `size_mismatch` (rc 2) | Files failed, were added since the pull, or a copy was cut short | Re-run pull (resume), verify again. Same names twice → inspect with the client. Never delete — investigate first. |
| verify `extra` non-empty | Recordings deleted in Zoom (retention auto-delete or by hand) after the pull | EXPECTED — the export legitimately holds more than the live account. Informational; never delete. |

## Secrets hygiene invariants (grep-testable)

- The three S2S secrets transit: client secure channel → user chat →
  heredoc stdin (3 lines: Account ID, Client ID, Client Secret) → this
  process's memory. Never argv (ps-visible), never env, never files,
  never echoed (dry-run prints `Basic <credentials-redacted>` /
  `Bearer <token-redacted>`).
- The minted access token lives only in the TokenBox (process memory),
  ~1h, auto-re-minted; it rides in Authorization headers and as the
  `access_token` query on download URLs Azure copies from — never in
  output.
- The SAS is minted in-process, rides only as a URL query string on live
  requests, and is never printed (dry-run shows `<sas-redacted>`). 2-day
  expiry, never revoked — it lapses on its own.
- After the engagement the client deactivates/deletes the app
  (marketplace.zoom.us → Manage → Built Apps); nothing persists on our
  side.
