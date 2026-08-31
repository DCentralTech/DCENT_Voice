# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.attach.registry import default_registry_dir  # noqa: E402
from dcent_voice.util.owned_process import (  # noqa: E402
    start_owned_process,
    terminate_owned_process,
)

_ISOLATED_SOURCE_BOOTSTRAP = """
import os

import dcent_voice.app as app
from dcent_voice.attach.single_instance import SingleInstanceLock


def isolated_lock():
    return SingleInstanceLock(mutex_name=os.environ["DCENT_VOICE_SMOKE_MUTEX"])


app.SingleInstanceLock = isolated_lock
raise SystemExit(app.main())
"""


def main() -> int:
    args = build_parser().parse_args()
    port = args.port or find_free_port()
    with tempfile.TemporaryDirectory(prefix="dcent_voice_smoke_") as tmp:
        temp_root = Path(tmp)
        config_path = temp_root / "config.toml"
        config_path.write_text(
            smoke_config(port, packaged=args.executable is not None), encoding="utf-8"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        # Never let a smoke run rewrite the user's OS login item. The app
        # recognizes this test/automation-only opt-out from autostart syncing.
        env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
        local_app_data = temp_root / "localappdata"
        app_data = temp_root / "appdata"
        env["LOCALAPPDATA"] = str(local_app_data)
        env["APPDATA"] = str(app_data)
        env["DCENT_VOICE_SMOKE_MUTEX"] = f"Local\\DCENT_Voice_Smoke_{secrets.token_hex(12)}"
        registry_dir = local_app_data / "DCENT" / "modules"
        command = build_command(args.executable, config_path, isolate_source=args.isolate)
        process = start_owned_process(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = ProcessOutput(process)
        output.start()
        try:
            health = wait_for_health(port, timeout_s=args.timeout)
            if not health:
                print("service did not become healthy", file=sys.stderr)
                output.dump()
                return 1
            print(f"health ok on 127.0.0.1:{port}")
            print(health)
            token = wait_for_token(registry_dir=registry_dir, timeout_s=10.0)
            if not token:
                print("service token was not published to the registry", file=sys.stderr)
                output.dump()
                return 1
            command_result = post_json_or_fail(
                port,
                "/command",
                {"transcript": "what's 2+2"},
                output,
                token=token,
                registry_dir=registry_dir,
            )
            if command_result.get("text") != "4":
                print(f"unexpected command result: {command_result}", file=sys.stderr)
                output.dump()
                return 1
            transcribe_result = post_json_or_fail(
                port,
                "/transcribe",
                {"audio": [0.0] * 1600, "samplerate": 16000, "cleanup": False},
                output,
                token=token,
                registry_dir=registry_dir,
            )
            if "raw" not in transcribe_result:
                print(f"unexpected transcribe result: {transcribe_result}", file=sys.stderr)
                output.dump()
                return 1
            print("command ok")
            print("transcribe ok")
            return 0
        finally:
            terminate_owned_process(process, grace_s=5.0, kill_s=5.0)
            output.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start DCENT_Voice with a tiny local config and probe /health."
    )
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--executable",
        type=Path,
        default=None,
        help="Optional dcent-voice executable to smoke-test instead of python -m dcent_voice.",
    )
    parser.add_argument(
        "--isolate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=("patch source runs for an isolated mutex; enabled by default"),
    )
    return parser


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_command(
    executable: Path | None, config_path: Path, *, isolate_source: bool = False
) -> list[str]:
    common = [
        "--config",
        str(config_path),
        "--no-tray",
        "--no-hotkeys",
        "--no-overlay",
    ]
    if executable is not None:
        return [str(executable), *common]
    if isolate_source:
        return [sys.executable, "-c", _ISOLATED_SOURCE_BOOTSTRAP, *common]
    return [sys.executable, "-m", "dcent_voice", *common]


def smoke_config(port: int, *, packaged: bool = False) -> str:
    profile = "desktop" if packaged else "tiny"
    asr = "parakeet:tdt-0.6b-v3:int8" if packaged else "faster-whisper:tiny:int8"
    return f"""
active_profile = "{profile}"
language = "en"
cleanup_enabled = false
launch_at_startup = false

[hotkeys]
mode = "hold"
dictation = "ctrl+win"
command = "off"
streaming = "off"

[overlay]
enabled = false
lazy = true
position = "bottom-center"
reduced_motion = false

[service]
enabled = true
host = "127.0.0.1"
port = {port}

[injector]
default = "clipboard"
restore_clipboard = true

[privacy]
first_run_education_shown = true
consent_ledger_path = ""
egress_log_path = ""

[profile.{profile}]
asr = "{asr}"
llm = "none"
cleanup_enabled = false
"""


def wait_for_health(port: int, *, timeout_s: float) -> dict | None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=0.5)
            if response.status_code == 200:
                return response.json()
        except Exception:
            time.sleep(0.1)
    return None


def wait_for_token(*, registry_dir: Path | None = None, timeout_s: float) -> str | None:
    """Read the per-session bearer token the app publishes to the ADE registry."""
    token_path = (registry_dir or default_registry_dir()) / "dcent-voice.token"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
        time.sleep(0.1)
    return None


def post_json_or_fail(
    port: int,
    path: str,
    payload: dict,
    output: ProcessOutput,
    *,
    token: str,
    registry_dir: Path | None = None,
) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    response = httpx.post(
        url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=20.0
    )
    if response.status_code == 401:
        # A stale token file from a previous run may have been read before the
        # child overwrote it — re-read once and retry.
        time.sleep(0.5)
        fresh = wait_for_token(registry_dir=registry_dir, timeout_s=5.0)
        if fresh and fresh != token:
            response = httpx.post(
                url, json=payload, headers={"Authorization": f"Bearer {fresh}"}, timeout=20.0
            )
    if response.status_code >= 400:
        print(f"{path} failed with HTTP {response.status_code}: {response.text}", file=sys.stderr)
        output.dump()
        raise SystemExit(1)
    return response.json()


class ProcessOutput:
    def __init__(self, process: subprocess.Popen[str], *, max_lines: int = 240) -> None:
        self.process = process
        self.max_lines = max_lines
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self.process.stdout is not None:
            self._threads.append(
                threading.Thread(
                    target=self._drain,
                    args=(self.process.stdout, self._stdout_lines),
                    daemon=True,
                )
            )
        if self.process.stderr is not None:
            self._threads.append(
                threading.Thread(
                    target=self._drain,
                    args=(self.process.stderr, self._stderr_lines),
                    daemon=True,
                )
            )
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        for thread in self._threads:
            thread.join(timeout=1)

    def dump(self) -> None:
        if self._stdout_lines:
            print("--- child stdout ---", file=sys.stderr)
            print("".join(self._stdout_lines[-self.max_lines :]), file=sys.stderr)
        if self._stderr_lines:
            print("--- child stderr ---", file=sys.stderr)
            print("".join(self._stderr_lines[-self.max_lines :]), file=sys.stderr)

    def _drain(self, stream, target: list[str]) -> None:
        for line in stream:
            target.append(line)


if __name__ == "__main__":
    raise SystemExit(main())
