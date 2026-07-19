from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

import pytest

import orchestrator
import profile_review
from profile_review import FactoryStore


class FakeTelemetry:
    def __init__(self):
        self.calls = []

    def start_run(self, **kwargs):
        self.calls.append(("start_run", kwargs))

    def finish_run(self, **kwargs):
        self.calls.append(("finish_run", kwargs))

    def heartbeat(self, **kwargs):
        self.calls.append(("heartbeat", kwargs))


def static_orchestrator_script_targets() -> set[str]:
    source = Path(orchestrator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Name) or node.func.id not in {"run_script", "run_script_rc"}:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            targets.add(first.value)
    return targets


def populated_skill_dir(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "core"
    skill_dir.mkdir()
    for name in orchestrator.REQUIRED_CORE_SCRIPTS:
        (skill_dir / name).write_text("# packaged test script\n", encoding="utf-8")
    return skill_dir


def test_every_static_orchestrator_script_has_an_explicit_required_or_optional_classification():
    classified = (
        set(orchestrator.REQUIRED_CORE_SCRIPTS)
        | set(orchestrator.OPTIONAL_CONNECTOR_SCRIPTS)
        | set(orchestrator.INACTIVE_COMPATIBILITY_SCRIPTS)
    )

    assert static_orchestrator_script_targets() == classified


@pytest.mark.parametrize("missing_name", ["profile.py", "index_db.py"])
def test_run_main_fails_before_collection_when_required_script_is_missing(
    tmp_path,
    monkeypatch,
    missing_name,
):
    skill_dir = populated_skill_dir(tmp_path)
    (skill_dir / missing_name).unlink()
    calls = []
    monkeypatch.setattr(orchestrator, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(orchestrator, "collect", lambda: calls.append("collect"))
    monkeypatch.setattr(
        orchestrator,
        "run_script",
        lambda *_args, **_kwargs: calls.append("run_script"),
    )

    outcome = orchestrator.run_main()

    assert outcome == {
        "status": "failed",
        "exit_code": 1,
        "errors": [f"required core script missing: {missing_name}"],
        "results": {},
    }
    assert calls == []


def test_main_reports_real_missing_script_preflight_as_failed_telemetry(
    tmp_path,
    monkeypatch,
):
    skill_dir = populated_skill_dir(tmp_path)
    (skill_dir / "profile.py").unlink()
    telemetry = FakeTelemetry()
    monkeypatch.setattr(orchestrator, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(orchestrator, "RUNTIME_TELEMETRY", telemetry)
    monkeypatch.setattr(orchestrator, "acquire_lock", lambda: True)
    monkeypatch.setattr(orchestrator, "release_lock", lambda: None)
    monkeypatch.setattr(orchestrator, "telemetry_finish_active", lambda _errors: None)
    monkeypatch.setattr(
        orchestrator,
        "collect",
        lambda: pytest.fail("collection must not start after failed script preflight"),
    )

    exit_code = orchestrator.main()

    assert exit_code == 1
    assert telemetry.calls[-1] == (
        "finish_run",
        {
            "status": "failed",
            "results": {},
            "error": "required core script missing: profile.py",
        },
    )


def test_damaged_packaged_core_copy_is_rejected_by_real_preflight(tmp_path):
    packaged_core = tmp_path / "site-packages" / "immortal_memory"
    shutil.copytree(Path(orchestrator.__file__).parent, packaged_core)
    (packaged_core / "index_db.py").unlink()

    assert orchestrator.preflight_required_scripts(packaged_core) == ["index_db.py"]


def test_required_stage_failure_returns_nonzero_and_failed_telemetry(monkeypatch):
    telemetry = FakeTelemetry()
    monkeypatch.setattr(orchestrator, "RUNTIME_TELEMETRY", telemetry)
    monkeypatch.setattr(orchestrator, "acquire_lock", lambda: True)
    monkeypatch.setattr(orchestrator, "release_lock", lambda: None)
    monkeypatch.setattr(orchestrator, "telemetry_finish_active", lambda _errors: None)
    monkeypatch.setattr(
        orchestrator,
        "run_main",
        lambda: {
            "status": "failed",
            "exit_code": 1,
            "results": {},
            "errors": ["search index sync failed"],
        },
    )

    exit_code = orchestrator.main()

    assert exit_code == 1
    assert telemetry.calls[-1] == (
        "finish_run",
        {
            "status": "failed",
            "results": {},
            "error": "search index sync failed",
        },
    )


@pytest.mark.parametrize(
    "errors",
    [
        ["collect failed"],
        ["required core script missing: claim_migrate.py"],
        ["index integrity failed"],
        ["search index sync failed"],
        ["migration failed"],
        ["claims migration failed"],
        ["context compile failed"],
        ["export verify failed"],
        ["portable export failed"],
        ["portable restore-check failed"],
    ],
)
def test_required_failure_classification_is_authoritative(errors):
    assert orchestrator.orchestration_status(errors) == ("failed", 1)


def test_optional_connector_limitation_can_stay_attention():
    assert orchestrator.orchestration_status(["feishu mirror inventory failed"]) == (
        "attention",
        0,
    )


def test_full_pipeline_declares_only_the_real_v11_stages():
    assert FactoryStore.FULL_PIPELINE_STAGES == (
        "run",
        "claims-migrate",
        "profile-attribution-audit",
        "living-self-build",
        "cards-build",
        "context-preview",
    )


def test_full_pipeline_fails_closed_before_any_command_when_v11_is_unavailable(
    tmp_path,
    monkeypatch,
):
    missing = (
        "claims-migrate",
        "profile-attribution-audit",
        "living-self-build",
        "cards-build",
        "context-preview",
    )
    monkeypatch.setattr(profile_review, "missing_pipeline_stages", lambda _root: missing)
    factory = FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
    )

    with pytest.raises(profile_review.FullPipelineUnavailable) as error:
        factory._commands_for("full", {"goal": "真实闭环"})

    assert error.value.missing_stages == missing


def test_full_pipeline_commands_and_summary_match_completed_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_review, "missing_pipeline_stages", lambda _root: ())
    factory = FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
    )

    commands = factory._commands_for("full", {"goal": "真实闭环"})
    stages = [command.stage_id for command in commands]
    summary = factory._success_summary("full", {"goal": "真实闭环"}, tuple(stages))

    assert stages == list(FactoryStore.FULL_PIPELINE_STAGES)
    assert "采集、清洗、蒸馏、画像" not in summary
    assert "已按真实命令完成" in summary
    for stage in FactoryStore.FULL_PIPELINE_STAGES:
        assert stage in summary


def test_full_pipeline_does_not_unlock_when_registry_reports_missing(tmp_path, monkeypatch):
    missing = (
        "claims-migrate",
        "profile-attribution-audit",
        "living-self-build",
        "context-preview",
    )
    monkeypatch.setattr(profile_review, "missing_pipeline_stages", lambda _root: missing)
    factory = FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
    )

    with pytest.raises(profile_review.FullPipelineUnavailable) as error:
        factory._commands_for("full", {"goal": "真实闭环"})

    assert error.value.missing_stages == missing


def test_unready_cards_capability_does_not_unlock_cards_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        profile_review,
        "missing_pipeline_stages",
        lambda _root: ("cards-build",),
    )
    factory = FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
    )

    with pytest.raises(profile_review.FullPipelineUnavailable) as error:
        factory._commands_for("full", {"goal": "真实闭环"})

    assert error.value.missing_stages == ("cards-build",)


def test_orchestrator_module_entrypoint_uses_main_exit_code():
    source = (orchestrator.Path(orchestrator.__file__)).read_text(encoding="utf-8")
    assert "raise SystemExit(main())" in source
