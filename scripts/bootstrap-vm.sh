#!/usr/bin/env bash
# Bootstrap the transfer VM: rclone + azcopy + tmux + config dirs. Idempotent —
# safe to re-run on a half-bootstrapped VM. Run as root (piped via
# `ssh ... sudo bash -s` by scripts/gcs_transfer.py).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Wait for cloud-init before touching apt. On a freshly created Azure VM it is
# still running at first-ssh: it holds the dpkg lock and leaves
# /var/lib/apt/lists empty, so an install racing it resolves ONLY packages
# already baked into the image and fails the rest with "Unable to locate
# package" — while tmux/curl/git appear to succeed because they are
# pre-installed, which makes the failure look arbitrary. Hit live on the
# wallaroo takeout VM, 2026-08-31 (unzip, python3-boto3, git-lfs all missed).
cloud-init status --wait >/dev/null 2>&1 || true
# Lock::Timeout covers unattended-upgrades, which can grab dpkg right after
# cloud-init releases it; the retry covers a transient mirror failure, which
# -qq would otherwise swallow into an empty package list.
APT_OPTS="-o DPkg::Lock::Timeout=300"
for attempt in 1 2 3; do
    apt-get $APT_OPTS update -qq && break
    echo "apt-get update failed (attempt $attempt) — retrying in 15s"
    sleep 15
done
# python3-boto3: flat-bucket range-sharded S3 listing (s3_flat.py) — VM-only
# dependency; laptop-side harness scripts stay stdlib-only
# git + git-lfs: the github-azure-transfer puller (mirror clones + LFS
# fetch; a fetch-only path needs just the binaries, no `git lfs install`).
# Harmless few-MB extra for the other sources.
apt-get $APT_OPTS install -y -qq tmux curl unzip python3-boto3 git git-lfs >/dev/null

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

for pkg in tmux curl unzip git; do
    command -v "$pkg" >/dev/null 2>&1 || {
        echo "FATAL: $pkg missing after install — apt lists are incomplete"
        exit 1
    }
done

echo "bootstrap-complete"
