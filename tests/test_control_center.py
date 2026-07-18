from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_center import ControlCenter, candidate_scheduler_labels


NOW = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_center(tmp_path, *, scheduler=None):
    return ControlCenter(
        tmp_path,
        skill_dir=tmp_path / "skill",
        clock=lambda: NOW,
        scheduler_probe=(lambda: scheduler or {"status": "unknown", "detail": "没有调度证据"}),
        pid_exists=lambda _pid: True,
    )


def test_no_evidence_is_unknown_not_healthy(tmp_path):
    snapshot = make_center(tmp_path).build_snapshot()

    assert snapshot["status"] == "unknown"
    assert snapshot["status_label"] == "证据不足"
    assert all(item["status"] != "healthy" for item in snapshot["proofs"])


def test_live_fresh_run_is_running(tmp_path):
    runtime = tmp_path / "runtime"
    write_json(
        runtime / "current_run.json",
        {
            "status": "running",
            "pid": 42,
            "started_at": (NOW - timedelta(minutes=3)).isoformat(),
            "updated_at": (NOW - timedelta(seconds=5)).isoformat(),
            "current_stage": "collect",
            "stage_index": 1,
            "stage_total": 7,
            "stages": [{"id": "collect", "label": "采集", "status": "running"}],
            "results": {"new_records": 12},
        },
    )

    snapshot = make_center(
        tmp_path,
        scheduler={"status": "healthy", "detail": "LaunchAgent 已加载"},
    ).build_snapshot()

    assert snapshot["status"] == "running"
    assert snapshot["current_run"]["current_stage"] == "collect"
    assert snapshot["metrics"]["new_records"] == 12


def test_recent_failed_run_is_failed(tmp_path):
    write_json(
        tmp_path / "runtime" / "current_run.json",
        {
            "status": "failed",
            "started_at": (NOW - timedelta(minutes=8)).isoformat(),
            "updated_at": (NOW - timedelta(minutes=2)).isoformat(),
            "finished_at": (NOW - timedelta(minutes=2)).isoformat(),
            "error": "collect failed",
            "stages": [],
            "results": {},
        },
    )

    snapshot = make_center(tmp_path).build_snapshot()

    assert snapshot["status"] == "failed"
    assert any(item["status"] == "failed" for item in snapshot["attention"])


def test_verified_backup_on_same_disk_with_secret_warnings_needs_attention(tmp_path):
    export_dir = tmp_path / "exports" / "portable-20260718"
    write_json(
        export_dir / "manifest.json",
        {
            "generated_at": (NOW - timedelta(hours=2)).isoformat(),
            "totals": {"files": 20, "bytes": 2048},
            "verification": {"ok": True, "checked_files": 20},
            "storage": {"same_disk": True, "external": False},
            "secret_scan": {"unique_candidates": 3},
            "warnings": ["secret_shapes_present: 3"],
        },
    )
    write_json(
        tmp_path / "orchestrator_state.json",
        {
            "last_portable_export_dir": str(export_dir),
            "last_portable_restore_check_status": "ok",
            "last_portable_restore_check_files": 20,
        },
    )

    snapshot = make_center(tmp_path).build_snapshot()
    backup = next(item for item in snapshot["proofs"] if item["id"] == "backup")

    assert backup["status"] == "attention"
    assert "同一磁盘" in backup["detail"]
    assert "敏感形态" in backup["detail"]


def test_backup_location_on_vault_filesystem_is_inferred_as_same_disk(tmp_path):
    export_dir = tmp_path / "exports" / "portable-20260718"
    write_json(
        export_dir / "manifest.json",
        {
            "generated_at": (NOW - timedelta(hours=2)).isoformat(),
            "totals": {"files": 2, "bytes": 20},
            "warnings": [],
        },
    )
    write_json(
        tmp_path / "orchestrator_state.json",
        {
            "last_portable_export_dir": str(export_dir),
            "last_portable_restore_check_status": "ok",
        },
    )

    snapshot = make_center(tmp_path).build_snapshot()
    backup = next(item for item in snapshot["proofs"] if item["id"] == "backup")

    assert backup["status"] == "attention"
    assert "同一磁盘" in backup["detail"]


def test_fresh_outputs_quality_and_scheduler_create_healthy_proofs(tmp_path):
    write_json(
        tmp_path / "runtime" / "current_run.json",
        {
            "status": "success",
            "started_at": (NOW - timedelta(hours=1)).isoformat(),
            "updated_at": (NOW - timedelta(minutes=50)).isoformat(),
            "finished_at": (NOW - timedelta(minutes=50)).isoformat(),
            "stages": [],
            "results": {"new_records": 4, "total_records": 100},
        },
    )
    write_json(
        tmp_path / "orchestrator_state.json",
        {
            "last_collect": (NOW - timedelta(minutes=50)).isoformat(),
            "last_portable_restore_check_status": "ok",
            "last_portable_export_dir": "",
        },
    )
    write_json(tmp_path / "quality" / "latest.json", {"status": "ok", "score": 97, "issue_count": 0})
    for relpath in ("index.jsonl", "profile.json"):
        path = tmp_path / relpath
        path.write_text("{}\n", encoding="utf-8")

    snapshot = make_center(
        tmp_path,
        scheduler={"status": "healthy", "detail": "LaunchAgent 已加载"},
    ).build_snapshot()

    assert snapshot["quality"]["score"] == 97
    assert snapshot["scheduler"]["status"] == "healthy"
    assert snapshot["metrics"]["total_records"] == 100
    assert snapshot["layers"][0]["exists"] is True


def test_scheduler_candidates_are_discovered_without_owner_specific_code(tmp_path):
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    (launch_agents / "com.example.immortal.daily-backup.plist").write_text("", encoding="utf-8")
    (launch_agents / "com.example.unrelated.plist").write_text("", encoding="utf-8")

    labels = candidate_scheduler_labels(launch_agents)

    assert "com.example.immortal.daily-backup" in labels
    assert "com.example.unrelated" not in labels
