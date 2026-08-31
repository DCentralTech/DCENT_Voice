# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Build Linux AppImage + .deb via Docker Desktop (Linux engine).
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker is not on PATH. Start Docker Desktop with Linux containers."
}
$dist = Join-Path $Root "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null

$src = ($Root -replace '\\', '/')
Write-Host "Building Linux release in ubuntu:22.04 from $src"
docker run --rm `
    --memory=10g `
    -e DCENT_IN_DOCKER=1 `
    -e APPIMAGE_EXTRACT_AND_RUN=1 `
    -e DEBIAN_FRONTEND=noninteractive `
    -v "${Root}:/src:ro" `
    -v "${dist}:/out" `
    ubuntu:22.04 `
    bash /src/scripts/docker_linux_release.sh
if ($LASTEXITCODE -ne 0) {
    throw "docker Linux release build failed with exit $LASTEXITCODE"
}
Write-Host "Done. Look under dist\ for AppImage and .deb"
