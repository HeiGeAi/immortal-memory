"""Safe, bounded and recoverable ingestion for user-authored Obsidian notes."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from file_utils import atomic_write_json
from index_locks import source_lock
from notes_transactions import (
    AppendFailure,
    MigrationRequired,
    TransactionConflict,
    commit_record,
    ensure_manifest,
    manifest_readiness,
    recover_pending,
)
from secret_scan import scan_text_shapes


NOTES_SUBDIR = "笔记"


class ReconciliationConflict(RuntimeError):
    """The same note fact has different payloads across durable stores."""


@dataclass(frozen=True)
class NoteIngestionLimits:
    max_files: int = 1000
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 20 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_files < 1 or self.max_file_bytes < 1 or self.max_total_bytes < 1:
            raise ValueError("note ingestion limits must be positive")


@dataclass(frozen=True)
class NoteCandidate:
    path: Path
    relative_path: str
    text: str
    size: int
    mtime: float


def state_path(vault_dir: Path) -> Path:
    return Path(vault_dir) / "notes" / "state.json"


def after_daily_append() -> None:
    """Fault-injection boundary between authoritative daily and index writes."""


def before_file_open(_relative_path: str) -> None:
    """Deterministic test boundary; production leaves the anchored fd unchanged."""


def after_file_fstat(_relative_path: str, _fd: int) -> None:
    """Deterministic test boundary for concurrent source growth."""


def _skip(
    skipped: list[dict[str, Any]],
    counts: Counter,
    relative_path: str,
    reason: str,
    *,
    rules: Optional[list[str]] = None,
) -> None:
    item: dict[str, Any] = {"path": relative_path, "reason": reason}
    if rules:
        item["rules"] = sorted(rules)
    skipped.append(item)
    counts[reason] += 1


def _skip_secret(
    skipped: list[dict[str, Any]],
    counts: Counter,
    relative_path: str,
    rules: list[str],
) -> None:
    item: dict[str, Any] = {
        "path_sha256_16": hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16],
        "reason": "secret_shape",
        "rules": sorted(rules),
    }
    basename = Path(relative_path).name
    if not scan_text_shapes(basename):
        item["basename"] = basename
    skipped.append(item)
    counts["secret_shape"] += 1


def _walk_note_entries(
    directory_fd: int,
    relative_directory: str,
    skipped: list[dict[str, Any]],
    counts: Counter,
):
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError:
        _skip(skipped, counts, relative_directory or ".", "directory_unreadable")
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    for name in names:
        relative = f"{relative_directory}/{name}" if relative_directory else name
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            _skip(skipped, counts, relative, "file_unreadable")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            try:
                followed = os.stat(name, dir_fd=directory_fd, follow_symlinks=True)
                is_directory = stat.S_ISDIR(followed.st_mode)
            except OSError:
                is_directory = False
            _skip(
                skipped,
                counts,
                relative,
                "symlink_directory" if is_directory else "symlink_file",
            )
            continue
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
            except OSError:
                _skip(skipped, counts, relative, "directory_unreadable")
                continue
            try:
                yield from _walk_note_entries(
                    child_fd,
                    relative,
                    skipped,
                    counts,
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            if Path(name).suffix.lower() == ".md":
                _skip(skipped, counts, relative, "not_regular_file")
            continue
        if Path(name).suffix.lower() != ".md" or name.startswith("_"):
            continue
        yield directory_fd, name, relative


def _open_regular_file(
    directory_fd: int,
    name: str,
) -> tuple[Optional[int], Optional[os.stat_result], str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        return None, None, "file_unreadable"
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            return None, metadata, "not_regular_file"
        return fd, metadata, ""
    except OSError:
        os.close(fd)
        return None, None, "file_unreadable"


def _read_open_file(
    fd: int,
    max_bytes: int,
) -> tuple[Optional[bytes], str, int]:
    total = 0
    try:
        chunks: list[bytes] = []
        while total < max_bytes:
            chunk = os.read(fd, min(65536, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        current_size = os.fstat(fd).st_size
        if current_size > total:
            return None, "file_too_large", total
        return b"".join(chunks), "", total
    except OSError:
        return None, "file_unreadable", total
    finally:
        os.close(fd)


def discover_notes(
    obsidian_vault: Path,
    *,
    limits: NoteIngestionLimits,
) -> tuple[
    list[NoteCandidate],
    list[dict[str, Any]],
    dict[str, int],
    dict[str, int],
    int,
    int,
    int,
]:
    notes_root = Path(obsidian_vault) / NOTES_SUBDIR
    skipped: list[dict[str, Any]] = []
    skipped_counts: Counter = Counter()
    secret_counts: Counter = Counter()
    candidates: list[NoteCandidate] = []
    scanned = 0
    accepted_bytes = 0
    processed_bytes = 0
    bounded_files = 0

    if notes_root.is_symlink():
        _skip(skipped, skipped_counts, ".", "symlink_directory")
        return [], skipped, dict(skipped_counts), {}, scanned, 0, 0
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        root_fd = os.open(notes_root, directory_flags)
    except FileNotFoundError:
        return [], skipped, {}, {}, scanned, 0, 0
    except OSError:
        _skip(skipped, skipped_counts, ".", "directory_unreadable")
        return [], skipped, dict(skipped_counts), {}, scanned, 0, 0

    try:
        for parent_fd, name, relative in _walk_note_entries(
            root_fd,
            "",
            skipped,
            skipped_counts,
        ):
            scanned += 1
            before_file_open(relative)
            fd, metadata, open_error = _open_regular_file(parent_fd, name)
            if open_error:
                _skip(skipped, skipped_counts, relative, open_error)
                continue
            assert fd is not None and metadata is not None
            after_file_fstat(relative, fd)
            if metadata.st_size > limits.max_file_bytes:
                os.close(fd)
                _skip(skipped, skipped_counts, relative, "file_too_large")
                continue
            if bounded_files >= limits.max_files:
                os.close(fd)
                _skip(skipped, skipped_counts, relative, "max_files_reached")
                continue
            bounded_files += 1
            if processed_bytes + metadata.st_size > limits.max_total_bytes:
                os.close(fd)
                _skip(skipped, skipped_counts, relative, "total_bytes_exceeded")
                continue
            remaining_budget = limits.max_total_bytes - processed_bytes
            read_limit = min(limits.max_file_bytes, remaining_budget)
            payload, read_error, bytes_read = _read_open_file(fd, read_limit)
            processed_bytes += bytes_read
            if read_error:
                reason = (
                    "total_bytes_exceeded"
                    if read_error == "file_too_large"
                    and remaining_budget <= limits.max_file_bytes
                    else read_error
                )
                _skip(skipped, skipped_counts, relative, reason)
                if processed_bytes >= limits.max_total_bytes:
                    break
                continue
            assert payload is not None
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                _skip(skipped, skipped_counts, relative, "invalid_utf8")
                if processed_bytes >= limits.max_total_bytes:
                    break
                continue
            rules = scan_text_shapes(text)
            if rules:
                secret_counts.update(rules)
                _skip_secret(
                    skipped,
                    skipped_counts,
                    relative,
                    list(rules),
                )
                if processed_bytes >= limits.max_total_bytes:
                    break
                continue
            if not text.strip():
                _skip(skipped, skipped_counts, relative, "empty_note")
                if processed_bytes >= limits.max_total_bytes:
                    break
                continue
            candidates.append(
                NoteCandidate(
                    path=Path(relative),
                    relative_path=relative,
                    text=text,
                    size=len(payload),
                    mtime=metadata.st_mtime,
                )
            )
            accepted_bytes += len(payload)
            if processed_bytes >= limits.max_total_bytes:
                break
    finally:
        os.close(root_fd)
    return (
        candidates,
        skipped,
        dict(sorted(skipped_counts.items())),
        dict(sorted(secret_counts.items())),
        scanned,
        processed_bytes,
        accepted_bytes,
    )


def _record(candidate: NoteCandidate) -> dict[str, Any]:
    content_hash = hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
    dedup_key = f"obsidian-note|{candidate.relative_path}|{content_hash}"
    record_id = f"obsidian-note-{hashlib.sha256(dedup_key.encode()).hexdigest()[:20]}"
    timestamp = datetime.fromtimestamp(candidate.mtime, tz=timezone.utc).isoformat()
    title = candidate.path.stem
    for line in candidate.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip() or title
            break
    return {
        "id": record_id,
        "timestamp": timestamp,
        "source": "obsidian-note",
        "type": "manual-note",
        "role": "user",
        "project": "notes",
        "title": title[:120],
        "content": candidate.text.strip(),
        "metadata": {
            "relative_path": candidate.relative_path,
            "dedup_key": dedup_key,
        },
        "_dedup_key": dedup_key,
    }


def _read_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.is_file():
        return ids
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("id"):
                ids.add(str(row["id"]))
    return ids


def _public_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _read_note_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return rows
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid_jsonl_fact") from exc
            if not isinstance(row, dict):
                raise ValueError("invalid_jsonl_fact")
            if row.get("source") != "obsidian-note":
                continue
            record_id = str(row.get("id") or "").strip()
            if not record_id:
                raise ValueError("invalid_jsonl_fact")
            existing = rows.get(record_id)
            if existing is not None and _public_fact(existing) != _public_fact(row):
                raise ReconciliationConflict("duplicate_note_payload_conflict")
            rows[record_id] = row
    return rows


def _daily_path_for(vault: Path, row: dict[str, Any]) -> Path:
    day = str(row.get("timestamp") or "")[:10]
    if len(day) != 10 or day[4:5] != "-" or day[7:8] != "-":
        raise ValueError("invalid_note_timestamp")
    return vault / "daily" / f"{day}.jsonl"


def _repair_partial_tail(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with path.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return False
        handle.seek(0, os.SEEK_END)
        file_size = handle.tell()
        position = file_size
        tail_start = 0
        while position:
            read_size = min(65536, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                tail_start = position + newline + 1
                break
        handle.seek(tail_start)
        tail = handle.read(file_size - tail_start)
        try:
            row = json.loads(tail.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            row = None
        valid_id = str(row.get("id") or "").strip() if isinstance(row, dict) else ""
        if valid_id:
            handle.seek(0, os.SEEK_END)
            handle.write(b"\n")
        else:
            handle.truncate(tail_start)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _append_jsonl(path: Path, rows: list[dict[str, Any]], *, public: bool) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        payload = "".join(
            json.dumps(
                (
                    {key: value for key, value in row.items() if not key.startswith("_")}
                    if public
                    else row
                ),
                ensure_ascii=False,
            )
            + "\n"
            for row in rows
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("note append made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed:
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _base_result(
    *,
    dry_run: bool,
    scanned: int,
    planned: int,
    skipped: list[dict[str, Any]],
    skipped_by_reason: dict[str, int],
    secret_shapes: dict[str, int],
    processed_bytes: int,
    accepted_bytes: int,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "dry_run": dry_run,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "scanned": scanned,
            "planned_this_run": planned,
            "ingested_this_run": 0,
            "repaired_index": 0,
            "repaired_daily": 0,
            "repaired_tails": 0,
            "processed_bytes": processed_bytes,
            "accepted_bytes": accepted_bytes,
        },
        "skipped": skipped,
        "skipped_by_reason": skipped_by_reason,
        "secret_shapes": secret_shapes,
    }


def _persist_state(vault: Path, result: dict[str, Any]) -> dict[str, Any]:
    try:
        atomic_write_json(state_path(vault), result)
        return result
    except OSError:
        failed = dict(result)
        failed["status"] = "error"
        failed["error_code"] = "state_write_failed"
        totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
        failed["facts_committed"] = bool(result.get("facts_committed")) or any(
            int((totals or {}).get(key) or 0) > 0
            for key in ("ingested_this_run", "repaired_index", "repaired_daily")
        )
        return failed


def _previous_last_success(vault: Path) -> Optional[str]:
    previous = read_ingestion_state(vault)
    if previous.get("status") == "ok":
        value = previous.get("last_success") or previous.get("generated_at")
    else:
        value = previous.get("last_success")
    return str(value) if value else None


def _persist_failure(
    vault: Path,
    result: dict[str, Any],
    *,
    stage: str,
    error: Exception,
    last_success: Optional[str],
    facts_committed: bool,
    pending_repair_direction: Optional[str],
    transaction_id: Optional[str] = None,
) -> dict[str, Any]:
    failed = dict(result)
    failed["status"] = "error"
    failed["error_code"] = (
        "reconciliation_conflict"
        if isinstance(error, ReconciliationConflict)
        else "write_failed"
    )
    failed["error_stage"] = stage
    failed["error_type"] = type(error).__name__
    failed["facts_committed"] = facts_committed
    if last_success:
        failed["last_success"] = last_success
    if pending_repair_direction:
        failed["pending_repair_direction"] = pending_repair_direction
    if transaction_id:
        failed["transaction_id"] = transaction_id
    persisted = _persist_state(vault, failed)
    if persisted.get("error_code") == "state_write_failed":
        persisted["original_error_stage"] = stage
        persisted["error_type"] = "OSError"
    return persisted


def ingest_notes(
    vault_dir: Path,
    obsidian_vault: Path,
    *,
    dry_run: bool,
    limits: Optional[NoteIngestionLimits] = None,
) -> dict[str, Any]:
    limits = limits or NoteIngestionLimits()
    vault = Path(vault_dir)
    readiness = manifest_readiness(vault)
    if not readiness["ok"]:
        return {
            "status": "error",
            "error_code": "notes_migration_required",
            "dry_run": dry_run,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "totals": {
                "scanned": 0,
                "planned_this_run": 0,
                "ingested_this_run": 0,
                "processed_bytes": 0,
                "accepted_bytes": 0,
            },
        }
    (
        candidates,
        skipped,
        skipped_by_reason,
        secret_shapes,
        scanned,
        processed_bytes,
        accepted_bytes,
    ) = discover_notes(Path(obsidian_vault), limits=limits)
    records = [_record(candidate) for candidate in candidates]
    result = _base_result(
        dry_run=dry_run,
        scanned=scanned,
        planned=len(records),
        skipped=skipped,
        skipped_by_reason=skipped_by_reason,
        secret_shapes=secret_shapes,
        processed_bytes=processed_bytes,
        accepted_bytes=accepted_bytes,
    )
    manifest = readiness.get("manifest")
    if dry_run:
        sources = manifest.get("sources", {}) if isinstance(manifest, dict) else {}
        result["totals"]["planned_this_run"] = sum(
            1
            for candidate, record in zip(candidates, records)
            if not isinstance(sources.get(candidate.relative_path), dict)
            or sources[candidate.relative_path].get("record_id") != record["id"]
        )
        return result

    last_success = _previous_last_success(vault)
    stage = "manifest"
    facts_committed = False
    pending_repair_direction: Optional[str] = None
    try:
        ensure_manifest(vault)
        stage = "recover_pending"
        recovery = recover_pending(vault)
        result["totals"]["recovered_transactions"] = recovery["recovered"]
        current_manifest = ensure_manifest(vault)
        sources = current_manifest.get("sources", {})
        ingested = 0
        for candidate, record in zip(candidates, records):
            previous = sources.get(candidate.relative_path)
            if isinstance(previous, dict) and previous.get("record_id") == record["id"]:
                continue
            source_value = {
                "record_id": record["id"],
                "content_sha256": hashlib.sha256(candidate.text.encode("utf-8")).hexdigest(),
                "size": candidate.size,
                "mtime": candidate.mtime,
            }
            stage = "transaction"
            transaction = commit_record(
                vault,
                record,
                source_entry=(candidate.relative_path, source_value),
            )
            facts_committed = facts_committed or transaction.facts_committed
            pending_repair_direction = None
            sources[candidate.relative_path] = source_value
            if transaction.facts_committed:
                ingested += 1
        result["totals"]["ingested_this_run"] = ingested
    except Exception as exc:
        if isinstance(exc, AppendFailure):
            facts_committed = facts_committed or exc.result.bytes_written > 0
        if isinstance(exc, (AppendFailure, TransactionConflict)):
            pending_repair_direction = "journal_declared"
            stage = str(getattr(exc, "stage", None) or stage)
        return _persist_failure(
            vault,
            result,
            stage=stage,
            error=exc,
            last_success=last_success,
            facts_committed=facts_committed,
            pending_repair_direction=pending_repair_direction,
            transaction_id=str(getattr(exc, "tx_id", None) or "") or None,
        )

    result["last_success"] = result["generated_at"]
    return _persist_state(vault, result)


def read_ingestion_state(vault_dir: Path) -> dict[str, Any]:
    path = state_path(Path(vault_dir))
    if not path.is_file():
        return {"status": "missing", "state_path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "error",
            "error_code": "state_corrupt",
            "state_path": str(path),
        }
    if not isinstance(payload, dict):
        return {
            "status": "error",
            "error_code": "state_corrupt",
            "state_path": str(path),
        }
    return payload
