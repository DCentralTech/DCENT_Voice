# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Does the user's configuration exist, parse, and point at models that are here?

Doctor deliberately loads the config with ``create=False``: it must describe a
missing or corrupt config, never silently repair one. Seeding and recovery are
the app's job on a normal launch; the whole point of this check is to say
"your config.toml is at X and line 12 is invalid" to somebody reading a report
over a chat window.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..result import FAIL, PASS, WARN, CheckResult

_LOCAL_MODEL_PROVIDERS = frozenset({"faster-whisper", "parakeet"})


def run(*, config_path: Path | None = None) -> list[CheckResult]:
    from dcent_voice.config import ConfigError, default_config_path, load_config

    path = config_path or default_config_path()
    file_result, exists = check_file(path)
    if not exists:
        skipped = "skipped: no configuration file to inspect"
        return [
            file_result,
            CheckResult("config.profile", PASS, skipped),
            CheckResult("config.asr_model", PASS, skipped),
            CheckResult("config.unbundled_models", PASS, skipped),
        ]

    try:
        config = load_config(path, create=False)
    except ConfigError as exc:
        parse_failed = CheckResult(
            "config.profile",
            FAIL,
            f"the configuration could not be loaded: {exc}",
            "Delete or rename the file and relaunch DCENT_Voice: it reseeds a default "
            f"configuration from the bundled example. Your file is at {path}.",
            {"path": str(path)},
        )
        blocked = "not evaluated: the configuration did not load"
        return [
            file_result,
            parse_failed,
            CheckResult("config.asr_model", PASS, blocked),
            CheckResult("config.unbundled_models", PASS, blocked),
        ]
    except OSError as exc:
        unreadable = CheckResult(
            "config.profile",
            FAIL,
            f"the configuration file could not be read: {exc}",
            "Check file permissions on the config directory, or set "
            "DCENT_VOICE_PROFILE_ROOT to a writable directory.",
            {"path": str(path)},
        )
        return [
            file_result,
            unreadable,
            CheckResult("config.asr_model", PASS, "not evaluated"),
            CheckResult("config.unbundled_models", PASS, "not evaluated"),
        ]

    return [
        file_result,
        check_profile(config, path),
        check_active_asr_model(config),
        check_unbundled_models(config),
    ]


def check_file(path: Path) -> tuple[CheckResult, bool]:
    data: dict[str, Any] = {"path": str(path)}
    if not path.exists():
        return (
            CheckResult(
                "config.file",
                WARN,
                f"no configuration file at {path}. On the next launch the app seeds one from "
                "the bundled example; if that ever failed, the app used to exit silently.",
                "Launch DCENT_Voice once. If the file is still absent afterwards, check the "
                "env.install and env.write_access results in this report.",
                data,
            ),
            False,
        )
    try:
        info = path.stat()
        data["sizeBytes"] = info.st_size
        data["modified"] = info.st_mtime
    except OSError as exc:
        data["error"] = str(exc)
    result = CheckResult("config.file", PASS, f"configuration file present at {path}", data=data)
    return result, True


def check_profile(config: Any, path: Path) -> CheckResult:
    profile = config.current_profile
    data = {
        "path": str(path),
        "activeProfile": config.active_profile,
        "profiles": sorted(config.profiles),
        "asr": profile.asr.raw,
        "llm": profile.llm.raw,
        "languageMode": config.language_mode,
        "language": config.language,
        "locality": config.session_locality.value,
        "servicePort": config.service.port,
        "serviceHost": config.service.host,
    }
    return CheckResult(
        "config.profile",
        PASS,
        f"active profile {config.active_profile!r}: asr={profile.asr.raw} llm={profile.llm.raw} "
        f"({config.session_locality.value})",
        data=data,
    )


def check_active_asr_model(config: Any) -> CheckResult:
    spec = config.current_profile.asr
    data: dict[str, Any] = {"spec": spec.raw, "provider": spec.provider, "model": spec.model}
    if spec.provider not in _LOCAL_MODEL_PROVIDERS:
        return CheckResult(
            "config.asr_model",
            PASS,
            f"the active profile uses {spec.provider!r}, which has no locally shipped weights "
            "to resolve",
            data=data,
        )
    resolved, detail = resolve_local_model(spec.provider, spec.model)
    data["resolved"] = str(resolved) if resolved else ""
    data["detail"] = detail
    if resolved is None:
        return CheckResult(
            "config.asr_model",
            FAIL,
            f"the model for the active profile ({spec.raw}) is not available locally: {detail}",
            "Reinstall DCENT_Voice so the shipped models are staged again, or switch the "
            "active profile to one whose model is present (see config.unbundled_models). "
            "DCENT_Voice never downloads speech models while dictating.",
            data,
        )
    return CheckResult(
        "config.asr_model",
        PASS,
        f"{spec.raw} resolves to {resolved}",
        data=data,
    )


def check_unbundled_models(config: Any) -> CheckResult:
    unresolved: list[str] = []
    data: dict[str, Any] = {}
    for name, profile in sorted(config.profiles.items()):
        spec = profile.asr
        if spec.provider not in _LOCAL_MODEL_PROVIDERS:
            data[name] = {"spec": spec.raw, "resolved": "", "detail": "not a local model provider"}
            continue
        resolved, detail = resolve_local_model(spec.provider, spec.model)
        data[name] = {
            "spec": spec.raw,
            "resolved": str(resolved) if resolved else "",
            "detail": detail,
        }
        if resolved is None:
            unresolved.append(f"{name} ({spec.raw})")
    if unresolved:
        return CheckResult(
            "config.unbundled_models",
            WARN,
            f"{len(unresolved)} profile(s) reference a model that is not installed: "
            + ", ".join(unresolved)
            + ". Only the active profile matters for dictation right now.",
            "Switching to one of these profiles in Settings will fail until the model is "
            "installed. Build an offline bundle with scripts/download_models.py, or stay on "
            "a profile whose model ships with the app.",
            data,
        )
    return CheckResult(
        "config.unbundled_models",
        PASS,
        f"every local model referenced by the {len(data)} configured profile(s) is installed",
        data=data,
    )


def resolve_local_model(provider: str, model: str) -> tuple[Path | None, str]:
    """Resolve a profile's weights the same way the ASR factory would."""
    if provider == "parakeet":
        from dcent_voice.asr.parakeet_provider import resolve_parakeet_model_dir

        try:
            path = resolve_parakeet_model_dir()
        except Exception as exc:  # noqa: BLE001 - report, never propagate
            return None, f"{type(exc).__name__}: {exc}"
        if path is None:
            return None, "no verified Parakeet snapshot found beside the app or in the profile"
        return path, "verified snapshot"
    if provider == "faster-whisper":
        from dcent_voice.asr.model_registry import (
            ModelUnavailableError,
            resolve_faster_whisper_model,
        )

        try:
            return Path(resolve_faster_whisper_model(model)), "verified snapshot"
        except ModelUnavailableError as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001 - report, never propagate
            return None, f"{type(exc).__name__}: {exc}"
    return None, f"unknown local provider {provider!r}"
