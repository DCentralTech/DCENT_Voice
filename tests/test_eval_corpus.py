# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dcent_voice.eval_corpus import load_corpus, word_error_rate


def test_corpus_includes_hello_fixture() -> None:
    items = {item.id: item for item in load_corpus()}
    assert "hello" in items
    assert items["hello"].audio is not None
    assert items["hello"].audio.is_file()
    assert items["hello"].reference.lower().startswith("hello")
    spoken = [item for item in items.values() if item.audio is not None]
    assert len(spoken) >= 10
    for item in spoken:
        assert item.audio.is_file()


def test_wer_is_zero_for_identical_text() -> None:
    assert word_error_rate("Hello world", "Hello world.") == 0.0
    assert word_error_rate("Hello world", "Goodbye") > 0.5


def test_corpus_includes_public_librispeech_audio() -> None:
    items = {item.id: item for item in load_corpus()}
    public = [item for item in items.values() if "librispeech" in item.tags]
    assert len(public) >= 14
    speakers = {item.audio.stem.split("-")[0] for item in public if item.audio}
    assert len(speakers) >= 8
    assert any("female" in item.tags for item in public)
    assert any("male" in item.tags for item in public)
    noisy = [item for item in public if "noisy" in item.tags]
    assert len(noisy) >= 6
    for item in noisy:
        assert item.audio is not None and item.audio.is_file()
    long_items = [item for item in public if "long" in item.tags]
    assert len(long_items) >= 3
    for item in public:
        assert item.audio is not None
        assert item.audio.is_file()
        assert item.audio.stat().st_size > 1000


def test_multilingual_smoke_corpus_uses_verified_real_public_speech() -> None:
    items = load_corpus(Path("eval/multilingual.json"))
    by_id = {item.id: item for item in items}
    assert len(items) >= 3
    langs = {item.language for item in items}
    assert {"en", "fr", "de"} <= langs or {"en", "fr", "es"} <= langs
    assert len(langs) >= 3
    fr = by_id["lingua-libre-fr-je-mappelle"]
    assert fr.language == "fr"
    assert {"public", "real-speech", "multilingual-smoke"} <= set(fr.tags)
    assert fr.audio is not None and fr.audio.is_file()
    assert hashlib.sha256(fr.audio.read_bytes()).hexdigest() == (
        "168fa81af78b34da34adc79fd9b29b4d4d9c7478ad60ee4c0bed8498555bafda"
    )
    de = by_id["lingua-libre-de-hallo"]
    assert de.language == "de"
    assert hashlib.sha256(de.audio.read_bytes()).hexdigest() == (
        "3657db7aad9c38cd3d998458acfc84c797ae298c204d433d5d5cfdf551a4a24c"
    )
    es = by_id["lingua-libre-es-hola"]
    assert es.language == "es"
    assert hashlib.sha256(es.audio.read_bytes()).hexdigest() == (
        "ecad3025298576f5c13d3077b776682f8292a8a00f1b99b91bfe79771237d829"
    )
    for item in items:
        assert item.audio is not None and item.audio.is_file()
        assert item.audio.stat().st_size > 1000


def test_corpus_spoken_audio_is_not_silence_and_hello_is_not_special() -> None:
    from dcent_voice.eval_corpus import speech_rms
    from scripts import eval_dictation

    items = {item.id: item for item in load_corpus()}
    assert items["silence"].audio is None
    assert items["silence"].synthetic == "silence_2s"
    assert "silence" not in (items["hello"].tags)
    spoken = [item for item in items.values() if item.audio is not None]
    assert len(spoken) >= 40
    for item in spoken:
        assert item.audio is not None
        assert item.audio.stat().st_size > 1000
        assert speech_rms(item.audio) >= 0.01
        assert item.reference.strip(), f"{item.id} has empty reference"
    source = Path(eval_dictation.__file__).read_text(encoding="utf-8")
    assert "hello.wav" not in source
    assert 'if item.id == "hello"' not in source


def test_corpus_includes_public_multilingual_and_english_pronunciations() -> None:
    items = {item.id: item for item in load_corpus()}
    fr = items["lingua-libre-fr-je-mappelle"]
    assert fr.language == "fr"
    assert fr.audio is not None
    assert hashlib.sha256(fr.audio.read_bytes()).hexdigest() == (
        "168fa81af78b34da34adc79fd9b29b4d4d9c7478ad60ee4c0bed8498555bafda"
    )
    assert items["lingua-libre-de-hallo"].language == "de"
    assert items["lingua-libre-es-hola"].language == "es"
    above = items["ll-en-above-all"]
    assert above.audio is not None
    assert hashlib.sha256(above.audio.read_bytes()).hexdigest() == (
        "bdd673e97c5bbc478121454c0cacbb5518ed8484254797166c4174373af40c4d"
    )
    pollution = items["ll-en-air-pollution"]
    assert pollution.audio is not None
    assert hashlib.sha256(pollution.audio.read_bytes()).hexdigest() == (
        "0f6ef569857f1e5313e7edd251a828cd65de0033e960d27d7ce266a24e41df6b"
    )


def test_code_switch_items_are_explicit_deterministic_public_composites() -> None:
    from dcent_voice.eval_corpus import concatenate_wav_segments, wav_duration_s

    items = {item.id: item for item in load_corpus()}
    switches = [item for item in items.values() if "code-switch" in item.tags]
    assert {item.id for item in switches} == {
        "code-switch-en-fr-composite",
        "code-switch-fr-en-composite",
    }
    for item in switches:
        assert item.audio is None
        assert item.synthetic == "concatenate_wavs"
        assert item.language == "auto"
        assert len(item.segments) == 2
        assert {"public-components", "real-speech-components", "synthetic-composite"} <= set(
            item.tags
        )
        audio, samplerate = concatenate_wav_segments(item.segments)
        expected_frames = sum(
            round(wav_duration_s(path) * samplerate) for path in item.segments
        ) + round(0.25 * samplerate)
        assert samplerate == 16000
        assert len(audio) == expected_frames
        assert float((audio**2).mean() ** 0.5) >= 0.01

    dedicated = load_corpus(Path("eval/code_switch.json"))
    assert [item.id for item in dedicated] == [item.id for item in switches]


def test_summarize_separates_asr_wer_from_text_polish() -> None:
    from dcent_voice.eval_corpus import ItemScore, summarize

    scores = (
        ItemScore("a", "hello", "hello", 0.0, 0.0, True, "asr", ("en",), 0.1),
        ItemScore("b", "world", "wrong", 1.0, 0.8, False, "asr", ("librispeech",), 0.2),
        ItemScore("c", "polish", "polish", 0.0, 0.0, True, "text", ("text",), None),
    )
    report = summarize(scores)
    assert report["audio_items"] == 2
    assert report["text_items"] == 1
    assert report["asr_wer_mean"] == 0.5
    assert report["public_asr_items"] == 1
    assert report["public_asr_wer_mean"] == 1.0
    assert report["text_wer_mean"] == 0.0
    assert report["wer_mean"] == pytest.approx(1.0 / 3)
    assert report["noisy_asr_wer_mean"] is None
    assert report["long_asr_wer_mean"] is None
    assert report["asr_wer_micro"] == 0.5
    assert report["asr_word_edits"] == 1
    assert report["asr_reference_words"] == 2
    assert report["code_switch_asr_items"] == 0
    assert report["code_switch_asr_wer_mean"] is None


def test_mix_eval_awgn_refuses_silence(tmp_path: Path) -> None:
    import wave

    import numpy as np

    from scripts.mix_eval_awgn import mix_awgn

    silent = np.zeros(1600, dtype=np.float64)
    with pytest.raises(ValueError, match="silence"):
        mix_awgn(silent, snr_db=8.0, seed=1)
    speech = np.linspace(-0.2, 0.2, 1600, dtype=np.float64)
    mixed = mix_awgn(speech, snr_db=8.0, seed=1)
    assert float(np.sqrt(np.mean(np.square(mixed)))) >= 0.01
    dest = tmp_path / "ok.wav"
    pcm = np.round(np.clip(speech, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(dest), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm.tobytes())
    from dcent_voice.eval_corpus import speech_rms

    assert speech_rms(dest) >= 0.01


def test_eval_dictation_refuses_tiny_standin() -> None:
    from scripts.eval_dictation import require_shipped_asr

    with pytest.raises(ValueError, match="tiny"):
        require_shipped_asr("faster-whisper:tiny.en:cpu-int8")
    with pytest.raises(ValueError, match="distil"):
        require_shipped_asr("faster-whisper:distil-small.en:cpu-int8")
    require_shipped_asr("parakeet:tdt-0.6b-v3:int8")


def test_eval_dictation_atomically_persists_scoped_report(tmp_path: Path) -> None:
    import json

    from scripts import eval_dictation

    output = tmp_path / "nested" / "dictation.json"
    assert eval_dictation.main(["--skip-asr", "--output-json", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema"] == "dcent-dictation-eval-result-v1"
    assert report["scope"] == "offline_file_corpus_no_microphone_no_injection"
    assert report["summary"]["audio_items"] == 0
    assert not list(output.parent.glob("*.tmp"))


def test_eval_writing_atomically_persists_scoped_report(tmp_path: Path) -> None:
    import json

    from scripts import eval_writing

    corpus = tmp_path / "writing.json"
    corpus.write_text(
        json.dumps({"items": [{"id": "plain", "input": "Hello", "exact": "Hello"}]}),
        encoding="utf-8",
    )
    output = tmp_path / "nested" / "writing-result.json"
    assert eval_writing.main(["--corpus", str(corpus), "--output-json", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema"] == "dcent-writing-eval-result-v1"
    assert report["scope"] == "offline_text_writing_no_asr_no_microphone_no_injection"
    assert report["passed"] == 1
    assert not list(output.parent.glob("*.tmp"))
