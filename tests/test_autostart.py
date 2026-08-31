# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import plistlib

from dcent_voice import autostart


def test_launch_command_is_nonempty_and_quoted() -> None:
    command = autostart.launch_command()
    assert isinstance(command, str)
    assert command.strip()
    # The executable path is always quoted so a "Program Files" path survives.
    assert command.startswith('"')


def test_launch_command_targets_the_module_in_dev() -> None:
    # In a non-frozen (editable) run it should launch the package, not a bare exe.
    command = autostart.launch_command()
    assert "-m dcent_voice" in command or command.endswith('"')


def test_split_strips_quotes_but_keeps_backslashes() -> None:
    # launchd ProgramArguments (macOS plist) need bare paths: shlex with
    # posix=False keeps Windows backslashes but also the literal quotes, which
    # must be stripped or launchd would exec a path starting with `"`.
    tokens = autostart._split('"C:\\Program Files\\Py\\pythonw.exe" -m dcent_voice')
    assert tokens == ["C:\\Program Files\\Py\\pythonw.exe", "-m", "dcent_voice"]


def test_split_handles_the_real_launch_command() -> None:
    tokens = autostart._split(autostart.launch_command())
    assert tokens
    assert not tokens[0].startswith('"')
    assert not tokens[0].endswith('"')


def test_macos_launch_agent_is_valid_plist_with_special_path(tmp_path, monkeypatch) -> None:
    path = tmp_path / "Launch Agents & Tests" / "dcent-voice.plist"
    executable = "/Applications/DCENT & Voice <Preview>/python"
    monkeypatch.setattr(autostart, "_macos_agent_path", lambda: path)
    monkeypatch.setattr(
        autostart,
        "launch_command",
        lambda: f'"{executable}" -m dcent_voice',
    )

    assert autostart._set_macos(True) is True
    payload = plistlib.loads(path.read_bytes())

    assert payload == {
        "Label": "tech.d-central.dcent-voice",
        "ProgramArguments": [executable, "-m", "dcent_voice"],
        "RunAtLoad": True,
    }
    assert autostart._set_macos(False) is True
    assert not path.exists()


def test_linux_autostart_preserves_quoted_special_path(tmp_path, monkeypatch) -> None:
    config_home = tmp_path / "Config & Data"
    executable = "/opt/DCENT Voice & Tools/python"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(
        autostart,
        "launch_command",
        lambda: f'"{executable}" -m dcent_voice',
    )

    assert autostart._set_linux(True) is True
    entry = config_home / "autostart" / "dcent-voice.desktop"
    contents = entry.read_text(encoding="utf-8")
    assert f'Exec="{executable}" -m dcent_voice\n' in contents
    assert contents.startswith("[Desktop Entry]\n")

    assert autostart._set_linux(False) is True
    assert not entry.exists()


# --- parity: rewritten every launch, removed on disable (WS9) -----------------


def test_set_enabled_routes_to_the_platform_writer(monkeypatch) -> None:
    """One entry point, three mechanisms — and no silent no-op on Linux/macOS."""
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(autostart, "_set_windows", lambda on: seen.append(("win", on)) or True)
    monkeypatch.setattr(autostart, "_set_macos", lambda on: seen.append(("mac", on)) or True)
    monkeypatch.setattr(autostart, "_set_linux", lambda on: seen.append(("linux", on)) or True)

    for system, tag in (("Windows", "win"), ("Darwin", "mac"), ("Linux", "linux")):
        monkeypatch.setattr(autostart.platform, "system", lambda system=system: system)
        assert autostart.set_enabled(True) is True
        assert seen[-1] == (tag, True)

    monkeypatch.setattr(autostart.platform, "system", lambda: "Haiku")
    assert autostart.set_enabled(True) is False


def test_login_item_path_is_the_file_the_writers_use(monkeypatch) -> None:
    monkeypatch.setattr(autostart.platform, "system", lambda: "Darwin")
    assert autostart.login_item_path() == autostart._macos_agent_path()
    monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
    assert autostart.login_item_path() == autostart._linux_desktop_path()
    # Windows registers a registry value, not a file.
    monkeypatch.setattr(autostart.platform, "system", lambda: "Windows")
    assert autostart.login_item_path() is None


def test_macos_agent_is_rewritten_to_the_current_bundle(tmp_path, monkeypatch) -> None:
    """A .app dragged to /Applications must repair its own login item."""
    path = tmp_path / "LaunchAgents" / "dcent-voice.plist"
    monkeypatch.setattr(autostart, "_macos_agent_path", lambda: path)
    old = "/Volumes/DCENT Voice/DCENT Voice.app/Contents/MacOS/dcent-voice"
    monkeypatch.setattr(autostart, "launch_command", lambda: f'"{old}"')
    assert autostart._set_macos(True) is True
    assert plistlib.loads(path.read_bytes())["ProgramArguments"] == [old]

    new = "/Applications/DCENT Voice.app/Contents/MacOS/dcent-voice"
    monkeypatch.setattr(autostart, "launch_command", lambda: f'"{new}"')
    assert autostart._set_macos(True) is True
    payload = plistlib.loads(path.read_bytes())
    # Rewritten in full: no trace of the old mount point survives.
    assert payload["ProgramArguments"] == [new]
    assert payload["RunAtLoad"] is True


def test_linux_entry_is_rewritten_to_the_current_appimage(tmp_path, monkeypatch) -> None:
    """An AppImage upgraded to a new filename must not leave a dead entry."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    old = "/home/u/Apps/DCENT_Voice-linux-x86_64-0.2.0.AppImage"
    monkeypatch.setattr(autostart, "launch_command", lambda: f'"{old}"')
    assert autostart._set_linux(True) is True

    new = "/home/u/Apps/DCENT_Voice-linux-x86_64-0.3.0.AppImage"
    monkeypatch.setattr(autostart, "launch_command", lambda: f'"{new}"')
    assert autostart._set_linux(True) is True

    contents = (tmp_path / "autostart" / "dcent-voice.desktop").read_text(encoding="utf-8")
    assert f'Exec="{new}"' in contents
    assert "0.2.0" not in contents


def test_linux_entry_is_a_valid_desktop_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert autostart._set_linux(True) is True
    lines = (tmp_path / "autostart" / "dcent-voice.desktop").read_text(encoding="utf-8")
    assert lines.startswith("[Desktop Entry]\n")
    for required in ("Type=Application", "Name=DCENT_Voice", "Terminal=false"):
        assert f"{required}\n" in lines
    # GNOME reads this key to decide whether to honour the entry at all.
    assert "X-GNOME-Autostart-enabled=true" in lines


def test_disabling_twice_is_not_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "_macos_agent_path", lambda: tmp_path / "a.plist")
    assert autostart._set_linux(False) is True
    assert autostart._set_linux(False) is True
    assert autostart._set_macos(False) is True
    assert autostart._set_macos(False) is True


def test_an_unwritable_home_degrades_instead_of_raising(tmp_path, monkeypatch) -> None:
    """set_enabled runs on every launch; it must never take a launch down."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocked))
    monkeypatch.setattr(autostart, "_macos_agent_path", lambda: blocked / "x" / "a.plist")

    assert autostart._set_linux(True) is False
    assert autostart._set_macos(True) is False
