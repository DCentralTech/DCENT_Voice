#!/usr/bin/env bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Build the native payload and emit distributable .app, .dmg, and .zip artifacts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${1:-}" == "--check" ]]; then
  cd "$ROOT"
  exec uv run python "$ROOT/scripts/check_macos_pipeline.py"
fi
BOUNDED=(uv run python "$ROOT/scripts/run_bounded.py" --timeout 900 --)
SRC="${1:-$ROOT/dist/DCENT_Voice}"
VERSION="${DCENT_VOICE_VERSION:-$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$ROOT/pyproject.toml" | head -n 1)}"
VERSION="${VERSION#v}"
uv run python "$ROOT/scripts/release_version.py" --check-version "$VERSION"
APPLE_MARKETING_VERSION="$(uv run python "$ROOT/scripts/release_version.py" --format apple-marketing)"
APPLE_BUILD_VERSION="$(uv run python "$ROOT/scripts/release_version.py" --format apple-build)"
ARCH="$(uname -m)"
APP="$ROOT/dist/DCENT Voice.app"
DMG="${2:-$ROOT/dist/DCENT_Voice-macos-${ARCH}-${VERSION}.dmg}"
ZIP="${3:-$ROOT/dist/DCENT_Voice-macos-${ARCH}-${VERSION}.zip}"

# Resolve every `uv run` against this checkout, regardless of the caller's
# working directory.  Release validation must never import an adjacent source
# tree or create/replace the caller's virtual environment.
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This builder must run on macOS; PyInstaller cannot cross-compile." >&2
  echo "Run: uv run python scripts/check_macos_pipeline.py" >&2
  exit 1
fi

if [[ ! -x "$SRC/dcent-voice" ]]; then
  bash "$ROOT/scripts/build_pyinstaller.sh" "$SRC"
fi

"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$SRC"
SEALED="$(mktemp -d "$ROOT/dist/.dcent-macos-sealed.XXXXXX")"
case "$SEALED" in "$ROOT"/dist/.dcent-macos-sealed.*) ;; *) exit 1 ;; esac
NOTARY_ZIP=""
MOUNT=""
STATUS_TMP=""
cleanup() {
  if [[ -n "$MOUNT" ]]; then
    hdiutil detach "$MOUNT" -quiet >/dev/null 2>&1 || true
    case "$MOUNT" in "$ROOT"/dist/.dcent-macos-mount.*) rm -rf -- "$MOUNT" ;; esac
  fi
  if [[ -n "$NOTARY_ZIP" ]]; then
    case "$NOTARY_ZIP" in "$ROOT"/dist/.dcent-macos-notary.*.zip) rm -f -- "$NOTARY_ZIP" ;; esac
  fi
  if [[ -n "$STATUS_TMP" ]]; then
    case "$STATUS_TMP" in "$ROOT"/dist/.macos-pipeline-status.*.tmp) rm -f -- "$STATUS_TMP" ;; esac
  fi
  rm -rf -- "$SEALED"
}
trap cleanup EXIT
"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry stage-payload "$SRC" "$SEALED"
SRC="$SEALED"
"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$SRC"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ROOT/packaging/macos/Info.plist" "$APP/Contents/"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $APPLE_BUILD_VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $APPLE_MARKETING_VERSION" "$APP/Contents/Info.plist"
cp -a "$SRC/." "$APP/Contents/MacOS/"
chmod 0755 "$APP/Contents/MacOS/dcent-voice"

# iconutil requires a complete iconset. Derive it reproducibly from the source
# PNG so Finder and the Dock do not show the generic application icon.
if [[ -f "$ROOT/packaging/dcent-voice-256.png" ]]; then
  ICONSET="$ROOT/dist/DCENT_Voice.iconset"
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$ROOT/packaging/dcent-voice-256.png" \
      --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    doubled=$((size * 2))
    sips -z "$doubled" "$doubled" "$ROOT/packaging/dcent-voice-256.png" \
      --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil --convert icns "$ICONSET" --output "$APP/Contents/Resources/dcent-voice.icns"
  rm -rf "$ICONSET"
fi

"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$APP/Contents/MacOS"
if [[ -n "${MACOS_SIGNING_IDENTITY:-}" ]]; then
  codesign --force --deep --options runtime --timestamp \
    --entitlements "$ROOT/packaging/macos/entitlements.plist" \
    --sign "$MACOS_SIGNING_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
else
  echo "MACOS_SIGNING_IDENTITY is unset; producing unsigned CI artifacts."
fi

notary_args=()
if [[ -n "${MACOS_NOTARY_KEYCHAIN_PROFILE:-}" ]]; then
  notary_args=(--keychain-profile "$MACOS_NOTARY_KEYCHAIN_PROFILE")
elif [[ -n "${MACOS_NOTARY_KEY:-}" && -n "${MACOS_NOTARY_KEY_ID:-}" && -n "${MACOS_NOTARY_ISSUER:-}" ]]; then
  notary_args=(
    --key "$MACOS_NOTARY_KEY"
    --key-id "$MACOS_NOTARY_KEY_ID"
    --issuer "$MACOS_NOTARY_ISSUER"
  )
elif [[ -n "${MACOS_APPLE_ID:-}" && -n "${MACOS_APP_PASSWORD:-}" && -n "${MACOS_TEAM_ID:-}" ]]; then
  notary_args=(
    --apple-id "$MACOS_APPLE_ID"
    --password "$MACOS_APP_PASSWORD"
    --team-id "$MACOS_TEAM_ID"
  )
elif [[ -n "${MACOS_NOTARY_KEYCHAIN_PROFILE:-}${MACOS_NOTARY_KEY:-}${MACOS_NOTARY_KEY_ID:-}${MACOS_NOTARY_ISSUER:-}${MACOS_APPLE_ID:-}${MACOS_APP_PASSWORD:-}${MACOS_TEAM_ID:-}" ]]; then
  echo "Incomplete macOS notarization credentials." >&2
  exit 1
fi

if (( ${#notary_args[@]} )); then
  [[ -n "${MACOS_SIGNING_IDENTITY:-}" ]] || {
    echo "Notarization requires MACOS_SIGNING_IDENTITY." >&2
    exit 1
  }
  NOTARY_ZIP="$(mktemp "$ROOT/dist/.dcent-macos-notary.XXXXXX.zip")"
  case "$NOTARY_ZIP" in "$ROOT"/dist/.dcent-macos-notary.*.zip) ;; *) exit 1 ;; esac
  rm -f -- "$NOTARY_ZIP"
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$NOTARY_ZIP"
  xcrun notarytool submit "$NOTARY_ZIP" "${notary_args[@]}" --wait
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP"
fi

rm -f "$DMG" "$ZIP"
"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$APP/Contents/MacOS"
hdiutil create -quiet -fs HFS+ -volname "DCENT Voice" -srcfolder "$APP" -format UDZO "$DMG"
if [[ -n "${MACOS_SIGNING_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$MACOS_SIGNING_IDENTITY" "$DMG"
fi

if (( ${#notary_args[@]} )); then
  xcrun notarytool submit "$DMG" "${notary_args[@]}" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
  MOUNT="$(mktemp -d "$ROOT/dist/.dcent-macos-mount.XXXXXX")"
  case "$MOUNT" in "$ROOT"/dist/.dcent-macos-mount.*) ;; *) exit 1 ;; esac
  hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MOUNT" "$DMG"
  xcrun stapler validate "$MOUNT/DCENT Voice.app"
  codesign --verify --deep --strict --verbose=2 "$MOUNT/DCENT Voice.app"
  hdiutil detach "$MOUNT" -quiet
  rmdir "$MOUNT"
  MOUNT=""
fi

"${BOUNDED[@]}" uv run python -m dcent_voice.asr.model_registry verify-payload "$APP/Contents/MacOS"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
shasum -a 256 "$DMG" > "$DMG.sha256"
shasum -a 256 "$ZIP" > "$ZIP.sha256"

SIGNED=false
NOTARIZED=false
[[ -n "${MACOS_SIGNING_IDENTITY:-}" ]] && SIGNED=true
if (( ${#notary_args[@]} )); then
  NOTARIZED=true
fi
STATUS="$ROOT/dist/macos-pipeline-status.json"
STATUS_TMP="$(mktemp "$ROOT/dist/.macos-pipeline-status.XXXXXX.tmp")"
case "$STATUS_TMP" in "$ROOT"/dist/.macos-pipeline-status.*.tmp) ;; *) exit 1 ;; esac
cat > "$STATUS_TMP" <<EOF
{
  "signed": ${SIGNED},
  "notarized": ${NOTARIZED},
  "unsigned": $([[ "$SIGNED" == "true" ]] && echo false || echo true),
  "artifacts": ["$DMG", "$ZIP"],
  "notarization_is_not_a_product_win": true
}
EOF
mv -f -- "$STATUS_TMP" "$STATUS"
STATUS_TMP=""

echo "Wrote $DMG"
echo "Wrote $ZIP"
echo "Wrote $STATUS"
