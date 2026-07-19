from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest


class LazyNotesIngestion:
    def __getattr__(self, name):
        path = Path(__file__).resolve().parents[1] / "core" / "notes_ingestion.py"
        assert path.is_file(), "notes ingestion module is missing"
        return getattr(importlib.import_module("notes_ingestion"), name)


notes_ingestion = LazyNotesIngestion()


FAKE_SECRET = "sk-" + ("A1b2" * 6)


def make_obsidian(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    obsidian = tmp_path / "obsidian"
    (obsidian / "笔记").mkdir(parents=True)
    return vault, obsidian


def jsonl_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_enumerator_rejects_file_and_directory_symlinks(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    notes = obsidian / "笔记"
    (notes / "safe.md").write_text("# Safe\n\nOwned note.", encoding="utf-8")
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("# Outside\n\nMust not ingest.", encoding="utf-8")
    (notes / "linked-file.md").symlink_to(outside_file)
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "hidden.md").write_text("# Hidden", encoding="utf-8")
    (notes / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

    result = notes_ingestion.ingest_notes(vault, obsidian, dry_run=True)

    assert result["totals"]["planned_this_run"] == 1
    assert result["skipped_by_reason"] == {
        "symlink_directory": 1,
        "symlink_file": 1,
    }
    assert all("Outside" not in json.dumps(item) for item in result["skipped"])


def test_file_count_single_file_and_total_byte_limits_are_enforced(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    notes = obsidian / "笔记"
    (notes / "a.md").write_text("a" * 8, encoding="utf-8")
    (notes / "b.md").write_text("b" * 8, encoding="utf-8")
    (notes / "too-large.md").write_text("x" * 40, encoding="utf-8")
    limits = notes_ingestion.NoteIngestionLimits(
        max_files=1,
        max_file_bytes=20,
        max_total_bytes=10,
    )

    result = notes_ingestion.ingest_notes(
        vault,
        obsidian,
        dry_run=True,
        limits=limits,
    )

    assert result["totals"]["planned_this_run"] == 1
    assert result["skipped_by_reason"] == {
        "file_too_large": 1,
        "max_files_reached": 1,
    }


def test_total_byte_limit_is_enforced_independently_of_file_count(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    notes = obsidian / "笔记"
    (notes / "a.md").write_text("a" * 8, encoding="utf-8")
    (notes / "b.md").write_text("b" * 8, encoding="utf-8")

    result = notes_ingestion.ingest_notes(
        vault,
        obsidian,
        dry_run=True,
        limits=notes_ingestion.NoteIngestionLimits(
            max_files=10,
            max_file_bytes=20,
            max_total_bytes=10,
        ),
    )

    assert result["totals"]["planned_this_run"] == 1
    assert result["skipped_by_reason"] == {"total_bytes_exceeded": 1}


def test_secret_shapes_are_skipped_without_leaking_the_candidate(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    (obsidian / "笔记" / "secret.md").write_text(
        f"# Credentials\n\nvalue: {FAKE_SECRET}",
        encoding="utf-8",
    )

    result = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)
    dumped = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "ok"
    assert result["totals"]["planned_this_run"] == 0
    assert result["secret_shapes"] == {"sk_key": 1}
    assert result["skipped_by_reason"] == {"secret_shape": 1}
    skipped = result["skipped"][0]
    assert skipped["basename"] == "secret.md"
    assert len(skipped["path_sha256_16"]) == 16
    assert skipped["rules"] == ["sk_key"]
    assert FAKE_SECRET not in dumped
    assert not (vault / "index.jsonl").exists()


def test_repeated_sync_never_duplicates_daily_or_index(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    (obsidian / "笔记" / "one.md").write_text("# One\n\nStable.", encoding="utf-8")

    first = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)
    second = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)
    daily_files = list((vault / "daily").glob("*.jsonl"))

    assert first["totals"]["ingested_this_run"] == 1
    assert second["totals"]["ingested_this_run"] == 0
    assert len(jsonl_rows(vault / "index.jsonl")) == 1
    assert len(daily_files) == 1
    assert len(jsonl_rows(daily_files[0])) == 1


def test_concurrent_sync_uses_one_source_lock_and_writes_one_record(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    (obsidian / "笔记" / "one.md").write_text("# One\n\nConcurrent.", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: notes_ingestion.ingest_notes(
                    vault,
                    obsidian,
                    dry_run=False,
                ),
                range(2),
            )
        )

    daily = next((vault / "daily").glob("*.jsonl"))
    assert sorted(result["totals"]["ingested_this_run"] for result in results) == [0, 1]
    assert len(jsonl_rows(vault / "index.jsonl")) == 1
    assert len(jsonl_rows(daily)) == 1


def test_retry_repairs_failure_between_daily_and_index_without_daily_duplicate(
    tmp_path,
    monkeypatch,
):
    vault, obsidian = make_obsidian(tmp_path)
    (obsidian / "笔记" / "one.md").write_text("# One\n\nRecoverable.", encoding="utf-8")
    calls = {"count": 0}

    def fail_once():
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("injected interruption")

    module = importlib.import_module("notes_ingestion")
    monkeypatch.setattr(module, "after_daily_append", fail_once)
    interrupted = module.ingest_notes(vault, obsidian, dry_run=False)
    recovered = module.ingest_notes(vault, obsidian, dry_run=False)
    daily = next((vault / "daily").glob("*.jsonl"))

    assert interrupted["status"] == "error"
    assert interrupted["error_code"] == "write_interrupted"
    assert recovered["status"] == "ok"
    assert recovered["totals"]["repaired_index"] == 1
    assert len(jsonl_rows(daily)) == 1
    assert len(jsonl_rows(vault / "index.jsonl")) == 1


def test_retry_repairs_missing_daily_when_index_record_already_exists(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    (obsidian / "笔记" / "one.md").write_text("# One\n\nReverse repair.", encoding="utf-8")
    first = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)
    daily = next((vault / "daily").glob("*.jsonl"))
    daily.unlink()

    repaired = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)

    assert first["totals"]["ingested_this_run"] == 1
    assert repaired["totals"]["ingested_this_run"] == 0
    assert repaired["totals"]["repaired_daily"] == 1
    assert len(jsonl_rows(daily)) == 1
    assert len(jsonl_rows(vault / "index.jsonl")) == 1


def test_retry_truncates_partial_jsonl_tails_before_appending(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    note = obsidian / "笔记" / "one.md"
    note.write_text("# One\n\nVersion one.", encoding="utf-8")
    first = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)
    daily = next((vault / "daily").glob("*.jsonl"))
    with daily.open("ab") as handle:
        handle.write(b'{"id":"partial')
    with (vault / "index.jsonl").open("ab") as handle:
        handle.write(b'{"id":"partial')
    note.write_text("# One\n\nVersion two.", encoding="utf-8")

    recovered = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)

    assert first["status"] == "ok"
    assert recovered["status"] == "ok"
    assert recovered["totals"]["repaired_tails"] == 2
    assert len(jsonl_rows(daily)) == 2
    assert len(jsonl_rows(vault / "index.jsonl")) == 2


def test_sync_preserves_complete_index_object_without_trailing_newline(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    note = obsidian / "笔记" / "new.md"
    note.write_text("# New\n\nAppend after valid index fact.", encoding="utf-8")
    index = vault / "index.jsonl"
    index.parent.mkdir(parents=True)
    existing = {"id": "existing-index-fact", "content": "preserve me"}
    index.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    result = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)

    rows = jsonl_rows(index)
    assert result["status"] == "ok"
    assert result["totals"]["repaired_tails"] == 1
    assert rows[0] == existing
    assert len(rows) == 2
    assert index.read_bytes().endswith(b"\n")


def test_sync_preserves_complete_daily_object_without_trailing_newline(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    note = obsidian / "笔记" / "new.md"
    note.write_text("# New\n\nAppend after valid daily fact.", encoding="utf-8")
    day = datetime.fromtimestamp(note.stat().st_mtime, tz=timezone.utc).date().isoformat()
    daily = vault / "daily" / f"{day}.jsonl"
    daily.parent.mkdir(parents=True)
    existing = {"id": "existing-daily-fact", "content": "preserve me"}
    daily.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    result = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)

    rows = jsonl_rows(daily)
    assert result["status"] == "ok"
    assert result["totals"]["repaired_tails"] == 1
    assert rows[0] == existing
    assert len(rows) == 2
    assert daily.read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "invalid_tail",
    [
        {"content": "missing id"},
        {"id": "", "content": "empty id"},
        ["not", "an", "object"],
    ],
)
def test_sync_truncates_complete_json_tail_that_is_not_a_valid_fact(
    tmp_path,
    invalid_tail,
):
    vault, obsidian = make_obsidian(tmp_path)
    note = obsidian / "笔记" / "new.md"
    note.write_text("# New\n\nReplace invalid tail.", encoding="utf-8")
    index = vault / "index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps(invalid_tail), encoding="utf-8")

    result = notes_ingestion.ingest_notes(vault, obsidian, dry_run=False)

    rows = jsonl_rows(index)
    assert result["status"] == "ok"
    assert result["totals"]["repaired_tails"] == 1
    assert len(rows) == 1
    assert rows[0]["title"] == "New"


def test_state_write_failure_is_typed_and_preserves_committed_fact_for_retry(
    tmp_path,
    monkeypatch,
):
    vault, obsidian = make_obsidian(tmp_path)
    (obsidian / "笔记" / "one.md").write_text("# One\n\nCommitted.", encoding="utf-8")
    module = importlib.import_module("notes_ingestion")
    original = module.atomic_write_json
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("state disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "atomic_write_json", fail_once)
    failed_state = module.ingest_notes(vault, obsidian, dry_run=False)
    retried = module.ingest_notes(vault, obsidian, dry_run=False)

    assert failed_state["status"] == "error"
    assert failed_state["error_code"] == "state_write_failed"
    assert failed_state["facts_committed"] is True
    assert len(jsonl_rows(vault / "index.jsonl")) == 1
    assert retried["status"] == "ok"
    assert retried["totals"]["ingested_this_run"] == 0


def test_corrupt_state_returns_typed_error_instead_of_traceback(tmp_path):
    vault, _obsidian = make_obsidian(tmp_path)
    state = vault / "notes" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{broken", encoding="utf-8")

    result = notes_ingestion.read_ingestion_state(vault)

    assert result == {
        "status": "error",
        "error_code": "state_corrupt",
        "state_path": str(state),
    }


def test_dry_run_creates_no_vault_state_lock_daily_or_index(tmp_path):
    vault, obsidian = make_obsidian(tmp_path)
    (obsidian / "笔记" / "one.md").write_text("# One\n\nDry.", encoding="utf-8")

    result = notes_ingestion.ingest_notes(vault, obsidian, dry_run=True)

    assert result["status"] == "ok"
    assert result["totals"]["planned_this_run"] == 1
    assert not vault.exists()


def test_cli_limits_override_configured_defaults():
    adapter = importlib.import_module("obsidian_notes_sync")
    limits = adapter.configured_limits(
        {
            "obsidian": {
                "notes_sync": {
                    "max_files": 20,
                    "max_file_bytes": 200,
                    "max_total_bytes": 2000,
                }
            }
        },
        max_files=3,
    )

    assert limits == notes_ingestion.NoteIngestionLimits(
        max_files=3,
        max_file_bytes=200,
        max_total_bytes=2000,
    )


def test_cli_corrupt_status_is_typed_nonzero_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
):
    adapter = importlib.import_module("obsidian_notes_sync")
    vault, _obsidian = make_obsidian(tmp_path)
    state = vault / "notes" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        adapter,
        "load_config",
        lambda: {"vault_dir": str(vault)},
    )

    exit_code = adapter.main(["status", "--json"])
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert exit_code == 1
    assert payload["status"] == "error"
    assert payload["error_code"] == "state_corrupt"
    assert "Traceback" not in output.out + output.err
