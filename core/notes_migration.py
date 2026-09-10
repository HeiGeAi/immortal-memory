"""Explicit, checkpointed migration for legacy Obsidian note facts."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from index_locks import source_lock
from notes_transactions import (
    MANIFEST_SCHEMA_VERSION,
    _open_directory_fd,
    _manifest_semantically_valid,
    _secure_read_json,
    _secure_regular_exists,
    _secure_unlink,
    durable_atomic_json,
    manifest_path,
)


MIGRATION_VERSION = 2
MAX_MANIFEST_SOURCES = 1000
DAILY_RELATIVE_PATTERN = re.compile(
    r"^daily/(\d{4}-\d{2}-\d{2})\.jsonl(?:\.gz)?$"
)


class MigrationConflict(RuntimeError):
    pass


class MigrationCapacity(RuntimeError):
    def __init__(
        self,
        *,
        phase: str,
        required_bytes: int,
        available_bytes: int,
        reserve_bytes: int,
    ) -> None:
        super().__init__("migration_insufficient_scratch_space")
        self.phase = phase
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        self.reserve_bytes = reserve_bytes


class MigrationExpansionLimit(RuntimeError):
    pass


class MigrationDeadline(RuntimeError):
    pass


class CatalogBindingError(MigrationConflict):
    pass


@dataclass(frozen=True)
class _Deadline:
    clock: Callable[[], float]
    expires_at: float

    def check(self) -> None:
        if self.clock() >= self.expires_at:
            raise MigrationDeadline("migration_deadline_reached")


class _CompressedBudgetReached(RuntimeError):
    pass


class _BoundedCountingReader:
    def __init__(self, handle: Any, limit: int) -> None:
        self.handle = handle
        self.remaining = limit
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            raise _CompressedBudgetReached()
        requested = self.remaining if size < 0 else min(size, self.remaining)
        payload = self.handle.read(requested)
        self.bytes_read += len(payload)
        self.remaining -= len(payload)
        return payload

    def tell(self) -> int:
        return self.handle.tell()


def after_catalog_commit_before_checkpoint() -> None:
    """Fault-injection boundary for catalog/checkpoint crash tests."""


@dataclass(frozen=True)
class MigrationLimits:
    max_files: int = 10000
    max_bytes: int = 2 * 1024 * 1024 * 1024
    max_seconds: float = 3600.0
    max_compressed_bytes: int = 2 * 1024 * 1024 * 1024
    reserve_bytes: int = 64 * 1024 * 1024
    max_gzip_expanded_bytes: int = 8 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            self.max_files < 1
            or self.max_bytes < 1
            or self.max_seconds < 0
            or self.max_compressed_bytes < 1
            or self.reserve_bytes < 0
            or self.max_gzip_expanded_bytes < 1
        ):
            raise ValueError("migration limits must be non-negative and bounded")


def _migration_dir(vault: Path) -> Path:
    return Path(vault) / "notes" / "migration"


def _checkpoint_path(vault: Path) -> Path:
    return _migration_dir(vault) / "checkpoint.json"


def _catalog_path(vault: Path) -> Path:
    return _migration_dir(vault) / "catalog.sqlite3"


def _inflate_dir(vault: Path) -> Path:
    return _migration_dir(vault) / "inflate"


def _available_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _require_capacity(
    path: Path,
    *,
    phase: str,
    additional_bytes: int,
    reserve_bytes: int,
) -> None:
    available = _available_bytes(path)
    required = max(0, int(additional_bytes)) + max(0, int(reserve_bytes))
    if available < required:
        raise MigrationCapacity(
            phase=phase,
            required_bytes=required,
            available_bytes=available,
            reserve_bytes=reserve_bytes,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_files(
    vault: Path,
    *,
    deadline: Optional[_Deadline] = None,
) -> list[tuple[str, Path]]:
    if deadline is not None:
        deadline.check()
    vault = Path(vault)
    try:
        vault_metadata = os.lstat(vault)
    except FileNotFoundError as exc:
        raise MigrationConflict("vault_root_invalid") from exc
    if stat.S_ISLNK(vault_metadata.st_mode) or not stat.S_ISDIR(
        vault_metadata.st_mode
    ):
        raise MigrationConflict("vault_root_invalid")
    files: list[tuple[str, Path]] = []
    index = vault / "index.jsonl"
    try:
        index_metadata = os.lstat(index)
    except FileNotFoundError:
        index_metadata = None
    if index_metadata is not None:
        if stat.S_ISLNK(index_metadata.st_mode) or not stat.S_ISREG(
            index_metadata.st_mode
        ):
            raise MigrationConflict("index_source_invalid")
        files.append(("index.jsonl", index))
    daily = vault / "daily"
    try:
        daily_metadata = os.lstat(daily)
    except FileNotFoundError:
        daily_metadata = None
    if daily_metadata is not None:
        if stat.S_ISLNK(daily_metadata.st_mode) or not stat.S_ISDIR(
            daily_metadata.st_mode
        ):
            raise MigrationConflict("daily_source_invalid")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        daily_fd = os.open(daily, flags)
        try:
            with os.scandir(daily_fd) as entries:
                names = []
                for entry in entries:
                    if deadline is not None:
                        deadline.check()
                    if (
                        (
                            entry.name.endswith(".jsonl")
                            or entry.name.endswith(".jsonl.gz")
                        )
                        and entry.is_file(follow_symlinks=False)
                        and not entry.is_symlink()
                    ):
                        names.append(entry.name)
                names.sort()
        finally:
            os.close(daily_fd)
    else:
        names = []
    for name in names:
        files.append((f"daily/{name}", daily / name))
    return files


def _connect_catalog(
    path: Path,
    *,
    reset: bool,
    catalog_id: Optional[str] = None,
) -> sqlite3.Connection:
    path = Path(path)
    parent_fd = _open_directory_fd(path.parent, create=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        candidates = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        for candidate in candidates:
            try:
                metadata = os.stat(
                    candidate.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("migration_catalog_not_regular")
        if reset:
            for candidate in candidates:
                try:
                    os.unlink(candidate.name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        catalog_fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        try:
            catalog_metadata = os.fstat(catalog_fd)
            if not stat.S_ISREG(catalog_metadata.st_mode):
                raise OSError("migration_catalog_not_regular")
            catalog_identity = (
                catalog_metadata.st_dev,
                catalog_metadata.st_ino,
            )
        finally:
            os.close(catalog_fd)
    finally:
        os.close(parent_fd)
    con = sqlite3.connect(path)
    try:
        current_metadata = os.lstat(path)
        if (
            not stat.S_ISREG(current_metadata.st_mode)
            or (current_metadata.st_dev, current_metadata.st_ino)
            != catalog_identity
        ):
            raise OSError("migration_catalog_replaced")
    except Exception:
        con.close()
        raise
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS facts(
          id TEXT PRIMARY KEY,
          public_json TEXT NOT NULL,
          index_json TEXT,
          daily_relpath TEXT NOT NULL,
          source_relative TEXT,
          source_timestamp TEXT,
          seen_index INTEGER NOT NULL DEFAULT 0,
          seen_daily INTEGER NOT NULL DEFAULT 0,
          first_seen_file INTEGER NOT NULL,
          first_seen_seq INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS file_rows(
          file_rel TEXT NOT NULL,
          seq INTEGER NOT NULL,
          kind TEXT NOT NULL,
          record_id TEXT,
          raw BLOB,
          emit INTEGER NOT NULL,
          PRIMARY KEY(file_rel, seq)
        );
        CREATE TABLE IF NOT EXISTS scanned_files(
          file_rel TEXT PRIMARY KEY,
          size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_rows(
          file_rel TEXT NOT NULL,
          seq INTEGER NOT NULL,
          raw_sha256 TEXT NOT NULL,
          PRIMARY KEY(file_rel, seq)
        );
        CREATE TABLE IF NOT EXISTS migration_meta(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migration_progress(
          id INTEGER PRIMARY KEY CHECK(id=1),
          file_index INTEGER NOT NULL,
          offset INTEGER NOT NULL,
          generation INTEGER NOT NULL
        );
        """
    )
    if reset:
        bound_id = catalog_id or uuid.uuid4().hex
        con.execute(
            "INSERT OR REPLACE INTO migration_meta(key,value) VALUES('catalog_id',?)",
            (bound_id,),
        )
        con.execute(
            "INSERT OR REPLACE INTO migration_progress(id,file_index,offset,generation) "
            "VALUES(1,0,0,0)"
        )
    elif catalog_id is not None:
        row = con.execute(
            "SELECT value FROM migration_meta WHERE key='catalog_id'"
        ).fetchone()
        if row is None or row[0] != catalog_id:
            con.close()
            raise CatalogBindingError("migration_catalog_id_mismatch")
    con.commit()
    return con


def _catalog_progress(con: sqlite3.Connection) -> tuple[int, int, int]:
    row = con.execute(
        "SELECT file_index,offset,generation FROM migration_progress WHERE id=1"
    ).fetchone()
    if row is None:
        raise CatalogBindingError("migration_catalog_progress_missing")
    try:
        file_index, offset, generation = map(int, row)
    except (TypeError, ValueError) as exc:
        raise CatalogBindingError("migration_catalog_progress_invalid") from exc
    if file_index < 0 or offset < 0 or generation < 0:
        raise CatalogBindingError("migration_catalog_progress_invalid")
    return file_index, offset, generation


def _validate_catalog_progress(
    con: sqlite3.Connection,
    checkpoint: dict[str, Any],
) -> None:
    db_file_index, db_offset, db_generation = _catalog_progress(con)
    try:
        checkpoint_file_index = int(checkpoint.get("file_index") or 0)
        checkpoint_offset = int(checkpoint.get("offset") or 0)
        checkpoint_generation = int(checkpoint["catalog_generation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogBindingError("migration_checkpoint_progress_invalid") from exc
    if (
        db_generation < checkpoint_generation
        or (db_file_index, db_offset)
        < (checkpoint_file_index, checkpoint_offset)
    ):
        raise CatalogBindingError("migration_catalog_progress_behind")


def _commit_catalog_progress(
    con: sqlite3.Connection,
    *,
    file_index: int,
    offset: int,
) -> int:
    current_file, current_offset, generation = _catalog_progress(con)
    next_file, next_offset = max(
        (current_file, current_offset),
        (int(file_index), int(offset)),
    )
    next_generation = generation + 1
    con.execute(
        "UPDATE migration_progress SET file_index=?,offset=?,generation=? WHERE id=1",
        (next_file, next_offset, next_generation),
    )
    con.commit()
    return next_generation


def _strict_daily_relpath(row: dict[str, Any]) -> str:
    timestamp = str(row.get("timestamp") or "")
    day_text = timestamp[:10]
    if len(day_text) != 10:
        raise MigrationConflict("invalid_note_date")
    try:
        parsed = date.fromisoformat(day_text)
    except ValueError as exc:
        raise MigrationConflict("invalid_note_date") from exc
    if parsed.isoformat() != day_text:
        raise MigrationConflict("invalid_note_date")
    return f"daily/{day_text}.jsonl"


def _daily_date_from_relative(relative: str) -> str:
    match = DAILY_RELATIVE_PATTERN.fullmatch(relative)
    if not match:
        raise MigrationConflict("daily_target_invalid")
    day_text = match.group(1)
    try:
        parsed = date.fromisoformat(day_text)
    except ValueError as exc:
        raise MigrationConflict("daily_target_invalid") from exc
    if parsed.isoformat() != day_text:
        raise MigrationConflict("daily_target_invalid")
    return day_text


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _catalog_row(
    con: sqlite3.Connection,
    file_rel: str,
    raw: bytes,
    seq: int,
    file_order: int,
) -> None:
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    processed = con.execute(
        "SELECT raw_sha256 FROM processed_rows WHERE file_rel=? AND seq=?",
        (file_rel, seq),
    ).fetchone()
    if processed is not None:
        if processed[0] != raw_sha256:
            raise MigrationConflict("catalog_row_replay_conflict")
        return

    def mark_processed() -> None:
        con.execute(
            "INSERT INTO processed_rows(file_rel,seq,raw_sha256) VALUES(?,?,?)",
            (file_rel, seq, raw_sha256),
        )

    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationConflict("invalid_legacy_jsonl") from exc
    if not isinstance(row, dict):
        raise MigrationConflict("invalid_legacy_jsonl")
    if row.get("source") != "obsidian-note":
        con.execute(
            "INSERT OR REPLACE INTO file_rows(file_rel,seq,kind,record_id,raw,emit) "
            "VALUES(?,?,?,?,?,?)",
            (file_rel, seq, "non_note", None, raw, 1),
        )
        mark_processed()
        return
    record_id = str(row.get("id") or "").strip()
    if not record_id:
        raise MigrationConflict("missing_note_id")
    logical_daily_relpath = _strict_daily_relpath(row)
    if file_rel.startswith("daily/"):
        if _daily_date_from_relative(file_rel) != Path(logical_daily_relpath).stem:
            raise MigrationConflict("daily_date_mismatch")
        daily_relpath = file_rel
    else:
        daily_relpath = logical_daily_relpath
    public_json = json.dumps(_public(row), ensure_ascii=False, sort_keys=True)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    source_relative = metadata.get("relative_path")
    if not isinstance(source_relative, str) or not source_relative:
        source_relative = None
    source_timestamp = str(row.get("timestamp") or "")
    existing = con.execute(
        "SELECT public_json,index_json,daily_relpath,seen_index,seen_daily "
        "FROM facts WHERE id=?",
        (record_id,),
    ).fetchone()
    is_index = file_rel == "index.jsonl"
    if existing is None:
        con.execute(
            "INSERT INTO facts(id,public_json,index_json,daily_relpath,"
            "source_relative,source_timestamp,seen_index,seen_daily,"
            "first_seen_file,first_seen_seq) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                public_json,
                json.dumps(row, ensure_ascii=False, sort_keys=True) if is_index else None,
                daily_relpath,
                source_relative,
                source_timestamp,
                1 if is_index else 0,
                0 if is_index else 1,
                file_order,
                seq,
            ),
        )
        con.execute(
            "INSERT OR REPLACE INTO file_rows(file_rel,seq,kind,record_id,raw,emit) "
            "VALUES(?,?,?,?,?,?)",
            (file_rel, seq, "note", record_id, None, 1),
        )
        mark_processed()
        return
    if (
        existing[0] != public_json
        or _daily_date_from_relative(str(existing[2]))
        != _daily_date_from_relative(daily_relpath)
    ):
        raise MigrationConflict("note_payload_conflict")
    index_json = existing[1]
    if is_index and index_json is None:
        index_json = json.dumps(row, ensure_ascii=False, sort_keys=True)
    emit = 1 if (is_index and int(existing[3]) == 0) or (
        not is_index and int(existing[4]) == 0
    ) else 0
    con.execute(
        "INSERT OR REPLACE INTO file_rows(file_rel,seq,kind,record_id,raw,emit) "
        "VALUES(?,?,?,?,?,?)",
        (file_rel, seq, "note", record_id, None, emit),
    )
    selected_daily_relpath = (
        daily_relpath
        if not is_index and int(existing[4]) == 0
        else str(existing[2])
    )
    con.execute(
        "UPDATE facts SET index_json=?, daily_relpath=?, "
        "source_relative=COALESCE(source_relative,?),"
        "source_timestamp=MAX(source_timestamp,?), seen_index=?, seen_daily=? WHERE id=?",
        (
            index_json,
            selected_daily_relpath,
            source_relative,
            source_timestamp,
            int(existing[3]) + (1 if is_index else 0),
            int(existing[4]) + (0 if is_index else 1),
            record_id,
        ),
    )
    mark_processed()


def _load_checkpoint(vault: Path) -> Optional[dict[str, Any]]:
    path = _checkpoint_path(vault)
    try:
        payload = _secure_read_json(path)
    except FileNotFoundError:
        return None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_checkpoint(vault: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at"] = _now()
    durable_atomic_json(_checkpoint_path(vault), checkpoint)


def _signature(path: Path) -> dict[str, int]:
    metadata = path.stat()
    return {
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
        "dev": metadata.st_dev,
        "ino": metadata.st_ino,
    }


def _source_fingerprint(
    path: Path,
    *,
    deadline: Optional[_Deadline] = None,
) -> dict[str, Any]:
    fingerprint: dict[str, Any] = dict(_signature(path))
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if deadline is not None:
                deadline.check()
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if deadline is not None:
        deadline.check()
    fingerprint["sha256"] = digest.hexdigest()
    return fingerprint


def _validate_completed_sources(
    files: list[tuple[str, Path]],
    checkpoint: dict[str, Any],
    *,
    deadline: _Deadline,
) -> None:
    signatures = checkpoint.get("completed_signatures") or {}
    by_rel = dict(files)
    for relative, expected in signatures.items():
        deadline.check()
        path = by_rel.get(relative)
        expected_metadata = {
            key: value for key, value in expected.items() if key != "sha256"
        }
        if path is None or _signature(path) != expected_metadata:
            raise MigrationConflict("legacy_source_changed")


def _inflate_spool_path(vault: Path, relative: str) -> Path:
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    return _inflate_dir(vault) / f"{digest}.jsonl"


def _inflate_gzip_source(
    vault: Path,
    relative: str,
    source: Path,
    checkpoint: dict[str, Any],
    limits: MigrationLimits,
    *,
    deadline: _Deadline,
    compressed_used: int,
    expanded_used: int,
) -> tuple[Optional[Path], int, int, Optional[str]]:
    completed = checkpoint.setdefault("inflated_sources", {}).get(relative)
    spool = _inflate_spool_path(vault, relative)
    if isinstance(completed, dict):
        if (
            completed.get("source_signature") != _signature(source)
            or not spool.is_file()
            or _hash_file(spool)
            != (completed.get("sha256"), int(completed.get("expanded_bytes") or -1))
        ):
            raise MigrationConflict("legacy_source_changed")
        return spool, 0, 0, None

    root = spool.parent
    root.mkdir(parents=True, exist_ok=True)
    partial = spool.with_suffix(".partial")
    try:
        partial.unlink()
    except FileNotFoundError:
        pass
    digest = hashlib.sha256()
    expanded_this_run = 0
    raw_handle = source.open("rb")
    counting_handle = _BoundedCountingReader(
        raw_handle,
        limits.max_compressed_bytes - compressed_used,
    )
    zipped = gzip.GzipFile(fileobj=counting_handle, mode="rb")
    try:
        with partial.open("wb") as output:
            try:
                while True:
                    try:
                        deadline.check()
                    except MigrationDeadline:
                        return None, counting_handle.bytes_read, expanded_this_run, "time"
                    remaining_expanded = limits.max_bytes - expanded_used - expanded_this_run
                    if remaining_expanded <= 0:
                        return None, counting_handle.bytes_read, expanded_this_run, "expanded"
                    chunk = zipped.read(min(1024 * 1024, remaining_expanded))
                    if not chunk:
                        output.flush()
                        os.fsync(output.fileno())
                        break
                    if expanded_this_run + len(chunk) > limits.max_gzip_expanded_bytes:
                        raise MigrationExpansionLimit("migration_gzip_expansion_limit")
                    _require_capacity(
                        vault,
                        phase="gzip_inflate",
                        additional_bytes=len(chunk),
                        reserve_bytes=limits.reserve_bytes,
                    )
                    output.write(chunk)
                    digest.update(chunk)
                    expanded_this_run += len(chunk)
            except _CompressedBudgetReached:
                return None, counting_handle.bytes_read, expanded_this_run, "compressed"
        os.replace(partial, spool)
        _fsync_parent(spool)
        metadata = {
            "source_signature": _signature(source),
            "sha256": digest.hexdigest(),
            "expanded_bytes": expanded_this_run,
            "compressed_bytes": counting_handle.bytes_read,
        }
        checkpoint.setdefault("inflated_sources", {})[relative] = metadata
        _write_checkpoint(vault, checkpoint)
        return (
            spool,
            counting_handle.bytes_read,
            expanded_this_run,
            None,
        )
    finally:
        zipped.close()
        raw_handle.close()
        if partial.exists():
            partial.unlink()


def _scan_catalog(
    vault: Path,
    con: sqlite3.Connection,
    files: list[tuple[str, Path]],
    checkpoint: dict[str, Any],
    limits: MigrationLimits,
    deadline: _Deadline,
) -> tuple[bool, dict[str, int]]:
    file_index = int(checkpoint.get("file_index") or 0)
    offset = int(checkpoint.get("offset") or 0)
    files_started = 0
    bytes_read = 0
    compressed_read = 0
    rows_since_checkpoint = 0
    bytes_since_checkpoint = 0
    _validate_completed_sources(files, checkpoint, deadline=deadline)
    while file_index < len(files):
        relative, path = files[file_index]
        if offset == 0:
            if checkpoint.get("active_file") is not None:
                if (
                    checkpoint.get("active_file") != relative
                    or checkpoint.get("active_signature") != _signature(path)
                ):
                    raise MigrationConflict("legacy_source_changed")
            if files_started >= limits.max_files:
                break
            files_started += 1
            if checkpoint.get("active_file") is None:
                checkpoint["active_file"] = relative
                checkpoint["active_signature"] = _signature(path)
                _write_checkpoint(vault, checkpoint)
        elif (
            checkpoint.get("active_file") != relative
            or checkpoint.get("active_signature") != _signature(path)
        ):
            raise MigrationConflict("legacy_source_changed")
        try:
            deadline.check()
        except MigrationDeadline:
            break
        try:
            is_gzip = relative.endswith(".gz")
            scan_path = path
            if is_gzip:
                scan_path, compressed_delta, expanded_delta, limit_kind = (
                    _inflate_gzip_source(
                        vault,
                        relative,
                        path,
                        checkpoint,
                        limits,
                        deadline=deadline,
                        compressed_used=compressed_read,
                        expanded_used=bytes_read,
                    )
                )
                compressed_read += compressed_delta
                bytes_read += expanded_delta
                if scan_path is None:
                    checkpoint["file_index"] = file_index
                    checkpoint["offset"] = offset
                    _write_checkpoint(vault, checkpoint)
                    return False, {
                        "files_processed": files_started,
                        "bytes_processed": bytes_read,
                        "expanded_bytes_processed": bytes_read,
                        "compressed_bytes_processed": compressed_read,
                        "limit_kind": str(limit_kind),
                    }
            raw_handle = Path(scan_path).open("rb")
            handle = raw_handle
            try:
                handle.seek(offset)
                seq = offset
                while True:
                    try:
                        deadline.check()
                    except MigrationDeadline:
                        break
                    remaining = (
                        1024 * 1024
                        if is_gzip
                        else limits.max_bytes - bytes_read
                    )
                    if remaining <= 0:
                        break
                    raw = handle.readline(remaining)
                    if not raw:
                        if checkpoint.get("active_signature") != _signature(path):
                            raise MigrationConflict("legacy_source_changed")
                        offset = 0
                        checkpoint.setdefault("completed_signatures", {})[
                            relative
                        ] = _source_fingerprint(path, deadline=deadline)
                        con.execute(
                            "INSERT OR REPLACE INTO scanned_files(file_rel,size,mtime_ns) "
                            "VALUES(?,?,?)",
                            (
                                relative,
                                checkpoint["completed_signatures"][relative]["size"],
                                checkpoint["completed_signatures"][relative]["mtime_ns"],
                            ),
                        )
                        catalog_generation = _commit_catalog_progress(
                            con,
                            file_index=file_index + 1,
                            offset=0,
                        )
                        after_catalog_commit_before_checkpoint()
                        file_index += 1
                        checkpoint["file_index"] = file_index
                        checkpoint["offset"] = 0
                        checkpoint["catalog_generation"] = catalog_generation
                        checkpoint.pop("active_file", None)
                        checkpoint.pop("active_signature", None)
                        if is_gzip:
                            checkpoint.setdefault("inflated_sources", {}).pop(
                                relative, None
                            )
                        _write_checkpoint(vault, checkpoint)
                        if is_gzip:
                            try:
                                Path(scan_path).unlink()
                            except FileNotFoundError:
                                pass
                        rows_since_checkpoint = 0
                        bytes_since_checkpoint = 0
                        break
                    if not is_gzip:
                        bytes_read += len(raw)
                    if not raw.endswith(b"\n"):
                        break
                    bytes_since_checkpoint += len(raw)
                    rows_since_checkpoint += 1
                    _catalog_row(con, relative, raw, seq, file_index)
                    offset += len(raw)
                    seq = offset
                    checkpoint["file_index"] = file_index
                    checkpoint["offset"] = offset
                    if (
                        rows_since_checkpoint >= 500
                        or bytes_since_checkpoint >= 1024 * 1024
                    ):
                        catalog_generation = _commit_catalog_progress(
                            con,
                            file_index=file_index,
                            offset=offset,
                        )
                        after_catalog_commit_before_checkpoint()
                        checkpoint["catalog_generation"] = catalog_generation
                        _write_checkpoint(vault, checkpoint)
                        rows_since_checkpoint = 0
                        bytes_since_checkpoint = 0
                if (
                    offset != 0
                    or bytes_read >= limits.max_bytes
                    or compressed_read >= limits.max_compressed_bytes
                ):
                    catalog_generation = _commit_catalog_progress(
                        con,
                        file_index=file_index,
                        offset=offset,
                    )
                    after_catalog_commit_before_checkpoint()
                    checkpoint["file_index"] = file_index
                    checkpoint["offset"] = offset
                    checkpoint["catalog_generation"] = catalog_generation
                    _write_checkpoint(vault, checkpoint)
                    break
            finally:
                raw_handle.close()
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise MigrationConflict("invalid_legacy_gzip") from exc
    complete = file_index >= len(files)
    return complete, {
        "files_processed": files_started,
        "bytes_processed": bytes_read,
        "expanded_bytes_processed": bytes_read,
        "compressed_bytes_processed": compressed_read,
    }


def _staging_path(root: Path, relative: str) -> Path:
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    suffix = ".jsonl.gz" if relative.endswith(".gz") else ".jsonl"
    return root / f"{digest}{suffix}"


def _hash_file(
    path: Path,
    *,
    deadline: Optional[_Deadline] = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            if deadline is not None:
                deadline.check()
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    if deadline is not None:
        deadline.check()
    return digest.hexdigest(), size


def _write_staging(
    vault: Path,
    con: sqlite3.Connection,
    files: list[tuple[str, Path]],
    limits: MigrationLimits,
    deadline: _Deadline,
) -> list[dict[str, Any]]:
    root = _migration_dir(vault) / "staging"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    targets = {relative for relative, _path in files}
    targets.add("index.jsonl")
    targets.update(
        row[0]
        for row in con.execute("SELECT DISTINCT daily_relpath FROM facts").fetchall()
    )
    entries: list[dict[str, Any]] = []
    for relative in sorted(targets):
        deadline.check()
        _validate_fact_relative(relative)
        staging = _staging_path(root, relative)
        raw_handle = staging.open("wb")
        handle = (
            gzip.GzipFile(
                filename="",
                fileobj=raw_handle,
                mode="wb",
                mtime=0,
            )
            if relative.endswith(".gz")
            else raw_handle
        )
        unchecked_bytes = 0

        def write_payload(payload: bytes) -> None:
            nonlocal unchecked_bytes
            deadline.check()
            unchecked_bytes += len(payload)
            if unchecked_bytes >= 1024 * 1024:
                _require_capacity(
                    vault,
                    phase="staging",
                    additional_bytes=unchecked_bytes,
                    reserve_bytes=limits.reserve_bytes,
                )
                unchecked_bytes = 0
            handle.write(payload)

        try:
            for kind, raw, record_id, emit in con.execute(
                "SELECT kind,raw,record_id,emit FROM file_rows "
                "WHERE file_rel=? ORDER BY seq",
                (relative,),
            ):
                if kind == "non_note":
                    write_payload(bytes(raw))
                elif int(emit):
                    if relative == "index.jsonl":
                        payload = con.execute(
                            "SELECT COALESCE(index_json,public_json) FROM facts WHERE id=?",
                            (record_id,),
                        ).fetchone()[0]
                    else:
                        payload = con.execute(
                            "SELECT public_json FROM facts WHERE id=?",
                            (record_id,),
                        ).fetchone()[0]
                    write_payload((str(payload) + "\n").encode("utf-8"))
            if relative == "index.jsonl":
                query = (
                    "SELECT COALESCE(index_json,public_json) FROM facts "
                    "WHERE seen_index=0 ORDER BY first_seen_file,first_seen_seq"
                )
                params: tuple[Any, ...] = ()
            else:
                query = (
                    "SELECT public_json FROM facts "
                    "WHERE daily_relpath=? AND seen_daily=0 "
                    "ORDER BY first_seen_file,first_seen_seq"
                )
                params = (relative,)
            for (payload,) in con.execute(query, params):
                write_payload((str(payload) + "\n").encode("utf-8"))
            if unchecked_bytes:
                _require_capacity(
                    vault,
                    phase="staging",
                    additional_bytes=unchecked_bytes,
                    reserve_bytes=limits.reserve_bytes,
                )
            handle.flush()
        finally:
            if handle is not raw_handle:
                handle.close()
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
            raw_handle.close()
        digest, length = _hash_file(staging, deadline=deadline)
        entries.append(
            {
                "relative": relative,
                "sha256": digest,
                "length": length,
                "published": False,
            }
        )
    return entries


def _fsync_parent(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_fact_relative(relative: str) -> str:
    if relative == "index.jsonl":
        return relative
    _daily_date_from_relative(relative)
    return relative


def _derived_publication_paths(
    vault: Path,
    entry: dict[str, Any],
) -> tuple[Path, Path, Path]:
    if any(key in entry for key in ("target", "staging", "backup")):
        raise MigrationConflict("publication_absolute_path_forbidden")
    relative = _validate_fact_relative(str(entry.get("relative") or ""))
    vault_root = Path(vault).resolve(strict=True)
    migration_root = (_migration_dir(vault)).resolve(strict=True)
    daily_root = vault_root / "daily"
    if daily_root.is_symlink():
        raise MigrationConflict("publication_symlink_forbidden")
    daily_root.mkdir(parents=True, exist_ok=True)
    backups_root = migration_root / "backups"
    if backups_root.is_symlink():
        raise MigrationConflict("publication_symlink_forbidden")
    backups_root.mkdir(parents=True, exist_ok=True)
    target = Path(vault) / relative
    staging = _staging_path(migration_root / "staging", relative)
    backup = migration_root / "backups" / relative
    if target.is_symlink() or staging.is_symlink() or backup.is_symlink():
        raise MigrationConflict("publication_symlink_forbidden")
    target_parent = target.parent.resolve(strict=True)
    expected_target_parent = (
        vault_root
        if relative == "index.jsonl"
        else daily_root.resolve(strict=True)
    )
    if target_parent != expected_target_parent:
        raise MigrationConflict("publication_target_outside_vault")
    try:
        staging.parent.resolve(strict=True).relative_to(
            (migration_root / "staging").resolve(strict=True)
        )
    except (FileNotFoundError, ValueError) as exc:
        raise MigrationConflict("publication_staging_outside_root") from exc
    if backup.parent.is_symlink():
        raise MigrationConflict("publication_symlink_forbidden")
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        backup.parent.resolve(strict=True).relative_to(
            backups_root.resolve(strict=True)
        )
    except ValueError as exc:
        raise MigrationConflict("publication_backup_outside_root") from exc
    return target, staging, backup


def _source_snapshot(
    files: list[tuple[str, Path]],
    *,
    include_hash: bool = False,
    deadline: Optional[_Deadline] = None,
) -> dict[str, dict[str, Any]]:
    if include_hash:
        return {
            relative: _source_fingerprint(path, deadline=deadline)
            for relative, path in files
        }
    return {relative: _signature(path) for relative, path in files}


def _validate_source_snapshot(
    vault: Path,
    expected: dict[str, Any],
    *,
    deadline: Optional[_Deadline] = None,
) -> None:
    current_files = _source_files(vault, deadline=deadline)
    current = _source_snapshot(
        current_files,
        include_hash=True,
        deadline=deadline,
    )
    if current != expected:
        raise MigrationConflict("legacy_source_changed_before_publication")


def _publication_changed(vault: Path, journal: dict[str, Any]) -> bool:
    entries = journal.get("entries")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("replace_started"):
            continue
        try:
            relative = _validate_fact_relative(str(entry.get("relative") or ""))
            target = Path(vault) / relative
            before = entry.get("target_generation_before")
            if target.is_symlink() or not target.is_file():
                continue
            if "target_generation_before" not in entry:
                expected = (str(entry["sha256"]), int(entry["length"]))
                if _hash_file(target) == expected:
                    return True
                continue
            current = _signature(target)
            if before is None or (
                isinstance(before, dict) and current != before
            ):
                return True
        except (OSError, TypeError, ValueError, MigrationConflict):
            continue
    return False


def _recover_publication(
    vault: Path,
    *,
    boundary: Callable[[str], None],
    deadline: Optional[_Deadline] = None,
) -> Optional[dict[str, Any]]:
    journal_path = _migration_dir(vault) / "publication.json"
    try:
        journal = _secure_read_json(journal_path)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationConflict("publication_journal_invalid") from exc
    entries = journal.get("entries")
    source_snapshot = journal.get("source_snapshot")
    if (
        journal.get("schema_version") != 1
        or not isinstance(entries, list)
        or not isinstance(source_snapshot, dict)
        or journal.get("stage")
        not in {
            "prepared",
            "publishing",
            "publishing_zero",
            "publishing_started",
            "complete",
        }
    ):
        raise MigrationConflict("publication_journal_invalid")
    roll_forward_required = _publication_changed(vault, journal)
    if deadline is not None and not roll_forward_required:
        deadline.check()
    if journal["stage"] == "publishing":
        journal["stage"] = "publishing_zero"
        durable_atomic_json(journal_path, journal)
    index = Path(vault) / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with source_lock(index, exclusive=True):
        if journal["stage"] == "prepared":
            _validate_source_snapshot(
                vault,
                source_snapshot,
                deadline=None if roll_forward_required else deadline,
            )
            journal["stage"] = "publishing_zero"
            durable_atomic_json(journal_path, journal)
        if journal["stage"] == "publishing_zero" and entries:
            first = entries[0]
            if not isinstance(first, dict):
                raise MigrationConflict("publication_journal_invalid")
            first_target, _first_staging, _first_backup = (
                _derived_publication_paths(vault, first)
            )
            first_expected = (
                str(first.get("sha256") or ""),
                int(first.get("length") or 0),
            )
            first_hash = (
                _hash_file(
                    first_target,
                    deadline=None if roll_forward_required else deadline,
                )
                if first_target.is_file()
                else None
            )
            if first.get("replace_started") and first_hash == first_expected:
                first["published"] = True
                journal["stage"] = "publishing_started"
                durable_atomic_json(journal_path, journal)
            else:
                _validate_source_snapshot(
                    vault,
                    source_snapshot,
                    deadline=None if roll_forward_required else deadline,
                )
        for position, entry in enumerate(entries):
            if deadline is not None and not roll_forward_required:
                deadline.check()
            if not isinstance(entry, dict):
                raise MigrationConflict("publication_journal_invalid")
            target, staging, backup = _derived_publication_paths(vault, entry)
            target_hash = (
                _hash_file(
                    target,
                    deadline=None if roll_forward_required else deadline,
                )
                if target.is_file()
                else None
            )
            try:
                expected = (str(entry["sha256"]), int(entry["length"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise MigrationConflict("publication_journal_invalid") from exc
            if target_hash == expected:
                entry["published"] = True
                if position == 0 and journal["stage"] == "publishing_zero":
                    journal["stage"] = "publishing_started"
                durable_atomic_json(journal_path, journal)
                continue
            if entry.get("published"):
                raise MigrationConflict("published_target_changed")
            if not staging.is_file() or _hash_file(
                staging,
                deadline=None if roll_forward_required else deadline,
            ) != expected:
                raise MigrationConflict("publication_staging_missing")
            target.parent.mkdir(parents=True, exist_ok=True)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not backup.exists():
                shutil.copy2(target, backup)
                _fsync_parent(backup)
            if position == 0 and journal["stage"] == "publishing_zero":
                _validate_source_snapshot(
                    vault,
                    source_snapshot,
                    deadline=None if roll_forward_required else deadline,
                )
            if deadline is not None and not roll_forward_required:
                deadline.check()
            entry["target_generation_before"] = (
                _signature(target) if target.exists() else None
            )
            entry["replace_started"] = True
            durable_atomic_json(journal_path, journal)
            os.replace(staging, target)
            roll_forward_required = True
            _fsync_parent(target)
            boundary(f"publication_replaced:{entry['relative']}")
            if _hash_file(target) != expected:
                raise MigrationConflict("published_hash_mismatch")
            entry["published"] = True
            if position == 0 and journal["stage"] == "publishing_zero":
                journal["stage"] = "publishing_started"
            durable_atomic_json(journal_path, journal)
    journal["stage"] = "complete"
    durable_atomic_json(journal_path, journal)
    return journal


def _publish(
    vault: Path,
    entries: list[dict[str, Any]],
    *,
    source_snapshot: dict[str, dict[str, int]],
    boundary: Callable[[str], None],
    deadline: _Deadline,
) -> None:
    journal_path = _migration_dir(vault) / "publication.json"
    journal = {
        "schema_version": 1,
        "stage": "prepared",
        "source_snapshot": source_snapshot,
        "entries": entries,
    }
    durable_atomic_json(journal_path, journal)
    _recover_publication(vault, boundary=boundary, deadline=deadline)


def _manifest_payload(con: sqlite3.Connection) -> dict[str, Any]:
    facts = int(con.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
    distinct_sources = int(
        con.execute(
            "SELECT COUNT(DISTINCT source_relative) FROM facts "
            "WHERE source_relative IS NOT NULL"
        ).fetchone()[0]
    )
    if distinct_sources > MAX_MANIFEST_SOURCES:
        raise MigrationConflict("manifest_source_limit")
    sources: dict[str, dict[str, Any]] = {}
    for record_id, public_json, relative, timestamp in con.execute(
        "SELECT id,public_json,source_relative,source_timestamp FROM facts "
        "WHERE source_relative IS NOT NULL "
        "ORDER BY source_timestamp DESC,id"
    ):
        if relative in sources:
            continue
        row = json.loads(public_json)
        content = str(row.get("content") or "")
        sources[str(relative)] = {
            "record_id": str(record_id),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "size": len(content.encode("utf-8")),
            "mtime": 0,
            "migrated_at": str(timestamp),
        }
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "migration_status": "complete",
        "migration_version": MIGRATION_VERSION,
        "migration_completed_at": _now(),
        "index_rebuild_required": True,
        "last_successful_tx": None,
        "pending_transactions": [],
        "applied_transactions": [],
        "sources": sources,
        "stats": {
            "committed_transactions": 0,
            "migrated_note_facts": facts,
        },
    }


def _completed_manifest(
    vault: Path,
    con: sqlite3.Connection,
    *,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    manifest = payload or _manifest_payload(con)
    durable_atomic_json(manifest_path(vault), manifest)
    return manifest


def _cleanup_completed_publication(vault: Path) -> None:
    root = _migration_dir(vault)
    journal = root / "publication.json"
    _secure_unlink(journal, missing_ok=True)
    staging = root / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    _fsync_parent(journal)


def _migrate_notes_locked(
    vault_dir: Path,
    *,
    limits: Optional[MigrationLimits] = None,
    boundary: Optional[Callable[[str], None]] = None,
    deadline: _Deadline,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    vault = Path(vault_dir)
    limits = limits or MigrationLimits()
    callback = boundary or (lambda _stage: None)
    publication_path = _migration_dir(vault) / "publication.json"
    try:
        publication_exists = _secure_regular_exists(publication_path)
    except OSError:
        return {
            "status": "error",
            "error_code": "migration_publication_failed",
            "error_stage": "publication",
            "error_type": "MigrationConflict",
            "production_changed": False,
        }
    if publication_exists:
        try:
            recovered_publication = _recover_publication(
                vault,
                boundary=callback,
                deadline=deadline,
            )
            publication_changed = bool(
                recovered_publication
                and _publication_changed(vault, recovered_publication)
            )
            checkpoint = _load_checkpoint(vault)
            if not isinstance(checkpoint, dict):
                raise CatalogBindingError("migration_checkpoint_missing")
            catalog_id = checkpoint.get("catalog_id")
            if not isinstance(catalog_id, str) or not catalog_id:
                raise CatalogBindingError("migration_checkpoint_catalog_id_missing")
            catalog = _connect_catalog(
                _catalog_path(vault),
                reset=False,
                catalog_id=catalog_id,
            )
            try:
                _validate_catalog_progress(catalog, checkpoint)
                duplicates = int(
                    catalog.execute(
                        "SELECT COALESCE(SUM(MAX(seen_index-1,0)+MAX(seen_daily-1,0)),0) "
                        "FROM facts"
                    ).fetchone()[0]
                )
                manifest_payload = _manifest_payload(catalog)
                _completed_manifest(vault, catalog, payload=manifest_payload)
            finally:
                catalog.close()
            checkpoint["stage"] = "complete"
            _write_checkpoint(vault, checkpoint)
            _cleanup_completed_publication(vault)
            return {
                "status": "ok",
                "migration_version": MIGRATION_VERSION,
                "checkpoint_resumed": True,
                "publication_recovered": recovered_publication is not None,
                "duplicates_compacted": duplicates,
                "index_rebuild_required": True,
                "production_changed": publication_changed,
            }
        except MigrationDeadline:
            try:
                deadline_journal = _secure_read_json(
                    _migration_dir(vault) / "publication.json"
                )
                deadline_changed = _publication_changed(vault, deadline_journal)
            except Exception:
                deadline_changed = False
            return {
                "status": "partial",
                "error_code": "migration_limit_reached",
                "error_stage": "publication",
                "limit_kind": "time",
                "production_changed": deadline_changed,
            }
        except Exception as exc:
            try:
                failed_journal = _secure_read_json(publication_path)
                production_changed = _publication_changed(vault, failed_journal)
            except Exception:
                production_changed = False
            return {
                "status": "error",
                "error_code": "migration_publication_failed",
                "error_stage": "publication",
                "error_type": type(exc).__name__,
                "production_changed": production_changed,
            }
    existing_manifest_path = manifest_path(vault)
    try:
        existing_manifest = _secure_read_json(existing_manifest_path)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        existing_manifest = None
    if existing_manifest is not None:
        if (
            isinstance(existing_manifest, dict)
            and _manifest_semantically_valid(existing_manifest)
            and existing_manifest.get("schema_version") == MANIFEST_SCHEMA_VERSION
            and existing_manifest.get("migration_status") == "complete"
            and existing_manifest.get("migration_version") == MIGRATION_VERSION
        ):
            return {
                "status": "ok",
                "migration_version": MIGRATION_VERSION,
                "already_migrated": True,
                "index_rebuild_required": bool(
                    existing_manifest.get("index_rebuild_required")
                ),
                "production_changed": False,
            }
    try:
        deadline.check()
        discovered_files = _source_files(vault)
    except MigrationDeadline:
        return {
            "status": "partial",
            "error_code": "migration_limit_reached",
            "limit_kind": "time",
            "production_changed": False,
        }
    except (MigrationConflict, OSError):
        return {
            "status": "error",
            "error_code": "notes_migration_source_invalid",
            "production_changed": False,
        }
    try:
        checkpoint = _load_checkpoint(vault)
        checkpoint_exists = _secure_regular_exists(_checkpoint_path(vault))
    except OSError:
        return {
            "status": "error",
            "error_code": "notes_migration_scratch_invalid",
            "production_changed": False,
        }
    resumed = checkpoint is not None
    if checkpoint_exists and checkpoint is None:
        return {
            "status": "error",
            "error_code": "notes_migration_scratch_invalid",
            "production_changed": False,
        }
    if checkpoint is not None and checkpoint.get("migration_version") != MIGRATION_VERSION:
        reset_run_id = checkpoint.get("reset_run_id")
        if not isinstance(reset_run_id, str) or not reset_run_id:
            reset_run_id = uuid.uuid4().hex
            checkpoint["reset_run_id"] = reset_run_id
            _write_checkpoint(vault, checkpoint)
        return {
            "status": "error",
            "error_code": "notes_migration_legacy_scratch",
            "found_version": checkpoint.get("migration_version"),
            "required_version": MIGRATION_VERSION,
            "run_id": reset_run_id,
            "reset_command": (
                "immortal notes-migrate-reset --run-id "
                f"{reset_run_id} --json"
            ),
            "production_changed": False,
        }
    if checkpoint is None:
        source_plan = [relative for relative, _path in discovered_files]
        source_signatures = _source_snapshot(discovered_files)
        catalog_id = uuid.uuid4().hex
        checkpoint = {
            "migration_version": MIGRATION_VERSION,
            "run_id": uuid.uuid4().hex,
            "catalog_id": catalog_id,
            "catalog_generation": 0,
            "file_index": 0,
            "offset": 0,
            "completed_signatures": {},
            "source_plan": source_plan,
            "source_signatures": source_signatures,
        }
        _write_checkpoint(vault, checkpoint)
        reset = True
    else:
        source_plan = checkpoint.get("source_plan")
        source_signatures = checkpoint.get("source_signatures")
        if (
            not isinstance(source_plan, list)
            or not isinstance(source_signatures, dict)
            or [relative for relative, _path in discovered_files] != source_plan
            or _source_snapshot(discovered_files) != source_signatures
        ):
            return {
                "status": "error",
                "error_code": "notes_migration_reset_required",
                "run_id": checkpoint.get("run_id"),
                "reset_command": (
                    "immortal notes-migrate-reset --run-id "
                    f"{checkpoint.get('run_id')} --json"
                ),
                "production_changed": False,
            }
        reset = False
    files = [
        (relative, vault / _validate_fact_relative(str(relative)))
        for relative in checkpoint["source_plan"]
    ]
    try:
        catalog_id = checkpoint.get("catalog_id")
        if not isinstance(catalog_id, str) or not catalog_id:
            raise CatalogBindingError("migration_checkpoint_catalog_id_missing")
        con = _connect_catalog(
            _catalog_path(vault),
            reset=reset,
            catalog_id=catalog_id,
        )
        _validate_catalog_progress(con, checkpoint)
    except CatalogBindingError:
        try:
            con.close()
        except (NameError, sqlite3.Error):
            pass
        return {
            "status": "error",
            "error_code": "notes_migration_reset_required",
            "run_id": checkpoint.get("run_id"),
            "reset_command": (
                "immortal notes-migrate-reset --run-id "
                f"{checkpoint.get('run_id')} --json"
            ),
            "production_changed": False,
        }
    except (OSError, sqlite3.DatabaseError):
        return {
            "status": "error",
            "error_code": "notes_migration_scratch_invalid",
            "production_changed": False,
        }
    try:
        try:
            complete, run_stats = _scan_catalog(
                vault,
                con,
                files,
                checkpoint,
                limits,
                deadline,
            )
        except MigrationCapacity as exc:
            return {
                "status": "error",
                "error_code": "migration_insufficient_scratch_space",
                "error_stage": exc.phase,
                "required_bytes": exc.required_bytes,
                "available_bytes": exc.available_bytes,
                "reserve_bytes": exc.reserve_bytes,
                "production_changed": False,
            }
        except MigrationExpansionLimit:
            return {
                "status": "error",
                "error_code": "migration_gzip_expansion_limit",
                "error_stage": "gzip_inflate",
                "production_changed": False,
            }
        except MigrationDeadline:
            return {
                "status": "partial",
                "error_code": "migration_limit_reached",
                "limit_kind": "time",
                "production_changed": False,
                "checkpoint_resumed": resumed,
            }
        except MigrationConflict:
            return {
                "status": "error",
                "error_code": "notes_migration_conflict",
                "production_changed": False,
            }
        except Exception as exc:
            return {
                "status": "error",
                "error_code": "migration_interrupted",
                "error_stage": "catalog_checkpoint",
                "error_type": type(exc).__name__,
                "production_changed": False,
            }
        if not complete:
            return {
                "status": "partial",
                "error_code": "migration_limit_reached",
                "production_changed": False,
                "checkpoint_resumed": resumed,
                **run_stats,
            }
        duplicates = int(
            con.execute(
                "SELECT COALESCE(SUM(MAX(seen_index-1,0)+MAX(seen_daily-1,0)),0) "
                "FROM facts"
            ).fetchone()[0]
        )
        try:
            manifest_payload = _manifest_payload(con)
        except MigrationConflict:
            return {
                "status": "error",
                "error_code": "notes_migration_conflict",
                "error_stage": "manifest_capacity",
                "production_changed": False,
            }
        try:
            staging_upper_bound = sum(path.stat().st_size for _relative, path in files)
            _require_capacity(
                vault,
                phase="staging",
                additional_bytes=staging_upper_bound,
                reserve_bytes=limits.reserve_bytes,
            )
            entries = _write_staging(vault, con, files, limits, deadline)
            publication_source_snapshot = dict(
                checkpoint.get("completed_signatures") or {}
            )
            if set(publication_source_snapshot) != {
                relative for relative, _path in files
            }:
                raise MigrationConflict("source_plan_incomplete")
            callback("staging_verified")
            deadline.check()
        except MigrationCapacity as exc:
            return {
                "status": "error",
                "error_code": "migration_insufficient_scratch_space",
                "error_stage": exc.phase,
                "required_bytes": exc.required_bytes,
                "available_bytes": exc.available_bytes,
                "reserve_bytes": exc.reserve_bytes,
                "production_changed": False,
            }
        except MigrationDeadline:
            try:
                deadline_journal = _secure_read_json(
                    _migration_dir(vault) / "publication.json"
                )
                deadline_changed = _publication_changed(vault, deadline_journal)
            except Exception:
                deadline_changed = False
            return {
                "status": "partial",
                "error_code": "migration_limit_reached",
                "error_stage": "staging_verified",
                "limit_kind": "time",
                "production_changed": deadline_changed,
            }
        except Exception as exc:
            try:
                failed_journal = _secure_read_json(
                    _migration_dir(vault) / "publication.json"
                )
                failed_changed = _publication_changed(vault, failed_journal)
            except Exception:
                failed_changed = False
            return {
                "status": "error",
                "error_code": "migration_interrupted",
                "error_stage": "staging_verified",
                "error_type": type(exc).__name__,
                "production_changed": False,
            }
        try:
            publication_bytes = sum(
                (vault / str(entry["relative"])).stat().st_size
                for entry in entries
                if (vault / str(entry["relative"])).is_file()
            )
            _require_capacity(
                vault,
                phase="publication",
                additional_bytes=publication_bytes,
                reserve_bytes=limits.reserve_bytes,
            )
            _publish(
                vault,
                entries,
                source_snapshot=publication_source_snapshot,
                boundary=callback,
                deadline=deadline,
            )
            callback("published")
            _completed_manifest(vault, con, payload=manifest_payload)
        except MigrationCapacity as exc:
            return {
                "status": "error",
                "error_code": "migration_insufficient_scratch_space",
                "error_stage": exc.phase,
                "required_bytes": exc.required_bytes,
                "available_bytes": exc.available_bytes,
                "reserve_bytes": exc.reserve_bytes,
                "production_changed": False,
            }
        except MigrationDeadline:
            try:
                deadline_journal = _secure_read_json(
                    _migration_dir(vault) / "publication.json"
                )
                deadline_changed = _publication_changed(vault, deadline_journal)
            except Exception:
                deadline_changed = False
            return {
                "status": "partial",
                "error_code": "migration_limit_reached",
                "error_stage": "publication",
                "limit_kind": "time",
                "production_changed": deadline_changed,
            }
        except Exception as exc:
            try:
                failed_journal = _secure_read_json(
                    _migration_dir(vault) / "publication.json"
                )
                failed_changed = _publication_changed(vault, failed_journal)
            except Exception:
                failed_changed = False
            return {
                "status": "error",
                "error_code": "migration_publication_failed",
                "error_stage": "publication",
                "error_type": type(exc).__name__,
                "production_changed": failed_changed,
            }
        checkpoint["stage"] = "complete"
        _write_checkpoint(vault, checkpoint)
        _cleanup_completed_publication(vault)
        return {
            "status": "ok",
            "migration_version": MIGRATION_VERSION,
            "checkpoint_resumed": resumed,
            "duplicates_compacted": duplicates,
            "index_rebuild_required": True,
            "production_changed": True,
            **run_stats,
        }
    finally:
        con.close()


def migrate_notes(
    vault_dir: Path,
    *,
    limits: Optional[MigrationLimits] = None,
    boundary: Optional[Callable[[str], None]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    vault = Path(vault_dir)
    try:
        vault_metadata = os.lstat(vault)
    except FileNotFoundError:
        vault_metadata = None
    if vault_metadata is not None and (
        stat.S_ISLNK(vault_metadata.st_mode)
        or not stat.S_ISDIR(vault_metadata.st_mode)
    ):
        return {
            "status": "error",
            "error_code": "notes_migration_source_invalid",
            "production_changed": False,
        }
    resolved_limits = limits or MigrationLimits()
    deadline = _Deadline(
        clock=clock,
        expires_at=clock() + resolved_limits.max_seconds,
    )
    root = _migration_dir(vault)
    lock_path = root / "migration.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_fd: Optional[int] = None
    try:
        directory_fd = _open_directory_fd(root, create=True)
        fd = os.open(lock_path.name, flags, 0o600, dir_fd=directory_fd)
    except OSError:
        if directory_fd is not None:
            os.close(directory_fd)
        return {
            "status": "error",
            "error_code": "notes_migration_busy",
            "production_changed": False,
        }
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "status": "error",
                "error_code": "notes_migration_busy",
                "production_changed": False,
            }
        return _migrate_notes_locked(
            vault,
            limits=resolved_limits,
            boundary=boundary,
            deadline=deadline,
            clock=clock,
        )
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            if directory_fd is not None:
                os.close(directory_fd)


def reset_migration_scratch(vault_dir: Path, *, run_id: str) -> dict[str, Any]:
    vault = Path(vault_dir)
    root = _migration_dir(vault)
    lock_path = root / "migration.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_fd: Optional[int] = None
    try:
        directory_fd = _open_directory_fd(root, create=True)
        fd = os.open(lock_path.name, flags, 0o600, dir_fd=directory_fd)
    except OSError:
        if directory_fd is not None:
            os.close(directory_fd)
        return {
            "status": "error",
            "error_code": "notes_migration_busy",
            "production_changed": False,
        }
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "status": "error",
                "error_code": "notes_migration_busy",
                "production_changed": False,
            }
        publication = root / "publication.json"
        if publication.exists():
            return {
                "status": "error",
                "error_code": "notes_migration_reset_unsafe_publication_exists",
                "production_changed": False,
            }
        checkpoint = _load_checkpoint(vault)
        expected = (
            checkpoint.get("run_id") or checkpoint.get("reset_run_id")
            if isinstance(checkpoint, dict)
            else None
        )
        if not expected or run_id != expected:
            return {
                "status": "error",
                "error_code": "notes_migration_reset_run_id_mismatch",
                "production_changed": False,
            }
        removed: list[str] = []
        catalog = _catalog_path(vault)
        for candidate in (
            _checkpoint_path(vault),
            catalog,
            Path(f"{catalog}-wal"),
            Path(f"{catalog}-shm"),
        ):
            try:
                candidate.unlink()
                removed.append(str(candidate.relative_to(vault)))
            except FileNotFoundError:
                pass
        for directory in (
            _inflate_dir(vault),
            root / "staging",
            root / "backups",
        ):
            if directory.exists():
                shutil.rmtree(directory)
                removed.append(str(directory.relative_to(vault)))
        _fsync_parent(root / "checkpoint.json")
        return {
            "status": "ok",
            "reset_run_id": run_id,
            "removed": removed,
            "production_changed": False,
        }
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            if directory_fd is not None:
                os.close(directory_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate legacy Obsidian note facts")
    parser.add_argument("--vault-dir", required=True)
    parser.add_argument("--max-files", type=int, default=10000)
    parser.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    parser.add_argument("--max-seconds", type=float, default=3600.0)
    parser.add_argument(
        "--max-compressed-bytes",
        type=int,
        default=2 * 1024 * 1024 * 1024,
    )
    parser.add_argument("--reserve-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument(
        "--max-gzip-expanded-bytes",
        type=int,
        default=8 * 1024 * 1024 * 1024,
    )
    parser.add_argument("--reset-scratch", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.reset_scratch:
        result = (
            reset_migration_scratch(Path(args.vault_dir), run_id=args.run_id)
            if args.run_id
            else {
                "status": "error",
                "error_code": "notes_migration_reset_run_id_required",
                "production_changed": False,
            }
        )
    else:
        result = migrate_notes(
            Path(args.vault_dir),
            limits=MigrationLimits(
                args.max_files,
                args.max_bytes,
                args.max_seconds,
                args.max_compressed_bytes,
                args.reserve_bytes,
                args.max_gzip_expanded_bytes,
            ),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["status"])
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
