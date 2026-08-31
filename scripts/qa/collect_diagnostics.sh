#!/usr/bin/env bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
#
# Gather the evidence a failed package smoke leaves behind into build/qa-logs/
# so one upload-artifact step can publish it. Never fails the job: it runs when
# something has already gone wrong, and a missing directory is information, not
# an error.
#
# WS2 writes startup.log and last-startup-failure.json under
# <profile>/config/logs/, falling back to $TMPDIR/DCENT_Voice/ when the profile
# directory itself cannot be created — which is precisely the failure class
# worth capturing, so both are collected.
#
# Extra roots (a fake $HOME, for instance) can be named in DCENT_QA_EXTRA_ROOTS
# as a colon-separated list.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
OUT="$ROOT/build/qa-logs"
mkdir -p "$OUT"

copy_tree() {
  local source="$1" label="$2" count=0
  if [[ ! -d "$source" ]]; then
    echo "absent: $label ($source)"
    return 0
  fi
  while IFS= read -r file; do
    local relative="${file#"$source"/}"
    local target="$OUT/$label/$relative"
    mkdir -p "$(dirname "$target")"
    cp -f -- "$file" "$target" 2>/dev/null && count=$((count + 1))
  done < <(find "$source" -type f \
    \( -name '*.log' -o -name '*.json' -o -name '*.txt' \) 2>/dev/null)
  echo "collected $count file(s): $label ($source)"
}

copy_tree "$ROOT/build/qa-profiles" "profiles"
copy_tree "${TMPDIR:-/tmp}/DCENT_Voice" "temp-fallback"
copy_tree "${XDG_CONFIG_HOME:-$HOME/.config}/DCENT_Voice" "xdg-config"
copy_tree "${XDG_STATE_HOME:-$HOME/.local/state}/DCENT_Voice" "xdg-state"

index=0
IFS=':' read -r -a extra_roots <<< "${DCENT_QA_EXTRA_ROOTS:-}"
for extra in "${extra_roots[@]}"; do
  [[ -n "$extra" ]] || continue
  index=$((index + 1))
  copy_tree "$extra" "extra-$index"
done

echo "--- startup failure records found ---"
find "$OUT" -type f \
  \( -name 'last-startup-failure.json' -o -name 'startup.log' \) 2>/dev/null |
  sed 's/^/  /'

exit 0
