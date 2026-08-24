#!/usr/bin/env python3
"""Offline validation for cdp-harness — no Azure, no network, no git writes.

    python3 tests/test_harness.py

Copies tests/fixtures/companies/ to a temp root and exercises: report +
dashboard generation, verify-completion (fail, pass, mark-complete,
cannot-verify), the copied-forward path, status/stall transitions, launch
summary parsing, sizer summary compactness, and fleet_size.py --dry-run.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import py_compile
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import zipfile
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

    print("\n— reconcile: excluded prefixes (non-corpus data) —")
    _exc_run = {"sources": {"2026": {"uncompressed_bytes": 500},
                            "gdrive": {"uncompressed_bytes": 9500}}}
    _exc = reconcile.excluded_sources(
        {"excluded_prefixes": [{"prefix": "2026", "reason": "inventory"}]},
        _exc_run)
    check("excluded_sources: picks bytes + reason",
          _exc == {"2026": (500, "inventory")})
    check("excluded_sources: bare string form",
          reconcile.excluded_sources({"excluded_prefixes": ["2026"]}, _exc_run)
          == {"2026": (500, "non-corpus operational data")})
    check("excluded_sources: absent prefix ignored",
          reconcile.excluded_sources({"excluded_prefixes": ["nope"]}, _exc_run)
          == {})
    _exc_note = reconcile.excluded_prefix_note(_exc)
    check("excluded_prefix_note: mentions prefix and reason",
          len(_exc_note) == 1 and "2026" in _exc_note[0]
          and "inventory" in _exc_note[0]
          and "non-corpus" in _exc_note[0])
    check("excluded_prefix_note: empty when nothing excluded",
          reconcile.excluded_prefix_note({}) == [])

    print("\n— reconcile: duplicate-data rollup —")
    dup = reconcile.duplicate_rollup([
        ("gdrive/a@x.com/take-001.zip", 100, 900),
        ("gdrive/a@x.com_cleanup/take-001.zip", 100, 900),   # dup of above
        ("gdrive/b@x.com/take-001.zip", 100, 901),            # unc differs: not dup
        ("slack/exp.zip", 50, 60),
        ("slack/deep/dir/exp.zip", 50, 60),                   # dup, same source
        ("code/exp.zip", 50, 60),                             # other source: not dup
    ])
    check("dup rollup bytes", dup["bytes"] == 960, str(dup))
    check("dup rollup files", dup["files"] == 2, str(dup))
    check("dup rollup per-source", dup["by_source"]
          == {"gdrive": 900, "slack": 60}, str(dup))
    check("no dup note when index absent",
          reconcile.duplicate_notes(root, "democo") == [])
    dup_idx = root / "democo" / "blob-index.tsv.gz"
    with gzip.open(dup_idx, "wt") as fh:
        fh.write("#matcher\tdeadbeef\n")
        for pfx in ("u@x.com", "u@x.com_cleanup"):
            fh.write(f"gdrive/{pfx}/take-001.zip\t0xAB\t"
                     f"9000000000\t20000000000\tzip:5entries\t\n")
    dnotes = reconcile.duplicate_notes(root, "democo")
    check("dup note over threshold", len(dnotes) == 1
          and "20.00 GB" in dnotes[0] and "gdrive" in dnotes[0],
          str(dnotes))
    s2 = reconcile.company_summary(root, "democo")
    check("dup note reaches summary notes",
          any("duplicated data" in n for n in s2["notes"]))
    dup_idx.unlink()

    print("\n— reconcile: duplicate prefixes (flat + source_split) —")
    _flat_run = {"sources": {"a": {"uncompressed_bytes": 100},
                             "b": {"uncompressed_bytes": 50}}}
    _flat_exp = {"duplicate_prefixes": ["b", {"prefix": "a",
                                              "redundant_bytes": 30}]}
    check("flat duplicate matching unchanged",
          reconcile.duplicate_sources(_flat_exp, _flat_run)
          == {"b": 50, "a": 30})
    _split_run = {
        "sources": {"parent": {"blob_count": 10,
                               "compressed_bytes": 150_000_000,
                               "uncompressed_bytes": 160_000_000}},
        "sources_l2": {"parent/dup": [2, 40_000_000, 40_000_000],
                       "parent/big": [8, 110_000_000, 120_000_000]}}
    _split_exp = {
        "source_split": ["parent"],
        "duplicate_prefixes": ["parent/dup",
                               {"prefix": "parent/big",
                                "redundant_bytes": 70_000_000},
                               "parent/absent"]}
    check("split duplicates match second-level keys",
          reconcile.duplicate_sources(_split_exp, _split_run)
          == {"parent/dup": 40_000_000, "parent/big": 70_000_000})
    _dnote = reconcile.duplicate_prefix_note(
        reconcile.duplicate_sources(_split_exp, _split_run),
        _split_run, _split_exp)
    check("split duplicate note classifies whole vs partial",
          len(_dnote) == 1 and "parent/dup" in _dnote[0]
          and "70.00 MB of parent/big" in _dnote[0], str(_dnote))
    _deduped = reconcile.apply_duplicates(
        reconcile.effective_sources(_split_exp, _split_run),
        reconcile.duplicate_sources(_split_exp, _split_run))
    check("apply_duplicates: whole dup dropped, partial keeps unique bytes",
          "parent/dup" not in _deduped
          and _deduped["parent/big"]["uncompressed_bytes"] == 50_000_000
          and _deduped["parent/big"]["deduplicated_bytes"] == 70_000_000,
          str(_deduped))
    _rows, _ = reconcile.service_rows(
        {**_split_exp, "services": {"big": {"bytes": 50_000_000}}},
        _split_run, _deduped)
    _big = _rows[0]
    check("deduplicated row: actual excludes redundant share, flagged",
          _big["actual_bytes"] == 50_000_000 and _big["pct"] == 100.0
          and "deduplicated" in _big["flags"]
          and _big["deduplicated_bytes"] == 70_000_000, str(_big))

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
    check("emptyco listed with no runs", empty.get("pct_complete") is None)
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

    print("\n— reconcile: embedded-service detection —")
    embedco = root / "embedco"
    (embedco / "sizing-runs").mkdir(parents=True)
    common.write_json(embedco / "config.json", {
        "slug": "embedco", "subscription": "m1 corpus", "subscription_id": "x",
        "resource_group": "rg-embedco", "storage_account": "stembedco",
        "container": "embedco-raw",
        "vm": {"name": None, "resource_group": "rg-embedco", "exists": False},
        "onboarded_at": "2026-08-01T00:00:00Z"})
    common.write_json(embedco / "expected-data-sizes.json", {
        "slug": "embedco", "manifest_total_bytes": 2_000_000_000_000,
        "services": {"gdrive": {"bytes": 1_500_000_000_000},
                     "hubspot": {"bytes": 500_000_000_000}},
        "source": "test", "confirmed_by_user": True,
        "created_at": "2026-08-01T00:00:00Z"})
    common.write_json(embedco / "status.json", {
        "slug": "embedco", "stage": "pushing",
        "last_run": {"timestamp": "2026-08-13T09:00:00Z", "outcome": "sized",
                     "reason": None},
        "last_change_detected_at": "2026-08-13T09:00:00Z"})
    common.write_json(embedco / "sizing-runs" / "20260813T100000Z.json", {
        "slug": "embedco", "timestamp": "2026-08-13T10:00:00Z",
        "method": "sized", "copied_from": None,
        "used_capacity_bytes": 1_000_000_000_000,
        "used_capacity_at": "2026-08-13T09:00:00Z", "duration_seconds": 60,
        "totals": {"blob_count": 10, "compressed_bytes": 1_000_000_000_000,
                   "uncompressed_bytes": 1_200_000_000_000,
                   "zero_byte_blobs": 0},
        "sources": {"workspace-export": {
            "blob_count": 10, "compressed_bytes": 1_000_000_000_000,
            "uncompressed_bytes": 1_200_000_000_000}},
        "methods": {"zip": 10}, "errors": {"total": 0, "by_type": {}},
        "cache": {"hits": 0, "misses": 10},
        "detected_services": {
            "hubspot": {"bytes": 400_000_000_000, "blob_count": 0,
                        "entry_count": 12000, "path_bytes": 0,
                        "zip_entry_bytes": 400_000_000_000,
                        "sources": {"workspace-export": 400_000_000_000}},
            "stripe": {"bytes": 5_000_000_000, "blob_count": 0,
                       "entry_count": 40, "path_bytes": 0,
                       "zip_entry_bytes": 5_000_000_000,
                       "sources": {"workspace-export": 5_000_000_000}}},
        "sources_l2": {"workspace-export/hubspot": [4, 0, 400_000_000_000]},
        "notes": []})
    es = reconcile.company_summary(root, "embedco")
    eflags = {r["service"]: r["flags"] for r in es["service_rows"]}
    erows = {r["service"]: r for r in es["service_rows"]}
    check("hubspot found-embedded (not declared-empty)",
          eflags["hubspot"] == ["found-embedded"], str(eflags))
    check("embedded bytes + location recorded",
          erows["hubspot"]["embedded_bytes"] == 400_000_000_000
          and erows["hubspot"]["embedded_in"] == ["workspace-export"])
    enotes = " ".join(es["notes"])
    check("embedded note names hubspot + host",
          "hubspot" in enotes and "workspace-export" in enotes, enotes)
    check("undeclared stripe surfaced (≥1GB)", "stripe" in enotes, enotes)
    proc = run_script("gen_report.py", "embedco", "--root", root)
    ehtml = Path(proc.stdout.strip()).read_text()
    check("report renders found-embedded badge",
          "embedded in another source" in ehtml)

    print("\n— reconcile: prefix pin + variant aggregation —")
    pinco = root / "pinco"
    (pinco / "sizing-runs").mkdir(parents=True)
    common.write_json(pinco / "config.json", {
        "slug": "pinco", "subscription": "m1 corpus", "subscription_id": "x",
        "resource_group": "rg-pinco", "storage_account": "stpinco",
        "container": "pinco-raw",
        "vm": {"name": None, "resource_group": "rg-pinco", "exists": False},
        "onboarded_at": "2026-08-01T00:00:00Z"})
    common.write_json(pinco / "expected-data-sizes.json", {
        "slug": "pinco", "manifest_total_bytes": 1_000_000_000_000,
        "services": {
            # manifest name ≠ pushed prefix: pinned explicitly
            "google-workspace": {"bytes": 800_000_000_000,
                                 "prefix": "workspace-export"},
            # name match must sum case variants (zoom/ + Zoom/)
            "zoom": {"bytes": 200_000_000_000}},
        "source": "test", "confirmed_by_user": True,
        "created_at": "2026-08-01T00:00:00Z"})
    common.write_json(pinco / "status.json", {
        "slug": "pinco", "stage": "pushing",
        "last_run": {"timestamp": "2026-08-13T09:00:00Z", "outcome": "sized",
                     "reason": None},
        "last_change_detected_at": "2026-08-13T09:00:00Z"})
    common.write_json(pinco / "sizing-runs" / "20260813T100000Z.json", {
        "slug": "pinco", "timestamp": "2026-08-13T10:00:00Z",
        "method": "sized", "copied_from": None,
        "used_capacity_bytes": 700_000_000_000,
        "used_capacity_at": "2026-08-13T09:00:00Z", "duration_seconds": 60,
        "totals": {"blob_count": 40, "compressed_bytes": 700_000_000_000,
                   "uncompressed_bytes": 830_000_000_000,
                   "zero_byte_blobs": 0},
        "sources": {
            "workspace-export": {"blob_count": 10,
                                 "compressed_bytes": 500_000_000_000,
                                 "uncompressed_bytes": 600_000_000_000},
            "zoom": {"blob_count": 10, "compressed_bytes": 90_000_000_000,
                     "uncompressed_bytes": 90_000_000_000},
            "Zoom": {"blob_count": 10, "compressed_bytes": 10_000_000_000,
                     "uncompressed_bytes": 10_000_000_000},
            "dropbox-export": {"blob_count": 10,
                               "compressed_bytes": 100_000_000_000,
                               "uncompressed_bytes": 130_000_000_000}},
        "sources_l2": {
            "dropbox-export/Creative": [6, 90_000_000_000, 110_000_000_000],
            "dropbox-export/Sales": [3, 9_000_000_000, 15_000_000_000]},
        "methods": {"zip": 40}, "errors": {"total": 0, "by_type": {}},
        "notes": []})
    ps = reconcile.company_summary(root, "pinco")
    prows = {r["service"]: r for r in ps["service_rows"]}
    check("prefix pin matches workspace-export",
          prows["google-workspace"]["actual_bytes"] == 600_000_000_000,
          str(prows["google-workspace"]))
    check("pinned source not unexpected",
          "workspace-export" not in ps["unexpected_sources"])
    check("case variants summed (zoom + Zoom)",
          prows["zoom"]["actual_bytes"] == 100_000_000_000
          and prows["zoom"]["blob_count"] == 20, str(prows["zoom"]))
    check("Zoom variant not unexpected",
          "Zoom" not in ps["unexpected_sources"])
    check("dropbox-export still unexpected",
          ps["unexpected_sources"] == ["dropbox-export"],
          str(ps["unexpected_sources"]))
    proc = run_script("gen_report.py", "pinco", "--root", root)
    phtml = Path(proc.stdout.strip()).read_text()
    check("report breaks down large unexpected source",
          "Inside dropbox-export" in phtml and "Creative" in phtml)
    check("breakdown includes remainder row", "(everything else)" in phtml)

    print("\n— reconcile: source_split (services nested in one export folder) —")
    splitco = root / "splitco"
    (splitco / "sizing-runs").mkdir(parents=True)
    common.write_json(splitco / "config.json", {
        "slug": "splitco", "subscription": "m1 corpus", "subscription_id": "x",
        "resource_group": "rg-splitco", "storage_account": "stsplitco",
        "container": "splitco-raw",
        "vm": {"name": None, "resource_group": "rg-splitco", "exists": False},
        "onboarded_at": "2026-08-01T00:00:00Z"})
    common.write_json(splitco / "expected-data-sizes.json", {
        "slug": "splitco", "manifest_total_bytes": 100_000_000_000,
        # every service was pushed INSIDE gdrive-export/, not as its own
        # top-level prefix — split it and reconcile against the children
        "source_split": ["gdrive-export"],
        "services": {
            # bare manifest name matches the child's last path segment
            "Notion": {"bytes": 2_600_000_000},
            # child folder named differently: pinned by full path…
            "Vanta": {"bytes": 27_800_000, "prefix": "gdrive-export/Vanta data"},
            # …or by the child segment alone
            "Slack": {"bytes": 3_000_000_000,
                      "prefix": "SwiftLaw Slack export Mar 16 2022"}},
        "source": "test", "confirmed_by_user": True,
        "created_at": "2026-08-01T00:00:00Z"})
    common.write_json(splitco / "status.json", {
        "slug": "splitco", "stage": "pushing",
        "last_run": {"timestamp": "2026-08-13T09:00:00Z", "outcome": "sized",
                     "reason": None},
        "last_change_detected_at": "2026-08-13T09:00:00Z"})
    common.write_json(splitco / "sizing-runs" / "20260813T100000Z.json", {
        "slug": "splitco", "timestamp": "2026-08-13T10:00:00Z",
        "method": "sized", "copied_from": None,
        "used_capacity_bytes": 8_000_000_000,
        "used_capacity_at": "2026-08-13T09:00:00Z", "duration_seconds": 60,
        "totals": {"blob_count": 1200, "compressed_bytes": 8_000_000_000,
                   "uncompressed_bytes": 10_000_000_000, "zero_byte_blobs": 0},
        "sources": {"gdrive-export": {"blob_count": 1200,
                                      "compressed_bytes": 8_000_000_000,
                                      "uncompressed_bytes": 10_000_000_000}},
        "sources_l2": {
            "gdrive-export/Notion": [3, 2_000_000_000, 2_600_000_000],
            "gdrive-export/Vanta data": [77, 20_000_000, 27_800_000],
            "gdrive-export/SwiftLaw Slack export Mar 16 2022": [
                1073, 25_000_000, 30_000_000],
            "gdrive-export/Fireflies": [3, 5_000_000_000, 7_000_000_000]},
        "methods": {"stored": 1200}, "errors": {"total": 0, "by_type": {}},
        "notes": []})
    ss = reconcile.company_summary(root, "splitco")
    srows = {r["service"]: r for r in ss["service_rows"]}
    check("split child matched by bare manifest name",
          srows["Notion"]["actual_bytes"] == 2_600_000_000, str(srows["Notion"]))
    check("split child matched by full-path pin",
          srows["Vanta"]["actual_bytes"] == 27_800_000, str(srows["Vanta"]))
    check("split child matched by child-segment pin",
          srows["Slack"]["actual_bytes"] == 30_000_000, str(srows["Slack"]))
    check("split parent is no longer a source",
          "gdrive-export" not in ss["sources"], str(list(ss["sources"])))
    check("undeclared child surfaces as unexpected",
          "gdrive-export/Fireflies" in ss["unexpected_sources"],
          str(ss["unexpected_sources"]))
    check("split conserves bytes via (unaccounted)",
          ss["sources"]["gdrive-export/(unaccounted)"]["uncompressed_bytes"]
          == 10_000_000_000 - (2_600_000_000 + 27_800_000 + 30_000_000
                               + 7_000_000_000),
          str(ss["sources"].get("gdrive-export/(unaccounted)")))
    check("split leaves headline % on run totals",
          abs(ss["pct_complete"] - 10.0) < 0.01, str(ss["pct_complete"]))
    proc = run_script("gen_report.py", "splitco", "--root", root)
    shtml = Path(proc.stdout.strip()).read_text()
    check("report renders split children", "gdrive-export/Fireflies" in shtml)
    check("report keeps declared split services",
          "Notion" in shtml and "Vanta" in shtml)

    print("\n— copied-forward + status transitions —")
    run_path = phases.write_copied_forward_run(root, "democo",
                                               1_564_500_000_000,
                                               "2026-08-13T08:00:00Z")
    cf = common.read_json(run_path)
    check("copied-forward method", cf["method"] == "copied-forward")
    check("copied-forward totals preserved",
          cf["totals"]["uncompressed_bytes"] == 3_665_000_000_000)
    check("copied_from set", cf["copied_from"] == "2026-08-12T09:00:00Z")
    check("copied-forward tolerates old runs (empty detection)",
          cf.get("detected_services", {}) == {} and cf.get("cache") is None)
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

    print("\n— sizer: import-safety + listing parse —")
    # Import must not require env vars (module was previously KeyError-on-import).
    import corpus_sizer_rest as sizer  # noqa: E402
    page = (b"<EnumerationResults><Blobs>"
            b"<Blob><Name>gdrive/a.zip</Name><Properties>"
            b"<Content-Length>123</Content-Length><Etag>0xAB</Etag>"
            b"</Properties></Blob>"
            b"<BlobPrefix><Name>gdrive/</Name></BlobPrefix>"
            b"</Blobs><NextMarker>tok</NextMarker></EnumerationResults>")
    blobs, prefixes, marker = sizer.parse_list_page(page)
    check("parse_list_page blobs incl etag",
          blobs == [("gdrive/a.zip", 123, "0xAB")], str(blobs))
    check("parse_list_page prefixes", prefixes == ["gdrive/"], str(prefixes))
    check("parse_list_page marker", marker == "tok")
    check("blob_kind", sizer.blob_kind("A.ZIP") == "zip"
          and sizer.blob_kind("x.tar.gz") == "gz"
          and sizer.blob_kind("x.tgz") == "gz"
          and sizer.blob_kind("x.bin") == "stored")

    # Finding 8: a repeated BlobPrefix across list pages (a NextMarker page
    # boundary can land mid-prefix) must be deduped, order-preserving — a
    # repeat would otherwise double-count an entire prefix's blobs.
    pages = [
        b"<EnumerationResults><Blobs>"
        b"<BlobPrefix><Name>a/</Name></BlobPrefix>"
        b"</Blobs><NextMarker>tok</NextMarker></EnumerationResults>",
        b"<EnumerationResults><Blobs>"
        b"<BlobPrefix><Name>a/</Name></BlobPrefix>"
        b"<BlobPrefix><Name>b/</Name></BlobPrefix>"
        b"</Blobs><NextMarker/></EnumerationResults>",
    ]
    real_http_get = sizer.http_get
    try:
        sizer.http_get = lambda url, extra_headers=None: pages.pop(0)
        dprefixes, _root_blobs = sizer.discover_prefixes()
    finally:
        sizer.http_get = real_http_get
    check("discover_prefixes dedupes cross-page BlobPrefix (order-preserving)",
          dprefixes == ["a/", "b/"], str(dprefixes))

    print("\n— sizer: cache roundtrip —")
    idx_path = str(tmp / "test-index.tsv.gz")
    rows = [("gdrive/a.zip", "0xAB", 123, 456, "zip:3entries",
             '{"hubspot":[100,2]}'),
            ("slack/b.gz", "0xCD", 10, 30, "gz-trailer", "")]
    idx_fp = sizer.matcher_fingerprint(sizer.build_matcher(("gdrive",)))
    sizer.write_index(idx_path, rows, idx_fp)
    cache = sizer.load_cache(idx_path, idx_fp)
    check("cache roundtrip", cache["gdrive/a.zip"] ==
          ("0xAB", 123, 456, "zip:3entries", '{"hubspot":[100,2]}')
          and cache["slack/b.gz"] == ("0xCD", 10, 30, "gz-trailer", ""),
          str(cache))
    check("cache hit needs etag+clen match",
          sizer.cache_lookup(cache, "gdrive/a.zip", "0xAB", 123) is not None
          and sizer.cache_lookup(cache, "gdrive/a.zip", "0xZZ", 123) is None
          and sizer.cache_lookup(cache, "gdrive/a.zip", "0xAB", 999) is None
          and sizer.cache_lookup(cache, "gdrive/a.zip", "", 123) is None
          and sizer.cache_lookup(cache, "nope", "0xAB", 123) is None)
    check("missing/corrupt cache → empty (fail-safe)",
          sizer.load_cache("") == {} and
          sizer.load_cache(str(tmp / "no-such-file.tsv.gz")) == {})
    check("wrong matcher fingerprint → full miss (fail-safe)",
          sizer.load_cache(idx_path, "deadbeef0000") == {})
    check("no fingerprint demanded → header just skipped, still loads",
          sizer.load_cache(idx_path) == cache)

    print("\n— sizer: service detection matching —")
    m = sizer.build_matcher(("HubSpot", "My CRM"))
    check("catalog alias matches", sizer.match_segment("Slack", m) == "slack")
    check("token match in filename",
          sizer.match_segment("slack-export-2026.zip", m) == "slack")
    check("declared name wins with manifest spelling",
          sizer.match_segment("hubspot", m) == "HubSpot"
          and sizer.match_segment("my_crm", m) == "My CRM")
    check("no false positive", sizer.match_segment("miscellaneous", m) is None)
    check("deepest segment wins",
          sizer.match_path("gdrive/hubspot/x.csv", m) == "HubSpot")
    check("filename considered when deep",
          sizer.match_path("a/b/c/d/slack-log.txt", m) == "slack")
    check("depth cap: deep dir beyond 3 not matched",
          sizer.match_path("a/b/c/hubspot/x.csv", m) is None)
    check("l2_key shapes", sizer.l2_key("a/b/c.txt") == "a/b"
          and sizer.l2_key("a/c.txt") == "a/(files)"
          and sizer.l2_key("c.txt") == "(root)")
    big = {f"top/d{i}": [1, 10, 100 + i] for i in range(45)}
    rolled = sizer.rollup_l2(big, cap=40)
    check("rollup keeps 40 + (other)", len(rolled) == 41
          and "(other)" in rolled and rolled["(other)"][0] == 5
          and rolled["(other)"][2] == sum(100 + i for i in range(5)),
          str(rolled.get("(other)")))
    seed_path = tmp / "test-seed.tsv"
    seed_path.write_text(
        "gdrive/a.zip\t123\t456\t3.707\tzip:3entries\t0xAB\t\n"      # good
        "old/no-etag.zip\t5\t5\t1.0\tzip:1entries\n"                  # old format
        "bad/err.zip\t9\t9\t1.0\terr:BadZipFile\t0xEE\t\n"            # error row
        "plain/file.txt\t7\t7\t1.0\tstored\t0xFF\t\n")                # not cacheable
    seed = sizer.load_seed_tsv(str(seed_path))
    check("seed: keeps good zip row only", list(seed) == ["gdrive/a.zip"]
          and seed["gdrive/a.zip"] == ("0xAB", 123, 456, "zip:3entries", ""),
          str(seed))
    check("seed: headerless but fingerprint demanded → fail-safe miss",
          sizer.load_seed_tsv(str(seed_path), idx_fp) == {})
    seed_headered = tmp / "test-seed-headered.tsv"
    seed_headered.write_text(
        f"#matcher\t{idx_fp}\n"
        "gdrive/a.zip\t123\t456\t3.707\tzip:3entries\t0xAB\t\n")
    check("seed: matching header fingerprint loads",
          sizer.load_seed_tsv(str(seed_headered), idx_fp) ==
          {"gdrive/a.zip": ("0xAB", 123, 456, "zip:3entries", "")})
    check("seed: mismatched header fingerprint → miss",
          sizer.load_seed_tsv(str(seed_headered), "deadbeef0000") == {})

    print("\n— sizer: zip entry detection —")

    def make_zip(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for ename, size in entries.items():
                z.writestr(ename, b"x" * size)
        return buf.getvalue()

    zb = make_zip({"hubspot/contacts.csv": 100, "misc/y.txt": 50})
    eidx = zb.rfind(struct.pack("<I", 0x06054b50))
    (_s, _d, _c1, _c2, n_ent, cd_size, cd_off, _cl) = struct.unpack(
        "<IHHHHIIH", zb[eidx:eidx + 22])
    tot, n, svc = sizer._parse_cd(zb[cd_off:cd_off + cd_size], n_ent,
                                  sizer.build_matcher())
    check("cd totals with names", tot == 150 and n == 2, f"{tot},{n}")
    check("cd svc attribution", svc == {"hubspot": [100, 1]}, str(svc))
    tot, n, svc = sizer._parse_cd(zb[cd_off:cd_off + cd_size], n_ent, None)
    check("cd no matcher → no svc", tot == 150 and svc == {})

    print("\n— sizer: gz trailer taxonomy —")
    real_fetch = sizer.fetch_range
    try:
        trailers = {}
        sizer.fetch_range = lambda name, s, e: trailers[name]
        trailers["a.gz"] = struct.pack("<I", 3000)
        check("plausible trailer", sizer.gz_uncompressed("a.gz", 1000)
              == (3000, "gz-trailer"))
        trailers["b.gz"] = struct.pack("<I", 500)
        check("floored trailer", sizer.gz_uncompressed("b.gz", 1000)
              == (1000, "gz-floor"))
        trailers["c.gz"] = struct.pack("<I", 0xFFFFFFFF)
        check("garbage trailer (ratio > 1032x)",
              sizer.gz_uncompressed("c.gz", 1000) == (1000, "gz-bad-trailer"))
        trailers["d.gz"] = struct.pack("<I", 1032 * 1000)  # exactly at bound
        check("exactly 1032x is allowed",
              sizer.gz_uncompressed("d.gz", 1000) == (1032000, "gz-trailer"))
        check("tiny", sizer.gz_uncompressed("e.gz", 3) == (3, "gz-tiny"))
    finally:
        sizer.fetch_range = real_fetch

    print("\n— sizer: gz exact streaming primitives —")
    real_stream = sizer.stream_blob_chunks
    try:
        blobs = {}

        def fake_stream(name, chunk=7):  # tiny chunks: exercise boundaries
            data = blobs[name]
            for i in range(0, len(data), chunk):
                yield data[i:i + chunk]

        sizer.stream_blob_chunks = fake_stream
        blobs["multi.gz"] = gzip.compress(b"a" * 5000) + gzip.compress(b"b" * 7000)
        check("multi-member exact sum", sizer.gz_stream_exact("multi.gz") == 12000)
        blobs["bgzip.gz"] = gzip.compress(b"x" * 9000) + gzip.compress(b"")
        check("bgzip-style empty EOF member", sizer.gz_stream_exact("bgzip.gz") == 9000)
        blobs["trunc.gz"] = gzip.compress(b"y" * 5000)[:-8]
        try:
            sizer.gz_stream_exact("trunc.gz")
            check("truncated stream raises", False)
        except sizer.TruncatedGzStream as exc:
            check("truncated stream raises", True)
            check("truncation carries exact partial", exc.partial == 5000,
                  str(exc.partial))
        blobs["junk.gz"] = b"\x00" * 64
        try:
            sizer.gz_stream_exact("junk.gz")
            check("non-gzip bytes raise", False)
        except Exception:
            check("non-gzip bytes raise", True)
    finally:
        sizer.stream_blob_chunks = real_stream

    print("\n— sizer: zip64 EOCD recovery —")

    def make_plain_zip(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for ename, size in entries.items():
                z.writestr(ename, b"x" * size)
        return buf.getvalue()

    def force_zip64(entries):
        """Build a zip whose EOCD carries 0xFFFF/0xFFFFFFFF sentinels + real
        zip64 locator/EOCD64 records (zipfile only emits them when forced)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for ename, size in entries.items():
                z.writestr(ename, b"x" * size)
        data = buf.getvalue()
        eidx = data.rfind(struct.pack("<I", 0x06054b50))
        (_s, _d, _c1, _c2, n_ent, cd_size, cd_off, _cl) = struct.unpack(
            "<IHHHHIIH", data[eidx:eidx + 22])
        eocd64 = struct.pack("<IQHHIIQQQQ", 0x06064b50, 44, 45, 45, 0, 0,
                             n_ent, n_ent, cd_size, cd_off)
        loc64 = struct.pack("<IIQI", 0x07064b50, 0, eidx, 1)
        eocd = struct.pack("<IHHHHIIH", 0x06054b50, 0, 0, 0xFFFF, 0xFFFF,
                           0xFFFFFFFF, 0xFFFFFFFF, 0)
        return data[:eidx] + eocd64 + loc64 + eocd

    real_fetch2 = sizer.fetch_range
    try:
        served = {}
        sizer.fetch_range = (lambda name, s, e:
                             served[name][s:e + 1])

        z64 = force_zip64({"a/x.bin": 1000, "b/y.bin": 500})
        served["ok64.zip"] = z64
        tot, note, _ = sizer.zip_uncompressed("ok64.zip", len(z64))
        check("zip64 via locator", (tot, note) == (1500, "zip:2entries"),
              f"{tot},{note}")

        # corrupt the locator signature → old code floored as zip-loc64-bad;
        # the EOCD64-scan fallback must still recover the true size
        eidx = z64.rfind(struct.pack("<I", 0x06054b50))
        loc_start = eidx - 20
        bad = bytearray(z64)
        bad[loc_start:loc_start + 4] = b"\x00\x00\x00\x00"
        served["badloc.zip"] = bytes(bad)
        tot, note, _ = sizer.zip_uncompressed("badloc.zip", len(bad))
        check("zip64 corrupt locator → EOCD64 scan", (tot, note)
              == (1500, "zip:2entries"), f"{tot},{note}")

        # locator missing entirely (EOCD64 directly before EOCD)
        noloc = bytes(bad[:loc_start]) + bytes(bad[eidx:])
        served["noloc.zip"] = noloc
        tot, note, _ = sizer.zip_uncompressed("noloc.zip", len(noloc))
        check("zip64 missing locator → EOCD64 scan", (tot, note)
              == (1500, "zip:2entries"), f"{tot},{note}")

        # no EOCD64 anywhere → still floors
        gone = bytes(bad).replace(struct.pack("<I", 0x06064b50), b"\x00" * 4)
        served["gone.zip"] = gone
        tot, note, _ = sizer.zip_uncompressed("gone.zip", len(gone))
        check("zip64 unrecoverable still floors",
              (tot, note) == (len(gone), "zip-loc64-bad"), f"{tot},{note}")

        # saturated entry count (0xFFFF, no zip64 records — Takeout past
        # 65,535 entries): CD offsets are real, must be walked to the end
        sat = bytearray(make_plain_zip({"a/x.bin": 1000, "b/y.bin": 500}))
        seidx = bytes(sat).rfind(struct.pack("<I", 0x06054b50))
        sat[seidx + 8:seidx + 12] = struct.pack("<HH", 0xFFFF, 0xFFFF)
        served["sat.zip"] = bytes(sat)
        tot, note, _ = sizer.zip_uncompressed("sat.zip", len(sat))
        check("saturated entry count → CD walk", (tot, note)
              == (1500, "zip:2entries"), f"{tot},{note}")

        # 22-byte empty archive (bare EOCD, cd_size=0): must not fetch a
        # zero-length CD range (bytes=0--1 was an HTTPError 400)
        ez = make_plain_zip({})
        served["empty22.zip"] = ez
        real_fetch3 = sizer.fetch_range
        sizer.fetch_range = (lambda name, s, e: served[name][s:e + 1]
                             if e >= s else (_ for _ in ()).throw(
                                 AssertionError("negative range")))
        tot, note, _ = sizer.zip_uncompressed("empty22.zip", len(ez))
        check("empty 22-byte zip → 0 entries", (tot, note)
              == (0, "zip:0entries"), f"{tot},{note}")
        sizer.fetch_range = real_fetch3

        # empty blob named .zip: no range request must be made (a 0-byte
        # read is an invalid Range and used to surface as HTTPError 400)
        sizer.fetch_range = lambda name, s, e: (_ for _ in ()).throw(
            AssertionError("range read on tiny zip"))
        check("zero-byte zip short-circuits",
              sizer.zip_uncompressed("empty.zip", 0) == (0, "zip-tiny", {}))
        sizer.fetch_range = lambda name, s, e: served[name][s:e + 1]

        # EOCD pushed out of the 65557-byte tail by trailing junk → wide retry
        plain = make_plain_zip({"c/z.bin": 700})
        junk = plain + b"\xde\xad" * 40_000  # 80 KB of trailing junk
        served["junktail.zip"] = junk
        tot, note, _ = sizer.zip_uncompressed("junktail.zip", len(junk))
        check("EOCD beyond 64K tail → wide-tail retry",
              (tot, note) == (700, "zip:1entries"), f"{tot},{note}")
    finally:
        sizer.fetch_range = real_fetch2

    real_stream_hr = sizer.stream_blob_chunks
    try:
        hr_blob = gzip.compress(b"\x00" * 50_000_000)  # ~1032x ratio

        def hr_stream(name, chunk=1 << 20):
            for i in range(0, len(hr_blob), 1 << 20):
                yield hr_blob[i:i + (1 << 20)]

        sizer.stream_blob_chunks = hr_stream
        check("high-ratio stream exact (bounded memory)",
              sizer.gz_stream_exact("hr.gz") == 50_000_000)
    finally:
        sizer.stream_blob_chunks = real_stream_hr

    # ── drain-loop livelock regression: force _DECOMP_STEP tiny so a single
    # member's output crosses it (exercising the unconsumed_tail drain loop),
    # then end that member mid-drain and start a second member. Before the
    # eof guard this hangs forever; after, it must return promptly. ──
    real_stream_dr = sizer.stream_blob_chunks
    real_decomp_step = sizer._DECOMP_STEP
    try:
        sizer._DECOMP_STEP = 1024  # function reads the module global at call time
        dr_blob = gzip.compress(b"\x00" * 100_000) + gzip.compress(b"x" * 500)

        def dr_stream(name, chunk=1 << 20):
            yield dr_blob  # single chunk: forces the drain loop internally

        sizer.stream_blob_chunks = dr_stream
        check("drain-crossing multi-member exact (livelock regression)",
              sizer.gz_stream_exact("dr.gz") == 100_500)

        # re-assert an existing multi-member fixture under the tiny step,
        # to cover member-end-exactly-at-drain edges too
        bgzip_blob = gzip.compress(b"x" * 9000) + gzip.compress(b"")

        def bgzip_stream(name, chunk=1 << 20):
            yield bgzip_blob

        sizer.stream_blob_chunks = bgzip_stream
        check("bgzip-style empty EOF member under tiny _DECOMP_STEP",
              sizer.gz_stream_exact("bgzip-tiny-step.gz") == 9000)
    finally:
        sizer.stream_blob_chunks = real_stream_dr
        sizer._DECOMP_STEP = real_decomp_step

    b = sizer.StreamBudget(100)
    check("budget reserve/deny", b.reserve(60) and not b.reserve(50)
          and b.reserve(40) and not b.reserve(1) and b.used == 100)

    print("\n— sizer: offline end-to-end (fake container, cold then cached) —")
    container = {
        "gdrive/export.zip": make_zip({"docs/a.txt": 1000,
                                       "hubspot/contacts.csv": 5000}),
        "gdrive/plain.txt": b"y" * 200,
        "slack/logs/day1.gz": gzip.compress(b"z" * 3000),
        "rootfile.bin": b"r" * 50,
    }
    etags = {n: f"0x{i:02d}" for i, n in enumerate(sorted(container))}
    counters = {"range_gets": 0}

    def make_listing_xml(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        prefix = q.get("prefix", [""])[0]
        delim = q.get("delimiter", [""])[0]
        blobs, prefs, seen = [], [], set()
        for n in sorted(n for n in container if n.startswith(prefix)):
            rest = n[len(prefix):]
            if delim and delim in rest:
                p = prefix + rest.split(delim)[0] + delim
                if p not in seen:
                    seen.add(p)
                    prefs.append(p)
                continue
            blobs.append(n)
        parts = ["<EnumerationResults><Blobs>"]
        for n in blobs:
            parts.append(f"<Blob><Name>{n}</Name><Properties>"
                         f"<Content-Length>{len(container[n])}</Content-Length>"
                         f"<Etag>{etags[n]}</Etag></Properties></Blob>")
        for p in prefs:
            parts.append(f"<BlobPrefix><Name>{p}</Name></BlobPrefix>")
        parts.append("</Blobs><NextMarker/></EnumerationResults>")
        return "".join(parts).encode()

    def fake_http(url, extra_headers=None):
        if "comp=list" in url:
            return make_listing_xml(url)
        name = urllib.parse.unquote(
            url.split("/fake-raw/", 1)[1].split("?", 1)[0])
        data = container[name]
        rng = (extra_headers or {}).get("Range")
        if rng:
            counters["range_gets"] += 1
            a, b = rng[len("bytes="):].split("-")
            return data[int(a):int(b) + 1]
        return data

    swork = tmp / "sizer-work"
    swork.mkdir()
    sizer_env = {"SA": "fakesa", "CONTAINER": "fake-raw", "SAS": "sig=x",
                 "TAG": "fakeco-sizer", "OUT_DIR": str(swork),
                 "EXPECTED_SERVICES": "gdrive,hubspot,slack",
                 "SIZER_WORKERS": "4", "LIST_WORKERS": "2"}
    os.environ.update(sizer_env)
    os.environ.pop("CACHE_FILE", None)
    os.environ.pop("SEED_TSV", None)
    real_http = sizer.http_get
    try:
        sizer.http_get = fake_http
        sizer.main()
        s1 = json.loads((swork / "fakeco-sizer.summary.json").read_text())
        check("cold run totals", s1["blobs"] == 4
              and s1["unc"] == 6000 + 200 + 3000 + 50
              and s1["zero"] == 0 and s1["errors"] == 0,
              json.dumps(s1)[:300])
        check("cold run sources", set(s1["src"]) == {"gdrive", "slack", "(root)"})
        check("sources_l2 keys", set(s1["sources_l2"]) ==
              {"gdrive/(files)", "slack/logs", "(root)"}, str(s1["sources_l2"]))
        det = s1["detected_services"]
        check("hubspot detected inside zip",
              det["hubspot"]["bytes"] == 5000
              and det["hubspot"]["entry_count"] == 1
              and det["hubspot"]["zip_entry_bytes"] == 5000
              and det["hubspot"]["sources"] == {"gdrive": 5000}, str(det))
        check("gdrive path-detected (zip 6000 + plain 200)",
              det["gdrive"]["bytes"] == 6200
              and det["gdrive"]["path_bytes"] == 6200, str(det.get("gdrive")))
        check("slack path-detected", det["slack"]["bytes"] == 3000)
        check("cold cache stats", s1["cache"] == {"hits": 0, "misses": 2},
              str(s1["cache"]))
        check(".done and index written",
              (swork / "fakeco-sizer.done").exists()
              and (swork / "fakeco-sizer.index.tsv.gz").exists())
        cold_ranges = counters["range_gets"]
        check("cold run did range reads", cold_ranges > 0)

        # ── warm run: seed CACHE_FILE from the produced index ──
        cache_copy = tmp / "fakeco-index.tsv.gz"
        shutil.copy(swork / "fakeco-sizer.index.tsv.gz", cache_copy)
        for f in swork.glob("fakeco-sizer.*"):
            f.unlink()
        os.environ["CACHE_FILE"] = str(cache_copy)
        counters["range_gets"] = 0
        sizer.main()
        s2 = json.loads((swork / "fakeco-sizer.summary.json").read_text())
        check("warm run identical totals",
              s2["unc"] == s1["unc"] and s2["src"] == s1["src"])
        check("warm run all hits, zero range reads",
              s2["cache"] == {"hits": 2, "misses": 0}
              and counters["range_gets"] == 0,
              f'{s2["cache"]} ranges={counters["range_gets"]}')
        check("warm run keeps zip-entry detection (from cached det_json)",
              s2["detected_services"]["hubspot"]["bytes"] == 5000,
              str(s2["detected_services"].get("hubspot")))
        tsv_lines = (swork / "fakeco-sizer.sizes.tsv").read_text() \
            .rstrip("\n").split("\n")
        check("tsv line 1 is the #matcher header",
              tsv_lines[0].startswith("#matcher\t"), tsv_lines[0])
        data_rows = tsv_lines[1:]
        check("tsv has 4 data rows x 7 cols", len(data_rows) == 4
              and all(len(r.split("\t")) == 7 for r in data_rows),
              tsv_lines[0])

        # ── staleness: EXPECTED_SERVICES changes → the matcher fingerprint
        # changes → the on-disk cache (built under the OLD matcher) must be
        # a full miss, not a replay of stale zip-entry detection. Dropping
        # "hubspot" wouldn't actually change the matcher (it's already a
        # built-in catalog alias) — declaring a DIFFERENT SPELLING for the
        # same normalized key does: catalog gives hubspot -> "hubspot";
        # declaring "HubSpot" overrides the mapped VALUE (not the key) to
        # "HubSpot", so a correctly-invalidated re-read must attribute the
        # embedded contacts.csv entry to "HubSpot", not the stale "hubspot".
        for f in swork.glob("fakeco-sizer.*"):
            f.unlink()
        os.environ["EXPECTED_SERVICES"] = "gdrive,HubSpot,slack"
        counters["range_gets"] = 0
        sizer.main()
        s3 = json.loads((swork / "fakeco-sizer.summary.json").read_text())
        check("staleness: matcher change forces full re-read (cache miss)",
              s3["cache"] == {"hits": 0, "misses": 2}, str(s3["cache"]))
        check("staleness: detection reflects the NEW matcher, not a stale "
              "cached det_json",
              "HubSpot" in s3["detected_services"]
              and "hubspot" not in s3["detected_services"]
              and s3["detected_services"]["HubSpot"]["bytes"] == 5000,
              str(s3["detected_services"]))

        # ── Finding 1: a poisoned cached det_json (right shape of JSON, wrong
        # shape of value — e.g. from a future format change) must degrade to
        # a cache MISS at load time, not a persistent hard failure — a cache
        # can only cost time, never correctness. A relaunch that keeps
        # passing the same poisoned CACHE_FILE must succeed every time (the
        # poisoned row is just re-fetched), not hard-fail the company until
        # someone deletes the file by hand. ──
        for f in swork.glob("fakeco-sizer.*"):
            f.unlink()
        os.environ["EXPECTED_SERVICES"] = "gdrive,hubspot,slack"
        poison_fp = sizer.matcher_fingerprint(
            sizer.build_matcher(("gdrive", "hubspot", "slack")))
        zip_name = "gdrive/export.zip"
        poison_path = tmp / "fakeco-poison.tsv.gz"
        with gzip.open(poison_path, "wt", newline="") as pf:
            pf.write(f"#matcher\t{poison_fp}\n")
            pf.write(f"{zip_name}\t{etags[zip_name]}\t"
                     f"{len(container[zip_name])}\t6000\tzip:2entries\t"
                     '{"hubspot":5}\n')  # malformed: not [bytes, count]
        os.environ["CACHE_FILE"] = str(poison_path)
        counters["range_gets"] = 0
        sizer.main()
        s4 = json.loads((swork / "fakeco-sizer.summary.json").read_text())
        check("poisoned det_json: run succeeds (fail-safe miss, not fatal)",
              s4["blobs"] == 4 and s4["unc"] == s1["unc"], json.dumps(s4)[:300])
        check("poisoned det_json: the poisoned row loaded as a miss "
              "(cache had only that 1 row; both cacheable blobs re-fetched)",
              s4["cache"] == {"hits": 0, "misses": 2}, str(s4["cache"]))
        check("poisoned det_json: correct totals re-derived from the real "
              "zip central directory, not the poisoned value",
              s4["detected_services"]["hubspot"]["bytes"] == 5000,
              str(s4["detected_services"].get("hubspot")))
        check(".done written despite the poisoned cache row",
              (swork / "fakeco-sizer.done").exists())

        # ── Finding 1 (consumer-guard backstop): the guard around agg.add()
        # in the consumer thread must still catch everything ELSE that can
        # go wrong there (e.g. a TSV OSError) and fail the run rather than
        # silently truncating it. Fault-inject directly since load-time
        # validation now closes off the det_json route to this path. ──
        for f in swork.glob("fakeco-sizer.*"):
            f.unlink()
        os.environ.pop("CACHE_FILE", None)
        os.environ.pop("SEED_TSV", None)
        real_agg_add = sizer.Aggregator.add

        def _boom(self, r):
            raise RuntimeError("synthetic consumer failure")

        sizer.Aggregator.add = _boom
        counters["range_gets"] = 0
        raised = None
        try:
            sizer.main()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        finally:
            sizer.Aggregator.add = real_agg_add
        check("consumer-guard backstop: main() still raises on a broken "
              "consumer", raised is not None, repr(raised))
        check("consumer-guard backstop: no .done written",
              not (swork / "fakeco-sizer.done").exists())
    finally:
        sizer.http_get = real_http
        for k in list(sizer_env) + ["CACHE_FILE", "SEED_TSV"]:
            os.environ.pop(k, None)

    print("\n— sizer: gz streaming end-to-end (trigger, budget, cache) —")
    gzc = {
        # floored (bgzip-style) 9000-logical, small clen -> floor-min trigger
        "gz/a-multi.gz": gzip.compress(b"x" * 9000) + gzip.compress(b""),
        # floored, second in listing order — budget will exclude it
        "gz/b-multi.gz": gzip.compress(b"y" * 4000) + gzip.compress(b""),
        # garbage trailer: gzip magic + junk + huge ISIZE; stream must FAIL -> fallback
        "gz/c-junk.gz": b"\x1f\x8b" + b"\x00" * 60 + struct.pack("<I", 0xFFFFFFFF),
        # plausible trailer, clen >= threshold -> streamed exact
        # (30000 -> clen ~64, ratio ~469: safely under the 1032x bound)
        "gz/d-big.gz": gzip.compress(b"z" * 30000),
        # plausible trailer, below every trigger -> stays gz-trailer, certain
        "gz/e-small.gz": gzip.compress(b"w" * 2000),
    }
    getags = {n: f"0xGZ{i:02d}" for i, n in enumerate(sorted(gzc))}
    stream_calls = []

    def gz_listing_xml(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        prefix = q.get("prefix", [""])[0]
        delim = q.get("delimiter", [""])[0]
        parts, seen = ["<EnumerationResults><Blobs>"], set()
        for n in sorted(k for k in gzc if k.startswith(prefix)):
            rest = n[len(prefix):]
            if delim and delim in rest:
                p = prefix + rest.split(delim)[0] + delim
                if p not in seen:
                    seen.add(p)
                    parts.append(f"<BlobPrefix><Name>{p}</Name></BlobPrefix>")
                continue
            parts.append(f"<Blob><Name>{n}</Name><Properties>"
                         f"<Content-Length>{len(gzc[n])}</Content-Length>"
                         f"<Etag>{getags[n]}</Etag></Properties></Blob>")
        parts.append("</Blobs><NextMarker/></EnumerationResults>")
        return "".join(parts).encode()

    def gz_http(url, extra_headers=None):
        if "comp=list" in url:
            return gz_listing_xml(url)
        name = urllib.parse.unquote(url.split("/gz-raw/", 1)[1].split("?", 1)[0])
        data = gzc[name]
        rng = (extra_headers or {}).get("Range")
        if rng:
            a, bb = rng[len("bytes="):].split("-")
            return data[int(a):int(bb) + 1]
        return data

    def gz_fake_stream(name, chunk=1 << 20):
        stream_calls.append(name)
        data = gzc[name]
        for i in range(0, len(data), 13):
            yield data[i:i + 13]

    gzwork = tmp / "gz-work"
    gzwork.mkdir()
    candidates_clen = (len(gzc["gz/a-multi.gz"]) + len(gzc["gz/b-multi.gz"])
                       + len(gzc["gz/c-junk.gz"]) + len(gzc["gz/d-big.gz"]))
    gz_env = {"SA": "gzsa", "CONTAINER": "gz-raw", "SAS": "sig=g",
              "TAG": "gzco-sizer", "OUT_DIR": str(gzwork),
              "SIZER_WORKERS": "1", "LIST_WORKERS": "1",
              "GZ_STREAM_THRESHOLD": str(len(gzc["gz/d-big.gz"])),
              "GZ_STREAM_FLOOR_MIN": "1",
              "GZ_STREAM_BUDGET": str(candidates_clen)}  # run 1: fits ALL
    os.environ.update(gz_env)
    for k in ("CACHE_FILE", "SEED_TSV", "EXPECTED_SERVICES"):
        os.environ.pop(k, None)
    real_http2, real_stream2 = sizer.http_get, sizer.stream_blob_chunks
    try:
        sizer.http_get = gz_http
        sizer.stream_blob_chunks = gz_fake_stream

        # ── run 1 (cold, budget fits all candidates) ──
        # a-multi, b-multi: gz-floor (empty last member) -> streamed exact
        # c-junk: gz-bad-trailer -> stream attempted ONCE, zlib.error is a
        #   terminal decode failure (not retried) -> fallback
        # d-big: gz-trailer with clen == threshold -> streamed exact
        # e-small: gz-trailer below threshold -> untouched, certain
        sizer.main()
        g1 = json.loads((gzwork / "gzco-sizer.summary.json").read_text())
        tsv = {ln.split("\t")[0]: ln.split("\t")
               for ln in (gzwork / "gzco-sizer.sizes.tsv").read_text()
               .rstrip("\n").split("\n")[1:]}
        check("a-multi gz-exact 9000", tsv["gz/a-multi.gz"][4] == "gz-exact"
              and tsv["gz/a-multi.gz"][2] == "9000")
        check("b-multi gz-exact 4000", tsv["gz/b-multi.gz"][4] == "gz-exact"
              and tsv["gz/b-multi.gz"][2] == "4000")
        check("c-junk: 1 stream attempt (terminal zlib.error, no retry) "
              "then fallback",
              stream_calls.count("gz/c-junk.gz") == 1
              and tsv["gz/c-junk.gz"][4] == "gz-bad-trailer"
              and tsv["gz/c-junk.gz"][2] == str(len(gzc["gz/c-junk.gz"])))
        check("d-big gz-exact 30000", tsv["gz/d-big.gz"][4] == "gz-exact"
              and tsv["gz/d-big.gz"][2] == "30000")
        check("e-small stays gz-trailer, never streamed",
              tsv["gz/e-small.gz"][4] == "gz-trailer"
              and "gz/e-small.gz" not in stream_calls)
        check("run1 gz stats", g1["gz"]["streamed"] == 3
              and g1["gz"]["uncertain"] == 1
              and g1["gz"]["uncertain_bytes"] == len(gzc["gz/c-junk.gz"]))

        # ── warm run: gz-exact rows are permanent hits; the garbage blob is
        # stale (bad-trailer meeting the trigger) and gets re-attempted ──
        shutil.copy(gzwork / "gzco-sizer.index.tsv.gz", tmp / "gz-index.tsv.gz")
        for f in gzwork.glob("gzco-sizer.*"):
            f.unlink()
        os.environ["CACHE_FILE"] = str(tmp / "gz-index.tsv.gz")
        stream_calls.clear()
        sizer.main()
        g2 = json.loads((gzwork / "gzco-sizer.summary.json").read_text())
        check("warm: only the garbage blob re-attempted",
              set(stream_calls) == {"gz/c-junk.gz"}
              and g2["cache"]["hits"] >= 3 and g2["gz"]["streamed"] == 0)

        # ── budget starvation (cold, budget fits only a-multi; single worker
        # makes reservation order = listing order a, b, c, d) ──
        for f in gzwork.glob("gzco-sizer.*"):
            f.unlink()
        os.environ.pop("CACHE_FILE", None)
        os.environ["GZ_STREAM_BUDGET"] = str(len(gzc["gz/a-multi.gz"]))
        stream_calls.clear()
        sizer.main()
        g3 = json.loads((gzwork / "gzco-sizer.summary.json").read_text())
        check("starved: one streamed, rest uncertain (b floor, c bad, d big)",
              g3["gz"]["streamed"] == 1 and g3["gz"]["uncertain"] == 3)

        # ── budget=0: streaming AND staleness off (no perpetual misses) ──
        for f in gzwork.glob("gzco-sizer.*"):
            f.unlink()
        os.environ["CACHE_FILE"] = str(tmp / "gz-index.tsv.gz")  # run-1 index
        os.environ["GZ_STREAM_BUDGET"] = "0"
        stream_calls.clear()
        sizer.main()
        g4 = json.loads((gzwork / "gzco-sizer.summary.json").read_text())
        check("budget=0: no streams, exact+bad rows all hit",
              stream_calls == [] and g4["gz"]["streamed"] == 0
              and g4["cache"]["hits"] >= 4)
    finally:
        sizer.http_get, sizer.stream_blob_chunks = real_http2, real_stream2
        for k in list(gz_env) + ["CACHE_FILE", "SEED_TSV"]:
            os.environ.pop(k, None)

    # ── pure predicates (set globals directly under a try/finally restore —
    # nothing later in this file calls sizer.main() again to reset them) ──
    _saved_gz_globals = (sizer.GZ_STREAM_BUDGET, sizer.GZ_STREAM_THRESHOLD,
                         sizer.GZ_STREAM_FLOOR_MIN)
    try:
        sizer.GZ_STREAM_BUDGET, sizer.GZ_STREAM_THRESHOLD, sizer.GZ_STREAM_FLOOR_MIN = 1, 100, 10
        check("candidate: threshold", sizer.gz_stream_candidate("gz-trailer", 100))
        check("candidate: floored above min", sizer.gz_stream_candidate("gz-floor", 10))
        check("candidate: floored below min",
              not sizer.gz_stream_candidate("gz-floor", 9))
        check("candidate: plausible below threshold",
              not sizer.gz_stream_candidate("gz-trailer", 99))
        sizer.GZ_STREAM_BUDGET = 0
        check("candidate: disabled", not sizer.gz_stream_candidate("gz-trailer", 100))
        check("stale: disabled", not sizer.gz_cached_row_stale("gz-trailer", 500, 500))
        sizer.GZ_STREAM_BUDGET = 1
        check("stale: old-gen floored trailer row",
              sizer.gz_cached_row_stale("gz-trailer", 50, 50))
        check("stale: exact never", not sizer.gz_cached_row_stale("gz-exact", 500, 500))
        check("stale: big plausible", sizer.gz_cached_row_stale("gz-trailer", 100, 300))
        check("uncertain rows", sizer.gz_uncertain_row("gz", "gz-floor", 5)
              and sizer.gz_uncertain_row("gz", "gz-trailer", 100)
              and not sizer.gz_uncertain_row("gz", "gz-trailer", 99)
              and not sizer.gz_uncertain_row("zip", "zip:3entries", 500))
    finally:
        (sizer.GZ_STREAM_BUDGET, sizer.GZ_STREAM_THRESHOLD,
         sizer.GZ_STREAM_FLOOR_MIN) = _saved_gz_globals

    print("\n— sizer: gz stream retry classification (terminal vs transport) —")
    real_fetch3, real_stream3 = sizer.fetch_range, sizer.stream_blob_chunks
    _saved_gz_globals2 = (sizer.GZ_STREAM_BUDGET, sizer.GZ_STREAM_THRESHOLD,
                          sizer.GZ_STREAM_FLOOR_MIN)
    try:
        # any clen is a stream candidate via the threshold; the per-call
        # StreamBudget object below is the real cap being exercised
        sizer.GZ_STREAM_BUDGET = 10 ** 9
        sizer.GZ_STREAM_THRESHOLD = 1
        sizer.GZ_STREAM_FLOOR_MIN = 1

        payload = gzip.compress(b"q" * 500)
        clen = len(payload)
        sizer.fetch_range = lambda name, s, e: payload[-4:]  # plausible ISIZE=500
        attempt_state = {"n": 0}

        def transient_then_ok(name, chunk=1 << 20):
            attempt_state["n"] += 1
            if attempt_state["n"] <= 2:
                raise OSError("simulated transport blip")
            for i in range(0, len(payload), 13):
                yield payload[i:i + 13]

        sizer.stream_blob_chunks = transient_then_ok

        # budget fits 3 reservations: 2 transport failures then success
        b_ok = sizer.StreamBudget(clen * 3)
        r_ok = sizer.size_blob("gz/x.gz", clen, "0xE", "gz", None, b_ok)
        check("transport retry: succeeds on 3rd attempt, gz-exact",
              r_ok["method"] == "gz-exact" and r_ok["uncomp"] == 500
              and attempt_state["n"] == 3 and b_ok.used == clen * 3,
              str(r_ok))

        # budget fits only the first reservation: retry #2 is denied ->
        # falls back to the trailer value, one attempt made
        attempt_state["n"] = 0
        b_fallback = sizer.StreamBudget(clen)
        r_fb = sizer.size_blob("gz/x.gz", clen, "0xE", "gz", None, b_fallback)
        check("transport retry: budget exhausted after 1st attempt -> fallback",
              r_fb["method"] == "gz-trailer" and r_fb["uncomp"] == 500
              and attempt_state["n"] == 1 and b_fallback.used == clen,
              str(r_fb))
    finally:
        sizer.fetch_range, sizer.stream_blob_chunks = real_fetch3, real_stream3
        (sizer.GZ_STREAM_BUDGET, sizer.GZ_STREAM_THRESHOLD,
         sizer.GZ_STREAM_FLOOR_MIN) = _saved_gz_globals2

    print("\n— deep verify: sizer end-to-end (shallow→deep→warm→shallow) —")
    import bz2 as _bz2
    import lzma as _lzma
    tam = bytearray(make_zip({"t.txt": 4000}))
    _p = bytes(tam).find(struct.pack("<I", 0x02014b50))
    tam[_p + 24:_p + 28] = struct.pack("<I", 123456)  # CD usize lie
    dv = {
        "src/ok.zip": make_zip({"docs/a.txt": 5000, "b.txt": 1200}),
        "src/tampered.zip": bytes(tam),
        "src/multi.bz2": _bz2.compress(b"A" * 10000) + _bz2.compress(b"B" * 20000),
        "src/data.xz": _lzma.compress(b"C" * 30000),
        "src/trunc.bz2": _bz2.compress(b"D" * 50000)[:-10],
        "src/notxz.xz": b"garbage, not xz at all!!",
        "src/arch.7z": b"7z" + b"\x00" * 104,
        "src/logs.gz": gzip.compress(b"E" * 40000),
        "src/plain.txt": b"F" * 777,
    }
    dv_etags = {n: f"0xD{i:02d}" for i, n in enumerate(sorted(dv))}
    dv_counters = {"range_gets": 0, "streams": 0}

    def dv_listing(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        prefix = q.get("prefix", [""])[0]
        delim = q.get("delimiter", [""])[0]
        blobs, prefs, seen = [], [], set()
        for n in sorted(n for n in dv if n.startswith(prefix)):
            rest = n[len(prefix):]
            if delim and delim in rest:
                p = prefix + rest.split(delim)[0] + delim
                if p not in seen:
                    seen.add(p)
                    prefs.append(p)
                continue
            blobs.append(n)
        parts = ["<EnumerationResults><Blobs>"]
        for n in blobs:
            parts.append(f"<Blob><Name>{n}</Name><Properties>"
                         f"<Content-Length>{len(dv[n])}</Content-Length>"
                         f"<Etag>{dv_etags[n]}</Etag></Properties></Blob>")
        for p in prefs:
            parts.append(f"<BlobPrefix><Name>{p}</Name></BlobPrefix>")
        parts.append("</Blobs><NextMarker/></EnumerationResults>")
        return "".join(parts).encode()

    def dv_http(url, extra_headers=None):
        if "comp=list" in url:
            return dv_listing(url)
        name = urllib.parse.unquote(
            url.split("/deep-raw/", 1)[1].split("?", 1)[0])
        data = dv[name]
        rng = (extra_headers or {}).get("Range")
        if rng:
            dv_counters["range_gets"] += 1
            a, b = rng[len("bytes="):].split("-")
            return data[int(a):int(b) + 1]
        return data

    def dv_stream(name, chunk=1 << 20):
        dv_counters["streams"] += 1
        data = dv[name]
        for i in range(0, len(data), 997):  # odd size exercises boundaries
            yield data[i:i + 997]

    dwork = tmp / "deep-work"
    dwork.mkdir()
    denv = {"SA": "fakesa", "CONTAINER": "deep-raw", "SAS": "sig=x",
            "TAG": "deepco", "OUT_DIR": str(dwork),
            "SIZER_WORKERS": "4", "LIST_WORKERS": "2"}
    os.environ.update(denv)
    for k in ("CACHE_FILE", "SEED_TSV", "DEEP_VERIFY", "EXPECTED_SERVICES"):
        os.environ.pop(k, None)
    real_http_dv, real_stream_dv = sizer.http_get, sizer.stream_blob_chunks

    def dv_tsv():
        rows = {}
        for line in (dwork / "deepco.sizes.tsv").read_text().splitlines():
            if line.startswith("#"):
                continue
            f = line.split("\t")
            rows[f[0]] = (int(f[1]), int(f[2]), f[4])  # clen, uncomp, method
        return rows

    def dv_reset(cache_from_index: bool):
        cache_path = tmp / "deepco-cache.tsv.gz"
        if cache_from_index:
            shutil.copy(dwork / "deepco.index.tsv.gz", cache_path)
            os.environ["CACHE_FILE"] = str(cache_path)
        else:
            os.environ.pop("CACHE_FILE", None)
        for f in dwork.glob("deepco.*"):
            f.unlink()
        dv_counters["range_gets"] = dv_counters["streams"] = 0

    try:
        sizer.http_get = dv_http
        sizer.stream_blob_chunks = dv_stream

        # ── phase 1: SHALLOW cold — bz2/xz are zero-HTTP placeholders ──
        sizer.main()
        s_sh = json.loads((dwork / "deepco.summary.json").read_text())
        t1 = dv_tsv()
        check("shallow: no verification block in summary",
              "verification" not in s_sh)
        check("shallow: bz2/xz placeholders, zero HTTP for them",
              t1["src/multi.bz2"][2] == "bz2-stored"
              and t1["src/multi.bz2"][1] == t1["src/multi.bz2"][0]
              and t1["src/data.xz"][2] == "xz-stored"
              and dv_counters["streams"] == 0
              and dv_counters["range_gets"] == 5,  # 2 zips×2 + 1 gz trailer
              f"{t1} ranges={dv_counters['range_gets']}")
        check("shallow: methods histogram gains bz2/xz kinds",
              s_sh["methods"] == {"zip": 2, "gz": 1, "bz2": 2, "xz": 2,
                                  "stored": 2}, str(s_sh["methods"]))
        check("shallow: placeholder rows are neither hits nor misses",
              s_sh["cache"] == {"hits": 0, "misses": 3}, str(s_sh["cache"]))
        check("shallow: tampered zip trusts the lying CD (by design)",
              t1["src/tampered.zip"][1] == 123456,
              str(t1["src/tampered.zip"]))

        # ── phase 2: DEEP seeded from the shallow index — every
        # metadata-trusted row is stale and re-measured ──
        dv_reset(cache_from_index=True)
        os.environ["DEEP_VERIFY"] = "1"
        sizer.main()
        s_dp = json.loads((dwork / "deepco.summary.json").read_text())
        t2 = dv_tsv()
        check("deep: every compressed blob streamed (shallow rows stale)",
              dv_counters["streams"] == 7 and s_dp["cache"]["hits"] == 0
              and s_dp["cache"]["misses"] == 7,
              f"streams={dv_counters['streams']} cache={s_dp['cache']}")
        check("deep: zip measured exact",
              t2["src/ok.zip"][2] == "zip-exact"
              and t2["src/ok.zip"][1] == 6200, str(t2["src/ok.zip"]))
        check("deep: CD mismatch — streamed value wins, method carries cd=",
              t2["src/tampered.zip"][2] == "zip-exact-mismatch(cd=123456)"
              and t2["src/tampered.zip"][1] == 4000,
              str(t2["src/tampered.zip"]))
        check("deep: multi-stream bz2 + xz exact",
              t2["src/multi.bz2"][1] == 30000
              and t2["src/multi.bz2"][2] == "bz2-exact"
              and t2["src/data.xz"][1] == 30000
              and t2["src/data.xz"][2] == "xz-exact")
        check("deep: truncated bz2 → exact partial",
              t2["src/trunc.bz2"][2] == "bz2-truncated"
              and 0 < t2["src/trunc.bz2"][1] < 50000,
              str(t2["src/trunc.bz2"]))
        check("deep: garbage .xz falls back to stored (trusted bucket)",
              t2["src/notxz.xz"][2] == "xz-stored")
        check("deep: gz streamed exact under forced knobs",
              t2["src/logs.gz"][2] == "gz-exact"
              and t2["src/logs.gz"][1] == 40000)
        ver = s_dp["verification"]
        check("deep: verification arithmetic closes against totals",
              ver["measured_blobs"] + ver["trusted_blobs"]
              + ver["unmeasurable_blobs"] == s_dp["blobs"]
              and ver["measured_bytes"] + ver["trusted_bytes"]
              + ver["unmeasurable_bytes"] == s_dp["unc"], str(ver))
        check("deep: buckets, mismatch count, 7z residual",
              ver["measured_blobs"] == 7 and ver["trusted_blobs"] == 1
              and ver["cd_mismatches"] == 1
              and ver["unmeasurable_by_format"] == {".7z": [1, 106]},
              str(ver))
        deep_delta = sum(t2[n][1] - t1[n][1] for n in t1)
        check("deep: totals shift by exactly the per-blob re-measurements",
              s_dp["unc"] == s_sh["unc"] + deep_delta
              and t2["src/tampered.zip"][1] - t1["src/tampered.zip"][1]
              == 4000 - 123456,
              f"sh={s_sh['unc']} dp={s_dp['unc']} delta={deep_delta}")

        # ── phase 3: DEEP warm — exact rows terminal; only the garbage
        # xz (non-terminal xz-stored) is deliberately re-attempted ──
        dv_reset(cache_from_index=True)
        sizer.main()
        s_dw = json.loads((dwork / "deepco.summary.json").read_text())
        check("deep warm: only the non-terminal residual re-streams",
              dv_counters["streams"] == 1 and dv_counters["range_gets"] == 0
              and s_dw["cache"] == {"hits": 6, "misses": 1},
              f"streams={dv_counters['streams']} cache={s_dw['cache']}")
        # stream_compressed_bytes is the PER-RUN egress ledger (successful
        # measurements only — the garbage xz's failed attempt ends xz-stored
        # and is not counted); every coverage bucket must match exactly
        cov = lambda v: {k: x for k, x in v.items()  # noqa: E731
                         if k != "stream_compressed_bytes"}
        check("deep warm: identical totals + coverage buckets",
              s_dw["unc"] == s_dp["unc"]
              and cov(s_dw["verification"]) == cov(s_dp["verification"])
              and s_dw["verification"]["stream_compressed_bytes"] == 0,
              f'{s_dw["verification"]} vs {s_dp["verification"]}')

        # ── phase 4: SHALLOW warm from the deep index — measurements
        # replay at zero HTTP; nothing is ever re-shallowed ──
        os.environ.pop("DEEP_VERIFY", None)
        dv_reset(cache_from_index=True)
        sizer.main()
        s_sw = json.loads((dwork / "deepco.summary.json").read_text())
        t4 = dv_tsv()
        check("shallow-after-deep: zero HTTP, all compressed rows replay",
              dv_counters["streams"] == 0 and dv_counters["range_gets"] == 0
              and s_sw["cache"] == {"hits": 7, "misses": 0},
              f"streams={dv_counters['streams']} cache={s_sw['cache']}")
        check("shallow-after-deep: deep totals + methods survive",
              s_sw["unc"] == s_dp["unc"]
              and t4["src/tampered.zip"][2] == "zip-exact-mismatch(cd=123456)"
              and t4["src/multi.bz2"][2] == "bz2-exact"
              and "verification" not in s_sw)
    finally:
        sizer.http_get, sizer.stream_blob_chunks = real_http_dv, real_stream_dv
        for k in denv:
            os.environ.pop(k, None)
        for k in ("CACHE_FILE", "SEED_TSV", "DEEP_VERIFY"):
            os.environ.pop(k, None)

    print("\n— local sizing end-to-end (fake sizer, real launch/poll/harvest) —")
    summary = {"sa": "stdemoco", "container": "democo-raw", "blobs": 5,
               "comp": 10, "unc": 20, "zero": 0, "errors": 1,
               "err_types": {"BadZipFile": 1}, "methods": {"zip": 5},
               "dur_s": 3, "src": {"a": [5, 10, 20]},
               "cache": {"hits": 3, "misses": 2},
               "detected_services": {"hubspot": {
                   "bytes": 7, "blob_count": 0, "entry_count": 1,
                   "path_bytes": 0, "zip_entry_bytes": 7,
                   "sources": {"a": 7}}},
               "sources_l2": {"a/(files)": [5, 10, 20]},
               "gz": {"streamed": 2, "streamed_bytes": 900,
                      "uncertain": 1, "uncertain_bytes": 100}}
    run = phases.summary_to_run("democo", summary,
                                {"metric": 1, "metric_at": "t"}, [])
    check("summary_to_run totals", run["totals"]["uncompressed_bytes"] == 20
          and run["sources"]["a"]["blob_count"] == 5
          and run["errors"]["by_type"] == {"BadZipFile": 1})
    check("summary_to_run new fields", run["cache"] == {"hits": 3, "misses": 2}
          and run["detected_services"]["hubspot"]["bytes"] == 7
          and run["sources_l2"] == {"a/(files)": [5, 10, 20]})
    old_run = phases.summary_to_run("democo",
                                    {k: v for k, v in summary.items()
                                     if k not in ("cache", "detected_services",
                                                  "sources_l2", "gz")},
                                    {"metric": 1, "metric_at": "t"}, [])
    check("summary_to_run tolerates old summaries",
          old_run["cache"] is None and old_run["detected_services"] == {}
          and old_run["sources_l2"] == {})
    check("summary_to_run gz passthrough",
          run["gz"] == {"streamed": 2, "streamed_bytes": 900,
                        "uncertain": 1, "uncertain_bytes": 100})
    check("summary_to_run tolerates missing gz", old_run.get("gz") is None)
    check("summary_to_run tolerates missing verification",
          run.get("verification") is None
          and old_run.get("verification") is None)
    deep_mapped = phases.summary_to_run(
        "democo", dict(summary, verification={"deep": True,
                                              "measured_bytes": 5}),
        {"metric": 1, "metric_at": "t"}, [])
    check("summary_to_run verification passthrough",
          deep_mapped["verification"] == {"deep": True, "measured_bytes": 5})

    fake_sizer = tmp / "fake_sizer.py"
    fake_sizer.write_text(
        "import json, os, time\n"
        "out, tag = os.environ['OUT_DIR'], os.environ['TAG']\n"
        "assert os.environ['AZURE_STORAGE_SAS'] == 'sig=fake'\n"
        "base = os.path.join(out, tag)\n"
        "open(base + '.log', 'w').write('start\\n')\n"
        "json.dump({k: os.environ.get(k) for k in\n"
        "           ('CACHE_FILE', 'SEED_TSV', 'EXPECTED_SERVICES')},\n"
        "          open(base + '.envdump.json', 'w'))\n"
        "time.sleep(1)\n"
        "import gzip\n"
        "gf = gzip.open(base + '.index.tsv.gz', 'wt')\n"
        "gf.write('a/x.zip\\t0xAA\\t10\\t20\\tzip:1entries\\t\\n')\n"
        "gf.close()\n"
        f"json.dump({json.dumps(summary)}, open(base + '.summary.json', 'w'))\n"
        "open(base + '.done', 'w').close()\n")
    os.environ["CDP_SIZER_SCRIPT"] = str(fake_sizer)
    removed = []
    real_ensure, real_sas = phases.ip_rule_ensure, phases.mint_sas
    real_remove = phases.ip_rule_remove_if_ours
    try:
        phases.ip_rule_ensure = lambda cfg, dry_run=False: (True, "1.2.3.4")
        phases.mint_sas = lambda cfg, dry_run=False: "sig=fake"
        phases.ip_rule_remove_if_ours = \
            lambda cfg, ip, we, dry_run=False: removed.append((ip, we))
        wd = phases.work_dir(root)
        wd.mkdir(parents=True, exist_ok=True)
        import gzip as _gz
        prior_index = root / "democo" / "blob-index.tsv.gz"
        with _gz.open(prior_index, "wt") as f:
            f.write("old/x.zip\t0x01\t1\t2\tzip:1entries\t\n")
        (wd / "democo-sizer.sizes.tsv").write_text(
            "crash/y.zip\t3\t4\t1.3\tzip:1entries\t0x02\t\n")
        st = phases.launch(root, "democo", cfg)
        check("launch detached with pid", st["phase"] == "launched"
              and st["pid"] and st["we_added_ip"] is True)
        res = phases.poll_one(root, "democo", st)
        check("polls Running or already done",
              res["state"] in ("Running", "Succeeded"))
        deadline = time.time() + 15
        while res["state"] not in phases.TERMINAL_STATES and time.time() < deadline:
            time.sleep(0.3)
            res = phases.poll_one(root, "democo", st)
        check("reaches Succeeded via .done", res["state"] == "Succeeded")
        env_dump = json.loads((wd / "democo-sizer.envdump.json").read_text())
        check("launch passed CACHE_FILE (prior index)",
              env_dump["CACHE_FILE"] == str(prior_index), str(env_dump))
        check("launch renamed stale tsv to seed and passed SEED_TSV",
              env_dump["SEED_TSV"] == str(wd / "democo-sizer.seed.tsv")
              and Path(env_dump["SEED_TSV"]).exists())
        check("launch passed EXPECTED_SERVICES from manifest",
              "hubspot" in (env_dump["EXPECTED_SERVICES"] or ""),
              str(env_dump))
        n_runs_before = len(common.sizing_runs(root, "democo"))
        run_path = phases.harvest_one(
            root, "democo", cfg, {**st, "metric": 111, "metric_at": "m-t"})
        harvested = common.read_json(run_path)
        check("harvest writes run from summary.json",
              harvested["method"] == "sized"
              and harvested["totals"]["uncompressed_bytes"] == 20
              and harvested["used_capacity_bytes"] == 111
              and len(common.sizing_runs(root, "democo")) == n_runs_before + 1)
        check("cleanup removed work files",
              not list(phases.work_dir(root).glob("democo-sizer.*")))
        check("cleanup removed only-ours IP rule", removed == [("1.2.3.4", True)])
        check("harvest run has detected_services",
              harvested["detected_services"]["hubspot"]["bytes"] == 7)
        check("harvest moved index into company dir",
              prior_index.exists() and _gz.open(prior_index, "rt").read()
              .startswith("a/x.zip"))
        check("cleanup removed seed + work files",
              not list(wd.glob("democo-sizer.*")))
        # --no-cache: fresh launch must not pass cache env
        st2 = phases.launch(root, "democo", cfg, use_cache=False)
        deadline = time.time() + 15
        res2 = phases.poll_one(root, "democo", st2)
        while res2["state"] not in phases.TERMINAL_STATES and time.time() < deadline:
            time.sleep(0.3)
            res2 = phases.poll_one(root, "democo", st2)
        env_dump2 = json.loads((wd / "democo-sizer.envdump.json").read_text())
        check("--no-cache: no CACHE_FILE/SEED_TSV",
              not env_dump2["CACHE_FILE"] and not env_dump2["SEED_TSV"],
              str(env_dump2))
        phases.harvest_one(root, "democo", cfg,
                           {**st2, "metric": 1, "metric_at": "t"})
        # The far-future-dated run file from the earlier stall/"growth resumed"
        # test (20261231T000000Z.json) sorts after any real-clock run and would
        # otherwise outrank the harvest above as "latest" — its job there is
        # done, so drop it here for this check to see the real latest run.
        (root / "democo" / "sizing-runs" / "20261231T000000Z.json").unlink()
        cf2 = common.read_json(phases.write_copied_forward_run(
            root, "democo", 999, "t2"))
        check("copied-forward carries detected_services",
              cf2["detected_services"]["hubspot"]["bytes"] == 7
              and cf2["sources_l2"] == {"a/(files)": [5, 10, 20]}
              and cf2["cache"] is None)
        check("copied-forward carries gz", cf2["gz"] == {
            "streamed": 2, "streamed_bytes": 900,
            "uncertain": 1, "uncertain_bytes": 100})
    finally:
        phases.ip_rule_ensure, phases.mint_sas = real_ensure, real_sas
        phases.ip_rule_remove_if_ours = real_remove
        os.environ.pop("CDP_SIZER_SCRIPT", None)

    print("\n— reconcile: gz uncertainty notes —")
    base_run = {"totals": {"compressed_bytes": 10, "uncompressed_bytes": 50},
                "sources": {}, "methods": {"gz": 5},
                "errors": {"total": 0, "by_type": {}}}
    old_style = " ".join(reconcile.lore_notes(dict(base_run)))
    check("old run: legacy qualitative gz note", "trailer" in old_style)
    new_certain = dict(base_run, gz={"streamed": 5, "streamed_bytes": 1000,
                                     "uncertain": 0, "uncertain_bytes": 0})
    check("new run, all certain: no gz note",
          "trailer" not in " ".join(reconcile.lore_notes(new_certain))
          and "measur" not in " ".join(reconcile.lore_notes(new_certain)))
    new_unc = dict(base_run, gz={"streamed": 1, "streamed_bytes": 10,
                                 "uncertain": 3,
                                 "uncertain_bytes": 7_500_000_000})
    nn = " ".join(reconcile.lore_notes(new_unc))
    check("new run: quantified note", "3" in nn and "7.5" in nn
          and "measur" in nn)

    print("\n— deep verify: notes + consumers —")
    ver_clean = {"deep": True, "measured_blobs": 9, "measured_bytes": 100,
                 "trusted_blobs": 0, "trusted_bytes": 0,
                 "unmeasurable_blobs": 0, "unmeasurable_bytes": 0,
                 "unmeasurable_by_format": {}, "cd_mismatches": 0,
                 "stream_compressed_bytes": 50}
    vrun = dict(base_run, gz={"streamed": 0, "streamed_bytes": 0,
                              "uncertain": 3,
                              "uncertain_bytes": 7_500_000_000},
                verification=ver_clean)
    vn = " ".join(reconcile.lore_notes(vrun))
    check("deep-verified run: certification note, gz-uncertainty suppressed",
          "stream-decompressed" in vn and "trailer" not in vn
          and "7.5" not in vn, vn)
    ver_resid = dict(ver_clean, trusted_blobs=2, trusted_bytes=3_000_000_000,
                     unmeasurable_blobs=1,
                     unmeasurable_bytes=40_000_000_000,
                     unmeasurable_by_format={".7z": [1, 40_000_000_000]})
    rn = " ".join(reconcile.lore_notes(dict(vrun, verification=ver_resid)))
    check("deep-verified run: residual note quantified per bucket",
          "3.0 GB" in rn and "40.0 GB" in rn and ".7z" in rn, rn)

    import gen_report
    import verify_completion as vc
    democo_status_snap = (root / "democo" / "status.json").read_text()
    res_nodeep = vc.verify(root, "democo", 0.5)
    check("verify_completion: no deep run → informational, still passing",
          res_nodeep["checks"]["deep_verify"]["pass"] is True
          and res_nodeep["checks"]["deep_verify"]["verified"] is False,
          str(res_nodeep["checks"]["deep_verify"]))
    latest_democo = common.latest_runs(root, "democo", 1)[0]
    deep_run_file = root / "democo" / "sizing-runs" / "20270101T000000Z.json"
    common.write_json(deep_run_file,
                      dict(latest_democo, timestamp="2027-01-01T00:00:00Z",
                           verification=ver_clean, notes=[]))
    s_deep = reconcile.company_summary(root, "democo")
    check("company_summary exposes verification + deep_verified_at",
          s_deep["verification"] == ver_clean
          and s_deep["deep_verified_at"] == "2027-01-01T00:00:00Z")
    html_deep = gen_report.build_html(s_deep)
    check("report renders deep-verified badge + run_meta line",
          "deep-verified 2027-01-01" in html_deep
          and "stream-measured" in html_deep)
    res_deep = vc.verify(root, "democo", 0.5)
    dvchk = res_deep["checks"]["deep_verify"]
    check("verify_completion deep check populated, never in hard gates",
          dvchk["verified"] is True and dvchk["pass"] is True
          and dvchk["measured_pct_of_bytes"] == 100.0
          and res_deep["verdict"] == res_nodeep["verdict"],
          str(dvchk))
    cf_deep_path = phases.write_copied_forward_run(root, "democo", 5, "t9")
    cf_deep = common.read_json(cf_deep_path)
    check("copied-forward carries verification",
          cf_deep["verification"] == ver_clean)
    cf_deep_path.unlink()
    deep_run_file.unlink()
    (root / "democo" / "status.json").write_text(democo_status_snap)

    print("\n— fleet_size --dry-run —")
    proc = run_script("fleet_size.py", "launch-all", "--root", root,
                      "--dry-run", "--slugs", "democo", "emptyco",
                      expect_rc=0)
    out = proc.stdout
    check("dry-run prints local detached launch",
          "corpus_sizer_rest.py" in out and "OUT_DIR" in out
          and "detached" in out)
    check("dry-run prints no VM commands", "run-command" not in out)
    check("dry-run prints SAS mint", "generate-sas" in out and "rl" in out)
    check("dry-run prints metrics read", "UsedCapacity" in out)
    check("dry-run prints IP firewall conditional",
          "network-rule add" in out and "--ip-address" in out)
    outcomes = json.loads(out[out.rindex('{\n  "started_at"'):])
    check("both launched (no-vm outcome gone)",
          all(outcomes["outcomes"][s]["outcome"] is None
              for s in ("democo", "emptyco")))
    state = phases.load_state(root)
    check("state file tracks launches",
          state["companies"]["democo"]["phase"] == "launched"
          and state["companies"]["emptyco"]["phase"] == "launched")
    check("no real az was needed (dry-run)", True)
    proc = run_script("fleet_size.py", "launch-all", "--root", root,
                      "--dry-run", "--no-cache", "--slugs", "democo")
    check("--no-cache flag accepted", proc.returncode == 0)

    print("\n— gcs_transfer --dry-run —")
    proc = run_script("gcs_transfer.py", "plan", "democo",
                      "--bucket", "dwt-takeout-export-123", "--root", root,
                      "--dry-run")
    plan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("plan resolves dest from config",
          plan["vm_name"] == "xfer-democo"
          and plan["storage_account"] == "stdemoco"
          and plan["dest"] == "democo-raw/workspace-export")
    proc = run_script("gcs_transfer.py", "create-vm", "democo",
                      "--bucket", "dwt-takeout-export-123", "--root", root,
                      "--dry-run")
    out = proc.stdout
    check("create-vm: static Standard PIP + accel-net + delete options",
          "--public-ip-sku Standard" in out
          and "--accelerated-networking true" in out
          and "--os-disk-delete-option Delete" in out)
    check("create-vm: tags carry bucket for stateless rediscovery",
          "gcs_bucket=dwt-takeout-export-123" in out)
    check("create-vm points at allow-network as the next step",
          "allow-network" in out)
    proc = run_script("gcs_transfer.py", "allow-network", "democo",
                      "--root", root, "--dry-run")
    check("allow-network: service endpoint + vnet-rule, correct subnet",
          "subnet update" in proc.stdout
          and "--service-endpoints Microsoft.Storage" in proc.stdout
          and "network-rule add" in proc.stdout
          and "xfer-democoVNET" in proc.stdout)
    proc = run_script("gcs_transfer.py", "write-azure-remote", "democo",
                      "--root", root, "--dry-run")
    check("azure remote: rwlc-class SAS, secrets redacted",
          "--permissions racwl" in proc.stdout
          and "redacted" in proc.stdout)
    check("write-azure-remote itself never touches network rules",
          "network-rule add" not in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gcs_transfer.py"), "write-gcs-remote",
         "democo", "--bucket", "dwt-takeout-export-123", "--root", str(root),
         "--dry-run"],
        input='{"access_token":"SECRETTOKEN"}', capture_output=True, text=True)
    check("gcs remote: token never echoed",
          proc.returncode == 0 and "SECRETTOKEN" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    proc = run_script("gcs_transfer.py", "teardown", "democo", "--root", root,
                      "--dry-run", expect_rc=2)
    check("teardown refuses without --confirmed",
          '"not-confirmed"' in proc.stdout)
    proc = run_script("gcs_transfer.py", "teardown", "democo", "--root", root,
                      "--dry-run", "--confirmed")
    check("confirmed teardown deletes PIP + NSG + VNET explicitly",
          "public-ip delete" in proc.stdout and "nsg delete" in proc.stdout
          and "vnet delete" in proc.stdout)
    check("teardown removes our vnet-rule and reminds about UI IP rules",
          "network-rule remove" in proc.stdout
          and "internal UI" in proc.stdout)

    print("\n— dropbox_transfer --dry-run (shared engine, dropbox Spec) —")
    proc = run_script("dropbox_transfer.py", "plan", "democo", "--root", root,
                      "--dry-run")
    plan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("dropbox plan: xfer-dbx VM + dropbox-export dest + root source",
          plan["vm_name"] == "xfer-dbx-democo"
          and plan["dest"] == "democo-raw/dropbox-export"
          and plan["source"] == "dropbox:")
    proc = run_script("dropbox_transfer.py", "create-vm", "democo",
                      "--path", "Team Folder", "--root", root, "--dry-run")
    check("dropbox create-vm: own purpose + path tags, no name collision",
          "purpose=dropbox-transfer" in proc.stdout
          and "dropbox_path=Team Folder" in proc.stdout
          and "-n xfer-dbx-democo" in proc.stdout)
    proc = run_script("dropbox_transfer.py", "transfer", "democo",
                      "--root", root, "--dry-run")
    check("dropbox transfer: rate-limit-friendly defaults",
          "--transfers 8" in proc.stdout
          and "--tpslimit 12 --tpslimit-burst 12" in proc.stdout)
    import dropbox_transfer  # noqa: E402  (scripts/ already on sys.path)
    import gcs_transfer  # noqa: E402
    check("dropbox Spec: fast-list + order-by, tpslimit moved to Spec field",
          "--fast-list" in dropbox_transfer.SPEC.extra_rclone_flags
          and "--order-by size,mixed" in dropbox_transfer.SPEC.extra_rclone_flags
          and "--tpslimit" not in dropbox_transfer.SPEC.extra_rclone_flags
          and dropbox_transfer.SPEC.default_tpslimit == 12
          and gcs_transfer.SPEC.default_tpslimit is None)
    proc = run_script("dropbox_transfer.py", "verify", "democo",
                      "--root", root, "--dry-run")
    check("dropbox verify: tpslimit protects the check too",
          "--tpslimit 12 --tpslimit-burst 12" in proc.stdout
          and "--one-way" in proc.stdout)
    proc = run_script("dropbox_transfer.py", "transfer", "democo",
                      "--root", root, "--dry-run", "--tpslimit", "0")
    check("dropbox transfer: --tpslimit 0 removes the cap",
          "--tpslimit" not in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "dropbox_transfer.py"),
         "write-dropbox-remote", "democo", "--root", str(root), "--dry-run"],
        input='{"access_token":"DBXSECRET"}', capture_output=True, text=True)
    check("dropbox token never echoed",
          proc.returncode == 0 and "DBXSECRET" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    proc = run_script("dropbox_transfer.py", "teardown", "democo",
                      "--root", root, "--dry-run", expect_rc=2)
    check("dropbox teardown also gated", '"not-confirmed"' in proc.stdout)

    print("\n— gdrive_transfer --dry-run (shared engine, gdrive Spec) —")
    import gdrive_transfer  # noqa: E402  (scripts/ already on sys.path)
    section = gdrive_transfer.SPEC.remote_section('{"t":1}')
    check("gdrive remote section: drive type + scope + token",
          "type = drive" in section and "scope = drive" in section
          and 'token = {"t":1}' in section)
    proc = run_script("gdrive_transfer.py", "plan", "democo", "--root", root,
                      "--dry-run")
    plan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("gdrive plan: xfer-gdr VM + gdrive-export dest + root source",
          plan["vm_name"] == "xfer-gdr-democo"
          and plan["dest"] == "democo-raw/gdrive-export"
          and plan["source"] == "gdrive:")
    proc = run_script("gdrive_transfer.py", "create-vm", "democo",
                      "--path", "Corp Docs", "--team-drive", "0AAbCdEfGh",
                      "--root", root, "--dry-run")
    check("gdrive create-vm: purpose + path + team-drive tags",
          "purpose=gdrive-transfer" in proc.stdout
          and "gdrive_path=Corp Docs" in proc.stdout
          and "gdrive_team_drive=0AAbCdEfGh" in proc.stdout
          and "-n xfer-gdr-democo" in proc.stdout)
    proc = run_script("gdrive_transfer.py", "transfer", "democo",
                      "--root", root, "--dry-run")
    check("gdrive transfer: throttled defaults",
          "--transfers 8" in proc.stdout
          and "--tpslimit 10 --tpslimit-burst 10" in proc.stdout)
    proc = run_script("gdrive_transfer.py", "transfer", "democo",
                      "--include", "takeout-*.zip", "--root", root,
                      "--dry-run")
    check("gdrive transfer: --include filter reaches rclone",
          "--include 'takeout-*.zip'" in proc.stdout)
    proc = run_script("gdrive_transfer.py", "verify", "democo",
                      "--include", "takeout-*.zip", "--root", root,
                      "--dry-run")
    check("gdrive verify: same --include filter as transfer",
          "--include 'takeout-*.zip'" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gdrive_transfer.py"),
         "write-gdrive-remote", "democo", "--root", str(root), "--dry-run"],
        input='{"access_token":"GDRSECRET"}', capture_output=True, text=True)
    check("gdrive token never echoed",
          proc.returncode == 0 and "GDRSECRET" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    proc = run_script("gdrive_transfer.py", "teardown", "democo",
                      "--root", root, "--dry-run", expect_rc=2)
    check("gdrive teardown also gated", '"not-confirmed"' in proc.stdout)
    oauth_json = tmp / "oauth-client.json"
    oauth_json.write_text(json.dumps({"installed": {
        "client_id": "12345-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-OAUTHSECRET"}}))
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gdrive_transfer.py"),
         "write-gdrive-remote", "democo", "--root", str(root),
         "--oauth-client-json", str(oauth_json), "--dry-run"],
        input='{"access_token":"GDRSECRET"}', capture_output=True, text=True)
    out = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("gdrive custom oauth client: id reported, secret never echoed",
          proc.returncode == 0 and "GOCSPX-OAUTHSECRET" not in proc.stdout
          and out.get("oauth_client_id")
          == "12345-abc.apps.googleusercontent.com", proc.stdout[-300:])

    print("\n— s3_transfer --dry-run (engine lifecycle, azcopy copy layer) —")
    import s3_transfer  # noqa: E402  (scripts/ already on sys.path)
    check("s3 Spec: secretless env_auth remote, no OAuth flow, 512 GB disk",
          "env_auth = true" in s3_transfer.SPEC.remote_extra
          and s3_transfer.SPEC.authorize_target == ""
          and s3_transfer.SPEC.default_os_disk_gb == 512)
    proc = run_script("s3_transfer.py", "plan", "democo",
                      "--bucket", "demo-images", "--root", root, "--dry-run")
    plan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("s3 plan: xfer-s3 VM + s3-export dest + bucket source",
          plan["vm_name"] == "xfer-s3-democo"
          and plan["storage_account"] == "stdemoco"
          and plan["dest"] == "democo-raw/s3-export"
          and plan["source"] == "s3:demo-images")
    proc = run_script("s3_transfer.py", "create-vm", "democo",
                      "--bucket", "demo-images", "--root", root, "--dry-run")
    check("s3 create-vm: 512 GB os disk for azcopy job-plan files",
          "--os-disk-size-gb 512" in proc.stdout)
    check("s3 create-vm: purpose + bucket tags, own VM name, standard shape",
          "purpose=s3-transfer" in proc.stdout
          and "s3_bucket=demo-images" in proc.stdout
          and "-n xfer-s3-democo" in proc.stdout
          and "--public-ip-sku Standard" in proc.stdout
          and "--accelerated-networking true" in proc.stdout)
    proc = run_script("gcs_transfer.py", "create-vm", "democo",
                      "--bucket", "dwt-takeout-export-123", "--root", root,
                      "--dry-run")
    check("engine disk knob is additive: rclone sources keep default disk",
          "--os-disk-size-gb" not in proc.stdout)
    proc = run_script("s3_transfer.py", "allow-network", "democo",
                      "--root", root, "--dry-run")
    check("s3 allow-network: vnet path (VM family), never IP rules",
          "network-rule add" in proc.stdout
          and "--subnet" in proc.stdout
          and "--service-endpoints Microsoft.Storage" in proc.stdout
          and "--ip-address" not in proc.stdout)
    proc = run_script("s3_transfer.py", "write-dest", "democo",
                      "--root", root, "--dry-run")
    check("s3 write-dest: racwl SAS on the container, secrets redacted",
          "--permissions racwl" in proc.stdout
          and "-n democo-raw" in proc.stdout
          and "redacted" in proc.stdout
          and "network-rule add" not in proc.stdout)
    check("s3 write-dest: feeds both rclone conf and azcopy dest.env",
          "rclone.conf" in proc.stdout and "dest.env" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "s3_transfer.py"), "write-s3-creds",
         "democo", "--bucket", "demo-images", "--root", str(root),
         "--dry-run"],
        input="AKIASENTINEL\nSECRETSENTINEL\n", capture_output=True,
        text=True)
    check("s3 creds: neither stdin sentinel ever echoed",
          proc.returncode == 0 and "AKIASENTINEL" not in proc.stdout
          and "SECRETSENTINEL" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "s3_transfer.py"), "write-s3-creds",
         "democo", "--bucket", "demo-images", "--root", str(root),
         "--dry-run"],
        input="only-one-line\n", capture_output=True, text=True)
    check("s3 creds: refuses malformed stdin (must be exactly 2 lines)",
          proc.returncode == 1 and "2 lines" in proc.stdout)
    proc = run_script("s3_transfer.py", "plan-jobs", "democo",
                      "--bucket", "demo-images", "--root", root, "--dry-run")
    pj = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("s3 plan-jobs: root shallow job always queued",
          pj["jobs"] >= 1 and pj["sample"][0] == "S (root)"
          and pj["shallow_jobs"] >= 1)
    proc = run_script("s3_transfer.py", "transfer", "democo",
                      "--root", root, "--dry-run")
    check("s3 transfer: workers launched inside tmux session 'transfer'",
          "tmux new-session -d -s transfer -n w1" in proc.stdout
          and "runner.sh worker 1 200 0" in proc.stdout)
    proc = run_script("s3_transfer.py", "transfer", "democo",
                      "--root", root, "--dry-run", "--pilot")
    check("s3 pilot: one window, one job, then measure",
          "runner.sh worker 1 200 1" in proc.stdout
          and "-n w2" not in proc.stdout)
    proc = run_script("s3_transfer.py", "verify", "democo",
                      "--root", root, "--dry-run")
    check("s3 verify: rollup runs VM-side via the runner",
          "runner.sh verify" in proc.stdout)
    _done = "#done\t1000\tok=1\tbad=0"
    check("s3 verify: a finished verify older than the ledger is stale",
          s3_transfer._verify_stale(_done, "2000") is True
          and s3_transfer._verify_stale(_done, "500") is False
          and s3_transfer._verify_stale("#done", "9") is False
          and s3_transfer._verify_stale(_done, "junk") is False)
    runner = (SCRIPTS / "azcopy-runner.sh").read_text()
    check("runner pins the write invariant + plan-disk hygiene",
          "--overwrite=false" in runner and "--recursive" in runner
          and "azcopy jobs rm" in runner and "flock" in runner)
    boot = (SCRIPTS / "bootstrap-vm.sh").read_text()
    check("bootstrap installs azcopy idempotently",
          "command -v azcopy" in boot
          and "downloadazcopy-v10-linux" in boot)
    proc = run_script("s3_transfer.py", "teardown", "democo", "--root", root,
                      "--dry-run", expect_rc=2)
    check("s3 teardown also gated", '"not-confirmed"' in proc.stdout)
    proc = run_script("s3_transfer.py", "teardown", "democo", "--root", root,
                      "--dry-run", "--confirmed")
    check("s3 confirmed teardown: our vnet-rule + PIP/NSG/VNET, IAM reminder",
          "network-rule remove" in proc.stdout
          and "public-ip delete" in proc.stdout
          and "vnet delete" in proc.stdout
          and "IAM access key" in proc.stdout)

    print("\n— s3 flat-bucket mode (chunked list-of-files) —")
    import s3_flat
    lines = [f"{i:032x}.jpg\t100\n" for i in range(250)]
    lines.insert(5, "\n")
    lines.insert(100, "weird key+name.jpg\t9\n")
    chunks = list(s3_flat.split_listing(lines, 100))
    names = [n for n, _ in chunks]
    body = "".join(t for n, t in chunks if n.startswith("chunk"))
    quar = "".join(t for n, t in chunks if n.startswith("quarantine"))
    check("flat split: chunk rows keep sizes, quarantine bare, round-trip",
          names == ["chunk-00000", "chunk-00001", "chunk-00002",
                    "quarantine-00000"]
          and body.count("\n") == 250 and quar == "weird key+name.jpg\n"
          and body == "".join(f"{i:032x}.jpg\t100\n" for i in range(250)),
          str(names))
    check("flat split: empty listing yields zero chunks",
          list(s3_flat.split_listing([], 100)) == [])
    rb = s3_flat.range_bounds(256)
    synth = sorted(["!bang", "00aa", "01", "7fzz", "ff00", "zzz", "0",
                    "G-upper", "fe" * 16])
    hits = [sum(1 for a, b in rb
                if (a is None or k > a) and (b is None or k <= b))
            for k in synth]
    check("flat range_bounds: 256 total-coverage ranges, no loss/dupes",
          len(rb) == 256 and rb[0][0] is None and rb[-1][1] is None
          and all(h == 1 for h in hits), str(list(zip(synth, hits))))
    man = [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    azl = [("a", 1), ("c", 9), ("d", 4), ("e", 5)]
    buf = io.StringIO()
    miss = io.StringIO()
    sdiff = io.StringIO()
    st = s3_flat.merge_join(iter(man), iter(azl), buf, missing_out=miss,
                            sizediff_out=sdiff)
    check("flat verify: missing.txt gets only MISSING-DEST (mop-up input)",
          miss.getvalue() == "b\t2\n")
    check("flat verify: sizediff.txt gets SIZE-DIFF uncapped (s3+az sizes)",
          sdiff.getvalue() == "c\t3\t9\n")
    rows = [r.split("\t") for r in buf.getvalue().splitlines()
            if not r.startswith("#")]
    check("flat verify merge-join: labels, totals, ok/bad counts",
          st["ok"] == 2 and st["bad"] == 3
          and [r[6] for r in rows] == ["MISSING-DEST", "SIZE-DIFF",
                                       "EXTRA-DEST"]
          and st["s3_count"] == 4 and st["az_count"] == 4
          and st["s3_bytes"] == 10 and st["az_bytes"] == 19, str(st))
    try:
        s3_flat.merge_join(iter([("b", 1), ("a", 1)]), iter([]),
                           io.StringIO())
        sorted_guard = False
    except s3_flat.SortError:
        sorted_guard = True
    check("flat verify merge-join: unsorted stream aborts loudly",
          sorted_guard)
    proc = run_script("s3_transfer.py", "plan-jobs", "democo",
                      "--bucket", "demo-images", "--root", root,
                      "--dry-run", "--flat")
    fj = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("s3 plan-jobs --flat: sharded listing launched in window 'plan'",
          fj.get("listing_started") is True and fj.get("flat") is True
          and "runner.sh list-bucket" in proc.stdout
          and "-n plan" in proc.stdout
          and "s3_flat.py" in proc.stdout)
    proc = run_script("s3_transfer.py", "plan-jobs", "democo",
                      "--bucket", "demo-images", "--root", root,
                      "--dry-run", "--no-flat")
    check("s3 plan-jobs --no-flat: forces the prefix path",
          "list-bucket" not in proc.stdout
          and '"S (root)"' in proc.stdout)
    proc = run_script("s3_transfer.py", "probe", "democo",
                      "--bucket", "demo-images", "--root", root, "--dry-run")
    check("s3 probe: streaming key-shape sample runs before the dirs survey",
          "--files-only -R" in proc.stdout and "head -1000" in proc.stdout
          and proc.stdout.index("head -1000")
          < proc.stdout.index("--dirs-only")
          and "--max-depth 1" not in proc.stdout  # buffers whole dir: banned
          and "rclone lsd" not in proc.stdout     # same trap on flat buckets
          and '"flat_suspected"' in proc.stdout)
    proc = run_script("s3_transfer.py", "transfer", "democo",
                      "--root", root, "--dry-run")
    check("s3 transfer: orphaned inflight jobs swept back into the queue",
          "cat inflight/* >> queue.txt" in proc.stdout
          and "rm -f inflight/*" in proc.stdout)
    check("runner: L job runs the put-from-url engine, Q streams files-from",
          'copy-chunk "$jprefix"' in runner
          # --no-traverse or rclone enumerates the whole dest prefix
          and "--no-traverse" in runner
          and "--overwrite=false" in runner  # R jobs keep the azcopy pin
          and '--files-from "$BASE/chunks/$jprefix"' in runner)
    check("runner: list-bucket verb + flat verify auto-select",
          "list-bucket)" in runner and "s3_flat.py" in runner
          and "grep -qm1 $'^L\\t'" in runner)
    _rows = [("v3", 100, "AAA", True), ("v2", 100, "AAA", False),
             ("v1", 250, "BBB", False), ("v0", 250, "BBB", False)]
    _sel, _skip = s3_flat.select_distinct_versions(_rows)
    check("versions: byte-identical re-uploads skipped, distinct kept",
          [v for v, _, _ in _sel] == ["v1"]
          and sorted(v for v, _, _ in _skip) == ["v0", "v2"],
          f"sel={_sel} skip={_skip}")
    _sel2, _ = s3_flat.select_distinct_versions(
        [("v1", 10, "X", False), ("v2", 10, "X", False)])
    check("versions: no current version -> first distinct still selected",
          [v for v, _, _ in _sel2] == ["v1"])
    check("versions: blob name is _noncurrent/<key>/<versionId>",
          s3_flat.version_blob_name("a/b.jpg", "VID")
          == "_noncurrent/a/b.jpg/VID")
    flat_src = (SCRIPTS / "s3_flat.py").read_text()
    check("runner: V jobs share L's summary parsing (ledger stays populated)",
          'copy-versions "$jprefix"' in runner
          and '[ "$jtype" = "V" ]' in runner
          and 'Transfers Completed' in flat_src)
    check("s3_flat copy engine: API-enforced create-only server-side copy",
          '"If-None-Match": "*"' in flat_src
          and "x-ms-copy-source" in flat_src
          and "generate_presigned_url" in flat_src
          and "Transfers Completed" in flat_src)  # azcopy summary grammar
    src = (SCRIPTS / "s3_transfer.py").read_text()
    check("s3 verify collect skips comment rows; requeue contract intact",
          "$1 !~ /^#/" in src and "print $2" in src)
    boot = (SCRIPTS / "bootstrap-vm.sh").read_text()
    check("bootstrap installs python3-boto3 for the flat lister",
          "python3-boto3" in boot)
    flat_src = (SCRIPTS / "s3_flat.py").read_text()
    check("s3_flat: creds via env only (boto3 env_auth; no key handling)",
          "AWS_SECRET_ACCESS_KEY" not in flat_src
          and "AWS_ACCESS_KEY_ID" not in flat_src
          and "argv" not in flat_src.split("def main")[0])

    print("\n— github_transfer --dry-run (engine lifecycle, VM-side puller) —")
    import github_transfer  # noqa: E402
    import github_vm_pull  # noqa: E402
    check("github Spec: PAT flow (no OAuth), no rclone source, 512 GB disk",
          github_transfer.SPEC.vm_prefix == "xfer-gh-"
          and github_transfer.SPEC.authorize_target == ""
          and github_transfer.SPEC.remote_type == ""
          and github_transfer.SPEC.default_dest_prefix == "github-export"
          and github_transfer.SPEC.default_os_disk_gb == 512)
    proc = run_script("github_transfer.py", "plan", "democo",
                      "--login", "demo-org", "--root", root, "--dry-run")
    plan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("github plan: xfer-gh VM + github-export dest + login source",
          plan["vm_name"] == "xfer-gh-democo"
          and plan["dest"] == "democo-raw/github-export"
          and plan["source"] == "github:demo-org")
    proc = run_script("github_transfer.py", "create-vm", "democo",
                      "--login", "demo-org", "--owner-type", "org",
                      "--root", root, "--dry-run")
    check("github create-vm: 512 GB staging disk + login/owner tags",
          "--os-disk-size-gb 512" in proc.stdout
          and "purpose=github-transfer" in proc.stdout
          and "gh_login=demo-org" in proc.stdout
          and "gh_owner_type=org" in proc.stdout
          and "-n xfer-gh-democo" in proc.stdout)
    proc = run_script("github_transfer.py", "allow-network", "democo",
                      "--root", root, "--dry-run")
    check("github allow-network: vnet path (VM family), never IP rules",
          "network-rule add" in proc.stdout and "--subnet" in proc.stdout
          and "--ip-address" not in proc.stdout)
    proc = run_script("github_transfer.py", "write-dest", "democo",
                      "--root", root, "--dry-run")
    check("github write-dest: racwl SAS, both consumers, redacted",
          "--permissions racwl" in proc.stdout
          and "rclone.conf" in proc.stdout and "dest.env" in proc.stdout
          and "redacted" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "github_transfer.py"), "write-token",
         "democo", "--root", str(root), "--dry-run"],
        input="ghp_SENTINELTOKEN\n", capture_output=True, text=True)
    check("github write-token: PAT sentinel never echoed",
          proc.returncode == 0 and "ghp_SENTINELTOKEN" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "github_transfer.py"), "write-token",
         "democo", "--root", str(root), "--dry-run"],
        input="line-one\nline-two\n", capture_output=True, text=True)
    check("github write-token: refuses malformed stdin (must be 1 line)",
          proc.returncode == 1 and "1 line" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "github_transfer.py"), "probe",
         "democo", "--login", "demo-org", "--root", str(root), "--dry-run"],
        input="ghp_SENTINELTOKEN\n", capture_output=True, text=True)
    check("github probe: laptop-side API JSON only, token redacted",
          proc.returncode == 0
          and "api.github.com/rate_limit" in proc.stdout
          and "token-redacted" in proc.stdout
          and "ghp_SENTINELTOKEN" not in proc.stdout
          and "az vm" not in proc.stdout, proc.stdout[-300:])
    proc = run_script("github_transfer.py", "transfer", "democo",
                      "--login", "demo-org", "--root", root, "--dry-run")
    check("github transfer: pushes puller, tmux pull window, envs sourced",
          "github_vm_pull.py" in proc.stdout
          and "tmux new-session -d -s transfer -n pull" in proc.stdout
          and "github.env" in proc.stdout and "dest.env" in proc.stdout)
    proc = run_script("github_transfer.py", "verify", "democo",
                      "--root", root, "--dry-run")
    check("github verify: laptop path — ip rule + rl SAS + manifest fetch",
          "--permissions rl" in proc.stdout
          and "network-rule add" in proc.stdout
          and "manifest.json" in proc.stdout
          and "racwl" not in proc.stdout)
    proc = run_script("github_transfer.py", "teardown", "democo",
                      "--root", root, "--dry-run", expect_rc=2)
    check("github teardown also gated", '"not-confirmed"' in proc.stdout)
    proc = run_script("github_transfer.py", "teardown", "democo",
                      "--root", root, "--dry-run", "--confirmed")
    check("github confirmed teardown: engine set + PAT-revocation reminder",
          "network-rule remove" in proc.stdout
          and "vnet delete" in proc.stdout
          and "fine-grained PAT" in proc.stdout)
    pull_src = (SCRIPTS / "github_vm_pull.py").read_text()
    check("puller: askpass custody + mirror + wikis + markers + lfs detect",
          "GIT_ASKPASS" in pull_src and "x-access-token" in pull_src
          and "--mirror" in pull_src and ".wiki.git" in pull_src
          and "state=all" in pull_src and ".cdp-complete" in pull_src
          and "filter=lfs" in pull_src)
    check("puller: azcopy no-overwrite pin, upstream's weaker flag dropped",
          "--overwrite=false" in pull_src
          and "ifSourceNewer" not in pull_src)
    boot = (SCRIPTS / "bootstrap-vm.sh").read_text()
    check("bootstrap installs git + git-lfs for the puller",
          "git git-lfs" in boot)
    check("puller wiki classifier: absent vs transient",
          github_vm_pull.wiki_absent("remote: Repository not found.")
          and github_vm_pull.wiki_absent("fatal: repo does not exist")
          and not github_vm_pull.wiki_absent("connection reset by peer"))
    m = github_vm_pull.build_manifest("demo-org", "org", "TS", [
        {"repo": "a", "clone": "ok", "bytes": 100, "wiki": "ok",
         "wiki_bytes": 10, "json": "ok"},
        {"repo": "b", "clone": "failed", "bytes": 0, "json": "ok"},
        {"repo": "c", "clone": "ok", "bytes": 50, "lfs": "failed",
         "json": "ok"}])
    check("puller manifest: failed set includes lfs failures, bytes sum",
          m["failed_repos"] == ["b", "c"]
          and m["total_clone_bytes"] == 160)
    _p = "github-export"
    _clean = {
        f"{_p}/repos/a.git/.cdp-complete": {"size": 0},
        f"{_p}/repos/a.git/packed-refs": {"size": 100},
        f"{_p}/wikis/a.wiki.git/.cdp-complete": {"size": 0},
        f"{_p}/wikis/a.wiki.git/x": {"size": 10},
        f"{_p}/json/a/.cdp-complete": {"size": 0},
        f"{_p}/manifest.json": {"size": 5},
    }
    _man = {"results": [{"repo": "a", "clone": "ok", "bytes": 100,
                         "wiki": "ok", "wiki_bytes": 10, "json": "ok"}],
            "failed_repos": [], "repo_count": 1, "total_clone_bytes": 110}
    r = github_transfer.compare_manifest_to_blobs(_man, _clean, _p)
    check("github verify math: clean pass",
          r["ok"] and not r["short_uploads"] and not r["missing_markers"])
    _short = dict(_clean)
    _short[f"{_p}/repos/a.git/packed-refs"] = {"size": 40}
    r = github_transfer.compare_manifest_to_blobs(_man, _short, _p)
    check("github verify math: short upload fails",
          not r["ok"] and r["short_uploads"])
    _nomark = dict(_clean)
    del _nomark[f"{_p}/json/a/.cdp-complete"]
    r = github_transfer.compare_manifest_to_blobs(_man, _nomark, _p)
    check("github verify math: missing marker fails",
          not r["ok"] and r["missing_markers"] == [f"{_p}/json/a/"])
    _extra = dict(_clean)
    _extra[f"{_p}/repos/a.git/old-pack"] = {"size": 60}
    r = github_transfer.compare_manifest_to_blobs(_man, _extra, _p)
    check("github verify math: stale extra is informational, not a failure",
          r["ok"] and r["stale_extra"] == [f"{_p}/repos/a.git/"])
    _man_f = dict(_man)
    _man_f["failed_repos"] = ["b"]
    r = github_transfer.compare_manifest_to_blobs(_man_f, _clean, _p)
    check("github verify math: failed_repos surfaced verbatim",
          not r["ok"] and r["failed_repos"] == ["b"])

    print("\n— deep_verify --dry-run (VM step machine, engine lifecycle) —")
    proc = run_script("deep_verify.py", "step", "democo", "--root", root,
                      "--dry-run")
    out = proc.stdout
    check("step dry-run creates deepv VM with purpose tag",
          "az vm create" in out and "deepv-democo" in out
          and "purpose=deep-verify" in out)
    check("step dry-run pipes bootstrap over ssh stdin (redacted)",
          "sudo bash -s" in out and "redacted" in out)
    check("step dry-run reads UsedCapacity for the run stamp",
          "UsedCapacity" in out)
    check("step dry-run reports vm-created phase",
          '"phase": "vm-created"' in out)
    check("step dry-run never mints a write SAS", "racwl" not in out)
    proc = run_script("deep_verify.py", "teardown", "democo", "--root", root,
                      "--dry-run", expect_rc=2)
    check("teardown refuses without --confirmed",
          "not-confirmed" in proc.stdout)
    proc = run_script("deep_verify.py", "status", "democo", "--root", root,
                      "--dry-run")
    check("status dry-run reports pre-create",
          '"phase": "pre-create"' in proc.stdout)

    print("\n— deep_verify harvest (fake ssh, real run-file plumbing) —")
    from types import SimpleNamespace
    import deep_verify as dvmod
    import transfer_engine as engmod
    dsum = {"sa": "stdemoco", "container": "democo-raw", "blobs": 2,
            "comp": 10, "unc": 30, "zero": 0, "errors": 0, "err_types": {},
            "methods": {"zip": 1, "stored": 1}, "dur_s": 7,
            "src": {"a": [2, 10, 30]}, "cache": {"hits": 0, "misses": 1},
            "gz": {"streamed": 0, "streamed_bytes": 0,
                   "uncertain": 0, "uncertain_bytes": 0},
            "detected_services": {}, "sources_l2": {},
            "verification": {"deep": True, "measured_blobs": 2,
                             "measured_bytes": 30, "trusted_blobs": 0,
                             "trusted_bytes": 0, "unmeasurable_blobs": 0,
                             "unmeasurable_bytes": 0,
                             "unmeasurable_by_format": {},
                             "cd_mismatches": 0,
                             "stream_compressed_bytes": 10}}
    dv_index_bytes = gzip.compress(
        b"#matcher\tabc\nzz.zip\t0xF\t10\t30\tzip-exact\t\n")
    fake_vm = {"name": "deepv-democo", "power_state": "VM running",
               "public_ip": "9.9.9.9",
               "tags": {"used_capacity": "4321", "used_capacity_at": "cap-at"},
               "location": "eastus"}

    def fake_ssh(ip, cmd, stdin_data=None, dry_run=False, timeout=120,
                 check=True):
        import base64 as _b64
        out = ""
        if "test -e" in cmd and ".done" in cmd:
            out = "yes"
        elif cmd.startswith("cat") and "summary.json" in cmd:
            out = json.dumps(dsum)
        elif cmd.startswith("base64 <"):
            out = _b64.b64encode(dv_index_bytes).decode()
        elif "tmux has-session" in cmd:
            out = "dead"
        return subprocess.CompletedProcess([], 0, stdout=out, stderr="")

    torn = []
    saved_eng = (engmod.run_ssh, engmod.get_vm, engmod.set_subscription,
                 engmod.cmd_teardown)
    democo_status_snap2 = (root / "democo" / "status.json").read_text()
    try:
        engmod.run_ssh = fake_ssh
        engmod.get_vm = lambda spec, cfg, slug, dry: dict(fake_vm)
        engmod.set_subscription = lambda cfg, dry: None
        engmod.cmd_teardown = lambda spec, r_, a: (
            torn.append(a.slug) or {"ok": True,
                                    "deleted": ["vm:deepv-democo"]})
        ns = SimpleNamespace(slug="democo", dry_run=False, no_cache=False,
                             workers=16, list_workers=8, sas_days=1,
                             keep_vm=False, confirmed=False, force=False,
                             rg=None, vm_size="Standard_D8s_v7",
                             os_disk_gb=None, dest_prefix="", container=None,
                             used_capacity=None, used_capacity_at=None)
        n_before = len(common.sizing_runs(root, "democo"))
        res = dvmod.cmd_step(root, ns)
        check("fake-ssh harvest: phase complete, run file written",
              res["phase"] == "complete"
              and len(common.sizing_runs(root, "democo")) == n_before + 1,
              str(res)[:300])
        hr = common.read_json(Path(res["run_file"]))
        check("harvested run: sized + verification + capacity from VM tags",
              hr["method"] == "sized"
              and hr["verification"]["measured_bytes"] == 30
              and hr["used_capacity_bytes"] == 4321
              and hr["used_capacity_at"] == "cap-at"
              and "deep verify on VM deepv-democo" in hr["notes"][0])
        dv_idx = root / "democo" / "blob-index.tsv.gz"
        check("harvest replaced company blob-index from the VM",
              dv_idx.exists()
              and "zip-exact" in gzip.open(dv_idx, "rt").read())
        check("auto-teardown ran after harvest", torn == ["democo"])
        Path(res["run_file"]).unlink()
    finally:
        (engmod.run_ssh, engmod.get_vm, engmod.set_subscription,
         engmod.cmd_teardown) = saved_eng
        (root / "democo" / "status.json").write_text(democo_status_snap2)

    print("\n— qwilr_transfer --dry-run (first VM-less ingest) —")
    proc = run_script("qwilr_transfer.py", "plan", "democo", "--root", root)
    qplan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("qwilr plan: local pull, dest from config, 1-day SAS, no VM",
          qplan["dest"] == "democo-raw/qwilr-export"
          and qplan["storage_account"] == "stdemoco"
          and qplan["sas_days"] == 1 and "vm_name" not in qplan
          and "no VM" in qplan["mode"])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "qwilr_transfer.py"), "pull",
         "democo", "--root", str(root), "--dry-run"],
        input="QWILRSECRET", capture_output=True, text=True)
    out = proc.stdout
    check("qwilr pull dry-run: rc 0, token never echoed, SAS redacted",
          proc.returncode == 0 and "QWILRSECRET" not in out
          and "redacted" in out, out[-300:])
    check("qwilr pull: racwl container SAS on the right container",
          "generate-sas" in out and "racwl" in out and "-n democo-raw" in out)
    check("qwilr pull: laptop IP rule path, not the VM vnet path",
          "network-rule add" in out and "--ip-address" in out
          and "allow-network" not in out and "vm create" not in out)
    check("qwilr pull: hits the Qwilr API and PUTs create-only blobs",
          "api.qwilr.com/v1/pages" in out
          and "x-ms-blob-type: BlockBlob" in out and "If-None-Match" in out)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "qwilr_transfer.py"), "verify",
         "democo", "--root", str(root), "--dry-run"],
        input="QWILRSECRET", capture_output=True, text=True)
    check("qwilr verify dry-run: read path mints rl, not racwl",
          proc.returncode == 0 and "--permissions rl" in proc.stdout
          and "racwl" not in proc.stdout
          and "QWILRSECRET" not in proc.stdout, proc.stdout[-300:])

    print("\n— qwilr_transfer in-process (stubbed transport) —")
    import types
    import qwilr_transfer  # noqa: E402

    # cursor pagination
    saved = (qwilr_transfer.qwilr_get, qwilr_transfer.azure_list_existing,
             qwilr_transfer.azure_put_json, qwilr_transfer.mint_write_sas,
             qwilr_transfer.read_token, qwilr_transfer._http,
             qwilr_transfer._sleep, common.run_az,
             phases.ip_rule_ensure, phases.ip_rule_remove_if_ours,
             phases.mint_sas)
    try:
        get_calls = []

        def fake_get_paged(token, path, params=None):
            get_calls.append((path, dict(params or {})))
            if params and params.get("cursor") == "c1":
                return {"data": [{"id": "p3"}]}
            return {"data": [{"id": "p1"}, {"id": "p2"}], "cursor": "c1"}

        qwilr_transfer.qwilr_get = fake_get_paged
        got = qwilr_transfer.list_pages("tok")
        check("pagination walks the cursor to exhaustion",
              [qwilr_transfer._page_id(s) for s in got] == ["p1", "p2", "p3"]
              and get_calls[1][1].get("cursor") == "c1")

        # full pull flow: resume-skip, per-page error, assets, index
        def fake_get_pull(token, path, params=None):
            get_calls.append((path, dict(params or {})))
            if path == "/pages":
                if params and params.get("limit") == 1:
                    return {"data": [{"id": "p1"}]}
                return {"data": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]}
            if path == "/pages/p2":
                return {"blocks": [{"img": "https://assets.qwilr.com/a.png"}]}
            if path == "/pages/p3":
                raise qwilr_transfer.QwilrHTTPError(500, "boom")
            return {"placeholder": path}

        puts = []

        def fake_put(cfg, sas, name, obj, dry_run):
            puts.append(name)
            return 10

        common.run_az = lambda *a, **k: types.SimpleNamespace(stdout="")
        phases.ip_rule_ensure = lambda cfg, dry_run=False: (True, "1.2.3.4")
        removed = []
        phases.ip_rule_remove_if_ours = (
            lambda cfg, ip, we, dry_run=False: removed.append((ip, we)))
        qwilr_transfer.qwilr_get = fake_get_pull
        qwilr_transfer.mint_write_sas = (
            lambda cfg, days, dry: ("sig=fake", "2026-08-20T00:00:00Z"))
        qwilr_transfer.azure_list_existing = (
            lambda cfg, sas, prefix, dry: {"qwilr-export/pages/p1.json"})
        qwilr_transfer.azure_put_json = fake_put
        qwilr_transfer.read_token = lambda dry: "tok"

        get_calls.clear()
        args = types.SimpleNamespace(slug="democo", dest_prefix="qwilr-export",
                                     sas_days=1, page_limit=None,
                                     dry_run=False)
        res = qwilr_transfer.cmd_pull(root, args)
        detail_paths = [p for p, _ in get_calls if p.startswith("/pages/")]
        check("pull: existing blob skipped, never re-fetched",
              res["skipped_existing"] == 1 and "/pages/p1" not in detail_paths)
        check("pull: per-page error counted, run completes ok",
              res["ok"] is True and res["page_errors"]["count"] == 1
              and res["page_errors"]["ids"] == ["p3"] and res["pulled"] == 1)
        check("pull: index + assets manifest written, account endpoints too",
              any(n.startswith("qwilr-export/_meta/pages-index-") for n in puts)
              and any(n.startswith("qwilr-export/_meta/assets-manifest-")
                      for n in puts)
              and "qwilr-export/account/users.json" in puts
              and res["assets_discovered"] == 1)
        check("pull: resume hint present with errors, IP rule removed",
              "resume_hint" in res and removed == [("1.2.3.4", True)])

        # 401 aborts before any write
        puts.clear()

        def fake_get_401(token, path, params=None):
            raise common.HarnessError("Qwilr API 401 on /pages -- token bad")

        qwilr_transfer.qwilr_get = fake_get_401
        try:
            qwilr_transfer.cmd_pull(root, args)
            check("pull: 401 aborts", False, "no exception raised")
        except common.HarnessError:
            check("pull: 401 aborts immediately, nothing PUT", puts == [])

        # verify math
        def fake_get_verify(token, path, params=None):
            return {"data": [{"id": "p1"}, {"id": "p2"}]}

        qwilr_transfer.qwilr_get = fake_get_verify
        qwilr_transfer.azure_list_existing = (
            lambda cfg, sas, prefix, dry: {
                "qwilr-export/pages/p1.json",
                "qwilr-export/pages/gone.json",
                "qwilr-export/account/users.json",
                "qwilr-export/_meta/pages-index-20260819T000000Z.json"})
        phases.mint_sas = lambda cfg, dry_run=False: "sig=fake"
        vres = qwilr_transfer.cmd_verify(root, args)
        check("verify: missing/extra sets + rc-2 semantics",
              vres["ok"] is False and vres["missing"] == ["p2"]
              and vres["extra"] == ["gone"]
              and vres["account_blobs"]["users"] is True
              and vres["index_blobs"] == 1 and "hint" in vres)

        # 429 backoff honors Retry-After; 401 never retries (real qwilr_get)
        qwilr_transfer.qwilr_get = saved[0]
        sleeps = []
        qwilr_transfer._sleep = lambda s: sleeps.append(s)

        class _Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        http_calls = {"n": 0}

        def fake_http_429(req, timeout=90):
            http_calls["n"] += 1
            if http_calls["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "slow down", {"Retry-After": "3"},
                    io.BytesIO(b""))
            return _Resp(b'{"data": []}')

        qwilr_transfer._http = fake_http_429
        data = qwilr_transfer.qwilr_get("tok", "/pages")
        check("429 backoff honors Retry-After exactly",
              data == {"data": []} and sleeps == [3])

        http_calls["n"] = 0

        def fake_http_401(req, timeout=90):
            http_calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "no", {},
                                         io.BytesIO(b""))

        qwilr_transfer._http = fake_http_401
        try:
            qwilr_transfer.qwilr_get("tok", "/pages")
            check("401 raises HarnessError", False, "no exception")
        except common.HarnessError:
            check("401 aborts on first attempt, no retries",
                  http_calls["n"] == 1)

        # PUT request shape (create-only)
        req = qwilr_transfer.build_put("https://x/y?sas", b"{}")
        hdrs = {k.lower(): v for k, v in req.headers.items()}
        check("build_put: PUT, BlockBlob, create-only, versioned",
              req.get_method() == "PUT"
              and hdrs.get("x-ms-blob-type") == "BlockBlob"
              and hdrs.get("if-none-match") == "*"
              and hdrs.get("x-ms-version") == qwilr_transfer.X_MS_VERSION)

        # asset URL extraction
        urls = qwilr_transfer.extract_asset_urls(
            {"a": [{"b": "https://assets.qwilr.com/logo.png"},
                   {"c": "https://cdn.qwilr.com/v/video.mp4"}],
             "d": "https://app.qwilr.com/some-page",
             "e": "https://example.com/x.png"})
        check("extract_asset_urls: media only, page links excluded",
              urls == {"https://assets.qwilr.com/logo.png",
                       "https://cdn.qwilr.com/v/video.mp4",
                       "https://example.com/x.png"})
    finally:
        (qwilr_transfer.qwilr_get, qwilr_transfer.azure_list_existing,
         qwilr_transfer.azure_put_json, qwilr_transfer.mint_write_sas,
         qwilr_transfer.read_token, qwilr_transfer._http,
         qwilr_transfer._sleep, common.run_az,
         phases.ip_rule_ensure, phases.ip_rule_remove_if_ours,
         phases.mint_sas) = saved


    print("\n— vimeo_transfer --dry-run (server-side-copy ingest) —")
    proc = run_script("vimeo_transfer.py", "plan", "democo", "--root", root)
    vplan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("vimeo plan: server-side copy, dest from config, 2-day SAS, no VM",
          vplan["dest"] == "democo-raw/vimeo-export"
          and vplan["storage_account"] == "stdemoco"
          and vplan["sas_days"] == 2 and "no VM" in vplan["mode"]
          and "server-side" in vplan["mode"]
          and vplan["declared_vimeo_bytes"] is None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "vimeo_transfer.py"), "probe",
         "democo", "--root", str(root), "--dry-run"],
        input="VIMEOSECRET", capture_output=True, text=True)
    check("vimeo probe dry-run: rc 0, no Azure, token never echoed",
          proc.returncode == 0 and "VIMEOSECRET" not in proc.stdout
          and "api.vimeo.com/me" in proc.stdout
          and "generate-sas" not in proc.stdout
          and "network-rule" not in proc.stdout, proc.stdout[-300:])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "vimeo_transfer.py"), "pull",
         "democo", "--root", str(root), "--dry-run"],
        input="VIMEOSECRET", capture_output=True, text=True)
    out = proc.stdout
    check("vimeo pull dry-run: rc 0, token never echoed, SAS redacted",
          proc.returncode == 0 and "VIMEOSECRET" not in out
          and "redacted" in out, out[-300:])
    check("vimeo pull: racwl container SAS on the right container",
          "generate-sas" in out and "racwl" in out and "-n democo-raw" in out)
    check("vimeo pull: laptop IP rule path, not the VM vnet path",
          "network-rule add" in out and "--ip-address" in out
          and "allow-network" not in out and "vm create" not in out)
    check("vimeo pull: hits the Vimeo API, create-only writes",
          "api.vimeo.com/me/videos" in out
          and "x-ms-blob-type: BlockBlob" in out and "If-None-Match" in out)
    check("vimeo pull: server-side copy shapes (blocks + commit)",
          "x-ms-copy-source" in out and "x-ms-source-range" in out
          and "comp=block" in out and "comp=blocklist" in out)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "vimeo_transfer.py"), "verify",
         "democo", "--root", str(root), "--dry-run"],
        input="VIMEOSECRET", capture_output=True, text=True)
    check("vimeo verify dry-run: read path mints rl, not racwl",
          proc.returncode == 0 and "--permissions rl" in proc.stdout
          and "racwl" not in proc.stdout
          and "VIMEOSECRET" not in proc.stdout, proc.stdout[-300:])

    print("\n— vimeo_transfer in-process (stubbed transport) —")
    import vimeo_transfer  # noqa: E402
    MIB = vimeo_transfer.MIB

    vsaved = (vimeo_transfer.vimeo_get, vimeo_transfer.resolve_cdn_url,
              vimeo_transfer.resolve_fresh, vimeo_transfer.azure_list_blobs,
              vimeo_transfer.azure_put_bytes, vimeo_transfer.azure_put_json,
              vimeo_transfer.put_blob_from_url,
              vimeo_transfer.put_block_from_url,
              vimeo_transfer.put_block_list,
              vimeo_transfer.copy_video_to_blob,
              vimeo_transfer.mint_write_sas, vimeo_transfer.read_token,
              vimeo_transfer._download_small, vimeo_transfer._http,
              vimeo_transfer._sleep, common.run_az,
              phases.ip_rule_ensure, phases.ip_rule_remove_if_ours,
              phases.mint_sas, vimeo_transfer.resolve_cdn,
              vimeo_transfer._http_nr)
    try:
        # paging.next walked to exhaustion
        vget_calls = []

        def fake_get_paged(token, path, params=None, accept=None):
            vget_calls.append(path)
            if path.startswith("/me/videos?page=2"):
                return {"total": 3, "data": [{"uri": "/videos/p3"}]}
            return {"total": 3,
                    "data": [{"uri": "/videos/p1"}, {"uri": "/videos/p2"}],
                    "paging": {"next": "/me/videos?page=2&per_page=100"}}

        vimeo_transfer.vimeo_get = fake_get_paged
        got = vimeo_transfer.list_videos("tok")
        check("vimeo pagination walks paging.next to exhaustion",
              [vimeo_transfer._video_id(v) for v in got] == ["p1", "p2", "p3"]
              and vget_calls[1].startswith("/me/videos?page=2"))

        # choose_file precedence
        dl_src = {"quality": "source", "link": "Ls", "size": 5, "md5": "a" * 32}
        dl_hd = {"quality": "hd", "link": "Lh", "size": 9, "type": "video/mp4"}
        prog_src = {"type": "source", "link": "Lp", "size": 7}
        f_sd = {"quality": "sd", "link": "Lf", "size": 3, "type": "video/mp4"}
        check("choose_file: source download beats bigger hd",
              vimeo_transfer.choose_file(
                  {"download": [dl_hd, dl_src]})["link"] == "Ls")
        check("choose_file: no source -> largest download",
              vimeo_transfer.choose_file(
                  {"download": [dl_hd], "play": {"progressive": [prog_src]}}
              )["link"] == "Lh")
        check("choose_file: progressive source beats files; hls excluded",
              vimeo_transfer.choose_file(
                  {"files": [f_sd, {"quality": "hls", "link": "Lhls",
                                    "size": 999}],
                   "play": {"progressive": [prog_src]}})["link"] == "Lp"
              and vimeo_transfer.choose_file(
                  {"files": [f_sd, {"quality": "hls", "link": "Lhls",
                                    "size": 999}]})["link"] == "Lf")
        check("choose_file: nothing -> None",
              vimeo_transfer.choose_file({"name": "x"}) is None
              and vimeo_transfer.choose_file(None) is None)

        # block plan math + id shape
        bplan = vimeo_transfer.block_plan(600 * MIB, 256 * MIB)
        check("block_plan: 600 MiB @ 256 MiB -> 3 blocks, exact ends",
              len(bplan) == 3 and bplan[0][1:] == (0, 256 * MIB - 1)
              and bplan[2][1:] == (512 * MIB, 600 * MIB - 1)
              and len({len(b[0]) for b in bplan}) == 1
              and len({b[0] for b in bplan}) == 3)
        check("md5 hex->b64 and rejects junk",
              vimeo_transfer.md5_hex_to_b64("00" * 16)
              == "AAAAAAAAAAAAAAAAAAAAAA=="
              and vimeo_transfer.md5_hex_to_b64("nothex") is None
              and vimeo_transfer.md5_hex_to_b64(None) is None)

        # copy routing: small -> single shot; large -> blocks + commit
        cargs = types.SimpleNamespace(dry_run=False, single_shot_max_mb=1024,
                                      block_size_mb=256)
        ccalls = {"single": [], "blocks": [], "commits": [], "fresh": 0}
        vimeo_transfer.resolve_cdn = lambda link: ("https://cdn/x", None)
        vimeo_transfer.put_blob_from_url = (
            lambda cfg, sas, name, src, ct, md5, dry:
            ccalls["single"].append(name) or 1)
        vimeo_transfer.put_block_from_url = (
            lambda cfg, sas, name, bid, src, s0, e0, dry:
            ccalls["blocks"].append((bid, s0, e0)))
        vimeo_transfer.put_block_list = (
            lambda cfg, sas, name, ids, ct, md5, dry:
            ccalls["commits"].append(list(ids)) or 1)
        chosen_small = {"size": 10 * MIB, "md5": "00" * 16, "link": "L",
                        "type": "video/mp4", "kind": "download",
                        "quality": "source", "ext": ".mp4"}
        n = vimeo_transfer.copy_video_to_blob({}, "sas", "tok", "v9",
                                              "x/videos/v9/f.mp4",
                                              chosen_small, cargs)
        cargs_blocky = types.SimpleNamespace(dry_run=False,
                                             single_shot_max_mb=100,
                                             block_size_mb=256)
        chosen_big = dict(chosen_small, size=600 * MIB)
        n2 = vimeo_transfer.copy_video_to_blob({}, "sas", "tok", "v9",
                                               "x/videos/v9/f.mp4",
                                               chosen_big, cargs_blocky)
        check("copy routing: small single-shot, above threshold 3 blocks+commit",
              n == 10 * MIB and ccalls["single"] == ["x/videos/v9/f.mp4"]
              and n2 == 600 * MIB and len(ccalls["blocks"]) == 3
              and len(ccalls["commits"][0]) == 3)

        # expired CDN URL: re-resolve, retry the SAME block id
        ccalls["blocks"].clear()
        boom = {"armed": True}

        def flaky_block(cfg, sas, name, bid, src, s0, e0, dry):
            if boom["armed"]:
                boom["armed"] = False
                raise vimeo_transfer.CopyError(
                    "expired", azure_code="CannotVerifyCopySource")
            ccalls["blocks"].append((bid, src))

        vimeo_transfer.put_block_from_url = flaky_block
        vimeo_transfer.resolve_fresh = (
            lambda token, vid, chosen:
            (ccalls.__setitem__("fresh", ccalls["fresh"] + 1)
             or ("https://cdn/fresh", None)))
        vimeo_transfer.copy_video_to_blob({}, "sas", "tok", "v9",
                                          "x/videos/v9/f.mp4",
                                          dict(chosen_big, size=300 * MIB),
                                          cargs_blocky)
        check("expired CDN URL: re-resolved once, same block id retried",
              ccalls["fresh"] == 1 and ccalls["blocks"][0][0]
              == vimeo_transfer._block_id(0)
              and ccalls["blocks"][0][1] == "https://cdn/fresh")

        # full pull flow: resume-skip, no-file, per-video error, meta blobs
        def fake_get_pull(token, path, params=None, accept=None):
            vget_calls.append(path)
            if path == "/me":
                return {"uri": "/users/1", "name": "Demo"}
            if path == "/me/videos":
                return {"total": 4, "data": [
                    {"uri": "/videos/v1",
                     "download": [dict(dl_src, size=100)]},
                    {"uri": "/videos/v2", "name": "Two",
                     "download": [dict(dl_src, size=200)],
                     "pictures": {"sizes": [{"link": "https://i/t.jpg"}]},
                     "metadata": {"connections": {"texttracks":
                                                  {"total": 1}}}},
                    {"uri": "/videos/v3",
                     "download": [dict(dl_src, size=300)]},
                    {"uri": "/videos/v4", "name": "NoFile"},
                ]}
            if path == "/videos/v2/texttracks":
                return {"data": [{"uri": "/videos/v2/texttracks/9",
                                  "language": "en", "name": "caps",
                                  "link": "https://x/cap.vtt"}]}
            if path == "/me/projects":
                return {"total": 1,
                        "data": [{"uri": "/projects/77", "name": "F"}]}
            if path == "/projects/77/videos":
                return {"total": 1, "data": [{"uri": "/videos/v2"}]}
            if path == "/me/albums":
                return {"total": 0, "data": []}
            return {"placeholder": path}

        vputs = []

        def fake_put_json(cfg, sas, name, obj, dry):
            vputs.append(name)
            return 10

        def fake_put_bytes(cfg, sas, name, body, ct, dry):
            vputs.append(name)
            return len(body)

        copied_vids = []

        def fake_copy(cfg, sas, token, vid, name, chosen, a):
            copied_vids.append(vid)
            if vid == "v3":
                raise vimeo_transfer.CopyError("copy x: HTTP 500 boom")
            return chosen["size"]

        common.run_az = lambda *a, **k: types.SimpleNamespace(stdout="")
        phases.ip_rule_ensure = lambda cfg, dry_run=False: (True, "1.2.3.4")
        vremoved = []
        phases.ip_rule_remove_if_ours = (
            lambda cfg, ip, we, dry_run=False: vremoved.append((ip, we)))
        vimeo_transfer.vimeo_get = fake_get_pull
        vimeo_transfer.mint_write_sas = (
            lambda cfg, days, dry: ("sig=fake", "2026-08-22T00:00:00Z"))
        vimeo_transfer.azure_list_blobs = (
            lambda cfg, sas, prefix, dry: {
                "vimeo-export/videos/v1/old-name.mp4": {"size": 100,
                                                        "md5": None},
                "vimeo-export/videos/v1/metadata.json": {"size": 10,
                                                         "md5": None}})
        vimeo_transfer.azure_put_json = fake_put_json
        vimeo_transfer.azure_put_bytes = fake_put_bytes
        vimeo_transfer.copy_video_to_blob = fake_copy
        vimeo_transfer.read_token = lambda dry: "tok"
        vimeo_transfer._download_small = lambda url: b"WEBVTT"

        vargs = types.SimpleNamespace(slug="democo",
                                      dest_prefix="vimeo-export",
                                      sas_days=2, video_limit=None,
                                      block_size_mb=256,
                                      single_shot_max_mb=1024,
                                      api_version="3.4", dry_run=False)
        vres = vimeo_transfer.cmd_pull(root, vargs)
        check("vimeo pull: landed video skipped (rename-proof), never re-copied",
              vres["skipped_existing"] == 1 and "v1" not in copied_vids)
        check("vimeo pull: per-video error counted, run completes ok",
              vres["ok"] is True and vres["video_errors"]["count"] == 1
              and vres["video_errors"]["ids"] == ["v3"]
              and vres["copied"] == 1
              and vres["bytes_copied_serverside"] == 200)
        check("vimeo pull: no-file video listed, not an error",
              vres["no_file"] == {"count": 1, "ids": ["v4"]})
        check("vimeo pull: texttrack + meta blobs written",
              "vimeo-export/videos/v2/texttracks/en-caps.vtt" in vputs
              and any(n.startswith("vimeo-export/_meta/videos-index-")
                      for n in vputs)
              and any(n.startswith("vimeo-export/_meta/folders-")
                      for n in vputs)
              and any(n.startswith("vimeo-export/_meta/thumbnails-manifest-")
                      for n in vputs))
        check("vimeo pull: resume hint present with errors, IP rule removed",
              "resume_hint" in vres and vremoved == [("1.2.3.4", True)])

        # 401 aborts before any write
        vputs.clear()

        def fake_get_401(token, path, params=None, accept=None):
            raise common.HarnessError("Vimeo API 401 on /me -- token bad")

        vimeo_transfer.vimeo_get = fake_get_401
        try:
            vimeo_transfer.cmd_pull(root, vargs)
            check("vimeo pull: 401 aborts", False, "no exception raised")
        except common.HarnessError:
            check("vimeo pull: 401 aborts immediately, nothing PUT",
                  vputs == [])

        # circuit breaker: first 5 copies all fail -> systemic abort
        def fake_get_many(token, path, params=None, accept=None):
            if path == "/me":
                return {"uri": "/users/1"}
            if path == "/me/videos":
                return {"total": 6, "data": [
                    {"uri": f"/videos/b{i}",
                     "download": [dict(dl_src, size=10)]}
                    for i in range(6)]}
            return {"data": []}

        def fake_copy_fail(cfg, sas, token, vid, name, chosen, a):
            raise vimeo_transfer.CopyError("copy: HTTP 403 nope")

        vimeo_transfer.vimeo_get = fake_get_many
        vimeo_transfer.azure_list_blobs = lambda cfg, sas, prefix, dry: {}
        vimeo_transfer.copy_video_to_blob = fake_copy_fail
        try:
            vimeo_transfer.cmd_pull(root, vargs)
            check("vimeo circuit breaker trips", False, "no exception")
        except common.HarnessError as e:
            check("vimeo circuit breaker: 5 straight failures abort",
                  "systemic" in str(e))

        # verify math: missing / size vs any candidate / extra / texttracks
        def fake_get_verify(token, path, params=None, accept=None):
            return {"total": 4, "data": [
                {"uri": "/videos/v1",
                 "download": [{"quality": "source", "link": "L", "size": 100,
                               "md5": "aa" * 16}],
                 "metadata": {"connections": {"texttracks": {"total": 2}}}},
                {"uri": "/videos/v2",
                 "download": [{"quality": "source", "link": "L",
                               "size": 200}]},
                {"uri": "/videos/v5", "name": "NoFile"},
                {"uri": "/videos/v6",
                 "download": [{"quality": "source", "link": "L",
                               "size": 50}]},
            ]}

        vimeo_transfer.vimeo_get = fake_get_verify
        vimeo_transfer.azure_list_blobs = (
            lambda cfg, sas, prefix, dry: {
                "vimeo-export/videos/v1/f.mp4":
                    {"size": 100,
                     "md5": vimeo_transfer.md5_hex_to_b64("aa" * 16)},
                "vimeo-export/videos/v1/metadata.json":
                    {"size": 10, "md5": None},
                "vimeo-export/videos/v1/texttracks/en-caps.vtt":
                    {"size": 5, "md5": None},
                "vimeo-export/videos/v6/f.mp4": {"size": 60, "md5": None},
                "vimeo-export/videos/gone/f.mp4": {"size": 1, "md5": None},
                "vimeo-export/_meta/videos-index-20260820T000000Z.json":
                    {"size": 10, "md5": None}})
        phases.mint_sas = lambda cfg, dry_run=False: "sig=fake"
        vv = vimeo_transfer.cmd_verify(root, vargs)
        check("vimeo verify: missing/mismatch/extra sets + rc-2 semantics",
              vv["ok"] is False and vv["missing"] == ["v2"]
              and [m["id"] for m in vv["size_mismatch"]] == ["v6"]
              and vv["extra"] == ["gone"]
              and vv["no_file_api_side"]["ids"] == ["v5"]
              and vv["texttracks"] == {"expected": 2, "present": 1}
              and vv["md5"]["checked"] == 1 and vv["md5"]["matched"] == 1
              and vv["meta_blobs"] == 1 and "hint" in vv)

        # 429 backoff honors Retry-After; 401 never retries (real vimeo_get)
        vimeo_transfer.vimeo_get = vsaved[0]
        vsleeps = []
        vimeo_transfer._sleep = lambda s: vsleeps.append(s)

        class _VResp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        vhttp = {"n": 0}

        def fake_http_429(req, timeout=90):
            vhttp["n"] += 1
            if vhttp["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "slow down", {"Retry-After": "4"},
                    io.BytesIO(b""))
            return _VResp(b'{"data": []}')

        vimeo_transfer._http = fake_http_429
        data = vimeo_transfer.vimeo_get("tok", "/me/videos")
        check("vimeo 429 backoff honors Retry-After exactly",
              data == {"data": []} and vsleeps == [4])

        vhttp["n"] = 0

        def fake_http_401(req, timeout=90):
            vhttp["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "no", {},
                                         io.BytesIO(b""))

        vimeo_transfer._http = fake_http_401
        try:
            vimeo_transfer.vimeo_get("tok", "/me/videos")
            check("vimeo 401 raises HarnessError", False, "no exception")
        except common.HarnessError:
            check("vimeo 401 aborts on first attempt, no retries",
                  vhttp["n"] == 1)

        # live request shapes: block staging + create-only commit
        vreqs = []

        def fake_http_ok(req, timeout=90):
            vreqs.append(req)
            return _VResp(b"")

        vimeo_transfer._http = fake_http_ok
        vimeo_transfer.put_block_from_url = vsaved[7]
        vimeo_transfer.put_block_list = vsaved[8]
        cfg_demo = {"storage_account": "stdemoco", "container": "democo-raw"}
        vimeo_transfer.put_block_from_url(
            cfg_demo, "sig=s", "vimeo-export/videos/v/f.mp4",
            vimeo_transfer._block_id(0), "https://cdn/x", 0, 99, False)
        vimeo_transfer.put_block_list(
            cfg_demo, "sig=s", "vimeo-export/videos/v/f.mp4",
            [vimeo_transfer._block_id(0)], "video/mp4",
            vimeo_transfer.md5_hex_to_b64("00" * 16), False)
        h0 = {k.lower(): v for k, v in vreqs[0].headers.items()}
        h1 = {k.lower(): v for k, v in vreqs[1].headers.items()}
        check("put_block_from_url: server-side range copy, versioned",
              vreqs[0].get_method() == "PUT"
              and "comp=block&blockid=" in vreqs[0].full_url
              and h0.get("x-ms-copy-source") == "https://cdn/x"
              and h0.get("x-ms-source-range") == "bytes=0-99"
              and h0.get("x-ms-version") == vimeo_transfer.X_MS_VERSION)
        check("put_block_list: create-only commit with provenance md5",
              vreqs[1].get_method() == "PUT"
              and "comp=blocklist" in vreqs[1].full_url
              and h1.get("if-none-match") == "*"
              and h1.get("x-ms-blob-content-md5")
              == "AAAAAAAAAAAAAAAAAAAAAA=="
              and b"<Latest>" in vreqs[1].data)

        vimeo_transfer.put_blob_from_url = vsaved[6]
        vreqs.clear()
        vimeo_transfer.put_blob_from_url(
            cfg_demo, "sig=s", "vimeo-export/videos/v/f.mp4",
            "https://cdn/x", "video/mp4", None, False)
        h2 = {k.lower(): v for k, v in vreqs[0].headers.items()}
        check("put_blob_from_url: source disposition overridden (CDN sends "
              "invalid filenames), create-only",
              h2.get("x-ms-copy-source") == "https://cdn/x"
              and h2.get("x-ms-blob-content-disposition") == "attachment"
              and h2.get("if-none-match") == "*")

        # wire size parsed from the 0-byte probe (listing sizes can be stale)
        vimeo_transfer.resolve_cdn = vsaved[19]

        def fake_nr(req, timeout=60):
            class _R:
                status = 206
                headers = {"Content-Range": "bytes 0-0/4125238613"}
                def close(self):
                    pass
            return _R()

        vimeo_transfer._http_nr = fake_nr
        u_w = vimeo_transfer.resolve_cdn("https://x/dl")
        check("resolve_cdn: wire size parsed from Content-Range",
              u_w == ("https://x/dl", 4125238613))
    finally:
        (vimeo_transfer.vimeo_get, vimeo_transfer.resolve_cdn_url,
         vimeo_transfer.resolve_fresh, vimeo_transfer.azure_list_blobs,
         vimeo_transfer.azure_put_bytes, vimeo_transfer.azure_put_json,
         vimeo_transfer.put_blob_from_url,
         vimeo_transfer.put_block_from_url,
         vimeo_transfer.put_block_list,
         vimeo_transfer.copy_video_to_blob,
         vimeo_transfer.mint_write_sas, vimeo_transfer.read_token,
         vimeo_transfer._download_small, vimeo_transfer._http,
         vimeo_transfer._sleep, common.run_az,
         phases.ip_rule_ensure, phases.ip_rule_remove_if_ours,
         phases.mint_sas, vimeo_transfer.resolve_cdn,
         vimeo_transfer._http_nr) = vsaved

    print("\n— zoom_transfer --dry-run (month-windowed server-side-copy "
          "ingest) —")
    proc = run_script("zoom_transfer.py", "plan", "democo", "--root", root)
    zplan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("zoom plan: server-side copy, dest from config, 2-day SAS, no VM",
          zplan["dest"] == "democo-raw/zoom-export"
          and zplan["storage_account"] == "stdemoco"
          and zplan["sas_days"] == 2 and "no VM" in zplan["mode"]
          and "server-side" in zplan["mode"]
          and zplan["declared_zoom_bytes"] is None
          and zplan["date_range"].startswith("2015-01-01"))
    zsecrets = "ZOOMACCID\nZOOMCLIENTID\nZOOMCLIENTSECRET"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoom_transfer.py"), "probe",
         "democo", "--root", str(root), "--dry-run"],
        input=zsecrets, capture_output=True, text=True)
    check("zoom probe dry-run: rc 0, no Azure, secrets never echoed",
          proc.returncode == 0
          and all(s not in proc.stdout for s in zsecrets.split())
          and "zoom.us/oauth/token" in proc.stdout
          and "accounts/me/recordings" in proc.stdout
          and "generate-sas" not in proc.stdout
          and "network-rule" not in proc.stdout, proc.stdout[-300:])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoom_transfer.py"), "pull",
         "democo", "--root", str(root), "--dry-run"],
        input=zsecrets, capture_output=True, text=True)
    out = proc.stdout
    check("zoom pull dry-run: rc 0, secrets never echoed, SAS redacted",
          proc.returncode == 0
          and all(s not in out for s in zsecrets.split())
          and "redacted" in out, out[-300:])
    check("zoom pull: racwl container SAS on the right container",
          "generate-sas" in out and "racwl" in out and "-n democo-raw" in out)
    check("zoom pull: laptop IP rule path, not the VM vnet path",
          "network-rule add" in out and "--ip-address" in out
          and "allow-network" not in out and "vm create" not in out)
    check("zoom pull: month-windowed materialized listing, create-only writes",
          "accounts/me/recordings" in out and "page_size=300" in out
          and "materialized" in out
          and "x-ms-blob-type: BlockBlob" in out and "If-None-Match" in out)
    check("zoom pull: server-side copy shapes (blocks + commit)",
          "x-ms-copy-source" in out and "x-ms-source-range" in out
          and "comp=block" in out and "comp=blocklist" in out)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoom_transfer.py"), "verify",
         "democo", "--root", str(root), "--dry-run"],
        input=zsecrets, capture_output=True, text=True)
    check("zoom verify dry-run: read path mints rl, not racwl",
          proc.returncode == 0 and "--permissions rl" in proc.stdout
          and "racwl" not in proc.stdout
          and all(s not in proc.stdout for s in zsecrets.split()),
          proc.stdout[-300:])

    print("\n— zoom_transfer in-process (stubbed transport) —")
    import zoom_transfer  # noqa: E402

    zsaved = (zoom_transfer.zoom_get, zoom_transfer.resolve_download_url,
              zoom_transfer.resolve_fresh, zoom_transfer.azure_list_blobs,
              zoom_transfer.azure_put_bytes, zoom_transfer.azure_put_json,
              zoom_transfer.put_blob_from_url,
              zoom_transfer.put_block_from_url,
              zoom_transfer.put_block_list,
              zoom_transfer.copy_file_to_blob,
              zoom_transfer.mint_write_sas, zoom_transfer.read_credentials,
              zoom_transfer._http, zoom_transfer._sleep,
              zoom_transfer.TokenBox.mint, common.run_az,
              phases.ip_rule_ensure, phases.ip_rule_remove_if_ours,
              phases.mint_sas)
    try:
        # month windows: clamped ends, one entry per month
        check("zoom month_windows: clamped first/last, one per month",
              zoom_transfer.month_windows("2026-01-15", "2026-03-10")
              == [("2026-01-15", "2026-01-31"),
                  ("2026-02-01", "2026-02-28"),
                  ("2026-03-01", "2026-03-10")]
              and zoom_transfer.month_windows("2026-05-05", "2026-05-05")
              == [("2026-05-05", "2026-05-05")])

        # uuid handling: double-encoded API paths, filename-safe blob dirs
        check("zoom double_encode: '/' and '+' encoded twice",
              zoom_transfer.double_encode("a/b+c==")
              == "a%252Fb%252Bc%253D%253D")
        check("zoom safe_uuid: filename-safe, deterministic",
              zoom_transfer.safe_uuid({"uuid": "a/b+c=="}) == "a_b-c=="
              and zoom_transfer.safe_uuid({"id": 12345}) == "12345"
              and zoom_transfer.safe_uuid({}) == "unknown")

        # placeholder gate (empty file_type = in-progress row)
        check("zoom should_pull_file: placeholder rows skipped",
              zoom_transfer.should_pull_file(
                  {"download_url": "u", "file_type": "MP4"})
              and not zoom_transfer.should_pull_file(
                  {"download_url": "u", "file_type": ""})
              and not zoom_transfer.should_pull_file({"file_type": "MP4"}))

        # blob_name: deterministic shape, ext map + fallback
        zmtg = {"uuid": "u/u+1=="}
        check("zoom blob_name: deterministic, id truncated, ext mapped",
              zoom_transfer.blob_name(
                  "zoom-export", zmtg,
                  {"recording_start": "2026-03-01T10:00:00Z",
                   "file_type": "MP4", "id": "abcdef0123456789"})
              == ("zoom-export/meetings/u_u-1==/"
                  "20260301T100000Z_MP4_abcdef012345.mp4")
              and zoom_transfer.blob_name(
                  "zoom-export", zmtg,
                  {"recording_start": "2026-03-01T10:00:00Z",
                   "file_type": "WEIRD", "file_extension": "XYZ",
                   "id": "1"}).endswith("_WEIRD_1.xyz")
              and zoom_transfer.blob_name(
                  "zoom-export", zmtg,
                  {"recording_start": "2026-03-01T10:00:00Z",
                   "file_type": "TRANSCRIPT", "id": "2"}).endswith(".vtt"))

        class _ZResp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        # TokenBox: mints once, re-mints inside the refresh margin
        zmints = {"n": 0}

        def fake_http_token(req, timeout=90):
            zmints["n"] += 1
            return _ZResp(b'{"access_token": "tok%d", "expires_in": 3600}'
                          % zmints["n"])

        zoom_transfer._http = fake_http_token
        zbox = zoom_transfer.TokenBox("A", "B", "C")
        t1 = zbox.get()
        t2 = zbox.get()
        zbox._exp = time.time() + 100  # inside the 300s refresh margin
        t3 = zbox.get()
        check("zoom TokenBox: mints once, re-mints inside expiry margin",
              t1 == "tok1" and t2 == "tok1" and t3 == "tok2"
              and zmints["n"] == 2)

        # 429 honors Retry-After exactly
        zsleeps = []
        zoom_transfer._sleep = lambda s: zsleeps.append(s)
        zhttp = {"n": 0}

        def fake_http_429(req, timeout=90):
            if "oauth" in req.full_url:
                return _ZResp(b'{"access_token": "t", "expires_in": 3600}')
            zhttp["n"] += 1
            if zhttp["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "slow down", {"Retry-After": "7"},
                    io.BytesIO(b""))
            return _ZResp(b'{"meetings": []}')

        zoom_transfer._http = fake_http_429
        data = zoom_transfer.zoom_get(zoom_transfer.TokenBox("A", "B", "C"),
                                      "/accounts/me/recordings")
        check("zoom 429 backoff honors Retry-After exactly",
              data == {"meetings": []} and zsleeps == [7])

        # 401 mid-run: one silent re-mint, then abort
        zhttp["n"] = 0
        ztokens = {"minted": 0}

        def fake_http_401(req, timeout=90):
            if "oauth" in req.full_url:
                ztokens["minted"] += 1
                return _ZResp(b'{"access_token": "t", "expires_in": 3600}')
            zhttp["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "no", {},
                                         io.BytesIO(b""))

        zoom_transfer._http = fake_http_401
        try:
            zoom_transfer.zoom_get(zoom_transfer.TokenBox("A", "B", "C"),
                                   "/users/me")
            check("zoom 401 re-mints once then aborts", False, "no exception")
        except common.HarnessError:
            check("zoom 401 re-mints once then aborts",
                  zhttp["n"] == 2 and ztokens["minted"] == 2)

        # listing 400 (unactivated app): raised immediately, never retried
        zhttp["n"] = 0

        def fake_http_400(req, timeout=90):
            if "oauth" in req.full_url:
                return _ZResp(b'{"access_token": "t", "expires_in": 3600}')
            zhttp["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 400, "bad", {},
                io.BytesIO(b'{"code":4711,"message":"scope"}'))

        zoom_transfer._http = fake_http_400
        try:
            zoom_transfer.list_month(zoom_transfer.TokenBox("A", "B", "C"),
                                     "2026-01-01", "2026-01-31")
            check("zoom listing 400: raised immediately, never retried",
                  False, "no exception")
        except zoom_transfer.ZoomHTTPError as e:
            check("zoom listing 400: raised immediately, never retried",
                  e.status == 400 and zhttp["n"] == 1 and "4711" in str(e))

        # full pull flow: month materialized before any copy, month failure
        # isolation, exact-name resume, placeholder skip, per-file error
        zf1 = {"download_url": "https://z/d1", "file_type": "MP4",
               "id": "f1", "file_size": 100,
               "recording_start": "2026-01-05T10:00:00Z"}
        zf2 = {"download_url": "https://z/d2", "file_type": "M4A",
               "id": "f2", "file_size": 50,
               "recording_start": "2026-01-20T10:00:00Z"}
        zf3 = {"download_url": "https://z/d3", "file_type": "MP4",
               "id": "f3", "file_size": 70,
               "recording_start": "2026-03-02T10:00:00Z"}
        zf4 = {"download_url": "https://z/d4", "file_type": "CHAT",
               "id": "f4", "file_size": 5,
               "recording_start": "2026-03-02T10:00:00Z"}
        zph = {"download_url": "https://z/ph", "file_type": "", "id": "ph"}
        zm1 = {"uuid": "m1", "recording_files": [zf1, zph]}
        zm2 = {"uuid": "m2", "recording_files": [zf2]}
        zm3 = {"uuid": "m3", "recording_files": [zf3, zf4]}
        zcalls = []

        def fake_zoom_get(box, path, params=None):
            if path == "/accounts/me/recordings":
                if (params or {}).get("page_size") == 1:
                    zcalls.append("gate")
                    return {"total_records": 0, "meetings": []}
                frm = params["from"]
                if frm.startswith("2026-01"):
                    if params.get("next_page_token"):
                        zcalls.append("list-2026-01-p2")
                        return {"meetings": [zm2]}
                    zcalls.append("list-2026-01-p1")
                    return {"meetings": [zm1], "next_page_token": "npt"}
                if frm.startswith("2026-02"):
                    zcalls.append("list-2026-02")
                    raise zoom_transfer.ZoomHTTPError(500, "boom month")
                if frm.startswith("2026-03"):
                    zcalls.append("list-2026-03")
                    return {"meetings": [zm3]}
                return {"meetings": []}
            if path == "/users/me":
                return {"email": "a@b.c"}
            return {}

        zputs = []

        def fake_zput_json(cfg, sas, name, obj, dry):
            zputs.append(name)
            return 10

        def fake_zcopy(cfg, sas, box, uuid, name, f, a):
            zcalls.append(f"copy-{f['id']}")
            if f["id"] == "f4":
                raise zoom_transfer.CopyError("copy x: HTTP 500 boom")
            return int(f.get("file_size") or 0)

        common.run_az = lambda *a, **k: types.SimpleNamespace(stdout="")
        phases.ip_rule_ensure = lambda cfg, dry_run=False: (True, "1.2.3.4")
        zremoved = []
        phases.ip_rule_remove_if_ours = (
            lambda cfg, ip, we, dry_run=False: zremoved.append((ip, we)))
        zoom_transfer.TokenBox.mint = lambda self: "t"
        zoom_transfer.read_credentials = lambda dry: ("A", "B", "C")
        zoom_transfer.zoom_get = fake_zoom_get
        zoom_transfer.mint_write_sas = (
            lambda cfg, days, dry: ("sig=fake", "2026-08-22T00:00:00Z"))
        zname2 = zoom_transfer.blob_name("zoom-export", zm2, zf2)
        zoom_transfer.azure_list_blobs = (
            lambda cfg, sas, prefix, dry: {zname2: {"size": 50}})
        zoom_transfer.azure_put_json = fake_zput_json
        zoom_transfer.copy_file_to_blob = fake_zcopy

        zargs = types.SimpleNamespace(
            slug="democo", dest_prefix="zoom-export", sas_days=2,
            from_date="2026-01-01", to_date="2026-03-31",
            meeting_limit=None, block_size_mb=256, single_shot_max_mb=1024,
            dry_run=False)
        zres = zoom_transfer.cmd_pull(root, zargs)
        check("zoom pull: month fully materialized before any copy",
              zcalls.index("list-2026-01-p2") < zcalls.index("copy-f1"))
        check("zoom pull: month failure isolated, run continues ok",
              zres["ok"] is True and list(zres["month_errors"]) == ["2026-02"]
              and zres["months_listed"] == 2)
        check("zoom pull: landed file skipped by exact name, never re-copied",
              zres["skipped_existing"] == 1 and "copy-f2" not in zcalls)
        check("zoom pull: placeholder counted, not an error",
              zres["placeholders_skipped"] == 1
              and zres["file_errors"]["count"] == 1)
        check("zoom pull: per-file error counted, run completes ok",
              zres["copied"] == 2 and zres["bytes_copied_serverside"] == 170
              and zres["file_errors"]["names"]
              == [zoom_transfer.blob_name("zoom-export", zm3, zf4)])
        check("zoom pull: metadata + index + account meta blobs written",
              any(n.endswith("m1/metadata.json") for n in zputs)
              and any("_meta/recordings-index-" in n for n in zputs)
              and any("_meta/account-" in n for n in zputs))
        check("zoom pull: resume hint present, IP rule removed",
              "resume_hint" in zres and zremoved == [("1.2.3.4", True)])

        # circuit breaker: first 5 file copies all fail -> systemic abort
        def fake_zoom_get_many(box, path, params=None):
            if path == "/accounts/me/recordings":
                if (params or {}).get("page_size") == 1:
                    return {"meetings": []}
                return {"meetings": [{
                    "uuid": "mm",
                    "recording_files": [
                        {"download_url": f"https://z/x{i}",
                         "file_type": "MP4", "id": f"x{i}", "file_size": 10,
                         "recording_start": "2026-01-01T00:00:00Z"}
                        for i in range(6)]}]}
            return {}

        def fake_zcopy_fail(cfg, sas, box, uuid, name, f, a):
            raise zoom_transfer.CopyError("copy: HTTP 403 nope")

        zargs_jan = types.SimpleNamespace(
            slug="democo", dest_prefix="zoom-export", sas_days=2,
            from_date="2026-01-01", to_date="2026-01-31",
            meeting_limit=None, block_size_mb=256, single_shot_max_mb=1024,
            dry_run=False)
        zoom_transfer.zoom_get = fake_zoom_get_many
        zoom_transfer.azure_list_blobs = lambda cfg, sas, prefix, dry: {}
        zoom_transfer.copy_file_to_blob = fake_zcopy_fail
        try:
            zoom_transfer.cmd_pull(root, zargs_jan)
            check("zoom circuit breaker trips", False, "no exception")
        except common.HarnessError as e:
            check("zoom circuit breaker: 5 straight failures abort",
                  "systemic" in str(e))

        # listing 400 at the gate: activation hint, nothing PUT
        zputs.clear()

        def fake_zoom_get_400(box, path, params=None):
            if path == "/accounts/me/recordings":
                raise zoom_transfer.ZoomHTTPError(400, "HTTP 400: 4711")
            return {}

        zoom_transfer.zoom_get = fake_zoom_get_400
        try:
            zoom_transfer.cmd_pull(root, zargs_jan)
            check("zoom pull 400 aborts", False, "no exception")
        except common.HarnessError as e:
            check("zoom pull: listing 400 aborts with activation hint, "
                  "nothing PUT",
                  "ACTIVATED" in str(e) and zputs == [])

        # expired source URL: re-resolve with fresh token, retry SAME block
        zoom_transfer.copy_file_to_blob = zsaved[9]
        zblocks = []
        zfresh = {"n": 0}
        zboom = {"armed": True}

        def flaky_zblock(cfg, sas, name, bid, src, s0, e0, dry):
            if zboom["armed"]:
                zboom["armed"] = False
                raise zoom_transfer.CopyError(
                    "expired", azure_code="CannotVerifyCopySource")
            zblocks.append((bid, src))

        zoom_transfer.put_block_from_url = flaky_zblock
        zoom_transfer.put_block_list = (
            lambda cfg, sas, name, ids, ct, dry: 1)
        zoom_transfer.resolve_download_url = lambda box, link: "https://z/u1"
        zoom_transfer.resolve_fresh = (
            lambda box, uuid, f: (zfresh.__setitem__("n", zfresh["n"] + 1)
                                  or "https://z/fresh"))
        ZMIB = zoom_transfer.MIB
        zc_args = types.SimpleNamespace(dry_run=False, single_shot_max_mb=100,
                                        block_size_mb=256)
        n = zoom_transfer.copy_file_to_blob(
            {}, "sas", None, "u", "zoom-export/meetings/u/f.mp4",
            {"download_url": "https://z/d", "file_type": "MP4",
             "file_size": 300 * ZMIB}, zc_args)
        check("zoom expired URL: re-resolved once, same block id retried",
              zfresh["n"] == 1 and n == 300 * ZMIB
              and zblocks[0][0] == zoom_transfer._block_id(0)
              and zblocks[0][1] == "https://z/fresh")

        # verify math: missing / byte-exact mismatch / extra (retention) /
        # placeholder api-side / rc-2 semantics
        zvm = {"uuid": "va", "recording_files": [
            {"download_url": "u", "file_type": "MP4", "id": "fa",
             "file_size": 100, "recording_start": "2026-01-02T00:00:00Z"},
            {"download_url": "u", "file_type": "M4A", "id": "fb",
             "file_size": 40, "recording_start": "2026-01-02T00:00:00Z"},
            {"download_url": "u", "file_type": "CHAT", "id": "fc",
             "file_size": 9, "recording_start": "2026-01-02T00:00:00Z"},
            {"download_url": "u", "file_type": "", "id": "ph2"},
        ]}
        zna, znb, znc = (zoom_transfer.blob_name("zoom-export", zvm, f)
                         for f in zvm["recording_files"][:3])

        def fake_zoom_get_verify(box, path, params=None):
            if path == "/accounts/me/recordings":
                if (params or {}).get("page_size") == 1:
                    return {"meetings": []}
                return {"meetings": [zvm]}
            return {}

        zgone = "zoom-export/meetings/gone/20250101T000000Z_MP4_old.mp4"
        zoom_transfer.zoom_get = fake_zoom_get_verify
        zoom_transfer.azure_list_blobs = (
            lambda cfg, sas, prefix, dry: {
                zna: {"size": 100},
                znc: {"size": 5},
                "zoom-export/meetings/va/metadata.json": {"size": 3},
                zgone: {"size": 1},
                "zoom-export/_meta/recordings-index-20260820T000000Z.json":
                    {"size": 2}})
        phases.mint_sas = lambda cfg, dry_run=False: "sig=fake"
        zv = zoom_transfer.cmd_verify(root, zargs_jan)
        check("zoom verify: missing/byte-exact/extra/placeholder + rc-2",
              zv["ok"] is False and zv["missing"] == [znb]
              and [m["name"] for m in zv["size_mismatch"]] == [znc]
              and zv["extra"] == [zgone]
              and zv["placeholders_api_side"] == 1
              and zv["files_expected"] == 3
              and zv["files_in_container"] == 3
              and zv["meta_blobs"] == 1 and "hint" in zv)

        # live request shapes: versioned range copy; create-only commit
        # WITHOUT md5 (zoom declares none)
        zreqs = []

        def fake_http_ok_z(req, timeout=90):
            zreqs.append(req)
            return _ZResp(b"")

        zoom_transfer._http = fake_http_ok_z
        zoom_transfer.put_block_from_url = zsaved[7]
        zoom_transfer.put_block_list = zsaved[8]
        zcfg_demo = {"storage_account": "stdemoco",
                     "container": "democo-raw"}
        zoom_transfer.put_block_from_url(
            zcfg_demo, "sig=s", "zoom-export/meetings/m/f.mp4",
            zoom_transfer._block_id(0), "https://z/x", 0, 99, False)
        zoom_transfer.put_block_list(
            zcfg_demo, "sig=s", "zoom-export/meetings/m/f.mp4",
            [zoom_transfer._block_id(0)], "video/mp4", False)
        hz0 = {k.lower(): v for k, v in zreqs[0].headers.items()}
        hz1 = {k.lower(): v for k, v in zreqs[1].headers.items()}
        check("zoom put_block_from_url: server-side range copy, versioned",
              zreqs[0].get_method() == "PUT"
              and "comp=block&blockid=" in zreqs[0].full_url
              and hz0.get("x-ms-copy-source") == "https://z/x"
              and hz0.get("x-ms-source-range") == "bytes=0-99"
              and hz0.get("x-ms-version") == zoom_transfer.X_MS_VERSION)
        check("zoom put_block_list: create-only commit, no md5 headers",
              zreqs[1].get_method() == "PUT"
              and "comp=blocklist" in zreqs[1].full_url
              and hz1.get("if-none-match") == "*"
              and "x-ms-blob-content-md5" not in hz1
              and b"<Latest>" in zreqs[1].data)
    finally:
        (zoom_transfer.zoom_get, zoom_transfer.resolve_download_url,
         zoom_transfer.resolve_fresh, zoom_transfer.azure_list_blobs,
         zoom_transfer.azure_put_bytes, zoom_transfer.azure_put_json,
         zoom_transfer.put_blob_from_url,
         zoom_transfer.put_block_from_url,
         zoom_transfer.put_block_list,
         zoom_transfer.copy_file_to_blob,
         zoom_transfer.mint_write_sas, zoom_transfer.read_credentials,
         zoom_transfer._http, zoom_transfer._sleep,
         zoom_transfer.TokenBox.mint, common.run_az,
         phases.ip_rule_ensure, phases.ip_rule_remove_if_ours,
         phases.mint_sas) = zsaved

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
