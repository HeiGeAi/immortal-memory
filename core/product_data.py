#!/usr/bin/env python3
"""Bounded, privacy-safe read models for the Immortal product API."""

import base64
import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from claim_store import CLAIM_EVENT_TYPES, ClaimStore
from context_compiler import ContextCompiler
from context_store import ContextStore
from control_center import ControlCenter
from control_data import ControlData
from evidence_catalog import EvidenceCatalog, EvidenceCatalogError
from event_store import (
    EventPathError,
    _anchored_parent,
    _directory_flags,
    _exclusive_lock,
    _regular_stat_at,
    safe_atomic_write_text,
    safe_read_text,
)
from index_integrity import (
    INDEX_SCHEMA_VERSION,
    IndexIntegrityError,
    locator_schema_is_current,
    normalize_timestamp_utc,
)
from index_locks import index_lock_pair
from judgment_store import JudgmentStore
from living_self_service import LivingSelfService
from model_types import (
    CLAIM_STATUSES,
    CLAIM_TYPES,
    DOMAIN_SCOPES,
    PRIVACY_LEVELS,
    ROLE_SCOPES,
    SOURCE_KINDS,
)
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
INDEX_VERIFICATION_VERSION = 1
MEMORY_VALUE_COHORTS = (
    "pending_review",
    "never_acknowledged",
    "frequently_helpful",
    "frequently_challenged",
    "high_confidence_unused",
    "expiring",
    "has_counter_evidence",
    "missing_evidence",
    "privacy_sensitive",
    "recently_changed",
)
MEMORY_VALUE_AUTHORITY_EVENT_LIMIT = 5_000
MEMORY_VALUE_REFERENCE_LIMIT = 10_000
MEMORY_VALUE_RECENT_DAYS = 30
MEMORY_VALUE_UNUSED_DAYS = 30
MEMORY_VALUE_EXPIRING_DAYS = 30
MEMORY_VALUE_FREQUENT_ACKS = 2
CLAIM_HISTORY_INDEX_SCHEMA_VERSION = 2
CLAIM_HISTORY_PRIVATE_REASON = "私密记忆历史（原因已隐藏）"
DARWIN_ACL_TYPE_EXTENDED = 0x00000100
HOME_SECTION_ERROR_CODES = frozenset(
    {
        "clock_unavailable",
        "context_unavailable",
        "index_unavailable",
        "internal_error",
        "judgment_unavailable",
        "outcome_unavailable",
        "self_model_unavailable",
        "system_unavailable",
        "trust_unavailable",
    }
)
HOME_SECTION_DATA_ERRORS = (
    AttributeError,
    KeyError,
    OverflowError,
    TypeError,
    ValueError,
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


def _all_safe_ids(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [
        value
        for value in values
        if isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None
    ]


def _fd_has_extended_acl(descriptor: int) -> bool:
    if sys.platform == "darwin":
        library = ctypes.CDLL(None, use_errno=True)
        try:
            get_acl = library.acl_get_fd_np
            free_acl = library.acl_free
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "extended ACL probe unavailable") from exc
        get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
        get_acl.restype = ctypes.c_void_p
        free_acl.argtypes = (ctypes.c_void_p,)
        free_acl.restype = ctypes.c_int
        ctypes.set_errno(0)
        acl = get_acl(descriptor, DARWIN_ACL_TYPE_EXTENDED)
        if acl:
            free_acl(acl)
            return True
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return False
        raise OSError(error or errno.EIO, "extended ACL probe failed")
    if hasattr(os, "listxattr"):
        return any("acl" in str(name).lower() for name in os.listxattr(descriptor))
    raise OSError(errno.ENOTSUP, "extended ACL probe unavailable")


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
        uri = self.database_path.resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _database_signature(self) -> Tuple[Any, ...]:
        wal_path = Path(str(self.database_path) + "-wal")
        try:
            wal = self._regular_file(wal_path)
        except FileNotFoundError:
            pass
        else:
            if wal.st_size:
                raise ValueError("search index has a non-empty WAL")
        return (self._signature(self._regular_file(self.database_path)),)

    def _verification_identity(
        self,
        rows: Mapping[str, str],
        source_stat: os.stat_result,
        database_signature: Tuple[Any, ...],
    ) -> Dict[str, Any]:
        metadata = json.dumps(
            {key: rows[key] for key in INDEX_META_KEYS},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return {
            "validation_version": INDEX_VERIFICATION_VERSION,
            "schema_version": INDEX_SCHEMA_VERSION,
            "source_signature": list(self._signature(source_stat)),
            "database_signature": [
                list(value) if value is not None else None
                for value in database_signature
            ],
            "metadata_generation": hashlib.sha256(metadata).hexdigest(),
        }

    def _read_verification_receipt(self, path: Path) -> Optional[Dict[str, Any]]:
        raw = safe_read_text(path)
        if raw is None:
            return None
        metadata = os.lstat(str(path))
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o777 != 0o600
        ):
            raise ValueError("index verification receipt is unsafe")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("index verification receipt is corrupt") from exc
        if not isinstance(value, dict):
            raise ValueError("index verification receipt is corrupt")
        return value

    def _deep_validate_index(
        self,
        connection: sqlite3.Connection,
        rows: Mapping[str, str],
    ) -> None:
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
            or actual_digest.hexdigest() != rows["indexed_ids_sha256"]
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
        if connection.execute(
            "SELECT 1 FROM docs d JOIN docs_fts f ON f.rowid=d.rowid "
            "WHERE d.content IS NOT f.content LIMIT 1"
        ).fetchone() is not None:
            raise ValueError("FTS content differs from docs")

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
        database_signature = self._database_signature()
        identity = self._verification_identity(
            rows,
            source_stat,
            database_signature,
        )
        cache_key = json.dumps(
            identity, sort_keys=True, separators=(",", ":")
        )
        # The surrounding shared source/database locks make the identity
        # stable against cooperative index publishers. Immutable reads reject
        # a non-empty WAL and bind the receipt to the main database identity;
        # mutable SHM bookkeeping is deliberately not a generation identity.
        # This is not a claim of protection from an attacker that bypasses the
        # repository lock contract and restores file metadata.
        # The receipt itself is re-read on every access so a long-running
        # process cannot hide later corruption behind its in-memory cache.
        receipt_path = self.vault_dir / "product" / "index-verification.json"
        with _exclusive_lock(
            self.vault_dir / "product" / "index-verification.lock",
            timeout=30.0,
            stale_after=60.0,
        ):
            receipt = self._read_verification_receipt(receipt_path)
            if (
                receipt is None
                and self._verified_database_generation == cache_key
            ):
                raise ValueError("index verification receipt disappeared")
            if receipt != identity:
                self._deep_validate_index(connection, rows)
                if self._database_signature() != database_signature:
                    raise ValueError(
                        "database changed during integrity validation"
                    )
                safe_atomic_write_text(
                    receipt_path,
                    json.dumps(
                        identity,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                )
                if self._read_verification_receipt(receipt_path) != identity:
                    raise ValueError("index verification receipt write failed")
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
        except (
            EventPathError,
            OSError,
            sqlite3.Error,
            TimeoutError,
            TypeError,
            ValueError,
        ) as exc:
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
        context_compiler: Optional[Any] = None,
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
        self._context_compiler = context_compiler
        self.outcome_store = outcome_store or OutcomeStore(
            self.vault_dir,
            context_store=self.context_store,
        )
        self.index_integrity = index_integrity or ProductIndexIntegrity(
            self.vault_dir
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cursor_key = None
        self._claim_history_cache_identity = None

    @property
    def context_compiler(self) -> Any:
        if self._context_compiler is None:
            self._context_compiler = ContextCompiler(
                self.vault_dir,
                claims=self.claim_store,
                living_self=self.living_self,
                judgments=self.judgment_store,
                context_store=self.context_store,
                clock=self._clock,
            )
        return self._context_compiler

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
    def _claim_allowed_actions(status_value: Any) -> List[str]:
        return {
            "candidate": ["confirm", "reject"],
            "confirmed": ["correct"],
            "rejected": ["reconsider"],
            "superseded": [],
        }.get(str(status_value or ""), [])

    @classmethod
    def _claim_item(cls, claim: Mapping[str, Any]) -> Dict[str, Any]:
        private = claim.get("privacy") == "private"
        if private:
            return {
                "claim_id": str(claim.get("claim_id") or ""),
                "revision": claim.get("revision"),
                "statement": "私密记忆（正文已隐藏）",
                "masked": True,
                "status": _compact_text(claim.get("status"), 40),
                "privacy": "private",
                "updated_at": str(claim.get("updated_at") or ""),
                "allowed_actions": cls._claim_allowed_actions(
                    claim.get("status")
                ),
            }
        subject = claim.get("subject") if isinstance(claim.get("subject"), Mapping) else {}
        speaker = claim.get("speaker") if isinstance(claim.get("speaker"), Mapping) else {}
        confidence_basis = (
            claim.get("confidence_basis")
            if isinstance(claim.get("confidence_basis"), Mapping)
            else {}
        )
        return {
            "claim_id": str(claim.get("claim_id") or ""),
            "revision": claim.get("revision"),
            "statement": _compact_text(claim.get("statement"), 500),
            "masked": False,
            "status": _compact_text(claim.get("status"), 40),
            "claim_type": _compact_text(claim.get("claim_type"), 40),
            "source_kind": _compact_text(claim.get("source_kind"), 40),
            "subject_kind": _compact_text(subject.get("kind"), 40),
            "speaker_kind": _compact_text(speaker.get("kind"), 40),
            "confidence": claim.get("confidence"),
            "confidence_explanation": _compact_text(
                confidence_basis.get("explanation"), 240
            ),
            "privacy": _compact_text(claim.get("privacy"), 40),
            "role_scope": _safe_strings(claim.get("role_scope"), 40),
            "domain_scope": _safe_strings(claim.get("domain_scope"), 40),
            "evidence_count": len(_all_safe_ids(claim.get("evidence_ids"))),
            "counter_evidence_count": len(
                _all_safe_ids(claim.get("counter_evidence_ids"))
            ),
            "valid_from": claim.get("valid_from"),
            "valid_to": claim.get("valid_to"),
            "created_at": str(claim.get("created_at") or ""),
            "updated_at": str(claim.get("updated_at") or ""),
            "allowed_actions": cls._claim_allowed_actions(claim.get("status")),
        }

    @staticmethod
    def _claim_generation(rows: Sequence[Mapping[str, Any]]) -> str:
        digest = hashlib.sha256()
        encoded_rows = sorted(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            for row in rows
        )
        for encoded in encoded_rows:
            digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
            digest.update(encoded)
        return digest.hexdigest()

    def claims(
        self, query: Optional[Mapping[str, Sequence[str]]] = None
    ) -> Dict[str, Any]:
        limit, cursor, filters = self._query(
            query,
            allowed_filters=(
                "status",
                "claim_type",
                "source_kind",
                "privacy",
                "role_scope",
                "domain_scope",
                "q",
            ),
        )
        allowed_values = {
            "status": CLAIM_STATUSES,
            "claim_type": CLAIM_TYPES,
            "source_kind": SOURCE_KINDS,
            "privacy": PRIVACY_LEVELS,
            "role_scope": ROLE_SCOPES,
            "domain_scope": DOMAIN_SCOPES,
        }
        for field, allowed in allowed_values.items():
            if field in filters and filters[field] not in allowed:
                raise ProductDataError("invalid_query", "Claim 筛选值无效")
        query_text = filters.get("q", "")
        if query_text and len(query_text) < 2:
            raise ProductDataError("query_too_short", "Claim 搜索至少需要两个字符")
        rows = self._claims()
        status_counts = {
            status_value: sum(row.get("status") == status_value for row in rows)
            for status_value in sorted(CLAIM_STATUSES)
        }
        filtered = []
        for row in rows:
            private = row.get("privacy") == "private"
            if private and any(
                field in filters
                for field in (
                    "q",
                    "claim_type",
                    "source_kind",
                    "role_scope",
                    "domain_scope",
                )
            ):
                continue
            if any(
                (
                    filters[field] not in row.get(field, [])
                    if field in {"role_scope", "domain_scope"}
                    else str(row.get(field) or "") != filters[field]
                )
                for field in allowed_values
                if field in filters
            ):
                continue
            if query_text and query_text.casefold() not in str(
                row.get("statement") or ""
            ).casefold():
                continue
            filtered.append(row)
        try:
            keyed = [
                (
                    normalize_timestamp_utc(row.get("updated_at")),
                    str(row.get("claim_id") or ""),
                    row,
                )
                for row in filtered
            ]
        except IndexIntegrityError as exc:
            raise ProductDataError(
                "claim_unavailable", "记忆理解的时间信息当前不可用"
            ) from exc
        keyed.sort(key=lambda value: (value[0], value[1]), reverse=True)
        generation = self._claim_generation(rows)
        key = self._decode_cursor(cursor, "claims", filters, generation, 2)
        if key is not None:
            if not all(isinstance(value, str) for value in key):
                raise ProductDataError("invalid_cursor", "分页游标无效")
            keyed = [
                value for value in keyed if (value[0], value[1]) < (key[0], key[1])
            ]
        visible = keyed[: limit + 1]
        has_more = len(visible) > limit
        visible = visible[:limit]
        next_cursor = ""
        if has_more and visible:
            last = visible[-1]
            next_cursor = self._encode_cursor(
                "claims", filters, generation, (last[0], last[1])
            )
        return {
            "items": [self._claim_item(value[2]) for value in visible],
            "limit": limit,
            "total": len(filtered),
            "status_counts": status_counts,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _evidence_value(ref: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "evidence_id": str(ref.get("evidence_id") or ""),
            "source": _compact_text(ref.get("source"), 40),
            "status": _compact_text(ref.get("status"), 40),
            "observed_at": str(ref.get("observed_at") or ""),
            "privacy": _compact_text(ref.get("privacy"), 40),
            "content_hash": str(ref.get("content_hash") or ""),
        }

    def _claim_evidence(self, evidence_ids: Any) -> List[Dict[str, Any]]:
        requested = _all_safe_ids(evidence_ids)[:MAX_PAGE_SIZE]
        try:
            catalog = EvidenceCatalog(
                self.vault_dir / "index.jsonl",
                database_path=self.vault_dir / "search_index.db",
            )
        except (EvidenceCatalogError, OSError, ValueError):
            return [
                {"evidence_id": evidence_id, "status": "unavailable"}
                for evidence_id in requested
            ]
        result = []
        for evidence_id in requested:
            try:
                result.append(self._evidence_value(catalog.resolve(evidence_id)))
            except (EvidenceCatalogError, OSError, ValueError):
                result.append(
                    {"evidence_id": evidence_id, "status": "unavailable"}
                )
        return result

    def _living_self_claim_refs(
        self, claim_id: str
    ) -> Tuple[str, List[Dict[str, Any]]]:
        try:
            current = self.living_self.current()
        except FileNotFoundError:
            return "empty", []
        except (EventPathError, OSError, TypeError, ValueError):
            return "unavailable", []
        sections = current.get("sections") if isinstance(current, Mapping) else None
        if not isinstance(sections, Mapping):
            return "unavailable", []
        refs = []
        for section, rows in sections.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping) or claim_id not in _all_safe_ids(
                    row.get("claim_ids")
                ):
                    continue
                refs.append(
                    {
                        "item_id": str(row.get("item_id") or ""),
                        "section": _compact_text(section, 60),
                        "status": _compact_text(row.get("status"), 40),
                    }
                )
        visible = refs[:MAX_PAGE_SIZE]
        return ("ready" if visible else "empty"), visible

    def claim_detail(self, claim_id: str) -> Dict[str, Any]:
        requested = _safe_identifier(claim_id, code="invalid_claim_id")
        try:
            claim = self.claim_store.get(requested)
        except Exception as exc:
            code = getattr(exc, "code", "claim_unavailable")
            if code == "claim_not_found":
                raise ProductDataError("claim_not_found", "没有找到这条记忆理解") from exc
            raise ProductDataError("claim_unavailable", "记忆理解当前不可用") from exc
        result = self._claim_item(claim)
        if claim.get("privacy") == "private":
            return result
        try:
            rows = _safe_sequence(self.claim_store.list())
        except Exception as exc:
            raise ProductDataError("claim_unavailable", "记忆理解当前不可用") from exc
        result["statement"] = _compact_text(claim.get("statement"), 4000)
        result["masked"] = False
        result["subject"] = _safe_product_tree(claim.get("subject"))
        result["speaker"] = _safe_product_tree(claim.get("speaker"))
        result["confidence_basis"] = _safe_product_tree(
            claim.get("confidence_basis")
        )
        evidence_ids = _all_safe_ids(claim.get("evidence_ids"))
        counter_evidence_ids = _all_safe_ids(claim.get("counter_evidence_ids"))
        result["evidence"] = self._claim_evidence(evidence_ids)
        result["evidence_total"] = len(evidence_ids)
        result["evidence_truncated"] = len(evidence_ids) > MAX_PAGE_SIZE
        result["counter_evidence"] = self._claim_evidence(counter_evidence_ids)
        result["counter_evidence_total"] = len(counter_evidence_ids)
        result["counter_evidence_truncated"] = (
            len(counter_evidence_ids) > MAX_PAGE_SIZE
        )
        result["supersedes"] = str(claim.get("supersedes") or "")
        result["superseded_by"] = next(
            (
                str(row.get("claim_id") or "")
                for row in rows
                if row.get("supersedes") == requested
            ),
            "",
        )
        living_status, living_refs = self._living_self_claim_refs(requested)
        result["living_self_status"] = living_status
        result["living_self_refs"] = living_refs
        try:
            snapshot = self._memory_value_snapshot()
            value_claim = snapshot["claims_by_id"][requested]
            if value_claim.get("revision") != claim.get("revision"):
                raise ProductDataError(
                    "memory_value_unavailable", "Claim 权威在读取期间发生变化"
                )
            evidence = self._memory_value_evidence([value_claim]).get(requested, {})
            result["value"] = self._memory_value_item(
                value_claim,
                snapshot["usage"].get(requested, self._memory_value_usage()),
                evidence,
                snapshot["now"],
            )
            result["value_status"] = "ready"
        except (KeyError, ProductDataError):
            result["value_status"] = "unavailable"
        return result

    def _claim_history_source_signature(self) -> Tuple[int, int, int, int, int]:
        path = getattr(getattr(self.claim_store, "events", None), "path", None)
        if path is None:
            raise ProductDataError("claim_unavailable", "Claim 权威不支持历史索引")
        try:
            with _anchored_parent(Path(path), create=False) as (parent_fd, name):
                metadata = _regular_stat_at(parent_fd, name)
                if metadata is None:
                    raise FileNotFoundError(str(path))
                return ProductIndexIntegrity._signature(metadata)
        except FileNotFoundError as exc:
            raise ProductDataError("claim_not_found", "没有找到这条记忆理解") from exc
        except OSError as exc:
            raise ProductDataError("claim_unavailable", "Claim 权威当前不可用") from exc

    @staticmethod
    def _claim_history_validate_events(
        events: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        current: Dict[str, Dict[str, Any]] = {}
        corrections: Dict[str, Mapping[str, Any]] = {}
        completed = set()
        try:
            for event in events:
                ClaimStore._project_claim_event(event, current, corrections, completed)
        except Exception as exc:
            raise ProductDataError("claim_unavailable", "Claim 权威重放失败") from exc
        if set(corrections) - completed:
            raise ProductDataError("claim_unavailable", "Claim 修订权威尚未完成")
        for claim_id, claim in current.items():
            _safe_identifier(claim_id, code="claim_unavailable")
            supersedes = claim.get("supersedes")
            if supersedes:
                _safe_identifier(supersedes, code="claim_unavailable")
        return current

    @staticmethod
    def _claim_history_index_digest(connection: sqlite3.Connection) -> str:
        digest = hashlib.sha256()
        queries = (
            (
                "claims",
                "SELECT stream_id,is_private,total FROM claim_streams "
                "ORDER BY stream_id",
            ),
            (
                "history",
                "SELECT stream_id,time_key,event_key,event_type,occurred_at,"
                "stream_version,revision,actor_kind,previous_status,current_status,"
                "reason,related_claim_id,is_private FROM history "
                "ORDER BY stream_id,time_key,event_key",
            ),
        )
        for table, query in queries:
            for row in connection.execute(query):
                encoded = json.dumps(
                    [table, *row],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        return digest.hexdigest()

    def _claim_history_index_receipt(self, identity: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hmac.new(self._signing_key(), encoded, hashlib.sha256).hexdigest()

    @staticmethod
    def _claim_history_index_item(
        event: Mapping[str, Any], *, private: bool
    ) -> Dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        claim = payload.get("claim") if isinstance(payload.get("claim"), Mapping) else {}
        replacement = (
            payload.get("replacement")
            if isinstance(payload.get("replacement"), Mapping)
            else {}
        )
        actor = event.get("actor") if isinstance(event.get("actor"), Mapping) else {}
        occurred_at = str(event.get("occurred_at") or "")
        try:
            time_key = normalize_timestamp_utc(occurred_at)
        except IndexIntegrityError as exc:
            raise ProductDataError(
                "claim_unavailable", "记忆历史时间当前不可用"
            ) from exc
        item = {
            "event_type": _compact_text(event.get("event_type"), 80),
            "occurred_at": occurred_at,
            "stream_version": event.get("stream_version"),
            "revision": claim.get("revision"),
            "actor_kind": _compact_text(actor.get("kind"), 40),
            "previous_status": _compact_text(event.get("previous_status"), 40),
            "current_status": _compact_text(claim.get("status"), 40),
            "reason": _compact_text(payload.get("reason"), 500),
            "related_claim_id": str(
                replacement.get("claim_id") or claim.get("supersedes") or ""
            ),
        }
        if private:
            item["actor_kind"] = ""
            item["reason"] = CLAIM_HISTORY_PRIVATE_REASON
            item["related_claim_id"] = ""
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ProductDataError("claim_unavailable", "Claim 历史事件标识无效")
        return {
            "stream_id": str(event.get("stream_id") or ""),
            "time_key": time_key,
            "event_key": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
            "is_private": int(private),
            **item,
        }

    def _claim_history_product_directory(self) -> Path:
        directory = self.vault_dir / "product"
        descriptor = -1
        try:
            with _anchored_parent(directory, create=False) as (parent_fd, name):
                parent = os.fstat(parent_fd)
                if (
                    not stat.S_ISDIR(parent.st_mode)
                    or parent.st_uid != os.geteuid()
                    or stat.S_IMODE(parent.st_mode) & 0o022
                ):
                    raise ProductDataError(
                        "claim_unavailable", "Claim 历史索引目录不安全"
                    )
                observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(
                    observed.st_mode
                ):
                    raise ProductDataError(
                        "claim_unavailable", "Claim 历史索引目录不安全"
                    )
                descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
                metadata = os.fstat(descriptor)
                identity = (metadata.st_dev, metadata.st_ino)
                mode = stat.S_IMODE(metadata.st_mode)
                if (
                    identity != (observed.st_dev, observed.st_ino)
                    or metadata.st_uid != os.geteuid()
                    or mode not in (0o700, 0o755)
                    or (mode == 0o755 and mode & 0o022)
                ):
                    raise ProductDataError(
                        "claim_unavailable", "Claim 历史索引目录不安全"
                    )
                if _fd_has_extended_acl(parent_fd) or _fd_has_extended_acl(
                    descriptor
                ):
                    raise ProductDataError(
                        "claim_unavailable", "Claim 历史索引目录不安全"
                    )
                if mode == 0o755:
                    os.fchmod(descriptor, 0o700)
                    metadata = os.fstat(descriptor)
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    (metadata.st_dev, metadata.st_ino) != identity
                    or (current.st_dev, current.st_ino) != identity
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    raise ProductDataError(
                        "claim_unavailable", "Claim 历史索引目录不安全"
                    )
        except ProductDataError:
            raise
        except (EventPathError, OSError) as exc:
            raise ProductDataError(
                "claim_unavailable", "Claim 历史索引目录不可用"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return directory

    def _rebuild_claim_history_index(self, database_path: Path) -> None:
        source_before = self._claim_history_source_signature()
        events = self._memory_value_event_rows(self.claim_store)
        if events is None:
            raise ProductDataError("claim_unavailable", "Claim 权威不支持历史索引")
        current = self._claim_history_validate_events(events)
        source_after = self._claim_history_source_signature()
        if source_before != source_after:
            raise ProductDataError(
                "claim_unavailable", "Claim 权威在索引重建期间发生变化"
            )
        history_rows = []
        totals = {claim_id: 0 for claim_id in current}
        for event in events:
            if event.get("event_type") not in CLAIM_EVENT_TYPES:
                continue
            claim = event.get("payload", {}).get("claim", {})
            if not isinstance(claim, Mapping):
                raise ProductDataError("claim_unavailable", "Claim 历史载荷无效")
            stream_id = str(event.get("stream_id") or "")
            private = claim.get("privacy") == "private"
            history_rows.append(
                self._claim_history_index_item(event, private=private)
            )
            totals[stream_id] = totals.get(stream_id, 0) + 1

        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._claim_history_product_directory()
        temporary = database_path.with_name(
            "." + database_path.name + "." + secrets.token_hex(8) + ".tmp"
        )
        descriptor = None
        connection = None
        try:
            descriptor = os.open(
                str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(descriptor)
            descriptor = None
            connection = sqlite3.connect(str(temporary), timeout=3)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE claim_streams("
                "stream_id TEXT PRIMARY KEY,is_private INTEGER NOT NULL,total INTEGER NOT NULL);"
                "CREATE TABLE history("
                "stream_id TEXT NOT NULL,time_key TEXT NOT NULL,event_key TEXT NOT NULL,"
                "event_type TEXT NOT NULL,occurred_at TEXT NOT NULL,"
                "stream_version INTEGER NOT NULL,revision INTEGER,actor_kind TEXT NOT NULL,"
                "previous_status TEXT NOT NULL,current_status TEXT NOT NULL,"
                "reason TEXT NOT NULL,related_claim_id TEXT NOT NULL,"
                "is_private INTEGER NOT NULL,"
                "PRIMARY KEY(event_key));"
                "CREATE INDEX idx_claim_history_stream_page "
                "ON history(stream_id,time_key,event_key);"
            )
            connection.executemany(
                "INSERT INTO claim_streams(stream_id,is_private,total) VALUES(?,?,?)",
                [
                    (
                        claim_id,
                        int(claim.get("privacy") == "private"),
                        totals.get(claim_id, 0),
                    )
                    for claim_id, claim in sorted(current.items())
                ],
            )
            connection.executemany(
                "INSERT INTO history("
                "stream_id,time_key,event_key,event_type,occurred_at,stream_version,"
                "revision,actor_kind,previous_status,current_status,reason,"
                "related_claim_id,is_private) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    tuple(
                        row[key]
                        for key in (
                            "stream_id",
                            "time_key",
                            "event_key",
                            "event_type",
                            "occurred_at",
                            "stream_version",
                            "revision",
                            "actor_kind",
                            "previous_status",
                            "current_status",
                            "reason",
                            "related_claim_id",
                            "is_private",
                        )
                    )
                    for row in history_rows
                ],
            )
            identity = {
                "schema_version": CLAIM_HISTORY_INDEX_SCHEMA_VERSION,
                "source_signature": list(source_after),
                "source_generation": self._claim_generation(events),
                "event_count": len(history_rows),
                "row_digest": self._claim_history_index_digest(connection),
            }
            connection.executemany(
                "INSERT INTO meta(key,value) VALUES(?,?)",
                (
                    (
                        "identity",
                        json.dumps(identity, sort_keys=True, separators=(",", ":")),
                    ),
                    ("receipt", self._claim_history_index_receipt(identity)),
                ),
            )
            connection.commit()
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise sqlite3.DatabaseError("claim history index check failed")
            connection.close()
            connection = None
            descriptor = os.open(str(temporary), os.O_RDONLY)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(str(temporary), str(database_path))
            os.chmod(str(database_path), 0o600)
            parent_fd = os.open(str(database_path.parent), os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            self._claim_history_cache_identity = None
        except ProductDataError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise ProductDataError(
                "claim_unavailable", "Claim 历史索引重建失败"
            ) from exc
        finally:
            if connection is not None:
                connection.close()
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _open_claim_history_index(
        self, database_path: Path, source_signature: Sequence[int]
    ) -> Tuple[sqlite3.Connection, Dict[str, Any], Tuple[Any, ...]]:
        metadata = ProductIndexIntegrity._regular_file(database_path)
        if metadata.st_mode & 0o777 != 0o600 or metadata.st_nlink != 1:
            raise ValueError("claim history index mode is unsafe")
        wal = Path(str(database_path) + "-wal")
        try:
            wal_meta = ProductIndexIntegrity._regular_file(wal)
        except FileNotFoundError:
            pass
        else:
            if wal_meta.st_size:
                raise ValueError("claim history index has a non-empty WAL")
        database_signature = ProductIndexIntegrity._signature(metadata)
        uri = database_path.resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.execute("PRAGMA query_only=ON")
        try:
            meta = dict(connection.execute("SELECT key,value FROM meta").fetchall())
            if set(meta) != {"identity", "receipt"}:
                raise ValueError("claim history index metadata is incomplete")
            identity = json.loads(meta["identity"])
            if (
                not isinstance(identity, Mapping)
                or set(identity)
                != {
                    "schema_version",
                    "source_signature",
                    "source_generation",
                    "event_count",
                    "row_digest",
                }
                or identity["schema_version"] != CLAIM_HISTORY_INDEX_SCHEMA_VERSION
                or identity["source_signature"] != list(source_signature)
                or re.fullmatch(r"[0-9a-f]{64}", str(identity["source_generation"]))
                is None
                or re.fullmatch(r"[0-9a-f]{64}", str(identity["row_digest"])) is None
                or not isinstance(identity["event_count"], int)
                or not hmac.compare_digest(
                    str(meta["receipt"]), self._claim_history_index_receipt(identity)
                )
            ):
                raise ValueError("claim history index receipt is invalid")
            cache_identity = (
                database_signature,
                tuple(source_signature),
                identity["source_generation"],
                identity["row_digest"],
            )
            if self._claim_history_cache_identity != cache_identity:
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("claim history index is corrupt")
                count = int(connection.execute("SELECT count(*) FROM history").fetchone()[0])
                if (
                    count != identity["event_count"]
                    or self._claim_history_index_digest(connection)
                    != identity["row_digest"]
                ):
                    raise ValueError("claim history index rows differ from receipt")
                self._claim_history_cache_identity = cache_identity
            if ProductIndexIntegrity._signature(
                ProductIndexIntegrity._regular_file(database_path)
            ) != database_signature:
                raise ValueError("claim history index changed during verification")
            return connection, dict(identity), database_signature
        except Exception:
            connection.close()
            raise

    def _claim_history_index(
        self,
    ) -> Tuple[sqlite3.Connection, Dict[str, Any], Tuple[int, int, int, int, int]]:
        database_path = self.vault_dir / "product" / "claim-history-v1.sqlite3"
        self._claim_history_product_directory()
        source_signature = self._claim_history_source_signature()
        try:
            connection, identity, _database_signature = self._open_claim_history_index(
                database_path, source_signature
            )
        except ProductDataError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            self._rebuild_claim_history_index(database_path)
            source_signature = self._claim_history_source_signature()
            try:
                connection, identity, _database_signature = self._open_claim_history_index(
                    database_path, source_signature
                )
            except Exception as exc:
                raise ProductDataError(
                    "claim_unavailable", "Claim 历史索引当前不可用"
                ) from exc
        return connection, identity, source_signature

    def claim_history(
        self,
        claim_id: str,
        query: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> Dict[str, Any]:
        requested = _safe_identifier(claim_id, code="invalid_claim_id")
        limit, cursor, filters = self._query(query, allowed_filters=())
        self._signing_key()
        try:
            with _exclusive_lock(
                self.vault_dir / "product" / "claim-history.lock",
                timeout=30.0,
                stale_after=60.0,
            ):
                connection, identity, source_signature = self._claim_history_index()
                try:
                    stream = connection.execute(
                        "SELECT is_private,total FROM claim_streams WHERE stream_id=?",
                        (requested,),
                    ).fetchone()
                    if stream is None:
                        raise ProductDataError(
                            "claim_not_found", "没有找到这条记忆理解"
                        )
                    private = bool(stream[0])
                    total = int(stream[1])
                    endpoint = (
                        "claim_history:v"
                        + str(CLAIM_HISTORY_INDEX_SCHEMA_VERSION)
                        + ":"
                        + requested
                    )
                    generation = str(identity["source_generation"])
                    key = self._decode_cursor(
                        cursor, endpoint, filters, generation, 2
                    )
                    parameters: List[Any] = [requested]
                    condition = "stream_id=?"
                    if key is not None:
                        if (
                            not all(isinstance(value, str) for value in key)
                            or re.fullmatch(r"[0-9a-f]{64}", key[1]) is None
                        ):
                            raise ProductDataError("invalid_cursor", "分页游标无效")
                        condition += " AND (time_key,event_key) > (?,?)"
                        parameters.extend((key[0], key[1]))
                    parameters.append(limit + 1)
                    rows = connection.execute(
                        "SELECT time_key,event_key,event_type,occurred_at,stream_version,"
                        "revision,actor_kind,previous_status,current_status,reason,"
                        "related_claim_id FROM history WHERE "
                        + condition
                        + " ORDER BY time_key,event_key LIMIT ?",
                        parameters,
                    ).fetchall()
                    if self._claim_history_source_signature() != source_signature:
                        raise ProductDataError(
                            "claim_unavailable", "Claim 权威在读取期间发生变化"
                        )
                finally:
                    connection.close()
        except Exception as exc:
            code = getattr(exc, "code", "claim_unavailable")
            if code == "claim_not_found":
                raise ProductDataError("claim_not_found", "没有找到这条记忆理解") from exc
            if code == "invalid_cursor":
                raise
            raise ProductDataError("claim_unavailable", "记忆历史当前不可用") from exc
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            item = {
                "event_type": row[2],
                "occurred_at": row[3],
                "stream_version": row[4],
                "revision": row[5],
                "actor_kind": row[6],
                "previous_status": row[7],
                "current_status": row[8],
                "reason": row[9],
                "related_claim_id": row[10],
            }
            if private:
                item = {
                    key: item[key]
                    for key in (
                        "event_type",
                        "occurred_at",
                        "stream_version",
                        "revision",
                        "previous_status",
                        "current_status",
                        "reason",
                    )
                }
            items.append(item)
        next_cursor = ""
        if has_more and rows:
            last = rows[-1]
            next_cursor = self._encode_cursor(
                endpoint,
                filters,
                str(identity["source_generation"]),
                (last[0], last[1]),
            )
        return {
            "claim_id": requested,
            "items": items,
            "limit": limit,
            "total": total,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _memory_value_usage() -> Dict[str, Any]:
        return {
            "attribution_status": "available",
            "trace_selected": 0,
            "preview_selected": 0,
            "compiled_snapshot_selected": 0,
            "delivered": 0,
            "acknowledged": 0,
            "last_acknowledged_at": None,
            "outcomes": {name: 0 for name in ("positive", "mixed", "negative", "unknown")},
            "confirmed": 0,
            "challenged": 0,
            "unattributed": 0,
            "last_challenged_at": None,
            "timeline": [],
            "timeline_total": 0,
        }

    @staticmethod
    def _memory_value_time(value: Any, *, required: bool = False) -> str:
        if (value is None or value == "") and not required:
            return ""
        try:
            return normalize_timestamp_utc(value)
        except (IndexIntegrityError, TypeError, ValueError) as exc:
            raise ProductDataError(
                "memory_value_unavailable", "记忆使用时间当前不可用"
            ) from exc

    @staticmethod
    def _memory_value_event_rows(store: Any) -> Optional[List[Dict[str, Any]]]:
        events = getattr(store, "events", None)
        reader = getattr(events, "read_all", None)
        if not callable(reader):
            return None
        try:
            if callable(getattr(events, "exists", None)) and not events.exists():
                return []
            rows = reader(limit=MEMORY_VALUE_AUTHORITY_EVENT_LIMIT + 1)
        except Exception as exc:
            raise ProductDataError(
                "memory_value_unavailable", "记忆价值权威当前不可用"
            ) from exc
        if len(rows) > MEMORY_VALUE_AUTHORITY_EVENT_LIMIT:
            raise ProductDataError(
                "memory_value_unavailable", "记忆价值权威超过安全重放上限"
            )
        return _safe_sequence(rows)

    @staticmethod
    def _memory_value_fallback_rows(store: Any) -> List[Dict[str, Any]]:
        try:
            rows = _safe_sequence(store.list())
        except Exception as exc:
            raise ProductDataError(
                "memory_value_unavailable", "记忆价值权威当前不可用"
            ) from exc
        if len(rows) > MEMORY_VALUE_AUTHORITY_EVENT_LIMIT:
            raise ProductDataError(
                "memory_value_unavailable", "记忆价值权威超过安全投影上限"
            )
        return rows

    @classmethod
    def _memory_value_authority_generation(
        cls, rows: Sequence[Tuple[str, Sequence[Mapping[str, Any]]]]
    ) -> str:
        tagged = [
            {"authority": authority, "position": position, "row": dict(row)}
            for authority, values in rows
            for position, row in enumerate(values)
        ]
        return cls._claim_generation(tagged)

    def _memory_value_authorities(self) -> Dict[str, Any]:
        claim_events = self._memory_value_event_rows(self.claim_store)
        context_events = self._memory_value_event_rows(self.context_store)
        outcome_events = self._memory_value_event_rows(self.outcome_store)
        for store, rows in (
            (self.claim_store, claim_events),
            (self.context_store, context_events),
            (self.outcome_store, outcome_events),
        ):
            if rows is None:
                continue
            current = self._memory_value_event_rows(store)
            if current is None or self._claim_generation(rows) != self._claim_generation(current):
                raise ProductDataError(
                    "memory_value_unavailable", "记忆价值权威在读取期间发生变化"
                )

        if claim_events is None:
            claims = self._memory_value_fallback_rows(self.claim_store)
        else:
            current_claims: Dict[str, Dict[str, Any]] = {}
            corrections: Dict[str, Mapping[str, Any]] = {}
            completed = set()
            try:
                for event in claim_events:
                    ClaimStore._project_claim_event(
                        event, current_claims, corrections, completed
                    )
            except Exception as exc:
                raise ProductDataError(
                    "memory_value_unavailable", "Claim 权威重放失败"
                ) from exc
            if set(corrections) - completed:
                raise ProductDataError(
                    "memory_value_unavailable", "Claim 修订权威尚未完成"
                )
            claims = [current_claims[key] for key in sorted(current_claims)]

        if context_events is None:
            contexts = self._memory_value_fallback_rows(self.context_store)
        else:
            current_contexts: Dict[str, Dict[str, Any]] = {}
            try:
                for event in context_events:
                    ContextStore._project_event(event, current_contexts)
            except Exception as exc:
                raise ProductDataError(
                    "memory_value_unavailable", "Context 权威重放失败"
                ) from exc
            contexts = [current_contexts[key] for key in sorted(current_contexts)]

        outcome_linkages: Dict[str, List[Dict[str, Any]]] = {}
        if context_events is not None:
            for event in context_events:
                if event.get("event_type") != "context.outcome_recorded":
                    continue
                record = ContextStore._event_record(event)
                context_id = str(record.get("context_id") or "")
                outcome_linkages.setdefault(context_id, []).append(event)

        decoded_outcomes = []
        if outcome_events is None:
            outcomes = self._memory_value_fallback_rows(self.outcome_store)
        else:
            try:
                for event in outcome_events:
                    outcome, operation = OutcomeStore._decode_event(event)
                    decoded_outcomes.append(
                        {"event": event, "operation": operation, "outcome": outcome}
                    )
            except Exception as exc:
                raise ProductDataError(
                    "memory_value_unavailable", "Outcome 权威重放失败"
                ) from exc
            outcomes = [row["outcome"] for row in decoded_outcomes]

        raw_generation_rows = [
            ("claims", claim_events if claim_events is not None else claims),
            ("contexts", context_events if context_events is not None else contexts),
            ("outcomes", outcome_events if outcome_events is not None else outcomes),
        ]
        return {
            "claims": claims,
            "claim_events": claim_events,
            "contexts": contexts,
            "context_events": context_events,
            "outcomes": outcomes,
            "decoded_outcomes": decoded_outcomes,
            "outcome_linkages": outcome_linkages,
            "claim_event_head": (
                int(claim_events[-1]["seq"]) if claim_events else None
            ),
            "generation": self._memory_value_authority_generation(raw_generation_rows),
        }

    @staticmethod
    def _memory_value_historical_claims(
        contexts: Sequence[Mapping[str, Any]],
        claim_events: Optional[Sequence[Mapping[str, Any]]],
        current_claims: Mapping[str, Mapping[str, Any]],
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        if claim_events is None:
            return None
        known_claim_ids = set(current_claims)
        for event in claim_events:
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            for field in ("claim", "replacement"):
                candidate = payload.get(field)
                if not isinstance(candidate, Mapping):
                    continue
                claim_id = str(candidate.get("claim_id") or "")
                if ID_PATTERN.fullmatch(claim_id) is not None:
                    known_claim_ids.add(claim_id)
        requests = []
        for context in contexts:
            identity = str(context.get("context_id") or context.get("preview_id") or "")
            source_revision = context.get("source_revision")
            trace = context.get("selection_trace")
            candidates = trace.get("candidates") if isinstance(trace, Mapping) else []
            selected = [
                (str(row.get("id") or ""), row.get("revision"))
                for row in candidates
                if isinstance(row, Mapping)
                and row.get("kind") == "claim"
                and row.get("decision") == "selected"
            ]
            legacy = (
                isinstance(source_revision, Mapping)
                and source_revision.get("policy_version") == 1
                and not selected
            )
            if legacy:
                selection = context.get("selection")
                selected_ids = (
                    selection.get("selected_item_ids")
                    if isinstance(selection, Mapping)
                    else []
                )
                selected = [
                    (claim_id, None)
                    for claim_id in _all_safe_ids(selected_ids)
                    if claim_id in known_claim_ids
                ]
            if not selected:
                continue
            sequence = (
                source_revision.get("claims_event_seq")
                if isinstance(source_revision, Mapping)
                else None
            )
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ProductDataError(
                    "memory_value_unavailable", "Context 缺少 Claim 权威水位"
                )
            requests.append((sequence, identity, selected, legacy))
        requests.sort(key=lambda row: (row[0], row[1]))
        current: Dict[str, Dict[str, Any]] = {}
        corrections: Dict[str, Mapping[str, Any]] = {}
        completed = set()
        position = 0
        result: Dict[str, Dict[str, Any]] = {}
        allowed_sources = {"direct", "quoted", "observed", "user_declared"}
        for sequence, identity, selected, legacy in requests:
            while (
                position < len(claim_events)
                and int(claim_events[position].get("seq") or 0) <= sequence
            ):
                try:
                    ClaimStore._project_claim_event(
                        claim_events[position], current, corrections, completed
                    )
                except Exception as exc:
                    raise ProductDataError(
                        "memory_value_unavailable", "Context 的 Claim 水位无法验证"
                    ) from exc
                position += 1
            eligible: Dict[str, int] = {}
            unavailable = set()
            for claim_id, revision in selected:
                historical = current.get(claim_id)
                if (
                    historical is None
                    or not isinstance(historical.get("revision"), int)
                    or isinstance(historical.get("revision"), bool)
                    or historical.get("status") != "confirmed"
                    or historical.get("privacy") == "private"
                    or historical.get("source_kind") not in allowed_sources
                ):
                    if legacy:
                        unavailable.add(claim_id)
                        continue
                    raise ProductDataError(
                        "memory_value_unavailable",
                        "Context 选择的 Claim 在记录水位不可用",
                    )
                if (
                    not legacy
                    and (
                        not isinstance(revision, int)
                        or isinstance(revision, bool)
                        or historical.get("revision") != revision
                    )
                ):
                    raise ProductDataError(
                        "memory_value_unavailable",
                        "Context 选择的 Claim 在记录水位不可用",
                    )
                eligible[claim_id] = int(historical["revision"])
            result[identity] = {
                "revisions": eligible,
                "unavailable": unavailable,
            }
        return result

    @classmethod
    def _memory_value_timeline(
        cls, usage: Dict[str, Any], item: Mapping[str, Any]
    ) -> None:
        occurred_at = cls._memory_value_time(item.get("occurred_at"))
        if not occurred_at:
            return
        usage["timeline_total"] += 1
        usage["timeline"].append({**dict(item), "occurred_at": occurred_at})
        usage["timeline"].sort(
            key=lambda row: str(row.get("occurred_at") or ""), reverse=True
        )
        del usage["timeline"][MAX_PAGE_SIZE:]

    @staticmethod
    def _memory_value_real_outcome_binding(
        decoded: Mapping[str, Any],
        context: Mapping[str, Any],
        matches: Sequence[Mapping[str, Any]],
    ) -> None:
        outcome = decoded["outcome"]
        event = decoded["event"]
        operation = decoded["operation"]
        if len(matches) != 1:
            raise ProductDataError(
                "memory_value_unavailable", "Outcome 与 Context 权威关联不唯一"
            )
        linkage = matches[0]
        linkage_operation = linkage.get("payload", {}).get("operation", {})
        event_hash, request_id, idempotency_key = OutcomeStore._binding_metadata(
            event, str(outcome.get("outcome_id") or "")
        )
        if (
            context.get("outcome_id") != outcome.get("outcome_id")
            or context.get("outcome_hash") != event_hash
            or linkage.get("request_id") != request_id
            or linkage.get("idempotency_key")
            != ContextStore._public_key(idempotency_key)
            or linkage.get("occurred_at") != context.get("outcome_recorded_at")
            or linkage_operation.get("outcome_id") != outcome.get("outcome_id")
            or linkage_operation.get("outcome_hash") != event_hash
            or linkage_operation.get("actor") != operation.get("actor")
            or linkage_operation.get("reason") != operation.get("reason")
        ):
            raise ProductDataError(
                "memory_value_unavailable", "Outcome 与 Context 权威关联冲突"
            )

    def _memory_value_snapshot(self) -> Dict[str, Any]:
        authority = self._memory_value_authorities()
        try:
            now = self._clock()
        except Exception as exc:
            raise ProductDataError("clock_unavailable", "系统时间不可用") from exc
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ProductDataError("clock_unavailable", "系统时间不可用")
        now = now.astimezone(timezone.utc)

        claims: Dict[str, Dict[str, Any]] = {}
        for row in authority["claims"]:
            claim_id = str(row.get("claim_id") or "")
            if ID_PATTERN.fullmatch(claim_id) is None or claim_id in claims:
                raise ProductDataError(
                    "memory_value_unavailable", "Claim 权威标识冲突"
                )
            claims[claim_id] = row

        usage: Dict[str, Dict[str, Any]] = {}
        contexts: Dict[str, Dict[str, Any]] = {}
        acknowledged_contexts = set()
        historical_claims = self._memory_value_historical_claims(
            authority["contexts"], authority["claim_events"], claims
        )
        for context in authority["contexts"]:
            context_id = str(context.get("context_id") or "")
            preview_id = str(context.get("preview_id") or "")
            identity = context_id or preview_id
            selection = context.get("selection")
            if not identity:
                if any(value not in (None, "", [], {}) for value in context.values()):
                    raise ProductDataError(
                        "memory_value_unavailable", "Context 权威缺少稳定标识"
                    )
                continue
            if identity in contexts or ID_PATTERN.fullmatch(identity) is None:
                raise ProductDataError(
                    "memory_value_unavailable", "Context 权威标识冲突"
                )
            source_revision = context.get("source_revision")
            if authority["claim_event_head"] is not None and isinstance(
                source_revision, Mapping
            ):
                claims_event_seq = source_revision.get("claims_event_seq")
                if (
                    not isinstance(claims_event_seq, int)
                    or isinstance(claims_event_seq, bool)
                    or claims_event_seq > authority["claim_event_head"]
                ):
                    raise ProductDataError(
                        "memory_value_unavailable", "Context 引用未来 Claim 权威"
                    )
            if not isinstance(selection, Mapping):
                selection = {}
            raw_selected = selection.get("selected_item_ids") or []
            raw_excluded = selection.get("excluded_item_ids") or []
            if (
                not isinstance(raw_selected, list)
                or not isinstance(raw_excluded, list)
                or len(raw_selected) > MEMORY_VALUE_REFERENCE_LIMIT
                or len(raw_excluded) > MEMORY_VALUE_REFERENCE_LIMIT
            ):
                raise ProductDataError(
                    "memory_value_unavailable", "Context 引用超过安全上限"
                )
            selected = set(_all_safe_ids(raw_selected))
            excluded = set(_all_safe_ids(raw_excluded))
            selected_claims = selected & set(claims)
            if historical_claims is None and any(
                claims[item].get("privacy") == "private"
                for item in selected_claims
            ):
                raise ProductDataError(
                    "memory_value_unavailable", "Context 包含不应投影的私密 Claim"
                )
            lifecycle = str(context.get("lifecycle_status") or "")
            receipt = context.get("delivery_receipt")
            acknowledged = (
                lifecycle in {"consumed", "outcome_recorded"}
                and isinstance(receipt, Mapping)
                and bool(receipt.get("delivery_id"))
            )
            if isinstance(receipt, Mapping) and receipt and not acknowledged:
                raise ProductDataError(
                    "memory_value_unavailable", "Context 回执与生命周期冲突"
                )
            consumed_at = self._memory_value_time(
                context.get("consumed_at"), required=acknowledged
            )
            trace = context.get("selection_trace")
            candidates = trace.get("candidates") if isinstance(trace, Mapping) else []
            if not isinstance(candidates, list) or len(candidates) > MEMORY_VALUE_REFERENCE_LIMIT:
                raise ProductDataError(
                    "memory_value_unavailable", "Context 选择轨迹超过安全上限"
                )
            trace_claims = set()
            trace_claim_revisions: Dict[str, int] = {}
            for candidate in candidates:
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("kind") == "claim"
                    and candidate.get("decision") == "selected"
                ):
                    candidate_id = str(candidate.get("id") or "")
                    if candidate_id not in claims or (
                        historical_claims is None
                        and claims[candidate_id].get("privacy") == "private"
                    ):
                        raise ProductDataError(
                            "memory_value_unavailable", "Context 选择轨迹引用未知 Claim"
                        )
                    candidate_revision = candidate.get("revision")
                    if (
                        not isinstance(candidate_revision, int)
                        or isinstance(candidate_revision, bool)
                    ):
                        raise ProductDataError(
                            "memory_value_unavailable", "Context 选择轨迹缺少 Claim revision"
                        )
                    trace_claims.add(candidate_id)
                    trace_claim_revisions[candidate_id] = candidate_revision
            if not trace_claims.issubset(selected):
                raise ProductDataError(
                    "memory_value_unavailable", "Context 选择轨迹与选择结果不一致"
                )
            if historical_claims is not None:
                historical = historical_claims.get(
                    identity, {"revisions": {}, "unavailable": set()}
                )
                selected_claim_revisions = dict(historical["revisions"])
                selected_claims = set(selected_claim_revisions)
                for claim_id in historical["unavailable"]:
                    value = usage.setdefault(claim_id, self._memory_value_usage())
                    value["attribution_status"] = "unavailable"
            else:
                selected_claim_revisions = {}
                for claim_id in selected_claims:
                    revision = trace_claim_revisions.get(
                        claim_id, claims[claim_id].get("revision")
                    )
                    if not isinstance(revision, int) or isinstance(revision, bool):
                        raise ProductDataError(
                            "memory_value_unavailable", "Context Claim revision 不可用"
                        )
                    selected_claim_revisions[claim_id] = revision
            for claim_id in trace_claims:
                value = usage.setdefault(claim_id, self._memory_value_usage())
                value["trace_selected"] += 1
                self._memory_value_timeline(
                    value,
                    {
                        "context_id": identity,
                        "stage": "trace_selected",
                        "occurred_at": context.get("generated_at"),
                    },
                )
            compiled_claim_revisions = (
                {
                    claim_id: selected_claim_revisions[claim_id]
                    for claim_id in selected_claims - excluded
                }
                if lifecycle in {"compiled", "consumed", "outcome_recorded"}
                else {}
            )
            if (
                authority["context_events"] is not None
                and lifecycle in {"compiled", "consumed", "outcome_recorded"}
            ):
                try:
                    snapshot = self.context_compiler.load_historical_snapshot(identity)
                    snapshot_refs = OutcomeStore._snapshot_refs(snapshot)
                except Exception as exc:
                    raise ProductDataError(
                        "memory_value_unavailable",
                        "Context 不可变快照当前不可验证",
                    ) from exc
                snapshot_claim_revisions: Dict[str, int] = {}
                for kind, claim_id, revision in snapshot_refs:
                    if kind != "claim":
                        continue
                    if (
                        ID_PATTERN.fullmatch(str(claim_id or "")) is None
                        or not isinstance(revision, int)
                        or isinstance(revision, bool)
                        or (
                            claim_id in snapshot_claim_revisions
                            and snapshot_claim_revisions[claim_id] != revision
                        )
                    ):
                        raise ProductDataError(
                            "memory_value_unavailable",
                            "Context 不可变快照 Claim 引用无效",
                        )
                    snapshot_claim_revisions[claim_id] = revision
                if snapshot_claim_revisions != compiled_claim_revisions:
                    raise ProductDataError(
                        "memory_value_unavailable",
                        "Context 不可变快照与 Claim 权威不一致",
                    )
                compiled_claim_revisions = snapshot_claim_revisions
            compiled_claims = set(compiled_claim_revisions)
            for claim_id in selected_claims:
                value = usage.setdefault(claim_id, self._memory_value_usage())
                value["preview_selected"] += 1
                self._memory_value_timeline(
                    value,
                    {
                        "context_id": identity,
                        "stage": "preview_selected",
                        "occurred_at": context.get("generated_at"),
                    },
                )
                if claim_id in compiled_claims:
                    value["compiled_snapshot_selected"] += 1
                    self._memory_value_timeline(
                        value,
                        {
                            "context_id": identity,
                            "stage": "compiled_snapshot_selected",
                            "occurred_at": context.get("compiled_at"),
                        },
                    )
                if acknowledged and claim_id in compiled_claims:
                    value["delivered"] += 1
                    value["acknowledged"] += 1
                    value["last_acknowledged_at"] = max(
                        str(value.get("last_acknowledged_at") or ""), consumed_at
                    )
                    for stage in ("delivered", "acknowledged"):
                        self._memory_value_timeline(
                            value,
                            {
                                "context_id": identity,
                                "stage": stage,
                                "occurred_at": consumed_at,
                            },
                        )
            if acknowledged and compiled_claims:
                acknowledged_contexts.add(identity)
            contexts[identity] = {
                "row": context,
                "selected_claims": selected_claims,
                "compiled_claims": compiled_claims,
                "compiled_claim_revisions": compiled_claim_revisions,
                "acknowledged": acknowledged,
            }

        decoded_by_context = {
            str(row["outcome"].get("context_id") or ""): row
            for row in authority["decoded_outcomes"]
        }
        if len(decoded_by_context) != len(authority["decoded_outcomes"]):
            raise ProductDataError(
                "memory_value_unavailable", "Outcome 权威包含重复 Context"
            )
        evaluated_contexts = set()
        unattributed_outcome_refs = 0
        seen_outcomes = set()
        for outcome in authority["outcomes"]:
            context_id = str(outcome.get("context_id") or "")
            outcome_id = str(outcome.get("outcome_id") or "")
            if not context_id and not outcome_id and not any(outcome.values()):
                continue
            if not context_id or context_id in seen_outcomes or context_id not in contexts:
                raise ProductDataError(
                    "memory_value_unavailable", "Outcome 与 Context 权威不一致"
                )
            seen_outcomes.add(context_id)
            linked = contexts[context_id]
            if not linked["acknowledged"]:
                raise ProductDataError(
                    "memory_value_unavailable", "Outcome 缺少可验证的 Context 回执"
                )
            decoded = decoded_by_context.get(context_id)
            if decoded is not None:
                self._memory_value_real_outcome_binding(
                    decoded,
                    linked["row"],
                    authority["outcome_linkages"].get(context_id, []),
                )
            compiled_claim_revisions = linked["compiled_claim_revisions"]
            selected_claims = set(compiled_claim_revisions)
            result = str(outcome.get("result") or "")
            if result not in {"positive", "mixed", "negative", "unknown"}:
                raise ProductDataError(
                    "memory_value_unavailable", "Outcome 结果状态无效"
                )
            created_at = self._memory_value_time(
                outcome.get("created_at"), required=bool(selected_claims)
            )
            claim_refs: Dict[str, set] = {"confirmed": set(), "challenged": set()}
            for field, counter in (
                ("confirmed_refs", "confirmed"),
                ("challenged_refs", "challenged"),
            ):
                refs = outcome.get(field) or []
                if not isinstance(refs, list) or len(refs) > MEMORY_VALUE_REFERENCE_LIMIT:
                    raise ProductDataError(
                        "memory_value_unavailable", "Outcome 引用超过安全上限"
                    )
                for ref in refs:
                    if not isinstance(ref, Mapping) or ref.get("kind") != "claim":
                        continue
                    claim_id = str(ref.get("id") or "")
                    if claim_id not in selected_claims:
                        raise ProductDataError(
                            "memory_value_unavailable", "Outcome 引用了未选中的 Claim"
                        )
                    revision = ref.get("revision")
                    if (
                        not isinstance(revision, int)
                        or isinstance(revision, bool)
                        or revision != compiled_claim_revisions[claim_id]
                    ):
                        value = usage.setdefault(
                            claim_id, self._memory_value_usage()
                        )
                        value["unattributed"] += 1
                        unattributed_outcome_refs += 1
                        continue
                    claim_refs[counter].add(claim_id)
            if not selected_claims:
                continue
            evaluated_contexts.add(context_id)
            for claim_id in selected_claims:
                value = usage.setdefault(claim_id, self._memory_value_usage())
                value["outcomes"][result] += 1
                if claim_id in claim_refs["confirmed"]:
                    value["confirmed"] += 1
                if claim_id in claim_refs["challenged"]:
                    value["challenged"] += 1
                    value["last_challenged_at"] = max(
                        str(value.get("last_challenged_at") or ""), created_at
                    )
                self._memory_value_timeline(
                    value,
                    {
                        "context_id": context_id,
                        "outcome_id": outcome_id,
                        "stage": "outcome",
                        "result": result,
                        "occurred_at": created_at,
                    },
                )
        authority.update(
            {
                "claims_by_id": claims,
                "usage": usage,
                "acknowledged_contexts": len(acknowledged_contexts),
                "evaluated_contexts": len(evaluated_contexts),
                "unattributed_outcome_refs": unattributed_outcome_refs,
                "now": now,
            }
        )
        return authority

    def _memory_value_evidence(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        references = []
        for claim in claims:
            if claim.get("privacy") == "private":
                continue
            references.extend(_all_safe_ids(claim.get("evidence_ids")))
            references.extend(_all_safe_ids(claim.get("counter_evidence_ids")))
        unique = list(dict.fromkeys(references))
        if len(unique) > MEMORY_VALUE_REFERENCE_LIMIT:
            raise ProductDataError(
                "memory_value_unavailable", "证据引用超过安全投影上限"
            )
        states: Dict[str, str] = {}
        try:
            catalog = EvidenceCatalog(
                self.vault_dir / "index.jsonl",
                database_path=self.vault_dir / "search_index.db",
            )
        except (EvidenceCatalogError, OSError, ValueError):
            catalog = None
        for evidence_id in unique:
            if catalog is None:
                states[evidence_id] = "unavailable"
                continue
            try:
                ref = catalog.resolve(evidence_id)
                status_value = ref.get("status")
                states[evidence_id] = {
                    "available": "available",
                    "source_broken": "broken",
                    "source_deleted": "broken",
                }.get(status_value, "unavailable")
            except (EvidenceCatalogError, OSError, ValueError) as exc:
                states[evidence_id] = (
                    "missing"
                    if getattr(exc, "code", "") == "evidence_not_found"
                    else "unavailable"
                )
        result = {}
        for claim in claims:
            claim_id = str(claim.get("claim_id") or "")
            supporting = _all_safe_ids(claim.get("evidence_ids"))
            counter = _all_safe_ids(claim.get("counter_evidence_ids"))
            available = sum(states.get(item) == "available" for item in supporting)
            missing = sum(states.get(item) == "missing" for item in supporting)
            broken = sum(states.get(item) == "broken" for item in supporting)
            unavailable = sum(states.get(item) == "unavailable" for item in supporting)
            available_counter = sum(states.get(item) == "available" for item in counter)
            missing_counter = sum(states.get(item) == "missing" for item in counter)
            broken_counter = sum(states.get(item) == "broken" for item in counter)
            unavailable_counter = sum(states.get(item) == "unavailable" for item in counter)
            result[claim_id] = {
                "evidence_reference_count": len(supporting),
                "available_evidence_count": available,
                "missing_evidence_count": missing,
                "broken_evidence_count": broken,
                "unavailable_evidence_count": unavailable,
                "source_status": (
                    "unavailable"
                    if unavailable
                    else (
                        "broken_reference"
                        if broken
                        else (
                            "missing"
                            if missing or not supporting
                            else "available"
                        )
                    )
                ),
                "counter_evidence_reference_count": len(counter),
                "available_counter_evidence_count": available_counter,
                "missing_counter_evidence_count": missing_counter,
                "broken_counter_evidence_count": broken_counter,
                "unavailable_counter_evidence_count": unavailable_counter,
            }
        return result

    @classmethod
    def _memory_value_cohorts(
        cls,
        claim: Mapping[str, Any],
        usage: Mapping[str, Any],
        evidence: Mapping[str, Any],
        now: datetime,
    ) -> List[str]:
        if claim.get("privacy") == "private":
            return ["privacy_sensitive"]
        result = []
        usage_available = usage.get("attribution_status", "available") == "available"
        acknowledged = int(usage.get("acknowledged") or 0)
        outcomes = usage.get("outcomes") if isinstance(usage.get("outcomes"), Mapping) else {}
        if claim.get("status") == "candidate":
            result.append("pending_review")
        if usage_available and acknowledged == 0:
            result.append("never_acknowledged")
        if (
            usage_available
            and acknowledged >= MEMORY_VALUE_FREQUENT_ACKS
            and int(usage.get("confirmed") or 0) > 0
            and int(outcomes.get("positive") or 0) > 0
            and int(outcomes.get("mixed") or 0) == 0
            and int(outcomes.get("negative") or 0) == 0
            and int(usage.get("challenged") or 0) == 0
        ):
            result.append("frequently_helpful")
        if (
            usage_available
            and acknowledged >= MEMORY_VALUE_FREQUENT_ACKS
            and int(usage.get("challenged") or 0) > 0
        ):
            result.append("frequently_challenged")
        updated = datetime.fromisoformat(
            cls._memory_value_time(claim.get("updated_at"), required=True).replace("Z", "+00:00")
        )
        confidence = claim.get("confidence")
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and confidence >= 0.8
            and usage_available
            and acknowledged == 0
            and updated <= now - timedelta(days=MEMORY_VALUE_UNUSED_DAYS)
        ):
            result.append("high_confidence_unused")
        valid_to = claim.get("valid_to")
        if valid_to:
            expiry = datetime.fromisoformat(
                cls._memory_value_time(valid_to, required=True).replace("Z", "+00:00")
            )
            if expiry <= now + timedelta(days=MEMORY_VALUE_EXPIRING_DAYS):
                result.append("expiring")
        if _all_safe_ids(claim.get("counter_evidence_ids")):
            result.append("has_counter_evidence")
        reference_count = int(evidence.get("evidence_reference_count") or 0)
        if reference_count == 0 or (
            int(evidence.get("missing_evidence_count") or 0) == reference_count
            and int(evidence.get("broken_evidence_count") or 0) == 0
            and int(evidence.get("unavailable_evidence_count") or 0) == 0
        ):
            result.append("missing_evidence")
        if claim.get("privacy") == "restricted":
            result.append("privacy_sensitive")
        revision = claim.get("revision")
        if (
            isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision > 1
            and now - timedelta(days=MEMORY_VALUE_RECENT_DAYS) <= updated <= now
        ):
            result.append("recently_changed")
        return result

    def _memory_value_item(
        self,
        claim: Mapping[str, Any],
        usage: Mapping[str, Any],
        evidence: Mapping[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        item = self._claim_item(claim)
        if claim.get("privacy") == "private":
            item.update({"value_limited": True, "cohorts": ["privacy_sensitive"]})
            return item
        empty = self._memory_value_usage()
        current = {**empty, **dict(usage)}
        current_outcomes = current.get("outcomes")
        if not isinstance(current_outcomes, Mapping):
            current_outcomes = empty["outcomes"]
        usage_status = str(current.get("attribution_status") or "unavailable")
        signal_fields = (
            "trace_selected",
            "preview_selected",
            "compiled_snapshot_selected",
            "delivered",
            "acknowledged",
            "last_acknowledged_at",
        )
        if usage_status == "available":
            signals = {field: current.get(field) for field in signal_fields}
            outcomes = dict(current_outcomes)
            human_feedback = {
                "status": (
                    "invalid"
                    if int(current.get("unattributed") or 0)
                    else "available"
                ),
                "confirmed": int(current.get("confirmed") or 0),
                "challenged": int(current.get("challenged") or 0),
                "unattributed": int(current.get("unattributed") or 0),
                "last_challenged_at": current.get("last_challenged_at"),
            }
        else:
            signals = {
                "status": "unavailable",
                **{field: None for field in signal_fields},
            }
            outcomes = {
                name: None for name in ("positive", "mixed", "negative", "unknown")
            }
            human_feedback = {
                "status": "unavailable",
                "confirmed": None,
                "challenged": None,
                "unattributed": int(current.get("unattributed") or 0),
                "last_challenged_at": None,
            }
        item.update(
            {
                "usage_status": usage_status,
                "credibility": {
                    "confidence": claim.get("confidence"),
                    **dict(evidence),
                },
                "freshness": {
                    "valid_from": claim.get("valid_from"),
                    "valid_to": claim.get("valid_to"),
                    "last_claim_change_at": str(claim.get("updated_at") or ""),
                },
                "source_review": {
                    "status": "unavailable",
                    "last_reviewed_at": None,
                },
                "conflict": {
                    "status": (
                        "available"
                        if int(evidence.get("available_counter_evidence_count") or 0) > 0
                        else (
                            "broken_reference"
                            if int(evidence.get("broken_counter_evidence_count") or 0) > 0
                            else (
                                "missing_reference"
                                if int(evidence.get("missing_counter_evidence_count") or 0) > 0
                                else (
                                    "unavailable"
                                    if int(evidence.get("unavailable_counter_evidence_count") or 0) > 0
                                    else "none"
                                )
                            )
                        )
                    ),
                    "counter_evidence_reference_count": int(
                        evidence.get("counter_evidence_reference_count") or 0
                    ),
                    "available_counter_evidence_count": int(
                        evidence.get("available_counter_evidence_count") or 0
                    ),
                },
                "signals": signals,
                "outcomes": outcomes,
                "human_feedback": human_feedback,
                "cost": {"status": "unavailable", "estimated_chars": None},
                "timeline": list(current.get("timeline") or []),
                "timeline_total": int(current.get("timeline_total") or 0),
                "timeline_truncated": int(current.get("timeline_total") or 0)
                > MAX_PAGE_SIZE,
            }
        )
        item["cohorts"] = self._memory_value_cohorts(claim, current, evidence, now)
        return item

    def memory_value_overview(self) -> Dict[str, Any]:
        snapshot = self._memory_value_snapshot()
        claims = list(snapshot["claims_by_id"].values())
        evidence = self._memory_value_evidence(claims)
        cohort_counts = {cohort: 0 for cohort in MEMORY_VALUE_COHORTS}
        for claim in claims:
            claim_id = str(claim.get("claim_id") or "")
            cohorts = self._memory_value_cohorts(
                claim,
                snapshot["usage"].get(claim_id, self._memory_value_usage()),
                evidence.get(claim_id, {}),
                snapshot["now"],
            )
            for cohort in cohorts:
                cohort_counts[cohort] += 1
        return {
            "claim_total": len(claims),
            "acknowledged_contexts": snapshot["acknowledged_contexts"],
            "evaluated_contexts": snapshot["evaluated_contexts"],
            "unattributed_outcome_refs": snapshot["unattributed_outcome_refs"],
            "usage_status": (
                "unavailable"
                if any(
                    row.get("attribution_status") == "unavailable"
                    for row in snapshot["usage"].values()
                )
                else "available"
            ),
            "cohort_counts": cohort_counts,
            "cohort_windows_days": {
                "expiring": MEMORY_VALUE_EXPIRING_DAYS,
                "high_confidence_unused": MEMORY_VALUE_UNUSED_DAYS,
                "recently_changed": MEMORY_VALUE_RECENT_DAYS,
            },
            "frequent_acknowledgement_threshold": MEMORY_VALUE_FREQUENT_ACKS,
            "trace_scope": "persisted_selected_claim_entries_only",
        }

    def memory_value_claims(
        self, query: Optional[Mapping[str, Sequence[str]]] = None
    ) -> Dict[str, Any]:
        limit, cursor, filters = self._query(query, allowed_filters=("cohort",))
        cohort = filters.get("cohort", "")
        if cohort and cohort not in MEMORY_VALUE_COHORTS:
            raise ProductDataError("invalid_query", "记忆分析队列无效")
        snapshot = self._memory_value_snapshot()
        claims = list(snapshot["claims_by_id"].values())
        evidence = self._memory_value_evidence(claims)
        cohorts_by_id = {}
        membership_rows = []
        for claim in claims:
            claim_id = str(claim.get("claim_id") or "")
            cohorts = self._memory_value_cohorts(
                claim,
                snapshot["usage"].get(claim_id, self._memory_value_usage()),
                evidence.get(claim_id, {}),
                snapshot["now"],
            )
            cohorts_by_id[claim_id] = cohorts
            membership_rows.append(
                {
                    "claim_id": claim_id,
                    "cohorts": cohorts,
                    "evidence": evidence.get(claim_id, {}),
                }
            )
        if cohort:
            claims = [
                claim
                for claim in claims
                if cohort in cohorts_by_id.get(str(claim.get("claim_id") or ""), [])
            ]
        try:
            keyed = [
                (
                    normalize_timestamp_utc(claim.get("updated_at")),
                    str(claim.get("claim_id") or ""),
                    claim,
                )
                for claim in claims
            ]
        except IndexIntegrityError as exc:
            raise ProductDataError(
                "memory_value_unavailable", "记忆价值时间当前不可用"
            ) from exc
        keyed.sort(key=lambda value: (value[0], value[1]), reverse=True)
        generation = hashlib.sha256(
            (
                snapshot["generation"]
                + ":"
                + self._claim_generation(membership_rows)
            ).encode("utf-8")
        ).hexdigest()
        key = self._decode_cursor(
            cursor, "memory_value_claims", filters, generation, 2
        )
        if key is not None:
            if not all(isinstance(value, str) for value in key):
                raise ProductDataError("invalid_cursor", "分页游标无效")
            keyed = [
                value for value in keyed if (value[0], value[1]) < (key[0], key[1])
            ]
        visible = keyed[: limit + 1]
        has_more = len(visible) > limit
        visible = visible[:limit]
        visible_claims = [value[2] for value in visible]
        items = [
            self._memory_value_item(
                claim,
                snapshot["usage"].get(
                    str(claim.get("claim_id") or ""), self._memory_value_usage()
                ),
                evidence.get(str(claim.get("claim_id") or ""), {}),
                snapshot["now"],
            )
            for claim in visible_claims
        ]
        next_cursor = ""
        if has_more and visible:
            last = visible[-1]
            next_cursor = self._encode_cursor(
                "memory_value_claims", filters, generation, (last[0], last[1])
            )
        return {
            "items": items,
            "limit": limit,
            "total": len(claims),
            "has_more": has_more,
            "next_cursor": next_cursor,
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
        total = 0
        for section in SELF_SECTIONS:
            rows = current["sections"].get(section)
            if not isinstance(rows, list):
                raise ProductDataError(
                    "self_model_unavailable", "当前自我模型不可用"
                )
            total += sum(isinstance(row, Mapping) for row in rows)
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
            "total": total,
            "truncated": total > MAX_PAGE_SIZE,
        }

    def _claim_refs(self, claim_ids: Any) -> List[Dict[str, Any]]:
        refs = []
        for claim_id in _safe_ids(claim_ids):
            try:
                claim = self.claim_store.get(claim_id)
            except Exception as exc:
                raise ProductDataError(
                    "self_model_unavailable", "当前自我模型引用的事实不可用"
                ) from exc
            revision = claim.get("revision") if isinstance(claim, Mapping) else None
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 1
            ):
                raise ProductDataError(
                    "self_model_unavailable", "当前自我模型引用的事实不可用"
                )
            refs.append({"claim_id": claim_id, "revision": revision})
        return refs

    def self_item(self, item_id: str) -> Dict[str, Any]:
        requested = _safe_identifier(item_id, code="invalid_self_item_id")
        current = self._current_self()
        for section in SELF_SECTIONS:
            rows = current["sections"].get(section)
            if not isinstance(rows, list):
                raise ProductDataError(
                    "self_model_unavailable", "当前自我模型不可用"
                )
            for item in rows:
                if isinstance(item, Mapping) and item.get("item_id") == requested:
                    result = self._self_item_value(item, section)
                    result["claim_refs"] = self._claim_refs(
                        result["claim_ids"]
                    )
                    return result
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
        lifecycle = result["lifecycle_status"]
        if lifecycle == "preview":
            try:
                body = self.context_store.load_preview_body(
                    str(value.get("preview_id") or ""),
                    str(value.get("preview_hash") or ""),
                )
            except Exception as exc:
                raise ProductDataError(
                    "context_unavailable", "任务上下文预览当前不可用"
                ) from exc
            sections = body.get("sections") if isinstance(body, Mapping) else None
            policy = body.get("compile_policy") if isinstance(body, Mapping) else None
            if not isinstance(sections, Mapping) or not isinstance(policy, Mapping):
                raise ProductDataError(
                    "context_unavailable", "任务上下文预览当前不可用"
                )
            encoded = json.dumps(
                sections,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            copied_sections = json.loads(encoded)
            result.update(
                {
                    "preview_hash": str(value.get("preview_hash") or ""),
                    "expires_at": str(value.get("expires_at") or ""),
                    "sections": copied_sections,
                    "budget": {
                        "max_chars": policy.get("max_chars"),
                        "used_chars": len(encoded),
                        "max_bytes": policy.get("max_bytes"),
                        "used_bytes": len(encoded.encode("utf-8")),
                    },
                    "provenance": self._context_provenance(copied_sections),
                }
            )
        elif lifecycle in {"compiled", "consumed", "outcome_recorded"}:
            try:
                if lifecycle == "compiled":
                    snapshot = self.context_compiler.load_compiled(
                        str(value.get("context_id") or "")
                    )
                else:
                    snapshot = self.context_compiler.load_outcome_snapshot(
                        str(value.get("context_id") or "")
                    )
            except Exception as exc:
                raise ProductDataError(
                    "context_unavailable", "任务上下文不可变快照当前不可用"
                ) from exc
            if not isinstance(snapshot, Mapping):
                raise ProductDataError(
                    "context_unavailable", "任务上下文不可变快照当前不可用"
                )
            result.update(
                {
                    "preview_hash": snapshot.get("preview_hash"),
                    "expires_at": snapshot.get("expires_at"),
                    "content_hash": snapshot.get("content_hash"),
                    "context_markdown_hash": snapshot.get(
                        "context_markdown_hash"
                    ),
                    "context_markdown": snapshot.get("context_markdown"),
                    "sections": json.loads(
                        json.dumps(snapshot.get("sections"), ensure_ascii=False)
                    ),
                    "budget": json.loads(
                        json.dumps(snapshot.get("budget"), ensure_ascii=False)
                    ),
                    "provenance": json.loads(
                        json.dumps(snapshot.get("provenance"), ensure_ascii=False)
                    ),
                    "privacy": json.loads(
                        json.dumps(
                            snapshot.get("privacy_policy"), ensure_ascii=False
                        )
                    ),
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

    @staticmethod
    def _context_provenance(
        sections: Mapping[str, Any]
    ) -> Dict[str, List[str]]:
        items = [
            item
            for rows in sections.values()
            if isinstance(rows, list)
            for item in rows
            if isinstance(item, Mapping)
        ]
        return {
            "evidence_ids": sorted(
                {
                    item_id
                    for item in items
                    for item_id in _safe_ids(item.get("evidence_ids"))
                }
            ),
            "claim_ids": sorted(
                {
                    item_id
                    for item in items
                    for item_id in _safe_ids(item.get("claim_ids"))
                }
            ),
            "self_model_item_ids": [
                str(item.get("id") or "")
                for item in sections.get("confirmed_self_models", [])
                if isinstance(item, Mapping)
            ],
            "judgment_card_ids": [
                str(item.get("id") or "")
                for item in sections.get("judgment_cards", [])
                if isinstance(item, Mapping)
            ],
        }

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
        try:
            keyed = [
                (
                    normalize_timestamp_utc(row.get(time_field)),
                    str(row.get(id_field) or ""),
                    row,
                )
                for row in filtered
            ]
        except IndexIntegrityError as exc:
            code = {
                "self_versions": "self_model_unavailable",
                "judgments": "judgment_unavailable",
                "contexts": "context_unavailable",
            }.get(endpoint, "model_unavailable")
            raise ProductDataError(code, "权威时间信息当前不可用") from exc
        keyed.sort(key=lambda value: (value[0], value[1]), reverse=True)
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
            keyed = [
                value
                for value in keyed
                if (value[0], value[1]) < (key[0], key[1])
            ]
        visible = keyed[: limit + 1]
        has_more = len(visible) > limit
        visible = visible[:limit]
        next_cursor = ""
        if has_more and visible:
            last = visible[-1]
            next_cursor = self._encode_cursor(
                endpoint,
                filters,
                generation,
                (last[0], last[1]),
            )
        return {
            "items": [transform(value[2]) for value in visible],
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

    @staticmethod
    def _home_blocked(code: str) -> Dict[str, Any]:
        public_code = (
            code if code in HOME_SECTION_ERROR_CODES else "internal_error"
        )
        section = {"status": "blocked", "error": {"code": public_code}}
        if public_code == "index_unavailable":
            section["action"] = "rebuild_derived_index"
        return section

    def _home_now(self) -> datetime:
        try:
            now = self._clock()
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError("internal_error", "首页时间当前不可用") from exc
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ProductDataError("clock_unavailable", "系统时间不可用")
        return now

    def _home_system_health(self) -> Dict[str, Any]:
        try:
            snapshot = self.control_center.build_snapshot()
        except ProductDataError:
            raise
        except Exception as exc:
            raise ProductDataError(
                "system_unavailable", "系统健康信息当前不可用"
            ) from exc
        try:
            if not isinstance(snapshot, Mapping):
                raise TypeError("snapshot must be a mapping")
            attention = snapshot.get("attention") or []
            if not isinstance(attention, list):
                raise TypeError("attention must be a list")
            return {
                "status": str(snapshot.get("status") or "unknown"),
                "status_label": _compact_text(snapshot.get("status_label"), 80),
                "version": _compact_text(snapshot.get("version"), 40),
                "attention_count": len(attention),
            }
        except Exception as exc:
            raise ProductDataError("internal_error", "系统健康信息当前不可用") from exc

    def home(self) -> Dict[str, Any]:
        empty_changes = {
            "kind": "none",
            "from_version_id": None,
            "to_version_id": None,
            "counts": {"added": 0, "changed": 0, "removed": 0},
            "added": [],
            "changed": [],
            "removed": [],
        }
        try:
            now = self._home_now()
            local_midnight = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            remembered = self.memories(
                {
                    "from": [local_midnight.astimezone(timezone.utc).isoformat()],
                    "to": [now.astimezone(timezone.utc).isoformat()],
                    "limit": ["8"],
                }
            )["items"]
            if not isinstance(remembered, list):
                raise TypeError("memory items must be a list")
        except ProductDataError as exc:
            remembered = []
            memory_evidence = self._home_blocked(exc.code)
        except HOME_SECTION_DATA_ERRORS:
            remembered = []
            memory_evidence = self._home_blocked("internal_error")
        else:
            memory_evidence = {"status": "ready" if remembered else "empty"}

        try:
            versions = self.self_versions({"limit": ["8"]})["items"]
            if not versions:
                changes = empty_changes
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
                        **empty_changes,
                        "kind": "initial",
                        "to_version_id": latest["version_id"],
                    }
            claims = self._claims()
            judgments = self._judgment_rows()
            confirmations = [
                {
                    "kind": "claim",
                    "id": str(row.get("claim_id") or ""),
                    "summary": _compact_text(row.get("statement"), 240),
                    "status": _compact_text(row.get("status"), 40),
                    "revision": row.get("revision"),
                }
                for row in claims
                if row.get("status") == "candidate"
            ]
            candidate_claim_count = len(confirmations)
            confirmations.extend(
                {
                    "kind": "judgment",
                    "id": str(row.get("card_id") or ""),
                    "summary": _compact_text(row.get("title"), 240),
                    "status": _compact_text(row.get("status"), 40),
                    "revision": row.get("revision"),
                }
                for row in judgments
                if row.get("status") == "candidate"
            )
            candidate_judgment_count = len(confirmations) - candidate_claim_count
        except ProductDataError as exc:
            changes = empty_changes
            confirmations = []
            candidate_claim_count = 0
            candidate_judgment_count = 0
            claim_review = self._home_blocked(exc.code)
        except HOME_SECTION_DATA_ERRORS:
            changes = empty_changes
            confirmations = []
            candidate_claim_count = 0
            candidate_judgment_count = 0
            claim_review = self._home_blocked("internal_error")
        else:
            claim_review = {
                "status": "attention"
                if confirmations
                else ("ready" if versions else "empty")
            }

        try:
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
        except ProductDataError as exc:
            latest_context = None
            latest_outcome = None
            agent_use = self._home_blocked(exc.code)
        except HOME_SECTION_DATA_ERRORS:
            latest_context = None
            latest_outcome = None
            agent_use = self._home_blocked("internal_error")
        else:
            agent_use = {
                "status": "ready"
                if latest_context is not None or latest_outcome is not None
                else "empty"
            }

        try:
            system_health = self._home_system_health()
        except ProductDataError as exc:
            system_health = {
                "status": "unknown",
                "status_label": "系统健康信息当前不可用",
                "version": "",
                "attention_count": 0,
            }
            system = self._home_blocked(exc.code)
        else:
            system = {
                "status": "ready"
                if system_health["status"] == "healthy"
                else "attention"
            }

        return {
            "remembered_today": remembered,
            "understanding_changes": changes,
            "needs_confirmation": confirmations[:8],
            "latest_context_use": latest_context,
            "latest_outcome": latest_outcome,
            "system_health": system_health,
            "confirmation_summary": {
                "total": len(confirmations),
                "claims": candidate_claim_count,
                "judgments": candidate_judgment_count,
                "visible": min(len(confirmations), 8),
            },
            "memory_evidence": memory_evidence,
            "claim_review": claim_review,
            "agent_use": agent_use,
            "system": system,
        }

    def trust(self) -> Dict[str, Any]:
        claims = self._claims()
        judgments = self._judgment_rows()
        contexts = self._context_rows()
        outcomes = self._outcomes()
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
            "failed_outcome": "complete",
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
            reasons = privacy.get("reasons") if isinstance(privacy.get("reasons"), list) else []
            if count and "private" in reasons:
                add(
                    "privacy_exclusion",
                    str(row.get("context_id") or row.get("preview_id") or ""),
                    "上下文包含按隐私策略排除的项目",
                    "info",
                )
        for outcome in outcomes:
            summary = _compact_text(outcome.get("summary"), 180) or "任务结果对这条记忆提出了挑战"
            for ref in outcome.get("challenged_refs") or []:
                if not isinstance(ref, Mapping):
                    continue
                add(
                    "failed_outcome",
                    str(ref.get("id") or ""),
                    "%s；%s %s（版本 %s）"
                    % (
                        summary,
                        str(ref.get("kind") or "memory"),
                        str(ref.get("id") or ""),
                        str(ref.get("revision") or "未知"),
                    ),
                    "attention",
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
                "candidate_claims": candidate_claims,
                "candidate_judgments": candidate_judgments,
                "low_confidence": categories["low_confidence"]["count"],
                "privacy_exclusions": categories["privacy_exclusion"]["count"],
                "challenged_memories": categories["failed_outcome"]["count"],
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
