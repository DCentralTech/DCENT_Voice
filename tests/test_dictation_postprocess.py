# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import re
from pathlib import Path

import pytest

from dcent_voice.config import SnippetEntry, VocabEntry
from dcent_voice.dictation.postprocess import (
    apply_dictation_postprocess,
    apply_snippets,
    apply_spoken_edits,
    apply_spoken_tokens,
    compose_dictation,
    extract_spoken_corrections,
    is_undo_last_command,
    local_polish,
    peel_spoken_cleanup,
    peel_spoken_press_enter,
)
from tests.frozen_artifacts import require_isolatable_frozen_exe
from tests.win32_native import requires_win32_native


def test_spoken_punctuation_and_newlines() -> None:
    text = apply_spoken_tokens("hello comma world full stop new line next item")
    compact = re.sub(r"\s+", " ", text)
    assert "hello, world." in compact
    assert "\n" in text
    para = apply_spoken_tokens("hello new paragraph world")
    assert para.replace("\r\n", "\n") == "hello\n\nworld"


def test_spoken_tom_l_becomes_toml_not_tom_left() -> None:
    text = apply_dictation_postprocess("Save it as config example tom l.")
    assert "example.toml" in text
    assert apply_dictation_postprocess("Tom left the room.") == "Tom left the room."


def test_run_get_status_becomes_git() -> None:
    text = apply_dictation_postprocess("Run get status then cargo test.")
    assert "git status" in text.lower()
    assert "get status" not in text.lower()
    # Ordinary English is left alone.
    plain = apply_dictation_postprocess("Can you get status of the shipment.")
    assert "get status of the shipment" in plain.lower()


def test_new_sentence_and_dev_spoken_forms() -> None:
    text = apply_spoken_tokens("first thought new sentence second thought")
    assert ". " in text or text.count(".") >= 1
    dev = apply_spoken_tokens("open main dot py in vs code")
    assert ".py" in dev
    assert "VS Code" in dev


def test_period_of_growth_not_mangled() -> None:
    text = apply_spoken_tokens("during this period of growth")
    assert "period of growth" in text.lower()
    assert "this. of" not in text


def test_bullet_list_formatting() -> None:
    text = apply_spoken_tokens("shopping list bullet point milk next bullet eggs")
    assert re.search(r"\n-\s*milk", text)
    assert re.search(r"\n-\s*eggs", text)
    composed = compose_dictation("Shopping list bullet point milk next bullet rice")
    folded = composed.replace("\r\n", "\n")
    assert re.search(r"\n-\s*milk", folded)
    assert re.search(r"\n-\s*rice", folded)
    assert "bullet point" not in folded.lower()
    assert "next bullet" not in folded.lower()
    assert "\n\n" not in folded
    assert folded == "Shopping list.\n- milk\n- rice"


def test_scratch_that_removes_prior_clause() -> None:
    text = apply_spoken_edits("Buy milk, buy bread scratch that")
    assert "bread" not in text.lower()
    assert "milk" in text.lower()


def test_scratch_that_whole_utterance_clears() -> None:
    assert apply_spoken_edits("this was a mistake scratch that") == ""


def test_is_undo_last_command_only_matches_whole_utterance() -> None:
    assert is_undo_last_command("scratch that")
    assert is_undo_last_command("Undo that.")
    assert is_undo_last_command("um undo last")
    assert is_undo_last_command("please scratch that")
    assert not is_undo_last_command("this was a mistake scratch that")
    assert not is_undo_last_command("hello world")
    assert not is_undo_last_command("undo")


def test_replace_with_and_i_meant_corrections() -> None:
    assert (
        apply_spoken_edits("ship the beta tomorrow replace tomorrow with Friday")
        == "ship the beta Friday"
    )
    text = apply_spoken_edits("call Alice no I meant Bob")
    assert text == "call Bob"
    assert "Alice" not in text
    know = apply_spoken_edits("Meet Bob know I meant Alice")
    assert know == "Meet Alice"
    assert "Bob" not in know
    assert compose_dictation("Meet Bob know I meant Alice") == "Meet Alice."


def test_scratch_that_drops_trailing_phrase_not_entire_thought() -> None:
    text = apply_spoken_edits("send the invite to the team thanks scratch that")
    assert "invite" in text.lower()
    assert "thanks" not in text.lower()
    # Three trailing words removed; earlier content remains.
    assert "team" not in text.lower()


def test_delete_last_word() -> None:
    assert apply_spoken_edits("one two three delete last word") == "one two"
    assert compose_dictation("Ship Monday Tuesday delete last word") == "Ship Monday."


def test_delete_last_sentence() -> None:
    text = apply_spoken_edits("First sentence. Second sentence. delete last sentence")
    assert "First sentence" in text
    assert "Second" not in text
    punct = apply_spoken_edits("The meeting is Monday. Ship Tuesday. Delete last sentence.")
    assert punct == "The meeting is Monday."
    assert "Tuesday" not in punct
    assert (
        compose_dictation("The meeting is Monday. Ship Tuesday. Delete last sentence.")
        == "The meeting is Monday."
    )


def test_new_line_token_stays_single_break() -> None:
    assert compose_dictation("Alpha report new line Bravo draft") == ("Alpha report.\nBravo draft.")
    titled = compose_dictation("Alpha Report New Line Bravo Draft")
    assert titled == "Alpha Report.\nBravo Draft."
    folded = titled.replace("\r\n", "\n")
    assert "\n" in folded
    assert "\n\n" not in folded
    para = compose_dictation("Hello new paragraph world")
    assert "\n\n" in para.replace("\r\n", "\n")


def test_delete_last_line() -> None:
    assert apply_spoken_edits("Keep the intro\ndrop the outro delete last line") == (
        "Keep the intro"
    )
    punct = apply_spoken_edits("Keep the intro\ndrop the outro delete last line.")
    assert punct == "Keep the intro"
    assert "outro" not in punct.lower()
    assert (
        compose_dictation("Keep the intro new line drop the outro delete last line.")
        == "Keep the intro."
    )


def test_empty_snippet_expansion_does_not_eat_the_cue() -> None:
    snippets = (SnippetEntry(spoken="my email", expansion=""),)
    text = apply_snippets("send it to my email please", snippets)
    assert "my email" in text.lower()


def test_snippets_starred_wins_when_cues_conflict() -> None:
    snippets = (
        SnippetEntry(spoken="my email", expansion="plain@example.com"),
        SnippetEntry(spoken="my email", expansion="star@example.com", starred=True),
    )
    text = apply_snippets("send my email please", snippets)
    assert text == "send star@example.com please"


def test_snippets_expand_case_insensitive() -> None:
    snippets = (SnippetEntry(spoken="my calendar", expansion="https://cal.example/me"),)
    text = apply_snippets("Book time via my calendar please", snippets)
    assert "https://cal.example/me" in text
    assert "my calendar" not in text.lower()


def test_local_polish_removes_fillers_and_capitalizes() -> None:
    text = local_polish("um hello world uh this is a test")
    assert "um" not in text.lower()
    assert text.startswith("Hello")
    assert text.endswith(".")


def test_local_polish_capitalizes_lonely_i() -> None:
    text = local_polish("i think i can ship this")
    assert text.startswith("I ")
    assert " i " not in text


def test_local_polish_preserves_bullets_without_forced_period() -> None:
    text = local_polish("- milk\n- eggs")
    assert text == "- milk\n- eggs"


def test_spoken_email_at_becomes_address() -> None:
    text = apply_dictation_postprocess("Email ada at d-central.tech")
    assert "ada@d-central.tech" in text.lower()
    glued = apply_dictation_postprocess("mail ada@dcentral.tech")
    assert "d-central.tech" in glued.lower()


def test_grouped_digits_collapse_to_ports_and_ids() -> None:
    assert "8765" in local_polish("The port is 8, 765")
    assert "8765" in apply_dictation_postprocess("The port is 8,765")


def test_urls_keep_scheme_slash_glue() -> None:
    text = apply_dictation_postprocess("visit https://d-central.tech please")
    assert "https://d-central.tech" in text
    assert "https: //" not in text


def test_extract_spoken_corrections_from_replace_and_meant() -> None:
    pairs = extract_spoken_corrections("use d sent no I meant DCENT_Voice")
    assert pairs
    assert pairs[0][1] == "DCENT_Voice"
    replaced = extract_spoken_corrections("hello foo replace foo with bar")
    assert ("foo", "bar") in replaced
    remembered = extract_spoken_corrections("remember d sent as DCENT_Voice")
    assert ("d sent", "DCENT_Voice") in remembered
    from dcent_voice.dictation.postprocess import extract_last_correction

    assert extract_last_correction("correct that to DCENT_Voice") == "DCENT_Voice"
    assert extract_last_correction("hello world") is None


def test_developer_file_extension_tokens() -> None:
    text = apply_spoken_tokens("open app dot py and config dot json", include_dev=True)
    assert "app.py" in text
    assert "config.json" in text


def test_full_postprocess_pipeline_order() -> None:
    snippets = (SnippetEntry(spoken="sig block", expansion="— Alice\nEngineer"),)
    text = apply_dictation_postprocess(
        "um write hello world full stop new line sig block",
        snippets=snippets,
        polish=True,
        spoken_edits=True,
        developer_terms=True,
    )
    assert "hello world." in text.lower()
    assert "— Alice" in text
    assert "um" not in text.lower()
    assert text[0].isupper()


def test_local_polish_keeps_snippet_expansions() -> None:
    snippets = (SnippetEntry(spoken="my email", expansion="um ops@example.com"),)
    text = compose_dictation("um send my email", snippets=snippets)
    assert "um ops@example.com" in text
    assert not text.lower().startswith("um ")


def test_local_polish_keeps_dictionary_written_forms() -> None:
    dictionary = (VocabEntry(spoken="vip", written="um VIP Corp"),)
    text = compose_dictation("um say um VIP Corp", dictionary=dictionary)
    assert "um VIP Corp" in text
    assert not text.lower().startswith("um ")


def test_writing_style_keeps_snippet_expansions() -> None:
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    text = compose_dictation("sig", snippets=snippets, style="formal")
    assert "I'm Ada" in text
    assert "I am Ada" not in text


def test_writing_style_still_expands_contractions_outside_snippets() -> None:
    text = compose_dictation("I'm here", style="formal")
    assert "I am here" in text


def test_ade_compose_keeps_snippet_expansions(fake_asr) -> None:
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "um send my email"
    snippets = (SnippetEntry(spoken="my email", expansion="um ops@example.com"),)
    engine = ServiceEngine(asr=fake_asr, snippets=snippets)
    body = engine.transcribe(TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True))
    assert "um ops@example.com" in body["cleaned"]
    assert body["raw"] == "um send my email"


def test_ade_writing_style_keeps_snippet_expansions(fake_asr) -> None:
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, snippets=snippets)
    body = engine.transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True, style="formal")
    )
    assert "I'm Ada" in body["cleaned"]
    assert "I am Ada" not in body["cleaned"]
    assert body["raw"] == "sig"
    control = ServiceEngine(asr=fake_asr).transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True, style="formal")
    )
    assert "I'm Ada" not in control["cleaned"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_cli_writing_style_keeps_snippet_expansions(capsys) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from dcent_voice.app import run_compose_command
    from dcent_voice.dictation.style import apply_style

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    args = Namespace(text=["sig"], style="formal", cleanup_level="medium")
    config = SimpleNamespace(snippets=snippets, dictionary=())
    assert run_compose_command(config, args) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    assert run_compose_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_transcribe_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store)
    audio = np.zeros(1600, dtype=np.float32)
    result = engine.transcribe(audio, polish=False, style="formal")
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "vip"
    control = VoiceEngine(
        replace(config, dictionary=()),
        asr=FakeASR("vip"),
        personalization=store,
    ).transcribe(audio, polish=False, style="formal")
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_headless_transcribe_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store, polish=False)
    audio = np.zeros(1600, dtype=np.float32)
    result = engine.transcribe(audio, style="formal")
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "vip"
    control = VoiceEngine(
        replace(config, dictionary=()),
        asr=FakeASR("vip"),
        personalization=store,
        polish=False,
    ).transcribe(audio, style="formal")
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_headless_transcribe_writing_style_keeps_snippet_expansions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("sig"), personalization=store, polish=False)
    audio = np.zeros(1600, dtype=np.float32)
    result = engine.transcribe(audio, style="formal")
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "sig"
    control = VoiceEngine(
        replace(config, snippets=()),
        asr=FakeASR("sig"),
        personalization=store,
        polish=False,
    ).transcribe(audio, style="formal")
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_headless_transcribe_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store, polish=False)
    audio = np.zeros(1600, dtype=np.float32)
    engine.transcribe(audio, style="formal")
    result = engine.transcribe(audio)
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "vip"
    control = VoiceEngine(
        replace(config, dictionary=()),
        asr=FakeASR("vip"),
        personalization=store,
        polish=False,
    )
    control.transcribe(audio, style="formal")
    control_result = control.transcribe(audio)
    assert "I'm Ada" not in control_result.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_headless_transcribe_writing_style_keeps_snippet_expansions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("sig"), personalization=store, polish=False)
    audio = np.zeros(1600, dtype=np.float32)
    engine.transcribe(audio, style="formal")
    result = engine.transcribe(audio)
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "sig"
    control = VoiceEngine(
        replace(config, snippets=()),
        asr=FakeASR("sig"),
        personalization=store,
        polish=False,
    )
    control.transcribe(audio, style="formal")
    control_result = control.transcribe(audio)
    assert "I'm Ada" not in control_result.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_headless_transcribe_file_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path,
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store)
    assert engine.polish is True
    engine.transcribe_file(wav, style="formal", polish=False)
    result = engine.transcribe_file(wav)
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "vip"
    control = VoiceEngine(
        replace(config, dictionary=()),
        asr=FakeASR("vip"),
        personalization=store,
    )
    control.transcribe_file(wav, style="formal", polish=False)
    control_result = control.transcribe_file(wav)
    assert "I'm Ada" not in control_result.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_headless_transcribe_file_writing_style_keeps_snippet_expansions(
    tmp_path: Path,
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("sig"), personalization=store)
    assert engine.polish is True
    engine.transcribe_file(wav, style="formal", polish=False)
    result = engine.transcribe_file(wav)
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "sig"
    control = VoiceEngine(
        replace(config, snippets=()),
        asr=FakeASR("sig"),
        personalization=store,
    )
    control.transcribe_file(wav, style="formal", polish=False)
    control_result = control.transcribe_file(wav)
    assert "I'm Ada" not in control_result.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_compose_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
        snippets=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, personalization=store)
    text = engine.compose("vip", style="formal")
    assert "I'm Ada" in text
    assert "I am Ada" not in text
    control = VoiceEngine(
        replace(config, dictionary=()),
        personalization=store,
    )
    control_text = control.compose("vip", style="formal")
    assert "I'm Ada" not in control_text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_compose_writing_style_keeps_snippet_expansions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, personalization=store)
    text = engine.compose("sig", style="formal")
    assert "I'm Ada" in text
    assert "I am Ada" not in text
    assert engine._asr is None
    control = VoiceEngine(
        replace(config, snippets=()),
        personalization=store,
    )
    control_text = control.compose("sig", style="formal")
    assert "I'm Ada" not in control_text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_compose_writing_style_keeps_app_scoped_learned_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=(),
        snippets=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    engine = VoiceEngine(config, personalization=store)
    matching = engine.compose("vip", style="formal", app_context="Code.exe")
    assert "I'm Ada" in matching
    assert "I am Ada" not in matching
    other = engine.compose("vip", style="formal", app_context="notepad.exe")
    assert "I'm Ada" not in other
    omitted = engine.compose("vip", style="formal")
    assert "I'm Ada" not in omitted
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_text_compose_writing_style_keeps_dictionary_written_forms(
    fake_asr,
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ComposeRequest, ServiceEngine, create_app

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary, snippets=())
    body = engine.compose(ComposeRequest(text="vip", style="formal"))
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    control = ServiceEngine(asr=fake_asr, dictionary=(), snippets=())
    control_body = control.compose(ComposeRequest(text="vip", style="formal"))
    assert "I'm Ada" not in control_body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"
    client = TestClient(create_app(engine))
    response = client.post("/compose", json={"text": "vip", "style": "formal"})
    assert response.status_code == 200
    assert "I'm Ada" in response.json()["text"]
    assert "I am Ada" not in response.json()["text"]


def test_ade_text_compose_writing_style_keeps_snippet_expansions(
    fake_asr,
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ComposeRequest, ServiceEngine, create_app

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=(), snippets=snippets)
    body = engine.compose(ComposeRequest(text="sig", style="formal"))
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    control = ServiceEngine(asr=fake_asr, dictionary=(), snippets=())
    control_body = control.compose(ComposeRequest(text="sig", style="formal"))
    assert "I'm Ada" not in control_body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"
    client = TestClient(create_app(engine))
    response = client.post("/compose", json={"text": "sig", "style": "formal"})
    assert response.status_code == 200
    assert "I'm Ada" in response.json()["text"]
    assert "I am Ada" not in response.json()["text"]


def test_ade_attach_compose_writing_style_keeps_dictionary_written_forms(
    fake_asr,
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary, snippets=())
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(create_app(engine, token="s3cret")),
    )
    caps = client.capabilities()
    assert "compose" in caps["features"]
    body = client.compose("vip", style="formal")
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    control = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(
            create_app(
                ServiceEngine(asr=fake_asr, dictionary=(), snippets=()),
                token="s3cret",
            )
        ),
    )
    control_body = control.compose("vip", style="formal")
    assert "I'm Ada" not in control_body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_attach_compose_writing_style_keeps_snippet_expansions(
    fake_asr,
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=(), snippets=snippets)
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(create_app(engine, token="s3cret")),
    )
    caps = client.capabilities()
    assert "compose" in caps["features"]
    body = client.compose("sig", style="formal")
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    control = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(
            create_app(
                ServiceEngine(asr=fake_asr, dictionary=(), snippets=()),
                token="s3cret",
            )
        ),
    )
    control_body = control.compose("sig", style="formal")
    assert "I'm Ada" not in control_body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_attach_compose_writing_style_keeps_learned_written_forms(
    fake_asr, tmp_path: Path
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(create_app(engine, token="s3cret")),
    )
    learned = client.learn("vip", "I'm Ada")
    assert learned["ok"] is True
    caps = client.capabilities()
    assert "compose" in caps["features"]
    assert "learn" in caps["features"]
    body = client.compose("vip", style="formal")
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    control_store = PersonalizationStore(tmp_path / "empty.json", enabled=True, learn=True)
    control = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(
            create_app(
                ServiceEngine(
                    asr=fake_asr,
                    personalization=control_store,
                    dictionary=(),
                    snippets=(),
                ),
                token="s3cret",
            )
        ),
    )
    control_body = control.compose("vip", style="formal")
    assert "I'm Ada" not in control_body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_attach_compose_writing_style_keeps_app_scoped_learned_written_forms(
    fake_asr, tmp_path: Path
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(create_app(engine, token="s3cret")),
    )
    learned = client.learn("vip", "I'm Ada", app_context="Code.exe")
    assert learned["ok"] is True
    matching = client.compose("vip", style="formal", app_context="Code.exe")
    assert "I'm Ada" in matching["text"]
    assert "I am Ada" not in matching["text"]
    other = client.compose("vip", style="formal", app_context="notepad.exe")
    assert "I'm Ada" not in other["text"]
    unscoped = client.compose("vip", style="formal")
    assert "I'm Ada" not in unscoped["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_attach_learn_records_app_scoped_learned_written_forms(
    fake_asr, tmp_path: Path
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(create_app(engine, token="s3cret")),
    )
    learned = client.learn("vip", "I'm Ada", app_context="Code.exe")
    assert learned["ok"] is True
    term = learned.get("term") or {}
    assert term.get("written") == "I'm Ada"
    assert (term.get("app") or "").lower() == "code.exe"
    matching = client.compose("vip", style="formal", app_context="Code.exe")
    assert "I'm Ada" in matching["text"]
    assert "I am Ada" not in matching["text"]
    other = client.compose("vip", style="formal", app_context="notepad.exe")
    assert "I'm Ada" not in other["text"]
    unscoped = client.compose("vip", style="formal")
    assert "I'm Ada" not in unscoped["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_learn_records_app_scoped_learned_written_forms(fake_asr, tmp_path: Path) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import (
        ComposeRequest,
        LearnRequest,
        ServiceEngine,
        create_app,
    )

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    learned = engine.learn(LearnRequest(spoken="vip", written="I'm Ada", app_context="Code.exe"))
    assert learned["ok"] is True
    term = learned.get("term") or {}
    assert term.get("written") == "I'm Ada"
    assert (term.get("app") or "").lower() == "code.exe"
    matching = engine.compose(ComposeRequest(text="vip", style="formal", app_context="Code.exe"))
    assert "I'm Ada" in matching["text"]
    assert "I am Ada" not in matching["text"]
    other = engine.compose(ComposeRequest(text="vip", style="formal", app_context="notepad.exe"))
    assert "I'm Ada" not in other["text"]
    omitted = engine.compose(ComposeRequest(text="vip", style="formal"))
    assert "I'm Ada" not in omitted["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"
    client = TestClient(create_app(engine, token="s3cret"))
    headers = {"Authorization": "Bearer s3cret"}
    http = client.post(
        "/learn",
        json={
            "spoken": "vip",
            "written": "I'm Ada",
            "app_context": "Code.exe",
        },
        headers=headers,
    )
    assert http.status_code == 200
    assert http.json()["ok"] is True


def test_ade_attach_personalization_snapshot_returns_app_scoped_learned_written_forms(
    fake_asr, tmp_path: Path
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(create_app(engine, token="s3cret")),
    )
    learned = client.learn("vip", "I'm Ada", app_context="Code.exe")
    assert learned["ok"] is True
    snap = client.personalization()
    assert snap["stores_audio"] is False
    written = [term["written"] for term in snap["terms"]]
    apps = [term["app"] for term in snap["terms"]]
    assert "I'm Ada" in written
    assert "I am Ada" not in written
    assert "code.exe" in apps
    empty = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(
            create_app(
                ServiceEngine(
                    asr=fake_asr,
                    personalization=PersonalizationStore(
                        tmp_path / "empty.json", enabled=True, learn=True
                    ),
                    dictionary=(),
                    snippets=(),
                ),
                token="s3cret",
            )
        ),
    )
    control = empty.personalization()
    assert "I'm Ada" not in [term["written"] for term in control["terms"]]
    assert control["stores_audio"] is False
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_attach_transcribe_writing_style_keeps_app_scoped_learned_written_forms(
    tmp_path: Path,
) -> None:
    import wave

    from fastapi.testclient import TestClient

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = ServiceEngine(
        asr=FakeASR("vip"),
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(create_app(engine, token="s3cret")),
    )
    learned = client.learn("vip", "I'm Ada", app_context="Code.exe")
    assert learned["ok"] is True
    matching = client.transcribe_file(wav, style="formal", app_context="Code.exe")
    assert "I'm Ada" in matching["cleaned"]
    assert "I am Ada" not in matching["cleaned"]
    assert matching["raw"] == "vip"
    other = client.transcribe_file(wav, style="formal", app_context="notepad.exe")
    assert "I'm Ada" not in other["cleaned"]
    unscoped = client.transcribe_file(wav, style="formal")
    assert "I'm Ada" not in unscoped["cleaned"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_attach_transcribe_oneshot_writing_style_keeps_app_scoped_learned_written_forms(
    tmp_path: Path,
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = ServiceEngine(
        asr=FakeASR("vip"),
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(create_app(engine, token="s3cret")),
    )
    learned = client.learn("vip", "I'm Ada", app_context="Code.exe")
    assert learned["ok"] is True
    payload = {
        "audio": [0.0] * 1600,
        "samplerate": 16000,
        "style": "formal",
        "app_context": "Code.exe",
    }
    matching = client.transcribe(payload)
    assert "I'm Ada" in matching["cleaned"]
    assert "I am Ada" not in matching["cleaned"]
    assert matching["raw"] == "vip"
    other = client.transcribe(
        {
            "audio": [0.0] * 1600,
            "samplerate": 16000,
            "style": "formal",
            "app_context": "notepad.exe",
        }
    )
    assert "I'm Ada" not in other["cleaned"]
    omitted = client.transcribe({"audio": [0.0] * 1600, "samplerate": 16000, "style": "formal"})
    assert "I'm Ada" not in omitted["cleaned"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_attach_stream_writing_style_keeps_app_scoped_learned_written_forms(
    tmp_path: Path,
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.attach import VoiceAttachClient
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.ws import add_stream_websocket

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = ServiceEngine(
        asr=FakeASR("vip"),
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    app = create_app(engine, token="s3cret")
    add_stream_websocket(app, engine, token="s3cret")
    client = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "s3cret",
        client=TestClient(app),
    )
    learned = client.learn("vip", "I'm Ada", app_context="Code.exe")
    assert learned["ok"] is True
    audio = [0.1] * 16000
    matching = client.stream(audio, style="formal", app_context="Code.exe")
    assert matching["type"] == "final"
    assert "I'm Ada" in matching["text"]
    assert "I am Ada" not in matching["text"]
    other = client.stream(audio, style="formal", app_context="notepad.exe")
    assert "I'm Ada" not in other["text"]
    unscoped = client.stream(audio, style="formal")
    assert "I'm Ada" not in unscoped["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_cli_compose_writing_style_keeps_learned_written_forms(capsys, tmp_path: Path) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from dcent_voice.app import reset_cli_compose_sticky, run_compose_command
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    reset_cli_compose_sticky()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed")
    assert term is not None
    args = Namespace(text=["vip"], style="formal", cleanup_level="medium")
    config = SimpleNamespace(snippets=(), dictionary=())
    assert run_compose_command(config, args, personalization=store) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = PersonalizationStore(tmp_path / "empty.json", enabled=True, learn=True)
    assert run_compose_command(config, args, personalization=empty) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_cli_compose_writing_style_keeps_app_scoped_learned_written_forms(
    capsys, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from dcent_voice.app import (
        build_parser,
        reset_cli_compose_sticky,
        run_compose_command,
    )
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    reset_cli_compose_sticky()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    config = SimpleNamespace(snippets=(), dictionary=())
    matching = build_parser().parse_args(
        ["compose", "vip", "--style", "formal", "--app", "Code.exe"]
    )
    assert run_compose_command(config, matching, personalization=store) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    other = build_parser().parse_args(
        ["compose", "vip", "--style", "formal", "--app", "notepad.exe"]
    )
    assert run_compose_command(config, other, personalization=store) == 0
    other_out = capsys.readouterr().out
    assert "I'm Ada" not in other_out
    omitted = build_parser().parse_args(["compose", "vip", "--style", "formal"])
    assert run_compose_command(config, omitted, personalization=store) == 0
    omitted_out = capsys.readouterr().out
    assert "I'm Ada" not in omitted_out
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_cli_learn_records_app_scoped_learned_written_forms(capsys, tmp_path: Path) -> None:
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from dcent_voice.app import (
        build_parser,
        reset_cli_compose_sticky,
        run_compose_command,
        run_learn_command,
    )
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    reset_cli_compose_sticky()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    config = load_config(Path("config.example.toml"), create=False)
    learn = build_parser().parse_args(
        ["learn", "--from", "vip", "--to", "I'm Ada", "--app", "Code.exe"]
    )
    assert run_learn_command(config, learn, personalization=store) == 0
    learned = json.loads(capsys.readouterr().out)
    assert learned["ok"] is True
    assert learned["written"] == "I'm Ada"
    compose_config = SimpleNamespace(snippets=(), dictionary=())
    matching = build_parser().parse_args(
        ["compose", "vip", "--style", "formal", "--app", "Code.exe"]
    )
    assert run_compose_command(compose_config, matching, personalization=store) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    other = build_parser().parse_args(
        ["compose", "vip", "--style", "formal", "--app", "notepad.exe"]
    )
    assert run_compose_command(compose_config, other, personalization=store) == 0
    other_out = capsys.readouterr().out
    assert "I'm Ada" not in other_out
    omitted = build_parser().parse_args(["compose", "vip", "--style", "formal"])
    assert run_compose_command(compose_config, omitted, personalization=store) == 0
    omitted_out = capsys.readouterr().out
    assert "I'm Ada" not in omitted_out
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_learn_records_app_scoped_learned_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=(),
        snippets=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    learned = engine.learn("vip", "I'm Ada", app_context="Code.exe")
    assert learned["ok"] is True
    assert learned["written"] == "I'm Ada"
    matching = engine.compose("vip", style="formal", app_context="Code.exe")
    assert "I'm Ada" in matching
    assert "I am Ada" not in matching
    other = engine.compose("vip", style="formal", app_context="notepad.exe")
    assert "I'm Ada" not in other
    omitted = engine.compose("vip", style="formal")
    assert "I'm Ada" not in omitted
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_transcribe_writing_style_keeps_learned_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=(),
        snippets=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed")
    assert term is not None
    audio = np.zeros(1600, dtype=np.float32)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store)
    result = engine.transcribe(audio, style="formal")
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "vip"
    empty = PersonalizationStore(tmp_path / "empty.json", enabled=True, learn=True)
    control = VoiceEngine(config, asr=FakeASR("vip"), personalization=empty).transcribe(
        audio, style="formal"
    )
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_transcribe_writing_style_keeps_app_scoped_learned_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=(),
        snippets=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    audio = np.zeros(1600, dtype=np.float32)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store)
    matching = engine.transcribe(audio, style="formal", app_context="Code.exe")
    assert "I'm Ada" in matching.text
    assert "I am Ada" not in matching.text
    assert matching.raw == "vip"
    other = engine.transcribe(audio, style="formal", app_context="notepad.exe")
    assert "I'm Ada" not in other.text
    omitted = engine.transcribe(audio, style="formal")
    assert "I'm Ada" not in omitted.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_transcribe_file_writing_style_keeps_app_scoped_learned_written_forms(
    tmp_path: Path,
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=(),
        snippets=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store)
    matching = engine.transcribe_file(wav, style="formal", app_context="Code.exe")
    assert "I'm Ada" in matching.text
    assert "I am Ada" not in matching.text
    assert matching.raw == "vip"
    other = engine.transcribe_file(wav, style="formal", app_context="notepad.exe")
    assert "I'm Ada" not in other.text
    omitted = engine.transcribe_file(wav, style="formal")
    assert "I'm Ada" not in omitted.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_shipped_default_real_speech_keeps_accuracy_latency_reliability(
    tmp_path: Path,
) -> None:
    import time
    import wave
    from pathlib import Path

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.eval_corpus import word_error_rate
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    with wave.open(str(wav), "rb") as handle:
        channels = handle.getnchannels()
        samplerate = handle.getframerate()
        frames = handle.getnframes()
        width = handle.getsampwidth()
        pcm = handle.readframes(frames)
    assert channels == 1
    assert width == 2
    duration_s = frames / float(samplerate)
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(np.square(audio))))
    peak = float(np.abs(audio).max())
    assert duration_s >= 1.0
    assert rms > 0.01
    assert peak > 0.01
    assert wav.stat().st_size > 10_000
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        started = time.perf_counter()
        result = engine.transcribe_file(wav)
        wall_s = time.perf_counter() - started
    finally:
        engine.unload()
    assert result.rejected_reason == ""
    assert result.provider == "parakeet"
    assert "tiny" not in result.model.lower()
    assert "tdt" in result.model.lower()
    assert word_error_rate("Hello world", result.text) == 0.0
    assert "hello" in result.text.lower()
    assert "world" in result.text.lower()
    assert "asr" in result.timings
    assert "postprocess" in result.timings
    assert float(result.timings["postprocess"]) >= 0.0
    assert wall_s > 0.0
    assert wall_s >= float(result.timings["asr"]) * 0.5
    assert wall_s < 5.0
    assert result.asr_latency_s > 0.0
    assert result.asr_latency_s <= wall_s + 0.05


def test_shipped_default_first_dictation_stays_warm(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine, score_shipped_default_first_dictation
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_first_dictation(engine, wav)
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "first_dictation"
    assert score.model_loaded_after is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.wer == 0.0
    assert "hello" in score.text.lower()
    assert "world" in score.text.lower()
    assert score.load_s >= 0.0
    assert score.transcribe_s > 0.0
    assert score.transcribe_s < 2.0
    assert score.asr_s > 0.0
    assert score.asr_s < 2.0


def test_shipped_default_model_load_stays_bounded(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine, score_shipped_default_model_load
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_model_load(engine)
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "model_load"
    assert score.model_loaded_before is False
    assert score.model_loaded_after is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.load_s >= 0.2
    assert score.load_s < 8.0


def test_shipped_default_dictation_cpu_stays_bounded(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine, score_shipped_default_utterance_cpu
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_utterance_cpu(engine, wav)
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "utterance_cpu"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.wer == 0.0
    assert "hello" in score.text.lower()
    assert "world" in score.text.lower()
    assert score.audio_s >= 1.0
    assert score.cpu_s > 0.05
    assert score.cpu_s < 4.0
    assert score.wall_s > 0.0
    assert score.wall_s < 2.0


def test_shipped_default_loaded_ram_stays_bounded(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine, score_shipped_default_loaded_ram
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_loaded_ram(engine)
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "loaded_ram"
    assert score.model_loaded is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.rss_bytes > 100 * 1024 * 1024
    assert score.rss_bytes <= 2 * 1024 * 1024 * 1024


def test_shipped_default_idle_cpu_stays_near_zero(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine, score_shipped_default_idle_cpu
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_idle_cpu(engine)
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "idle_cpu"
    assert score.model_loaded is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert len(score.samples) >= 4
    assert score.cpu_mean <= 5.0
    assert score.cpu_max <= 15.0
    assert score.rss_bytes > 0


def test_shipped_default_streaming_dictation_stays_responsive(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine, score_shipped_default_stream_dictation
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_stream_dictation(engine, wav)
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "stream_dictation"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.wer == 0.0
    assert "hello" in score.text.lower()
    assert "world" in score.text.lower()
    assert score.chunks >= 4
    assert score.partials >= 1
    assert score.finals >= 1
    assert "partial" in score.events
    assert "final" in score.events
    assert score.wall_s > 0.0
    assert score.wall_s < 8.0


def test_shipped_default_transcribe_tail_stays_bounded(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import (
        SHIPPED_DEFAULT_TAIL_AUDIO_IDS,
        VoiceEngine,
        score_shipped_default_transcribe_tail,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_transcribe_tail(engine)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert score.kind == "transcribe_tail"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.ids == SHIPPED_DEFAULT_TAIL_AUDIO_IDS
    assert len(score.ids) == 5
    assert "hello" in score.ids
    assert len(score.walls_s) == 5
    assert len(score.wers) == 5
    assert all(wer == 0.0 for wer in score.wers)
    assert score.p50_s > 0.0
    assert score.p50_s <= score.p95_s
    assert score.p95_s <= score.max_s
    assert score.p95_s < 1.0


def test_shipped_default_streaming_tail_stays_bounded(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import (
        SHIPPED_DEFAULT_TAIL_AUDIO_IDS,
        VoiceEngine,
        score_shipped_default_stream_tail,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_stream_tail(engine)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert score.kind == "stream_tail"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.ids == SHIPPED_DEFAULT_TAIL_AUDIO_IDS
    assert len(score.ids) == 5
    assert "hello" in score.ids
    assert len(score.walls_s) == 5
    assert len(score.wers) == 5
    assert all(wer == 0.0 for wer in score.wers)
    assert all(n >= 1 for n in score.partials)
    assert all(n >= 1 for n in score.finals)
    assert score.p50_s > 0.0
    assert score.p50_s <= score.p95_s
    assert score.p95_s <= score.max_s
    assert score.p95_s < 2.0


def test_shipped_default_real_speech_corpus_keeps_wer_cer(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.eval_corpus import (
        SHIPPED_DEFAULT_AUDIO_IDS,
        score_shipped_default_audio_corpus,
        summarize,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        scores = score_shipped_default_audio_corpus(engine.transcribe_file)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    ids = [item.id for item in scores]
    assert ids == list(SHIPPED_DEFAULT_AUDIO_IDS)
    assert len(scores) >= 12
    assert "hello" in ids
    assert ids != ["hello"]
    public = [item for item in scores if "librispeech" in item.tags or "public" in item.tags]
    noisy = [item for item in scores if "noisy" in item.tags]
    langs = {item.language for item in scores}
    assert len(public) >= 6
    assert len(noisy) >= 1
    assert {"en", "fr", "de"} <= langs or len(langs) >= 2
    report = summarize(scores)
    assert report["audio_items"] == len(scores)
    assert report["asr_wer_mean"] is not None
    assert report["asr_wer_mean"] <= 0.10
    assert report["cer_mean"] <= 0.15
    hello = next(item for item in scores if item.id == "hello")
    assert hello.wer == 0.0


def test_shipped_default_longform_real_speech_keeps_wer_cer(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.eval_corpus import (
        SHIPPED_DEFAULT_AUDIO_IDS,
        SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS,
        score_shipped_default_longform_corpus,
        summarize,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        scores = score_shipped_default_longform_corpus(engine.transcribe_file)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
        asr_class = type(engine.asr).__name__
    finally:
        engine.unload()
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    ids = [item.id for item in scores]
    assert ids == list(SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_AUDIO_IDS)
    assert "hello" not in ids
    assert len(scores) >= 5
    long_items = [item for item in scores if "long" in item.tags]
    noisy = [item for item in scores if "noisy" in item.tags]
    assert len(long_items) >= 3
    assert len(noisy) >= 1
    assert "ls-noisy-marie" in ids
    assert "ls-tc-angor" in ids
    report = summarize(scores)
    assert report["audio_items"] == len(scores)
    assert report["asr_wer_mean"] is not None
    assert report["asr_wer_mean"] <= 0.15
    assert report["cer_mean"] <= 0.20
    assert report["long_asr_items"] >= 3
    assert report["long_asr_wer_mean"] is not None
    assert report["long_asr_wer_mean"] <= 0.10


def test_shipped_default_product_dictation_keeps_wer_cer(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.eval_corpus import (
        SHIPPED_DEFAULT_AUDIO_IDS,
        SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS,
        SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS,
        score_shipped_default_product_corpus,
        summarize,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        scores = score_shipped_default_product_corpus(engine.transcribe_file)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    ids = [item.id for item in scores]
    assert ids == list(SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS)
    assert set(ids).isdisjoint(SHIPPED_DEFAULT_AUDIO_IDS)
    assert set(ids).isdisjoint(SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS)
    assert "hello" not in ids
    assert len(scores) >= 6
    for required in (
        "numbers",
        "url-email",
        "filename",
        "developer-file",
        "shell",
        "bitcoin",
        "dcentral-terms",
    ):
        assert required in ids
    report = summarize(scores)
    assert report["audio_items"] == len(scores)
    assert report["asr_wer_mean"] is not None
    assert report["asr_wer_mean"] <= 0.10
    assert report["cer_mean"] <= 0.15


def test_shipped_default_learned_vocabulary_keeps_product_names(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine, score_shipped_default_learned_vocab
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_learned_vocab(engine)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert score.kind == "learned_vocab"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert "sent voice" in score.before.lower()
    assert "DCENT_Voice" not in score.before
    assert score.before_wer > 0.0
    assert "DCENT_Voice" in score.after
    assert "sent voice" not in score.after.lower()
    assert score.after_wer == 0.0


def test_shipped_default_app_scoped_vocabulary_stays_fail_closed(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import (
        VoiceEngine,
        score_shipped_default_app_learned_vocab,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_app_learned_vocab(engine)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert score.kind == "app_learned_vocab"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0


def test_shipped_default_app_scoped_streaming_vocabulary_stays_fail_closed(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import (
        VoiceEngine,
        score_shipped_default_app_learned_stream,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_app_learned_stream(engine)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert score.kind == "app_learned_stream"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert score.other_partials >= 1
    assert score.other_finals >= 1
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert score.none_partials >= 1
    assert score.none_finals >= 1
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0
    assert score.same_partials >= 1
    assert score.same_finals >= 1


def test_shipped_default_reloaded_streaming_vocabulary_stays_fail_closed(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import (
        VoiceEngine,
        score_shipped_default_app_learned_stream_reload,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store_path = tmp_path / "p.json"
    store = PersonalizationStore(store_path, enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_app_learned_stream_reload(engine)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert score.kind == "app_learned_stream_reload"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.store_exists is True
    assert score.distinct_engine is True
    assert store_path.is_file()
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert score.other_partials >= 1
    assert score.other_finals >= 1
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert score.none_partials >= 1
    assert score.none_finals >= 1
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0
    assert score.same_partials >= 1
    assert score.same_finals >= 1


def test_shipped_default_restarted_streaming_vocabulary_stays_fail_closed(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import (
        VoiceEngine,
        score_shipped_default_app_learned_stream_restart,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store_path = tmp_path / "p.json"
    store = PersonalizationStore(store_path, enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_app_learned_stream_restart(engine)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert score.kind == "app_learned_stream_restart"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.store_exists is True
    assert score.distinct_pid is True
    assert score.parent_pid != score.child_pid
    assert score.child_pid > 0
    assert store_path.is_file()
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert score.other_partials >= 1
    assert score.other_finals >= 1
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert score.none_partials >= 1
    assert score.none_finals >= 1
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0
    assert score.same_partials >= 1
    assert score.same_finals >= 1


def test_shipped_default_frozen_streaming_vocabulary_stays_fail_closed(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import (
        VoiceEngine,
        score_shipped_default_frozen_stream_restart,
    )
    from dcent_voice.personalization import PersonalizationStore

    frozen_exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    require_isolatable_frozen_exe(frozen_exe)
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store_path = tmp_path / "p.json"
    store = PersonalizationStore(store_path, enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        score = score_shipped_default_frozen_stream_restart(engine, frozen_exe)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert score.kind == "app_learned_stream_frozen"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.store_exists is True
    assert score.distinct_pid is True
    assert score.frozen is True
    assert score.parent_pid != score.child_pid
    assert score.child_pid > 0
    assert "dcent-voice" in Path(score.child_exe).name.lower()
    assert store_path.is_file()
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert score.other_partials >= 1
    assert score.other_finals >= 1
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert score.none_partials >= 1
    assert score.none_finals >= 1
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0
    assert score.same_partials >= 1
    assert score.same_finals >= 1


def test_shipped_default_appdata_streaming_vocabulary_stays_fail_closed() -> None:
    import os
    from pathlib import Path

    from dcent_voice.engine import score_shipped_default_appdata_stream_relaunch
    from dcent_voice.personalization import default_personalization_path

    frozen_exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    require_isolatable_frozen_exe(frozen_exe)
    appdata_store = default_personalization_path()
    cfg = Path(os.environ["APPDATA"]) / "DCENT_Voice" / "config.toml"
    cfg_before = cfg.read_bytes() if cfg.is_file() else b""
    score = score_shipped_default_appdata_stream_relaunch(frozen_exe)
    assert not appdata_store.is_file()
    if cfg.is_file():
        assert cfg.read_bytes() == cfg_before
    assert score.kind == "app_learned_stream_appdata"
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.store_is_appdata is True
    assert score.restored is True
    assert score.frozen is True
    assert score.distinct_pid is True
    assert score.parent_pid != score.stream_pid
    assert score.parent_pid != score.learn_pid
    assert score.learn_pid != score.stream_pid
    assert score.stream_pid > 0
    assert score.learn_pid > 0
    assert "dcent-voice" in Path(score.child_exe).name.lower()
    assert Path(score.store_path).name == "personalization.json"
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert score.other_partials >= 1
    assert score.other_finals >= 1
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert score.none_partials >= 1
    assert score.none_finals >= 1
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0
    assert score.same_partials >= 1
    assert score.same_finals >= 1


def test_shipped_default_desktop_relaunch_vocabulary_stays_fail_closed() -> None:
    import os
    from pathlib import Path

    from dcent_voice.engine import score_shipped_default_desktop_stream_relaunch
    from dcent_voice.personalization import default_personalization_path

    frozen_exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    require_isolatable_frozen_exe(frozen_exe)
    appdata_store = default_personalization_path()
    cfg = Path(os.environ["APPDATA"]) / "DCENT_Voice" / "config.toml"
    cfg_before = cfg.read_bytes() if cfg.is_file() else b""
    score = score_shipped_default_desktop_stream_relaunch(frozen_exe)
    assert not appdata_store.is_file()
    if cfg.is_file():
        assert cfg.read_bytes() == cfg_before
    assert score.kind == "app_learned_stream_desktop"
    assert score.desktop is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.store_is_appdata is True
    assert score.restored is True
    assert score.frozen is True
    assert score.distinct_pid is True
    assert score.parent_pid != score.stream_pid
    assert score.parent_pid != score.learn_pid
    assert score.learn_pid != score.stream_pid
    assert score.stream_pid > 0
    assert score.learn_pid > 0
    assert "dcent-voice" in Path(score.child_exe).name.lower()
    assert Path(score.store_path).name == "personalization.json"
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert score.other_partials >= 1
    assert score.other_finals >= 1
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert score.none_partials >= 1
    assert score.none_finals >= 1
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0
    assert score.same_partials >= 1
    assert score.same_finals >= 1


def test_shipped_default_desktop_relaunch_oneshot_vocabulary_stays_fail_closed() -> None:
    import os
    from pathlib import Path

    from dcent_voice.engine import score_shipped_default_desktop_oneshot_relaunch
    from dcent_voice.personalization import default_personalization_path

    frozen_exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    require_isolatable_frozen_exe(frozen_exe)
    appdata_store = default_personalization_path()
    cfg = Path(os.environ["APPDATA"]) / "DCENT_Voice" / "config.toml"
    cfg_before = cfg.read_bytes() if cfg.is_file() else b""
    score = score_shipped_default_desktop_oneshot_relaunch(frozen_exe)
    assert not appdata_store.is_file()
    if cfg.is_file():
        assert cfg.read_bytes() == cfg_before
    assert score.kind == "app_learned_oneshot_desktop"
    assert score.desktop is True
    assert score.oneshot is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.store_is_appdata is True
    assert score.restored is True
    assert score.frozen is True
    assert score.distinct_pid is True
    assert score.parent_pid != score.oneshot_pid
    assert score.parent_pid != score.learn_pid
    assert score.learn_pid != score.oneshot_pid
    assert score.oneshot_pid > 0
    assert score.learn_pid > 0
    assert "dcent-voice" in Path(score.child_exe).name.lower()
    assert Path(score.store_path).name == "personalization.json"
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0


def test_shipped_default_desktop_relaunch_compose_vocabulary_stays_fail_closed() -> None:
    import os
    from pathlib import Path

    from dcent_voice.engine import score_shipped_default_desktop_compose_relaunch
    from dcent_voice.personalization import default_personalization_path

    frozen_exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    require_isolatable_frozen_exe(frozen_exe)
    appdata_store = default_personalization_path()
    cfg = Path(os.environ["APPDATA"]) / "DCENT_Voice" / "config.toml"
    cfg_before = cfg.read_bytes() if cfg.is_file() else b""
    score = score_shipped_default_desktop_compose_relaunch(frozen_exe)
    assert not appdata_store.is_file()
    if cfg.is_file():
        assert cfg.read_bytes() == cfg_before
    assert score.kind == "app_learned_compose_desktop"
    assert score.desktop is True
    assert score.compose is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.store_is_appdata is True
    assert score.restored is True
    assert score.frozen is True
    assert score.distinct_pid is True
    assert score.parent_pid != score.compose_pid
    assert score.parent_pid != score.learn_pid
    assert score.learn_pid != score.compose_pid
    assert score.compose_pid > 0
    assert score.learn_pid > 0
    assert "dcent-voice" in Path(score.child_exe).name.lower()
    assert Path(score.store_path).name == "personalization.json"
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0


def test_shipped_default_desktop_relaunch_personalization_stays_fail_closed() -> None:
    import os
    from pathlib import Path

    from dcent_voice.engine import (
        score_shipped_default_desktop_personalization_relaunch,
    )
    from dcent_voice.personalization import default_personalization_path

    frozen_exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    require_isolatable_frozen_exe(frozen_exe)
    appdata_store = default_personalization_path()
    cfg = Path(os.environ["APPDATA"]) / "DCENT_Voice" / "config.toml"
    cfg_before = cfg.read_bytes() if cfg.is_file() else b""
    score = score_shipped_default_desktop_personalization_relaunch(frozen_exe)
    assert not appdata_store.is_file()
    if cfg.is_file():
        assert cfg.read_bytes() == cfg_before
    assert score.kind == "app_learned_personalization_desktop"
    assert score.desktop is True
    assert score.inspect is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.term_app == "notepad.exe"
    assert score.term_count >= 1
    assert score.stores_audio is False
    assert score.other_app_absent is True
    assert score.store_is_appdata is True
    assert score.restored is True
    assert score.frozen is True
    assert score.distinct_pid is True
    assert score.parent_pid != score.inspect_pid
    assert score.parent_pid != score.learn_pid
    assert score.learn_pid != score.inspect_pid
    assert score.inspect_pid > 0
    assert score.learn_pid > 0
    assert "dcent-voice" in Path(score.child_exe).name.lower()
    assert Path(score.store_path).name == "personalization.json"


def test_shipped_default_desktop_relaunch_json_transcribe_vocabulary_stays_fail_closed() -> None:
    import os
    from pathlib import Path

    from dcent_voice.engine import score_shipped_default_desktop_json_relaunch
    from dcent_voice.personalization import default_personalization_path

    frozen_exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    require_isolatable_frozen_exe(frozen_exe)
    appdata_store = default_personalization_path()
    cfg = Path(os.environ["APPDATA"]) / "DCENT_Voice" / "config.toml"
    cfg_before = cfg.read_bytes() if cfg.is_file() else b""
    score = score_shipped_default_desktop_json_relaunch(frozen_exe)
    assert not appdata_store.is_file()
    if cfg.is_file():
        assert cfg.read_bytes() == cfg_before
    assert score.kind == "app_learned_json_desktop"
    assert score.desktop is True
    assert score.json_audio is True
    assert score.provider == "parakeet"
    assert "tiny" not in score.model.lower()
    assert "tdt" in score.model.lower()
    assert score.spoken == "sent voice"
    assert score.written == "DCENT_Voice"
    assert score.app == "notepad.exe"
    assert score.other_app == "chrome.exe"
    assert score.store_is_appdata is True
    assert score.restored is True
    assert score.frozen is True
    assert score.distinct_pid is True
    assert score.parent_pid != score.transcribe_pid
    assert score.parent_pid != score.learn_pid
    assert score.learn_pid != score.transcribe_pid
    assert score.transcribe_pid > 0
    assert score.learn_pid > 0
    assert "dcent-voice" in Path(score.child_exe).name.lower()
    assert Path(score.store_path).name == "personalization.json"
    assert "sent voice" in score.other.lower()
    assert "DCENT_Voice" not in score.other
    assert score.other_wer > 0.0
    assert "sent voice" in score.none.lower()
    assert "DCENT_Voice" not in score.none
    assert score.none_wer > 0.0
    assert "DCENT_Voice" in score.same
    assert "sent voice" not in score.same.lower()
    assert score.same_wer == 0.0


def test_shipped_default_multilingual_dictation_keeps_wer_cer(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.eval_corpus import (
        SHIPPED_DEFAULT_AUDIO_IDS,
        SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS,
        SHIPPED_DEFAULT_MULTILINGUAL_AUDIO_IDS,
        SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS,
        score_shipped_default_multilingual_corpus,
        summarize,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        scores = score_shipped_default_multilingual_corpus(engine.transcribe_file)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    ids = [item.id for item in scores]
    assert ids == list(SHIPPED_DEFAULT_MULTILINGUAL_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS)
    assert "hello" not in ids
    assert "lingua-libre-es-hola" in ids
    assert "lingua-libre-fr-je-mappelle" in ids
    assert "lingua-libre-de-hallo" in ids
    langs = {item.language for item in scores}
    assert {"fr", "de", "es"} <= langs
    report = summarize(scores)
    assert report["audio_items"] == len(scores)
    assert report["asr_wer_mean"] is not None
    assert report["asr_wer_mean"] <= 0.10
    assert report["cer_mean"] <= 0.15


def test_shipped_default_accented_dictation_keeps_wer_cer(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.eval_corpus import (
        SHIPPED_DEFAULT_ACCENT_AUDIO_IDS,
        SHIPPED_DEFAULT_AUDIO_IDS,
        SHIPPED_DEFAULT_MULTILINGUAL_AUDIO_IDS,
        SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS,
        score_shipped_default_accent_corpus,
        summarize,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        scores = score_shipped_default_accent_corpus(engine.transcribe_file)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    ids = [item.id for item in scores]
    assert ids == list(SHIPPED_DEFAULT_ACCENT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_MULTILINGUAL_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS)
    assert "hello" not in ids
    assert "ll-en-air-pollution" in ids
    assert "ll-en-above-all" in ids
    assert "ll-en-air-pollution" not in SHIPPED_DEFAULT_AUDIO_IDS
    report = summarize(scores)
    assert report["audio_items"] == len(scores)
    assert report["asr_wer_mean"] is not None
    assert report["asr_wer_mean"] <= 0.10
    assert report["cer_mean"] <= 0.15


def test_shipped_default_noisy_dictation_keeps_wer_cer(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.eval_corpus import (
        SHIPPED_DEFAULT_ACCENT_AUDIO_IDS,
        SHIPPED_DEFAULT_AUDIO_IDS,
        SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS,
        SHIPPED_DEFAULT_MULTILINGUAL_AUDIO_IDS,
        SHIPPED_DEFAULT_NOISY_AUDIO_IDS,
        SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS,
        score_shipped_default_noisy_corpus,
        summarize,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        scores = score_shipped_default_noisy_corpus(engine.transcribe_file)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    ids = [item.id for item in scores]
    assert ids == list(SHIPPED_DEFAULT_NOISY_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_ACCENT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_MULTILINGUAL_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS)
    assert "hello" not in ids
    assert "ls-noisy-quilter" in ids
    assert "ls-noisy-exist" in ids
    assert "ls-noisy-fortune" in ids
    assert set(ids).isdisjoint(SHIPPED_DEFAULT_AUDIO_IDS)
    assert set(ids).isdisjoint(SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS)
    assert "ls-noisy-quilter" not in SHIPPED_DEFAULT_AUDIO_IDS
    report = summarize(scores)
    assert report["audio_items"] == len(scores)
    assert report["asr_wer_mean"] is not None
    assert report["asr_wer_mean"] <= 0.10
    assert report["cer_mean"] <= 0.15


def test_shipped_default_named_dictation_keeps_wer_cer(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.eval_corpus import (
        SHIPPED_DEFAULT_ACCENT_AUDIO_IDS,
        SHIPPED_DEFAULT_AUDIO_IDS,
        SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS,
        SHIPPED_DEFAULT_NAMED_AUDIO_IDS,
        SHIPPED_DEFAULT_NOISY_AUDIO_IDS,
        SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS,
        score_shipped_default_named_corpus,
        summarize,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr_class = type(engine.asr).__name__
        scores = score_shipped_default_named_corpus(engine.transcribe_file)
        provider = engine.asr.spec.provider if getattr(engine.asr, "spec", None) else ""
        model = engine.asr.spec.model if getattr(engine.asr, "spec", None) else ""
    finally:
        engine.unload()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    ids = [item.id for item in scores]
    assert ids == list(SHIPPED_DEFAULT_NAMED_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_NOISY_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_ACCENT_AUDIO_IDS)
    assert ids != list(SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS)
    assert "hello" not in ids
    assert "ls-importance" in ids
    assert "ls-rose-princess" in ids
    assert "ls-quilter" in ids
    assert set(ids).isdisjoint(SHIPPED_DEFAULT_AUDIO_IDS)
    assert set(ids).isdisjoint(SHIPPED_DEFAULT_NOISY_AUDIO_IDS)
    assert "ls-importance" not in SHIPPED_DEFAULT_AUDIO_IDS
    report = summarize(scores)
    assert report["audio_items"] == len(scores)
    assert report["asr_wer_mean"] is not None
    assert report["asr_wer_mean"] <= 0.10
    assert report["cer_mean"] <= 0.15


def test_shipped_default_hold_release_injection_keeps_reliability(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.pipeline import score_shipped_default_hold_release

    class RecordingInjector:
        def __init__(self) -> None:
            self.injected: list[str] = []

        def inject(self, text: str) -> None:
            self.injected.append(text)

        def retract(self, char_count: int) -> None:
            return None

        def press_enter(self) -> None:
            return None

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    injector = RecordingInjector()
    engine = VoiceEngine(config, personalization=store)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release(asr, injector, audio_path=wav)
    finally:
        engine.unload()
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release"
    assert score.id == "hello"
    assert score.injected is True
    assert score.discarded is False
    assert score.wer == 0.0
    assert score.rms >= 0.01
    assert score.duration_s >= 1.0
    assert "hello" in score.hypothesis.lower()
    assert "world" in score.hypothesis.lower()
    assert injector.injected == [score.hypothesis]
    assert "asr" in score.timings
    assert "inject" in score.timings
    assert "capture" in score.timings
    assert float(score.timings["asr"]) > 0.0
    assert float(score.timings["inject"]) >= 0.0


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_hold_release_injects_real_apps(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_app,
    )
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_app(
            asr,
            config,
            audio_path=wav,
            scratch_root=tmp_path / "hold-app",
        )
    finally:
        engine.unload()
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_app"
    assert score.app == "notepad"
    assert score.id == "hello"
    assert score.injected is True
    assert score.discarded is False
    assert score.wer == 0.0
    assert score.rms >= 0.01
    assert score.duration_s >= 1.0
    assert "hello" in score.observed.lower()
    assert "world" in score.observed.lower()
    assert score.baseline not in score.observed
    assert score.observed == score.hypothesis
    assert "asr" in score.timings
    assert "inject" in score.timings
    assert "capture" in score.timings
    assert float(score.timings["asr"]) > 0.0


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_hold_release_injects_real_apps(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic,
    )
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic(
            asr,
            config,
            audio_path=wav,
            apps=("notepad",),
            scratch_root=tmp_path / "hold-acoustic",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.input_device != default_in
    assert len(score.apps) >= 1
    assert score.apps == ("notepad",)
    assert "notepad" in score.apps
    assert len(score.observed) == len(score.apps)
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert all(wer == 0.0 for wer in score.wer)
    for text in score.observed:
        assert "hello" in text.lower()
        assert "world" in text.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_extra_app_acoustic_hold_release_injects_real_apps(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_extra_app,
    )
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_extra_app(
            asr,
            config,
            audio_path=wav,
            apps=("vscode",),
            scratch_root=tmp_path / "hold-extra-app",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_extra_app"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.extra_app is True
    assert score.stolen_foreground is True
    assert score.restored_foreground is True
    assert score.input_device != default_in
    assert score.apps == ("vscode",)
    assert "notepad" not in score.apps
    assert len(score.observed) == 1
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert all(wer == 0.0 for wer in score.wer)
    for text in score.observed:
        assert "hello" in text.lower()
        assert "world" in text.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_extra_app_acoustic_hold_release_injects_browsers(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_browser,
    )
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_browser(
            asr,
            config,
            audio_path=wav,
            apps=("edge-ce",),
            scratch_root=tmp_path / "hold-browser",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_browser"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.extra_app is True
    assert score.browser is True
    assert score.stolen_foreground is True
    assert score.restored_foreground is True
    assert score.input_device != default_in
    assert score.apps == ("edge-ce",)
    assert "vscode" not in score.apps
    assert "notepad" not in score.apps
    assert len(score.observed) == 1
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert all(wer == 0.0 for wer in score.wer)
    for text in score.observed:
        assert "hello" in text.lower()
        assert "world" in text.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_extra_app_acoustic_hold_release_injects_chrome(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_chrome,
    )
    from dcent_voice.personalization import PersonalizationStore

    wav = Path("tests/fixtures/audio/hello.wav")
    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_chrome(
            asr,
            config,
            audio_path=wav,
            apps=("chrome-ce",),
            scratch_root=tmp_path / "hold-chrome",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_chrome"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.extra_app is True
    assert score.browser is True
    assert score.chrome is True
    assert score.stolen_foreground is True
    assert score.restored_foreground is True
    assert score.input_device != default_in
    assert score.apps == ("chrome-ce",)
    assert "edge-ce" not in score.apps
    assert "vscode" not in score.apps
    assert "notepad" not in score.apps
    assert len(score.observed) == 1
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert all(wer == 0.0 for wer in score.wer)
    for text in score.observed:
        assert "hello" in text.lower()
        assert "world" in text.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_similar_sounding_names_stay_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_named,
    )
    from dcent_voice.personalization import PersonalizationStore

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    engine = VoiceEngine(config, personalization=store)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_named(
            asr,
            config,
            store,
            scratch_root=tmp_path / "hold-named",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_named"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.similar_sounding is True
    assert score.spoken == "brand"
    assert score.written == "Brandd"
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "Brandd" not in score.before
    assert "Brand" in score.before
    assert score.before_wer > 0.0
    assert "Brandd" in score.after
    assert score.after_wer == 0.0


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_ramble_rewrite_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_rewrite,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_rewrite(
            asr,
            config,
            scratch_root=tmp_path / "hold-ramble",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_rewrite"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.ramble is True
    assert score.spoken == "five actually six"
    assert score.written == "The meeting is at 6."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "actually" in score.source.lower()
    assert "5" in score.source or "five" in score.source.lower()
    assert "actually" not in score.observed.lower()
    assert "meeting" in score.observed.lower()
    assert "6" in score.observed or "six" in score.observed.lower()
    assert "actually" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_email_rewrite_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_email,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_email(
            asr,
            config,
            scratch_root=tmp_path / "hold-email",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_email"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.email is True
    assert score.spoken == "email style"
    assert score.written == "Could you send a report?"
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "email" in score.source.lower()
    assert "style" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n")
    assert "email style" not in observed.lower()
    assert "could you" in observed.lower()
    assert "thanks" in observed.lower()
    assert "\n\n" in observed
    assert "could you" in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_high_cleanup_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_high,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_high(
            asr,
            config,
            scratch_root=tmp_path / "hold-high",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_high"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.high is True
    assert score.spoken == "cleanup high"
    assert score.written == "We should ship Monday."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "cleanup" in score.source.lower()
    assert "high" in score.source.lower()
    assert "i think" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    assert "cleanup high" not in observed
    assert "high cleanup" not in observed
    assert "i think" not in observed
    assert "monday" in observed
    assert "we should ship" in observed
    assert "i think" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_snippet_expansion_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_snippet,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_snippet(
            asr,
            config,
            scratch_root=tmp_path / "hold-snip",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_snippet"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.snippet is True
    assert score.spoken == "my calendar"
    assert score.written == "https://cal.example/me"
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "my calendar" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    assert "my calendar" not in observed
    assert "https://cal.example/me" in observed
    assert "https://cal.example/me" in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_scratch_that_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_scratch,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_scratch(
            asr,
            config,
            scratch_root=tmp_path / "hold-scratch",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_scratch"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.scratch is True
    assert score.spoken == "scratch that"
    assert score.written == "Send the report."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "scratch that" in score.source.lower()
    assert "hello" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    assert "hello" not in observed
    assert "scratch" not in observed
    assert "report" in observed
    assert "send" in observed
    assert "hello" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_spoken_replace_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_replace,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_replace(
            asr,
            config,
            scratch_root=tmp_path / "hold-replace",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_replace"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.replace is True
    assert score.spoken == "replace Monday with Friday"
    assert score.written == "Ship Friday."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "replace" in score.source.lower()
    assert "monday" in score.source.lower()
    assert "friday" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    assert "monday" not in observed
    assert "replace" not in observed
    assert "friday" in observed
    assert "ship" in observed
    assert "monday" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_spoken_meant_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_meant,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_meant(
            asr,
            config,
            scratch_root=tmp_path / "hold-meant",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_meant"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.meant is True
    assert score.spoken == "no I meant"
    assert score.written == "Meet Alice."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "meant" in score.source.lower()
    assert "bob" in score.source.lower()
    assert "alice" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    assert "bob" not in observed
    assert "meant" not in observed
    assert "alice" in observed
    assert "meet" in observed
    assert "bob" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_press_enter_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_enter,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_enter(
            asr,
            config,
            scratch_root=tmp_path / "hold-enter",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_enter"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.enter is True
    assert score.spoken == "press enter"
    assert score.written == "Hello world."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "press" in score.source.lower()
    assert "enter" in score.source.lower()
    assert "hello" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = observed.lower()
    assert "press enter" not in folded
    assert "hello" in folded
    assert observed.endswith("\n")
    assert "press enter" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_new_paragraph_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_paragraph,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_paragraph(
            asr,
            config,
            scratch_root=tmp_path / "hold-para",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_paragraph"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.paragraph is True
    assert score.spoken == "new paragraph"
    assert score.written == "Hello\n\nWorld"
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "new paragraph" in score.source.lower()
    assert "hello" in score.source.lower()
    assert "world" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = observed.lower()
    assert "new paragraph" not in folded
    assert "hello" in folded
    assert "world" in folded
    assert "\n\n" in observed
    assert "\n\n" in score.rewritten.replace("\r\n", "\n")


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_delete_last_word_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_delete_word,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_delete_word(
            asr,
            config,
            scratch_root=tmp_path / "hold-delw",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_delete_word"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.delete_word is True
    assert score.spoken == "delete last word"
    assert score.written == "Ship Monday."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "delete last word" in score.source.lower()
    assert "monday" in score.source.lower()
    assert "tuesday" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    assert "delete last word" not in observed
    assert "tuesday" not in observed
    assert "monday" in observed
    assert "ship" in observed
    assert "tuesday" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_delete_last_sentence_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_delete_sentence,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_delete_sentence(
            asr,
            config,
            scratch_root=tmp_path / "hold-dels",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_delete_sentence"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.delete_sentence is True
    assert score.spoken == "delete last sentence"
    assert score.written == "The meeting is Monday."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "delete last sentence" in score.source.lower()
    assert "monday" in score.source.lower()
    assert "tuesday" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    assert "delete last sentence" not in observed
    assert "tuesday" not in observed
    assert "monday" in observed
    assert "meeting" in observed
    assert "tuesday" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_delete_last_line_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_delete_line,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_delete_line(
            asr,
            config,
            scratch_root=tmp_path / "hold-dell",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_delete_line"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.delete_line is True
    assert score.spoken == "delete last line"
    assert score.written == "Keep the intro."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "delete last line" in score.source.lower()
    assert "new line" in score.source.lower()
    assert "intro" in score.source.lower()
    assert "outro" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    assert "delete last line" not in observed
    assert "outro" not in observed
    assert "new line" not in observed
    assert "intro" in observed
    assert "outro" not in score.rewritten.lower()


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_new_line_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_newline,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_newline(
            asr,
            config,
            scratch_root=tmp_path / "hold-nl",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_newline"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.newline is True
    assert score.spoken == "new line"
    assert score.written == "Alpha Report.\nBravo Draft."
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "new line" in score.source.lower()
    assert "alpha" in score.source.lower()
    assert "bravo" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = observed.lower()
    assert "new line" not in folded
    assert "alpha" in folded
    assert "bravo" in folded
    assert "\n" in observed
    assert "\n\n" not in observed
    rewritten = score.rewritten.replace("\r\n", "\n")
    assert "\n" in rewritten
    assert "\n\n" not in rewritten


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_acoustic_bullet_list_stays_written(
    tmp_path: Path,
) -> None:
    from pathlib import Path

    import sounddevice as sd

    from dcent_voice.config import load_config
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.integration.windows_hold_release import (
        score_shipped_default_hold_release_acoustic_bullet,
    )

    config = load_config(Path("config.example.toml"), create=False)
    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert "tiny" not in config.current_profile.asr.model.lower()
    engine = VoiceEngine(config)
    try:
        asr = engine.asr
        asr_class = type(asr).__name__
        provider = asr.spec.provider if getattr(asr, "spec", None) else ""
        model = asr.spec.model if getattr(asr, "spec", None) else ""
        score = score_shipped_default_hold_release_acoustic_bullet(
            asr,
            config,
            scratch_root=tmp_path / "hold-bul",
        )
    finally:
        engine.unload()
    default_in = sd.default.device[0]
    assert provider == "parakeet"
    assert "tiny" not in str(model).lower()
    assert "tdt" in str(model).lower()
    assert asr_class == "ParakeetASRProvider"
    assert "Fake" not in asr_class
    assert score.kind == "hold_release_acoustic_bullet"
    assert score.audio_bypass is False
    assert score.default_microphone is False
    assert score.bullet is True
    assert score.spoken == "bullet point"
    assert score.written == "Shopping list.\n- milk\n- rice"
    assert score.input_device != default_in
    assert all(rms >= 0.01 for rms in score.captured_rms)
    assert "bullet point" in score.source.lower()
    assert "next bullet" in score.source.lower()
    assert "milk" in score.source.lower()
    assert "rice" in score.source.lower()
    observed = score.observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = observed.lower()
    assert "bullet point" not in folded
    assert "next bullet" not in folded
    assert "milk" in folded
    assert "rice" in folded
    assert re.search(r"\n-\s*milk", folded)
    assert re.search(r"\n-\s*rice", folded)
    assert "\n\n" not in observed
    rewritten = score.rewritten.replace("\r\n", "\n").lower()
    assert re.search(r"\n-\s*milk", rewritten)
    assert "\n\n" not in rewritten


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_silent_install_lands_onedir(
    tmp_path: Path,
) -> None:
    import hashlib
    import os
    from pathlib import Path

    from dcent_voice.package_windows import score_shipped_default_silent_install

    setup = Path("dist/DCENT_Voice-Setup.exe")
    onedir = Path("dist/DCENT_Voice/dcent-voice.exe")
    dest = tmp_path / "silent-dest"
    local_exe = Path(os.environ["LOCALAPPDATA"]) / "DCENT_Voice" / "dcent-voice.exe"
    shortcut = (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "DCENT_Voice"
        / "DCENT_Voice.lnk"
    )
    before_local = hashlib.sha256(local_exe.read_bytes()).hexdigest() if local_exe.is_file() else ""
    before_shortcut = shortcut.stat().st_mtime if shortcut.is_file() else None
    score = score_shipped_default_silent_install(setup, onedir, dest)
    after_local = hashlib.sha256(local_exe.read_bytes()).hexdigest() if local_exe.is_file() else ""
    after_shortcut = shortcut.stat().st_mtime if shortcut.is_file() else None
    assert score.kind == "silent_install"
    assert score.returncode == 0
    assert score.onedir_match is True
    assert score.dest_size > 0
    assert score.dest_sha256 == score.onedir_sha256
    assert Path(score.dest_exe).is_file()
    assert after_local == before_local
    assert after_shortcut == before_shortcut


@pytest.mark.interactive
@requires_win32_native
def test_shipped_default_silent_uninstall_removes_onedir(
    tmp_path: Path,
) -> None:
    import hashlib
    import os
    from pathlib import Path

    from dcent_voice.package_windows import score_shipped_default_silent_uninstall

    setup = Path("dist/DCENT_Voice-Setup.exe")
    dest = tmp_path / "silent-uninst"
    local_exe = Path(os.environ["LOCALAPPDATA"]) / "DCENT_Voice" / "dcent-voice.exe"
    shortcut = (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "DCENT_Voice"
        / "DCENT_Voice.lnk"
    )
    before_local = hashlib.sha256(local_exe.read_bytes()).hexdigest() if local_exe.is_file() else ""
    before_shortcut = shortcut.stat().st_mtime if shortcut.is_file() else None
    score = score_shipped_default_silent_uninstall(setup, dest)
    after_local = hashlib.sha256(local_exe.read_bytes()).hexdigest() if local_exe.is_file() else ""
    after_shortcut = shortcut.stat().st_mtime if shortcut.is_file() else None
    assert score.kind == "silent_uninstall"
    assert score.install_returncode == 0
    assert score.uninstall_returncode == 0
    assert score.dest_exe_present_after is False
    assert score.dest_removed is True
    assert not (dest / "dcent-voice.exe").is_file()
    assert after_local == before_local
    assert after_shortcut == before_shortcut


def test_cli_transcribe_writing_style_keeps_app_scoped_learned_written_forms(
    tmp_path: Path, capsys
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.app import (
        build_parser,
        reset_cli_transcribe_sticky,
        run_transcribe_command,
    )
    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    reset_cli_transcribe_sticky()
    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=(),
        snippets=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    matching = build_parser().parse_args(
        ["transcribe", str(wav), "--style", "formal", "--app", "Code.exe"]
    )
    matching.asr_provider = FakeASR("vip")
    matching.personalization = store
    assert run_transcribe_command(config, matching, personalization=store) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    other = build_parser().parse_args(
        ["transcribe", str(wav), "--style", "formal", "--app", "notepad.exe"]
    )
    other.asr_provider = FakeASR("vip")
    other.personalization = store
    assert run_transcribe_command(config, other, personalization=store) == 0
    assert "I'm Ada" not in capsys.readouterr().out
    unscoped = build_parser().parse_args(["transcribe", str(wav), "--style", "formal"])
    unscoped.asr_provider = FakeASR("vip")
    unscoped.personalization = store
    assert run_transcribe_command(config, unscoped, personalization=store) == 0
    assert "I'm Ada" not in capsys.readouterr().out
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_transcribe_stream_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store, polish=False)
    audio = np.ones(1600, dtype=np.float32)
    events = list(engine.transcribe_stream([audio, audio], style="formal", polish=False))
    final = events[-1]
    assert final["type"] == "final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    assert final["raw"] == "vip"
    control = list(
        VoiceEngine(
            replace(config, dictionary=()),
            asr=FakeASR("vip"),
            personalization=store,
            polish=False,
        ).transcribe_stream([audio, audio], style="formal", polish=False)
    )[-1]
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_headless_transcribe_stream_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store, polish=False)
    audio = np.ones(1600, dtype=np.float32)
    events = list(engine.transcribe_stream([audio, audio], style="formal"))
    final = events[-1]
    assert final["type"] == "final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    assert final["raw"] == "vip"
    control = list(
        VoiceEngine(
            replace(config, dictionary=()),
            asr=FakeASR("vip"),
            personalization=store,
            polish=False,
        ).transcribe_stream([audio, audio], style="formal")
    )[-1]
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_headless_transcribe_stream_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store, polish=False)
    audio = np.ones(1600, dtype=np.float32)
    list(engine.transcribe_stream([audio, audio], style="formal"))
    final = list(engine.transcribe_stream([audio, audio]))[-1]
    assert final["type"] == "final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    assert final["raw"] == "vip"
    control_engine = VoiceEngine(
        replace(config, dictionary=()),
        asr=FakeASR("vip"),
        personalization=store,
        polish=False,
    )
    list(control_engine.transcribe_stream([audio, audio], style="formal"))
    control = list(control_engine.transcribe_stream([audio, audio]))[-1]
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_headless_transcribe_stream_writing_style_keeps_snippet_expansions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("sig"), personalization=store, polish=False)
    audio = np.ones(1600, dtype=np.float32)
    list(engine.transcribe_stream([audio, audio], style="formal"))
    final = list(engine.transcribe_stream([audio, audio]))[-1]
    assert final["type"] == "final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    assert final["raw"] == "sig"
    control_engine = VoiceEngine(
        replace(config, snippets=()),
        asr=FakeASR("sig"),
        personalization=store,
        polish=False,
    )
    list(control_engine.transcribe_stream([audio, audio], style="formal"))
    control = list(control_engine.transcribe_stream([audio, audio]))[-1]
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_headless_transcribe_stream_writing_style_keeps_snippet_expansions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("sig"), personalization=store, polish=False)
    audio = np.ones(1600, dtype=np.float32)
    events = list(engine.transcribe_stream([audio, audio], style="formal"))
    final = events[-1]
    assert final["type"] == "final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    assert final["raw"] == "sig"
    control = list(
        VoiceEngine(
            replace(config, snippets=()),
            asr=FakeASR("sig"),
            personalization=store,
            polish=False,
        ).transcribe_stream([audio, audio], style="formal")
    )[-1]
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_transcribe_stream_writing_style_keeps_snippet_expansions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("sig"), personalization=store, polish=False)
    audio = np.ones(1600, dtype=np.float32)
    events = list(engine.transcribe_stream([audio, audio], style="formal", polish=False))
    final = events[-1]
    assert final["type"] == "final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    assert final["raw"] == "sig"
    control = list(
        VoiceEngine(
            replace(config, snippets=()),
            asr=FakeASR("sig"),
            personalization=store,
            polish=False,
        ).transcribe_stream([audio, audio], style="formal", polish=False)
    )[-1]
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_transcribe_stream_writing_style_keeps_app_scoped_learned_written_forms(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=(),
        snippets=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    audio = np.ones(1600, dtype=np.float32)
    engine = VoiceEngine(config, asr=FakeASR("vip"), personalization=store)
    matching = list(
        engine.transcribe_stream([audio, audio], style="formal", app_context="Code.exe")
    )[-1]
    assert matching["type"] == "final"
    assert "I'm Ada" in matching["text"]
    assert "I am Ada" not in matching["text"]
    assert matching["raw"] == "vip"
    other = list(
        engine.transcribe_stream([audio, audio], style="formal", app_context="notepad.exe")
    )[-1]
    assert "I'm Ada" not in other["text"]
    omitted = list(engine.transcribe_stream([audio, audio], style="formal"))[-1]
    assert "I'm Ada" not in omitted["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_transcribe_writing_style_keeps_snippet_expansions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=FakeASR("sig"), personalization=store)
    audio = np.zeros(1600, dtype=np.float32)
    result = engine.transcribe(audio, polish=False, style="formal")
    assert "I'm Ada" in result.text
    assert "I am Ada" not in result.text
    assert result.raw == "sig"
    control = VoiceEngine(
        replace(config, snippets=()),
        asr=FakeASR("sig"),
        personalization=store,
    ).transcribe(audio, polish=False, style="formal")
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_cli_transcribe_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path, capsys
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.app import build_parser, run_transcribe_command
    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    args = build_parser().parse_args(["transcribe", str(wav), "--style", "formal", "--no-polish"])
    args.asr_provider = FakeASR("vip")
    args.personalization = store
    assert run_transcribe_command(config, args) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = replace(config, dictionary=())
    args.asr_provider = FakeASR("vip")
    assert run_transcribe_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_cli_transcribe_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path, capsys
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.app import build_parser, run_transcribe_command
    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    args = build_parser().parse_args(["transcribe", str(wav), "--style", "formal"])
    args.asr_provider = FakeASR("vip")
    args.personalization = store
    assert run_transcribe_command(config, args) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = replace(config, dictionary=())
    args.asr_provider = FakeASR("vip")
    assert run_transcribe_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_cli_transcribe_writing_style_keeps_dictionary_written_forms(
    tmp_path: Path, capsys
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.app import build_parser, run_transcribe_command
    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        dictionary=dictionary,
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    first = build_parser().parse_args(["transcribe", str(wav), "--style", "formal", "--no-polish"])
    first.asr_provider = FakeASR("vip")
    first.personalization = store
    assert run_transcribe_command(config, first) == 0
    capsys.readouterr()
    second = build_parser().parse_args(["transcribe", str(wav)])
    second.asr_provider = FakeASR("vip")
    second.personalization = store
    assert run_transcribe_command(config, second) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    control_config = replace(config, dictionary=())
    control_first = build_parser().parse_args(
        ["transcribe", str(wav), "--style", "formal", "--no-polish"]
    )
    control_first.asr_provider = FakeASR("vip")
    control_first.personalization = store
    from dcent_voice.app import reset_cli_transcribe_sticky

    reset_cli_transcribe_sticky()
    assert run_transcribe_command(control_config, control_first) == 0
    capsys.readouterr()
    control_second = build_parser().parse_args(["transcribe", str(wav)])
    control_second.asr_provider = FakeASR("vip")
    control_second.personalization = store
    assert run_transcribe_command(control_config, control_second) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_cli_transcribe_writing_style_keeps_snippet_expansions(
    tmp_path: Path, capsys
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.app import build_parser, reset_cli_transcribe_sticky, run_transcribe_command
    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    first = build_parser().parse_args(["transcribe", str(wav), "--style", "formal", "--no-polish"])
    first.asr_provider = FakeASR("sig")
    first.personalization = store
    assert run_transcribe_command(config, first) == 0
    capsys.readouterr()
    second = build_parser().parse_args(["transcribe", str(wav)])
    second.asr_provider = FakeASR("sig")
    second.personalization = store
    assert run_transcribe_command(config, second) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    control_config = replace(config, snippets=())
    control_first = build_parser().parse_args(
        ["transcribe", str(wav), "--style", "formal", "--no-polish"]
    )
    control_first.asr_provider = FakeASR("sig")
    control_first.personalization = store
    reset_cli_transcribe_sticky()
    assert run_transcribe_command(control_config, control_first) == 0
    capsys.readouterr()
    control_second = build_parser().parse_args(["transcribe", str(wav)])
    control_second.asr_provider = FakeASR("sig")
    control_second.personalization = store
    assert run_transcribe_command(control_config, control_second) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_cli_transcribe_writing_style_keeps_snippet_expansions(
    tmp_path: Path, capsys
) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.app import build_parser, run_transcribe_command
    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    args = build_parser().parse_args(["transcribe", str(wav), "--style", "formal"])
    args.asr_provider = FakeASR("sig")
    args.personalization = store
    assert run_transcribe_command(config, args) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = replace(config, snippets=())
    args.asr_provider = FakeASR("sig")
    assert run_transcribe_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_cli_transcribe_writing_style_keeps_snippet_expansions(tmp_path: Path, capsys) -> None:
    import wave
    from dataclasses import replace
    from pathlib import Path

    from dcent_voice.app import build_parser, run_transcribe_command
    from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore

    class FakeASR(ASRProvider):
        locality = Locality.LOCAL

        def __init__(self, text: str) -> None:
            self.text = text

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            return TranscriptResult(
                text=self.text, language="en", duration_s=1.0, asr_latency_s=0.01
            )

    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        snippets=snippets,
        dictionary=(),
    )
    store = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    args = build_parser().parse_args(["transcribe", str(wav), "--style", "formal", "--no-polish"])
    args.asr_provider = FakeASR("sig")
    args.personalization = store
    assert run_transcribe_command(config, args) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = replace(config, snippets=())
    args.asr_provider = FakeASR("sig")
    assert run_transcribe_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_compose_keeps_dictionary_written_forms(fake_asr) -> None:
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "um say um VIP Corp"
    dictionary = (VocabEntry(spoken="vip", written="um VIP Corp"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    body = engine.transcribe(TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True))
    assert "um VIP Corp" in body["cleaned"]
    assert body["raw"] == "um say um VIP Corp"
    control = ServiceEngine(asr=fake_asr).transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True)
    )
    assert "um VIP Corp" not in control["cleaned"]


def test_cli_compose_keeps_snippet_expansions(capsys) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from dcent_voice.app import run_compose_command

    snippets = (SnippetEntry(spoken="my email", expansion="um ops@example.com"),)
    args = Namespace(text=["um", "send", "my", "email"], style="plain", cleanup_level="medium")
    config = SimpleNamespace(snippets=snippets, dictionary=())
    assert run_compose_command(config, args) == 0
    out = capsys.readouterr().out
    assert "um ops@example.com" in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    assert run_compose_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "um ops@example.com" not in control


def test_cli_compose_keeps_dictionary_written_forms(capsys) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from dcent_voice.app import run_compose_command

    dictionary = (VocabEntry(spoken="vip", written="um VIP Corp"),)
    args = Namespace(text=["um", "say", "um", "VIP", "Corp"], style="plain", cleanup_level="medium")
    config = SimpleNamespace(snippets=(), dictionary=dictionary)
    assert run_compose_command(config, args) == 0
    out = capsys.readouterr().out
    assert "um VIP Corp" in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    assert run_compose_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "um VIP Corp" not in control


def test_local_compose_applies_dictionary_terms() -> None:
    dictionary = (VocabEntry(spoken="vip", written="um VIP Corp"),)
    text = compose_dictation("um say vip", dictionary=dictionary)
    assert "um VIP Corp" in text
    assert "um VIP Corp" not in compose_dictation("um say vip")


def test_ade_compose_applies_dictionary_terms(fake_asr) -> None:
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "um say vip"
    dictionary = (VocabEntry(spoken="vip", written="um VIP Corp"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    body = engine.transcribe(TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True))
    assert "um VIP Corp" in body["cleaned"]
    assert body["raw"] == "um say vip"
    control = ServiceEngine(asr=fake_asr).transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True)
    )
    assert "um VIP Corp" not in control["cleaned"]


def test_ade_writing_style_keeps_dictionary_written_forms(fake_asr) -> None:
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    body = engine.transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True, style="formal")
    )
    assert "I'm Ada" in body["cleaned"]
    assert "I am Ada" not in body["cleaned"]
    assert body["raw"] == "vip"
    control = ServiceEngine(asr=fake_asr).transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=True, style="formal")
    )
    assert "I'm Ada" not in control["cleaned"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_ade_writing_style_keeps_dictionary_written_forms(fake_asr) -> None:
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    body = engine.transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False, style="formal")
    )
    assert "I'm Ada" in body["cleaned"]
    assert "I am Ada" not in body["cleaned"]
    assert body["raw"] == "vip"
    control = ServiceEngine(asr=fake_asr).transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False, style="formal")
    )
    assert "I'm Ada" not in control["cleaned"]
    raw = ServiceEngine(asr=fake_asr, dictionary=dictionary).transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False)
    )
    assert raw["cleaned"] == "vip"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_ade_writing_style_keeps_dictionary_written_forms(
    fake_asr,
) -> None:

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    engine.transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False, style="formal")
    )
    body = engine.transcribe(TranscribeRequest(audio=[0.1] * 16000, samplerate=16000))
    assert "I'm Ada" in body["cleaned"]
    assert "I am Ada" not in body["cleaned"]
    assert body["raw"] == "vip"
    control_engine = ServiceEngine(asr=fake_asr)
    control_engine.transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False, style="formal")
    )
    control = control_engine.transcribe(TranscribeRequest(audio=[0.1] * 16000, samplerate=16000))
    assert "I'm Ada" not in control["cleaned"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_ade_writing_style_keeps_snippet_expansions(
    fake_asr,
) -> None:

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, snippets=snippets, dictionary=())
    engine.transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False, style="formal")
    )
    body = engine.transcribe(TranscribeRequest(audio=[0.1] * 16000, samplerate=16000))
    assert "I'm Ada" in body["cleaned"]
    assert "I am Ada" not in body["cleaned"]
    assert body["raw"] == "sig"
    control_engine = ServiceEngine(asr=fake_asr)
    control_engine.transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False, style="formal")
    )
    control = control_engine.transcribe(TranscribeRequest(audio=[0.1] * 16000, samplerate=16000))
    assert "I'm Ada" not in control["cleaned"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_ade_writing_style_keeps_snippet_expansions(fake_asr) -> None:
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, TranscribeRequest

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, snippets=snippets)
    body = engine.transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False, style="formal")
    )
    assert "I'm Ada" in body["cleaned"]
    assert "I am Ada" not in body["cleaned"]
    assert body["raw"] == "sig"
    control = ServiceEngine(asr=fake_asr).transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False, style="formal")
    )
    assert "I'm Ada" not in control["cleaned"]
    raw = ServiceEngine(asr=fake_asr, snippets=snippets).transcribe(
        TranscribeRequest(audio=[0.1] * 16000, samplerate=16000, polish=False)
    )
    assert raw["cleaned"] == "sig"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_stream_writing_style_keeps_dictionary_written_forms(fake_asr) -> None:
    import numpy as np

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine
    from dcent_voice.service.streaming import StreamingSession

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    session = StreamingSession(ServiceEngine(asr=fake_asr, dictionary=dictionary))
    audio = np.ones(16000, dtype=np.float32) * 0.1
    message = session.push(audio, final=True, style="formal")
    assert message.type == "final"
    assert "I'm Ada" in message.text
    assert "I am Ada" not in message.text
    assert message.result is not None
    assert message.result["raw"] == "vip"
    control = StreamingSession(ServiceEngine(asr=fake_asr)).push(audio, final=True, style="formal")
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_stream_writing_style_keeps_snippet_expansions(fake_asr) -> None:
    import numpy as np

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine
    from dcent_voice.service.streaming import StreamingSession

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    session = StreamingSession(ServiceEngine(asr=fake_asr, snippets=snippets))
    audio = np.ones(16000, dtype=np.float32) * 0.1
    message = session.push(audio, final=True, style="formal")
    assert message.type == "final"
    assert "I'm Ada" in message.text
    assert "I am Ada" not in message.text
    assert message.result is not None
    assert message.result["raw"] == "sig"
    control = StreamingSession(ServiceEngine(asr=fake_asr)).push(audio, final=True, style="formal")
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_ade_stream_writing_style_keeps_dictionary_written_forms(
    fake_asr,
) -> None:
    import numpy as np

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine
    from dcent_voice.service.streaming import StreamingSession

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    session = StreamingSession(ServiceEngine(asr=fake_asr, dictionary=dictionary))
    audio = np.ones(16000, dtype=np.float32) * 0.1
    message = session.push(audio, final=True, style="formal", polish=False)
    assert message.type == "final"
    assert "I'm Ada" in message.text
    assert "I am Ada" not in message.text
    assert message.result is not None
    assert message.result["raw"] == "vip"
    control = StreamingSession(ServiceEngine(asr=fake_asr)).push(
        audio, final=True, style="formal", polish=False
    )
    assert "I'm Ada" not in control.text
    raw = StreamingSession(ServiceEngine(asr=fake_asr, dictionary=dictionary)).push(
        audio, final=True, polish=False
    )
    assert raw.text == "vip"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_ade_stream_writing_style_keeps_dictionary_written_forms(
    fake_asr,
) -> None:
    import numpy as np

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine
    from dcent_voice.service.streaming import StreamingSession

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    session = StreamingSession(ServiceEngine(asr=fake_asr, dictionary=dictionary))
    audio = np.ones(16000, dtype=np.float32) * 0.1
    session.push(audio, final=False, style="formal", polish=False)
    message = session.push(audio, final=True)
    assert message.type == "final"
    assert "I'm Ada" in message.text
    assert "I am Ada" not in message.text
    assert message.result is not None
    assert message.result["raw"] == "vip"
    control = StreamingSession(ServiceEngine(asr=fake_asr))
    control.push(audio, final=False, style="formal", polish=False)
    control_msg = control.push(audio, final=True)
    assert "I'm Ada" not in control_msg.text
    raw = StreamingSession(ServiceEngine(asr=fake_asr, dictionary=dictionary))
    raw.push(audio, final=False, polish=False)
    raw_msg = raw.push(audio, final=True)
    assert raw_msg.text == "vip"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_ade_stream_writing_style_keeps_snippet_expansions(
    fake_asr,
) -> None:
    import numpy as np

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine
    from dcent_voice.service.streaming import StreamingSession

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    session = StreamingSession(ServiceEngine(asr=fake_asr, snippets=snippets))
    audio = np.ones(16000, dtype=np.float32) * 0.1
    session.push(audio, final=False, style="formal", polish=False)
    message = session.push(audio, final=True)
    assert message.type == "final"
    assert "I'm Ada" in message.text
    assert "I am Ada" not in message.text
    assert message.result is not None
    assert message.result["raw"] == "sig"
    control = StreamingSession(ServiceEngine(asr=fake_asr))
    control.push(audio, final=False, style="formal", polish=False)
    control_msg = control.push(audio, final=True)
    assert "I'm Ada" not in control_msg.text
    raw = StreamingSession(ServiceEngine(asr=fake_asr, snippets=snippets))
    raw.push(audio, final=False, polish=False)
    raw_msg = raw.push(audio, final=True)
    assert raw_msg.text == "sig"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_ade_stream_writing_style_keeps_snippet_expansions(
    fake_asr,
) -> None:
    import numpy as np

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine
    from dcent_voice.service.streaming import StreamingSession

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    session = StreamingSession(ServiceEngine(asr=fake_asr, snippets=snippets))
    audio = np.ones(16000, dtype=np.float32) * 0.1
    message = session.push(audio, final=True, style="formal", polish=False)
    assert message.type == "final"
    assert "I'm Ada" in message.text
    assert "I am Ada" not in message.text
    assert message.result is not None
    assert message.result["raw"] == "sig"
    control = StreamingSession(ServiceEngine(asr=fake_asr)).push(
        audio, final=True, style="formal", polish=False
    )
    assert "I'm Ada" not in control.text
    raw = StreamingSession(ServiceEngine(asr=fake_asr, snippets=snippets)).push(
        audio, final=True, polish=False
    )
    assert raw.text == "sig"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_stream_writing_style_keeps_dictionary_written_forms(fake_asr, tmp_path) -> None:
    from dataclasses import replace

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = load_config(__import__("pathlib").Path("config.example.toml"), create=False)
    config = replace(config, dictionary=dictionary, snippets=())
    personalization = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=fake_asr, personalization=personalization, polish=False)
    audio = np.ones(16000, dtype=np.float32) * 0.1
    event = engine.open_stream(style="formal", polish=False).push(audio, final=True)
    assert event.type == "final"
    assert "I'm Ada" in event.text
    assert "I am Ada" not in event.text
    assert event.raw == "vip"
    control = (
        VoiceEngine(
            replace(config, dictionary=()),
            asr=fake_asr,
            personalization=personalization,
            polish=False,
        )
        .open_stream(style="formal", polish=False)
        .push(audio, final=True)
    )
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_headless_stream_writing_style_keeps_dictionary_written_forms(
    fake_asr, tmp_path
) -> None:
    from dataclasses import replace

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = load_config(__import__("pathlib").Path("config.example.toml"), create=False)
    config = replace(config, dictionary=dictionary, snippets=())
    personalization = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=fake_asr, personalization=personalization, polish=False)
    audio = np.ones(16000, dtype=np.float32) * 0.1
    event = engine.open_stream(style="formal").push(audio, final=True)
    assert event.type == "final"
    assert "I'm Ada" in event.text
    assert "I am Ada" not in event.text
    assert event.raw == "vip"
    control = (
        VoiceEngine(
            replace(config, dictionary=()),
            asr=fake_asr,
            personalization=personalization,
            polish=False,
        )
        .open_stream(style="formal")
        .push(audio, final=True)
    )
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_headless_stream_writing_style_keeps_dictionary_written_forms(
    fake_asr, tmp_path
) -> None:
    from dataclasses import replace

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = load_config(__import__("pathlib").Path("config.example.toml"), create=False)
    config = replace(config, dictionary=dictionary, snippets=())
    personalization = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=fake_asr, personalization=personalization, polish=False)
    audio = np.ones(16000, dtype=np.float32) * 0.1
    session = engine.open_stream()
    session.push(audio, final=False, style="formal", polish=False)
    event = session.push(audio, final=True)
    assert event.type == "final"
    assert "I'm Ada" in event.text
    assert "I am Ada" not in event.text
    assert event.raw == "vip"
    control_engine = VoiceEngine(
        replace(config, dictionary=()),
        asr=fake_asr,
        personalization=personalization,
        polish=False,
    )
    control = control_engine.open_stream()
    control.push(audio, final=False, style="formal", polish=False)
    control_msg = control.push(audio, final=True)
    assert "I'm Ada" not in control_msg.text
    raw = engine.open_stream()
    raw.push(audio, final=False, polish=False)
    raw_msg = raw.push(audio, final=True)
    assert raw_msg.text == "vip" or raw_msg.raw == "vip"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_headless_stream_writing_style_keeps_dictionary_written_forms(
    fake_asr, tmp_path
) -> None:
    from dataclasses import replace

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = load_config(__import__("pathlib").Path("config.example.toml"), create=False)
    config = replace(config, dictionary=dictionary, snippets=())
    personalization = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=fake_asr, personalization=personalization, polish=False)
    audio = np.ones(16000, dtype=np.float32) * 0.1
    session = engine.open_stream()
    session.push(audio, final=False, style="formal")
    event = session.push(audio, final=True)
    assert event.type == "final"
    assert "I'm Ada" in event.text
    assert "I am Ada" not in event.text
    assert event.raw == "vip"
    control_engine = VoiceEngine(
        replace(config, dictionary=()),
        asr=fake_asr,
        personalization=personalization,
        polish=False,
    )
    control = control_engine.open_stream()
    control.push(audio, final=False, style="formal")
    control_msg = control.push(audio, final=True)
    assert "I'm Ada" not in control_msg.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_headless_stream_writing_style_keeps_snippet_expansions(
    fake_asr, tmp_path
) -> None:
    from dataclasses import replace

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = load_config(__import__("pathlib").Path("config.example.toml"), create=False)
    config = replace(config, dictionary=(), snippets=snippets)
    personalization = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=fake_asr, personalization=personalization, polish=False)
    audio = np.ones(16000, dtype=np.float32) * 0.1
    session = engine.open_stream()
    session.push(audio, final=False, style="formal")
    event = session.push(audio, final=True)
    assert event.type == "final"
    assert "I'm Ada" in event.text
    assert "I am Ada" not in event.text
    assert event.raw == "sig"
    control_engine = VoiceEngine(
        replace(config, snippets=()),
        asr=fake_asr,
        personalization=personalization,
        polish=False,
    )
    control = control_engine.open_stream()
    control.push(audio, final=False, style="formal")
    control_msg = control.push(audio, final=True)
    assert "I'm Ada" not in control_msg.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_headless_stream_writing_style_keeps_snippet_expansions(fake_asr, tmp_path) -> None:
    from dataclasses import replace

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = load_config(__import__("pathlib").Path("config.example.toml"), create=False)
    config = replace(config, dictionary=(), snippets=snippets)
    personalization = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=fake_asr, personalization=personalization, polish=False)
    audio = np.ones(16000, dtype=np.float32) * 0.1
    session = engine.open_stream()
    session.push(audio, final=False, style="formal", polish=False)
    event = session.push(audio, final=True)
    assert event.type == "final"
    assert "I'm Ada" in event.text
    assert "I am Ada" not in event.text
    assert event.raw == "sig"
    control_engine = VoiceEngine(
        replace(config, snippets=()),
        asr=fake_asr,
        personalization=personalization,
        polish=False,
    )
    control = control_engine.open_stream()
    control.push(audio, final=False, style="formal", polish=False)
    control_msg = control.push(audio, final=True)
    assert "I'm Ada" not in control_msg.text
    raw = engine.open_stream()
    raw.push(audio, final=False, polish=False)
    raw_msg = raw.push(audio, final=True)
    assert raw_msg.text == "sig" or raw_msg.raw == "sig"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_headless_stream_writing_style_keeps_snippet_expansions(
    fake_asr, tmp_path
) -> None:
    from dataclasses import replace

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = load_config(__import__("pathlib").Path("config.example.toml"), create=False)
    config = replace(config, dictionary=(), snippets=snippets)
    personalization = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=fake_asr, personalization=personalization, polish=False)
    audio = np.ones(16000, dtype=np.float32) * 0.1
    event = engine.open_stream(style="formal").push(audio, final=True)
    assert event.type == "final"
    assert "I'm Ada" in event.text
    assert "I am Ada" not in event.text
    assert event.raw == "sig"
    control = (
        VoiceEngine(
            replace(config, snippets=()),
            asr=fake_asr,
            personalization=personalization,
            polish=False,
        )
        .open_stream(style="formal")
        .push(audio, final=True)
    )
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_headless_stream_writing_style_keeps_snippet_expansions(fake_asr, tmp_path) -> None:
    from dataclasses import replace

    import numpy as np

    from dcent_voice.config import load_config
    from dcent_voice.dictation.style import apply_style
    from dcent_voice.engine import VoiceEngine
    from dcent_voice.personalization import PersonalizationStore

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = load_config(__import__("pathlib").Path("config.example.toml"), create=False)
    config = replace(config, dictionary=(), snippets=snippets)
    personalization = PersonalizationStore(tmp_path / "p.json", enabled=False, learn=False)
    engine = VoiceEngine(config, asr=fake_asr, personalization=personalization, polish=False)
    audio = np.ones(16000, dtype=np.float32) * 0.1
    event = engine.open_stream(style="formal", polish=False).push(audio, final=True)
    assert event.type == "final"
    assert "I'm Ada" in event.text
    assert "I am Ada" not in event.text
    assert event.raw == "sig"
    control = (
        VoiceEngine(
            replace(config, snippets=()),
            asr=fake_asr,
            personalization=personalization,
            polish=False,
        )
        .open_stream(style="formal", polish=False)
        .push(audio, final=True)
    )
    assert "I'm Ada" not in control.text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_dvap_stream_writing_style_keeps_dictionary_written_forms(fake_asr) -> None:
    import numpy as np
    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    client = TestClient(app)
    pcm = (np.ones(16000, dtype="<i2") * 1000).tobytes()
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w186",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w186"})
        final = websocket.receive_json()
    assert final["type"] == "stt.final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    control_engine = ServiceEngine(asr=fake_asr)
    control_app = create_app(control_engine)
    add_dvap_websocket(control_app, control_engine)
    with TestClient(control_app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w186c",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w186c"})
        control = websocket.receive_json()
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_dvap_stream_writing_style_keeps_snippet_expansions(fake_asr) -> None:
    import numpy as np
    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, snippets=snippets)
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    client = TestClient(app)
    pcm = (np.ones(16000, dtype="<i2") * 1000).tobytes()
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w187",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w187"})
        final = websocket.receive_json()
    assert final["type"] == "stt.final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    control_engine = ServiceEngine(asr=fake_asr)
    control_app = create_app(control_engine)
    add_dvap_websocket(control_app, control_engine)
    with TestClient(control_app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w187c",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w187c"})
        control = websocket.receive_json()
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_dvap_stream_writing_style_keeps_dictionary_written_forms(
    fake_asr,
) -> None:
    import numpy as np
    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    client = TestClient(app)
    pcm = (np.ones(16000, dtype="<i2") * 1000).tobytes()
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w188",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w188"})
        final = websocket.receive_json()
    assert final["type"] == "stt.final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    control_engine = ServiceEngine(asr=fake_asr)
    control_app = create_app(control_engine)
    add_dvap_websocket(control_app, control_engine)
    with TestClient(control_app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w188c",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w188c"})
        control = websocket.receive_json()
    assert "I'm Ada" not in control["text"]
    raw_engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    raw_app = create_app(raw_engine)
    add_dvap_websocket(raw_app, raw_engine)
    with TestClient(raw_app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w188r",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w188r"})
        raw = websocket.receive_json()
    assert raw["text"] == "vip"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_dvap_stream_writing_style_keeps_dictionary_written_forms(
    fake_asr,
) -> None:
    import numpy as np
    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello

    fake_asr.text = "vip"
    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary)
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    client = TestClient(app)
    pcm = (np.ones(16000, dtype="<i2") * 1000).tobytes()
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w212",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w212"})
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w212b",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w212b"})
        final = websocket.receive_json()
    assert final["type"] == "stt.final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    control_engine = ServiceEngine(asr=fake_asr)
    control_app = create_app(control_engine)
    add_dvap_websocket(control_app, control_engine)
    with TestClient(control_app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w212c",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w212c"})
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w212d",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w212d"})
        control = websocket.receive_json()
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_dvap_stream_writing_style_keeps_snippet_expansions(
    fake_asr,
) -> None:
    import numpy as np
    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, snippets=snippets, dictionary=())
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    client = TestClient(app)
    pcm = (np.ones(16000, dtype="<i2") * 1000).tobytes()
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w213",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w213"})
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w213b",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w213b"})
        final = websocket.receive_json()
    assert final["type"] == "stt.final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    control_engine = ServiceEngine(asr=fake_asr)
    control_app = create_app(control_engine)
    add_dvap_websocket(control_app, control_engine)
    with TestClient(control_app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w213c",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w213c"})
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w213d",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w213d"})
        control = websocket.receive_json()
    assert "I'm Ada" not in control["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_dvap_stream_writing_style_keeps_snippet_expansions(
    fake_asr,
) -> None:
    import numpy as np
    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello

    fake_asr.text = "sig"
    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, snippets=snippets)
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    client = TestClient(app)
    pcm = (np.ones(16000, dtype="<i2") * 1000).tobytes()
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w189",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w189"})
        final = websocket.receive_json()
    assert final["type"] == "stt.final"
    assert "I'm Ada" in final["text"]
    assert "I am Ada" not in final["text"]
    control_engine = ServiceEngine(asr=fake_asr)
    control_app = create_app(control_engine)
    add_dvap_websocket(control_app, control_engine)
    with TestClient(control_app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w189c",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "style": "formal",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w189c"})
        control = websocket.receive_json()
    assert "I'm Ada" not in control["text"]
    raw_engine = ServiceEngine(asr=fake_asr, snippets=snippets)
    raw_app = create_app(raw_engine)
    add_dvap_websocket(raw_app, raw_engine)
    with TestClient(raw_app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "w189r",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "polish": False,
            }
        )
        websocket.send_bytes(pcm)
        websocket.send_json({"type": "audio.in.end", "requestId": "w189r"})
        raw = websocket.receive_json()
    assert raw["text"] == "sig"
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_dvap_compose_writing_style_keeps_dictionary_written_forms(fake_asr) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello, compose_text

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=dictionary, snippets=())
    composed = compose_text(engine, {"text": "vip", "style": "formal"})
    assert composed["type"] == "text.composed"
    assert "I'm Ada" in composed["text"]
    assert "I am Ada" not in composed["text"]
    control = compose_text(
        ServiceEngine(asr=fake_asr, dictionary=(), snippets=()),
        {"text": "vip", "style": "formal"},
    )
    assert "I'm Ada" not in control["text"]
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    with TestClient(app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("text.compose",)))
        welcome = websocket.receive_json()
        assert "text.compose" in welcome["capabilities"]
        websocket.send_json({"type": "text.compose", "text": "vip", "style": "formal"})
        body = websocket.receive_json()
    assert body["type"] == "text.composed"
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_dvap_compose_writing_style_keeps_snippet_expansions(fake_asr) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello, compose_text

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    engine = ServiceEngine(asr=fake_asr, dictionary=(), snippets=snippets)
    composed = compose_text(engine, {"text": "sig", "style": "formal"})
    assert composed["type"] == "text.composed"
    assert "I'm Ada" in composed["text"]
    assert "I am Ada" not in composed["text"]
    control = compose_text(
        ServiceEngine(asr=fake_asr, dictionary=(), snippets=()),
        {"text": "sig", "style": "formal"},
    )
    assert "I'm Ada" not in control["text"]
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    with TestClient(app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("text.compose",)))
        welcome = websocket.receive_json()
        assert "text.compose" in welcome["capabilities"]
        websocket.send_json({"type": "text.compose", "text": "sig", "style": "formal"})
        body = websocket.receive_json()
    assert body["type"] == "text.composed"
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_dvap_compose_writing_style_keeps_learned_written_forms(fake_asr, tmp_path: Path) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello, compose_text

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed")
    assert term is not None
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    composed = compose_text(engine, {"text": "vip", "style": "formal"})
    assert composed["type"] == "text.composed"
    assert "I'm Ada" in composed["text"]
    assert "I am Ada" not in composed["text"]
    empty = PersonalizationStore(tmp_path / "empty.json", enabled=True, learn=True)
    control = compose_text(
        ServiceEngine(
            asr=fake_asr,
            personalization=empty,
            dictionary=(),
            snippets=(),
        ),
        {"text": "vip", "style": "formal"},
    )
    assert "I'm Ada" not in control["text"]
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    with TestClient(app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("text.compose",)))
        welcome = websocket.receive_json()
        assert "text.compose" in welcome["capabilities"]
        websocket.send_json({"type": "text.compose", "text": "vip", "style": "formal"})
        body = websocket.receive_json()
    assert body["type"] == "text.composed"
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_dvap_compose_writing_style_keeps_app_scoped_learned_written_forms(
    fake_asr, tmp_path: Path
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello, compose_text

    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    matching = compose_text(engine, {"text": "vip", "style": "formal", "app_context": "Code.exe"})
    assert matching["type"] == "text.composed"
    assert "I'm Ada" in matching["text"]
    assert "I am Ada" not in matching["text"]
    other = compose_text(engine, {"text": "vip", "style": "formal", "app_context": "notepad.exe"})
    assert "I'm Ada" not in other["text"]
    unscoped = compose_text(engine, {"text": "vip", "style": "formal"})
    assert "I'm Ada" not in unscoped["text"]
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    with TestClient(app).websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("text.compose",)))
        welcome = websocket.receive_json()
        assert "text.compose" in welcome["capabilities"]
        websocket.send_json(
            {
                "type": "text.compose",
                "text": "vip",
                "style": "formal",
                "app_context": "Code.exe",
            }
        )
        body = websocket.receive_json()
    assert body["type"] == "text.composed"
    assert "I'm Ada" in body["text"]
    assert "I am Ada" not in body["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_dvap_stream_writing_style_keeps_app_scoped_learned_written_forms(
    fake_asr, tmp_path: Path
) -> None:

    import numpy as np
    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.dvap import add_dvap_websocket, build_hello

    fake_asr.text = "vip"
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    pcm = (np.ones(16000, dtype="<i2") * 1000).tobytes()

    def _stt_final(app_context: str | None, request_id: str) -> dict:
        begin: dict = {
            "type": "audio.in.begin",
            "requestId": request_id,
            "sampleRate": 16000,
            "channels": 1,
            "encoding": "pcm_s16le",
            "style": "formal",
        }
        if app_context is not None:
            begin["app_context"] = app_context
        with TestClient(app).websocket_connect("/dvap") as websocket:
            websocket.send_json(build_hello(capabilities=("audio.in.stream", "stt.final")))
            websocket.receive_json()
            websocket.send_json(begin)
            websocket.send_bytes(pcm)
            websocket.send_json({"type": "audio.in.end", "requestId": request_id})
            return websocket.receive_json()

    matching = _stt_final("Code.exe", "w239")
    assert matching["type"] == "stt.final"
    assert "I'm Ada" in matching["text"]
    assert "I am Ada" not in matching["text"]
    other = _stt_final("notepad.exe", "w239o")
    assert "I'm Ada" not in other["text"]
    omitted = _stt_final(None, "w239u")
    assert "I'm Ada" not in omitted["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_ade_stream_writing_style_keeps_app_scoped_learned_written_forms(
    fake_asr, tmp_path: Path
) -> None:

    from fastapi.testclient import TestClient

    from dcent_voice.dictation.style import apply_style
    from dcent_voice.personalization import PersonalizationStore
    from dcent_voice.service.api import ServiceEngine, create_app
    from dcent_voice.service.ws import add_stream_websocket

    fake_asr.text = "vip"
    store = PersonalizationStore(tmp_path / "p.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed", app="Code.exe")
    assert term is not None
    engine = ServiceEngine(
        asr=fake_asr,
        personalization=store,
        dictionary=(),
        snippets=(),
    )
    app = create_app(engine)
    add_stream_websocket(app, engine)
    audio = [0.1] * 16000

    def _final(app_context: str | None) -> dict:
        payload: dict = {
            "audio": audio,
            "samplerate": 16000,
            "final": True,
            "style": "formal",
        }
        if app_context is not None:
            payload["app_context"] = app_context
        with TestClient(app).websocket_connect("/stream") as websocket:
            websocket.send_json(payload)
            return websocket.receive_json()

    matching = _final("Code.exe")
    assert matching["type"] == "final"
    assert "I'm Ada" in matching["text"]
    assert "I am Ada" not in matching["text"]
    other = _final("notepad.exe")
    assert "I'm Ada" not in other["text"]
    omitted = _final(None)
    assert "I'm Ada" not in omitted["text"]
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_writing_style_keeps_dictionary_written_forms() -> None:
    from dcent_voice.dictation.style import apply_style

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    text = compose_dictation("vip", dictionary=dictionary, style="formal")
    assert "I'm Ada" in text
    assert "I am Ada" not in text
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_cli_writing_style_keeps_dictionary_written_forms(capsys) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from dcent_voice.app import run_compose_command
    from dcent_voice.dictation.style import apply_style

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    args = Namespace(text=["vip"], style="formal", cleanup_level="medium")
    config = SimpleNamespace(snippets=(), dictionary=dictionary)
    assert run_compose_command(config, args) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    assert run_compose_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_cli_writing_style_keeps_dictionary_written_forms(capsys) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from dcent_voice.app import run_compose_command
    from dcent_voice.dictation.style import apply_style

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    args = Namespace(text=["vip"], style="formal", cleanup_level="medium", no_polish=True)
    config = SimpleNamespace(snippets=(), dictionary=dictionary)
    assert run_compose_command(config, args) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    assert run_compose_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    # --no-polish alone still runs compose (dictionary apply); filler is not stripped.
    filled = Namespace(text=["um", "vip"], style="formal", cleanup_level="medium", no_polish=True)
    assert run_compose_command(config, filled) == 0
    filled_out = capsys.readouterr().out
    assert "I'm Ada" in filled_out
    assert "I am Ada" not in filled_out
    assert "um" in filled_out.lower()
    polished = Namespace(
        text=["um", "vip"], style="formal", cleanup_level="medium", no_polish=False
    )
    assert run_compose_command(config, polished) == 0
    polished_out = capsys.readouterr().out
    assert "I'm Ada" in polished_out
    assert "um" not in polished_out.lower()
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_unpolished_cli_writing_style_keeps_snippet_expansions(capsys) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from dcent_voice.app import run_compose_command
    from dcent_voice.dictation.style import apply_style

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    args = Namespace(text=["sig"], style="formal", cleanup_level="medium", no_polish=True)
    config = SimpleNamespace(snippets=snippets, dictionary=())
    assert run_compose_command(config, args) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    assert run_compose_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    filled = Namespace(text=["um", "sig"], style="formal", cleanup_level="medium", no_polish=True)
    assert run_compose_command(config, filled) == 0
    filled_out = capsys.readouterr().out
    assert "I'm Ada" in filled_out
    assert "I am Ada" not in filled_out
    assert "um" in filled_out.lower()
    polished = Namespace(
        text=["um", "sig"], style="formal", cleanup_level="medium", no_polish=False
    )
    assert run_compose_command(config, polished) == 0
    polished_out = capsys.readouterr().out
    assert "I'm Ada" in polished_out
    assert "um" not in polished_out.lower()
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_cli_compose_writing_style_keeps_snippet_expansions(
    capsys,
) -> None:
    from types import SimpleNamespace

    from dcent_voice.app import (
        build_parser,
        reset_cli_compose_sticky,
        run_compose_command,
    )
    from dcent_voice.dictation.style import apply_style

    snippets = (SnippetEntry(spoken="sig", expansion="I'm Ada"),)
    config = SimpleNamespace(snippets=snippets, dictionary=())
    first = build_parser().parse_args(["compose", "sig", "--style", "formal", "--no-polish"])
    assert run_compose_command(config, first) == 0
    capsys.readouterr()
    second = build_parser().parse_args(["compose", "sig"])
    assert run_compose_command(config, second) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    reset_cli_compose_sticky()
    control_first = build_parser().parse_args(
        ["compose", "sig", "--style", "formal", "--no-polish"]
    )
    assert run_compose_command(empty, control_first) == 0
    capsys.readouterr()
    control_second = build_parser().parse_args(["compose", "sig"])
    assert run_compose_command(empty, control_second) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_sticky_unpolished_cli_compose_writing_style_keeps_dictionary_written_forms(
    capsys,
) -> None:
    from types import SimpleNamespace

    from dcent_voice.app import (
        build_parser,
        reset_cli_compose_sticky,
        run_compose_command,
    )
    from dcent_voice.dictation.style import apply_style

    dictionary = (VocabEntry(spoken="vip", written="I'm Ada"),)
    config = SimpleNamespace(snippets=(), dictionary=dictionary)
    first = build_parser().parse_args(["compose", "vip", "--style", "formal", "--no-polish"])
    assert run_compose_command(config, first) == 0
    capsys.readouterr()
    second = build_parser().parse_args(["compose", "vip"])
    assert run_compose_command(config, second) == 0
    out = capsys.readouterr().out
    assert "I'm Ada" in out
    assert "I am Ada" not in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    reset_cli_compose_sticky()
    control_first = build_parser().parse_args(
        ["compose", "vip", "--style", "formal", "--no-polish"]
    )
    assert run_compose_command(empty, control_first) == 0
    capsys.readouterr()
    control_second = build_parser().parse_args(["compose", "vip"])
    assert run_compose_command(empty, control_second) == 0
    control = capsys.readouterr().out
    assert "I'm Ada" not in control
    assert apply_style("I'm Ada", "formal") == "I am Ada"


def test_cli_compose_applies_dictionary_terms(capsys) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from dcent_voice.app import run_compose_command

    dictionary = (VocabEntry(spoken="vip", written="um VIP Corp"),)
    args = Namespace(text=["um", "say", "vip"], style="plain", cleanup_level="medium")
    config = SimpleNamespace(snippets=(), dictionary=dictionary)
    assert run_compose_command(config, args) == 0
    out = capsys.readouterr().out
    assert "um VIP Corp" in out
    empty = SimpleNamespace(snippets=(), dictionary=())
    assert run_compose_command(empty, args) == 0
    control = capsys.readouterr().out
    assert "um VIP Corp" not in control


def test_postprocess_empty_passthrough() -> None:
    assert apply_dictation_postprocess("") == ""
    assert apply_dictation_postprocess("   ") == "   "


def test_default_path_rewrites_ramble_and_false_start() -> None:
    text = compose_dictation("so um I want to I need to send the report you know")
    lowered = text.lower()
    assert "um" not in lowered
    assert "you know" not in lowered
    assert "i want to" not in lowered
    assert "need to send the report" in lowered


def test_default_path_mid_utterance_number_correction() -> None:
    text = compose_dictation("the meeting is at 5 actually 6")
    assert "5" not in text
    assert "6" in text
    assert "actually" not in text.lower()


def test_default_path_keeps_actually_as_content() -> None:
    text = compose_dictation("I actually like this design")
    assert "actually" in text.lower()
    assert "like this" in text.lower()


def test_default_path_does_not_eat_wait_as_verb() -> None:
    text = compose_dictation("He could wait no longer")
    assert "wait" in text.lower()
    assert "no longer" in text.lower()


def test_default_path_destinations_differ() -> None:
    spoken = "hey can you send the deck to alice actually bob thanks"
    email = compose_dictation(spoken, style="email")
    chat = compose_dictation(spoken, style="chat")
    assert email != chat
    assert "\n\n" in email
    assert "Could you" in email
    assert email.rstrip().endswith("Thanks,")
    assert "bob" in email.lower()
    assert "alice" not in email.lower()
    assert "alice" not in chat.lower()
    assert "could you" not in chat.lower()
    assert "\n\n" not in chat
    assert chat.startswith("hey")


def test_default_path_i_mean_swaps_last_noun() -> None:
    text = compose_dictation("Let's meet Tuesday I mean Thursday")
    assert "thursday" in text.lower()
    assert "tuesday" not in text.lower()
    assert "i mean" not in text.lower()
    swapped = compose_dictation("send the report I mean the deck")
    assert "deck" in swapped.lower()
    assert "report" not in swapped.lower()


def test_default_path_i_mean_keeps_discourse_frames() -> None:
    """Explanatory 'I mean' is not a last-noun swap on the shipped path."""
    by_that = compose_dictation("what I mean by that is we should wait")
    assert "what i mean" in by_that.lower()
    assert "by that" in by_that.lower()
    assert "should wait" in by_that.lower()
    assert not by_that.lower().startswith("by that")

    idiom = compose_dictation("when I mean business I am serious")
    assert "i mean business" in idiom.lower()
    assert "i am serious" in idiom.lower()
    assert not idiom.lower().startswith("business")

    explain = compose_dictation("by that I mean the deck")
    assert "by that i mean" in explain.lower()
    assert "deck" in explain.lower()
    assert explain.lower().startswith("by that")


def test_default_path_drops_phrase_stutter() -> None:
    text = compose_dictation("I think I think we should ship Monday")
    assert text.lower().count("i think") == 1
    assert "ship monday" in text.lower()


def test_default_path_task_list_destinations_differ() -> None:
    spoken = "hey can you send the deck to alice actually bob and then update the timeline thanks"
    email = compose_dictation(spoken, style="email")
    chat = compose_dictation(spoken, style="chat")
    assert "1." in email
    assert "Bob" in email
    assert "alice" not in email.lower()
    assert email.startswith("Hey,")
    assert "Thanks," in email
    assert chat.startswith("- ")
    assert "1." not in chat
    assert "Thanks," not in chat
    by_that = compose_dictation("what I mean by that is we should wait")
    assert "what i mean" in by_that.lower()
    number = compose_dictation("the meeting is at 5 actually 6")
    assert "6" in number
    assert "5" not in number


def test_default_path_questions_get_question_marks() -> None:
    asked = compose_dictation("what time is the meeting")
    assert asked.endswith("?")
    assert asked.lower().startswith("what time")
    can = compose_dictation("can you look at the previous work")
    assert can.endswith("?")
    statement = compose_dictation("what I mean by that is we should wait")
    assert statement.endswith(".")
    assert "?" not in statement


def test_cleanup_level_high_drops_hedges_not_prose() -> None:
    high = compose_dictation("I think we should ship Monday", cleanup_level="high")
    assert "i think" not in high.lower()
    assert "ship monday" in high.lower()
    medium = compose_dictation("I think we should ship Monday", cleanup_level="medium")
    assert "i think" in medium.lower()
    prose = compose_dictation("He doesn't work at all", cleanup_level="high")
    assert "doesn't work at all" in prose.lower()
    assert compose_dictation("can you run git status", style="code", cleanup_level="high") == (
        "git status"
    )


def test_cleanup_level_high_mid_stacked_trailing() -> None:
    """High is more than a leading I-think strip. Default stays medium."""
    mid = compose_dictation("We should, I think, ship Monday", cleanup_level="high")
    assert "i think" not in mid.lower()
    assert "ship monday" in mid.lower()
    mid_med = compose_dictation("We should, I think, ship Monday", cleanup_level="medium")
    assert "i think" in mid_med.lower()

    stacked = compose_dictation("I guess I think we should ship Monday", cleanup_level="high")
    assert "i guess" not in stacked.lower()
    assert "i think" not in stacked.lower()
    assert "ship monday" in stacked.lower()
    stacked_med = compose_dictation("I guess I think we should ship Monday", cleanup_level="medium")
    assert "i think" in stacked_med.lower() or "i guess" in stacked_med.lower()

    trail = compose_dictation("The build is green I think", cleanup_level="high")
    assert "i think" not in trail.lower()
    assert "green" in trail.lower()
    trail_med = compose_dictation("The build is green I think", cleanup_level="medium")
    assert "i think" in trail_med.lower()

    content = compose_dictation("that's what I think", cleanup_level="high")
    assert "i think" in content.lower()
    important = compose_dictation("What I think is important", cleanup_level="high")
    assert "i think" in important.lower()
    think_so = compose_dictation("I think so", cleanup_level="high")
    assert "i think" in think_so.lower()

    assert (
        "sort of" in compose_dictation("It was a sort of experiment", cleanup_level="high").lower()
    )
    by_that = compose_dictation("what I mean by that is we should wait", cleanup_level="high")
    assert "what i mean" in by_that.lower()
    assert compose_dictation("I think we should ship Monday").lower().startswith("i think")


def test_cleanup_level_high_drops_uncommaed_mid_clause() -> None:
    """Spoken High rarely inserts commas around I-think. Medium keeps them."""
    high = compose_dictation("We should I think ship Monday", cleanup_level="high")
    assert "i think" not in high.lower()
    assert "ship monday" in high.lower()
    medium = compose_dictation("We should I think ship Monday", cleanup_level="medium")
    assert "i think" in medium.lower()
    guess = compose_dictation("We should I guess ship Monday", cleanup_level="high")
    assert "i guess" not in guess.lower()
    assert "ship monday" in guess.lower()
    stacked = compose_dictation("We should I guess I think ship Monday", cleanup_level="high")
    assert "i guess" not in stacked.lower()
    assert "i think" not in stacked.lower()
    about = compose_dictation("people I think about this design", cleanup_level="high")
    assert "i think" in about.lower()
    question = compose_dictation("Do I think we should ship Monday", cleanup_level="high")
    assert "i think" in question.lower()
    comma = compose_dictation("We should, I think, ship Monday", cleanup_level="high")
    assert "i think" not in comma.lower()
    assert "i think" in compose_dictation("that's what I think", cleanup_level="high").lower()
    assert "i think" in compose_dictation("What I think is important", cleanup_level="high").lower()


def test_cleanup_level_high_keeps_inverted_dont_i_think() -> None:
    """Apostrophe tokens must match Don't, not the trailing t."""
    high = compose_dictation("Don't I think we should ship Monday", cleanup_level="high")
    assert "i think" in high.lower()
    assert "don't" in high.lower()
    assert "we should" in high.lower()
    doesnt = compose_dictation("Doesn't I think we should ship Monday", cleanup_level="high")
    assert "i think" in doesnt.lower()


def test_cleanup_level_high_keeps_it_seems_as_verb() -> None:
    """Uncommaed 'it seems' is the predicate, not a mid-clause hedge."""
    high = compose_dictation("Now it seems to work", cleanup_level="high")
    assert "seems" in high.lower()
    assert "to work" in high.lower()
    assert high.lower() != "now to work."
    like = compose_dictation("Now it seems like it works", cleanup_level="high")
    assert "seems" in like.lower()
    named = compose_dictation("We should I think ship Monday", cleanup_level="high")
    assert "i think" not in named.lower()
    assert "ship monday" in named.lower()
    paren = compose_dictation("We should, it seems, ship Monday", cleanup_level="high")
    assert "seems" not in paren.lower()
    assert "ship monday" in paren.lower()


def test_cleanup_level_none_skips_rewrite_and_polish() -> None:
    raw = compose_dictation("so um I want to I need to send the report", cleanup_level="none")
    assert "um" in raw.lower()
    light = compose_dictation("so um I want to I need to send the report", cleanup_level="light")
    assert "um" not in light.lower()
    assert "i want to" in light.lower()
    medium = compose_dictation("so um I want to I need to send the report", cleanup_level="medium")
    assert "i want to" not in medium.lower()
    assert "need to send the report" in medium.lower()


def test_default_path_named_addressee_list_does_not_repeat_envelope() -> None:
    """Peeled greeting/closing must not be re-scanned as list items."""
    spoken = "Hey Alice, send the deck and then update the timeline thanks"
    email = compose_dictation(spoken, style="email")
    assert email.startswith("Hey Alice,")
    assert "1. Send the deck" in email
    assert "2. Update the timeline" in email
    assert email.rstrip().endswith("Thanks,")
    assert email.lower().count("hey alice") == 1
    assert "thanks" not in email.split("2.", 1)[-1].split("Thanks")[0].lower()
    chat = compose_dictation(spoken, style="chat")
    assert chat.startswith("- ")
    assert "Hey Alice" not in chat
    assert "Thanks," not in chat


def test_default_path_code_strips_can_you_run_cli_question() -> None:
    """Question polish must not leave '?' on a terminal command."""
    assert compose_dictation("can you run git status", style="code") == "git status"
    assert compose_dictation("could you run git log", style="code") == "git log"


def test_default_path_code_strips_please_run_cli() -> None:
    spoken = "please run git status actually git log"
    code = compose_dictation(spoken, style="code")
    assert code == "git log"
    email = compose_dictation(spoken, style="email")
    assert "git log" in email.lower()
    assert email != code


def test_default_path_clause_restart_keeps_last_intent() -> None:
    text = compose_dictation("send the report to alice no wait send the deck to bob")
    lowered = text.lower()
    assert "deck" in lowered
    assert "bob" in lowered
    assert "alice" not in lowered
    assert "report" not in lowered
    assert "no wait" not in lowered


def test_default_path_start_over_drops_prior_clause() -> None:
    text = compose_dictation("this is the wrong draft let me start over ship monday")
    lowered = text.lower()
    assert "ship monday" in lowered
    assert "wrong draft" not in lowered
    assert "let me start over" not in lowered


def test_default_path_or_rather_swaps_parallel() -> None:
    text = compose_dictation("the meeting is Tuesday or rather Thursday")
    assert "thursday" in text.lower()
    assert "tuesday" not in text.lower()
    assert "or rather" not in text.lower()


def test_default_path_i_mean_keeps_noun_phrase() -> None:
    text = compose_dictation("send the report I mean the status update")
    lowered = text.lower()
    assert "status update" in lowered
    assert "report" not in lowered
    assert "i mean" not in lowered


def test_default_path_does_not_eat_wait_no_longer() -> None:
    text = compose_dictation("He could wait no longer")
    assert "wait" in text.lower()
    assert "no longer" in text.lower()


def test_default_path_keeps_clean_read_speech() -> None:
    text = compose_dictation("He could wait no longer")
    assert "He could wait no longer" in text


def test_default_path_email_tell_is_sendable() -> None:
    email = compose_dictation("tell alex the invoice is ready", style="email")
    assert email.startswith("Hi Alex,")
    assert "The invoice is ready." in email
    assert email.rstrip().endswith("Thanks,")
    chat = compose_dictation("tell alex the invoice is ready", style="chat")
    assert "hi alex" not in chat.lower()
    assert "invoice is ready" in chat.lower()
    assert chat != email


def test_default_path_email_let_them_know() -> None:
    email = compose_dictation("let them know the build is green", style="email")
    assert "The build is green." in email
    assert email.rstrip().endswith("Thanks,")
    assert "Hi Them" not in email


def test_default_path_shipped_domain_vocab() -> None:
    text = compose_dictation("open bitcoin core and pay the lightning invoice")
    assert "Bitcoin Core" in text
    assert "Lightning invoice" in text
    lone = compose_dictation("Send 0.021 bitcoin to the hardware wallet")
    assert "bitcoin" in lone
    assert "Bitcoin Core" not in lone


def test_default_path_code_bitcoin_cli() -> None:
    assert compose_dictation("can you run bitcoin cli getbalance", style="code") == (
        "bitcoin-cli getbalance"
    )
    assert compose_dictation("please run lncli getinfo", style="code") == "lncli getinfo"


def test_spoken_style_cue_overrides_destination_on_compose() -> None:
    email = compose_dictation("email style hey send the deck thanks", style="plain")
    assert "email style" not in email.lower()
    assert email.startswith("Hey")
    assert "Thanks" in email
    notes = compose_dictation(
        "as notes we decided to ship Monday action item update the timeline",
        style="plain",
    )
    assert "## Decision" in notes
    assert "as notes" not in notes.lower()
    code = compose_dictation("use code style git status", style="email")
    assert code == "git status"
    prose = compose_dictation("code style is important", style="plain")
    assert "code style is important" in prose.lower()


def test_peel_spoken_press_enter_trailing_only() -> None:
    assert peel_spoken_press_enter("hello world press enter") == (True, "hello world")
    assert peel_spoken_press_enter("Hello world. Press enter.") == (True, "Hello world.")
    assert peel_spoken_press_enter("press enter") == (True, "")
    assert peel_spoken_press_enter("please press enter") == (True, "")
    assert peel_spoken_press_enter("hit return") == (True, "")
    assert peel_spoken_press_enter("hello world, press enter") == (True, "hello world")
    assert peel_spoken_press_enter("press enter to continue") == (
        False,
        "press enter to continue",
    )
    assert peel_spoken_press_enter("hello world") == (False, "hello world")


def test_compose_strips_trailing_press_enter() -> None:
    text = compose_dictation("hello world press enter", polish=False)
    assert text == "hello world"
    assert "press enter" not in text.lower()
    kept = compose_dictation("press enter to continue", polish=False)
    assert "press enter to continue" in kept.lower()


def test_peel_spoken_cleanup_leading_cues() -> None:
    assert peel_spoken_cleanup("cleanup high I think we should ship") == (
        "high",
        "I think we should ship",
    )
    assert peel_spoken_cleanup("no cleanup um I want to ship Monday")[0] == "none"
    assert peel_spoken_cleanup("light cleanup I was going to I want to ship")[0] == "light"
    assert peel_spoken_cleanup("cleanup the kitchen") == (None, "cleanup the kitchen")
    assert peel_spoken_cleanup("high cleanup costs money") == (
        None,
        "high cleanup costs money",
    )


def test_spoken_cleanup_cue_overrides_level_on_compose() -> None:
    high = compose_dictation(
        "cleanup high I think we should ship Monday",
        cleanup_level="medium",
    )
    medium = compose_dictation("I think we should ship Monday", cleanup_level="medium")
    assert "i think" not in high.lower()
    assert "I think" in medium
    assert "cleanup high" not in high.lower()
    none = compose_dictation(
        "no cleanup um I was going to I want to ship Monday",
        cleanup_level="medium",
    )
    assert none.lower().startswith("um") or "um" in none.lower()
