#!/usr/bin/env python3
"""Qwilr -> Azure ingest: pull a company's Qwilr account (pages, saved blocks,
account settings) via Qwilr's REST API and PUT it into <slug>-raw/qwilr-export/.

The harness's first VM-less ingest: the corpus is small JSON, so it runs
locally on the laptop. Standalone over common.py + phases (NOT built on
transfer_engine.py, which is rclone/VM-shaped), but keeps its CLI contract:
one JSON object on stdout, exit 0 ok / 1 hard error / 2 refusal.

Subcommands:
  plan <slug>     read-only: what would be pulled and where it lands
  pull <slug>     the pull (token on stdin; resumable -- re-running skips
                  blobs that already exist in the container)
  verify <slug>   completeness: fresh /pages listing vs blobs under the prefix

Hard rules (see the qwilr-azure-transfer SKILL.md):
- The Qwilr bearer token arrives on STDIN only -- never argv/env/files/logs.
- Writes are create-only (If-None-Match: *) under the dest prefix.
- Firewall via phases.ip_rule_ensure -- the laptop's external IP, same
  mechanism as sizing. allow-network is the VM path and does not apply.
- No PDF/HTML export exists in Qwilr's API; audit trail/analytics have no
  bulk export. Embedded CDN assets are manifested, not downloaded (v1).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path

import common
import phases

QWILR_BASE = "https://api.qwilr.com/v1"
X_MS_VERSION = "2021-08-06"
DEFAULT_DEST_PREFIX = "qwilr-export"

# blob basename -> API path; pulled once each, skip-if-exists like pages
ACCOUNT_ENDPOINTS = [
    ("users", "/users"),
    ("saved-blocks", "/blocks/saved"),
    ("taxes", "/taxes"),
    ("payment-gateways", "/payment-gateways"),
    ("webhooks", "/webhooks"),
]

ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
              ".mp4", ".webm", ".mov", ".pdf", ".ttf", ".woff", ".woff2"}

_sleep = time.sleep  # seam so tests can record/skip waits


def _http(req: urllib.request.Request, timeout: int = 90):
    """Single transport seam (tests stub this; production never branches)."""
    return urllib.request.urlopen(req, timeout=timeout)


class QwilrHTTPError(Exception):
    """Non-auth Qwilr API failure after retries -- counted, not fatal."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class PutError(Exception):
    """Blob upload failure after retries -- counted per page, not fatal."""


# -- Qwilr API ----------------------------------------------------------------

def qwilr_get(token: str, path: str, params: dict | None = None):
    """GET a Qwilr API path. 429 honors Retry-After (else exponential, cap
    32s); 5xx/network get linear backoff; 401/403 abort immediately -- a bad
    token cannot be retried away."""
    url = QWILR_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last: Exception | None = None
    for attempt in range(6):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        try:
            with _http(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise common.HarnessError(
                    f"Qwilr API {e.code} on {path} -- token invalid, revoked "
                    "or lacking access; get a fresh token (retrying cannot "
                    "help)")
            last = e
            if e.code == 429:
                retry_after = (e.headers.get("Retry-After") or "").strip()
                delay = (int(retry_after) if retry_after.isdigit()
                         else min(2 ** attempt, 32))
                _sleep(delay)
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise QwilrHTTPError(e.code, f"HTTP {e.code} on {path}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    status = getattr(last, "code", 0) or 0
    raise QwilrHTTPError(status, f"gave up on {path} after retries: {last}")


def _page_id(summary: dict) -> str | None:
    for key in ("id", "pageId", "_id"):
        if summary.get(key):
            return str(summary[key])
    return None


def _cursor_of(data: dict) -> str | None:
    return (data.get("cursor") or data.get("nextCursor")
            or (data.get("pagination") or {}).get("cursor"))


def list_pages(token: str, page_limit: int | None = None) -> list[dict]:
    """All page summaries via the cursor loop. A listing failure is FATAL
    (same philosophy as the sizer: per-item errors are counted, but a broken
    enumeration must never masquerade as a complete run)."""
    summaries: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            data = qwilr_get(token, "/pages", params)
        except QwilrHTTPError as e:
            raise common.HarnessError(f"Qwilr page listing failed: {e}")
        if isinstance(data, list):  # tolerate a bare-array response
            batch, cursor = data, None
        else:
            batch = data.get("data") or data.get("pages") or []
            cursor = _cursor_of(data)
        summaries.extend(batch)
        print(f"progress listed {len(summaries)} page summaries",
              file=sys.stderr, flush=True)
        if page_limit and len(summaries) >= page_limit:
            return summaries[:page_limit]
        if not cursor or not batch:
            return summaries


def extract_asset_urls(obj) -> set[str]:
    """Recursive walk for media URLs embedded in page JSON (Qwilr CDN hosts
    or media file extensions). v1 records them; nothing is downloaded."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            found |= extract_asset_urls(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= extract_asset_urls(v)
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        try:
            parsed = urllib.parse.urlparse(obj)
        except ValueError:
            return found
        ext = ("." + parsed.path.rsplit(".", 1)[-1].lower()
               if "." in parsed.path else "")
        if ext in ASSET_EXTS:
            found.add(obj)
    return found


# -- Azure blob REST ----------------------------------------------------------

def load_cfg(root: Path, slug: str) -> dict:
    try:
        return common.load_config(root, slug)
    except FileNotFoundError:
        raise common.HarnessError(
            f"{slug} is not onboarded (no companies/{slug}/config.json) -- "
            "run onboard-company first")


def mint_write_sas(cfg: dict, days: int, dry_run: bool) -> tuple[str, str]:
    """Container SAS, racwl (the sanctioned ingest write path). Default
    expiry is 1 day, not the VM transfers' 21: this SAS lives only in this
    process's memory -- there is no VM to outlive, and a resume re-mints."""
    expiry = common.iso(common.utc_now() + timedelta(days=days))
    proc = common.run_az(["storage", "container", "generate-sas",
                          "--account-name", cfg["storage_account"],
                          "-n", cfg["container"],
                          "--permissions", "racwl",
                          "--expiry", expiry, "--https-only",
                          "-o", "tsv"], dry_run=dry_run, timeout=120)
    return ("<sas-redacted>" if dry_run else proc.stdout.strip()), expiry


def _container_url(cfg: dict) -> str:
    return (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")


def _blob_url(cfg: dict, name: str) -> str:
    return _container_url(cfg) + "/" + urllib.parse.quote(name, safe="/")


def azure_get(url: str) -> bytes:
    """GET with retries. A 403 early in a run is usually IP-rule propagation
    (CLAUDE.md lore) -- wait and retry, never re-mint the SAS for it."""
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"x-ms-version": X_MS_VERSION})
        try:
            with _http(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                _sleep(15 * (attempt + 1))  # firewall propagation, not a bad SAS
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise common.HarnessError(f"blob GET failed: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise common.HarnessError(f"blob GET failed after retries: {last}")


def azure_list_existing(cfg: dict, sas: str, prefix: str,
                        dry_run: bool) -> set[str]:
    """One marker-paginated listing of the dest prefix -- both discovery and
    the resume seed (cheaper than per-blob HEADs)."""
    if dry_run:
        print(f"DRY-RUN: GET {_container_url(cfg)}"
              f"?restype=container&comp=list&prefix={prefix}/&<sas-redacted>")
        return set()
    names: set[str] = set()
    marker = ""
    while True:
        url = (f"{_container_url(cfg)}?restype=container&comp=list"
               f"&maxresults=5000"
               f"&prefix={urllib.parse.quote(prefix + '/', safe='')}")
        if marker:
            url += f"&marker={urllib.parse.quote(marker, safe='')}"
        url += "&" + sas
        root = ET.fromstring(azure_get(url))
        for blob in root.iter("Blob"):
            name = blob.findtext("Name")
            if name:
                names.add(name)
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return names


def build_put(url: str, body: bytes) -> urllib.request.Request:
    """Single-shot Put Blob, create-only. Page JSONs are KBs; the single-put
    limit at this API version (~5000 MB) is five orders of magnitude away."""
    return urllib.request.Request(url, data=body, method="PUT", headers={
        "x-ms-version": X_MS_VERSION,
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": "application/json",
        "If-None-Match": "*",  # create-only: the storage API enforces the
    })                         # "never modify existing data" invariant


def azure_put_json(cfg: dict, sas: str, name: str, obj,
                   dry_run: bool) -> int:
    """PUT one JSON blob. Returns bytes uploaded; 0 = already existed (a
    benign resume race -- the 409 is the If-None-Match doing its job)."""
    body = (json.dumps(obj, indent=2) + "\n").encode("utf-8")
    if dry_run:
        print(f"DRY-RUN: PUT {_blob_url(cfg, name)}?<sas-redacted>  "
              f"(x-ms-blob-type: BlockBlob, If-None-Match: *, "
              f"{len(body)} bytes)")
        return len(body)
    url = _blob_url(cfg, name) + "?" + sas
    last: Exception | None = None
    for attempt in range(4):
        try:
            with _http(build_put(url, body), timeout=90) as r:
                r.read()
                return len(body)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 0  # BlobAlreadyExists -- benign, resume artifact
            last = e
            if e.code == 403:
                _sleep(15 * (attempt + 1))  # propagation, never re-mint
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise PutError(f"PUT {name}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise PutError(f"PUT {name} failed after retries: {last}")


# -- token --------------------------------------------------------------------

def read_token(dry_run: bool) -> str:
    """The Qwilr bearer token arrives on stdin ONLY (heredoc) -- argv is
    world-readable via ps, env leaks into child processes, files persist."""
    data = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    if data:
        return data
    if dry_run:
        return "<token>"
    raise common.HarnessError(
        "no Qwilr token on stdin -- pipe it: pull <slug> <<'EOF' ... EOF")


# -- subcommands --------------------------------------------------------------

def cmd_plan(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    return {
        "ok": True,
        "slug": args.slug,
        "mode": "local-pull (no VM, no rclone)",
        "storage_account": cfg["storage_account"],
        "container": cfg["container"],
        "dest": f"{cfg['container']}/{args.dest_prefix}",
        "source": "Qwilr REST API (api.qwilr.com/v1); bearer token on stdin "
                  "at pull time",
        "sas": "container SAS, racwl, minted at pull time",
        "sas_days": args.sas_days,
        "firewall": "per-run IP rule for this laptop's public IP, added and "
                    "removed by pull itself (phases.ip_rule_ensure)",
        "will_write": [
            f"{args.dest_prefix}/pages/<pageId>.json (full page JSON)",
            f"{args.dest_prefix}/account/{{{', '.join(n for n, _ in ACCOUNT_ENDPOINTS)}}}.json",
            f"{args.dest_prefix}/_meta/pages-index-<ts>.json",
            f"{args.dest_prefix}/_meta/assets-manifest-<ts>.json (URLs only)",
        ],
        "note": "writes are create-only (If-None-Match: *); re-running pull "
                "skips blobs that already exist. No PDF/audit-trail export "
                "exists in Qwilr's API.",
    }


def cmd_pull(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    token = read_token(args.dry_run)
    prefix = args.dest_prefix

    # Validate the token BEFORE touching the firewall -- fail fast rather
    # than after the 60s propagation sleep.
    if args.dry_run:
        print("DRY-RUN: GET https://api.qwilr.com/v1/pages?limit=1 "
              "(Authorization: Bearer <token-redacted>)")
    else:
        try:
            qwilr_get(token, "/pages", {"limit": 1})
        except QwilrHTTPError as e:
            raise common.HarnessError(f"Qwilr token validation failed: {e}")

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas, sas_expiry = mint_write_sas(cfg, args.sas_days, args.dry_run)
        existing = azure_list_existing(cfg, sas, prefix, args.dry_run)

        if args.dry_run:
            print("DRY-RUN: GET https://api.qwilr.com/v1/pages?limit=100 "
                  "(cursor loop until exhausted)")
            print("DRY-RUN: GET https://api.qwilr.com/v1/pages/<pageId> "
                  "(per page)")
            summaries = []
        else:
            summaries = list_pages(token, args.page_limit)

        pulled = skipped = bytes_uploaded = 0
        errors: dict[str, str] = {}
        assets: dict[str, list[str]] = {}
        consecutive_failures = 0

        for i, summary in enumerate(summaries, 1):
            pid = _page_id(summary)
            if pid is None:
                errors["(no-id)"] = f"summary without an id field: " \
                                    f"{str(summary)[:120]}"
                continue
            name = f"{prefix}/pages/{pid}.json"
            if name in existing:
                skipped += 1
                continue
            try:
                detail = qwilr_get(token, f"/pages/{pid}")
                urls = extract_asset_urls(detail)
                if urls:
                    assets[pid] = sorted(urls)
                n = azure_put_json(cfg, sas, name, detail, args.dry_run)
                if n == 0:
                    skipped += 1  # 409: landed in a previous crashed run
                else:
                    bytes_uploaded += n
                    pulled += 1
                consecutive_failures = 0
            except (QwilrHTTPError, PutError) as e:
                errors[pid] = str(e)
                consecutive_failures += 1
                if pulled == 0 and consecutive_failures >= 5:
                    raise common.HarnessError(
                        "first 5 page uploads all failed -- systemic "
                        f"(SAS/firewall/API), aborting: last error: {e}")
            if i % 25 == 0 or i == len(summaries):
                print(f"progress {i}/{len(summaries)} pages, "
                      f"errors={len(errors)}", file=sys.stderr, flush=True)

        # one placeholder PUT in dry-run so the plan of record shows the
        # exact request shape (headers, create-only) without any network
        if args.dry_run:
            azure_put_json(cfg, sas, f"{prefix}/pages/<pageId>.json",
                           {"placeholder": True}, True)

        account_results: dict[str, str] = {}
        for basename, api_path in ACCOUNT_ENDPOINTS:
            name = f"{prefix}/account/{basename}.json"
            if name in existing:
                account_results[basename] = "skipped-existing"
                continue
            try:
                if args.dry_run:
                    print(f"DRY-RUN: GET https://api.qwilr.com/v1{api_path} "
                          "(Authorization: Bearer <token-redacted>)")
                    data = {"placeholder": True}
                else:
                    data = qwilr_get(token, api_path)
                n = azure_put_json(cfg, sas, name, data, args.dry_run)
                account_results[basename] = ("skipped-existing" if n == 0
                                             else "written")
                bytes_uploaded += n
            except (QwilrHTTPError, PutError) as e:
                account_results[basename] = f"error: {e}"

        ts = common.ts_basic()
        index = {
            "slug": args.slug,
            "pulled_at": common.iso_now(),
            "pages_total": len(summaries),
            "pulled": pulled,
            "skipped_existing": skipped,
            "page_errors": errors,
            "account_endpoints": account_results,
            "summaries": summaries,
        }
        bytes_uploaded += azure_put_json(
            cfg, sas, f"{prefix}/_meta/pages-index-{ts}.json", index,
            args.dry_run)
        if assets:
            bytes_uploaded += azure_put_json(
                cfg, sas, f"{prefix}/_meta/assets-manifest-{ts}.json",
                assets, args.dry_run)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    result = {
        "ok": True,  # verify is the completeness authority, not pull
        "dest": f"{cfg['container']}/{prefix}",
        "pages_total": len(summaries),
        "pulled": pulled,
        "skipped_existing": skipped,
        "page_errors": {"count": len(errors),
                        "ids": sorted(errors)[:50]},
        "account_endpoints": account_results,
        "assets_discovered": sum(len(v) for v in assets.values()),
        "bytes_uploaded": bytes_uploaded,
        "sas_expiry": sas_expiry,
        "ip_rule": "added-and-removed" if we_added else "not-needed",
    }
    if errors:
        result["resume_hint"] = ("re-run pull -- blobs that landed are "
                                 "skipped; only the failed pages re-fetch")
    if assets:
        result["note"] = ("embedded CDN assets were manifested "
                          "(_meta/assets-manifest), NOT downloaded")
    return result


def cmd_verify(root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    token = read_token(args.dry_run)
    prefix = args.dest_prefix

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl -- the READ path
        blob_names = azure_list_existing(cfg, sas, prefix, args.dry_run)
        if args.dry_run:
            print("DRY-RUN: GET https://api.qwilr.com/v1/pages?limit=100 "
                  "(cursor loop, Authorization: Bearer <token-redacted>)")
            summaries = []
        else:
            summaries = list_pages(token)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    api_ids = {p for p in (_page_id(s) for s in summaries) if p}
    page_prefix = f"{prefix}/pages/"
    blob_ids = {n[len(page_prefix):-len(".json")] for n in blob_names
                if n.startswith(page_prefix) and n.endswith(".json")}
    missing = sorted(api_ids - blob_ids)
    extra = sorted(blob_ids - api_ids)
    account_blobs = {basename: f"{prefix}/account/{basename}.json" in blob_names
                     for basename, _ in ACCOUNT_ENDPOINTS}
    index_blobs = sum(1 for n in blob_names
                      if n.startswith(f"{prefix}/_meta/pages-index-"))

    result = {
        "ok": not missing,
        "pages_in_qwilr": len(api_ids),
        "pages_in_container": len(blob_ids),
        "missing_count": len(missing),
        "missing": missing[:50],
        "extra": extra[:50],  # informational: deleted in Qwilr since pull
        "account_blobs": account_blobs,
        "index_blobs": index_blobs,
    }
    if missing:
        result["hint"] = ("re-run pull -- resume skips what already landed; "
                          "only the missing pages re-fetch")
    return result


# -- CLI ----------------------------------------------------------------------

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["plan", "pull", "verify"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--dest-prefix", default=DEFAULT_DEST_PREFIX,
                   help=f"prefix inside <slug>-raw (default "
                        f"{DEFAULT_DEST_PREFIX})")
    p.add_argument("--sas-days", type=int, default=1,
                   help="write-SAS expiry (default 1 -- lives only in this "
                        "process; a resume re-mints)")
    p.add_argument("--page-limit", type=int, default=None,
                   help="pull only the first N pages (live smoke tests)")
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
