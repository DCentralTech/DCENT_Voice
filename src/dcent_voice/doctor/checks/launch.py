# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Actually start the app on a throwaway profile and time how long it takes.

Every other check is an inference. This one is the experiment: seed a config in
an empty ``DCENT_VOICE_PROFILE_ROOT`` from a neutral working directory, launch
the app on a free port with an isolated single-instance mutex, wait for
``/health`` with the bearer token the app publishes, then shut it down. It is
the same protocol as ``scripts/fresh_profile_smoke.py``, reimplemented inside
the package so the frozen executable carries it — a user with a broken install
has no repository to run scripts from.

The user's real profile, tray icon, autostart entry and running instance are all
untouched: separate profile root, ``--no-tray --no-hotkeys --no-overlay``,
``DCENT_VOICE_DISABLE_AUTOSTART=1`` and a smoke-scoped mutex name.
"""

from __future__ import annotations

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
from typing import Any

from dcent_voice.util import paths

from ..result import FAIL, PASS, WARN, CheckResult

SEED_TIMEOUT_S = 180.0
READY_TIMEOUT_S = 300.0


def run(*, enabled: bool = True, timeout_s: float = READY_TIMEOUT_S) -> list[CheckResult]:
    if not enabled:
        return [
            CheckResult(
                "launch.fresh_profile",
                PASS,
                "skipped: --no-launch-checks was requested, so the app was not started",
            )
        ]
    return [check_fresh_profile_launch(timeout_s=timeout_s)]


def check_fresh_profile_launch(
    *, timeout_s: float = READY_TIMEOUT_S, attempts: int = 2
) -> CheckResult:
    """Run the trial launch, retrying once when the chosen port was stolen.

    ``_free_port()`` asks the OS for a free port and closes it again, so there
    is an unavoidable gap before the child binds it. Losing that race looks
    exactly like a real bind failure, so rather than report a failure we did not
    observe, try once more with a fresh port and report the second result.
    """
    last: CheckResult | None = None
    for attempt in range(max(1, attempts)):
        with tempfile.TemporaryDirectory(prefix="dcent_voice_doctor_") as tmp:
            profile_root = Path(tmp)
            try:
                last = _launch(profile_root, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001 - this check must never crash doctor
                return CheckResult(
                    "launch.fresh_profile",
                    WARN,
                    f"the launch check could not run: {type(exc).__name__}: {exc}",
                    "Re-run doctor, or run it with --no-launch-checks and send the rest of "
                    "the report.",
                    {"profileRoot": str(profile_root)},
                )
            if last.status == PASS or not _looks_like_port_collision(last):
                return last
            if attempt + 1 < attempts:
                continue
    assert last is not None
    return last


def _looks_like_port_collision(result: CheckResult) -> bool:
    """True when the trial instance failed in a way a stolen port would explain."""
    logs = result.data.get("childLogs")
    haystack = json.dumps(logs, default=str).casefold() if logs else ""
    return any(
        marker in haystack
        for marker in ("address already in use", "only one usage of each socket address")
    )


def launch_argv() -> list[str]:
    """How to start this application, frozen or from source."""
    executable = sys.executable or ""
    if paths.is_frozen():
        return [executable]
    return [executable, "-m", "dcent_voice"]


def launch_env(profile_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["DCENT_VOICE_PROFILE_ROOT"] = str(profile_root)
    # Never rewrite the real login item, never show a dialog, never contend for
    # the production single-instance mutex.
    env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
    env["DCENT_VOICE_NO_DIALOGS"] = "1"
    env["DCENT_VOICE_SMOKE_MUTEX"] = f"Local\\DCENT_Voice_Smoke_{secrets.token_hex(12)}"
    # A developer's model-dir override would defeat the point of testing the
    # shipped payload.
    env.pop("DCENT_VOICE_MODEL_DIR", None)
    return env


def _launch(profile_root: Path, *, timeout_s: float) -> CheckResult:
    from dcent_voice.util.owned_process import start_owned_process, terminate_owned_process

    argv = launch_argv()
    env = launch_env(profile_root)
    workdir = _neutral_cwd()
    data: dict[str, Any] = {
        "argv": argv,
        "profileRoot": str(profile_root),
        "workingDirectory": str(workdir),
    }

    seeded, seed_detail, config_path = _seed_config(argv, env, workdir)
    data["seed"] = seed_detail
    data["configPath"] = str(config_path) if config_path else ""
    if not seeded:
        return CheckResult(
            "launch.fresh_profile",
            FAIL,
            f"the app could not create a configuration on a profile that has never run it: "
            f"{seed_detail}. This is exactly what a first-time user hits: the process exits "
            "and no window ever appears.",
            "Reinstall DCENT_Voice from the official Setup.exe. If it persists, send this "
            "diagnostics zip: the bundled config.example.toml is missing or unreadable "
            "(see env.install).",
            data,
        )

    port = _free_port()
    data["port"] = port
    assert config_path is not None
    if not _retarget_port(config_path, port):
        return CheckResult(
            "launch.fresh_profile",
            WARN,
            f"the seeded configuration at {config_path} has no [service] port to retarget, so "
            "the launch check was skipped rather than risk colliding with a running instance.",
            "No action needed unless you edited config.example.toml.",
            data,
        )

    started = time.monotonic()
    process = start_owned_process(
        [*argv, "--no-hotkeys", "--no-overlay", "--no-tray"],
        cwd=str(workdir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        token = _wait_for_token(profile_root, timeout_s=timeout_s, process=process)
        data["tokenPublished"] = bool(token)
        if not token:
            data["exitCode"] = process.poll()
            return CheckResult(
                "launch.fresh_profile",
                FAIL,
                "the app started but never published its loopback session token "
                f"(exit code {process.poll()!r} after {time.monotonic() - started:.1f} s).",
                "Check history.logs and history.last_startup_failure in this report: the "
                "startup log from this trial run is under the temporary profile and its tail "
                "is included below.",
                _with_child_logs(data, profile_root),
            )
        health = _wait_for_health(port, token, timeout_s=timeout_s, process=process)
        elapsed = time.monotonic() - started
        data["timeToReadySeconds"] = round(elapsed, 2)
        if health is None:
            data["exitCode"] = process.poll()
            return CheckResult(
                "launch.fresh_profile",
                FAIL,
                f"the loopback API at 127.0.0.1:{port}/health never became ready within "
                f"{timeout_s:.0f} s (exit code {process.poll()!r}).",
                "A very slow disk or an antivirus scanning the 670 MB model on first read can "
                "exceed this. Re-run doctor once; if it fails again, send the zip.",
                _with_child_logs(data, profile_root),
            )
        asr = (health.get("subsystems") or {}).get("asr") or {}
        data["health"] = {
            "ok": health.get("ok"),
            "modelLoaded": health.get("model_loaded"),
            "asr": asr,
        }
        if asr.get("ok") is False:
            return CheckResult(
                "launch.fresh_profile",
                WARN,
                f"the app started and answered /health in {elapsed:.1f} s, but the ASR "
                f"subsystem reported a problem: {asr}",
                "See config.asr_model and payload.models in this report.",
                data,
            )
        return CheckResult(
            "launch.fresh_profile",
            PASS,
            f"a fresh profile launch succeeded: config seeded, service ready in {elapsed:.1f} s, "
            f"ASR provider {asr.get('provider') or 'unknown'} "
            f"status {asr.get('status') or 'unknown'}",
            data=data,
        )
    finally:
        terminate_owned_process(process, grace_s=15.0, kill_s=10.0)
        data["shutdownExitCode"] = process.poll()


def _seed_config(
    argv: list[str], env: dict[str, str], workdir: Path
) -> tuple[bool, str, Path | None]:
    """Run ``--print-config`` from a neutral cwd; it must create the config."""
    config_path = _expected_config_path(Path(env["DCENT_VOICE_PROFILE_ROOT"]))
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*argv, "--print-config"],
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=SEED_TIMEOUT_S,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return False, f"--print-config did not finish within {SEED_TIMEOUT_S:.0f} s", None
    except OSError as exc:
        return False, f"the executable could not be started: {exc}", None
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = tail[-1][:400] if tail else "no output"
        return False, f"--print-config exited {completed.returncode}: {message}", None
    if not config_path.is_file():
        return False, f"--print-config exited 0 but did not create {config_path}", None
    return True, f"created {config_path}", config_path


def _expected_config_path(profile_root: Path) -> Path:
    """Ask the real resolver where the child will put its config."""
    previous = os.environ.get(paths.PROFILE_ROOT_ENV)
    os.environ[paths.PROFILE_ROOT_ENV] = str(profile_root)
    try:
        from dcent_voice.config import default_config_path

        return default_config_path()
    finally:
        if previous is None:
            os.environ.pop(paths.PROFILE_ROOT_ENV, None)
        else:
            os.environ[paths.PROFILE_ROOT_ENV] = previous


def _registry_dir(profile_root: Path) -> Path:
    previous = os.environ.get(paths.PROFILE_ROOT_ENV)
    os.environ[paths.PROFILE_ROOT_ENV] = str(profile_root)
    try:
        from dcent_voice.attach.registry import default_registry_dir

        return default_registry_dir()
    finally:
        if previous is None:
            os.environ.pop(paths.PROFILE_ROOT_ENV, None)
        else:
            os.environ[paths.PROFILE_ROOT_ENV] = previous


def _retarget_port(config_path: Path, port: int) -> bool:
    """Point the trial instance's loopback service at ``port``.

    The rewrite is anchored to the ``[service]`` table. An unanchored
    ``^port =`` matches the first such key anywhere in the file, so a ``port``
    under an earlier section would absorb the edit and leave ``[service]`` on
    8765 — the trial instance would then collide with the real one it is meant
    to run beside, and the check would report a failure caused by itself.

    The app reads its port from the config only; ``DCENT_VOICE_SERVICE_PORT``
    exists solely in the ADE bundle descriptor and is not consulted at startup,
    so editing the seeded config is the available mechanism.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False

    header = re.search(r"(?m)^\[service\]\s*$", text)
    if header is None:
        return False
    head, tail = text[: header.end()], text[header.end() :]
    # Stop at the next table header so only keys inside [service] are touched.
    following = re.search(r"(?m)^\[", tail)
    if following is None:
        body, rest = tail, ""
    else:
        body, rest = tail[: following.start()], tail[following.start() :]

    # ``[^\S\n]*`` is horizontal whitespace only: a plain ``\s*$`` would consume
    # the line's own newline (and any blank lines after it), welding the next
    # table header onto the rewritten value.
    patched, count = re.subn(
        r"(?m)^port[^\S\n]*=[^\S\n]*\d+[^\S\n]*$", f"port = {port}", body, count=1
    )
    if count != 1:
        return False
    try:
        config_path.write_text(head + patched + rest, encoding="utf-8")
    except OSError:
        return False
    return True


def _wait_for_token(
    profile_root: Path, *, timeout_s: float, process: subprocess.Popen
) -> str | None:
    token_path = _registry_dir(profile_root) / "dcent-voice.token"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return None
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
        time.sleep(0.2)
    return None


def _wait_for_health(
    port: int, token: str, *, timeout_s: float, process: subprocess.Popen
) -> dict[str, Any] | None:
    """Poll the trial instance's loopback ``/health``, carrying its bearer token.

    ``trust_env=False`` is load-bearing, not stylistic: the module-level
    ``httpx.get`` honours ``HTTP_PROXY``/``ALL_PROXY`` from the environment, so
    on a machine with a proxy configured this request — and the session token in
    its ``Authorization`` header — would be sent to that proxy instead of to
    127.0.0.1. Redirects are refused for the same reason: a 3xx must not be able
    to replay the header at another origin. Every other HTTP call site in the
    repo takes the same posture.
    """
    import httpx

    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=2.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return None
            try:
                response = client.get(url, headers=headers)
            except Exception:  # noqa: BLE001 - the service is simply not up yet
                time.sleep(0.25)
                continue
            if response.status_code == 200:
                payload = response.json()
                if payload.get("ok"):
                    return payload
            time.sleep(0.5)
    return None


def _with_child_logs(data: dict[str, Any], profile_root: Path) -> dict[str, Any]:
    """Attach the trial run's own logs; the temp profile is deleted right after."""
    from .history import TAIL_LINES, tail_lines

    previous = os.environ.get(paths.PROFILE_ROOT_ENV)
    os.environ[paths.PROFILE_ROOT_ENV] = str(profile_root)
    try:
        logs = paths.user_config_dir() / "logs"
    finally:
        if previous is None:
            os.environ.pop(paths.PROFILE_ROOT_ENV, None)
        else:
            os.environ[paths.PROFILE_ROOT_ENV] = previous
    collected: dict[str, Any] = {}
    for name in ("startup.log", "dcent_voice.log", "dcent_voice_fault.log"):
        path = logs / name
        if path.is_file():
            collected[name] = tail_lines(path, TAIL_LINES)
    data["childLogs"] = collected
    return data


def _neutral_cwd() -> Path:
    """A directory the app has no business depending on (the fresh-machine bug)."""
    root = Path("C:\\") if os.name == "nt" else Path("/")
    return root if root.is_dir() else Path.home()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
