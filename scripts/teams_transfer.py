#!/usr/bin/env python3
"""Microsoft Teams -> Azure transfer CLI (VM REST puller; VM family).

Teams has no rclone backend and no bulk export: the corpus is every team's
every channel's every message thread plus replies plus embedded
hostedContents, walked page by page against Microsoft Graph — an app-only
client-credentials pull (no signed-in user, no delegated/browser flow
anywhere) that is throttled per-app and genuinely a multi-hour-to-multi-day
job for a real tenant. That shape is the github/zoho/figma precedent: this
script reuses transfer_engine.py's VM lifecycle (create-vm / allow-network /
check-azure / teardown run the engine functions verbatim) but owns the pull
layer — scripts/teams_vm_pull.py is pushed to the VM and runs the whole walk
there in tmux, azcopying each unit up as it completes, to
<slug>-raw/<dest-prefix>/ (default teams-export/).

The day-one stall this family is built around is NOT a bad token or a wrong
seat — it is Microsoft's own message-content gate. A tenant's app can mint a
token and read the directory (groups/users/channels) just fine and still get
refused reading actual channel MESSAGES: 402 means the tenant needs an Azure
subscription linked for Teams' metered API billing (model A/B), and 403 means
the tenant has not been approved for Microsoft's protected Teams APIs
(aka.ms/GraphTeamsProtectedApis) — an application process that takes days to
weeks and is a client conversation, not a retry. `probe` (this task) is the
gate: it mints a token and tries exactly one message page before any VM is
ever created, and classifies the answer as open / metered-model-required /
protected-api-approval-missing with a next_step sentence for each.

The sharepoint boundary: a Teams channel's file tab (and any attachment
object referenced from a message) is backed by a SharePoint document
library, not Teams storage — those bytes belong to a future sharepoint
ingest, not this one. This pull stages ONLY what Graph's Teams surface
itself owns: team/channel/member directory, message + reply JSON, and
hostedContents (the inline images/files embedded directly in a message's
HTML body, served from `.../messages/{id}/hostedContents/{id}/$value`).
Attachment objects are left as references in the message JSON, never
fetched — see teams_vm_pull.py's module docstring for the exact regex
boundary.

Auth: a client-made Entra ID (Azure AD) app registration, application
(not delegated) permissions — Group.Read.All or Team.ReadBasic.All (team/
channel directory), User.Read.All (tenant roster), and the Teams messaging
permissions (ChannelMessage.Read.All, Chat.Read.All is explicitly OUT of
scope unless the client says otherwise — see probe's chat check) — with
admin consent granted. THREE secrets arrive on stdin, in order: tenant id
(the Entra "Directory (tenant) ID" GUID), client id, client secret. They
travel only over ssh stdin into a 600 env file on the VM (never argv, tags,
logs, or laptop files); the write-creds tenant guard below is this family's
wrong-DC-guard analogue — a secret minted for the wrong tenant is silently
useless until it fails loudly, so a stdin/flag disagreement is refused
outright rather than written.

Verify runs on the LAPTOP, same as figma/zoho: the completeness authority is
the manifest.json the pull uploads to <dest-prefix>/_meta/manifest.json, so
no Graph access is needed at verify time — just a blob listing over
phases.ip_rule_ensure + an rl account SAS.

This CLI covers the full lifecycle: the Spec, the 3-line credential plumbing
(read_secrets, cmd_write_creds with its tenant guard), the laptop-side probe
gate, and transfer/status/verify/teardown.
"""
from __future__ import annotations

import json
import re
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
import teams_vm_pull as puller  # noqa: E402  (import-safe; pure helpers)

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
X_MS_VERSION = "2021-08-06"

_sleep = time.sleep  # seam so tests can record/skip waits


def _http(req: urllib.request.Request, timeout: int = 60):
    """Single transport seam (tests stub this; production never branches)."""
    return urllib.request.urlopen(req, timeout=timeout)


def validate_tenant_id(raw: str) -> str:
    t = (raw or "").strip()
    if not GUID_RE.match(t):
        raise common.HarnessError(
            f"tenant id {t!r} is not a GUID — copy the Directory (tenant) "
            "ID from the client's Entra app registration page")
    return t.lower()


def validate_client_id(raw: str) -> str:
    """The app registration's Application (client) ID is always a GUID.
    write-creds writes it UNQUOTED into the VM's sourced env file (unlike
    the secret, which is single-quoted) — a non-GUID value could carry a
    shell metacharacter (`&`, a space, a stray quote already refused by
    read_secrets) that corrupts that `source` line, so it is validated
    up front exactly like the tenant id."""
    c = (raw or "").strip()
    if not GUID_RE.match(c):
        raise common.HarnessError(
            f"client id {c!r} is not a GUID — copy the Application "
            "(client) ID from the client's Entra app registration page")
    return c


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


# ── VM-side plumbing ─────────────────────────────────────────────────────────

def _write_env(ip: str, path: str, content: str, dry_run: bool) -> None:
    """600 env file on the VM; content rides ssh stdin only."""
    eng.run_ssh(ip, f"umask 077 && mkdir -p {ENV_DIR} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _push_puller(ip: str, dry_run: bool) -> None:
    """Fresh copy every transfer so harness upgrades propagate."""
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > "
                    f"{XFER_DIR}/teams_vm_pull.py",
                stdin_data=PULLER_PY.read_text(), dry_run=dry_run)


def _dest_prefix(vm: dict, args) -> str:
    return (getattr(args, "dest_prefix", None)
            or (vm.get("tags") or {}).get("dest_prefix")
            or SPEC.default_dest_prefix)


def _tenant_guard(stdin_tenant: str, expected: str | None) -> None:
    """PURE. Refuse to write credentials whose tenant contradicts the
    EXPECTED one (--tenant-id, or failing that the VM's teams_tenant_id
    tag) — the zoho wrong-DC guard adapted to Teams: a client secret is
    only valid in the tenant it was issued for, and a creds/tag
    disagreement means someone is about to pull the wrong tenant. Dry-run
    VMs have empty tags (transfer_engine.py's require_vm), so an absent
    expectation is a no-op, never a failure."""
    if expected and expected != stdin_tenant:
        raise common.HarnessError(
            f"tenant mismatch: stdin declares tenant {stdin_tenant!r} but "
            f"the expected tenant is {expected!r} (from --tenant-id, or "
            f"the VM's {SPEC.loc_tag} tag). A client secret is only valid "
            "for the tenant it was issued in — either re-run with the "
            f"right --tenant-id, or tear down and re-create the VM with "
            f"--tenant-id {stdin_tenant}. Nothing was written.")


# ── azure listing (verify only; laptop path) ─────────────────────────────────
# Local copies of the established laptop-side blob-listing pair — kept here
# rather than imported so the VM-family CLIs stay import-independent of the
# local-pull family. Same signatures as figma_transfer.py's.

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
    """Pure. Certifies STAGED -> CONTAINER completeness against the
    uploaded manifest (Teams -> staged completeness is the puller's own
    exit code + failed_units — verify surfaces that list verbatim).

    Per successful unit (status ok or skipped-complete): the
    .cdp-complete marker blob must exist, and the per-unit container byte
    sum must be >= the staged bytes. LESS than staged = a partial upload
    = failure. MORE happens legitimately when a re-pull wrote a shorter
    file and --overwrite=false kept the older sibling — reported as
    stale_extra, informational.

    NO source-size claim is made: Graph publishes no message or
    attachment byte size anywhere, so the manifest's total_staged_bytes
    is the first honest byte number in this engagement's life.
    """
    results = manifest.get("results", [])
    missing_markers: list[str] = []
    short_uploads: list[dict] = []
    stale_extra: list[str] = []

    def rollup(sub: str) -> int:
        return sum(b["size"] for n, b in blobs.items() if n.startswith(sub))

    for r in results:
        if r.get("status") not in ("ok", "skipped-complete"):
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
    ok = not failed and not missing_markers and not short_uploads
    return {
        "ok": ok,
        "source": manifest.get("source"),
        "context": manifest.get("context"),
        "unit_count": manifest.get("unit_count"),
        "total_staged_bytes": manifest.get("total_staged_bytes"),
        "api_calls": manifest.get("api_calls"),
        "api_sleeps": manifest.get("api_sleeps"),
        "hosted_errors": manifest.get("hosted_errors"),
        "failed_units": failed,
        "skipped_units": skipped,
        "missing_markers": missing_markers,
        "short_uploads": short_uploads,
        "stale_extra": stale_extra,
        "hint": None if ok else
        "failed_units / missing markers / short uploads: re-run transfer "
        "(per-unit .cdp-complete markers and .cdp-cursor.json resume, "
        "azcopy --overwrite=false skips landed blobs), then re-verify. "
        "stale_extra alone is informational: no-overwrite kept an "
        "earlier pass's longer file. skipped_units are deliberate and "
        "never a failure — read their reasons (a channel that 404s on "
        "its first page is a fresh-start skip, not a corpus gap).",
    }


# ── laptop-side token mint (probe + write-creds smoke test) ──────────────────

class TokenBox:
    """App-only AAD client-credentials token, auto-refreshed. Laptop twin
    of teams_vm_pull.py's TokenBox — deliberately duplicated (the
    github/zoho precedent: this file and the VM puller must each stand on
    their own). It raises common.HarnessError where the VM twin raises
    SystemExit."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self._tenant = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._value: str | None = None
        self._exp = 0.0
        self.mints = 0

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
                        "token mint returned no access_token: "
                        f"{str(body)[:300]}")
                self._value = tok
                self._exp = time.time() + int(body.get("expires_in", 3599))
                self.mints += 1
                return tok
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", "replace")[:300]
                if e.code in (400, 401):
                    # AADSTS7000215 bad secret / AADSTS700016 bad app /
                    # AADSTS90002 bad tenant — never retryable
                    raise common.HarnessError(
                        f"token mint refused ({e.code}): {err} — check the "
                        "3 stdin values (tenant, client id, secret)")
                last = f"{e.code}: {err}"
            except (urllib.error.URLError, TimeoutError) as e:
                last = str(e)
            _sleep(min(60, 5 * attempt))
        raise common.HarnessError(f"token mint failed after retries: {last}")


def graph_get(token: str | None, path_or_url: str, params: dict | None = None,
              dry_run: bool = False):
    """One GET -> (status, parsed-json-or-None). `path_or_url` is either a
    Graph-relative path ("/groups") or an already-absolute @odata.nextLink
    URL. Never raises on an HTTP error status — probe classifies those
    itself (402/403 are expected, informative answers here, not failures);
    only a genuine transport failure raises common.HarnessError."""
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


def _graph_paginate(token: str, path: str, params: dict | None,
                    max_pages: int = 50) -> tuple[int, list]:
    """Follow @odata.nextLink to completion (capped) -> (first-page-status,
    all items). A non-2xx first page returns that status with an empty
    list; a later page's refusal just ends the walk with what was
    collected so far — probe treats that as a sample, not a failure."""
    status, body = graph_get(token, path, params)
    items: list = []
    pages = 0
    while status in (200, 201) and body is not None:
        items.extend(body.get("value", []))
        pages += 1
        nxt = body.get("@odata.nextLink")
        if not nxt or pages >= max_pages:
            break
        status, body = graph_get(token, nxt)
    return status, items


PAGE_SAMPLE_CAP = 20   # bound on the message-page-depth sample (I5) — a
                       # probe must stay cheap, never a partial corpus walk


def _sample_message_pages(token: str, gid: str, cid: str,
                          max_pages: int = PAGE_SAMPLE_CAP) -> int | None:
    """How many message pages a channel has, up to `max_pages` — page
    COUNT only (never message content, never bytes), used to scale
    probe's wall-clock estimate (I5). Returns None if even the first page
    is refused (the channel is excluded from the sample, not counted as
    depth 0 — a refusal is not the same as an empty channel)."""
    status, body = graph_get(
        token, f"/teams/{gid}/channels/{cid}/messages",
        {"$top": str(puller.MESSAGES_PAGE_SIZE)})
    if status not in (200, 201) or body is None:
        return None
    pages = 1
    nxt = body.get("@odata.nextLink")
    while nxt and pages < max_pages:
        status, body = graph_get(token, nxt)
        if status not in (200, 201) or body is None:
            break
        pages += 1
        nxt = body.get("@odata.nextLink")
    return pages


def _estimate_from_samples(teams_total: int, team_channel_counts: list,
                           channel_page_depths: list,
                           rps_messages: float) -> dict:
    """PURE. Scales a cheap, sampled census (a handful of teams' channel
    LISTINGS, a handful of channels' message-page DEPTH) up to a rough
    tenant-wide wall-clock estimate:
    teams_total x avg_channels_per_sampled_team x avg_pages_per_sampled_channel
    / rps_messages — NOT one team's channel count treated as the whole
    tenant (the bug this replaces). Sample sizes always ride the output so
    the estimate's basis is auditable, never asserted silently. Counts
    only, never bytes — Graph publishes no message/attachment byte size."""
    avg_channels = (sum(team_channel_counts) / len(team_channel_counts)
                    if team_channel_counts else 0.0)
    avg_pages = (sum(channel_page_depths) / len(channel_page_depths)
                if channel_page_depths else 0.0)
    est_seconds = 0.0
    if avg_channels and avg_pages and rps_messages > 0:
        est_seconds = teams_total * avg_channels * avg_pages / rps_messages
    return {
        "teams_total": teams_total,
        "teams_sampled_for_channels": len(team_channel_counts),
        "avg_channels_per_sampled_team": round(avg_channels, 2),
        "channels_sampled_for_pages": len(channel_page_depths),
        "avg_pages_per_sampled_channel": round(avg_pages, 2),
        "rps_messages": rps_messages,
        "estimated_seconds": round(est_seconds, 1),
        "estimate_basis": "sampled",
    }


# ── subcommands (the engine covers create-vm/allow-network/check-azure) ──────

_TEAMS_PLAN_NOTE = (
    "Teams-specific: run probe first — the message-content gate (open / "
    "metered-model-required / protected-api-approval-missing) can turn "
    "this into a client conversation before any VM is billed.")


def cmd_plan(root: Path, args) -> dict:
    """Under --dry-run this is a hand-built minimal dict that never shells
    out to az at all — Teams has no rclone remote and no bucket/export to
    look up before a VM exists, so there is nothing a dry-run preview
    would need az for, and skipping it means a caller piping stdout
    straight into json.loads() gets clean JSON with no DRY-RUN az-command
    line ahead of it (unlike the other *-azure-transfer plans, which still
    print that preview even though they change nothing).

    A real (non-dry-run) plan delegates to transfer_engine.cmd_plan for
    the actual SA region lookup, same as every other source — one source
    of truth for the shared dict shape — and just appends the
    Teams-specific note."""
    if args.dry_run:
        cfg = eng.load_cfg(root, args.slug)
        return {
            "slug": args.slug,
            "vm_name": SPEC.vm_name(args.slug),
            "vm_size": args.vm_size,
            "region": "(unknown — dry-run)",
            "resource_group": args.rg or cfg["resource_group"],
            "storage_account": cfg["storage_account"],
            "container": cfg["container"],
            "dest": f"{cfg['container']}/{args.dest_prefix}",
            "source": SPEC.source_ref(args.tenant_id or ""),
            "sas_expiry_days": args.sas_days,
            "note": _TEAMS_PLAN_NOTE,
        }
    result = eng.cmd_plan(SPEC, root, args)
    result["note"] = result["note"] + " " + _TEAMS_PLAN_NOTE
    return result


def cmd_write_creds(root: Path, args) -> dict:
    """3 stdin lines -> 600 teams.env on the VM. The tenant on line 1 must
    agree with --tenant-id (or, absent that, the VM's teams_tenant_id tag —
    the normal workflow is create-vm --tenant-id once, then write-creds
    with no flag at all, reading the tag) — see _tenant_guard. When not
    dry-run, a laptop TokenBox mint proves the three values actually work
    together before declaring success.

    Stdin is read and validated FIRST, before any VM lookup (the qwilr
    fail-fast convention) — a malformed paste should never cost an az call."""
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
    # single-quoted for the same reason as figma/zoho's env files (a
    # value containing '&' or similar would otherwise break `source`);
    # read_secrets already refused any of the 3 values containing a
    # single quote, so this can never produce a corrupt env file.
    env = (f"TEAMS_TENANT_ID={stdin_tenant}\n"
           f"TEAMS_CLIENT_ID={client_id}\n"
           f"TEAMS_CLIENT_SECRET='{secret}'\n")
    _write_env(vm["public_ip"], TEAMS_ENV, env, args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "secret": "redacted",
                "written_to": f"{vm['name']}:{TEAMS_ENV}"}
    TokenBox(stdin_tenant, client_id, secret).mint()  # smoke test
    return {"ok": True, "secret": "redacted",
            "written_to": f"{vm['name']}:{TEAMS_ENV}"}


# ── probe ────────────────────────────────────────────────────────────────────

_GROUP_PARAMS = {
    "$filter": "resourceProvisioningOptions/Any(x:x eq 'Team')",
    "$select": "id,displayName", "$top": "100"}
_USER_PARAMS = {"$select": "id", "$top": "100"}
_CHAT_PARAMS = {"$top": "1"}


def cmd_probe(root: Path, args) -> dict:
    """Day-one gate for a Teams engagement, BEFORE any billable VM: does
    the app registration's client-credentials mint a token, can it read
    the directory (teams/channels/users), and — the specific Teams stall —
    does the tenant's message-content Graph surface actually answer (open,
    or behind Microsoft's metered-model billing, or behind the protected-
    API approval process)? Laptop-side only: Graph JSON, no Azure call, no
    VM. Emits team/channel/user COUNTS and a sampled wall-clock estimate,
    NEVER bytes — Graph publishes no message or attachment byte sizes.
    """
    eng.load_cfg(root, args.slug)  # onboarding check only; no network
    tenant, client_id, secret = read_secrets(args.dry_run)
    if args.dry_run:
        graph_get(None, "/groups", _GROUP_PARAMS, dry_run=True)
        graph_get(None, "/users", _USER_PARAMS, dry_run=True)
        graph_get(None, "/chats", _CHAT_PARAMS, dry_run=True)
        return {"ok": True, "dry_run": True,
                "note": "no network I/O under --dry-run — printed the "
                        "Graph URLs the real probe would call"}

    box = TokenBox(tenant, client_id, secret)
    token = box.mint()  # proves the app registration + secret actually work

    gstatus, groups = _graph_paginate(token, "/groups", _GROUP_PARAMS)
    if gstatus != 200:
        raise common.HarnessError(
            f"/groups refused ({gstatus}) — the app registration is "
            "missing the Group.Read.All (or Team.ReadBasic.All) "
            "application permission, or an admin never granted consent. "
            "Nothing else can be probed until this is fixed.")
    team_names_sample = [g.get("displayName") for g in groups[:10]]

    ustatus, ubody = graph_get(token, "/users", _USER_PARAMS)
    # sampled COUNT, not a bool (m5, counts discipline) — a refused /users
    # (missing User.Read.All) reads as 0 sampled, same as a genuinely
    # user-less tenant; the top-level ok/message_gate fields are what
    # actually gates the engagement, not this count.
    users_sampled = (len((ubody or {}).get("value", []))
                     if ustatus == 200 else 0)

    channels: list = []
    cstatus = None
    first_team = groups[0] if groups else None
    if first_team is not None:
        cstatus, channels = _graph_paginate(
            token, f"/teams/{first_team['id']}/channels", None)

    message_gate = "no-channel-to-test"
    if first_team is None:
        next_step = ("no team is visible to this app registration — probe "
                     "cannot confirm the message-content gate yet.")
    elif cstatus not in (200, 201):
        next_step = (
            f"the first team's channel listing was refused (status "
            f"{cstatus}) — check the Channel.ReadBasic.All (or "
            "ChannelSettings.Read.All) application permission before "
            "assuming the team simply has no channels.")
    else:
        next_step = ("the first team has no channels to test messages "
                     "against — probe cannot confirm the message-content "
                     "gate yet.")
    first_channel = channels[0] if channels else None
    if first_team is not None and first_channel is not None:
        mstatus, _ = graph_get(
            token,
            f"/teams/{first_team['id']}/channels/{first_channel['id']}"
            "/messages", {"$top": "1"})
        if mstatus == 200:
            message_gate = "open"
            next_step = ("channel messages are readable — transfer can "
                         "proceed.")
        elif mstatus == 402:
            message_gate = "metered-model-required"
            next_step = (
                "Microsoft requires the tenant to link an Azure "
                "subscription for Teams' metered API billing (model A/B) "
                "before this Graph surface answers — the client sets that "
                "up in the Teams admin center, then re-run probe.")
        elif mstatus == 403:
            message_gate = "protected-api-approval-missing"
            next_step = (
                "this tenant has not been approved for Microsoft's "
                "protected Teams messaging APIs "
                "(aka.ms/GraphTeamsProtectedApis) — the client files that "
                "request; approval takes days to weeks, so this is a "
                "client conversation, not a retry.")
        else:
            message_gate = f"unexpected-{mstatus}"
            next_step = (f"unexpected status {mstatus} reading a message "
                        "page — investigate before quoting a timeline.")

    chat_status, _ = graph_get(token, "/chats", _CHAT_PARAMS)
    if chat_status == 403:
        chats = "out-of-scope (no Chat.Read.All)"
    elif chat_status == 200:
        chats = ("WARNING: 1:1/group chats are readable (Chat.Read.All is "
                "granted) — confirm with the client whether chats are "
                "actually in scope before this engagement pulls them")
    else:
        chats = f"unexpected-{chat_status}"

    channels_sampled = len(channels)

    # -- I5: sample a handful of teams' channel LISTINGS (first_team's is
    # already in hand above) and a handful of channels' message-page
    # DEPTH (first_channel's is folded in too), then scale by the REAL
    # team total. Cheap and bounded: at most TEAM_SAMPLE_SIZE extra
    # channel listings and CHANNEL_SAMPLE_SIZE page-depth walks (each
    # capped at PAGE_SAMPLE_CAP pages), never a corpus walk. --
    TEAM_SAMPLE_SIZE = 5
    CHANNEL_SAMPLE_SIZE = 5
    team_channel_counts: list = []
    sampled_channel_refs: list = []
    for g in groups[:TEAM_SAMPLE_SIZE]:
        if g is first_team:
            team_channels = channels
        else:
            gcstatus, team_channels = _graph_paginate(
                token, f"/teams/{g['id']}/channels", None)
            if gcstatus not in (200, 201):
                continue
        team_channel_counts.append(len(team_channels))
        for ch in team_channels:
            sampled_channel_refs.append((g["id"], ch["id"]))

    channel_page_depths = [
        depth for depth in (
            _sample_message_pages(token, gid, cid) for gid, cid in
            sampled_channel_refs[:CHANNEL_SAMPLE_SIZE])
        if depth is not None]

    estimate = _estimate_from_samples(
        len(groups), team_channel_counts, channel_page_depths,
        puller.DEFAULT_RPS_MESSAGES)

    return {
        "ok": message_gate == "open",
        "teams_sampled": len(groups),
        "team_names_sample": team_names_sample,
        "users_sampled": users_sampled,
        "channels_sampled": channels_sampled,
        "message_gate": message_gate,
        "next_step": next_step,
        "chats": chats,
        "estimate": estimate,
        "note": ("counts and a sampled wall-clock estimate only, NEVER "
                 "bytes — Graph publishes no message or attachment byte "
                 "sizes. Only message_gate == 'open' means the skill can "
                 "proceed straight to create-vm; the other two gates are a "
                 "client conversation first."),
    }


# ── transfer / status / verify / teardown / discover ─────────────────────────

def cmd_write_dest(root: Path, args) -> dict:
    """racwl SAS -> rclone [azure] section (so check-azure has something to
    test against) AND DEST_ENV, both on-VM. DEST_URL is deliberately the
    BARE container URL — unlike figma/zoho, teams_vm_pull.py appends
    DEST_PREFIX itself (controller ruling, 2026-08-28), so DEST_PREFIX
    rides the env file as a real, consumed setting instead of being baked
    into the URL."""
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    prefix = _dest_prefix(vm, args)
    sas, expiry = eng.mint_container_sas(cfg, args.sas_days, args.dry_run)
    base = _container_url(cfg)
    eng.write_conf_section(vm["public_ip"], "azure",
                           f"[azure]\ntype = azureblob\n"
                           f"sas_url = {base}?{sas}\n",
                           dry_run=args.dry_run)
    # values are SINGLE-QUOTED: the SAS contains '&', and sourcing an
    # unquoted VAR=a&b line backgrounds the assignment at the '&' — the
    # var lands empty and the puller hits Azure with no SAS at all (401,
    # found live on checkmate). az SAS/URLs never contain single quotes.
    env = (f"DEST_URL='{base}'\n"
           f"DEST_SAS='{sas}'\n"
           f"DEST_PREFIX='{prefix}'\n")
    _write_env(vm["public_ip"], DEST_ENV, env, args.dry_run)
    return {"remote": "azure", "container": cfg["container"],
            "dest_prefix": prefix, "sas_expiry": expiry,
            "written_to": [f"{vm['name']}:{eng.RCLONE_CONF}",
                           f"{vm['name']}:{DEST_ENV}"]}


def cmd_transfer(root: Path, args) -> dict:
    """Fresh puller push -> tmux window 'teams', sourcing both env files.
    teams_vm_pull.py is fully env-driven (no argv at all — it is launched
    with zero arguments), so an optional pilot (--rps-messages /
    --limit-teams) rides `export` lines ahead of `set +a`, not CLI flags."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    if eng._tmux_alive(ip, args.dry_run):
        return {"ok": False, "cause": "already-running",
                "hint": "tmux session 'transfer' is alive — use status."}
    _push_puller(ip, args.dry_run)
    env_extra = ""
    if args.rps_messages:
        env_extra += f"export RPS_MESSAGES={args.rps_messages}; "
    if args.limit_teams:
        env_extra += f"export LIMIT_TEAMS={args.limit_teams}; "
    inner = (f"set -a; . {TEAMS_ENV}; . {DEST_ENV}; set +a; "
             f"{env_extra}python3 {XFER_DIR}/teams_vm_pull.py "
             f"2>&1 | tee -a {LOG_FILE}")
    eng.run_ssh(ip, f'tmux new-session -d -s {eng.TMUX_SESSION} -n teams '
                    f'"{inner}"',
                dry_run=args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True,
                "rps_messages": args.rps_messages or None,
                "limit_teams": args.limit_teams or None}
    eng.run_ssh(ip, "sleep 5", check=False)
    alive = eng._tmux_alive(ip, False)
    tail = eng.run_ssh(ip, f"tail -3 {LOG_FILE} 2>/dev/null", check=False)
    return {"ok": alive, "session": eng.TMUX_SESSION, "window": "teams",
            "rps_messages": args.rps_messages or None,
            "limit_teams": args.limit_teams or None,
            "log_tail": (tail.stdout or "").strip().splitlines(),
            "note": ("re-running transfer is safe — per-unit .cdp-complete "
                     "markers and .cdp-cursor.json resume, and uploads are "
                     "--overwrite=false" if alive else None),
            "hint": None if alive else
            f"the puller died immediately — tail {LOG_FILE} on the VM "
            "(bad env files? expired secret? tenant not approved for the "
            "protected Teams messaging APIs?)"}


_STATUS_PY = r"""
import json, os, shutil
base = os.path.expanduser("~/xfer-teams")
dest = os.path.join(base, "dest")
out = {}
try:
    out["progress"] = json.load(open(os.path.join(dest, "progress.json")))
except (OSError, ValueError):
    out["progress"] = None
try:
    m = json.load(open(os.path.join(dest, "_meta", "manifest.json")))
    out["manifest"] = dict(unit_count=m.get("unit_count"),
                           total_staged_bytes=m.get("total_staged_bytes"),
                           api_calls=m.get("api_calls"),
                           api_sleeps=m.get("api_sleeps"),
                           failed_units=m.get("failed_units"),
                           skipped_units=m.get("skipped_units"),
                           hosted_errors=m.get("hosted_errors"),
                           finished_utc=m.get("finished_utc"))
except (OSError, ValueError):
    out["manifest"] = None
try:
    lines = open(os.path.join(base, "pull-teams.log")).read().splitlines()
    out["log_tail"] = lines[-5:]
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
    manifest = detail.get("manifest")
    hint = None
    if not alive:
        hint = ("a pass finished — run verify" if manifest else
                "not running and no manifest — it never finished a pass; "
                f"tail {LOG_FILE} on the VM, then re-run transfer")
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "transfer_running": alive,
            "staged_bytes_human": common.human_bytes(
                (manifest or {}).get("total_staged_bytes") or 0),
            "hint": hint,
            "note": "a nonzero api_sleeps in the manifest is normal metering "
                    "against Graph's per-app throttle, not a hang",
            **detail}


def cmd_verify(root: Path, args) -> dict:
    """Laptop-side; the VM may already be torn down. Lists the dest prefix
    and compares against the uploaded _meta/manifest.json — see
    compare_manifest_to_blobs for exactly what is (and is not) asserted.
    Takes NO Graph credentials, and mints only the READ (rl) account SAS —
    never the racwl write SAS."""
    cfg = eng.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    prefix = (args.dest_prefix or SPEC.default_dest_prefix).strip("/")
    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl -- the READ path
        blobs = azure_list_blobs(cfg, sas, prefix, args.dry_run)
        if args.dry_run:
            print(f"DRY-RUN: GET {_container_url(cfg)}/{prefix}/_meta/"
                  "manifest.json?<sas-redacted>")
            return {"ok": True, "dry_run": True, "prefix": prefix}
        mname = f"{prefix}/_meta/manifest.json"
        if mname not in blobs:
            return {"ok": False, "cause": "no-manifest", "prefix": prefix,
                    "blobs_under_prefix": len(blobs),
                    "hint": "no _meta/manifest.json under the prefix — the "
                            "pull never finished a pass. Run status/"
                            "transfer on the VM first."}
        manifest = json.loads(azure_get(
            f"{_container_url(cfg)}/{urllib.parse.quote(mname, safe='/')}"
            f"?{sas}"))
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)
    result = compare_manifest_to_blobs(manifest, blobs, prefix)
    result.update({"prefix": prefix, "blobs_under_prefix": len(blobs),
                   "finished_utc": manifest.get("finished_utc")})
    if result["ok"]:
        result["note"] = (
            "certifies staged->container completeness against the "
            "uploaded manifest; Graph->staged completeness is the "
            "puller's exit code + failed_units (clean here). NO "
            "source-size claim is made — Graph publishes no message or "
            "attachment byte size, so the manifest's total_staged_bytes "
            "is the first honest byte number. Remember the sharepoint "
            "boundary when reporting: a channel's file tab and any "
            "message attachment lives in SharePoint, not here. After "
            f'this, pin the teams service in expected-data-sizes.json '
            f'with "prefix": "{prefix}", then let size-company pick it up.')
    return result


def cmd_teardown(root: Path, args) -> dict:
    result = eng.cmd_teardown(SPEC, root, args)
    if result.get("ok") and "reminders" in result:
        result["reminders"].append(
            "Tell the client to rotate (or delete) the Entra app "
            "registration's client secret now that the pull is torn down "
            "and verified — it was handed to us in chat as one of the "
            "three write-creds values, and rotating the client secret is "
            "the clean end of this engagement.")
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
                "hint": "no transfer VM — run probe (needs only the 3 "
                        "stdin credentials + --tenant-id), then setup"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base,
                "hint": "VM exists but has no public IP (deallocated?)"}
    checks = [f"test -f {TEAMS_ENV} && echo teams-env",
              f"test -f {DEST_ENV} && echo dest-env",
              f"test -f {DEST_DIR}/_meta/manifest.json && echo manifest",
              f"tmux has-session -t {eng.TMUX_SESSION} 2>/dev/null "
              "&& echo tmux-alive"]
    probe = eng.run_ssh(vm["public_ip"], "; ".join(checks), check=False)
    if probe.returncode != 0 and not (probe.stdout or "").strip():
        return {"phase": "vm-unreachable", **base,
                "hint": "ssh failed — VM booting, or your key changed. "
                        f"Try: ssh {eng.ADMIN_USER}@{vm['public_ip']}"}
    out = probe.stdout or ""
    if "tmux-alive" in out:
        return {"phase": "transfer-running", **base,
                "hint": "use status for progress"}
    if "manifest" in out:
        return {"phase": "transfer-stopped", **base,
                "hint": "a pass finished — run status, then verify "
                        "(laptop-side); failed units mean re-run transfer"}
    if not ("teams-env" in out and "dest-env" in out):
        return {"phase": "mid-setup", **base,
                "hint": "VM up but creds/dest incomplete — resume setup at "
                        "the missing write-dest / write-creds step."}
    return {"phase": "setup-complete", **base,
            "hint": "creds + dest in place — run transfer --limit-teams 1 "
                    "(pilot) first"}


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
    p.add_argument("--tenant-id", dest="tenant_id", default=None,
                   help="the client's Entra ID Directory (tenant) ID (a "
                        "GUID) — required for plan/probe; for write-creds "
                        f"it falls back to the {SPEC.loc_tag} VM tag set at "
                        "create-vm, so the normal workflow only passes it "
                        "once.")
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None,
                   help="create-vm: OS disk GB (default: the Spec's 64 — "
                        "staging is JSONL + inline images, not video/DB "
                        "dumps)")
    p.add_argument("--dest-prefix", default=None,
                   help=f"prefix inside <slug>-raw (default "
                        f"{SPEC.default_dest_prefix})")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--rps-messages", dest="rps_messages", type=float,
                   default=0.0,
                   help="transfer: override the puller's messages-family "
                        f"pace (default {puller.DEFAULT_RPS_MESSAGES}/s — "
                        "conservative, Teams messaging is Graph's slow "
                        "lane)")
    p.add_argument("--limit-teams", dest="limit_teams", type=int, default=0,
                   help="transfer: pilot — only the first N teams")
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-transfer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.command in ("plan", "create-vm", "probe") \
            and args.tenant_id is None:
        p.error(f"{args.command} requires --tenant-id")
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    engine_cmds = {"create-vm": eng.cmd_create_vm,
                   "allow-network": eng.cmd_allow_network,
                   "check-azure": eng.cmd_check_azure}
    own_cmds = {"discover": cmd_discover, "plan": cmd_plan,
                "write-dest": cmd_write_dest, "write-creds": cmd_write_creds,
                "probe": cmd_probe, "transfer": cmd_transfer,
                "status": cmd_status, "verify": cmd_verify,
                "teardown": cmd_teardown}
    try:
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
