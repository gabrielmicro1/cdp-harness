#!/usr/bin/env python3
"""verify_completion.py — completion checklist for a company that looks done.

Checks (tolerance is a parameter, default 0.98 = within 98%):
  headline    total uncompressed ≥ tolerance × manifest headline total
  services    every byte-declared (>0) service present and ≥ tolerance × declared
  zero_bytes  zero-byte blob count from the latest run (unknown on old runs)
  unexpected  sources present that aren't in the manifest
  errors      sizing-run error counts surfaced for review (never auto-fails)
  ambiguity   record-count services (not byte-comparable) and overshoots
              (>100% — often a wrong manifest, commercially significant) are
              listed for HUMAN judgment; the script does not resolve them.

Exit 0 = all hard checks pass; 1 = at least one fails; 2 = cannot verify.
--mark-complete sets stage: complete (only when passing).

  verify_completion.py <slug> [--tolerance 0.98] [--root companies] [--mark-complete]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import common
import reconcile


def verify(root: Path, slug: str, tolerance: float) -> dict:
    s = reconcile.company_summary(root, slug)
    checks = {}
    if s["latest_run"] is None:
        return {"slug": slug, "verdict": "cannot-verify",
                "reason": "no sizing runs"}
    if not s["expected_confirmed"]:
        return {"slug": slug, "verdict": "cannot-verify",
                "reason": "expected-data-sizes.json not confirmed by user"}

    total = s["manifest_total_bytes"]
    unc = s["uncompressed_total"]
    checks["headline"] = {
        "pass": bool(total and unc >= tolerance * total),
        "declared": total, "actual": unc,
        "pct": round(unc / total * 100, 2) if total else None,
        "tolerance_pct": tolerance * 100,
    }

    failing, passing, records, overshoot = [], [], [], []
    for r in s["service_rows"]:
        if r["declared_records"] is not None:
            records.append({"service": r["service"],
                            "records": r["declared_records"],
                            "actual_bytes": r["actual_bytes"]})
            continue
        if not r["declared_bytes"]:
            continue  # 0 B declarations: surfaced via flags, not a hard check
        entry = {"service": r["service"], "declared": r["declared_bytes"],
                 "actual": r["actual_bytes"],
                 "pct": round(r["pct"], 2) if r["pct"] is not None else None}
        if r["actual_bytes"] >= tolerance * r["declared_bytes"]:
            passing.append(entry)
            if r["pct"] and r["pct"] > 100:
                overshoot.append(entry)
        else:
            failing.append(entry)
    checks["services"] = {"pass": not failing, "failing": failing,
                          "passing_count": len(passing)}

    zb = s["latest_run"]["totals"].get("zero_byte_blobs")
    checks["zero_bytes"] = {
        "pass": zb == 0 if zb is not None else None,
        "count": zb,
        "note": (None if zb is not None else
                 "run predates zero-byte tracking — re-size to populate"),
    }

    checks["unexpected_sources"] = {
        "pass": not s["unexpected_sources"],
        "sources": s["unexpected_sources"],
    }

    errs = s["latest_run"].get("errors", {})
    checks["errors"] = {"pass": True, "total": errs.get("total", 0),
                        "by_type": errs.get("by_type", {}),
                        "note": "review only — never auto-fails"}

    hard = [checks["headline"]["pass"], checks["services"]["pass"],
            checks["zero_bytes"]["pass"] is not False,
            checks["unexpected_sources"]["pass"]]
    return {
        "slug": slug,
        "verdict": "pass" if all(hard) else "fail",
        "tolerance": tolerance,
        "checks": checks,
        "needs_judgment": {
            "record_count_services": records,
            "overshoot": overshoot,
            "zero_declared_with_data": [
                {"service": r["service"], "actual": r["actual_bytes"]}
                for r in s["service_rows"]
                if "zero-declared-has-data" in r["flags"]],
        },
        "notes": s["notes"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("slug")
    ap.add_argument("--tolerance", type=float, default=0.98)
    ap.add_argument("--root", type=Path, default=common.DEFAULT_COMPANIES_ROOT)
    ap.add_argument("--mark-complete", action="store_true",
                    help="on pass: set stage complete")
    args = ap.parse_args()
    try:
        result = verify(args.root, args.slug, args.tolerance)
    except FileNotFoundError as exc:
        print(json.dumps({"slug": args.slug, "verdict": "cannot-verify",
                          "reason": str(exc)}))
        return 2
    if args.mark_complete and result["verdict"] == "pass":
        status = common.load_status(args.root, args.slug)
        status["stage"] = "complete"
        common.save_status(args.root, args.slug, status)
        result["stage_set"] = "complete"
    print(json.dumps(result, indent=2))
    return {"pass": 0, "fail": 1}.get(result["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
