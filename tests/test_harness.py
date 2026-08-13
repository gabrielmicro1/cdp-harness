#!/usr/bin/env python3
"""Offline validation for cdp-harness — no Azure, no network, no git writes.

    python3 tests/test_harness.py

Copies tests/fixtures/companies/ to a temp root and exercises: report +
dashboard generation, verify-completion (fail, pass, mark-complete,
cannot-verify), the copied-forward path, status/stall transitions, launch
summary parsing, sizer summary compactness, and fleet_size.py --dry-run.
"""
from __future__ import annotations

import json
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
FIXTURES = REPO / "tests" / "fixtures" / "companies"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import phases  # noqa: E402
import reconcile  # noqa: E402

PASS = 0
checks = []


def check(name: str, cond: bool, detail: str = "") -> None:
    checks.append((name, bool(cond), detail))
    mark = "ok " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))


def run_script(script: str, *args, expect_rc=0) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                          capture_output=True, text=True)
    if expect_rc is not None:
        check(f"{script} {' '.join(map(str, args[:2]))} rc={expect_rc}",
              proc.returncode == expect_rc,
              f"rc={proc.returncode} stderr={proc.stderr[-400:]}")
    return proc


def main() -> int:
    for f in SCRIPTS.glob("*.py"):
        py_compile.compile(str(f), doraise=True)
    print("all scripts compile")

    tmp = Path(tempfile.mkdtemp(prefix="cdp-harness-test-"))
    root = tmp / "companies"
    shutil.copytree(FIXTURES, root)

    print("\n— reconcile —")
    s = reconcile.company_summary(root, "democo")
    check("pct_complete ≈ 73.3", abs(s["pct_complete"] - 73.3) < 0.1,
          str(s["pct_complete"]))
    check("delta_24h = 100 GB", s["delta_24h"] == 100_000_000_000,
          str(s["delta_24h"]))
    check("rate = 100 GB/day", abs(s["rate_bytes_per_day"] - 1e11) < 1e9)
    check("eta ~13 days out", s["eta"] is not None and s["eta"][:7] == "2026-08",
          str(s["eta"]))
    check("not stalled (grew yesterday)", s["stalled"] is False)
    flags = {r["service"]: r["flags"] for r in s["service_rows"]}
    check("code overshoot flagged", "overshoot" in flags["code"])
    check("slack record-count flagged", "record-count" in flags["slack"])
    check("zendesk 0B-has-data flagged", "zero-declared-has-data" in flags["zendesk"])
    check("hubspot declared-empty flagged", "declared-empty" in flags["hubspot"])
    check("timestamp prefix unexpected", "20260101T000000Z" in s["unexpected_sources"])
    notes = " ".join(s["notes"])
    check("timestamp-prefix note", "export" in notes and "timestamp" in notes)
    check("tar.gz caveat note", "tar.gz" in notes)
    check("BadZipFile note", "BadZipFile" in notes)
    check("no store-mode note (ratio 2.3)", "store-mode" not in notes)

    print("\n— gen_report —")
    proc = run_script("gen_report.py", "democo", "--root", root)
    report_path = Path(proc.stdout.strip())
    check("report written", report_path.exists(), proc.stdout)
    html = report_path.read_text() if report_path.exists() else ""
    for needle in ("democo", "73.3%", "over 100%", "declared 0 B, has data",
                   "record count", "not in manifest", "micro1", "svg",
                   "declared, no data yet"):
        check(f"report contains '{needle}'", needle in html)
    check("report self-contained (no external asset loads)",
          not any(m in html for m in ('src="http', "src='http", 'href="http',
                                      "url(http", "@import")))
    run_script("gen_report.py", "emptyco", "--root", root)  # no runs: still works

    print("\n— gen_dashboard —")
    dash_out = tmp / "index.html"
    proc = run_script("gen_dashboard.py", "--root", root, "--out", dash_out)
    summary = json.loads(proc.stdout)
    check("fleet pct ≈ 73.3", abs(summary["fleet_pct"] - 73.3) < 0.1,
          str(summary["fleet_pct"]))
    check("dashboard lists both companies", len(summary["companies"]) == 2)
    empty = [c for c in summary["companies"] if c["slug"] == "emptyco"][0]
    check("emptyco flagged no_vm", empty.get("no_vm") is True)
    dh = dash_out.read_text()
    check("dashboard links democo report",
          f"companies/democo/reports/{report_path.name}" in dh)
    check("dashboard has stage badge", "pushing" in dh)

    print("\n— verify_completion —")
    proc = run_script("verify_completion.py", "democo", "--root", root,
                      expect_rc=1)  # 73% → fail
    v = json.loads(proc.stdout)
    check("democo verdict fail", v["verdict"] == "fail")
    check("headline check fails", v["checks"]["headline"]["pass"] is False)
    check("hubspot in failing services",
          any(f["service"] == "hubspot" for f in v["checks"]["services"]["failing"]))
    check("overshoot needs judgment",
          any(o["service"] == "code" for o in v["needs_judgment"]["overshoot"]))
    check("record service listed",
          any(r["service"] == "slack"
              for r in v["needs_judgment"]["record_count_services"]))
    run_script("verify_completion.py", "emptyco", "--root", root, expect_rc=2)

    # a passing company, built in-place
    passco = root / "passco"
    (passco / "sizing-runs").mkdir(parents=True)
    common.write_json(passco / "config.json", {
        "slug": "passco", "subscription": "m1 corpus", "subscription_id": "x",
        "resource_group": "rg-passco", "storage_account": "stpassco",
        "container": "passco-raw",
        "vm": {"name": "verify-vm-passco", "resource_group": "rg-passco",
               "exists": True},
        "onboarded_at": "2026-08-01T00:00:00Z"})
    common.write_json(passco / "expected-data-sizes.json", {
        "slug": "passco", "manifest_total_bytes": 1_000_000_000_000,
        "services": {"gdrive": {"bytes": 1_000_000_000_000}},
        "confirmed_by_user": True, "created_at": "2026-08-01T00:00:00Z"})
    common.write_json(passco / "status.json", {
        "slug": "passco", "stage": "verifying",
        "last_run": {"timestamp": "2026-08-12T09:00:00Z", "outcome": "sized",
                     "reason": None},
        "last_change_detected_at": "2026-08-12T09:00:00Z"})
    common.write_json(passco / "sizing-runs" / "20260812T100000Z.json", {
        "slug": "passco", "timestamp": "2026-08-12T10:00:00Z", "method": "sized",
        "copied_from": None, "used_capacity_bytes": 990_000_000_000,
        "used_capacity_at": "2026-08-12T09:00:00Z", "duration_seconds": 60,
        "totals": {"blob_count": 100, "compressed_bytes": 990_000_000_000,
                   "uncompressed_bytes": 995_000_000_000, "zero_byte_blobs": 0},
        "sources": {"gdrive": {"blob_count": 100,
                               "compressed_bytes": 990_000_000_000,
                               "uncompressed_bytes": 995_000_000_000}},
        "methods": {"zip": 100}, "errors": {"total": 0, "by_type": {}},
        "notes": []})
    proc = run_script("verify_completion.py", "passco", "--root", root,
                      "--mark-complete", expect_rc=0)
    v = json.loads(proc.stdout)
    check("passco verdict pass", v["verdict"] == "pass")
    check("passco marked complete",
          common.load_status(root, "passco")["stage"] == "complete")

    print("\n— copied-forward + status transitions —")
    run_path = phases.write_copied_forward_run(root, "democo",
                                               1_564_500_000_000,
                                               "2026-08-13T08:00:00Z")
    cf = common.read_json(run_path)
    check("copied-forward method", cf["method"] == "copied-forward")
    check("copied-forward totals preserved",
          cf["totals"]["uncompressed_bytes"] == 3_665_000_000_000)
    check("copied_from set", cf["copied_from"] == "2026-08-12T09:00:00Z")
    st = phases.update_status(root, "democo", "skipped-unchanged", "test")
    check("no growth → stage still pushing (only 1 day)", st["stage"] == "pushing")
    check("last_run outcome recorded",
          st["last_run"]["outcome"] == "skipped-unchanged")
    # age the last change past the stall threshold → next no-growth run stalls
    st["last_change_detected_at"] = "2026-08-01T00:00:00Z"
    common.save_status(root, "democo", st)
    phases.write_copied_forward_run(root, "democo", 1_564_500_000_000,
                                    "2026-08-13T09:00:00Z")
    st = phases.update_status(root, "democo", "skipped-unchanged", "test")
    check("no growth ≥3 days → stalled", st["stage"] == "stalled")
    # growth resumes → back to pushing
    grown = json.loads(json.dumps(cf))
    # filename/timestamp must sort AFTER the real-clock copied-forward runs
    grown["timestamp"] = "2026-12-31T00:00:00Z"
    grown["method"] = "sized"
    grown["totals"]["uncompressed_bytes"] += 50_000_000_000
    common.write_json(root / "democo" / "sizing-runs" / "20261231T000000Z.json",
                      grown)
    st = phases.update_status(root, "democo", "sized")
    check("growth resumed → pushing", st["stage"] == "pushing")
    check("last_change_detected_at refreshed",
          st["last_change_detected_at"][:10] == common.iso_now()[:10])

    print("\n— skip-check decision (metric stubbed) —")
    cfg = common.load_config(root, "democo")
    real = phases.read_used_capacity
    try:
        phases.read_used_capacity = lambda c, dry_run=False: (
            grown["totals"]["uncompressed_bytes"], "2026-08-13T13:00:00Z")
        # latest run's used_capacity differs from stub → changed
        r = phases.skip_check(root, "democo", cfg)
        check("metric changed → no skip", r["skip"] is False)
        latest = common.latest_runs(root, "democo", 1)[0]
        phases.read_used_capacity = lambda c, dry_run=False: (
            latest["used_capacity_bytes"], "t")
        r = phases.skip_check(root, "democo", cfg)
        check("metric unchanged → skip", r["skip"] is True)
        phases.read_used_capacity = lambda c, dry_run=False: (None, None)
        r = phases.skip_check(root, "democo", cfg)
        check("no metric → size to be safe", r["skip"] is False)
    finally:
        phases.read_used_capacity = real

    print("\n— launch-output parsing + summary compactness —")
    sample = 'Enable succeeded\npid=1234\nrc=0\n{"sa":"st","container":"c","blobs":5,"comp":10,"unc":20,"zero":0,"errors":0,"err_types":{},"methods":{"zip":5},"dur_s":3,"src":{"a":[5,10,20]}}'
    parsed = phases._extract_summary_json(sample)
    check("summary parses from instance-view output",
          parsed is not None and parsed["unc"] == 20)
    check("truncated output → None",
          phases._extract_summary_json(sample[:-30]) is None)
    run = phases.summary_to_run("democo", parsed, {"metric": 1, "metric_at": "t"},
                                {}, [])
    check("summary_to_run totals", run["totals"]["uncompressed_bytes"] == 20
          and run["sources"]["a"]["blob_count"] == 5)
    big = {"sa": "storageaccount", "container": "slug-raw", "blobs": 10_900_000,
           "comp": 2**43, "unc": 2**44, "zero": 12345,
           "errors": 999, "err_types": {"BadZipFile": 900, "URLError": 99},
           "methods": {"zip": 9_000_000, "gz": 100, "stored": 1_899_900},
           "dur_s": 2700,
           "src": {f"source-name-{i}": [123456, 2**40 + i, 2**41 + i]
                   for i in range(30)}}
    size = len(json.dumps(big, separators=(",", ":")))
    check(f"30-source summary fits 4KB cap ({size}B)", size < 3500)

    print("\n— fleet_size --dry-run —")
    proc = run_script("fleet_size.py", "launch-all", "--root", root,
                      "--dry-run", "--slugs", "democo", "emptyco",
                      expect_rc=0)
    out = proc.stdout
    check("dry-run prints run-command create",
          "az vm run-command create" in out and "--async-execution" in out)
    check("dry-run prints SAS mint", "generate-sas" in out and "rl" in out)
    check("dry-run prints metrics read", "UsedCapacity" in out)
    check("dry-run prints firewall conditional", "network-rule add" in out)
    outcomes = json.loads(out[out.rindex('{\n  "started_at"'):])
    check("emptyco outcome no-vm",
          outcomes["outcomes"]["emptyco"]["outcome"] == "no-vm")
    check("democo launched (no outcome yet)",
          outcomes["outcomes"]["democo"]["outcome"] in (None, "failed") and
          outcomes["outcomes"]["democo"].get("exec_state") is None)
    state = phases.load_state(root)
    check("state file tracks democo launch",
          state["companies"]["democo"]["phase"] == "launched")
    check("no real az was needed (dry-run)", True)

    shutil.rmtree(tmp)
    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        for name, _, detail in failed:
            print(f"FAILED: {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
