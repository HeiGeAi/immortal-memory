from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import export_restore
import pytest
from index_integrity import INDEX_SCHEMA_VERSION, ids_sha256


@pytest.fixture(autouse=True)
def _trusted_hermes_root(tmp_path, monkeypatch):
    root = tmp_path / "trusted-hermes-sessions"
    root.mkdir()
    monkeypatch.setattr(export_restore, "HERMES_SESSIONS_ROOT", root)


def _write_index(vault: Path, rows: list[dict]) -> bytes:
    vault.mkdir(parents=True, exist_ok=True)
    body = b"".join(
        (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for row in rows
    )
    (vault / "index.jsonl").write_bytes(body)
    return body


def _contract(path: Path, source_body: bytes, *, session_ids: list[str]) -> Path:
    evidence_path = export_restore.HERMES_SESSIONS_ROOT / "timezone-metadata.json"
    evidence_body = {
        "schema_version": 1,
        "authority": export_restore.TIMEZONE_EVIDENCE_AUTHORITY,
        "source": "hermes-conversation",
        "timestamp_semantics": "local_wall_time",
        "timezone": "Asia/Shanghai",
        "session_ids": session_ids,
        "fold_by_record_id": {},
        "verified_by": "owner",
        "verified_at": "2026-07-22T00:00:00+00:00",
    }
    evidence_path.write_text(json.dumps(evidence_body), encoding="utf-8")
    evidence_path.chmod(0o600)
    payload = {
        "schema_version": 1,
        "source": "hermes-conversation",
        "timestamp_semantics": "local_wall_time",
        "timezone": "Asia/Shanghai",
        "source_index_sha256": hashlib.sha256(source_body).hexdigest(),
        "session_ids": session_ids,
        "fold_by_record_id": {},
        "verified_by": "owner",
        "verified_at": "2026-07-22T00:00:00+00:00",
        "evidence": {
            "kind": "source_metadata",
            "reference": "Hermes legacy datetime.now().isoformat contract",
            "path": str(evidence_path),
            "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_evidence_backed_naive_timestamp_is_staged_with_explicit_offset(tmp_path):
    vault = tmp_path / "vault"
    source = _write_index(
        vault,
        [
            {
                "id": "h1",
                "source": "hermes-conversation",
                "session_id": "s1",
                "timestamp": "2026-05-07T23:25:55.530342",
                "content": "kept",
            },
            {
                "id": "aware",
                "source": "codex",
                "timestamp": "2026-05-07T16:00:00+00:00",
                "content": "unchanged",
            },
        ],
    )
    contract = _contract(tmp_path / "contract.json", source, session_ids=["s1"])

    result = export_restore.stage_v11_index(vault, timezone_contract=contract)

    assert result["ok"] is True
    assert result["production_switch_allowed"] is False
    assert result["blockers"] == ["production_prewarm_pending"]
    assert result["converted"] == 1
    assert result["quarantined"] == 0
    assert (vault / "index.jsonl").read_bytes() == source
    rows = [json.loads(line) for line in Path(result["staging_source"]).read_text().splitlines()]
    assert rows[0]["timestamp"] == "2026-05-07T23:25:55.530342+08:00"
    assert rows[0]["content"] == "kept"
    assert rows[1]["timestamp"] == "2026-05-07T16:00:00+00:00"


def test_unproven_naive_timestamp_is_quarantined_and_blocks_switch(tmp_path):
    vault = tmp_path / "vault"
    source = _write_index(
        vault,
        [
            {
                "id": "h1",
                "source": "hermes-conversation",
                "session_id": "s1",
                "timestamp": "2026-05-07T23:25:55.530342",
                "content": "private body must not enter the report",
            },
            {
                "id": "aware",
                "source": "codex",
                "timestamp": "2026-05-07T16:00:00+00:00",
                "content": "safe",
            },
        ],
    )

    result = export_restore.stage_v11_index(vault)

    assert result["ok"] is False
    assert result["blockers"] == ["unresolved_naive_timestamps"]
    assert result["converted"] == 0
    assert result["quarantined"] == 1
    assert (vault / "index.jsonl").read_bytes() == source
    quarantine = json.loads(Path(result["quarantine_report"]).read_text())
    assert quarantine["count"] == 1
    assert quarantine["items"][0]["line_number"] == 1
    assert "content" not in quarantine["items"][0]
    assert "private body" not in json.dumps(quarantine)
    staged = [json.loads(line) for line in Path(result["staging_source"]).read_text().splitlines()]
    assert [row["id"] for row in staged] == ["aware"]


def test_contract_is_bound_to_exact_source_generation(tmp_path):
    vault = tmp_path / "vault"
    source = _write_index(
        vault,
        [{"id": "h1", "source": "hermes-conversation", "session_id": "s1", "timestamp": "2026-05-07T23:25:55", "content": "x"}],
    )
    contract = _contract(tmp_path / "contract.json", source, session_ids=["s1"])
    (vault / "index.jsonl").write_bytes(source + b"\n")

    result = export_restore.stage_v11_index(vault, timezone_contract=contract)

    assert result["ok"] is False
    assert "timezone_contract_source_mismatch" in result["blockers"]
    assert result["converted"] == 0


def test_contract_evidence_artifact_must_match_declared_hash(tmp_path):
    vault = tmp_path / "vault"
    source = _write_index(
        vault,
        [{"id": "h1", "source": "hermes-conversation", "session_id": "s1", "timestamp": "2026-05-07T23:25:55", "content": "x"}],
    )
    contract = _contract(tmp_path / "contract.json", source, session_ids=["s1"])
    payload = json.loads(contract.read_text())
    Path(payload["evidence"]["path"]).write_text("changed\n", encoding="utf-8")

    result = export_restore.stage_v11_index(vault, timezone_contract=contract)

    assert result["ok"] is False
    assert "timezone_evidence_mismatch" in result["blockers"]
    assert result["converted"] == 0


def test_ambiguous_dst_wall_time_requires_explicit_fold_evidence(tmp_path):
    vault = tmp_path / "vault"
    source = _write_index(
        vault,
        [{"id": "h1", "source": "hermes-conversation", "session_id": "s1", "timestamp": "2026-11-01T01:30:00", "content": "x"}],
    )
    contract = _contract(tmp_path / "contract.json", source, session_ids=["s1"])
    payload = json.loads(contract.read_text())
    payload["timezone"] = "America/New_York"
    evidence_path = Path(payload["evidence"]["path"])
    evidence = json.loads(evidence_path.read_text())
    evidence["timezone"] = "America/New_York"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    payload["evidence"]["sha256"] = hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()
    contract.write_text(json.dumps(payload), encoding="utf-8")
    contract.chmod(0o600)

    result = export_restore.stage_v11_index(vault, timezone_contract=contract)

    assert result["ok"] is False
    quarantine = json.loads(Path(result["quarantine_report"]).read_text())
    assert quarantine["items"][0]["reason"] == "ambiguous_wall_time"


def test_staging_output_directory_cannot_be_a_symlink(tmp_path):
    vault = tmp_path / "vault"
    _write_index(
        vault,
        [{"id": "m1", "source": "codex", "timestamp": "2026-07-20T00:00:00+00:00", "content": "safe"}],
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "output"
    output.symlink_to(outside, target_is_directory=True)

    result = export_restore.stage_v11_index(vault, output_dir=output)

    assert result["ok"] is False
    assert result["blockers"] == ["migration_output_unsafe"]


def test_v10_vault_migrates_without_source_changes(tmp_path):
    vault = tmp_path / "vault"
    source = _write_index(
        vault,
        [{"id": "m1", "source": "codex", "timestamp": "2026-07-20T00:00:00+00:00", "content": "safe"}],
    )
    (vault / "profile.md").write_text("legacy\n", encoding="utf-8")
    reviewed = vault / "reviewed/profile_memories.jsonl"
    reviewed.parent.mkdir(parents=True)
    reviewed.write_text(
        json.dumps(
            {
                "memory_id": "legacy-1",
                "statement": "偏好短段落",
                "focus": "self_profile",
                "memory_type": "preference",
                "evidence_ids": ["m1"],
                "speaker": "owner",
                "sensitivity": "internal",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    before = {
        "index": hashlib.sha256((vault / "index.jsonl").read_bytes()).hexdigest(),
        "profile": hashlib.sha256((vault / "profile.md").read_bytes()).hexdigest(),
        "reviewed": hashlib.sha256(reviewed.read_bytes()).hexdigest(),
    }

    result = export_restore.run_v11_migration(vault)

    after = {
        "index": hashlib.sha256((vault / "index.jsonl").read_bytes()).hexdigest(),
        "profile": hashlib.sha256((vault / "profile.md").read_bytes()).hexdigest(),
        "reviewed": hashlib.sha256(reviewed.read_bytes()).hexdigest(),
    }
    assert result["ok"] is True
    assert result["production_switch_allowed"] is False
    assert result["blockers"] == ["production_prewarm_pending"]
    assert before == after
    for relative in export_restore.V11_REQUIRED_EVENT_PATHS:
        assert (vault / relative).is_file()
    assert (vault / "model/claims/current.jsonl").is_file()
    assert len((vault / "model/claims/events.jsonl").read_text().splitlines()) == 1
    assert (vault / "model/living-self/current.json").is_file()
    assert (vault / "model/living-self/current.md").is_file()


def test_v11_migration_stops_before_model_writes_when_index_has_quarantine(tmp_path):
    vault = tmp_path / "vault"
    source = _write_index(
        vault,
        [{"id": "h1", "source": "hermes-conversation", "session_id": "s1", "timestamp": "2026-05-07T23:25:55", "content": "x"}],
    )

    result = export_restore.run_v11_migration(vault)

    assert result["ok"] is False
    assert result["production_switch_allowed"] is False
    assert not (vault / "model/claims/events.jsonl").exists()
    assert (vault / "index.jsonl").read_bytes() == source


def test_v11_model_migration_refuses_direct_live_vault(tmp_path, monkeypatch):
    vault = tmp_path / "live"
    _write_index(
        vault,
        [{"id": "m1", "source": "codex", "timestamp": "2026-07-20T00:00:00+00:00", "content": "safe"}],
    )
    monkeypatch.setattr(export_restore, "IMMORTAL_DIR", vault)

    result = export_restore.run_v11_migration(vault)

    assert result["ok"] is False
    assert result["blockers"] == ["staging_vault_required"]
    assert not (vault / "model").exists()


def _seed_trusted_index(vault: Path) -> None:
    source_body = _write_index(
        vault,
        [{"id": "m1", "source": "codex", "timestamp": "2026-07-20T00:00:00+00:00", "content": "safe"}],
    )
    database = vault / "search_index.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE docs(rowid INTEGER PRIMARY KEY, rec_id TEXT, ts TEXT, ts_utc TEXT NOT NULL, source TEXT, "
        "role TEXT, project TEXT, content TEXT, source_offset INTEGER NOT NULL, source_length INTEGER NOT NULL, "
        "line_number INTEGER NOT NULL, content_sha256 TEXT NOT NULL)"
    )
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE INDEX idx_docs_rec_id ON docs(rec_id)")
    connection.execute("CREATE INDEX idx_docs_source ON docs(source)")
    connection.execute("CREATE INDEX idx_docs_ts ON docs(ts)")
    connection.execute("CREATE INDEX idx_docs_ts_utc_rowid ON docs(ts_utc DESC,rowid DESC)")
    connection.execute("CREATE UNIQUE INDEX idx_docs_source_offset ON docs(source_offset)")
    connection.execute("CREATE UNIQUE INDEX idx_docs_line_number ON docs(line_number)")
    connection.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(content, tokenize='trigram')")
    connection.execute(
        "INSERT INTO docs(rec_id,ts,ts_utc,source,role,project,content,source_offset,source_length,line_number,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("m1", "2026-07-20T00:00:00+00:00", "2026-07-20T00:00:00.000000Z", "codex", "user", "p", "safe", 0, len(source_body), 1, hashlib.sha256(b"safe").hexdigest()),
    )
    connection.execute("INSERT INTO docs_fts(rowid,content) VALUES(1,'safe')")
    metadata = (vault / "index.jsonl").stat()
    rows = {
        "parity_status": "trusted",
        "last_size": metadata.st_size,
        "source_dev": metadata.st_dev,
        "source_ino": metadata.st_ino,
        "source_mtime_ns": metadata.st_mtime_ns,
        "source_ctime_ns": metadata.st_ctime_ns,
        "indexed_id_count": 1,
        "indexed_ids_sha256": ids_sha256(["m1"]),
        "index_schema_version": INDEX_SCHEMA_VERSION,
    }
    connection.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        [(key, str(value)) for key, value in rows.items()],
    )
    connection.commit()
    connection.close()


def test_index_receipt_prewarm_measures_full_validation_and_fresh_process_hit(tmp_path):
    vault = tmp_path / "vault"
    _seed_trusted_index(vault)

    result = export_restore.prewarm_index_verification(vault)

    assert result["ok"] is True
    assert result["full_validation"]["mode"] == "generated"
    assert result["fresh_process"]["mode"] == "receipt_hit"
    assert result["full_validation"]["elapsed_ms"] >= 0
    assert result["fresh_process"]["elapsed_ms"] >= 0
    receipt = vault / "product/index-verification.json"
    assert receipt.is_file()
    assert receipt.stat().st_mode & 0o777 == 0o600
    body = json.loads(receipt.read_text())
    assert body["schema_version"] == INDEX_SCHEMA_VERSION
    assert body["validation_version"] == 1


def test_index_receipt_prewarm_rejects_symlinked_receipt(tmp_path):
    vault = tmp_path / "vault"
    _seed_trusted_index(vault)
    receipt = vault / "product/index-verification.json"
    receipt.parent.mkdir(parents=True)
    receipt.symlink_to(vault / "index.jsonl")

    result = export_restore.prewarm_index_verification(vault)

    assert result["ok"] is False
    assert result["blockers"] == ["index_verification_receipt_unsafe"]


def test_production_switch_gate_binds_staged_source_and_exact_prewarmed_db(tmp_path):
    vault = tmp_path / "vault"
    _seed_trusted_index(vault)
    staged = export_restore.stage_v11_index(vault)
    prewarm = export_restore.prewarm_index_verification(vault)

    result = export_restore.v11_production_switch_gate(vault, staged, prewarm)

    assert result["ok"] is True
    assert result["production_switch_allowed"] is True
    assert result["staging_sha256"] == hashlib.sha256(
        (vault / "index.jsonl").read_bytes()
    ).hexdigest()


def test_production_switch_gate_rejects_unbound_migration_generation(tmp_path):
    vault = tmp_path / "vault"
    _seed_trusted_index(vault)
    staged = export_restore.stage_v11_index(vault)
    prewarm = export_restore.prewarm_index_verification(vault)
    staged["staging_sha256"] = "f" * 64

    result = export_restore.v11_production_switch_gate(vault, staged, prewarm)

    assert result["ok"] is False
    assert "published_source_generation_mismatch" in result["blockers"]
