import json
import threading

import pytest

from claim_store import ClaimNotFound, ClaimStore, InvalidTransition
from model_types import new_claim, validate_claim


ACTOR = {"kind": "owner", "id": "owner"}


def claim(claim_id: str = "clm-1", statement: str = "先给结论") -> dict:
    value = new_claim(
        statement=statement,
        source_kind="direct",
        evidence_ids=["ev-1"],
        confidence=0.8,
        confidence_basis={
            "speaker": 0.8,
            "recurrence": 0.8,
            "source_quality": 0.8,
            "policy_version": 1,
            "explanation": "weighted policy inputs verified by the test fixture",
        },
        role_scope=["work"],
        domain_scope=["content"],
        privacy="context_safe",
        now="2026-07-20T00:00:00+00:00",
    )
    value["claim_id"] = claim_id
    validate_claim(value)
    return value


def create(store: ClaimStore, value: dict) -> dict:
    return store.create(
        value,
        expected_revision=0,
        request_id=f"req-create-{value['claim_id']}",
        idempotency_key=f"idem-create-{value['claim_id']}",
        actor=ACTOR,
        reason="captured from direct evidence",
    )


def transition(
    store: ClaimStore,
    claim_id: str,
    status: str,
    expected_revision: int,
    suffix: str,
) -> dict:
    return store.transition(
        claim_id,
        status,
        reason=f"owner {status}",
        expected_revision=expected_revision,
        request_id=f"req-{suffix}",
        idempotency_key=f"idem-{suffix}",
        actor=ACTOR,
    )


def test_create_get_and_list_write_sorted_current_view_with_watermarks(tmp_path):
    store = ClaimStore(tmp_path)

    second = create(store, claim("clm-b", "第二条"))
    first = create(store, claim("clm-a", "第一条"))

    assert second["based_on_event_seq"] == 1
    assert first["based_on_event_seq"] == 2
    assert second["stream_version"] == 1
    assert [row["claim_id"] for row in store.list()] == ["clm-a", "clm-b"]
    assert store.get("clm-a")["statement"] == "第一条"
    current_rows = [
        json.loads(line)
        for line in store.current_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["claim_id"] for row in current_rows] == ["clm-a", "clm-b"]


def test_every_public_write_rejects_missing_or_blank_operation_metadata(tmp_path):
    store = ClaimStore(tmp_path)

    with pytest.raises(InvalidTransition) as create_error:
        store.create(
            claim(),
            expected_revision=0,
            request_id="",
            idempotency_key="idem-create",
            actor=ACTOR,
            reason="reason",
        )
    assert create_error.value.code == "write_metadata_required"

    create(store, claim())
    with pytest.raises(InvalidTransition) as transition_error:
        store.transition(
            "clm-1",
            "confirmed",
            expected_revision=1,
            request_id="req-confirm",
            idempotency_key="idem-confirm",
            actor={},
            reason="reason",
        )
    assert transition_error.value.code == "write_metadata_required"


def test_unsupported_actor_kind_raises_stable_invalid_transition(tmp_path):
    store = ClaimStore(tmp_path)

    with pytest.raises(InvalidTransition) as captured:
        store.create(
            claim(),
            expected_revision=0,
            request_id="req-create",
            idempotency_key="idem-create",
            actor={"kind": "admin", "id": "owner"},
            reason="reason",
        )

    assert captured.value.code == "invalid_actor"


def test_explicit_state_machine_accepts_only_declared_transitions(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim())

    confirmed = transition(store, "clm-1", "confirmed", 1, "confirm")
    assert confirmed["status"] == "confirmed"
    assert confirmed["revision"] == 2

    with pytest.raises(InvalidTransition) as captured:
        transition(store, "clm-1", "rejected", 2, "reject-confirmed")
    assert captured.value.code == "invalid_transition"

    with pytest.raises(InvalidTransition) as missing:
        store.get("missing")
    assert isinstance(missing.value, ClaimNotFound)
    assert missing.value.code == "claim_not_found"


def test_correct_supersedes_original_and_preserves_four_event_history(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim())
    transition(store, "clm-1", "confirmed", 1, "confirm")

    corrected = store.correct(
        "clm-1",
        "先给结果，再给必要依据",
        reason="more precise",
        expected_revision=2,
        request_id="req-correct",
        idempotency_key="idem-correct",
        actor=ACTOR,
    )

    original = store.get("clm-1")
    assert original["status"] == "superseded"
    assert original["revision"] == 3
    assert corrected["status"] == "confirmed"
    assert corrected["revision"] == 1
    assert corrected["supersedes"] == "clm-1"
    assert corrected["claim_id"] != "clm-1"
    assert len(store.events.read_all()) == 4
    validate_claim(original)
    validate_claim(corrected)


def test_external_suffix_key_cannot_preempt_internal_correction_event(tmp_path):
    store = ClaimStore(tmp_path)
    store.create(
        claim("clm-attacker", "正常外部写入"),
        expected_revision=0,
        request_id="req-attacker",
        idempotency_key="correct-a:replacement",
        actor=ACTOR,
        reason="normal external key remains allowed",
    )
    create(store, claim())
    transition(store, "clm-1", "confirmed", 1, "confirm")

    corrected = store.correct(
        "clm-1",
        "先给结果，再给必要依据",
        reason="more precise",
        expected_revision=2,
        request_id="req-correct-a",
        idempotency_key="correct-a",
        actor=ACTOR,
    )

    assert corrected["status"] == "confirmed"
    assert corrected["supersedes"] == "clm-1"
    assert store.get("clm-1")["status"] == "superseded"
    assert len(store.events.read_all()) == 5


def test_rejected_claim_requires_evidence_not_already_on_the_claim(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim())
    transition(store, "clm-1", "rejected", 1, "reject")

    for evidence_ids in ([], ["ev-1"]):
        with pytest.raises(InvalidTransition) as captured:
            store.reconsider(
                "clm-1",
                evidence_ids=evidence_ids,
                reason="review again",
                expected_revision=2,
                request_id=f"req-reconsider-{len(evidence_ids)}",
                idempotency_key=f"idem-reconsider-{len(evidence_ids)}",
                actor=ACTOR,
            )
        assert captured.value.code == "new_evidence_required"

    reconsidered = store.reconsider(
        "clm-1",
        evidence_ids=["ev-1", "ev-2"],
        reason="new direct evidence",
        expected_revision=2,
        request_id="req-reconsider-new",
        idempotency_key="idem-reconsider-new",
        actor=ACTOR,
    )
    assert reconsidered["status"] == "candidate"
    assert reconsidered["revision"] == 3
    assert reconsidered["evidence_ids"] == ["ev-1", "ev-2"]


def test_reconsider_none_evidence_raises_stable_invalid_transition(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim())
    transition(store, "clm-1", "rejected", 1, "reject")

    with pytest.raises(InvalidTransition) as captured:
        store.reconsider(
            "clm-1",
            evidence_ids=None,
            reason="review again",
            expected_revision=2,
            request_id="req-reconsider-none",
            idempotency_key="idem-reconsider-none",
            actor=ACTOR,
        )

    assert captured.value.code == "new_evidence_required"


def test_concurrent_same_revision_allows_only_one_correction(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim())
    transition(store, "clm-1", "confirmed", 1, "confirm")
    outcomes = []

    def correct(number: int) -> None:
        try:
            store.correct(
                "clm-1",
                f"修正版本 {number}",
                reason="concurrent correction",
                expected_revision=2,
                request_id=f"req-correct-{number}",
                idempotency_key=f"idem-correct-{number}",
                actor=ACTOR,
            )
            outcomes.append("ok")
        except InvalidTransition as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=correct, args=(number,)) for number in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["ok", "version_conflict"]
    active = [row for row in store.list() if row["status"] != "superseded"]
    assert len(active) == 1


def test_missing_current_view_is_repaired_from_event_sequence(tmp_path):
    store = ClaimStore(tmp_path)
    created = create(store, claim())
    store.current_path.unlink()

    repaired = ClaimStore(tmp_path)

    assert repaired.get("clm-1") == created
    assert repaired.get("clm-1")["based_on_event_seq"] == 1
    assert repaired.current_path.is_file()


def test_stale_current_view_is_repaired_to_event_head(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim())
    stale = store.current_path.read_bytes()
    transition(store, "clm-1", "confirmed", 1, "confirm")
    store.current_path.write_bytes(stale)

    repaired = ClaimStore(tmp_path)

    assert repaired.get("clm-1")["status"] == "confirmed"
    assert repaired.get("clm-1")["based_on_event_seq"] == 2


def test_incomplete_current_view_at_event_head_is_repaired(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim("clm-a", "第一条"))
    create(store, claim("clm-b", "第二条"))
    latest_only = store.get("clm-b")
    store.current_path.write_text(
        json.dumps(latest_only, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    repaired = ClaimStore(tmp_path)

    assert [row["claim_id"] for row in repaired.list()] == ["clm-a", "clm-b"]


def test_current_row_missing_stream_version_is_repaired(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim())
    malformed = store.get("clm-1")
    malformed.pop("stream_version")
    store.current_path.write_text(
        json.dumps(malformed, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    repaired = ClaimStore(tmp_path)

    assert repaired.get("clm-1")["stream_version"] == 1


def test_forged_current_row_at_global_head_is_replayed_per_claim(tmp_path):
    store = ClaimStore(tmp_path)
    create(store, claim())
    transition(store, "clm-1", "confirmed", 1, "confirm")
    old_candidate = dict(store.events.read_all()[0]["payload"]["claim"])
    old_candidate["based_on_event_seq"] = 2
    old_candidate["stream_version"] = 2
    store.current_path.write_text(
        json.dumps(old_candidate, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    repaired = ClaimStore(tmp_path)

    assert repaired.get("clm-1")["status"] == "confirmed"
    assert repaired.get("clm-1")["revision"] == 2
    assert repaired.get("clm-1")["stream_version"] == 2


def test_event_commit_before_current_write_is_repaired_on_restart(
    tmp_path,
    monkeypatch,
):
    store = ClaimStore(tmp_path)

    def fail_current(_claims):
        raise OSError("current replacement interrupted")

    monkeypatch.setattr(store, "_write_current", fail_current)
    with pytest.raises(OSError, match="current replacement interrupted"):
        create(store, claim())

    assert store.events.watermark() == 1
    repaired = ClaimStore(tmp_path)
    assert repaired.get("clm-1")["statement"] == "先给结论"


def test_idempotent_retry_after_projection_failure_does_not_duplicate_event(
    tmp_path,
    monkeypatch,
):
    store = ClaimStore(tmp_path)
    real_write = store._write_current
    attempts = {"count": 0}

    def fail_once(claims):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("projection failed")
        return real_write(claims)

    monkeypatch.setattr(store, "_write_current", fail_once)
    with pytest.raises(OSError, match="projection failed"):
        create(store, claim())

    retried = create(store, claim())

    assert retried["claim_id"] == "clm-1"
    assert retried["based_on_event_seq"] == 1
    assert len(store.events.read_all()) == 1


def test_transition_retry_after_projection_failure_is_idempotent(
    tmp_path,
    monkeypatch,
):
    store = ClaimStore(tmp_path)
    create(store, claim())
    real_write = store._write_current
    attempts = {"count": 0}

    def fail_once(claims):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("transition projection failed")
        return real_write(claims)

    monkeypatch.setattr(store, "_write_current", fail_once)
    with pytest.raises(OSError, match="transition projection failed"):
        transition(store, "clm-1", "confirmed", 1, "confirm")

    retried = transition(store, "clm-1", "confirmed", 1, "confirm")

    assert retried["status"] == "confirmed"
    assert retried["revision"] == 2
    assert len(store.events.read_all()) == 2


def test_correction_retry_finishes_second_event_after_mid_operation_failure(
    tmp_path,
    monkeypatch,
):
    store = ClaimStore(tmp_path)
    create(store, claim())
    transition(store, "clm-1", "confirmed", 1, "confirm")
    real_append = store.events.append
    attempts = {"count": 0}

    def fail_second_append(event):
        attempts["count"] += 1
        if attempts["count"] == 2:
            raise OSError("replacement append interrupted")
        return real_append(event)

    monkeypatch.setattr(store.events, "append", fail_second_append)
    with pytest.raises(OSError, match="replacement append interrupted"):
        store.correct(
            "clm-1",
            "先给结果，再给必要依据",
            reason="more precise",
            expected_revision=2,
            request_id="req-correct",
            idempotency_key="idem-correct",
            actor=ACTOR,
        )

    corrected = store.correct(
        "clm-1",
        "先给结果，再给必要依据",
        reason="more precise",
        expected_revision=2,
        request_id="req-correct",
        idempotency_key="idem-correct",
        actor=ACTOR,
    )

    assert corrected["supersedes"] == "clm-1"
    assert store.get("clm-1")["status"] == "superseded"
    assert len(store.events.read_all()) == 4


def test_restart_recovers_pending_correction_without_caller_retry(
    tmp_path,
    monkeypatch,
):
    store = ClaimStore(tmp_path)
    create(store, claim())
    transition(store, "clm-1", "confirmed", 1, "confirm")
    real_append = store.events.append
    attempts = {"count": 0}

    def fail_second_append(event):
        attempts["count"] += 1
        if attempts["count"] == 2:
            raise OSError("replacement append interrupted")
        return real_append(event)

    monkeypatch.setattr(store.events, "append", fail_second_append)
    with pytest.raises(OSError, match="replacement append interrupted"):
        store.correct(
            "clm-1",
            "先给结果，再给必要依据",
            reason="more precise",
            expected_revision=2,
            request_id="req-correct",
            idempotency_key="idem-correct",
            actor=ACTOR,
        )

    repaired = ClaimStore(tmp_path)
    active = [row for row in repaired.list() if row["status"] != "superseded"]

    assert repaired.get("clm-1")["status"] == "superseded"
    assert len(active) == 1
    assert active[0]["statement"] == "先给结果，再给必要依据"
    assert active[0]["supersedes"] == "clm-1"
    assert len(repaired.events.read_all()) == 4
