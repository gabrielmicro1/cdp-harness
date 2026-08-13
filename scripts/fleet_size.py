#!/usr/bin/env python3
"""fleet_size.py — fleet-wide sizing orchestrator.

Runs fleet-wide PHASES, not per-company loops: skip-check everyone (parallel
ARM reads), launch all non-skipped as detached LOCAL sizer processes
(sequential launches; the sizers run concurrently — total wall time ≈
slowest company), poll their .done files/pids, then harvest. One broken
company never kills the run: every company gets an outcome
(sized | skipped-unchanged | failed) and the script exits nonzero only after
processing everyone. Detached sizers survive the harness dying; their work
files are the rescue path.

Subcommands:
  launch-all   phase 0 (skip check) + phase 1 (launch)
  poll-all     phase 2; bounded by --max-wait, resumable across invocations
  harvest      phase 3; writes run files, updates status, cleans up
  run          launch-all + poll-all + harvest (for small fleets / one sitting)
  status       print current in-flight state

State lives in <root>/.fleet-state.json so poll/harvest resume across agent
turns (each Bash call is bounded by a 10-minute timeout — poll repeatedly).

--dry-run prints the az commands instead of executing them.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import common
import phases
from common import HarnessError

POLL_INTERVAL_S = 30


def cmd_launch_all(args) -> dict:
    root = args.root
    common.ensure_subscription(dry_run=args.dry_run)
    slugs = args.slugs or common.list_companies(root)
    state = phases.load_state(root)
    # Drop settled entries from previous runs; keep anything still in flight
    # (a size_company launch must not clobber an in-flight fleet run).
    state["companies"] = {s: c for s, c in state["companies"].items()
                          if c.get("phase") in ("launched", "terminal")
                          and s not in slugs}
    state["started_at"] = common.iso_now()

    def check(slug):
        try:
            cfg = common.load_config(root, slug)
            return slug, cfg, phases.skip_check(root, slug, cfg, dry_run=args.dry_run), None
        except Exception as exc:  # noqa: BLE001 — isolation: one company ≠ the fleet
            return slug, None, None, str(exc)

    # Phase 0 — parallel ARM metric reads
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = list(pool.map(check, slugs))

    to_launch = []
    for slug, cfg, skip_info, err in checks:
        comp = {"phase": None, "metric": None, "metric_at": None,
                "outcome": None, "reason": None}
        state["companies"][slug] = comp
        if err is not None:
            comp.update(phase="done", outcome="failed", reason=f"skip-check: {err}")
            continue
        comp.update(metric=skip_info["metric"], metric_at=skip_info["metric_at"])
        if skip_info["skip"]:
            try:
                phases.write_copied_forward_run(root, slug, skip_info["metric"],
                                                skip_info["metric_at"])
                phases.update_status(root, slug, "skipped-unchanged",
                                     skip_info["reason"])
                comp.update(phase="done", outcome="skipped-unchanged",
                            reason=skip_info["reason"])
            except Exception as exc:  # noqa: BLE001
                comp.update(phase="done", outcome="failed",
                            reason=f"copy-forward: {exc}")
        else:
            to_launch.append((slug, cfg))

    # Phase 1 — sequential launches (a launch is a few ARM reads + spawning a
    # detached process; the +60s propagation sleep only happens for SAs whose
    # firewall needed our IP added)
    for slug, cfg in to_launch:
        comp = state["companies"][slug]
        try:
            launched = phases.launch(root, slug, cfg, dry_run=args.dry_run)
            comp.update(launched)
        except HarnessError as exc:
            phases.update_status(root, slug, "failed", str(exc))
            comp.update(phase="done", outcome="failed", reason=str(exc))
        except Exception as exc:  # noqa: BLE001
            phases.update_status(root, slug, "failed", f"launch: {exc}")
            comp.update(phase="done", outcome="failed", reason=f"launch: {exc}")
        phases.save_state(root, state)

    phases.save_state(root, state)
    return state


def _in_flight(state: dict) -> list[str]:
    return [s for s, c in state["companies"].items() if c.get("phase") == "launched"]


def cmd_poll_all(args) -> dict:
    root = args.root
    state = phases.load_state(root)
    deadline = time.time() + args.max_wait
    while True:
        pending = _in_flight(state)
        if not pending:
            break
        for slug in pending:  # local .done/pid checks — instant, no az calls
            comp = state["companies"][slug]
            res = phases.poll_one(root, slug, comp, dry_run=args.dry_run)
            comp["exec_state"] = res["state"]
            if res["state"] == "Failed":
                comp["exec_error"] = res.get("err", "")
            if res["state"] in phases.TERMINAL_STATES:
                comp["phase"] = "terminal"
        phases.save_state(root, state)
        pending = _in_flight(state)
        done = len(state["companies"]) - len(pending)
        print(f"poll: {done}/{len(state['companies'])} settled; "
              f"in-flight: {pending or 'none'}", file=sys.stderr)
        if not pending or args.dry_run or time.time() + POLL_INTERVAL_S > deadline:
            break
        time.sleep(POLL_INTERVAL_S)
    return state


def cmd_harvest(args) -> dict:
    root = args.root
    state = phases.load_state(root)
    for slug, comp in state["companies"].items():
        if comp.get("phase") != "terminal":
            if comp.get("phase") == "launched":
                comp.update(outcome=comp.get("outcome") or "failed",
                            reason="still in flight — poll-all until terminal, "
                                   "then harvest again")
            continue
        try:
            cfg = common.load_config(root, slug)
            phases.harvest_one(root, slug, cfg, comp, dry_run=args.dry_run)
            phases.update_status(root, slug, "sized")
            comp.update(phase="done", outcome="sized", reason=None)
        except Exception as exc:  # noqa: BLE001
            try:
                phases.update_status(root, slug, "failed", str(exc))
            except Exception:  # noqa: BLE001
                pass
            comp.update(phase="done", outcome="failed", reason=str(exc))
        phases.save_state(root, state)
    return state


def cmd_run(args) -> dict:
    cmd_launch_all(args)
    state = cmd_poll_all(args)
    if _in_flight(state) and not args.dry_run:
        print("run: some companies still in flight after --max-wait; "
              "re-run `poll-all` then `harvest`", file=sys.stderr)
        return state
    return cmd_harvest(args)


def print_outcomes(state: dict) -> int:
    outcomes = {s: {"outcome": c.get("outcome"), "reason": c.get("reason"),
                    "exec_state": c.get("exec_state")}
                for s, c in state["companies"].items()}
    print(json.dumps({"started_at": state.get("started_at"),
                      "outcomes": outcomes}, indent=2))
    return 1 if any(c.get("outcome") == "failed"
                    for c in state["companies"].values()) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["launch-all", "poll-all", "harvest",
                                        "run", "status"])
    ap.add_argument("--root", type=Path, default=common.DEFAULT_COMPANIES_ROOT,
                    help="companies root (fixtures for tests)")
    ap.add_argument("--slugs", nargs="*", default=None,
                    help="subset of companies (default: all onboarded)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print az commands instead of running them")
    ap.add_argument("--max-wait", type=int, default=480,
                    help="poll-all/run: seconds to keep polling before returning "
                         "(keep under the 10-min Bash timeout; resumable)")
    args = ap.parse_args()
    fn = {"launch-all": cmd_launch_all, "poll-all": cmd_poll_all,
          "harvest": cmd_harvest, "run": cmd_run,
          "status": lambda a: phases.load_state(a.root)}[args.command]
    state = fn(args)
    return print_outcomes(state)


if __name__ == "__main__":
    sys.exit(main())
