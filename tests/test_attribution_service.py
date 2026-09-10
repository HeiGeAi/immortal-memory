from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from attribution_service import (
    AttributionService,
    EvidenceCatalogClaimResolver,
)
from evidence_catalog import EvidenceCatalog
from model_types import new_claim
import profile_attribution_audit


def content_hash(content):
    normalized = " ".join(content.split())
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class EvidenceResolver:
    def __init__(self, content, refs, *, error=None):
        self.expected_content = " ".join(content.split())
        self.refs = refs
        self.error = error

    def resolve_for_claim(self, evidence_id, normalized_claim):
        if self.error is not None:
            raise self.error
        if normalized_claim != self.expected_content:
            raise LookupError("unrelated claim")
        ref = dict(self.refs[evidence_id])
        ref["content_hash"] = content_hash(self.expected_content)
        return ref


def available_refs(*pairs):
    return {
        evidence_id: {
            "evidence_id": evidence_id,
            "status": "available",
            "observed_at": observed_at,
        }
        for evidence_id, observed_at in pairs
    }


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
    service = AttributionService(owner_aliases={"owner", "Example Owner", "黑哥"})
    result = service.classify(
        {
            "role": "user",
            "author": "EXAMPLE OWNER",
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
        "张三说这项决定应该重新评估",
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
        "产品经理说，这个需求不该上线",
        "负责人认为这个方案风险太高",
        "主管说，这个版本不能发布",
        "经理提到黑哥需要调整节奏",
        "老板认为黑哥需要调整方向",
        "配偶说，这个决定应该再考虑",
        "妻子说，这个决定应该再考虑",
        "丈夫认为这个安排不合理",
        "哥哥提到黑哥最近太疲惫",
        "姐姐说，应该先休息",
        "弟弟认为这个选择太冒险",
        "妹妹说，这个计划需要修改",
        "法务认为这个条款风险太高",
        "工程师说，这个实现存在竞态",
        "奶奶提到应该注意休息",
        "爷爷评价这个决定太冒险",
        "儿子说，这个安排需要调整",
        "女儿表示这个选择不合适",
        "队友反馈这个流程有问题",
        "合作伙伴指出合同需要修改",
        "供应商说，交付时间需要延后",
        "新角色甲认为这个方案不可行",
        "我的同事A说，黑哥做事太激进",
        "妈妈说，这个决定应该再考虑",
    ],
)
def test_common_chinese_third_party_subjects_are_quoted(content):
    service = AttributionService(owner_aliases={"owner", "黑哥"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
        }
    )

    assert result["speaker"]["kind"] == "other"
    assert result["claim_type"] == "external_view"
    assert result["source_kind"] == "quoted"
    assert result["auto_confirm_allowed"] is False


@pytest.mark.parametrize(
    "content",
    [
        "我说，我认为先做只读审计更稳妥。",
        "我之前说，先做只读审计更稳妥。",
        "我认为先做只读审计更稳妥。",
        "这是我认为最稳妥的处理方式。",
        "黑哥提到自己的长期原则是先验证再发布。",
        "项目说明需要写清楚风险。",
        "这个功能需要说明风险。",
        "黑哥之前说，先验证再发布。",
        "owner 刚才说，先验证再发布。",
        "用户 之前说，先验证再发布。",
        "本人 刚才说，先验证再发布。",
        "我们 曾经说，先验证再发布。",
        "我想说，这个方案需要再验证。",
        "一般来说，这个做法风险更低。",
        "总的来说，这个方向可行。",
        "换句话说，这不是长期偏好。",
        "也就是说，这次只做临时调整。",
        "不得不说，这个结果不错。",
        "我不得不说，这个结果不错。",
        "可以说，这个方案基本成熟。",
        "我可以说，这个方案基本成熟。",
        "应该说，这次判断比较谨慎。",
        "比如说，可以先验证一个样本。",
        "产品说明需要补充风险。",
        "项目经理说明了当前进度。",
        "我的原则说明文档需要更新。",
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


@pytest.mark.parametrize(
    "content",
    [
        "严格来说，我一直偏好先验证再发布。",
        "客观来说，我一直偏好先验证再发布。",
        "准确来说，我一直偏好先验证再发布。",
        "具体来说，我一直偏好先验证再发布。",
        "相对来说，我一直偏好先验证再发布。",
        "整体来说，我一直偏好先验证再发布。",
        "简单来说，我一直偏好先验证再发布。",
        "坦白说，我一直偏好先验证再发布。",
        "老实说，我一直偏好先验证再发布。",
        "实话说，我一直偏好先验证再发布。",
        "只能说，我一直偏好先验证再发布。",
        "这么说，我一直偏好先验证再发布。",
        "谨慎地说，我一直偏好先验证再发布。",
        "保守点说，我一直偏好先验证再发布。",
        "通俗来说，我一直偏好先验证再发布。",
    ],
)
def test_ambiguous_say_markers_preserve_owner_but_block_auto_confirm(content):
    resolver = EvidenceResolver(
        content,
        available_refs(
            ("ev_1", "2026-07-01T00:00:00Z"),
            ("ev_2", "2026-07-02T00:00:00Z"),
            ("ev_3", "2026-07-03T00:00:00Z"),
        ),
    )
    service = AttributionService(
        owner_aliases={"owner", "黑哥"},
        evidence_resolver=resolver,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": "ev_1"},
                {"evidence_id": "ev_2"},
                {"evidence_id": "ev_3"},
            ],
        }
    )

    assert result["speaker"] == {"kind": "owner", "id": "owner"}
    assert result["source_kind"] == "direct"
    assert "third_party_quote" not in result["trust_flags"]
    assert "reported_speech_ambiguous" in result["trust_flags"]
    assert result["auto_confirm_allowed"] is False


@pytest.mark.parametrize(
    "content",
    [
        "Alice说，这个实现存在竞态。",
        "她说，这个安排需要调整。",
        "我的架构师说，这个实现需要重写。",
        "甲方代表说，交付时间需要延后。",
    ],
)
def test_entity_shaped_say_authors_still_fail_closed_as_quoted(content):
    result = AttributionService(owner_aliases={"owner", "黑哥"}).classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
        }
    )

    assert result["speaker"]["kind"] == "other"
    assert result["source_kind"] == "quoted"
    assert "third_party_quote" in result["trust_flags"]
    assert "reported_speech_ambiguous" not in result["trust_flags"]
    assert result["auto_confirm_allowed"] is False


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
    content = "我一直偏好先做只读审计。"
    resolver = EvidenceResolver(
        content,
        available_refs(
            ("ev_1", "2026-07-01T00:00:00Z"),
            ("ev_2", "2026-07-02T00:00:00Z"),
            ("ev_3", "2026-07-03T00:00:00Z"),
        ),
    )
    service = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
    )
    durable = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
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
    content = "我一直偏好先验证再发布。"
    repeated_resolver = EvidenceResolver(
        content,
        available_refs(
            ("ev_1", "2026-07-01T00:00:00Z"),
        ),
    )
    repeated_same_event = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=repeated_resolver,
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
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
    verified_resolver = EvidenceResolver(
        content,
        available_refs(
            ("ev_1", "2026-07-11T00:00:00Z"),
            ("ev_2", "2026-07-12T00:00:00Z"),
            ("ev_3", "2026-07-13T00:00:00Z"),
        ),
    )
    verified = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=verified_resolver,
    ).classify(
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

    assert repeated_same_event["confidence_basis"]["recurrence"] == 0.0
    assert repeated_same_event["auto_confirm_allowed"] is False
    assert verified["confidence_basis"]["recurrence"] == 1.0
    assert verified["auto_confirm_allowed"] is True


def test_recurrence_fails_closed_without_authoritative_resolver():
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "我一直偏好先验证再发布。",
            "source": "codex",
            "recurrence": 1.0,
            "recurrence_count": 999,
            "recurrence_evidence": [
                {
                    "evidence_id": f"does_not_exist_{index}",
                    "observed_at": f"2026-07-0{index}T00:00:00Z",
                }
                for index in range(1, 4)
            ],
        }
    )

    assert result["confidence_basis"]["recurrence"] == 0.0
    assert result["auto_confirm_allowed"] is False
    assert "recurrence_unverified" in result["trust_flags"]


def test_real_catalog_resolver_binds_authoritative_content_and_time(tmp_path):
    content = "我一直偏好先验证再发布。"
    index = tmp_path / "index.jsonl"
    index.write_text(
        "".join(
            json.dumps(
                {
                    "id": f"ev_{number}",
                    "source": "codex-conversation",
                    "timestamp": f"2026-07-0{number}T00:00:00Z",
                    "content": content,
                },
                ensure_ascii=False,
            )
            + "\n"
            for number in range(1, 4)
        ),
        encoding="utf-8",
    )
    resolver = EvidenceCatalogClaimResolver(EvidenceCatalog(index))
    result = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": f"ev_{number}", "observed_at": "1999-01-01T00:00:00Z"}
                for number in range(1, 4)
            ],
        }
    )

    assert result["confidence_basis"]["recurrence"] == 1.0
    assert result["auto_confirm_allowed"] is True


def test_real_catalog_resolver_rejects_unrelated_authoritative_content(tmp_path):
    index = tmp_path / "index.jsonl"
    index.write_text(
        json.dumps(
            {
                "id": "ev_1",
                "source": "codex-conversation",
                "timestamp": "2026-07-01T00:00:00Z",
                "content": "完全无关的原始内容",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    result = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=EvidenceCatalogClaimResolver(EvidenceCatalog(index)),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": "我一直偏好先验证再发布。",
            "source": "codex",
            "recurrence_evidence": [{"evidence_id": "ev_1"}],
        }
    )

    assert result["confidence_basis"]["recurrence"] == 0.0
    assert result["auto_confirm_allowed"] is False
    assert "recurrence_unverified" in result["trust_flags"]


def test_future_authoritative_evidence_is_rejected():
    content = "我一直偏好先验证再发布。"
    resolver = EvidenceResolver(
        content,
        available_refs(
            ("ev_1", "2026-07-21T00:00:00Z"),
            ("ev_2", "2026-07-22T00:00:00Z"),
            ("ev_3", "2026-07-23T00:00:00Z"),
        ),
    )
    result = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": f"ev_{number}"}
                for number in range(1, 4)
            ],
        }
    )

    assert result["confidence_basis"]["recurrence"] == 0.0
    assert result["auto_confirm_allowed"] is False
    assert "recurrence_unverified" in result["trust_flags"]


def test_same_absolute_instant_with_different_timezones_is_one_occurrence():
    content = "我一直偏好先验证再发布。"
    resolver = EvidenceResolver(
        content,
        available_refs(
            ("ev_1", "2026-07-01T08:00:00+08:00"),
            ("ev_2", "2026-07-01T00:00:00Z"),
            ("ev_3", "2026-06-30T20:00:00-04:00"),
        ),
    )
    result = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": f"ev_{number}"}
                for number in range(1, 4)
            ],
        }
    )

    assert result["confidence_basis"]["recurrence"] == 0.0
    assert result["auto_confirm_allowed"] is False


@pytest.mark.parametrize(
    "resolver",
    [
        EvidenceResolver(
            "我一直偏好先验证再发布。",
            {},
            error=RuntimeError("catalog unavailable"),
        ),
        EvidenceResolver(
            "我一直偏好先验证再发布。",
            {
                "ev_1": {
                    "evidence_id": "ev_1",
                    "status": "source_deleted",
                    "observed_at": "2026-07-01T00:00:00Z",
                }
            },
        ),
        EvidenceResolver(
            "我一直偏好先验证再发布。",
            {
                "ev_1": {
                    "evidence_id": "ev_1",
                    "status": "available",
                    "observed_at": "corrupt-time",
                }
            },
        ),
    ],
)
def test_resolver_error_or_unavailable_ref_fails_closed(resolver):
    service = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
    )
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "我一直偏好先验证再发布。",
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": "ev_1", "observed_at": "2099-01-01T00:00:00Z"},
                {"evidence_id": "ev_2", "observed_at": "2099-01-02T00:00:00Z"},
            ],
        }
    )

    assert result["confidence_basis"]["recurrence"] == 0.0
    assert result["auto_confirm_allowed"] is False
    assert "recurrence_unverified" in result["trust_flags"]


def test_duplicate_authoritative_evidence_does_not_create_recurrence():
    content = "我一直偏好先验证再发布。"
    resolver = EvidenceResolver(
        content,
        available_refs(("ev_1", "2026-07-01T00:00:00Z")),
    )
    result = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": "ev_1", "observed_at": "2099-01-01T00:00:00Z"},
                {"evidence_id": "ev_1", "observed_at": "2099-01-02T00:00:00Z"},
                {"evidence_id": "ev_1", "observed_at": "2099-01-03T00:00:00Z"},
            ],
        }
    )

    assert result["confidence_basis"]["recurrence"] == 0.0
    assert result["auto_confirm_allowed"] is False


def test_resolver_must_bind_evidence_to_current_claim():
    resolver = EvidenceResolver(
        "另一条完全无关的偏好。",
        available_refs(
            ("ev_1", "2026-07-01T00:00:00Z"),
            ("ev_2", "2026-07-02T00:00:00Z"),
            ("ev_3", "2026-07-03T00:00:00Z"),
        ),
    )
    result = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": "我一直偏好先验证再发布。",
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": "ev_1", "observed_at": "2099-01-01T00:00:00Z"},
                {"evidence_id": "ev_2", "observed_at": "2099-01-02T00:00:00Z"},
                {"evidence_id": "ev_3", "observed_at": "2099-01-03T00:00:00Z"},
            ],
        }
    )

    assert result["confidence_basis"]["recurrence"] == 0.0
    assert result["auto_confirm_allowed"] is False


@pytest.mark.parametrize(
    "content",
    [
        "我长期反对临时修改生产数据",
        "我长期坚持在焦虑时先列清单",
        "我的原则是每次发布前都先验证",
    ],
)
def test_durable_principles_outrank_incidental_transient_words(content):
    resolver = EvidenceResolver(
        content,
        available_refs(
            ("ev_1", "2026-07-01T00:00:00Z"),
            ("ev_2", "2026-07-02T00:00:00Z"),
            ("ev_3", "2026-07-03T00:00:00Z"),
        ),
    )
    result = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": "ev_1", "observed_at": "2099-01-01T00:00:00Z"},
                {"evidence_id": "ev_2", "observed_at": "2099-01-02T00:00:00Z"},
                {"evidence_id": "ev_3", "observed_at": "2099-01-03T00:00:00Z"},
            ],
        }
    )

    assert result["claim_type"] == "preference"
    assert "transient_content" not in result["trust_flags"]
    assert "one_off_content" not in result["trust_flags"]
    assert result["auto_confirm_allowed"] is True


def test_real_one_off_action_remains_transient():
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "今天先临时修改这一个测试文件",
            "source": "codex",
            "claim_type": "preference",
        }
    )

    assert result["claim_type"] == "request"
    assert "transient_content" in result["trust_flags"]
    assert result["auto_confirm_allowed"] is False


def test_emotion_noun_in_project_description_is_not_current_emotion():
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "焦虑管理模块需要说明风险",
            "source": "codex",
        }
    )

    assert result["speaker"]["kind"] == "owner"
    assert result["claim_type"] == "request"
    assert "third_party_quote" not in result["trust_flags"]


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
        "Authorization: Bea" + "rer abcdefghijklmnopqrstuvwxyz",
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


def test_system_actor_without_role_remains_system():
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "actor": "system",
            "content": "系统健康检查通过。",
            "source": "local",
        }
    )

    assert result["speaker"] == {"kind": "system", "id": "system"}
    assert result["subject"] == {"kind": "system", "id": "system"}
    assert result["source_kind"] == "observed"


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
            "--evidence-index",
            str(tmp_path / "missing-index.jsonl"),
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
    audit_report = json.loads(
        (report_dir / "profile_attribution_audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit_report["evidence_resolver"] == "unavailable"


@pytest.mark.parametrize("failure", ["catalog_limit_exceeded", "unsafe_path"])
def test_profile_audit_reports_probe_failures_unavailable(
    tmp_path,
    monkeypatch,
    failure,
):
    class FailedProbeCatalog:
        def __init__(self, _path):
            pass

        def preflight(self):
            raise RuntimeError(failure)

        def list(self):
            raise AssertionError("unreachable")

    monkeypatch.setattr(
        profile_attribution_audit,
        "EvidenceCatalog",
        FailedProbeCatalog,
    )
    reviewed = tmp_path / "reviewed.jsonl"
    reviewed_md = tmp_path / "reviewed.md"
    distilled = tmp_path / "distilled.jsonl"
    records = tmp_path / "records.jsonl"
    report_dir = tmp_path / "quality"
    trust_report = tmp_path / "latest-report.json"
    for path in (reviewed, reviewed_md, distilled, records):
        path.write_text("", encoding="utf-8")

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
            "--evidence-index",
            str(tmp_path / "index.jsonl"),
            "--report-dir",
            str(report_dir),
            "--trust-report",
            str(trust_report),
        ]
    )
    report = json.loads(
        (report_dir / "profile_attribution_audit.json").read_text(
            encoding="utf-8"
        )
    )

    assert code == 0
    assert report["evidence_resolver"] == "unavailable"


def test_large_verified_catalog_is_not_rejected_by_listing_limit(
    tmp_path,
    monkeypatch,
):
    content = "我一直偏好先验证再发布。"

    class LargeVerifiedCatalog:
        def __init__(self, _path):
            pass

        def preflight(self):
            return {
                "source_state": "current",
                "source_size": 2 * 1024 * 1024 * 1024,
            }

        def list(self):
            raise AssertionError("production wiring must not list the catalog")

        def resolve(self, evidence_id):
            number = int(evidence_id.rsplit("_", 1)[1])
            return {
                "evidence_id": evidence_id,
                "status": "available",
                "observed_at": f"2026-07-0{number}T00:00:00Z",
                "content_hash": content_hash(content),
            }

    monkeypatch.setattr(
        profile_attribution_audit,
        "EvidenceCatalog",
        LargeVerifiedCatalog,
    )
    resolver = profile_attribution_audit.build_evidence_resolver(
        tmp_path / "large-index.jsonl"
    )
    result = AttributionService(
        owner_aliases={"owner"},
        evidence_resolver=resolver,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    ).classify(
        {
            "role": "user",
            "author": "owner",
            "content": content,
            "source": "codex",
            "recurrence_evidence": [
                {"evidence_id": f"ev_{number}"}
                for number in range(1, 4)
            ],
        }
    )

    assert resolver is not None
    assert resolver.health == "available"
    assert result["auto_confirm_allowed"] is True


def test_profile_audit_injects_real_catalog_resolver(tmp_path):
    content = "我一直偏好先验证再发布。"
    reviewed = tmp_path / "reviewed.jsonl"
    reviewed_md = tmp_path / "reviewed.md"
    distilled = tmp_path / "distilled.jsonl"
    records = tmp_path / "records.jsonl"
    evidence_index = tmp_path / "index.jsonl"
    report_dir = tmp_path / "quality"
    trust_report = tmp_path / "model" / "attribution" / "latest-report.json"
    reviewed.write_text(
        json.dumps(
            {
                "memory_id": "mem-1",
                "statement": content,
                "focus": "self_profile",
                "recurrence_evidence": [
                    {"evidence_id": f"ev_{number}", "observed_at": "2099-01-01T00:00:00Z"}
                    for number in range(1, 4)
                ],
                "source": {
                    "clean_id": "clean-1",
                    "source": "feishu-im",
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
                "text": content,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    evidence_index.write_text(
        "".join(
            json.dumps(
                {
                    "id": f"ev_{number}",
                    "source": "feishu-im",
                    "timestamp": f"2026-07-0{number}T00:00:00Z",
                    "content": content,
                },
                ensure_ascii=False,
            )
            + "\n"
            for number in range(1, 4)
        ),
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
            "--evidence-index",
            str(evidence_index),
            "--report-dir",
            str(report_dir),
            "--trust-report",
            str(trust_report),
        ]
    )
    report = json.loads(trust_report.read_text(encoding="utf-8"))
    audit_report = json.loads(
        (report_dir / "profile_attribution_audit.json").read_text(
            encoding="utf-8"
        )
    )

    assert code in {0, 2}
    assert report["counts"]["auto_confirm"] == {"allowed": 1}
    assert audit_report["evidence_resolver"] == "available"


def test_profile_audit_marks_malformed_catalog_unavailable_and_blocks(tmp_path):
    content = "我一直偏好先验证再发布。"
    reviewed = tmp_path / "reviewed.jsonl"
    reviewed_md = tmp_path / "reviewed.md"
    distilled = tmp_path / "distilled.jsonl"
    records = tmp_path / "records.jsonl"
    evidence_index = tmp_path / "index.jsonl"
    report_dir = tmp_path / "quality"
    trust_report = tmp_path / "model" / "attribution" / "latest-report.json"
    reviewed.write_text(
        json.dumps(
            {
                "memory_id": "mem-1",
                "statement": content,
                "focus": "self_profile",
                "recurrence_evidence": [
                    {"evidence_id": f"ev_{number}"}
                    for number in range(1, 4)
                ],
                "source": {
                    "clean_id": "clean-1",
                    "source": "feishu-im",
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
                "text": content,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    evidence_index.write_text("{not-json}\n", encoding="utf-8")

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
            "--evidence-index",
            str(evidence_index),
            "--report-dir",
            str(report_dir),
            "--trust-report",
            str(trust_report),
        ]
    )
    report = json.loads(trust_report.read_text(encoding="utf-8"))
    audit_report = json.loads(
        (report_dir / "profile_attribution_audit.json").read_text(
            encoding="utf-8"
        )
    )

    assert code in {0, 2}
    assert audit_report["evidence_resolver"] == "unavailable"
    assert report["counts"]["auto_confirm"] == {"blocked": 1}
    assert report["counts"]["trust_flags"]["recurrence_unverified"] == 1
