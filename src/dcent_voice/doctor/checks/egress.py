# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Prove — not assert — that starting up and loading a model reaches no network.

Users report "it made an internet connection" when they run an unsigned 800 MB
download, because SmartScreen looks it up. That is the OS, not this app. Rather
than asking anyone to take that on faith, doctor wraps the socket layer, does
the single most network-suspicious thing the app ever does at startup (resolve,
verify and load the speech model), idles briefly, and lists every non-loopback
connection attempt. The expected answer is: none.

The monitor is a wrapper, not a block: it records and forwards. A cloud profile
is *supposed* to reach out, so the model load is skipped there and the finding
says so instead of manufacturing a scary result.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import threading
import time
from typing import Any

from ..result import FAIL, PASS, WARN, CheckResult

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

#: The socket patches are process-wide, so only one monitor may own them.
_MONITOR_GUARD = threading.Lock()
_ACTIVE_MONITOR: SocketMonitor | None = None


class SocketMonitor:
    """Record every outbound connection attempt and name lookup while installed.

    The patches are process-wide, so two monitors installed at once would have
    the second capture the first's wrappers as "the originals" and restore those
    on exit, leaving the process permanently instrumented. A module-level guard
    makes that impossible: the second install raises instead.

    Scope, stated plainly because an egress proof is only worth what it covers:
    this observes TCP connects and DNS resolutions made through Python's socket
    module. It does not see UDP sendto, raw sockets, or traffic from a native
    library that bypasses Python's socket layer.
    """

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._saved: dict[str, Any] = {}
        #: True when another monitor already owned the patches at install time.
        self._nested = False

    def __enter__(self) -> SocketMonitor:
        return self.install()

    def __exit__(self, *_exc: object) -> None:
        self.restore()

    def install(self) -> SocketMonitor:
        """Patch the socket layer and return the monitor that owns the patches.

        A nested install is a no-op that hands back the *outer* monitor. Without
        that, the second monitor would capture the first one's wrappers as "the
        originals" and restore those on exit, leaving the process permanently
        instrumented by a monitor nobody holds a reference to.
        """
        global _ACTIVE_MONITOR

        with _MONITOR_GUARD:
            if _ACTIVE_MONITOR is not None:
                self._nested = True
                return _ACTIVE_MONITOR
            self._saved = {
                "connect": socket.socket.connect,
                "connect_ex": socket.socket.connect_ex,
                "create_connection": socket.create_connection,
                "getaddrinfo": socket.getaddrinfo,
            }
            monitor = self

            def connect(sock: socket.socket, address: Any):  # type: ignore[no-untyped-def]
                monitor.record(address, "socket.connect")
                return monitor._saved["connect"](sock, address)

            def connect_ex(sock: socket.socket, address: Any):  # type: ignore[no-untyped-def]
                monitor.record(address, "socket.connect_ex")
                return monitor._saved["connect_ex"](sock, address)

            def create_connection(address: Any, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                monitor.record(address, "socket.create_connection")
                return monitor._saved["create_connection"](address, *args, **kwargs)

            def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                # Resolving a remote name is already a network question, and it
                # happens before any connect — so it catches an attempt that
                # fails at DNS and would otherwise leave no trace here.
                monitor.record((host, port), "socket.getaddrinfo", kind="resolve")
                return monitor._saved["getaddrinfo"](host, port, *args, **kwargs)

            socket.socket.connect = connect  # type: ignore[method-assign]
            socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
            socket.create_connection = create_connection  # type: ignore[assignment]
            socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
            _ACTIVE_MONITOR = self
            return self

    def restore(self) -> None:
        """Unpatch, but only from the monitor that actually installed."""
        global _ACTIVE_MONITOR

        with _MONITOR_GUARD:
            if self._nested or not self._saved:
                self._nested = False
                return
            socket.socket.connect = self._saved["connect"]  # type: ignore[method-assign]
            socket.socket.connect_ex = self._saved["connect_ex"]  # type: ignore[method-assign]
            socket.create_connection = self._saved["create_connection"]  # type: ignore[assignment]
            socket.getaddrinfo = self._saved["getaddrinfo"]  # type: ignore[assignment]
            self._saved = {}
            _ACTIVE_MONITOR = None

    def record(self, address: Any, api: str, *, kind: str = "connect") -> None:
        host, port = _split_address(address)
        entry = {
            "host": host,
            "port": port,
            "api": api,
            "kind": kind,
            "loopback": is_loopback(host),
            "at": time.time(),
        }
        with self._lock:
            self.attempts.append(entry)

    @property
    def remote_attempts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [entry for entry in self.attempts if not entry["loopback"]]


def _split_address(address: Any) -> tuple[str, int]:
    """Normalize every address form ``connect`` accepts into (host, port)."""
    if isinstance(address, tuple) and address:
        host = address[0]
        port = address[1] if len(address) > 1 and isinstance(address[1], int) else 0
        return (str(host) if host is not None else ""), port
    if isinstance(address, str | bytes):
        # AF_UNIX / AF_PIPE path: local by construction.
        return "", 0
    return str(address), 0


def is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1, unix sockets and the localhost names."""
    if not host:
        # An AF_UNIX path or an unparseable address never leaves the machine.
        return True
    lowered = host.strip("[]").casefold()
    if lowered in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def run(*, load_timeout_s: float = 120.0, idle_s: float = 2.0) -> list[CheckResult]:
    return [check_egress(load_timeout_s=load_timeout_s, idle_s=idle_s)]


def check_egress(*, load_timeout_s: float = 120.0, idle_s: float = 2.0) -> CheckResult:
    spec_raw = ""
    load_note = ""
    monitor = SocketMonitor()
    with monitor:
        try:
            spec, cloud = _active_asr_spec()
            spec_raw = spec.raw if spec is not None else ""
        except Exception as exc:  # noqa: BLE001 - a broken config is reported elsewhere
            spec, cloud = None, False
            load_note = f"the active ASR spec could not be read ({type(exc).__name__}: {exc})"
        if spec is None:
            load_note = load_note or "no local ASR spec to load"
        elif cloud:
            load_note = (
                f"the model load was skipped because the active profile ({spec.raw}) is a cloud "
                "provider, which is designed to reach the network"
            )
        else:
            load_note = _load_model_bounded(spec, load_timeout_s)
        time.sleep(max(0.0, idle_s))

    remote = monitor.remote_attempts
    data = {
        "asrSpec": spec_raw,
        "modelLoad": load_note,
        "idleSeconds": idle_s,
        "attempts": monitor.attempts,
        "remoteAttempts": remote,
    }
    if remote:
        listed = ", ".join(
            f"{entry['host']}:{entry['port']} via {entry['api']}" for entry in remote[:8]
        )
        return CheckResult(
            "egress.connections",
            FAIL,
            f"{len(remote)} non-loopback connection attempt(s) were observed while loading the "
            f"speech model: {listed}. DCENT_Voice is supposed to make none.",
            "Please send this diagnostics zip to the maintainers: a startup egress attempt is "
            "a bug, not a configuration issue.",
            data,
        )
    loopback = len(monitor.attempts)
    if load_note.startswith("the model load did not finish"):
        return CheckResult(
            "egress.connections",
            WARN,
            f"no outbound connection was attempted, but the check is partial: {load_note}",
            "Re-run doctor when the machine is idle. A very slow disk (or an antivirus "
            "scanning the 670 MB model on every read) is the usual cause.",
            data,
        )
    return CheckResult(
        "egress.connections",
        PASS,
        f"no non-loopback connection was attempted while resolving and loading the speech "
        f"model ({loopback} loopback attempt(s)); {load_note}",
        data=data,
    )


def _active_asr_spec() -> tuple[Any, bool]:
    from dcent_voice.config import Locality, default_config_path, load_config

    config = load_config(default_config_path(), create=False)
    spec = config.current_profile.asr
    return spec, spec.locality is Locality.CLOUD


def _load_model_bounded(spec: Any, timeout_s: float) -> str:
    """Load the model on a daemon thread and give up (without killing it) on time."""
    outcome: dict[str, str] = {}

    def work() -> None:
        try:
            from dcent_voice.asr.factory import build_asr_provider

            provider = build_asr_provider(spec, idle_unload_s=0.0)
            provider.load()
            with contextlib.suppress(Exception):  # unloading is best effort
                provider.unload()
            outcome["detail"] = f"the model for {spec.raw} loaded successfully"
        except Exception as exc:  # noqa: BLE001 - a load failure is reported by config.asr_model
            outcome["detail"] = f"the model load failed ({type(exc).__name__}: {exc})"

    started = time.monotonic()
    worker = threading.Thread(target=work, name="doctor-egress-model-load", daemon=True)
    worker.start()
    worker.join(timeout=timeout_s)
    if worker.is_alive():
        return (
            f"the model load did not finish within {timeout_s:.0f} s, so only the first "
            f"{timeout_s:.0f} s of the load were observed"
        )
    return f"{outcome.get('detail', 'the model load ended')} in {time.monotonic() - started:.1f} s"
