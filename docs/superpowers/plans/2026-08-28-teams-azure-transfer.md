# teams-azure-transfer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `teams-azure-transfer` ingest engine that pulls Microsoft Teams
channel messages (replies fully expanded), hosted content, and
team/channel/membership metadata into `<slug>-raw/teams-export/` — one copy
per team, VM-side puller on the transfer-engine lifecycle.

**Architecture:** `scripts/teams_transfer.py` is a laptop CLI over
`transfer_engine.py` (create/allow-network/teardown reused verbatim, figma's
Spec shape: no rclone source, secrets on stdin). It pushes
`scripts/teams_vm_pull.py` to VM `xfer-teams-<slug>`, which walks Microsoft
Graph in tmux, stages under `~/xfer-teams/dest/`, and azcopies per unit.
Verify runs on the laptop against the uploaded manifest.

**Tech Stack:** python3 stdlib only (both scripts), Microsoft Graph v1.0
app-only (client credentials), azcopy on the VM, `az` CLI via
`scripts/common.py`.

**Spec:** `docs/superpowers/specs/2026-08-28-teams-azure-transfer-design.md`

## Global Constraints

- stdlib-only python3, both scripts (test_harness AST-checks imports).
- Secrets: exactly 3 stdin lines (tenant id, client id, client secret) →
  ssh stdin → 600 env file on the VM. Never argv, tags, logs, files on the
  laptop. Client secret never echoed (test uses a sentinel).
- Tenant id is NOT secret: it rides the `teams_tenant_id` VM tag
  (`loc_tag`), and write-creds cross-checks stdin vs tag (zoho's DC guard).
- Dest layout is guid-led: `teams-export/_meta/…` +
  `teams-export/teams/<team-guid>/<channel-id>/…`. Display names only in
  `_meta/name-map.json`.
- Uploads: units as they complete, azcopy `--overwrite=false`;
  `manifest.json` `--overwrite=true`. Never `If-None-Match` and never
  `x-ms-copy-source-authorization` on this engine (test-checked).
- `_meta` unit failures are fatal; per-team/per-channel failures are
  recorded skips (`.cdp-skipped.json`).
- `--dry-run` on every Azure-touching subcommand prints az commands.
- All timestamps ISO-8601 UTC; run `python3 tests/test_harness.py` for the
  full offline suite (dry-run + source-invariant checks; engines have no
  pytest — checks use the harness's `check()`/`run_script()` helpers).
- Commit after each task; never commit `companies/`.

**Read before starting:** `scripts/figma_transfer.py` +
`scripts/figma_vm_pull.py` end to end (the closest shape), the figma test
block in `tests/test_harness.py` (search `— figma_transfer --dry-run`), and
the spec.

---

### Task 1: `teams_vm_pull.py` — pure helpers, TokenBox, classify, pacing

**Files:**
- Create: `scripts/teams_vm_pull.py` (helpers + classes only; `main()` comes in Task 3)
- Modify: `tests/test_harness.py` (new checks at the end of the figma block)

**Interfaces:**
- Produces (used by Tasks 2–5):
  - `class TokenBox(tenant_id, client_id, client_secret)` with
    `.get() -> str`, `.mint() -> str`, `.invalidate()`, `.mints: int`
  - `classify(status: int, family: str, required: bool) -> str` returning
    one of `"ok" | "fatal" | "skip" | "retry" | "sleep" | "remint"`
  - `class PaceBucket(rps: float)` with `.wait()` (monotonic-clock pacing)
  - `safe_component(name: str, limit: int = 120) -> str` (copy figma's)
  - `resume_truncate(jsonl: Path, cursor: dict) -> int` (copy figma's)
  - marker helpers `unit_dir/is_complete/mark_complete/mark_skipped/read_cursor/clear_unit`
    (copy figma's, identical signatures)
  - module constants `GRAPH = "https://graph.microsoft.com/v1.0"`,
    `LOGIN = "https://login.microsoftonline.com"`,
    `TOKEN_REFRESH_MARGIN = 120`, `API_RETRIES = 4`

- [ ] **Step 1: Write the failing checks.** Append to `tests/test_harness.py`
  directly after the figma block (mirror its style):

```python
    print("\n— teams_vm_pull pure helpers (classify, TokenBox shape, "
          "pacing)")
    import teams_vm_pull  # noqa: E402
    check("teams classify: 403 on the messages family is the "
          "protected-API fatal",
          teams_vm_pull.classify(403, "messages", False) == "fatal"
          and teams_vm_pull.classify(402, "messages", False) == "fatal")
    check("teams classify: 403/404 on a single team unit is a recorded "
          "skip, never fatal",
          teams_vm_pull.classify(403, "team", False) == "skip"
          and teams_vm_pull.classify(404, "channel", False) == "skip")
    check("teams classify: _meta required units are fatal on any refusal",
          teams_vm_pull.classify(404, "directory", True) == "fatal")
    check("teams classify: 429 sleeps, 401 re-mints, 5xx retries",
          teams_vm_pull.classify(429, "messages", True) == "sleep"
          and teams_vm_pull.classify(401, "directory", True) == "remint"
          and teams_vm_pull.classify(503, "messages", False) == "retry")
    check("teams TokenBox: client-credentials mint against the tenant's "
          "v2.0 endpoint, .default scope",
          "oauth2/v2.0/token" in teams_vm_pull.TOKEN_PATH_FMT
          and ".default" in
          inspect.getsource(teams_vm_pull.TokenBox.mint))
```

  (Add `import inspect` at the top of test_harness.py if absent.)

- [ ] **Step 2: Run to verify failure.**
  Run: `python3 tests/test_harness.py`
  Expected: `ModuleNotFoundError: teams_vm_pull` (or check failures).

- [ ] **Step 3: Implement.** Create `scripts/teams_vm_pull.py` with a module
  docstring (mirror figma_vm_pull.py's: what it pulls, unit model, resume,
  honesty rules). Copy VERBATIM from `scripts/figma_vm_pull.py` (same
  names, same signatures — the deliberate engine duplication):
  `log`, `human_bytes`, `write_progress`, `dir_size`, `atomic_write_json`,
  `safe_component`, `resume_truncate`, `_count_lines`, `unit_dir`,
  `is_complete`, `mark_complete`, `mark_skipped`, `read_cursor`,
  `clear_unit`, `_result`. Then the teams-specific pieces:

```python
GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
TOKEN_PATH_FMT = "{login}/{tenant}/oauth2/v2.0/token"
TOKEN_REFRESH_MARGIN = 120   # re-mint 2 min before the 1 h expiry
API_RETRIES = 4
DEFAULT_RPS_MESSAGES = 4.0   # conservative; Teams messaging is Graph's slow lane
DEFAULT_RPS_DIRECTORY = 10.0 # groups/users/channels reads


class TokenBox:
    """App-only AAD client-credentials token, auto-refreshed. VM twin of
    teams_transfer.py's TokenBox — deliberately duplicated, the
    github/zoho precedent (this file is pushed to the VM alone; it raises
    SystemExit where the laptop raises HarnessError)."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self._tenant = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._value = None
        self._exp = 0.0
        self.mints = 0

    def get(self) -> str:
        if self._value and time.time() < self._exp - TOKEN_REFRESH_MARGIN:
            return self._value
        return self.mint()

    def invalidate(self) -> None:
        self._value = None

    def mint(self) -> str:
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }).encode()
        url = TOKEN_PATH_FMT.format(login=LOGIN, tenant=self._tenant)
        last = None
        for attempt in range(1, API_RETRIES + 2):
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type":
                         "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    body = json.loads(r.read().decode())
                tok = body.get("access_token")
                if not tok:
                    raise SystemExit(f"token mint returned no access_token:"
                                     f" {str(body)[:300]}")
                self._value = tok
                self._exp = time.time() + int(body.get("expires_in", 3599))
                self.mints += 1
                return tok
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", "replace")[:300]
                if e.code in (400, 401):
                    # AADSTS7000215 bad secret / AADSTS700016 bad app /
                    # AADSTS90002 bad tenant — never retryable
                    raise SystemExit(
                        f"token mint refused ({e.code}): {err} — check the "
                        "3 stdin values (tenant, client id, secret)")
                last = f"{e.code}: {err}"
            except (urllib.error.URLError, TimeoutError) as e:
                last = str(e)
            time.sleep(min(60, 5 * attempt))
        raise SystemExit(f"token mint failed after retries: {last}")


def classify(status: int, family: str, required: bool) -> str:
    """Status + endpoint family, figma's rule (Graph error bodies are
    secondary evidence). Families: 'directory' (_meta reads: groups,
    users, teams, channels, members), 'messages' (channel messages,
    replies, hostedContents), 'team' / 'channel' (a single optional
    unit's refusal)."""
    if status in (200, 201, 204):
        return "ok"
    if status == 429:
        return "sleep"
    if status == 401:
        return "remint"
    if status >= 500 or status in (408,):
        return "retry"
    if required:
        return "fatal"          # _meta is the one required unit
    if family == "messages" and status in (402, 403):
        return "fatal"          # the protected-API / metered day-one stall
    if status in (403, 404):
        return "skip"           # archived team, deleted channel, quirk
    return "retry"


class PaceBucket:
    """Proactive pacing with the 429 Retry-After backstop handled by the
    caller. One bucket per family; monotonic clock."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._next = 0.0

    def wait(self) -> None:
        if not self._interval:
            return
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(now, self._next) + self._interval
```

- [ ] **Step 4: Run to verify pass.**
  Run: `python3 tests/test_harness.py`
  Expected: all checks pass, including the new teams block.

- [ ] **Step 5: Commit.**
```bash
git add scripts/teams_vm_pull.py tests/test_harness.py
git commit -m "teams ingest: VM puller skeleton — TokenBox, classify, pacing, markers"
```

---

### Task 2: `teams_vm_pull.py` — Graph client and the `_meta` unit

**Files:**
- Modify: `scripts/teams_vm_pull.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: Task 1's `TokenBox`, `classify`, `PaceBucket`, marker helpers.
- Produces:
  - `class GraphAPI(box: TokenBox, rps_messages: float, rps_directory: float)`
    with `.get(path: str, family: str, params: dict | None = None,
    required: bool = False) -> tuple[int, dict|bytes|None]` — follows
    retry/sleep/remint per `classify`; returns terminal (status, parsed
    body) and never raises for "skip" statuses; raises `SystemExit` on
    "fatal". `.get_raw(url: str, family: str) -> tuple[int, bytes, str]`
    (absolute-URL variant returning content-type, for `@odata.nextLink`
    and hostedContents `$value`).
  - `paged(api: GraphAPI, path: str, family: str, params: dict,
    required: bool = False)` — generator over items across
    `@odata.nextLink` pages.
  - `pull_meta(api: GraphAPI, dest: Path) -> dict` — writes the `_meta`
    unit files, returns `{"teams": [...team dicts...], "channels":
    {team_id: [...channel dicts...]}, "counts": {...}}`.

- [ ] **Step 1: Write the failing checks** (append to the teams block):

```python
    check("teams GraphAPI: exactly ONE place builds the Authorization "
          "header; bearer only",
          inspect.getsource(teams_vm_pull).count('"Authorization"') == 1)
    check("teams _meta: the documented org-wide team filter, and "
          "channel/member walks",
          "resourceProvisioningOptions/Any(x:x eq 'Team')"
          in inspect.getsource(teams_vm_pull.pull_meta)
          and "/channels" in inspect.getsource(teams_vm_pull.pull_meta)
          and "/members" in inspect.getsource(teams_vm_pull.pull_meta))
    check("teams _meta filenames match the spec layout",
          all(n in inspect.getsource(teams_vm_pull.pull_meta) for n in
              ("teams.jsonl", "channels.jsonl", "users.jsonl",
               "team-members.jsonl", "channel-members.jsonl",
               "name-map.json")))
```

- [ ] **Step 2: Run to verify failure.**
  Run: `python3 tests/test_harness.py` — Expected: AttributeError/check fail.

- [ ] **Step 3: Implement.**

```python
class GraphAPI:
    def __init__(self, box, rps_messages=DEFAULT_RPS_MESSAGES,
                 rps_directory=DEFAULT_RPS_DIRECTORY):
        self.box = box
        self._buckets = {"messages": PaceBucket(rps_messages)}
        self._dir_bucket = PaceBucket(rps_directory)
        self.calls = 0
        self.sleeps = 0

    def _bucket(self, family):
        return self._buckets.get(family, self._dir_bucket)

    def get_raw(self, url: str, family: str, required: bool = False):
        for attempt in range(1, API_RETRIES + 2):
            self._bucket(family).wait()
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.box.get()}"})
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return (r.status, r.read(),
                            r.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
            except (urllib.error.URLError, TimeoutError):
                status, body = 599, b""
            action = classify(status, family, required)
            if action == "sleep":
                retry_after = 30
                try:
                    retry_after = int(e.headers.get("Retry-After", "30"))
                except Exception:
                    pass
                self.sleeps += 1
                time.sleep(min(300, max(1, retry_after)))
            elif action == "remint":
                self.box.invalidate()
                if attempt > 1:
                    raise SystemExit("401 persists after re-mint — token "
                                     "or permission problem")
            elif action == "retry":
                time.sleep(min(120, 10 * attempt))
            elif action == "fatal":
                raise SystemExit(
                    f"fatal {status} on {family} {url.split('?')[0]}: "
                    f"{body[:300].decode('utf-8', 'replace')}")
            else:            # skip — terminal, caller records it
                return (status, body, "")
        raise SystemExit(f"retries exhausted on {url.split('?')[0]}")

    def get(self, path, family, params=None, required=False):
        url = GRAPH + path
        if params:
            url += "?" + urllib.parse.urlencode(params, safe="$()/'! ,=")
        status, body, _ = self.get_raw(url, family, required)
        if status in (200, 201) and body:
            return status, json.loads(body.decode())
        return status, None


def paged(api, path, family, params, required=False):
    status, body = api.get(path, family, params, required)
    while True:
        if status not in (200, 201) or body is None:
            return
        for item in body.get("value", []):
            yield item
        nxt = body.get("@odata.nextLink")
        if not nxt:
            return
        status, raw, _ = api.get_raw(nxt, family, required)
        body = json.loads(raw.decode()) if status == 200 and raw else None
```

  `pull_meta(api, dest)` — the one REQUIRED unit (`required=True`
  everywhere; any refusal is fatal by classify). Into
  `dest / "_meta"` (a unit dir with markers; skip whole unit if
  `is_complete`):
  1. `teams.jsonl`: iterate
     `paged(api, "/groups", "directory", {"$filter": "resourceProvisioningOptions/Any(x:x eq 'Team')", "$select": "id,displayName,description,createdDateTime,visibility", "$top": "100"}, required=True)`;
     for each, fetch `api.get(f"/teams/{gid}", "directory", required=False)`
     (archived/broken teams: record `{"id": gid, "team_settings_status": s}`
     instead of failing) and write one merged JSON line.
  2. `channels.jsonl`: per team,
     `paged(api, f"/teams/{gid}/channels", "directory", {}, required=True)`
     — each line gets `"team_id": gid` added. Collect
     `channels[gid] = [...]` for the return value.
  3. `users.jsonl`: `paged(api, "/users", "directory", {"$select": "id,userPrincipalName,displayName,mail,accountEnabled,userType", "$top": "100"}, required=True)`.
  4. `team-members.jsonl`: per team,
     `paged(api, f"/groups/{gid}/members", "directory", {"$select": "id,displayName,userPrincipalName", "$top": "100"})`
     with `"team_id"` added; a 403/404 on one team appends a
     `{"team_id": gid, "members_status": s}` line (skip, not fatal).
  5. `channel-members.jsonl`: only for channels whose
     `membershipType != "standard"`:
     `paged(api, f"/teams/{gid}/channels/{cid}/members", "directory", {})`
     with `"team_id"`/`"channel_id"` added; per-channel refusals recorded
     the same way.
  6. `name-map.json` via `atomic_write_json`: `{"teams": {gid:
     displayName}, "channels": {cid: displayName}}`.
  7. `mark_complete`, return the roster dict.

- [ ] **Step 4: Run to verify pass.** `python3 tests/test_harness.py`

- [ ] **Step 5: Commit.**
```bash
git add scripts/teams_vm_pull.py tests/test_harness.py
git commit -m "teams ingest: Graph client + _meta unit (roster, channels, membership index)"
```

---

### Task 3: `teams_vm_pull.py` — channel message units, hosted content, upload, main

**Files:**
- Modify: `scripts/teams_vm_pull.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `complete_thread(api, gid, cid, msg) -> dict` — pure-ish assembler:
    if `msg` carries `replies@odata.nextLink`, pages
    `/teams/{gid}/channels/{cid}/messages/{mid}/replies` (family
    `"messages"`) until done; returns the message with `replies` a
    complete inline list. A JSONL line is ALWAYS a complete thread.
  - `hosted_refs(msg) -> list[tuple[str, str]]` — PURE: scans
    `body.content` of the message and every reply for
    `…/messages/<mid>/hostedContents/<hcid>/$value` URLs (regex
    `HOSTED_RE`), returns unique `(mid, hcid)` pairs.
  - `pull_channel(api, gid, cid, dest, args) -> dict` — one unit:
    `teams/<gid>/<cid>/messages.jsonl` + `hosted/` dir, cursor resume.
  - `build_manifest(...)`, `upload(...)`, `upload_run_metadata(...)` —
    copied from figma_vm_pull.py (same signatures, `figma`→`teams`
    naming, same azcopy flags including the
    `.cdp-complete;.cdp-cursor.json;.cdp-skipped.json` exclude and
    `--overwrite=true` only for run metadata).
  - `main() -> int` — env-driven entrypoint (below).

- [ ] **Step 1: Write the failing checks:**

```python
    tsrc = (SCRIPTS / "teams_vm_pull.py").read_text()
    check("teams puller: markers, cursor, honest write invariant "
          "(client-side no-overwrite, no If-None-Match, no "
          "copy-source-auth)",
          ".cdp-complete" in tsrc and ".cdp-cursor.json" in tsrc
          and "--overwrite=false" in tsrc
          and '"If-None-Match"' not in tsrc
          and "x-ms-copy-source-authorization" not in tsrc)
    check("teams puller: manifest replaces, corpus never does (the "
          "pilot-poisons-verify fix ships from day one)",
          "--overwrite=true" in tsrc and "upload_run_metadata" in tsrc)
    check("teams puller: messages are pulled $top=50 with replies "
          "expanded, and truncated reply lists are paginated to "
          "completion before the line is written",
          "$expand" in tsrc and "replies@odata.nextLink" in tsrc)
    check("teams puller: hosted content is staged (Bearer fetch), "
          "attachment bytes are NOT fetched",
          "hostedContents" in tsrc and "attachment" in tsrc.lower()
          and "contentUrl" not in tsrc)
    check("teams puller: hosted_refs finds refs in root AND replies",
          teams_vm_pull.hosted_refs({
              "id": "1", "body": {"content":
                  '<img src="https://graph.microsoft.com/v1.0/teams/t/'
                  'channels/c/messages/1/hostedContents/AAA/$value">'},
              "replies": [{"id": "2", "body": {"content":
                  '<img src="https://graph.microsoft.com/v1.0/teams/t/'
                  'channels/c/messages/2/hostedContents/BBB/$value">'}}],
          }) == [("1", "AAA"), ("2", "BBB")])
    check("teams puller: secrets via env only, never argv",
          "TEAMS_CLIENT_SECRET" in tsrc and "--secret" not in tsrc)
    _tmods = set()
    for _n in ast.walk(ast.parse(tsrc)):
        if isinstance(_n, ast.Import):
            for _a in _n.names:
                _tmods.add((_a.asname or _a.name).split(".")[0])
        elif isinstance(_n, ast.ImportFrom) and _n.level == 0:
            _tmods.add(_n.module.split(".")[0])
    check("teams puller: stdlib-only imports", all(
        m in sys.stdlib_module_names for m in _tmods), str(_tmods))
```

- [ ] **Step 2: Run to verify failure.** `python3 tests/test_harness.py`

- [ ] **Step 3: Implement.**

```python
HOSTED_RE = re.compile(
    r"https://graph\.microsoft\.com/v1\.0/teams/[^\"'\s]+"
    r"/messages/([^/\"'\s]+)/hostedContents/([^/\"'\s]+)/\$value")


def hosted_refs(msg: dict) -> list:
    """PURE. (message-id, hostedContent-id) pairs referenced by this
    thread's HTML bodies — root and replies. Attachment objects are left
    as references (document-library bytes belong to the sharepoint
    completion), so only hostedContents URLs are harvested."""
    out, seen = [], set()
    def scan(m):
        content = ((m.get("body") or {}).get("content")) or ""
        for mid, hcid in HOSTED_RE.findall(content):
            if (mid, hcid) not in seen:
                seen.add((mid, hcid))
                out.append((mid, hcid))
    scan(msg)
    for rep in msg.get("replies") or []:
        scan(rep)
    return out


def complete_thread(api, gid, cid, msg) -> dict:
    nxt = msg.pop("replies@odata.nextLink", None)
    if nxt:
        replies = list(msg.get("replies") or [])
        while nxt:
            status, raw, _ = api.get_raw(nxt, "messages")
            if status != 200 or not raw:
                msg["replies_truncated"] = f"status {status}"
                break
            body = json.loads(raw.decode())
            replies.extend(body.get("value", []))
            nxt = body.get("@odata.nextLink")
        msg["replies"] = replies
    return msg
```

  `pull_channel(api, gid, cid, dest, args)`:
  - `d = unit_dir(dest, f"teams/{gid}/{cid}")`; return early if
    `is_complete(d)`.
  - Cursor: `read_cursor(d)` → `{"next_link": ..., "lines": N}`;
    `resume_truncate` the JSONL to `lines`, continue from `next_link`; a
    cursor `next_link` that answers 4xx clears the unit (`clear_unit`) and
    re-walks — channels are small.
  - Page loop: start
    `api.get(f"/teams/{gid}/channels/{cid}/messages", "messages", {"$top": "50", "$expand": "replies"})`
    then follow `@odata.nextLink` via `get_raw`. A terminal skip status on
    the FIRST page → `mark_skipped(d, f"status-{status}")` and return the
    skip result. Per page: `complete_thread` each message, write JSONL
    lines (`json.dumps(msg, separators=(",", ":"))`), then write the
    cursor (`atomic_write_json(d / ".cdp-cursor.json", {"next_link": nxt,
    "lines": total})`).
  - Hosted content: after the message walk, re-read the JSONL, for each
    line's `hosted_refs` fetch
    `api.get_raw(f"{GRAPH}/teams/{gid}/channels/{cid}/messages/{mid}/hostedContents/{hcid}/$value", "messages")`
    → `hosted/{mid}_{safe_component(hcid, 60)}{fill_ext(ctype)}` (copy
    figma's `fill_ext`; skip files that already exist — deterministic
    names ARE the resume). Per-item refusals are counted, not fatal.
  - `mark_complete(d)`; return
    `_result(f"teams/{gid}/{cid}", "channel", "ok", messages=n_root,
    replies=n_replies, hosted=n_hosted, bytes=dir_size(d))`.

  `main()`: reads env — `TEAMS_TENANT_ID`, `TEAMS_CLIENT_ID`,
  `TEAMS_CLIENT_SECRET`, `DEST_URL`, `DEST_SAS`, `DEST_PREFIX`
  (default `teams-export`), optional `RPS_MESSAGES`, `RPS_DIRECTORY`,
  `LIMIT_TEAMS` (pilot). Flow: `pull_meta` → upload `_meta` → for each
  team, for each channel: `pull_channel` → upload that team's subtree as
  it completes (figma's `upload(dest, dest_url, sas, subpath=...)`) →
  `build_manifest` (per-unit results, counts, `total_staged_bytes`,
  `api.calls`, `api.sleeps`, started/finished timestamps) →
  `upload_run_metadata`. Exit 0 all-ok, 2 completed-with-skips (the rc-2
  zoho convention), nonzero on fatal. Progress via `write_progress` and
  `log` throughout.

- [ ] **Step 4: Run to verify pass.** `python3 tests/test_harness.py`

- [ ] **Step 5: Commit.**
```bash
git add scripts/teams_vm_pull.py tests/test_harness.py
git commit -m "teams ingest: channel message units — complete threads, hosted content, per-unit upload"
```

---

### Task 4: `teams_transfer.py` — Spec, creds, probe

**Files:**
- Create: `scripts/teams_transfer.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: `transfer_engine as eng` (`Spec`, `run_ssh`, `load_cfg`,
  `require_vm`, `mint_container_sas`, `cmd_*` engine functions, `main`),
  `teams_vm_pull as puller` (import-safe helpers: `TokenBox` shape is
  duplicated, not imported — laptop version raises `common.HarnessError`).
- Produces (for Task 5 and the skill):
  - `SPEC` (below), `read_secrets(dry_run: bool) -> tuple[str, str, str]`,
  - `cmd_write_creds(root, args) -> dict`, `cmd_probe(root, args) -> dict`.

- [ ] **Step 1: Write the failing checks:**

```python
    print("\n— teams_transfer --dry-run (engine lifecycle, VM-side REST "
          "puller)")
    import teams_transfer  # noqa: E402
    check("teams Spec: stdin secrets (no OAuth/rclone), engine-default "
          "disk with 64 GB headroom",
          teams_transfer.SPEC.vm_prefix == "xfer-teams-"
          and teams_transfer.SPEC.authorize_target == ""
          and teams_transfer.SPEC.remote_type == ""
          and teams_transfer.SPEC.default_dest_prefix == "teams-export"
          and teams_transfer.SPEC.default_os_disk_gb == 64)
    proc = run_script("teams_transfer.py", "plan", "democo",
                      "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
                      "--root", root, "--dry-run")
    tplan = json.loads(proc.stdout)
    check("teams plan: dest + tenant as the loc",
          tplan["vm_name"] == "xfer-teams-democo"
          and tplan["dest"] == "democo-raw/teams-export"
          and "505f352c" in tplan["source"])
    proc = run_script("teams_transfer.py", "plan", "democo",
                      "--tenant-id", "not-a-guid", "--root", root,
                      "--dry-run", expect_rc=1)
    check("teams tenant-id validation: refuses a non-GUID before any az "
          "call", "GUID" in proc.stdout and "az " not in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "write-creds",
         "democo", "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
         "--root", str(root), "--dry-run"],
        input="505f352c-ec82-4ff9-9191-556112b420f9\n"
              "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1\n"
              "TEAMSSECRETSENTINEL\n",
        capture_output=True, text=True)
    check("teams write-creds: secret sentinel never echoed; 3-line stdin",
          proc.returncode == 0
          and "TEAMSSECRETSENTINEL" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "write-creds",
         "democo", "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
         "--root", str(root), "--dry-run"],
        input="only-two\nlines\n", capture_output=True, text=True)
    check("teams write-creds: refuses malformed stdin (must be 3 lines)",
          proc.returncode == 1 and "3 lines" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "write-creds",
         "democo", "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
         "--root", str(root), "--dry-run"],
        input="deadbeef-0000-0000-0000-000000000000\n"
              "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1\nsecret\n",
        capture_output=True, text=True)
    check("teams write-creds: stdin tenant must match the VM tag / flag "
          "(zoho's wrong-DC guard)",
          proc.returncode == 1 and "tenant" in proc.stdout.lower()
          and "mismatch" in proc.stdout.lower())
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "probe",
         "democo", "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
         "--root", str(root), "--dry-run"],
        input="505f352c-ec82-4ff9-9191-556112b420f9\n"
              "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1\n"
              "TEAMSSECRETSENTINEL\n",
        capture_output=True, text=True)
    check("teams probe: laptop-side Graph JSON only — no Azure, no VM, "
          "secret redacted",
          proc.returncode == 0
          and "graph.microsoft.com/v1.0/groups" in proc.stdout
          and "TEAMSSECRETSENTINEL" not in proc.stdout
          and "generate-sas" not in proc.stdout
          and "az vm" not in proc.stdout, proc.stdout[-300:])
```

- [ ] **Step 2: Run to verify failure.** `python3 tests/test_harness.py`

- [ ] **Step 3: Implement.** Module docstring (the engine story: what it
  pulls, the protected-API day-one stall, the sharepoint boundary,
  laptop verify). Then, mirroring figma_transfer.py's layout exactly:

```python
SPEC = eng.Spec(
    source_name="teams",
    vm_prefix="xfer-teams-",
    purpose="teams-transfer",
    loc_tag="teams_tenant_id",
    loc_argname="tenant_id",
    loc_required=True,
    default_dest_prefix="teams-export",
    authorize_target="",   # no rclone OAuth — 3 secrets on stdin
    remote_type="",        # no rclone source; Graph REST is the source
    extra_cli_opts=[],
    default_os_disk_gb=64,  # staging is JSONL + inline images: GBs, not TBs
)

PULLER_PY = Path(__file__).resolve().parent / "teams_vm_pull.py"
XFER_DIR = f"/home/{eng.ADMIN_USER}/xfer-teams"
DEST_DIR = f"{XFER_DIR}/dest"
LOG_FILE = f"{XFER_DIR}/pull-teams.log"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/xfer"
TEAMS_ENV = f"{ENV_DIR}/teams.env"
DEST_ENV = f"{ENV_DIR}/dest-teams.env"
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def validate_tenant_id(raw: str) -> str:
    t = (raw or "").strip()
    if not GUID_RE.match(t):
        raise common.HarnessError(
            f"tenant id {t!r} is not a GUID — copy the Directory (tenant) "
            "ID from the client's Entra app registration page")
    return t.lower()


def read_secrets(dry_run: bool) -> tuple[str, str, str]:
    """Exactly 3 stdin lines: tenant id, client id, client secret (the
    zoom 3-line convention). Stdin only — argv is world-readable via ps,
    env leaks into children, files persist."""
    data = "" if sys.stdin.isatty() else sys.stdin.read()
    lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
    if len(lines) == 3:
        if any("'" in ln for ln in lines):
            raise common.HarnessError(
                "a credential contains a single quote — it would break "
                "the VM env file; nothing was written")
        return lines[0], lines[1], lines[2]
    if dry_run and not lines:
        return "<tenant>", "<client-id>", "<secret>"
    raise common.HarnessError(
        "stdin must be exactly 3 lines: tenant id, client id, client "
        "secret — pipe them: write-creds <slug> <<'EOF' ... EOF")
```

  `cmd_write_creds(root, args)`: `read_secrets`; `validate_tenant_id` on
  line 1; resolve the expected tenant from `--tenant-id` or the VM's
  `teams_tenant_id` tag (`eng.require_vm` unless `--dry-run` with no VM)
  and **refuse on mismatch** (message contains "tenant" and "mismatch" —
  zoho's wrong-DC guard: a creds/tag disagreement means someone is about
  to pull the wrong tenant). Then `_write_env(ip, TEAMS_ENV, ...)` (copy
  figma's `_write_env`) with
  `TEAMS_TENANT_ID=…\nTEAMS_CLIENT_ID=…\nTEAMS_CLIENT_SECRET='…'\n`.
  Output JSON says `{"ok": true, "secret": "redacted"}` — never the
  values. Laptop `TokenBox` (duplicate of the VM one, raising
  `common.HarnessError`) does a mint as a smoke test when not dry-run.

  `cmd_probe(root, args)`: laptop-only (NO Azure calls — the test greps
  for their absence). `read_secrets` → `TokenBox.mint()` → then, against
  `https://graph.microsoft.com/v1.0` with a small figma-style
  `graph_get(token, path, params)` helper (single transport seam
  `_http`):
  1. `/groups?$filter=resourceProvisioningOptions/Any(x:x eq 'Team')&$select=id,displayName&$top=100`
     paged → team count (+ first-page names echoed).
  2. `/users?$select=id&$top=100` first page → confirms `User.Read.All`.
  3. Channels of the first team → channel count sample.
  4. **The gate:** one message page
     (`/teams/{gid}/channels/{cid}/messages?$top=1`) from the first
     readable channel; classify the result:
     `200 → {"message_gate": "open"}`;
     `402 → {"message_gate": "metered-model-required"}` (client links an
     Azure subscription for model=A/B billing);
     `403 → {"message_gate": "protected-api-approval-missing"}` (client
     files Microsoft's protected-API request; days-to-weeks). All three
     print a `next_step` sentence; only 200 lets the skill proceed.
  5. Chat check, recorded not fatal: `/chats?$top=1` →
     `{"chats": "out-of-scope (no Chat.Read.All)"}` on 403, or a warning
     that chats ARE readable if 200 (scope conversation).
  6. Wall-clock estimate: channels sampled × pages-per-channel sampled at
     `DEFAULT_RPS_MESSAGES`, labeled `"estimate_basis": "sampled"` —
     counts only, never bytes. In `--dry-run`, print the URLs it WOULD
     call (the test greps `graph.microsoft.com/v1.0/groups`).

- [ ] **Step 4: Run to verify pass.** `python3 tests/test_harness.py`

- [ ] **Step 5: Commit.**
```bash
git add scripts/teams_transfer.py tests/test_harness.py
git commit -m "teams ingest: laptop CLI — Spec, 3-line creds with tenant guard, probe gate"
```

---

### Task 5: `teams_transfer.py` — transfer, status, verify, teardown, main

**Files:**
- Modify: `scripts/teams_transfer.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: engine `cmd_plan`, `cmd_create_vm`, `cmd_allow_network`,
  `cmd_check_azure`, `cmd_teardown`, `cmd_discover`, `mint_container_sas`;
  figma's laptop-verify helpers (`azure_list_blobs`,
  `compare_manifest_to_blobs` — copy both, same signatures).
- Produces: the full CLI surface the SKILL.md drives:
  `plan | create-vm | allow-network | write-dest | write-creds | probe |
  transfer | status | verify | teardown | discover`.

- [ ] **Step 1: Write the failing checks:**

```python
    proc = run_script("teams_transfer.py", "allow-network", "democo",
                      "--root", root, "--dry-run")
    check("teams allow-network: vnet path (VM family), never IP rules",
          "network-rule add" in proc.stdout and "--subnet" in proc.stdout
          and "--ip-address" not in proc.stdout)
    proc = run_script("teams_transfer.py", "write-dest", "democo",
                      "--root", root, "--dry-run")
    check("teams write-dest: racwl SAS -> dest-teams.env, redacted",
          "--permissions racwl" in proc.stdout
          and "teams-export" in proc.stdout
          and "dest-teams.env" in proc.stdout
          and "redacted" in proc.stdout)
    proc = run_script("teams_transfer.py", "transfer", "democo",
                      "--tenant-id",
                      "505f352c-ec82-4ff9-9191-556112b420f9",
                      "--root", root, "--dry-run")
    check("teams transfer: pushes the puller fresh into tmux window "
          "'teams', sourcing both env files",
          "teams_vm_pull.py" in proc.stdout
          and "tmux new-session -d -s transfer -n teams" in proc.stdout
          and "teams.env" in proc.stdout
          and "dest-teams.env" in proc.stdout)
    proc = run_script("teams_transfer.py", "verify", "democo", "--root",
                      root, "--dry-run")
    check("teams verify: laptop path — ip rule + rl SAS + manifest, "
          "never the write SAS",
          "--permissions rl" in proc.stdout
          and "network-rule add" in proc.stdout
          and "teams-export/_meta/manifest.json" in proc.stdout
          and "racwl" not in proc.stdout)
    proc = run_script("teams_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", expect_rc=2)
    check("teams teardown gated", '"not-confirmed"' in proc.stdout)
    proc = run_script("teams_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", "--confirmed")
    check("teams confirmed teardown: engine set + secret-rotation "
          "reminder",
          "network-rule remove" in proc.stdout
          and "rotate" in proc.stdout.lower()
          and "client secret" in proc.stdout.lower())
```

- [ ] **Step 2: Run to verify failure.** `python3 tests/test_harness.py`

- [ ] **Step 3: Implement.** Mirror figma_transfer.py function-for-function:
  - `cmd_write_dest`: `eng.mint_container_sas(cfg, args.sas_days)` (21-day
    default), write `DEST_ENV` with `DEST_URL`, `DEST_SAS`, `DEST_PREFIX`
    via `_write_env`; JSON output redacts the SAS.
  - `cmd_transfer`: `eng.require_vm` → `_push_puller(ip, dry_run)` (cat →
    `{XFER_DIR}/teams_vm_pull.py`) → optional `--limit-teams N` pilot env →
    launch:
    `tmux new-session -d -s transfer -n teams "set -a; . {TEAMS_ENV}; . {DEST_ENV}; set +a; python3 {XFER_DIR}/teams_vm_pull.py 2>&1 | tee -a {LOG_FILE}"`.
  - `cmd_status`: figma's `_STATUS_PY` pattern — ssh a python one-liner
    reading the puller's `progress.json` + log tail; no Azure calls.
  - `cmd_verify`: figma's laptop path verbatim: `phases.ip_rule_ensure` →
    1-day `rl` account SAS (`az storage account generate-sas … --permissions rl`)
    → GET `teams-export/_meta/manifest.json` → `azure_list_blobs(cfg, sas,
    "teams-export/")` → `compare_manifest_to_blobs` (name+size per unit;
    staged→container only) → `phases.ip_rule_remove_if_ours`. Nonzero on
    any mismatch, JSON report of missing/short blobs.
  - `cmd_teardown`: `eng.cmd_teardown` + a printed reminder that the
    client should **rotate the app's client secret** after verify (the
    engagement handed it over in chat).
  - `main()`: figma's argparse layout with subcommands mapped to engine
    functions where shared (`plan`→`eng.cmd_plan(SPEC,…)`, `create-vm`,
    `allow-network`, `check-azure`, `discover`) and local ones otherwise;
    `--sas-days` (default 21), `--rps-messages`, `--limit-teams`,
    `--dest-prefix`, `--root`, `--dry-run` flags.

- [ ] **Step 4: Run to verify pass.** `python3 tests/test_harness.py`

- [ ] **Step 5: Commit.**
```bash
git add scripts/teams_transfer.py tests/test_harness.py
git commit -m "teams ingest: transfer/status/verify/teardown — full CLI over the engine"
```

---

### Task 6: the skill — `SKILL.md` + `references/commands.md`

**Files:**
- Create: `.claude/skills/teams-azure-transfer/SKILL.md`
- Create: `.claude/skills/teams-azure-transfer/references/commands.md`

**Interfaces:**
- Consumes: the Task 4–5 CLI surface (exact subcommand names and flags).

- [ ] **Step 1: Write SKILL.md.** Frontmatter description is triggers-only
  (repo recipe): `Use when transferring a company's Microsoft Teams data —
  channel messages, thread replies, hosted content, team/channel/membership
  metadata — into their Azure -raw container — "transfer <company>'s
  teams", "pull their teams messages", "teams to azure", or any probe,
  status check, verification, or teardown of an existing teams transfer
  engagement.` Body sections, mirroring figma's SKILL.md structure:
  - **What this pulls / does NOT pull** — messages+metadata only; channel
    FILES belong to the sharepoint completion (the boundary, stated with
    the saxon evidence); chats/calendars/mail/tabs/Planner out of scope;
    the corpus is one copy per team; the membership index is how "per
    user" questions get answered.
  - **Client asks** — an Entra app registration with application
    permissions `Team.ReadBasic.All`, `Channel.ReadBasic.All`,
    `Group.Read.All`, `User.Read.All`, `ChannelMessage.Read.All`,
    admin-consented; tenant id + client id + client secret handed over;
    secret rotated after verify.
  - **The day-one stalls** — (1) the protected-API gate: probe's
    `message_gate` values and the client conversation each implies;
    (2) admin consent missing (403 on the very first directory read).
  - **Workflow** — probe → create-vm → allow-network → write-dest →
    write-creds → transfer (`--limit-teams 2` pilot first; manifest is
    `--overwrite=true` so a pilot never poisons verify) → status →
    verify → teardown. Each step's exact command lives in
    references/commands.md.
  - **Reconciliation** — `"microsoft_teams": {"prefix": "teams-export"}`;
    for saxon, the legacy `Microsoft-Teams/` prefix keeps its
    `duplicate_prefixes` treatment separately.
  - **Nudge/report guidance** — probe reports counts and a rough
    wall-clock, never bytes; `manifest.json`'s `total_staged_bytes` is
    the first real byte number (say so to the client).
- [ ] **Step 2: Write references/commands.md** with the copy-paste
  command per workflow step (heredoc stdin blocks for write-creds/probe,
  the 3-line order spelled out), mirroring
  `.claude/skills/figma-azure-transfer/references/commands.md`.
- [ ] **Step 3: Check frontmatter loads.**
  Run: `python3 - <<'EOF'` … parse the SKILL.md frontmatter with a small
  yaml-free split (name + description present, description starts with
  "Use when") `EOF` — or simply eyeball plus
  `head -5 .claude/skills/teams-azure-transfer/SKILL.md`.
- [ ] **Step 4: Commit.**
```bash
git add .claude/skills/teams-azure-transfer/
git commit -m "teams ingest: skill — workflow, day-one stalls, boundary with sharepoint"
```

---

### Task 7: CLAUDE.md — repo map, ingest paragraph, conventions

**Files:**
- Modify: `CLAUDE.md` (repo map block; the "Cloud → Azure transfers"
  section; the skills list in the repo map)

- [ ] **Step 1: Add repo-map lines** under the existing transfer entries:
  `teams-azure-transfer/SKILL.md` + `references/commands.md` in the
  skills block; `teams_transfer.py` and `teams_vm_pull.py` in the scripts
  block (one-line descriptions matching the established style).
- [ ] **Step 2: Add the ingest paragraph** after the figma one:
  "**Teams rides the engine's VM with its own REST pull layer.**" — cover:
  no bulk export for channel messages; app-only reads are protected APIs
  (probe's 200/402/403 gate is the day-one stall); `xfer-teams-<slug>`;
  guid-led `teams-export/` layout (`_meta` + `teams/<guid>/<channel>/`);
  a JSONL line is always a complete thread (replies paginated before
  write); hosted content staged, attachment bytes NOT fetched (the
  sharepoint boundary); classification keys on status + endpoint family
  (figma's rule); `_meta` fatal, everything else per-unit skips; cursors
  per channel, re-walk on expiry; upload-as-units-complete
  `--overwrite=false`, manifest `--overwrite=true`; verify on the LAPTOP
  via `phases.ip_rule_ensure` + `rl` SAS, staged→container only; probe
  reports counts never bytes; 3 stdin secrets, tenant-vs-tag guard; out
  of scope list (chats without `Chat.Read.All`, calendars, mail, tabs,
  Planner, document-library files).
- [ ] **Step 3: Run the full suite one last time.**
  Run: `python3 tests/test_harness.py` — Expected: everything green.
- [ ] **Step 4: Commit.**
```bash
git add CLAUDE.md
git commit -m "teams ingest: CLAUDE.md — repo map + engine paragraph"
```

---

### Task 8: live pilot on saxon (integration, operator-driven)

No code. The engine convention: offline suite green → integration-test
live on the first engagement. Operator steps (the skill drives these):

- [ ] `probe saxon` with the client's 3 creds on stdin — expect
  `message_gate: open` (Relay's output proves the app can), team count ≈
  322 (cross-check against the container census), chats reported
  out-of-scope.
- [ ] `create-vm` → `allow-network` → `write-dest` → `write-creds` →
  `transfer --limit-teams 2` pilot; `status` until done; spot-check a
  pulled `messages.jsonl` for a thread that shows `"replies": null` in
  Relay's `_ChannelMessages` export and full replies in ours.
- [ ] Full `transfer`; `status`; `verify`; `teardown --confirmed`.
- [ ] Update `companies/saxon/expected-data-sizes.json`:
  `"microsoft_teams": {"prefix": "teams-export"}` (bytes once the client
  declares; the manifest's `total_staged_bytes` is our first real
  number). Re-run sizing + `gen_report.py saxon`.
- [ ] Remind the client to rotate the app secret.

---

## Self-review (done at plan time)

- **Spec coverage:** every spec section maps to a task — components (T1–T6),
  auth + tenant guard (T1/T4), protected-API gate (T4), units/layout
  (T2–T3), pacing/classification (T1), resume (T3), lifecycle/verify (T5),
  reconciliation + docs (T6–T7), success criteria (T8).
- **Deviation from spec, deliberate:** the spec's "no offline tests" line
  followed an outdated reading of the repo; the ACTUAL engine convention
  (figma/zoho blocks in test_harness.py) is dry-run + source-invariant
  checks, and this plan follows the repo. The spec's Docs-and-tests section
  should be read accordingly.
- **Type consistency:** `classify` families are `directory | messages |
  team | channel` everywhere; marker helpers keep figma's exact signatures;
  `TokenBox` laptop/VM twins differ only in error type (HarnessError vs
  SystemExit), per the documented precedent.
