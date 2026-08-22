#!/usr/bin/env bash
# VM-side job runner for the s3-azure-transfer skill. Pushed over ssh by
# scripts/s3_transfer.py at transfer/verify time (never run on the laptop).
#
#   azcopy-runner.sh worker <n> [concurrency] [max_jobs]   # drain the queue
#   azcopy-runner.sh verify [deep]                         # count/byte rollup
#
# Queue lines are "<type>\t<arg>": R = recursive azcopy server-side copy
# job on a folder prefix (Put Blob/Block From URL — bytes never transit
# this VM), L = azcopy server-side copy of one flat-bucket chunk manifest
# (--list-of-files chunks/<arg>; same S2S transport as R), S = shallow
# rclone copy of the LOOSE files directly at that level (the objects a
# prefix split would otherwise orphan; these few DO stream through the VM),
# Q = rclone --files-from copy of a quarantine manifest (keys whose names
# azcopy list files can't be trusted with; expected ~empty).
# ~/xfer-jobs/jobs.txt is the immutable master list (verify iterates it);
# queue.txt is the consumable copy. --overwrite=false is pinned: the copy
# only ever creates blobs that don't already exist, so re-running is a safe
# resume (skips are normal, not errors).
set -uo pipefail

BASE="$HOME/xfer-jobs"
ENVD="$HOME/.config/xfer"
QUEUE="$BASE/queue.txt"
LOCK="$BASE/queue.lock"

set -a
. "$ENVD/aws.env"
. "$ENVD/dest.env"
set +a
export AZCOPY_JOB_PLAN_LOCATION="$BASE/plans"
export AZCOPY_LOG_LOCATION="$BASE/logs"
mkdir -p "$BASE/plans" "$BASE/logs" "$BASE/inflight"

pop_job() {  # atomically take the first queue line (empty output = drained)
    (
        flock 9
        [ -s "$QUEUE" ] || exit 0
        head -n 1 "$QUEUE"
        tail -n +2 "$QUEUE" > "$QUEUE.tmp" && mv "$QUEUE.tmp" "$QUEUE"
    ) 9>>"$LOCK"
}

grab() {  # last integer on the first matching summary line
    printf '%s\n' "$2" | grep -m1 -E "$1" | grep -oE '[0-9]+' | tail -1
}

worker() {
    local n="$1" conc="${2:-200}" max="${3:-0}" count=0
    export AZCOPY_CONCURRENCY_VALUE="$conc"
    local hb="$BASE/w$n.log"
    echo "$(date -u +%FT%TZ) w$n start concurrency=$conc max=$max" >> "$hb"
    while :; do
        local job jtype jprefix
        job="$(pop_job)"
        [ -z "$job" ] && break
        IFS=$'\t' read -r jtype jprefix <<<"$job"
        printf '%s\n' "$job" > "$BASE/inflight/w$n"
        local start rc out jobid comp fail skip bytes status
        start=$(date +%s)
        if [ "$jtype" = "R" ] || [ "$jtype" = "L" ]; then
            if [ "$jtype" = "R" ]; then
                out="$(azcopy copy "$S3_SRC_URL/$jprefix/" \
                    "$AZURE_DEST_URL/$jprefix?$AZURE_DEST_SAS" \
                    --recursive --overwrite=false --log-level ERROR 2>&1)"
                rc=$?
            else
                # L: flat-bucket chunk — server-side Put Blob From URL
                # driven by s3_flat.py (azcopy's list-of-files enumerates
                # sequentially, ~15 obj/s measured live; this path presigns
                # locally, needs zero per-object HEADs, and its
                # If-None-Match: * makes create-only API-enforced). It
                # prints azcopy's summary grammar so parsing below is
                # engine-agnostic.
                out="$(python3 "$BASE/s3_flat.py" copy-chunk "$jprefix" \
                    "$conc" 2>&1)"
                rc=$?
            fi
            jobid="$(printf '%s\n' "$out" \
                | sed -n 's/^Job \([0-9a-f-]*\) has started.*/\1/p' | head -1)"
            comp="$(grab 'Transfers Completed' "$out")"
            fail="$(grab 'Transfers Failed' "$out")"
            skip="$(grab 'Transfers Skipped' "$out")"
            bytes="$(grab '[Bb]ytes ?[Tt]ransferred' "$out")"
            status="$(printf '%s\n' "$out" \
                | sed -n 's/.*Final Job Status: *//p' | head -1)"
            # success = nothing failed and the job ran to a Completed* end
            # (CompletedWithSkipped is the normal resume outcome)
            if [ "${fail:-0}" = "0" ] && { [ "$rc" -eq 0 ] \
                    || [[ "${status:-}" == Completed* ]]; }; then
                rc=0
            else
                [ "$rc" -eq 0 ] && rc=1
            fi
            # reclaim plan-file disk for finished jobs
            [ "$rc" -eq 0 ] && [ -n "$jobid" ] \
                && azcopy jobs rm "$jobid" >/dev/null 2>&1
        elif [ "$jtype" = "Q" ]; then
            # Q job: quarantine manifest — keys whose names azcopy list
            # files can't be trusted with stream via rclone --files-from
            rclone copy "s3:$S3_BUCKET" \
                "azure:$AZURE_DEST_CONTAINER/$AZURE_DEST_PREFIX" \
                --files-from "$BASE/chunks/$jprefix" \
                --transfers 16 --checkers 16 --retries 5 \
                --log-file "$BASE/logs/shallow-w$n.log" --log-level ERROR
            rc=$?
            jobid="rclone"; comp=""; fail=""; skip=""; bytes=""
        else
            # S job: loose files at exactly this level; empty prefix = root
            local src="s3:$S3_BUCKET${jprefix:+/$jprefix}"
            local dst="azure:$AZURE_DEST_CONTAINER/$AZURE_DEST_PREFIX${jprefix:+/$jprefix}"
            rclone copy "$src" "$dst" --max-depth 1 \
                --transfers 16 --checkers 16 --retries 5 \
                --log-file "$BASE/logs/shallow-w$n.log" --log-level ERROR
            rc=$?
            jobid="rclone"; comp=""; fail=""; skip=""; bytes=""
        fi
        # a job that failed without a parseable summary died before azcopy
        # even started (bad invocation, unbound var, ...) — keep its output
        # or the only evidence dies with this variable (learned live)
        if [ "$rc" -ne 0 ] && [ -z "${comp:-}" ] && [ "$jtype" != "S" ] \
                && [ "$jtype" != "Q" ]; then
            printf '%s\n' "$out" | sed 's/sig=[^&"]*/sig=REDACTED/g' \
                > "$BASE/logs/job-${jprefix//\//_}.err"
        fi
        local end row dest
        end=$(date +%s)
        row="$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' \
            "$end" "$jtype" "$jprefix" "${jobid:-?}" "${comp:-?}" \
            "${fail:-?}" "${skip:-?}" "${bytes:-?}" "$((end - start))")"
        if [ "$rc" -eq 0 ]; then dest="$BASE/done.txt"; else dest="$BASE/failed.txt"; fi
        printf '%s\n' "$row" >> "$dest"
        echo "$(date -u +%FT%TZ) w$n $jtype '$jprefix' rc=$rc" \
            "comp=${comp:-?} fail=${fail:-?} skip=${skip:-?}" \
            "bytes=${bytes:-?} $((end - start))s" >> "$hb"
        rm -f "$BASE/inflight/w$n"
        count=$((count + 1))
        [ "$max" -gt 0 ] && [ "$count" -ge "$max" ] && break
    done
    echo "$(date -u +%FT%TZ) w$n done ($count jobs)" >> "$hb"
}

list_bucket() {  # flat-bucket full listing -> listing.txt + listing.done
    echo "$(date -u +%FT%TZ) list-bucket start (${1:-sharded})" \
        >> "$BASE/plan.log"
    if [ "${1:-}" = "sequential" ]; then
        # escape hatch: single rclone continuation-token walk (slow at
        # 300M keys — hours; the sharded boto3 path is the default)
        rclone lsf --files-only -R --format "ps" \
            --separator "$(printf '\t')" "s3:$S3_BUCKET" \
            > "$BASE/listing.txt.tmp" \
            && mv "$BASE/listing.txt.tmp" "$BASE/listing.txt" \
            && python3 -c "import json,sys;print(json.dumps({'keys': sum(1 for _ in open(sys.argv[1])), 'unsafe': 0}))" \
                "$BASE/listing.txt" > "$BASE/listing.done"
    else
        # stdout/stderr to a file, not the tmux pane — a pane dies with its
        # window and takes the crash traceback with it (learned live)
        python3 "$BASE/s3_flat.py" list >> "$BASE/plan-run.log" 2>&1
    fi
    local rc=$?
    echo "$(date -u +%FT%TZ) list-bucket exit rc=$rc" >> "$BASE/plan.log"
    return $rc
}

verify() {  # per-job count+byte rollup, S3 vs Azure; deep adds rclone check
    # flat engagements (L jobs) verify manifest-vs-Azure instead: per-object
    # name+size at two-listing cost, and "deep" is implied
    if grep -qm1 $'^L\t' "$BASE/jobs.txt" 2>/dev/null; then
        python3 "$BASE/s3_flat.py" verify "${1:-}"
        return $?
    fi
    per_job_verify "${1:-}"
}

per_job_verify() {  # the foldered-bucket rollup (R/S jobs)
    python3 - "${1:-}" <<'PYEOF'
import json, os, subprocess, sys, time

deep = sys.argv[1] == "deep"
base = os.path.expanduser("~/xfer-jobs")
env = os.environ
bucket = env["S3_BUCKET"]
dest = f"{env['AZURE_DEST_CONTAINER']}/{env['AZURE_DEST_PREFIX']}"

def size(remote, shallow):
    cmd = ["rclone", "size", "--json", remote]
    if shallow:
        cmd += ["--max-depth", "1"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return None
    d = json.loads(p.stdout)
    return d.get("count", -1), d.get("bytes", -1)

ok = bad = 0
out = open(os.path.join(base, "verify.tsv"), "w")
for ln in open(os.path.join(base, "jobs.txt")):
    ln = ln.rstrip("\n")
    if not ln:
        continue
    jtype, _, prefix = ln.partition("\t")
    shallow = jtype == "S"
    sub = f"/{prefix}" if prefix else ""
    s3 = size(f"s3:{bucket}{sub}", shallow)
    az = size(f"azure:{dest}{sub}", shallow)
    if s3 is None or az is None:
        status, row = "LIST-ERROR", ("?", "?", "?", "?")
    else:
        row = (s3[0], s3[1], az[0], az[1])
        status = "ok" if (s3[0] == az[0] and s3[1] == az[1]) else "MISMATCH"
    diffs = ""
    if deep and status == "ok" and not shallow:
        p = subprocess.run(["rclone", "check", f"s3:{bucket}{sub}",
                           f"azure:{dest}{sub}", "--one-way", "--size-only"],
                          capture_output=True, text=True)
        if p.returncode != 0:
            status, diffs = "DEEP-DIFF", "rc=%d" % p.returncode
    if status == "ok":
        ok += 1
    else:
        bad += 1
    out.write("\t".join(str(x) for x in
                        (jtype, prefix, *row, status, diffs)) + "\n")
    out.flush()
out.write(f"#done\t{int(time.time())}\tok={ok}\tbad={bad}\n")
out.close()
PYEOF
}

case "${1:-}" in
    worker) shift; worker "$@" ;;
    verify) shift; verify "${1:-}" ;;
    list-bucket) shift; list_bucket "${1:-}" ;;
    *) echo "usage: azcopy-runner.sh worker <n> [concurrency] [max_jobs] | verify [deep] | list-bucket [sequential]" >&2
       exit 64 ;;
esac
