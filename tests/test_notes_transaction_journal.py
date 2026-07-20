from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
from pathlib import Path

import pytest


def transactions():
    return importlib.import_module("notes_transactions")


def record() -> dict:
    return {
        "id": "obsidian-note-transaction-test",
        "timestamp": "2026-07-19T12:00:00+00:00",
        "source": "obsidian-note",
        "content": "# Transaction",
    }


@pytest.mark.parametrize(
    "crash_stage",
    ["prepared", "daily_committed", "index_committed", "manifest_committed"],
)
def test_crash_recovery_uses_declared_offsets_and_exact_bytes(tmp_path, crash_stage):
    module = transactions()
    vault = tmp_path / "vault"
    seen = []

    def crash(stage):
        seen.append(stage)
        if stage == crash_stage:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        module.commit_record(vault, record(), boundary=crash)

    journals = list((vault / "notes" / "transactions").glob("*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["daily_sha256"] == hashlib.sha256(
        module.serialize_record(record(), public=True)
    ).hexdigest()
    assert journal["index_sha256"] == hashlib.sha256(
        module.serialize_record(record(), public=False)
    ).hexdigest()

    recovered = module.recover_pending(vault)
    daily = vault / journal["daily_relpath"]
    index = vault / "index.jsonl"
    assert recovered["recovered"] == 1
    assert daily.read_bytes() == module.serialize_record(record(), public=True)
    assert index.read_bytes() == module.serialize_record(record(), public=False)
    assert not journals[0].exists()
    manifest = json.loads((vault / "notes" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stats"]["committed_transactions"] == 1


def test_offset_or_byte_mismatch_fails_closed_without_append(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"

    def crash(stage):
        if stage == "prepared":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        module.commit_record(vault, record(), boundary=crash)
    journal_path = next((vault / "notes" / "transactions").glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    daily = vault / journal["daily_relpath"]
    daily.parent.mkdir(parents=True, exist_ok=True)
    daily.write_bytes(b"different")
    index_before = (vault / "index.jsonl").read_bytes() if (vault / "index.jsonl").exists() else b""

    with pytest.raises(module.TransactionConflict):
        module.recover_pending(vault)

    assert daily.read_bytes() == b"different"
    assert ((vault / "index.jsonl").read_bytes() if (vault / "index.jsonl").exists() else b"") == index_before


def test_append_failure_reports_real_partial_progress(tmp_path, monkeypatch):
    module = transactions()
    target = tmp_path / "facts.jsonl"
    real_write = os.write
    calls = {"count": 0}

    def partial_then_fail(fd, payload):
        calls["count"] += 1
        if calls["count"] == 1:
            return real_write(fd, bytes(payload[:2]))
        raise OSError("injected")

    monkeypatch.setattr(module.os, "write", partial_then_fail)
    with pytest.raises(module.AppendFailure) as caught:
        module.append_exact(target, 0, b"abcdef")

    assert caught.value.result.bytes_written == 2
    assert caught.value.result.fsynced is False
    assert target.read_bytes() == b"ab"


@pytest.mark.parametrize(
    "timestamp",
    ["2026-02-30T12:00:00+00:00", "../escape", "2026-7-1T00:00:00Z"],
)
def test_daily_target_requires_strict_iso_date_inside_resolved_daily(tmp_path, timestamp):
    module = transactions()
    row = record()
    row["timestamp"] = timestamp

    with pytest.raises(module.InvalidDailyTarget):
        module.daily_target(tmp_path / "vault", row)


def test_new_vault_manifest_is_compatible_and_bounded(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"

    manifest = module.ensure_manifest(vault)

    assert manifest["schema_version"] == module.MANIFEST_SCHEMA_VERSION
    assert manifest["migration_status"] == "not_required"
    assert manifest["sources"] == {}
    assert (vault / "notes" / "manifest.json").is_file()


def test_manifest_creation_supports_completely_missing_home_ancestry(tmp_path):
    module = transactions()
    vault = tmp_path / "missing-home" / ".immortal"

    manifest = module.ensure_manifest(vault)

    assert manifest["migration_status"] == "not_required"
    assert (vault / "notes" / "manifest.json").is_file()


@pytest.mark.parametrize(
    "damaged_manifest",
    [
        {"schema_version": 2, "sources": {}},
        {
            "schema_version": 2,
            "migration_status": "complete",
            "sources": {},
            "pending_transactions": [],
            "applied_transactions": [],
            "last_successful_tx": None,
            "stats": {"committed_transactions": 0},
        },
    ],
)
def test_semantically_damaged_manifest_cannot_bypass_migration(
    tmp_path,
    damaged_manifest,
):
    module = transactions()
    vault = tmp_path / "vault"
    index = vault / "index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_bytes(module.serialize_record(record(), public=False))
    manifest = module.manifest_path(vault)
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(damaged_manifest), encoding="utf-8")
    before = index.read_bytes()
    candidate = record()
    candidate["id"] = "must-not-append"

    readiness = module.manifest_readiness(vault)
    with pytest.raises(module.MigrationRequired):
        module.commit_record(vault, candidate)

    assert readiness == {"ok": False, "error_code": "notes_migration_required"}
    assert index.read_bytes() == before


def test_legacy_fact_layer_requires_explicit_migration_without_full_scan(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.jsonl").write_bytes(b'{"id":"legacy"}\n')

    result = module.manifest_readiness(vault)

    assert result == {"ok": False, "error_code": "notes_migration_required"}


def test_recovery_uses_manifest_pending_ids_not_complete_journal_glob(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"
    module.ensure_manifest(vault)
    directory = vault / "notes" / "transactions"
    directory.mkdir(parents=True)
    for number in range(200):
        tx_id = f"{number:024x}"
        (directory / f"{tx_id}.json").write_text(
            json.dumps({"tx_id": tx_id, "stage": "complete"}),
            encoding="utf-8",
        )

    result = module.recover_pending(vault)

    assert result == {"recovered": 0, "cleaned": 200}
    assert list(directory.iterdir()) == []


def test_orphan_prepared_journal_is_recovered_after_registration_window_crash(
    tmp_path,
):
    module = transactions()
    vault = tmp_path / "vault"

    def crash(stage):
        if stage == "journal_persisted":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        module.commit_record(vault, record(), boundary=crash)
    manifest = json.loads(module.manifest_path(vault).read_text(encoding="utf-8"))
    assert manifest["pending_transactions"] == []
    assert len(list((vault / "notes" / "transactions").iterdir())) == 1

    recovered = module.recover_pending(vault)

    assert recovered == {"recovered": 1, "cleaned": 0}
    assert list((vault / "notes" / "transactions").iterdir()) == []


def test_complete_orphan_is_cleaned_after_pending_clear_window_crash(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"

    def crash(stage):
        if stage == "pending_cleared":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        module.commit_record(vault, record(), boundary=crash)
    index_before = (vault / "index.jsonl").read_bytes()
    assert len(list((vault / "notes" / "transactions").iterdir())) == 1

    recovered = module.recover_pending(vault)

    assert recovered == {"recovered": 0, "cleaned": 1}
    assert (vault / "index.jsonl").read_bytes() == index_before
    assert list((vault / "notes" / "transactions").iterdir()) == []


def test_manifest_parent_fsync_failure_is_typed(tmp_path, monkeypatch):
    module = transactions()
    real_fsync = module.os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected")
        return real_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_directory_fsync)

    with pytest.raises(module.JournalDurabilityError) as caught:
        module.ensure_manifest(tmp_path / "vault")

    assert caught.value.stage == "parent_fsync"


def test_normal_sync_does_not_glob_daily_or_scan_full_index(tmp_path, monkeypatch):
    ingestion = importlib.import_module("notes_ingestion")
    module = transactions()
    vault = tmp_path / "vault"
    obsidian = tmp_path / "obsidian"
    (obsidian / "笔记").mkdir(parents=True)
    (obsidian / "笔记" / "new.md").write_text("# New", encoding="utf-8")
    module.ensure_manifest(vault)
    (vault / "daily").mkdir(exist_ok=True)
    (vault / "daily" / "2000-01-01.jsonl").write_text(
        '{"id":"unrelated","source":"other"}\n',
        encoding="utf-8",
    )
    (vault / "index.jsonl").write_text(
        '{"id":"unrelated","source":"other"}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        ingestion,
        "_read_note_rows",
        lambda _path: (_ for _ in ()).throw(AssertionError("full scan")),
    )
    original_glob = Path.glob

    def reject_daily_glob(path, pattern):
        if path == vault / "daily":
            raise AssertionError("daily glob")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_daily_glob)
    result = ingestion.ingest_notes(vault, obsidian, dry_run=False)

    assert result["status"] == "ok"
    assert result["totals"]["ingested_this_run"] == 1


def test_growth_consumes_only_actual_remaining_read_budget(tmp_path, monkeypatch):
    ingestion = importlib.import_module("notes_ingestion")
    vault = tmp_path / "vault"
    obsidian = tmp_path / "obsidian"
    notes = obsidian / "笔记"
    notes.mkdir(parents=True)
    target = notes / "grow.md"
    target.write_bytes(b"abc")

    def grow(relative_path, _fd):
        if relative_path == "grow.md":
            with target.open("ab") as handle:
                handle.write(b"0123456789")

    monkeypatch.setattr(ingestion, "after_file_fstat", grow)
    result = ingestion.ingest_notes(
        vault,
        obsidian,
        dry_run=True,
        limits=ingestion.NoteIngestionLimits(
            max_files=10,
            max_file_bytes=100,
            max_total_bytes=5,
        ),
    )

    assert result["totals"]["processed_bytes"] == 5
    assert result["totals"]["accepted_bytes"] == 0
    assert result["skipped_by_reason"] == {"total_bytes_exceeded": 1}


@pytest.mark.parametrize("side", ["index", "daily"])
def test_transaction_append_rejects_symlink_target(tmp_path, side):
    module = transactions()
    vault = tmp_path / "vault"
    outside = tmp_path / "outside.jsonl"
    outside.write_text("private\n", encoding="utf-8")
    module.ensure_manifest(vault)
    if side == "index":
        (vault / "index.jsonl").symlink_to(outside)
    else:
        (vault / "daily").mkdir(parents=True)
        (vault / "daily" / "2026-07-19.jsonl").symlink_to(outside)

    with pytest.raises(module.TransactionConflict):
        module.commit_record(vault, record())

    assert outside.read_text(encoding="utf-8") == "private\n"


def test_pending_transaction_is_recovered_before_next_commit(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"
    first = record()
    first["id"] = "first"
    second = record()
    second["id"] = "second"
    second["content"] = "# Second"

    def interrupt(stage):
        if stage == "manifest_committed":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        module.commit_record(
            vault,
            first,
            boundary=interrupt,
            source_entry=("first.md", {"record_id": "first"}),
        )
    module.commit_record(
        vault,
        second,
        source_entry=("second.md", {"record_id": "second"}),
    )

    manifest = json.loads(module.manifest_path(vault).read_text(encoding="utf-8"))
    assert manifest["pending_transactions"] == []
    assert manifest["stats"]["committed_transactions"] == 2
    assert manifest["sources"]["first.md"]["last_tx_id"]
    assert manifest["sources"]["second.md"]["last_tx_id"]


def test_unscoped_transaction_replay_is_idempotent_after_journal_cleanup(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"

    first = module.commit_record(vault, record())
    replay = module.commit_record(vault, record())

    manifest = json.loads(module.manifest_path(vault).read_text(encoding="utf-8"))
    index_rows = [
        json.loads(line)
        for line in (vault / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert first.facts_committed is True
    assert replay.stage == "already_committed"
    assert replay.facts_committed is False
    assert [row["id"] for row in index_rows] == [record()["id"]]
    assert manifest["stats"]["committed_transactions"] == 1
    assert manifest["applied_transactions"] == [first.tx_id]


def test_manifest_rejects_symlinked_notes_directory(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (vault / "notes").symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.JournalDurabilityError):
        module.ensure_manifest(vault)

    assert list(outside.iterdir()) == []


def test_commit_rejects_symlinked_transactions_directory(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    outside.mkdir()
    module.ensure_manifest(vault)
    (vault / "notes" / "transactions").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(module.TransactionConflict):
        module.commit_record(vault, record())

    assert list(outside.iterdir()) == []


def test_commit_rejects_symlinked_journal_file(tmp_path):
    module = transactions()
    vault = tmp_path / "vault"
    outside = tmp_path / "outside.json"
    outside.write_text('{"private":true}\n', encoding="utf-8")
    module.ensure_manifest(vault)
    journal = module._prepared_journal(vault, record(), None)
    directory = module.transactions_dir(vault)
    directory.mkdir()
    (directory / f"{journal['tx_id']}.json").symlink_to(outside)

    with pytest.raises(module.TransactionConflict):
        module.commit_record(vault, record())

    assert outside.read_text(encoding="utf-8") == '{"private":true}\n'
