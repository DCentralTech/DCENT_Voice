# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
param(
    [switch]$NoConfirm
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $PSScriptRoot "invoke_bounded.ps1")
Set-Location $Root

# Keep local builds on the same uv-managed environment and command path as the
# release workflow. A bare interpreter's pip may resolve outside the project.
uv sync --extra dev --frozen
if ($LASTEXITCODE -ne 0) {
    throw "uv sync failed with exit code $LASTEXITCODE."
}
uv run python scripts/release_version.py --write-windows-resources
if ($LASTEXITCODE -ne 0) {
    throw "Release version/resource generation failed."
}

$PyInstallerArgs = @("packaging/DCENT_Voice.spec", "--clean")
if ($NoConfirm) {
    $PyInstallerArgs += "--noconfirm"
}

# Model snapshots can originate in the Hugging Face cache with Windows'
# read-only attribute set. PyInstaller's --noconfirm replacement must be able
# to remove an earlier payload, so normalize only the exact dist tree it owns.
$ExistingPayload = [IO.Path]::GetFullPath((Join-Path $Root "dist\DCENT_Voice"))
$ExpectedDistRoot = [IO.Path]::GetFullPath((Join-Path $Root "dist"))
if ([IO.Path]::GetDirectoryName($ExistingPayload) -ne $ExpectedDistRoot) {
    throw "Refusing to normalize attributes outside the release dist directory."
}
if (Test-Path -LiteralPath $ExistingPayload -PathType Container) {
    $ReadOnly = [IO.FileAttributes]::ReadOnly
    @(
        Get-Item -LiteralPath $ExistingPayload -Force
        Get-ChildItem -LiteralPath $ExistingPayload -Recurse -Force
    ) | Where-Object { ($_.Attributes -band $ReadOnly) -ne 0 } | ForEach-Object {
        $_.Attributes = $_.Attributes -band (-bnot $ReadOnly)
    }
}

uv run pyinstaller @PyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$Payload = Join-Path $Root "dist\DCENT_Voice"
# Both shipped models are acquired only by this explicit release-build step,
# at immutable revisions and through exact file allowlists with SHA-256 checks.
uv run python scripts/download_models.py `
    --bundle-dir $Payload `
    --models "istupakov/parakeet-tdt-0.6b-v3-onnx,Systran/faster-whisper-base" `
    --accept-model-license
if ($LASTEXITCODE -ne 0) {
    throw "Pinned speech models were not staged and verified."
}
$Toc = Join-Path $Root "build\DCENT_Voice\PYZ-00.toc"
uv run python scripts/generate_release_sbom.py --payload $Payload --toc $Toc --repo-root $Root
if ($LASTEXITCODE -ne 0) {
    throw "Artifact-derived SBOM/license validation failed."
}
Invoke-DCENTBoundedProcess `
    -FilePath "uv" `
    -Arguments @("run", "python", "-m", "dcent_voice.asr.model_registry", "verify-payload", $Payload) `
    -Description "Shipped speech-model payload closed-world verification" `
    -TimeoutSeconds 300 `
    -WorkingDirectory $Root

# A development checkout is installed editable, which gives its distribution
# metadata a PEP 610 direct_url.json containing an absolute local path. The
# spec excludes that file; keep this post-build guard so a future PyInstaller
# or metadata-collection change cannot silently ship it.
$Internal = Join-Path $Root "dist\DCENT_Voice\_internal"
$EditableMetadata = @(
    Get-ChildItem -LiteralPath $Internal -Recurse -File -Filter "direct_url.json" |
        Where-Object { $_.Directory.Name -like "dcent_voice-*.dist-info" }
)
if ($EditableMetadata.Count -gt 0) {
    $Paths = $EditableMetadata.FullName -join ", "
    throw "PyInstaller output contains editable-install provenance: $Paths"
}
