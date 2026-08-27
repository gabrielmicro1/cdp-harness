#!/usr/bin/env python3
"""Qwilr CSV-export -> Azure ingest: when the Qwilr API is unavailable, Qwilr
support can export a back-office CSV of every page in the account, each row
carrying a public/shareable URL, a collaborator URL (secret token -- works for
Drafts too) and a PDF download URL. This script drives that CSV: it fetches
each page's rendered HTML, triggers + collects Qwilr's async PDF renders, and
PUTs everything create-only into <slug>-raw/qwilr-export/.

Sibling of qwilr_transfer.py (the API path) -- same laptop-local shape, same
firewall (phases.ip_rule_ensure), same racwl SAS, same create-only writes,
same CLI contract: one JSON object on stdout, exit 0 ok / 1 hard / 2 refusal.
NO token or secret is needed: the CSV's links are themselves the capability
(collaborator URLs embed a secret; treat the CSV as sensitive).

Ground truth (probed live on song-division, 2026-08):
- public + /pdf/ URLs answer unauthenticated for Live/Accepted/Declined
  pages; Drafts 401 on both, but their collaborator URL answers 200.
- GET /pdf/<token> kicks off a server-side render and returns a loader page
  embedding a fresh download.qwilr.com/<uuid>.pdf; the PDF appears on S3
  minutes later (S3 says 403 AccessDenied until it exists). Every GET of
  /pdf/<token> starts a NEW render -- trigger once, then poll the uuid URL.

Subcommands:
  plan <slug>     read-only: parse the CSV, what would be pulled and where
  pull <slug>     the pull (resumable -- re-running skips landed blobs)
  verify <slug>   completeness: CSV rows vs blobs under the prefix (rl SAS)
"""
from __future__ import annotations

import csv
import html as html_mod
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import common
import phases
import qwilr_transfer as qt  # blob REST + SAS helpers (the API-path sibling)

DEFAULT_DEST_PREFIX = "qwilr-export"
DEFAULT_CSV_NAME = "qwilr-pages.csv"
USER_AGENT = "cdp-harness-qwilr-csv-pull/1 (client-authorized corpus export)"

# statuses whose public + /pdf/ URLs answer unauthenticated
PUBLIC_STATUSES = {"Live", "Accepted", "Declined"}

PDF_LOADER_RE = re.compile(
    r"downloadPdfPath&quot;:&quot;(https://[^&]+?\.pdf)")
ASSET_URL_RE = re.compile(r'https://[^\s"\'<>\\]+')
ASSET_HOSTS = ("qwilr.imgix.net", "cloudfront.net", "evs.cdp.qwilr.com")

_sleep = time.sleep      # seams so tests can record/skip waits
_now = time.monotonic    # and drive the trigger-pacing clock


class FetchError(Exception):
    """Non-fatal per-page fetch failure -- counted, not fatal."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class RateLimited(Exception):
    """Qwilr 429 on a render trigger -- REQUEUE with backoff, never count.
    Learned live: /pdf/ allows only a handful of renders per window; a
    trigger storm burns the whole queue in minutes if 429 is treated as a
    per-page failure."""


def _http(req: urllib.request.Request, timeout: int = 120):
    return urllib.request.urlopen(req, timeout=timeout)


def http_get(url: str, retries: int = 4, timeout: int = 120,
             ok_statuses: tuple[int, ...] = ()) -> tuple[int, bytes]:
    """GET a public URL. Returns (status, body). Statuses in ok_statuses are
    returned instead of raised (the PDF poll treats S3's 403 as not-ready);
    5xx/network retry with linear backoff; other 4xx raise FetchError."""
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with _http(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code in ok_statuses:
                return e.code, b""
            if e.code == 429:
                retry_after = (e.headers.get("Retry-After") or "").strip()
                last = e
                _sleep(int(retry_after) if retry_after.isdigit()
                       else min(15 * 2 ** attempt, 120))
                continue
            if e.code >= 500:
                last = e
                _sleep(2 + attempt)
                continue
            raise FetchError(e.code, f"HTTP {e.code} on {url.split('?')[0]}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(2 + attempt)
    raise FetchError(0, f"gave up on {url.split('?')[0]}: {last}")


# -- CSV ----------------------------------------------------------------------

def load_rows(csv_path: Path) -> list[dict]:
    if not csv_path.is_file():
        raise common.HarnessError(f"CSV not found: {csv_path}")
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"Page name", "Page ID", "Status", "Public/shareable URL",
                "Collaborator URL", "PDF download URL"}
    if not rows or not required.issubset(rows[0].keys()):
        raise common.HarnessError(
            f"{csv_path} is not a Qwilr pages export (missing "
            f"{sorted(required - set(rows[0].keys() if rows else []))})")
    ids = [r["Page ID"] for r in rows]
    if len(set(ids)) != len(ids):
        raise common.HarnessError(f"{csv_path} has duplicate Page IDs")
    return rows


def pdf_token(row: dict) -> str | None:
    """The share token, from the PDF URL (its cleanest home)."""
    m = re.search(r"/pdf/([A-Za-z0-9]+)\s*$", row["PDF download URL"].strip())
    return m.group(1) if m else None


def is_public(row: dict) -> bool:
    return (row["Status"] in PUBLIC_STATUSES
            and row.get("Password protected", "false") != "true")


def extract_asset_urls(page_html: bytes) -> list[str]:
    """CDN asset URLs embedded in a rendered page -- manifested, never
    downloaded (the PDFs flatten the images in anyway)."""
    text = html_mod.unescape(page_html.decode("utf-8", "replace"))
    found: set[str] = set()
    for url in ASSET_URL_RE.findall(text):
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            continue
        ext = ("." + parsed.path.rsplit(".", 1)[-1].lower()
               if "." in parsed.path else "")
        if (parsed.hostname or "").endswith(ASSET_HOSTS) \
                or ext in qt.ASSET_EXTS:
            found.add(url)
    return sorted(found)


# -- blob writes (content-typed; qwilr_transfer's PUT is JSON-only) -----------

def azure_put_bytes(cfg: dict, sas: str, name: str, body: bytes,
                    content_type: str, dry_run: bool) -> int:
    """Create-only single-shot Put Blob. Returns bytes uploaded; 0 = already
    existed (benign resume race). Largest body here is a ~tens-of-MB PDF --
    the single-put limit (~5000 MB) is far away."""
    if dry_run:
        print(f"DRY-RUN: PUT {qt._blob_url(cfg, name)}?<sas-redacted>  "
              f"({content_type}, If-None-Match: *, {len(body)} bytes)")
        return len(body)
    url = qt._blob_url(cfg, name) + "?" + sas
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="PUT", headers={
            "x-ms-version": qt.X_MS_VERSION,
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": content_type,
            "If-None-Match": "*",  # create-only, API-enforced
        })
        try:
            with _http(req, timeout=300) as r:
                r.read()
                return len(body)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 0  # BlobAlreadyExists -- resume artifact
            last = e
            if e.code == 403:
                _sleep(15 * (attempt + 1))  # IP-rule propagation, never re-mint
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise qt.PutError(f"PUT {name}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise qt.PutError(f"PUT {name} failed after retries: {last}")


def azure_list_sizes(cfg: dict, sas: str, prefix: str,
                     dry_run: bool) -> dict[str, int]:
    """Marker-paginated listing of the dest prefix, name -> size."""
    if dry_run:
        print(f"DRY-RUN: GET {qt._container_url(cfg)}"
              f"?restype=container&comp=list&prefix={prefix}/&<sas-redacted>")
        return {}
    sizes: dict[str, int] = {}
    marker = ""
    while True:
        url = (f"{qt._container_url(cfg)}?restype=container&comp=list"
               f"&maxresults=5000"
               f"&prefix={urllib.parse.quote(prefix + '/', safe='')}")
        if marker:
            url += f"&marker={urllib.parse.quote(marker, safe='')}"
        url += "&" + sas
        root = ET.fromstring(qt.azure_get(url))
        for blob in root.iter("Blob"):
            name = blob.findtext("Name")
            size = blob.findtext("Properties/Content-Length")
            if name:
                sizes[name] = int(size or 0)
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return sizes


# -- the PDF render pipeline --------------------------------------------------

def trigger_render(token: str) -> str:
    """GET /pdf/<token> once; parse the fresh download URL out of the loader
    page. Each GET starts a NEW server-side render -- never re-trigger to
    poll. 429 raises RateLimited so the pipeline requeues instead of
    counting an error."""
    status, body = http_get(f"https://pages.qwilr.com/pdf/{token}",
                            ok_statuses=(429,))
    if status == 429:
        raise RateLimited(token)
    m = PDF_LOADER_RE.search(body.decode("utf-8", "replace"))
    if not m:
        raise FetchError(status, f"/pdf/{token}: no downloadPdfPath in "
                                 "loader page")
    return m.group(1)


def poll_pdf(url: str) -> bytes | None:
    """One poll of the rendered-PDF URL. None = not ready yet (S3 answers
    403 AccessDenied until the render lands)."""
    status, body = http_get(url, retries=2, ok_statuses=(403,))
    if status == 403:
        return None
    if not body.startswith(b"%PDF"):
        raise FetchError(status, f"render at {url.split('?')[0]} is not a PDF")
    return body


TRIGGER_SPACING = 20      # seconds between successful triggers (politeness)
TRIGGER_BACKOFF_BASE = 60     # first 429 -> wait this long
TRIGGER_BACKOFF_CAP = 900     # exponential cap; the limiter window is opaque


def run_pdf_pipeline(cfg: dict, sas: str, jobs: list[tuple[str, str, str]],
                     concurrency: int, timeout_s: int, poll_s: int,
                     dry_run: bool) -> tuple[int, int, dict[str, str]]:
    """jobs: (page_id, share_token, dest_name). Keeps up to `concurrency`
    renders in flight; polls each until its PDF lands, then uploads it.
    Returns (pdfs_uploaded, bytes_uploaded, errors_by_page_id).

    Renders take minutes each and are Qwilr-side work -- the concurrency cap
    is politeness toward their render farm, not a laptop constraint. The
    /pdf/ endpoint rate-limits hard (learned live: ~5 renders, then a wall
    of 429s), so triggers are paced -- at most one per sweep, TRIGGER_SPACING
    apart -- and a 429 REQUEUES the job and backs off exponentially; only
    non-429 failures are counted as errors."""
    if dry_run:
        for _, tok, name in jobs[:1]:
            print(f"DRY-RUN: GET https://pages.qwilr.com/pdf/{tok} "
                  "(trigger render) -> poll download.qwilr.com/<uuid>.pdf")
            azure_put_bytes(cfg, sas, name, b"%PDF-placeholder",
                            "application/pdf", True)
        return 0, 0, {}

    queue = list(jobs)
    in_flight: dict[str, dict] = {}  # page_id -> {url, name, started}
    errors: dict[str, str] = {}
    uploaded = nbytes = 0
    done_total = len(jobs)
    backoff = TRIGGER_BACKOFF_BASE
    next_trigger_at = 0.0
    consecutive_429 = 0

    while queue or in_flight:
        now = _now()
        if queue and len(in_flight) < concurrency and now >= next_trigger_at:
            pid, tok, name = queue[0]
            try:
                url = trigger_render(tok)
                queue.pop(0)
                in_flight[pid] = {"url": url, "name": name,
                                  "started": _now()}
                backoff = TRIGGER_BACKOFF_BASE
                consecutive_429 = 0
                next_trigger_at = now + TRIGGER_SPACING
            except RateLimited:
                consecutive_429 += 1
                if consecutive_429 >= 12:  # >1.5h of pure waiting -- yield
                    for qpid, _, _ in queue:  # this pass; resume re-runs
                        errors[qpid] = ("trigger: rate-limited, gave up "
                                        "this pass (re-run pull later)")
                    queue.clear()
                    continue
                next_trigger_at = now + backoff
                print(f"progress pdf rate-limited, backing off {backoff}s "
                      f"(queue {len(queue)}, in-flight {len(in_flight)})",
                      file=sys.stderr, flush=True)
                backoff = min(backoff * 2, TRIGGER_BACKOFF_CAP)
            except FetchError as e:
                queue.pop(0)
                errors[pid] = f"trigger: {e}"
        _sleep(poll_s)
        for pid in list(in_flight):
            job = in_flight[pid]
            try:
                body = poll_pdf(job["url"])
            except FetchError as e:
                errors[pid] = f"poll: {e}"
                del in_flight[pid]
                continue
            if body is None:
                if _now() - job["started"] > timeout_s:
                    errors[pid] = (f"render timed out after {timeout_s}s "
                                   f"({job['url'].split('/')[-1]})")
                    del in_flight[pid]
                continue
            try:
                n = azure_put_bytes(cfg, sas, job["name"], body,
                                    "application/pdf", False)
                uploaded += 1
                nbytes += n
            except qt.PutError as e:
                errors[pid] = str(e)
            del in_flight[pid]
            done = uploaded + len(errors)
            print(f"progress pdf {done}/{done_total} "
                  f"(in-flight {len(in_flight)}, errors={len(errors)})",
                  file=sys.stderr, flush=True)
    return uploaded, nbytes, errors


# -- subcommands --------------------------------------------------------------

def _csv_path(root: Path, args) -> Path:
    if args.csv:
        return Path(args.csv)
    return root / args.slug / DEFAULT_CSV_NAME


def _classify(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    public = [r for r in rows if is_public(r)]
    draft = [r for r in rows if not is_public(r)]
    return public, draft


def cmd_plan(root: Path, args) -> dict:
    cfg = qt.load_cfg(root, args.slug)
    csv_path = _csv_path(root, args)
    rows = load_rows(csv_path)
    public, draft = _classify(rows)
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["Status"]] = statuses.get(r["Status"], 0) + 1
    return {
        "ok": True,
        "slug": args.slug,
        "mode": "local CSV-driven pull (no VM, no API token)",
        "csv": str(csv_path),
        "pages": len(rows),
        "statuses": statuses,
        "public_pages (html+pdf)": len(public),
        "restricted_pages (collaborator html only)": len(draft),
        "storage_account": cfg["storage_account"],
        "container": cfg["container"],
        "dest": f"{cfg['container']}/{args.dest_prefix}",
        "firewall": "per-run IP rule for this laptop's public IP, added and "
                    "removed by pull itself (phases.ip_rule_ensure)",
        "sas": f"container SAS, racwl, minted at pull time "
               f"({args.sas_days} day)",
        "will_write": [
            f"{args.dest_prefix}/pages/<pageId>/page.html (rendered page; "
            "collaborator render for restricted pages)",
            f"{args.dest_prefix}/pages/<pageId>/page.pdf (public pages only "
            "-- Qwilr's async render, minutes each)",
            f"{args.dest_prefix}/pages/<pageId>/metadata.json (the CSV row)",
            f"{args.dest_prefix}/_meta/{DEFAULT_CSV_NAME} (the export "
            "ledger itself -- carries analytics/acceptance data the API "
            "cannot export)",
            f"{args.dest_prefix}/_meta/pull-index-<ts>.json",
            f"{args.dest_prefix}/_meta/assets-manifest-<ts>.json (URLs only)",
        ],
        "note": "writes are create-only (If-None-Match: *); re-running pull "
                "skips blobs that already exist. PDF renders are ~3-5 min "
                "each on Qwilr's side; budget hours of wall clock.",
    }


def cmd_pull(root: Path, args) -> dict:
    cfg = qt.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    csv_path = _csv_path(root, args)
    rows = load_rows(csv_path)
    if args.limit:
        rows = rows[:args.limit]
    prefix = args.dest_prefix

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas, sas_expiry = qt.mint_write_sas(cfg, args.sas_days, args.dry_run)
        existing = set(azure_list_sizes(cfg, sas, prefix, args.dry_run))

        bytes_uploaded = 0
        html_written = html_skipped = 0
        errors: dict[str, str] = {}
        assets: dict[str, list[str]] = {}
        consecutive_failures = 0

        # the ledger itself, once (fixed name: one client export, one blob)
        ledger_name = f"{prefix}/_meta/{DEFAULT_CSV_NAME}"
        if ledger_name not in existing:
            bytes_uploaded += azure_put_bytes(
                cfg, sas, ledger_name, csv_path.read_bytes(), "text/csv",
                args.dry_run)

        # phase 1: HTML + metadata (seconds per page)
        for i, row in enumerate(rows, 1):
            pid = row["Page ID"]
            base = f"{prefix}/pages/{pid}"
            meta_name = f"{base}/metadata.json"
            if meta_name not in existing:
                try:
                    bytes_uploaded += qt.azure_put_json(
                        cfg, sas, meta_name,
                        {"csv_row": row, "fetched_via":
                         "public" if is_public(row) else "collaborator"},
                        args.dry_run)
                except qt.PutError as e:
                    errors[pid] = str(e)
            html_name = f"{base}/page.html"
            if html_name in existing:
                html_skipped += 1
            else:
                url = (row["Public/shareable URL"] if is_public(row)
                       else row["Collaborator URL"])
                try:
                    if args.dry_run:
                        if i == 1:
                            print(f"DRY-RUN: GET {url} -> PUT {html_name} "
                                  f"(per page)")
                        body = b"<html>placeholder</html>"
                    else:
                        status, body = http_get(url)
                        if status != 200 and is_public(row):
                            # a Live page can 401 if unpublished since the
                            # export -- the collaborator link still answers
                            status, body = http_get(row["Collaborator URL"])
                    page_assets = extract_asset_urls(body)
                    if page_assets:
                        assets[pid] = page_assets
                    n = azure_put_bytes(cfg, sas, html_name, body,
                                        "text/html", args.dry_run)
                    if n == 0:
                        html_skipped += 1
                    else:
                        html_written += 1
                        bytes_uploaded += n
                    consecutive_failures = 0
                except FetchError as e:
                    if e.status in (401, 404) and is_public(row):
                        try:
                            status, body = http_get(row["Collaborator URL"])
                            n = azure_put_bytes(cfg, sas, html_name, body,
                                                "text/html", args.dry_run)
                            html_written += 1 if n else 0
                            bytes_uploaded += n
                            consecutive_failures = 0
                            continue
                        except (FetchError, qt.PutError) as e2:
                            e = e2 if isinstance(e2, FetchError) else e
                    errors[pid] = f"html: {e}"
                    consecutive_failures += 1
                    if html_written == 0 and consecutive_failures >= 5:
                        raise common.HarnessError(
                            "first 5 page fetches/uploads all failed -- "
                            f"systemic, aborting: last error: {e}")
                except qt.PutError as e:
                    errors[pid] = f"html: {e}"
            if i % 10 == 0 or i == len(rows):
                print(f"progress html {i}/{len(rows)} pages, "
                      f"errors={len(errors)}", file=sys.stderr, flush=True)

        # phase 2: PDFs for public pages (minutes per render, batched)
        pdf_jobs: list[tuple[str, str, str]] = []
        pdf_skipped = 0
        for row in rows:
            if not is_public(row):
                continue
            pid = row["Page ID"]
            tok = pdf_token(row)
            name = f"{prefix}/pages/{pid}/page.pdf"
            if name in existing:
                pdf_skipped += 1
            elif tok is None:
                errors[pid] = "pdf: no token in PDF download URL"
            else:
                pdf_jobs.append((pid, tok, name))
        print(f"progress pdf phase: {len(pdf_jobs)} renders to run, "
              f"{pdf_skipped} already landed", file=sys.stderr, flush=True)
        pdf_written, pdf_bytes, pdf_errors = run_pdf_pipeline(
            cfg, sas, pdf_jobs, args.pdf_concurrency, args.pdf_timeout,
            args.pdf_poll_seconds, args.dry_run)
        bytes_uploaded += pdf_bytes
        for pid, msg in pdf_errors.items():
            errors[pid] = f"pdf: {msg}" if not msg.startswith("pdf") else msg

        ts = common.ts_basic()
        index = {
            "slug": args.slug,
            "pulled_at": common.iso_now(),
            "csv": csv_path.name,
            "pages_total": len(rows),
            "html_written": html_written,
            "html_skipped_existing": html_skipped,
            "pdf_written": pdf_written,
            "pdf_skipped_existing": pdf_skipped,
            "errors": errors,
        }
        bytes_uploaded += qt.azure_put_json(
            cfg, sas, f"{prefix}/_meta/pull-index-{ts}.json", index,
            args.dry_run)
        if assets:
            bytes_uploaded += qt.azure_put_json(
                cfg, sas, f"{prefix}/_meta/assets-manifest-{ts}.json",
                assets, args.dry_run)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    result = {
        "ok": True,  # verify is the completeness authority, not pull
        "dest": f"{cfg['container']}/{prefix}",
        "pages_total": len(rows),
        "html_written": html_written,
        "html_skipped_existing": html_skipped,
        "pdf_written": pdf_written,
        "pdf_skipped_existing": pdf_skipped,
        "errors": {"count": len(errors),
                   "by_page": dict(sorted(errors.items())[:50])},
        "assets_discovered": sum(len(v) for v in assets.values()),
        "bytes_uploaded": bytes_uploaded,
        "sas_expiry": sas_expiry,
        "ip_rule": "added-and-removed" if we_added else "not-needed",
    }
    if errors:
        result["resume_hint"] = ("re-run pull -- blobs that landed are "
                                 "skipped; only the failures re-fetch")
    return result


def cmd_verify(root: Path, args) -> dict:
    cfg = qt.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    csv_path = _csv_path(root, args)
    rows = load_rows(csv_path)
    prefix = args.dest_prefix

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl -- the READ path
        sizes = azure_list_sizes(cfg, sas, prefix, args.dry_run)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    missing_html = [r["Page ID"] for r in rows
                   if f"{prefix}/pages/{r['Page ID']}/page.html" not in sizes]
    missing_pdf = [r["Page ID"] for r in rows if is_public(r)
                  and f"{prefix}/pages/{r['Page ID']}/page.pdf" not in sizes]
    empty = sorted(n for n, s in sizes.items() if s == 0)
    csv_ids = {r["Page ID"] for r in rows}
    page_re = re.compile(re.escape(prefix) + r"/pages/([^/]+)/")
    blob_ids = {m.group(1) for n in sizes if (m := page_re.match(n))}
    extra = sorted(blob_ids - csv_ids)  # informational: not in this CSV

    result = {
        "ok": not missing_html and not missing_pdf and not empty,
        "pages_in_csv": len(rows),
        "public_pages": sum(1 for r in rows if is_public(r)),
        "blobs_in_container": len(sizes),
        "bytes_in_container": sum(sizes.values()),
        "ledger_csv_landed":
            f"{prefix}/_meta/{DEFAULT_CSV_NAME}" in sizes,
        "missing_html_count": len(missing_html),
        "missing_html": missing_html[:50],
        "missing_pdf_count": len(missing_pdf),
        "missing_pdf": missing_pdf[:50],
        "zero_byte_blobs": empty[:50],
        "extra_page_ids": extra[:50],
    }
    if missing_html or missing_pdf:
        result["hint"] = ("re-run pull -- resume skips what already landed; "
                          "only the missing pages re-fetch/re-render")
    return result


# -- CLI ----------------------------------------------------------------------

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["plan", "pull", "verify"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--csv", default=None,
                   help=f"path to the Qwilr pages export CSV (default "
                        f"companies/<slug>/{DEFAULT_CSV_NAME})")
    p.add_argument("--dest-prefix", default=DEFAULT_DEST_PREFIX)
    p.add_argument("--sas-days", type=int, default=1)
    p.add_argument("--limit", type=int, default=None,
                   help="pull only the first N CSV rows (live smoke tests)")
    p.add_argument("--pdf-concurrency", type=int, default=6,
                   help="max Qwilr renders in flight (politeness cap)")
    p.add_argument("--pdf-timeout", type=int, default=1500,
                   help="seconds before one render is declared dead")
    p.add_argument("--pdf-poll-seconds", type=int, default=15)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    root = Path(args.root)
    fn = {"plan": cmd_plan, "pull": cmd_pull, "verify": cmd_verify}[args.command]
    try:
        result = fn(root, args)
    except common.HarnessError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
