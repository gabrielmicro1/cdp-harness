# GitHub transfer — implementation handoff

Audience: an agent building a `github-azure-transfer` skill for this harness.
This is the mechanism-level brief for how GitHub pulls already work in the
sibling repo `cdp-platform` (the admin platform), what of it is reusable here,
and what this harness must supply itself. Written 2026-08-22 against
cdp-platform `main` @ `2707be2`; the pull script referenced below is at commit
`dc320d1` (2026-08-06). When this document and the code disagree, the code wins
— fix this file.

Source files referenced (all in
`../cdp-platform/cdp-platform-backend/`, relative to this repo):
`scripts/github_pull.py` · `app/pull/adapters/github.py` ·
`app/oauth/providers/github.py` · `app/github_app.py`

---

## 0. The headline decision

**The platform's OAuth flow does not port to this harness. Use a fine-grained
PAT.**

The platform authenticates GitHub pulls with a **GitHub App installation**,
which structurally requires three things the harness does not have and cannot
have: a public HTTPS callback URL, a registered GitHub App whose private key
lives in AWS KMS (imported, non-exportable, IAM-bound to the cdp EC2 box), and
a database to persist the `installation_id`. There is also no cdp endpoint that
hands out a raw installation token. A local operator tool cannot mint one.

A PAT is also what this harness already does for qwilr / vimeo / zoom, and —
conveniently — `github_pull.py` supports both token kinds natively (see §2.3).

**When NOT to use this skill:** if the company already installed micro1's
GitHub App through the data.micro1.ai portal, the platform pull path gives
strictly better custody (short-lived, org-scoped, auto-revoking installation
tokens, nothing long-lived stored). Reach for this harness skill when the
company isn't onboarded to that path, or when an operator needs direct control.

---

## 1. How the platform does it with OAuth (reference only)

Five legs. Recorded here so nobody re-derives it, and so the custody comparison
above is checkable.

**Leg 1 — Begin.** Portal → `GET /api/companies/:cid/sources/:sid/oauth/connect`.
cdp returns the GitHub App **install** URL with a signed `state` appended
(HS256 JWT, TTL 600s, claims `{prv, cid, sid, nonce}`). The state is the only
CSRF anchor; the pull target is deliberately not in it.

**Leg 2 — Install.** Partner installs the App on their org and selects repos.
GitHub redirects to the **data.micro1.ai** callback (never cdp) carrying
`installation_id`, `setup_action`, `code`, `state`.

**Leg 3 — Exchange.** data.micro1.ai relays server-to-server to cdp
`POST /api/oauth/exchange`. cdp verifies the state, then performs the
security-critical check — the `installation_id` in a redirect is spoofable per
GitHub's own guidance:

```python
user_token = github_app.exchange_user_code(client_id, client_secret, code)
if installation_id not in github_app.user_installation_ids(user_token):
    raise OAuthError("installation does not belong to the authorizing user", 403)
```

**Leg 4 — Store.** `ConnectOutcome(secret={}, metadata={...})` — **no secret is
stored at all**. Only `{kind: "github_app", installation_id, login, owner_type,
available_orgs, repo_count, private_repo_count}` in `metadata_json`.

**Leg 5 — Mint at pull time.** `materialize_token()` runs the App auth chain:

```
App private key ──RS256 JWT (9-min TTL, iat backdated 60s)──▶
  POST /app/installations/{id}/access_tokens ──▶
    installation token (~1h, scoped to the granted repos + permissions)
```

Signed with **AWS KMS `kms:Sign`** when `GITHUB_APP_KMS_KEY_ID` is set
(`RSASSA_PKCS1_V1_5_SHA_256` is exactly JWT `RS256`); local PEM is the dev
fallback. Tokens are cached in-process per installation and re-minted 5 minutes
before expiry.

---

## 2. What actually moves the data (this part IS reusable)

`../cdp-platform/cdp-platform-backend/scripts/github_pull.py` — 483 lines,
**Python 3.8+ stdlib only**, needs `git` on PATH (`azcopy` only when
`--upload` is used, `git-lfs` only with `--lfs`). It already matches this
harness's local-ingest CLI contract.

### 2.1 Invocation

```bash
python3 github_pull.py \
  --login acme-inc --owner-type org \
  --dest /path/to/scratch \
  --token-stdin \
  --upload 'https://ACCT.blob.core.windows.net/<slug>-raw/github-export/?<SAS>'
```

Token: one line on stdin (`--token-stdin`) or `GITHUB_TOKEN` env.
Other flags: `--limit N` (smoke test), `--only REPO`, `--skip-clone`,
`--skip-json`, `--lfs`, `--refresh` (re-fetch instead of resume).
**Exit codes: `0` complete · `1` fatal setup error · `2` finished with failures.**

### 2.2 What it pulls, per repo

1. `git clone --mirror` (code + full history), 3 retries, `.cdp-complete`
   resume marker per repo
2. Four JSONL exports, paginated 100/page: `issues`, `pulls`,
   `issue_comments`, `review_comments` (all `?state=all`)
3. `git lfs fetch --all` when `--lfs`
4. `azcopy copy <dest>/* <container_url> --recursive --overwrite=ifSourceNewer`
   when `--upload`

Output layout (on disk, and therefore in the container):

```
<dest>/repos/<name>.git/                        # mirror clones
<dest>/json/<name>/{issues,pulls,issue_comments,review_comments}.jsonl
<dest>/manifest.json    # {login, owner_type, finished_utc, repo_count,
                        #  total_clone_bytes, failed_repos, results[]}
<dest>/progress.json    # {phase, done, total, message} — rewritten per repo
```

### 2.3 PAT vs App token — already handled

`list_repos()` first tries `GET /installation/repositories` (the repos
explicitly granted at install). A PAT gets 401/403/404 there, the helper
returns `None`, and it falls back to `/orgs/{login}/repos?type=all` or
`/users/{login}/repos?type=all`. The installation path is *preferred* upstream
because `/orgs/.../repos` also returns the org's **public** repos, which the App
was never granted — over-collection. With a PAT that distinction disappears:
you get everything the PAT can see, so **scope the PAT deliberately**.

### 2.4 Token custody — copy this pattern verbatim

The token never appears in a clone URL, in argv, or in `ps`. Git authenticates
through a temporary `GIT_ASKPASS` helper (mode 0700, deleted on exit):

```sh
#!/bin/sh
case "$1" in
  Username*) echo "x-access-token" ;;
  Password*) echo "$GITHUB_TOKEN" ;;
esac
```

The token is passed to git only via that process's environment. Clone URLs stay
clean (`repo["clone_url"]`, no embedded credential).

### 2.5 Failure semantics worth preserving

- `whoami_check()` fails fast before any heavy work: `GET /rate_limit`
  (validates the token) then `GET /orgs/{login}` — a wrong login/owner_type or
  a fine-grained PAT the org never approved dies immediately.
- **403 with the rate limit intact is a FATAL scope error**, never retried —
  "likely missing Contents/Issues/Pull requests: read".
- **401 is fatal** — token invalid or expired.
- 403/429 with `Retry-After`, or exhausted `X-RateLimit-Remaining` + `reset`
  → sleep until reset (capped at 3900s), up to 4 attempts.
- 5xx → exponential backoff.
- **404 on a JSON endpoint is not a failure** — the feature is disabled on that
  repo (e.g. issues off). Skipped and logged.
- git auth failure whose stderr contains `403` / `Authentication failed` →
  fatal, with the "PAT lacks Contents: read" diagnosis.
- **Upload is skipped entirely if any repo failed.** Fix and re-run; the
  resume markers skip completed repos.

---

## 3. What this harness must supply

`github_pull.py` covers the GitHub side completely. Missing is the Azure-side
scaffolding, all of which already exists here.

| Need | Harness pattern to reuse |
|---|---|
| Credential | Fine-grained PAT, **stdin only, never written to disk** (`qwilr_transfer.py` pattern). Permissions: Contents, Issues, Pull requests = **read**. Org-owned repos require the org to *approve* the fine-grained PAT — a real stall, gate on it. |
| Storage firewall | `phases.ip_rule_ensure` — add the laptop IP, ~60s propagation, remove at cleanup (only rules we added this run). Laptop path, not the VM/vnet path. |
| SAS | `phases.mint_sas`, `racwl`, 1–2 day expiry, passed via `AZURE_STORAGE_SAS` process env, never on disk. |
| Destination prefix | Platform writes to `<slug>-raw/github/`; this harness's siblings use `<source>-export/`. **Recommend `github-export/`** for consistency, and pin `"prefix": "github-export"` in `expected-data-sizes.json` (the manifest service will be declared as "github" and won't name-match otherwise). |
| Local vs VM | Repo corpora are usually tens of GB, not TB → **run locally**, like qwilr/vimeo/zoom. Only reach for `transfer_engine.py`'s VM if a corpus is huge or the operator's uplink is the bottleneck. |
| Skill shape | Mirror `qwilr-azure-transfer`: `plan` → **PAUSE for the PAT** → `pull` (background; long-running) → `verify`. Consider a `probe` gate like vimeo/zoom/s3: `whoami_check` + repo enumeration + summed `diskUsage` + LFS check, so a scope/approval problem surfaces on day one instead of mid-run. |
| Verify | Byte-compare against `manifest.json`'s `total_clone_bytes`; surface `failed_repos`; remember **LFS is separate** (Org → Billing → Git LFS shows usage) and is only fetched with `--lfs`. |
| Sizing note | `gh repo list ORG --limit 999 --json name,diskUsage` summed is the declared-size cross-check (this is what the platform catalog's `sizeTip` recommends). |

### Two design calls to make explicitly

1. **Fork vs vendor.** Copying `github_pull.py` into
   `scripts/github_transfer.py` fits this harness (stdlib-only, no venv), but
   forks a file owned by cdp-platform. If you copy it, put a header comment
   naming the upstream path **and commit `dc320d1`** so drift stays traceable.
2. **Write invariant.** `--upload` uses `--overwrite=ifSourceNewer`, weaker than
   this harness's usual create-only (`If-None-Match: *`) rule. Either document
   the exception or switch to `--overwrite=false` (the choice `azcopy-runner.sh`
   makes on the S3 path). Do not claim create-only semantics without changing
   the flag.

---

## 4. Client-facing steps (for the skill's PAUSE step)

The client generates a **fine-grained personal access token**:
Settings → Developer settings → Personal access tokens → Fine-grained tokens.

- **Resource owner**: the organization (not their personal account) — this is
  what triggers the org-approval requirement.
- **Repository access**: all repos, or the specific set in scope.
- **Repository permissions**: Contents = read, Issues = read,
  Pull requests = read. Metadata read is implied.
- An **org owner must approve** the token if the org requires approval for
  fine-grained PATs. Until then every call 403s — `whoami_check` catches this
  immediately with a clear message.
- Ask whether **Git LFS** is in use (Org → Billing → Git LFS); if so the run
  needs `--lfs` and the size estimate must include it.
- Set an expiry that covers the engagement, and have them **revoke it** after
  verify.

Out of scope, worth stating up front: GitHub Actions logs/artifacts, Packages,
Projects, Discussions, wikis (separate `.wiki.git` repos), and releases'
binary assets are not pulled by this script.
