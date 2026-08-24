#!/usr/bin/env python3
"""Flat-bucket helpers for the s3-azure-transfer skill.

Runs ON THE TRANSFER VM: pushed by s3_transfer.py's _push_runner next to
azcopy-runner.sh, which invokes it with aws.env + dest.env already sourced
into the environment. Three verbs:

  list    range-sharded parallel S3 listing -> listing.txt ("key\tsize",
          globally sorted) + listing.done sentinel (JSON: keys, unsafe)
  split   listing.txt -> chunks/chunk-NNNNN manifests (azcopy
          --list-of-files input; unsafe-charset keys go to
          quarantine-NNNNN manifests copied by rclone --files-from)
          + jobs.txt/queue.txt -- jobs.txt is written LAST (commit point)
  verify  stream merge-join of listing.txt (the cutoff manifest) vs the
          Azure dest-prefix listing -> verify.tsv (mismatch rows in the
          runner's 8-field shape + #progress/#done sentinels)

Why range shards, not prefix filters: ListObjectsV2 StartAfter is
exclusive, so range i covers start_after < key <= end; every key falls in
exactly one range regardless of its charset, and concatenating shard
outputs in range order is a complete, globally sorted listing. A flat
300M-object bucket lists in ~minutes at 64-wide instead of the 8-21 h a
single continuation-token chain costs.

Why the Azure side is a single sequential listing: List Blobs markers are
opaque (no StartAfter equivalent), so key-range sharding is impossible
there; one complete prefix listing is the only order-preserving,
gap-free enumeration. ~60k pages at 5000/page -- budget 1-3 h.

Trust boundary: secrets arrive only via the environment (sourced from the
600 env files); nothing here prints or persists them. Import time is
stdlib-only so the laptop test harness can unit-test the pure functions;
boto3 (VM apt package python3-boto3) is imported lazily inside the S3
shard lister only.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = os.path.expanduser("~/xfer-jobs")

# Keys azcopy --list-of-files is trusted with. Anything else (spaces, '+',
# unicode, ...) has documented URL-encoding pitfalls in azcopy list files,
# so those keys ride rclone --files-from instead (Q jobs) -- slower per
# object but byte-exact on names, and expected to be ~empty on hex-hash
# buckets.
SAFE_KEY_RE = re.compile(r"^[0-9A-Za-z._/-]+$")

DEFAULT_SHARDS = 256
DEFAULT_LIST_WORKERS = 64
PROGRESS_EVERY = 5_000_000
MISMATCH_CAP = 1000


def log(msg: str) -> None:
    with open(os.path.join(BASE, "plan.log"), "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                + " " + msg + "\n")


# ── pure logic (unit-tested on the laptop) ──────────────────────────────────

def range_bounds(n: int) -> list[tuple[str | None, str | None]]:
    """n key ranges over the keyspace as (start_after, end) pairs.

    Range i covers keys k with start_after < k <= end (None = unbounded),
    matching ListObjectsV2 semantics: StartAfter is exclusive, so a key
    exactly equal to a boundary lands in the range that ENDS there.
    Bounds are hex strings; keys sorting before "0"/after "f" fall in the
    first/last range, so coverage is total for any key charset.
    """
    if n < 2:
        return [(None, None)]
    k = 1
    while 16 ** k < n:
        k += 1
    bounds: list[str] = []
    for i in range(1, n):
        b = format(i * 16 ** k // n, f"0{k}x")
        if not bounds or b != bounds[-1]:
            bounds.append(b)
    ranges: list[tuple[str | None, str | None]] = [(None, bounds[0])]
    ranges += list(zip(bounds, bounds[1:]))
    ranges.append((bounds[-1], None))
    return ranges


def split_listing(lines, per_chunk: int):
    """Pure generator: manifest lines ("key\tsize") -> yields (name, text)
    manifests. chunk-NNNNN keep the full "key\tsize" rows (the copy-chunk
    engine routes single-shot vs block-staged by size WITHOUT any HEADs);
    quarantine-NNNNN hold bare keys (rclone --files-from input) for names
    outside the safe charset. Blank lines dropped; O(per_chunk) memory."""
    safe: list[str] = []
    quar: list[str] = []
    ci = qi = 0
    for ln in lines:
        ln = ln.rstrip("\n")
        if not ln:
            continue
        key = ln.split("\t", 1)[0]
        if SAFE_KEY_RE.match(key):
            safe.append(ln)
            if len(safe) >= per_chunk:
                yield f"chunk-{ci:05d}", "".join(r + "\n" for r in safe)
                ci += 1
                safe = []
        else:
            quar.append(key)
            if len(quar) >= per_chunk:
                yield f"quarantine-{qi:05d}", "".join(k + "\n" for k in quar)
                qi += 1
                quar = []
    if safe:
        yield f"chunk-{ci:05d}", "".join(r + "\n" for r in safe)
    if quar:
        yield f"quarantine-{qi:05d}", "".join(k + "\n" for k in quar)


def merge_join(manifest_rows, azure_rows, out, now=time.time,
               missing_out=None, sizediff_out=None):
    """Stream compare two lexically sorted (key, size) iterators; write
    mismatch rows (runner 8-field shape, capped at MISMATCH_CAP) plus
    #progress sentinels to `out`. Returns totals for the #done line.
    missing_out (uncapped, one "key\tsize" per line) collects EVERY
    MISSING-DEST key so a mop-up pass can copy exactly the shortfall
    instead of re-scanning whole failed chunks (SIZE-DIFF is excluded:
    the create-only engine cannot overwrite, deliberately). Raises
    SortError if either stream violates strict ascending order -- the
    one assumption the whole verify rests on."""
    stats = {"ok": 0, "bad": 0, "s3_count": 0, "s3_bytes": 0,
             "az_count": 0, "az_bytes": 0}
    written = 0
    compared = 0

    def emit(key, s3c, s3b, azc, azb, status):
        nonlocal written
        stats["bad"] += 1
        if written < MISMATCH_CAP:
            out.write(f"F\t{key}\t{s3c}\t{s3b}\t{azc}\t{azb}\t{status}\t\n")
            out.flush()
            written += 1

    def checked(it, label):
        prev = None
        for key, size in it:
            if prev is not None and key <= prev:
                raise SortError(f"{label} listing not strictly sorted at "
                                f"{key!r}")
            prev = key
            yield key, size

    man = checked(manifest_rows, "manifest")
    az = checked(azure_rows, "azure")
    m = next(man, None)
    a = next(az, None)
    while m is not None or a is not None:
        compared += 1
        if compared % PROGRESS_EVERY == 0:
            out.write(f"#progress\t{compared}\t{int(now())}\n")
            out.flush()
        if a is None or (m is not None and m[0] < a[0]):
            stats["s3_count"] += 1
            stats["s3_bytes"] += m[1]
            emit(m[0], 1, m[1], 0, "", "MISSING-DEST")
            if missing_out is not None:
                missing_out.write(f"{m[0]}\t{m[1]}\n")
            m = next(man, None)
        elif m is None or a[0] < m[0]:
            stats["az_count"] += 1
            stats["az_bytes"] += a[1]
            emit(a[0], 0, "", 1, a[1], "EXTRA-DEST")
            a = next(az, None)
        else:
            stats["s3_count"] += 1
            stats["s3_bytes"] += m[1]
            stats["az_count"] += 1
            stats["az_bytes"] += a[1]
            if m[1] == a[1]:
                stats["ok"] += 1
            else:
                emit(m[0], 1, m[1], 1, a[1], "SIZE-DIFF")
                # NOT added to missing_out: the create-only engine cannot
                # overwrite a bad blob — SIZE-DIFF is a deliberate
                # remediation decision, never a silent re-copy. It gets
                # its OWN uncapped file so the full population is
                # quantifiable (the verify.tsv sample is capped at 1000
                # and is lexically biased toward the start of the
                # keyspace — useless for characterising the class).
                if sizediff_out is not None:
                    sizediff_out.write(f"{m[0]}\t{m[1]}\t{a[1]}\n")
            m = next(man, None)
            a = next(az, None)
    return stats


class SortError(Exception):
    pass


# ── S3 range-sharded listing (VM only; boto3 imported lazily) ───────────────

VERSION_PREFIX = "_noncurrent"   # reserved: no live key starts with "_"


def version_blob_name(key: str, version_id: str) -> str:
    """Dest-relative name for a NONCURRENT version.

    "<_noncurrent>/<key>/<versionId>" keeps every revision grouped under its
    own key, preserves the client's one-folder-per-bucket layout, and cannot
    collide with a live key (verified: no key in the manifest starts with
    "_"). Version ids are opaque and NOT time-ordered -- ordering lives in
    the versions manifest, never in the name."""
    return f"{VERSION_PREFIX}/{key}/{version_id}"


def select_distinct_versions(rows):
    """Pure: version records for ONE key -> the noncurrent ones worth
    copying.

    rows: iterable of (version_id, size, etag, is_latest). A noncurrent
    version is skipped when its ETag matches the current version's (a
    byte-identical re-upload -- 85.6% of this bucket) or an already-selected
    older revision's (duplicates among the history itself). ETag equality is
    a sound identity test here: these are single-part objects (~196 KB avg),
    so the ETag is the content MD5.

    Returns (selected, skipped_dup) as lists of (version_id, size, etag)."""
    current_etag = next((e for _, _, e, latest in rows if latest), None)
    seen = {current_etag} if current_etag else set()
    selected, skipped = [], []
    for vid, size, etag, latest in rows:
        if latest:
            continue
        if etag in seen:
            skipped.append((vid, size, etag))
        else:
            seen.add(etag)
            selected.append((vid, size, etag))
    return selected, skipped


def _list_shard(bucket, region, idx, start_after, end, out_dir):
    import boto3.session  # apt python3-boto3; VM-only dependency
    from botocore.config import Config
    # a PRIVATE Session per shard: boto3's module-level client() shares one
    # default Session, whose create_client races across threads
    # (KeyError: 'credential_provider' — hit live, checkmate 2026-08-22)
    cli = boto3.session.Session().client(
        "s3", region_name=region or None,
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}))
    final = os.path.join(out_dir, f"shard-{idx:03d}.tsv")
    if os.path.exists(final):
        return  # resume after a crash: this shard already completed
    unsafe = 0
    with open(final + ".part", "w") as f:
        kwargs = {"Bucket": bucket}
        if start_after:
            kwargs["StartAfter"] = start_after
        done = False
        for page in cli.get_paginator("list_objects_v2").paginate(**kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if end is not None and key > end:
                    done = True
                    break
                if "\t" in key or "\n" in key or "\r" in key:
                    # would corrupt the TSV manifest; recorded, not copied
                    with open(os.path.join(out_dir,
                                           f"unsafe-{idx:03d}.txt"),
                              "a") as uf:
                        uf.write(repr(key) + "\n")
                    unsafe += 1
                    continue
                f.write(f"{key}\t{obj['Size']}\n")
            if done:
                break
    os.replace(final + ".part", final)
    if unsafe:
        log(f"shard {idx:03d}: {unsafe} keys with tab/newline recorded to "
            f"unsafe-{idx:03d}.txt (NOT copied)")


def _list_shard_group(bucket, region, jobs, out_dir, threads):
    """Worker-process entry: drain a group of shards with a small thread
    pool. Processes, not just threads, because boto3's per-page XML parse
    is CPU-real and the GIL caps one process at ~13k keys/s no matter how
    many threads it runs (measured live, checkmate 2026-08-22)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(_list_shard, bucket, region, i, a, b, out_dir)
                for i, a, b in jobs]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()


def cmd_list() -> int:
    bucket = os.environ["S3_BUCKET"]
    region = os.environ.get("AWS_REGION", "")
    done_path = os.path.join(BASE, "listing.done")
    if os.path.exists(done_path):
        print(open(done_path).read().strip())
        return 0
    shards = int(os.environ.get("FLAT_SHARDS", DEFAULT_SHARDS))
    workers = int(os.environ.get("FLAT_LIST_WORKERS", DEFAULT_LIST_WORKERS))
    procs = int(os.environ.get("FLAT_LIST_PROCS", os.cpu_count() or 8))
    out_dir = os.path.join(BASE, "shards")
    os.makedirs(out_dir, exist_ok=True)
    ranges = range_bounds(shards)
    threads = max(1, workers // max(1, procs))
    groups: list[list] = [[] for _ in range(procs)]
    for i, (a, b) in enumerate(ranges):
        groups[i % procs].append((i, a, b))
    groups = [g for g in groups if g]
    log(f"list start: {len(ranges)} range shards, {len(groups)} procs x "
        f"{threads} threads")
    with concurrent.futures.ProcessPoolExecutor(max_workers=procs) as ex:
        futs = [ex.submit(_list_shard_group, bucket, region, g, out_dir,
                          threads) for g in groups]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()  # first shard failure aborts the run loudly
    keys = 0
    unsafe = 0
    tmp = os.path.join(BASE, "listing.txt.tmp")
    prev = None
    with open(tmp, "w") as out:
        for i in range(len(ranges)):
            with open(os.path.join(out_dir, f"shard-{i:03d}.tsv")) as f:
                for ln in f:
                    key = ln.split("\t", 1)[0]
                    if prev is not None and key <= prev:
                        raise SortError(
                            f"shard seam broke sort order at {key!r} "
                            f"(shard {i:03d})")
                    prev = key
                    out.write(ln)
                    keys += 1
    for name in os.listdir(out_dir):
        if name.startswith("unsafe-"):
            with open(os.path.join(out_dir, name)) as f:
                unsafe += sum(1 for _ in f)
    os.replace(tmp, os.path.join(BASE, "listing.txt"))
    sentinel = {"keys": keys, "unsafe": unsafe}
    with open(done_path, "w") as f:
        json.dump(sentinel, f)
    shutil.rmtree(out_dir, ignore_errors=True)
    log(f"list done: {keys} keys, {unsafe} unsafe")
    print(json.dumps(sentinel))
    return 0


# ── split ───────────────────────────────────────────────────────────────────

def _version_shard(bucket, region, idx, start_after, end, out_dir):
    """One KeyMarker-range shard of list_object_versions. Writes
    "key\tversionId\tsize\tetag\tisLatest\tlastModified" rows, grouped
    by key in listing order. KeyMarker is exclusive like StartAfter, so the
    same range partition used for the current-object listing applies."""
    import boto3.session
    from botocore.config import Config
    final = os.path.join(out_dir, f"vshard-{idx:03d}.tsv")
    if os.path.exists(final):
        return
    cli = boto3.session.Session().client(
        "s3", region_name=region or None,
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}))
    dm = 0
    with open(final + ".part", "w") as f:
        kw = {"Bucket": bucket, "MaxKeys": 1000}
        if start_after:
            kw["KeyMarker"] = start_after
        done = False
        while not done:
            r = cli.list_object_versions(**kw)
            for o in r.get("Versions", []):
                key = o["Key"]
                if end is not None and key > end:
                    done = True
                    break
                if "\t" in key or "\n" in key or "\r" in key:
                    continue
                etag = (o.get("ETag") or "").strip('"')
                f.write(f"{key}\t{o['VersionId']}\t{o['Size']}\t{etag}\t"
                        f"{int(bool(o['IsLatest']))}\t"
                        f"{o['LastModified'].strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
            for o in r.get("DeleteMarkers", []):
                if end is None or o["Key"] <= end:
                    dm += 1
            if done or not r.get("IsTruncated"):
                break
            kw["KeyMarker"] = r["NextKeyMarker"]
            kw["VersionIdMarker"] = r["NextVersionIdMarker"]
    os.replace(final + ".part", final)
    if dm:
        with open(os.path.join(out_dir, f"deletemarkers-{idx:03d}.count"),
                  "w") as f:
            f.write(str(dm))


def _version_shard_group(bucket, region, jobs, out_dir, threads):
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(_version_shard, bucket, region, i, a, b, out_dir)
                for i, a, b in jobs]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()


def cmd_list_versions() -> int:
    """Full version history -> versions.txt (all records) + versions.done."""
    bucket = os.environ["S3_BUCKET"]
    region = os.environ.get("AWS_REGION", "")
    done_path = os.path.join(BASE, "versions.done")
    if os.path.exists(done_path):
        print(open(done_path).read().strip())
        return 0
    shards = int(os.environ.get("FLAT_SHARDS", DEFAULT_SHARDS))
    workers = int(os.environ.get("FLAT_LIST_WORKERS", DEFAULT_LIST_WORKERS))
    procs = int(os.environ.get("FLAT_LIST_PROCS", os.cpu_count() or 8))
    out_dir = os.path.join(BASE, "vshards")
    os.makedirs(out_dir, exist_ok=True)
    ranges = range_bounds(shards)
    threads = max(1, workers // max(1, procs))
    groups: list[list] = [[] for _ in range(procs)]
    for i, (a, b) in enumerate(ranges):
        groups[i % procs].append((i, a, b))
    groups = [g for g in groups if g]
    log(f"list-versions start: {len(ranges)} shards, {len(groups)} procs x "
        f"{threads} threads")
    with concurrent.futures.ProcessPoolExecutor(max_workers=procs) as ex:
        futs = [ex.submit(_version_shard_group, bucket, region, g, out_dir,
                          threads) for g in groups]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()
    rows = 0
    tmp = os.path.join(BASE, "versions.txt.tmp")
    with open(tmp, "w") as out:
        for i in range(len(ranges)):
            with open(os.path.join(out_dir, f"vshard-{i:03d}.tsv")) as f:
                for ln in f:
                    out.write(ln)
                    rows += 1
    dms = 0
    for name in os.listdir(out_dir):
        if name.startswith("deletemarkers-"):
            dms += int(open(os.path.join(out_dir, name)).read().strip() or 0)
    os.replace(tmp, os.path.join(BASE, "versions.txt"))
    sentinel = {"version_records": rows, "delete_markers": dms}
    with open(done_path, "w") as f:
        json.dump(sentinel, f)
    shutil.rmtree(out_dir, ignore_errors=True)
    log(f"list-versions done: {sentinel}")
    print(json.dumps(sentinel))
    return 0


def iter_key_groups(path):
    """versions.txt (grouped by key) -> (key, [(vid, size, etag, latest)])."""
    cur_key, rows = None, []
    with open(path) as f:
        for ln in f:
            parts = ln.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            key, vid, size, etag, latest = parts[0], parts[1], parts[2], parts[3], parts[4]
            if key != cur_key:
                if cur_key is not None:
                    yield cur_key, rows
                cur_key, rows = key, []
            rows.append((vid, int(size), etag, latest == "1"))
    if cur_key is not None:
        yield cur_key, rows


def cmd_split_versions(per_chunk: int, distinct_only: bool = True) -> int:
    """versions.txt -> vchunk-NNNNN manifests of NONCURRENT versions worth
    copying, and vjobs.txt/queue additions. Rows: key\tvid\tsize."""
    src = os.path.join(BASE, "versions.txt")
    if not os.path.exists(src):
        print(json.dumps({"ok": False, "cause": "no-versions-listing"}))
        return 1
    tmp_dir = os.path.join(BASE, "vchunks.tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)
    jobs, buf, ci = [], [], 0
    sel_n = sel_b = dup_n = dup_b = 0

    def flush():
        nonlocal ci, buf
        if not buf:
            return
        name = f"vchunk-{ci:05d}"
        with open(os.path.join(tmp_dir, name), "w") as f:
            f.writelines(buf)
        jobs.append(f"V\t{name}\n")
        ci += 1
        buf = []

    for key, rows in iter_key_groups(src):
        selected, skipped = select_distinct_versions(rows)
        dup_n += len(skipped)
        dup_b += sum(s for _, s, _ in skipped)
        take = selected if distinct_only else selected + skipped
        for vid, size, _etag in take:
            sel_n += 1
            sel_b += size
            buf.append(f"{key}\t{vid}\t{size}\n")
            if len(buf) >= per_chunk:
                flush()
    flush()
    final_dir = os.path.join(BASE, "vchunks")
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(tmp_dir, final_dir)
    with open(os.path.join(BASE, "vjobs.txt"), "w") as f:
        f.writelines(jobs)
    with open(os.path.join(BASE, "queue.txt"), "a") as f:
        f.writelines(jobs)
    with open(os.path.join(BASE, "jobs.txt"), "a") as f:
        f.writelines(jobs)
    out = {"ok": True, "version_jobs": len(jobs), "to_copy": sel_n,
           "to_copy_bytes": sel_b, "skipped_duplicates": dup_n,
           "skipped_duplicate_bytes": dup_b, "distinct_only": distinct_only}
    log(f"split-versions: {out}")
    print(json.dumps(out))
    return 0


def cmd_copy_versions(name: str, concurrency: int) -> int:
    """Copy one vchunk: presigned GET per (key, versionId) -> Put Blob From
    URL at _noncurrent/<key>/<versionId>. Same transport, counters and
    summary grammar as copy-chunk."""
    import boto3.session
    from botocore.config import Config
    path = os.path.join(BASE, "vchunks", name)
    if not os.path.exists(path):
        print(f"no such version chunk: {path}", file=sys.stderr)
        return 1
    bucket = os.environ["S3_BUCKET"]
    dest_url = os.environ["AZURE_DEST_URL"]
    sas = os.environ["AZURE_DEST_SAS"]
    cli = boto3.session.Session().client(
        "s3", region_name=os.environ.get("AWS_REGION") or None,
        config=Config(signature_version="s3v4"))
    counters = _CopyCounters()

    def presign_v(key, vid):
        return cli.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key, "VersionId": vid},
            ExpiresIn=PRESIGN_TTL)

    def one(line):
        key, vid, size = line.rstrip("\n").split("\t")
        _copy_one(lambda _k: presign_v(key, vid), dest_url, sas,
                  version_blob_name(key, vid), int(size), counters)

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency) as ex:
        with open(path) as f:
            futs = [ex.submit(one, ln) for ln in f if ln.strip()]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()
    status = ("Failed" if counters.failed else
              "CompletedWithSkipped" if counters.skipped else "Completed")
    print(f"Transfers Completed: {counters.completed}")
    print(f"Transfers Failed: {counters.failed}")
    print(f"Transfers Skipped: {counters.skipped}")
    print(f"Bytes Transferred: {counters.bytes}")
    print(f"Final Job Status: {status}")
    for err, n in sorted(counters.errors.items()):
        print(f"error {err}: {n}")
    return 1 if counters.failed else 0


def cmd_split(per_chunk: int) -> int:
    listing = os.path.join(BASE, "listing.txt")
    if not os.path.exists(listing):
        print(json.dumps({"ok": False, "cause": "no-listing"}))
        return 1
    tmp_dir = os.path.join(BASE, "chunks.tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)
    jobs: list[str] = []
    keys = 0
    quarantined = 0
    with open(listing) as f:
        for name, text in split_listing(f, per_chunk):
            with open(os.path.join(tmp_dir, name), "w") as cf:
                cf.write(text)
            n = text.count("\n")
            keys += n
            if name.startswith("quarantine-"):
                quarantined += n
                jobs.append(f"Q\t{name}\n")
            else:
                jobs.append(f"L\t{name}\n")
    final_dir = os.path.join(BASE, "chunks")
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(tmp_dir, final_dir)
    with open(os.path.join(BASE, "jobs.txt.tmp"), "w") as f:
        f.writelines(jobs)
    os.replace(os.path.join(BASE, "jobs.txt.tmp"),
               os.path.join(BASE, "jobs.txt"))  # commit point
    shutil.copy(os.path.join(BASE, "jobs.txt"),
                os.path.join(BASE, "queue.txt"))
    for name in ("done.txt", "failed.txt", "verify.tsv"):
        try:
            os.remove(os.path.join(BASE, name))
        except FileNotFoundError:
            pass
    out = {"ok": True, "jobs": len(jobs), "keys": keys,
           "chunks": sum(1 for j in jobs if j.startswith("L")),
           "quarantine_jobs": sum(1 for j in jobs if j.startswith("Q")),
           "quarantined_keys": quarantined, "per_chunk": per_chunk}
    log(f"split done: {out['jobs']} jobs ({out['quarantined_keys']} "
        f"quarantined keys)")
    print(json.dumps(out))
    return 0


# ── copy-chunk: presigned-GET → Put Blob From URL server-side copy ──────────
# azcopy's --list-of-files enumerates entries SEQUENTIALLY (~15 obj/s
# measured live — weeks at 242M objects), so flat chunks are copied by the
# same transport the vimeo/zoom ingests use: Azure fetches each object
# from a presigned S3 GET; bytes never touch this VM; sizes come from the
# manifest so there are ZERO per-object HEADs; If-None-Match: * makes the
# copy API-enforced create-only (stronger than azcopy's client-side
# --overwrite=false). Request budget: 1 Azure PUT per object.

X_MS_VERSION = "2021-08-06"  # >= 2020-04-08: Put Blob/Block From URL
SINGLE_SHOT_MAX = 256 * 1024 * 1024   # Put Blob From URL size ceiling
BLOCK_SIZE = 128 * 1024 * 1024
PRESIGN_TTL = 6 * 3600


class _CopyCounters:
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.completed = self.skipped = self.failed = self.bytes = 0
        self.errors: dict[str, int] = {}

    def add(self, outcome, size=0, err=None):
        with self.lock:
            setattr(self, outcome, getattr(self, outcome) + 1)
            if outcome == "completed":
                self.bytes += size
            if err:
                self.errors[err] = self.errors.get(err, 0) + 1


def _dest_headers():
    return {"x-ms-version": X_MS_VERSION, "If-None-Match": "*"}


_TLS = None  # thread-local holder, created lazily (keeps import light)


def _conn(host):
    """Persistent per-thread HTTPS connection. urllib.request opens a new
    TLS session per request — at 550 req/s that's 550 handshakes/s and ~4
    CPU cores per worker (measured live); one keep-alive connection per
    thread makes the copy loop latency-bound instead of CPU-bound."""
    global _TLS
    if _TLS is None:
        import threading
        _TLS = threading.local()
    c = getattr(_TLS, "conn", None)
    if c is None or getattr(_TLS, "host", None) != host:
        if c is not None:
            c.close()
        import http.client
        c = http.client.HTTPSConnection(host, timeout=600)
        _TLS.conn, _TLS.host = c, host
    return c


def _drop_conn():
    if _TLS is not None and getattr(_TLS, "conn", None) is not None:
        _TLS.conn.close()
        _TLS.conn = None


def _azure_put(url, headers, body=b"", tries=5):
    """PUT over the thread's keep-alive connection, with dest-side retry.
    Returns 'created' | 'exists' | raises CopyError."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path + ("?" + parts.query if parts.query else "")
    last = "retries-exhausted"
    for attempt in range(tries):
        try:
            c = _conn(parts.netloc)
            c.request("PUT", path, body=body, headers=headers)
            r = c.getresponse()
            r.read()  # drain so the connection can be reused
            if r.status in (200, 201):
                return "created"
            code = r.getheader("x-ms-error-code") or str(r.status)
            if r.status == 409 and "BlobAlreadyExists" in code:
                return "exists"
            if r.status in (429, 500, 503) and attempt < tries - 1:
                time.sleep(min(2 ** attempt,
                               int(r.getheader("Retry-After") or 0)
                               or 2 ** attempt))
                last = code
                continue
            raise CopyError(code)
        except CopyError:
            raise
        except (OSError, Exception) as exc:  # noqa: BLE001 — includes
            # http.client exceptions; a dropped keep-alive is normal churn
            _drop_conn()
            last = type(exc).__name__
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise CopyError(last) from exc
    raise CopyError(last)


class CopyError(Exception):
    pass


def _copy_one(presign, dest_url, sas, key, size, counters):
    try:
        src = presign(key)
        blob = f"{dest_url}/{urllib.parse.quote(key)}"
        if size <= SINGLE_SHOT_MAX:
            h = _dest_headers()
            h.update({"x-ms-blob-type": "BlockBlob",
                      "x-ms-copy-source": src})
            out = _azure_put(f"{blob}?{sas}", h)
        else:
            # block-staged server-side copy for the rare >256 MiB object
            import base64
            ids = []
            for i, off in enumerate(range(0, size, BLOCK_SIZE)):
                bid = base64.b64encode(f"pbfu{i:08d}".encode()).decode()
                end = min(off + BLOCK_SIZE, size) - 1
                h = {"x-ms-version": X_MS_VERSION,
                     "x-ms-copy-source": src,
                     "x-ms-source-range": f"bytes={off}-{end}"}
                _azure_put(f"{blob}?comp=block&blockid="
                           f"{urllib.parse.quote(bid)}&{sas}", h)
                ids.append(bid)
            body = ("<?xml version='1.0' encoding='utf-8'?><BlockList>"
                    + "".join(f"<Uncommitted>{b}</Uncommitted>" for b in ids)
                    + "</BlockList>").encode()
            out = _azure_put(f"{blob}?comp=blocklist&{sas}",
                             _dest_headers(), body=body)
        counters.add("completed" if out == "created" else "skipped", size)
    except CopyError as exc:
        counters.add("failed", err=str(exc))
    except Exception as exc:  # noqa: BLE001 — one key must not kill the job
        counters.add("failed", err=type(exc).__name__)


def cmd_copy_chunk(name: str, concurrency: int) -> int:
    import boto3.session
    from botocore.config import Config
    path = os.path.join(BASE, "chunks", name)
    if not os.path.exists(path):
        print(f"no such chunk manifest: {path}", file=sys.stderr)
        return 1
    bucket = os.environ["S3_BUCKET"]
    dest_url = os.environ["AZURE_DEST_URL"]
    sas = os.environ["AZURE_DEST_SAS"]
    cli = boto3.session.Session().client(
        "s3", region_name=os.environ.get("AWS_REGION") or None,
        config=Config(signature_version="s3v4"))

    def presign(key):  # pure local signing; no network
        return cli.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_TTL)

    counters = _CopyCounters()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency) as ex:
        with open(path) as f:
            futs = []
            for ln in f:
                ln = ln.rstrip("\n")
                if not ln:
                    continue
                key, _, size = ln.partition("\t")
                futs.append(ex.submit(_copy_one, presign, dest_url, sas,
                                      key, int(size or 0), counters))
        for fut in concurrent.futures.as_completed(futs):
            fut.result()
    status = ("Failed" if counters.failed else
              "CompletedWithSkipped" if counters.skipped else "Completed")
    # azcopy summary grammar on purpose: the runner's grab() parsing and
    # the whole done.txt/status/ETA pipeline consume it unchanged
    print(f"Transfers Completed: {counters.completed}")
    print(f"Transfers Failed: {counters.failed}")
    print(f"Transfers Skipped: {counters.skipped}")
    print(f"Bytes Transferred: {counters.bytes}")
    print(f"Final Job Status: {status}")
    for err, n in sorted(counters.errors.items()):
        print(f"error {err}: {n}")
    return 1 if counters.failed else 0


# ── verify (manifest vs Azure dest listing) ─────────────────────────────────

def _http_get(url: str, tries: int = 5) -> bytes:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError):
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def iter_manifest(path):
    with open(path) as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln:
                continue
            key, _, size = ln.partition("\t")
            yield key, int(size or 0)


def iter_azure_listing():
    """Complete sequential List Blobs walk of the dest prefix, yielding
    (bucket-relative key, size) in the service's lexical order."""
    dest_url = os.environ["AZURE_DEST_URL"]
    sas = os.environ["AZURE_DEST_SAS"]
    prefix = os.environ["AZURE_DEST_PREFIX"]
    if not dest_url.endswith("/" + prefix):
        raise RuntimeError("AZURE_DEST_URL does not end with the dest "
                           "prefix -- dest.env inconsistent")
    container_url = dest_url[:-(len(prefix) + 1)]
    marker = ""
    strip = len(prefix) + 1
    while True:
        q = ("restype=container&comp=list&maxresults=5000&prefix="
             + urllib.parse.quote(prefix + "/"))
        if marker:
            q += "&marker=" + urllib.parse.quote(marker)
        data = _http_get(f"{container_url}?{q}&{sas}")
        root = ET.fromstring(data)
        for blob in root.iter("Blob"):
            name = blob.findtext("Name")
            size = int(blob.find("Properties").findtext("Content-Length"))
            yield name[strip:], size
        marker = root.findtext("NextMarker") or ""
        if not marker:
            break


def cmd_verify() -> int:
    out_path = os.path.join(BASE, "verify.tsv")
    miss_path = os.path.join(BASE, "missing.txt")
    diff_path = os.path.join(BASE, "sizediff.txt")
    with open(out_path, "w") as out, \
            open(miss_path + ".tmp", "w") as miss, \
            open(diff_path + ".tmp", "w") as diff:
        try:
            stats = merge_join(iter_manifest(os.path.join(BASE,
                                                          "listing.txt")),
                               iter_azure_listing(), out, missing_out=miss,
                               sizediff_out=diff)
        except SortError as exc:
            out.write(f"#error\tunsorted\t{exc}\n")
            log(f"verify ABORTED: {exc}")
            return 1
        kv = "\t".join(f"{k}={v}" for k, v in stats.items())
        out.write(f"#done\t{int(time.time())}\t{kv}\n")
    os.replace(miss_path + ".tmp", miss_path)
    os.replace(diff_path + ".tmp", diff_path)
    log(f"verify done: {stats}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: s3_flat.py list | split [--per-chunk N] | verify "
              "[deep] | list-versions | split-versions [--per-chunk N] "
              "[--all] | copy-versions <vchunk> [conc]", file=sys.stderr)
        return 64
    os.makedirs(BASE, exist_ok=True)
    if argv[0] == "list":
        return cmd_list()
    if argv[0] == "split":
        per_chunk = 250_000
        if "--per-chunk" in argv:
            per_chunk = int(argv[argv.index("--per-chunk") + 1])
        return cmd_split(per_chunk)
    if argv[0] == "copy-chunk":
        conc = int(argv[2]) if len(argv) > 2 else 200
        return cmd_copy_chunk(argv[1], conc)
    if argv[0] == "list-versions":
        return cmd_list_versions()
    if argv[0] == "split-versions":
        per_chunk = 250_000
        if "--per-chunk" in argv:
            per_chunk = int(argv[argv.index("--per-chunk") + 1])
        return cmd_split_versions(per_chunk,
                                  distinct_only="--all" not in argv)
    if argv[0] == "copy-versions":
        conc = int(argv[2]) if len(argv) > 2 else 200
        return cmd_copy_versions(argv[1], conc)
    if argv[0] == "verify":
        # a trailing "deep" arg is accepted and ignored: per-object
        # name+size against the manifest IS the deep check here
        return cmd_verify()
    print(f"unknown verb {argv[0]!r}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
