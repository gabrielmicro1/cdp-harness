# figma-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/figma_transfer.py` (engine lifecycle from
`scripts/transfer_engine.py`, VM-side puller `scripts/figma_vm_pull.py`)
runs under the hood. Reference it for debugging and manual rescue; never
bypass the script for normal operation.

## Script subcommand map

```bash
S="python3 scripts/figma_transfer.py"

$S discover      <slug>                        # reconstruct the phase from Azure
$S plan          <slug> --team-ids 1,2 --plan <p>  # offline; the confirm gate
$S create-vm     <slug> --team-ids 1,2 --plan <p>  # billing starts here
$S allow-network <slug>                        # service endpoint + vnet-rule
$S write-dest    <slug>                        # racwl SAS -> rclone.conf + dest-figma.env
$S check-azure   <slug>                        # prove the VM can reach the container
$S write-creds   <slug>                        # 1 stdin line -> 600 figma.env + smoke test
$S probe         <slug> --team-ids 1,2 --plan <p>  # laptop-only gate, no Azure
$S transfer      <slug>                        # push puller, tmux window "figma"
$S status        <slug>                        # progress + manifest + log tail
$S verify        <slug>                        # laptop-side, rl SAS, no Figma creds
$S teardown      <slug> --confirmed            # refuses without --confirmed
```

Common flags: `--root` (companies dir), `--team-ids 1,2` (comma-separated,
from the client's file-browser URLs — required for plan/create-vm/probe,
later read from the `figma_team_ids` VM tag; there is NO API that lists
teams), `--plan starter|pro|org|enterprise` (the pacing schedule; rides the
`figma_plan` tag; unset = the starter floor), `--dest-prefix`
(default `figma-export`), `--sas-days` (21), `--dry-run` on everything.

Transfer flags: `--limit N` (pilot: first N file units), `--only <unit>`
(one unit — `meta`, `library/<team>`, a full `files/...` label, or a bare
file key), `--refresh` (ignore markers AND cursors; re-pulled units upload
with overwrite so a stale export can't strand), `--skip-upload`,
`--no-render-pages` (skip per-page PNGs — roughly halves the Tier-1 wall
clock), `--no-fills` (skips the embedded bitmaps — usually loses bytes:
fill URLs expire in ≤14 days), `--no-comments`, `--no-versions`,
`--no-library`, `--fill-workers N` (default 4, cap 8 — CDN pool, separate
from the API tiers), `--rate-sleep-max S` (default 900).

Probe flags: `--sample-files N` (editorType sample size, default 5).

Exit codes: **0** ok, **1** hard error, **2** refusal or check-failed
(unconfirmed teardown, verify found missing/short units, a transfer pass
that finished with failed units).

Re-running `transfer` is always safe: per-unit `.cdp-complete` markers skip
finished files, the library `.cdp-cursor.json` resumes mid-walk, fill
downloads resume by file existence, and azcopy `--overwrite=false` skips
landed blobs.

## Stdin heredocs

Both `probe` and `write-creds` take exactly one line:

```bash
python3 scripts/figma_transfer.py write-creds <slug> <<'EOF'
figd_AbC...          # 1. the personal access token (Full/Dev seat)
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

# create-vm tags (the whole comma-separated team list is ONE tag value;
# ARM caps tag values at 256 chars, so the script refuses past ~240 —
# run one cycle per team batch instead)
--tags purpose=figma-transfer engagement=<slug> figma_team_ids=1,2 \
       dest_container=<slug>-raw dest_prefix=figma-export figma_plan=<p>
```

## Underlying Figma calls

```
# every api.figma.com call carries `X-Figma-Token: <PAT>`; the tier tags
# below are the rate buckets (per-user, per-plan; Tier 1 caps at 20/min
# even on Enterprise and is the whole job's clock)

GET  /v1/me                                    # T3; token self-test.
       A dead token is 403, NOT 401 — and Figma does not say whether it
       was never valid or expired (PATs live <=90 days).
GET  /v2/teams/<id>/folders                    # T2; the walk root.
GET  /v2/folders/<id>/folders                  # T2; subfolders NEST — recurse
GET  /v2/folders/<id>/files?branch_data=true   # T2; full list, NO pagination
       Listings carry NO editor type — FigJam/Slides presence is only
       knowable per file. Branch keys are ordinary file keys.
GET  /v1/teams/<id>/projects                   # T2; DEPRECATED v1 fallback —
GET  /v1/projects/<id>/files                   #     new PATs may lack its
                                               #     scope; recorded if used
GET  /v1/files/<key>                           # T1; THE document (node tree).
       Oversized file -> 400 "try a smaller request" or 5xx -> decompose:
GET  /v1/files/<key>?depth=1                   # T1; pages only
GET  /v1/files/<key>/nodes?ids=a,b,c           # T1; per-node subtrees (<=50 ids)
GET  /v1/files/<key>/comments                  # T2; no pagination.
       (comment REACTIONS are cursor-paginated per comment — out of scope)
GET  /v1/files/<key>/versions                  # T2; follow pagination.next_page
       URL VERBATIM (host-checked: the token never leaves api.figma.com)
GET  /v1/files/<key>/images                    # T2; image-fill URL map
       {meta:{images:{imageRef: presigned-url}}} — URLs expire <=14 DAYS,
       so the map is ALWAYS re-fetched fresh; never cache it
GET  /v1/images/<key>?ids=a,b&format=png&scale=1   # T1; page renders,
       batched ids (batching is free — the CALL is what's metered).
       A 200 can carry null per-node values = that render FAILED; the
       map is guaranteed to name every requested id. URLs expire 30 days.
GET  /v1/teams/<id>/components?page_size=1000[&after=..]   # T3
GET  /v1/teams/<id>/component_sets / styles    # T3; published assets ONLY
       (unpublished ones live inside each file's own JSON, already staged)
# Dead ends, recorded so nobody re-derives them:
#   there is NO endpoint that lists an org's teams — ids come from URLs
#   there is NO .fig source-file export — the corpus is a derivative
#   variables endpoints exist but need an Enterprise FULL seat
#   429 headers: Retry-After (honored exactly), X-Figma-Plan-Tier,
#   X-Figma-Rate-Limit-Type ("low" = View/Collab seat = 6 T1 calls/MONTH
#   = the wrong-seat day-one stall; fatal, never slept through)

# CDN asset downloads (fills, renders): plain GET on the presigned URL,
# NO auth header of any kind — the token must never ride to a third-party
# host. The CDN throttles separately from the API tiers.
```

Blob layout produced:

```
<slug>-raw/figma-export/
├── manifest.json              # verify's authority
├── progress.json
├── meta/                      .cdp-complete · teams.json ·
│                              folders.jsonl · files.jsonl   ← the ledger
├── library/<team_id>/         .cdp-complete · .cdp-cursor.json ·
│                              components.jsonl · component_sets.jsonl ·
│                              styles.jsonl
└── files/<team_id>/<file_key>__<safe-name>/
    ├── .cdp-complete
    ├── file.json              # key, name, editorType, version,
    │                          # folder_path, per-leg outcomes, decomposed
    ├── document.json          # the node tree (or depth-1 when decomposed)
    ├── nodes/<node_id>.json   # only when decomposed
    ├── comments.json · versions.json · fills-manifest.json
    ├── fills/<imageRef>.<ext> # embedded bitmaps (ext from Content-Type)
    ├── renders/<node_id>.png  # one per page (absent under --no-render-pages)
    └── branches/<branch_key>/document.json
```

The file KEY leads the unit path (names are mutable; keys are not — a
rename between passes must not orphan the marker), and the folder path is
deliberately NOT in the blob path (folder renames would do the same); the
tree lives in `meta/files.jsonl` and each unit's `file.json`.

## VM-side layout & manual rescue

```
~/.config/xfer/figma.env       # 600: FIGMA_TOKEN
~/.config/xfer/dest-figma.env  # 600: AZURE_DEST_URL / AZURE_DEST_SAS / ...
~/xfer-figma/figma_vm_pull.py  # re-pushed fresh on every transfer
~/xfer-figma/pull-figma.log    # the heartbeat status tails
~/xfer-figma/dest/             # staging; the container tree mirrors this
```

```bash
ssh azureuser@<ip>
tmux ls; tmux attach -t transfer
tail -f ~/xfer-figma/pull-figma.log
cat ~/xfer-figma/dest/progress.json
python3 -m json.tool ~/xfer-figma/dest/manifest.json | head -40
# exactly where the library walk stopped:
cat ~/xfer-figma/dest/library/<team_id>/.cdp-cursor.json
# re-run the upload by hand:
set -a; . ~/.config/xfer/dest-figma.env; set +a
azcopy copy "$HOME/xfer-figma/dest/*" "$AZURE_DEST_URL?$AZURE_DEST_SAS" \
  --recursive --overwrite=false
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| probe/`/v1/me` refused with 403 | token invalid OR expired — Figma uses 403 for both and does not say which | client re-issues the PAT (Settings → Security), re-run `write-creds` |
| 429 with `X-Figma-Rate-Limit-Type: low` | the token owner holds a **View/Collab seat** (6 Tier-1 calls per MONTH) | the wrong-seat day-one stall — token must come from a Full or Dev seat; retrying cannot help |
| team walk 403/404 | token owner not a member of that team, OR a mis-copied id — Figma does not distinguish | check the id against `figma.com/files/team/<ID>/...` FIRST (cheaper), then the membership conversation |
| probe census smaller than the client expects | a team they forgot to name — no API lists teams, so it is invisible | read the census back to them; add the id and re-probe |
| a previously working run goes all-403 mid-pass | **PAT expiry** (≤90-day tokens), or revocation | fresh token → `write-creds` → re-run transfer; markers make it a resumption |
| `scope_probe` shows a family refused | that scope was not enabled at token creation (Figma documents no missing-scope response code, hence the probe) | client regenerates with the full checklist; families other than file_content degrade to recorded skips |
| `400 Request timeout, try a smaller request` / 5xx on one file | the file is too large for one response — documented behavior | automatic: the puller decomposes (depth-1 + per-node JSON); `decomposed_files` in verify is informational |
| render map has null values on a 200 | that node's render failed server-side (documented) | retried once, then recorded as `render_nulls` — informational, not a defect |
| fill download errors | the presigned URL expired (≤14 days) mid-run or CDN throttling | re-run transfer — the URL map is re-fetched fresh and existing files are skipped |
| `rate-limited; sleeping Ns` / pacing sleeps in the log | Figma's per-minute tiers; Retry-After honored, client-side pacing at 0.9× the documented caps | normal metering — do not intervene; Tier 1 is the clock and big workspaces run for days |
| unit skipped `no-access-or-missing` | the file is invisible to the token OR was deleted — Figma does not say which | deliberate skip, never a failure; report the count, don't over-diagnose |
| `check-azure` 403 | vnet-rule missing or propagating | re-run `allow-network`; company infra may strip rules not added through their UI |
| `verify: no-manifest` | the pull never finished a pass, or ran `--skip-upload` | run `status`, then `transfer` |
| `verify: short_uploads` | a partial azcopy | re-run `transfer` (no-overwrite skips what landed), verify again |
| `verify: stale_extra` | a `--refresh` pass wrote a shorter file, no-overwrite kept the longer old one | informational only |
| verify certifies an old pass | (the github pilot bug) `manifest.json` uploaded no-overwrite | cannot happen here: run metadata uploads with `--overwrite=true` from day one; corpus blobs stay no-overwrite |
| team-id list refused (>240 chars) | ARM caps tag values at 256 | one cycle per team batch, same `--dest-prefix` — units are keyed per team, batches never collide |

## Secrets hygiene invariants (grep-testable)

- The token transits: client secure channel → user chat → heredoc stdin →
  ssh stdin → `~/.config/xfer/figma.env` (600) on the VM → the puller's
  process env. Never argv (ps-visible), never VM tags, never laptop files,
  never echoed (dry-run prints `token-redacted` and the sentinel never
  appears in stdout).
- The token goes out on exactly one header, to `api.figma.com` only —
  NEVER on a presigned CDN URL fetch (those URLs are their own credential;
  adding ours would leak it to a third-party host), and pagination URLs
  are host-checked before being followed.
- The SAS appears only in `dest-figma.env` and `rclone.conf` on the VM;
  raw azcopy output is never echoed wholesale because a URL line leaks
  `sig=`.
- `verify` takes **no Figma credentials at all** — it is a blob listing
  plus a manifest read.
- The token dies with the VM at teardown; the client revokes it
  afterwards (it would lapse on its own within 90 days regardless).
