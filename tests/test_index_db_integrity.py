import json
import sqlite3
from pathlib import Path

import pytest

import integrity_audit
import index_integrity
from index_integrity import reconcile_index


def record(rec_id, content):
    return {
        "id": rec_id,
        "source": "test",
        "type": "message",
        "timestamp": "2026-07-19T12:00:00+08:00",
        "content": content,
    }


def write_records(path: Path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def db_ids(path: Path):
    with sqlite3.connect(str(path)) as con:
        return {
            row[0]
            for row in con.execute("SELECT rec_id FROM docs").fetchall()
        }


def test_middle_insertion_triggers_staging_rebuild_and_bidirectional_id_parity(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha"), record("c", "charlie")])
    first = reconcile_index(source, database)
    assert first["mode"] == "full_rebuild"

    original = source.read_text(encoding="utf-8").splitlines(keepends=True)
    source.write_text(
        original[0]
        + json.dumps(record("b", "bravo"), ensure_ascii=False)
        + "\n"
        + original[1],
        encoding="utf-8",
    )

    report = reconcile_index(source, database)

    assert report["mode"] == "full_rebuild"
    assert report["reason"] == "prefix_mismatch"
    assert report["jsonl_unique_ids"] == 3
    assert report["sqlite_ids"] == 3
    assert report["missing_in_sqlite"] == []
    assert report["missing_in_jsonl"] == []
    assert db_ids(database) == {"a", "b", "c"}


def test_same_size_middle_rewrite_replaces_old_id_instead_of_appending(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha"), record("b", "bravo")])
    reconcile_index(source, database)
    size_before = source.stat().st_size

    write_records(source, [record("a", "alpha"), record("c", "bravo")])
    assert source.stat().st_size == size_before

    report = reconcile_index(source, database)

    assert report["mode"] == "full_rebuild"
    assert report["reason"] == "prefix_mismatch"
    assert db_ids(database) == {"a", "c"}


def test_failed_staging_validation_preserves_live_database_bytes(tmp_path, monkeypatch):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    before = database.read_bytes()

    write_records(source, [record("replacement", "replacement")])

    def fail_validation(*_args, **_kwargs):
        raise index_integrity.IndexIntegrityError("forced validation failure")

    monkeypatch.setattr(index_integrity, "verify_id_parity", fail_validation)

    with pytest.raises(index_integrity.IndexIntegrityError):
        reconcile_index(source, database)

    assert database.read_bytes() == before
    assert db_ids(database) == {"a"}


def test_forced_rebuild_uses_staging_without_predeleting_database(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    before = database.read_bytes()

    report = reconcile_index(source, database, force_rebuild=True)

    assert report["mode"] == "full_rebuild"
    assert report["reason"] == "forced"
    assert report["jsonl_unique_ids"] == report["sqlite_ids"] == 1
    assert database.exists()
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('{"id":"a"}\n{broken\n', "malformed JSONL at line 2"),
        (
            json.dumps({"source": "test", "content": "missing"}) + "\n",
            "missing record id at line 1",
        ),
        (
            json.dumps({"id": "   ", "source": "test", "content": "blank"}) + "\n",
            "missing record id at line 1",
        ),
        (
            json.dumps(record("duplicate", "first"))
            + "\n"
            + json.dumps(record("duplicate", "second"))
            + "\n",
            "duplicate record id at line 2",
        ),
    ],
)
def test_invalid_source_is_rejected_with_line_number_and_preserves_database(
    tmp_path, body, message
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("safe", "existing")])
    reconcile_index(source, database)
    before = database.read_bytes()

    source.write_text(body, encoding="utf-8")

    with pytest.raises(index_integrity.IndexIntegrityError, match=message):
        reconcile_index(source, database)

    assert database.read_bytes() == before
    assert db_ids(database) == {"safe"}


def test_report_only_does_not_create_database_lock_or_staging(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "missing.db"
    staging = tmp_path / "custom.staging"
    write_records(source, [record("a", "alpha")])
    before = sorted(path.name for path in tmp_path.iterdir())

    report = reconcile_index(
        source,
        database,
        report_only=True,
        staging_path=staging,
    )

    assert report["mode"] == "report_only"
    assert report["jsonl_unique_ids"] == 1
    assert report["sqlite_ids"] == 0
    assert report["missing_in_sqlite"] == ["a"]
    assert report["missing_in_jsonl"] == []
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_report_only_opens_existing_database_in_read_only_mode(
    tmp_path, monkeypatch
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    calls = []
    real_connect = sqlite3.connect

    def recording_connect(target, *args, **kwargs):
        calls.append((str(target), dict(kwargs)))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(index_integrity.sqlite3, "connect", recording_connect)

    report = reconcile_index(source, database, report_only=True)

    assert report["reason"] == "in_sync"
    assert calls
    assert all(call[0].startswith("file:") for call in calls)
    assert all(call[1].get("uri") is True for call in calls)
    assert all("immutable=1" in call[0] for call in calls)


def test_report_only_does_not_modify_existing_wal_or_shm_sidecars(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"do-not-touch")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (database, wal, shm)
    }

    report = reconcile_index(source, database, report_only=True)

    assert report["reason"] == "in_sync"
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (database, wal, shm)
    } == before


def test_report_only_rejects_nonempty_wal_instead_of_ignoring_committed_data(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    Path(str(database) + "-wal").write_bytes(b"not-empty")

    with pytest.raises(
        index_integrity.IndexIntegrityError,
        match="non-empty WAL",
    ):
        reconcile_index(source, database, report_only=True)


def test_missing_fingerprint_forces_staging_full_rebuild(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    with sqlite3.connect(str(database)) as con:
        con.execute("DELETE FROM meta WHERE key='prefix_sha256'")
        con.commit()

    report = reconcile_index(source, database)

    assert report["mode"] == "full_rebuild"
    assert report["reason"] == "fingerprint_missing"
    assert report["jsonl_unique_ids"] == report["sqlite_ids"] == 1


def test_database_id_count_mismatch_forces_staging_full_rebuild(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha"), record("b", "bravo")])
    reconcile_index(source, database)
    with sqlite3.connect(str(database)) as con:
        con.execute("DELETE FROM docs WHERE rec_id='b'")
        con.commit()

    report = reconcile_index(source, database)

    assert report["mode"] == "full_rebuild"
    assert report["reason"] == "id_count_mismatch"
    assert db_ids(database) == {"a", "b"}


def test_pure_append_uses_incremental_sync(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record("b", "bravo")) + "\n")

    report = reconcile_index(source, database)

    assert report["mode"] == "incremental"
    assert report["added"] == 1
    assert db_ids(database) == {"a", "b"}


def test_fact_layer_audit_includes_aggregate_search_index_reconciliation(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha"), record("b", "bravo")])
    reconcile_index(source, database)
    with sqlite3.connect(str(database)) as con:
        con.execute("DELETE FROM docs WHERE rec_id='b'")
        con.commit()

    report = integrity_audit.audit(daily, source, database)

    assert report["search_index"] == {
        "status": "ok",
        "database_exists": True,
        "reason": "id_set_mismatch",
        "jsonl_unique_ids": 2,
        "sqlite_ids": 1,
        "missing_in_sqlite_count": 1,
        "missing_in_jsonl_count": 0,
    }
    serialized = json.dumps(report["search_index"])
    assert '"a"' not in serialized
    assert '"b"' not in serialized
