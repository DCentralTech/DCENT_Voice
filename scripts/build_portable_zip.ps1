# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
[CmdletBinding()]
param(
    [string]$Source = "",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "invoke_bounded.ps1")
if (-not $Source) { $Source = Join-Path $repoRoot "dist\DCENT_Voice" }
if (-not $Output) { $Output = Join-Path $repoRoot "dist\DCENT_Voice-portable.zip" }
$sealed = Join-Path $repoRoot ("dist\DCENT_Voice-portable-sealed-" + [guid]::NewGuid().ToString("N"))
if (Test-Path $sealed) { Remove-Item -LiteralPath $sealed -Recurse -Force }
if (Test-Path $Output) { Remove-Item -LiteralPath $Output -Force }
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
$env:PYTHONPATH = Join-Path $repoRoot "src"
Invoke-DCENTBoundedProcess `
    -FilePath $python `
    -Arguments @("-m", "dcent_voice.asr.model_registry", "stage-payload", $Source, $sealed) `
    -Description "Portable payload staging" `
    -TimeoutSeconds 900
Invoke-DCENTBoundedProcess `
    -FilePath $python `
    -Arguments @("-m", "dcent_voice.asr.model_registry", "verify-payload", $sealed) `
    -Description "Portable payload verification" `
    -TimeoutSeconds 300
Compress-Archive -Path (Join-Path $sealed "*") -DestinationPath $Output -Force
Remove-Item -LiteralPath $sealed -Recurse -Force
& (Join-Path $PSScriptRoot "write_sha256.ps1") -Path $Output -Output "$Output.sha256"
Write-Host "Verified portable ZIP and SHA-256 written to $Output"
