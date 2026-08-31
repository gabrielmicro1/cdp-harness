#!/usr/bin/env bash
# client_push.sh — upload your data-source folders to micro1.
#
# Each immediate subdirectory under --source-dir is uploaded recursively
# to the matching folder inside the secure landing container. So:
#
#     ./exports/
#       gdrive/     →  <container>/gdrive/
#       slack/      →  <container>/slack/
#       github/     →  <container>/github/
#
# Usage:
#     ./client_push.sh --source-dir ./exports
#         # reads the SAS URL from sas-credentials.txt in the current dir
#
#     ./client_push.sh --source-dir ./exports --sas-url 'https://…?sv=…'
#         # use an explicit SAS URL (e.g. the previous bundle expired and
#         # you got a fresh URL on its own, no new bundle).
#
# Requires: bash, azcopy (https://aka.ms/downloadazcopy).
# Tested on macOS, Ubuntu 22.04 / 24.04, and Git Bash on Windows.

set -euo pipefail

CREDS_FILE="sas-credentials.txt"
SOURCE_DIR=""
SAS_URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir) SOURCE_DIR="${2:-}"; shift 2 ;;
    --sas-url)    SAS_URL="${2:-}"; shift 2 ;;
    --creds)      CREDS_FILE="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

command -v azcopy >/dev/null \
  || { echo "azcopy not found — install: https://aka.ms/downloadazcopy" >&2; exit 1; }

[[ -n "$SOURCE_DIR" ]] || { echo "--source-dir is required" >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "--source-dir $SOURCE_DIR does not exist" >&2; exit 1; }

if [[ -z "$SAS_URL" ]]; then
  [[ -f "$CREDS_FILE" ]] \
    || { echo "$CREDS_FILE not found (or pass --sas-url)" >&2; exit 1; }
  SAS_URL=$(awk '/^SAS URL:/{getline; sub(/^[[:space:]]+/,""); print; exit}' "$CREDS_FILE")
  [[ -n "$SAS_URL" ]] \
    || { echo "could not read SAS URL from $CREDS_FILE" >&2; exit 1; }
fi

# Split the SAS URL once: <container_url>?<sas_query>. Each per-source
# target is then <container_url>/<source>/?<sas_query>.
container_root="${SAS_URL%%\?*}"
sas_query="${SAS_URL#*\?}"

shopt -s nullglob
count=0
failures=()
for d in "$SOURCE_DIR"/*/; do
  key=$(basename "$d")
  target="${container_root}/${key}/?${sas_query}"
  echo "→ uploading $key/  →  <container>/$key/"
  # --as-subdir=false: the target ALREADY names the destination folder
  # (<container>/<key>/). azcopy defaults to --as-subdir=true, which would
  # re-append the source folder's own name and nest it as
  # <container>/<key>/<key>/. Copy the directory's CONTENTS into the target
  # instead, so ./exports/<key>/ maps to <container>/<key>/ as documented.
  if azcopy copy "$d" "$target" --recursive --as-subdir=false --overwrite=ifSourceNewer; then
    count=$((count + 1))
  else
    failures+=("$key")
  fi
done

if [[ $count -eq 0 && ${#failures[@]} -eq 0 ]]; then
  echo "no subdirectories under $SOURCE_DIR — nothing to upload" >&2
  exit 1
fi

if [[ ${#failures[@]} -gt 0 ]]; then
  echo
  echo "✗ ${#failures[@]} source(s) failed: ${failures[*]}"
  echo "  re-run the same command — --overwrite=ifSourceNewer makes retries cheap."
  exit 1
fi

echo
echo "✓ uploaded $count source(s)."
