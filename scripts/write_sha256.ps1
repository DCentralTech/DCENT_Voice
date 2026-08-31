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
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact.FullName).Hash.ToLowerInvariant()
$line = "$hash  $($artifact.Name)"
Set-Content -LiteralPath $Output -Value $line -Encoding ascii
Write-Host "SHA256: $line"
