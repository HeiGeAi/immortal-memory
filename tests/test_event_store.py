import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from event_store import (
    EventConflict,
    EventCorruption,
    JsonlEventStore,
    ReplayLimitExceeded,
)
from model_types import new_event


def event(
    event_id: str,
    *,
    stream_id: str = "stream-1",
    expected_version: int = 0,
    key: str = "",
    value: int = 1,
) -> dict:
    return new_event(
        event_id=event_id,
        event_type="test.changed",
        stream_id=stream_id,
        stream_version=expected_version + 1,
        expected_version=expected_version,
        request_id=f"req-{event_id}",
        idempotency_key=key or f"idem-{event_id}",
        actor={"kind": "system", "id": "test"},
        payload={"value": value},
        now="2026-07-20T00:00:00+00:00",
    )


def append_in_process(path: str, event_id: str, stream_id: str) -> None:
    JsonlEventStore(Path(path)).append(
        event(event_id, stream_id=stream_id, expected_version=0)
    )


def test_append_assigns_sequence_and_tracks_stream_and_global_watermarks(tmp_path):
    store = JsonlEventStore(tmp_path / "events.jsonl")

    first = store.append(event("evt-1", stream_id="stream-a"))
    second = store.append(event("evt-2", stream_id="stream-b"))
    third = store.append(
        event("evt-3", stream_id="stream-a", expected_version=1)
    )

    assert [first["seq"], second["seq"], third["seq"]] == [1, 2, 3]
    assert store.watermark() == 3
    assert store.stream_version("stream-a") == 2
    assert [row["event_id"] for row in store.read_stream("stream-a")] == [
        "evt-1",
        "evt-3",
    ]


def test_expected_version_conflict_does_not_append(tmp_path):
    store = JsonlEventStore(tmp_path / "events.jsonl")
    store.append(event("evt-1", expected_version=0))

    with pytest.raises(EventConflict) as captured:
        store.append(event("evt-2", expected_version=0))

    assert captured.value.code == "version_conflict"
    assert [row["event_id"] for row in store.read_all()] == ["evt-1"]


def test_event_id_retry_is_idempotent_but_changed_event_conflicts(tmp_path):
    store = JsonlEventStore(tmp_path / "events.jsonl")
    original = event("evt-1", key="idem-original", value=1)

    first = store.append(original)
    retry = store.append(dict(original))

    assert retry == first
    assert store.watermark() == 1

    changed = event("evt-1", key="idem-original", value=2)
    with pytest.raises(EventConflict) as captured:
        store.append(changed)
    assert captured.value.code == "event_id_conflict"
    assert store.watermark() == 1


def test_idempotency_key_returns_original_or_rejects_changed_intent(tmp_path):
    store = JsonlEventStore(tmp_path / "events.jsonl")
    first = store.append(event("evt-1", key="idem-shared", value=1))

    equivalent_retry = event("evt-retry", key="idem-shared", value=1)
    assert store.append(equivalent_retry) == first

    changed_retry = event("evt-changed", key="idem-shared", value=2)
    with pytest.raises(EventConflict) as captured:
        store.append(changed_retry)
    assert captured.value.code == "idempotency_conflict"
    assert store.watermark() == 1


def test_process_concurrent_appends_do_not_lose_events(tmp_path):
    path = tmp_path / "events.jsonl"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=append_in_process,
            args=(str(path), f"evt-{number}", f"stream-{number}"),
        )
        for number in range(8)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0] * len(processes)
    rows = JsonlEventStore(path).read_all()
    assert {row["event_id"] for row in rows} == {
        f"evt-{number}" for number in range(8)
    }
    assert sorted(row["seq"] for row in rows) == list(range(1, 9))


def test_live_process_lock_is_never_stolen_only_because_it_is_old(tmp_path):
    path = tmp_path / "events.jsonl"
    lock_path = path.with_name(path.name + ".lock")
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "created_at": 0}),
        encoding="utf-8",
    )
    old = time.time() - 3600
    os.utime(lock_path, (old, old))
    store = JsonlEventStore(
        path,
        lock_timeout=0.05,
        stale_lock_after=0.0,
    )

    with pytest.raises(TimeoutError):
        store.append(event("evt-1"))

    assert lock_path.exists()
    assert not path.exists()


def test_replay_waits_for_the_process_lock_instead_of_reading_a_transient_tail(
    tmp_path,
):
    path = tmp_path / "events.jsonl"
    lock_path = path.with_name(path.name + ".lock")
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "created_at": time.time()}),
        encoding="utf-8",
    )

    with pytest.raises(TimeoutError):
        JsonlEventStore(path, lock_timeout=0.05).read_all()


def test_lock_durability_failure_cleans_up_its_lock_file(tmp_path, monkeypatch):
    import event_store as module

    path = tmp_path / "events.jsonl"

    def fail_fsync(_fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        JsonlEventStore(path).append(event("evt-1"))

    assert not path.with_name(path.name + ".lock").exists()
    assert not path.exists()


def test_same_stream_concurrency_never_silently_loses_a_write(tmp_path):
    path = tmp_path / "events.jsonl"
    JsonlEventStore(path).append(event("evt-created"))
    outcomes = []

    def append(value: dict) -> None:
        try:
            JsonlEventStore(path).append(value)
            outcomes.append("ok")
        except EventConflict as exc:
            outcomes.append(exc.code)

    threads = [
        threading.Thread(
            target=append,
            args=(event(f"evt-{number}", expected_version=1),),
        )
        for number in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["ok", "version_conflict"]
    assert len(JsonlEventStore(path).read_stream("stream-1")) == 2


def test_append_fsyncs_file_and_new_parent_entry(tmp_path, monkeypatch):
    import event_store as module

    calls = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", recording_fsync)
    JsonlEventStore(tmp_path / "new" / "events.jsonl").append(event("evt-1"))

    assert len(calls) >= 2


def test_partial_tail_is_reported_then_recovered_before_next_append(tmp_path):
    path = tmp_path / "events.jsonl"
    store = JsonlEventStore(path)
    first = store.append(event("evt-1"))
    with path.open("ab") as handle:
        handle.write(b'{"event_id":"partial"')
        handle.flush()
        os.fsync(handle.fileno())

    with pytest.raises(EventCorruption) as captured:
        store.read_all()
    assert captured.value.line_number == 2
    assert captured.value.recoverable_tail is True

    second = store.append(event("evt-2", expected_version=1))

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert [row["event_id"] for row in store.read_all()] == ["evt-1", "evt-2"]


def test_complete_tail_without_newline_is_preserved_during_recovery(tmp_path):
    path = tmp_path / "events.jsonl"
    first = {**event("evt-1"), "seq": 1}
    path.write_text(
        json.dumps(first, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    stored = JsonlEventStore(path).append(event("evt-2", expected_version=1))

    assert stored["seq"] == 2
    assert [row["event_id"] for row in JsonlEventStore(path).read_all()] == [
        "evt-1",
        "evt-2",
    ]


def test_middle_corruption_fails_closed_without_modifying_file(tmp_path):
    path = tmp_path / "events.jsonl"
    first = {**event("evt-1"), "seq": 1}
    second = {**event("evt-2", expected_version=1), "seq": 2}
    path.write_text(
        json.dumps(first, sort_keys=True)
        + "\n"
        + "{broken}\n"
        + json.dumps(second, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    with pytest.raises(EventCorruption) as captured:
        JsonlEventStore(path).append(event("evt-3", expected_version=2))

    assert captured.value.line_number == 2
    assert captured.value.recoverable_tail is False
    assert path.read_bytes() == before


def test_middle_blank_line_is_corruption_not_silently_skipped(tmp_path):
    path = tmp_path / "events.jsonl"
    first = {**event("evt-1"), "seq": 1}
    second = {**event("evt-2", expected_version=1), "seq": 2}
    path.write_text(
        json.dumps(first, sort_keys=True)
        + "\n\n"
        + json.dumps(second, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EventCorruption) as captured:
        JsonlEventStore(path).read_all()

    assert captured.value.code == "blank_event_line"
    assert captured.value.line_number == 2


def test_complete_but_invalid_tail_fails_closed_without_truncation(tmp_path):
    path = tmp_path / "events.jsonl"
    first = {**event("evt-1"), "seq": 1}
    path.write_text(
        json.dumps(first, sort_keys=True) + "\n" + '{"event_id":"not-an-envelope"}',
        encoding="utf-8",
    )
    before = path.read_bytes()

    with pytest.raises(EventCorruption) as captured:
        JsonlEventStore(path).append(event("evt-2", expected_version=1))

    assert captured.value.code == "invalid_event"
    assert captured.value.recoverable_tail is False
    assert path.read_bytes() == before


def test_replay_rejects_duplicate_ids_and_non_monotonic_versions(tmp_path):
    duplicate_path = tmp_path / "duplicate.jsonl"
    duplicate = {**event("evt-1"), "seq": 1}
    duplicate_path.write_text(
        json.dumps(duplicate) + "\n" + json.dumps({**duplicate, "seq": 2}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EventCorruption) as duplicate_error:
        JsonlEventStore(duplicate_path).read_all()
    assert duplicate_error.value.code == "duplicate_event_id"

    version_path = tmp_path / "version.jsonl"
    first = {**event("evt-a", expected_version=0), "seq": 1}
    skipped = {**event("evt-b", expected_version=2), "seq": 2}
    version_path.write_text(
        json.dumps(first) + "\n" + json.dumps(skipped) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EventCorruption) as version_error:
        JsonlEventStore(version_path).read_all()
    assert version_error.value.code == "non_monotonic_stream_version"


def test_replay_rejects_duplicate_idempotency_keys_in_existing_log(tmp_path):
    path = tmp_path / "events.jsonl"
    first = {**event("evt-1", key="idem-shared", value=1), "seq": 1}
    second = {
        **event(
            "evt-2",
            expected_version=1,
            key="idem-shared",
            value=2,
        ),
        "seq": 2,
    }
    path.write_text(
        json.dumps(first, sort_keys=True)
        + "\n"
        + json.dumps(second, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EventCorruption) as captured:
        JsonlEventStore(path).read_all()

    assert captured.value.code == "duplicate_idempotency_key"
    assert captured.value.line_number == 2


def test_replay_is_bounded_and_supports_paging_and_projection(tmp_path):
    store = JsonlEventStore(
        tmp_path / "events.jsonl",
        max_replay_events=2,
    )
    store.append(event("evt-1", expected_version=0, value=1))
    store.append(event("evt-2", expected_version=1, value=2))
    store.append(event("evt-3", expected_version=2, value=3))

    with pytest.raises(ReplayLimitExceeded) as captured:
        store.read_all()
    assert captured.value.limit == 2

    page = store.read_all(after_seq=1, limit=2)
    assert [row["seq"] for row in page] == [2, 3]
    total = store.rebuild_view(
        lambda current, row: current + row["payload"]["value"],
        initial=0,
        after_seq=1,
        limit=2,
    )
    assert total == 5
