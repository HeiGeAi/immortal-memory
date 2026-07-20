#!/usr/bin/env python3
"""On-demand stable evidence resolution against an authoritative JSONL source."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from index_integrity import INDEX_SCHEMA_VERSION, locator_schema_is_current
from model_types import (
    ModelValidationError,
    canonical_evidence_source,
    new_evidence_ref,
)


DEFAULT_MAX_SCAN_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_RECORDS = 1_000_000
DEFAULT_MAX_LINE_BYTES = 1024 * 1024
DEFAULT_MAX_FALLBACK_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_FALLBACK_RECORDS = 100_000
DEFAULT_MAX_CACHE_ENTRIES = 128
DEFAULT_MAX_BATCH_IDS = 10_000
MAX_METADATA_CHARS = 512

REQUIRED_DATABASE_META = frozenset(
    {
        "last_size",
        "source_size",
        "source_dev",
        "source_ino",
        "source_mtime_ns",
        "source_ctime_ns",
        "prefix_sha256",
        "indexed_id_count",
        "last_line",
        "parity_status",
        "index_schema_version",
    }
)


class EvidenceCatalogError(RuntimeError):
    """A fail-closed catalog error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        line_number: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.line_number = line_number


def _signature(value: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _readline(handle: Any, limit: int) -> bytes:
    """Small seam for deterministic source-growth tests."""
    return handle.readline(limit)


def _required_text(
    value: Any,
    *,
    code: str,
    field: str,
    line_number: Optional[int] = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceCatalogError(
            code,
            field + " must be a non-empty string",
            line_number=line_number,
        )
    normalized = value.strip()
    if len(normalized) > MAX_METADATA_CHARS:
        raise EvidenceCatalogError(
            "catalog_metadata_too_large",
            field + " exceeds the safe metadata limit",
            line_number=line_number,
        )
    return normalized


def _required_identifier(
    value: Any,
    *,
    code: str,
    field: str,
    line_number: Optional[int] = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceCatalogError(
            code,
            field + " must be a non-empty string",
            line_number=line_number,
        )
    if len(value) > MAX_METADATA_CHARS:
        raise EvidenceCatalogError(
            "catalog_metadata_too_large",
            field + " exceeds the safe metadata limit",
            line_number=line_number,
        )
    return value


def _required_body(
    value: Any,
    *,
    code: str,
    field: str,
    line_number: Optional[int] = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceCatalogError(
            code,
            field + " must be a non-empty string",
            line_number=line_number,
        )
    return value


def _decode_record(raw: bytes, line_number: int) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceCatalogError(
            "malformed_jsonl",
            "malformed JSONL at line " + str(line_number),
            line_number=line_number,
        ) from exc
    if not isinstance(value, Mapping):
        raise EvidenceCatalogError(
            "malformed_jsonl",
            "JSONL record must be an object at line " + str(line_number),
            line_number=line_number,
        )
    return value


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def _assert_no_symlink_chain(path: Path) -> Path:
    candidate = _absolute(path)
    current = candidate
    while True:
        try:
            metadata = os.lstat(str(current))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise EvidenceCatalogError(
                "unsafe_path",
                "path chain cannot be inspected safely",
            ) from exc
        else:
            if stat.S_ISLNK(metadata.st_mode):
                raise EvidenceCatalogError(
                    "unsafe_path",
                    "symlink paths are not allowed",
                )
        parent = current.parent
        if parent == current:
            break
        current = parent
    return candidate


def _safe_regular_stat(path: Path, *, missing_ok: bool = False) -> Optional[os.stat_result]:
    candidate = _assert_no_symlink_chain(path)
    try:
        metadata = os.lstat(str(candidate))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise EvidenceCatalogError(
            "source_unreadable",
            "required catalog path does not exist",
        )
    except OSError as exc:
        raise EvidenceCatalogError(
            "source_unreadable",
            "catalog path cannot be inspected",
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise EvidenceCatalogError(
            "unsafe_path",
            "catalog paths must be regular files",
        )
    return metadata


@contextmanager
def _open_regular_nofollow(path: Path) -> Iterator[Any]:
    candidate = _assert_no_symlink_chain(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise EvidenceCatalogError(
            "source_unreadable",
            "catalog source cannot be opened safely",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(str(candidate))
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _signature(opened) != _signature(current)
        ):
            raise EvidenceCatalogError(
                "unsafe_path",
                "catalog source identity changed during open",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(descriptor)


@contextmanager
def _readonly_database(path: Path) -> Iterator[sqlite3.Connection]:
    candidate = _assert_no_symlink_chain(path)
    metadata = _safe_regular_stat(candidate)
    assert metadata is not None
    wal_path = Path(str(candidate) + "-wal")
    if _lexists(wal_path):
        wal = _safe_regular_stat(wal_path)
        if wal is not None and wal.st_size:
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite index has a non-empty WAL",
            )

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise EvidenceCatalogError(
            "database_untrusted",
            "SQLite index cannot be opened safely",
        ) from exc
    connection: Optional[sqlite3.Connection] = None
    try:
        if _signature(os.fstat(descriptor)) != _signature(metadata):
            raise EvidenceCatalogError(
                "unsafe_path",
                "SQLite index identity changed during open",
            )
        uri = candidate.as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
        current = os.lstat(str(candidate))
        if _signature(current) != _signature(metadata):
            raise EvidenceCatalogError(
                "unsafe_path",
                "SQLite index identity changed during connect",
            )
        yield connection
        try:
            after_path = os.lstat(str(candidate))
        except OSError as exc:
            raise EvidenceCatalogError(
                "unsafe_path",
                "SQLite index disappeared during read",
            ) from exc
        if (
            _signature(os.fstat(descriptor)) != _signature(metadata)
            or _signature(after_path) != _signature(metadata)
        ):
            raise EvidenceCatalogError(
                "unsafe_path",
                "SQLite index identity changed during read",
            )
    except sqlite3.DatabaseError as exc:
        raise EvidenceCatalogError(
            "database_untrusted",
            "SQLite index cannot be queried safely",
        ) from exc
    finally:
        if connection is not None:
            connection.close()
        os.close(descriptor)


class EvidenceCatalog:
    """Resolve one stable ID at a time without materializing the full source."""

    def __init__(
        self,
        index_path: Path,
        *,
        database_path: Optional[Path] = None,
        max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_fallback_bytes: int = DEFAULT_MAX_FALLBACK_BYTES,
        max_fallback_records: int = DEFAULT_MAX_FALLBACK_RECORDS,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
        max_batch_ids: int = DEFAULT_MAX_BATCH_IDS,
    ) -> None:
        self.index_path = _absolute(Path(index_path))
        self.max_scan_bytes = self._positive_limit(
            max_scan_bytes,
            "max_scan_bytes",
        )
        self.max_records = self._positive_limit(max_records, "max_records")
        self.max_line_bytes = self._positive_limit(
            max_line_bytes,
            "max_line_bytes",
        )
        self.max_fallback_bytes = self._positive_limit(
            max_fallback_bytes,
            "max_fallback_bytes",
        )
        self.max_fallback_records = self._positive_limit(
            max_fallback_records,
            "max_fallback_records",
        )
        self.max_cache_entries = self._positive_limit(
            max_cache_entries,
            "max_cache_entries",
        )
        self.max_batch_ids = self._positive_limit(
            max_batch_ids,
            "max_batch_ids",
        )
        self._resolved_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        source_stat = _safe_regular_stat(self.index_path, missing_ok=True)
        self._source_signature = (
            _signature(source_stat) if source_stat is not None else None
        )
        self._source_size = int(source_stat.st_size) if source_stat is not None else 0

        explicit_database = database_path is not None
        candidate_database = (
            _absolute(Path(database_path))
            if explicit_database
            else self.index_path.with_name("search_index.db")
        )
        if explicit_database or _lexists(candidate_database):
            self.database_path: Optional[Path] = candidate_database
        else:
            self.database_path = None
        self._database_signature: Optional[Tuple[int, int, int, int, int]] = None
        self._database_meta: Dict[str, str] = {}
        if self.database_path is not None:
            if source_stat is None:
                raise EvidenceCatalogError(
                    "database_stale",
                    "SQLite index cannot be trusted without its JSONL source",
                )
            self._verify_database_revision(source_stat)

    @staticmethod
    def _positive_limit(value: Any, name: str) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
        ):
            raise EvidenceCatalogError(
                "invalid_catalog_limit",
                name + " must be a positive integer",
            )
        return value

    def _verify_database_revision(self, source_stat: os.stat_result) -> None:
        assert self.database_path is not None
        database_stat = _safe_regular_stat(self.database_path)
        assert database_stat is not None
        try:
            with _readonly_database(self.database_path) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' "
                        "AND name IN ('docs','docs_fts','meta')"
                    )
                }
                if tables != {"docs", "docs_fts", "meta"}:
                    raise EvidenceCatalogError(
                        "database_untrusted",
                        "SQLite index is missing required tables",
                    )
                locator_schema_current = locator_schema_is_current(connection)
                meta = {
                    str(key): str(value)
                    for key, value in connection.execute(
                        "SELECT key,value FROM meta"
                    )
                }
        except EvidenceCatalogError:
            raise
        if not REQUIRED_DATABASE_META.issubset(meta):
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite index is missing trusted revision metadata",
            )
        if (
            meta["index_schema_version"] != str(INDEX_SCHEMA_VERSION)
            or not locator_schema_current
        ):
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite index locator schema is missing or obsolete",
            )
        if meta["parity_status"] != "trusted":
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite index parity is not trusted",
            )
        digest = meta["prefix_sha256"]
        try:
            valid_digest = len(digest) == 64 and int(digest, 16) >= 0
        except ValueError:
            valid_digest = False
        if not valid_digest:
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite source digest metadata is invalid",
            )
        expected = {
            "last_size": source_stat.st_size,
            "source_size": source_stat.st_size,
            "source_dev": source_stat.st_dev,
            "source_ino": source_stat.st_ino,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "source_ctime_ns": source_stat.st_ctime_ns,
        }
        try:
            stale = any(int(meta[key]) != int(value) for key, value in expected.items())
            indexed_count = int(meta["indexed_id_count"])
            last_line = int(meta["last_line"])
        except (TypeError, ValueError) as exc:
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite revision metadata is not numeric",
            ) from exc
        if stale:
            raise EvidenceCatalogError(
                "database_stale",
                "SQLite revision does not match the JSONL source",
            )
        if (
            indexed_count < 0
            or last_line != indexed_count
        ):
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite indexed count metadata is inconsistent",
            )
        self._database_signature = _signature(database_stat)
        self._database_meta = meta

    def preflight(self) -> Dict[str, Any]:
        state = self._source_state()
        return {
            "mode": "verified_sqlite" if self.database_path is not None else "jsonl_stream",
            "source_state": state,
            "source_size": self._source_size,
            "indexed_id_count": (
                int(self._database_meta["indexed_id_count"])
                if self._database_meta
                else None
            ),
            "bounded_scan": True,
        }

    def _source_state(self) -> str:
        if self._source_signature is None:
            return "missing" if not _lexists(self.index_path) else "changed"
        if not _lexists(self.index_path):
            return "deleted"
        try:
            current = _safe_regular_stat(self.index_path)
        except EvidenceCatalogError:
            return "changed"
        assert current is not None
        return (
            "current"
            if _signature(current) == self._source_signature
            else "changed"
        )

    @contextmanager
    def _verified_source_snapshot(self) -> Iterator[Any]:
        expected = self._source_signature
        if expected is None:
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source is not available for snapshot verification",
            )
        with _open_regular_nofollow(self.index_path) as handle:
            if _signature(os.fstat(handle.fileno())) != expected:
                raise EvidenceCatalogError(
                    "source_changed",
                    "evidence source changed before resolution",
                )
            body_failed = False
            try:
                yield handle
            except BaseException:
                body_failed = True
                raise
            finally:
                opened_after = os.fstat(handle.fileno())
                try:
                    current = _safe_regular_stat(self.index_path)
                except EvidenceCatalogError as exc:
                    if not body_failed:
                        raise EvidenceCatalogError(
                            "source_changed",
                            "evidence source changed during resolution",
                        ) from exc
                    current = None
                if (
                    not body_failed
                    and (
                        current is None
                        or _signature(opened_after) != expected
                        or _signature(current) != expected
                    )
                ):
                    raise EvidenceCatalogError(
                        "source_changed",
                        "evidence source changed during resolution",
                    )

    def _cache_get(self, raw_id: str) -> Optional[Dict[str, Any]]:
        existing = self._resolved_cache.get(raw_id)
        if existing is None:
            return None
        self._resolved_cache.move_to_end(raw_id)
        return dict(existing)

    def _cache_put(self, raw_id: str, ref: Mapping[str, Any]) -> None:
        self._resolved_cache[raw_id] = dict(ref)
        self._resolved_cache.move_to_end(raw_id)
        while len(self._resolved_cache) > self.max_cache_entries:
            self._resolved_cache.popitem(last=False)

    def _database_candidate(self, raw_id: str) -> Optional[Dict[str, Any]]:
        assert self.database_path is not None
        database_stat = _safe_regular_stat(self.database_path)
        assert database_stat is not None
        if _signature(database_stat) != self._database_signature:
            raise EvidenceCatalogError(
                "database_changed",
                "SQLite index changed after revision verification",
            )
        with _readonly_database(self.database_path) as connection:
            rows = connection.execute(
                "SELECT d.rec_id,d.ts,d.source,"
                "d.source_offset,d.source_length,d.line_number,d.content_sha256 "
                "FROM docs AS d INDEXED BY idx_docs_rec_id "
                "WHERE d.rec_id=? LIMIT 2",
                (raw_id,),
            ).fetchall()
            if len(rows) > 1:
                raise EvidenceCatalogError(
                    "database_untrusted",
                    "SQLite index contains duplicate stable IDs",
                )
            if not rows:
                return None
            (
                rec_id,
                timestamp,
                source,
                source_offset,
                source_length,
                line_number,
                content_sha256,
            ) = rows[0]
            if (
                not isinstance(source_offset, int)
                or isinstance(source_offset, bool)
                or not isinstance(source_length, int)
                or isinstance(source_length, bool)
                or not isinstance(line_number, int)
                or isinstance(line_number, bool)
                or source_offset < 0
                or source_length < 1
                or line_number < 1
                or line_number > int(self._database_meta["indexed_id_count"])
                or source_offset + source_length > self._source_size
                or not isinstance(content_sha256, str)
                or len(content_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in content_sha256
                )
            ):
                raise EvidenceCatalogError(
                    "database_untrusted",
                    "SQLite evidence locator is invalid",
                )
            previous = connection.execute(
                "SELECT source_offset,source_length FROM docs "
                "INDEXED BY idx_docs_line_number WHERE line_number=?",
                (line_number - 1,),
            ).fetchone()
            following = connection.execute(
                "SELECT source_offset FROM docs "
                "INDEXED BY idx_docs_line_number WHERE line_number=?",
                (line_number + 1,),
            ).fetchone()
            offset_previous = connection.execute(
                "SELECT source_offset,source_length FROM docs "
                "INDEXED BY idx_docs_source_offset "
                "WHERE source_offset < ? "
                "ORDER BY source_offset DESC LIMIT 1",
                (source_offset,),
            ).fetchone()
            offset_following = connection.execute(
                "SELECT source_offset FROM docs "
                "INDEXED BY idx_docs_source_offset "
                "WHERE source_offset > ? "
                "ORDER BY source_offset ASC LIMIT 1",
                (source_offset,),
            ).fetchone()
        indexed_count = int(self._database_meta["indexed_id_count"])
        previous_valid = (
            (line_number == 1 and source_offset == 0 and previous is None)
            or (
                line_number > 1
                and previous is not None
                and int(previous[0]) + int(previous[1]) == source_offset
            )
        )
        following_valid = (
            (
                line_number == indexed_count
                and source_offset + source_length == self._source_size
                and following is None
            )
            or (
                line_number < indexed_count
                and following is not None
                and source_offset + source_length == int(following[0])
            )
        )
        offset_previous_valid = (
            (source_offset == 0 and offset_previous is None)
            or (
                source_offset > 0
                and offset_previous is not None
                and int(offset_previous[0]) + int(offset_previous[1])
                == source_offset
            )
        )
        offset_following_valid = (
            (
                source_offset + source_length == self._source_size
                and offset_following is None
            )
            or (
                source_offset + source_length < self._source_size
                and offset_following is not None
                and int(offset_following[0])
                == source_offset + source_length
            )
        )
        if (
            not previous_valid
            or not following_valid
            or not offset_previous_valid
            or not offset_following_valid
        ):
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite evidence locator is not contiguous with its neighbors",
            )
        if not isinstance(timestamp, str) or not isinstance(source, str):
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite evidence metadata is invalid",
            )
        return {
            "id": str(rec_id),
            "timestamp": str(timestamp),
            "source": str(source),
            "source_offset": source_offset,
            "source_length": source_length,
            "line_number": line_number,
            "content_sha256": content_sha256,
        }

    @staticmethod
    def _candidate_matches(
        row: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> bool:
        content = row.get("content")
        return (
            isinstance(content, str)
            and hashlib.sha256(content.encode("utf-8")).hexdigest()
            == candidate.get("content_sha256")
            and all(
                isinstance(row.get(field), str)
                and row[field] == candidate[field]
                for field in ("id", "timestamp", "source")
            )
        )

    def _resolve_locator(
        self,
        handle: Any,
        raw_id: str,
        candidate: Mapping[str, Any],
    ) -> Dict[str, Any]:
        source_length = int(candidate["source_length"])
        line_number = int(candidate["line_number"])
        if source_length > self.max_line_bytes:
            raise EvidenceCatalogError(
                "catalog_line_too_large",
                "evidence record exceeds the bounded line size",
                line_number=line_number,
            )
        try:
            raw = os.pread(
                handle.fileno(),
                source_length,
                int(candidate["source_offset"]),
            )
        except OSError as exc:
            raise EvidenceCatalogError(
                "source_unreadable",
                "authoritative evidence record cannot be read",
                line_number=line_number,
            ) from exc
        if (
            len(raw) != source_length
            or not raw.endswith(b"\n")
            or not raw.strip()
            or b"\n" in raw[:-1]
        ):
            raise EvidenceCatalogError(
                "database_untrusted",
                "SQLite evidence locator does not identify one JSONL record",
                line_number=line_number,
            )
        row = _decode_record(raw[:-1], line_number)
        authoritative_id = _required_identifier(
            row.get("id"),
            code="missing_evidence_id",
            field="id",
            line_number=line_number,
        )
        if authoritative_id != raw_id or not self._candidate_matches(row, candidate):
            raise EvidenceCatalogError(
                "database_source_mismatch",
                "SQLite locator metadata does not match authoritative JSONL",
                line_number=line_number,
            )
        content = _required_body(
            row.get("content"),
            code="invalid_evidence_record",
            field="content",
            line_number=line_number,
        )
        observed_at = _required_text(
            row.get("timestamp"),
            code="invalid_evidence_record",
            field="timestamp",
            line_number=line_number,
        )
        source = _required_text(
            row.get("source"),
            code="invalid_evidence_source",
            field="source",
            line_number=line_number,
        )
        return self._make_ref(
            evidence_id=authoritative_id,
            source=source,
            raw_id=authoritative_id,
            content=content,
            status="available",
            observed_at=observed_at,
        )

    @staticmethod
    def _make_ref(
        *,
        evidence_id: str,
        source: str,
        raw_id: Optional[str],
        content: str,
        status: str,
        observed_at: str,
    ) -> Dict[str, Any]:
        try:
            canonical_source, source_detail = canonical_evidence_source(source)
            return new_evidence_ref(
                evidence_id=evidence_id,
                source=canonical_source,
                source_detail=source_detail,
                raw_id=raw_id,
                content_hash=_sha256(content),
                status=status,
                privacy="restricted",
                observed_at=observed_at,
            )
        except ModelValidationError as exc:
            raise EvidenceCatalogError(
                "invalid_evidence_record",
                str(exc),
            ) from exc

    def _scan_source(
        self,
        *,
        requested_ids: Optional[set],
        candidates: Optional[Mapping[str, Mapping[str, Any]]],
        collect: bool,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        if self._source_state() != "current":
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source changed after catalog preflight",
            )
        if self._source_size > self.max_scan_bytes:
            raise EvidenceCatalogError(
                "catalog_limit_exceeded",
                "evidence source exceeds the bounded scan budget",
            )
        fallback = self.database_path is None
        if fallback and self._source_size > self.max_fallback_bytes:
            raise EvidenceCatalogError(
                "database_required",
                "large evidence sources require a verified SQLite index",
            )

        expected_signature = self._source_signature
        found: Dict[str, Dict[str, Any]] = {}
        rows: List[Dict[str, Any]] = []
        seen_ids = set() if fallback else None
        bytes_read = 0
        record_count = 0
        with _open_regular_nofollow(self.index_path) as handle:
            opened = os.fstat(handle.fileno())
            if _signature(opened) != expected_signature:
                raise EvidenceCatalogError(
                    "source_changed",
                    "evidence source changed before scan",
                )
            while True:
                raw = _readline(handle, self.max_line_bytes + 1)
                if not raw:
                    break
                bytes_read += len(raw)
                if bytes_read > self.max_scan_bytes:
                    raise EvidenceCatalogError(
                        "catalog_limit_exceeded",
                        "evidence scan exceeded its byte budget",
                    )
                record_count += 1
                bounded_collection = fallback or collect
                record_limit = (
                    min(self.max_records, self.max_fallback_records)
                    if bounded_collection
                    else self.max_records
                )
                if record_count > record_limit:
                    raise EvidenceCatalogError(
                        "catalog_limit_exceeded",
                        "evidence scan exceeded its record budget",
                        line_number=record_count,
                    )
                if len(raw) > self.max_line_bytes:
                    raise EvidenceCatalogError(
                        "catalog_line_too_large",
                        "evidence record exceeds the bounded line size",
                        line_number=record_count,
                    )
                if not raw.endswith(b"\n") or not raw.strip():
                    raise EvidenceCatalogError(
                        "malformed_jsonl",
                        "blank or unterminated JSONL at line "
                        + str(record_count),
                        line_number=record_count,
                    )
                row = _decode_record(raw[:-1], record_count)
                raw_id = _required_identifier(
                    row.get("id"),
                    code="missing_evidence_id",
                    field="id",
                    line_number=record_count,
                )
                if seen_ids is not None:
                    if raw_id in seen_ids:
                        raise EvidenceCatalogError(
                            "duplicate_evidence_id",
                            "duplicate evidence id at line " + str(record_count),
                            line_number=record_count,
                        )
                    seen_ids.add(raw_id)
                target = requested_ids is not None and raw_id in requested_ids
                if not (bounded_collection or target):
                    continue
                content = _required_body(
                    row.get("content"),
                    code="invalid_evidence_record",
                    field="content",
                    line_number=record_count,
                )
                observed_at = _required_text(
                    row.get("timestamp"),
                    code="invalid_evidence_record",
                    field="timestamp",
                    line_number=record_count,
                )
                source = _required_text(
                    row.get("source"),
                    code="invalid_evidence_source",
                    field="source",
                    line_number=record_count,
                )
                ref = self._make_ref(
                    evidence_id=raw_id,
                    source=source,
                    raw_id=raw_id,
                    content=content,
                    status="available",
                    observed_at=observed_at,
                )
                if collect:
                    rows.append(ref)
                if target:
                    if raw_id in found:
                        raise EvidenceCatalogError(
                            "duplicate_evidence_id",
                            "requested evidence ID appears more than once",
                            line_number=record_count,
                        )
                    candidate = (
                        candidates.get(raw_id)
                        if candidates is not None
                        else None
                    )
                    if candidate is not None and not self._candidate_matches(row, candidate):
                        raise EvidenceCatalogError(
                            "database_source_mismatch",
                            "SQLite candidate does not match authoritative JSONL",
                            line_number=record_count,
                        )
                    found[raw_id] = ref
                    if (
                        candidates is not None
                        and requested_ids is not None
                        and len(found) == len(requested_ids)
                        and not collect
                    ):
                        break
            opened_after = os.fstat(handle.fileno())

        current = _safe_regular_stat(self.index_path)
        if (
            current is None
            or _signature(opened_after) != expected_signature
            or _signature(current) != expected_signature
        ):
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source changed during scan",
            )
        return found, rows

    def _scan_for_id(
        self,
        raw_id: str,
        candidate: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        candidates = {raw_id: candidate} if candidate is not None else None
        found, _rows = self._scan_source(
            requested_ids={raw_id},
            candidates=candidates,
            collect=False,
        )
        if raw_id not in found:
            raise EvidenceCatalogError(
                "evidence_not_found",
                "requested evidence ID was not confirmed in JSONL",
            )
        return found[raw_id]

    def resolve(self, requested_id: str) -> Dict[str, Any]:
        raw_id = _required_identifier(
            requested_id,
            code="evidence_id_required",
            field="requested_id",
        )
        state = self._source_state()
        cached = self._cache_get(raw_id)
        if state == "missing":
            raise EvidenceCatalogError(
                "evidence_not_found",
                "missing source cannot confirm an evidence ID",
            )
        if state == "deleted":
            if cached is None:
                raise EvidenceCatalogError(
                    "evidence_not_found",
                    "deleted source cannot confirm an uncached evidence ID",
                )
            cached["status"] = "source_deleted"
            return cached
        if state != "current":
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source changed after catalog preflight",
            )
        with self._verified_source_snapshot() as handle:
            if cached is not None:
                return cached
            if self.database_path is not None:
                candidate = self._database_candidate(raw_id)
                if candidate is None:
                    raise EvidenceCatalogError(
                        "evidence_not_found",
                        "requested evidence ID was not present in verified SQLite",
                    )
                ref = self._resolve_locator(handle, raw_id, candidate)
            else:
                ref = self._scan_for_id(raw_id)
        self._cache_put(raw_id, ref)
        return dict(ref)

    def resolve_many(self, requested_ids: List[str]) -> List[Dict[str, Any]]:
        if not isinstance(requested_ids, list):
            raise EvidenceCatalogError(
                "invalid_evidence_ids",
                "requested evidence IDs must be a list",
            )
        if len(requested_ids) > self.max_batch_ids:
            raise EvidenceCatalogError(
                "catalog_limit_exceeded",
                "evidence batch exceeds the bounded request count",
            )
        normalized = [
            _required_identifier(
                raw_id,
                code="evidence_id_required",
                field="requested_id",
            )
            for raw_id in requested_ids
        ]
        if not normalized:
            return []
        if self._source_state() != "current":
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source changed after catalog preflight",
            )
        unique_ids = set(normalized)
        candidates: Optional[Dict[str, Dict[str, Any]]] = None
        if self.database_path is not None:
            candidates = {}
            for raw_id in unique_ids:
                candidate = self._database_candidate(raw_id)
                if candidate is None:
                    raise EvidenceCatalogError(
                        "evidence_not_found",
                        "requested evidence ID was not present in verified SQLite",
                    )
                candidates[raw_id] = candidate
        found, _rows = self._scan_source(
            requested_ids=unique_ids,
            candidates=candidates,
            collect=False,
        )
        missing = unique_ids - set(found)
        if missing:
            raise EvidenceCatalogError(
                "evidence_not_found",
                "requested evidence ID was not confirmed in JSONL",
            )
        for raw_id, ref in found.items():
            self._cache_put(raw_id, ref)
        return [dict(found[raw_id]) for raw_id in normalized]

    def list(self) -> List[Dict[str, Any]]:
        if self._source_state() == "missing":
            return []
        if self._source_size > self.max_fallback_bytes:
            raise EvidenceCatalogError(
                "catalog_limit_exceeded",
                "full evidence listing exceeds the bounded fallback size",
            )
        _found, rows = self._scan_source(
            requested_ids=None,
            candidates=None,
            collect=True,
        )
        return sorted(rows, key=lambda row: str(row["evidence_id"]))

    def from_legacy(
        self,
        *,
        source: str,
        raw_id: Optional[str],
        timestamp: str,
        statement: str,
        source_deleted: bool = False,
    ) -> Dict[str, Any]:
        source_value = _required_text(
            source,
            code="invalid_evidence_source",
            field="source",
        )
        observed_at = _required_text(
            timestamp,
            code="invalid_evidence_record",
            field="timestamp",
        )
        body = _required_body(
            statement,
            code="invalid_evidence_record",
            field="statement",
        )
        if len(body.encode("utf-8")) > self.max_line_bytes:
            raise EvidenceCatalogError(
                "catalog_line_too_large",
                "legacy evidence body exceeds the bounded line size",
            )
        normalized_raw_id: Optional[str] = None
        if raw_id is not None:
            normalized_raw_id = _required_identifier(
                raw_id,
                code="invalid_evidence_record",
                field="raw_id",
            )
            try:
                return self.resolve(normalized_raw_id)
            except EvidenceCatalogError as exc:
                if exc.code != "evidence_not_found":
                    raise

        legacy_seed = {
            "raw_id": normalized_raw_id,
            "source": source_value,
            "statement_sha256": _sha256(body),
            "timestamp": observed_at,
        }
        encoded_seed = json.dumps(
            legacy_seed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_id = "ev_legacy_" + hashlib.sha256(
            encoded_seed.encode("utf-8")
        ).hexdigest()
        deleted = source_deleted or self._source_state() == "deleted"
        return self._make_ref(
            evidence_id=evidence_id,
            source=source_value,
            raw_id=normalized_raw_id,
            content=body,
            status="source_deleted" if deleted else "source_broken",
            observed_at=observed_at,
        )
