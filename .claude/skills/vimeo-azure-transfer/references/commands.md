# vimeo-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/vimeo_transfer.py` (standalone — NOT built
on transfer_engine.py) runs under the hood. Reference it for debugging and
manual rescue; never bypass the script for normal operation.

## Script subcommand map

```bash
export PATH="/opt/homebrew/bin:$PATH"
S=python3\ scripts/vimeo_transfer.py     # all take <slug>; JSON on stdout

$S plan <slug>                           # read-only, offline: dest, layout
$S probe <slug> <<'EOF'                  # token on STDIN; NO Azure calls
<vimeo personal access token>
EOF
#   the plan gate: download_capable / api_version_used / range_probe
$S pull <slug> <<'EOF'                   # token on STDIN; resumable
<vimeo personal access token>
EOF
#   --video-limit N        first-run smoke (auth+firewall+SAS+copy e2e)
#   --sas-days D           write-SAS expiry (default 2 — multi-hour copies)
#   --api-version 3.2      only if probe reported it
#   --block-size-mb N      Put Block From URL block size (default 256 MiB)
#   --single-shot-max-mb N single-request copy threshold (default 1024;
#                          Azure hard cap 5000 MB)
$S verify <slug> <<'EOF'                 # fresh API listing + sizes vs container
<vimeo personal access token>
EOF
```

Common flags: `--dry-run`, `--root`, `--dest-prefix` (default
`vimeo-export`). Exit codes: 0 ok, 1 hard error, 2 refusal / check-failed
(probe not download-capable; verify with missing/size-mismatched videos).
Progress heartbeat goes to stderr (`progress N/M videos, copied=…,
gb_copied=…` per video; `progress <id>: block i/n` inside big copies);
stdout stays one JSON object. Re-running `pull` is always safe — it lists
the dest prefix first and skips every video whose media blob exists
(keyed by `videos/<id>/` directory, so a title rename can't defeat it).

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
# Vimeo API (Authorization: Bearer <token>;
#            Accept: application/vnd.vimeo.*+json;version=3.4 — or 3.2)
GET https://api.vimeo.com/me?fields=name,account,upload_quota  # probe/account
GET https://api.vimeo.com/me/videos?per_page=100&fields=uri,name,...,download,
    files,play,pictures.sizes,metadata.connections.texttracks
    # paging.next loop; always with fields= (unfiltered = harsher rate limit)
GET https://api.vimeo.com/videos/<id>/texttracks   # caption list; each track's
                                                   # `link` is downloaded (KBs)
GET https://api.vimeo.com/me/projects              # folders → _meta/folders
GET https://api.vimeo.com/projects/<id>/videos     # folder → video-id map
GET https://api.vimeo.com/me/albums                # showcases → _meta/showcases
GET <download link>                # 302 → signed CDN URL. Resolved locally
                                   # (Range: bytes=0-0), because Azure
                                   # copy-from-URL never follows redirects.
                                   # Expires in HOURS — re-resolved on every
                                   # retry, never cached.

# Azure blob REST (x-ms-version: 2021-08-06; SAS as query string)
GET https://<sa>.blob.core.windows.net/<slug>-raw?restype=container&comp=list
    &prefix=vimeo-export/&maxresults=5000[&marker=...]   # resume seed + verify
PUT .../<slug>-raw/vimeo-export/videos/<id>/metadata.json      # small JSON/VTT
    x-ms-blob-type: BlockBlob
    If-None-Match: *          # create-only: 409 = already landed (benign)
PUT .../videos/<id>/<name>.mp4                     # file ≤ single-shot max:
    x-ms-copy-source: <signed CDN URL>             # Azure fetches server-side
    x-ms-source-content-md5: <vimeo md5, b64>      # Azure validates the bytes
    If-None-Match: *
PUT .../videos/<id>/<name>.mp4?comp=block&blockid=<b64>   # file > threshold:
    x-ms-copy-source: <signed CDN URL>                    # one block per
    x-ms-source-range: bytes=<start>-<end>                # 256 MiB range
PUT .../videos/<id>/<name>.mp4?comp=blocklist      # the commit — the moment
    If-None-Match: *                               # the blob exists, hence
    x-ms-blob-content-md5: <vimeo md5, b64>        # create-only rides HERE
```

Blob layout produced:

```
vimeo-export/
  videos/<video_id>/<safe-name>.<ext>   # source quality, else best available
  videos/<video_id>/metadata.json       # full raw video JSON from the listing
  videos/<video_id>/texttracks/<lang>-<name>.vtt
  _meta/videos-index-<ts>.json          # per-video outcomes + run stats
  _meta/folders-<ts>.json               # folder tree + per-folder video ids
  _meta/showcases-<ts>.json
  _meta/account-<ts>.json               # /me incl. upload_quota
  _meta/thumbnails-manifest-<ts>.json   # {video_id: [urls]} — NOT downloaded
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Vimeo API 401/403 ... token invalid` | Token wrong, deleted, or missing scopes | Fresh token with public+private+video_files (developer.vimeo.com → My Apps). |
| probe `download_capable: false` (rc 2) | The Vimeo plan doesn't include API file access (needs Standard/Advanced/Pro+), or the token lacks `video_files` | Plan/scope conversation with the client — NOT a retry. |
| download/files empty under 3.4, probe says `api_version_used: 3.2` | Known Vimeo API quirk (their own SDK issue #194) | Pass `--api-version 3.2` to pull and verify. |
| Persistent 429s | Vimeo rate limiting (rolling window, plan-dependent) | The script honors Retry-After / backs off. Wait and re-run if a run dies on them. Never parallelize by hand. |
| `CannotVerifyCopySource` in block errors | The signed CDN URL expired mid-copy (they live hours) | NORMAL — the script re-resolves and retries the same block (up to 20×/video). Only escalate if a single video exhausts the budget. |
| `Md5Mismatch` on a small-file copy | Azure fetched bytes that don't match Vimeo's declared md5 | Real integrity failure. Re-run pull for that video; repeats → suspect the file on Vimeo's side. |
| `403 AuthorizationFailure` on list/PUT right after start | IP-rule propagation (~60s) | The script waits and retries on its own. NOT a SAS problem — never re-mint for a 403. |
| `first 5 video copies all failed — systemic` | SAS/firewall/CDN/API broken, not per-video flakiness | Check the error text: 403 = firewall (wait, re-run); `CannotVerifyCopySource` on everything = CDN/resolve issue (probe again); signature error = re-run pull (fresh mint is automatic). |
| `409 BlobAlreadyExists` mentions | Create-only commit hit a blob from a crashed run | Benign — counted as skipped, never an error. |
| verify `missing` / `size_mismatch` (rc 2) | Videos failed, were added since the pull, or a copy was cut short | Re-run pull (resume), verify again. Same ids twice → inspect with the client. Never delete — investigate first. |
| verify `extra` non-empty | Videos deleted in Vimeo after the pull | Informational — the export legitimately has more than the live account. |
| `no_file` / `no_file_api_side` ids | Vimeo exposes no file links for those videos (upload still processing, or plan edge cases) | Informational. Persistent → check those videos with the client. |
| `range_probe: no-range-support` in probe | CDN refused `Range: bytes=0-0` | Files above `--single-shot-max-mb` can't be block-copied. Raise it (hard cap 5000 MB) or investigate before the full pull. |

## Secrets hygiene invariants (grep-testable)

- The Vimeo token transits: client secure channel → user chat → heredoc
  stdin → this process's memory. Never argv (ps-visible), never env, never
  files, never echoed (dry-run prints `Bearer <token-redacted>`).
- The SAS is minted in-process, rides only as a URL query string on live
  requests, and is never printed (dry-run shows `<sas-redacted>`). 2-day
  expiry, never revoked — it lapses on its own.
- The signed CDN URLs Azure copies from are themselves short-lived Vimeo
  credentials — they appear only in request headers, never in output.
- After the engagement the client deletes the token (developer.vimeo.com →
  My Apps); nothing persists on our side.
