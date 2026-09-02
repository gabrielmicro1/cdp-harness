#!/usr/bin/env python3
"""Slack engine — connector-only, snapshot-driven state + logic for the
slack-kickoff / slack-inbox / harvest-voice skills.

This module never talks to Slack. The skills do every read through the
Claude Slack connector, transcribe the result to a snapshot file, and call
the CLI here; the only Slack write anywhere is the connector's
`slack_send_message_draft`, which the skills call themselves.

State: companies/<slug>/slack/{channel,snapshot,drafts}.json (gitignored),
companies/.voice/ (gitignored). Library: knowledge/kickoff-copy/ (committed).
See docs/superpowers/specs/2026-09-01-slack-engine-design.md.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import common

CHANNEL_TYPES = ("public", "private", "slack_connect")
CHANNEL_ID_RE = re.compile(r"[CDG][A-Z0-9]+")
USER_ID_RE = re.compile(r"[UW][A-Z0-9]+")
CANVAS_ID_RE = re.compile(r"F[A-Z0-9]+")


class SlackEngineError(Exception):
    """A caller-safe contract or state failure (exit 2 at the CLI)."""


# ---------------------------------------------------------------- paths / io

def slack_dir(root: Path, slug: str) -> Path:
    return common.company_dir(root, slug) / "slack"


def channel_path(root: Path, slug: str) -> Path:
    return slack_dir(root, slug) / "channel.json"


def load_channel(root: Path, slug: str) -> dict:
    path = channel_path(root, slug)
    if not path.exists():
        raise SlackEngineError(f"{slug} has no Slack registration; run register first")
    return common.read_json(path)


def save_channel(root: Path, slug: str, reg: dict) -> None:
    reg["updated_at"] = common.iso_now()
    common.write_json(channel_path(root, slug), reg)


def _registered_channels(root: Path) -> dict[str, str]:
    """channel_id -> slug for every registered company under root."""
    out = {}
    for slug in common.list_companies(root):
        path = channel_path(root, slug)
        if path.exists():
            out[common.read_json(path)["channel_id"]] = slug
    return out


# -------------------------------------------------------------- registration

def parse_channel_url(channel_url: str) -> tuple[str, str]:
    parsed = urlparse(channel_url.strip())
    host = parsed.netloc.lower()
    if parsed.scheme != "https" or not host.endswith(".slack.com"):
        raise SlackEngineError("channel_url must be an https Slack workspace URL")
    parts = [p for p in parsed.path.split("/") if p]
    try:
        channel_id = parts[parts.index("archives") + 1]
    except (ValueError, IndexError):
        raise SlackEngineError("channel_url must contain /archives/<channel-id>")
    if not CHANNEL_ID_RE.fullmatch(channel_id):
        raise SlackEngineError("channel_url contains an invalid Slack channel ID")
    return host.removesuffix(".slack.com"), channel_id


def _teammate_ids(owner_user_id: str, user_ids) -> list[str]:
    """Validate + dedupe (order kept) the colleague list. The owner is never a teammate."""
    out: list[str] = []
    for raw in user_ids or ():
        uid = raw.strip()
        if not USER_ID_RE.fullmatch(uid):
            raise SlackEngineError(f"teammate {raw!r} is not a Slack user ID")
        if uid == owner_user_id:
            raise SlackEngineError("the owner cannot be listed as a teammate")
        if uid not in out:
            out.append(uid)
    return out


def register(root: Path, slug: str, *, channel_url: str, owner_user_id: str,
             company_name: str, channel_type: str, teammate_user_ids=()) -> dict:
    if not common.company_dir(root, slug).is_dir():
        raise SlackEngineError(f"no company directory for {slug}; onboard it first")
    if channel_type not in CHANNEL_TYPES:
        raise SlackEngineError(f"channel_type must be one of {CHANNEL_TYPES}")
    owner_user_id = owner_user_id.strip()
    if not USER_ID_RE.fullmatch(owner_user_id):
        raise SlackEngineError("owner_user_id must be a Slack user ID")
    if not company_name.strip():
        raise SlackEngineError("company_name is required")
    teammates = _teammate_ids(owner_user_id, teammate_user_ids)
    workspace, channel_id = parse_channel_url(channel_url)

    owner_slug = _registered_channels(root).get(channel_id)
    if owner_slug and owner_slug != slug:
        raise SlackEngineError(f"channel {channel_id} is already registered to {owner_slug}")

    path = channel_path(root, slug)
    if path.exists():
        existing = common.read_json(path)
        if existing["channel_id"] != channel_id:
            raise SlackEngineError(
                f"{slug} is already registered to channel {existing['channel_id']}; "
                "refusing to re-point it")
        return existing

    now = common.iso_now()
    reg = {
        "slug": slug,
        "company_name": company_name.strip(),
        "workspace_domain": workspace,
        "channel_id": channel_id,
        "channel_url": channel_url.strip(),
        "channel_type": channel_type,
        "owner_user_id": owner_user_id,
        "teammate_user_ids": teammates,
        "instructions_canvas": None,
        "parents": [],
        "channel_hwm_ts": None,
        "thread_hwm": {},
        "acked": {},
        "registered_at": now,
        "updated_at": now,
    }
    common.write_json(path, reg)
    return reg


def set_teammates(root: Path, slug: str, user_ids) -> dict:
    """Replace the registration's colleague list (empty list clears it)."""
    reg = load_channel(root, slug)
    reg["teammate_user_ids"] = _teammate_ids(reg["owner_user_id"], user_ids)
    save_channel(root, slug, reg)
    return reg


def record_canvas(root: Path, slug: str, *, canvas_id: str, title: str,
                  permalink: str, replace: bool = False) -> dict:
    reg = load_channel(root, slug)
    canvas_id = canvas_id.strip()
    if not CANVAS_ID_RE.fullmatch(canvas_id):
        raise SlackEngineError("canvas_id must be a Slack file ID")
    parsed = urlparse(permalink.strip())
    expected_host = f"{reg['workspace_domain']}.slack.com"
    if parsed.scheme != "https" or parsed.netloc.lower() != expected_host:
        raise SlackEngineError("canvas permalink must belong to the registered workspace")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3 or parts[0] != "docs" or parts[-1] != canvas_id:
        raise SlackEngineError("canvas permalink must identify the supplied canvas_id")
    candidate = {"canvas_id": canvas_id, "title": re.sub(r"\s+", " ", title.strip()),
                 "permalink": permalink.strip(), "recorded_at": common.iso_now()}
    existing = reg.get("instructions_canvas")
    if existing and existing["canvas_id"] != canvas_id and not replace:
        raise SlackEngineError(
            f"a different canvas ({existing['canvas_id']}) is recorded; pass --replace")
    reg["instructions_canvas"] = candidate
    save_channel(root, slug, reg)
    return reg


# ---------------------------------------------------------------- read plan

def read_plan(root: Path, slug: str) -> dict:
    reg = load_channel(root, slug)
    threads = [{"thread_ts": ts, "last_reply_ts": last}
               for ts, last in sorted(reg["thread_hwm"].items())]
    return {
        "slug": slug,
        "channel_id": reg["channel_id"],
        "channel_hwm_ts": reg["channel_hwm_ts"],
        "threads": threads,
        "full_read": reg["channel_hwm_ts"] is None,
    }


# ---------------------------------------------------------------- snapshot

SNAPSHOT_FIELDS = {"channel_id", "taken_at", "oldest_ts", "complete", "messages"}
MESSAGE_FIELDS = {"ts", "thread_ts", "user_id", "user_name", "is_bot", "text",
                  "reply_count", "latest_reply_ts", "reactions"}
TS_RE = re.compile(r"\d+\.\d+")
PARENT_LABEL_RE = re.compile(r"\[([^\[\]\n]+)\]")
FIXED_PARENT_KINDS = {"onboarding": ("onboarding", "Onboarding"),
                      "progress check": ("progress_check", "Progress Check")}


def ts_key(ts: str) -> tuple[int, int]:
    a, b = ts.split(".")
    return int(a), int(b)


def validate_snapshot(snap: dict) -> None:
    if not isinstance(snap, dict) or set(snap) != SNAPSHOT_FIELDS:
        raise SlackEngineError(
            f"snapshot must have exactly the fields {sorted(SNAPSHOT_FIELDS)}")
    if not isinstance(snap["complete"], bool):
        raise SlackEngineError("snapshot.complete must be a boolean")
    if snap["oldest_ts"] is not None and not TS_RE.fullmatch(str(snap["oldest_ts"])):
        raise SlackEngineError("snapshot.oldest_ts must be a Slack ts or null")
    if not isinstance(snap["messages"], list):
        raise SlackEngineError("snapshot.messages must be a list")
    for i, m in enumerate(snap["messages"]):
        if not isinstance(m, dict) or set(m) != MESSAGE_FIELDS:
            raise SlackEngineError(
                f"message {i} must have exactly the fields {sorted(MESSAGE_FIELDS)}")
        if not TS_RE.fullmatch(str(m["ts"])):
            raise SlackEngineError(f"message {i}: ts is not a Slack timestamp")
        for opt in ("thread_ts", "latest_reply_ts"):
            if m[opt] is not None and not TS_RE.fullmatch(str(m[opt])):
                raise SlackEngineError(f"message {i}: {opt} must be a Slack ts or null")
        if not isinstance(m["is_bot"], bool) or not isinstance(m["reply_count"], int):
            raise SlackEngineError(f"message {i}: is_bot must be bool, reply_count int")
        if not isinstance(m["text"], str) or not isinstance(m["reactions"], list):
            raise SlackEngineError(f"message {i}: text must be str, reactions a list")


def snapshot_path(root: Path, slug: str) -> Path:
    return slack_dir(root, slug) / "snapshot.json"


def load_snapshot(root: Path, slug: str) -> dict:
    path = snapshot_path(root, slug)
    if not path.exists():
        return {"channel_id": None, "taken_at": None, "complete": True, "messages": []}
    return common.read_json(path)


def parse_parent_label(text: str) -> tuple[str, str] | None:
    """('onboarding'|'progress_check'|'service', label) for an exact [Label] post."""
    m = PARENT_LABEL_RE.fullmatch(text.strip())
    if not m:
        return None
    label = re.sub(r"\s+", " ", m.group(1).strip())
    fixed = FIXED_PARENT_KINDS.get(label.casefold())
    return fixed if fixed else ("service", label)


def discover_parents(messages: list[dict], existing: list[dict]) -> tuple[list[dict], list[str]]:
    """Bot-authored exact [Label] top-level posts; duplicates by label are ambiguous."""
    by_ts = {p["ts"]: dict(p) for p in existing}
    for m in messages:
        if m["thread_ts"] is not None or not m["is_bot"]:
            continue
        parsed = parse_parent_label(m["text"])
        if not parsed:
            continue
        kind, label = parsed
        prev = by_ts.get(m["ts"], {})
        by_ts[m["ts"]] = {"ts": m["ts"], "kind": kind, "label": label,
                          "service_id": prev.get("service_id"),
                          "author_user_id": m["user_id"], "ambiguous": False}
    parents = sorted(by_ts.values(), key=lambda p: ts_key(p["ts"]))
    counts: dict[str, int] = {}
    for p in parents:
        counts[p["label"].casefold()] = counts.get(p["label"].casefold(), 0) + 1
    ambiguous = sorted(k for k, n in counts.items() if n > 1)
    for p in parents:
        p["ambiguous"] = p["label"].casefold() in ambiguous
    return parents, ambiguous


def ingest(root: Path, slug: str, snap: dict, *,
           library: dict[str, dict] | None = None) -> dict:
    validate_snapshot(snap)
    library = library if library is not None else load_library()
    reg = load_channel(root, slug)
    if snap["channel_id"] != reg["channel_id"]:
        raise SlackEngineError(
            f"snapshot is for {snap['channel_id']} but {slug} is registered to {reg['channel_id']}")
    stored = load_snapshot(root, slug)
    merged = {m["ts"]: m for m in stored["messages"]}
    for m in snap["messages"]:
        merged[m["ts"]] = m
    messages = sorted(merged.values(), key=lambda m: ts_key(m["ts"]))
    common.write_json(snapshot_path(root, slug), {
        "channel_id": reg["channel_id"], "taken_at": snap["taken_at"],
        "complete": bool(snap["complete"]), "messages": messages})

    top = [m for m in messages if m["thread_ts"] is None]
    if top:
        newest = max(m["ts"] for m in top)
        if reg["channel_hwm_ts"] is None or ts_key(newest) > ts_key(reg["channel_hwm_ts"]):
            reg["channel_hwm_ts"] = newest
    for m in top:
        if m["latest_reply_ts"]:
            cur = reg["thread_hwm"].get(m["ts"])
            if cur is None or ts_key(m["latest_reply_ts"]) > ts_key(cur):
                reg["thread_hwm"][m["ts"]] = m["latest_reply_ts"]
    before = {p["ts"] for p in reg["parents"]}
    reg["parents"], ambiguous = discover_parents(messages, reg["parents"])
    _assign_service_ids(reg["parents"], library)
    save_channel(root, slug, reg)
    return {"slug": slug, "messages_stored": len(messages),
            "messages_ingested": len(snap["messages"]),
            "complete": bool(snap["complete"]),
            "channel_hwm_ts": reg["channel_hwm_ts"],
            "parents_discovered": len(reg["parents"]),
            "parents_new": len([p for p in reg["parents"] if p["ts"] not in before]),
            "ambiguous_labels": ambiguous}


# ------------------------------------------------------------------ library

LIBRARY_DIR = common.REPO_ROOT / "knowledge" / "kickoff-copy"
LIBRARY_FIELDS = {"service_id", "display_name", "aliases", "direction", "status",
                  "message", "notes", "source", "updated_at"}
DIRECTIONS = ("pull", "push", "adaptive", None)
STATUSES = ("approved", "draft")
MAX_MESSAGE_CHARS = 40_000
CLOSE_MATCH_MIN = 0.85
CLOSE_MATCH_GAP = 0.05
# Credential-shaped material that must never appear in a draft (ported from
# the slack-operator plugin's gate): Slack tokens, AWS keys, bearer tokens,
# signed query strings, Stripe keys, "password: xxx"-style pairs.
SECRET_RE = re.compile(
    r"(?:xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16}|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"[?&](?:sig|signature|token|se|sp|sv)=[^\s&]{8,}|"
    r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{12,}|"
    r"\b(?:client[_ -]?secret|api[_ -]?key|password|connection string)"
    r"\s*[:=]\s*[^\s]{8,})",
    re.IGNORECASE,
)


# Route sections in kickoff copy. A line that opens with "You push it" / "We
# pull it" (after any bullet marker / bold / qualifier) is a route line and
# is rendered as "- **You push it**" + the rest verbatim, so the Slack draft
# shows a bold bullet. Prose that merely mentions pushing/pulling mid-line is
# not a route line.
ROUTE_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s+)?(?:\*\*\s*)?(?P<label>you push it|we pull it)(?:\s*\*\*)?"
    r"(?:\s*\((?:partial|full)\))?(?P<rest>.*)$",
    re.IGNORECASE,
)
ROUTE_LABELS = {"you push it": "You push it", "we pull it": "We pull it"}


def normalize_route_sections(text: str) -> str:
    out = []
    for line in text.split("\n"):
        m = ROUTE_LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        label = ROUTE_LABELS[m.group("label").casefold()]
        rest = m.group("rest")
        out.append(f"- **{label}**{rest}".rstrip())
    return "\n".join(out)


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def check_message_text(text: str, where: str) -> None:
    if not text.strip():
        raise SlackEngineError(f"{where}: message is empty")
    if len(text) > MAX_MESSAGE_CHARS:
        raise SlackEngineError(f"{where}: message exceeds {MAX_MESSAGE_CHARS} characters")
    if "—" in text:
        raise SlackEngineError(f"{where}: message contains an em dash")
    if SECRET_RE.search(text):
        raise SlackEngineError(f"{where}: message contains credential-shaped text")
    for line in text.split("\n"):
        if ROUTE_LINE_RE.match(line) and normalize_route_sections(line) != line:
            raise SlackEngineError(
                f"{where}: route line must read '- **You push it**' / '- **We pull it**' "
                f"with no (partial)/(full) qualifier; got {line[:60]!r}")


def load_library(path: Path | None = None) -> dict[str, dict]:
    path = Path(path) if path else LIBRARY_DIR
    if not path.is_dir():
        raise SlackEngineError(f"kickoff library directory not found: {path}")
    library: dict[str, dict] = {}
    aliases_seen: dict[str, str] = {}
    for file in sorted(path.glob("*.json")):
        e = common.read_json(file)
        where = file.name
        if not isinstance(e, dict) or set(e) != LIBRARY_FIELDS:
            raise SlackEngineError(f"{where}: entry must have exactly {sorted(LIBRARY_FIELDS)}")
        if e["service_id"] != file.stem:
            raise SlackEngineError(f"{where}: service_id {e['service_id']!r} must match the filename")
        if e["status"] not in STATUSES:
            raise SlackEngineError(f"{where}: status must be one of {STATUSES}")
        if e["direction"] not in DIRECTIONS:
            raise SlackEngineError(f"{where}: direction must be pull, push, adaptive, or null")
        if not isinstance(e["aliases"], list) or not isinstance(e["notes"], list):
            raise SlackEngineError(f"{where}: aliases and notes must be lists")
        check_message_text(e["message"], where)
        for name in [e["service_id"], e["display_name"], *e["aliases"]]:
            key = normalize_name(name)
            owner = aliases_seen.get(key)
            if owner and owner != e["service_id"]:
                raise SlackEngineError(f"{where}: alias {name!r} already belongs to {owner}")
            aliases_seen[key] = e["service_id"]
        library[e["service_id"]] = e
    return library


def _alias_index(library: dict[str, dict]) -> dict[str, str]:
    index = {}
    for sid, e in library.items():
        for name in [sid, e["display_name"], *e["aliases"]]:
            index[normalize_name(name)] = sid
    return index


def match_service(name: str, library: dict[str, dict]) -> tuple[str, str] | None:
    """(service_id, 'exact'|'alias'|'close') or None. Ladder: exact id, alias,
    single unambiguous close match (difflib ratio >= 0.85, runner-up > 0.05 behind)."""
    import difflib
    key = normalize_name(name)
    if not key:
        return None
    if key in library:
        return key, "exact"
    index = _alias_index(library)
    if key in index:
        return index[key], "alias"
    scored = sorted(((difflib.SequenceMatcher(None, key, cand).ratio(), sid)
                     for cand, sid in index.items()), reverse=True)
    best = [(r, sid) for r, sid in scored if r >= CLOSE_MATCH_MIN]
    if not best:
        return None
    top_ratio, top_sid = best[0]
    rivals = [sid for r, sid in best[1:] if sid != top_sid and top_ratio - r < CLOSE_MATCH_GAP]
    if rivals:
        return None
    return top_sid, "close"


# ---------------------------------------------------------------- reconcile

def _assign_service_ids(parents: list[dict], library: dict[str, dict]) -> None:
    for p in parents:
        if p["kind"] != "service":
            p["service_id"] = p["kind"].replace("_", "-")
            continue
        m = match_service(p["label"], library)
        p["service_id"] = m[0] if m else None


def _declared_services(root: Path, slug: str) -> dict[str, dict]:
    exp = common.load_expected(root, slug)
    return dict((exp or {}).get("services", {}))


def reconcile(root: Path, slug: str, *, library: dict[str, dict] | None = None) -> dict:
    library = library if library is not None else load_library()
    reg = load_channel(root, slug)
    parents = [p for p in reg["parents"] if p["kind"] == "service" and not p["ambiguous"]]
    declared = _declared_services(root, slug)

    thread_by_key: dict[str, dict] = {}
    for p in parents:
        thread_by_key[normalize_name(p["label"])] = p
        if p["service_id"]:
            thread_by_key.setdefault(p["service_id"], p)

    matched, declared_without = [], []
    claimed: set[str] = set()
    for service, decl in declared.items():
        candidates = [normalize_name(service)]
        if isinstance(decl, dict) and decl.get("slack_label"):
            candidates.insert(0, normalize_name(str(decl["slack_label"])))
        lm = match_service(service, library)
        if lm:
            candidates.append(lm[0])
        hit = next((thread_by_key[c] for c in candidates if c in thread_by_key), None)
        if hit is None:
            declared_without.append(service)
            continue
        claimed.add(hit["ts"])
        matched.append({"service": service, "thread_ts": hit["ts"], "label": hit["label"],
                        "library_entry": hit["service_id"]})

    without_decl, unmatched = [], []
    for p in parents:
        if p["ts"] in claimed:
            continue
        item = {"thread_ts": p["ts"], "label": p["label"], "library_entry": p["service_id"]}
        (without_decl if p["service_id"] else unmatched).append(item)

    return {"slug": slug, "matched": matched,
            "declared_without_thread": declared_without,
            "threads_without_declaration": without_decl,
            "unmatched_threads": unmatched,
            "ambiguous_labels": sorted({p["label"].casefold() for p in reg["parents"]
                                        if p["ambiguous"]})}


# ------------------------------------------------------------ kickoff plan

DRAFT_KINDS = ("kickoff", "reply")
DRAFT_OUTCOMES = ("drafted", "already_exists", "error")


def drafts_path(root: Path, slug: str) -> Path:
    return slack_dir(root, slug) / "drafts.json"


def load_drafts(root: Path, slug: str) -> list[dict]:
    path = drafts_path(root, slug)
    return common.read_json(path)["drafts"] if path.exists() else []


def render_message(entry: dict, *, company_name: str, instructions_url: str | None) -> str:
    text = entry["message"]
    if "{instructions_url}" in text and not instructions_url:
        raise SlackEngineError("entry needs {instructions_url} but no canvas is recorded")
    return (text.replace("{company_name}", company_name)
                .replace("{instructions_url}", instructions_url or "")).strip()


def kickoff_plan(root: Path, slug: str, *, library: dict[str, dict] | None = None,
                 force: bool = False) -> dict:
    library = library if library is not None else load_library()
    reg = load_channel(root, slug)
    rec = reconcile(root, slug, library=library)
    declared_ts = {m["thread_ts"] for m in rec["matched"]}
    drafted_ts = {d["thread_ts"] for d in load_drafts(root, slug)
                  if d["kind"] == "kickoff" and d["outcome"] in ("drafted", "already_exists")}
    canvas = reg.get("instructions_canvas") or {}
    instructions_url = canvas.get("permalink")

    items = []
    for p in reg["parents"]:
        item = {"thread_ts": p["ts"], "label": p["label"], "kind": p["kind"],
                "service_id": p["service_id"], "status": None, "message": None,
                "undeclared": p["kind"] == "service" and p["ts"] not in declared_ts,
                "draft_entry": None, "reason": None}
        entry = library.get(p["service_id"]) if p["service_id"] else None
        if p["ambiguous"]:
            item["status"], item["reason"] = "ambiguous", "duplicate parent label"
        elif p["ts"] in drafted_ts and not force:
            item["status"] = "already-drafted"
        elif entry is None or entry["status"] != "approved":
            item["status"] = "missing"
            item["draft_entry"] = entry  # a status: draft entry to resume iterating on
            item["reason"] = ("no library entry" if entry is None
                              else "library entry is still a draft")
        else:
            try:
                item["message"] = render_message(entry, company_name=reg["company_name"],
                                                 instructions_url=instructions_url)
                item["status"] = "planned"
            except SlackEngineError as exc:
                item["status"], item["reason"] = "blocked", str(exc)
        items.append(item)

    counts = {k: 0 for k in ("planned", "missing", "blocked", "already-drafted", "ambiguous")}
    for i in items:
        counts[i["status"]] += 1
    return {"slug": slug, "channel_id": reg["channel_id"], "items": items, "counts": counts,
            "declared_without_thread": rec["declared_without_thread"]}


def record_draft(root: Path, slug: str, *, kind: str, thread_ts: str, text: str,
                 service_id: str | None = None, outcome: str = "drafted",
                 reason: str | None = None, library: dict[str, dict] | None = None) -> dict:
    import hashlib
    if kind not in DRAFT_KINDS:
        raise SlackEngineError(f"kind must be one of {DRAFT_KINDS}")
    if outcome not in DRAFT_OUTCOMES:
        raise SlackEngineError(f"outcome must be one of {DRAFT_OUTCOMES}")
    if not TS_RE.fullmatch(thread_ts):
        raise SlackEngineError("thread_ts must be a Slack timestamp")
    load_channel(root, slug)  # must be registered
    entry_version = None
    if service_id:
        library = library if library is not None else load_library()
        entry = library.get(service_id)
        entry_version = entry["updated_at"] if entry else None
    receipt = {"kind": kind, "thread_ts": thread_ts, "service_id": service_id,
               "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
               "library_entry_updated_at": entry_version, "outcome": outcome,
               "reason": reason, "created_at": common.iso_now()}
    drafts = load_drafts(root, slug)
    drafts.append(receipt)
    common.write_json(drafts_path(root, slug), {"drafts": drafts})
    return receipt


# --------------------------------------------------------------------- inbox

CONTEXT_MESSAGES = 5
COMPLETE_REACTION = "white_check_mark"


def _conversations(messages: list[dict]) -> dict[str, list[dict]]:
    """conversation key (parent ts, or a solo top-level's own ts) -> messages, ascending."""
    convs: dict[str, list[dict]] = {}
    for m in messages:
        convs.setdefault(m["thread_ts"] or m["ts"], []).append(m)
    for key in convs:
        convs[key].sort(key=lambda m: ts_key(m["ts"]))
    return convs


def inbox(root: Path, slug: str) -> dict:
    reg = load_channel(root, slug)
    snap = load_snapshot(root, slug)
    owner = reg["owner_user_id"]
    # Teammate semantics: a colleague's message means the conversation is NOT
    # waiting on the owner (ball in the client's court) and is never itself
    # "unanswered" — but it is not the owner's reply either: the floor that
    # clears earlier client messages (and their mention) is the owner's own
    # last message or an ack, never a teammate's. Pre-teammate registrations
    # have no key and behave exactly as before.
    ours = {owner, *reg.get("teammate_user_ids", [])}
    parents = {p["ts"]: p for p in reg["parents"]}
    now = common.utc_now().timestamp()
    items = []
    for key, msgs in _conversations(snap["messages"]).items():
        head = msgs[0]
        if head["thread_ts"] is None and COMPLETE_REACTION in head["reactions"]:
            continue
        last = msgs[-1]
        if last["is_bot"] or last["user_id"] in ours:
            continue
        owner_last = max((m["ts"] for m in msgs if m["user_id"] == owner),
                         key=ts_key, default=None)
        acked = reg["acked"].get(key)
        floor = max((t for t in (owner_last, acked) if t), key=ts_key, default=None)
        unanswered = [m for m in msgs if floor is None or ts_key(m["ts"]) > ts_key(floor)]
        unanswered = [m for m in unanswered if not m["is_bot"] and m["user_id"] not in ours]
        if not unanswered:
            continue
        parent = parents.get(key)
        items.append({
            "slug": slug, "thread_ts": key,
            "kind": parent["kind"] if parent else "top_level",
            "label": parent["label"] if parent else None,
            "service_id": parent["service_id"] if parent else None,
            "last_author": last["user_name"],
            "waiting_since_ts": unanswered[0]["ts"],
            "age_hours": round(max(0.0, now - float(unanswered[0]["ts"])) / 3600, 1),
            "unanswered": len(unanswered),
            "mentioned": any(mentions_user(m["text"], owner) for m in unanswered),
            "context": [{"ts": m["ts"], "user_name": m["user_name"], "text": m["text"]}
                        for m in msgs[-CONTEXT_MESSAGES:]],
        })
    items.sort(key=lambda i: (not i["mentioned"], ts_key(i["waiting_since_ts"])))
    return {"slug": slug, "channel_id": reg["channel_id"], "partial": not snap["complete"],
            "taken_at": snap["taken_at"], "items": items}


def ack(root: Path, slug: str, thread_ts: str) -> dict:
    reg = load_channel(root, slug)
    snap = load_snapshot(root, slug)
    convs = _conversations(snap["messages"])
    if thread_ts not in convs:
        raise SlackEngineError(f"no conversation {thread_ts} in {slug}'s snapshot")
    reg["acked"][thread_ts] = convs[thread_ts][-1]["ts"]
    save_channel(root, slug, reg)
    return {"slug": slug, "thread_ts": thread_ts, "acked_through": reg["acked"][thread_ts]}


def inbox_all(root: Path) -> dict:
    """Thin fleet loop over inbox(); one broken company never hides another."""
    companies, items, failed = [], [], {}
    for slug in common.list_companies(root):
        if not channel_path(root, slug).exists():
            continue
        companies.append(slug)
        try:
            items.extend(inbox(root, slug)["items"])
        except Exception as exc:  # noqa: BLE001 — failure isolation
            failed[slug] = str(exc)
    items.sort(key=lambda i: (not i["mentioned"], ts_key(i["waiting_since_ts"])))
    return {"companies": companies, "items": items, "failed": failed}


# --------------------------------------------------------------------- voice

VOICE_DIRNAME = ".voice"
# Slack's wire form is <@Uid>, but the Claude Slack connector transcribes
# mentions as <@Uid|Display Name>. Group 1 is always the bare user id.
MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
RECENT_DAYS = 90


def voice_dir(root: Path) -> Path:
    return root / VOICE_DIRNAME


def _voice_rows(root: Path) -> list[dict]:
    path = voice_dir(root) / "messages.jsonl"
    if not path.exists():
        return []
    import json
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_voice_rows(root: Path, rows: list[dict]) -> None:
    import json
    voice_dir(root).mkdir(parents=True, exist_ok=True)
    path = voice_dir(root) / "messages.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(path)


def _harvest_state(root: Path) -> dict:
    path = voice_dir(root) / "harvest-state.json"
    return common.read_json(path) if path.exists() else {"channels": {}}


def mentions_user(text: str, user_id: str) -> bool:
    return any(m.group(1) == user_id for m in MENTION_RE.finditer(text))


def redact(text: str, names: dict[str, str]) -> str:
    text = SECRET_RE.sub("[redacted]", text)
    return MENTION_RE.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), text)


def voice_harvest(root: Path, slug: str) -> dict:
    from datetime import datetime, timezone
    reg = load_channel(root, slug)
    snap = load_snapshot(root, slug)
    owner = reg["owner_user_id"]
    names = {m["user_id"]: m["user_name"] for m in snap["messages"]}
    state = _harvest_state(root)
    hwm = state["channels"].get(reg["channel_id"])
    rows = _voice_rows(root)
    seen = {(r["channel_id"], r["ts"]) for r in rows}
    added = 0
    for key, msgs in _conversations(snap["messages"]).items():
        for i, m in enumerate(msgs):
            if m["user_id"] != owner:
                continue
            if hwm and ts_key(m["ts"]) <= ts_key(hwm):
                continue
            if (reg["channel_id"], m["ts"]) in seen:
                continue
            replied_to = None
            if m["thread_ts"] is not None:
                prev = next((x for x in reversed(msgs[:i]) if x["user_id"] != owner), None)
                if prev is not None:
                    replied_to = {"ts": prev["ts"], "user_name": prev["user_name"],
                                  "text": redact(prev["text"], names)}
            sent_at = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc)
            rows.append({"slug": slug, "channel_id": reg["channel_id"], "ts": m["ts"],
                         "thread_ts": m["thread_ts"], "sent_at": common.iso(sent_at),
                         "text": redact(m["text"], names), "replied_to": replied_to,
                         "tags": [], "harvested_at": common.iso_now()})
            seen.add((reg["channel_id"], m["ts"]))
            added += 1
    rows.sort(key=lambda r: (r["channel_id"], ts_key(r["ts"])))
    _write_voice_rows(root, rows)
    owner_ts = [m["ts"] for m in snap["messages"] if m["user_id"] == owner]
    if owner_ts:
        newest = max(owner_ts, key=ts_key)
        if hwm is None or ts_key(newest) > ts_key(hwm):
            state["channels"][reg["channel_id"]] = newest
    common.write_json(voice_dir(root) / "harvest-state.json", state)
    return {"slug": slug, "added": added, "total_rows": len(rows),
            "channel_hwm_ts": state["channels"].get(reg["channel_id"])}


def voice_tag(root: Path, *, channel_id: str, ts: str, tags: list[str]) -> dict:
    rows = _voice_rows(root)
    for r in rows:
        if r["channel_id"] == channel_id and r["ts"] == ts:
            r["tags"] = sorted({t.strip() for t in tags if t.strip()})
            _write_voice_rows(root, rows)
            return r
    raise SlackEngineError(f"no harvested message {channel_id}/{ts}")


def voice_untagged(root: Path, *, limit: int = 50) -> dict:
    """Harvested rows still waiting for the skill to tag them (oldest first)."""
    rows = [r for r in _voice_rows(root) if not r["tags"]]
    rows.sort(key=lambda r: (r["channel_id"], ts_key(r["ts"])))
    return {"untagged_total": len(rows),
            "rows": [{"slug": r["slug"], "channel_id": r["channel_id"], "ts": r["ts"],
                      "replied_to": r["replied_to"], "text": r["text"]}
                     for r in rows[:limit]]}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def voice_select(root: Path, *, intent: str, service: str | None = None,
                 context: str = "", limit: int = 5) -> dict:
    from datetime import timedelta
    style_path = voice_dir(root) / "style.md"
    style = style_path.read_text(encoding="utf-8") if style_path.exists() else None
    ctx_words = _words(context)
    cutoff = common.iso(common.utc_now() - timedelta(days=RECENT_DAYS))
    scored = []
    for r in _voice_rows(root):
        tags = set(r["tags"])
        if "exclude" in tags:
            continue
        score = 0.0
        if intent in tags:
            score += 3
        if service and service in tags:
            score += 2
        if ctx_words and r["replied_to"]:
            rw = _words(r["replied_to"]["text"])
            if rw:
                score += 3 * len(ctx_words & rw) / len(ctx_words | rw)
        if r["sent_at"] >= cutoff:
            score += 1
        scored.append((score, ts_key(r["ts"]), r))
    scored.sort(key=lambda x: (-x[0], tuple(-v for v in x[1])))
    examples = [{"ts": r["ts"], "slug": r["slug"], "score": round(s, 3), "tags": r["tags"],
                 "replied_to": r["replied_to"], "text": r["text"]}
                for s, _, r in scored[:limit]]
    return {"intent": intent, "service": service, "style": style, "examples": examples}


# ----------------------------------------------------------------------- CLI

def _emit(obj) -> None:
    import json
    print(json.dumps(obj, indent=2, sort_keys=True))


def _split_ids(csv: str) -> list[str]:
    return [x for x in (part.strip() for part in csv.split(",")) if x]


def _read_text(path_or_dash: str) -> str:
    import sys
    if path_or_dash == "-":
        return sys.stdin.read()
    return Path(path_or_dash).read_text(encoding="utf-8")


def build_parser():
    import argparse
    ap = argparse.ArgumentParser(
        description="Slack engine CLI (connector-only): state + logic for the slack-kickoff, "
                    "slack-inbox and harvest-voice skills. Never talks to Slack.")
    ap.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    ap.add_argument("--library", default=str(LIBRARY_DIR),
                    help="kickoff-copy directory (default knowledge/kickoff-copy)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register", help="register a company's Slack channel")
    p.add_argument("slug"); p.add_argument("--channel-url", required=True)
    p.add_argument("--owner-user-id", required=True); p.add_argument("--company-name", required=True)
    p.add_argument("--channel-type", choices=CHANNEL_TYPES, required=True)
    p.add_argument("--teammates", default="",
                   help="comma-separated Slack user IDs of micro1 colleagues in the channel")

    p = sub.add_parser("set-teammates", help="replace the registration's colleague list")
    p.add_argument("slug")
    p.add_argument("--teammates", required=True,
                   help="comma-separated Slack user IDs; an empty string clears the list")

    p = sub.add_parser("record-canvas", help="record the EDP Instructions canvas reference")
    p.add_argument("slug"); p.add_argument("--canvas-id", required=True)
    p.add_argument("--title", required=True); p.add_argument("--permalink", required=True)
    p.add_argument("--replace", action="store_true")

    p = sub.add_parser("read-plan", help="what the skill must read from Slack next")
    p.add_argument("slug")

    p = sub.add_parser("ingest", help="validate + merge a snapshot.new.json (consumed on success)")
    p.add_argument("slug"); p.add_argument("snapshot_file")

    p = sub.add_parser("reconcile", help="declared services vs discovered threads")
    p.add_argument("slug")

    p = sub.add_parser("kickoff-plan", help="one item per parent: planned/missing/blocked/…")
    p.add_argument("slug"); p.add_argument("--force", action="store_true")

    p = sub.add_parser("record-draft", help="append a draft receipt")
    p.add_argument("slug"); p.add_argument("--kind", choices=DRAFT_KINDS, required=True)
    p.add_argument("--thread-ts", required=True)
    p.add_argument("--text-file", required=True, help="path to the exact draft text, or - for stdin")
    p.add_argument("--service"); p.add_argument("--outcome", choices=DRAFT_OUTCOMES, default="drafted")
    p.add_argument("--reason")

    p = sub.add_parser("inbox", help="conversations waiting on the owner")
    p.add_argument("slug", nargs="?"); p.add_argument("--all", action="store_true")

    p = sub.add_parser("ack", help="mark a conversation handled without a draft")
    p.add_argument("slug"); p.add_argument("--thread-ts", required=True)

    sub.add_parser("validate-library", help="load every kickoff entry and run the content checks")

    p = sub.add_parser("voice-harvest", help="append the owner's messages to the voice store")
    p.add_argument("slug")

    p = sub.add_parser("voice-untagged", help="harvested rows still without tags")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("voice-tag", help="tag one harvested message")
    p.add_argument("--channel-id", required=True); p.add_argument("--ts", required=True)
    p.add_argument("--tags", required=True, help="comma-separated")

    p = sub.add_parser("voice-select", help="style guide + best examples for a draft")
    p.add_argument("--intent", required=True); p.add_argument("--service")
    p.add_argument("--context-file", help="the message being answered (path or -)")
    p.add_argument("--limit", type=int, default=5)
    return ap


def main(argv=None) -> int:
    import json
    import sys
    args = build_parser().parse_args(argv)
    root = Path(args.root)

    def lib():
        return load_library(Path(args.library))

    try:
        if args.cmd == "register":
            out = register(root, args.slug, channel_url=args.channel_url,
                           owner_user_id=args.owner_user_id, company_name=args.company_name,
                           channel_type=args.channel_type,
                           teammate_user_ids=_split_ids(args.teammates))
        elif args.cmd == "set-teammates":
            out = set_teammates(root, args.slug, _split_ids(args.teammates))
        elif args.cmd == "record-canvas":
            out = record_canvas(root, args.slug, canvas_id=args.canvas_id, title=args.title,
                                permalink=args.permalink, replace=args.replace)
        elif args.cmd == "read-plan":
            out = read_plan(root, args.slug)
        elif args.cmd == "ingest":
            path = Path(args.snapshot_file)
            try:
                snap = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SlackEngineError(f"cannot read snapshot {path}: {exc}")
            out = ingest(root, args.slug, snap, library=lib())
            path.unlink()
        elif args.cmd == "reconcile":
            out = reconcile(root, args.slug, library=lib())
        elif args.cmd == "kickoff-plan":
            out = kickoff_plan(root, args.slug, library=lib(), force=args.force)
        elif args.cmd == "record-draft":
            out = record_draft(root, args.slug, kind=args.kind, thread_ts=args.thread_ts,
                               text=_read_text(args.text_file), service_id=args.service,
                               outcome=args.outcome, reason=args.reason,
                               library=lib() if args.service else None)
        elif args.cmd == "inbox":
            if args.all:
                out = inbox_all(root)
            elif args.slug:
                out = inbox(root, args.slug)
            else:
                raise SlackEngineError("inbox needs a slug or --all")
        elif args.cmd == "ack":
            out = ack(root, args.slug, args.thread_ts)
        elif args.cmd == "validate-library":
            library = lib()
            out = {"library": args.library, "entries": len(library),
                   "approved": sum(e["status"] == "approved" for e in library.values()),
                   "draft": sum(e["status"] == "draft" for e in library.values())}
        elif args.cmd == "voice-harvest":
            out = voice_harvest(root, args.slug)
        elif args.cmd == "voice-untagged":
            out = voice_untagged(root, limit=args.limit)
        elif args.cmd == "voice-tag":
            out = voice_tag(root, channel_id=args.channel_id, ts=args.ts,
                            tags=args.tags.split(","))
        elif args.cmd == "voice-select":
            context = _read_text(args.context_file) if args.context_file else ""
            out = voice_select(root, intent=args.intent, service=args.service,
                               context=context, limit=args.limit)
        else:  # pragma: no cover
            raise SlackEngineError(f"unknown command {args.cmd}")
    except SlackEngineError as exc:
        print(json.dumps({"error": str(exc), "error_type": "contract_error"}), file=sys.stderr)
        return 2
    _emit(out)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
