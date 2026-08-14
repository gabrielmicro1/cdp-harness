#!/usr/bin/env python3
"""Shared engine for the *-azure-transfer skills (gcs, dropbox).

Moves a cloud-source corpus into a company's `<slug>-raw` container via a
temporary same-region Azure VM running rclone. One code path per behavior:
the thin CLIs (`gcs_transfer.py`, `dropbox_transfer.py`) supply a SOURCE
spec; everything else — VM lifecycle, SAS, secrets plumbing, status parsing,
verify, teardown — lives here exactly once. See the corresponding SKILL.md
files under .claude/skills/.

Hard rules enforced here (identical for every source):
- NEVER touches storage-account network rules (human-only via internal UI;
  az only on an explicit user override relayed by the skill layer).
- Secrets (SAS URL, source OAuth token) go to the VM's rclone.conf over ssh
  stdin only — never argv, never files here, never printed.
- No state file: Azure is the source of truth (VM name + tags).

Lessons baked in from the song-division run (2026-08):
- Same-region VMs can't be allowed by IP rule — check-azure reads the
  ruleset on 403 and says whether the entry is missing vs propagating
  (propagation is ~10s, not minutes).
- `az vm create` masks SkuNotAvailable behind a CLI traceback — create
  failures are parsed and unrestricted same-family sizes are suggested.
- Teardown must delete the VNET (and remind about the vnet-rule + IP rule
  on the SA) — NIC/disk die with the VM, PIP/NSG/VNET don't.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

ADMIN_USER = "azureuser"
TMUX_SESSION = "transfer"
LOG_PATH = f"/home/{ADMIN_USER}/transfer.log"
RCLONE_CONF = f"/home/{ADMIN_USER}/.config/rclone/rclone.conf"
BOOTSTRAP_SH = Path(__file__).resolve().parent / "bootstrap-vm.sh"
DRY_RUN_IP = "203.0.113.10"  # TEST-NET placeholder shown in --dry-run output


class Spec:
    """What differs between sources. Everything else is shared."""

    def __init__(self, *, source_name: str, vm_prefix: str, purpose: str,
                 loc_tag: str, loc_argname: str, loc_required: bool,
                 default_dest_prefix: str, authorize_target: str,
                 remote_type: str, extra_rclone_flags: str = "",
                 default_transfers: int = 32, default_checkers: int = 64,
                 remote_extra: str = "", extra_cli_opts: list | None = None):
        self.source_name = source_name          # rclone remote name ("gcs")
        self.vm_prefix = vm_prefix              # "xfer-" / "xfer-dbx-"
        self.purpose = purpose                  # purpose=<...> tag
        self.loc_tag = loc_tag                  # VM tag carrying the source loc
        self.loc_argname = loc_argname          # argparse dest ("bucket"/"path")
        self.loc_required = loc_required
        self.default_dest_prefix = default_dest_prefix
        self.authorize_target = authorize_target  # `rclone authorize "<...>"`
        self.remote_type = remote_type          # rclone config `type = <...>`
        self.extra_rclone_flags = extra_rclone_flags
        self.default_transfers = default_transfers
        self.default_checkers = default_checkers
        self.remote_extra = remote_extra        # fixed conf lines ("scope = …")
        # Optional source-specific settings that flow CLI flag → VM tag →
        # rclone.conf key (e.g. gdrive --team-drive). Each entry:
        # {"flag", "argname", "tag", "conf_key", "help"}
        self.extra_cli_opts = extra_cli_opts or []

    def vm_name(self, slug: str) -> str:
        return f"{self.vm_prefix}{slug}"

    def source_ref(self, loc: str) -> str:
        """rclone path for the source ('gcs:bucket', 'dropbox:' for root)."""
        return f"{self.source_name}:{loc or ''}"

    def remote_section(self, token_json: str) -> str:
        return (f"[{self.source_name}]\ntype = {self.remote_type}\n"
                + self.remote_extra + f"token = {token_json}\n")


# ── ssh plumbing ─────────────────────────────────────────────────────────────

def run_ssh(ip: str, remote_cmd: str, stdin_data: str | None = None,
            dry_run: bool = False, timeout: int = 120,
            check: bool = True) -> subprocess.CompletedProcess:
    """Run a command on the VM. Secrets ride stdin_data only, never argv.

    In dry-run, the command is printed but stdin content is summarized as a
    byte count — the SAS/token must never appear in any output.
    """
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
           f"{ADMIN_USER}@{ip}", remote_cmd]
    if dry_run:
        note = f"  (stdin: {len(stdin_data)} bytes, redacted)" if stdin_data else ""
        shown = " ".join(remote_cmd.split())  # collapse embedded scripts
        if len(shown) > 160:
            shown = shown[:160] + "…"
        print(f"DRY-RUN: ssh {ADMIN_USER}@{ip} '{shown}'{note}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    proc = subprocess.run(cmd, input=stdin_data, capture_output=True,
                          text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise common.HarnessError(
            f"ssh command failed (rc={proc.returncode}): {remote_cmd[:80]}: "
            f"{(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


# Remote helper: rewrite one section of rclone.conf atomically, mode 600.
# The new section arrives on stdin so its contents never touch argv or logs.
_SECTION_WRITER = """
import os, sys
section = sys.argv[1]
new = sys.stdin.read()
path = os.path.expanduser("~/.config/rclone/rclone.conf")
os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
lines, keep, skipping = [], [], False
if os.path.exists(path):
    lines = open(path).read().splitlines()
for ln in lines:
    if ln.strip() == "[%s]" % section:
        skipping = True
        continue
    if skipping and ln.strip().startswith("["):
        skipping = False
    if not skipping:
        keep.append(ln)
body = "\\n".join(keep).strip()
out = (body + "\\n\\n" if body else "") + new.strip() + "\\n"
fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(out)
os.replace(path + ".tmp", path)
os.chmod(path, 0o600)
print("section [%s] written" % section)
"""


def write_conf_section(ip: str, section: str, section_text: str,
                       dry_run: bool = False) -> None:
    run_ssh(ip, f"python3 -c '{_SECTION_WRITER}' {section}",
            stdin_data=section_text, dry_run=dry_run)


# ── azure lookups ────────────────────────────────────────────────────────────

def load_cfg(root: Path, slug: str) -> dict:
    try:
        return common.load_config(root, slug)
    except FileNotFoundError:
        raise common.HarnessError(
            f"{slug} is not onboarded (no companies/{slug}/config.json) — "
            "run onboard-company first")


def set_subscription(cfg: dict, dry_run: bool) -> None:
    common.run_az(["account", "set", "-s", cfg["subscription"]], dry_run=dry_run)


def sa_region(cfg: dict, dry_run: bool) -> str:
    data = common.az_json(["storage", "account", "show",
                           "-g", cfg["resource_group"],
                           "-n", cfg["storage_account"],
                           "--query", "location"], dry_run=dry_run)
    return data if data else "(unknown — dry-run)"


def get_vm(spec: Spec, cfg: dict, slug: str, dry_run: bool) -> dict | None:
    """VM record with tags, power state, public IP — or None if absent."""
    proc = common.run_az(["vm", "show", "-d", "-g", cfg["resource_group"],
                          "-n", spec.vm_name(slug), "-o", "json"],
                         dry_run=dry_run, check=False)
    if dry_run or proc.returncode != 0 or not proc.stdout.strip():
        return None
    vm = json.loads(proc.stdout)
    return {"name": vm["name"], "power_state": vm.get("powerState"),
            "public_ip": vm.get("publicIps") or None,
            "tags": vm.get("tags") or {}, "location": vm.get("location")}


def require_vm(spec: Spec, cfg: dict, slug: str, dry_run: bool) -> dict:
    vm = get_vm(spec, cfg, slug, dry_run)
    if vm is None:
        if dry_run:
            return {"name": spec.vm_name(slug), "power_state": "VM running",
                    "public_ip": DRY_RUN_IP, "tags": {}, "location": "(dry-run)"}
        raise common.HarnessError(
            f"transfer VM {spec.vm_name(slug)} not found in "
            f"{cfg['resource_group']} — run setup first")
    if not vm["public_ip"]:
        raise common.HarnessError(
            f"{vm['name']} has no public IP (power state: {vm['power_state']})"
            " — was it deallocated? Static IP + no-deallocate is a hard rule.")
    return vm


def loc_and_prefix(spec: Spec, vm: dict, args) -> tuple[str, str]:
    """Source location + dest prefix from VM tags (set at create) or flags."""
    loc = getattr(args, spec.loc_argname, None)
    if loc is None:
        loc = vm["tags"].get(spec.loc_tag)
    prefix = getattr(args, "dest_prefix", None) or vm["tags"].get(
        "dest_prefix", spec.default_dest_prefix)
    if loc is None and spec.loc_required:
        raise common.HarnessError(
            f"source location unknown: VM has no {spec.loc_tag} tag — "
            f"pass --{spec.loc_argname.replace('_', '-')}")
    return loc or "", prefix


# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_plan(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    set_subscription(cfg, args.dry_run)
    region = sa_region(cfg, args.dry_run)
    loc = getattr(args, spec.loc_argname) or ""
    return {
        "slug": args.slug,
        "vm_name": spec.vm_name(args.slug),
        "vm_size": args.vm_size,
        "region": region,
        "resource_group": args.rg or cfg["resource_group"],
        "storage_account": cfg["storage_account"],
        "container": cfg["container"],
        "dest": f"{cfg['container']}/{args.dest_prefix}",
        "source": spec.source_ref(loc),
        "sas_expiry_days": args.sas_days,
        "note": ("VM billing starts at create and runs until teardown; "
                 "public IP is static Standard SKU (never deallocate). "
                 "Same-region reminder: the SA firewall needs a vnet-rule "
                 "for this VM's subnet — IP rules alone never match "
                 "same-region traffic."),
    }


def _diagnose_create_failure(stderr: str, size: str, region: str,
                             dry_run: bool) -> str:
    """Turn az vm create's noisy failures into an actionable reason."""
    if "SkuNotAvailable" in stderr or "Capacity Restrictions" in stderr:
        family = size.rsplit("s_", 1)[0].rsplit("_v", 1)[0]  # Standard_D8
        alts = common.az_json(["vm", "list-skus", "--location", region,
                               "--size", family,
                               "--query", "[?restrictions==`[]`].name"],
                              dry_run=dry_run) or []
        return (f"SkuNotAvailable: {size} is capacity-restricted in {region} "
                f"for this subscription. Unrestricted {family}* sizes there: "
                f"{', '.join(alts) or 'none found'} — re-run with --vm-size.")
    if "already consumed" in stderr:
        return ("azure-cli masked the real ARM error ('content already "
                "consumed' bug) — re-run the printed az vm create by hand "
                "to see the preflight validation failure. Full tail: "
                + stderr[-800:])
    return stderr[-800:]


def cmd_create_vm(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    set_subscription(cfg, args.dry_run)
    rg = args.rg or cfg["resource_group"]
    region = sa_region(cfg, args.dry_run)
    name = spec.vm_name(args.slug)
    loc = getattr(args, spec.loc_argname) or ""
    create_args = ["vm", "create", "-g", rg, "-n", name,
                   "--image", "Ubuntu2204", "--size", args.vm_size,
                   "--public-ip-sku", "Standard",
                   "--accelerated-networking", "true",
                   "--admin-username", ADMIN_USER, "--generate-ssh-keys",
                   "--os-disk-delete-option", "Delete",
                   "--nic-delete-option", "Delete",
                   "--tags", f"purpose={spec.purpose}",
                   f"engagement={args.slug}",
                   f"{spec.loc_tag}={loc}",
                   f"dest_container={cfg['container']}",
                   f"dest_prefix={args.dest_prefix}"]
    for opt in spec.extra_cli_opts:  # e.g. gdrive team-drive id → tag
        val = getattr(args, opt["argname"], None)
        if val:
            create_args.append(f"{opt['tag']}={val}")
    if region and not region.startswith("("):
        create_args += ["--location", region]
    proc = common.run_az(create_args, dry_run=args.dry_run, timeout=900,
                         check=False)
    if proc.returncode != 0 and not args.dry_run:
        raise common.HarnessError("az vm create failed: " +
                                  _diagnose_create_failure(
                                      proc.stderr or "", args.vm_size,
                                      region, args.dry_run))
    vm = get_vm(spec, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"] if vm else DRY_RUN_IP
    if not args.dry_run and not ip:
        raise common.HarnessError(f"{name} created but no public IP visible")
    bootstrap = BOOTSTRAP_SH.read_text()
    run_ssh(ip, "sudo bash -s", stdin_data=bootstrap, dry_run=args.dry_run,
            timeout=600)
    return {"vm": name, "resource_group": rg, "region": region,
            "public_ip": ip,
            "next": ("HUMAN STEP: allow this VM on storage account "
                     f"{cfg['storage_account']} via the internal "
                     "network-rules UI. Same-region caveat: an IP rule "
                     "alone will NOT work — the VM's subnet needs the "
                     "Microsoft.Storage service endpoint + a vnet-rule "
                     "(az only on explicit user override). This harness "
                     "never adds network rules on its own.")}


def cmd_write_azure_remote(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    set_subscription(cfg, args.dry_run)
    vm = require_vm(spec, cfg, args.slug, args.dry_run)
    expiry = common.iso(common.utc_now() + timedelta(days=args.sas_days))
    proc = common.run_az(["storage", "container", "generate-sas",
                          "--account-name", cfg["storage_account"],
                          "-n", cfg["container"],
                          "--permissions", "racwl",
                          "--expiry", expiry, "--https-only",
                          "-o", "tsv"], dry_run=args.dry_run, timeout=120)
    sas = proc.stdout.strip() if not args.dry_run else "<sas-redacted>"
    sas_url = (f"https://{cfg['storage_account']}.blob.core.windows.net/"
               f"{cfg['container']}?{sas}")
    section = f"[azure]\ntype = azureblob\nsas_url = {sas_url}\n"
    write_conf_section(vm["public_ip"], "azure", section, dry_run=args.dry_run)
    return {"remote": "azure", "container": cfg["container"],
            "sas_expiry": expiry, "written_to": f"{vm['name']}:{RCLONE_CONF}"}


def cmd_check_azure(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    vm = require_vm(spec, cfg, args.slug, args.dry_run)
    proc = run_ssh(vm["public_ip"], f"rclone lsd azure:{cfg['container']} 2>&1",
                   dry_run=args.dry_run, check=False, timeout=90)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return {"ok": True, "note": "container reachable (empty output is fine)"}
    if "AuthorizationFailure" in out or "403" in out:
        # Read-only look at the ruleset (never modified on this path) to
        # split "UI entry never landed" from "propagating".
        rules = common.az_json(["storage", "account", "show",
                                "-g", cfg["resource_group"],
                                "-n", cfg["storage_account"],
                                "--query", "networkRuleSet.ipRules[].ipAddressOrRange"],
                               dry_run=args.dry_run) or []
        vnets = common.az_json(["storage", "account", "show",
                                "-g", cfg["resource_group"],
                                "-n", cfg["storage_account"],
                                "--query",
                                "networkRuleSet.virtualNetworkRules[].virtualNetworkResourceId"],
                               dry_run=args.dry_run) or []
        has_vnet_rule = any(vm["name"] + "VNET" in v for v in vnets)
        listed = vm["public_ip"] in rules
        if has_vnet_rule:
            hint = ("VM subnet vnet-rule is present but storage still 403s "
                    "— propagation. Wait ~10s and retry.")
        elif listed:
            hint = ("VM IP is in the ruleset but 403 persists — same-region "
                    "VMs are NOT matched by IP rules. The subnet needs the "
                    "Microsoft.Storage service endpoint + a vnet-rule "
                    "(human/UI first; az only on explicit user override).")
        else:
            hint = (f"VM IP {vm['public_ip']} is NOT in the storage "
                    "account's rules and no vnet-rule exists — the "
                    "internal-UI entry didn't land (wrong account or "
                    "typo?). NOT a SAS problem — do not re-mint.")
        return {"ok": False, "cause": "network-rule",
                "vm_ip_in_ruleset": listed,
                "vm_vnet_rule_present": has_vnet_rule, "hint": hint}
    if "AuthenticationFailed" in out or "Signature" in out:
        return {"ok": False, "cause": "sas",
                "hint": "SAS invalid/expired/wrong container — re-mint via "
                        "write-azure-remote."}
    return {"ok": False, "cause": "other", "output_tail": out.strip()[-400:]}


def cmd_write_source_remote(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    vm = require_vm(spec, cfg, args.slug, args.dry_run)
    loc, _ = loc_and_prefix(spec, vm, args)
    token = sys.stdin.read().strip()
    if not args.dry_run:
        try:  # token block from `rclone authorize` is one JSON object
            json.loads(token)
        except ValueError:
            raise common.HarnessError(
                "stdin was not valid JSON — paste exactly the token block "
                "between the ---> markers from `rclone authorize`")
    section = spec.remote_section(" ".join(token.split()))
    for opt in spec.extra_cli_opts:  # flag wins, else the VM tag from create
        val = (getattr(args, opt["argname"], None)
               or vm["tags"].get(opt["tag"]))
        if val:
            section += f"{opt['conf_key']} = {val}\n"
    write_conf_section(vm["public_ip"], spec.source_name, section,
                       dry_run=args.dry_run)
    src = spec.source_ref(loc)
    ls = run_ssh(vm["public_ip"],
                 f"rclone lsd '{src}' --max-depth 2 2>&1 | head -20",
                 dry_run=args.dry_run, check=False, timeout=180)
    if ls.returncode != 0 and not args.dry_run:
        return {"ok": False, "stage": f"rclone lsd {spec.source_name}",
                "output_tail": (ls.stdout + ls.stderr).strip()[-400:],
                "hint": "auth errors here usually mean the token came from "
                        "the wrong account (one without access to the "
                        "source data)."}
    size = run_ssh(vm["public_ip"], f"rclone size '{src}' --json",
                   dry_run=args.dry_run, check=False, timeout=1800)
    info = {}
    if not args.dry_run and size.returncode == 0:
        info = json.loads(size.stdout)
    return {"ok": True, "source": src,
            "total_bytes": info.get("bytes"),
            "total_human": common.human_bytes(info.get("bytes")),
            "object_count": info.get("count"),
            "listing_head": ls.stdout.strip()[:800] if not args.dry_run else ""}


def _tmux_alive(ip: str, dry_run: bool) -> bool:
    proc = run_ssh(ip, f"tmux has-session -t {TMUX_SESSION} 2>/dev/null "
                       "&& echo alive || echo dead",
                   dry_run=dry_run, check=False)
    return "alive" in (proc.stdout or "")


def cmd_transfer(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    vm = require_vm(spec, cfg, args.slug, args.dry_run)
    loc, prefix = loc_and_prefix(spec, vm, args)
    if _tmux_alive(vm["public_ip"], args.dry_run):
        return {"ok": False, "cause": "already-running",
                "hint": "tmux session 'transfer' is alive — use status."}
    extra = (" " + spec.extra_rclone_flags) if spec.extra_rclone_flags else ""
    rclone_cmd = (f"rclone copy '{spec.source_ref(loc)}' "
                  f"azure:{cfg['container']}/{prefix} "
                  f"--transfers {args.transfers} --checkers {args.checkers} "
                  f"--retries 5 --log-file={LOG_PATH} --log-level INFO "
                  f"--stats 1m --stats-log-level NOTICE{extra}")
    run_ssh(vm["public_ip"],
            f"tmux new-session -d -s {TMUX_SESSION} \"{rclone_cmd}\"",
            dry_run=args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True}
    run_ssh(vm["public_ip"], "sleep 5", check=False)
    alive = _tmux_alive(vm["public_ip"], False)
    tail = run_ssh(vm["public_ip"], f"tail -5 {LOG_PATH} 2>/dev/null",
                   check=False)
    return {"ok": alive, "session": TMUX_SESSION,
            "dest": f"{cfg['container']}/{prefix}",
            "log_tail": (tail.stdout or "").strip(),
            "note": ("re-running transfer after an interruption is safe — "
                     "rclone skips files already copied") if alive else
                    "session died immediately — check status / log tail"}


_STATS_XFER = re.compile(
    r"Transferred:\s+([\d.]+\s*\w+) / ([\d.]+\s*\w+), (\d+%|-)"
    r", ([\d.]+\s*\w+/s), ETA (\S+)")


def cmd_status(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    vm = require_vm(spec, cfg, args.slug, args.dry_run)
    alive = _tmux_alive(vm["public_ip"], args.dry_run)
    tail = run_ssh(vm["public_ip"], f"tail -n 300 {LOG_PATH} 2>/dev/null",
                   dry_run=args.dry_run, check=False, timeout=60)
    out = tail.stdout or ""
    stats = {}
    for m in _STATS_XFER.finditer(out):  # keep the LAST stats block
        stats = {"done": m.group(1), "total": m.group(2),
                 "percent": m.group(3), "throughput": m.group(4),
                 "eta": m.group(5)}
    errors_m = re.findall(r"Errors:\s+(\d+)", out)
    error_lines = [ln for ln in out.splitlines() if " ERROR " in ln]
    distinct_errors: list[str] = []
    for ln in reversed(error_lines):  # most recent distinct messages
        msg = ln.split(" ERROR ", 1)[1][:160]
        if msg not in distinct_errors:
            distinct_errors.append(msg)
        if len(distinct_errors) >= 5:
            break
    finished = (not alive) and ("Elapsed time" in out)
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "transfer_running": alive, "looks_finished": finished,
            "stats": stats or None,
            "error_count": int(errors_m[-1]) if errors_m else None,
            "recent_distinct_errors": distinct_errors,
            "log_tail": "\n".join(out.strip().splitlines()[-8:])}


def cmd_verify(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    vm = require_vm(spec, cfg, args.slug, args.dry_run)
    loc, prefix = loc_and_prefix(spec, vm, args)
    proc = run_ssh(vm["public_ip"],
                   f"rclone check '{spec.source_ref(loc)}' "
                   f"azure:{cfg['container']}/{prefix} --one-way 2>&1 "
                   "| tail -40",
                   dry_run=args.dry_run, check=False, timeout=3600)
    out = (proc.stdout or "").strip()
    clean = proc.returncode == 0
    m = re.search(r"(\d+) differences found", out)
    return {"clean": clean if not args.dry_run else None,
            "differences": int(m.group(1)) if m else (0 if clean else None),
            "output_tail": out[-1200:],
            "hint": None if clean else (
                "Missing files usually mean the source was still changing "
                "when the copy ran (or export packets expired). Re-run "
                "transfer (resume is safe), then re-verify.")}


def cmd_teardown(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    set_subscription(cfg, args.dry_run)
    rg = args.rg or cfg["resource_group"]
    name = spec.vm_name(args.slug)
    vm = get_vm(spec, cfg, args.slug, args.dry_run)
    if vm is None and not args.dry_run:
        return {"ok": False, "cause": "no-vm",
                "hint": f"{name} not found in {rg} — nothing to tear down."}
    ip = vm["public_ip"] if vm else DRY_RUN_IP
    if not args.force:
        if vm and ip and _tmux_alive(ip, args.dry_run):
            return {"ok": False, "cause": "transfer-running",
                    "hint": "tmux session 'transfer' is still alive. "
                            "Refusing. Use --force only if the user "
                            "explicitly overrides."}
    if not args.confirmed:
        return {"ok": False, "cause": "not-confirmed",
                "hint": "Teardown deletes billable resources. The skill must "
                        "show the plan and get explicit user confirmation, "
                        "then re-run with --confirmed."}
    # NIC + OS disk are delete-option-tied to the VM; PIP, NSG and VNET are
    # not. VNET last — its subnet is only free once the NIC died with the VM.
    common.run_az(["vm", "delete", "-g", rg, "-n", name, "--yes"],
                  dry_run=args.dry_run, timeout=900)
    deleted = [f"vm:{name}", "nic (delete-option)", "os-disk (delete-option)"]
    for kind, res in (("public-ip", f"{name}PublicIP"), ("nsg", f"{name}NSG"),
                      ("vnet", f"{name}VNET")):
        proc = common.run_az(["network", kind, "delete", "-g", rg, "-n", res],
                             dry_run=args.dry_run, check=False, timeout=300)
        if proc.returncode == 0:
            deleted.append(f"{kind}:{res}")
    return {"ok": True, "deleted": deleted, "released_ip": ip,
            "reminders": [
                f"Remove {ip} AND the {name}VNET subnet vnet-rule from "
                f"storage account {cfg['storage_account']} (internal UI, or "
                "az only on explicit user override) — the vnet rule goes "
                "stale once the VNET is deleted",
                "The rwlc container SAS lapses on its own at its expiry — "
                "no revocation needed, but note the date",
                "Optionally tell the customer admin they can revoke the "
                "rclone OAuth grant from their account's security page"]}


def cmd_discover(spec: Spec, root: Path, args) -> dict:
    cfg = load_cfg(root, args.slug)
    set_subscription(cfg, args.dry_run)
    if args.dry_run:
        get_vm(spec, cfg, args.slug, True)
        return {"phase": "unknown (dry-run)",
                "note": "dry-run prints the discovery commands only"}
    vm = get_vm(spec, cfg, args.slug, False)
    if vm is None:
        return {"phase": "pre-setup", "vm": None,
                "hint": "no transfer VM — run setup to begin"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base,
                "hint": "VM exists but has no public IP (deallocated?) — "
                        "the manually-allowlisted access may be lost."}
    remotes = run_ssh(vm["public_ip"], "rclone listremotes 2>/dev/null",
                      check=False)
    if remotes.returncode != 0:
        return {"phase": "vm-unreachable", **base,
                "hint": "ssh failed — VM booting, or your key/IP changed. "
                        f"Try: ssh {ADMIN_USER}@{vm['public_ip']}"}
    have = set((remotes.stdout or "").split())
    if "azure:" not in have or f"{spec.source_name}:" not in have:
        return {"phase": "mid-setup", **base,
                "remotes_configured": sorted(have),
                "hint": "VM up but rclone remotes incomplete — resume setup "
                        "at the missing remote."}
    if _tmux_alive(vm["public_ip"], False):
        return {"phase": "transfer-running", **base,
                "hint": "use status for progress"}
    tail = run_ssh(vm["public_ip"], f"tail -3 {LOG_PATH} 2>/dev/null",
                   check=False)
    if (tail.stdout or "").strip():
        return {"phase": "transfer-stopped", **base,
                "log_tail": tail.stdout.strip(),
                "hint": "tmux dead but a log exists — run status, then "
                        "verify (clean finish) or transfer (resume)."}
    return {"phase": "setup-complete", **base,
            "hint": "both remotes configured, no transfer started yet — "
                    "run transfer when ready"}


# ── CLI builder (shared by the thin per-source scripts) ──────────────────────

def main(spec: Spec, doc: str) -> int:
    import argparse
    p = argparse.ArgumentParser(description=doc)
    p.add_argument("command", choices=[
        "discover", "plan", "create-vm", "write-azure-remote", "check-azure",
        f"write-{spec.source_name}-remote", "transfer", "status", "verify",
        "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    loc_flag = "--" + spec.loc_argname.replace("_", "-")
    p.add_argument(loc_flag, dest=spec.loc_argname, default=None,
                   help="source location (required for plan/create-vm if the "
                        "source needs one; later read from VM tags)")
    for opt in spec.extra_cli_opts:
        p.add_argument(opt["flag"], dest=opt["argname"], default=None,
                       help=opt["help"])
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--dest-prefix", default=None,
                   help=f"prefix inside <slug>-raw "
                        f"(default {spec.default_dest_prefix})")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--transfers", type=int, default=spec.default_transfers)
    p.add_argument("--checkers", type=int, default=spec.default_checkers)
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-transfer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if (spec.loc_required and args.command in ("plan", "create-vm")
            and getattr(args, spec.loc_argname) is None):
        p.error(f"{args.command} requires {loc_flag}")
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = spec.default_dest_prefix

    root = Path(args.root)
    fn = {"discover": cmd_discover, "plan": cmd_plan,
          "create-vm": cmd_create_vm,
          "write-azure-remote": cmd_write_azure_remote,
          "check-azure": cmd_check_azure,
          f"write-{spec.source_name}-remote": cmd_write_source_remote,
          "transfer": cmd_transfer, "status": cmd_status,
          "verify": cmd_verify, "teardown": cmd_teardown}[args.command]
    try:
        result = fn(spec, root, args)
    except common.HarnessError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    except subprocess.TimeoutExpired as e:
        print(json.dumps({"ok": False,
                          "error": f"timeout: {str(e)[:200]}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2
