# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Build dist\DCENT_Voice-Setup.exe from the PyInstaller tree + native stub.
[CmdletBinding()]
param(
    [string]$Source = "",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "invoke_bounded.ps1")
if (-not $Source) { $Source = Join-Path $repoRoot "dist\DCENT_Voice" }
if (-not $Output) { $Output = Join-Path $repoRoot "dist\DCENT_Voice-Setup.exe" }
$exe = Join-Path $Source "dcent-voice.exe"
if (-not (Test-Path $exe)) {
    Write-Error "No dcent-voice.exe in $Source. Build with scripts/build_pyinstaller.ps1 first."
}

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
$env:PYTHONPATH = Join-Path $repoRoot "src"
& $python scripts/release_version.py --write-windows-resources
if ($LASTEXITCODE -ne 0) {
    Write-Error "Release version/resource generation failed with exit code $LASTEXITCODE"
}

$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$userDotnet = Join-Path $env:LOCALAPPDATA "dotnet\dotnet.exe"
if (Test-Path $userDotnet) {
    $env:PATH = "$(Split-Path $userDotnet);$env:PATH"
    $env:DOTNET_ROOT = Split-Path $userDotnet
}
$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) {
    Write-Error "dotnet SDK not found. Install .NET 8 SDK or place it at $userDotnet"
}
$dotnetRoot = Split-Path -Parent $dotnet.Source

$stubProj = Join-Path $repoRoot "packaging\windows\setup-stub\DCENT_Voice.Setup.csproj"
$stubOut = Join-Path $repoRoot "packaging\windows\setup-stub\bin\Release\net8.0-windows\win-x64\publish\DCENT_Voice-Setup.exe"
Write-Host "Publishing installer stub..."
dotnet restore $stubProj --runtime win-x64 --locked-mode --nologo
if ($LASTEXITCODE -ne 0) {
    Write-Error "dotnet locked restore failed with exit code $LASTEXITCODE"
}
dotnet publish $stubProj -c Release --no-restore --nologo
if ($LASTEXITCODE -ne 0) {
    Write-Error "dotnet publish failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path $stubOut)) {
    Write-Error "dotnet publish did not produce $stubOut"
}
& $python scripts/patch_setup_original_filename.py $stubOut
if ($LASTEXITCODE -ne 0) {
    Write-Error "Setup OriginalFilename resource patch failed with exit code $LASTEXITCODE"
}

$payloadDir = Join-Path $repoRoot ("dist\DCENT_Voice-sealed-" + [guid]::NewGuid().ToString("N"))
$payload = Join-Path $repoRoot "dist\DCENT_Voice-payload.zip"
if (Test-Path $payloadDir) { Remove-Item $payloadDir -Recurse -Force }
if (Test-Path $payload) { Remove-Item $payload -Force }
Write-Host "Staging verified payload from $Source ..."
Invoke-DCENTBoundedProcess `
    -FilePath $python `
    -Arguments @("-m", "dcent_voice.asr.model_registry", "stage-payload", $Source, $payloadDir) `
    -Description "Model-safe Setup payload staging" `
    -TimeoutSeconds 900
$toc = Join-Path $repoRoot "build\DCENT_Voice\PYZ-00.toc"
$setupAssets = Join-Path $repoRoot "packaging\windows\setup-stub\obj\project.assets.json"
& $python scripts/generate_release_sbom.py `
    --payload $payloadDir `
    --toc $toc `
    --repo-root $repoRoot `
    --setup-dotnet-root $dotnetRoot `
    --setup-assets $setupAssets
if ($LASTEXITCODE -ne 0) {
    Write-Error "Setup-specific SBOM/.NET license validation failed with exit code $LASTEXITCODE"
}
Invoke-DCENTBoundedProcess `
    -FilePath $python `
    -Arguments @("-m", "dcent_voice.asr.model_registry", "verify-payload", $payloadDir) `
    -Description "Final Setup payload verification" `
    -TimeoutSeconds 300
Write-Host "Zipping payload..."
Compress-Archive -Path (Join-Path $payloadDir "*") -DestinationPath $payload -Force

& $python -m dcent_voice.package_windows $stubOut $payload $Output
if ($LASTEXITCODE -ne 0) {
    Write-Error "package_windows failed with exit code $LASTEXITCODE"
}
Remove-Item -LiteralPath $payloadDir -Recurse -Force

$shaPath = "$Output.sha256"
& (Join-Path $PSScriptRoot "write_sha256.ps1") -Path $Output -Output $shaPath
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Output).Hash.ToLowerInvariant()
$setupBytes = (Get-Item $Output).Length
$setupSize = [math]::Round($setupBytes / 1MB, 1)
Write-Host "Unsigned Setup.exe written to $Output ($setupSize MB)"
# WS5.7: size is not a release blocker, but a silent creep past ~900 MB makes the
# download the worst part of the first-run experience. Warn, do not fail.
$sizeBudgetMb = 900
if ($setupBytes -gt ($sizeBudgetMb * 1MB)) {
    Write-Warning ("Setup.exe is $setupSize MB, over the $sizeBudgetMb MB budget. " +
        "Investigate the payload before shipping (measure first: zip level, and the " +
        "_internal\pythonnet\runtime\System.*.dll facades netfx does not need).")
}
Write-Host "SHA256: $hash"
Write-Host "ENV: Authenticode signing is not applied."
