#!/usr/bin/env python3
"""Event-backed claim storage with an explicit correction state machine."""

from __future__ import annotations

import json
import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from event_store import (
    EventConflict,
    JsonlEventStore,
    safe_atomic_write_text,
    safe_read_text,
)
from model_types import ACTOR_KINDS, ModelValidationError, new_event, validate_claim


ALLOWED_TRANSITIONS = {
    "candidate": frozenset({"confirmed", "rejected"}),
    "confirmed": frozenset({"superseded"}),
    "rejected": frozenset({"candidate"}),
    "superseded": frozenset(),
}

CLAIM_EVENT_TYPES = frozenset(
    {
        "claim.created",
        "claim.transitioned",
        "claim.reconsidered",
        "claim.correction_started",
        "claim.corrected",
    }
)

PUBLIC_IDEMPOTENCY_PREFIX = "claim:public:v1:"
INTERNAL_IDEMPOTENCY_PREFIX = "claim:internal:v1:"


class InvalidTransition(ValueError):
    """A rejected claim operation with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ClaimNotFound(InvalidTransition):
    def __init__(self, claim_id: str) -> None:
        super().__init__("claim_not_found", "claim does not exist: " + claim_id)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _key_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ClaimStore:
    """Maintain immutable claim events and a replayable current JSONL view."""

    def __init__(self, vault_dir: Path) -> None:
        vault = Path(os.path.abspath(str(vault_dir)))
        self.root = vault / "model" / "claims"
        self.events = JsonlEventStore(self.root / "events.jsonl")
        self.current_path = self.root / "current.jsonl"
        if self.events.exists():
            self._ensure_current()

    @staticmethod
    def _validate_write_metadata(
        *,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
    ) -> Dict[str, str]:
        valid_revision = (
            isinstance(expected_revision, int)
            and not isinstance(expected_revision, bool)
            and expected_revision >= 0
        )
        valid_actor = (
            isinstance(actor, Mapping)
            and isinstance(actor.get("kind"), str)
            and bool(actor.get("kind", "").strip())
            and isinstance(actor.get("id"), str)
            and bool(actor.get("id", "").strip())
        )
        if (
            not valid_revision
            or not isinstance(request_id, str)
            or not request_id.strip()
            or not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or not valid_actor
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise InvalidTransition(
                "write_metadata_required",
                "expected revision, request ID, idempotency key, actor, and reason are required",
            )
        if actor["kind"] not in ACTOR_KINDS:
            raise InvalidTransition(
                "invalid_actor",
                "actor kind is not allowed",
            )
        return {"kind": actor["kind"], "id": actor["id"]}

    @staticmethod
    def _public_idempotency_key(value: str) -> str:
        return PUBLIC_IDEMPOTENCY_PREFIX + _key_digest(value)

    @staticmethod
    def _internal_replacement_key(started: Mapping[str, Any]) -> str:
        return (
            INTERNAL_IDEMPOTENCY_PREFIX
            + "correction:"
            + _key_digest(str(started["event_id"]))
        )

    @staticmethod
    def _claim_corruption(message: str, exc: Optional[BaseException] = None) -> None:
        error = InvalidTransition("claim_event_corruption", message)
        if exc is None:
            raise error
        raise error from exc

    @classmethod
    def _event_claim(cls, event: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            stored = dict(event["payload"]["claim"])
        except (KeyError, TypeError, ValueError) as exc:
            cls._claim_corruption("claim event payload is malformed", exc)
        stored["based_on_event_seq"] = int(event["seq"])
        stored["stream_version"] = int(event["stream_version"])
        try:
            validate_claim(stored)
        except (ModelValidationError, KeyError, TypeError, ValueError) as exc:
            cls._claim_corruption("claim event contains an invalid claim", exc)
        return stored

    @classmethod
    def _require(cls, condition: bool, message: str) -> None:
        if not condition:
            cls._claim_corruption(message)

    @staticmethod
    def _without_projection_fields(value: Mapping[str, Any]) -> Dict[str, Any]:
        result = dict(value)
        result.pop("stream_version", None)
        return result

    @classmethod
    def _unchanged_except(
        cls,
        previous: Mapping[str, Any],
        candidate: Mapping[str, Any],
        allowed: set,
    ) -> None:
        old = cls._without_projection_fields(previous)
        new = cls._without_projection_fields(candidate)
        for field in allowed:
            old.pop(field, None)
            new.pop(field, None)
        cls._require(
            _canonical(old) == _canonical(new),
            "claim event changed fields outside its operation contract",
        )

    @classmethod
    def _validated_replacement(
        cls,
        started: Mapping[str, Any],
    ) -> Dict[str, Any]:
        try:
            payload = started["payload"]
            superseded = dict(payload["claim"])
            replacement = dict(payload["replacement"])
            operation = payload["operation"]
        except (KeyError, TypeError, ValueError) as exc:
            cls._claim_corruption("correction payload is malformed", exc)
        try:
            validate_claim(superseded)
            validate_claim(replacement)
        except (ModelValidationError, KeyError, TypeError, ValueError) as exc:
            cls._claim_corruption("correction payload contains an invalid claim", exc)
        cls._require(
            operation.get("method") == "correct",
            "correction operation method is invalid",
        )
        original_id = superseded.get("claim_id")
        cls._require(
            superseded.get("status") == "superseded",
            "correction must supersede the original claim",
        )
        cls._require(
            replacement.get("claim_id") != original_id
            and replacement.get("status") == "confirmed"
            and replacement.get("revision") == 1
            and replacement.get("supersedes") == original_id,
            "correction replacement relation is invalid",
        )
        cls._require(
            operation.get("claim_id") == original_id
            and operation.get("statement") == replacement.get("statement"),
            "correction operation does not match its replacement",
        )
        cls._unchanged_except(
            superseded,
            replacement,
            {
                "based_on_event_seq",
                "claim_id",
                "created_at",
                "revision",
                "statement",
                "status",
                "supersedes",
                "updated_at",
                "valid_from",
                "valid_to",
            },
        )
        return replacement

    @classmethod
    def _project_claim_event(
        cls,
        event: Mapping[str, Any],
        current: Dict[str, Dict[str, Any]],
        corrections: Dict[str, Mapping[str, Any]],
        completed: set,
    ) -> None:
        event_type = event["event_type"]
        if event_type not in CLAIM_EVENT_TYPES:
            return
        try:
            payload = event["payload"]
            operation = payload["operation"]
            claim = dict(payload["claim"])
            reason = payload["reason"]
        except (KeyError, TypeError, ValueError) as exc:
            cls._claim_corruption("claim event payload is malformed", exc)
        cls._require(
            isinstance(operation, Mapping),
            "claim operation must be an object",
        )
        projected = cls._event_claim(event)
        claim_id = projected["claim_id"]
        cls._require(
            event["stream_id"] == claim_id,
            "claim stream does not match payload claim_id",
        )

        if event_type == "claim.corrected":
            parent_id = operation.get("parent_event_id")
            parent = corrections.get(str(parent_id))
            cls._require(
                operation.get("method") == "correct.replacement"
                and parent is not None,
                "corrected claim does not reference a prior correction",
            )
            replacement = cls._validated_replacement(parent)
            cls._require(
                _canonical(claim) == _canonical(replacement),
                "corrected claim differs from pending replacement",
            )
            cls._require(
                event.get("previous_status") is None
                and int(event["expected_version"]) == 0
                and int(event["stream_version"]) == 1,
                "corrected claim stream state is invalid",
            )
            cls._require(
                event["actor"] == parent["actor"]
                and reason == parent["payload"]["reason"],
                "corrected claim actor or reason differs from its parent",
            )
            cls._require(
                claim_id not in current,
                "corrected claim_id already exists",
            )
            current[claim_id] = projected
            completed.add(str(parent_id))
            return

        cls._require(
            operation.get("actor") == event["actor"]
            and operation.get("reason") == reason,
            "claim event actor or reason differs from its operation",
        )
        previous = current.get(claim_id)

        if event_type == "claim.created":
            cls._require(
                operation.get("method") == "create"
                and previous is None
                and event.get("previous_status") is None
                and projected["status"] == "candidate"
                and projected["revision"] == 1
                and int(event["expected_version"]) == 0
                and int(event["stream_version"]) == 1,
                "claim.created semantics are invalid",
            )
            cls._require(
                _canonical(operation.get("claim"))
                == _canonical(claim),
                "claim.created payload differs from its operation",
            )
        else:
            cls._require(
                previous is not None,
                "claim mutation has no projected predecessor",
            )
            expected_revision = operation.get("expected_revision")
            cls._require(
                isinstance(expected_revision, int)
                and not isinstance(expected_revision, bool)
                and expected_revision == previous["revision"]
                and projected["revision"] == previous["revision"] + 1
                and event.get("previous_status") == previous["status"],
                "claim mutation revision or previous_status is invalid",
            )
            cls._require(
                operation.get("claim_id") == claim_id,
                "claim operation claim_id does not match its stream",
            )
            if event_type == "claim.transitioned":
                cls._require(
                    operation.get("method") == "transition"
                    and operation.get("status") == projected["status"]
                    and projected["status"]
                    in ALLOWED_TRANSITIONS.get(
                        previous["status"],
                        frozenset(),
                    ),
                    "claim.transitioned semantics are invalid",
                )
                cls._unchanged_except(
                    previous,
                    projected,
                    {
                        "based_on_event_seq",
                        "revision",
                        "status",
                        "updated_at",
                        "valid_to",
                    },
                )
            elif event_type == "claim.reconsidered":
                evidence = operation.get("evidence_ids")
                expected_evidence = list(
                    dict.fromkeys(
                        list(previous["evidence_ids"])
                        + (evidence if isinstance(evidence, list) else [])
                    )
                )
                cls._require(
                    operation.get("method") == "reconsider"
                    and previous["status"] == "rejected"
                    and projected["status"] == "candidate"
                    and isinstance(evidence, list)
                    and projected["evidence_ids"] == expected_evidence,
                    "claim.reconsidered semantics are invalid",
                )
                cls._unchanged_except(
                    previous,
                    projected,
                    {
                        "based_on_event_seq",
                        "evidence_ids",
                        "revision",
                        "status",
                        "updated_at",
                    },
                )
            elif event_type == "claim.correction_started":
                cls._require(
                    operation.get("method") == "correct"
                    and previous["status"] == "confirmed"
                    and projected["status"] == "superseded",
                    "claim.correction_started semantics are invalid",
                )
                cls._unchanged_except(
                    previous,
                    projected,
                    {
                        "based_on_event_seq",
                        "revision",
                        "status",
                        "updated_at",
                        "valid_to",
                    },
                )
                cls._validated_replacement(event)
                corrections[str(event["event_id"])] = event
        current[claim_id] = projected

    def _scan_claim_events(
        self,
    ) -> Tuple[
        Dict[str, Dict[str, Any]],
        int,
        List[Mapping[str, Any]],
    ]:
        current: Dict[str, Dict[str, Any]] = {}
        corrections: Dict[str, Mapping[str, Any]] = {}
        completed = set()
        head = 0
        for event in self.events.iter_all():
            head = int(event["seq"])
            self._project_claim_event(
                event,
                current,
                corrections,
                completed,
            )
        pending = [
            event
            for event_id, event in corrections.items()
            if event_id not in completed
        ]
        return current, head, pending

    def _replay(self) -> Tuple[Dict[str, Dict[str, Any]], int]:
        current, head, _pending = self._scan_claim_events()
        return current, head

    def _read_current(self) -> Optional[Dict[str, Dict[str, Any]]]:
        try:
            content = safe_read_text(self.current_path)
        except (OSError, UnicodeDecodeError):
            return None
        if content is None:
            return None
        current: Dict[str, Dict[str, Any]] = {}
        try:
            for line in content.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    return None
                validate_claim(row)
                if (
                    not isinstance(row.get("stream_version"), int)
                    or isinstance(row.get("stream_version"), bool)
                    or int(row["stream_version"]) < 1
                ):
                    return None
                claim_id = str(row["claim_id"])
                if claim_id in current:
                    return None
                current[claim_id] = row
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ModelValidationError):
            return None
        return current

    def _write_current(self, claims: Mapping[str, Mapping[str, Any]]) -> None:
        rows = [
            json.dumps(
                claims[claim_id],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for claim_id in sorted(claims)
        ]
        safe_atomic_write_text(
            self.current_path,
            ("\n".join(rows) + "\n") if rows else "",
        )

    def _refresh_current(self) -> Dict[str, Dict[str, Any]]:
        while True:
            current, replay_head = self._replay()
            self._write_current(current)
            if self.events.watermark() == replay_head:
                return current

    def _ensure_current(self) -> Dict[str, Dict[str, Any]]:
        if not self.events.exists():
            return {}
        self._recover_pending_corrections()
        while True:
            projected, replay_head = self._replay()
            current = self._read_current()
            if current is None or _canonical(current) != _canonical(projected):
                self._write_current(projected)
                current = projected
            if self.events.watermark() == replay_head:
                return current

    def _find_idempotent_by_keys(
        self,
        idempotency_keys: List[str],
        operation: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        self._scan_claim_events()
        requested_keys = set(idempotency_keys)
        for event in self.events.iter_all():
            if event["idempotency_key"] not in requested_keys:
                continue
            existing = event["payload"].get("operation")
            if not isinstance(existing, Mapping) or _canonical(existing) != _canonical(operation):
                raise InvalidTransition(
                    "idempotency_conflict",
                    "idempotency key was reused for a different intent",
                )
            return event
        return None

    def _find_public_idempotent(
        self,
        idempotency_key: str,
        operation: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        return self._find_idempotent_by_keys(
            [self._public_idempotency_key(idempotency_key), idempotency_key],
            operation,
        )

    def _append(
        self,
        *,
        event_type: str,
        claim: Mapping[str, Any],
        operation: Mapping[str, Any],
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
        previous_status: Optional[str],
        extra_payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {
            "claim": dict(claim),
            "operation": dict(operation),
            "reason": reason,
        }
        if extra_payload:
            payload.update(extra_payload)
        event = new_event(
            event_type=event_type,
            stream_id=str(claim["claim_id"]),
            stream_version=expected_version + 1,
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            payload=payload,
            previous_status=previous_status,
        )
        try:
            return self.events.append(event)
        except EventConflict as exc:
            raise InvalidTransition(exc.code, str(exc)) from exc

    def _return_projected(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        self._refresh_current()
        return self._event_claim(event)

    def create(
        self,
        claim: Mapping[str, Any],
        *,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
    ) -> Dict[str, Any]:
        actor_value = self._validate_write_metadata(
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )
        if not isinstance(claim, Mapping):
            raise InvalidTransition(
                "invalid_claim",
                "claim must be an object",
            )
        candidate = dict(claim)
        try:
            validate_claim(candidate)
        except ModelValidationError as exc:
            raise InvalidTransition(exc.code, str(exc)) from exc
        if expected_revision != 0:
            raise InvalidTransition(
                "version_conflict",
                "claim creation requires expected revision 0",
            )
        if int(candidate["revision"]) != 1:
            raise InvalidTransition(
                "version_conflict",
                "a new claim must start at revision 1",
            )
        if candidate["status"] != "candidate":
            raise InvalidTransition(
                "invalid_transition",
                "a new claim must start as candidate",
            )
        operation = {
            "actor": actor_value,
            "claim": candidate,
            "expected_revision": expected_revision,
            "method": "create",
            "reason": reason,
        }
        previous = self._find_public_idempotent(idempotency_key, operation)
        if previous is not None:
            return self._return_projected(previous)
        if self.events.stream_version(str(candidate["claim_id"])) != 0:
            raise InvalidTransition("version_conflict", "claim already exists")
        stored = dict(candidate)
        stored["based_on_event_seq"] = 0
        event = self._append(
            event_type="claim.created",
            claim=stored,
            operation=operation,
            expected_version=0,
            request_id=request_id,
            idempotency_key=self._public_idempotency_key(idempotency_key),
            actor=actor_value,
            reason=reason,
            previous_status=None,
        )
        return self._return_projected(event)

    def get(self, claim_id: str) -> Dict[str, Any]:
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise InvalidTransition(
                "claim_id_required",
                "claim_id must be a non-empty string",
            )
        current = self._ensure_current()
        try:
            return dict(current[claim_id])
        except KeyError as exc:
            raise ClaimNotFound(claim_id) from exc

    def list(self) -> List[Dict[str, Any]]:
        current = self._ensure_current()
        return [dict(current[claim_id]) for claim_id in sorted(current)]

    def transition(
        self,
        claim_id: str,
        status: str,
        *,
        reason: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
    ) -> Dict[str, Any]:
        actor_value = self._validate_write_metadata(
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )
        operation = {
            "actor": actor_value,
            "claim_id": claim_id,
            "expected_revision": expected_revision,
            "method": "transition",
            "reason": reason,
            "status": status,
        }
        previous = self._find_public_idempotent(idempotency_key, operation)
        if previous is not None:
            return self._return_projected(previous)
        claim = self.get(claim_id)
        if int(claim["revision"]) != expected_revision:
            raise InvalidTransition("version_conflict", "claim revision changed")
        if status == "candidate" and claim["status"] == "rejected":
            raise InvalidTransition(
                "new_evidence_required",
                "rejected claims must be reconsidered with new evidence",
            )
        if status not in ALLOWED_TRANSITIONS.get(claim["status"], frozenset()):
            raise InvalidTransition(
                "invalid_transition",
                str(claim["status"]) + " cannot transition to " + str(status),
            )
        updated = dict(claim)
        updated.pop("stream_version", None)
        updated.update(
            {
                "revision": expected_revision + 1,
                "status": status,
                "updated_at": _utc_now(),
                "based_on_event_seq": 0,
            }
        )
        if status == "superseded":
            updated["valid_to"] = updated["updated_at"]
        event = self._append(
            event_type="claim.transitioned",
            claim=updated,
            operation=operation,
            expected_version=int(claim["stream_version"]),
            request_id=request_id,
            idempotency_key=self._public_idempotency_key(idempotency_key),
            actor=actor_value,
            reason=reason,
            previous_status=str(claim["status"]),
        )
        return self._return_projected(event)

    def reconsider(
        self,
        claim_id: str,
        *,
        evidence_ids: List[str],
        reason: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
    ) -> Dict[str, Any]:
        actor_value = self._validate_write_metadata(
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )
        if (
            not isinstance(evidence_ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
        ):
            raise InvalidTransition(
                "new_evidence_required",
                "reconsideration requires valid new evidence",
            )
        operation = {
            "actor": actor_value,
            "claim_id": claim_id,
            "evidence_ids": list(evidence_ids),
            "expected_revision": expected_revision,
            "method": "reconsider",
            "reason": reason,
        }
        previous = self._find_public_idempotent(idempotency_key, operation)
        if previous is not None:
            return self._return_projected(previous)
        claim = self.get(claim_id)
        if int(claim["revision"]) != expected_revision:
            raise InvalidTransition("version_conflict", "claim revision changed")
        if claim["status"] != "rejected":
            raise InvalidTransition(
                "invalid_transition",
                "only rejected claims can be reconsidered",
            )
        new_evidence = [
            item for item in evidence_ids if item not in claim["evidence_ids"]
        ]
        if not new_evidence:
            raise InvalidTransition(
                "new_evidence_required",
                "reconsideration requires evidence not already on the claim",
            )
        updated = dict(claim)
        updated.pop("stream_version", None)
        updated.update(
            {
                "revision": expected_revision + 1,
                "status": "candidate",
                "evidence_ids": list(
                    dict.fromkeys(list(claim["evidence_ids"]) + evidence_ids)
                ),
                "updated_at": _utc_now(),
                "based_on_event_seq": 0,
            }
        )
        event = self._append(
            event_type="claim.reconsidered",
            claim=updated,
            operation=operation,
            expected_version=int(claim["stream_version"]),
            request_id=request_id,
            idempotency_key=self._public_idempotency_key(idempotency_key),
            actor=actor_value,
            reason=reason,
            previous_status="rejected",
        )
        return self._return_projected(event)

    def _append_replacement(
        self,
        started: Mapping[str, Any],
    ) -> Dict[str, Any]:
        replacement = self._validated_replacement(started)
        operation = {
            "method": "correct.replacement",
            "parent_event_id": str(started["event_id"]),
        }
        derived_key = self._internal_replacement_key(started)
        existing = self._find_idempotent_by_keys([derived_key], operation)
        if existing is None:
            existing = self._append(
                event_type="claim.corrected",
                claim=replacement,
                operation=operation,
                expected_version=0,
                request_id=str(started["request_id"]) + ":replacement",
                idempotency_key=derived_key,
                actor=dict(started["actor"]),
                reason=str(started["payload"]["reason"]),
                previous_status=None,
            )
        return existing

    def _recover_pending_corrections(self) -> None:
        _current, _head, pending = self._scan_claim_events()
        for event in pending:
            self._append_replacement(event)

    def _finish_correction(
        self,
        started: Mapping[str, Any],
    ) -> Dict[str, Any]:
        existing = self._append_replacement(started)
        return self._return_projected(existing)

    def correct(
        self,
        claim_id: str,
        statement: str,
        *,
        reason: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
    ) -> Dict[str, Any]:
        actor_value = self._validate_write_metadata(
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )
        if not isinstance(statement, str) or not statement.strip():
            raise InvalidTransition(
                "statement_required",
                "corrected statement must not be empty",
            )
        operation = {
            "actor": actor_value,
            "claim_id": claim_id,
            "expected_revision": expected_revision,
            "method": "correct",
            "reason": reason,
            "statement": statement.strip(),
        }
        previous = self._find_public_idempotent(idempotency_key, operation)
        if previous is not None:
            return self._finish_correction(previous)
        claim = self.get(claim_id)
        if int(claim["revision"]) != expected_revision:
            raise InvalidTransition("version_conflict", "claim revision changed")
        if claim["status"] != "confirmed":
            raise InvalidTransition(
                "invalid_transition",
                "only confirmed claims can be corrected",
            )
        generated = _utc_now()
        superseded = dict(claim)
        superseded.pop("stream_version", None)
        superseded.update(
            {
                "revision": expected_revision + 1,
                "status": "superseded",
                "updated_at": generated,
                "valid_to": generated,
                "based_on_event_seq": 0,
            }
        )
        replacement = dict(claim)
        replacement.pop("stream_version", None)
        replacement.update(
            {
                "revision": 1,
                "claim_id": "clm_" + uuid.uuid4().hex,
                "statement": statement.strip(),
                "status": "confirmed",
                "created_at": generated,
                "updated_at": generated,
                "valid_from": generated,
                "valid_to": None,
                "based_on_event_seq": 0,
                "supersedes": claim_id,
            }
        )
        validate_claim(superseded)
        validate_claim(replacement)
        started = self._append(
            event_type="claim.correction_started",
            claim=superseded,
            operation=operation,
            expected_version=int(claim["stream_version"]),
            request_id=request_id,
            idempotency_key=self._public_idempotency_key(idempotency_key),
            actor=actor_value,
            reason=reason,
            previous_status="confirmed",
            extra_payload={"replacement": replacement},
        )
        return self._finish_correction(started)
