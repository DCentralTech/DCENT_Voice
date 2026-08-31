# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Write a portable sha256sum-compatible sidecar for one release artifact.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$artifact = Get-Item -LiteralPath $Path -ErrorAction Stop
if ($artifact.PSIsContainer) {
    throw "Cannot checksum a directory: $($artifact.FullName)"
}
if (-not $Output) {
    $Output = "$($artifact.FullName).sha256"
}
# Hash through .NET rather than Get-FileHash. Get-FileHash lives in
# Microsoft.PowerShell.Utility, and that module does not always autoload under
# `powershell.exe -NoProfile` on a CI runner whose PSModulePath the host has
# rewritten -- the release checksum step then dies with "The term 'Get-FileHash'
# is not recognized". .NET is always present, so this works everywhere.
$sha256 = [System.Security.Cryptography.SHA256]::Create()
try {
    $stream = [System.IO.File]::OpenRead($artifact.FullName)
    try {
        $digest = $sha256.ComputeHash($stream)
    } finally {
        $stream.Dispose()
    }
} finally {
    $sha256.Dispose()
}
$hash = [System.BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
$line = "$hash  $($artifact.Name)"
# sha256sum-compatible sidecars are ASCII with a trailing newline.
[System.IO.File]::WriteAllText($Output, "$line`n", [System.Text.Encoding]::ASCII)
[Console]::WriteLine("SHA256: $line")
