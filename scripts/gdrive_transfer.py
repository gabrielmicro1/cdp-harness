#!/usr/bin/env python3
"""Google Drive -> Azure transfer CLI.

Thin wrapper over transfer_engine.py — see that module and
.claude/skills/gdrive-azure-transfer/SKILL.md. Subcommands:
discover / plan / create-vm / write-azure-remote / check-azure /
write-gdrive-remote / transfer / status / verify / teardown.

Source location flag: --path (optional; empty = the root of My Drive, or of
the Shared Drive when --team-drive is given). --team-drive <id> targets a
Shared Drive; the id flows CLI flag → VM tag → rclone.conf `team_drive` so
any later session rediscovers it. VM is xfer-gdr-<slug> so it can coexist
with GCS/Dropbox transfers. Conservative defaults + --tpslimit: the Drive
API 403s (userRateLimitExceeded) under aggressive parallelism. Native
Google Docs/Sheets/Slides are exported as docx/xlsx/pptx (rclone default).
"""
import sys

import transfer_engine

SPEC = transfer_engine.Spec(
    source_name="gdrive",
    vm_prefix="xfer-gdr-",
    purpose="gdrive-transfer",
    loc_tag="gdrive_path",
    loc_argname="path",
    loc_required=False,
    default_dest_prefix="gdrive-export",
    authorize_target="drive",
    remote_type="drive",
    remote_extra="scope = drive\n",
    default_transfers=8,
    default_checkers=16,
    default_tpslimit=10,
    extra_cli_opts=[{
        "flag": "--team-drive", "argname": "team_drive",
        "tag": "gdrive_team_drive", "conf_key": "team_drive",
        "help": "Shared Drive ID (default: the token owner's My Drive)",
    }, {
        "flag": "--shared-with-me", "argname": "shared_with_me",
        "tag": "gdrive_shared_with_me", "conf_key": "shared_with_me",
        "help": "pass 'true' when the source is a folder shared TO the "
                "token's account ('Shared with me' — not in its My Drive "
                "and not a Shared Drive)",
    }],
)

if __name__ == "__main__":
    sys.exit(transfer_engine.main(SPEC, __doc__))
