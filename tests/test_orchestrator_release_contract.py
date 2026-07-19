from __future__ import annotations

import sys

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


def test_full_pipeline_fails_closed_before_any_command_when_v11_is_unavailable(tmp_path):
    (tmp_path / "immortal.py").write_text('sub.add_parser("run")\n', encoding="utf-8")
    factory = FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
    )

    with pytest.raises(profile_review.FullPipelineUnavailable) as error:
        factory._commands_for("full", {"goal": "真实闭环"})

    assert error.value.missing_stages == (
        "claims-migrate",
        "profile-attribution-audit",
        "living-self-build",
        "cards-build",
        "context-preview",
    )


def test_full_pipeline_commands_and_summary_match_completed_stages(tmp_path):
    for filename in (
        "profile_attribution_audit.py",
        "claim_migrate.py",
        "living_self.py",
        "cards.py",
        "context_compiler.py",
    ):
        (tmp_path / filename).write_text("", encoding="utf-8")
    (tmp_path / "immortal.py").write_text(
        "\n".join(
            (
                'sub.add_parser("run")',
                'sub.add_parser("claims-migrate")',
                'sub.add_parser("profile-attribution-audit")',
                'sub.add_parser("living-self-build")',
                'sub.add_parser("cards")',
                'sub.add_parser("context-preview")',
            )
        ),
        encoding="utf-8",
    )
    factory = FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
    )

    commands = factory._commands_for("full", {"goal": "真实闭环"})
    stages = factory._command_stages(commands)
    summary = factory._success_summary("full", {"goal": "真实闭环"}, commands)

    assert stages == list(FactoryStore.FULL_PIPELINE_STAGES)
    assert "采集、清洗、蒸馏、画像" not in summary
    assert "已按真实命令完成" in summary
    for stage in FactoryStore.FULL_PIPELINE_STAGES:
        assert stage in summary


def test_full_pipeline_does_not_unlock_when_file_exists_but_cli_is_unregistered(tmp_path):
    for filename in (
        "profile_attribution_audit.py",
        "claim_migrate.py",
        "living_self.py",
        "cards.py",
        "context_compiler.py",
    ):
        (tmp_path / filename).write_text("", encoding="utf-8")
    (tmp_path / "immortal.py").write_text(
        'sub.add_parser("run")\nsub.add_parser("cards")\n',
        encoding="utf-8",
    )
    factory = FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
    )

    with pytest.raises(profile_review.FullPipelineUnavailable) as error:
        factory._commands_for("full", {"goal": "真实闭环"})

    assert error.value.missing_stages == (
        "claims-migrate",
        "profile-attribution-audit",
        "living-self-build",
        "context-preview",
    )


def test_fail_closed_stub_does_not_unlock_cards_stage(tmp_path):
    for filename in (
        "profile_attribution_audit.py",
        "claim_migrate.py",
        "living_self.py",
        "context_compiler.py",
    ):
        (tmp_path / filename).write_text("", encoding="utf-8")
    (tmp_path / "cards.py").write_text("not_available_until_v11\n", encoding="utf-8")
    (tmp_path / "immortal.py").write_text(
        "\n".join(
            f'sub.add_parser("{command}")'
            for command in (
                "run",
                "claims-migrate",
                "profile-attribution-audit",
                "living-self-build",
                "cards",
                "context-preview",
            )
        ),
        encoding="utf-8",
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
