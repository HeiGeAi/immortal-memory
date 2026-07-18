from __future__ import annotations

import json
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
    server.factory = FactoryStore(history_path=tmp_path / "runtime" / "control_jobs.json")
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
        snapshot_status, _, snapshot_body = get(base + "/snapshot")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert content_type == "text/html"
    assert b"CONTROL" in body
    assert snapshot_status in {200, 404}
    if snapshot_status == 404:
        assert b"snapshot not found" in snapshot_body


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
    assert "unknown" in bad_payload["error"]


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
    assert "origin" in payload["error"].lower()


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

    assert restored["status"] == "failed"
    assert "服务重启" in restored["error"]
