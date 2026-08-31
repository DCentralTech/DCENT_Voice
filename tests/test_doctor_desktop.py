# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Linux and macOS desktop-host diagnostics (WS9).

Every test fakes the host, because the point of these checks is to describe
machines this suite will never run on: a Wayland desktop with no ``wtype``, an
Ubuntu 22.04 box with the 4.0 WebKit typelib, a Mac whose owner has not yet
granted Accessibility. The checks must produce the right severity and a
remediation a person can act on without any of those hosts being present.
"""

from __future__ import annotations

import plistlib
import sys

import pytest

from dcent_voice.doctor.checks import desktop
from dcent_voice.doctor.result import FAIL, PASS, WARN


def _by_id(results, check_id):
    matches = [result for result in results if result.id == check_id]
    assert matches, f"{check_id} missing from {[result.id for result in results]}"
    return matches[0]


def _linux(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")


def _macos(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")


def _tools(monkeypatch, available: set[str]) -> None:
    monkeypatch.setattr(
        desktop.shutil, "which", lambda name: f"/usr/bin/{name}" if name in available else None
    )


# --- session detection -------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"XDG_SESSION_TYPE": "wayland"}, "wayland"),
        ({"XDG_SESSION_TYPE": "x11"}, "x11"),
        ({"XDG_SESSION_TYPE": "X11"}, "x11"),
        # A display manager that sets nothing: fall back to the live sockets.
        ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
        ({"DISPLAY": ":0"}, "x11"),
        # Wayland wins when both are advertised (XWayland also sets DISPLAY).
        ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, "wayland"),
        ({"XDG_SESSION_TYPE": "tty"}, "none"),
        ({}, "none"),
    ],
)
def test_session_type_reads_the_environment(environ, expected) -> None:
    assert desktop.session_type(environ) == expected


def test_headless_session_warns_rather_than_failing(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    result = desktop.check_session("none")
    # An SSH session is a legitimate way to run the headless API; that is not
    # a broken install, so it must not be a failure.
    assert result.status == WARN
    assert "graphical session" in result.detail


def test_named_session_passes_and_reports_the_desktop(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    result = desktop.check_session("wayland")
    assert result.status == PASS
    assert result.data["desktop"] == "GNOME"


# --- PortAudio ---------------------------------------------------------------


def test_missing_portaudio_is_a_failure_with_install_commands(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "_library_dirs", list)
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)
    result = desktop.check_portaudio()
    # No PortAudio means no microphone at all: dictation cannot work.
    assert result.status == FAIL
    assert "libportaudio2" in result.detail
    assert "apt install libportaudio2" in result.remediation


def test_portaudio_found_by_find_library_passes(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "_library_dirs", list)
    monkeypatch.setattr("ctypes.util.find_library", lambda name: "libportaudio.so.2")
    result = desktop.check_portaudio()
    assert result.status == PASS


def test_portaudio_found_on_disk_when_find_library_is_blind(tmp_path, monkeypatch) -> None:
    """`find_library` needs `ldconfig`/`gcc`; a slim container has neither."""
    (tmp_path / "libportaudio.so.2").write_bytes(b"")
    monkeypatch.setattr(desktop, "_library_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)
    result = desktop.check_portaudio()
    assert result.status == PASS
    assert result.data["candidates"] == ["libportaudio.so.2"]


# --- injection helpers -------------------------------------------------------


def test_x11_session_with_xclip_and_xdotool_passes(monkeypatch) -> None:
    _tools(monkeypatch, {"xclip", "xdotool"})
    result = desktop.check_injection_tools("x11")
    assert result.status == PASS
    assert result.data["x11Ready"] is True


def test_x11_session_missing_xdotool_fails(monkeypatch) -> None:
    _tools(monkeypatch, {"xclip"})
    result = desktop.check_injection_tools("x11")
    # Without a keystroke sender there is nowhere for transcribed text to go.
    assert result.status == FAIL
    assert "xclip xdotool" in result.remediation
    assert "xdotool" in result.data["missing"]


def test_wayland_session_needs_wayland_tools_not_x11_ones(monkeypatch) -> None:
    """A Wayland desktop with only the X11 helpers installed is still broken."""
    _tools(monkeypatch, {"xclip", "xdotool"})
    result = desktop.check_injection_tools("wayland")
    assert result.status == FAIL
    assert "Wayland" in result.detail
    assert "wl-clipboard" in result.remediation
    assert result.data["x11Ready"] is True
    assert result.data["waylandReady"] is False


@pytest.mark.parametrize("keystroke", ["wtype", "ydotool"])
def test_wayland_accepts_either_keystroke_sender(monkeypatch, keystroke) -> None:
    _tools(monkeypatch, {"wl-copy", keystroke})
    assert desktop.check_injection_tools("wayland").status == PASS


def test_no_session_warns_rather_than_failing_twice(monkeypatch) -> None:
    """Headless (CI, SSH): desktop.session already reports it — don't double-fail."""
    _tools(monkeypatch, set())
    result = desktop.check_injection_tools("none")
    assert result.status == WARN
    assert "desktop.session" in result.detail


def test_no_session_still_lists_the_helpers_that_are_installed(monkeypatch) -> None:
    _tools(monkeypatch, {"xclip", "xdotool"})
    result = desktop.check_injection_tools("none")
    assert result.status == WARN
    assert "xclip" in result.detail and "xdotool" in result.detail


# --- uinput / input group ----------------------------------------------------


def test_uinput_is_not_required_on_x11(monkeypatch) -> None:
    monkeypatch.setattr(desktop.os.path, "exists", lambda path: False)
    result = desktop.check_uinput("x11")
    # X11 delivers global hotkeys through the X server; /dev/uinput is irrelevant.
    assert result.status == PASS
    assert "not required" in result.detail


def test_wayland_without_writable_uinput_warns_with_the_group_fix(monkeypatch) -> None:
    monkeypatch.setattr(desktop.os.path, "exists", lambda path: True)
    monkeypatch.setattr(desktop.os, "access", lambda path, mode: False)
    monkeypatch.setattr(desktop, "_group_names", lambda: ["users"])
    result = desktop.check_uinput("wayland")
    # A warning, not a failure: the app still runs, the hotkey may not fire.
    assert result.status == WARN
    assert "usermod -aG input" in result.remediation
    assert result.data["inInputGroup"] is False


def test_wayland_with_writable_uinput_passes(monkeypatch) -> None:
    monkeypatch.setattr(desktop.os.path, "exists", lambda path: True)
    monkeypatch.setattr(desktop.os, "access", lambda path, mode: True)
    monkeypatch.setattr(desktop, "_group_names", lambda: ["input"])
    result = desktop.check_uinput("wayland")
    assert result.status == PASS
    assert result.data["inInputGroup"] is True


def test_missing_uinput_device_names_the_device(monkeypatch) -> None:
    monkeypatch.setattr(desktop.os.path, "exists", lambda path: False)
    monkeypatch.setattr(desktop, "_group_names", list)
    result = desktop.check_uinput("wayland")
    assert result.status == WARN
    assert "/dev/uinput does not exist" in result.detail
    assert "modprobe uinput" in result.remediation


# --- WebKitGTK ---------------------------------------------------------------


def _fake_probe(monkeypatch, payload) -> None:
    monkeypatch.setattr("dcent_voice.doctor.probe.probe", lambda name, **kw: payload)


def test_webkitgtk_present_passes_and_reports_the_abi(monkeypatch) -> None:
    _fake_probe(monkeypatch, {"ok": True, "webkit": "4.1", "soup": "3.0"})
    result = desktop.check_webkitgtk()
    assert result.status == PASS
    assert "WebKit2 4.1" in result.detail


def test_jammy_webkit_40_is_also_accepted(monkeypatch) -> None:
    _fake_probe(monkeypatch, {"ok": True, "webkit": "4.0", "soup": "2.4"})
    assert desktop.check_webkitgtk().status == PASS


def test_webkitgtk_absent_warns_like_a_missing_webview2(monkeypatch) -> None:
    """No WebKitGTK is Linux's "Settings will not open" — dictation still works."""
    _fake_probe(
        monkeypatch,
        {"ok": True, "webkitError": "Namespace WebKit2 not available"},
    )
    result = desktop.check_webkitgtk()
    assert result.status == WARN
    assert "Hold-to-talk dictation is unaffected" in result.detail
    assert "gir1.2-webkit2-4.1" in result.remediation


def test_webkitgtk_when_pygobject_itself_is_missing(monkeypatch) -> None:
    _fake_probe(monkeypatch, {"ok": False, "detail": "ModuleNotFoundError: No module named 'gi'"})
    result = desktop.check_webkitgtk()
    assert result.status == WARN
    assert "python3-gi" in result.remediation


def test_webkitgtk_skipped_off_linux_passes(monkeypatch) -> None:
    _fake_probe(monkeypatch, {"ok": True, "skipped": True, "detail": "not applicable"})
    assert desktop.check_webkitgtk().status == PASS


# --- macOS TCC ---------------------------------------------------------------


def test_accessibility_denied_is_a_failure_naming_the_pane(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "accessibility_trusted", lambda: False)
    result = desktop.check_macos_accessibility()
    # Without Accessibility neither the hotkey nor the injector works at all.
    assert result.status == FAIL
    assert "Privacy_Accessibility" in result.remediation
    assert "System Settings" in result.remediation


def test_accessibility_granted_passes(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "accessibility_trusted", lambda: True)
    assert desktop.check_macos_accessibility().status == PASS


def test_accessibility_unreadable_warns_rather_than_guessing(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "accessibility_trusted", lambda: None)
    result = desktop.check_macos_accessibility()
    assert result.status == WARN
    assert "unknown" in result.detail


@pytest.mark.parametrize("status", ["denied", "restricted"])
def test_microphone_denied_is_a_failure(monkeypatch, status) -> None:
    monkeypatch.setattr(desktop, "microphone_authorization", lambda: status)
    result = desktop.check_macos_microphone()
    assert result.status == FAIL
    assert "Privacy_Microphone" in result.remediation


def test_microphone_not_determined_warns_and_explains_the_prompt(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "microphone_authorization", lambda: "notDetermined")
    result = desktop.check_macos_microphone()
    # macOS has simply not asked yet; that is normal on a first launch.
    assert result.status == WARN
    assert "prompt" in result.detail


def test_microphone_authorized_passes(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "microphone_authorization", lambda: "authorized")
    assert desktop.check_macos_microphone().status == PASS


def test_accessibility_trusted_never_raises_without_the_framework(monkeypatch) -> None:
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)
    assert desktop.accessibility_trusted() is None


def test_microphone_authorization_never_raises_without_pyobjc() -> None:
    # AVFoundation is absent on this host; the answer is "unknown", not a crash.
    assert desktop.microphone_authorization() in {None, "authorized", "denied", "notDetermined"}


def test_macos_never_requires_homebrew() -> None:
    result = desktop.check_macos_dependencies()
    assert result.status == PASS
    assert "no Homebrew" in result.detail


# --- dispatch ----------------------------------------------------------------


def test_run_on_linux_returns_the_linux_checks(monkeypatch) -> None:
    _linux(monkeypatch)
    _tools(monkeypatch, {"xclip", "xdotool"})
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    _fake_probe(monkeypatch, {"ok": True, "webkit": "4.1", "soup": "3.0"})
    monkeypatch.setattr("ctypes.util.find_library", lambda name: "libportaudio.so.2")
    ids = [result.id for result in desktop.run(timeout_s=1.0)]
    assert ids == [
        "desktop.session",
        "desktop.portaudio",
        "desktop.injection_tools",
        "desktop.uinput",
        "desktop.webkitgtk",
    ]


def test_run_on_macos_returns_the_tcc_checks(monkeypatch) -> None:
    _macos(monkeypatch)
    monkeypatch.setattr(desktop, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(desktop, "microphone_authorization", lambda: "authorized")
    ids = [result.id for result in desktop.run(timeout_s=1.0)]
    assert ids == ["desktop.accessibility", "desktop.microphone", "desktop.dependencies"]


def test_run_on_windows_is_a_single_pass(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    results = desktop.run(timeout_s=1.0)
    assert [result.status for result in results] == [PASS]
    assert "not applicable" in results[0].detail


def test_every_non_pass_result_carries_a_remediation(monkeypatch) -> None:
    """A finding with no fix is a finding the user cannot act on."""
    _linux(monkeypatch)
    _tools(monkeypatch, set())
    monkeypatch.setattr(desktop, "_library_dirs", list)
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)
    monkeypatch.setattr(desktop.os.path, "exists", lambda path: False)
    monkeypatch.setattr(desktop, "_group_names", list)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    _fake_probe(monkeypatch, {"ok": False, "detail": "no gi"})
    for result in desktop.run(timeout_s=1.0):
        if result.status != PASS:
            assert result.remediation.strip(), result.id


def test_check_ids_are_documented_in_troubleshooting() -> None:
    """Every id doctor can emit must be explained in the user-facing doc."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "TROUBLESHOOTING.md").read_text(
        encoding="utf-8"
    )
    for check_id in (
        "desktop.session",
        "desktop.portaudio",
        "desktop.injection_tools",
        "desktop.uinput",
        "desktop.webkitgtk",
        "desktop.accessibility",
        "desktop.microphone",
        "desktop.dependencies",
    ):
        assert f"`{check_id}`" in doc, check_id


# --- login-item parity -------------------------------------------------------


def test_doctor_reads_a_launch_agent_pointing_at_a_missing_app(tmp_path, monkeypatch) -> None:
    from dcent_voice.doctor.checks import instance

    plist = tmp_path / "tech.d-central.dcent-voice.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "tech.d-central.dcent-voice",
                "ProgramArguments": ["/Applications/Old Location.app/Contents/MacOS/dcent-voice"],
                "RunAtLoad": True,
            }
        )
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("dcent_voice.autostart.login_item_path", lambda: plist)

    result = instance.check_autostart()
    # A moved .app leaves a login item that silently does nothing every morning,
    # but the app rewrites its login item on every launch, so this self-heals:
    # WARN, not FAIL (a FAIL here would abort the installer's post-install
    # self-check for a condition the skipped first launch would have fixed).
    assert result.status == WARN
    assert "Old Location.app" in result.detail
    assert "rewrites the login item on every start" in result.remediation


def test_doctor_reads_a_desktop_entry_pointing_at_the_running_executable(
    tmp_path, monkeypatch
) -> None:
    from dcent_voice.doctor.checks import instance

    entry = tmp_path / "dcent-voice.desktop"
    entry.write_text(
        f'[Desktop Entry]\nType=Application\nExec="{sys.executable}" -m dcent_voice\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("dcent_voice.autostart.login_item_path", lambda: entry)

    result = instance.check_autostart()
    assert result.status == PASS
    assert result.data["target"] == sys.executable


def test_doctor_passes_when_no_login_item_is_registered(tmp_path, monkeypatch) -> None:
    from dcent_voice.doctor.checks import instance

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "dcent_voice.autostart.login_item_path", lambda: tmp_path / "absent.desktop"
    )
    result = instance.check_autostart()
    # "Start at login is off" is a normal state, not a problem.
    assert result.status == PASS
    assert "off" in result.detail


def test_doctor_warns_on_an_unparseable_login_item(tmp_path, monkeypatch) -> None:
    from dcent_voice.doctor.checks import instance

    entry = tmp_path / "dcent-voice.desktop"
    entry.write_text("[Desktop Entry]\nType=Application\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("dcent_voice.autostart.login_item_path", lambda: entry)

    result = instance.check_autostart()
    assert result.status == WARN
    assert "Exec=" in result.data["parseError"]
