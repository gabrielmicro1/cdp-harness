#!/usr/bin/env python3
"""Zoho -> Azure transfer CLI (VM REST puller + azcopy; VM family).

Zoho's download endpoints authenticate with `Authorization: Zoho-oauthtoken
<tok>`, and Azure's `x-ms-copy-source-authorization` only speaks `Bearer` —
so the vimeo/zoom server-side-copy transport is STRUCTURALLY unavailable and
CRM attachment bytes must be staged somewhere. That one fact puts zoho in the
github family: this script reuses transfer_engine.py's VM lifecycle
(create-vm / allow-network / check-azure / teardown run the engine functions
verbatim) but owns the pull layer — scripts/zoho_vm_pull.py is pushed to the
VM and runs the whole pull there in tmux, then azcopies the staged tree to
<slug>-raw/<dest-prefix>/<product>/.

The first MULTI-PRODUCT ingest: one script covers Zoho CRM, Zoho Learn and
Zoho WorkDrive behind --product, one tmux window each (CRM and Learn hit
disjoint APIs and may run concurrently).

Subcommands: discover / plan / create-vm / allow-network / write-dest /
check-azure / write-creds / probe / transfer / status / verify / teardown.
Source location flag: --dc (the Zoho data center; required for plan/create-vm/
probe, later read from VM tags). See
.claude/skills/zoho-azure-transfer/SKILL.md.

Secrets: FOUR values arrive as 4 stdin lines, in order — data center,
client_id, client_secret, refresh_token (a client-made Self Client app; they
do the grant->refresh exchange themselves because grant tokens expire in
3-10 minutes). They travel only over ssh stdin into a 600 env file on the VM
— never argv, tags, logs or laptop files (probe holds them in process memory
only). The dest SAS goes the same way. All die with the VM at teardown.

The data center is the day-one stall (the zoho analogue of github's
unapproved PAT): a wrong .com/.eu looks like a generic auth failure. It is
caught three independent ways — the mint error body, an api_domain
cross-check against the declared DC, and a stdin-vs-VM-tag guard.

Verify runs on the LAPTOP (the VM is normally gone by then): the completeness
authority is the per-product manifest.json the pull uploaded, so no Zoho
access is needed — just a blob listing, which from this machine's external IP
uses phases.ip_rule_ensure + an rl account SAS (the zoom/qwilr laptop path;
allow-network stays VM-only).
"""
from __future__ import annotations

import json
import subprocess
import sys
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

SPEC = eng.Spec(
    source_name="zoho",
    vm_prefix="xfer-zoho-",
    purpose="zoho-transfer",
    loc_tag="zoho_dc",
    loc_argname="dc",
    loc_required=True,
    default_dest_prefix="zoho-export",
    authorize_target="",  # no rclone OAuth flow — Self Client creds on stdin
    remote_type="",       # no rclone source remote; REST is the source
    extra_cli_opts=[
        {"flag": "--product", "argname": "product", "tag": "zoho_product",
         "conf_key": "", "help": "crm | learn | workdrive"},
        {"flag": "--portal", "argname": "portal", "tag": "zoho_portal",
         "conf_key": "", "help": "Zoho Learn portal networkurl"}],
    default_os_disk_gb=512,  # staging holds the corpus before azcopy
)

PULLER_PY = Path(__file__).resolve().parent / "zoho_vm_pull.py"
XFER_DIR = f"/home/{eng.ADMIN_USER}/xfer-zoho"
DEST_DIR = f"{XFER_DIR}/dest"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/xfer"
ZOHO_ENV = f"{ENV_DIR}/zoho.env"


def _dest_env(product: str) -> str:
    """PER-PRODUCT dest env. One VM runs crm/learn/workdrive concurrently in
    separate tmux windows, and each writes to a DIFFERENT container prefix —
    a single shared dest.env would mean the second write-dest silently
    re-pointed the first product's resume at the wrong prefix."""
    return f"{ENV_DIR}/dest-{product}.env"
PRODUCTS = ("crm", "learn", "workdrive")
X_MS_VERSION = "2021-08-06"
TOKEN_REFRESH_MARGIN = 300  # re-mint this many seconds before expiry

_sleep = time.sleep  # seam so tests can record/skip waits


def _http(req: urllib.request.Request, timeout: int = 60):
    """Single transport seam (tests stub this; production never branches)."""
    return urllib.request.urlopen(req, timeout=timeout)


def _log_file(product: str) -> str:
    return f"{XFER_DIR}/pull-{product}.log"


# ── data centers ─────────────────────────────────────────────────────────────
# Zoho is DC-bound: accounts/api/learn all live under the same suffix, and a
# refresh token minted in one DC is meaningless in another. Getting this
# wrong is the day-one stall, so every host is derived from one validated
# value and never hand-built at a call site.

DCS = ("com", "eu", "in", "com.au", "jp", "ca", "sa", "com.cn")


def validate_dc(dc: str) -> str:
    d = (dc or "").strip().lower().lstrip(".")
    if d not in DCS:
        raise common.HarnessError(
            f"unknown Zoho data center {dc!r} — expected one of "
            f"{', '.join(DCS)} (it is the suffix of the URL the client sees "
            "when signed in to Zoho, e.g. crm.zoho.eu -> eu)")
    return d


def accounts_host(dc: str) -> str:
    return f"accounts.zoho.{dc}"


def api_host(dc: str) -> str:
    return f"www.zohoapis.{dc}"


def learn_hosts(dc: str) -> list[str]:
    """Learn has historically served some DCs only from .com — try the
    DC-local host first and fall back, recording which answered."""
    hosts = [f"learn.zoho.{dc}"]
    if dc != "com":
        hosts.append("learn.zoho.com")
    return hosts


def expected_api_domain(dc: str) -> str:
    return f"https://{api_host(dc)}"


def _dc_guard(stdin_dc: str, tags: dict) -> None:
    """PURE. Refuse to write credentials whose DC contradicts the VM's tag —
    the third of the three DC detections. Dry-run VMs have empty tags
    (transfer_engine.py:202-203), so an absent tag is a no-op, never a
    failure."""
    tagged = (tags or {}).get(SPEC.loc_tag)
    if tagged and tagged != stdin_dc:
        raise common.HarnessError(
            f"data-center mismatch: stdin says {stdin_dc!r} but the VM was "
            f"created with {SPEC.loc_tag}={tagged!r}. A refresh token is "
            "only valid in the DC that issued it. Either re-run with the "
            f"right DC, or tear down and re-create the VM with --dc "
            f"{stdin_dc}. Nothing was written.")


# ── VM-side plumbing ─────────────────────────────────────────────────────────

def _write_env(ip: str, path: str, content: str, dry_run: bool) -> None:
    """600 env file on the VM; content rides ssh stdin only."""
    eng.run_ssh(ip, f"umask 077 && mkdir -p {ENV_DIR} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _push_puller(ip: str, dry_run: bool) -> None:
    """Fresh copy every transfer so harness upgrades propagate."""
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > "
                    f"{XFER_DIR}/zoho_vm_pull.py",
                stdin_data=PULLER_PY.read_text(), dry_run=dry_run)


def _product(vm: dict, args) -> str:
    p = (getattr(args, "product", None)
         or (vm.get("tags") or {}).get("zoho_product") or "crm")
    if p not in PRODUCTS:
        raise common.HarnessError(
            f"unknown --product {p!r} — expected one of {', '.join(PRODUCTS)}")
    return p


def _portal(vm: dict, args) -> str | None:
    return (getattr(args, "portal", None)
            or (vm.get("tags") or {}).get("zoho_portal") or None)


def _dest_prefix(vm: dict, args) -> str:
    """The BASE prefix, without the product. Kept separate on purpose: the
    dest_prefix VM tag must stay 'zoho-export' so one VM can serve all three
    products — tagging it 'zoho-export/crm' at create time would silently
    poison learn and workdrive."""
    return (getattr(args, "dest_prefix", None)
            or (vm.get("tags") or {}).get("dest_prefix")
            or SPEC.default_dest_prefix)


def _product_prefix(vm: dict, args) -> str:
    """Where this product's blobs actually land.

    Default: <base>/<product>, so three products can share one base without
    colliding. --dest-exact overrides with a LITERAL prefix and appends
    nothing — for when the operator wants one top-level folder per product
    (e.g. zoho_crm / zoho_learn, which normalize to the same key as the
    manifest service names and so need no prefix pin at all)."""
    exact = getattr(args, "dest_exact", None)
    if exact:
        return exact.strip("/")
    return f"{_dest_prefix(vm, args)}/{_product(vm, args)}"


def _tmux_windows(ip: str, dry_run: bool) -> list[str]:
    # '#W' (window_name alias) keeps braces out of the dry-run echo, which
    # the test harness scans with stdout.index("{") to find the JSON tail
    proc = eng.run_ssh(
        ip, f"tmux list-windows -t {eng.TMUX_SESSION} "
            "-F '#W' 2>/dev/null", dry_run=dry_run, check=False)
    return [w for w in (proc.stdout or "").split() if w]


# ── laptop-side Zoho client (probe only; creds stay in process memory) ───────

class ZohoHTTPError(Exception):
    """Non-fatal Zoho API failure — the caller decides whether this unit is
    required (fatal) or optional (a recorded skip)."""

    def __init__(self, status: int, code: str, msg: str):
        super().__init__(msg)
        self.status = status
        self.code = code


def zoho_error_code(body) -> str:
    """PURE. Zoho carries the real meaning in a body error code, not the HTTP
    status — the same 401 is INVALID_TOKEN or OAUTH_SCOPE_MISMATCH. Bodies
    arrive in at least four shapes, and garbage must degrade to "" rather
    than raise."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - defensive; never fail a diagnosis
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


def _err_body(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")[:400]
    except Exception:  # noqa: BLE001 - diagnosis must never mask the error
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


class TokenBox:
    """Holds the Self Client credentials and the ~1h access token they mint.

    get() re-mints TOKEN_REFRESH_MARGIN seconds before expiry so a long run
    never presents a dead token. The mint response's api_domain is Zoho
    telling us which DC this refresh token actually belongs to — cross-checked
    against the declared DC, which is the second of the three DC detections.
    Credentials and token live only in this object (process memory) — never
    argv/env/files/logs, never printed.
    """

    def __init__(self, dc: str, client_id: str, client_secret: str,
                 refresh_token: str, dry_run: bool = False):
        self._dc = dc
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._dry_run = dry_run
        self._value: str | None = None
        self._exp = 0.0
        self.api_domain: str | None = None
        self.mints = 0

    def get(self) -> str:
        if self._dry_run:
            return "<token>"
        if self._value and time.time() < self._exp - TOKEN_REFRESH_MARGIN:
            return self._value
        return self.mint()

    def invalidate(self) -> None:
        """Drop the cached token so the next get() re-mints (used on a 401
        mid-run — a call can straddle the hourly expiry)."""
        self._value = None

    def mint(self) -> str:
        if self._dry_run:
            print(f"DRY-RUN: POST https://{accounts_host(self._dc)}"
                  "/oauth/v2/token "
                  "(grant_type=refresh_token, client_id/client_secret/"
                  "refresh_token redacted)")
            return "<token>"
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }).encode()
        last: Exception | None = None
        for attempt in range(6):
            req = urllib.request.Request(
                f"https://{accounts_host(self._dc)}/oauth/v2/token",
                data=data, method="POST",
                headers={"Content-Type":
                         "application/x-www-form-urlencoded"})
            try:
                with _http(req, timeout=60) as r:
                    payload = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429:
                    retry_after = (e.headers.get("Retry-After") or "").strip()
                    _sleep(int(retry_after) if retry_after.isdigit()
                           else min(2 ** attempt, 32))
                    continue
                if e.code >= 500:
                    _sleep(1 + attempt)
                    continue
                raise common.HarnessError(
                    f"Zoho token mint failed: HTTP {e.code} {_err_body(e)}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                _sleep(1 + attempt)
                continue
            self.mints += 1
            return self._accept(payload)
        raise common.HarnessError(
            f"Zoho token mint failed after retries: {last}")

    def _accept(self, payload: dict) -> str:
        """Zoho answers OAuth failures with HTTP 200 and an error body, so
        the error branch lives here rather than in an except."""
        err = zoho_error_code(payload)
        if err or "access_token" not in payload:
            if err in ("invalid_code", "invalid_grant"):
                raise common.HarnessError(
                    f"Zoho token mint rejected the refresh token "
                    f"({err}) using data center {self._dc!r} "
                    f"({accounts_host(self._dc)}). Two causes look "
                    "identical here: (a) WRONG DATA CENTER — the token was "
                    "issued by a different Zoho DC, check the URL the "
                    "client sees when signed in (crm.zoho.eu -> --dc eu); "
                    "(b) the refresh token was revoked or the Self Client "
                    "was deleted. Retrying cannot help either way.")
            if err == "invalid_client":
                raise common.HarnessError(
                    "Zoho token mint rejected the client credentials "
                    "(invalid_client) — client_id/client_secret are wrong, "
                    "or the Self Client was deleted in the API console. "
                    "Retrying cannot help.")
            raise common.HarnessError(
                f"Zoho token mint returned no access_token (code={err!r}) "
                f"— unexpected response shape from "
                f"{accounts_host(self._dc)}")
        domain = (payload.get("api_domain") or "").rstrip("/")
        want = expected_api_domain(self._dc)
        if domain and domain != want:
            raise common.HarnessError(
                f"data-center mismatch: the refresh token belongs to "
                f"{domain}, but --dc {self._dc} means {want}. Zoho itself "
                f"told us this in the mint response's api_domain. Re-run "
                f"with the DC that matches {domain}.")
        self.api_domain = domain or want
        self._value = payload["access_token"]
        self._exp = time.time() + int(payload.get("expires_in", 3600))
        return self._value


def read_credentials(dry_run: bool) -> tuple[str, str, str, str]:
    """The four Self Client values arrive on stdin ONLY (heredoc), one per
    line: data center, Client ID, Client Secret, Refresh Token — argv is
    world-readable via ps, env leaks into child processes, files persist."""
    data = "" if sys.stdin.isatty() else sys.stdin.read()
    lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
    if len(lines) == 4:
        return validate_dc(lines[0]), lines[1], lines[2], lines[3]
    if dry_run and not lines:
        return "com", "<client-id>", "<client-secret>", "<refresh-token>"
    raise common.HarnessError(
        "stdin must be exactly 4 lines, in order: data center (com/eu/in/"
        "com.au/jp/ca/sa), Client ID, Client Secret, Refresh Token — "
        "pipe them: probe <slug> --product crm <<'EOF' ... EOF")


def zoho_get(box: TokenBox, host: str, path: str,
             params: dict | None = None) -> object:
    """One GET -> parsed JSON (or None on HTTP 204).

    The probe-side slice of the puller's failure taxonomy, raised as
    HarnessError/ZohoHTTPError instead of SystemExit. 429 honors Retry-After;
    a 401 re-mints exactly once (a call can straddle the hourly expiry) but
    OAUTH_SCOPE_MISMATCH is never retried — a missing scope is a client
    conversation, not throttling.
    """
    url = f"https://{host}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    if box._dry_run:
        print(f"DRY-RUN: GET {url} "
              "(Authorization: Zoho-oauthtoken <token-redacted>)")
        return None
    reminted = False
    last: Exception | None = None
    for attempt in range(6):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Zoho-oauthtoken {box.get()}",
            "Accept": "application/json",
            "User-Agent": "cdp-harness-zoho-transfer/1.0",
        })
        try:
            with _http(req, timeout=90) as r:
                if r.status == 204:
                    return None  # empty module — NOT an error
                raw = r.read()
                parsed = json.loads(raw) if raw.strip() else None
                bad = body_failure(parsed)
                if bad:
                    raise ZohoHTTPError(
                        200, bad,
                        f"HTTP 200 but body says failure ({bad}) on {path}")
                return parsed
        except urllib.error.HTTPError as e:
            last = e
            body = _err_body(e)
            code = zoho_error_code(body)
            if e.code == 401 and code == "OAUTH_SCOPE_MISMATCH":
                raise common.HarnessError(
                    f"HTTP 401 OAUTH_SCOPE_MISMATCH on {path} — the Self "
                    "Client was created without the scope this call needs. "
                    "The client must regenerate the token with the full "
                    "scope list (see the skill's PAUSE snippet); retrying "
                    "cannot help.")
            if e.code == 401:
                if not reminted:
                    reminted = True
                    box.invalidate()
                    continue
                raise common.HarnessError(
                    f"HTTP 401 on {path} even with a freshly minted token "
                    "— the refresh token was revoked, or the Self Client "
                    "was deleted (retrying cannot help)")
            if e.code == 429:
                retry_after = (e.headers.get("Retry-After") or "").strip()
                _sleep(int(retry_after) if retry_after.isdigit()
                       else min(2 ** attempt, 32))
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise ZohoHTTPError(e.code, code,
                                f"HTTP {e.code} {code} on {path}: {body}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    status = getattr(last, "code", 0) or 0
    raise ZohoHTTPError(status, "",
                        f"gave up on {path} after retries: {last}")


# ── azure listing (verify only; laptop path) ─────────────────────────────────
# Local copies of zoom_transfer.py's azure_get / azure_list_blobs (the
# established laptop-side blob-listing pair) — kept here rather than
# imported so the VM-family CLIs stay import-independent of the local-pull
# family.

def _container_url(cfg: dict) -> str:
    return (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")


def azure_get(url: str) -> bytes:
    """GET with retries. A 403 early in a run is usually IP-rule
    propagation (CLAUDE.md lore) -- wait and retry, never re-mint the
    SAS for it."""
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url,
                                     headers={"x-ms-version": X_MS_VERSION})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                _sleep(15 * (attempt + 1))  # propagation, not a bad SAS
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise common.HarnessError(f"blob GET failed: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise common.HarnessError(f"blob GET failed after retries: {last}")


def azure_list_blobs(cfg: dict, sas: str, prefix: str,
                     dry_run: bool) -> dict[str, dict]:
    """One marker-paginated listing of the dest prefix -> {name: {size}}.
    Content-Length is what Azure actually committed — verify's ground
    truth."""
    if dry_run:
        print(f"DRY-RUN: GET {_container_url(cfg)}"
              f"?restype=container&comp=list&prefix={prefix}/&<sas-redacted>")
        return {}
    blobs: dict[str, dict] = {}
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
            props = blob.find("Properties")
            if name:
                blobs[name] = {
                    "size": int((props.findtext("Content-Length") or 0)
                                if props is not None else 0),
                }
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return blobs


def compare_manifest_to_blobs(manifest: dict, blobs: dict,
                              prefix: str) -> dict:
    """Pure. Certifies STAGED -> CONTAINER completeness against the uploaded
    manifest (Zoho -> staged completeness is the puller's own exit code +
    failed_units — verify surfaces that list verbatim).

    Per successful unit: the .cdp-complete marker blob must exist, and the
    per-unit container byte sum must be >= the staged bytes. LESS than
    staged = a partial upload = failure. MORE happens legitimately when a
    --refresh re-pull wrote a longer JSONL and --overwrite=false kept the
    older sibling — reported as stale_extra, informational.

    No source-size claim is made for the RECORD ledger, and there that is
    forced rather than chosen: Zoho exposes no byte size for a CRM record.
    Attachments are different — /crm/v8/Attachments reports Size as an
    EXACT byte integer (verified live 2026-08-24; the docs' "rounded
    string" describes a different field), so the attachments unit carries
    expected_bytes and that IS comparable against what landed. Record
    counts are the puller's own census, reported but not re-derived.
    """
    results = manifest.get("results", [])
    missing_markers: list[str] = []
    short_uploads: list[dict] = []
    stale_extra: list[str] = []

    def rollup(sub: str) -> int:
        return sum(b["size"] for n, b in blobs.items() if n.startswith(sub))

    for r in results:
        if r.get("status") not in ("ok", "skipped-complete", "partial"):
            continue
        sub = f"{prefix}/{r['unit']}/"
        staged = r.get("bytes") or 0
        if f"{sub}.cdp-complete" not in blobs:
            missing_markers.append(sub)
            continue
        landed = rollup(sub)
        if staged and landed < staged:
            short_uploads.append({"prefix": sub, "staged": staged,
                                  "landed": landed})
        elif staged and landed > staged:
            stale_extra.append(sub)

    failed = manifest.get("failed_units", [])
    skipped = manifest.get("skipped_units", [])
    # A partial unit landed everything it fetched, so staged->container is
    # still clean — but the LEDGER is short of the source, which no re-run
    # fixes (it is Zoho's 100k page_token ceiling). Surface it loudly rather
    # than failing forever on something a retry cannot cure.
    partial = manifest.get("partial_units", [])
    ok = not failed and not missing_markers and not short_uploads
    return {
        "ok": ok,
        "product": manifest.get("product"),
        "unit_count": manifest.get("unit_count"),
        "total_staged_bytes": manifest.get("total_staged_bytes"),
        "api_calls": manifest.get("api_calls"),
        "failed_units": failed,
        "skipped_units": skipped,
        "partial_units": partial,
        "missing_markers": missing_markers,
        "short_uploads": short_uploads,
        "stale_extra": stale_extra,
        "record_census": {r["unit"]: r.get("records")
                          for r in results if r.get("records") is not None},
        "hint": None if ok else
        "failed_units / missing markers / short uploads: re-run transfer "
        "(per-unit .cdp-complete markers and cursors resume mid-module, "
        "azcopy --overwrite=false skips landed blobs), then re-verify. "
        "stale_extra alone is informational: no-overwrite kept an earlier "
        "pass's longer file. skipped_units are deliberate and never a "
        "failure — read their reasons.",
    }


# ── subcommands (the engine covers create-vm/allow-network/check-azure) ──────

def cmd_plan(root: Path, args) -> dict:
    """Thin wrapper over eng.cmd_plan: the engine prints dest as
    <container>/<dest_prefix>, but the operator confirms a PRODUCT dest at
    this gate, so the product suffix is added here rather than poisoning the
    dest_prefix tag."""
    result = eng.cmd_plan(SPEC, root, args)
    product = _product({}, args)
    result["product"] = product
    cfg_container = result["container"]
    result["dest"] = f"{cfg_container}/{_product_prefix({}, args)}"
    result["dest_prefix_tag"] = args.dest_prefix
    if getattr(args, "dest_exact", None):
        result["dest_exact"] = args.dest_exact
    result["source"] = f"zoho:{args.dc}"
    if product == "learn":
        result["portal"] = getattr(args, "portal", None)
    result["note"] = (
        result["note"] + " Multi-product: the dest_prefix TAG stays the base "
        "(no product suffix) so one VM can serve crm/learn/workdrive; each "
        "product gets its own tmux window and its own manifest.")
    return result


def cmd_write_dest(root: Path, args) -> dict:
    """racwl SAS -> rclone [azure] section AND azcopy dest.env, both on-VM.
    The dest URL carries the PRODUCT prefix; the tag stays the base."""
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    base_prefix = _dest_prefix(vm, args)
    prefix = _product_prefix(vm, args)
    sas, expiry = eng.mint_container_sas(cfg, args.sas_days, args.dry_run)
    base = _container_url(cfg)
    eng.write_conf_section(vm["public_ip"], "azure",
                           f"[azure]\ntype = azureblob\n"
                           f"sas_url = {base}?{sas}\n",
                           dry_run=args.dry_run)
    # values are SINGLE-QUOTED: the SAS contains '&', and sourcing an
    # unquoted VAR=a&b line backgrounds the assignment at the '&' — the
    # var lands empty and azcopy hits Azure with no SAS at all (401,
    # found live on checkmate). az SAS/URLs never contain single quotes.
    env = (f"AZURE_DEST_URL='{base}/{prefix}'\n"
           f"AZURE_DEST_SAS='{sas}'\n"
           f"AZURE_DEST_CONTAINER='{cfg['container']}'\n"
           f"AZURE_DEST_PREFIX='{prefix}'\n")
    _write_env(vm["public_ip"], _dest_env(_product(vm, args)), env,
               args.dry_run)
    return {"remote": "azure", "container": cfg["container"],
            "product": _product(vm, args),
            "dest_prefix_tag": base_prefix, "dest_prefix": prefix,
            "sas_expiry": expiry,
            "written_to": [f"{vm['name']}:{eng.RCLONE_CONF}",
                           f"{vm['name']}:{_dest_env(_product(vm, args))}"]}


def cmd_write_creds(root: Path, args) -> dict:
    """4 stdin lines -> 600 zoho.env on the VM, then a mint + one CRM
    settings call with the sourced credentials as a smoke test."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    # unlike probe, this always reads + validates stdin (the s3
    # write-s3-creds semantics): the 4-line contract is checked even in
    # dry-run, with the values replaced before anything is written
    lines = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    if len(lines) != 4:
        raise common.HarnessError(
            "stdin must be exactly 4 lines, in order: data center, Client "
            "ID, Client Secret, Refresh Token")
    dc = validate_dc(lines[0])
    _dc_guard(dc, vm.get("tags") or {})
    # single-quoted for the same reason as dest.env (see cmd_write_dest).
    # Zoho ids/secrets/tokens are [A-Za-z0-9._-] in practice, but a stray
    # quote would produce a silently corrupt env file — refuse instead.
    # Checked on the RAW lines, BEFORE the dry-run substitution, so
    # --dry-run exercises the guard for real (the values are still never
    # written or echoed in dry-run).
    for v in lines[1:]:
        if "'" in v:
            raise common.HarnessError(
                "a credential contains a single quote — refusing to write a "
                "corrupt env file. Re-copy the value from the Zoho API "
                "console; nothing was written.")
    values = (["<client-id>", "<client-secret>", "<refresh-token>"]
              if args.dry_run else lines[1:])
    env = (f"ZOHO_DC='{dc}'\n"
           f"ZOHO_CLIENT_ID='{values[0]}'\n"
           f"ZOHO_CLIENT_SECRET='{values[1]}'\n"
           f"ZOHO_REFRESH_TOKEN='{values[2]}'\n")
    _write_env(vm["public_ip"], ZOHO_ENV, env, args.dry_run)
    # The dry-run echo of this command must stay brace-free (the test
    # harness finds the JSON tail with stdout.index("{")): $VAR never
    # ${VAR}, -D -/sed never -w '%{http_code}', $(...) never braces.
    smoke = eng.run_ssh(
        vm["public_ip"],
        f"set -a; . {ZOHO_ENV}; set +a; "
        "T=$(curl -s -X POST "
        "\"https://accounts.zoho.$ZOHO_DC/oauth/v2/token\" "
        "-d grant_type=refresh_token "
        "-d \"client_id=$ZOHO_CLIENT_ID\" "
        "-d \"client_secret=$ZOHO_CLIENT_SECRET\" "
        "-d \"refresh_token=$ZOHO_REFRESH_TOKEN\" "
        "| tr ',' '\\n' "
        "| sed -n 's/.*\"access_token\":\"\\([^\"]*\\)\".*/\\1/p'); "
        "test -n \"$T\" || exit 7; "
        "curl -s -o /dev/null -D - "
        "-H \"Authorization: Zoho-oauthtoken $T\" "
        "\"https://www.zohoapis.$ZOHO_DC/crm/v8/settings/modules"
        "?per_page=1\" "
        "| sed -n 's,^HTTP[^ ]* \\([0-9]*\\).*,\\1,p' | tail -1",
        dry_run=args.dry_run, check=False, timeout=90)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "dc": dc,
                "written_to": f"{vm['name']}:{ZOHO_ENV}"}
    code = (smoke.stdout or "").strip()
    if smoke.returncode == 7:
        return {"ok": False, "stage": "token-mint", "dc": dc,
                "hint": "the VM could not mint an access token — wrong data "
                        "center, revoked refresh token, or a deleted Self "
                        "Client. Run probe from the laptop for the precise "
                        "diagnosis; the creds are written either way, so "
                        "fix and re-run write-creds to replace them."}
    if code == "200":
        return {"ok": True, "dc": dc,
                "written_to": f"{vm['name']}:{ZOHO_ENV}"}
    return {"ok": False, "stage": "creds-smoke-test", "http_code": code,
            "dc": dc,
            "hint": "the token minted but the CRM settings call failed. "
                    "401 = scope list missing ZohoCRM.settings.ALL; "
                    "anything else = network/API trouble from the VM. The "
                    "creds are written either way — fix and re-run "
                    "write-creds to replace them."}


# ── probe ────────────────────────────────────────────────────────────────────

LEARN_KB_CANDIDATES = (
    "/learn/api/v1/portal/{portal}/manual",
    "/learn/api/v1/portal/{portal}/space",
    "/learn/api/v1/portal/{portal}/article",
)


def _attempt(box: TokenBox, host: str, path: str,
             params: dict | None = None) -> tuple[str, object]:
    """Try one endpoint and classify the outcome without ever failing the
    probe. Zoho has no has_wiki analogue — whether a feature exists is only
    knowable by asking, so discovery is a first-class artifact."""
    try:
        return "ok", zoho_get(box, host, path, params)
    except ZohoHTTPError as e:
        return f"{e.status}-{e.code or 'no-code'}", None
    except common.HarnessError as e:
        return f"error: {str(e)[:120]}", None


def _probe_crm(box: TokenBox, dc: str, args) -> dict:
    host = api_host(dc)
    org_state, org = _attempt(box, host, "/crm/v8/org")
    mod_state, mods = _attempt(box, host, "/crm/v8/settings/modules")
    if args.dry_run:
        zoho_get(box, host, "/crm/v8/Leads",
                 {"fields": "id", "per_page": args.sample_records})
        return {"modules": [], "dry_run": True}
    if mod_state != "ok" or not isinstance(mods, dict):
        raise common.HarnessError(
            f"could not list CRM modules ({mod_state}) — without "
            "/settings/modules there is nothing to plan. Check that the "
            "scope list includes ZohoCRM.settings.ALL.")
    usable, skipped = [], []
    for m in mods.get("modules") or []:
        name = m.get("api_name")
        if not name:
            continue
        if m.get("deleted"):
            skipped.append({"module": name, "reason": "deleted"})
        elif not m.get("api_supported"):
            skipped.append({"module": name, "reason": "api_supported=false"})
        else:
            usable.append(name)
    sample_state, sample = ("skipped", None)
    more = None
    if usable:
        target = "Leads" if "Leads" in usable else usable[0]
        sample_state, sample = _attempt(
            box, host, f"/crm/v8/{target}",
            {"fields": "id", "per_page": args.sample_records})
        if isinstance(sample, dict):
            more = (sample.get("info") or {}).get("more_records")
    counts, count_note = {}, None
    attach, attach_note = {"available": False, "reason": "not probed"}, None
    if args.count_probe and usable:
        counts, count_note = _crm_counts(box, host, usable)
        attach, attach_note = _attachment_census(box, host)
    org_name = None
    if isinstance(org, dict) and org.get("org"):
        org_name = (org["org"][0] or {}).get("company_name")
    return {
        "org_name": org_name, "org_call": org_state,
        "modules_usable": len(usable), "modules_skipped": len(skipped),
        "modules": usable[:60], "modules_skipped_detail": skipped[:20],
        "sample_call": sample_state, "sample_has_more_records": more,
        "record_counts": counts, "record_count_note": count_note,
        "attachments": attach, "attachment_note": attach_note,
    }


def _crm_counts(box: TokenBox, host: str, modules: list[str]) -> tuple:
    """Per-module record census via GET /crm/v8/<Module>/actions/count.

    NOT COQL: COQL makes `where` mandatory and additionally requires a
    `group by` for count(id), so a plain per-module count is impossible
    through it (verified live on song-division, 2026-08-24). The dedicated
    count endpoint returns an exact integer and is the only honest
    pre-run census we have — there is still no BYTE estimate, because Zoho
    publishes no record size.

    Modules that legitimately refuse (400 NOT_SUPPORTED — e.g. Attachments)
    are recorded as null rather than dropped, so the map says "asked, and
    this module has no count" instead of staying silently absent.
    """
    counts: dict[str, object] = {}
    unsupported = 0
    for name in modules:
        try:
            payload = zoho_get(box, host, f"/crm/v8/{name}/actions/count")
            counts[name] = (payload or {}).get("count")
        except ZohoHTTPError:
            counts[name] = None
            unsupported += 1
        except common.HarnessError:
            break  # auth/scope trouble — the caller already knows
    total = sum(v for v in counts.values() if isinstance(v, int))
    note = ("exact per-module record counts from "
            "/crm/v8/<Module>/actions/count. No BYTE figure is given for "
            "records — Zoho exposes no record size. Attachment bytes are "
            "reported separately and ARE exact.")
    if unsupported:
        note += (f" {unsupported} module(s) returned null — they do not "
                 "support counting (normal, e.g. Attachments).")
    return {"per_module": counts, "total_records": total}, note


def _attachment_census(box: TokenBox, host: str) -> tuple:
    """Walk /crm/v8/Attachments for a real file+byte census.

    The obvious approach — one Attachments call per record — is ~2.2 calls/s
    and would take ~32 h on a 251k-record org. This module is directly
    listable at ~300 rows/s (whole tenant in ~28 s) and its Size field is an
    EXACT byte integer, so this is the one genuinely honest pre-run size
    number the Zoho API gives us. Best-effort: any failure degrades to
    "not available" rather than failing the probe."""
    by_module: dict[str, list] = {}
    page_token = None
    files = total = pages = 0
    try:
        while pages < 2000:
            params = {"fields": "id,Size,Parent_Id", "per_page": 200}
            if page_token:
                params["page_token"] = page_token
            payload = zoho_get(box, host, "/crm/v8/Attachments", params) or {}
            rows = payload.get("data") or []
            for rec in rows:
                files += 1
                try:
                    size = int(rec.get("Size") or 0)
                except (TypeError, ValueError):
                    size = 0
                total += size
                mod = (((rec.get("Parent_Id") or {}).get("module") or {})
                       .get("api_name") or "(unknown)")
                slot = by_module.setdefault(mod, [0, 0])
                slot[0] += 1
                slot[1] += size
            info = payload.get("info") or {}
            page_token = info.get("next_page_token") \
                if info.get("more_records") else None
            pages += 1
            if not page_token:
                break
    except (ZohoHTTPError, common.HarnessError) as e:
        return {"available": False, "reason": str(e)[:200]}, None
    top = sorted(by_module.items(), key=lambda kv: -kv[1][1])[:15]
    return ({"available": True, "files": files, "bytes": total,
             "human": common.human_bytes(total),
             "by_parent_module": {m: {"files": v[0], "bytes": v[1]}
                                  for m, v in top}},
            "attachment bytes are EXACT (Size is an integer on this "
            "endpoint, not the rounded string the docs describe), so this "
            "IS a real size census. Records carry no byte size and are not "
            "included here.")


def _probe_learn(box: TokenBox, dc: str, portal: str, args) -> dict:
    """Courses are documented; the knowledge-base half is not. Attempt both
    and report what answered — Learn KB is DISCOVERED, never assumed."""
    discovery: dict[str, str] = {}
    course_host = None
    courses = None
    for host in learn_hosts(dc):
        state, payload = _attempt(
            box, host, f"/learn/api/v1/portal/{portal}/course",
            {"pageIndex": 0, "limit": 1})
        discovery[f"{host} /course"] = state
        if state == "ok":
            course_host = host
            courses = payload
            break
    if args.dry_run:
        for cand in LEARN_KB_CANDIDATES:
            zoho_get(box, learn_hosts(dc)[0], cand.format(portal=portal),
                     {"limit": 1})
        return {"portal": portal, "dry_run": True}
    kb: dict[str, str] = {}
    if args.kb_probe and course_host:
        for cand in LEARN_KB_CANDIDATES:
            path = cand.format(portal=portal)
            state, _ = _attempt(box, course_host, path, {"limit": 1})
            kb[path] = state
    kb_reachable = [p for p, s in kb.items() if s == "ok"]
    dash = {}
    if isinstance(courses, dict):
        dash = courses.get("dashboard") or {}
    return {
        "portal": portal, "course_host": course_host,
        "course_discovery": discovery,
        "total_courses": dash.get("totalCourses"),
        "kb_probe": kb, "kb_reachable": kb_reachable,
        "kb_note": (
            "the knowledge-base half of Zoho Learn (manuals, spaces, "
            "articles) has NO documented API. These paths were ATTEMPTED, "
            "not assumed. " + (
                f"{len(kb_reachable)} answered, so the pull will include "
                "them and record exactly what it found."
                if kb_reachable else
                "None answered, so the pull covers COURSES ONLY — say that "
                "to the client before quoting Learn scope, and treat the "
                "knowledge base as a separate manual-export conversation.")),
    }


def _probe_workdrive(box: TokenBox, dc: str, args) -> dict:
    """WorkDrive is 'whatever the token can see' — enumerate the reachable
    boundary and report it rather than asserting a scope."""
    host = api_host(dc)
    me_state, me = _attempt(box, host, "/workdrive/api/v1/users/me")
    if args.dry_run:
        zoho_get(box, host, "/workdrive/api/v1/users/me/teams")
        return {"dry_run": True}
    user_id = None
    if isinstance(me, dict):
        user_id = (me.get("data") or {}).get("id")
    if user_id:
        teams_state, teams = _attempt(
            box, host, f"/workdrive/api/v1/users/{user_id}/teams")
    else:
        teams_state, teams = "skipped-no-user-id", None
    team_list = []
    if isinstance(teams, dict):
        for t in teams.get("data") or []:
            attrs = t.get("attributes") or {}
            team_list.append({"id": t.get("id"),
                              "name": attrs.get("name")})
    return {"me_call": me_state, "user_id_seen": bool(user_id),
            "teams_call": teams_state, "teams": team_list[:20],
            "boundary_note":
                "WorkDrive scope is whatever the granted token reaches. The "
                "pull records the reachable boundary in _meta/boundary.json; "
                "anything outside it was never visible to us, which is a "
                "client conversation, not a failure."}


def cmd_probe(root: Path, args) -> dict:
    """Day-one gate, BEFORE any billable resource: do the credentials work,
    is the data center right, what is actually there? Laptop-side, Zoho API
    JSON only — no corpus data, no Azure access, no VM needed. The four
    credentials are read from stdin into process memory and discarded.

    Emits no byte estimate for RECORDS (Zoho exposes no record size), but
    the attachment census IS honest: /crm/v8/Attachments is directly
    listable at ~300 rows/s and reports exact byte sizes, so probe walks it
    and reports real totals rather than inventing one.
    """
    eng.load_cfg(root, args.slug)  # onboarding check only; no network from it
    dc, client_id, client_secret, refresh = read_credentials(args.dry_run)
    if args.dc and validate_dc(args.dc) != dc:
        raise common.HarnessError(
            f"--dc {args.dc} contradicts the data center on stdin ({dc}) — "
            "they must agree; nothing was contacted.")
    product = args.product
    box = TokenBox(dc, client_id, client_secret, refresh, args.dry_run)
    box.mint()  # proves creds + DC before anything else runs
    if product == "learn" and not args.portal:
        raise common.HarnessError(
            "probe --product learn requires --portal <networkurl> (the "
            "portal segment of the client's Learn URL, e.g. "
            "learn.zoho.com/portal/zylker-network -> zylker-network)")
    if product == "crm":
        detail = _probe_crm(box, dc, args)
    elif product == "learn":
        detail = _probe_learn(box, dc, args.portal, args)
    else:
        detail = _probe_workdrive(box, dc, args)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "dc": dc, "product": product,
                **detail}
    return {"ok": True, "dc": dc, "product": product,
            "api_domain": box.api_domain, **detail,
            "note": "no byte estimate is given on purpose — Zoho exposes no "
                    "record size and attachment Size is a rounded string. "
                    "Do not quote a timeline off this; pilot a single "
                    "module first."}


# ── transfer / status / verify / teardown / discover ─────────────────────────

def cmd_transfer(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    product = _product(vm, args)
    # Refusal is per WINDOW, not per session: three products share one VM
    # and one tmux session, so the engine's session-level check would
    # wrongly block learn while crm is running.
    if product in _tmux_windows(ip, args.dry_run):
        return {"ok": False, "cause": "already-running", "product": product,
                "hint": f"tmux window '{product}' is alive — use status "
                        "--product " + product}
    dc = getattr(args, "dc", None) or (vm.get("tags") or {}).get(SPEC.loc_tag)
    portal = _portal(vm, args)
    if product == "learn" and not portal:
        raise common.HarnessError(
            "transfer --product learn requires --portal (no zoho_portal VM "
            "tag exists)")
    _push_puller(ip, args.dry_run)
    flags = ""
    for flag, val in (("--limit", args.limit), ("--only", args.only),
                      ("--modules", args.modules),
                      ("--skip-modules", args.skip_modules),
                      ("--since", args.since),
                      ("--email-modules", args.email_modules),
                      ("--page-size", args.page_size),
                      ("--attachment-workers", args.attachment_workers),
                      ("--rate-sleep-max", args.rate_sleep_max)):
        if val:
            flags += f" {flag} {val}"
    for flag, on in (("--refresh", args.refresh),
                     ("--skip-upload", args.skip_upload),
                     ("--no-attachments", args.no_attachments),
                     ("--no-bulk", args.no_bulk),
                     ("--no-emails", args.no_emails)):
        if on:
            flags += f" {flag}"
    if portal:
        flags += f" --portal {portal}"
    log = _log_file(product)
    inner = (f"set -a; . {ZOHO_ENV}; . {_dest_env(product)}; set +a; "
             f"python3 {XFER_DIR}/zoho_vm_pull.py "
             f"--product {product} "
             f"--dest {DEST_DIR}/{product}{flags} >> {log} 2>&1")
    # `new-session -A` would ATTACH to an existing session and silently
    # ignore -n and the command, so the second product would never launch.
    # Branch on session liveness the way s3_transfer.py does for its
    # worker windows (s3_transfer.py:395-399).
    cmd = f"\"bash -c '{inner}'\""
    if eng._tmux_alive(ip, args.dry_run):
        launch = f"tmux new-window -t {eng.TMUX_SESSION} -n {product} {cmd}"
    else:
        launch = (f"tmux new-session -d -s {eng.TMUX_SESSION} "
                  f"-n {product} {cmd}")
    eng.run_ssh(ip, launch, dry_run=args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "product": product, "dc": dc}
    eng.run_ssh(ip, "sleep 5", check=False)
    alive = product in _tmux_windows(ip, False)
    tail = eng.run_ssh(ip, f"tail -3 {log} 2>/dev/null", check=False)
    return {"ok": alive, "session": eng.TMUX_SESSION, "window": product,
            "product": product, "dc": dc, "portal": portal,
            "pilot_limit": args.limit or None,
            "log_tail": (tail.stdout or "").strip().splitlines(),
            "note": ("re-running transfer is safe — per-unit .cdp-complete "
                     "markers skip finished units, .cdp-cursor.json resumes "
                     "a partial module mid-walk, and azcopy "
                     "--overwrite=false skips landed blobs"
                     if alive else None),
            "hint": None if alive else
            f"the puller died immediately — tail {log} on the VM "
            "(bad env files? wrong data center?)"}


_STATUS_PY = r"""
import json, os, shutil, sys
base = os.path.expanduser("~/xfer-zoho")
out = {"products": {}}
for product in ("crm", "learn", "workdrive"):
    dest = os.path.join(base, "dest", product)
    if not os.path.isdir(dest):
        continue
    entry = {}
    try:
        entry["progress"] = json.load(open(os.path.join(dest,
                                                        "progress.json")))
    except (OSError, ValueError):
        entry["progress"] = None
    try:
        m = json.load(open(os.path.join(dest, "manifest.json")))
        entry["manifest"] = dict(unit_count=m.get("unit_count"),
                                 total_staged_bytes=m.get(
                                     "total_staged_bytes"),
                                 api_calls=m.get("api_calls"),
                                 failed_units=m.get("failed_units"),
                                 skipped_units=m.get("skipped_units"),
                                 finished_utc=m.get("finished_utc"))
    except (OSError, ValueError):
        entry["manifest"] = None
    try:
        lines = open(os.path.join(base,
                                  "pull-%s.log" % product)).read().splitlines()
        entry["log_tail"] = lines[-5:]
    except OSError:
        entry["log_tail"] = []
    out["products"][product] = entry
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
    windows = _tmux_windows(vm["public_ip"], args.dry_run)
    proc = eng.run_ssh(vm["public_ip"], "python3 -", stdin_data=_STATUS_PY,
                       dry_run=args.dry_run, check=False, timeout=120)
    if args.dry_run:
        return {"ok": True, "dry_run": True}
    try:
        detail = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        detail = {"aggregator_error": (proc.stdout or proc.stderr)[-300:]}
    products = detail.get("products") or {}
    if getattr(args, "product", None):
        products = {k: v for k, v in products.items() if k == args.product}
        detail["products"] = products
    hints = []
    for name, entry in products.items():
        if name in windows:
            continue
        hints.append(
            f"{name}: a pass finished — run verify --product {name}"
            if entry.get("manifest") else
            f"{name}: not running and no manifest — it never finished a "
            f"pass; tail {_log_file(name)} on the VM, then re-run transfer")
    staged = {name: common.human_bytes(
        (entry.get("manifest") or {}).get("total_staged_bytes") or 0)
        for name, entry in products.items()}
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "running_windows": windows, "staged_bytes_human": staged,
            "hint": "; ".join(hints) or None,
            "note": "a 'rate-limited; sleeping Ns' line in a log tail is "
                    "normal pacing against Zoho's API credits, not a hang",
            **detail}


def cmd_verify(root: Path, args) -> dict:
    """Laptop-side; the VM may already be torn down. Lists the product's dest
    prefix and compares against the uploaded manifest.json — see
    compare_manifest_to_blobs for exactly what is (and is not) asserted.
    Takes NO Zoho credentials."""
    cfg = eng.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    base = args.dest_prefix or SPEC.default_dest_prefix
    prefix = (args.dest_exact.strip("/") if args.dest_exact
              else f"{base}/{args.product}")
    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl -- the READ path
        blobs = azure_list_blobs(cfg, sas, prefix, args.dry_run)
        if args.dry_run:
            print(f"DRY-RUN: GET {_container_url(cfg)}/{prefix}/"
                  "manifest.json?<sas-redacted>")
            return {"ok": True, "dry_run": True, "prefix": prefix}
        mname = f"{prefix}/manifest.json"
        if mname not in blobs:
            return {"ok": False, "cause": "no-manifest", "prefix": prefix,
                    "blobs_under_prefix": len(blobs),
                    "hint": "no manifest.json under the prefix — the pull "
                            "never finished a pass (or uploaded with "
                            "--skip-upload). Run status/transfer on the "
                            "VM first."}
        manifest = json.loads(azure_get(
            f"{_container_url(cfg)}/{urllib.parse.quote(mname, safe='/')}"
            f"?{sas}"))
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)
    result = compare_manifest_to_blobs(manifest, blobs, prefix)
    result.update({"prefix": prefix, "blobs_under_prefix": len(blobs),
                   "dc": manifest.get("dc"),
                   "finished_utc": manifest.get("finished_utc")})
    if result["ok"] and result.get("partial_units"):
        result["warning"] = (
            "STAGED->CONTAINER IS CLEAN, BUT THE LEDGER IS SHORT OF THE "
            "SOURCE for: "
            + ", ".join(f"{u['unit']} ({u.get('records')}/"
                        f"{u.get('source_count')})"
                        for u in result["partial_units"])
            + ". Zoho stops page_token pagination at 100,000 records and the "
              "module exceeds what two-directional paging can reach. Re-"
              "running will NOT fix it — use that module's Bulk Read ZIP, or "
              "slice the pull by date. Say this out loud when reporting "
              "completeness.")
    if result["ok"]:
        result["note"] = (
            "certifies staged->container completeness against the uploaded "
            "manifest; Zoho->staged completeness is the puller's exit code "
            "+ failed_units (clean here). No source-size claim is made for "
            "the record ledger (Zoho exposes no record byte size); "
            "attachment bytes ARE exact and are reported. After this, make "
            "sure "
            f'the matching service in expected-data-sizes.json covers '
            f'"{prefix}" (a pin, or a name that normalizes to it), then '
            "let size-company pick it up.")
    return result


def cmd_teardown(root: Path, args) -> dict:
    result = eng.cmd_teardown(SPEC, root, args)
    if result.get("ok") and "reminders" in result:
        result["reminders"][-1] = (
            "Tell the client they can revoke the refresh token and DELETE "
            "the Self Client now (api-console.zoho.com -> the Self Client "
            "-> revoke/delete) — revocation on their side is the clean end "
            "of the engagement. The account-wide scopes make this "
            "non-optional.")
    return result


def cmd_discover(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    if args.dry_run:
        eng.get_vm(SPEC, cfg, args.slug, True)
        return {"phase": "unknown (dry-run)",
                "note": "dry-run prints the discovery commands only"}
    vm = eng.get_vm(SPEC, cfg, args.slug, False)
    if vm is None:
        return {"phase": "pre-setup", "vm": None,
                "hint": "no transfer VM — run probe (needs only the four "
                        "credentials), then setup"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base,
                "hint": "VM exists but has no public IP (deallocated?)"}
    checks = [f"test -f {ZOHO_ENV} && echo zoho-env"]
    for product in PRODUCTS:
        checks.append(f"test -f {_dest_env(product)} && echo dest-env-"
                      f"{product}")
    for product in PRODUCTS:
        checks.append(f"test -f {DEST_DIR}/{product}/manifest.json "
                      f"&& echo manifest-{product}")
    checks.append(f"tmux has-session -t {eng.TMUX_SESSION} 2>/dev/null "
                  "&& echo tmux-alive")
    probe = eng.run_ssh(vm["public_ip"], "; ".join(checks), check=False)
    if probe.returncode != 0 and not (probe.stdout or "").strip():
        return {"phase": "vm-unreachable", **base,
                "hint": "ssh failed — VM booting, or your key changed. "
                        f"Try: ssh {eng.ADMIN_USER}@{vm['public_ip']}"}
    out = probe.stdout or ""
    done = [p for p in PRODUCTS if f"manifest-{p}" in out]
    base["products_with_manifest"] = done
    if "tmux-alive" in out:
        return {"phase": "transfer-running", **base,
                "running_windows": _tmux_windows(vm["public_ip"], False),
                "hint": "use status for progress"}
    if done:
        return {"phase": "transfer-stopped", **base,
                "hint": "a pass finished for " + ", ".join(done) +
                        " — run status, then verify --product <name> "
                        "(laptop-side); failed units mean re-run transfer"}
    if not ("zoho-env" in out and "dest-env-" in out):
        return {"phase": "mid-setup", **base,
                "hint": "VM up but creds/dest incomplete — resume setup at "
                        "the missing write-dest / write-creds step."}
    return {"phase": "setup-complete", **base,
            "hint": "creds + dest in place — run transfer --product crm "
                    "--only records/Leads (pilot) first"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[
        "discover", "plan", "create-vm", "allow-network", "write-dest",
        "check-azure", "write-creds", "probe", "transfer", "status",
        "verify", "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--dc", default=None,
                   help="Zoho data center: " + " / ".join(DCS) +
                        " (required for plan/create-vm/probe; later read "
                        "from the VM's zoho_dc tag)")
    p.add_argument("--product", choices=list(PRODUCTS), default=None,
                   help="which Zoho product (required for probe/transfer/"
                        "verify; rides the zoho_product VM tag)")
    p.add_argument("--portal", default=None,
                   help="Zoho Learn portal networkurl (required with "
                        "--product learn; rides the zoho_portal VM tag)")
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None,
                   help="create-vm: OS disk GB (default: the Spec's 512 — "
                        "staging holds the whole corpus before azcopy)")
    p.add_argument("--dest-prefix", default=None,
                   help=f"BASE prefix inside <slug>-raw (default "
                        f"{SPEC.default_dest_prefix}); the product is "
                        f"appended, e.g. {SPEC.default_dest_prefix}/crm")
    p.add_argument("--dest-exact", dest="dest_exact", default=None,
                   help="LITERAL dest prefix — the product is NOT appended. "
                        "Use for one top-level folder per product "
                        "(e.g. --dest-exact zoho_crm). Applies to "
                        "plan/write-dest/verify.")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--modules", default=None,
                   help="transfer/crm: only these modules (comma-separated)")
    p.add_argument("--skip-modules", dest="skip_modules", default=None,
                   help="transfer/crm: exclude these modules")
    p.add_argument("--limit", type=int, default=0,
                   help="transfer: only the first N units (pilot)")
    p.add_argument("--only", default=None,
                   help="transfer: only this one unit, e.g. records/Leads")
    p.add_argument("--refresh", action="store_true",
                   help="transfer: ignore .cdp-complete markers and cursors")
    p.add_argument("--skip-upload", dest="skip_upload", action="store_true",
                   help="transfer: pull to VM disk but don't azcopy")
    p.add_argument("--no-attachments", dest="no_attachments",
                   action="store_true", help="transfer/crm: skip attachments")
    p.add_argument("--no-bulk", dest="no_bulk", action="store_true",
                   help="transfer/crm: skip the Bulk Read archive ZIPs")
    p.add_argument("--no-emails", dest="no_emails", action="store_true",
                   help="transfer/crm: skip the per-record email sweep")
    p.add_argument("--email-modules", dest="email_modules", default=None,
                   help="transfer/crm: restrict the email sweep to these "
                        "modules (comma-separated). The sweep is ONE call "
                        "per record (~1.3/s), so scoping it to the modules "
                        "that carry mail is the difference between hours "
                        "and days")
    p.add_argument("--attachment-workers", dest="attachment_workers",
                   type=int, default=0,
                   help="transfer/crm: parallel attachment downloads "
                        "(default 3, hard cap 5 — staying under the "
                        "smallest edition's concurrency ceiling)")
    p.add_argument("--page-size", dest="page_size", type=int, default=0,
                   help="transfer: records per page (default and max 200; "
                        "credits scale with CALLS, so smaller is strictly "
                        "worse)")
    p.add_argument("--rate-sleep-max", dest="rate_sleep_max", type=int,
                   default=0, help="transfer: cap on one 429 sleep (s)")
    p.add_argument("--since", default=None,
                   help="transfer/crm: Modified_Time top-up (ISO-8601). "
                        "NEVER use on the first pass — it would silently "
                        "produce a partial ledger")
    p.add_argument("--sample-records", dest="sample_records", type=int,
                   default=1, help="probe: records in the sample page")
    p.add_argument("--kb-probe", dest="kb_probe", action="store_true",
                   default=True,
                   help="probe/learn: attempt the undocumented "
                        "knowledge-base paths (default on)")
    p.add_argument("--no-kb-probe", dest="kb_probe", action="store_false")
    p.add_argument("--count-probe", dest="count_probe", action="store_true",
                   default=True,
                   help="probe/crm: attempt a COQL count(id) census "
                        "(default on)")
    p.add_argument("--no-count-probe", dest="count_probe",
                   action="store_false")
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-transfer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.command in ("plan", "create-vm", "probe") and args.dc is None:
        p.error(f"{args.command} requires --dc")
    if args.command in ("plan", "probe", "transfer", "verify") \
            and args.product is None:
        p.error(f"{args.command} requires --product "
                f"({'|'.join(PRODUCTS)})")
    if args.command in ("probe", "transfer") and args.product == "learn" \
            and args.portal is None:
        p.error(f"{args.command} --product learn requires --portal")
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    engine_cmds = {"plan": None, "create-vm": eng.cmd_create_vm,
                   "allow-network": eng.cmd_allow_network,
                   "check-azure": eng.cmd_check_azure}
    own_cmds = {"discover": cmd_discover, "plan": cmd_plan,
                "write-dest": cmd_write_dest, "write-creds": cmd_write_creds,
                "probe": cmd_probe, "transfer": cmd_transfer,
                "status": cmd_status, "verify": cmd_verify,
                "teardown": cmd_teardown}
    try:
        if args.command in engine_cmds and engine_cmds[args.command]:
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
