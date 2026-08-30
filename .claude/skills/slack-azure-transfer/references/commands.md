# slack-azure-transfer — command reference & troubleshooting

Everything here is what `scripts/slack_transfer.py` (engine lifecycle from
`scripts/transfer_engine.py`, VM-side puller `scripts/slack_vm_pull.py`)
runs under the hood. Reference it for debugging and manual rescue; never
bypass the script for normal operation.

## Script subcommand map

```bash
S="python3 scripts/slack_transfer.py"

$S discover-export <slug>                      # find the export in <slug>-raw
$S probe         <slug> --export-blob <blob>   # laptop-only gate: census + one live Slack GET
$S discover      <slug>                        # reconstruct the VM phase from Azure
$S plan          <slug> --export-blob <blob>   # offline; the confirm gate
$S create-vm     <slug> --export-blob <blob>   # billing starts here
$S allow-network <slug>                        # service endpoint + vnet-rule
$S write-dest    <slug>                        # racwl SAS + export location -> dest-slack.env
$S check-azure   <slug>                        # prove the VM can reach the container
$S write-creds   <slug>                        # OPTIONAL: 1 stdin line, a Slack token override
$S transfer      <slug>                        # push puller, tmux window "slack"
$S status        <slug>                        # phase + manifest + log tail
$S verify        <slug>                        # laptop-side, rl SAS, no Slack creds
$S teardown      <slug> --confirmed            # refuses without --confirmed
```

`plan`, `create-vm` and `probe` **require** `--export-blob` (a `.zip` in the
container) or `--export-prefix` (an already-extracted tree). `write-dest`
does not — it falls back to the VM's `slack_export` tag set at `create-vm`,
so the normal workflow passes the location once.

Common flags: `--root` (companies dir, default `companies/`), `--rg`
(override VM resource group), `--vm-size` (default `Standard_D8s_v7`),
`--os-disk-gb` (default 256 — the export ARCHIVE stages here; file bytes
never do), `--dest-prefix` (default `slack-export-files`), `--sas-days`
(default 21), `--dry-run` on everything.

**`--dest-prefix` resolution is NOT the same on every command.**
`write-dest` resolves flag → the VM's `dest_prefix` tag → the default.
`verify` resolves flag → the default ONLY, because the VM is normally torn
down by the time you verify. **A two-phase export run with a non-default
`--dest-prefix` must be passed that same prefix on `verify`**, or it
silently certifies the wrong path.

Transfer/probe knobs: `--renditions` / `--no-renditions` (default ON),
`--limit-files N` (pilot), `--rps-files` (copy-request pacing ceiling,
default 12/s), `--copy-workers` (default 8), `--shard-size` (objects per
resumable unit, default 2000). `probe` also takes `--sample` (day files to
census, default 400) and `--slack-token` (try a token without installing it).

Exit codes: **0** ok, **1** hard error (`common.HarnessError`, a dead export
token, timeout), **2** refusal or check-failed (unconfirmed teardown, verify
found missing objects or size mismatches, discovery found zero or several
candidates, a pass that finished with incomplete shards).

## Full run

```bash
S="python3 scripts/slack_transfer.py"
SLUG=acmeco

# 1. Where is the export?  Zero or several candidates -> ASK the user.
$S discover-export $SLUG

# 2. Does it still work?  No VM exists yet; nothing is billed.
$S probe $SLUG --export-blob 'slack/export-2026-08.zip'

# 3. Show the plan + the probe census, get explicit confirmation, then:
$S plan          $SLUG --export-blob 'slack/export-2026-08.zip'
$S create-vm     $SLUG --export-blob 'slack/export-2026-08.zip'
$S allow-network $SLUG
$S write-dest    $SLUG
$S check-azure   $SLUG

# 4. Pilot first, always.
$S transfer $SLUG --limit-files 200
$S status   $SLUG
$S verify   $SLUG

# 5. Full run, then verify, then tear down.
$S transfer $SLUG
$S status   $SLUG
$S verify   $SLUG
$S teardown $SLUG --confirmed
```

Only if `probe` reported `token-expired` or `no-file-links` **and** the
client supplied a Slack token with `files:read`:

```bash
python3 scripts/slack_transfer.py write-creds acmeco <<'EOF'
xoxp-the-clients-token
EOF
```

One line, stdin only. The token is validated (`xox` prefix, no single quote
that would corrupt the sourced env file), written to a 600 file on the VM,
and never echoed. It rides as `Authorization: Bearer` — which Azure's
`x-ms-copy-source-authorization` also speaks, so server-side copy still
works.

## What lands in the container

```
slack-export-files/
  files/<F047>/<file_id>/<name>              # originals, id-led, 4-char fan-out
  renditions/<F047>/<file_id>/<field>__<name>
  _meta/files-index.jsonl        # one row per unique file: conversation, ts, blob
  _meta/file-shares.jsonl        # every additional sighting of a shared file
  _meta/objects.jsonl            # one row per blob — verify's authority
  _meta/conversations.jsonl      # dir/name/id/kind for every conversation
  _meta/external-references.jsonl# the gdrive gap: recorded, never fetched
  _meta/unavailable.jsonl        # tombstone / hidden_by_limit, with reasons
  _meta/export-source.json       # which blob was parsed + its .slack-manifest
  _meta/manifest.json            # the run manifest
  _meta/progress.json            # heartbeat
  _meta/shards/<nnnnn>.cdp-complete
```

The ledgers deliberately store URLs with the token **stripped** (the
parameter name is kept so it can be re-attached correctly). The export
already holds that credential, but duplicating it 470k times into a new
prefix is not hygiene worth shipping — and it cut `objects.jsonl` by more
than half.

## Manual rescue on the VM

```bash
ssh azureuser@<ip>
tmux attach -t transfer          # detach with ctrl-b d
tail -f ~/xfer-slack/pull-slack.log
cat ~/xfer-slack/dest/progress.json
python3 -c "import json;print(json.load(open('$HOME/xfer-slack/dest/_meta/manifest.json'))['totals'])"
ls -la ~/xfer-slack/export.zip   # the downloaded archive
wc -l ~/xfer-slack/dest/_meta/objects.jsonl
```

`~/xfer-slack/ledger.json` is the VM's scratch resume aid — its presence is
why a re-run skips the whole export walk. Delete it to force a rebuild (do
that if you change `--renditions`, since the object set changes).

## Troubleshooting

**`discover-export` says `no-export-found`.** Read `top_level_prefixes` in
the output back to the user. Either the export has not been pushed yet (a
push conversation), or it is somewhere the signature check does not reach —
re-run with `--export-prefix <prefix>` to narrow the listing, or point
`--export-blob` straight at the archive.

**`discover-export` says `several-candidates`.** Do not pick one. A
two-phase export (public channels shipped first, private/DMs later) is the
usual cause, and those are two separate runs into two different
`--dest-prefix` values. Ask.

**`probe` says `token-expired`.** Slack answered 401/403. Export download
links expire *with the export* and cannot be refreshed. The client runs a
fresh export, or issues a `files:read` token for `write-creds`. Days of
client work — not a retry, and not something a VM would fix.

**`probe` says `files-missing`.** Every sampled file 404s: those files were
deleted from Slack after the export was produced. Re-run with a larger
`--sample` before concluding; if it holds, the export is a transcript of
files that no longer exist and the honest number is much smaller than
`declared_bytes`.

**`probe` says `no-range-support`.** The file host answered the range probe
without a 206, so files above 256 MiB cannot use Put Block From URL and fall
back to streaming through the VM. Small files are unaffected; a video-heavy
corpus will be slower than the estimate.

**The puller dies immediately.** `tail ~/xfer-slack/pull-slack.log`. Usual
causes: `dest-slack.env` never written (`write-dest`), the export blob name
in the tag not matching what is actually in the container, or the archive
not being a Slack export (no `channels.json` + `users.json` at any single
level).

**A 403 from Azure mid-run.** Vnet-rule propagation, or company
infrastructure stripping rules that were not added through the internal UI
(the saxon lesson). Re-run `allow-network`; never re-mint the SAS for a 403.

**The run ends with incomplete shards (rc 2).** Read
`failed_sample` in the manifest. Re-run `transfer` — writes are create-only,
so a re-run costs only the objects that are actually missing — then
re-verify.

**`verify` reports `size_mismatches`.** This is the check no sibling ingest
can make: the export declared N bytes and Azure committed something else.
Investigate before reporting the company complete; a mismatch on a handful
of files usually means those files changed in Slack between the export and
the pull, but a systematic one means the transport is wrong.

**`verify` reports `unexpected_present`.** Informational only: a later pass
landed a file an earlier pass had recorded as gone.

## Live smoke test (real Azure)

```bash
python3 scripts/slack_transfer.py discover-export <small-slug>
python3 scripts/slack_transfer.py probe <small-slug> --export-blob <blob> --sample 50
```

Offline validation is `python3 tests/test_harness.py`, which exercises the
ledger, the token handling, `classify()`, discovery, verify's comparison and
every subcommand under `--dry-run` against
`tests/fixtures/slack-export-mini.zip`. That fixture is synthetic —
regenerate it with `python3 tests/fixtures/make_slack_export_mini.py`.
