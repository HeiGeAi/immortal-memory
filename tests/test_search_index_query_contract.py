import importlib
import json
import threading
import time


index_db = importlib.import_module("index_db")
index_integrity = importlib.import_module("index_integrity")
index_locks = importlib.import_module("index_locks")
search = importlib.import_module("search")


def record(rec_id, content):
    return {
        "id": rec_id,
        "timestamp": "2026-07-19T12:00:00+08:00",
        "source": "test",
        "role": "user",
        "project": "immortal",
        "content": content,
    }


def fail_if_called(name):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not run in recall query path")

    return fail


def test_unified_search_uses_one_atomic_ready_channels_call(monkeypatch):
    expected = [(1.0, record("a", "needle"))]
    monkeypatch.setattr(
        index_db,
        "ready_channels",
        lambda *_args, **_kwargs: (True, ["bm25"], [expected]),
    )
    monkeypatch.setattr(
        index_db,
        "is_ready",
        fail_if_called("index_db.is_ready"),
    )
    monkeypatch.setattr(
        index_db,
        "channels",
        fail_if_called("index_db.channels"),
    )
    monkeypatch.setattr(index_db, "sync", fail_if_called("index_db.sync"))
    monkeypatch.setattr(
        index_integrity,
        "reconcile_index",
        fail_if_called("reconcile_index"),
    )
    monkeypatch.setattr(
        index_integrity,
        "sha256_prefix",
        fail_if_called("sha256_prefix"),
    )
    monkeypatch.setattr(
        index_integrity,
        "_scan_jsonl_once",
        fail_if_called("_scan_jsonl_once"),
    )
    monkeypatch.setattr(
        index_integrity,
        "_database_ids",
        fail_if_called("_database_ids"),
    )
    monkeypatch.setattr(
        search,
        "_inmemory_search",
        fail_if_called("_inmemory_search"),
    )
    mode, results = search.unified_search("needle", limit=5)

    assert mode == "bm25"
    assert results == expected


def test_unified_search_fails_closed_when_trusted_watermark_is_not_ready(
    monkeypatch,
):
    monkeypatch.setattr(
        index_db,
        "ready_channels",
        lambda *_args, **_kwargs: (False, [], []),
    )
    monkeypatch.setattr(
        index_db,
        "is_ready",
        fail_if_called("index_db.is_ready"),
    )
    monkeypatch.setattr(index_db, "sync", fail_if_called("index_db.sync"))
    monkeypatch.setattr(index_db, "channels", fail_if_called("index_db.channels"))
    monkeypatch.setattr(
        search,
        "_inmemory_search",
        fail_if_called("_inmemory_search"),
    )

    mode, results = search.unified_search("needle", limit=5)

    assert mode == "index_unavailable"
    assert results == []


def test_unified_search_distinguishes_ready_empty_from_unavailable(monkeypatch):
    monkeypatch.setattr(
        index_db,
        "ready_channels",
        lambda *_args, **_kwargs: (True, [], []),
    )

    mode, results = search.unified_search("absent", limit=5)

    assert mode == "none"
    assert results == []


def test_index_ready_check_is_o1_and_turns_false_after_source_append(
    tmp_path, monkeypatch
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    source.write_text(
        json.dumps(record("a", "alpha"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    index_integrity.reconcile_index(source, database)
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)
    monkeypatch.setattr(
        index_integrity,
        "reconcile_index",
        fail_if_called("reconcile_index"),
    )
    monkeypatch.setattr(
        index_integrity,
        "sha256_prefix",
        fail_if_called("sha256_prefix"),
    )
    monkeypatch.setattr(
        index_integrity,
        "_scan_jsonl_once",
        fail_if_called("_scan_jsonl_once"),
    )
    monkeypatch.setattr(
        index_integrity,
        "_database_ids",
        fail_if_called("_database_ids"),
    )

    assert index_db.is_ready() is True

    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record("b", "bravo")) + "\n")

    assert index_db.is_ready() is False


def test_ready_channels_blocks_source_writer_until_query_snapshot_finishes(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    source.write_text(
        json.dumps(record("a", "alpha"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    index_integrity.reconcile_index(source, database)
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)
    readiness_checked = threading.Event()
    release_query = threading.Event()
    writer_entered = threading.Event()
    original_ready = index_db._is_ready_unlocked

    def pause_after_readiness():
        ready = original_ready()
        readiness_checked.set()
        assert release_query.wait(timeout=5)
        return ready

    monkeypatch.setattr(index_db, "_is_ready_unlocked", pause_after_readiness)
    query_result = []

    def query_worker():
        query_result.append(index_db.ready_channels("alpha", limit=5))

    def writer_worker():
        with index_locks.source_lock(source, exclusive=True):
            writer_entered.set()
            with source.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record("b", "bravo")) + "\n")

    query = threading.Thread(target=query_worker)
    query.start()
    assert readiness_checked.wait(timeout=5)
    writer = threading.Thread(target=writer_worker)
    writer.start()
    time.sleep(0.1)
    assert writer.is_alive()
    assert not writer_entered.is_set()

    release_query.set()
    query.join(timeout=5)
    writer.join(timeout=5)

    assert query_result
    ready, labels, rankings = query_result[0]
    assert ready is True
    assert labels
    assert rankings
    assert writer_entered.is_set()
    assert index_db.is_ready() is False
