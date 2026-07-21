#!/usr/bin/env python3
"""Bounded, privacy-safe read models for the Immortal product API."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from claim_store import ClaimStore
from context_store import ContextStore
from control_center import ControlCenter
from control_data import ControlData
from event_store import (
    EventPathError,
    _exclusive_lock,
    safe_atomic_write_text,
    safe_read_text,
)
from index_integrity import (
    INDEX_SCHEMA_VERSION,
    locator_schema_is_current,
    normalize_timestamp_utc,
)
from index_locks import index_lock_pair
from judgment_store import JudgmentStore
from living_self_service import LivingSelfService
from outcome_store import OutcomeStore
from redact_common import redact


MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 20
CURSOR_SCHEMA_VERSION = 1
SUMMARY_CHARS = 180
DETAIL_CHARS = 12000
ID_PATTERN = re.compile(r"\A[A-Za-z0-9._:@+-]{1,180}\Z")
CURSOR_PATTERN = re.compile(r"\A[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")
SELF_SECTIONS = (
    "identity_commitments",
    "values",
    "expression_dna",
    "mental_models",
    "decision_heuristics",
    "anti_patterns",
    "tensions",
    "honest_boundaries",
)
INDEX_META_KEYS = (
    "parity_status",
    "last_size",
    "source_dev",
    "source_ino",
    "source_mtime_ns",
    "source_ctime_ns",
    "indexed_id_count",
    "indexed_ids_sha256",
    "index_schema_version",
)


class ProductDataError(ValueError):
    """A product read failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        decoded = base64.b64decode(
            (value + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        if _b64_encode(decoded) != value:
            raise ValueError("non-canonical base64")
        return decoded
    except (ValueError, UnicodeEncodeError) as exc:
        raise ProductDataError("invalid_cursor", "分页游标无效") from exc


def _compact_text(value: Any, maximum: int = SUMMARY_CHARS) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"-{5}BEGIN ([A-Z0-9 ]*PRIVATE KEY)-{5}.*?"
        r"-{5}END \1-{5}",
        "[REDACTED_PRIVATE_KEY]",
        text,
    )
    text = re.sub(
        r"(?i)(https?://)[^/@\s|]+:[^@\s|]+@",
        r"\1[REDACTED]@",
        text,
    )
    text = redact(text)
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:Cookie|Set-Cookie)\s*:\s*[^|]+",
        "Cookie" + ": [REDACTED] ",
        text,
    )
    text = re.sub(r"\bou_[A-Za-z0-9_-]{8,}\b", "ou_[REDACTED]", text)
    text = re.sub(
        r"(?<![A-Za-z0-9])/(?:Users|home)/[^\s|]+",
        "/[HOME]/[REDACTED]",
        text,
    )
    text = re.sub(
        r"-{5}BEGIN [A-Z0-9 ]*PRIVATE KEY-{5}",
        "[REDACTED_PRIVATE_KEY]",
        text,
    )
    text = re.sub(
        r"-{5}END [A-Z0-9 ]*PRIVATE KEY-{5}",
        "[REDACTED_PRIVATE_KEY]",
        text,
    )
    if len(text) > maximum:
        return text[: max(0, maximum - 3)].rstrip() + "..."
    return text


def _safe_strings(values: Any, maximum: int = SUMMARY_CHARS) -> List[str]:
    if not isinstance(values, list):
        return []
    return [_compact_text(value, maximum) for value in values[:MAX_PAGE_SIZE]]


def _safe_ids(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:MAX_PAGE_SIZE]:
        text = str(value or "")
        if ID_PATTERN.fullmatch(text):
            result.append(text)
    return result


def _canonical_filters(filters: Mapping[str, str]) -> str:
    return json.dumps(
        dict(sorted(filters.items())),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _safe_identifier(value: Any, *, code: str) -> str:
    candidate = str(value or "").strip()
    if ID_PATTERN.fullmatch(candidate) is None:
        raise ProductDataError(code, "标识符无效")
    return candidate


def _safe_sequence(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("authority list is invalid")
    result = []
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError("authority row is invalid")
        result.append(dict(row))
    return result


def _safe_product_tree(value: Any, depth: int = 0) -> Any:
    """Redact nested operational data and remove execution-only fields."""
    if depth > 8:
        return None
    if isinstance(value, str):
        return _compact_text(value, 4000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [
            _safe_product_tree(item, depth + 1)
            for item in list(value)[:MAX_PAGE_SIZE]
        ]
    if isinstance(value, Mapping):
        result = {}
        forbidden = {
            "args",
            "argv",
            "command",
            "commands",
            "cwd",
            "path",
            "paths",
            "stderr",
            "stdout",
        }
        for raw_key, item in list(value.items())[:200]:
            key = str(raw_key)
            lowered = key.casefold()
            if (
                lowered in forbidden
                or lowered.endswith("_path")
                or lowered.endswith("_command")
                or lowered.endswith("_args")
            ):
                continue
            result[key] = _safe_product_tree(item, depth + 1)
        return result
    return None


class ProductIndexIntegrity:
    """Open one trusted source and SQLite generation without scanning JSONL."""

    def __init__(self, vault_dir: Path) -> None:
        self.vault_dir = Path(os.path.abspath(str(vault_dir)))
        self.source_path = self.vault_dir / "index.jsonl"
        self.database_path = self.vault_dir / "search_index.db"
        self._forced_untrusted = ""
        self._verified_database_generation = None

    def mark_untrusted(self, reason: str) -> None:
        self._forced_untrusted = str(reason or "untrusted")

    @staticmethod
    def _regular_file(path: Path) -> os.stat_result:
        metadata = os.lstat(str(path))
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OSError("regular file required")
        return metadata

    @staticmethod
    def _signature(value: os.stat_result) -> Tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def _connect(self) -> sqlite3.Connection:
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _validate(
        self,
        connection: sqlite3.Connection,
        source_stat: os.stat_result,
    ) -> Dict[str, str]:
        rows = dict(
            connection.execute(
                "SELECT key,value FROM meta WHERE key IN ("
                + ",".join("?" for _key in INDEX_META_KEYS)
                + ")",
                INDEX_META_KEYS,
            ).fetchall()
        )
        if set(rows) != set(INDEX_META_KEYS):
            raise ValueError("index metadata is incomplete")
        if (
            rows["parity_status"] != "trusted"
            or rows["index_schema_version"] != str(INDEX_SCHEMA_VERSION)
            or not locator_schema_is_current(connection)
        ):
            raise ValueError("index metadata is not trusted")
        expected = (
            int(rows["source_dev"]),
            int(rows["source_ino"]),
            int(rows["last_size"]),
            int(rows["source_mtime_ns"]),
            int(rows["source_ctime_ns"]),
        )
        if self._signature(source_stat) != expected:
            raise ValueError("source generation differs from SQLite metadata")
        if int(rows["indexed_id_count"]) < 0:
            raise ValueError("indexed ID count is invalid")
        digest = rows["indexed_ids_sha256"]
        if re.fullmatch(r"[0-9a-f]{64}", digest or "") is None:
            raise ValueError("indexed ID digest is invalid")
        database_stat = self._regular_file(self.database_path)
        sidecar_signatures = []
        for suffix in ("-wal", "-shm"):
            path = Path(str(self.database_path) + suffix)
            try:
                sidecar_signatures.append(self._signature(self._regular_file(path)))
            except FileNotFoundError:
                sidecar_signatures.append(None)
        cache_key = (
            self._signature(database_stat),
            tuple(sidecar_signatures),
            rows["indexed_id_count"],
            digest,
        )
        if self._verified_database_generation != cache_key:
            # The surrounding shared source/database locks make this cache
            # stable against cooperative index publishers. Main, WAL, and SHM
            # signatures still invalidate it after a published generation
            # changes; this is not a claim of protection from an attacker that
            # bypasses the repository lock contract and restores file metadata.
            actual_digest = hashlib.sha256()
            actual_count = 0
            for (rec_id,) in connection.execute(
                "SELECT rec_id FROM docs ORDER BY rec_id"
            ):
                encoded = str(rec_id).encode("utf-8")
                actual_digest.update(
                    len(encoded).to_bytes(8, byteorder="big", signed=False)
                )
                actual_digest.update(encoded)
                actual_count += 1
            if (
                actual_count != int(rows["indexed_id_count"])
                or actual_digest.hexdigest() != digest
            ):
                raise ValueError("actual indexed IDs differ from metadata")
            fts_count = int(
                connection.execute("SELECT count(*) FROM docs_fts").fetchone()[0]
            )
            if fts_count != actual_count:
                raise ValueError("FTS row count differs from docs")
            if connection.execute(
                "SELECT 1 FROM docs d LEFT JOIN docs_fts f ON f.rowid=d.rowid "
                "WHERE f.rowid IS NULL LIMIT 1"
            ).fetchone() is not None:
                raise ValueError("FTS is missing a docs row")
            if connection.execute(
                "SELECT 1 FROM docs_fts f LEFT JOIN docs d ON d.rowid=f.rowid "
                "WHERE d.rowid IS NULL LIMIT 1"
            ).fetchone() is not None:
                raise ValueError("FTS contains an unknown docs row")
            database_after = self._regular_file(self.database_path)
            if self._signature(database_stat) != self._signature(database_after):
                raise ValueError("database changed during integrity validation")
            self._verified_database_generation = cache_key
        coverage_rows = dict(
            connection.execute(
                "SELECT key,value FROM meta WHERE key IN (?,?,?)",
                (
                    "coverage_person",
                    "coverage_project",
                    "coverage_topic",
                ),
            ).fetchall()
        )
        for dimension in ("person", "project", "topic"):
            status_value = coverage_rows.get("coverage_" + dimension, "unknown")
            if status_value not in {"complete", "partial", "unknown"}:
                status_value = "unknown"
            rows["coverage_" + dimension] = status_value
        generation = hashlib.sha256(
            (
                "|".join(rows[key] for key in INDEX_META_KEYS)
                + "|"
                + "|".join(
                    rows["coverage_" + dimension]
                    for dimension in ("person", "project", "topic")
                )
            ).encode("utf-8")
        ).hexdigest()
        rows["generation"] = generation
        return rows

    @contextmanager
    def trusted_connection(self) -> Iterator[Tuple[sqlite3.Connection, Dict[str, str]]]:
        if self._forced_untrusted:
            raise ProductDataError("index_unavailable", "记忆索引当前不可用")
        connection = None
        try:
            with index_lock_pair(
                self.source_path,
                self.database_path,
                source_exclusive=False,
                database_exclusive=False,
            ):
                source_before = self._regular_file(self.source_path)
                self._regular_file(self.database_path)
                connection = self._connect()
                metadata = self._validate(connection, source_before)
                source_after = self._regular_file(self.source_path)
                if self._signature(source_before) != self._signature(source_after):
                    raise ValueError("source changed during index validation")
                yield connection, metadata
        except ProductDataError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise ProductDataError(
                "index_unavailable", "记忆索引当前不可用"
            ) from exc
        finally:
            if connection is not None:
                connection.close()


class ProductData:
    """Aggregate only bounded and redacted product-facing read models."""

    def __init__(
        self,
        vault_dir: Path,
        *,
        control_data: Optional[ControlData] = None,
        control_center: Optional[ControlCenter] = None,
        claim_store: Optional[ClaimStore] = None,
        living_self: Optional[LivingSelfService] = None,
        judgment_store: Optional[JudgmentStore] = None,
        context_store: Optional[ContextStore] = None,
        outcome_store: Optional[OutcomeStore] = None,
        index_integrity: Optional[ProductIndexIntegrity] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.vault_dir = Path(os.path.abspath(str(vault_dir)))
        self.control_data = control_data or ControlData(self.vault_dir)
        self.control_center = control_center or ControlCenter(self.vault_dir)
        self.claim_store = claim_store or ClaimStore(self.vault_dir)
        self.living_self = living_self or LivingSelfService(self.vault_dir)
        self.judgment_store = judgment_store or JudgmentStore(self.vault_dir)
        self.context_store = context_store or ContextStore(self.vault_dir)
        self.outcome_store = outcome_store or OutcomeStore(
            self.vault_dir,
            context_store=self.context_store,
        )
        self.index_integrity = index_integrity or ProductIndexIntegrity(
            self.vault_dir
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cursor_key = None

    def _signing_key(self) -> bytes:
        if self._cursor_key is None:
            self._cursor_key = self._load_cursor_key()
        return self._cursor_key

    def _load_cursor_key(self) -> bytes:
        key_path = self.vault_dir / "product" / "cursor-signing.key"
        try:
            with _exclusive_lock(
                self.vault_dir / "product" / "cursor-signing.lock",
                timeout=3.0,
                stale_after=30.0,
            ):
                raw = safe_read_text(key_path)
                if raw is None:
                    safe_atomic_write_text(key_path, secrets.token_hex(32) + "\n")
                    raw = safe_read_text(key_path)
                metadata = os.lstat(str(key_path))
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_mode & 0o777 != 0o600
                    or raw is None
                    or re.fullmatch(r"[0-9a-f]{64}\n?", raw) is None
                ):
                    raise ValueError("cursor key is unsafe")
                return bytes.fromhex(raw.strip())
        except ProductDataError:
            raise
        except (EventPathError, OSError, TypeError, ValueError) as exc:
            raise ProductDataError(
                "cursor_key_unavailable", "分页安全密钥当前不可用"
            ) from exc

    @staticmethod
    def _query_value(query: Mapping[str, Sequence[str]], key: str) -> str:
        values = query.get(key) or []
        if len(values) > 1:
            raise ProductDataError("invalid_query", "查询参数不能重复")
        return str(values[0]).strip() if values else ""

    def _query(
        self,
        query: Optional[Mapping[str, Sequence[str]]],
        *,
        allowed_filters: Sequence[str],
    ) -> Tuple[int, str, Dict[str, str]]:
        source = dict(query or {})
        allowed = set(allowed_filters) | {"limit", "cursor"}
        if set(source) - allowed or "offset" in source:
            raise ProductDataError("invalid_query", "查询参数不受支持")
        raw_limit = self._query_value(source, "limit") or str(DEFAULT_PAGE_SIZE)
        try:
            parsed_limit = int(raw_limit)
        except ValueError as exc:
            raise ProductDataError("invalid_query", "limit 必须是整数") from exc
        if parsed_limit < 1:
            raise ProductDataError("invalid_query", "limit 必须大于零")
        limit = min(MAX_PAGE_SIZE, parsed_limit)
        cursor = self._query_value(source, "cursor")
        if len(cursor) > 4096:
            raise ProductDataError("invalid_cursor", "分页游标无效")
        filters = {
            key: self._query_value(source, key)
            for key in allowed_filters
            if self._query_value(source, key)
        }
        if any(len(value) > 256 or "\x00" in value for value in filters.values()):
            raise ProductDataError("invalid_query", "查询值超过允许范围")
        return limit, cursor, filters

    def _encode_cursor(
        self,
        endpoint: str,
        filters: Mapping[str, str],
        generation: str,
        key: Sequence[Any],
    ) -> str:
        payload = json.dumps(
            {
                "e": endpoint,
                "f": hashlib.sha256(
                    _canonical_filters(filters).encode("utf-8")
                ).hexdigest(),
                "g": generation,
                "k": list(key),
                "v": CURSOR_SCHEMA_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(
            self._signing_key(), payload, hashlib.sha256
        ).digest()
        return _b64_encode(payload) + "." + _b64_encode(signature)

    def _decode_cursor(
        self,
        cursor: str,
        endpoint: str,
        filters: Mapping[str, str],
        generation: str,
        key_size: int,
    ) -> Optional[List[Any]]:
        if not cursor:
            return None
        if CURSOR_PATTERN.fullmatch(cursor) is None:
            raise ProductDataError("invalid_cursor", "分页游标无效")
        encoded, encoded_signature = cursor.split(".", 1)
        payload = _b64_decode(encoded)
        signature = _b64_decode(encoded_signature)
        expected = hmac.new(
            self._signing_key(), payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ProductDataError("invalid_cursor", "分页游标无效")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductDataError("invalid_cursor", "分页游标无效") from exc
        fingerprint = hashlib.sha256(
            _canonical_filters(filters).encode("utf-8")
        ).hexdigest()
        if (
            not isinstance(value, dict)
            or set(value) != {"e", "f", "g", "k", "v"}
            or value["v"] != CURSOR_SCHEMA_VERSION
            or value["e"] != endpoint
            or value["f"] != fingerprint
            or value["g"] != generation
            or not isinstance(value["k"], list)
            or len(value["k"]) != key_size
        ):
            raise ProductDataError("invalid_cursor", "分页游标无效")
        return list(value["k"])

    @staticmethod
    def _coverage(meta: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
        result = {}
        for dimension in ("person", "project", "topic"):
            status_value = meta.get("coverage_" + dimension, "unknown")
            result[dimension] = {
                "status": status_value,
                "complete": status_value == "complete",
            }
        return result

    def memories(
        self, query: Optional[Mapping[str, Sequence[str]]] = None
    ) -> Dict[str, Any]:
        limit, cursor, filters = self._query(
            query,
            allowed_filters=(
                "q",
                "source",
                "person",
                "project",
                "topic",
                "from",
                "to",
            ),
        )
        time_values = {}
        for field in ("from", "to"):
            if field not in filters:
                continue
            try:
                parsed = datetime.fromisoformat(
                    filters[field].replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ProductDataError(
                    "invalid_query", "时间筛选必须是 ISO-8601 时间"
                ) from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ProductDataError(
                    "invalid_query", "时间筛选必须包含时区"
                )
            normalized = parsed.astimezone(timezone.utc)
            time_values[field] = normalized
            filters[field] = normalize_timestamp_utc(filters[field])
        if (
            "from" in time_values
            and "to" in time_values
            and time_values["from"] > time_values["to"]
        ):
            raise ProductDataError("invalid_query", "开始时间不能晚于结束时间")
        text_terms = [
            filters[field]
            for field in ("q", "person", "topic")
            if field in filters
        ]
        if any(len(term) < 3 for term in text_terms):
            raise ProductDataError(
                "query_too_short", "文本筛选至少需要三个字符"
            )
        with self.index_integrity.trusted_connection() as (connection, meta):
            key = self._decode_cursor(
                cursor,
                "memories",
                filters,
                meta["generation"],
                2,
            )
            clauses = []
            params = []
            joins = ""
            for name, column, operator in (
                ("source", "d.source", "="),
                ("project", "d.project", "="),
                ("from", "d.ts_utc", ">="),
                ("to", "d.ts_utc", "<="),
            ):
                if filters.get(name):
                    clauses.append(column + operator + "?")
                    params.append(filters[name])
            if text_terms:
                joins = " JOIN docs_fts ON docs_fts.rowid=d.rowid"
                clauses.append("docs_fts MATCH ?")
                params.append(
                    " ".join(
                        '"' + term.replace('"', '""') + '"'
                        for term in text_terms
                    )
                )
            if key is not None:
                if not isinstance(key[0], str) or not isinstance(key[1], int):
                    raise ProductDataError("invalid_cursor", "分页游标无效")
                clauses.append("(d.ts_utc,d.rowid) < (?,?)")
                params.extend((key[0], key[1]))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            try:
                rows = connection.execute(
                    "SELECT d.rowid,d.rec_id,d.ts,d.ts_utc,d.source,d.role,d.project,d.content "
                    "FROM docs d"
                    + joins
                    + where
                    + " ORDER BY d.ts_utc DESC,d.rowid DESC LIMIT ?",
                    params + [limit + 1],
                ).fetchall()
            except sqlite3.Error as exc:
                raise ProductDataError(
                    "index_unavailable", "记忆索引当前不可用"
                ) from exc
            has_more = len(rows) > limit
            visible = rows[:limit]
            items = [
                {
                    "id": str(row[1] or ""),
                    "timestamp": str(row[2] or ""),
                    "source": _compact_text(row[4], 80),
                    "role": _compact_text(row[5], 40),
                    "project": _compact_text(row[6], 100),
                    "sensitivity": "internal",
                    "summary": _compact_text(row[7]),
                }
                for row in visible
            ]
            next_cursor = ""
            if has_more and visible:
                last = visible[-1]
                next_cursor = self._encode_cursor(
                    "memories",
                    filters,
                    meta["generation"],
                    (str(last[3] or ""), int(last[0])),
                )
        coverage = self._coverage(meta)
        requested_coverage = [coverage[key] for key in ("person", "project", "topic") if key in filters]
        return {
            "items": items,
            "limit": limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "coverage": coverage,
            "coverage_complete": all(row["complete"] for row in requested_coverage),
        }

    def memory_detail(self, memory_id: str) -> Dict[str, Any]:
        requested = _safe_identifier(memory_id, code="invalid_memory_id")
        with self.index_integrity.trusted_connection() as (connection, _meta):
            try:
                row = connection.execute(
                    "SELECT rec_id,ts,source,role,project,content "
                    "FROM docs WHERE rec_id=? ORDER BY rowid DESC LIMIT 1",
                    (requested,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise ProductDataError(
                    "index_unavailable", "记忆索引当前不可用"
                ) from exc
        if row is None:
            raise ProductDataError("memory_not_found", "没有找到这条记忆")
        return {
            "id": str(row[0] or ""),
            "timestamp": str(row[1] or ""),
            "source": _compact_text(row[2], 80),
            "role": _compact_text(row[3], 40),
            "project": _compact_text(row[4], 100),
            "sensitivity": "internal",
            "content": _compact_text(row[5], DETAIL_CHARS),
        }

    @staticmethod
    def _self_item_value(item: Mapping[str, Any], section: str) -> Dict[str, Any]:
        result = {
            "item_id": str(item.get("item_id") or ""),
            "section": section,
            "kind": _compact_text(item.get("kind"), 60),
            "title": _compact_text(item.get("title"), 160),
            "summary": _compact_text(item.get("summary"), 1200),
            "confidence": item.get("confidence"),
            "status": _compact_text(item.get("status"), 40),
            "scope": _safe_strings(
                item.get("domain_scope") or item.get("scope"), 80
            ),
            "evidence_ids": _safe_ids(item.get("evidence_ids")),
            "counter_evidence_ids": _safe_ids(item.get("counter_evidence_ids")),
            "claim_ids": _safe_ids(item.get("claim_ids")),
            "application": _safe_strings(item.get("application"), 400),
            "failure_conditions": _safe_strings(
                item.get("failure_conditions"), 400
            ),
        }
        return result

    def _current_self(self) -> Dict[str, Any]:
        try:
            value = self.living_self.current()
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError(
                "self_model_unavailable", "当前自我模型不可用"
            ) from exc
        if not isinstance(value, Mapping) or not isinstance(value.get("sections"), Mapping):
            raise ProductDataError("self_model_unavailable", "当前自我模型不可用")
        return dict(value)

    def self_model(self) -> Dict[str, Any]:
        current = self._current_self()
        sections = {}
        remaining = MAX_PAGE_SIZE
        for section in SELF_SECTIONS:
            rows = current["sections"].get(section)
            if not isinstance(rows, list):
                raise ProductDataError(
                    "self_model_unavailable", "当前自我模型不可用"
                )
            sections[section] = [
                self._self_item_value(row, section)
                for row in rows[:remaining]
                if isinstance(row, Mapping)
            ]
            remaining -= len(sections[section])
        return {
            "version_id": str(current.get("version_id") or ""),
            "status": _compact_text(current.get("status"), 40),
            "based_on_claim_seq": current.get("based_on_claim_seq"),
            "generated_at": str(current.get("generated_at") or ""),
            "confirmed_at": str(current.get("confirmed_at") or ""),
            "sections": sections,
        }

    def self_item(self, item_id: str) -> Dict[str, Any]:
        requested = _safe_identifier(item_id, code="invalid_self_item_id")
        model = self.self_model()
        for section in SELF_SECTIONS:
            for item in model["sections"][section]:
                if item["item_id"] == requested:
                    return dict(item)
        raise ProductDataError("self_item_not_found", "没有找到这条理解")

    @staticmethod
    def _version_summary(row: Mapping[str, Any]) -> Dict[str, Any]:
        sections = row.get("sections") if isinstance(row.get("sections"), Mapping) else {}
        return {
            "version_id": str(row.get("version_id") or ""),
            "parent_version_id": row.get("parent_version_id"),
            "status": _compact_text(row.get("status"), 40),
            "generation_reason": _compact_text(row.get("generation_reason"), 80),
            "based_on_claim_seq": row.get("based_on_claim_seq"),
            "generated_at": str(row.get("generated_at") or ""),
            "confirmed_at": str(row.get("confirmed_at") or ""),
            "item_count": sum(len(value) for value in sections.values() if isinstance(value, list)),
        }

    def self_versions(
        self, query: Optional[Mapping[str, Sequence[str]]] = None
    ) -> Dict[str, Any]:
        try:
            rows = _safe_sequence(self.living_self.versions())
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError(
                "self_model_unavailable", "自我模型版本不可用"
            ) from exc
        return self._model_page(
            rows,
            query,
            endpoint="self_versions",
            allowed_filters=(),
            time_field="confirmed_at",
            id_field="version_id",
            transform=self._version_summary,
        )

    def self_diff(self, from_version_id: str, to_version_id: str) -> Dict[str, Any]:
        before = _safe_identifier(from_version_id, code="invalid_version_id")
        after = _safe_identifier(to_version_id, code="invalid_version_id")
        try:
            value = self.living_self.diff(before, after)
        except FileNotFoundError as exc:
            raise ProductDataError("self_version_not_found", "没有找到该版本") from exc
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError(
                "self_model_unavailable", "自我模型版本不可用"
            ) from exc
        result = {"added": [], "changed": [], "removed": []}
        if not isinstance(value, Mapping):
            raise ProductDataError("self_model_unavailable", "自我模型版本不可用")
        for group in result:
            rows = value.get(group)
            if not isinstance(rows, list):
                raise ProductDataError("self_model_unavailable", "自我模型版本不可用")
            for row in rows[:MAX_PAGE_SIZE]:
                if not isinstance(row, Mapping):
                    continue
                safe = {
                    "item_id": str(row.get("item_id") or ""),
                    "section": _compact_text(
                        row.get("section") or row.get("to_section") or row.get("from_section"),
                        80,
                    ),
                }
                candidate = row.get("item") or row.get("after") or row.get("before")
                if isinstance(candidate, Mapping):
                    safe["item"] = self._self_item_value(
                        candidate, safe["section"]
                    )
                result[group].append(safe)
        return result

    @staticmethod
    def _judgment_summary(row: Mapping[str, Any]) -> Dict[str, Any]:
        outcome = row.get("outcome") if isinstance(row.get("outcome"), Mapping) else {}
        return {
            "card_id": str(row.get("card_id") or ""),
            "title": _compact_text(row.get("title"), 240),
            "status": _compact_text(row.get("status"), 40),
            "outcome_status": _compact_text(outcome.get("status"), 40),
            "updated_at": str(row.get("updated_at") or ""),
            "revision": row.get("revision"),
            "evidence_count": len(row.get("evidence_ids") or []),
            "claim_count": len(row.get("claim_ids") or []),
        }

    def _judgment_rows(self) -> List[Dict[str, Any]]:
        try:
            return _safe_sequence(self.judgment_store.list())
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError(
                "judgment_unavailable", "判断卡当前不可用"
            ) from exc

    def judgments(
        self, query: Optional[Mapping[str, Sequence[str]]] = None
    ) -> Dict[str, Any]:
        return self._model_page(
            self._judgment_rows(),
            query,
            endpoint="judgments",
            allowed_filters=("status",),
            time_field="updated_at",
            id_field="card_id",
            transform=self._judgment_summary,
        )

    def judgment_detail(self, card_id: str) -> Dict[str, Any]:
        requested = _safe_identifier(card_id, code="invalid_judgment_id")
        try:
            value = self.judgment_store.get(requested)
        except (KeyError, FileNotFoundError) as exc:
            raise ProductDataError("judgment_not_found", "没有找到这张判断卡") from exc
        except ProductDataError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "judgment_unavailable")
            if code in {"judgment_not_found", "card_id_required"}:
                raise ProductDataError("judgment_not_found", "没有找到这张判断卡") from exc
            raise ProductDataError("judgment_unavailable", "判断卡当前不可用") from exc
        if not isinstance(value, Mapping):
            raise ProductDataError("judgment_unavailable", "判断卡当前不可用")
        outcome = value.get("outcome") if isinstance(value.get("outcome"), Mapping) else {}
        return {
            "card_id": str(value.get("card_id") or ""),
            "title": _compact_text(value.get("title"), 240),
            "situation": _compact_text(value.get("situation"), 1600),
            "goal": _compact_text(value.get("goal"), 800),
            "constraints": _safe_strings(value.get("constraints"), 600),
            "signals": _safe_strings(value.get("signals"), 600),
            "decision": _compact_text(value.get("decision"), 1600),
            "alternatives": _safe_strings(value.get("alternatives"), 600),
            "outcome": {
                "status": _compact_text(outcome.get("status"), 40),
                "summary": _compact_text(outcome.get("summary"), 1200),
                "observed_at": outcome.get("observed_at"),
            },
            "lesson": _compact_text(value.get("lesson"), 1200),
            "next_trigger": _compact_text(value.get("next_trigger"), 800),
            "status": _compact_text(value.get("status"), 40),
            "evidence_ids": _safe_ids(value.get("evidence_ids")),
            "claim_ids": _safe_ids(value.get("claim_ids")),
            "privacy": _compact_text(value.get("privacy"), 40),
            "created_at": str(value.get("created_at") or ""),
            "updated_at": str(value.get("updated_at") or ""),
            "revision": value.get("revision"),
        }

    @staticmethod
    def _context_summary(row: Mapping[str, Any]) -> Dict[str, Any]:
        privacy = row.get("privacy_policy") if isinstance(row.get("privacy_policy"), Mapping) else {}
        return {
            "context_id": str(row.get("context_id") or ""),
            "preview_id": str(row.get("preview_id") or ""),
            "task": _compact_text(row.get("task"), 240),
            "mode": _compact_text(row.get("mode"), 40),
            "lifecycle_status": _compact_text(row.get("lifecycle_status"), 40),
            "availability_status": _compact_text(row.get("availability_status"), 40),
            "generated_at": str(row.get("generated_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "consumed_at": row.get("consumed_at"),
            "outcome_recorded_at": row.get("outcome_recorded_at"),
            "outcome_id": row.get("outcome_id"),
            "privacy_excluded_count": int(privacy.get("excluded_count") or 0),
            "revision": row.get("revision"),
        }

    def _context_rows(self) -> List[Dict[str, Any]]:
        try:
            return _safe_sequence(self.context_store.list())
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError(
                "context_unavailable", "任务上下文当前不可用"
            ) from exc

    def contexts(
        self, query: Optional[Mapping[str, Sequence[str]]] = None
    ) -> Dict[str, Any]:
        return self._model_page(
            self._context_rows(),
            query,
            endpoint="contexts",
            allowed_filters=("status", "mode"),
            time_field="updated_at",
            id_field="context_id",
            transform=self._context_summary,
            filter_alias={"status": "lifecycle_status"},
        )

    @staticmethod
    def _outcome_value(value: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "outcome_id": str(value.get("outcome_id") or ""),
            "context_id": str(value.get("context_id") or ""),
            "adopted": _compact_text(value.get("adopted"), 40),
            "result": _compact_text(value.get("result"), 40),
            "summary": _compact_text(value.get("summary"), 1200),
            "confirmed_refs": [
                {
                    "kind": _compact_text(row.get("kind"), 40),
                    "id": str(row.get("id") or ""),
                    "revision": row.get("revision"),
                }
                for row in (value.get("confirmed_refs") or [])[:MAX_PAGE_SIZE]
                if isinstance(row, Mapping)
            ],
            "challenged_refs": [
                {
                    "kind": _compact_text(row.get("kind"), 40),
                    "id": str(row.get("id") or ""),
                    "revision": row.get("revision"),
                }
                for row in (value.get("challenged_refs") or [])[:MAX_PAGE_SIZE]
                if isinstance(row, Mapping)
            ],
            "created_at": str(value.get("created_at") or ""),
        }

    def context_detail(self, context_id: str) -> Dict[str, Any]:
        requested = _safe_identifier(context_id, code="invalid_context_id")
        try:
            value = self.context_store.get(requested)
        except (KeyError, FileNotFoundError) as exc:
            raise ProductDataError("context_not_found", "没有找到该任务上下文") from exc
        except ProductDataError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "context_unavailable")
            if code == "context_not_found":
                raise ProductDataError("context_not_found", "没有找到该任务上下文") from exc
            raise ProductDataError("context_unavailable", "任务上下文当前不可用") from exc
        if not isinstance(value, Mapping):
            raise ProductDataError("context_unavailable", "任务上下文当前不可用")
        result = self._context_summary(value)
        selection = value.get("selection") if isinstance(value.get("selection"), Mapping) else {}
        privacy = value.get("privacy_policy") if isinstance(value.get("privacy_policy"), Mapping) else {}
        revision = value.get("source_revision") if isinstance(value.get("source_revision"), Mapping) else {}
        result.update(
            {
                "selected_item_ids": _safe_ids(selection.get("selected_item_ids")),
                "excluded_item_ids": _safe_ids(selection.get("excluded_item_ids")),
                "privacy": {
                    "excluded_count": int(privacy.get("excluded_count") or 0),
                    "reasons": _safe_strings(privacy.get("reasons"), 160),
                },
                "source_revision": {
                    "claims_event_seq": revision.get("claims_event_seq"),
                    "living_self_version": revision.get("living_self_version"),
                    "judgments_event_seq": revision.get("judgments_event_seq"),
                    "compiler_version": revision.get("compiler_version"),
                    "policy_version": revision.get("policy_version"),
                },
            }
        )
        if result.get("outcome_id") and result.get("context_id"):
            try:
                outcome = self.outcome_store.get(result["context_id"])
            except (KeyError, FileNotFoundError):
                outcome = None
            except ProductDataError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", "outcome_unavailable")
                if code not in {"outcome_not_found", "outcome_uncommitted"}:
                    raise ProductDataError(
                        "outcome_unavailable", "任务结果当前不可用"
                    ) from exc
                outcome = None
            if isinstance(outcome, Mapping):
                result["outcome"] = self._outcome_value(outcome)
        return result

    def _model_page(
        self,
        rows: List[Dict[str, Any]],
        query: Optional[Mapping[str, Sequence[str]]],
        *,
        endpoint: str,
        allowed_filters: Sequence[str],
        time_field: str,
        id_field: str,
        transform: Callable[[Mapping[str, Any]], Dict[str, Any]],
        filter_alias: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        limit, cursor, filters = self._query(
            query, allowed_filters=allowed_filters
        )
        aliases = dict(filter_alias or {})
        filtered = []
        for row in rows:
            if any(
                str(row.get(aliases.get(key, key)) or "") != value
                for key, value in filters.items()
            ):
                continue
            filtered.append(row)
        filtered.sort(
            key=lambda row: (
                str(row.get(time_field) or ""),
                str(row.get(id_field) or ""),
            ),
            reverse=True,
        )
        canonical_rows = [
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            for row in rows
        ]
        generation_digest = hashlib.sha256()
        for encoded_row in sorted(canonical_rows):
            encoded = encoded_row.encode("utf-8")
            generation_digest.update(
                len(encoded).to_bytes(8, byteorder="big", signed=False)
            )
            generation_digest.update(encoded)
        generation = generation_digest.hexdigest()
        key = self._decode_cursor(
            cursor, endpoint, filters, generation, 2
        )
        if key is not None:
            if not all(isinstance(value, str) for value in key):
                raise ProductDataError("invalid_cursor", "分页游标无效")
            filtered = [
                row
                for row in filtered
                if (
                    str(row.get(time_field) or ""),
                    str(row.get(id_field) or ""),
                )
                < (key[0], key[1])
            ]
        visible = filtered[: limit + 1]
        has_more = len(visible) > limit
        visible = visible[:limit]
        next_cursor = ""
        if has_more and visible:
            last = visible[-1]
            next_cursor = self._encode_cursor(
                endpoint,
                filters,
                generation,
                (
                    str(last.get(time_field) or ""),
                    str(last.get(id_field) or ""),
                ),
            )
        return {
            "items": [transform(row) for row in visible],
            "limit": limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    def _claims(self) -> List[Dict[str, Any]]:
        try:
            return _safe_sequence(self.claim_store.list())
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError("trust_unavailable", "信任信息当前不可用") from exc

    def _outcomes(self) -> List[Dict[str, Any]]:
        try:
            return _safe_sequence(self.outcome_store.list())
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError("outcome_unavailable", "任务结果当前不可用") from exc

    def home(self) -> Dict[str, Any]:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ProductDataError("clock_unavailable", "系统时间不可用")
        local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_boundary = local_midnight.astimezone(timezone.utc).isoformat()
        utc_now = now.astimezone(timezone.utc).isoformat()
        remembered = self.memories(
            {
                "from": [utc_boundary],
                "to": [utc_now],
                "limit": ["8"],
            }
        )["items"]
        versions = self.self_versions({"limit": ["8"]})["items"]
        if not versions:
            changes = {
                "kind": "none",
                "from_version_id": None,
                "to_version_id": None,
                "counts": {"added": 0, "changed": 0, "removed": 0},
                "added": [],
                "changed": [],
                "removed": [],
            }
        else:
            latest = versions[0]
            parent_id = latest.get("parent_version_id")
            if parent_id:
                diff = self.self_diff(str(parent_id), str(latest["version_id"]))
                changes = {
                    "kind": "diff",
                    "from_version_id": parent_id,
                    "to_version_id": latest["version_id"],
                    "counts": {
                        key: len(diff[key])
                        for key in ("added", "changed", "removed")
                    },
                    **diff,
                }
            else:
                changes = {
                    "kind": "initial",
                    "from_version_id": None,
                    "to_version_id": latest["version_id"],
                    "counts": {"added": 0, "changed": 0, "removed": 0},
                    "added": [],
                    "changed": [],
                    "removed": [],
                }
        claims = self._claims()
        judgments = self._judgment_rows()
        confirmations = [
            {
                "kind": "claim",
                "id": str(row.get("claim_id") or ""),
                "summary": _compact_text(row.get("statement"), 240),
                "status": _compact_text(row.get("status"), 40),
            }
            for row in claims
            if row.get("status") == "candidate"
        ]
        confirmations.extend(
            {
                "kind": "judgment",
                "id": str(row.get("card_id") or ""),
                "summary": _compact_text(row.get("title"), 240),
                "status": _compact_text(row.get("status"), 40),
            }
            for row in judgments
            if row.get("status") == "candidate"
        )
        used_contexts = [
            row
            for row in self._context_rows()
            if row.get("lifecycle_status") in {"consumed", "outcome_recorded"}
            and row.get("consumed_at")
        ]
        used_contexts.sort(
            key=lambda row: (
                str(row.get("consumed_at") or ""),
                str(row.get("context_id") or ""),
            ),
            reverse=True,
        )
        latest_context = (
            self._context_summary(used_contexts[0]) if used_contexts else None
        )
        outcomes = sorted(
            self._outcomes(),
            key=lambda row: (
                str(row.get("created_at") or ""),
                str(row.get("outcome_id") or ""),
            ),
            reverse=True,
        )
        latest_outcome = self._outcome_value(outcomes[0]) if outcomes else None
        try:
            snapshot = self.control_center.build_snapshot()
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError(
                "system_unavailable", "系统健康信息当前不可用"
            ) from exc
        return {
            "remembered_today": remembered,
            "understanding_changes": changes,
            "needs_confirmation": confirmations[:8],
            "latest_context_use": latest_context,
            "latest_outcome": latest_outcome,
            "system_health": {
                "status": str(snapshot.get("status") or "unknown"),
                "status_label": _compact_text(snapshot.get("status_label"), 80),
                "version": _compact_text(snapshot.get("version"), 40),
                "attention_count": len(snapshot.get("attention") or []),
            },
        }

    def trust(self) -> Dict[str, Any]:
        claims = self._claims()
        judgments = self._judgment_rows()
        contexts = self._context_rows()
        category_coverage = {
            "unknown_speaker": "complete",
            "other_view_candidate": "complete",
            "missing_evidence": "complete",
            "low_confidence": "complete",
            "expired_model": "complete",
            "conflict": "complete",
            "source_broken": "unknown",
            "privacy_exclusion": "complete",
            "recent_correction": "partial",
            "model_evaluation": "partial",
        }
        category_items = {key: {} for key in category_coverage}

        def add(kind: str, item_id: str, summary: str, severity: str) -> None:
            normalized_id = str(item_id or "")
            if not normalized_id or normalized_id in category_items[kind]:
                return
            category_items[kind][normalized_id] = {
                "kind": kind,
                "id": normalized_id,
                "summary": _compact_text(summary, 240),
                "severity": severity,
            }

        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ProductDataError("clock_unavailable", "系统时间不可用")
        now_utc = now.astimezone(timezone.utc)
        for row in claims:
            claim_id = str(row.get("claim_id") or "")
            speaker = row.get("speaker") if isinstance(row.get("speaker"), Mapping) else {}
            if speaker.get("kind") == "unknown":
                add("unknown_speaker", claim_id, "说话人尚未确认", "attention")
            if (
                speaker.get("kind") == "other"
                and (
                    row.get("status") == "candidate"
                    or row.get("claim_type") == "external_view"
                )
            ):
                add(
                    "other_view_candidate",
                    claim_id,
                    "他人观点仍处于候选状态",
                    "attention",
                )
            if row.get("status") == "candidate":
                add(
                    "missing_evidence" if not row.get("evidence_ids") else "low_confidence",
                    claim_id,
                    row.get("statement") or "候选主张待确认",
                    "attention",
                )
            confidence = row.get("confidence")
            if isinstance(confidence, (int, float)) and confidence < 0.6:
                add("low_confidence", claim_id, "证据置信度偏低", "attention")
            if not row.get("evidence_ids"):
                add("missing_evidence", claim_id, "尚无可核验支持证据", "attention")
            valid_to = row.get("valid_to")
            if isinstance(valid_to, str) and valid_to:
                try:
                    expires = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ProductDataError(
                        "trust_unavailable", "信任信息当前不可用"
                    ) from exc
                if expires.tzinfo is None or expires.utcoffset() is None:
                    raise ProductDataError(
                        "trust_unavailable", "信任信息当前不可用"
                    )
                if expires.astimezone(timezone.utc) <= now_utc:
                    add("expired_model", claim_id, "该理解已经过期", "attention")
            if row.get("counter_evidence_ids"):
                add("conflict", claim_id, "存在反例或冲突证据", "attention")
            if row.get("status") in {"superseded", "rejected"}:
                add("recent_correction", claim_id, "该理解最近被纠正或替换", "info")
        for row in contexts:
            privacy = row.get("privacy_policy") if isinstance(row.get("privacy_policy"), Mapping) else {}
            count = int(privacy.get("excluded_count") or 0)
            if count:
                add(
                    "privacy_exclusion",
                    str(row.get("context_id") or row.get("preview_id") or ""),
                    "上下文因隐私策略排除 %d 项" % count,
                    "info",
                )
        try:
            current_self = self.self_model()
        except ProductDataError:
            current_self = None
            category_coverage["model_evaluation"] = "unknown"
        if current_self is not None:
            category_coverage["model_evaluation"] = "complete"
            for section in SELF_SECTIONS:
                for item in current_self["sections"][section]:
                    add(
                        "model_evaluation",
                        str(item.get("item_id") or ""),
                        "模型状态 %s，支持证据 %d 条，反例 %d 条"
                        % (
                            item.get("status") or "unknown",
                            len(item.get("evidence_ids") or []),
                            len(item.get("counter_evidence_ids") or []),
                        ),
                        "info",
                    )
        candidate_judgments = sum(
            1 for row in judgments if row.get("status") == "candidate"
        )
        candidate_claims = sum(
            1 for row in claims if row.get("status") == "candidate"
        )
        visible_by_category = {key: [] for key in category_coverage}
        pending = {
            key: list(category_items[key].values())
            for key in category_coverage
        }
        flat_items = []
        while len(flat_items) < MAX_PAGE_SIZE:
            made_progress = False
            for key in category_coverage:
                if len(flat_items) >= MAX_PAGE_SIZE:
                    break
                rows = pending[key]
                visible_count = len(visible_by_category[key])
                if visible_count >= len(rows):
                    continue
                item = rows[visible_count]
                visible_by_category[key].append(item)
                flat_items.append(item)
                made_progress = True
            if not made_progress:
                break
        categories = {}
        for key in category_coverage:
            total = len(category_items[key])
            visible = visible_by_category[key]
            categories[key] = {
                "count": total,
                "coverage": category_coverage[key],
                "items": visible,
                "truncated": total > len(visible),
            }
        return {
            "summary": {
                "needs_confirmation": candidate_claims + candidate_judgments,
                "low_confidence": categories["low_confidence"]["count"],
                "privacy_exclusions": categories["privacy_exclusion"]["count"],
            },
            "categories": categories,
            "items": flat_items,
        }

    def system(self) -> Dict[str, Any]:
        try:
            value = {
                "health": self.control_center.build_snapshot(),
                "capabilities": self.control_data.capabilities(),
                "sources": self.control_data.sources(),
                "backups": self.control_data.backups(),
                "diagnostics": self.control_data.diagnostics(),
            }
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError(
                "system_unavailable", "系统信息当前不可用"
            ) from exc
        return _safe_product_tree(value)
