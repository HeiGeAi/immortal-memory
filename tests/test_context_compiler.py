import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    validate_context_pack,
    validate_judgment_card,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)
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


class MutableClock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


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
    clock=None,
):
    from context_compiler import ContextCompiler

    trusted_clock = clock or (lambda: NOW)
    return ContextCompiler(
        tmp_path,
        claims=FakeClaims(claims),
        living_self=FakeLivingSelf(self_model or living_self()),
        judgments=FakeJudgments(judgments),
        evidence=evidence or FakeEvidence(),
        context_store=ContextStore(tmp_path, clock=trusted_clock),
        clock=trusted_clock,
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
    private_home = "/" + "Users/alice/key"
    instance = compiler(
        tmp_path,
        claims=[
            claim(
                "clm_secret",
                "联系 owner@example.com 或 13812345678，Bearer abc.def.ghi，"
                "api_key: abcdefghijklmnop，路径 " + private_home,
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
    assert private_home.rsplit("/", 1)[0] not in encoded


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


def test_task_and_reason_are_redacted_in_response_cache_and_events(tmp_path):
    instance = compiler(
        tmp_path,
        claims=[
            claim(
                "clm_fact",
                "客户技术方案需要可回滚",
            )
        ],
    )
    task_secret = "sk-" + "a" * 24
    reason_secret = "ghp_" + "b" * 24

    result = instance.preview(
        "评审客户技术方案，临时凭证 " + task_secret,
        mode="reviewer",
        role_scope=["work"],
        domain_scope=["technical"],
        request_id="req_redacted_metadata",
        idempotency_key="idem_redacted_metadata",
        actor=ACTOR,
        reason="预览原因 " + reason_secret,
    )
    cache_text = (
        instance.context_store.previews_dir
        / (result["preview_id"] + ".json")
    ).read_text(encoding="utf-8")
    event_text = instance.context_store.events.path.read_text(encoding="utf-8")

    assert task_secret not in json.dumps(result, ensure_ascii=False)
    assert reason_secret not in json.dumps(result, ensure_ascii=False)
    assert task_secret not in cache_text
    assert reason_secret not in cache_text
    assert task_secret not in event_text
    assert reason_secret not in event_text


def compile_preview(instance, preview_result, *, suffix="one", **kwargs):
    return instance.compile(
        preview_id=preview_result["preview_id"],
        preview_hash=preview_result["preview_hash"],
        excluded_item_ids=[],
        request_id="req_compile_" + suffix,
        idempotency_key="idem_compile_" + suffix,
        actor=ACTOR,
        reason="preview approved",
        **kwargs,
    )


def test_compile_rejects_stale_source_before_and_during_authorization(tmp_path):
    from context_compiler import ContextCompilerError

    before = compiler(
        tmp_path / "before",
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    preview_before = preview(before)
    before.claims.rows.append(
        claim("clm_new", "新增约束", event_seq=2)
    )
    before.claims.events.value = 2

    with pytest.raises(ContextCompilerError) as stale:
        compile_preview(before, preview_before)
    assert stale.value.code == "stale_preview"
    assert before.context_store.get(preview_before["preview_id"])[
        "lifecycle_status"
    ] == "preview"

    during = compiler(
        tmp_path / "during",
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    preview_during = preview(during)

    def mutate_authority():
        during.claims.rows.append(
            claim("clm_race", "竞态新增约束", event_seq=2)
        )
        during.claims.events.value = 2

    during._compile_authorization_hook = mutate_authority
    with pytest.raises(ContextCompilerError) as raced:
        compile_preview(during, preview_during)
    assert raced.value.code == "stale_preview"
    packs = during.context_store.root / "packs"
    assert not packs.exists() or not list(packs.glob("ctx_*/context.json"))


def test_compiled_context_becomes_unusable_when_authority_changes(tmp_path):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    compiled = compile_preview(instance, preview(instance))
    instance.claims.rows.append(
        claim("clm_after", "编译后新增约束", event_seq=2)
    )
    instance.claims.events.value = 2

    with pytest.raises(ContextCompilerError) as failure:
        instance.load_compiled(compiled["context_id"])
    assert failure.value.code == "stale_context"


def test_load_compiled_returns_safe_read_body_when_path_is_replaced_after_read(
    tmp_path, monkeypatch
):
    import context_compiler as context_compiler_module

    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    compiled = compile_preview(instance, preview(instance))
    markdown_path = Path(compiled["context_md"])
    verified_markdown = markdown_path.read_text(encoding="utf-8")
    attacker_path = tmp_path / "attacker.md"
    attacker_path.write_text("# attacker replacement\n", encoding="utf-8")
    original_safe_read = context_compiler_module.safe_read_text

    def replace_after_safe_read(path):
        body = original_safe_read(path)
        if Path(path) == markdown_path:
            markdown_path.unlink()
            markdown_path.symlink_to(attacker_path)
        return body

    monkeypatch.setattr(
        context_compiler_module, "safe_read_text", replace_after_safe_read
    )

    loaded = instance.load_compiled(compiled["context_id"])

    assert loaded["context_markdown"] == verified_markdown
    assert Path(loaded["context_md"]).read_text(encoding="utf-8") == (
        "# attacker replacement\n"
    )


def test_compile_contract_rejects_client_body_invalid_exclusions_hash_and_ttl(
    tmp_path,
):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path / "contract",
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    result = preview(instance)

    with pytest.raises(TypeError):
        instance.compile(
            preview_id=result["preview_id"],
            preview_hash=result["preview_hash"],
            excluded_item_ids=[],
            sections={"verified_facts": []},
        )
    with pytest.raises(ContextCompilerError) as invalid_exclusion:
        instance.compile(
            preview_id=result["preview_id"],
            preview_hash=result["preview_hash"],
            excluded_item_ids=["not_in_preview"],
            request_id="req_invalid_exclusion",
            idempotency_key="idem_invalid_exclusion",
            actor=ACTOR,
            reason="invalid exclusion",
        )
    assert invalid_exclusion.value.code == "stale_preview"
    with pytest.raises(ContextCompilerError) as invalid_hash:
        instance.compile(
            preview_id=result["preview_id"],
            preview_hash="sha256:" + "0" * 64,
            excluded_item_ids=[],
            request_id="req_invalid_hash",
            idempotency_key="idem_invalid_hash",
            actor=ACTOR,
            reason="invalid hash",
        )
    assert invalid_hash.value.code == "stale_preview"

    clock = MutableClock()
    expiring = compiler(
        tmp_path / "ttl",
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
        clock=clock,
    )
    expired = expiring.preview(
        "评审客户技术方案",
        mode="reviewer",
        role_scope=["work"],
        domain_scope=["technical"],
        ttl_seconds=1,
        request_id="req_expiring",
        idempotency_key="idem_expiring",
        actor=ACTOR,
    )
    clock.advance(2)
    with pytest.raises(ContextCompilerError) as ttl:
        compile_preview(expiring, expired)
    assert ttl.value.code == "stale_preview"


def test_compile_idempotency_and_exclusions_recompute_exact_pack(tmp_path):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path,
        claims=[
            claim("clm_keep", "客户技术方案需要可回滚"),
            claim("clm_remove", "客户技术方案需要灰度发布"),
        ],
    )
    result = preview(instance)
    first = instance.compile(
        preview_id=result["preview_id"],
        preview_hash=result["preview_hash"],
        excluded_item_ids=["clm_remove"],
        request_id="req_compile_same",
        idempotency_key="idem_compile_same",
        actor=ACTOR,
        reason="remove one item",
    )
    repeated = instance.compile(
        preview_id=result["preview_id"],
        preview_hash=result["preview_hash"],
        excluded_item_ids=["clm_remove"],
        request_id="req_compile_repeat",
        idempotency_key="idem_compile_same",
        actor=ACTOR,
        reason="remove one item",
    )

    assert repeated["context_id"] == first["context_id"]
    pack = json.loads(Path(first["context_json"]).read_text(encoding="utf-8"))
    validate_context_pack(pack)
    encoded = json.dumps(pack, ensure_ascii=False)
    assert "clm_keep" in encoded
    assert "clm_remove" not in encoded
    assert pack["provenance"]["claim_ids"] == ["clm_keep"]
    assert "user_excluded" in pack["privacy_policy"]["reasons"]

    with pytest.raises(ContextCompilerError) as conflict:
        instance.compile(
            preview_id=result["preview_id"],
            preview_hash=result["preview_hash"],
            excluded_item_ids=[],
            request_id="req_compile_conflict",
            idempotency_key="idem_compile_same",
            actor=ACTOR,
            reason="remove one item",
        )
    assert conflict.value.code == "idempotency_conflict"


def test_auto_requires_resolution_and_markdown_has_exact_trust_labels(tmp_path):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    auto = instance.preview(
        "评审客户技术方案",
        mode="auto",
        role_scope=["work"],
        domain_scope=["technical"],
        request_id="req_auto_compile",
        idempotency_key="idem_auto_compile",
        actor=ACTOR,
    )

    with pytest.raises(ContextCompilerError) as unresolved:
        compile_preview(instance, auto)
    assert unresolved.value.code == "unresolved_context_mode"
    compiled = compile_preview(
        instance,
        auto,
        suffix="resolved",
        resolved_mode="reviewer",
    )
    markdown = Path(compiled["context_md"]).read_text(encoding="utf-8")

    assert "## 已验证事实" in markdown
    assert "## 系统推断" in markdown
    assert "## 未知与边界" in markdown
    assert "Evidence IDs" in markdown
    assert ("/" + "Users/") not in markdown
    assert compiled["lifecycle_status"] == "compiled"


def test_auto_resolved_mode_is_authoritative_across_publish_failure_and_retry(
    tmp_path, monkeypatch
):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    auto = instance.preview(
        "评审客户技术方案",
        mode="auto",
        role_scope=["work"],
        domain_scope=["technical"],
        request_id="req_auto_mode_binding",
        idempotency_key="idem_auto_mode_binding",
        actor=ACTOR,
    )
    original = instance._publish_pack
    attempts = {"count": 0}

    def fail_publication_once(pack, markdown):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("injected mode publication failure")
        return original(pack, markdown)

    monkeypatch.setattr(instance, "_publish_pack", fail_publication_once)
    with pytest.raises(ContextCompilerError) as first:
        compile_preview(
            instance,
            auto,
            suffix="mode-first",
            resolved_mode="reviewer",
        )
    assert first.value.code == "pack_publish_failed"
    committed = instance.context_store.get(auto["preview_id"])
    assert committed["mode"] == "reviewer"

    with pytest.raises(ContextCompilerError) as changed:
        compile_preview(
            instance,
            auto,
            suffix="mode-changed",
            resolved_mode="writer",
        )
    assert changed.value.code == "resolved_mode_conflict"

    repaired = compile_preview(
        instance,
        auto,
        suffix="mode-repair",
        resolved_mode="reviewer",
    )
    before = Path(repaired["context_json"]).read_bytes()
    with pytest.raises(ContextCompilerError) as overwrite:
        compile_preview(
            instance,
            auto,
            suffix="mode-overwrite",
            resolved_mode="writer",
        )
    assert overwrite.value.code == "resolved_mode_conflict"
    assert Path(repaired["context_json"]).read_bytes() == before


def test_pack_staging_and_event_failures_are_not_distributable(tmp_path):
    from context_compiler import ContextCompilerError

    pack_failure = compiler(
        tmp_path / "pack",
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    pack_preview = preview(pack_failure)

    def fail_stage(*_args, **_kwargs):
        raise OSError("injected pack failure")

    pack_failure._stage_pack = fail_stage
    with pytest.raises(ContextCompilerError) as failed_pack:
        compile_preview(pack_failure, pack_preview)
    assert failed_pack.value.code == "pack_write_failed"
    assert pack_failure.context_store.get(pack_preview["preview_id"])[
        "lifecycle_status"
    ] == "preview"

    event_failure = compiler(
        tmp_path / "event",
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    event_preview = preview(event_failure)

    def fail_event(*_args, **_kwargs):
        raise OSError("injected event failure")

    event_failure.context_store.begin_compile = fail_event
    with pytest.raises(ContextCompilerError) as failed_event:
        compile_preview(event_failure, event_preview)
    assert failed_event.value.code == "compile_commit_failed"
    packs = event_failure.context_store.root / "packs"
    assert not packs.exists() or not list(packs.glob("ctx_*/context.json"))


def test_same_idempotency_repairs_ready_publication_after_compiled_event(
    tmp_path, monkeypatch
):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    result = preview(instance)
    original = instance._publish_pack
    attempts = {"count": 0}

    def fail_publication_once(pack, markdown):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("injected READY publication failure")
        return original(pack, markdown)

    monkeypatch.setattr(instance, "_publish_pack", fail_publication_once)
    with pytest.raises(ContextCompilerError) as first:
        compile_preview(instance, result, suffix="repair")
    assert first.value.code == "pack_publish_failed"
    compiled_record = instance.context_store.get(result["preview_id"])
    assert compiled_record["lifecycle_status"] == "compiled"
    with pytest.raises(ContextCompilerError) as unavailable:
        instance.load_compiled(compiled_record["context_id"])
    assert unavailable.value.code == "context_not_ready"

    repaired = compile_preview(instance, result, suffix="repair")

    assert repaired["context_id"] == compiled_record["context_id"]
    assert Path(repaired["context_json"]).is_file()
    assert Path(repaired["context_md"]).is_file()


def test_different_idempotency_repairs_same_committed_context_without_new_event(
    tmp_path, monkeypatch
):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    result = preview(instance)
    original = instance._publish_pack
    attempts = {"count": 0}

    def fail_publication_once(pack, markdown):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("injected READY publication failure")
        return original(pack, markdown)

    monkeypatch.setattr(instance, "_publish_pack", fail_publication_once)
    with pytest.raises(ContextCompilerError) as first:
        compile_preview(instance, result, suffix="lost-key")
    assert first.value.code == "pack_publish_failed"
    committed = instance.context_store.get(result["preview_id"])
    head = instance.context_store.events.watermark()

    repaired = compile_preview(instance, result, suffix="new-key")

    assert repaired["context_id"] == committed["context_id"]
    assert instance.context_store.events.watermark() == head
    assert instance.load_compiled(committed["context_id"])["context_id"] == committed[
        "context_id"
    ]


def test_concurrent_equivalent_compile_converges_and_publishes_winning_context(
    tmp_path, monkeypatch
):
    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )
    result = preview(instance)
    original = instance.context_store.begin_compile
    raced = {"value": False}

    def commit_competitor_then_continue(preview_id, **kwargs):
        if not raced["value"]:
            raced["value"] = True
            competing = dict(kwargs)
            competing["request_id"] = "req_competing_compile"
            competing["idempotency_key"] = "idem_competing_compile"
            original(preview_id, **competing)
        return original(preview_id, **kwargs)

    monkeypatch.setattr(
        instance.context_store, "begin_compile", commit_competitor_then_continue
    )

    compiled = compile_preview(instance, result, suffix="concurrent")

    authority = instance.context_store.get(result["preview_id"])
    assert compiled["context_id"] == authority["context_id"]
    assert instance.context_store.events.watermark() == 2
    assert instance.load_compiled(authority["context_id"])["context_id"] == authority[
        "context_id"
    ]


@pytest.mark.parametrize(
    "identifier",
    ["../escape", "prv_../escape", "ctx_../escape", "prv_" + "a" * 31 + "/"],
)
def test_context_identifiers_fail_closed_before_any_pack_path(identifier, tmp_path):
    from context_compiler import ContextCompilerError

    instance = compiler(
        tmp_path,
        claims=[claim("clm_fact", "客户技术方案需要可回滚")],
    )

    with pytest.raises(ContextCompilerError) as compile_error:
        instance.compile(
            preview_id=identifier,
            preview_hash="sha256:" + "0" * 64,
            excluded_item_ids=[],
            request_id="req_traversal",
            idempotency_key="idem_traversal",
            actor=ACTOR,
            reason="reject traversal",
        )
    assert compile_error.value.code == "invalid_identifier"

    with pytest.raises(ContextCompilerError) as load_error:
        instance.load_compiled(identifier)
    assert load_error.value.code == "invalid_identifier"
    assert not (tmp_path.parent / "escape").exists()
