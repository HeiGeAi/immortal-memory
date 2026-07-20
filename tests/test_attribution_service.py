from __future__ import annotations

import json

import pytest

from attribution_service import AttributionService
from model_types import new_claim
import profile_attribution_audit


def test_speaker_and_subject_cover_owner_other_system_and_unknown():
    service = AttributionService(owner_aliases={"owner", "黑哥"})

    owner = service.classify(
        {
            "role": "user",
            "author": " 黑哥 ",
            "content": "我一直偏好先给结论。",
            "source": "codex",
            "recurrence_count": 3,
        }
    )
    other = service.classify(
        {
            "role": "user",
            "author": "同事A",
            "content": "你做事太激进了。",
            "source": "feishu-im",
        }
    )
    system = service.classify(
        {
            "role": "system",
            "content": "自动生成的状态观察。",
            "source": "local",
        }
    )
    unknown = service.classify(
        {
            "role": "mystery",
            "content": "来源不明。",
            "source": "unknown-provider",
        }
    )

    assert owner["speaker"] == {"kind": "owner", "id": "owner"}
    assert owner["subject"] == {"kind": "owner", "id": "owner"}
    assert other["speaker"]["kind"] == "other"
    assert other["subject"] == {"kind": "owner", "id": "owner"}
    assert system["speaker"] == {"kind": "system", "id": "system"}
    assert unknown["speaker"] == {"kind": "unknown", "id": "unknown"}


def test_other_speaker_cannot_become_owner_direct_fact():
    service = AttributionService(owner_aliases={"owner", "小黑" + "子"})
    result = service.classify(
        {
            "role": "assistant",
            "author": "同事A",
            "content": "你做事太激进了",
            "source": "feishu",
        }
    )

    assert result["speaker"]["kind"] == "other"
    assert result["subject"]["kind"] == "owner"
    assert result["claim_type"] == "external_view"
    assert result["source_kind"] == "quoted"
    assert result["auto_confirm_allowed"] is False
    assert "other_speaker" in result["trust_flags"]

    claim = new_claim(
        statement="同事认为用户做事太激进",
        source_kind=result["source_kind"],
        evidence_ids=["ev_1"],
        claim_type=result["claim_type"],
        speaker_kind=result["speaker"]["kind"],
        speaker_id=result["speaker"]["id"],
        subject_kind=result["subject"]["kind"],
        subject_id=result["subject"]["id"],
        confidence=result["confidence"],
        confidence_basis=result["confidence_basis"],
        role_scope=result["role_scope"],
        domain_scope=result["domain_scope"],
        privacy=result["privacy"],
        now="2026-07-20T00:00:00+00:00",
    )
    assert claim["claim_type"] == "external_view"


@pytest.mark.parametrize(
    ("content", "expected_type"),
    [
        ("这一次先写详细一点", "request"),
        ("今天我有点焦虑", "emotion"),
    ],
)
def test_transient_owner_content_never_upgrades_to_preference(
    content, expected_type
):
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_count": 1,
        }
    )

    assert result["claim_type"] == expected_type
    assert result["confidence"] < 0.7
    assert result["auto_confirm_allowed"] is False
    assert "transient_content" in result["trust_flags"]


def test_real_owner_alias_does_not_override_quoted_third_party_content():
    service = AttributionService(owner_aliases={"owner", "Blake Xu", "黑哥"})
    result = service.classify(
        {
            "role": "user",
            "author": "BLAKE XU",
            "content": "同事A说：我认为黑哥应该放弃这个项目。",
            "source": "codex",
        }
    )

    assert result["speaker"]["kind"] == "other"
    assert result["subject"]["kind"] == "owner"
    assert result["source_kind"] == "quoted"
    assert result["claim_type"] == "external_view"
    assert result["auto_confirm_allowed"] is False
    assert {"other_speaker", "third_party_quote"}.issubset(
        result["trust_flags"]
    )


@pytest.mark.parametrize(
    "content",
    [
        "同事A说，我认为你做事太激进了",
        "同事A说“黑哥应该放弃这个项目”",
        "同事A说 '黑哥应该放弃这个项目'",
        "同事A表示，黑哥应该放弃这个项目",
        "同事A认为黑哥应该放弃这个项目",
        "同事A提到\n黑哥应该放弃这个项目",
        "同事A说，同事B认为：黑哥应该放弃这个项目",
        ("前置信息" * 80) + "\n同事A说：黑哥应该放弃这个项目",
    ],
)
def test_common_third_party_restatements_fail_closed_as_quoted(content):
    service = AttributionService(owner_aliases={"owner", "黑哥"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": "ev_1", "observed_at": "2026-07-01T00:00:00Z"},
                {"evidence_id": "ev_2", "observed_at": "2026-07-02T00:00:00Z"},
                {"evidence_id": "ev_3", "observed_at": "2026-07-03T00:00:00Z"},
            ],
        }
    )

    assert result["speaker"]["kind"] == "other"
    assert result["claim_type"] == "external_view"
    assert result["source_kind"] == "quoted"
    assert result["auto_confirm_allowed"] is False
    assert "third_party_quote" in result["trust_flags"]


@pytest.mark.parametrize(
    "content",
    [
        "我说，我认为先做只读审计更稳妥。",
        "我之前说，先做只读审计更稳妥。",
        "我认为先做只读审计更稳妥。",
        "这是我认为最稳妥的处理方式。",
        "黑哥提到自己的长期原则是先验证再发布。",
    ],
)
def test_owner_first_person_expressions_are_not_false_positive_quotes(content):
    service = AttributionService(owner_aliases={"owner", "黑哥"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
        }
    )

    assert result["speaker"] == {"kind": "owner", "id": "owner"}
    assert result["source_kind"] == "direct"
    assert "third_party_quote" not in result["trust_flags"]


def test_explicit_quoted_author_always_fails_closed():
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "quoted_author": "外部顾问",
            "content": "建议保留原方案。",
            "source": "codex",
        }
    )

    assert result["speaker"]["kind"] == "other"
    assert result["claim_type"] == "external_view"
    assert result["auto_confirm_allowed"] is False


def test_confidence_policy_is_recomputable_and_auto_confirm_is_strict():
    service = AttributionService(owner_aliases={"owner"})
    durable = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "我一直偏好先做只读审计。",
            "source": "codex",
            "recurrence_count": 3,
            "role_scope": ["work"],
            "domain_scope": ["technical", "risk"],
            "privacy": "context_safe",
            "recurrence_evidence": [
                {"evidence_id": "ev_1", "observed_at": "2026-07-01T00:00:00Z"},
                {"evidence_id": "ev_2", "observed_at": "2026-07-02T00:00:00Z"},
                {"evidence_id": "ev_3", "observed_at": "2026-07-03T00:00:00Z"},
            ],
        }
    )
    one_off = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "我偏好先做只读审计。",
            "source": "codex",
            "recurrence_count": 1,
        }
    )

    basis = durable["confidence_basis"]
    recomputed = (
        basis["speaker"] * 0.4
        + basis["recurrence"] * 0.3
        + basis["source_quality"] * 0.3
    )
    assert basis["policy_version"] == 1
    assert durable["confidence"] == pytest.approx(recomputed)
    assert durable["confidence"] >= 0.85
    assert durable["auto_confirm_allowed"] is True
    assert durable["role_scope"] == ["work"]
    assert durable["domain_scope"] == ["technical", "risk"]
    assert durable["privacy"] == "context_safe"
    assert one_off["auto_confirm_allowed"] is False


def test_transient_semantics_override_explicit_claim_type_and_raw_recurrence():
    service = AttributionService(owner_aliases={"owner"})
    emotion = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "今天我有点焦虑",
            "source": "codex",
            "claim_type": "preference",
            "recurrence": 1.0,
            "recurrence_count": 999,
        }
    )
    temporary_preference = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "今天我喜欢写详细一点",
            "source": "codex",
            "claim_type": "preference",
            "recurrence_count": 3,
        }
    )

    assert emotion["claim_type"] == "emotion"
    assert temporary_preference["claim_type"] == "request"
    assert emotion["confidence_basis"]["recurrence"] == 0.0
    assert temporary_preference["confidence_basis"]["recurrence"] == 0.0
    assert emotion["auto_confirm_allowed"] is False
    assert temporary_preference["auto_confirm_allowed"] is False
    assert "one_off_content" in emotion["trust_flags"]
    assert "one_off_content" in temporary_preference["trust_flags"]


def test_only_distinct_verifiable_evidence_and_times_create_recurrence():
    service = AttributionService(owner_aliases={"owner"})
    repeated_same_event = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "我一直偏好先验证再发布。",
            "source": "codex",
            "recurrence": 1.0,
            "recurrence_count": 99,
            "recurrence_evidence": [
                {"evidence_id": "ev_1", "observed_at": "2026-07-01T00:00:00Z"},
                {"evidence_id": "ev_1", "observed_at": "2026-07-02T00:00:00Z"},
                {"evidence_id": "ev_2", "observed_at": "not-a-time"},
            ],
        }
    )
    verified = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "我一直偏好先验证再发布。",
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": "ev_1", "observed_at": "2026-07-01T00:00:00Z"},
                {"evidence_id": "ev_2", "observed_at": "2026-07-02T00:00:00Z"},
                {"evidence_id": "ev_3", "observed_at": "2026-07-03T00:00:00Z"},
            ],
        }
    )

    assert repeated_same_event["confidence_basis"]["recurrence"] == 0.0
    assert repeated_same_event["auto_confirm_allowed"] is False
    assert verified["confidence_basis"]["recurrence"] == 1.0
    assert verified["auto_confirm_allowed"] is True


def test_scope_and_privacy_inference_are_bounded_enums():
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "客户项目的 API 密钥需要立刻轮换。",
            "source": "feishu-im",
        }
    )

    assert set(result["role_scope"]) <= {
        "general",
        "personal",
        "work",
        "creator",
        "family",
        "custom",
    }
    assert set(result["domain_scope"]) <= {
        "general",
        "business",
        "content",
        "technical",
        "relationship",
        "project",
        "risk",
        "custom",
    }
    assert result["privacy"] == "private"
    assert result["auto_confirm_allowed"] is False


@pytest.mark.parametrize(
    "content",
    [
        "客户合同报价需要长期保存",
        "身份证信息需要长期保存",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_explicit_public_cannot_lower_content_privacy(content):
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "privacy": "public",
        }
    )

    assert result["privacy"] in {"restricted", "private"}
    assert result["auto_confirm_allowed"] is False


def test_custom_scopes_are_claim_compatible_or_safely_downgraded():
    service = AttributionService(owner_aliases={"owner"})
    stable = service.classify(
        {
            "role": "assistant",
            "author": "同事A",
            "content": "项目内使用严格审查。",
            "source": "feishu",
            "role_scope": ["custom"],
            "domain_scope": ["custom"],
            "custom_scope_ids": ["role_immortal", "domain_living_self"],
        }
    )
    missing = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "项目内使用严格审查。",
            "source": "codex",
            "role_scope": ["custom"],
            "domain_scope": ["custom"],
        }
    )

    claim = new_claim(
        statement="同事建议项目内使用严格审查。",
        source_kind=stable["source_kind"],
        evidence_ids=["ev_1"],
        claim_type=stable["claim_type"],
        speaker_kind=stable["speaker"]["kind"],
        speaker_id=stable["speaker"]["id"],
        subject_kind=stable["subject"]["kind"],
        subject_id=stable["subject"]["id"],
        confidence=stable["confidence"],
        confidence_basis=stable["confidence_basis"],
        role_scope=stable["role_scope"],
        domain_scope=stable["domain_scope"],
        custom_scope_ids=stable["custom_scope_ids"],
        privacy=stable["privacy"],
        now="2026-07-20T00:00:00+00:00",
    )

    assert claim["custom_scope_ids"] == [
        "role_immortal",
        "domain_living_self",
    ]
    assert missing["role_scope"] == ["general"]
    assert missing["domain_scope"] == ["general"]
    assert missing["custom_scope_ids"] == []
    assert "custom_scope_missing_id" in missing["trust_flags"]


def test_system_role_with_author_remains_system():
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "system",
            "author": "system",
            "content": "自动生成的状态观察。",
            "source": "local",
        }
    )

    assert result["speaker"] == {"kind": "system", "id": "system"}
    assert result["subject"] == {"kind": "system", "id": "system"}
    assert result["source_kind"] == "observed"
    assert result["claim_type"] != "external_view"


def test_latest_report_is_bounded_and_contains_no_raw_body():
    service = AttributionService(owner_aliases={"owner"}, max_report_samples=3)
    secret = "Authorization: Bea" + "rer abcdefghijklmnopqrstuvwxyz"
    records = [
        {
            "record_id": f"raw-{index}",
            "role": "user",
            "author": "owner" if index % 2 == 0 else "同事A",
            "content": f"{secret} 私密正文 {index}",
            "source": "codex",
        }
        for index in range(20)
    ]

    report = service.build_report(
        records,
        generated_at="2026-07-20T00:00:00+00:00",
    )
    encoded = json.dumps(report, ensure_ascii=False)

    assert report["total"] == 20
    assert len(report["samples"]) == 3
    assert secret not in encoded
    assert "私密正文" not in encoded
    assert "同事A" not in encoded
    assert all(len(item["summary"]) <= 200 for item in report["samples"])


def test_profile_audit_writes_safe_latest_report_without_raw_examples(tmp_path):
    secret = "Authorization: Bea" + "rer abcdefghijklmnopqrstuvwxyz"
    reviewed = tmp_path / "reviewed.jsonl"
    reviewed_md = tmp_path / "reviewed.md"
    distilled = tmp_path / "distilled.jsonl"
    records = tmp_path / "records.jsonl"
    report_dir = tmp_path / "quality"
    trust_report = tmp_path / "model" / "attribution" / "latest-report.json"
    reviewed.write_text(
        json.dumps(
            {
                "memory_id": "mem-1",
                "statement": secret + " 私密原文",
                "focus": "self_profile",
                "source": {
                    "clean_id": "clean-1",
                    "source": "feishu-im",
                    "title": "私密标题",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    reviewed_md.write_text("", encoding="utf-8")
    distilled.write_text("", encoding="utf-8")
    records.write_text(
        json.dumps(
            {
                "clean_id": "clean-1",
                "actor": "owner",
                "source": "feishu-im",
                "text": secret + " 私密原文",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    code = profile_attribution_audit.main(
        [
            "--reviewed",
            str(reviewed),
            "--reviewed-md",
            str(reviewed_md),
            "--distilled",
            str(distilled),
            "--records",
            str(records),
            "--report-dir",
            str(report_dir),
            "--trust-report",
            str(trust_report),
        ]
    )
    encoded = "\n".join(
        [
            trust_report.read_text(encoding="utf-8"),
            (report_dir / "profile_attribution_audit.json").read_text(
                encoding="utf-8"
            ),
            (report_dir / "profile_attribution_audit.md").read_text(
                encoding="utf-8"
            ),
        ]
    )

    assert code in {0, 2}
    assert secret not in encoded
    assert "私密原文" not in encoded
    assert trust_report.stat().st_size < 64_000
