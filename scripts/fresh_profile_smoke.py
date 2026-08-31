# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Prove the app works on a machine that has never run it (AC1).

This is the regression guard for the fresh-machine root cause: the frozen exe
could not find ``config.example.toml``, so ``--print-config`` exited 2 with no
window, no log line and no dialog, and a first-time user saw nothing at all.

Two phases, both against the *frozen* artifact and never with ``--config``
(passing an explicit config path is exactly what hid the bug from the existing
smoke):

1. Seeding: an empty ``DCENT_VOICE_PROFILE_ROOT`` and a neutral working
   directory (``C:\\``), then ``--print-config``. Expect exit 0 and a freshly
   created ``config.toml`` inside the profile root.
2. Launch: start the app with the seeded config on a free port, wait for
   ``/health`` using the bearer token the app publishes into the profile root's
   ADE registry, drive a real ``/transcribe`` call so the bundled Parakeet
   weights must resolve, verify and load, then shut down and assert the log.

Usage::

    python scripts/fresh_profile_smoke.py --executable dist\\DCENT_Voice\\dcent-voice.exe --cwd C:\\
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from dcent_voice.util.owned_process import (  # noqa: E402
    start_owned_process,
    terminate_owned_process,
)

# Reproduce an Explorer double-click: no console for the child, and a process
# group of its own so a Ctrl+C here cannot reach the app under test.
_DETACHED_CREATION_FLAGS = (
    (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
)

#: Standard streams for the phase-2 launch. ``None`` means "inherit" to
#: subprocess, which is what Windows wants (no STARTF_USESTDHANDLES, and
#: DETACHED_PROCESS then denies the child a console) but is exactly wrong on
#: POSIX, where inheriting hands the child this harness's pipe.
_DETACHED_STDIO = None if os.name == "nt" else subprocess.DEVNULL


# Phase 2's argv. Named because assert_stdio_was_detached matches on it to pick
# the launch's boot line out of startup.log, which also holds phase 1's.
_LAUNCH_ARGS = ("--no-hotkeys", "--no-overlay")


class SmokeFailure(RuntimeError):
    """A fresh-profile expectation was not met."""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.executable.is_file():
        print(f"executable not found: {args.executable}", file=sys.stderr)
        return 2

    if args.profile_root is not None:
        profile_root = args.profile_root.resolve()
        profile_root.mkdir(parents=True, exist_ok=True)
        return _run(args, profile_root)
    with tempfile.TemporaryDirectory(prefix="dcent_voice_fresh_") as tmp:
        return _run(args, Path(tmp))


def _run(args: argparse.Namespace, profile_root: Path) -> int:
    executable = args.executable.resolve()
    workdir = _neutral_cwd(args.cwd)
    env = smoke_env(profile_root)
    print(f"profile root : {profile_root}")
    print(f"executable   : {executable}")
    print(f"working dir  : {workdir}")
    try:
        config_path = phase_seed(executable, workdir, env, profile_root, timeout_s=args.timeout)
        if args.seed_only:
            print("PASS (seeding only)")
            return 0
        phase_launch(
            executable,
            workdir,
            env,
            config_path,
            port=args.port or find_free_port(),
            timeout_s=args.launch_timeout,
        )
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS")
    return 0


# --- phase 1: config seeding -------------------------------------------------


def phase_seed(
    executable: Path,
    workdir: Path,
    env: dict[str, str],
    profile_root: Path,
    *,
    timeout_s: float,
) -> Path:
    """Run ``--print-config`` from a neutral cwd and require it to seed."""
    print("\n[1/2] fresh-profile config seeding")
    config_path = expected_config_path(profile_root)
    if config_path.exists():
        raise SmokeFailure(f"profile root is not fresh; {config_path} already exists")

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(executable), "--print-config"],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    print(f"  exit={completed.returncode}")
    for line in completed.stdout.splitlines():
        print(f"  stdout: {line}")
    for line in completed.stderr.splitlines():
        print(f"  stderr: {line}")
    if completed.returncode != 0:
        raise SmokeFailure(
            f"--print-config exited {completed.returncode} on a fresh profile "
            "(this is the fresh-machine bug)"
        )
    if not config_path.is_file():
        raise SmokeFailure(f"--print-config did not create {config_path}")
    print(f"  created: {config_path}")
    return config_path


def expected_config_path(profile_root: Path) -> Path:
    """Where the app must put the seeded config for this profile root."""
    previous = os.environ.get("DCENT_VOICE_PROFILE_ROOT")
    os.environ["DCENT_VOICE_PROFILE_ROOT"] = str(profile_root)
    try:
        from dcent_voice.config import default_config_path

        return default_config_path()
    finally:
        if previous is None:
            os.environ.pop("DCENT_VOICE_PROFILE_ROOT", None)
        else:
            os.environ["DCENT_VOICE_PROFILE_ROOT"] = previous


def registry_dir_for(profile_root: Path) -> Path:
    previous = os.environ.get("DCENT_VOICE_PROFILE_ROOT")
    os.environ["DCENT_VOICE_PROFILE_ROOT"] = str(profile_root)
    try:
        from dcent_voice.attach.registry import default_registry_dir

        return default_registry_dir()
    finally:
        if previous is None:
            os.environ.pop("DCENT_VOICE_PROFILE_ROOT", None)
        else:
            os.environ["DCENT_VOICE_PROFILE_ROOT"] = previous


# --- phase 2: launch ---------------------------------------------------------


def phase_launch(
    executable: Path,
    workdir: Path,
    env: dict[str, str],
    config_path: Path,
    *,
    port: int,
    timeout_s: float,
) -> None:
    """Launch the seeded install and prove the loopback service and ASR work."""
    print("\n[2/2] launch on the seeded profile")
    profile_root = Path(env["DCENT_VOICE_PROFILE_ROOT"])
    # Retarget only the loopback port. Everything else stays exactly as the app
    # seeded it, so this exercises the real shipped defaults (desktop/Parakeet).
    set_service_port(config_path, port)
    print(f"  port: {port}")

    process = start_owned_process(
        # Never --config: resolving the user config from the profile root is
        # precisely what this smoke exists to verify.
        [str(executable), *_LAUNCH_ARGS],
        cwd=str(workdir),
        env=env,
        # Launch it the way a desktop launcher does, which differs by OS.
        #
        # Windows: leave all three streams at None so Python does not set
        # STARTF_USESTDHANDLES, and let DETACHED_PROCESS deny the child a
        # console. GetStdHandle then returns NULL in the child — the same
        # condition Explorer produces, and the one that makes sys.stdout None.
        # A piped stdout would hand a windowed (console=False) build a usable
        # sys.stdout that a real double-click never has, hiding every crash
        # that only happens when sys.stdout is None.
        #
        # POSIX: there is no equivalent of "no stdout" — a .desktop launch
        # still gives the process descriptors. The honest analogue is /dev/null
        # in a session of its own, and start_owned_process already requests
        # start_new_session=True. Passing None here would mean *inherit*, which
        # hands the child this harness's own pipe and quietly tests the easy
        # path (and, on CI, keeps the job's stdout open).
        stdin=_DETACHED_STDIO,
        stdout=_DETACHED_STDIO,
        stderr=_DETACHED_STDIO,
        creationflags=_DETACHED_CREATION_FLAGS,
    )
    try:
        # Before trusting anything else this phase reports, prove the launch
        # really was double-click-shaped (see assert_stdio_was_detached).
        assert_stdio_was_detached(profile_root, timeout_s=min(timeout_s, 60.0))
        registry_dir = registry_dir_for(profile_root)
        token = wait_for_token(registry_dir / "dcent-voice.token", timeout_s=timeout_s)
        if not token:
            print_profile_diagnostics(profile_root)
            raise SmokeFailure(
                f"the app never published a session token to {registry_dir} (exit={process.poll()})"
            )
        # A published registry entry proves the loopback service really bound,
        # rather than the token file merely having been written first.
        assert_registry_entry_published(registry_dir, port=port, timeout_s=30.0)
        health = wait_for_health(port, token=token, timeout_s=timeout_s)
        if health is None:
            print_profile_diagnostics(profile_root)
            raise SmokeFailure(
                f"127.0.0.1:{port}/health never became ready (exit={process.poll()})"
            )
        print(f"  health.ok={health.get('ok')} model_loaded={health.get('model_loaded')}")
        assert_asr_ready(port, token=token, health=health, profile_root=profile_root)
    finally:
        terminate_owned_process(process, grace_s=15.0, kill_s=10.0)
        print(f"  shutdown exit={process.poll()}")
    assert_logs_exist(profile_root)
    assert_parakeet_was_loaded(profile_root)


def assert_stdio_was_detached(profile_root: Path, *, timeout_s: float) -> None:
    """Prove the child was launched the way the platform's desktop launcher does.

    On Windows, ``Popen(stdout=None, creationflags=DETACHED_PROCESS)`` is the
    right way to ask for a double-click-shaped launch, but it is not a hard
    guarantee: what the child ends up with depends on the parent's own handles
    and their inheritability. If it ever silently degrades to a usable stdout,
    this phase would go back to exercising the easy path and would stop catching
    the whole class of bug that only appears when ``sys.stdout is None``
    (uvicorn's ``ColourizedFormatter`` calling ``isatty()``, a
    ``StreamHandler`` built over a ``None`` stream). ``util.bootlog`` records the
    real state on its ``boot`` line, so assert on the app's own observation
    rather than on our intent.

    ``sys.stdout is None`` is a Windows windowed-build condition with no POSIX
    equivalent: a ``.desktop`` launch still hands the process descriptors, and
    ``stream_state`` answers "can the app write anywhere", so it reports
    ``handle`` there however the process was started. On POSIX this therefore
    asserts what is actually provable — that the detached launch ran and wrote
    its own boot line — while the launch itself points the streams at
    ``/dev/null`` in a new session so nothing is inherited from this harness.
    """
    deadline = time.monotonic() + timeout_s
    boot_line = ""
    saw_any_boot_line = False
    while time.monotonic() < deadline:
        for path in sorted(profile_root.rglob("startup.log")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "boot " not in line:
                    continue
                saw_any_boot_line = True
                # Phase 1 runs --print-config with pipes and legitimately logs
                # stdout=handle, so match this phase's launch argv rather than
                # whichever boot line happens to come first.
                if _LAUNCH_ARGS[0] in line and "stdout=" in line:
                    boot_line = line
        if boot_line:
            break
        time.sleep(0.2)

    if not boot_line:
        detail = (
            "its boot line carries no 'stdout=' field"
            if saw_any_boot_line
            else "no boot line was written at all"
        )
        raise SmokeFailure(
            f"startup.log under {profile_root} did not record the stdio state ({detail}), so this "
            "run cannot prove it launched the app the way a double-click does. util.bootlog must "
            "log 'stdout=none|handle' on its boot line; without it a silently inherited stdout "
            "would make this whole phase test the easy path."
        )
    if "stdout=none" not in boot_line:
        if os.name == "nt":
            raise SmokeFailure(
                "the app was launched with a usable stdout, which a real double-click never has, "
                "so this run proves nothing about the windowed build. DETACHED_PROCESS did not "
                f"deny the child a console here. boot line:\n{boot_line.strip()}"
            )
        # POSIX: see the docstring. The streams point at /dev/null in a new
        # session, which is the closest thing to a double-click this OS has.
        print("  stdio: detached session, streams on /dev/null (no POSIX 'no stdout' state)")
        return
    print("  stdio: stdout=none (launched as a double-click, not as a console child)")


def assert_registry_entry_published(registry_dir: Path, *, port: int, timeout_s: float) -> None:
    """Require the ADE registry entry the app writes once the service is bound."""
    entry_path = registry_dir / "dcent-voice.json"
    deadline = time.monotonic() + timeout_s
    entry: dict | None = None
    while time.monotonic() < deadline:
        try:
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
            break
        except (OSError, json.JSONDecodeError):
            time.sleep(0.2)
    if entry is None:
        raise SmokeFailure(f"the app never published an ADE registry entry at {entry_path}")
    endpoint = str(entry.get("endpoint", ""))
    if f":{port}" not in endpoint:
        raise SmokeFailure(f"registry entry advertises {endpoint!r}, not the requested port {port}")
    token_ref = Path(str(entry.get("tokenRef", "")))
    if not token_ref.is_file():
        raise SmokeFailure(f"registry entry points at a missing token file: {token_ref}")
    if registry_dir not in token_ref.parents:
        raise SmokeFailure(
            f"the session token escaped the profile root: {token_ref} is not under {registry_dir}"
        )
    print(f"  registry: {entry_path.name} endpoint={endpoint} token={token_ref.name}")


def print_profile_diagnostics(profile_root: Path, *, tail_lines: int = 60) -> None:
    """Report what the app itself recorded; there are no pipes to read now."""
    print("--- profile diagnostics ---", file=sys.stderr)
    found = False
    for name in ("dcent_voice.log", "startup.log", "dcent_voice_fault.log"):
        for path in sorted(profile_root.rglob(name)):
            found = True
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            print(f"--- {path} (last {tail_lines} lines) ---", file=sys.stderr)
            print("\n".join(lines[-tail_lines:]), file=sys.stderr)
    for path in sorted(profile_root.rglob("last-startup-failure.json")):
        found = True
        print(f"--- {path} ---", file=sys.stderr)
        print(path.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
    if not found:
        print(
            f"no log files exist under {profile_root} at all — the app died before it could "
            "write one (see WS2: nothing may fail silently)",
            file=sys.stderr,
        )


def assert_asr_ready(port: int, *, token: str, health: dict, profile_root: Path) -> None:
    """Prove the shipped Parakeet model actually transcribes on a fresh profile.

    ``/health``'s ``model_loaded`` flag is deliberately not the gate. A runtime
    config reload (the app persists its own first-run flag) can swap in a fresh,
    not-yet-loaded provider, so the flag goes stale while the app is perfectly
    healthy. Driving a real ``/transcribe`` call is the honest check: it forces
    the load and fails if the bundled weights cannot be resolved or verified.
    """
    asr = (health.get("subsystems") or {}).get("asr") or {}
    if asr.get("ok") is False:
        raise SmokeFailure(f"ASR subsystem reported not ok: {asr}")
    # Parakeet exposes no runtime_status, so the provider class name is the
    # identity the health payload carries for it.
    identity = f"{asr.get('provider', '')} {asr.get('model', '')}".lower()
    if "parakeet" not in identity:
        raise SmokeFailure(f"expected the bundled Parakeet model, got {asr}")
    print(f"  asr: {asr.get('provider')} status={asr.get('status')}")

    response = httpx.post(
        f"http://127.0.0.1:{port}/transcribe",
        json={"audio": [0.0] * 1600, "samplerate": 16000, "cleanup": False},
        headers={"Authorization": f"Bearer {token}"},
        timeout=180.0,
    )
    if response.status_code != 200:
        print_profile_diagnostics(profile_root)
        raise SmokeFailure(f"/transcribe returned HTTP {response.status_code}: {response.text}")
    body = response.json()
    if "raw" not in body:
        raise SmokeFailure(f"unexpected /transcribe result: {body}")
    print("  transcribe ok (bundled Parakeet weights resolved, verified and loaded)")

    after = httpx.get(
        f"http://127.0.0.1:{port}/health",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    ).json()
    if not after.get("model_loaded"):
        raise SmokeFailure(
            "the ASR model is still not loaded after a successful transcription: "
            f"{json.dumps(after.get('subsystems'), default=str)}"
        )
    print(f"  health.model_loaded={after.get('model_loaded')} after transcription")


def assert_parakeet_was_loaded(profile_root: Path) -> None:
    """The log must show the bundled snapshot being loaded, not a fallback."""
    logs = sorted(profile_root.rglob("dcent_voice.log"))
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in logs)
    if "parakeet ready" not in text:
        raise SmokeFailure(
            "the log never recorded a successful Parakeet load; the app may have silently "
            f"fallen back:\n{text[-2000:]}"
        )
    if "verified Parakeet unavailable" in text:
        raise SmokeFailure("the app fell back to Faster Whisper: the bundled Parakeet was rejected")
    for line in text.splitlines():
        if "loading verified Parakeet weights" in line:
            print(f"  {line.strip()}")


def assert_logs_exist(profile_root: Path) -> None:
    logs = sorted(profile_root.rglob("dcent_voice.log"))
    if not logs:
        raise SmokeFailure(f"no log file was written under {profile_root}")
    print(f"  log: {logs[0]} ({logs[0].stat().st_size} bytes)")


def set_service_port(config_path: Path, port: int) -> None:
    text = config_path.read_text(encoding="utf-8")
    patched, count = re.subn(r"(?m)^port\s*=\s*\d+\s*$", f"port = {port}", text, count=1)
    if count != 1:
        raise SmokeFailure(f"could not retarget the service port in {config_path}")
    config_path.write_text(patched, encoding="utf-8")


# --- helpers -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a frozen DCENT_Voice build on a profile that has never run it."
    )
    parser.add_argument(
        "--executable",
        type=Path,
        required=True,
        help=r"Frozen executable, e.g. dist\DCENT_Voice\dcent-voice.exe",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help=r"Neutral working directory to launch from (default: C:\ or /).",
    )
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=None,
        help="Profile root to use. Default: a fresh temporary directory that is removed after.",
    )
    parser.add_argument("--port", type=int, default=0, help="Loopback port (default: a free one).")
    parser.add_argument("--timeout", type=float, default=120.0, help="--print-config timeout.")
    parser.add_argument(
        "--launch-timeout",
        type=float,
        default=240.0,
        help="Seconds to wait for /health; the first model load hashes ~670 MB.",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Run phase 1 only (no launch). Useful for a quick regression check.",
    )
    return parser


def smoke_env(profile_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["DCENT_VOICE_PROFILE_ROOT"] = str(profile_root)
    # Never let a smoke run rewrite the real OS login item.
    env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
    # Isolate the single-instance mutex so this can run beside a real instance;
    # single_instance.py only honours this exact prefix.
    env["DCENT_VOICE_SMOKE_MUTEX"] = f"Local\\DCENT_Voice_Smoke_{secrets.token_hex(12)}"
    # WS2 turns every startup failure into a modal dialog. Unattended runs must
    # still get the log line, the JSON record and the non-zero exit — but a
    # MessageBox nobody can dismiss would hang the smoke until its timeout.
    env["DCENT_VOICE_NO_DIALOGS"] = "1"
    # A stale model-dir override from the developer's shell would defeat the
    # point of testing the shipped payload.
    env.pop("DCENT_VOICE_MODEL_DIR", None)
    return env


def _neutral_cwd(requested: Path | None) -> Path:
    if requested is not None:
        return requested
    return Path("C:\\") if os.name == "nt" else Path("/")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_token(token_path: Path, *, timeout_s: float) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
        time.sleep(0.2)
    return None


def wait_for_health(port: int, *, token: str, timeout_s: float) -> dict | None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    headers = {"Authorization": f"Bearer {token}"}
    last: dict | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, headers=headers, timeout=2.0)
        except Exception:
            time.sleep(0.25)
            continue
        if response.status_code == 200:
            last = response.json()
            if last.get("ok"):
                return last
        time.sleep(0.5)
    return last


if __name__ == "__main__":
    raise SystemExit(main())
