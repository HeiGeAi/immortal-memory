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
    new_self_model_item,
    new_typed_ref,
    validate_claim,
    validate_context_pack,
    validate_event,
    validate_evidence_ref,
    validate_judgment_card,
    validate_living_self_version,
    validate_outcome_event,
    validate_self_model_item,
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
        now="2026-07-20T00:00:00+00:00",
    )


def context_pack():
    return new_context_pack(
        task="审查生产升级方案",
        mode="reviewer",
        living_self_version="lsv_1",
        now="2099-07-20T00:00:00+00:00",
        expires_at="2099-07-20T01:00:00+00:00",
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


def test_claim_confidence_requires_a_consistent_explained_basis():
    created = new_claim(
        statement="高风险变更先审计",
        source_kind="direct",
        evidence_ids=["ev_raw_1"],
        confidence=0.8,
        confidence_basis={
            "speaker": 0.8,
            "recurrence": 0.4,
            "source_quality": 0.9,
            "explanation": "owner direct statement with verified evidence",
        },
    )
    validate_claim(created)

    unexplained = copy.deepcopy(created)
    unexplained["confidence_basis"]["explanation"] = ""
    assert_error(
        "confidence_explanation_required",
        lambda: validate_claim(unexplained),
    )

    unsupported = copy.deepcopy(created)
    unsupported["confidence"] = 0.95
    assert_error(
        "confidence_basis_inconsistent",
        lambda: validate_claim(unsupported),
    )


def test_claim_rejects_unknown_scopes_and_invalid_timestamp_order():
    invalid_scope = claim()
    invalid_scope["role_scope"] = ["wrk_typo"]
    assert_error("invalid_role_scope", lambda: validate_claim(invalid_scope))

    invalid_domain = claim()
    invalid_domain["domain_scope"] = ["anything"]
    assert_error("invalid_domain_scope", lambda: validate_claim(invalid_domain))

    invalid_time = claim()
    invalid_time["created_at"] = 123
    assert_error("invalid_timestamp", lambda: validate_claim(invalid_time))

    reversed_validity = claim()
    reversed_validity["valid_from"] = "2026-07-21T00:00:00+00:00"
    reversed_validity["valid_to"] = "2026-07-20T00:00:00+00:00"
    assert_error(
        "invalid_time_order",
        lambda: validate_claim(reversed_validity),
    )


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


def test_event_optional_fields_require_text_or_null():
    event = new_event(
        event_type="claim.created",
        stream_id="clm_1",
        stream_version=1,
        expected_version=0,
        request_id="req-1",
        idempotency_key="idem-1",
        actor={"kind": "owner", "id": "owner"},
        payload={},
    )
    invalid_status = copy.deepcopy(event)
    invalid_status["previous_status"] = {"status": "candidate"}
    assert_error(
        "invalid_previous_status",
        lambda: validate_event(invalid_status),
    )

    invalid_migration = copy.deepcopy(event)
    invalid_migration["migration_source"] = ["legacy"]
    assert_error(
        "invalid_migration_source",
        lambda: validate_event(invalid_migration),
    )


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


def test_evidence_ref_normalizes_real_l0_sources_without_losing_detail():
    obsidian = new_evidence_ref(
        evidence_id="obsidian-note-1",
        source="obsidian-note",
        raw_id="obsidian-note-1",
        content_hash="sha256:" + "a" * 64,
        status="available",
        privacy="restricted",
        observed_at="2026-07-20T00:00:00+00:00",
    )
    assert obsidian["source"] == "local"
    assert obsidian["source_detail"] == "obsidian-note"

    hermes = new_evidence_ref(
        evidence_id="hermes-1",
        source="hermes-conversation",
        raw_id="hermes-1",
        content_hash="sha256:" + "b" * 64,
        status="available",
        privacy="restricted",
        observed_at="2026-07-20T00:00:00+00:00",
    )
    assert hermes["source"] == "custom"
    assert hermes["source_detail"] == "hermes-conversation"
    validate_evidence_ref(hermes)


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


def test_self_model_item_has_complete_source_backed_contract():
    item = new_self_model_item(
        kind="mental_model",
        title="先形成独立预判",
        summary="在询问 AI 前先写下自己的判断。",
        evidence_ids=["ev_raw_1"],
        claim_ids=["clm_1"],
        confidence=0.8,
        validation={
            "cross_domain_recurrence": 2,
            "generative_power": "tested",
            "distinctiveness": "high",
        },
        role_scope=["work"],
        domain_scope=["technical"],
        based_on_claim_seq=7,
        now="2026-07-20T00:00:00+00:00",
    )

    validate_self_model_item(item)
    assert item["schema_version"] == 1
    assert item["revision"] == 1
    assert item["item_id"].startswith("self_")


def test_living_self_rejects_invalid_items_forged_hash_and_confirmation_state():
    version = living_self_version()
    invalid_item = copy.deepcopy(version)
    invalid_item["sections"]["mental_models"] = [{"status": "confirmed"}]
    assert_error(
        "missing_field",
        lambda: validate_living_self_version(invalid_item),
    )

    wrong_section = living_self_version()
    wrong_section["sections"]["values"] = [
        new_self_model_item(
            kind="mental_model",
            title="错误分区",
            summary="kind 与 section 不匹配。",
            evidence_ids=["ev_raw_1"],
            claim_ids=["clm_1"],
            now="2026-07-20T00:00:00+00:00",
        )
    ]
    assert_error(
        "self_model_section_mismatch",
        lambda: validate_living_self_version(wrong_section),
    )

    forged = living_self_version()
    forged["content_hash"] = "sha256:" + "f" * 64
    assert_error(
        "content_hash_mismatch",
        lambda: validate_living_self_version(forged),
    )

    confirmed_without_time = living_self_version()
    confirmed_without_time["status"] = "confirmed"
    confirmed_without_time["confirmed_at"] = None
    assert_error(
        "confirmed_at_required",
        lambda: validate_living_self_version(confirmed_without_time),
    )


def test_self_model_item_validates_scope_and_temporal_contract():
    item = new_self_model_item(
        kind="mental_model",
        title="先形成独立预判",
        summary="在询问 AI 前先形成自己的判断。",
        evidence_ids=["ev_raw_1"],
        claim_ids=["clm_1"],
        now="2026-07-20T00:00:00+00:00",
    )
    invalid_scope = copy.deepcopy(item)
    invalid_scope["domain_scope"] = ["anything"]
    assert_error(
        "invalid_domain_scope",
        lambda: validate_self_model_item(invalid_scope),
    )

    invalid_timestamp = copy.deepcopy(item)
    invalid_timestamp["last_reviewed_at"] = "2026-07-20"
    assert_error(
        "invalid_timestamp",
        lambda: validate_self_model_item(invalid_timestamp),
    )

    reversed_validity = copy.deepcopy(item)
    reversed_validity["valid_to"] = "2026-07-19T00:00:00+00:00"
    assert_error(
        "invalid_time_order",
        lambda: validate_self_model_item(reversed_validity),
    )


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


def test_judgment_non_unknown_outcome_requires_observation_time():
    invalid = judgment_card()
    invalid["outcome"] = {
        "status": "positive",
        "summary": "执行有效",
        "observed_at": None,
    }
    assert_error(
        "outcome_observed_at_required",
        lambda: validate_judgment_card(invalid),
    )

    valid = judgment_card()
    valid["outcome"] = {
        "status": "mixed",
        "summary": "部分有效",
        "observed_at": "2026-07-20T01:00:00+00:00",
    }
    validate_judgment_card(valid)


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


def test_context_pack_rejects_tampered_content_hash_and_false_used_chars():
    tampered = context_pack()
    tampered["task"] = "篡改后的任务"
    assert_error(
        "content_hash_mismatch",
        lambda: validate_context_pack(tampered),
    )

    pack = new_context_pack(
        task="带内容的上下文",
        mode="reviewer",
        living_self_version="lsv_1",
        sections={
            "verified_facts": [{"summary": "已验证事实"}],
            "confirmed_self_models": [],
            "judgment_cards": [],
            "counter_evidence": [],
            "inferences": [],
            "unknowns": [],
        },
        now="2099-07-20T00:00:00+00:00",
        expires_at="2099-07-20T01:00:00+00:00",
    )
    assert pack["budget"]["used_chars"] > 0
    pack["budget"]["used_chars"] = 0
    assert_error(
        "context_used_chars_mismatch",
        lambda: validate_context_pack(pack),
    )


def test_context_pack_rejects_private_raw_body_and_section_item_overflow():
    private_sections = {
        "verified_facts": [
            {
                "summary": "不得进入上下文",
                "privacy": "private",
                "raw_body": "secret",
            }
        ],
        "confirmed_self_models": [],
        "judgment_cards": [],
        "counter_evidence": [],
        "inferences": [],
        "unknowns": [],
    }
    assert_error(
        "private_context_item_forbidden",
        lambda: new_context_pack(
            task="隐私验证",
            mode="reviewer",
            living_self_version="lsv_1",
            sections=private_sections,
            now="2099-07-20T00:00:00+00:00",
            expires_at="2099-07-20T01:00:00+00:00",
        ),
    )

    overflow = copy.deepcopy(private_sections)
    overflow["verified_facts"] = [
        {"summary": f"事实 {index}", "privacy": "context_safe"}
        for index in range(21)
    ]
    assert_error(
        "context_section_item_limit",
        lambda: new_context_pack(
            task="数量验证",
            mode="reviewer",
            living_self_version="lsv_1",
            sections=overflow,
            now="2099-07-20T00:00:00+00:00",
            expires_at="2099-07-20T01:00:00+00:00",
        ),
    )


def test_context_pack_requires_hash_shaped_preview_and_coherent_ttl_status():
    invalid_preview = context_pack()
    invalid_preview["preview_hash"] = "not-a-hash"
    assert_error(
        "invalid_preview_hash",
        lambda: validate_context_pack(invalid_preview),
    )

    assert_error(
        "context_expired",
        lambda: new_context_pack(
            task="已经过期",
            mode="reviewer",
            living_self_version="lsv_1",
            availability_status="active",
            now="2020-07-20T00:00:00+00:00",
            expires_at="2020-07-20T01:00:00+00:00",
        ),
    )

    assert_error(
        "context_not_expired",
        lambda: new_context_pack(
            task="尚未过期",
            mode="reviewer",
            living_self_version="lsv_1",
            availability_status="expired",
            now="2099-07-20T00:00:00+00:00",
            expires_at="2099-07-20T01:00:00+00:00",
        ),
    )

    reversed_ttl = context_pack()
    reversed_ttl["expires_at"] = "2099-07-19T00:00:00+00:00"
    assert_error("invalid_time_order", lambda: validate_context_pack(reversed_ttl))


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


def test_outcome_confirmed_and_challenged_refs_must_be_disjoint():
    ref = new_typed_ref(kind="claim", id="clm_1", revision=1)
    assert_error(
        "outcome_ref_conflict",
        lambda: new_outcome_event(
            context_id="ctx_1",
            confirmed_refs=[ref],
            challenged_refs=[ref],
        ),
    )


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


@pytest.mark.parametrize(
    "operation",
    [
        lambda: new_claim(
            statement="错误输入",
            source_kind="direct",
            evidence_ids=None,
        ),
        lambda: new_event(
            event_type="claim.created",
            stream_id="clm_1",
            stream_version=1,
            expected_version=0,
            request_id="req-1",
            idempotency_key="idem-1",
            actor=None,
            payload={},
        ),
        lambda: new_evidence_ref(
            evidence_id="ev_1",
            source=None,
            raw_id=None,
            content_hash="sha256:" + "a" * 64,
            status="available",
            privacy="restricted",
        ),
        lambda: new_typed_ref(kind="claim", id=None, revision=1),
        lambda: new_self_model_item(
            kind="mental_model",
            title="错误输入",
            summary="错误输入",
            evidence_ids=None,
            claim_ids=["clm_1"],
        ),
        lambda: new_living_self_version(
            sections=None,
            generation_reason="claim_change",
            based_on_claim_seq=0,
        ),
        lambda: new_judgment_card(
            title="错误输入",
            situation="错误输入",
            decision="错误输入",
            evidence_ids=None,
        ),
        lambda: new_context_pack(
            task="错误输入",
            mode="reviewer",
            living_self_version="lsv_1",
            sections=42,
        ),
        lambda: new_outcome_event(
            context_id="ctx_1",
            confirmed_refs=[None],
        ),
    ],
)
def test_all_constructors_return_stable_validation_errors_for_invalid_types(
    operation,
):
    with pytest.raises(ModelValidationError) as exc_info:
        operation()
    assert exc_info.value.code
