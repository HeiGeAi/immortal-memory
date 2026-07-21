import json
import threading
from datetime import datetime, timezone

import pytest

from context_compiler import ContextCompiler
from context_store import ContextStore
from model_types import CONTEXT_SECTIONS, new_context_pack


ACTOR = {"kind": "owner", "id": "owner"}
SOURCE_REVISION = {
    "claims_event_seq": 1,
    "living_self_version": "lsv_" + "a" * 32,
    "judgments_event_seq": 1,
    "compiler_version": "1.1.0",
    "policy_version": 1,
}


def _sections():
    result = {name: [] for name in CONTEXT_SECTIONS}
    result["verified_facts"] = [
        {
            "kind": "claim",
            "id": "clm_" + "1" * 32,
            "revision": 2,
            "status": "confirmed",
            "source_kind": "direct",
            "summary": "已验证事实",
            "privacy": "restricted",
            "evidence_ids": ["ev_1"],
            "claim_ids": ["clm_" + "1" * 32],
        }
    ]
    result["confirmed_self_models"] = [
        {
            "kind": "self_model",
            "id": "smi_" + "2" * 32,
            "revision": 3,
            "status": "confirmed",
            "source_kind": "self_model",
            "summary": "已确认模型",
            "privacy": "restricted",
            "evidence_ids": ["ev_2"],
            "claim_ids": ["clm_" + "1" * 32],
        }
    ]
    result["judgment_cards"] = [
        {
            "kind": "judgment",
            "id": "jdg_" + "3" * 32,
            "revision": 4,
            "status": "confirmed",
            "source_kind": "judgment",
            "summary": "已确认判断",
            "privacy": "restricted",
            "evidence_ids": ["ev_3"],
            "claim_ids": [],
        }
    ]
    return result


def _seed_compiled(vault, *, expires_at="2099-07-21T09:00:00+00:00"):
    clock = lambda: datetime(2099, 7, 21, 8, 0, tzinfo=timezone.utc)
    contexts = ContextStore(vault, clock=clock)
    sections = _sections()
    preview = contexts.create_preview(
        task="验证任务",
        mode="reviewer",
        source_revision=SOURCE_REVISION,
        sections=sections,
        privacy_policy={"excluded_count": 0, "reasons": []},
        ttl_seconds=3600,
        expected_version=0,
        request_id="req_preview",
        idempotency_key="idem_preview",
        actor=ACTOR,
        reason="创建预览",
    )
    compiled = contexts.begin_compile(
        preview["preview_id"],
        approved_mode="reviewer",
        preview_hash=preview["preview_hash"],
        source_revision=SOURCE_REVISION,
        excluded_item_ids=[],
        expected_version=1,
        request_id="req_compile",
        idempotency_key="idem_compile",
        actor=ACTOR,
        reason="批准预览",
    )
    pack = new_context_pack(
        task="验证任务",
        mode="reviewer",
        living_self_version=SOURCE_REVISION["living_self_version"],
        lifecycle_status="compiled",
        availability_status="active",
        max_chars=24_000,
        max_bytes=256_000,
        claims_event_seq=1,
        judgments_event_seq=1,
        compiler_version="1.1.0",
        policy_version=1,
        preview_hash=preview["preview_hash"],
        sections=sections,
        now=preview["generated_at"],
        expires_at=expires_at,
    )
    pack["context_id"] = compiled["context_id"]
    pack = ContextCompiler._rehash_pack(pack)
    compiler = ContextCompiler(vault, context_store=contexts, clock=clock)
    compiler._publish_pack(pack, compiler._render_markdown(pack))
    return contexts, compiled, pack


def _consume(store, compiled, *, key="idem_consume"):
    return store.consume(
        compiled["context_id"],
        expected_version=2,
        request_id="req_consume",
        idempotency_key=key,
        actor=ACTOR,
        reason="Agent 已接收",
    )


def _outcome_store(vault, contexts):
    from outcome_store import OutcomeStore

    compiler = ContextCompiler(vault, context_store=contexts, clock=contexts._clock)
    compiler._current_source_revision = lambda: dict(SOURCE_REVISION)
    return OutcomeStore(
        vault,
        context_store=contexts,
        context_compiler=compiler,
        clock=contexts._clock,
    )


def _record(store, compiled, **overrides):
    values = {
        "adopted": "partial",
        "result": "mixed",
        "summary": "结果需要复盘",
        "confirmed_refs": [
            {"kind": "claim", "id": "clm_" + "1" * 32, "revision": 2}
        ],
        "challenged_refs": [
            {"kind": "judgment", "id": "jdg_" + "3" * 32, "revision": 4}
        ],
        "expected_version": 3,
        "request_id": "req_outcome",
        "idempotency_key": "idem_outcome",
        "actor": ACTOR,
        "reason": "用户反馈",
    }
    values.update(overrides)
    return store.record_outcome(compiled["context_id"], **values)


def test_context_must_be_consumed_before_outcome(tmp_path):
    from outcome_store import OutcomeStore, OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)

    with pytest.raises(OutcomeStoreError) as failure:
        _record(store, compiled)

    assert failure.value.code == "invalid_transition"
    assert not store.events.exists()


def test_consume_verifies_pack_and_outcome_commits_exact_snapshot_refs(tmp_path):
    from outcome_store import OutcomeStore

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    consumed = _consume(store, compiled)
    outcome = _record(store, compiled)

    assert consumed["lifecycle_status"] == "consumed"
    assert outcome["confirmed_refs"][0]["revision"] == 2
    assert store.context(compiled["context_id"])["outcome_id"] == outcome["outcome_id"]
    assert store.get(compiled["context_id"]) == outcome
    assert store.list() == [outcome]


@pytest.mark.parametrize(
    "field,refs,code",
    [
        (
            "confirmed_refs",
            [{"kind": "claim", "id": "clm_" + "9" * 32, "revision": 1}],
            "outcome_ref_not_in_context",
        ),
        (
            "confirmed_refs",
            [{"kind": "claim", "id": "clm_" + "1" * 32, "revision": 1}],
            "outcome_ref_not_in_context",
        ),
        (
            "confirmed_refs",
            [
                {"kind": "claim", "id": "clm_" + "1" * 32, "revision": 2},
                {"kind": "claim", "id": "clm_" + "1" * 32, "revision": 2},
            ],
            "duplicate_outcome_ref",
        ),
    ],
)
def test_outcome_refs_fail_closed_when_unknown_stale_or_duplicate(
    tmp_path, field, refs, code
):
    from outcome_store import OutcomeStore, OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)

    with pytest.raises(OutcomeStoreError) as failure:
        _record(store, compiled, **{field: refs})
    assert failure.value.code == code


def test_same_ref_cannot_be_confirmed_and_challenged(tmp_path):
    from outcome_store import OutcomeStore, OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)
    ref = {"kind": "claim", "id": "clm_" + "1" * 32, "revision": 2}

    with pytest.raises(OutcomeStoreError) as failure:
        _record(store, compiled, confirmed_refs=[ref], challenged_refs=[ref])
    assert failure.value.code == "outcome_ref_conflict"


def test_outcome_does_not_mutate_model_stores(tmp_path):
    from outcome_store import OutcomeStore

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    protected = {}
    for relative in (
        "model/claims/events.jsonl",
        "model/living-self/current.json",
        "judgment/events.jsonl",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("sentinel:" + relative).encode("utf-8"))
        protected[relative] = path.read_bytes()
    _consume(store, compiled)
    _record(store, compiled)

    for relative, body in protected.items():
        assert (tmp_path / relative).read_bytes() == body


def test_append_then_context_failure_is_reconciled_by_same_intent_retry(
    tmp_path, monkeypatch
):
    from outcome_store import OutcomeStore

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)
    original = contexts.mark_outcome_recorded
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("injected context append failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(contexts, "mark_outcome_recorded", fail_once)
    with pytest.raises(OSError):
        _record(store, compiled)
    assert store.events.watermark() == 1
    assert store.list() == []

    recovered = _record(store, compiled)
    assert contexts.get(compiled["context_id"])["outcome_id"] == recovered["outcome_id"]
    assert store.events.watermark() == 1


def test_same_key_different_intent_conflicts_without_second_event(tmp_path):
    from outcome_store import OutcomeStore, OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)
    _record(store, compiled)

    with pytest.raises(OutcomeStoreError) as failure:
        _record(store, compiled, summary="另一个结果")
    assert failure.value.code == "idempotency_conflict"
    assert store.events.watermark() == 1


def test_concurrent_same_key_converges_to_one_committed_outcome(tmp_path):
    from outcome_store import OutcomeStore

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)
    results = []
    failures = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait()
            results.append(_record(store, compiled))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len({row["outcome_id"] for row in results}) == 1
    assert store.events.watermark() == 1


def test_pack_tamper_blocks_consume_and_outcome(tmp_path):
    from outcome_store import OutcomeStore, OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    context_path = contexts.root / "packs" / compiled["context_id"] / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(OutcomeStoreError) as consume_failure:
        _consume(store, compiled)
    assert consume_failure.value.code == "context_not_ready"

    contexts, compiled, _pack = _seed_compiled(tmp_path / "second")
    store = _outcome_store(tmp_path / "second", contexts)
    _consume(store, compiled)
    ready = contexts.root / "packs" / compiled["context_id"] / "READY.json"
    ready.write_text("{}\n", encoding="utf-8")
    with pytest.raises(OutcomeStoreError) as outcome_failure:
        _record(store, compiled)
    assert outcome_failure.value.code == "context_not_ready"
    assert not store.events.exists()


def test_summary_and_reason_are_redacted_and_bounded(tmp_path):
    from outcome_store import MAX_OUTCOME_REASON_CHARS, MAX_OUTCOME_SUMMARY_CHARS, OutcomeStore

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)
    secret = "ghp_" + "a" * 30
    outcome = _record(
        store,
        compiled,
        summary=secret + ("有效" * 600),
        reason=secret + ("复盘" * 600),
    )
    raw = store.events.path.read_text(encoding="utf-8")

    assert len(outcome["summary"]) <= MAX_OUTCOME_SUMMARY_CHARS
    event = json.loads(raw)
    assert len(event["payload"]["operation"]["reason"]) <= MAX_OUTCOME_REASON_CHARS
    assert secret not in raw


def test_context_linkage_without_matching_event_fails_closed(tmp_path):
    from outcome_store import OutcomeStore, OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)
    outcome = _record(store, compiled)
    event = json.loads(store.events.path.read_text(encoding="utf-8"))
    event["payload"]["outcome"]["summary"] = "tampered"
    store.events.path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(OutcomeStoreError) as failure:
        store.get(compiled["context_id"])
    assert failure.value.code == "outcome_event_corruption"


def test_context_link_without_outcome_event_is_not_exposed(tmp_path):
    from outcome_store import OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)
    contexts.mark_outcome_recorded(
        compiled["context_id"],
        expected_version=3,
        request_id="req_orphan",
        idempotency_key="idem_orphan",
        actor=ACTOR,
        reason="injected orphan",
        outcome_id="out_" + "4" * 32,
        outcome_hash="sha256:" + "5" * 64,
    )

    with pytest.raises(OutcomeStoreError) as failure:
        store.get(compiled["context_id"])
    assert failure.value.code == "outcome_link_missing"
    assert store.list() == []


def test_unlinked_outcome_and_mismatched_context_hash_are_not_exposed(
    tmp_path, monkeypatch
):
    from outcome_store import OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path)
    store = _outcome_store(tmp_path, contexts)
    _consume(store, compiled)
    original = contexts.mark_outcome_recorded
    monkeypatch.setattr(
        contexts,
        "mark_outcome_recorded",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected")),
    )
    with pytest.raises(OSError):
        _record(store, compiled)
    with pytest.raises(OutcomeStoreError) as unlinked:
        store.get(compiled["context_id"])
    assert unlinked.value.code == "outcome_uncommitted"
    assert store.list() == []

    monkeypatch.setattr(contexts, "mark_outcome_recorded", original)
    _record(store, compiled)
    rows = contexts.events.path.read_text(encoding="utf-8").splitlines()
    last = json.loads(rows[-1])
    bad_hash = "sha256:" + "f" * 64
    last["payload"]["operation"]["outcome_hash"] = bad_hash
    last["payload"]["record"]["outcome_hash"] = bad_hash
    rows[-1] = json.dumps(last, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    contexts.events.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(OutcomeStoreError) as mismatched:
        store.get(compiled["context_id"])
    assert mismatched.value.code == "outcome_uncommitted"


def test_expired_or_source_stale_compiled_context_cannot_be_consumed(tmp_path):
    from outcome_store import OutcomeStoreError

    contexts, compiled, _pack = _seed_compiled(tmp_path / "expired")
    store = _outcome_store(tmp_path / "expired", contexts)
    contexts._clock = lambda: datetime(2099, 7, 21, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(OutcomeStoreError) as expired:
        _consume(store, compiled)
    assert expired.value.code == "stale_context"

    contexts, compiled, _pack = _seed_compiled(tmp_path / "stale")
    store = _outcome_store(tmp_path / "stale", contexts)
    store.compiler._current_source_revision = lambda: dict(
        SOURCE_REVISION, claims_event_seq=2
    )
    with pytest.raises(OutcomeStoreError) as stale:
        _consume(store, compiled)
    assert stale.value.code == "stale_context"
