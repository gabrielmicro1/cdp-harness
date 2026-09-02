#!/usr/bin/env python3
"""Offline tests for scripts/import_slack_operator.py (one-off seed/import)."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import import_slack_operator as imp  # noqa: E402
import slack_engine as se  # noqa: E402

CATALOG = {
    "catalog_version": "2026-09-01.1",
    "services": [
        {"service_id": "zoom", "display_name": "Zoom", "aliases": ["zoom recordings"],
         "default_case_id": "zoom-recordings-pull", "case_ids": ["zoom-recordings-pull"]},
        {"service_id": "figma", "display_name": "Figma", "aliases": [],
         "default_case_id": "figma-explicit-files-push",
         "case_ids": ["figma-explicit-files-push", "figma-api-pull"]},
    ],
    "cases": [
        {"case_id": "zoom-recordings-pull", "service_id": "zoom", "direction": "pull",
         "status": "approved_default",
         "kickoff_draft": ["Yes, we can pull Zoom for {company_name}.", "", "Please:",
                           "1. Create the app."],
         "recommendation": "Pull via S2S app.", "confirm": [], "steps": [],
         "platform_submission": [], "slack_reply": [], "verification": [], "cleanup": [],
         "alternative_note": "", "limitations": ["Manual download only for small sets."]},
        {"case_id": "figma-explicit-files-push", "service_id": "figma", "direction": "push",
         "status": "approved_default", "kickoff_draft": [],
         "recommendation": "Export the files explicitly.",
         "confirm": [], "steps": ["Open Figma.", "Export each file."],
         "platform_submission": ["Upload via client_push.sh."], "slack_reply": [],
         "verification": [], "cleanup": [], "alternative_note": "", "limitations": []},
        {"case_id": "figma-api-pull", "service_id": "figma", "direction": "pull",
         "status": "seen_case", "kickoff_draft": ["never used"], "recommendation": "",
         "confirm": [], "steps": [], "platform_submission": [], "slack_reply": [],
         "verification": [], "cleanup": [], "alternative_note": "", "limitations": []},
    ],
    "common_responses": [], "review_services": [],
}

REGISTRATIONS = {"version": 2, "registrations": {
    "ktf_aaaa": {
        "kickoff_id": "ktf_aaaa", "company_name": "Sentient Decision Science",
        "company_slug": "sentient-decision-science", "workspace_domain": "micro1-companies",
        "channel_id": "C0SENT00001",
        "channel_url": "https://micro1-companies.slack.com/archives/C0SENT00001",
        "owner_fde_id": "U0OWNER0001", "channel_type": "slack_connect",
        "monitoring_scope": "full_channel",
        "instructions_canvas": {"canvas_id": "F0SENTCANV", "title": "EDP Instructions",
                                "permalink": "https://micro1-companies.slack.com/docs/T0B8/F0SENTCANV",
                                "discovered_at": "2026-08-31T00:00:00+00:00"},
        "parents": [
            {"item_id": "onboarding", "kind": "onboarding", "display_name": "Onboarding",
             "title": "[Onboarding]", "workflow_name": "Company Transfer Setup",
             "workflow_author_id": "U0BT0NY588Y", "parent_message_ts": "100.000001",
             "discovered_at": "2026-08-31T00:00:00+00:00"},
            {"item_id": "service:zoom", "kind": "service", "display_name": "Zoom",
             "title": "[Zoom]", "workflow_name": "Company Transfer Setup",
             "workflow_author_id": "U0BT0NY588Y", "parent_message_ts": "101.000001",
             "discovered_at": "2026-08-31T00:00:00+00:00"},
        ],
        "created_at": "2026-08-31T00:00:00+00:00", "updated_at": "2026-08-31T00:00:00+00:00"},
    "ktf_bbbb": {
        "kickoff_id": "ktf_bbbb", "company_name": "Wiza", "company_slug": "wiza",
        "workspace_domain": "micro1-companies", "channel_id": "C0WIZA00001",
        "channel_url": "https://micro1-companies.slack.com/archives/C0WIZA00001",
        "owner_fde_id": "U0OWNER0001", "channel_type": "slack_connect",
        "monitoring_scope": "full_channel", "instructions_canvas": None, "parents": [],
        "created_at": "2026-08-31T00:00:00+00:00", "updated_at": "2026-08-31T00:00:00+00:00"},
}}


class ImportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cdp-slack-import-"))
        self.catalog = self.tmp / "support-cases.json"
        self.catalog.write_text(json.dumps(CATALOG))
        self.regs = self.tmp / "registrations.json"
        self.regs.write_text(json.dumps(REGISTRATIONS))
        self.root = self.tmp / "companies"
        shutil.copytree(REPO / "tests" / "fixtures" / "companies", self.root)
        self.lib = self.tmp / "kickoff-copy"

    def tearDown(self):
        shutil.rmtree(self.tmp)


class CatalogSeedTests(ImportCase):
    def test_seed_writes_default_case_per_service(self):
        r = imp.seed_library(self.catalog, self.lib)
        self.assertEqual(sorted(r["written"]), ["figma", "zoom"])
        zoom = json.loads((self.lib / "zoom.json").read_text())
        self.assertEqual(zoom["message"],
                         "Yes, we can pull Zoom for {company_name}.\n\nPlease:\n1. Create the app.")
        self.assertEqual(zoom["direction"], "pull")
        self.assertEqual(zoom["status"], "approved")
        self.assertEqual(zoom["aliases"], ["zoom recordings"])
        self.assertEqual(zoom["notes"], ["Manual download only for small sets."])
        self.assertIn("2026-09-01.1", zoom["source"])
        self.assertNotIn("never used", json.dumps(json.loads((self.lib / "figma.json").read_text())))

    def test_seed_falls_back_to_recommendation_and_steps(self):
        imp.seed_library(self.catalog, self.lib)
        figma = json.loads((self.lib / "figma.json").read_text())
        self.assertEqual(figma["message"],
                         "Export the files explicitly.\n\n1. Open Figma.\n2. Export each file.")
        self.assertEqual(figma["direction"], "push")

    def test_seed_normalizes_route_lines(self):
        cat = json.loads(self.catalog.read_text())
        cat["cases"][0]["kickoff_draft"] = ["* You push it (partial), Instructions: export.",
                                            "We pull it, Instructions: token."]
        self.catalog.write_text(json.dumps(cat))
        imp.seed_library(self.catalog, self.lib)
        self.assertEqual(json.loads((self.lib / "zoom.json").read_text())["message"],
                         "- **You push it**, Instructions: export.\n- **We pull it**, Instructions: token.")

    def test_seed_output_loads_as_a_valid_library(self):
        imp.seed_library(self.catalog, self.lib)
        lib = se.load_library(self.lib)
        self.assertEqual(sorted(lib), ["figma", "zoom"])

    def test_seed_does_not_overwrite_without_flag(self):
        imp.seed_library(self.catalog, self.lib)
        (self.lib / "zoom.json").write_text(json.dumps({**json.loads((self.lib / "zoom.json").read_text()),
                                                        "message": "hand-edited"}))
        r = imp.seed_library(self.catalog, self.lib)
        self.assertEqual(sorted(r["skipped_existing"]), ["figma", "zoom"])
        self.assertEqual(json.loads((self.lib / "zoom.json").read_text())["message"], "hand-edited")
        r = imp.seed_library(self.catalog, self.lib, overwrite=True)
        self.assertIn("zoom", r["written"])

    def test_seed_replaces_em_dashes_so_entries_validate(self):
        cat = json.loads(self.catalog.read_text())
        cat["cases"][0]["kickoff_draft"] = ["We pull it — easy."]
        self.catalog.write_text(json.dumps(cat))
        imp.seed_library(self.catalog, self.lib)
        self.assertEqual(json.loads((self.lib / "zoom.json").read_text())["message"],
                         "- **We pull it**, easy.")
        se.load_library(self.lib)


class RegistrationImportTests(ImportCase):
    def test_import_maps_slug_and_copies_parents(self):
        (self.root / "sentient").mkdir()
        (self.root / "sentient" / "config.json").write_text('{"slug": "sentient"}')
        r = imp.import_registrations(self.regs, self.root,
                                     mapping={"sentient-decision-science": "sentient"},
                                     library={})
        self.assertEqual(r["imported"], ["sentient"])
        self.assertEqual(r["skipped_no_company"], ["wiza"])
        reg = se.load_channel(self.root, "sentient")
        self.assertEqual(reg["channel_id"], "C0SENT00001")
        self.assertEqual(reg["company_name"], "Sentient Decision Science")
        self.assertEqual(reg["instructions_canvas"]["canvas_id"], "F0SENTCANV")
        self.assertEqual([(p["ts"], p["kind"], p["label"]) for p in reg["parents"]],
                         [("100.000001", "onboarding", "Onboarding"),
                          ("101.000001", "service", "Zoom")])
        self.assertEqual(reg["parents"][1]["author_user_id"], "U0BT0NY588Y")
        self.assertIsNone(reg["channel_hwm_ts"])
        self.assertTrue(se.read_plan(self.root, "sentient")["full_read"])

    def test_import_uses_plugin_slug_when_it_matches_a_company_dir(self):
        (self.root / "wiza").mkdir()
        (self.root / "wiza" / "config.json").write_text('{"slug": "wiza"}')
        r = imp.import_registrations(self.regs, self.root, mapping={}, library={})
        self.assertEqual(r["imported"], ["wiza"])

    def test_import_is_idempotent(self):
        (self.root / "wiza").mkdir()
        (self.root / "wiza" / "config.json").write_text('{"slug": "wiza"}')
        imp.import_registrations(self.regs, self.root, mapping={}, library={})
        r = imp.import_registrations(self.regs, self.root, mapping={}, library={})
        self.assertEqual(r["already_registered"], ["wiza"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
