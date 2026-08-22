#!/usr/bin/env bash
# Bootstrap the transfer VM: rclone + azcopy + tmux + config dirs. Idempotent —
# safe to re-run on a half-bootstrapped VM. Run as root (piped via
# `ssh ... sudo bash -s` by scripts/gcs_transfer.py).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python3-boto3: flat-bucket range-sharded S3 listing (s3_flat.py) — VM-only
# dependency; laptop-side harness scripts stay stdlib-only
apt-get install -y -qq tmux curl unzip python3-boto3 >/dev/null

if ! command -v rclone >/dev/null 2>&1; then
    curl -fsSL https://rclone.org/install.sh | bash
fi
rclone version | head -1

# azcopy: the S3 -> Azure server-side copy engine (s3-azure-transfer).
# Harmless few-MB extra for the rclone-only sources.
if ! command -v azcopy >/dev/null 2>&1; then
    tmpd="$(mktemp -d)"
    curl -fsSL https://aka.ms/downloadazcopy-v10-linux | tar xz -C "$tmpd"
    install -m 755 "$tmpd"/azcopy_linux_amd64_*/azcopy /usr/local/bin/azcopy
    rm -rf "$tmpd"
fi
azcopy --version | head -1

# rclone.conf lives here, mode 600, owned by the admin user; secrets die
# with the VM.
install -d -m 700 -o azureuser -g azureuser /home/azureuser/.config
install -d -m 700 -o azureuser -g azureuser /home/azureuser/.config/rclone

echo "bootstrap-complete"
