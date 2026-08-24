#!/usr/bin/env python3
"""GitHub -> Azure transfer CLI (VM mirror-clone + azcopy; VM family).

Reuses transfer_engine.py's VM lifecycle (create-vm / allow-network /
check-azure / teardown run the engine functions verbatim) but owns the
copy layer: scripts/github_vm_pull.py is pushed to the VM and runs the
whole pull there in tmux — mirror clones, wikis, LFS, the 4 issue/PR
JSONL exports — then azcopies the staged tree to
<slug>-raw/<dest-prefix>/. Nothing downloads to this machine; the
laptop-side subcommands are control-plane only (probe is GitHub API
JSON, verify is a blob listing).

Subcommands: discover / plan / create-vm / allow-network / write-dest /
check-azure / write-token / probe / transfer / status / verify /
teardown. Source location flag: --login (required for plan/create-vm/
probe; later read from VM tags). See
.claude/skills/github-azure-transfer/SKILL.md.

Secrets: the fine-grained PAT arrives as 1 line on stdin and travels only
over ssh stdin into a 600 env file on the VM — never argv, tags, logs or
laptop files (probe holds it in process memory only). The dest SAS goes
the same way. Both die with the VM at teardown.

Verify runs on the LAPTOP (the VM is normally gone by then): the
completeness authority is the manifest.json the pull uploaded, so no
GitHub access is needed — just a blob listing, which from this machine's
external IP uses phases.ip_rule_ensure + an rl account SAS (the zoom/
qwilr laptop path; allow-network stays VM-only).
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
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
    source_name="github",
    vm_prefix="xfer-gh-",
    purpose="github-transfer",
    loc_tag="gh_login",
    loc_argname="login",
    loc_required=True,
    default_dest_prefix="github-export",
    authorize_target="",  # no OAuth flow — fine-grained PAT on stdin
    remote_type="",       # no rclone source remote; git+API is the source
    extra_cli_opts=[{"flag": "--owner-type", "argname": "owner_type",
                     "tag": "gh_owner_type", "conf_key": "",
                     "help": "org (default) or user"}],
    default_os_disk_gb=512,  # staging holds the whole corpus before azcopy
)

PULLER_PY = Path(__file__).resolve().parent / "github_vm_pull.py"
XFER_DIR = f"/home/{eng.ADMIN_USER}/xfer-gh"
DEST_DIR = f"{XFER_DIR}/dest"
LOG_FILE = f"{XFER_DIR}/pull.log"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/xfer"
GH_ENV = f"{ENV_DIR}/github.env"
DEST_ENV = f"{ENV_DIR}/dest.env"
API = "https://api.github.com"
X_MS_VERSION = "2021-08-06"


# ── VM-side plumbing ─────────────────────────────────────────────────────────

def _write_env(ip: str, path: str, content: str, dry_run: bool) -> None:
    """600 env file on the VM; content rides ssh stdin only."""
    eng.run_ssh(ip, f"umask 077 && mkdir -p {ENV_DIR} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _push_puller(ip: str, dry_run: bool) -> None:
    """Fresh copy every transfer so harness upgrades propagate."""
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > "
                    f"{XFER_DIR}/github_vm_pull.py",
                stdin_data=PULLER_PY.read_text(), dry_run=dry_run)


def _owner_type(vm: dict, args) -> str:
    return (getattr(args, "owner_type", None)
            or vm["tags"].get("gh_owner_type") or "org")


def _dest_prefix(vm: dict, args) -> str:
    """Prefix without the login requirement — write-dest needs no source
    location (same shape as s3_transfer's helper)."""
    return (getattr(args, "dest_prefix", None)
            or vm["tags"].get("dest_prefix") or SPEC.default_dest_prefix)


# ── laptop-side GitHub client (probe only; PAT stays in process memory) ──────

def read_token(dry_run: bool) -> str:
    if dry_run:
        return "<token>"
    if sys.stdin.isatty():
        raise common.HarnessError(
            "no PAT on stdin -- pipe it: probe <slug> --login <org> "
            "<<'EOF'\n<the fine-grained PAT>\nEOF")
    token = sys.stdin.read().strip()
    if not token or "\n" in token:
        raise common.HarnessError(
            "stdin must be exactly 1 line: the fine-grained PAT")
    return token


def gh_get(token: str, path: str, dry_run: bool):
    """One GET -> (json, headers). The probe-side slice of the puller's
    failure taxonomy, as HarnessError instead of SystemExit: 401 fatal,
    403 with the rate limit intact fatal (scope/approval), 404 raised to
    the caller. Probe never needs the sleep-through-reset leg — it makes
    a handful of calls against a 5k/h budget."""
    if dry_run:
        print(f"DRY-RUN: GET {API}{path} "
              "(Authorization: Bearer <token-redacted>)")
        return None, {}
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cdp-harness-github-transfer/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise common.HarnessError(
                "HTTP 401 -- the PAT is invalid or expired")
        if e.code in (403, 429):
            if e.headers.get("X-RateLimit-Remaining") == "0":
                raise common.HarnessError(
                    "GitHub rate limit exhausted -- wait for the reset "
                    "(probe needs only a handful of calls; something else "
                    "is burning this token's budget)")
            raise common.HarnessError(
                f"HTTP {e.code} with the rate limit intact on {path} -- "
                "an auth/scope problem, not throttling: the PAT is likely "
                "missing Contents/Issues/Pull requests: read, or the org "
                "has not approved it")
        raise  # 404 and the rest: caller decides
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise common.HarnessError(f"GitHub API unreachable: {e}")


def gh_paginate(token: str, path: str, dry_run: bool) -> list:
    """Follow Link headers; probe uses this once, for the repo listing."""
    if dry_run:
        gh_get(token, path + "&per_page=100", dry_run)
        return []
    items: list = []
    url = API + path + "&per_page=100"
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cdp-harness-github-transfer/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                items.extend(json.loads(resp.read()))
                link = resp.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            raise common.HarnessError(
                f"repo listing failed: HTTP {e.code} on {url.split('?')[0]}")
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part[part.find("<") + 1:part.find(">")]
                break
    return items


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
    import time
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
                time.sleep(15 * (attempt + 1))  # propagation, not a bad SAS
                continue
            if e.code >= 500:
                time.sleep(1 + attempt)
                continue
            raise common.HarnessError(f"blob GET failed: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(1 + attempt)
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
    uploaded manifest (GitHub -> staged completeness is the puller's own
    exit code + failed_repos — verify surfaces that list verbatim).

    Per successful leg: the .cdp-complete marker blob must exist, and the
    per-repo container byte sum must be >= the staged bytes (equality in
    the clean path; MORE than staged happens legitimately when a re-clone
    after --refresh/partial produced different packfiles and create-only
    kept the old ones — reported as stale_extra, informational). LESS
    than staged = a partial upload = failure. No source-size claim is
    made: git packing differs from the API's diskUsage and probe's
    estimate excludes LFS."""
    results = manifest.get("results", [])
    missing_markers: list[str] = []
    short_uploads: list[dict] = []
    stale_extra: list[str] = []

    def rollup(sub: str) -> int:
        return sum(b["size"] for n, b in blobs.items()
                   if n.startswith(sub))

    for r in results:
        name = r["repo"]
        legs = []
        if r.get("clone") in ("ok", "skipped"):
            legs.append((f"{prefix}/repos/{name}.git/", r.get("bytes", 0)))
        if r.get("wiki") in ("ok", "skipped"):
            legs.append((f"{prefix}/wikis/{name}.wiki.git/",
                         r.get("wiki_bytes", 0)))
        if r.get("json") in ("ok", "skipped"):
            legs.append((f"{prefix}/json/{name}/", 0))
        for sub, staged in legs:
            if f"{sub}.cdp-complete" not in blobs:
                missing_markers.append(sub)
                continue
            landed = rollup(sub)
            if staged and landed < staged:
                short_uploads.append({"prefix": sub, "staged": staged,
                                      "landed": landed})
            elif staged and landed > staged:
                stale_extra.append(sub)

    failed = manifest.get("failed_repos", [])
    ok = not failed and not missing_markers and not short_uploads
    return {
        "ok": ok,
        "repo_count": manifest.get("repo_count"),
        "total_clone_bytes": manifest.get("total_clone_bytes"),
        "failed_repos": failed,
        "missing_markers": missing_markers,
        "short_uploads": short_uploads,
        "stale_extra": stale_extra,
        "hint": None if ok else
        "failed_repos / missing markers / short uploads: re-run transfer "
        "(resume markers skip completed repos, azcopy --overwrite=false "
        "skips landed blobs), then re-verify. stale_extra alone is "
        "informational: create-only kept an older clone's packfiles.",
    }


# ── subcommands (the engine covers plan/create-vm/allow-network/check-azure) ─

def cmd_write_dest(root: Path, args) -> dict:
    """racwl SAS -> rclone [azure] section AND azcopy dest.env, both on-VM."""
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
    # var lands empty and azcopy hits Azure with no SAS at all (401,
    # found live on checkmate). az SAS/URLs never contain single quotes.
    env = (f"AZURE_DEST_URL='{base}/{prefix}'\n"
           f"AZURE_DEST_SAS='{sas}'\n"
           f"AZURE_DEST_CONTAINER='{cfg['container']}'\n"
           f"AZURE_DEST_PREFIX='{prefix}'\n")
    _write_env(vm["public_ip"], DEST_ENV, env, args.dry_run)
    return {"remote": "azure", "container": cfg["container"],
            "dest_prefix": prefix, "sas_expiry": expiry,
            "written_to": [f"{vm['name']}:{eng.RCLONE_CONF}",
                           f"{vm['name']}:{DEST_ENV}"]}


def cmd_write_token(root: Path, args) -> dict:
    """1 stdin line (the fine-grained PAT) -> 600 github.env on the VM,
    then a /rate_limit smoke test with the sourced token."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    # unlike probe, this always reads + validates stdin (the s3
    # write-s3-creds semantics): the 1-line contract is checked even in
    # dry-run, with the value replaced before anything is written
    lines = [ln.strip() for ln in sys.stdin.read().splitlines()
             if ln.strip()]
    if len(lines) != 1:
        raise common.HarnessError(
            "stdin must be exactly 1 line: the fine-grained PAT")
    token = "<token>" if args.dry_run else lines[0]
    # single-quoted for the same reason as dest.env (see cmd_write_dest);
    # GitHub PATs are [A-Za-z0-9_] — never contain single quotes
    _write_env(vm["public_ip"], GH_ENV, f"GITHUB_TOKEN='{token}'\n",
               args.dry_run)
    # status code via -D -/sed, not -w '%{http_code}': the dry-run echo of
    # this command must stay brace-free (the test harness finds the JSON
    # tail with stdout.index("{"))
    smoke = eng.run_ssh(
        vm["public_ip"],
        f"set -a; . {GH_ENV}; set +a; "
        "curl -s -o /dev/null -D - "
        "-H \"Authorization: Bearer $GITHUB_TOKEN\" "
        "-H 'X-GitHub-Api-Version: 2022-11-28' "
        f"{API}/rate_limit "
        "| sed -n 's,^HTTP[^ ]* \\([0-9]*\\).*,\\1,p' | tail -1",
        dry_run=args.dry_run, check=False, timeout=60)
    code = (smoke.stdout or "").strip()
    if args.dry_run:
        return {"ok": True, "dry_run": True,
                "written_to": f"{vm['name']}:{GH_ENV}"}
    if code == "200":
        return {"ok": True, "written_to": f"{vm['name']}:{GH_ENV}"}
    return {"ok": False, "stage": "token-smoke-test", "http_code": code,
            "hint": "401 = token invalid/expired; anything else = "
                    "network/API trouble from the VM. The token is "
                    "written either way — fix and re-run write-token "
                    "to replace it."}


def cmd_probe(root: Path, args) -> dict:
    """Day-one gate, BEFORE any billable resource: does the PAT work, can
    it see the org, what's in it, is LFS in play? Laptop-side, GitHub API
    JSON only — no corpus data, no Azure access, no VM needed. The PAT is
    read from stdin into process memory and discarded."""
    eng.load_cfg(root, args.slug)  # onboarding check only
    if args.login is None:
        raise common.HarnessError("probe requires --login (no VM tags "
                                  "exist yet this early)")
    token = read_token(args.dry_run)
    owner_type = args.owner_type or "org"
    notes: list[str] = []

    gh_get(token, "/rate_limit", args.dry_run)  # 401 → token diagnosis
    seg = "orgs" if owner_type == "org" else "users"
    try:
        gh_get(token, f"/{seg}/{args.login}", args.dry_run)
    except urllib.error.HTTPError as e:
        return {"ok": False, "stage": "owner-visibility",
                "http_code": e.code, "login": args.login,
                "hint": f"the PAT cannot see {owner_type} "
                        f"'{args.login}' — wrong login/owner-type, OR the "
                        "org has not yet approved the fine-grained PAT "
                        "(org Settings → Third-party Access → Personal "
                        "access tokens → pending requests). A client "
                        "conversation, not a retry."}

    repos = gh_paginate(
        token, f"/{seg}/{args.login}/repos?type=all", args.dry_run)
    if args.dry_run:
        print(f"DRY-RUN: GET {API}/repos/{args.login}/<name>/contents/"
              ".gitattributes (LFS check, first "
              f"{args.lfs_check_limit} repos)")
        return {"ok": True, "dry_run": True, "login": args.login}

    # REST `size` is KB and EXCLUDES LFS objects and our JSON exports —
    # a floor for disk/timeline planning, never a quote
    est_bytes = sum((r.get("size") or 0) * 1024 for r in repos)
    lfs_repos: list[str] = []
    checked = 0
    for r in repos[: args.lfs_check_limit]:
        checked += 1
        try:
            data, _ = gh_get(
                token,
                f"/repos/{args.login}/{r['name']}/contents/.gitattributes",
                False)
            body = base64.b64decode(data.get("content") or "")
            if b"filter=lfs" in body:
                lfs_repos.append(r["name"])
        except (urllib.error.HTTPError, common.HarnessError, ValueError,
                KeyError, TypeError):
            continue  # no .gitattributes / unreadable — never fails probe
    if lfs_repos:
        notes.append(
            f"{len(lfs_repos)} repo(s) declare filter=lfs in a root "
            ".gitattributes — the pull fetches their LFS objects "
            "automatically, and the size estimate above does NOT include "
            "them (Org → Billing → Git LFS shows usage).")
    if checked < len(repos):
        notes.append(f"LFS check covered the first {checked} of "
                     f"{len(repos)} repos (--lfs-check-limit); root "
                     ".gitattributes only — the puller's own detection "
                     "runs on every clone regardless.")
    notes.append("estimated_clone_bytes is the API's per-repo size sum — "
                 "excludes LFS objects and the JSON exports; a floor, "
                 "not a quote.")
    return {"ok": True, "login": args.login, "owner_type": owner_type,
            "repo_count": len(repos),
            "private_repos": sum(1 for r in repos if r.get("private")),
            "archived_repos": sum(1 for r in repos if r.get("archived")),
            "forks": sum(1 for r in repos if r.get("fork")),
            "estimated_clone_bytes": est_bytes,
            "estimated_clone_human": common.human_bytes(est_bytes),
            "lfs_repos_detected": lfs_repos[:20],
            "lfs_checked": checked, "notes": notes}


def cmd_transfer(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    if eng._tmux_alive(ip, args.dry_run):
        return {"ok": False, "cause": "already-running",
                "hint": "tmux session 'transfer' is alive — use status."}
    login, _ = eng.loc_and_prefix(SPEC, vm, args)
    owner_type = _owner_type(vm, args)
    _push_puller(ip, args.dry_run)
    flags = ""
    if args.limit:
        flags += f" --limit {args.limit}"
    if args.only:
        flags += f" --only {args.only}"
    if args.refresh:
        flags += " --refresh"
    if args.skip_upload:
        flags += " --skip-upload"
    inner = (f"set -a; . {GH_ENV}; . {DEST_ENV}; set +a; "
             f"python3 {XFER_DIR}/github_vm_pull.py "
             f"--login {login} --owner-type {owner_type} "
             f"--dest {DEST_DIR}{flags} >> {LOG_FILE} 2>&1")
    launch = (f"tmux new-session -d -s {eng.TMUX_SESSION} -n pull "
              f"\"bash -c '{inner}'\"")
    eng.run_ssh(ip, launch, dry_run=args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "login": login}
    eng.run_ssh(ip, "sleep 5", check=False)
    alive = eng._tmux_alive(ip, False)
    tail = eng.run_ssh(ip, f"tail -3 {LOG_FILE} 2>/dev/null", check=False)
    return {"ok": alive, "session": eng.TMUX_SESSION, "login": login,
            "owner_type": owner_type, "pilot_limit": args.limit or None,
            "log_tail": (tail.stdout or "").strip().splitlines(),
            "note": ("re-running transfer is safe — per-repo .cdp-complete "
                     "markers skip finished repos and azcopy "
                     "--overwrite=false skips landed blobs"
                     if alive else None),
            "hint": None if alive else
            f"the puller died immediately — tail {LOG_FILE} on the VM "
            "(bad env files? whoami_check failure?)"}


_STATUS_PY = r"""
import json, os, shutil
base = os.path.expanduser("~/xfer-gh")
dest = os.path.join(base, "dest")
out = {}
try:
    out["progress"] = json.load(open(os.path.join(dest, "progress.json")))
except (OSError, ValueError):
    out["progress"] = None
try:
    m = json.load(open(os.path.join(dest, "manifest.json")))
    out["manifest"] = dict(repo_count=m.get("repo_count"),
                           total_clone_bytes=m.get("total_clone_bytes"),
                           failed_repos=m.get("failed_repos"),
                           finished_utc=m.get("finished_utc"))
except (OSError, ValueError):
    out["manifest"] = None
try:
    lines = open(os.path.join(base, "pull.log")).read().splitlines()
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
    manifest = detail.get("manifest") or {}
    staged = manifest.get("total_clone_bytes")
    hint = None
    if not alive:
        hint = ("pull finished a pass — run verify (laptop-side)"
                if detail.get("manifest") else
                "pull is not running and no manifest exists — it never "
                f"finished a pass; tail {LOG_FILE} on the VM, then re-run "
                "transfer")
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "transfer_running": alive,
            "staged_bytes_human": common.human_bytes(staged)
            if staged else None, "hint": hint, **detail}


def cmd_verify(root: Path, args) -> dict:
    """Laptop-side; the VM may already be torn down. Lists the dest prefix
    and compares against the uploaded manifest.json — see
    compare_manifest_to_blobs for exactly what is (and is not) asserted."""
    cfg = eng.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    prefix = args.dest_prefix or SPEC.default_dest_prefix
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
                   "login": manifest.get("login"),
                   "finished_utc": manifest.get("finished_utc")})
    if result["ok"]:
        result["note"] = (
            "certifies staged->container completeness against the "
            "uploaded manifest; GitHub->staged completeness is the "
            "puller's exit code + failed_repos (clean here). After this, "
            'pin {"prefix": "github-export"} on the github service in '
            "expected-data-sizes.json and let size-company pick it up.")
    return result


def cmd_teardown(root: Path, args) -> dict:
    result = eng.cmd_teardown(SPEC, root, args)
    if result.get("ok") and "reminders" in result:
        result["reminders"][-1] = (
            "Tell the client they can revoke the fine-grained PAT now "
            "(Settings → Developer settings → Fine-grained tokens) — "
            "revocation on their side is the clean end of the engagement")
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
                "hint": "no transfer VM — run probe (needs only the PAT), "
                        "then setup"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base,
                "hint": "VM exists but has no public IP (deallocated?)"}
    probe = eng.run_ssh(
        vm["public_ip"],
        f"test -f {GH_ENV} && echo gh-env; "
        f"test -f {DEST_ENV} && echo dest-env; "
        f"test -f {DEST_DIR}/manifest.json && echo manifest; "
        f"tmux has-session -t {eng.TMUX_SESSION} 2>/dev/null "
        "&& echo tmux-alive", check=False)
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
                        "(laptop-side); failed repos mean re-run transfer"}
    if not ("gh-env" in out and "dest-env" in out):
        return {"phase": "mid-setup", **base,
                "hint": "VM up but creds/dest incomplete — resume setup at "
                        "the missing write-dest / write-token step."}
    return {"phase": "setup-complete", **base,
            "hint": "creds + dest in place — run transfer --limit 2 "
                    "(pilot) first"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[
        "discover", "plan", "create-vm", "allow-network", "write-dest",
        "check-azure", "write-token", "probe", "transfer", "status",
        "verify", "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--login", default=None,
                   help="GitHub org/user login (required for plan/"
                        "create-vm/probe; later read from the VM's "
                        "gh_login tag)")
    p.add_argument("--owner-type", dest="owner_type",
                   choices=["org", "user"], default=None,
                   help="org (default) or user — rides the gh_owner_type "
                        "VM tag")
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None,
                   help="create-vm: OS disk GB (default: the Spec's 512 — "
                        "staging holds the whole corpus before azcopy)")
    p.add_argument("--dest-prefix", default=None,
                   help=f"prefix inside <slug>-raw (default "
                        f"{SPEC.default_dest_prefix}; multi-org: "
                        f"{SPEC.default_dest_prefix}/<org-login>)")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--limit", type=int, default=0,
                   help="transfer: only the first N repos (pilot)")
    p.add_argument("--only", default=None,
                   help="transfer: only this one repo by name")
    p.add_argument("--refresh", action="store_true",
                   help="transfer: re-fetch repos even if marked complete")
    p.add_argument("--skip-upload", dest="skip_upload", action="store_true",
                   help="transfer: pull to VM disk but don't azcopy")
    p.add_argument("--lfs-check-limit", dest="lfs_check_limit", type=int,
                   default=200,
                   help="probe: how many repos to check for a root "
                        ".gitattributes with filter=lfs")
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-transfer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.command in ("plan", "create-vm", "probe") and args.login is None:
        p.error(f"{args.command} requires --login")
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    engine_cmds = {"plan": eng.cmd_plan, "create-vm": eng.cmd_create_vm,
                   "allow-network": eng.cmd_allow_network,
                   "check-azure": eng.cmd_check_azure}
    own_cmds = {"discover": cmd_discover, "write-dest": cmd_write_dest,
                "write-token": cmd_write_token, "probe": cmd_probe,
                "transfer": cmd_transfer, "status": cmd_status,
                "verify": cmd_verify, "teardown": cmd_teardown}
    try:
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
