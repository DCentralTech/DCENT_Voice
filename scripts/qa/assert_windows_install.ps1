# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Assert the per-user Windows install state produced by DCENT_Voice-Setup.exe.

.DESCRIPTION
    The setup stub writes exactly four user-visible artefacts for a registered
    (non-/D) install: the Add/Remove Programs key, two Start Menu shortcuts under
    a "DCENT_Voice" folder (the app and "DCENT_Voice Diagnostics", which runs
    `dcent-voice.exe doctor --open`), and the payload under
    %LOCALAPPDATA%\DCENT_Voice. See packaging/windows/setup-stub/Program.cs
    (WriteShortcut / WriteUninstall) and InstallRegistration.cs.

    It writes no Startup-folder shortcut: launch-at-login is the HKCU Run value
    managed by src/dcent_voice/autostart.py and nothing else.

    Uninstall is deferred: RecoveryCoordinator.RunUninstaller launches a helper
    that removes the tree after the stub returns, so -Expect Removed polls
    rather than asserting once.

.PARAMETER Expect
    Installed (default) or Removed.

.PARAMETER TimeoutSeconds
    How long to wait for the expected state. Installed is checked immediately
    unless a timeout is given; Removed always polls.
#>
[CmdletBinding()]
param(
    [ValidateSet("Installed", "Removed")]
    [string]$Expect = "Installed",
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"

$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DCENT_Voice"
$InstallDir = Join-Path $env:LOCALAPPDATA "DCENT_Voice"
$InstalledExe = Join-Path $InstallDir "dcent-voice.exe"
$ProgramsFolder = Join-Path ([Environment]::GetFolderPath("Programs")) "DCENT_Voice"
$Shortcut = Join-Path $ProgramsFolder "DCENT_Voice.lnk"
$DiagnosticsShortcut = Join-Path $ProgramsFolder "DCENT_Voice Diagnostics.lnk"
$StartupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "DCENT_Voice.lnk"

function Get-DCENTInstallState {
    [ordered]@{
        "Add/Remove Programs key ($UninstallKey)"        = Test-Path -LiteralPath $UninstallKey
        "Start Menu shortcut ($Shortcut)"                = Test-Path -LiteralPath $Shortcut
        "Diagnostics shortcut ($DiagnosticsShortcut)"    = Test-Path -LiteralPath $DiagnosticsShortcut
        "Installed executable ($InstalledExe)"           = Test-Path -LiteralPath $InstalledExe
    }
}

$wantPresent = $Expect -eq "Installed"
$deadline = (Get-Date).AddSeconds([Math]::Max($TimeoutSeconds, 0))
$state = $null
while ($true) {
    $state = Get-DCENTInstallState
    $satisfied = @($state.Values | Where-Object { $_ -ne $wantPresent }).Count -eq 0
    if ($satisfied) { break }
    if ((Get-Date) -ge $deadline) { break }
    Start-Sleep -Seconds 2
}

foreach ($entry in $state.GetEnumerator()) {
    $status = if ($entry.Value) { "present" } else { "absent" }
    Write-Host ("  {0,-8} {1}" -f $status, $entry.Key)
}

$bad = @($state.GetEnumerator() | Where-Object { $_.Value -ne $wantPresent })
if ($bad.Count -ne 0) {
    $verb = if ($wantPresent) { "missing after install" } else { "still present after uninstall" }
    $names = ($bad | ForEach-Object { $_.Key }) -join "; "
    throw "DCENT_Voice install assertion failed ($Expect): $verb -> $names"
}

# Never present, in either state: the installer must not create a Startup-folder
# shortcut. Autostart is the HKCU Run value alone (src/dcent_voice/autostart.py).
if (Test-Path -LiteralPath $StartupShortcut) {
    throw ("DCENT_Voice install assertion failed: a Startup-folder shortcut exists at " +
        "$StartupShortcut. The installer must never create one; launch-at-login is the " +
        "HKCU Run value only.")
}
Write-Host ("  absent   Startup-folder shortcut ($StartupShortcut) [must never exist]")

if ($wantPresent) {
    $displayVersion = (Get-ItemProperty -LiteralPath $UninstallKey).DisplayVersion
    Write-Host "DCENT_Voice is installed (DisplayVersion=$displayVersion) at $InstallDir"
    $shell = New-Object -ComObject WScript.Shell
    $diagnosticsArgs = $shell.CreateShortcut($DiagnosticsShortcut).Arguments
    if ($diagnosticsArgs -ne "doctor --open") {
        throw ("DCENT_Voice install assertion failed: the diagnostics shortcut runs " +
            "'$diagnosticsArgs', expected 'doctor --open' " +
            "(dcent_voice.doctor.start_menu_shortcut_args).")
    }
    Write-Host "  ok       Diagnostics shortcut arguments: $diagnosticsArgs"
}
else {
    Write-Host "DCENT_Voice install artefacts are fully removed."
}
