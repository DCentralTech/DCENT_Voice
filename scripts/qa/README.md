# QA helpers

Support files for the fresh-machine reliability gates (WS7 of
`docs/FRESH_MACHINE_IMPLEMENTATION_PLAN.md`). The human protocol lives in
[`docs/QA_FRESH_MACHINE.md`](../../docs/QA_FRESH_MACHINE.md).

| File | Purpose |
|---|---|
| `warm_model_cache.py` | Populate the Hugging Face hub cache with the pinned model snapshots so `actions/cache` actually spares CI the ~800 MB download. |
| `assert_windows_install.ps1` | Assert (or poll for the absence of) the three artefacts `DCENT_Voice-Setup.exe` creates: the Add/Remove Programs key, the Start Menu shortcut, and `%LOCALAPPDATA%\DCENT_Voice\dcent-voice.exe`. |
| `assert_doctor.py` | Judge a `doctor --json` report in CI: schema-validate it and fail on any `fail` outside an explicit `--allow-fail` list. See below. |
| `collect_diagnostics.sh` | Gather profile logs, the `$TMPDIR/DCENT_Voice` startup-failure fallback and XDG dirs into `build/qa-logs/` for one upload step (Linux/macOS; the Windows job does this inline). |
| `DCENT_Voice.wsb` | Windows Sandbox configuration **template** (the `<HostFolder>` is a placeholder). |
| `new_sandbox.ps1` | Render the template with this checkout's absolute `dist` path into `build\qa\DCENT_Voice.wsb`. |

## `doctor-enabled` — the CI gate (now live)

Six workflow steps (Windows/Linux/macOS in both `ci.yml` and `release.yml`) run
`dcent-voice doctor` and are guarded by

```yaml
if: hashFiles('scripts/qa/doctor-enabled') != ''
```

The guard is a repository file, not a runtime `Test-Path`, so the gate is
visible in the diff and cannot silently pass on a runner where the payload
happens to be missing. WS4 has created the marker, so **these steps are live**.
Deleting `scripts/qa/doctor-enabled` disables all six at once — do that only to
unblock an emergency, and restore it in the same PR.

## `assert_doctor.py` — why doctor's exit code is not the verdict

`doctor` exits 1 when any check fails, which is correct on a user's machine. CI
runners are not user machines: a headless macos-14 runner can never hold the
Accessibility TCC grant and has no microphone to authorize. So the workflows run
`doctor` unjudged and hand the report to `assert_doctor.py`, which

* validates it against `docs/schemas/doctor.schema.json` (so CI also gates the
  report contract), and
* fails on every `fail` **except** ids named in an explicit `--allow-fail`.

Current exceptions, and the only ones:

| Job | `--allow-fail` | Why |
|---|---|---|
| Windows (both workflows) | *none* | A missing audio device is already `warn`. |
| Linux (both workflows) | *none* | `xclip` + `xdotool` are installed so `desktop.injection_tools` passes under xvfb, whose session reads as x11. |
| macOS (both workflows) | `desktop.accessibility`, `desktop.microphone` | Un-grantable on a headless runner. |

The script prints a note when an allowed id did *not* fail, so an exception that
has become unnecessary surfaces instead of quietly accumulating. Never add an
`--allow-fail` to silence a real defect; fix the check or the build.
