#!/usr/bin/env python3
"""AWS S3 -> Azure transfer CLI (azcopy server-side copy; VM family).

Reuses transfer_engine.py's VM lifecycle (create-vm / allow-network /
check-azure / teardown run the engine functions verbatim) but owns the copy
layer: instead of one rclone process streaming bytes through the VM, azcopy
drives Azure's Put Blob/Block From URL against presigned S3 GETs — the
storage fabric pulls from S3 directly, the VM issues only control calls.
That is why this path does in days what a streaming copy does in months on
a many-million-object bucket. rclone rides along with a SECRETLESS [s3]
remote (env_auth) for listing, probe and verify.

Subcommands: discover / plan / create-vm / allow-network / write-dest /
check-azure / write-s3-creds / probe / plan-jobs / transfer / status /
verify / teardown. Source location flag: --bucket (required for
plan/create-vm; later read from VM tags). See
.claude/skills/s3-azure-transfer/SKILL.md.

Secrets: the read-only AWS key arrives as 2 lines on stdin (key id, then
secret) and travels only over ssh stdin into a 600 env file on the VM —
never argv, tags, logs or laptop files. The dest SAS goes the same way.
Both die with the VM at teardown.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import transfer_engine as eng  # noqa: E402

SPEC = eng.Spec(
    source_name="s3",
    vm_prefix="xfer-s3-",
    purpose="s3-transfer",
    loc_tag="s3_bucket",
    loc_argname="bucket",
    loc_required=True,
    default_dest_prefix="s3-export",
    authorize_target="",  # no OAuth flow — static key on stdin instead
    remote_type="s3",
    remote_extra="provider = AWS\nenv_auth = true\n",
    default_os_disk_gb=512,  # azcopy job-plan files; 30 GB default would wedge
)

RUNNER_SH = Path(__file__).resolve().parent / "azcopy-runner.sh"
FLAT_PY = Path(__file__).resolve().parent / "s3_flat.py"
XFER_DIR = f"/home/{eng.ADMIN_USER}/xfer-jobs"
ENV_DIR = f"/home/{eng.ADMIN_USER}/.config/xfer"
AWS_ENV = f"{ENV_DIR}/aws.env"
DEST_ENV = f"{ENV_DIR}/dest.env"
COLD_TIERS = {"GLACIER", "DEEP_ARCHIVE"}  # GLACIER_IR reads in real time


# ── VM-side plumbing ─────────────────────────────────────────────────────────

def _write_env(ip: str, path: str, content: str, dry_run: bool) -> None:
    """600 env file on the VM; content rides ssh stdin only."""
    eng.run_ssh(ip, f"umask 077 && mkdir -p {ENV_DIR} && cat > {path}",
                stdin_data=content, dry_run=dry_run)


def _s3_ssh(ip: str, cmd: str, **kw) -> subprocess.CompletedProcess:
    """Run a VM command with the AWS env sourced (rclone env_auth needs it)."""
    return eng.run_ssh(ip, f"set -a; . {AWS_ENV} 2>/dev/null; set +a; {cmd}",
                       **kw)


def _push_runner(ip: str, dry_run: bool) -> None:
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > {XFER_DIR}/runner.sh "
                    f"&& chmod +x {XFER_DIR}/runner.sh",
                stdin_data=RUNNER_SH.read_text(), dry_run=dry_run)
    eng.run_ssh(ip, f"cat > {XFER_DIR}/s3_flat.py",
                stdin_data=FLAT_PY.read_text(), dry_run=dry_run)


def _dest_prefix(vm: dict, args) -> str:
    return (getattr(args, "dest_prefix", None)
            or vm["tags"].get("dest_prefix") or SPEC.default_dest_prefix)


def _bucket_region(ip: str, bucket: str, dry_run: bool) -> str:
    """x-amz-bucket-region via an unauthenticated HEAD (works even on 403)."""
    proc = eng.run_ssh(
        ip, f"curl -sI --max-time 20 https://{bucket}.s3.amazonaws.com/ "
            "| tr -d '\\r' | awk 'tolower($1)==\"x-amz-bucket-region:\""
            "{print $2}'",
        dry_run=dry_run, check=False, timeout=40)
    region = (proc.stdout or "").strip()
    return region if region and not dry_run else "(dry-run)" if dry_run else ""


def _tmux_windows(ip: str, dry_run: bool) -> list[str]:
    # '#W' (window_name alias) keeps braces out of the dry-run echo, which
    # the test harness scans with stdout.index("{") to find the JSON tail
    proc = eng.run_ssh(
        ip, f"tmux list-windows -t {eng.TMUX_SESSION} "
            "-F '#W' 2>/dev/null", dry_run=dry_run, check=False)
    return [w for w in (proc.stdout or "").split() if w]


# ── subcommands (the engine covers plan/create-vm/allow-network/check-azure) ─

def cmd_write_dest(root: Path, args) -> dict:
    """racwl SAS -> rclone [azure] section AND azcopy dest.env, both on-VM."""
    cfg = eng.load_cfg(root, args.slug)
    eng.set_subscription(cfg, args.dry_run)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    prefix = _dest_prefix(vm, args)
    sas, expiry = eng.mint_container_sas(cfg, args.sas_days, args.dry_run)
    base = (f"https://{cfg['storage_account']}.blob.core.windows.net/"
            f"{cfg['container']}")
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


def cmd_write_s3_creds(root: Path, args) -> dict:
    """2 stdin lines (key id, secret) -> 600 aws.env on the VM; then a
    secretless [s3] rclone section (env_auth) and one listing smoke test."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    bucket, _ = eng.loc_and_prefix(SPEC, vm, args)
    lines = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    if len(lines) != 2:
        raise common.HarnessError(
            "stdin must be exactly 2 lines: the AWS Access Key ID, then "
            "the Secret Access Key")
    key_id, secret = lines
    region = _bucket_region(vm["public_ip"], bucket, args.dry_run)
    if not region and not args.dry_run:
        return {"ok": False, "stage": "region-detect",
                "hint": f"bucket {bucket} returned no x-amz-bucket-region — "
                        "typo in the bucket name? (a nonexistent bucket "
                        "still 404s without the header)"}
    # single-quoted for the same reason as dest.env (see cmd_write_dest);
    # AWS secrets are base64-ish (never contain single quotes)
    env = (f"AWS_ACCESS_KEY_ID='{key_id}'\n"
           f"AWS_SECRET_ACCESS_KEY='{secret}'\n"
           f"AWS_REGION='{region}'\n"
           f"S3_BUCKET='{bucket}'\n"
           f"S3_SRC_URL='https://{bucket}.s3.{region}.amazonaws.com'\n")
    _write_env(vm["public_ip"], AWS_ENV, env, args.dry_run)
    section = (f"[s3]\ntype = s3\nprovider = AWS\nenv_auth = true\n"
               f"region = {region}\n")
    eng.write_conf_section(vm["public_ip"], "s3", section,
                           dry_run=args.dry_run)
    # smoke test with a RECURSIVE lsf, never lsd/--max-depth 1: rclone's
    # non-recursive listing buffers the entire directory before printing
    # (300M entries on a flat bucket = hang), and lsd on a flat bucket
    # emits nothing at all; only -R streams per page, so head fills after
    # the first page. Success = at least one key listed (this bucket is
    # never legitimately empty at setup time).
    ls = _s3_ssh(vm["public_ip"],
                 f"timeout 60 rclone lsf --files-only -R s3:{bucket} "
                 "2>/dev/null | head -5",
                 dry_run=args.dry_run, check=False, timeout=90)
    if not (ls.stdout or "").strip() and not args.dry_run:
        err = _s3_ssh(vm["public_ip"],
                      f"timeout 60 rclone lsf --files-only -R s3:{bucket} "
                      "2>&1 >/dev/null | head -5",
                      check=False, timeout=90)
        return {"ok": False, "stage": "rclone lsf s3",
                "output_tail": (err.stdout or "").strip()[-400:],
                "hint": "listing failed — wrong/inactive key, a policy "
                        "missing s3:ListBucket, or a requester-pays bucket. "
                        "Run probe for the requester-pays check."}
    return {"ok": True, "bucket": bucket, "region": region,
            "written_to": [f"{vm['name']}:{AWS_ENV}",
                           f"{vm['name']}:{eng.RCLONE_CONF}"],
            "listing_head": ls.stdout.strip()[:600]
            if not args.dry_run else ""}


def cmd_probe(root: Path, args) -> dict:
    """Day-one gate: can this key list the bucket, and can server-side copy
    actually move what's in it? Read-only, cheap, never a full enumeration."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    bucket, _ = eng.loc_and_prefix(SPEC, vm, args)
    ip = vm["public_ip"]
    region = _bucket_region(ip, bucket, args.dry_run)
    notes: list[str] = []
    # recursive lsf, never lsd/--max-depth 1: only -R streams per page;
    # non-recursive listings buffer the whole directory (300M entries on a
    # flat bucket) before printing, so head never fills and the ssh
    # timeout turns into a crash
    ls = _s3_ssh(ip, f"timeout 60 rclone lsf --files-only -R s3:{bucket} "
                     "2>/dev/null | head -5",
                 dry_run=args.dry_run, check=False, timeout=90)
    listing_ok = bool((ls.stdout or "").strip()) or (
        args.dry_run and ls.returncode == 0)
    requester_pays = False
    if not listing_ok and not args.dry_run:
        rp = _s3_ssh(ip, f"timeout 60 rclone lsf --files-only -R "
                         f"s3:{bucket} --s3-requester-pays 2>/dev/null "
                         "| head -5",
                     check=False, timeout=90)
        if (rp.stdout or "").strip():
            requester_pays, listing_ok = True, True
            notes.append(
                "REQUESTER-PAYS bucket: rclone can list it with "
                "--s3-requester-pays, but azcopy's requester-pays support "
                "is UNVERIFIED — pilot one prefix before promising a "
                "timeline, or ask the client to flip the payer setting "
                "for the engagement.")
    if not listing_ok and not args.dry_run:
        err = _s3_ssh(ip, f"timeout 60 rclone lsf --files-only -R "
                          f"s3:{bucket} 2>&1 >/dev/null | head -5",
                      check=False, timeout=90)
        return {"ok": False, "bucket": bucket, "region": region,
                "stage": "listing",
                "output_tail": (err.stdout or "").strip()[-400:],
                "hint": "the key cannot list the bucket — a client "
                        "conversation (key/policy), not a retry."}
    # key shape sample FIRST (recursive: only -R streams — see the smoke
    # test comment above): keys containing "/" prove folder structure; a
    # flat bucket's sample is all bare root keys. The --dirs-only survey
    # below must be gated on this, or it pages the whole bucket without
    # ever emitting a line.
    rs = _s3_ssh(ip, f"timeout 60 rclone lsf --files-only -R s3:{bucket} "
                     "2>/dev/null | head -1000",
                 dry_run=args.dry_run, check=False, timeout=90)
    sample = [ln for ln in (rs.stdout or "").splitlines() if ln.strip()]
    root_dirs = sum(1 for ln in sample if "/" in ln)
    root_files = len(sample) - root_dirs
    flat_suspected = root_files > 0 and root_dirs == 0
    if flat_suspected:
        notes.append(
            "FLAT bucket (files at root, no folder prefixes in the first "
            "1000 entries): plan-jobs will use chunked list-of-files mode "
            "— the full listing runs detached (~30 min sharded; re-run "
            "plan-jobs to poll), then the queue is built from chunk "
            "manifests.")
    # storage-class histogram of the FIRST listing pages — a sample, not a
    # census; a cold tail beyond it would surface as job failures + verify
    tiers: dict[str, int] = {}
    tp = _s3_ssh(ip, f"rclone lsf --format \"T\" --files-only -R "
                     f"s3:{bucket} 2>/dev/null | head -20000 "
                     "| sort | uniq -c | sort -rn",
                 dry_run=args.dry_run, check=False, timeout=300)
    for ln in (tp.stdout or "").splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0].isdigit():
            tiers[parts[1]] = int(parts[0])
    cold = sum(n for t, n in tiers.items() if t in COLD_TIERS)
    if cold:
        notes.append(
            f"{cold} of the sampled objects are GLACIER/DEEP_ARCHIVE — "
            "server-side copy FAILS on those (InvalidObjectState) until "
            "the client restores them. Out of scope until restored; "
            "say so up front.")
    if flat_suspected and not args.dry_run:
        # on a flat bucket --dirs-only never fills head and rclone pages
        # every key until the ssh timeout kills it — skip the survey
        prefixes: list[str] = []
    else:
        pf = _s3_ssh(ip, f"rclone lsf --dirs-only s3:{bucket} 2>/dev/null "
                         "| head -200", dry_run=args.dry_run, check=False,
                     timeout=300)
        prefixes = [p.rstrip("/") for p in (pf.stdout or "").splitlines()
                    if p.strip()]
    notes.append("tier histogram covers only the first ~20k listed objects "
                 "(a full 300M-object enumeration is hours of LIST calls "
                 "— deliberately not done here)")
    return {"ok": True, "bucket": bucket, "region": region,
            "listing_ok": listing_ok, "requester_pays": requester_pays,
            "root_sample": {"files": root_files, "dirs": root_dirs},
            "flat_suspected": flat_suspected,
            "tier_sample": tiers, "cold_objects_in_sample": cold,
            "top_level_prefixes": len(prefixes),
            "prefix_head": prefixes[:15], "notes": notes}


def cmd_plan_jobs(root: Path, args) -> dict:
    """Split the bucket into per-prefix azcopy jobs; write the queue on-VM.

    One recursive job per prefix keeps each azcopy plan file bounded (a
    single job over the whole bucket would build a 100+ GB plan). Splitting
    a level orphans the LOOSE objects at that level, so every split parent
    (and the root) also gets a shallow rclone job for exactly its own files.
    jobs.txt is the immutable master (verify iterates it); queue.txt is the
    consumable copy the workers drain.

    FLAT buckets (files at root, no folder prefixes — auto-detected, or
    forced with --flat/--no-flat) can't be prefix-split; they get chunked
    list-of-files jobs instead, built as a launch/poll/collect state
    machine because the full listing is hours-scale: launch starts the
    range-sharded listing detached in tmux window 'plan'; re-running
    plan-jobs polls it; once listing.done exists, the same invocation
    splits the manifest into chunks/ and writes the L/Q queue.
    --rebuild re-splits from the existing listing; --relist discards the
    listing itself and starts over.
    """
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    bucket, _ = eng.loc_and_prefix(SPEC, vm, args)
    ip = vm["public_ip"]
    windows = _tmux_windows(ip, args.dry_run)
    if any(w.startswith("w") and w[1:].isdigit() for w in windows):
        return {"ok": False, "cause": "transfer-running",
                "hint": "worker windows are alive — a new queue would be "
                        "drained mid-build. Use status."}
    if "verify" in windows:
        return {"ok": False, "cause": "verify-running",
                "hint": "a verify is running — wait for it (re-run verify "
                        "to poll) before re-planning."}
    if "plan" in windows:
        # flat listing in progress — poll it
        if args.rebuild or args.relist:
            return {"ok": False, "cause": "listing-running",
                    "hint": "the bucket listing is still running — wait, "
                            "or kill it: tmux kill-window -t "
                            f"{eng.TMUX_SESSION}:plan"}
        prog = eng.run_ssh(
            ip, f"wc -l {XFER_DIR}/shards/* {XFER_DIR}/listing.txt* "
                "2>/dev/null | tail -1 | awk '{print $1+0}'", check=False)
        n = (prog.stdout or "0").strip()
        return {"ok": True, "flat": True, "listing_running": True,
                "keys_listed_so_far": int(n or 0),
                "hint": "range-sharded listing runs detached — re-run "
                        "plan-jobs to poll; it will split into chunks "
                        "automatically once the listing completes"}
    if not args.dry_run and not args.rebuild:
        chk = eng.run_ssh(ip, f"test -s {XFER_DIR}/done.txt && echo has-progress",
                          check=False)
        if "has-progress" in (chk.stdout or ""):
            return {"ok": False, "cause": "progress-exists",
                    "hint": f"{XFER_DIR}/done.txt already records finished "
                            "jobs — re-planning would orphan them. To "
                            "resume, run transfer (safe). To rebuild the "
                            "queue anyway, pass --rebuild."}
    if not args.dry_run:
        if args.relist:
            eng.run_ssh(ip, f"rm -f {XFER_DIR}/listing.txt "
                            f"{XFER_DIR}/listing.txt.tmp "
                            f"{XFER_DIR}/listing.done && "
                            f"rm -rf {XFER_DIR}/shards", check=False)
        elif args.flat is not False:
            chk = eng.run_ssh(ip, f"test -s {XFER_DIR}/listing.done "
                                  "&& echo listed", check=False)
            if "listed" in (chk.stdout or ""):
                # collect state: listing done — split into the L/Q queue
                _push_runner(ip, args.dry_run)
                sp = eng.run_ssh(
                    ip, f"python3 {XFER_DIR}/s3_flat.py split "
                        f"--per-chunk {args.chunk_objects}",
                    check=False, timeout=3600)
                try:
                    res = json.loads((sp.stdout or "").strip().splitlines()[-1])
                except (json.JSONDecodeError, IndexError):
                    return {"ok": False, "cause": "split-failed",
                            "output_tail": ((sp.stdout or "")
                                            + (sp.stderr or ""))[-400:]}
                res.update({"flat": True, "bucket": bucket,
                            "queue": f"{vm['name']}:{XFER_DIR}/queue.txt",
                            "note": "L jobs are server-side Put-Blob-From-"
                                    "URL copies (s3_flat.py, API-enforced "
                                    "create-only); Q jobs (if any) stream "
                                    "unsafe-named keys via rclone. Pilot "
                                    "one job before scaling."})
                return res
    flat = args.flat
    if flat is None:
        # cheap shape sample: recursive (-R streams per page; non-recursive
        # buffers the whole dir), SIGPIPE-terminated after ~one page. Keys
        # containing "/" prove folder structure.
        rs = _s3_ssh(ip, f"timeout 60 rclone lsf --files-only -R "
                         f"s3:{bucket} 2>/dev/null | head -1000",
                     dry_run=args.dry_run, check=False, timeout=90)
        sample = [ln for ln in (rs.stdout or "").splitlines() if ln.strip()]
        dirs_n = sum(1 for ln in sample if "/" in ln)
        flat = len(sample) > 0 and dirs_n == 0
    if flat:
        # launch state: start the detached range-sharded listing
        _push_runner(ip, args.dry_run)
        cmd = f"\"{XFER_DIR}/runner.sh list-bucket\""
        if eng._tmux_alive(ip, args.dry_run):
            launch = f"tmux new-window -t {eng.TMUX_SESSION} -n plan {cmd}"
        else:
            launch = (f"tmux new-session -d -s {eng.TMUX_SESSION} "
                      f"-n plan {cmd}")
        eng.run_ssh(ip, launch, dry_run=args.dry_run)
        return {"ok": True, "flat": True, "listing_started": True,
                "bucket": bucket,
                "hint": "full bucket listing runs detached in tmux window "
                        "'plan' (range-sharded boto3; ~30 min at 300M "
                        "keys, hours if it falls back to sequential) — "
                        "re-run plan-jobs to poll, then it splits into "
                        "chunk jobs automatically"}
    ls = _s3_ssh(ip, f"rclone lsf --dirs-only --max-depth 2 -R s3:{bucket} "
                     "2>/dev/null", dry_run=args.dry_run, check=False,
                 timeout=3600)
    dirs = [d.rstrip("/") for d in (ls.stdout or "").splitlines()
            if d.strip()]
    d1 = [d for d in dirs if "/" not in d]
    children: dict[str, list[str]] = {}
    for d in dirs:
        if "/" in d:
            parent = d.split("/", 1)[0]
            children.setdefault(parent, []).append(d)
    depth = args.split_depth
    use_depth2 = (depth == 2 or (depth == 0 and len(d1) < args.min_jobs))
    jobs: list[tuple[str, str]] = [("S", "")]  # loose objects at the root
    if use_depth2:
        n_deep = sum(len(children.get(p, [])) or 1 for p in d1) + len(
            [p for p in d1 if children.get(p)])
        if n_deep > args.max_jobs:
            use_depth2 = False  # a monster fan-out: depth 1 is safer
    if use_depth2:
        for p in d1:
            kids = children.get(p, [])
            if kids:
                jobs.append(("S", p))  # p's own loose files
                jobs += [("R", k) for k in kids]
            else:
                jobs.append(("R", p))
        depth_used = 2
    else:
        jobs += [("R", p) for p in d1]
        depth_used = 1
    content = "".join(f"{t}\t{p}\n" for t, p in jobs)
    eng.run_ssh(ip, f"mkdir -p {XFER_DIR} && cat > {XFER_DIR}/jobs.txt "
                    f"&& cp {XFER_DIR}/jobs.txt {XFER_DIR}/queue.txt "
                    f"&& rm -f {XFER_DIR}/done.txt {XFER_DIR}/failed.txt "
                    f"{XFER_DIR}/verify.tsv",
                stdin_data=content, dry_run=args.dry_run)
    return {"ok": True, "bucket": bucket, "jobs": len(jobs),
            "recursive_jobs": sum(1 for t, _ in jobs if t == "R"),
            "shallow_jobs": sum(1 for t, _ in jobs if t == "S"),
            "split_depth_used": depth_used if not args.dry_run else None,
            "queue": f"{vm['name']}:{XFER_DIR}/queue.txt",
            "sample": [f"{t} {p or '(root)'}" for t, p in jobs[:10]],
            "note": "each R job is one azcopy server-side copy; S jobs are "
                    "the loose files a split level would orphan (those few "
                    "stream via rclone)"}


def cmd_transfer(root: Path, args) -> dict:
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    if eng._tmux_alive(ip, args.dry_run):
        return {"ok": False, "cause": "already-running",
                "hint": "tmux session 'transfer' is alive — use status."}
    # sweep orphaned in-flight markers (worker/VM died mid-job) back into
    # the queue — without this a crashed job is silently lost until verify;
    # safe here because the guard above proved no workers are running
    eng.run_ssh(ip, f"cd {XFER_DIR} 2>/dev/null && ls inflight/* "
                    ">/dev/null 2>&1 && (flock 9; cat inflight/* >> "
                    "queue.txt; rm -f inflight/*) 9>>queue.lock || true",
                dry_run=args.dry_run, check=False)
    if args.requeue_failed and not args.dry_run:
        eng.run_ssh(ip, f"cd {XFER_DIR} && touch failed.txt && "
                        "awk -F'\\t' '{print $2 \"\\t\" $3}' failed.txt "
                        ">> queue.txt && mv failed.txt "
                        "failed.requeued.$(date +%s)", check=False)
    if not args.dry_run:
        chk = eng.run_ssh(ip, f"test -s {XFER_DIR}/queue.txt && echo queued",
                          check=False)
        if "queued" not in (chk.stdout or ""):
            return {"ok": False, "cause": "no-queue",
                    "hint": "queue.txt is missing or empty — run plan-jobs "
                            "first (or the queue is fully drained: run "
                            "status, then verify)."}
    _push_runner(ip, args.dry_run)
    windows = 1 if args.pilot else args.windows
    max_jobs = 1 if args.pilot else 0
    launch = (f"tmux new-session -d -s {eng.TMUX_SESSION} -n w1 "
              f"\"{XFER_DIR}/runner.sh worker 1 {args.concurrency} "
              f"{max_jobs}\"")
    for n in range(2, windows + 1):
        launch += (f" && tmux new-window -t {eng.TMUX_SESSION} -n w{n} "
                   f"\"{XFER_DIR}/runner.sh worker {n} {args.concurrency} "
                   f"{max_jobs}\"")
    eng.run_ssh(ip, launch, dry_run=args.dry_run)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "windows": windows}
    eng.run_ssh(ip, "sleep 5", check=False)
    alive = eng._tmux_alive(ip, False)
    hb = eng.run_ssh(ip, f"tail -qn 1 {XFER_DIR}/w*.log 2>/dev/null",
                     check=False)
    return {"ok": alive, "session": eng.TMUX_SESSION, "windows": windows,
            "pilot": args.pilot, "concurrency": args.concurrency,
            "heartbeats": (hb.stdout or "").strip().splitlines(),
            "note": ("pilot: exactly one job runs, then the worker exits — "
                     "measure objects/s via status before scaling --windows"
                     if args.pilot else
                     "re-running transfer after an interruption is safe — "
                     "--overwrite=false skips blobs that already landed")
            if alive else "workers died immediately — run status"}


_STATUS_PY = r"""
import json, os, shutil, subprocess, time
base = os.path.expanduser("~/xfer-jobs")
def rows(name):
    try:
        return [ln.split("\t") for ln in
                open(os.path.join(base, name)).read().splitlines() if ln]
    except OSError:
        return []
def num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0
done, failed = rows("done.txt"), rows("failed.txt")
try:
    pending = sum(1 for ln in open(os.path.join(base, "queue.txt"))
                  if ln.strip())
except OSError:
    pending = None
inflight = []
infd = os.path.join(base, "inflight")
if os.path.isdir(infd):
    for f in sorted(os.listdir(infd)):
        try:
            inflight.append(open(os.path.join(infd, f)).read().strip()
                            .replace("\t", " "))
        except OSError:
            pass
# objects/bytes come from BOTH ledgers: a job lands in failed.txt if ANY
# object failed, but a chunk with 150 failures out of 250k still copied
# 249,850 — counting only done.txt under-reported a real run by 13x
finished = [r for r in done + failed if len(r) >= 9]
files = sum(num(r[4]) + num(r[6]) for r in finished)
bytes_ = sum(num(r[7]) for r in finished)
objects_failed = sum(num(r[5]) for r in finished)
p = subprocess.run(["tmux", "list-windows", "-t", "transfer",
                    "-F", "#{window_name}"], capture_output=True, text=True)
windows = [w for w in (p.stdout or "").split() if w]
now = time.time()
recent = [r for r in finished if num(r[0]) > now - 1800]
rate = None
if recent:
    span = max(1, int(now) - min(num(r[0]) - num(r[8]) for r in recent))
    rate = round(sum(num(r[4]) for r in recent) / span, 1)
eta_hours = None
nwork = sum(1 for w in windows if w.startswith("w")) or 1
if pending and finished:
    # weight by RECENT jobs when we have them: early chunks in an
    # already-populated keyspace are all-skip and finish far too fast to
    # predict fresh-copy work
    sample = recent or finished
    avg = sum(num(r[8]) for r in sample) / len(sample)
    eta_hours = round(pending * avg / nwork / 3600.0, 1)
hb = []
for f in sorted(os.listdir(base)):
    if f.startswith("w") and f.endswith(".log"):
        lines = open(os.path.join(base, f)).read().splitlines()
        if lines:
            hb.append(lines[-1])
du = shutil.disk_usage(base)
print(json.dumps({
    "jobs": {"pending": pending, "inflight": inflight,
             "done": len(done), "failed": len(failed)},
    "files_done": files, "bytes_done": bytes_,
    "objects_failed": objects_failed,
    "recent_files_per_sec": rate, "eta_hours": eta_hours,
    "tmux_windows": windows, "worker_heartbeats": hb,
    "recent_failures": ["\t".join(r)[:200] for r in failed[-5:]],
    "workdir_free_gb": round(du.free / 1e9, 1)}))
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
    detail.setdefault("files_done", None)
    return {"vm": vm["name"], "power_state": vm["power_state"],
            "transfer_running": alive,
            "files_done_human": f"{detail['files_done']:,}"
            if isinstance(detail.get("files_done"), int) else None,
            "bytes_done_human": common.human_bytes(detail.get("bytes_done"))
            if detail.get("bytes_done") else None, **detail}


def _verify_stale(done_line: str, ledger_ts: str) -> bool:
    """True when copy jobs finished AFTER the verify that produced
    done_line — its numbers no longer describe the container."""
    try:
        verify_ts = int(done_line.split("\t")[1])
        return int(ledger_ts or 0) > verify_ts
    except (IndexError, ValueError):
        return False


def cmd_verify(root: Path, args) -> dict:
    """Tier-1: per-job count+byte rollup S3 vs Azure (listing-only), run in
    a tmux window; --deep adds rclone check --size-only. Size-match is the
    honest invariant — S3 multipart ETags aren't MD5s, so content-hash
    verification does not exist at this scale."""
    cfg = eng.load_cfg(root, args.slug)
    vm = eng.require_vm(SPEC, cfg, args.slug, args.dry_run)
    ip = vm["public_ip"]
    if args.prefix is not None:
        bucket, _ = eng.loc_and_prefix(SPEC, vm, args)
        prefix = _dest_prefix(vm, args)
        sub = f"/{args.prefix}" if args.prefix else ""
        s3 = _s3_ssh(ip, f"rclone size --json 's3:{bucket}{sub}'",
                     dry_run=args.dry_run, check=False, timeout=1800)
        az = _s3_ssh(ip, f"rclone size --json "
                         f"'azure:{cfg['container']}/{prefix}{sub}'",
                     dry_run=args.dry_run, check=False, timeout=1800)
        if args.dry_run:
            return {"ok": True, "dry_run": True}
        try:
            s3d, azd = json.loads(s3.stdout), json.loads(az.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "cause": "listing-failed",
                    "output_tail": (s3.stdout + az.stdout)[-300:]}
        match = (s3d.get("count") == azd.get("count")
                 and s3d.get("bytes") == azd.get("bytes"))
        return {"ok": match, "prefix": args.prefix,
                "s3": s3d, "azure": azd,
                "hint": None if match else
                "counts/bytes differ — re-run transfer (resume is safe), "
                "then re-verify"}
    windows = _tmux_windows(ip, args.dry_run)
    # LAST= / LEDGER= markers, never line positions: an empty field must
    # not be able to masquerade as the next one
    state = eng.run_ssh(
        ip, f"echo \"LAST=$(tail -1 {XFER_DIR}/verify.tsv 2>/dev/null)\"; "
            f"echo \"LEDGER=$(cat {XFER_DIR}/done.txt {XFER_DIR}/failed.txt "
            "2>/dev/null | awk -F'\\t' '{if ($1+0 > m) m = $1+0} "
            "END {print m+0}')\"",
        dry_run=args.dry_run, check=False)
    out_lines = (state.stdout or "").splitlines()
    last = next((l[5:] for l in out_lines if l.startswith("LAST=")),
                "").strip()
    ledger_ts = next((l[7:] for l in out_lines if l.startswith("LEDGER=")),
                     "0").strip()
    if args.dry_run:
        pass
    elif last.startswith("#error"):
        return {"ok": False, "cause": "verify-aborted",
                "detail": last,
                "hint": "the flat comparator hit a listing-order violation "
                        "— inspect verify.tsv and plan.log on the VM "
                        "before re-running verify"}
    elif last.startswith("#done") and _verify_stale(last, ledger_ts):
        # copy work landed AFTER this verify finished (e.g. a mop-up):
        # collecting would report stale numbers as if they were fresh —
        # the one way this command could certify the wrong thing
        eng.run_ssh(ip, f"mv {XFER_DIR}/verify.tsv "
                        f"{XFER_DIR}/verify.tsv.superseded.$(date +%s)",
                    check=False)
        last = ""  # fall through to the launch path below
    elif last.startswith("#done"):
        parts = dict(kv.split("=") for kv in last.split("\t")[2:]
                     if "=" in kv)
        bad = int(parts.get("bad", 0))
        rows = eng.run_ssh(
            ip, f"awk -F'\\t' '$7 != \"ok\" && $1 !~ /^#/' "
                f"{XFER_DIR}/verify.tsv | head -50", check=False)
        totals = {k: int(v) for k, v in parts.items()
                  if k in ("s3_count", "s3_bytes", "az_count", "az_bytes")}
        return {"ok": bad == 0, "clean": bad == 0,
                "jobs_ok": int(parts.get("ok", 0)), "jobs_bad": bad,
                **({"totals": totals} if totals else {}),
                "mismatches": (rows.stdout or "").strip().splitlines(),
                "hint": None if bad == 0 else
                "MISMATCH rows: re-run transfer --requeue-failed (or plain "
                "transfer for skips), then verify again. After a clean "
                "verify, size-company picks the data up as the s3-export "
                "source. Flat mode: EXTRA-DEST rows are dest blobs absent "
                "from the cutoff manifest (e.g. the client's own "
                "post-cutoff pushes) — reported, never touched."}
    elif "verify" in windows:
        if last.startswith("#progress"):
            f = last.split("\t")
            return {"ok": True, "verify_running": True,
                    "progress": f"{f[1]} objects compared"
                    if len(f) > 1 else None,
                    "hint": "flat manifest-vs-Azure compare in progress — "
                            "re-run verify later to collect"}
        prog = eng.run_ssh(
            ip, f"wc -l < {XFER_DIR}/verify.tsv 2>/dev/null; "
                f"wc -l < {XFER_DIR}/jobs.txt 2>/dev/null", check=False)
        nums = (prog.stdout or "").split()
        return {"ok": True, "verify_running": True,
                "progress": f"{nums[0]}/{nums[1]} jobs checked"
                if len(nums) == 2 else None,
                "hint": "listing-only but slow at scale — re-run verify "
                        "later to collect"}
    elif any(w.startswith("w") and w[1:].isdigit() for w in windows):
        return {"ok": False, "cause": "transfer-running",
                "hint": "worker windows are still alive — verifying "
                        "mid-copy just reports mismatches. Wait for "
                        "status to show the queue drained."}
    _push_runner(ip, args.dry_run)
    mode = "deep" if args.deep else ""
    cmd = f"\"{XFER_DIR}/runner.sh verify {mode}\""
    if eng._tmux_alive(ip, args.dry_run):
        launch = f"tmux new-window -t {eng.TMUX_SESSION} -n verify {cmd}"
    else:
        launch = (f"tmux new-session -d -s {eng.TMUX_SESSION} "
                  f"-n verify {cmd}")
    eng.run_ssh(ip, launch, dry_run=args.dry_run)
    return {"ok": True, "verify_started": True, "deep": args.deep,
            "dry_run": args.dry_run or None,
            "hint": "rollup is written incrementally to "
                    f"{XFER_DIR}/verify.tsv — re-run verify to collect. "
                    "Flat engagements (L jobs) auto-switch to the "
                    "manifest-vs-Azure per-object compare, where --deep "
                    "is implied (name+size per object IS the deep check)"}


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
                "hint": "no transfer VM — run setup to begin"}
    base = {"vm": vm["name"], "public_ip": vm["public_ip"],
            "power_state": vm["power_state"], "tags": vm["tags"]}
    if not vm["public_ip"]:
        return {"phase": "vm-no-public-ip", **base,
                "hint": "VM exists but has no public IP (deallocated?)"}
    probe = eng.run_ssh(
        vm["public_ip"],
        f"test -f {AWS_ENV} && echo aws-env; "
        f"test -f {DEST_ENV} && echo dest-env; "
        "rclone listremotes 2>/dev/null | tr '\\n' ' '; echo; "
        f"test -s {XFER_DIR}/queue.txt && echo queue; "
        f"test -s {XFER_DIR}/done.txt && echo progress; "
        f"tmux list-windows -t {eng.TMUX_SESSION} -F '#{{window_name}}' "
        "2>/dev/null | tr '\\n' ' '", check=False)
    if probe.returncode != 0:
        return {"phase": "vm-unreachable", **base,
                "hint": "ssh failed — VM booting, or your key changed. "
                        f"Try: ssh {eng.ADMIN_USER}@{vm['public_ip']}"}
    out = probe.stdout or ""
    windows = out.splitlines()[-1].split() if out.splitlines() else []
    workers = any(w.startswith("w") and w[1:].isdigit() for w in windows)
    if not ("aws-env" in out and "dest-env" in out and "azure:" in out):
        return {"phase": "mid-setup", **base,
                "hint": "VM up but creds/dest incomplete — resume setup at "
                        "the missing write-dest / write-s3-creds step."}
    if "plan" in windows and not workers:
        return {"phase": "listing-running", **base,
                "hint": "flat-bucket listing in progress — run plan-jobs "
                        "to poll (it collects + splits automatically when "
                        "the listing completes)"}
    if "verify" in windows and not workers:
        return {"phase": "verify-running", **base,
                "hint": "run verify to see progress"}
    if workers:
        return {"phase": "transfer-running", **base,
                "hint": "use status for progress"}
    if "progress" in out:
        return {"phase": "transfer-stopped", **base,
                "hint": "workers dead but done.txt has rows — run status, "
                        "then transfer (resume) or verify (drained queue)."}
    if "queue" in out:
        return {"phase": "jobs-planned", **base,
                "hint": "queue built, nothing started — run transfer "
                        "(--pilot first on a new engagement)"}
    return {"phase": "setup-complete", **base,
            "hint": "creds + dest in place — run probe, then plan-jobs"}


def cmd_teardown(root: Path, args) -> dict:
    result = eng.cmd_teardown(SPEC, root, args)
    if result.get("ok") and "reminders" in result:
        result["reminders"][-1] = (
            "Tell the client they can delete the read-only IAM access key "
            "now — deletion on their side is the clean end of the "
            "engagement")
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[
        "discover", "plan", "create-vm", "allow-network", "write-dest",
        "check-azure", "write-s3-creds", "probe", "plan-jobs", "transfer",
        "status", "verify", "teardown"])
    p.add_argument("slug")
    p.add_argument("--root", default=str(common.DEFAULT_COMPANIES_ROOT))
    p.add_argument("--bucket", default=None,
                   help="S3 bucket name (required for plan/create-vm; "
                        "later read from the VM's s3_bucket tag)")
    p.add_argument("--rg", help="override VM resource group "
                               "(default: company's RG)")
    p.add_argument("--vm-size", default="Standard_D8s_v7")
    p.add_argument("--os-disk-gb", dest="os_disk_gb", type=int, default=None,
                   help="create-vm: OS disk GB (default: the Spec's 512 — "
                        "azcopy job-plan files need the room)")
    p.add_argument("--dest-prefix", default=None,
                   help=f"prefix inside <slug>-raw "
                        f"(default {SPEC.default_dest_prefix})")
    p.add_argument("--sas-days", type=int, default=21)
    p.add_argument("--split-depth", type=int, choices=(0, 1, 2), default=0,
                   help="plan-jobs: prefix depth for job splitting "
                        "(0 = auto: deepen to 2 when depth 1 yields fewer "
                        "than --min-jobs prefixes)")
    p.add_argument("--min-jobs", type=int, default=64,
                   help="plan-jobs auto mode: deepen the split below this "
                        "many jobs")
    p.add_argument("--max-jobs", type=int, default=4096,
                   help="plan-jobs: fall back to depth 1 above this many "
                        "jobs")
    p.add_argument("--windows", type=int, default=8,
                   help="transfer: parallel worker windows (each runs one "
                        "azcopy job at a time)")
    p.add_argument("--concurrency", type=int, default=200,
                   help="transfer: AZCOPY_CONCURRENCY_VALUE per worker")
    p.add_argument("--pilot", action="store_true",
                   help="transfer: run exactly ONE job in one window — "
                        "calibrate objects/s before scaling")
    p.add_argument("--requeue-failed", dest="requeue_failed",
                   action="store_true",
                   help="transfer: move failed.txt jobs back into the queue")
    p.add_argument("--flat", dest="flat", action="store_const", const=True,
                   default=None,
                   help="plan-jobs: force flat-bucket mode (chunked "
                        "list-of-files jobs); default is auto-detect from "
                        "a 1000-entry root sample")
    p.add_argument("--no-flat", dest="flat", action="store_const",
                   const=False,
                   help="plan-jobs: force the folder-prefix split even if "
                        "the root sample looks flat")
    p.add_argument("--chunk-objects", dest="chunk_objects", type=int,
                   default=250_000,
                   help="plan-jobs (flat): keys per chunk manifest "
                        "(default 250k — azcopy list-of-files degrades "
                        "well above this; drop it if the pilot's "
                        "enumeration drags)")
    p.add_argument("--relist", action="store_true",
                   help="plan-jobs (flat): discard listing.txt and redo "
                        "the expensive bucket listing (--rebuild alone "
                        "re-splits from the existing listing)")
    p.add_argument("--rebuild", action="store_true",
                   help="plan-jobs: rebuild the queue even though done.txt "
                        "records finished jobs")
    p.add_argument("--deep", action="store_true",
                   help="verify: add per-object rclone check --size-only")
    p.add_argument("--prefix", default=None,
                   help="verify: synchronous spot-check of one prefix")
    p.add_argument("--confirmed", action="store_true",
                   help="teardown only: user confirmed the deletion plan")
    p.add_argument("--force", action="store_true",
                   help="teardown only: skip the running-transfer check")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.command in ("plan", "create-vm") and args.bucket is None:
        p.error(f"{args.command} requires --bucket")
    if args.dest_prefix is None and args.command in ("plan", "create-vm"):
        args.dest_prefix = SPEC.default_dest_prefix

    root = Path(args.root)
    engine_cmds = {"plan": eng.cmd_plan, "create-vm": eng.cmd_create_vm,
                   "allow-network": eng.cmd_allow_network,
                   "check-azure": eng.cmd_check_azure}
    own_cmds = {"discover": cmd_discover, "write-dest": cmd_write_dest,
                "write-s3-creds": cmd_write_s3_creds, "probe": cmd_probe,
                "plan-jobs": cmd_plan_jobs, "transfer": cmd_transfer,
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
    except subprocess.TimeoutExpired as e:
        print(json.dumps({"ok": False,
                          "error": f"timeout: {str(e)[:200]}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
