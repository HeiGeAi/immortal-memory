#!/usr/bin/env python3
"""Control-center job lifecycle helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from redact_common import redact


class JobConflict(RuntimeError):
    pass


def sanitize_job_output(value: str, limit: int = 50000) -> str:
    return str(redact(str(value or "")) or "")[-limit:]


def run_evidence_marker(path: Path) -> tuple[Any, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stat = path.stat()
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    return (
        payload.get("run_id"),
        payload.get("started_at"),
        payload.get("finished_at"),
        payload.get("status"),
        stat.st_mtime_ns,
    )


def live_pid_lock(path: Path) -> bool:
    try:
        value = path.read_text(encoding="utf-8", errors="ignore").strip()
        pid = int(value.split()[0]) if value else 0
    except (OSError, ValueError, IndexError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
