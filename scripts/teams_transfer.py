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

Verify (Task 5) runs on the LAPTOP, same as figma/zoho: the completeness
authority is the manifest.json the pull uploads to
<dest-prefix>/_meta/manifest.json, so no Graph access is needed at verify
time — just a blob listing over phases.ip_rule_ensure + an rl account SAS.

This task (4) builds the Spec, the 3-line credential plumbing (read_secrets,
cmd_write_creds with its tenant guard) and the laptop-side probe gate.
transfer/status/verify/teardown are Task 5.
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
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


# ── subcommands (the engine covers create-vm/allow-network/check-azure) ──────

def cmd_plan(root: Path, args) -> dict:
    """Same shape as transfer_engine.cmd_plan, but Teams has no rclone
    remote and no bucket/export to look up before a VM exists — so under
    --dry-run this never shells out to az at all (unlike the other
    *-azure-transfer plans, which still print their DRY-RUN az-command
    preview even though they change nothing): a caller piping stdout
    straight into json.loads() gets clean JSON, no az-command noise ahead
    of it. A real (non-dry-run) plan still resolves the SA's actual
    region, same as every other source."""
    cfg = eng.load_cfg(root, args.slug)
    if args.dry_run:
        region = "(unknown — dry-run)"
    else:
        eng.set_subscription(cfg, args.dry_run)
        region = eng.sa_region(cfg, args.dry_run)
    return {
        "slug": args.slug,
        "vm_name": SPEC.vm_name(args.slug),
        "vm_size": args.vm_size,
        "region": region,
        "resource_group": args.rg or cfg["resource_group"],
        "storage_account": cfg["storage_account"],
        "container": cfg["container"],
        "dest": f"{cfg['container']}/{args.dest_prefix}",
        "source": SPEC.source_ref(args.tenant_id or ""),
        "sas_expiry_days": args.sas_days,
        "note": ("VM billing starts at create and runs until teardown; "
                 "public IP is static Standard SKU (never deallocate). "
                 "Same-region reminder: the SA firewall needs a vnet-rule "
                 "for this VM's subnet — IP rules alone never match "
                 "same-region traffic. Teams-specific: run probe first — "
                 "the message-content gate (open / metered-model-required "
                 "/ protected-api-approval-missing) can turn this into a "
                 "client conversation before any VM is billed."),
    }


def cmd_write_creds(root: Path, args) -> dict:
    """3 stdin lines -> 600 teams.env on the VM. The tenant on line 1 must
    agree with --tenant-id (or, absent that, the VM's teams_tenant_id tag)
    — see _tenant_guard. When not dry-run, a laptop TokenBox mint proves
    the three values actually work together before declaring success."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    stdin_tenant, client_id, secret = read_secrets(args.dry_run)
    stdin_tenant = validate_tenant_id(stdin_tenant)
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

    ustatus, _users = graph_get(token, "/users", _USER_PARAMS)
    user_read_all = ustatus == 200

    channels: list = []
    first_team = groups[0] if groups else None
    if first_team is not None:
        _cstatus, channels = _graph_paginate(
            token, f"/teams/{first_team['id']}/channels", None)

    message_gate = "no-channel-to-test"
    next_step = ("no team has a readable channel to test messages against "
                 "— probe cannot confirm the message-content gate yet.")
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
    pages_per_channel_sampled = 1 if first_channel is not None else 0
    est_seconds = 0.0
    if channels_sampled and pages_per_channel_sampled:
        est_seconds = (channels_sampled * pages_per_channel_sampled
                      / puller.DEFAULT_RPS_MESSAGES)

    return {
        "ok": message_gate == "open",
        "teams_sampled": len(groups),
        "team_names_sample": team_names_sample,
        "user_read_all": user_read_all,
        "channels_sampled": channels_sampled,
        "message_gate": message_gate,
        "next_step": next_step,
        "chats": chats,
        "estimate": {
            "channels_sampled": channels_sampled,
            "pages_per_channel_sampled": pages_per_channel_sampled,
            "rps_messages": puller.DEFAULT_RPS_MESSAGES,
            "estimated_seconds": round(est_seconds, 1),
            "estimate_basis": "sampled",
        },
        "note": ("counts and a sampled wall-clock estimate only, NEVER "
                 "bytes — Graph publishes no message or attachment byte "
                 "sizes. Only message_gate == 'open' means the skill can "
                 "proceed straight to create-vm; the other two gates are a "
                 "client conversation first."),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["plan", "write-creds", "probe"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--tenant-id", dest="tenant_id", default=None,
                   help="the client's Entra ID Directory (tenant) ID (a "
                        "GUID) — required for plan/write-creds/probe; "
                        f"later read from the {SPEC.loc_tag} VM tag once a "
                        "VM exists.")
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
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.command in ("plan", "write-creds", "probe") \
            and args.tenant_id is None:
        p.error(f"{args.command} requires --tenant-id")
    if args.dest_prefix is None and args.command == "plan":
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    own_cmds = {"plan": cmd_plan, "write-creds": cmd_write_creds,
                "probe": cmd_probe}
    try:
        if args.tenant_id is not None:
            args.tenant_id = validate_tenant_id(args.tenant_id)
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
