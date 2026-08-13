"""Shared sizing phases. fleet_size.py and size_company.py are thin CLIs over
these functions — the fleet is a loop over the same phases a single company
uses (single company = fleet of one). Never fork this logic into a CLI.

Phases (see CLAUDE.md "Azure operational model" for the why):
  0. skip_check      — UsedCapacity metric vs last run; unchanged → copied-forward
  1. launch          — firewall ensure, mint rl SAS, managed run-command create
  2. poll            — instance-view show (pure ARM read; parallel-safe)
  3. harvest         — parse summary JSON (4KB-truncation fallback: one invoke),
                       write sizing-run file, update status, cleanup

In-flight state persists in <root>/.fleet-state.json (gitignored) so polling
can resume across separate CLI invocations / agent turns.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import common
from common import HarnessError

RUN_CMD_TIMEOUT_S = 3 * 3600  # webspiders (10.9M blobs) took ~45 min; 3h headroom
TERMINAL_STATES = {"Succeeded", "Failed", "TimedOut", "Canceled"}


def state_path(root: Path) -> Path:
    return root / ".fleet-state.json"


def load_state(root: Path) -> dict:
    p = state_path(root)
    if p.exists():
        return common.read_json(p)
    return {"started_at": None, "companies": {}}


def save_state(root: Path, state: dict) -> None:
    common.write_json(state_path(root), state)


def run_command_name(slug: str) -> str:
    return f"sizer-{slug}"


def tag_for(slug: str) -> str:
    return f"{slug}-sizer"


def sa_resource_id(cfg: dict) -> str:
    return (f"/subscriptions/{cfg['subscription_id']}/resourceGroups/"
            f"{cfg['resource_group']}/providers/Microsoft.Storage/"
            f"storageAccounts/{cfg['storage_account']}")


# ── Phase 0: UsedCapacity skip check ─────────────────────────────────────────

def read_used_capacity(cfg: dict, dry_run: bool = False):
    """Latest non-null UsedCapacity datapoint → (bytes, iso_timestamp) or (None, None).
    Pure ARM read — no blob listing, no SAS, no firewall involvement."""
    data = common.az_json([
        "monitor", "metrics", "list",
        "--resource", sa_resource_id(cfg),
        "--metric", "UsedCapacity", "--interval", "PT1H", "--offset", "4h",
        "--aggregation", "Average",
    ], dry_run=dry_run)
    if data is None:
        return None, None
    try:
        points = data["value"][0]["timeseries"][0]["data"]
    except (KeyError, IndexError):
        return None, None
    for pt in reversed(points):
        if pt.get("average") is not None:
            return int(pt["average"]), pt.get("timeStamp")
    return None, None


def skip_check(root: Path, slug: str, cfg: dict, dry_run: bool = False) -> dict:
    """Decide launch vs copy-forward. Unchanged metric AND a previous run with
    totals → skip. Any doubt (no metric, no prior run) → launch (safe side)."""
    metric, metric_at = read_used_capacity(cfg, dry_run=dry_run)
    prev = common.latest_runs(root, slug, 1)
    prev = prev[0] if prev else None
    if metric is None:
        return {"skip": False, "metric": None, "metric_at": None,
                "reason": "no UsedCapacity datapoint — sizing to be safe"}
    if prev is None or "totals" not in prev:
        return {"skip": False, "metric": metric, "metric_at": metric_at,
                "reason": "no previous sizing run"}
    if prev.get("used_capacity_bytes") == metric:
        return {"skip": True, "metric": metric, "metric_at": metric_at,
                "reason": f"UsedCapacity unchanged ({metric} bytes)"}
    return {"skip": False, "metric": metric, "metric_at": metric_at,
            "reason": f"UsedCapacity changed {prev.get('used_capacity_bytes')} → {metric}"}


def write_copied_forward_run(root: Path, slug: str, metric, metric_at) -> Path:
    """New run file that copies the previous run's numbers without a VM launch."""
    prev = common.latest_runs(root, slug, 1)
    if not prev:
        raise HarnessError("copied-forward requires a previous sizing run")
    prev = prev[0]
    now = common.utc_now()
    run = {
        "slug": slug,
        "timestamp": common.iso(now),
        "method": "copied-forward",
        "copied_from": prev["timestamp"],
        "used_capacity_bytes": metric,
        "used_capacity_at": metric_at,
        "duration_seconds": 0,
        "totals": prev["totals"],
        "sources": prev["sources"],
        "methods": prev.get("methods", {}),
        "errors": prev.get("errors", {"total": 0, "by_type": {}}),
        "notes": [f"copied forward from {prev['timestamp']} (UsedCapacity unchanged)"],
    }
    path = common.company_dir(root, slug) / "sizing-runs" / f"{common.ts_basic(now)}.json"
    common.write_json(path, run)
    return path


# ── Phase 1: launch ──────────────────────────────────────────────────────────

def check_vm_running(cfg: dict, dry_run: bool = False) -> str:
    vm = cfg["vm"]
    proc = common.run_az([
        "vm", "get-instance-view", "-g", vm["resource_group"], "-n", vm["name"],
        "--query",
        "instanceView.statuses[?starts_with(code,'PowerState')].displayStatus|[0]",
        "-o", "tsv",
    ], dry_run=dry_run)
    return "VM running" if dry_run else proc.stdout.strip()


def firewall_ensure(cfg: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Allow the VM's subnet on the SA if (and only if) it isn't already.
    Returns (we_added, subnet_id). NEVER touches pre-existing rules."""
    vm = cfg["vm"]
    nic_id = common.run_az([
        "vm", "show", "-g", vm["resource_group"], "-n", vm["name"],
        "--query", "networkProfile.networkInterfaces[0].id", "-o", "tsv",
    ], dry_run=dry_run).stdout.strip()
    subnet_id = common.run_az([
        "network", "nic", "show", "--ids", nic_id or "<nic-id>",
        "--query", "ipConfigurations[0].subnet.id", "-o", "tsv",
    ], dry_run=dry_run).stdout.strip()
    already = common.run_az([
        "storage", "account", "show", "-n", cfg["storage_account"],
        "-g", cfg["resource_group"], "--query",
        f"contains(networkRuleSet.virtualNetworkRules[].virtualNetworkResourceId, '{subnet_id or '<subnet-id>'}')",
        "-o", "tsv",
    ], dry_run=dry_run).stdout.strip()
    if dry_run:
        print("DRY-RUN: (if subnet not already allowed) az network vnet subnet update "
              "--ids <subnet-id> --service-endpoints Microsoft.Storage; "
              "az storage account network-rule add ...; sleep 60")
        return False, subnet_id
    if already == "true":
        return False, subnet_id  # pre-existing — do NOT remove in cleanup
    common.run_az(["network", "vnet", "subnet", "update", "--ids", subnet_id,
                   "--service-endpoints", "Microsoft.Storage", "-o", "none"])
    common.run_az(["storage", "account", "network-rule", "add",
                   "-g", cfg["resource_group"], "--account-name",
                   cfg["storage_account"], "--subnet", subnet_id, "-o", "none"])
    time.sleep(60)  # propagation — a 403 right after adding = not propagated yet
    return True, subnet_id


def firewall_remove_if_ours(cfg: dict, subnet_id: str, we_added: bool,
                            dry_run: bool = False) -> None:
    if not we_added:
        return  # pre-existing rule — never delete
    common.run_az(["storage", "account", "network-rule", "remove",
                   "-g", cfg["resource_group"], "--account-name",
                   cfg["storage_account"], "--subnet", subnet_id, "-o", "none"],
                  dry_run=dry_run, check=False)


def mint_sas(cfg: dict, dry_run: bool = False) -> str:
    """Account SAS, rl only, 1-day expiry, https-only. Read-only is policy —
    we never write to client storage (see CLAUDE.md principle 3)."""
    from datetime import timedelta
    expiry = (common.utc_now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%MZ")
    proc = common.run_az([
        "storage", "account", "generate-sas",
        "--account-name", cfg["storage_account"],
        "--services", "b", "--resource-types", "sco",
        "--permissions", "rl", "--expiry", expiry, "--https-only", "-o", "tsv",
    ], dry_run=dry_run)
    return "<sas>" if dry_run else proc.stdout.strip()


def build_launch_script(cfg: dict, sas: str, tag: str) -> str:
    """The managed run-command script. nohup is belt-and-suspenders: if the
    run-command agent dies, the sizer survives and /var/tmp/$TAG.* remain as
    the manual rescue path. `wait $PID` makes executionState track completion;
    the final cat rides the summary home in instance-view output (≤4KB)."""
    sizer_b64 = base64.b64encode(
        (common.REPO_ROOT / "scripts" / "corpus_sizer_rest.py").read_bytes()
    ).decode()
    sas_b64 = base64.b64encode(sas.encode()).decode()
    return f"""\
echo '{sizer_b64}' | base64 -d > /var/tmp/corpus_sizer_rest.py || exit 9
export SA='{cfg["storage_account"]}' CONTAINER='{cfg["container"]}' TAG='{tag}'
export AZURE_STORAGE_SAS="$(echo '{sas_b64}' | base64 -d)"
rm -f /var/tmp/{tag}.done
nohup python3 /var/tmp/corpus_sizer_rest.py > /var/tmp/{tag}.stdout 2>&1 </dev/null &
PID=$!
echo "pid=$PID"
wait $PID
RC=$?
echo "rc=$RC"
cat /var/tmp/{tag}.summary.json 2>/dev/null
exit $RC
"""


def delete_run_command(cfg: dict, slug: str, dry_run: bool = False) -> None:
    """Managed run-command resources persist; a stale same-name one breaks the
    next create. Called defensively pre-launch and always in cleanup."""
    vm = cfg["vm"]
    common.run_az(["vm", "run-command", "delete",
                   "-g", vm["resource_group"], "--vm-name", vm["name"],
                   "--run-command-name", run_command_name(slug), "--yes"],
                  dry_run=dry_run, check=False, timeout=120)


def launch(root: Path, slug: str, cfg: dict, dry_run: bool = False) -> dict:
    """Fire off the sizer via managed run-command create --async-execution.
    Returns the per-company in-flight state dict."""
    if not cfg.get("vm", {}).get("exists", False):
        raise HarnessError("no VM discovered for this company (vm.exists=false)")
    power = check_vm_running(cfg, dry_run=dry_run)
    if "running" not in power.lower():
        raise HarnessError(f"VM {cfg['vm']['name']} is not running ({power or 'unknown'})")
    we_added_fw, subnet_id = firewall_ensure(cfg, dry_run=dry_run)
    sas = mint_sas(cfg, dry_run=dry_run)
    tag = tag_for(slug)
    script = build_launch_script(cfg, sas, tag)
    delete_run_command(cfg, slug, dry_run=dry_run)
    vm = cfg["vm"]
    if dry_run:
        print(f"DRY-RUN: az vm run-command create -g {vm['resource_group']} "
              f"--vm-name {vm['name']} --run-command-name {run_command_name(slug)} "
              f"--script '<{len(script)}-byte launch script: decode sizer, nohup, "
              f"wait pid, cat summary.json>' --timeout-in-seconds {RUN_CMD_TIMEOUT_S} "
              f"--async-execution")
    else:
        common.run_az(["vm", "run-command", "create",
                       "-g", vm["resource_group"], "--vm-name", vm["name"],
                       "--run-command-name", run_command_name(slug),
                       "--script", script,
                       "--timeout-in-seconds", str(RUN_CMD_TIMEOUT_S),
                       "--async-execution", "-o", "none"], timeout=300)
    return {"phase": "launched", "tag": tag, "we_added_fw": we_added_fw,
            "subnet_id": subnet_id, "launched_at": common.iso_now(),
            "outcome": None, "reason": None}


# ── Phase 2: poll ────────────────────────────────────────────────────────────

def poll_one(cfg: dict, slug: str, dry_run: bool = False) -> dict:
    """Instance-view read: pure ARM management-plane, no execution slot, no
    Conflict, parallel-safe. Returns {state, out, err}."""
    vm = cfg["vm"]
    data = common.az_json([
        "vm", "run-command", "show",
        "-g", vm["resource_group"], "--vm-name", vm["name"],
        "--run-command-name", run_command_name(slug), "--instance-view",
        "--query", "instanceView.{state:executionState,out:output,err:error}",
    ], dry_run=dry_run, timeout=120)
    if data is None:
        return {"state": "Unknown", "out": "", "err": ""}
    return {"state": data.get("state") or "Pending",
            "out": data.get("out") or "", "err": data.get("err") or ""}


# ── Phase 3: harvest ─────────────────────────────────────────────────────────

def _extract_summary_json(output: str):
    """The launch script's last stdout is the compact summary JSON. Find the
    last '{' and parse to the end; truncated output (4KB cap) fails parse."""
    idx = output.rfind("{\"sa\"")
    if idx < 0:
        idx = output.rfind("{")
    if idx < 0:
        return None
    try:
        return json.loads(output[idx:])
    except json.JSONDecodeError:
        return None


def invoke_cat_summary(cfg: dict, slug: str) -> dict | None:
    """Truncation fallback: ONE legacy invoke (one-at-a-time per VM applies)."""
    vm = cfg["vm"]
    proc = common.run_az([
        "vm", "run-command", "invoke", "-g", vm["resource_group"], "-n", vm["name"],
        "--command-id", "RunShellScript",
        "--scripts", f"cat /var/tmp/{tag_for(slug)}.summary.json",
        "--query", "value[0].message", "-o", "tsv",
    ], timeout=300)
    return _extract_summary_json(proc.stdout)


def fetch_sizes_tsv(cfg: dict, slug: str, dest: Path, max_chunks: int = 40) -> bool:
    """Optional per-blob detail via chunked invokes (~3KB/chunk after base64).
    Only for small containers / spot checks — a 10M-blob TSV cannot come home
    this way; use scp or a storage-side copy instead. Serial invokes only."""
    vm = cfg["vm"]
    b64_parts = []
    lines_per_chunk = 40  # 40 * ~76 chars ≈ 3KB, under the 4KB message cap
    for i in range(max_chunks):
        start = i * lines_per_chunk + 1
        end = start + lines_per_chunk - 1
        proc = common.run_az([
            "vm", "run-command", "invoke", "-g", vm["resource_group"], "-n",
            vm["name"], "--command-id", "RunShellScript",
            "--scripts",
            f"base64 /var/tmp/{tag_for(slug)}.sizes.tsv | sed -n '{start},{end}p'; "
            f"echo CHUNK-END",
            "--query", "value[0].message", "-o", "tsv",
        ], timeout=300)
        body = [ln for ln in proc.stdout.splitlines()
                if ln and "CHUNK-END" not in ln and not ln.startswith("Enable")]
        if not body:
            break
        b64_parts.extend(body)
        if len(body) < lines_per_chunk:
            break
    else:
        return False  # hit max_chunks — file bigger than this path supports
    dest.write_bytes(base64.b64decode("".join(b64_parts)))
    return True


def summary_to_run(slug: str, summary: dict, skip_info: dict, state: dict,
                   notes: list[str]) -> dict:
    return {
        "slug": slug,
        "timestamp": common.iso_now(),
        "method": "sized",
        "copied_from": None,
        "used_capacity_bytes": skip_info.get("metric"),
        "used_capacity_at": skip_info.get("metric_at"),
        "duration_seconds": summary.get("dur_s", 0),
        "totals": {
            "blob_count": summary["blobs"],
            "compressed_bytes": summary["comp"],
            "uncompressed_bytes": summary["unc"],
            "zero_byte_blobs": summary.get("zero", 0),
        },
        "sources": {
            name: {"blob_count": v[0], "compressed_bytes": v[1],
                   "uncompressed_bytes": v[2]}
            for name, v in summary.get("src", {}).items()
        },
        "methods": summary.get("methods", {}),
        "errors": {"total": summary.get("errors", 0),
                   "by_type": summary.get("err_types", {})},
        "notes": notes,
    }


def harvest_one(root: Path, slug: str, cfg: dict, comp_state: dict,
                poll_result: dict, dry_run: bool = False) -> Path:
    """Parse the finished run, write the sizing-run file, clean up Azure side.
    Raises HarnessError if no summary can be recovered."""
    notes = []
    summary = _extract_summary_json(poll_result.get("out", ""))
    if summary is None and not dry_run:
        # 4KB truncation or agent hiccup — the nohup'd sizer may still have
        # finished cleanly on the VM. One fallback invoke.
        summary = invoke_cat_summary(cfg, slug)
        notes.append("instance-view output truncated/unusable; "
                     "summary recovered via fallback invoke")
    if summary is None:
        raise HarnessError(
            f"run-command state={poll_result.get('state')} but no summary JSON "
            f"recoverable — manual rescue: check /var/tmp/{tag_for(slug)}.log on the VM")
    skip_info = {"metric": comp_state.get("metric"),
                 "metric_at": comp_state.get("metric_at")}
    run = summary_to_run(slug, summary, skip_info, comp_state, notes)
    path = (common.company_dir(root, slug) / "sizing-runs"
            / f"{common.ts_basic()}.json")
    common.write_json(path, run)
    cleanup(cfg, slug, comp_state, dry_run=dry_run)
    return path


def cleanup(cfg: dict, slug: str, comp_state: dict, dry_run: bool = False) -> None:
    """Delete the run-command resource (stale ones break the next create),
    rm temp files, remove the firewall rule only if WE added it."""
    delete_run_command(cfg, slug, dry_run=dry_run)
    vm = cfg["vm"]
    tag = tag_for(slug)
    common.run_az([
        "vm", "run-command", "invoke", "-g", vm["resource_group"], "-n", vm["name"],
        "--command-id", "RunShellScript",
        "--scripts", f"rm -f /var/tmp/{tag}.* /var/tmp/corpus_sizer_rest.py",
        "--query", "value[0].message", "-o", "tsv",
    ], dry_run=dry_run, check=False, timeout=300)
    firewall_remove_if_ours(cfg, comp_state.get("subnet_id", ""),
                            comp_state.get("we_added_fw", False), dry_run=dry_run)
    # SAS expires on its own (1 day) — nothing to revoke.


# ── status transitions (single owner of this logic — see CLAUDE.md) ──────────

STALL_DAYS = 3


def update_status(root: Path, slug: str, outcome: str, reason: str | None = None) -> dict:
    """Record last_run and apply automated stage transitions:
    pushing → stalled (no growth ≥3 days while <100%), stalled → pushing
    (growth resumed). Never touches onboarding/verifying/complete."""
    status = common.load_status(root, slug)
    now = common.utc_now()
    status["slug"] = slug
    status["last_run"] = {"timestamp": common.iso(now), "outcome": outcome,
                          "reason": reason}
    runs = common.latest_runs(root, slug, 2)
    if outcome in ("sized", "skipped-unchanged") and runs:
        latest = runs[0]["totals"]["uncompressed_bytes"]
        prev = runs[1]["totals"]["uncompressed_bytes"] if len(runs) > 1 else None
        grew = prev is None or latest > prev
        if grew:
            status["last_change_detected_at"] = common.iso(now)
            if status.get("stage") == "stalled":
                status["stage"] = "pushing"
        elif status.get("stage") == "pushing":
            last_change = status.get("last_change_detected_at")
            stale_days = ((now - common.parse_iso(last_change)).days
                          if last_change else 0)
            expected = common.load_expected(root, slug)
            total = (expected or {}).get("manifest_total_bytes") or 0
            pct = (latest / total * 100) if total else 0
            if last_change and stale_days >= STALL_DAYS and pct < 100:
                status["stage"] = "stalled"
    common.save_status(root, slug, status)
    return status
