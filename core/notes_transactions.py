"""Bounded transaction journals for Obsidian note facts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from index_locks import source_lock
from maintenance_gate import MaintenanceInProgress, writer_access


MANIFEST_SCHEMA_VERSION = 2
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_MANIFEST_SOURCES = 1000
MAX_PENDING_TRANSACTIONS = 2000
MAX_UNSCOPED_APPLIED_TRANSACTIONS = 2000


class TransactionConflict(RuntimeError):
    tx_id: Optional[str] = None
    stage: Optional[str] = None


class InvalidDailyTarget(TransactionConflict, ValueError):
    pass


class MigrationRequired(RuntimeError):
    pass


class MigrationInProgress(TransactionConflict):
    pass


class JournalDurabilityError(OSError):
    def __init__(self, stage: str):
        super().__init__("journal_durability_failed")
        self.stage = stage
        self.tx_id: Optional[str] = None


@dataclass(frozen=True)
class AppendResult:
    bytes_written: int
    fsynced: bool


class AppendFailure(OSError):
    def __init__(self, result: AppendResult):
        super().__init__("transaction_append_failed")
        self.result = result
        self.tx_id: Optional[str] = None
        self.stage: Optional[str] = None


@dataclass(frozen=True)
class TransactionResult:
    tx_id: str
    stage: str
    daily: AppendResult
    index: AppendResult

    @property
    def facts_committed(self) -> bool:
        return self.daily.bytes_written > 0 or self.index.bytes_written > 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_directory_fd(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute():
        raise OSError("directory_path_not_absolute")
    parts = list(absolute.parts)
    note_positions = [
        index for index, part in enumerate(parts) if part == "notes"
    ]
    if note_positions:
        boundary = note_positions[-1]
        vault_anchor = Path(*parts[:boundary])
        if vault_anchor.exists():
            anchor = vault_anchor.resolve(strict=True)
            remaining = parts[boundary:]
        else:
            anchor = vault_anchor.parent.resolve(strict=True)
            remaining = [vault_anchor.name, *parts[boundary:]]
    else:
        anchor = Path(absolute.anchor)
        remaining = parts[1:]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(anchor, flags)
    try:
        for part in remaining:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _secure_read_bytes(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> bytes:
    path = Path(path)
    parent_fd = _open_directory_fd(path.parent, create=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                raise OSError("metadata_file_invalid")
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(fd, min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > max_bytes:
                raise OSError("metadata_file_too_large")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _secure_read_json(path: Path) -> Any:
    return json.loads(_secure_read_bytes(path).decode("utf-8"))


def _secure_regular_exists(path: Path) -> bool:
    path = Path(path)
    try:
        parent_fd = _open_directory_fd(path.parent, create=False)
    except FileNotFoundError:
        return False
    try:
        try:
            metadata = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError("metadata_symlink_forbidden")
        return stat.S_ISREG(metadata.st_mode)
    finally:
        os.close(parent_fd)


def _secure_unlink(path: Path, *, missing_ok: bool) -> None:
    path = Path(path)
    parent_fd = _open_directory_fd(path.parent, create=False)
    try:
        try:
            metadata = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError("metadata_target_invalid")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def durable_atomic_json(path: Path, payload: Any) -> None:
    path = Path(path)
    parent_fd: Optional[int] = None
    temporary_name: Optional[str] = None
    stage = "temp_write"
    try:
        try:
            stage = "parent_fsync"
            parent_fd = _open_directory_fd(path.parent, create=True)
            stage = "temp_write"
            try:
                existing = os.stat(
                    path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(
                    existing.st_mode
                ):
                    raise OSError("metadata_target_invalid")
            except FileNotFoundError:
                pass
            temporary_name = (
                f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            try:
                encoded = (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("metadata_write_no_progress")
                    view = view[written:]
                stage = "file_fsync"
                os.fsync(fd)
            finally:
                os.close(fd)
            stage = "replace"
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = None
            stage = "parent_fsync"
            os.fsync(parent_fd)
        except OSError as exc:
            raise JournalDurabilityError(stage) from exc
    finally:
        if temporary_name is not None and parent_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        if parent_fd is not None:
            os.close(parent_fd)


def manifest_path(vault: Path) -> Path:
    return Path(vault) / "notes" / "manifest.json"


def transactions_dir(vault: Path) -> Path:
    return Path(vault) / "notes" / "transactions"


def serialize_record(row: dict[str, Any], *, public: bool) -> bytes:
    payload = (
        {key: value for key, value in row.items() if not key.startswith("_")}
        if public
        else row
    )
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _fact_layer_nonempty(vault: Path) -> bool:
    index = Path(vault) / "index.jsonl"
    try:
        if index.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass
    daily = Path(vault) / "daily"
    try:
        with os.scandir(daily) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False) and entry.stat(
                        follow_symlinks=False
                    ).st_size > 0:
                        return True
                except OSError:
                    return True
    except FileNotFoundError:
        pass
    except OSError:
        return True
    return False


def _load_manifest(vault: Path) -> Optional[dict[str, Any]]:
    path = manifest_path(vault)
    try:
        payload = _secure_read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return None
    if not isinstance(payload.get("sources"), dict):
        return None
    return payload


def manifest_readiness(vault: Path) -> dict[str, Any]:
    vault = Path(vault)
    manifest = _load_manifest(vault)
    if manifest is not None:
        return {"ok": True, "manifest": manifest}
    if (vault / "notes" / "state.json").exists() or _fact_layer_nonempty(vault):
        return {"ok": False, "error_code": "notes_migration_required"}
    return {"ok": True, "manifest": None}


def ensure_manifest(vault: Path) -> dict[str, Any]:
    vault = Path(vault)
    readiness = manifest_readiness(vault)
    if not readiness["ok"]:
        raise MigrationRequired("notes_migration_required")
    existing = readiness.get("manifest")
    if isinstance(existing, dict):
        if len(existing.get("sources") or {}) > MAX_MANIFEST_SOURCES:
            raise TransactionConflict("manifest_source_limit")
        return existing
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "migration_status": "not_required",
        "generated_at": utc_now(),
        "last_successful_tx": None,
        "pending_transactions": [],
        "applied_transactions": [],
        "sources": {},
        "stats": {"committed_transactions": 0},
    }
    durable_atomic_json(manifest_path(vault), manifest)
    return manifest


def daily_target(vault: Path, row: dict[str, Any]) -> tuple[Path, str]:
    timestamp = str(row.get("timestamp") or "")
    day_text = timestamp[:10]
    if not DATE_PATTERN.fullmatch(day_text):
        raise InvalidDailyTarget("invalid_daily_date")
    try:
        parsed = date.fromisoformat(day_text)
    except ValueError as exc:
        raise InvalidDailyTarget("invalid_daily_date") from exc
    if parsed.isoformat() != day_text:
        raise InvalidDailyTarget("invalid_daily_date")
    daily_root = Path(vault) / "daily"
    if daily_root.is_symlink():
        raise InvalidDailyTarget("daily_symlink_forbidden")
    daily_root.mkdir(parents=True, exist_ok=True)
    resolved_root = daily_root.resolve(strict=True)
    target = daily_root / f"{day_text}.jsonl"
    if target.is_symlink():
        raise InvalidDailyTarget("daily_target_symlink_forbidden")
    if target.parent.resolve(strict=True) != resolved_root:
        raise InvalidDailyTarget("daily_parent_mismatch")
    return target, f"daily/{day_text}.jsonl"


def _target_name(relative: str) -> tuple[Optional[str], str]:
    if relative == "index.jsonl":
        return None, "index.jsonl"
    match = re.fullmatch(r"daily/(\d{4}-\d{2}-\d{2})\.jsonl", relative)
    if not match:
        raise TransactionConflict("journal_daily_target_invalid")
    try:
        parsed = date.fromisoformat(match.group(1))
    except ValueError as exc:
        raise TransactionConflict("journal_daily_target_invalid") from exc
    if parsed.isoformat() != match.group(1):
        raise TransactionConflict("journal_daily_target_invalid")
    return "daily", f"{match.group(1)}.jsonl"


def _open_vault_target(
    vault: Path,
    relative: str,
    *,
    writable: bool,
    create: bool,
) -> tuple[int, int, bool]:
    vault = Path(vault)
    vault.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    root_fd = os.open(vault, directory_flags)
    parent_fd = root_fd
    try:
        directory, name = _target_name(relative)
        if directory is not None:
            try:
                os.mkdir(directory, 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            parent_fd = os.open(directory, directory_flags, dir_fd=root_fd)
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise TransactionConflict("transaction_target_not_regular")
            existed = True
        except FileNotFoundError:
            existed = False
        flags = os.O_WRONLY if writable else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise TransactionConflict("transaction_target_open_failed") from exc
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            raise TransactionConflict("transaction_target_not_regular")
        if parent_fd != root_fd:
            os.close(root_fd)
        return fd, parent_fd, existed
    except Exception:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)
        raise


def _close_vault_target(fd: int, parent_fd: int) -> None:
    os.close(fd)
    os.close(parent_fd)


def _vault_target_size(vault: Path, relative: str) -> int:
    try:
        fd, parent_fd, _existed = _open_vault_target(
            vault,
            relative,
            writable=False,
            create=False,
        )
    except FileNotFoundError:
        return 0
    try:
        return os.fstat(fd).st_size
    finally:
        _close_vault_target(fd, parent_fd)


def append_exact(path: Path, offset: int, payload: bytes) -> AppendResult:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    flags = os.O_WRONLY | os.O_CREAT
    fd = os.open(path, flags, 0o600)
    written = 0
    try:
        current_size = os.fstat(fd).st_size
        if current_size != offset:
            raise TransactionConflict("append_offset_mismatch")
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(payload)
        while view:
            try:
                count = os.write(fd, view)
            except OSError as exc:
                raise AppendFailure(AppendResult(written, False)) from exc
            if count <= 0:
                raise AppendFailure(AppendResult(written, False))
            written += count
            view = view[count:]
        try:
            os.fsync(fd)
        except OSError as exc:
            raise AppendFailure(AppendResult(written, False)) from exc
    finally:
        os.close(fd)
    if not existed:
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    return AppendResult(written, True)


def _append_vault_exact(
    vault: Path,
    relative: str,
    offset: int,
    payload: bytes,
) -> AppendResult:
    fd, parent_fd, existed = _open_vault_target(
        vault,
        relative,
        writable=True,
        create=True,
    )
    written = 0
    try:
        current_size = os.fstat(fd).st_size
        if current_size != offset:
            raise TransactionConflict("append_offset_mismatch")
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(payload)
        while view:
            try:
                count = os.write(fd, view)
            except OSError as exc:
                raise AppendFailure(AppendResult(written, False)) from exc
            if count <= 0:
                raise AppendFailure(AppendResult(written, False))
            written += count
            view = view[count:]
        try:
            os.fsync(fd)
        except OSError as exc:
            raise AppendFailure(AppendResult(written, False)) from exc
        if not existed:
            os.fsync(parent_fd)
        return AppendResult(written, True)
    finally:
        _close_vault_target(fd, parent_fd)


def _vault_side_committed(
    vault: Path,
    relative: str,
    offset: int,
    payload: bytes,
) -> bool:
    try:
        fd, parent_fd, _existed = _open_vault_target(
            vault,
            relative,
            writable=False,
            create=False,
        )
    except FileNotFoundError:
        if offset == 0:
            return False
        raise TransactionConflict("declared_offset_mismatch")
    try:
        size = os.fstat(fd).st_size
        if size == offset:
            return False
        if size < offset or size < offset + len(payload):
            raise TransactionConflict("declared_offset_mismatch")
        os.lseek(fd, offset, os.SEEK_SET)
        actual = bytearray()
        while len(actual) < len(payload):
            chunk = os.read(fd, len(payload) - len(actual))
            if not chunk:
                break
            actual.extend(chunk)
        if bytes(actual) != payload:
            raise TransactionConflict("declared_bytes_mismatch")
        return True
    finally:
        _close_vault_target(fd, parent_fd)


def _journal_path(vault: Path, tx_id: str) -> Path:
    return transactions_dir(vault) / f"{tx_id}.json"


def _assert_no_publication_in_progress(vault: Path) -> None:
    publication = Path(vault) / "notes" / "migration" / "publication.json"
    try:
        exists = _secure_regular_exists(publication)
    except OSError as exc:
        raise MigrationInProgress("notes_migration_in_progress") from exc
    if exists:
        raise MigrationInProgress("notes_migration_in_progress")


def _path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _prepared_journal(
    vault: Path,
    row: dict[str, Any],
    source_entry: Optional[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    _daily, daily_relpath = daily_target(vault, row)
    daily_bytes = serialize_record(row, public=True)
    index_bytes = serialize_record(row, public=False)
    tx_seed = "|".join(
        (
            str(row.get("id") or ""),
            hashlib.sha256(daily_bytes).hexdigest(),
            hashlib.sha256(index_bytes).hexdigest(),
        )
    )
    tx_id = hashlib.sha256(tx_seed.encode("utf-8")).hexdigest()[:24]
    journal = {
        "schema_version": 1,
        "tx_id": tx_id,
        "record_id": str(row.get("id") or ""),
        "daily_relpath": daily_relpath,
        "daily_offset": _vault_target_size(vault, daily_relpath),
        "index_offset": _vault_target_size(vault, "index.jsonl"),
        "daily_length": len(daily_bytes),
        "index_length": len(index_bytes),
        "daily_sha256": hashlib.sha256(daily_bytes).hexdigest(),
        "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
        "daily_bytes_hex": daily_bytes.hex(),
        "index_bytes_hex": index_bytes.hex(),
        "stage": "prepared",
        "prepared_at": utc_now(),
    }
    if source_entry:
        journal["source_entry"] = {
            "relative_path": source_entry[0],
            "value": source_entry[1],
        }
    return journal


def _validated_target(vault: Path, journal: dict[str, Any]) -> Path:
    relative = str(journal.get("daily_relpath") or "")
    if not re.fullmatch(r"daily/\d{4}-\d{2}-\d{2}\.jsonl", relative):
        raise TransactionConflict("journal_daily_target_invalid")
    day_text = Path(relative).stem
    try:
        parsed = date.fromisoformat(day_text)
    except ValueError as exc:
        raise TransactionConflict("journal_daily_target_invalid") from exc
    if parsed.isoformat() != day_text:
        raise TransactionConflict("journal_daily_target_invalid")
    daily_root = Path(vault) / "daily"
    if daily_root.is_symlink():
        raise TransactionConflict("journal_daily_symlink_forbidden")
    root = daily_root.resolve(strict=True)
    target = Path(vault) / relative
    if target.is_symlink():
        raise TransactionConflict("journal_daily_target_symlink_forbidden")
    if target.parent.resolve(strict=True) != root:
        raise TransactionConflict("journal_daily_parent_mismatch")
    return target


def _declared_bytes(journal: dict[str, Any], side: str) -> bytes:
    try:
        payload = bytes.fromhex(str(journal[f"{side}_bytes_hex"]))
    except (KeyError, ValueError) as exc:
        raise TransactionConflict("journal_payload_invalid") from exc
    if len(payload) != int(journal.get(f"{side}_length") or -1):
        raise TransactionConflict("journal_length_mismatch")
    if hashlib.sha256(payload).hexdigest() != journal.get(f"{side}_sha256"):
        raise TransactionConflict("journal_hash_mismatch")
    return payload


def _side_committed(path: Path, offset: int, payload: bytes) -> bool:
    size = _path_size(path)
    if size == offset:
        return False
    if size < offset or size < offset + len(payload):
        raise TransactionConflict("declared_offset_mismatch")
    with path.open("rb") as handle:
        handle.seek(offset)
        actual = handle.read(len(payload))
    if actual != payload:
        raise TransactionConflict("declared_bytes_mismatch")
    return True


def _update_journal(path: Path, journal: dict[str, Any], stage: str) -> None:
    journal["stage"] = stage
    journal["updated_at"] = utc_now()
    durable_atomic_json(path, journal)


def _register_pending(vault: Path, tx_id: str) -> None:
    manifest = ensure_manifest(vault)
    pending = list(manifest.get("pending_transactions") or [])
    if tx_id not in pending:
        if len(pending) >= MAX_PENDING_TRANSACTIONS:
            raise TransactionConflict("pending_transaction_limit")
        pending.append(tx_id)
    manifest["pending_transactions"] = pending
    durable_atomic_json(manifest_path(vault), manifest)


def _update_manifest(vault: Path, journal: dict[str, Any]) -> None:
    manifest = ensure_manifest(vault)
    source_entry = journal.get("source_entry")
    source_relative: Optional[str] = None
    source_value: Optional[dict[str, Any]] = None
    if isinstance(source_entry, dict):
        relative = source_entry.get("relative_path")
        value = source_entry.get("value")
        if isinstance(relative, str) and isinstance(value, dict):
            source_relative = relative
            source_value = dict(value)
    existing_source = (
        manifest.setdefault("sources", {}).get(source_relative)
        if source_relative
        else None
    )
    applied = list(manifest.get("applied_transactions") or [])
    already_applied = (
        journal["tx_id"] in applied
        or (
            isinstance(existing_source, dict)
            and existing_source.get("last_tx_id") == journal["tx_id"]
        )
    )
    manifest["last_successful_tx"] = journal["tx_id"]
    manifest["updated_at"] = utc_now()
    stats = manifest.setdefault("stats", {})
    if not already_applied:
        stats["committed_transactions"] = int(stats.get("committed_transactions") or 0) + 1
    if source_relative and source_value is not None:
        if (
            source_relative not in manifest["sources"]
            and len(manifest["sources"]) >= MAX_MANIFEST_SOURCES
        ):
            raise TransactionConflict("manifest_source_limit")
        source_value["last_tx_id"] = journal["tx_id"]
        manifest["sources"][source_relative] = source_value
    elif not already_applied:
        if len(applied) >= MAX_UNSCOPED_APPLIED_TRANSACTIONS:
            raise TransactionConflict("applied_transaction_limit")
        applied.append(journal["tx_id"])
        manifest["applied_transactions"] = applied
    durable_atomic_json(manifest_path(vault), manifest)


def _finalize_transaction(
    vault: Path,
    journal_path: Path,
    journal: dict[str, Any],
    *,
    boundary: Callable[[str], None],
) -> None:
    manifest = ensure_manifest(vault)
    pending = [
        tx_id
        for tx_id in list(manifest.get("pending_transactions") or [])
        if tx_id != journal["tx_id"]
    ]
    manifest["pending_transactions"] = pending
    durable_atomic_json(manifest_path(vault), manifest)
    boundary("pending_cleared")
    try:
        _secure_unlink(journal_path, missing_ok=True)
    except OSError as exc:
        raise TransactionConflict("transaction_journal_cleanup_failed") from exc


def _resume_one(
    vault: Path,
    journal_path: Path,
    journal: dict[str, Any],
    *,
    boundary: Callable[[str], None],
) -> TransactionResult:
    stage = "validate_journal"
    try:
        _validated_target(vault, journal)
        index_target = Path(vault) / "index.jsonl"
        if index_target.is_symlink():
            raise TransactionConflict("journal_index_target_symlink_forbidden")
        daily_bytes = _declared_bytes(journal, "daily")
        index_bytes = _declared_bytes(journal, "index")
        daily_result = AppendResult(0, False)
        index_result = AppendResult(0, False)

        stage = "daily_append"
        daily_relative = str(journal["daily_relpath"])
        if not _vault_side_committed(
            vault,
            daily_relative,
            int(journal["daily_offset"]),
            daily_bytes,
        ):
            daily_result = _append_vault_exact(
                vault,
                daily_relative,
                int(journal["daily_offset"]),
                daily_bytes,
            )
        stage = "daily_journal"
        _update_journal(journal_path, journal, "daily_committed")
        boundary("daily_committed")

        stage = "index_append"
        if not _vault_side_committed(
            vault,
            "index.jsonl",
            int(journal["index_offset"]),
            index_bytes,
        ):
            index_result = _append_vault_exact(
                vault,
                "index.jsonl",
                int(journal["index_offset"]),
                index_bytes,
            )
        stage = "index_journal"
        _update_journal(journal_path, journal, "index_committed")
        boundary("index_committed")

        stage = "manifest_update"
        _update_manifest(vault, journal)
        boundary("manifest_committed")
        stage = "complete_journal"
        _update_journal(journal_path, journal, "complete")
        boundary("journal_completed")
        stage = "finalize"
        _finalize_transaction(
            vault,
            journal_path,
            journal,
            boundary=boundary,
        )
        boundary("complete")
        return TransactionResult(
            tx_id=str(journal["tx_id"]),
            stage="complete",
            daily=daily_result,
            index=index_result,
        )
    except (AppendFailure, JournalDurabilityError, TransactionConflict) as exc:
        exc.tx_id = str(journal.get("tx_id") or "")
        exc.stage = stage
        raise


def _commit_record_guarded(
    vault: Path,
    row: dict[str, Any],
    *,
    boundary: Optional[Callable[[str], None]] = None,
    source_entry: Optional[tuple[str, dict[str, Any]]] = None,
) -> TransactionResult:
    vault = Path(vault)
    callback = boundary or (lambda _stage: None)
    index = vault / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with source_lock(index, exclusive=True):
        _assert_no_publication_in_progress(vault)
        ensure_manifest(vault)
        _recover_pending_locked(vault)
        manifest = ensure_manifest(vault)
        if (
            source_entry
            and source_entry[0] not in manifest.get("sources", {})
            and len(manifest.get("sources", {})) >= MAX_MANIFEST_SOURCES
        ):
            raise TransactionConflict("manifest_source_limit")
        if source_entry:
            previous = manifest.get("sources", {}).get(source_entry[0])
            if (
                isinstance(previous, dict)
                and previous.get("record_id") == row.get("id")
            ):
                return TransactionResult(
                    str(manifest.get("last_successful_tx") or ""),
                    "already_committed",
                    AppendResult(0, True),
                    AppendResult(0, True),
                )
        journal = _prepared_journal(vault, row, source_entry)
        if str(journal["tx_id"]) in set(
            str(item) for item in manifest.get("applied_transactions") or []
        ):
            return TransactionResult(
                str(journal["tx_id"]),
                "already_committed",
                AppendResult(0, True),
                AppendResult(0, True),
            )
        path = _journal_path(vault, str(journal["tx_id"]))
        try:
            existing = _secure_read_json(path)
        except FileNotFoundError:
            durable_atomic_json(path, journal)
            callback("journal_persisted")
            _register_pending(vault, str(journal["tx_id"]))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransactionConflict("transaction_journal_invalid") from exc
        else:
            if not isinstance(existing, dict):
                raise TransactionConflict("transaction_journal_invalid")
            journal = existing
        callback("prepared")
        return _resume_one(
            vault,
            path,
            journal,
            boundary=callback,
        )


def commit_record(
    vault: Path,
    row: dict[str, Any],
    *,
    boundary: Optional[Callable[[str], None]] = None,
    source_entry: Optional[tuple[str, dict[str, Any]]] = None,
) -> TransactionResult:
    try:
        with writer_access(Path(vault)):
            return _commit_record_guarded(
                vault,
                row,
                boundary=boundary,
                source_entry=source_entry,
            )
    except MaintenanceInProgress as exc:
        raise MigrationInProgress("notes_migration_in_progress") from exc


def _recover_pending_locked(vault: Path) -> dict[str, int]:
    vault = Path(vault)
    _assert_no_publication_in_progress(vault)
    recovered = 0
    cleaned = 0
    ensure_manifest(vault)
    manifest = ensure_manifest(vault)
    pending = list(manifest.get("pending_transactions") or [])
    if len(pending) > MAX_PENDING_TRANSACTIONS:
        raise TransactionConflict("pending_transaction_limit")
    discovered: set[str] = set()
    try:
        directory_fd = _open_directory_fd(transactions_dir(vault), create=False)
        try:
            names = os.listdir(directory_fd)
            for name in names:
                if len(discovered) >= MAX_PENDING_TRANSACTIONS:
                    raise TransactionConflict("transaction_directory_limit")
                if not name.endswith(".json"):
                    continue
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(metadata.st_mode):
                    raise TransactionConflict("transaction_journal_symlink")
                if stat.S_ISREG(metadata.st_mode):
                    discovered.add(name[:-5])
        finally:
            os.close(directory_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise TransactionConflict("transaction_directory_invalid") from exc
    tx_ids = set(pending) | discovered
    if len(tx_ids) > MAX_PENDING_TRANSACTIONS:
        raise TransactionConflict("transaction_directory_limit")
    for tx_id in sorted(tx_ids):
        if not isinstance(tx_id, str) or not re.fullmatch(r"[0-9a-f]{24}", tx_id):
            raise TransactionConflict("pending_transaction_id_invalid")
        path = _journal_path(vault, tx_id)
        try:
            journal = _secure_read_json(path)
        except FileNotFoundError:
            raise TransactionConflict("pending_transaction_missing")
        except (OSError, json.JSONDecodeError) as exc:
            raise TransactionConflict("transaction_journal_invalid") from exc
        if not isinstance(journal, dict) or journal.get("tx_id") != tx_id:
            raise TransactionConflict("transaction_id_mismatch")
        if journal.get("stage") == "complete":
            _finalize_transaction(
                vault,
                path,
                journal,
                boundary=lambda _stage: None,
            )
            cleaned += 1
            continue
        if tx_id not in pending:
            _register_pending(vault, tx_id)
        _resume_one(vault, path, journal, boundary=lambda _stage: None)
        recovered += 1
    return {"recovered": recovered, "cleaned": cleaned}


def _recover_pending_guarded(vault: Path) -> dict[str, int]:
    vault = Path(vault)
    index = vault / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with source_lock(index, exclusive=True):
        _assert_no_publication_in_progress(vault)
        return _recover_pending_locked(vault)


def recover_pending(vault: Path) -> dict[str, int]:
    try:
        with writer_access(Path(vault)):
            return _recover_pending_guarded(vault)
    except MaintenanceInProgress as exc:
        raise MigrationInProgress("notes_migration_in_progress") from exc
