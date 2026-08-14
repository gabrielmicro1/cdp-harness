"""Declared-vs-actual reconciliation — the single owner of comparison math.

gen_report.py, gen_dashboard.py, and verify_completion.py all consume
company_summary(); none of them re-derive percentages, deltas, ETAs, or flags.
The comparison column is UNCOMPRESSED bytes (the manifest declares the
client's logical data size — see CLAUDE.md "learned the hard way").
"""
from __future__ import annotations

import re
from pathlib import Path

import common

# Export-timestamp top-level prefixes (latchel: 20260707T180401Z, a Google
# Takeout run) — the real sources are one level deeper.
TIMESTAMP_PREFIX_RE = re.compile(r"^\d{8}[T_-]?\d{4,6}Z?$")


def norm(name: str) -> str:
    """Match manifest service names to blob prefixes: casefold, alnum only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find_source(service: str, sources: dict) -> str | None:
    n = norm(service)
    for src in sources:
        if norm(src) == n:
            return src
    return None


def _find_detected(service: str, detected: dict) -> dict | None:
    n = norm(service)
    for svc, d in detected.items():
        if norm(svc) == n:
            return d
    return None


def service_rows(expected: dict | None, run: dict | None) -> tuple[list[dict], list[str]]:
    """Per-service reconciliation rows + unexpected actual sources.

    Row: {service, declared_bytes, declared_records, actual_bytes,
          actual_compressed, blob_count, pct, flags[]}
    Flags: record-count | declared-empty | zero-declared-has-data | overshoot | found-embedded
    """
    services = (expected or {}).get("services", {})
    sources = dict((run or {}).get("sources", {}))
    detected = (run or {}).get("detected_services", {})
    rows = []
    matched = set()
    for svc, decl in services.items():
        src = _find_source(svc, sources)
        actual = sources.get(src, {}) if src else {}
        if src:
            matched.add(src)
        row = {
            "service": svc,
            "declared_bytes": decl.get("bytes"),
            "declared_records": decl.get("records"),
            "actual_bytes": actual.get("uncompressed_bytes", 0),
            "actual_compressed": actual.get("compressed_bytes", 0),
            "blob_count": actual.get("blob_count", 0),
            "pct": None,
            "flags": [],
        }
        if row["declared_records"] is not None:
            # record-count declarations are EXCLUDED from byte reconciliation
            row["flags"].append("record-count")
        elif row["declared_bytes"] == 0:
            if row["actual_bytes"] > 0:
                row["flags"].append("zero-declared-has-data")
        elif row["declared_bytes"]:
            row["pct"] = row["actual_bytes"] / row["declared_bytes"] * 100
            if row["actual_bytes"] == 0:
                d = _find_detected(svc, detected)
                if d and d.get("bytes", 0) > 0:
                    # data exists, just inside another source's blobs
                    row["flags"].append("found-embedded")
                    row["embedded_bytes"] = d["bytes"]
                    row["embedded_in"] = sorted(
                        d.get("sources", {}),
                        key=lambda s: -d["sources"][s])
                else:
                    row["flags"].append("declared-empty")
            elif row["pct"] > 100:
                # overshoot is often a WRONG MANIFEST — commercially significant
                row["flags"].append("overshoot")
        rows.append(row)
    unexpected = sorted(s for s in sources if s not in matched)
    return rows, unexpected


def lore_notes(run: dict | None) -> list[str]:
    """Interpretation notes from the original sizing runs (now maintained in
    size-company's sizing-lore.md), applied when the run's data shows the
    pattern they explain."""
    if not run:
        return []
    notes = []
    t = run.get("totals", {})
    comp, unc = t.get("compressed_bytes", 0), t.get("uncompressed_bytes", 0)
    if comp and 0.9 <= unc / comp <= 1.02:
        notes.append(
            "Compression ratio ≈ 1.0 across the container: the zips are "
            "store-mode bundles (packaged, not compressed). Uncompressed "
            "slightly below compressed is the classic tell. The numbers are real.")
    ts_prefixes = [s for s in run.get("sources", {})
                   if TIMESTAMP_PREFIX_RE.match(s)]
    if ts_prefixes:
        notes.append(
            f"Top-level prefix(es) {', '.join(ts_prefixes)} look like export "
            f"timestamps (e.g. a Google Takeout run), not source names — the "
            f"real sources are one level deeper. Consider re-splitting on the "
            f"second path segment before sharing per-source numbers.")
    gzinfo = run.get("gz")
    if gzinfo is not None:
        if gzinfo.get("uncertain", 0) > 0:
            notes.append(
                f"{gzinfo['uncertain']} gz blob(s) "
                f"({gzinfo['uncertain_bytes'] / 1e9:.1f} GB compressed) could "
                f"not be measured from gzip trailers (>=4 GiB wrap or "
                f"multi-member archives) — the true logical size may exceed "
                f"the measured total.")
    elif run.get("methods", {}).get("gz", 0) > 0:
        notes.append(
            ".tar.gz files are sized from the gzip trailer: exact below 4 GiB, "
            "floored at stored size above — multi-GB tarballs are a small, "
            "known undercount (the price of not streaming them for hours).")
    badzip = run.get("errors", {}).get("by_type", {}).get("BadZipFile", 0)
    if badzip:
        notes.append(
            f"{badzip} BadZipFile error(s): corrupt or mislabeled .zip files "
            f"(common in scraped/backup trees), counted at stored size — "
            f"negligible impact.")
    return notes


UNDECLARED_NOTE_FLOOR = 1_000_000_000  # surface undeclared finds ≥1 GB only


def detection_notes(rows: list[dict], expected: dict | None,
                    run: dict | None) -> list[str]:
    """Notes from the sizer's embedded-service detection: declared services
    found inside other sources, and material undeclared discoveries."""
    notes = []
    for r in rows:
        if "found-embedded" in r["flags"]:
            hosts = ", ".join(r["embedded_in"][:3])
            notes.append(
                f"{r['service']} shows no top-level data, but "
                f"~{r['embedded_bytes'] / 1e9:.1f} GB of {r['service']} data "
                f"was detected inside {hosts} — likely exported within "
                f"another service's archive.")
    declared = {norm(s) for s in (expected or {}).get("services", {})}
    tops = {norm(s) for s in (run or {}).get("sources", {})}
    for svc, d in (run or {}).get("detected_services", {}).items():
        if norm(svc) in declared or norm(svc) in tops:
            continue
        if d.get("bytes", 0) >= UNDECLARED_NOTE_FLOOR:
            hosts = ", ".join(sorted(d.get("sources", {}),
                                     key=lambda s: -d["sources"][s])[:3])
            notes.append(
                f"Detected ~{d['bytes'] / 1e9:.1f} GB of {svc} data under "
                f"{hosts}; {svc} is not declared in the manifest.")
    return notes


def _rate_and_eta(latest: dict, prev: dict | None, remaining: float):
    """bytes/day from the two most recent runs → projected completion date."""
    if not prev:
        return None, None
    try:
        dt_days = ((common.parse_iso(latest["timestamp"])
                    - common.parse_iso(prev["timestamp"])).total_seconds() / 86400)
    except (KeyError, ValueError):
        return None, None
    if dt_days <= 0:
        return None, None
    delta = (latest["totals"]["uncompressed_bytes"]
             - prev["totals"]["uncompressed_bytes"])
    rate = delta / dt_days
    if rate <= 0 or remaining is None:
        return rate, None
    from datetime import timedelta
    days_left = remaining / rate
    if days_left > 3650:
        return rate, None
    eta = common.parse_iso(latest["timestamp"]) + timedelta(days=days_left)
    return rate, common.iso(eta)


def delta_24h(root: Path, slug: str) -> int | None:
    """Uncompressed growth vs the newest run at least ~20h older than the
    latest (falls back to the previous run)."""
    paths = common.sizing_runs(root, slug)
    if len(paths) < 2:
        return None
    latest = common.read_json(paths[-1])
    latest_t = common.parse_iso(latest["timestamp"])
    baseline = None
    for p in reversed(paths[:-1]):
        r = common.read_json(p)
        baseline = r
        if (latest_t - common.parse_iso(r["timestamp"])).total_seconds() >= 20 * 3600:
            break
    if baseline is None:
        return None
    return (latest["totals"]["uncompressed_bytes"]
            - baseline["totals"]["uncompressed_bytes"])


def company_summary(root: Path, slug: str) -> dict:
    """Everything a report/dashboard/verification needs, in one dict."""
    cfg = common.load_config(root, slug)
    expected = common.load_expected(root, slug)
    status = common.load_status(root, slug)
    runs = common.latest_runs(root, slug, 2)
    latest = runs[0] if runs else None
    prev = runs[1] if len(runs) > 1 else None

    manifest_total = (expected or {}).get("manifest_total_bytes")
    unc_total = latest["totals"]["uncompressed_bytes"] if latest else None
    pct = (unc_total / manifest_total * 100
           if latest and manifest_total else None)
    remaining = (max(manifest_total - unc_total, 0)
                 if latest and manifest_total else None)
    rate, eta = _rate_and_eta(latest, prev, remaining) if latest else (None, None)

    rows, unexpected = service_rows(expected, latest)

    now = common.utc_now()
    last_change = status.get("last_change_detected_at")
    days_since_change = ((now - common.parse_iso(last_change)).total_seconds() / 86400
                         if last_change else None)
    stalled = bool(latest and pct is not None and pct < 100
                   and days_since_change is not None and days_since_change >= 3)

    return {
        "slug": slug,
        "config": cfg,
        "expected": expected,
        "status": status,
        "latest_run": latest,
        "prev_run": prev,
        "manifest_total_bytes": manifest_total,
        "uncompressed_total": unc_total,
        "pct_complete": pct,
        "remaining_bytes": remaining,
        "rate_bytes_per_day": rate,
        "eta": eta,
        "delta_24h": delta_24h(root, slug),
        "stalled": stalled,
        "days_since_change": days_since_change,
        "service_rows": rows,
        "unexpected_sources": unexpected,
        "notes": lore_notes(latest) + detection_notes(rows, expected, latest),
        "expected_confirmed": bool((expected or {}).get("confirmed_by_user")),
    }
