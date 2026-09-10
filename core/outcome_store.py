#!/usr/bin/env python3
"""Recoverable, event-backed feedback for consumed Context snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from context_compiler import ContextCompiler, ContextCompilerError
from context_store import ContextStore, ContextStoreError
from event_store import EventConflict, EventCorruption, JsonlEventStore
from model_types import (
    ACTOR_KINDS,
    ModelValidationError,
    new_event,
    new_outcome_event,
    validate_outcome_event,
)
from redact_common import redact as redact_credentials


MAX_OUTCOME_SUMMARY_CHARS = 500
MAX_OUTCOME_REASON_CHARS = 300
PUBLIC_IDEMPOTENCY_PREFIX = "outcome:public:v1:"
TYPED_REF_KEYS = frozenset({"kind", "id", "revision"})
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(cookie\s*[:：=]\s*)\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bou_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?:/Users|/home)/[^/\s]+"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
)


class OutcomeStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeStoreError("invalid_" + field, field + " is required")
    result = redact_credentials(value.strip())
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result[:maximum]


def _copy_refs(value: Optional[Sequence[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise OutcomeStoreError("invalid_typed_refs", "outcome refs must be a list")
    copied = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != TYPED_REF_KEYS:
            raise OutcomeStoreError(
                "invalid_typed_refs",
                "outcome refs require exactly kind, id, and revision",
            )
        copied.append(dict(item))
    return sorted(
        copied,
        key=lambda item: (
            str(item.get("kind", "")),
            str(item.get("id", "")),
            int(item.get("revision", 0))
            if isinstance(item.get("revision"), int)
            else 0,
        ),
    )


class OutcomeStore:
    """Append outcome first, then bind it to Context with recoverable replay."""

    def __init__(
        self,
        vault_dir: Path,
        *,
        context_store: Optional[ContextStore] = None,
        context_compiler: Optional[ContextCompiler] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        vault = Path(os.path.abspath(str(vault_dir)))
        self.root = vault / "outcomes"
        self.events = JsonlEventStore(self.root / "events.jsonl")
        self.contexts = context_store or ContextStore(vault, clock=clock)
        self.compiler = context_compiler or ContextCompiler(
            vault,
            context_store=self.contexts,
            clock=clock,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> str:
        try:
            value = self._clock()
        except Exception as exc:
            raise OutcomeStoreError("invalid_clock", "outcome clock failed") from exc
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise OutcomeStoreError(
                "invalid_clock", "outcome clock must be timezone-aware"
            )
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _public_key(value: str) -> str:
        return PUBLIC_IDEMPOTENCY_PREFIX + hashlib.sha256(
            value.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _metadata(
        *,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
    ) -> Tuple[int, str, str, Dict[str, str], str]:
        actor_value = dict(actor) if isinstance(actor, Mapping) else {}
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
            or not isinstance(request_id, str)
            or not request_id.strip()
            or not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or set(actor_value) != {"kind", "id"}
            or actor_value.get("kind") not in ACTOR_KINDS
            or not isinstance(actor_value.get("id"), str)
            or not actor_value["id"].strip()
        ):
            raise OutcomeStoreError(
                "write_metadata_required",
                "expected version, request ID, idempotency key, actor, and reason are required",
            )
        safe_reason = _safe_text(
            reason,
            field="reason",
            maximum=MAX_OUTCOME_REASON_CHARS,
        )
        return (
            expected_version,
            request_id.strip(),
            idempotency_key.strip(),
            {"kind": actor_value["kind"], "id": actor_value["id"].strip()},
            safe_reason,
        )

    @staticmethod
    def _snapshot_refs(pack: Mapping[str, Any]) -> set:
        allowed = set()
        for section in (
            "verified_facts",
            "confirmed_self_models",
            "judgment_cards",
            "counter_evidence",
            "inferences",
        ):
            for item in pack["sections"][section]:
                kind = item.get("kind")
                if kind in {"claim", "self_model", "judgment"}:
                    allowed.add((kind, item["id"], item["revision"]))
        return allowed

    @classmethod
    def _validate_refs(
        cls,
        pack: Mapping[str, Any],
        confirmed_refs: Sequence[Mapping[str, Any]],
        challenged_refs: Sequence[Mapping[str, Any]],
    ) -> None:
        allowed = cls._snapshot_refs(pack)
        for ref in list(confirmed_refs) + list(challenged_refs):
            identity = (ref.get("kind"), ref.get("id"), ref.get("revision"))
            if identity not in allowed:
                raise OutcomeStoreError(
                    "outcome_ref_not_in_context",
                    "outcome ref is not part of the compiled Context snapshot",
                )

    @staticmethod
    def _operation(
        *,
        context_id: str,
        adopted: str,
        result: str,
        summary: str,
        confirmed_refs: Sequence[Mapping[str, Any]],
        challenged_refs: Sequence[Mapping[str, Any]],
        expected_version: int,
        actor: Mapping[str, str],
        reason: str,
    ) -> Dict[str, Any]:
        return {
            "actor": dict(actor),
            "adopted": adopted,
            "challenged_refs": [dict(item) for item in challenged_refs],
            "confirmed_refs": [dict(item) for item in confirmed_refs],
            "context_expected_version": expected_version,
            "context_id": context_id,
            "reason": reason,
            "result": result,
            "summary": summary,
        }

    @classmethod
    def _decode_event(cls, event: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        try:
            if (
                event["event_type"] != "outcome.recorded"
                or not isinstance(event["stream_version"], int)
                or event["stream_version"] < 1
                or event["expected_version"] != event["stream_version"] - 1
                or event["previous_status"] is not None
                or set(event["payload"]) != {"operation", "outcome"}
            ):
                raise ValueError("invalid outcome envelope")
            outcome = dict(event["payload"]["outcome"])
            operation = dict(event["payload"]["operation"])
            validate_outcome_event(outcome)
            if (
                re.fullmatch(r"out_[0-9a-f]{32}", outcome["outcome_id"]) is None
                or re.fullmatch(r"ctx_[0-9a-f]{32}", outcome["context_id"]) is None
                or not isinstance(operation.get("context_expected_version"), int)
                or isinstance(operation.get("context_expected_version"), bool)
                or operation["context_expected_version"] < 0
                or set(operation.get("actor", {})) != {"kind", "id"}
                or operation["actor"].get("kind") not in ACTOR_KINDS
                or not isinstance(operation["actor"].get("id"), str)
                or not operation["actor"]["id"].strip()
                or _safe_text(
                    operation.get("reason"),
                    field="reason",
                    maximum=MAX_OUTCOME_REASON_CHARS,
                )
                != operation.get("reason")
                or _safe_text(
                    outcome["summary"],
                    field="outcome_summary",
                    maximum=MAX_OUTCOME_SUMMARY_CHARS,
                )
                != outcome["summary"]
                or _copy_refs(outcome["confirmed_refs"])
                != outcome["confirmed_refs"]
                or _copy_refs(outcome["challenged_refs"])
                != outcome["challenged_refs"]
                or re.fullmatch(
                    re.escape(PUBLIC_IDEMPOTENCY_PREFIX) + r"[0-9a-f]{64}",
                    str(event["idempotency_key"]),
                )
                is None
            ):
                raise ValueError("invalid outcome authority metadata")
            expected_operation = cls._operation(
                context_id=outcome["context_id"],
                adopted=outcome["adopted"],
                result=outcome["result"],
                summary=outcome["summary"],
                confirmed_refs=outcome["confirmed_refs"],
                challenged_refs=outcome["challenged_refs"],
                expected_version=operation["context_expected_version"],
                actor=operation["actor"],
                reason=operation["reason"],
            )
            if (
                operation != expected_operation
                or event["stream_id"] != outcome["context_id"]
                or event["actor"] != operation["actor"]
                or event["occurred_at"] != outcome["created_at"]
            ):
                raise ValueError("outcome event does not match intent")
            return outcome, operation
        except (
            KeyError,
            TypeError,
            ValueError,
            ModelValidationError,
            OutcomeStoreError,
        ) as exc:
            raise OutcomeStoreError(
                "outcome_event_corruption", "outcome event replay failed closed"
            ) from exc

    def _events_for_context(self, context_id: str) -> List[Dict[str, Any]]:
        try:
            rows = self.events.read_stream(context_id)
        except EventCorruption as exc:
            raise OutcomeStoreError("outcome_event_corruption", str(exc)) from exc
        decoded = []
        for row in rows:
            outcome, operation = self._decode_event(row)
            decoded.append({"event": row, "outcome": outcome, "operation": operation})
        return decoded

    @staticmethod
    def _event_hash(event: Mapping[str, Any]) -> str:
        return _hash(event)

    @classmethod
    def _binding_metadata(
        cls, event: Mapping[str, Any], outcome_id: str
    ) -> Tuple[str, str, str]:
        event_hash = cls._event_hash(event)
        digest = hashlib.sha256(
            (event_hash + ":" + outcome_id).encode("utf-8")
        ).hexdigest()
        return (
            event_hash,
            "req_outcome_bind_" + digest[:32],
            "outcome-bind:v1:" + digest,
        )

    def _verify_binding(
        self,
        context: Mapping[str, Any],
        row: Mapping[str, Any],
    ) -> None:
        outcome = row["outcome"]
        event_hash, bind_request, bind_key = self._binding_metadata(
            row["event"], outcome["outcome_id"]
        )
        try:
            authority = self.contexts.outcome_linkage_authority(
                outcome["context_id"]
            )
        except ContextStoreError as exc:
            raise OutcomeStoreError(exc.code, str(exc)) from exc
        if (
            context["lifecycle_status"] != "outcome_recorded"
            or context["outcome_id"] != outcome["outcome_id"]
            or context["outcome_hash"] != event_hash
            or authority["outcome_id"] != outcome["outcome_id"]
            or authority["outcome_hash"] != event_hash
            or authority["actor"] != row["operation"]["actor"]
            or authority["reason"] != row["operation"]["reason"]
            or authority["request_id"] != bind_request
            or authority["idempotency_key"]
            != self.contexts._public_key(bind_key)
            or authority["occurred_at"] != context["outcome_recorded_at"]
        ):
            raise OutcomeStoreError(
                "outcome_link_conflict", "cross-stream outcome authority does not match"
            )

    def _find_idempotent(
        self, idempotency_key: str, operation: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        public = self._public_key(idempotency_key)
        try:
            rows = list(self.events.iter_all()) if self.events.exists() else []
        except EventCorruption as exc:
            raise OutcomeStoreError("outcome_event_corruption", str(exc)) from exc
        for row in rows:
            outcome, stored_operation = self._decode_event(row)
            if row["idempotency_key"] != public:
                continue
            if stored_operation != operation:
                raise OutcomeStoreError(
                    "idempotency_conflict",
                    "idempotency key was reused for a different outcome intent",
                )
            return {"event": row, "outcome": outcome, "operation": stored_operation}
        return None

    def context(self, context_id: str) -> Dict[str, Any]:
        try:
            return self.contexts.get(context_id)
        except ContextStoreError as exc:
            raise OutcomeStoreError(exc.code, str(exc)) from exc

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
        try:
            current = self.contexts.get(context_id)
            if current["lifecycle_status"] == "compiled":
                self.compiler.load_compiled(context_id)
            return self.contexts.consume(
                context_id,
                expected_version=expected_version,
                request_id=request_id,
                idempotency_key=idempotency_key,
                actor=actor,
                reason=_safe_text(
                    reason,
                    field="reason",
                    maximum=MAX_OUTCOME_REASON_CHARS,
                ),
            )
        except (ContextCompilerError, ContextStoreError) as exc:
            raise OutcomeStoreError(exc.code, str(exc)) from exc

    def record_outcome(
        self,
        context_id: str,
        *,
        adopted: str,
        result: str,
        summary: str,
        confirmed_refs: Optional[Sequence[Mapping[str, Any]]] = None,
        challenged_refs: Optional[Sequence[Mapping[str, Any]]] = None,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
    ) -> Dict[str, Any]:
        expected, request, idem, actor_value, safe_reason = self._metadata(
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )
        safe_summary = _safe_text(
            summary,
            field="outcome_summary",
            maximum=MAX_OUTCOME_SUMMARY_CHARS,
        )
        confirmed = _copy_refs(confirmed_refs)
        challenged = _copy_refs(challenged_refs)
        operation = self._operation(
            context_id=context_id,
            adopted=adopted,
            result=result,
            summary=safe_summary,
            confirmed_refs=confirmed,
            challenged_refs=challenged,
            expected_version=expected,
            actor=actor_value,
            reason=safe_reason,
        )
        current = self.context(context_id)
        prior = self._find_idempotent(idem, operation)
        if current["lifecycle_status"] == "outcome_recorded":
            if prior is None:
                raise OutcomeStoreError(
                    "outcome_conflict", "context already has a committed outcome"
                )
            self._verify_binding(current, prior)
            try:
                self.compiler.load_outcome_snapshot(context_id)
            except ContextCompilerError as exc:
                raise OutcomeStoreError(exc.code, str(exc)) from exc
            return dict(prior["outcome"])
        if current["lifecycle_status"] != "consumed":
            raise OutcomeStoreError(
                "invalid_transition", "context must be consumed before outcome"
            )
        if current["stream_version"] != expected:
            raise OutcomeStoreError("version_conflict", "context version changed")
        try:
            pack = self.compiler.load_outcome_snapshot(context_id)
        except ContextCompilerError as exc:
            raise OutcomeStoreError(exc.code, str(exc)) from exc
        self._validate_refs(pack, confirmed, challenged)
        try:
            provisional = new_outcome_event(
                context_id=context_id,
                adopted=adopted,
                result=result,
                summary=safe_summary,
                confirmed_refs=confirmed,
                challenged_refs=challenged,
                now=self._now(),
            )
            validate_outcome_event(provisional)
        except ModelValidationError as exc:
            raise OutcomeStoreError(exc.code, str(exc)) from exc
        existing = self._events_for_context(context_id)
        if prior is None:
            if any(
                row["operation"]["context_expected_version"] == expected
                for row in existing
            ):
                prior = self._find_idempotent(idem, operation)
                if prior is None:
                    raise OutcomeStoreError(
                        "outcome_conflict",
                        "context has an unresolved outcome with a valid version",
                    )
        if prior is None:
            outcome_stream_version = len(existing) + 1
            envelope = new_event(
                event_type="outcome.recorded",
                stream_id=context_id,
                stream_version=outcome_stream_version,
                request_id=request,
                idempotency_key=self._public_key(idem),
                actor=actor_value,
                expected_version=outcome_stream_version - 1,
                payload={"operation": operation, "outcome": provisional},
                previous_status=None,
                now=provisional["created_at"],
            )
            try:
                stored = self.events.append(envelope)
            except EventConflict as exc:
                if exc.code == "idempotency_conflict":
                    prior = self._find_idempotent(idem, operation)
                    if prior is None:
                        raise OutcomeStoreError(exc.code, str(exc)) from exc
                elif exc.code == "version_conflict":
                    raise OutcomeStoreError(
                        "outcome_conflict", "a concurrent outcome won"
                    ) from exc
                else:
                    raise OutcomeStoreError(exc.code, str(exc)) from exc
            else:
                outcome, stored_operation = self._decode_event(stored)
                prior = {
                    "event": stored,
                    "outcome": outcome,
                    "operation": stored_operation,
                }
        assert prior is not None
        outcome = prior["outcome"]
        outcome_hash, bind_request, bind_key = self._binding_metadata(
            prior["event"], outcome["outcome_id"]
        )
        current = self.context(context_id)
        if current["lifecycle_status"] == "consumed":
            try:
                self.contexts.mark_outcome_recorded(
                    context_id,
                    expected_version=expected,
                    request_id=bind_request,
                    idempotency_key=bind_key,
                    actor=actor_value,
                    reason=safe_reason,
                    outcome_id=outcome["outcome_id"],
                    outcome_hash=outcome_hash,
                )
            except ContextStoreError as exc:
                raise OutcomeStoreError(exc.code, str(exc)) from exc
        committed = self.context(context_id)
        self._verify_binding(committed, prior)
        try:
            self.compiler.load_outcome_snapshot(context_id)
        except ContextCompilerError as exc:
            raise OutcomeStoreError(exc.code, str(exc)) from exc
        return dict(outcome)

    def get(self, context_id: str) -> Dict[str, Any]:
        rows = self._events_for_context(context_id)
        context = self.context(context_id)
        if not rows:
            if context["lifecycle_status"] == "outcome_recorded":
                raise OutcomeStoreError(
                    "outcome_link_missing", "Context links to a missing outcome event"
                )
            raise OutcomeStoreError("outcome_not_found", "outcome was not found")
        if context["lifecycle_status"] != "outcome_recorded":
            raise OutcomeStoreError(
                "outcome_uncommitted", "outcome linkage is incomplete or mismatched"
            )
        linked = [
            row
            for row in rows
            if row["outcome"]["outcome_id"] == context["outcome_id"]
            and self._event_hash(row["event"]) == context["outcome_hash"]
        ]
        if not linked:
            if any(
                row["outcome"]["outcome_id"] == context["outcome_id"]
                for row in rows
            ):
                raise OutcomeStoreError(
                    "outcome_uncommitted",
                    "outcome event authority does not match Context linkage",
                )
            raise OutcomeStoreError(
                "outcome_link_missing", "Context links to a missing outcome event"
            )
        if len(linked) != 1:
            raise OutcomeStoreError(
                "outcome_event_corruption", "Context linkage is ambiguous"
            )
        self._verify_binding(context, linked[0])
        outcome = linked[0]["outcome"]
        try:
            self.compiler.load_outcome_snapshot(context_id)
        except ContextCompilerError as exc:
            raise OutcomeStoreError(exc.code, str(exc)) from exc
        return dict(outcome)

    def list(self) -> List[Dict[str, Any]]:
        if not self.events.exists():
            return []
        result = []
        seen = set()
        try:
            rows = list(self.events.iter_all())
        except EventCorruption as exc:
            raise OutcomeStoreError("outcome_event_corruption", str(exc)) from exc
        for row in rows:
            outcome, _operation = self._decode_event(row)
            context_id = outcome["context_id"]
            if context_id in seen:
                continue
            seen.add(context_id)
            try:
                result.append(self.get(context_id))
            except OutcomeStoreError as exc:
                if exc.code == "outcome_uncommitted":
                    continue
                raise
        return result
