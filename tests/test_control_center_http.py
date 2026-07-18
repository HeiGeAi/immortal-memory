from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

from control_center import ControlCenter
from profile_review import FactoryStore, ReviewHandler, ReviewStore, ThreadingHTTPServer


def start_server(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReviewHandler)
    server.store = ReviewStore(
        tmp_path / "proposal.md",
        tmp_path / "memories.jsonl",
        tmp_path / "reviewed.jsonl",
        tmp_path / "review_state.json",
    )
    server.factory = FactoryStore(
        history_path=tmp_path / "runtime" / "control_jobs.json",
        immortal_dir=tmp_path,
        skill_dir=tmp_path,
    )
    server.control_center = ControlCenter(
        tmp_path,
        skill_dir=tmp_path,
        scheduler_probe=lambda: {"status": "unknown", "detail": "test"},
        service_reachable=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.headers.get_content_type(), response.read()


def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def post(url, body, *, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_root_serves_control_center_and_snapshot_route_is_preserved(tmp_path):
    server, base = start_server(tmp_path)
    try:
        status, content_type, body = get(base + "/")
        try:
            get(base + "/snapshot")
            raise AssertionError("retired snapshot unexpectedly returned success")
        except urllib.error.HTTPError as exc:
            snapshot_status = exc.code
            snapshot_body = exc.read()
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert content_type == "text/html"
    assert b"CONTROL" in body
    assert snapshot_status == 410
    assert "已停用".encode() in snapshot_body


def test_state_api_returns_service_proof(tmp_path):
    server, base = start_server(tmp_path)
    try:
        status, _, body = get(base + "/api/control-center/state")
    finally:
        server.shutdown()
        server.server_close()

    payload = json.loads(body)
    assert status == 200
    assert payload["schema_version"] == 1
    assert payload["proofs"][0]["status"] == "healthy"


def test_actions_allow_health_and_reject_unknown_action(tmp_path):
    server, base = start_server(tmp_path)
    try:
        ok_status, ok_payload = post(base + "/api/control-center/actions", {"action": "health"})
        bad_status, bad_payload = post(base + "/api/control-center/actions", {"action": "shell"})
    finally:
        server.shutdown()
        server.server_close()

    assert ok_status == 202
    assert ok_payload["kind"] == "health"
    assert bad_status == 400
    assert bad_payload["error"]["code"] == "invalid_request"
    assert "unknown" in bad_payload["error"]["message"]


def test_actions_reject_non_loopback_origin(tmp_path):
    server, base = start_server(tmp_path)
    try:
        status, payload = post(
            base + "/api/control-center/actions",
            {"action": "health"},
            origin="https://evil.example",
        )
    finally:
        server.shutdown()
        server.server_close()

    assert status == 403
    assert payload["error"]["code"] == "origin_not_allowed"


def test_control_action_commands_are_fixed_allowlist(tmp_path):
    factory = FactoryStore(history_path=tmp_path / "jobs.json")

    health = factory._commands_for("health", {})
    backup = factory._commands_for("backup_verify", {})
    profile = factory._commands_for("profile_refresh", {})

    assert health[0][0][-3:] == ["health", "--max-age-hours", "30"]
    assert "backup-status" in backup[0][0]
    assert "--verify" in backup[0][0]
    assert [command[0][-1] for command in profile] == ["profile", "profile-nuwa", "quality"]


def test_persisted_running_job_is_marked_interrupted_after_restart(tmp_path):
    history_path = tmp_path / "jobs.json"
    history_path.write_text(
        json.dumps(
            [
                {
                    "id": "deadbeef",
                    "kind": "health",
                    "status": "running",
                    "created_at": "2026-07-18T08:00:00+08:00",
                    "started_at": "2026-07-18T08:00:01+08:00",
                }
            ]
        ),
        encoding="utf-8",
    )

    factory = FactoryStore(history_path=history_path)
    restored = factory.get_job("deadbeef")

    assert restored["status"] == "interrupted"
    assert restored["error_code"] == "service_restarted"
    assert "服务重启" in restored["error"]


def test_job_routes_expose_history_detail_and_bounded_logs(tmp_path):
    server, base = start_server(tmp_path)
    server.factory.jobs["job-1"] = {
        "id": "job-1",
        "kind": "health",
        "body": {},
        "status": "failed",
        "created_at": "2026-07-18T08:00:00+08:00",
        "stdout": "a" * 20,
        "stderr": "failure",
    }
    try:
        list_status, jobs = get_json(base + "/api/v1/jobs")
        detail_status, detail = get_json(base + "/api/v1/jobs/job-1")
        logs_status, logs = get_json(base + "/api/v1/jobs/job-1/logs?offset=5&limit=7")
        missing_status, missing = get_json(base + "/api/v1/jobs/missing")
    finally:
        server.shutdown()
        server.server_close()

    assert list_status == 200
    assert [item["id"] for item in jobs["items"]] == ["job-1"]
    assert detail_status == 200
    assert detail["status"] == "failed"
    assert logs_status == 200
    assert logs["offset"] == 5
    assert logs["next_offset"] == 12
    assert logs["text"] == "a" * 7
    assert missing_status == 404
    assert missing["error"]["code"] == "job_not_found"


def test_job_cancel_and_retry_routes_are_real_transitions(tmp_path):
    server, base = start_server(tmp_path)
    server.factory.jobs["running-job"] = {
        "id": "running-job",
        "kind": "health",
        "body": {},
        "status": "running",
        "created_at": "2026-07-18T08:00:00+08:00",
    }
    server.factory.jobs["failed-job"] = {
        "id": "failed-job",
        "kind": "health",
        "body": {},
        "status": "failed",
        "created_at": "2026-07-18T07:00:00+08:00",
    }
    try:
        cancel_status, canceled = post(base + "/api/v1/jobs/running-job/cancel", {})
        retry_status, retried = post(base + "/api/v1/jobs/failed-job/retry", {})
    finally:
        server.shutdown()
        server.server_close()

    assert cancel_status == 202
    assert canceled["status"] == "cancel_requested"
    assert retry_status == 202
    assert retried["id"] != "failed-job"
    assert retried["kind"] == "health"


def test_run_route_returns_conflict_when_orchestrator_is_live(tmp_path):
    (tmp_path / "orchestrator.lock").write_text(f"{os.getpid()} active", encoding="utf-8")
    server, base = start_server(tmp_path)
    try:
        status, payload = post(base + "/api/v1/jobs", {"kind": "run", "params": {}})
    finally:
        server.shutdown()
        server.server_close()

    assert status == 409
    assert payload["error"]["code"] == "job_conflict"
    assert payload["error"]["retryable"] is True


def test_profile_routes_mask_lists_reveal_detail_and_audit_actions(tmp_path):
    memory_id = "b" * 24
    (tmp_path / "proposal.md").write_text(
        f"- [ ] `{memory_id}` candidate\n",
        encoding="utf-8",
    )
    (tmp_path / "memories.jsonl").write_text(
        json.dumps(
            {
                "memory_id": memory_id,
                "statement": "confidential statement",
                "evidence": "confidential evidence",
                "sensitivity": "confidential",
                "source": {"title": "private source"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    server, base = start_server(tmp_path)
    try:
        list_status, page = get_json(base + "/api/v1/profile/candidates")
        detail_status, detail = get_json(base + f"/api/v1/profile/candidates/{memory_id}")
        action_status, action_state = post(
            base + f"/api/v1/profile/candidates/{memory_id}/actions",
            {"action": "approve"},
        )
    finally:
        server.shutdown()
        server.server_close()

    encoded_page = json.dumps(page, ensure_ascii=False)
    assert list_status == 200
    assert "confidential statement" not in encoded_page
    assert page["items"][0]["masked"] is True
    assert detail_status == 200
    assert detail["statement"] == "confidential statement"
    assert action_status == 200
    assert action_state["counts"]["selected"] == 1
    audit = tmp_path / "runtime" / "profile_actions.jsonl"
    assert json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])["action"] == "approve"


def test_agent_backup_and_diagnostic_routes_use_controlled_actions(tmp_path):
    (tmp_path / "runtime").mkdir()
    server, base = start_server(tmp_path)
    try:
        agent_status, agent = get_json(base + "/api/v1/agent")
        backup_status, backups = get_json(base + "/api/v1/backups")
        diagnostic_status, diagnostics = get_json(base + "/api/v1/diagnostics")
        context_status, context_job = post(
            base + "/api/v1/agent/contexts",
            {"goal": "prepare customer plan", "mode": "plan"},
        )
        injection_status, injection = post(
            base + "/api/v1/agent/contexts",
            {"goal": "test", "command": "rm -rf /"},
        )
        verify_status, verify_job = post(base + "/api/v1/backups/verify", {})
        restore_status, restore = post(base + "/api/v1/backups/restore", {})
    finally:
        server.shutdown()
        server.server_close()

    assert agent_status == 200
    assert agent["available"] is False
    assert backup_status == 200
    assert backups["restore_available"] is False
    assert diagnostic_status == 200
    assert diagnostics["listen_address"] == "127.0.0.1"
    assert context_status == 202
    assert context_job["kind"] == "session"
    assert injection_status == 400
    assert injection["error"]["code"] == "invalid_request"
    assert verify_status == 202
    assert verify_job["kind"] == "backup_verify"
    assert restore_status == 404
    assert restore["error"]["code"] == "not_found"
