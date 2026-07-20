from __future__ import annotations

from datetime import datetime, timezone

import export_restore


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
