"""Shared sizing phases. fleet_size.py and size_company.py are thin CLIs over
these functions — the fleet is a loop over the same phases a single company
uses (single company = fleet of one). Never fork this logic into a CLI.

Sizing runs LOCALLY on this machine — no VMs (the sizer reads only blob-list
pages, zip central directories, and gzip trailers: kilobytes per blob, so
nothing large flows). Phases (see CLAUDE.md "Azure operational model"):

  0. skip_check      — UsedCapacity metric vs last run; unchanged → copied-forward
  1. launch          — IP firewall rule if needed, mint rl SAS, seed the sizer's
                       cache from the company's blob-index.tsv.gz + any crashed
                       -run progress tsv (unless use_cache=False / --no-cache),
                       pass declared service names, start the sizer as a
                       DETACHED local process (survives the harness dying; its
                       work files are the manual-rescue path)
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


# Bytes of account Ingress below which we treat the window as "no writes".
# A genuinely idle account still logs a few KB of transaction ingress
# (monterey-financial, 19 days at zero blobs: 35 KB total over 49 h, max 8 KB
# in any hour). Active accounts are 5-6 orders of magnitude above this
# (bacancy 2.42 GB/25 h, helpsy 12.4 TB/49 h), so 1 MB separates them with
# enormous margin.
INGRESS_FLOOR_BYTES = 1_000_000


def read_ingress(cfg: dict, since: str, dry_run: bool = False):
    """Total bytes WRITTEN to the account since `since` (ISO-8601 Z) → int,
    or None when the metric can't be read.

    Ingress is a TRANSACTION metric: PT1M granularity, emitted as writes
    happen. UsedCapacity is a CAPACITY SNAPSHOT that ARM re-emits every hour
    carrying the last known value — so it can be many hours stale while its
    timestamp reads as current. See skip_check for why that distinction is
    load-bearing.

    Both --start-time AND --end-time are passed: az returns a single bucket
    for the whole window when --end-time is omitted, which would silently
    collapse the sum."""
    end = common.iso(common.utc_now())
    data = common.az_json([
        "monitor", "metrics", "list",
        "--resource", sa_resource_id(cfg),
        "--metric", "Ingress", "--interval", "PT1H",
        "--start-time", since, "--end-time", end,
        "--aggregation", "Total",
    ], dry_run=dry_run)
    if data is None:
        return None
    try:
        points = data["value"][0]["timeseries"][0]["data"]
    except (KeyError, IndexError):
        return None
    return int(sum(pt.get("total") or 0 for pt in points))


def _last_measured_run(root: Path, slug: str):
    """The most recent run that actually LISTED the container (method
    "sized"), not one that copied numbers forward. That run's timestamp is
    the last moment we truly know the container's contents, so it is the
    correct start of the ingress window: a chain of copied-forward runs must
    not keep sliding the window forward past writes nobody ever looked at."""
    for run in common.latest_runs(root, slug, 50):
        if run.get("method") == "sized" and "totals" in run:
            return run
    return None


def skip_check(root: Path, slug: str, cfg: dict, dry_run: bool = False,
               force: bool = False) -> dict:
    """Decide launch vs copy-forward. Unchanged metric AND a previous run with
    totals → skip. Any doubt (no metric, no prior run) → launch (safe side).
    force never skips: for re-sizing an UNCHANGED container under different
    sizer settings (e.g. gz exact-streaming knobs), where the metric is
    unchanged by definition but the numbers we want are not."""
    metric, metric_at = read_used_capacity(cfg, dry_run=dry_run)
    if force:
        return {"skip": False, "metric": metric, "metric_at": metric_at,
                "reason": "forced re-size (skip check bypassed)"}
    prev = common.latest_runs(root, slug, 1)
    prev = prev[0] if prev else None
    if metric is None:
        return {"skip": False, "metric": None, "metric_at": None,
                "reason": "no UsedCapacity datapoint — sizing to be safe"}
    if prev is None or "totals" not in prev:
        return {"skip": False, "metric": metric, "metric_at": metric_at,
                "reason": "no previous sizing run"}
    if prev.get("used_capacity_bytes") != metric:
        return {"skip": False, "metric": metric, "metric_at": metric_at,
                "reason": f"UsedCapacity changed {prev.get('used_capacity_bytes')} → {metric}"}

    # The metric is unchanged — but "unchanged" is exactly what a FROZEN
    # metric also looks like, and a frozen one gets stamped into the run file
    # and then matches itself indefinitely. oneorb, 2026-09-01: UsedCapacity
    # froze at 236 MB at 16:00Z and still read 236 MB at 00:00Z while 678 GB
    # sat in the container; the 670 GB S3 ingest landed at ~19:53Z inside that
    # blind window and the daily brief published 2.1% instead of 196.5%.
    # metric_at is no defence: ARM re-emits the stale value hourly under a
    # current timestamp. So two independent guards must also pass.

    # Guard 1 — hard invariant. UsedCapacity is ACCOUNT-level and the
    # container is a subset of the account, so the metric can never honestly
    # sit below the compressed bytes we ourselves listed. If it does, it is
    # stale (or data was deleted) and either way we must look.
    measured = _last_measured_run(root, slug)
    prev_compressed = (measured or {}).get("totals", {}).get("compressed_bytes")
    if prev_compressed and metric < prev_compressed:
        return {"skip": False, "metric": metric, "metric_at": metric_at,
                "reason": (f"stale UsedCapacity: metric {metric} B is below the "
                           f"{prev_compressed} B we listed on "
                           f"{(measured or {}).get('timestamp')} — sizing")}

    # Guard 2 — did anything actually get WRITTEN since we last looked?
    # Ingress answers that directly and in near-real-time, where the capacity
    # snapshot only answers it eventually.
    since = (measured or prev).get("timestamp")
    ingress = read_ingress(cfg, since, dry_run=dry_run) if since else None
    if since is None or ingress is None:
        return {"skip": False, "metric": metric, "metric_at": metric_at,
                "reason": "ingress unreadable — sizing to be safe"}
    if ingress > INGRESS_FLOOR_BYTES:
        return {"skip": False, "metric": metric, "metric_at": metric_at,
                "reason": (f"{ingress} bytes written since {since} despite "
                           f"UsedCapacity unchanged — sizing")}

    return {"skip": True, "metric": metric, "metric_at": metric_at,
            "reason": (f"UsedCapacity unchanged ({metric} bytes) and "
                       f"{ingress} bytes written since {since}")}


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
        "cache": None,
        "gz": prev.get("gz"),
        "detected_services": prev.get("detected_services", {}),
        "sources_l2": prev.get("sources_l2", {}),
        "verification": prev.get("verification"),  # unchanged container keeps
                                                   # its deep-verify coverage
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


def mint_sas(cfg: dict, dry_run: bool = False, days: int = 1) -> str:
    """Account SAS, rl only, https-only. 1-day expiry by default (sizing
    policy); deep_verify.py may pass days=2 for a monster-container VM run.
    Read-only is policy — we never write to client storage (see CLAUDE.md
    principle 3)."""
    from datetime import timedelta
    expiry = (common.utc_now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%MZ")
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


def launch(root: Path, slug: str, cfg: dict, dry_run: bool = False,
          use_cache: bool = True, skip_prefixes: list[str] | None = None) -> dict:
    """Start the sizer as a detached local process (nohup-equivalent: new
    session, stdin closed, output to <tag>.stdout). It keeps running if the
    harness/agent dies — the work files are the rescue path. When use_cache,
    seeds the sizer from the company's blob-index.tsv.gz (prior harvest) and
    any crashed-run progress tsv, and passes the manifest's declared service
    names for detection. Returns the per-company in-flight state dict."""
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
    seed = wd / f"{tag}.seed.tsv"
    stale_tsv = wd / f"{tag}.sizes.tsv"
    if use_cache and stale_tsv.exists():
        os.replace(stale_tsv, seed)  # crashed-run progress becomes a seed
    for stale in wd.glob(f"{tag}.*"):  # a stale .done would fake completion
        if stale != seed:
            stale.unlink()
    env = os.environ.copy()
    env.pop("CACHE_FILE", None)   # never inherit these from our own process
    env.pop("SEED_TSV", None)
    env.pop("EXPECTED_SERVICES", None)
    env.update({"SA": cfg["storage_account"], "CONTAINER": cfg["container"],
                "TAG": tag, "OUT_DIR": str(wd), "AZURE_STORAGE_SAS": sas})
    index = common.company_dir(root, slug) / "blob-index.tsv.gz"
    if use_cache and index.exists():
        env["CACHE_FILE"] = str(index)
    if use_cache and seed.exists():
        env["SEED_TSV"] = str(seed)
    expected = common.load_expected(root, slug)
    services = ",".join((expected or {}).get("services", {}).keys())
    if services:
        env["EXPECTED_SERVICES"] = services
    if skip_prefixes:
        env["SKIP_PREFIXES"] = ",".join(skip_prefixes)
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

def carry_forward_skipped(run: dict, summary: dict, prev: dict | None) -> dict:
    """Fold prefixes the sizer was told to SKIP back in, from the previous
    run's numbers, and say so in notes.

    A skipped prefix was not listed at all this run, so its blobs are absent
    from summary["src"] and from the totals. The caller asserted it is
    unchanged (size_company.py --skip-prefix), which is the per-prefix
    analogue of the container-level UsedCapacity copy-forward. Every carried
    prefix gets a note naming the run it came from — the omission is never
    silent, and a prefix with no previous numbers is reported as a gap rather
    than quietly dropped."""
    skipped = summary.get("skipped_prefixes") or []
    if not skipped:
        return run
    prev_sources = (prev or {}).get("sources", {})
    for name in skipped:
        src = prev_sources.get(name)
        if not src:
            run["notes"].append(
                f"--skip-prefix {name}: NOT re-measured this run and no "
                f"previous numbers exist to carry forward — this prefix is "
                f"MISSING from the totals below.")
            continue
        run["sources"][name] = dict(src)
        run["totals"]["blob_count"] += src["blob_count"]
        run["totals"]["compressed_bytes"] += src["compressed_bytes"]
        run["totals"]["uncompressed_bytes"] += src["uncompressed_bytes"]
        run["notes"].append(
            f"--skip-prefix {name}: NOT re-measured this run; "
            f"{src['blob_count']:,} blobs / "
            f"{src['uncompressed_bytes'] / 1e12:.2f} TB carried forward from "
            f"run {(prev or {}).get('timestamp')}. detected_services, "
            f"sources_l2, methods and the blob-index cache cover only the "
            f"prefixes actually scanned.")
    run["skipped_prefixes"] = list(skipped)
    return run


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
        "cache": summary.get("cache"),
        "gz": summary.get("gz"),
        "detected_services": summary.get("detected_services", {}),
        "sources_l2": summary.get("sources_l2", {}),
        "verification": summary.get("verification"),  # deep-verify coverage
                                                      # block; None for
                                                      # shallow/old summaries
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
    prev_runs = common.latest_runs(root, slug, 1)
    run = summary_to_run(slug, summary, skip_info, [])
    run = carry_forward_skipped(run, summary, prev_runs[0] if prev_runs else None)
    path = _run_path(root, slug)
    common.write_json(path, run)
    idx = wd / f"{tag}.index.tsv.gz"
    if idx.exists():  # per-blob detail survives harvest as the company's cache
        os.replace(idx, common.company_dir(root, slug) / "blob-index.tsv.gz")
    cleanup(root, slug, cfg, comp_state, dry_run=dry_run)
    return path


def cleanup(root: Path, slug: str, cfg: dict, comp_state: dict,
            dry_run: bool = False) -> None:
    """Remove work files; remove the IP firewall rule only if WE added it.
    The per-blob sizes.tsv is deleted too — its detail lives on in
    companies/<slug>/blob-index.tsv.gz (moved there by harvest_one), which
    seeds the next run's cache; re-size with --no-cache to ignore it."""
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
