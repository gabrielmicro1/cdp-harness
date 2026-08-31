#!/usr/bin/env python3
"""Saxon SharePoint completion CLI — a ONE-OFF for saxon, not an engine
family (the slug guard below enforces it). The client's own export tool
pushed ~479 site folders into sharepoint/ and left some incomplete; this
tool completes exactly those folders from Microsoft Graph and never starts
a site the client didn't. See scripts/saxon_sp_vm_pull.py (the VM puller)
for the copy semantics — the one-line version: create-only server-side
copies of files whose exact dest path is absent, gated by a path-convention
calibration check, with zero bookkeeping written into the container.

Reuses transfer_engine.py's VM lifecycle verbatim (create-vm /
allow-network / check-azure / teardown — the teams/github/zoho/figma
precedent) on VM `xfer-sp-saxon` in saxon's RG. The VM is mandatory here
twice over: the user asked for it, and saxon's provisioner strips manual SA
IP rules in ~90s, so the engine's vnet-rule grant is the only reliable
write path (the laptop rl read path used by plan/verify survives because
ip_rule_ensure re-adds and a listing is minutes).

Auth: the client's read-only Entra app "Relay Export 2" (app-only
client-credentials, Sites.Read.All + Files.Read.All). THREE secrets on
stdin, in order: tenant id, client id, client secret — ssh stdin into a
600 env file on the VM, never argv/tags/logs/laptop files (the teams
plumbing, duplicated here per the VM-family convention).

Workflow (the mapping-approval gate is the extra step vs teams):
  probe → plan → [user reviews mapping.json] → approve-mapping →
  create-vm → allow-network → write-dest → check-azure → write-creds →
  transfer --diff-only (pilot 1: measured missing set + calibration gate)
  → transfer --only-site <small site> (pilot 2: real copies e2e) →
  transfer (full) → status → harvest → verify → teardown --confirmed

`plan` snapshots the in-scope folder set (everything under sharepoint/ at
that moment — the frozen scope), enumerates the tenant's sites via Graph,
and auto-maps folder→site-collection: exact slug match, GUID fallback,
sanitized-display-name fallback; anything else is skip-ambiguous and stays
excluded until the user edits mapping.json and re-runs approve-mapping.
Two folders resolving to one collection (Connect4-0/Connect4.0,
e-Docs/eDocs) keep the byte-heavier folder as the completion target.
Calibration sites (believed complete: census-walked, existing bytes >=
the census's exact bytes) are marked so the puller can prove the path
convention before any copy.

State lives under companies/saxon/sharepoint-completion/ (gitignored):
mapping.json, then run-<ts>/{state.json,results.jsonl,run-summary.json,
manifests/} pulled back by `harvest` — which is a hard gate before
teardown, because the VM copy is the only one. `verify` (laptop, VM may be
gone) merges the harvested per-site manifests against a fresh container
listing: complete = every action=complete site has missing == 0.
"""
from __future__ import annotations

import gzip
import io
import json
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import phases  # noqa: E402
import transfer_engine as eng  # noqa: E402
import saxon_sp_vm_pull as puller  # noqa: E402  (import-safe; pure helpers)

ONLY_SLUG = "saxon"   # one-off-ness enforced in code, not just in the name

SPEC = eng.Spec(
    source_name="sp",
    vm_prefix="xfer-sp-",
    purpose="saxon-sp-complete",
    loc_tag="sp_tenant_id",
    loc_argname="tenant_id",
    loc_required=True,
    default_dest_prefix="sharepoint",   # the client's OWN prefix — the point
    authorize_target="",
    remote_type="",
    extra_cli_opts=[],
    default_os_disk_gb=64,   # streamed-fallback headroom only, never staging
)

PULLER_PY = Path(__file__).resolve().parent / "saxon_sp_vm_pull.py"
XFER_DIR = f"/home/{eng.ADMIN_USER}/xfer-sp"
LOG_FILE = f"{XFER_DIR}/pull-sp.log"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/xfer"
SP_ENV = f"{ENV_DIR}/sp.env"
DEST_ENV = f"{ENV_DIR}/dest-sp.env"
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
X_MS_VERSION = "2021-08-06"

CENSUS_DIR = (Path(__file__).resolve().parent.parent
              / "companies" / "saxon" / "reports"
              / "sharepoint-census-20260827")
CAL_MAX_SITES = 5
CAL_MIN_BYTES = 1_000_000_000   # a calibration site must be big enough to
                                # prove the convention across many paths

_sleep = time.sleep  # seam so tests can record/skip waits


def _http(req: urllib.request.Request, timeout: int = 90):
    return urllib.request.urlopen(req, timeout=timeout)


def state_dir(root: Path) -> Path:
    return root / ONLY_SLUG / "sharepoint-completion"


def guard_slug(slug: str) -> None:
    if slug != ONLY_SLUG:
        raise common.HarnessError(
            f"this is a ONE-OFF tool for {ONLY_SLUG!r} only (its dest-path "
            "convention, census calibration and mapping logic are saxon-"
            f"specific) — refusing to run against {slug!r}")


def validate_tenant_id(raw: str) -> str:
    t = (raw or "").strip()
    if not GUID_RE.match(t):
        raise common.HarnessError(
            f"tenant id {t!r} is not a GUID — copy the Directory (tenant) "
            "ID from the client's Entra app registration page")
    return t.lower()


def validate_client_id(raw: str) -> str:
    c = (raw or "").strip()
    if not GUID_RE.match(c):
        raise common.HarnessError(
            f"client id {c!r} is not a GUID — copy the Application "
            "(client) ID from the client's Entra app registration page")
    return c


def read_secrets(dry_run: bool) -> tuple[str, str, str]:
    """Exactly 3 stdin lines: tenant id, client id, client secret (the
    teams/zoom convention). Stdin only — argv is world-readable via ps."""
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
        "secret — pipe them: <cmd> saxon <<'EOF' ... EOF")


def _tenant_guard(stdin_tenant: str, expected: str | None) -> None:
    """PURE. The teams/zoho wrong-tenant guard: refuse credentials whose
    tenant contradicts --tenant-id or the VM's sp_tenant_id tag."""
    if expected and expected != stdin_tenant:
        raise common.HarnessError(
            f"tenant mismatch: stdin declares tenant {stdin_tenant!r} but "
            f"the expected tenant is {expected!r} (from --tenant-id, or "
            f"the VM's {SPEC.loc_tag} tag). Nothing was written.")


# ── laptop Graph (probe + plan; teams plumbing, duplicated per convention) ───

class TokenBox:
    """Laptop twin of the puller's TokenBox — raises HarnessError where
    the VM twin raises SystemExit."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self._tenant = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._value: str | None = None
        self._exp = 0.0

    def get(self) -> str:
        if (self._value
                and time.time() < self._exp - puller.TOKEN_REFRESH_MARGIN):
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
        url = puller.TOKEN_PATH_FMT.format(login=puller.LOGIN,
                                           tenant=self._tenant)
        last = None
        for attempt in range(1, puller.API_RETRIES + 2):
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type":
                         "application/x-www-form-urlencoded"})
            try:
                with _http(req, timeout=60) as r:
                    body = json.loads(r.read().decode())
                tok = body.get("access_token")
                if not tok:
                    raise common.HarnessError(
                        f"token mint returned no access_token: "
                        f"{str(body)[:300]}")
                self._value = tok
                self._exp = time.time() + int(body.get("expires_in", 3599))
                return tok
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", "replace")[:300]
                if e.code in (400, 401):
                    raise common.HarnessError(
                        f"token mint refused ({e.code}): {err} — check the "
                        "3 stdin values (tenant, client id, secret)")
                last = f"{e.code}: {err}"
            except (urllib.error.URLError, TimeoutError) as e:
                last = str(e)
            _sleep(min(60, 5 * attempt))
        raise common.HarnessError(f"token mint failed after retries: {last}")


def graph_get(token: str | None, path_or_url: str,
              params: dict | None = None, dry_run: bool = False):
    """One GET -> (status, parsed-json-or-None); never raises on an HTTP
    error status (plan/probe classify those themselves)."""
    url = (path_or_url if path_or_url.startswith("http")
           else f"{puller.GRAPH}{path_or_url}")
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="$()/'! ,=")
    if dry_run:
        print(f"DRY-RUN: GET {url} (Authorization: Bearer token-redacted)")
        return 0, None
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"})
        try:
            with _http(req, timeout=90) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                payload = json.loads(body) if body else None
            except ValueError:
                payload = None
            if e.code == 429:
                ra = (e.headers.get("Retry-After") or "").strip()
                _sleep(int(ra) if ra.isdigit() else min(2 ** attempt, 30))
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise common.HarnessError(
        f"Graph GET failed after retries on {path_or_url}: {last}")


def list_all_sites(token: str) -> list[dict]:
    """Full tenant site enumeration (webs included), the census's method.
    A refused first page is fatal to plan — there is nothing to map
    without it."""
    status, body = graph_get(token, "/sites",
                             {"search": "*",
                              "$select": "id,webUrl,displayName,name"})
    if status != 200 or body is None:
        raise common.HarnessError(
            f"/sites?search=* refused ({status}) — the app is missing "
            "Sites.Read.All application permission or admin consent")
    sites = list(body.get("value", []))
    nxt = body.get("@odata.nextLink")
    while nxt:
        status, body = graph_get(token, nxt)
        if status != 200 or body is None:
            raise common.HarnessError(
                f"/sites pagination refused ({status}) — a partial site "
                "list would silently unmap folders; re-run plan")
        sites += body.get("value", [])
        nxt = body.get("@odata.nextLink")
    return sites


# ── azure listing (plan + verify; laptop rl path) ────────────────────────────

def _container_url(cfg: dict) -> str:
    return (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")


def azure_get(url: str) -> bytes:
    """GET with retries; 403 = IP-rule propagation (or saxon's provisioner
    stripping the rule mid-listing) — wait and retry, never re-mint."""
    last: Exception | None = None
    for attempt in range(5):
        req = urllib.request.Request(url,
                                     headers={"x-ms-version": X_MS_VERSION})
        try:
            with _http(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                _sleep(20 * (attempt + 1))
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise common.HarnessError(f"blob GET failed: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise common.HarnessError(f"blob GET failed after retries: {last}")


def list_prefix_by_folder(cfg: dict, sas: str, prefix: str,
                          dry_run: bool) -> dict[str, dict[str, int]]:
    """One marker-paginated listing of <prefix>/ -> {folder: {relpath:
    size}} — the laptop twin of the puller's build_dest_index (~300
    pages for saxon's 1.4M sharepoint blobs; minutes)."""
    if dry_run:
        print(f"DRY-RUN: GET {_container_url(cfg)}"
              f"?restype=container&comp=list&prefix={prefix}/"
              "&<sas-redacted> (marker-paginated)")
        return {}
    index: dict[str, dict[str, int]] = {}
    marker = ""
    total = 0
    plen = len(prefix) + 1
    while True:
        url = (f"{_container_url(cfg)}?restype=container&comp=list"
               f"&maxresults=5000"
               f"&prefix={urllib.parse.quote(prefix + '/', safe='')}")
        if marker:
            url += f"&marker={urllib.parse.quote(marker, safe='')}"
        url += "&" + sas
        root = ET.fromstring(azure_get(url))
        for blob in root.iter("Blob"):
            name = blob.findtext("Name") or ""
            props = blob.find("Properties")
            size = int((props.findtext("Content-Length") or 0)
                       if props is not None else 0)
            rest = name[plen:]
            folder, sep, rel = rest.partition("/")
            if not sep or not rel:
                continue
            index.setdefault(folder, {})[rel] = size
            total += 1
            if total % 200_000 == 0:
                print(f"progress listed {total} blobs...", file=sys.stderr,
                      flush=True)
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return index


def folder_stats(files: dict[str, int]) -> tuple[int, int, int]:
    """PURE. (real files, real bytes, sidecars) — sidecars are the old push
    era's `.meta.json` companions, excluded from the byte tie-breaks."""
    rf = rb = mf = 0
    for rel, size in files.items():
        if rel.endswith(".meta.json"):
            mf += 1
        else:
            rf += 1
            rb += size
    return rf, rb, mf


# ── mapping (plan's core; pure so tests can drive it) ────────────────────────

def _sanitize(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def build_collections(sites: list[dict]) -> dict[str, dict]:
    """PURE. Graph site rows -> {collection key: {url, slug, site_ids,
    names}}; personal (-my.sharepoint.com) sites are out of scope."""
    colls: dict[str, dict] = {}
    for s in sites:
        web_url = s.get("webUrl") or ""
        if "-my.sharepoint.com" in web_url:
            continue
        key = puller.sitecoll(web_url)
        c = colls.setdefault(key, {
            "url": key, "slug": key.rsplit("/", 1)[-1],
            "site_ids": [], "names": []})
        if s.get("id"):
            c["site_ids"].append(s["id"])
        nm = s.get("displayName") or s.get("name") or ""
        if nm:
            c["names"].append(nm)
    return colls


def match_folders(folders: dict[str, tuple], colls: dict[str, dict]) -> list:
    """PURE. folder -> collection rows. folders: {name: (real_files,
    real_bytes, sidecars)}. Matching: exact slug (the convention the
    client's tool actually used, per match.json's 479/479) -> site-id GUID
    -> sanitized display name -> ambiguous. Multi-folder collections keep
    the byte-heavier folder as the completion target."""
    by_slug = {c["slug"].lower(): k for k, c in colls.items()}
    by_guid: dict[str, str] = {}
    for k, c in colls.items():
        for sid in c["site_ids"]:
            for part in sid.split(","):
                part = part.strip().lower()
                if GUID_RE.match(part):
                    by_guid.setdefault(part, k)
    by_name: dict[str, list[str]] = {}
    for k, c in colls.items():
        for nm in c["names"]:
            by_name.setdefault(_sanitize(nm), []).append(k)

    rows = []
    for folder, (rf, rb, mf) in sorted(folders.items()):
        key = by_slug.get(folder.lower())
        confidence = "exact"
        if key is None:
            key = by_guid.get(folder.lower())
            confidence = "guid"
        if key is None:
            cands = by_name.get(_sanitize(folder)) or []
            if len(set(cands)) == 1:
                key = cands[0]
                confidence = "display-name"
        if key is None:
            rows.append({"folder": folder, "site_url": None,
                         "site_ids": [], "confidence": "none",
                         "existing_files": rf, "existing_bytes": rb,
                         "sidecars": mf, "action": "skip-ambiguous",
                         "why": "no slug/guid/display-name match"})
            continue
        c = colls[key]
        rows.append({"folder": folder, "site_url": c["url"],
                     "site_ids": c["site_ids"], "confidence": confidence,
                     "existing_files": rf, "existing_bytes": rb,
                     "sidecars": mf, "action": "complete"})

    # two folders -> one collection: byte-heavier folder wins
    by_coll: dict[str, list[dict]] = {}
    for r in rows:
        if r["site_url"]:
            by_coll.setdefault(r["site_url"], []).append(r)
    for url, group in by_coll.items():
        if len(group) > 1:
            group.sort(key=lambda r: -r["existing_bytes"])
            for r in group[1:]:
                r["action"] = "skip-duplicate-target"
                r["why"] = (f"same collection as folder "
                            f"{group[0]['folder']!r} (byte-heavier target)")
    return rows


def mark_calibration(rows: list, census_walk: dict) -> list[str]:
    """Mark up to CAL_MAX_SITES rows whose folder the 08-27 census walked
    AND whose container bytes now meet/exceed the census's exact source
    bytes — the sites we most believe are complete, i.e. the ones that can
    prove the path convention. Also stamps order_bytes on every row."""
    cands = []
    for r in rows:
        slug = (r.get("site_url") or "").rsplit("/", 1)[-1]
        walked = census_walk.get(slug)
        census_bytes = (walked or {}).get("bytes") or 0
        r["order_bytes"] = max(r["existing_bytes"], census_bytes)
        if (r["action"] == "complete" and walked
                and census_bytes >= CAL_MIN_BYTES
                and r["existing_bytes"] >= census_bytes):
            cands.append((census_bytes, r))
    cands.sort(key=lambda x: -x[0])
    marked = []
    for _b, r in cands[:CAL_MAX_SITES]:
        r["calibrate"] = True
        marked.append(r["folder"])
    return marked


def load_census() -> tuple[dict, dict]:
    """(walk by slug, match.json folder->siteUrl) from the preserved
    census; empty dicts when absent (plan warns — no calibration means the
    puller refuses to copy, which is the safe failure)."""
    walk: dict[str, dict] = {}
    match: dict[str, str] = {}
    wpath = CENSUS_DIR / "walk-results.jsonl"
    if wpath.exists():
        for ln in wpath.read_text().splitlines():
            try:
                row = json.loads(ln)
                walk[row["slug"]] = row
            except (ValueError, KeyError):
                pass
    mpath = CENSUS_DIR / "match.json"
    if mpath.exists():
        try:
            for folder, url, _b, _f in json.loads(
                    mpath.read_text())["rows"]:
                match[folder] = url
        except (ValueError, KeyError, TypeError):
            pass
    return walk, match


# ── VM plumbing (teams shapes) ───────────────────────────────────────────────

def _write_env(ip: str, path: str, content: str, dry_run: bool) -> None:
    eng.run_ssh(ip, f"umask 077 && mkdir -p {ENV_DIR} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _push_puller(ip: str, dry_run: bool) -> None:
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > "
                    f"{XFER_DIR}/saxon_sp_vm_pull.py",
                stdin_data=PULLER_PY.read_text(), dry_run=dry_run)


def _push_mapping(ip: str, mapping_path: Path, dry_run: bool) -> None:
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > {XFER_DIR}/mapping.json",
                stdin_data=mapping_path.read_text(), dry_run=dry_run)


def _load_mapping(root: Path) -> dict:
    path = state_dir(root) / "mapping.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise common.HarnessError(
            f"no readable mapping at {path} — run plan first")


def _require_approved(root: Path) -> dict:
    mapping = _load_mapping(root)
    if not mapping.get("approved_utc"):
        raise common.HarnessError(
            "mapping.json is not approved — review it (especially "
            "skip-ambiguous rows), then run approve-mapping")
    return mapping


# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_probe(root: Path, args) -> dict:
    """Day-one gate, laptop-only, before any VM: token mints, sites
    enumerate, drives list, a delta page answers, a file's downloadUrl
    resolves on SharePoint hosts and honors Range (the Put Block From URL
    prerequisite). No Azure calls."""
    eng.load_cfg(root, args.slug)
    tenant, client_id, secret = read_secrets(args.dry_run)
    if args.dry_run:
        graph_get(None, "/sites", {"search": "*"}, dry_run=True)
        return {"ok": True, "dry_run": True,
                "note": "no network I/O under --dry-run"}
    box = TokenBox(validate_tenant_id(tenant),
                   validate_client_id(client_id), secret)
    token = box.mint()
    status, body = graph_get(token, "/sites",
                             {"search": "*",
                              "$select": "id,webUrl,displayName"})
    if status != 200 or not body:
        return {"ok": False, "gate": f"sites-refused-{status}",
                "next_step": "Sites.Read.All application permission or "
                             "admin consent is missing"}
    sites = [s for s in body.get("value", [])
             if "/sites/" in (s.get("webUrl") or "")
             and "-my.sharepoint.com" not in (s.get("webUrl") or "")]
    if not sites:
        return {"ok": False, "gate": "no-sites-visible",
                "next_step": "the app sees no team sites — wrong tenant?"}
    # hunt a real FILE item across the first few sites/drives — many
    # libraries are empty at their root (found live: ProjectDocs/Alturki),
    # and the downloadUrl+Range legs need actual bytes to probe against
    probe_site = sites[0]
    drive = item = None
    delta_ok = drives_ok = False
    for site in sites[:5]:
        dstatus, drives = graph_get(token, f"/sites/{site['id']}/drives",
                                    {"$top": "10"})
        if dstatus != 200 or not (drives or {}).get("value"):
            continue
        drives_ok = True
        for d in drives["value"]:
            wstatus, page = graph_get(
                token, f"/drives/{d['id']}/root/delta",
                {"$select": "id,name,size,file,parentReference",
                 "$top": "200"})
            pages = 0
            while wstatus == 200 and page is not None and pages < 3:
                delta_ok = True
                item = next((it for it in page.get("value", [])
                             if "file" in it), None)
                if item is not None:
                    break
                nxt = page.get("@odata.nextLink")
                if not nxt:
                    break
                wstatus, page = graph_get(token, nxt)
                pages += 1
            if item is not None:
                drive, probe_site = d, site
                break
        if item is not None:
            break
    if not drives_ok:
        return {"ok": False, "gate": "drives-refused",
                "next_step": "Files.Read.All application permission or "
                             "admin consent is missing"}
    if not delta_ok:
        return {"ok": False, "gate": "delta-refused",
                "next_step": "delta walk refused — investigate before "
                             "quoting a timeline"}
    if item is None:
        return {"ok": False, "gate": "no-file-found",
                "sites_first_page": len(body.get("value", [])),
                "next_step": "no file item in the first 5 sites' drives — "
                             "widen the probe by hand"}
    dl_host = range_ok = resolved = None
    if item is not None:
        istatus, full = graph_get(token,
                                  f"/drives/{drive['id']}/items/{item['id']}")
        dl = (full or {}).get("@microsoft.graph.downloadUrl")
        if istatus == 200 and dl:
            try:
                url, _wire = puller.resolve_download(dl)
                resolved = True
                dl_host = urllib.parse.urlparse(url).netloc
                req = urllib.request.Request(
                    url, headers={"Range": "bytes=0-1023"})
                with _http(req, timeout=60) as r:
                    range_ok = getattr(r, "status", None) == 206
            except Exception as e:  # noqa: BLE001 — probe reports, not dies
                resolved = False
                dl_host = f"resolve-failed: {e}"
    ok = bool(resolved) and bool(range_ok)
    return {
        "ok": ok,
        "sites_first_page": len(body.get("value", [])),
        "probe_site": probe_site.get("webUrl"),
        "probe_drive": drive.get("name"),
        "probe_file_size": item.get("size"),
        "download_resolved": resolved,
        "download_host": dl_host,
        "range_supported": range_ok,
        "next_step": ("all gates green — run plan next" if ok else
                      "downloadUrl did not resolve/range on a SharePoint "
                      "host — investigate before any VM is billed"),
        "note": "counts only, never bytes — the honest byte numbers come "
                "from plan's container listing and the census",
    }


def cmd_plan(root: Path, args) -> dict:
    """Freeze the in-scope folder set + build the mapping ledger. Needs the
    3 stdin credentials (Graph site enumeration) AND a container listing
    (ip_rule_ensure + rl SAS — the laptop read path)."""
    cfg = eng.load_cfg(root, args.slug)
    tenant, client_id, secret = read_secrets(args.dry_run)
    prefix = (args.dest_prefix or SPEC.default_dest_prefix).strip("/")
    if args.dry_run:
        graph_get(None, "/sites", {"search": "*"}, dry_run=True)
        list_prefix_by_folder(cfg, "<sas>", prefix, True)
        return {"ok": True, "dry_run": True,
                "would_write": str(state_dir(root) / "mapping.json")}

    box = TokenBox(validate_tenant_id(tenant),
                   validate_client_id(client_id), secret)
    token = box.mint()
    common.run_az(["account", "set", "-s", cfg["subscription"]])
    we_added, ip = phases.ip_rule_ensure(cfg, False)
    try:
        sas = phases.mint_sas(cfg, False)   # rl — the READ path
        print(f"progress listing {prefix}/ ...", file=sys.stderr, flush=True)
        index = list_prefix_by_folder(cfg, sas, prefix, False)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, False)

    folders = {f: folder_stats(files) for f, files in index.items()}
    print(f"progress enumerating tenant sites ...", file=sys.stderr,
          flush=True)
    sites = list_all_sites(token)
    colls = build_collections(sites)
    rows = match_folders(folders, colls)
    census_walk, census_match = load_census()
    calibration = mark_calibration(rows, census_walk)

    census_diffs = []
    for r in rows:
        prev = census_match.get(r["folder"])
        if prev and r.get("site_url") and prev.lower() != r["site_url"]:
            census_diffs.append({"folder": r["folder"],
                                 "census": prev, "now": r["site_url"]})

    sdir = state_dir(root)
    sdir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "slug": args.slug,
        "dest_prefix": prefix,
        "created_utc": common.iso_now(),
        "approved_utc": None,
        "snapshot": {
            "folders": len(folders),
            "files": sum(v[0] + v[2] for v in folders.values()),
            "real_bytes": sum(v[1] for v in folders.values()),
            "collections_in_tenant": len(colls),
        },
        "calibration_sites": calibration,
        "census_match_diffs": census_diffs,
        "folders": rows,
    }
    common.write_json(sdir / "mapping.json", mapping)

    actions: dict[str, int] = {}
    for r in rows:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
    ambiguous = [r["folder"] for r in rows
                 if r["action"] == "skip-ambiguous"]
    return {
        "ok": True,
        "mapping": str(sdir / "mapping.json"),
        "folders_in_scope": len(rows),
        "actions": actions,
        "ambiguous_folders": ambiguous,
        "duplicate_target_folders": [
            r["folder"] for r in rows
            if r["action"] == "skip-duplicate-target"],
        "calibration_sites": calibration,
        "census_match_diffs": census_diffs,
        "existing_real_bytes_human": common.human_bytes(
            mapping["snapshot"]["real_bytes"]),
        "warning": (None if calibration else
                    "NO calibration sites found — the puller will refuse "
                    "to copy; investigate before proceeding"),
        "next": "review mapping.json (ambiguous + duplicate-target rows, "
                "census diffs), then approve-mapping",
    }


def cmd_approve_mapping(root: Path, args) -> dict:
    mapping = _load_mapping(root)
    bad = [r["folder"] for r in mapping.get("folders", [])
           if r.get("action") == "complete"
           and not (r.get("site_url") and r.get("site_ids"))]
    if bad:
        raise common.HarnessError(
            f"complete-action rows missing site_url/site_ids: {bad[:10]} — "
            "fix mapping.json before approving")
    mapping["approved_utc"] = common.iso_now()
    common.write_json(state_dir(root) / "mapping.json", mapping)
    ambiguous = [r["folder"] for r in mapping["folders"]
                 if r["action"] == "skip-ambiguous"]
    return {"ok": True, "approved_utc": mapping["approved_utc"],
            "complete_rows": sum(1 for r in mapping["folders"]
                                 if r["action"] == "complete"),
            "still_ambiguous": ambiguous,
            "note": ("ambiguous rows stay EXCLUDED until you set their "
                     "site_url/site_ids/action and re-run approve-mapping"
                     if ambiguous else None)}


def cmd_write_dest(root: Path, args) -> dict:
    """racwl SAS -> rclone [azure] section (so check-azure works) AND
    dest-sp.env. DEST_URL is the BARE container URL (teams convention:
    the puller appends DEST_PREFIX itself)."""
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    prefix = (args.dest_prefix
              or (vm.get("tags") or {}).get("dest_prefix")
              or SPEC.default_dest_prefix)
    sas, expiry = eng.mint_container_sas(cfg, args.sas_days, args.dry_run)
    base = _container_url(cfg)
    eng.write_conf_section(vm["public_ip"], "azure",
                           f"[azure]\ntype = azureblob\n"
                           f"sas_url = {base}?{sas}\n",
                           dry_run=args.dry_run)
    env = (f"DEST_URL='{base}'\n"
           f"DEST_SAS='{sas}'\n"
           f"DEST_PREFIX='{prefix}'\n")
    _write_env(vm["public_ip"], DEST_ENV, env, args.dry_run)
    return {"remote": "azure", "container": cfg["container"],
            "dest_prefix": prefix, "sas_expiry": expiry,
            "written_to": [f"{vm['name']}:{eng.RCLONE_CONF}",
                           f"{vm['name']}:{DEST_ENV}"]}


def cmd_write_creds(root: Path, args) -> dict:
    stdin_tenant, client_id, secret = read_secrets(args.dry_run)
    stdin_tenant = validate_tenant_id(stdin_tenant)
    client_id = validate_client_id(client_id)
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    expected = getattr(args, "tenant_id", None) \
        or (vm.get("tags") or {}).get(SPEC.loc_tag)
    if expected:
        expected = validate_tenant_id(expected)
    _tenant_guard(stdin_tenant, expected)
    env = (f"SP_TENANT_ID={stdin_tenant}\n"
           f"SP_CLIENT_ID={client_id}\n"
           f"SP_CLIENT_SECRET='{secret}'\n")
    _write_env(vm["public_ip"], SP_ENV, env, args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "secret": "redacted",
                "written_to": f"{vm['name']}:{SP_ENV}"}
    TokenBox(stdin_tenant, client_id, secret).mint()  # smoke test
    return {"ok": True, "secret": "redacted",
            "written_to": f"{vm['name']}:{SP_ENV}"}


def cmd_push_mapping(root: Path, args) -> dict:
    _require_approved(root)
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    _push_mapping(vm["public_ip"], state_dir(root) / "mapping.json",
                  args.dry_run)
    return {"ok": True, "written_to": f"{vm['name']}:{XFER_DIR}/mapping.json"}


def cmd_transfer(root: Path, args) -> dict:
    """Fresh puller + mapping push -> tmux window 'sp'. The puller is fully
    env-driven; pilots ride export lines. ALWAYS start with --diff-only."""
    mapping = _require_approved(root)
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    if eng._tmux_alive(ip, args.dry_run):
        return {"ok": False, "cause": "already-running",
                "hint": "tmux session 'transfer' is alive — use status."}
    _push_puller(ip, args.dry_run)
    _push_mapping(ip, state_dir(root) / "mapping.json", args.dry_run)
    env_extra = ""
    if args.diff_only:
        env_extra += "export DIFF_ONLY=1; "
    if args.only_site:
        env_extra += f"export ONLY_SITES='{','.join(args.only_site)}'; "
    if args.limit_sites:
        env_extra += f"export LIMIT_SITES={args.limit_sites}; "
    if args.rps_graph:
        env_extra += f"export RPS_GRAPH={args.rps_graph}; "
    if args.max_rps:
        env_extra += f"export MAX_RPS={args.max_rps}; "
    if args.workers:
        env_extra += f"export WORKERS={args.workers}; "
    if args.copy_threads:
        env_extra += f"export COPY_THREADS={args.copy_threads}; "
    if args.copy_order:
        env_extra += f"export COPY_ORDER={args.copy_order}; "
    if args.reuse_manifest_hours:
        env_extra += (f"export REUSE_MANIFEST_HOURS="
                      f"{args.reuse_manifest_hours}; ")
    if args.refresh_sites:
        env_extra += "export REFRESH_SITES=1; "
    if args.allow_no_calibration:
        env_extra += "export ALLOW_NO_CALIBRATION=1; "
    inner = (f"set -a; . {SP_ENV}; . {DEST_ENV}; set +a; "
             f"{env_extra}python3 {XFER_DIR}/saxon_sp_vm_pull.py "
             f"2>&1 | tee -a {LOG_FILE}")
    eng.run_ssh(ip, f'tmux new-session -d -s {eng.TMUX_SESSION} -n sp '
                    f'"{inner}"',
                dry_run=args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True,
                "diff_only": args.diff_only or None}
    eng.run_ssh(ip, "sleep 5", check=False)
    alive = eng._tmux_alive(ip, False)
    tail = eng.run_ssh(ip, f"tail -3 {LOG_FILE} 2>/dev/null", check=False)
    return {"ok": alive, "session": eng.TMUX_SESSION, "window": "sp",
            "diff_only": args.diff_only or None,
            "only_sites": args.only_site or None,
            "calibration_sites": mapping.get("calibration_sites"),
            "log_tail": (tail.stdout or "").strip().splitlines(),
            "note": ("re-running transfer is safe — the dest is the "
                     "resume: fresh diff + create-only 409s" if alive
                     else None),
            "hint": None if alive else
            f"the puller died immediately — tail {LOG_FILE} on the VM"}


_STATUS_PY = r"""
import json, os, shutil
base = os.path.expanduser("~/xfer-sp")
out = {}
for name in ("progress.json", "run-summary.json"):
    try:
        out[name.split(".")[0].replace("-", "_")] = json.load(
            open(os.path.join(base, name)))
    except (OSError, ValueError):
        out[name.split(".")[0].replace("-", "_")] = None
try:
    st = json.load(open(os.path.join(base, "state.json")))
    counts = {}
    for v in st.values():
        counts[v.get("status")] = counts.get(v.get("status"), 0) + 1
    out["state_counts"] = counts
    out["copied_total"] = sum(v.get("copied") or 0 for v in st.values())
except (OSError, ValueError):
    out["state_counts"] = None
try:
    lines = open(os.path.join(base, "pull-sp.log")).read().splitlines()
    out["log_tail"] = lines[-6:]
except OSError:
    out["log_tail"] = []
try:
    du = shutil.disk_usage(base)
    out["disk_free_gb"] = round(du.free / 1e9, 1)
except OSError:
    pass
print(json.dumps(out))
"""


def cmd_status(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    alive = eng._tmux_alive(vm["public_ip"], args.dry_run)
    proc = eng.run_ssh(vm["public_ip"], "python3 -", stdin_data=_STATUS_PY,
                       dry_run=args.dry_run, check=False, timeout=120)
    if args.dry_run:
        return {"ok": True, "dry_run": True}
    try:
        detail = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        detail = {"aggregator_error": (proc.stdout or proc.stderr)[-300:]}
    summary = detail.get("run_summary")
    hint = None
    if not alive:
        hint = ("a pass finished — run harvest, then verify" if summary
                else "not running and no run-summary — it never finished "
                     f"a pass; tail {LOG_FILE} on the VM")
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "transfer_running": alive, "hint": hint, **detail}


def cmd_harvest(root: Path, args) -> dict:
    """Pull the run's state home BEFORE teardown — the VM copy is the only
    one, and verify runs off the harvested manifests. Binary-safe ssh tar
    (run_ssh is text-mode)."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    run_dir = state_dir(root) / f"run-{common.ts_basic()}"
    # Only tar what EXISTS: run-summary.json is written at the end of a
    # pass, so naming it unconditionally made tar exit nonzero and killed
    # every mid-run harvest — the one moment (a deadline, a pass still
    # copying) when harvesting matters most.
    # Only tar what EXISTS: run-summary.json is written at the end of a
    # pass, so naming it unconditionally made tar exit nonzero and killed
    # every mid-run harvest — the one moment (a deadline, a pass still
    # copying) when harvesting matters most. `;` not `&&`: ls itself
    # exits nonzero whenever one of the named files is absent, which is
    # the normal mid-run case.
    remote = (f"cd {XFER_DIR}; F=$(ls -d state.json results.jsonl "
              "run-summary.json progress.json manifests 2>/dev/null); "
              "tar czf - $F")
    if args.dry_run:
        print(f"DRY-RUN: ssh {eng.ADMIN_USER}@{vm['public_ip']} '{remote}' "
              f"> {run_dir}/")
        return {"ok": True, "dry_run": True, "run_dir": str(run_dir)}
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
         f"{eng.ADMIN_USER}@{vm['public_ip']}", remote],
        capture_output=True, timeout=600)
    if proc.returncode != 0 or not proc.stdout:
        raise common.HarnessError(
            "harvest tar failed: "
            + (proc.stderr or b"").decode("utf-8", "replace")[-300:])
    run_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as tf:
        try:
            tf.extractall(run_dir, filter="data")
        except TypeError:   # filter= needs 3.12; members are our own files
            tf.extractall(run_dir)
    manifests = sorted((run_dir / "manifests").glob("*.tsv.gz")) \
        if (run_dir / "manifests").is_dir() else []
    return {"ok": True, "run_dir": str(run_dir),
            "manifests": len(manifests),
            "have_results": (run_dir / "results.jsonl").exists(),
            "next": f"verify --run {run_dir.name.removeprefix('run-')}"}


def _latest_run_dir(root: Path) -> Path:
    runs = sorted(state_dir(root).glob("run-*"))
    if not runs:
        raise common.HarnessError("no harvested run dir — run harvest first")
    return runs[-1]


def cmd_verify(root: Path, args) -> dict:
    """Laptop-side; the VM may be gone. Merges the harvested per-site
    manifests (the Graph truth at walk time) against a FRESH container
    listing: complete = every action=complete site has missing == 0 and no
    site failed its pass. Mismatch/dest-only/sidecars never gate. Ambiguous
    and duplicate-target folders are re-surfaced loudly — they were never
    completed, deliberately."""
    cfg = eng.load_cfg(root, args.slug)
    mapping = _load_mapping(root)
    prefix = (args.dest_prefix or mapping.get("dest_prefix")
              or SPEC.default_dest_prefix).strip("/")
    run_dir = (state_dir(root) / f"run-{args.run}" if args.run
               else _latest_run_dir(root))
    manifests = sorted((run_dir / "manifests").glob("*.tsv.gz"))
    if not manifests and not args.dry_run:
        raise common.HarnessError(f"no manifests under {run_dir}")
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)   # rl — the READ path
        index = list_prefix_by_folder(cfg, sas, prefix, args.dry_run)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "run_dir": str(run_dir)}

    results_by_folder: dict[str, dict] = {}
    rpath = run_dir / "results.jsonl"
    if rpath.exists():
        for ln in rpath.read_text().splitlines():
            try:
                row = json.loads(ln)
                results_by_folder[row["folder"]] = row   # last wins
            except (ValueError, KeyError):
                pass

    actions = {r["folder"]: r["action"] for r in mapping.get("folders", [])}
    per_site = []
    incomplete = []
    failed = [f for f, r in results_by_folder.items()
              if r.get("status") == "failed"]
    for mpath in manifests:
        folder = mpath.name[:-len(".tsv.gz")]
        expected: dict[str, int] = {}
        site_url = ""
        with gzip.open(mpath, "rt", encoding="utf-8") as fh:
            for ln in fh:
                if ln.startswith("#site\t"):
                    site_url = ln.rstrip("\n").split("\t")[2]
                    continue
                p = ln.rstrip("\n").split("\t")
                if len(p) >= 2:
                    expected[p[0]] = int(p[1])
        dest = index.get(folder, {})
        diff = puller.diff_folder(
            {rel: (size,) for rel, size in expected.items()}, dest)
        # Files Defender quarantined at source can never land — Graph
        # answers their download with 403 malwareDetected forever. They
        # are a recorded source-side fact, not a transfer gap, so they
        # come OUT of `missing` and get their own column (never silent).
        quarantined = set(
            (results_by_folder.get(folder) or {}).get("quarantined_paths")
            or [])
        if quarantined:
            diff["missing"] = [m for m in diff["missing"]
                               if m not in quarantined]
        row = {"folder": folder, "site_url": site_url,
               "quarantined_at_source": len(quarantined),
               "expected_files": len(expected),
               "expected_bytes": sum(expected.values()),
               "matched": diff["matched"],
               "matched_bytes": diff["matched_bytes"],
               "missing": len(diff["missing"]),
               "missing_bytes": sum(expected[m] for m in diff["missing"]),
               "missing_sample": diff["missing"][:5],
               "mismatched": len(diff["mismatched"]),
               "dest_only": diff["dest_only"],
               "sidecars": diff["sidecars"]}
        per_site.append(row)
        if actions.get(folder, "complete") == "complete" \
                and row["missing"]:
            incomplete.append(row)

    per_site.sort(key=lambda r: -r["expected_bytes"])
    skipped = {a: [r["folder"] for r in mapping.get("folders", [])
                   if r["action"] == a]
               for a in ("skip-ambiguous", "skip-duplicate-target")}
    ok = not incomplete and not failed
    verdict = {
        "ok": ok,
        "run_dir": str(run_dir),
        "prefix": prefix,
        "sites_verified": len(per_site),
        "total_expected_bytes": sum(r["expected_bytes"] for r in per_site),
        "total_matched_bytes": sum(r["matched_bytes"] for r in per_site),
        "incomplete_sites": incomplete,
        "failed_sites": failed,
        "mismatched_total": sum(r["mismatched"] for r in per_site),
        "dest_only_total": sum(r["dest_only"] for r in per_site),
        "quarantined_total": sum(r.get("quarantined_at_source", 0)
                                 for r in per_site),
        "skipped_by_design": skipped,
        "top_sites": per_site[:15],
        "hint": None if ok else
        "incomplete/failed sites: re-run transfer (the dest is the resume "
        "— fresh diff + create-only 409s make it idempotent), then harvest "
        "+ verify again",
        "note": ("certifies Graph-walk -> container completeness per file "
                 "(name+size) for every walked site. Mismatched files are "
                 "the client's bytes left alone (a report conversation); "
                 "dest-only files were deleted at source since the push; "
                 "skipped_by_design folders were never completed — "
                 "deliberately." if ok else None),
    }
    out = run_dir / f"verify-{common.ts_basic()}.json"
    common.write_json(out, verdict)
    verdict["written_to"] = str(out)
    return verdict


def cmd_teardown(root: Path, args) -> dict:
    result = eng.cmd_teardown(SPEC, root, args)
    if result.get("ok") and "reminders" in result:
        result["reminders"] += [
            "HARVEST FIRST if you haven't — the VM held the only copy of "
            "the run manifests (companies/saxon/sharepoint-completion/"
            "run-*/ should exist and be fresh).",
            "Tell the client to rotate (or delete) the Relay Export 2 "
            "app's client secret — the engagement is done.",
        ]
    return result


def cmd_discover(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    if args.dry_run:
        eng.get_vm(SPEC, cfg, args.slug, True)
        return {"phase": "unknown (dry-run)"}
    vm = eng.get_vm(SPEC, cfg, args.slug, False)
    if vm is None:
        return {"phase": "pre-setup", "vm": None,
                "hint": "no VM — run probe, then plan/approve-mapping, "
                        "then create-vm"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base}
    checks = [f"test -f {SP_ENV} && echo sp-env",
              f"test -f {DEST_ENV} && echo dest-env",
              f"test -f {XFER_DIR}/mapping.json && echo mapping",
              f"test -f {XFER_DIR}/run-summary.json && echo summary",
              f"tmux has-session -t {eng.TMUX_SESSION} 2>/dev/null "
              "&& echo tmux-alive"]
    probe = eng.run_ssh(vm["public_ip"], "; ".join(checks), check=False)
    if probe.returncode != 0 and not (probe.stdout or "").strip():
        return {"phase": "vm-unreachable", **base}
    out = probe.stdout or ""
    if "tmux-alive" in out:
        return {"phase": "transfer-running", **base,
                "hint": "use status for progress"}
    if "summary" in out:
        runs = sorted(state_dir(root).glob("run-*"))
        return {"phase": "transfer-stopped", **base,
                "harvested_runs": [r.name for r in runs],
                "hint": "a pass finished — run harvest (if not yet), then "
                        "verify; failed sites mean re-run transfer"}
    if not ("sp-env" in out and "dest-env" in out and "mapping" in out):
        return {"phase": "mid-setup", **base,
                "hint": "resume at the missing write-dest / write-creds / "
                        "push-mapping step"}
    return {"phase": "setup-complete", **base,
            "hint": "run transfer --diff-only first (calibration gate + "
                    "measured missing set), then pilots, then the full run"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[
        "discover", "probe", "plan", "approve-mapping", "create-vm",
        "allow-network", "write-dest", "check-azure", "write-creds",
        "push-mapping", "transfer", "status", "harvest", "verify",
        "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--tenant-id", dest="tenant_id", default=None)
    p.add_argument("--rg", help="override VM resource group")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None)
    p.add_argument("--dest-prefix", default=None,
                   help=f"default {SPEC.default_dest_prefix} — the client's "
                        "own prefix; only override for a drill")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--diff-only", action="store_true",
                   help="transfer: walk + diff + calibration gate, ZERO "
                        "copies (ALWAYS run this first)")
    p.add_argument("--only-site", action="append", default=None,
                   help="transfer: pilot on named dest folder(s); "
                        "repeatable")
    p.add_argument("--limit-sites", type=int, default=0)
    p.add_argument("--rps-graph", dest="rps_graph", type=float, default=0.0,
                   help="transfer: STARTING aggregate rate for every "
                        "SharePoint-bound call (adaptive AIMD from there)")
    p.add_argument("--max-rps", dest="max_rps", type=float, default=0.0,
                   help="transfer: adaptive ceiling (default 16)")
    p.add_argument("--workers", type=int, default=0)
    p.add_argument("--copy-order", dest="copy_order", default=None,
                   choices=["walk", "size-desc"],
                   help="transfer: 'size-desc' copies biggest files first "
                        "— a file costs ~2 paced calls regardless of size, "
                        "so under a deadline this maximises bytes landed "
                        "(VSS1: top 25k of 1.06M files = 87%% of bytes). "
                        "Reorders only; nothing is excluded.")
    p.add_argument("--reuse-manifest-hours", dest="reuse_manifest_hours",
                   type=float, default=0.0,
                   help="transfer: reuse a site's manifest if it is younger "
                        "than N hours instead of re-walking it (a VSS1-"
                        "scale re-walk is ~2h before any byte moves). The "
                        "dest is still re-diffed fresh, so this can only "
                        "miss items created at SOURCE since that walk.")
    p.add_argument("--copy-threads", dest="copy_threads", type=int,
                   default=0,
                   help="transfer: per-site copy pool size (default 8) — "
                        "keeps a monster site saturating the global "
                        "bucket")
    p.add_argument("--refresh-sites", action="store_true",
                   help="transfer: re-walk sites state.json already "
                        "marks ok")
    p.add_argument("--allow-no-calibration", action="store_true",
                   help="transfer: copy without calibration rows — only "
                        "after a diff-only pass proved the convention")
    p.add_argument("--run", default=None,
                   help="verify: run timestamp (default: latest harvested)")
    p.add_argument("--confirmed", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.command == "create-vm" and args.tenant_id is None:
        p.error("create-vm requires --tenant-id (it rides the VM's "
                f"{SPEC.loc_tag} tag for the write-creds guard)")
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    engine_cmds = {"create-vm": eng.cmd_create_vm,
                   "allow-network": eng.cmd_allow_network,
                   "check-azure": eng.cmd_check_azure}
    own_cmds = {"discover": cmd_discover, "probe": cmd_probe,
                "plan": cmd_plan, "approve-mapping": cmd_approve_mapping,
                "write-dest": cmd_write_dest,
                "write-creds": cmd_write_creds,
                "push-mapping": cmd_push_mapping, "transfer": cmd_transfer,
                "status": cmd_status, "harvest": cmd_harvest,
                "verify": cmd_verify, "teardown": cmd_teardown}
    try:
        guard_slug(args.slug)
        if args.tenant_id is not None:
            args.tenant_id = validate_tenant_id(args.tenant_id)
        if args.command in engine_cmds:
            result = engine_cmds[args.command](SPEC, root, args)
        else:
            result = own_cmds[args.command](root, args)
    except common.HarnessError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    except subprocess.TimeoutExpired as e:
        print(json.dumps({"ok": False,
                          "error": f"timeout: {str(e)[:200]}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
