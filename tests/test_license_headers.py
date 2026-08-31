# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Verify the branded SPDX-header migration helper."""

from __future__ import annotations

from pathlib import Path

from scripts import add_license_headers as headers


def test_helper_uses_the_exact_public_beta_branding() -> None:
    assert headers.HEADER_LINES == (
        "DCENT_Voice — open-source, local-first voice dictation",
        "Copyright (c) 2026 D-Central Technologies — "
        "decentralized technologies for digital sovereignty",
        "SPDX-License-Identifier: MIT",
    )


def test_helper_migrates_legacy_html_without_duplicate_headers(tmp_path) -> None:
    path = tmp_path / "index.html"
    legacy = headers._header_for(path, "\n", headers.LEGACY_HEADER_LINES)
    content = f"<!doctype html>\n{legacy}\n<html></html>\n"

    migrated = headers._with_header(path, content)

    assert headers._has_expected_header(path, migrated)
    assert migrated.count("SPDX-License-Identifier: MIT") == 1
    assert headers._with_header(path, migrated) == migrated


def test_every_target_has_the_canonical_branded_header() -> None:
    root = Path(__file__).resolve().parents[1]

    assert headers._missing_headers(root) == []
