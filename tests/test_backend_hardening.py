"""Regression checks for the daily backend without touching the live vault."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import export_restore
import feishu_collect
import orchestrator


FAKE_SECRET = "sk-" + "a" * 48


def test_json_script_failure_logs_redacted_stderr_tail():
    stderr = "\n".join(["old line"] * 5 + [f"failure {FAKE_SECRET}"] + ["recent"] * 19)
    result = SimpleNamespace(returncode=1, stdout="", stderr=stderr)
    with mock.patch.object(orchestrator, "run_process", return_value=result), mock.patch.object(
        orchestrator, "log"
    ) as log:
        ok, out = orchestrator.run_script("export_restore.py", want_stdout=True)
    assert not ok and out == ""
    logged = log.call_args.args[0]
    assert "old line" not in logged
    assert "failure" in logged
    assert FAKE_SECRET not in logged
    assert len(logged.splitlines()) == 21


def test_log_writes_once_without_redirected_stdout(tmp_path, capsys):
    with mock.patch.object(orchestrator, "LOG_FILE", tmp_path / "backup.log"), mock.patch.object(
        orchestrator.sys.stdout, "isatty", return_value=False
    ):
        orchestrator.log("single entry")
    assert capsys.readouterr().out == ""
    assert (tmp_path / "backup.log").read_text().count("single entry") == 1


def test_daily_portable_export_requests_redaction():
    with mock.patch.object(orchestrator, "run_script", return_value=(False, "")) as run, mock.patch.object(
        orchestrator, "log"
    ):
        orchestrator.portable_export()
    assert "--redact-secrets" in run.call_args.args


def test_redacted_export_publishes_atomically_and_restores(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.jsonl").write_text(json.dumps({"content": FAKE_SECRET}) + "\n")
    exports = tmp_path / "exports"
    manifest = export_restore.create_export(vault, exports, redact_secrets=True)
    final_dir = Path(manifest["export_dir"])
    assert final_dir.is_dir()
    assert not list(exports.glob("*.partial"))
    assert FAKE_SECRET not in (final_dir / "index.jsonl").read_text()
    assert export_restore.restore_check(final_dir, strict=True)["ok"] is True


def test_failed_export_removes_only_its_partial(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.jsonl").write_text("{}\n")
    exports = tmp_path / "exports"
    unrelated = exports / "unrelated.partial"
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_text("safe")

    def fail_copy(_source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("incomplete")
        raise OSError("copy failed")

    with mock.patch.object(export_restore, "copy_file", side_effect=fail_copy):
        try:
            export_restore.create_export(vault, exports)
        except OSError:
            pass
        else:
            assert False, "copy failure must propagate"
    assert (unrelated / "keep").read_text() == "safe"
    assert not list(exports.glob("immortal-export-*"))


def test_manifest_failure_cleans_partial_after_all_copies(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.jsonl").write_text("{}\n")
    exports = tmp_path / "exports"
    original = export_restore.write_json_atomic

    def fail_manifest(path, payload):
        if path.name == export_restore.MANIFEST_NAME:
            raise OSError("manifest failed")
        return original(path, payload)

    with mock.patch.object(export_restore, "write_json_atomic", side_effect=fail_manifest):
        try:
            export_restore.create_export(vault, exports)
        except OSError:
            pass
        else:
            assert False, "manifest failure must propagate"
    assert not list(exports.iterdir())


def test_latest_export_ignores_orphan_partial(tmp_path):
    vault = tmp_path / "vault"
    partial = vault / "exports" / "immortal-export-20260926T000000Z.partial"
    partial.mkdir(parents=True)
    (partial / export_restore.MANIFEST_NAME).write_text("{}")
    assert not export_restore.find_latest_export(vault)["export_dir"]


def make_collector(tmp_path):
    collector = object.__new__(feishu_collect.Collector)
    collector.conn = sqlite3.connect(":memory:")
    collector.conn.execute("create table runs (run_id text primary key, finished_at text, stats_json text, errors_json text)")
    collector.conn.execute("insert into runs(run_id) values ('run-1')")
    collector.run_id = "run-1"
    collector.stats = {}
    collector.errors = []
    collector.skips = []
    collector.start = datetime(2026, 9, 24, tzinfo=timezone.utc)
    collector.end = datetime(2026, 9, 26, tzinfo=timezone.utc)
    return collector


def test_truncation_records_source_and_keeps_window_watermark(tmp_path):
    collector = make_collector(tmp_path)
    state = {"last_window_end": "2026-09-23T00:00:00+08:00"}
    saved = {}
    with mock.patch.object(feishu_collect, "log_event"), mock.patch.object(
        feishu_collect, "read_json", return_value=state
    ), mock.patch.object(feishu_collect, "write_json", side_effect=lambda _path, value: saved.update(value)), mock.patch.object(
        feishu_collect, "update_sources_backup"
    ):
        collector.truncated("feishu-im", "message_page_limit")
        collector.finish_run()
    assert collector.errors[0]["source"] == "feishu-im"
    assert "message_page_limit" in collector.errors[0]["message"]
    assert saved["last_window_end"] == state["last_window_end"]


def test_message_page_limit_is_reported_as_truncation():
    collector = make_collector(None)
    collector.args = SimpleNamespace(
        message_page_size=50, message_page_limit=1, page_delay=0,
        max_messages=0, flush_size=100,
    )
    collector.chats = [{"chat_id": "chat-1", "name": "chat"}]
    collector.conn.execute(
        "create table seen (record_key text primary key, source text, first_seen_at text)"
    )
    collector.add_records = mock.Mock()
    payload = {"data": {"messages": [], "has_more": True, "page_token": "next"}}
    with mock.patch.object(feishu_collect, "run_lark", return_value=(True, payload, "")), mock.patch.object(
        feishu_collect, "log_event"
    ):
        collector.collect_messages()
    assert collector.incomplete_window is True
    assert collector.errors[0]["source"] == "feishu-im"
    assert "message_page_limit" in collector.errors[0]["message"]


def test_runtime_auth_failure_is_hard_and_keeps_window_watermark():
    collector = make_collector(None)
    state = {"last_window_end": "2026-09-23T00:00:00+08:00"}
    saved = {}
    with mock.patch.object(feishu_collect, "log_event"), mock.patch.object(
        feishu_collect, "read_json", return_value=state
    ), mock.patch.object(feishu_collect, "write_json", side_effect=lambda _path, value: saved.update(value)), mock.patch.object(
        feishu_collect, "update_sources_backup"
    ):
        collector.error("feishu-im", "99991663 invalid access token")
        collector.finish_run()
    assert feishu_collect.run_exit_code(collector.errors) == 1
    assert saved["last_window_end"] == state["last_window_end"]
    assert orchestrator.orchestration_status(["feishu auth failed"]) == ("failed", 1)


def test_startup_auth_failure_and_orchestrator_stage_are_hard():
    collector = make_collector(None)
    with mock.patch.object(feishu_collect, "current_auth_status", return_value=(False, {}, "expired")):
        try:
            collector.start_run()
        except RuntimeError as exc:
            assert "AUTH_FAILURE" in str(exc)
        else:
            assert False, "startup auth failure must stop collection"
    with mock.patch.object(orchestrator, "feishu_daily_args", return_value=["--x"]), mock.patch.object(
        orchestrator, "run_script_rc", return_value=(1, "AUTH_FAILURE: expired")
    ), mock.patch.object(orchestrator, "log"):
        status, _ = orchestrator.collect_feishu()
    assert status == "auth_failed"


def test_freeze_config_is_opt_in_and_protects_collection(tmp_path):
    assert orchestrator.DEFAULT_FROZEN_STAGES == frozenset()
    (tmp_path / "config.json").write_text(json.dumps({"pipeline": {"frozen_stages": ["summary", "collect"]}}))
    with mock.patch.object(orchestrator, "IMMORTAL_DIR", tmp_path), mock.patch.object(orchestrator, "log") as log:
        frozen = orchestrator.frozen_stages()
        orchestrator._FROZEN_LOGGED.clear()
        assert not orchestrator.stage_enabled("summary", frozen)
        assert not orchestrator.stage_enabled("summary", frozen)
    assert frozen == {"summary"}
    log.assert_called_once_with("summary: skipped (frozen)")


def test_freezing_feishu_clean_also_freezes_its_downstream_consumers(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"pipeline": {"frozen_stages": ["feishu_clean"]}}))
    with mock.patch.object(orchestrator, "IMMORTAL_DIR", tmp_path):
        assert orchestrator.frozen_stages() == {
            "feishu_clean", "feishu_distill", "feishu_auto_review", "feishu_attribution"
        }


def test_zero_collection_only_fails_after_48h_since_last_nonempty():
    now = datetime.now(timezone.utc)
    state = {"last_collect": (now - timedelta(hours=2)).isoformat()}
    errors = []
    with mock.patch.object(orchestrator, "log"):
        orchestrator.record_collect_outcome(state, True, {"total_new": 0}, now.isoformat(), errors)
    assert errors == []
    stale = {"last_nonempty_collect": (now - timedelta(hours=49)).isoformat(), "last_collect": now.isoformat()}
    with mock.patch.object(orchestrator, "log"):
        orchestrator.record_collect_outcome(stale, True, {"total_new": 0}, now.isoformat(), errors)
    assert errors == ["collect stale: no new records for over 48 hours"]
    assert orchestrator.orchestration_status(errors) == ("failed", 1)
    assert stale["last_collect"] == now.isoformat()


def test_snapshot_source_truncation_does_not_hold_the_window_cursor():
    """群成员上限是常规截断，拦游标会让时间窗口永远不前进。"""
    import types

    import feishu_collect

    c = feishu_collect.Collector.__new__(feishu_collect.Collector)
    c.errors, c.skips = [], []
    feishu_collect.Collector.truncated(c, "feishu-chat-member", "max_members limit")
    assert not getattr(c, "incomplete_window", False)
    assert c.errors and c.errors[0]["source"] == "feishu-chat-member"
    feishu_collect.Collector.truncated(c, "feishu-im", "max_messages limit at 1000")
    assert c.incomplete_window is True
