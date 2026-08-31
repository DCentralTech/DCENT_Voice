# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
import threading
import time

import numpy as np
import pytest

from dcent_voice.asr.faster_whisper_provider import (
    FasterWhisperASRProvider,
    resample_to_16k,
    resolve_device_compute,
)
from dcent_voice.config import ASRSpec


def test_int8_spec_defaults_to_cpu() -> None:
    assert resolve_device_compute(ASRSpec.parse("faster-whisper:tiny:int8")) == ("cpu", "int8")
    assert resolve_device_compute(ASRSpec.parse("faster-whisper:tiny:cpu-int8")) == ("cpu", "int8")
    assert resolve_device_compute(ASRSpec.parse("faster-whisper:base.en:cpu-int8")) == (
        "cpu",
        "int8",
    )


def test_bench_load_audio_defaults_to_speech_fixture_not_silence() -> None:
    """Honest latency gates must not score silence (VAD-skipped sub-ms fakes)."""
    from dcent_voice.bench_latency import load_audio, resample_to_16k

    audio, sample_rate, source = load_audio(None)
    assert "hello.wav" in source.replace("\\", "/")
    assert audio.size > 0
    assert sample_rate > 0
    # Real speech fixture has energy; pure silence would be all zeros.
    assert float(abs(audio).max()) > 0.01
    # Bench path resamples non-16 kHz fixtures before decode.
    if sample_rate != 16000:
        audio = resample_to_16k(audio, sample_rate)
    assert audio.size >= 16000  # at least ~1 s of speech at 16 kHz


def test_auto_prefers_cpu_when_cuda_runtime_not_ready(monkeypatch) -> None:
    from dcent_voice.asr import faster_whisper_provider as fw

    monkeypatch.setattr(fw, "cuda_runtime_ready", lambda: False)
    assert resolve_device_compute(ASRSpec.parse("faster-whisper:distil-small.en")) == (
        "cpu",
        "int8",
    )


def test_auto_prefers_cuda_when_runtime_ready(monkeypatch) -> None:
    from dcent_voice.asr import faster_whisper_provider as fw

    monkeypatch.setattr(fw, "cuda_runtime_ready", lambda: True)
    assert resolve_device_compute(ASRSpec.parse("faster-whisper:distil-small.en")) == (
        "cuda",
        "float16",
    )


def test_explicit_cpu_suffix_never_selects_cuda(monkeypatch) -> None:
    from dcent_voice.asr import faster_whisper_provider as fw

    # Even if a full GPU stack is present, :cpu-int8 must stay on CPU.
    monkeypatch.setattr(fw, "cuda_runtime_ready", lambda: True)
    assert resolve_device_compute(ASRSpec.parse("faster-whisper:distil-small.en:cpu-int8")) == (
        "cpu",
        "int8",
    )


def test_transcribe_falls_back_to_cpu_on_cuda_inference_error() -> None:
    # Regression: cublas64_12.dll (and other CUDA libs) can be missing at
    # inference time even when load() succeeded, which surfaced as an HTTP 500.
    # The provider must degrade to CPU int8 and retry rather than raise.
    # Force an explicit CUDA request so the test still exercises fallback when
    # the host would otherwise resolve auto → cpu (incomplete CUDA stack).
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:large-v3:cuda-float16"))

    class _Seg:
        def __init__(self, text: str) -> None:
            self.text = text
            self.no_speech_prob = 0.1
            self.avg_logprob = -0.2
            self.compression_ratio = 1.2

    class _Info:
        language = "en"

    class _CudaModel:
        def transcribe(self, *args: object, **kwargs: object):
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")

    class _CpuModel:
        def transcribe(self, *args: object, **kwargs: object):
            return [_Seg(" hello ")], _Info()

    def fake_load(device: str, compute_type: str):
        return _CpuModel() if device == "cpu" else _CudaModel()

    provider._load_model = fake_load  # type: ignore[assignment]

    result = provider.transcribe(np.zeros(1600, dtype=np.float32), samplerate=16000)

    assert result.text == "hello"
    assert provider.runtime == ("cpu", "int8")
    status = provider.runtime_status()
    assert status["requested_device"] == "cuda"
    assert status["actual_device"] == "cpu"
    assert status["compute_type"] == "int8"
    assert status["model_loaded"] is True
    assert status["last_load_s"] is not None
    assert status["last_decode_s"] is not None
    assert "cublas" in str(status["fallback_reason"]).lower()


def test_transcribe_uses_anti_hallucination_kwargs() -> None:
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny:int8"))
    seen: dict[str, object] = {}

    class _Seg:
        text = "hello world"
        no_speech_prob = 0.05
        avg_logprob = -0.2
        compression_ratio = 1.1

    class _Info:
        language = "en"

    class _Model:
        def transcribe(self, *args: object, **kwargs: object):
            seen.update(kwargs)
            return [_Seg()], _Info()

    provider._load_model = lambda device, compute_type: _Model()  # type: ignore[assignment]
    # ~1s of audio so density of "hello world" is fine.
    audio = np.zeros(16000, dtype=np.float32)
    result = provider.transcribe(audio, samplerate=16000)
    assert result.text == "hello world"
    assert seen.get("condition_on_previous_text") is False
    assert seen.get("temperature") == 0.0
    assert seen.get("no_repeat_ngram_size") == 3
    assert seen.get("without_timestamps") is True


def test_multilingual_per_call_language_is_immutable_and_auto_detects() -> None:
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny:int8"), language="en")
    observed: list[object] = []

    class _Seg:
        text = "bonjour"
        no_speech_prob = 0.05
        avg_logprob = -0.2
        compression_ratio = 1.1

    class _Info:
        language = "fr"

    class _Model:
        def transcribe(self, *args: object, **kwargs: object):
            observed.append(kwargs.get("language"))
            return [_Seg()], _Info()

    provider._load_model = lambda device, compute_type: _Model()  # type: ignore[assignment]
    provider.transcribe(np.zeros(16000, dtype=np.float32), language="FR")
    provider.transcribe(np.zeros(16000, dtype=np.float32), language="auto")

    assert observed == ["fr", None]
    assert provider.language == "en"


def test_english_only_faster_whisper_rejects_non_english_before_load() -> None:
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny.en:int8"), language="en")
    loads = 0

    def load_model(device: str, compute_type: str):
        nonlocal loads
        del device, compute_type
        loads += 1
        raise AssertionError("must reject before model load")

    provider._load_model = load_model  # type: ignore[assignment]
    with pytest.raises(ValueError, match="supports only English"):
        provider.transcribe(np.zeros(16000, dtype=np.float32), language="fr")
    assert loads == 0


def test_model_constructor_is_defense_in_depth_local_files_only(tmp_path, monkeypatch) -> None:
    import dcent_voice.asr.faster_whisper_provider as provider_module

    snapshot = tmp_path / "verified"
    snapshot.mkdir()
    seen: dict[str, object] = {}

    class FakeWhisperModel:
        def __init__(self, model: str, **kwargs: object) -> None:
            seen["model"] = model
            seen.update(kwargs)

    monkeypatch.setattr(
        provider_module,
        "resolve_faster_whisper_model",
        lambda _model: str(snapshot),
    )
    from contextlib import nullcontext

    monkeypatch.setattr(
        provider_module, "verified_snapshot_lock", lambda path, _id: nullcontext(path)
    )
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        type("FasterWhisperModule", (), {"WhisperModel": FakeWhisperModel}),
    )
    provider = FasterWhisperASRProvider(
        ASRSpec.parse("faster-whisper:base:cpu-int8"), language="fr"
    )
    provider._load_model("cpu", "int8")
    assert seen["model"] == str(snapshot)
    assert seen["local_files_only"] is True


@pytest.mark.parametrize("automatic", ["", "auto", "detect"])
def test_english_only_faster_whisper_rejects_auto_before_load(
    automatic: str,
) -> None:
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny.en:int8"), language="en")
    with pytest.raises(ValueError, match="automatic language detection"):
        provider.transcribe(np.zeros(16, dtype=np.float32), language=automatic)
    assert provider._model is None


def test_transcribe_drops_no_speech_segments() -> None:
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny:int8"))

    class _Seg:
        def __init__(self, text: str, no_speech: float) -> None:
            self.text = text
            self.no_speech_prob = no_speech
            self.avg_logprob = -0.2
            self.compression_ratio = 1.1

    class _Info:
        language = "en"

    class _Model:
        def transcribe(self, *args: object, **kwargs: object):
            return [_Seg("Thanks for watching", 0.95), _Seg("real words", 0.1)], _Info()

    provider._load_model = lambda device, compute_type: _Model()  # type: ignore[assignment]
    result = provider.transcribe(np.zeros(16000, dtype=np.float32), samplerate=16000)
    assert result.text == "real words"


def test_transcribe_rejects_loop_density() -> None:
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny:int8"))

    class _Seg:
        text = ("should be able to " * 40).strip()
        no_speech_prob = 0.1
        avg_logprob = -0.2
        compression_ratio = 1.5

    class _Info:
        language = "en"

    class _Model:
        def transcribe(self, *args: object, **kwargs: object):
            return [_Seg()], _Info()

    provider._load_model = lambda device, compute_type: _Model()  # type: ignore[assignment]
    # 5s of pure loop text must hard-reject (no remnant inject).
    result = provider.transcribe(np.zeros(16000 * 5, dtype=np.float32), samplerate=16000)
    assert result.rejected_reason == "asr_hallucination"
    assert result.text == ""


def test_transcribe_retries_without_hotwords_on_hint_echo() -> None:
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:base:int8"))
    observed: list[object] = []

    class _Seg:
        no_speech_prob = 0.05
        avg_logprob = -0.2
        compression_ratio = 1.1

        def __init__(self, text: str) -> None:
            self.text = text

    class _Info:
        language = "en"

    class _Model:
        def transcribe(self, *args: object, **kwargs: object):
            del args
            hotwords = kwargs.get("hotwords")
            observed.append(hotwords)
            text = (
                "D-Central Technologies d central technologies"
                if hotwords
                else "A golden fortune and a happy life."
            )
            return [_Seg(text)], _Info()

    provider._load_model = lambda device, compute_type: _Model()  # type: ignore[assignment]
    result = provider.transcribe(
        np.zeros(16000 * 3, dtype=np.float32),
        samplerate=16000,
        initial_prompt="D-Central, Lightning Network.",
        hotwords="D-Central d central technologies Lightning Network",
    )
    assert result.text == "A golden fortune and a happy life."
    assert result.rejected_reason == ""
    assert observed == ["D-Central d central technologies Lightning Network", None]


def test_idle_timer_unloads_and_transcribe_reloads() -> None:
    provider = FasterWhisperASRProvider(
        ASRSpec.parse("faster-whisper:tiny:int8"), idle_unload_s=0.05
    )
    loads = {"count": 0}

    class _Seg:
        text = "hi"
        no_speech_prob = 0.1
        avg_logprob = -0.2
        compression_ratio = 1.1

    class _Info:
        language = "en"

    class _Model:
        def transcribe(self, *args: object, **kwargs: object):
            return [_Seg()], _Info()

    def fake_load(device: str, compute_type: str):
        loads["count"] += 1
        return _Model()

    provider._load_model = fake_load  # type: ignore[assignment]
    audio = np.zeros(1600, dtype=np.float32)

    assert provider.transcribe(audio, samplerate=16000).text == "hi"
    assert provider.loaded
    deadline = time.time() + 2.0
    while provider.loaded and time.time() < deadline:
        time.sleep(0.01)
    assert not provider.loaded  # idle timer released the model

    # Next utterance transparently reloads.
    assert provider.transcribe(audio, samplerate=16000).text == "hi"
    assert loads["count"] == 2
    provider.unload()  # cancel the re-armed timer for a clean test exit


def test_concurrent_transcribes_serialize() -> None:
    # CTranslate2 models are not safe for concurrent transcribe(); a straggling
    # streaming pass must never overlap the finalize decode.
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny:int8"))
    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    class _Seg:
        text = "ok"
        no_speech_prob = 0.1
        avg_logprob = -0.2
        compression_ratio = 1.1

    class _Info:
        language = "en"

    class _Model:
        def transcribe(self, *args: object, **kwargs: object):
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.05)
            with guard:
                active["now"] -= 1
            return [_Seg()], _Info()

    provider._load_model = lambda device, compute_type: _Model()  # type: ignore[assignment]
    audio = np.zeros(1600, dtype=np.float32)

    threads = [
        threading.Thread(target=provider.transcribe, args=(audio,), kwargs={"samplerate": 16000})
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert active["max"] == 1


def test_unload_during_decode_cannot_null_active_model() -> None:
    # An idle timer that fired just before transcribe() re-locked can still run
    # unload(); the in-flight decode must keep its own strong model reference.
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny:int8"))

    class _Seg:
        text = "survived"
        no_speech_prob = 0.1
        avg_logprob = -0.2
        compression_ratio = 1.1

    class _Info:
        language = "en"

    class _Model:
        def __init__(self, owner: FasterWhisperASRProvider) -> None:
            self.owner = owner

        def transcribe(self, *args: object, **kwargs: object):
            # Simulate the fired idle timer nulling the provider's model slot
            # while this decode is in flight.
            self.owner.unload()
            return [_Seg()], _Info()

    provider._load_model = lambda device, compute_type: _Model(provider)  # type: ignore[assignment]

    result = provider.transcribe(np.zeros(16000, dtype=np.float32), samplerate=16000)

    assert result.text == "survived"
    provider.unload()


def test_cuda_prefix_forces_cuda() -> None:
    assert resolve_device_compute(ASRSpec.parse("faster-whisper:large-v3:cuda-float16")) == (
        "cuda",
        "float16",
    )


def test_windows_cuda_dll_probe_does_not_stat_every_path_entry(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from dcent_voice.asr.faster_whisper_provider import _windows_cuda_dlls_present

    for index in range(32):
        (tmp_path / f"unrelated-{index}.dll").write_bytes(b"")
    (tmp_path / "cudnn64_8.dll").write_bytes(b"")
    (tmp_path / "cublas64_12.dll").write_bytes(b"")
    monkeypatch.setenv("PATH", str(tmp_path))

    original_is_file = Path.is_file

    def reject_path_is_file(path: Path) -> bool:
        raise AssertionError(f"probe performed a separate stat for {path.name}")

    monkeypatch.setattr(Path, "is_file", reject_path_is_file)
    try:
        assert _windows_cuda_dlls_present() is True
    finally:
        monkeypatch.setattr(Path, "is_file", original_is_file)


def test_resample_to_16k_changes_length() -> None:
    audio = np.zeros(22050, dtype=np.float32)

    resampled = resample_to_16k(audio, 22050)

    assert len(resampled) == 16000
