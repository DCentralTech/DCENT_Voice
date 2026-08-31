#!/usr/bin/env bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Build the native Unix PyInstaller payload and stage the shipped-default model.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PAYLOAD="${1:-$ROOT/dist/DCENT_Voice}"
BOUNDED=(uv run python "$ROOT/scripts/run_bounded.py" --timeout 900 --)
cd "$ROOT"

if [[ "$(uname -s)" != "Linux" && "$(uname -s)" != "Darwin" ]]; then
  echo "build_pyinstaller.sh requires a native Linux or macOS host." >&2
  exit 1
fi

command -v uv >/dev/null 2>&1 || {
  echo "uv is required to build the release payload." >&2
  exit 1
}

# Honor an explicit project environment so a Linux-home copy without a
# checkout-local .venv still resolves `uv pip` / `uv run`. Never point this
# at a Windows .venv (those interpreters are not executable on WSL).
if [[ -n "${UV_PROJECT_ENVIRONMENT:-}" && -x "${UV_PROJECT_ENVIRONMENT}/bin/python" ]]; then
  export VIRTUAL_ENV="$UV_PROJECT_ENVIRONMENT"
  if [[ ! -e "$ROOT/.venv" ]]; then
    ln -sfn "$UV_PROJECT_ENVIRONMENT" "$ROOT/.venv"
  fi
fi

uv sync --extra dev --frozen
uv run pyinstaller packaging/DCENT_Voice.spec --noconfirm --clean

DEFAULT_OUT="$ROOT/dist/DCENT_Voice"
if [[ ! -x "$PAYLOAD/dcent-voice" && -x "$DEFAULT_OUT/dcent-voice" && "$PAYLOAD" != "$DEFAULT_OUT" ]]; then
  mkdir -p "$PAYLOAD"
  # spec always writes ROOT/dist/DCENT_Voice; copy when the caller asked
  # for a separate payload root (WSL home, not the Windows checkout).
  cp -a "$DEFAULT_OUT/." "$PAYLOAD/"
fi

if [[ ! -x "$PAYLOAD/dcent-voice" ]]; then
  echo "PyInstaller did not produce $PAYLOAD/dcent-voice." >&2
  exit 1
fi

# Import the platform backend from the frozen archive before wrapping it. This
# caught the former Linux artifact that built successfully but crashed on
# `--settings` because pywebview's dynamically imported `gi` bridge was absent.
"${BOUNDED[@]}" "$PAYLOAD/dcent-voice" platform-check

# Acquire both immutable snapshots only in this explicit release-build step.
# Runtime dictation is local-files-only and never contacts Hugging Face.
uv run python scripts/download_models.py \
  --bundle-dir "$PAYLOAD" \
  --models istupakov/parakeet-tdt-0.6b-v3-onnx,Systran/faster-whisper-base \
  --accept-model-license
uv run python scripts/generate_release_sbom.py \
  --payload "$PAYLOAD" \
  --toc "$ROOT/build/DCENT_Voice/PYZ-00.toc" \
  --repo-root "$ROOT"
"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$PAYLOAD"

# Never publish the absolute checkout path recorded by editable installs.
if find "$PAYLOAD" -path '*/dcent_voice-*.dist-info/direct_url.json' -print -quit | grep -q .; then
  echo "PyInstaller output contains editable-install direct_url.json provenance." >&2
  exit 1
fi

echo "Native payload ready at $PAYLOAD"
