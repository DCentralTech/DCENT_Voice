# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT

function Invoke-DCENTBoundedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description,
        [ValidateRange(1, 3600)][int]$TimeoutSeconds = 900,
        [string]$WorkingDirectory = ""
    )

    $file = Get-Item -LiteralPath $FilePath -ErrorAction SilentlyContinue
    if ($file) {
        $resolvedExecutable = $file.FullName
    } else {
        $command = Get-Command $FilePath -ErrorAction Stop
        $resolvedExecutable = $command.Source
    }
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $pythonCommand = Get-Command "python.exe" -ErrorAction Stop
        $python = $pythonCommand.Source
    }
    $runner = Join-Path $PSScriptRoot "run_bounded.py"
    $previousDirectory = Get-Location
    try {
        if ($WorkingDirectory) {
            Set-Location -LiteralPath $WorkingDirectory
        }
        & $python $runner --timeout $TimeoutSeconds -- $resolvedExecutable @Arguments
        $exitCode = $LASTEXITCODE
    } finally {
        Set-Location -LiteralPath $previousDirectory
    }
    if ($exitCode -eq 124) {
        throw "$Description timed out after $TimeoutSeconds seconds and was terminated."
    }
    if ($exitCode -ne 0) {
        throw "$Description failed with exit code $exitCode."
    }
}
