#!/usr/bin/env python3
"""VM-side Figma puller: file JSON + comments + versions + image fills +
page renders + team library metadata -> azcopy.

Runs on the transfer VM inside tmux, launched by scripts/figma_transfer.py
with ~/.config/xfer/{figma.env,dest-figma.env} sourced:
  FIGMA_TOKEN         — personal access token (client-made, Full/Dev seat)
  AZURE_DEST_URL      — https://ACCT.blob.core.windows.net/<cont>/<prefix>
  AZURE_DEST_SAS      — racwl container SAS

Why a VM at all: Figma's Tier-1 rate limit (file JSON, node JSON and image
renders all share ONE bucket, capped at 20/min even on Enterprise) makes a
real workspace a multi-hour-to-multi-day walk — exactly the job that wants
tmux detachment and per-unit durability rather than a laptop process. The
corpus majority (document JSON) must be token-fetched and staged regardless,
so one transport carries everything. Note the vimeo/zoom server-side-copy
transport IS structurally available for the presigned CDN asset URLs — it is
deliberately not used, so verify has one story instead of two.

The token never appears in argv or a log line: it is read from this
process's environment only, and goes out on exactly ONE header to
api.figma.com — never to the presigned CDN URLs the API hands back
(cdn_download builds its requests with no auth header at all; sending a
credential to a third-party host would leak it).

classify() keys on STATUS + endpoint family — the INVERSE of zoho's
body-code-first rule, and deliberate: Figma carries no body error codes, so
the status is all there is. Do not "fix" this back to the zoho shape.
403 and 404 are treated identically on purpose (Figma does not disambiguate
no-access from missing); both are per-unit recorded skips, never fatal —
except the startup /v1/me check, where 403 means the token itself is bad or
expired (Figma answers a dead token with 403, not 401).

Resume is per-unit `.cdp-complete` markers; the one cursored unit is
`library/<team_id>` (a big org library is many Tier-3 pages), where
`.cdp-cursor.json` carries {leg, after, bytes, items} and a resume truncates
the JSONL to the last whole page. File units are bounded (a handful of
Tier-1 calls each), so a marker suffices; within a re-pulled file, fill
downloads resume by file existence and the imageRef->URL map is re-fetched
fresh — stored fill URLs expire in <=14 days, so a cached one is poison.

Stdlib-only (azcopy on PATH — bootstrap-vm.sh installs it). Import-safe on
the laptop so the pure functions (classify, plan_units, estimate_tier1,
parse_render_map, page_node_ids, resume_truncate, build_manifest,
safe_component, TierBucket) are unit-testable offline; nothing runs at
import time.

Exit codes: 0 = complete success, 1 = fatal setup error (bad/expired token /
no teams reachable / no azcopy), 2 = finished but one or more units failed
(see manifest.json).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FIGMA_API = "api.figma.com"

# Per-user per-plan caps, the post-2025-11-17 model (developers.figma.com/
# docs/rest-api/rate-limits/). ALL scripts sharing one PAT share ONE bucket
# — never run probe and transfer concurrently on the same token.
# Tier 1: GET file / GET file nodes / GET image (renders). Tier 2: comments,
# image fills, folders, versions. Tier 3: components & styles, users.
TIER_LIMITS_PER_MIN = {              # {plan: {tier: calls/min}}
    "starter":    {1: 10, 2: 25, 3: 50},
    "pro":        {1: 15, 2: 50, 3: 100},
    "org":        {1: 20, 2: 100, 3: 150},
    "enterprise": {1: 20, 2: 100, 3: 150},
}
DEFAULT_PLAN = "starter"             # honest floor until --plan says more
RATE_SAFETY = 0.9                    # client-side headroom; 429 is backstop
TIER_BY_FAMILY = {"file": 1, "nodes": 1, "render": 1,
                  "folders": 2, "comments": 2, "versions": 2, "fills": 2,
                  "library": 3, "me": 3}
LIBRARY_PAGE_SIZE = 1000             # documented max on /v1/teams/:id/*
LIBRARY_LEGS = ("components", "component_sets", "styles")
VERSIONS_MAX_PAGES = 200             # backstop on next_page URL loops
NODE_BATCH_IDS = 50                  # ids per /nodes decomposition call
RENDER_BATCH_IDS = 50                # ids per /v1/images call (batching is
                                     # free; the CALL is what Tier 1 meters)
RENDER_SCALE = 1                     # documented range 0.01-4
FILL_WORKERS = 4                     # CDN pool — the CDN throttles
FILL_WORKERS_CAP = 8                 # separately from the API tiers
CDN_RETRIES = 4
API_RETRIES = 4
RATE_SLEEP_MAX = 900                 # a Retry-After past this on a Full/Dev
                                     # seat is anomalous — fail the unit and
                                     # name the wall (Figma has no daily
                                     # credit budget; the per-minute limit
                                     # IS the clock)
SYSTEMIC_BREAKER = 5                 # first N units all failing = systemic
STREAM_CHUNK = 1 << 20

# Only the walk is unconditionally required: without meta's discovery
# ledger there is nothing to plan. One 403 file among 10,000 must degrade
# to a recorded skip (Figma does not even tell us whether it was no-access
# or deleted), never lose the other 9,999. A wholesale failure is still
# caught by SYSTEMIC_BREAKER and the startup /v1/me check.
REQUIRED_KINDS = ("meta",)

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
    """Heartbeat figma_transfer.py's status subcommand reads. Never fatal."""
    try:
        (dest / "progress.json").write_text(json.dumps(
            {"source": "figma", "phase": phase, "done": done,
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
    library walk at a bogus offset."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def safe_component(name: str, limit: int = 120) -> str:
    """PURE. Blob-safe, deterministic path component. Callers put the Figma
    id (file key, node id, imageRef) FIRST and this second, so two files
    sharing a name never collide and the name is identical across runs
    (resume keys on the exact name — the zoom rule; Figma file names are
    mutable, keys are not)."""
    out = []
    for ch in str(name or ""):
        out.append(ch if (ch.isascii() and (ch.isalnum() or ch in "._-"))
                   else "_")
    s = "".join(out).strip("._-") or "unnamed"
    return s[:limit]


# ── pure helpers (unit-testable offline) ─────────────────────────────────────

def classify(status: int, family: str, required: bool) -> str:
    """PURE. A table, not judgment.

    THE STATUS + ENDPOINT FAMILY DECIDE — the inverse of zoho's body-code-
    first rule, because Figma carries no body error codes. Do not port the
    zoho shape back. Two Figma-specific rows:
      - 403 and 404 are IDENTICAL on purpose: Figma does not disambiguate
        no-access from missing, so both are recorded skips
        ("no-access-or-missing") on optional units. The bad-token case
        (Figma answers a dead PAT with 403, not 401) is caught by the
        startup /v1/me check before any unit runs.
      - 400 and 5xx on the file/nodes family mean "too large / render
        timeout" (documented: 400 "requested resources are too large",
        500 "most commonly very large image render requests") and get the
        `decompose` verdict — retry with depth=1 + per-node fetches.
    429 never reaches here (the request layer sleeps Retry-After itself).
    ONE body-message exception lives OUTSIDE this table: _pull_document
    short-circuits 400 "File type not supported by this endpoint"
    (a Slides/Buzz deck — /v1/files serves Design+FigJam only) to
    UnsupportedFileType before classify is consulted, because that 400
    refuses identically at any depth and must not trigger decompose.
    """
    if status == 429:
        return "sleep"
    if status in (403, 404):
        return "fatal" if required else "skip"
    if status == 400:
        return "decompose" if family in ("file", "nodes") \
            else ("fatal" if required else "skip")
    if status >= 500:
        return "decompose" if family == "file" else "retry"
    return "fatal" if required else "skip"


def plan_units(rows: list, team_ids: list, limit: int = 0,
               only: str | None = None,
               include_library: bool = True) -> list:
    """PURE. Ordered unit list from the meta ledger: library units first
    (fast Tier-3 metadata), then one unit per file, ordered by
    (team, folder_path, key) so the walk order is deterministic.

    The folder path is deliberately NOT part of the unit key — folder names
    are mutable and a rename between passes would orphan every marker under
    it. The path lives in the ledger and each unit's file.json instead.
    """
    units: list[tuple[str, object]] = []
    if include_library:
        for team in team_ids:
            units.append(("library", team))
    file_units = []
    for row in sorted(rows, key=lambda r: (str(r.get("team_id")),
                                           str(r.get("folder_path")),
                                           str(r.get("key")))):
        file_units.append(("file", row))
    if limit:
        file_units = file_units[:limit]
    units.extend(file_units)
    if only:
        def _match(kind, payload):
            label = unit_label(kind, payload)
            if label == only:
                return True
            return kind == "file" and f"/{only}__" in f"/{label}"
        units = [(k, p) for k, p in units if _match(k, p)]
    return units


def unit_label(kind: str, payload) -> str:
    """PURE. The unit's label doubles as its directory under the staging
    root, so the container tree mirrors staging exactly. File key FIRST,
    safe name second (renames never break resume)."""
    if kind == "meta":
        return "meta"
    if kind == "library":
        return f"library/{safe_component(str(payload))}"
    row = payload
    return (f"files/{safe_component(str(row.get('team_id')))}/"
            f"{row.get('key')}__{safe_component(row.get('name'))}")


def estimate_tier1(files: int, branches: int, render_pages: bool,
                   pages_per_file: float = 2.0) -> dict:
    """PURE. The honest pre-run number: Tier-1 calls and wall-clock per
    plan. One document call per file and per branch; renders add roughly
    one batched call per file (RENDER_BATCH_IDS ids/call at ~pages_per_file
    pages each). Decomposition of oversized files adds calls that are
    unknowable pre-run — say so, do not silently absorb it. NO byte
    estimate exists anywhere in this: Figma publishes no sizes."""
    calls = files + branches
    if render_pages:
        batches = int(-(-pages_per_file // RENDER_BATCH_IDS))  # ceil
        calls += files * max(1, batches)
    hours = {}
    for plan, tiers in TIER_LIMITS_PER_MIN.items():
        per_min = tiers[1] * RATE_SAFETY
        hours[plan] = round(calls / per_min / 60.0, 1) if per_min else None
    return {"tier1_calls": calls, "hours_by_plan": hours,
            "assumption": (f"~{pages_per_file} rendered pages/file; "
                           "oversized-file decomposition adds calls that "
                           "cannot be known pre-run")}


def page_node_ids(document: dict) -> list[str]:
    """PURE. Top-level canvas (page) node ids from a file's document tree —
    what one PNG render per page is keyed on. Works on a full document or a
    depth=1 shallow one (pages are always the document's children)."""
    doc = (document or {}).get("document") or {}
    out = []
    for child in doc.get("children") or []:
        if isinstance(child, dict) and child.get("type") == "CANVAS" \
                and child.get("id"):
            out.append(str(child["id"]))
    return out


def parse_render_map(payload: dict) -> tuple[dict, list]:
    """PURE. A 200 from GET /v1/images is NOT success per node: the map is
    guaranteed to carry every requested id, and a null value means that
    node's render FAILED. Split them so nulls are recorded, never counted
    as delivered."""
    images = (payload or {}).get("images") or {}
    ok = {k: v for k, v in images.items() if v}
    nulls = [k for k, v in images.items() if not v]
    return ok, nulls


def fill_ext(content_type: str) -> str:
    """PURE. Extension for a downloaded image fill, from the CDN's
    Content-Type (fill URLs carry no extension). Unknown types keep no
    extension rather than guessing."""
    return EXT_BY_CONTENT_TYPE.get((content_type or "").split(";")[0]
                                   .strip().lower(), "")


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


def build_manifest(team_ids: list, plan: str, context: dict,
                   started_utc: str, finished_utc: str, api_calls: int,
                   results: list) -> dict:
    """PURE. verify's authority. failed_units and skipped_units are strictly
    separate: a skip is deliberate and never a failure. decomposed files are
    informational — pulled complete, just shaped as depth-1 + per-node
    JSON."""
    failed = [r["unit"] for r in results if r.get("status") == "failed"]
    skipped = [{"unit": r["unit"], "reason": r.get("reason"),
                "detail": r.get("detail")}
               for r in results if r.get("status") == "skipped"]
    decomposed = [r["unit"] for r in results if r.get("decomposed")]
    total = sum(int(r.get("bytes") or 0) for r in results
                if r.get("status") in ("ok", "skipped-complete"))
    return {
        "source": "figma",
        "team_ids": team_ids,
        "plan": plan,
        "puller_version": 1,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "context": context,
        "unit_count": len(results),
        "total_staged_bytes": total,
        "api_calls": api_calls,
        "failed_units": failed,
        "skipped_units": skipped,
        "decomposed_files": decomposed,
        "fill_errors": sum(int(r.get("fill_errors") or 0) for r in results),
        "render_nulls": sum(int(r.get("render_nulls") or 0)
                            for r in results),
        "results": results,
    }


class TierBucket:
    """Client-side token bucket per rate tier, paced at RATE_SAFETY x the
    plan's DOCUMENTED per-minute cap — proactive pacing, a deliberate
    deviation from zoho's react-to-429 shape, because Figma publishes the
    numbers. 429 stays the backstop: on_429 honors Retry-After EXACTLY,
    drains the tier's bucket, and downgrades the assumed plan (downward
    only) when the X-Figma-Plan-Tier header says we assumed too high.

    now_fn/sleep_fn are injectable so the tests can drive a fake clock.
    API calls are single-threaded (only CDN downloads pool), so no lock.
    """

    def __init__(self, plan: str, now_fn=time.monotonic,
                 sleep_fn=time.sleep):
        self.plan = plan if plan in TIER_LIMITS_PER_MIN else DEFAULT_PLAN
        self._now = now_fn
        self._sleep = sleep_fn
        self._tokens: dict[int, float] = {}
        self._last: dict[int, float] = {}
        self.pace_sleeps = 0
        self.hits_429 = 0

    def _rate(self, tier: int) -> float:
        return TIER_LIMITS_PER_MIN[self.plan][tier] * RATE_SAFETY / 60.0

    def _cap(self, tier: int) -> float:
        return max(1.0, TIER_LIMITS_PER_MIN[self.plan][tier] * RATE_SAFETY)

    def acquire(self, tier: int) -> None:
        while True:
            now = self._now()
            tok = self._tokens.get(tier, self._cap(tier))
            last = self._last.get(tier)
            if last is not None:
                tok = min(self._cap(tier), tok + (now - last)
                          * self._rate(tier))
            self._tokens[tier] = tok
            self._last[tier] = now
            if tok >= 1.0:
                self._tokens[tier] = tok - 1.0
                return
            self.pace_sleeps += 1
            self._sleep((1.0 - tok) / self._rate(tier))

    def on_429(self, tier: int, retry_after: int,
               plan_header: str = "") -> None:
        p = (plan_header or "").strip().lower()
        if p in TIER_LIMITS_PER_MIN \
                and TIER_LIMITS_PER_MIN[p][1] \
                < TIER_LIMITS_PER_MIN[self.plan][1]:
            log(f"plan header says {p!r} — downgrading pacing from "
                f"{self.plan!r}")
            self.plan = p
        self.hits_429 += 1
        self._tokens[tier] = 0.0
        wait = retry_after if retry_after and retry_after > 0 else 60
        log(f"rate-limited; sleeping {wait}s")
        self._sleep(wait)
        self._last[tier] = self._now()


# ── REST client ──────────────────────────────────────────────────────────────

class FigmaAPIError(Exception):
    def __init__(self, status: int, family: str, msg: str):
        super().__init__(msg)
        self.status = status
        self.family = family


class UnsupportedFileType(FigmaAPIError):
    """400 'File type not supported by this endpoint' — /v1/files serves
    Design + FigJam only; Slides/Buzz/etc decks refuse identically at any
    depth, so decomposition cannot help. A deliberate skip, never a
    failure (seen live: 3 Slides decks on wallaroo-media, 2026-08).
    Subclasses FigmaAPIError so branch handling stays unchanged."""


class FigmaAPI:
    """One client for the whole pull. Counts its own calls (the manifest's
    api_calls). The token appears on exactly one header, built in exactly
    one place — _headers() — and never on a CDN request."""

    def __init__(self, token: str, bucket: TierBucket,
                 rate_sleep_max: int = RATE_SLEEP_MAX):
        self._token = token
        self.bucket = bucket
        self.calls = 0
        self.rate_sleep_max = rate_sleep_max

    def _headers(self) -> dict:
        return {"X-Figma-Token": self._token,
                "Accept": "application/json",
                "User-Agent": "cdp-harness-figma-transfer/1.0"}

    def get_json(self, family: str, path: str,
                 params: dict | None = None,
                 absolute_url: str | None = None):
        """One GET -> parsed JSON. 429 honors Retry-After via the bucket;
        5xx retries with backoff; everything else raises for classify().
        absolute_url is for versions' next_page URLs — followed verbatim,
        but ONLY when the host is api.figma.com (the token must never ride
        to any other host)."""
        tier = TIER_BY_FAMILY[family]
        if absolute_url:
            host = urllib.parse.urlparse(absolute_url).netloc
            if host != FIGMA_API:
                raise FigmaAPIError(
                    0, family, f"refusing to follow pagination off-host "
                               f"({host!r}) — the token stays on {FIGMA_API}")
            # Figma emits next_page URLs with RAW spaces in query values
            # (versions' after=<timestamp> — seen live on wallaroo-media,
            # 2026-08); http.client rejects any URL with control chars, so
            # re-encode path+query, leaving existing %-escapes alone.
            parts = urllib.parse.urlsplit(absolute_url)
            url = urllib.parse.urlunsplit((
                parts.scheme, parts.netloc,
                urllib.parse.quote(parts.path, safe="/%:@"),
                urllib.parse.quote(parts.query, safe="=&%+:@/?~.,-_"),
                ""))
        else:
            url = f"https://{FIGMA_API}{path}"
            if params:
                url += "?" + urllib.parse.urlencode(params)
        last: Exception | None = None
        for attempt in range(1, API_RETRIES + 1):
            self.bucket.acquire(tier)
            req = urllib.request.Request(url, headers=self._headers())
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=180) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429:
                    hdrs = e.headers or {}
                    limit_type = (hdrs.get("X-Figma-Rate-Limit-Type")
                                  or "").strip().lower()
                    if limit_type == "low":
                        # the monthly View/Collab budget — 6 Tier-1 calls a
                        # MONTH. Sleeping cannot help; the token owner needs
                        # a Full or Dev seat. A seat conversation, not a
                        # retry.
                        raise SystemExit(
                            "FATAL: 429 with X-Figma-Rate-Limit-Type=low — "
                            "the token owner holds a View/Collab seat, "
                            "whose Tier-1 budget is 6 calls per MONTH. The "
                            "client must issue the token from a Full or "
                            "Dev seat; retrying cannot help.")
                    ra = (hdrs.get("Retry-After") or "").strip()
                    wait = int(ra) if ra.isdigit() else 0
                    if wait > self.rate_sleep_max:
                        raise FigmaAPIError(
                            429, family,
                            f"rate limit wants {wait}s, past the "
                            f"{self.rate_sleep_max}s cap — anomalous for a "
                            "Full/Dev seat (reason=rate-wall)")
                    self.bucket.on_429(
                        tier, wait, hdrs.get("X-Figma-Plan-Tier") or "")
                    continue
                if e.code >= 500:
                    if family == "file":
                        break  # -> classify() says decompose; don't burn
                               # three more Tier-1 calls on a monster file
                    time.sleep(2 ** attempt)
                    continue
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001 - diagnosis must not fail
                    pass
                raise FigmaAPIError(
                    e.code, family, f"HTTP {e.code} on {path or url}: "
                                    f"{detail}")
            except (urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException) as e:
                # HTTPException covers http.client.InvalidURL: urllib does
                # NOT wrap it in URLError, and uncaught it kills the whole
                # pass instead of failing one unit.
                last = e
                time.sleep(2 ** attempt)
        status = getattr(last, "code", 0) or 0
        raise FigmaAPIError(status, family,
                            f"GET {path or url} failed after retries: "
                            f"{last}")


def cdn_download(url: str, out_base: Path) -> tuple[int, Path]:
    """One presigned CDN asset -> disk, streamed in chunks, NO auth header
    of any kind (the URL's signature is the credential; adding ours would
    leak it to a third-party host). Returns (bytes, final path) — the
    extension comes from the CDN's Content-Type since fill URLs carry none.
    The CDN throttles separately from the API tiers, so this path never
    touches the TierBucket."""
    last: Exception | None = None
    for attempt in range(1, CDN_RETRIES + 1):
        req = urllib.request.Request(url)  # deliberately header-free
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                ext = fill_ext(resp.headers.get("Content-Type") or "")
                out_path = out_base.with_name(out_base.name + ext)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = out_path.with_suffix(out_path.suffix + ".part")
                written = 0
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = resp.read(STREAM_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                os.replace(tmp, out_path)
                return written, out_path
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError) as e:
            last = e
            time.sleep(min(2 ** attempt, 30))
    raise OSError(f"CDN download failed after retries: {last}")


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


def _handle_unit_error(unit: str, kind: str, e: FigmaAPIError) -> dict:
    """One place where classify() decides a unit's fate, so the required-vs-
    optional split cannot drift into per-call judgment."""
    required = kind in REQUIRED_KINDS
    verdict = classify(e.status, e.family, required)
    if verdict == "fatal":
        raise SystemExit(
            f"FATAL: {unit} ({kind}) failed unrecoverably: {e}. "
            + ("403/404 on the required walk means the token cannot see the "
               "named team(s) — check the team ids against the client's "
               "URLs, or the token's scopes/owner."
               if e.status in (403, 404) else ""))
    if verdict == "skip":
        return _result(unit, kind, "skipped",
                       reason="no-access-or-missing"
                       if e.status in (403, 404) else "api-refused",
                       detail=str(e))
    return _result(unit, kind, "failed",
                   reason="rate-wall" if "rate-wall" in str(e)
                   else "api-error", detail=str(e))


# ── the walk (meta unit) ─────────────────────────────────────────────────────

def walk_team(api: FigmaAPI, team_id: str) -> tuple[list, list, str]:
    """(folders, files, walk_api) for one team. /v2 folders first (the
    current API; nested subfolders exist so the walk recurses); the
    deprecated /v1 projects pair only as a recorded fallback — new PATs are
    issued without projects:read, so v1 can 403 where v2 works. Neither
    listing paginates (documented full-list responses)."""
    folders: list[dict] = []
    files: list[dict] = []
    try:
        top = api.get_json("folders", f"/v2/teams/{team_id}/folders") or {}
        queue = [(f.get("id"), safe_component(f.get("name") or ""), 0)
                 for f in top.get("folders") or [] if f.get("id")]
        seen = set()
        while queue:
            fid, path, depth = queue.pop(0)
            if fid in seen or depth > 20:
                continue
            seen.add(fid)
            folders.append({"team_id": team_id, "folder_id": fid,
                            "folder_path": path, "depth": depth})
            subs = api.get_json(
                "folders", f"/v2/folders/{fid}/folders") or {}
            for sub in subs.get("folders") or []:
                if sub.get("id"):
                    queue.append((sub["id"],
                                  f"{path}/{safe_component(sub.get('name') or '')}",
                                  depth + 1))
            listing = api.get_json(
                "folders", f"/v2/folders/{fid}/files",
                {"branch_data": "true"}) or {}
            for fl in listing.get("files") or []:
                if not fl.get("key"):
                    continue
                files.append({
                    "team_id": team_id, "folder_id": fid,
                    "folder_path": path, "key": fl["key"],
                    "name": fl.get("name"),
                    "last_modified": fl.get("last_modified"),
                    "branches": [{"key": b.get("key"), "name": b.get("name")}
                                 for b in fl.get("branches") or []
                                 if b.get("key")]})
        return folders, files, "v2"
    except FigmaAPIError as e:
        if e.status not in (403, 404):
            raise
        log(f"meta: /v2 folders walk refused for team {team_id} "
            f"({e.status}) — trying the deprecated /v1 projects pair")
    projects = api.get_json(
        "folders", f"/v1/teams/{team_id}/projects") or {}
    for proj in projects.get("projects") or []:
        pid = proj.get("id")
        if not pid:
            continue
        pname = safe_component(proj.get("name") or "")
        folders.append({"team_id": team_id, "folder_id": str(pid),
                        "folder_path": pname, "depth": 0})
        listing = api.get_json(
            "folders", f"/v1/projects/{pid}/files",
            {"branch_data": "true"}) or {}
        for fl in listing.get("files") or []:
            if not fl.get("key"):
                continue
            files.append({
                "team_id": team_id, "folder_id": str(pid),
                "folder_path": pname, "key": fl["key"],
                "name": fl.get("name"),
                "last_modified": fl.get("last_modified"),
                "branches": [{"key": b.get("key"), "name": b.get("name")}
                             for b in fl.get("branches") or []
                             if b.get("key")]})
    return folders, files, "v1-deprecated"


def pull_meta(api: FigmaAPI, dest: Path, team_ids: list,
              refresh: bool) -> tuple[dict, list]:
    """The discovery ledger — the one REQUIRED unit. Records what the
    listings actually contained (listings carry no editor type, so
    FigJam/Slides presence is discovered per file later, never assumed).
    A team the token cannot see is recorded as unreachable; only ZERO
    reachable teams is fatal — there is no team-listing API, so a team the
    client forgot to name is invisible and the census must be read back to
    them."""
    unit = "meta"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        rows = [json.loads(ln) for ln in
                (d / "files.jsonl").read_text().splitlines() if ln.strip()]
        return _result(unit, "meta", "skipped-complete",
                       bytes=dir_size(d), files=len(rows)), rows
    if refresh:
        clear_unit(d)
    teams_out: list[dict] = []
    all_rows: list[dict] = []
    folders_fh = open(d / "folders.jsonl", "w", encoding="utf-8")
    files_fh = open(d / "files.jsonl", "w", encoding="utf-8")
    last_err: FigmaAPIError | None = None
    try:
        for team_id in team_ids:
            try:
                folders, files, walk_api = walk_team(api, team_id)
            except FigmaAPIError as e:
                last_err = e
                teams_out.append({"team_id": team_id, "state": "unreachable",
                                  "detail": str(e)})
                log(f"meta: team {team_id} unreachable ({e.status})")
                continue
            teams_out.append({"team_id": team_id, "state": "ok",
                              "walk_api": walk_api,
                              "folders": len(folders), "files": len(files)})
            for f in folders:
                folders_fh.write(json.dumps(f, ensure_ascii=False) + "\n")
            for f in files:
                files_fh.write(json.dumps(f, ensure_ascii=False) + "\n")
            all_rows.extend(files)
            log(f"meta: team {team_id}: {len(folders)} folders, "
                f"{len(files)} files ({walk_api})")
    finally:
        folders_fh.close()
        files_fh.close()
    atomic_write_json(d / "teams.json", {"teams": teams_out})
    if not any(t["state"] == "ok" for t in teams_out):
        return _handle_unit_error(unit, "meta",
                                  last_err or FigmaAPIError(
                                      403, "folders",
                                      "no team reachable")), []
    mark_complete(d)
    branches = sum(len(r.get("branches") or []) for r in all_rows)
    return _result(unit, "meta", "ok", bytes=dir_size(d),
                   files=len(all_rows), branches=branches,
                   teams_unreachable=[t["team_id"] for t in teams_out
                                      if t["state"] != "ok"]), all_rows


# ── library units ────────────────────────────────────────────────────────────

def _library_items(payload: dict, leg: str) -> tuple[list, str | None]:
    """One page of a team library listing -> (items, after cursor)."""
    meta = (payload or {}).get("meta") or {}
    items = meta.get(leg) or (payload or {}).get(leg) or []
    cursor = meta.get("cursor") or {}
    after = cursor.get("after") if items else None
    return items, after


def pull_library(api: FigmaAPI, team_id: str, dest: Path,
                 refresh: bool) -> dict:
    """Published components / component sets / styles for one team (Tier 3,
    page_size max 1000). Unpublished assets are NOT exposed by these
    endpoints — they live only in each file's own JSON, which the file
    units already stage. The one cursored unit: a big org library is many
    pages, so `.cdp-cursor.json` carries {leg, after, bytes, items}."""
    unit = f"library/{safe_component(team_id)}"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "library", "skipped-complete",
                       bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    cursor = read_cursor(d) or {}
    start_leg = cursor.get("leg") or LIBRARY_LEGS[0]
    counts: dict[str, int] = {}
    try:
        started = False
        for leg in LIBRARY_LEGS:
            if leg == start_leg:
                started = True
            if not started:
                counts[leg] = _count_lines(d / f"{leg}.jsonl") \
                    if (d / f"{leg}.jsonl").exists() else 0
                continue
            jsonl = d / f"{leg}.jsonl"
            if leg == start_leg and cursor.get("after"):
                written = resume_truncate(jsonl, cursor)
                after = cursor.get("after")
            else:
                try:
                    jsonl.unlink()
                except OSError:
                    pass
                written, after = 0, None
            while True:
                params = {"page_size": LIBRARY_PAGE_SIZE}
                if after:
                    params["after"] = after
                payload = api.get_json(
                    "library", f"/v1/teams/{team_id}/{leg}", params) or {}
                items, nxt = _library_items(payload, leg)
                if items:
                    with open(jsonl, "a", encoding="utf-8") as fh:
                        for item in items:
                            fh.write(json.dumps(item, ensure_ascii=False)
                                     + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                    written += len(items)
                atomic_write_json(d / ".cdp-cursor.json", {
                    "leg": leg, "after": nxt,
                    "bytes": jsonl.stat().st_size if jsonl.exists() else 0,
                    "items": written})
                if not nxt or not items:
                    break
                after = nxt
            counts[leg] = written
    except FigmaAPIError as e:
        return _handle_unit_error(unit, "library", e)
    mark_complete(d)
    return _result(unit, "library", "ok", bytes=dir_size(d), **counts)


# ── file units ───────────────────────────────────────────────────────────────

def _fill_done(fills_dir: Path, ref: str) -> bool:
    """Resume check by file existence — the ext is only known post-download,
    so match on the imageRef stem."""
    if not fills_dir.is_dir():
        return False
    stem = safe_component(ref)
    for p in fills_dir.iterdir():
        if p.name == stem or p.name.startswith(stem + "."):
            return not p.name.endswith(".part")
    return False


def _pull_document(api: FigmaAPI, key: str, d: Path) -> tuple[dict, bool]:
    """(document payload, decomposed?). A monster file answers 400
    "Request timeout, try a smaller request" or 5xx — the documented
    mitigation is depth=1 (pages only) plus per-node subtree fetches, so
    the pull is still COMPLETE, just shaped differently (recorded, and
    surfaced by verify as informational)."""
    try:
        doc = api.get_json("file", f"/v1/files/{key}") or {}
        atomic_write_json(d / "document.json", doc)
        return doc, False
    except FigmaAPIError as e:
        if e.status == 400 and "not supported" in str(e).lower():
            raise UnsupportedFileType(e.status, e.family, str(e)) from None
        if classify(e.status, e.family, False) != "decompose":
            raise
        log(f"files/{key}: full document refused ({e.status}) — "
            "decomposing (depth=1 + per-node subtrees)")
    shallow = api.get_json("file", f"/v1/files/{key}", {"depth": 1}) or {}
    atomic_write_json(d / "document.json", shallow)
    pages = page_node_ids(shallow)
    nodes_dir = d / "nodes"
    nodes_dir.mkdir(exist_ok=True)
    for i in range(0, len(pages), NODE_BATCH_IDS):
        batch = pages[i:i + NODE_BATCH_IDS]
        payload = api.get_json("nodes", f"/v1/files/{key}/nodes",
                               {"ids": ",".join(batch)}) or {}
        for node_id, node in (payload.get("nodes") or {}).items():
            atomic_write_json(
                nodes_dir / f"{safe_component(node_id)}.json", node)
    return shallow, True


def _pull_fills(api: FigmaAPI, key: str, d: Path,
                workers: int) -> dict:
    """The embedded bitmaps (imageRef paints). The URL map is ALWAYS
    re-fetched fresh — fill URLs expire in <=14 days, so a stored one is
    poison; resume is by downloaded-file existence instead. Downloads pool
    on the CDN (which throttles separately) and never carry the token."""
    try:
        payload = api.get_json("fills", f"/v1/files/{key}/images") or {}
    except FigmaAPIError as e:
        if classify(e.status, e.family, False) == "skip":
            return {"listed": 0, "downloaded": 0, "errors": 0,
                    "state": f"skip:{e.status}"}
        raise
    images = ((payload.get("meta") or {}).get("images")
              or payload.get("images") or {})
    fills_dir = d / "fills"
    jobs = [(ref, url) for ref, url in images.items()
            if url and not _fill_done(fills_dir, ref)]
    manifest = {"listed": len(images),
                "already_present": len(images) - len(jobs)}
    got = errs = got_bytes = 0
    if jobs:
        workers = max(1, min(workers or FILL_WORKERS, FILL_WORKERS_CAP))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers) as pool:
            futs = {pool.submit(cdn_download, url,
                                fills_dir / safe_component(ref)): ref
                    for ref, url in jobs}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    n, _ = fut.result()
                    got += 1
                    got_bytes += n
                except Exception:  # noqa: BLE001 - counted, never fatal
                    errs += 1
    atomic_write_json(d / "fills-manifest.json",
                      {"listed": len(images), "downloaded": got,
                       "errors": errs, "bytes": got_bytes,
                       "refs": sorted(images.keys())})
    return {"listed": len(images), "downloaded": got, "errors": errs,
            "state": "ok"}


def _pull_renders(api: FigmaAPI, key: str, doc: dict, d: Path) -> dict:
    """One PNG per page (top-level canvas), batched RENDER_BATCH_IDS ids
    per Tier-1 call. A 200 can carry null per-node values (render failed)
    — nulls are retried once as their own batch, then recorded."""
    pages = page_node_ids(doc)
    if not pages:
        return {"pages": 0, "rendered": 0, "nulls": 0}
    renders_dir = d / "renders"
    todo = [p for p in pages
            if not (renders_dir / f"{safe_component(p)}.png").exists()]
    rendered = len(pages) - len(todo)
    nulls: list[str] = []
    urls: dict[str, str] = {}
    for i in range(0, len(todo), RENDER_BATCH_IDS):
        batch = todo[i:i + RENDER_BATCH_IDS]
        try:
            payload = api.get_json(
                "render", f"/v1/images/{key}",
                {"ids": ",".join(batch), "format": "png",
                 "scale": RENDER_SCALE}) or {}
        except FigmaAPIError as e:
            if classify(e.status, e.family, False) in ("skip", "decompose"):
                nulls.extend(batch)
                continue
            raise
        ok, bad = parse_render_map(payload)
        urls.update(ok)
        nulls.extend(bad)
    if nulls:  # one retry for the failed nodes, then record what remains
        retry, nulls = nulls, []
        for i in range(0, len(retry), RENDER_BATCH_IDS):
            batch = retry[i:i + RENDER_BATCH_IDS]
            try:
                payload = api.get_json(
                    "render", f"/v1/images/{key}",
                    {"ids": ",".join(batch), "format": "png",
                     "scale": RENDER_SCALE}) or {}
                ok, bad = parse_render_map(payload)
                urls.update(ok)
                nulls.extend(bad)
            except FigmaAPIError:
                nulls.extend(batch)
    errs = 0
    for node_id, url in urls.items():
        try:
            cdn_download(url, renders_dir / safe_component(node_id))
            rendered += 1
        except OSError:
            errs += 1
    return {"pages": len(pages), "rendered": rendered,
            "nulls": len(nulls) + errs,
            "null_nodes": sorted(nulls)[:50]}


def pull_file(api: FigmaAPI, row: dict, dest: Path, args) -> dict:
    """One Figma file = one unit. document.json is the essence — its
    failure fails the unit. Sub-resources (comments, versions, fills,
    renders, branches) that the API refuses with 403/404 are recorded in
    file.json and counted, never unit failures — a re-run would refuse
    identically."""
    key = row["key"]
    unit = unit_label("file", row)
    d = unit_dir(dest, unit)
    if is_complete(d) and not args.refresh:
        return _result(unit, "file", "skipped-complete", bytes=dir_size(d))
    if args.refresh:
        clear_unit(d)
    meta: dict = {"key": key, "name": row.get("name"),
                  "team_id": row.get("team_id"),
                  "folder_path": row.get("folder_path"),
                  "last_modified": row.get("last_modified")}
    try:
        doc, decomposed = _pull_document(api, key, d)
    except UnsupportedFileType as e:
        mark_skipped(d, "unsupported-file-type", str(e))
        return _result(unit, "file", "skipped",
                       reason="unsupported-file-type", detail=str(e))
    except FigmaAPIError as e:
        return _handle_unit_error(unit, "file", e)
    meta["editor_type"] = doc.get("editorType")
    meta["version"] = doc.get("version")
    meta["decomposed"] = decomposed
    fill_errors = render_nulls = 0
    try:
        for leg, family, path in (
                ("comments", "comments", f"/v1/files/{key}/comments"),):
            if args.no_comments:
                meta[leg] = "disabled"
                continue
            try:
                atomic_write_json(d / f"{leg}.json",
                                  api.get_json(family, path))
                meta[leg] = "ok"
            except FigmaAPIError as e:
                if classify(e.status, e.family, False) != "skip":
                    raise
                meta[leg] = f"skip:{e.status}"
        if args.no_versions:
            meta["versions"] = "disabled"
        else:
            meta["versions"] = _pull_versions(api, key, d)
        if args.no_fills:
            meta["fills"] = "disabled"
        else:
            fills = _pull_fills(api, key, d, args.fill_workers)
            meta["fills"] = fills
            fill_errors = fills.get("errors") or 0
        if args.render_pages:
            renders = _pull_renders(api, key, doc, d)
            meta["renders"] = renders
            render_nulls = renders.get("nulls") or 0
        else:
            meta["renders"] = "disabled"
        meta["branches"] = _pull_branches(api, row, d)
    except FigmaAPIError as e:
        return _handle_unit_error(unit, "file", e)
    atomic_write_json(d / "file.json", meta)
    mark_complete(d)
    return _result(unit, "file", "ok", bytes=dir_size(d),
                   editor_type=meta.get("editor_type"),
                   decomposed=decomposed, fill_errors=fill_errors,
                   render_nulls=render_nulls,
                   branches=len(row.get("branches") or []))


def _pull_versions(api: FigmaAPI, key: str, d: Path):
    """Version-history LIST only (per-version file JSON is out of scope —
    it would multiply the Tier-1 walk). Paginated by an opaque next_page
    URL followed verbatim (host-checked); version lists are small, so no
    cursor — a resume re-walks, capped at VERSIONS_MAX_PAGES."""
    versions: list = []
    url = None
    try:
        for _ in range(VERSIONS_MAX_PAGES):
            if url:
                payload = api.get_json("versions", "",
                                       absolute_url=url) or {}
            else:
                payload = api.get_json(
                    "versions", f"/v1/files/{key}/versions") or {}
            versions.extend(payload.get("versions") or [])
            url = (payload.get("pagination") or {}).get("next_page")
            if not url:
                break
    except FigmaAPIError as e:
        if classify(e.status, e.family, False) != "skip":
            raise
        return f"skip:{e.status}"
    atomic_write_json(d / "versions.json", {"versions": versions})
    return {"count": len(versions)}


def _pull_branches(api: FigmaAPI, row: dict, d: Path):
    """Branch keys are ordinary file keys; they stage INSIDE the parent
    unit (one marker covers file + branches) as the node tree only. A
    refused branch is recorded, never a unit failure."""
    out = {}
    for br in row.get("branches") or []:
        bkey = br.get("key")
        if not bkey:
            continue
        bdir = d / "branches" / safe_component(bkey)
        bdir.mkdir(parents=True, exist_ok=True)
        try:
            bdoc, decomposed = _pull_document(api, bkey, bdir)
            out[bkey] = {"state": "ok", "name": br.get("name"),
                         "decomposed": decomposed}
        except FigmaAPIError as e:
            if classify(e.status, e.family, False) not in ("skip",
                                                           "decompose"):
                raise
            out[bkey] = {"state": f"skip:{e.status}", "name": br.get("name")}
    return out


# ── upload ───────────────────────────────────────────────────────────────────

def upload_run_metadata(dest: Path, dest_url: str, sas: str) -> bool:
    """manifest.json / progress.json with overwrite ALLOWED.

    Everything else rides --overwrite=false, but these are OUR run
    bookkeeping, not client corpus data — and manifest.json is exactly what
    verify treats as authoritative. Uploading it no-overwrite means a
    re-run's manifest is silently skipped and verify certifies against the
    FIRST pass forever (the github pilot-poisons-verify bug, observed live;
    zoho fixed it and this ships the fix from day one)."""
    if shutil.which("azcopy") is None:
        return False
    ok = True
    for name in ("manifest.json", "progress.json"):
        src = dest / name
        if not src.exists():
            continue
        proc = subprocess.run(
            ["azcopy", "copy", str(src), f"{dest_url}/{name}?{sas}",
             "--overwrite=true", "--log-level", "ERROR"],
            capture_output=True, text=True)
        good = proc.returncode == 0
        log(f"upload: {name} {'DONE' if good else 'FAILED'} "
            "(overwrite=true)")
        ok = ok and good
    # Per-unit control files are bookkeeping too: a --refresh rewrites a
    # cursor and no-overwrite would keep the old one, so verify would
    # report a phantom short_upload on a complete unit.
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


def upload(dest: Path, dest_url: str, sas: str, subpath: str = "",
           overwrite: bool = False) -> bool:
    """azcopy the staged tree (or one unit of it) to the container prefix.

    --overwrite=false is the write invariant on this path: client-side
    no-overwrite, the same choice as the s3/github/zoho paths — NOT the
    API-enforced If-None-Match of the local pulls. Say it that way when
    describing the guarantee. SAS never printed; output is scanned, not
    echoed raw.

    `overwrite=True` is used for exactly one case: a unit re-pulled under
    --refresh (which already means "discard the previous pull of this
    unit"), so a stale earlier export cannot strand as a phantom
    short_upload. Only blobs this ingest itself wrote, under its own dest
    prefix — client-uploaded data is never in scope.
    """
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


def _record_unit(results: list, res: dict, state: dict, dest: Path,
                 dest_url: str, sas: str, skip_upload: bool,
                 refresh: bool = False) -> None:
    """Append a unit result, run the systemic breaker, and upload the unit
    as soon as it is whole — a mid-run VM loss costs one file, not the
    pass. The breaker's message names the figma-specific systemic cause:
    PATs expire (<=90 days), so a run that was succeeding and then goes
    all-403 mid-pass is token expiry, not scopes."""
    results.append(res)
    if res.get("status") in ("ok", "skipped-complete"):
        state["ok"] += 1
    elif res.get("status") == "failed":
        state["failed"] += 1
        if state["ok"] == 0 and state["failed"] >= SYSTEMIC_BREAKER:
            raise SystemExit(
                f"FATAL: the first {SYSTEMIC_BREAKER} units all failed with "
                "zero successes — systemic (expired/revoked token, missing "
                "scopes, network, or SAS), aborting. Figma PATs expire "
                "within ~90 days; a previously working run going all-403 "
                f"is expiry. Last error: {res.get('detail')}")
    if res.get("status") == "ok" and not skip_upload:
        upload(dest, dest_url, sas, res["unit"], overwrite=refresh)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="VM-side Figma puller (figma-azure-transfer)")
    ap.add_argument("--dest", required=True,
                    help="staging dir on the VM, e.g. ~/xfer-figma/dest")
    ap.add_argument("--team-ids", dest="team_ids", required=True,
                    help="comma-separated Figma team ids (from the client's "
                         "file-browser URLs — no API lists teams)")
    ap.add_argument("--plan", default=os.environ.get("FIGMA_PLAN", ""),
                    help="starter|pro|org|enterprise — sets the pacing "
                         "schedule (default: starter, the honest floor)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N file units (pilot)")
    ap.add_argument("--only", default=None,
                    help="only this one unit (a unit label, or a bare "
                         "file key)")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--skip-upload", dest="skip_upload", action="store_true")
    ap.add_argument("--no-render-pages", dest="render_pages",
                    action="store_false", default=True,
                    help="skip the per-page PNG renders (they share the "
                         "Tier-1 bucket with document pulls)")
    ap.add_argument("--no-fills", dest="no_fills", action="store_true",
                    help="skip image-fill downloads")
    ap.add_argument("--no-comments", dest="no_comments", action="store_true")
    ap.add_argument("--no-versions", dest="no_versions", action="store_true")
    ap.add_argument("--no-library", dest="no_library", action="store_true",
                    help="skip the team library units")
    ap.add_argument("--fill-workers", dest="fill_workers", type=int,
                    default=FILL_WORKERS)
    ap.add_argument("--rate-sleep-max", dest="rate_sleep_max", type=int,
                    default=RATE_SLEEP_MAX)
    args = ap.parse_args()

    token = os.environ.get("FIGMA_TOKEN", "").strip()
    if not token:
        log("FATAL: FIGMA_TOKEN not in environment (figma.env not sourced?)")
        return 1
    dest_url = os.environ.get("AZURE_DEST_URL", "").strip()
    dest_sas = os.environ.get("AZURE_DEST_SAS", "").strip()
    if not args.skip_upload and not (dest_url and dest_sas):
        log("FATAL: AZURE_DEST_URL/AZURE_DEST_SAS not in environment "
            "(dest-figma.env not sourced?)")
        return 1
    team_ids = [t.strip() for t in args.team_ids.split(",") if t.strip()]
    if not team_ids:
        log("FATAL: --team-ids is empty")
        return 1

    dest = Path(os.path.expanduser(args.dest))
    dest.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()

    bucket = TierBucket(args.plan.strip().lower() or DEFAULT_PLAN)
    api = FigmaAPI(token, bucket, args.rate_sleep_max)
    try:
        me = api.get_json("me", "/v1/me") or {}
    except FigmaAPIError as e:
        # Figma answers a dead token with 403, not 401 — and it does not
        # say whether the PAT was never valid or has expired (<=90 days).
        # Both are a re-issue conversation with the client.
        log(f"FATAL: /v1/me refused ({e.status}) — the token is invalid or "
            "expired (Figma uses 403 for both and does not say which). "
            "The client must re-issue the PAT; re-run write-creds.")
        return 1
    log(f"token ok — acting as {me.get('handle') or me.get('email') or '?'}"
        f", pacing as plan {bucket.plan!r}")

    results: list = []
    state = {"ok": 0, "failed": 0}
    write_progress(dest, "walk", 0, 0, "meta")
    meta_res, rows = pull_meta(api, dest, team_ids, args.refresh)
    _record_unit(results, meta_res, state, dest, dest_url, dest_sas,
                 args.skip_upload, args.refresh)
    if meta_res.get("status") not in ("ok", "skipped-complete"):
        log("FATAL: the meta walk did not produce a ledger — nothing to "
            "plan. See teams.json for per-team detail.")
        return 1

    units = plan_units(rows, team_ids, args.limit, args.only,
                       include_library=not args.no_library)
    est = estimate_tier1(
        sum(1 for k, _ in units if k == "file"),
        sum(len((r.get("branches") or []))
            for k, r in units if k == "file"),
        args.render_pages)
    log(f"{len(units)} units planned; ~{est['tier1_calls']} Tier-1 calls "
        f"≈ {est['hours_by_plan'].get(bucket.plan)} h at plan "
        f"{bucket.plan!r} ({est['assumption']})")

    for i, (kind, payload) in enumerate(units, 1):
        label = unit_label(kind, payload)
        write_progress(dest, "pull", i, len(units), label)
        log(f"[{i}/{len(units)}] {label}")
        if kind == "library":
            res = pull_library(api, payload, dest, args.refresh)
        else:
            res = pull_file(api, payload, dest, args)
        _record_unit(results, res, state, dest, dest_url, dest_sas,
                     args.skip_upload, args.refresh)

    editor_types: dict[str, int] = {}
    for r in results:
        et = r.get("editor_type")
        if et:
            editor_types[et] = editor_types.get(et, 0) + 1
    context = {"teams": team_ids,
               "teams_unreachable": meta_res.get("teams_unreachable") or [],
               "files_in_ledger": len(rows),
               "editor_types": editor_types,
               "render_pages": args.render_pages,
               "estimate": est}
    manifest = build_manifest(
        team_ids, bucket.plan, context, started,
        datetime.now(timezone.utc).isoformat(), api.calls, results)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    failed = manifest["failed_units"]
    log(f"SUMMARY: {len(results)} units, "
        f"{human_bytes(manifest['total_staged_bytes'])} staged, "
        f"{api.calls} api calls, {bucket.pace_sleeps} pacing sleeps, "
        f"{bucket.hits_429} 429s, {len(failed)} failed "
        f"{failed if failed else ''}")

    upload_ok = True
    if not args.skip_upload:
        # upload-what-succeeded: the final sweep is cheap because
        # --overwrite=false skips everything already landed per unit
        write_progress(dest, "upload", len(results), len(results),
                       "azcopy final sweep")
        upload_ok = upload(dest, dest_url, dest_sas,
                           overwrite=args.refresh)
        # the manifest must REPLACE any earlier pass's — verify trusts it
        upload_ok = upload_run_metadata(dest, dest_url, dest_sas) \
            and upload_ok
    write_progress(dest, "done", len(results), len(results),
                   f"{len(failed)} failed")
    return 2 if failed or not upload_ok else 0


if __name__ == "__main__":
    sys.exit(main())
