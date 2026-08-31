# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path

WIZARD_ROOT = Path("src/dcent_voice/ui/web/wizard")


def _asset(name: str) -> str:
    return (WIZARD_ROOT / name).read_text(encoding="utf-8")


def test_wizard_has_no_hard_coded_model_tradeoffs_or_install_claims() -> None:
    html = _asset("index.html")

    assert "nothing else to install" not in html.lower()
    assert 'style="width:80%"' not in html
    assert 'style="width:100%"' not in html
    assert 'style="width:30%"' not in html
    assert 'id="engineCard"' in html


def test_wizard_surfaces_runtime_cpu_path() -> None:
    script = _asset("wizard.js")
    assert "Runtime path" in script
    assert "no high-end GPU" in script or "without a high-end GPU" in script
    assert "cpu-int8" in script or "CPU int8" in script


def test_wizard_profile_card_is_active_profile_model_and_hardware_aware() -> None:
    script = _asset("wizard.js")

    assert "state.active_profile" in script
    assert "state.active_profile_config" in script
    assert "state.profiles?.[name]" in script
    assert 'desktop: { asr: "parakeet:tdt-0.6b-v3:int8"' in script
    assert 'quality: { asr: "faster-whisper:distil-small.en:cpu-int8"' in script
    assert 'accurate: { asr: "faster-whisper:large-v3:cpu-int8"' in script
    assert 'gpu: { asr: "faster-whisper:distil-small.en:cuda-float16"' in script
    assert '"parakeet"' in script
    assert 'asr.option.startsWith("cpu-")' in script
    assert 'asr.option.startsWith("cuda-")' in script
    assert 'label: "Automatic"' in script
    assert "works without a discrete GPU" in script
    assert "Scores are estimates, not a benchmark" in script


def test_wizard_renders_accessible_dynamic_tradeoff_meters() -> None:
    script = _asset("wizard.js")

    for axis in ("Responsiveness", "Accuracy", "Efficiency", "Sovereignty"):
        assert f'title: "{axis}"' in script
    assert 'role="meter"' in script
    assert 'aria-valuenow="${axis.score}"' in script
    assert "axis.provenance" in script
    assert "LOCAL_LLM_PROVIDERS" in script
    assert "Hybrid — transcript may leave" in script


def test_wizard_explains_verified_offline_readiness_permissions_and_helpers() -> None:
    html = _asset("index.html")
    script = _asset("wizard.js")

    combined = f"{html}\n{script}"
    assert "verified local speech model and its offline fallback" in combined
    assert "never downloads a speech model at runtime" in combined
    assert "never uploads microphone audio" in combined
    assert "reinstall the complete package" in combined.lower()
    assert "first dictation downloads" not in combined
    assert "will be fetched" not in combined
    assert "Download required before first use" not in combined
    assert "state.asr_readiness" in script
    assert "primary.ready === true" in script
    assert "fallback?.ready === true" in script
    assert "shipped Parakeet model" in script
    assert "verified Faster Whisper base fallback" in script
    assert "Using the verified offline fallback" in script
    assert "Windows checks" in script
    assert "macOS checks" in script
    assert "Accessibility permission" in script
    assert "Wayland clipboard insertion" in script
    assert "X11 clipboard insertion" in script
    assert "This wizard cannot verify those helper programs yet" in script


def test_wizard_model_readiness_uses_backend_verification_not_cache_names() -> None:
    script = _asset("wizard.js")

    assert "state.hardware?.model_readiness" in script
    assert "readiness.primary_provider" in script
    assert "installed.includes(selected)" not in script
    assert "item.includes(selected)" not in script


def test_wizard_teaches_modes_and_exposes_accessible_status() -> None:
    html = _asset("index.html")
    css = _asset("wizard.css")

    assert "Live Dictation" in html
    assert "scratch that" in html
    assert "retract" in html.lower()
    assert "With text selected" in html
    assert "With nothing selected" in html
    assert "Rich commands need a running LLM" in html
    assert "Hold a shortcut and speak" in html
    assert "Hold <kbd>Ctrl+Win</kbd>" in html
    assert "English is the default" in html
    assert 'class="advanced"' in html
    assert "Offline polish is on by default" in html
    assert "scratch that" in html
    assert 'aria-label="Microphone input level"' in html
    assert 'aria-live="polite"' in html
    assert "prefers-reduced-motion: reduce" in css
    assert "animation: none !important" in css
