"""Typed dictionary contracts for the Immortal Memory v1.1 model layers."""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional


CLAIM_STATUSES = frozenset({"candidate", "confirmed", "rejected", "superseded"})
SOURCE_KINDS = frozenset({"direct", "quoted", "observed", "inferred", "user_declared"})
PRIVACY_LEVELS = frozenset({"private", "restricted", "context_safe", "public"})
CLAIM_TYPES = frozenset(
    {
        "fact",
        "preference",
        "value",
        "commitment",
        "decision",
        "lesson",
        "relationship",
        "style",
        "emotion",
        "request",
    }
)
SPEAKER_KINDS = frozenset({"owner", "other", "system", "unknown"})
SUBJECT_KINDS = frozenset({"owner", "other", "system", "unknown"})
ACTOR_KINDS = frozenset({"owner", "system", "migration"})
EVIDENCE_STATUSES = frozenset({"available", "source_broken", "source_deleted"})
EVIDENCE_SOURCES = frozenset({"codex", "claude", "feishu", "local", "web", "custom"})
REF_KINDS = frozenset({"claim", "self_model", "judgment"})
LIVING_SELF_STATUSES = frozenset({"candidate", "confirmed", "superseded"})
LIVING_SELF_REASONS = frozenset(
    {"migration", "claim_change", "manual_restore", "scheduled_rebuild"}
)
LIVING_SELF_SECTIONS = frozenset(
    {
        "identity_commitments",
        "values",
        "expression_dna",
        "mental_models",
        "decision_heuristics",
        "anti_patterns",
        "tensions",
        "honest_boundaries",
    }
)
JUDGMENT_STATUSES = frozenset({"candidate", "confirmed", "rejected", "retired"})
OUTCOME_STATUSES = frozenset({"unknown", "positive", "mixed", "negative"})
CONTEXT_MODES = frozenset(
    {"advisor", "writer", "reviewer", "business", "project", "custom"}
)
CONTEXT_LIFECYCLE_STATUSES = frozenset(
    {"preview", "compiled", "consumed", "outcome_recorded"}
)
CONTEXT_AVAILABILITY_STATUSES = frozenset({"active", "expired"})
CONTEXT_SECTIONS = frozenset(
    {
        "verified_facts",
        "confirmed_self_models",
        "judgment_cards",
        "counter_evidence",
        "inferences",
        "unknowns",
    }
)
ADOPTED_STATUSES = frozenset({"yes", "partial", "no", "unknown"})
RESULT_STATUSES = frozenset({"positive", "mixed", "negative", "unknown"})


class ModelValidationError(ValueError):
    """A model contract failure with a stable, machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _require_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelValidationError("invalid_model", "model must be a mapping")
    return value


def _require_fields(value: Mapping[str, Any], fields: Iterable[str]) -> None:
    for field in fields:
        if field not in value:
            raise ModelValidationError("missing_field", "missing required field: " + field)


def _require_text(value: Any, code: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError(code, field + " must not be empty")


def _require_enum(value: Any, allowed: frozenset, code: str, field: str) -> None:
    if value not in allowed:
        raise ModelValidationError(code, field + " has an unsupported value")


def _require_nonnegative_int(value: Any, code: str, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ModelValidationError(code, field + " must be a non-negative integer")


def _require_positive_int(value: Any, code: str, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ModelValidationError(code, field + " must be a positive integer")


def _require_list(value: Any, code: str, field: str) -> None:
    if not isinstance(value, list):
        raise ModelValidationError(code, field + " must be a list")


def _require_text_list(value: Any, code: str, field: str) -> None:
    _require_list(value, code, field)
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ModelValidationError(code, field + " must contain non-empty strings")


def _require_hash(value: Any, code: str = "invalid_content_hash") -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        raise ModelValidationError(code, "content hash must use sha256")
    try:
        int(value[7:], 16)
    except ValueError:
        raise ModelValidationError(code, "content hash must use hexadecimal sha256")


def _content_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _deduplicate(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(values))


def validate_claim(value: Mapping[str, Any]) -> None:
    claim = _require_mapping(value)
    _require_fields(
        claim,
        (
            "schema_version",
            "revision",
            "claim_id",
            "subject",
            "speaker",
            "claim_type",
            "statement",
            "evidence_ids",
            "counter_evidence_ids",
            "source_kind",
            "confidence",
            "confidence_basis",
            "role_scope",
            "domain_scope",
            "custom_scope_ids",
            "privacy",
            "valid_from",
            "valid_to",
            "status",
            "created_at",
            "updated_at",
            "based_on_event_seq",
        ),
    )
    if claim["schema_version"] != 1:
        raise ModelValidationError("invalid_schema_version", "claim schema must be 1")
    _require_positive_int(claim["revision"], "invalid_revision", "revision")
    _require_text(claim["claim_id"], "claim_id_required", "claim_id")
    _require_text(claim["statement"], "statement_required", "statement")
    _require_enum(claim["claim_type"], CLAIM_TYPES, "invalid_claim_type", "claim_type")
    _require_enum(claim["source_kind"], SOURCE_KINDS, "invalid_source_kind", "source_kind")
    _require_enum(claim["status"], CLAIM_STATUSES, "invalid_claim_status", "status")
    _require_enum(claim["privacy"], PRIVACY_LEVELS, "invalid_privacy", "privacy")
    _require_text_list(claim["evidence_ids"], "invalid_evidence_ids", "evidence_ids")
    _require_text_list(
        claim["counter_evidence_ids"],
        "invalid_counter_evidence_ids",
        "counter_evidence_ids",
    )
    if not claim["evidence_ids"] and claim["source_kind"] != "user_declared":
        raise ModelValidationError("evidence_required", "claim requires evidence")
    if claim["source_kind"] == "inferred" and claim["status"] == "confirmed":
        raise ModelValidationError(
            "inferred_claim_requires_review",
            "inferred claim cannot start confirmed",
        )
    confidence = claim["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.0 <= confidence <= 1.0
    ):
        raise ModelValidationError("invalid_confidence", "confidence must be from 0 to 1")
    for field, kinds, code in (
        ("subject", SUBJECT_KINDS, "invalid_subject_kind"),
        ("speaker", SPEAKER_KINDS, "invalid_speaker_kind"),
    ):
        identity = _require_mapping(claim[field])
        _require_fields(identity, ("kind", "id"))
        _require_enum(identity["kind"], kinds, code, field + ".kind")
        _require_text(identity["id"], field + "_id_required", field + ".id")
    basis = _require_mapping(claim["confidence_basis"])
    _require_fields(basis, ("speaker", "recurrence", "source_quality", "explanation"))
    for field in ("speaker", "recurrence", "source_quality"):
        score = basis[field]
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0.0 <= score <= 1.0
        ):
            raise ModelValidationError(
                "invalid_confidence_basis", field + " must be from 0 to 1"
            )
    if not isinstance(basis["explanation"], str):
        raise ModelValidationError(
            "invalid_confidence_basis", "explanation must be text"
        )
    for field in ("role_scope", "domain_scope", "custom_scope_ids"):
        _require_text_list(claim[field], "invalid_scope", field)
    if (
        "custom" in claim["role_scope"] or "custom" in claim["domain_scope"]
    ) and not claim["custom_scope_ids"]:
        raise ModelValidationError(
            "custom_scope_id_required", "custom scope requires a stable id"
        )
    _require_nonnegative_int(
        claim["based_on_event_seq"], "invalid_based_on_event_seq", "based_on_event_seq"
    )


def new_claim(
    *,
    statement: str,
    source_kind: str,
    evidence_ids: List[str],
    status: str = "candidate",
    claim_type: str = "fact",
    speaker_kind: str = "owner",
    speaker_id: Optional[str] = None,
    subject_kind: str = "owner",
    subject_id: Optional[str] = None,
    confidence: float = 0.0,
    role_scope: Optional[List[str]] = None,
    domain_scope: Optional[List[str]] = None,
    custom_scope_ids: Optional[List[str]] = None,
    privacy: str = "restricted",
    now: Optional[str] = None,
) -> Dict[str, Any]:
    generated = now or _now()
    value = {
        "schema_version": 1,
        "revision": 1,
        "claim_id": _identifier("clm_"),
        "subject": {
            "kind": subject_kind,
            "id": subject_id
            if subject_id is not None
            else ("owner" if subject_kind == "owner" else subject_kind),
        },
        "speaker": {
            "kind": speaker_kind,
            "id": speaker_id
            if speaker_id is not None
            else ("owner" if speaker_kind == "owner" else speaker_kind),
        },
        "claim_type": claim_type,
        "statement": statement.strip() if isinstance(statement, str) else statement,
        "evidence_ids": _deduplicate(evidence_ids),
        "counter_evidence_ids": [],
        "source_kind": source_kind,
        "confidence": confidence,
        "confidence_basis": {
            "speaker": 0.0,
            "recurrence": 0.0,
            "source_quality": 0.0,
            "explanation": "",
        },
        "role_scope": list(role_scope) if role_scope is not None else ["general"],
        "domain_scope": list(domain_scope) if domain_scope is not None else ["general"],
        "custom_scope_ids": _deduplicate(custom_scope_ids or []),
        "privacy": privacy,
        "valid_from": None,
        "valid_to": None,
        "status": status,
        "created_at": generated,
        "updated_at": generated,
        "based_on_event_seq": 0,
    }
    validate_claim(value)
    return value


def validate_event(value: Mapping[str, Any]) -> None:
    event = _require_mapping(value)
    _require_fields(
        event,
        (
            "event_id",
            "event_type",
            "stream_id",
            "stream_version",
            "schema_version",
            "request_id",
            "idempotency_key",
            "actor",
            "occurred_at",
            "expected_version",
            "payload",
            "previous_status",
            "migration_source",
        ),
    )
    if event["schema_version"] != 1:
        raise ModelValidationError("invalid_schema_version", "event schema must be 1")
    for field, code in (
        ("event_id", "event_id_required"),
        ("event_type", "event_type_required"),
        ("stream_id", "stream_id_required"),
        ("request_id", "request_id_required"),
        ("idempotency_key", "idempotency_key_required"),
        ("occurred_at", "occurred_at_required"),
    ):
        _require_text(event[field], code, field)
    _require_positive_int(event["stream_version"], "invalid_stream_version", "stream_version")
    _require_nonnegative_int(
        event["expected_version"], "invalid_expected_version", "expected_version"
    )
    if event["stream_version"] != event["expected_version"] + 1:
        raise ModelValidationError(
            "invalid_stream_version", "stream version must follow expected version"
        )
    actor = _require_mapping(event["actor"])
    _require_fields(actor, ("kind", "id"))
    _require_enum(actor["kind"], ACTOR_KINDS, "invalid_actor_kind", "actor.kind")
    _require_text(actor["id"], "actor_id_required", "actor.id")
    _require_mapping(event["payload"])


def new_event(
    *,
    event_id: Optional[str] = None,
    event_type: str,
    stream_id: str,
    stream_version: int,
    expected_version: int,
    request_id: str,
    idempotency_key: str,
    actor: Mapping[str, str],
    payload: Mapping[str, Any],
    previous_status: Optional[str] = None,
    migration_source: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    value = {
        "event_id": event_id or _identifier("evt_"),
        "event_type": event_type,
        "stream_id": stream_id,
        "stream_version": stream_version,
        "schema_version": 1,
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "actor": dict(actor),
        "occurred_at": now or _now(),
        "expected_version": expected_version,
        "payload": dict(payload),
        "previous_status": previous_status,
        "migration_source": migration_source,
    }
    validate_event(value)
    return value


def validate_evidence_ref(value: Mapping[str, Any]) -> None:
    ref = _require_mapping(value)
    if "rowid" in ref:
        raise ModelValidationError("rowid_forbidden", "SQLite rowid is not stable evidence")
    _require_fields(
        ref,
        (
            "evidence_id",
            "source",
            "raw_id",
            "content_hash",
            "status",
            "observed_at",
            "privacy",
        ),
    )
    _require_text(ref["evidence_id"], "evidence_id_required", "evidence_id")
    _require_enum(ref["source"], EVIDENCE_SOURCES, "invalid_evidence_source", "source")
    if ref["raw_id"] is not None and not isinstance(ref["raw_id"], str):
        raise ModelValidationError("invalid_raw_id", "raw_id must be text or null")
    _require_hash(ref["content_hash"])
    _require_enum(
        ref["status"], EVIDENCE_STATUSES, "invalid_evidence_status", "status"
    )
    _require_text(ref["observed_at"], "observed_at_required", "observed_at")
    _require_enum(ref["privacy"], PRIVACY_LEVELS, "invalid_privacy", "privacy")


def new_evidence_ref(
    *,
    evidence_id: str,
    source: str,
    raw_id: Optional[str],
    content_hash: str,
    status: str,
    privacy: str,
    observed_at: Optional[str] = None,
) -> Dict[str, Any]:
    value = {
        "evidence_id": evidence_id,
        "source": source,
        "raw_id": raw_id,
        "content_hash": content_hash,
        "status": status,
        "observed_at": observed_at or _now(),
        "privacy": privacy,
    }
    validate_evidence_ref(value)
    return value


def validate_typed_ref(value: Mapping[str, Any]) -> None:
    ref = _require_mapping(value)
    _require_fields(ref, ("kind", "id", "revision"))
    _require_enum(ref["kind"], REF_KINDS, "invalid_ref_kind", "kind")
    _require_text(ref["id"], "ref_id_required", "id")
    _require_positive_int(ref["revision"], "invalid_revision", "revision")


def new_typed_ref(*, kind: str, id: str, revision: int) -> Dict[str, Any]:
    value = {"kind": kind, "id": id, "revision": revision}
    validate_typed_ref(value)
    return value


def validate_living_self_version(value: Mapping[str, Any]) -> None:
    version = _require_mapping(value)
    _require_fields(
        version,
        (
            "version_id",
            "parent_version_id",
            "status",
            "generation_reason",
            "content_hash",
            "based_on_claim_seq",
            "generated_at",
            "confirmed_at",
            "sections",
        ),
    )
    _require_text(version["version_id"], "version_id_required", "version_id")
    if version["parent_version_id"] is not None:
        _require_text(
            version["parent_version_id"],
            "parent_version_id_required",
            "parent_version_id",
        )
    _require_enum(
        version["status"],
        LIVING_SELF_STATUSES,
        "invalid_living_self_status",
        "status",
    )
    _require_enum(
        version["generation_reason"],
        LIVING_SELF_REASONS,
        "invalid_generation_reason",
        "generation_reason",
    )
    _require_hash(version["content_hash"])
    _require_nonnegative_int(
        version["based_on_claim_seq"],
        "invalid_based_on_claim_seq",
        "based_on_claim_seq",
    )
    _require_text(version["generated_at"], "generated_at_required", "generated_at")
    sections = _require_mapping(version["sections"])
    if set(sections) != LIVING_SELF_SECTIONS:
        raise ModelValidationError(
            "invalid_living_self_sections", "Living Self requires exactly eight sections"
        )
    for section, items in sections.items():
        _require_list(items, "invalid_living_self_section", section)


def new_living_self_version(
    *,
    sections: Mapping[str, List[Mapping[str, Any]]],
    generation_reason: str,
    based_on_claim_seq: int,
    content_hash: Optional[str] = None,
    parent_version_id: Optional[str] = None,
    status: str = "candidate",
    now: Optional[str] = None,
) -> Dict[str, Any]:
    generated = now or _now()
    copied_sections = {name: [dict(item) for item in items] for name, items in sections.items()}
    value = {
        "version_id": _identifier("lsv_"),
        "parent_version_id": parent_version_id,
        "status": status,
        "generation_reason": generation_reason,
        "content_hash": content_hash or _content_hash(copied_sections),
        "based_on_claim_seq": based_on_claim_seq,
        "generated_at": generated,
        "confirmed_at": generated if status == "confirmed" else None,
        "sections": copied_sections,
    }
    validate_living_self_version(value)
    return value


def validate_judgment_card(value: Mapping[str, Any]) -> None:
    card = _require_mapping(value)
    _require_fields(
        card,
        (
            "card_id",
            "title",
            "situation",
            "goal",
            "constraints",
            "signals",
            "decision",
            "alternatives",
            "outcome",
            "lesson",
            "next_trigger",
            "evidence_ids",
            "claim_ids",
            "privacy",
            "status",
            "created_at",
            "updated_at",
        ),
    )
    for field, code in (
        ("card_id", "card_id_required"),
        ("title", "title_required"),
        ("situation", "situation_required"),
        ("decision", "decision_required"),
        ("created_at", "created_at_required"),
        ("updated_at", "updated_at_required"),
    ):
        _require_text(card[field], code, field)
    for field in ("goal", "lesson", "next_trigger"):
        if not isinstance(card[field], str):
            raise ModelValidationError("invalid_judgment_text", field + " must be text")
    for field in (
        "constraints",
        "signals",
        "alternatives",
        "evidence_ids",
        "claim_ids",
    ):
        _require_text_list(card[field], "invalid_" + field, field)
    if not card["evidence_ids"]:
        raise ModelValidationError("evidence_required", "judgment requires evidence")
    outcome = _require_mapping(card["outcome"])
    _require_fields(outcome, ("status", "summary", "observed_at"))
    _require_enum(
        outcome["status"], OUTCOME_STATUSES, "invalid_outcome_status", "outcome.status"
    )
    if not isinstance(outcome["summary"], str):
        raise ModelValidationError("invalid_outcome_summary", "outcome summary must be text")
    _require_enum(card["privacy"], PRIVACY_LEVELS, "invalid_privacy", "privacy")
    _require_enum(
        card["status"], JUDGMENT_STATUSES, "invalid_judgment_status", "status"
    )


def new_judgment_card(
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
    status: str = "candidate",
    now: Optional[str] = None,
) -> Dict[str, Any]:
    generated = now or _now()
    value = {
        "card_id": _identifier("jdg_"),
        "title": title.strip() if isinstance(title, str) else title,
        "situation": situation.strip() if isinstance(situation, str) else situation,
        "goal": goal,
        "constraints": list(constraints or []),
        "signals": list(signals or []),
        "decision": decision.strip() if isinstance(decision, str) else decision,
        "alternatives": list(alternatives or []),
        "outcome": {"status": "unknown", "summary": "", "observed_at": None},
        "lesson": lesson,
        "next_trigger": next_trigger,
        "evidence_ids": _deduplicate(evidence_ids),
        "claim_ids": _deduplicate(claim_ids or []),
        "privacy": privacy,
        "status": status,
        "created_at": generated,
        "updated_at": generated,
    }
    validate_judgment_card(value)
    return value


def validate_context_pack(value: Mapping[str, Any]) -> None:
    pack = _require_mapping(value)
    _require_fields(
        pack,
        (
            "context_id",
            "task",
            "mode",
            "lifecycle_status",
            "availability_status",
            "budget",
            "sections",
            "provenance",
            "privacy_policy",
            "source_revision",
            "preview_hash",
            "content_hash",
            "generated_at",
            "expires_at",
        ),
    )
    _require_text(pack["context_id"], "context_id_required", "context_id")
    _require_text(pack["task"], "task_required", "task")
    _require_enum(pack["mode"], CONTEXT_MODES, "invalid_context_mode", "mode")
    _require_enum(
        pack["lifecycle_status"],
        CONTEXT_LIFECYCLE_STATUSES,
        "invalid_lifecycle_status",
        "lifecycle_status",
    )
    _require_enum(
        pack["availability_status"],
        CONTEXT_AVAILABILITY_STATUSES,
        "invalid_availability_status",
        "availability_status",
    )
    budget = _require_mapping(pack["budget"])
    _require_fields(budget, ("max_chars", "used_chars"))
    _require_positive_int(budget["max_chars"], "invalid_context_budget", "max_chars")
    _require_nonnegative_int(
        budget["used_chars"], "invalid_context_budget", "used_chars"
    )
    if budget["used_chars"] > budget["max_chars"]:
        raise ModelValidationError(
            "context_budget_exceeded", "used chars exceeds context budget"
        )
    sections = _require_mapping(pack["sections"])
    if set(sections) != CONTEXT_SECTIONS:
        raise ModelValidationError(
            "invalid_context_sections", "context requires exactly six sections"
        )
    for section, items in sections.items():
        _require_list(items, "invalid_context_section", section)
    provenance = _require_mapping(pack["provenance"])
    provenance_fields = (
        "evidence_ids",
        "claim_ids",
        "self_model_item_ids",
        "judgment_card_ids",
    )
    _require_fields(provenance, provenance_fields)
    for field in provenance_fields:
        _require_text_list(provenance[field], "invalid_provenance", field)
    policy = _require_mapping(pack["privacy_policy"])
    _require_fields(policy, ("excluded_count", "reasons"))
    _require_nonnegative_int(
        policy["excluded_count"], "invalid_privacy_policy", "excluded_count"
    )
    _require_text_list(policy["reasons"], "invalid_privacy_policy", "reasons")
    revision = _require_mapping(pack["source_revision"])
    _require_fields(
        revision,
        (
            "claims_event_seq",
            "living_self_version",
            "judgments_event_seq",
            "compiler_version",
            "policy_version",
        ),
    )
    _require_nonnegative_int(
        revision["claims_event_seq"], "invalid_source_revision", "claims_event_seq"
    )
    _require_nonnegative_int(
        revision["judgments_event_seq"],
        "invalid_source_revision",
        "judgments_event_seq",
    )
    _require_text(
        revision["living_self_version"],
        "invalid_source_revision",
        "living_self_version",
    )
    _require_text(
        revision["compiler_version"], "invalid_source_revision", "compiler_version"
    )
    _require_positive_int(
        revision["policy_version"], "invalid_source_revision", "policy_version"
    )
    if not isinstance(pack["preview_hash"], str):
        raise ModelValidationError("invalid_preview_hash", "preview hash must be text")
    _require_hash(pack["content_hash"])
    _require_text(pack["generated_at"], "generated_at_required", "generated_at")
    _require_text(pack["expires_at"], "expires_at_required", "expires_at")


def new_context_pack(
    *,
    task: str,
    mode: str,
    living_self_version: str,
    lifecycle_status: str = "preview",
    availability_status: str = "active",
    max_chars: int = 24000,
    claims_event_seq: int = 0,
    judgments_event_seq: int = 0,
    compiler_version: str = "1.1.0",
    policy_version: int = 1,
    preview_hash: str = "",
    now: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> Dict[str, Any]:
    generated = now or _now()
    sections = {name: [] for name in sorted(CONTEXT_SECTIONS)}
    value = {
        "context_id": _identifier("ctx_"),
        "task": task.strip() if isinstance(task, str) else task,
        "mode": mode,
        "lifecycle_status": lifecycle_status,
        "availability_status": availability_status,
        "budget": {"max_chars": max_chars, "used_chars": 0},
        "sections": sections,
        "provenance": {
            "evidence_ids": [],
            "claim_ids": [],
            "self_model_item_ids": [],
            "judgment_card_ids": [],
        },
        "privacy_policy": {"excluded_count": 0, "reasons": []},
        "source_revision": {
            "claims_event_seq": claims_event_seq,
            "living_self_version": living_self_version,
            "judgments_event_seq": judgments_event_seq,
            "compiler_version": compiler_version,
            "policy_version": policy_version,
        },
        "preview_hash": preview_hash,
        "content_hash": "",
        "generated_at": generated,
        "expires_at": expires_at or generated,
    }
    value["content_hash"] = _content_hash(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    validate_context_pack(value)
    return value


def validate_outcome_event(value: Mapping[str, Any]) -> None:
    outcome = _require_mapping(value)
    _require_fields(
        outcome,
        (
            "outcome_id",
            "context_id",
            "adopted",
            "result",
            "summary",
            "confirmed_refs",
            "challenged_refs",
            "created_at",
        ),
    )
    _require_text(outcome["outcome_id"], "outcome_id_required", "outcome_id")
    _require_text(outcome["context_id"], "context_id_required", "context_id")
    _require_enum(
        outcome["adopted"], ADOPTED_STATUSES, "invalid_adopted_status", "adopted"
    )
    _require_enum(
        outcome["result"], RESULT_STATUSES, "invalid_result_status", "result"
    )
    if not isinstance(outcome["summary"], str):
        raise ModelValidationError("invalid_outcome_summary", "summary must be text")
    for field in ("confirmed_refs", "challenged_refs"):
        _require_list(outcome[field], "invalid_typed_refs", field)
        for ref in outcome[field]:
            validate_typed_ref(ref)
    _require_text(outcome["created_at"], "created_at_required", "created_at")


def new_outcome_event(
    *,
    context_id: str,
    adopted: str = "unknown",
    result: str = "unknown",
    summary: str = "",
    confirmed_refs: Optional[List[Mapping[str, Any]]] = None,
    challenged_refs: Optional[List[Mapping[str, Any]]] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    value = {
        "outcome_id": _identifier("out_"),
        "context_id": context_id,
        "adopted": adopted,
        "result": result,
        "summary": summary,
        "confirmed_refs": [dict(ref) for ref in (confirmed_refs or [])],
        "challenged_refs": [dict(ref) for ref in (challenged_refs or [])],
        "created_at": now or _now(),
    }
    validate_outcome_event(value)
    return value
