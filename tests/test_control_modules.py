from __future__ import annotations

import json
import subprocess

from control_data import ControlData


def test_agent_get_does_not_generate_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("GET spawned process")
        ),
    )
    data = ControlData(tmp_path, skill_dir=tmp_path)

    result = data.agent_status()

    assert result["available"] is False
    assert result["entry"]["exists"] is False
    assert not (tmp_path / "agent").exists()


def test_agent_status_returns_metadata_not_context_body(tmp_path):
    agent_dir = tmp_path / "agent"
    sessions = tmp_path / "sessions" / "session-1"
    agent_dir.mkdir()
    sessions.mkdir(parents=True)
    (agent_dir / "ENTRY.md").write_text("private agent entry", encoding="utf-8")
    (sessions / "manifest.json").write_text(
        json.dumps({"query": "customer plan", "generated_at": "2026-07-18T08:00:00Z"}),
        encoding="utf-8",
    )
    (sessions / "TASK_CONTEXT.md").write_text("private vault context", encoding="utf-8")
    data = ControlData(tmp_path, skill_dir=tmp_path)

    status = data.agent_status()
    detail = data.agent_context_detail("session-1")
    encoded = json.dumps({"status": status, "detail": detail}, ensure_ascii=False)

    assert status["available"] is True
    assert status["contexts"][0]["slug"] == "session-1"
    assert "private agent entry" not in encoded
    assert "private vault context" not in encoded


def test_same_disk_unverified_backup_is_attention(tmp_path):
    export_dir = tmp_path / "exports" / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-18T08:00:00Z",
                "export_dir": str(export_dir),
                "totals": {"files": 4, "bytes": 100},
                "secret_scan": {"unique_candidates": 1},
                "warnings": ["secret_shapes_present"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "orchestrator_state.json").write_text(
        json.dumps(
            {
                "last_portable_export_dir": str(export_dir),
                "last_portable_restore_check_status": "failed",
            }
        ),
        encoding="utf-8",
    )
    data = ControlData(tmp_path, skill_dir=tmp_path)

    backup = data.backups()["items"][0]

    assert backup["status"] == "attention"
    assert "same_disk" in backup["risks"]
    assert "not_verified" in backup["risks"]
    assert "secret_shapes_present" in backup["risks"]


def test_diagnostics_are_bounded_and_secret_free(tmp_path):
    (tmp_path / "runtime").mkdir()
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    data = ControlData(tmp_path, skill_dir=skill_dir)

    diagnostics = data.diagnostics()
    encoded = json.dumps(diagnostics, ensure_ascii=False)

    assert diagnostics["version"] == "1.0.0"
    assert diagnostics["listen_address"] == "127.0.0.1"
    assert diagnostics["dependencies"]["vault_readable"] is True
    assert "content" not in encoded
    assert "token" not in encoded.casefold()
