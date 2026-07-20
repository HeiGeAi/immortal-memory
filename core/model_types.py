"""Typed dictionary contracts for the Immortal Memory v1.1 model layers."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
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
ROLE_SCOPES = frozenset(
    {"general", "personal", "work", "creator", "family", "custom"}
)
DOMAIN_SCOPES = frozenset(
    {
        "general",
        "business",
        "content",
        "technical",
        "relationship",
        "project",
        "risk",
        "custom",
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
SELF_MODEL_KINDS = frozenset(
    {
        "identity_commitment",
        "value",
        "expression_dna",
        "mental_model",
        "decision_heuristic",
        "anti_pattern",
        "tension",
        "honest_boundary",
    }
)
SELF_MODEL_SECTION_KINDS = {
    "identity_commitments": "identity_commitment",
    "values": "value",
    "expression_dna": "expression_dna",
    "mental_models": "mental_model",
    "decision_heuristics": "decision_heuristic",
    "anti_patterns": "anti_pattern",
    "tensions": "tension",
    "honest_boundaries": "honest_boundary",
}
GENERATIVE_POWER_STATUSES = frozenset({"tested", "untested", "failed"})
DISTINCTIVENESS_LEVELS = frozenset({"high", "medium", "low"})
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
CONTEXT_SECTION_ITEM_LIMIT = 20
CONTEXT_SUMMARY_CHAR_LIMIT = 500
CONTEXT_RAW_BODY_KEYS = frozenset(
    {"raw", "raw_body", "raw_content", "private_body", "full_text"}
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


def _require_optional_text(value: Any, code: str, field: str) -> None:
    if value is not None:
        _require_text(value, code, field)


def _require_enum(value: Any, allowed: frozenset, code: str, field: str) -> None:
    try:
        supported = value in allowed
    except TypeError:
        supported = False
    if not supported:
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


def _parse_timestamp(
    value: Any,
    *,
    field: str,
    nullable: bool = False,
) -> Optional[datetime]:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError("invalid_timestamp", field + " must be ISO 8601")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ModelValidationError(
            "invalid_timestamp", field + " must be ISO 8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelValidationError(
            "invalid_timestamp", field + " must include a timezone"
        )
    return parsed


def _require_time_order(
    earlier: Optional[datetime],
    later: Optional[datetime],
    *,
    allow_equal: bool = True,
) -> None:
    if earlier is None or later is None:
        return
    if later < earlier or (not allow_equal and later == earlier):
        raise ModelValidationError(
            "invalid_time_order", "timestamp order is invalid"
        )


def _content_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _deduplicate(values: Any) -> List[str]:
    _require_text_list(values, "invalid_text_list", "values")
    return list(dict.fromkeys(values))


def _copy_mapping(value: Any, code: str, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelValidationError(code, field + " must be a mapping")
    return dict(value)


def _copy_mapping_list(value: Any, code: str, field: str) -> List[Dict[str, Any]]:
    _require_list(value, code, field)
    return [
        _copy_mapping(item, code, field + " item")
        for item in value
    ]


def _optional_text_list(value: Any, code: str, field: str) -> List[str]:
    if value is None:
        return []
    _require_text_list(value, code, field)
    return list(value)


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
    if confidence > 0.0 and not basis["explanation"].strip():
        raise ModelValidationError(
            "confidence_explanation_required",
            "positive confidence requires an explanation",
        )
    if confidence > max(
        float(basis["speaker"]),
        float(basis["recurrence"]),
        float(basis["source_quality"]),
    ):
        raise ModelValidationError(
            "confidence_basis_inconsistent",
            "confidence exceeds every supporting basis score",
        )
    _require_text_list(claim["role_scope"], "invalid_scope", "role_scope")
    _require_text_list(claim["domain_scope"], "invalid_scope", "domain_scope")
    _require_text_list(
        claim["custom_scope_ids"], "invalid_scope", "custom_scope_ids"
    )
    if any(scope not in ROLE_SCOPES for scope in claim["role_scope"]):
        raise ModelValidationError(
            "invalid_role_scope", "role_scope contains an unsupported value"
        )
    if any(scope not in DOMAIN_SCOPES for scope in claim["domain_scope"]):
        raise ModelValidationError(
            "invalid_domain_scope", "domain_scope contains an unsupported value"
        )
    if (
        "custom" in claim["role_scope"] or "custom" in claim["domain_scope"]
    ) and not claim["custom_scope_ids"]:
        raise ModelValidationError(
            "custom_scope_id_required", "custom scope requires a stable id"
        )
    _require_nonnegative_int(
        claim["based_on_event_seq"], "invalid_based_on_event_seq", "based_on_event_seq"
    )
    created_at = _parse_timestamp(claim["created_at"], field="created_at")
    updated_at = _parse_timestamp(claim["updated_at"], field="updated_at")
    valid_from = _parse_timestamp(
        claim["valid_from"], field="valid_from", nullable=True
    )
    valid_to = _parse_timestamp(claim["valid_to"], field="valid_to", nullable=True)
    _require_time_order(created_at, updated_at)
    _require_time_order(valid_from, valid_to)


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
    confidence_basis: Optional[Mapping[str, Any]] = None,
    role_scope: Optional[List[str]] = None,
    domain_scope: Optional[List[str]] = None,
    custom_scope_ids: Optional[List[str]] = None,
    privacy: str = "restricted",
    now: Optional[str] = None,
) -> Dict[str, Any]:
    generated = now or _now()
    resolved_basis = (
        _copy_mapping(
            confidence_basis,
            "invalid_confidence_basis",
            "confidence_basis",
        )
        if confidence_basis is not None
        else {
            "speaker": 0.0,
            "recurrence": 0.0,
            "source_quality": confidence if isinstance(confidence, (int, float)) else 0.0,
            "explanation": (
                "confidence bounded by source quality and supplied evidence"
                if isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and confidence > 0
                else ""
            ),
        }
    )
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
        "confidence_basis": resolved_basis,
        "role_scope": (
            _optional_text_list(role_scope, "invalid_role_scope", "role_scope")
            if role_scope is not None
            else ["general"]
        ),
        "domain_scope": (
            _optional_text_list(
                domain_scope, "invalid_domain_scope", "domain_scope"
            )
            if domain_scope is not None
            else ["general"]
        ),
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
    _require_optional_text(
        event["previous_status"],
        "invalid_previous_status",
        "previous_status",
    )
    _require_optional_text(
        event["migration_source"],
        "invalid_migration_source",
        "migration_source",
    )
    _parse_timestamp(event["occurred_at"], field="occurred_at")


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
        "actor": _copy_mapping(actor, "invalid_actor", "actor"),
        "occurred_at": now or _now(),
        "expected_version": expected_version,
        "payload": _copy_mapping(payload, "invalid_payload", "payload"),
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
            "source_detail",
            "raw_id",
            "content_hash",
            "status",
            "observed_at",
            "privacy",
        ),
    )
    _require_text(ref["evidence_id"], "evidence_id_required", "evidence_id")
    _require_enum(ref["source"], EVIDENCE_SOURCES, "invalid_evidence_source", "source")
    _require_text(
        ref["source_detail"], "source_detail_required", "source_detail"
    )
    if ref["raw_id"] is not None and not isinstance(ref["raw_id"], str):
        raise ModelValidationError("invalid_raw_id", "raw_id must be text or null")
    _require_hash(ref["content_hash"])
    _require_enum(
        ref["status"], EVIDENCE_STATUSES, "invalid_evidence_status", "status"
    )
    _require_text(ref["observed_at"], "observed_at_required", "observed_at")
    _parse_timestamp(ref["observed_at"], field="observed_at")
    _require_enum(ref["privacy"], PRIVACY_LEVELS, "invalid_privacy", "privacy")


def _canonical_evidence_source(source: Any) -> str:
    _require_text(source, "invalid_evidence_source", "source")
    normalized = source.strip().casefold()
    if normalized in EVIDENCE_SOURCES:
        return normalized
    if normalized.startswith("codex-"):
        return "codex"
    if normalized.startswith("claude-"):
        return "claude"
    if normalized.startswith("feishu-"):
        return "feishu"
    if normalized.startswith("web-"):
        return "web"
    if normalized in {"obsidian-note", "desktop-output", "immortal-smoke"}:
        return "local"
    return "custom"


def new_evidence_ref(
    *,
    evidence_id: str,
    source: str,
    raw_id: Optional[str],
    content_hash: str,
    status: str,
    privacy: str,
    observed_at: Optional[str] = None,
    source_detail: Optional[str] = None,
) -> Dict[str, Any]:
    canonical_source = _canonical_evidence_source(source)
    value = {
        "evidence_id": evidence_id,
        "source": canonical_source,
        "source_detail": source_detail or source,
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


def validate_self_model_item(value: Mapping[str, Any]) -> None:
    item = _require_mapping(value)
    _require_fields(
        item,
        (
            "schema_version",
            "revision",
            "item_id",
            "kind",
            "title",
            "summary",
            "evidence_ids",
            "claim_ids",
            "counter_evidence_ids",
            "confidence",
            "validation",
            "application",
            "failure_conditions",
            "role_scope",
            "domain_scope",
            "valid_from",
            "valid_to",
            "status",
            "last_reviewed_at",
            "based_on_claim_seq",
        ),
    )
    if item["schema_version"] != 1:
        raise ModelValidationError(
            "invalid_schema_version", "self model item schema must be 1"
        )
    _require_positive_int(item["revision"], "invalid_revision", "revision")
    _require_text(item["item_id"], "item_id_required", "item_id")
    _require_enum(
        item["kind"], SELF_MODEL_KINDS, "invalid_self_model_kind", "kind"
    )
    _require_text(item["title"], "title_required", "title")
    _require_text(item["summary"], "summary_required", "summary")
    for field in (
        "evidence_ids",
        "claim_ids",
        "counter_evidence_ids",
        "application",
        "failure_conditions",
        "role_scope",
        "domain_scope",
    ):
        _require_text_list(item[field], "invalid_self_model_item", field)
    if any(scope not in ROLE_SCOPES for scope in item["role_scope"]):
        raise ModelValidationError(
            "invalid_role_scope", "role_scope contains an unsupported value"
        )
    if any(scope not in DOMAIN_SCOPES for scope in item["domain_scope"]):
        raise ModelValidationError(
            "invalid_domain_scope", "domain_scope contains an unsupported value"
        )
    if not item["evidence_ids"] and not item["claim_ids"]:
        raise ModelValidationError(
            "self_model_source_required",
            "self model item requires evidence or a claim",
        )
    confidence = item["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.0 <= confidence <= 1.0
    ):
        raise ModelValidationError(
            "invalid_confidence", "confidence must be from 0 to 1"
        )
    validation = _require_mapping(item["validation"])
    _require_fields(
        validation,
        (
            "cross_domain_recurrence",
            "generative_power",
            "distinctiveness",
        ),
    )
    _require_nonnegative_int(
        validation["cross_domain_recurrence"],
        "invalid_self_model_validation",
        "cross_domain_recurrence",
    )
    _require_enum(
        validation["generative_power"],
        GENERATIVE_POWER_STATUSES,
        "invalid_self_model_validation",
        "generative_power",
    )
    _require_enum(
        validation["distinctiveness"],
        DISTINCTIVENESS_LEVELS,
        "invalid_self_model_validation",
        "distinctiveness",
    )
    _require_enum(
        item["status"], CLAIM_STATUSES, "invalid_self_model_status", "status"
    )
    _require_nonnegative_int(
        item["based_on_claim_seq"],
        "invalid_based_on_claim_seq",
        "based_on_claim_seq",
    )
    valid_from = _parse_timestamp(
        item["valid_from"], field="valid_from", nullable=True
    )
    valid_to = _parse_timestamp(item["valid_to"], field="valid_to", nullable=True)
    _parse_timestamp(item["last_reviewed_at"], field="last_reviewed_at")
    _require_time_order(valid_from, valid_to)


def new_self_model_item(
    *,
    kind: str,
    title: str,
    summary: str,
    evidence_ids: List[str],
    claim_ids: List[str],
    confidence: float = 0.0,
    validation: Optional[Mapping[str, Any]] = None,
    counter_evidence_ids: Optional[List[str]] = None,
    application: Optional[List[str]] = None,
    failure_conditions: Optional[List[str]] = None,
    role_scope: Optional[List[str]] = None,
    domain_scope: Optional[List[str]] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    status: str = "candidate",
    last_reviewed_at: Optional[str] = None,
    based_on_claim_seq: int = 0,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    generated = now or _now()
    value = {
        "schema_version": 1,
        "revision": 1,
        "item_id": _identifier("self_"),
        "kind": kind,
        "title": title.strip() if isinstance(title, str) else title,
        "summary": summary.strip() if isinstance(summary, str) else summary,
        "evidence_ids": _deduplicate(evidence_ids),
        "claim_ids": _deduplicate(claim_ids),
        "counter_evidence_ids": _deduplicate(counter_evidence_ids or []),
        "confidence": confidence,
        "validation": _copy_mapping(
            validation
            if validation is not None
            else {
                "cross_domain_recurrence": 0,
                "generative_power": "untested",
                "distinctiveness": "low",
            },
            "invalid_self_model_validation",
            "validation",
        ),
        "application": _optional_text_list(
            application, "invalid_self_model_item", "application"
        ),
        "failure_conditions": _optional_text_list(
            failure_conditions,
            "invalid_self_model_item",
            "failure_conditions",
        ),
        "role_scope": (
            _optional_text_list(role_scope, "invalid_role_scope", "role_scope")
            if role_scope is not None
            else ["general"]
        ),
        "domain_scope": (
            _optional_text_list(
                domain_scope, "invalid_domain_scope", "domain_scope"
            )
            if domain_scope is not None
            else ["general"]
        ),
        "valid_from": valid_from or generated,
        "valid_to": valid_to,
        "status": status,
        "last_reviewed_at": last_reviewed_at or generated,
        "based_on_claim_seq": based_on_claim_seq,
    }
    validate_self_model_item(value)
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
    generated_at = _parse_timestamp(
        version["generated_at"], field="generated_at"
    )
    if version["status"] == "confirmed":
        _require_text(
            version["confirmed_at"],
            "confirmed_at_required",
            "confirmed_at",
        )
        confirmed_at = _parse_timestamp(
            version["confirmed_at"], field="confirmed_at"
        )
        _require_time_order(generated_at, confirmed_at)
    elif version["confirmed_at"] is not None:
        raise ModelValidationError(
            "unexpected_confirmed_at",
            "only confirmed versions can have confirmed_at",
        )
    sections = _require_mapping(version["sections"])
    if set(sections) != LIVING_SELF_SECTIONS:
        raise ModelValidationError(
            "invalid_living_self_sections", "Living Self requires exactly eight sections"
        )
    for section, items in sections.items():
        _require_list(items, "invalid_living_self_section", section)
        expected_kind = SELF_MODEL_SECTION_KINDS[section]
        for item in items:
            validate_self_model_item(item)
            if item["kind"] != expected_kind:
                raise ModelValidationError(
                    "self_model_section_mismatch",
                    "self model item kind does not match section",
                )
    if version["content_hash"] != _content_hash(sections):
        raise ModelValidationError(
            "content_hash_mismatch",
            "Living Self content hash does not match sections",
        )


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
    copied_sections = _copy_context_sections(sections)
    calculated_hash = _content_hash(copied_sections)
    value = {
        "version_id": _identifier("lsv_"),
        "parent_version_id": parent_version_id,
        "status": status,
        "generation_reason": generation_reason,
        "content_hash": content_hash or calculated_hash,
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
    if outcome["status"] == "unknown":
        if outcome["observed_at"] is not None:
            raise ModelValidationError(
                "unexpected_outcome_observed_at",
                "unknown outcome cannot have observed_at",
            )
    else:
        if outcome["observed_at"] is None:
            raise ModelValidationError(
                "outcome_observed_at_required",
                "known outcome requires observed_at",
            )
        _parse_timestamp(outcome["observed_at"], field="outcome.observed_at")
    _require_enum(card["privacy"], PRIVACY_LEVELS, "invalid_privacy", "privacy")
    _require_enum(
        card["status"], JUDGMENT_STATUSES, "invalid_judgment_status", "status"
    )
    created_at = _parse_timestamp(card["created_at"], field="created_at")
    updated_at = _parse_timestamp(card["updated_at"], field="updated_at")
    _require_time_order(created_at, updated_at)


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
        "constraints": _optional_text_list(
            constraints, "invalid_constraints", "constraints"
        ),
        "signals": _optional_text_list(signals, "invalid_signals", "signals"),
        "decision": decision.strip() if isinstance(decision, str) else decision,
        "alternatives": _optional_text_list(
            alternatives, "invalid_alternatives", "alternatives"
        ),
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


def _context_text_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return sum(_context_text_chars(item) for item in value.values())
    if isinstance(value, list):
        return sum(_context_text_chars(item) for item in value)
    return 0


def _validate_context_item(value: Any) -> None:
    item = _require_mapping(value)
    if item.get("privacy") == "private":
        raise ModelValidationError(
            "private_context_item_forbidden",
            "private items cannot enter a Context Pack",
        )
    if any(key in item for key in CONTEXT_RAW_BODY_KEYS):
        raise ModelValidationError(
            "private_context_item_forbidden",
            "raw bodies cannot enter a Context Pack",
        )
    summary = item.get("summary")
    if summary is not None:
        if not isinstance(summary, str):
            raise ModelValidationError(
                "invalid_context_summary", "context summary must be text"
            )
        if len(summary) > CONTEXT_SUMMARY_CHAR_LIMIT:
            raise ModelValidationError(
                "context_summary_limit",
                "context summary exceeds the character limit",
            )


def _copy_context_sections(
    sections: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    copied = _copy_mapping(sections, "invalid_context_sections", "sections")
    result: Dict[str, List[Dict[str, Any]]] = {}
    for name, items in copied.items():
        result[name] = _copy_mapping_list(
            items,
            "invalid_context_section",
            str(name),
        )
    return result


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
        if len(items) > CONTEXT_SECTION_ITEM_LIMIT:
            raise ModelValidationError(
                "context_section_item_limit",
                "context section exceeds its item limit",
            )
        for item in items:
            _validate_context_item(item)
    actual_used_chars = _context_text_chars(sections)
    if budget["used_chars"] != actual_used_chars:
        raise ModelValidationError(
            "context_used_chars_mismatch",
            "used_chars does not match the structured Context Pack content",
        )
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
    _require_hash(pack["preview_hash"], code="invalid_preview_hash")
    _require_hash(pack["content_hash"])
    generated_at = _parse_timestamp(pack["generated_at"], field="generated_at")
    expires_at = _parse_timestamp(pack["expires_at"], field="expires_at")
    _require_time_order(generated_at, expires_at, allow_equal=False)
    current = datetime.now(timezone.utc)
    if (
        pack["availability_status"] == "active"
        and expires_at is not None
        and expires_at <= current
    ):
        raise ModelValidationError("context_expired", "active Context Pack has expired")
    if (
        pack["availability_status"] == "expired"
        and expires_at is not None
        and expires_at > current
    ):
        raise ModelValidationError(
            "context_not_expired", "expired Context Pack still has a live TTL"
        )
    expected_content_hash = _content_hash(
        {key: item for key, item in pack.items() if key != "content_hash"}
    )
    if pack["content_hash"] != expected_content_hash:
        raise ModelValidationError(
            "content_hash_mismatch",
            "Context Pack content hash does not match its content",
        )


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
    sections: Optional[Mapping[str, List[Mapping[str, Any]]]] = None,
    now: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> Dict[str, Any]:
    generated = now or _now()
    if expires_at is None:
        generated_time = _parse_timestamp(generated, field="generated_at")
        if generated_time is None:
            raise ModelValidationError(
                "invalid_timestamp", "generated_at must be ISO 8601"
            )
        expires_at = (generated_time + timedelta(hours=1)).isoformat()
    copied_sections = (
        _copy_context_sections(sections)
        if sections is not None
        else {name: [] for name in sorted(CONTEXT_SECTIONS)}
    )
    value = {
        "context_id": _identifier("ctx_"),
        "task": task.strip() if isinstance(task, str) else task,
        "mode": mode,
        "lifecycle_status": lifecycle_status,
        "availability_status": availability_status,
        "budget": {
            "max_chars": max_chars,
            "used_chars": _context_text_chars(copied_sections),
        },
        "sections": copied_sections,
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
        "expires_at": expires_at,
    }
    if not value["preview_hash"]:
        value["preview_hash"] = _content_hash(
            {
                key: item
                for key, item in value.items()
                if key not in {"preview_hash", "content_hash"}
            }
        )
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
    confirmed = {
        (ref["kind"], ref["id"], ref["revision"])
        for ref in outcome["confirmed_refs"]
    }
    challenged = {
        (ref["kind"], ref["id"], ref["revision"])
        for ref in outcome["challenged_refs"]
    }
    if confirmed & challenged:
        raise ModelValidationError(
            "outcome_ref_conflict",
            "the same ref cannot be both confirmed and challenged",
        )
    _require_text(outcome["created_at"], "created_at_required", "created_at")
    _parse_timestamp(outcome["created_at"], field="created_at")


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
        "confirmed_refs": _copy_mapping_list(
            confirmed_refs or [],
            "invalid_typed_refs",
            "confirmed_refs",
        ),
        "challenged_refs": _copy_mapping_list(
            challenged_refs or [],
            "invalid_typed_refs",
            "challenged_refs",
        ),
        "created_at": now or _now(),
    }
    validate_outcome_event(value)
    return value
