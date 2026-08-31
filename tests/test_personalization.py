# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from dcent_voice.personalization import (
    MAX_STORE_BYTES,
    PersistenceStateReconciledError,
    PersonalizationStore,
)
from dcent_voice.pipeline import apply_dictionary


def _record_in_process(
    path: str,
    spoken: str,
    written: str,
    ready: object,
    start: object,
    results: object,
) -> None:
    store = PersonalizationStore(Path(path))
    ready.put(True)
    start.wait(10)
    try:
        results.put(store.record_correction(spoken, written) is not None)
    except Exception as exc:  # pragma: no cover - reported to parent process
        results.put(f"{type(exc).__name__}: {exc}")


def _policy_save_in_process(
    path: str,
    ready: object,
    start: object,
    results: object,
) -> None:
    store = PersonalizationStore(Path(path))
    ready.put(True)
    start.wait(10)
    try:
        store.update_policy(enabled=False, learn=False)
        store.save()
        results.put(True)
    except Exception as exc:  # pragma: no cover - reported to parent process
        results.put(f"{type(exc).__name__}: {exc}")


def _hold_store_lock(path: str, ready: object, release: object) -> None:
    from dcent_voice.personalization import _coordinated_store_write

    with _coordinated_store_write(Path(path), timeout_s=10):
        ready.put(True)
        release.wait(10)


def _valid_v3_term(**overrides: object) -> dict[str, object]:
    term: dict[str, object] = {
        "spoken": "d central",
        "written": "D-Central",
        "count": 1,
        "source": "typed",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "style": "",
        "app": "",
    }
    term.update(overrides)
    return term


def _valid_v3_payload() -> dict[str, object]:
    return {
        "version": 3,
        "enabled": True,
        "learn": True,
        "terms": [_valid_v3_term()],
    }


def test_store_roundtrip_and_reset(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    store = PersonalizationStore(path)
    store.record_correction("d sent", "DCENT_Voice")
    store.record_correction("d sent", "DCENT_Voice")
    again = PersonalizationStore(path)
    vocab = again.as_vocab()
    assert vocab[0].spoken == "d sent"
    assert vocab[0].written == "DCENT_Voice"
    assert again.snapshot()["stores_audio"] is False
    assert "audio" not in path.read_text(encoding="utf-8")
    again.reset()
    assert PersonalizationStore(path).as_vocab() == ()


def test_saved_disabled_policy_round_trips_for_plain_reload(tmp_path: Path) -> None:
    path = tmp_path / "saved-policy.json"
    store = PersonalizationStore(path)
    store.record_correction("d central", "D-Central")
    store.update_policy(enabled=False, learn=False)
    store.save()
    before = path.read_bytes()

    reloaded = PersonalizationStore(path)

    assert reloaded.enabled is False
    assert reloaded.learn is False
    assert reloaded.apply("d central") == "d central"
    assert reloaded.record_correction("private client", "SecretName") is None
    assert path.read_bytes() == before


def test_explicit_constructor_policy_overrides_saved_policy_per_field(
    tmp_path: Path,
) -> None:
    path = tmp_path / "explicit-policy.json"
    store = PersonalizationStore(path)
    store.record_correction("d central", "D-Central")
    store.update_policy(enabled=False, learn=False)
    store.save()

    enabled_override = PersonalizationStore(path, enabled=True)
    assert enabled_override.enabled is True
    assert enabled_override.learn is False
    assert enabled_override.apply("d central") == "D-Central"
    assert enabled_override.record_correction("private client", "SecretName") is None

    both_override = PersonalizationStore(path, enabled=True, learn=True)
    assert both_override.enabled is True
    assert both_override.learn is True


def test_legacy_missing_policy_defaults_on_but_current_missing_policy_fails_closed(
    tmp_path: Path,
) -> None:
    term = {
        "spoken": "d central",
        "written": "D-Central",
        "count": 1,
        "source": "typed",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps({"version": 2, "terms": [term]}), encoding="utf-8")
    unversioned_path = tmp_path / "unversioned.json"
    unversioned_path.write_text(json.dumps({"terms": [term]}), encoding="utf-8")
    current_path = tmp_path / "current-missing-policy.json"
    current_path.write_text(json.dumps({"version": 3, "terms": [term]}), encoding="utf-8")

    legacy = PersonalizationStore(legacy_path)
    unversioned = PersonalizationStore(unversioned_path)
    current = PersonalizationStore(current_path)

    assert (legacy.enabled, legacy.learn) == (True, True)
    assert legacy.apply("d central") == "D-Central"
    assert (unversioned.enabled, unversioned.learn) == (True, True)
    assert unversioned.apply("d central") == "D-Central"
    assert (current.enabled, current.learn) == (False, False)
    assert current.apply("d central") == "d central"


def test_malformed_persisted_policy_fails_closed_without_rewriting_disk(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed-policy.json"
    payload = {
        "version": 3,
        "enabled": "false",
        "learn": 1,
        "terms": [
            {
                "spoken": "d central",
                "written": "D-Central",
                "count": 1,
                "source": "typed",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.apply("d central") == "d central"
    assert store.record_correction("private client", "SecretName") is None
    assert path.read_bytes() == before
    explicit = PersonalizationStore(path, enabled=True, learn=True)
    assert (explicit.enabled, explicit.learn) == (False, False)
    assert explicit.apply("d central") == "d central"
    assert explicit.snapshot()["term_count"] == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        json.dumps({"version": "3", "terms": []}).encode(),
        json.dumps({"version": 3, "enabled": True, "learn": True, "terms": {}}).encode(),
    ],
)
def test_corrupted_store_payload_policy_fails_closed(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "corrupted-store.json"
    path.write_bytes(payload)
    before = path.read_bytes()

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.record_correction("private client", "SecretName") is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", ["3", True, False, 4, -1, 3.0, None])
def test_unsupported_or_non_exact_store_version_fails_fully_closed(
    tmp_path: Path, version: object
) -> None:
    path = tmp_path / "hostile-version.json"
    payload = _valid_v3_payload()
    payload["version"] = version
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0
    assert store.apply("d central") == "d central"
    assert path.read_bytes() == before


def test_current_store_envelope_and_terms_validate_transactionally(
    tmp_path: Path,
) -> None:
    malformed: list[dict[str, object]] = []
    for missing in ("enabled", "learn", "terms"):
        payload = _valid_v3_payload()
        payload.pop(missing)
        malformed.append(payload)
    for field, value in (("enabled", "true"), ("learn", 1), ("terms", {})):
        payload = _valid_v3_payload()
        payload[field] = value
        malformed.append(payload)
    bad_terms = (
        {"spoken": "broken"},
        _valid_v3_term(count=True),
        _valid_v3_term(count=0),
        _valid_v3_term(count=1.0),
        _valid_v3_term(count=1_000_001),
        _valid_v3_term(spoken=1),
        _valid_v3_term(spoken=" d central"),
        _valid_v3_term(written=""),
        _valid_v3_term(source=[]),
        _valid_v3_term(updated_at="not-a-time"),
        _valid_v3_term(updated_at="2026-01-01T00:00:00"),
        _valid_v3_term(style=1),
        _valid_v3_term(app="C:/Code.exe"),
        _valid_v3_term(extra="unexpected"),
    )
    for bad_term in bad_terms:
        payload = _valid_v3_payload()
        payload["terms"] = [_valid_v3_term(), bad_term]
        malformed.append(payload)

    for index, payload in enumerate(malformed):
        path = tmp_path / f"malformed-envelope-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = path.read_bytes()
        store = PersonalizationStore(path)
        assert (store.enabled, store.learn) == (False, False)
        assert store.snapshot()["term_count"] == 0
        assert path.read_bytes() == before


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_term_counts_are_rejected_without_crash_or_mutation(
    tmp_path: Path, constant: str
) -> None:
    path = tmp_path / f"non-finite-{constant}.json"
    encoded = json.dumps(_valid_v3_payload()).replace('"count": 1', f'"count": {constant}')
    path.write_text(encoded, encoding="utf-8")
    before = path.read_bytes()

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0
    assert path.read_bytes() == before


def test_duplicate_json_members_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-member.json"
    path.write_text(
        '{"version":3,"enabled":true,"enabled":false,"learn":true,"terms":[]}',
        encoding="utf-8",
    )
    before = path.read_bytes()

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0
    assert path.read_bytes() == before


def test_store_size_limit_is_checked_before_json_parse(tmp_path: Path, monkeypatch) -> None:
    from dcent_voice import personalization as module

    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (MAX_STORE_BYTES + 1))
    called = False

    def forbidden_parse(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("oversized state reached JSON parser")

    monkeypatch.setattr(module.json, "loads", forbidden_parse)
    store = PersonalizationStore(path)

    assert called is False
    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0


def test_store_size_limit_accepts_exact_boundary_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "boundary.json"
    encoded = json.dumps({"version": 3, "enabled": False, "learn": False, "terms": []}).encode()
    path.write_bytes(encoded + b" " * (MAX_STORE_BYTES - len(encoded)))

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0


@pytest.mark.parametrize("depth", [17, 100, 1_000, 5_000])
def test_deep_json_fails_closed_without_recursion_crash(tmp_path: Path, depth: int) -> None:
    path = tmp_path / f"depth-{depth}.json"
    path.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0


def test_depth_scanner_ignores_brackets_inside_valid_strings(tmp_path: Path) -> None:
    path = tmp_path / "bracket-string.json"
    payload = _valid_v3_payload()
    payload["terms"] = [_valid_v3_term(written="[" * 256)]
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = PersonalizationStore(path)

    assert store.snapshot()["term_count"] == 1
    assert store.apply("d central") == "[" * 256


def test_decoder_recursion_error_is_inert(tmp_path: Path, monkeypatch) -> None:
    from dcent_voice import personalization as module

    path = tmp_path / "decoder-recursion.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        module.json,
        "loads",
        lambda *args, **kwargs: (_ for _ in ()).throw(RecursionError("deep")),
    )

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0


@pytest.mark.parametrize("conflicting", [False, True])
def test_normalized_duplicate_scoped_terms_reject_entire_store(
    tmp_path: Path, conflicting: bool
) -> None:
    path = tmp_path / f"duplicate-scope-{conflicting}.json"
    first = _valid_v3_term(spoken="D Central", style="plain", app="notes.exe")
    second = _valid_v3_term(
        spoken="d central",
        written="Other" if conflicting else "D-Central",
        style="plain",
        app="notes.exe",
    )
    payload = _valid_v3_payload()
    payload["terms"] = [first, second]
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0


def test_same_spoken_term_in_distinct_scopes_remains_valid(tmp_path: Path) -> None:
    path = tmp_path / "distinct-scopes.json"
    payload = _valid_v3_payload()
    payload["terms"] = [
        _valid_v3_term(style="plain", app="notes.exe"),
        _valid_v3_term(style="code", app="code.exe", written="DCENT"),
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = PersonalizationStore(path)

    assert store.snapshot()["term_count"] == 2
    assert store.apply("d central", style="plain", app="notes.exe") == "D-Central"
    assert store.apply("d central", style="code", app="code.exe") == "DCENT"


@pytest.mark.parametrize(
    "timestamp",
    [
        "0001-01-01T00:00:00+00:00",
        "9999-01-01T00:00:00+00:00",
        "2200-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00Z",
        "2026-01-01T14:00:00+14:00",
        "2026-01-01T00:00:00.1+00:00",
        "2026-01-01T00:00:00",
        "2026-01-01",
        " 2026-01-01T00:00:00+00:00",
    ],
)
def test_v3_timestamp_must_match_canonical_bounded_utc_form(tmp_path: Path, timestamp: str) -> None:
    path = tmp_path / "timestamp.json"
    payload = _valid_v3_payload()
    payload["terms"] = [_valid_v3_term(updated_at=timestamp)]
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0


@pytest.mark.parametrize("timestamp", ["1970-01-01T00:00:00+00:00", "2199-12-31T23:59:59+00:00"])
def test_v3_timestamp_supported_boundaries_are_valid(tmp_path: Path, timestamp: str) -> None:
    path = tmp_path / "timestamp-boundary.json"
    payload = _valid_v3_payload()
    payload["terms"] = [_valid_v3_term(updated_at=timestamp)]
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert PersonalizationStore(path).snapshot()["term_count"] == 1


def test_legacy_timestamp_is_deliberately_canonicalized_to_utc(tmp_path: Path) -> None:
    path = tmp_path / "legacy-timestamp.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "terms": [
                    {
                        "spoken": "d central",
                        "written": "D-Central",
                        "count": 1,
                        "source": "typed",
                        "updated_at": "2026-01-01T14:00:00.123456+14:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    term = PersonalizationStore(path).snapshot()["terms"][0]

    assert term["updated_at"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize("version", [None, 1, 2])
def test_legacy_migration_rejects_entire_malformed_term_list(
    tmp_path: Path, version: int | None
) -> None:
    path = tmp_path / f"malformed-legacy-{version}.json"
    payload: dict[str, object] = {
        "terms": [
            {
                "spoken": "d central",
                "written": "D-Central",
                "count": 1,
                "source": "typed",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            {"spoken": "broken", "written": "Broken", "count": "1"},
        ]
    }
    if version is not None:
        payload["version"] = version
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    store = PersonalizationStore(path)

    assert (store.enabled, store.learn) == (False, False)
    assert store.snapshot()["term_count"] == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("invalid", ["false", "true", 1, 0, 1.0, 0.0, [], {}, np.bool_(True)])
def test_call_scoped_policy_overrides_are_strict(tmp_path: Path, invalid: object) -> None:
    path = tmp_path / "strict-call-policy.json"
    store = PersonalizationStore(path)
    store.record_correction("d central", "D-Central")
    before = path.read_bytes()

    with pytest.raises(TypeError, match="policy_enabled"):
        store.apply("d central", policy_enabled=invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="policy_enabled"):
        store.as_vocab(policy_enabled=invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="policy_learn"):
        store.record_correction(
            "private client",
            "SecretName",
            policy_enabled=True,
            policy_learn=invalid,  # type: ignore[arg-type]
        )

    assert store.snapshot()["term_count"] == 1
    assert path.read_bytes() == before


def test_call_scoped_policy_does_not_mutate_direct_store_policy(tmp_path: Path) -> None:
    path = tmp_path / "call-policy.json"
    store = PersonalizationStore(path)
    store.record_correction("d central", "D-Central")
    store.update_policy(enabled=False, learn=False)
    store.save()
    before = path.read_bytes()

    assert store.apply("d central") == "d central"
    assert store.apply("d central", policy_enabled=True) == "D-Central"
    assert store.as_vocab() == ()
    assert store.as_vocab(policy_enabled=True)[0].written == "D-Central"
    learned = store.record_correction(
        "private client",
        "SecretName",
        policy_enabled=True,
        policy_learn=True,
    )

    assert learned is not None
    assert (store.enabled, store.learn) == (False, False)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["enabled"] is False
    assert persisted["learn"] is False
    assert path.read_bytes() != before


@pytest.mark.parametrize("invalid", ["false", "true", 1, 0, None, [], {}])
def test_apply_rejects_non_boolean_prose_context(tmp_path: Path, invalid: object) -> None:
    store = PersonalizationStore(tmp_path / "strict-context.json")
    store.record_correction("d central", "D-Central")

    with pytest.raises(TypeError, match="prose_context must be a boolean"):
        store.apply("Open d central settings.", prose_context=invalid)  # type: ignore[arg-type]

    assert store.apply("Open d central settings.") == "Open d central settings."


def test_learn_last_infers_pair_without_storing_audio(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    store = PersonalizationStore(path)
    store.note_utterance("hello d sent", "Hello d sent.")
    term = store.learn_last("Hello DCENT_Voice.")
    assert term is not None
    assert "DCENT_Voice" in term.written
    payload = path.read_text(encoding="utf-8")
    assert "audio" not in payload
    assert "hello d sent" not in payload.lower() or "DCENT_Voice" in payload
    snap = store.snapshot()
    assert snap["stores_audio"] is False
    assert snap["stores_last_on_disk"] is False
    assert snap["has_last"] is True


def test_learn_last_can_capture_case_only_name_correction(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.note_utterance("ask satoshi", "Ask satoshi.", style="email")

    term = store.learn_last("Ask Satoshi.")

    assert term is not None
    assert term.spoken == "satoshi"
    assert term.written == "Satoshi"
    assert term.style == "email"


def test_disabled_store_does_not_learn(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    store = PersonalizationStore(path, enabled=True, learn=False)
    assert store.record_correction("foo", "bar") is None
    assert store.as_vocab() == ()


_INVALID_POLICY_BOOLEANS = (
    "false",
    "true",
    "yes",
    "on",
    1,
    0,
    1.0,
    0.0,
    None,
    [],
    {},
    np.bool_(True),
    np.bool_(False),
)


@pytest.mark.parametrize("invalid", _INVALID_POLICY_BOOLEANS)
def test_store_constructor_rejects_non_boolean_policy_without_disk_change(
    tmp_path: Path, invalid: object
) -> None:
    path = tmp_path / "constructor-policy.json"
    path.write_bytes(b"unchanged")

    with pytest.raises(TypeError, match="enabled must be a boolean"):
        PersonalizationStore(path, enabled=invalid, learn=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="learn must be a boolean"):
        PersonalizationStore(path, enabled=True, learn=invalid)  # type: ignore[arg-type]

    assert path.read_bytes() == b"unchanged"


@pytest.mark.parametrize("invalid", _INVALID_POLICY_BOOLEANS)
def test_invalid_live_policy_is_atomic_and_cannot_learn_or_persist(
    tmp_path: Path, invalid: object
) -> None:
    path = tmp_path / "live-policy.json"
    store = PersonalizationStore(path, enabled=False, learn=False)
    store.save()
    before = path.read_bytes()

    with pytest.raises(TypeError, match="enabled must be a boolean"):
        store.update_policy(enabled=invalid, learn=True)  # type: ignore[arg-type]
    assert store.enabled is False
    assert store.learn is False
    with pytest.raises(TypeError, match="learn must be a boolean"):
        store.update_policy(enabled=True, learn=invalid)  # type: ignore[arg-type]
    assert store.enabled is False
    assert store.learn is False
    with pytest.raises(TypeError, match="enabled must be a boolean"):
        store.enabled = invalid  # type: ignore[assignment]
    with pytest.raises(TypeError, match="learn must be a boolean"):
        store.learn = invalid  # type: ignore[assignment]
    assert store.enabled is False
    assert store.learn is False
    assert store.record_correction("private name", "PrivateName") is None
    assert store.apply("private name") == "private name"
    assert path.read_bytes() == before


def test_corrupted_internal_policy_fails_closed_and_cannot_save(tmp_path: Path) -> None:
    path = tmp_path / "corrupt-policy.json"
    store = PersonalizationStore(path)
    store.record_correction("d central", "D-Central")
    before = path.read_bytes()
    object.__setattr__(store, "_enabled", "false")
    object.__setattr__(store, "_learn", "false")

    assert store.apply("d central") == "d central"
    assert store.as_vocab() == ()
    assert store.record_correction("private name", "PrivateName") is None
    assert store.snapshot()["enabled"] is False
    assert store.snapshot()["learn"] is False
    with pytest.raises(TypeError, match="enabled must be a boolean"):
        store.save()
    with pytest.raises(TypeError, match="enabled must be a boolean"):
        store.reset()
    assert store.snapshot()["term_count"] == 1
    assert path.read_bytes() == before


def test_live_policy_disables_apply_and_learning_without_reload(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("d sent", "DCENT_Voice")

    store.update_policy(enabled=False, learn=True)
    assert store.apply("Open d sent.") == "Open d sent."
    assert store.as_vocab() == ()
    assert store.record_correction("bit coin", "Bitcoin") is None

    store.update_policy(enabled=True, learn=False)
    assert store.apply("Open d sent.", prose_context=True) == "Open DCENT_Voice."
    assert store.record_correction("bit coin", "Bitcoin") is None


def test_adaptive_held_out_quality_improves_without_fuzzy_matching(tmp_path: Path) -> None:
    """Repeated explicit evidence generalizes separators and safe inflections."""
    store = PersonalizationStore(tmp_path / "personalization.json")
    learned = (
        ("d central", "D-Central", None, None),
        ("bit coin", "Bitcoin", None, None),
        ("nick zabo", "Nick Szabo", None, None),
        ("pie test", "pytest", None, None),
        ("d sent voice", "DCENT_Voice", None, None),
    )
    for spoken, written, style, app in learned:
        store.record_correction(spoken, written, style=style, app=app)
        store.record_correction(spoken, written, style=style, app=app)

    cases = (
        ("Deploy the d central node.", "Deploy the D-Central node."),
        ("Deploy the d-central node.", "Deploy the d-central node."),
        ("D central's repository.", "D-Central's repository."),
        ("Two d centrals.", "Two D-Centrals."),
        ("Open a bit coin wallet.", "Open a Bitcoin wallet."),
        ("Open a bit-coin wallet.", "Open a bit-coin wallet."),
        ("Two bit coins.", "Two Bitcoins."),
        ("Nick zabo signed it.", "Nick Szabo signed it."),
        ("Nick-zabo's key.", "Nick-zabo's key."),
        ("Run pie test.", "Run pytest."),
        ("Run pie-test.", "Run pie-test."),
        ("Open d-sent voice.", "Open d-sent voice."),
    )
    vocab = store.as_vocab(style="plain", app="code.exe")
    exact_score = sum(apply_dictionary(text, vocab) == expected for text, expected in cases)
    adaptive_score = sum(
        store.apply(text, style="plain", app="code.exe", prose_context=True) == expected
        for text, expected in cases
    )

    assert exact_score == 10
    assert adaptive_score == len(cases)


def test_adaptive_negative_collision_corpus_is_unchanged(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    for _ in range(2):
        store.record_correction("d central", "D-Central")
        store.record_correction("bit coin", "Bitcoin")
        store.record_correction("satoshi", "Satoshi")
        store.record_correction("pie test", "pytest", style="code")
        store.record_correction("there", "their")

    negatives = (
        "central planning is useful",
        "a sentimental note",
        "bitcoin is already one token",
        "the pie testing fixture passed",
        "therefore this stays unchanged",
        "there's no fuzzy possessive rewrite",
        "Satoshiko Nakamura is a different name",
    )
    assert [store.apply(text, style="code") for text in negatives] == list(negatives)


def test_generalization_requires_repeated_consistent_evidence(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("d central", "D-Central")
    assert store.apply("d central") == "D-Central"
    assert store.apply("d-central") == "d-central"

    store.record_correction("d central", "Decentral")
    assert store.apply("d-central") == "d-central"
    store.record_correction("d central", "Decentral")
    assert store.apply("d-central") == "Decentral"


def test_generated_separator_variants_are_whole_utterance_only(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    for _ in range(2):
        store.record_correction("d central", "D-Central")
        store.record_correction("pie test", "pytest")

    assert store.apply("d-central") == "D-Central"
    assert store.apply("d_central。") == "D-Central。"
    assert store.apply("dcentral!") == "D-Central!"
    assert store.apply("d-central !") == "D-Central !"
    assert store.apply("pie_test") == "pytest"

    unchanged = (
        "deploy d-central node",
        "d_central = value",
        "def d_central(): pass",
        r"folder\d-central\config",
        r"folder\d central\config",
        r"folder-name\d central-config\settings.toml",
        r"C:folder\d central\config",
        "PIE_TEST=1",
    )
    assert [store.apply(text) for text in unchanged] == list(unchanged)
    assert store.apply("Deploy d central node.", prose_context=True) == ("Deploy D-Central node.")
    assert store.apply(r"C:d central\config") == r"C:d central\config"


def test_natural_space_correction_never_rewrites_leaf_paths(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    for _ in range(2):
        store.record_correction("d central", "D-Central")

    paths = (
        r"C:d central",
        r"C:d central.txt",
        r"C:d-central",
        r"C:d-central.toml",
        r"C:\d central",
        r"C:\d central.json",
        r"\d central",
        r"\d central.yaml",
        r"\\server\share\d central",
        r"\\server-name\share\d central.ini",
        "/d central",
        "/d central.txt",
        "//server/share/d central",
        "//server-name/share/d central.ini",
        "~/d central",
        "./d central.json",
        "../d central",
        "folder/d central",
        "folder-name/d central-file.toml",
    )
    for style in ("plain", "code"):
        assert [store.apply(path, style=style) for path in paths] == list(paths)
        surrounded = tuple(f"open {path}, please" for path in paths)
        assert [store.apply(text, style=style) for text in surrounded] == list(surrounded)
    assert store.apply("Open the d central project.", style="plain", prose_context=True) == (
        "Open the D-Central project."
    )


def test_code_style_only_applies_whole_utterance_corrections(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    for _ in range(2):
        store.record_correction("d central", "D-Central")
        store.record_correction("pie test", "pytest")

    assert store.apply("d central", style="code") == "D-Central"
    assert store.apply("d central !", style="code") == "D-Central !"
    assert store.apply("d-central !", style="code") == "D-Central !"
    assert store.apply("pie tests", style="code") == "pytests"

    snippets = (
        "open d central project",
        "d central = value",
        "result = d central + 1",
        "emit(d central)",
        "# deploy d central",
        "// deploy d central",
        "echo d central",
        "git --branch d central",
        "const name = 'd central'",
        "pie test --quiet",
    )
    assert [store.apply(text, style="code") for text in snippets] == list(snippets)
    assert store.apply("Open d central project.", style="plain", prose_context=True) == (
        "Open D-Central project."
    )


def test_spaced_filenames_are_protected_across_styles_and_scopes(
    tmp_path: Path,
) -> None:
    global_store = PersonalizationStore(tmp_path / "global.json")
    scoped_store = PersonalizationStore(tmp_path / "scoped.json")
    for _ in range(2):
        global_store.record_correction("d central", "D-Central")
        scoped_store.record_correction("d central", "D-Central", style="plain", app="notes.exe")

    filenames = (
        "d central file.txt",
        "my d central final-report.md",
        "d central (final report).md",
        "d central user's guide.txt",
        "d central R&D notes.txt",
        "d central + final.txt",
        "d central [final] #1.txt",
        "d central {release} [#1].md",
        "d central asset.xyz",
        "d central scene.blend",
        "d central photo.heic",
        "d central module.wasm",
        "d central dependencies.lock",
        "d central launcher.desktop",
        "d central daemon.service",
        "d central schema.proto",
        "d central dataset.parquet",
        "d central archive.7z",
        "open d central file.txt",
        "upload my d central final-report.md now",
        "please edit d central user's guide.txt",
        "save d central R&D notes.txt now",
    )
    for style in ("plain", "code"):
        assert [global_store.apply(text, style=style) for text in filenames] == list(filenames)
    assert [scoped_store.apply(text, style="plain", app="notes.exe") for text in filenames] == list(
        filenames
    )

    mixed = (
        (
            "deploy d central node then open report.txt",
            "deploy D-Central node then open report.txt",
        ),
        (
            "deploy d central node then open d central (final report).md",
            "deploy D-Central node then open d central (final report).md",
        ),
        (
            "open report.txt then deploy d central node",
            "open report.txt then deploy D-Central node",
        ),
        (
            "open d central R&D notes.txt then deploy d central node",
            "open d central R&D notes.txt then deploy D-Central node",
        ),
    )
    assert [global_store.apply(text, style="plain") for text, _ in mixed] == [
        text for text, _ in mixed
    ]

    sentence_fields = (
        (
            "ask d central team to review notes.md",
            "ask D-Central team to review notes.md",
        ),
        (
            "please tell d central team about report.txt",
            "please tell D-Central team about report.txt",
        ),
        (
            "d central team wrote report.txt",
            "D-Central team wrote report.txt",
        ),
        (
            "read what d central team wrote in report.txt",
            "read what D-Central team wrote in report.txt",
        ),
        (
            "d central team updated artifact.wasm",
            "D-Central team updated artifact.wasm",
        ),
        (
            "the d central node uses dataset.parquet",
            "the D-Central node uses dataset.parquet",
        ),
        (
            "ask d central team to review notes.xyz then open d central asset.7z",
            "ask D-Central team to review notes.xyz then open d central asset.7z",
        ),
        (
            "open report.blend then deploy d central module",
            "open report.blend then deploy D-Central module",
        ),
    )
    assert [global_store.apply(text, style="plain") for text, _ in sentence_fields] == [
        text for text, _ in sentence_fields
    ]
    assert (
        global_store.apply("d central asset.abcdefghijklmnopq", style="plain")
        == "d central asset.abcdefghijklmnopq"
    )


def test_w23j_clause_aware_filename_matrix_global_and_scoped(tmp_path: Path) -> None:
    global_store = PersonalizationStore(tmp_path / "global-w23j.json")
    scoped_store = PersonalizationStore(tmp_path / "scoped-w23j.json")
    for _ in range(2):
        global_store.record_correction("d central", "D-Central")
        scoped_store.record_correction("d central", "D-Central", style="plain", app="notes.exe")

    cases = (
        ("d central (final report).md needs review",) * 2,
        ("d central user's guide.txt is ready",) * 2,
        ("d central R&D notes.txt needs edits",) * 2,
        ("d central + final.txt was uploaded",) * 2,
        ("d central [final] #1.txt needs review",) * 2,
        ("d central {release} [#1].md is ready",) * 2,
        ("d central asset.xyz needs review",) * 2,
        ("d central scene.blend is ready",) * 2,
        ("d central photo.heic was uploaded",) * 2,
        ("d central module.wasm needs review",) * 2,
        ("d central dependencies.lock is ready",) * 2,
        ("d central launcher.desktop needs review",) * 2,
        ("d central daemon.service is ready",) * 2,
        ("d central schema.proto needs review",) * 2,
        ("d central archive.7z is ready",) * 2,
        (
            "d central (final report).md then deploy d central node",
            "d central (final report).md then deploy D-Central node",
        ),
        (
            "d central user's guide.txt; discuss d central rollout",
            "d central user's guide.txt; discuss D-Central rollout",
        ),
        (
            "d central R&D notes.txt before review d central proposal",
            "d central R&D notes.txt before review D-Central proposal",
        ),
        (
            "d central + final.txt after update d central docs",
            "d central + final.txt after update D-Central docs",
        ),
        (
            "d central [final] #1.txt, then deploy d central",
            "d central [final] #1.txt, then deploy D-Central",
        ),
        (
            "review d central proposal in notes.md",
            "review D-Central proposal in notes.md",
        ),
        (
            "inspect d central proposal in notes.xyz",
            "inspect D-Central proposal in notes.xyz",
        ),
        (
            "analyze d central metrics in data.parquet",
            "analyze D-Central metrics in data.parquet",
        ),
        (
            "compare d central output with baseline.txt",
            "compare D-Central output with baseline.txt",
        ),
        (
            "summarize d central findings in report.md",
            "summarize D-Central findings in report.md",
        ),
        (
            "document d central behavior in spec.proto",
            "document D-Central behavior in spec.proto",
        ),
        (
            "explain d central changes in guide.txt",
            "explain D-Central changes in guide.txt",
        ),
        (
            "mention d central status in notes.md",
            "mention D-Central status in notes.md",
        ),
        (
            "ask d central team to review notes.md",
            "ask D-Central team to review notes.md",
        ),
        (
            "please tell d central team about report.txt",
            "please tell D-Central team about report.txt",
        ),
        (
            "d central team wrote report.txt",
            "D-Central team wrote report.txt",
        ),
        (
            "read what d central team wrote in report.txt",
            "read what D-Central team wrote in report.txt",
        ),
        (
            "the d central node uses dataset.parquet",
            "the D-Central node uses dataset.parquet",
        ),
        (
            "d central team updated artifact.wasm",
            "D-Central team updated artifact.wasm",
        ),
    )
    assert len(cases) == 34
    assert [global_store.apply(text, style="plain") for text, _ in cases] == [
        text for text, _ in cases
    ]
    assert [scoped_store.apply(text, style="plain", app="notes.exe") for text, _ in cases] == [
        text for text, _ in cases
    ]


def test_w23k_direct_action_content_clause_matrix(tmp_path: Path) -> None:
    global_store = PersonalizationStore(tmp_path / "global-w23k.json")
    scoped_store = PersonalizationStore(tmp_path / "scoped-w23k.json")
    for _ in range(2):
        global_store.record_correction("d central", "D-Central")
        scoped_store.record_correction("d central", "D-Central", style="plain", app="notes.exe")

    content_cases = (
        "open discussion with d central team about report.txt",
        "edit notes from d central team in status.md",
        "read feedback on d central proposal in review.txt",
        "write summary about d central module for notes.md",
        "save discussion with d central team in archive.txt",
        "print comments by d central team from report.pdf",
        "view notes regarding d central proposal in status.txt",
        "send feedback from d central team in message.txt",
        "attach comments about d central proposal in ticket.md",
        "load analysis on d central output from result.json",
        "export notes concerning d central behavior in report.csv",
        "import feedback regarding d central module from data.json",
        "select notes by d central team in review.md",
        "choose summary for d central proposal in notes.txt",
        "upload analysis about d central output in bundle.zip",
        "download notes from d central team in package.7z",
        "approve d central proposal in notes.md",
        "audit d central behavior in report.txt",
        "verify d central output in result.json",
        "debug d central behavior in trace.log",
        "assess d central accuracy in metrics.csv",
        "evaluate d central latency in results.parquet",
        "validate d central output in snapshot.json",
        "investigate d central failure in trace.txt",
        "diagnose d central startup in profile.json",
        "test d central behavior in scenario.md",
    )
    direct_filenames = (
        "open d central report.txt",
        "edit d central notes.md",
        "read d central user's guide.txt",
        "write d central R&D notes.txt",
        "save d central + final.txt",
        "print d central [final] #1.txt",
    )
    assert len(content_cases) + len(direct_filenames) == 32
    for store, kwargs in (
        (global_store, {"style": "plain"}),
        (scoped_store, {"style": "plain", "app": "notes.exe"}),
    ):
        assert [store.apply(text, **kwargs) for text in content_cases] == list(content_cases)
        assert [store.apply(text, **kwargs) for text in direct_filenames] == list(direct_filenames)


def test_multifile_and_measure_track_regressions(tmp_path: Path) -> None:
    global_store = PersonalizationStore(tmp_path / "global-multifile.json")
    scoped_store = PersonalizationStore(tmp_path / "scoped-multifile.json")
    for _ in range(2):
        global_store.record_correction("d central", "D-Central")
        scoped_store.record_correction("d central", "D-Central", style="plain", app="notes.exe")

    cases = (
        (
            "d central one.txt and d central team linked d central two.md for d central users",
            "d central one.txt and D-Central team linked d central two.md for D-Central users",
        ),
        (
            "d central one.txt and d central team uses d central two.md for d central users",
            "d central one.txt and D-Central team uses d central two.md for D-Central users",
        ),
        (
            "open d central one.txt, ask d central team about d central two.md",
            "open d central one.txt, ask D-Central team about d central two.md",
        ),
        (
            "edit d central one.txt, tell d central team about d central two.md",
            "edit d central one.txt, tell D-Central team about d central two.md",
        ),
        (
            "d central one.xyz then d central team linked d central two.7z for d central users",
            "d central one.xyz then D-Central team linked d central two.7z for D-Central users",
        ),
        (
            "open d central one.txt then ask d central team to edit "
            "d central two.md then track d central users in roadmap.md",
            "open d central one.txt then ask D-Central team to edit "
            "d central two.md then track D-Central users in roadmap.md",
        ),
        (
            "d central one.txt and d central team linked "
            "d central (final two).md for d central users",
            "d central one.txt and D-Central team linked "
            "d central (final two).md for D-Central users",
        ),
        (
            "open d central one.txt, ask d central team about d central (final two).md",
            "open d central one.txt, ask D-Central team about d central (final two).md",
        ),
        (
            "measure d central latency in results.json",
            "measure D-Central latency in results.json",
        ),
        (
            "we measure d central latency in results.json",
            "we measure D-Central latency in results.json",
        ),
        (
            "the suite measured d central latency in results.json",
            "the suite measured D-Central latency in results.json",
        ),
        (
            "track d central progress in roadmap.md",
            "track D-Central progress in roadmap.md",
        ),
        (
            "we track d central progress in roadmap.md",
            "we track D-Central progress in roadmap.md",
        ),
        (
            "the team tracked d central progress in roadmap.md",
            "the team tracked D-Central progress in roadmap.md",
        ),
    )
    for store, kwargs in (
        (global_store, {"style": "plain"}),
        (scoped_store, {"style": "plain", "app": "notes.exe"}),
    ):
        assert [store.apply(text, **kwargs) for text, _ in cases] == [text for text, _ in cases]
        assert [store.apply(text, style="code") for text, _ in cases] == [text for text, _ in cases]


def test_w23l_structured_fail_close_656_case_matrix(tmp_path: Path) -> None:
    global_store = PersonalizationStore(tmp_path / "global-w23l.json")
    scoped_store = PersonalizationStore(tmp_path / "scoped-w23l.json")
    for _ in range(2):
        global_store.record_correction("d central", "D-Central")
        scoped_store.record_correction("d central", "D-Central", style="plain", app="notes.exe")

    stems = (
        "d central report",
        "my d central final-report",
        "d central (final report)",
        "d central user's guide",
        "d central R&D notes",
        "d central + final",
        "d central [final] #1",
        "d central {release} [#1]",
        "measure d central for team",
        "track d central from team",
    )
    extensions = (
        "txt",
        "md",
        "xyz",
        "blend",
        "heic",
        "wasm",
        "desktop",
        "proto",
        "parquet",
        "7z",
    )
    filenames = tuple(f"{stem}.{extension}" for stem in stems for extension in extensions)
    paths = (
        r"open C:\d central\config.ini",
        r"open C:d central\config",
        r"open \d central\config",
        r"open \\server\share\d central",
        r"open folder\d central\config",
        r"open .\d central\file",
        r"open ..\d central\file",
        r"open C:\folder-name\d central report",
        "open /d central/config",
        "open /srv/d central/config",
        "open ~/d central/config",
        "open ./d central/config",
        "open ../d central/config",
        "open folder/d central/config",
        "open //server/share/d central",
        "open /srv/d-central/d central",
    )
    urls = tuple(
        f"deploy d central via https://example{i}.test/path?q=value#fragment" for i in range(8)
    )
    emails = tuple(f"tell d central team at owner{i}@example.test" for i in range(8))
    inline_code = (
        "keep `d central` literal",
        "keep 'd central' literal",
        'keep "d central" literal',
        "compare `value` with d central",
        "say d central beside `value`",
        "use ```d central```",
        "the 'value' belongs to d central",
        'd central is labeled "value"',
    )
    code = (
        "d central = value",
        "result = d central + 1",
        "emit(d central)",
        "[d central]",
        "{d central: 1}",
        "from d central import value",
        "const name = d central",
        "# deploy d central",
    )
    shell = (
        "echo d central",
        "git --branch d central",
        "FOO=d-central pytest d central",
        "python -m d central",
        "rg d central",
        "curl https://example.test d central",
        "npm run d central",
        "docker build d central",
    )
    mixed = (
        "measure d central latency in results.json",
        "track d central progress for users in roadmap.md",
        "d central one.txt and d central two.md",
        "open d central one.txt, ask d central team about d central two.md",
        "deploy d central then email owner@example.test",
        "deploy d central using /srv/config",
        "deploy d central with `--flag`",
        "deploy d central then run pytest --quiet",
    )
    corpus = filenames + paths + urls + emails + inline_code + code + shell + mixed
    assert len(filenames) == 100
    assert len(corpus) == 164

    checks = 0
    for store, app in ((global_store, None), (scoped_store, "notes.exe")):
        for style in ("plain", "code"):
            for text in corpus:
                assert store.apply(text, style=style, app=app) == text
                checks += 1
    assert checks == 656

    assert global_store.apply("Deploy the d central node.", style="plain", prose_context=True) == (
        "Deploy the D-Central node."
    )


def test_w23m_high_confidence_prose_gate_exact_corpora(tmp_path: Path) -> None:
    global_store = PersonalizationStore(tmp_path / "global-w23m.json")
    scoped_store = PersonalizationStore(tmp_path / "scoped-w23m.json")
    for _ in range(2):
        global_store.record_correction("d central", "D-Central")
        scoped_store.record_correction("d central", "D-Central", style="plain", app="notes.exe")

    structured = (
        "sudo apt install d central.",
        "dcent-voice transcribe d central.",
        "Get-ChildItem d central.",
        "cp source d central.",
        "mv source d central.",
        "systemctl restart d central.",
        "Set-Location d central.",
        "cat d central.",
        "awk d central.",
        "grep d central.",
        "openssl verify d central.",
        "name: d central.",
        "x > d central.",
        "service: d central.",
        "SELECT d central FROM records.",
        "value: d central.",
        "x == d central.",
        "x && d central.",
        "value | d central.",
        "return {d central}.",
        "items[d central].",
        "emit(d central).",
        "Keep `d central` literal.",
        'Keep "d central" literal.',
        "Visit https://example.test/d central.",
        "Email d central@example.test.",
        "Open /srv/d central/config.",
        "Keep d central report.thisextensionistoolong.",
        "Keep d central report.配置。",
        "Please deploy d central.\nThen ship.",
    )
    assert len(structured) == 30

    prose = (
        ("If d central agrees, ship today.", "If D-Central agrees, ship today."),
        (
            "For d central customers, privacy matters.",
            "For D-Central customers, privacy matters.",
        ),
        ("From d central, we learned a lot.", "From D-Central, we learned a lot."),
        ("Try d central today.", "Try D-Central today."),
        (
            "Return d central to the agenda.",
            "Return D-Central to the agenda.",
        ),
        ("Class d central as ready.", "Class D-Central as ready."),
        ("While d central runs, wait here.", "While D-Central runs, wait here."),
        (
            "Function d central remains simple.",
            "Function D-Central remains simple.",
        ),
        ("Go d central team!", "Go D-Central team!"),
        ("Make d central simpler.", "Make D-Central simpler."),
        ("Echo d central clearly.", "Echo D-Central clearly."),
        (
            "Please ask Alice (our lead) about d central.",
            "Please ask Alice (our lead) about D-Central.",
        ),
        (
            "The score is 9.5 and d central won.",
            "The score is 9.5 and D-Central won.",
        ),
        (
            "We owe d central $3.50 dollars.",
            "We owe D-Central $3.50 dollars.",
        ),
        (
            "D central's privacy-first design works.",
            "D-Central's privacy-first design works.",
        ),
        (
            "After review, d central ships.",
            "After review, D-Central ships.",
        ),
    )
    controls = (
        ("Please deploy d central today.", "Please deploy D-Central today."),
        ("The d central team agreed.", "The D-Central team agreed."),
        ("Our d central node is ready.", "Our D-Central node is ready."),
        ("Everyone supports d central now.", "Everyone supports D-Central now."),
        ("Yesterday d central shipped.", "Yesterday D-Central shipped."),
        ("Privacy makes d central useful.", "Privacy makes D-Central useful."),
    )
    assert len(prose) == 16
    assert len(controls) == 6

    for store, app in ((global_store, None), (scoped_store, "notes.exe")):
        assert [store.apply(text, style="plain", app=app) for text in structured] == list(
            structured
        )
        for corpus in (prose, controls):
            assert [store.apply(text, style="plain", app=app) for text, _ in corpus] == [
                text for text, _ in corpus
            ]
            assert [
                store.apply(
                    text,
                    style="plain",
                    app=app,
                    prose_context=True,
                )
                for text, _ in corpus
            ] == [expected for _, expected in corpus]
        assert [store.apply(text, style="code", app=app) for text, _ in prose] == [
            text for text, _ in prose
        ]

    assert global_store.apply("Please deploy d central", style="plain") == (
        "Please deploy d central"
    )
    assert global_store.apply("d central", style="plain") == "D-Central"


def test_w23n_explicit_context_replaces_text_shape_inference(tmp_path: Path) -> None:
    global_store = PersonalizationStore(tmp_path / "global-w23n.json")
    scoped_store = PersonalizationStore(tmp_path / "scoped-w23n.json")
    for _ in range(2):
        global_store.record_correction("d central", "D-Central")
        scoped_store.record_correction("d central", "D-Central", style="plain", app="notes.exe")

    prose = (
        "Our verdict: d central is ready.",
        "The team agreed; d central should ship.",
        "At 9:30, d central started.",
        'Everyone called it "d central" yesterday.',
        "If d central agrees, ship today.",
        "For d central customers, privacy matters.",
        "Please ask Alice (our lead) about d central.",
        "The score is 9.5 and d central won.",
        "We owe d central $3.50 dollars.",
        "D central's privacy-first design works.",
        "The privacy-first d central release is ready.",
        "After review, d central ships.",
        "The result—d central worked—was clear.",
        "From d central, we learned a lot.",
        "Try d central today.",
        "Echo d central clearly.",
    )
    structured = (
        "Sudo apt install d central.",
        "Git status d central.",
        "Use fooBar with d central.",
        "Dcent-voice transcribe d central.",
        "Kubectl get pods d central.",
        "Python -m tool d central.",
        "Get-ChildItem d central.",
        "Set-Location d central.",
        "SELECT d central FROM records.",
        "name: d central.",
        "value: d central.",
        "x == d central.",
        "x > d central.",
        "x && d central.",
        "emit(d central).",
        "items[d central].",
        "Keep `d central` literal.",
        'Keep "d central" literal.',
        "Open /srv/d central/config.",
        r"Open C:\d central\config.",
        "Keep d central report.thisextensionistoolong.",
        "Keep d central report.配置。",
        "Visit https://example.test/d central.",
        "Email d central@example.test.",
        "Please deploy d central.\nThen ship.",
    )
    assert len(prose) == 16
    assert len(structured) == 25

    for store, app in ((global_store, None), (scoped_store, "notes.exe")):
        assert [store.apply(text, style="plain", app=app) for text in prose] == list(prose)
        assert [
            store.apply(text, style="plain", app=app, prose_context=True) for text in prose
        ] == [
            text.replace("d central", "D-Central").replace("D central", "D-Central")
            for text in prose
        ]
        assert [store.apply(text, style="plain", app=app) for text in structured] == list(
            structured
        )
        clear_literals = structured[11:17] + structured[18:24]
        assert [
            store.apply(text, style="plain", app=app, prose_context=True) for text in clear_literals
        ] == list(clear_literals)
        assert [
            store.apply(text, style="code", app=app, prose_context=True) for text in prose
        ] == list(prose)


def test_scopes_persist_and_do_not_leak_across_destinations(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    store = PersonalizationStore(path)
    store.record_correction("flow", "FLOW", style="code", app="Code.exe")
    store.record_correction("flow", "Flo", style="chat", app="Slack.exe")

    again = PersonalizationStore(path)
    assert again.apply("flow", style="code", app=r"C:\Apps\Code.exe") == "FLOW"
    assert again.apply("flow", style="chat", app="slack.exe") == "Flo"
    assert again.apply("flow", style="email", app="outlook.exe") == "flow"
    assert again.as_vocab() == ()
    assert again.as_vocab(style="email", app="outlook.exe") == ()
    assert {term["style"] for term in again.snapshot()["terms"]} == {"code", "chat"}


def test_equal_specificity_conflict_fails_closed(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("pie test", "pytest", style="code")
    store.record_correction("pie test", "PyTest", app="code.exe")
    assert store.apply("run pie test", style="code", app="code.exe") == "run pie test"


def test_overlapping_mappings_use_original_text_and_longest_match(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("d central", "D-Central")
    store.record_correction("central", "Core")

    assert store.apply("Open d central settings.", prose_context=True) == (
        "Open D-Central settings."
    )
    assert store.apply("Central settings.") == "Central settings."
    assert store.apply("D central and central.", prose_context=True) == ("D-Central and central.")


def test_prose_corrections_never_rewrite_structured_tokens(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("there", "their")
    store.record_correction("git", "Git")

    plain_structured = (
        "https://there.example/there?q=there#there",
        "email there@example.com",
        "open /srv/there/config.toml",
        r"open C:\Users\there\config.ini",
        r"open .\there\file.txt",
        "open src/there.py and there.txt",
        'open "there report.txt"',
        "keep `there` in Markdown",
        "read obj.there and package.there.value",
        "const there = obj.there;",
        "there = obj.there",
        "there(value)",
        "git --branch=there /tmp/there",
        "echo there",
        "git --branch there",
        "FOO=there pytest",
    )
    for style in ("plain", "code"):
        for text in plain_structured:
            assert store.apply(text, style=style) == text

    code_structured = (
        "const there = obj.there;",
        "if there: return there",
        'print("there")',
        "git --there=there && curl https://there.example/there",
        "THERE=/tmp/there",
        "result = there + 1",
        "emit(there)",
        "[there]",
        "{there:1}",
        "from there import value",
        "obj[there]",
    )
    for text in code_structured:
        assert store.apply(text, style="code") == text

    assert store.apply("There!", style="plain") == "Their!"
    assert store.apply("  THERE?  ", style="plain") == "  THEIR?  "
    assert store.apply("there…", style="plain") == "their…"
    assert store.apply("there …", style="plain") == "their …"
    assert store.apply("there !", style="plain") == "their !"
    assert store.apply("There！", style="plain") == "Their！"
    assert store.apply("there？", style="plain") == "their？"
    assert store.apply("there，", style="plain") == "their，"
    assert store.apply("there。", style="plain") == "their。"
    for style in ("plain", "code"):
        assert store.apply("there", style=style) == "their"
        assert store.apply("Please let there team decide", style=style) == (
            "Please let there team decide"
        )
    assert (
        store.apply(
            "visit https://there.example/there?q=there, then there is prose",
            style="plain",
        )
        == "visit https://there.example/there?q=there, then there is prose"
    )


def test_protected_spans_preserve_scoped_brand_and_developer_corrections(
    tmp_path: Path,
) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    for _ in range(2):
        store.record_correction("d central", "D-Central", style="plain", app="notes.exe")
        store.record_correction("pie test", "pytest", style="plain", app="notes.exe")

    prose = "Deploy the d central node and run pie test."
    assert store.apply(prose, style="plain", app="notes.exe", prose_context=True) == (
        "Deploy the D-Central node and run pytest."
    )
    structured = "open d-central.md and /srv/pie-test/config.toml"
    assert store.apply(structured, style="plain", app="notes.exe") == structured
    assert store.apply(prose, style="email", app="outlook.exe") == prose


def test_single_ascii_name_defaults_to_whole_utterance_only(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("satoshi", "Satoshi")
    store.record_correction("satoshi", "Satoshi")

    assert store.apply("satoshi") == "Satoshi"
    assert store.apply("SATOSHI!") == "SATOSHI!"
    assert store.apply("ask satoshi to review") == "ask satoshi to review"
    assert store.apply("satoshi's key") == "satoshi's key"


def test_w23d_ambiguous_single_matrix_fails_closed_in_longer_text(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("there", "their")
    groups = (
        (
            r"C:there\file.txt",
            r"\there\file.txt",
            r"folder\there\report.txt",
            r".\there\file.txt",
            r"..\there\file.txt",
            r"C:\there\file.txt",
            r"\\server\share\there\file.txt",
            "/there/file.txt",
            "./there/file.txt",
            "../there/file.txt",
            "~/there/file.txt",
            "folder/there/report.txt",
            "there.txt",
            ".there",
            '"there report.txt"',
        ),
        (
            "https://there.example/there?q=there#there",
            "http://example.test/there",
            "https://example.test/?next=there",
            "https://example.test/#there",
            "there@example.com",
            "ftp://there.example/file",
            "www.there.example/path",
            "[there](https://example.test)",
            "use `there` inline",
            '<a id="there">link</a>',
        ),
        (
            "const there = obj.there;",
            "let there = value;",
            "var there = value;",
            "result = there + 1",
            "result = there, other",
            "for there in values:",
            "lambda there: there + 1",
            "items = [there for there in xs]",
            "emit(there)",
            "[there]",
            "mapping = {there: 1}",
            "from there import value",
            "import there as module",
            "obj[there]",
            "obj.there",
            "there(value)",
            "if there: pass",
            "while there: break",
            "return there",
            "await there",
            "$there",
            "$env:there",
            "${there}",
            "${env:there}",
            "# there",
            "// there",
            "/* there */",
            "there => value",
            "function f(there) {}",
            "def f(there): pass",
        ),
        (
            "echo there",
            "git --branch there",
            "FOO=there pytest",
            "./tool there",
            "Get-Item -Path there",
            "custom | grep there",
            "printf '%s' there",
            "sed -n there input.txt",
            "awk there input.txt",
            "python --arg there",
            "docker run --env VALUE=there image",
            "kubectl get pods --selector there",
            "npm run build -- there",
            "cargo test there",
            'powershell -Command "there"',
            "cmd /c echo there",
            "unknown-command there",
            "cat there.txt",
            "ssh host there",
            "rg there src",
        ),
        (
            "Please let there team decide",
            "I went (there) yesterday.",
            "Choose [there] for emphasis.",
            "Well, there: that is the point.",
            "Please import there ideas into the plan.",
            "We stayed there yesterday.",
            "Is there any alternative?",
            "Put it there please.",
            "From there we walked home.",
            'He said "there" yesterday.',
            "There, however, was no answer.",
            "Go there!",
        ),
    )
    matrix = tuple(text for group in groups for text in group)
    assert len(matrix) == 87

    for style in ("plain", "code"):
        assert [store.apply(text, style=style) for text in matrix] == list(matrix)


def test_cjk_terms_apply_in_unspaced_sentences_with_safe_identifier_guards(
    tmp_path: Path,
) -> None:
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("比特币", "Bitcoin")
    store.record_correction("サトシ", "Satoshi")
    store.record_correction("中", "Middle")

    assert store.apply("我使用比特币钱包。", prose_context=True) == ("我使用Bitcoin钱包。")
    assert store.apply("サトシの鍵。", prose_context=True) == "Satoshiの鍵。"
    assert store.apply("abc比特币def。") == "abc比特币def。"
    assert store.apply("中国。") == "中国。"
    assert store.apply("在 中 工作。", prose_context=True) == ("在 Middle 工作。")


def test_store_is_bounded_and_declares_explicit_retention(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    store = PersonalizationStore(path)
    assert store.record_correction("x" * 257, "bounded") is None
    assert store.record_correction("bounded", "x" * 257) is None
    store.note_utterance("private raw transcript", "Private cleaned transcript")
    store.record_correction("d sent", "DCENT_Voice")

    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshot = store.snapshot()
    assert payload["version"] == 3
    assert "private raw transcript" not in path.read_text(encoding="utf-8")
    assert snapshot["learning_requires_explicit_correction"] is True
    assert snapshot["retention"] == {"max_terms": 400, "max_phrase_chars": 256}


def test_same_path_store_threads_merge_acknowledged_corrections(tmp_path: Path) -> None:
    path = tmp_path / "shared.json"
    first = PersonalizationStore(path)
    second = PersonalizationStore(path)
    barrier = threading.Barrier(3)
    results: list[bool] = []

    def record(store: PersonalizationStore, spoken: str, written: str) -> None:
        barrier.wait()
        results.append(store.record_correction(spoken, written) is not None)

    threads = [
        threading.Thread(target=record, args=(first, "project alpha", "ProjectAlpha")),
        threading.Thread(target=record, args=(second, "project beta", "ProjectBeta")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert results == [True, True]
    persisted = PersonalizationStore(path).snapshot()["terms"]
    assert {term["spoken"] for term in persisted} == {
        "project alpha",
        "project beta",
    }


def test_stale_same_path_conflict_is_rejected_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "conflict.json"
    first = PersonalizationStore(path)
    stale = PersonalizationStore(path)

    assert first.record_correction("client name", "AlphaClient") is not None
    assert stale.record_correction("client name", "BetaClient") is None
    persisted = PersonalizationStore(path)
    assert persisted.apply("client name") == "AlphaClient"


def test_same_path_processes_merge_acknowledged_corrections(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "process-shared.json"
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_record_in_process,
            args=(str(path), "process alpha", "ProcessAlpha", ready, start, results),
        ),
        context.Process(
            target=_record_in_process,
            args=(str(path), "process beta", "ProcessBeta", ready, start, results),
        ),
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=15) is True
    assert ready.get(timeout=15) is True
    start.set()
    outcomes = [results.get(timeout=20), results.get(timeout=20)]
    for process in processes:
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert outcomes == [True, True]
    assert all(process.exitcode == 0 for process in processes)
    persisted = PersonalizationStore(path).snapshot()["terms"]
    assert {term["spoken"] for term in persisted} == {
        "process alpha",
        "process beta",
    }


def test_stale_public_policy_save_merges_remote_terms_and_is_repeatable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale-policy-save.json"
    writer = PersonalizationStore(path)
    stale_policy = PersonalizationStore(path)
    assert writer.record_correction("project alpha", "ProjectAlpha") is not None

    stale_policy.update_policy(enabled=False, learn=False)
    stale_policy.save()
    stale_policy.save()

    persisted = PersonalizationStore(path)
    assert (persisted.enabled, persisted.learn) == (False, False)
    terms = persisted.snapshot()["terms"]
    assert [term["spoken"] for term in terms] == ["project alpha"]


def test_stale_public_save_does_not_resurrect_explicit_remote_reset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reset-vs-save.json"
    resetter = PersonalizationStore(path)
    assert resetter.record_correction("project alpha", "ProjectAlpha")
    stale = PersonalizationStore(path)

    resetter.reset()
    stale.update_policy(enabled=False, learn=False)
    stale.save()

    persisted = PersonalizationStore(path)
    assert persisted.snapshot()["term_count"] == 0
    assert (persisted.enabled, persisted.learn) == (False, False)


def test_stale_public_policy_save_merges_process_correction(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "process-policy-save.json"
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    process = context.Process(
        target=_policy_save_in_process,
        args=(str(path), ready, start, results),
    )
    process.start()
    assert ready.get(timeout=15) is True
    assert PersonalizationStore(path).record_correction("remote project", "RemoteProject")
    start.set()
    assert results.get(timeout=20) is True
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)

    assert process.exitcode == 0
    persisted = PersonalizationStore(path)
    assert persisted.snapshot()["term_count"] == 1
    assert persisted.snapshot()["terms"][0]["spoken"] == "remote project"
    assert (persisted.enabled, persisted.learn) == (False, False)


def test_public_save_three_way_conflict_fails_closed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "public-conflict.json"
    seed = PersonalizationStore(path)
    assert seed.record_correction("client name", "OriginalClient")
    remote = PersonalizationStore(path)
    local = PersonalizationStore(path)

    with monkeypatch.context() as local_only:
        local_only.setattr(local, "save", lambda: None)
        assert local.record_correction("client name", "LocalClient")
    assert remote.record_correction("client name", "RemoteClient")
    before_disk = path.read_bytes()

    with pytest.raises(ValueError, match="concurrent mapping conflict"):
        local.save()

    assert path.read_bytes() == before_disk
    assert PersonalizationStore(path).apply("client name") == "RemoteClient"


def test_advisory_lock_timeout_is_bounded_and_recoverable(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "lock-timeout.json"
    ready = context.Queue()
    release = context.Event()
    holder = context.Process(
        target=_hold_store_lock,
        args=(str(path), ready, release),
    )
    holder.start()
    assert ready.get(timeout=15) is True
    store = PersonalizationStore(path)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="advisory lock"):
        store.record_correction("blocked project", "BlockedProject")
    elapsed = time.monotonic() - started
    assert elapsed < 2.5
    assert store.snapshot()["term_count"] == 0

    release.set()
    holder.join(10)
    if holder.is_alive():
        holder.terminate()
        holder.join(5)
    assert holder.exitcode == 0
    assert store.record_correction("recovered project", "RecoveredProject")


def test_atomic_writer_fsyncs_before_replace(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "durable.json"
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = Path.replace

    def tracked_fsync(fd: int) -> None:
        events.append("fsync")
        original_fsync(fd)

    def tracked_replace(self: Path, target: Path) -> Path:
        assert events and events[-1] == "fsync"
        events.append("replace")
        return original_replace(self, target)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    monkeypatch.setattr(Path, "replace", tracked_replace)
    assert PersonalizationStore(path).record_correction("durable project", "DurableProject")

    assert events[:2] == ["fsync", "replace"]
    assert PersonalizationStore(path).snapshot()["term_count"] == 1


def test_fsync_and_directory_sync_failures_roll_back_and_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    import dcent_voice.personalization as personalization_module

    path = tmp_path / "durability-failure.json"
    store = PersonalizationStore(path)
    assert store.record_correction("original project", "OriginalProject")
    before_snapshot = store.snapshot()
    before_disk = path.read_bytes()

    with monkeypatch.context() as file_sync_failure:
        file_sync_failure.setattr(
            os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("file sync failed")),
        )
        with pytest.raises(OSError, match="file sync failed"):
            store.record_correction("new project", "NewProject")
    assert store.snapshot() == before_snapshot
    assert path.read_bytes() == before_disk
    assert not list(tmp_path.glob("*.tmp"))

    with monkeypatch.context() as directory_sync_failure:
        directory_sync_failure.setattr(
            personalization_module,
            "_sync_parent_directory",
            lambda _path: (_ for _ in ()).throw(OSError("directory sync failed")),
        )
        with pytest.raises(OSError, match="directory sync failed"):
            store.record_correction("other project", "OtherProject")
    assert store.snapshot() == before_snapshot
    assert path.read_bytes() == before_disk
    assert not list(tmp_path.glob("*.rollback"))


def test_public_save_failure_restores_last_durable_policy_and_terms(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "policy-rollback.json"
    store = PersonalizationStore(path)
    assert store.record_correction("d central", "D-Central")
    before_snapshot = store.snapshot()
    before_disk = path.read_bytes()
    store.update_policy(enabled=False, learn=False)

    monkeypatch.setattr(
        os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("sync failed")),
    )
    with pytest.raises(OSError, match="sync failed"):
        store.save()

    assert store.snapshot() == before_snapshot
    assert path.read_bytes() == before_disk


def test_stale_unchanged_policy_preserves_newer_remote_policy(tmp_path: Path) -> None:
    path = tmp_path / "policy-three-way.json"
    remote = PersonalizationStore(path)
    remote.save()
    stale = PersonalizationStore(path)

    remote.update_policy(enabled=False, learn=False)
    remote.save()
    stale.save()

    persisted = PersonalizationStore(path)
    assert (persisted.enabled, persisted.learn) == (False, False)


def test_explicit_policy_change_wins_clean_base_and_conflicts_if_both_diverge(
    tmp_path: Path,
) -> None:
    clean_path = tmp_path / "clean-policy-change.json"
    seed = PersonalizationStore(clean_path)
    seed.save()
    local = PersonalizationStore(clean_path)
    remote = PersonalizationStore(clean_path)
    assert remote.record_correction("remote term", "RemoteTerm")
    local.update_policy(enabled=False, learn=False)
    local.save()
    persisted = PersonalizationStore(clean_path)
    assert (persisted.enabled, persisted.learn) == (False, False)
    assert persisted.snapshot()["term_count"] == 1

    conflict_path = tmp_path / "policy-conflict.json"
    seed = PersonalizationStore(conflict_path)
    seed.save()
    first = PersonalizationStore(conflict_path)
    second = PersonalizationStore(conflict_path)
    first.update_policy(enabled=False, learn=True)
    first.save()
    before = conflict_path.read_bytes()
    second.update_policy(enabled=True, learn=False)
    with pytest.raises(ValueError, match="concurrent policy conflict"):
        second.save()
    assert conflict_path.read_bytes() == before
    persisted = PersonalizationStore(conflict_path)
    assert (persisted.enabled, persisted.learn) == (False, True)

    override_path = tmp_path / "explicit-constructor-policy.json"
    saved = PersonalizationStore(override_path)
    saved.update_policy(enabled=False, learn=False)
    saved.save()
    explicit = PersonalizationStore(override_path, enabled=True, learn=True)
    explicit.save()
    persisted = PersonalizationStore(override_path)
    assert (persisted.enabled, persisted.learn) == (True, True)


def test_startup_and_reset_remove_only_scoped_private_crash_artifacts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personalization.json"
    store = PersonalizationStore(path)
    assert store.record_correction("private codename", "PrivateCodename")
    private = b'{"spoken":"private codename","written":"PrivateCodename"}'
    crash_tmp = tmp_path / ".personalization.json.dead.tmp"
    crash_rollback = tmp_path / ".personalization.json.dead.rollback"
    unrelated = tmp_path / ".other.json.dead.tmp"
    crash_tmp.write_bytes(private)
    crash_rollback.write_bytes(private)
    unrelated.write_bytes(private)

    PersonalizationStore(path)
    assert not crash_tmp.exists()
    assert not crash_rollback.exists()
    assert unrelated.read_bytes() == private

    crash_tmp.write_bytes(private)
    crash_rollback.write_bytes(private)
    store.reset()
    assert not crash_tmp.exists()
    assert not crash_rollback.exists()
    assert PersonalizationStore(path).snapshot()["term_count"] == 0
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_directory_sync_and_failed_rollback_reconcile_visible_state(
    tmp_path: Path, monkeypatch
) -> None:
    import dcent_voice.personalization as personalization_module

    path = tmp_path / "compound-durability.json"
    store = PersonalizationStore(path)
    assert store.record_correction("original term", "OriginalTerm")
    original_replace = Path.replace
    replaces = 0

    def fail_rollback_replace(self: Path, target: Path) -> Path:
        nonlocal replaces
        replaces += 1
        if replaces == 1:
            return original_replace(self, target)
        raise OSError("rollback replace failed")

    monkeypatch.setattr(Path, "replace", fail_rollback_replace)
    monkeypatch.setattr(
        personalization_module,
        "_sync_parent_directory",
        lambda _path: (_ for _ in ()).throw(OSError("directory sync failed")),
    )

    with pytest.raises(PersistenceStateReconciledError, match="reconciled"):
        store.record_correction("failed new term", "FailedNewTerm")

    memory_terms = store.snapshot()["terms"]
    disk_terms = PersonalizationStore(path).snapshot()["terms"]
    assert memory_terms == disk_terms
    assert {term["spoken"] for term in disk_terms} == {
        "original term",
        "failed new term",
    }
    assert not list(tmp_path.glob("*.rollback"))


def test_save_failure_rolls_back_record_update_reset_and_direct_retry(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "rollback.json"
    store = PersonalizationStore(path)
    assert store.record_correction("d central", "D-Central") is not None
    store.note_utterance("hello d sent", "Hello d sent")
    before_snapshot = store.snapshot()
    before_disk = path.read_bytes()
    original_replace = Path.replace

    def fail_replace(self: Path, target: Path) -> Path:
        del self, target
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        store.record_correction("private client", "PrivateClient")
    assert store.snapshot() == before_snapshot
    assert path.read_bytes() == before_disk

    with pytest.raises(OSError, match="simulated"):
        store.record_correction("d central", "DCentralChanged")
    assert store.snapshot() == before_snapshot
    assert path.read_bytes() == before_disk

    with pytest.raises(OSError, match="simulated"):
        store.reset()
    assert store.snapshot() == before_snapshot
    assert path.read_bytes() == before_disk

    with pytest.raises(OSError, match="simulated"):
        store.learn_last("Hello DCENT_Voice")
    assert store.snapshot() == before_snapshot
    assert path.read_bytes() == before_disk

    monkeypatch.setattr(Path, "replace", original_replace)
    learned = store.learn_last("Hello DCENT_Voice")
    assert learned is not None
    assert learned.spoken == "d sent"


def test_unicode_nfc_identity_matching_and_transactional_duplicate_rejection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nfc.json"
    store = PersonalizationStore(path)
    assert store.record_correction("cafe\u0301", "Café") is not None
    assert store.apply("café") == "Café"
    assert store.apply("cafe\u0301") == "Café"
    # Equivalent public identity updates one term rather than creating twins.
    assert store.record_correction("café", "Café") is not None
    assert store.snapshot()["term_count"] == 1

    duplicate_path = tmp_path / "nfc-duplicate.json"
    payload = _valid_v3_payload()
    payload["terms"] = [
        _valid_v3_term(spoken="café", written="Café"),
        _valid_v3_term(spoken="cafe\u0301", written="Other"),
    ]
    duplicate_path.write_text(json.dumps(payload), encoding="utf-8")
    rejected = PersonalizationStore(duplicate_path)
    assert (rejected.enabled, rejected.learn) == (False, False)
    assert rejected.snapshot()["term_count"] == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO/symlink contract")
def test_store_open_rejects_fifo_and_symlink_without_blocking(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_valid_v3_payload()), encoding="utf-8")
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    linked = PersonalizationStore(symlink)
    assert (linked.enabled, linked.learn) == (False, False)
    assert linked.snapshot()["term_count"] == 0

    fifo = tmp_path / "state.fifo"
    os.mkfifo(fifo)
    opened: list[PersonalizationStore] = []
    worker = threading.Thread(target=lambda: opened.append(PersonalizationStore(fifo)))
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert len(opened) == 1
    assert (opened[0].enabled, opened[0].learn) == (False, False)


@pytest.mark.skipif(os.name == "nt", reason="POSIX replacement-race contract")
def test_store_open_fstats_replacement_fifo_without_blocking(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "raced.json"
    path.write_text(json.dumps(_valid_v3_payload()), encoding="utf-8")
    original_open = os.open
    replaced = False

    def racing_open(candidate, flags, *args):
        nonlocal replaced
        if Path(candidate) == path and not replaced:
            replaced = True
            path.unlink()
            os.mkfifo(path)
        return original_open(candidate, flags, *args)

    monkeypatch.setattr(os, "open", racing_open)
    opened: list[PersonalizationStore] = []
    worker = threading.Thread(target=lambda: opened.append(PersonalizationStore(path)))
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert len(opened) == 1
    assert (opened[0].enabled, opened[0].learn) == (False, False)


def test_capacity_evicts_actual_oldest_and_retains_accepted_new_term(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "personalization.json"
    store = PersonalizationStore(path)
    monkeypatch.setattr(store, "save", lambda: None)
    for index in range(400):
        assert store.record_correction(f"term {index:03}", f"Value{index:03}")

    # A reconfirmed mapping is newest, so the next insertion evicts term 001.
    assert store.record_correction("term 000", "Value000")
    accepted = store.record_correction("term 400", "Value400")
    assert accepted is not None
    assert accepted.spoken == "term 400"
    terms = store.snapshot()["terms"]
    spoken = [term["spoken"] for term in terms]
    assert len(terms) == 400
    assert "term 000" in spoken
    assert "term 001" not in spoken
    assert spoken[-1] == "term 400"

    PersonalizationStore.save(store)
    reloaded = PersonalizationStore(path)
    assert [term["spoken"] for term in reloaded.snapshot()["terms"]] == spoken


def test_v2_capacity_migration_uses_confirmation_time_not_file_sort(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    terms = [
        {
            "spoken": f"legacy {index:03}",
            "written": f"Legacy{index:03}",
            "count": 1,
            "source": "typed",
            "updated_at": f"2026-01-{1 + index // 24:02}T{index % 24:02}:00:00+00:00",
        }
        for index in range(401)
    ]
    terms.reverse()  # Simulate the old non-chronological display ordering.
    path.write_text(json.dumps({"version": 2, "terms": terms}), encoding="utf-8")

    store = PersonalizationStore(path)
    spoken = [term["spoken"] for term in store.snapshot()["terms"]]

    assert len(spoken) == 400
    assert "legacy 000" not in spoken
    assert spoken[-1] == "legacy 400"


def test_v1_store_migrates_as_global_terms(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "terms": [
                    {
                        "spoken": "d sent",
                        "written": "DCENT_Voice",
                        "count": 2,
                        "source": "typed",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = PersonalizationStore(path)
    assert store.apply("d sent", style="code", app="code.exe") == "DCENT_Voice"


def test_learned_app_styles_inspect_reset_and_activation(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    store = PersonalizationStore(path)
    first = store.record_correction(
        "invoice ready", "The invoice is ready.", style="email", app="notepad.exe"
    )
    assert first is not None
    assert store.learned_app_styles() == {}
    assert store.snapshot()["app_styles"][0]["count"] == 1

    second = store.record_correction(
        "invoice ready", "The invoice is ready.", style="email", app="notepad.exe"
    )
    assert second is not None
    assert store.learned_app_styles() == {"notepad.exe": "email"}

    typed = store.remember_app_style("Code.exe", "formal", immediate=True)
    assert typed is not None
    assert typed.count >= 2
    learned = store.learned_app_styles()
    assert learned["notepad.exe"] == "email"
    assert learned["code.exe"] == "formal"

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert "audio" not in payload
    assert payload["app_styles"]
    reloaded = PersonalizationStore(path)
    assert reloaded.learned_app_styles() == learned
    assert reloaded.snapshot()["stores_audio"] is False

    store.reset_app_styles()
    assert store.learned_app_styles() == {}
    assert store.snapshot()["terms"][0]["written"] == "The invoice is ready."
    assert PersonalizationStore(path).learned_app_styles() == {}

    store.reset()
    assert store.as_vocab() == ()
    assert store.snapshot()["app_styles"] == []


def test_unscoped_correction_does_not_learn_plain_app_style(tmp_path: Path) -> None:
    store = PersonalizationStore(tmp_path / "unscoped.json")
    store.record_correction("d sent", "DCENT_Voice", source="typed", app="notepad.exe")
    assert store.learned_app_styles() == {}
    assert store.snapshot()["app_styles"] == []


def test_legacy_v3_store_without_app_styles_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v3.json"
    path.write_text(json.dumps(_valid_v3_payload()), encoding="utf-8")
    store = PersonalizationStore(path)
    assert store.as_vocab()[0].written == "D-Central"
    assert store.learned_app_styles() == {}
    assert store.snapshot()["app_styles"] == []


def test_store_rejects_even_empty_audio_fields(tmp_path: Path) -> None:
    path = tmp_path / "personalization.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "audio": [],
                "terms": [{"spoken": "foo", "written": "bar"}],
            }
        ),
        encoding="utf-8",
    )
    assert PersonalizationStore(path).as_vocab() == ()
