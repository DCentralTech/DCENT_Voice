#!/usr/bin/env bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Verify that a packaged Linux settings window stays alive under a virtual X server.
set -euo pipefail

ARTIFACT="${1:?usage: smoke_linux_settings.sh ARTIFACT [SECONDS]}"
DURATION="${2:-10}"

command -v xvfb-run >/dev/null 2>&1 || {
  echo "xvfb-run is required for the Linux settings smoke." >&2
  exit 2
}
command -v timeout >/dev/null 2>&1 || {
  echo "timeout is required for the Linux settings smoke." >&2
  exit 2
}

set +e
DCENT_VOICE_OFFLINE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  xvfb-run -a timeout "${DURATION}s" "$ARTIFACT" --settings
STATUS=$?
set -e

if [[ "$STATUS" -ne 124 ]]; then
  printf '{"artifact":"%s","expected_timeout_s":%s,"status":"failed","exit_code":%s}\n' \
    "$ARTIFACT" "$DURATION" "$STATUS" >&2
  exit 1
fi
printf '{"artifact":"%s","expected_timeout_s":%s,"status":"alive_until_timeout"}\n' \
  "$ARTIFACT" "$DURATION"
