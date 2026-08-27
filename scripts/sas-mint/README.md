# sas-mint — client push credentials

`mint_sas.py` mints a **user-delegation SAS** for one company's container and
bundles it into a password-protected zip you can send the client.

This is deliberately **not** part of the harness's stdlib-only `scripts/` layer:
it signs with the Azure SDK (`azure-identity` + `azure-storage-blob`), so it
lives in its own directory with its own venv. Nothing else in the harness
imports it.

Two roles:

| `--role`  | container   | perms   | who it's for |
|-----------|-------------|---------|---------------|
| `partner` (default) | `<deal>-raw` | `racwl` (no delete) | the selling company pushing their corpus in |
| `client`  | `<deal>-scrubbed` | `rl` | the buyer pulling the scrubbed corpus out |

## Setup (once)

```bash
python3 -m venv scripts/sas-mint/.venv
scripts/sas-mint/.venv/bin/pip install -r scripts/sas-mint/requirements.txt
```

`.venv/` is gitignored.

## RBAC (already granted fleet-wide, 2026-08-25)

Owner/Contributor are **control-plane only** — Azure separates control and data
planes for storage, so Owner alone cannot sign a delegation key and a `racwl`
request would silently mint a weaker SAS. Minting needs two explicit data-plane
roles.

These are granted to `gabriel.a@micro1.ai` at **subscription scope**
(`/subscriptions/600b01d1-…`, "m1 corpus"), so no per-company grant is needed
for any current or future company in that subscription:

```bash
SCOPE="/subscriptions/600b01d1-fe30-46df-b332-ec8bdccdd40d"
az role assignment create --assignee <your-object-id> --role "Storage Blob Delegator"        --scope "$SCOPE"
az role assignment create --assignee <your-object-id> --role "Storage Blob Data Contributor" --scope "$SCOPE"
```

Get your object id with `az ad signed-in-user show --query id -o tsv`.
Role assignments take ~1-2 minutes to propagate; a 403 immediately after
granting is propagation, not a misconfiguration.

**Do not use `Storage Blob Data Contributor` here.** Its dataActions are
`delete`, `read`, `write`, `move/action`, `add/action` — so holding it means
standing power to destroy client corpus data across every account in scope,
which cuts against CLAUDE.md principle 3. A `racwl` SAS needs only `read`,
`write` and `add/action`; `delete` is dead weight.

`corpus-blob-pusher-role.json` in this directory defines **Corpus Blob Pusher**,
a custom role with exactly those three dataActions and no delete. Create it once
and assign it at subscription scope:

```bash
az role definition create --role-definition scripts/sas-mint/corpus-blob-pusher-role.json
az role assignment create --assignee <your-object-id> --role "Corpus Blob Pusher" --scope "$SCOPE"
```

`Storage Blob Data Reader` is not a substitute — a user-delegation SAS is capped
by the signer's RBAC, so a `racwl` request from a Reader mints a silently
read-only SAS and the client's first upload 403s.

Nothing else in the harness uses these roles. Sizing (`phases.mint_sas`) and
every ingest path (`transfer_engine.mint_container_sas`) mint via **account
keys**, authorized by control-plane Contributor — so removing a blob data role
never breaks them.

To scope a grant to a single account instead, use
`$SCOPE/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<sa>`.

### Verifying the custom role

There is no dry-run for a blob write, and the harness never writes to a client
container — so do not "test" a re-minted SAS by uploading a probe blob into a
`-raw` container (with no delete permission, you could not remove it). Verify
against a throwaway storage account, or treat the client's first upload as the
test and stay reachable while they try it.

## Firewall

The corpus storage accounts are `defaultAction: Deny` with an IP allowlist.
Every call this script makes — including `get_user_delegation_key` — is
data-plane, so **your current public IP must be on the allowlist** or you get
`403 AuthorizationFailure`. Check first:

```bash
az storage account show -n <sa> -g <rg> --query "networkRuleSet.ipRules" -o table
curl -s https://api.ipify.org
```

If your IP is already there (it may be, from sizing runs), **do not touch the
rules** — the other entries are the client's own push locations and removing
one breaks their transfer. If it isn't, add it, wait ~60s for propagation, and
remove only the rule you added when you're done:

```bash
az storage account network-rule add    -g <rg> --account-name <sa> --ip-address <your-ip>
az storage account network-rule remove -g <rg> --account-name <sa> --ip-address <your-ip>
```

## Minting

```bash
scripts/sas-mint/.venv/bin/python scripts/sas-mint/mint_sas.py <storage-account> \
  --container <container> --days 7 --out companies/<slug>/<name>-sas.zip
```

Notes:

- `--container` is worth passing explicitly. Auto-discovery lists containers and
  picks the single `-raw` match, which is fine for `<slug>-raw` but the
  container name doesn't always match the harness slug (kidinme's is
  `kidinme-corporation-raw`).
- Write the zip into `companies/<slug>/` — that whole tree is gitignored, so a
  credential zip can never be committed.
- TTL caps at **7 days** (Azure's limit for user-delegation SAS). Re-mint on
  expiry; there is nothing to revoke.
- The password is a fresh `secrets.token_urlsafe(16)` printed to stdout, never
  written to disk. Send it out-of-band from the zip (zip over email, password
  over Slack).
- Zip encryption is ZipCrypto (the system `zip` CLI) so `unzip -P <pw>` works
  anywhere. It is not AES — the out-of-band split is doing the real work.

## Revocation

A user-delegation SAS is signed by your AAD identity, so it dies when the
delegation key does: removing your `Storage Blob Delegator` / data role on the
SA, or rotating your credentials, invalidates every SAS you signed there.
Otherwise it simply lapses at expiry.
