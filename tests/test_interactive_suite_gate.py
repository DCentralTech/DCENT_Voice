# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Release gate for live desktop, clipboard, focus, hotkey, and microphone tests."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _function_decorators(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return {ast.unparse(decorator) for decorator in node.decorator_list}
    raise AssertionError(f"missing gated test {path.name}::{function_name}")


def _functions_with_decorator(path: Path, decorator_name: str) -> dict[str, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
        if decorator_name in decorators:
            found[node.name] = decorators
    return found


def _module_markers(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            assert node.value is not None
            return ast.unparse(node.value)
    raise AssertionError(f"missing pytestmark in {path.name}")


def test_live_desktop_tests_require_the_exact_explicit_environment_opt_in() -> None:
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'INTERACTIVE_TEST_ENV = "DCENT_VOICE_ALLOW_INTERACTIVE_TESTS"' in conftest
    assert 'os.environ.get(INTERACTIVE_TEST_ENV) == "1"' in conftest
    assert '"interactive" in item.keywords' in conftest


def test_every_known_live_desktop_test_is_marked_interactive() -> None:
    gated = {
        "test_clipboard_inject.py": "test_real_clipboard_snapshot_restore_round_trip",
        "test_windows_injection_self_test.py": (
            "test_real_windows_injection_into_private_native_edit_control"
        ),
    }
    for filename, function_name in gated.items():
        decorators = _function_decorators(ROOT / "tests" / filename, function_name)
        assert "pytest.mark.interactive" in decorators


def test_every_shipped_windows_end_to_end_test_is_marked_interactive() -> None:
    """Do not let native app/mic/installer tests leak back into routine pytest."""

    path = ROOT / "tests" / "test_dictation_postprocess.py"
    native_tests = _functions_with_decorator(path, "requires_win32_native")
    assert native_tests, "expected shipped Windows end-to-end coverage"
    missing = sorted(
        function_name
        for function_name, decorators in native_tests.items()
        if "pytest.mark.interactive" not in decorators
    )
    assert missing == []


def test_windows_installer_and_user_profile_integrations_are_interactive() -> None:
    uninstaller = ROOT / "tests" / "test_windows_uninstaller.py"
    assert "pytest.mark.interactive" in _module_markers(uninstaller)

    recovery = ROOT / "tests" / "test_setup_recovery.py"
    # Every native setup function is detected below by its skipif prefix; the
    # explicit list count makes adding an ungated native function fail closed.
    tree = ast.parse(recovery.read_text(encoding="utf-8"), filename=str(recovery))
    missing: list[str] = []
    native_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
        if any("sys.platform != 'win32'" in decorator for decorator in decorators):
            native_count += 1
            if "pytest.mark.interactive" not in decorators:
                missing.append(node.name)
    assert native_count == 8
    assert missing == []


def test_ci_and_release_workflows_never_enable_interactive_tests() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / ".github" / "workflows").glob("*.yml")
    )
    assert "DCENT_VOICE_ALLOW_INTERACTIVE_TESTS" not in workflows
