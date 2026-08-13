#!/usr/bin/env python3
"""discover_company.py — Azure discovery for onboarding. Writes config.json.

Discovery logic (from SIZING-SKILL.md Steps 0–1):
  SA:        first storage account whose resource group name contains the slug
  container: <slug>-raw (ignore -scrubbed and insights-logs-*); warn if absent
  VM:        prefer verify-vm-*, else ANY VM in the SA's RG, else exists:false.
             INFORMATIONAL ONLY — sizing runs locally on this machine; the vm
             block is cached context (most companies have no VM, which is fine)

Idempotent: re-running refreshes the cached discovery, preserving onboarded_at.
Prints the resulting config as JSON on stdout; exits nonzero on failure
(e.g. no storage account matches the slug).

  discover_company.py <slug> [--root companies] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import common
from common import HarnessError


def discover(slug: str, dry_run: bool = False) -> dict:
    common.ensure_subscription(dry_run=dry_run)
    sub_id = ""
    if not dry_run:
        sub_id = common.run_az(["account", "show", "--query", "id", "-o", "tsv"]
                               ).stdout.strip()

    sa = common.run_az([
        "storage", "account", "list",
        "--query", f"[?contains(resourceGroup,'{slug}')].name | [0]", "-o", "tsv",
    ], dry_run=dry_run).stdout.strip()
    if not dry_run and not sa:
        raise HarnessError(
            f"no storage account found with resource group containing '{slug}' "
            f"in subscription '{common.SUBSCRIPTION}' — check the slug, or the "
            f"company may not be provisioned yet")
    rg = common.run_az([
        "storage", "account", "show", "-n", sa or "<sa>",
        "--query", "resourceGroup", "-o", "tsv",
    ], dry_run=dry_run).stdout.strip()

    container = f"{slug}-raw"
    containers = common.az_json([
        "rest", "--method", "get", "--url",
        f"https://management.azure.com/subscriptions/{sub_id}/resourceGroups/{rg}"
        f"/providers/Microsoft.Storage/storageAccounts/{sa}"
        f"/blobServices/default/containers?api-version=2023-05-01",
        "--query", "value[].name",
    ], dry_run=dry_run) or []
    container_warning = None
    if not dry_run and container not in containers:
        interesting = [c for c in containers
                       if not c.endswith("-scrubbed")
                       and not c.startswith("insights-logs-")]
        container_warning = (f"expected container '{container}' not found; "
                             f"account has: {interesting or containers}")

    # VM discovery: verify-vm-* preferred, else any VM in the RG
    vm_name = common.run_az([
        "vm", "list", "-g", rg or "<rg>",
        "--query", "[?starts_with(name,'verify-vm-')].name | [0]", "-o", "tsv",
    ], dry_run=dry_run).stdout.strip()
    if not vm_name and not dry_run:
        vm_name = common.run_az([
            "vm", "list", "-g", rg, "--query", "[0].name", "-o", "tsv",
        ]).stdout.strip()

    cfg = {
        "slug": slug,
        "subscription": common.SUBSCRIPTION,
        "subscription_id": sub_id,
        "resource_group": rg,
        "storage_account": sa,
        "container": container,
        "vm": {"name": vm_name or None, "resource_group": rg,
               "exists": bool(vm_name)},
        "onboarded_at": common.iso_now(),
    }
    if container_warning:
        cfg["container_warning"] = container_warning
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("slug")
    ap.add_argument("--root", type=Path, default=common.DEFAULT_COMPANIES_ROOT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        print(f"invalid slug '{args.slug}' (lowercase alphanumerics and hyphens)",
              file=sys.stderr)
        return 2
    try:
        cfg = discover(args.slug, dry_run=args.dry_run)
    except HarnessError as exc:
        print(json.dumps({"slug": args.slug, "outcome": "failed",
                          "reason": str(exc)}))
        return 1

    path = common.company_dir(args.root, args.slug) / "config.json"
    if path.exists():  # refresh discovery but keep the original onboarding time
        cfg["onboarded_at"] = common.read_json(path).get(
            "onboarded_at", cfg["onboarded_at"])
    if not args.dry_run:
        common.write_json(path, cfg)
        (common.company_dir(args.root, args.slug) / "sizing-runs").mkdir(exist_ok=True)
        (common.company_dir(args.root, args.slug) / "reports").mkdir(exist_ok=True)
    print(json.dumps(cfg, indent=2))
    if not cfg["vm"]["exists"]:
        print(f"note: no VM in {cfg['resource_group']} (informational — "
              f"sizing runs locally and does not need one)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
