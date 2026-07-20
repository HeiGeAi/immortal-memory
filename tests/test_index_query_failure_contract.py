import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import index_db
import index_integrity
import search


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
