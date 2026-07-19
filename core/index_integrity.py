#!/usr/bin/env python3
"""Build and reconcile the disposable SQLite search read model safely."""

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import stat as stat_module
from pathlib import Path
from typing import Dict, Optional, Set, Tuple


SOURCE_STABILITY_ATTEMPTS = 3


class IndexIntegrityError(RuntimeError):
    """The source or staging database failed a loss-prevention check."""


class SourceChangedError(IndexIntegrityError):
    """The source changed while a consistency-sensitive scan was running."""


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
    with Path(path).open("rb") as handle:
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


def _stat_signature(value: os.stat_result) -> Tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _revision(
    value: os.stat_result,
    content_sha256: str,
) -> Dict[str, object]:
    return {
        "dev": value.st_dev,
        "ino": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "content_sha256": content_sha256,
        "prefix_sha256": content_sha256,
    }


def _read_source_revision_once(path: Path) -> Dict[str, object]:
    path = Path(path)
    path_before = path.stat()
    digest = hashlib.sha256()
    read_size = 0
    with path.open("rb") as handle:
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
    path_after = path.stat()
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


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS docs("
        "rowid INTEGER PRIMARY KEY, rec_id TEXT, ts TEXT, source TEXT, "
        "role TEXT, project TEXT, content TEXT)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_rec_id ON docs(rec_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_source ON docs(source)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_ts ON docs(ts)")
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


def _insert_record(con: sqlite3.Connection, row: Dict) -> None:
    content = row.get("content", "") or ""
    cur = con.execute(
        "INSERT INTO docs(rec_id,ts,source,role,project,content) "
        "VALUES(?,?,?,?,?,?)",
        (
            str(row.get("id") or ""),
            str(row.get("timestamp") or ""),
            str(row.get("source") or ""),
            str(row.get("role") or ""),
            str(row.get("project") or ""),
            str(content),
        ),
    )
    con.execute(
        "INSERT INTO docs_fts(rowid,content) VALUES(?,?)",
        (cur.lastrowid, str(content)),
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
    with source.open("rb") as handle:
        handle.seek(start_offset)
        line_number = start_line_number
        while True:
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
            _insert_record(con, row)
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


def _scan_jsonl_once(
    source: Path,
    connection: Optional[sqlite3.Connection] = None,
) -> Tuple[Set[str], Dict[str, object]]:
    source = Path(source)
    path_before = source.stat()
    ids: Set[str] = set()
    digest = hashlib.sha256()
    read_size = 0
    with source.open("rb") as handle:
        fd_before = os.fstat(handle.fileno())
        if _stat_signature(fd_before) != _stat_signature(path_before):
            raise SourceChangedError("source identity changed before JSONL scan")
        try:
            for line_number, raw in enumerate(handle, 1):
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
                    _insert_record(connection, row)
        except IndexIntegrityError as exc:
            if _stat_signature(source.stat()) != _stat_signature(path_before):
                raise SourceChangedError("source changed during JSONL scan") from exc
            raise
        fd_after = os.fstat(handle.fileno())
    path_after = source.stat()
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
    }


def _db_state(database: Path, immutable: bool = False) -> Dict[str, object]:
    if not database.exists():
        return _empty_db_state(False)
    try:
        with _readonly_connect(database, immutable=immutable) as con:
            return _db_state_from_connection(con)
    except (sqlite3.DatabaseError, ValueError):
        return _empty_db_state(True)


def _rebuild_reason(source: Path, state: Dict[str, object]) -> Optional[str]:
    if not state["exists"]:
        return "database_missing"
    last_size = int(state["last_size"])
    if source.stat().st_size < last_size:
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
    _meta_set(con, "prefix_sha256", revision["content_sha256"])
    _meta_set(con, "indexed_id_count", id_count)
    _meta_set(con, "indexed_ids_sha256", ids_sha256(ids))


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
    return {
        "jsonl_unique_ids": len(expected_ids),
        "sqlite_ids": len(db_ids),
        "missing_in_sqlite": sorted(source_only),
        "missing_in_jsonl": sorted(database_only),
        "source_unique_ids": len(expected_ids),
        "database_unique_ids": len(db_ids),
        "source_only_ids": len(source_only),
        "database_only_ids": len(database_only),
    }


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
    for _attempt in range(max_attempts):
        _cleanup_staging(staging)
        try:
            result, scanned_revision = _build_staging_once(source, staging)
            final_revision = source_revision(source, max_attempts=1)
        except SourceChangedError:
            _cleanup_staging(staging)
            continue
        except Exception:
            _cleanup_staging(staging)
            raise
        if scanned_revision != final_revision:
            _cleanup_staging(staging)
            continue
        _replace_database(staging, database)
        return result
    _cleanup_staging(staging)
    raise SourceChangedError(
        f"source changed during staging replacement after {max_attempts} attempts"
    )


def _replace_database(staging: Path, database: Path) -> None:
    # Do not checkpoint the current database here. A checkpoint would mutate
    # its bytes before the atomic replacement and break loss-free rollback if
    # os.replace fails. The staging database is a complete DELETE-journal
    # snapshot, so legacy WAL contents are neither needed nor copied.
    os.replace(str(staging), str(database))
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    _fsync_file(database)
    _fsync_directory(database.parent)


def _incremental_sync(source: Path, database: Path) -> Dict:
    with sqlite3.connect(str(database), timeout=30) as con:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("BEGIN IMMEDIATE")
        fresh = _db_state_from_connection(con)
        reason = _rebuild_reason(source, fresh)
        if reason:
            con.rollback()
            return {"retry_rebuild": reason}
        last_size = int(fresh["last_size"])
        last_line = int(fresh["last_line"])
        source_before = _stat_signature(source.stat())
        existing_ids = {
            str(row[0])
            for row in con.execute("SELECT rec_id FROM docs").fetchall()
        }
        added, processed_offset, ids = _load_source(
            con,
            source,
            start_offset=last_size,
            start_line_number=last_line,
            known_ids=existing_ids,
        )
        # Recheck the indexed prefix after reading the append range. If a
        # non-cooperating writer rewrote earlier bytes during this transaction,
        # never bless the mixed view with a new fingerprint.
        if sha256_prefix(source, last_size) != fresh["prefix_sha256"]:
            con.rollback()
            return {"retry_rebuild": "prefix_mismatch"}
        docs_count = int(con.execute("SELECT count(*) FROM docs").fetchone()[0])
        unique_count = int(
            con.execute("SELECT count(DISTINCT rec_id) FROM docs").fetchone()[0]
        )
        fts_count = int(con.execute("SELECT count(*) FROM docs_fts").fetchone()[0])
        if docs_count != len(ids) or unique_count != len(ids) or fts_count != len(ids):
            con.rollback()
            return {"retry_rebuild": "id_count_mismatch"}
        revision = source_revision(source, max_attempts=1)
        revision_signature = (
            int(revision["dev"]),
            int(revision["ino"]),
            int(revision["size"]),
            int(revision["mtime_ns"]),
        )
        if (
            revision_signature != source_before
            or int(revision["size"]) != processed_offset
        ):
            con.rollback()
            raise SourceChangedError(
                "source changed after incremental scan"
            )
        _write_metadata(con, revision, ids)
        con.commit()
    return {
        "mode": "incremental" if added else "current",
        "action": "incremental" if added else "current",
        "reason": "append_only",
        "added": added,
        "jsonl_unique_ids": len(ids),
        "sqlite_ids": len(ids),
        "missing_in_sqlite": [],
        "missing_in_jsonl": [],
        "source_unique_ids": len(ids),
        "database_unique_ids": len(ids),
        "source_only_ids": 0,
        "database_only_ids": 0,
    }


def _db_state_from_connection(con: sqlite3.Connection) -> Dict[str, object]:
    database_ids = [
        str(row[0])
        for row in con.execute("SELECT rec_id FROM docs").fetchall()
    ]
    count = len(database_ids)
    unique_count = len(set(database_ids))
    fts_count = int(con.execute("SELECT count(*) FROM docs_fts").fetchone()[0])
    indexed = _meta_get(con, "indexed_id_count")
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
    }


def _report_only(source: Path, database: Path) -> Dict:
    source_ids, revision = _scan_source_ids_once(source)
    database_ids = _database_ids(database, immutable=True)
    state = _db_state(database, immutable=True)
    reason = _rebuild_reason(source, state)
    source_only = source_ids - database_ids
    database_only = database_ids - source_ids
    if source_only or database_only:
        reason = "id_set_mismatch"
    return {
        "mode": "report_only",
        "action": "report_only",
        "reason": reason or "in_sync",
        "jsonl_unique_ids": len(source_ids),
        "sqlite_ids": len(database_ids),
        "missing_in_sqlite": sorted(source_only),
        "missing_in_jsonl": sorted(database_only),
        "source_unique_ids": len(source_ids),
        "database_unique_ids": len(database_ids),
        "source_only_ids": len(source_only),
        "database_only_ids": len(database_only),
        "source_size": revision["size"],
        "database_exists": database.exists(),
        "_source_revision": revision,
    }


def report_index_integrity(source: Path, database: Path) -> Dict:
    """Return a strictly read-only bidirectional source/database report."""
    source = Path(source)
    database = Path(database)
    for _attempt in range(SOURCE_STABILITY_ATTEMPTS):
        before = _strict_snapshot_signature(database)
        try:
            report = _report_only(source, database)
            final_source_revision = source_revision(source, max_attempts=1)
        except SourceChangedError:
            continue
        after = _strict_snapshot_signature(database)
        if before != after:
            raise IndexIntegrityError(
                "search index changed during strict read-only report"
            )
        scanned_revision = report.pop("_source_revision")
        if scanned_revision == final_source_revision:
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
    database_parent = database.parent.resolve()
    if staging.parent.resolve() != database_parent:
        raise ValueError(
            "unsafe staging path: must be a direct child of database.parent"
        )
    forbidden = (
        source,
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
        lock_path,
    )
    if any(_paths_collide(staging, candidate) for candidate in forbidden):
        raise ValueError("unsafe staging path: conflicts with protected index path")
    if staging.is_symlink():
        raise ValueError("unsafe staging path: symlink is not allowed")
    if staging.exists() and not stat_module.S_ISREG(staging.lstat().st_mode):
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
    if not source.exists():
        raise FileNotFoundError(source)
    if report_only:
        return report_index_integrity(source, database)

    database.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(database) + ".reconcile.lock")
    staging = (
        Path(staging_path)
        if staging_path is not None
        else Path(str(database) + ".staging")
    )
    staging = _validate_staging_path(source, database, staging, lock_path)
    with lock_path.open("a+b") as lock:
        _acquire_lock(lock)
        if force_rebuild:
            reason = "forced"
        elif not database.exists():
            reason = "database_missing"
        else:
            try:
                incremental = _incremental_sync(source, database)
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
        printable[key + "_count"] = len(printable.pop(key, []))
    print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
