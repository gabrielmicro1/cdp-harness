# teams-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/teams_transfer.py` (engine lifecycle from
`scripts/transfer_engine.py`, VM-side puller `scripts/teams_vm_pull.py`)
runs under the hood. Reference it for debugging and manual rescue; never
bypass the script for normal operation.

## Script subcommand map

```bash
S="python3 scripts/teams_transfer.py"

$S discover      <slug>                          # reconstruct the phase from Azure
$S plan          <slug> --tenant-id <guid>       # offline; the confirm gate
$S create-vm     <slug> --tenant-id <guid>       # billing starts here
$S allow-network <slug>                          # service endpoint + vnet-rule
$S write-dest    <slug>                          # racwl SAS -> dest-teams.env (bare container URL)
$S check-azure   <slug>                          # prove the VM can reach the container
$S write-creds   <slug>                          # 3 stdin lines -> 600 teams.env + token smoke test
$S probe         <slug> --tenant-id <guid>       # laptop-only gate, no Azure, no VM
$S transfer      <slug>                          # push puller, tmux window "teams"
$S status        <slug>                          # progress + manifest + log tail
$S verify        <slug>                          # laptop-side, rl SAS, no Graph creds
$S teardown      <slug> --confirmed              # refuses without --confirmed
```

`plan`, `create-vm` and `probe` **require** `--tenant-id` (the CLI errors
out otherwise). `write-creds` does **not** require it — it falls back to the
VM's `teams_tenant_id` tag (set at `create-vm`), so the normal workflow only
passes `--tenant-id` once. Note that even on `probe`, the tenant actually
used to mint the token is stdin line 1 — pass the SAME tenant id to both
`--tenant-id` and the stdin heredoc, or the flag is just along for the ride.

Common flags: `--root` (companies dir, default `companies/`), `--rg`
(override VM resource group; default the company's RG), `--vm-size`
(default `Standard_D8s_v7`), `--os-disk-gb` (default: the Spec's 64 —
staging is JSONL + inline images, not video/DB dumps), `--dest-prefix`
(default `teams-export`, resolved for `plan`/`create-vm`; other commands
read the VM's `dest_prefix` tag when omitted), `--sas-days` (default 21),
`--dry-run` on everything.

Transfer flags: `--rps-messages` (float, override the puller's messages-
family pace; default is unset, which lets the puller use its own default of
4 req/s — Teams messaging is Graph's slow lane), `--limit-teams N` (pilot:
only the first N teams).

Teardown flags: `--confirmed` (required — the command refuses without it),
`--force` (skip the running-transfer check).

Exit codes: **0** ok, **1** hard error (bad credentials, timeout,
`common.HarnessError`), **2** refusal or check-failed (unconfirmed teardown,
verify found missing/short units, a transfer pass that finished with failed
units — mirrored by the CLI's own `result.get("ok", True)` check in `main()`).

Re-running `transfer` is always safe: per-channel `.cdp-complete` markers
skip finished channels, `.cdp-cursor.json` resumes mid-walk (a cursor whose
`next_link` now refuses is discarded and the channel is re-walked from
scratch — channels are small), hostedContents downloads resume by file
existence, and azcopy `--overwrite=false` skips landed blobs. The manifest
and other run metadata upload with `--overwrite=true`, so a `--limit-teams`
pilot's manifest is always replaced by the full run's.

## Stdin heredocs

Both `probe` and `write-creds` take **exactly 3 lines, in this order**:
tenant id, client id, client secret.

```bash
python3 scripts/teams_transfer.py write-creds <slug> <<'EOF'
72f988bf-86f1-41af-91ab-2d7cd011db47   # 1. tenant id (Entra Directory (tenant) ID, a GUID)
11111111-2222-3333-4444-555555555555   # 2. client id (the app registration's Application ID)
super-secret-value                     # 3. client secret
EOF
```

```bash
python3 scripts/teams_transfer.py probe <slug> --tenant-id 72f988bf-86f1-41af-91ab-2d7cd011db47 <<'EOF'
72f988bf-86f1-41af-91ab-2d7cd011db47
11111111-2222-3333-4444-555555555555
super-secret-value
EOF
```

A credential containing a single quote is refused outright (it would break
the VM's env file) — nothing is written. Under `--dry-run` with empty stdin,
placeholder values (`<tenant>` / `<client-id>` / `<secret>`) are used so a
dry run never blocks on a heredoc.

## Underlying az templates

```bash
# create-vm tags
--tags purpose=teams-transfer engagement=<slug> \
       teams_tenant_id=<tenant-guid> \
       dest_container=<slug>-raw dest_prefix=teams-export

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
```

`write-dest` writes the container SAS into `~/.config/xfer/dest-teams.env`
on the VM as `DEST_URL` (the **bare** container URL — unlike figma/zoho,
`teams_vm_pull.py` appends `DEST_PREFIX` itself), `DEST_SAS`, and
`DEST_PREFIX`. It also drops an `[azure]` rclone remote so `check-azure` has
something to test against, even though the puller itself never uses rclone.

## Underlying Graph calls

```
# token mint (client-credentials, app-only — no delegated/browser flow
# anywhere in this pull):
POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token
  grant_type=client_credentials&client_id=<id>&client_secret=<secret>
  &scope=https://graph.microsoft.com/.default

# directory family (paced separately from messages; required unless noted)
GET /groups?$filter=resourceProvisioningOptions/Any(x:x eq 'Team')
           &$select=id,displayName,description,createdDateTime,visibility
           &$top=100                          # required — team discovery
GET /teams/{id}                                # optional — per-team settings
GET /teams/{id}/channels                       # required — channel discovery
GET /users?$select=id,userPrincipalName,displayName,mail,accountEnabled,
           userType&$top=100                   # required — tenant roster
GET /groups/{id}/members?$select=id,displayName,userPrincipalName&$top=100
                                                # optional — team membership
GET /teams/{id}/channels/{id}/members          # optional — only for
                                                # non-"standard" (private/
                                                # shared) channels

# messages family (the metered, protected surface — paced separately,
# conservative default 4 req/s)
GET /teams/{id}/channels/{id}/messages?$top=50&$expand=replies
                                                # THE day-one gate endpoint;
                                                # 402/403 here is fatal, not
                                                # a per-channel skip
GET <replies@odata.nextLink>                   # pages a truncated replies
                                                # list to completion before
                                                # the thread line is written
GET /teams/{id}/channels/{id}/messages/{id}/hostedContents/{id}/$value
                                                # inline body images/files —
                                                # attachment OBJECTS are never
                                                # fetched (sharepoint boundary)

# probe-only checks
GET /chats?$top=1                              # confirms Chat.Read.All is
                                                # NOT granted (expected: 403)
```

`classify(status, family, required)` is the single place refusal handling
is decided (figma/zoho precedent — a table, not judgment): 429 always
sleeps (`Retry-After` honored), 401 always re-mints once, 5xx/408 always
retry, any refusal on a `required=True` call is fatal, a `messages`-family
402/403 is fatal (the day-one stall), everything else 403/404 is a recorded
skip.

Dead ends, recorded so nobody re-derives them:

```
#   Chat.Read.All is deliberately never requested — chats are out of scope
#   attachment objects (as opposed to hostedContents) are never fetched —
#     those bytes live in SharePoint, a different completion effort
#   there is no per-user export — the membership index in _meta answers
#     "what does user X have access to" without a second copy of anything
```

## Blob layout produced

```
<slug>-raw/teams-export/
├── _meta/
│   ├── .cdp-complete
│   ├── teams.jsonl              # id, displayName, description, + /teams/{id} settings
│   ├── channels.jsonl           # + team_id on every row
│   ├── users.jsonl              # tenant roster
│   ├── team-members.jsonl       # + team_id
│   ├── channel-members.jsonl    # + team_id, channel_id (private/shared only)
│   ├── name-map.json            # {"teams": {id: name}, "channels": {id: name}}
│   └── manifest.json            # verify's authority
├── progress.json
└── teams/<team_id>/<channel_id>/
    ├── .cdp-complete
    ├── .cdp-cursor.json         # {next_link, lines, bytes} — only while resuming
    ├── .cdp-skipped.json        # only if the channel was skipped
    ├── messages.jsonl           # one COMPLETE thread per line (root + replies)
    └── hosted/                  # hostedContents referenced from message bodies
        └── <message_id>_<hostedContentId>.<ext>
```

The team/channel **id** leads every unit path (display names are mutable;
ids are not — a rename between passes must never orphan a marker).

## VM-side layout & manual rescue

```
~/.config/xfer/teams.env        # 600: TEAMS_TENANT_ID / TEAMS_CLIENT_ID / TEAMS_CLIENT_SECRET
~/.config/xfer/dest-teams.env   # 600: DEST_URL (bare container) / DEST_SAS / DEST_PREFIX
~/xfer-teams/teams_vm_pull.py   # re-pushed fresh on every transfer
~/xfer-teams/pull-teams.log     # the heartbeat status tails
~/xfer-teams/dest/              # staging; the container tree mirrors this
```

```bash
ssh azureuser@<ip>
tmux ls; tmux attach -t transfer   # window name: teams
tail -f ~/xfer-teams/pull-teams.log
cat ~/xfer-teams/dest/progress.json
python3 -m json.tool ~/xfer-teams/dest/_meta/manifest.json | head -40
# exactly where a channel's message walk stopped:
cat ~/xfer-teams/dest/teams/<team_id>/<channel_id>/.cdp-cursor.json
# re-run the upload by hand:
set -a; . ~/.config/xfer/teams.env; . ~/.config/xfer/dest-teams.env; set +a
azcopy copy "$HOME/xfer-teams/dest/*" "${DEST_URL}/${DEST_PREFIX}?${DEST_SAS}" \
  --recursive --overwrite=false
```

`teams_vm_pull.py` is fully env-driven — it is launched with **zero
argv**; `--rps-messages` / `--limit-teams` ride as `export RPS_MESSAGES=…`
/ `export LIMIT_TEAMS=…` lines ahead of `python3 teams_vm_pull.py`, not CLI
flags.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `probe`/`write-creds` refuses on token mint (400/401) | bad secret (AADSTS7000215), bad app id (AADSTS700016), or bad tenant (AADSTS90002) | re-check the 3 stdin values with the client; none of these are retryable |
| `/groups refused (403)` on `probe` | admin consent was never granted for the app's application permissions | the client's Entra admin grants consent (Team.ReadBasic.All/Group.Read.All at minimum) before anything else can be probed |
| `message_gate: metered-model-required` (402) | the tenant has not linked an Azure subscription for Teams' metered API billing (model A/B) | client sets that up in the Teams admin center, then re-run probe |
| `message_gate: protected-api-approval-missing` (403) | tenant not approved for Microsoft's protected Teams messaging APIs (aka.ms/GraphTeamsProtectedApis) | client files that request; days-to-weeks — a client conversation, not a retry |
| `probe`'s `chats` field is a WARNING, not "out-of-scope" | `Chat.Read.All` is granted when it shouldn't be | confirm with the client whether 1:1/group chats are actually in scope before transfer runs — this tool never pulls them regardless |
| `write-creds` refuses with "tenant mismatch" | stdin tenant id disagrees with `--tenant-id` or the VM's `teams_tenant_id` tag | pass the matching tenant, or tear down and re-create the VM with the correct `--tenant-id`; nothing was written |
| a channel's cursor `next_link` now refuses | the channel was deleted/archived mid-run | automatic: the unit is cleared and re-walked from scratch (channels are small, unlike a CRM module) |
| `hosted_errors` > 0 in the manifest | a per-hostedContent fetch failed (not the whole messages surface, which would already be fatal) | informational; re-run transfer, existing hostedContents files are skipped by name |
| `verify: no-manifest` | the pull never finished a pass | run `status`, then `transfer` |
| `verify: short_uploads` | a partial azcopy | re-run `transfer` (no-overwrite skips what landed), verify again |
| `verify: stale_extra` | a re-walked channel wrote a shorter file, no-overwrite kept the longer old one | informational only |
| `check-azure` 403 | vnet-rule missing or propagating | re-run `allow-network`; company infra may strip rules not added through their UI |
| `skipped_units` in the manifest | archived team, deleted/private channel the app can't see | deliberate skip, never a failure — read the reason, don't over-diagnose |

## Secrets hygiene invariants (grep-testable)

- The 3 credential values transit: client secure channel → user chat →
  heredoc stdin → ssh stdin → `~/.config/xfer/teams.env` (600) on the VM →
  the puller's process environment. Never argv (ps-visible), never VM tags,
  never laptop files, never echoed (dry-run prints `secret: redacted`).
- The minted Graph access token lives in memory only (`TokenBox`, laptop
  and VM copies deliberately duplicated, the github/zoho precedent) — never
  written to disk, a tag, or a log line.
- The container SAS appears only in `dest-teams.env` and the VM's rclone
  remote section; raw azcopy output is never echoed wholesale because a URL
  line leaks `sig=`.
- `verify` takes **no Graph credentials at all** — it is a blob listing
  plus a manifest read, using only the laptop's `rl` account SAS.
- The client rotates (or deletes) the Entra app registration's client
  secret after teardown — `teardown`'s own reminder says so.
