#!/usr/bin/env python3
"""Crash-safe append-only JSONL storage for versioned model events."""

from __future__ import annotations

import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, TypeVar

from model_types import ModelValidationError, validate_event

try:
    import fcntl
except ImportError:  # pragma: no cover - guarded for non-POSIX imports
    fcntl = None


T = TypeVar("T")


class EventConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EventCorruption(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        line_number: int,
        recoverable_tail: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.line_number = line_number
        self.recoverable_tail = recoverable_tail


class ReplayLimitExceeded(RuntimeError):
    def __init__(self, limit: int) -> None:
        super().__init__("event replay limit exceeded")
        self.limit = limit


class EventPathError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unsafe_path(message: str, exc: Optional[BaseException] = None) -> EventPathError:
    error = EventPathError("unsafe_path", message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_verified_directory_at(parent_fd: int, name: str) -> int:
    try:
        observed = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise _unsafe_path(
            "concurrently created parent cannot be inspected safely",
            exc,
        )
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise _unsafe_path(
            "concurrently created parent must be a real directory",
        )
    try:
        child = os.open(
            name,
            _directory_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise _unsafe_path(
            "concurrently created parent cannot be opened safely",
            exc,
        )
    try:
        opened = os.fstat(child)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (observed.st_dev, observed.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise _unsafe_path(
                "concurrently created parent identity changed",
            )
        return child
    except Exception:
        os.close(child)
        raise


@contextmanager
def _anchored_parent(path: Path, *, create: bool) -> Iterator[tuple]:
    absolute = Path(os.path.abspath(str(path)))
    parts = absolute.parent.parts
    descriptor = os.open(parts[0], _directory_flags())
    try:
        for component in parts[1:]:
            try:
                child = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(
                        component,
                        _directory_flags(),
                        dir_fd=descriptor,
                    )
                except FileExistsError:
                    child = _open_verified_directory_at(
                        descriptor,
                        component,
                    )
                except OSError as exc:
                    raise _unsafe_path(
                        "event store parent cannot be created safely",
                        exc,
                    )
            except OSError as exc:
                raise _unsafe_path(
                    "event store parent path is not a safe directory",
                    exc,
                )
            previous = descriptor
            descriptor = child
            os.close(previous)
        yield descriptor, absolute.name
    finally:
        os.close(descriptor)


def _regular_stat_at(parent_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        metadata = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unsafe_path("event store path cannot be inspected safely", exc)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _unsafe_path("event store path must be a regular file")
    return metadata


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _verify_public_parent(path: Path, parent_fd: int) -> None:
    try:
        with _anchored_parent(path, create=False) as (public_fd, _name):
            if not _same_identity(os.fstat(parent_fd), os.fstat(public_fd)):
                raise _unsafe_path(
                    "event store public parent identity changed",
                )
    except FileNotFoundError as exc:
        raise _unsafe_path(
            "event store public parent disappeared",
            exc,
        )


def _verify_public_file(
    parent_fd: int,
    name: str,
    descriptor: int,
) -> None:
    current = _regular_stat_at(parent_fd, name)
    if current is None or not _same_identity(current, os.fstat(descriptor)):
        raise _unsafe_path("event store public file identity changed")


def safe_regular_exists(path: Path) -> bool:
    try:
        with _anchored_parent(path, create=False) as (parent_fd, name):
            return _regular_stat_at(parent_fd, name) is not None
    except FileNotFoundError:
        return False


def safe_read_text(path: Path) -> Optional[str]:
    try:
        with _anchored_parent(path, create=False) as (parent_fd, name):
            if _regular_stat_at(parent_fd, name) is None:
                return None
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise _unsafe_path("event store file cannot be opened safely", exc)
            try:
                with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                    return handle.read()
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        return None


def safe_atomic_write_text(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    with _anchored_parent(path, create=True) as (parent_fd, name):
        _regular_stat_at(parent_fd, name)
        temporary = "." + name + "." + str(os.getpid()) + "." + str(time.time_ns())
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("safe file write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            owned_descriptor = descriptor
            descriptor = -1
            os.close(owned_descriptor)
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


@contextmanager
def _exclusive_lock(
    lock_path: Path,
    *,
    timeout: float,
    stale_after: float,
) -> Iterator[None]:
    with _anchored_parent(lock_path, create=True) as (parent_fd, name):
        fd = _acquire_event_lock(parent_fd, name, timeout, stale_after)
        try:
            yield parent_fd
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _acquire_event_lock(
    parent_fd: int,
    name: str,
    timeout: float,
    stale_after: float,
) -> int:
    del stale_after  # Kept in the public constructor for compatibility.
    if fcntl is None:
        raise RuntimeError("kernel file locking is unavailable")
    deadline = time.monotonic() + timeout
    while True:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            if time.monotonic() >= deadline:
                raise _unsafe_path(
                    "event lock cannot be opened safely",
                    exc,
                )
            time.sleep(0.002)
            continue
        except OSError as exc:
            raise _unsafe_path("event lock cannot be opened safely", exc)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            raise _unsafe_path("event lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for event store lock")
            time.sleep(0.02)
            continue
        try:
            current = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            owned = os.fstat(fd)
            if (
                not stat.S_ISREG(current.st_mode)
                or (owned.st_dev, owned.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                owned_fd = fd
                fd = -1
                try:
                    fcntl.flock(owned_fd, fcntl.LOCK_UN)
                finally:
                    os.close(owned_fd)
                if time.monotonic() >= deadline:
                    raise _unsafe_path(
                        "event lock identity changed during acquisition",
                    )
                continue
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            encoded = json.dumps(
                {"pid": os.getpid(), "created_at": time.time()},
                ensure_ascii=True,
            ).encode("ascii")
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("event lock write made no progress")
                view = view[written:]
            os.fsync(fd)
            return fd
        except Exception:
            if fd >= 0:
                owned_fd = fd
                fd = -1
                try:
                    fcntl.flock(owned_fd, fcntl.LOCK_UN)
                finally:
                    os.close(owned_fd)
            raise


@contextmanager
def _descriptor_lock(
    descriptor: int,
    *,
    timeout: float,
    exclusive: bool,
) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError("kernel file locking is unavailable")
    deadline = time.monotonic() + timeout
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for event file lock")
            time.sleep(0.02)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _canonical_event(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "seq"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _idempotency_intent(value: Mapping[str, Any]) -> str:
    ignored = {
        "seq",
        "event_id",
        "request_id",
        "occurred_at",
        "stream_version",
        "expected_version",
    }
    payload = {key: item for key, item in value.items() if key not in ignored}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class JsonlEventStore:
    def __init__(
        self,
        path: Path,
        *,
        lock_timeout: float = 5.0,
        stale_lock_after: float = 60.0,
        max_replay_events: int = 100_000,
        max_event_bytes: int = 1024 * 1024,
    ) -> None:
        self.path = Path(os.path.abspath(str(path)))
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout = float(lock_timeout)
        self.stale_lock_after = float(stale_lock_after)
        self.max_replay_events = int(max_replay_events)
        self.max_event_bytes = int(max_event_bytes)
        if self.max_replay_events < 1:
            raise ValueError("max_replay_events must be positive")
        if self.max_event_bytes < 1:
            raise ValueError("max_event_bytes must be positive")

    def _decode_row(
        self,
        raw: bytes,
        *,
        line_number: int,
        recoverable_tail: bool,
    ) -> Dict[str, Any]:
        if len(raw) > self.max_event_bytes:
            raise EventCorruption(
                "event_too_large",
                "event exceeds maximum encoded size",
                line_number=line_number,
            )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventCorruption(
                "partial_tail" if recoverable_tail else "malformed_event",
                "event JSON is incomplete or malformed",
                line_number=line_number,
                recoverable_tail=recoverable_tail,
            ) from exc
        if not isinstance(value, dict):
            raise EventCorruption(
                "invalid_event",
                "event must be a JSON object",
                line_number=line_number,
            )
        try:
            validate_event(value)
        except (ModelValidationError, KeyError, TypeError, ValueError) as exc:
            raise EventCorruption(
                "invalid_event",
                "event envelope is invalid",
                line_number=line_number,
            ) from exc
        if not _positive_int(value.get("seq")):
            raise EventCorruption(
                "invalid_sequence",
                "event sequence must be a positive integer",
                line_number=line_number,
            )
        return dict(value)

    def _validated_events(
        self,
        *,
        recover_tail: bool,
        parent_fd: int,
        descriptor: Optional[int] = None,
    ) -> Iterator[Dict[str, Any]]:
        mode = "r+b" if recover_tail else "rb"
        flags = os.O_RDWR if recover_tail else os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        owns_descriptor = descriptor is None
        if descriptor is None:
            try:
                descriptor = os.open(self.path.name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise _unsafe_path("event log cannot be opened safely", exc)
        assert descriptor is not None
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            if owns_descriptor:
                os.close(descriptor)
            raise _unsafe_path("event log must be a regular file")
        descriptor_lock = None
        if owns_descriptor:
            descriptor_lock = _descriptor_lock(
                descriptor,
                timeout=self.lock_timeout,
                exclusive=recover_tail,
            )
            lock_entered = False
            try:
                descriptor_lock.__enter__()
                lock_entered = True
                _verify_public_file(parent_fd, self.path.name, descriptor)
                _verify_public_parent(self.path, parent_fd)
            except Exception:
                try:
                    if lock_entered:
                        descriptor_lock.__exit__(None, None, None)
                finally:
                    os.close(descriptor)
                raise
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            handle_context = os.fdopen(
                descriptor,
                mode,
                closefd=False,
            )
            with handle_context as handle:
                seen_event_ids = set()
                seen_idempotency_keys = set()
                stream_versions: Dict[str, int] = {}
                expected_seq = 1
                line_number = 0
                while True:
                    start = handle.tell()
                    raw = handle.readline(self.max_event_bytes + 2)
                    if not raw:
                        break
                    line_number += 1
                    if len(raw) > self.max_event_bytes + 1:
                        raise EventCorruption(
                            "event_too_large",
                            "event exceeds maximum encoded size",
                            line_number=line_number,
                        )
                    if not raw.strip():
                        raise EventCorruption(
                            "blank_event_line",
                            "event log contains a blank line",
                            line_number=line_number,
                        )
                    complete_line = raw.endswith(b"\n")
                    payload = raw[:-1] if complete_line else raw
                    needs_terminator = False
                    if not complete_line:
                        try:
                            row = self._decode_row(
                                payload,
                                line_number=line_number,
                                recoverable_tail=True,
                            )
                        except EventCorruption as exc:
                            if not recover_tail or exc.code != "partial_tail":
                                raise
                            handle.seek(start)
                            handle.truncate()
                            handle.flush()
                            os.fsync(handle.fileno())
                            break
                        if not recover_tail:
                            raise EventCorruption(
                                "partial_tail",
                                "event line is missing its terminator",
                                line_number=line_number,
                                recoverable_tail=True,
                            )
                        needs_terminator = True
                    else:
                        row = self._decode_row(
                            payload,
                            line_number=line_number,
                            recoverable_tail=False,
                        )
                    event_id = str(row["event_id"])
                    if event_id in seen_event_ids:
                        raise EventCorruption(
                            "duplicate_event_id",
                            "duplicate event_id in event log",
                            line_number=line_number,
                        )
                    seen_event_ids.add(event_id)
                    idempotency_key = str(row["idempotency_key"])
                    if idempotency_key in seen_idempotency_keys:
                        raise EventCorruption(
                            "duplicate_idempotency_key",
                            "duplicate idempotency key in event log",
                            line_number=line_number,
                        )
                    seen_idempotency_keys.add(idempotency_key)
                    seq = int(row["seq"])
                    if seq != expected_seq:
                        raise EventCorruption(
                            "non_monotonic_sequence",
                            "event sequence is not contiguous",
                            line_number=line_number,
                        )
                    expected_seq += 1
                    stream_id = str(row["stream_id"])
                    current_version = stream_versions.get(stream_id, 0)
                    if (
                        int(row["expected_version"]) != current_version
                        or int(row["stream_version"]) != current_version + 1
                    ):
                        raise EventCorruption(
                            "non_monotonic_stream_version",
                            "stream version is not contiguous",
                            line_number=line_number,
                        )
                    stream_versions[stream_id] = int(row["stream_version"])
                    if needs_terminator:
                        handle.seek(0, os.SEEK_END)
                        handle.write(b"\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    yield row
        finally:
            try:
                if descriptor_lock is not None:
                    descriptor_lock.__exit__(None, None, None)
            finally:
                if owns_descriptor:
                    os.close(descriptor)

    def _append_bytes(
        self,
        payload: bytes,
        *,
        parent_fd: int,
        event_fd: Optional[int] = None,
    ) -> None:
        _verify_public_parent(self.path, parent_fd)
        owns_descriptor = event_fd is None
        created = False
        fd = event_fd
        if fd is None:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(self.path.name, flags, 0o600, dir_fd=parent_fd)
                created = True
            except OSError as exc:
                raise _unsafe_path("event log cannot be created safely", exc)
        assert fd is not None
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise _unsafe_path("event store path is not a regular file")
            _verify_public_file(parent_fd, self.path.name, fd)
            _verify_public_parent(self.path, parent_fd)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("event append made no progress")
                view = view[written:]
            os.fsync(fd)
            if created:
                os.fsync(parent_fd)
            _verify_public_file(parent_fd, self.path.name, fd)
            _verify_public_parent(self.path, parent_fd)
        finally:
            if owns_descriptor:
                os.close(fd)

    def append(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        candidate = dict(event)
        if "seq" in candidate:
            raise ValueError("seq is assigned by the event store")
        try:
            validate_event(candidate)
        except (ModelValidationError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid event envelope") from exc
        event_id = str(candidate.get("event_id") or "")
        if not event_id:
            raise ValueError("event_id is required")

        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ) as parent_fd:
            _verify_public_parent(self.path, parent_fd)
            event_fd: Optional[int] = None
            flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                existed = _regular_stat_at(parent_fd, self.path.name) is not None
                try:
                    event_fd = os.open(
                        self.path.name,
                        flags,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise _unsafe_path(
                        "event log cannot be opened safely",
                        exc,
                    )
                if not existed:
                    os.fsync(parent_fd)
                with _descriptor_lock(
                    event_fd,
                    timeout=self.lock_timeout,
                    exclusive=True,
                ):
                    _verify_public_file(parent_fd, self.path.name, event_fd)
                    _verify_public_parent(self.path, parent_fd)
                    by_event_id: Dict[str, Dict[str, Any]] = {}
                    by_idempotency_key: Dict[str, Dict[str, Any]] = {}
                    stream_versions: Dict[str, int] = {}
                    watermark = 0
                    for existing in self._validated_events(
                        recover_tail=True,
                        parent_fd=parent_fd,
                        descriptor=event_fd,
                    ):
                        by_event_id[str(existing["event_id"])] = existing
                        key = str(existing.get("idempotency_key") or "")
                        if key:
                            by_idempotency_key[key] = existing
                        stream_versions[str(existing["stream_id"])] = int(
                            existing["stream_version"]
                        )
                        watermark = int(existing["seq"])

                    same_event = by_event_id.get(event_id)
                    if same_event is not None:
                        if _canonical_event(same_event) == _canonical_event(candidate):
                            _verify_public_file(
                                parent_fd,
                                self.path.name,
                                event_fd,
                            )
                            _verify_public_parent(self.path, parent_fd)
                            return same_event
                        raise EventConflict(
                            "event_id_conflict",
                            "event_id was already used for a different event",
                        )

                    idempotency_key = str(candidate.get("idempotency_key") or "")
                    same_key = by_idempotency_key.get(idempotency_key)
                    if same_key is not None:
                        if _idempotency_intent(same_key) == _idempotency_intent(candidate):
                            _verify_public_file(
                                parent_fd,
                                self.path.name,
                                event_fd,
                            )
                            _verify_public_parent(self.path, parent_fd)
                            return same_key
                        raise EventConflict(
                            "idempotency_conflict",
                            "idempotency key was reused for a different intent",
                        )

                    stream_id = str(candidate["stream_id"])
                    current_version = stream_versions.get(stream_id, 0)
                    if int(candidate["expected_version"]) != current_version:
                        raise EventConflict(
                            "version_conflict",
                            "stream version changed",
                        )
                    stored = {**candidate, "seq": watermark + 1}
                    encoded = (
                        json.dumps(
                            stored,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    if len(encoded) > self.max_event_bytes:
                        raise ValueError("event exceeds maximum encoded size")
                    self._append_bytes(
                        encoded,
                        parent_fd=parent_fd,
                        event_fd=event_fd,
                    )
                    return stored
            finally:
                if event_fd is not None:
                    os.close(event_fd)

    def read_all(
        self,
        *,
        after_seq: int = 0,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        effective_limit = self.max_replay_events if limit is None else int(limit)
        rows: List[Dict[str, Any]] = []
        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ) as parent_fd:
            for row in self._validated_events(
                recover_tail=False,
                parent_fd=parent_fd,
            ):
                if int(row["seq"]) <= after_seq:
                    continue
                if len(rows) >= effective_limit:
                    if limit is None:
                        raise ReplayLimitExceeded(effective_limit)
                    break
                rows.append(row)
        return rows

    def read_stream(
        self,
        stream_id: str,
        *,
        after_version: int = 0,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if after_version < 0:
            raise ValueError("after_version must be non-negative")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        rows: List[Dict[str, Any]] = []
        effective_limit = self.max_replay_events if limit is None else int(limit)
        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ) as parent_fd:
            for row in self._validated_events(
                recover_tail=False,
                parent_fd=parent_fd,
            ):
                if (
                    row["stream_id"] != stream_id
                    or int(row["stream_version"]) <= after_version
                ):
                    continue
                if len(rows) >= effective_limit:
                    if limit is None:
                        raise ReplayLimitExceeded(effective_limit)
                    break
                rows.append(row)
        return rows

    def watermark(self) -> int:
        value = 0
        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ) as parent_fd:
            for row in self._validated_events(
                recover_tail=False,
                parent_fd=parent_fd,
            ):
                value = int(row["seq"])
        return value

    def stream_version(self, stream_id: str) -> int:
        value = 0
        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ) as parent_fd:
            for row in self._validated_events(
                recover_tail=False,
                parent_fd=parent_fd,
            ):
                if row["stream_id"] == stream_id:
                    value = int(row["stream_version"])
        return value

    def recover_tail(self) -> int:
        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ) as parent_fd:
            value = 0
            for row in self._validated_events(
                recover_tail=True,
                parent_fd=parent_fd,
            ):
                value = int(row["seq"])
            return value

    def exists(self) -> bool:
        return safe_regular_exists(self.path)

    def iter_all(self) -> Iterator[Dict[str, Any]]:
        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ) as parent_fd:
            for row in self._validated_events(
                recover_tail=False,
                parent_fd=parent_fd,
            ):
                yield row

    def rebuild_view(
        self,
        projector: Callable[[T, Dict[str, Any]], T],
        *,
        initial: T,
        after_seq: int = 0,
        limit: Optional[int] = None,
    ) -> T:
        current = initial
        for row in self.read_all(after_seq=after_seq, limit=limit):
            current = projector(current, row)
        return current
