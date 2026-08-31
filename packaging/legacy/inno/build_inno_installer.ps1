# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
[CmdletBinding()]
param([string]$Source = "")

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "invoke_bounded.ps1")
if (-not $Source) { $Source = Join-Path $repoRoot "dist\DCENT_Voice" }
$sealed = Join-Path $repoRoot ("dist\DCENT_Voice-inno-sealed-" + [guid]::NewGuid().ToString("N"))
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
$env:PYTHONPATH = Join-Path $repoRoot "src"
Invoke-DCENTBoundedProcess `
    -FilePath $python `
    -Arguments @("-m", "dcent_voice.asr.model_registry", "stage-payload", $Source, $sealed) `
    -Description "Inno payload staging" `
    -TimeoutSeconds 900
Invoke-DCENTBoundedProcess `
    -FilePath $python `
    -Arguments @("-m", "dcent_voice.asr.model_registry", "verify-payload", $sealed) `
    -Description "Inno payload verification" `
    -TimeoutSeconds 300
iscc "/DSealedPayload=$sealed" (Join-Path $repoRoot "packaging\windows\dcent-voice.iss")
if ($LASTEXITCODE -ne 0) { Write-Error "Inno Setup failed." }
Remove-Item -LiteralPath $sealed -Recurse -Force
