#!/usr/bin/env python3
"""Cross-process atomic updates for Immortal's shared JSON state."""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping


def read_state(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _lock_is_stale(lock_path: Path, stale_after: float) -> bool:
    age = 0.0
    try:
        age = max(0.0, time.time() - lock_path.stat().st_mtime)
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0) if isinstance(payload, dict) else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return lock_path.exists() and age >= stale_after
    return not _pid_is_alive(pid) or age >= stale_after


def _acquire_lock(lock_path: Path, timeout: float, stale_after: float) -> int:
    deadline = time.monotonic() + timeout
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            payload = json.dumps(
                {"pid": os.getpid(), "created_at": time.time()},
                ensure_ascii=True,
            ).encode("ascii")
            os.write(fd, payload)
            os.fsync(fd)
            return fd
        except FileExistsError:
            if _lock_is_stale(lock_path, stale_after):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for state lock: {lock_path}")
            time.sleep(0.02)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        temp_path = None
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def update_state_atomic(
    path: Path,
    updates: Mapping[str, Any],
    *,
    timeout: float = 5.0,
    stale_after: float = 60.0,
) -> Dict[str, Any]:
    """Reload, merge, fsync, and replace a shared JSON object under a lock."""
    def merge(current: Dict[str, Any]) -> Dict[str, Any]:
        current.update(dict(updates))
        return current

    return mutate_state_atomic(
        path,
        merge,
        timeout=timeout,
        stale_after=stale_after,
    )


def mutate_state_atomic(
    path: Path,
    mutator: Callable[[Dict[str, Any]], Dict[str, Any]],
    *,
    timeout: float = 5.0,
    stale_after: float = 60.0,
) -> Dict[str, Any]:
    """Apply a read-modify-write callback while holding the state lock."""
    lock_path = path.with_name(path.name + ".lock")
    fd = _acquire_lock(lock_path, timeout, stale_after)
    try:
        current = read_state(path, {})
        if not isinstance(current, dict):
            raise ValueError(f"state root must be an object: {path}")
        updated = mutator(dict(current))
        if not isinstance(updated, dict):
            raise ValueError("state mutator must return an object")
        _write_json_atomic(path, updated)
        return updated
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
