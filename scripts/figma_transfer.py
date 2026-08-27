#!/usr/bin/env python3
"""Figma -> Azure transfer CLI (VM REST puller + azcopy; VM family).

Figma has no rclone backend and no bulk export of any kind — the REST API
serves file JSON, comments, versions, presigned asset URLs and published
library metadata, one file at a time, behind a rate limit whose Tier-1
bucket (file JSON, node JSON and image renders SHARE it) caps at 20
requests/min even on Enterprise. A real workspace is therefore a
multi-hour-to-multi-day metered walk — the github/zoho family shape: this
script reuses transfer_engine.py's VM lifecycle (create-vm / allow-network /
check-azure / teardown run the engine functions verbatim) but owns the pull
layer — scripts/figma_vm_pull.py is pushed to the VM and runs the whole pull
there in tmux, azcopying each unit up as it completes, to
<slug>-raw/<dest-prefix>/.

Honesty note on the transport: the presigned CDN URLs Figma hands back for
fills/renders WOULD work with the vimeo/zoom server-side-copy path (plain
GET, no auth header). It is deliberately not used — the corpus majority
(document JSON) must be token-fetched and staged regardless, and one
transport keeps one verify story.

Subcommands: discover / plan / create-vm / allow-network / write-dest /
check-azure / write-creds / probe / transfer / status / verify / teardown.
Source location flag: --team-ids (comma-separated Figma team ids — there is
NO API that lists an org's teams, so the client reads them out of their
file-browser URLs: figma.com/files/team/<TEAM_ID>/...; required for
plan/create-vm/probe, later read from VM tags). See
.claude/skills/figma-azure-transfer/SKILL.md.

Secrets: ONE value arrives on stdin — the personal access token (client-made,
from a Full/Dev seat; a View/Collab seat's token gets 6 Tier-1 calls per
MONTH and is unusable). It travels only over ssh stdin into a 600 env file
on the VM — never argv, tags, logs or laptop files (probe holds it in
process memory only). Team ids are NOT secret (they are in every URL) and
ride the normal flag -> VM tag path. The dest SAS goes the same env-file
way. All die with the VM at teardown; the client revokes the token after
verification.

The day-one stalls (both caught before any billable resource): a token from
the WRONG SEAT (429 with X-Figma-Rate-Limit-Type=low), and a token whose
owner is not a member of a named team (403/404 on the team walk — which
Figma does not distinguish from "no such team").

Verify runs on the LAPTOP (the VM is normally gone by then): the
completeness authority is the manifest.json the pull uploaded, so no Figma
access is needed — just a blob listing, which from this machine's external
IP uses phases.ip_rule_ensure + an rl account SAS (the zoom/qwilr laptop
path; allow-network stays VM-only).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import phases  # noqa: E402
import transfer_engine as eng  # noqa: E402
import figma_vm_pull as puller  # noqa: E402  (import-safe; pure helpers)

SPEC = eng.Spec(
    source_name="figma",
    vm_prefix="xfer-figma-",
    purpose="figma-transfer",
    loc_tag="figma_team_ids",
    loc_argname="team_ids",
    loc_required=True,
    default_dest_prefix="figma-export",
    authorize_target="",  # no rclone OAuth flow — PAT on stdin
    remote_type="",       # no rclone source remote; REST is the source
    extra_cli_opts=[
        {"flag": "--plan", "argname": "plan", "tag": "figma_plan",
         "conf_key": "",
         "help": "Figma plan: starter | pro | org | enterprise (sets the "
                 "puller's pacing schedule; rides the figma_plan VM tag)"}],
    default_os_disk_gb=512,  # staging holds the corpus before azcopy
)

PULLER_PY = Path(__file__).resolve().parent / "figma_vm_pull.py"
XFER_DIR = f"/home/{eng.ADMIN_USER}/xfer-figma"
DEST_DIR = f"{XFER_DIR}/dest"
LOG_FILE = f"{XFER_DIR}/pull-figma.log"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/xfer"
FIGMA_ENV = f"{ENV_DIR}/figma.env"
DEST_ENV = f"{ENV_DIR}/dest-figma.env"
X_MS_VERSION = "2021-08-06"
FIGMA_API = "api.figma.com"
AZURE_TAG_VALUE_MAX = 240  # ARM caps tag values at 256; leave headroom

_sleep = time.sleep  # seam so tests can record/skip waits


def _http(req: urllib.request.Request, timeout: int = 60):
    """Single transport seam (tests stub this; production never branches)."""
    return urllib.request.urlopen(req, timeout=timeout)


def validate_team_ids(raw: str) -> str:
    """PURE. Canonical comma-joined team-id list. Team ids come from the
    file-browser URL (figma.com/files/team/<TEAM_ID>/...) and are all
    digits; anything else is a mis-paste. The whole list rides ONE VM tag,
    and ARM caps tag values at 256 chars — refuse past the headroom line
    rather than silently truncating an engagement's scope."""
    ids = [t.strip() for t in (raw or "").split(",") if t.strip()]
    if not ids:
        raise common.HarnessError(
            "--team-ids is empty — supply the comma-separated team ids "
            "from the client's file-browser URLs "
            "(figma.com/files/team/<TEAM_ID>/...)")
    for t in ids:
        if not t.isdigit():
            raise common.HarnessError(
                f"team id {t!r} is not numeric — copy the number between "
                "/team/ and the team name in the client's Figma URL")
    joined = ",".join(ids)
    if len(joined) > AZURE_TAG_VALUE_MAX:
        raise common.HarnessError(
            f"the team-id list is {len(joined)} chars — past the "
            f"{AZURE_TAG_VALUE_MAX}-char VM-tag ceiling. Run one cycle per "
            "batch of teams instead (same --dest-prefix; units are keyed "
            "per team, so batches never collide).")
    return joined


# ── VM-side plumbing ─────────────────────────────────────────────────────────

def _write_env(ip: str, path: str, content: str, dry_run: bool) -> None:
    """600 env file on the VM; content rides ssh stdin only."""
    eng.run_ssh(ip, f"umask 077 && mkdir -p {ENV_DIR} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _push_puller(ip: str, dry_run: bool) -> None:
    """Fresh copy every transfer so harness upgrades propagate."""
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > "
                    f"{XFER_DIR}/figma_vm_pull.py",
                stdin_data=PULLER_PY.read_text(), dry_run=dry_run)


def _team_ids(vm: dict, args) -> str:
    raw = (getattr(args, "team_ids", None)
           or (vm.get("tags") or {}).get(SPEC.loc_tag))
    if not raw:
        raise common.HarnessError(
            "team ids unknown: VM has no figma_team_ids tag — pass "
            "--team-ids")
    return validate_team_ids(raw)


def _plan(vm: dict, args) -> str:
    return (getattr(args, "plan", None)
            or (vm.get("tags") or {}).get("figma_plan") or "")


def _dest_prefix(vm: dict, args) -> str:
    return (getattr(args, "dest_prefix", None)
            or (vm.get("tags") or {}).get("dest_prefix")
            or SPEC.default_dest_prefix)


# ── laptop-side Figma client (probe only; token stays in process memory) ─────

class FigmaProbeError(Exception):
    """Non-fatal Figma API refusal — the caller decides what it means for
    the family being probed."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def read_token(dry_run: bool) -> str:
    """The PAT arrives on stdin ONLY (heredoc), one line — argv is
    world-readable via ps, env leaks into child processes, files persist."""
    data = "" if sys.stdin.isatty() else sys.stdin.read()
    lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
    if len(lines) == 1:
        return lines[0]
    if dry_run and not lines:
        return "<figma-pat>"
    raise common.HarnessError(
        "stdin must be exactly 1 line: the Figma personal access token — "
        "pipe it: probe <slug> --team-ids 123 <<'EOF' ... EOF")


def figma_get(token: str, path: str, params: dict | None = None,
              dry_run: bool = False, pace_s: float = 0.0):
    """One GET -> parsed JSON. The probe-side slice of the puller's
    behavior: 429 honors Retry-After (a 429 whose X-Figma-Rate-Limit-Type
    is `low` is the wrong-seat diagnosis and aborts immediately — sleeping
    cannot help a 6-calls-per-MONTH budget); 5xx retries with backoff;
    403/404 raise FigmaProbeError for the caller to record. An invalid or
    expired token is 403 (not 401) — Figma does not say which."""
    url = f"https://{FIGMA_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    if dry_run:
        print(f"DRY-RUN: GET {url} (X-Figma-Token: token-redacted)")
        return None
    if pace_s:
        _sleep(pace_s)
    last: Exception | None = None
    for attempt in range(6):
        req = urllib.request.Request(url, headers={
            "X-Figma-Token": token,
            "Accept": "application/json",
            "User-Agent": "cdp-harness-figma-transfer/1.0"})
        try:
            with _http(req, timeout=90) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                hdrs = e.headers or {}
                if (hdrs.get("X-Figma-Rate-Limit-Type")
                        or "").strip().lower() == "low":
                    raise common.HarnessError(
                        "429 with X-Figma-Rate-Limit-Type=low — the token "
                        "owner holds a View/Collab seat (6 Tier-1 calls "
                        "per MONTH). The client must issue the token from "
                        "a Full or Dev seat; retrying cannot help.")
                ra = (hdrs.get("Retry-After") or "").strip()
                _sleep(int(ra) if ra.isdigit() else min(2 ** attempt, 60))
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise FigmaProbeError(e.code, f"HTTP {e.code} on {path}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    status = getattr(last, "code", 0) or 0
    raise FigmaProbeError(status, f"gave up on {path}: {last}")


# ── azure listing (verify only; laptop path) ─────────────────────────────────
# Local copies of the established laptop-side blob-listing pair — kept here
# rather than imported so the VM-family CLIs stay import-independent of the
# local-pull family.

def _container_url(cfg: dict) -> str:
    return (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")


def azure_get(url: str) -> bytes:
    """GET with retries. A 403 early in a run is usually IP-rule
    propagation (CLAUDE.md lore) -- wait and retry, never re-mint the
    SAS for it."""
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url,
                                     headers={"x-ms-version": X_MS_VERSION})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                _sleep(15 * (attempt + 1))  # propagation, not a bad SAS
                continue
            if e.code >= 500:
                _sleep(1 + attempt)
                continue
            raise common.HarnessError(f"blob GET failed: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            _sleep(1 + attempt)
    raise common.HarnessError(f"blob GET failed after retries: {last}")


def azure_list_blobs(cfg: dict, sas: str, prefix: str,
                     dry_run: bool) -> dict[str, dict]:
    """One marker-paginated listing of the dest prefix -> {name: {size}}.
    Content-Length is what Azure actually committed — verify's ground
    truth."""
    if dry_run:
        print(f"DRY-RUN: GET {_container_url(cfg)}"
              f"?restype=container&comp=list&prefix={prefix}/&<sas-redacted>")
        return {}
    blobs: dict[str, dict] = {}
    marker = ""
    while True:
        url = (f"{_container_url(cfg)}?restype=container&comp=list"
               f"&maxresults=5000"
               f"&prefix={urllib.parse.quote(prefix + '/', safe='')}")
        if marker:
            url += f"&marker={urllib.parse.quote(marker, safe='')}"
        url += "&" + sas
        root = ET.fromstring(azure_get(url))
        for blob in root.iter("Blob"):
            name = blob.findtext("Name")
            props = blob.find("Properties")
            if name:
                blobs[name] = {
                    "size": int((props.findtext("Content-Length") or 0)
                                if props is not None else 0),
                }
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return blobs


def compare_manifest_to_blobs(manifest: dict, blobs: dict,
                              prefix: str) -> dict:
    """Pure. Certifies STAGED -> CONTAINER completeness against the uploaded
    manifest (Figma -> staged completeness is the puller's own exit code +
    failed_units — verify surfaces that list verbatim).

    Per successful unit: the .cdp-complete marker blob must exist, and the
    per-unit container byte sum must be >= the staged bytes. LESS than
    staged = a partial upload = failure. MORE happens legitimately when a
    --refresh re-pull wrote a shorter file and --overwrite=false kept the
    older sibling — reported as stale_extra, informational.

    NO source-size claim is made, and here that is total: Figma publishes
    no byte size for anything — no file size, no asset size, nothing. The
    first honest byte number in this engagement's life is the manifest's
    total_staged_bytes. decomposed_files, fill_errors and render_nulls are
    surfaced as informational quality signals, never failures.
    """
    results = manifest.get("results", [])
    missing_markers: list[str] = []
    short_uploads: list[dict] = []
    stale_extra: list[str] = []

    def rollup(sub: str) -> int:
        return sum(b["size"] for n, b in blobs.items() if n.startswith(sub))

    for r in results:
        if r.get("status") not in ("ok", "skipped-complete"):
            continue
        sub = f"{prefix}/{r['unit']}/"
        staged = r.get("bytes") or 0
        if f"{sub}.cdp-complete" not in blobs:
            missing_markers.append(sub)
            continue
        landed = rollup(sub)
        if staged and landed < staged:
            short_uploads.append({"prefix": sub, "staged": staged,
                                  "landed": landed})
        elif staged and landed > staged:
            stale_extra.append(sub)

    failed = manifest.get("failed_units", [])
    skipped = manifest.get("skipped_units", [])
    ok = not failed and not missing_markers and not short_uploads
    return {
        "ok": ok,
        "source": manifest.get("source"),
        "team_ids": manifest.get("team_ids"),
        "unit_count": manifest.get("unit_count"),
        "total_staged_bytes": manifest.get("total_staged_bytes"),
        "api_calls": manifest.get("api_calls"),
        "failed_units": failed,
        "skipped_units": skipped,
        "missing_markers": missing_markers,
        "short_uploads": short_uploads,
        "stale_extra": stale_extra,
        "decomposed_files": manifest.get("decomposed_files") or [],
        "fill_errors": manifest.get("fill_errors") or 0,
        "render_nulls": manifest.get("render_nulls") or 0,
        "editor_types": (manifest.get("context") or {}).get("editor_types"),
        "hint": None if ok else
        "failed_units / missing markers / short uploads: re-run transfer "
        "(per-unit .cdp-complete markers skip finished files, azcopy "
        "--overwrite=false skips landed blobs), then re-verify. "
        "stale_extra alone is informational: no-overwrite kept an earlier "
        "pass's longer file. skipped_units are deliberate and never a "
        "failure — read their reasons (no-access-or-missing = Figma does "
        "not say whether the file was invisible or deleted).",
    }


# ── subcommands (the engine covers create-vm/allow-network/check-azure) ──────

def cmd_plan(root: Path, args) -> dict:
    result = eng.cmd_plan(SPEC, root, args)
    result["note"] = (
        result["note"] + " Figma-specific: the rate limit is the clock — "
        "Tier 1 (file JSON + renders) caps at 20/min even on Enterprise, "
        "so quote timelines off probe's estimate, never off byte counts "
        "(Figma publishes none).")
    return result


def cmd_write_dest(root: Path, args) -> dict:
    """racwl SAS -> rclone [azure] section AND azcopy dest-figma.env, both
    on-VM."""
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    prefix = _dest_prefix(vm, args)
    sas, expiry = eng.mint_container_sas(cfg, args.sas_days, args.dry_run)
    base = _container_url(cfg)
    eng.write_conf_section(vm["public_ip"], "azure",
                           f"[azure]\ntype = azureblob\n"
                           f"sas_url = {base}?{sas}\n",
                           dry_run=args.dry_run)
    # values are SINGLE-QUOTED: the SAS contains '&', and sourcing an
    # unquoted VAR=a&b line backgrounds the assignment at the '&' — the
    # var lands empty and azcopy hits Azure with no SAS at all (401,
    # found live on checkmate). az SAS/URLs never contain single quotes.
    env = (f"AZURE_DEST_URL='{base}/{prefix}'\n"
           f"AZURE_DEST_SAS='{sas}'\n"
           f"AZURE_DEST_CONTAINER='{cfg['container']}'\n"
           f"AZURE_DEST_PREFIX='{prefix}'\n")
    _write_env(vm["public_ip"], DEST_ENV, env, args.dry_run)
    return {"remote": "azure", "container": cfg["container"],
            "dest_prefix": prefix, "sas_expiry": expiry,
            "written_to": [f"{vm['name']}:{eng.RCLONE_CONF}",
                           f"{vm['name']}:{DEST_ENV}"]}


def cmd_write_creds(root: Path, args) -> dict:
    """1 stdin line -> 600 figma.env on the VM, then a smoke test with the
    sourced token: /v1/me (does the token work) and the first tagged
    team's folder listing (can it SEE the engagement's teams — the second
    day-one stall)."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    # always reads + validates stdin (the s3/zoho write-creds semantics):
    # the 1-line contract is checked even in dry-run, with the value
    # replaced before anything is written
    lines = [ln.strip() for ln in sys.stdin.read().splitlines()
             if ln.strip()]
    if len(lines) != 1:
        raise common.HarnessError(
            "stdin must be exactly 1 line: the Figma personal access token")
    # single-quoted for the same reason as dest-figma.env (see
    # cmd_write_dest). Figma PATs are figd_-prefixed URL-safe chars; a
    # stray quote means a bad paste — refuse instead of writing a
    # silently corrupt env file. Checked on the RAW line, BEFORE the
    # dry-run substitution, so --dry-run exercises the guard for real.
    if "'" in lines[0]:
        raise common.HarnessError(
            "the token contains a single quote — refusing to write a "
            "corrupt env file. Re-copy it from the Figma settings page; "
            "nothing was written.")
    token = "<figma-pat>" if args.dry_run else lines[0]
    _write_env(vm["public_ip"], FIGMA_ENV, f"FIGMA_TOKEN='{token}'\n",
               args.dry_run)
    try:
        first_team = _team_ids(vm, args).split(",")[0]
    except common.HarnessError:
        first_team = ""
    # The dry-run echo of this command must stay brace-free (the test
    # harness finds the JSON tail with stdout.index("{")): $VAR never
    # ${VAR}, -D -/sed never -w '%{http_code}', $(...) never braces.
    team_leg = ""
    if first_team:
        team_leg = (
            "T=$(curl -s -o /dev/null -D - "
            "-H \"X-Figma-Token: $FIGMA_TOKEN\" "
            f"\"https://{FIGMA_API}/v2/teams/{first_team}/folders\" "
            "| sed -n 's,^HTTP[^ ]* \\([0-9]*\\).*,\\1,p' | tail -1); ")
    smoke = eng.run_ssh(
        vm["public_ip"],
        f"set -a; . {FIGMA_ENV}; set +a; "
        "M=$(curl -s -o /dev/null -D - "
        "-H \"X-Figma-Token: $FIGMA_TOKEN\" "
        f"\"https://{FIGMA_API}/v1/me\" "
        "| sed -n 's,^HTTP[^ ]* \\([0-9]*\\).*,\\1,p' | tail -1); "
        + team_leg
        + "echo me=$M team=$T",
        dry_run=args.dry_run, check=False, timeout=90)
    if args.dry_run:
        return {"ok": True, "dry_run": True,
                "written_to": f"{vm['name']}:{FIGMA_ENV}"}
    out = (smoke.stdout or "").strip()
    me_code = team_code = ""
    for part in out.split():
        if part.startswith("me="):
            me_code = part[3:]
        if part.startswith("team="):
            team_code = part[5:]
    if me_code != "200":
        hint = ("the token is invalid or expired — Figma answers a dead "
                "PAT with 403 (not 401) and does not say which; the "
                "client re-issues it and you re-run write-creds"
                if me_code == "403" else
                "a 429 here suggests a View/Collab seat (6 Tier-1 calls "
                "per MONTH) — the token owner needs a Full or Dev seat"
                if me_code == "429" else
                "network/API trouble from the VM")
        return {"ok": False, "stage": "token", "http_code": me_code,
                "hint": hint + ". The creds are written either way — fix "
                               "and re-run write-creds to replace them."}
    if first_team and team_code != "200":
        return {"ok": False, "stage": "team-visibility",
                "http_code": team_code, "team_id": first_team,
                "hint": "the token works but cannot see team "
                        f"{first_team} — the token owner must be a member "
                        "of every named team (Figma does not distinguish "
                        "no-access from no-such-team, so also re-check the "
                        "id against the client's URL: "
                        "figma.com/files/team/<ID>/...). Creds are "
                        "written; fix and re-run."}
    return {"ok": True, "written_to": f"{vm['name']}:{FIGMA_ENV}"}


# ── probe ────────────────────────────────────────────────────────────────────

def _attempt(token: str, path: str, params: dict | None = None,
             pace_s: float = 0.0) -> tuple[str, object]:
    """Try one endpoint and classify the outcome without failing the probe.
    Figma documents no response code for a missing PAT scope, so probing
    each endpoint family once at startup is the ONLY fail-fast — better
    one recorded refusal now than a scope gap discovered 8 hours into the
    walk."""
    try:
        return "ok", figma_get(token, path, params, pace_s=pace_s)
    except FigmaProbeError as e:
        return f"{e.status}", None
    except common.HarnessError as e:
        return f"error: {str(e)[:120]}", None


def _walk_census(token: str, team_id: str, pace_s: float,
                 dry_run: bool) -> dict:
    """Full folder recursion for one team (Tier 2, unpaginated listings —
    cheap next to the Tier-1 walk the transfer will do)."""
    out = {"team_id": team_id, "folders": 0, "files": 0, "branches": 0,
           "file_keys": [], "last_modified": [None, None]}
    top = figma_get(token, f"/v2/teams/{team_id}/folders",
                    dry_run=dry_run, pace_s=pace_s) or {}
    if dry_run:
        return out
    queue = [f.get("id") for f in top.get("folders") or [] if f.get("id")]
    seen: set = set()
    while queue:
        fid = queue.pop(0)
        if fid in seen or len(seen) > 5000:
            continue
        seen.add(fid)
        out["folders"] += 1
        subs = figma_get(token, f"/v2/folders/{fid}/folders",
                         pace_s=pace_s) or {}
        queue.extend(s.get("id") for s in subs.get("folders") or []
                     if s.get("id"))
        listing = figma_get(token, f"/v2/folders/{fid}/files",
                            {"branch_data": "true"}, pace_s=pace_s) or {}
        for fl in listing.get("files") or []:
            if not fl.get("key"):
                continue
            out["files"] += 1
            out["branches"] += len(fl.get("branches") or [])
            if len(out["file_keys"]) < 20:
                out["file_keys"].append(fl["key"])
            lm = fl.get("last_modified")
            if lm:
                lo, hi = out["last_modified"]
                out["last_modified"] = [min(lm, lo) if lo else lm,
                                        max(lm, hi) if hi else lm]
    return out


def cmd_probe(root: Path, args) -> dict:
    """Day-one gate, BEFORE any billable resource: does the token work, is
    the seat right, can it see every named team, which scopes answer, and
    what is actually there? Laptop-side, Figma API JSON only — no corpus
    data, no Azure access, no VM needed. The token is read from stdin into
    process memory and discarded.

    Emits COUNTS and a Tier-1 wall-clock estimate, NEVER bytes — Figma
    publishes no file or asset sizes, so any pre-run byte figure would be
    an invention. The first honest byte number is the transfer manifest's
    total_staged_bytes.
    """
    eng.load_cfg(root, args.slug)  # onboarding check only; no network
    token = read_token(args.dry_run)
    team_ids = validate_team_ids(args.team_ids).split(",")
    plan = (args.plan or "").strip().lower()
    limits = puller.TIER_LIMITS_PER_MIN.get(
        plan, puller.TIER_LIMITS_PER_MIN[puller.DEFAULT_PLAN])
    pace_s = 60.0 / (limits[2] * puller.RATE_SAFETY)   # folders/library pace
    pace_1 = 60.0 / (limits[1] * puller.RATE_SAFETY)   # Tier-1 sample pace
    if args.dry_run:
        figma_get(token, "/v1/me", dry_run=True)
        for team_id in team_ids:
            figma_get(token, f"/v2/teams/{team_id}/folders", dry_run=True)
        return {"ok": True, "dry_run": True, "team_ids": team_ids,
                "plan": plan or "undetected"}
    me_state, me = _attempt(token, "/v1/me")
    if me_state != "ok":
        raise common.HarnessError(
            f"/v1/me refused ({me_state}) — the token is invalid or "
            "expired (Figma answers a dead PAT with 403, not 401, and "
            "does not say which), or the current_user:read scope is "
            "missing. Both are a re-issue conversation with the client; "
            "retrying cannot help.")
    identity = {"handle": (me or {}).get("handle"),
                "email": (me or {}).get("email")}
    teams = []
    unreachable = []
    for team_id in team_ids:
        try:
            teams.append(_walk_census(token, team_id, pace_s, False))
        except FigmaProbeError as e:
            unreachable.append({"team_id": team_id, "status": e.status})
    if not teams:
        raise common.HarnessError(
            "no named team is visible to this token "
            f"({json.dumps(unreachable)}) — the token owner must be a "
            "member of every team, and the ids must match the client's "
            "URLs. Nothing else can be probed until this is fixed.")
    files_total = sum(t["files"] for t in teams)
    branches_total = sum(t["branches"] for t in teams)
    sample_key = next((k for t in teams for k in t["file_keys"]), None)
    scope_probe: dict[str, str] = {}
    editor_types: dict[str, int] = {}
    if sample_key:
        for fam, path, params in (
                ("file_content", f"/v1/files/{sample_key}", {"depth": 1}),
                ("comments", f"/v1/files/{sample_key}/comments", None),
                ("versions", f"/v1/files/{sample_key}/versions", None),
                ("image_fills", f"/v1/files/{sample_key}/images", None),
                ("team_library",
                 f"/v1/teams/{teams[0]['team_id']}/components",
                 {"page_size": 1})):
            state, payload = _attempt(
                token, path, params,
                pace_s=pace_1 if fam == "file_content" else pace_s)
            scope_probe[fam] = state
            if fam == "file_content" and isinstance(payload, dict):
                et = payload.get("editorType")
                if et:
                    editor_types[et] = editor_types.get(et, 0) + 1
        # editorType histogram from a SAMPLE (listings carry no editor
        # type, so FigJam/Slides presence cannot be known from the walk)
        sample_keys = [k for t in teams for k in t["file_keys"]][
            1:max(1, args.sample_files)]
        for key in sample_keys:
            state, payload = _attempt(token, f"/v1/files/{key}",
                                      {"depth": 1}, pace_s=pace_1)
            if isinstance(payload, dict) and payload.get("editorType"):
                et = payload["editorType"]
                editor_types[et] = editor_types.get(et, 0) + 1
    est = puller.estimate_tier1(files_total, branches_total,
                                render_pages=not args.no_render_pages)
    blocking = [f for f in ("file_content",) if scope_probe.get(f)
                not in (None, "ok")]
    if blocking:
        return {"ok": False, "cause": "scope",
                "scope_probe": scope_probe, "identity": identity,
                "hint": "file_content:read did not answer — without it "
                        "there is nothing to pull. The client regenerates "
                        "the token with the full scope checklist from the "
                        "skill's PAUSE snippet."}
    return {
        "ok": True, "identity": identity,
        "plan": plan or "undetected (pass --plan; pacing defaults to the "
                        "starter floor until a plan is known)",
        "teams": [{k: v for k, v in t.items() if k != "file_keys"}
                  for t in teams],
        "teams_unreachable": unreachable,
        "totals": {"teams": len(teams), "files": files_total,
                   "branches": branches_total,
                   "folders": sum(t["folders"] for t in teams)},
        "editor_type_sample": editor_types,
        "scope_probe": scope_probe,
        "estimate": est,
        "note": ("counts and a Tier-1 wall-clock only, NO byte estimate — "
                 "on purpose: Figma publishes no file or asset sizes. "
                 "Tier 1 (documents + renders, one shared bucket) is the "
                 "clock; quote the timeline off estimate.hours_by_plan at "
                 "the client's actual plan. A team the client forgot to "
                 "name is invisible (no listing API) — read the census "
                 "back to them before quoting scope."),
    }


# ── transfer / status / verify / teardown / discover ─────────────────────────

def cmd_transfer(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    if eng._tmux_alive(ip, args.dry_run):
        return {"ok": False, "cause": "already-running",
                "hint": "tmux session 'transfer' is alive — use status."}
    team_ids = _team_ids(vm, args)
    plan = _plan(vm, args)
    _push_puller(ip, args.dry_run)
    flags = f" --team-ids {team_ids}"
    if plan:
        flags += f" --plan {plan}"
    for flag, val in (("--limit", args.limit), ("--only", args.only),
                      ("--fill-workers", args.fill_workers),
                      ("--rate-sleep-max", args.rate_sleep_max)):
        if val:
            flags += f" {flag} {val}"
    for flag, on in (("--refresh", args.refresh),
                     ("--skip-upload", args.skip_upload),
                     ("--no-render-pages", args.no_render_pages),
                     ("--no-fills", args.no_fills),
                     ("--no-comments", args.no_comments),
                     ("--no-versions", args.no_versions),
                     ("--no-library", args.no_library)):
        if on:
            flags += f" {flag}"
    inner = (f"set -a; . {FIGMA_ENV}; . {DEST_ENV}; set +a; "
             f"python3 {XFER_DIR}/figma_vm_pull.py "
             f"--dest {DEST_DIR}{flags} >> {LOG_FILE} 2>&1")
    eng.run_ssh(ip, f"tmux new-session -d -s {eng.TMUX_SESSION} -n figma "
                    f"\"bash -c '{inner}'\"",
                dry_run=args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "team_ids": team_ids}
    eng.run_ssh(ip, "sleep 5", check=False)
    alive = eng._tmux_alive(ip, False)
    tail = eng.run_ssh(ip, f"tail -3 {LOG_FILE} 2>/dev/null", check=False)
    return {"ok": alive, "session": eng.TMUX_SESSION, "window": "figma",
            "team_ids": team_ids, "plan": plan or "(starter floor)",
            "pilot_limit": args.limit or None,
            "log_tail": (tail.stdout or "").strip().splitlines(),
            "note": ("re-running transfer is safe — per-unit .cdp-complete "
                     "markers skip finished files, the library cursor "
                     "resumes mid-walk, and azcopy --overwrite=false skips "
                     "landed blobs" if alive else None),
            "hint": None if alive else
            f"the puller died immediately — tail {LOG_FILE} on the VM "
            "(bad env files? expired token?)"}


_STATUS_PY = r"""
import json, os, shutil
base = os.path.expanduser("~/xfer-figma")
dest = os.path.join(base, "dest")
out = {}
try:
    out["progress"] = json.load(open(os.path.join(dest, "progress.json")))
except (OSError, ValueError):
    out["progress"] = None
try:
    m = json.load(open(os.path.join(dest, "manifest.json")))
    out["manifest"] = dict(unit_count=m.get("unit_count"),
                           total_staged_bytes=m.get("total_staged_bytes"),
                           api_calls=m.get("api_calls"),
                           failed_units=m.get("failed_units"),
                           skipped_units=m.get("skipped_units"),
                           decomposed_files=m.get("decomposed_files"),
                           finished_utc=m.get("finished_utc"))
except (OSError, ValueError):
    out["manifest"] = None
try:
    lines = open(os.path.join(base, "pull-figma.log")).read().splitlines()
    out["log_tail"] = lines[-5:]
except OSError:
    out["log_tail"] = []
try:
    du = shutil.disk_usage(base)
    out["disk_free_gb"] = round(du.free / 1e9, 1)
except OSError:
    pass
print(json.dumps(out))
"""


def cmd_status(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    alive = eng._tmux_alive(vm["public_ip"], args.dry_run)
    proc = eng.run_ssh(vm["public_ip"], "python3 -", stdin_data=_STATUS_PY,
                       dry_run=args.dry_run, check=False, timeout=120)
    if args.dry_run:
        return {"ok": True, "dry_run": True}
    try:
        detail = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        detail = {"aggregator_error": (proc.stdout or proc.stderr)[-300:]}
    manifest = detail.get("manifest")
    hint = None
    if not alive:
        hint = ("a pass finished — run verify" if manifest else
                "not running and no manifest — it never finished a pass; "
                f"tail {LOG_FILE} on the VM, then re-run transfer")
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "transfer_running": alive,
            "staged_bytes_human": common.human_bytes(
                (manifest or {}).get("total_staged_bytes") or 0),
            "hint": hint,
            "note": "a 'rate-limited; sleeping Ns' or pacing-sleep line in "
                    "the log tail is normal metering against Figma's "
                    "per-minute tiers, not a hang",
            **detail}


def cmd_verify(root: Path, args) -> dict:
    """Laptop-side; the VM may already be torn down. Lists the dest prefix
    and compares against the uploaded manifest.json — see
    compare_manifest_to_blobs for exactly what is (and is not) asserted.
    Takes NO Figma credentials."""
    cfg = eng.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    prefix = (args.dest_prefix or SPEC.default_dest_prefix).strip("/")
    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl -- the READ path
        blobs = azure_list_blobs(cfg, sas, prefix, args.dry_run)
        if args.dry_run:
            print(f"DRY-RUN: GET {_container_url(cfg)}/{prefix}/"
                  "manifest.json?<sas-redacted>")
            return {"ok": True, "dry_run": True, "prefix": prefix}
        mname = f"{prefix}/manifest.json"
        if mname not in blobs:
            return {"ok": False, "cause": "no-manifest", "prefix": prefix,
                    "blobs_under_prefix": len(blobs),
                    "hint": "no manifest.json under the prefix — the pull "
                            "never finished a pass (or uploaded with "
                            "--skip-upload). Run status/transfer on the "
                            "VM first."}
        manifest = json.loads(azure_get(
            f"{_container_url(cfg)}/{urllib.parse.quote(mname, safe='/')}"
            f"?{sas}"))
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)
    result = compare_manifest_to_blobs(manifest, blobs, prefix)
    result.update({"prefix": prefix, "blobs_under_prefix": len(blobs),
                   "finished_utc": manifest.get("finished_utc")})
    if result["ok"]:
        result["note"] = (
            "certifies staged->container completeness against the uploaded "
            "manifest; Figma->staged completeness is the puller's exit "
            "code + failed_units (clean here). NO source-size claim is "
            "made — Figma publishes no byte sizes at all, so the "
            "manifest's total_staged_bytes is the first honest byte "
            "number. Remember the derivative caveat when reporting: no "
            ".fig export endpoint exists, so this corpus (API JSON + "
            "assets) is not restorable into Figma. After this, pin the "
            f'figma service in expected-data-sizes.json with '
            f'"prefix": "{prefix}", then let size-company pick it up.')
    return result


def cmd_teardown(root: Path, args) -> dict:
    result = eng.cmd_teardown(SPEC, root, args)
    if result.get("ok") and "reminders" in result:
        result["reminders"][-1] = (
            "Tell the client they can revoke the Figma personal access "
            "token now (Settings -> Security -> Personal access tokens -> "
            "Revoke) — it can read every team the owner belongs to, so "
            "revocation is the clean end of the engagement (it would lapse "
            "on its own within 90 days regardless).")
    return result


def cmd_discover(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    if args.dry_run:
        eng.get_vm(SPEC, cfg, args.slug, True)
        return {"phase": "unknown (dry-run)",
                "note": "dry-run prints the discovery commands only"}
    vm = eng.get_vm(SPEC, cfg, args.slug, False)
    if vm is None:
        return {"phase": "pre-setup", "vm": None,
                "hint": "no transfer VM — run probe (needs only the token "
                        "+ team ids), then setup"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base,
                "hint": "VM exists but has no public IP (deallocated?)"}
    checks = [f"test -f {FIGMA_ENV} && echo figma-env",
              f"test -f {DEST_ENV} && echo dest-env",
              f"test -f {DEST_DIR}/manifest.json && echo manifest",
              f"tmux has-session -t {eng.TMUX_SESSION} 2>/dev/null "
              "&& echo tmux-alive"]
    probe = eng.run_ssh(vm["public_ip"], "; ".join(checks), check=False)
    if probe.returncode != 0 and not (probe.stdout or "").strip():
        return {"phase": "vm-unreachable", **base,
                "hint": "ssh failed — VM booting, or your key changed. "
                        f"Try: ssh {eng.ADMIN_USER}@{vm['public_ip']}"}
    out = probe.stdout or ""
    if "tmux-alive" in out:
        return {"phase": "transfer-running", **base,
                "hint": "use status for progress"}
    if "manifest" in out:
        return {"phase": "transfer-stopped", **base,
                "hint": "a pass finished — run status, then verify "
                        "(laptop-side); failed units mean re-run transfer"}
    if not ("figma-env" in out and "dest-env" in out):
        return {"phase": "mid-setup", **base,
                "hint": "VM up but creds/dest incomplete — resume setup at "
                        "the missing write-dest / write-creds step."}
    return {"phase": "setup-complete", **base,
            "hint": "creds + dest in place — run transfer --limit 2 "
                    "(pilot) first"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[
        "discover", "plan", "create-vm", "allow-network", "write-dest",
        "check-azure", "write-creds", "probe", "transfer", "status",
        "verify", "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--team-ids", dest="team_ids", default=None,
                   help="comma-separated Figma team ids from the client's "
                        "file-browser URLs (required for plan/create-vm/"
                        "probe; later read from the figma_team_ids VM tag). "
                        "There is NO API that lists teams.")
    p.add_argument("--plan", default=None,
                   help="Figma plan: starter | pro | org | enterprise — "
                        "sets the puller's pacing schedule (rides the "
                        "figma_plan VM tag; default: the starter floor)")
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None,
                   help="create-vm: OS disk GB (default: the Spec's 512 — "
                        "staging holds the whole corpus before azcopy)")
    p.add_argument("--dest-prefix", default=None,
                   help=f"prefix inside <slug>-raw (default "
                        f"{SPEC.default_dest_prefix})")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--limit", type=int, default=0,
                   help="transfer: only the first N file units (pilot)")
    p.add_argument("--only", default=None,
                   help="transfer: only this one unit (a unit label like "
                        "meta or library/<team>, or a bare file key)")
    p.add_argument("--refresh", action="store_true",
                   help="transfer: ignore .cdp-complete markers and cursors")
    p.add_argument("--skip-upload", dest="skip_upload", action="store_true",
                   help="transfer: pull to VM disk but don't azcopy")
    p.add_argument("--no-render-pages", dest="no_render_pages",
                   action="store_true",
                   help="transfer: skip per-page PNG renders (they share "
                        "the Tier-1 bucket with document pulls — roughly "
                        "halves the wall clock)")
    p.add_argument("--no-fills", dest="no_fills", action="store_true",
                   help="transfer: skip image-fill downloads (the embedded "
                        "bitmaps — their URLs expire in <=14 days, so "
                        "skipping them usually loses the bytes)")
    p.add_argument("--no-comments", dest="no_comments", action="store_true")
    p.add_argument("--no-versions", dest="no_versions", action="store_true")
    p.add_argument("--no-library", dest="no_library", action="store_true",
                   help="transfer: skip the team library metadata units")
    p.add_argument("--fill-workers", dest="fill_workers", type=int,
                   default=0,
                   help="transfer: parallel CDN fill downloads (default 4, "
                        "cap 8 — the CDN throttles separately from the "
                        "API tiers)")
    p.add_argument("--rate-sleep-max", dest="rate_sleep_max", type=int,
                   default=0, help="transfer: cap on one 429 sleep (s)")
    p.add_argument("--sample-files", dest="sample_files", type=int,
                   default=5,
                   help="probe: files in the editorType sample")
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-transfer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.command in ("plan", "create-vm", "probe") \
            and args.team_ids is None:
        p.error(f"{args.command} requires --team-ids")
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    engine_cmds = {"create-vm": eng.cmd_create_vm,
                   "allow-network": eng.cmd_allow_network,
                   "check-azure": eng.cmd_check_azure}
    own_cmds = {"discover": cmd_discover, "plan": cmd_plan,
                "write-dest": cmd_write_dest, "write-creds": cmd_write_creds,
                "probe": cmd_probe, "transfer": cmd_transfer,
                "status": cmd_status, "verify": cmd_verify,
                "teardown": cmd_teardown}
    try:
        if args.team_ids is not None:
            args.team_ids = validate_team_ids(args.team_ids)
        if args.command in engine_cmds:
            result = engine_cmds[args.command](SPEC, root, args)
        else:
            result = own_cmds[args.command](root, args)
    except common.HarnessError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    except subprocess.TimeoutExpired as e:
        print(json.dumps({"ok": False,
                          "error": f"timeout: {str(e)[:200]}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
