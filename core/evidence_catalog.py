#!/usr/bin/env python3
"""Bounded safe-metadata catalog for stable JSONL evidence identifiers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


DEFAULT_MAX_RECORDS = 100_000
DEFAULT_MAX_LINE_BYTES = 1024 * 1024
DEFAULT_MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_METADATA_CHARS = 512


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


def _normalize_source(raw_source: Any) -> Tuple[str, Optional[str]]:
    source = _required_text(
        raw_source,
        code="invalid_evidence_source",
        field="source",
    )
    lowered = source.casefold()
    if lowered == "codex" or lowered.startswith("codex-"):
        return "codex", None
    if lowered == "claude" or lowered.startswith("claude-"):
        return "claude", None
    if lowered in {"feishu", "lark"} or lowered.startswith(("feishu-", "lark-")):
        return "feishu", None
    if lowered == "web" or lowered.startswith("web-"):
        return "web", None
    if lowered == "local" or lowered.startswith("local-"):
        return "local", None
    if lowered == "custom":
        return "custom", "custom"
    return "custom", source


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


class EvidenceCatalog:
    """Resolve exact JSONL fact IDs without retaining raw evidence bodies."""

    def __init__(
        self,
        index_path: Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    ) -> None:
        self.index_path = Path(index_path)
        self.max_records = self._positive_limit(max_records, "max_records")
        self.max_line_bytes = self._positive_limit(
            max_line_bytes,
            "max_line_bytes",
        )
        self.max_source_bytes = self._positive_limit(
            max_source_bytes,
            "max_source_bytes",
        )
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._source_signature: Optional[Tuple[int, int, int, int, int]] = None
        self._source_digest: Optional[str] = None
        if self.index_path.exists():
            self._load()

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

    def _load(self) -> None:
        try:
            before = self.index_path.stat()
        except OSError as exc:
            raise EvidenceCatalogError(
                "source_unreadable",
                "evidence source cannot be inspected",
            ) from exc
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceCatalogError(
                "source_unreadable",
                "evidence source must be a regular file",
            )
        if before.st_size > self.max_source_bytes:
            raise EvidenceCatalogError(
                "catalog_limit_exceeded",
                "evidence source exceeds the bounded scan size",
            )

        entries: Dict[str, Dict[str, Any]] = {}
        digest = hashlib.sha256()
        bytes_read = 0
        try:
            with self.index_path.open("rb") as handle:
                if _signature(os.fstat(handle.fileno())) != _signature(before):
                    raise EvidenceCatalogError(
                        "source_changed",
                        "evidence source changed before catalog scan",
                    )
                line_number = 0
                while True:
                    raw = handle.readline(self.max_line_bytes + 1)
                    if not raw:
                        break
                    line_number += 1
                    bytes_read += len(raw)
                    digest.update(raw)
                    if len(raw) > self.max_line_bytes:
                        raise EvidenceCatalogError(
                            "catalog_line_too_large",
                            "evidence record exceeds the bounded line size",
                            line_number=line_number,
                        )
                    if not raw.endswith(b"\n") or not raw.strip():
                        raise EvidenceCatalogError(
                            "malformed_jsonl",
                            "blank or unterminated JSONL at line "
                            + str(line_number),
                            line_number=line_number,
                        )
                    if line_number > self.max_records:
                        raise EvidenceCatalogError(
                            "catalog_limit_exceeded",
                            "evidence source exceeds the bounded record count",
                            line_number=line_number,
                        )
                    row = _decode_record(raw[:-1], line_number)
                    raw_id = _required_identifier(
                        row.get("id"),
                        code="missing_evidence_id",
                        field="id",
                        line_number=line_number,
                    )
                    if raw_id in entries:
                        raise EvidenceCatalogError(
                            "duplicate_evidence_id",
                            "duplicate evidence id at line " + str(line_number),
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
                    normalized_source, detail = _normalize_source(row.get("source"))
                    ref: Dict[str, Any] = {
                        "evidence_id": raw_id,
                        "source": normalized_source,
                        "raw_id": raw_id,
                        "content_hash": _sha256(content),
                        "status": "available",
                        "observed_at": observed_at,
                        "privacy": "restricted",
                    }
                    if detail is not None:
                        ref["source_detail"] = detail
                    entries[raw_id] = ref
                after_fd = os.fstat(handle.fileno())
        except EvidenceCatalogError:
            raise
        except OSError as exc:
            raise EvidenceCatalogError(
                "source_unreadable",
                "evidence source cannot be read",
            ) from exc

        try:
            after_path = self.index_path.stat()
        except OSError as exc:
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source disappeared during catalog scan",
            ) from exc
        expected = _signature(before)
        if (
            _signature(after_fd) != expected
            or _signature(after_path) != expected
            or bytes_read != before.st_size
        ):
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source changed during catalog scan",
            )
        self._entries = entries
        self._source_signature = expected
        self._source_digest = digest.hexdigest()

    def _source_state(self) -> str:
        if self._source_signature is None:
            return "missing" if not self.index_path.exists() else "changed"
        try:
            current = self.index_path.stat()
        except FileNotFoundError:
            return "deleted"
        except OSError:
            return "changed"
        if _signature(current) != self._source_signature:
            return "changed"
        return "current"

    def resolve(self, requested_id: str) -> Dict[str, Any]:
        raw_id = _required_identifier(
            requested_id,
            code="evidence_id_required",
            field="requested_id",
        )
        state = self._source_state()
        if state == "changed":
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source changed after catalog verification",
            )
        existing = self._entries.get(raw_id)
        if existing is None:
            raise EvidenceCatalogError(
                "evidence_not_found",
                "requested evidence ID was not confirmed in JSONL",
            )
        result = dict(existing)
        if state == "deleted":
            result["status"] = "source_deleted"
        return result

    def list(self) -> List[Dict[str, Any]]:
        state = self._source_state()
        if state == "changed":
            raise EvidenceCatalogError(
                "source_changed",
                "evidence source changed after catalog verification",
            )
        rows = []
        for raw_id in sorted(self._entries):
            row = dict(self._entries[raw_id])
            if state == "deleted":
                row["status"] = "source_deleted"
            rows.append(row)
        return rows

    def from_legacy(
        self,
        *,
        source: str,
        raw_id: Optional[str],
        timestamp: str,
        statement: str,
        source_deleted: bool = False,
    ) -> Dict[str, Any]:
        normalized_source, detail = _normalize_source(source)
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
                if exc.code not in {"evidence_not_found"}:
                    raise

        legacy_seed = {
            "raw_id": normalized_raw_id,
            "source": source.strip(),
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
        result: Dict[str, Any] = {
            "evidence_id": evidence_id,
            "source": normalized_source,
            "raw_id": normalized_raw_id,
            "content_hash": _sha256(body),
            "status": "source_deleted" if deleted else "source_broken",
            "observed_at": observed_at,
            "privacy": "restricted",
        }
        if detail is not None:
            result["source_detail"] = detail
        return result
