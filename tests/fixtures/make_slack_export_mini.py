#!/usr/bin/env python3
"""Generate tests/fixtures/slack-export-mini.zip — a SYNTHETIC Slack export.

Built to the schema observed on a real Business+ compliance export, never
from one: every id, name, user and token here is invented, and the workspace
is fictional. Regenerate with:

    python3 tests/fixtures/make_slack_export_mini.py

Every awkward shape the real format has is represented exactly once, so the
offline tests exercise the ledger's real edge cases:

  * a conversation directory whose UTF-8 name is stored WITHOUT the zip
    UTF-8 flag bit (Slack's own behaviour — the cp437 mojibake case)
  * a hosted file whose url carries `?token=`, plus thumbnail renditions
  * a hosted file nested in `attachments[].files[]` carrying NO token
  * a transcoded video whose url carries `?t=` instead of `?token=`
  * a `mode: external` gdrive reference (no Slack bytes exist for it)
  * a `mode: tombstone` entry with no url at all
  * the same file shared in two conversations (dedup + the shares ledger)
  * root canvases.json / lists.json / huddle_transcripts.json entries, which
    no message walk would ever reach
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent / "slack-export-mini.zip"
TEAM = "T0FIXTURE01"
TOKEN = "xoxe-0000000000000-0000000000000-0000000000000-" + "0" * 32
FILES = "https://files.slack.com"
WS = "https://acmeco-fixture.slack.com"
# The emoji makes this name non-ASCII, which is what forces the cp437 path.
FC_DIR = "FC:F0FIXCANVAS:\U0001f517 Links"


def pri(fid: str, name: str, tok: str = TOKEN, param: str = "token") -> dict:
    stem = f"{FILES}/files-pri/{TEAM}-{fid}"
    q = f"?{param}={tok}" if tok else ""
    return {"url_private": f"{stem}/{name}{q}",
            "url_private_download": f"{stem}/download/{name}{q}"}


def hosted(fid, name, size, filetype, mimetype, user, **extra) -> dict:
    row = {"id": fid, "created": 1767225600, "timestamp": 1767225600,
           "name": name, "title": name, "mimetype": mimetype,
           "filetype": filetype, "pretty_type": filetype.upper(),
           "user": user, "user_team": TEAM, "editable": False, "size": size,
           "mode": "hosted", "is_external": False, "external_type": "",
           "is_public": False, "public_url_shared": False,
           "display_as_bot": False, "username": "",
           "permalink": f"{WS}/files/{user}/{fid}/{name}",
           "is_starred": False, "has_rich_preview": False,
           "file_access": "visible"}
    row.update(pri(fid, name))
    row.update(extra)
    return row


def msg(ts, user, text, **extra) -> dict:
    out = {"type": "message", "user": user, "text": text, "ts": ts,
           "team": TEAM}
    out.update(extra)
    return out


PNG = hosted("F0FIXPNG001", "diagram.png", 52594, "png", "image/png",
             "U0FIXUSER01",
             thumb_64=f"{FILES}/files-tmb/{TEAM}-F0FIXPNG001-aa/"
                      f"diagram_64.png?t={TOKEN}",
             thumb_360=f"{FILES}/files-tmb/{TEAM}-F0FIXPNG001-aa/"
                       f"diagram_360.png?t={TOKEN}")

# The attachments[].files[] case: real hosted bytes, NO token on the url.
ATT = hosted("F0FIXATT001", "unfurled.png", 12345, "png", "image/png",
             "U0FIXUSER02")
ATT.update(pri("F0FIXATT001", "unfurled.png", tok=""))

# A screenshare: Slack serves the TRANSCODE from files-tmb with `?t=`.
VID = hosted("F0FIXVID001", "screenshare.mp4", 402653184, "mp4", "video/mp4",
             "U0FIXUSER01", subtype="slack_video")
VID["url_private"] = (f"{FILES}/files-tmb/{TEAM}-F0FIXVID001-bb/"
                      f"screenshare.mp4?t={TOKEN}")
VID["url_private_download"] = (f"{FILES}/files-tmb/{TEAM}-F0FIXVID001-bb/"
                               f"download/screenshare.mp4?t={TOKEN}")
VID["mp4_low"] = (f"{FILES}/files-tmb/{TEAM}-F0FIXVID001-bb/"
                  f"screenshare_low.mp4?t={TOKEN}")

EXT = {"id": "F0FIXEXT001", "created": 1767225600, "name": "Roadmap",
       "title": "Roadmap", "mimetype": "application/vnd.google-apps.document",
       "filetype": "gdoc", "pretty_type": "GDoc", "user": "U0FIXUSER02",
       "size": 4194304, "mode": "external", "is_external": True,
       "external_type": "gdrive", "file_access": "visible",
       "url_private": "https://docs.google.com/document/d/FIXTURE/edit",
       "permalink": f"{WS}/files/U0FIXUSER02/F0FIXEXT001/roadmap"}

TOMB = {"id": "F0FIXTOMB01", "created": 1767225600, "mode": "tombstone",
        "name": None, "title": "This file was deleted.", "size": 0,
        "user": "U0FIXUSER01", "filetype": "", "mimetype": ""}

DMFILE = hosted("F0FIXDM0001", "notes.txt", 900, "text", "text/plain",
                "U0FIXUSER03")
CANVASFILE = hosted("F0FIXCANVAS", "Links", 80, "quip",
                    "application/vnd.slack-docs", "U0FIXUSER01")
CANVASFILE["mode"] = "quip"

ROOT = {
    ".slack-manifest.json": {
        "version": 2,
        "metadata": {"created_at": 1767225600,
                     "created_at_iso": "2026-01-01T00:00:00Z",
                     "export_type": "MANUAL_COMPLIANCE", "exporter": "slack",
                     "file_count": 5, "total_bytes": 4096},
        "checksum": {"algorithm": "sha256-agg-v2", "value": "0" * 64},
    },
    "channels.json": [
        {"id": "C0FIXGENERAL", "name": "general", "created": 1767225600,
         "creator": "U0FIXUSER01", "is_archived": False, "is_general": True,
         "members": ["U0FIXUSER01", "U0FIXUSER02"], "pins": [],
         "topic": {"value": "", "creator": "", "last_set": 0},
         "purpose": {"value": "", "creator": "", "last_set": 0}}],
    "groups.json": [
        {"id": "C0FIXPRIVATE", "name": "private-ops", "created": 1767225600,
         "creator": "U0FIXUSER01", "is_archived": False,
         "members": ["U0FIXUSER01"],
         "topic": {"value": "", "creator": "", "last_set": 0},
         "purpose": {"value": "", "creator": "", "last_set": 0}}],
    "mpims.json": [
        {"id": "C0FIXMPIM001", "name": "mpdm-a--b--c-1",
         "created": 1767225600, "creator": "U0FIXUSER01", "is_archived": False,
         "members": ["U0FIXUSER01", "U0FIXUSER02", "U0FIXUSER03"],
         "topic": {"value": "", "creator": "", "last_set": 0},
         "purpose": {"value": "", "creator": "", "last_set": 0}}],
    "dms.json": [
        {"id": "D0FIXDM0001", "created": 1767225600,
         "members": ["U0FIXUSER01", "U0FIXUSER03"]}],
    "file_conversations.json": [
        {"id": "C0FIXFCONV01", "name": FC_DIR, "created": 1767225600,
         "creator": "USLACKBOT", "is_archived": False,
         "members": ["U0FIXUSER01"],
         "topic": {"value": "", "creator": "", "last_set": 0},
         "purpose": {"value": "", "creator": "", "last_set": 0}}],
    "users.json": [
        {"id": f"U0FIXUSER0{i}", "name": f"user{i}", "deleted": False,
         "is_bot": False, "profile": {"real_name": f"User {i}"}}
        for i in (1, 2, 3)],
    "integration_logs.json": [],
    "canvases.json": [
        {"id": "F0FIXCANVAS", "created": 1767225600, "name": "Links",
         "title": "\U0001f517 Links",
         "mimetype": "application/vnd.slack-docs", "filetype": "quip",
         "pretty_type": "Canvas", "user": "U0FIXUSER01", "editable": True,
         "size": 80, "mode": "quip", "is_public": True,
         "is_tombstoned": False, "public_url_shared": False, "date_delete": 0,
         "url_private_download":
             f"{FILES}/files-pri/{TEAM}-F0FIXCANVAS/download/canvas"
             f"?token={TOKEN}",
         "shares": [{"team": TEAM, "channel": "C0FIXFCONV01"}],
         "is_modified_by_ai": False,
         "permalink": f"{WS}/docs/{TEAM}/F0FIXCANVAS",
         "canvas_history_download":
             f"{FILES}/files-canvas-history/{TEAM}/F0FIXCANVAS"
             f"?history_start=1&history_end=2&t={TOKEN}",
         "export_timestamp": 1767225600}],
    "lists.json": [
        {"id": "F0FIXLIST001", "created": 1767225600, "name": "list",
         "title": "To-do", "mimetype": "application/vnd.slack-list",
         "filetype": "list", "pretty_type": "List", "user": "U0FIXUSER01",
         "editable": True, "size": 0, "mode": "list", "is_public": False,
         "is_tombstoned": False, "public_url_shared": False, "date_delete": 0,
         "url_private_download":
             f"{FILES}/files-pri/{TEAM}-F0FIXLIST001/download/list"
             f"?token={TOKEN}",
         "shares": [{"team": TEAM, "channel": "D0FIXDM0001"}],
         "permalink": f"{WS}/lists/{TEAM}/F0FIXLIST001",
         "list_history_download":
             f"{FILES}/files-list-history/{TEAM}/F0FIXLIST001?t={TOKEN}",
         "export_timestamp": 1767225600}],
    "huddle_transcripts.json": [
        {"id": "F0FIXHUDDLE1", "created": 1767225600,
         "name": "Huddle_transcript", "title": "Huddle transcript",
         "mimetype": "application/vnd.slack-huddle-transcript",
         "filetype": "huddle_transcript", "pretty_type": "Huddle transcript",
         "user": "U0FIXUSER01", "editable": True, "size": 5066, "mode":
         "huddle_transcript", "is_public": False, "is_tombstoned": False,
         "public_url_shared": False, "date_delete": 0,
         "url_private_download":
             f"{FILES}/files-pri/{TEAM}-F0FIXHUDDLE1/download/"
             f"huddle_transcript?token={TOKEN}",
         "shares": [{"team": TEAM, "channel": "C0FIXGENERAL"}],
         "permalink": f"{WS}/files/U0FIXUSER01/F0FIXHUDDLE1/huddle",
         "export_timestamp": 1767225600}],
}

DAYS = {
    "general/2026-01-02.json": [
        msg("1767312000.000100", "U0FIXUSER01", "here is the diagram",
            files=[PNG], upload=True),
        {"type": "message", "subtype": "bot_message", "ts":
         "1767312000.000200", "text": "deploy finished",
         "bot_id": "B0FIXBOT001", "username": "ci",
         "attachments": [{"fallback": "screenshot", "files": [ATT]}]},
        msg("1767312000.000300", "U0FIXUSER02", "roadmap doc", files=[EXT]),
        msg("1767312000.000400", "U0FIXUSER01", "gone", files=[TOMB]),
    ],
    "private-ops/2026-01-03.json": [
        # the SAME png again: dedup keeps one blob, the shares ledger keeps
        # the second sighting's conversation linkage
        msg("1767398400.000100", "U0FIXUSER01", "reposting", files=[PNG]),
        msg("1767398400.000200", "U0FIXUSER01", "screenshare", files=[VID]),
    ],
    f"{FC_DIR}/2026-01-04.json": [
        msg("1767484800.000100", "U0FIXUSER01", "canvas thread",
            files=[CANVASFILE]),
    ],
    "D0FIXDM0001/2026-01-05.json": [
        msg("1767571200.000100", "U0FIXUSER03", "notes", files=[DMFILE]),
    ],
}


def clear_utf8_flag(path: Path) -> None:
    """Strip the zip UTF-8 general-purpose flag bit (0x800) from every entry.

    Slack's own exporter writes UTF-8 member names with this bit UNSET, which
    is why Python's zipfile mis-decodes them as cp437 and why the puller has
    to re-decode. Reproducing that faithfully needs a byte-level edit: the
    flag is ORed in by zipfile at write time, so it cannot be cleared through
    the API. Offsets are fixed by the ZIP spec — 6 into a local file header,
    8 into a central-directory header.
    """
    data = bytearray(path.read_bytes())
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = data.find(sig)
        while i != -1:
            data[i + off + 1] &= ~0x08     # bit 11 lives in the 2nd byte
            i = data.find(sig, i + 4)
    path.write_bytes(bytes(data))


def main() -> None:
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for name, payload in ROOT.items():
            z.writestr(name, json.dumps(payload, ensure_ascii=False,
                                        indent=1))
        for name, payload in DAYS.items():
            z.writestr(name, json.dumps(payload, ensure_ascii=False,
                                        indent=1))
    clear_utf8_flag(OUT)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
