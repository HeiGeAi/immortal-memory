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
from secret_scan import scan_text_shapes


NOTES_SUBDIR = "笔记"


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


def _safe_relative(path: Path, root: Path) -> Optional[str]:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


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
    directory: Path,
    root: Path,
    skipped: list[dict[str, Any]],
    counts: Counter,
):
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        relative = _safe_relative(directory, root) or "."
        _skip(skipped, counts, relative, "directory_unreadable")
        return
    for entry in entries:
        path = Path(entry.path)
        relative = _safe_relative(path, root) or entry.name
        if entry.is_symlink():
            try:
                is_directory = entry.is_dir(follow_symlinks=True)
            except OSError:
                is_directory = False
            _skip(
                skipped,
                counts,
                relative,
                "symlink_directory" if is_directory else "symlink_file",
            )
            continue
        if entry.is_dir(follow_symlinks=False):
            yield from _walk_note_entries(path, root, skipped, counts)
            continue
        if not entry.is_file(follow_symlinks=False):
            if path.suffix.lower() == ".md":
                _skip(skipped, counts, relative, "not_regular_file")
            continue
        if path.suffix.lower() != ".md" or path.name.startswith("_"):
            continue
        yield path


def _read_regular_file(path: Path, max_bytes: int) -> tuple[Optional[bytes], Optional[os.stat_result], str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, None, "file_unreadable"
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            return None, metadata, "not_regular_file"
        if metadata.st_size > max_bytes:
            return None, metadata, "file_too_large"
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                return None, metadata, "file_too_large"
        return b"".join(chunks), metadata, ""
    except OSError:
        return None, None, "file_unreadable"
    finally:
        os.close(fd)


def discover_notes(
    obsidian_vault: Path,
    *,
    limits: NoteIngestionLimits,
) -> tuple[list[NoteCandidate], list[dict[str, Any]], dict[str, int], dict[str, int], int]:
    notes_root = Path(obsidian_vault) / NOTES_SUBDIR
    skipped: list[dict[str, Any]] = []
    skipped_counts: Counter = Counter()
    secret_counts: Counter = Counter()
    candidates: list[NoteCandidate] = []
    scanned = 0
    accepted_bytes = 0
    bounded_files = 0

    if notes_root.is_symlink():
        _skip(skipped, skipped_counts, ".", "symlink_directory")
        return [], skipped, dict(skipped_counts), {}, scanned
    if not notes_root.is_dir():
        return [], skipped, {}, {}, scanned
    try:
        root = notes_root.resolve(strict=True)
    except OSError:
        _skip(skipped, skipped_counts, ".", "directory_unreadable")
        return [], skipped, dict(skipped_counts), {}, scanned

    for path in _walk_note_entries(notes_root, root, skipped, skipped_counts):
        scanned += 1
        relative = _safe_relative(path, notes_root) or path.name
        try:
            resolved = path.resolve(strict=True)
            if _safe_relative(resolved, root) is None:
                _skip(skipped, skipped_counts, relative, "outside_notes_root")
                continue
            preliminary = path.lstat()
        except OSError:
            _skip(skipped, skipped_counts, relative, "file_unreadable")
            continue
        if preliminary.st_size > limits.max_file_bytes:
            _skip(skipped, skipped_counts, relative, "file_too_large")
            continue
        if bounded_files >= limits.max_files:
            _skip(skipped, skipped_counts, relative, "max_files_reached")
            continue
        bounded_files += 1
        payload, metadata, read_error = _read_regular_file(
            path,
            limits.max_file_bytes,
        )
        if read_error:
            _skip(skipped, skipped_counts, relative, read_error)
            continue
        assert payload is not None and metadata is not None
        if accepted_bytes + len(payload) > limits.max_total_bytes:
            _skip(skipped, skipped_counts, relative, "total_bytes_exceeded")
            continue
        text = payload.decode("utf-8", errors="replace")
        rules = scan_text_shapes(text)
        if rules:
            secret_counts.update(rules)
            _skip_secret(
                skipped,
                skipped_counts,
                relative,
                list(rules),
            )
            continue
        if not text.strip():
            _skip(skipped, skipped_counts, relative, "empty_note")
            continue
        candidates.append(
            NoteCandidate(
                path=path,
                relative_path=relative,
                text=text,
                size=len(payload),
                mtime=metadata.st_mtime,
            )
        )
        accepted_bytes += len(payload)
    return (
        candidates,
        skipped,
        dict(sorted(skipped_counts.items())),
        dict(sorted(secret_counts.items())),
        scanned,
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


def _base_result(
    *,
    dry_run: bool,
    scanned: int,
    planned: int,
    skipped: list[dict[str, Any]],
    skipped_by_reason: dict[str, int],
    secret_shapes: dict[str, int],
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
        result["status"] = "error"
        result["error_code"] = "state_write_failed"
        totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
        result["facts_committed"] = any(
            int((totals or {}).get(key) or 0) > 0
            for key in ("ingested_this_run", "repaired_index", "repaired_daily")
        )
        return result


def ingest_notes(
    vault_dir: Path,
    obsidian_vault: Path,
    *,
    dry_run: bool,
    limits: Optional[NoteIngestionLimits] = None,
) -> dict[str, Any]:
    limits = limits or NoteIngestionLimits()
    candidates, skipped, skipped_by_reason, secret_shapes, scanned = discover_notes(
        Path(obsidian_vault),
        limits=limits,
    )
    records = [_record(candidate) for candidate in candidates]
    result = _base_result(
        dry_run=dry_run,
        scanned=scanned,
        planned=len(records),
        skipped=skipped,
        skipped_by_reason=skipped_by_reason,
        secret_shapes=secret_shapes,
    )
    vault = Path(vault_dir)
    index = vault / "index.jsonl"
    if dry_run:
        existing = _read_ids(index)
        result["totals"]["planned_this_run"] = sum(
            1 for record in records if record["id"] not in existing
        )
        return result
    if not records:
        return _persist_state(vault, result)

    index.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source_lock(index, exclusive=True):
            repaired_tails = 1 if _repair_partial_tail(index) else 0
            index_ids = _read_ids(index)
            daily_ids: dict[Path, set[str]] = {}
            checked_daily_tails: set[Path] = set()
            daily_appends: dict[Path, list[dict[str, Any]]] = {}
            index_appends: list[dict[str, Any]] = []
            repaired_index = 0
            repaired_daily = 0
            ingested = 0
            for record in records:
                daily = vault / "daily" / f"{str(record['timestamp'])[:10]}.jsonl"
                if daily not in checked_daily_tails:
                    if _repair_partial_tail(daily):
                        repaired_tails += 1
                    checked_daily_tails.add(daily)
                ids = daily_ids.setdefault(daily, _read_ids(daily))
                in_daily = record["id"] in ids
                in_index = record["id"] in index_ids
                if in_daily and in_index:
                    continue
                if not in_daily:
                    daily_appends.setdefault(daily, []).append(record)
                    ids.add(record["id"])
                    if in_index:
                        repaired_daily += 1
                if not in_index:
                    index_appends.append(record)
                    index_ids.add(record["id"])
                    if in_daily:
                        repaired_index += 1
                    ingested += 1
            for daily, rows in sorted(daily_appends.items(), key=lambda item: str(item[0])):
                _append_jsonl(daily, rows, public=True)
            if daily_appends:
                after_daily_append()
            _append_jsonl(index, index_appends, public=False)
            result["totals"]["ingested_this_run"] = ingested
            result["totals"]["repaired_index"] = repaired_index
            result["totals"]["repaired_daily"] = repaired_daily
            result["totals"]["repaired_tails"] = repaired_tails
    except Exception:
        result["status"] = "error"
        result["error_code"] = "write_interrupted"
        return result

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
