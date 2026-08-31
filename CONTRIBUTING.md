# Contributing

DCENT_Voice is D-Central Technologies original work, released under the MIT
License. Thanks for helping make local-first dictation better.

## Licensing

Keep all contributions compatible with MIT, BSD, or Apache-2.0 licensing unless
the project explicitly approves a different boundary. Do not copy source code,
tests, prompts, or assets from GPL-licensed or otherwise incompatible projects
into this repository. Contributions must be your own work or carry a license we
can accept.

Every source file carries the D-Central copyright line and an
`SPDX-License-Identifier: MIT` header. New files must too — CI checks this.
Bundled third-party components are itemized in
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md); adding a dependency means
adding its entry there.

## Engineering rules

- Keep local-first behavior as the default. Anything that leaves the machine is
  explicit, consent-gated, and logged.
- Keep hotkey callbacks publish-only; no blocking work in input hooks.
- Log per-stage timings for real utterances.
- Prefer small, testable contracts over framework-heavy abstractions.
- Describe behavior in terms of what DCENT_Voice does. Do not name third-party
  dictation products in code, comments, tests, docs, or user-facing strings.

## Working in this checkout

A full checkout carries a PyInstaller `dist/` and `build/` tree, downloaded ASR
`models/`, and local benchmark output. Together they are large enough to exhaust
file watchers and RAM on a 16 GB machine, which stalls editors and language
servers. Avoid recursively indexing `dist/`, `build/`, `models/`, `internal/`,
`artifacts/`, or `packaging/windows/setup-stub/{bin,obj}/`, and prefer
path-limited searches under `src/`, `tests/`, `scripts/`, `packaging/`, and
`docs/`.

Do not launch a second `dcent-voice` GUI while one is running — a
single-instance lock is held and the speech process is roughly 1 GB. Use the CLI
subcommands (`transcribe`, `devices`, `verify-models`, `doctor`) for checks.

## Local checks

Install the development tools and repository hooks before contributing:

```powershell
uv sync --extra dev
uv run pre-commit install
```

Run the same checks on demand with `uv run pre-commit run --all-files`.

## Tests

The suite is 2000+ tests and takes roughly 5–6 minutes.

- Run it once, in one process, and read the summary. Do not run two `pytest`
  processes against the same checkout at the same time — concurrent runs collide
  on the Setup stub's `dotnet publish` output and on `dist/` while it is being
  rebuilt, producing failures that do not reproduce serially.
- When iterating, run only the affected files: `uv run pytest -q tests/test_x.py`.
  A default `timeout = 900` per test is configured in `pyproject.toml`; do not
  raise it globally.
- Packaging tests that publish the Windows Setup stub take a cross-process lock
  (`packaging/windows/setup-stub/.publish.lock`). Do not delete it while a run is
  active.
- Do not rebuild `dist/` while a test run is in flight;
  `tests/test_frozen_windows_payload.py` reads it in place.
- Live-desktop tests require the exact environment variable
  `DCENT_VOICE_ALLOW_INTERACTIVE_TESTS=1`. Set it only when you intend to drive
  the real desktop.

## Windows packaging

Build with `scripts/build_pyinstaller.ps1`, then `scripts/build_installer.ps1`.
The end-user artifact is `dist/DCENT_Voice-Setup.exe`, a per-user install under
`%LOCALAPPDATA%\DCENT_Voice` requiring no Python, admin rights, or model
download. See [docs/PACKAGING.md](docs/PACKAGING.md).

Public-beta builds are unsigned and SmartScreen may warn. Tagged releases
require Authenticode credentials and must never be described as signed without
them.

Please also follow the [Code of Conduct](CODE_OF_CONDUCT.md).
