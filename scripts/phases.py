"""Shared sizing phases. fleet_size.py and size_company.py are thin CLIs over
these functions — the fleet is a loop over the same phases a single company
uses (single company = fleet of one). Never fork this logic into a CLI.

Sizing runs LOCALLY on this machine — no VMs (the sizer reads only blob-list
pages, zip central directories, and gzip trailers: kilobytes per blob, so
nothing large flows). Phases (see CLAUDE.md "Azure operational model"):

  0. skip_check      — UsedCapacity metric vs last run; unchanged → copied-forward
  1. launch          — IP firewall rule if needed, mint rl SAS, start the sizer
                       as a DETACHED local process (survives the harness dying;
                       its work files are the manual-rescue path)
  2. poll            — .done file → Succeeded; else pid alive → Running
  3. harvest         — read <tag>.summary.json, write sizing-run file, update
                       status, cleanup (work files + only-ours IP rule)

Work files live in <root>/.sizer-work/<slug>-sizer.* (gitignored). In-flight
state persists in <root>/.fleet-state.json so polling resumes across separate
CLI invocations / agent turns.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import common
from common import HarnessError

TERMINAL_STATES = {"Succeeded", "Failed"}
PUBLIC_IP_SERVICES = ("https://api.ipify.org", "https://checkip.amazonaws.com")


def state_path(root: Path) -> Path:
    return root / ".fleet-state.json"


def load_state(root: Path) -> dict:
    p = state_path(root)
    if p.exists():
        return common.read_json(p)
    return {"started_at": None, "companies": {}}


def save_state(root: Path, state: dict) -> None:
    common.write_json(state_path(root), state)


def tag_for(slug: str) -> str:
    return f"{slug}-sizer"


def work_dir(root: Path) -> Path:
    return root / ".sizer-work"


def _run_path(root: Path, slug: str) -> Path:
    """Sizing-run path for now; bumps by a second on collision so a run can
    never silently overwrite another (append-only audit trail)."""
    from datetime import timedelta
    ts = common.utc_now()
    d = common.company_dir(root, slug) / "sizing-runs"
    while (d / f"{common.ts_basic(ts)}.json").exists():
        ts += timedelta(seconds=1)
    return d / f"{common.ts_basic(ts)}.json"


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
    """New run file that copies the previous run's numbers without a launch."""
    prev = common.latest_runs(root, slug, 1)
    if not prev:
        raise HarnessError("copied-forward requires a previous sizing run")
    prev = prev[0]
    path = _run_path(root, slug)
    run = {
        "slug": slug,
        "timestamp": common.iso(common.utc_now()),
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
    common.write_json(path, run)
    return path


# ── Phase 1: launch ──────────────────────────────────────────────────────────

def get_public_ip() -> str:
    for url in PUBLIC_IP_SERVICES:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                ip = r.read().decode().strip()
                if ip:
                    return ip
        except Exception:  # noqa: BLE001 — try the next service
            continue
    raise HarnessError("could not determine this machine's public IP (offline?)")


def ip_rule_ensure(cfg: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Allow this machine's public IP on the SA if (and only if) it isn't
    already reachable. Returns (we_added, ip). NEVER touches pre-existing
    rules — the client's own IPs in the allowlist are their push path."""
    if dry_run:
        print("DRY-RUN: az storage account show -n "
              f"{cfg['storage_account']} -g {cfg['resource_group']} "
              "--query networkRuleSet   # then, only if defaultAction=Deny and "
              "our public IP is missing:")
        print(f"DRY-RUN: az storage account network-rule add -g "
              f"{cfg['resource_group']} --account-name {cfg['storage_account']} "
              f"--ip-address <public-ip>; sleep 60  # propagation")
        return False, "<public-ip>"
    ip = get_public_ip()
    rules = common.az_json(["storage", "account", "show",
                            "-n", cfg["storage_account"],
                            "-g", cfg["resource_group"],
                            "--query", "networkRuleSet"]) or {}
    if rules.get("defaultAction") == "Allow":
        return False, ip
    existing = {r.get("ipAddressOrRange") for r in rules.get("ipRules") or []}
    if ip in existing:
        return False, ip  # pre-existing — do NOT remove in cleanup
    common.run_az(["storage", "account", "network-rule", "add",
                   "-g", cfg["resource_group"], "--account-name",
                   cfg["storage_account"], "--ip-address", ip, "-o", "none"])
    time.sleep(60)  # propagation — a 403 right after adding = not propagated yet
    return True, ip


def ip_rule_remove_if_ours(cfg: dict, ip: str, we_added: bool,
                           dry_run: bool = False) -> None:
    if not we_added:
        return  # pre-existing rule (or defaultAction=Allow) — never delete
    common.run_az(["storage", "account", "network-rule", "remove",
                   "-g", cfg["resource_group"], "--account-name",
                   cfg["storage_account"], "--ip-address", ip, "-o", "none"],
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


def _sizer_cmd() -> list[str]:
    """The sizer invocation. caffeinate -i (macOS) holds off idle sleep while
    a sizer runs — note it does NOT survive a closed lid; keep the laptop open
    for monster containers. CDP_SIZER_SCRIPT overrides the script (tests)."""
    sizer = os.environ.get("CDP_SIZER_SCRIPT",
                           str(common.REPO_ROOT / "scripts" / "corpus_sizer_rest.py"))
    cmd = [sys.executable, sizer]
    if shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd
    return cmd


def launch(root: Path, slug: str, cfg: dict, dry_run: bool = False) -> dict:
    """Start the sizer as a detached local process (nohup-equivalent: new
    session, stdin closed, output to <tag>.stdout). It keeps running if the
    harness/agent dies — the work files are the rescue path. Returns the
    per-company in-flight state dict."""
    we_added_ip, ip = ip_rule_ensure(cfg, dry_run=dry_run)
    sas = mint_sas(cfg, dry_run=dry_run)
    tag = tag_for(slug)
    wd = work_dir(root)
    cmd = _sizer_cmd()
    if dry_run:
        print(f"DRY-RUN: SA={cfg['storage_account']} "
              f"CONTAINER={cfg['container']} TAG={tag} OUT_DIR={wd} "
              f"AZURE_STORAGE_SAS=<sas> {' '.join(cmd)} "
              f"# detached, output → {wd}/{tag}.*")
        return {"phase": "launched", "tag": tag, "pid": None,
                "we_added_ip": we_added_ip, "ip": ip,
                "launched_at": common.iso_now(), "outcome": None, "reason": None}
    wd.mkdir(parents=True, exist_ok=True)
    for stale in wd.glob(f"{tag}.*"):  # a stale .done would fake completion
        stale.unlink()
    env = os.environ.copy()
    env.update({"SA": cfg["storage_account"], "CONTAINER": cfg["container"],
                "TAG": tag, "OUT_DIR": str(wd), "AZURE_STORAGE_SAS": sas})
    with open(wd / f"{tag}.stdout", "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True, env=env,
                                cwd=str(common.REPO_ROOT))
    return {"phase": "launched", "tag": tag, "pid": proc.pid,
            "we_added_ip": we_added_ip, "ip": ip,
            "launched_at": common.iso_now(), "outcome": None, "reason": None}


# ── Phase 2: poll ────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def poll_one(root: Path, slug: str, comp_state: dict,
             dry_run: bool = False) -> dict:
    """Local check, instant and side-effect-free: .done file wins (written on
    clean completion), else a live pid means still running."""
    if dry_run:
        return {"state": "Unknown", "err": ""}
    tag = comp_state.get("tag") or tag_for(slug)
    wd = work_dir(root)
    if (wd / f"{tag}.done").exists():
        return {"state": "Succeeded", "err": ""}
    pid = comp_state.get("pid")
    if pid and _pid_alive(pid):
        return {"state": "Running", "err": ""}
    tail = ""
    log = wd / f"{tag}.stdout"
    if log.exists():
        tail = log.read_text()[-400:]
    return {"state": "Failed",
            "err": f"sizer process gone without {tag}.done; stdout tail: {tail}"}


# ── Phase 3: harvest ─────────────────────────────────────────────────────────

def summary_to_run(slug: str, summary: dict, skip_info: dict,
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
                dry_run: bool = False) -> Path:
    """Read the finished run's summary.json, write the sizing-run file, clean
    up. Raises HarnessError if there is no summary to harvest."""
    tag = comp_state.get("tag") or tag_for(slug)
    wd = work_dir(root)
    summary_path = wd / f"{tag}.summary.json"
    if dry_run:
        print(f"DRY-RUN: harvest would read {summary_path}, write a sizing-run "
              f"file, and clean up")
        raise HarnessError("dry-run: nothing to harvest")
    if not summary_path.exists():
        raise HarnessError(
            f"no summary at {summary_path} — check {tag}.log / {tag}.stdout "
            f"in {wd} (a 403 in the log = firewall propagation, not the SAS)")
    summary = common.read_json(summary_path)
    skip_info = {"metric": comp_state.get("metric"),
                 "metric_at": comp_state.get("metric_at")}
    run = summary_to_run(slug, summary, skip_info, [])
    path = _run_path(root, slug)
    common.write_json(path, run)
    cleanup(root, slug, cfg, comp_state, dry_run=dry_run)
    return path


def cleanup(root: Path, slug: str, cfg: dict, comp_state: dict,
            dry_run: bool = False) -> None:
    """Remove work files; remove the IP firewall rule only if WE added it.
    The per-blob sizes.tsv is deleted too — the sizing-run JSON keeps
    everything the harness needs; re-size if per-blob detail is wanted."""
    if not dry_run:
        for f in work_dir(root).glob(f"{comp_state.get('tag') or tag_for(slug)}.*"):
            f.unlink()
    ip_rule_remove_if_ours(cfg, comp_state.get("ip", ""),
                           comp_state.get("we_added_ip", False),
                           dry_run=dry_run)
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
