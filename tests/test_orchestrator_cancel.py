from __future__ import annotations

import json

from orchestrator import control_job_cancel_requested


def test_control_job_cancel_request_is_scoped_to_its_job(tmp_path):
    cancel_dir = tmp_path / "cancel_requests"
    cancel_dir.mkdir()
    (cancel_dir / "job-a.json").write_text(
        json.dumps({"job_id": "job-a"}),
        encoding="utf-8",
    )

    assert control_job_cancel_requested("job-a", runtime_dir=tmp_path) is True
    assert control_job_cancel_requested("job-b", runtime_dir=tmp_path) is False
    assert control_job_cancel_requested("", runtime_dir=tmp_path) is False
