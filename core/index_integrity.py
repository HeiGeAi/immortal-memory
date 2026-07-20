#!/usr/bin/env python3
"""Build and reconcile the disposable SQLite search read model safely."""

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import stat as stat_module
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Set, Tuple

from index_locks import (
    database_lock_path,
    index_lock_pair,
    source_lock,
    source_lock_path,
)
from index_publication import (
    RecoveryError,
    cleanup_rollback_artifacts,
    create_rollback_artifacts,
    replace_database,
    restore_rollback_artifacts,
)


SOURCE_STABILITY_ATTEMPTS = 3
MISSING_ID_SAMPLE_LIMIT = 100
INDEX_SCHEMA_VERSION = 2
LOCATOR_COLUMNS = frozenset(
    {
        "source_offset",
        "source_length",
        "line_number",
        "content_sha256",
    }
)
LOCATOR_INDEXES = {
    "idx_docs_rec_id": ("rec_id", None),
    "idx_docs_source_offset": ("source_offset", True),
    "idx_docs_line_number": ("line_number", True),
}


class IndexIntegrityError(RuntimeError):
    """The source or staging database failed a loss-prevention check."""


class SourceChangedError(IndexIntegrityError):
    """The source changed while a consistency-sensitive scan was running."""


def _normalized_source_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(str(path)))
    for system_root in (Path("/var"), Path("/tmp")):
        try:
            relative = candidate.relative_to(system_root)
        except ValueError:
            continue
        if system_root.is_symlink():
            return Path(os.path.realpath(str(system_root))) / relative
        break
    return candidate


def _safe_source_stat(path: Path) -> os.stat_result:
    candidate = _normalized_source_path(path)
    current = candidate
    while True:
        try:
            metadata = os.lstat(str(current))
        except FileNotFoundError:
            if current == candidate:
                raise
            raise IndexIntegrityError(
                "unsafe source path: ancestor is missing"
            )
        except OSError as exc:
            raise IndexIntegrityError(
                "unsafe source path: cannot inspect path chain"
            ) from exc
        if stat_module.S_ISLNK(metadata.st_mode):
            raise IndexIntegrityError(
                "unsafe source path: symlinks are not allowed"
            )
        parent = current.parent
        if parent == current:
            break
        current = parent
    source_stat = os.lstat(str(candidate))
    if not stat_module.S_ISREG(source_stat.st_mode):
        raise IndexIntegrityError(
            "unsafe source path: regular file required"
        )
    return source_stat


@contextmanager
def _open_source_nofollow(path: Path) -> Iterator[Any]:
    candidate = _normalized_source_path(path)
    expected = _safe_source_stat(candidate)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise IndexIntegrityError(
            "unsafe source path: cannot open source safely"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        current = _safe_source_stat(candidate)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or _stat_signature(opened) != _stat_signature(expected)
            or _stat_signature(current) != _stat_signature(expected)
        ):
            raise SourceChangedError(
                "source identity changed during safe open"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(descriptor)


def ids_sha256(ids) -> str:
    """Return an unambiguous deterministic digest for a collection of IDs."""
    digest = hashlib.sha256()
    for rec_id in sorted(str(value) for value in ids):
        encoded = rec_id.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def sha256_prefix(path: Path, length: int) -> str:
    """Return SHA-256 for exactly ``length`` bytes from ``path``."""
    if length < 0:
        raise ValueError("length must be non-negative")
    digest = hashlib.sha256()
    remaining = length
    with _open_source_nofollow(Path(path)) as handle:
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise EOFError(
                    "source ended before requested prefix: "
                    f"wanted={length} read={length - remaining}"
                )
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _stat_signature(value: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _revision(
    value: os.stat_result,
    content_sha256: str,
) -> Dict[str, object]:
    return {
        "dev": value.st_dev,
        "ino": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "content_sha256": content_sha256,
        "prefix_sha256": content_sha256,
    }


def _read_source_revision_once(path: Path) -> Dict[str, object]:
    path = Path(path)
    path_before = _safe_source_stat(path)
    digest = hashlib.sha256()
    read_size = 0
    with _open_source_nofollow(path) as handle:
        fd_before = os.fstat(handle.fileno())
        if _stat_signature(fd_before) != _stat_signature(path_before):
            raise SourceChangedError("source identity changed before revision scan")
        while True:
            block = handle.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
            read_size += len(block)
        fd_after = os.fstat(handle.fileno())
    path_after = _safe_source_stat(path)
    expected = _stat_signature(path_before)
    if (
        _stat_signature(fd_after) != expected
        or _stat_signature(path_after) != expected
        or read_size != path_before.st_size
    ):
        raise SourceChangedError("source changed during revision scan")
    return _revision(path_before, digest.hexdigest())


def source_revision(
    path: Path,
    max_attempts: int = SOURCE_STABILITY_ATTEMPTS,
) -> Dict[str, object]:
    """Return a stat-bound whole-file content revision after finite retries."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    for _attempt in range(max_attempts):
        try:
            return _read_source_revision_once(Path(path))
        except SourceChangedError:
            continue
    raise SourceChangedError(
        f"source changed during revision scan after {max_attempts} attempts"
    )


def _revision_stat_matches(
    path: Path,
    revision: Dict[str, object],
) -> bool:
    try:
        value = _safe_source_stat(Path(path))
    except (OSError, IndexIntegrityError):
        return False
    return _stat_signature(value) == (
        int(revision["dev"]),
        int(revision["ino"]),
        int(revision["size"]),
        int(revision["mtime_ns"]),
        int(revision["ctime_ns"]),
    )


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS docs("
        "rowid INTEGER PRIMARY KEY, rec_id TEXT, ts TEXT, source TEXT, "
        "role TEXT, project TEXT, content TEXT, "
        "source_offset INTEGER NOT NULL, source_length INTEGER NOT NULL, "
        "line_number INTEGER NOT NULL, content_sha256 TEXT NOT NULL)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_rec_id ON docs(rec_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_source ON docs(source)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_ts ON docs(ts)")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_source_offset "
        "ON docs(source_offset)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_line_number "
        "ON docs(line_number)"
    )
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
        "content, tokenize='trigram')"
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")


def _meta_get(con: sqlite3.Connection, key: str, default=None):
    try:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.DatabaseError:
        return default
    return row[0] if row else default


def _meta_set(con: sqlite3.Connection, key: str, value: object) -> None:
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def locator_schema_is_current(con: sqlite3.Connection) -> bool:
    """Verify locator columns and the actual semantics of every named index."""
    try:
        columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info(docs)")
        }
        if (
            not LOCATOR_COLUMNS.issubset(columns)
            or str(_meta_get(con, "index_schema_version"))
            != str(INDEX_SCHEMA_VERSION)
        ):
            return False
        index_rows = {
            str(row[1]): row
            for row in con.execute("PRAGMA index_list(docs)")
        }
        for name, (expected_column, expected_unique) in LOCATOR_INDEXES.items():
            index_row = index_rows.get(name)
            if index_row is None:
                return False
            is_unique = bool(index_row[2])
            is_partial = bool(index_row[4])
            if is_partial or (
                expected_unique is not None
                and is_unique is not expected_unique
            ):
                return False
            key_columns = [
                row
                for row in con.execute(
                    "SELECT cid,name,coll,key FROM pragma_index_xinfo(?) "
                    "ORDER BY seqno",
                    (name,),
                )
                if bool(row[3])
            ]
            if (
                len(key_columns) != 1
                or int(key_columns[0][0]) < 0
                or str(key_columns[0][1]) != expected_column
                or str(key_columns[0][2]).upper() != "BINARY"
            ):
                return False
    except (IndexError, sqlite3.DatabaseError):
        return False
    return True


def _insert_record(
    con: sqlite3.Connection,
    row: Dict,
    *,
    source_offset: int,
    source_length: int,
    line_number: int,
) -> None:
    content = row.get("content", "") or ""
    content_text = str(content)
    content_sha256 = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
    cur = con.execute(
        "INSERT INTO docs("
        "rec_id,ts,source,role,project,content,"
        "source_offset,source_length,line_number,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            str(row.get("id") or ""),
            str(row.get("timestamp") or ""),
            str(row.get("source") or ""),
            str(row.get("role") or ""),
            str(row.get("project") or ""),
            content_text,
            source_offset,
            source_length,
            line_number,
            content_sha256,
        ),
    )
    con.execute(
        "INSERT INTO docs_fts(rowid,content) VALUES(?,?)",
        (cur.lastrowid, content_text),
    )


def _decode_record(raw: bytes, line_number: int) -> Dict:
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IndexIntegrityError(f"malformed JSONL at line {line_number}: {exc}") from exc
    if not isinstance(row, dict):
        raise IndexIntegrityError(f"malformed JSONL at line {line_number}: object required")
    if not str(row.get("id") or "").strip():
        raise IndexIntegrityError(f"missing record id at line {line_number}")
    return row


def _load_source(
    con: sqlite3.Connection,
    source: Path,
    start_offset: int = 0,
    start_line_number: int = 0,
    known_ids: Optional[Set[str]] = None,
) -> Tuple[int, int, Set[str]]:
    ids = set(known_ids or ())
    added = 0
    with _open_source_nofollow(source) as handle:
        handle.seek(start_offset)
        line_number = start_line_number
        while True:
            source_offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            line_number += 1
            if not raw.endswith(b"\n"):
                raise IndexIntegrityError(
                    f"malformed JSONL at line {line_number}: unterminated record"
                )
            row = _decode_record(raw, line_number)
            rec_id = str(row["id"])
            if rec_id in ids:
                raise IndexIntegrityError(
                    f"duplicate record id at line {line_number}"
                )
            ids.add(rec_id)
            _insert_record(
                con,
                row,
                source_offset=source_offset,
                source_length=len(raw),
                line_number=line_number,
            )
            added += 1
        processed_offset = handle.tell()
    return added, processed_offset, ids


def _readonly_connect(
    database: Path,
    immutable: bool = False,
) -> sqlite3.Connection:
    uri = database.resolve().as_uri() + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _database_ids(database: Path, immutable: bool = False) -> Set[str]:
    if not database.exists():
        return set()
    try:
        with _readonly_connect(database, immutable=immutable) as con:
            return {
                str(row[0])
                for row in con.execute("SELECT rec_id FROM docs").fetchall()
            }
    except sqlite3.DatabaseError as exc:
        raise IndexIntegrityError(
            f"cannot read SQLite index {database}: {exc}"
        ) from exc


def _database_snapshot(
    database: Path,
    immutable: bool = False,
) -> Tuple[Set[str], Dict[str, object]]:
    if not database.exists():
        return set(), _empty_db_state(False)
    with _readonly_connect(database, immutable=immutable) as con:
        database_ids = {
            str(row[0])
            for row in con.execute("SELECT rec_id FROM docs")
        }
        return database_ids, _db_state_from_connection(con, database_ids)


def _scan_jsonl_once(
    source: Path,
    connection: Optional[sqlite3.Connection] = None,
) -> Tuple[Set[str], Dict[str, object]]:
    source = Path(source)
    path_before = _safe_source_stat(source)
    ids: Set[str] = set()
    digest = hashlib.sha256()
    read_size = 0
    with _open_source_nofollow(source) as handle:
        fd_before = os.fstat(handle.fileno())
        if _stat_signature(fd_before) != _stat_signature(path_before):
            raise SourceChangedError("source identity changed before JSONL scan")
        try:
            line_number = 0
            while True:
                source_offset = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                line_number += 1
                digest.update(raw)
                read_size += len(raw)
                if not raw.endswith(b"\n"):
                    raise IndexIntegrityError(
                        f"malformed JSONL at line {line_number}: unterminated record"
                    )
                row = _decode_record(raw, line_number)
                rec_id = str(row["id"])
                if rec_id in ids:
                    raise IndexIntegrityError(
                        f"duplicate record id at line {line_number}"
                    )
                ids.add(rec_id)
                if connection is not None:
                    _insert_record(
                        connection,
                        row,
                        source_offset=source_offset,
                        source_length=len(raw),
                        line_number=line_number,
                    )
        except IndexIntegrityError as exc:
            if _stat_signature(_safe_source_stat(source)) != _stat_signature(
                path_before
            ):
                raise SourceChangedError("source changed during JSONL scan") from exc
            raise
        fd_after = os.fstat(handle.fileno())
    path_after = _safe_source_stat(source)
    expected = _stat_signature(path_before)
    if (
        _stat_signature(fd_after) != expected
        or _stat_signature(path_after) != expected
        or read_size != path_before.st_size
    ):
        raise SourceChangedError("source changed during JSONL scan")
    return ids, _revision(path_before, digest.hexdigest())


def _scan_source_ids_once(source: Path) -> Tuple[Set[str], Dict[str, object]]:
    return _scan_jsonl_once(source)


def _empty_db_state(exists: bool) -> Dict[str, object]:
    return {
        "exists": exists,
        "last_size": 0,
        "last_line": 0,
        "prefix_sha256": None,
        "record_count": 0,
        "unique_id_count": 0,
        "fts_count": 0,
        "indexed_id_count": None,
        "indexed_ids_sha256": None,
        "actual_ids_sha256": ids_sha256(set()),
        "source_dev": None,
        "source_ino": None,
        "source_mtime_ns": None,
        "source_ctime_ns": None,
        "locator_schema_current": False,
        "locator_count": 0,
        "locator_invalid_count": 0,
        "locator_overlap_count": 0,
        "locator_topology_invalid_count": 0,
    }


def _db_state(database: Path, immutable: bool = False) -> Dict[str, object]:
    if not database.exists():
        return _empty_db_state(False)
    try:
        _database_ids_value, state = _database_snapshot(
            database,
            immutable=immutable,
        )
        return state
    except (sqlite3.DatabaseError, ValueError):
        return _empty_db_state(True)


def _rebuild_reason(
    source: Path,
    state: Dict[str, object],
    revision: Optional[Dict[str, object]] = None,
) -> Optional[str]:
    if not state["exists"]:
        return "database_missing"
    if not state["locator_schema_current"]:
        return "locator_schema_missing"
    last_size = int(state["last_size"])
    source_size = (
        int(revision["size"])
        if revision is not None
        else _safe_source_stat(source).st_size
    )
    if source_size < last_size:
        return "source_shrank"
    if not state["prefix_sha256"]:
        return "fingerprint_missing"
    if (
        state["indexed_id_count"] != state["record_count"]
        or state["unique_id_count"] != state["record_count"]
        or state["fts_count"] != state["record_count"]
    ):
        return "id_count_mismatch"
    if not state["indexed_ids_sha256"]:
        return "id_digest_missing"
    if state["indexed_ids_sha256"] != state["actual_ids_sha256"]:
        return "id_digest_mismatch"
    if (
        state["locator_count"] != state["record_count"]
        or state["locator_invalid_count"]
        or state["locator_overlap_count"]
        or state["locator_topology_invalid_count"]
    ):
        return "locator_invalid"
    if revision is not None and source_size == last_size:
        current_prefix = str(revision["content_sha256"])
    else:
        try:
            current_prefix = sha256_prefix(source, last_size)
        except EOFError:
            return "source_shrank"
    if current_prefix != state["prefix_sha256"]:
        return "prefix_mismatch"
    return None


def _write_metadata(
    con: sqlite3.Connection,
    revision: Dict[str, object],
    ids: Set[str],
) -> None:
    processed_offset = int(revision["size"])
    id_count = len(ids)
    _meta_set(con, "last_offset", processed_offset)
    _meta_set(con, "last_size", processed_offset)
    _meta_set(con, "last_line", id_count)
    _meta_set(con, "source_size", processed_offset)
    _meta_set(con, "source_dev", revision["dev"])
    _meta_set(con, "source_ino", revision["ino"])
    _meta_set(con, "source_mtime_ns", revision["mtime_ns"])
    _meta_set(con, "source_ctime_ns", revision["ctime_ns"])
    _meta_set(con, "prefix_sha256", revision["content_sha256"])
    _meta_set(con, "indexed_id_count", id_count)
    _meta_set(con, "indexed_ids_sha256", ids_sha256(ids))
    _meta_set(con, "parity_status", "trusted")
    _meta_set(con, "index_schema_version", INDEX_SCHEMA_VERSION)


def _parity_result(source_ids: Set[str], database_ids: Set[str]) -> Dict:
    source_only = source_ids - database_ids
    database_only = database_ids - source_ids
    return {
        "jsonl_unique_ids": len(source_ids),
        "sqlite_ids": len(database_ids),
        "missing_in_sqlite": sorted(source_only),
        "missing_in_jsonl": sorted(database_only),
        "source_unique_ids": len(source_ids),
        "database_unique_ids": len(database_ids),
        "source_only_ids": len(source_only),
        "database_only_ids": len(database_only),
    }


def _bounded_parity_result(
    source_ids: Set[str],
    database_ids: Set[str],
    sample_limit: int = MISSING_ID_SAMPLE_LIMIT,
) -> Dict:
    source_only = source_ids - database_ids
    database_only = database_ids - source_ids
    source_sample = sorted(source_only)[:sample_limit]
    database_sample = sorted(database_only)[:sample_limit]
    return {
        "jsonl_unique_ids": len(source_ids),
        "sqlite_ids": len(database_ids),
        "missing_in_sqlite": source_sample,
        "missing_in_jsonl": database_sample,
        "missing_in_sqlite_count": len(source_only),
        "missing_in_jsonl_count": len(database_only),
        "missing_in_sqlite_truncated": len(source_only) > len(source_sample),
        "missing_in_jsonl_truncated": len(database_only) > len(database_sample),
        "source_unique_ids": len(source_ids),
        "database_unique_ids": len(database_ids),
        "source_only_ids": len(source_only),
        "database_only_ids": len(database_only),
    }


def verify_id_parity(staging: Path, expected_ids: Set[str]) -> Dict:
    with sqlite3.connect(str(staging)) as con:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise IndexIntegrityError(f"staging integrity_check failed: {integrity}")
        db_ids = {
            str(row[0])
            for row in con.execute("SELECT rec_id FROM docs").fetchall()
        }
        source_only = expected_ids - db_ids
        database_only = db_ids - expected_ids
        if source_only or database_only:
            raise IndexIntegrityError(
                "staging ID reconciliation failed: "
                f"source_only={len(source_only)} database_only={len(database_only)}"
            )
        docs_count = int(con.execute("SELECT count(*) FROM docs").fetchone()[0])
        fts_count = int(con.execute("SELECT count(*) FROM docs_fts").fetchone()[0])
        if docs_count != len(db_ids) or docs_count != len(expected_ids):
            raise IndexIntegrityError(
                "staging unique ID count mismatch: "
                f"docs={docs_count} sqlite_ids={len(db_ids)} "
                f"jsonl_ids={len(expected_ids)}"
            )
        if docs_count != fts_count:
            raise IndexIntegrityError(
                f"staging FTS count mismatch: docs={docs_count} fts={fts_count}"
            )
        state = _db_state_from_connection(con, db_ids)
        if (
            not state["locator_schema_current"]
            or state["locator_count"] != docs_count
            or state["locator_invalid_count"]
            or state["locator_overlap_count"]
            or state["locator_topology_invalid_count"]
        ):
            raise IndexIntegrityError("staging locator validation failed")
    return _parity_result(expected_ids, db_ids)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_staging(staging: Path) -> None:
    for candidate in (
        staging,
        Path(str(staging) + "-wal"),
        Path(str(staging) + "-shm"),
        Path(str(staging) + "-journal"),
    ):
        if candidate.is_symlink():
            raise ValueError("unsafe staging path: symlink artifact")
        if not candidate.exists():
            continue
        if not stat_module.S_ISREG(candidate.lstat().st_mode):
            raise ValueError("unsafe staging path: non-regular artifact")
        candidate.unlink()


def _build_staging_once(
    source: Path,
    staging: Path,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    with sqlite3.connect(str(staging), timeout=30) as con:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")
        _ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        ids, revision = _scan_jsonl_once(source, connection=con)
        _write_metadata(con, revision, ids)
        con.commit()
    result = verify_id_parity(staging, ids)
    _fsync_file(staging)
    return result, revision


def _build_and_replace_staging(
    source: Path,
    staging: Path,
    database: Path,
    max_attempts: int = SOURCE_STABILITY_ATTEMPTS,
) -> Dict[str, object]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    staging.parent.mkdir(parents=True, exist_ok=True)
    source_changed_after_replace = False
    rollback_artifacts: Optional[Dict[Path, Optional[Path]]] = None
    for _attempt in range(max_attempts):
        _cleanup_staging(staging)
        try:
            result, scanned_revision = _build_staging_once(source, staging)
        except SourceChangedError:
            _cleanup_staging(staging)
            continue
        except Exception:
            _cleanup_staging(staging)
            if rollback_artifacts is not None:
                cleanup_rollback_artifacts(
                    database,
                    rollback_artifacts,
                    _fsync_directory,
                )
            raise
        if not _revision_stat_matches(source, scanned_revision):
            _cleanup_staging(staging)
            continue
        if rollback_artifacts is None:
            rollback_artifacts = create_rollback_artifacts(
                database,
                _fsync_directory,
            )
        try:
            _replace_database(staging, database)
        except Exception:
            restore_rollback_artifacts(
                database,
                rollback_artifacts,
                _fsync_file,
                _fsync_directory,
            )
            cleanup_rollback_artifacts(
                database,
                rollback_artifacts,
                _fsync_directory,
            )
            raise
        if not _revision_stat_matches(source, scanned_revision):
            # This proves point-in-time parity at return. A writer that does not
            # share this reconciler's lock can still append after this check.
            restore_rollback_artifacts(
                database,
                rollback_artifacts,
                _fsync_file,
                _fsync_directory,
            )
            source_changed_after_replace = True
            continue
        cleanup_rollback_artifacts(
            database,
            rollback_artifacts,
            _fsync_directory,
        )
        return result
    _cleanup_staging(staging)
    if rollback_artifacts is not None:
        cleanup_rollback_artifacts(
            database,
            rollback_artifacts,
            _fsync_directory,
        )
    if source_changed_after_replace:
        raise SourceChangedError(
            f"source changed after staging replace after {max_attempts} attempts"
        )
    raise SourceChangedError(
        f"source changed during staging replacement after {max_attempts} attempts"
    )


def _replace_database(staging: Path, database: Path) -> None:
    replace_database(
        staging,
        database,
        _fsync_file,
        _fsync_directory,
    )


def _incremental_sync(
    source: Path,
    database: Path,
    expected_source_ids: Set[str],
    expected_revision: Dict[str, object],
    state: Dict[str, object],
    existing_ids: Set[str],
) -> Dict:
    with sqlite3.connect(str(database), timeout=30) as con:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("BEGIN IMMEDIATE")
        last_size = int(state["last_size"])
        last_line = int(state["last_line"])
        added, processed_offset, ids = _load_source(
            con,
            source,
            start_offset=last_size,
            start_line_number=last_line,
            known_ids=existing_ids,
        )
        docs_count = int(con.execute("SELECT count(*) FROM docs").fetchone()[0])
        unique_count = int(
            con.execute("SELECT count(DISTINCT rec_id) FROM docs").fetchone()[0]
        )
        fts_count = int(con.execute("SELECT count(*) FROM docs_fts").fetchone()[0])
        if docs_count != len(ids) or unique_count != len(ids) or fts_count != len(ids):
            con.rollback()
            return {"retry_rebuild": "id_count_mismatch"}
        if ids != expected_source_ids:
            con.rollback()
            return {"retry_rebuild": "id_set_mismatch"}
        if int(expected_revision["size"]) != processed_offset:
            con.rollback()
            raise SourceChangedError(
                "source changed after incremental scan"
            )
        if not _revision_stat_matches(source, expected_revision):
            con.rollback()
            raise SourceChangedError("source changed after incremental scan")
        _write_metadata(con, expected_revision, ids)
        con.commit()
    parity = _parity_result(expected_source_ids, ids)
    return {
        "mode": "incremental" if added else "current",
        "action": "incremental" if added else "current",
        "reason": "append_only",
        "added": added,
        **parity,
    }


def _mark_current(
    source: Path,
    database: Path,
    revision: Dict[str, object],
    ids: Set[str],
) -> Dict:
    with sqlite3.connect(str(database), timeout=30) as con:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("BEGIN IMMEDIATE")
        if not _revision_stat_matches(source, revision):
            con.rollback()
            raise SourceChangedError("source changed before current watermark")
        _write_metadata(con, revision, ids)
        con.commit()
    return {
        "mode": "current",
        "action": "current",
        "reason": "in_sync",
        "added": 0,
        **_parity_result(ids, ids),
    }


def _db_state_from_connection(
    con: sqlite3.Connection,
    database_ids: Optional[Set[str]] = None,
) -> Dict[str, object]:
    if database_ids is None:
        database_ids = {
            str(row[0])
            for row in con.execute("SELECT rec_id FROM docs")
        }
    count = int(con.execute("SELECT count(*) FROM docs").fetchone()[0])
    unique_count = len(database_ids)
    fts_count = int(con.execute("SELECT count(*) FROM docs_fts").fetchone()[0])
    indexed = _meta_get(con, "indexed_id_count")
    locator_schema_current = locator_schema_is_current(con)
    locator_count = 0
    locator_invalid_count = 0
    locator_overlap_count = 0
    locator_topology_invalid_count = 0
    if locator_schema_current:
        last_size = int(_meta_get(con, "last_size", 0) or 0)
        locator_count = int(
            con.execute(
                "SELECT count(*) FROM docs "
                "WHERE source_offset IS NOT NULL "
                "AND source_length IS NOT NULL "
                "AND line_number IS NOT NULL "
                "AND content_sha256 IS NOT NULL"
            ).fetchone()[0]
        )
        locator_invalid_count = int(
            con.execute(
                "SELECT count(*) FROM docs "
                "WHERE typeof(source_offset) != 'integer' "
                "OR typeof(source_length) != 'integer' "
                "OR typeof(line_number) != 'integer' "
                "OR source_offset < 0 OR source_length <= 0 "
                "OR line_number <= 0 "
                "OR source_offset + source_length > ? "
                "OR length(content_sha256) != 64 "
                "OR content_sha256 GLOB '*[^0-9a-f]*'",
                (last_size,),
            ).fetchone()[0]
        )
        locator_overlap_count = int(
            con.execute(
                "SELECT count(*) FROM ("
                "SELECT source_offset,source_length,"
                "lag(source_offset + source_length) OVER "
                "(ORDER BY source_offset) AS previous_end "
                "FROM docs"
                ") WHERE previous_end > source_offset"
            ).fetchone()[0]
        )
        locator_topology_invalid_count = int(
            con.execute(
                "SELECT count(*) FROM ("
                "SELECT source_offset,source_length,line_number,"
                "row_number() OVER (ORDER BY line_number) AS expected_line,"
                "count(*) OVER () AS total_lines,"
                "lag(source_offset + source_length) OVER "
                "(ORDER BY line_number) AS previous_end,"
                "lag(source_offset) OVER "
                "(ORDER BY line_number) AS previous_offset "
                "FROM docs"
                ") WHERE line_number != expected_line "
                "OR (expected_line = 1 AND source_offset != 0) "
                "OR (expected_line > 1 AND previous_end != source_offset) "
                "OR (expected_line > 1 AND previous_offset >= source_offset) "
                "OR (expected_line = total_lines "
                "AND source_offset + source_length != ?)",
                (last_size,),
            ).fetchone()[0]
        )
    return {
        "exists": True,
        "last_size": int(_meta_get(con, "last_size", 0) or 0),
        "last_line": int(_meta_get(con, "last_line", count) or count),
        "prefix_sha256": _meta_get(con, "prefix_sha256"),
        "record_count": count,
        "unique_id_count": unique_count,
        "fts_count": fts_count,
        "indexed_id_count": int(indexed) if indexed is not None else None,
        "indexed_ids_sha256": _meta_get(con, "indexed_ids_sha256"),
        "actual_ids_sha256": ids_sha256(database_ids),
        "source_dev": _meta_get(con, "source_dev"),
        "source_ino": _meta_get(con, "source_ino"),
        "source_mtime_ns": _meta_get(con, "source_mtime_ns"),
        "source_ctime_ns": _meta_get(con, "source_ctime_ns"),
        "locator_schema_current": locator_schema_current,
        "locator_count": locator_count,
        "locator_invalid_count": locator_invalid_count,
        "locator_overlap_count": locator_overlap_count,
        "locator_topology_invalid_count": locator_topology_invalid_count,
    }


def _report_only(source: Path, database: Path) -> Dict:
    source_ids, revision = _scan_source_ids_once(source)
    database_ids, state = _database_snapshot(database, immutable=True)
    reason = _rebuild_reason(source, state, revision)
    source_only = source_ids - database_ids
    database_only = database_ids - source_ids
    if source_only or database_only:
        reason = "id_set_mismatch"
    return {
        "mode": "report_only",
        "action": "report_only",
        "reason": reason or "in_sync",
        **_bounded_parity_result(source_ids, database_ids),
        "source_size": revision["size"],
        "database_exists": database.exists(),
        "_source_revision": revision,
    }


def report_index_integrity(source: Path, database: Path) -> Dict:
    """Return a strictly read-only bidirectional source/database report."""
    source = Path(source)
    database = Path(database)
    if database.parent.exists():
        with index_lock_pair(
            source,
            database,
            source_exclusive=False,
            database_exclusive=False,
        ):
            return _report_index_integrity_locked(source, database)
    with source_lock(source, exclusive=False):
        return _report_index_integrity_locked(source, database)


def _report_index_integrity_locked(source: Path, database: Path) -> Dict:
    for _attempt in range(SOURCE_STABILITY_ATTEMPTS):
        before = _strict_snapshot_signature(database)
        try:
            report = _report_only(source, database)
        except SourceChangedError:
            continue
        after = _strict_snapshot_signature(database)
        if before != after:
            raise IndexIntegrityError(
                "search index changed during strict read-only report"
            )
        scanned_revision = report.pop("_source_revision")
        if _revision_stat_matches(source, scanned_revision):
            return report
    raise SourceChangedError(
        "source changed during stable scan after "
        f"{SOURCE_STABILITY_ATTEMPTS} attempts"
    )


def _strict_snapshot_signature(database: Path) -> Optional[Tuple[int, int, int, int]]:
    wal = Path(str(database) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise IndexIntegrityError(
            "search index has a non-empty WAL; strict read-only report unavailable"
        )
    if not database.exists():
        return None
    stat = database.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _acquire_lock(lock) -> None:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)


def _paths_collide(first: Path, second: Path) -> bool:
    if first.absolute() == second.absolute():
        return True
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True
    if first.exists() and second.exists():
        try:
            return os.path.samefile(str(first), str(second))
        except OSError:
            return False
    return False


def _validate_staging_path(
    source: Path,
    database: Path,
    staging: Path,
    lock_path: Path,
) -> Path:
    staging = staging.absolute()
    database_parent = database.parent.resolve(strict=False)
    if staging.parent.resolve(strict=False) != database_parent:
        raise ValueError(
            "unsafe staging path: must be a direct child of database.parent"
        )
    protected = (
        source,
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
        lock_path,
        source_lock_path(source),
        database_lock_path(database),
    )
    artifacts = (
        staging,
        Path(str(staging) + "-wal"),
        Path(str(staging) + "-shm"),
        Path(str(staging) + "-journal"),
    )
    for artifact in artifacts:
        if any(_paths_collide(artifact, candidate) for candidate in protected):
            raise ValueError(
                "unsafe staging path: conflicts with protected index path"
            )
        if artifact.is_symlink():
            raise ValueError("unsafe staging path: symlink is not allowed")
        if artifact.exists() and not stat_module.S_ISREG(artifact.lstat().st_mode):
            raise ValueError("unsafe staging path: regular file required")
    return staging


def reconcile_index(
    source: Path,
    database: Path,
    report_only: bool = False,
    staging_path: Optional[Path] = None,
    force_rebuild: bool = False,
) -> Dict:
    """Reconcile ``database`` against the authoritative JSONL ``source``."""
    source = Path(source)
    database = Path(database)
    if not os.path.lexists(str(source)):
        raise FileNotFoundError(source)
    _safe_source_stat(source)
    if _paths_collide(source, database):
        raise ValueError("source and database paths must not collide")
    if report_only:
        return report_index_integrity(source, database)

    lock_path = Path(str(database) + ".reconcile.lock")
    staging = (
        Path(staging_path)
        if staging_path is not None
        else Path(str(database) + ".staging")
    )
    staging = _validate_staging_path(source, database, staging, lock_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with index_lock_pair(
        source,
        database,
        source_exclusive=False,
        database_exclusive=True,
    ):
        with lock_path.open("a+b") as lock:
            _acquire_lock(lock)
            if force_rebuild:
                reason = "forced"
            elif not database.exists():
                reason = "database_missing"
            else:
                try:
                    source_ids, revision = _scan_source_ids_once(source)
                    database_ids, state = _database_snapshot(database)
                    reason = _rebuild_reason(source, state, revision)
                    source_only = source_ids - database_ids
                    database_only = database_ids - source_ids
                    sets_match = not source_only and not database_only
                    append_candidate = (
                        bool(source_only)
                        and not database_only
                        and database_ids < source_ids
                    )
                    if sets_match and reason is None:
                        return _mark_current(
                            source,
                            database,
                            revision,
                            database_ids,
                        )
                    if append_candidate and reason is None:
                        incremental = _incremental_sync(
                            source,
                            database,
                            source_ids,
                            revision,
                            state,
                            database_ids,
                        )
                    else:
                        if reason is None:
                            reason = "id_set_mismatch"
                        incremental = {"retry_rebuild": reason}
                except sqlite3.DatabaseError:
                    reason = "database_invalid"
                else:
                    retry_reason = incremental.pop("retry_rebuild", None)
                    if retry_reason is None:
                        return incremental
                    reason = retry_reason

            parity = _build_and_replace_staging(source, staging, database)
            return {
                "mode": "full_rebuild",
                "action": "rebuilt",
                "reason": reason,
                "added": parity["database_unique_ids"],
                **parity,
            }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--staging-path")
    args = parser.parse_args(argv)
    report = reconcile_index(
        Path(args.source).expanduser(),
        Path(args.database).expanduser(),
        report_only=args.report_only,
        staging_path=Path(args.staging_path).expanduser() if args.staging_path else None,
    )
    printable = dict(report)
    for key in ("missing_in_sqlite", "missing_in_jsonl"):
        sample = printable.pop(key, [])
        printable.setdefault(key + "_count", len(sample))
    print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
