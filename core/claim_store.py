#!/usr/bin/env python3
"""Event-backed claim storage with an explicit correction state machine."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from event_store import EventConflict, JsonlEventStore
from file_utils import atomic_write_text
from model_types import ModelValidationError, new_event, validate_claim


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


class ClaimStore:
    """Maintain immutable claim events and a replayable current JSONL view."""

    def __init__(self, vault_dir: Path) -> None:
        self.root = Path(vault_dir) / "model" / "claims"
        self.events = JsonlEventStore(self.root / "events.jsonl")
        self.current_path = self.root / "current.jsonl"
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
        return {"kind": actor["kind"], "id": actor["id"]}

    @staticmethod
    def _event_claim(event: Mapping[str, Any]) -> Dict[str, Any]:
        stored = dict(event["payload"]["claim"])
        stored["based_on_event_seq"] = int(event["seq"])
        stored["stream_version"] = int(event["stream_version"])
        validate_claim(stored)
        return stored

    def _replay(self) -> Tuple[Dict[str, Dict[str, Any]], int]:
        current: Dict[str, Dict[str, Any]] = {}
        head = 0
        for event in self.events.read_all():
            head = int(event["seq"])
            event_type = event["event_type"]
            if event_type in CLAIM_EVENT_TYPES:
                projected = self._event_claim(event)
                current[projected["claim_id"]] = projected
        return current, head

    def _read_current(self) -> Optional[Dict[str, Dict[str, Any]]]:
        if not self.current_path.is_file():
            return None
        current: Dict[str, Dict[str, Any]] = {}
        try:
            with self.current_path.open("r", encoding="utf-8") as handle:
                for line in handle:
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
        atomic_write_text(
            self.current_path,
            ("\n".join(rows) + "\n") if rows else "",
        )

    @staticmethod
    def _view_watermark(claims: Mapping[str, Mapping[str, Any]]) -> int:
        return max(
            (int(claim.get("based_on_event_seq", 0)) for claim in claims.values()),
            default=0,
        )

    def _refresh_current(self) -> Dict[str, Dict[str, Any]]:
        while True:
            current, replay_head = self._replay()
            self._write_current(current)
            if self.events.watermark() == replay_head:
                return current

    def _ensure_current(self) -> Dict[str, Dict[str, Any]]:
        current = self._read_current()
        events = self.events.read_all()
        head = int(events[-1]["seq"]) if events else 0
        event_claim_ids = {
            str(event["payload"]["claim"]["claim_id"])
            for event in events
            if event["event_type"] in CLAIM_EVENT_TYPES
        }
        if (
            current is None
            or self._view_watermark(current) != head
            or set(current) != event_claim_ids
        ):
            return self._refresh_current()
        return current

    def _find_idempotent(
        self,
        idempotency_key: str,
        operation: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        for event in self.events.read_all():
            if event["idempotency_key"] != idempotency_key:
                continue
            existing = event["payload"].get("operation")
            if not isinstance(existing, Mapping) or _canonical(existing) != _canonical(operation):
                raise InvalidTransition(
                    "idempotency_conflict",
                    "idempotency key was reused for a different intent",
                )
            return event
        return None

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
        previous = self._find_idempotent(idempotency_key, operation)
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
            idempotency_key=idempotency_key,
            actor=actor_value,
            reason=reason,
            previous_status=None,
        )
        return self._return_projected(event)

    def get(self, claim_id: str) -> Dict[str, Any]:
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
        previous = self._find_idempotent(idempotency_key, operation)
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
            idempotency_key=idempotency_key,
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
        operation = {
            "actor": actor_value,
            "claim_id": claim_id,
            "evidence_ids": list(evidence_ids),
            "expected_revision": expected_revision,
            "method": "reconsider",
            "reason": reason,
        }
        previous = self._find_idempotent(idempotency_key, operation)
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
        if (
            not isinstance(evidence_ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
        ):
            raise InvalidTransition(
                "new_evidence_required",
                "reconsideration requires valid new evidence",
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
            idempotency_key=idempotency_key,
            actor=actor_value,
            reason=reason,
            previous_status="rejected",
        )
        return self._return_projected(event)

    def _finish_correction(
        self,
        started: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
    ) -> Dict[str, Any]:
        replacement = dict(started["payload"]["replacement"])
        operation = {
            "method": "correct.replacement",
            "parent_idempotency_key": idempotency_key,
        }
        derived_key = idempotency_key + ":replacement"
        existing = self._find_idempotent(derived_key, operation)
        if existing is None:
            existing = self._append(
                event_type="claim.corrected",
                claim=replacement,
                operation=operation,
                expected_version=0,
                request_id=request_id + ":replacement",
                idempotency_key=derived_key,
                actor=actor,
                reason=reason,
                previous_status=None,
            )
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
        previous = self._find_idempotent(idempotency_key, operation)
        if previous is not None:
            return self._finish_correction(
                previous,
                request_id=request_id,
                idempotency_key=idempotency_key,
                actor=actor_value,
                reason=reason,
            )
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
            idempotency_key=idempotency_key,
            actor=actor_value,
            reason=reason,
            previous_status="confirmed",
            extra_payload={"replacement": replacement},
        )
        return self._finish_correction(
            started,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor_value,
            reason=reason,
        )
