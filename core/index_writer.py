"""Durable, process-safe appends to the authoritative JSONL index."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from index_locks import source_lock
from maintenance_gate import writer_access


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("index append made no progress")
        remaining = remaining[written:]


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_jsonl_records(
    source: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    maintenance_held: bool = False,
) -> int:
    """Append one serialized batch under the shared source lock contract."""
    source = Path(source)
    rows = list(records)
    if not rows:
        return 0
    payload = "".join(
        json.dumps(record, ensure_ascii=False) + "\n"
        for record in rows
    ).encode("utf-8")
    source.parent.mkdir(parents=True, exist_ok=True)

    def append_locked() -> None:
        source_existed = source.exists()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(source, flags, 0o600)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not source_existed:
            _fsync_directory(source.parent)

    if maintenance_held:
        with source_lock(source, exclusive=True):
            append_locked()
    else:
        with writer_access(source.parent):
            with source_lock(source, exclusive=True):
                append_locked()
    return len(rows)
