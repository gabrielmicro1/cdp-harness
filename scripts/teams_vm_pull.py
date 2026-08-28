#!/usr/bin/env python3
"""VM-side Microsoft Teams puller: channels + messages + replies +
hostedContents metadata (directory reads via Graph) -> azcopy.

Runs on the transfer VM inside tmux, launched by scripts/teams_transfer.py
with ~/.config/xfer/{teams.env,dest-teams.env} sourced:
  TEAMS_TENANT_ID     — AAD tenant id (or verified domain)
  TEAMS_CLIENT_ID     — client-made app registration's client id
  TEAMS_CLIENT_SECRET — that app's client secret
  AZURE_DEST_URL      — https://ACCT.blob.core.windows.net/<cont>/<prefix>
  AZURE_DEST_SAS      — racwl container SAS

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

This module holds helpers + classes only; main() and the Graph-calling
pull logic land in later tasks.

Exit codes (once main() exists): 0 = complete success, 1 = fatal setup
error (bad credentials / no teams reachable / no azcopy), 2 = finished
but one or more units failed (see manifest.json).
"""
from __future__ import annotations

import json
import os
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
DEFAULT_RPS_MESSAGES = 4.0   # conservative; Teams messaging is Graph's slow lane
DEFAULT_RPS_DIRECTORY = 10.0 # groups/users/channels reads


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
