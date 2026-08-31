# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Earliest-possible bootstrap: a log file, crash hooks, and the offline posture.

This module must be the *first* DCENT_Voice import of every entry point
(``_packaged.py``, ``__main__.py``, ``app.py``). Two guarantees depend on that
ordering:

* **Nothing fails silently.** The frozen Windows exe is built ``console=False``
  (``packaging/DCENT_Voice.spec``), so anything that dies before
  :func:`dcent_voice.util.logging.configure_logging` used to leave no trace at
  all — no stdout, no log line, no dialog. Importing this module installs a
  minimal ``logging.FileHandler`` on ``<profile>/logs/startup.log`` plus
  ``sys.excepthook`` / ``threading.excepthook`` / ``faulthandler`` before any
  heavy import runs, so an import-time native DLL failure is recorded.
* **Offline is enforced, not assumed.** ``huggingface_hub`` reads
  ``HF_HUB_OFFLINE`` and friends exactly once, at *import* time
  (``huggingface_hub/constants.py``). Setting them here — before any module
  that transitively imports it — turns the offline posture into a hard
  in-process guarantee instead of an accident of a model directory existing.
  ``scripts/download_models.py`` and ``scripts/qa/warm_model_cache.py`` opt out
  with ``DCENT_VOICE_ALLOW_HUB=1``.

Everything in here is cheap (stdlib only, no third-party import beyond
``platformdirs`` via :mod:`dcent_voice.util.paths`) and nothing in here may
raise: a bootstrap logger that can crash the app is worse than none.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Environment variables that pin the process to a local-only posture. They are
#: read at import time by ``huggingface_hub``/``transformers``; setting them
#: afterwards has no effect, which is why this module is imported first.
OFFLINE_ENV: dict[str, str] = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "DO_NOT_TRACK": "1",
}

#: Opt-out used only by the deliberate, user-initiated model download scripts.
ALLOW_HUB_ENV = "DCENT_VOICE_ALLOW_HUB"

#: Escape hatch for embedding contexts that install their own hooks.
DISABLE_ENV = "DCENT_VOICE_NO_BOOTLOG"

BOOT_LOG_FILENAME = "startup.log"

#: Directory name used for the ``%TEMP%`` / ``/tmp`` fallback.
APP_DIR_NAME = "DCENT_Voice"

BOOT_LOG_MAX_BYTES = 512 * 1024
BOOT_LOG_BACKUPS = 2

#: Marks the bootstrap handler so ``configure_logging()`` keeps it instead of
#: clearing it away with the rest of the handlers.
BOOTSTRAP_HANDLER_ATTR = "_dcent_bootstrap_handler"

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

_installed = False
_boot_log_path: Path | None = None
_fault_handle = None
_lock = threading.Lock()


def enforce_offline_env() -> bool:
    """Pin the hub/telemetry environment to offline. Returns False when opted out.

    Existing values are overwritten on purpose: a stale ``HF_HUB_OFFLINE=0``
    inherited from a parent shell must not silently re-enable network model
    resolution inside a build that promises local-only operation.
    """
    if _is_true(os.environ.get(ALLOW_HUB_ENV)):
        return False
    for key, value in OFFLINE_ENV.items():
        os.environ[key] = value
    return True


def allow_hub() -> bool:
    """True when this process deliberately opted into Hugging Face network access."""
    return _is_true(os.environ.get(ALLOW_HUB_ENV))


def boot_log_path() -> Path:
    """Where the bootstrap log lives (profile logs dir, else ``%TEMP%``).

    Not a read-only question: the first call proves the candidate is writable by
    opening it for append, which creates it. That is deliberate — a log location
    we cannot write to is not an answer — but it means asking has a side effect,
    and the result is memoised for the process. Use
    :func:`probe_boot_log_path` when you only want to know where we would look,
    and :func:`reset_boot_log_path` when the profile root changes under you.
    """
    global _boot_log_path
    if _boot_log_path is not None:
        return _boot_log_path
    resolved = _resolve_boot_log_path()
    if resolved is None:
        # Nothing proved writable. Name the temp-dir candidate anyway so
        # callers and diagnostics have a concrete path to report; actually
        # attaching a handler to it is allowed to fail, exactly as before.
        import tempfile

        resolved = Path(tempfile.gettempdir()) / APP_DIR_NAME / BOOT_LOG_FILENAME
    _boot_log_path = resolved
    return resolved


def probe_boot_log_path() -> Path | None:
    """Where the bootstrap log *would* live, without creating anything.

    Returns the memoised path when one has been resolved, otherwise the first
    candidate for the current profile. Never touches the filesystem, so a caller
    reporting "no logs found" (``doctor``) does not create the very file it is
    about to say is missing.
    """
    if _boot_log_path is not None:
        return _boot_log_path
    candidates = _boot_log_candidates()
    # Candidates carry a "lives in a shared parent" flag for _prepare_log_dir;
    # callers of this helper only ever want the path.
    return candidates[0][0] if candidates else None


def reset_boot_log_path() -> None:
    """Forget the memoised location so the next call re-resolves it.

    The memo is per-process and the profile root is per-launch, which is only a
    problem for one process that outlives several profiles: the test suite.
    Without this, whichever test touched bootlog first would pin the location
    for every later test — possibly to the developer's real profile.
    """
    global _boot_log_path
    _boot_log_path = None


def install(argv: list[str] | None = None) -> Path | None:
    """Install the bootstrap handler, crash hooks and offline env. Idempotent.

    Returns the bootstrap log path, or ``None`` when no file could be opened
    anywhere (the hooks are still installed so stderr keeps working).
    """
    global _installed
    enforce_offline_env()
    if _is_true(os.environ.get(DISABLE_ENV)):
        return None
    with _lock:
        if _installed:
            return _boot_log_path
        _installed = True
    path = None
    try:
        path = _attach_handler()
    except Exception:  # pragma: no cover - bootstrap must never raise
        _emergency(traceback.format_exc())
    with contextlib.suppress(Exception):
        _install_hooks()
    with contextlib.suppress(Exception):
        _enable_faulthandler(path)
    with contextlib.suppress(Exception):
        _log_environment(argv)
    return path


def logger() -> logging.Logger:
    """The application logger the bootstrap handler is attached to."""
    from dcent_voice.util.paths import APP_NAME

    return logging.getLogger(APP_NAME)


def demote_bootstrap_handler(level: int = logging.WARNING) -> None:
    """Raise the bootstrap handler's threshold once real logging is configured.

    The handler stays attached — a crash after ``configure_logging()`` must
    still reach ``startup.log`` — but routine INFO chatter belongs in the
    rotating ``dcent_voice.log`` only, so the two files do not duplicate each
    other for the whole life of the process.
    """
    for handler in list(logger().handlers):
        if getattr(handler, BOOTSTRAP_HANDLER_ATTR, False):
            handler.setLevel(level)


def _attach_handler() -> Path | None:
    log = logger()
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in log.handlers:
        if getattr(handler, BOOTSTRAP_HANDLER_ATTR, False):
            base_filename = getattr(handler, "baseFilename", None)
            return Path(base_filename) if base_filename else None

    path = boot_log_path()
    # Rotating, not plain: startup.log is appended to on every launch and lives
    # for the life of the install, and it ships inside the diagnostics zip.
    try:
        file_handler = RotatingFileHandler(
            path,
            maxBytes=BOOT_LOG_MAX_BYTES,
            backupCount=BOOT_LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError:  # pragma: no cover - no writable profile or temp dir
        return None
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    file_handler.setLevel(logging.INFO)
    setattr(file_handler, BOOTSTRAP_HANDLER_ATTR, True)
    log.addHandler(file_handler)
    return path


def _resolve_boot_log_path() -> Path | None:
    for candidate, shared in _boot_log_candidates():
        try:
            _prepare_log_dir(candidate.parent, shared=shared)
            # Prove writability now, while we can still fall back, rather than
            # discovering a read-only profile root inside an exception handler.
            _secure_touch(candidate)
        except Exception:
            continue
        return candidate
    return None


def _boot_log_candidates() -> list[tuple[Path, bool]]:
    """Candidate log paths, each flagged as living in a world-writable parent."""
    candidates: list[tuple[Path, bool]] = []
    try:
        from dcent_voice.util import paths

        candidates.append((paths.user_config_dir() / "logs" / BOOT_LOG_FILENAME, False))
    except Exception:  # pragma: no cover - platformdirs missing/unimportable
        pass
    import tempfile

    # POSIX ``/tmp`` is shared and world-writable; Windows ``%TEMP%`` resolves
    # under ``%LOCALAPPDATA%`` and is already per-user.
    candidates.append(
        (Path(tempfile.gettempdir()) / APP_DIR_NAME / BOOT_LOG_FILENAME, os.name == "posix")
    )
    return candidates


def _prepare_log_dir(directory: Path, *, shared: bool) -> None:
    """Create the log directory, refusing a hostile one in a shared parent.

    In a world-writable ``/tmp`` another local user can pre-create
    ``/tmp/DCENT_Voice`` — or a symlink pointing at something of theirs — and
    then read every diagnostic we write, or have us append to a file they chose.
    Under a shared parent we therefore create the directory ourselves with mode
    0o700 and, if it already exists, require it to be a real directory we own
    rather than following it blindly.
    """
    if not shared:
        directory.mkdir(parents=True, exist_ok=True)
        return

    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(directory, 0o700)
        return
    except FileExistsError:
        pass

    import stat as stat_module

    info = os.lstat(directory)  # lstat: a symlink here must fail, not resolve
    if not stat_module.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"{directory} exists but is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"{directory} is owned by another user")
    os.chmod(directory, 0o700)


def _secure_touch(path: Path) -> None:
    """Create/append-open the log file without following a symlink into it."""
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    # O_NOFOLLOW applies to the final component: a planted symlink named
    # startup.log fails here instead of redirecting our writes.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    os.close(os.open(path, flags, 0o600))


def _install_hooks() -> None:
    previous_excepthook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def excepthook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        if not issubclass(exc_type, KeyboardInterrupt):
            with contextlib.suppress(Exception):
                logger().critical(
                    "uncaught exception\n%s",
                    "".join(traceback.format_exception(exc_type, exc, tb)),
                )
        with contextlib.suppress(Exception):
            previous_excepthook(exc_type, exc, tb)

    def thread_excepthook(args):  # type: ignore[no-untyped-def]
        if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
            with contextlib.suppress(Exception):
                logger().critical(
                    "uncaught thread exception in %s\n%s",
                    args.thread.name if args.thread else "?",
                    "".join(
                        traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback
                        )
                    ),
                )
        with contextlib.suppress(Exception):
            previous_thread_hook(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def _enable_faulthandler(path: Path | None) -> None:
    global _fault_handle
    if path is None:
        return
    import faulthandler

    fault_path = path.with_name("startup_fault.log")
    # Same symlink-safe creation as the log itself; faulthandler needs a real
    # file object afterwards, and the directory has already been vetted.
    _secure_touch(fault_path)
    handle = open(fault_path, "a", encoding="utf-8")  # noqa: SIM115 - lives for the process
    faulthandler.enable(file=handle, all_threads=True)
    _fault_handle = handle


def _log_environment(argv: list[str] | None) -> None:
    from dcent_voice import __version__
    from dcent_voice.util import paths

    profile = paths.profile_root()
    logger().info(
        "boot version=%s frozen=%s exe=%s cwd=%s argv=%s bundle_root=%s "
        "profile_root=%s config_dir=%s os=%s python=%s offline=%s stdout=%s stderr=%s",
        __version__,
        paths.is_frozen(),
        getattr(sys, "executable", "?"),
        _safe_cwd(),
        redact_argv(argv),
        _safe(paths.bundle_root),
        profile if profile is not None else "<platform default>",
        _safe(paths.user_config_dir),
        f"{sys.platform} {_os_build()}",
        sys.version.split()[0],
        not allow_hub(),
        stream_state("stdout"),
        stream_state("stderr"),
    )


#: Subcommands whose arguments carry what the user actually said. ``compose``
#: takes the transcript as positionals; ``learn`` takes it in ``--from`` /
#: ``--to`` / ``--last``. ``transcribe`` is absent on purpose: it only ever
#: receives a WAV path, which is not content.
CONTENT_SUBCOMMANDS = frozenset({"compose", "learn"})

REDACTED = "<redacted>"


def redact_argv(argv: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Drop spoken content from a command line before it is written to disk.

    SECURITY.md promises transcripts are never logged to disk, but the command
    line is not obviously "a transcript" until you notice that
    ``dcent-voice compose hey can you send alice the deck`` puts a dictated
    sentence straight into ``argv`` — and from there into ``startup.log``,
    ``last-startup-failure.json`` and the diagnostics zip a user emails us.

    What survives is everything needed to debug a launch: the executable, the
    subcommand, and every flag *name*. What does not is any value that could be
    content — positionals and flag values after a content subcommand. Flags are
    kept because ``--style`` tells us how the app was invoked; their values are
    dropped because ``--from`` carries a sentence.
    """
    source = list(sys.argv if argv is None else argv)
    redacted: list[str] = []
    scrubbing = False
    for index, token in enumerate(source):
        if index == 0:  # the executable path is not content
            redacted.append(token)
            continue
        if not scrubbing:
            redacted.append(token)
            if token in CONTENT_SUBCOMMANDS:
                scrubbing = True
            continue
        if token.startswith("-"):
            # ``--from=hello there`` hides a value inside a flag-shaped token.
            name, separator, _value = token.partition("=")
            redacted.append(f"{name}={REDACTED}" if separator else token)
            continue
        redacted.append(REDACTED)
    return redacted


def stream_state(name: str) -> str:
    """``"none"`` or ``"handle"`` for ``sys.<name>``.

    Recorded on the boot line so every log says for itself whether the process
    had a console. Two shipped bugs turned on exactly this condition — uvicorn's
    ``ColourizedFormatter`` calling ``sys.stdout.isatty()``, and a
    ``logging.StreamHandler`` built over a ``None`` stream — and both were
    invisible for years because every harness handed the app a pipe that a real
    double-click never provides. ``scripts/fresh_profile_smoke.py`` asserts on
    this field rather than trusting that ``DETACHED_PROCESS`` did its job.

    Exactly two values. A stream object with no OS handle behind it (pytest
    capture, a StringIO) is still ``handle``: the question this answers is "can
    the app write anywhere", not "which file descriptor".
    """
    return "none" if getattr(sys, name, None) is None else "handle"


def _safe(func) -> str:  # type: ignore[no-untyped-def]
    try:
        return str(func())
    except Exception as exc:
        return f"<unavailable: {type(exc).__name__}: {exc}>"


def _safe_cwd() -> str:
    try:
        return str(Path.cwd())
    except Exception as exc:  # pragma: no cover - deleted cwd
        return f"<unavailable: {type(exc).__name__}: {exc}>"


def _os_build() -> str:
    try:
        import platform

        return platform.platform()
    except Exception:  # pragma: no cover
        return "?"


def _emergency(text: str) -> None:
    with contextlib.suppress(Exception):
        if sys.stderr is not None:
            sys.stderr.write(f"DCENT_Voice bootstrap logging unavailable:\n{text}\n")


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


install()
