#!/usr/bin/env python3
"""Offline validation for cdp-harness — no Azure, no network, no git writes.

    python3 tests/test_harness.py

Copies tests/fixtures/companies/ to a temp root and exercises: report +
dashboard generation, verify-completion (fail, pass, mark-complete,
cannot-verify), offboard/restore archiving, the copied-forward path,
status/stall transitions, launch summary parsing, sizer summary compactness,
and fleet_size.py --dry-run.
"""
from __future__ import annotations

import ast
import base64
import gzip
import inspect
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
from datetime import timedelta
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


def run_script(script: str, *args, expect_rc=0,
               stdin_data: str = "") -> subprocess.CompletedProcess:
    # stdin is ALWAYS fed (default: empty, i.e. immediate EOF). Without
    # this the child inherits the runner's stdin, and any script that
    # reads credentials from it (saxon_sp_complete's probe/plan/
    # write-creds) blocks forever when the suite runs detached — a real
    # 3-hour hang, 2026-08-29.
    proc = subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                          input=stdin_data, capture_output=True, text=True)
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

    # The democo fixtures encode "grew yesterday" / "one day since last
    # change" but the stall logic in reconcile/phases compares against the
    # real clock — rebase the date-sensitive timestamps onto now (run files
    # renamed to match) so the fixtures can't rot as the calendar advances.
    now = common.utc_now()

    def _rebase_democo_run(old_name: str, days_ago: int) -> str:
        runs_dir = root / "democo" / "sizing-runs"
        run = common.read_json(runs_dir / old_name)
        ts = now - timedelta(days=days_ago)
        run["timestamp"] = common.iso(ts)
        run["used_capacity_at"] = common.iso(ts - timedelta(hours=1))
        (runs_dir / old_name).unlink()
        common.write_json(runs_dir / f"{common.ts_basic(ts)}.json", run)
        return run["timestamp"]

    _rebase_democo_run("20260811T090000Z.json", 2)
    democo_last_ts = _rebase_democo_run("20260812T090000Z.json", 1)
    democo_status = common.read_json(root / "democo" / "status.json")
    democo_status["last_run"]["timestamp"] = democo_last_ts
    democo_status["last_change_detected_at"] = democo_last_ts
    common.write_json(root / "democo" / "status.json", democo_status)

    print("\n— reconcile —")
    s = reconcile.company_summary(root, "democo")
    check("pct_complete ≈ 73.3", abs(s["pct_complete"] - 73.3) < 0.1,
          str(s["pct_complete"]))
    check("delta_24h = 100 GB", s["delta_24h"] == 100_000_000_000,
          str(s["delta_24h"]))
    check("rate = 100 GB/day", abs(s["rate_bytes_per_day"] - 1e11) < 1e9)
    # remaining 1.335 TB at 100 GB/day = 13.35 days after the latest run
    # (now-1d), i.e. ~12.35 days from now
    check("eta ~13 days out", s["eta"] is not None and
          abs((common.parse_iso(s["eta"]) - now).total_seconds() / 86400
              - 12.35) < 0.5,
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

    print("\n— reconcile + report: record_unit (non-'records' count units) —")
    import gen_report as _gr
    _ru_exp = {"services": {
        "gusto": {"records": 4, "record_unit": "W2 employees"},
        "outplay_ai": {"records": 100, "record_unit": "transactions"},
        "slack": {"records": 1500000},
        # record-declared, NO top-level prefix of its own, but detection
        # finds it inside another source's archives (declared as
        # google_calendar; detected under its canonical id gcal — the row
        # must resolve through the alias table like byte-declared rows do)
        "google_calendar": {"records": 125000}}}
    _ru_run = {"sources": {
        "gusto": {"blob_count": 1, "compressed_bytes": 500,
                  "uncompressed_bytes": 900},
        "outplay_ai": {"blob_count": 6, "compressed_bytes": 73445,
                       "uncompressed_bytes": 73445},
        "slack": {"blob_count": 2, "compressed_bytes": 10,
                  "uncompressed_bytes": 20}},
        "detected_services": {
            "gcal": {"bytes": 206_000_000,
                     "sources": {"workspace-export": 206_000_000}}}}
    _ru_rows, _ru_unexp = reconcile.service_rows(_ru_exp, _ru_run)
    _ru_by = {r["service"]: r for r in _ru_rows}
    check("record_unit carried onto the row",
          _ru_by["gusto"].get("declared_record_unit") == "W2 employees")
    check("record_unit absent → None (renderer falls back to 'records')",
          _ru_by["slack"].get("declared_record_unit") is None)
    check("record_unit rows still flagged record-count",
          all("record-count" in _ru_by[s]["flags"]
              for s in ("gusto", "outplay_ai", "slack")))
    check("record_unit rows have no byte pct",
          all(_ru_by[s]["pct"] is None
              for s in ("gusto", "outplay_ai", "slack")))
    # a record-declared service with no top-level prefix but embedded
    # detection must still surface the detection (found-embedded), exactly
    # like a byte-declared one — going record-declared must never HIDE data
    _ru_gc = _ru_by["google_calendar"]
    check("record-declared + embedded: found-embedded flagged",
          "found-embedded" in _ru_gc["flags"], str(_ru_gc["flags"]))
    check("record-declared + embedded: bytes + host carried",
          _ru_gc.get("embedded_bytes") == 206_000_000
          and _ru_gc.get("embedded_in") == ["workspace-export"])
    check("record-declared + embedded: pct stays None (no byte declaration)",
          _ru_gc["pct"] is None)
    check("record-declared with own top-level bytes: NOT found-embedded",
          "found-embedded" not in _ru_by["gusto"]["flags"])
    _ru_tbl = _gr.service_table(_ru_rows, _ru_unexp, _ru_run["sources"])
    check("table renders the declared unit verbatim",
          "4 W2 employees" in _ru_tbl and "100 transactions" in _ru_tbl
          and "1,500,000 records" in _ru_tbl, _ru_tbl)
    check("table never mislabels a non-record unit as 'records'",
          "4 records" not in _ru_tbl and "100 records" not in _ru_tbl)
    check("table shows embedded host for record-declared service",
          "inside workspace-export" in _ru_tbl, _ru_tbl)
    _ru_chart = _gr.bar_chart(_ru_rows, _ru_unexp, _ru_run["sources"])
    check("chart labels the declared unit verbatim",
          "4 W2 employees" in _ru_chart and "100 transactions" in _ru_chart,
          _ru_chart[:400])
    # the point of a record declaration: an actual bar, never a declared bar
    # (google_calendar's one bar is its embedded bytes, ‡-marked)
    check("chart draws exactly one (actual) bar per record-declared service",
          _ru_chart.count("<rect") == 4, str(_ru_chart.count("<rect")))
    check("chart ‡-marks the embedded record-declared service",
          "google_calendar ‡" in _ru_chart)

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

    print("\n— reconcile: equivalent_bytes (unit-adjusted declarations) —")
    _eq_run = {"sources": {"google_bigquery": {"uncompressed_bytes": 3_830,
                                               "compressed_bytes": 985,
                                               "blob_count": 7}}}
    _eq_exp = {"services": {"bigquery": {
        "bytes": 10_300, "prefix": "google_bigquery",
        "equivalent_bytes": 9_100, "equivalent_note": "decoded-sample basis"}}}
    _eq_rows, _ = reconcile.service_rows(_eq_exp, _eq_run)
    _r = _eq_rows[0]
    check("equivalent: pct from equivalent, actual stays measured",
          abs(_r["pct"] - 9_100 / 10_300 * 100) < 0.01
          and _r["actual_bytes"] == 3_830
          and _r["equivalent_bytes"] == 9_100, str(_r))
    check("equivalent: unit-adjusted flagged, no overshoot",
          _r["flags"] == ["unit-adjusted"], str(_r["flags"]))
    _eq_notes = reconcile.equivalent_unit_notes(_eq_rows)
    check("equivalent: note always emitted with basis",
          len(_eq_notes) == 1 and "bigquery" in _eq_notes[0]
          and "decoded-sample basis" in _eq_notes[0], str(_eq_notes))
    _plain_rows, _ = reconcile.service_rows(
        {"services": {"bigquery": {"bytes": 10_300,
                                   "prefix": "google_bigquery"}}}, _eq_run)
    check("equivalent absent: row and notes unchanged",
          abs(_plain_rows[0]["pct"] - 3_830 / 10_300 * 100) < 0.01
          and "equivalent_bytes" not in _plain_rows[0]
          and reconcile.equivalent_unit_notes(_plain_rows) == [])
    _no_data_rows, _ = reconcile.service_rows(
        _eq_exp, {"sources": {}})
    check("equivalent ignored when no data landed (declared-empty wins)",
          "declared-empty" in _no_data_rows[0]["flags"]
          and "equivalent_bytes" not in _no_data_rows[0],
          str(_no_data_rows[0]))

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
    check("embedded pct compares embedded vs declared (400/500 = 80%)",
          erows["hubspot"]["pct"] is not None
          and abs(erows["hubspot"]["pct"] - 80.0) < 0.01,
          str(erows["hubspot"]["pct"]))
    over_rows, _ = reconcile.service_rows(
        {"services": {"crm": {"bytes": 100}}},
        {"sources": {},
         "detected_services": {"crm": {"bytes": 150, "sources": {"x": 150}}}})
    check("embedded overshoot flagged",
          set(over_rows[0]["flags"]) == {"found-embedded", "overshoot"}
          and abs(over_rows[0]["pct"] - 150.0) < 0.01, str(over_rows[0]))
    # detected keys are catalog canonical ids when the path resolves through
    # the catalog (checkmate's Takeout/Calendar → "gcal"); a declared alias
    # spelling must still find them (the gcal/Google Calendar bug, 2026-08-29)
    alias_rows, _ = reconcile.service_rows(
        {"services": {"Google Calendar": {"bytes": 100}}},
        {"sources": {},
         "detected_services": {"gcal": {"bytes": 60, "sources": {"gdrive": 60}}}})
    check("declared alias matches canonical detected id (gcal)",
          alias_rows[0]["flags"] == ["found-embedded"]
          and alias_rows[0]["embedded_bytes"] == 60, str(alias_rows[0]))
    # Takeout children NOT in SERVICE_CATALOG detect under their own id
    # ("gphotos") — the declared spelling must bridge via TAKEOUT_CHILDREN
    photo_rows, _ = reconcile.service_rows(
        {"services": {"Google Photos": {"bytes": 100}}},
        {"sources": {},
         "detected_services": {"gphotos": {"bytes": 60,
                                           "sources": {"gdrive": 60}}}})
    check("declared alias matches Takeout child id (gphotos)",
          photo_rows[0]["flags"] == ["found-embedded"]
          and photo_rows[0]["embedded_bytes"] == 60, str(photo_rows[0]))
    enotes = " ".join(es["notes"])
    check("embedded note names hubspot + host",
          "hubspot" in enotes and "workspace-export" in enotes, enotes)
    check("undeclared stripe surfaced (≥1GB)", "stripe" in enotes, enotes)
    proc = run_script("gen_report.py", "embedco", "--root", root)
    ehtml = Path(proc.stdout.strip()).read_text()
    check("report renders found-embedded badge",
          "embedded in another source" in ehtml)
    check("report table shows the embedded bytes, not 0 B",
          "400.00 GB" in ehtml and "80.0%" in ehtml, ehtml[:0])
    check("chart bars the embedded bytes (‡ marker + legend note)",
          "hubspot ‡" in ehtml and "detected inside" in ehtml)

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

    print("\n— reconcile: zoho multi-product prefix (one ingest, three "
          "declared services) —")
    # The zoho ingest lands zoho-export/{crm,learn,workdrive} from ONE
    # skill, but the manifest declares three separate services. This is the
    # test that protects that prefix decision: source_split on the shared
    # base plus a per-product pin must attribute bytes SEPARATELY.
    zco = root / "zohoco"
    (zco / "sizing-runs").mkdir(parents=True)
    common.write_json(zco / "config.json", {
        "slug": "zohoco", "subscription": "m1 corpus", "subscription_id": "x",
        "resource_group": "rg-zohoco", "storage_account": "stzohoco",
        "container": "zohoco-raw",
        "vm": {"name": None, "resource_group": "rg-zohoco", "exists": False},
        "onboarded_at": "2026-08-01T00:00:00Z"})
    common.write_json(zco / "expected-data-sizes.json", {
        "slug": "zohoco", "manifest_total_bytes": 143_300_000_000,
        "source_split": ["zoho-export"],
        "services": {
            "zoho_crm": {"bytes": 133_000_000_000,
                         "prefix": "zoho-export/crm"},
            "zoho_learn": {"bytes": 8_000_000_000,
                           "prefix": "zoho-export/learn"},
            "zoho_workdrive": {"bytes": 2_300_000_000,
                               "prefix": "zoho-export/workdrive"}},
        "source": "test", "confirmed_by_user": True,
        "created_at": "2026-08-01T00:00:00Z"})
    common.write_json(zco / "status.json", {
        "slug": "zohoco", "stage": "pushing",
        "last_run": {"timestamp": "2026-08-13T09:00:00Z", "outcome": "sized",
                     "reason": None},
        "last_change_detected_at": "2026-08-13T09:00:00Z"})
    common.write_json(zco / "sizing-runs" / "20260813T100000Z.json", {
        "slug": "zohoco", "timestamp": "2026-08-13T10:00:00Z",
        "method": "sized", "copied_from": None,
        "used_capacity_bytes": 120_000_000_000,
        "used_capacity_at": "2026-08-13T09:00:00Z", "duration_seconds": 60,
        "totals": {"blob_count": 900, "compressed_bytes": 120_000_000_000,
                   "uncompressed_bytes": 141_000_000_000,
                   "zero_byte_blobs": 0},
        "sources": {"zoho-export": {"blob_count": 900,
                                    "compressed_bytes": 120_000_000_000,
                                    "uncompressed_bytes": 141_000_000_000}},
        "sources_l2": {
            "zoho-export/crm": [800, 115_000_000_000, 133_000_000_000],
            "zoho-export/learn": [100, 5_000_000_000, 8_000_000_000]},
        "methods": {"stored": 900}, "errors": {"total": 0, "by_type": {}},
        "notes": []})
    zs = reconcile.company_summary(root, "zohoco")
    zrows = {r["service"]: r for r in zs["service_rows"]}
    check("zoho pins: crm and learn attribute to their own sub-prefixes",
          zrows["zoho_crm"]["actual_bytes"] == 133_000_000_000
          and zrows["zoho_learn"]["actual_bytes"] == 8_000_000_000,
          str({k: v["actual_bytes"] for k, v in zrows.items()}))
    check("zoho pins: an un-run product reads as declared-empty, not as "
          "the whole prefix",
          (zrows["zoho_workdrive"]["actual_bytes"] or 0) == 0,
          str(zrows["zoho_workdrive"]))
    check("zoho pins: the shared base is no longer a single source",
          "zoho-export" not in zs["sources"], str(list(zs["sources"])))

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
    check("copied_from set", cf["copied_from"] == democo_last_ts,
          str(cf["copied_from"]))
    check("copied-forward tolerates old runs (empty detection)",
          cf.get("detected_services", {}) == {} and cf.get("cache") is None)
    st = phases.update_status(root, "democo", "skipped-unchanged", "test")
    check("no growth → stage still pushing (only 1 day)", st["stage"] == "pushing")
    check("last_run outcome recorded",
          st["last_run"]["outcome"] == "skipped-unchanged")
    # age the last change past the stall threshold → next no-growth run stalls
    st["last_change_detected_at"] = common.iso(now - timedelta(days=10))
    common.save_status(root, "democo", st)
    phases.write_copied_forward_run(root, "democo", 1_564_500_000_000,
                                    "2026-08-13T09:00:00Z")
    st = phases.update_status(root, "democo", "skipped-unchanged", "test")
    check("no growth ≥3 days → stalled", st["stage"] == "stalled")
    # growth resumes → back to pushing
    grown = json.loads(json.dumps(cf))
    # filename/timestamp must sort AFTER the real-clock copied-forward runs
    grown_name = f"{common.ts_basic(now + timedelta(days=1))}.json"
    grown["timestamp"] = common.iso(now + timedelta(days=1))
    grown["method"] = "sized"
    grown["totals"]["uncompressed_bytes"] += 50_000_000_000
    common.write_json(root / "democo" / "sizing-runs" / grown_name, grown)
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
    check("takeout child: Mail is gmail, not the wrapper's gdrive",
          sizer.match_path("Takeout/Mail/All mail Including Spam.mbox", m)
          == "gmail")
    check("takeout child: Drive stays gdrive",
          sizer.match_path("Takeout/Drive/folder/file.bin", m) == "gdrive")
    check("takeout child: uncataloged Google service gets its own name",
          sizer.match_path("Takeout/Chat/log.json", m) == "gchat")
    check("takeout child: declared spelling wins",
          sizer.build_matcher(("Gmail",))["gmail"] == "Gmail"
          and sizer.match_path("Takeout/Mail/x.mbox",
                               sizer.build_matcher(("Gmail",))) == "Gmail")
    check("takeout child: deeper embedded service still wins",
          sizer.match_path("Takeout/Drive/hubspot/x.csv", m) == "HubSpot")
    check("takeout filename token still matches the wrapper",
          sizer.match_path("workspace-export/u@x.com/takeout-2026-001.zip", m)
          == "gdrive")
    check("unknown takeout child falls back to the wrapper",
          sizer.match_path("Takeout/Whatever/x", m) == "gdrive")
    fp1 = sizer.matcher_fingerprint(m)
    sizer.TAKEOUT_CHILDREN["zzz-test"] = "zzz"
    try:
        fp2 = sizer.matcher_fingerprint(m)
    finally:
        del sizer.TAKEOUT_CHILDREN["zzz-test"]
    check("takeout child map feeds the matcher fingerprint", fp1 != fp2)

    # Path-layer attribution must exclude entry bytes attributed elsewhere,
    # so the detection lens stays disjoint (a takeout zip's Mail entries
    # belong to gmail, not ALSO to the zip's own gdrive path attribution).
    agg = sizer.Aggregator(str(tmp / "test-agg.tsv"), m, "fp")
    agg.add({"name": "workspace-export/u@x.com/takeout-1.zip",
             "clen": 100, "uncomp": 1000, "method": "zip:3entries",
             "kind": "zip", "etag": "0xA1", "cached": False, "err": None,
             "svc": {"gdrive": [700, 2], "gmail": [300, 1]}})
    agg.close()
    det = agg.detected
    check("disjoint lens: path bytes exclude other services' entries",
          det["gdrive"]["bytes"] == 700 and det["gdrive"]["path_bytes"] == 700
          and det["gmail"]["bytes"] == 300
          and det["gmail"]["zip_entry_bytes"] == 300,
          str({k: v["bytes"] for k, v in det.items()}))
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
        check("gdrive path-detected, minus the embedded hubspot entry "
              "(zip 6000 - 5000 + plain 200)",
              det["gdrive"]["bytes"] == 1200
              and det["gdrive"]["path_bytes"] == 1200, str(det.get("gdrive")))
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

    print("\n— sizer: parquet footers (thrift compact, cold/warm/deep) —")
    # Independent thrift-compact ENCODER (vs the sizer's decoder) building
    # real footer bytes: PAR1 + data + FileMetaData + <u32 len> + PAR1.

    def tc_varint(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | 0x80 if n else b)
            if not n:
                return bytes(out)

    def tc_zz(n):
        return tc_varint((n << 1) ^ (n >> 63))

    def tc_field(delta, ttype, payload=b""):
        return bytes([(delta << 4) | ttype]) + payload

    def pq_col(unc, comp):
        md = (tc_field(1, 5, tc_zz(1))       # 1 type (enum)
              + tc_field(3, 5, tc_zz(1))     # 4 codec (skips 2,3)
              + tc_field(1, 6, tc_zz(10))    # 5 num_values
              + tc_field(1, 6, tc_zz(unc))   # 6 total_uncompressed_size
              + tc_field(1, 6, tc_zz(comp))  # 7 total_compressed_size
              + b"\x00")
        return (tc_field(2, 6, tc_zz(4))     # 2 file_offset (skips 1)
                + tc_field(1, 12, md)        # 3 meta_data
                + b"\x00")

    def pq_row_group(cols, total_byte_size, nrows):
        body = b""
        if cols:
            body += tc_field(1, 9, bytes([(len(cols) << 4) | 12])
                             + b"".join(cols))          # 1 columns
            body += tc_field(1, 6, tc_zz(total_byte_size))  # 2 total_byte_size
        else:
            body += tc_field(2, 6, tc_zz(total_byte_size))  # 2 (skips 1)
        return body + tc_field(1, 6, tc_zz(nrows)) + b"\x00"  # 3 num_rows

    def pq_file(row_groups, nrows, pad=100):
        schema_el = tc_field(4, 8, tc_varint(3) + b"col") + b"\x00"
        footer = (tc_field(1, 5, tc_zz(1))                       # 1 version
                  + tc_field(1, 9, bytes([(1 << 4) | 12]) + schema_el)  # 2
                  + tc_field(1, 6, tc_zz(nrows))                 # 3 num_rows
                  + tc_field(1, 9, bytes([(len(row_groups) << 4) | 12])
                             + b"".join(row_groups))             # 4 row_groups
                  + b"\x00")
        return (b"PAR1" + b"\x00" * pad + footer
                + struct.pack("<I", len(footer)) + b"PAR1")

    # a.parquet: column-level sizes (7000+3000) + (5000) = 15000 must WIN over
    # deliberately-wrong RowGroup.total_byte_size values
    pqc = {
        "bq/a.parquet": pq_file(
            [pq_row_group([pq_col(7000, 700), pq_col(3000, 300)], 999_999, 5),
             pq_row_group([pq_col(5000, 500)], 888_888, 5)], 10),
        # b.parquet: no column metadata → RowGroup.total_byte_size fallback
        "bq/b.parquet": pq_file([pq_row_group([], 4321, 7)], 7),
        "bq/junk.parquet": b"this is not a parquet file at all........",
        "bq/enc.parquet": b"PAR1" + b"\x00" * 40 + b"\x00\x00\x00\x00PARE",
        "bq/tiny.parquet": b"PAR1",
    }
    pq_unc_expected = {
        "bq/a.parquet": (15000, "parquet-footer"),
        "bq/b.parquet": (4321, "parquet-footer"),
        "bq/junk.parquet": (len(pqc["bq/junk.parquet"]), "parquet-bad-magic"),
        "bq/enc.parquet": (len(pqc["bq/enc.parquet"]), "parquet-encrypted"),
        "bq/tiny.parquet": (4, "parquet-tiny"),
    }
    pqtags = {n: f"0xPQ{i:02d}" for i, n in enumerate(sorted(pqc))}

    def pq_listing_xml(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        prefix = q.get("prefix", [""])[0]
        delim = q.get("delimiter", [""])[0]
        parts, seen = ["<EnumerationResults><Blobs>"], set()
        for n in sorted(k for k in pqc if k.startswith(prefix)):
            rest = n[len(prefix):]
            if delim and delim in rest:
                p = prefix + rest.split(delim)[0] + delim
                if p not in seen:
                    seen.add(p)
                    parts.append(f"<BlobPrefix><Name>{p}</Name></BlobPrefix>")
                continue
            parts.append(f"<Blob><Name>{n}</Name><Properties>"
                         f"<Content-Length>{len(pqc[n])}</Content-Length>"
                         f"<Etag>{pqtags[n]}</Etag></Properties></Blob>")
        parts.append("</Blobs><NextMarker/></EnumerationResults>")
        return "".join(parts).encode()

    pq_counters = {"range_gets": 0}

    def pq_http(url, extra_headers=None):
        if "comp=list" in url:
            return pq_listing_xml(url)
        name = urllib.parse.unquote(url.split("/pq-raw/", 1)[1].split("?", 1)[0])
        data = pqc[name]
        rng = (extra_headers or {}).get("Range")
        if rng:
            pq_counters["range_gets"] += 1
            a, bb = rng[len("bytes="):].split("-")
            return data[int(a):int(bb) + 1]
        return data

    pq_env = {"SA": "fakesa", "CONTAINER": "pq-raw", "SAS": "sig=x",
              "TAG": "pqco-sizer", "OUT_DIR": str(swork),
              "SIZER_WORKERS": "4", "LIST_WORKERS": "2"}
    os.environ.update(pq_env)
    for k in ("CACHE_FILE", "SEED_TSV", "EXPECTED_SERVICES", "DEEP_VERIFY"):
        os.environ.pop(k, None)
    real_http = sizer.http_get
    try:
        sizer.http_get = pq_http
        sizer.main()
        sp = json.loads((swork / "pqco-sizer.summary.json").read_text())
        want_unc = sum(v[0] for v in pq_unc_expected.values())
        check("parquet cold totals (column sizes win over total_byte_size)",
              sp["blobs"] == 5 and sp["unc"] == want_unc
              and sp["errors"] == 0, json.dumps(sp)[:300])
        check("parquet methods kind counted", sp["methods"].get("parquet") == 5,
              str(sp["methods"]))
        rows = {}
        for line in (swork / "pqco-sizer.sizes.tsv").read_text().splitlines():
            if line.startswith("#"):
                continue
            f = line.split("\t")
            rows[f[0]] = (int(f[2]), f[4])
        check("parquet per-blob methods + values",
              rows == pq_unc_expected, str(rows))
        check("parquet cold cache stats (all 5 are misses)",
              sp["cache"] == {"hits": 0, "misses": 5}, str(sp["cache"]))

        # footer bigger than the tail read → exercises the second range GET
        saved_tail = sizer.PARQUET_TAIL
        try:
            sizer.PARQUET_TAIL = 16  # too small to hold any of our footers
            unc2, meth2 = sizer.parquet_uncompressed("bq/a.parquet",
                                                     len(pqc["bq/a.parquet"]))
            check("parquet second-GET path when footer outgrows the tail",
                  (unc2, meth2) == (15000, "parquet-footer"),
                  f"{unc2} {meth2}")
        finally:
            sizer.PARQUET_TAIL = saved_tail

        # warm shallow run: every parquet row replays from cache, zero HTTP
        pq_cache = tmp / "pqco-index.tsv.gz"
        shutil.copy(swork / "pqco-sizer.index.tsv.gz", pq_cache)
        for f in swork.glob("pqco-sizer.*"):
            f.unlink()
        os.environ["CACHE_FILE"] = str(pq_cache)
        pq_counters["range_gets"] = 0
        sizer.main()
        sp2 = json.loads((swork / "pqco-sizer.summary.json").read_text())
        check("parquet warm run: all hits, zero range reads",
              sp2["cache"] == {"hits": 5, "misses": 0}
              and pq_counters["range_gets"] == 0
              and sp2["unc"] == want_unc,
              f'{sp2["cache"]} ranges={pq_counters["range_gets"]}')

        # deep run: parquet methods are TERMINAL (no codec for the pages) —
        # cached rows must replay, not re-measure, and land in "trusted"
        for f in swork.glob("pqco-sizer.*"):
            f.unlink()
        os.environ["DEEP_VERIFY"] = "1"
        pq_counters["range_gets"] = 0
        sizer.main()
        sp3 = json.loads((swork / "pqco-sizer.summary.json").read_text())
        ver = sp3.get("verification") or {}
        check("parquet deep run: terminal rows replay (zero range reads)",
              sp3["cache"] == {"hits": 5, "misses": 0}
              and pq_counters["range_gets"] == 0, str(sp3["cache"]))
        check("parquet deep coverage: 4 trusted + tiny measured",
              ver.get("trusted_blobs") == 4
              and ver.get("measured_blobs") == 1
              and ver.get("unmeasurable_blobs") == 0,
              json.dumps(ver))
    finally:
        sizer.http_get = real_http
        for k in list(pq_env) + ["CACHE_FILE", "SEED_TSV", "DEEP_VERIFY"]:
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
        # The future-dated run file from the earlier stall/"growth resumed"
        # test (now+1d) sorts after any real-clock run and would otherwise
        # outrank the harvest above as "latest" — its job there is done, so
        # drop it here for this check to see the real latest run.
        (root / "democo" / "sizing-runs" / grown_name).unlink()
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

    print("\n— zoho_transfer --dry-run (engine lifecycle, VM-side REST "
          "puller) —")
    import zoho_transfer  # noqa: E402
    import zoho_vm_pull  # noqa: E402
    zcreds = "com\nZCLIENTIDSENTINEL\nZSECRETSENTINEL\nZREFRESHSENTINEL\n"
    check("zoho Spec: Self Client flow (no OAuth/rclone), 512 GB disk",
          zoho_transfer.SPEC.vm_prefix == "xfer-zoho-"
          and zoho_transfer.SPEC.authorize_target == ""
          and zoho_transfer.SPEC.remote_type == ""
          and zoho_transfer.SPEC.default_dest_prefix == "zoho-export"
          and zoho_transfer.SPEC.default_os_disk_gb == 512)
    proc = run_script("zoho_transfer.py", "plan", "democo", "--dc", "com",
                      "--product", "crm", "--root", root, "--dry-run")
    plan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("zoho plan: product dest, but the dest_prefix TAG stays the base",
          plan["vm_name"] == "xfer-zoho-democo"
          and plan["dest"] == "democo-raw/zoho-export/crm"
          and plan["dest_prefix_tag"] == "zoho-export"
          and plan["source"] == "zoho:com")
    proc = run_script("zoho_transfer.py", "create-vm", "democo", "--dc",
                      "com", "--product", "crm", "--root", root, "--dry-run")
    check("zoho create-vm: 512 GB staging disk + dc/product tags",
          "--os-disk-size-gb 512" in proc.stdout
          and "purpose=zoho-transfer" in proc.stdout
          and "zoho_dc=com" in proc.stdout
          and "zoho_product=crm" in proc.stdout
          and "dest_prefix=zoho-export" in proc.stdout
          and "dest_prefix=zoho-export/crm" not in proc.stdout
          and "-n xfer-zoho-democo" in proc.stdout)
    proc = run_script("zoho_transfer.py", "allow-network", "democo",
                      "--root", root, "--dry-run")
    check("zoho allow-network: vnet path (VM family), never IP rules",
          "network-rule add" in proc.stdout and "--subnet" in proc.stdout
          and "--ip-address" not in proc.stdout)
    proc = run_script("zoho_transfer.py", "write-dest", "democo",
                      "--product", "crm", "--root", root, "--dry-run")
    check("zoho write-dest: racwl SAS, product-suffixed dest, redacted",
          "--permissions racwl" in proc.stdout
          and "zoho-export/crm" in proc.stdout
          and "rclone.conf" in proc.stdout
          and "dest-crm.env" in proc.stdout
          and "redacted" in proc.stdout)
    check("zoho write-dest: per-product env, so a second product never "
          "re-points the first product's resume",
          "dest-learn.env" not in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoho_transfer.py"), "write-creds",
         "democo", "--root", str(root), "--dry-run"],
        input=zcreds, capture_output=True, text=True)
    check("zoho write-creds: none of the 4 sentinels ever echoed",
          proc.returncode == 0
          and "ZCLIENTIDSENTINEL" not in proc.stdout
          and "ZSECRETSENTINEL" not in proc.stdout
          and "ZREFRESHSENTINEL" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    check("zoho write-creds: dry-run echo stays brace-free before the JSON",
          "{" not in proc.stdout[:proc.stdout.index("{")]
          and json.loads(proc.stdout[proc.stdout.index("{"):])["dc"] == "com")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoho_transfer.py"), "write-creds",
         "democo", "--root", str(root), "--dry-run"],
        input="com\nid\nsecret\n", capture_output=True, text=True)
    check("zoho write-creds: refuses malformed stdin (must be 4 lines)",
          proc.returncode == 1 and "4 lines" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoho_transfer.py"), "write-creds",
         "democo", "--root", str(root), "--dry-run"],
        input="com\nid\nse'cret\nrefresh\n", capture_output=True, text=True)
    check("zoho write-creds: single-quote guard refuses before writing",
          proc.returncode == 1 and "single quote" in proc.stdout
          and "nothing was written" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoho_transfer.py"), "write-creds",
         "democo", "--root", str(root), "--dry-run"],
        input="gov\nid\nsecret\nrefresh\n", capture_output=True, text=True)
    check("zoho write-creds: unknown data center refused",
          proc.returncode == 1 and "data center" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoho_transfer.py"), "probe",
         "democo", "--dc", "com", "--product", "crm", "--root", str(root),
         "--dry-run"], input=zcreds, capture_output=True, text=True)
    check("zoho probe crm: laptop-side API JSON only — no Azure, no VM",
          proc.returncode == 0
          and "accounts.zoho.com/oauth/v2/token" in proc.stdout
          and "www.zohoapis.com/crm/v8/settings/modules" in proc.stdout
          and "token-redacted" in proc.stdout
          and "ZREFRESHSENTINEL" not in proc.stdout
          and "generate-sas" not in proc.stdout
          and "network-rule" not in proc.stdout
          and "az vm" not in proc.stdout, proc.stdout[-300:])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "zoho_transfer.py"), "probe",
         "democo", "--dc", "eu", "--product", "learn", "--portal", "zylker",
         "--root", str(root), "--dry-run"],
        input="eu\nid\nsecret\nrefresh\n", capture_output=True, text=True)
    check("zoho probe learn: DC-local host + undocumented KB paths tried",
          proc.returncode == 0
          and "learn.zoho.eu/learn/api/v1/portal/zylker/course" in proc.stdout
          and "pageIndex" in proc.stdout
          and "portal/zylker/manual" in proc.stdout, proc.stdout[-300:])
    proc = run_script("zoho_transfer.py", "plan", "democo", "--dc", "com",
                      "--product", "crm", "--dest-exact", "zoho_crm",
                      "--root", root, "--dry-run")
    plan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("zoho --dest-exact: literal prefix, product NOT appended",
          plan["dest"] == "democo-raw/zoho_crm")
    proc = run_script("zoho_transfer.py", "write-dest", "democo",
                      "--product", "crm", "--dest-exact", "zoho_crm",
                      "--root", root, "--dry-run")
    wd = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("zoho --dest-exact: write-dest points dest.env at it",
          wd["dest_prefix"] == "zoho_crm")
    proc = run_script("zoho_transfer.py", "verify", "democo", "--product",
                      "crm", "--dest-exact", "zoho_crm", "--root", root,
                      "--dry-run")
    check("zoho --dest-exact: verify reads the same literal prefix",
          "zoho_crm/manifest.json" in proc.stdout
          and "zoho-export" not in proc.stdout)
    proc = run_script("zoho_transfer.py", "transfer", "democo", "--product",
                      "crm", "--root", root, "--dry-run")
    check("zoho transfer: pushes puller into a per-PRODUCT tmux window",
          "zoho_vm_pull.py" in proc.stdout
          and "tmux new-session -d -s transfer -n crm" in proc.stdout
          and "zoho.env" in proc.stdout
          and "dest-crm.env" in proc.stdout)
    proc = run_script("zoho_transfer.py", "verify", "democo", "--product",
                      "crm", "--root", root, "--dry-run")
    check("zoho verify: laptop path — ip rule + rl SAS + product manifest",
          "--permissions rl" in proc.stdout
          and "network-rule add" in proc.stdout
          and "zoho-export/crm/manifest.json" in proc.stdout
          and "racwl" not in proc.stdout)
    proc = run_script("zoho_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", expect_rc=2)
    check("zoho teardown also gated", '"not-confirmed"' in proc.stdout)
    proc = run_script("zoho_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", "--confirmed")
    check("zoho confirmed teardown: engine set + Self Client revocation",
          "network-rule remove" in proc.stdout
          and "vnet delete" in proc.stdout
          and "refresh token" in proc.stdout
          and "Self Client" in proc.stdout)
    zsrc = (SCRIPTS / "zoho_vm_pull.py").read_text()
    check("zoho puller: cursors, markers, bulk pipeline, oauthtoken auth",
          "page_token" in zsrc and ".cdp-complete" in zsrc
          and ".cdp-cursor.json" in zsrc and "crm/bulk/v8/read" in zsrc
          and "Zoho-oauthtoken" in zsrc)
    check("zoho puller: manifest REPLACES an earlier pass's, corpus never "
          "does — a no-overwrite manifest would freeze verify on pass 1",
          "--overwrite=true" in zsrc and "upload_run_metadata" in zsrc
          and ".cdp-cursor.json;" in zsrc  # control files overwrite too
          and "--include-pattern" in zsrc
          and zsrc.count("--overwrite=false") >= 1
          and "manifest.json" in zsrc)
    check("zoho puller: --refresh replaces its OWN prior export (Zoho "
          "mutates viewCount, so a re-pull differs and no-overwrite would "
          "strand a stale copy as a phantom short_upload)",
          "overwrite: bool = False" in zsrc
          and "overwrite=refresh" in zsrc
          and '"--overwrite=true" if overwrite else "--overwrite=false"'
          in zsrc)
    check("zoho puller: honest write invariant + no server-side copy path",
          "--overwrite=false" in zsrc
          # the HEADER, not the word: upload()'s docstring names
          # If-None-Match on purpose, to say what this path does NOT do
          and '"If-None-Match"' not in zsrc
          and '"x-ms-copy-source' not in zsrc)
    check("zoho puller: credentials never reach argv",
          "--client-secret" not in zsrc and "--refresh-token" not in zsrc)
    _zmods = set()
    for _n in ast.walk(ast.parse(zsrc)):
        if isinstance(_n, ast.Import):
            for _a in _n.names:
                _zmods.add((_a.asname or _a.name).split(".")[0])
        elif isinstance(_n, ast.ImportFrom) and _n.level == 0:
            _zmods.add((_n.module or "").split(".")[0])
    check("zoho puller is stdlib-only (nothing to pip install on the VM)",
          _zmods <= {"argparse", "concurrent", "csv", "hashlib", "io",
                     "json", "os", "shutil", "subprocess", "sys", "time",
                     "urllib", "zipfile", "datetime", "pathlib",
                     "__future__"}, str(sorted(_zmods)))

    print("\n— zoho_transfer / zoho_vm_pull in-process (stubbed transport) —")

    class _ZResp:
        def __init__(self, payload, status=200):
            self._b = (payload if isinstance(payload, bytes)
                       else json.dumps(payload).encode())
            self.status = status

        def read(self, n=None):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _zhttp_err(code, body=b"{}", headers=None):
        return urllib.error.HTTPError(
            "https://x", code, "err", headers or {}, io.BytesIO(body))

    zsaved = (zoho_transfer._http, zoho_transfer._sleep)
    try:
        zsleeps = []
        zoho_transfer._sleep = lambda s: zsleeps.append(s)

        check("zoho DC layer: hosts derive from one validated value",
              zoho_transfer.validate_dc("EU") == "eu"
              and zoho_transfer.validate_dc(".com.au") == "com.au"
              and zoho_transfer.accounts_host("eu") == "accounts.zoho.eu"
              and zoho_transfer.api_host("com.au") == "www.zohoapis.com.au"
              and zoho_transfer.learn_hosts("eu") == ["learn.zoho.eu",
                                                      "learn.zoho.com"])
        try:
            zoho_transfer.validate_dc("gov")
            _ok = False
        except common.HarnessError:
            _ok = True
        check("zoho DC layer: unknown data center rejected", _ok)
        try:
            zoho_transfer._dc_guard("eu", {"zoho_dc": "com"})
            _ok = False
        except common.HarnessError as e:
            _ok = "eu" in str(e) and "com" in str(e)
        check("zoho _dc_guard: stdin-vs-tag mismatch names both DCs", _ok)
        zoho_transfer._dc_guard("com", {})
        check("zoho _dc_guard: dry-run VMs have no tags, so absent is a "
              "no-op", True)

        _mint_calls = {"n": 0}

        def _mint_http(req, timeout=60):
            _mint_calls["n"] += 1
            return _ZResp({"access_token": "AT", "expires_in": 3600,
                           "api_domain": "https://www.zohoapis.com"})
        zoho_transfer._http = _mint_http
        box = zoho_transfer.TokenBox("com", "id", "sec", "ref")
        check("zoho TokenBox: one mint serves repeated get()",
              box.get() == "AT" and box.get() == "AT"
              and _mint_calls["n"] == 1
              and box.api_domain == "https://www.zohoapis.com")
        box.invalidate()
        check("zoho TokenBox: invalidate forces exactly one re-mint",
              box.get() == "AT" and _mint_calls["n"] == 2)

        zoho_transfer._http = lambda req, timeout=60: _ZResp(
            {"access_token": "AT", "expires_in": 3600,
             "api_domain": "https://www.zohoapis.eu"})
        try:
            zoho_transfer.TokenBox("com", "i", "s", "r").mint()
            _ok = False
        except common.HarnessError as e:
            _ok = ("zohoapis.eu" in str(e) and "zohoapis.com" in str(e)
                   and "api_domain" in str(e))
        check("zoho TokenBox: api_domain cross-check catches the wrong DC",
              _ok)

        zsleeps.clear()
        zoho_transfer._http = lambda req, timeout=60: _ZResp(
            {"error": "invalid_code"})
        try:
            zoho_transfer.TokenBox("eu", "i", "s", "r").mint()
            _ok = False
        except common.HarnessError as e:
            _ok = ("WRONG DATA CENTER" in str(e) and "revoked" in str(e)
                   and "'eu'" in str(e))
        check("zoho TokenBox: invalid_code names BOTH causes, never sleeps",
              _ok and zsleeps == [])
        zoho_transfer._http = lambda req, timeout=60: _ZResp(
            {"error": "invalid_client"})
        try:
            zoho_transfer.TokenBox("com", "i", "s", "r").mint()
            _ok = False
        except common.HarnessError as e:
            _ok = "invalid_client" in str(e) and "client_id" in str(e)
        check("zoho TokenBox: invalid_client is a distinct diagnosis", _ok)

        zsleeps.clear()
        _seq = [_zhttp_err(429, b"{}", {"Retry-After": "7"}),
                _ZResp({"data": [{"id": "1"}]})]

        def _seq_http(req, timeout=60):
            item = _seq.pop(0)
            if isinstance(item, urllib.error.HTTPError):
                raise item
            return item
        box2 = zoho_transfer.TokenBox("com", "i", "s", "r")
        box2._value, box2._exp = "AT", time.time() + 9999
        zoho_transfer._http = _seq_http
        got = zoho_transfer.zoho_get(box2, "www.zohoapis.com", "/crm/v8/Leads")
        check("zoho zoho_get: 429 honors Retry-After exactly, then succeeds",
              zsleeps == [7] and got == {"data": [{"id": "1"}]})

        zsleeps.clear()
        _scope_body = b'{"code":"OAUTH_SCOPE_MISMATCH"}'
        _calls = {"n": 0}

        def _scope_http(req, timeout=60):
            _calls["n"] += 1
            raise _zhttp_err(401, _scope_body)
        zoho_transfer._http = _scope_http
        box3 = zoho_transfer.TokenBox("com", "i", "s", "r")
        box3._value, box3._exp = "AT", time.time() + 9999
        try:
            zoho_transfer.zoho_get(box3, "www.zohoapis.com", "/crm/v8/Leads")
            _ok = False
        except common.HarnessError as e:
            _ok = "OAUTH_SCOPE_MISMATCH" in str(e) and "cannot help" in str(e)
        check("zoho zoho_get: scope mismatch aborts on the FIRST call",
              _ok and _calls["n"] == 1 and zsleeps == [])

        check("zoho_error_code tolerates all four body shapes + garbage",
              zoho_transfer.zoho_error_code('{"code":"X"}') == "X"
              and zoho_transfer.zoho_error_code('[{"code":"Y"}]') == "Y"
              and zoho_transfer.zoho_error_code(
                  '{"data":[{"code":"Z"}]}') == "Z"
              and zoho_transfer.zoho_error_code(
                  b'{"error":"invalid_code"}') == "invalid_code"
              and zoho_transfer.zoho_error_code("<html>nope") == ""
              and zoho_transfer.zoho_error_code(None) == "")
    finally:
        (zoho_transfer._http, zoho_transfer._sleep) = zsaved

    _mods = ["Contacts", "Deals", "Tasks", "Notes"]
    _src = (SCRIPTS / "zoho_vm_pull.py").read_text()
    check("zoho unit order: attachments (fast, most of the bytes) run "
          "BEFORE the multi-hour email sweep",
          _src.index('units.append(("attachments", None))')
          < _src.index('units.append(("emails", m))'))
    check("zoho email_modules: scoping the 1-call-per-record sweep is the "
          "difference between ~12 h and ~54 h",
          zoho_vm_pull.email_modules(_mods, False, "Contacts,Deals")
          == ["Contacts", "Deals"]
          and zoho_vm_pull.email_modules(_mods, False, None) == _mods
          and zoho_vm_pull.email_modules(_mods, False, "") == _mods
          and zoho_vm_pull.email_modules(_mods, True, "Contacts") == []
          and zoho_vm_pull.email_modules(_mods, False, "Nope") == [])
    check("zoho classify: body code decides, not the status (github's "
          "404-vs-403 rule does NOT port)",
          zoho_vm_pull.classify(401, "OAUTH_SCOPE_MISMATCH", True) == "fatal"
          and zoho_vm_pull.classify(401, "OAUTH_SCOPE_MISMATCH",
                                    False) == "skip"
          and zoho_vm_pull.classify(401, "INVALID_TOKEN",
                                    True) == "remint-once-then-fatal"
          and zoho_vm_pull.classify(404, "", True) == "fatal"
          and zoho_vm_pull.classify(404, "", False) == "skip"
          and zoho_vm_pull.classify(400, "INVALID_MODULE", True) == "skip"
          and zoho_vm_pull.classify(429, "", True) == "sleep"
          and zoho_vm_pull.classify(503, "", True) == "retry")
    check("zoho REQUIRED_KINDS: only the schema is unconditionally "
          "required — one inaccessible module must not lose the other 91",
          zoho_vm_pull.REQUIRED_KINDS == ("settings",)
          and zoho_vm_pull.classify(401, "OAUTH_SCOPE_MISMATCH",
                                    "records" in
                                    zoho_vm_pull.REQUIRED_KINDS) == "skip"
          and zoho_vm_pull.classify(401, "OAUTH_SCOPE_MISMATCH",
                                    "settings" in
                                    zoho_vm_pull.REQUIRED_KINDS) == "fatal")
    _rc = (SCRIPTS / "zoho_vm_pull.py").read_text()
    check("zoho wholesale-failure guard: a scope list missing "
          "ZohoCRM.modules.ALL still aborts loudly",
          "records_ok == 0" in _rc
          and "systemic, not a per-module permission quirk" in _rc
          and "record_modules_inaccessible" in _rc)
    check("zoho classify: NO_PERMISSION is a per-module skip under ANY "
          "status — Zoho sends it as 400 on /settings/fields and 403 "
          "elsewhere; keying it on 403 killed a live run at 21/187 units",
          zoho_vm_pull.classify(400, "NO_PERMISSION", True) == "skip"
          and zoho_vm_pull.classify(403, "NO_PERMISSION", True) == "skip"
          and zoho_vm_pull.classify(500, "NO_PERMISSION", True) == "skip")
    check("zoho classify: a required unit is still fatal on a bare 400 with "
          "no recognised code (no silent data loss)",
          zoho_vm_pull.classify(400, "", True) == "fatal"
          and zoho_vm_pull.classify(400, "", False) == "skip")
    _chunks = zoho_vm_pull.fields_param(
        [f"f{i}" for i in range(450)], chunk=100)
    check("zoho fields_param: chunks a wide module, every chunk carries id",
          len(_chunks) == 5
          and all(c.startswith("id,") for c in _chunks)
          and sum(len(c.split(",")) - 1 for c in _chunks) == 450)
    check("zoho fields_param: a module with no fields still selects id",
          zoho_vm_pull.fields_param([]) == ["id"])
    check("zoho fields_param: default chunk respects v8's HARD 50-field cap "
          "(400 LIMIT_EXCEEDED above it)",
          zoho_vm_pull.FIELD_CHUNK <= 50
          and all(len(c.split(",")) <= 50 for c in
                  zoho_vm_pull.fields_param([f"f{i}" for i in range(500)])))
    _merged = zoho_vm_pull.merge_chunks([
        [{"id": "1", "a": 1}, {"id": "2", "a": 2}],
        [{"id": "1", "b": 9}, {"id": "2", "b": 8}],
        [{"id": "3", "c": 7}]])
    check("zoho merge_chunks: merges by id, keeps order, drops nothing",
          [r["id"] for r in _merged] == ["1", "2", "3"]
          and _merged[0] == {"id": "1", "a": 1, "b": 9}
          and _merged[2] == {"id": "3", "c": 7})
    _zt = Path(tempfile.mkdtemp(prefix="zoho-test-"))
    _jl = _zt / "records.jsonl"
    _l1 = json.dumps({"id": "1"}) + "\n"
    _l2 = json.dumps({"id": "2"}) + "\n"
    _jl.write_text(_l1 + _l2 + json.dumps({"id": "3"}) + "\n")
    check("zoho resume_truncate: rewinds to the last whole page",
          zoho_vm_pull.resume_truncate(
              _jl, {"bytes": len(_l1 + _l2)}) == 2
          and _jl.read_text() == _l1 + _l2)
    _jl.write_text(_l1 + _l2 + '{"id": "3', )
    check("zoho resume_truncate: a torn trailing line is discarded",
          zoho_vm_pull.resume_truncate(
              _jl, {"bytes": len(_l1 + _l2)}) == 2
          and "3" not in _jl.read_text())
    check("zoho resume_truncate: absent cursor means start over",
          zoho_vm_pull.resume_truncate(_jl, {}) == 0)
    _zip = _zt / "job.zip"
    with zipfile.ZipFile(_zip, "w") as zf:
        zf.writestr("job.csv", "id,name\n1,a\n2,b\n3,c\n")
    _jl.write_text(_l1 + _l2 + json.dumps({"id": "3"}) + "\n")
    _cc = zoho_vm_pull.bulk_crosscheck(_zip, _jl)
    check("zoho bulk_crosscheck: CSV rows vs ledger lines, delta 0",
          _cc["csv_rows"] == 3 and _cc["json_records"] == 3
          and _cc["delta"] == 0 and "delta_note" not in _cc)
    _jl.write_text(_l1 + _l2 + json.dumps({"id": "3"}) + "\n"
                   + json.dumps({"id": "4"}) + "\n")
    _cc = zoho_vm_pull.bulk_crosscheck(_zip, _jl)
    check("zoho bulk_crosscheck: a delta is informational snapshot skew",
          _cc["delta"] == 1 and "informational" in _cc["delta_note"]
          and "authority" in _cc["delta_note"])
    check("zoho safe_component: deterministic, separator-free, id-first "
          "keeps same-named attachments apart",
          zoho_vm_pull.safe_component("réport (v2).pdf")
          == zoho_vm_pull.safe_component("réport (v2).pdf")
          and "/" not in zoho_vm_pull.safe_component("a/b/c")
          and len(zoho_vm_pull.safe_component("x" * 400)) == 120
          and (f"111__{zoho_vm_pull.safe_component('f.pdf')}"
               != f"222__{zoho_vm_pull.safe_component('f.pdf')}"))
    _man = zoho_vm_pull.build_manifest(
        "crm", "com", "https://www.zohoapis.com", {"modules_selected": 2},
        "T0", "T1", 4242, [
            {"unit": "settings", "kind": "settings", "status": "ok",
             "bytes": 10},
            {"unit": "records/Leads", "kind": "records", "status": "ok",
             "bytes": 100, "records": 5},
            {"unit": "attachments/Deals", "kind": "attachments",
             "status": "failed", "detail": "boom"},
            {"unit": "emails/Contacts", "kind": "emails",
             "status": "skipped", "reason": "endpoint-absent"}])
    check("zoho build_manifest: skipped is NEVER counted as failed",
          _man["failed_units"] == ["attachments/Deals"]
          and _man["skipped_units"] == [
              {"unit": "emails/Contacts", "reason": "endpoint-absent",
               "detail": None}]
          and _man["total_staged_bytes"] == 110
          and _man["api_calls"] == 4242)
    _p = "zoho-export/crm"
    _clean = {
        f"{_p}/settings/.cdp-complete": {"size": 0},
        f"{_p}/settings/modules.json": {"size": 10},
        f"{_p}/records/Leads/.cdp-complete": {"size": 0},
        f"{_p}/records/Leads/records.jsonl": {"size": 100},
        f"{_p}/manifest.json": {"size": 5},
    }
    _zman = {"product": "crm", "unit_count": 2, "total_staged_bytes": 110,
             "failed_units": [], "skipped_units": [],
             "results": [
                 {"unit": "settings", "kind": "settings", "status": "ok",
                  "bytes": 10},
                 {"unit": "records/Leads", "kind": "records", "status": "ok",
                  "bytes": 100, "records": 5}]}
    r = zoho_transfer.compare_manifest_to_blobs(_zman, _clean, _p)
    check("zoho verify math: clean pass reports the record census",
          r["ok"] and not r["short_uploads"] and not r["missing_markers"]
          and r["record_census"] == {"records/Leads": 5})
    _short = dict(_clean)
    _short[f"{_p}/records/Leads/records.jsonl"] = {"size": 40}
    r = zoho_transfer.compare_manifest_to_blobs(_zman, _short, _p)
    check("zoho verify math: short upload fails",
          not r["ok"] and r["short_uploads"])
    _nomark = dict(_clean)
    del _nomark[f"{_p}/settings/.cdp-complete"]
    r = zoho_transfer.compare_manifest_to_blobs(_zman, _nomark, _p)
    check("zoho verify math: missing marker fails",
          not r["ok"] and r["missing_markers"] == [f"{_p}/settings/"])
    _extra = dict(_clean)
    _extra[f"{_p}/records/Leads/records.jsonl.old"] = {"size": 60}
    r = zoho_transfer.compare_manifest_to_blobs(_zman, _extra, _p)
    check("zoho verify math: stale extra is informational, not a failure",
          r["ok"] and r["stale_extra"] == [f"{_p}/records/Leads/"])
    _fman = dict(_zman)
    _fman["failed_units"] = ["attachments/Deals"]
    r = zoho_transfer.compare_manifest_to_blobs(_fman, _clean, _p)
    check("zoho verify math: failed_units surfaced verbatim",
          not r["ok"] and r["failed_units"] == ["attachments/Deals"])
    _sman = dict(_zman)
    _sman["skipped_units"] = [{"unit": "kb", "reason": "endpoint-absent"}]
    r = zoho_transfer.compare_manifest_to_blobs(_sman, _clean, _p)
    check("zoho verify math: a deliberate skip never fails the verify",
          r["ok"] and r["skipped_units"][0]["reason"] == "endpoint-absent")

    class _FakeAPI:
        """Minimal ZohoAPI stand-in: the pullers only ever call get_json."""

        def __init__(self, fields, pages, empty=False, count=3):
            self.fields = fields
            self.pages = list(pages)
            self.empty = empty
            self.count = count
            self.seen = []

        def get_json(self, path, params=None, host=None):
            self.seen.append((path, dict(params or {})))
            if path == "/crm/v8/settings/fields":
                return {"fields": [{"api_name": f} for f in self.fields]}
            if path.endswith("/actions/count"):
                return {"count": self.count}
            if self.empty:
                return None          # HTTP 204 — an EMPTY module
            return self.pages.pop(0) if self.pages else {"data": []}

    _dest = _zt / "crm"
    _api = _FakeAPI(["Company", "Email"], [
        {"data": [{"id": "1"}, {"id": "2"}],
         "info": {"more_records": True, "next_page_token": "TK"}},
        {"data": [{"id": "3"}], "info": {"more_records": False}}])
    _res = zoho_vm_pull.pull_module_records(_api, "Leads", _dest, False,
                                            None, 200)
    _ledger = _dest / "records" / "Leads" / "records.jsonl"
    check("zoho pull_module_records: paginates to exhaustion, writes ledger",
          _res["status"] == "ok" and _res["records"] == 3
          and _res["pages"] == 2
          and len(_ledger.read_text().strip().splitlines()) == 3
          and (_dest / "records" / "Leads" / ".cdp-complete").exists())
    check("zoho pull_module_records: v8's mandatory fields param is sent",
          any(p == "/crm/v8/Leads" and "fields" in q and "id" in q["fields"]
              for p, q in _api.seen))
    _res2 = zoho_vm_pull.pull_module_records(_api, "Leads", _dest, False,
                                             None, 200)
    check("zoho pull_module_records: a complete unit is skipped on re-run",
          _res2["status"] == "skipped-complete")
    _edest = _zt / "crm-empty"
    _eres = zoho_vm_pull.pull_module_records(
        _FakeAPI(["A"], [], empty=True), "Empty", _edest, False, None, 200)
    check("zoho pull_module_records: HTTP 204 is an EMPTY module, not an "
          "error", _eres["status"] == "ok" and _eres["records"] == 0
          and (_edest / "records" / "Empty" / ".cdp-complete").exists())

    check("zoho is_pagination_cap: the 100k ceiling is NOT a stale token",
          zoho_vm_pull.is_pagination_cap(
              zoho_vm_pull.ZohoAPIError(400, "PAGINATION_LIMIT_EXCEEDED", "x"))
          and not zoho_vm_pull.is_pagination_cap(
              zoho_vm_pull.ZohoAPIError(400, "INVALID_TOKEN", "x")))
    _dj = _zt / "dedupe.jsonl"
    _dj.write_text("".join(json.dumps({"id": i}) + "\n"
                           for i in ["1", "2", "3", "2", "1"]))
    check("zoho dedupe_jsonl_by_id: keeps first per id (two-ended walks "
          "overlap in the middle)",
          zoho_vm_pull.dedupe_jsonl_by_id(_dj) == (3, 2)
          and [json.loads(x)["id"] for x in
               _dj.read_text().strip().splitlines()] == ["1", "2", "3"])

    class _CapAPI:
        """Zoho's real behaviour: page_token dies at the cap. A blind
        re-walk on that 400 is an infinite loop — this pins that it cannot
        happen again, and that the other end is walked instead."""

        def __init__(self, per_dir=3, total=1000):
            self.calls, self.dirs, self.total = 0, [], total

        def get_json(self, path, params=None, host=None):
            p = params or {}
            if path == "/crm/v8/settings/fields":
                return {"fields": [{"api_name": "A"}]}
            if path.endswith("/actions/count"):
                return {"count": self.total}
            self.calls += 1
            if self.calls > 60:
                raise AssertionError("INFINITE LOOP: blind page_token re-walk")
            d = p.get("sort_order")
            self.dirs.append(d)
            n = sum(1 for x in self.dirs if x == d)
            if n > 3:
                raise zoho_vm_pull.ZohoAPIError(
                    400, "PAGINATION_LIMIT_EXCEEDED", "cap")
            base = 0 if d == "asc" else 500
            return {"data": [{"id": str(base + n * 10 + i)} for i in range(2)],
                    "info": {"more_records": True, "next_page_token": f"T{n}"}}
    _cd = _zt / "cap"
    _capi = _CapAPI(total=1000)
    _cres = zoho_vm_pull.pull_module_records(_capi, "Tasks", _cd, False,
                                             None, 200)
    check("zoho pagination cap: no infinite re-walk, BOTH ends walked, "
          "result flagged when the ledger is short of the source",
          _capi.calls <= 60
          and "asc" in _capi.dirs and "desc" in _capi.dirs
          and _cres["hit_pagination_cap"] is True
          and _cres["complete"] is False
          and _cres["status"] == "partial"
          and _cres["source_count"] == 1000
          and any("INCOMPLETE" in n for n in (_cres.get("notes") or [])))
    _capi2 = _CapAPI(total=12)
    _cres2 = zoho_vm_pull.pull_module_records(_capi2, "Small", _zt / "cap2",
                                              False, None, 200)
    check("zoho pagination cap: a module the two ends DO cover reads as "
          "complete", _cres2["complete"] is True
          and _cres2["status"] == "ok")
    _pman = zoho_vm_pull.build_manifest(
        "crm", "com", None, {}, "T0", "T1", 1,
        [{"unit": "records/Tasks", "kind": "records", "status": "partial",
          "bytes": 5, "records": 100000, "source_count": 114999}])
    check("zoho manifest: a partial unit is neither failed nor skipped, and "
          "its bytes still count",
          _pman["partial_units"] == [
              {"unit": "records/Tasks", "records": 100000,
               "source_count": 114999,
               "reason": "zoho pagination ceiling"}]
          and _pman["failed_units"] == [] and _pman["skipped_units"] == []
          and _pman["total_staged_bytes"] == 5)
    _pv = zoho_transfer.compare_manifest_to_blobs(
        {"results": [{"unit": "records/Tasks", "status": "partial",
                      "bytes": 5}],
         "failed_units": [], "skipped_units": [],
         "partial_units": [{"unit": "records/Tasks", "records": 100000,
                            "source_count": 114999}]},
        {"p/records/Tasks/.cdp-complete": {"size": 0},
         "p/records/Tasks/records.jsonl": {"size": 9}}, "p")
    check("zoho verify: a partial unit still certifies staged->container, "
          "but is surfaced as a source-completeness warning",
          _pv["ok"] and _pv["partial_units"])

    class _WideAPI(_FakeAPI):
        def get_json(self, path, params=None, host=None):
            self.seen.append((path, dict(params or {})))
            if path == "/crm/v8/settings/fields":
                return {"fields": [{"api_name": f} for f in self.fields]}
            if path.endswith("/actions/count"):
                return {"count": self.count}
            if "ids" in (params or {}):
                return {"data": [{"id": "1", "z": 1}, {"id": "2", "z": 2}]}
            return {"data": [{"id": "1", "a": 1}, {"id": "2", "a": 2}],
                    "info": {"more_records": False}}
    _wdest = _zt / "wide"
    _wapi = _WideAPI([f"f{i}" for i in range(150)], [], count=2)
    _wres = zoho_vm_pull.pull_module_records(_wapi, "Wide", _wdest, False,
                                             None, 200)
    _wrows = [json.loads(x) for x in
              (_wdest / "records" / "Wide" / "records.jsonl")
              .read_text().strip().splitlines()]
    check("zoho wide module: extra field chunks re-fetch by ids and merge — "
          "a page_token is never reused across different queries",
          _wres["field_chunks"] > 1
          and any("ids" in q for _, q in _wapi.seen)
          and not any("page_token" in q and "ids" in q
                      for _, q in _wapi.seen)
          and _wrows[0] == {"id": "1", "a": 1, "z": 1})

    check("zoho body_failure: a 200 with a failure BODY is a refusal, not "
          "success (Learn's Access Denied arrives this way)",
          zoho_vm_pull.body_failure(
              {"result": "failure", "errorCode": "9001"}) == "9001"
          and zoho_vm_pull.body_failure(
              {"status": "failure", "errorCode": "INVALID_METHOD"})
          == "INVALID_METHOD"
          and zoho_vm_pull.body_failure({"errorCode": "EXTRA_PARAM_FOUND"})
          == "EXTRA_PARAM_FOUND"
          # success shapes must NOT trip it
          and zoho_vm_pull.body_failure({"STATUS": "OK", "DATA": []}) == ""
          and zoho_vm_pull.body_failure(
              {"data": [{"id": "1"}], "info": {}}) == ""
          and zoho_vm_pull.body_failure({"modules": []}) == ""
          and zoho_vm_pull.body_failure(None) == "")

    class _DeniedAPI:
        """/course answers; /customportal returns HTTP 200 + Access Denied."""

        def get_json(self, path, params=None, host=None):
            if path.endswith("/course"):
                return {"STATUS": "OK", "DATA": []}
            code = zoho_vm_pull.body_failure(
                {"result": "failure", "errorCode": "9001"})
            raise zoho_vm_pull.ZohoAPIError(200, code, "denied")
    _dd = zoho_vm_pull.learn_discover(_DeniedAPI(), "songdivision", "com")
    check("zoho learn_discover: an Access-Denied path is NOT reported as "
          "reachable",
          _dd["kb_reachable"] == []
          and all(v["state"].startswith("200-9001")
                  for v in _dd["kb"].values())
          and "COURSES ONLY" in _dd["note"])

    class _AttAPI:
        """Two pages of the Attachments module, then a download per row."""

        def __init__(self):
            self.listed = 0
            self.downloads = []

        def get_json(self, path, params=None, host=None):
            assert path == "/crm/v8/Attachments", path
            self.listed += 1
            if self.listed == 1:
                return {"data": [
                    {"id": "a1", "File_Name": "p.pdf", "Size": "100",
                     "Parent_Id": {"module": {"api_name": "Deals"},
                                   "id": "d1"}},
                    {"id": "a2", "File_Name": "p.pdf", "Size": "200",
                     "Parent_Id": {"module": {"api_name": "Deals"},
                                   "id": "d1"}}],
                    "info": {"more_records": True, "next_page_token": "T"}}
            return {"data": [
                {"id": "a3", "File_Name": "q.pdf", "Size": "50",
                 "Parent_Id": {"module": {"api_name": "Contacts"},
                               "id": "c9"}}],
                "info": {"more_records": False}}

        def download(self, path, out_path):
            self.downloads.append(path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"x")
            return 1
    _adest = _zt / "att"
    _aapi = _AttAPI()
    _ares = zoho_vm_pull.pull_attachments(_aapi, _adest, False, 2)
    check("zoho attachments: ONE module walk, not a per-record sweep",
          _aapi.listed == 2 and _ares["listed"] == 3 and _ares["files"] == 3
          and _ares["expected_bytes"] == 350
          and all(p.startswith("/crm/v8/") and "/Attachments/" in p
                  for p in _aapi.downloads))
    check("zoho attachments: id-first names keep same-filename files apart, "
          "foldered by parent module/record",
          (_adest / "attachments" / "Deals" / "d1"
           / "a1__p.pdf").exists()
          and (_adest / "attachments" / "Deals" / "d1"
               / "a2__p.pdf").exists()
          and (_adest / "attachments" / "Contacts" / "c9"
               / "a3__q.pdf").exists())
    _aapi2 = _AttAPI()
    _ares2 = zoho_vm_pull.pull_attachments(_aapi2, _adest, False, 2)
    check("zoho attachments: complete unit skipped on re-run",
          _ares2["status"] == "skipped-complete"
          and _aapi2.downloads == [])

    class _EmAttAPI:
        """Email attachments: the ledger drives it, detail yields ids, and
        the DEDICATED download endpoint (not /Attachments/) fetches them."""

        def __init__(self):
            self.detail_calls = 0
            self.dl = []

        def get_json(self, path, params=None, host=None):
            self.detail_calls += 1
            assert "/Emails/" in path, path
            return {"Emails": [{"attachments": [
                {"id": "hex1", "name": "a.pdf", "size": 10},
                {"id": "hex2", "name": "b.pdf", "size": 20}]}]}

        def download(self, path, out_path, params=None):
            self.dl.append((path, dict(params or {})))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"x")
            return 1
    _ed = _zt / "ematt"
    (_ed / "emails" / "Contacts").mkdir(parents=True, exist_ok=True)
    (_ed / "emails" / "Contacts" / "emails.jsonl").write_text(
        json.dumps({"record_id": "r1", "messages": [
            {"message_id": "m1", "has_attachment": "true",
             "owner": {"id": "u1"}},
            {"message_id": "m2", "has_attachment": "false",
             "owner": {"id": "u1"}}]}) + "\n")
    _eapi = _EmAttAPI()
    _eres = zoho_vm_pull.pull_email_attachments(_eapi, "Contacts", _ed,
                                                False, 2)
    check("zoho email attachments: only has_attachment messages are opened, "
          "and the DEDICATED download endpoint is used (not /Attachments/)",
          _eapi.detail_calls == 1
          and _eres["messages_with_attachments"] == 1
          and _eres["files"] == 2
          and all(p.endswith("/Emails/actions/download_attachments")
                  for p, _ in _eapi.dl)
          and all(q.get("message_id") == "m1" and q.get("user_id") == "u1"
                  and q.get("id") in ("hex1", "hex2")
                  for _, q in _eapi.dl))
    check("zoho email attachments: no email ledger is a recorded skip",
          zoho_vm_pull.pull_email_attachments(
              _eapi, "Nope", _ed, False, 2)["reason"] == "no-email-ledger")
    _asrc = (SCRIPTS / "zoho_vm_pull.py").read_text()
    check("zoho attachments: Emails-parented rows are DEFERRED, not counted "
          "as download errors (they need the other endpoint)",
          'if pmod == "Emails":' in _asrc
          and "deferred_to_email_unit" in _asrc)
    check("zoho notes: sends v8's mandatory fields param",
          '"fields": ",".join(note_fields)' in _asrc)

    class _KBAPI:
        def __init__(self, ok_paths):
            self.ok = ok_paths

        def get_json(self, path, params=None, host=None):
            if any(path.endswith(s) for s in self.ok):
                return {"data": []}
            raise zoho_vm_pull.ZohoAPIError(404, "", f"HTTP 404 on {path}")
    _disc = zoho_vm_pull.learn_discover(_KBAPI(["/course"]), "zylker", "com")
    check("zoho learn_discover: KB is discovered, never assumed",
          _disc["course_host"] == "learn.zoho.com"
          and _disc["kb_reachable"] == []
          and "COURSES ONLY" in _disc["note"]
          and all(v["state"].startswith("404")
                  for v in _disc["kb"].values()))
    _disc2 = zoho_vm_pull.learn_discover(
        _KBAPI(["/course", "/tag"]), "zylker", "eu")
    check("zoho learn_discover: a reachable KB path is recorded and used",
          _disc2["kb_reachable"] == ["tags"]
          and "Reachable: tags" in _disc2["note"])
    check("zoho learn_rows: Learn answers in UPPERCASE keys, unlike CRM",
          zoho_vm_pull.learn_rows({"STATUS": "OK", "DATA": [1, 2]}) == [1, 2]
          and zoho_vm_pull.learn_rows({"data": [3]}) == [3]
          and zoho_vm_pull.learn_rows({"STATUS": "OK", "QUIZ": []}) == []
          and zoho_vm_pull.learn_rows(None) == [])

    class _ViewAPI:
        """Mimics Learn's default scoping: view=learn (the API default) sees
        only the caller's enrolments — zero for a Self Client admin."""

        def __init__(self):
            self.views = []
            self.lesson_params = []

        def get_json(self, path, params=None, host=None):
            p = params or {}
            if path.endswith("/course"):
                self.views.append(p.get("view"))
                if p.get("view") != "all":
                    return {"STATUS": "OK", "DATA": [],
                            "DASHBOARD": {"totalCourses": "0"}}
                return {"STATUS": "OK", "DATA": [{"id": "c1"}, {"id": "c2"}]}
            if "/lesson/" in path:
                self.lesson_params.append(params or {})
                return {"STATUS": "OK", "DATA": {"id": "L1"}}
            return {"STATUS": "OK",
                    "COURSE": {"id": "c1", "url": "cslug", "lessonCount": "1",
                               "lessons": [{"id": "L1", "url": "lslug"}]}}
    _vapi = _ViewAPI()
    _vd = _zt / "learn-view"
    _vdisc = {"course_host": "learn.zoho.com"}
    _vres = zoho_vm_pull.pull_learn_courses(_vapi, "songdivision", _vdisc,
                                            _vd, False)
    check("zoho courses: view=all is REQUIRED — the API default 'learn' "
          "means only the caller's enrolments and silently reports an "
          "empty portal",
          zoho_vm_pull.LEARN_COURSE_VIEW == "all"
          and all(v == "all" for v in _vapi.views)
          and _vres["items"] == 2 and _vres["details"] == 2
          and (_vd / "courses" / "detail" / "c1.json").exists())
    check("zoho flatten_lessons: CHAPTERs nest their BLOCKs — top-level "
          "iteration silently drops the real content",
          [L["id"] for L in zoho_vm_pull.flatten_lessons([
              {"id": "ch", "lessons": [{"id": "b1"}, {"id": "b2"}]},
              {"id": "v1"}])] == ["ch", "b1", "b2", "v1"]
          and zoho_vm_pull.flatten_lessons(None) == []
          and zoho_vm_pull.flatten_lessons([{"id": "a", "lessons": []}])
          == [{"id": "a", "lessons": []}])
    check("zoho lessons: detail route needs BOTH lesson.url and course.url "
          "(slugs, not ids) and is gated on ZohoLearn.lesson.READ",
          _vres["lessons"] == 2
          and all(q.get("lesson.url") == "lslug"
                  and q.get("course.url") == "cslug"
                  for q in _vapi.lesson_params)
          and (_vd / "courses" / "lessons" / "c1" / "L1.json").exists())

    class _NoLimitAPI:
        """A collection that IGNORES `limit` and returns everything every
        time — /tag does exactly this (755 rows, verified live)."""

        def __init__(self):
            self.calls = 0

        def get_json(self, path, params=None, host=None):
            self.calls += 1
            return {"STATUS": "OK",
                    "DATA": [{"id": str(i)} for i in range(755)]}
    _nl = _NoLimitAPI()
    _nld = _zt / "learn-nolimit"
    _nld.mkdir(parents=True, exist_ok=True)
    _n = zoho_vm_pull._learn_walk(_nl, "h", "/p", _nld, "tags.jsonl")
    check("zoho _learn_walk: a limit-ignoring collection terminates, no "
          "duplicates", _n == 755 and _nl.calls == 2
          and len((_nld / "tags.jsonl").read_text().strip().splitlines())
          == 755)
    _kbres = zoho_vm_pull.pull_learn_kb(
        _KBAPI(["/course"]), "zylker", _disc, _zt / "learn", False)
    check("zoho pull_learn_kb: an absent KB is a recorded skip, not a "
          "failure",
          len(_kbres) == 1 and _kbres[0]["status"] == "skipped"
          and _kbres[0]["reason"] == "endpoint-absent"
          and (_zt / "learn" / "kb" / ".cdp-skipped.json").exists())
    shutil.rmtree(_zt)

    print("\n— figma_transfer --dry-run (engine lifecycle, VM-side REST "
          "puller) —")
    import figma_transfer  # noqa: E402
    import figma_vm_pull  # noqa: E402
    ftoken = "FIGDTOKENSENTINEL\n"
    check("figma Spec: PAT flow (no OAuth/rclone), 512 GB disk",
          figma_transfer.SPEC.vm_prefix == "xfer-figma-"
          and figma_transfer.SPEC.authorize_target == ""
          and figma_transfer.SPEC.remote_type == ""
          and figma_transfer.SPEC.default_dest_prefix == "figma-export"
          and figma_transfer.SPEC.default_os_disk_gb == 512)
    proc = run_script("figma_transfer.py", "plan", "democo", "--team-ids",
                      "123,456", "--root", root, "--dry-run")
    fplan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("figma plan: dest + comma-joined team source",
          fplan["vm_name"] == "xfer-figma-democo"
          and fplan["dest"] == "democo-raw/figma-export"
          and fplan["source"] == "figma:123,456")
    proc = run_script("figma_transfer.py", "create-vm", "democo",
                      "--team-ids", " 123 ,456", "--plan", "org",
                      "--root", root, "--dry-run")
    check("figma create-vm: 512 GB disk + the whole team list as ONE tag "
          "value (canonicalized), plan tag rides along",
          "--os-disk-size-gb 512" in proc.stdout
          and "purpose=figma-transfer" in proc.stdout
          and "figma_team_ids=123,456" in proc.stdout
          and "figma_plan=org" in proc.stdout
          and "dest_prefix=figma-export" in proc.stdout
          and "-n xfer-figma-democo" in proc.stdout)
    proc = run_script("figma_transfer.py", "plan", "democo", "--team-ids",
                      "12a", "--root", root, "--dry-run", expect_rc=1)
    check("figma team-id validation: a non-numeric id is a mis-paste, "
          "refused before any az call",
          "not numeric" in proc.stdout and "az " not in proc.stdout)
    proc = run_script("figma_transfer.py", "allow-network", "democo",
                      "--root", root, "--dry-run")
    check("figma allow-network: vnet path (VM family), never IP rules",
          "network-rule add" in proc.stdout and "--subnet" in proc.stdout
          and "--ip-address" not in proc.stdout)
    proc = run_script("figma_transfer.py", "write-dest", "democo",
                      "--root", root, "--dry-run")
    check("figma write-dest: racwl SAS -> rclone.conf + dest-figma.env, "
          "redacted",
          "--permissions racwl" in proc.stdout
          and "figma-export" in proc.stdout
          and "rclone.conf" in proc.stdout
          and "dest-figma.env" in proc.stdout
          and "redacted" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "figma_transfer.py"), "write-creds",
         "democo", "--team-ids", "123", "--root", str(root), "--dry-run"],
        input=ftoken, capture_output=True, text=True)
    check("figma write-creds: the token sentinel is never echoed",
          proc.returncode == 0
          and "FIGDTOKENSENTINEL" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    check("figma write-creds: dry-run echo stays brace-free before the "
          "JSON (VM smoke test uses curl -D -/sed, $VAR never braces)",
          "{" not in proc.stdout[:proc.stdout.index("{")]
          and json.loads(proc.stdout[proc.stdout.index("{"):])["ok"] is True)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "figma_transfer.py"), "write-creds",
         "democo", "--root", str(root), "--dry-run"],
        input="tok1\ntok2\n", capture_output=True, text=True)
    check("figma write-creds: refuses malformed stdin (must be 1 line)",
          proc.returncode == 1 and "1 line" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "figma_transfer.py"), "write-creds",
         "democo", "--root", str(root), "--dry-run"],
        input="fig'd\n", capture_output=True, text=True)
    check("figma write-creds: single-quote guard refuses before writing",
          proc.returncode == 1 and "single quote" in proc.stdout
          and "nothing was written" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "figma_transfer.py"), "probe",
         "democo", "--team-ids", "123,456", "--root", str(root),
         "--dry-run"], input=ftoken, capture_output=True, text=True)
    check("figma probe: laptop-side API JSON only — no Azure, no VM, "
          "token redacted",
          proc.returncode == 0
          and "api.figma.com/v1/me" in proc.stdout
          and "api.figma.com/v2/teams/123/folders" in proc.stdout
          and "token-redacted" in proc.stdout
          and "FIGDTOKENSENTINEL" not in proc.stdout
          and "generate-sas" not in proc.stdout
          and "network-rule" not in proc.stdout
          and "az vm" not in proc.stdout, proc.stdout[-300:])
    proc = run_script("figma_transfer.py", "transfer", "democo",
                      "--team-ids", "123", "--root", root, "--dry-run")
    check("figma transfer: pushes the puller fresh into tmux window "
          "'figma', sourcing both env files",
          "figma_vm_pull.py" in proc.stdout
          and "tmux new-session -d -s transfer -n figma" in proc.stdout
          and "figma.env" in proc.stdout
          and "dest-figma.env" in proc.stdout)
    proc = run_script("figma_transfer.py", "verify", "democo", "--root",
                      root, "--dry-run")
    check("figma verify: laptop path — ip rule + rl SAS + manifest, "
          "never the write SAS",
          "--permissions rl" in proc.stdout
          and "network-rule add" in proc.stdout
          and "figma-export/manifest.json" in proc.stdout
          and "racwl" not in proc.stdout)
    proc = run_script("figma_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", expect_rc=2)
    check("figma teardown also gated", '"not-confirmed"' in proc.stdout)
    proc = run_script("figma_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", "--confirmed")
    check("figma confirmed teardown: engine set + PAT revocation reminder",
          "network-rule remove" in proc.stdout
          and "vnet delete" in proc.stdout
          and "personal access token" in proc.stdout
          and "revoke" in proc.stdout.lower())
    fsrc = (SCRIPTS / "figma_vm_pull.py").read_text()
    check("figma puller: markers, library cursor, honest write invariant",
          ".cdp-complete" in fsrc and ".cdp-cursor.json" in fsrc
          and "--overwrite=false" in fsrc
          and '"If-None-Match"' not in fsrc
          and '"x-ms-copy-source' not in fsrc)
    check("figma puller: manifest REPLACES an earlier pass's, corpus never "
          "does — the github pilot-poisons-verify fix ships from day one",
          "--overwrite=true" in fsrc and "upload_run_metadata" in fsrc
          and ".cdp-cursor.json;" in fsrc
          and "--include-pattern" in fsrc)
    check("figma puller: the token header is built in exactly ONE place "
          "and never rides to the CDN (fills/renders are plain GETs)",
          fsrc.count('"X-Figma-Token"') == 1
          and "deliberately header-free" in fsrc)
    check("figma puller: the token never reaches argv",
          "--token" not in fsrc and "FIGMA_TOKEN" in fsrc)
    _fmods = set()
    for _n in ast.walk(ast.parse(fsrc)):
        if isinstance(_n, ast.Import):
            for _a in _n.names:
                _fmods.add((_a.asname or _a.name).split(".")[0])
        elif isinstance(_n, ast.ImportFrom) and _n.level == 0:
            _fmods.add((_n.module or "").split(".")[0])
    check("figma puller is stdlib-only (nothing to pip install on the VM)",
          _fmods <= {"argparse", "concurrent", "http", "json", "os",
                     "shutil", "subprocess", "sys", "time", "urllib",
                     "datetime", "pathlib", "__future__"}, str(sorted(_fmods)))

    print("\n— figma_transfer / figma_vm_pull in-process (stubbed "
          "transport) —")
    check("figma validate_team_ids: canonicalizes, rejects mis-pastes",
          figma_transfer.validate_team_ids(" 123 , 456 ") == "123,456")
    for _bad, _frag in (("12a", "not numeric"), ("", "empty"),
                        (",".join(["9" * 19] * 20), "batch")):
        try:
            figma_transfer.validate_team_ids(_bad)
            _ok = False
        except common.HarnessError as e:
            _ok = _frag in str(e)
        check(f"figma validate_team_ids: refuses {_frag}", _ok)
    check("figma classify: status+family decide (the INVERSE of zoho's "
          "body-code rule — Figma has no body codes)",
          figma_vm_pull.classify(429, "file", True) == "sleep"
          and figma_vm_pull.classify(403, "file", False) == "skip"
          and figma_vm_pull.classify(404, "file", False) == "skip"
          and figma_vm_pull.classify(403, "folders", True) == "fatal"
          and figma_vm_pull.classify(400, "file", False) == "decompose"
          and figma_vm_pull.classify(400, "nodes", False) == "decompose"
          and figma_vm_pull.classify(400, "comments", False) == "skip"
          and figma_vm_pull.classify(400, "comments", True) == "fatal"
          and figma_vm_pull.classify(500, "file", False) == "decompose"
          and figma_vm_pull.classify(503, "comments", False) == "retry")
    check("figma REQUIRED_KINDS: only the walk is unconditionally "
          "required — one 403 file must not lose the other 9,999",
          figma_vm_pull.REQUIRED_KINDS == ("meta",)
          and figma_vm_pull.classify(
              403, "file", "file" in figma_vm_pull.REQUIRED_KINDS)
          == "skip")

    _fclock = {"t": 0.0}
    _fsleeps: list = []

    def _fnow():
        return _fclock["t"]

    def _fslp(s):
        _fsleeps.append(round(s, 2))
        _fclock["t"] += s
    _fb = figma_vm_pull.TierBucket("org", now_fn=_fnow, sleep_fn=_fslp)
    for _ in range(18):  # org tier 1 = 20/min * 0.9 safety = 18 burst
        _fb.acquire(1)
    check("figma TierBucket: a burst within the documented cap never "
          "sleeps", _fsleeps == [])
    _fb.acquire(1)
    check("figma TierBucket: past the burst, pacing sleeps at the "
          "documented rate (1 token / 0.3 per s)",
          len(_fsleeps) == 1 and abs(_fsleeps[0] - (1 / 0.3)) < 0.1)
    _fsleeps.clear()
    _fb.on_429(1, 7)
    check("figma TierBucket: 429 honors Retry-After EXACTLY and drains "
          "the bucket", _fsleeps[0] == 7 and _fb._tokens[1] == 0.0)
    _fb2 = figma_vm_pull.TierBucket("enterprise", now_fn=_fnow,
                                    sleep_fn=_fslp)
    _fb2.on_429(1, 5, "starter")
    _fb3 = figma_vm_pull.TierBucket("starter", now_fn=_fnow,
                                    sleep_fn=_fslp)
    _fb3.on_429(1, 5, "enterprise")
    check("figma TierBucket: the plan header downgrades pacing, never "
          "upgrades it",
          _fb2.plan == "starter" and _fb3.plan == "starter")

    _fest = figma_vm_pull.estimate_tier1(10000, 500, render_pages=False)
    check("figma estimate_tier1: counts + wall-clock per plan, NEVER bytes",
          _fest["tier1_calls"] == 10500
          and _fest["hours_by_plan"]["org"] == round(10500 / 18 / 60, 1)
          and "bytes" not in json.dumps(_fest["hours_by_plan"]))
    check("figma estimate_tier1: renders add ~one Tier-1 call per file",
          figma_vm_pull.estimate_tier1(100, 0, True)["tier1_calls"] == 200)
    check("figma page_node_ids: only top-level CANVAS children are pages",
          figma_vm_pull.page_node_ids(
              {"document": {"children": [
                  {"type": "CANVAS", "id": "1:2"},
                  {"type": "SECTION", "id": "9"}]}}) == ["1:2"]
          and figma_vm_pull.page_node_ids({}) == [])
    check("figma parse_render_map: a 200 with null values is a per-node "
          "FAILURE, never counted as delivered",
          figma_vm_pull.parse_render_map(
              {"images": {"a": "u", "b": None}}) == ({"a": "u"}, ["b"]))
    check("figma fill_ext: extension from Content-Type; unknown stays bare",
          figma_vm_pull.fill_ext("image/png") == ".png"
          and figma_vm_pull.fill_ext("image/jpeg; charset=x") == ".jpg"
          and figma_vm_pull.fill_ext("application/octet-stream") == "")
    _frow = {"team_id": "1", "folder_path": "Root", "key": "KEY1",
             "name": "My Design!", "branches": [{"key": "BR1"}]}
    check("figma unit_label: file KEY first, mutable name second — a "
          "rename never orphans the marker",
          figma_vm_pull.unit_label("file", _frow)
          == "files/1/KEY1__My_Design")
    _funits = figma_vm_pull.plan_units(
        [_frow, {"team_id": "1", "folder_path": "A", "key": "AKEY",
                 "name": "a"}], ["1"], 0, None)
    check("figma plan_units: library first, then files in deterministic "
          "(team, folder, key) order",
          [figma_vm_pull.unit_label(k, p) for k, p in _funits]
          == ["library/1", "files/1/AKEY__a", "files/1/KEY1__My_Design"])
    check("figma plan_units: --only matches a bare file key",
          [figma_vm_pull.unit_label(k, p) for k, p in
           figma_vm_pull.plan_units([_frow], ["1"], 0, "KEY1")]
          == ["files/1/KEY1__My_Design"])
    check("figma plan_units: --limit pilots the first N file units",
          len(figma_vm_pull.plan_units(
              [_frow, {"team_id": "1", "folder_path": "A", "key": "AKEY",
                       "name": "a"}], ["1"], 1, None,
              include_library=False)) == 1)

    _ft = Path(tempfile.mkdtemp(prefix="figma-test-"))
    _fjl = _ft / "lib.jsonl"
    _fl1 = json.dumps({"key": "1"}) + "\n"
    _fjl.write_text(_fl1 + '{"key": "2')
    check("figma resume_truncate: a torn trailing line is discarded",
          figma_vm_pull.resume_truncate(_fjl, {"bytes": len(_fl1)}) == 1
          and _fjl.read_text() == _fl1)

    _fman = figma_vm_pull.build_manifest(
        ["1"], "org", {"files_in_ledger": 2}, "T0", "T1", 99, [
            {"unit": "meta", "kind": "meta", "status": "ok", "bytes": 10},
            {"unit": "files/1/K__a", "kind": "file", "status": "ok",
             "bytes": 100, "decomposed": True, "fill_errors": 2,
             "render_nulls": 1},
            {"unit": "files/1/K2__b", "kind": "file", "status": "failed",
             "detail": "boom"},
            {"unit": "files/1/K3__c", "kind": "file", "status": "skipped",
             "reason": "no-access-or-missing"}])
    check("figma build_manifest: skipped is NEVER failed; decomposed and "
          "quality rollups surfaced",
          _fman["failed_units"] == ["files/1/K2__b"]
          and _fman["skipped_units"][0]["reason"] == "no-access-or-missing"
          and _fman["decomposed_files"] == ["files/1/K__a"]
          and _fman["total_staged_bytes"] == 110
          and _fman["fill_errors"] == 2 and _fman["render_nulls"] == 1)
    _fp = "figma-export"
    _fclean = {
        f"{_fp}/meta/.cdp-complete": {"size": 0},
        f"{_fp}/meta/files.jsonl": {"size": 10},
        f"{_fp}/files/1/K__a/.cdp-complete": {"size": 0},
        f"{_fp}/files/1/K__a/document.json": {"size": 100},
        f"{_fp}/manifest.json": {"size": 5},
    }
    _fvman = {"source": "figma", "unit_count": 2, "total_staged_bytes": 110,
              "failed_units": [], "skipped_units": [],
              "decomposed_files": [], "results": [
                  {"unit": "meta", "kind": "meta", "status": "ok",
                   "bytes": 10},
                  {"unit": "files/1/K__a", "kind": "file", "status": "ok",
                   "bytes": 100}]}
    r = figma_transfer.compare_manifest_to_blobs(_fvman, _fclean, _fp)
    check("figma verify math: clean pass",
          r["ok"] and not r["short_uploads"] and not r["missing_markers"])
    _fshort = dict(_fclean)
    _fshort[f"{_fp}/files/1/K__a/document.json"] = {"size": 40}
    r = figma_transfer.compare_manifest_to_blobs(_fvman, _fshort, _fp)
    check("figma verify math: short upload fails",
          not r["ok"] and r["short_uploads"])
    _fnomark = dict(_fclean)
    del _fnomark[f"{_fp}/meta/.cdp-complete"]
    r = figma_transfer.compare_manifest_to_blobs(_fvman, _fnomark, _fp)
    check("figma verify math: missing marker fails",
          not r["ok"] and r["missing_markers"] == [f"{_fp}/meta/"])
    _fextra = dict(_fclean)
    _fextra[f"{_fp}/files/1/K__a/old.json"] = {"size": 60}
    r = figma_transfer.compare_manifest_to_blobs(_fvman, _fextra, _fp)
    check("figma verify math: stale extra is informational, not a failure",
          r["ok"] and r["stale_extra"] == [f"{_fp}/files/1/K__a/"])
    _ffman = dict(_fvman)
    _ffman["failed_units"] = ["files/1/K2__b"]
    r = figma_transfer.compare_manifest_to_blobs(_ffman, _fclean, _fp)
    check("figma verify math: failed_units surfaced verbatim",
          not r["ok"] and r["failed_units"] == ["files/1/K2__b"])
    _fsman = dict(_fvman)
    _fsman["skipped_units"] = [{"unit": "files/1/K3__c",
                                "reason": "no-access-or-missing"}]
    r = figma_transfer.compare_manifest_to_blobs(_fsman, _fclean, _fp)
    check("figma verify math: a deliberate skip never fails the verify",
          r["ok"] and r["skipped_units"][0]["reason"]
          == "no-access-or-missing")

    class _FigAPI:
        """Minimal FigmaAPI stand-in: the pullers only call get_json."""

        def __init__(self, fail_full=False, null_once=None):
            self.seen = []
            self.fail_full = fail_full
            self.null_once = null_once
            self.render_calls = 0

        def get_json(self, family, path, params=None, absolute_url=None):
            self.seen.append((family, path or absolute_url,
                              dict(params or {})))
            p = params or {}
            if path.startswith("/v2/teams/"):
                return {"folders": [{"id": "f1", "name": "Root"}]}
            if path == "/v2/folders/f1/folders":
                return {"folders": [{"id": "f2", "name": "Sub"}]}
            if path == "/v2/folders/f2/folders":
                return {"folders": []}
            if path == "/v2/folders/f1/files":
                return {"files": [
                    {"key": "KEY1", "name": "My Design!",
                     "last_modified": "2026-01-01",
                     "branches": [{"key": "BR1", "name": "b"}]}]}
            if path == "/v2/folders/f2/files":
                return {"files": []}
            if path in ("/v1/files/KEY1", "/v1/files/BR1"):
                if self.fail_full and "depth" not in p \
                        and path == "/v1/files/KEY1":
                    raise figma_vm_pull.FigmaAPIError(
                        400, "file", "Request timeout, try smaller")
                return {"editorType": "figma", "version": "7",
                        "document": {"children": [
                            {"type": "CANVAS", "id": "1:2"},
                            {"type": "CANVAS", "id": "1:3"}]}}
            if path == "/v1/files/KEY1/nodes":
                return {"nodes": {i: {"document": {}}
                                  for i in p.get("ids", "").split(",")}}
            if path == "/v1/files/KEY1/comments":
                return {"comments": []}
            if path == "/v1/files/KEY1/versions" and not absolute_url:
                return {"versions": [{"id": "v1"}],
                        "pagination": {"next_page":
                                       "https://api.figma.com/x?page=2"}}
            if absolute_url:
                return {"versions": [{"id": "v2"}], "pagination": {}}
            if path == "/v1/files/KEY1/images":
                return {"meta": {"images": {"ref1": "https://cdn/x",
                                            "ref2": None}}}
            if path == "/v1/images/KEY1":
                self.render_calls += 1
                ids = p.get("ids", "").split(",")
                out = {i: f"https://cdn/{i}" for i in ids}
                if self.null_once and self.render_calls == 1 \
                        and self.null_once in out:
                    out[self.null_once] = None
                return {"images": out}
            raise figma_vm_pull.FigmaAPIError(404, family,
                                              f"unexpected {path}")

    _fcdn: list = []

    def _fake_cdn(url, out_base):
        _fcdn.append(url)
        out_path = out_base.with_name(out_base.name + ".png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"x")
        return 1, out_path
    _saved_cdn = figma_vm_pull.cdn_download
    figma_vm_pull.cdn_download = _fake_cdn
    try:
        _fdest = _ft / "dest"
        _fapi = _FigAPI()
        _mres, _mrows = figma_vm_pull.pull_meta(_fapi, _fdest, ["9"], False)
        check("figma pull_meta: recursive v2 walk writes the ledger, "
              "nested subfolder found, branches counted",
              _mres["status"] == "ok" and len(_mrows) == 1
              and _mrows[0]["key"] == "KEY1"
              and _mres["branches"] == 1
              and (_fdest / "meta" / ".cdp-complete").exists()
              and any(f == "folders" and "/v2/folders/f2/folders" in p
                      for f, p, _ in _fapi.seen))
        from types import SimpleNamespace as _NS
        _fargs = _NS(refresh=False, no_comments=False, no_versions=False,
                     no_fills=False, fill_workers=1, render_pages=True)
        _fres = figma_vm_pull.pull_file(_fapi, _mrows[0], _fdest, _fargs)
        _fudir = _fdest / "files" / "9" / "KEY1__My_Design"
        _fmeta = json.loads((_fudir / "file.json").read_text())
        check("figma pull_file: document + comments + paginated versions "
              "+ fills + per-page renders + branch tree, one marker",
              _fres["status"] == "ok"
              and (_fudir / "document.json").exists()
              and (_fudir / ".cdp-complete").exists()
              and _fmeta["editor_type"] == "figma"
              and len(json.loads((_fudir / "versions.json").read_text())
                      ["versions"]) == 2
              and (_fudir / "fills" / "ref1.png").exists()
              and (_fudir / "renders" / "1_2.png").exists()
              and (_fudir / "renders" / "1_3.png").exists()
              and (_fudir / "branches" / "BR1"
                   / "document.json").exists())
        check("figma pull_file: a null-URL fill is listed but never "
              "fetched (a 200 is not per-item success)",
              _fmeta["fills"]["listed"] == 2
              and _fmeta["fills"]["downloaded"] == 1
              and not any("ref2" in u for u in _fcdn))
        _fres2 = figma_vm_pull.pull_file(_fapi, _mrows[0], _fdest, _fargs)
        check("figma pull_file: a complete unit is skipped on re-run",
              _fres2["status"] == "skipped-complete")
        _fapi3 = _FigAPI(fail_full=True)
        _fdec = figma_vm_pull.pull_file(
            _fapi3, {"team_id": "9", "folder_path": "Root", "key": "KEY1",
                     "name": "big", "branches": []},
            _ft / "dest2", _fargs)
        check("figma decompose: an oversized file re-pulls as depth-1 + "
              "per-node JSON — complete, recorded, not failed",
              _fdec["status"] == "ok" and _fdec["decomposed"] is True
              and (_ft / "dest2" / "files" / "9" / "KEY1__big"
                   / "nodes" / "1_2.json").exists()
              and any(f == "file" and p == "/v1/files/KEY1"
                      and q.get("depth") == 1
                      for f, p, q in _fapi3.seen))
        _fapi4 = _FigAPI(null_once="1:2")
        _fnull = figma_vm_pull.pull_file(
            _fapi4, {"team_id": "9", "folder_path": "Root", "key": "KEY1",
                     "name": "nul", "branches": []},
            _ft / "dest3", _fargs)
        check("figma renders: a null node is retried once, then delivered "
              "or recorded — never silently dropped",
              _fnull["status"] == "ok" and _fapi4.render_calls >= 2
              and _fnull["render_nulls"] == 0
              and (_ft / "dest3" / "files" / "9" / "KEY1__nul"
                   / "renders" / "1_2.png").exists())
    finally:
        figma_vm_pull.cdn_download = _saved_cdn
    shutil.rmtree(_ft)

    print("\n— reconcile: figma plain prefix pin (single-prefix ingest) —")
    figco = root / "figmaco"
    (figco / "sizing-runs").mkdir(parents=True)
    common.write_json(figco / "config.json", {
        "slug": "figmaco", "subscription": "m1 corpus",
        "subscription_id": "x", "resource_group": "rg-figmaco",
        "storage_account": "stfigmaco", "container": "figmaco-raw",
        "vm": {"name": None, "resource_group": "rg-figmaco",
               "exists": False},
        "onboarded_at": "2026-08-01T00:00:00Z"})
    common.write_json(figco / "expected-data-sizes.json", {
        "slug": "figmaco", "manifest_total_bytes": 50_000_000_000,
        "services": {
            "figma": {"bytes": 50_000_000_000, "prefix": "figma-export"}},
        "source": "test", "confirmed_by_user": True,
        "created_at": "2026-08-01T00:00:00Z"})
    common.write_json(figco / "status.json", {
        "slug": "figmaco", "stage": "pushing",
        "last_run": {"timestamp": "2026-08-13T09:00:00Z",
                     "outcome": "sized", "reason": None},
        "last_change_detected_at": "2026-08-13T09:00:00Z"})
    common.write_json(figco / "sizing-runs" / "20260813T100000Z.json", {
        "slug": "figmaco", "timestamp": "2026-08-13T10:00:00Z",
        "method": "sized", "copied_from": None,
        "used_capacity_bytes": 40_000_000_000,
        "used_capacity_at": "2026-08-13T09:00:00Z", "duration_seconds": 60,
        "totals": {"blob_count": 500,
                   "compressed_bytes": 40_000_000_000,
                   "uncompressed_bytes": 42_000_000_000,
                   "zero_byte_blobs": 0},
        "sources": {"figma-export": {"blob_count": 500,
                                     "compressed_bytes": 40_000_000_000,
                                     "uncompressed_bytes":
                                         42_000_000_000}},
        "methods": {"stored": 500}, "errors": {"total": 0, "by_type": {}},
        "notes": []})
    figs = reconcile.company_summary(root, "figmaco")
    figrows = {r["service"]: r for r in figs["service_rows"]}
    check("figma pin: the plain prefix pin attributes the whole export "
          "(no source_split needed — single prefix, unlike zoho)",
          figrows["figma"]["actual_bytes"] == 42_000_000_000,
          str(figrows["figma"]))
    check("figma pin: the pinned prefix is not an unexpected source",
          "figma-export" not in figs["unexpected_sources"],
          str(figs["unexpected_sources"]))

    print("\n— teams_vm_pull pure helpers (classify, TokenBox shape, "
          "pacing)")
    import teams_vm_pull  # noqa: E402
    check("teams classify: 403 on the messages family is the "
          "protected-API fatal",
          teams_vm_pull.classify(403, "messages", False) == "fatal"
          and teams_vm_pull.classify(402, "messages", False) == "fatal")
    check("teams classify: 403/404 on a single team unit is a recorded "
          "skip, never fatal",
          teams_vm_pull.classify(403, "team", False) == "skip"
          and teams_vm_pull.classify(404, "channel", False) == "skip")
    check("teams classify: a 403 on an individual hostedContents fetch "
          "(family 'hosted') is a per-item skip, NEVER the protected-API "
          "fatal — a video preview Graph refuses must not kill the pass",
          teams_vm_pull.classify(403, "hosted", False) == "skip"
          and teams_vm_pull.classify(404, "hosted", False) == "skip"
          and 'api.get_raw(url, "hosted")' in
          inspect.getsource(teams_vm_pull.pull_channel))
    check("teams classify: _meta required units are fatal on any refusal",
          teams_vm_pull.classify(404, "directory", True) == "fatal")
    check("teams classify: 429 sleeps, 401 re-mints, 5xx retries",
          teams_vm_pull.classify(429, "messages", True) == "sleep"
          and teams_vm_pull.classify(401, "directory", True) == "remint"
          and teams_vm_pull.classify(503, "messages", False) == "retry")
    check("teams TokenBox: client-credentials mint against the tenant's "
          "v2.0 endpoint, .default scope",
          "oauth2/v2.0/token" in teams_vm_pull.TOKEN_PATH_FMT
          and ".default" in
          inspect.getsource(teams_vm_pull.TokenBox.mint))
    check("teams GraphAPI: exactly ONE place builds the Authorization "
          "header; bearer only",
          inspect.getsource(teams_vm_pull).count('"Authorization"') == 1)
    check("teams _meta: the documented org-wide team filter, and "
          "channel/member walks",
          "resourceProvisioningOptions/Any(x:x eq 'Team')"
          in inspect.getsource(teams_vm_pull.pull_meta)
          and "/channels" in inspect.getsource(teams_vm_pull.pull_meta)
          and "/members" in inspect.getsource(teams_vm_pull.pull_meta))
    check("teams _meta filenames match the spec layout",
          all(n in inspect.getsource(teams_vm_pull.pull_meta) for n in
              ("teams.jsonl", "channels.jsonl", "users.jsonl",
               "team-members.jsonl", "channel-members.jsonl",
               "name-map.json")))

    print("\n— teams_vm_pull GraphAPI / pull_meta in-process "
          "(stubbed transport) —")

    class _TResp:
        def __init__(self, payload, status=200):
            self._b = (payload if isinstance(payload, bytes)
                       else json.dumps(payload).encode())
            self.status = status
            self.headers = {"Content-Type": "application/json"}

        def read(self, n=None):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _thttp_err(code, body=b"{}", headers=None):
        return urllib.error.HTTPError(
            "https://x", code, "err", headers or {}, io.BytesIO(body))

    tsaved_urlopen = teams_vm_pull.urllib.request.urlopen
    tsaved_sleep = teams_vm_pull.time.sleep
    try:
        # -- get_raw: a stubbed 429 with Retry-After sleeps (recorded, not
        # actually slept) and then succeeds on the next attempt, with no
        # NameError — the exact bug the controller ruling fixed (Retry-After
        # must be captured INSIDE the except clause, since `e` unbinds once
        # the except block ends). --
        tsleeps = []
        teams_vm_pull.time.sleep = lambda s: tsleeps.append(s)
        _tseq = [_thttp_err(429, b"{}", {"Retry-After": "7"}),
                 _TResp({"value": []})]

        def _t429_urlopen(req, timeout=120):
            item = _tseq.pop(0)
            if isinstance(item, urllib.error.HTTPError):
                raise item
            return item
        teams_vm_pull.urllib.request.urlopen = _t429_urlopen

        class _FixedBox:
            def get(self):
                return "TOK"

            def invalidate(self):
                pass
        # rps_directory=0 disables PaceBucket's own proactive throttling
        # sleep, so tsleeps records ONLY the 429/Retry-After backstop this
        # check is about.
        tapi = teams_vm_pull.GraphAPI(_FixedBox(), rps_directory=0)
        status, body, _ct = tapi.get_raw(
            teams_vm_pull.GRAPH + "/groups", "directory", required=True)
        check("teams GraphAPI.get_raw: a stubbed 429 with Retry-After "
              "sleeps the captured value (no NameError) then succeeds",
              tsleeps == [7] and status == 200
              and json.loads(body.decode()) == {"value": []}
              and tapi.sleeps == 1)

        # -- I3: 429s never consume the retry budget — more consecutive
        # 429s than API_RETRIES must still end in success, not
        # SystemExit. --
        tsleeps.clear()
        _tseq[:] = ([_thttp_err(429, b"{}", {"Retry-After": "1"})]
                    * (teams_vm_pull.API_RETRIES + 3)
                    + [_TResp({"value": ["ok"]})])
        status, body, _ct = tapi.get_raw(
            teams_vm_pull.GRAPH + "/groups", "directory", required=True)
        check("teams GraphAPI.get_raw: sustained 429s beyond API_RETRIES "
              "still succeed (sleep never charges the retry budget)",
              status == 200 and len(tsleeps) == teams_vm_pull.API_RETRIES + 3)

        # -- C2: a deterministic 4xx that exhausts retries on a
        # required=False call RETURNS the terminal status (caller records
        # a skip / clears the cursor) instead of killing the whole pass
        # with SystemExit; required=True still raises. --
        def _t400_urlopen(req, timeout=120):
            raise _thttp_err(400, b'{"error":{"code":"BadRequest"}}')
        teams_vm_pull.urllib.request.urlopen = _t400_urlopen
        status, body, _ct = tapi.get_raw(
            teams_vm_pull.GRAPH + "/x", "messages", required=False)
        _c2_raised = False
        try:
            tapi.get_raw(teams_vm_pull.GRAPH + "/x", "directory",
                         required=True)
        except SystemExit:
            _c2_raised = True
        check("teams GraphAPI.get_raw: exhausted retries on an OPTIONAL "
              "call return the terminal 4xx (failure isolation); a "
              "REQUIRED call still raises",
              status == 400 and _c2_raised)

        # -- pull_meta: happy path + the two documented tolerances +
        # is_complete short-circuit on resume. --
        class _FakeGraphAPI:
            """Duck-types GraphAPI's .get/.get_raw for pull_meta. Every
            stubbed page is single-page (no @odata.nextLink), so get_raw
            is never exercised here — asserting that keeps this test
            honestly scoped to pull_meta's own branching, not paged()'s
            (which has its own coverage in the get_raw check above and in
            classify()'s pure checks)."""

            def __init__(self, routes):
                self.routes = routes
                self.calls = []

            def get(self, path, family, params=None, required=False):
                self.calls.append(path)
                if path not in self.routes:
                    raise AssertionError(f"unstubbed teams path: {path}")
                return self.routes[path]

            def get_raw(self, url, family, required=False):
                raise AssertionError(
                    "get_raw unexpected — a stubbed page carried a "
                    "nextLink it shouldn't have")

        troutes = {
            "/groups": (200, {"value": [
                {"id": "T1", "displayName": "Team One"},
                {"id": "T2", "displayName": "Team Two"}]}),
            "/teams/T1": (200, {"id": "T1", "someTeamSetting": True}),
            "/teams/T2": (404, None),
            "/teams/T1/channels": (200, {"value": [
                {"id": "C1", "displayName": "General",
                 "membershipType": "standard"},
                {"id": "C2", "displayName": "Private1",
                 "membershipType": "private"}]}),
            "/teams/T2/channels": (200, {"value": []}),
            "/users": (200, {"value": [
                {"id": "U1", "userPrincipalName": "u1@x.com"}]}),
            "/groups/T1/members": (200, {"value": [
                {"id": "U1", "displayName": "User One"}]}),
            "/groups/T2/members": (403, None),
            "/teams/T1/channels/C2/members": (200, {"value": [
                {"id": "U2", "displayName": "User Two"}]}),
        }
        with tempfile.TemporaryDirectory() as tdest_s:
            tdest = Path(tdest_s)
            fake_api = _FakeGraphAPI(troutes)
            roster = teams_vm_pull.pull_meta(fake_api, tdest)
            mdir = tdest / "_meta"
            tteams = [json.loads(ln) for ln in
                     (mdir / "teams.jsonl").read_text().splitlines()]
            tchannels = [json.loads(ln) for ln in
                        (mdir / "channels.jsonl").read_text().splitlines()]
            tmembers = [json.loads(ln) for ln in
                       (mdir / "team-members.jsonl").read_text()
                       .splitlines()]
            tchmembers = [json.loads(ln) for ln in
                         (mdir / "channel-members.jsonl").read_text()
                         .splitlines()]
            check("teams pull_meta: happy-path team gets settings merged "
                  "in, six files written, unit marked complete",
                  {t["id"] for t in tteams} == {"T1", "T2"}
                  and any(t.get("someTeamSetting") is True
                          for t in tteams if t["id"] == "T1")
                  and (mdir / "users.jsonl").exists()
                  and (mdir / "name-map.json").exists()
                  and teams_vm_pull.is_complete(mdir),
                  str(tteams))
            check("teams pull_meta: a team whose /teams/{id} fetch 404s "
                  "gets team_settings_status recorded, not a failure",
                  any(t.get("id") == "T2" and
                      t.get("team_settings_status") == 404
                      for t in tteams),
                  str(tteams))
            check("teams pull_meta: a team whose members walk is refused "
                  "(403) gets a members_status line, not a failure",
                  any(m.get("team_id") == "T2" and
                      m.get("members_status") == 403 for m in tmembers)
                  and any(m.get("team_id") == "T1" and m.get("id") == "U1"
                          for m in tmembers),
                  str(tmembers))
            check("teams pull_meta: only the non-standard channel gets a "
                  "channel-members walk",
                  len(tchmembers) == 1
                  and tchmembers[0]["channel_id"] == "C2"
                  and tchmembers[0]["id"] == "U2"
                  and not any(c.get("channel_id") == "C1"
                              for c in tchmembers),
                  str(tchmembers))
            check("teams pull_meta: return value matches the roster "
                  "written to disk",
                  roster["counts"] == {"teams": 2, "channels": 2}
                  and len(roster["channels"]["T1"]) == 2
                  and roster["channels"]["T2"] == [],
                  str(roster))

            # is_complete short-circuit: a second call must not touch the
            # api at all and must reproduce the same roster.
            calls_before = len(fake_api.calls)
            roster2 = teams_vm_pull.pull_meta(fake_api, tdest)
            check("teams pull_meta: is_complete short-circuits resume "
                  "(no further API calls) and reproduces the roster",
                  len(fake_api.calls) == calls_before
                  and roster2 == roster,
                  str(roster2))
    finally:
        teams_vm_pull.urllib.request.urlopen = tsaved_urlopen
        teams_vm_pull.time.sleep = tsaved_sleep

    print("\n— teams_vm_pull: channel units, hosted content, upload, main "
          "(Task 3) —")
    tsrc = (SCRIPTS / "teams_vm_pull.py").read_text()
    check("teams puller: markers, cursor, honest write invariant "
          "(client-side no-overwrite, no If-None-Match, no "
          "copy-source-auth)",
          ".cdp-complete" in tsrc and ".cdp-cursor.json" in tsrc
          and "--overwrite=false" in tsrc
          and '"If-None-Match"' not in tsrc
          and "x-ms-copy-source-authorization" not in tsrc)
    check("teams puller: manifest replaces, corpus never does (the "
          "pilot-poisons-verify fix ships from day one)",
          "--overwrite=true" in tsrc and "upload_run_metadata" in tsrc)
    check("teams puller: messages are pulled $top=50 with replies "
          "expanded, and truncated reply lists are paginated to "
          "completion before the line is written",
          "$expand" in tsrc and "replies@odata.nextLink" in tsrc)
    check("teams puller: hosted content is staged (Bearer fetch), "
          "attachment bytes are NOT fetched",
          "hostedContents" in tsrc and "attachment" in tsrc.lower()
          and "contentUrl" not in tsrc)
    _hr = teams_vm_pull.hosted_refs({
        "id": "1", "body": {"content":
            '<img src="https://graph.microsoft.com/v1.0/teams/t/'
            'channels/c/messages/1/hostedContents/AAA/$value">'},
        "replies": [{"id": "2", "body": {"content":
            '<img src="https://graph.microsoft.com/v1.0/teams/t/'
            'channels/c/messages/1/replies/2/hostedContents/BBB/'
            '$value">'}}],
    })
    check("teams puller: hosted_refs finds refs in root AND replies — "
          "including the REPLY-SHAPED URL (…/messages/{root}/replies/"
          "{reply}/hostedContents/…), keyed by innermost id, fetch URL "
          "kept verbatim",
          [(m, h) for _u, m, h in _hr] == [("1", "AAA"), ("2", "BBB")]
          and _hr[0][0].endswith("/messages/1/hostedContents/AAA/$value")
          and "/replies/2/hostedContents/BBB" in _hr[1][0])
    check("teams puller: hosted_refs host-checks candidate URLs — a "
          "non-Graph host is never yielded (no Bearer fetch off-host)",
          teams_vm_pull.hosted_refs({
              "id": "1", "body": {"content":
                  '<img src="https://graph.microsoft.com.evil.example/'
                  'v1.0/teams/t/channels/c/messages/1/hostedContents/'
                  'AAA/$value">'}}) == [])
    check("teams puller: secrets via env only, never argv",
          "TEAMS_CLIENT_SECRET" in tsrc and "--secret" not in tsrc)
    _tmods = set()
    for _n in ast.walk(ast.parse(tsrc)):
        if isinstance(_n, ast.Import):
            for _a in _n.names:
                _tmods.add((_a.asname or _a.name).split(".")[0])
        elif isinstance(_n, ast.ImportFrom) and _n.level == 0:
            _tmods.add(_n.module.split(".")[0])
    check("teams puller: stdlib-only imports", all(
        m in sys.stdlib_module_names for m in _tmods), str(_tmods))
    check("teams puller: run manifest lives at _meta/manifest.json, not "
          "the dest root (controller ruling — a later verify greps this "
          "exact path)",
          '"_meta/manifest.json"' in tsrc and "manifest.json" in
          inspect.getsource(teams_vm_pull.upload_run_metadata))
    check("teams main(): the _meta unit's bytes/counts are recorded into "
          "results BEFORE the channel loop, so total_staged_bytes/"
          "unit_count cover the whole uploaded tree, not just channels",
          '"_meta", "meta", "ok"' in inspect.getsource(teams_vm_pull.main)
          and inspect.getsource(teams_vm_pull.main).index(
              '"_meta", "meta", "ok"')
          < inspect.getsource(teams_vm_pull.main).index("for i, (gid, cid)"))

    print("\n— teams_vm_pull pull_channel in-process (stubbed transport) —")

    class _FakeChannelAPI:
        """Duck-types GraphAPI's .get/.get_raw for pull_channel. get_routes
        keyed by path (as api.get is always called with the same literal
        messages-list path); raw_routes keyed by the exact URL passed to
        get_raw (nextLinks and hostedContents $value URLs alike)."""

        def __init__(self, get_routes=None, raw_routes=None):
            self.get_routes = get_routes or {}
            self.raw_routes = raw_routes or {}
            self.get_calls = []
            self.raw_calls = []

        def get(self, path, family, params=None, required=False):
            self.get_calls.append(path)
            return self.get_routes[path]

        def get_raw(self, url, family, required=False):
            self.raw_calls.append(url)
            return self.raw_routes[url]

    with tempfile.TemporaryDirectory() as tdest_s:
        tdest = Path(tdest_s)

        # -- happy path: two message pages, a reply page that must be
        # paginated to completion BEFORE the line is written, one hosted
        # ref fetched fresh and one skipped because its file already
        # exists (deterministic-name resume). --
        hosted_url = (teams_vm_pull.GRAPH +
                      "/teams/G1/channels/C1/messages/M2/hostedContents/"
                      "H1/$value")
        msg1 = {"id": "M1", "body": {"content": "plain"}, "replies": []}
        msg2 = {"id": "M2",
                "body": {"content": f'<img src="{hosted_url}">'},
                "replies": [], "replies@odata.nextLink": "URL_REPLIES_M2"}
        msg3_hosted_url = (teams_vm_pull.GRAPH +
                           "/teams/G1/channels/C1/messages/M3/"
                           "hostedContents/H2/$value")
        msg3 = {"id": "M3",
                "body": {"content": f'<img src="{msg3_hosted_url}">'},
                "replies": []}
        chan_unit = teams_vm_pull.unit_dir(tdest, "teams/G1/C1")
        (chan_unit / "hosted").mkdir(parents=True, exist_ok=True)
        (chan_unit / "hosted" / "M3_H2.png").write_bytes(b"OLD")

        fake1 = _FakeChannelAPI(
            get_routes={
                "/teams/G1/channels/C1/messages": (200, {
                    "value": [msg1, msg2], "@odata.nextLink": "URL_PAGE2"}),
            },
            raw_routes={
                "URL_PAGE2": (200, json.dumps(
                    {"value": [msg3], "@odata.nextLink": None}).encode(),
                              ""),
                "URL_REPLIES_M2": (200, json.dumps(
                    {"value": [{"id": "M2-R1", "body": {"content": ""}}],
                     "@odata.nextLink": None}).encode(), ""),
                hosted_url: (200, b"PNGDATA", "image/png"),
            })
        res1 = teams_vm_pull.pull_channel(fake1, "G1", "C1", tdest, None)
        lines1 = (chan_unit / "messages.jsonl").read_text().splitlines()
        check("pull_channel happy path: 3 root messages across 2 pages, "
              "1 reply pulled via pagination-to-completion, 1 hosted ref "
              "fetched fresh, the pre-existing one skipped (never "
              "re-fetched) but still counted (hosted is the cumulative "
              "on-disk total, not a this-pass-only delta), unit marked "
              "complete",
              res1["status"] == "ok" and res1["messages"] == 3
              and res1["replies"] == 1 and res1["hosted"] == 2
              and res1["hosted_errors"] == 0
              and len(lines1) == 3
              and msg3_hosted_url not in fake1.raw_calls
              and (chan_unit / "hosted" / "M2_H1.png").read_bytes()
                  == b"PNGDATA"
              and (chan_unit / "hosted" / "M3_H2.png").read_bytes()
                  == b"OLD"
              and teams_vm_pull.is_complete(chan_unit),
              str(res1))
        check("pull_channel happy path: the M2-R1 reply landed on M2's "
              "line, not a separate line (a JSONL line is ALWAYS a "
              "complete thread)",
              any(json.loads(ln)["id"] == "M2"
                  and [r["id"] for r in json.loads(ln)["replies"]]
                  == ["M2-R1"] for ln in lines1),
              lines1)

        # -- cursor resume: a prior partial pull left one line + a cursor
        # naming the next page; resume must NOT call the fresh-start
        # endpoint at all, must truncate/continue correctly, and must
        # preserve the already-written line untouched. --
        chan2 = teams_vm_pull.unit_dir(tdest, "teams/G2/C2")
        prev_line = json.dumps({"id": "M0", "body": {"content": ""},
                                "replies": []}, separators=(",", ":")) + "\n"
        (chan2 / "messages.jsonl").write_text(prev_line)
        teams_vm_pull.atomic_write_json(chan2 / ".cdp-cursor.json", {
            "next_link": "URL_RESUME", "lines": 1,
            "bytes": (chan2 / "messages.jsonl").stat().st_size})
        fake2 = _FakeChannelAPI(raw_routes={
            "URL_RESUME": (200, json.dumps(
                {"value": [{"id": "M1", "body": {"content": ""},
                           "replies": []}],
                 "@odata.nextLink": None}).encode(), ""),
        })
        res2 = teams_vm_pull.pull_channel(fake2, "G2", "C2", tdest, None)
        lines2 = (chan2 / "messages.jsonl").read_text().splitlines()
        check("pull_channel cursor resume: continues from next_link "
              "without re-hitting the fresh-start endpoint, appends "
              "exactly the new page, keeps the prior line intact",
              fake2.get_calls == [] and res2["status"] == "ok"
              and res2["messages"] == 2 and len(lines2) == 2
              and json.loads(lines2[0])["id"] == "M0"
              and json.loads(lines2[1])["id"] == "M1",
              str(res2))

        # -- resume with prior replies + hosted content: a first pass left
        # one root message with 2 already-resolved replies and one already-
        # fetched hosted file; the second (resuming) pass adds one more
        # root message with 1 reply and 1 fresh hosted ref. The final
        # result's replies/hosted must be the CUMULATIVE on-disk totals
        # (2+1=3 replies, 1+1=2 hosted), not just this pass's own delta
        # (which would wrongly read 1 reply / 1 hosted). --
        chan5 = teams_vm_pull.unit_dir(tdest, "teams/G5/C5")
        hosted_url_p1 = (teams_vm_pull.GRAPH +
                         "/teams/G5/channels/C5/messages/P1/hostedContents/"
                         "H9/$value")
        hosted_url_p2 = (teams_vm_pull.GRAPH +
                         "/teams/G5/channels/C5/messages/P2/hostedContents/"
                         "H10/$value")
        msg_p1 = {"id": "P1", "body": {"content": f'<img src="{hosted_url_p1}">'},
                  "replies": [{"id": "P1-R1", "body": {"content": ""}},
                             {"id": "P1-R2", "body": {"content": ""}}]}
        prev_line5 = json.dumps(msg_p1, separators=(",", ":")) + "\n"
        (chan5 / "messages.jsonl").write_text(prev_line5)
        (chan5 / "hosted").mkdir(parents=True, exist_ok=True)
        (chan5 / "hosted" / "P1_H9.png").write_bytes(b"OLDHOSTED")
        teams_vm_pull.atomic_write_json(chan5 / ".cdp-cursor.json", {
            "next_link": "URL_RESUME2", "lines": 1,
            "bytes": (chan5 / "messages.jsonl").stat().st_size})
        msg_p2 = {"id": "P2", "body": {"content": f'<img src="{hosted_url_p2}">'},
                  "replies": [{"id": "P2-R1", "body": {"content": ""}}]}
        fake5 = _FakeChannelAPI(raw_routes={
            "URL_RESUME2": (200, json.dumps(
                {"value": [msg_p2], "@odata.nextLink": None}).encode(), ""),
            hosted_url_p2: (200, b"NEWHOSTED", "image/png"),
        })
        res5 = teams_vm_pull.pull_channel(fake5, "G5", "C5", tdest, None)
        check("pull_channel resume: replies/hosted in the final result "
              "are the CUMULATIVE on-disk total across both passes "
              "(2 prior + 1 new = 3 replies; 1 prior + 1 new = 2 hosted), "
              "not this pass's own delta",
              fake5.get_calls == [] and res5["status"] == "ok"
              and res5["messages"] == 2 and res5["replies"] == 3
              and res5["hosted"] == 2
              and (chan5 / "hosted" / "P1_H9.png").read_bytes()
                  == b"OLDHOSTED"
              and any((chan5 / "hosted").glob("P2_H10*"))
              and next((chan5 / "hosted").glob("P2_H10*")).read_bytes()
                  == b"NEWHOSTED",
              str(res5))

        # -- a cursor whose next_link now 4xxs is NOT trusted forward: the
        # unit is cleared and re-walked from scratch (channels are small). --
        chan3 = teams_vm_pull.unit_dir(tdest, "teams/G3/C3")
        (chan3 / "messages.jsonl").write_text(prev_line)
        teams_vm_pull.atomic_write_json(chan3 / ".cdp-cursor.json", {
            "next_link": "URL_STALE", "lines": 1,
            "bytes": (chan3 / "messages.jsonl").stat().st_size})
        fake3 = _FakeChannelAPI(
            get_routes={
                "/teams/G3/channels/C3/messages": (200, {
                    "value": [{"id": "M9", "body": {"content": ""},
                              "replies": []}],
                    "@odata.nextLink": None}),
            },
            raw_routes={"URL_STALE": (404, None, "")})
        res3 = teams_vm_pull.pull_channel(fake3, "G3", "C3", tdest, None)
        lines3 = (chan3 / "messages.jsonl").read_text().splitlines()
        check("pull_channel: a cursor next_link that now 4xxs clears the "
              "unit and re-walks fresh rather than trusting it forward",
              res3["status"] == "ok" and res3["messages"] == 1
              and len(lines3) == 1 and json.loads(lines3[0])["id"] == "M9"
              and fake3.get_calls == ["/teams/G3/channels/C3/messages"],
              str(res3))

        # -- skip status: a terminal refusal on the very first page (no
        # cursor at all) is a recorded skip, never a failure. --
        fake4 = _FakeChannelAPI(get_routes={
            "/teams/G4/channels/C4/messages": (404, None),
        })
        res4 = teams_vm_pull.pull_channel(fake4, "G4", "C4", tdest, None)
        chan4 = teams_vm_pull.unit_dir(tdest, "teams/G4/C4")
        check("pull_channel: a 404 on the first page (fresh start) is "
              "mark_skipped, not mark_complete, and is returned as a skip",
              res4["status"] == "skipped" and res4["reason"] == "status-404"
              and (chan4 / ".cdp-skipped.json").exists()
              and not teams_vm_pull.is_complete(chan4),
              str(res4))

    print("\n— teams_transfer --dry-run (engine lifecycle, VM-side REST "
          "puller)")
    import teams_transfer  # noqa: E402
    check("teams Spec: stdin secrets (no OAuth/rclone), engine-default "
          "disk with 64 GB headroom",
          teams_transfer.SPEC.vm_prefix == "xfer-teams-"
          and teams_transfer.SPEC.authorize_target == ""
          and teams_transfer.SPEC.remote_type == ""
          and teams_transfer.SPEC.default_dest_prefix == "teams-export"
          and teams_transfer.SPEC.default_os_disk_gb == 64)
    proc = run_script("teams_transfer.py", "plan", "democo",
                      "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
                      "--root", root, "--dry-run")
    tplan = json.loads(proc.stdout)
    check("teams plan: dest + tenant as the loc",
          tplan["vm_name"] == "xfer-teams-democo"
          and tplan["dest"] == "democo-raw/teams-export"
          and "505f352c" in tplan["source"])
    proc = run_script("teams_transfer.py", "plan", "democo",
                      "--tenant-id", "not-a-guid", "--root", root,
                      "--dry-run", expect_rc=1)
    check("teams tenant-id validation: refuses a non-GUID before any az "
          "call", "GUID" in proc.stdout and "az " not in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "write-creds",
         "democo", "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
         "--root", str(root), "--dry-run"],
        input="505f352c-ec82-4ff9-9191-556112b420f9\n"
              "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1\n"
              "TEAMSSECRETSENTINEL\n",
        capture_output=True, text=True)
    check("teams write-creds: secret sentinel never echoed; 3-line stdin",
          proc.returncode == 0
          and "TEAMSSECRETSENTINEL" not in proc.stdout
          and "redacted" in proc.stdout, proc.stdout[-300:])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "write-creds",
         "democo", "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
         "--root", str(root), "--dry-run"],
        input="only-two\nlines\n", capture_output=True, text=True)
    check("teams write-creds: refuses malformed stdin (must be 3 lines)",
          proc.returncode == 1 and "3 lines" in proc.stdout)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "write-creds",
         "democo", "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
         "--root", str(root), "--dry-run"],
        input="deadbeef-0000-0000-0000-000000000000\n"
              "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1\nsecret\n",
        capture_output=True, text=True)
    check("teams write-creds: stdin tenant must match the VM tag / flag "
          "(zoho's wrong-DC guard)",
          proc.returncode == 1 and "tenant" in proc.stdout.lower()
          and "mismatch" in proc.stdout.lower())
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "probe",
         "democo", "--tenant-id", "505f352c-ec82-4ff9-9191-556112b420f9",
         "--root", str(root), "--dry-run"],
        input="505f352c-ec82-4ff9-9191-556112b420f9\n"
              "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1\n"
              "TEAMSSECRETSENTINEL\n",
        capture_output=True, text=True)
    check("teams probe: laptop-side Graph JSON only — no Azure, no VM, "
          "secret redacted",
          proc.returncode == 0
          and "graph.microsoft.com/v1.0/groups" in proc.stdout
          and "TEAMSSECRETSENTINEL" not in proc.stdout
          and "generate-sas" not in proc.stdout
          and "az vm" not in proc.stdout, proc.stdout[-300:])

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "teams_transfer.py"), "write-creds",
         "democo", "--root", str(root), "--dry-run"],
        input="505f352c-ec82-4ff9-9191-556112b420f9\n"
              "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1\n"
              "TEAMSSECRETSENTINEL4\n",
        capture_output=True, text=True)
    check("teams write-creds: --tenant-id is NOT required by argparse — "
          "the normal workflow (create-vm --tenant-id once, then "
          "write-creds bare) must reach the VM-tag fallback branch",
          proc.returncode == 0 and "requires --tenant-id" not in proc.stderr
          and "redacted" in proc.stdout,
          proc.stdout[-300:] + proc.stderr[-300:])

    print("\n— teams_transfer: write-creds tenant resolution from the VM "
          "tag (--tenant-id omitted, in-process since a dry-run VM's tags "
          "are always empty) —")
    from types import SimpleNamespace as _TeamsNS
    _tw_require_vm = teams_transfer.eng.require_vm
    _tw_read_secrets = teams_transfer.read_secrets
    try:
        teams_transfer.eng.require_vm = lambda spec, cfg, slug, dry_run: {
            "name": "xfer-teams-democo", "power_state": "VM running",
            "public_ip": "203.0.113.10",
            "tags": {"teams_tenant_id":
                    "505f352c-ec82-4ff9-9191-556112b420f9"},
            "location": "eastus"}
        teams_transfer.read_secrets = lambda dry_run: (
            "505f352c-ec82-4ff9-9191-556112b420f9",
            "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1", "TEAMSSECRETSENTINEL5")
        wargs = _TeamsNS(slug="democo", tenant_id=None, dry_run=True)
        wres = teams_transfer.cmd_write_creds(root, wargs)
        check("teams write-creds: no --tenant-id + matching VM tag "
              "succeeds (the tag-fallback branch is reachable and works)",
              wres.get("ok") is True and wres.get("secret") == "redacted",
              str(wres))

        teams_transfer.read_secrets = lambda dry_run: (
            "deadbeef-0000-0000-0000-000000000000",
            "7dd22ed9-b3ce-4016-88fb-a043f99fd3f1", "TEAMSSECRETSENTINEL6")
        tag_mismatch_msg = ""
        try:
            teams_transfer.cmd_write_creds(root, wargs)
        except common.HarnessError as e:
            tag_mismatch_msg = str(e)
        check("teams write-creds: no --tenant-id + a stdin tenant that "
              "disagrees with the VM tag still raises the mismatch guard "
              "(the tag path is a real check, not a silent no-op)",
              "tenant" in tag_mismatch_msg.lower()
              and "mismatch" in tag_mismatch_msg.lower(), tag_mismatch_msg)
    finally:
        teams_transfer.eng.require_vm = _tw_require_vm
        teams_transfer.read_secrets = _tw_read_secrets

    print("\n— teams_transfer: transfer/status/verify/teardown "
          "(full CLI over the engine) —")
    proc = run_script("teams_transfer.py", "allow-network", "democo",
                      "--root", root, "--dry-run")
    check("teams allow-network: vnet path (VM family), never IP rules",
          "network-rule add" in proc.stdout and "--subnet" in proc.stdout
          and "--ip-address" not in proc.stdout)
    proc = run_script("teams_transfer.py", "write-dest", "democo",
                      "--root", root, "--dry-run")
    check("teams write-dest: racwl SAS -> dest-teams.env, redacted",
          "--permissions racwl" in proc.stdout
          and "teams-export" in proc.stdout
          and "dest-teams.env" in proc.stdout
          and "redacted" in proc.stdout)
    proc = run_script("teams_transfer.py", "transfer", "democo",
                      "--tenant-id",
                      "505f352c-ec82-4ff9-9191-556112b420f9",
                      "--root", root, "--dry-run")
    check("teams transfer: pushes the puller fresh into tmux window "
          "'teams', sourcing both env files",
          "teams_vm_pull.py" in proc.stdout
          and "tmux new-session -d -s transfer -n teams" in proc.stdout
          and "teams.env" in proc.stdout
          and "dest-teams.env" in proc.stdout)
    proc = run_script("teams_transfer.py", "verify", "democo", "--root",
                      root, "--dry-run")
    check("teams verify: laptop path — ip rule + rl SAS + manifest, "
          "never the write SAS",
          "--permissions rl" in proc.stdout
          and "network-rule add" in proc.stdout
          and "teams-export/_meta/manifest.json" in proc.stdout
          and "racwl" not in proc.stdout)
    proc = run_script("teams_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", expect_rc=2)
    check("teams teardown gated", '"not-confirmed"' in proc.stdout)
    proc = run_script("teams_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", "--confirmed")
    check("teams confirmed teardown: engine set + secret-rotation "
          "reminder",
          "network-rule remove" in proc.stdout
          and "rotate" in proc.stdout.lower()
          and "client secret" in proc.stdout.lower())

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

    print("\n— qwilr_csv_pull (support-CSV fallback, no API) —")
    import qwilr_csv_pull as qcp  # noqa: E402

    csv_cols = ("Page name,Page ID,Status,Password protected,"
                "Public/shareable URL,Collaborator URL,PDF download URL\n")
    (root / "democo" / "qwilr-pages.csv").write_text(
        csv_cols
        + "Landed,aaa111,Live,false,https://pages.qwilr.com/Landed-TOKAAA,"
          "https://pages.qwilr.com/collaborate/TOKAAA/secA,"
          "https://pages.qwilr.com/pdf/TOKAAA\n"
        + "Fresh,bbb222,Accepted,false,https://pages.qwilr.com/Fresh-TOKBBB,"
          "https://pages.qwilr.com/collaborate/TOKBBB/secB,"
          "https://pages.qwilr.com/pdf/TOKBBB\n"
        + "Secret,ccc333,Draft,false,https://pages.qwilr.com/Secret-TOKCCC,"
          "https://pages.qwilr.com/collaborate/TOKCCC/secC,"
          "https://pages.qwilr.com/pdf/TOKCCC\n")

    # pure helpers
    check("csv: pdf_token from the PDF URL; is_public gates on status",
          qcp.pdf_token({"PDF download URL":
                         "https://pages.qwilr.com/pdf/AbC123 "}) == "AbC123"
          and qcp.is_public({"Status": "Live", "Password protected": "false"})
          and not qcp.is_public({"Status": "Draft",
                                 "Password protected": "false"})
          and not qcp.is_public({"Status": "Declined",
                                 "Password protected": "false"})
          and not qcp.is_public({"Status": "Live",
                                 "Password protected": "true"}))
    loader = ('ng-init="initialData = {&quot;downloadPdfPath&quot;:&quot;'
              'https://download.qwilr.com/u-u-i-d.pdf&quot;,&quot;pdfPath')
    m = qcp.PDF_LOADER_RE.search(loader)
    check("csv: loader-page regex finds the fresh render URL",
          m and m.group(1) == "https://download.qwilr.com/u-u-i-d.pdf")
    got_assets = qcp.extract_asset_urls(
        b'<img src="https://qwilr.imgix.net/raster/x?w=1"> '
        b'{&quot;v&quot;:&quot;https://example.com/clip.mp4&quot;} '
        b'<a href="https://app.qwilr.com/some-page">')
    check("csv: asset extraction by CDN host and media ext, pages excluded",
          got_assets == ["https://example.com/clip.mp4",
                         "https://qwilr.imgix.net/raster/x?w=1"])

    # plan + pull --dry-run via the CLI
    proc = run_script("qwilr_csv_pull.py", "plan", "democo", "--root", root)
    cplan = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("csv plan: counts public vs restricted, dest from config, no token",
          cplan["pages"] == 3 and cplan["public_pages (html+pdf)"] == 2
          and cplan["restricted_pages (collaborator html only)"] == 1
          and cplan["dest"] == "democo-raw/qwilr-export"
          and "no API token" in cplan["mode"])
    proc = run_script("qwilr_csv_pull.py", "pull", "democo", "--root", root,
                      "--dry-run")
    out = proc.stdout
    check("csv pull dry-run: rc 0, racwl SAS, laptop IP rule, create-only",
          proc.returncode == 0 and "racwl" in out
          and "network-rule add" in out and "allow-network" not in out
          and "If-None-Match: *" in out, out[-300:])
    check("csv pull dry-run: ledger CSV + html + pdf render planned",
          "_meta/qwilr-pages.csv" in out and "text/csv" in out
          and "page.html" in out and "trigger render" in out
          and "application/pdf" in out)

    # in-process pull: resume skips, draft via collaborator, pdf pipeline
    saved_csv = (qcp.http_get, qcp.azure_put_bytes, qcp.azure_list_sizes,
                 qcp.trigger_render, qcp.poll_pdf, qcp._sleep, qcp._now,
                 qwilr_transfer.azure_put_json, qwilr_transfer.mint_write_sas,
                 common.run_az, phases.ip_rule_ensure,
                 phases.ip_rule_remove_if_ours, phases.mint_sas)
    try:
        fetched, put_names, triggered = [], [], []

        def fake_http_get(url, retries=4, timeout=120, ok_statuses=()):
            fetched.append(url)
            return 200, (b'<html><img src='
                         b'"https://qwilr.imgix.net/pic"></html>')

        def fake_put_bytes(cfg, sas, name, body, ctype, dry_run):
            put_names.append((name, ctype))
            return len(body)

        qcp.http_get = fake_http_get
        qcp.azure_put_bytes = fake_put_bytes
        qcp.azure_list_sizes = lambda cfg, sas, prefix, dry: {
            "qwilr-export/_meta/qwilr-pages.csv": 100,
            "qwilr-export/pages/aaa111/metadata.json": 5,
            "qwilr-export/pages/aaa111/page.html": 10,
            "qwilr-export/pages/aaa111/page.pdf": 20}
        qcp.trigger_render = lambda tok: (triggered.append(tok)
                                          or f"https://dl/{tok}.pdf")
        qcp.poll_pdf = lambda url: b"%PDF-1.7 fake"
        qcp._sleep = lambda s: None
        qwilr_transfer.azure_put_json = (
            lambda cfg, sas, name, obj, dry: put_names.append(
                (name, "application/json")) or 10)
        qwilr_transfer.mint_write_sas = (
            lambda cfg, days, dry: ("sig=fake", "2026-08-28T00:00:00Z"))
        common.run_az = lambda *a, **k: types.SimpleNamespace(stdout="")
        phases.ip_rule_ensure = lambda cfg, dry_run=False: (True, "1.2.3.4")
        removed_csv = []
        phases.ip_rule_remove_if_ours = (
            lambda cfg, ip, we, dry_run=False: removed_csv.append((ip, we)))

        cargs = types.SimpleNamespace(
            slug="democo", csv=None, dest_prefix="qwilr-export", sas_days=1,
            limit=None, pdf_concurrency=2, pdf_timeout=60,
            pdf_poll_seconds=0, html_only=False, dry_run=False)
        cres = qcp.cmd_pull(root, cargs)
        names = [n for n, _ in put_names]
        check("csv pull: landed page + ledger skipped, never re-fetched",
              cres["html_skipped_existing"] == 1
              and cres["pdf_skipped_existing"] == 1
              and "qwilr-export/_meta/qwilr-pages.csv" not in names
              and not any("TOKAAA" in u for u in fetched))
        check("csv pull: draft fetched via its collaborator URL, no render",
              any("/collaborate/TOKCCC/" in u for u in fetched)
              and "TOKCCC" not in triggered)
        check("csv pull: fresh public page gets html + pdf, draft html only",
              ("qwilr-export/pages/bbb222/page.pdf", "application/pdf")
              in put_names
              and ("qwilr-export/pages/bbb222/page.html", "text/html")
              in put_names
              and ("qwilr-export/pages/ccc333/page.html", "text/html")
              in put_names
              and triggered == ["TOKBBB"]
              and not any(n.endswith("ccc333/page.pdf") for n in names))
        check("csv pull: index + assets manifest written, clean run, IP rule "
              "removed",
              cres["ok"] is True and cres["errors"]["count"] == 0
              and any(n.startswith("qwilr-export/_meta/pull-index-")
                      for n in names)
              and any(n.startswith("qwilr-export/_meta/assets-manifest-")
                      for n in names)
              and removed_csv == [("1.2.3.4", True)])

        # 429 on a render trigger: requeued with backoff, never burned
        clock = {"t": 0.0}
        qcp._now = lambda: clock["t"]

        def fake_sleep(s):
            clock["t"] += max(s, 1)

        qcp._sleep = fake_sleep
        trig = {"n": 0}

        def flaky_trigger(tok):
            trig["n"] += 1
            if trig["n"] <= 2:
                raise qcp.RateLimited(tok)
            return f"https://dl/{tok}.pdf"

        qcp.trigger_render = flaky_trigger
        up, _, perrs = qcp.run_pdf_pipeline(
            {"storage_account": "st", "container": "c"}, "sas",
            [("pX", "TOKX", "qwilr-export/pages/pX/page.pdf")],
            2, 600, 1, False)
        check("csv pdf pipeline: 429 requeues with backoff, then lands",
              up == 1 and perrs == {} and trig["n"] == 3)

        def always_429(tok):
            raise qcp.RateLimited(tok)

        qcp.trigger_render = always_429
        up, _, perrs = qcp.run_pdf_pipeline(
            {"storage_account": "st", "container": "c"}, "sas",
            [("pY", "TOKY", "qwilr-export/pages/pY/page.pdf")],
            2, 600, 1, False)
        check("csv pdf pipeline: endless 429 gives up the pass, run ends",
              up == 0 and "gave up" in perrs.get("pY", ""))

        # verify math: missing pdf on a public page; drafts never owe one
        qcp.azure_list_sizes = lambda cfg, sas, prefix, dry: {
            "qwilr-export/pages/aaa111/page.html": 10,
            "qwilr-export/pages/aaa111/page.pdf": 20,
            "qwilr-export/pages/bbb222/page.html": 10,
            "qwilr-export/pages/ccc333/page.html": 0}
        phases.mint_sas = lambda cfg, dry_run=False: "sig=fake"
        cver = qcp.cmd_verify(root, cargs)
        check("csv verify: missing pdf flagged, drafts exempt, zero-byte "
              "caught",
              cver["ok"] is False and cver["missing_pdf"] == ["bbb222"]
              and cver["missing_html"] == []
              and cver["zero_byte_blobs"] == [
                  "qwilr-export/pages/ccc333/page.html"]
              and cver["ledger_csv_landed"] is False)
    finally:
        (qcp.http_get, qcp.azure_put_bytes, qcp.azure_list_sizes,
         qcp.trigger_render, qcp.poll_pdf, qcp._sleep, qcp._now,
         qwilr_transfer.azure_put_json, qwilr_transfer.mint_write_sas,
         common.run_az, phases.ip_rule_ensure,
         phases.ip_rule_remove_if_ours, phases.mint_sas) = saved_csv
        (root / "democo" / "qwilr-pages.csv").unlink()


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

    print("\n— offboard_company (archive out of the fleet, restore back) —")
    goneco = root / "goneco"
    (goneco / "sizing-runs").mkdir(parents=True)
    common.write_json(goneco / "config.json", {
        "slug": "goneco", "subscription": "m1 corpus", "subscription_id": "x",
        "resource_group": "rg-goneco", "storage_account": "stgoneco",
        "container": "goneco-raw",
        "vm": {"name": None, "resource_group": "rg-goneco", "exists": False},
        "onboarded_at": "2026-08-01T00:00:00Z"})
    common.write_json(goneco / "status.json", {
        "slug": "goneco", "stage": "complete",
        "last_run": {"timestamp": "2026-08-10T09:00:00Z", "outcome": "sized",
                     "reason": None},
        "last_change_detected_at": "2026-08-10T09:00:00Z"})
    arch = root / ".archive" / "goneco"
    check("goneco active before offboard", "goneco" in common.list_companies(root))

    proc = run_script("offboard_company.py", "offboard", "goneco",
                      "--root", root, "--dry-run")
    check("dry-run reports but moves nothing",
          json.loads(proc.stdout).get("dry_run") is True
          and goneco.is_dir() and not arch.exists())

    ostate = phases.load_state(root)
    ostate["companies"]["goneco"] = {"phase": "launched",
                                     "tag": "goneco-sizer", "pid": None}
    phases.save_state(root, ostate)
    proc = run_script("offboard_company.py", "offboard", "goneco",
                      "--root", root, expect_rc=1)
    check("in-flight guard refuses and moves nothing",
          json.loads(proc.stdout)["outcome"] == "failed" and goneco.is_dir())
    ostate["companies"].pop("goneco")
    phases.save_state(root, ostate)

    proc = run_script("offboard_company.py", "offboard", "goneco", "--root", root)
    o = json.loads(proc.stdout)
    check("offboard outcome", o["outcome"] == "offboarded")
    check("dir moved to companies/.archive/",
          arch.is_dir() and not goneco.exists())
    check("invisible to list_companies",
          "goneco" not in common.list_companies(root))
    check("offboarded_at stamped in archived status.json",
          bool(common.read_json(arch / "status.json").get("offboarded_at")))
    dash_out2 = tmp / "index-offboard.html"
    proc = run_script("gen_dashboard.py", "--root", root, "--out", dash_out2)
    dsum = json.loads(proc.stdout)
    check("dashboard omits offboarded company",
          all(c["slug"] != "goneco" for c in dsum["companies"]))
    proc = run_script("offboard_company.py", "offboard", "goneco", "--root", root)
    check("re-offboard idempotent",
          json.loads(proc.stdout)["outcome"] == "already-offboarded")

    proc = run_script("offboard_company.py", "list", "--root", root)
    larch = json.loads(proc.stdout)["archived"]
    check("list shows archived company",
          any(c["slug"] == "goneco" and c.get("offboarded_at")
              and c.get("stage") == "complete" for c in larch))

    proc = run_script("offboard_company.py", "restore", "goneco", "--root", root)
    check("restore outcome", json.loads(proc.stdout)["outcome"] == "restored")
    check("dir moved back", goneco.is_dir() and not arch.exists())
    check("offboarded_at cleared on restore",
          "offboarded_at" not in common.read_json(goneco / "status.json"))
    check("active again after restore",
          "goneco" in common.list_companies(root))
    proc = run_script("offboard_company.py", "restore", "goneco", "--root", root)
    check("re-restore idempotent",
          json.loads(proc.stdout)["outcome"] == "already-active")

    run_script("offboard_company.py", "offboard", "nosuchco", "--root", root,
               expect_rc=1)

    print("\n— mint_push_sas (client push SAS: ledger + tokens page) —")
    ledger_path = root / ".sas-ledger.json"

    def _tail_json(proc):  # dry-run prints DRY-RUN: az lines before the JSON
        return json.loads(proc.stdout[proc.stdout.index("{"):])

    proc = run_script("mint_push_sas.py", "mint", "democo", "--root", root,
                      "--dry-run")
    m = _tail_json(proc)
    check("mint dry-run reports dry_run and mints nothing",
          m.get("dry_run") is True and not ledger_path.exists())
    check("mint dry-run carries the 14-day default",
          m.get("days") == 14)
    check("mint dry-run prints the generate-sas az command",
          "storage container generate-sas" in proc.stdout
          and "--permissions racwl" in proc.stdout)
    proc = run_script("mint_push_sas.py", "mint", "democo", "--root", root,
                      "--dry-run", "--days", "30")
    check("mint --days overrides the default",
          _tail_json(proc).get("days") == 30)
    run_script("mint_push_sas.py", "mint", "nosuchco", "--root", root,
               "--dry-run", expect_rc=1)

    # Real mint with az stubbed out: the deliverable is a password-protected
    # zip (ZipCrypto, like scripts/sas-mint) — the raw SAS never in the summary.
    import argparse as _argparse
    import mint_push_sas
    FAKE_SAS = "se=2026-09-11T00%3A00%3A00Z&sp=racwl&sig=FAKEFAKEFAKE"

    def _fake_run_az(azargs, dry_run=False, timeout=300, check=True):
        out = FAKE_SAS if "generate-sas" in azargs else ""
        return subprocess.CompletedProcess(["az"] + azargs, 0,
                                           stdout=out, stderr="")

    saved_run_az = common.run_az
    common.run_az = _fake_run_az
    try:
        msum = mint_push_sas.cmd_mint(root, _argparse.Namespace(
            slug="democo", days=14, note="test mint", dry_run=False,
            read_only=False))
        rsum = mint_push_sas.cmd_mint(root, _argparse.Namespace(
            slug="democo", days=14, note="test read mint", dry_run=False,
            read_only=True))
    finally:
        common.run_az = saved_run_az
    zip_path = Path(msum["zip"])
    check("mint outputs zip + password, never the raw SAS",
          zip_path.is_file() and msum["password"]
          and "sas_url" not in msum and FAKE_SAS not in json.dumps(msum))
    check("ledger entry written with zip path",
          ledger_path.is_file()
          and common.read_json(ledger_path)["tokens"][0]["zip"] == str(zip_path)
          and FAKE_SAS not in ledger_path.read_text())
    unz = tempfile.mkdtemp(prefix="cdp-sas-unzip-")
    p_ok = subprocess.run(["unzip", "-P", msum["password"], "-d", unz,
                           str(zip_path)], capture_output=True, text=True)
    creds = Path(unz) / "sas-credentials.txt"
    creds_txt = creds.read_text() if creds.is_file() else ""
    check("zip opens with the printed password and holds the SAS URL",
          p_ok.returncode == 0 and FAKE_SAS in creds_txt
          and "democo-raw" in creds_txt)
    check("credentials txt warns about the default-deny firewall",
          "network-rule add" in creds_txt and "403" in creds_txt
          and "rg-democo" in creds_txt)
    p_bad = subprocess.run(["unzip", "-o", "-P", "wrong-password", "-d", unz,
                            str(zip_path)], capture_output=True, text=True)
    check("zip refuses a wrong password", p_bad.returncode != 0)
    shutil.rmtree(unz)

    # --read-only: the rl download token for a data buyer, not a push token.
    r_zip = Path(rsum["zip"])
    check("read-only mint records rl perms in summary + ledger",
          rsum["permissions"] == "rl" and "read-sas-" in r_zip.name
          and common.read_json(ledger_path)["tokens"][1]["permissions"] == "rl")
    r_unz = tempfile.mkdtemp(prefix="cdp-sas-runzip-")
    subprocess.run(["unzip", "-P", rsum["password"], "-d", r_unz,
                    str(r_zip)], capture_output=True, text=True)
    r_txt = (Path(r_unz) / "sas-credentials.txt").read_text()
    check("read-only credentials txt gives download, not push, instructions",
          "download credentials" in r_txt and "read/list" in r_txt
          and "x-ms-blob-type" not in r_txt
          and "network-rule add" in r_txt)
    shutil.rmtree(r_unz)

    # Ledger/page are offline-testable without az: seed entries directly —
    # one live token minted "now", one long-expired.
    common.write_json(ledger_path, {"tokens": [
        {"id": "democo-20260101T000000Z", "slug": "democo",
         "storage_account": "stdemoco", "container": "democo-raw",
         "permissions": "racwl", "signing": "account-key",
         "created_at": "2026-01-01T00:00:00Z",
         "expires_at": "2026-01-15T00:00:00Z", "days": 14,
         "note": "initial push token", "fingerprint": "aaaaaaaaaaaa"},
        {"id": "democo-" + common.ts_basic(now), "slug": "democo",
         "storage_account": "stdemoco", "container": "democo-raw",
         "permissions": "racwl", "signing": "account-key",
         "created_at": common.iso(now),
         "expires_at": common.iso(now + timedelta(days=14)), "days": 14,
         "note": "re-mint", "fingerprint": "bbbbbbbbbbbb"},
    ]})
    proc = run_script("mint_push_sas.py", "list", "--root", root)
    lst = json.loads(proc.stdout)["tokens"]
    check("list computes status per token",
          [t["status"] for t in lst] == ["expired", "active"])
    check("list computes days_remaining",
          lst[0]["days_remaining"] == 0 and 13 <= lst[1]["days_remaining"] <= 14)

    sas_page = tmp / "sas-tokens.html"
    proc = run_script("mint_push_sas.py", "page", "--root", root,
                      "--out", sas_page)
    psum = json.loads(proc.stdout)
    page_html = sas_page.read_text()
    check("page written with both tokens",
          sas_page.is_file() and psum["tokens"] == 2 and psum["active"] == 1
          and page_html.count("democo-raw") == 2)
    check("page badges expired vs active",
          "expired" in page_html and "active" in page_html)
    check("page never contains a SAS query string", "sig=" not in page_html)
    proc = run_script("mint_push_sas.py", "page", "--root", root,
                      "--out", sas_page)
    check("page regeneration idempotent",
          json.loads(proc.stdout)["tokens"] == 2)

    print("\n— saxon sharepoint completion (one-off: pure functions) —")
    import saxon_sp_vm_pull as spp  # noqa: E402
    import saxon_sp_complete as spc  # noqa: E402

    check("relpath nested",
          spp.expected_relpath("Documents", "/drives/x/root:/A/B", "f.txt")
          == "Documents/A/B/f.txt")
    check("relpath at drive root",
          spp.expected_relpath("Documents", "/drives/x/root:", "f.txt")
          == "Documents/f.txt")
    check("relpath refuses missing root anchor",
          spp.expected_relpath("Documents", "/drives/x", "f.txt") is None)
    check("relpath keeps odd chars",
          spp.expected_relpath("Docs", "/drives/x/root:/RFQ's/a b", "f")
          == "Docs/RFQ's/a b/f")
    check("sitecoll ports the census regex",
          spp.sitecoll("https://h.sharepoint.com/sites/VSS1/Docs/x")
          == "https://h.sharepoint.com/sites/vss1"
          and spp.sitecoll("https://h.sharepoint.com/")
          == "https://h.sharepoint.com")

    _exp = {"Documents/a.txt": (10, "d", "i", "m"),
            "Documents/b.txt": (20, "d", "i", "m"),
            "Documents/c.txt": (30, "d", "i", "m")}
    _dst = {"Documents/a.txt": 10, "Documents/b.txt": 25,
            "Documents/x.txt": 5, "Documents/a.txt.meta.json": 1}
    _d = spp.diff_folder(_exp, _dst)
    check("diff: missing", _d["missing"] == ["Documents/c.txt"])
    check("diff: mismatch recorded, never copied",
          len(_d["mismatched"]) == 1
          and _d["mismatched"][0]["path"] == "Documents/b.txt"
          and _d["mismatched"][0]["dest_size"] == 25)
    check("diff: matched", _d["matched"] == 1 and _d["matched_bytes"] == 10)
    check("diff: sidecar counted apart from dest_only",
          _d["dest_only"] == 1 and _d["sidecars"] == 1)
    check("azure_name strips trailing dots per segment",
          spp.azure_name("A Pvt. Ltd./Docs/f.txt") == "A Pvt. Ltd/Docs/f.txt"
          and spp.azure_name("plain/path.txt") == "plain/path.txt")
    _dot_exp = {"Corp Pvt. Ltd./Docs/f.xlsx": (99, "d", "i", "m")}
    _dot_dest = {"Corp Pvt. Ltd/Docs/f.xlsx": 99}   # what Azure stored
    _dd = spp.diff_folder(_dot_exp, _dot_dest)
    check("trailing-dot path is MATCHED, not a phantom gap",
          _dd["missing"] == [] and _dd["matched"] == 1
          and _dd["dest_only"] == 0)

    check("classify table",
          [spp.classify(s, "delta") for s in
           (200, 429, 401, 503, 408, 403, 404, 400)]
          == ["ok", "sleep", "remint", "retry", "retry",
              "skip", "skip", "skip"])
    _bp = spp.block_plan(600 * spp.MIB, 256 * spp.MIB)
    check("block plan bounds",
          len(_bp) == 3 and _bp[0][1] == 0
          and _bp[-1][2] == 600 * spp.MIB - 1)
    check("block ids are uniform length (Azure's rule)",
          len({len(b[0]) for b in _bp}) == 1 and len(_bp[0][0]) == 12)
    _bp36 = spp.block_plan(600 * spp.MIB, 256 * spp.MIB, 36)
    check("widened block ids match a foreign 48-char id set",
          len({len(b[0]) for b in _bp36}) == 1 and len(_bp36[0][0]) == 48
          and [b[1] for b in _bp36] == [b[1] for b in _bp])
    check("padded vs unpadded 12-char ids decode to different widths",
          len(base64.b64decode(spp._block_id(0, 8))) == 8
          and len(base64.b64decode(spp._block_id(0, 9))) == 9
          and len(spp._block_id(0, 8)) == len(spp._block_id(0, 9)) == 12)
    check("widened ids stay decodable + distinct",
          base64.b64decode(_bp36[0][0]) == b"0" * 36
          and len({b[0] for b in _bp36}) == 3)
    check("source host pin",
          spp.source_host_ok("https://x.sharepoint.com/a")
          and spp.source_host_ok("https://cdn.svc.ms/a")
          and not spp.source_host_ok("https://evil.example.com/a"))

    _map = {"folders": [
        {"folder": "big", "action": "complete", "order_bytes": 100},
        {"folder": "cal", "action": "complete", "order_bytes": 1,
         "calibrate": True},
        {"folder": "small", "action": "complete", "order_bytes": 10},
        {"folder": "amb", "action": "skip-ambiguous"}]}
    check("plan_sites: calibration first, then size order",
          [r["folder"] for r in spp.plan_sites(_map, None, 0)]
          == ["cal", "big", "small"])
    _map_p = {"folders": _map["folders"] + [
        {"folder": "prio", "action": "complete", "order_bytes": 2,
         "priority": True}]}
    check("plan_sites: priority tier between calibration and the rest",
          [r["folder"] for r in spp.plan_sites(_map_p, None, 0)]
          == ["cal", "prio", "big", "small"])
    check("plan_sites: limit keeps calibration rows",
          [r["folder"] for r in spp.plan_sites(_map, None, 2)]
          == ["cal", "big"])
    check("plan_sites: only-sites filter",
          [r["folder"] for r in spp.plan_sites(_map, {"small"}, 0)]
          == ["small"])
    check("gate passes at 0.5% missing",
          spp.gate_check({"folder": "f", "expected_files": 1000,
                          "missing_before": 5}) is None)
    check("gate breaches at 2% missing",
          spp.gate_check({"folder": "f", "expected_files": 1000,
                          "missing_before": 20}) is not None)
    check("mapping-suspect: near-zero overlap on a real folder",
          spp.mapping_suspect({"matched": 1, "mismatched": [],
                               "dest_only": 999}, 500))
    check("mapping-suspect: healthy overlap passes",
          not spp.mapping_suspect({"matched": 400, "mismatched": [],
                                   "dest_only": 100}, 500))
    check("mapping-suspect: sliver folders exempt",
          not spp.mapping_suspect({"matched": 0, "mismatched": [],
                                   "dest_only": 30}, 500))

    _ord_exp = {"a": (10, "d", "i", "m"), "b": (5_000_000, "d", "i", "m"),
                "c": (900, "d", "i", "m")}
    _ord = sorted(["a", "b", "c"], key=lambda r: -_ord_exp[r][0])
    check("size-desc puts the byte-heavy files first",
          _ord == ["b", "c", "a"])

    _mdir = tmp / "spmani"
    _exp5 = {"Documents/a.txt": (10, "d1", "i1", "text/plain"),
             "Documents/b.bin": (20, "d1", "i2", "application/octet-stream")}
    spp.write_manifest(_mdir, "SiteX", "https://h/sites/x", _exp5)
    check("manifest round-trips through load_manifest",
          spp.load_manifest(_mdir / "SiteX.tsv.gz") == _exp5)
    _legacy = _mdir / "Legacy.tsv.gz"
    with gzip.open(_legacy, "wt", encoding="utf-8") as _fh:
        _fh.write("#site\tLegacy\thttps://h/sites/l\n")
        _fh.write("Documents/old.txt\t7\td9\ti9\n")     # 4-column, pre-mime
    _lm = spp.load_manifest(_legacy)
    check("4-column legacy manifest still loads, mime guessed",
          list(_lm) == ["Documents/old.txt"]
          and _lm["Documents/old.txt"][:3] == (7, "d9", "i9")
          and _lm["Documents/old.txt"][3] == "text/plain")

    _pb = spp.PaceBucket(12.0, 16.0)
    _pb.throttled()
    check("pace: one 429 halves the rate", abs(_pb.rate - 6.0) < 1e-6)
    for _ in range(8):          # a burst from the concurrent copy pool
        _pb.throttled()
    check("pace: burst 429s coalesce into ONE halving",
          abs(_pb.rate - 6.0) < 1e-6 and _pb.throttles == 9)
    _pb._last_throttle -= (spp.PaceBucket.THROTTLE_COOLDOWN_S + 1)
    _pb.throttled()
    check("pace: a LATER congestion event halves again",
          abs(_pb.rate - 3.0) < 1e-6)
    _pb2 = spp.PaceBucket(2.0, 16.0)
    for _ in range(5):
        _pb2._last_throttle -= (spp.PaceBucket.THROTTLE_COOLDOWN_S + 1)
        _pb2.throttled()
    check("pace: never drops below the floor",
          abs(_pb2.rate - spp.PaceBucket.MIN_RPS) < 1e-6)
    _pb3 = spp.PaceBucket(4.0, 16.0)
    _pb3._last_bump -= 61
    _pb3.wait()
    check("pace: a clean minute creeps the rate back up",
          abs(_pb3.rate - 4.5) < 1e-6)

    _guid = "dfeaf684-b537-4e57-9ea5-b76dae8558f0"
    _sites = [
        {"id": f"h.sharepoint.com,{_guid},"
               "11111111-1111-1111-1111-111111111111",
         "webUrl": "https://h.sharepoint.com/sites/connect4-0",
         "displayName": "Connect4.0"},
        {"id": "h.sharepoint.com,22222222-2222-2222-2222-222222222222,"
               "33333333-3333-3333-3333-333333333333",
         "webUrl": "https://h.sharepoint.com/sites/VSS1",
         "displayName": "VSS One"},
        {"id": "pers", "webUrl": "https://h-my.sharepoint.com/personal/u",
         "displayName": "personal"},
    ]
    _colls = spc.build_collections(_sites)
    check("collections drop personal sites",
          len(_colls) == 2
          and all("-my." not in k for k in _colls))
    _folders = {"VSS1": (10, 100, 0), "Connect4-0": (5, 500, 0),
                "Connect4.0": (5, 50, 2), _guid: (1, 1, 0),
                "NoSuchSite": (1, 1, 0)}
    _rows = {r["folder"]: r for r in spc.match_folders(_folders, _colls)}
    check("match: exact slug", _rows["VSS1"]["confidence"] == "exact"
          and _rows["VSS1"]["action"] == "complete")
    check("match: bare-GUID folder via site id",
          _rows[_guid]["confidence"] == "guid")
    check("match: two folders one collection -> byte-heavier wins",
          _rows["Connect4-0"]["action"] == "complete"
          and _rows["Connect4.0"]["action"] == "skip-duplicate-target")
    check("match: unmatched is skip-ambiguous, never guessed",
          _rows["NoSuchSite"]["action"] == "skip-ambiguous")
    _cal = spc.mark_calibration(
        list(_rows.values()),
        {"vss1": {"bytes": 2_000_000_000},
         "connect4-0": {"bytes": 3_000_000_000}})
    _r = _rows["VSS1"]
    check("calibration: only existing>=census sites marked",
          _cal == [] or all(
              _rows[f]["existing_bytes"] >= 2_000_000_000 for f in _cal))
    _rows["VSS1"]["existing_bytes"] = 2_500_000_000
    _cal = spc.mark_calibration(list(_rows.values()),
                                {"vss1": {"bytes": 2_000_000_000}})
    check("calibration: marks the caught-up site", _cal == ["VSS1"]
          and _rows["VSS1"].get("calibrate") is True)
    check("folder_stats splits sidecars",
          spc.folder_stats({"a": 10, "a.meta.json": 1, "b": 20})
          == (2, 30, 1))
    try:
        spc._tenant_guard("aaa", "bbb")
        check("tenant guard refuses mismatch", False)
    except common.HarnessError:
        check("tenant guard refuses mismatch", True)

    print("\n— saxon_sp_complete --dry-run (one-off guard + gates) —")
    check("SPEC is the sp one-off",
          spc.SPEC.vm_prefix == "xfer-sp-"
          and spc.SPEC.purpose == "saxon-sp-complete"
          and spc.SPEC.default_dest_prefix == "sharepoint"
          and spc.SPEC.default_os_disk_gb == 64)
    proc = run_script("saxon_sp_complete.py", "plan", "democo",
                      "--root", root, "--dry-run", expect_rc=1)
    check("slug guard refuses non-saxon",
          "ONE-OFF" in json.loads(proc.stdout)["error"])
    saxon_dir = root / "saxon"
    saxon_dir.mkdir()
    common.write_json(saxon_dir / "config.json", {
        "slug": "saxon", "subscription": "m1 corpus",
        "subscription_id": "x", "resource_group": "rg-x",
        "storage_account": "stx", "container": "saxon-technologies-raw",
        "vm": {"name": None, "resource_group": "rg-x", "exists": False}})
    run_script("saxon_sp_complete.py", "plan", "saxon",
               "--root", root, "--dry-run")
    proc = run_script("saxon_sp_complete.py", "transfer", "saxon",
                      "--root", root, "--dry-run", expect_rc=1)
    check("transfer refuses without a mapping",
          "plan first" in json.loads(proc.stdout)["error"])
    common.write_json(saxon_dir / "sharepoint-completion" / "mapping.json", {
        "slug": "saxon", "dest_prefix": "sharepoint", "approved_utc": None,
        "folders": [{"folder": "AGDemo", "action": "complete",
                     "site_url": "https://h.sharepoint.com/sites/agdemo",
                     "site_ids": ["h,1,2"], "existing_bytes": 1,
                     "order_bytes": 1}]})
    proc = run_script("saxon_sp_complete.py", "transfer", "saxon",
                      "--root", root, "--dry-run", expect_rc=1)
    check("transfer refuses an unapproved mapping",
          "not approved" in json.loads(proc.stdout)["error"])
    run_script("saxon_sp_complete.py", "approve-mapping", "saxon",
               "--root", root, "--dry-run")
    proc = run_script("saxon_sp_complete.py", "transfer", "saxon",
                      "--root", root, "--dry-run", "--diff-only")
    # DRY-RUN ssh lines precede the JSON — parse from the first brace
    check("transfer dry-run after approval",
          json.loads(proc.stdout[proc.stdout.index("{"):])
          .get("dry_run") is True and "DIFF_ONLY=1" in proc.stdout)
    run_script("saxon_sp_complete.py", "harvest", "saxon",
               "--root", root, "--dry-run")
    run_script("saxon_sp_complete.py", "create-vm", "saxon",
               "--root", root, "--dry-run", "--tenant-id",
               "12345678-1234-1234-1234-123456789012")
    run_script("saxon_sp_complete.py", "teardown", "saxon",
               "--root", root, "--dry-run", "--confirmed")

    # ── slack export file ingest ────────────────────────────────────────────
    # The fixture is a SYNTHETIC mini-export (tests/fixtures/
    # make_slack_export_mini.py) built to the schema of a real Business+
    # compliance export. Every awkward shape the real format has is in it
    # once, so these checks exercise the ledger's actual edge cases rather
    # than a happy path.
    print("\n— slack export ingest (ledger, transport classification, CLI)")
    import slack_vm_pull as svp   # noqa: E402
    import slack_transfer as slt  # noqa: E402

    MINI = REPO / "tests" / "fixtures" / "slack-export-mini.zip"
    check("slack fixture exists", MINI.exists(), str(MINI))

    # Slack writes UTF-8 member names with the UTF-8 flag bit UNSET, so
    # zipfile mis-decodes them as cp437. The fixture reproduces that exactly;
    # if this check ever fails the fixture stopped testing anything.
    zi = [i for i in zipfile.ZipFile(MINI).infolist()
          if "F0FIXCANVAS:" in i.filename][0]
    check("slack fixture reproduces Slack's missing UTF-8 flag bit",
          not (zi.flag_bits & 0x800) and zi.filename != svp.zip_member_name(zi),
          f"flag={zi.flag_bits:#06x} raw={zi.filename!r}")
    check("slack cp437 member names decode back to UTF-8",
          svp.zip_member_name(zi).startswith("FC:F0FIXCANVAS:\U0001f517 Links/"),
          repr(svp.zip_member_name(zi)))

    # Token handling (fact 1 + fact 2): files-pri uses `token=`, files-tmb
    # transcodes use `t=`, and attachments[].files[] entries carry neither.
    u_pri = "https://files.slack.com/files-pri/T-F/download/a.m4a?token=xoxe-A"
    u_tmb = "https://files.slack.com/files-tmb/T-F-x/f.vtt?_xcb=dc&t=xoxe-A"
    u_none = "https://files.slack.com/files-pri/T-F/download/b.png"
    check("slack token read from both `token=` and `t=`",
          svp.url_token(u_pri) == "xoxe-A" and svp.url_token(u_tmb) == "xoxe-A"
          and svp.url_token(u_none) is None)
    check("slack tokenless url gets the workspace token appended",
          svp.url_with_token(u_none, "xoxe-B").endswith("?token=xoxe-B"))
    check("slack tokened url is used verbatim",
          svp.url_with_token(u_tmb, "xoxe-B") == u_tmb)
    for url, want_param in ((u_pri, "token"), (u_tmb, "t"), (u_none, None)):
        bare, param = svp.strip_token(url)
        check(f"slack token strip/restore round-trips ({want_param})",
              param == want_param and "xoxe-A" not in bare
              and svp.restore_token(bare, param, "xoxe-A") == url
              if want_param else param is None and bare == url,
              f"{param!r} {bare!r}")
    check("slack `_xcb` and other query params survive the strip",
          "_xcb=dc" in svp.strip_token(u_tmb)[0])

    # Blob paths are id-led and deterministic: Slack file ids are immutable,
    # names are not, and a rename between two export passes must never orphan
    # already-copied bytes.
    check("slack blob path is id-led and sanitised",
          svp.blob_path("F047H9921PW", "Audio Clip (2022-10-20).m4a")
          == "files/F047/F047H9921PW/Audio_Clip__2022-10-20_.m4a")
    check("slack rendition path keeps the field name",
          svp.rendition_blob_path(
              "F047H9921PW", "thumb_360",
              "https://files.slack.com/files-tmb/x/i_360.png?t=1")
          == "renditions/F047/F047H9921PW/thumb_360__i_360.png")

    export = svp.ZipExport(MINI)
    ledger_dir = tmp / "slack-ledger"
    ledger = svp.build_ledger(export, ledger_dir, renditions=True)
    counts = ledger["counts"]
    check("slack ledger counts the fixture exactly",
          (counts["unique_files"], counts["hosted"], counts["external"],
           counts["unavailable"], counts["renditions"], counts["objects"])
          == (9, 7, 1, 1, 5, 12), json.dumps(counts))
    check("slack ledger harvests the export's embedded token",
          ledger["export_token_present"]
          and ledger["export_token"].startswith("xoxe-"))
    check("slack ledger sums declared bytes per disposition",
          ledger["declared_bytes"] == {"hosted": 402724169,
                                       "external": 4194304},
          json.dumps(ledger["declared_bytes"]))

    def jsonl(name):
        return [json.loads(x) for x in
                (ledger_dir / "_meta" / name).read_text(
                    encoding="utf-8").splitlines() if x.strip()]

    objects = jsonl("objects.jsonl")
    index = jsonl("files-index.jsonl")
    shares = jsonl("file-shares.jsonl")
    check("slack ledger writes one object row per blob",
          len(objects) == 12 and len({o["blob"] for o in objects}) == 12)
    check("slack ledger never stores the token in the container ledger",
          not any("xox" in json.dumps(r) for r in objects + index),
          "a ledger row still carries a credential")
    check("slack ledger records the token PARAM so it can be re-attached",
          {o["tp"] for o in objects} == {"token", "t", None},
          str(sorted(str(o["tp"]) for o in objects)))
    check("slack ledger picks up attachments[].files[] (tokenless)",
          any(o["file_id"] == "F0FIXATT001" and o["tp"] is None
              for o in objects))
    check("slack ledger picks up root canvases/lists/huddle transcripts",
          {"F0FIXCANVAS", "F0FIXLIST001", "F0FIXHUDDLE1"}
          <= {o["file_id"] for o in objects})
    check("slack ledger captures the canvas history link as a rendition",
          any(o["kind"] == "rendition:canvas_history" for o in objects))
    check("slack ledger dedups a re-shared file and records the sighting",
          len(shares) == 2
          and {s["file_id"] for s in shares} == {"F0FIXCANVAS", "F0FIXPNG001"}
          and len([o for o in objects
                   if o["file_id"] == "F0FIXPNG001"
                   and o["kind"] == "original"]) == 1)
    check("slack shares ledger carries the cp437-decoded conversation dir",
          any(s["conversation"]["dir"]
              == "FC:F0FIXCANVAS:\U0001f517 Links" for s in shares))
    check("slack external (gdrive) files are recorded, never queued for copy",
          [r["file_id"] for r in jsonl("external-references.jsonl")]
          == ["F0FIXEXT001"]
          and not any(o["file_id"] == "F0FIXEXT001" for o in objects))
    check("slack tombstones are recorded with their reason",
          [(r["file_id"], r["reason"])
           for r in jsonl("unavailable.jsonl")] == [("F0FIXTOMB01",
                                                     "tombstone")])
    check("slack ledger links every file to its conversation and message",
          all(r.get("conversation") for r in index)
          and any(r["file_id"] == "F0FIXPNG001"
                  and r["conversation"]["name"] == "general"
                  and r["message_ts"] == "1767312000.000100" for r in index))

    # --no-renditions is the object-count lever probe reports on.
    lean = svp.build_ledger(export, tmp / "slack-ledger-lean", renditions=False)
    check("slack --no-renditions drops derivative objects only",
          lean["counts"]["objects"] == 7
          and lean["counts"]["renditions"] == 0
          and lean["counts"]["hosted"] == counts["hosted"])

    # A second build must produce byte-identical blob paths — that is what
    # makes create-only resume correct across runs.
    again = svp.build_ledger(export, tmp / "slack-ledger-again",
                             renditions=True)
    check("slack ledger is deterministic across runs",
          [o["blob"] for o in jsonl("objects.jsonl")]
          == [json.loads(x)["blob"] for x in
              (tmp / "slack-ledger-again" / "_meta" / "objects.jsonl")
              .read_text(encoding="utf-8").splitlines() if x.strip()]
          and again["counts"] == counts)

    # classify() keys on the COPY-SOURCE status, not just Azure's.
    table = {
        (201, "", ""): "ok", (409, "", ""): "ok",
        (0, "", "429"): "sleep", (429, "", ""): "sleep",
        (0, "", "401"): "source-auth", (0, "", "403"): "source-auth",
        (0, "", "404"): "skip",
        (412, "CannotVerifyCopySource", ""): "resolve-again",
        (500, "", ""): "retry", (403, "", ""): "retry", (0, "", "503"): "retry",
        (400, "InvalidHeaderValue", ""): "fallback",
    }
    check("slack classify() maps every copy outcome",
          all(svp.classify(*k) == v for k, v in table.items()),
          str({k: svp.classify(*k) for k, v in table.items()
               if svp.classify(*k) != v}))

    # Export discovery: both shapes, and never a silent guess.
    listing = {
        "slack/export-2026.zip": {"size": 999},
        "slack-extracted/channels.json": {"size": 10},
        "slack-extracted/users.json": {"size": 10},
        "slack-extracted/general/2026-01-02.json": {"size": 10},
        "gdrive-export/whatever.txt": {"size": 1},
    }
    cands = slt.find_export_candidates(listing)
    check("slack discovery finds an extracted tree by its signature files",
          any(c["kind"] == "tree" and c["prefix"] == "slack-extracted"
              for c in cands))
    check("slack discovery surfaces .zip blobs as candidates",
          any(c["kind"] == "zip" and c["blob"] == "slack/export-2026.zip"
              for c in cands))
    check("slack discovery ignores unrelated prefixes",
          not any("gdrive-export" in json.dumps(c) for c in cands))
    check("slack export_ref round-trips both shapes",
          slt.parse_export_ref(slt.export_ref(
              {"kind": "zip", "blob": "a/b.zip"})) == {"kind": "zip",
                                                       "blob": "a/b.zip"}
          and slt.parse_export_ref(slt.export_ref(
              {"kind": "tree", "prefix": "x/y"})) == {"kind": "tree",
                                                      "prefix": "x/y"})
    check("slack detect_export_root handles a re-zipped wrapper folder",
          svp.detect_export_root(
              ["Slack Export/channels.json", "Slack Export/users.json",
               "Slack Export/general/2026-01-02.json"]) == "Slack Export/"
          and svp.detect_export_root(["random/thing.json"]) is None)

    # verify: the byte-exact claim only this family can make.
    ok_objs = [{"blob": "files/F0/A/a.png", "kind": "original", "size": 100,
                "status": "copied"},
               {"blob": "renditions/F0/A/t.png", "kind": "rendition:thumb_64",
                "size": 0, "status": "copied"},
               {"blob": "files/F0/B/b.png", "kind": "original", "size": 5,
                "status": "gone"}]
    good = {"p/files/F0/A/a.png": {"size": 100},
            "p/renditions/F0/A/t.png": {"size": 42}}
    res = slt.compare_objects_to_blobs(list(ok_objs), good, "p")
    check("slack verify passes when declared bytes match what Azure committed",
          res["ok"] and res["expected_objects"] == 2 and res["originals"] == 1
          and res["renditions"] == 1 and res["missing"] == 0, json.dumps(res))
    res = slt.compare_objects_to_blobs(
        list(ok_objs), {"p/files/F0/A/a.png": {"size": 99},
                        "p/renditions/F0/A/t.png": {"size": 42}}, "p")
    check("slack verify catches a declared-vs-landed size mismatch",
          not res["ok"] and res["size_mismatches"] == 1
          and res["size_mismatch_sample"][0]["landed"] == 99)
    res = slt.compare_objects_to_blobs(
        list(ok_objs), {"p/renditions/F0/A/t.png": {"size": 42}}, "p")
    check("slack verify catches a missing object",
          not res["ok"] and res["missing"] == 1
          and res["missing_sample"][0]["blob"] == "files/F0/A/a.png")
    res = slt.compare_objects_to_blobs(
        list(ok_objs), dict(good, **{"p/files/F0/B/b.png": {"size": 5}}), "p")
    check("slack verify treats a since-landed 'gone' file as informational",
          res["ok"] and res["unexpected_present"] == 1)

    # CLI: every subcommand under --dry-run, and the refusals that matter.
    check("slack Spec follows the VM-family conventions",
          slt.SPEC.vm_prefix == "xfer-slack-"
          and slt.SPEC.authorize_target == ""
          and slt.SPEC.remote_type == ""
          and slt.SPEC.default_dest_prefix == "slack-export-files"
          and slt.SPEC.default_os_disk_gb == 256)
    proc = run_script("slack_transfer.py", "plan", "democo", "--root", root,
                      "--dry-run", "--export-blob", "slack/export.zip")
    plan = json.loads(proc.stdout)
    check("slack plan names the export and warns about the token clock",
          plan["source"] == "slack:zip:slack/export.zip"
          and "expire" in plan["note"], json.dumps(plan)[:300])
    proc = run_script("slack_transfer.py", "plan", "democo", "--root", root,
                      "--dry-run", expect_rc=2)
    check("slack plan refuses without an export location",
          "requires --export-blob" in proc.stderr, proc.stderr[-200:])
    for cmd in ("discover", "discover-export", "create-vm", "allow-network",
                "check-azure", "status", "verify", "transfer"):
        run_script("slack_transfer.py", cmd, "democo", "--root", root,
                   "--dry-run", "--export-blob", "slack/export.zip")
    proc = run_script("slack_transfer.py", "write-dest", "democo", "--root",
                      root, "--dry-run", "--export-blob", "slack/export.zip")
    out = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("slack write-dest carries the export location to the VM env",
          out["export"] == {"kind": "zip", "blob": "slack/export.zip"}
          and out["dest_prefix"] == "slack-export-files")
    proc = run_script("slack_transfer.py", "write-dest", "democo", "--root",
                      root, "--dry-run", expect_rc=1)
    check("slack write-dest refuses without an export location",
          "no export location" in json.loads(
              proc.stdout[proc.stdout.index("{"):])["error"])
    proc = run_script("slack_transfer.py", "transfer", "democo", "--root",
                      root, "--dry-run", "--limit-files", "50",
                      "--no-renditions")
    out = json.loads(proc.stdout[proc.stdout.index("{"):])
    check("slack transfer dry-run discloses the pilot + rendition env",
          "LIMIT_FILES=50" in out["env"] and "RENDITIONS=0" in out["env"]
          and out["limit_files"] == 50 and out["renditions"] is False,
          json.dumps(out))
    proc = run_script("slack_transfer.py", "teardown", "democo", "--root",
                      root, "--dry-run", expect_rc=2)
    check("slack teardown refuses without --confirmed",
          "confirm" in proc.stdout.lower())
    proc = run_script("slack_transfer.py", "write-creds", "democo", "--root",
                      root, "--dry-run", expect_rc=1,
                      stdin_data="not-a-slack-token\n")
    check("slack write-creds refuses a non-Slack token",
          "not a Slack token" in json.loads(
              proc.stdout[proc.stdout.index("{"):])["error"])
    proc = run_script("slack_transfer.py", "write-creds", "democo", "--root",
                      root, "--dry-run", expect_rc=1,
                      stdin_data="xoxp-a'b\n")
    check("slack write-creds refuses a token that would corrupt the env file",
          "single quote" in json.loads(
              proc.stdout[proc.stdout.index("{"):])["error"])
    proc = run_script("slack_transfer.py", "write-creds", "democo", "--root",
                      root, "--dry-run", stdin_data="xoxp-fixture-token\n")
    check("slack write-creds never echoes the token",
          "xoxp-fixture-token" not in proc.stdout
          and json.loads(proc.stdout[proc.stdout.index("{"):])["secret"]
          == "redacted")

    # ── slack copy phase, end to end against an in-memory Azure + Slack ─────
    # The copy phase is where every interesting failure lives (server-side
    # copy, block staging, a deleted file, an Azure refusal falling back to
    # streaming, a dead export token), and none of it is reachable through
    # --dry-run. Both HTTP seams and Blob.list are stubbed; nothing leaves
    # this process.
    print("\n— slack copy phase (fake Azure + fake Slack)")
    import email.message  # noqa: E402

    SLACK_BASE = "https://stfixture.blob.core.windows.net/fixture-raw"
    SLACK_PREFIX = "slack-export-files"
    SLACK_SIZES = {"F0FIXCANVAS": 80, "F0FIXLIST001": 7,
                   "F0FIXHUDDLE1": 5066, "F0FIXDM0001": 900,
                   "F0FIXPNG001": 52594, "F0FIXATT001": 12345,
                   "F0FIXVID001": 402653184}

    class _Resp:
        def __init__(self, status=200, headers=None, body=b""):
            self.status, self.headers = status, headers or {}
            self._body = body

        def read(self, n=-1):
            return self._body

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _herr(url, code, hdrs=None, body=b""):
        m = email.message.Message()
        for k, v in (hdrs or {}).items():
            m[k] = v
        return urllib.error.HTTPError(url, code, f"HTTP {code}", m,
                                      io.BytesIO(body))

    class _FakeCloud:
        """Azure container + Slack file host, in memory."""

        def __init__(self, behaviour):
            self.blobs, self.blocks, self.behaviour = {}, {}, behaviour
            self.copies = 0

        @staticmethod
        def _fid(url):
            for part in urllib.parse.urlsplit(url).path.split("/"):
                for tok in part.split("-"):
                    if tok.startswith("F0FIX"):
                        return tok
            return None

        def nr(self, req, timeout=60):          # Slack, no redirects
            fid = self._fid(req.full_url)
            how = self.behaviour.get(fid, "ok")
            if how in ("404", "403"):
                raise _herr(req.full_url, int(how))
            return _Resp(206, {"Content-Range":
                               f"bytes 0-0/{SLACK_SIZES.get(fid, 10)}"})

        def _name(self, url):
            path = urllib.parse.urlsplit(url).path
            return urllib.parse.unquote(
                path[len(urllib.parse.urlsplit(SLACK_BASE).path) + 1:])

        def http(self, req, timeout=90):        # Azure blob REST
            q = urllib.parse.urlsplit(req.full_url).query
            name = self._name(req.full_url)
            if req.get_method() != "PUT":
                return _Resp(200, body=self.blobs.get(name, b""))
            if "comp=blocklist" in q:
                self.blobs[name] = sum(
                    self.blocks.pop(k) for k in list(self.blocks)
                    if k[0] == name)
                return _Resp(201)
            if "comp=block" in q:
                lo, hi = (req.get_header("X-ms-source-range")
                          or "bytes=0-0").split("=")[1].split("-")
                self.blocks[(name, q)] = int(hi) - int(lo) + 1
                return _Resp(201)
            if req.get_header("If-none-match") == "*" and name in self.blobs:
                raise _herr(req.full_url, 409,
                            body=b"<Error><Code>BlobAlreadyExists</Code>"
                                 b"</Error>")
            src = req.get_header("X-ms-copy-source")
            if src:
                if self.behaviour.get(self._fid(src)) == "azure-refuses":
                    raise _herr(req.full_url, 400, {},
                                b"<Error><Code>InvalidHeaderValue</Code>"
                                b"</Error>")
                self.copies += 1
                self.blobs[name] = SLACK_SIZES.get(self._fid(src), 10)
            else:
                self.blobs[name] = len(req.data or b"")
            return _Resp(201)

        def listing(self, prefix):
            return {k: (v if isinstance(v, int) else len(v))
                    for k, v in self.blobs.items() if k.startswith(prefix)}

    _saved = (svp._http, svp._http_nr, svp.Blob.list, svp._sleep)

    def slack_run(behaviour, seed_blobs=None):
        cloud = _FakeCloud(behaviour)
        if seed_blobs:
            cloud.blobs.update(seed_blobs)
        svp._http, svp._http_nr, svp._sleep = cloud.http, cloud.nr, \
            (lambda _s: None)
        svp.Blob.list = lambda self, prefix: cloud.listing(prefix)
        d = Path(tempfile.mkdtemp(prefix="slack-copy-"))
        led = svp.build_ledger(svp.ZipExport(MINI), d, renditions=True)
        out = svp.run_copy(svp.Blob(SLACK_BASE, "sig=fake"), SLACK_PREFIX, d,
                           d / "_meta" / "objects.jsonl",
                           led["export_token"], None,
                           {"shard_size": 5, "copy_workers": 2,
                            "limit_files": 0, "rps_files": 0})
        return cloud, led, out, d

    try:
        cloud, led, out, cdir = slack_run({})
        check("slack copy: every object lands server-side, no failures",
              out["totals"] == {"copied": 12, "streamed": 0,
                                "already_present": 0,
                                "copied_bytes": 805482635, "failed": 0},
              json.dumps(out["totals"]))
        check("slack copy: a file over the single-shot limit is block-staged",
              cloud.copies == 10 and cloud.blobs[
                  f"{SLACK_PREFIX}/files/F0FI/F0FIXVID001/screenshare.mp4"]
              == 402653184, f"copies={cloud.copies}")
        check("slack copy: one .cdp-complete marker per shard",
              sorted(k for k in cloud.blobs
                     if k.endswith(".cdp-complete"))
              == [f"{SLACK_PREFIX}/_meta/shards/{i:05d}.cdp-complete"
                  for i in range(3)])

        svp.patch_statuses(cdir / "_meta" / "objects.jsonl",
                           out["status_by_blob"])
        objs = [json.loads(x) for x in
                (cdir / "_meta" / "objects.jsonl").read_text(
                    encoding="utf-8").splitlines() if x.strip()]
        res = slt.compare_objects_to_blobs(
            objs, {k: {"size": v} for k, v in cloud.blobs.items()},
            SLACK_PREFIX)
        check("slack verify certifies a clean run byte-for-byte",
              res["ok"] and res["missing"] == 0
              and res["size_mismatches"] == 0
              and res["landed_bytes"] == 805482635, json.dumps(res)[:300])

        # A file deleted from Slack since the export is a recorded gap, and an
        # Azure refusal falls back to streaming that one file through the VM.
        cloud, led, out, _ = slack_run({"F0FIXDM0001": "404",
                                        "F0FIXATT001": "azure-refuses"})
        check("slack copy: a since-deleted file is 'gone', never a failure",
              out["status_by_blob"]["files/F0FI/F0FIXDM0001/notes.txt"]
              == "gone" and out["totals"]["failed"] == 0)
        check("slack copy: an Azure copy refusal falls back to streaming",
              out["totals"]["streamed"] == 1
              and out["status_by_blob"][
                  "files/F0FI/F0FIXATT001/unfurled.png"] == "copied")

        # A dead export token must stop the run, not burn hours on nothing.
        try:
            slack_run({k: "403" for k in SLACK_SIZES})
            check("slack copy: a dead export token aborts the run", False,
                  "TokenDead was not raised")
        except svp.TokenDead as exc:
            check("slack copy: a dead export token aborts the run",
                  "token is dead" in str(exc)
                  and "not a retry" in str(exc), str(exc)[:200])

        # Resume: the blob's own existence is the record.
        cloud0, _led0, _out0, _d0 = slack_run({})
        cloud, led, out, _ = slack_run({}, seed_blobs=dict(cloud0.blobs))
        check("slack copy: a resume re-copies nothing and says so",
              cloud.copies == 0 and out["totals"]["copied"] == 0
              and out["totals"]["already_present"] == 12
              and all(r["status"] == "skipped-complete"
                      for r in out["results"]), json.dumps(out["totals"]))
    finally:
        svp._http, svp._http_nr, svp.Blob.list, svp._sleep = _saved

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
