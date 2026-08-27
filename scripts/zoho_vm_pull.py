#!/usr/bin/env python3
"""VM-side Zoho puller: CRM ledger + Bulk Read ZIPs + attachments, Learn,
WorkDrive -> azcopy.

Runs on the transfer VM inside tmux, launched by scripts/zoho_transfer.py
with ~/.config/xfer/{zoho.env,dest.env} sourced:
  ZOHO_DC             — com / eu / in / com.au / jp / ca / sa / com.cn
  ZOHO_CLIENT_ID      — Self Client id
  ZOHO_CLIENT_SECRET  — Self Client secret
  ZOHO_REFRESH_TOKEN  — long-lived refresh token (the client made it)
  AZURE_DEST_URL      — https://ACCT.blob.core.windows.net/<cont>/<prefix>
  AZURE_DEST_SAS      — racwl container SAS

Why a VM at all: Zoho's download endpoints authenticate with
`Authorization: Zoho-oauthtoken <tok>`, and Azure's
`x-ms-copy-source-authorization` only speaks `Bearer` — so the vimeo/zoom
server-side-copy transport is STRUCTURALLY unavailable and attachment bytes
must be staged. Hence the github-family shape: stage here, azcopy up.

The credentials never appear in argv or a log line: they are read from this
process's environment only, and the minted access token is never printed.

CRM's authority is a REST JSON ledger per module. v8's GET /crm/v8/<Module>
requires an explicit `fields` param, so field metadata is fetched first and
every api_name is requested (chunked when a module is very wide, with the
extra chunks re-fetched per page by record id and merged — never by reusing
a page_token across different queries, which is undefined). A per-module
Bulk Read job additionally lands `{job_id}.zip` as an archival blob and its
CSV row count is recorded as a cross-check: Bulk Read EXCLUDES Notes,
Attachments, Emails and related/cross modules, so the JSON is the ledger and
the ZIP is the check, never the reverse.

Resume is per-unit `.cdp-complete` markers PLUS cursors — a CRM module can be
millions of records, so deleting and re-walking a partial one (the github
precedent) is not acceptable. `.cdp-cursor.json` carries
{page_token, bytes, records, fields_sha} and a resume truncates the JSONL to
the last whole page.

Stdlib-only (azcopy on PATH — bootstrap-vm.sh installs it). Import-safe on
the laptop so the pure functions (classify, fields_param, merge_chunks,
resume_truncate, bulk_crosscheck, build_manifest, safe_component) are
unit-testable offline; nothing runs at import time.

Exit codes: 0 = complete success, 1 = fatal setup error (bad credentials /
wrong data center / missing required scope / no azcopy), 2 = finished but one
or more units failed (see manifest.json).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PER_PAGE = 200            # API max; credits scale with CALLS, so never lower
PAGINATION_CAP = 100_000  # HARD: page_token stops at 100k records with
                          # 400 PAGINATION_LIMIT_EXCEEDED. Walking the
                          # module from BOTH ends covers up to 2x that.
FIELD_CHUNK = 50          # HARD API limit: v8 rejects >50 field names per
                          # request with 400 LIMIT_EXCEEDED
                          # (details.limit=50, param_name=fields) —
                          # verified live on song-division 2026-08-25.
                          # fields_param emits id + (chunk-1) names, so
                          # 50 lands exactly on the cap.
ID_BATCH = 100            # ids per follow-up chunk request (v8 `ids` cap)
API_RETRIES = 4
BULK_POLL_S = 30
BULK_DOWNLOAD_MIN_INTERVAL = 6.0   # documented cap: 10 downloads / minute
BULK_RESULT_TTL_H = 24             # documented: results expire after 1 day
BULK_JOB_TIMEOUT_S = 3600          # how long to WAIT for a job — a different
                                   # clock from the result TTL above: a job
                                   # that has not finished in an hour is
                                   # wedged, and the ZIP is only a
                                   # cross-check, so give up and record it
BULK_MAX_FIELDS = 200              # documented Bulk Read select-field cap
LEARN_LIMIT = 99                   # documented max
LEARN_MIN_INTERVAL = 0.1           # no documented credit model; be polite
LEARN_MAX_PAGES = 10000            # backstop: some collections ignore `limit`
LEARN_COURSE_VIEW = "all"          # NOT the API default ("learn" = only the
                                   # calling user's enrolments, which is 0 for
                                   # a Self Client admin)
TOKEN_REFRESH_MARGIN = 300
RATE_SLEEP_MAX = 3900
ATTACHMENT_WORKERS = 3
ATTACHMENT_WORKERS_CAP = 5
ATTACHMENT_BREAKER = 25            # consecutive failures that fail a module
SYSTEMIC_BREAKER = 5               # first N units all failing = systemic
STREAM_CHUNK = 1 << 20

# Only the SCHEMA is unconditionally required: without /settings/modules
# there is nothing to plan and nothing to interpret the ledger with.
#
# `records` is deliberately NOT required per-module. Zoho has modules the
# API user's profile cannot see (NO_PERMISSION) and system modules like
# Scoring_Rules__s that need scopes ZohoCRM.modules.ALL does not grant
# (OAUTH_SCOPE_MISMATCH) — both killed live runs at 21/187 and 105/191
# units (song-division, 2026-08-25). One inaccessible module out of 92 must
# degrade to a recorded skip, not lose the other 91. A WHOLESALE scope
# failure is still caught: run_crm aborts if NO record module succeeded.
REQUIRED_KINDS = ("settings",)

# Verified live against a real tenant (song-division, 2026-08-24). Zoho
# Learn's knowledge base has no documented API; these are the routes that
# actually answer with the read scopes a Self Client can hold. `manual` and
# `space` exist as routes but reject a bare GET (INVALID_METHOD) — the
# documented manuals listing hangs off a CUSTOM PORTAL, which returns
# Access Denied unless one is configured and the token may read it. All of
# it is ATTEMPTED and recorded; nothing here is assumed.
LEARN_KB_CANDIDATES = (
    ("tags", "/learn/api/v1/portal/{portal}/tag"),
    ("quizzes", "/learn/api/v1/portal/{portal}/quiz"),
    ("customportals", "/learn/api/v1/portal/{portal}/customportal"),
)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return f"{n} B"


def write_progress(dest: Path, product: str, phase: str, done: int,
                   total: int, message: str) -> None:
    """Heartbeat zoho_transfer.py's status subcommand reads. Never fatal."""
    try:
        (dest / "progress.json").write_text(json.dumps(
            {"product": product, "phase": phase, "done": done,
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
    module at a bogus offset."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def safe_component(name: str, limit: int = 120) -> str:
    """PURE. Blob-safe, deterministic path component. Callers put the Zoho id
    FIRST and this second, so two attachments sharing a filename never
    collide and the name is identical across runs (resume keys on the exact
    name — the zoom rule)."""
    out = []
    for ch in str(name or ""):
        out.append(ch if (ch.isascii() and (ch.isalnum() or ch in "._-"))
                   else "_")
    s = "".join(out).strip("._-") or "unnamed"
    return s[:limit]


# ── pure helpers (unit-testable offline) ─────────────────────────────────────

def zoho_error_code(body) -> str:
    """PURE. Zoho carries the real meaning in a body error code, not the HTTP
    status — the same 401 is INVALID_TOKEN or OAUTH_SCOPE_MISMATCH. Bodies
    arrive in at least four shapes; garbage degrades to "" rather than
    raising."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a diagnosis must never itself fail
            return ""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            return ""
    if isinstance(body, list):
        body = body[0] if body else {}
    if not isinstance(body, dict):
        return ""
    if isinstance(body.get("data"), list) and body["data"]:
        first = body["data"][0]
        if isinstance(first, dict) and first.get("code"):
            return str(first["code"])
    for key in ("code", "error"):
        val = body.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def body_failure(payload) -> str:
    """PURE. Zoho answers some failures with HTTP 200/202 and a failure BODY
    — Learn especially (`{"result":"failure","errorCode":"9001"}` for Access
    Denied, `EXTRA_PARAM_FOUND`, `INVALID_METHOD`). Verified live on
    song-division 2026-08-25, where an Access-Denied /customportal was
    recorded as "reachable" and marked complete with no data.

    Returns the error code when the body signals failure, else "". Success
    shapes are unaffected: CRM returns {"data":[...],"info":{...}} and Learn
    returns {"STATUS":"OK","DATA":[...]} — neither carries these markers.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("status", "result"):
        val = payload.get(key)
        if isinstance(val, str) and val.lower() in ("failure", "error"):
            return str(payload.get("errorCode")
                       or payload.get("code") or val)
    if payload.get("errorCode"):
        return str(payload["errorCode"])
    return ""


def classify(status: int, code: str, required: bool) -> str:
    """PURE. A table, not judgment.

    THE BODY CODE IS CHECKED FIRST, before the status. github's taxonomy is
    status-based (404 = feature disabled = skip; 403 with the rate limit
    intact = scope = fatal) and that does NOT port: Zoho sends the SAME
    condition under different statuses. NO_PERMISSION arrives as 400 on
    /settings/fields and as 403 elsewhere — keying it on 403 killed a live
    run at 21 of 187 units (song-division, 2026-08-25). If a condition has a
    code, the code decides.
    """
    # ── code first ───────────────────────────────────────────────────────
    if code == "NO_PERMISSION":
        # the API user's CRM PROFILE lacks the module — a per-module skip,
        # never fatal and never retryable, whatever status carries it
        return "skip"
    if code == "OAUTH_SCOPE_MISMATCH":
        return "fatal" if required else "skip"
    if code in ("INVALID_MODULE", "INVALID_URL_PATTERN", "INVALID_REQUEST",
                "NOT_SUPPORTED", "INVALID_DATA"):
        return "skip"
    if code == "INTERNAL_ERROR":
        return "retry"
    # ── then status ──────────────────────────────────────────────────────
    if status == 401:                       # INVALID_TOKEN
        return "remint-once-then-fatal"
    if status == 403:
        return "skip"
    if status == 404:
        return "fatal" if required else "skip"
    if status == 429:
        return "sleep"
    if status >= 500:
        return "retry"
    return "fatal" if required else "skip"


def fields_param(fields, chunk: int = FIELD_CHUNK) -> list[str]:
    """PURE. v8 makes `fields` mandatory and a wide module can exceed a safe
    URL length. Split into comma-joined chunks, each starting with `id` so
    every chunk's rows can be merged back together."""
    names = [f for f in dict.fromkeys(fields or []) if f and f != "id"]
    if not names:
        return ["id"]
    out = []
    for i in range(0, len(names), max(1, chunk - 1)):
        out.append(",".join(["id"] + names[i:i + max(1, chunk - 1)]))
    return out


def merge_chunks(pages) -> list[dict]:
    """PURE. Merge per-chunk record lists by `id`, preserving the order of
    the first (pagination-driving) chunk. A record present only in a later
    chunk is still emitted — dropping it silently is exactly the failure
    mode this whole path exists to avoid."""
    order: list[str] = []
    merged: dict[str, dict] = {}
    for page in pages or []:
        for rec in page or []:
            if not isinstance(rec, dict):
                continue
            rid = str(rec.get("id") or "")
            if not rid:
                continue
            if rid not in merged:
                merged[rid] = {}
                order.append(rid)
            merged[rid].update(rec)
    return [merged[r] for r in order]


def resume_truncate(jsonl: Path, cursor: dict) -> int:
    """Truncate a partially written ledger back to the last whole page and
    return the surviving line count. The cursor's byte offset is written
    only after a full page has been flushed and fsynced, so anything past it
    is a torn trailing line from a crash."""
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


def bulk_crosscheck(zip_path: Path, jsonl_path: Path) -> dict:
    """PURE-ish (filesystem reads only). Compare the archival Bulk Read CSV
    row count against the JSON ledger's line count.

    A non-zero delta is INFORMATIONAL, not a failure: the two run minutes
    apart, so records created or deleted in between legitimately differ.
    The JSON ledger is always the authority."""
    csv_rows = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if names:
                with zf.open(names[0]) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8",
                                            errors="replace")
                    reader = csv.reader(text)
                    csv_rows = max(sum(1 for _ in reader) - 1, 0)  # header
    except (OSError, zipfile.BadZipFile, csv.Error):
        csv_rows = None
    json_records = _count_lines(jsonl_path) if jsonl_path.exists() else None
    out = {"csv_rows": csv_rows, "json_records": json_records}
    if csv_rows is not None and json_records is not None:
        out["delta"] = json_records - csv_rows
        if out["delta"]:
            out["delta_note"] = (
                "snapshot skew (the JSON walk and the bulk job ran minutes "
                "apart) — informational; the JSON ledger is the authority")
    return out


def build_manifest(product: str, dc: str, api_domain, context: dict,
                   started_utc: str, finished_utc: str, api_calls: int,
                   results: list) -> dict:
    """PURE. verify's authority. failed_units and skipped_units are strictly
    separate: a skip is deliberate and never a failure."""
    failed = [r["unit"] for r in results if r.get("status") == "failed"]
    skipped = [{"unit": r["unit"], "reason": r.get("reason"),
                "detail": r.get("detail")}
               for r in results if r.get("status") == "skipped"]
    partial = [{"unit": r["unit"], "records": r.get("records"),
                "source_count": r.get("source_count"),
                "reason": "zoho pagination ceiling"}
               for r in results if r.get("status") == "partial"]
    total = sum(int(r.get("bytes") or 0) for r in results
                if r.get("status") in ("ok", "skipped-complete", "partial"))
    return {
        "product": product,
        "dc": dc,
        "api_domain": api_domain,
        "puller_version": 1,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "context": context,
        "unit_count": len(results),
        "total_staged_bytes": total,
        "api_calls": api_calls,
        "failed_units": failed,
        "skipped_units": skipped,
        "partial_units": partial,
        "results": results,
    }


# ── auth ─────────────────────────────────────────────────────────────────────

class TokenBox:
    """VM twin of zoho_transfer.py's TokenBox. Deliberately duplicated rather
    than imported (the github_transfer.py vs github_vm_pull.py precedent):
    this file is pushed to the VM alone, and it raises SystemExit where the
    laptop raises HarnessError."""

    def __init__(self, dc: str, client_id: str, client_secret: str,
                 refresh_token: str):
        self.dc = dc
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._value = None
        self._exp = 0.0
        self.api_domain = None
        self.mints = 0

    def get(self) -> str:
        if self._value and time.time() < self._exp - TOKEN_REFRESH_MARGIN:
            return self._value
        return self.mint()

    def invalidate(self) -> None:
        self._value = None

    def mint(self) -> str:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }).encode()
        last = None
        for attempt in range(1, API_RETRIES + 2):
            req = urllib.request.Request(
                f"https://accounts.zoho.{self.dc}/oauth/v2/token",
                data=data, method="POST",
                headers={"Content-Type":
                         "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    payload = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(
                    f"FATAL: Zoho token mint failed: HTTP {e.code}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                time.sleep(2 ** attempt)
                continue
            self.mints += 1
            return self._accept(payload)
        raise SystemExit(f"FATAL: Zoho token mint failed after retries: "
                         f"{last}")

    def _accept(self, payload: dict) -> str:
        """Zoho answers OAuth failures with HTTP 200 and an error body, so
        the error branch lives here rather than in an except."""
        err = zoho_error_code(payload)
        if err or "access_token" not in payload:
            if err in ("invalid_code", "invalid_grant"):
                raise SystemExit(
                    f"FATAL: Zoho rejected the refresh token ({err}) using "
                    f"data center '{self.dc}'. Either the token belongs to a "
                    "DIFFERENT data center, or it was revoked / the Self "
                    "Client was deleted. Retrying cannot help.")
            if err == "invalid_client":
                raise SystemExit(
                    "FATAL: Zoho rejected the client credentials "
                    "(invalid_client) — wrong client_id/client_secret, or "
                    "the Self Client was deleted.")
            raise SystemExit(
                f"FATAL: Zoho token mint returned no access_token "
                f"(code={err!r})")
        domain = (payload.get("api_domain") or "").rstrip("/")
        want = f"https://www.zohoapis.{self.dc}"
        if domain and domain != want:
            raise SystemExit(
                f"FATAL: data-center mismatch — the refresh token belongs to "
                f"{domain} but ZOHO_DC={self.dc} means {want}. Zoho said so "
                "in the mint response's api_domain.")
        self.api_domain = domain or want
        self._value = payload["access_token"]
        self._exp = time.time() + int(payload.get("expires_in", 3600))
        log("minted zoho access token")
        return self._value


class ZohoAPIError(Exception):
    def __init__(self, status: int, code: str, msg: str):
        super().__init__(msg)
        self.status = status
        self.code = code


# ── REST client ──────────────────────────────────────────────────────────────

class ZohoAPI:
    """One client for every Zoho product on this DC. Counts its own calls
    (the manifest's api_calls) because Zoho's credit budget is edition-scaled
    and we refuse to hard-code numbers we cannot verify."""

    def __init__(self, box: TokenBox, rate_sleep_max: int = RATE_SLEEP_MAX):
        self.box = box
        self.calls = 0
        self.rate_sleeps = 0
        self.rate_sleep_max = rate_sleep_max
        self.credits_exhausted = False
        self._last_bulk_download = 0.0

    @property
    def host(self) -> str:
        return f"www.zohoapis.{self.box.dc}"

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Zoho-oauthtoken {self.box.get()}",
             "Accept": "application/json",
             "User-Agent": "cdp-harness-zoho-transfer/1.0"}
        if extra:
            h.update(extra)
        return h

    def _sleep_for_rate(self, e, attempt: int) -> None:
        retry_after = (e.headers.get("Retry-After") or "").strip() \
            if e.headers else ""
        wait = int(retry_after) if retry_after.isdigit() \
            else max(60, 2 ** attempt)
        if wait > self.rate_sleep_max:
            # A retry horizon past the cap is the daily credit budget, not
            # throttling. A credit budget is a clock, not a bug: mark it and
            # let the caller fail this unit cheaply so the pass ends with a
            # manifest naming the wall. Tomorrow's run resumes from cursors.
            self.credits_exhausted = True
            raise ZohoAPIError(429, "LIMIT_EXCEEDED",
                               f"rate limit wants {wait}s, past the "
                               f"{self.rate_sleep_max}s cap — daily API "
                               "credits are likely exhausted")
        self.rate_sleeps += 1
        log(f"rate-limited; sleeping {wait}s")
        time.sleep(wait)

    def request(self, method: str, host: str, path: str,
                params: dict | None = None, body: bytes | None = None,
                headers: dict | None = None, stream_to: Path | None = None):
        """One call. Returns parsed JSON, or None on 204 (an EMPTY module —
        not an error; treating 204's empty body as a parse failure is the
        classic Zoho client bug). With stream_to, writes the body to disk in
        chunks and returns the byte count — a 2 GB attachment must never be
        read into memory on a VM running three products."""
        url = f"https://{host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last = None
        reminted = False
        for attempt in range(1, API_RETRIES + 1):
            req = urllib.request.Request(
                url, data=body, method=method,
                headers=self._headers(headers))
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=180) as resp:
                    if resp.status == 204:
                        return None
                    if stream_to is not None:
                        return self._stream(resp, stream_to)
                    raw = resp.read()
                    parsed = json.loads(raw) if raw.strip() else None
                    # a 200 can still be a refusal — see body_failure()
                    bad = body_failure(parsed)
                    if bad:
                        raise ZohoAPIError(
                            200, bad, f"HTTP 200 but body says failure "
                                      f"({bad}) on {path}")
                    return parsed
            except ZohoAPIError:
                raise
            except urllib.error.HTTPError as e:
                last = e
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001
                    detail = ""
                code = zoho_error_code(detail)
                if e.code == 401 and code != "OAUTH_SCOPE_MISMATCH" \
                        and not reminted:
                    reminted = True
                    self.box.invalidate()
                    continue
                if e.code == 429:
                    self._sleep_for_rate(e, attempt)
                    continue
                if e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise ZohoAPIError(
                    e.code, code, f"HTTP {e.code} {code} on {path}: {detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                time.sleep(2 ** attempt)
        status = getattr(last, "code", 0) or 0
        raise ZohoAPIError(status, "",
                           f"{method} {path} failed after "
                           f"{API_RETRIES} tries: {last}")

    def _stream(self, resp, out_path: Path) -> int:
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
        return written

    def get_json(self, path: str, params: dict | None = None,
                 host: str | None = None):
        return self.request("GET", host or self.host, path, params=params)

    def post_json(self, path: str, payload, host: str | None = None):
        return self.request(
            "POST", host or self.host, path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})

    def download(self, path: str, out_path: Path,
                 params: dict | None = None, host: str | None = None) -> int:
        return self.request("GET", host or self.host, path, params=params,
                            stream_to=out_path)

    def pace_bulk_download(self) -> None:
        """Bulk Read result downloads are capped at 10/minute."""
        gap = time.time() - self._last_bulk_download
        if self._last_bulk_download and gap < BULK_DOWNLOAD_MIN_INTERVAL:
            time.sleep(BULK_DOWNLOAD_MIN_INTERVAL - gap)
        self._last_bulk_download = time.time()


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


def fields_sha(fields) -> str:
    return hashlib.sha256(
        ",".join(sorted(fields or [])).encode()).hexdigest()[:16]


def _result(unit: str, kind: str, status: str, **extra) -> dict:
    out = {"unit": unit, "kind": kind, "status": status}
    out.update(extra)
    return out


def _handle_unit_error(unit: str, kind: str, e: ZohoAPIError) -> dict:
    """One place where classify() decides a unit's fate, so the required-vs-
    optional split cannot drift into per-call judgment."""
    required = kind in REQUIRED_KINDS
    verdict = classify(e.status, e.code, required)
    if verdict == "fatal" or verdict == "remint-once-then-fatal":
        raise SystemExit(
            f"FATAL: {unit} ({kind}) failed unrecoverably: {e}. "
            + ("The scope list is missing what this required unit needs — "
               "the client must regenerate the token."
               if e.code == "OAUTH_SCOPE_MISMATCH" else
               "The refresh token was revoked or the Self Client deleted."
               if e.status == 401 else ""))
    if verdict == "skip":
        reason = {403: "permission-denied", 404: "endpoint-absent"}.get(
            e.status, "api-refused")
        return _result(unit, kind, "skipped", reason=reason, detail=str(e))
    return _result(unit, kind, "failed",
                   reason="credits-exhausted"
                   if e.code == "LIMIT_EXCEEDED" else "api-error",
                   detail=str(e))


# ── CRM ──────────────────────────────────────────────────────────────────────

def list_modules(api: ZohoAPI) -> tuple[list[str], list[dict]]:
    payload = api.get_json("/crm/v8/settings/modules") or {}
    usable, skipped = [], []
    for m in payload.get("modules") or []:
        name = m.get("api_name")
        if not name:
            continue
        if m.get("deleted"):
            skipped.append({"module": name, "reason": "deleted"})
        elif not m.get("api_supported"):
            skipped.append({"module": name, "reason": "api_supported=false"})
        else:
            usable.append(name)
    return usable, skipped


def module_fields(api: ZohoAPI, module: str) -> list[str]:
    payload = api.get_json("/crm/v8/settings/fields",
                           {"module": module}) or {}
    names = []
    for f in payload.get("fields") or []:
        api_name = f.get("api_name")
        if api_name:
            names.append(api_name)
    return names


def pull_settings(api: ZohoAPI, dest: Path, modules: list[str],
                  refresh: bool) -> dict:
    unit = "settings"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "settings", "skipped-complete",
                       bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    try:
        atomic_write_json(d / "modules.json",
                          api.get_json("/crm/v8/settings/modules"))
        for leg, path in (("roles", "/crm/v8/settings/roles"),
                          ("profiles", "/crm/v8/settings/profiles"),
                          ("users", "/crm/v8/users"),
                          ("currencies", "/crm/v8/org/currencies")):
            try:
                atomic_write_json(d / f"{leg}.json", api.get_json(path))
            except ZohoAPIError as e:
                # an org-config leg the profile cannot read is not a reason
                # to lose the field schemas, which ARE the deliverable
                atomic_write_json(d / f"{leg}.json",
                                  {"unavailable": str(e)})
        (d / "fields").mkdir(exist_ok=True)
        (d / "layouts").mkdir(exist_ok=True)
        for module in modules:
            try:
                atomic_write_json(
                    d / "fields" / f"{safe_component(module)}.json",
                    api.get_json("/crm/v8/settings/fields",
                                 {"module": module}))
            except ZohoAPIError as e:
                atomic_write_json(
                    d / "fields" / f"{safe_component(module)}.json",
                    {"unavailable": str(e)})
            try:
                atomic_write_json(
                    d / "layouts" / f"{safe_component(module)}.json",
                    api.get_json("/crm/v8/settings/layouts",
                                 {"module": module}))
            except ZohoAPIError as e:
                atomic_write_json(
                    d / "layouts" / f"{safe_component(module)}.json",
                    {"unavailable": str(e)})
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "settings", e)
    mark_complete(d)
    return _result(unit, "settings", "ok", bytes=dir_size(d))


def dedupe_jsonl_by_id(path: Path) -> tuple[int, int]:
    """Rewrite a record JSONL keeping the FIRST row per id. Returns
    (kept, dropped). Needed because covering a >100k module means walking it
    ascending and then descending, which overlaps in the middle."""
    if not path.exists():
        return 0, 0
    seen: set = set()
    kept = dropped = 0
    tmp = path.with_suffix(path.suffix + ".dedupe")
    with open(path, "r", encoding="utf-8", errors="replace") as src, \
            open(tmp, "w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                rid = str(json.loads(line).get("id") or "")
            except ValueError:
                continue
            if rid and rid in seen:
                dropped += 1
                continue
            if rid:
                seen.add(rid)
            dst.write(line + "\n")
            kept += 1
    os.replace(tmp, path)
    return kept, dropped


def module_count(api: ZohoAPI, module: str):
    """Exact record count, or None. Cheap (one call) and it is what tells us
    whether a two-directional walk actually covered the module."""
    try:
        payload = api.get_json(f"/crm/v8/{module}/actions/count")
        return int((payload or {}).get("count"))
    except (ZohoAPIError, TypeError, ValueError):
        return None


def is_pagination_cap(e) -> bool:
    """PURE. Zoho's hard 100k page_token ceiling, which is NOT a stale token
    and must never trigger a re-walk (doing so is an infinite loop: the walk
    marches back to the same depth and fails identically — observed live on
    song-division 2026-08-25, ~45 min of looping on a 115k module)."""
    return getattr(e, "code", "") == "PAGINATION_LIMIT_EXCEEDED"


def _fetch_page(api: ZohoAPI, module: str, chunks: list[str],
                params: dict) -> tuple[list[dict], str | None]:
    """One page. The FIRST field chunk drives pagination; the remaining
    chunks are re-fetched for exactly this page's record ids and merged.

    Reusing a page_token across different `fields` sets is undefined (the
    token encodes the query), so the extra chunks go through the documented
    `ids` filter instead — more calls, but a ledger that is actually
    complete."""
    first = dict(params)
    first["fields"] = chunks[0]
    payload = api.get_json(f"/crm/v8/{module}", first) or {}
    rows = payload.get("data") or []
    info = payload.get("info") or {}
    next_token = info.get("next_page_token") if info.get("more_records") \
        else None
    if not rows or len(chunks) == 1:
        return rows, next_token
    pages = [rows]
    ids = [str(r.get("id")) for r in rows if r.get("id")]
    for chunk in chunks[1:]:
        for i in range(0, len(ids), ID_BATCH):
            batch = ids[i:i + ID_BATCH]
            extra = api.get_json(f"/crm/v8/{module}",
                                 {"fields": chunk,
                                  "ids": ",".join(batch)}) or {}
            pages.append(extra.get("data") or [])
    return merge_chunks(pages), next_token


def pull_module_records(api: ZohoAPI, module: str, dest: Path, refresh: bool,
                        since: str | None, page_size: int) -> dict:
    unit = f"records/{module}"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "records", "skipped-complete", bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    notes = []
    try:
        fields = module_fields(api, module)
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "records", e)
    chunks = fields_param(fields)
    if len(chunks) > 1:
        notes.append(
            f"{len(fields)} fields exceeded one request; extra chunks were "
            "re-fetched per page by record id and merged")
    sha = fields_sha(fields)
    atomic_write_json(d / "fields.json",
                      {"fields": fields, "fields_sha": sha,
                       "chunks": len(chunks)})
    jsonl = d / "records.jsonl"
    cursor = read_cursor(d)
    if cursor and cursor.get("fields_sha") != sha:
        notes.append("field schema changed since the last pass — the cursor "
                     "was invalidated and the module re-walked from page 1 "
                     "(a ledger with different schemas per range is worse "
                     "than one re-walk)")
        cursor = None
    if cursor:
        written = resume_truncate(jsonl, cursor)
        page_token = cursor.get("page_token")
        log(f"{unit}: resuming at {written} records")
    else:
        try:
            jsonl.unlink()
        except OSError:
            pass
        written, page_token = 0, None
    params_base = {"per_page": min(page_size or PER_PAGE, PER_PAGE)}
    if since:
        params_base["Modified_Time"] = since
    total = module_count(api, module)
    pages_done = 0
    restarted = False
    capped = False

    def _pass(direction: str, token, pages: int, count: int):
        """One directional walk. Returns (records, pages, hit_cap)."""
        nonlocal restarted
        params_dir = dict(params_base)
        params_dir["sort_by"] = "id"
        params_dir["sort_order"] = direction
        while True:
            params = dict(params_dir)
            if token:
                params["page_token"] = token
            try:
                rows, next_token = _fetch_page(api, module, chunks, params)
            except ZohoAPIError as e:
                if is_pagination_cap(e):
                    return count, pages, True
                if token and e.status == 400 and not restarted:
                    # a genuinely stale token — retry the walk ONCE. Never
                    # more: an unconditional restart on any 400 is how the
                    # infinite loop happened.
                    restarted = True
                    notes.append("the stored page_token was rejected once — "
                                 "that direction was re-walked from page 1")
                    token, pages = None, 0
                    continue
                raise
            if rows:
                with open(jsonl, "a", encoding="utf-8") as fh:
                    for rec in rows:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                count += len(rows)
            pages += 1
            atomic_write_json(d / ".cdp-cursor.json", {
                "page_token": next_token, "direction": direction,
                "bytes": jsonl.stat().st_size if jsonl.exists() else 0,
                "records": count, "fields_sha": sha})
            if pages % 25 == 0:
                log(f"{unit}: {count} records, {pages} pages ({direction})")
            if not next_token:
                return count, pages, False
            token = next_token

    try:
        written, pages_done, capped = _pass(
            (cursor or {}).get("direction") or "asc", page_token,
            pages_done, written)
        if capped:
            # Zoho stops page_token at 100k. Walking the SAME module in the
            # opposite id order reaches the other end, so a module up to
            # 2 x PAGINATION_CAP is fully covered once the halves are merged.
            log(f"{unit}: hit Zoho's {PAGINATION_CAP:,}-record page_token "
                f"cap; walking the other end (module has "
                f"{total if total is not None else '?'} records)")
            notes.append(
                f"Zoho caps page_token pagination at {PAGINATION_CAP:,} "
                "records, so this module was walked from both ends and "
                "merged")
            written, pages_done, capped2 = _pass("desc", None, pages_done,
                                                 written)
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "records", e)

    kept, dropped = dedupe_jsonl_by_id(jsonl)
    complete = True
    if capped:
        complete = total is not None and kept >= total
        if not complete:
            notes.append(
                f"INCOMPLETE: {kept} of {total if total is not None else '?'} "
                f"records. The module exceeds 2 x {PAGINATION_CAP:,}, which "
                "two-directional paging cannot cover — use the Bulk Read ZIP "
                "for this module, or slice the pull by date.")
    mark_complete(d)
    return _result(unit, "records", "ok" if complete else "partial",
                   bytes=dir_size(d), records=kept, source_count=total,
                   duplicates_dropped=dropped, fields=len(fields),
                   pages=pages_done, field_chunks=len(chunks),
                   hit_pagination_cap=capped, complete=complete,
                   notes=notes or None)


def pull_module_bulk(api: ZohoAPI, module: str, dest: Path,
                     refresh: bool) -> dict:
    """The archival cross-check. Bulk Read is asynchronous and its result
    EXPIRES after a day, so resume here is job-level: an expired or missing
    result re-submits a fresh job rather than resuming a byte offset."""
    unit = f"bulk/{module}"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "bulk", "skipped-complete", bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    try:
        fields = module_fields(api, module)[:BULK_MAX_FIELDS]
        payload = {"query": {"module": {"api_name": module},
                             "fields": fields, "page": 1},
                   "file_type": "csv"}
        resp = api.post_json("/crm/bulk/v8/read", payload) or {}
        details = ((resp.get("data") or [{}])[0] or {}).get("details") or {}
        job_id = details.get("id")
        if not job_id:
            return _result(unit, "bulk", "skipped", reason="no-job-id",
                           detail=json.dumps(resp)[:200])
        deadline = time.time() + BULK_JOB_TIMEOUT_S
        state = ""
        while time.time() < deadline:
            time.sleep(BULK_POLL_S)
            info = api.get_json(f"/crm/bulk/v8/read/{job_id}") or {}
            row = (info.get("data") or [{}])[0] or {}
            state = row.get("state") or ""
            if state in ("COMPLETED", "FAILURE"):
                break
        if state != "COMPLETED":
            return _result(unit, "bulk", "skipped",
                           reason="job-not-completed",
                           detail=state or
                           f"job did not finish within {BULK_JOB_TIMEOUT_S}s")
        api.pace_bulk_download()
        out_zip = d / f"{job_id}.zip"
        size = api.download(f"/crm/bulk/v8/read/{job_id}/result", out_zip)
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "bulk", e)
    check = bulk_crosscheck(out_zip,
                            dest / "records" / module / "records.jsonl")
    atomic_write_json(d / "job.json",
                      {"job_id": job_id, "module": module,
                       "zip_bytes": size, **check})
    mark_complete(d)
    return _result(unit, "bulk", "ok", bytes=dir_size(d), job_id=job_id,
                   **check)


def pull_notes(api: ZohoAPI, dest: Path, refresh: bool,
               page_size: int) -> dict:
    """Notes are a module Bulk Read explicitly excludes, so they get their
    own JSON walk."""
    unit = "notes"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "notes", "skipped-complete", bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    try:
        note_fields = module_fields(api, "Notes")[:FIELD_CHUNK - 1] or ["id"]
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "notes", e)
    note_fields = ["id"] + [f for f in note_fields if f != "id"]
    jsonl = d / "notes.jsonl"
    cursor = read_cursor(d)
    if cursor:
        written = resume_truncate(jsonl, cursor)
        page_token = cursor.get("page_token")
    else:
        try:
            jsonl.unlink()
        except OSError:
            pass
        written, page_token = 0, None
    try:
        while True:
            params = {"per_page": min(page_size or PER_PAGE, PER_PAGE),
                      # v8 makes `fields` mandatory here too — omitting it
                      # is 400 REQUIRED_PARAM_MISSING, which skipped the
                      # whole unit on the first live run
                      "fields": ",".join(note_fields)}
            if page_token:
                params["page_token"] = page_token
            payload = api.get_json("/crm/v8/Notes", params) or {}
            rows = payload.get("data") or []
            info = payload.get("info") or {}
            if rows:
                with open(jsonl, "a", encoding="utf-8") as fh:
                    for rec in rows:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                written += len(rows)
            nxt = info.get("next_page_token") if info.get("more_records") \
                else None
            atomic_write_json(d / ".cdp-cursor.json", {
                "page_token": nxt,
                "bytes": jsonl.stat().st_size if jsonl.exists() else 0,
                "records": written})
            if not nxt:
                break
            page_token = nxt
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "notes", e)
    mark_complete(d)
    return _result(unit, "notes", "ok", bytes=dir_size(d), records=written)


def _iter_record_ids(jsonl: Path):
    if not jsonl.exists():
        return
    with open(jsonl, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rid = rec.get("id")
            if rid:
                yield str(rid)


def pull_attachments(api: ZohoAPI, dest: Path, refresh: bool,
                     workers: int) -> dict:
    """ONE unit, driven by a direct walk of the Attachments MODULE.

    The obvious implementation — list /crm/v8/<M>/<recordId>/Attachments for
    every record — is one call per record: 251,298 calls at ~2.2 calls/s is
    ~32 hours, and on a real tenant 80 sampled records across the four
    biggest modules had none at all (song-division, 2026-08-24).
    `/crm/v8/Attachments` is directly listable and paginates at ~300
    rows/s, so the whole census (8,511 files / 23.4 GB there) takes ~28
    seconds. Each row carries Parent_Id.module.api_name + Parent_Id.id,
    which is exactly what the per-attachment download URL needs.

    `Size` is an EXACT byte integer here (verified live), not the rounded
    human string the docs describe — so this unit records expected bytes and
    verify can compare source against staged.
    """
    unit = "attachments"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "attachments", "skipped-complete",
                       bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    index_path = d / "index.jsonl"
    cursor = read_cursor(d) or {}
    page_token = cursor.get("page_token")
    if not page_token:
        try:
            index_path.unlink()
        except OSError:
            pass
    listed = int(cursor.get("listed") or 0)
    expected_bytes = int(cursor.get("expected_bytes") or 0)
    files = int(cursor.get("files") or 0)
    file_errors = int(cursor.get("file_errors") or 0)
    consecutive = 0
    deferred_email = 0
    workers = max(1, min(workers or ATTACHMENT_WORKERS,
                         ATTACHMENT_WORKERS_CAP))
    fields = "id,File_Name,Size,Parent_Id,Created_Time,Modified_Time"
    try:
        while True:
            params = {"fields": fields, "per_page": PER_PAGE}
            if page_token:
                params["page_token"] = page_token
            payload = api.get_json("/crm/v8/Attachments", params) or {}
            rows = payload.get("data") or []
            info = payload.get("info") or {}
            if rows:
                with open(index_path, "a", encoding="utf-8") as fh:
                    for rec in rows:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                listed += len(rows)
                for rec in rows:
                    try:
                        expected_bytes += int(rec.get("Size") or 0)
                    except (TypeError, ValueError):
                        pass
                got, errs, defer = _download_attachment_rows(
                    api, rows, d, workers)
                files += got
                file_errors += errs
                deferred_email += defer
                consecutive = consecutive + errs if not got else 0
                if consecutive >= ATTACHMENT_BREAKER and files == 0:
                    return _result(
                        unit, "attachments", "failed",
                        reason="download-breaker",
                        detail=f"{consecutive} consecutive attachment "
                               "failures with zero successes")
            page_token = info.get("next_page_token") \
                if info.get("more_records") else None
            atomic_write_json(d / ".cdp-cursor.json", {
                "page_token": page_token, "listed": listed,
                "expected_bytes": expected_bytes, "files": files,
                "file_errors": file_errors})
            log(f"{unit}: {listed} listed, {files} downloaded, "
                f"{file_errors} errors, "
                f"{human_bytes(expected_bytes)} expected")
            if not page_token:
                break
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "attachments", e)
    mark_complete(d)
    return _result(unit, "attachments", "ok", bytes=dir_size(d),
                   listed=listed, files=files, file_errors=file_errors,
                   deferred_to_email_unit=deferred_email,
                   expected_bytes=expected_bytes)


def _download_attachment_rows(api: ZohoAPI, rows: list, d: Path,
                              workers: int) -> tuple[int, int]:
    """Download one page of attachment rows, in a small pool. The cap keeps
    us under the smallest edition's concurrency ceiling so we never CAUSE a
    429. Blob name is <parentModule>/<parentId>/<attId>__<safe-name>:
    id-first so two attachments sharing a filename never collide, and
    deterministic so a resume skips what already landed."""
    jobs = []
    deferred = 0
    for rec in rows:
        att_id = rec.get("id")
        parent = rec.get("Parent_Id") or {}
        pmod = ((parent.get("module") or {}).get("api_name")
                or "UnknownModule")
        pid = parent.get("id")
        if not (att_id and pid):
            continue
        if pmod == "Emails":
            # Email attachments are NOT reachable at
            # /crm/v8/<M>/<id>/Attachments/<attId> — they need the dedicated
            # download_attachments endpoint plus a message_id/user_id that
            # this listing does not carry. The email_attachments unit owns
            # them; counting them as download errors here was wrong.
            deferred += 1
            continue
        name = safe_component(rec.get("File_Name") or att_id)
        out = (d / safe_component(pmod) / str(pid)
               / f"{att_id}__{name}")
        if out.exists():
            continue
        jobs.append((f"/crm/v8/{pmod}/{pid}/Attachments/{att_id}", out))
    if not jobs:
        return 0, deferred
    got = errs = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(api.download, path, out)
                   for path, out in jobs]
        for fut in concurrent.futures.as_completed(futures):
            try:
                fut.result()
                got += 1
            except Exception:  # noqa: BLE001 - counted per file, never fatal
                errs += 1
    return got, errs, deferred


def pull_email_attachments(api: ZohoAPI, module: str, dest: Path,
                           refresh: bool, workers: int) -> dict:
    """Email attachments, which the Attachments module cannot deliver.

    Two things make these their own unit:
      1. `/crm/v8/<M>/<id>/Attachments/<attId>` does NOT serve them. They
         need `/crm/v8/<M>/<recordId>/Emails/actions/download_attachments`
         with message_id + user_id + the attachment's own id.
      2. The Attachments MODULE lists only a fraction of them (388 rows on
         song-division) while 7,586 messages actually carry attachments —
         so the module census is not the authority here; the staged email
         ledger is.

    Driven off the staged emails/<M>/emails.jsonl so ordering is
    deterministic and the cursor can be a line index. Only messages flagged
    has_attachment are opened, which keeps this proportional to attachments
    rather than to messages.
    """
    unit = f"email_attachments/{module}"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "email_attachments", "skipped-complete",
                       bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    ledger = dest / "emails" / module / "emails.jsonl"
    if not ledger.exists():
        mark_skipped(d, "no-email-ledger",
                     f"emails/{module} did not run")
        return _result(unit, "email_attachments", "skipped",
                       reason="no-email-ledger",
                       detail=f"emails/{module} did not run")
    cursor = read_cursor(d) or {}
    start = int(cursor.get("line") or 0)
    files = int(cursor.get("files") or 0)
    errs = int(cursor.get("file_errors") or 0)
    scanned = int(cursor.get("messages") or 0)
    workers = max(1, min(workers or ATTACHMENT_WORKERS,
                         ATTACHMENT_WORKERS_CAP))
    index_fh = open(d / "index.jsonl", "a", encoding="utf-8")
    try:
        with open(ledger, "r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh):
                if line_no < start:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rid = rec.get("record_id")
                for msg in (rec.get("messages") or []):
                    if str(msg.get("has_attachment")).lower() != "true":
                        continue
                    mid = msg.get("message_id")
                    uid = (msg.get("owner") or {}).get("id")
                    if not (rid and mid):
                        continue
                    scanned += 1
                    try:
                        detail = api.get_json(
                            f"/crm/v8/{module}/{rid}/Emails/{mid}") or {}
                    except ZohoAPIError as e:
                        if classify(e.status, e.code, False) == "skip":
                            continue
                        raise
                    atts = ((detail.get("Emails") or [{}])[0]
                            or {}).get("attachments") or []
                    if not atts:
                        continue
                    index_fh.write(json.dumps(
                        {"record_id": rid, "message_id": mid,
                         "user_id": uid, "attachments": atts},
                        ensure_ascii=False) + "\n")
                    index_fh.flush()
                    got, bad = _download_email_attachments(
                        api, module, rid, mid, uid, atts, d, workers)
                    files += got
                    errs += bad
                if line_no % 200 == 0:
                    atomic_write_json(d / ".cdp-cursor.json", {
                        "line": line_no, "files": files, "file_errors": errs,
                        "messages": scanned})
                    log(f"{unit}: line {line_no}, {scanned} msgs w/ "
                        f"attachments, {files} files, {errs} errors")
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "email_attachments", e)
    finally:
        index_fh.close()
    atomic_write_json(d / ".cdp-cursor.json", {
        "line": -1, "files": files, "file_errors": errs,
        "messages": scanned})
    mark_complete(d)
    return _result(unit, "email_attachments", "ok", bytes=dir_size(d),
                   messages_with_attachments=scanned, files=files,
                   file_errors=errs)


def _download_email_attachments(api: ZohoAPI, module: str, rid: str,
                                mid: str, uid, atts: list, d: Path,
                                workers: int) -> tuple[int, int]:
    """One message's attachments via the dedicated download endpoint."""
    jobs = []
    for att in atts:
        aid = att.get("id")
        if not aid:
            continue
        name = safe_component(att.get("name") or aid)
        out = d / str(rid) / str(mid) / f"{safe_component(aid)}__{name}"
        if out.exists():
            continue
        params = {"message_id": mid, "id": aid,
                  "name": att.get("name") or name}
        if uid:
            params["user_id"] = uid
        jobs.append((params, out))
    if not jobs:
        return 0, 0
    got = bad = 0
    path = f"/crm/v8/{module}/{rid}/Emails/actions/download_attachments"
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(api.download, path, out, params)
                for params, out in jobs]
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
                got += 1
            except Exception:  # noqa: BLE001 - counted per file, never fatal
                bad += 1
    return got, bad


def pull_emails(api: ZohoAPI, module: str, dest: Path, refresh: bool) -> dict:
    """Emails are a related list Bulk Read excludes and that many modules do
    not support at all — a 404 here means 'feature not enabled', a skip."""
    unit = f"emails/{module}"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "emails", "skipped-complete", bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    ledger = dest / "records" / module / "records.jsonl"
    if not ledger.exists():
        mark_skipped(d, "no-ledger", "records unit did not run")
        return _result(unit, "emails", "skipped", reason="no-ledger",
                       detail="records/%s did not run" % module)
    ids = list(_iter_record_ids(ledger))
    cursor = read_cursor(d) or {}
    start = int(cursor.get("record_index") or 0)
    jsonl = d / "emails.jsonl"
    found = 0
    try:
        with open(jsonl, "a", encoding="utf-8") as fh:
            for i in range(start, len(ids)):
                rid = ids[i]
                try:
                    payload = api.get_json(
                        f"/crm/v8/{module}/{rid}/Emails") or {}
                except ZohoAPIError as e:
                    verdict = classify(e.status, e.code, False)
                    if verdict == "skip" and i == start:
                        mark_skipped(d, "endpoint-absent", str(e))
                        return _result(unit, "emails", "skipped",
                                       reason="endpoint-absent",
                                       detail=str(e))
                    if verdict == "skip":
                        continue
                    raise
                msgs = payload.get("Emails") or payload.get("data") or []
                if msgs:
                    fh.write(json.dumps({"record_id": rid,
                                         "messages": msgs},
                                        ensure_ascii=False) + "\n")
                    fh.flush()
                    found += 1
                if i % 200 == 0:
                    atomic_write_json(d / ".cdp-cursor.json",
                                      {"record_index": i, "found": found})
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "emails", e)
    atomic_write_json(d / ".cdp-cursor.json",
                      {"record_index": len(ids), "found": found})
    mark_complete(d)
    return _result(unit, "emails", "ok", bytes=dir_size(d),
                   records_with_email=found)


# ── Learn ────────────────────────────────────────────────────────────────────

def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def learn_discover(api: ZohoAPI, portal: str, dc: str) -> dict:
    """Zoho has no has_wiki analogue: whether a Learn endpoint exists is only
    knowable by asking. Every attempt is recorded so the run can say what it
    found rather than what it assumed."""
    hosts = [f"learn.zoho.{dc}"] + ([] if dc == "com" else ["learn.zoho.com"])
    out = {"portal": portal, "attempts": {}, "course_host": None, "kb": {}}
    for host in hosts:
        path = f"/learn/api/v1/portal/{portal}/course"
        try:
            api.get_json(path, {"pageIndex": 0, "limit": 1,
                                "view": LEARN_COURSE_VIEW}, host=host)
            out["attempts"][f"{host}{path}"] = "ok"
            out["course_host"] = host
            break
        except ZohoAPIError as e:
            out["attempts"][f"{host}{path}"] = f"{e.status}-{e.code or 'x'}"
    host = out["course_host"]
    if host:
        for key, tmpl in LEARN_KB_CANDIDATES:
            path = tmpl.format(portal=portal)
            try:
                api.get_json(path, {"limit": 1}, host=host)
                out["kb"][key] = {"path": path, "state": "ok"}
            except ZohoAPIError as e:
                out["kb"][key] = {"path": path,
                                  "state": f"{e.status}-{e.code or 'x'}"}
    out["kb_reachable"] = [k for k, v in out["kb"].items()
                           if v.get("state") == "ok"]
    out["note"] = (
        "the knowledge-base half of Zoho Learn has NO documented API. These "
        "paths were ATTEMPTED, not assumed. "
        + (f"Reachable: {', '.join(out['kb_reachable'])}."
           if out["kb_reachable"] else
           "None answered — this pull covers COURSES ONLY."))
    return out


def learn_rows(payload) -> list:
    """PURE. Learn answers with UPPERCASE keys (`{"STATUS":"OK","DATA":[...]}`)
    while CRM uses lowercase `data` — verified live on song-division,
    2026-08-24. Some collections use their own key entirely (`QUIZ`). Read
    all of them so a live response is never silently seen as empty."""
    if not isinstance(payload, dict):
        return []
    for key in ("DATA", "data", "QUIZ", "COURSE", "MANUAL", "TAG"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


def _learn_walk(api: ZohoAPI, host: str, path: str, d: Path, out_name: str,
                cursor_extra: dict | None = None,
                extra_params: dict | None = None) -> int:
    """Shared 0-based pageIndex walk. Learn has no documented credit model,
    so a small inter-call floor stands in for one."""
    jsonl = d / out_name
    cursor = read_cursor(d)
    if cursor:
        written = resume_truncate(jsonl, cursor)
        page = int(cursor.get("pageIndex") or 0)
    else:
        try:
            jsonl.unlink()
        except OSError:
            pass
        written, page = 0, 0
    seen: set = set()
    while page < LEARN_MAX_PAGES:
        params = {"pageIndex": page, "limit": LEARN_LIMIT}
        params.update(extra_params or {})
        payload = api.get_json(path, params, host=host) or {}
        rows = learn_rows(payload)
        # Some Learn collections IGNORE `limit` and return the whole set on
        # every pageIndex (/tag returns all 755 at once — verified live).
        # Stopping on `len(rows) < limit` alone would loop forever, so stop
        # when a page contributes nothing new.
        fresh = []
        for rec in rows:
            key = json.dumps(rec, sort_keys=True) if not isinstance(rec, dict) \
                else str(rec.get("id") or json.dumps(rec, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            fresh.append(rec)
        if fresh:
            with open(jsonl, "a", encoding="utf-8") as fh:
                for rec in fresh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            written += len(fresh)
        page += 1
        atomic_write_json(d / ".cdp-cursor.json", {
            "pageIndex": page,
            "bytes": jsonl.stat().st_size if jsonl.exists() else 0,
            "items": written, **(cursor_extra or {})})
        if not fresh or len(rows) < LEARN_LIMIT:
            break
        time.sleep(LEARN_MIN_INTERVAL)
    return written


def flatten_lessons(lessons) -> list:
    """PURE. Lessons NEST: a CHAPTER carries its child BLOCKs in its own
    `lessons` array, so a course whose lessonCount is 6 can expose exactly
    ONE top-level lesson. Iterating only the top level silently drops the
    actual content (verified live on song-division 2026-08-25: 1 CHAPTER ->
    6 BLOCK children). Depth-first, parents included."""
    out = []
    stack = list(lessons or [])
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        out.append(node)
        kids = node.get("lessons")
        if isinstance(kids, list) and kids:
            stack = list(kids) + stack
    return out


def pull_lesson_details(api: ZohoAPI, host: str, portal: str, detail,
                        d: Path) -> int:
    """Full detail for every lesson of one course.

    Needs ZohoLearn.lesson.READ — WITHOUT it this 401s as
    INVALID_OAUTHSCOPE, and that scope is missing from Zoho's own published
    scope list. The route also demands BOTH `lesson.url` and `course.url`
    (the slugs, not the ids), which the lesson objects carry as `url` and
    `courseUrl`.

    This is where Learn's actual lesson content lives: `lessonMeta.blocks`.
    Note that video lessons are typically **embeds, not stored media** —
    song-division's are `VIDEO_TYPE: VIMEO_EMBED` pointing at Vimeo ids that
    the vimeo ingest already captured, so Learn holds the reference and
    Vimeo holds the bytes. Record the reference; do not expect Learn to
    yield video files.
    """
    course = (detail or {}).get("COURSE") or {}
    cid = course.get("id")
    curl = course.get("url")
    out_dir = d / "lessons" / safe_component(cid or "unknown")
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for L in flatten_lessons(course.get("lessons")):
        lid, lurl = L.get("id"), L.get("url")
        if not (lid and lurl and curl and cid):
            continue
        try:
            full = api.get_json(
                f"/learn/api/v1/portal/{portal}/course/{cid}/lesson/{lid}",
                {"lesson.url": lurl, "course.url": curl}, host=host)
        except ZohoAPIError as e:
            log(f"courses: lesson {lid} unavailable ({e})")
            continue
        atomic_write_json(out_dir / f"{safe_component(lid)}.json", full)
        n += 1
    return n


def pull_learn_courses(api: ZohoAPI, portal: str, discovery: dict,
                       dest: Path, refresh: bool) -> dict:
    unit = "courses"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "courses", "skipped-complete", bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    host = discovery.get("course_host")
    if not host:
        mark_skipped(d, "endpoint-absent",
                     "no Learn host answered the documented /course path")
        return _result(unit, "courses", "skipped", reason="endpoint-absent",
                       detail=json.dumps(discovery.get("attempts"))[:300])
    try:
        # view MUST be "all". It defaults to "learn", which means "courses
        # the CALLING USER is enrolled in" — a Self Client admin is enrolled
        # in nothing, so the default silently reports an empty portal.
        # Verified live on song-division 2026-08-25: view=learn -> 0 courses,
        # view=all -> 2. This is the single easiest way to conclude a Learn
        # tenant is empty when it is not.
        written = _learn_walk(
            api, host, f"/learn/api/v1/portal/{portal}/course", d,
            "courses.jsonl", extra_params={"view": LEARN_COURSE_VIEW})
        # course DETAIL carries the lesson list inline (lessonCount/lessons),
        # so it is the only place lesson metadata is reachable without
        # ZohoLearn.lesson.READ
        details = 0
        lessons = 0
        (d / "detail").mkdir(exist_ok=True)
        for rec in _iter_jsonl(d / "courses.jsonl"):
            cid = rec.get("id")
            if not cid:
                continue
            try:
                detail = api.get_json(
                    f"/learn/api/v1/portal/{portal}/course/{cid}", host=host)
            except ZohoAPIError as e:
                log(f"courses: detail {cid} unavailable ({e})")
                continue
            atomic_write_json(d / "detail" / f"{safe_component(cid)}.json",
                              detail)
            details += 1
            lessons += pull_lesson_details(api, host, portal, detail, d)
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "courses", e)
    mark_complete(d)
    return _result(unit, "courses", "ok", bytes=dir_size(d), items=written,
                   details=details, lessons=lessons)


def pull_learn_kb(api: ZohoAPI, portal: str, discovery: dict, dest: Path,
                  refresh: bool) -> list[dict]:
    """One unit per KB collection that actually answered at discovery. If
    none did, exactly one recorded skip is emitted so the manifest says so
    out loud."""
    host = discovery.get("course_host")
    reachable = discovery.get("kb_reachable") or []
    if not reachable:
        d = unit_dir(dest, "kb")
        mark_skipped(d, "endpoint-absent",
                     "no undocumented knowledge-base path answered")
        return [_result("kb", "kb", "skipped", reason="endpoint-absent",
                        detail="courses-only; the knowledge base needs a "
                               "manual export conversation with the client")]
    out = []
    for key in reachable:
        unit = f"kb/{key}"
        d = unit_dir(dest, unit)
        if is_complete(d) and not refresh:
            out.append(_result(unit, "kb", "skipped-complete",
                               bytes=dir_size(d)))
            continue
        if refresh:
            clear_unit(d)
        path = discovery["kb"][key]["path"]
        try:
            written = _learn_walk(api, host, path, d, f"{key}.jsonl")
        except ZohoAPIError as e:
            out.append(_handle_unit_error(unit, "kb", e))
            continue
        mark_complete(d)
        out.append(_result(unit, "kb", "ok", bytes=dir_size(d),
                           items=written))
    return out


# ── WorkDrive ────────────────────────────────────────────────────────────────

def workdrive_boundary(api: ZohoAPI) -> dict:
    """'Whatever the token can see': enumerate the reachable boundary and
    record it, including what was explicitly NOT reachable. The boundary is a
    client conversation, never a silent omission."""
    out = {"reachable": [], "unreachable": {}, "teams": [], "folders": []}
    try:
        me = api.get_json("/workdrive/api/v1/users/me") or {}
        user_id = (me.get("data") or {}).get("id")
        out["reachable"].append("users/me")
    except ZohoAPIError as e:
        out["unreachable"]["users/me"] = str(e)
        user_id = None
    if user_id:
        try:
            teams = api.get_json(
                f"/workdrive/api/v1/users/{user_id}/teams") or {}
            for t in teams.get("data") or []:
                out["teams"].append({
                    "id": t.get("id"),
                    "name": (t.get("attributes") or {}).get("name")})
            out["reachable"].append("users/<id>/teams")
        except ZohoAPIError as e:
            out["unreachable"]["users/<id>/teams"] = str(e)
    for team in out["teams"]:
        try:
            folders = api.get_json(
                f"/workdrive/api/v1/teams/{team['id']}/teamfolders") or {}
            for f in folders.get("data") or []:
                out["folders"].append({
                    "id": f.get("id"), "team": team["id"],
                    "name": (f.get("attributes") or {}).get("name")})
        except ZohoAPIError as e:
            out["unreachable"][f"teams/{team['id']}/teamfolders"] = str(e)
    out["note"] = (
        "WorkDrive coverage is bounded by what the granted token reaches. "
        "Anything under 'unreachable' was never visible to us — that is a "
        "scope conversation with the client, not a failure of this run.")
    return out


def pull_workdrive_folder(api: ZohoAPI, folder: dict, dest: Path,
                          refresh: bool) -> dict:
    """Breadth-first over one folder tree. Files stream to disk; the listing
    is kept alongside so the corpus is self-describing."""
    fid = folder["id"]
    unit = f"files/{safe_component(fid)}"
    d = unit_dir(dest, unit)
    if is_complete(d) and not refresh:
        return _result(unit, "workdrive", "skipped-complete",
                       bytes=dir_size(d))
    if refresh:
        clear_unit(d)
    listing = d / "listing.jsonl"
    files = errs = 0
    queue = [fid]
    seen = set()
    try:
        with open(listing, "a", encoding="utf-8") as fh:
            while queue:
                current = queue.pop(0)
                if current in seen:
                    continue
                seen.add(current)
                payload = api.get_json(
                    f"/workdrive/api/v1/files/{current}/files") or {}
                for item in payload.get("data") or []:
                    attrs = item.get("attributes") or {}
                    fh.write(json.dumps({"parent": current, **item},
                                        ensure_ascii=False) + "\n")
                    if attrs.get("is_folder"):
                        queue.append(item.get("id"))
                        continue
                    name = safe_component(attrs.get("name")
                                          or item.get("id"))
                    out = d / f"{item.get('id')}__{name}"
                    if out.exists():
                        continue
                    try:
                        api.download(
                            f"/workdrive/api/v1/download/{item.get('id')}",
                            out)
                        files += 1
                    except ZohoAPIError:
                        errs += 1
                fh.flush()
                atomic_write_json(d / ".cdp-cursor.json",
                                  {"visited": len(seen), "files": files,
                                   "file_errors": errs})
    except ZohoAPIError as e:
        return _handle_unit_error(unit, "workdrive", e)
    mark_complete(d)
    return _result(unit, "workdrive", "ok", bytes=dir_size(d),
                   folders_visited=len(seen), files=files, file_errors=errs)


# ── upload ───────────────────────────────────────────────────────────────────

def upload_run_metadata(dest: Path, dest_url: str, sas: str) -> bool:
    """manifest.json / progress.json with overwrite ALLOWED.

    Everything else rides --overwrite=false, but these two are OUR run
    bookkeeping, not client corpus data — and manifest.json is exactly what
    verify treats as authoritative. Uploading it no-overwrite means a
    re-run's manifest is silently skipped and verify certifies against the
    FIRST pass forever (observed live on song-division 2026-08-25: a 3-unit
    re-run still verified as the 4-unit original). The no-modify invariant
    is about the client's data; overwriting our own manifest is required for
    it to stay true."""
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
        log(f"upload: {name} {'DONE' if good else 'FAILED'} (overwrite=true)")
        ok = ok and good
    # Per-unit control files are bookkeeping too. A --refresh rewrites a
    # cursor, no-overwrite keeps the OLD one, and verify then reports a
    # few-byte short_upload on a unit that is actually complete (seen live:
    # zoho_learn/courses staged 32,148 vs landed 32,145).
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
    no-overwrite, the same choice as azcopy-runner.sh on the S3 path — NOT
    the API-enforced If-None-Match of the local pulls. Say it that way when
    describing the guarantee. SAS never printed; output is scanned, not
    echoed raw.

    `overwrite=True` is used for exactly one case: a unit re-pulled under
    --refresh. That flag already means "discard the previous pull of this
    unit and redo it" (it deletes the local files, markers and cursor), so
    leaving the PREVIOUS export in the container would strand a stale copy
    and make verify report a phantom short_upload — Zoho mutates read-only
    fields like viewCount, so a re-pull of the same object legitimately
    differs by a few bytes. This only ever touches blobs this ingest itself
    wrote, under its own dest prefix; client-uploaded data is never in
    scope.
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


# ── product drivers ──────────────────────────────────────────────────────────

def _record_unit(results: list, res: dict, state: dict, dest: Path,
                 dest_url: str, sas: str, skip_upload: bool,
                 refresh: bool = False) -> None:
    """Append a unit result, run the systemic breaker, and upload the unit
    as soon as it is whole.

    Uploading per unit (rather than github's stage-everything-then-azcopy) is
    deliberate: 133 GB of CRM attachments should be durable in the container
    long before the product finishes, so a mid-run VM loss costs one unit
    instead of the whole pass.
    """
    results.append(res)
    if res.get("status") in ("ok", "skipped-complete"):
        state["ok"] += 1
    elif res.get("status") == "failed":
        state["failed"] += 1
        if state["ok"] == 0 and state["failed"] >= SYSTEMIC_BREAKER:
            raise SystemExit(
                f"FATAL: the first {SYSTEMIC_BREAKER} units all failed with "
                "zero successes — systemic (credentials / scopes / network / "
                f"SAS), aborting. Last error: {res.get('detail')}")
    if res.get("status") == "ok" and not skip_upload:
        upload(dest, dest_url, sas, res["unit"], overwrite=refresh)


def email_modules(modules, no_emails: bool, only) -> list:
    """PURE. Which modules get a per-record email sweep.

    That sweep is ONE call per record (~1.3 rec/s measured on song-division,
    2026-08-25), so across 251k records it is ~54 h. Scoping it to the
    modules that actually carry mail is the difference between ~12 h and
    ~54 h — Tasks (115k) and Notes (67k) dominate the record count and carry
    none. An empty/absent filter means every module, preserving the old
    behaviour."""
    if no_emails:
        return []
    wanted = {m.strip() for m in (only or "").split(",") if m.strip()}
    if not wanted:
        return list(modules)
    return [m for m in modules if m in wanted]


def run_crm(api: ZohoAPI, dest: Path, args, dest_url: str,
            sas: str) -> tuple[list, dict]:
    results: list = []
    state = {"ok": 0, "failed": 0}
    meta = dest / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    modules, skipped_modules = list_modules(api)
    if args.modules:
        want = {m.strip() for m in args.modules.split(",") if m.strip()}
        modules = [m for m in modules if m in want]
    if args.skip_modules:
        drop = {m.strip() for m in args.skip_modules.split(",") if m.strip()}
        modules = [m for m in modules if m not in drop]
    if args.limit:
        modules = modules[: args.limit]
    atomic_write_json(meta / "modules-selected.json",
                      {"selected": modules, "skipped": skipped_modules})
    try:
        atomic_write_json(meta / "org.json", api.get_json("/crm/v8/org"))
    except ZohoAPIError as e:
        atomic_write_json(meta / "org.json", {"unavailable": str(e)})
    log(f"crm: {len(modules)} modules selected, "
        f"{len(skipped_modules)} skipped by /settings/modules")

    units: list[tuple[str, object]] = [("settings", None)]
    for m in modules:
        units.append(("records", m))
        if not args.no_bulk:
            units.append(("bulk", m))
    units.append(("notes", None))
    # Attachments BEFORE emails, deliberately: attachments are one fast
    # module walk carrying most of the corpus bytes, whereas the email sweep
    # is one call per record and can run for many hours. Ordering it last
    # means a pass interrupted mid-emails still delivered the bulk.
    if not args.no_attachments:
        # ONE unit: a direct Attachments-module walk, not a per-record sweep
        units.append(("attachments", None))
    for m in email_modules(modules, args.no_emails, args.email_modules):
        units.append(("emails", m))
        units.append(("email_attachments", m))
    if args.only:
        units = [(k, m) for k, m in units
                 if (f"{k}/{m}" if m else k) == args.only]
        if not units:
            raise SystemExit(f"FATAL: --only {args.only} matched no unit")

    records_ok = records_attempted = 0
    for i, (kind, module) in enumerate(units, 1):
        label = f"{kind}/{module}" if module else kind
        write_progress(dest, "crm", "pull", i, len(units), label)
        log(f"[{i}/{len(units)}] {label}")
        if kind == "settings":
            res = pull_settings(api, dest, modules, args.refresh)
        elif kind == "records":
            res = pull_module_records(api, module, dest, args.refresh,
                                      args.since, args.page_size)
        elif kind == "bulk":
            res = pull_module_bulk(api, module, dest, args.refresh)
        elif kind == "notes":
            res = pull_notes(api, dest, args.refresh, args.page_size)
        elif kind == "emails":
            res = pull_emails(api, module, dest, args.refresh)
        elif kind == "email_attachments":
            res = pull_email_attachments(api, module, dest, args.refresh,
                                         args.attachment_workers)
        else:
            res = pull_attachments(api, dest, args.refresh,
                                   args.attachment_workers)
        if kind == "records":
            records_attempted += 1
            if res.get("status") in ("ok", "partial", "skipped-complete"):
                records_ok += 1
        _record_unit(results, res, state, dest, dest_url, sas,
                     args.skip_upload, args.refresh)
        if api.credits_exhausted:
            log("daily API credits look exhausted — stopping this pass. "
                "Cursors and markers make tomorrow a resumption, not a "
                "restart.")
            break
    if records_attempted and records_ok == 0:
        raise SystemExit(
            f"FATAL: all {records_attempted} record modules failed — this is "
            "systemic, not a per-module permission quirk. The scope list is "
            "almost certainly missing ZohoCRM.modules.ALL, or the token was "
            "revoked. Nothing useful was pulled; fix the credentials rather "
            "than re-running.")
    inaccessible = [r["unit"] for r in results
                    if r.get("kind") == "records"
                    and r.get("status") == "skipped"]
    if inaccessible:
        log(f"NOTE: {len(inaccessible)} of {records_attempted} record "
            f"modules were inaccessible and skipped: "
            f"{', '.join(x.split('/', 1)[-1] for x in inaccessible[:12])}"
            + (" ..." if len(inaccessible) > 12 else ""))
    context = {"modules_selected": len(modules),
               "modules_skipped": skipped_modules[:40],
               "record_modules_attempted": records_attempted,
               "record_modules_ok": records_ok,
               "record_modules_inaccessible": inaccessible}
    return results, context


def run_learn(api: ZohoAPI, dest: Path, args, dest_url: str,
              sas: str) -> tuple[list, dict]:
    results: list = []
    state = {"ok": 0, "failed": 0}
    meta = dest / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    discovery = learn_discover(api, args.portal, api.box.dc)
    atomic_write_json(meta / "discovery.json", discovery)
    log("learn: " + discovery["note"])
    write_progress(dest, "learn", "pull", 1, 2, "courses")
    _record_unit(results,
                 pull_learn_courses(api, args.portal, discovery, dest,
                                    args.refresh),
                 state, dest, dest_url, sas, args.skip_upload,
                 args.refresh)
    write_progress(dest, "learn", "pull", 2, 2, "kb")
    for res in pull_learn_kb(api, args.portal, discovery, dest,
                             args.refresh):
        _record_unit(results, res, state, dest, dest_url, sas,
                     args.skip_upload, args.refresh)
    return results, {"portal": args.portal,
                     "kb_reachable": discovery.get("kb_reachable"),
                     "kb_note": discovery.get("note")}


def run_workdrive(api: ZohoAPI, dest: Path, args, dest_url: str,
                  sas: str) -> tuple[list, dict]:
    results: list = []
    state = {"ok": 0, "failed": 0}
    meta = dest / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    boundary = workdrive_boundary(api)
    atomic_write_json(meta / "boundary.json", boundary)
    folders = boundary.get("folders") or []
    if args.limit:
        folders = folders[: args.limit]
    log(f"workdrive: {len(folders)} team folder(s) reachable")
    for i, folder in enumerate(folders, 1):
        write_progress(dest, "workdrive", "pull", i, len(folders),
                       folder.get("name") or folder["id"])
        _record_unit(results,
                     pull_workdrive_folder(api, folder, dest, args.refresh),
                     state, dest, dest_url, sas, args.skip_upload,
                 args.refresh)
    return results, {"teams": boundary.get("teams"),
                     "unreachable": boundary.get("unreachable"),
                     "boundary_note": boundary.get("note")}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="VM-side Zoho puller (zoho-azure-transfer)")
    ap.add_argument("--product", choices=["crm", "learn", "workdrive"],
                    required=True)
    ap.add_argument("--dest", required=True,
                    help="staging dir on the VM, e.g. ~/xfer-zoho/dest/crm")
    ap.add_argument("--portal", default=None,
                    help="Zoho Learn portal networkurl (--product learn)")
    ap.add_argument("--modules", default=None)
    ap.add_argument("--skip-modules", dest="skip_modules", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--skip-upload", dest="skip_upload", action="store_true")
    ap.add_argument("--no-attachments", dest="no_attachments",
                    action="store_true")
    ap.add_argument("--no-bulk", dest="no_bulk", action="store_true")
    ap.add_argument("--no-emails", dest="no_emails", action="store_true")
    ap.add_argument("--email-modules", dest="email_modules", default=None,
                    help="restrict the per-record email sweep to these "
                         "modules (comma-separated); everything else is "
                         "left without an emails unit")
    ap.add_argument("--attachment-workers", dest="attachment_workers",
                    type=int, default=ATTACHMENT_WORKERS)
    ap.add_argument("--page-size", dest="page_size", type=int,
                    default=PER_PAGE)
    ap.add_argument("--rate-sleep-max", dest="rate_sleep_max", type=int,
                    default=RATE_SLEEP_MAX)
    ap.add_argument("--since", default=None)
    args = ap.parse_args()

    dc = os.environ.get("ZOHO_DC", "").strip()
    client_id = os.environ.get("ZOHO_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ZOHO_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("ZOHO_REFRESH_TOKEN", "").strip()
    if not (dc and client_id and client_secret and refresh_token):
        log("FATAL: ZOHO_DC/ZOHO_CLIENT_ID/ZOHO_CLIENT_SECRET/"
            "ZOHO_REFRESH_TOKEN not all in environment (zoho.env not "
            "sourced?)")
        return 1
    dest_url = os.environ.get("AZURE_DEST_URL", "").strip()
    dest_sas = os.environ.get("AZURE_DEST_SAS", "").strip()
    if not args.skip_upload and not (dest_url and dest_sas):
        log("FATAL: AZURE_DEST_URL/AZURE_DEST_SAS not in environment "
            "(dest.env not sourced?)")
        return 1
    if args.product == "learn" and not args.portal:
        log("FATAL: --product learn requires --portal")
        return 1

    dest = Path(os.path.expanduser(args.dest))
    dest.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()

    box = TokenBox(dc, client_id, client_secret, refresh_token)
    box.mint()  # proves credentials + data center before anything else
    api = ZohoAPI(box, args.rate_sleep_max)

    driver = {"crm": run_crm, "learn": run_learn,
              "workdrive": run_workdrive}[args.product]
    results, context = driver(api, dest, args, dest_url, dest_sas)

    manifest = build_manifest(
        args.product, dc, box.api_domain, context, started,
        datetime.now(timezone.utc).isoformat(), api.calls, results)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    failed = manifest["failed_units"]
    log(f"SUMMARY: {len(results)} units, "
        f"{human_bytes(manifest['total_staged_bytes'])} staged, "
        f"{api.calls} api calls, {api.rate_sleeps} rate sleeps, "
        f"{len(failed)} failed {failed if failed else ''}")

    upload_ok = True
    if not args.skip_upload:
        # upload-what-succeeded: a final sweep is cheap because
        # --overwrite=false skips everything already landed per unit, and it
        # is what carries manifest.json and progress.json up
        write_progress(dest, args.product, "upload", len(results),
                       len(results), "azcopy final sweep")
        upload_ok = upload(dest, dest_url, dest_sas,
                           overwrite=args.refresh)
        # the manifest must REPLACE any earlier pass's — verify trusts it
        upload_ok = upload_run_metadata(dest, dest_url, dest_sas) and upload_ok
    write_progress(dest, args.product, "done", len(results), len(results),
                   f"{len(failed)} failed")
    return 2 if failed or not upload_ok else 0


if __name__ == "__main__":
    sys.exit(main())
