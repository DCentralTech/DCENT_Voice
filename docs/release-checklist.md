# Release checklist

A release is not "the build went green". It is a claim that a person who has
never run DCENT_Voice can download one file and end up dictating. This checklist
exists because that claim was false for every build shipped before
`v0.2.0-beta.2`: the app could not create its own configuration on a machine
with no `config.toml`, and it failed silently — no window, no log, no exit
message. See [`FRESH_MACHINE_IMPLEMENTATION_PLAN.md`](FRESH_MACHINE_IMPLEMENTATION_PLAN.md).

The rule that follows from that: **nothing is published until it has been proven
on a host that has never had DCENT_Voice on it, and the proof is committed.**

---

## Gate 0 — the evidence directory (blocking)

Before you may publish anything, `docs/release-evidence/<version>/` must exist
in the repository and contain the artefacts listed in
[Gate 4](#gate-4--pristine-host-evidence-blocking). `<version>` is the exact tag
being published, e.g. `docs/release-evidence/v0.2.0-beta.2/`.

If that directory is absent or incomplete, **stop**. Do not tag, do not upload,
do not send anyone a link. This gate is the whole point of the checklist: CI can
only prove what a GitHub runner can see, and a GitHub runner is not a person's
laptop.

---

## Gate 1 — the tree is releasable

- [ ] Working tree is clean and on the release branch.
- [ ] `uv run ruff check . && uv run ruff format --check .` passes.
- [ ] `uv run pytest -q` passes on Windows.
- [ ] `CHANGELOG.md` has a dated section for this version — no entries left
      under `[Unreleased]` that belong in it.
- [ ] `pyproject.toml` version and `scripts/release_version.py --check-version`
      agree with the tag.
- [ ] `THIRD-PARTY-LICENSES.md` regenerated if dependencies changed.

## Gate 2 — CI is green on all three platforms

The automated half of AC7. Confirm the run for **this exact commit**:

- [ ] `Windows package smoke` — payload build, fresh-profile smoke from a
      neutral working directory, `doctor`, `Setup.exe /S`, installed-executable
      smoke, `/uninstall /S`.
- [ ] `Linux package smoke` — AppImage build and fresh-profile smoke under
      `xvfb-run` from `/`.
- [ ] `macOS package smoke` — `.app` build and fresh-profile smoke from `/` with
      an isolated `HOME`.
- [ ] `test` matrix green.

CI proves the app starts on a clean profile. It does **not** prove what a human
sees, and on Linux/macOS it runs headless — so it cannot exercise the desktop
permission and helper-tool checks (`desktop.*`). That is Gate 4's job.

## Gate 3 — artefacts and their hashes

- [ ] Built by `release.yml`, not from a laptop.
- [ ] Every artefact has a `.sha256` sidecar: `DCENT_Voice-Setup.exe`, the
      portable ZIP, the AppImage, the `.deb`, the `.dmg`, the macOS `.zip`.
- [ ] `dist/macos-pipeline-status.json` reviewed: if `signed` or `notarized` is
      false, the release notes must say so in plain words.
- [ ] Windows Authenticode signature verified, **or** the release notes state
      that the build is unsigned and link the SmartScreen section of
      `INSTALL_WINDOWS.md` with the SHA-256 to compare.

## Gate 4 — pristine-host evidence (blocking)

Run [`QA_FRESH_MACHINE.md`](QA_FRESH_MACHINE.md) on all three host classes.
Every one produces a `doctor` zip; commit all of them.

| # | Host | Proves |
|---|---|---|
| 1 | Windows Sandbox, **networking disabled** | AC9: it works with no network at all |
| 2 | A physical machine that has never had DCENT_Voice | AC1/AC3: real hardware, real user, real tray |
| 3 | A Windows 10 host with **WebView2 removed** | AC2/S3: dictation still works and the fallback explains itself |

Required contents of `docs/release-evidence/<version>/`:

- [ ] `sandbox-offline/doctor-*.zip` and a screenshot of the first-run surface.
- [ ] `physical/doctor-*.zip`, plus a screenshot showing dictated text landing
      in another application.
- [ ] `no-webview2/doctor-*.zip`, plus a screenshot of the native first-run
      dialog naming the missing runtime.
- [ ] `NOTES.md` — one file recording, per host: Windows build number, whether
      WebView2 was present, time from double-click to tray icon, and anything
      that surprised you. Write down what was wrong even if you fixed it.

A `doctor` zip containing a `fail` is not evidence of success. Either fix it or
record explicitly why it is acceptable for this release.

### Linux and macOS

CI covers the packaged fresh-profile launch on both. What CI cannot cover, and
what must therefore be done by hand at least once per **minor** release (not
every beta), on a real desktop:

- [ ] Linux, X11: install the `.deb`, run `dcent-voice doctor`, confirm
      `desktop.injection_tools` passes, dictate into a text editor.
- [ ] Linux, Wayland: same with the AppImage; confirm `desktop.uinput` and
      `desktop.webkitgtk` report honestly on that host.
- [ ] macOS: drag the `.app` to `/Applications`, launch, grant Microphone and
      Accessibility when prompted, confirm `desktop.accessibility` and
      `desktop.microphone` pass, dictate into TextEdit.
- [ ] macOS: confirm the login item points into `/Applications` after the move
      (`instance.autostart` passes) — the app rewrites it on every launch.

Store these under `docs/release-evidence/<version>/linux/` and `/macos/`.

## Gate 5 — publish

- [ ] Tag pushed; GitHub release created by `release.yml` with all artefacts and
      sidecars attached.
- [ ] Release notes state the signing status plainly and link
      `docs/INSTALL_WINDOWS.md`.
- [ ] `docs/INSTALL_WINDOWS.md` still matches what the installer actually shows
      — re-read it against this build's dialogs, not the last one's.
- [ ] README download links point at the new release.

## Gate 6 — after publishing

- [ ] Download the published Setup.exe on a machine that did not build it,
      verify its SHA-256 against the sidecar, and install it once.
- [ ] Only then send it to anyone outside the project. If they report a problem,
      the first question is "run Start Menu → DCENT_Voice Diagnostics and send
      me the zip" — that is what the whole `doctor` workstream is for.
