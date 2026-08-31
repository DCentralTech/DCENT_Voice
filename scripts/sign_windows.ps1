# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Authenticode-sign Windows artifacts when a PFX is available.
# Missing credentials are an environment blocker, not a product pass.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$Path
)

$ErrorActionPreference = "Stop"
$certB64 = $env:WINDOWS_CERT_PFX_BASE64
if (-not $certB64) { $certB64 = $env:CERT_B64 }
$certPwd = $env:WINDOWS_CERT_PASSWORD
if (-not $certPwd) { $certPwd = $env:CERT_PWD }

if (-not $certB64 -or -not $certPwd) {
    Write-Host "ENV: Authenticode credentials absent; leaving files unsigned."
    foreach ($item in $Path) {
        if (Test-Path $item) {
            $status = (Get-AuthenticodeSignature -FilePath $item).Status
            Write-Host "  $item status=$status"
        }
    }
    exit 0
}

$pfx = Join-Path $env:TEMP ("dcent-authenticode-" + [guid]::NewGuid().ToString("N") + ".pfx")
try {
    [IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($certB64))
    $secure = ConvertTo-SecureString $certPwd -AsPlainText -Force
    $cert = Get-PfxCertificate -FilePath $pfx -Password $secure
    foreach ($item in $Path) {
        if (-not (Test-Path $item)) {
            throw "missing file to sign: $item"
        }
        $result = Set-AuthenticodeSignature -FilePath $item `
            -Certificate $cert -HashAlgorithm SHA256 `
            -TimestampServer "http://timestamp.digicert.com"
        Write-Host "signed $item status=$($result.Status)"
        if ($result.Status -ne "Valid") {
            throw "Signing failed for $item : $($result.StatusMessage)"
        }
        # Signing changes the artifact bytes. Refresh an existing build
        # sidecar only after the signed file validates, so release packaging
        # can never publish the pre-sign checksum.
        $shaPath = "$item.sha256"
        if (Test-Path -LiteralPath $shaPath) {
            & (Join-Path $PSScriptRoot "write_sha256.ps1") -Path $item -Output $shaPath
        }
    }
}
finally {
    if (Test-Path $pfx) { Remove-Item $pfx -Force }
}
