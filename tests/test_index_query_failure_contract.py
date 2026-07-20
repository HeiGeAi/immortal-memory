import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import index_db
import index_integrity
import notes_transactions
import search
from index_locks import source_lock


CORE_DIR = Path(index_db.__file__).resolve().parent


def record(rec_id, content):
    return {
        "id": rec_id,
        "timestamp": "2026-07-19T12:00:00+08:00",
        "source": "test",
        "role": "user",
        "project": "immortal",
        "content": content,
    }


def build_index(source: Path, database: Path, content: str = "needle"):
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(record("a", content), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    index_integrity.reconcile_index(source, database)


def test_missing_fts_table_maps_trusted_watermark_to_unavailable(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    build_index(source, database)
    with sqlite3.connect(database) as con:
        con.execute("DROP TABLE docs_fts")
        con.commit()
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)

    assert index_db.is_ready() is True
    ready, labels, rankings = index_db.ready_channels("needle", limit=5)
    assert (ready, labels, rankings) == (False, [], [])
    assert search.unified_search("needle", limit=5) == (
        "index_unavailable",
        [],
    )


def test_real_cli_and_json_fail_nonzero_for_trusted_broken_query_schema(
    tmp_path,
):
    vault = tmp_path / ".immortal"
    source = vault / "index.jsonl"
    database = vault / "search_index.db"
    build_index(source, database)
    with sqlite3.connect(database) as con:
        con.execute("DROP TABLE docs_fts")
        con.commit()
    environment = {**os.environ, "HOME": str(tmp_path)}

    text_result = subprocess.run(
        [sys.executable, str(CORE_DIR / "search.py"), "needle"],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    json_result = subprocess.run(
        [
            sys.executable,
            str(CORE_DIR / "immortal.py"),
            "recall",
            "needle",
            "--json",
        ],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert text_result.returncode == 2
    assert text_result.stdout == ""
    assert "搜索索引不可用" in text_result.stderr
    assert json_result.returncode == 2
    payload = json.loads(json_result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "index_unavailable"


def test_escaped_user_query_remains_a_normal_successful_query(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    query = 'alpha "beta"'
    build_index(source, database, content=f"prefix {query} suffix")
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)

    ready, labels, rankings = index_db.ready_channels(query, limit=5)

    assert ready is True
    assert labels
    assert rankings


def test_notes_migration_rebuild_marker_forces_index_unavailable(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    build_index(source, database)
    manifest = tmp_path / "notes" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "migration_status": "complete",
                "index_rebuild_required": True,
                "sources": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)

    assert index_db.is_ready() is False
    assert index_db.ready_channels("needle", limit=5) == (False, [], [])


def test_successful_index_sync_clears_notes_migration_rebuild_marker(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    source.parent.mkdir(exist_ok=True)
    source.write_text(
        json.dumps(record("a", "needle"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "notes" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "migration_status": "complete",
                "index_rebuild_required": True,
                "sources": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)

    index_db.sync(force_rebuild=True)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["index_rebuild_required"] is False
    assert payload["index_rebuilt_at"]
    assert index_db.is_ready() is True


def test_index_sync_does_not_overwrite_concurrent_notes_manifest_update(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    source.write_text(
        json.dumps(record("a", "needle"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "notes" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "migration_status": "complete",
                "index_rebuild_required": True,
                "pending_transactions": [],
                "sources": {},
                "stats": {"committed_transactions": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)

    marker_write_started = threading.Event()
    concurrent_write_finished = threading.Event()
    real_atomic_json = notes_transactions.durable_atomic_json
    intercepted = False

    def delayed_marker_write(path, payload):
        nonlocal intercepted
        if (
            Path(path) == manifest
            and payload.get("index_rebuild_required") is False
            and not intercepted
        ):
            intercepted = True
            marker_write_started.set()
            concurrent_write_finished.wait(timeout=0.5)
        real_atomic_json(path, payload)

    monkeypatch.setattr(
        notes_transactions,
        "durable_atomic_json",
        delayed_marker_write,
    )

    def update_manifest():
        assert marker_write_started.wait(timeout=5)
        with source_lock(source, exclusive=True):
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["pending_transactions"] = ["concurrent-tx"]
            payload["sources"]["notes/live.md"] = {"record_id": "live"}
            payload["stats"]["committed_transactions"] = 1
            real_atomic_json(manifest, payload)
        concurrent_write_finished.set()

    writer = threading.Thread(target=update_manifest)
    writer.start()
    index_db.sync(force_rebuild=True)
    writer.join(timeout=5)

    assert not writer.is_alive()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["index_rebuild_required"] is False
    assert payload["pending_transactions"] == ["concurrent-tx"]
    assert payload["sources"]["notes/live.md"]["record_id"] == "live"
    assert payload["stats"]["committed_transactions"] == 1


def test_index_sync_keeps_rebuild_marker_when_source_generation_changes(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    source.write_text(
        json.dumps(record("a", "needle"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "notes" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "migration_status": "complete",
                "index_rebuild_required": True,
                "sources": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)
    real_reconcile = index_integrity.reconcile_index

    def reconcile_then_change_source(*args, **kwargs):
        result = real_reconcile(*args, **kwargs)
        with source.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(record("b", "new fact"), ensure_ascii=False) + "\n"
            )
        return result

    monkeypatch.setattr(
        index_integrity,
        "reconcile_index",
        reconcile_then_change_source,
    )

    index_db.sync(force_rebuild=True)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["index_rebuild_required"] is True
    assert "index_rebuilt_at" not in payload
    assert index_db.is_ready() is False
