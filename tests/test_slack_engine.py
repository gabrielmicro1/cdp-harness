#!/usr/bin/env python3
"""Offline tests for scripts/slack_engine.py — no Slack, no network.

    python3 tests/test_slack_engine.py

Also invoked as one section of tests/test_harness.py.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
FIXTURES = REPO / "tests" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import slack_engine as se  # noqa: E402

CHANNEL_URL = "https://micro1-companies.slack.com/archives/C0TEST12345"
OWNER = "U0OWNER0001"


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cdp-slack-test-"))
        self.root = self.tmp / "companies"
        shutil.copytree(FIXTURES / "companies", self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def register(self, slug="democo", **kw):
        args = dict(channel_url=CHANNEL_URL, owner_user_id=OWNER,
                    company_name="Democo", channel_type="slack_connect")
        args.update(kw)
        return se.register(self.root, slug, **args)


class RegistrationTests(EngineCase):
    def test_register_writes_channel_json(self):
        reg = self.register()
        path = self.root / "democo" / "slack" / "channel.json"
        self.assertTrue(path.exists())
        stored = json.loads(path.read_text())
        self.assertEqual(stored["channel_id"], "C0TEST12345")
        self.assertEqual(stored["workspace_domain"], "micro1-companies")
        self.assertEqual(stored["owner_user_id"], OWNER)
        self.assertEqual(stored["channel_type"], "slack_connect")
        self.assertEqual(stored["parents"], [])
        self.assertIsNone(stored["channel_hwm_ts"])
        self.assertEqual(stored["thread_hwm"], {})
        self.assertEqual(stored["acked"], {})
        self.assertIsNone(stored["instructions_canvas"])
        self.assertEqual(reg["slug"], "democo")

    def test_register_is_idempotent_for_same_channel(self):
        first = self.register()
        second = self.register()
        self.assertEqual(first["registered_at"], second["registered_at"])

    def test_register_refuses_channel_owned_by_another_slug(self):
        self.register()
        (self.root / "othco").mkdir()
        with self.assertRaises(se.SlackEngineError):
            self.register(slug="othco")

    def test_register_refuses_changing_channel_of_registered_slug(self):
        self.register()
        with self.assertRaises(se.SlackEngineError):
            self.register(channel_url="https://micro1-companies.slack.com/archives/C0OTHER9999")

    def test_register_rejects_non_slack_url(self):
        with self.assertRaises(se.SlackEngineError):
            self.register(channel_url="https://example.com/archives/C0TEST12345")

    def test_register_rejects_bad_channel_id(self):
        with self.assertRaises(se.SlackEngineError):
            self.register(channel_url="https://micro1-companies.slack.com/archives/nope")

    def test_register_rejects_unknown_channel_type(self):
        with self.assertRaises(se.SlackEngineError):
            self.register(channel_type="shared")

    def test_register_rejects_missing_company_dir(self):
        with self.assertRaises(se.SlackEngineError):
            self.register(slug="ghost")

    def test_register_defaults_to_no_teammates(self):
        self.register()
        stored = json.loads((self.root / "democo" / "slack" / "channel.json").read_text())
        self.assertEqual(stored["teammate_user_ids"], [])

    def test_register_stores_teammates_deduplicated_in_order(self):
        reg = self.register(teammate_user_ids=["U0TEAM00001", "U0TEAM00002", "U0TEAM00001"])
        self.assertEqual(reg["teammate_user_ids"], ["U0TEAM00001", "U0TEAM00002"])
        stored = json.loads((self.root / "democo" / "slack" / "channel.json").read_text())
        self.assertEqual(stored["teammate_user_ids"], ["U0TEAM00001", "U0TEAM00002"])

    def test_register_rejects_bad_teammate_id(self):
        with self.assertRaises(se.SlackEngineError):
            self.register(teammate_user_ids=["arrsha"])

    def test_register_rejects_owner_as_teammate(self):
        with self.assertRaises(se.SlackEngineError):
            self.register(teammate_user_ids=[OWNER])


class TeammateTests(EngineCase):
    """set_teammates: the post-registration edit path for the colleague list."""

    def test_set_teammates_replaces_and_persists(self):
        self.register(teammate_user_ids=["U0TEAM00001"])
        out = se.set_teammates(self.root, "democo", ["U0TEAM00002", "U0TEAM00003"])
        self.assertEqual(out["teammate_user_ids"], ["U0TEAM00002", "U0TEAM00003"])
        self.assertEqual(se.load_channel(self.root, "democo")["teammate_user_ids"],
                         ["U0TEAM00002", "U0TEAM00003"])

    def test_set_teammates_empty_clears(self):
        self.register(teammate_user_ids=["U0TEAM00001"])
        self.assertEqual(se.set_teammates(self.root, "democo", [])["teammate_user_ids"], [])

    def test_set_teammates_rejects_owner_and_bad_ids(self):
        self.register()
        with self.assertRaises(se.SlackEngineError):
            se.set_teammates(self.root, "democo", [OWNER])
        with self.assertRaises(se.SlackEngineError):
            se.set_teammates(self.root, "democo", ["nope"])

    def test_set_teammates_needs_registration(self):
        with self.assertRaises(se.SlackEngineError):
            se.set_teammates(self.root, "democo", ["U0TEAM00001"])


class CanvasTests(EngineCase):
    PERMALINK = "https://micro1-companies.slack.com/docs/T0B8VHRABR7/F0CANVAS001"

    def test_record_canvas_stores_reference(self):
        self.register()
        reg = se.record_canvas(self.root, "democo", canvas_id="F0CANVAS001",
                               title="EDP Instructions", permalink=self.PERMALINK)
        self.assertEqual(reg["instructions_canvas"]["canvas_id"], "F0CANVAS001")
        self.assertEqual(reg["instructions_canvas"]["permalink"], self.PERMALINK)

    def test_record_canvas_rejects_permalink_from_other_workspace(self):
        self.register()
        with self.assertRaises(se.SlackEngineError):
            se.record_canvas(self.root, "democo", canvas_id="F0CANVAS001",
                             title="EDP Instructions",
                             permalink="https://other.slack.com/docs/T1/F0CANVAS001")

    def test_record_canvas_rejects_permalink_naming_other_canvas(self):
        self.register()
        with self.assertRaises(se.SlackEngineError):
            se.record_canvas(self.root, "democo", canvas_id="F0CANVAS001",
                             title="EDP Instructions",
                             permalink="https://micro1-companies.slack.com/docs/T1/F0OTHER")

    def test_record_canvas_refuses_silent_replace(self):
        self.register()
        se.record_canvas(self.root, "democo", canvas_id="F0CANVAS001",
                         title="EDP Instructions", permalink=self.PERMALINK)
        with self.assertRaises(se.SlackEngineError):
            se.record_canvas(self.root, "democo", canvas_id="F0CANVAS002",
                             title="EDP Instructions",
                             permalink=self.PERMALINK.replace("001", "002"))
        reg = se.record_canvas(self.root, "democo", canvas_id="F0CANVAS002",
                               title="EDP Instructions",
                               permalink=self.PERMALINK.replace("001", "002"),
                               replace=True)
        self.assertEqual(reg["instructions_canvas"]["canvas_id"], "F0CANVAS002")


class ReadPlanTests(EngineCase):
    def test_read_plan_fresh_registration(self):
        self.register()
        plan = se.read_plan(self.root, "democo")
        self.assertEqual(plan["channel_id"], "C0TEST12345")
        self.assertIsNone(plan["channel_hwm_ts"])
        self.assertEqual(plan["threads"], [])
        self.assertTrue(plan["full_read"])

    def test_read_plan_without_registration_errors(self):
        with self.assertRaises(se.SlackEngineError):
            se.read_plan(self.root, "democo")



BOT = "U0BOT000001"
CLIENT = "U0CLIENT001"
TEAMMATE = "U0TEAM00001"


def msg(ts, text, *, user=CLIENT, name="Client", thread_ts=None, bot=False,
        reply_count=0, latest_reply_ts=None, reactions=()):
    return {"ts": ts, "thread_ts": thread_ts, "user_id": user, "user_name": name,
            "is_bot": bot, "text": text, "reply_count": reply_count,
            "latest_reply_ts": latest_reply_ts, "reactions": list(reactions)}


def snapshot(messages, *, channel_id="C0TEST12345", complete=True, oldest_ts=None):
    return {"channel_id": channel_id, "taken_at": "2026-09-01T14:00:00Z",
            "oldest_ts": oldest_ts, "complete": complete, "messages": messages}


def parent(ts, label, **kw):
    return msg(ts, f"[{label}]", user=BOT, name="Company Transfer Setup", bot=True, **kw)


class SnapshotValidationTests(EngineCase):
    def test_rejects_missing_message_field(self):
        bad = msg("1.000001", "hi")
        del bad["reactions"]
        with self.assertRaises(se.SlackEngineError):
            se.validate_snapshot(snapshot([bad]))

    def test_rejects_extra_message_field(self):
        bad = msg("1.000001", "hi")
        bad["blocks"] = []
        with self.assertRaises(se.SlackEngineError):
            se.validate_snapshot(snapshot([bad]))

    def test_rejects_bad_ts(self):
        with self.assertRaises(se.SlackEngineError):
            se.validate_snapshot(snapshot([msg("not-a-ts", "hi")]))

    def test_rejects_missing_top_level_field(self):
        snap = snapshot([])
        del snap["complete"]
        with self.assertRaises(se.SlackEngineError):
            se.validate_snapshot(snap)

    def test_accepts_well_formed(self):
        se.validate_snapshot(snapshot([msg("1.000001", "hi")]))


class IngestTests(EngineCase):
    def ingest(self, messages, **kw):
        return se.ingest(self.root, "democo", snapshot(messages, **kw), library={})

    def test_ingest_rejects_wrong_channel(self):
        self.register()
        with self.assertRaises(se.SlackEngineError):
            self.ingest([], channel_id="C0OTHER9999")

    def test_ingest_stores_union_and_replaces_by_ts(self):
        self.register()
        self.ingest([msg("1.000001", "first"), msg("2.000001", "second")])
        self.ingest([msg("2.000001", "second (edited)"), msg("3.000001", "third")])
        snap = se.load_snapshot(self.root, "democo")
        texts = {m["ts"]: m["text"] for m in snap["messages"]}
        self.assertEqual(texts, {"1.000001": "first", "2.000001": "second (edited)",
                                 "3.000001": "third"})

    def test_ingest_advances_high_water_marks(self):
        self.register()
        self.ingest([
            parent("10.000001", "Zoom", reply_count=2, latest_reply_ts="12.000001"),
            msg("11.000001", "q", thread_ts="10.000001"),
            msg("12.000001", "a", thread_ts="10.000001", user=OWNER, name="Gabe"),
            msg("13.000001", "top-level, no replies"),
        ])
        reg = se.load_channel(self.root, "democo")
        self.assertEqual(reg["channel_hwm_ts"], "13.000001")
        self.assertEqual(reg["thread_hwm"], {"10.000001": "12.000001"})
        plan = se.read_plan(self.root, "democo")
        self.assertFalse(plan["full_read"])
        self.assertEqual(plan["threads"], [{"thread_ts": "10.000001",
                                            "last_reply_ts": "12.000001"}])

    def test_ingest_never_lowers_high_water_mark(self):
        self.register()
        self.ingest([msg("20.000001", "late")])
        self.ingest([msg("5.000001", "backfill")])
        self.assertEqual(se.load_channel(self.root, "democo")["channel_hwm_ts"],
                         "20.000001")

    def test_ingest_records_partial_flag(self):
        self.register()
        self.ingest([msg("1.000001", "x")], complete=False)
        self.assertFalse(se.load_snapshot(self.root, "democo")["complete"])
        self.ingest([msg("2.000001", "y")], complete=True)
        self.assertTrue(se.load_snapshot(self.root, "democo")["complete"])

    def test_ingest_discovers_parents_from_bot_bracket_posts(self):
        self.register()
        result = self.ingest([
            parent("10.000001", "Onboarding"),
            parent("11.000001", "Zoom"),
            parent("12.000001", "Progress Check"),
            msg("13.000001", "[Not a parent]"),          # human, not a bot
            msg("14.000001", "chatter, not a label", user=BOT, bot=True),  # bot, not bracketed
        ])
        reg = se.load_channel(self.root, "democo")
        kinds = {p["ts"]: (p["kind"], p["label"]) for p in reg["parents"]}
        self.assertEqual(kinds, {"10.000001": ("onboarding", "Onboarding"),
                                 "11.000001": ("service", "Zoom"),
                                 "12.000001": ("progress_check", "Progress Check")})
        self.assertEqual(reg["parents"][0]["author_user_id"], BOT)
        self.assertEqual(result["parents_discovered"], 3)

    def test_ingest_flags_duplicate_labels_as_ambiguous(self):
        self.register()
        result = self.ingest([parent("10.000001", "Zoom"), parent("11.000001", "zoom")])
        reg = se.load_channel(self.root, "democo")
        self.assertTrue(all(p["ambiguous"] for p in reg["parents"]))
        self.assertEqual(result["ambiguous_labels"], ["zoom"])


def entry(service_id, message="Hello {company_name}.", *, aliases=(), direction="pull",
          status="approved", display_name=None, notes=()):
    return {"service_id": service_id, "display_name": display_name or service_id.title(),
            "aliases": list(aliases), "direction": direction, "status": status,
            "message": message, "notes": list(notes), "source": "test",
            "updated_at": "2026-09-01T00:00:00Z"}


def write_library(path: Path, entries):
    path.mkdir(parents=True, exist_ok=True)
    for e in entries:
        (path / f"{e['service_id']}.json").write_text(json.dumps(e, indent=2))
    return path


class LibraryTests(EngineCase):
    def lib(self, entries):
        return write_library(self.tmp / "kickoff-copy", entries)

    def test_load_library_indexes_by_service_id(self):
        lib = se.load_library(self.lib([entry("zoom"), entry("hubspot", direction="push")]))
        self.assertEqual(sorted(lib), ["hubspot", "zoom"])
        self.assertEqual(lib["hubspot"]["direction"], "push")

    def test_load_library_rejects_missing_field(self):
        e = entry("zoom"); del e["notes"]
        with self.assertRaises(se.SlackEngineError):
            se.load_library(self.lib([e]))

    def test_load_library_rejects_bad_status_and_direction(self):
        with self.assertRaises(se.SlackEngineError):
            se.load_library(self.lib([entry("zoom", status="reviewed")]))
        with self.assertRaises(se.SlackEngineError):
            se.load_library(self.lib([entry("zoom", direction="sideways")]))

    def test_load_library_rejects_em_dash(self):
        with self.assertRaises(se.SlackEngineError):
            se.load_library(self.lib([entry("zoom", "We pull it — easy.")]))

    def test_load_library_rejects_secret_shaped_text(self):
        for bad in ("token xoxb-123456789012-abcdefghijkl", "key AKIAABCDEFGHIJKLMNOP",
                    "see https://x/y?sig=abcdefghijklmnop", "api_key: supersecret123"):
            with self.assertRaises(se.SlackEngineError, msg=bad):
                se.load_library(self.lib([entry("zoom", bad)]))

    def test_load_library_rejects_duplicate_alias_across_entries(self):
        with self.assertRaises(se.SlackEngineError):
            se.load_library(self.lib([entry("zoom", aliases=["meetings"]),
                                      entry("teams", aliases=["Meetings"])]))

    def test_load_library_rejects_filename_mismatch(self):
        path = self.lib([entry("zoom")])
        (path / "zoom.json").rename(path / "zoomer.json")
        with self.assertRaises(se.SlackEngineError):
            se.load_library(path)

    def test_load_library_rejects_oversized_message(self):
        with self.assertRaises(se.SlackEngineError):
            se.load_library(self.lib([entry("zoom", "x" * 40_001)]))


class MatchingTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.lib = se.load_library(write_library(self.tmp / "kickoff-copy", [
            entry("zoom", aliases=["zoom recordings"]),
            entry("google-workspace", aliases=["google", "gdrive", "workspace"]),
            entry("hubspot"), entry("zendesk"), entry("zendesc"),
        ]))

    def test_exact_id(self):
        self.assertEqual(se.match_service("HubSpot", self.lib), ("hubspot", "exact"))

    def test_alias(self):
        self.assertEqual(se.match_service("Zoom Recordings", self.lib), ("zoom", "alias"))
        self.assertEqual(se.match_service("gdrive", self.lib), ("google-workspace", "alias"))

    def test_close_unambiguous_typo(self):
        self.assertEqual(se.match_service("Hubspt", self.lib), ("hubspot", "close"))

    def test_close_ambiguous_returns_none(self):
        # equally close to zendesk and zendesc: refuse rather than guess
        self.assertIsNone(se.match_service("zendes", self.lib))

    def test_extra_word_is_not_close(self):
        # "Hubspot CRM" is below the 0.85 ratio; the skill iterates instead
        self.assertIsNone(se.match_service("Hubspot CRM", self.lib))

    def test_no_match(self):
        self.assertIsNone(se.match_service("Carta", self.lib))

    def test_normalize(self):
        self.assertEqual(se.normalize_name("  Bill.com / Invoices  "), "bill com invoices")


class ReconcileTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.libpath = write_library(self.tmp / "kickoff-copy", [
            entry("onboarding", "Start with {instructions_url}.", direction=None),
            entry("progress-check", "Progress here.", direction=None),
            entry("zoom"), entry("hubspot", direction="push"), entry("slack"),
            entry("github", aliases=["code repos"]),
        ])
        self.register()
        se.ingest(self.root, "democo", snapshot([
            parent("10.000001", "Onboarding"), parent("11.000001", "HubSpot"),
            parent("12.000001", "Slack"), parent("13.000001", "Zoom"),
            parent("14.000001", "GitHub"), parent("15.000001", "Mystery"),
            parent("16.000001", "Progress Check"),
        ]), library=se.load_library(self.libpath))

    def test_ingest_assigns_service_ids_to_parents(self):
        by_label = {p["label"]: p["service_id"]
                    for p in se.load_channel(self.root, "democo")["parents"]}
        self.assertEqual(by_label["HubSpot"], "hubspot")
        self.assertEqual(by_label["Onboarding"], "onboarding")
        self.assertIsNone(by_label["Mystery"])

    def test_reconcile_sets(self):
        r = se.reconcile(self.root, "democo", library=se.load_library(self.libpath))
        self.assertEqual({m["service"] for m in r["matched"]}, {"hubspot", "slack"})
        self.assertEqual(sorted(r["declared_without_thread"]), ["code", "gdrive", "zendesk"])
        self.assertEqual([t["label"] for t in r["threads_without_declaration"]], ["Zoom", "GitHub"])
        self.assertEqual([t["label"] for t in r["unmatched_threads"]], ["Mystery"])

    def test_reconcile_honours_slack_label_on_declaration(self):
        exp = common.load_expected(self.root, "democo")
        exp["services"]["code"]["slack_label"] = "GitHub"
        common.write_json(self.root / "democo" / "expected-data-sizes.json", exp)
        r = se.reconcile(self.root, "democo", library=se.load_library(self.libpath))
        matched = {m["service"]: m for m in r["matched"]}
        self.assertIn("code", matched)
        self.assertEqual(matched["code"]["thread_ts"], "14.000001")
        self.assertEqual(matched["code"]["library_entry"], "github")
        self.assertNotIn("code", r["declared_without_thread"])
        self.assertEqual([t["label"] for t in r["threads_without_declaration"]], ["Zoom"])

    def test_reconcile_excludes_ambiguous_parents(self):
        se.ingest(self.root, "democo", snapshot([parent("17.000001", "zoom")]),
                  library=se.load_library(self.libpath))
        r = se.reconcile(self.root, "democo", library=se.load_library(self.libpath))
        self.assertEqual([t["label"] for t in r["threads_without_declaration"]], ["GitHub"])
        self.assertEqual(sorted(r["ambiguous_labels"]), ["zoom"])


class KickoffPlanTests(EngineCase):
    CANVAS = "https://micro1-companies.slack.com/docs/T0B8VHRABR7/F0CANVAS001"

    def setUp(self):
        super().setUp()
        self.lib = se.load_library(write_library(self.tmp / "kickoff-copy", [
            entry("onboarding", "Start with {instructions_url}, {company_name}.", direction=None),
            entry("progress-check", "Progress here.", direction=None),
            entry("zoom", "Zoom copy for {company_name}."),
            entry("hubspot", "HubSpot copy.", direction="push"),
            entry("carta", "Carta draft copy.", status="draft"),
        ]))
        self.register()
        se.ingest(self.root, "democo", snapshot([
            parent("10.000001", "Onboarding"), parent("11.000001", "Zoom"),
            parent("12.000001", "HubSpot"), parent("13.000001", "Carta"),
            parent("14.000001", "Mystery"), parent("15.000001", "Progress Check"),
        ]), library=self.lib)

    def plan(self, **kw):
        items = se.kickoff_plan(self.root, "democo", library=self.lib, **kw)["items"]
        return {i["label"]: i for i in items}

    def test_plan_renders_approved_entries(self):
        p = self.plan()
        self.assertEqual(p["Zoom"]["status"], "planned")
        self.assertEqual(p["Zoom"]["message"], "Zoom copy for Democo.")
        self.assertEqual(p["Zoom"]["thread_ts"], "11.000001")
        self.assertTrue(p["Zoom"]["undeclared"])       # democo declares no zoom
        self.assertFalse(p["HubSpot"]["undeclared"])
        self.assertEqual(p["Progress Check"]["status"], "planned")

    def test_plan_blocks_onboarding_without_canvas(self):
        p = self.plan()
        self.assertEqual(p["Onboarding"]["status"], "blocked")
        se.record_canvas(self.root, "democo", canvas_id="F0CANVAS001",
                         title="EDP Instructions", permalink=self.CANVAS)
        p = self.plan()
        self.assertEqual(p["Onboarding"]["status"], "planned")
        self.assertIn(self.CANVAS, p["Onboarding"]["message"])

    def test_plan_reports_missing_with_draft_text_when_available(self):
        p = self.plan()
        self.assertEqual(p["Carta"]["status"], "missing")
        self.assertEqual(p["Carta"]["draft_entry"]["message"], "Carta draft copy.")
        self.assertEqual(p["Mystery"]["status"], "missing")
        self.assertIsNone(p["Mystery"]["draft_entry"])

    def test_plan_marks_already_drafted_and_force_replans(self):
        se.record_draft(self.root, "democo", kind="kickoff", thread_ts="11.000001",
                        text="Zoom copy for Democo.", service_id="zoom", library=self.lib)
        se.record_draft(self.root, "democo", kind="kickoff", thread_ts="12.000001",
                        text="HubSpot copy.", service_id="hubspot", outcome="already_exists",
                        library=self.lib)
        p = self.plan()
        self.assertEqual(p["Zoom"]["status"], "already-drafted")
        self.assertEqual(p["HubSpot"]["status"], "already-drafted")
        self.assertEqual(self.plan(force=True)["Zoom"]["status"], "planned")

    def test_error_receipt_does_not_count_as_drafted(self):
        se.record_draft(self.root, "democo", kind="kickoff", thread_ts="11.000001",
                        text="Zoom copy for Democo.", service_id="zoom",
                        outcome="error", reason="not_in_channel", library=self.lib)
        self.assertEqual(self.plan()["Zoom"]["status"], "planned")

    def test_plan_skips_ambiguous_parents(self):
        se.ingest(self.root, "democo", snapshot([parent("16.000001", "zoom")]), library=self.lib)
        p = self.plan()
        self.assertEqual(p["Zoom"]["status"], "ambiguous")

    def test_plan_summary_counts(self):
        r = se.kickoff_plan(self.root, "democo", library=self.lib)
        self.assertEqual(r["counts"], {"planned": 3, "missing": 2, "blocked": 1,
                                       "already-drafted": 0, "ambiguous": 0})
        self.assertEqual(sorted(r["declared_without_thread"]),
                         ["code", "gdrive", "slack", "zendesk"])


class RecordDraftTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.lib = se.load_library(write_library(self.tmp / "kickoff-copy", [entry("zoom")]))
        self.register()

    def test_receipt_stores_hash_not_text(self):
        r = se.record_draft(self.root, "democo", kind="reply", thread_ts="50.000001",
                            text="Here is the update.")
        stored = json.loads((self.root / "democo" / "slack" / "drafts.json").read_text())
        self.assertEqual(len(stored["drafts"]), 1)
        self.assertNotIn("Here is the update.", json.dumps(stored))
        self.assertEqual(r["text_sha256"], stored["drafts"][0]["text_sha256"])
        self.assertEqual(r["outcome"], "drafted")

    def test_receipt_records_library_version_for_kickoff(self):
        r = se.record_draft(self.root, "democo", kind="kickoff", thread_ts="11.000001",
                            text="x", service_id="zoom", library=self.lib)
        self.assertEqual(r["library_entry_updated_at"], "2026-09-01T00:00:00Z")

    def test_rejects_bad_kind_outcome_or_ts(self):
        with self.assertRaises(se.SlackEngineError):
            se.record_draft(self.root, "democo", kind="memo", thread_ts="1.1", text="x")
        with self.assertRaises(se.SlackEngineError):
            se.record_draft(self.root, "democo", kind="reply", thread_ts="1.1", text="x",
                            outcome="sent")
        with self.assertRaises(se.SlackEngineError):
            se.record_draft(self.root, "democo", kind="reply", thread_ts="nope", text="x")


class InboxTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.register()
        self.snap = json.loads((FIXTURES / "slack" / "inbox-snapshot.json").read_text())
        se.ingest(self.root, "democo", self.snap, library={})

    def labels(self, result):
        return [(i["label"], i["waiting_since_ts"]) for i in result["items"]]

    def test_inbox_lists_waiting_conversations_in_order(self):
        r = se.inbox(self.root, "democo")
        self.assertEqual(self.labels(r), [("HubSpot", "31.000001"), ("Zoom", "22.000001"),
                                          (None, "50.000001"), ("GitHub", "73.000001")])
        self.assertTrue(r["items"][0]["mentioned"])
        self.assertFalse(r["items"][1]["mentioned"])
        self.assertFalse(r["partial"])

    def test_inbox_mention_matches_connector_pipe_format(self):
        # The Claude Slack connector transcribes mentions as <@Uid|Name>, not
        # the bare <@Uid> of the fixture; both must count as a mention.
        se.ingest(self.root, "democo", snapshot([
            msg("90.000001", "<@U0OWNER0001|Gabe> can you look at this?"),
            msg("91.000001", "<@U0OWNER0001X|Other> not for the owner"),
        ]), library={})
        by_ts = {i["thread_ts"]: i for i in se.inbox(self.root, "democo")["items"]}
        self.assertTrue(by_ts["90.000001"]["mentioned"])
        self.assertFalse(by_ts["91.000001"]["mentioned"])

    def test_inbox_item_carries_context_and_service(self):
        item = next(i for i in se.inbox(self.root, "democo")["items"] if i["label"] == "GitHub")
        self.assertEqual(item["thread_ts"], "70.000001")
        self.assertEqual(item["last_author"], "Client")
        self.assertEqual([m["ts"] for m in item["context"]],
                         ["70.000001", "71.000001", "72.000001", "73.000001"])
        self.assertGreaterEqual(item["age_hours"], 0)
        self.assertEqual(item["kind"], "service")
        top = next(i for i in se.inbox(self.root, "democo")["items"] if i["label"] is None)
        self.assertEqual(top["kind"], "top_level")
        self.assertEqual(top["thread_ts"], "50.000001")

    def test_ack_hides_until_new_activity(self):
        se.ack(self.root, "democo", "70.000001")
        self.assertNotIn("GitHub", [l for l, _ in self.labels(se.inbox(self.root, "democo"))])
        se.ingest(self.root, "democo", snapshot([
            parent("70.000001", "GitHub", reply_count=4, latest_reply_ts="74.000001"),
            msg("74.000001", "one more thing", thread_ts="70.000001")]), library={})
        self.assertIn(("GitHub", "74.000001"), self.labels(se.inbox(self.root, "democo")))

    def test_ack_rejects_unknown_conversation(self):
        with self.assertRaises(se.SlackEngineError):
            se.ack(self.root, "democo", "99.000001")

    def test_check_mark_and_owner_last_are_excluded(self):
        labels = [l for l, _ in self.labels(se.inbox(self.root, "democo"))]
        self.assertNotIn("Slack", labels)
        self.assertNotIn("Onboarding", labels)
        self.assertNotIn("Progress Check", labels)

    def test_partial_snapshot_is_flagged(self):
        se.ingest(self.root, "democo", snapshot([], complete=False), library={})
        self.assertTrue(se.inbox(self.root, "democo")["partial"])

    def test_inbox_all_loops_registered_companies(self):
        (self.root / "othco").mkdir()
        (self.root / "othco" / "config.json").write_text('{"slug": "othco"}')
        se.register(self.root, "othco",
                    channel_url="https://micro1-companies.slack.com/archives/C0OTHER9999",
                    owner_user_id=OWNER, company_name="Othco", channel_type="private")
        se.ingest(self.root, "othco", snapshot([msg("5.000001", "hello?")],
                                                channel_id="C0OTHER9999"), library={})
        r = se.inbox_all(self.root)
        slugs = sorted({i["slug"] for i in r["items"]})
        self.assertEqual(slugs, ["democo", "othco"])
        self.assertEqual(r["companies"], ["democo", "othco"])


    # ---- teammates: a colleague's reply means the ball is in the client's
    # court, but only the owner (or an ack) clears the owner's own floor.

    def with_teammate(self, *messages):
        se.set_teammates(self.root, "democo", [TEAMMATE])
        se.ingest(self.root, "democo", snapshot(list(messages)), library={})
        return se.inbox(self.root, "democo")

    def test_teammate_last_is_not_waiting_on_owner(self):
        r = self.with_teammate(
            parent("80.000001", "Dropbox", reply_count=2, latest_reply_ts="82.000001"),
            msg("81.000001", "how do I share the folder?", thread_ts="80.000001"),
            msg("82.000001", "add us as a viewer", user=TEAMMATE, name="Arrsha",
                thread_ts="80.000001"))
        self.assertNotIn("Dropbox", [l for l, _ in self.labels(r)])

    def test_unregistered_colleague_is_still_a_client(self):
        se.ingest(self.root, "democo", snapshot([
            parent("80.000001", "Dropbox", reply_count=2, latest_reply_ts="82.000001"),
            msg("81.000001", "how do I share the folder?", thread_ts="80.000001"),
            msg("82.000001", "add us as a viewer", user=TEAMMATE, name="Arrsha",
                thread_ts="80.000001")]), library={})
        self.assertIn("Dropbox", [l for l, _ in self.labels(se.inbox(self.root, "democo"))])

    def test_teammate_reply_is_not_the_owners_floor(self):
        # client -> teammate -> client: waiting on micro1 again. The teammate's
        # line is not counted as unanswered, but it does not answer FOR the
        # owner either: the mention and waiting_since stay on the first
        # client message until the owner replies or acks.
        r = self.with_teammate(
            parent("90.000001", "Notion", reply_count=3, latest_reply_ts="93.000001"),
            msg("91.000001", "<@U0OWNER0001> which workspace?", thread_ts="90.000001"),
            msg("92.000001", "the main one I think", user=TEAMMATE, name="Arrsha",
                thread_ts="90.000001"),
            msg("93.000001", "ok and the export format?", thread_ts="90.000001"))
        item = next(i for i in r["items"] if i["label"] == "Notion")
        self.assertEqual(item["unanswered"], 2)
        self.assertEqual(item["waiting_since_ts"], "91.000001")
        self.assertTrue(item["mentioned"])
        self.assertEqual(item["last_author"], "Client")

    def test_teammate_top_level_message_is_not_listed(self):
        r = self.with_teammate(msg("95.000001", "fyi the export started",
                                   user=TEAMMATE, name="Alexis"))
        self.assertNotIn("95.000001", [t for _, t in self.labels(r)])

    def test_registration_without_teammate_key_still_works(self):
        path = self.root / "democo" / "slack" / "channel.json"
        reg = json.loads(path.read_text())
        reg.pop("teammate_user_ids", None)
        path.write_text(json.dumps(reg))
        self.assertEqual(len(se.inbox(self.root, "democo")["items"]), 4)


class VoiceTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.register()
        snap = json.loads((FIXTURES / "slack" / "inbox-snapshot.json").read_text())
        se.ingest(self.root, "democo", snap, library={})

    def rows(self):
        path = self.root / ".voice" / "messages.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_harvest_keeps_only_owner_messages_with_replied_to(self):
        r = se.voice_harvest(self.root, "democo")
        rows = {row["ts"]: row for row in self.rows()}
        self.assertEqual(r["added"], 3)
        self.assertEqual(sorted(rows), ["12.000001", "21.000001", "72.000001"])
        self.assertEqual(rows["12.000001"]["replied_to"]["ts"], "11.000001")   # client msg
        self.assertEqual(rows["21.000001"]["replied_to"]["ts"], "20.000001")   # the parent
        self.assertEqual(rows["72.000001"]["replied_to"]["text"], "which org?")
        self.assertEqual(rows["12.000001"]["tags"], [])
        self.assertEqual(rows["12.000001"]["slug"], "democo")

    def test_harvest_is_idempotent_and_incremental(self):
        se.voice_harvest(self.root, "democo")
        self.assertEqual(se.voice_harvest(self.root, "democo")["added"], 0)
        se.ingest(self.root, "democo", snapshot([
            msg("80.000001", "Top-level note from me", user=OWNER, name="Gabe")]), library={})
        self.assertEqual(se.voice_harvest(self.root, "democo")["added"], 1)
        self.assertIsNone({r["ts"]: r for r in self.rows()}["80.000001"]["replied_to"])
        self.assertEqual(len(self.rows()), 4)

    def test_harvest_redacts_secrets_and_resolves_mentions(self):
        se.ingest(self.root, "democo", snapshot([
            msg("81.000001", "<@U0CLIENT001> use token xoxb-123456789012-abcdefghijkl",
                user=OWNER, name="Gabe", thread_ts="70.000001")]), library={})
        se.voice_harvest(self.root, "democo")
        row = {r["ts"]: r for r in self.rows()}["81.000001"]
        self.assertNotIn("xoxb-", row["text"])
        self.assertIn("[redacted]", row["text"])
        self.assertIn("@Client", row["text"])
        self.assertNotIn("<@U0CLIENT001>", row["text"])

    def test_harvest_resolves_connector_pipe_mentions(self):
        se.ingest(self.root, "democo", snapshot([
            msg("82.000001", "<@U0CLIENT001|Client> sounds good", user=OWNER, name="Gabe",
                thread_ts="70.000001")]), library={})
        se.voice_harvest(self.root, "democo")
        row = {r["ts"]: r for r in self.rows()}["82.000001"]
        self.assertEqual(row["text"], "@Client sounds good")

    def test_tag_updates_row_and_rejects_unknown(self):
        se.voice_harvest(self.root, "democo")
        r = se.voice_tag(self.root, channel_id="C0TEST12345", ts="12.000001",
                         tags=["answer", "onboarding"])
        self.assertEqual(r["tags"], ["answer", "onboarding"])
        self.assertEqual({x["ts"]: x for x in self.rows()}["12.000001"]["tags"],
                         ["answer", "onboarding"])
        with self.assertRaises(se.SlackEngineError):
            se.voice_tag(self.root, channel_id="C0TEST12345", ts="99.000001", tags=["x"])

    def test_select_ranks_by_intent_service_overlap_and_excludes(self):
        se.voice_harvest(self.root, "democo")
        se.voice_tag(self.root, channel_id="C0TEST12345", ts="12.000001", tags=["answer"])
        se.voice_tag(self.root, channel_id="C0TEST12345", ts="21.000001", tags=["kickoff", "zoom"])
        se.voice_tag(self.root, channel_id="C0TEST12345", ts="72.000001", tags=["answer", "github"])
        (self.root / ".voice" / "style.md").write_text("# Style\nShort and direct.\n")
        r = se.voice_select(self.root, intent="answer", service="github", context="which org do we use?")
        self.assertEqual([e["ts"] for e in r["examples"]][:2], ["72.000001", "12.000001"])
        self.assertEqual(len(r["examples"]), 3)   # no score floor: top-N of what exists
        self.assertEqual(r["style"], "# Style\nShort and direct.\n")
        self.assertIn("replied_to", r["examples"][0])
        se.voice_tag(self.root, channel_id="C0TEST12345", ts="72.000001", tags=["exclude"])
        r = se.voice_select(self.root, intent="answer", service="github", context="which org?")
        self.assertNotIn("72.000001", [e["ts"] for e in r["examples"]])
        self.assertEqual([e["ts"] for e in r["examples"]][0], "12.000001")

    def test_select_respects_limit_and_untagged_rows_still_eligible(self):
        se.voice_harvest(self.root, "democo")
        r = se.voice_select(self.root, intent="answer", limit=2)
        self.assertEqual(len(r["examples"]), 2)
        self.assertIsNone(r["style"])

    def test_untagged_lists_only_rows_without_tags(self):
        se.voice_harvest(self.root, "democo")
        se.voice_tag(self.root, channel_id="C0TEST12345", ts="12.000001", tags=["answer"])
        r = se.voice_untagged(self.root, limit=10)
        self.assertEqual([x["ts"] for x in r["rows"]], ["21.000001", "72.000001"])
        self.assertEqual(r["untagged_total"], 2)
        self.assertIn("replied_to", r["rows"][0])
        r = se.voice_untagged(self.root, limit=1)
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["untagged_total"], 2)

    def test_select_with_empty_store(self):
        r = se.voice_select(self.root, intent="answer")
        self.assertEqual(r["examples"], [])


class CliTests(EngineCase):
    """The argparse surface the skills call. Every command prints one JSON object."""

    def setUp(self):
        super().setUp()
        self.lib = write_library(self.tmp / "kickoff-copy", [
            entry("onboarding", "Start at {instructions_url}.", direction=None),
            entry("progress-check", "Progress.", direction=None),
            entry("zoom", "Zoom copy for {company_name}."),
        ])

    def cli(self, *args, rc=0, stdin=""):
        import subprocess
        proc = subprocess.run([sys.executable, str(SCRIPTS / "slack_engine.py"), "--root",
                               str(self.root), "--library", str(self.lib), *map(str, args)],
                              capture_output=True, text=True, input=stdin)
        self.assertEqual(proc.returncode, rc, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def test_register_read_plan_ingest_plan_record_inbox_ack(self):
        out = self.cli("register", "democo", "--channel-url", CHANNEL_URL,
                       "--owner-user-id", OWNER, "--company-name", "Democo",
                       "--channel-type", "slack_connect")
        self.assertEqual(out["channel_id"], "C0TEST12345")
        self.assertTrue(self.cli("read-plan", "democo")["full_read"])

        snap_file = self.tmp / "snapshot.new.json"
        snap_file.write_text(json.dumps(snapshot([
            parent("10.000001", "Zoom", reply_count=1, latest_reply_ts="11.000001"),
            msg("11.000001", "ready when you are", thread_ts="10.000001")])))
        out = self.cli("ingest", "democo", str(snap_file))
        self.assertEqual(out["parents_discovered"], 1)
        self.assertFalse(snap_file.exists(), "ingest consumes the .new file")

        plan = self.cli("kickoff-plan", "democo")
        self.assertEqual(plan["items"][0]["status"], "planned")
        self.assertEqual(plan["items"][0]["message"], "Zoom copy for Democo.")

        text_file = self.tmp / "draft.txt"
        text_file.write_text("Zoom copy for Democo.")
        out = self.cli("record-draft", "democo", "--kind", "kickoff", "--thread-ts", "10.000001",
                       "--service", "zoom", "--text-file", str(text_file))
        self.assertEqual(out["outcome"], "drafted")
        self.assertEqual(self.cli("kickoff-plan", "democo")["items"][0]["status"], "already-drafted")

        box = self.cli("inbox", "democo")
        self.assertEqual([i["label"] for i in box["items"]], ["Zoom"])
        self.cli("ack", "democo", "--thread-ts", "10.000001")
        self.assertEqual(self.cli("inbox", "democo")["items"], [])
        self.assertEqual(self.cli("inbox", "--all")["companies"], ["democo"])

    def test_register_teammates_flag_and_set_teammates(self):
        out = self.cli("register", "democo", "--channel-url", CHANNEL_URL,
                       "--owner-user-id", OWNER, "--company-name", "Democo",
                       "--channel-type", "slack_connect",
                       "--teammates", "U0TEAM00001, U0TEAM00002")
        self.assertEqual(out["teammate_user_ids"], ["U0TEAM00001", "U0TEAM00002"])
        out = self.cli("set-teammates", "democo", "--teammates", "U0TEAM00003")
        self.assertEqual(out["teammate_user_ids"], ["U0TEAM00003"])
        self.assertEqual(self.cli("set-teammates", "democo", "--teammates", "")["teammate_user_ids"], [])
        self.cli("set-teammates", "democo", "--teammates", OWNER, rc=2)

    def test_record_draft_reads_text_from_stdin(self):
        self.register()
        out = self.cli("record-draft", "democo", "--kind", "reply", "--thread-ts", "5.000001",
                       "--text-file", "-", "--outcome", "error", "--reason", "not_in_channel",
                       stdin="hello")
        self.assertEqual(out["reason"], "not_in_channel")

    def test_contract_error_exits_2_with_json_on_stderr(self):
        import subprocess
        proc = subprocess.run([sys.executable, str(SCRIPTS / "slack_engine.py"), "--root",
                               str(self.root), "read-plan", "democo"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr)["error_type"], "contract_error")

    def test_validate_library_and_voice_commands(self):
        out = self.cli("validate-library")
        self.assertEqual(out["entries"], 3)
        self.register()
        se.ingest(self.root, "democo", snapshot([
            msg("1.000001", "q?"), msg("2.000001", "a.", user=OWNER, name="Gabe",
                                          thread_ts="1.000001")]), library={})
        self.assertEqual(self.cli("voice-harvest", "democo")["added"], 1)
        self.assertEqual(self.cli("voice-untagged")["untagged_total"], 1)
        out = self.cli("voice-tag", "--channel-id", "C0TEST12345", "--ts", "2.000001",
                       "--tags", "answer,zoom")
        self.assertEqual(self.cli("voice-untagged")["untagged_total"], 0)
        self.assertEqual(out["tags"], ["answer", "zoom"])
        ctx = self.tmp / "ctx.txt"; ctx.write_text("q?")
        out = self.cli("voice-select", "--intent", "answer", "--service", "zoom",
                       "--context-file", str(ctx), "--limit", "3")
        self.assertEqual(out["examples"][0]["ts"], "2.000001")

    def test_record_canvas_and_reconcile(self):
        self.register()
        out = self.cli("record-canvas", "democo", "--canvas-id", "F0CANVAS001",
                       "--title", "EDP Instructions", "--permalink",
                       "https://micro1-companies.slack.com/docs/T0B8VHRABR7/F0CANVAS001")
        self.assertEqual(out["instructions_canvas"]["canvas_id"], "F0CANVAS001")
        out = self.cli("reconcile", "democo")
        self.assertEqual(sorted(out["declared_without_thread"]),
                         ["code", "gdrive", "hubspot", "slack", "zendesk"])


class RouteSectionTests(EngineCase):
    """Kickoff copy route lines: '- **You push it**' / '- **We pull it**' bullets."""

    def norm(self, text):
        return se.normalize_route_sections(text)

    def test_bare_line_becomes_bold_bullet(self):
        self.assertEqual(self.norm("You push it, Instructions: go to Settings."),
                         "- **You push it**, Instructions: go to Settings.")
        self.assertEqual(self.norm("We pull it: open the CDP platform."),
                         "- **We pull it**: open the CDP platform.")

    def test_existing_bullet_markers_are_normalized(self):
        for marker in ("* ", "• ", "- ", "  * "):
            self.assertEqual(self.norm(marker + "We pull it, Instructions: x"),
                             "- **We pull it**, Instructions: x", marker)

    def test_qualifiers_are_dropped(self):
        self.assertEqual(self.norm("• You push it (partial), Instructions: per repo"),
                         "- **You push it**, Instructions: per repo")
        self.assertEqual(self.norm("We pull it (full): via API"),
                         "- **We pull it**: via API")

    def test_dash_separator_variants(self):
        self.assertEqual(self.norm("- You push it - Instructions: admin > backup"),
                         "- **You push it** - Instructions: admin > backup")
        self.assertEqual(self.norm("You push it -- An admin exports each site."),
                         "- **You push it** -- An admin exports each site.")

    def test_already_formatted_is_idempotent(self):
        text = "- **You push it**, Instructions: x\n\n- **We pull it**, Instructions: y"
        self.assertEqual(self.norm(text), text)
        self.assertEqual(self.norm(self.norm("You push it, Instructions: x")),
                         "- **You push it**, Instructions: x")

    def test_prose_mentioning_routes_is_untouched(self):
        prose = ("Let me know if you would prefer to push this data to us or have us pull it!\n"
                 "For Figma we typically pull your data.\n"
                 "If you push it yourself, tell us when done.")
        self.assertEqual(self.norm(prose), prose)

    def test_case_insensitive_label_is_canonicalized(self):
        self.assertEqual(self.norm("we pull it, Instructions: x"),
                         "- **We pull it**, Instructions: x")

    def test_library_rejects_unformatted_route_lines(self):
        path = write_library(self.tmp / "kickoff-copy",
                             [entry("zoom", "We pull it, Instructions: create the app.")])
        with self.assertRaises(se.SlackEngineError) as cm:
            se.load_library(path)
        self.assertIn("route line", str(cm.exception))
        (path / "zoom.json").write_text(json.dumps(entry(
            "zoom", "- **We pull it**, Instructions: create the app.")))
        se.load_library(path)

    def test_committed_library_route_lines_are_formatted(self):
        lib = se.load_library()   # the real knowledge/kickoff-copy
        routes = [l for e in lib.values() for l in e["message"].split("\n")
                  if se.ROUTE_LINE_RE.match(l)]
        self.assertGreater(len(routes), 30)
        self.assertTrue(all(l.startswith(("- **You push it**", "- **We pull it**")) for l in routes))
        self.assertFalse(any("(partial)" in l or "(full)" in l for l in routes))


if __name__ == "__main__":
    unittest.main(verbosity=1)
