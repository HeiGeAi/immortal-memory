from __future__ import annotations

import importlib
import subprocess
import time
from pathlib import Path

import profile_review


def capability_module():
    path = Path(__file__).resolve().parents[1] / "core" / "pipeline_capabilities.py"
    assert path.is_file(), "pipeline capability registry is missing"
    return importlib.import_module("pipeline_capabilities")


def write_module(path: Path, stage_id: str, *, ready: bool = True) -> None:
    path.write_text(
        "\n".join(
            (
                "def handler(*_args, **_kwargs):",
                "    return 0",
                f"PIPELINE_CAPABILITIES = {{{stage_id!r}: handler}}",
                f"CAPABILITY_READY = {ready!r}",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def write_ready_pipeline(root: Path) -> None:
    (root / "immortal.py").write_text(
        """
import argparse

def command_run(_args=None):
    return 0

def command_claims_migrate(_args=None):
    return 0

def command_profile_attribution_audit(_args=None):
    return 0

def command_living_self_build(_args=None):
    return 0

def command_cards_build(_args=None):
    return 0

def command_context_preview(_args=None):
    return 0

PIPELINE_CAPABILITIES = {"run": command_run}

def build_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    handlers = {
        "run": command_run,
        "claims-migrate": command_claims_migrate,
        "profile-attribution-audit": command_profile_attribution_audit,
        "living-self-build": command_living_self_build,
        "cards": command_cards_build,
        "context-preview": command_context_preview,
    }
    for command, handler in handlers.items():
        sub.add_parser(command).set_defaults(func=handler)
    return parser
""".lstrip(),
        encoding="utf-8",
    )
    write_module(root / "model_migration.py", "claims-migrate")
    write_module(root / "profile_attribution_audit.py", "profile-attribution-audit")
    write_module(root / "living_self_service.py", "living-self-build")
    write_module(root / "judgment_store.py", "cards-build")
    write_module(root / "context_compiler.py", "context-preview")
    write_module(root / "cards.py", "cards-build", ready=True)


def test_registry_uses_the_planned_module_boundaries():
    module = capability_module()
    mapping = {
        capability.stage_id: capability.module_filename
        for capability in module.PIPELINE_CAPABILITIES
    }

    assert mapping == {
        "run": "immortal.py",
        "claims-migrate": "model_migration.py",
        "profile-attribution-audit": "profile_attribution_audit.py",
        "living-self-build": "living_self_service.py",
        "cards-build": "judgment_store.py",
        "context-preview": "context_compiler.py",
    }
    assert {
        capability.stage_id: capability.host_handler_name
        for capability in module.PIPELINE_CAPABILITIES
    } == {
        "run": "command_run",
        "claims-migrate": "command_claims_migrate",
        "profile-attribution-audit": "command_profile_attribution_audit",
        "living-self-build": "command_living_self_build",
        "cards-build": "command_cards_build",
        "context-preview": "command_context_preview",
    }


def test_current_tree_exposes_only_the_capabilities_that_are_really_ready():
    module = capability_module()
    core = Path(__file__).resolve().parents[1] / "core"

    statuses = {
        status.stage_id: status
        for status in module.pipeline_capability_status(core)
    }

    assert statuses["run"].ready is True
    assert statuses["profile-attribution-audit"].ready is True
    assert module.missing_pipeline_stages(core) == (
        "claims-migrate",
        "living-self-build",
        "cards-build",
        "context-preview",
    )


def test_ready_requires_real_subparser_importable_module_and_callable_export(tmp_path):
    module = capability_module()
    write_ready_pipeline(tmp_path)

    statuses = module.pipeline_capability_status(tmp_path)

    assert [status.stage_id for status in statuses if not status.ready] == []
    assert module.missing_pipeline_stages(tmp_path) == ()


def test_comments_and_unrelated_strings_do_not_register_a_command(tmp_path):
    module = capability_module()
    write_ready_pipeline(tmp_path)
    immortal = tmp_path / "immortal.py"
    immortal.write_text(
        immortal.read_text(encoding="utf-8").replace(
            '        "claims-migrate": command_claims_migrate,\n',
            '        # add_parser("claims-migrate") is not registration\n',
        ),
        encoding="utf-8",
    )

    status = {
        item.stage_id: item
        for item in module.pipeline_capability_status(tmp_path)
    }["claims-migrate"]

    assert status.ready is False
    assert status.reasons == ("subparser_missing",)


def test_command_name_without_callable_host_handler_does_not_unlock(tmp_path):
    module = capability_module()
    write_ready_pipeline(tmp_path)
    immortal = tmp_path / "immortal.py"
    source = immortal.read_text(encoding="utf-8")
    source = source.replace(
        '        sub.add_parser(command).set_defaults(func=handler)\n',
        '        parser_for_command = sub.add_parser(command)\n'
        '        if command != "claims-migrate":\n'
        '            parser_for_command.set_defaults(func=handler)\n',
    )
    immortal.write_text(source, encoding="utf-8")

    status = {
        item.stage_id: item
        for item in module.pipeline_capability_status(tmp_path)
    }["claims-migrate"]

    assert status.ready is False
    assert status.reasons == ("host_handler_missing",)


def test_wrong_callable_host_handler_does_not_unlock(tmp_path):
    module = capability_module()
    write_ready_pipeline(tmp_path)
    immortal = tmp_path / "immortal.py"
    source = immortal.read_text(encoding="utf-8")
    source = source.replace(
        '        sub.add_parser(command).set_defaults(func=handler)\n',
        '        parser_for_command = sub.add_parser(command)\n'
        '        if command == "claims-migrate":\n'
        '            parser_for_command.set_defaults(func=command_run)\n'
        '        else:\n'
        '            parser_for_command.set_defaults(func=handler)\n',
    )
    immortal.write_text(source, encoding="utf-8")

    status = {
        item.stage_id: item
        for item in module.pipeline_capability_status(tmp_path)
    }["claims-migrate"]

    assert status.ready is False
    assert status.reasons == ("host_handler_mismatch",)


def test_non_callable_export_and_import_failure_fail_closed(tmp_path):
    module = capability_module()
    write_ready_pipeline(tmp_path)
    (tmp_path / "model_migration.py").write_text(
        'PIPELINE_CAPABILITIES = {"claims-migrate": "not callable"}\n',
        encoding="utf-8",
    )
    (tmp_path / "living_self_service.py").write_text(
        "raise RuntimeError('broken import')\n",
        encoding="utf-8",
    )

    statuses = {
        item.stage_id: item
        for item in module.pipeline_capability_status(tmp_path)
    }

    assert statuses["claims-migrate"].reasons == ("capability_not_callable",)
    assert statuses["living-self-build"].reasons == ("module_import_failed",)


def test_cards_requires_judgment_store_and_ready_compatibility_module(tmp_path):
    module = capability_module()
    write_ready_pipeline(tmp_path)
    write_module(tmp_path / "cards.py", "cards-build", ready=False)

    status = {
        item.stage_id: item
        for item in module.pipeline_capability_status(tmp_path)
    }["cards-build"]

    assert status.ready is False
    assert status.reasons == ("compatibility_not_ready",)


def test_full_commands_are_typed_and_goal_named_run_cannot_change_stage_identity(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(profile_review, "missing_pipeline_stages", lambda _root: ())
    factory = profile_review.FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
    )

    commands = factory._commands_for("full", {"goal": "run", "mode": "auto"})

    assert all(isinstance(command, profile_review.StageCommand) for command in commands)
    assert [command.stage_id for command in commands] == list(factory.FULL_PIPELINE_STAGES)
    assert commands[-1].stage_id == "context-preview"
    assert "run" in commands[-1].argv
    summary = factory._success_summary(
        "full",
        {"goal": "run"},
        tuple(command.stage_id for command in commands),
    )
    assert summary.count("run") == 1
    assert summary.endswith("context-preview。")


def test_job_summary_uses_only_stage_ids_that_really_succeeded(tmp_path, monkeypatch):
    commands = [
        profile_review.StageCommand("first", ("python3", "first"), 5),
        profile_review.StageCommand("profile-nuwa", ("python3", "attention"), 5),
        profile_review.StageCommand("last", ("python3", "last"), 5),
    ]

    def runner(argv, **_kwargs):
        code = 2 if argv[-1] == "attention" else 0
        return subprocess.CompletedProcess(argv, code, "", "")

    factory = profile_review.FactoryStore(
        history_path=tmp_path / "jobs.json",
        skill_dir=tmp_path,
        immortal_dir=tmp_path / "vault",
        runner=runner,
    )
    monkeypatch.setattr(factory, "_commands_for", lambda _kind, _body: commands)

    job_id = factory.start_job("clean", {})["id"]
    deadline = time.monotonic() + 3
    job = factory.get_job(job_id)
    while job and job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.01)
        job = factory.get_job(job_id)

    assert job["status"] == "attention"
    assert job["summary"] == "已按真实命令完成：first、last。"
