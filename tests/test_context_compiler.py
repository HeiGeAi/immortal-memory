import json
from datetime import datetime, timedelta, timezone

import pytest

from context_store import ContextStore
from model_types import (
    CONTEXT_ITEM_FIELDS,
    CONTEXT_SECTIONS,
    new_claim,
    new_judgment_card,
    new_living_self_version,
    new_self_model_item,
    validate_claim,
    validate_judgment_card,
)


NOW = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
ACTOR = {"kind": "owner", "id": "owner"}


class FakeClaims:
    def __init__(self, rows):
        self.rows = list(rows)
        self.events = FakeEvents(
            max((row["based_on_event_seq"] for row in self.rows), default=0)
        )

    def list(self):
        return [dict(row) for row in self.rows]


class FakeJudgments:
    def __init__(self, rows):
        self.rows = list(rows)
        self.events = FakeEvents(len(self.rows))

    def list(self):
        return [dict(row) for row in self.rows]


class FakeEvents:
    def __init__(self, value):
        self.value = value

    def watermark(self):
        return self.value


class FakeLivingSelf:
    def __init__(self, current):
        self.value = current

    def current(self):
        return json.loads(json.dumps(self.value, ensure_ascii=False))


class FakeEvidence:
    def __init__(self, *, ready=True):
        self.ready = ready
        self.requested = []

    def preflight(self):
        return {
            "mode": "verified_sqlite" if self.ready else "jsonl_stream",
            "source_state": "current",
            "bounded_scan": True,
            "indexed_id_count": 100,
        }

    def resolve(self, item):
        assert len(self.requested) < 200
        self.requested.append(item)
        return {
            "evidence_id": item,
            "source": "local",
            "raw_id": item,
            "content_hash": "sha256:" + "a" * 64,
            "status": "available",
            "privacy": "restricted",
            "observed_at": NOW.isoformat(),
        }

    def resolve_many(self, ids):
        raise AssertionError("compiler must use verified SQLite locators by ID")

    def list(self):
        raise AssertionError("compiler must not enumerate the raw evidence index")


def claim(
    claim_id,
    statement,
    *,
    privacy="restricted",
    status="confirmed",
    source_kind="observed",
    role_scope=("work",),
    domain_scope=("technical",),
    custom_scope_ids=(),
    evidence_ids=None,
    valid_from=None,
    valid_to=None,
    updated_at=None,
    event_seq=1,
):
    evidence = ["ev_" + claim_id] if evidence_ids is None else list(evidence_ids)
    value = new_claim(
        statement=statement,
        source_kind=source_kind,
        evidence_ids=evidence,
        privacy=privacy,
        role_scope=list(role_scope),
        domain_scope=list(domain_scope),
        custom_scope_ids=list(custom_scope_ids),
        now=(updated_at or NOW.isoformat()),
    )
    value.update(
        {
            "claim_id": claim_id,
            "status": status,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "updated_at": updated_at or NOW.isoformat(),
            "based_on_event_seq": event_seq,
        }
    )
    validate_claim(value)
    return value


def living_self(*items):
    sections = {
        "identity_commitments": [],
        "values": [],
        "expression_dna": [],
        "mental_models": [],
        "decision_heuristics": [],
        "anti_patterns": [],
        "tensions": [],
        "honest_boundaries": [],
    }
    for section, item in items:
        sections[section].append(item)
    return new_living_self_version(
        status="confirmed",
        generation_reason="claim_change",
        based_on_claim_seq=10,
        sections=sections,
        now=NOW.isoformat(),
    )


def self_item(item_id, kind="value", summary="重视事实核验"):
    value = new_self_model_item(
        kind=kind,
        title=summary,
        summary=summary,
        evidence_ids=["ev_self"],
        claim_ids=["clm_self"],
        role_scope=["work"],
        domain_scope=["technical"],
        status="confirmed",
        now=NOW.isoformat(),
    )
    value["item_id"] = item_id
    return value


def judgment(card_id, *, outcome="unknown", privacy="restricted"):
    value = new_judgment_card(
        title="技术方案评审",
        situation="客户技术方案需要评审",
        decision="先核验证据再给结论",
        evidence_ids=["ev_judgment"],
        claim_ids=["clm_fact"],
        privacy=privacy,
        now=NOW.isoformat(),
    )
    value["card_id"] = card_id
    value["status"] = "confirmed"
    if outcome != "unknown":
        value["outcome"] = {
            "status": outcome,
            "summary": "该方法减少了返工",
            "observed_at": NOW.isoformat(),
        }
    validate_judgment_card(value)
    return value


def compiler(
    tmp_path,
    *,
    claims=(),
    self_model=None,
    judgments=(),
    evidence=None,
):
    from context_compiler import ContextCompiler

    return ContextCompiler(
        tmp_path,
        claims=FakeClaims(claims),
        living_self=FakeLivingSelf(self_model or living_self()),
        judgments=FakeJudgments(judgments),
        evidence=evidence or FakeEvidence(),
        context_store=ContextStore(tmp_path, clock=lambda: NOW),
        clock=lambda: NOW,
    )


def preview(instance, **kwargs):
    return instance.preview(
        "评审客户技术方案",
        mode="reviewer",
        role_scope=["work"],
        domain_scope=["technical"],
        request_id="req_preview",
        idempotency_key="idem_preview",
        actor=ACTOR,
        reason="context preview requested",
        **kwargs,
    )


def test_index_integrity_must_be_proven_before_derived_stores_are_read(tmp_path):
    from context_compiler import ContextCompilerError

    claims = FakeClaims([claim("clm_fact", "客户要求技术方案可回滚")])
    instance = compiler(
        tmp_path,
        claims=claims.rows,
        evidence=FakeEvidence(ready=False),
    )

    with pytest.raises(ContextCompilerError) as failure:
        preview(instance)
    assert failure.value.code == "index_unavailable"


def test_preview_excludes_private_wrong_scope_unconfirmed_expired_and_future(
    tmp_path,
):
    rows = [
        claim("clm_ok", "客户要求技术方案可回滚"),
        claim("clm_private", "家庭私密", privacy="private"),
        claim(
            "clm_scope",
            "内容账号风格",
            role_scope=("creator",),
            domain_scope=("content",),
        ),
        claim("clm_candidate", "尚未确认", status="candidate"),
        claim(
            "clm_expired",
            "已经过期",
            valid_to=(NOW - timedelta(seconds=1)).isoformat(),
        ),
        claim(
            "clm_future",
            "来自未来",
            valid_from=(NOW + timedelta(seconds=1)).isoformat(),
            updated_at=(NOW + timedelta(seconds=1)).isoformat(),
        ),
    ]

    result = preview(compiler(tmp_path, claims=rows))
    encoded = json.dumps(result, ensure_ascii=False)

    assert "客户要求技术方案可回滚" in encoded
    for forbidden in ("家庭私密", "内容账号风格", "尚未确认", "已经过期", "来自未来"):
        assert forbidden not in encoded
    assert result["privacy_policy"]["excluded_count"] == 5
    assert set(result["privacy_policy"]["reasons"]) == {
        "private",
        "scope_mismatch",
        "unconfirmed",
        "expired",
        "future",
    }


def test_preview_uses_closed_context_items_and_does_not_invent_unknowns(
    tmp_path,
):
    model = living_self(
        ("values", self_item("self_value")),
        (
            "expression_dna",
            self_item(
                "self_expression",
                kind="expression_dna",
                summary="表达应短句直接",
            ),
        ),
    )
    result = preview(
        compiler(
            tmp_path,
            claims=[claim("clm_fact", "客户要求技术方案可回滚")],
            self_model=model,
        )
    )

    assert result["sections"]["verified_facts"]
    assert result["sections"]["confirmed_self_models"][0]["id"] == "self_value"
    assert result["sections"]["inferences"] == []
    assert result["sections"]["unknowns"] == []
    assert "self_expression" not in json.dumps(result, ensure_ascii=False)
    for items in result["sections"].values():
        for item in items:
            assert set(item) == CONTEXT_ITEM_FIELDS


def test_relevant_judgment_with_outcome_ranks_before_unknown_outcome(tmp_path):
    result = preview(
        compiler(
            tmp_path,
            claims=[claim("clm_fact", "客户技术方案需要可回滚")],
            judgments=[
                judgment("jdg_unknown"),
                judgment("jdg_positive", outcome="positive"),
            ],
        )
    )

    assert [
        item["id"] for item in result["sections"]["judgment_cards"]
    ] == ["jdg_positive", "jdg_unknown"]
    assert "减少了返工" in result["sections"]["judgment_cards"][0]["summary"]


def test_custom_scope_requires_exact_stable_scope_id(tmp_path):
    rows = [
        claim(
            "clm_custom_a",
            "项目 A 的技术约束",
            role_scope=("custom",),
            domain_scope=("custom",),
            custom_scope_ids=("project_a",),
        ),
        claim(
            "clm_custom_b",
            "项目 B 的秘密",
            role_scope=("custom",),
            domain_scope=("custom",),
            custom_scope_ids=("project_b",),
        ),
    ]
    instance = compiler(tmp_path, claims=rows)
    result = instance.preview(
        "评审项目 A",
        mode="custom",
        role_scope=["custom"],
        domain_scope=["custom"],
        custom_scope_ids=["project_a"],
        request_id="req_custom",
        idempotency_key="idem_custom",
        actor=ACTOR,
        reason="custom preview",
    )

    encoded = json.dumps(result, ensure_ascii=False)
    assert "项目 A 的技术约束" in encoded
    assert "项目 B 的秘密" not in encoded


def test_preview_is_deterministic_bounded_and_persisted_by_context_store(
    tmp_path,
):
    rows = [
        claim(
            "clm_%03d" % index,
            "技术评审事实 %03d " % index + "证据充分" * 20,
            event_seq=index + 1,
        )
        for index in range(40)
    ]
    instance = compiler(tmp_path, claims=rows)
    result = preview(instance, max_chars=4000, max_bytes=12000)

    assert result["budget"]["used_chars"] <= 4000
    assert result["budget"]["used_bytes"] <= 12000
    assert all(len(items) <= 20 for items in result["sections"].values())
    assert set(result["sections"]) == CONTEXT_SECTIONS
    stored = instance.context_store.get(result["preview_id"])
    body = json.loads(
        (
            instance.context_store.previews_dir
            / (result["preview_id"] + ".json")
        ).read_text(encoding="utf-8")
    )
    assert stored["preview_hash"] == result["preview_hash"]
    assert body["sections"] == result["sections"]


def test_source_revision_and_evidence_resolution_are_exact_and_bounded(tmp_path):
    evidence = FakeEvidence()
    model = living_self(("values", self_item("self_value")))
    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚", event_seq=17)],
        self_model=model,
        judgments=[judgment("jdg_positive", outcome="positive")],
        evidence=evidence,
    )

    result = preview(instance)

    assert result["source_revision"] == {
        "claims_event_seq": 17,
        "living_self_version": model["version_id"],
        "judgments_event_seq": 1,
        "compiler_version": "1.1.0",
        "policy_version": 1,
    }
    assert set(evidence.requested) == {
        "ev_clm_fact",
        "ev_self",
        "ev_judgment",
    }


def test_preview_redacts_secret_shaped_text_and_keeps_auto_as_preview(tmp_path):
    instance = compiler(
        tmp_path,
        claims=[
            claim(
                "clm_secret",
                "联系 owner@example.com 或 13812345678，Bearer abc.def.ghi，"
                "api_key: abcdefghijklmnop，路径 /Users/alice/key",
                role_scope=("general",),
                domain_scope=("general",),
            )
        ],
    )

    result = instance.preview(
        "提供建议",
        mode="auto",
        request_id="req_auto",
        idempotency_key="idem_auto",
        actor=ACTOR,
        reason="auto preview",
    )
    encoded = json.dumps(result, ensure_ascii=False)

    assert result["mode"] == "auto"
    assert result["lifecycle_status"] == "preview"
    assert "[REDACTED]" in encoded
    assert "owner@example.com" not in encoded
    assert "abc.def.ghi" not in encoded
    assert "13812345678" not in encoded
    assert "abcdefghijklmnop" not in encoded
    assert "/Users/alice" not in encoded


def test_authority_change_during_preview_fails_closed(tmp_path):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    values = iter((1, 2))
    instance.claims.events.watermark = lambda: next(values)

    with pytest.raises(ContextCompilerError) as failure:
        preview(instance)
    assert failure.value.code == "source_changed"
