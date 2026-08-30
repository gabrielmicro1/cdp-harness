#!/usr/bin/env python3
"""Slack export -> Azure file ingest CLI (VM REST puller; VM family).

A Business+ / Enterprise Slack compliance export is a complete transcript and
ZERO file bytes: every attachment, image, video, canvas and huddle transcript
survives only as an authenticated `files.slack.com` link inside the JSON. On a
real sample export (2.35 GB of JSON, 123k day files, 1.1M messages) that is
63,636 unique files, of which 62,056 are Slack-hosted and worth 81.1 GB --
roughly 34x the export itself (the other 996 are Drive links Slack holds no
bytes for, and 584 are deleted or retention-aged out). Clients
push the export into `<slug>-raw`; this ingest reads it there, builds a ledger
linking every file back to the conversation and message that referenced it,
and copies the bytes into the same container under `slack-export-files/`.

This is the ninth VM ingest and it reuses transfer_engine.py's lifecycle
verbatim (create-vm / allow-network / check-azure / teardown, mint_container_sas)
exactly as teams_transfer.py does. What is new to the family: **the source is
not a remote SaaS API — it is the client's own export already sitting in the
destination container**, so the container is both read side and write side of
the same job and the skill has to FIND the export before anything else can
happen. `discover-export` does that (and asks the user rather than guessing
when it finds zero or several candidates).

Transport is Azure server-side copy (the vimeo/zoom transport), not the
stage-then-azcopy of github/zoho/figma/teams: the VM resolves each Slack URL's
redirect chain and hands the final signed URL to Put Blob / Put Block From URL,
so file bytes never touch the VM disk and create-only is API-enforced by
`If-None-Match: *`. See scripts/slack_vm_pull.py for the pull layer.

Auth is this family's genuine novelty: **the export carries its own token**.
Every file URL embeds one workspace-wide `xoxe-` value, so in the normal case
there is no client credential at all and `write-creds` is only an OVERRIDE for
an export whose links were stripped or whose token has expired. That expiry is
the day-one stall, and `probe` is the gate: it reads the export over a
read-only SAS and makes exactly one live request to Slack before any billable
VM exists.

Verify runs on the LAPTOP (the VM is normally torn down), and unlike its
siblings it can make a REAL source-truth claim: the export declares a byte
`size` for every file, so verify compares declared sizes against what Azure
actually committed rather than certifying staged->container only.
"""
from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import phases  # noqa: E402
import transfer_engine as eng  # noqa: E402
import slack_vm_pull as puller  # noqa: E402  (import-safe; pure helpers)

SPEC = eng.Spec(
    source_name="slack",
    vm_prefix="xfer-slack-",
    purpose="slack-transfer",
    loc_tag="slack_export",
    loc_argname="export_ref",
    loc_required=True,
    default_dest_prefix=puller.DEFAULT_DEST_PREFIX,
    authorize_target="",   # no rclone OAuth — the export carries its own token
    remote_type="",        # no rclone source; the source is a blob
    extra_cli_opts=[],
    # The export archive is downloaded to the VM once. It is JSON only —
    # tens of GB even for a large workspace — and file bytes never land here
    # (server-side copy), so this is staging for the archive alone.
    default_os_disk_gb=256,
)

PULLER_PY = Path(__file__).resolve().parent / "slack_vm_pull.py"
XFER_DIR = f"/home/{eng.ADMIN_USER}/xfer-slack"
DEST_DIR = f"{XFER_DIR}/dest"
LOG_FILE = f"{XFER_DIR}/pull-slack.log"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/xfer"
SLACK_ENV = f"{ENV_DIR}/slack.env"
DEST_ENV = f"{ENV_DIR}/dest-slack.env"
X_MS_VERSION = "2021-08-06"

# probe reads a bounded sample of day files rather than the whole export: a
# gate must stay cheap. The sample size always rides the output so the
# estimate's basis is auditable (the teams estimate_basis discipline).
PROBE_DAY_SAMPLE = 400
PROBE_ZIP_TAIL = 4 * 1024 * 1024   # enough for a ZIP64 central directory

_sleep = time.sleep  # seam so tests can record/skip waits


def _http(req: urllib.request.Request, timeout: int = 90):
    """Single transport seam (tests stub this; production never branches)."""
    return urllib.request.urlopen(req, timeout=timeout)


# ── azure listing / reads (laptop path: ip_rule_ensure + rl SAS) ─────────────
# Local copies of the established laptop-side blob pair — kept here rather than
# imported so the VM-family CLIs stay import-independent of each other. Same
# signatures as teams_transfer.py's.

def _container_url(cfg: dict) -> str:
    return (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")


def azure_get(url: str, byte_range: tuple[int, int] | None = None) -> bytes:
    """GET with retries. A 403 early in a run is usually IP-rule propagation
    (CLAUDE.md lore) -- wait and retry, never re-mint the SAS for it."""
    headers = {"x-ms-version": X_MS_VERSION}
    if byte_range:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, headers=headers)
        try:
            with _http(req, timeout=180) as r:
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


def azure_get_lines(url: str):
    """Yield decoded lines from a blob without buffering it.

    objects.jsonl is one row per object -- ~100 MB for a 60k-file workspace
    and linear in the corpus after that -- so verify streams it. Retries are
    deliberately absent here: a mid-stream failure cannot be resumed without
    a Range restart, and verify is cheap to re-run.
    """
    req = urllib.request.Request(url, headers={"x-ms-version": X_MS_VERSION})
    with _http(req, timeout=1800) as r:
        for raw in io.TextIOWrapper(r, encoding="utf-8"):
            line = raw.strip()
            if line:
                yield line


def azure_list_blobs(cfg: dict, sas: str, prefix: str,
                     dry_run: bool) -> dict[str, dict]:
    """One marker-paginated listing -> {name: {size}}. Content-Length is what
    Azure actually committed — verify's ground truth."""
    if dry_run:
        print(f"DRY-RUN: GET {_container_url(cfg)}"
              f"?restype=container&comp=list&prefix={prefix}&<sas-redacted>")
        return {}
    blobs: dict[str, dict] = {}
    marker = ""
    while True:
        url = (f"{_container_url(cfg)}?restype=container&comp=list"
               f"&maxresults=5000"
               f"&prefix={urllib.parse.quote(prefix, safe='')}")
        if marker:
            url += f"&marker={urllib.parse.quote(marker, safe='')}"
        root = ET.fromstring(azure_get(url + "&" + sas))
        for blob in root.iter("Blob"):
            name = blob.findtext("Name")
            props = blob.find("Properties")
            if name:
                blobs[name] = {"size": int(
                    (props.findtext("Content-Length") or 0)
                    if props is not None else 0)}
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return blobs


class BlobRangeFile(io.RawIOBase):
    """A seekable read-only file over one blob, backed by Range GETs.

    This is what lets `probe` open a multi-GB export archive from the laptop
    without downloading it: zipfile only needs the end-of-central-directory
    and the central directory to enumerate members, then one ranged read per
    member it actually wants. The last PROBE_ZIP_TAIL bytes are cached because
    zipfile seeks around the EOCD repeatedly while parsing it.
    """

    def __init__(self, url: str, size: int):
        self._url = url
        self._size = size
        self._pos = 0
        self._tail_start = max(0, size - PROBE_ZIP_TAIL)
        self._tail: bytes | None = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = self._size + offset
        self._pos = max(0, min(self._pos, self._size))
        return self._pos

    def _fetch_tail(self) -> bytes:
        if self._tail is None:
            self._tail = azure_get(
                self._url, (self._tail_start, self._size - 1))
        return self._tail

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        n = min(n, self._size - self._pos)
        if n <= 0:
            return b""
        start, end = self._pos, self._pos + n - 1
        if start >= self._tail_start:
            tail = self._fetch_tail()
            off = start - self._tail_start
            data = tail[off:off + n]
        else:
            data = azure_get(self._url, (start, end))
        self._pos += len(data)
        return data

    def readall(self) -> bytes:
        return self.read(-1)


# ── export discovery ────────────────────────────────────────────────────────

EXPORT_BASENAMES = set(puller.EXPORT_SIGNATURE) | {".slack-manifest.json"}


def find_export_candidates(blobs: dict) -> list:
    """PURE. Every plausible Slack export in a container listing.

    Two shapes are recognised, both seen in the wild:
      - an EXTRACTED TREE: channels.json and users.json sitting as sibling
        blobs, so the export root is their common parent prefix.
      - a ZIP archive: any .zip blob is a candidate; whether it really is an
        export is settled by reading its central directory (cheap, two ranged
        GETs), which the caller does.

    Returns candidates ordered biggest-evidence-first. The caller NEVER
    guesses between several: it reports them all and asks.
    """
    trees: dict[str, set] = {}
    zips: list = []
    for name, meta in blobs.items():
        base = name.rsplit("/", 1)[-1]
        parent = name[:-len(base)] if "/" in name else ""
        if base in EXPORT_BASENAMES:
            trees.setdefault(parent, set()).add(base)
        if base.lower().endswith(".zip"):
            zips.append({"kind": "zip", "blob": name,
                         "size": meta.get("size", 0)})
    out = []
    for parent, found in sorted(trees.items()):
        if puller.looks_like_export_root(found):
            out.append({"kind": "tree", "prefix": parent.rstrip("/"),
                        "evidence": sorted(found)})
    out.extend(sorted(zips, key=lambda z: -z["size"]))
    return out


def confirm_zip_export(cfg: dict, sas: str, blob_name: str,
                       size: int) -> dict | None:
    """Read a candidate .zip's central directory and decide whether it is a
    Slack export. Two ranged GETs — never a download."""
    url = f"{_container_url(cfg)}/{urllib.parse.quote(blob_name, safe='/')}?{sas}"
    try:
        zf = zipfile.ZipFile(BlobRangeFile(url, size))
    except (zipfile.BadZipFile, OSError, common.HarnessError):
        return None
    names = [puller.zip_member_name(i) for i in zf.infolist()
             if not i.is_dir()]
    root = puller.detect_export_root(names)
    if root is None:
        return None
    rel = [n[len(root):] for n in names if n.startswith(root)]
    return {"kind": "zip", "blob": blob_name, "size": size, "root": root,
            "members": len(rel),
            "day_files": len(puller.day_file_names(rel))}


def cmd_discover_export(root: Path, args) -> dict:
    """Find the client's Slack export inside their own -raw container.

    Laptop-side, one listing over the READ (rl) SAS — discovery never needs a
    write path. Zero candidates or several is a QUESTION for the user, never a
    guess: picking the wrong archive would build a ledger for the wrong
    workspace and copy tens of thousands of files under it.
    """
    cfg = eng.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl — the READ path
        prefix = (args.export_prefix or "").strip("/")
        blobs = azure_list_blobs(cfg, sas, prefix + "/" if prefix else "",
                                 args.dry_run)
        if args.dry_run:
            return {"ok": True, "dry_run": True,
                    "note": "listed the container; a real run would read each "
                            ".zip candidate's central directory"}
        candidates = find_export_candidates(blobs)
        confirmed = []
        for cand in candidates:
            if cand["kind"] == "tree":
                confirmed.append(cand)
                continue
            got = confirm_zip_export(cfg, sas, cand["blob"], cand["size"])
            if got:
                confirmed.append(got)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    tops = sorted({n.split("/")[0] for n in blobs})[:40]
    if not confirmed:
        return {
            "ok": False, "cause": "no-export-found",
            "blobs_scanned": len(blobs), "top_level_prefixes": tops,
            "hint": "no channels.json + users.json pair and no .zip whose "
                    "central directory holds one. Ask the user where the "
                    "export is and re-run with --export-prefix <prefix> (an "
                    "extracted tree) or point --export-blob at the archive; "
                    "if the client has not pushed it yet, that is the real "
                    "answer.",
        }
    if len(confirmed) > 1:
        return {
            "ok": False, "cause": "several-candidates",
            "candidates": confirmed,
            "hint": "more than one Slack export is present — ASK the user "
                    "which one (a two-phase export ships public channels "
                    "first and private/DMs later; those are separate runs "
                    "with separate --dest-prefix values, never merged). "
                    "Re-run with --export-blob or --export-prefix.",
        }
    chosen = confirmed[0]
    return {"ok": True, "export": chosen,
            "export_ref": export_ref(chosen),
            "next": "pass this export_ref to plan/create-vm as --export-blob "
                    "or --export-prefix, and run probe before create-vm."}


def export_ref(chosen: dict) -> str:
    """PURE. The single string that rides the VM's slack_export tag.

    One tag, unambiguous in both directions: "zip:<blob name>" or
    "tree:<prefix>". Encoding the kind here is what lets write-dest hand the
    puller a correct SLACK_EXPORT_KIND without a second tag to keep in sync.
    """
    if chosen.get("kind") == "tree":
        return f"tree:{chosen.get('prefix', '').strip('/')}"
    return f"zip:{chosen.get('blob', '')}"


def parse_export_ref(ref: str) -> dict:
    """PURE. Inverse of export_ref."""
    raw = (ref or "").strip()
    if raw.startswith("tree:"):
        return {"kind": "tree", "prefix": raw[5:].strip("/")}
    if raw.startswith("zip:"):
        return {"kind": "zip", "blob": raw[4:]}
    if raw.lower().endswith(".zip"):
        return {"kind": "zip", "blob": raw}
    if raw:
        return {"kind": "tree", "prefix": raw.strip("/")}
    return {"kind": "", "blob": "", "prefix": ""}


class LaptopExport:
    """Read-only view of the export from this machine, over the rl SAS.

    Deliberately separate from slack_vm_pull.py's ZipExport/TreeExport: the
    VM downloads the archive once and reads it locally, while probe must stay
    cheap enough to run before any VM exists — so here a zip is opened over
    ranged GETs (BlobRangeFile) and never downloaded.
    """

    def __init__(self, cfg: dict, sas: str, ref: dict, blobs: dict):
        self.ref = ref
        self._zf = None
        self._members: dict = {}
        self._blobs: dict = {}
        self.root = ""
        if ref["kind"] == "zip":
            name = ref["blob"]
            size = (blobs.get(name) or {}).get("size") or 0
            if not size:
                raise common.HarnessError(
                    f"export blob {name!r} not found in the container listing")
            url = (f"{_container_url(cfg)}/"
                   f"{urllib.parse.quote(name, safe='/')}?{sas}")
            self._zf = zipfile.ZipFile(BlobRangeFile(url, size))
            members = {puller.zip_member_name(i): i
                       for i in self._zf.infolist() if not i.is_dir()}
            self.root = puller.detect_export_root(members) or ""
            self._members = {n[len(self.root):]: i
                             for n, i in members.items()
                             if n.startswith(self.root)}
            self.size = size
        else:
            base = (ref["prefix"] + "/") if ref["prefix"] else ""
            rel_of = {n[len(base):]: n for n in blobs if n.startswith(base)}
            self.root = puller.detect_export_root(rel_of) or ""
            self._blobs = {r[len(self.root):]: b for r, b in rel_of.items()
                           if r.startswith(self.root)}
            self.size = sum((blobs[b] or {}).get("size", 0)
                            for b in self._blobs.values())
            self._cfg, self._sas = cfg, sas
        if not puller.looks_like_export_root(
                {n for n in self.names() if "/" not in n}):
            raise common.HarnessError(
                "that location does not look like a Slack export (no "
                "channels.json + users.json at its root)")

    def names(self) -> list:
        return sorted(self._members or self._blobs)

    def read(self, rel: str) -> bytes:
        if self._zf is not None:
            return self._zf.read(self._members[rel])
        name = self._blobs[rel]
        return azure_get(f"{_container_url(self._cfg)}/"
                         f"{urllib.parse.quote(name, safe='/')}?{self._sas}")

    def read_json(self, rel: str):
        try:
            return json.loads(self.read(rel))
        except (KeyError, ValueError, common.HarnessError):
            return None

    def describe(self) -> dict:
        return {"kind": self.ref["kind"],
                "blob": self.ref.get("blob"),
                "prefix": self.ref.get("prefix"),
                "root": self.root,
                "members": len(self._members or self._blobs),
                "bytes": self.size}


def census_sample(export: LaptopExport, sample_size: int,
                  seed: int = 20260829) -> dict:
    """Walk a bounded, deterministic sample of day files and count what is in
    them. Counts and DECLARED bytes only — no file is fetched here."""
    days = puller.day_file_names(export.names())
    rng = random.Random(seed)
    picked = days if len(days) <= sample_size else rng.sample(days,
                                                              sample_size)
    seen: set = set()
    modes: dict = {}
    bytes_by_mode: dict = {}
    renditions = 0
    refs = 0
    messages = 0
    token = None
    samples: list = []
    for rel in sorted(picked):
        payload = export.read_json(rel)
        if not isinstance(payload, list):
            continue
        for msg in payload:
            if not isinstance(msg, dict):
                continue
            messages += 1
            for f, _where in puller.iter_file_objects(msg):
                refs += 1
                fid = f.get("id")
                if fid in seen:
                    continue
                seen.add(fid)
                mode = f.get("mode") or "unknown"
                modes[mode] = modes.get(mode, 0) + 1
                bytes_by_mode[mode] = (bytes_by_mode.get(mode, 0)
                                       + int(f.get("size") or 0))
                renditions += len(puller.rendition_urls(f))
                url = puller.original_url(f)
                token = token or puller.url_token(url)
                if url and puller.file_disposition(f) == "hosted" \
                        and len(samples) < 8:
                    samples.append({"file_id": fid, "url": url,
                                    "size": int(f.get("size") or 0),
                                    "name": f.get("name")})
    return {"day_files_total": len(days), "day_files_sampled": len(picked),
            "messages_sampled": messages, "file_refs_sampled": refs,
            "unique_files_sampled": len(seen), "modes": modes,
            "bytes_by_mode": bytes_by_mode, "renditions_sampled": renditions,
            "export_token": token, "samples": samples}


def scale_census(sample: dict) -> dict:
    """PURE. Scale a sampled census to the whole export.

    The sample size and the scale factor always ride the output — an estimate
    whose basis is invisible is how a probe number quietly becomes a quoted
    commercial figure. Exact when the whole export was walked (factor 1.0).
    """
    total, taken = sample["day_files_total"], sample["day_files_sampled"]
    factor = (total / taken) if taken else 0.0
    exact = taken >= total and total > 0
    scaled_modes = {m: int(round(n * factor)) for m, n in
                    sample["modes"].items()}
    scaled_bytes = {m: int(round(b * factor)) for m, b in
                    sample["bytes_by_mode"].items()}
    originals = scaled_modes.get("hosted", 0)
    renditions = int(round(sample["renditions_sampled"] * factor))
    return {
        "estimate_basis": "exact" if exact else "sampled",
        "day_files_total": total,
        "day_files_sampled": taken,
        "scale_factor": round(factor, 3),
        "messages": int(round(sample["messages_sampled"] * factor)),
        "unique_files": int(round(sample["unique_files_sampled"] * factor)),
        "files_by_mode": scaled_modes,
        "declared_bytes_by_mode": scaled_bytes,
        "hosted_bytes": scaled_bytes.get("hosted", 0),
        "hosted_bytes_human": common.human_bytes(
            scaled_bytes.get("hosted", 0)),
        "external_files": scaled_modes.get("external", 0),
        "external_bytes": scaled_bytes.get("external", 0),
        "objects_originals": originals,
        "objects_with_renditions": originals + renditions,
        "rendition_multiplier": (round((originals + renditions) / originals, 1)
                                 if originals else 0),
    }


def estimate_runtime(objects: int, seconds_per_object: float,
                     workers: int, rps: float) -> dict:
    """PURE. Wall clock for the copy phase, bounded by BOTH the measured
    per-object latency across `workers` and the pacing ceiling — whichever is
    slower is the real rate."""
    by_latency = (workers / seconds_per_object) if seconds_per_object > 0 \
        else 0.0
    rate = min(by_latency, rps) if by_latency else rps
    seconds = (objects / rate) if rate > 0 else 0.0
    return {"objects": objects, "measured_seconds_per_object":
            round(seconds_per_object, 3), "copy_workers": workers,
            "rps_ceiling": rps, "effective_objects_per_second": round(rate, 2),
            "estimated_seconds": round(seconds), "estimated_hours":
            round(seconds / 3600.0, 1)}


# ── probe: the day-one gate, before any billable VM ─────────────────────────

def cmd_probe(root: Path, args) -> dict:
    """Read the export over the rl SAS, census it, and make exactly ONE live
    request to Slack to settle the transport.

    Unlike teams/figma/zoho, this probe reports REAL BYTES: the export
    declares a `size` for every file, so `hosted_bytes` is a measurement of
    the client's own export, not a guess. What it is NOT is a promise those
    bytes are still fetchable — that is what `link_gate` answers.
    """
    cfg = eng.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    ref = parse_export_ref(args.export_ref or "")
    if not ref["kind"]:
        raise common.HarnessError(
            "probe needs the export location — run discover-export first, "
            "then pass --export-blob <blob> or --export-prefix <prefix>")
    if args.dry_run:
        print(f"DRY-RUN: list {_container_url(cfg)} (rl SAS), open "
              f"{ref}, then GET one files.slack.com URL with the export's "
              "embedded token")
        return {"ok": True, "dry_run": True,
                "note": "no network I/O under --dry-run"}

    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)
        scope = (ref.get("prefix") or "")
        listing_prefix = (scope + "/") if scope else ""
        if ref["kind"] == "zip":
            listing_prefix = ref["blob"]
        blobs = azure_list_blobs(cfg, sas, listing_prefix, args.dry_run)
        export = LaptopExport(cfg, sas, ref, blobs)
        root_docs = {n: export.read_json(n) for n in puller.ROOT_JSONS
                     if n in set(export.names())}
        sample = census_sample(export, args.sample or PROBE_DAY_SAMPLE)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    conv_counts = {kind: len(root_docs.get(fname) or [])
                   for fname, kind in puller.CONV_META.items()}
    conv_counts["dm"] = len(root_docs.get("dms.json") or [])
    assets = {kind: len(root_docs.get(fname) or [])
              for fname, kind in puller.ASSET_JSONS.items()}
    census = scale_census(sample)
    census["conversations"] = conv_counts
    census["users"] = len(root_docs.get("users.json") or [])
    census["root_assets"] = assets

    token = sample["export_token"]
    bearer = (args.slack_token or "").strip() or None
    gate, next_step, transport = _link_gate(sample["samples"], token, bearer)
    objects = (census["objects_with_renditions"] if args.renditions
               else census["objects_originals"])
    estimate = estimate_runtime(
        objects, transport.get("seconds_per_object") or 0.0,
        args.copy_workers or puller.DEFAULT_COPY_WORKERS,
        args.rps_files or puller.DEFAULT_RPS_FILES)

    return {
        "ok": gate == "open",
        "export": export.describe(),
        "slack_manifest": root_docs.get(".slack-manifest.json"),
        "census": census,
        "link_gate": gate,
        "transport": transport,
        "next_step": next_step,
        "estimate": estimate,
        "note": (
            "declared_bytes come from the export's own per-file `size` "
            "fields — a real byte number on day one, unlike the other VM "
            "ingests. external files (gdrive) are counted and will be "
            "RECORDED, never fetched: Slack holds no bytes for them. "
            "objects_with_renditions vs objects_originals is the "
            "thumbnail/transcode decision — renditions multiply blob count "
            f"about {census['rendition_multiplier']}x here."),
    }


def _link_gate(samples: list, token: str | None,
               bearer: str | None) -> tuple[str, str, dict]:
    """One live Slack request (a few, if the first files were deleted).

    This is the whole point of probe: the export's download links die with the
    export, and an expired token means the engagement needs a FRESH export —
    days of client work, not a retry. Better to learn it now than eight hours
    into a VM run.
    """
    if not samples:
        return ("no-file-links",
                "the sampled day files reference no fetchable Slack file at "
                "all. Either this export was produced without file links (a "
                "Standard-plan or metadata-only export) or the sample missed "
                "them — re-run probe with a larger --sample before "
                "concluding.", {})
    if not token and not bearer:
        return ("no-file-links",
                "file URLs are present but none carries a download token, and "
                "no --slack-token was supplied. The export's links were "
                "stripped; ask the client for a fresh export, or for a Slack "
                "token with files:read to use via write-creds.", {})
    detail: dict = {}
    for item in samples[:5]:
        started = time.monotonic()
        try:
            final, wire, ranged = puller.resolve_slack(item["url"], token,
                                                       bearer)
        except puller.CopyError as e:
            status = (e.source_status or "").strip()
            detail = {"file_id": item["file_id"], "error": str(e)[:200]}
            if status in ("401", "403"):
                return ("token-expired",
                        "Slack refused the export's download link (HTTP "
                        f"{status}). Export links expire WITH the export — "
                        "the client must run a fresh Slack export, or supply "
                        "a Slack token with files:read for write-creds. This "
                        "is a client conversation, not a retry.", detail)
            if status == "404":
                continue     # deleted since the export; try the next sample
            return ("redirect-unresolvable",
                    "could not resolve the file URL to a fetchable location "
                    "— the pull would fall back to streaming through the VM. "
                    "Investigate before quoting a timeline.", detail)
        elapsed = time.monotonic() - started
        detail = {
            "file_id": item["file_id"],
            "redirected": not puller.is_slack_file_url(final)
            or final.split("?")[0] != item["url"].split("?")[0],
            "range_supported": ranged,
            "declared_size": item["size"],
            "wire_size": wire,
            "size_matches_declared": (wire == item["size"]
                                      if wire is not None else None),
            "seconds_per_object": round(elapsed, 3),
        }
        if not ranged:
            return ("no-range-support",
                    "the file host answered the range probe without 206, so "
                    "files above the single-shot limit cannot use Put Block "
                    "From URL and would stream through the VM instead. Small "
                    "files are unaffected; expect a slower run if the corpus "
                    "is video-heavy.", detail)
        return ("open",
                "the export's links authenticate and resolve — transfer can "
                "proceed.", detail)
    return ("files-missing",
            "every sampled file returned 404 — those files were deleted from "
            "Slack after the export was produced. Re-run probe with a larger "
            "--sample: if it stays 404 everywhere, the export is a transcript "
            "of files that no longer exist.", detail)


# ── VM-side plumbing ────────────────────────────────────────────────────────

def _write_env(ip: str, path: str, content: str, dry_run: bool) -> None:
    """600 env file on the VM; content rides ssh stdin only."""
    eng.run_ssh(ip, f"umask 077 && mkdir -p {ENV_DIR} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _push_puller(ip: str, dry_run: bool) -> None:
    """Fresh copy every transfer so harness upgrades propagate."""
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > "
                    f"{XFER_DIR}/slack_vm_pull.py",
                stdin_data=PULLER_PY.read_text(), dry_run=dry_run)


def _dest_prefix(vm: dict, args) -> str:
    return (getattr(args, "dest_prefix", None)
            or (vm.get("tags") or {}).get("dest_prefix")
            or SPEC.default_dest_prefix)


def _resolved_ref(vm: dict, args) -> dict:
    """flag -> the VM's slack_export tag -> error. Never a default: there is
    no sane guess for which archive in a container is the export."""
    raw = (getattr(args, "export_ref", None)
           or (vm.get("tags") or {}).get(SPEC.loc_tag) or "")
    ref = parse_export_ref(raw)
    if not ref["kind"]:
        raise common.HarnessError(
            "no export location: pass --export-blob/--export-prefix, or "
            "re-create the VM (create-vm tags it) after discover-export")
    return ref


def validate_slack_token(raw: str) -> str:
    """The override token is a Slack token (xoxp-/xoxb-/xoxe-). Anything else
    is a paste error, and a single quote would corrupt the sourced env file."""
    tok = (raw or "").strip()
    if "'" in tok:
        raise common.HarnessError(
            "the token contains a single quote — it would break the VM env "
            "file; nothing was written")
    if not tok.startswith("xox"):
        raise common.HarnessError(
            f"{tok[:6]!r}… is not a Slack token — expected one starting with "
            "xoxp- (user), xoxb- (bot) or xoxe- (export)")
    return tok


# ── setup / run / status ────────────────────────────────────────────────────

def cmd_write_dest(root: Path, args) -> dict:
    """racwl SAS -> the rclone [azure] section (so check-azure has something
    to test against) AND dest-slack.env, both on-VM.

    DEST_URL is the BARE container URL — slack_vm_pull.py appends DEST_PREFIX
    itself (the teams convention), and it also READS the export through this
    same SAS: the container is both sides of this job.
    """
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    prefix = _dest_prefix(vm, args)
    ref = _resolved_ref(vm, args)
    sas, expiry = eng.mint_container_sas(cfg, args.sas_days, args.dry_run)
    base = _container_url(cfg)
    eng.write_conf_section(vm["public_ip"], "azure",
                           f"[azure]\ntype = azureblob\n"
                           f"sas_url = {base}?{sas}\n",
                           dry_run=args.dry_run)
    # values are SINGLE-QUOTED: the SAS contains '&', and sourcing an
    # unquoted VAR=a&b line backgrounds the assignment at the '&' — the var
    # lands empty and the puller hits Azure with no SAS at all (401, found
    # live on checkmate). az SAS/URLs never contain single quotes.
    env = (f"DEST_URL='{base}'\n"
           f"DEST_SAS='{sas}'\n"
           f"DEST_PREFIX='{prefix}'\n"
           f"SLACK_EXPORT_KIND='{ref['kind']}'\n"
           f"SLACK_EXPORT_BLOB='{ref.get('blob') or ''}'\n"
           f"SLACK_EXPORT_PREFIX='{ref.get('prefix') or ''}'\n")
    _write_env(vm["public_ip"], DEST_ENV, env, args.dry_run)
    return {"remote": "azure", "container": cfg["container"],
            "dest_prefix": prefix, "export": ref, "sas_expiry": expiry,
            "written_to": [f"{vm['name']}:{eng.RCLONE_CONF}",
                           f"{vm['name']}:{DEST_ENV}"]}


def cmd_write_creds(root: Path, args) -> dict:
    """OPTIONAL in this family — the export normally carries its own token.

    Use it only when probe reported `token-expired` or `no-file-links` AND the
    client supplied a Slack token with `files:read`. One stdin line; it lands
    in a 600 env file on the VM and is used as an Authorization: Bearer header
    (which Azure's x-ms-copy-source-authorization also speaks, so server-side
    copy still works). Stdin is read and validated FIRST, before any VM lookup
    — the qwilr fail-fast convention.
    """
    data = "" if sys.stdin.isatty() else sys.stdin.read()
    lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
    if not lines:
        if args.dry_run:
            lines = ["xoxp-dry-run-placeholder"]
        else:
            raise common.HarnessError(
                "stdin must be exactly 1 line: the Slack token — pipe it: "
                "write-creds <slug> <<'EOF' ... EOF")
    if len(lines) != 1:
        raise common.HarnessError(
            f"stdin had {len(lines)} lines; expected exactly 1 (the Slack "
            "token). Nothing was written.")
    token = validate_slack_token(lines[0])
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    _write_env(vm["public_ip"], SLACK_ENV, f"SLACK_TOKEN='{token}'\n",
               args.dry_run)
    return {"ok": True, "secret": "redacted",
            "written_to": f"{vm['name']}:{SLACK_ENV}",
            "note": "this OVERRIDES the export's own embedded token. Most "
                    "engagements never need it — see probe's link_gate."}


def cmd_transfer(root: Path, args) -> dict:
    """Fresh puller push -> tmux window 'slack', sourcing the dest env (and
    slack.env when a token override was written).

    slack_vm_pull.py is fully env-driven (no argv at all), so the pilot and
    tuning knobs ride `export` lines ahead of the interpreter, not CLI flags.
    """
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    if eng._tmux_alive(ip, args.dry_run):
        return {"ok": False, "cause": "already-running",
                "hint": "tmux session 'transfer' is alive — use status."}
    _push_puller(ip, args.dry_run)
    env_extra = ""
    if args.limit_files:
        env_extra += f"export LIMIT_FILES={args.limit_files}; "
    if args.rps_files:
        env_extra += f"export RPS_FILES={args.rps_files}; "
    if args.copy_workers:
        env_extra += f"export COPY_WORKERS={args.copy_workers}; "
    if args.shard_size:
        env_extra += f"export SHARD_SIZE={args.shard_size}; "
    env_extra += f"export RENDITIONS={1 if args.renditions else 0}; "
    inner = (f"set -a; . {DEST_ENV}; [ -f {SLACK_ENV} ] && . {SLACK_ENV}; "
             f"set +a; {env_extra}python3 {XFER_DIR}/slack_vm_pull.py "
             f"2>&1 | tee -a {LOG_FILE}")
    eng.run_ssh(ip, f'tmux new-session -d -s {eng.TMUX_SESSION} -n slack '
                    f'"{inner}"',
                dry_run=args.dry_run)
    if args.dry_run:
        # transfer_engine.run_ssh truncates its DRY-RUN line at 160 chars, so
        # the `export` prefix carrying these knobs is cut off there. A dry-run
        # that hides what the run would do is worthless — report them here.
        return {"ok": True, "dry_run": True,
                "limit_files": args.limit_files or None,
                "renditions": args.renditions,
                "env": env_extra.strip().rstrip(";"),
                "rps_files": args.rps_files or None,
                "copy_workers": args.copy_workers or None,
                "shard_size": args.shard_size or None}
    eng.run_ssh(ip, "sleep 5", check=False)
    alive = eng._tmux_alive(ip, False)
    tail = eng.run_ssh(ip, f"tail -3 {LOG_FILE} 2>/dev/null", check=False)
    return {"ok": alive, "session": eng.TMUX_SESSION, "window": "slack",
            "limit_files": args.limit_files or None,
            "renditions": args.renditions,
            "log_tail": (tail.stdout or "").strip().splitlines(),
            "note": ("re-running transfer is safe — every write is create-only "
                     "(If-None-Match: *), so the blob's own existence is the "
                     "resume record" if alive else None),
            "hint": None if alive else
            f"the puller died immediately — tail {LOG_FILE} on the VM (bad "
            "env files? export blob not found? expired export token?)"}


_STATUS_PY = r"""
import json, os, shutil
base = os.path.expanduser("~/xfer-slack")
dest = os.path.join(base, "dest")
out = {}
for key, rel in (("progress", "progress.json"),
                 ("manifest", os.path.join("_meta", "manifest.json"))):
    try:
        out[key] = json.load(open(os.path.join(dest, rel)))
    except (OSError, ValueError):
        out[key] = None
m = out.get("manifest")
if m:
    out["manifest"] = dict(counts=m.get("counts"),
                           totals=m.get("totals"),
                           shard_count=m.get("shard_count"),
                           declared_bytes=m.get("declared_bytes"),
                           failed_units=m.get("failed_units"),
                           finished_utc=m.get("finished_utc"))
try:
    out["log_tail"] = open(os.path.join(base, "pull-slack.log")
                           ).read().splitlines()[-5:]
except OSError:
    out["log_tail"] = []
try:
    out["disk_free_gb"] = round(shutil.disk_usage(base).free / 1e9, 1)
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
    copied = ((manifest or {}).get("totals") or {}).get("copied_bytes") or 0
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "transfer_running": alive,
            "copied_bytes_human": common.human_bytes(copied),
            "hint": hint,
            "note": "the ledger pass (phase 'ledger') walks the whole export "
                    "before a single file is copied — a long quiet stretch "
                    "there is normal, not a hang",
            **detail}


# ── verify (laptop side) ────────────────────────────────────────────────────

def compare_objects_to_blobs(objects, blobs: dict, prefix: str,
                             cap: int = 50) -> dict:
    """Pure. Certifies the ledger against what Azure actually committed.

    This family verifies harder than its siblings and the reason is specific:
    the Slack export DECLARES a byte `size` for every original file, so a
    landed blob whose Content-Length disagrees is a real integrity finding,
    not a heuristic. Renditions carry no declared size, so for them only
    presence and non-zero length can be asserted — that asymmetry is reported,
    never blurred into one number.

    Expected = rows the run says it copied or found already present. Rows
    recorded as gone/failed/absent are NOT expected; if one is present anyway
    (a later pass landed it) that is reported as informational, never a fault.
    """
    expected = missing = mismatched = zero_length = 0
    unexpected_present = 0
    originals = renditions = 0
    landed_bytes = 0
    missing_sample: list = []
    mismatch_sample: list = []
    for row in objects:
        name = f"{prefix}/{row['blob']}" if prefix else row["blob"]
        status = row.get("status")
        is_orig = row.get("kind") == "original"
        if status not in ("copied", "present"):
            if name in blobs:
                unexpected_present += 1
            continue
        expected += 1
        originals += 1 if is_orig else 0
        renditions += 0 if is_orig else 1
        got = blobs.get(name)
        if got is None:
            missing += 1
            if len(missing_sample) < cap:
                missing_sample.append({"blob": row["blob"],
                                       "file_id": row.get("file_id")})
            continue
        landed_bytes += got["size"]
        declared = int(row.get("size") or 0)
        if declared > 0 and got["size"] != declared:
            mismatched += 1
            if len(mismatch_sample) < cap:
                mismatch_sample.append({"blob": row["blob"],
                                        "declared": declared,
                                        "landed": got["size"]})
        elif declared == 0 and got["size"] == 0:
            zero_length += 1
    ok = missing == 0 and mismatched == 0
    return {
        "ok": ok,
        "expected_objects": expected,
        "originals": originals,
        "renditions": renditions,
        "landed_bytes": landed_bytes,
        "landed_bytes_human": common.human_bytes(landed_bytes),
        "missing": missing,
        "missing_sample": missing_sample,
        "size_mismatches": mismatched,
        "size_mismatch_sample": mismatch_sample,
        "zero_length_no_declared_size": zero_length,
        "unexpected_present": unexpected_present,
    }


def cmd_verify(root: Path, args) -> dict:
    """Laptop-side; the VM may already be torn down. Lists the dest prefix and
    compares it against the uploaded ledger. Mints only the READ (rl) account
    SAS — never the racwl write SAS — and takes no Slack credential at all."""
    cfg = eng.load_cfg(root, args.slug)
    common.run_az(["account", "set", "-s", cfg["subscription"]],
                  dry_run=args.dry_run)
    prefix = (args.dest_prefix or SPEC.default_dest_prefix).strip("/")
    we_added, ip = phases.ip_rule_ensure(cfg, args.dry_run)
    try:
        sas = phases.mint_sas(cfg, args.dry_run)  # rl — the READ path
        blobs = azure_list_blobs(cfg, sas, prefix + "/", args.dry_run)
        if args.dry_run:
            print(f"DRY-RUN: GET {_container_url(cfg)}/{prefix}/_meta/"
                  "manifest.json and objects.jsonl ?<sas-redacted>")
            return {"ok": True, "dry_run": True, "prefix": prefix}
        mname = f"{prefix}/_meta/manifest.json"
        oname = f"{prefix}/_meta/objects.jsonl"
        if mname not in blobs:
            return {"ok": False, "cause": "no-manifest", "prefix": prefix,
                    "blobs_under_prefix": len(blobs),
                    "hint": "no _meta/manifest.json under the prefix — the "
                            "pull never finished a pass. Run status/transfer "
                            "on the VM first."}
        base = _container_url(cfg)
        manifest = json.loads(azure_get(
            f"{base}/{urllib.parse.quote(mname, safe='/')}?{sas}"))
        if oname not in blobs:
            return {"ok": False, "cause": "no-object-ledger", "prefix": prefix,
                    "hint": "manifest.json is there but _meta/objects.jsonl "
                            "is not — the pass died between the two uploads. "
                            "Re-run transfer (it is create-only, so this is "
                            "cheap), then re-verify."}
        objects = (json.loads(ln) for ln in azure_get_lines(
            f"{base}/{urllib.parse.quote(oname, safe='/')}?{sas}"))
        result = compare_objects_to_blobs(objects, blobs, prefix)
    finally:
        phases.ip_rule_remove_if_ours(cfg, ip, we_added, args.dry_run)

    failed_units = manifest.get("failed_units") or []
    result["ok"] = result["ok"] and not failed_units
    result.update({
        "prefix": prefix,
        "blobs_under_prefix": len(blobs),
        "failed_units": failed_units,
        "failed_sample": (manifest.get("failed_sample") or [])[:10],
        "counts": manifest.get("counts"),
        "declared_bytes": manifest.get("declared_bytes"),
        "external_references": manifest.get("external_references"),
        "external_bytes": manifest.get("external_bytes"),
        "finished_utc": manifest.get("finished_utc"),
    })
    declared = (manifest.get("declared_bytes") or {}).get("hosted") or 0
    result["declared_hosted_bytes_human"] = common.human_bytes(declared)
    result["hint"] = None if result["ok"] else (
        "missing objects / size mismatches / failed shards: re-run transfer "
        "(writes are create-only, so a re-run only mops up), then re-verify. "
        "unexpected_present alone is informational — a later pass landed a "
        "file an earlier one recorded as gone.")
    if result["ok"]:
        result["note"] = (
            "declared-vs-landed byte equality is asserted for every ORIGINAL "
            "(the export declares a size for each) — a real source-truth "
            "claim, not staged->container only. Renditions carry no declared "
            "size, so only presence is asserted for them. "
            f"{result['renditions']} rendition objects are in that class. "
            "The external (gdrive) references in _meta/external-references."
            "jsonl are a KNOWN, quantified exclusion — Slack holds no bytes "
            "for them — not a shortfall. Next: pin the slack service in "
            "expected-data-sizes.json with both prefixes (the client's export "
            f'and "{prefix}"), then let size-company pick it up.')
    return result


# ── teardown / discover / plan ──────────────────────────────────────────────

def cmd_teardown(root: Path, args) -> dict:
    result = eng.cmd_teardown(SPEC, root, args)
    if result.get("ok") and "reminders" in result:
        result["reminders"].append(
            "If the client issued a Slack token for write-creds, tell them to "
            "revoke it now. The export's OWN embedded token needs nothing "
            "from us — it lapses with the export, which is also why a re-run "
            "months from now needs a fresh export, not this one.")
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
                "hint": "no transfer VM — run discover-export, then probe "
                        "(both laptop-only), then setup"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base,
                "hint": "VM exists but has no public IP (deallocated?)"}
    checks = [f"test -f {DEST_ENV} && echo dest-env",
              f"test -f {SLACK_ENV} && echo slack-env",
              f"test -f {DEST_DIR}/_meta/objects.jsonl && echo ledger",
              f"test -f {DEST_DIR}/_meta/manifest.json && echo manifest",
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
                        "(laptop-side); failed shards mean re-run transfer"}
    if "ledger" in out:
        return {"phase": "ledger-built", **base,
                "hint": "the export was parsed but no pass finished — re-run "
                        "transfer; the ledger is reused, not rebuilt"}
    if "dest-env" not in out:
        return {"phase": "mid-setup", **base,
                "hint": "VM up but dest env missing — resume setup at "
                        "write-dest"}
    return {"phase": "setup-complete", **base,
            "hint": "dest in place — run transfer --limit-files 200 "
                    "(pilot) first"}


_SLACK_PLAN_NOTE = (
    "Slack-specific: run discover-export then probe first — the export's "
    "download links expire WITH the export, and a dead token turns this into "
    "a fresh-export request (days of client work) before any VM is billed.")


def cmd_plan(root: Path, args) -> dict:
    """Under --dry-run this is hand-built and never shells out to az, so a
    caller piping stdout into json.loads() gets clean JSON (the teams
    convention). A real plan delegates to transfer_engine.cmd_plan for the SA
    region lookup — one source of truth for the shared dict shape."""
    if args.dry_run:
        cfg = eng.load_cfg(root, args.slug)
        return {
            "slug": args.slug,
            "vm_name": SPEC.vm_name(args.slug),
            "vm_size": args.vm_size,
            "region": "(unknown — dry-run)",
            "resource_group": args.rg or cfg["resource_group"],
            "storage_account": cfg["storage_account"],
            "container": cfg["container"],
            "dest": f"{cfg['container']}/{args.dest_prefix}",
            "source": SPEC.source_ref(args.export_ref or ""),
            "sas_expiry_days": args.sas_days,
            "note": _SLACK_PLAN_NOTE,
        }
    result = eng.cmd_plan(SPEC, root, args)
    result["note"] = result["note"] + " " + _SLACK_PLAN_NOTE
    return result


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[
        "discover", "discover-export", "plan", "create-vm", "allow-network",
        "write-dest", "check-azure", "write-creds", "probe", "transfer",
        "status", "verify", "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--export-blob", dest="export_blob", default=None,
                   help="the export .zip blob inside <slug>-raw "
                        "(from discover-export)")
    p.add_argument("--export-prefix", dest="export_prefix", default=None,
                   help="the prefix holding an already-extracted export; on "
                        "discover-export it narrows the listing instead")
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None,
                   help="create-vm: OS disk GB (default: the Spec's 256 — the "
                        "export ARCHIVE is staged here; file bytes never are)")
    p.add_argument("--dest-prefix", default=None,
                   help=f"prefix inside <slug>-raw (default "
                        f"{SPEC.default_dest_prefix})")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--sample", type=int, default=PROBE_DAY_SAMPLE,
                   help=f"probe: day files to census (default "
                        f"{PROBE_DAY_SAMPLE})")
    p.add_argument("--slack-token", dest="slack_token", default=None,
                   help="probe only: try this token instead of the export's "
                        "own (never written anywhere; use write-creds to "
                        "install it on the VM)")
    p.add_argument("--renditions", dest="renditions", action="store_true",
                   default=True,
                   help="copy thumbnails/transcodes too (DEFAULT). They "
                        "multiply object count several-fold — probe reports "
                        "both numbers")
    p.add_argument("--no-renditions", dest="renditions", action="store_false",
                   help="originals only")
    p.add_argument("--limit-files", dest="limit_files", type=int, default=0,
                   help="transfer: pilot — stop after N objects")
    p.add_argument("--rps-files", dest="rps_files", type=float, default=0.0,
                   help="transfer/probe: copy-request pacing ceiling "
                        f"(default {puller.DEFAULT_RPS_FILES}/s)")
    p.add_argument("--copy-workers", dest="copy_workers", type=int, default=0,
                   help="transfer/probe: concurrent copies (default "
                        f"{puller.DEFAULT_COPY_WORKERS})")
    p.add_argument("--shard-size", dest="shard_size", type=int, default=0,
                   help=f"transfer: objects per resumable shard (default "
                        f"{puller.SHARD_SIZE})")
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-transfer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    # One canonical location string for the VM tag; the two friendly flags
    # collapse into it here so nothing downstream has to know about both.
    if args.export_blob:
        args.export_ref = f"zip:{args.export_blob}"
    elif args.export_prefix and args.command != "discover-export":
        args.export_ref = f"tree:{args.export_prefix.strip('/')}"
    else:
        args.export_ref = None
    if args.command in ("plan", "create-vm", "probe") and not args.export_ref:
        p.error(f"{args.command} requires --export-blob or --export-prefix "
                "(run discover-export first)")
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    engine_cmds = {"create-vm": eng.cmd_create_vm,
                   "allow-network": eng.cmd_allow_network,
                   "check-azure": eng.cmd_check_azure}
    own_cmds = {"discover": cmd_discover, "discover-export":
                cmd_discover_export, "plan": cmd_plan,
                "write-dest": cmd_write_dest, "write-creds": cmd_write_creds,
                "probe": cmd_probe, "transfer": cmd_transfer,
                "status": cmd_status, "verify": cmd_verify,
                "teardown": cmd_teardown}
    try:
        if args.command in engine_cmds:
            result = engine_cmds[args.command](SPEC, root, args)
        else:
            result = own_cmds[args.command](root, args)
    except common.HarnessError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    except puller.CopyError as e:
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
