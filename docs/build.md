# Build DCENT Voice yourself

**Last updated:** 2026-08-28 · **Author:** D-Central Technologies

DCENT Voice is MIT-licensed. Anyone can compile a **Windows Setup**, a **Linux AppImage/`.deb`**, or a **macOS `.app`/`.dmg`** from this repository. PyInstaller **cannot cross-compile**: each native artifact must be built **on that OS**.

| You have | You can build | Command |
|---|---|---|
| 64-bit Windows 10 1607+ / Windows 11, Python 3.11, .NET 8 SDK | `DCENT_Voice-Setup.exe` + portable ZIP | `scripts/build_pyinstaller.ps1` then `scripts/build_installer.ps1` |
| Linux x86_64 or aarch64 (or Docker Linux engine) | AppImage + `.deb` | `bash scripts/build_linux_appimage.sh` |
| macOS (Intel or Apple silicon) | `DCENT Voice.app`, `.dmg`, `.zip` | `bash scripts/build_macos_app.sh` |
| No Mac | Unsigned macOS artifacts via **GitHub Actions** `macos-14` | Actions → **Build native artifacts** → Run workflow |

Source-only (no freeze): `python -m pip install .` then `dcent-voice`. See [install-windows](install-windows.md), [install-macos](install-macos.md), [install-linux](install-linux.md).

All freeze recipes stage **pinned** Parakeet TDT 0.6B v3 and Faster Whisper `base` into the payload. First freeze needs network for those weights; runtime dictation does not.

---

## Windows (Setup.exe)

Needs: Git, [uv](https://docs.astral.sh/uv/), Python 3.11, [.NET SDK 8.0.424](https://dotnet.microsoft.com/download/dotnet/8.0) (pinned by `global.json`).

```powershell
git clone https://github.com/DCentralTech/DCENT_Voice.git
cd DCENT_Voice
powershell -ExecutionPolicy Bypass -File .\scripts\build_pyinstaller.ps1 -NoConfirm
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
```

Outputs:

- `dist\DCENT_Voice\` — frozen app
- `dist\DCENT_Voice-Setup.exe` — per-user installer
- `dist\DCENT_Voice-Setup.exe.sha256`

Optional portable ZIP: `.\scripts\build_portable_zip.ps1`.

The public-beta Setup is **unsigned** unless you Authenticode-sign it (`scripts/sign_windows.ps1`). SmartScreen may warn.

Details: [PACKAGING.md](PACKAGING.md).

---

## Linux (AppImage and .deb)

Needs a **Linux** kernel. WSL2 Ubuntu 22.04 or Docker Desktop’s Linux engine both count. Building on Ubuntu 22.04 (glibc 2.35) matches CI.

### Native Linux

```bash
sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
  appstream build-essential python3-dev linux-libc-dev libportaudio2 portaudio19-dev \
  libgtk-3-0 libgtk-3-dev libwebkit2gtk-4.0-37 libwebkit2gtk-4.0-dev \
  gir1.2-webkit2-4.0 xvfb \
  libgirepository1.0-dev libcairo2-dev pkg-config libx11-6 libxtst6 curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
# Pin appimagetool 1.9.1 and a type-2 runtime (do not fetch "continuous" at pack time):
# hashes are in .github/workflows/release.yml
export APPIMAGETOOL=/path/to/appimagetool-x86_64.AppImage
export APPIMAGE_RUNTIME_FILE=/path/to/runtime-x86_64
bash scripts/build_linux_appimage.sh
```

Outputs under `dist/`: `DCENT_Voice-linux-<arch>-<version>.AppImage` and `dcent-voice_<debian>_<arch>.deb` plus `.sha256` sidecars.

### Docker (from Windows or macOS)

The Linux engine must be running (Docker Desktop → linux containers). Then:

```powershell
# Windows PowerShell, from the repo root
.\scripts\docker_linux_release.ps1
```

```bash
# macOS / Linux with Docker
bash scripts/docker_linux_release.sh
```

That copies the source **into** the container (it does not reuse a Windows `.venv` or `dist/`). Artifacts land in `dist/`. AppImage packaging inside Docker sets `APPIMAGE_EXTRACT_AND_RUN=1` because FUSE is usually absent.

Expect several GB of RAM and 30–90 minutes on a first run (PyInstaller + model fetch).

---

## macOS (.app / .dmg / .zip)

Must run on **Darwin**. From a Mac:

```bash
xcode-select --install   # if needed
curl -LsSf https://astral.sh/uv/install.sh | sh
bash scripts/build_macos_app.sh
```

Unsigned local builds need no Apple Developer account. To codesign: `MACOS_SIGNING_IDENTITY`. To notarize: notary API key or Apple ID variables documented in [PACKAGING.md](PACKAGING.md). Partial credentials fail closed.

Validate the recipe on any OS (including Windows):

```bash
uv run python scripts/check_macos_pipeline.py
```

### No Mac: best option

**GitHub-hosted `macos-14` runners.** You do not rent hardware and you do not cross-compile.

1. Push this repo to GitHub (`DCentralTech/DCENT_Voice`).
2. Actions → **Build native artifacts** → **Run workflow**.
3. Download the `macos-unsigned` artifact (`.app` inside `.dmg`/`.zip`).

That build is **unsigned**. Users will right-click → Open the first time (Gatekeeper). Notarized builds need an Apple Developer ID in repository secrets and the tagged `release.yml` path — not this dispatch job.

Alternatives if you will not use GitHub Actions: a rented Mac (MacStadium, AWS EC2 Mac, MacinCloud) and the same `build_macos_app.sh`. A Linux VM or Docker cannot produce a macOS `.app`.

---

## GitHub Actions (maintainers)

| Workflow | When | What |
|---|---|---|
| `ci.yml` | PR / push `main` | tests; no native freeze |
| `build-native.yml` | **workflow_dispatch** | unsigned Linux AppImage/deb + unsigned macOS dmg/zip as Actions artifacts |
| `release.yml` | tag `v*` | signed Windows (Authenticode required), Linux AppImage/deb, signed+notarized macOS (Apple secrets required) |

For the public beta, attach Windows Setup with `scripts/publish_github_release.ps1 -ArtifactDir dist`. Do not push a `v*` tag until Authenticode (and, if you want a notarized Mac build, Apple) secrets exist.

---

## License

SPDX-License-Identifier: MIT  
Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty.
