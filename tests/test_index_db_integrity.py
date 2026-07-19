import hashlib
import json
import os
import sqlite3
import subprocess
import sys
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


def test_same_count_database_id_replacement_forces_rebuild(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha"), record("b", "bravo")])
    reconcile_index(source, database)
    with sqlite3.connect(str(database)) as con:
        con.execute("UPDATE docs SET rec_id='c' WHERE rec_id='b'")
        con.commit()

    report = reconcile_index(source, database)

    assert report["mode"] == "full_rebuild"
    assert report["reason"] == "id_digest_mismatch"
    assert report["missing_in_sqlite"] == []
    assert report["missing_in_jsonl"] == []
    assert db_ids(database) == {"a", "b"}
    with sqlite3.connect(str(database)) as con:
        meta = dict(con.execute("SELECT key,value FROM meta"))
    assert meta["indexed_ids_sha256"] == index_integrity.ids_sha256({"a", "b"})


def test_actual_source_database_parity_overrides_forged_matching_meta_digest(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha"), record("b", "bravo")])
    reconcile_index(source, database)
    with sqlite3.connect(str(database)) as con:
        con.execute("UPDATE docs SET rec_id='c' WHERE rec_id='b'")
        con.execute(
            "UPDATE meta SET value=? WHERE key='indexed_ids_sha256'",
            (index_integrity.ids_sha256({"a", "c"}),),
        )
        con.commit()

    report = reconcile_index(source, database)

    assert report["mode"] == "full_rebuild"
    assert report["reason"] == "id_set_mismatch"
    assert report["missing_in_sqlite"] == []
    assert report["missing_in_jsonl"] == []
    assert db_ids(database) == {"a", "b"}


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


def test_report_only_creates_only_shared_lock_files_not_database_or_staging(
    tmp_path,
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "missing.db"
    staging = tmp_path / "custom.staging"
    write_records(source, [record("a", "alpha")])
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
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "index.jsonl",
        "index.jsonl.source.lock",
        "missing.db.generation.lock",
    ]
    assert not database.exists()
    assert not staging.exists()
    assert not Path(str(database) + ".reconcile.lock").exists()


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


def test_report_only_rejects_orphan_nonempty_wal_when_database_is_missing(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "missing.db"
    write_records(source, [record("a", "alpha")])
    Path(str(database) + "-wal").write_bytes(b"orphan")

    with pytest.raises(
        index_integrity.IndexIntegrityError,
        match="non-empty WAL",
    ):
        reconcile_index(source, database, report_only=True)


def test_sha256_prefix_rejects_early_eof(tmp_path):
    source = tmp_path / "short.bin"
    source.write_bytes(b"abc")

    with pytest.raises(EOFError, match="wanted=4 read=3"):
        index_integrity.sha256_prefix(source, 4)


def test_source_revision_binds_identity_stat_and_content_hash(tmp_path):
    source = tmp_path / "index.jsonl"
    body = json.dumps(record("a", "alpha")) + "\n"
    source.write_text(body, encoding="utf-8")

    revision = index_integrity.source_revision(source)
    stat = source.stat()

    assert revision == {
        "dev": stat.st_dev,
        "ino": stat.st_ino,
        "size": len(body.encode("utf-8")),
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "prefix_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def test_report_only_retries_source_change_then_succeeds(tmp_path, monkeypatch):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    original = index_integrity._scan_source_ids_once
    calls = 0

    def append_once(path):
        nonlocal calls
        ids, revision = original(path)
        calls += 1
        if calls == 1:
            with source.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record("b", "bravo")) + "\n")
        return ids, revision

    monkeypatch.setattr(index_integrity, "_scan_source_ids_once", append_once)

    report = reconcile_index(source, database, report_only=True)

    assert calls == 2
    assert report["jsonl_unique_ids"] == 2
    assert report["source_size"] == source.stat().st_size
    assert report["missing_in_sqlite"] == ["b"]


def test_report_only_fails_closed_when_source_keeps_changing(tmp_path, monkeypatch):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)
    original = index_integrity._scan_source_ids_once
    calls = 0

    def rewrite_after_every_scan(path):
        nonlocal calls
        ids, revision = original(path)
        calls += 1
        replacement = "bravo" if calls % 2 else "alpha"
        write_records(source, [record("a", replacement)])
        return ids, revision

    monkeypatch.setattr(
        index_integrity,
        "_scan_source_ids_once",
        rewrite_after_every_scan,
    )

    with pytest.raises(
        index_integrity.SourceChangedError,
        match="source changed during stable scan after 3 attempts",
    ):
        reconcile_index(source, database, report_only=True)

    assert calls == 3


def test_full_rebuild_retries_source_change_and_binds_final_revision(
    tmp_path, monkeypatch
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    original = index_integrity._build_staging_once
    calls = 0

    def append_once(path, staging):
        nonlocal calls
        result = original(path, staging)
        calls += 1
        if calls == 1:
            with source.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record("b", "bravo")) + "\n")
        return result

    monkeypatch.setattr(index_integrity, "_build_staging_once", append_once)

    report = reconcile_index(source, database)

    assert calls == 2
    assert report["jsonl_unique_ids"] == 2
    assert db_ids(database) == {"a", "b"}
    revision = index_integrity.source_revision(source)
    with sqlite3.connect(str(database)) as con:
        meta = dict(con.execute("SELECT key,value FROM meta"))
    assert int(meta["source_dev"]) == revision["dev"]
    assert int(meta["source_ino"]) == revision["ino"]
    assert int(meta["last_size"]) == revision["size"]
    assert int(meta["source_mtime_ns"]) == revision["mtime_ns"]
    assert meta["prefix_sha256"] == revision["content_sha256"]


def test_full_rebuild_fails_closed_when_source_never_stabilizes(
    tmp_path, monkeypatch
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("original", "alpha")])
    reconcile_index(source, database)
    before = database.read_bytes()
    write_records(source, [record("replacement", "alpha")])
    original = index_integrity._build_staging_once
    calls = 0

    def rewrite_after_every_build(path, staging):
        nonlocal calls
        result = original(path, staging)
        calls += 1
        content = "bravo" if calls % 2 else "alpha"
        write_records(source, [record("replacement", content)])
        return result

    monkeypatch.setattr(
        index_integrity,
        "_build_staging_once",
        rewrite_after_every_build,
    )

    with pytest.raises(
        index_integrity.SourceChangedError,
        match="source changed during staging replacement after 3 attempts",
    ):
        reconcile_index(source, database)

    assert calls == 3
    assert database.read_bytes() == before
    assert db_ids(database) == {"original"}
    assert not Path(str(database) + ".staging").exists()


@pytest.mark.parametrize(
    "collision_name",
    [
        "source",
        "database",
        "wal",
        "shm",
        "journal",
        "lock",
    ],
)
def test_staging_path_rejects_source_database_sidecars_and_lock_without_deleting(
    tmp_path, collision_name
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    paths = {
        "source": source,
        "database": database,
        "wal": Path(str(database) + "-wal"),
        "shm": Path(str(database) + "-shm"),
        "journal": Path(str(database) + "-journal"),
        "lock": Path(str(database) + ".reconcile.lock"),
    }
    target = paths[collision_name]
    before = source.read_bytes()

    with pytest.raises(ValueError, match="unsafe staging path"):
        reconcile_index(source, database, staging_path=target)

    assert source.read_bytes() == before
    if collision_name != "source":
        assert not target.exists()


def test_staging_path_rejects_outside_directory_symlink_and_special_file(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    source = root / "index.jsonl"
    database = root / "search_index.db"
    write_records(source, [record("a", "alpha")])
    outside = tmp_path / "outside.staging"
    directory = root / "directory.staging"
    directory.mkdir()
    target = root / "target.db"
    target.write_bytes(b"keep")
    symlink = root / "symlink.staging"
    symlink.symlink_to(target)
    fifo = root / "fifo.staging"
    os.mkfifo(fifo)

    for staging in (outside, directory, symlink, fifo):
        with pytest.raises(ValueError, match="unsafe staging path"):
            reconcile_index(source, database, staging_path=staging)

    assert target.read_bytes() == b"keep"
    assert symlink.is_symlink()
    assert directory.is_dir()
    assert fifo.exists()


def test_staging_path_rejects_hardlink_samefile_without_unlinking_source(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    staging = tmp_path / "hardlink.staging"
    write_records(source, [record("a", "alpha")])
    os.link(source, staging)
    before = source.read_bytes()

    with pytest.raises(ValueError, match="unsafe staging path"):
        reconcile_index(source, database, staging_path=staging)

    assert source.read_bytes() == before
    assert staging.read_bytes() == before
    assert os.path.samefile(source, staging)


@pytest.mark.parametrize("artifact_suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize(
    "protected_name",
    ["source", "database", "wal", "shm", "journal", "lock"],
)
def test_every_staging_artifact_is_checked_against_every_protected_path(
    tmp_path, artifact_suffix, protected_name
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("source", "keep")])
    protected = {
        "source": source,
        "database": database,
        "wal": Path(str(database) + "-wal"),
        "shm": Path(str(database) + "-shm"),
        "journal": Path(str(database) + "-journal"),
        "lock": Path(str(database) + ".reconcile.lock"),
    }
    for name, path in protected.items():
        if name != "source":
            path.write_bytes(("protected-" + name).encode("utf-8"))
    staging = tmp_path / "candidate.staging"
    artifact = Path(str(staging) + artifact_suffix)
    os.link(protected[protected_name], artifact)
    before = protected[protected_name].read_bytes()

    with pytest.raises(ValueError, match="unsafe staging path"):
        reconcile_index(source, database, staging_path=staging)

    assert protected[protected_name].read_bytes() == before
    assert artifact.exists()
    assert os.path.samefile(artifact, protected[protected_name])


def test_default_staging_wal_cannot_be_the_source(tmp_path):
    database = tmp_path / "search_index.db"
    source = Path(str(database) + ".staging-wal")
    write_records(source, [record("a", "must survive")])
    before = source.read_bytes()

    with pytest.raises(ValueError, match="unsafe staging path"):
        reconcile_index(source, database)

    assert source.read_bytes() == before
    assert not database.exists()


@pytest.mark.parametrize("collision", ["same", "symlink", "hardlink"])
def test_source_and_database_collision_is_rejected_before_any_write(
    tmp_path, collision
):
    source = tmp_path / "index.jsonl"
    write_records(source, [record("a", "must survive")])
    if collision == "same":
        database = source
    elif collision == "symlink":
        database = tmp_path / "database-link"
        database.symlink_to(source)
    else:
        database = tmp_path / "database-hardlink"
        os.link(source, database)
    before = source.read_bytes()
    existing = sorted(path.name for path in tmp_path.iterdir())

    with pytest.raises(ValueError, match="source and database"):
        reconcile_index(source, database)

    assert source.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == existing


def test_invalid_staging_is_rejected_before_database_parent_mkdir(tmp_path):
    source = tmp_path / "index.jsonl"
    write_records(source, [record("a", "alpha")])
    database = tmp_path / "must-not-exist" / "search_index.db"
    staging = tmp_path / "outside.staging"

    with pytest.raises(ValueError, match="unsafe staging path"):
        reconcile_index(source, database, staging_path=staging)

    assert not database.parent.exists()


@pytest.mark.parametrize("mutation_timing", ["entry", "return"])
def test_source_change_at_replace_boundary_retries_to_exact_parity(
    tmp_path, monkeypatch, mutation_timing
):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    original = index_integrity._replace_database
    calls = 0

    def mutate_around_replace(staging, target):
        nonlocal calls
        calls += 1
        if calls == 1 and mutation_timing == "entry":
            write_records(source, [record("b", "bravo")])
        result = original(staging, target)
        if calls == 1 and mutation_timing == "return":
            write_records(source, [record("b", "bravo")])
        return result

    monkeypatch.setattr(
        index_integrity,
        "_replace_database",
        mutate_around_replace,
    )

    report = reconcile_index(source, database)

    assert calls == 2
    assert report["jsonl_unique_ids"] == 1
    assert report["sqlite_ids"] == 1
    assert report["missing_in_sqlite"] == []
    assert report["missing_in_jsonl"] == []
    assert db_ids(database) == {"b"}


def test_source_change_after_every_replace_fails_closed(tmp_path, monkeypatch):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    original = index_integrity._replace_database
    calls = 0

    def mutate_after_every_replace(staging, target):
        nonlocal calls
        result = original(staging, target)
        calls += 1
        rec_id = "b" if calls % 2 else "a"
        text = "bravo" if calls % 2 else "alpha"
        write_records(source, [record(rec_id, text)])
        return result

    monkeypatch.setattr(
        index_integrity,
        "_replace_database",
        mutate_after_every_replace,
    )

    with pytest.raises(
        index_integrity.SourceChangedError,
        match="source changed after staging replace after 3 attempts",
    ):
        reconcile_index(source, database)

    assert calls == 3


def test_report_only_cli_executes_real_arguments_without_creating_staging(tmp_path):
    source = tmp_path / "index.jsonl"
    database = tmp_path / "missing.db"
    staging = tmp_path / "must-not-exist.staging"
    write_records(source, [record("a", "alpha")])
    command = [
        sys.executable,
        str(Path(index_integrity.__file__).resolve()),
        "--source",
        str(source),
        "--database",
        str(database),
        "--report-only",
        "--staging-path",
        str(staging),
    ]

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["mode"] == "report_only"
    assert report["jsonl_unique_ids"] == 1
    assert report["sqlite_ids"] == 0
    assert report["missing_in_sqlite_count"] == 1
    assert not database.exists()
    assert not staging.exists()


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
        "status": "degraded",
        "check_status": "ok",
        "integrity_status": "degraded",
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


def test_fact_layer_audit_marks_in_sync_search_index_healthy(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(source, [record("a", "alpha")])
    reconcile_index(source, database)

    report = integrity_audit.audit(daily, source, database)
    markdown = integrity_audit.render_markdown(report)

    assert report["search_index"]["check_status"] == "ok"
    assert report["search_index"]["integrity_status"] == "healthy"
    assert report["search_index"]["status"] == "healthy"
    assert "Integrity status: healthy" in markdown
