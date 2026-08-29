#!/usr/bin/env python3
"""VM-side Microsoft Teams puller: channels + messages + replies +
hostedContents metadata (directory reads via Graph) -> azcopy.

Runs on the transfer VM inside tmux, launched by scripts/teams_transfer.py
with ~/.config/xfer/{teams.env,dest-teams.env} sourced:
  TEAMS_TENANT_ID     — AAD tenant id (or verified domain)
  TEAMS_CLIENT_ID     — client-made app registration's client id
  TEAMS_CLIENT_SECRET — that app's client secret
  DEST_URL            — https://ACCT.blob.core.windows.net/<container>
  DEST_SAS            — racwl container SAS
  DEST_PREFIX         — export sub-path under the container (default
                        "teams-export"); this script appends it itself —
                        unlike figma/zoho's already-prefixed DEST_URL —
                        so DEST_PREFIX is a real, consumed setting here.
  RPS_MESSAGES        — optional pacing override (default 4/s)
  RPS_DIRECTORY       — optional pacing override (default 10/s)
  LIMIT_TEAMS         — optional pilot: only the first N teams
  REFRESH_META        — optional: "1" clears the _meta unit's completion
                        marker before pulling, so a roster gone stale
                        between a --limit-teams pilot and the full run is
                        re-walked instead of served from is_complete's
                        short-circuit (manual VM rescue only)

Why a VM at all: a real tenant's Teams corpus is every team's every
channel's every message thread plus every reply, walked page by page
against Graph's per-app throttling — a multi-hour-to-multi-day job that
wants tmux detachment and per-unit durability, the github/zoho/figma
precedent. The pull is app-only (client-credentials, no signed-in user),
so it authenticates once per token lifetime and re-mints as needed; no
delegated/browser flow is involved anywhere in this file.

The unit model mirrors figma's: one discovery ledger (`_meta` — groups
filtered to resourceProvisioningOptions contains "Team", their channels,
and members) is the one REQUIRED unit, then one unit per channel holding
that channel's messages + replies + hostedContents references. A unit
that 403/404s (an archived team, a private channel the app can't see) is
a recorded skip, never fatal, EXCEPT the protected messaging endpoints
(the "messages" family) refusing with 402/403 — Teams messaging content
is a metered, permission-gated Graph surface and that refusal is the
day-one stall, not a per-unit quirk (classify() below encodes this).

classify() keys on STATUS + endpoint FAMILY plus a `required` flag, the
zoho/figma precedent of "a table, not judgment": 429 always sleeps (the
Retry-After backstop lives in the request layer, never here), 401 always
re-mints (the token can genuinely expire mid-run), 5xx/408 always retry.
Below that, `required` units (the `_meta` discovery walk) are fatal on
any refusal — there is nothing to plan without them. Optional units follow
the messages-vs-team/channel split described above.

Secrets never touch argv, a log line, or a file: the three credential
values arrive over this process's stdin only (github/zoho precedent) and
the minted access token lives in memory (TokenBox) alone — it is never
written to disk, a tag, or a log line.

Resume is per-unit `.cdp-complete` markers plus `.cdp-cursor.json`
cursors for the paginated units (a channel's message history can be
large): `resume_truncate` discards a torn trailing JSONL line so a
crash mid-page never leaves a half-written record ahead of its cursor.

Stdlib-only (azcopy on PATH — bootstrap-vm.sh installs it). Import-safe
on the laptop so the pure functions (classify, TokenBox, PaceBucket,
safe_component, resume_truncate, the marker helpers) are unit-testable
offline; nothing runs at import time. This file is pushed to the VM
ALONE and never imports from the repo (no `common`, no `phases`) — it
must stand on its own exactly like figma_vm_pull.py, github_vm_pull.py
and zoho_vm_pull.py before it.

The channel unit (`pull_channel`) writes one JSONL line per ROOT message,
each carrying its replies fully expanded inline — a JSONL line is ALWAYS a
complete thread, never a partial one paused mid-reply-page. Hosted content
referenced from a thread's HTML body (`hostedContents/<id>/$value`) is
staged as bytes under the unit's `hosted/` dir; attachment objects (a
different Teams primitive — document-library files, whose bytes belong to
the sharepoint-completion effort) are deliberately left as references in
the message JSON and never fetched.

Exit codes: 0 = complete success, 1 = fatal setup error (bad credentials /
no azcopy / missing env), 2 = finished but one or more units failed or
were skipped (see manifest.json).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
TOKEN_PATH_FMT = "{login}/{tenant}/oauth2/v2.0/token"
TOKEN_REFRESH_MARGIN = 120   # re-mint 2 min before the 1 h expiry
API_RETRIES = 4
MAX_SLEEPS_PER_CALL = 50    # 429 never counts against API_RETRIES (sustained
                            # throttling is not fatal), but this runaway
                            # backstop bounds a call that never stops being
                            # throttled.
DEFAULT_RPS_MESSAGES = 4.0   # conservative; Teams messaging is Graph's slow lane
DEFAULT_RPS_DIRECTORY = 10.0 # groups/users/channels reads
DEFAULT_DEST_PREFIX = "teams-export"
MESSAGES_PAGE_SIZE = 50

# hostedContents/$value URLs embedded in a message's body.content HTML —
# never an attachment object's own download-URL field, which stays as a
# reference (see module docstring). Group 1 = the message id the content
# is attached to (a thread ROOT id always appears here, even for a reply's
# hosted content — Graph's reply-shaped hostedContents URLs nest the reply
# under the root: ".../messages/{rootId}/replies/{replyId}/hostedContents/
# {id}/$value"), group 2 = the reply id when the URL is reply-shaped (None
# for a root message's own hosted content), group 3 = the hostedContent id.
# The whole match (group 0) is fetched VERBATIM — never reconstructed from
# ids — because a reply-shaped URL's path differs from a root one's and
# reconstructing it would require re-deriving that shape.
HOSTED_RE = re.compile(
    r"https://graph\.microsoft\.com/v1\.0/teams/[^\"'\s]+"
    r"/messages/([^/\"'\s]+)"
    r"(?:/replies/([^/\"'\s]+))?"
    r"/hostedContents/([^/\"'\s]+)/\$value")

EXT_BY_CONTENT_TYPE = {"image/png": ".png", "image/jpeg": ".jpg",
                       "image/gif": ".gif", "image/webp": ".webp",
                       "image/svg+xml": ".svg", "video/mp4": ".mp4",
                       "application/pdf": ".pdf"}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return f"{n} B"


def write_progress(dest: Path, phase: str, done: int, total: int,
                   message: str) -> None:
    """Heartbeat teams_transfer.py's status subcommand reads. Never fatal."""
    try:
        (dest / "progress.json").write_text(json.dumps(
            {"source": "teams", "phase": phase, "done": done,
             "total": total, "message": message}))
    except OSError:
        pass


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def atomic_write_json(path: Path, obj) -> None:
    """Cursors must never be half-written: a torn cursor would resume a
    channel walk at a bogus offset."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def safe_component(name: str, limit: int = 120) -> str:
    """PURE. Blob-safe, deterministic path component. Callers put the
    Teams id (team id, channel id) FIRST and this second, so two channels
    sharing a display name never collide and the name is identical across
    runs (resume keys on the exact name — the zoom rule; Teams display
    names are mutable, ids are not)."""
    out = []
    for ch in str(name or ""):
        out.append(ch if (ch.isascii() and (ch.isalnum() or ch in "._-"))
                   else "_")
    s = "".join(out).strip("._-") or "unnamed"
    return s[:limit]


def resume_truncate(jsonl: Path, cursor: dict) -> int:
    """Truncate a partially written JSONL back to the last whole page and
    return the surviving line count. The cursor's byte offset is written
    only after a full page has been flushed and fsynced, so anything past
    it is a torn trailing line from a crash."""
    want = int((cursor or {}).get("bytes") or 0)
    if not jsonl.exists():
        return 0
    size = jsonl.stat().st_size
    if want <= 0 or want > size:
        return 0 if want <= 0 else _count_lines(jsonl)
    with open(jsonl, "r+b") as fh:
        fh.truncate(want)
    return _count_lines(jsonl)


def _count_lines(path: Path) -> int:
    n = 0
    with open(path, "rb") as fh:
        for _ in fh:
            n += 1
    return n


def fill_ext(content_type: str) -> str:
    """PURE. Copied from figma_vm_pull.py. Extension for a downloaded
    hostedContent blob, from Graph's Content-Type ($value URLs carry no
    extension). Unknown types keep no extension rather than guessing."""
    return EXT_BY_CONTENT_TYPE.get((content_type or "").split(";")[0]
                                   .strip().lower(), "")


# ── unit bookkeeping ─────────────────────────────────────────────────────────

def unit_dir(dest: Path, unit: str) -> Path:
    d = dest / unit
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_complete(d: Path) -> bool:
    return (d / ".cdp-complete").exists()


def mark_complete(d: Path) -> None:
    (d / ".cdp-complete").write_text("")


def mark_skipped(d: Path, reason: str, detail: str = "") -> None:
    """A deliberate skip is recorded IN THE CONTAINER, not only in the
    manifest, so a later auditor reading the blobs can see it."""
    atomic_write_json(d / ".cdp-skipped.json", {
        "reason": reason, "detail": detail,
        "recorded_utc": datetime.now(timezone.utc).isoformat()})


def read_cursor(d: Path) -> dict | None:
    try:
        return json.loads((d / ".cdp-cursor.json").read_text())
    except (OSError, ValueError):
        return None


def clear_unit(d: Path) -> None:
    for name in (".cdp-complete", ".cdp-cursor.json", ".cdp-skipped.json"):
        try:
            (d / name).unlink()
        except OSError:
            pass


def _result(unit: str, kind: str, status: str, **extra) -> dict:
    out = {"unit": unit, "kind": kind, "status": status}
    out.update(extra)
    return out


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
    users, teams, channels, members), 'messages' (channel message
    LISTINGS and reply pagination — the surface the protected-API gate
    guards), 'hosted' (individual hostedContents/$value fetches — paced
    like messages but a 403 here is a PER-ITEM refusal, e.g. Graph
    refusing a video preview's bytes, counted as a hosted_error and
    never fatal; killed a real saxon run at 664/833 before this split),
    'team' / 'channel' (a single optional unit's refusal)."""
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


class GraphAPI:
    """Thin Graph client: pacing (one bucket per family, 'messages' vs
    everything-else-is-directory), retry/sleep/remint per classify(), and
    the SINGLE place that builds the Authorization header (bearer only —
    the app-only token from TokenBox, never a delegated/browser flow)."""

    def __init__(self, box, rps_messages=DEFAULT_RPS_MESSAGES,
                 rps_directory=DEFAULT_RPS_DIRECTORY):
        self.box = box
        self._buckets = {"messages": PaceBucket(rps_messages)}
        self._dir_bucket = PaceBucket(rps_directory)
        self.calls = 0
        self.sleeps = 0

    def _bucket(self, family):
        # "hosted" shares the messages bucket: hostedContents/$value is
        # Tier-1-adjacent Teams traffic, only its FAILURE classification
        # differs (per-item skip, never the protected-API fatal).
        if family == "hosted":
            family = "messages"
        return self._buckets.get(family, self._dir_bucket)

    def get_raw(self, url: str, family: str, required: bool = False):
        """attempt counts only retry/remint tries — a 429 sleep is NEVER
        charged against API_RETRIES (I3: sustained throttling must not
        become fatal), it only consumes its own generous MAX_SLEEPS_PER_CALL
        backstop. reminted tracks the remint-once guard on its OWN flag
        (m4) rather than the shared attempt counter, so a 401 that shows up
        after an earlier transient retry still gets its one re-mint instead
        of being treated as already-reminted.

        On exhausted retries: `required=True` raises (nothing can proceed
        without it), `required=False` RETURNS the last terminal status
        instead (C2) — the caller (pull_channel's cursor/fresh-start
        branches) already knows how to treat a non-2xx status as a
        recorded skip / clear-and-rewalk; raising SystemExit here would
        kill the whole pass over one deterministic 4xx on an optional
        call."""
        attempt = 1
        sleeps_this_call = 0
        reminted = False
        status, body = 599, b""
        while True:
            self._bucket(family).wait()
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.box.get()}"})
            self.calls += 1
            retry_after_hdr = None
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return (r.status, r.read(),
                            r.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
                retry_after_hdr = e.headers.get("Retry-After")
            except (urllib.error.URLError, TimeoutError):
                status, body = 599, b""
            action = classify(status, family, required)
            if action == "sleep":
                retry_after = 30
                try:
                    retry_after = int(retry_after_hdr)
                except (TypeError, ValueError):
                    pass
                self.sleeps += 1
                sleeps_this_call += 1
                if sleeps_this_call > MAX_SLEEPS_PER_CALL:
                    raise SystemExit(
                        f"429 sleep budget ({MAX_SLEEPS_PER_CALL}) "
                        f"exhausted on {family} {url.split('?')[0]} — "
                        "sustained throttling that never let up")
                time.sleep(min(300, max(1, retry_after)))
                continue          # sleeps never consume API_RETRIES
            elif action == "remint":
                self.box.invalidate()
                if reminted:
                    raise SystemExit("401 persists after re-mint — token "
                                     "or permission problem")
                reminted = True
            elif action == "retry":
                time.sleep(min(120, 10 * attempt))
            elif action == "fatal":
                raise SystemExit(
                    f"fatal {status} on {family} {url.split('?')[0]}: "
                    f"{body[:300].decode('utf-8', 'replace')}")
            else:            # skip — terminal, caller records it
                return (status, body, "")
            attempt += 1
            if attempt > API_RETRIES + 1:
                if required:
                    raise SystemExit(
                        f"retries exhausted on {url.split('?')[0]}")
                return (status, body, "")

    def get(self, path, family, params=None, required=False):
        url = GRAPH + path
        if params:
            url += "?" + urllib.parse.urlencode(params, safe="$()/'! ,=")
        status, body, _ = self.get_raw(url, family, required)
        if status in (200, 201) and body:
            return status, json.loads(body.decode())
        return status, None


def paged(api, path, family, params, required=False):
    """Generator over items across @odata.nextLink pages. A refused first
    page or a refused nextLink both just end the generator (the caller
    already got the terminal status via api.get/get_raw's own handling —
    classify() raises SystemExit for anything that should actually stop
    the run, so reaching here with a non-2xx status means a recorded skip)."""
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


def _paged_from_status(api, family, first_status, first_body,
                        required=False):
    """Continue a paged walk from an ALREADY-FETCHED first page, so a
    caller that needs the first page's status (to record a member-list
    refusal) never pays for it twice."""
    status, body = first_status, first_body
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


def pull_meta(api: GraphAPI, dest: Path) -> dict:
    """The one REQUIRED unit: org roster + channels + membership index.
    required=True everywhere here — classify() makes any refusal on these
    calls fatal, EXCEPT the specific per-team/per-channel tolerances noted
    inline (an archived team's settings 404ing, a team/channel's member
    list being 403'd) which are recorded, not fatal."""
    d = unit_dir(dest, "_meta")
    if is_complete(d):
        log("_meta: already complete, skipping")
        teams = [json.loads(ln) for ln in
                 (d / "teams.jsonl").read_text().splitlines() if ln.strip()]
        # Seed every team (even one with zero channels) so the reconstructed
        # roster matches the freshly-written one key-for-key.
        channels: dict[str, list] = {t["id"]: [] for t in teams}
        for ln in (d / "channels.jsonl").read_text().splitlines():
            if not ln.strip():
                continue
            rec = json.loads(ln)
            channels.setdefault(rec["team_id"], []).append(rec)
        return {"teams": teams, "channels": channels,
                "counts": {"teams": len(teams),
                           "channels": sum(len(v)
                                           for v in channels.values())}}

    teams = []
    channels: dict[str, list] = {}
    name_map = {"teams": {}, "channels": {}}

    teams_path = d / "teams.jsonl"
    with open(teams_path, "w") as fh:
        for grp in paged(
                api, "/groups", "directory",
                {"$filter":
                 "resourceProvisioningOptions/Any(x:x eq 'Team')",
                 "$select": "id,displayName,description,"
                            "createdDateTime,visibility",
                 "$top": "100"},
                required=True):
            gid = grp["id"]
            tstatus, settings = api.get(f"/teams/{gid}", "directory",
                                         required=False)
            rec = dict(grp)
            if settings is None:
                rec["team_settings_status"] = tstatus
            else:
                rec.update(settings)
            fh.write(json.dumps(rec) + "\n")
            teams.append(rec)
            name_map["teams"][gid] = grp.get("displayName")

    channels_path = d / "channels.jsonl"
    with open(channels_path, "w") as fh:
        for team in teams:
            gid = team["id"]
            team_channels = []
            for ch in paged(api, f"/teams/{gid}/channels", "directory",
                             {}, required=True):
                rec = dict(ch)
                rec["team_id"] = gid
                fh.write(json.dumps(rec) + "\n")
                team_channels.append(rec)
                name_map["channels"][ch["id"]] = ch.get("displayName")
            channels[gid] = team_channels

    users_path = d / "users.jsonl"
    with open(users_path, "w") as fh:
        for user in paged(
                api, "/users", "directory",
                {"$select": "id,userPrincipalName,displayName,mail,"
                            "accountEnabled,userType",
                 "$top": "100"},
                required=True):
            fh.write(json.dumps(user) + "\n")

    team_members_path = d / "team-members.jsonl"
    with open(team_members_path, "w") as fh:
        for team in teams:
            gid = team["id"]
            status, body = api.get(
                f"/groups/{gid}/members", "directory",
                {"$select": "id,displayName,userPrincipalName",
                 "$top": "100"},
                required=False)
            if status not in (200, 201):
                fh.write(json.dumps(
                    {"team_id": gid, "members_status": status}) + "\n")
                continue
            for member in _paged_from_status(api, "directory", status,
                                              body, required=False):
                rec = dict(member)
                rec["team_id"] = gid
                fh.write(json.dumps(rec) + "\n")

    channel_members_path = d / "channel-members.jsonl"
    with open(channel_members_path, "w") as fh:
        for gid, team_channels in channels.items():
            for ch in team_channels:
                if ch.get("membershipType") == "standard":
                    continue
                cid = ch["id"]
                status, body = api.get(
                    f"/teams/{gid}/channels/{cid}/members", "directory",
                    required=False)
                if status not in (200, 201):
                    fh.write(json.dumps(
                        {"team_id": gid, "channel_id": cid,
                         "members_status": status}) + "\n")
                    continue
                for member in _paged_from_status(api, "directory", status,
                                                  body, required=False):
                    rec = dict(member)
                    rec["team_id"] = gid
                    rec["channel_id"] = cid
                    fh.write(json.dumps(rec) + "\n")

    atomic_write_json(d / "name-map.json", name_map)

    mark_complete(d)
    return {"teams": teams, "channels": channels,
            "counts": {"teams": len(teams),
                       "channels": sum(len(v) for v in channels.values())}}


# ── channel units (messages + replies + hostedContents) ─────────────────────

def hosted_refs(msg: dict) -> list:
    """PURE. (fetch_url, innermost_mid, hostedContent-id) triples referenced
    by this thread's HTML bodies — root and replies. `fetch_url` is the
    FULL matched URL, fetched verbatim by the caller rather than
    reconstructed from ids (a reply-shaped URL nests under
    `.../messages/{rootId}/replies/{replyId}/hostedContents/{id}/$value`,
    a different shape than a root message's own `.../messages/{rootId}/
    hostedContents/{id}/$value` — reconstruction would have to re-derive
    which shape applies). `innermost_mid` is the reply id when the URL is
    reply-shaped, else the (root) message id — this is what the staged
    filename keys on, so two replies' hosted content never collides.

    Every candidate URL is host-checked (netloc == graph.microsoft.com)
    before being yielded — the figma next_page precedent: a URL lifted out
    of message HTML is untrusted content and must never be followed
    off-host with a Bearer token, even though the anchoring regex already
    makes a non-Graph match unlikely.

    Attachment objects are left as references (document-library bytes
    belong to the sharepoint completion), so only hostedContents URLs are
    harvested."""
    out, seen = [], set()

    def scan(m):
        content = ((m.get("body") or {}).get("content")) or ""
        for match in HOSTED_RE.finditer(content):
            url = match.group(0)
            if urllib.parse.urlparse(url).netloc != "graph.microsoft.com":
                continue
            mid, reply_id, hcid = match.group(1), match.group(2), \
                match.group(3)
            innermost = reply_id or mid
            key = (innermost, hcid)
            if key not in seen:
                seen.add(key)
                out.append((url, innermost, hcid))
    scan(msg)
    for rep in msg.get("replies") or []:
        scan(rep)
    return out


def complete_thread(api, gid, cid, msg) -> dict:
    """Pages a truncated replies list to completion before the caller ever
    writes the JSONL line — a line is ALWAYS a complete thread, never a
    partial one paused mid-reply-page. gid/cid are accepted (not used
    inside) for interface parity with pull_channel's other calls."""
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


def _count_replies(jsonl: Path) -> int:
    """Total replies across every ROOT message currently on disk — used to
    seed a resumed channel's reply count from the retained (post-
    resume_truncate) lines, so the final result is cumulative rather than
    a per-invocation delta. Channels are small, so a full re-read is
    cheap."""
    if not jsonl.exists():
        return 0
    n = 0
    with open(jsonl, "r", encoding="utf-8") as fh:
        for ln in fh:
            if not ln.strip():
                continue
            n += len(json.loads(ln).get("replies") or [])
    return n


def pull_channel(api, gid, cid, dest: Path, args) -> dict:
    """One channel = one unit: `teams/<gid>/<cid>/messages.jsonl` (one
    complete thread per line) + a `hosted/` dir of hostedContents blobs.

    Resume is `.cdp-cursor.json` ({"next_link", "lines", "bytes"}) plus
    `resume_truncate` discarding a torn trailing line. A cursor whose
    `next_link` now answers a non-2xx (most realistically a 404 — the
    channel was deleted or archived mid-run; a 403 on the "messages"
    family is NOT a realistic case here since classify() already makes
    that fatal via api.get_raw before this branch is ever reached) is NOT
    trusted forward — the unit is cleared and re-walked from scratch,
    which is cheap because channels are small (unlike a CRM module's
    millions of records).

    The final result's `messages`/`replies`/`hosted` counts are the
    CUMULATIVE on-disk truth, not this invocation's delta: `messages` is
    seeded by `resume_truncate`'s surviving line count, `replies` is
    seeded by counting replies already present in those retained lines,
    and `hosted` is the count of files actually present in `hosted/` at
    completion — so a channel that resumes after a crash reports the
    whole channel's totals, not just what this pass added.

    A terminal skip on the very FIRST page (no cursor at all — cid is 404,
    an archived/deleted channel) is recorded via mark_skipped and returned
    as a skip, never a failure. A terminal refusal on a LATER page (mid-
    walk, after at least one page already landed) is different — it still
    completes the unit with what was walked, but the result carries a
    `pagination_truncated: "status-N"` field (never-silent) instead of
    reading as a clean, total walk. `args` is accepted for interface parity
    with figma/zoho's per-unit signature; nothing here reads it today."""
    unit = f"teams/{gid}/{cid}"
    d = unit_dir(dest, unit)
    if is_complete(d):
        return _result(unit, "channel", "skipped-complete", bytes=dir_size(d))

    jsonl = d / "messages.jsonl"
    cursor = read_cursor(d)
    total = 0
    body = None

    if cursor:
        total = resume_truncate(jsonl, cursor)
        next_link = cursor.get("next_link")
        if next_link:
            status, raw, _ = api.get_raw(next_link, "messages")
            if status not in (200, 201):
                log(f"{unit}: cursor next_link refused (status {status}) — "
                    "clearing and re-walking from scratch (channels are "
                    "small)")
                clear_unit(d)
                try:
                    jsonl.unlink()
                except OSError:
                    pass
                return pull_channel(api, gid, cid, dest, args)
            body = json.loads(raw.decode()) if raw else None
        # else: pagination already finished last time (next_link was None)
        # — nothing left to page, fall through straight to hosted content.
    else:
        status, body = api.get(
            f"/teams/{gid}/channels/{cid}/messages", "messages",
            {"$top": str(MESSAGES_PAGE_SIZE), "$expand": "replies"})
        if status not in (200, 201):
            mark_skipped(d, f"status-{status}")
            return _result(unit, "channel", "skipped",
                           reason=f"status-{status}")

    # Seed from the on-disk truth BEFORE adding this pass's pages, so a
    # resumed channel's final count is cumulative, not a per-invocation
    # delta (a crash-resumed channel must report the whole channel, not
    # just what this pass added).
    n_replies = _count_replies(jsonl)
    pagination_truncated = None
    while body is not None:
        msgs = body.get("value", [])
        if msgs:
            with open(jsonl, "a", encoding="utf-8") as fh:
                for msg in msgs:
                    msg = complete_thread(api, gid, cid, msg)
                    n_replies += len(msg.get("replies") or [])
                    fh.write(json.dumps(msg, separators=(",", ":")) + "\n")
                    total += 1
                fh.flush()
                os.fsync(fh.fileno())
        nxt = body.get("@odata.nextLink")
        atomic_write_json(d / ".cdp-cursor.json", {
            "next_link": nxt, "lines": total,
            "bytes": jsonl.stat().st_size if jsonl.exists() else 0})
        if not nxt:
            break
        status, raw, _ = api.get_raw(nxt, "messages")
        if status not in (200, 201) or not raw:
            # A mid-walk page refusal (e.g. a 404 on page 2's nextLink) is
            # NOT the same as "pagination finished" — never-silent: record
            # it on the result (and thus the manifest) instead of falling
            # through to mark_complete as if every page had been walked.
            # The cursor above already names this exact next_link, so a
            # future run's cursor-resume path is the natural retry.
            pagination_truncated = f"status-{status}"
            body = None
            break
        body = json.loads(raw.decode())

    # hostedContents: harvested from the now-complete JSONL, one Bearer
    # fetch per unique (mid, hcid); deterministic filenames ARE the
    # resume, so an existing file (any extension) is skipped, never
    # re-fetched. Per-item refusals are counted, never fatal here — a
    # blanket refusal across the whole surface would already have raised
    # inside api.get_raw via classify()'s messages-family rule. `hosted`
    # in the final result is derived from files present on disk AFTER
    # this pass, not an incremental this-pass-only counter, so a resumed
    # channel reports the whole channel's hosted count, not just what
    # this pass fetched. `hosted_types` (Content-Type -> count) is
    # necessarily a THIS-PASS-only tally (Graph's Content-Type header is
    # only observed at fetch time; an already-landed file carries no
    # record of it) — an unknown content type still gets no filename
    # extension (fill_ext's existing behavior), but now it IS recorded
    # here under its raw Content-Type instead of vanishing with no trace.
    hosted_errors = 0
    hosted_types: dict[str, int] = {}
    hosted_dir = d / "hosted"
    if jsonl.exists():
        for ln in jsonl.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            msg = json.loads(ln)
            for url, mid, hcid in hosted_refs(msg):
                stem = f"{mid}_{safe_component(hcid, 60)}"
                if hosted_dir.is_dir() and any(
                        p.name == stem or p.name.startswith(stem + ".")
                        for p in hosted_dir.iterdir()):
                    continue
                # fetched VERBATIM (never reconstructed) — see hosted_refs
                hstatus, raw_h, ctype = api.get_raw(url, "hosted")
                if hstatus not in (200, 201) or raw_h is None:
                    hosted_errors += 1
                    continue
                ctype_key = ctype or "(unknown)"
                hosted_types[ctype_key] = hosted_types.get(ctype_key, 0) + 1
                hosted_dir.mkdir(parents=True, exist_ok=True)
                (hosted_dir / f"{stem}{fill_ext(ctype)}").write_bytes(raw_h)

    n_hosted = (sum(1 for p in hosted_dir.iterdir() if p.is_file())
               if hosted_dir.is_dir() else 0)

    mark_complete(d)
    extra = {}
    if pagination_truncated:
        extra["pagination_truncated"] = pagination_truncated
    if hosted_types:
        extra["hosted_types"] = hosted_types
    return _result(unit, "channel", "ok", messages=total,
                   replies=n_replies, hosted=n_hosted,
                   hosted_errors=hosted_errors, bytes=dir_size(d), **extra)


# ── manifest + upload ────────────────────────────────────────────────────────

def build_manifest(context: dict, started_utc: str, finished_utc: str,
                   api_calls: int, api_sleeps: int, results: list) -> dict:
    """PURE. verify's authority — adapted from figma_vm_pull.py's
    build_manifest (teams has no team_ids/plan inputs of its own: team
    discovery is org-wide, and pacing is RPS, not a pricing-tier plan, so
    both live inside `context` instead). failed_units and skipped_units
    stay strictly separate: a skip is deliberate and never a failure."""
    failed = [r["unit"] for r in results if r.get("status") == "failed"]
    skipped = [{"unit": r["unit"], "reason": r.get("reason"),
                "detail": r.get("detail")}
               for r in results if r.get("status") == "skipped"]
    total = sum(int(r.get("bytes") or 0) for r in results
                if r.get("status") in ("ok", "skipped-complete"))
    return {
        "source": "teams",
        "puller_version": 1,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "context": context,
        "unit_count": len(results),
        "total_staged_bytes": total,
        "api_calls": api_calls,
        "api_sleeps": api_sleeps,
        "failed_units": failed,
        "skipped_units": skipped,
        "hosted_errors": sum(int(r.get("hosted_errors") or 0)
                             for r in results),
        "results": results,
    }


def upload(dest: Path, dest_url: str, sas: str, subpath: str = "",
          overwrite: bool = False) -> bool:
    """azcopy the staged tree (or one unit of it) to the container prefix.
    Copied from figma_vm_pull.py verbatim (same flags, same write
    invariant): --overwrite=false is client-side no-overwrite (the s3/
    github/zoho/figma choice), NOT the API-enforced If-None-Match of the
    local (qwilr/vimeo/zoom) pulls, and NOT copy-source-authorization
    (that header belongs to the vimeo/zoom server-side-copy transport,
    structurally unavailable here — hostedContents need a Bearer fetch).
    SAS never printed; output is scanned, not echoed raw."""
    if shutil.which("azcopy") is None:
        log("upload: FATAL azcopy not found on PATH — bootstrap incomplete")
        return False
    src = dest / subpath if subpath else dest
    url = f"{dest_url}/{subpath}" if subpath else dest_url
    if not src.exists():
        return True
    log(f"upload: azcopy {src} -> {url.split('?')[0]}")
    # Trailing /* copies dir CONTENTS so the container prefix isn't nested.
    proc = subprocess.run(
        ["azcopy", "copy", str(src) + "/*", f"{url}?{sas}",
         "--recursive",
         "--overwrite=true" if overwrite else "--overwrite=false",
         "--log-level", "ERROR"],
        capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    failed = "0"
    status = ""
    for ln in out.splitlines():
        if "Transfers Failed" in ln:
            failed = ln.split(":")[-1].strip()
        if "Final Job Status" in ln:
            status = ln.split(":")[-1].strip()
    ok = failed == "0" and (proc.returncode == 0
                            or status.startswith("Completed"))
    log(f"upload: {'DONE' if ok else 'FAILED'} "
        f"(status={status or 'rc=%d' % proc.returncode}, failed={failed})")
    if not ok:
        # never echo raw azcopy output wholesale — a URL line would leak
        # the SAS; keep only sig-free lines
        for ln in [x for x in out.splitlines()[-15:] if "sig=" not in x]:
            log(f"upload:   {ln}")
    return ok


def upload_run_metadata(dest: Path, dest_url: str, sas: str) -> bool:
    """progress.json (dest root) and _meta/manifest.json ride with
    overwrite ALLOWED.

    Everything else rides --overwrite=false, but these are OUR run
    bookkeeping, not client corpus data — and manifest.json is exactly
    what verify treats as authoritative. Uploading it no-overwrite means a
    re-run's manifest is silently skipped and verify certifies against the
    FIRST pass forever (the github pilot-poisons-verify bug, observed
    live; zoho/figma shipped the fix from day one; teams inherits it).

    UNLIKE figma (manifest.json at the dest ROOT), teams' manifest lives
    at `_meta/manifest.json` — the spec layout, and where a later verify
    greps `teams-export/_meta/manifest.json` (controller ruling,
    2026-08-28)."""
    if shutil.which("azcopy") is None:
        return False
    ok = True
    for rel in ("progress.json", "_meta/manifest.json"):
        src = dest / rel
        if not src.exists():
            continue
        proc = subprocess.run(
            ["azcopy", "copy", str(src), f"{dest_url}/{rel}?{sas}",
             "--overwrite=true", "--log-level", "ERROR"],
            capture_output=True, text=True)
        good = proc.returncode == 0
        log(f"upload: {rel} {'DONE' if good else 'FAILED'} (overwrite=true)")
        ok = ok and good
    # Per-unit control files are bookkeeping too: a re-walked unit rewrites
    # a cursor/skip marker and no-overwrite would keep the stale one, so
    # verify would report a phantom short_upload on a since-fixed unit.
    proc = subprocess.run(
        ["azcopy", "copy", str(dest) + "/*", f"{dest_url}?{sas}",
         "--recursive", "--overwrite=true", "--log-level", "ERROR",
         "--include-pattern",
         ".cdp-complete;.cdp-cursor.json;.cdp-skipped.json"],
        capture_output=True, text=True)
    good = proc.returncode == 0
    log(f"upload: control files {'DONE' if good else 'FAILED'} "
        "(overwrite=true)")
    return ok and good


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    """Fully env-driven — no argv, per the secrets-never-touch-argv rule
    and because teams_transfer.py launches this with no arguments at all
    (`python3 teams_vm_pull.py` inside tmux, both env files pre-sourced)."""
    tenant = os.environ.get("TEAMS_TENANT_ID", "").strip()
    client_id = os.environ.get("TEAMS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TEAMS_CLIENT_SECRET", "").strip()
    if not (tenant and client_id and client_secret):
        log("FATAL: TEAMS_TENANT_ID/TEAMS_CLIENT_ID/TEAMS_CLIENT_SECRET "
            "not all in environment (teams.env not sourced?)")
        return 1
    dest_url = os.environ.get("DEST_URL", "").strip()
    dest_sas = os.environ.get("DEST_SAS", "").strip()
    if not (dest_url and dest_sas):
        log("FATAL: DEST_URL/DEST_SAS not in environment "
            "(dest-teams.env not sourced?)")
        return 1
    dest_prefix = (os.environ.get("DEST_PREFIX", "").strip()
                  or DEFAULT_DEST_PREFIX)
    full_dest_url = f"{dest_url.rstrip('/')}/{dest_prefix}"

    def _env_float(name, default):
        raw = os.environ.get(name, "").strip()
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    rps_messages = _env_float("RPS_MESSAGES", DEFAULT_RPS_MESSAGES)
    rps_directory = _env_float("RPS_DIRECTORY", DEFAULT_RPS_DIRECTORY)
    limit_raw = os.environ.get("LIMIT_TEAMS", "").strip()
    limit_teams = int(limit_raw) if limit_raw.isdigit() else 0

    dest = Path(os.path.expanduser(
        os.environ.get("XFER_DEST", "~/xfer-teams/dest")))
    dest.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()

    box = TokenBox(tenant, client_id, client_secret)
    box.mint()  # proves credentials before anything else
    api = GraphAPI(box, rps_messages=rps_messages, rps_directory=rps_directory)

    # REFRESH_META=1 clears the _meta unit's completion marker (not its
    # jsonl files — pull_meta always overwrites them in "w" mode) so a
    # roster that went stale between a --limit-teams pilot and the full
    # run gets re-walked instead of being served from is_complete's
    # short-circuit. Manual VM rescue: `export REFRESH_META=1` ahead of
    # `python3 teams_vm_pull.py` (see commands.md).
    if os.environ.get("REFRESH_META", "").strip() == "1":
        meta_unit_dir = dest / "_meta"
        if is_complete(meta_unit_dir):
            log("REFRESH_META=1: clearing _meta's completion marker — "
                "the roster will be walked fresh")
        clear_unit(meta_unit_dir)

    write_progress(dest, "walk", 0, 0, "_meta")
    log("pulling _meta (teams/channels/users/membership)")
    roster = pull_meta(api, dest)
    meta_dir = dest / "_meta"
    upload(dest, full_dest_url, dest_sas, "_meta")

    # pull_meta() only raises (never returns) on a genuine failure of its
    # one REQUIRED walk, so reaching here always means "ok" — but it must
    # still be RECORDED into results, or its bytes/unit never enter the
    # manifest's total_staged_bytes/unit_count and verify's authoritative
    # _meta/manifest.json undercounts the whole uploaded tree (the _meta
    # prefix is real staged/uploaded bytes, same as any channel unit).
    results: list = [_result(
        "_meta", "meta", "ok", bytes=dir_size(meta_dir),
        teams=roster["counts"].get("teams"),
        channels=roster["counts"].get("channels"))]

    teams = roster["teams"]
    if limit_teams:
        teams = teams[:limit_teams]
    channel_map = roster.get("channels") or {}
    plan = [(t["id"], ch["id"]) for t in teams
            for ch in channel_map.get(t["id"], [])]
    log(f"{len(teams)} team(s), {len(plan)} channel(s) planned"
        + (f" (LIMIT_TEAMS={limit_teams})" if limit_teams else ""))

    for i, (gid, cid) in enumerate(plan, 1):
        unit = f"teams/{gid}/{cid}"
        write_progress(dest, "pull", i, len(plan), unit)
        log(f"[{i}/{len(plan)}] {unit}")
        res = pull_channel(api, gid, cid, dest, None)
        results.append(res)
        if res.get("status") == "ok":
            upload(dest, full_dest_url, dest_sas, res["unit"])

    context = {"teams": roster["counts"].get("teams"),
              "channels": roster["counts"].get("channels"),
              "channels_planned": len(plan),
              "limit_teams": limit_teams or None,
              "dest_prefix": dest_prefix,
              "rps_messages": rps_messages,
              "rps_directory": rps_directory}
    manifest = build_manifest(
        context, started, datetime.now(timezone.utc).isoformat(),
        api.calls, api.sleeps, results)
    meta_dir = dest / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    failed = manifest["failed_units"]
    skipped = manifest["skipped_units"]
    log(f"SUMMARY: {len(results)} units, "
        f"{human_bytes(manifest['total_staged_bytes'])} staged, "
        f"{api.calls} api calls, {api.sleeps} sleeps, "
        f"{len(failed)} failed {failed if failed else ''}, "
        f"{len(skipped)} skipped")

    # upload-what-succeeded: the final sweep is cheap because
    # --overwrite=false skips everything already landed per unit, and it
    # is what carries any leftover control/skip-marker files up.
    write_progress(dest, "upload", len(results), len(results),
                   "azcopy final sweep")
    upload_ok = upload(dest, full_dest_url, dest_sas)
    # the manifest must REPLACE any earlier pass's — verify trusts it
    upload_ok = upload_run_metadata(dest, full_dest_url, dest_sas) \
        and upload_ok
    write_progress(dest, "done", len(results), len(results),
                   f"{len(failed)} failed, {len(skipped)} skipped")
    return 2 if failed or skipped or not upload_ok else 0


if __name__ == "__main__":
    sys.exit(main())
