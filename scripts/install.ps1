# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
param(
  [switch]$Offline,
  [string]$BundlePath = ".\build\offline-bundle",
  [string]$InstallDir = ".\.venv",
  [string]$Python = "python",
  [switch]$NoModels
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPath = if ([IO.Path]::IsPathRooted($InstallDir)) {
  [IO.Path]::GetFullPath($InstallDir)
} else {
  [IO.Path]::GetFullPath((Join-Path $RepoRoot $InstallDir))
}

function Require-Command($Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "Required command '$Name' was not found on PATH."
  }
}

Require-Command "uv"

$ResolvedPython = $Python
if ($Python -eq "python") {
  $ResolvedPython = (& uv python find ">=3.11").Trim()
  if ($LASTEXITCODE -ne 0 -or -not $ResolvedPython) {
    throw "Python 3.11 or newer was not found. Pass -Python with a compatible interpreter."
  }
}

Push-Location $RepoRoot
try {
  if ($Offline) {
    $ResolvedBundle = Resolve-Path $BundlePath
    $ManifestPath = Join-Path $ResolvedBundle "dcent-voice-offline-bundle.json"
    if (-not (Test-Path $ManifestPath)) {
      throw "Offline bundle manifest is missing: $ManifestPath"
    }
    $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    if ($Manifest.remoteUrls.Count -gt 0) {
      throw "Offline bundle manifest must not contain remote URLs."
    }
    $WheelDir = Join-Path $ResolvedBundle $Manifest.wheelDir
    if (-not (Test-Path $WheelDir)) {
      throw "Offline bundle wheel directory is missing: $WheelDir"
    }
    & $ResolvedPython (Join-Path $RepoRoot "scripts\verify_offline_bundle.py") $ManifestPath
    if ($LASTEXITCODE -ne 0) {
      throw "Offline wheel verification failed with exit code $LASTEXITCODE."
    }
  }

  uv venv $VenvPath --python $ResolvedPython
  if ($LASTEXITCODE -ne 0) {
    throw "Virtual environment creation failed with exit code $LASTEXITCODE."
  }

  if ($Offline) {
    uv pip install --python $VenvPath --offline --no-index --find-links $WheelDir dcent-voice
    if ($LASTEXITCODE -ne 0) {
      throw "Offline package installation failed with exit code $LASTEXITCODE."
    }
    if (-not $NoModels) {
      $MissingModels = @($Manifest.models | Where-Object { -not $_.present })
      if ($MissingModels.Count -gt 0) {
        throw "Offline bundle manifest has missing models: $($MissingModels.modelId -join ', ')"
      }
      $VenvPython = Join-Path $VenvPath "Scripts\python.exe"
      & $VenvPython -m dcent_voice.asr.model_registry install-bundle $ManifestPath
      if ($LASTEXITCODE -ne 0) {
        throw "Offline model installation failed with exit code $LASTEXITCODE."
      }
    }
  } else {
    uv pip install --python $VenvPath .
    if ($LASTEXITCODE -ne 0) {
      throw "Package installation failed with exit code $LASTEXITCODE."
    }
  }

  Write-Host "DCENT_Voice installed at $VenvPath"
  Write-Host "Run: $VenvPath\Scripts\python.exe -m dcent_voice --no-tray --no-overlay"
} finally {
  Pop-Location
}
