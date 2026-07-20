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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from index_locks import source_lock
from notes_transactions import (
    MANIFEST_SCHEMA_VERSION,
    durable_atomic_json,
    manifest_path,
)


MIGRATION_VERSION = 1
MAX_MANIFEST_SOURCES = 1000
DAILY_RELATIVE_PATTERN = re.compile(
    r"^daily/(\d{4}-\d{2}-\d{2})\.jsonl(?:\.gz)?$"
)


class MigrationConflict(RuntimeError):
    pass


def after_catalog_commit_before_checkpoint() -> None:
    """Fault-injection boundary for catalog/checkpoint crash tests."""


@dataclass(frozen=True)
class MigrationLimits:
    max_files: int = 10000
    max_bytes: int = 2 * 1024 * 1024 * 1024
    max_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_files < 1 or self.max_bytes < 1 or self.max_seconds < 0:
            raise ValueError("migration limits must be non-negative and bounded")


def _migration_dir(vault: Path) -> Path:
    return Path(vault) / "notes" / "migration"


def _checkpoint_path(vault: Path) -> Path:
    return _migration_dir(vault) / "checkpoint.json"


def _catalog_path(vault: Path) -> Path:
    return _migration_dir(vault) / "catalog.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_files(vault: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    index = Path(vault) / "index.jsonl"
    if index.is_file():
        files.append(("index.jsonl", index))
    daily = Path(vault) / "daily"
    try:
        with os.scandir(daily) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if (
                    entry.name.endswith(".jsonl")
                    or entry.name.endswith(".jsonl.gz")
                )
                and entry.is_file(follow_symlinks=False)
                and not entry.is_symlink()
            )
    except FileNotFoundError:
        names = []
    for name in names:
        files.append((f"daily/{name}", daily / name))
    return files


def _connect_catalog(path: Path, *, reset: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if reset:
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
    con = sqlite3.connect(path)
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
          seen_daily INTEGER NOT NULL DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS non_notes(
          file_rel TEXT NOT NULL,
          seq INTEGER NOT NULL,
          raw BLOB NOT NULL,
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
        """
    )
    con.commit()
    return con


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
            "INSERT OR REPLACE INTO non_notes(file_rel,seq,raw) VALUES(?,?,?)",
            (file_rel, seq, raw),
        )
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
            "source_relative,source_timestamp,seen_index,seen_daily) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                record_id,
                public_json,
                json.dumps(row, ensure_ascii=False, sort_keys=True) if is_index else None,
                daily_relpath,
                source_relative,
                source_timestamp,
                1 if is_index else 0,
                0 if is_index else 1,
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
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
        "dev": metadata.st_dev,
        "ino": metadata.st_ino,
    }


def _validate_completed_sources(
    files: list[tuple[str, Path]],
    checkpoint: dict[str, Any],
) -> None:
    signatures = checkpoint.get("completed_signatures") or {}
    by_rel = dict(files)
    for relative, expected in signatures.items():
        path = by_rel.get(relative)
        if path is None or _signature(path) != expected:
            raise MigrationConflict("legacy_source_changed")


def _scan_catalog(
    vault: Path,
    con: sqlite3.Connection,
    files: list[tuple[str, Path]],
    checkpoint: dict[str, Any],
    limits: MigrationLimits,
    clock: Callable[[], float],
) -> tuple[bool, dict[str, int]]:
    started = clock()
    file_index = int(checkpoint.get("file_index") or 0)
    offset = int(checkpoint.get("offset") or 0)
    files_started = 0
    bytes_read = 0
    rows_since_checkpoint = 0
    bytes_since_checkpoint = 0
    _validate_completed_sources(files, checkpoint)
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
        if clock() - started >= limits.max_seconds:
            break
        try:
            raw_handle = path.open("rb")
            handle = (
                gzip.GzipFile(fileobj=raw_handle, mode="rb")
                if relative.endswith(".gz")
                else raw_handle
            )
            try:
                handle.seek(offset)
                seq = offset
                while True:
                    if clock() - started >= limits.max_seconds:
                        break
                    remaining = limits.max_bytes - bytes_read
                    if remaining <= 0:
                        break
                    raw = handle.readline(remaining)
                    if not raw:
                        if checkpoint.get("active_signature") != _signature(path):
                            raise MigrationConflict("legacy_source_changed")
                        offset = 0
                        checkpoint.setdefault("completed_signatures", {})[
                            relative
                        ] = _signature(path)
                        con.execute(
                            "INSERT OR REPLACE INTO scanned_files(file_rel,size,mtime_ns) "
                            "VALUES(?,?,?)",
                            (
                                relative,
                                checkpoint["completed_signatures"][relative]["size"],
                                checkpoint["completed_signatures"][relative]["mtime_ns"],
                            ),
                        )
                        con.commit()
                        after_catalog_commit_before_checkpoint()
                        file_index += 1
                        checkpoint["file_index"] = file_index
                        checkpoint["offset"] = 0
                        checkpoint.pop("active_file", None)
                        checkpoint.pop("active_signature", None)
                        _write_checkpoint(vault, checkpoint)
                        rows_since_checkpoint = 0
                        bytes_since_checkpoint = 0
                        break
                    bytes_read += len(raw)
                    if not raw.endswith(b"\n"):
                        break
                    bytes_since_checkpoint += len(raw)
                    rows_since_checkpoint += 1
                    _catalog_row(con, relative, raw, seq)
                    offset += len(raw)
                    seq = offset
                    checkpoint["file_index"] = file_index
                    checkpoint["offset"] = offset
                    if (
                        rows_since_checkpoint >= 500
                        or bytes_since_checkpoint >= 1024 * 1024
                    ):
                        con.commit()
                        after_catalog_commit_before_checkpoint()
                        _write_checkpoint(vault, checkpoint)
                        rows_since_checkpoint = 0
                        bytes_since_checkpoint = 0
                if offset != 0 or bytes_read >= limits.max_bytes:
                    con.commit()
                    after_catalog_commit_before_checkpoint()
                    checkpoint["file_index"] = file_index
                    checkpoint["offset"] = offset
                    _write_checkpoint(vault, checkpoint)
                    break
            finally:
                if handle is not raw_handle:
                    handle.close()
                raw_handle.close()
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise MigrationConflict("invalid_legacy_gzip") from exc
    complete = file_index >= len(files)
    return complete, {
        "files_processed": files_started,
        "bytes_processed": bytes_read,
    }


def _staging_path(root: Path, relative: str) -> Path:
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    suffix = ".jsonl.gz" if relative.endswith(".gz") else ".jsonl"
    return root / f"{digest}{suffix}"


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _write_staging(
    vault: Path,
    con: sqlite3.Connection,
    files: list[tuple[str, Path]],
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
        try:
            for kind, raw, record_id, emit in con.execute(
                "SELECT kind,raw,record_id,emit FROM file_rows "
                "WHERE file_rel=? ORDER BY seq",
                (relative,),
            ):
                if kind == "non_note":
                    handle.write(bytes(raw))
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
                    handle.write((str(payload) + "\n").encode("utf-8"))
            if relative == "index.jsonl":
                query = "SELECT COALESCE(index_json,public_json) FROM facts WHERE seen_index=0 ORDER BY id"
                params: tuple[Any, ...] = ()
            else:
                query = (
                    "SELECT public_json FROM facts "
                    "WHERE daily_relpath=? AND seen_daily=0 ORDER BY id"
                )
                params = (relative,)
            for (payload,) in con.execute(query, params):
                handle.write((str(payload) + "\n").encode("utf-8"))
            handle.flush()
        finally:
            if handle is not raw_handle:
                handle.close()
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
            raw_handle.close()
        digest, length = _hash_file(staging)
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


def _source_snapshot(files: list[tuple[str, Path]]) -> dict[str, dict[str, int]]:
    return {relative: _signature(path) for relative, path in files}


def _validate_source_snapshot(
    vault: Path,
    expected: dict[str, Any],
) -> None:
    current_files = _source_files(vault)
    current = _source_snapshot(current_files)
    if current != expected:
        raise MigrationConflict("legacy_source_changed_before_publication")


def _recover_publication(
    vault: Path,
    *,
    boundary: Callable[[str], None],
) -> Optional[dict[str, Any]]:
    journal_path = _migration_dir(vault) / "publication.json"
    if not journal_path.is_file():
        return None
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationConflict("publication_journal_invalid") from exc
    entries = journal.get("entries")
    source_snapshot = journal.get("source_snapshot")
    if (
        journal.get("schema_version") != 1
        or not isinstance(entries, list)
        or not isinstance(source_snapshot, dict)
        or journal.get("stage") not in {"prepared", "publishing", "complete"}
    ):
        raise MigrationConflict("publication_journal_invalid")
    index = Path(vault) / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with source_lock(index, exclusive=True):
        if journal["stage"] == "prepared":
            _validate_source_snapshot(vault, source_snapshot)
            journal["stage"] = "publishing"
            durable_atomic_json(journal_path, journal)
        for entry in entries:
            if not isinstance(entry, dict):
                raise MigrationConflict("publication_journal_invalid")
            target, staging, backup = _derived_publication_paths(vault, entry)
            target_hash = _hash_file(target) if target.is_file() else None
            try:
                expected = (str(entry["sha256"]), int(entry["length"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise MigrationConflict("publication_journal_invalid") from exc
            if target_hash == expected:
                entry["published"] = True
                durable_atomic_json(journal_path, journal)
                continue
            if entry.get("published"):
                raise MigrationConflict("published_target_changed")
            if not staging.is_file() or _hash_file(staging) != expected:
                raise MigrationConflict("publication_staging_missing")
            target.parent.mkdir(parents=True, exist_ok=True)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not backup.exists():
                shutil.copy2(target, backup)
                _fsync_parent(backup)
            os.replace(staging, target)
            _fsync_parent(target)
            boundary(f"publication_replaced:{entry['relative']}")
            if _hash_file(target) != expected:
                raise MigrationConflict("published_hash_mismatch")
            entry["published"] = True
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
) -> None:
    journal_path = _migration_dir(vault) / "publication.json"
    journal = {
        "schema_version": 1,
        "stage": "prepared",
        "source_snapshot": source_snapshot,
        "entries": entries,
    }
    durable_atomic_json(journal_path, journal)
    _recover_publication(vault, boundary=boundary)


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
    try:
        journal.unlink()
    except FileNotFoundError:
        pass
    staging = root / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    _fsync_parent(journal)


def _migrate_notes_locked(
    vault_dir: Path,
    *,
    limits: Optional[MigrationLimits] = None,
    boundary: Optional[Callable[[str], None]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    vault = Path(vault_dir)
    limits = limits or MigrationLimits()
    callback = boundary or (lambda _stage: None)
    publication_path = _migration_dir(vault) / "publication.json"
    if publication_path.is_file():
        try:
            recovered_publication = _recover_publication(
                vault,
                boundary=callback,
            )
            catalog = _connect_catalog(_catalog_path(vault), reset=False)
            try:
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
            checkpoint = _load_checkpoint(vault) or {
                "migration_version": MIGRATION_VERSION
            }
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
                "production_changed": True,
            }
        except Exception as exc:
            return {
                "status": "error",
                "error_code": "migration_publication_failed",
                "error_stage": "publication",
                "error_type": type(exc).__name__,
                "production_changed": True,
            }
    existing_manifest_path = manifest_path(vault)
    if existing_manifest_path.is_file():
        try:
            existing_manifest = json.loads(
                existing_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            existing_manifest = None
        if (
            isinstance(existing_manifest, dict)
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
    discovered_files = _source_files(vault)
    checkpoint = _load_checkpoint(vault)
    resumed = checkpoint is not None
    if checkpoint is None or checkpoint.get("migration_version") != MIGRATION_VERSION:
        source_plan = [relative for relative, _path in discovered_files]
        source_signatures = _source_snapshot(discovered_files)
        checkpoint = {
            "migration_version": MIGRATION_VERSION,
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
                "error_code": "notes_migration_conflict",
                "production_changed": False,
            }
        reset = False
    files = [
        (relative, vault / _validate_fact_relative(str(relative)))
        for relative in checkpoint["source_plan"]
    ]
    con = _connect_catalog(_catalog_path(vault), reset=reset)
    try:
        try:
            complete, run_stats = _scan_catalog(
                vault,
                con,
                files,
                checkpoint,
                limits,
                clock,
            )
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
            entries = _write_staging(vault, con, files)
            publication_source_snapshot = dict(
                checkpoint.get("completed_signatures") or {}
            )
            if set(publication_source_snapshot) != {
                relative for relative, _path in files
            }:
                raise MigrationConflict("source_plan_incomplete")
            callback("staging_verified")
        except Exception as exc:
            return {
                "status": "error",
                "error_code": "migration_interrupted",
                "error_stage": "staging_verified",
                "error_type": type(exc).__name__,
                "production_changed": False,
            }
        try:
            _publish(
                vault,
                entries,
                source_snapshot=publication_source_snapshot,
                boundary=callback,
            )
            callback("published")
            _completed_manifest(vault, con, payload=manifest_payload)
        except Exception as exc:
            return {
                "status": "error",
                "error_code": "migration_publication_failed",
                "error_stage": "publication",
                "error_type": type(exc).__name__,
                "production_changed": any(entry.get("published") for entry in entries),
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
    root = _migration_dir(vault)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "migration.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError:
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
            limits=limits,
            boundary=boundary,
            clock=clock,
        )
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate legacy Obsidian note facts")
    parser.add_argument("--vault-dir", required=True)
    parser.add_argument("--max-files", type=int, default=10000)
    parser.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    parser.add_argument("--max-seconds", type=float, default=3600.0)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = migrate_notes(
        Path(args.vault_dir),
        limits=MigrationLimits(args.max_files, args.max_bytes, args.max_seconds),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["status"])
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
