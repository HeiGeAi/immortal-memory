from __future__ import annotations

import importlib
import gzip
import json
import os
import fcntl
import sqlite3
from pathlib import Path

import pytest


def migration():
    return importlib.import_module("notes_migration")


def note(record_id: str, content: str, day: str = "2026-07-19") -> dict:
    return {
        "id": record_id,
        "timestamp": f"{day}T12:00:00+00:00",
        "source": "obsidian-note",
        "content": content,
    }


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def rows(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def write_gzip_rows(path: Path, payload: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in payload:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def gzip_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_identical_duplicates_compact_and_non_notes_are_preserved(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    duplicate = note("note-1", "same")
    other = {"id": "other-1", "source": "web", "content": "preserve"}
    write_rows(vault / "index.jsonl", [other, duplicate, duplicate])
    write_rows(vault / "daily" / "2026-07-19.jsonl", [other, duplicate, duplicate])

    result = module.migrate_notes(vault)

    assert result["status"] == "ok"
    assert result["duplicates_compacted"] == 2
    assert rows(vault / "index.jsonl").count(other) == 1
    assert [row["id"] for row in rows(vault / "index.jsonl")].count("note-1") == 1
    assert [row["id"] for row in rows(vault / "daily" / "2026-07-19.jsonl")].count("note-1") == 1
    assert result["index_rebuild_required"] is True


def test_same_id_different_payload_fails_closed(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("note-1", "index")])
    write_rows(vault / "daily" / "2026-07-19.jsonl", [note("note-1", "daily")])
    before_index = (vault / "index.jsonl").read_bytes()
    before_daily = (vault / "daily" / "2026-07-19.jsonl").read_bytes()

    result = module.migrate_notes(vault)

    assert result["status"] == "error"
    assert result["error_code"] == "notes_migration_conflict"
    assert (vault / "index.jsonl").read_bytes() == before_index
    assert (vault / "daily" / "2026-07-19.jsonl").read_bytes() == before_daily


def test_daily_only_and_index_only_orphans_reconcile_without_source(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("index-only", "v2")])
    write_rows(
        vault / "daily" / "2026-07-18.jsonl",
        [note("daily-only", "v1", "2026-07-18")],
    )

    result = module.migrate_notes(vault)

    index_ids = {row["id"] for row in rows(vault / "index.jsonl")}
    daily_ids = {
        row["id"]
        for path in (vault / "daily").glob("*.jsonl")
        for row in rows(path)
    }
    assert result["status"] == "ok"
    assert index_ids == {"index-only", "daily-only"}
    assert daily_ids == {"index-only", "daily-only"}


def test_file_limit_checkpoint_resumes_without_changing_production_early(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])
    write_rows(vault / "daily" / "2026-07-19.jsonl", [note("one", "one")])
    before = (vault / "index.jsonl").read_bytes()

    partial = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(max_files=1, max_bytes=10_000, max_seconds=30),
    )

    assert partial["status"] == "partial"
    assert partial["error_code"] == "migration_limit_reached"
    assert (vault / "index.jsonl").read_bytes() == before
    assert (vault / "notes" / "migration" / "catalog.sqlite3").is_file()
    assert (vault / "notes" / "migration" / "checkpoint.json").is_file()

    completed = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(max_files=10, max_bytes=10_000, max_seconds=30),
    )
    assert completed["status"] == "ok"
    assert completed["checkpoint_resumed"] is True


@pytest.mark.parametrize(
    "limits",
    [
        lambda module: module.MigrationLimits(max_files=10, max_bytes=10, max_seconds=30),
        lambda module: module.MigrationLimits(max_files=10, max_bytes=10_000, max_seconds=0.0),
    ],
)
def test_byte_and_time_limits_are_authoritative(tmp_path, limits):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "content larger than ten")])

    result = module.migrate_notes(vault, limits=limits(module))

    assert result["status"] == "partial"
    assert result["error_code"] == "migration_limit_reached"
    assert result["production_changed"] is False


def test_interruption_after_staging_verification_keeps_production_unchanged(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one"), note("one", "one")])
    before = (vault / "index.jsonl").read_bytes()

    def interrupt(stage):
        if stage == "staging_verified":
            raise RuntimeError("injected")

    result = module.migrate_notes(vault, boundary=interrupt)

    assert result["status"] == "error"
    assert result["error_stage"] == "staging_verified"
    assert result["production_changed"] is False
    assert (vault / "index.jsonl").read_bytes() == before

    resumed = module.migrate_notes(vault)
    assert resumed["status"] == "ok"


def test_completed_manifest_marks_search_index_rebuild_required(tmp_path):
    module = migration()
    transactions = importlib.import_module("notes_transactions")
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])

    result = module.migrate_notes(vault)
    manifest = json.loads(
        transactions.manifest_path(vault).read_text(encoding="utf-8")
    )

    assert result["status"] == "ok"
    assert result["index_rebuild_required"] is True
    assert manifest["migration_status"] == "complete"
    assert manifest["migration_version"] == module.MIGRATION_VERSION
    assert manifest["index_rebuild_required"] is True


def test_staging_preserves_first_fact_relative_order(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    rows_before = [
        {"id": "other-a", "source": "web", "content": "a"},
        note("note-b", "b"),
        {"id": "other-b", "source": "web", "content": "b"},
        note("note-a", "a"),
        note("note-b", "b"),
    ]
    write_rows(vault / "index.jsonl", rows_before)

    result = module.migrate_notes(vault)

    assert result["status"] == "ok"
    assert [row["id"] for row in rows(vault / "index.jsonl")] == [
        "other-a",
        "note-b",
        "other-b",
        "note-a",
    ]


def test_migrate_then_sync_same_source_adds_zero_facts(tmp_path):
    module = migration()
    ingestion = importlib.import_module("notes_ingestion")
    vault = tmp_path / "vault"
    obsidian = tmp_path / "obsidian"
    source = obsidian / "笔记" / "one.md"
    source.parent.mkdir(parents=True)
    source.write_text("# One\n\nStable.", encoding="utf-8")
    candidate = ingestion.NoteCandidate(
        path=Path("one.md"),
        relative_path="one.md",
        text=source.read_text(encoding="utf-8"),
        size=source.stat().st_size,
        mtime=source.stat().st_mtime,
    )
    legacy = ingestion._record(candidate)
    write_rows(vault / "index.jsonl", [legacy])

    migrated = module.migrate_notes(vault)
    synced = ingestion.ingest_notes(vault, obsidian, dry_run=False)

    assert migrated["status"] == "ok"
    assert synced["status"] == "ok"
    assert synced["totals"]["ingested_this_run"] == 0
    assert len(rows(vault / "index.jsonl")) == 1


def test_publication_crash_rolls_forward_without_rescanning_changed_sources(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])
    write_rows(vault / "daily" / "2026-07-19.jsonl", [note("one", "one")])
    calls = {"count": 0}

    def interrupt(stage):
        if stage.startswith("publication_replaced:"):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("injected")

    failed = module.migrate_notes(vault, boundary=interrupt)
    resumed = module.migrate_notes(vault)

    assert failed["status"] == "error"
    assert failed["error_stage"] == "publication"
    assert resumed["status"] == "ok"
    assert {row["id"] for row in rows(vault / "index.jsonl")} == {"one"}
    assert {row["id"] for row in rows(vault / "daily" / "2026-07-19.jsonl")} == {"one"}


def test_active_source_signature_change_fails_closed_on_resume(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(
        vault / "index.jsonl",
        [note("one", "content large enough to checkpoint")],
    )
    partial = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(max_files=10, max_bytes=20, max_seconds=30),
    )
    with (vault / "index.jsonl").open("ab") as handle:
        handle.write(b" ")

    resumed = module.migrate_notes(vault)

    assert partial["status"] == "partial"
    assert resumed["status"] == "error"
    assert resumed["error_code"] == "notes_migration_reset_required"
    assert resumed["run_id"]


def test_migration_hashes_large_staging_without_read_bytes(tmp_path, monkeypatch):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])

    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: (_ for _ in ()).throw(AssertionError("unbounded read")),
    )
    result = module.migrate_notes(vault)

    assert result["status"] == "ok"


def test_publication_journal_rejects_paths_outside_vault(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    outside = tmp_path / "outside.jsonl"
    outside.write_text("do not replace\n", encoding="utf-8")
    migration_root = vault / "notes" / "migration"
    migration_root.mkdir(parents=True)
    staging = migration_root / "staging" / "payload.jsonl"
    staging.parent.mkdir()
    staging.write_text("attacker\n", encoding="utf-8")
    digest, length = module._hash_file(staging)
    publication = {
        "schema_version": 1,
        "stage": "prepared",
        "entries": [
            {
                "relative": "index.jsonl",
                "target": str(outside),
                "staging": str(staging),
                "backup": str(migration_root / "backups" / "index.jsonl"),
                "sha256": digest,
                "length": length,
                "published": False,
            }
        ],
    }
    (migration_root / "publication.json").write_text(
        json.dumps(publication),
        encoding="utf-8",
    )

    result = module.migrate_notes(vault)

    assert result["status"] == "error"
    assert result["error_code"] == "migration_publication_failed"
    assert outside.read_text(encoding="utf-8") == "do not replace\n"


def test_source_change_after_staging_is_detected_before_first_replace(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    original = note("one", "one")
    late = {"id": "late", "source": "web", "content": "must survive"}
    write_rows(vault / "index.jsonl", [original, original])

    def append_after_staging(stage):
        if stage == "staging_verified":
            with (vault / "index.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(late, ensure_ascii=False) + "\n")

    result = module.migrate_notes(vault, boundary=append_after_staging)

    assert result["status"] == "error"
    assert result["error_code"] == "migration_publication_failed"
    assert result["production_changed"] is False
    assert rows(vault / "index.jsonl") == [original, original, late]


def test_reset_catalog_removes_stale_sqlite_sidecars(tmp_path):
    module = migration()
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"stale database")
    Path(f"{catalog}-wal").write_bytes(b"stale wal")
    Path(f"{catalog}-shm").write_bytes(b"stale shm")

    connection = module._connect_catalog(catalog, reset=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM facts").fetchone() == (0,)
    finally:
        connection.close()

    for sidecar, stale in (
        (Path(f"{catalog}-wal"), b"stale wal"),
        (Path(f"{catalog}-shm"), b"stale shm"),
    ):
        assert not sidecar.exists() or sidecar.read_bytes() != stale


def test_manifest_source_limit_fails_before_publication(tmp_path, monkeypatch):
    module = migration()
    vault = tmp_path / "vault"
    first = note("one", "one")
    first["metadata"] = {"relative_path": "one.md"}
    second = note("two", "two")
    second["metadata"] = {"relative_path": "two.md"}
    write_rows(vault / "index.jsonl", [first, second])
    before = (vault / "index.jsonl").read_bytes()
    monkeypatch.setattr(module, "MAX_MANIFEST_SOURCES", 1)

    result = module.migrate_notes(vault)

    assert result["status"] == "error"
    assert result["error_code"] == "notes_migration_conflict"
    assert result["error_stage"] == "manifest_capacity"
    assert result["production_changed"] is False
    assert (vault / "index.jsonl").read_bytes() == before


def test_success_cleans_completed_publication_journal_and_staging(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])

    result = module.migrate_notes(vault)

    assert result["status"] == "ok"
    migration_root = vault / "notes" / "migration"
    assert not (migration_root / "publication.json").exists()
    assert not (migration_root / "staging").exists()


def test_gzip_daily_duplicates_compact_and_preserve_non_notes(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    duplicate = note("note-gz", "same")
    other = {"id": "other-gz", "source": "web", "content": "preserve"}
    archive = vault / "daily" / "2026-07-19.jsonl.gz"
    write_gzip_rows(archive, [other, duplicate, duplicate])
    write_rows(vault / "index.jsonl", [duplicate])

    result = module.migrate_notes(vault)

    assert result["status"] == "ok"
    assert gzip_rows(archive) == [other, duplicate]
    assert [row["id"] for row in rows(vault / "index.jsonl")] == ["note-gz"]


def test_gzip_and_index_payload_conflict_fails_closed(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    archive = vault / "daily" / "2026-07-19.jsonl.gz"
    write_gzip_rows(archive, [note("note-gz", "daily")])
    write_rows(vault / "index.jsonl", [note("note-gz", "index")])
    before_archive = archive.read_bytes()
    before_index = (vault / "index.jsonl").read_bytes()

    result = module.migrate_notes(vault)

    assert result["status"] == "error"
    assert result["error_code"] == "notes_migration_conflict"
    assert archive.read_bytes() == before_archive
    assert (vault / "index.jsonl").read_bytes() == before_index


def test_gzip_checkpoint_resumes_with_expanded_byte_limit(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    archive = vault / "daily" / "2026-07-19.jsonl.gz"
    payload = note("note-gz", "content larger than the first expanded budget")
    write_gzip_rows(archive, [payload])
    write_rows(vault / "index.jsonl", [payload])

    partial = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(
            max_files=10,
            max_bytes=20,
            max_seconds=30,
        ),
    )
    completed = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(
            max_files=10,
            max_bytes=100_000,
            max_seconds=30,
        ),
    )

    assert partial["status"] == "partial"
    assert partial["bytes_processed"] == 20
    assert completed["status"] == "ok"
    assert completed["checkpoint_resumed"] is True


def test_corrupt_gzip_fails_closed_without_publication(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    archive = vault / "daily" / "2026-07-19.jsonl.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not gzip")
    write_rows(vault / "index.jsonl", [note("one", "one")])
    before_archive = archive.read_bytes()
    before_index = (vault / "index.jsonl").read_bytes()

    result = module.migrate_notes(vault)

    assert result["status"] == "error"
    assert result["error_code"] == "notes_migration_conflict"
    assert archive.read_bytes() == before_archive
    assert (vault / "index.jsonl").read_bytes() == before_index


def test_catalog_commit_before_checkpoint_is_idempotent_on_resume(
    tmp_path,
    monkeypatch,
):
    module = migration()
    vault = tmp_path / "vault"
    duplicate = note("one", "one")
    write_rows(vault / "index.jsonl", [duplicate, duplicate])
    calls = {"count": 0}

    def interrupt_once():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("injected")

    monkeypatch.setattr(
        module,
        "after_catalog_commit_before_checkpoint",
        interrupt_once,
    )
    failed = module.migrate_notes(vault)
    monkeypatch.setattr(
        module,
        "after_catalog_commit_before_checkpoint",
        lambda: None,
    )
    resumed = module.migrate_notes(vault)

    assert failed["status"] == "error"
    assert failed["error_stage"] == "catalog_checkpoint"
    assert resumed["status"] == "ok"
    assert resumed["duplicates_compacted"] == 1
    assert [row["id"] for row in rows(vault / "index.jsonl")] == ["one"]


def test_resume_rejects_source_plan_addition(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])
    partial = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(
            max_files=1,
            max_bytes=20,
            max_seconds=30,
        ),
    )
    write_rows(
        vault / "daily" / "2026-07-18.jsonl",
        [note("late", "late", "2026-07-18")],
    )

    resumed = module.migrate_notes(vault)

    assert partial["status"] == "partial"
    assert resumed["status"] == "error"
    assert resumed["error_code"] == "notes_migration_reset_required"
    assert resumed["run_id"]


def test_concurrent_migration_lock_fails_closed(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])
    lock = vault / "notes" / "migration" / "migration.lock"
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = module.migrate_notes(vault)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result == {
        "status": "error",
        "error_code": "notes_migration_busy",
        "production_changed": False,
    }


def test_completed_migration_is_idempotent_without_rescanning(tmp_path, monkeypatch):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])
    first = module.migrate_notes(vault)
    monkeypatch.setattr(
        module,
        "_source_files",
        lambda _vault: (_ for _ in ()).throw(AssertionError("rescan")),
    )

    second = module.migrate_notes(vault)

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert second["already_migrated"] is True
    assert second["production_changed"] is False


def test_crash_before_first_replace_revalidates_snapshot_on_recovery(
    tmp_path,
    monkeypatch,
):
    module = migration()
    vault = tmp_path / "vault"
    original = note("one", "one")
    late = {"id": "late", "source": "web", "content": "must survive"}
    write_rows(vault / "index.jsonl", [original, original])
    real_replace = module.os.replace
    interrupted = {"done": False}

    def crash_before_fact_replace(source, target, *args, **kwargs):
        if (
            not interrupted["done"]
            and not kwargs
            and Path(source).parent.name == "staging"
        ):
            interrupted["done"] = True
            raise RuntimeError("injected")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", crash_before_fact_replace)
    failed = module.migrate_notes(vault)
    monkeypatch.setattr(module.os, "replace", real_replace)
    with (vault / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(late, ensure_ascii=False) + "\n")

    resumed = module.migrate_notes(vault)

    assert failed["status"] == "error"
    assert resumed["status"] == "error"
    assert resumed["error_code"] == "migration_publication_failed"
    assert rows(vault / "index.jsonl") == [original, original, late]


def test_pending_publication_blocks_note_transaction_writer(tmp_path):
    module = migration()
    transactions = importlib.import_module("notes_transactions")
    vault = tmp_path / "vault"
    original = note("one", "one")
    write_rows(vault / "index.jsonl", [original, original])

    def interrupt(stage):
        if stage.startswith("publication_replaced:"):
            raise RuntimeError("injected")

    failed = module.migrate_notes(vault, boundary=interrupt)

    with pytest.raises(transactions.MigrationInProgress):
        transactions.commit_record(vault, note("late", "late"))

    assert failed["status"] == "error"
    assert (vault / "notes" / "migration" / "publication.json").is_file()


def test_pending_publication_blocks_non_notes_daily_writer(tmp_path):
    module = migration()
    web_capture = importlib.import_module("web_capture")
    maintenance = importlib.import_module("maintenance_gate")
    vault = tmp_path / "vault"
    original = note("one", "one")
    write_rows(vault / "index.jsonl", [original, original])

    def interrupt(stage):
        if stage.startswith("publication_replaced:"):
            raise RuntimeError("injected")

    failed = module.migrate_notes(vault, boundary=interrupt)
    late = {
        "id": "late-web",
        "timestamp": "2026-07-19T15:00:00+00:00",
        "source": "web",
        "content": "must be blocked",
    }

    with pytest.raises(maintenance.MaintenanceInProgress):
        web_capture.append_records(vault, [late])

    assert failed["status"] == "error"
    assert all(
        row["id"] != "late-web"
        for path in (vault / "daily").glob("*.jsonl")
        for row in rows(path)
    )
    assert all(row["id"] != "late-web" for row in rows(vault / "index.jsonl"))


def test_pending_publication_blocks_direct_index_writer(tmp_path):
    module = migration()
    index_writer = importlib.import_module("index_writer")
    maintenance = importlib.import_module("maintenance_gate")
    vault = tmp_path / "vault"
    original = note("one", "one")
    write_rows(vault / "index.jsonl", [original, original])

    def interrupt(stage):
        if stage.startswith("publication_replaced:"):
            raise RuntimeError("injected")

    failed = module.migrate_notes(vault, boundary=interrupt)
    before = (vault / "index.jsonl").read_bytes()

    with pytest.raises(maintenance.MaintenanceInProgress):
        index_writer.append_jsonl_records(
            vault / "index.jsonl",
            [
                {
                    "id": "late-direct",
                    "source": "web",
                    "content": "must be blocked",
                }
            ],
        )

    assert failed["status"] == "error"
    assert (vault / "index.jsonl").read_bytes() == before


def test_catalog_stores_non_note_payload_only_once(tmp_path):
    module = migration()
    catalog = tmp_path / "catalog.sqlite3"

    connection = module._connect_catalog(catalog, reset=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "non_notes" not in tables
    finally:
        connection.close()


def test_gzip_scan_never_seeks_and_reports_physical_work(tmp_path, monkeypatch):
    module = migration()
    vault = tmp_path / "vault"
    payload = note("gzip-linear", "linear")
    write_gzip_rows(vault / "daily" / "2026-07-19.jsonl.gz", [payload])
    write_rows(vault / "index.jsonl", [payload])

    monkeypatch.setattr(
        module.gzip.GzipFile,
        "seek",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("gzip seek is forbidden")
        ),
    )
    result = module.migrate_notes(vault)

    assert result["status"] == "ok"
    assert result["compressed_bytes_processed"] > 0
    assert result["expanded_bytes_processed"] > 0


def test_capacity_gate_fails_closed_with_exact_fields(tmp_path, monkeypatch):
    module = migration()
    vault = tmp_path / "vault"
    original = note("one", "one")
    write_rows(vault / "index.jsonl", [original, original])
    before = (vault / "index.jsonl").read_bytes()

    monkeypatch.setattr(module, "_available_bytes", lambda _path: 1)
    result = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(
            max_files=10,
            max_bytes=10_000,
            max_seconds=30,
            reserve_bytes=2,
        ),
    )

    assert result["status"] == "error"
    assert result["error_code"] == "migration_insufficient_scratch_space"
    assert result["required_bytes"] > result["available_bytes"]
    assert result["production_changed"] is False
    assert (vault / "index.jsonl").read_bytes() == before


def test_source_plan_conflict_returns_safe_reset_contract(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])
    partial = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(max_files=1, max_bytes=10, max_seconds=30),
    )
    write_rows(vault / "daily" / "2026-07-18.jsonl", [note("late", "late", "2026-07-18")])

    conflict = module.migrate_notes(vault)

    assert partial["status"] == "partial"
    assert conflict["error_code"] == "notes_migration_reset_required"
    assert conflict["run_id"]
    assert "notes-migrate-reset" in conflict["reset_command"]


def test_scratch_reset_requires_matching_run_id_and_preserves_production(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])
    partial = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(max_files=1, max_bytes=10, max_seconds=30),
    )
    checkpoint = json.loads(
        (vault / "notes" / "migration" / "checkpoint.json").read_text()
    )
    before = (vault / "index.jsonl").read_bytes()

    rejected = module.reset_migration_scratch(vault, run_id="wrong")
    reset = module.reset_migration_scratch(vault, run_id=checkpoint["run_id"])

    assert partial["status"] == "partial"
    assert rejected["error_code"] == "notes_migration_reset_run_id_mismatch"
    assert reset["status"] == "ok"
    assert reset["production_changed"] is False
    assert (vault / "index.jsonl").read_bytes() == before
    assert not (vault / "notes" / "migration" / "checkpoint.json").exists()
    assert not (vault / "notes" / "migration" / "catalog.sqlite3").exists()


def test_scratch_reset_refuses_pending_publication(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    root = vault / "notes" / "migration"
    root.mkdir(parents=True)
    (root / "checkpoint.json").write_text(
        json.dumps({"migration_version": module.MIGRATION_VERSION, "run_id": "run"}),
        encoding="utf-8",
    )
    (root / "publication.json").write_text(
        json.dumps({"schema_version": 1, "stage": "prepared", "entries": []}),
        encoding="utf-8",
    )

    result = module.reset_migration_scratch(vault, run_id="run")

    assert result["error_code"] == "notes_migration_reset_unsafe_publication_exists"
    assert (root / "checkpoint.json").exists()


def test_legacy_scratch_fails_closed_and_requires_reset(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    write_rows(vault / "index.jsonl", [note("one", "one")])
    checkpoint = vault / "notes" / "migration" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps({"migration_version": module.MIGRATION_VERSION - 1}),
        encoding="utf-8",
    )

    result = module.migrate_notes(vault)

    assert result["status"] == "error"
    assert result["error_code"] == "notes_migration_legacy_scratch"
    assert result["production_changed"] is False
    assert "notes-migrate-reset" in result["reset_command"]
    reset = module.reset_migration_scratch(vault, run_id=result["run_id"])
    assert reset["status"] == "ok"


def test_same_size_restored_mtime_source_change_is_detected(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    source = vault / "index.jsonl"
    write_rows(source, [note("one", "aaaa")])
    partial = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(max_files=1, max_bytes=10, max_seconds=30),
    )
    original_stat = source.stat()
    data = source.read_bytes()
    source.write_bytes(data.replace(b"aaaa", b"bbbb"))
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    result = module.migrate_notes(vault)

    assert partial["status"] == "partial"
    assert result["error_code"] == "notes_migration_reset_required"


def test_missing_rows_follow_first_seen_file_and_offset_order(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    first = note("z-first", "first")
    second = note("a-second", "second")
    write_rows(vault / "daily" / "2026-07-19.jsonl", [first, second])

    result = module.migrate_notes(vault)

    assert result["status"] == "ok"
    assert [row["id"] for row in rows(vault / "index.jsonl")] == [
        "z-first",
        "a-second",
    ]


def test_missing_rows_preserve_first_seen_order_across_gzip_and_plain(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    first = note("z-gzip-first", "first", "2026-07-18")
    second = note("a-plain-second", "second", "2026-07-19")
    write_gzip_rows(vault / "daily" / "2026-07-18.jsonl.gz", [first])
    write_rows(vault / "daily" / "2026-07-19.jsonl", [second])

    result = module.migrate_notes(vault)

    assert result["status"] == "ok"
    assert [row["id"] for row in rows(vault / "index.jsonl")] == [
        "z-gzip-first",
        "a-plain-second",
    ]


def test_gzip_resume_does_not_reinflate_completed_sources(tmp_path, monkeypatch):
    module = migration()
    vault = tmp_path / "vault"
    first = note("first", "first", "2026-07-18")
    second = note("second", "second", "2026-07-19")
    write_gzip_rows(vault / "daily" / "2026-07-18.jsonl.gz", [first])
    write_gzip_rows(vault / "daily" / "2026-07-19.jsonl.gz", [second])
    calls: list[str] = []
    real_inflate = module._inflate_gzip_source

    def count_inflate(_vault, relative, *args, **kwargs):
        calls.append(relative)
        return real_inflate(_vault, relative, *args, **kwargs)

    monkeypatch.setattr(module, "_inflate_gzip_source", count_inflate)
    partial = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(
            max_files=1,
            max_bytes=100_000,
            max_seconds=30,
        ),
    )
    completed = module.migrate_notes(vault)

    assert partial["status"] == "partial"
    assert completed["status"] == "ok"
    assert calls.count("daily/2026-07-18.jsonl.gz") == 1
    assert calls.count("daily/2026-07-19.jsonl.gz") == 1
    assert partial["compressed_bytes_processed"] > 0
    assert partial["expanded_bytes_processed"] > 0


def test_gzip_expansion_limit_fails_closed(tmp_path):
    module = migration()
    vault = tmp_path / "vault"
    archive = vault / "daily" / "2026-07-19.jsonl.gz"
    payload = note("bomb", "x" * 100_000)
    write_gzip_rows(archive, [payload])
    before = archive.read_bytes()

    result = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(
            max_files=10,
            max_bytes=200_000,
            max_seconds=30,
            max_gzip_expanded_bytes=1024,
        ),
    )

    assert result["status"] == "error"
    assert result["error_code"] == "migration_gzip_expansion_limit"
    assert result["production_changed"] is False
    assert archive.read_bytes() == before


def test_publication_capacity_gate_fails_before_replace(tmp_path, monkeypatch):
    module = migration()
    vault = tmp_path / "vault"
    duplicate = note("one", "one")
    write_rows(vault / "index.jsonl", [duplicate, duplicate])
    before = (vault / "index.jsonl").read_bytes()
    real_require_capacity = module._require_capacity

    def fail_publication(path, *, phase, additional_bytes, reserve_bytes):
        if phase == "publication":
            raise module.MigrationCapacity(
                phase=phase,
                required_bytes=additional_bytes + reserve_bytes,
                available_bytes=1,
                reserve_bytes=reserve_bytes,
            )
        return real_require_capacity(
            path,
            phase=phase,
            additional_bytes=additional_bytes,
            reserve_bytes=reserve_bytes,
        )

    monkeypatch.setattr(module, "_require_capacity", fail_publication)

    result = module.migrate_notes(
        vault,
        limits=module.MigrationLimits(
            max_files=10,
            max_bytes=100_000,
            max_seconds=30,
            reserve_bytes=2,
        ),
    )

    assert result["error_code"] == "migration_insufficient_scratch_space"
    assert result["error_stage"] == "publication"
    assert result["production_changed"] is False
    assert (vault / "index.jsonl").read_bytes() == before
