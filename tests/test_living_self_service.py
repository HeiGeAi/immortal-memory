import copy
import hashlib
import json
import os
import threading

import profile_nuwa
import pytest
import living_self_service as living_module
from claim_store import ClaimStore
from event_store import EventPathError
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


def seeded_version_service(tmp_path):
    service = service_for(tmp_path)
    add_claim(
        service.claims,
        "clm_version_value",
        "长期选择可恢复性",
        claim_type="value",
        evidence_ids=["ev_version_value"],
    )
    return service


def section_hash(sections):
    encoded = json.dumps(
        sections,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_confirm_creates_immutable_version_and_current_pair(tmp_path):
    service = seeded_version_service(tmp_path)

    confirmed = service.confirm(service.build_candidate(), reason="owner reviewed")
    version_json = (
        tmp_path
        / "model"
        / "living-self"
        / "versions"
        / (confirmed["version_id"] + ".json")
    )
    version_md = version_json.with_suffix(".md")

    assert version_json.is_file()
    assert version_md.is_file()
    assert service.current()["version_id"] == confirmed["version_id"]
    assert service.current_path.with_suffix(".md").is_file()
    assert service.versions() == [confirmed]
    assert service.load_version(confirmed["version_id"]) == confirmed
    assert "ev_version_value" in version_md.read_text(encoding="utf-8")
    assert "raw evidence body" not in version_md.read_text(encoding="utf-8")


def test_confirm_rejects_tampering_stale_watermark_parent_and_empty_reason(
    tmp_path,
):
    service = seeded_version_service(tmp_path)
    candidate = service.build_candidate()

    with pytest.raises(ValueError, match="reason"):
        service.confirm(candidate, reason=" ")

    tampered = copy.deepcopy(candidate)
    tampered["sections"]["values"][0]["summary"] = "caller tampered"
    tampered["content_hash"] = section_hash(tampered["sections"])
    with pytest.raises(ValueError, match="candidate"):
        service.confirm(tampered, reason="reviewed")

    stale = copy.deepcopy(candidate)
    stale["based_on_claim_seq"] -= 1
    with pytest.raises(ValueError, match="candidate"):
        service.confirm(stale, reason="reviewed")

    forged_parent = copy.deepcopy(candidate)
    forged_parent["parent_version_id"] = "lsv_" + "f" * 32
    with pytest.raises(ValueError, match="candidate"):
        service.confirm(forged_parent, reason="reviewed")

def test_confirm_normalizes_untrusted_generation_metadata_from_fresh_candidate(
    tmp_path,
):
    service = service_for(tmp_path)
    candidate = service.build_candidate()
    forged = copy.deepcopy(candidate)
    forged["generated_at"] = "2000-01-01T00:00:00+00:00"
    forged["generation_reason"] = "scheduled_rebuild"

    confirmed = service.confirm(forged, reason="reviewed")

    assert confirmed["generated_at"] == candidate["generated_at"]
    assert confirmed["generation_reason"] == "claim_change"


def test_default_clock_empty_candidate_can_be_confirmed(tmp_path):
    from living_self_service import LivingSelfService

    service = LivingSelfService(tmp_path)
    candidate = service.build_candidate()

    confirmed = service.confirm(candidate, reason="reviewed")

    assert confirmed["status"] == "confirmed"
    assert confirmed["sections"] == candidate["sections"]
    assert confirmed["based_on_claim_seq"] == candidate["based_on_claim_seq"]


def test_confirm_never_overwrites_existing_version(tmp_path):
    service = seeded_version_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="first")
    first_path = service.versions_dir / (first["version_id"] + ".json")
    original = first_path.read_bytes()

    service._new_version_id = lambda: first["version_id"]
    with pytest.raises(FileExistsError):
        service.confirm(service.build_candidate(), reason="collision")

    assert first_path.read_bytes() == original
    assert service.current()["version_id"] == first["version_id"]


def test_restore_creates_new_version_and_preserves_history_bytes(tmp_path):
    service = seeded_version_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="first")
    first_json = service.versions_dir / (first["version_id"] + ".json")
    first_md = first_json.with_suffix(".md")
    original_json = first_json.read_bytes()
    original_md = first_md.read_bytes()
    add_claim(
        service.claims,
        "clm_second_value",
        "优先可验证交付",
        claim_type="value",
    )
    second = service.confirm(service.build_candidate(), reason="second")

    restored = service.restore(first["version_id"], reason="rollback")

    assert restored["version_id"] not in {
        first["version_id"],
        second["version_id"],
    }
    assert restored["generation_reason"] == "manual_restore"
    assert restored["restored_from"] == first["version_id"]
    assert restored["parent_version_id"] == second["version_id"]
    assert first_json.read_bytes() == original_json
    assert first_md.read_bytes() == original_md
    assert len(service.versions()) == 3
    assert service.current()["version_id"] == restored["version_id"]


def test_restore_rejects_candidate_corrupt_and_missing_history(tmp_path):
    service = seeded_version_service(tmp_path)
    confirmed = service.confirm(service.build_candidate(), reason="first")

    with pytest.raises(ValueError, match="reason"):
        service.restore(confirmed["version_id"], reason="")

    with pytest.raises(FileNotFoundError):
        service.restore("lsv_" + "a" * 32, reason="missing")

    path = service.versions_dir / (confirmed["version_id"] + ".json")
    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["content_hash"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(ValueError):
        service.restore(confirmed["version_id"], reason="corrupt")


def test_missing_version_markdown_is_rebuilt_from_valid_json(tmp_path):
    service = seeded_version_service(tmp_path)
    confirmed = service.confirm(service.build_candidate(), reason="first")
    markdown_path = service.versions_dir / (confirmed["version_id"] + ".md")
    markdown_path.unlink()

    loaded = service.load_version(confirmed["version_id"])

    assert loaded == confirmed
    assert markdown_path.read_text(encoding="utf-8") == service._render_markdown(
        confirmed
    )


def test_stale_current_markdown_is_rebuilt_from_authoritative_json(tmp_path):
    service = seeded_version_service(tmp_path)
    confirmed = service.confirm(service.build_candidate(), reason="first")
    service.current_md_path.write_text(
        "stale cache containing raw evidence body",
        encoding="utf-8",
    )

    loaded = service.current()

    assert loaded == confirmed
    repaired = service.current_md_path.read_text(encoding="utf-8")
    assert repaired == service._render_markdown(confirmed)
    assert "raw evidence body" not in repaired


def test_interrupted_version_markdown_write_recovers_from_json(
    tmp_path,
    monkeypatch,
):
    service = seeded_version_service(tmp_path)
    real_write = living_module.safe_atomic_write_text
    failed = {"value": False}

    def fail_version_markdown(path, content):
        if (
            path.parent == service.versions_dir
            and path.suffix == ".md"
            and not failed["value"]
        ):
            failed["value"] = True
            raise OSError("interrupted version markdown")
        return real_write(path, content)

    monkeypatch.setattr(
        living_module,
        "safe_atomic_write_text",
        fail_version_markdown,
    )
    candidate = service.build_candidate()
    confirmed = service.confirm(candidate, reason="first")

    versions = service.versions()

    assert versions == [confirmed]
    assert service.current()["version_id"] == confirmed["version_id"]
    assert (
        service.versions_dir / (confirmed["version_id"] + ".md")
    ).is_file()
    with pytest.raises(living_module.LivingSelfConflict) as stale:
        service.confirm(candidate, reason="retry")
    assert stale.value.code == "stale_candidate"
    assert service.versions() == [confirmed]


def test_interrupted_current_markdown_write_recovers_from_json(
    tmp_path,
    monkeypatch,
):
    service = seeded_version_service(tmp_path)
    real_write = living_module.safe_atomic_write_text
    failed = {"value": False}

    def fail_current_markdown(path, content):
        if path == service.current_md_path and not failed["value"]:
            failed["value"] = True
            raise OSError("interrupted current markdown")
        return real_write(path, content)

    monkeypatch.setattr(
        living_module,
        "safe_atomic_write_text",
        fail_current_markdown,
    )
    confirmed = service.confirm(service.build_candidate(), reason="first")

    current = service.current()

    assert current == confirmed
    assert service.current_md_path.read_text(
        encoding="utf-8"
    ) == service._render_markdown(current)


def test_diff_lists_added_and_removed_items(tmp_path):
    current_time = {"value": "2026-07-20T12:00:00+00:00"}
    from living_self_service import LivingSelfService

    service = LivingSelfService(
        tmp_path,
        clock=lambda: current_time["value"],
    )
    add_claim(
        service.claims,
        "clm_expiring",
        "阶段性优先可恢复性",
        claim_type="value",
        valid_to="2026-07-21T00:00:00+00:00",
    )
    first = service.confirm(service.build_candidate(), reason="first")
    add_claim(
        service.claims,
        "clm_added",
        "长期优先可验证交付",
        claim_type="value",
    )
    current_time["value"] = "2026-07-21T00:00:00+00:00"
    second = service.confirm(service.build_candidate(), reason="second")

    result = service.diff(first["version_id"], second["version_id"])

    assert [item["item"]["claim_ids"] for item in result["added"]] == [
        ["clm_added"]
    ]
    assert [item["item"]["claim_ids"] for item in result["removed"]] == [
        ["clm_expiring"]
    ]
    assert result["changed"] == []


def test_diff_lists_added_changed_removed_and_section_moves(tmp_path):
    service = seeded_version_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="first")
    second = copy.deepcopy(first)
    second["version_id"] = "lsv_" + "b" * 32
    second["parent_version_id"] = first["version_id"]
    moved = second["sections"]["values"].pop()
    moved["kind"] = "identity_commitment"
    moved["summary"] = "same item moved to a new section"
    second["sections"]["identity_commitments"].append(moved)
    second["content_hash"] = section_hash(second["sections"])
    second["reason"] = "fixture for structural diff"
    second_path = service.versions_dir / (second["version_id"] + ".json")
    second_path.write_text(
        json.dumps(second, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    second_path.with_suffix(".md").write_text("fixture", encoding="utf-8")

    result = service.diff(first["version_id"], second["version_id"])

    assert set(result) == {"added", "changed", "removed"}
    assert result["added"] == []
    assert result["removed"] == []
    assert result["changed"][0]["item_id"] == moved["item_id"]
    assert result["changed"][0]["from_section"] == "values"
    assert result["changed"][0]["to_section"] == "identity_commitments"


def test_concurrent_confirms_allow_one_linear_winner_without_partial_pairs(
    tmp_path,
):
    service = seeded_version_service(tmp_path)
    candidate = service.build_candidate()
    results = []
    failures = []

    def worker(index):
        try:
            results.append(
                service.confirm(candidate, reason="worker " + str(index))
            )
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(failures) == 3
    assert {failure.code for failure in failures} == {"stale_candidate"}
    assert len(service.versions()) == 1
    for item in results:
        base = service.versions_dir / item["version_id"]
        assert base.with_suffix(".json").is_file()
        assert base.with_suffix(".md").is_file()
    assert service.current()["version_id"] == results[0]["version_id"]
    assert sorted(path.suffix for path in service.versions_dir.iterdir()) == [
        ".json",
        ".md",
    ]


def test_version_paths_reject_symlinks_and_nonregular_files(tmp_path):
    service = seeded_version_service(tmp_path)
    candidate = service.build_candidate()
    outside = tmp_path / "outside"
    outside.mkdir()
    service.root.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, service.root)

    with pytest.raises(EventPathError):
        service.confirm(candidate, reason="unsafe root")


def test_version_id_rejects_traversal_and_nonregular_history(tmp_path):
    service = seeded_version_service(tmp_path)
    with pytest.raises(ValueError, match="version ID"):
        service.load_version("../outside")

    service.versions_dir.mkdir(parents=True)
    version_id = "lsv_" + "d" * 32
    (service.versions_dir / (version_id + ".json")).mkdir()
    with pytest.raises(EventPathError):
        service.load_version(version_id)


def test_recoverable_restore_reuses_preallocated_version_after_history_publish(tmp_path):
    service = seeded_version_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="initial")
    result_id = "lsv_" + "a" * 32

    original_publish = service._publish_current_pair
    service._publish_current_pair = lambda _version: (_ for _ in ()).throw(
        RuntimeError("crash after immutable history")
    )
    with pytest.raises(RuntimeError, match="crash after immutable"):
        service.restore(
            first["version_id"],
            reason="recoverable restore",
            result_version_id=result_id,
            expected_parent_version_id=first["version_id"],
        )

    service._publish_current_pair = original_publish
    recovered = service.restore(
        first["version_id"],
        reason="recoverable restore",
        result_version_id=result_id,
        expected_parent_version_id=first["version_id"],
    )

    assert recovered["version_id"] == result_id
    assert service.current()["version_id"] == result_id
    assert [v["version_id"] for v in service.versions()].count(result_id) == 1


def test_recoverable_restore_rejects_third_party_current(tmp_path):
    service = seeded_version_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="initial")
    service.restore(first["version_id"], reason="other writer")

    with pytest.raises(living_module.LivingSelfConflict) as caught:
        service.restore(
            first["version_id"],
            reason="stale request",
            result_version_id="lsv_" + "b" * 32,
            expected_parent_version_id=first["version_id"],
        )

    assert caught.value.code == "version_conflict"


def test_recoverable_restore_completed_retry_returns_same_version(tmp_path):
    service = seeded_version_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="initial")
    result_id = "lsv_" + "c" * 32
    first_result = service.restore(
        first["version_id"],
        reason="fixed restore",
        result_version_id=result_id,
        expected_parent_version_id=first["version_id"],
    )
    retry = service.restore(
        first["version_id"],
        reason="fixed restore",
        result_version_id=result_id,
        expected_parent_version_id=first["version_id"],
    )
    assert retry == first_result
    assert service.current()["version_id"] == result_id


def test_recoverable_claim_materialization_completed_retry_returns_same_version(tmp_path):
    service = seeded_version_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="initial")
    result_id = "lsv_" + "e" * 32
    first_result = service.materialize_claim_change(
        reason="claim mutation confirm",
        result_version_id=result_id,
        expected_parent_version_id=first["version_id"],
    )
    retry = service.materialize_claim_change(
        reason="claim mutation confirm",
        result_version_id=result_id,
        expected_parent_version_id=first["version_id"],
    )
    assert retry == first_result
    assert service.current()["version_id"] == result_id
