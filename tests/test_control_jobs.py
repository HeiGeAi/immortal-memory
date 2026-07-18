from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from control_jobs import JobConflict, run_evidence_marker, sanitize_job_output
from profile_review import FactoryStore


def test_persisted_active_job_becomes_interrupted(tmp_path):
    history = tmp_path / "runtime" / "control_jobs.json"
    history.parent.mkdir()
    history.write_text(
        json.dumps([{"id": "deadbeef", "kind": "health", "status": "running", "created_at": "now"}]),
        encoding="utf-8",
    )

    store = FactoryStore(history_path=history, immortal_dir=tmp_path, skill_dir=tmp_path)

    job = store.get_job("deadbeef")
    assert job["status"] == "interrupted"
    assert job["error_code"] == "service_restarted"


def test_run_is_rejected_when_orchestrator_lock_is_live(tmp_path):
    lock = tmp_path / "orchestrator.lock"
    lock.write_text(f"{os.getpid()} active", encoding="utf-8")
    store = FactoryStore(
        history_path=tmp_path / "runtime" / "control_jobs.json",
        immortal_dir=tmp_path,
        skill_dir=tmp_path,
    )

    with pytest.raises(JobConflict):
        store.start_job("run", {})

    assert store.list_jobs() == []


def test_stale_orchestrator_lock_does_not_block_run(tmp_path):
    lock = tmp_path / "orchestrator.lock"
    lock.write_text("999999 stale", encoding="utf-8")
    store = FactoryStore(
        history_path=tmp_path / "runtime" / "control_jobs.json",
        immortal_dir=tmp_path,
        skill_dir=tmp_path,
    )

    assert store._orchestrator_is_active() is False


def test_zero_exit_without_new_run_evidence_is_failed(tmp_path):
    store = FactoryStore(
        history_path=tmp_path / "runtime" / "control_jobs.json",
        immortal_dir=tmp_path,
        skill_dir=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    job_id = store.start_job("run", {})["id"]
    deadline = time.monotonic() + 3
    job = store.get_job(job_id)
    while job and job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.01)
        job = store.get_job(job_id)

    assert job["status"] == "failed"
    assert job["error_code"] == "run_not_observed"


def test_job_output_is_redacted_and_bounded():
    token = "ghp_" + ("a" * 40)
    output = sanitize_job_output(("x" * 60000) + token)

    assert token not in output
    assert "gh_[REDACTED]" in output
    assert len(output) <= 50000


def test_run_evidence_marker_changes_only_for_new_run(tmp_path):
    current = tmp_path / "runtime" / "current_run.json"
    current.parent.mkdir()
    current.write_text('{"run_id":"run-1","status":"success"}', encoding="utf-8")
    before = run_evidence_marker(current)
    assert run_evidence_marker(current) == before

    current.write_text('{"run_id":"run-2","status":"success"}', encoding="utf-8")
    assert run_evidence_marker(current) != before


def test_cancel_request_is_persisted_at_safe_boundary(tmp_path):
    store = FactoryStore(
        history_path=tmp_path / "runtime" / "control_jobs.json",
        immortal_dir=tmp_path,
        skill_dir=tmp_path,
    )
    store.jobs["job-1"] = {
        "id": "job-1",
        "kind": "health",
        "body": {},
        "status": "running",
        "created_at": "now",
    }

    canceled = store.request_cancel("job-1")

    assert canceled["status"] == "cancel_requested"
    assert store.get_job("job-1")["status"] == "cancel_requested"
    marker = tmp_path / "runtime" / "cancel_requests" / "job-1.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["job_id"] == "job-1"
