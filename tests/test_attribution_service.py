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
