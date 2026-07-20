from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import export_restore
import immortal


NOW = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)


def valid_evidence() -> dict:
    return {
        "generated_at": "2026-07-20T03:30:00Z",
        "storage": {"location": "external_disk"},
        "verification": {"mode": "strict-sha256", "ok": True},
        "restore_check": {"ok": True, "strict": True},
        "secret_scan": {"unique_candidates": 0},
        "warnings": [],
        "health": {"ok": True},
        "index_parity": {"ok": True},
    }


def test_internal_manifest_only_backup_with_secret_warning_blocks_migration():
    evidence = valid_evidence()
    evidence.update(
        {
            "storage": {"location": "internal_vault"},
            "verification": {"mode": "manifest-only", "ok": True},
            "warnings": ["secret_shapes_present"],
        }
    )

    result = export_restore.migration_backup_gate(evidence, require_external=True, now=NOW)

    assert result["ok"] is False
    assert {
        "backup_not_external",
        "verification_not_strict",
        "secret_shapes_present",
    }.issubset(result["blockers"])


def test_same_disk_backup_is_not_external():
    evidence = valid_evidence()
    evidence["storage"] = {"location": "same_disk"}

    assert "backup_not_external" in export_restore.migration_backup_gate(evidence, now=NOW)["blockers"]


def test_missing_strict_restore_evidence_fails_closed():
    evidence = valid_evidence()
    evidence.pop("restore_check")

    assert "restore_check_missing_or_failed" in export_restore.migration_backup_gate(evidence, now=NOW)["blockers"]


def test_secret_candidate_count_blocks_even_without_warning_text():
    evidence = valid_evidence()
    evidence["secret_scan"] = {"unique_candidates": 2}

    assert "secret_shapes_present" in export_restore.migration_backup_gate(evidence, now=NOW)["blockers"]


def test_stale_backup_failed_health_and_index_parity_all_block():
    evidence = valid_evidence()
    evidence.update(
        {
            "generated_at": "2026-07-10T03:30:00Z",
            "health": {"ok": False},
            "index_parity": {"ok": False},
        }
    )

    result = export_restore.migration_backup_gate(evidence, now=NOW, max_age_hours=24)

    assert {"backup_stale", "health_check_failed", "index_parity_failed"}.issubset(
        result["blockers"]
    )


def test_missing_timestamp_health_and_index_evidence_fail_closed():
    evidence = valid_evidence()
    evidence.pop("generated_at")
    evidence.pop("health")
    evidence.pop("index_parity")

    result = export_restore.migration_backup_gate(evidence, now=NOW)

    assert {
        "backup_timestamp_invalid",
        "health_check_failed",
        "index_parity_failed",
    }.issubset(result["blockers"])


@pytest.mark.parametrize(
    "secret_scan",
    [None, "invalid", [], {}, {"unique_candidates": -1}],
)
def test_missing_invalid_or_negative_secret_scan_fails_closed(secret_scan):
    evidence = valid_evidence()
    if secret_scan is None:
        evidence.pop("secret_scan")
    else:
        evidence["secret_scan"] = secret_scan

    result = export_restore.migration_backup_gate(evidence, now=NOW)

    assert "secret_scan_invalid" in result["blockers"]


def test_non_mapping_gate_evidence_fails_closed():
    result = export_restore.migration_backup_gate(None, now=NOW)

    assert result["ok"] is False
    assert "secret_scan_invalid" in result["blockers"]


def test_external_strict_restorable_fresh_healthy_backup_passes():
    result = export_restore.migration_backup_gate(valid_evidence(), require_external=True, now=NOW)

    assert result["ok"] is True
    assert result["blockers"] == []


def test_real_backup_status_field_shape_is_accepted():
    status = {
        "generated_at": "2026-07-20T03:30:00Z",
        "storage_location": "external_disk",
        "mode": "verified",
        "check": {"ok": True, "mode": "strict-sha256"},
        "secret_scan": {"unique_candidates": 0},
        "warnings": [],
        "health": {"ok": True},
        "index_parity": {"ok": True},
    }

    assert export_restore.migration_backup_gate(status, now=NOW)["ok"] is True


def test_backup_status_recomputes_storage_instead_of_trusting_manifest(tmp_path):
    vault = tmp_path / "vault"
    export_dir = vault / "exports" / "immortal-export-20260720T040000Z"
    export_dir.mkdir(parents=True)
    payload = b"proof"
    (export_dir / "proof.txt").write_bytes(payload)
    manifest = {
        "generated_at": "2026-07-20T03:30:00Z",
        "storage_location": "external_disk",
        "secret_scan": {"unique_candidates": 0},
        "warnings": [],
        "items": [
            {
                "relpath": "proof.txt",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "totals": {"files": 1, "bytes": len(payload)},
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    status = export_restore.get_backup_status(vault, verify=True)

    assert status["storage_location"] == "internal_vault"


def test_strict_restore_rejects_manifest_item_symlink(tmp_path):
    export_dir = tmp_path / "immortal-export-20260720T040000Z"
    export_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (export_dir / "proof.txt").symlink_to(outside)
    manifest = {
        "generated_at": "2026-07-20T03:30:00Z",
        "items": [
            {
                "relpath": "proof.txt",
                "size": len(b"outside"),
                "sha256": hashlib.sha256(b"outside").hexdigest(),
            }
        ],
        "totals": {"files": 1, "bytes": len(b"outside")},
        "warnings": [],
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert export_restore.restore_check(export_dir, strict=True)["ok"] is False


def test_migration_status_strictly_restores_state_selected_external_export(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    external = tmp_path / "mounted-external" / "immortal-export-20260720T040000Z"
    external.mkdir(parents=True)
    vault.mkdir()
    payload = b"external strict proof"
    (external / "proof.txt").write_bytes(payload)
    generated_at = "2026-07-20T03:30:00Z"
    manifest = {
        "generated_at": generated_at,
        "vault_dir": str(vault),
        "export_dir": str(external),
        "storage_location": "external_disk",
        "secret_scan": {"unique_candidates": 0},
        "warnings": [],
        "items": [
            {
                "relpath": "proof.txt",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "totals": {"files": 1, "bytes": len(payload)},
    }
    (external / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (vault / "orchestrator_state.json").write_text(
        json.dumps(
            {
                "last_portable_export_dir": str(external),
                "last_portable_export": generated_at,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        export_restore,
        "classify_storage_location",
        lambda target, source: "external_disk",
    )

    status = export_restore.get_migration_backup_status(vault)

    assert status["ok"] is True
    assert status["storage_location"] == "external_disk"
    assert status["check"]["mode"] == "strict-sha256"
    status["health"] = {"ok": True}
    status["index_parity"] = {"ok": True}
    assert export_restore.migration_backup_gate(status, now=NOW)["ok"] is True

    manifest["items"] = []
    manifest["totals"] = {"files": 0, "bytes": 0}
    (external / "proof.txt").unlink()
    (external / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert export_restore.get_migration_backup_status(vault)["ok"] is False


def test_migration_preflight_command_accepts_state_selected_external_export(
    tmp_path, monkeypatch, capsys
):
    import index_db
    import preflight

    vault = tmp_path / "vault"
    external = tmp_path / "external" / "immortal-export-command"
    external.mkdir(parents=True)
    vault.mkdir()
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = b"command external proof"
    (external / "proof.txt").write_bytes(payload)
    manifest = {
        "generated_at": generated_at,
        "vault_dir": str(vault),
        "export_dir": str(external),
        "storage_location": "external_disk",
        "secret_scan": {"unique_candidates": 0},
        "warnings": [],
        "items": [
            {
                "relpath": "proof.txt",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "totals": {"files": 1, "bytes": len(payload)},
    }
    (external / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    state = {
        "last_portable_export_dir": str(external),
        "last_portable_export": generated_at,
        "errors": [],
    }
    state_file = vault / "orchestrator_state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(immortal, "IMMORTAL_DIR", vault)
    monkeypatch.setattr(immortal, "STATE_FILE", state_file)
    monkeypatch.setattr(
        export_restore,
        "classify_storage_location",
        lambda target, source: "external_disk",
    )
    monkeypatch.setattr(index_db, "is_ready", lambda: True)
    monkeypatch.setattr(
        preflight,
        "gather_preflight",
        lambda **kwargs: {"context_status": "ready", "vault_status": "healthy"},
    )

    code = immortal.command_migration_preflight(
        argparse.Namespace(
            require_external_backup=True,
            max_age_hours=168,
            json=True,
        )
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    stale_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    manifest["generated_at"] = stale_at
    state["last_portable_export"] = stale_at
    (external / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    state_file.write_text(json.dumps(state), encoding="utf-8")
    stale_code = immortal.command_migration_preflight(
        argparse.Namespace(
            require_external_backup=True,
            max_age_hours=24,
            json=True,
        )
    )

    assert stale_code == 1
    assert "backup_stale" in json.loads(capsys.readouterr().out)["blockers"]


@pytest.mark.parametrize(
    "state_payload",
    [
        {},
        {"last_portable_export_dir": "/missing/export"},
        {"last_portable_export_dir": "", "last_portable_export": "2026-07-20T03:30:00Z"},
    ],
)
def test_migration_status_fails_closed_for_missing_or_invalid_state_export(tmp_path, state_payload):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "orchestrator_state.json").write_text(
        json.dumps(state_payload), encoding="utf-8"
    )

    status = export_restore.get_migration_backup_status(vault)

    assert status["ok"] is False
    assert status["check"]["ok"] is False


def test_migration_preflight_cli_uses_real_temporary_vault_evidence(tmp_path):
    home = tmp_path / "home"
    vault = home / ".immortal"
    vault.mkdir(parents=True)
    now_text = datetime.now(timezone.utc).isoformat()
    record = {
        "id": "real-1",
        "timestamp": now_text,
        "source": "test-source",
        "role": "user",
        "content": "real temporary vault record",
    }
    index_payload = (json.dumps(record) + "\n").encode()
    (vault / "index.jsonl").write_bytes(index_payload)
    export_dir = vault / "exports" / "immortal-export-20260720T040000Z"
    (vault / "orchestrator_state.json").write_text(
        json.dumps(
            {
                "last_collect": now_text,
                "total_records": 1,
                "errors": [],
                "last_portable_export_dir": str(export_dir),
                "last_portable_export": now_text,
            }
        ),
        encoding="utf-8",
    )
    (vault / "sources.json").write_text('{"sources":["test-source"]}', encoding="utf-8")

    export_dir.mkdir(parents=True)
    copied = b"strict restore evidence"
    (export_dir / "proof.txt").write_bytes(copied)
    manifest = {
        "generated_at": now_text,
        "vault_dir": str(vault),
        "export_dir": str(export_dir),
        "storage_location": "same_disk",
        "secret_scan": {"unique_candidates": 0},
        "warnings": [],
        "items": [
            {
                "relpath": "proof.txt",
                "size": len(copied),
                "sha256": hashlib.sha256(copied).hexdigest(),
            }
        ],
        "totals": {"files": 1, "bytes": len(copied)},
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(root / "core")}
    indexed = subprocess.run(
        [sys.executable, str(root / "core" / "index_db.py"), "sync"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert indexed.returncode == 0, indexed.stdout + indexed.stderr

    result = subprocess.run(
        [
            sys.executable,
            str(root / "core" / "immortal.py"),
            "migration-preflight",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["evidence"]["health"]["ok"] is True
    assert payload["evidence"]["index_parity"]["ok"] is True

    external_required = subprocess.run(
        [
            sys.executable,
            str(root / "core" / "immortal.py"),
            "migration-preflight",
            "--require-external-backup",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert external_required.returncode == 1
    assert "backup_not_external" in json.loads(external_required.stdout)["blockers"]

    manifest["secret_scan"] = {"unique_candidates": "invalid"}
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    invalid_secret_evidence = subprocess.run(
        [
            sys.executable,
            str(root / "core" / "immortal.py"),
            "migration-preflight",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert invalid_secret_evidence.returncode == 1
    assert "secret_scan_invalid" in json.loads(invalid_secret_evidence.stdout)["blockers"]
