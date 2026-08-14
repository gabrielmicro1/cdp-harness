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

        # ── Finding 1: a poisoned cached det_json (right shape of JSON,
        # wrong shape of value — e.g. from a future format change) must not
        # silently truncate a run. main() must raise and never write .done. ──
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
        raised = None
        try:
            sizer.main()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        check("poisoned cache: main() raises instead of a truncated run",
              raised is not None, repr(raised))
        check("poisoned cache: no .done written",
              not (swork / "fakeco-sizer.done").exists())
    finally:
        sizer.http_get = real_http
        for k in list(sizer_env) + ["CACHE_FILE", "SEED_TSV"]:
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
               "sources_l2": {"a/(files)": [5, 10, 20]}}
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
                                                  "sources_l2")},
                                    {"metric": 1, "metric_at": "t"}, [])
    check("summary_to_run tolerates old summaries",
          old_run["cache"] is None and old_run["detected_services"] == {}
          and old_run["sources_l2"] == {})

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
    finally:
        phases.ip_rule_ensure, phases.mint_sas = real_ensure, real_sas
        phases.ip_rule_remove_if_ours = real_remove
        os.environ.pop("CDP_SIZER_SCRIPT", None)

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
    check("create-vm surfaces the human network-rule step",
          "internal" in out and "network-rule" in out)
    proc = run_script("gcs_transfer.py", "write-azure-remote", "democo",
                      "--root", root, "--dry-run")
    check("azure remote: rwlc-class SAS, secrets redacted",
          "--permissions racwl" in proc.stdout
          and "redacted" in proc.stdout)
    check("transfer path NEVER adds network rules",
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
    check("teardown reminds about IP + vnet-rule removal",
          "internal UI" in proc.stdout and "vnet-rule" in proc.stdout)

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
          "--transfers 8" in proc.stdout)
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
