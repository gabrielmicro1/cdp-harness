#!/usr/bin/env python3
"""ONE-OFF: seed knowledge/kickoff-copy/ from the corpus-transfer-slack-operator
plugin's support-cases catalog, and import its live channel registrations into
companies/<slug>/slack/channel.json.

    python3 scripts/import_slack_operator.py seed --catalog <support-cases.json> [--overwrite]
    python3 scripts/import_slack_operator.py registrations \
        --registrations ~/.corpus-transfer-slack-operator/registrations.json \
        --map sentient-decision-science=sentient --map gr0=gr0 ...

Only the plugin's DEFAULT case per service is imported (kickoff_draft joined by
newline; recommendation + numbered steps when kickoff_draft is empty). Every
imported entry is marked approved: FDE-authored copy counts as reviewed. Em
dashes are rewritten to commas and route lines ("You push it" / "We pull it")
are normalized to bold bullets so entries pass the library's load-time gate.
Registrations are imported with empty high-water marks so the first read is a
full read; plugins slugs without a companies/<slug>/ directory are skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import common
import slack_engine as se


def _message_from_case(case: dict) -> str:
    lines = [str(x) for x in case.get("kickoff_draft") or []]
    if any(l.strip() for l in lines):
        text = "\n".join(lines).strip()
    else:
        parts = [case.get("recommendation", "").strip()]
        steps = [str(s) for s in case.get("steps") or []]
        if steps:
            parts.append("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
        text = "\n\n".join(p for p in parts if p).strip()
    text = text.replace(" — ", ", ").replace("—", ", ")
    return se.normalize_route_sections(text)


def seed_library(catalog_path: Path, library_dir: Path, *, overwrite: bool = False) -> dict:
    catalog = common.read_json(Path(catalog_path))
    cases = {c["case_id"]: c for c in catalog.get("cases", [])}
    version = catalog.get("catalog_version", "unknown")
    library_dir = Path(library_dir)
    library_dir.mkdir(parents=True, exist_ok=True)
    written, skipped, failed = [], [], {}
    for svc in catalog.get("services", []):
        sid = svc["service_id"]
        target = library_dir / f"{sid}.json"
        if target.exists() and not overwrite:
            skipped.append(sid)
            continue
        case = cases.get(svc.get("default_case_id"))
        if case is None:
            failed[sid] = "default case not found"
            continue
        message = _message_from_case(case)
        if not message:
            failed[sid] = "default case renders to an empty message"
            continue
        entry = {
            "service_id": sid,
            "display_name": svc.get("display_name") or sid,
            "aliases": [str(a) for a in svc.get("aliases") or []],
            "direction": case.get("direction") if case.get("direction") in ("pull", "push", "adaptive") else None,
            "status": "approved",
            "message": message,
            "notes": [str(x) for x in case.get("limitations") or []],
            "source": f"imported from cdp-corpus-transfer-plugin support-cases {version} "
                      f"(case {case['case_id']})",
            "updated_at": common.iso_now(),
        }
        try:
            se.check_message_text(entry["message"], sid)
        except se.SlackEngineError as exc:
            failed[sid] = str(exc)
            continue
        common.write_json(target, entry)
        written.append(sid)
    return {"catalog_version": version, "written": written,
            "skipped_existing": skipped, "failed": failed}


def import_registrations(registrations_path: Path, root: Path, *, mapping: dict[str, str],
                         library: dict | None = None) -> dict:
    raw = common.read_json(Path(registrations_path))
    library = library if library is not None else se.load_library()
    imported, already, skipped, failed = [], [], [], {}
    for reg in raw.get("registrations", {}).values():
        plugin_slug = reg["company_slug"]
        slug = mapping.get(plugin_slug, plugin_slug)
        if not common.company_dir(root, slug).is_dir():
            skipped.append(plugin_slug)
            continue
        if se.channel_path(root, slug).exists():
            already.append(slug)
            continue
        try:
            new = se.register(root, slug, channel_url=reg["channel_url"],
                              owner_user_id=reg["owner_fde_id"],
                              company_name=reg["company_name"],
                              channel_type=reg.get("channel_type", "private"))
            canvas = reg.get("instructions_canvas")
            if canvas:
                se.record_canvas(root, slug, canvas_id=canvas["canvas_id"],
                                 title=canvas["title"], permalink=canvas["permalink"])
                new = se.load_channel(root, slug)
            parents = []
            for p in reg.get("parents", []):
                parsed = se.parse_parent_label(p["title"])
                if not parsed:
                    continue
                kind, label = parsed
                parents.append({"ts": p["parent_message_ts"], "kind": kind, "label": label,
                                "service_id": None,
                                "author_user_id": p["workflow_author_id"], "ambiguous": False})
            new["parents"] = sorted(parents, key=lambda p: se.ts_key(p["ts"]))
            se._assign_service_ids(new["parents"], library)
            se.save_channel(root, slug, new)
            imported.append(slug)
        except se.SlackEngineError as exc:
            failed[slug] = str(exc)
    return {"imported": imported, "already_registered": already,
            "skipped_no_company": skipped, "failed": failed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="seed knowledge/kickoff-copy/ from a plugin catalog")
    s.add_argument("--catalog", required=True)
    s.add_argument("--library", default=str(se.LIBRARY_DIR))
    s.add_argument("--overwrite", action="store_true")
    r = sub.add_parser("registrations", help="import plugin registrations as channel.json")
    r.add_argument("--registrations", required=True)
    r.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    r.add_argument("--map", action="append", default=[], metavar="PLUGIN_SLUG=HARNESS_SLUG")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "seed":
            out = seed_library(Path(args.catalog), Path(args.library), overwrite=args.overwrite)
        else:
            mapping = {}
            for item in args.map:
                if "=" not in item:
                    raise se.SlackEngineError(f"--map expects PLUGIN_SLUG=HARNESS_SLUG, got {item!r}")
                a, b = item.split("=", 1)
                mapping[a.strip()] = b.strip()
            out = import_registrations(Path(args.registrations), Path(args.root), mapping=mapping)
    except se.SlackEngineError as exc:
        print(json.dumps({"error": str(exc), "error_type": "contract_error"}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 1 if out.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
