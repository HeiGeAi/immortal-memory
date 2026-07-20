#!/usr/bin/env python3
"""Event-backed judgment cards with replayable compatibility views."""

from __future__ import annotations

import hashlib
import json
import os
import sys
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
from model_types import (
    ACTOR_KINDS,
    ModelValidationError,
    new_event,
    new_judgment_card,
    validate_judgment_card,
)


ALLOWED_TRANSITIONS = {
    "candidate": frozenset({"confirmed", "rejected"}),
    "confirmed": frozenset({"candidate", "retired"}),
    "rejected": frozenset(),
    "retired": frozenset(),
}

JUDGMENT_EVENT_TYPES = frozenset(
    {
        "judgment.created",
        "judgment.transitioned",
        "judgment.corrected",
        "judgment.outcome_recorded",
    }
)

CORRECTABLE_FIELDS = frozenset(
    {
        "title",
        "situation",
        "goal",
        "constraints",
        "signals",
        "decision",
        "alternatives",
        "lesson",
        "next_trigger",
        "evidence_ids",
        "claim_ids",
        "privacy",
    }
)

PUBLIC_IDEMPOTENCY_PREFIX = "judgment:public:v1:"


class InvalidJudgmentOperation(ValueError):
    """A rejected operation with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JudgmentNotFound(InvalidJudgmentOperation):
    def __init__(self, card_id: str) -> None:
        super().__init__("judgment_not_found", "judgment does not exist: " + card_id)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _card_body(value: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(value)
    result.pop("revision", None)
    result.pop("stream_version", None)
    result.pop("based_on_event_seq", None)
    return result


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise InvalidJudgmentOperation(
            "invalid_timestamp",
            "observed_at must be a timezone-aware ISO-8601 timestamp",
        )
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise InvalidJudgmentOperation(
            "invalid_timestamp",
            "observed_at must be a timezone-aware ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidJudgmentOperation(
            "invalid_timestamp",
            "observed_at must include a UTC offset",
        )
    return parsed


class JudgmentStore:
    """Maintain immutable judgment events and deterministic derived views."""

    def __init__(self, vault_dir: Path) -> None:
        absolute = os.path.abspath(str(vault_dir))
        if sys.platform == "darwin" and (
            absolute == "/var" or absolute.startswith("/var/")
        ):
            absolute = "/private" + absolute
        vault = Path(absolute)
        self.root = vault / "judgment"
        self.events = JsonlEventStore(self.root / "events.jsonl")
        self.current_path = self.root / "current.jsonl"
        self.evaluations_path = self.root / "evaluations.jsonl"
        if self.events.exists():
            self._ensure_views()

    @staticmethod
    def _public_key(value: str) -> str:
        return PUBLIC_IDEMPOTENCY_PREFIX + _digest(value)

    @staticmethod
    def _metadata(
        *,
        expected_revision: Optional[int],
        current_revision: int,
        request_id: Optional[str],
        idempotency_key: Optional[str],
        actor: Optional[Mapping[str, str]],
        reason: Optional[str],
        default_reason: str,
    ) -> Tuple[int, str, str, Dict[str, str], str]:
        expected = current_revision if expected_revision is None else expected_revision
        actor_value = {"kind": "owner", "id": "owner"} if actor is None else dict(actor)
        request = "req_" + uuid.uuid4().hex if request_id is None else request_id
        idempotency = (
            "idem_" + uuid.uuid4().hex
            if idempotency_key is None
            else idempotency_key
        )
        reason_value = default_reason if reason is None else reason
        valid_revision = (
            isinstance(expected, int)
            and not isinstance(expected, bool)
            and expected >= 0
        )
        valid_actor = (
            isinstance(actor_value.get("kind"), str)
            and bool(actor_value["kind"].strip())
            and isinstance(actor_value.get("id"), str)
            and bool(actor_value["id"].strip())
        )
        if (
            not valid_revision
            or not isinstance(request, str)
            or not request.strip()
            or not isinstance(idempotency, str)
            or not idempotency.strip()
            or not valid_actor
            or not isinstance(reason_value, str)
            or not reason_value.strip()
        ):
            raise InvalidJudgmentOperation(
                "write_metadata_required",
                "expected revision, request ID, idempotency key, actor, and reason are required",
            )
        if actor_value["kind"] not in ACTOR_KINDS:
            raise InvalidJudgmentOperation("invalid_actor", "actor kind is not allowed")
        return (
            int(expected),
            request.strip(),
            idempotency.strip(),
            {"kind": actor_value["kind"], "id": actor_value["id"]},
            reason_value.strip(),
        )

    @staticmethod
    def _corruption(message: str, exc: Optional[BaseException] = None) -> None:
        error = InvalidJudgmentOperation("judgment_event_corruption", message)
        if exc is None:
            raise error
        raise error from exc

    @classmethod
    def _require(cls, condition: bool, message: str) -> None:
        if not condition:
            cls._corruption(message)

    @classmethod
    def _event_card(cls, event: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            card = dict(event["payload"]["card"])
            validate_judgment_card(card)
            revision = int(event["stream_version"])
            event_seq = int(event["seq"])
        except (KeyError, TypeError, ValueError, ModelValidationError) as exc:
            cls._corruption("judgment event contains an invalid card", exc)
        card["revision"] = revision
        card["stream_version"] = revision
        card["based_on_event_seq"] = event_seq
        return card

    @classmethod
    def _unchanged_except(
        cls,
        previous: Mapping[str, Any],
        candidate: Mapping[str, Any],
        allowed: set,
    ) -> None:
        old = _card_body(previous)
        new = _card_body(candidate)
        for field in allowed:
            old.pop(field, None)
            new.pop(field, None)
        cls._require(
            _canonical(old) == _canonical(new),
            "judgment event changed fields outside its operation contract",
        )

    @classmethod
    def _project_event(
        cls,
        event: Mapping[str, Any],
        current: Dict[str, Dict[str, Any]],
        evaluations: List[Dict[str, Any]],
    ) -> None:
        event_type = event.get("event_type")
        if event_type not in JUDGMENT_EVENT_TYPES:
            return
        try:
            payload = event["payload"]
            operation = payload["operation"]
            reason = payload["reason"]
            raw_card = dict(payload["card"])
        except (KeyError, TypeError, ValueError) as exc:
            cls._corruption("judgment event payload is malformed", exc)
        cls._require(isinstance(operation, Mapping), "operation must be an object")
        cls._require(
            operation.get("actor") == event.get("actor")
            and operation.get("reason") == reason,
            "event actor or reason differs from its operation",
        )
        projected = cls._event_card(event)
        card_id = projected["card_id"]
        cls._require(
            event.get("stream_id") == card_id,
            "event stream does not match its card",
        )
        previous = current.get(card_id)

        if event_type == "judgment.created":
            input_fields = operation.get("input")
            cls._require(
                previous is None
                and operation.get("method") == "create"
                and operation.get("expected_revision") == 0
                and isinstance(input_fields, Mapping)
                and all(raw_card.get(key) == value for key, value in input_fields.items())
                and event.get("previous_status") is None
                and int(event["expected_version"]) == 0
                and int(event["stream_version"]) == 1
                and projected["status"] == "candidate",
                "judgment.created semantics are invalid",
            )
            current[card_id] = projected
            return

        cls._require(previous is not None, "judgment mutation has no predecessor")
        cls._require(
            operation.get("card_id") == card_id
            and operation.get("expected_revision") == previous["revision"]
            and int(event["expected_version"]) == previous["stream_version"]
            and projected["revision"] == previous["revision"] + 1
            and event.get("previous_status") == previous["status"],
            "judgment mutation revision or status is invalid",
        )

        if event_type == "judgment.transitioned":
            cls._require(
                operation.get("method") == "transition"
                and operation.get("status") == projected["status"]
                and projected["status"]
                in ALLOWED_TRANSITIONS.get(previous["status"], frozenset()),
                "judgment.transitioned semantics are invalid",
            )
            cls._unchanged_except(
                previous,
                projected,
                {"status", "updated_at"},
            )
        elif event_type == "judgment.corrected":
            changes = operation.get("changes")
            cls._require(
                operation.get("method") == "correct"
                and isinstance(changes, Mapping)
                and bool(changes)
                and set(changes).issubset(CORRECTABLE_FIELDS)
                and all(projected.get(key) == value for key, value in changes.items())
                and projected["status"]
                == (
                    "candidate"
                    if previous["status"] in {"confirmed", "rejected"}
                    else previous["status"]
                )
                and previous["status"] != "retired",
                "judgment.corrected semantics are invalid",
            )
            cls._unchanged_except(
                previous,
                projected,
                set(changes) | {"status", "updated_at"},
            )
        else:
            outcome = operation.get("outcome")
            cls._require(
                operation.get("method") == "record_outcome"
                and previous["status"] == "confirmed"
                and isinstance(outcome, Mapping)
                and projected["outcome"] == outcome,
                "judgment.outcome_recorded semantics are invalid",
            )
            cls._unchanged_except(
                previous,
                projected,
                {"outcome", "updated_at"},
            )
            evaluations.append(
                {
                    "card_id": card_id,
                    "event_seq": int(event["seq"]),
                    "observed_at": projected["outcome"]["observed_at"],
                    "status": projected["outcome"]["status"],
                    "summary": projected["outcome"]["summary"],
                }
            )
        current[card_id] = projected

    def _replay(
        self,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], int]:
        current: Dict[str, Dict[str, Any]] = {}
        evaluations: List[Dict[str, Any]] = []
        head = 0
        for event in self.events.iter_all():
            head = int(event["seq"])
            self._project_event(event, current, evaluations)
        return current, evaluations, head

    @staticmethod
    def _serialize_rows(rows: List[Mapping[str, Any]]) -> str:
        return (
            "".join(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for row in rows
            )
            if rows
            else ""
        )

    def _write_views(
        self,
        current: Mapping[str, Mapping[str, Any]],
        evaluations: List[Mapping[str, Any]],
    ) -> None:
        safe_atomic_write_text(
            self.current_path,
            self._serialize_rows([current[key] for key in sorted(current)]),
        )
        safe_atomic_write_text(
            self.evaluations_path,
            self._serialize_rows(evaluations),
        )

    def _views_match(
        self,
        current: Mapping[str, Mapping[str, Any]],
        evaluations: List[Mapping[str, Any]],
    ) -> bool:
        try:
            return (
                safe_read_text(self.current_path)
                == self._serialize_rows([current[key] for key in sorted(current)])
                and safe_read_text(self.evaluations_path)
                == self._serialize_rows(evaluations)
            )
        except (OSError, UnicodeDecodeError):
            return False

    def _ensure_views(self) -> Dict[str, Dict[str, Any]]:
        if not self.events.exists():
            return {}
        while True:
            current, evaluations, head = self._replay()
            if not self._views_match(current, evaluations):
                self._write_views(current, evaluations)
            if self.events.watermark() == head:
                return current

    def _find_idempotent(
        self,
        idempotency_key: str,
        operation: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        requested = {idempotency_key, self._public_key(idempotency_key)}
        for event in self.events.iter_all():
            if event["idempotency_key"] not in requested:
                continue
            existing = event["payload"].get("operation")
            if not isinstance(existing, Mapping) or _canonical(existing) != _canonical(operation):
                raise InvalidJudgmentOperation(
                    "idempotency_conflict",
                    "idempotency key was reused for a different intent",
                )
            return event
        return None

    def _append(
        self,
        *,
        event_type: str,
        card: Mapping[str, Any],
        operation: Mapping[str, Any],
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
        expected_version: int,
        previous_status: Optional[str],
    ) -> Dict[str, Any]:
        event = new_event(
            event_type=event_type,
            stream_id=str(card["card_id"]),
            stream_version=expected_version + 1,
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=self._public_key(idempotency_key),
            actor=actor,
            payload={
                "card": _card_body(card),
                "operation": dict(operation),
                "reason": reason,
            },
            previous_status=previous_status,
        )
        try:
            return self.events.append(event)
        except EventConflict as exc:
            raise InvalidJudgmentOperation(exc.code, str(exc)) from exc

    def _return_projected(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        self._ensure_views()
        return self._event_card(event)

    def create(
        self,
        *,
        title: str,
        situation: str,
        decision: str,
        evidence_ids: List[str],
        goal: str = "",
        constraints: Optional[List[str]] = None,
        signals: Optional[List[str]] = None,
        alternatives: Optional[List[str]] = None,
        claim_ids: Optional[List[str]] = None,
        lesson: str = "",
        next_trigger: str = "",
        privacy: str = "restricted",
        card_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        actor: Optional[Mapping[str, str]] = None,
        reason: Optional[str] = None,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        expected, request, idem, actor_value, reason_value = self._metadata(
            expected_revision=expected_revision,
            current_revision=0,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
            default_reason="judgment created",
        )
        try:
            card = new_judgment_card(
                title=title,
                situation=situation,
                decision=decision,
                evidence_ids=evidence_ids,
                goal=goal,
                constraints=constraints,
                signals=signals,
                alternatives=alternatives,
                claim_ids=claim_ids,
                lesson=lesson,
                next_trigger=next_trigger,
                privacy=privacy,
                now=now,
            )
            if card_id is not None:
                card["card_id"] = card_id
                validate_judgment_card(card)
        except ModelValidationError as exc:
            raise InvalidJudgmentOperation(exc.code, str(exc)) from exc
        if expected != 0:
            raise InvalidJudgmentOperation(
                "version_conflict",
                "judgment creation requires expected revision 0",
            )
        intent = {
            "alternatives": card["alternatives"],
            "claim_ids": card["claim_ids"],
            "constraints": card["constraints"],
            "decision": card["decision"],
            "evidence_ids": card["evidence_ids"],
            "goal": card["goal"],
            "lesson": card["lesson"],
            "next_trigger": card["next_trigger"],
            "privacy": card["privacy"],
            "signals": card["signals"],
            "situation": card["situation"],
            "title": card["title"],
        }
        if card_id is not None:
            intent["card_id"] = card_id
        operation = {
            "actor": actor_value,
            "expected_revision": 0,
            "input": intent,
            "method": "create",
            "reason": reason_value,
        }
        prior = self._find_idempotent(idem, operation)
        if prior is not None:
            return self._return_projected(prior)
        if self.events.stream_version(card["card_id"]) != 0:
            raise InvalidJudgmentOperation(
                "version_conflict",
                "judgment already exists",
            )
        event = self._append(
            event_type="judgment.created",
            card=card,
            operation=operation,
            request_id=request,
            idempotency_key=idem,
            actor=actor_value,
            reason=reason_value,
            expected_version=0,
            previous_status=None,
        )
        return self._return_projected(event)

    def get(self, card_id: str) -> Dict[str, Any]:
        if not isinstance(card_id, str) or not card_id.strip():
            raise InvalidJudgmentOperation(
                "card_id_required",
                "card_id must be a non-empty string",
            )
        current = self._ensure_views()
        try:
            return dict(current[card_id])
        except KeyError as exc:
            raise JudgmentNotFound(card_id) from exc

    def list(self) -> List[Dict[str, Any]]:
        current = self._ensure_views()
        return [dict(current[key]) for key in sorted(current)]

    def transition(
        self,
        card_id: str,
        status: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        actor: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        current = self.get(card_id)
        expected, request, idem, actor_value, reason_value = self._metadata(
            expected_revision=expected_revision,
            current_revision=current["revision"],
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
            default_reason="judgment status changed",
        )
        operation = {
            "actor": actor_value,
            "card_id": card_id,
            "expected_revision": expected,
            "method": "transition",
            "reason": reason_value,
            "status": status,
        }
        prior = self._find_idempotent(idem, operation)
        if prior is not None:
            return self._return_projected(prior)
        if current["revision"] != expected:
            raise InvalidJudgmentOperation(
                "version_conflict",
                "judgment revision changed",
            )
        if status not in ALLOWED_TRANSITIONS.get(current["status"], frozenset()):
            raise InvalidJudgmentOperation(
                "invalid_transition",
                str(current["status"]) + " cannot transition to " + str(status),
            )
        updated = _card_body(current)
        updated["status"] = status
        updated["updated_at"] = _utc_now()
        validate_judgment_card(updated)
        event = self._append(
            event_type="judgment.transitioned",
            card=updated,
            operation=operation,
            request_id=request,
            idempotency_key=idem,
            actor=actor_value,
            reason=reason_value,
            expected_version=current["stream_version"],
            previous_status=current["status"],
        )
        return self._return_projected(event)

    def correct(
        self,
        card_id: str,
        *,
        changes: Mapping[str, Any],
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        actor: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        current = self.get(card_id)
        expected, request, idem, actor_value, reason_value = self._metadata(
            expected_revision=expected_revision,
            current_revision=current["revision"],
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
            default_reason="judgment corrected",
        )
        if (
            not isinstance(changes, Mapping)
            or not changes
            or not set(changes).issubset(CORRECTABLE_FIELDS)
        ):
            raise InvalidJudgmentOperation(
                "invalid_correction",
                "correction must contain only supported judgment fields",
            )
        operation = {
            "actor": actor_value,
            "card_id": card_id,
            "changes": dict(changes),
            "expected_revision": expected,
            "method": "correct",
            "reason": reason_value,
        }
        prior = self._find_idempotent(idem, operation)
        if prior is not None:
            return self._return_projected(prior)
        if current["revision"] != expected:
            raise InvalidJudgmentOperation(
                "version_conflict",
                "judgment revision changed",
            )
        if current["status"] == "retired":
            raise InvalidJudgmentOperation(
                "invalid_transition",
                "retired judgments cannot be corrected",
            )
        updated = _card_body(current)
        updated.update(dict(changes))
        if current["status"] in {"confirmed", "rejected"}:
            updated["status"] = "candidate"
        updated["updated_at"] = _utc_now()
        try:
            validate_judgment_card(updated)
        except ModelValidationError as exc:
            raise InvalidJudgmentOperation(exc.code, str(exc)) from exc
        event = self._append(
            event_type="judgment.corrected",
            card=updated,
            operation=operation,
            request_id=request,
            idempotency_key=idem,
            actor=actor_value,
            reason=reason_value,
            expected_version=current["stream_version"],
            previous_status=current["status"],
        )
        return self._return_projected(event)

    def record_outcome(
        self,
        card_id: str,
        *,
        status: str,
        summary: str,
        observed_at: str,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        actor: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        current = self.get(card_id)
        expected, request, idem, actor_value, reason_value = self._metadata(
            expected_revision=expected_revision,
            current_revision=current["revision"],
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
            default_reason="judgment outcome recorded",
        )
        observed = _parse_timestamp(observed_at)
        now = datetime.now(timezone.utc)
        updated_at = max(now, observed).isoformat()
        outcome = {
            "status": status,
            "summary": summary,
            "observed_at": observed.isoformat(),
        }
        operation = {
            "actor": actor_value,
            "card_id": card_id,
            "expected_revision": expected,
            "method": "record_outcome",
            "outcome": outcome,
            "reason": reason_value,
        }
        prior = self._find_idempotent(idem, operation)
        if prior is not None:
            return self._return_projected(prior)
        if current["revision"] != expected:
            raise InvalidJudgmentOperation(
                "version_conflict",
                "judgment revision changed",
            )
        if current["status"] != "confirmed":
            raise InvalidJudgmentOperation(
                "invalid_transition",
                "only confirmed judgments can record outcomes",
            )
        updated = _card_body(current)
        updated["outcome"] = outcome
        updated["updated_at"] = updated_at
        try:
            validate_judgment_card(updated)
        except ModelValidationError as exc:
            code = (
                "invalid_timestamp"
                if exc.code
                in {
                    "invalid_timestamp",
                    "invalid_time_order",
                    "outcome_observed_at_required",
                }
                else exc.code
            )
            raise InvalidJudgmentOperation(code, str(exc)) from exc
        event = self._append(
            event_type="judgment.outcome_recorded",
            card=updated,
            operation=operation,
            request_id=request,
            idempotency_key=idem,
            actor=actor_value,
            reason=reason_value,
            expected_version=current["stream_version"],
            previous_status=current["status"],
        )
        return self._return_projected(event)

    def cli_build(self) -> int:
        current = self._ensure_views()
        print(
            json.dumps(
                {
                    "event_seq": self.events.watermark() if self.events.exists() else 0,
                    "status": "ok",
                    "total": len(current),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    def cli_list(self, limit: int = 20) -> int:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 1000
        ):
            raise InvalidJudgmentOperation(
                "invalid_limit",
                "limit must be between 1 and 1000",
            )
        rows = []
        for card in self.list()[:limit]:
            if card["privacy"] == "private":
                rows.append(
                    {
                        "card_id": card["card_id"],
                        "outcome_status": card["outcome"]["status"],
                        "privacy": card["privacy"],
                        "redacted": True,
                        "revision": card["revision"],
                        "status": card["status"],
                    }
                )
            else:
                rows.append(
                    {
                        "card_id": card["card_id"],
                        "decision": card["decision"],
                        "outcome_status": card["outcome"]["status"],
                        "privacy": card["privacy"],
                        "revision": card["revision"],
                        "situation": card["situation"],
                        "status": card["status"],
                        "title": card["title"],
                    }
                )
        print(json.dumps({"items": rows, "limit": limit}, ensure_ascii=False, sort_keys=True))
        return 0

    def cli_stats(self) -> int:
        cards = self.list()
        payload = {
            "event_seq": self.events.watermark() if self.events.exists() else 0,
            "outcomes": {},
            "privacy": {},
            "statuses": {},
            "total": len(cards),
        }
        for card in cards:
            for field, value in (
                ("outcomes", card["outcome"]["status"]),
                ("privacy", card["privacy"]),
                ("statuses", card["status"]),
            ):
                payload[field][value] = payload[field].get(value, 0) + 1
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Immortal Judgment Store")
    parser.add_argument("action", choices=("build", "list", "stats"))
    parser.add_argument("extra", nargs="?", default=None)
    parser.add_argument("--vault-dir", default=str(Path.home() / ".immortal"))
    args = parser.parse_args(argv)
    try:
        store = JudgmentStore(Path(args.vault_dir))
        if args.action == "build":
            return store.cli_build()
        if args.action == "list":
            return store.cli_list(limit=int(args.extra or 20))
        return store.cli_stats()
    except Exception as exc:
        is_input_error = isinstance(exc, (InvalidJudgmentOperation, ValueError))
        print(
            json.dumps(
                {
                    "error": getattr(
                        exc,
                        "code",
                        "invalid_argument" if is_input_error else "cards_failed",
                    ),
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2 if is_input_error else 1


PIPELINE_CAPABILITIES = {"cards-build": _main}
CAPABILITY_READY = True


if __name__ == "__main__":
    raise SystemExit(_main())
