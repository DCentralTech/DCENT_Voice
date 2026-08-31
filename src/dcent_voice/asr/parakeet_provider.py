# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Optional NVIDIA Parakeet local ASR via onnx-asr (CPU ONNX, no CUDA)."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from dcent_voice.asr.base import (
    PARAKEET_V3_LANGUAGE_CODES,
    ASRProvider,
    Locality,
    TranscriptResult,
)
from dcent_voice.asr.lifecycle import DEFAULT_IDLE_UNLOAD_S, IdleUnloadMixin
from dcent_voice.asr.model_registry import (
    PINNED_PARAKEET_MODEL_ID,
    ModelUnavailableError,
    bundled_model_root,
    model_root,
    pinned_huggingface_snapshot,
    pinned_model_manifest,
    stage_verified_snapshot,
    verified_snapshot_lock,
    verify_pinned_snapshot,
)
from dcent_voice.asr.quality import sanitize_transcript
from dcent_voice.config import APP_NAME, ASRSpec
from dcent_voice.util import paths

logger = logging.getLogger(APP_NAME).getChild("asr")

DEFAULT_PARAKEET_MODEL = "nemo-parakeet-tdt-0.6b-v3"
BUNDLED_DIRNAME = "parakeet-tdt-0.6b-v3"
# TDT decoders often emit nothing on sub-2 s utterances. Pad to this floor.
_MIN_AUDIO_S = 3.0
_LEAD_PAD_S = 0.2
_ALIASES = {
    "v3": DEFAULT_PARAKEET_MODEL,
    "tdt-0.6b-v3": DEFAULT_PARAKEET_MODEL,
    "parakeet-tdt-0.6b-v3": DEFAULT_PARAKEET_MODEL,
    "nemo-parakeet-tdt-0.6b-v3": DEFAULT_PARAKEET_MODEL,
    "v2": "nemo-parakeet-tdt-0.6b-v2",
    "en": "nemo-parakeet-tdt-0.6b-v2",
    "tdt-0.6b-v2": "nemo-parakeet-tdt-0.6b-v2",
    "parakeet-tdt-0.6b-v2": "nemo-parakeet-tdt-0.6b-v2",
    "nemo-parakeet-tdt-0.6b-v2": "nemo-parakeet-tdt-0.6b-v2",
}


def resolve_parakeet_model_name(model: str) -> str:
    key = (model or "").strip()
    return _ALIASES.get(key.lower(), key or DEFAULT_PARAKEET_MODEL)


def looks_like_parakeet_dir(path: Path) -> bool:
    valid, _detail = verify_pinned_snapshot(path, PINNED_PARAKEET_MODEL_ID)
    return valid


def application_root() -> Path:
    """Directory that owns the shipped payload (exe dir when frozen, repo otherwise)."""
    return paths.app_dir()


def resolve_parakeet_model_dir(*, verify: bool = True) -> Path | None:
    """Resolve only an exact verified local snapshot; never hit the network."""
    candidates: list[Path] = []
    env = (os.environ.get("DCENT_VOICE_PARAKEET_DIR") or "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    root = application_root()
    candidates.extend(
        (
            model_root() / BUNDLED_DIRNAME,
            bundled_model_root() / BUNDLED_DIRNAME,
            root / "_internal" / "models" / BUNDLED_DIRNAME,
            root / "dist" / "DCENT_Voice" / "models" / BUNDLED_DIRNAME,
        )
    )
    cached = pinned_huggingface_snapshot(PINNED_PARAKEET_MODEL_ID)
    if cached is not None:
        candidates.append(cached)
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = Path(os.path.abspath(candidate.expanduser()))
        if candidate.is_symlink():
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        if looks_like_parakeet_dir(candidate) if verify else _looks_like_parakeet_shape(candidate):
            return candidate
    return None


def _looks_like_parakeet_shape(path: Path) -> bool:
    """Cheap candidate filter; the bound loader still hashes every byte."""
    manifest = pinned_model_manifest(PINNED_PARAKEET_MODEL_ID)
    if manifest is None or path.is_symlink() or not path.is_dir():
        return False
    try:
        entries = tuple(path.iterdir())
        if {entry.name for entry in entries} != set(manifest["files"]):
            return False
        return all(
            not entry.is_symlink()
            and entry.is_file()
            and entry.stat().st_size == manifest["files"][entry.name]["size"]
            for entry in entries
        )
    except OSError:
        return False


def default_hf_parakeet_snapshot() -> Path | None:
    candidate = pinned_huggingface_snapshot(PINNED_PARAKEET_MODEL_ID)
    return candidate if candidate is not None and looks_like_parakeet_dir(candidate) else None


def require_parakeet_model_dir(*, verify: bool = True) -> Path:
    candidate = resolve_parakeet_model_dir(verify=verify)
    if candidate is not None:
        return candidate
    raise ModelUnavailableError(
        "The verified local NVIDIA Parakeet TDT 0.6B v3 model is unavailable. "
        "DCENT Voice never downloads speech models during dictation. Reinstall "
        "the complete package or explicitly build and install the verified offline "
        "model bundle with scripts/download_models.py."
    )


def parakeet_model_status() -> dict[str, Any]:
    manifest = pinned_model_manifest(PINNED_PARAKEET_MODEL_ID)
    try:
        path = require_parakeet_model_dir()
    except ModelUnavailableError as exc:
        return {
            "ready": False,
            "model_id": PINNED_PARAKEET_MODEL_ID,
            "revision": manifest["revision"] if manifest else None,
            "path": None,
            "detail": str(exc),
        }
    return {
        "ready": True,
        "model_id": PINNED_PARAKEET_MODEL_ID,
        "revision": manifest["revision"] if manifest else None,
        "path": str(path),
        "detail": "Verified local snapshot ready",
    }


def stage_parakeet_bundle(dest_root: Path, source: Path | None = None) -> Path:
    """Copy exactly the verified allowlist next to the offline payload."""
    # Release builders commonly point at an already verified snapshot through
    # DCENT_VOICE_PARAKEET_DIR. Honor the same resolver used at runtime before
    # falling back to this platform user's Hugging Face cache.
    src = source or resolve_parakeet_model_dir() or default_hf_parakeet_snapshot()
    if src is None or not looks_like_parakeet_dir(src):
        raise FileNotFoundError(
            "Verified Parakeet ONNX weights were not found. Reinstall the complete "
            "package or explicitly acquire the pinned offline bundle with "
            "scripts/download_models.py --accept-model-license."
        )
    dest = dest_root / "models" / BUNDLED_DIRNAME
    return stage_verified_snapshot(src, dest, PINNED_PARAKEET_MODEL_ID)


def parakeet_available() -> bool:
    try:
        import onnx_asr  # noqa: F401
    except ImportError as exc:
        logger.warning("onnx_asr import failed: %s", exc)
        return False
    return True


class ParakeetASRProvider(IdleUnloadMixin, ASRProvider):
    """Local Parakeet TDT via onnx-asr. Optional extra; never the implicit default."""

    locality = Locality.LOCAL
    supports_per_call_language = True
    supports_language_auto_detection = True
    supported_language_codes = PARAKEET_V3_LANGUAGE_CODES
    language_hint_effect = "metadata_only"
    reports_detected_language = False

    def __init__(
        self,
        spec: ASRSpec,
        *,
        language: str = "en",
        idle_unload_s: float = DEFAULT_IDLE_UNLOAD_S,
    ) -> None:
        if spec.provider != "parakeet":
            raise ValueError(f"Unsupported ASR provider for ParakeetASRProvider: {spec.raw}")
        self.spec = spec
        configured = self.validate_language_hint(language)
        self.language = "auto" if configured == "" else configured or "en"
        self.model_name = resolve_parakeet_model_name(spec.model)
        self._model: Any | None = None
        self._fallback: ASRProvider | None = None
        self._lock = threading.RLock()
        self._last_decode_s: float | None = None
        self._last_load_s: float | None = None
        self.init_idle_unload(idle_unload_s)

    def load(self) -> None:
        with self._lock:
            if self._model is not None or self._fallback is not None:
                return
            try:
                import onnx_asr
            except ImportError as exc:
                raise RuntimeError(
                    "parakeet requires the optional extra: "
                    'python -m pip install "dcent-voice[parakeet]" '
                    "or `uv pip install onnx-asr`."
                ) from exc
            started = time.perf_counter()
            quantization = _quantization(self.spec.compute_type)
            kwargs: dict[str, Any] = {"sess_options": _cpu_session_options()}
            if quantization:
                kwargs["quantization"] = quantization
            explicit = Path(self.spec.model).expanduser()
            if explicit.is_dir():
                valid, detail = verify_pinned_snapshot(explicit, PINNED_PARAKEET_MODEL_ID)
                if not valid:
                    raise ModelUnavailableError(
                        f"Explicit Parakeet snapshot failed verification: {detail}"
                    )
                local_dir = explicit.resolve()
            else:
                try:
                    # Candidate discovery checks the exact manifest shape. The
                    # bound lock below performs the one authoritative SHA-256
                    # pass while denying replacement until ONNX has opened it.
                    local_dir = require_parakeet_model_dir(verify=False)
                except ModelUnavailableError:
                    from dcent_voice.asr.faster_whisper_provider import (
                        FasterWhisperASRProvider,
                    )

                    fallback = FasterWhisperASRProvider(
                        ASRSpec.parse("faster-whisper:base:cpu-int8"),
                        language=self.language,
                    )
                    fallback.load()
                    self._fallback = fallback
                    logger.warning(
                        "verified Parakeet unavailable; using pinned local Faster Whisper base"
                    )
                    self.notify_lifecycle(True, "loaded_fallback")
                    self.arm_idle_timer(self.unload)
                    return
            _assert_offline_local_dir(local_dir)
            logger.info("loading verified Parakeet weights from %s (offline)", local_dir)
            with verified_snapshot_lock(local_dir, PINNED_PARAKEET_MODEL_ID):
                self._model = onnx_asr.load_model(
                    self.model_name,
                    # ``path`` is what makes onnx_asr.Resolver set ``offline=True``
                    # (onnx_asr/resolver.py: ``if self.local_dir.exists(): self.offline
                    # = True``). It has no ``offline=`` kwarg of its own, so the
                    # directory must be proven to exist *before* the call — see
                    # _assert_offline_local_dir. Without it a vanished snapshot
                    # silently degrades into a Hugging Face download attempt.
                    path=local_dir,
                    **kwargs,
                )
            self._last_load_s = time.perf_counter() - started
            logger.info(
                "parakeet ready model=%s quantization=%s load_s=%.3f",
                self.model_name,
                quantization or "fp32",
                self._last_load_s,
            )
            self.notify_lifecycle(True, "loaded")
            self.arm_idle_timer(self.unload)

    def is_loaded(self) -> bool:
        return self._model is not None or self._fallback is not None

    def unload(self) -> None:
        with self._lock:
            self.cancel_idle_timer()
            was_loaded = self.is_loaded()
            self._model = None
            if self._fallback is not None:
                self._fallback.unload()
                self._fallback = None
        if was_loaded:
            self.notify_lifecycle(False, "idle_unload")
        self.collect_unloaded()

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        del initial_prompt, hotwords  # Parakeet TDT has no Whisper-style prompt.
        requested_language = self.validate_language_hint(language)
        try:
            with self._lock:
                self.cancel_idle_timer()
                self.load()
                fallback = self._fallback
                model = self._model
            if fallback is not None:
                return fallback.transcribe(
                    audio,
                    samplerate=samplerate,
                    language=("auto" if requested_language == "" else requested_language),
                )
            assert model is not None
            samples = pad_parakeet_audio(
                np.asarray(audio, dtype=np.float32).reshape(-1),
                samplerate or 16000,
            )
            started = time.perf_counter()
            raw = model.recognize(samples, sample_rate=int(samplerate) if samplerate else 16000)
            text = _as_text(raw)
            duration_s = len(np.asarray(audio).reshape(-1)) / float(samplerate or 16000)
            quality = sanitize_transcript(text, duration_s=duration_s)
            latency = time.perf_counter() - started
            self._last_decode_s = latency
            return TranscriptResult(
                text=quality.text if not quality.rejected_reason else "",
                # V3 decodes multilingual speech automatically. A concrete caller
                # hint is validated metadata (onnx-asr exposes no decoder bias);
                # auto returns unknown rather than inventing a detected language.
                language=(
                    ""
                    if requested_language == ""
                    else requested_language or ("" if self.language == "auto" else self.language)
                ),
                duration_s=duration_s,
                asr_latency_s=latency,
                rejected_reason=quality.rejected_reason,
                chars_per_s=quality.chars_per_s,
            )
        finally:
            self.arm_idle_timer(self.unload)


def pad_parakeet_audio(samples: np.ndarray, samplerate: int) -> np.ndarray:
    """Pad short utterances so Parakeet TDT does not emit empty text."""
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    sr = int(samplerate) or 16000
    need = int(_MIN_AUDIO_S * sr)
    if len(audio) >= need:
        return audio
    lead = int(_LEAD_PAD_S * sr)
    tail = max(need - len(audio) - lead, 0)
    return np.concatenate(
        [
            np.zeros(lead, dtype=np.float32),
            audio,
            np.zeros(tail, dtype=np.float32),
        ]
    )


def _assert_offline_local_dir(local_dir: Path) -> None:
    """Fail closed if the snapshot directory is not a real, present directory.

    ``onnx_asr`` decides between "load these files" and "download this repo from
    Hugging Face" purely on whether the ``path`` it was given exists. A snapshot
    that disappeared between verification and load (unmounted drive, OneDrive
    dehydration, antivirus quarantine) would therefore turn an offline-by-design
    app into one that reaches for the network. Assert it here instead.
    """
    if not local_dir.is_dir():
        raise ModelUnavailableError(
            f"Parakeet snapshot directory disappeared before load: {local_dir}. "
            "Refusing to fall back to a Hugging Face download; reinstall or "
            "re-download the model."
        )
    if os.environ.get("HF_HUB_OFFLINE") != "1" and not os.environ.get("DCENT_VOICE_ALLOW_HUB"):
        # bootlog sets this at process start; if it is missing we are running
        # inside an embedding that bypassed the entry point.
        logger.warning(
            "HF_HUB_OFFLINE is not set; import dcent_voice.util.bootlog before "
            "loading models to guarantee the offline posture"
        )


def _quantization(compute_type: str | None) -> str | None:
    value = (compute_type or "").strip().lower()
    if not value:
        return "int8"
    if value.startswith("cpu-"):
        value = value[4:]
    if value in {"int8", "int8_float16", "int8_float32"}:
        return "int8"
    if value in {"fp32", "float32", "default"}:
        return None
    return value


def _cpu_session_options() -> Any:
    """Favor bounded cold-start and decode latency without changing graph semantics."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    # ORT's CPU weight prepacking costs roughly one second for the 0.6B
    # encoder on each post-idle reload on the supported Windows CPU path. The
    # measured short and corpus decodes do not benefit, so avoid that blocking
    # startup work and keep the same graph optimizations and exact weights.
    options.add_session_config_entry("session.disable_prepacking", "1")
    # ORT otherwise sizes its pool from the whole host, which can oversubscribe
    # desktop CPUs and produce unstable tail latency when another local task is
    # active. Four workers was the best measured balance on the validated 6C/12T
    # CPU; smaller hosts are capped to their reported logical CPU count.
    options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
    options.inter_op_num_threads = 1
    return options


def _as_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    text = getattr(raw, "text", None)
    if isinstance(text, str):
        return text.strip()
    if isinstance(raw, list | tuple):
        return " ".join(_as_text(item) for item in raw).strip()
    return str(raw).strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == "--stage":
        dest = stage_parakeet_bundle(Path(args[1]))
        print(dest)
        return 0
    print("usage: python -m dcent_voice.asr.parakeet_provider --stage DEST", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
