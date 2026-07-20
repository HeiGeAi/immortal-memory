import copy

import pytest

from model_types import (
    ModelValidationError,
    new_claim,
    new_context_pack,
    new_event,
    new_evidence_ref,
    new_judgment_card,
    new_living_self_version,
    new_outcome_event,
    new_typed_ref,
    validate_claim,
    validate_context_pack,
    validate_event,
    validate_evidence_ref,
    validate_judgment_card,
    validate_living_self_version,
    validate_outcome_event,
    validate_typed_ref,
)


def assert_error(code, operation):
    with pytest.raises(ModelValidationError) as exc_info:
        operation()
    assert exc_info.value.code == code


def evidence_ref():
    return new_evidence_ref(
        evidence_id="ev_raw_1",
        source="codex",
        raw_id="raw-1",
        content_hash="sha256:" + "a" * 64,
        status="available",
        privacy="restricted",
        observed_at="2026-07-20T00:00:00+00:00",
    )


def claim():
    return new_claim(
        statement="偏好短段落",
        source_kind="direct",
        evidence_ids=["ev_raw_1", "ev_raw_1"],
        claim_type="preference",
        confidence=0.8,
        now="2026-07-20T00:00:00+00:00",
    )


def judgment_card():
    return new_judgment_card(
        title="先做只读审计",
        situation="生产升级",
        decision="先审计再变更",
        evidence_ids=["ev_raw_1"],
        now="2026-07-20T00:00:00+00:00",
    )


def living_self_version():
    return new_living_self_version(
        sections={
            "identity_commitments": [],
            "values": [],
            "expression_dna": [],
            "mental_models": [],
            "decision_heuristics": [],
            "anti_patterns": [],
            "tensions": [],
            "honest_boundaries": [],
        },
        generation_reason="claim_change",
        based_on_claim_seq=7,
        content_hash="sha256:" + "b" * 64,
        now="2026-07-20T00:00:00+00:00",
    )


def context_pack():
    return new_context_pack(
        task="审查生产升级方案",
        mode="reviewer",
        living_self_version="lsv_1",
        now="2026-07-20T00:00:00+00:00",
        expires_at="2026-07-20T01:00:00+00:00",
    )


def test_claim_requires_evidence_unless_user_declared():
    assert_error(
        "evidence_required",
        lambda: new_claim(
            statement="偏好短段落",
            source_kind="direct",
            evidence_ids=[],
        ),
    )
    created = new_claim(
        statement="偏好短段落",
        source_kind="user_declared",
        evidence_ids=[],
    )
    assert created["source_kind"] == "user_declared"


def test_inferred_claim_cannot_start_confirmed():
    assert_error(
        "inferred_claim_requires_review",
        lambda: new_claim(
            statement="遇到风险时先隔离",
            source_kind="inferred",
            evidence_ids=["ev_raw_1"],
            status="confirmed",
        ),
    )


def test_claim_constructor_deduplicates_evidence_and_validates_scope():
    created = claim()
    assert created["evidence_ids"] == ["ev_raw_1"]
    assert created["schema_version"] == 1
    assert created["revision"] == 1
    invalid = copy.deepcopy(created)
    invalid["role_scope"] = ["custom"]
    assert_error("custom_scope_id_required", lambda: validate_claim(invalid))


def test_claim_constructor_accepts_stable_custom_scope_and_non_owner_ids():
    created = new_claim(
        statement="项目内使用严格审查",
        source_kind="quoted",
        evidence_ids=["ev_raw_1"],
        speaker_kind="other",
        speaker_id="person_1",
        subject_kind="owner",
        subject_id="owner",
        role_scope=["custom"],
        custom_scope_ids=["role_immortal"],
    )
    assert created["speaker"] == {"kind": "other", "id": "person_1"}
    assert created["custom_scope_ids"] == ["role_immortal"]


def test_claim_validation_has_stable_field_codes():
    invalid = claim()
    invalid["confidence"] = 1.2
    assert_error("invalid_confidence", lambda: validate_claim(invalid))
    invalid = claim()
    invalid["claim_type"] = "guess"
    assert_error("invalid_claim_type", lambda: validate_claim(invalid))
    invalid = claim()
    invalid["privacy"] = "secret"
    assert_error("invalid_privacy", lambda: validate_claim(invalid))


def test_event_envelope_separates_schema_and_stream_version():
    event = new_event(
        event_type="claim.created",
        stream_id="clm_1",
        stream_version=1,
        expected_version=0,
        request_id="req-1",
        idempotency_key="idem-1",
        actor={"kind": "owner", "id": "owner"},
        payload={"statement": "偏好短段落"},
        now="2026-07-20T00:00:00+00:00",
    )
    assert event["schema_version"] == 1
    assert event["stream_version"] == 1
    assert event["expected_version"] == 0
    assert event["previous_status"] is None
    invalid = copy.deepcopy(event)
    invalid["actor"] = {"kind": "admin", "id": "owner"}
    assert_error("invalid_actor_kind", lambda: validate_event(invalid))


def test_event_accepts_caller_supplied_stable_event_id_for_replay_tests():
    event = new_event(
        event_id="evt_fixture_1",
        event_type="claim.created",
        stream_id="clm_1",
        stream_version=1,
        expected_version=0,
        request_id="req-1",
        idempotency_key="idem-1",
        actor={"kind": "system", "id": "test"},
        payload={},
    )
    assert event["event_id"] == "evt_fixture_1"


def test_event_requires_nonempty_idempotency_and_consistent_versions():
    assert_error(
        "idempotency_key_required",
        lambda: new_event(
            event_type="claim.created",
            stream_id="clm_1",
            stream_version=1,
            expected_version=0,
            request_id="req-1",
            idempotency_key=" ",
            actor={"kind": "owner", "id": "owner"},
            payload={},
        ),
    )
    event = new_event(
        event_type="claim.created",
        stream_id="clm_1",
        stream_version=3,
        expected_version=2,
        request_id="req-1",
        idempotency_key="idem-1",
        actor={"kind": "system", "id": "service"},
        payload={},
    )
    event["expected_version"] = 1
    assert_error("invalid_stream_version", lambda: validate_event(event))


def test_evidence_ref_never_uses_sqlite_rowid_and_validates_hash():
    ref = evidence_ref()
    assert ref["evidence_id"] == "ev_raw_1"
    assert "rowid" not in ref
    invalid = copy.deepcopy(ref)
    invalid["content_hash"] = "sha256:bad"
    assert_error("invalid_content_hash", lambda: validate_evidence_ref(invalid))
    invalid = copy.deepcopy(ref)
    invalid["rowid"] = 3
    assert_error("rowid_forbidden", lambda: validate_evidence_ref(invalid))


def test_typed_ref_accepts_only_supported_kind_and_positive_revision():
    ref = new_typed_ref(kind="claim", id="clm_1", revision=2)
    assert ref == {"kind": "claim", "id": "clm_1", "revision": 2}
    assert_error(
        "invalid_ref_kind",
        lambda: new_typed_ref(kind="evidence", id="ev_1", revision=1),
    )
    invalid = {"kind": "claim", "id": "clm_1", "revision": 0}
    assert_error("invalid_revision", lambda: validate_typed_ref(invalid))


def test_living_self_version_requires_exact_eight_sections():
    version = living_self_version()
    assert version["status"] == "candidate"
    assert version["parent_version_id"] is None
    validate_living_self_version(version)
    invalid = copy.deepcopy(version)
    invalid["sections"].pop("tensions")
    assert_error("invalid_living_self_sections", lambda: validate_living_self_version(invalid))


def test_living_self_version_validates_reason_hash_and_claim_watermark():
    invalid = living_self_version()
    invalid["generation_reason"] = "automatic"
    assert_error("invalid_generation_reason", lambda: validate_living_self_version(invalid))
    invalid = living_self_version()
    invalid["based_on_claim_seq"] = -1
    assert_error("invalid_based_on_claim_seq", lambda: validate_living_self_version(invalid))


def test_judgment_card_defaults_to_unknown_outcome():
    card = judgment_card()
    assert card["status"] == "candidate"
    assert card["outcome"] == {
        "status": "unknown",
        "summary": "",
        "observed_at": None,
    }
    validate_judgment_card(card)


def test_judgment_card_requires_decision_and_evidence():
    assert_error(
        "decision_required",
        lambda: new_judgment_card(
            title="审计",
            situation="生产升级",
            decision=" ",
            evidence_ids=["ev_raw_1"],
        ),
    )
    invalid = judgment_card()
    invalid["outcome"]["status"] = "successful"
    assert_error("invalid_outcome_status", lambda: validate_judgment_card(invalid))


def test_context_pack_has_lifecycle_availability_budget_and_exact_sections():
    pack = context_pack()
    assert pack["lifecycle_status"] == "preview"
    assert pack["availability_status"] == "active"
    assert pack["budget"] == {"max_chars": 24000, "used_chars": 0}
    assert set(pack["sections"]) == {
        "verified_facts",
        "confirmed_self_models",
        "judgment_cards",
        "counter_evidence",
        "inferences",
        "unknowns",
    }
    validate_context_pack(pack)


def test_context_pack_rejects_invalid_status_and_overspent_budget():
    invalid = context_pack()
    invalid["lifecycle_status"] = "ready"
    assert_error("invalid_lifecycle_status", lambda: validate_context_pack(invalid))
    invalid = context_pack()
    invalid["budget"]["used_chars"] = 24001
    assert_error("context_budget_exceeded", lambda: validate_context_pack(invalid))


def test_outcome_event_contains_validated_confirmed_and_challenged_refs():
    confirmed = new_typed_ref(kind="claim", id="clm_1", revision=1)
    challenged = new_typed_ref(kind="judgment", id="jdg_1", revision=2)
    outcome = new_outcome_event(
        context_id="ctx_1",
        adopted="partial",
        result="mixed",
        summary="部分建议有效",
        confirmed_refs=[confirmed],
        challenged_refs=[challenged],
        now="2026-07-20T00:00:00+00:00",
    )
    assert outcome["confirmed_refs"] == [confirmed]
    assert outcome["challenged_refs"] == [challenged]
    validate_outcome_event(outcome)
    invalid = copy.deepcopy(outcome)
    invalid["adopted"] = "maybe"
    assert_error("invalid_adopted_status", lambda: validate_outcome_event(invalid))


def test_all_validators_reject_missing_required_fields_with_stable_code():
    cases = [
        (validate_claim, claim(), "claim_id"),
        (validate_event, new_event(
            event_type="claim.created",
            stream_id="clm_1",
            stream_version=1,
            expected_version=0,
            request_id="req-1",
            idempotency_key="idem-1",
            actor={"kind": "owner", "id": "owner"},
            payload={},
        ), "event_id"),
        (validate_evidence_ref, evidence_ref(), "evidence_id"),
        (validate_judgment_card, judgment_card(), "card_id"),
        (validate_living_self_version, living_self_version(), "version_id"),
        (validate_context_pack, context_pack(), "context_id"),
        (validate_outcome_event, new_outcome_event(context_id="ctx_1"), "outcome_id"),
    ]
    for validator, value, field in cases:
        value.pop(field)
        assert_error("missing_field", lambda validator=validator, value=value: validator(value))
