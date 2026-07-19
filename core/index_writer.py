"""Durable, process-safe appends to the authoritative JSONL index."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from index_locks import source_lock


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

    with source_lock(source, exclusive=True):
        source_existed = source.exists()
        fd = os.open(source, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not source_existed:
            _fsync_directory(source.parent)
    return len(rows)
