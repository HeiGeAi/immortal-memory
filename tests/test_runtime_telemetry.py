from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from runtime_telemetry import RuntimeTelemetry


def test_start_run_and_stage_write_current_state(tmp_path):
    telemetry = RuntimeTelemetry(tmp_path, pid_exists=lambda _pid: True)

    run = telemetry.start_run(trigger="manual", pid=4321)
    staged = telemetry.start_stage("collect", "采集")

    assert run["status"] == "running"
    assert run["trigger"] == "manual"
    assert staged["current_stage"] == "collect"
    assert staged["stages"][0]["status"] == "running"
    assert json.loads((tmp_path / "current_run.json").read_text())["pid"] == 4321


def test_finish_stage_and_run_append_bounded_history(tmp_path):
    telemetry = RuntimeTelemetry(tmp_path, history_limit=2, pid_exists=lambda _pid: True)

    for index in range(3):
        telemetry.start_run(trigger="schedule", pid=100 + index)
        telemetry.start_stage("collect", "采集")
        telemetry.finish_stage("collect", status="success", summary=f"新增 {index} 条")
        result = telemetry.finish_run(
            status="success",
            results={"new_records": index, "total_records": 20 + index},
        )

    history = telemetry.read_history()

    assert result["status"] == "success"
    assert result["finished_at"]
    assert result["stages"][0]["summary"] == "新增 2 条"
    assert len(history) == 2
    assert [item["results"]["new_records"] for item in history] == [2, 1]


def test_failed_stage_finishes_run_with_error(tmp_path):
    telemetry = RuntimeTelemetry(tmp_path, pid_exists=lambda _pid: True)
    telemetry.start_run(trigger="manual", pid=99)
    telemetry.start_stage("backup", "备份")

    staged = telemetry.finish_stage("backup", status="failed", error="checksum mismatch")
    failed = telemetry.finish_run(status="failed", error="backup failed")

    assert staged["stages"][0]["status"] == "failed"
    assert staged["stages"][0]["error"] == "checksum mismatch"
    assert failed["error"] == "backup failed"


def test_read_current_marks_dead_process_as_failed(tmp_path):
    telemetry = RuntimeTelemetry(tmp_path, pid_exists=lambda _pid: False)
    telemetry.start_run(trigger="schedule", pid=9876)
    telemetry.start_stage("collect", "采集")

    current = telemetry.read_current()

    assert current["status"] == "failed"
    assert current["error"] == "运行进程已退出，遥测未正常结束"
    assert current["finished_at"]


def test_read_current_marks_old_heartbeat_as_stale(tmp_path):
    now = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
    telemetry = RuntimeTelemetry(
        tmp_path,
        stale_after_seconds=60,
        clock=lambda: now,
        pid_exists=lambda _pid: True,
    )
    telemetry.start_run(trigger="schedule", pid=123)
    current_path = tmp_path / "current_run.json"
    current = json.loads(current_path.read_text())
    current["updated_at"] = (now - timedelta(minutes=5)).isoformat()
    current_path.write_text(json.dumps(current), encoding="utf-8")

    stale = telemetry.read_current()

    assert stale["status"] == "stale"
    assert "心跳" in stale["error"]


def test_missing_current_returns_empty_dict(tmp_path):
    telemetry = RuntimeTelemetry(tmp_path)

    assert telemetry.read_current() == {}
    assert telemetry.read_history() == []
