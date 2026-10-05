#!/usr/bin/env bash
# Download files from a Databricks Unity Catalog volume using the Databricks CLI.
#
# Usage:
#   download_from_volume.sh <volume_path> <dest_dir> <file> [<file> ...]
#
# Example:
#   download_from_volume.sh /Volumes/hyperion_dev/didi/comp_poc ./comp_data \
#     eval_sample_100_profile_ids.csv profile_compensation_estimates.jsonl.gz
#
# Auth comes from the Databricks CLI config. To use a non-default profile:
#   DATABRICKS_CONFIG_PROFILE=<profile> download_from_volume.sh ...

set -euo pipefail

if [[ $# -lt 3 ]]; then
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 1
fi

if ! command -v databricks >/dev/null 2>&1; then
  echo "error: databricks CLI not found on PATH" >&2
  exit 1
fi

volume_path="${1%/}"
dest_dir="${2%/}"
shift 2

# `databricks fs` requires the dbfs: scheme, even for /Volumes paths.
remote_base="dbfs:${volume_path#dbfs:}"

mkdir -p "$dest_dir"

failed=()
for file in "$@"; do
  dest="$dest_dir/$file"
  mkdir -p "$(dirname "$dest")"
  echo "Downloading $remote_base/$file -> $dest"
  if databricks fs cp "$remote_base/$file" "$dest" --overwrite; then
    echo "  done ($(du -h "$dest" | cut -f1))"
  else
    echo "  FAILED: $file" >&2
    failed+=("$file")
  fi
done

if [[ ${#failed[@]} -gt 0 ]]; then
  echo "error: ${#failed[@]} file(s) failed to download: ${failed[*]}" >&2
  exit 1
fi

echo "All $# file(s) downloaded to $dest_dir"
