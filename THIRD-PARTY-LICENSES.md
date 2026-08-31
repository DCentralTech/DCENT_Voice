# Third-Party Licenses

DCENT_Voice itself is MIT licensed (see `LICENSE`). It depends on and, in a
packaged build, redistributes the following third-party components under their
own licenses. This file is a summary; each project's full license governs.

## Bundled assets

- **Barlow Condensed, Inter, JetBrains Mono** — SIL Open Font License 1.1.
  The full OFL, exact upstream copyright/source URLs, byte provenance,
  modification status, and Reserved Font Name analysis are in
  `src/dcent_voice/ui/web/fonts/LICENSE.md` and its adjacent `OFL-1.1.txt`.
  Native artifacts copy both into `_internal/licenses/fonts/` and bind every
  shipped WOFF2 to the artifact-derived SBOM.

## Python runtime dependencies

Verified 2026-07-11 with `pip-licenses` against the packages resolved by
`uv.lock`. Versions below are the current locked runtime versions; platform and
optional extras install only their applicable subset.

| Package | Locked version | License |
|---------|----------------|---------|
| faster-whisper / CTranslate2 | 1.2.1 / 4.8.1 | MIT |
| onnxruntime | 1.27.0 | MIT |
| tokenizers | 0.23.1 | Apache-2.0 |
| av (PyAV; online source environment only) | 18.0.0 | BSD-3-Clause |
| DCENT PCM-only `av` compatibility wheel (offline bundle only) | 18.0.0+dcentshim.1 | MIT; source and full text under `packaging/av-shim/` |
| fastapi / starlette / uvicorn | 0.139.0 / 1.3.1 / 0.50.0 | MIT / BSD-3-Clause |
| websockets | 16.0 | BSD-3-Clause |
| pydantic / pydantic-settings | 2.13.4 / 2.14.2 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| numpy | 2.4.6 | BSD-3-Clause plus bundled permissive components |
| sounddevice (PortAudio) | 0.5.5 | MIT / PortAudio license |
| pynput | 1.8.2 | LGPL-3.0 |
| pystray / Pillow | 0.19.5 / 12.3.0 | LGPL-3.0 / MIT-CMU (HPND) |
| pywebview | 6.2.1 | BSD-3-Clause |
| PyGObject / pycairo (Linux) | 3.50.0 / 1.29.1 | LGPL-2.1-or-later / LGPL-2.1-or-later or MPL-1.1 |
| SecretStorage (Linux) | 3.x | BSD-3-Clause |
| keyring / platformdirs | 25.7.0 / 4.10.0 | MIT |
| pywin32 (Windows) | 312 | PSF-2.0 |
| uiautomation (Windows) | 2.0.29 | Apache-2.0 |
| pyobjc frameworks (macOS) | 12.2.1 | MIT |
| deepgram-sdk / groq / openai (cloud extra) | 7.4.0 / 1.5.0 / 2.44.0 | MIT / Apache-2.0 |
| onnx-asr (default Parakeet runtime) | 0.12.0 | MIT |
| NVIDIA Parakeet TDT 0.6B v3 ONNX weights (NVIDIA model; ONNX conversion by istupakov) | pinned mirror revision `8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce`, shipped by native installers | CC-BY-4.0 |
| click / importlib-metadata / tqdm | 8.4.2 / 9.0.0 / 4.68.3 | BSD-3-Clause / Apache-2.0 / MPL-2.0 and MIT |

## Packaged language and native runtimes

Native artifacts also redistribute the PyInstaller bootloader and runtime hooks
(GPL-2.0-or-later with the PyInstaller bootloader exception), PyInstaller hooks
contrib, the CPython runtime (PSF-2.0), OpenSSL 3 (Apache-2.0), SQLite (public
domain), and libffi (permissive MIT-like license). Every release build derives
`THIRD-PARTY-SBOM.cdx.json` from the actual PyInstaller module table and fails
closed if an embedded distribution has no full license evidence. Full texts,
including the PyInstaller `COPYING.txt` with bootloader exception, are copied to
`_internal/licenses/`; the SBOM binds each notice to its SHA-256.

Windows payloads additionally redistribute the non-ASIO PortAudio library and
Microsoft Visual C++/Universal CRT files. Their exact artifact paths and hashes
are recorded in the SBOM; the bundle includes the full PortAudio MIT license
and Microsoft's proprietary redistribution notice with links to the governing
runtime terms and official redistribution list. The optional PyAV decoder is
not used by DCENT_Voice (audio reaches Faster Whisper as decoded float32 PCM),
so native release builds exclude PyAV, FFmpeg, and their codec libraries rather
than implying that PyAV's BSD license covers those binaries. The unused
PortAudio ASIO variant is excluded as well.

Offline wheelhouses also exclude the upstream PyAV wheel and its bundled
FFmpeg/x264/x265 codec stack. They include the source-visible MIT compatibility
distribution under `packaging/av-shim` instead. DCENT_Voice supplies decoded
float32 PCM to Faster Whisper; the shim exists only for its eager `av` import
and fails closed if a caller attempts file or codec decoding.

Windows Setup is a self-contained offline .NET 8 executable. Its staged Setup
payload includes the exact SDK-distributed `LICENSE.txt` and
`ThirdPartyNotices.txt`; the Setup-specific SBOM records the .NET and Windows
Desktop runtime versions resolved by the published stub. These notices are
Setup-only and are not claimed as components of the portable application ZIP.

## Optional TTS runtime preview (not bundled in the Windows public beta)

| Package | License |
|---------|---------|
| kokoro-onnx | MIT; its current `phonemizer-fork` dependency is GPL-3.0-or-later |

This is an optional source extra (`.[tts]`), not installed by default and not
redistributed in this beta bundle while license compatibility is reviewed.

## LGPL handling in native bundles

`pynput` and `pystray` are unmodified LGPL-3.0 Python components. The PyInstaller
artifact is a one-directory bundle and deliberately collects both packages as
separate, replaceable source trees rather than embedding them only in the
executable archive. Their distribution metadata and full license texts are
included alongside the package files, and `THIRD-PARTY-LICENSES.md` is shipped at
the bundle's `_internal` root. Users may replace those components or run the
application from source against modified versions.

Linux bundles additionally contain unmodified PyGObject and pycairo modules as
separate files in the one-directory payload. Their native GTK/WebKit libraries
remain dynamically linked, and the corresponding distribution metadata and
license files are collected by PyInstaller.

## Speech models

The default payload includes the CTranslate2 **Systran/faster-whisper-base**
snapshot at immutable Hugging Face revision
`ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66`, declared **MIT** by its model
card. Its source, revision, exact file sizes, and SHA-256 hashes are recorded in
`dcent_voice/asr/manifests/faster-whisper-base.json`. Release builders download
that revision only after an explicit license-acceptance flag and verify every
runtime file before packaging. Runtime transcription is local-files-only and
never downloads a model. The shipped Parakeet snapshot is likewise closed-world
verified from the immutable
[`istupakov/parakeet-tdt-0.6b-v3-onnx`](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/tree/8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce)
revision and redistributed under CC-BY-4.0 with attribution to NVIDIA and the
ONNX converter. Release artifacts include the complete CC-BY-4.0 text, NVIDIA
and converter attribution, and the full SYSTRAN MIT permission/copyright text
outside the closed model directories so model verification remains exact. Other Faster Whisper models must be installed as
explicit verified offline bundles; missing weights fail with a recovery action.

TTS models are likewise downloaded at runtime (consent-gated, SHA-256 verified,
with a license note written beside each file): **Kokoro-82M** weights under
**Apache-2.0** are the sole public-beta model path. Piper is deliberately not
offered or downloaded in this beta pending compatible voice licensing. Use
**Settings -> Models** to review and explicitly approve the Kokoro download;
the Windows public beta does not include the corresponding runtime engine.
**XTTS / Coqui TTS is intentionally not supported** — its model license
(Coqui Public Model License) is non-commercial and incompatible with this
project (ADR V003).

## Bitcoin logo / orange

The Bitcoin symbol and "Bitcoin orange" heritage inform the D-Central brand; the
Bitcoin logo is public domain.

> Re-run `uv run --with pip-licenses pip-licenses` after any `uv.lock` update and
> verify the platform-specific PyInstaller artifact before release.
