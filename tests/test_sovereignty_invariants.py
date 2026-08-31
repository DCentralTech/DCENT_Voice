# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Structural sovereignty invariants against shipped code (not re-implementations)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from dcent_voice.config import parse_config
from dcent_voice.pipeline import PipelineWorker
from dcent_voice.privacy import ConsentRequired, PrivacyMonitor

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "dcent_voice"


def test_default_profile_is_local_whisper_without_cloud() -> None:
    config = parse_config(
        {
            "active_profile": "desktop",
            "profile": {
                "desktop": {
                    "asr": "faster-whisper:base.en:cpu-int8",
                    "llm": "none",
                    "cleanup_enabled": False,
                }
            },
        }
    )
    assert config.current_profile.asr.provider == "faster-whisper"
    assert config.current_profile.asr.model == "base.en"
    assert config.current_profile.asr.compute_type == "cpu-int8"
    assert config.current_profile.llm.provider == "none"
    assert config.current_profile.cleanup_enabled is False
    assert config.service.allow_lan is False
    assert config.service.host in {"127.0.0.1", "localhost", "::1"}


def test_shipped_example_desktop_is_offline_local_cpu() -> None:
    """Fresh installs copy config.example.toml — must be local CPU, no cloud."""
    from pathlib import Path

    from dcent_voice.config import load_config

    config = load_config(Path("config.example.toml"), create=False)
    assert config.session_locality.value in {"local", "sovereign"}
    assert config.current_profile.asr.provider == "parakeet"
    assert config.current_profile.asr.compute_type == "int8"
    assert config.current_profile.cleanup_enabled is False
    assert config.current_profile.llm.provider == "none"


def test_service_rejects_non_loopback_without_allow_lan() -> None:
    from dcent_voice.config import ConfigError

    with pytest.raises(ConfigError, match="allow_lan"):
        parse_config(
            {
                "active_profile": "desktop",
                "service": {"host": "0.0.0.0", "allow_lan": False},
                "profile": {
                    "desktop": {
                        "asr": "faster-whisper:tiny",
                        "llm": "none",
                        "cleanup_enabled": False,
                    }
                },
            }
        )


def test_pipeline_source_does_not_log_transcripts_by_default() -> None:
    source = inspect.getsource(PipelineWorker)
    # Shipped path must gate transcript content on explicit env opt-in.
    assert "DCENT_VOICE_LOG_TRANSCRIPTS" in Path(SRC / "pipeline.py").read_text(encoding="utf-8")
    assert "_LOG_TRANSCRIPTS" in source or "LOG_TRANSCRIPTS" in Path(SRC / "pipeline.py").read_text(
        encoding="utf-8"
    )


def test_no_background_update_polling_module() -> None:
    updates = (SRC / "util" / "updates.py").read_text(encoding="utf-8")
    # Update check is an explicit callable path, not a daemon timer loop.
    assert "schedule" not in updates.lower() or "APScheduler" not in updates
    assert "while True" not in updates
    assert "threading.Timer" not in updates


def test_privacy_monitor_blocks_cloud_without_consent(tmp_path: Path) -> None:
    ledger = tmp_path / "consent.jsonl"
    config = parse_config(
        {
            "active_profile": "cloud",
            "privacy": {"consent_ledger_path": str(ledger)},
            "profile": {
                "cloud": {
                    "asr": "deepgram:nova-3",
                    "llm": "none",
                    "cleanup_enabled": False,
                }
            },
        }
    )
    monitor = PrivacyMonitor.from_config(config)
    with pytest.raises(ConsentRequired):
        monitor.validate_cloud_consent()


def test_config_example_ships_local_defaults() -> None:
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert 'active_profile = "desktop"' in text
    assert "faster-whisper" in text
    assert "cleanup_enabled = false" in text
