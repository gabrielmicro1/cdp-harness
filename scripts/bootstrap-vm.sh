#!/usr/bin/env bash
# Bootstrap the transfer VM: rclone + tmux + config dirs. Idempotent —
# safe to re-run on a half-bootstrapped VM. Run as root (piped via
# `ssh ... sudo bash -s` by scripts/gcs_transfer.py).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq tmux curl unzip >/dev/null

if ! command -v rclone >/dev/null 2>&1; then
    curl -fsSL https://rclone.org/install.sh | bash
fi
rclone version | head -1

# rclone.conf lives here, mode 600, owned by the admin user; secrets die
# with the VM.
install -d -m 700 -o azureuser -g azureuser /home/azureuser/.config
install -d -m 700 -o azureuser -g azureuser /home/azureuser/.config/rclone

echo "bootstrap-complete"
