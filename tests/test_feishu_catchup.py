from datetime import datetime, timedelta, timezone
import json

import pytest

import config
import feishu_catchup as catchup
import feishu_collect as collector
import orchestrator
from feishu_lock import collector_lock


NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)
SEED = NOW - timedelta(days=4)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "FEISHU_DIR", tmp_path)
    monkeypatch.setattr(collector, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(catchup, "feishu_message_catchup_enabled", lambda: True)
    monkeypatch.setattr(catchup, "feishu_guard_args", lambda: ["--expected-user-name", "Example Editor"])
    collector.write_json(collector.STATE_FILE, {"last_window_end": SEED.isoformat(), "last_errors": ["fixture"]})
    return tmp_path


def test_opt_in_requires_messages():
    assert not config.feishu_message_catchup_enabled({"feishu": {"daily_sources": "messages"}})
    assert not config.feishu_message_catchup_enabled({"feishu": {"message_catchup_enabled": True, "daily_sources": "message-search"}})
    assert config.feishu_message_catchup_enabled({"feishu": {"message_catchup_enabled": True, "daily_sources": "docs, messages"}})


def test_busy_preserves_receipt(sandbox):
    path = sandbox / "catchup-messages-status.json"
    collector.write_json(path, {"status": "running", "marker": "owner"})
    previous = path.read_bytes()
    with collector_lock(sandbox):
        assert catchup.run(now=NOW)["status"] == "busy"
    assert path.read_bytes() == previous


def test_no_guard_and_no_legacy_cursor(sandbox, monkeypatch):
    monkeypatch.setattr(catchup, "feishu_guard_args", lambda: ["--reject-user-name", "Other account"])
    assert catchup.run(now=NOW)["reason"] == "expected_account_required"
    assert not (sandbox / "catchup-messages.json").exists()
    monkeypatch.setattr(catchup, "feishu_guard_args", lambda: ["--expected-user-name", "Example Editor"])
    collector.STATE_FILE.unlink()
    assert catchup.run(now=NOW)["status"] == "not_initialized"


def test_window_bounded_preserves_legacy_and_partial_cursor(sandbox, monkeypatch):
    state = collector.STATE_FILE.read_bytes()
    seen = []

    class Connection:
        def close(self):
            pass

    class FakeCollector:
        def __init__(self, args, *, state_file):
            seen.append(args)
            self.end = args.until
            self.path = state_file
            self.conn = Connection()
            self.errors = []
            self.stats = {"feishu-im": 2}
            self.incomplete_window = False

        def run(self):
            if len(seen) == 2:
                self.errors.append("incomplete")
            else:
                collector.write_json(self.path, {"last_window_end": self.end})

    monkeypatch.setattr(collector, "Collector", FakeCollector)
    result = catchup.run(now=NOW)
    assert result["status"] == "partial"
    assert result["windows_completed"] == 1
    assert collector.parse_dt(result["cursor_after"]) == SEED + timedelta(hours=23)
    assert collector.parse_dt(seen[1].since) == SEED + timedelta(hours=22)
    assert all(args.expected_user_name == "Example Editor" and not args.allow_current_account for args in seen)
    assert collector.STATE_FILE.read_bytes() == state


def test_finish_window_after_aborted_buffer_never_advances(sandbox, monkeypatch):
    checkpoint = sandbox / "catchup-messages.json"
    collector.write_json(checkpoint, {"last_window_end": SEED.isoformat()})
    events = []

    class Connection:
        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

        def execute(self, *args):
            pass

        def commit(self):
            events.append("commit")

    monkeypatch.setattr(collector, "db", Connection)
    monkeypatch.setattr(collector, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(collector, "update_sources_backup", lambda *_a, **_k: pytest.fail("legacy summary touched"))
    monkeypatch.setattr(collector.Collector, "start_run", lambda self: None)

    def abort(self):
        raise catchup.BudgetExpired

    monkeypatch.setattr(collector.Collector, "collect_chats", abort)
    args = collector.build_parser().parse_args([
        "--sources", "messages", "--since", SEED.isoformat(), "--until", NOW.isoformat(),
    ])
    with pytest.raises(catchup.BudgetExpired):
        collector.Collector(args, state_file=checkpoint).run()
    assert events == ["rollback", "commit", "close"]
    assert json.loads(checkpoint.read_text())["last_window_end"] == SEED.isoformat()


@pytest.mark.parametrize(("response", "marker"), [
    ((False, {}, "timeout after 30s"), "AUTH_CHECK_UNAVAILABLE"),
    ((False, {}, "token expired"), "AUTH_FAILURE"),
    ((True, {"userName": "Different Account"}, ""), "AUTH_FAILURE"),
])
def test_identity_fail_closed(monkeypatch, response, marker):
    monkeypatch.setattr(collector, "current_auth_status", lambda: response)
    worker = object.__new__(collector.Collector)
    worker.args = collector.build_parser().parse_args(["--expected-user-name", "Example Editor"])
    with pytest.raises(RuntimeError, match=marker):
        worker.start_run()


def test_health_distinguishes_active_and_failed(sandbox):
    path = sandbox / "catchup-messages-status.json"
    collector.write_json(path, {"status": "running", "started_at": NOW.isoformat(), "budget_seconds": 1200})
    assert catchup.health(path, now=NOW + timedelta(minutes=10))[0] == "warn"
    assert catchup.health(path, now=NOW + timedelta(minutes=26))[0] == "fail"
    collector.write_json(path, {"status": "error"})
    assert catchup.health(path, now=NOW)[0] == "fail"


def test_catchup_precedes_local_scan(monkeypatch):
    order = []
    monkeypatch.setattr(orchestrator, "preflight_required_scripts", list)
    monkeypatch.setattr(orchestrator, "load_state", dict)
    monkeypatch.setattr(orchestrator, "frozen_stages", set)
    monkeypatch.setattr(orchestrator, "log", lambda *_: None)
    monkeypatch.setattr(orchestrator, "telemetry_stage", lambda *_: None)
    monkeypatch.setattr(orchestrator, "feishu_message_catchup_enabled", lambda: True)

    def local():
        order.append("local")
        raise StopIteration

    monkeypatch.setattr(orchestrator, "collect_feishu_catchup", lambda: order.append("catchup") or True)
    monkeypatch.setattr(orchestrator, "collect", local)
    with pytest.raises(StopIteration):
        orchestrator.run_main()
    assert order == ["catchup", "local"]


def test_real_collector_writes_synthetic_message_and_advances(sandbox, monkeypatch):
    for attr, name in {
        "DAILY_DIR": "daily",
        "INDEX_FILE": "index.jsonl",
        "SOURCES_FILE": "sources.json",
        "DB_FILE": "state.sqlite3",
        "LOG_FILE": "events.jsonl",
    }.items():
        monkeypatch.setattr(collector, attr, sandbox / name)
    monkeypatch.setattr(collector, "IMMORTAL_DIR", sandbox)
    (sandbox / "daily").mkdir()
    legacy = collector.STATE_FILE.read_bytes()

    def cli(args, **_kwargs):
        if args[:2] == ["auth", "status"]:
            return True, {"userName": "Example Editor"}, ""
        if args[:2] == ["im", "+chat-list"]:
            return True, {"data": {"chats": [{"chat_id": "example-room", "name": "Fixture room"}]}}, ""
        if args[:2] == ["im", "+chat-messages-list"]:
            return True, {"data": {"items": [{
                "message_id": "synthetic-message", "msg_type": "text",
                "content": "Synthetic recovery example", "create_time": str(int(SEED.timestamp() * 1000)),
            }]}}, ""
        pytest.fail("unexpected synthetic CLI request")

    monkeypatch.setattr(collector, "run_lark", cli)
    result = catchup.run(now=NOW, max_windows=1)
    assert result["status"] == "ok"
    assert result["windows_completed"] == 1
    assert collector.parse_dt(result["cursor_after"]) == SEED + timedelta(hours=23)
    lines = [json.loads(line) for line in collector.INDEX_FILE.read_text().splitlines()]
    message = next(item for item in lines if item["source"] == "feishu-im")
    assert "Synthetic recovery example" in message["content"]
    assert collector.STATE_FILE.read_bytes() == legacy
