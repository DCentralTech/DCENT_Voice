# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Validate the unsigned macOS .app/DMG/ZIP recipe on any host.

A Darwin runner still has to produce the binary. Signing and notarization need
Apple credentials. Missing notarization is an environment blocker, not a
product win.
"""

from __future__ import annotations

import argparse
import json
import platform
import plistlib
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_SCRIPT = (
    "DCENT Voice.app",
    'bash "$ROOT/scripts/build_pyinstaller.sh"',
    "hdiutil create",
    "ditto -c -k",
    "producing unsigned CI artifacts",
    "MACOS_SIGNING_IDENTITY is unset",
    "Incomplete macOS notarization credentials",
    "Notarization requires MACOS_SIGNING_IDENTITY",
    "macos-pipeline-status.json",
    "verify-payload",
    'verify-payload "$APP/Contents/MacOS"',
)

REQUIRED_WORKFLOW = (
    "runs-on: macos-14",
    "bash scripts/build_macos_app.sh",
    "dist/DCENT_Voice-macos-*.dmg",
    "dist/DCENT_Voice-macos-*.zip",
    "macos-pipeline-status.json",
    "MACOS_CERT_P12_BASE64",
    "MACOS_NOTARY_KEY_BASE64",
)

REQUIRED_ENTITLEMENTS = (
    "com.apple.security.device.audio-input",
    "com.apple.security.automation.apple-events",
)


def _missing(text: str, needles: tuple[str, ...], *, label: str) -> list[str]:
    return [f"{label}:{needle}" for needle in needles if needle not in text]


def inspect_pipeline(root: Path | None = None) -> dict[str, Any]:
    root = root or ROOT
    plist_path = root / "packaging" / "macos" / "Info.plist"
    with plist_path.open("rb") as stream:
        plist = plistlib.load(stream)
    entitlements = (root / "packaging" / "macos" / "entitlements.plist").read_text(encoding="utf-8")
    script = (root / "scripts" / "build_macos_app.sh").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    missing = [
        *_missing(entitlements, REQUIRED_ENTITLEMENTS, label="entitlements"),
        *_missing(script, REQUIRED_SCRIPT, label="script"),
        *_missing(workflow, REQUIRED_WORKFLOW, label="workflow"),
    ]
    required_plist = {
        "CFBundleIdentifier": "tech.dcentral.dcent-voice",
        "CFBundleExecutable": "dcent-voice",
        "NSMicrophoneUsageDescription": None,
        "NSAppleEventsUsageDescription": None,
        "NSAccessibilityUsageDescription": None,
        "LSUIElement": True,
        "LSMinimumSystemVersion": None,
    }
    for key, expected in required_plist.items():
        value = plist.get(key)
        if value is None or (expected is not None and value != expected):
            missing.append(f"plist:{key}")
    marketing = str(plist.get("CFBundleShortVersionString", ""))
    build = str(plist.get("CFBundleVersion", ""))
    if re.fullmatch(r"\d+\.\d+\.\d+", marketing) is None:
        missing.append("plist:CFBundleShortVersionString-invalid")
    # Apple CFBundleVersion accepts at most three numeric components. The
    # release builder replaces this neutral template value before signing.
    if re.fullmatch(r"\d{1,4}\.\d{1,2}\.\d{1,4}", build) is None:
        missing.append("plist:CFBundleVersion-invalid")
    if script.count('verify-payload "$APP/Contents/MacOS"') < 3:
        missing.append("script:verify-payload-count<3")
    notarization_order = (
        'notarytool submit "$NOTARY_ZIP"',
        'stapler staple "$APP"',
        "hdiutil create",
        'notarytool submit "$DMG"',
        'stapler staple "$DMG"',
        'hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MOUNT" "$DMG"',
        'stapler validate "$MOUNT/DCENT Voice.app"',
    )
    positions = [script.find(marker) for marker in notarization_order]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        missing.append("script:notarization-order-invalid")
    if "--check" not in script:
        missing.append("script:--check")
    host = platform.system()
    can_build = host == "Darwin"
    return {
        "unsigned_recipe_complete": not missing,
        "missing": missing,
        "this_host": host,
        "can_build_binary_on_this_host": can_build,
        "binary_blocker": None
        if can_build
        else "Apple hardware / Darwin GitHub runner (PyInstaller cannot cross-compile)",
        "signing_blocker": "MACOS_SIGNING_IDENTITY + Developer ID certificate",
        "notarization_blocker": (
            "Apple notary credentials. Missing notarization is an environment "
            "blocker, not a product win."
        ),
        "artifacts": [".app", ".dmg", ".zip", ".sha256", "macos-pipeline-status.json"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check the macOS packaging pipeline.")
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = inspect_pipeline()
    text = json.dumps(report, indent=2)
    print(text)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    if not report["unsigned_recipe_complete"]:
        print("macOS unsigned recipe is incomplete", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
