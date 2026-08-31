# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Install the unpacked PyInstaller directory as a per-user Windows app.
# Does not require Python. Signing is a separate step when credentials exist.
[CmdletBinding()]
param(
    [string]$Source = "",
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "invoke_bounded.ps1")
if (-not $Source) {
    $Source = Join-Path $repoRoot "dist\DCENT_Voice"
}
if (-not $Destination) {
    $Destination = Join-Path $env:LOCALAPPDATA "DCENT_Voice"
}

if (-not (Test-Path (Join-Path $Source "dcent-voice.exe"))) {
    Write-Error "No dcent-voice.exe in $Source. Build with scripts/build_pyinstaller.ps1 first."
}

$sourceExe = Join-Path $Source "dcent-voice.exe"
Invoke-DCENTBoundedProcess `
    -FilePath $sourceExe `
    -Arguments @("stage-payload", $Source, $Destination) `
    -Description "Verified install staging" `
    -TimeoutSeconds 900 `
    -WorkingDirectory $Source
$installedExe = Join-Path $Destination "dcent-voice.exe"
Invoke-DCENTBoundedProcess `
    -FilePath $installedExe `
    -Arguments @("verify-payload", $Destination) `
    -Description "Installed payload verification" `
    -TimeoutSeconds 300 `
    -WorkingDirectory $Destination

$exe = Join-Path $Destination "dcent-voice.exe"
$programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\DCENT_Voice"
New-Item -ItemType Directory -Force -Path $programs | Out-Null
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $programs "DCENT_Voice.lnk"))
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $Destination
$shortcut.Description = "Local-first voice dictation"
$shortcut.Save()

# The diagnostics entry is what a stuck user can still find when the app itself
# will not start. Its arguments must match dcent_voice.doctor.start_menu_shortcut_args().
$diagnostics = $shell.CreateShortcut((Join-Path $programs "DCENT_Voice Diagnostics.lnk"))
$diagnostics.TargetPath = $exe
$diagnostics.Arguments = "doctor --open"
$diagnostics.WorkingDirectory = $Destination
$diagnostics.Description = "Diagnose why DCENT_Voice will not start and open the report"
$diagnostics.Save()

$uninstallPath = Join-Path $Destination "Uninstall.ps1"
$uninstall = @"
# DCENT_Voice uninstaller
Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DCENT_Voice' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '$Destination' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '$programs' -Recurse -Force -ErrorAction SilentlyContinue
"@
Set-Content -Path $uninstallPath -Value $uninstall -Encoding UTF8

$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DCENT_Voice"
New-Item -Path $uninstallKey -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name "DisplayName" -Value "DCENT_Voice" -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name "Publisher" -Value "D-Central Technologies" -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name "DisplayVersion" -Value "0.2.0b1" -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name "InstallLocation" -Value $Destination -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name "UninstallString" -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallPath`"" -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null

Write-Host "Installed DCENT_Voice to $Destination"
Write-Host "Start Menu shortcut created. Apps & Features uninstall is registered."
Write-Host "Uninstall with $uninstallPath"
Write-Host "ENV: Authenticode signing is not applied in this environment."
