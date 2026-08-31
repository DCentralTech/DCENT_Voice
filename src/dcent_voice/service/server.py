# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Run the local ADE service in a managed thread."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ServiceThread:
    """Run the local HTTP service in a managed background thread."""

    app: Any
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "warning"
    # Optional identity probe so wait_ready does not treat a foreign TCP
    # listener on the same port as "our" service.
    health_path: str = "/health"

    def __post_init__(self) -> None:
        self._server: Any = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="DCENTService", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        self._thread.join(timeout)
        self.stopped.set()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error is not None:
                return False
            if self.ready.is_set():
                return True
            if self._thread is not None and not self._thread.is_alive() and self.error is None:
                # Thread died without recording error (e.g. import failure path).
                return False
            # Uvicorn's in-process flag is the only readiness authority. An HTTP
            # response can come from a foreign process squatting on this port;
            # accepting a matching public JSON shape would publish our fresh ADE
            # token to that foreign endpoint.
            server = self._server
            if server is not None and getattr(server, "started", False):
                self.ready.set()
                return True
            time.sleep(0.05)
        return self.error is None and self.ready.is_set()

    def _run(self) -> None:
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover - dependency/environment specific
            self.error = RuntimeError("uvicorn is required for the local service.")
            self.error.__cause__ = exc
            self.stopped.set()
            return

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            lifespan="off",
            access_log=False,
            # Do not let uvicorn install its own logging config. Its default
            # ColourizedFormatter calls ``sys.stdout.isatty()`` in __init__, and
            # the windowed frozen build launched from Explorer has
            # ``sys.stdout is None`` — which raised
            # "ValueError: Unable to configure formatter 'default'" inside this
            # thread and silently cost a real double-click the entire loopback
            # API (no ADE attach, no /health) while the tray still appeared.
            # DCENT_Voice configures its own handlers in util.logging anyway.
            log_config=None,
            # Belt and braces: nothing uvicorn builds should ask a stream that
            # may be None whether it is a terminal.
            use_colors=False,
        )
        try:
            server: Any = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None
            self._server = server
            server.run()
        except BaseException as exc:
            self.error = exc
            raise
        finally:
            self.stopped.set()


def format_http_base(host: str, port: int) -> str:
    """Format an HTTP origin, bracketing IPv6 literals as URLs require."""
    display_host = host
    if ":" in host and not host.startswith("["):
        display_host = f"[{host}]"
    return f"http://{display_host}:{port}"
