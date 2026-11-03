#!/usr/bin/env bash
set -euo pipefail

if (( $# != 2 )); then
  echo 'cleanup requires the runner temp root and the dedicated audit directory' >&2
  exit 2
fi

runner_temp="$1"
target="$2"
runner_root="$(realpath -e -- "$runner_temp")"
target_parent="$(realpath -e -- "$(dirname -- "$target")")"
target_name="$(basename -- "$target")"

if [[ "$target_parent" != "$runner_root" \
  || ! "$target_name" =~ ^monikey-access-audit\.[A-Za-z0-9]{6}$ \
  || ! -d "$target" \
  || -L "$target" ]]; then
  echo 'refusing to remove a path that is not the dedicated audit temp directory' >&2
  exit 1
fi

rm -rf -- "$target"
