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


def _leaf(name: str) -> str:
    """Last path segment of a source key ("gdrive-export2/Notion" → "Notion").
    Split sources keep their full path so reports stay unambiguous; matching
    also considers the leaf so a bare manifest name still finds them."""
    return name.rsplit("/", 1)[-1]


def _find_sources(service: str, decl: dict, sources: dict) -> list[str]:
    """All actual sources a declaration covers. An explicit "prefix" in the
    declaration (string or list) pins it to named source(s) — for manifest
    names that don't match what the client pushed (e.g. google_workspace →
    workspace-export). Otherwise match by normalized name; every case/
    punctuation variant counts (zoom/ and Zoom/ are one service). Both forms
    match a split source by its full path or by its leaf, so
    "gdrive-export2/Vanta data" can be pinned either way."""
    want = decl.get("prefix")
    if want:
        wanted = {norm(w) for w in ([want] if isinstance(want, str) else want)}
        return [s for s in sources
                if norm(s) in wanted or norm(_leaf(s)) in wanted]
    n = norm(service)
    return [s for s in sources if norm(s) == n or norm(_leaf(s)) == n]


def effective_sources(expected: dict | None, run: dict | None) -> dict:
    """The sources the reconciler compares against — the run's top-level
    prefixes, except that any prefix listed in the declaration's
    "source_split" is replaced by its second-level children.

    Why: some clients push every service INSIDE one export folder (swiftlaw's
    gdrive-export2/, 2026-08), so the top level is a single meaningless
    prefix and the real per-service split lives one level deeper. Children
    keep their full "parent/child" key.

    Bytes are conserved. sources_l2 is a capped rollup, so whatever the
    children don't account for stays in a "parent/(unaccounted)" bucket
    rather than silently vanishing from the per-source view."""
    sources = dict((run or {}).get("sources", {}))
    split = (expected or {}).get("source_split") or []
    l2 = (run or {}).get("sources_l2") or {}
    for parent in split:
        if parent not in sources:
            continue
        kids = {k: v for k, v in l2.items() if k.startswith(parent + "/")}
        if not kids:
            continue                      # nothing deeper recorded; leave as-is
        seen = [0, 0, 0]
        for key, (files, comp, unc) in kids.items():
            sources[key] = {"blob_count": files, "compressed_bytes": comp,
                            "uncompressed_bytes": unc}
            seen[0] += files
            seen[1] += comp
            seen[2] += unc
        total = sources.pop(parent)
        rest = [max(total.get(f, 0) - seen[i], 0) for i, f in enumerate(
            ("blob_count", "compressed_bytes", "uncompressed_bytes"))]
        if any(rest):
            sources[f"{parent}/(unaccounted)"] = {
                "blob_count": rest[0], "compressed_bytes": rest[1],
                "uncompressed_bytes": rest[2]}
    return sources


def _find_detected(service: str, detected: dict) -> dict | None:
    n = norm(service)
    for svc, d in detected.items():
        if norm(svc) == n:
            return d
    return None


def service_rows(expected: dict | None, run: dict | None,
                 sources: dict | None = None) -> tuple[list[dict], list[str]]:
    """Per-service reconciliation rows + unexpected actual sources.

    Row: {service, declared_bytes, declared_records, actual_bytes,
          actual_compressed, blob_count, pct, flags[]}
    Flags: record-count | declared-empty | zero-declared-has-data | overshoot | found-embedded | deduplicated | unit-adjusted

    A declaration may carry "equivalent_bytes" (+ mandatory
    "equivalent_note"): the ACTUAL data re-expressed in the manifest's own
    unit, for services where the two are known to differ (healthtap's
    bigquery: the sizer measures parquet decompressed PAGE bytes, the client
    declared BigQuery console LOGICAL bytes; a decoded-sample measurement
    bridges them). pct then compares like for like (equivalent vs declared),
    actual_bytes stays the sizer's truth, the row is flagged unit-adjusted,
    and equivalent_unit_notes() always surfaces the basis — never silent,
    the duplicate_prefixes discipline.
    """
    services = (expected or {}).get("services", {})
    if sources is None:
        sources = effective_sources(expected, run)
    sources = dict(sources)
    detected = (run or {}).get("detected_services", {})
    rows = []
    matched = set()
    for svc, decl in services.items():
        srcs = _find_sources(svc, decl, sources)
        matched.update(srcs)
        row = {
            "service": svc,
            "declared_bytes": decl.get("bytes"),
            "declared_records": decl.get("records"),
            "actual_bytes": sum(sources[s].get("uncompressed_bytes", 0)
                                for s in srcs),
            "actual_compressed": sum(sources[s].get("compressed_bytes", 0)
                                     for s in srcs),
            "blob_count": sum(sources[s].get("blob_count", 0) for s in srcs),
            "pct": None,
            "flags": [],
        }
        dedup = sum(sources[s].get("deduplicated_bytes", 0) for s in srcs)
        if dedup:
            # actual_bytes already excludes the redundant share (see
            # apply_duplicates) — the flag makes the subtraction visible
            row["flags"].append("deduplicated")
            row["deduplicated_bytes"] = dedup
        if row["declared_records"] is not None:
            # record-count declarations are EXCLUDED from byte reconciliation
            row["flags"].append("record-count")
        elif row["declared_bytes"] == 0:
            if row["actual_bytes"] > 0:
                row["flags"].append("zero-declared-has-data")
        elif row["declared_bytes"]:
            eq = decl.get("equivalent_bytes")
            if eq and row["actual_bytes"] > 0:
                row["equivalent_bytes"] = eq
                row["equivalent_note"] = decl.get("equivalent_note", "")
                row["flags"].append("unit-adjusted")
                row["pct"] = eq / row["declared_bytes"] * 100
            else:
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
    ver = run.get("verification")
    gzinfo = run.get("gz")
    if ver:
        # deep-verified run: supersedes the gz-uncertainty notes below —
        # archives were stream-decompressed, not trailer/CD-estimated
        if not ver.get("trusted_bytes") and not ver.get("unmeasurable_bytes"):
            notes.append(
                "Deep verify: every archive in the container was "
                "stream-decompressed and measured exactly — the totals are "
                "measurements, not metadata estimates.")
        else:
            bits = []
            if ver.get("trusted_bytes"):
                bits.append(f"{ver['trusted_bytes'] / 1e9:.1f} GB across "
                            f"{ver['trusted_blobs']} blob(s) still rely on "
                            f"archive metadata (unstreamable entries or "
                            f"stream failures)")
            if ver.get("unmeasurable_bytes"):
                fmts = ", ".join(sorted(ver.get("unmeasurable_by_format",
                                                {})))
                bits.append(f"{ver['unmeasurable_bytes'] / 1e9:.1f} GB in "
                            f"{ver['unmeasurable_blobs']} blob(s) use "
                            f"formats with no measurable index"
                            + (f" ({fmts})" if fmts else "")
                            + ", counted at stored size")
            notes.append("Deep verify: archive sizes are stream-measured, "
                         "with a residual — " + "; ".join(bits) + ".")
    elif gzinfo is not None:
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
    pq = run.get("methods", {}).get("parquet", 0)
    if pq:
        notes.append(
            f"{pq:,} parquet file(s) sized from their footers: the "
            f"uncompressed total is decompressed page bytes (compression "
            f"codecs undone; dictionary/run-length encodings intact). A "
            f"warehouse's logical size for the same tables — e.g. the "
            f"BigQuery console, which the client likely declared from — is "
            f"generally higher, so compare like for like before calling a "
            f"shortfall.")
    badzip = run.get("errors", {}).get("by_type", {}).get("BadZipFile", 0)
    if badzip:
        notes.append(
            f"{badzip} BadZipFile error(s): corrupt or mislabeled .zip files "
            f"(common in scraped/backup trees), counted at stored size — "
            f"negligible impact.")
    return notes


UNDECLARED_NOTE_FLOOR = 1_000_000_000  # surface undeclared finds ≥1 GB only


def detection_notes(rows: list[dict], expected: dict | None,
                    run: dict | None, sources: dict | None = None) -> list[str]:
    """Notes from the sizer's embedded-service detection: declared services
    found inside other sources, and material undeclared discoveries."""
    if sources is None:
        sources = effective_sources(expected, run)
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
    tops = {norm(s) for s in sources} | {norm(_leaf(s)) for s in sources}
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


def equivalent_unit_notes(rows: list[dict]) -> list[str]:
    """A unit-adjusted % must never appear without its basis on the page."""
    notes = []
    for r in rows:
        if "equivalent_bytes" in r:
            notes.append(
                f"{r['service']}: the {r['equivalent_bytes'] / 1e12:.1f} TB "
                f"used for %-complete is the actual data re-expressed in the "
                f"manifest's own unit (measured "
                f"{r['actual_bytes'] / 1e12:.2f} TB as stored/page bytes). "
                + (r.get("equivalent_note") or ""))
    return notes


def duplicate_rollup(rows) -> dict:
    """Duplicated bytes from blob-index rows [(name, comp, unc), ...]:
    identical archives (same top-level source, same basename, same compressed
    AND uncompressed size) stored 2+ times — re-uploaded exports, `_cleanup`
    copies, the same attachment in several mailboxes. Copies beyond the first
    count as duplicated. The index carries only zip/gz rows (stored blobs are
    never cached), so this is a LOWER BOUND on real duplication.
    Returns {"bytes": total_dup_unc, "files": n, "by_source": {top: bytes}}."""
    seen: dict = {}
    for name, comp, unc in rows:
        top = name.split("/", 1)[0] if "/" in name else "(root)"
        key = (top, name.rsplit("/", 1)[-1], comp, unc)
        seen[key] = seen.get(key, 0) + 1
    dup_bytes, dup_files, by_source = 0, 0, {}
    for (top, _base, _comp, unc), count in seen.items():
        if count > 1:
            extra = (count - 1) * unc
            dup_bytes += extra
            dup_files += count - 1
            by_source[top] = by_source.get(top, 0) + extra
    return {"bytes": dup_bytes, "files": dup_files, "by_source": by_source}


DUP_NOTE_MIN_BYTES = 10_000_000_000  # only note duplication ≥10 GB


def duplicate_notes(root: Path, slug: str) -> list[str]:
    """Read the company's blob index and note significant duplication.
    No index (never sized, or deleted) → no note, never an error."""
    idx = common.company_dir(root, slug) / "blob-index.tsv.gz"
    if not idx.exists():
        return []
    import gzip
    rows = []
    with gzip.open(idx, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if len(f) >= 4:
                try:
                    rows.append((f[0], int(f[2]), int(f[3])))
                except ValueError:
                    continue
    dup = duplicate_rollup(rows)
    if dup["bytes"] < DUP_NOTE_MIN_BYTES:
        return []
    srcs = sorted(dup["by_source"].items(), key=lambda kv: -kv[1])
    where = ", ".join(f"{s} ({common.human_bytes(b)})" for s, b in srcs[:3]
                      if b >= DUP_NOTE_MIN_BYTES) or srcs[0][0]
    return [
        f"At least {common.human_bytes(dup['bytes'])} of the uncompressed "
        f"total is duplicated data: {dup['files']:,} archive "
        f"file{'s' if dup['files'] != 1 else ''} "
        f"stored two or more times (identical name and sizes) — mostly in {where}. "
        "Re-uploaded exports and *_cleanup copies are typical causes; the "
        "deduplicated corpus is correspondingly smaller. Lower bound: only "
        "zip/gz files are checked."]


def duplicate_sources(expected: dict | None, run: dict | None) -> dict:
    """Top-level prefixes that are redundant copies of data already counted
    under another prefix, declared in expected-data-sizes.json's
    "duplicate_prefixes" and verified against the container (not guessed).

    Why: some clients push the same export twice under different top-level
    names (saxon, 2026-08: 76 root folders are byte-exact copies of children
    inside sharepoint/). Left alone they inflate the headline total and show
    up as dozens of bogus "not in manifest" sources.

    An entry is a bare prefix string (the WHOLE prefix is redundant) or
    {"prefix": str, "redundant_bytes": int} when only part of it is (a
    partial/aborted export that still holds some unique data).
    Returns {prefix: redundant_uncompressed_bytes} for prefixes in the run.

    Matching runs against the POST-SPLIT per-source view (effective_sources),
    so a source_split company declares duplicates at their "parent/child"
    keys (swiftlaw, 2026-08: everything lives under gdrive-export2/, and the
    redundant copies are its second-level children). Companies without
    source_split are unaffected — effective keys ARE the top-level keys."""
    decl = (expected or {}).get("duplicate_prefixes") or []
    srcs = effective_sources(expected, run)
    out = {}
    for e in decl:
        name = e if isinstance(e, str) else e.get("prefix")
        if name not in srcs:
            continue
        full = srcs[name].get("uncompressed_bytes", 0)
        out[name] = full if isinstance(e, str) else min(
            e.get("redundant_bytes", full), full)
    return out


def apply_duplicates(sources: dict, dups: dict) -> dict:
    """Fold duplicate_sources() into the per-source view: wholly-redundant
    prefixes disappear; partially-redundant ones keep only their unique bytes
    (uncompressed_bytes minus the redundant share) and carry the subtraction
    in "deduplicated_bytes" so rows can be flagged. The declared-vs-received
    comparison then runs on deduplicated numbers everywhere, matching the
    deduplicated headline."""
    out = dict(sources)
    for p, b in dups.items():
        src = out.get(p)
        if src is None:
            continue
        unc = src.get("uncompressed_bytes", 0)
        if b >= unc:
            out.pop(p)               # wholly redundant: not a real source
        else:
            out[p] = {**src, "uncompressed_bytes": unc - b,
                      "deduplicated_bytes": b}
    return out


def duplicate_prefix_note(dups: dict, run: dict | None,
                          expected: dict | None = None) -> list[str]:
    """Explain the deduplication so the subtraction is never silent."""
    if not dups:
        return []
    srcs = effective_sources(expected, run)
    whole = [k for k, v in dups.items()
             if v >= srcs.get(k, {}).get("uncompressed_bytes", 0)]
    part = [k for k in dups if k not in whole]
    bits = []
    if whole:
        bits.append(f"{len(whole)} top-level folder"
                    f"{'s' if len(whole) != 1 else ''} "
                    f"({', '.join(sorted(whole)[:3])}"
                    f"{', …' if len(whole) > 3 else ''}) are byte-exact copies "
                    "of data already counted under another prefix")
    for k in sorted(part):
        bits.append(f"{common.human_bytes(dups[k])} of {k} duplicates another "
                    "prefix (the rest is unique and still counted)")
    return [f"{common.human_bytes(sum(dups.values()))} excluded as duplicate "
            f"data: {'; '.join(bits)}. Headline total and %-complete are "
            "deduplicated; per-source rows omit the redundant copies."]


def excluded_sources(expected: dict | None, run: dict | None) -> dict:
    """Top-level prefixes that are NON-CORPUS operational data — writes that
    land in the container but are not part of the client's corpus at all
    (bacancy, 2026-08: a portal-configured blob-inventory policy writes daily
    "which files landed" reports under 2026/). Declared in
    expected-data-sizes.json's "excluded_prefixes"; each entry is a bare
    prefix string or {"prefix": str, "reason": str}. The WHOLE prefix is
    dropped from the headline total, %-complete, and per-source rows.
    Distinct from duplicate_prefixes (redundant copies of real corpus data).
    Returns {prefix: (uncompressed_bytes, reason)} for prefixes in the run."""
    decl = (expected or {}).get("excluded_prefixes") or []
    srcs = (run or {}).get("sources", {})
    out = {}
    for e in decl:
        name = e if isinstance(e, str) else e.get("prefix")
        if name not in srcs:
            continue
        reason = ("non-corpus operational data" if isinstance(e, str)
                  else e.get("reason") or "non-corpus operational data")
        out[name] = (srcs[name].get("uncompressed_bytes", 0), reason)
    return out


def excluded_prefix_note(exc: dict) -> list[str]:
    """Explain the exclusion so the subtraction is never silent."""
    if not exc:
        return []
    total = sum(b for b, _ in exc.values())
    bits = [f"{k} ({common.human_bytes(b)}): {r}"
            for k, (b, r) in sorted(exc.items())]
    return [f"{common.human_bytes(total)} excluded as non-corpus data — "
            f"{'; '.join(bits)}. Not counted in the headline total, "
            "%-complete, or per-source rows."]


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
    unc_raw = latest["totals"]["uncompressed_bytes"] if latest else None
    dup_total = sum(duplicate_sources(expected, latest).values())
    excluded = excluded_sources(expected, latest)
    exc_total = sum(b for b, _ in excluded.values())
    unc_total = (unc_raw - dup_total - exc_total) if unc_raw is not None else None
    pct = (unc_total / manifest_total * 100
           if latest and manifest_total else None)
    remaining = (max(manifest_total - unc_total, 0)
                 if latest and manifest_total else None)
    rate, eta = _rate_and_eta(latest, prev, remaining) if latest else (None, None)

    sources = apply_duplicates(effective_sources(expected, latest),
                               duplicate_sources(expected, latest))
    for _p in excluded:
        sources.pop(_p, None)        # non-corpus: not a source at all
    rows, unexpected = service_rows(expected, latest, sources)

    now = common.utc_now()
    last_change = status.get("last_change_detected_at")
    days_since_change = ((now - common.parse_iso(last_change)).total_seconds() / 86400
                         if last_change else None)
    stalled = bool(latest and pct is not None and pct < 100
                   and days_since_change is not None and days_since_change >= 3)

    # deep-verify state: the LATEST run's coverage block feeds numbers; the
    # newest run carrying one (any age) dates the last certification, so
    # "deep-verified <date>" survives later shallow runs
    verification = (latest or {}).get("verification")
    deep_verified_at = next(
        (r["timestamp"] for r in common.latest_runs(root, slug, 30)
         if r.get("verification")), None)

    return {
        "slug": slug,
        "config": cfg,
        "expected": expected,
        "status": status,
        "latest_run": latest,
        "prev_run": prev,
        "manifest_total_bytes": manifest_total,
        "uncompressed_total": unc_total,
        "uncompressed_total_raw": unc_raw,
        "duplicate_bytes": dup_total,
        "excluded_bytes": exc_total,
        "pct_complete": pct,
        "remaining_bytes": remaining,
        "rate_bytes_per_day": rate,
        "eta": eta,
        "delta_24h": delta_24h(root, slug),
        "stalled": stalled,
        "days_since_change": days_since_change,
        "verification": verification,
        "deep_verified_at": deep_verified_at,
        "service_rows": rows,
        "unexpected_sources": unexpected,
        # post-split view; consumers render per-source numbers from THIS, not
        # from latest_run["sources"], or split children go missing
        "sources": sources,
        "notes": duplicate_prefix_note(duplicate_sources(expected, latest),
                                       latest, expected)
                 + excluded_prefix_note(excluded)
                 + lore_notes(latest) + detection_notes(rows, expected, latest,
                                                        sources)
                 + equivalent_unit_notes(rows)
                 + duplicate_notes(root, slug),
        "expected_confirmed": bool((expected or {}).get("confirmed_by_user")),
    }
