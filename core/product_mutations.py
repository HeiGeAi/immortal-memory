#!/usr/bin/env python3
"""Fail-closed coordinator for bounded product mutations.

The coordinator ledger is intentionally not a business state store.  It keeps
only digests and safe identifiers while native event-store idempotency remains
the authority for every domain mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from claim_store import ClaimStore
from context_compiler import ContextCompiler
from event_store import (
    EventPathError,
    _anchored_parent,
    _exclusive_lock,
    _regular_stat_at,
    safe_atomic_write_text,
    safe_read_text,
)
from judgment_store import JudgmentStore
from living_self_service import LivingSelfConflict, LivingSelfService
from outcome_store import OutcomeStore
from product_data import _compact_text
from redact_common import redact


LEDGER_SCHEMA = 1
MAX_LEDGER_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 512
MAX_AUDIT = 1024
MAX_PUBLIC_MULTILINE_CHARS = 256 * 1024
ID_RE = re.compile(r"\A[A-Za-z0-9._:@+-]{1,180}\Z")
HASH_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
ACTOR = {"kind": "owner", "id": "local-owner"}
SAFE_DOMAIN_CODES = frozenset({
    "card_id_required", "claim_event_corruption", "claim_id_required",
    "compile_commit_failed", "context_budget_too_small", "context_not_ready",
    "context_budget_exceeded", "context_expired", "context_not_found",
    "custom_scope_id_required", "derived_store_invalid", "derived_store_limit",
    "evidence_not_found", "idempotency_conflict", "invalid_actor",
    "evidence_limit", "future_timestamp", "index_unavailable", "invalid_argument",
    "invalid_claim", "invalid_clock", "invalid_compile_policy",
    "invalid_context_budget", "invalid_context_event", "invalid_context_mode", "invalid_context_sections", "invalid_correction",
    "invalid_event_id", "invalid_expected_version", "invalid_identifier",
    "invalid_idempotency_key", "invalid_limit", "invalid_migration_source",
    "invalid_outcome_link", "invalid_pack_snapshot", "invalid_preview_body",
    "invalid_privacy_policy", "invalid_request_id", "invalid_scope", "invalid_selection",
    "invalid_source_revision", "invalid_summary", "invalid_timestamp", "invalid_ttl", "invalid_typed_refs",
    "invalid_request", "invalid_transition", "judgment_not_found",
    "judgment_event_corruption", "living_self_unavailable",
    "new_evidence_required", "outcome_conflict", "private_content_blocked",
    "outcome_event_corruption", "outcome_link_conflict", "outcome_link_missing",
    "outcome_not_found", "outcome_ref_not_in_context", "outcome_uncommitted",
    "pack_publish_failed", "pack_write_failed", "preview_unavailable",
    "resolved_mode_conflict", "scope_mismatch", "self_item_not_found", "source_changed",
    "stale_context", "stale_preview", "statement_required", "task_required",
    "unresolved_context_mode", "version_conflict", "write_metadata_required",
})
SAFE_LEDGER_ERROR_CODES = SAFE_DOMAIN_CODES | frozenset({
    "derived_update_pending", "invalid_json", "mutation_authority_unavailable",
    "mutation_failed", "not_found",
})


class MutationError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _strict_json(text: str) -> Any:
    def unique_object(pairs: Sequence[Any]) -> Dict[str, Any]:
        value: Dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def reject_constant(_value: str) -> Any:
        raise ValueError("non-standard JSON constant")

    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _public_multiline_text(value: Any) -> str:
    if not isinstance(value, str):
        raise MutationError("mutation_failed", "mutation response is invalid")
    text = redact(value)
    text = re.sub(
        r"-{5}BEGIN ([A-Z0-9 ]*PRIVATE KEY)-{5}.*?"
        r"-{5}END \1-{5}",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?i)(https?://)[^/@\s|]+:[^@\s|]+@",
        r"\1[REDACTED]@",
        text,
    )
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:Cookie|Set-Cookie)\s*:\s*[^\r\n|]+",
        "Cookie: [REDACTED]",
        text,
    )
    text = re.sub(r"\bou_[A-Za-z0-9_-]{8,}\b", "ou_[REDACTED]", text)
    text = re.sub(
        r"(?<![A-Za-z0-9])/(?:Users|home)/[^\s|]+",
        "/[HOME]/[REDACTED]",
        text,
    )
    if len(text) > MAX_PUBLIC_MULTILINE_CHARS:
        return text[: MAX_PUBLIC_MULTILINE_CHARS - 3] + "..."
    return text


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise MutationError("invalid_request", field + " is invalid")
    return value


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MutationError("invalid_request", field + " must be a non-negative integer")
    return value


def _text(value: Any, field: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MutationError("invalid_request", field + " is invalid")
    return value.strip()


def _fields(body: Mapping[str, Any], allowed: Sequence[str], required: Sequence[str]) -> None:
    if not isinstance(body, Mapping):
        raise MutationError("invalid_json", "JSON body must be an object")
    unknown = set(body) - set(allowed)
    missing = set(required) - set(body)
    if unknown or missing:
        raise MutationError("invalid_request", "request fields do not match the route contract")


class ProductMutationCoordinator:
    """Serialize prepare, native commit or recovery, and ledger completion."""

    def __init__(
        self,
        vault_dir: Path,
        *,
        claims: Optional[Any] = None,
        living_self: Optional[Any] = None,
        judgments: Optional[Any] = None,
        compiler: Optional[Any] = None,
        outcomes: Optional[Any] = None,
        lock_timeout: float = 10.0,
    ) -> None:
        self.vault_dir = Path(os.path.abspath(str(vault_dir)))
        self.ledger_path = self.vault_dir / "runtime" / "idempotency.json"
        self.lock_path = self.vault_dir / "runtime" / ".idempotency.lock"
        self.claims = claims or ClaimStore(self.vault_dir)
        self.living_self = living_self or LivingSelfService(self.vault_dir)
        self.judgments = judgments or JudgmentStore(self.vault_dir)
        self.compiler = compiler or ContextCompiler(self.vault_dir)
        self.outcomes = outcomes or OutcomeStore(
            self.vault_dir, context_compiler=self.compiler
        )
        self.lock_timeout = lock_timeout
        self._local_lock = threading.RLock()

    @staticmethod
    def _assert_safe_mode(path: Path, *, allow_missing: bool) -> None:
        try:
            with _anchored_parent(path, create=allow_missing) as (parent_fd, name):
                metadata = _regular_stat_at(parent_fd, name)
        except (FileNotFoundError, EventPathError) as exc:
            if allow_missing and isinstance(exc, FileNotFoundError):
                return
            raise MutationError(
                "mutation_authority_unavailable",
                "mutation authority path is unsafe",
                retryable=False,
            ) from exc
        if metadata is None:
            if allow_missing:
                return
            raise MutationError(
                "mutation_authority_unavailable", "mutation authority is missing"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise MutationError(
                "mutation_authority_unavailable",
                "mutation authority permissions are unsafe",
            )

    @contextmanager
    def _locked(self):
        with self._local_lock:
            self._assert_safe_mode(self.lock_path, allow_missing=True)
            try:
                with _exclusive_lock(
                    self.lock_path,
                    timeout=self.lock_timeout,
                    stale_after=60.0,
                ):
                    self._assert_safe_mode(self.lock_path, allow_missing=False)
                    self._assert_safe_mode(self.ledger_path, allow_missing=True)
                    yield
            except MutationError:
                raise
            except (EventPathError, TimeoutError, OSError) as exc:
                raise MutationError(
                    "mutation_authority_unavailable",
                    "mutation authority is unavailable",
                    retryable=True,
                ) from exc

    def _read_ledger(self) -> Dict[str, Any]:
        try:
            text = safe_read_text(self.ledger_path)
        except (EventPathError, OSError) as exc:
            raise MutationError(
                "mutation_authority_unavailable", "mutation ledger cannot be read"
            ) from exc
        if text is None:
            return {"schema_version": LEDGER_SCHEMA, "entries": [], "audit": []}
        if len(text.encode("utf-8")) > MAX_LEDGER_BYTES:
            raise MutationError("mutation_authority_unavailable", "mutation ledger is too large")
        try:
            value = _strict_json(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "entries", "audit"}
            or value.get("schema_version") != LEDGER_SCHEMA
            or not isinstance(value.get("entries"), list)
            or not isinstance(value.get("audit"), list)
            or len(value["entries"]) > MAX_ENTRIES
            or len(value["audit"]) > MAX_AUDIT
        ):
            raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt")
        entry_fields = {
            "key_digest", "intent_digest", "route", "action", "target",
            "preallocated", "status", "result", "error_code", "prepared_at",
            "completed_at",
        }
        audit_fields = {
            "request_id_digest", "route", "action", "target", "status",
            "error_code", "at",
        }
        for row in value["entries"]:
            if (
                not isinstance(row, dict)
                or set(row) != entry_fields
                or HASH_RE.fullmatch(str(row.get("key_digest") or "")) is None
                or HASH_RE.fullmatch(str(row.get("intent_digest") or "")) is None
                or row.get("status") not in {"pending", "completed", "failed"}
                or not isinstance(row.get("preallocated"), dict)
                or not isinstance(row.get("result"), dict)
                or not all(isinstance(row.get(key), str) for key in ("route", "action", "target"))
                or not _valid_time(row.get("prepared_at"))
                or (row.get("completed_at") is not None and not _valid_time(row.get("completed_at")))
                or (row.get("error_code") is not None and row.get("error_code") not in SAFE_LEDGER_ERROR_CODES)
            ):
                raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt")
            try:
                action, target = self._route_metadata(
                    row["route"], {"action": row["action"]}
                )
            except MutationError as exc:
                raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt") from exc
            expected_preallocated = set()
            if row["route"].startswith("/api/v2/self/"):
                expected_preallocated = {"version_id"}
            elif row["route"] == "/api/v2/judgments":
                expected_preallocated = {"card_id"}
            if action != row["action"] or target != row["target"] or set(row["preallocated"]) != expected_preallocated:
                raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt")
            for key, identifier in row["preallocated"].items():
                prefix = "lsv_" if key == "version_id" else "jdg_"
                if not isinstance(identifier, str) or not re.fullmatch(prefix + r"[0-9a-f]{32}", identifier):
                    raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt")
            status = row["status"]
            error_code = row["error_code"]
            completed_at = row["completed_at"]
            result = row["result"]
            if status == "pending":
                valid_state = (
                    completed_at is None
                    and (
                        (error_code is None and result == {})
                        or (
                            error_code == "derived_update_pending"
                            and result != {}
                        )
                    )
                )
            elif status == "completed":
                valid_state = completed_at is not None and error_code is None
            else:
                valid_state = (
                    completed_at is not None
                    and error_code not in {None, "derived_update_pending"}
                    and result == {}
                )
            if not valid_state:
                raise MutationError(
                    "mutation_authority_unavailable", "mutation ledger is corrupt"
                )
            if result:
                try:
                    safe_result = self._safe_result(result, route=row["route"])
                except MutationError as exc:
                    raise MutationError(
                        "mutation_authority_unavailable", "mutation ledger is corrupt"
                    ) from exc
                if status == "pending" and (
                    safe_result.get("derived_update_pending") is not True
                    or safe_result.get("error_code") != "derived_update_pending"
                ):
                    raise MutationError(
                        "mutation_authority_unavailable", "mutation ledger is corrupt"
                    )
                if (
                    status == "completed"
                    and safe_result.get("derived_update_pending") is True
                ):
                    raise MutationError(
                        "mutation_authority_unavailable", "mutation ledger is corrupt"
                    )
            elif status == "completed":
                raise MutationError(
                    "mutation_authority_unavailable", "mutation ledger is corrupt"
                )
        for row in value["audit"]:
            if (
                not isinstance(row, dict)
                or set(row) != audit_fields
                or HASH_RE.fullmatch(str(row.get("request_id_digest") or "")) is None
                or row.get("status") not in {"pending", "completed", "failed"}
                or not all(isinstance(row.get(key), str) for key in ("route", "action", "target"))
                or not _valid_time(row.get("at"))
                or (row.get("error_code") is not None and row.get("error_code") not in SAFE_LEDGER_ERROR_CODES)
            ):
                raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt")
            try:
                action, target = self._route_metadata(
                    row["route"], {"action": row["action"]}
                )
            except MutationError as exc:
                raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt") from exc
            if action != row["action"] or target != row["target"]:
                raise MutationError("mutation_authority_unavailable", "mutation ledger is corrupt")
            if (
                (row["status"] == "pending" and row["error_code"] != "derived_update_pending")
                or (row["status"] == "completed" and row["error_code"] is not None)
                or (
                    row["status"] == "failed"
                    and row["error_code"] in {None, "derived_update_pending"}
                )
            ):
                raise MutationError(
                    "mutation_authority_unavailable", "mutation ledger is corrupt"
                )
        return value

    def _write_ledger(self, ledger: Dict[str, Any]) -> None:
        pending = [row for row in ledger["entries"] if row.get("status") == "pending"]
        completed = [row for row in ledger["entries"] if row.get("status") != "pending"]
        if len(pending) > MAX_ENTRIES:
            raise MutationError("mutation_authority_unavailable", "too many pending mutations")
        slots = MAX_ENTRIES - len(pending)
        retained = completed[-slots:] if slots > 0 else []
        ledger["entries"] = pending + retained
        ledger["audit"] = ledger["audit"][-MAX_AUDIT:]
        try:
            safe_atomic_write_text(
                self.ledger_path,
                json.dumps(
                    ledger, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ) + "\n",
            )
            self._assert_safe_mode(self.ledger_path, allow_missing=False)
        except (EventPathError, OSError) as exc:
            raise MutationError(
                "mutation_authority_unavailable", "mutation ledger cannot be written"
            ) from exc

    @staticmethod
    def _preallocated(route: str) -> Dict[str, str]:
        if route.startswith("/api/v2/self/"):
            return {"version_id": "lsv_" + uuid.uuid4().hex}
        if route == "/api/v2/judgments":
            return {"card_id": "jdg_" + uuid.uuid4().hex}
        return {}

    @staticmethod
    def _route_metadata(route: str, body: Mapping[str, Any]) -> Any:
        if not isinstance(body, Mapping):
            raise MutationError("invalid_json", "JSON body must be an object")
        if route == "/api/v2/judgments":
            return "create", "collection"
        if route == "/api/v2/contexts/preview":
            return "preview", "collection"
        if route == "/api/v2/contexts":
            return "compile", "collection"
        routes = (
            (r"/api/v2/self/items/([A-Za-z0-9._:@+-]{1,180})/actions", {"correct"}, "self_action"),
            (r"/api/v2/self/versions/([A-Za-z0-9._:@+-]{1,180})/restore", None, "restore"),
            (r"/api/v2/judgments/([A-Za-z0-9._:@+-]{1,180})/actions", {"confirm", "reject", "correct", "record_outcome", "retire"}, "judgment_action"),
            (r"/api/v2/contexts/([A-Za-z0-9._:@+-]{1,180})/consume", None, "consume"),
            (r"/api/v2/contexts/([A-Za-z0-9._:@+-]{1,180})/outcomes", None, "outcome"),
        )
        for pattern, actions, default_action in routes:
            match = re.fullmatch(pattern, route)
            if match is None:
                continue
            action = default_action if actions is None else body.get("action")
            if actions is not None and action not in actions:
                raise MutationError("invalid_transition", "mutation action is not supported")
            return str(action), match.group(1)
        raise MutationError("not_found", "mutation route was not found")

    def mutate(
        self,
        route: str,
        body: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        request = _identifier(request_id, "request_id")
        key = _identifier(idempotency_key, "idempotency_key")
        action, safe_target = self._route_metadata(route, body)
        intent = {"route": route, "body": body}
        try:
            intent_digest = _digest(intent)
        except (TypeError, ValueError) as exc:
            raise MutationError("invalid_json", "request body is not canonical JSON") from exc
        key_digest = _digest({"key": key})
        with self._locked():
            ledger = self._read_ledger()
            prior = next(
                (row for row in ledger["entries"] if row.get("key_digest") == key_digest),
                None,
            )
            if prior is not None and prior.get("intent_digest") != intent_digest:
                raise MutationError(
                    "idempotency_conflict",
                    "idempotency key was reused for a different request",
                )
            if prior is not None and prior.get("status") == "completed":
                replay = self._dispatch(
                    route,
                    body,
                    request_id=request,
                    idempotency_key=key,
                    preallocated=dict(prior.get("preallocated") or {}),
                    recovering=True,
                )
                if self._safe_result(replay, route=route) != prior.get("result"):
                    raise MutationError(
                        "mutation_authority_unavailable",
                        "domain replay differs from the completed mutation authority",
                    )
                return self._public_result(replay)
            if prior is None:
                prior = {
                    "key_digest": key_digest,
                    "intent_digest": intent_digest,
                    "route": route,
                    "action": action,
                    "target": safe_target,
                    "preallocated": self._preallocated(route),
                    "status": "pending",
                    "result": {},
                    "error_code": None,
                    "prepared_at": _now(),
                    "completed_at": None,
                }
                ledger["entries"].append(prior)
                self._write_ledger(ledger)
            try:
                result = self._dispatch(
                    route,
                    body,
                    request_id=request,
                    idempotency_key=key,
                    preallocated=dict(prior.get("preallocated") or {}),
                    recovering=False,
                )
                public = self._public_result(result)
                safe = self._safe_result(result, route=route)
                if safe.get("derived_update_pending") is True:
                    prior.update(
                        status="pending", result=safe,
                        error_code="derived_update_pending", completed_at=None,
                    )
                    ledger["audit"].append({
                        "request_id_digest": _digest({"request_id": request}),
                        "route": route, "action": prior["action"],
                        "target": prior["target"], "status": "pending",
                        "error_code": "derived_update_pending", "at": _now(),
                    })
                    self._write_ledger(ledger)
                    return public
                prior.update(status="completed", result=safe, error_code=None, completed_at=_now())
                ledger["audit"].append(
                    {
                        "request_id_digest": _digest({"request_id": request}),
                        "route": route,
                        "action": prior["action"],
                        "target": prior["target"],
                        "status": "completed",
                        "error_code": None,
                        "at": prior["completed_at"],
                    }
                )
                self._write_ledger(ledger)
                return public
            except MutationError as exc:
                code = exc.code if exc.code in SAFE_LEDGER_ERROR_CODES else "mutation_failed"
                prior.update(status="failed", result={}, error_code=code, completed_at=_now())
                ledger["audit"].append(
                    {
                        "request_id_digest": _digest({"request_id": request}),
                        "route": route,
                        "action": prior["action"],
                        "target": prior["target"],
                        "status": "failed",
                        "error_code": code,
                        "at": prior["completed_at"],
                    }
                )
                self._write_ledger(ledger)
                if code != exc.code:
                    raise MutationError(
                        "mutation_failed", "mutation was rejected", retryable=True
                    ) from exc
                raise
            except Exception as exc:
                raw_code = getattr(exc, "code", None)
                if isinstance(raw_code, str):
                    code = raw_code if raw_code in SAFE_DOMAIN_CODES else "mutation_failed"
                    mapped = MutationError(code, "mutation was rejected by domain authority")
                    prior.update(status="failed", result={}, error_code=code, completed_at=_now())
                    self._write_ledger(ledger)
                    raise mapped from exc
                raise

    @staticmethod
    def _safe_target(route: str) -> str:
        parts = route.split("/")
        for marker in ("items", "versions", "judgments", "contexts"):
            if marker in parts:
                index = parts.index(marker) + 1
                if index < len(parts) and parts[index] not in {"preview", ""}:
                    return parts[index][:180]
        return "collection"

    @staticmethod
    def _safe_result(
        result: Mapping[str, Any], *, route: Optional[str] = None
    ) -> Dict[str, Any]:
        allowed = {
            "claim_id", "card_id", "context_id", "preview_id", "outcome_id",
            "version_id", "revision", "stream_version", "status",
            "lifecycle_status", "availability_status", "derived_update_pending",
            "derived_version_id", "error_code",
        }
        if not isinstance(result, Mapping):
            raise MutationError("mutation_failed", "domain result is invalid")
        safe = {
            key: value
            for key, value in result.items()
            if key in allowed and value is not None
        }
        required = set()
        if route is not None:
            if route.startswith("/api/v2/self/items/"):
                required = {"claim_id", "revision", "derived_update_pending", "derived_version_id"}
            elif route.startswith("/api/v2/self/versions/"):
                required = {"version_id", "status"}
            elif route == "/api/v2/judgments" or route.startswith("/api/v2/judgments/"):
                required = {"card_id", "revision", "status"}
            elif route == "/api/v2/contexts/preview":
                required = {"preview_id", "revision", "lifecycle_status"}
            elif route == "/api/v2/contexts":
                required = {"context_id", "lifecycle_status"}
            elif route.endswith("/consume"):
                required = {"context_id", "lifecycle_status"}
            elif route.endswith("/outcomes"):
                required = {"outcome_id"}
        if not required.issubset(safe):
            raise MutationError("mutation_failed", "domain result is incomplete")
        for key, value in safe.items():
            if key in {"revision", "stream_version"}:
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise MutationError("mutation_failed", "domain result is invalid")
            elif key == "derived_update_pending":
                if not isinstance(value, bool):
                    raise MutationError("mutation_failed", "domain result is invalid")
            elif key == "error_code":
                if value not in {None, "derived_update_pending"}:
                    raise MutationError("mutation_failed", "domain result is invalid")
            elif not isinstance(value, str) or ID_RE.fullmatch(value) is None:
                raise MutationError("mutation_failed", "domain result is invalid")
        return safe

    @classmethod
    def _public_result(cls, result: Any, depth: int = 0) -> Any:
        if depth > 10:
            return None
        if isinstance(result, Mapping):
            public = {}
            forbidden = {
                "args", "argv", "command", "commands", "cookie", "cookies",
                "cwd", "debug", "headers", "path", "paths", "stderr", "stdout",
                "token", "tokens", "secret", "secrets", "password", "api_key",
            }
            for key, value in list(result.items())[:500]:
                name = str(key)
                lowered = name.casefold()
                if (
                    lowered in forbidden
                    or lowered in {"context_json", "context_md"}
                    or lowered.endswith(("_path", "_command", "_args", "_token", "_cookie"))
                ):
                    continue
                if lowered == "context_markdown":
                    public[name] = _public_multiline_text(value)
                else:
                    public[name] = cls._public_result(value, depth + 1)
            return public
        if isinstance(result, list):
            return [cls._public_result(value, depth + 1) for value in result[:500]]
        if isinstance(result, str):
            return _compact_text(result, MAX_LEDGER_BYTES // 4)
        if isinstance(result, float) and not math.isfinite(result):
            raise MutationError("mutation_failed", "domain result is invalid")
        if result is None or isinstance(result, (bool, int, float)):
            return result
        return None

    def _dispatch(self, route: str, body: Mapping[str, Any], **metadata: Any) -> Dict[str, Any]:
        if route.startswith("/api/v2/self/items/") and route.endswith("/actions"):
            item_id = route[len("/api/v2/self/items/") : -len("/actions")]
            return self._self_action(item_id, body, **metadata)
        if route.startswith("/api/v2/self/versions/") and route.endswith("/restore"):
            version_id = route[len("/api/v2/self/versions/") : -len("/restore")]
            return self._self_restore(version_id, body, **metadata)
        if route == "/api/v2/judgments":
            return self._judgment_create(body, **metadata)
        if route.startswith("/api/v2/judgments/") and route.endswith("/actions"):
            card_id = route[len("/api/v2/judgments/") : -len("/actions")]
            return self._judgment_action(card_id, body, **metadata)
        if route == "/api/v2/contexts/preview":
            return self._context_preview(body, **metadata)
        if route == "/api/v2/contexts":
            return self._context_compile(body, **metadata)
        if route.startswith("/api/v2/contexts/") and route.endswith("/consume"):
            context_id = route[len("/api/v2/contexts/") : -len("/consume")]
            return self._context_consume(context_id, body, **metadata)
        if route.startswith("/api/v2/contexts/") and route.endswith("/outcomes"):
            context_id = route[len("/api/v2/contexts/") : -len("/outcomes")]
            return self._context_outcome(context_id, body, **metadata)
        raise MutationError("not_found", "mutation route was not found")

    def _self_item(self, version: Mapping[str, Any], item_id: str) -> Mapping[str, Any]:
        for items in version.get("sections", {}).values():
            for item in items:
                if item.get("item_id") == item_id:
                    return item
        raise MutationError("self_item_not_found", "Living Self item was not found")

    def _self_action(self, item_id: str, body: Mapping[str, Any], **meta: Any) -> Dict[str, Any]:
        allowed = {
            "action", "claim_id", "expected_self_version", "expected_version",
            "reason", "statement",
        }
        _fields(body, allowed, ("action", "claim_id", "expected_self_version", "expected_version", "reason", "statement"))
        action = body["action"]
        if action != "correct":
            raise MutationError(
                "invalid_transition",
                "confirmed Living Self items only support correction",
            )
        claim_id = _identifier(body["claim_id"], "claim_id")
        current = self.living_self.current()
        expected_self = _identifier(body["expected_self_version"], "expected_self_version")
        result_id = meta["preallocated"].get("version_id")
        if current.get("version_id") not in {expected_self, result_id}:
            raise MutationError("version_conflict", "Living Self version changed")
        snapshot = (
            current
            if current.get("version_id") == expected_self
            else self.living_self.load_version(expected_self)
        )
        item = self._self_item(snapshot, _identifier(item_id, "item_id"))
        if claim_id not in item.get("claim_ids", []):
            raise MutationError("scope_mismatch", "Claim does not belong to this Living Self item")
        revision = _integer(body["expected_version"], "expected_version")
        reason = _text(body["reason"], "reason", maximum=500)
        native = {
            "expected_revision": revision,
            "request_id": meta["request_id"],
            "idempotency_key": meta["idempotency_key"],
            "actor": ACTOR,
            "reason": reason,
        }
        claim = self.claims.correct(
            claim_id,
            _text(body["statement"], "statement", maximum=8000),
            **native,
        )
        try:
            derived = self.living_self.materialize_claim_change(
                reason="claim mutation " + action,
                result_version_id=result_id,
                expected_parent_version_id=expected_self,
            )
        except LivingSelfConflict as exc:
            raise MutationError(exc.code, "Living Self materialization conflicted") from exc
        except EventPathError as exc:
            raise MutationError(
                "mutation_authority_unavailable",
                "Living Self authority path is unsafe",
            ) from exc
        except ValueError as exc:
            raise MutationError(
                "mutation_authority_unavailable",
                "Living Self authority is invalid",
            ) from exc
        except (OSError, RuntimeError):
            return {
                "claim_id": claim["claim_id"],
                "revision": claim.get("revision"),
                "derived_update_pending": True,
                "derived_version_id": result_id,
                "error_code": "derived_update_pending",
            }
        return {
            "claim_id": claim["claim_id"],
            "revision": claim.get("revision"),
            "derived_update_pending": False,
            "derived_version_id": derived["version_id"],
        }

    def _self_restore(self, version_id: str, body: Mapping[str, Any], **meta: Any) -> Dict[str, Any]:
        _fields(body, ("expected_version", "reason"), ("expected_version", "reason"))
        expected = _identifier(body["expected_version"], "expected_version")
        restored = self.living_self.restore(
            _identifier(version_id, "version_id"),
            reason=_text(body["reason"], "reason", maximum=500),
            result_version_id=meta["preallocated"].get("version_id"),
            expected_parent_version_id=expected,
        )
        return {"version_id": restored["version_id"], "status": restored["status"]}

    def _judgment_create(self, body: Mapping[str, Any], **meta: Any) -> Dict[str, Any]:
        allowed = {
            "title", "situation", "decision", "evidence_ids", "goal", "constraints",
            "signals", "alternatives", "claim_ids", "lesson", "next_trigger", "privacy",
            "expected_version", "reason",
        }
        _fields(body, allowed, ("title", "situation", "decision", "evidence_ids", "expected_version", "reason"))
        expected = _integer(body["expected_version"], "expected_version")
        kwargs = {key: value for key, value in body.items() if key not in {"expected_version", "reason"}}
        card = self.judgments.create(
            **kwargs,
            card_id=meta["preallocated"].get("card_id"),
            expected_revision=expected,
            request_id=meta["request_id"],
            idempotency_key=meta["idempotency_key"],
            actor=ACTOR,
            reason=_text(body["reason"], "reason", maximum=500),
        )
        return card

    def _judgment_action(self, card_id: str, body: Mapping[str, Any], **meta: Any) -> Dict[str, Any]:
        allowed = {"action", "expected_version", "reason", "changes", "status", "summary", "observed_at"}
        _fields(body, allowed, ("action", "expected_version", "reason"))
        action = body["action"]
        if action not in {"confirm", "reject", "correct", "record_outcome", "retire"}:
            raise MutationError("invalid_transition", "judgment action is not supported")
        common = {
            "expected_revision": _integer(body["expected_version"], "expected_version"),
            "request_id": meta["request_id"], "idempotency_key": meta["idempotency_key"],
            "actor": ACTOR, "reason": _text(body["reason"], "reason", maximum=500),
        }
        card = _identifier(card_id, "card_id")
        if action in {"confirm", "reject", "retire"}:
            return self.judgments.transition(card, {"confirm": "confirmed", "reject": "rejected", "retire": "retired"}[action], **common)
        if action == "correct":
            return self.judgments.correct(card, changes=body.get("changes"), **common)
        return self.judgments.record_outcome(
            card, status=body.get("status"), summary=body.get("summary"),
            observed_at=body.get("observed_at"), **common
        )

    def _context_preview(self, body: Mapping[str, Any], **meta: Any) -> Dict[str, Any]:
        allowed = {"task", "mode", "role_scope", "domain_scope", "custom_scope_ids", "max_chars", "max_bytes", "ttl_seconds", "expected_version", "reason"}
        _fields(body, allowed, ("task", "expected_version", "reason"))
        if _integer(body["expected_version"], "expected_version") != 0:
            raise MutationError("version_conflict", "preview creation requires expected version 0")
        kwargs = {key: value for key, value in body.items() if key not in {"expected_version", "reason"}}
        return self.compiler.preview(
            **kwargs, request_id=meta["request_id"], idempotency_key=meta["idempotency_key"],
            actor=ACTOR, reason=_text(body["reason"], "reason", maximum=500)
        )

    def _context_compile(self, body: Mapping[str, Any], **meta: Any) -> Dict[str, Any]:
        allowed = {"preview_id", "preview_hash", "excluded_item_ids", "resolved_mode", "expected_version", "reason"}
        _fields(body, allowed, ("preview_id", "preview_hash", "excluded_item_ids", "expected_version", "reason"))
        if _integer(body["expected_version"], "expected_version") != 1:
            raise MutationError("version_conflict", "context compilation requires expected version 1")
        return self.compiler.compile(
            preview_id=body["preview_id"], preview_hash=body["preview_hash"],
            excluded_item_ids=body["excluded_item_ids"], resolved_mode=body.get("resolved_mode"),
            request_id=meta["request_id"], idempotency_key=meta["idempotency_key"],
            actor=ACTOR, reason=_text(body["reason"], "reason", maximum=500)
        )

    def _context_consume(self, context_id: str, body: Mapping[str, Any], **meta: Any) -> Dict[str, Any]:
        _fields(body, ("expected_version", "reason"), ("expected_version", "reason"))
        return self.outcomes.consume(
            _identifier(context_id, "context_id"),
            expected_version=_integer(body["expected_version"], "expected_version"),
            request_id=meta["request_id"], idempotency_key=meta["idempotency_key"],
            actor=ACTOR, reason=_text(body["reason"], "reason", maximum=500)
        )

    def _context_outcome(self, context_id: str, body: Mapping[str, Any], **meta: Any) -> Dict[str, Any]:
        allowed = {"adopted", "result", "summary", "confirmed_refs", "challenged_refs", "expected_version", "reason"}
        _fields(body, allowed, ("adopted", "result", "summary", "expected_version", "reason"))
        return self.outcomes.record_outcome(
            _identifier(context_id, "context_id"), adopted=body["adopted"], result=body["result"],
            summary=body["summary"], confirmed_refs=body.get("confirmed_refs"),
            challenged_refs=body.get("challenged_refs"),
            expected_version=_integer(body["expected_version"], "expected_version"),
            request_id=meta["request_id"], idempotency_key=meta["idempotency_key"],
            actor=ACTOR, reason=_text(body["reason"], "reason", maximum=500)
        )


def sanitize_public_mutation_result(result: Mapping[str, Any]) -> Dict[str, Any]:
    value = ProductMutationCoordinator._public_result(result)
    if not isinstance(value, dict):
        raise MutationError("mutation_failed", "mutation response is invalid")
    return value
