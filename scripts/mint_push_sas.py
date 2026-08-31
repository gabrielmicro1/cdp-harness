#!/usr/bin/env python3
"""mint_push_sas.py — client push credentials + token ledger + tokens page.

Mints the SAS a client uses to PUSH their corpus into `<slug>-raw`:
a container SAS, `racwl` (no delete), signed with the storage account key —
the same signing path as transfer_engine.mint_container_sas, chosen over
sas-mint's user-delegation SAS because (a) account-key SAS has no 7-day
delegation cap, so the 14-day default is possible, and (b) signing is pure
control-plane + local HMAC, so the SA firewall never matters (no IP-rule
dance, no 403 propagation waits).

The deliverable is a password-protected zip (the sas-mint packaging:
ZipCrypto via the system `zip` CLI, so `unzip -P` works anywhere) holding a
sas-credentials.txt with the SAS URL + push instructions, plus
client_push.sh — the client's upload helper, so the zip is self-sufficient
and nobody has to chase a second attachment. (`--read-only` omits the
helper: a buyer downloads, never pushes. Same split as the platform's
partner-vs-client bundles.) It lands in
`companies/<slug>/` (gitignored — a credential zip can never be committed);
the password is a fresh secrets.token_urlsafe(16) printed to stdout only.
Send the zip and the password over different channels.

Every mint is recorded in `companies/.sas-ledger.json` — METADATA ONLY
(expiry, permissions, zip path, a sha256 fingerprint); the token itself
lives only inside the zip. `page` renders the ledger as
`reports/sas-tokens.html`.

  mint_push_sas.py mint <slug> [--days N] [--note TEXT] [--read-only] [--dry-run]

`--read-only` mints an `rl` (read+list) DOWNLOAD token instead — for a data
buyer pulling the corpus out — with download instructions in the zip. The
default stays the racwl push token; the ledger records which was minted.
  mint_push_sas.py list  [--root companies]
  mint_push_sas.py page  [--root companies] [--out reports/sas-tokens.html]

Revocation caveat: an account-key SAS dies only with key rotation (which
kills EVERY outstanding SAS on that account, including a client's active
push). Policy is the same as every other SAS in this harness: never revoke,
let it lapse. Extending = mint a fresh token (--days if 14 is not enough);
the old one simply expires.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import common

# The hardcoded expiry policy. Change it here to change the fleet default;
# a single engagement that needs longer uses --days instead.
DEFAULT_EXPIRY_DAYS = 14
PERMISSIONS = "racwl"  # read/add/create/write/list — no delete, ever
READ_PERMISSIONS = "rl"  # --read-only: download token for a data buyer

LEDGER_NAME = ".sas-ledger.json"

# The client's upload helper, shipped inside every push zip. Vendored
# BYTE-IDENTICAL from cdp-platform `app/templates/client_push.sh`, which is
# the source of truth — re-copy it rather than editing this one, and keep
# `diff` between them clean. It is also the consumer of the `SAS URL:`
# contract in build_credentials_txt() below.
CLIENT_PUSH_SCRIPT = Path(__file__).resolve().parent / "templates" / "client_push.sh"


def load_client_push_script() -> str:
    if not CLIENT_PUSH_SCRIPT.is_file():
        raise RuntimeError(f"push helper template missing: {CLIENT_PUSH_SCRIPT}")
    return CLIENT_PUSH_SCRIPT.read_text()


def ledger_path(root: Path) -> Path:
    return root / LEDGER_NAME


def load_ledger(root: Path) -> dict:
    p = ledger_path(root)
    if p.is_file():
        return common.read_json(p)
    return {"tokens": []}


def token_view(entry: dict, now) -> dict:
    """Ledger entry + computed status/days_remaining (render-time, not stored)."""
    out = dict(entry)
    expires = common.parse_iso(entry["expires_at"])
    remaining = expires - now
    if remaining.total_seconds() <= 0:
        out["status"] = "expired"
        out["days_remaining"] = 0
    else:
        out["status"] = "active"
        out["days_remaining"] = round(remaining.total_seconds() / 86400, 1)
    return out


def build_credentials_txt(cfg: dict, sas_url: str, expiry: str,
                          permissions: str = PERMISSIONS) -> str:
    """The sas-credentials.txt body that goes inside the zip.

    The `SAS URL:` label and the URL on the IMMEDIATELY following line are a
    machine-read contract, not prose: the client's push helper
    (cdp-platform `app/templates/client_push.sh`) reads them with one awk
    one-liner — `/^SAS URL:/{getline; sub(/^[[:space:]]+/,""); print}` — so
    decorating the label ("SAS URL (treat as a secret...):") or slipping a
    blank line between label and value silently breaks every client upload
    with "could not read SAS URL from sas-credentials.txt". Clients already
    hold their copy of that script, so this file is the side that must
    conform. Same shape as scripts/sas-mint/mint_sas.py and the platform
    backend's _compose_credentials_file. Warnings go ABOVE the label.
    """
    account, container = cfg["storage_account"], cfg["container"]
    firewall = f"""Network ACL — the storage account firewall is default-deny. Your egress IP
must be on the allowlist or every request returns 403 even with a valid SAS.
Send us the current one and we will add it:
  az storage account network-rule add \\
    -g {cfg['resource_group']} \\
    --account-name {account} \\
    --ip-address <your-ip>"""
    if permissions == READ_PERMISSIONS:
        return f"""Azure download credentials — {cfg['slug']}

Storage account : {account}
Container       : {container}
Permissions     : {permissions} (read/list — download only, no write)
Expires (UTC)   : {expiry}

Treat the SAS URL below as a secret — anyone holding it can read the
container until it expires.

SAS URL:
  {sas_url}

{firewall}

Download options:

  azcopy (recommended; native chunking + retry):
    # macOS:   brew install azcopy
    # Linux/Windows: https://aka.ms/downloadazcopy
    azcopy copy "<SAS URL above>" "./<local-dir>/" --recursive

  curl (single blob):
    curl -o <local-file> \\
      "https://{account}.blob.core.windows.net/{container}/<blob-path>?<sas-query-only>"

List the container's contents:
  azcopy list "<SAS URL above>"
"""
    return f"""Azure push credentials — {cfg['slug']}

Storage account : {account}
Container       : {container}
Permissions     : {permissions} (read/add/create/write/list — no delete)
Expires (UTC)   : {expiry}

Treat the SAS URL below as a secret — anyone holding it can write to the
container until it expires.

SAS URL:
  {sas_url}

{firewall}

Push options:

  azcopy (recommended; native chunking + retry):
    # macOS:   brew install azcopy
    # Linux/Windows: https://aka.ms/downloadazcopy
    azcopy copy "./<local-file>" "<SAS URL above>"
    azcopy copy "./<local-dir>/" "<SAS URL above>" --recursive

  client_push.sh (bundled in this zip — the easiest path for a whole tree):
    Put each source in its own folder under one directory, then run the
    script from the folder holding this sas-credentials.txt. Every
    immediate subfolder uploads to the matching folder in the container,
    so ./exports/gdrive/ lands at <container>/gdrive/:
    ./client_push.sh --source-dir ./exports

  curl (single file):
    curl -X PUT -H "x-ms-blob-type: BlockBlob" \\
      --data-binary @<local-file> \\
      "https://{account}.blob.core.windows.net/{container}/<blob-path>?<sas-query-only>"

Verify uploads arrived:
  azcopy list "<SAS URL above>"
"""


def zip_with_password(sources: list, out_zip: Path, password: str) -> None:
    # ZipCrypto via the system `zip` CLI (sas-mint precedent): universal
    # `unzip -P` support. Not AES — the out-of-band password split is doing
    # the real work. `zip` carries unix permissions, so client_push.sh comes
    # out of `unzip` still executable.
    if not shutil.which("zip"):
        raise RuntimeError("`zip` CLI not found on PATH")
    if out_zip.exists():
        out_zip.unlink()
    subprocess.check_call(["zip", "-j", "-q", "-P", password, str(out_zip)]
                          + [str(p) for p in sources])


def cmd_mint(root: Path, args) -> dict:
    cfg = common.load_config(root, args.slug)
    common.ensure_subscription(args.dry_run)
    permissions = READ_PERMISSIONS if args.read_only else PERMISSIONS
    now = common.utc_now()
    expiry = common.iso(now + timedelta(days=args.days))
    proc = common.run_az(["storage", "container", "generate-sas",
                          "--account-name", cfg["storage_account"],
                          "-n", cfg["container"],
                          "--permissions", permissions,
                          "--expiry", expiry, "--https-only",
                          "-o", "tsv"], dry_run=args.dry_run, timeout=120)
    summary = {"slug": args.slug, "storage_account": cfg["storage_account"],
               "container": cfg["container"], "permissions": permissions,
               "days": args.days, "expires_at": expiry}
    if args.dry_run:
        summary["dry_run"] = True
        return summary

    sas = proc.stdout.strip()
    sas_url = (f"https://{cfg['storage_account']}.blob.core.windows.net/"
               f"{cfg['container']}?{sas}")

    # The deliverable: password-protected zip in the gitignored company dir.
    # The SAS lives only inside it; the password only on stdout.
    kind = "read" if args.read_only else "push"
    out_zip = (common.company_dir(root, args.slug)
               / f"{args.slug}-{kind}-sas-{common.ts_basic(now)}.zip")
    password = secrets.token_urlsafe(16)
    with tempfile.TemporaryDirectory() as td:
        tmp_txt = Path(td) / "sas-credentials.txt"
        tmp_txt.write_text(build_credentials_txt(cfg, sas_url, expiry,
                                                 permissions))
        bundle = [tmp_txt]
        if not args.read_only:
            # Push bundles carry the helper; read bundles do not (a buyer
            # downloads). Staged 0o755 so it survives the zip executable.
            tmp_sh = Path(td) / "client_push.sh"
            tmp_sh.write_text(load_client_push_script())
            tmp_sh.chmod(0o755)
            bundle.append(tmp_sh)
        zip_with_password(bundle, out_zip, password)

    entry = {"id": f"{args.slug}-{common.ts_basic(now)}", "slug": args.slug,
             "storage_account": cfg["storage_account"],
             "container": cfg["container"], "permissions": permissions,
             "signing": "account-key", "created_at": common.iso(now),
             "expires_at": expiry, "days": args.days,
             "note": args.note or "", "zip": str(out_zip),
             "fingerprint": hashlib.sha256(sas.encode()).hexdigest()[:12]}
    ledger = load_ledger(root)
    ledger["tokens"].append(entry)
    common.write_json(ledger_path(root), ledger)

    summary["id"] = entry["id"]
    summary["fingerprint"] = entry["fingerprint"]
    summary["zip"] = str(out_zip)
    summary["password"] = password
    return summary


def cmd_list(root: Path, args) -> dict:
    now = common.utc_now()
    return {"tokens": [token_view(t, now) for t in load_ledger(root)["tokens"]]}


CSS = """
:root { --s1:#121212; --s2:#191919; --w80:rgba(255,255,255,.8);
  --w60:rgba(255,255,255,.6); --w40:rgba(255,255,255,.4);
  --w10:rgba(255,255,255,.1); --w6:rgba(255,255,255,.06);
  --accent:#7065F0; --ok:#47B85F; --warn:#E6B95F; --err:#EC6D6D; }
* { box-sizing:border-box; margin:0; }
body { background:var(--s1); color:var(--w80); padding:32px 24px;
  font-family:'Outfit',-apple-system,'Segoe UI',Roboto,sans-serif; font-size:14px; }
.wrap { max-width:1100px; margin:0 auto; }
h1 { color:#fff; font-size:28px; font-weight:600; margin-bottom:4px; }
.meta { color:var(--w40); font-size:12px; margin-bottom:24px; }
table { width:100%; border-collapse:collapse; background:var(--s2);
  border-radius:8px; overflow:hidden; }
th { text-align:left; color:var(--w40); font-size:12px; font-weight:500;
  padding:10px 12px; border-bottom:1px solid var(--w6); }
td { padding:10px 12px; border-bottom:1px solid var(--w6); }
tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.badge { font-size:11px; padding:1px 6px; border-radius:4px; white-space:nowrap; }
.b-ok { background:rgba(71,184,95,.1); color:var(--ok); }
.b-warn { background:rgba(230,185,95,.1); color:var(--warn); }
.b-err { background:rgba(236,109,109,.1); color:var(--err); }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
  color:var(--w60); }
"""

EXPIRING_SOON_DAYS = 3


def cmd_page(root: Path, args) -> dict:
    import html as _html

    def esc(s) -> str:
        return _html.escape(str(s))

    now = common.utc_now()
    tokens = [token_view(t, now) for t in load_ledger(root)["tokens"]]
    tokens.sort(key=lambda t: t["created_at"], reverse=True)
    rows = []
    for t in tokens:
        if t["status"] == "expired":
            badge = '<span class="badge b-err">expired</span>'
        elif t["days_remaining"] <= EXPIRING_SOON_DAYS:
            badge = (f'<span class="badge b-warn">active — '
                     f'{t["days_remaining"]}d left</span>')
        else:
            badge = (f'<span class="badge b-ok">active — '
                     f'{t["days_remaining"]}d left</span>')
        rows.append(
            "<tr>"
            f"<td>{esc(t['slug'])}</td>"
            f"<td class='mono'>{esc(t['container'])}</td>"
            f"<td class='mono'>{esc(t['permissions'])}</td>"
            f"<td class='num'>{esc(t['days'])}</td>"
            f"<td class='mono'>{esc(t['created_at'])}</td>"
            f"<td class='mono'>{esc(t['expires_at'])}</td>"
            f"<td>{badge}</td>"
            f"<td class='mono'>{esc(t['fingerprint'])}</td>"
            f"<td>{esc(t['note'])}</td>"
            "</tr>")
    n_active = sum(1 for t in tokens if t["status"] == "active")
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>SAS tokens</title>"
        f"<style>{CSS}</style></head><body><div class='wrap'>"
        "<h1>Client push SAS tokens</h1>"
        f"<div class='meta'>generated {esc(common.iso(now))} — "
        f"{len(tokens)} minted, {n_active} active. Metadata only; the "
        "tokens themselves are never stored.</div>"
        "<table><tr><th>company</th><th>container</th><th>perms</th>"
        "<th class='num'>days</th><th>created</th><th>expires</th>"
        "<th>status</th><th>fingerprint</th><th>note</th></tr>"
        + "".join(rows) + "</table></div></body></html>")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    return {"out": str(out), "tokens": len(tokens), "active": n_active,
            "expired": len(tokens) - n_active}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    root_ap = argparse.ArgumentParser(add_help=False)
    root_ap.add_argument("--root", type=Path,
                         default=common.REPO_ROOT / "companies")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("mint", parents=[root_ap],
                       help="mint a racwl push SAS for one company "
                            "(--read-only for an rl download SAS)")
    p.add_argument("slug")
    p.add_argument("--days", type=int, default=DEFAULT_EXPIRY_DAYS,
                   help=f"expiry in days (default {DEFAULT_EXPIRY_DAYS})")
    p.add_argument("--note", default="", help="ledger note (who/why)")
    p.add_argument("--read-only", action="store_true",
                   help="mint an rl (read+list) download SAS instead of the "
                        "racwl push SAS — for a data buyer pulling the corpus")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("list", parents=[root_ap],
                   help="print the token ledger with computed status")

    p = sub.add_parser("page", parents=[root_ap],
                       help="render the tokens page from the ledger")
    p.add_argument("--out", default=str(common.REPO_ROOT / "reports"
                                        / "sas-tokens.html"))

    args = ap.parse_args()
    try:
        if args.cmd == "mint":
            out = cmd_mint(args.root, args)
        elif args.cmd == "list":
            out = cmd_list(args.root, args)
        else:
            out = cmd_page(args.root, args)
    except Exception as e:  # noqa: BLE001 — one JSON failure object, rc 1
        print(json.dumps({"outcome": "failed", "reason": str(e)}))
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
