import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

import immortal
import orchestrator
from judgment_store import (
    InvalidJudgmentOperation,
    JudgmentNotFound,
    JudgmentStore,
    _main,
)


ACTOR = {"kind": "owner", "id": "owner"}
TEST_NOW = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


def advancing_store(tmp_path):
    clock = {"now": datetime(2026, 7, 20, 7, tzinfo=timezone.utc)}
    return JudgmentStore(tmp_path, clock=lambda: clock["now"]), clock


def metadata(suffix: str, revision: int) -> dict:
    return {
        "expected_revision": revision,
        "request_id": "req-" + suffix,
        "idempotency_key": "idem-" + suffix,
        "actor": ACTOR,
        "reason": "test " + suffix,
    }


def create_card(store: JudgmentStore, suffix: str = "create") -> dict:
    return store.create(
        title="先做只读审计",
        situation="生产升级",
        decision="先审计再修改",
        evidence_ids=["ev-1"],
        privacy="context_safe",
        **metadata(suffix, 0),
    )


def test_card_result_is_event_backed_and_persistent(tmp_path):
    store, clock = advancing_store(tmp_path)
    card = create_card(store)
    clock["now"] = TEST_NOW
    confirmed = store.transition(
        card["card_id"],
        "confirmed",
        **metadata("confirm", 1),
    )
    updated = store.record_outcome(
        card["card_id"],
        status="positive",
        summary="避免了错误归因",
        observed_at="2026-07-20T08:00:00+00:00",
        **metadata("outcome", 2),
    )

    assert confirmed["revision"] == 2
    assert updated["outcome"]["status"] == "positive"
    assert updated["revision"] == 3
    assert JudgmentStore(tmp_path).get(card["card_id"]) == updated
    event_types = [
        row["event_type"]
        for row in JudgmentStore(tmp_path).events.read_all()
    ]
    assert event_types == [
        "judgment.created",
        "judgment.transitioned",
        "judgment.outcome_recorded",
    ]


def test_ergonomic_defaults_from_plan_remain_supported(tmp_path):
    store, clock = advancing_store(tmp_path)
    card = store.create(
        title="先审计",
        situation="生产升级",
        decision="先读后写",
        evidence_ids=["ev-1"],
    )
    clock["now"] = TEST_NOW
    confirmed = store.transition(card["card_id"], "confirmed", reason="owner confirmed")
    updated = store.record_outcome(
        card["card_id"],
        status="positive",
        summary="避免了误操作",
        observed_at="2026-07-20T08:00:00+00:00",
    )

    assert confirmed["status"] == "confirmed"
    assert updated["outcome"]["status"] == "positive"


def test_idempotency_replay_and_conflict_are_explicit(tmp_path):
    store = JudgmentStore(tmp_path)
    first = create_card(store)
    second = store.create(
        title="先做只读审计",
        situation="生产升级",
        decision="先审计再修改",
        evidence_ids=["ev-1"],
        privacy="context_safe",
        **metadata("create", 0),
    )
    assert second == first
    assert store.events.watermark() == 1

    with pytest.raises(InvalidJudgmentOperation) as captured:
        store.create(
            title="不同意图",
            situation="生产升级",
            decision="直接修改",
            evidence_ids=["ev-2"],
            **metadata("create", 0),
        )
    assert captured.value.code == "idempotency_conflict"


def test_expected_revision_and_legal_state_machine_are_enforced(tmp_path):
    store = JudgmentStore(tmp_path)
    card = create_card(store)

    with pytest.raises(InvalidJudgmentOperation) as conflict:
        store.transition(
            card["card_id"],
            "confirmed",
            **metadata("stale", 0),
        )
    assert conflict.value.code == "version_conflict"

    confirmed = store.transition(
        card["card_id"],
        "confirmed",
        **metadata("confirm", 1),
    )
    candidate = store.transition(
        card["card_id"],
        "candidate",
        **metadata("reopen", 2),
    )
    rejected = store.transition(
        card["card_id"],
        "rejected",
        **metadata("reject", 3),
    )
    assert [confirmed["status"], candidate["status"], rejected["status"]] == [
        "confirmed",
        "candidate",
        "rejected",
    ]

    with pytest.raises(InvalidJudgmentOperation) as invalid:
        store.transition(
            card["card_id"],
            "confirmed",
            **metadata("invalid", 4),
        )
    assert invalid.value.code == "invalid_transition"


def test_correct_is_event_backed_and_confirmed_correction_returns_to_candidate(tmp_path):
    store = JudgmentStore(tmp_path)
    card = create_card(store)
    store.transition(card["card_id"], "confirmed", **metadata("confirm", 1))

    corrected = store.correct(
        card["card_id"],
        changes={
            "decision": "先创建隔离快照，再做只读审计",
            "evidence_ids": ["ev-1", "ev-2"],
        },
        **metadata("correct", 2),
    )

    assert corrected["status"] == "candidate"
    assert corrected["decision"] == "先创建隔离快照，再做只读审计"
    assert corrected["evidence_ids"] == ["ev-1", "ev-2"]
    assert corrected["revision"] == 3
    assert store.events.read_all()[-1]["event_type"] == "judgment.corrected"


def test_outcome_requires_confirmed_card_and_a_valid_timestamp(tmp_path):
    store, clock = advancing_store(tmp_path)
    card = create_card(store)
    clock["now"] = TEST_NOW

    with pytest.raises(InvalidJudgmentOperation) as state_error:
        store.record_outcome(
            card["card_id"],
            status="positive",
            summary="尚未确认",
            observed_at="2026-07-20T08:00:00+00:00",
            **metadata("early-outcome", 1),
        )
    assert state_error.value.code == "invalid_transition"

    store.transition(card["card_id"], "confirmed", **metadata("confirm", 1))
    with pytest.raises(InvalidJudgmentOperation) as timestamp_error:
        store.record_outcome(
            card["card_id"],
            status="positive",
            summary="时间非法",
            observed_at="not-a-time",
            **metadata("bad-time", 2),
        )
    assert timestamp_error.value.code == "invalid_timestamp"


def test_current_and_evaluations_are_rebuilt_from_authoritative_events(tmp_path):
    store, clock = advancing_store(tmp_path)
    card = create_card(store)
    clock["now"] = TEST_NOW
    store.transition(card["card_id"], "confirmed", **metadata("confirm", 1))
    store.record_outcome(
        card["card_id"],
        status="mixed",
        summary="部分有效",
        observed_at="2026-07-20T08:00:00+00:00",
        **metadata("outcome", 2),
    )
    store.current_path.write_text('{"tampered":true}\n', encoding="utf-8")
    store.evaluations_path.write_text('{"private":"leak"}\n', encoding="utf-8")

    rebuilt = JudgmentStore(tmp_path)

    assert rebuilt.get(card["card_id"])["outcome"]["status"] == "mixed"
    evaluations = [
        json.loads(line)
        for line in rebuilt.evaluations_path.read_text(encoding="utf-8").splitlines()
    ]
    assert evaluations == [
        {
            "card_id": card["card_id"],
            "event_seq": 3,
            "observed_at": "2026-07-20T08:00:00+00:00",
            "status": "mixed",
            "summary": "部分有效",
        }
    ]


def test_events_reject_semantic_tampering(tmp_path):
    store = JudgmentStore(tmp_path)
    create_card(store)
    rows = [
        json.loads(line)
        for line in store.events.path.read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["payload"]["card"]["status"] = "confirmed"
    store.events.path.write_text(
        json.dumps(rows[0], ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidJudgmentOperation) as captured:
        JudgmentStore(tmp_path)
    assert captured.value.code == "judgment_event_corruption"


def test_current_projection_is_sorted_and_empty_init_is_side_effect_free(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    JudgmentStore(empty)
    assert list(empty.iterdir()) == []

    store = JudgmentStore(tmp_path)
    first = create_card(store, "z")
    second = store.create(
        title="另一个判断",
        situation="打包发布",
        decision="先做隐私扫描",
        evidence_ids=["ev-2"],
        card_id="jdg_a",
        **metadata("a", 0),
    )
    rows = [
        json.loads(line)
        for line in store.current_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["card_id"] for row in rows] == sorted(
        [first["card_id"], second["card_id"]]
    )


def test_cards_list_never_prints_private_body(tmp_path, capsys):
    store = JudgmentStore(tmp_path)
    card = store.create(
        title="敏感项目",
        situation="客户私密事项",
        decision="使用代号",
        evidence_ids=["ev-secret"],
        privacy="private",
    )
    assert store.cli_list(limit=10) == 0
    output = capsys.readouterr().out
    assert "客户私密事项" not in output
    assert "敏感项目" not in output
    assert "使用代号" not in output
    assert card["card_id"] in output
    assert '"redacted": true' in output


def test_cli_stats_contains_only_aggregates_and_build_replays(tmp_path, capsys):
    store = JudgmentStore(tmp_path)
    store.create(
        title="敏感项目",
        situation="客户私密事项",
        decision="使用代号",
        evidence_ids=["ev-secret"],
        privacy="private",
    )
    store.current_path.unlink()

    assert store.cli_build() == 0
    build = json.loads(capsys.readouterr().out)
    assert build["status"] == "ok"
    assert store.current_path.is_file()

    assert store.cli_stats() == 0
    stats_text = capsys.readouterr().out
    stats = json.loads(stats_text)
    assert stats["total"] == 1
    assert stats["privacy"]["private"] == 1
    assert "敏感项目" not in stats_text
    assert "客户私密事项" not in stats_text


def test_missing_card_and_unsafe_parent_have_stable_failures(tmp_path):
    store = JudgmentStore(tmp_path)
    with pytest.raises(JudgmentNotFound) as missing:
        store.get("jdg_missing")
    assert missing.value.code == "judgment_not_found"

    outside = tmp_path / "outside"
    outside.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "judgment").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception) as unsafe:
        JudgmentStore(vault).create(
            title="test",
            situation="test",
            decision="test",
            evidence_ids=["ev-1"],
        )
    assert getattr(unsafe.value, "code", None) == "unsafe_path"
    assert not (outside / "events.jsonl").exists()


def test_relative_vault_is_bound_at_construction(tmp_path, monkeypatch):
    original = tmp_path / "original"
    elsewhere = tmp_path / "elsewhere"
    original.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(original)
    store = JudgmentStore(Path("vault"))

    monkeypatch.chdir(elsewhere)
    create_card(store)

    assert (original / "vault" / "judgment" / "events.jsonl").is_file()
    assert not (elsewhere / "vault").exists()


def test_concurrent_writers_cannot_both_commit_the_same_revision(tmp_path):
    store = JudgmentStore(tmp_path)
    card = create_card(store)
    barrier = threading.Barrier(2)
    outcomes = []

    def transition(suffix):
        worker = JudgmentStore(tmp_path)
        barrier.wait()
        try:
            outcomes.append(
                worker.transition(
                    card["card_id"],
                    "confirmed",
                    **metadata(suffix, 1),
                )["status"]
            )
        except InvalidJudgmentOperation as exc:
            outcomes.append(exc.code)

    threads = [
        threading.Thread(target=transition, args=("concurrent-a",)),
        threading.Thread(target=transition, args=("concurrent-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["confirmed", "version_conflict"]
    assert JudgmentStore(tmp_path).get(card["card_id"])["revision"] == 2


def test_cli_failure_is_json_on_stderr_with_nonzero_exit(tmp_path, capsys):
    exit_code = _main(["list", "not-an-integer", "--vault-dir", str(tmp_path)])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"] == "invalid_argument"


def test_cards_syntax_errors_are_json_for_standalone_and_packaged_cli(
    tmp_path,
    capsys,
):
    assert _main(["invalid", "--vault-dir", str(tmp_path)]) == 2
    standalone = capsys.readouterr()
    assert standalone.out == ""
    assert json.loads(standalone.err)["error"] == "invalid_argument"

    assert immortal.main(["cards", "invalid"]) == 2
    packaged = capsys.readouterr()
    assert packaged.out == ""
    assert json.loads(packaged.err)["error"] == "invalid_argument"


def test_orchestrator_cards_stage_calls_packaged_cli(monkeypatch):
    calls = []

    def run_script(*args, **kwargs):
        calls.append((args, kwargs))
        return True, '{"status":"ok"}\n'

    monkeypatch.setattr(orchestrator, "run_script", run_script)

    assert orchestrator.cards_build() is True
    assert calls == [
        (("immortal.py", "cards", "build"), {"timeout": 180})
    ]


def test_created_event_binds_initial_outcome_and_timestamps(tmp_path):
    store = JudgmentStore(
        tmp_path,
        clock=lambda: datetime(2026, 7, 20, 8, tzinfo=timezone.utc),
    )
    create_card(store)
    rows = [
        json.loads(line)
        for line in store.events.path.read_text(encoding="utf-8").splitlines()
    ]
    card = rows[0]["payload"]["card"]
    card["outcome"] = {
        "status": "positive",
        "summary": "forged",
        "observed_at": card["created_at"],
    }
    rows[0]["payload"]["operation"]["input"]["outcome"] = dict(card["outcome"])
    store.events.path.write_text(
        json.dumps(rows[0], ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidJudgmentOperation) as captured:
        JudgmentStore(tmp_path)
    assert captured.value.code == "judgment_event_corruption"


def test_future_create_and_outcome_are_rejected_with_stable_errors(tmp_path):
    now = datetime(2026, 7, 20, 8, tzinfo=timezone.utc)
    store = JudgmentStore(tmp_path, clock=lambda: now)

    with pytest.raises(InvalidJudgmentOperation) as future_create:
        store.create(
            title="未来卡片",
            situation="未来",
            decision="未来",
            evidence_ids=["ev-1"],
            now="2099-01-01T00:00:00+00:00",
        )
    assert future_create.value.code == "future_timestamp"

    card = create_card(store)
    store.transition(card["card_id"], "confirmed", **metadata("confirm", 1))
    with pytest.raises(InvalidJudgmentOperation) as future_outcome:
        store.record_outcome(
            card["card_id"],
            status="positive",
            summary="未来结果",
            observed_at="2099-01-01T00:00:00+00:00",
            **metadata("future-outcome", 2),
        )
    assert future_outcome.value.code == "future_timestamp"


def test_future_existing_card_does_not_leak_model_validation_error(tmp_path):
    store = JudgmentStore(
        tmp_path,
        clock=lambda: datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    card = store.create(
        title="未来卡片",
        situation="未来",
        decision="未来",
        evidence_ids=["ev-1"],
        now="2099-01-01T00:00:00+00:00",
    )
    reopened = JudgmentStore(
        tmp_path,
        clock=lambda: datetime(2026, 7, 20, 8, tzinfo=timezone.utc),
    )

    with pytest.raises(InvalidJudgmentOperation) as captured:
        reopened.transition(
            card["card_id"],
            "confirmed",
            **metadata("future-transition", 1),
        )
    assert captured.value.code == "future_timestamp"


def test_concurrent_same_idempotency_create_converges(
    tmp_path,
    monkeypatch,
):
    seed = JudgmentStore(tmp_path)
    seed.create(
        title="seed",
        situation="seed",
        decision="seed",
        evidence_ids=["ev-seed"],
        card_id="jdg_seed",
        **metadata("seed", 0),
    )
    barrier = threading.Barrier(2)
    original = JudgmentStore._find_idempotent
    first_checks = threading.local()

    def synchronize_initial_check(self, idempotency_key, operation):
        if not getattr(first_checks, "done", False):
            first_checks.done = True
            result = original(self, idempotency_key, operation)
            barrier.wait()
            return result
        return original(self, idempotency_key, operation)

    monkeypatch.setattr(
        JudgmentStore,
        "_find_idempotent",
        synchronize_initial_check,
    )
    outcomes = []

    def create_concurrently():
        try:
            outcomes.append(
                JudgmentStore(tmp_path).create(
                    title="并发创建",
                    situation="并发",
                    decision="收敛",
                    evidence_ids=["ev-concurrent"],
                    **metadata("concurrent-create", 0),
                )
            )
        except InvalidJudgmentOperation as exc:
            outcomes.append(exc.code)

    threads = [
        threading.Thread(target=create_concurrently),
        threading.Thread(target=create_concurrently),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 2
    assert isinstance(outcomes[0], dict)
    assert outcomes[0] == outcomes[1]
    assert JudgmentStore(tmp_path).events.watermark() == 2


def test_outcome_idempotency_survives_later_card_state_changes(tmp_path):
    store, clock = advancing_store(tmp_path)
    card = create_card(store)
    clock["now"] = TEST_NOW
    store.transition(card["card_id"], "confirmed", **metadata("confirm", 1))
    outcome_args = {
        "status": "positive",
        "summary": "结果有效",
        "observed_at": "2026-07-20T08:00:00+00:00",
        **metadata("outcome", 2),
    }
    first = store.record_outcome(card["card_id"], **outcome_args)
    retired = store.transition(
        card["card_id"],
        "retired",
        **metadata("retire", 3),
    )

    retried = store.record_outcome(card["card_id"], **outcome_args)

    assert retried == first
    assert retired["status"] == "retired"
    assert store.get(card["card_id"])["status"] == "retired"
    assert store.events.watermark() == 4
