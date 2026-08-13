#!/usr/bin/env python3
"""corpus_sizer_rest.py — dependency-free per-source corpus sizer (portable).

Ported from the proven corpus-transfer-engine sizer (croplabel / webspiders /
latchel runs). Uses ONLY the Python standard library plus a SAS token — no
azure-storage-blob, no managed identity, no SDKs. It only talks to the storage
account, so it runs anywhere with HTTPS reach: this laptop (the harness
default — phases.py launches it as a detached local process) or any VM.

Env:
  SA                 storage account name
  CONTAINER          container name
  AZURE_STORAGE_SAS  a SAS with read+list (rl) on the account/container
  TAG                short label for output files (default: CONTAINER)
  OUT_DIR            directory for output files (default /var/tmp)
  MAX_ZIP_ENTRIES    safety cap on entries parsed per zip (default 5_000_000)

Writes in OUT_DIR:
  <TAG>.log           progress
  <TAG>.sizes.tsv     name<TAB>compressed<TAB>uncompressed<TAB>ratio<TAB>method
  <TAG>.summary       human per-datasource table + totals (decimal GB, /1e9)
  <TAG>.summary.json  compact machine summary (what harvest reads)
  <TAG>.done          written on clean completion

Sizing (no bulk download — reads only indexes/trailers):
  .zip            -> End-of-Central-Directory + Central Directory via Range GETs
                     (ZIP64-aware); sum entry uncompressed sizes. NON-recursive.
  .gz/.tgz/.tar.gz-> 4-byte ISIZE trailer, floored at compressed size (exact
                     <4 GiB, sane lower bound above). Never streams — so it
                     stays fast on multi-GB backup tarballs.
  everything else -> uncompressed = content-length (accurate for loose files).
Read-only; writes nothing back to the blob.
"""
import json
import os
import struct
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

SA = os.environ["SA"]
CONTAINER = os.environ["CONTAINER"]
SAS = os.environ.get("AZURE_STORAGE_SAS") or os.environ["SAS"]
TAG = os.environ.get("TAG", CONTAINER)
MAX_ZIP_ENTRIES = int(os.environ.get("MAX_ZIP_ENTRIES", "5000000"))

BASE = os.path.join(os.environ.get("OUT_DIR", "/var/tmp"), TAG)
LOG, OUT = f"{BASE}.log", f"{BASE}.sizes.tsv"
SUMMARY, SUMMARY_JSON, DONE = f"{BASE}.summary", f"{BASE}.summary.json", f"{BASE}.done"


def logmsg(m):
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [{TAG}] {m}\n")


def _auth(url):
    return url + ("&" if "?" in url else "?") + SAS


def http_get(url, extra_headers=None):
    hdrs = {"x-ms-version": "2021-08-06"}
    if extra_headers:
        hdrs.update(extra_headers)
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(_auth(url), headers=hdrs)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1 + attempt)
    raise last


def blob_base(name):
    return f"https://{SA}.blob.core.windows.net/{CONTAINER}/{urllib.parse.quote(name, safe='/')}"


def fetch_range(name, start, end):
    return http_get(blob_base(name), {"Range": f"bytes={start}-{end}"})


# ── ZIP central-directory parsing (ZIP64-aware) — ported from size_corpus.py ──
EOCD_SIG = 0x06054b50
EOCD_STRUCT = "<IHHHHIIH"
EOCD_SIZE = 22
LOC64_SIG = 0x07064b50
LOC64_STRUCT = "<IIQI"
LOC64_SIZE = 20
EOCD64_STRUCT = "<IQHHIIQQQQ"
CD_SIG = 0x02014b50


def _parse_cd(cd, total_entries):
    total_uncomp = 0
    n = 0
    p = 0
    cap = min(total_entries, MAX_ZIP_ENTRIES)
    sig = struct.pack("<I", CD_SIG)
    while p + 46 <= len(cd) and n < cap:
        if cd[p:p + 4] != sig:
            break
        (_s, _vm, _vn, _fl, _meth, _mt, _md, _crc, csize, usize,
         name_len, extra_len, cmt_len, _dn, _ia, _ea, _lho) = struct.unpack(
            "<IHHHHHHIIIHHHHHII", cd[p:p + 46])
        name_end = p + 46 + name_len
        extra_end = name_end + extra_len
        if usize == 0xFFFFFFFF:  # ZIP64 — real usize is in the extra field
            ex = cd[name_end:extra_end]
            xp = 0
            while xp + 4 <= len(ex):
                xid, xlen = struct.unpack("<HH", ex[xp:xp + 4])
                if xid == 0x0001:
                    zdata = ex[xp + 4:xp + 4 + xlen]
                    zp = 0
                    # order: usize, csize, lho, disk — usize first if it was 0xFFFFFFFF
                    usize = struct.unpack("<Q", zdata[zp:zp + 8])[0]
                    break
                xp += 4 + xlen
        total_uncomp += usize
        n += 1
        p = extra_end + cmt_len
    return total_uncomp, n


def zip_uncompressed(name, clen):
    """Return (uncompressed_bytes, note). Range-reads only the EOCD + CD."""
    tail_size = min(clen, 65557)
    tail = fetch_range(name, clen - tail_size, clen - 1)
    idx = tail.rfind(struct.pack("<I", EOCD_SIG))
    if idx < 0:
        return clen, "zip-no-eocd"
    (_sig, _disk, _cds, _etd, total_entries, cd_size, cd_off, _cl) = struct.unpack(
        EOCD_STRUCT, tail[idx:idx + EOCD_SIZE])
    if total_entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        loc_start = idx - LOC64_SIZE
        if loc_start < 0:
            return clen, "zip-loc64-split"
        (lsig, _ld, eocd64_off, _nd) = struct.unpack(LOC64_STRUCT, tail[loc_start:idx])
        if lsig != LOC64_SIG:
            return clen, "zip-loc64-bad"
        e64 = fetch_range(name, eocd64_off, eocd64_off + 55)
        (_s64, _sz64, _v, _vn, _d64, _cds64, _etd64, total_entries,
         cd_size, cd_off) = struct.unpack(EOCD64_STRUCT, e64[:56])
    cd = fetch_range(name, cd_off, cd_off + cd_size - 1)
    total_uncomp, n = _parse_cd(cd, total_entries)
    note = f"zip:{n}entries" if n == total_entries else f"zip:partial{n}/{total_entries}"
    return total_uncomp, note


def gz_uncompressed(name, clen):
    """4-byte ISIZE trailer, floored at compressed size. Never streams."""
    if clen < 4:
        return clen, "gz-tiny"
    trailer = fetch_range(name, clen - 4, clen - 1)
    isize = struct.unpack("<I", trailer[-4:])[0]
    return max(isize, clen), "gz-trailer"


def enumerate_and_size():
    open(OUT, "w").close()
    per = defaultdict(lambda: [0, 0, 0])  # prefix -> [files, comp, uncomp]
    err_types = defaultdict(int)          # exception class name -> count
    methods = defaultdict(int)            # zip / gz / stored blob counts
    marker = ""
    n = 0
    zero = 0
    errors = 0
    with open(OUT, "a", buffering=1) as tsv:
        while True:
            url = (f"https://{SA}.blob.core.windows.net/{CONTAINER}"
                   f"?restype=container&comp=list&maxresults=5000")
            if marker:
                url += "&marker=" + urllib.parse.quote(marker, safe="")
            root = ET.fromstring(http_get(url))
            for b in root.findall(".//Blob"):
                name = b.findtext("Name") or ""
                clen = int(b.findtext(".//Content-Length") or "0")
                lname = name.lower()
                uncomp, method = clen, "stored"
                kind = "stored"
                try:
                    if lname.endswith(".zip"):
                        kind = "zip"
                        uncomp, method = zip_uncompressed(name, clen)
                    elif lname.endswith((".tar.gz", ".tgz", ".gz")):
                        kind = "gz"
                        uncomp, method = gz_uncompressed(name, clen)
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    err_types[type(exc).__name__] += 1
                    method = f"err:{type(exc).__name__}"
                    uncomp = clen
                    logmsg(f"ERROR {name}: {type(exc).__name__}: {str(exc)[:160]}")
                methods[kind] += 1
                if clen == 0:
                    zero += 1
                ratio = (uncomp / clen) if clen else 0.0
                tsv.write(f"{name}\t{clen}\t{uncomp}\t{ratio:.3f}\t{method}\n")
                top = name.split("/", 1)[0] if "/" in name else "(root)"
                rec = per[top]
                rec[0] += 1
                rec[1] += clen
                rec[2] += uncomp
                n += 1
                if clen >= 1_000_000_000:
                    logmsg(f"  big: {name} comp={clen/1e9:.2f}GB unc={uncomp/1e9:.2f}GB ({method})")
                if n % 5000 == 0:
                    logmsg(f"progress {n} blobs, errors={errors}")
            marker = root.findtext("NextMarker") or ""
            if not marker:
                break
    return per, n, zero, errors, dict(err_types), dict(methods)


def write_summary(per, n, zero, errors, err_types, methods, dur_s):
    gb = lambda x: x / 1e9  # noqa: E731
    tc = sum(v[1] for v in per.values())
    tu = sum(v[2] for v in per.values())
    lines = [
        f"SA: {SA}",
        f"Container: {CONTAINER}",
        f"Blobs: {n}",
        f"Compressed:   {tc} bytes ({gb(tc):.2f} GB)",
        f"Uncompressed: {tu} bytes ({gb(tu):.2f} GB)",
        (f"Ratio: {tu / tc:.3f}" if tc else "Ratio: n/a"),
        f"Errors: {errors}",
        "",
        f"{'datasource':<22}{'files':>10}{'compressed_GB':>16}{'uncompressed_GB':>18}{'ratio':>9}",
    ]
    for k in sorted(per, key=lambda k: -per[k][2]):
        f_, c_, u_ = per[k]
        r_ = (u_ / c_) if c_ else 0.0
        lines.append(f"{k:<22}{f_:>10}{gb(c_):>16.2f}{gb(u_):>18.2f}{r_:>9.3f}")
    with open(SUMMARY, "w") as f:
        f.write("\n".join(lines) + "\n")
    # Compact machine summary — what phases.harvest_one reads.
    # src values are [files, compressed, uncompressed] arrays.
    machine = {
        "sa": SA, "container": CONTAINER, "blobs": n, "comp": tc, "unc": tu,
        "zero": zero, "errors": errors, "err_types": err_types,
        "methods": methods, "dur_s": round(dur_s),
        "src": {k: v for k, v in sorted(per.items(), key=lambda kv: -kv[1][2])},
    }
    with open(SUMMARY_JSON, "w") as f:
        json.dump(machine, f, separators=(",", ":"))
        f.write("\n")


def main():
    open(LOG, "w").close()
    t0 = time.time()
    logmsg(f"start SA={SA} CONTAINER={CONTAINER} (stdlib+SAS)")
    per, n, zero, errors, err_types, methods = enumerate_and_size()
    write_summary(per, n, zero, errors, err_types, methods, time.time() - t0)
    logmsg(f"size done blobs={n} errors={errors}")
    open(DONE, "w").close()
    print(open(SUMMARY).read())


if __name__ == "__main__":
    main()
