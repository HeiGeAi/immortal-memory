from __future__ import annotations

import http.client
import json
import subprocess
import threading
import urllib.error
import urllib.request

import profile_review
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


def raw_get(base: str, path: str, *, host: str) -> tuple[int, dict[str, str], bytes]:
    port = int(base.rsplit(":", 1)[-1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers={"Host": host})
    response = connection.getresponse()
    body = response.read()
    headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, headers, body


def test_non_loopback_host_is_rejected_with_structured_error(tmp_path):
    server, base = start_server(tmp_path)
    try:
        status, headers, body = raw_get(base, "/api/control-center/state", host="attacker.example")
    finally:
        server.shutdown()
        server.server_close()

    payload = json.loads(body)
    assert status == 403
    assert payload["error"]["code"] == "host_not_allowed"
    assert payload["error"]["retryable"] is False
    assert headers["x-frame-options"] == "DENY"


def test_loopback_host_and_security_headers_are_allowed(tmp_path):
    server, base = start_server(tmp_path)
    port = int(base.rsplit(":", 1)[-1])
    try:
        status, headers, _ = raw_get(base, "/healthz", host=f"localhost:{port}")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"].endswith("frame-ancestors 'none'")


def test_missing_snapshot_is_gone_not_fake_success(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_review, "DEFAULT_DASHBOARD", tmp_path / "missing-dashboard.html")
    server, base = start_server(tmp_path)
    try:
        with urllib.request.urlopen(base + "/snapshot", timeout=5):
            raise AssertionError("missing snapshot unexpectedly returned success")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        assert exc.code == 410
        assert "已停用" in body
    finally:
        server.shutdown()
        server.server_close()


def test_timeline_redirects_to_live_memory_view(tmp_path):
    server, base = start_server(tmp_path)
    port = int(base.rsplit(":", 1)[-1])
    try:
        status, headers, _ = raw_get(base, "/timeline", host=f"127.0.0.1:{port}")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 303
    assert headers["location"] == "/?view=memories&mode=timeline"


def test_agent_entry_get_has_no_subprocess_side_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_review, "DEFAULT_AGENT_ENTRY", tmp_path / "missing-entry.md")

    def forbidden_run(*args, **kwargs):
        raise AssertionError("GET route spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    server, base = start_server(tmp_path)
    try:
        with urllib.request.urlopen(base + "/agent-entry", timeout=5):
            raise AssertionError("missing entry unexpectedly returned success")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
        assert "尚未生成" in exc.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
