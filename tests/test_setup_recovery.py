# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# ruff: noqa: E501 -- embedded C# and cmd probes preserve production command lines
from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP_PROJECT = ROOT / "packaging/windows/setup-stub/DCENT_Voice.Setup.csproj"
RECOVERY_SOURCE = ROOT / "packaging/windows/setup-stub/RecoveryCoordinator.cs"
UNINSTALL_SOURCE = ROOT / "packaging/windows/setup-stub/uninstall.ps1"
DESTINATION_SOURCE = ROOT / "packaging/windows/setup-stub/InstallDestination.cs"
MODEL_MIGRATION_SOURCE = ROOT / "packaging/windows/setup-stub/ModelMigration.cs"
INSTALL_REGISTRATION_SOURCE = ROOT / "packaging/windows/setup-stub/InstallRegistration.cs"


def _dotnet() -> Path:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "dotnet/dotnet.exe",
        Path(shutil.which("dotnet.exe") or ""),
        Path(shutil.which("dotnet") or ""),
    ]
    tool = next((candidate for candidate in candidates if candidate.is_file()), None)
    if tool is None:
        pytest.skip(".NET 8 SDK is not installed")
    return tool


def _fixture(root: Path, marker: bytes) -> None:
    (root / "_internal").mkdir(parents=True)
    (root / "_internal/base_library.zip").write_bytes(b"payload")
    (root / "dcent-voice-offline-bundle.json").write_text("{}", encoding="utf-8")
    (root / "dcent-voice.exe").write_bytes(marker)


def _delete_registry_tree(relative: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            relative,
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            children: list[str] = []
            index = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, index))
                    index += 1
                except OSError:
                    break
        for child in children:
            _delete_registry_tree(relative + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, relative)
    except FileNotFoundError:
        pass


def _build_recovery_probe(tmp_path: Path, dotnet: Path) -> Path:
    probe = tmp_path / "recovery-probe"
    probe.mkdir()
    linked_source = html.escape(str(RECOVERY_SOURCE), quote=True)
    (probe / "RecoveryProbe.csproj").write_text(
        f"""<Project Sdk=\"Microsoft.NET.Sdk\">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0-windows</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include=\"Program.cs\" />
    <Compile Include=\"{linked_source}\" Link=\"RecoveryCoordinator.cs\" />
  </ItemGroup>
</Project>
""",
        encoding="utf-8",
    )
    (probe / "Program.cs").write_text(
        r"""using System;
using Microsoft.Win32;

internal static class Program
{
    private static int Main(string[] args)
    {
        try
        {
            switch (args[0])
            {
                case "reconcile":
                    RecoveryCoordinator.ReconcilePendingUninstall(args[1], args[2]);
                    return 0;
                case "clear":
                    using (var key = Registry.CurrentUser.CreateSubKey(args[1]))
                    {
                        if (key is null) throw new InvalidOperationException("registry key unavailable");
                        RecoveryCoordinator.ClearRecoveryValues(key);
                    }
                    return 0;
                case "uninstall":
                    return RecoveryCoordinator.RunUninstaller(args[1], true, false, args[2]);
                default:
                    throw new ArgumentException("unknown probe operation");
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.Message);
            return 1;
        }
    }
}
""",
        encoding="utf-8",
    )
    built = subprocess.run(
        [str(dotnet), "build", str(probe / "RecoveryProbe.csproj"), "--nologo"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    assembly = probe / "bin/Debug/net8.0-windows/RecoveryProbe.dll"
    assert assembly.is_file()
    return assembly


def _build_setup_safety_probe(tmp_path: Path, dotnet: Path) -> tuple[Path, Path]:
    probe = tmp_path / "setup-safety-probe"
    probe.mkdir()
    destination_source = html.escape(str(DESTINATION_SOURCE), quote=True)
    migration_source = html.escape(str(MODEL_MIGRATION_SOURCE), quote=True)
    registration_source = html.escape(str(INSTALL_REGISTRATION_SOURCE), quote=True)
    recovery_source = html.escape(str(RECOVERY_SOURCE), quote=True)
    (probe / "SafetyProbe.csproj").write_text(
        f"""<Project Sdk=\"Microsoft.NET.Sdk\">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0-windows</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <AssemblyName>DCENT-SafetyProbe</AssemblyName>
    <Product>DCENT_Voice</Product>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include=\"Program.cs\" />
    <Compile Include=\"{destination_source}\" Link=\"InstallDestination.cs\" />
    <Compile Include=\"{migration_source}\" Link=\"ModelMigration.cs\" />
    <Compile Include=\"{registration_source}\" Link=\"InstallRegistration.cs\" />
    <Compile Include=\"{recovery_source}\" Link=\"RecoveryCoordinator.cs\" />
  </ItemGroup>
</Project>
""",
        encoding="utf-8",
    )
    (probe / "Program.cs").write_text(
        r"""using System;
using System.IO;

internal static class Program
{
    private static int Main(string[] args)
    {
        try
        {
            if (args[0] == "validate")
            {
                Console.WriteLine(InstallDestination.ValidateForInstall(args[1]));
                return 0;
            }
            if (args[0] == "validate-special")
            {
                var path = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
                if (String.IsNullOrWhiteSpace(path))
                    path = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                Console.WriteLine(InstallDestination.ValidateForInstall(path));
                return 0;
            }
            if (args[0] == "migrate")
            {
                Console.WriteLine(ModelMigration.MigrateLegacyModels(args[1], args[2]));
                return 0;
            }
            if (args[0] == "legacy-layout")
            {
                Console.WriteLine(InstallDestination.IsLegacyModelsOnlyRoot(args[1], args[2]));
                return 0;
            }
            if (args[0] == "migrate-retire")
            {
                Console.WriteLine(ModelMigration.MigrateAndRetireLegacyRoot(args[1], args[2]));
                return 0;
            }
            if (args[0] == "migrate-interfere")
            {
                Console.WriteLine(ModelMigration.MigrateLegacyModels(
                    args[1], args[2], destination => {
                        Directory.CreateDirectory(destination);
                        File.WriteAllText(Path.Combine(destination, "racer.txt"), "unexpected");
                    }));
                return 0;
            }
            if (args[0] == "registration-rollback")
            {
                var snapshot = InstallRegistrationSnapshot.Capture(args[1], args[2]);
                Directory.CreateDirectory(args[1]);
                File.WriteAllText(Path.Combine(args[1], "DCENT_Voice.lnk"), "new shortcut");
                using (var key = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(args[2]))
                {
                    if (key is null) throw new InvalidOperationException("test registry unavailable");
                    key.SetValue("DisplayName", "new registration");
                }
                snapshot.Restore();
                return 0;
            }
            throw new ArgumentException("unknown probe operation");
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.Message);
            return 1;
        }
    }
}
""",
        encoding="utf-8",
    )
    built = subprocess.run(
        [str(dotnet), "build", str(probe / "SafetyProbe.csproj"), "--nologo"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    assembly = probe / "bin/Debug/net8.0-windows/DCENT-SafetyProbe.dll"
    executable = probe / "bin/Debug/net8.0-windows/DCENT-SafetyProbe.exe"
    assert assembly.is_file() and executable.is_file()
    return assembly, executable


def _write_uninstall_launcher(
    root: Path,
    programs: Path,
    registry_path: str,
    run_registry_path: str,
    user_data: Path,
    model_data: Path,
    modules: Path,
    credential_service: str,
) -> None:
    shutil.copyfile(UNINSTALL_SOURCE, root / "Uninstall.ps1")
    body = rf"""@echo off
setlocal EnableDelayedExpansion
if /i "%~1"=="__go" goto cleanup
for /f %%G in ('powershell.exe -NoProfile -Command "[guid]::NewGuid().ToString('N')"') do set "RUNID=%%G"
if not defined RUNID exit /b 60
set "RUNNER=%TEMP%\DCENT_Voice-Uninstall-!RUNID!.cmd"
set "HELPER=%TEMP%\DCENT_Voice-Uninstall-!RUNID!.ps1"
copy /y "%~f0" "!RUNNER!" >nul
if errorlevel 1 exit /b 60
copy /y "%~dp0Uninstall.ps1" "!HELPER!" >nul
if errorlevel 1 exit /b 62
"!RUNNER!" __go "%~dp0." "!HELPER!"
exit /b 61
:cleanup
powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~3" -InstallRoot "%~2" -ProgramsRoot "{programs}" -RegistryPath "{registry_path}" -RunRegistryPath "{run_registry_path}" -UserDataRoot "{user_data}" -ModelDataRoot "{model_data}" -AdeModulesRoot "{modules}" -CredentialService "{credential_service}" -GraceTimeoutMs 200 -TerminateTimeoutMs 3000
set "RC=!ERRORLEVEL!"
del /q "%~3" >nul 2>&1
exit /b !RC!
"""
    (root / "Uninstall.cmd").write_text(body, encoding="utf-8")


def test_setup_recovery_reconciles_before_replacement_and_clears_after_registration() -> None:
    source = (ROOT / "packaging/windows/setup-stub/Program.cs").read_text(encoding="utf-8")
    validate = source.index("ValidatePayload(stage)")
    reconcile = source.index("RecoveryCoordinator.ReconcilePendingUninstall(dest)", validate)
    replace = source.index("Directory.Move(stage, dest)", reconcile)
    normal_registration = source.index('key.SetValue("EstimatedSize"')
    clear_recovery = source.index("RecoveryCoordinator.ClearRecoveryValues(key)")

    assert validate < reconcile < replace
    assert normal_registration < clear_recovery


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows setup recovery")
def test_reinstall_reconciles_stale_recovery_then_outer_uninstall_keeps_data(
    tmp_path: Path,
) -> None:
    import winreg

    dotnet = _dotnet()
    probe = _build_recovery_probe(tmp_path, dotnet)
    root = tmp_path / "DCENT Voice install"
    programs = tmp_path / "Start Menu/DCENT_Voice"
    user_data = tmp_path / "roaming/DCENT_Voice"
    model_data = tmp_path / "local/DCENT_Voice.Models"
    modules = tmp_path / "local/DCENT/modules"
    user_data.mkdir(parents=True)
    roaming_sentinel = user_data / "personalization.json"
    roaming_sentinel.write_text("keep across reinstall", encoding="utf-8")
    programs.mkdir(parents=True)
    (programs / "DCENT_Voice.lnk").write_bytes(b"shortcut")
    _fixture(root, b"old install")

    token = uuid.uuid4().hex
    relative = rf"Software\DCENT_Voice\Tests\SetupRecovery\{token}"
    registry_path = rf"HKCU:\{relative}"
    run_registry_path = rf"HKCU:\Software\DCENT_Voice\Tests\Run\{token}"
    credential_service = "DCENT_Voice-Test-" + token
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, relative) as key:
        winreg.SetValueEx(key, "Sentinel", 0, winreg.REG_SZ, "registered")

    try:
        stopped = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(UNINSTALL_SOURCE),
                "-InstallRoot",
                str(root),
                "-ProgramsRoot",
                str(programs),
                "-RegistryPath",
                registry_path,
                "-RunRegistryPath",
                run_registry_path,
                "-UserDataRoot",
                str(user_data),
                "-ModelDataRoot",
                str(model_data),
                "-AdeModulesRoot",
                str(modules),
                "-CredentialService",
                credential_service,
                "-GraceTimeoutMs",
                "200",
                "-TerminateTimeoutMs",
                "3000",
                "-TestStopAfter",
                "registered",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert stopped.returncode == 70, stopped.stderr
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative) as key:
            stale_command = str(winreg.QueryValueEx(key, "DCENTRecoveryUninstaller")[0])
            stale_state = str(winreg.QueryValueEx(key, "DCENTRecoveryState")[0])
        state = json.loads(Path(stale_state).read_text(encoding="utf-8"))
        recovery_root = Path(state["RecoveryRoot"])
        assert root.exists() and recovery_root.exists()

        original_state = Path(stale_state).read_text(encoding="utf-8")
        state["InstallRoot"] = str(tmp_path / "unrelated replacement")
        Path(stale_state).write_text(json.dumps(state), encoding="utf-8")
        refused = subprocess.run(
            [str(dotnet), str(probe), "reconcile", str(root), relative],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert refused.returncode == 1
        assert "install root binding is invalid" in refused.stderr
        assert root.exists() and recovery_root.exists()
        Path(stale_state).write_text(original_state, encoding="utf-8")

        reconciled = subprocess.run(
            [str(dotnet), str(probe), "reconcile", str(root), relative],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert reconciled.returncode == 0, reconciled.stderr
        assert not root.exists()
        assert not recovery_root.exists()
        assert roaming_sentinel.read_text(encoding="utf-8") == "keep across reinstall"

        # Model the successful replacement and registration portion of Setup.
        # WriteUninstall clears recovery values only after the new payload and
        # normal uninstall fields exist.
        _fixture(root, b"new install")
        programs.mkdir(parents=True)
        (programs / "DCENT_Voice.lnk").write_bytes(b"new shortcut")
        _write_uninstall_launcher(
            root,
            programs,
            registry_path,
            run_registry_path,
            user_data,
            model_data,
            modules,
            credential_service,
        )
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, relative) as key:
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, str(root / "Uninstall.cmd"))
            winreg.SetValueEx(key, "DCENTRecoveryUninstaller", 0, winreg.REG_SZ, stale_command)
            winreg.SetValueEx(key, "DCENTRecoveryState", 0, winreg.REG_SZ, stale_state)
        cleared = subprocess.run(
            [str(dotnet), str(probe), "clear", relative],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert cleared.returncode == 0, cleared.stderr
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative) as key:
            value_names = {
                winreg.EnumValue(key, index)[0] for index in range(winreg.QueryInfoKey(key)[1])
            }
        assert "DCENTRecoveryUninstaller" not in value_names
        assert "DCENTRecoveryState" not in value_names

        outer_uninstall = subprocess.run(
            [str(dotnet), str(probe), "uninstall", str(root), relative],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert outer_uninstall.returncode == 0, outer_uninstall.stderr
        assert not root.exists()
        assert not programs.exists()
        assert roaming_sentinel.read_text(encoding="utf-8") == "keep across reinstall"
    finally:
        _delete_registry_tree(relative)
        _delete_registry_tree(rf"Software\DCENT_Voice\Tests\Run\{token}")
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows setup validation")
def test_payload_validation_forms_are_machine_safe_and_never_install(tmp_path: Path) -> None:
    dotnet = _dotnet()
    built = subprocess.run(
        [str(dotnet), "build", str(SETUP_PROJECT), "-c", "Release", "--nologo"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    setup = ROOT / (
        "packaging/windows/setup-stub/bin/Release/net8.0-windows/win-x64/DCENT_Voice-Setup.dll"
    )
    assert setup.is_file()
    broken = tmp_path / "broken payload"
    broken.mkdir()
    (broken / "dcent-voice.exe").write_bytes(b"MZ")

    for arguments in (
        [f"--validate-payload={broken}"],
        ["--validate-payload", str(broken)],
        ["--validate-payload"],
        ["--validate-payload="],
    ):
        result = subprocess.run(
            [str(dotnet), str(setup), *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 1, (arguments, result.stdout, result.stderr)
    assert not (tmp_path / "DCENT_Voice").exists()


def _write_registered_model(root: Path, model_id: str, marker: bytes) -> Path:
    relative = Path("faster-whisper") / model_id.replace("/", "--")
    snapshot = root / relative
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text('{"model_type":"whisper"}', encoding="utf-8")
    (snapshot / "model.bin").write_bytes(marker)
    (root / "dcent-voice-models.json").write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "provider": "faster-whisper",
                        "model_id": model_id,
                        "path": relative.as_posix(),
                        "installed_at": "2026-08-23T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return snapshot


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Setup safety")
def test_setup_destination_validation_refuses_broad_unowned_and_reparse_targets(
    tmp_path: Path,
) -> None:
    dotnet = _dotnet()
    probe, probe_exe = _build_setup_safety_probe(tmp_path, dotnet)

    safe = tmp_path / "safe custom install"
    accepted = subprocess.run(
        [str(dotnet), str(probe), "validate", str(safe)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr

    unrelated = tmp_path / "Documents copy"
    unrelated.mkdir()
    sentinel = unrelated / "taxes.txt"
    sentinel.write_text("must survive", encoding="utf-8")
    refused = subprocess.run(
        [str(dotnet), str(probe), "validate", str(unrelated)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert refused.returncode == 1
    assert "not an owned" in refused.stderr
    assert sentinel.read_text(encoding="utf-8") == "must survive"

    protected = subprocess.run(
        [str(dotnet), str(probe), "validate-special"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert protected.returncode == 1
    assert "Refusing" in protected.stderr

    junction_target = tmp_path / "junction target"
    junction_target.mkdir()
    junction = tmp_path / "junction install"
    junction_env = os.environ.copy()
    junction_env["DCENT_TEST_JUNCTION"] = str(junction)
    junction_env["DCENT_TEST_JUNCTION_TARGET"] = str(junction_target)
    linked = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ErrorActionPreference='Stop'; "
                "New-Item -ItemType Junction -Path $env:DCENT_TEST_JUNCTION "
                "-Target $env:DCENT_TEST_JUNCTION_TARGET | Out-Null"
            ),
        ],
        env=junction_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert linked.returncode == 0, linked.stderr
    assert junction.is_dir()
    assert os.lstat(junction).st_file_attributes & 0x400
    refused_link = subprocess.run(
        [str(dotnet), str(probe), "validate", str(junction)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert refused_link.returncode == 1
    assert "reparse point" in refused_link.stderr

    owned = tmp_path / "owned upgrade"
    (owned / "_internal").mkdir(parents=True)
    shutil.copy2(probe_exe, owned / "dcent-voice.exe")
    (owned / "Uninstall.cmd").write_text("@echo off", encoding="utf-8")
    (owned / "Uninstall.ps1").write_text("exit 0", encoding="utf-8")
    (owned / "_internal/base_library.zip").write_bytes(b"runtime")
    (owned / "dcent-voice-offline-bundle.json").write_text(
        '{"product":"DCENT_Voice"}', encoding="utf-8"
    )
    accepted_owned = subprocess.run(
        [str(dotnet), str(probe), "validate", str(owned)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert accepted_owned.returncode == 0, accepted_owned.stderr

    incomplete = tmp_path / "incomplete owned upgrade"
    incomplete.mkdir()
    shutil.copy2(probe_exe, incomplete / "dcent-voice.exe")
    (incomplete / "Uninstall.cmd").write_text("@echo off", encoding="utf-8")
    accepted_incomplete = subprocess.run(
        [str(dotnet), str(probe), "validate", str(incomplete)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert accepted_incomplete.returncode == 0, accepted_incomplete.stderr

    stripped_helper = tmp_path / "quarantined uninstall helper"
    stripped_helper.mkdir()
    shutil.copy2(probe_exe, stripped_helper / "dcent-voice.exe")
    (stripped_helper / "Uninstall.cmd").write_text("@echo off", encoding="utf-8")
    (stripped_helper / "Uninstall.ps1").write_bytes(b"")
    accepted_stripped = subprocess.run(
        [str(dotnet), str(probe), "validate", str(stripped_helper)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert accepted_stripped.returncode == 0, accepted_stripped.stderr


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Setup parser")
def test_malformed_destination_options_fail_before_default_install(tmp_path: Path) -> None:
    dotnet = _dotnet()
    built = subprocess.run(
        [str(dotnet), "build", str(SETUP_PROJECT), "-c", "Release", "--nologo"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    setup = ROOT / (
        "packaging/windows/setup-stub/bin/Release/net8.0-windows/win-x64/DCENT_Voice-Setup.dll"
    )
    for arguments in (
        ["/S", "/D"],
        ["/S", "--dest"],
        ["/S", "/D="],
        ["/S", "--dest="],
        ["/S", f"/D={tmp_path / 'one'}", f"--dest={tmp_path / 'two'}"],
        ["/S", f"/D={Path(os.environ['LOCALAPPDATA']) / 'DCENT_Voice'}"],
    ):
        result = subprocess.run(
            [str(dotnet), str(setup), *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 1, arguments
        assert "install directory" in result.stderr.lower(), (arguments, result.stderr)


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows model migration")
def test_custom_model_survives_update_and_normal_uninstall_but_purge_removes_it(
    tmp_path: Path,
) -> None:
    import winreg

    dotnet = _dotnet()
    probe, _probe_exe = _build_setup_safety_probe(tmp_path, dotnet)
    install = tmp_path / "installed DCENT_Voice"
    _fixture(install, b"old app")
    legacy_root = install / "models"
    model_id = "Acme/faster-whisper-sovereign"
    legacy_snapshot = _write_registered_model(legacy_root, model_id, b"custom weights")
    durable = tmp_path / "data" / "DCENT_Voice.Models"

    migrated = subprocess.run(
        [str(dotnet), str(probe), "migrate", str(legacy_root), str(durable)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert migrated.returncode == 0, migrated.stderr
    durable_snapshot = durable / "faster-whisper/Acme--faster-whisper-sovereign"
    assert (durable_snapshot / "model.bin").read_bytes() == b"custom weights"
    registry = json.loads((durable / "dcent-voice-models.json").read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(b"custom weights").hexdigest()
    assert registry["version"] == 2
    assert registry["models"][0]["files"]["model.bin"] == {
        "size": len(b"custom weights"),
        "sha256": expected_hash,
    }
    assert legacy_snapshot.is_dir()

    # Model the publish step of an update: the replaceable application tree is
    # swapped, while the separately migrated model remains available.
    shutil.rmtree(install)
    _fixture(install, b"new app")
    shipped = install / "models/parakeet-tdt-0.6b-v3/config.json"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("{}", encoding="utf-8")
    assert shipped.is_file()
    assert (durable_snapshot / "model.bin").read_bytes() == b"custom weights"

    programs = tmp_path / "Start Menu" / "DCENT_Voice"
    programs.mkdir(parents=True)
    user_data = tmp_path / "profile" / "DCENT_Voice"
    user_data.mkdir(parents=True)
    modules = tmp_path / "local" / "DCENT" / "modules"
    registry_relative = rf"Software\DCENT_Voice\Tests\ModelRetention\{uuid.uuid4().hex}"
    registry_path = rf"HKCU:\{registry_relative}"

    def register() -> None:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_relative) as key:
            winreg.SetValueEx(key, "Sentinel", 0, winreg.REG_SZ, "registered")

    def uninstall(*, purge: bool) -> subprocess.CompletedProcess[str]:
        arguments = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(UNINSTALL_SOURCE),
            "-InstallRoot",
            str(install),
            "-ProgramsRoot",
            str(programs),
            "-RegistryPath",
            registry_path,
            "-UserDataRoot",
            str(user_data),
            "-ModelDataRoot",
            str(durable),
            "-AdeModulesRoot",
            str(modules),
            "-GraceTimeoutMs",
            "200",
            "-TerminateTimeoutMs",
            "3000",
        ]
        if purge:
            arguments.append("-PurgeUserData")
        return subprocess.run(arguments, capture_output=True, text=True, timeout=30, check=False)

    try:
        register()
        normal = uninstall(purge=False)
        assert normal.returncode == 0, normal.stderr
        assert not install.exists()
        assert (durable_snapshot / "model.bin").read_bytes() == b"custom weights"

        _fixture(install, b"reinstalled app")
        programs.mkdir(parents=True)
        register()
        purged = uninstall(purge=True)
        assert purged.returncode == 0, purged.stderr
        assert not durable.exists()
    finally:
        _delete_registry_tree(registry_relative)


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows model merge")
def test_legacy_and_durable_models_merge_without_overwrite_or_silent_loss(
    tmp_path: Path,
) -> None:
    dotnet = _dotnet()
    probe, _probe_exe = _build_setup_safety_probe(tmp_path, dotnet)
    legacy = tmp_path / "legacy models"
    durable = tmp_path / "durable" / "DCENT_Voice.Models"
    _write_registered_model(legacy, "Acme/legacy-only", b"legacy")
    _write_registered_model(durable, "Acme/durable-only", b"durable")

    merged = subprocess.run(
        [str(dotnet), str(probe), "migrate", str(legacy), str(durable)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert merged.returncode == 0, merged.stderr
    assert (durable / "faster-whisper/Acme--legacy-only/model.bin").read_bytes() == b"legacy"
    assert (durable / "faster-whisper/Acme--durable-only/model.bin").read_bytes() == b"durable"
    records = json.loads((durable / "dcent-voice-models.json").read_text(encoding="utf-8"))
    assert {item["model_id"] for item in records["models"]} == {
        "Acme/legacy-only",
        "Acme/durable-only",
    }

    conflict_legacy = tmp_path / "conflict legacy"
    conflict_durable = tmp_path / "conflict durable" / "DCENT_Voice.Models"
    _write_registered_model(conflict_legacy, "Acme/conflict", b"old bytes")
    _write_registered_model(conflict_durable, "Acme/conflict", b"new bytes")
    conflict = subprocess.run(
        [str(dotnet), str(probe), "migrate", str(conflict_legacy), str(conflict_durable)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert conflict.returncode == 1
    assert "conflict" in conflict.stderr
    assert (
        conflict_legacy / "faster-whisper/Acme--conflict/model.bin"
    ).read_bytes() == b"old bytes"
    assert (
        conflict_durable / "faster-whisper/Acme--conflict/model.bin"
    ).read_bytes() == b"new bytes"

    empty_durable = tmp_path / "empty" / "DCENT_Voice.Models"
    empty_durable.mkdir(parents=True)
    refused_empty = subprocess.run(
        [str(dotnet), str(probe), "migrate", str(conflict_legacy), str(empty_durable)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert refused_empty.returncode == 1
    assert "no verified registry" in refused_empty.stderr
    assert (conflict_legacy / "faster-whisper/Acme--conflict/model.bin").is_file()

    race_legacy = tmp_path / "race legacy"
    race_durable = tmp_path / "race durable" / "DCENT_Voice.Models"
    _write_registered_model(race_legacy, "Acme/race-legacy", b"legacy safe")
    _write_registered_model(race_durable, "Acme/race-durable", b"durable safe")
    interfered = subprocess.run(
        [
            str(dotnet),
            str(probe),
            "migrate-interfere",
            str(race_legacy),
            str(race_durable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert interfered.returncode == 1
    assert "prior verified tree is retained" in interfered.stderr
    assert (race_durable / "racer.txt").read_text(encoding="utf-8") == "unexpected"
    backups = list(race_durable.parent.glob("DCENT_Voice.Models.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "faster-whisper/Acme--race-durable/model.bin").read_bytes() == (
        b"durable safe"
    )
    assert (race_legacy / "faster-whisper/Acme--race-legacy/model.bin").read_bytes() == (
        b"legacy safe"
    )


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="native legacy models-only migration")
def test_models_only_default_root_is_migrated_then_retired_but_lookalikes_are_refused(
    tmp_path: Path,
) -> None:
    dotnet = _dotnet()
    probe, _probe_exe = _build_setup_safety_probe(tmp_path, dotnet)

    legacy_app = tmp_path / "default local" / "DCENT_Voice"
    legacy_models = legacy_app / "models"
    snapshot = _write_registered_model(
        legacy_models, "Acme/legacy-default", b"sovereign legacy weights"
    )
    recognized = subprocess.run(
        [str(dotnet), str(probe), "legacy-layout", str(legacy_app), str(legacy_app)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert recognized.returncode == 0, recognized.stderr
    assert recognized.stdout.strip() == "True"

    durable = tmp_path / "durable" / "DCENT_Voice.Models"
    migrated = subprocess.run(
        [str(dotnet), str(probe), "migrate-retire", str(legacy_models), str(durable)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert migrated.returncode == 0, migrated.stderr
    assert not legacy_app.exists()
    assert not snapshot.exists()
    assert (
        durable / "faster-whisper/Acme--legacy-default/model.bin"
    ).read_bytes() == b"sovereign legacy weights"

    unrelated = tmp_path / "unrelated default" / "DCENT_Voice"
    unrelated_models = unrelated / "models"
    _write_registered_model(unrelated_models, "Acme/unrelated", b"model")
    sentinel = unrelated / "family-photo.jpg"
    sentinel.write_bytes(b"preserve")
    refused_unrelated = subprocess.run(
        [str(dotnet), str(probe), "legacy-layout", str(unrelated), str(unrelated)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert refused_unrelated.returncode == 0
    assert refused_unrelated.stdout.strip() == "False"
    assert sentinel.read_bytes() == b"preserve"

    poisoned = tmp_path / "poisoned default" / "DCENT_Voice"
    poisoned_models = poisoned / "models"
    poisoned_snapshot = _write_registered_model(
        poisoned_models, "Acme/poisoned", b"valid model bytes"
    )
    executable = poisoned_models / "payload.exe"
    executable.write_bytes(b"MZ arbitrary")
    superficially_recognized = subprocess.run(
        [str(dotnet), str(probe), "legacy-layout", str(poisoned), str(poisoned)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert superficially_recognized.returncode == 0
    assert superficially_recognized.stdout.strip() == "True"
    refused_poison = subprocess.run(
        [
            str(dotnet),
            str(probe),
            "migrate-retire",
            str(poisoned_models),
            str(tmp_path / "poison durable" / "DCENT_Voice.Models"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert refused_poison.returncode == 1
    assert "undeclared or unsafe" in refused_poison.stderr
    assert executable.read_bytes() == b"MZ arbitrary"
    assert (poisoned_snapshot / "model.bin").read_bytes() == b"valid model bytes"

    corrupt = tmp_path / "corrupt default" / "DCENT_Voice"
    corrupt_registry = corrupt / "models" / "dcent-voice-models.json"
    corrupt_registry.parent.mkdir(parents=True)
    corrupt_registry.write_text("not json", encoding="utf-8")
    refused_corrupt = subprocess.run(
        [
            str(dotnet),
            str(probe),
            "migrate-retire",
            str(corrupt_registry.parent),
            str(tmp_path / "corrupt durable" / "DCENT_Voice.Models"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert refused_corrupt.returncode == 1
    assert corrupt_registry.read_text(encoding="utf-8") == "not json"

    junction_target = tmp_path / "junction model target"
    _write_registered_model(junction_target, "Acme/junction", b"linked model")
    junction_app = tmp_path / "junction default" / "DCENT_Voice"
    junction_app.mkdir(parents=True)
    junction_models = junction_app / "models"
    junction_env = os.environ.copy()
    junction_env["DCENT_TEST_JUNCTION"] = str(junction_models)
    junction_env["DCENT_TEST_JUNCTION_TARGET"] = str(junction_target)
    linked = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ErrorActionPreference='Stop'; "
                "New-Item -ItemType Junction -Path $env:DCENT_TEST_JUNCTION "
                "-Target $env:DCENT_TEST_JUNCTION_TARGET | Out-Null"
            ),
        ],
        env=junction_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert linked.returncode == 0, linked.stderr
    assert os.lstat(junction_models).st_file_attributes & 0x400
    refused_junction = subprocess.run(
        [
            str(dotnet),
            str(probe),
            "legacy-layout",
            str(junction_app),
            str(junction_app),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert refused_junction.returncode == 0
    assert refused_junction.stdout.strip() == "False"


def test_setup_migrates_exact_legacy_models_only_root_before_destination_revalidation() -> None:
    source = (ROOT / "packaging/windows/setup-stub/Program.cs").read_text(encoding="utf-8")
    recognize = source.index("InstallDestination.IsLegacyModelsOnlyRoot(dest)")
    first_general_validation = source.index("InstallDestination.ValidateForInstall(dest)")
    retire = source.index("ModelMigration.MigrateAndRetireLegacyRoot")
    final_general_validation = source.index(
        "dest = InstallDestination.ValidateForInstall(dest)",
        retire,
    )

    assert recognize < first_general_validation < retire < final_general_validation


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows registration rollback")
def test_registration_snapshot_restores_upgrade_and_removes_fresh_partial_metadata(
    tmp_path: Path,
) -> None:
    import winreg

    dotnet = _dotnet()
    probe, _probe_exe = _build_setup_safety_probe(tmp_path, dotnet)
    programs = tmp_path / "Start Menu" / "DCENT_Voice"
    programs.mkdir(parents=True)
    shortcut = programs / "DCENT_Voice.lnk"
    shortcut.write_bytes(b"old shortcut bytes")
    relative = rf"Software\DCENT_Voice\Tests\RegistrationRollback\{uuid.uuid4().hex}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, relative) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "old registration")
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, 123)
    try:
        restored = subprocess.run(
            [str(dotnet), str(probe), "registration-rollback", str(programs), relative],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert restored.returncode == 0, restored.stderr
        assert shortcut.read_bytes() == b"old shortcut bytes"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative) as key:
            assert winreg.QueryValueEx(key, "DisplayName") == ("old registration", winreg.REG_SZ)
            assert winreg.QueryValueEx(key, "EstimatedSize") == (123, winreg.REG_DWORD)

        fresh_programs = tmp_path / "Fresh Start Menu" / "DCENT_Voice"
        fresh_relative = relative + "-fresh"
        fresh = subprocess.run(
            [
                str(dotnet),
                str(probe),
                "registration-rollback",
                str(fresh_programs),
                fresh_relative,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert fresh.returncode == 0, fresh.stderr
        assert not fresh_programs.exists()
        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, fresh_relative)
    finally:
        _delete_registry_tree(relative)
        _delete_registry_tree(relative + "-fresh")
