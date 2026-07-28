from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

import export_restore
import immortal
import profile_review
from control_center import ControlCenter
from control_data import ControlData
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
    server.control_data = ControlData(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_healthz_is_lightweight_and_readyz_reports_dependencies(tmp_path):
    (tmp_path / "runtime").mkdir()
    server, base = start_server(tmp_path)
    try:
        health_status, health = get_json(base + "/healthz")
        ready_status, ready = get_json(base + "/readyz")
        capability_status, capabilities = get_json(base + "/api/v1/capabilities")
    finally:
        server.shutdown()
        server.server_close()

    assert health_status == 200
    assert health == {"status": "ok"}
    assert ready_status == 200
    assert ready["status"] == "ready"
    assert capability_status == 200
    assert {item["id"] for item in capabilities["modules"]} >= {
        "overview",
        "runs",
        "sources",
        "memories",
        "profile",
        "agent",
        "backup",
        "diagnostics",
    }


def test_readyz_returns_503_when_vault_is_missing(tmp_path):
    server, base = start_server(tmp_path)
    server.control_data.immortal_dir = tmp_path / "missing-vault"
    try:
        status, payload = get_json(base + "/readyz")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 503
    assert payload["status"] == "not_ready"
    assert payload["checks"]["vault_readable"] is False


def test_backups_exposes_redacted_cloud_recovery_state(tmp_path, monkeypatch):
    data = ControlData(tmp_path, skill_dir=tmp_path)
    monkeypatch.setattr(
        export_restore,
        "get_feishu_recovery_backup_status",
        lambda _vault: {
            "ok": True,
            "storage_location": "external_cloud",
            "provider": "feishu_drive",
            "generated_at": "2026-07-22T00:00:00Z",
            "verification": {
                "mode": "remote-download-sha256+decrypt-restore",
                "ok": True,
            },
            "recovery_drill": {"ok": True, "mode": "decrypt-restore"},
            "source_binding": {"ok": True},
            "warnings": [],
        },
    )

    cloud = data.backups()["cloud_recovery"]

    assert cloud == {
        "status": "verified",
        "provider": "Feishu Drive",
        "last_verified_at": "2026-07-22T00:00:00Z",
        "verification": "remote-download-sha256+decrypt-restore",
        "source_binding": "matched",
        "reason_code": "",
        "action": "无需操作",
    }
    encoded = json.dumps(cloud, ensure_ascii=False)
    assert "folder_token" not in encoded
    assert "path" not in encoded


def test_backups_marks_pending_drill_without_exposing_remote_receipt(tmp_path, monkeypatch):
    data = ControlData(tmp_path, skill_dir=tmp_path)
    monkeypatch.setattr(
        export_restore,
        "get_feishu_recovery_backup_status",
        lambda _vault: {
            "ok": False,
            "storage_location": "external_cloud",
            "provider": "feishu_drive",
            "generated_at": "2026-07-22T00:00:00Z",
            "verification": {
                "mode": "remote-download-sha256+decrypt-restore",
                "ok": False,
            },
            "recovery_drill": {"ok": False, "mode": "decrypt-restore"},
            "source_binding": {"ok": False},
            "warnings": ["cloud_recovery_drill_pending"],
        },
    )

    cloud = data.backups()["cloud_recovery"]

    assert cloud["status"] == "attention"
    assert cloud["reason_code"] == "cloud_recovery_drill_pending"
    assert cloud["action"] == "在终端执行恢复演练"


def test_dashboard_server_scopes_default_paths_to_requested_vault(tmp_path):
    vault = tmp_path / "isolated-vault"
    vault.mkdir()
    (vault / "runtime").mkdir()
    (vault / "index.jsonl").write_text(
        '{"id":"m1","timestamp":"2026-07-22T00:00:00Z","content":"safe"}\n',
        encoding="utf-8",
    )
    args = profile_review.build_parser().parse_args(
        ["--vault-dir", str(vault), "--port", "0"]
    )

    server = profile_review.create_server(args)
    try:
        assert server.control_center.immortal_dir == vault
        assert server.control_data.immortal_dir == vault
        assert server.factory.immortal_dir == vault
        assert server.store.proposal == vault / "feishu/distilled/profile_merge_proposal.md"
        assert server.store.memories == vault / "feishu/distilled/profile_memories.jsonl"
        assert server.store.reviewed == vault / "reviewed/profile_memories.jsonl"
        assert server.store.review_state == vault / "reviewed/profile_review_state.json"
        assert server.factory.allow_commands is False
        assert server.control_data.capabilities()["actions"] == []
        assert server.factory.snapshot()["commands"] == {}
        with pytest.raises(profile_review.JobConflict, match="隔离 vault"):
            server.factory.start_job("health", {})
        scheduler = server.control_center.build_snapshot()["scheduler"]
        assert scheduler["status"] == "unknown"
        assert scheduler["source"] == "isolated_vault"
    finally:
        server.server_close()


def test_factory_snapshot_reads_its_configured_vault(tmp_path, monkeypatch):
    vault = tmp_path / "isolated-vault"
    vault.mkdir()
    (vault / "index.jsonl").write_text(
        '{"id":"m1","timestamp":"2026-07-22T00:00:00Z","content":"safe"}\n',
        encoding="utf-8",
    )
    other = tmp_path / "other-vault"
    other.mkdir()
    monkeypatch.setattr(profile_review, "IMMORTAL_DIR", other)
    monkeypatch.setattr(profile_review, "DEFAULT_SESSIONS_DIR", other / "sessions")

    snapshot = profile_review.FactoryStore(
        history_path=vault / "runtime/control_jobs.json",
        immortal_dir=vault,
        skill_dir=tmp_path,
    ).snapshot()

    assert snapshot["layers"]["index"]["exists"] is True


def test_agent_factory_forwards_explicit_vault_to_dashboard(tmp_path, monkeypatch):
    vault = tmp_path / "isolated-vault"
    captured = {}

    def fake_run_script(script, args=None):
        captured["script"] = script
        captured["args"] = args
        return 0

    monkeypatch.setattr(immortal, "run_script", fake_run_script)
    args = immortal.build_parser().parse_args(
        ["agent-factory", "--vault-dir", str(vault), "--port", "0"]
    )

    assert immortal.command_agent_factory(args) == 0
    assert captured == {
        "script": "profile_review.py",
        "args": ["--host", "127.0.0.1", "--port", "0", "--vault-dir", str(vault)],
    }


def test_agent_context_forwards_review_lifecycle_arguments(monkeypatch):
    captured = {}

    def fake_run_script(script, args=None):
        captured["script"] = script
        captured["args"] = args
        return 0

    monkeypatch.setattr(immortal, "run_script", fake_run_script)
    args = immortal.build_parser().parse_args(
        [
            "agent-context",
            "review task",
            "--mode",
            "reviewer",
            "--preview-id",
            "prv_one",
            "--preview-hash",
            "sha256:" + "1" * 64,
            "--exclude-item-id",
            "clm_private",
            "--ttl-seconds",
            "600",
            "--metadata-output",
            "/tmp/immortal-context.json",
            "--print",
        ]
    )

    assert immortal.command_agent_context(args) == 0
    assert captured == {
        "script": "agent_bridge.py",
        "args": [
            "context",
            "review task",
            "--since",
            "2026-03-01",
            "--timeout",
            "240",
            "--mode",
            "reviewer",
            "--preview-id",
            "prv_one",
            "--preview-hash",
            "sha256:" + "1" * 64,
            "--exclude-item-id",
            "clm_private",
            "--ttl-seconds",
            "600",
            "--metadata-output",
            "/tmp/immortal-context.json",
            "--print",
        ],
    }


def test_health_uses_judgment_build_state_not_retired_cards_file(
    tmp_path, monkeypatch, capsys
):
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "total_records": 1,
        "errors": [],
        "last_obsidian_sync_status": "ok",
        "last_portable_restore_check_status": "ok",
        "last_portable_restore_check_files": 1,
        "last_portable_restore_check_dir": str(tmp_path / "export"),
    }
    for key in (
        "last_collect",
        "last_feishu_collect",
        "last_feishu_clean",
        "last_feishu_distill",
        "last_profile",
        "last_profile_nuwa",
        "last_people_index",
        "last_relationship_index",
        "last_quality",
        "last_profile_attribution_audit",
        "last_feishu_mirror_inventory",
        "last_feishu_mirror_download",
        "last_agent_entry",
        "last_web_collect",
        "last_obsidian_sync",
        "last_cards_build",
        "last_portable_restore_check",
    ):
        state[key] = now

    files = {
        "STATE_FILE": ("state.json", state),
        "QUALITY_JSON": (
            "quality.json",
            {"status": "ok", "score": 100, "issue_count": 0},
        ),
        "WEB_STATE_JSON": (
            "web.json",
            {"status": "ok", "totals": {"errors": 0}},
        ),
        "OBSIDIAN_STATUS_JSON": (
            "obsidian.json",
            {"status": "ok", "broken_links_count": 0},
        ),
        "RELATIONSHIP_JSON": (
            "relationships.json",
            {"summary": {"person_edges": 1, "project_edges": 1}},
        ),
    }
    for name, (filename, payload) in files.items():
        path = tmp_path / filename
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(immortal, name, path)

    fresh = tmp_path / "fresh.txt"
    fresh.write_text("x" * 2048, encoding="utf-8")
    for name in (
        "QUALITY_MD",
        "PROFILE_ATTRIBUTION_AUDIT_JSON",
        "PROFILE_NUWA_JSON",
        "PROFILE_NUWA_MD",
        "AGENT_ENTRY_MD",
        "AGENT_ENTRY_JSON",
        "FEISHU_CLEAN_COVERAGE",
        "FEISHU_DISTILLED_COVERAGE",
        "FEISHU_DRIVE_MIRROR_COVERAGE",
    ):
        monkeypatch.setattr(immortal, name, fresh)
    retired = tmp_path / "cards_compact.md"
    retired.write_text("legacy" * 100, encoding="utf-8")
    os.utime(retired, (1, 1))
    monkeypatch.setattr(immortal, "CARDS_COMPACT_MD", retired, raising=False)
    monkeypatch.setattr(immortal, "check_crontab", lambda: (True, ["scheduler"]))
    monkeypatch.setattr(
        immortal,
        "get_backup_status",
        lambda _vault: {
            "ok": True,
            "mode": "manifest-only",
            "latest_export": {
                "generated_at": now,
                "export_dir": str(tmp_path / "export"),
                "totals": {"files": 1, "bytes": 1},
            },
        },
    )

    code = immortal.command_health(type("Args", (), {"max_age_hours": 30})())
    output = capsys.readouterr().out

    assert code == 0
    assert "OK   判断力卡片盒" in output
    assert "FAIL 判断力卡片盒" not in output


def test_raw_context_does_not_inject_retired_cards_cache(
    tmp_path, monkeypatch, capsys
):
    missing = tmp_path / "missing"
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"last_collect": "2026-07-28T00:00:00Z", "total_records": 1}),
        encoding="utf-8",
    )
    retired = tmp_path / "cards_compact.md"
    retired.write_text("RETIRED_UNREVIEWED_JUDGMENT", encoding="utf-8")
    monkeypatch.setattr(immortal, "STATE_FILE", state)
    monkeypatch.setattr(immortal, "CARDS_COMPACT_MD", retired, raising=False)
    for name in (
        "PROFILE_COMPACT_MD",
        "PROFILE_MD",
        "SOUL_FILE",
        "PROFILE_NUWA_MD",
        "PEOPLE_MD",
    ):
        monkeypatch.setattr(immortal, name, missing)

    code = immortal.command_context(
        type(
            "Args",
            (),
            {
                "query": "task",
                "since": "2026-03-01",
                "with_recall": False,
                "profile_lines": 10,
                "nuwa_lines": 10,
                "digest_lines": 10,
                "people_lines": 10,
            },
        )()
    )

    assert code == 0
    assert "RETIRED_UNREVIEWED_JUDGMENT" not in capsys.readouterr().out


def test_v1_overview_reuses_truth_snapshot(tmp_path):
    server, base = start_server(tmp_path)
    try:
        legacy_status, legacy = get_json(base + "/api/control-center/state")
        v1_status, v1 = get_json(base + "/api/v1/overview")
    finally:
        server.shutdown()
        server.server_close()

    assert legacy_status == 200
    assert v1_status == 200
    legacy_generated_at = datetime.fromisoformat(legacy.pop("generated_at"))
    v1_generated_at = datetime.fromisoformat(v1.pop("generated_at"))
    assert v1 == legacy
    assert abs((v1_generated_at - legacy_generated_at).total_seconds()) <= 2


def write_index(root, count=60):
    rows = [
        {
            "id": f"memory-{index}",
            "timestamp": f"2026-07-{(index % 18) + 1:02d}T08:00:00Z",
            "source": "feishu-im" if index % 2 else "codex",
            "type": "message",
            "sensitivity": "confidential",
            "content": f"private memory body {index}",
        }
        for index in range(count)
    ]
    (root / "index.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_memory_list_is_bounded_and_omits_full_content(tmp_path):
    write_index(tmp_path)
    data = ControlData(tmp_path)

    page = data.memories({"limit": ["20"], "offset": ["0"]})
    encoded = json.dumps(page, ensure_ascii=False)

    assert len(page["items"]) == 20
    assert page["total"] == 60
    assert page["limit"] == 20
    assert all("content" not in item and "evidence" not in item for item in page["items"])
    assert "private memory body 59" not in encoded


def test_memory_filters_and_detail_use_stable_id(tmp_path):
    write_index(tmp_path, count=6)
    data = ControlData(tmp_path)

    page = data.memories({"q": ["body 3"], "source": ["feishu-im"], "limit": ["100"]})
    detail = data.memory_detail("memory-3")

    assert page["limit"] == 50
    assert [item["id"] for item in page["items"]] == ["memory-3"]
    assert detail["id"] == "memory-3"
    assert detail["content"] == "private memory body 3"


def test_memory_detail_rejects_path_identifiers(tmp_path):
    data = ControlData(tmp_path)

    with pytest.raises(ValueError):
        data.memory_detail("../../profile.json")


def test_sources_keep_partial_and_skipped_distinct_from_success(tmp_path):
    (tmp_path / "orchestrator_state.json").write_text(
        json.dumps(
            {
                "last_collect": "2026-07-18T08:00:00Z",
                "last_run_new_records": 4,
                "last_feishu_collect": "2026-07-18T07:00:00Z",
                "last_feishu_status": "partial",
                "last_run_feishu_new_records": 2,
                "last_obsidian_sync_status": "disabled",
            }
        ),
        encoding="utf-8",
    )
    data = ControlData(tmp_path)

    sources = {item["id"]: item for item in data.sources()["items"]}

    assert sources["local"]["status"] == "success"
    assert sources["feishu"]["status"] == "partial"
    assert sources["obsidian"]["status"] == "skipped"


def test_memory_and_source_routes_are_live(tmp_path):
    write_index(tmp_path, count=3)
    server, base = start_server(tmp_path)
    try:
        source_status, sources = get_json(base + "/api/v1/sources")
        list_status, page = get_json(base + "/api/v1/memories?limit=2")
        detail_status, detail = get_json(base + "/api/v1/memories/memory-1")
        bad_status, bad = get_json(base + "/api/v1/memories/..%2Fprofile.json")
    finally:
        server.shutdown()
        server.server_close()

    assert source_status == 200
    assert len(sources["items"]) == 11
    assert list_status == 200
    assert len(page["items"]) == 2
    assert detail_status == 200
    assert detail["id"] == "memory-1"
    assert bad_status == 400
    assert bad["error"]["code"] == "invalid_memory_id"


def test_memory_api_uses_sqlite_index_without_scanning_jsonl(tmp_path, monkeypatch):
    db = tmp_path / "search_index.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "create table docs("
            "rowid integer primary key, rec_id text, ts text, source text, "
            "role text, project text, content text)"
        )
        connection.execute("create table meta(key text primary key, value text)")
        connection.execute(
            "insert into docs(rec_id,ts,source,role,project,content) values(?,?,?,?,?,?)",
            ("fast-1", "2026-07-18T08:00:00Z", "codex", "assistant", "immortal", "fast indexed memory"),
        )
        connection.execute("insert into meta(key,value) values('last_size','0')")
    data = ControlData(tmp_path)
    monkeypatch.setattr(
        data,
        "_iter_memories",
        lambda: (_ for _ in ()).throw(AssertionError("jsonl scan used")),
    )

    page = data.memories({"limit": ["20"]})
    detail = data.memory_detail("fast-1")

    assert page["total"] == 1
    assert page["items"][0]["id"] == "fast-1"
    assert page["backend"] == "sqlite"
    assert detail["content"] == "fast indexed memory"
