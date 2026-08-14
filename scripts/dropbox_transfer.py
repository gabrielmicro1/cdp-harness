#!/usr/bin/env python3
"""Dropbox -> Azure transfer CLI.

Thin wrapper over transfer_engine.py — see that module and
.claude/skills/dropbox-azure-transfer/SKILL.md. Subcommands:
discover / plan / create-vm / write-azure-remote / check-azure /
write-dropbox-remote / transfer / status / verify / teardown.
Source location flag: --path (optional; empty = the whole Dropbox root the
token can see). VM is xfer-dbx-<slug> so a company can run a GCS and a
Dropbox transfer side by side. Conservative transfer defaults + --tpslimit:
Dropbox rate-limits aggressively (429 too_many_requests).
"""
import sys

import transfer_engine

SPEC = transfer_engine.Spec(
    source_name="dropbox",
    vm_prefix="xfer-dbx-",
    purpose="dropbox-transfer",
    loc_tag="dropbox_path",
    loc_argname="path",
    loc_required=False,
    default_dest_prefix="dropbox-export",
    authorize_target="dropbox",
    remote_type="dropbox",
    extra_rclone_flags="--tpslimit 12",
    default_transfers=8,
    default_checkers=16,
)

if __name__ == "__main__":
    sys.exit(transfer_engine.main(SPEC, __doc__))
