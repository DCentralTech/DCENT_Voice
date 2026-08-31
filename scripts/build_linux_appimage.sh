#!/usr/bin/env bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Build a native payload, an AppImage, and a Debian package on Linux.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOUNDED=(uv run python "$ROOT/scripts/run_bounded.py" --timeout 900 --)
SRC="${1:-$ROOT/dist/DCENT_Voice}"
VERSION="${DCENT_VOICE_VERSION:-$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$ROOT/pyproject.toml" | head -n 1)}"
VERSION="${VERSION#v}"
uv run python "$ROOT/scripts/release_version.py" --check-version "$VERSION"
DEB_VERSION="$(uv run python "$ROOT/scripts/release_version.py" --format debian)"

case "$(uname -m)" in
  x86_64|amd64) APPIMAGE_ARCH="x86_64"; DEB_ARCH="amd64" ;;
  aarch64|arm64) APPIMAGE_ARCH="aarch64"; DEB_ARCH="arm64" ;;
  *) echo "Unsupported Linux release architecture: $(uname -m)" >&2; exit 1 ;;
esac

APPIMAGE_OUT="${2:-$ROOT/dist/DCENT_Voice-linux-${APPIMAGE_ARCH}-${VERSION}.AppImage}"
DEB_OUT="${3:-$ROOT/dist/DCENT_Voice-linux-${DEB_ARCH}-${VERSION}.deb}"
APPDIR="$ROOT/dist/DCENT_Voice.AppDir"
DEBROOT="$ROOT/dist/dcent-voice_${DEB_VERSION}_${DEB_ARCH}"

# Resolve every `uv run` against this checkout, regardless of the caller's
# working directory.  Release validation must never import an adjacent source
# tree or create/replace the caller's virtual environment.
cd "$ROOT"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This builder must run on Linux; PyInstaller cannot cross-compile." >&2
  exit 1
fi

if [[ ! -x "$SRC/dcent-voice" ]]; then
  bash "$ROOT/scripts/build_pyinstaller.sh" "$SRC"
fi

"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$SRC"
SEALED="$(mktemp -d "$ROOT/dist/.dcent-linux-sealed.XXXXXX")"
case "$SEALED" in "$ROOT"/dist/.dcent-linux-sealed.*) ;; *) exit 1 ;; esac
trap 'rm -rf -- "$SEALED"' EXIT
"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry stage-payload "$SRC" "$SEALED"
SRC="$SEALED"
"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$SRC"
MODEL_ROOT="$SRC/models"
# Model sources may come from a Windows/DrvFS release cache where every file is
# presented as executable. Never carry world-writable or executable model data
# into a root-owned Debian installation.
find "$MODEL_ROOT" -type d -exec chmod 0755 {} +
find "$MODEL_ROOT" -type f -exec chmod 0644 {} +

normalize_package_directories() {
  local tree="$1"
  local label="$2"
  local unexpected

  # The sealed snapshot root is intentionally private (0700). GNU cp -a of
  # SOURCE/. copies that mode onto an already-created destination directory,
  # which would make an otherwise valid system package unusable by non-root
  # users. Package payloads contain no user secrets: every directory must be
  # traversable, while ordinary file modes remain unchanged.
  find "$tree" -type d -exec chmod 0755 {} +
  unexpected="$(find "$tree" -type d ! -perm 0755 -print -quit)"
  if [[ -n "$unexpected" ]]; then
    echo "$label contains a directory that is not mode 0755: $unexpected" >&2
    exit 1
  fi
}

rm -rf "$APPDIR" "$DEBROOT"
mkdir -p \
  "$APPDIR/usr/bin" \
  "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/metainfo" \
  "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$SRC/." "$APPDIR/usr/bin/"
cp "$ROOT/packaging/linux/dcent-voice.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/packaging/linux/dcent-voice.desktop" "$APPDIR/"
sed "s/@VERSION@/$VERSION/g" \
  "$ROOT/packaging/linux/tech.dcentral.dcent-voice.metainfo.xml" \
  > "$APPDIR/usr/share/metainfo/tech.dcentral.dcent-voice.metainfo.xml"
command -v appstreamcli >/dev/null 2>&1 || {
  echo "appstreamcli is required to validate Linux release metadata." >&2
  exit 1
}
# --no-net: local release builds must not fail because GitHub is unreachable.
appstreamcli validate --no-net \
  "$APPDIR/usr/share/metainfo/tech.dcentral.dcent-voice.metainfo.xml"
if [[ -f "$ROOT/packaging/dcent-voice-256.png" ]]; then
  cp "$ROOT/packaging/dcent-voice-256.png" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps/dcent-voice.png"
  cp "$ROOT/packaging/dcent-voice-256.png" "$APPDIR/dcent-voice.png"
fi
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="$HERE/usr/bin:$PATH"
wayland_ready=false
x11_ready=false
if command -v wl-copy >/dev/null 2>&1 &&
   { command -v wtype >/dev/null 2>&1 || command -v ydotool >/dev/null 2>&1; }; then
  wayland_ready=true
fi
if { command -v xclip >/dev/null 2>&1 || command -v xsel >/dev/null 2>&1; } &&
   command -v xdotool >/dev/null 2>&1; then
  x11_ready=true
fi
if [ "$wayland_ready" != true ] && [ "$x11_ready" != true ]; then
  echo "DCENT Voice AppImage needs host clipboard helpers for universal insertion:" >&2
  echo "  Wayland: wl-clipboard + wtype (or ydotool); X11: xclip/xsel + xdotool." >&2
fi
exec "$HERE/usr/bin/dcent-voice" "$@"
EOF
chmod 0755 "$APPDIR/AppRun"
normalize_package_directories "$APPDIR" "AppDir"
"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$APPDIR/usr/bin"

APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"
APPIMAGE_RUNTIME_FILE="${APPIMAGE_RUNTIME_FILE:-}"
if [[ "$APPIMAGETOOL" == */* ]]; then
  [[ -x "$APPIMAGETOOL" ]] || {
    echo "APPIMAGETOOL is not executable: $APPIMAGETOOL" >&2
    exit 1
  }
else
  command -v "$APPIMAGETOOL" >/dev/null 2>&1 || {
    echo "appimagetool is required (set APPIMAGETOOL to its path)." >&2
    exit 1
  }
fi
if [[ -z "$APPIMAGE_RUNTIME_FILE" || ! -f "$APPIMAGE_RUNTIME_FILE" ]]; then
  echo "APPIMAGE_RUNTIME_FILE must name a pinned local type2 runtime." >&2
  exit 1
fi
ARCH="$APPIMAGE_ARCH" VERSION="$VERSION" APPIMAGE_EXTRACT_AND_RUN=1 \
  "$APPIMAGETOOL" --no-appstream --runtime-file "$APPIMAGE_RUNTIME_FILE" \
  "$APPDIR" "$APPIMAGE_OUT"
chmod 0755 "$APPIMAGE_OUT"

mkdir -p \
  "$DEBROOT/DEBIAN" \
  "$DEBROOT/opt/dcent-voice" \
  "$DEBROOT/usr/bin" \
  "$DEBROOT/usr/share/applications" \
  "$DEBROOT/usr/share/metainfo" \
  "$DEBROOT/usr/share/icons/hicolor/256x256/apps"
cp -a "$SRC/." "$DEBROOT/opt/dcent-voice/"
sed \
  -e "s/^Version:.*/Version: $DEB_VERSION/" \
  -e "s/^Architecture:.*/Architecture: $DEB_ARCH/" \
  "$ROOT/packaging/linux/debian/control" > "$DEBROOT/DEBIAN/control"
cp "$ROOT/packaging/linux/debian/postinst" "$DEBROOT/DEBIAN/postinst"
chmod 0755 "$DEBROOT/DEBIAN/postinst"
cat > "$DEBROOT/usr/bin/dcent-voice" <<'EOF'
#!/bin/sh
wayland_ready=false
x11_ready=false
if command -v wl-copy >/dev/null 2>&1 &&
   { command -v wtype >/dev/null 2>&1 || command -v ydotool >/dev/null 2>&1; }; then
  wayland_ready=true
fi
if { command -v xclip >/dev/null 2>&1 || command -v xsel >/dev/null 2>&1; } &&
   command -v xdotool >/dev/null 2>&1; then
  x11_ready=true
fi
if [ "$wayland_ready" != true ] && [ "$x11_ready" != true ]; then
  echo "DCENT Voice clipboard helpers are unavailable; see apt package recommendations." >&2
fi
exec /opt/dcent-voice/dcent-voice "$@"
EOF
chmod 0755 "$DEBROOT/usr/bin/dcent-voice" "$DEBROOT/opt/dcent-voice/dcent-voice"
cp "$ROOT/packaging/linux/dcent-voice.desktop" "$DEBROOT/usr/share/applications/"
sed "s/@VERSION@/$VERSION/g" \
  "$ROOT/packaging/linux/tech.dcentral.dcent-voice.metainfo.xml" \
  > "$DEBROOT/usr/share/metainfo/tech.dcentral.dcent-voice.metainfo.xml"
if [[ -f "$ROOT/packaging/dcent-voice-256.png" ]]; then
  cp "$ROOT/packaging/dcent-voice-256.png" \
    "$DEBROOT/usr/share/icons/hicolor/256x256/apps/dcent-voice.png"
fi
normalize_package_directories "$DEBROOT" "Debian package tree"
command -v dpkg-deb >/dev/null 2>&1 || {
  echo "dpkg-deb is required to produce the Debian package." >&2
  exit 1
}
"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$DEBROOT/opt/dcent-voice"
dpkg-deb --root-owner-group --build "$DEBROOT" "$DEB_OUT"

(
  cd "$(dirname "$APPIMAGE_OUT")"
  sha256sum "$(basename "$APPIMAGE_OUT")" > "$(basename "$APPIMAGE_OUT").sha256"
)
(
  cd "$(dirname "$DEB_OUT")"
  sha256sum "$(basename "$DEB_OUT")" > "$(basename "$DEB_OUT").sha256"
)
echo "Wrote $APPIMAGE_OUT"
echo "Wrote $DEB_OUT"
