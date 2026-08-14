#!/usr/bin/env python3
"""GCS -> Azure transfer CLI (Google Workspace Data Export buckets).

Thin wrapper over transfer_engine.py — see that module and
.claude/skills/gcs-azure-transfer/SKILL.md. Subcommands:
discover / plan / create-vm / write-azure-remote / check-azure /
write-gcs-remote / transfer / status / verify / teardown.
Source location flag: --bucket (required for plan/create-vm).
"""
import sys

import transfer_engine

SPEC = transfer_engine.Spec(
    source_name="gcs",
    vm_prefix="xfer-",
    purpose="gcs-transfer",
    loc_tag="gcs_bucket",
    loc_argname="bucket",
    loc_required=True,
    default_dest_prefix="workspace-export",
    authorize_target="google cloud storage",
    remote_type="google cloud storage",
    default_transfers=32,
    default_checkers=64,
)

if __name__ == "__main__":
    sys.exit(transfer_engine.main(SPEC, __doc__))
