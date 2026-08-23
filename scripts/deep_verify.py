#!/usr/bin/env python3
"""deep_verify.py — deep-verify a company's container on an in-region VM.

Runs the sizer (scripts/corpus_sizer_rest.py, pushed as-is — it is portable
stdlib+SAS by design) with DEEP_VERIFY=1 on a temporary Azure VM in the
storage account's own region: every compressed blob (zip/gz/bz2/xz) is
stream-decompressed and measured exactly instead of trusting zip central
directories and gz trailers. Same-region traffic means free egress and
multi-TB/hour throughput — the laptop path would pay ~$85/TB and days of
wall clock for the same bytes. Results land as a normal sizing run
(method "sized") carrying a `verification` coverage block, and the pulled
blob-index caches every measurement by ETag, so later shallow daily runs
replay the exact numbers at zero HTTP and a repeat deep run is listing-only.

VM family rules (borrowed from the transfer engines — transfer_engine.py is
reused verbatim for the lifecycle): storage access via the Microsoft.Storage
service endpoint + a vnet-rule (IP rules never match same-region VM
traffic); secrets over ssh stdin into a 600 env file only; no state file —
Azure (VM `deepv-<slug>` + tags) is the source of truth mid-run. Sizing
rules kept: the SAS is ACCOUNT-level `rl` READ-ONLY (1-day default,
--sas-days up to 2) — deep verify never gains a write path.

One-shot lifecycle: `step <slug>` inspects Azure and advances exactly one
phase per invocation (create VM → grant network + push + launch → poll →
harvest + AUTO-TEARDOWN). A deep run takes hours; each `step` call returns
in seconds-to-minutes, so the skill loops it across turns. Re-running after
a crash is safe: the partial TSV on the VM seeds the relaunch, and the
laptop cache caps the redo cost even if the VM itself is gone.

  deep_verify.py step <slug>          # advance one phase (the primary UX)
  deep_verify.py status <slug>        # inspect without advancing
  deep_verify.py discover <slug>      # phase reconstruction for new sessions
  deep_verify.py teardown <slug> --confirmed [--force]
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import phases  # noqa: E402
import transfer_engine as eng  # noqa: E402

SPEC = eng.Spec(
    source_name="deepverify",
    vm_prefix="deepv-",          # not xfer-* (may run beside a live transfer)
    purpose="deep-verify",       # and not verify-vm-* (legacy discovery name)
    loc_tag="container",
    loc_argname="container",
    loc_required=False,
    default_dest_prefix="",
    authorize_target="",         # no OAuth, no rclone remote — SAS only
    remote_type="",
    # UsedCapacity observed at create rides VM tags so harvest can stamp the
    # run file without a state file (tags are strings — bacancy lesson)
    extra_cli_opts=[
        {"flag": "--used-capacity", "argname": "used_capacity",
         "tag": "used_capacity", "conf_key": "", "help": ""},
        {"flag": "--used-capacity-at", "argname": "used_capacity_at",
         "tag": "used_capacity_at", "conf_key": "", "help": ""},
    ],
)

SIZER_PY = Path(__file__).resolve().parent / "corpus_sizer_rest.py"
WORK_DIR = f"/home/{eng.ADMIN_USER}/deep-verify"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/deepverify"
ENV_PATH = f"{ENV_DIR}/env"
TMUX = "deepverify"              # NOT the engine's "transfer" session


def tag_for(slug: str) -> str:
    return f"{slug}-deep"


# ── VM-side plumbing ─────────────────────────────────────────────────────────

def _push_text(ip: str, path: str, content: str, dry_run: bool,
               mode600: bool = False) -> None:
    pre = "umask 077 && " if mode600 else ""
    eng.run_ssh(ip, f"{pre}mkdir -p {os.path.dirname(path)} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _push_b64(ip: str, path: str, payload: bytes, dry_run: bool) -> None:
    """Binary push: base64 text over ssh stdin (run_ssh is text-mode)."""
    eng.run_ssh(ip, f"mkdir -p {os.path.dirname(path)} && "
                    f"base64 -d > {path}",
                stdin_data=base64.b64encode(payload).decode(),
                dry_run=dry_run, timeout=600)


def _pull_b64(ip: str, path: str, dry_run: bool) -> bytes:
    proc = eng.run_ssh(ip, f"base64 < {path}", dry_run=dry_run, timeout=600)
    return b"" if dry_run else base64.b64decode(proc.stdout)


def _remote_exists(ip: str, path: str, dry_run: bool) -> bool:
    proc = eng.run_ssh(ip, f"test -e {path} && echo yes || echo no",
                       dry_run=dry_run, check=False)
    return "yes" in (proc.stdout or "")


def _tmux_alive(ip: str, dry_run: bool) -> bool:
    proc = eng.run_ssh(ip, f"tmux has-session -t {TMUX} 2>/dev/null "
                           "&& echo alive || echo dead",
                       dry_run=dry_run, check=False)
    return "alive" in (proc.stdout or "")


def _progress(ip: str, tag: str, dry_run: bool) -> dict:
    """Log tail + rows-so-far. wc -1: line 1 of sizes.tsv is the #matcher
    header, not a data row."""
    proc = eng.run_ssh(
        ip, f"cd {WORK_DIR} 2>/dev/null; "
            f"tail -2 {tag}.log 2>/dev/null; "
            f"echo ---; wc -l < {tag}.sizes.tsv 2>/dev/null || echo 0",
        dry_run=dry_run, check=False)
    out = (proc.stdout or "").split("---")
    rows = 0
    try:
        rows = max(int(out[1].strip() or 0) - 1, 0)
    except (IndexError, ValueError):
        pass
    return {"log_tail": out[0].strip()[-400:], "blobs_done": rows}


def _connectivity_check(ip: str, dry_run: bool) -> subprocess.CompletedProcess:
    """One maxresults=1 list GET from the VM using the pushed env's SAS.
    NOT eng.cmd_check_azure — that assumes an rclone [azure] remote, whose
    setup would need a racwl SAS this read-only path must never mint."""
    py = ('import os,urllib.request;'
          'u="https://%s.blob.core.windows.net/%s?restype=container'
          '&comp=list&maxresults=1&%s"%(os.environ["SA"],'
          'os.environ["CONTAINER"],os.environ["AZURE_STORAGE_SAS"]);'
          'urllib.request.urlopen(u,timeout=20).read();print("OK")')
    return eng.run_ssh(ip, f"set -a; . {ENV_PATH}; set +a; "
                           f"python3 -c '{py}' 2>&1",
                       dry_run=dry_run, check=False, timeout=60)


# ── step phases ──────────────────────────────────────────────────────────────

def _phase_create(cfg: dict, root: Path, args) -> dict:
    metric, metric_at = phases.read_used_capacity(cfg, dry_run=args.dry_run)
    if metric is not None:
        args.used_capacity = str(metric)       # tags are strings
        args.used_capacity_at = metric_at or ""
    res = eng.cmd_create_vm(SPEC, root, args)
    return {"phase": "vm-created", "vm": res["vm"],
            "public_ip": res["public_ip"], "region": res["region"],
            "used_capacity": metric,
            "next": "run `step` again — it grants storage access, pushes the "
                    "sizer + cache, and launches the deep run"}


def _phase_launch(cfg: dict, root: Path, vm: dict, args) -> dict:
    ip = vm["public_ip"]
    tag = tag_for(args.slug)
    # allow-network is idempotent (already-present short-circuits) — calling
    # it every launch also self-heals a reconciler-stripped vnet-rule
    net = eng.cmd_allow_network(SPEC, root, args)

    prev_tail = None
    if _remote_exists(ip, f"{WORK_DIR}/{tag}.stdout", args.dry_run):
        proc = eng.run_ssh(ip, f"tail -c 400 {WORK_DIR}/{tag}.stdout",
                           dry_run=args.dry_run, check=False)
        prev_tail = (proc.stdout or "").strip() or None

    # seed handling mirrors phases.launch, executed remotely: a crashed
    # attempt's partial TSV becomes the seed; every other stale work file
    # dies (a stale .done would fake completion)
    use_cache = not args.no_cache
    keep_seed = ("[ -f {t}.sizes.tsv ] && mv -f {t}.sizes.tsv {t}.seed.tsv; "
                 if use_cache else "rm -f {t}.seed.tsv; ")
    eng.run_ssh(ip, f"mkdir -p {WORK_DIR} && cd {WORK_DIR} && "
                    + keep_seed.format(t=tag) +
                    f"find . -maxdepth 1 -name '{tag}.*' "
                    f"! -name '{tag}.seed.tsv' -delete; true",
                dry_run=args.dry_run, check=False)
    have_seed = use_cache and _remote_exists(
        ip, f"{WORK_DIR}/{tag}.seed.tsv", args.dry_run)

    _push_text(ip, f"{WORK_DIR}/corpus_sizer_rest.py", SIZER_PY.read_text(),
               args.dry_run)
    index = common.company_dir(root, args.slug) / "blob-index.tsv.gz"
    have_cache = use_cache and index.exists()
    if have_cache:
        _push_b64(ip, f"{WORK_DIR}/blob-index.tsv.gz", index.read_bytes(),
                  args.dry_run)

    sas = phases.mint_sas(cfg, dry_run=args.dry_run, days=args.sas_days)
    expected = common.load_expected(root, args.slug)
    services = ",".join((expected or {}).get("services", {}).keys())
    # single-quoted values: the SAS contains '&' (checkmate lesson); az SAS
    # and service names never contain single quotes
    env_lines = [
        f"SA='{cfg['storage_account']}'",
        f"CONTAINER='{cfg['container']}'",
        f"AZURE_STORAGE_SAS='{sas}'",
        f"TAG='{tag}'",
        f"OUT_DIR='{WORK_DIR}'",
        "DEEP_VERIFY='1'",
        f"SIZER_WORKERS='{args.workers}'",
        f"LIST_WORKERS='{args.list_workers}'",
    ]
    if services:
        env_lines.append(f"EXPECTED_SERVICES='{services}'")
    if have_cache:
        env_lines.append(f"CACHE_FILE='{WORK_DIR}/blob-index.tsv.gz'")
    if have_seed:
        env_lines.append(f"SEED_TSV='{WORK_DIR}/{tag}.seed.tsv'")
    _push_text(ip, ENV_PATH, "\n".join(env_lines) + "\n", args.dry_run,
               mode600=True)

    conn = _connectivity_check(ip, args.dry_run)
    out = (conn.stdout or "") + (conn.stderr or "")
    if not args.dry_run and conn.returncode != 0:
        if "403" in out:
            return {"phase": "waiting-network", "vnet_rule": net,
                    "hint": "storage still 403s from the VM — vnet-rule "
                            "propagation (~10-30s). Run `step` again. If it "
                            "persists, an external reconciler may be "
                            "stripping the rule. NOT a SAS problem — do "
                            "not re-mint."}
        return {"phase": "connectivity-failed", "output_tail": out[-400:],
                "hint": "the VM cannot reach the storage account — check "
                        "the output tail before relaunching"}

    eng.run_ssh(ip, f"tmux new-session -d -s {TMUX} "
                    f"\"set -a; . {ENV_PATH}; set +a; "
                    f"python3 {WORK_DIR}/corpus_sizer_rest.py "
                    f">> {WORK_DIR}/{tag}.stdout 2>&1\"",
                dry_run=args.dry_run)
    if args.dry_run:
        return {"phase": "launched", "dry_run": True}
    eng.run_ssh(ip, "sleep 3", check=False)
    alive = _tmux_alive(ip, False)
    # a tiny container can finish inside the 3s settle — dead tmux with a
    # .done is instant success, not a failed launch
    done = _remote_exists(ip, f"{WORK_DIR}/{tag}.done", False)
    phase = "running" if alive else ("finished" if done else "launch-failed")
    res = {"phase": phase, "vm": vm["name"],
           "cache_pushed": have_cache, "seeded": have_seed,
           **_progress(ip, tag, False)}
    if prev_tail:
        res["previous_attempt_tail"] = prev_tail
    if phase == "finished":
        res["next"] = "run `step` again — it harvests and tears down"
    elif not alive:
        res["hint"] = ("sizer died immediately — see log_tail / "
                       "previous_attempt_tail; `step` relaunches (seeded)")
    return res


def _phase_harvest(cfg: dict, root: Path, vm: dict, args) -> dict:
    ip = vm["public_ip"]
    tag = tag_for(args.slug)
    slug = args.slug
    proc = eng.run_ssh(ip, f"cat {WORK_DIR}/{tag}.summary.json",
                       dry_run=args.dry_run, timeout=300)
    if args.dry_run:
        return {"phase": "harvest", "dry_run": True}
    summary = json.loads(proc.stdout)

    metric = None
    try:
        metric = int(vm["tags"].get("used_capacity", ""))
    except ValueError:
        pass
    ver = summary.get("verification") or {}
    notes = [f"deep verify on VM {vm['name']} ({vm.get('location')}): "
             f"{ver.get('stream_compressed_bytes', 0) / 1e12:.2f} TB "
             f"compressed streamed server-side"]
    run = phases.summary_to_run(
        slug, summary,
        {"metric": metric, "metric_at": vm["tags"].get("used_capacity_at")},
        notes)
    run_path = phases._run_path(root, slug)
    common.write_json(run_path, run)

    # the pulled index is the company cache now — deep measurements replay
    # on every later shallow run (same move as phases.harvest_one)
    index_bytes = _pull_b64(ip, f"{WORK_DIR}/{tag}.index.tsv.gz", False)
    idx_path = common.company_dir(root, slug) / "blob-index.tsv.gz"
    fd, tmp = tempfile.mkstemp(dir=idx_path.parent, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(index_bytes)
    os.replace(tmp, idx_path)

    phases.update_status(root, slug, "sized")

    result = {"phase": "complete", "run_file": str(run_path),
              "totals": run["totals"], "verification": run["verification"],
              "duration_seconds": run["duration_seconds"]}
    if args.keep_vm:
        result["teardown"] = "skipped (--keep-vm) — remember `teardown " \
                             f"{slug} --confirmed` when done"
        return result
    # auto-teardown (the one-shot contract): harvest succeeded and the run
    # file is on disk, so a teardown failure must not fail the harvest
    args.confirmed = True
    args.force = True  # the engine checks its own 'transfer' session, not ours
    try:
        result["teardown"] = eng.cmd_teardown(SPEC, root, args)
    except Exception as exc:  # noqa: BLE001
        result["teardown"] = {"ok": False, "error": str(exc),
                              "hint": f"run `teardown {slug} --confirmed` "
                                      "to finish cleanup — the VM is still "
                                      "billing"}
    return result


def cmd_step(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    args.container = cfg["container"]   # SPEC loc → VM tag
    vm = eng.get_vm(SPEC, cfg, args.slug, args.dry_run)
    if vm is None:
        return _phase_create(cfg, root, args)
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", "vm": vm["name"],
                "hint": "VM exists but has no public IP (deallocated?) — "
                        "teardown and re-run step"}
    tag = tag_for(args.slug)
    if _remote_exists(vm["public_ip"], f"{WORK_DIR}/{tag}.done",
                      args.dry_run):
        return _phase_harvest(cfg, root, vm, args)
    if _tmux_alive(vm["public_ip"], args.dry_run):
        return {"phase": "running", "vm": vm["name"],
                **_progress(vm["public_ip"], tag, args.dry_run),
                "next": "run `step` again later — poll cadence should match "
                        "container size (minutes for small, hours for huge)"}
    return _phase_launch(cfg, root, vm, args)


def cmd_status(root: Path, args) -> dict:
    """Inspect without advancing: same detection as step, zero side effects."""
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.get_vm(SPEC, cfg, args.slug, args.dry_run)
    if vm is None:
        return {"phase": "pre-create", "vm": None,
                "hint": "no deep-verify VM — `step` creates one"}
    tag = tag_for(args.slug)
    ip = vm["public_ip"]
    base = {"vm": vm["name"], "public_ip": ip,
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not ip:
        return {"phase": "vm-no-public-ip", **base}
    if _remote_exists(ip, f"{WORK_DIR}/{tag}.done", args.dry_run):
        return {"phase": "ready-to-harvest", **base,
                "next": "`step` harvests and tears down"}
    if _tmux_alive(ip, args.dry_run):
        return {"phase": "running", **base, **_progress(ip, tag, args.dry_run)}
    return {"phase": "ready-to-launch", **base,
            **_progress(ip, tag, args.dry_run),
            "note": "no sizer running — `step` (re)launches, seeded from any "
                    "partial progress"}


def cmd_discover(root: Path, args) -> dict:
    """Phase reconstruction for a fresh session — status plus the config
    facts a new agent needs."""
    out = cmd_status(root, args)
    cfg = eng.load_cfg(root, args.slug)
    out["storage_account"] = cfg["storage_account"]
    out["container"] = cfg["container"]
    return out


def cmd_teardown(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.get_vm(SPEC, cfg, args.slug, args.dry_run)
    if vm and vm["public_ip"] and not args.force \
            and _tmux_alive(vm["public_ip"], args.dry_run):
        return {"ok": False, "cause": "deep-verify-running",
                "hint": f"tmux session '{TMUX}' is still alive — the run "
                        "would be lost. --force only on explicit user "
                        "override (the partial TSV seeds a future relaunch "
                        "only if you harvest it first)."}
    return eng.cmd_teardown(SPEC, root, args)


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["step", "status", "discover",
                                       "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None,
                   help="create: OS disk GB (image default ~30 GB is fine — "
                        "the sizer holds only chunks in memory)")
    p.add_argument("--sas-days", type=int, default=1, choices=(1, 2),
                   help="rl SAS expiry (2 for containers whose compressed "
                        "bytes exceed ~a day of streaming)")
    p.add_argument("--workers", type=int, default=16,
                   help="SIZER_WORKERS on the VM")
    p.add_argument("--list-workers", type=int, default=8,
                   help="LIST_WORKERS on the VM")
    p.add_argument("--no-cache", action="store_true",
                   help="skip the cache push — re-measure every blob")
    p.add_argument("--keep-vm", action="store_true",
                   help="harvest without the automatic teardown (debugging)")
    p.add_argument("--dest-prefix", default="")  # engine tag plumbing only
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-sizer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    args.container = None            # filled from config in cmd_step
    args.used_capacity = None        # filled before create
    args.used_capacity_at = None

    root = Path(args.root)
    cmds = {"step": cmd_step, "status": cmd_status,
            "discover": cmd_discover, "teardown": cmd_teardown}
    try:
        result = cmds[args.command](root, args)
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
