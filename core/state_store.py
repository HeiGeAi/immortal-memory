#!/usr/bin/env python3
"""Cross-process atomic updates for Immortal's shared JSON state."""

import json
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Tuple


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


def _process_start_identity(pid: int):
    """Return a stable-enough identity to distinguish PID reuse when available."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _lock_is_stale(lock_path: Path, stale_after: float) -> bool:
    age = 0.0
    try:
        age = max(0.0, time.time() - lock_path.stat().st_mtime)
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return lock_path.exists() and age >= stale_after
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return lock_path.exists() and age >= stale_after
    if pid <= 0:
        return age >= stale_after
    recorded_host = payload.get("hostname")
    if recorded_host and recorded_host != socket.gethostname():
        return False
    if not _pid_is_alive(pid):
        return True
    recorded_start = payload.get("process_start")
    if recorded_start:
        current_start = _process_start_identity(pid)
        if current_start is None:
            return False
        return current_start != recorded_start
    return False


def _acquire_lock(lock_path: Path, timeout: float, stale_after: float) -> Tuple[int, str]:
    deadline = time.monotonic() + timeout
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_id = uuid.uuid4().hex
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_start": _process_start_identity(os.getpid()),
            "lock_id": lock_id,
            "created_at": time.time(),
        },
        ensure_ascii=True,
    ).encode("ascii")
    fd, unpublished_path = tempfile.mkstemp(
        dir=str(lock_path.parent),
        prefix=f".{lock_path.name}.",
        suffix=".pending",
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("failed to write complete state lock identity")
            remaining = remaining[written:]
        os.fsync(fd)
        while True:
            try:
                os.link(unpublished_path, lock_path)
                try:
                    os.unlink(unpublished_path)
                except OSError:
                    pass
                return fd, lock_id
            except FileExistsError:
                pass
            try:
                original_stat = lock_path.stat()
            except FileNotFoundError:
                continue
            if _lock_is_stale(lock_path, stale_after):
                try:
                    current_stat = lock_path.stat()
                    if (
                        current_stat.st_dev == original_stat.st_dev
                        and current_stat.st_ino == original_stat.st_ino
                    ):
                        lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for state lock: {lock_path}")
            time.sleep(0.02)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(unpublished_path)
        except FileNotFoundError:
            pass
        raise


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
    fd, lock_id = _acquire_lock(lock_path, timeout, stale_after)
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
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("lock_id") == lock_id:
                lock_path.unlink()
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass
