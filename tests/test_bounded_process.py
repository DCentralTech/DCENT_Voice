# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_portable_bounded_runner_terminates_hung_process_clearly() -> None:
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_bounded.py",
            "--timeout",
            "0.1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 124
    assert time.monotonic() - started < 5
    assert "timed out after 0.1s and was terminated" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows installer timeout contract")
def test_powershell_installer_runner_terminates_hung_process_clearly() -> None:
    helper = Path("scripts/invoke_bounded.ps1").resolve()
    python = Path(sys.executable).resolve()
    command = (
        f". '{helper}'; try {{ "
        f"Invoke-DCENTBoundedProcess -FilePath '{python}' "
        "-Arguments @('-c', 'import time; time.sleep(30)') "
        "-Description 'Frozen payload verification' -TimeoutSeconds 1; exit 99 "
        "} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 124 }"
    )
    started = time.monotonic()
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 124
    assert time.monotonic() - started < 6
    assert "timed out after 1 seconds and was terminated" in result.stderr
