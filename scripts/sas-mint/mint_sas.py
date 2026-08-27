#!/usr/bin/env python3
"""
mint_sas.py — mint a user-delegation SAS for a corpus-transfer container and
bundle the credentials into a password-protected zip.

Two roles, picked with --role:
  partner  (default) — the source company pushing their corpus INTO our
                       landing bucket. Targets the "-raw" container with
                       upload perms (racwl: read/add/create/write/list;
                       no delete). Produces "<deal>-raw-sas.zip".
  client            — the buyer/scrubber pulling the scrubbed corpus OUT
                       of our storage. Targets the "-scrubbed" container
                       with read-only perms (rl: read + list). Produces
                       "<deal>-scrubbed-sas.zip".

Usage:
    python mint_sas.py <storage-account-name> [--role partner|client]
                       [--days N] [--container NAME] [--permissions PERMS]
                       [--out FILE]

Examples:
    # Partner (seller) push SAS for wrd-digital raw container:
    python mint_sas.py wrddigitalprod6337f5

    # Client (buyer) pull SAS for wrd-digital scrubbed container:
    python mint_sas.py wrddigitalprod6337f5 --role client

Prerequisites the runner (you) must have before this succeeds:
  1. `az login` completed against the tenant that owns the storage account
     (DefaultAzureCredential picks up the CLI's token).
  2. Azure RBAC on the storage account (or a parent scope):
       - "Storage Blob Delegator" — required for get_user_delegation_key.
       - "Storage Blob Data Contributor" (or Owner) — the SAS inherits the
         signer's data-plane roles at mint-time (see [[sas-permission-snapshot]]),
         so requesting `racwl` while only having Reader silently gives you a
         read-only SAS. Grant at the SA (or container) scope. For --role
         client (rl only), "Storage Blob Data Reader" is sufficient.
       - "Reader" on the SA control-plane — used by `az storage account show`
         to resolve the resource group. Contributor/Owner works too.
       Note: "Owner" alone does NOT include data-plane rights — Azure RBAC
       separates control and data planes for storage. You need an explicit
       Blob Data role on top of Owner.
  3. Network reachability to the SA's blob endpoint. The corpus SAs are
     default_action=Deny (see [[corpus-storage-nacl]]), so your current
     public IP must be on the account's ip-rules allowlist or every call
     — including get_user_delegation_key — returns 403 AuthorizationFailure.
     Add it once with:
         az storage account network-rule add \\
           -g <rg> --account-name <sa> --ip-address <your-ip>
  4. Local tools on PATH: `az` (subscription lookup + RG lookup) and `zip`
     (bundled on macOS; `apt install zip` on Ubuntu).

Notes:
  - Default container depends on --role (-raw for partner, -scrubbed for
    client); override with --container. Discovery uses list_containers,
    which is data-plane — see prereq #3.
  - Default TTL is 7 days (Azure's cap on user-delegation SAS).
  - Password is a fresh secrets.token_urlsafe(16) printed to stdout at the end.
    Share it out-of-band from the zip itself.
  - Zip encryption uses the system `zip` CLI (ZipCrypto) so any recipient can
    open it with `unzip -P <password>`. Not AES — do not rely on it for
    confidentiality of high-value secrets.
"""
from __future__ import annotations

import argparse
import datetime as dt
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobServiceClient,
    ContainerSasPermissions,
    generate_container_sas,
)


@dataclass(frozen=True)
class RolePolicy:
    """Per-role defaults for container suffix, perms, and doc text."""
    container_suffix: str  # e.g. "-raw" or "-scrubbed"
    perms: str             # SAS permission letters
    direction: str         # "push" or "pull" — labels the credentials file
    verb_hint: str         # human-readable perms description
    verify_verb: str       # "uploads arrived" / "delivery ready"


ROLES: dict[str, RolePolicy] = {
    "partner": RolePolicy(
        container_suffix="-raw",
        perms="racwl",
        direction="push",
        verb_hint="read, add, create, write, list (racwl) — upload + listing, no delete",
        verify_verb="uploads arrived",
    ),
    "client": RolePolicy(
        container_suffix="-scrubbed",
        perms="rl",
        direction="pull",
        verb_hint="read, list (rl) — download + listing, no write / no delete",
        verify_verb="delivery is ready",
    ),
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def pick_container(svc: BlobServiceClient, override: str | None, suffix: str) -> str:
    if override:
        return override
    names = [c.name for c in svc.list_containers()]
    matches = [n for n in names if n.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"no container ending in {suffix!r} on this account; found: {names}")
    sys.exit(f"multiple {suffix!r} containers ({matches}); pass --container to pick one")


def find_resource_group(account: str) -> str:
    try:
        rg = subprocess.check_output(
            ["az", "storage", "account", "show", "-n", account, "--query", "resourceGroup", "-o", "tsv"],
            stderr=subprocess.STDOUT,
        ).decode().strip()
    except subprocess.CalledProcessError as e:
        sys.exit(f"could not resolve resource group for {account!r}: {e.output.decode().strip()}")
    if not rg:
        sys.exit(f"storage account {account!r} not found in the current subscription")
    return rg


def deal_slug(container: str, suffix: str) -> str:
    return container[: -len(suffix)] if container.endswith(suffix) else container


def build_credentials_txt(
    *,
    account: str,
    container: str,
    resource_group: str,
    sas_url: str,
    expiry: dt.datetime,
    signer_upn: str,
    role: str,
    policy: RolePolicy,
) -> str:
    deal = deal_slug(container, policy.container_suffix)

    if policy.direction == "push":
        transfer_block = f"""Push options:

  azcopy (recommended for archives > 100 MB; native chunking + retry):
    # macOS:   brew install azcopy
    # Linux:   https://aka.ms/downloadazcopy
    # Windows: https://aka.ms/downloadazcopy
    azcopy copy "./<local-file>" "<SAS URL above>"
    azcopy copy "./<local-dir>/" "<SAS URL above>" --recursive

  curl (single file):
    curl -X PUT -H "x-ms-blob-type: BlockBlob" \\
      --data-binary @<local-file> \\
      "https://{account}.blob.core.windows.net/{container}/<blob-path>?<sas-query-only>"

  client_push.sh (walks a local ./exports/ tree and pushes each subfolder into
    the matching source prefix — see the repo for the script):
    ./client_push.sh --creds sas-credentials.txt --source-dir ./exports

Verify {policy.verify_verb}:
  azcopy list "<SAS URL above>"
"""
    else:
        transfer_block = f"""Pull options:

  azcopy (recommended — resumable, chunked, checksum-verified):
    # macOS:   brew install azcopy
    # Linux:   https://aka.ms/downloadazcopy
    # Windows: https://aka.ms/downloadazcopy
    azcopy copy "<SAS URL above>" "./<local-dir>/" --recursive

  server-side copy into your own Azure container (no egress via your laptop):
    azcopy copy "<SAS URL above>" "<your-destination-sas-url>" --recursive

  curl (single file):
    curl -o <local-file> \\
      "https://{account}.blob.core.windows.net/{container}/<blob-path>?<sas-query-only>"

Verify {policy.verify_verb}:
  azcopy list "<SAS URL above>"
"""

    return f"""{deal} corpus-transfer {policy.direction} credentials  (role: {role})
Minted: {utcnow():%Y-%m-%d %H:%M} UTC by {signer_upn} (AAD user-delegation SAS)

Container URL:
  https://{account}.blob.core.windows.net/{container}

SAS URL:
  {sas_url}

Properties:
  - Container:   {container} only (cannot reach any other container)
  - Permissions: {policy.verb_hint}
  - Valid:       until {expiry:%Y-%m-%dT%H:%MZ}
  - HTTPS-only, signed by the minter's AAD identity (revocable by sign-out,
    token rotation, or role removal)

Network ACL — the storage account firewall is default-deny. Your egress IP must
be on the allowlist or every request returns 403 even with a valid SAS. Send us
the current one and we will add it:
  az storage account network-rule add \\
    -g {resource_group} \\
    --account-name {account} \\
    --ip-address <your-ip>

{transfer_block}"""


def signer_display_name() -> str:
    try:
        upn = subprocess.check_output(
            ["az", "account", "show", "--query", "user.name", "-o", "tsv"]
        ).decode().strip()
        return upn or "current AAD user"
    except Exception:
        return "current AAD user"


def zip_with_password(source_txt: Path, out_zip: Path, password: str) -> None:
    if not shutil.which("zip"):
        sys.exit("`zip` CLI not found on PATH — install `zip` (macOS: bundled; Ubuntu: apt install zip)")
    if out_zip.exists():
        out_zip.unlink()
    # -j strip directory paths, -P password (ZipCrypto — universal `unzip` support)
    subprocess.check_call(
        ["zip", "-j", "-q", "-P", password, str(out_zip), str(source_txt)]
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("account", help="Storage account name, e.g. wrddigitalprod6337f5")
    p.add_argument(
        "--role",
        choices=sorted(ROLES.keys()),
        default="partner",
        help=(
            "Access policy: 'partner' (seller pushes raw corpus in; -raw / racwl; default) or "
            "'client' (buyer pulls scrubbed corpus out; -scrubbed / rl)."
        ),
    )
    p.add_argument("--days", type=int, default=7, help="SAS TTL in days (Azure caps user-delegation SAS at 7; default: 7)")
    p.add_argument("--container", help="Override container name (default: the -raw or -scrubbed container matching --role)")
    p.add_argument("--permissions", help="Override SAS perm letters (default depends on --role: racwl for partner, rl for client)")
    p.add_argument("--out", help="Output zip path (default: ./<deal>-<raw|scrubbed>-sas.zip)")
    args = p.parse_args()

    policy = ROLES[args.role]
    perms = args.permissions or policy.perms

    cred = DefaultAzureCredential()
    resource_group = find_resource_group(args.account)

    svc = BlobServiceClient(f"https://{args.account}.blob.core.windows.net", credential=cred)
    container = pick_container(svc, args.container, policy.container_suffix)

    start = utcnow() - dt.timedelta(minutes=5)  # small skew tolerance
    expiry = utcnow() + dt.timedelta(days=args.days)

    udk = svc.get_user_delegation_key(key_start_time=start, key_expiry_time=expiry)
    sas_query = generate_container_sas(
        account_name=args.account,
        container_name=container,
        user_delegation_key=udk,
        permission=ContainerSasPermissions.from_string(perms),
        expiry=expiry,
        start=start,
        protocol="https",
    )
    sas_url = f"https://{args.account}.blob.core.windows.net/{container}?{sas_query}"

    txt = build_credentials_txt(
        account=args.account,
        container=container,
        resource_group=resource_group,
        sas_url=sas_url,
        expiry=expiry,
        signer_upn=signer_display_name(),
        role=args.role,
        policy=policy,
    )

    deal = deal_slug(container, policy.container_suffix)
    suffix_tag = policy.container_suffix.lstrip("-")  # "raw" or "scrubbed"
    out_zip = Path(args.out) if args.out else Path.cwd() / f"{deal}-{suffix_tag}-sas.zip"
    password = secrets.token_urlsafe(16)

    with tempfile.TemporaryDirectory() as td:
        tmp_txt = Path(td) / "sas-credentials.txt"
        tmp_txt.write_text(txt)
        zip_with_password(tmp_txt, out_zip, password)

    print(f"role:     {args.role}")
    print(f"zip:      {out_zip}")
    print(f"password: {password}")
    print(f"expires:  {expiry:%Y-%m-%dT%H:%MZ}  ({args.days}d)")
    print(f"container:{container}")
    print(f"perms:    {perms}")
    print(f"rg:       {resource_group}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
