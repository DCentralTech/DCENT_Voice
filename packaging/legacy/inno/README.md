# Retired: the Inno Setup Windows installer

**These files are unsupported. Do not build or ship them.**

DCENT_Voice has exactly one supported Windows installer: the self-contained
.NET 8 SFX stub in [`packaging/windows/setup-stub/`](../../windows/setup-stub/),
built by [`scripts/build_installer.ps1`](../../../scripts/build_installer.ps1)
and published by `.github/workflows/release.yml` as `DCENT_Voice-Setup.exe`.
See [`docs/PACKAGING.md`](../../../docs/PACKAGING.md).

## Why they were retired

Two parallel installer pipelines with different post-install verification meant
drift: it was never clear which artifact a given user actually had, and only one
of them ran the installed executable as a self-check. The SFX stub won because it
needs no third-party toolchain (Inno Setup must be installed to compile
`dcent-voice.iss`), it already carried the recovery, model-migration and
uninstall logic, and it is what the release workflow builds.

The Inno pipeline's one advantage — running `dcent-voice.exe` after install
under a kill-on-close Job Object — was ported into the stub. See
`packaging/windows/setup-stub/OwnedJob.cs`, which is a direct C# port of the
`DcentOwnedJob` type in `verify-installed.ps1` below, and
`packaging/windows/setup-stub/PostInstallCheck.cs`, which uses it to run
`dcent-voice.exe doctor` against an isolated profile root.

## What is here

| File | Was |
|---|---|
| `dcent-voice.iss` | Inno Setup 6 script (`packaging/windows/dcent-voice.iss`) |
| `verify-installed.ps1` | Post-install verifier Inno invoked (`packaging/windows/verify-installed.ps1`) |
| `build_inno_installer.ps1` | Build driver (`scripts/build_inno_installer.ps1`) |

They are kept only as a reference for the Job Object ownership pattern and for
anyone auditing what previous unreleased builds did. They are not tested, not
built in CI, and will not be updated. `tests/test_packaging.py` asserts that they
stay out of the supported pipeline.

## Also retired here

The Inno script created a `{userstartup}` Startup-folder shortcut that the app's
own autostart code (`src/dcent_voice/autostart.py`) did not manage, so the two
could disagree. There is now exactly one autostart mechanism: the
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run\DCENT_Voice` value written by
`autostart.py` and removed by the uninstaller. The supported installer never
creates a Startup-folder shortcut.
