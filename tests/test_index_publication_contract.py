import json
import multiprocessing
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import collect
import feishu_collect
import index_db
import index_integrity
import index_locks
import index_writer


def record(rec_id, content):
    return {
        "id": rec_id,
        "timestamp": "2026-07-19T12:00:00+08:00",
        "source": "test",
        "role": "user",
        "project": "immortal",
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
            str(row[0])
            for row in con.execute("SELECT rec_id FROM docs")
        }


def hold_database_reader(database, ready, release, observed):
    with index_locks.database_lock(Path(database), exclusive=False):
        with sqlite3.connect(str(database)) as con:
            observed.put(
                {
                    str(row[0])
                    for row in con.execute("SELECT rec_id FROM docs")
                }
            )
        ready.set()
        release.wait(timeout=10)


def publish_database(source, database, completed):
    try:
        index_integrity.reconcile_index(
            Path(source),
            Path(database),
            force_rebuild=True,
        )
        completed.put(None)
    except Exception as exc:
        completed.put(repr(exc))


def test_ready_rejects_same_size_rewrite_with_restored_mtime(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    index_integrity.reconcile_index(source, database)
    monkeypatch.setattr(index_db, "INDEX_FILE", source)
    monkeypatch.setattr(index_db, "DB_FILE", database)
    original = source.stat()

    write_records(source, [record("b", "alpha")])
    assert source.stat().st_size == original.st_size
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert source.stat().st_mtime_ns == original.st_mtime_ns
    assert source.stat().st_ctime_ns != original.st_ctime_ns
    assert index_db.is_ready() is False


def test_current_reconcile_uses_one_source_scan_and_one_database_snapshot(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    index_integrity.reconcile_index(source, database)
    counts = {
        "scan": 0,
        "snapshot": 0,
        "prefix": 0,
        "revision": 0,
    }

    original_scan = index_integrity._scan_jsonl_once
    original_snapshot = index_integrity._database_snapshot
    original_prefix = index_integrity.sha256_prefix
    original_revision = index_integrity.source_revision

    def scan(*args, **kwargs):
        counts["scan"] += 1
        return original_scan(*args, **kwargs)

    def snapshot(*args, **kwargs):
        counts["snapshot"] += 1
        return original_snapshot(*args, **kwargs)

    def prefix(*args, **kwargs):
        counts["prefix"] += 1
        return original_prefix(*args, **kwargs)

    def revision(*args, **kwargs):
        counts["revision"] += 1
        return original_revision(*args, **kwargs)

    monkeypatch.setattr(index_integrity, "_scan_jsonl_once", scan)
    monkeypatch.setattr(index_integrity, "_database_snapshot", snapshot)
    monkeypatch.setattr(index_integrity, "sha256_prefix", prefix)
    monkeypatch.setattr(index_integrity, "source_revision", revision)

    report = index_integrity.reconcile_index(source, database)

    assert report["mode"] == "current"
    assert counts == {
        "scan": 1,
        "snapshot": 1,
        "prefix": 0,
        "revision": 0,
    }


def test_pure_append_uses_one_deep_scan_and_one_old_prefix_read(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    index_integrity.reconcile_index(source, database)
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record("b", "bravo")) + "\n")
    counts = {
        "scan": 0,
        "snapshot": 0,
        "prefix": 0,
        "revision": 0,
    }
    originals = {
        "scan": index_integrity._scan_jsonl_once,
        "snapshot": index_integrity._database_snapshot,
        "prefix": index_integrity.sha256_prefix,
        "revision": index_integrity.source_revision,
    }

    def counted(name):
        def wrapper(*args, **kwargs):
            counts[name] += 1
            return originals[name](*args, **kwargs)

        return wrapper

    monkeypatch.setattr(index_integrity, "_scan_jsonl_once", counted("scan"))
    monkeypatch.setattr(
        index_integrity,
        "_database_snapshot",
        counted("snapshot"),
    )
    monkeypatch.setattr(index_integrity, "sha256_prefix", counted("prefix"))
    monkeypatch.setattr(index_integrity, "source_revision", counted("revision"))

    report = index_integrity.reconcile_index(source, database)

    assert report["mode"] == "incremental"
    assert counts == {
        "scan": 1,
        "snapshot": 1,
        "prefix": 1,
        "revision": 0,
    }


def test_report_only_bounds_missing_id_samples_but_preserves_totals(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "missing.db"
    write_records(
        source,
        [record(f"id-{number:04d}", "content") for number in range(150)],
    )

    report = index_integrity.report_index_integrity(source, database)

    assert report["missing_in_sqlite_count"] == 150
    assert report["missing_in_sqlite_truncated"] is True
    assert report["missing_in_sqlite"] == [
        f"id-{number:04d}" for number in range(100)
    ]
    assert report["missing_in_jsonl_count"] == 0
    assert report["missing_in_jsonl_truncated"] is False


def test_retry_failure_restores_pre_call_database_and_sidecars(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("old", "old")])
    index_integrity.reconcile_index(source, database)
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"original-shm")
    before = {
        database: database.read_bytes(),
        wal: wal.read_bytes(),
        shm: shm.read_bytes(),
    }
    write_records(source, [record("v1", "first")])
    original_replace = index_integrity._replace_database
    original_build = index_integrity._build_staging_once
    builds = 0

    def replace_then_change(staging, target):
        result = original_replace(staging, target)
        write_records(source, [record("v2", "second")])
        return result

    def fail_second_build(path, staging):
        nonlocal builds
        builds += 1
        if builds == 2:
            raise index_integrity.IndexIntegrityError("second build failed")
        return original_build(path, staging)

    monkeypatch.setattr(index_integrity, "_replace_database", replace_then_change)
    monkeypatch.setattr(index_integrity, "_build_staging_once", fail_second_build)

    with pytest.raises(
        index_integrity.IndexIntegrityError,
        match="second build failed",
    ):
        index_integrity.reconcile_index(source, database, force_rebuild=True)

    assert db_ids(database) == {"old"}
    for path, body in before.items():
        assert path.read_bytes() == body
    assert not list(tmp_path.glob("*.rollback.*"))


def test_publish_directory_fsync_failure_restores_pre_call_generation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("old", "old")])
    index_integrity.reconcile_index(source, database)
    before = database.read_bytes()
    write_records(source, [record("new", "new")])
    original_fsync = index_integrity._fsync_directory
    calls = 0

    def fail_publish_directory_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        return original_fsync(path)

    monkeypatch.setattr(
        index_integrity,
        "_fsync_directory",
        fail_publish_directory_fsync,
    )

    with pytest.raises(OSError, match="directory fsync failed"):
        index_integrity.reconcile_index(source, database, force_rebuild=True)

    assert database.read_bytes() == before
    assert db_ids(database) == {"old"}
    assert not list(tmp_path.glob("*.rollback.*"))


def test_failed_first_publication_restores_absent_database(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("new", "new")])
    original_fsync = index_integrity._fsync_directory
    calls = 0

    def fail_publish_directory_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        return original_fsync(path)

    monkeypatch.setattr(
        index_integrity,
        "_fsync_directory",
        fail_publish_directory_fsync,
    )

    with pytest.raises(OSError, match="directory fsync failed"):
        index_integrity.reconcile_index(source, database)

    assert not database.exists()
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()
    assert not list(tmp_path.glob("*.rollback.*"))


def test_recovery_failure_raises_stable_error_and_retains_artifacts(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("old", "old")])
    index_integrity.reconcile_index(source, database)
    write_records(source, [record("new", "new")])
    original_fsync = index_integrity._fsync_directory
    calls = 0

    def fail_publish_and_recovery_fsync(path):
        nonlocal calls
        calls += 1
        if calls in (2, 3):
            raise OSError("forced directory fsync failure")
        return original_fsync(path)

    monkeypatch.setattr(
        index_integrity,
        "_fsync_directory",
        fail_publish_and_recovery_fsync,
    )

    with pytest.raises(
        index_integrity.RecoveryError,
        match="database generation recovery failed",
    ):
        index_integrity.reconcile_index(source, database, force_rebuild=True)

    assert list(tmp_path.glob("*.rollback.*"))


@pytest.mark.parametrize("module", [collect, feishu_collect])
def test_public_collectors_hold_exclusive_source_lock_for_index_append(
    tmp_path,
    monkeypatch,
    module,
):
    daily = tmp_path / "daily"
    daily.mkdir()
    source = tmp_path / "index.jsonl"
    lock_entries = []

    @contextmanager
    def recording_lock(path, exclusive):
        lock_entries.append((Path(path), exclusive))
        yield

    monkeypatch.setattr(module, "DAILY_DIR", daily)
    monkeypatch.setattr(module, "INDEX_FILE", source)
    monkeypatch.setattr(index_writer, "source_lock", recording_lock)
    if module is collect:
        module.write_records({"2026-07-19": [record("a", "alpha")]})
    else:
        module.write_records([record("a", "alpha")])

    assert lock_entries == [(source, True)]
    assert json.loads(source.read_text(encoding="utf-8"))["id"] == "a"


def test_index_lock_pair_always_acquires_source_before_database(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    order = []

    @contextmanager
    def source_guard(path, exclusive):
        order.append(("source_enter", Path(path), exclusive))
        yield
        order.append(("source_exit", Path(path), exclusive))

    @contextmanager
    def database_guard(path, exclusive):
        order.append(("database_enter", Path(path), exclusive))
        yield
        order.append(("database_exit", Path(path), exclusive))

    monkeypatch.setattr(index_locks, "source_lock", source_guard)
    monkeypatch.setattr(index_locks, "database_lock", database_guard)

    with index_locks.index_lock_pair(
        source,
        database,
        source_exclusive=False,
        database_exclusive=True,
    ):
        order.append(("body", None, None))

    assert [entry[0] for entry in order] == [
        "source_enter",
        "database_enter",
        "body",
        "database_exit",
        "source_exit",
    ]


def test_publisher_waits_for_old_reader_then_new_reader_sees_one_generation(
    tmp_path,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("old", "old generation")])
    index_integrity.reconcile_index(source, database)
    context = multiprocessing.get_context("spawn")
    reader_ready = context.Event()
    reader_release = context.Event()
    observed = context.Queue()
    completed = context.Queue()
    reader = context.Process(
        target=hold_database_reader,
        args=(str(database), reader_ready, reader_release, observed),
    )
    reader.start()
    assert reader_ready.wait(timeout=10)
    assert observed.get(timeout=5) == {"old"}

    write_records(source, [record("new", "new generation")])
    publisher = context.Process(
        target=publish_database,
        args=(str(source), str(database), completed),
    )
    publisher.start()
    time.sleep(0.2)
    assert publisher.is_alive()
    assert db_ids(database) == {"old"}

    reader_release.set()
    reader.join(timeout=10)
    publisher.join(timeout=10)

    assert reader.exitcode == 0
    assert publisher.exitcode == 0
    assert completed.get(timeout=5) is None
    assert db_ids(database) == {"new"}
