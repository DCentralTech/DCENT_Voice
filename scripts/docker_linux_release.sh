#!/usr/bin/env bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Build Linux AppImage + .deb inside Ubuntu 22.04 (matches GitHub release glibc).
# Host (Windows/macOS/Linux with Docker):
#   DCENT_IN_DOCKER=0 bash scripts/docker_linux_release.sh
#   or: powershell -File scripts/docker_linux_release.ps1
set -euo pipefail

APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64"
RUNTIME_SHA="1cc49bcf1e2ccd593c379adb17c9f85a36d619088296504de95b1d06215aebbf"
RUNTIME_BYTES=944632

if [[ "${DCENT_IN_DOCKER:-}" != "1" ]]; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  mkdir -p "$ROOT/dist"
  exec docker run --rm \
    --memory=10g \
    -e DCENT_IN_DOCKER=1 \
    -e APPIMAGE_EXTRACT_AND_RUN=1 \
    -e DEBIAN_FRONTEND=noninteractive \
    -v "$ROOT:/src:ro" \
    -v "$ROOT/dist:/out" \
    ubuntu:22.04 \
    bash /src/scripts/docker_linux_release.sh
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "In-container path requires Linux." >&2
  exit 1
fi

apt-get update -qq
apt-get install --yes --no-install-recommends \
  ca-certificates curl xz-utils file \
  appstream build-essential python3-dev linux-libc-dev libportaudio2 portaudio19-dev \
  libgtk-3-0 libgtk-3-dev libwebkit2gtk-4.0-37 libwebkit2gtk-4.0-dev \
  gir1.2-webkit2-4.0 xvfb \
  libgirepository1.0-dev libcairo2-dev pkg-config libx11-6 libxtst6 \
  >/dev/null

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="${HOME}/.local/bin:${PATH}"

mkdir -p /work
tar -C /src -cf - \
  --exclude=.venv --exclude=.git --exclude=dist --exclude=build --exclude=models \
  --exclude=__pycache__ --exclude=.pytest_cache --exclude=.mypy_cache \
  --exclude=.ruff_cache --exclude=internal --exclude=artifacts \
  . | tar -C /work -xf -
cd /work

uv python install 3.11
uv sync --extra dev --frozen

mkdir -p /opt/appimage
curl --fail --location --retry 3 "$APPIMAGETOOL_URL" --output /opt/appimage/appimagetool
echo "${APPIMAGETOOL_SHA}  /opt/appimage/appimagetool" | sha256sum --check --strict
chmod 0755 /opt/appimage/appimagetool
curl --fail --location --retry 3 "$RUNTIME_URL" --output /opt/appimage/runtime-x86_64
test "$(stat --format=%s /opt/appimage/runtime-x86_64)" -eq "$RUNTIME_BYTES"
echo "${RUNTIME_SHA}  /opt/appimage/runtime-x86_64" | sha256sum --check --strict

export APPIMAGETOOL=/opt/appimage/appimagetool
export APPIMAGE_RUNTIME_FILE=/opt/appimage/runtime-x86_64
export APPIMAGE_EXTRACT_AND_RUN=1
export DCENT_VOICE_VERSION
DCENT_VOICE_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -n 1)"
export DCENT_VOICE_VERSION

bash scripts/build_linux_appimage.sh

mkdir -p /out
shopt -s nullglob
cp -a dist/DCENT_Voice-linux-*.AppImage dist/DCENT_Voice-linux-*.AppImage.sha256 \
  dist/*.deb dist/*.sha256 /out/ 2>/dev/null || true
# Debian package name is dcent-voice_*.deb
cp -a dist/dcent-voice_*.deb dist/dcent-voice_*.deb.sha256 /out/ 2>/dev/null || true
ls -la /out
echo "Linux artifacts copied to the host dist/ directory."
