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


@contextmanager
def _exclusive_lock(
    lock_path: Path,
    *,
    timeout: float,
    stale_after: float,
) -> Iterator[None]:
    fd = _acquire_event_lock(lock_path, timeout, stale_after)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_reclaimable(lock_path: Path, stale_after: float) -> bool:
    try:
        age = max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return False
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("lock payload must be an object")
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return age >= stale_after
    return not _pid_is_alive(pid)


def _acquire_event_lock(
    lock_path: Path,
    timeout: float,
    stale_after: float,
) -> int:
    deadline = time.monotonic() + timeout
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(lock_path), flags, 0o600)
        except FileExistsError:
            if _lock_is_reclaimable(lock_path, stale_after):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for event store lock")
            time.sleep(0.02)
            continue
        try:
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
            os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            raise


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
        self.path = Path(path)
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

    def _validated_events(self, *, recover_tail: bool) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return
        mode = "r+b" if recover_tail else "rb"
        with self.path.open(mode) as handle:
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

    def _append_bytes(self, payload: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(self.path), flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("event store path is not a regular file")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("event append made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

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
        ):
            by_event_id: Dict[str, Dict[str, Any]] = {}
            by_idempotency_key: Dict[str, Dict[str, Any]] = {}
            stream_versions: Dict[str, int] = {}
            watermark = 0
            for existing in self._validated_events(recover_tail=True):
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
                    return same_event
                raise EventConflict(
                    "event_id_conflict",
                    "event_id was already used for a different event",
                )

            idempotency_key = str(candidate.get("idempotency_key") or "")
            same_key = by_idempotency_key.get(idempotency_key)
            if same_key is not None:
                if _idempotency_intent(same_key) == _idempotency_intent(candidate):
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
            self._append_bytes(encoded)
            return stored

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
        ):
            for row in self._validated_events(recover_tail=False):
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
        ):
            for row in self._validated_events(recover_tail=False):
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
        ):
            for row in self._validated_events(recover_tail=False):
                value = int(row["seq"])
        return value

    def stream_version(self, stream_id: str) -> int:
        value = 0
        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ):
            for row in self._validated_events(recover_tail=False):
                if row["stream_id"] == stream_id:
                    value = int(row["stream_version"])
        return value

    def recover_tail(self) -> int:
        with _exclusive_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            stale_after=self.stale_lock_after,
        ):
            value = 0
            for row in self._validated_events(recover_tail=True):
                value = int(row["seq"])
            return value

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
