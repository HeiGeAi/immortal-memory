from __future__ import annotations

import pytest

import orchestrator


class FakeTelemetry:
    def __init__(self):
        self.calls = []

    def start_run(self, **kwargs):
        self.calls.append(("start_run", kwargs))

    def start_stage(self, *args):
        self.calls.append(("start_stage", args))

    def finish_stage(self, *args, **kwargs):
        self.calls.append(("finish_stage", args, kwargs))

    def finish_run(self, **kwargs):
        self.calls.append(("finish_run", kwargs))

    def heartbeat(self, **kwargs):
        self.calls.append(("heartbeat", kwargs))


def test_main_finishes_successful_run_with_results(monkeypatch):
    fake = FakeTelemetry()
    monkeypatch.setattr(orchestrator, "RUNTIME_TELEMETRY", fake)
    monkeypatch.setattr(orchestrator, "acquire_lock", lambda: True)
    monkeypatch.setattr(orchestrator, "release_lock", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "run_main",
        lambda: {"status": "success", "results": {"new_records": 3}, "errors": []},
    )

    orchestrator.main()

    assert fake.calls[0][0] == "start_run"
    assert fake.calls[-1] == ("finish_run", {"status": "success", "results": {"new_records": 3}, "error": ""})


def test_main_records_uncaught_exception_as_failed(monkeypatch):
    fake = FakeTelemetry()
    monkeypatch.setattr(orchestrator, "RUNTIME_TELEMETRY", fake)
    monkeypatch.setattr(orchestrator, "acquire_lock", lambda: True)
    monkeypatch.setattr(orchestrator, "release_lock", lambda: None)

    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "run_main", explode)

    with pytest.raises(RuntimeError, match="boom"):
        orchestrator.main()

    assert fake.calls[-1] == ("finish_run", {"status": "failed", "error": "boom"})


def test_stage_checkpoint_marks_previous_stage_attention_when_errors_grow(monkeypatch):
    fake = FakeTelemetry()
    monkeypatch.setattr(orchestrator, "RUNTIME_TELEMETRY", fake)
    orchestrator._ACTIVE_STAGE = ""
    orchestrator._STAGE_ERROR_COUNT = 0

    orchestrator.telemetry_stage("collect", "采集", [])
    orchestrator.telemetry_stage("external", "外部来源", ["collect failed"])

    assert ("finish_stage", ("collect",), {"status": "attention", "summary": "新增 1 个需关注项"}) in fake.calls
    assert ("start_stage", ("external", "外部来源")) in fake.calls


def test_heartbeat_loop_keeps_long_stage_fresh(monkeypatch):
    fake = FakeTelemetry()
    monkeypatch.setattr(orchestrator, "RUNTIME_TELEMETRY", fake)

    class TwoWaits:
        def __init__(self):
            self.count = 0

        def wait(self, _interval):
            self.count += 1
            return self.count > 1

    orchestrator.telemetry_heartbeat_loop(TwoWaits(), interval=0)

    assert ("heartbeat", {}) in fake.calls


def test_v11_model_stages_run_in_dependency_order(monkeypatch, tmp_path):
    calls = []
    vault = tmp_path / "vault"
    monkeypatch.setattr(orchestrator, "IMMORTAL_DIR", vault)
    monkeypatch.setattr(orchestrator, "claims_migrate", lambda _vault: calls.append("claims-migrate") or True)
    monkeypatch.setattr(
        orchestrator,
        "profile_attribution_audit",
        lambda _vault: calls.append("profile-attribution-audit") or True,
    )
    monkeypatch.setattr(
        orchestrator,
        "living_self_build",
        lambda _vault: calls.append("living-self-build") or True,
    )
    monkeypatch.setattr(orchestrator, "cards_build", lambda: calls.append("cards build") or True)
    monkeypatch.setattr(orchestrator, "quality_report", lambda: calls.append("quality") or True)
    migration = {"staging_sha256": "bound"}
    prewarm = {"receipt": "bound"}
    monkeypatch.setattr(
        orchestrator,
        "evaluate_v11_production_switch",
        lambda actual_vault, actual_migration, actual_prewarm: {
            "ok": actual_vault == vault
            and actual_migration is migration
            and actual_prewarm is prewarm,
            "production_switch_allowed": True,
        },
    )

    result = orchestrator.run_v11_model_stages(
        vault,
        migration,
        prewarm,
    )

    assert result == {"ok": True, "completed": calls, "blockers": []}
    assert calls == [
        "profile-attribution-audit",
        "claims-migrate",
        "living-self-build",
        "cards build",
        "quality",
    ]


def test_v11_model_stages_stop_at_first_failed_dependency(monkeypatch, tmp_path):
    calls = []
    vault = tmp_path / "vault"
    monkeypatch.setattr(orchestrator, "IMMORTAL_DIR", vault)
    monkeypatch.setattr(orchestrator, "claims_migrate", lambda _vault: calls.append("claims-migrate") or True)
    monkeypatch.setattr(
        orchestrator,
        "profile_attribution_audit",
        lambda _vault: calls.append("profile-attribution-audit") or False,
    )
    monkeypatch.setattr(
        orchestrator,
        "living_self_build",
        lambda _vault: calls.append("living-self-build") or True,
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_v11_production_switch",
        lambda _vault, _migration, _prewarm: {
            "ok": True,
            "production_switch_allowed": True,
        },
    )

    result = orchestrator.run_v11_model_stages(
        vault,
        {"staging": "bound"},
        {"receipt": "bound"},
    )

    assert result["ok"] is False
    assert result["blockers"] == ["profile-attribution-audit_failed"]
    assert calls == ["profile-attribution-audit"]


def test_v11_model_stages_require_bound_production_gate(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    monkeypatch.setattr(orchestrator, "IMMORTAL_DIR", vault)
    monkeypatch.setattr(
        orchestrator,
        "profile_attribution_audit",
        lambda _vault: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_v11_production_switch",
        lambda _vault, _migration, _prewarm: {
            "ok": False,
            "production_switch_allowed": False,
            "blockers": ["published_source_generation_mismatch"],
        },
    )

    result = orchestrator.run_v11_model_stages(
        vault,
        {"staging": "unbound"},
        {"receipt": "unbound"},
    )

    assert result["ok"] is False
    assert result["blockers"] == ["production_switch_gate_required"]
    assert result["switch_gate"]["blockers"] == [
        "published_source_generation_mismatch"
    ]
