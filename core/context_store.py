#!/usr/bin/env python3
"""Event-backed lifecycle storage for Context preview metadata."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from event_store import (
    EventConflict,
    EventCorruption,
    EventPathError,
    JsonlEventStore,
    safe_atomic_write_text,
    safe_read_text,
)
from model_types import (
    ACTOR_KINDS,
    CONTEXT_DEFAULT_MAX_BYTES,
    CONTEXT_MODES,
    CONTEXT_SECTIONS,
    new_event,
)


SOURCE_REVISION_FIELDS = {
    "claims_event_seq",
    "living_self_version",
    "judgments_event_seq",
    "compiler_version",
    "policy_version",
}
PRIVACY_POLICY_FIELDS = {"excluded_count", "reasons"}
SELECTION_FIELDS = {
    "section_item_ids",
    "selected_item_ids",
    "excluded_item_ids",
}
RECORD_FIELDS = {
    "preview_id",
    "context_id",
    "revision",
    "lifecycle_status",
    "availability_status",
    "task",
    "mode",
    "source_revision",
    "selection",
    "privacy_policy",
    "preview_hash",
    "preview_body_hash",
    "generated_at",
    "expires_at",
    "updated_at",
    "compiled_at",
    "consumed_at",
    "outcome_recorded_at",
    "outcome_id",
    "outcome_hash",
    "based_on_event_seq",
    "stream_version",
}
MAX_PREVIEW_TTL_SECONDS = 24 * 60 * 60
LIFECYCLE_EVENTS = {
    "context.preview_created": (None, "preview"),
    "context.compiled": ("preview", "compiled"),
    "context.consumed": ("compiled", "consumed"),
    "context.outcome_recorded": ("consumed", "outcome_recorded"),
}
IDENTIFIER_PATTERN = re.compile(r"\A(?:prv|ctx)_[0-9a-f]{32}\Z")
PREVIEW_IDENTIFIER_PATTERN = re.compile(r"\Aprv_[0-9a-f]{32}\Z")
CONTEXT_IDENTIFIER_PATTERN = re.compile(r"\Actx_[0-9a-f]{32}\Z")


class ContextStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ContextNotFound(ContextStoreError):
    def __init__(self, identifier: str) -> None:
        super().__init__("context_not_found", "context metadata was not found: " + identifier)


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identifier(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _validated_identifier(value: Any, *, kind: Optional[str] = None) -> str:
    pattern = {
        "preview": PREVIEW_IDENTIFIER_PATTERN,
        "context": CONTEXT_IDENTIFIER_PATTERN,
        None: IDENTIFIER_PATTERN,
    }[kind]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContextStoreError(
            "invalid_identifier", "context identifier is not a canonical ID"
        )
    return value


def _is_hash(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    body = value[7:]
    return len(body) == 64 and all(character in "0123456789abcdef" for character in body)


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ContextStoreError("invalid_timestamp", field + " must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContextStoreError(
            "invalid_timestamp", field + " must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContextStoreError(
            "invalid_timestamp", field + " must include a UTC offset"
        )
    return parsed.astimezone(timezone.utc)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextStoreError(field + "_required", field + " is required")
    return value.strip()


def _string_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, list):
        raise ContextStoreError("invalid_" + field, field + " must be a list")
    result = []
    for item in value:
        result.append(_text(item, field))
    if len(set(result)) != len(result):
        raise ContextStoreError("invalid_" + field, field + " must be unique")
    return result


def _source_revision(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != SOURCE_REVISION_FIELDS:
        raise ContextStoreError(
            "invalid_source_revision", "source_revision has an invalid schema"
        )
    result = dict(value)
    for field in ("claims_event_seq", "judgments_event_seq"):
        item = result[field]
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ContextStoreError(
                "invalid_source_revision", field + " must be non-negative"
            )
    if (
        not isinstance(result["policy_version"], int)
        or isinstance(result["policy_version"], bool)
        or result["policy_version"] < 1
    ):
        raise ContextStoreError(
            "invalid_source_revision", "policy_version must be positive"
        )
    for field in ("living_self_version", "compiler_version"):
        result[field] = _text(result[field], field)
    return result


def _privacy_policy(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != PRIVACY_POLICY_FIELDS:
        raise ContextStoreError(
            "invalid_privacy_policy", "privacy_policy has an invalid schema"
        )
    count = value["excluded_count"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ContextStoreError(
            "invalid_privacy_policy", "excluded_count must be non-negative"
        )
    return {
        "excluded_count": count,
        "reasons": _string_list(value["reasons"], "privacy_reasons"),
    }


def _compile_policy(value: Any) -> Dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"max_chars", "max_bytes"}:
        raise ContextStoreError(
            "invalid_compile_policy", "compile policy has an invalid schema"
        )
    result = {}
    for field in ("max_chars", "max_bytes"):
        item = value[field]
        if not isinstance(item, int) or isinstance(item, bool) or item < 1:
            raise ContextStoreError(
                "invalid_compile_policy", field + " must be positive"
            )
        result[field] = item
    return result


def _selection(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != CONTEXT_SECTIONS:
        raise ContextStoreError(
            "invalid_context_sections", "sections must contain exactly six sections"
        )
    section_item_ids: Dict[str, List[str]] = {}
    selected: List[str] = []
    for section in sorted(CONTEXT_SECTIONS):
        rows = value[section]
        if not isinstance(rows, list):
            raise ContextStoreError(
                "invalid_context_sections", "each context section must be a list"
            )
        ids = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ContextStoreError(
                    "invalid_context_sections", "section items must be objects"
                )
            item_id = _text(row.get("id"), "item_id")
            ids.append(item_id)
            selected.append(item_id)
        if len(set(ids)) != len(ids):
            raise ContextStoreError(
                "invalid_context_sections", "section item IDs must be unique"
            )
        section_item_ids[section] = sorted(ids)
    if len(set(selected)) != len(selected):
        raise ContextStoreError(
            "invalid_context_sections", "selected item IDs must be globally unique"
        )
    return {
        "section_item_ids": section_item_ids,
        "selected_item_ids": sorted(selected),
        "excluded_item_ids": [],
    }


class ContextStore:
    """Persist safe Context lifecycle metadata and rebuildable projections."""

    def __init__(
        self,
        vault_dir: Path,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        absolute = os.path.abspath(str(vault_dir))
        if sys.platform == "darwin" and (
            absolute == "/var" or absolute.startswith("/var/")
        ):
            absolute = "/private" + absolute
        self.root = Path(absolute) / "contexts"
        self.events = JsonlEventStore(self.root / "events.jsonl")
        self.current_path = self.root / "current.jsonl"
        self.previews_dir = self.root / "previews"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._write_lock = threading.RLock()
        try:
            has_events = self.events.exists()
        except EventPathError:
            has_events = False
        if has_events:
            self._ensure_current()

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception as exc:
            raise ContextStoreError("invalid_clock", "context clock failed") from exc
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ContextStoreError(
                "invalid_clock", "context clock must return a timezone-aware datetime"
            )
        return value.astimezone(timezone.utc)

    def _operation_time(
        self, previous: Optional[Mapping[str, Any]] = None
    ) -> datetime:
        now = self._now()
        if previous is not None:
            latest = _timestamp(previous["updated_at"], field="updated_at")
            if latest > now:
                raise ContextStoreError(
                    "future_timestamp", "stored context metadata is from the future"
                )
        return now

    @staticmethod
    def _public_key(value: str) -> str:
        return "ctx-idem:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize(current: Mapping[str, Mapping[str, Any]]) -> str:
        return "".join(
            _canonical(current[key]) + "\n" for key in sorted(current)
        )

    @staticmethod
    def _event_record(event: Mapping[str, Any]) -> Dict[str, Any]:
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("record"), Mapping
        ):
            raise EventCorruption(
                "invalid_context_event",
                "context event payload is invalid",
                line_number=int(event["seq"]),
            )
        record = dict(payload["record"])
        missing_link_fields = {
            field for field in ("outcome_id", "outcome_hash") if field not in record
        }
        if missing_link_fields:
            if (
                missing_link_fields != {"outcome_id", "outcome_hash"}
                or event.get("event_type") == "context.outcome_recorded"
            ):
                raise EventCorruption(
                    "invalid_context_event",
                    "context outcome linkage fields are incomplete",
                    line_number=int(event["seq"]),
                )
            record["outcome_id"] = None
            record["outcome_hash"] = None
        return record

    @staticmethod
    def _corruption(event: Mapping[str, Any], message: str) -> EventCorruption:
        return EventCorruption(
            "invalid_context_event",
            message,
            line_number=int(event["seq"]),
        )

    @classmethod
    def _validate_selection(
        cls, value: Any, event: Mapping[str, Any]
    ) -> Dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != SELECTION_FIELDS:
            raise cls._corruption(event, "context selection schema is invalid")
        section_ids = value["section_item_ids"]
        if not isinstance(section_ids, Mapping) or set(section_ids) != CONTEXT_SECTIONS:
            raise cls._corruption(event, "context section selection is invalid")
        normalized: Dict[str, List[str]] = {}
        all_ids: List[str] = []
        try:
            for section in sorted(CONTEXT_SECTIONS):
                normalized[section] = _string_list(
                    section_ids[section], "section_item_ids"
                )
                if normalized[section] != sorted(normalized[section]):
                    raise ContextStoreError(
                        "invalid_selection", "section item IDs must be sorted"
                    )
                all_ids.extend(normalized[section])
            selected = _string_list(value["selected_item_ids"], "selected_item_ids")
            excluded = _string_list(value["excluded_item_ids"], "excluded_item_ids")
        except ContextStoreError as exc:
            raise cls._corruption(event, str(exc)) from exc
        if (
            len(set(all_ids)) != len(all_ids)
            or selected != sorted(all_ids)
            or excluded != sorted(excluded)
            or not set(excluded).issubset(selected)
        ):
            raise cls._corruption(event, "context selection IDs are inconsistent")
        return {
            "section_item_ids": normalized,
            "selected_item_ids": selected,
            "excluded_item_ids": excluded,
        }

    @classmethod
    def _validate_record(
        cls, record: Mapping[str, Any], event: Mapping[str, Any]
    ) -> None:
        if set(record) != RECORD_FIELDS:
            raise cls._corruption(event, "context record fields are invalid")
        if (
            not _is_hash(record["preview_hash"])
            or not _is_hash(record["preview_body_hash"])
            or record["mode"] not in CONTEXT_MODES
            or record["availability_status"] not in {"active", "expired"}
        ):
            raise cls._corruption(event, "context record identity is invalid")
        try:
            _validated_identifier(record["preview_id"], kind="preview")
            if record["context_id"] is not None:
                _validated_identifier(record["context_id"], kind="context")
            _text(record["task"], "task")
            _source_revision(record["source_revision"])
            _privacy_policy(record["privacy_policy"])
            outcome_id = record["outcome_id"]
            outcome_hash = record["outcome_hash"]
            if (outcome_id is None) != (outcome_hash is None):
                raise ContextStoreError(
                    "invalid_outcome_link", "outcome linkage is incomplete"
                )
            if outcome_id is not None:
                if (
                    not isinstance(outcome_id, str)
                    or re.fullmatch(r"out_[0-9a-f]{32}", outcome_id) is None
                    or not _is_hash(outcome_hash)
                ):
                    raise ContextStoreError(
                        "invalid_outcome_link", "outcome linkage is invalid"
                    )
        except ContextStoreError as exc:
            raise cls._corruption(event, str(exc)) from exc
        cls._validate_selection(record["selection"], event)
        try:
            generated = _timestamp(record["generated_at"], field="generated_at")
            expires = _timestamp(record["expires_at"], field="expires_at")
            updated = _timestamp(record["updated_at"], field="updated_at")
            occurred = _timestamp(event["occurred_at"], field="occurred_at")
        except ContextStoreError as exc:
            raise cls._corruption(event, str(exc)) from exc
        if (
            generated >= expires
            or updated != occurred
            or updated < generated
            or record["updated_at"] != event["occurred_at"]
        ):
            raise cls._corruption(event, "context record timestamps are inconsistent")
        for field in ("compiled_at", "consumed_at", "outcome_recorded_at"):
            value = record[field]
            if value is not None:
                parsed = _timestamp(value, field=field)
                if parsed < generated or parsed > occurred:
                    raise cls._corruption(
                        event, "context lifecycle timestamp is inconsistent"
                    )
        if (
            not isinstance(record["revision"], int)
            or isinstance(record["revision"], bool)
            or not isinstance(record["stream_version"], int)
            or isinstance(record["stream_version"], bool)
            or not isinstance(record["based_on_event_seq"], int)
            or isinstance(record["based_on_event_seq"], bool)
            or record["based_on_event_seq"] < 0
        ):
            raise cls._corruption(event, "context record revision is invalid")

    @classmethod
    def _created_expected(
        cls, event: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> Dict[str, Any]:
        safe_input = operation.get("input")
        if (
            set(operation)
            != {"actor", "expected_version", "input", "method", "reason"}
            or operation.get("method") != "create_preview"
            or operation.get("expected_version") != 0
            or not isinstance(safe_input, Mapping)
            or set(safe_input)
            != {
                "mode",
                "compile_policy",
                "privacy_policy",
                "preview_body_hash",
                "selection",
                "source_revision",
                "task",
                "ttl_seconds",
            }
        ):
            raise cls._corruption(event, "preview creation intent is invalid")
        ttl = safe_input["ttl_seconds"]
        if (
            not isinstance(ttl, int)
            or isinstance(ttl, bool)
            or ttl < 1
            or ttl > MAX_PREVIEW_TTL_SECONDS
        ):
            raise cls._corruption(event, "preview TTL is invalid")
        record = cls._event_record(event)
        occurred = _timestamp(event["occurred_at"], field="occurred_at")
        return {
            "preview_id": record["preview_id"],
            "context_id": None,
            "revision": 1,
            "lifecycle_status": "preview",
            "availability_status": "active",
            "task": safe_input["task"],
            "mode": safe_input["mode"],
            "source_revision": safe_input["source_revision"],
            "selection": safe_input["selection"],
            "privacy_policy": safe_input["privacy_policy"],
            "preview_hash": _digest(safe_input),
            "preview_body_hash": safe_input["preview_body_hash"],
            "generated_at": event["occurred_at"],
            "expires_at": (occurred + timedelta(seconds=ttl)).isoformat(),
            "updated_at": event["occurred_at"],
            "compiled_at": None,
            "consumed_at": None,
            "outcome_recorded_at": None,
            "outcome_id": None,
            "outcome_hash": None,
            "based_on_event_seq": 0,
            "stream_version": 1,
        }

    @classmethod
    def _transition_expected(
        cls,
        event: Mapping[str, Any],
        operation: Mapping[str, Any],
        previous: Mapping[str, Any],
        next_status: str,
    ) -> Dict[str, Any]:
        method = operation.get("method")
        base_fields = {"actor", "expected_version", "identifier", "method", "reason"}
        if method == "begin_compile":
            expected_fields = base_fields | {
                "approved_mode",
                "preview_hash",
                "source_revision",
                "excluded_item_ids",
            }
        elif method == "mark_outcome_recorded":
            expected_fields = base_fields | {"outcome_id", "outcome_hash"}
        else:
            expected_fields = base_fields
        expected_method = {
            "compiled": "begin_compile",
            "consumed": "consume",
            "outcome_recorded": "mark_outcome_recorded",
        }[next_status]
        if (
            set(operation) != expected_fields
            or method != expected_method
            or operation.get("expected_version") != previous["stream_version"]
            or operation.get("identifier")
            not in {previous["preview_id"], previous.get("context_id")}
        ):
            raise cls._corruption(event, "context transition intent is invalid")
        occurred = _timestamp(event["occurred_at"], field="occurred_at")
        expires = _timestamp(previous["expires_at"], field="expires_at")
        expected = dict(previous)
        expected["lifecycle_status"] = next_status
        expected["revision"] = event["stream_version"]
        expected["stream_version"] = event["stream_version"]
        expected["updated_at"] = event["occurred_at"]
        expected["availability_status"] = (
            "active" if occurred < expires else "expired"
        )
        record = cls._event_record(event)
        if method == "begin_compile":
            excluded = operation["excluded_item_ids"]
            approved_mode = operation["approved_mode"]
            if (
                operation["preview_hash"] != previous["preview_hash"]
                or operation["source_revision"] != previous["source_revision"]
                or approved_mode not in CONTEXT_MODES
                or approved_mode == "auto"
                or (
                    previous["mode"] != "auto"
                    and approved_mode != previous["mode"]
                )
                or not isinstance(excluded, list)
                or excluded != sorted(excluded)
                or not set(excluded).issubset(
                    previous["selection"]["selected_item_ids"]
                )
                or CONTEXT_IDENTIFIER_PATTERN.fullmatch(
                    str(record.get("context_id") or "")
                )
                is None
            ):
                raise cls._corruption(event, "compiled context approval is invalid")
            selection = dict(previous["selection"])
            selection["excluded_item_ids"] = excluded
            expected["selection"] = selection
            expected["mode"] = approved_mode
            expected["context_id"] = record["context_id"]
            expected["compiled_at"] = event["occurred_at"]
        elif method == "consume":
            expected["consumed_at"] = event["occurred_at"]
        elif method == "mark_outcome_recorded":
            if (
                not isinstance(operation.get("outcome_id"), str)
                or re.fullmatch(
                    r"out_[0-9a-f]{32}", str(operation.get("outcome_id"))
                )
                is None
                or not _is_hash(operation.get("outcome_hash"))
            ):
                raise cls._corruption(event, "outcome linkage intent is invalid")
            expected["outcome_recorded_at"] = event["occurred_at"]
            expected["outcome_id"] = operation["outcome_id"]
            expected["outcome_hash"] = operation["outcome_hash"]
        return expected

    @classmethod
    def _project_event(
        cls,
        event: Mapping[str, Any],
        current: Dict[str, Dict[str, Any]],
    ) -> None:
        event_type = event["event_type"]
        if event_type not in LIFECYCLE_EVENTS:
            raise EventCorruption(
                "invalid_context_event",
                "unknown context lifecycle event",
                line_number=int(event["seq"]),
            )
        record = cls._event_record(event)
        cls._validate_record(record, event)
        preview_id = str(record.get("preview_id") or "")
        before, after = LIFECYCLE_EVENTS[event_type]
        previous = current.get(preview_id)
        if not preview_id or (previous is None) != (before is None):
            raise EventCorruption(
                "invalid_context_event",
                "context event stream is inconsistent",
                line_number=int(event["seq"]),
            )
        payload = event["payload"]
        operation = payload.get("operation")
        if (
            set(payload) != {"record", "operation", "reason"}
            or
            not isinstance(operation, Mapping)
            or payload.get("reason") != operation.get("reason")
            or event.get("actor") != operation.get("actor")
            or event.get("expected_version") != operation.get("expected_version")
            or event.get("previous_status") != before
        ):
            raise cls._corruption(event, "context event authority metadata is invalid")
        try:
            _text(operation.get("reason"), "reason")
        except ContextStoreError as exc:
            raise cls._corruption(event, str(exc)) from exc
        if previous is not None:
            if previous["lifecycle_status"] != before:
                raise EventCorruption(
                    "invalid_context_event",
                    "context lifecycle transition is invalid",
                    line_number=int(event["seq"]),
                )
            expected_record = cls._transition_expected(
                event, operation, previous, after
            )
        else:
            expected_record = cls._created_expected(event, operation)
        if (
            record.get("lifecycle_status") != after
            or record.get("stream_version") != event["stream_version"]
            or record.get("revision") != event["stream_version"]
            or event["stream_id"] != preview_id
        ):
            raise EventCorruption(
                "invalid_context_event",
                "context projection revision is invalid",
                line_number=int(event["seq"]),
            )
        if record != expected_record:
            raise cls._corruption(
                event, "context event does not match its authoritative intent"
            )
        record["based_on_event_seq"] = int(event["seq"])
        current[preview_id] = record

    def _replay(self) -> Tuple[Dict[str, Dict[str, Any]], int]:
        current: Dict[str, Dict[str, Any]] = {}
        head = 0
        for event in self.events.iter_all():
            head = int(event["seq"])
            self._project_event(event, current)
        return current, head

    def _ensure_current(self) -> Dict[str, Dict[str, Any]]:
        if not self.events.exists():
            return {}
        while True:
            current, head = self._replay()
            serialized = self._serialize(current)
            if safe_read_text(self.current_path) != serialized:
                safe_atomic_write_text(self.current_path, serialized)
            if self.events.watermark() == head:
                return current

    def _dynamic(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        result = dict(record)
        now = self._now()
        if (
            _timestamp(result["generated_at"], field="generated_at") > now
            or _timestamp(result["updated_at"], field="updated_at") > now
        ):
            raise ContextStoreError(
                "future_timestamp", "stored context metadata is from the future"
            )
        expires = _timestamp(result["expires_at"], field="expires_at")
        result["availability_status"] = (
            "active" if now < expires else "expired"
        )
        return result

    def _lookup(
        self, identifier: str, current: Mapping[str, Mapping[str, Any]]
    ) -> Dict[str, Any]:
        _validated_identifier(identifier)
        if identifier in current:
            return dict(current[identifier])
        for record in current.values():
            if record.get("context_id") == identifier:
                return dict(record)
        raise ContextNotFound(identifier)

    def get(self, identifier: str) -> Dict[str, Any]:
        return self._dynamic(self._lookup(identifier, self._ensure_current()))

    def list(self) -> List[Dict[str, Any]]:
        current = self._ensure_current()
        return [self._dynamic(current[key]) for key in sorted(current)]

    @staticmethod
    def _metadata(
        *,
        expected_version: Any,
        request_id: Any,
        idempotency_key: Any,
        actor: Any,
        reason: Any,
    ) -> Tuple[int, str, str, Dict[str, str], str]:
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
        ):
            raise ContextStoreError(
                "invalid_expected_version", "expected_version must be non-negative"
            )
        request = _text(request_id, "request_id")
        idem = _text(idempotency_key, "idempotency_key")
        why = _text(reason, "reason")
        if (
            not isinstance(actor, Mapping)
            or set(actor) != {"kind", "id"}
            or actor.get("kind") not in ACTOR_KINDS
        ):
            raise ContextStoreError("invalid_actor", "actor is invalid")
        return (
            expected_version,
            request,
            idem,
            {"kind": str(actor["kind"]), "id": _text(actor["id"], "actor_id")},
            why,
        )

    @staticmethod
    def _operation_intent(operation: Mapping[str, Any]) -> str:
        return _canonical(operation)

    def _find_idempotent(
        self, key: str, operation: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        requested = {key, self._public_key(key)}
        with self._write_lock:
            self._replay()
            for event in self.events.iter_all():
                if event["idempotency_key"] not in requested:
                    continue
                existing = event["payload"].get("operation")
                if (
                    not isinstance(existing, Mapping)
                    or self._operation_intent(existing)
                    != self._operation_intent(operation)
                ):
                    raise ContextStoreError(
                        "idempotency_conflict",
                        "idempotency key was reused for a different intent",
                    )
                return event
        return None

    def _return_event(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        return self.get(self._event_record(event)["preview_id"])

    def _append(
        self,
        *,
        event_type: str,
        record: Mapping[str, Any],
        operation: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
        expected_version: int,
        previous_status: Optional[str],
        occurred_at: str,
    ) -> Dict[str, Any]:
        event = new_event(
            event_type=event_type,
            stream_id=str(record["preview_id"]),
            stream_version=expected_version + 1,
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=self._public_key(idempotency_key),
            actor=actor,
            payload={
                "record": dict(record),
                "operation": dict(operation),
                "reason": reason,
            },
            previous_status=previous_status,
            now=occurred_at,
        )
        try:
            with self._write_lock:
                return self.events.append(event)
        except EventConflict as exc:
            if exc.code == "idempotency_conflict":
                prior = self._find_idempotent(idempotency_key, operation)
                if prior is not None:
                    return prior
            raise ContextStoreError(exc.code, str(exc)) from exc

    def _write_preview_cache(
        self,
        record: Mapping[str, Any],
        sections: Mapping[str, Any],
        compile_policy: Mapping[str, int],
    ) -> None:
        path = self.previews_dir / (str(record["preview_id"]) + ".json")
        try:
            copied_sections = json.loads(
                json.dumps(
                    sections,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ContextStoreError(
                "invalid_preview_body", "preview sections must be JSON serializable"
            ) from exc
        body = {
            "preview_id": record["preview_id"],
            "preview_hash": record["preview_hash"],
            "preview_body_hash": record["preview_body_hash"],
            "source_revision": record["source_revision"],
            "sections": copied_sections,
            "compile_policy": dict(compile_policy),
            "generated_at": record["generated_at"],
            "expires_at": record["expires_at"],
        }
        with self._write_lock:
            safe_atomic_write_text(path, _canonical(body) + "\n")

    def _verify_preview_cache(
        self, record: Mapping[str, Any]
    ) -> Dict[str, Any]:
        path = self.previews_dir / (str(record["preview_id"]) + ".json")
        raw = safe_read_text(path)
        if raw is None:
            raise ContextStoreError(
                "preview_unavailable", "private preview body is unavailable"
            )
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContextStoreError(
                "stale_preview", "private preview body is malformed"
            ) from exc
        if (
            not isinstance(body, Mapping)
            or set(body)
            != {
                "preview_id",
                "preview_hash",
                "preview_body_hash",
                "source_revision",
                "sections",
                "compile_policy",
                "generated_at",
                "expires_at",
            }
            or body["preview_id"] != record["preview_id"]
            or body["preview_hash"] != record["preview_hash"]
            or body["preview_body_hash"] != record["preview_body_hash"]
            or body["source_revision"] != record["source_revision"]
            or body["generated_at"] != record["generated_at"]
            or body["expires_at"] != record["expires_at"]
        ):
            raise ContextStoreError(
                "stale_preview", "private preview body does not match authority metadata"
            )
        if (
            _digest(
                {
                    "sections": body["sections"],
                    "compile_policy": body["compile_policy"],
                }
            )
            != record["preview_body_hash"]
        ):
            raise ContextStoreError(
                "stale_preview", "private preview body hash has changed"
            )
        try:
            cached_selection = _selection(body["sections"])
            _compile_policy(body["compile_policy"])
        except ContextStoreError as exc:
            raise ContextStoreError("stale_preview", str(exc)) from exc
        expected_selection = dict(record["selection"])
        expected_selection["excluded_item_ids"] = []
        if cached_selection != expected_selection:
            raise ContextStoreError(
                "stale_preview", "private preview body selection has changed"
            )
        return json.loads(json.dumps(body, ensure_ascii=False))

    def load_preview_body(
        self, preview_id: str, preview_hash: str
    ) -> Dict[str, Any]:
        """Return a verified private preview copy without exposing its path."""
        record = self.get(preview_id)
        if (
            not isinstance(preview_hash, str)
            or preview_hash != record["preview_hash"]
        ):
            raise ContextStoreError(
                "stale_preview", "preview hash no longer matches authority"
            )
        return self._verify_preview_cache(record)

    def create_preview(
        self,
        *,
        task: str,
        mode: str,
        source_revision: Mapping[str, Any],
        sections: Mapping[str, Any],
        privacy_policy: Mapping[str, Any],
        ttl_seconds: int,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
        compile_policy: Optional[Mapping[str, int]] = None,
    ) -> Dict[str, Any]:
        expected, request, idem, actor_value, why = self._metadata(
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )
        if expected != 0:
            raise ContextStoreError(
                "version_conflict", "preview creation requires expected version 0"
            )
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or ttl_seconds < 1
            or ttl_seconds > MAX_PREVIEW_TTL_SECONDS
        ):
            raise ContextStoreError(
                "invalid_ttl",
                "ttl_seconds must be between 1 and "
                + str(MAX_PREVIEW_TTL_SECONDS),
            )
        task_value = _text(task, "task")
        mode_value = _text(mode, "mode")
        if mode_value not in CONTEXT_MODES:
            raise ContextStoreError("invalid_context_mode", "mode is not supported")
        revision = _source_revision(source_revision)
        selection = _selection(sections)
        policy = _privacy_policy(privacy_policy)
        compile_policy_value = _compile_policy(
            compile_policy
            if compile_policy is not None
            else {
                "max_chars": 24_000,
                "max_bytes": CONTEXT_DEFAULT_MAX_BYTES,
            }
        )
        try:
            private_sections = json.loads(
                json.dumps(
                    sections,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ContextStoreError(
                "invalid_preview_body", "preview sections must be JSON serializable"
            ) from exc
        safe_input = {
            "compile_policy": compile_policy_value,
            "mode": mode_value,
            "privacy_policy": policy,
            "preview_body_hash": _digest(
                {
                    "sections": private_sections,
                    "compile_policy": compile_policy_value,
                }
            ),
            "selection": selection,
            "source_revision": revision,
            "task": task_value,
            "ttl_seconds": ttl_seconds,
        }
        operation = {
            "actor": actor_value,
            "expected_version": 0,
            "input": safe_input,
            "method": "create_preview",
            "reason": why,
        }
        prior = self._find_idempotent(idem, operation)
        if prior is not None:
            result = self._return_event(prior)
            self._write_preview_cache(
                result, private_sections, compile_policy_value
            )
            return result
        now = self._operation_time()
        preview_id = _identifier("prv_")
        record = {
            "preview_id": preview_id,
            "context_id": None,
            "revision": 1,
            "lifecycle_status": "preview",
            "availability_status": "active",
            "task": task_value,
            "mode": mode_value,
            "source_revision": revision,
            "selection": selection,
            "privacy_policy": policy,
            "preview_hash": _digest(safe_input),
            "preview_body_hash": safe_input["preview_body_hash"],
            "generated_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "updated_at": now.isoformat(),
            "compiled_at": None,
            "consumed_at": None,
            "outcome_recorded_at": None,
            "outcome_id": None,
            "outcome_hash": None,
            "based_on_event_seq": 0,
            "stream_version": 1,
        }
        event = self._append(
            event_type="context.preview_created",
            record=record,
            operation=operation,
            request_id=request,
            idempotency_key=idem,
            actor=actor_value,
            reason=why,
            expected_version=0,
            previous_status=None,
            occurred_at=now.isoformat(),
        )
        result = self._return_event(event)
        self._write_preview_cache(result, private_sections, compile_policy_value)
        return result

    def begin_compile(
        self,
        preview_id: str,
        *,
        approved_mode: str,
        preview_hash: str,
        source_revision: Mapping[str, Any],
        excluded_item_ids: List[str],
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
    ) -> Dict[str, Any]:
        return self._transition(
            preview_id,
            method="begin_compile",
            event_type="context.compiled",
            required_status="preview",
            next_status="compiled",
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
            approved_mode=approved_mode,
            preview_hash=preview_hash,
            source_revision=source_revision,
            excluded_item_ids=excluded_item_ids,
        )

    def consume(
        self,
        context_id: str,
        *,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
    ) -> Dict[str, Any]:
        return self._transition(
            context_id,
            method="consume",
            event_type="context.consumed",
            required_status="compiled",
            next_status="consumed",
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )

    def mark_outcome_recorded(
        self,
        context_id: str,
        *,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
        outcome_id: str,
        outcome_hash: str,
    ) -> Dict[str, Any]:
        return self._transition(
            context_id,
            method="mark_outcome_recorded",
            event_type="context.outcome_recorded",
            required_status="consumed",
            next_status="outcome_recorded",
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
            outcome_id=outcome_id,
            outcome_hash=outcome_hash,
        )

    def _transition(
        self,
        identifier: str,
        *,
        method: str,
        event_type: str,
        required_status: str,
        next_status: str,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
        approved_mode: Optional[str] = None,
        preview_hash: Optional[str] = None,
        source_revision: Optional[Mapping[str, Any]] = None,
        excluded_item_ids: Optional[List[str]] = None,
        outcome_id: Optional[str] = None,
        outcome_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        current = self.get(identifier)
        expected, request, idem, actor_value, why = self._metadata(
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )
        operation: Dict[str, Any] = {
            "actor": actor_value,
            "expected_version": expected,
            "identifier": identifier,
            "method": method,
            "reason": why,
        }
        if method == "begin_compile":
            operation.update(
                {
                    "preview_hash": preview_hash,
                    "approved_mode": approved_mode,
                    "source_revision": dict(source_revision or {}),
                    "excluded_item_ids": list(excluded_item_ids or []),
                }
            )
        elif method == "mark_outcome_recorded":
            operation.update(
                {
                    "outcome_id": outcome_id,
                    "outcome_hash": outcome_hash,
                }
            )
        prior = self._find_idempotent(idem, operation)
        if prior is not None:
            return self._return_event(prior)
        if current["stream_version"] != expected:
            raise ContextStoreError("version_conflict", "context version changed")
        if current["lifecycle_status"] != required_status:
            raise ContextStoreError(
                "invalid_transition",
                current["lifecycle_status"] + " cannot transition to " + next_status,
            )
        if current["availability_status"] == "expired" and method != "mark_outcome_recorded":
            code = "stale_preview" if required_status == "preview" else "context_expired"
            raise ContextStoreError(code, "context metadata has expired")
        if method == "begin_compile":
            self._verify_preview_cache(current)
            try:
                if (
                    approved_mode not in CONTEXT_MODES
                    or approved_mode == "auto"
                    or (
                        current["mode"] != "auto"
                        and approved_mode != current["mode"]
                    )
                ):
                    raise ContextStoreError(
                        "resolved_mode_conflict",
                        "approved mode does not match the reviewed preview",
                    )
                supplied_revision = _source_revision(source_revision)
                excluded = sorted(_string_list(excluded_item_ids, "excluded_item_ids"))
            except ContextStoreError as exc:
                raise ContextStoreError("stale_preview", str(exc)) from exc
            selected = set(current["selection"]["selected_item_ids"])
            if (
                preview_hash != current["preview_hash"]
                or supplied_revision != current["source_revision"]
                or not set(excluded).issubset(selected)
            ):
                raise ContextStoreError(
                    "stale_preview", "preview approval no longer matches the preview"
                )
        elif method == "mark_outcome_recorded":
            if (
                not isinstance(outcome_id, str)
                or re.fullmatch(r"out_[0-9a-f]{32}", outcome_id) is None
                or not _is_hash(outcome_hash)
            ):
                raise ContextStoreError(
                    "invalid_outcome_link", "outcome linkage is invalid"
                )
            excluded = []
        else:
            excluded = []
        now = self._operation_time(current)
        updated = dict(current)
        updated["lifecycle_status"] = next_status
        updated["revision"] = expected + 1
        updated["stream_version"] = expected + 1
        updated["updated_at"] = now.isoformat()
        if method == "begin_compile":
            selection = dict(updated["selection"])
            selection["excluded_item_ids"] = excluded
            updated["selection"] = selection
            updated["mode"] = str(approved_mode)
            updated["context_id"] = _identifier("ctx_")
            updated["compiled_at"] = now.isoformat()
        elif method == "consume":
            updated["consumed_at"] = now.isoformat()
        else:
            updated["outcome_recorded_at"] = now.isoformat()
            updated["outcome_id"] = outcome_id
            updated["outcome_hash"] = outcome_hash
        event = self._append(
            event_type=event_type,
            record=updated,
            operation=operation,
            request_id=request,
            idempotency_key=idem,
            actor=actor_value,
            reason=why,
            expected_version=expected,
            previous_status=required_status,
            occurred_at=now.isoformat(),
        )
        return self._return_event(event)
