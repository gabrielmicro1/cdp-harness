#!/usr/bin/env python3
"""size_company.py — size ONE company: a fleet of one.

Thin wrapper over the exact same phase functions fleet_size.py uses (via the
fleet CLI with --slugs), so single-company and fleet behavior can never drift.

  size_company.py <slug>                # skip-check → launch → poll → harvest
  size_company.py <slug> --phase launch # phases individually (resumable)
  size_company.py <slug> --phase poll --max-wait 480
  size_company.py <slug> --phase harvest
  size_company.py <slug> --dry-run

Each invocation is bounded (default --max-wait 480s, under the 10-minute Bash
timeout). The sizer runs locally as a detached process, so it survives between
invocations. For huge containers (blob COUNT drives runtime; ~3–4k blobs/sec
in-region, slower over the internet — webspiders' 10.9M blobs is hours, not
minutes, locally) run `--phase poll` repeatedly across turns, then
`--phase harvest`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import common
import fleet_size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("slug")
    ap.add_argument("--phase", choices=["all", "launch", "poll", "harvest"],
                    default="all")
    ap.add_argument("--root", type=Path, default=common.DEFAULT_COMPANIES_ROOT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the per-company blob index (full re-size)")
    ap.add_argument("--force", action="store_true",
                    help="bypass the UsedCapacity skip check and size anyway "
                         "(for re-sizing an unchanged container under new "
                         "sizer settings, e.g. GZ_STREAM_* knobs)")
    ap.add_argument("--max-wait", type=int, default=480)
    args = ap.parse_args()
    args.slugs = [args.slug]

    if args.phase == "all":
        state = fleet_size.cmd_run(args)
    elif args.phase == "launch":
        state = fleet_size.cmd_launch_all(args)
    elif args.phase == "poll":
        state = fleet_size.cmd_poll_all(args)
    else:
        state = fleet_size.cmd_harvest(args)
    return fleet_size.print_outcomes(state)


if __name__ == "__main__":
    sys.exit(main())
