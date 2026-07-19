import importlib
import json


index_db = importlib.import_module("index_db")
index_integrity = importlib.import_module("index_integrity")
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


def test_unified_search_uses_only_ready_check_and_sqlite_channels(monkeypatch):
    expected = [(1.0, record("a", "needle"))]
    monkeypatch.setattr(index_db, "is_ready", lambda: True)
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
    monkeypatch.setattr(
        index_db,
        "channels",
        lambda *_args, **_kwargs: (["bm25"], [expected]),
    )

    mode, results = search.unified_search("needle", limit=5)

    assert mode == "bm25"
    assert results == expected


def test_unified_search_fails_closed_when_trusted_watermark_is_not_ready(
    monkeypatch,
):
    monkeypatch.setattr(index_db, "is_ready", lambda: False)
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
