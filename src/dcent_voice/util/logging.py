# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Configure structured application logging."""

from __future__ import annotations

import atexit
import contextlib
import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dcent_voice.config import APP_NAME, user_config_dir
from dcent_voice.util import bootlog

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

_fault_handle = None


def default_log_path() -> Path:
    return user_config_dir() / "logs" / "dcent_voice.log"


def configure_logging(
    *,
    level: int = logging.INFO,
    log_path: Path | None = None,
    console: bool = True,
) -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(level)
    # Keep the bootstrap handler installed by util.bootlog: it owns startup.log,
    # which is the only record of anything that happens before this point, and a
    # crash after this point must still reach it. Everything else is replaced.
    preserved = [
        handler
        for handler in logger.handlers
        if getattr(handler, bootlog.BOOTSTRAP_HANDLER_ATTR, False)
    ]
    logger.handlers.clear()
    for handler in preserved:
        logger.addHandler(handler)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)
    target = log_path or default_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        target,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    # The windowed frozen build (console=False) launched from Explorer has
    # ``sys.stderr is None``. ``logging.StreamHandler()`` binds that None as its
    # stream and then swallows an AttributeError inside ``handleError`` on every
    # single record — a handler that cannot write, hiding its own failure. Only
    # attach one when there is a stream to attach it to.
    if console and getattr(sys, "stderr", None) is not None:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        logger.addHandler(console_handler)

    # startup.log now only needs the failures; the routine INFO stream lives in
    # the rotating log so the two files do not mirror each other forever.
    bootlog.demote_bootstrap_handler()

    _install_exception_hooks(logger, target)
    atexit.register(flush_logging)
    logger.debug("Logging configured at %s", target)
    return logger


def flush_logging() -> None:
    root = logging.getLogger(APP_NAME)
    for handler in list(root.handlers):
        with contextlib.suppress(Exception):
            handler.flush()


def _install_exception_hooks(logger: logging.Logger, log_path: Path) -> None:
    """Surface uncaught exceptions under pythonw / tray-only launches."""

    def excepthook(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical(
            "uncaught exception\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )
        flush_logging()

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is None:
            return
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical(
            "uncaught thread exception in %s\n%s",
            args.thread.name if args.thread else "?",
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )
        flush_logging()

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    _enable_faulthandler(logger, log_path)


def _enable_faulthandler(logger: logging.Logger, log_path: Path) -> None:
    global _fault_handle
    try:
        import faulthandler

        fault_path = log_path.with_name("dcent_voice_fault.log")
        fault_path.parent.mkdir(parents=True, exist_ok=True)
        if _fault_handle is not None:
            with contextlib.suppress(Exception):
                _fault_handle.close()
        _fault_handle = open(fault_path, "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=_fault_handle, all_threads=True)
    except Exception:
        logger.debug("faulthandler unavailable", exc_info=True)
