# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Record and enforce consent and provider privacy rules."""

from __future__ import annotations

import errno
import json
import math
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO

from dcent_voice.asr.base import Locality
from dcent_voice.config import AppConfig, user_config_dir

FIRST_RUN_EDUCATION = (
    "DCENT_Voice defaults to local providers, so your voice stays on this machine. "
    "Cloud providers are available for thin devices or maximum accuracy, but they are "
    "always labeled and require consent before audio or text is sent."
)

PRIVACY_LOCK_TIMEOUT_S = 10.0
PRIVACY_LOCK_RETRY_S = 0.01
EGRESS_TAIL_MAX_SCAN_BYTES = 1024 * 1024

_PATH_LOCKS: dict[Path, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class PrivacyStatus(Enum):
    SOVEREIGN = "sovereign"
    HYBRID = "hybrid"
    CLOUD = "cloud"


class ConsentRequired(RuntimeError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("Cloud consent required for: " + ", ".join(missing))


@dataclass(frozen=True)
class ProviderPrivacy:
    key: str
    role: str
    provider: str
    locality: Locality
    payload_type: str


@dataclass(frozen=True)
class ConsentEntry:
    provider_key: str
    accepted_at: float
    payload_type: str
    policy_url: str = ""


@dataclass(frozen=True)
class EgressEntry:
    timestamp: float
    provider_key: str
    payload_type: str
    byte_count: int


class ConsentLedger:
    """Records explicit user consent for external providers."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_consent_ledger_path()

    def has_consent(self, provider_key: str, *, payload_type: str | None = None) -> bool:
        """Return whether a valid consent covers this provider and payload.

        Provider keys identify a configured role (for example ``asr:openai``),
        but the payload type is an independent part of what the user approved.
        A stale or corrupted text-only record must never authorize audio egress.
        Callers that know the payload type therefore bind both values here.
        """

        entry = self.entries().get(provider_key)
        if entry is None or entry.provider_key != provider_key:
            return False
        return payload_type is None or entry.payload_type == payload_type

    def grant(self, provider_key: str, *, payload_type: str, policy_url: str = "") -> ConsentEntry:
        if not isinstance(provider_key, str) or not provider_key.strip():
            raise ValueError("provider_key must contain text")
        if not isinstance(payload_type, str) or not payload_type.strip():
            raise ValueError("payload_type must contain text")
        with _coordinated_file_access(self.path):
            entries = self._read_entries()
            entry = ConsentEntry(
                provider_key=provider_key.strip(),
                accepted_at=time.time(),
                payload_type=payload_type.strip(),
                policy_url=policy_url,
            )
            entries[entry.provider_key] = entry
            self._write(entries)
        return entry

    def revoke(self, provider_key: str) -> None:
        with _coordinated_file_access(self.path):
            entries = self._read_entries()
            entries.pop(provider_key, None)
            self._write(entries)

    def entries(self) -> dict[str, ConsentEntry]:
        with _coordinated_file_access(self.path):
            return self._read_entries()

    def _read_entries(self) -> dict[str, ConsentEntry]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        parsed: dict[str, ConsentEntry] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not key or not isinstance(value, dict):
                continue
            provider_key = value.get("provider_key", key)
            payload_type = value.get("payload_type")
            accepted_at = value.get("accepted_at")
            policy_url = value.get("policy_url", "")
            if (
                not isinstance(provider_key, str)
                or provider_key != key
                or not isinstance(payload_type, str)
                or not payload_type
                or isinstance(accepted_at, bool)
                or not isinstance(accepted_at, (int, float))
                or not math.isfinite(float(accepted_at))
                or float(accepted_at) <= 0
                or not isinstance(policy_url, str)
            ):
                continue
            parsed[key] = ConsentEntry(
                provider_key=provider_key,
                accepted_at=float(accepted_at),
                payload_type=payload_type,
                policy_url=policy_url,
            )
        return parsed

    def _write(self, entries: dict[str, ConsentEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {key: asdict(value) for key, value in sorted(entries.items())}
        encoded = json.dumps(data, indent=2, sort_keys=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(self.path)
            _sync_parent_directory(self.path.parent)
        finally:
            with suppress(FileNotFoundError):
                temp_path.unlink()


class EgressLog:
    """Records metadata about approved external data egress."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_egress_log_path()

    def record(self, provider_key: str, *, payload_type: str, byte_count: int) -> EgressEntry:
        entry = EgressEntry(
            timestamp=time.time(),
            provider_key=provider_key,
            payload_type=payload_type,
            byte_count=max(0, int(byte_count)),
        )
        with _coordinated_file_access(self.path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = (json.dumps(asdict(entry), sort_keys=True) + "\n").encode("utf-8")
            with _open_private_append(self.path) as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() > 0:
                    handle.seek(-1, os.SEEK_END)
                    trailing = handle.read(1)
                    handle.seek(0, os.SEEK_END)
                    if trailing != b"\n":
                        # A killed writer can leave one incomplete JSON record.
                        # Delimit it so future valid metadata is never poisoned.
                        handle.write(b"\n")
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        return entry

    def tail(self, limit: int = 100) -> list[EgressEntry]:
        if limit <= 0:
            return []
        with _coordinated_file_access(self.path):
            if not self.path.exists():
                return []
            size = self.path.stat().st_size
            scan_size = min(size, EGRESS_TAIL_MAX_SCAN_BYTES)
            with self.path.open("rb") as handle:
                start = size - scan_size
                preceding = b"\n"
                if start > 0:
                    handle.seek(start - 1)
                    preceding = handle.read(1)
                encoded = handle.read(scan_size)
        if scan_size < size and preceding != b"\n":
            # The capped scan can start in the middle of a JSONL record. Never
            # interpret that fragment as an entry.
            _, separator, encoded = encoded.partition(b"\n")
            if not separator:
                return []
        entries: list[EgressEntry] = []
        for line in reversed(encoded.splitlines()):
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    continue
                entry = EgressEntry(
                    timestamp=float(raw.get("timestamp", 0)),
                    provider_key=str(raw.get("provider_key", "")),
                    payload_type=str(raw.get("payload_type", "")),
                    byte_count=int(raw.get("byte_count", 0)),
                )
            except (json.JSONDecodeError, TypeError, ValueError, OverflowError):
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        entries.reverse()
        return entries


def _open_private_append(path: Path) -> BinaryIO:
    """Open an egress JSONL file for append with owner-only permissions.

    Passing ``0o600`` to ``os.open`` prevents a permissive umask from exposing
    new logs.  ``fchmod`` also repairs logs created by older releases before any
    new metadata is appended.  Validation happens on the opened descriptor so
    symlinks and non-regular-file substitutions fail closed.
    """

    fallback_identity: tuple[int, int] | None = None
    requires_identity_check = not hasattr(os, "O_NOFOLLOW")
    if requires_identity_check:
        try:
            before_open = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(before_open.st_mode):
                raise ValueError("egress log symlinks are forbidden")
            fallback_identity = (before_open.st_dev, before_open.st_ino)
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | os.O_APPEND
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("egress log must be a regular file")
        if requires_identity_check:
            after_open = os.lstat(path)
            opened_identity = (opened.st_dev, opened.st_ino)
            if (
                stat.S_ISLNK(after_open.st_mode)
                or opened_identity != (after_open.st_dev, after_open.st_ino)
                or (fallback_identity is not None and fallback_identity != opened_identity)
            ):
                raise ValueError("egress log changed during safe open")
        if os.name != "nt":
            platform_os: Any = os
            platform_os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a+b")
    except BaseException:
        os.close(fd)
        raise


def _path_lock(path: Path) -> threading.RLock:
    key = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _coordinated_file_access(path: Path, *, timeout_s: float = PRIVACY_LOCK_TIMEOUT_S):
    """Serialize one privacy file across threads and app processes."""

    if timeout_s < 0:
        raise ValueError("privacy lock timeout must be non-negative")
    deadline = time.monotonic() + timeout_s
    thread_lock = _path_lock(path)
    if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("timed out waiting for privacy path lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        fallback_identity: tuple[int, int] | None = None
        requires_identity_check = not hasattr(os, "O_NOFOLLOW")
        if requires_identity_check:
            try:
                before_open = os.lstat(lock_path)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(before_open.st_mode):
                    raise ValueError("privacy lock symlinks are forbidden")
                fallback_identity = (before_open.st_dev, before_open.st_ino)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("privacy lock must be a regular file")
            if requires_identity_check:
                after_open = os.lstat(lock_path)
                opened_identity = (opened.st_dev, opened.st_ino)
                if (
                    stat.S_ISLNK(after_open.st_mode)
                    or opened_identity != (after_open.st_dev, after_open.st_ino)
                    or (fallback_identity is not None and fallback_identity != opened_identity)
                ):
                    raise ValueError("privacy lock changed during safe open")
            if os.name != "nt":
                platform_os: Any = os
                platform_os.fchmod(fd, 0o600)
            if os.name == "nt":
                import msvcrt

                if opened.st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                _acquire_advisory_lock(fd, deadline, windows=True)
                try:
                    yield
                finally:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl: Any = __import__("fcntl")

                _acquire_advisory_lock(fd, deadline, windows=False)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        thread_lock.release()


def _acquire_advisory_lock(fd: int, deadline: float, *, windows: bool) -> None:
    while True:
        try:
            if windows:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = __import__("fcntl")

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for privacy advisory lock") from exc
            time.sleep(min(PRIVACY_LOCK_RETRY_S, remaining))


def _sync_parent_directory(path: Path) -> None:
    """Durably publish a completed atomic replace where directory fsync exists."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", -1)}:
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {
                errno.EINVAL,
                errno.ENOSYS,
                getattr(errno, "ENOTSUP", -1),
            }:
                raise
    finally:
        os.close(fd)


class PrivacyMonitor:
    """Reports the current privacy posture of configured providers."""

    def __init__(
        self,
        providers: tuple[ProviderPrivacy, ...],
        *,
        ledger: ConsentLedger | None = None,
        egress_log: EgressLog | None = None,
    ) -> None:
        self.providers = providers
        self.ledger = ledger or ConsentLedger()
        self.egress_log = egress_log or EgressLog()

    @classmethod
    def from_config(cls, config: AppConfig) -> PrivacyMonitor:
        profile = config.current_profile
        providers = [
            ProviderPrivacy(
                key=f"asr:{profile.asr.provider}",
                role="asr",
                provider=profile.asr.provider,
                locality=profile.asr.locality,
                payload_type="audio",
            )
        ]
        if profile.llm.enabled:
            providers.append(
                ProviderPrivacy(
                    key=f"llm:{profile.llm.provider}",
                    role="llm",
                    provider=profile.llm.provider,
                    locality=profile.llm.locality,
                    payload_type="text",
                )
            )
        return cls(
            tuple(providers),
            ledger=ConsentLedger(config.privacy.consent_ledger_path),
            egress_log=EgressLog(config.privacy.egress_log_path),
        )

    @property
    def status(self) -> PrivacyStatus:
        active = self.providers
        cloud_count = sum(1 for provider in active if provider.locality is Locality.CLOUD)
        if cloud_count == 0:
            return PrivacyStatus.SOVEREIGN
        if cloud_count == len(active):
            return PrivacyStatus.CLOUD
        return PrivacyStatus.HYBRID

    def missing_consents(self) -> tuple[str, ...]:
        return tuple(
            provider.key
            for provider in self.providers
            if provider.locality is Locality.CLOUD
            and not self.ledger.has_consent(
                provider.key,
                payload_type=provider.payload_type,
            )
        )

    def validate_cloud_consent(self) -> None:
        missing = self.missing_consents()
        if missing:
            raise ConsentRequired(missing)

    def record_egress(
        self, provider_key: str, *, payload_type: str, byte_count: int
    ) -> EgressEntry:
        matching = tuple(
            provider
            for provider in self.providers
            if provider.key == provider_key and provider.payload_type == payload_type
        )
        if not matching:
            raise ConsentRequired((provider_key,))
        if any(
            provider.locality is Locality.CLOUD for provider in matching
        ) and not self.ledger.has_consent(provider_key, payload_type=payload_type):
            # Providers invoke their egress logger before the network request.
            # Re-check here so revoking consent also stops an already-created
            # headless/desktop provider instead of merely changing the UI state.
            raise ConsentRequired((provider_key,))
        return self.egress_log.record(
            provider_key, payload_type=payload_type, byte_count=byte_count
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "providers": [
                {
                    "key": provider.key,
                    "role": provider.role,
                    "provider": provider.provider,
                    "locality": provider.locality.value,
                    "payload_type": provider.payload_type,
                }
                for provider in self.providers
            ],
            "missing_consents": list(self.missing_consents()),
        }


def default_consent_ledger_path() -> Path:
    return user_config_dir() / "privacy" / "consent_ledger.json"


def default_egress_log_path() -> Path:
    return user_config_dir() / "privacy" / "egress.jsonl"
