import copy
import json

import profile_nuwa
from claim_store import ClaimStore
from model_types import (
    LIVING_SELF_SECTIONS,
    new_claim,
    validate_living_self_version,
    validate_self_model_item,
)


ACTOR = {"kind": "owner", "id": "owner"}


def add_claim(
    store,
    claim_id,
    statement,
    *,
    claim_type="lesson",
    status="confirmed",
    evidence_ids=None,
    counter_evidence_ids=None,
    domain="business",
    role="work",
    source_kind="direct",
    privacy="context_safe",
    at="2026-03-01T00:00:00+00:00",
    valid_from=None,
    valid_to=None,
):
    value = new_claim(
        statement=statement,
        source_kind=source_kind,
        evidence_ids=evidence_ids or ["ev_" + claim_id],
        claim_type=claim_type,
        role_scope=[role],
        domain_scope=[domain],
        privacy=privacy,
        now=at,
    )
    value["claim_id"] = claim_id
    value["counter_evidence_ids"] = list(counter_evidence_ids or [])
    value["valid_from"] = valid_from
    value["valid_to"] = valid_to
    created = store.create(
        value,
        expected_revision=0,
        request_id="req_create_" + claim_id,
        idempotency_key="idem_create_" + claim_id,
        actor=ACTOR,
        reason="test evidence",
    )
    if status == "candidate":
        return created
    return store.transition(
        claim_id,
        status,
        reason="test transition",
        expected_revision=created["revision"],
        request_id="req_transition_" + claim_id,
        idempotency_key="idem_transition_" + claim_id,
        actor=ACTOR,
    )


def service_for(tmp_path):
    from living_self_service import LivingSelfService

    return LivingSelfService(
        tmp_path,
        clock=lambda: "2026-07-20T12:00:00+00:00",
    )


def test_build_candidate_has_exactly_eight_sections_and_valid_contract(tmp_path):
    service = service_for(tmp_path)

    model = service.build_candidate()

    assert set(model["sections"]) == LIVING_SELF_SECTIONS
    assert all(model["sections"][name] == [] for name in LIVING_SELF_SECTIONS)
    assert model["generation_reason"] == "claim_change"
    assert model["based_on_claim_seq"] == 0
    validate_living_self_version(model)


def test_single_transient_claim_does_not_become_mental_model(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_request",
        "这一次先快速做",
        claim_type="request",
        domain="project",
    )
    add_claim(
        service.claims,
        "clm_emotion",
        "今天有点焦虑",
        claim_type="emotion",
        domain="general",
    )

    model = service.build_candidate()

    assert all(
        not items
        for section, items in model["sections"].items()
        if section != "honest_boundaries"
    )


def test_only_confirmed_current_non_private_claims_are_sources(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_candidate",
        "先独立预判再问 AI",
        status="candidate",
        at="2026-03-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_rejected",
        "代码审查先形成自己的判断",
        status="rejected",
        domain="technical",
        at="2026-06-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_expired",
        "风险决策先形成独立判断",
        domain="risk",
        at="2026-04-01T00:00:00+00:00",
        valid_to="2026-05-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_private",
        "我的私密原文绝不能被复制到模型",
        claim_type="value",
        privacy="private",
    )

    model = service.build_candidate()
    serialized = json.dumps(model, ensure_ascii=False)

    assert all(not items for items in model["sections"].values())
    assert "我的私密原文" not in serialized
    assert model["based_on_claim_seq"] == max(
        claim["based_on_event_seq"] for claim in service.claims.list()
    )


def test_future_valid_from_claim_is_not_included_before_it_becomes_effective(
    tmp_path,
):
    current_time = {"value": "2026-07-20T12:00:00+00:00"}
    from living_self_service import LivingSelfService

    service = LivingSelfService(
        tmp_path,
        clock=lambda: current_time["value"],
    )
    add_claim(
        service.claims,
        "clm_future_value",
        "长期选择可恢复性",
        claim_type="value",
        valid_from="2026-07-21T00:00:00+00:00",
    )

    before = service.build_candidate()
    current_time["value"] = "2026-07-21T00:00:00+00:00"
    after = service.build_candidate()

    assert before["sections"]["values"] == []
    assert before["generated_at"] == "2026-07-20T12:00:00+00:00"
    assert after["sections"]["values"][0]["claim_ids"] == ["clm_future_value"]


def test_long_lived_service_excludes_claim_after_valid_to(tmp_path):
    current_time = {"value": "2026-07-20T12:00:00+00:00"}
    from living_self_service import LivingSelfService

    service = LivingSelfService(
        tmp_path,
        clock=lambda: current_time["value"],
    )
    add_claim(
        service.claims,
        "clm_expiring_value",
        "长期选择可恢复性",
        claim_type="value",
        valid_from="2026-07-01T00:00:00+00:00",
        valid_to="2026-07-21T00:00:00+00:00",
    )

    before = service.build_candidate()
    current_time["value"] = "2026-07-21T00:00:00+00:00"
    after = service.build_candidate()

    assert before["sections"]["values"][0]["claim_ids"] == [
        "clm_expiring_value"
    ]
    assert after["sections"]["values"] == []


def test_invalid_or_naive_validity_timestamps_fail_closed():
    from living_self_service import _is_effective

    base = {
        "created_at": "2026-07-01T00:00:00+00:00",
        "valid_from": "2026-07-01T00:00:00+00:00",
        "valid_to": None,
    }
    assert _is_effective(base, "2026-07-20T00:00:00+00:00") is True

    naive_from = dict(base, valid_from="2026-07-01T00:00:00")
    invalid_to = dict(base, valid_to="not-a-timestamp")

    assert _is_effective(naive_from, "2026-07-20T00:00:00+00:00") is False
    assert _is_effective(invalid_to, "2026-07-20T00:00:00+00:00") is False
    assert _is_effective(base, "2026-07-20T00:00:00") is False


def test_inactive_claims_do_not_choose_candidate_generation_time(tmp_path):
    current_time = {"value": "2026-07-20T12:00:00+00:00"}
    from living_self_service import LivingSelfService

    service = LivingSelfService(
        tmp_path,
        clock=lambda: current_time["value"],
    )
    add_claim(
        service.claims,
        "clm_future_candidate",
        "未审核且未来的 Claim",
        claim_type="value",
        status="candidate",
        at="2026-08-01T00:00:00+00:00",
    )

    model = service.build_candidate()

    assert model["sections"]["values"] == []
    assert model["generated_at"] == current_time["value"]


def test_cross_time_cross_domain_claims_form_candidate_mental_model(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_business",
        "先独立预判再问 AI",
        evidence_ids=["ev_1"],
        domain="business",
        at="2026-03-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_technical",
        "代码审查先形成自己的判断",
        evidence_ids=["ev_2"],
        domain="technical",
        at="2026-06-01T00:00:00+00:00",
    )

    model = service.build_candidate()
    item = model["sections"]["mental_models"][0]

    assert item["validation"]["cross_domain_recurrence"] == 2
    assert item["status"] == "candidate"
    assert item["evidence_ids"] == ["ev_1", "ev_2"]
    assert item["claim_ids"] == ["clm_business", "clm_technical"]
    assert item["domain_scope"] == ["business", "technical"]
    validate_self_model_item(item)


def test_same_domain_or_same_authority_time_does_not_meet_mental_threshold(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_one",
        "先独立预判再问 AI",
        domain="business",
        at="2026-03-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_two",
        "形成自己的判断再使用工具",
        domain="business",
        at="2026-03-01T00:00:00+00:00",
    )

    model = service.build_candidate()

    assert model["sections"]["mental_models"] == []


def test_counter_evidence_lowers_confidence_and_remains_visible(tmp_path):
    baseline = service_for(tmp_path / "baseline")
    challenged = service_for(tmp_path / "challenged")
    for service in (baseline, challenged):
        add_claim(
            service.claims,
            "clm_one",
            "先独立预判再问 AI",
            evidence_ids=["ev_1"],
            domain="business",
            at="2026-03-01T00:00:00+00:00",
        )
        add_claim(
            service.claims,
            "clm_two",
            "代码审查先形成自己的判断",
            evidence_ids=["ev_2"],
            counter_evidence_ids=(
                ["ev_counter"] if service is challenged else []
            ),
            domain="technical",
            at="2026-06-01T00:00:00+00:00",
        )

    baseline_item = baseline.build_candidate()["sections"]["mental_models"][0]
    challenged_item = challenged.build_candidate()["sections"]["mental_models"][0]

    assert challenged_item["counter_evidence_ids"] == ["ev_counter"]
    assert challenged_item["confidence"] < baseline_item["confidence"]


def test_conflicting_confirmed_claims_become_tension(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_speed",
        "先快速验证",
        claim_type="decision",
        evidence_ids=["ev_speed"],
        domain="project",
        at="2026-03-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_assurance",
        "高风险变更先完整审查",
        claim_type="decision",
        evidence_ids=["ev_assurance"],
        domain="risk",
        at="2026-04-01T00:00:00+00:00",
    )

    model = service.build_candidate()
    tension = model["sections"]["tensions"][0]

    assert tension["poles"] == ["speed", "assurance"]
    assert tension["claim_ids"] == ["clm_assurance", "clm_speed"]
    assert {"clm_speed", "clm_assurance"} <= {
        claim_id
        for section in model["sections"].values()
        for item in section
        for claim_id in item["claim_ids"]
    }


def test_expression_dna_is_not_reused_as_judgment_model(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_style_one",
        "表达时先给结论",
        claim_type="style",
        domain="content",
        at="2026-03-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_style_two",
        "沟通时先说结论",
        claim_type="style",
        domain="business",
        at="2026-04-01T00:00:00+00:00",
    )

    sections = service.build_candidate()["sections"]

    assert sections["expression_dna"]
    style_claim_ids = set(sections["expression_dna"][0]["claim_ids"])
    assert all(
        style_claim_ids.isdisjoint(item["claim_ids"])
        for section in ("mental_models", "decision_heuristics")
        for item in sections[section]
    )


def test_item_identity_content_hash_and_order_are_rebuild_deterministic(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_z",
        "代码审查先形成自己的判断",
        evidence_ids=["ev_z"],
        domain="technical",
        at="2026-06-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_a",
        "先独立预判再问 AI",
        evidence_ids=["ev_a"],
        domain="business",
        at="2026-03-01T00:00:00+00:00",
    )

    first = service.build_candidate()
    second = service.build_candidate()

    assert first == second
    assert first["version_id"].startswith("lsv_")
    assert first["content_hash"].startswith("sha256:")
    assert first["sections"]["mental_models"][0]["claim_ids"] == [
        "clm_a",
        "clm_z",
    ]


def test_nonconfirmed_claim_does_not_change_existing_section_content(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_a",
        "先独立预判再问 AI",
        domain="business",
        at="2026-03-01T00:00:00+00:00",
    )
    add_claim(
        service.claims,
        "clm_b",
        "代码审查先形成自己的判断",
        domain="technical",
        at="2026-06-01T00:00:00+00:00",
    )
    before = service.build_candidate()
    add_claim(
        service.claims,
        "clm_unreviewed",
        "这条候选不能改变长期模型内容",
        claim_type="value",
        status="candidate",
        at="2026-07-21T00:00:00+00:00",
    )

    after = service.build_candidate()

    assert after["sections"] == before["sections"]
    assert after["content_hash"] == before["content_hash"]
    assert after["based_on_claim_seq"] > before["based_on_claim_seq"]


def test_existing_current_is_parent_only_and_is_never_overwritten(tmp_path):
    service = service_for(tmp_path)
    current = copy.deepcopy(service.build_candidate())
    current["status"] = "confirmed"
    current["confirmed_at"] = current["generated_at"]
    service.current_path.parent.mkdir(parents=True)
    service.current_path.write_text(
        json.dumps(current, ensure_ascii=False),
        encoding="utf-8",
    )

    candidate = service.build_candidate()

    assert candidate["parent_version_id"] == current["version_id"]
    assert json.loads(service.current_path.read_text(encoding="utf-8")) == current


def test_malformed_or_unconfirmed_current_is_not_trusted_as_parent(tmp_path):
    service = service_for(tmp_path)
    service.current_path.parent.mkdir(parents=True)
    service.current_path.write_text(
        json.dumps({"version_id": "lsv_forged"}),
        encoding="utf-8",
    )
    assert service.build_candidate()["parent_version_id"] is None

    candidate = service.build_candidate()
    service.current_path.write_text(
        json.dumps(candidate, ensure_ascii=False),
        encoding="utf-8",
    )
    assert service.build_candidate()["parent_version_id"] is None


def test_profile_nuwa_exports_living_self_current_when_present(
    tmp_path,
    monkeypatch,
):
    service = service_for(tmp_path)
    current = copy.deepcopy(service.build_candidate())
    current["status"] = "confirmed"
    current["confirmed_at"] = current["generated_at"]
    service.current_path.parent.mkdir(parents=True)
    service.current_path.write_text(
        json.dumps(current, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(profile_nuwa, "LIVING_SELF_CURRENT", service.current_path)

    report = profile_nuwa.build_report()

    assert report["output_mode"] == "living_self_compat_export"
    assert report["legacy_rule_output"] is False
    assert report["living_self"]["version_id"] == current["version_id"]
    assert set(report["sections"]) == LIVING_SELF_SECTIONS


def test_profile_nuwa_renders_all_living_self_sections(tmp_path, monkeypatch):
    service = service_for(tmp_path)
    current = copy.deepcopy(service.build_candidate())
    current["status"] = "confirmed"
    current["confirmed_at"] = current["generated_at"]
    service.current_path.parent.mkdir(parents=True)
    service.current_path.write_text(
        json.dumps(current, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(profile_nuwa, "LIVING_SELF_CURRENT", service.current_path)

    markdown = profile_nuwa.render_markdown(profile_nuwa.build_report())

    for section in LIVING_SELF_SECTIONS:
        assert section in markdown
    assert "Compatibility Export" in markdown


def test_profile_nuwa_labels_legacy_output_and_does_not_confirm_accepted(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        profile_nuwa,
        "LIVING_SELF_CURRENT",
        tmp_path / "missing-current.json",
    )
    monkeypatch.setattr(profile_nuwa, "PROFILE_JSON", tmp_path / "profile.json")
    monkeypatch.setattr(
        profile_nuwa,
        "REVIEWED_PROFILE_JSONL",
        tmp_path / "reviewed.jsonl",
    )

    report = profile_nuwa.build_report()

    assert report["output_mode"] == "legacy_rule_output"
    assert report["legacy_rule_output"] is True
    assert all(
        model.get("status") != "confirmed"
        for model in report["mental_models"]
    )
