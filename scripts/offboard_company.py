#!/usr/bin/env python3
"""offboard_company.py — archive a company out of the active fleet (and back).

Offboarding is LOCAL bookkeeping only: it moves companies/<slug>/ to
companies/.archive/<slug>/ — a dot-prefixed dir that common.list_companies()
skips — so the company disappears from the dashboard and every fleet loop
(update-all, report-all, daily-brief) while ALL of its state survives intact
for a later restore: config.json, expected-data-sizes.json, status.json, the
append-only sizing-runs/ history, reports, and the blob-index.tsv.gz cache
that seeds an incremental re-size. Azure is never touched — the storage
account, container, and client data all remain exactly as they are.

  offboard_company.py offboard <slug> [--root companies] [--dry-run] [--force]
  offboard_company.py restore  <slug> [--root companies] [--dry-run]
  offboard_company.py list     [--root companies]

offboard stamps "offboarded_at" into status.json (restore removes it) and
refuses while the slug is mid-flight in .fleet-state.json (phase "launched" —
poll/harvest first so the run isn't orphaned, or --force). Both moves are
idempotent: re-running reports already-offboarded / already-active at rc 0.
Exit 0 = done (or already in the requested state); 1 = error, nothing moved.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import common
import phases


def archive_dir(root: Path) -> Path:
    return root / ".archive"


def fail(slug: str | None, reason: str) -> int:
    print(json.dumps({"slug": slug, "outcome": "failed", "reason": reason},
                     indent=2))
    return 1


def stale_work_files(root: Path, slug: str) -> list[str]:
    wd = phases.work_dir(root)
    if not wd.is_dir():
        return []
    return sorted(p.name for p in wd.glob(f"{phases.tag_for(slug)}.*"))


def offboard(root: Path, slug: str, dry_run: bool, force: bool) -> int:
    src = common.company_dir(root, slug)
    dst = archive_dir(root) / slug
    if not src.is_dir():
        if dst.is_dir():
            print(json.dumps({"slug": slug, "outcome": "already-offboarded",
                              "archived_at": str(dst)}, indent=2))
            return 0
        return fail(slug, f"no such company: {src} does not exist")
    if dst.exists():
        return fail(slug, f"{dst} already exists — resolve the collision "
                          "manually before offboarding")

    comp_state = phases.load_state(root)["companies"].get(slug) or {}
    if comp_state.get("phase") == "launched" and not force:
        return fail(slug, "a sizing run is in flight (.fleet-state.json phase "
                          "'launched') — poll/harvest it first, or --force")

    stale = stale_work_files(root, slug)
    out = {"slug": slug, "outcome": "offboarded", "archived_to": str(dst),
           "stale_work_files": stale}
    if dry_run:
        out["dry_run"] = True
        print(json.dumps(out, indent=2))
        return 0

    status = common.load_status(root, slug)
    status["offboarded_at"] = common.iso_now()
    common.save_status(root, slug, status)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    out["offboarded_at"] = status["offboarded_at"]
    print(json.dumps(out, indent=2))
    return 0


def restore(root: Path, slug: str, dry_run: bool) -> int:
    src = archive_dir(root) / slug
    dst = common.company_dir(root, slug)
    if not src.is_dir():
        if dst.is_dir():
            print(json.dumps({"slug": slug, "outcome": "already-active"},
                             indent=2))
            return 0
        return fail(slug, f"no such archived company: {src} does not exist")
    if dst.exists():
        return fail(slug, f"{dst} already exists — resolve the collision "
                          "manually before restoring")

    out = {"slug": slug, "outcome": "restored", "restored_to": str(dst)}
    if dry_run:
        out["dry_run"] = True
        print(json.dumps(out, indent=2))
        return 0

    shutil.move(str(src), str(dst))
    status = common.load_status(root, slug)
    status.pop("offboarded_at", None)
    common.save_status(root, slug, status)
    print(json.dumps(out, indent=2))
    return 0


def list_archived(root: Path) -> int:
    ad = archive_dir(root)
    rows = []
    if ad.is_dir():
        for d in sorted(p for p in ad.iterdir() if p.is_dir()):
            status = {}
            sp = d / "status.json"
            if sp.exists():
                status = common.read_json(sp)
            rows.append({"slug": d.name,
                         "offboarded_at": status.get("offboarded_at"),
                         "stage": status.get("stage"),
                         "sizing_runs": len(list((d / "sizing-runs")
                                                 .glob("*.json"))
                                           if (d / "sizing-runs").is_dir()
                                           else [])})
    print(json.dumps({"archived": rows}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_off = sub.add_parser("offboard", help="archive a company out of the fleet")
    p_off.add_argument("slug")
    p_off.add_argument("--force", action="store_true",
                       help="offboard even with an in-flight sizing run")
    p_res = sub.add_parser("restore", help="bring an archived company back")
    p_res.add_argument("slug")
    p_ls = sub.add_parser("list", help="list archived companies")
    for p in (p_off, p_res, p_ls):
        p.add_argument("--root", type=Path,
                       default=common.DEFAULT_COMPANIES_ROOT)
    for p in (p_off, p_res):
        p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.cmd == "offboard":
        return offboard(args.root, args.slug, args.dry_run, args.force)
    if args.cmd == "restore":
        return restore(args.root, args.slug, args.dry_run)
    return list_archived(args.root)


if __name__ == "__main__":
    sys.exit(main())
