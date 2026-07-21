#!/usr/bin/env python3
"""Control-center job lifecycle helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from redact_common import redact


_QUOTED_LOCAL_PATH = re.compile(
    r"(?P<quote>['\"])(?:/|~/|[A-Za-z]:\\)(?P<body>[^\r\n]*?)(?P=quote)"
)
_UNQUOTED_LOCAL_PATH = re.compile(
    r"(?<![:/\w])(?:/|~/|[A-Za-z]:\\)[^\s'\"<>|]+"
)


def redact_local_paths(value: str) -> str:
    text = _QUOTED_LOCAL_PATH.sub("[本机路径]", str(value or ""))
    return _UNQUOTED_LOCAL_PATH.sub("[本机路径]", text)


class JobConflict(RuntimeError):
    pass


def sanitize_job_output(value: str, limit: int = 50000) -> str:
    credential_safe = str(redact(str(value or "")) or "")
    return redact_local_paths(credential_safe)[-limit:]


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
