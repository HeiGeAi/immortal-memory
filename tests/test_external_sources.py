from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import config
import export_restore
import external_sources
import immortal
import orchestrator
from control_data import ControlData


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    )
    return result.stdout.strip()


def _seed_repo(root: Path) -> Path:
    repo = root / "project-a"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@example.invalid")
    (repo / "README.md").write_text("private file body\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "record the first decision")
    return repo


def test_external_sources_are_disabled_by_default():
    assert config.DEFAULT_CONFIG["external_sources"] == {
        "git": {"enabled": False, "paths": [], "max_commits": 200},
        "github": {"enabled": False, "paths": [], "repositories": [], "max_items": 50},
        "claude-web": {"enabled": False, "paths": []},
        "chatgpt": {"enabled": False, "paths": []},
        "cursor": {"enabled": False, "paths": []},
    }


def test_portable_backup_preserves_external_source_state():
    assert "external_sources" in export_restore.REQUIRED_PATHS


def test_register_source_requires_explicit_existing_path(tmp_path):
    settings: dict = {}
    external_sources.register_source(settings, "git", tmp_path)
    assert settings["external_sources"]["git"]["paths"] == [str(tmp_path.resolve())]
    with pytest.raises(ValueError, match="does not exist"):
        external_sources.register_source(settings, "git", tmp_path / "missing")


def test_register_github_repository_without_local_checkout():
    settings: dict = {}
    external_sources.register_source(settings, "github", "owner/project")
    assert settings["external_sources"]["github"] == {
        "enabled": True,
        "repositories": ["owner/project"],
    }


def test_registered_conversation_export_is_snapshotted_inside_vault(tmp_path):
    source = tmp_path / "downloads" / "conversations.json"
    source.parent.mkdir()
    source.write_text("[]", encoding="utf-8")
    settings = {"vault_dir": str(tmp_path / "vault")}

    external_sources.register_source(settings, "claude-web", source)

    imported = tmp_path / "vault" / "imports" / "claude-web" / "conversations.json"
    assert imported.read_text(encoding="utf-8") == "[]"
    assert settings["external_sources"]["claude-web"]["paths"] == [str(imported.resolve())]


def test_git_collection_is_incremental_and_does_not_read_file_body(tmp_path):
    repo = _seed_repo(tmp_path / "repos")
    vault = tmp_path / "vault"

    first = external_sources.collect_git_history(vault, [repo.parent], max_commits=20)
    second = external_sources.collect_git_history(vault, [repo.parent], max_commits=20)

    assert first["records_written"] == 1
    assert second["records_written"] == 0
    row = json.loads((vault / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["source"] == "git-history"
    assert row["content"] == "record the first decision"
    assert "private file body" not in json.dumps(row)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/HeiGeAi/immortal-memory.git", "HeiGeAi/immortal-memory"),
        ("git@github.com:HeiGeAi/immortal-memory.git", "HeiGeAi/immortal-memory"),
        ("https://example.com/owner/repo.git", ""),
    ],
)
def test_github_remote_parser_accepts_only_github(remote, expected):
    assert external_sources.github_slug_from_remote(remote) == expected


def test_disabled_github_issues_are_a_supported_empty_state(monkeypatch):
    monkeypatch.setattr(
        external_sources.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="the repository has disabled issues"
        ),
    )
    assert external_sources._gh_list("owner/repo", "issue", 50) == ([], True)


def test_missing_github_cli_is_reported_as_connector_failure(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(external_sources.subprocess, "run", missing)

    assert external_sources._gh_list("owner/repo", "pull-request", 50) == ([], False)


def test_github_collection_retries_and_reports_bounded_error(monkeypatch, tmp_path):
    repo = _seed_repo(tmp_path / "repos")
    monkeypatch.setattr(external_sources, "_github_remote", lambda _repo: "owner/project")
    calls = []

    def failed(*_args, **_kwargs):
        calls.append(1)
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="network unavailable")

    monkeypatch.setattr(external_sources.subprocess, "run", failed)

    result = external_sources.collect_github_history(tmp_path / "vault", [repo.parent])

    assert result["status"] == "partial"
    assert result["error_count"] == 2
    assert len(calls) == 4
    assert result["errors"][0] == {
        "repository": "owner/project",
        "kind": "pull-request",
        "error_type": "gh_command_failed",
        "message": "network unavailable",
    }


def test_github_collection_accepts_explicit_repositories_without_local_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        external_sources,
        "_gh_list_with_error",
        lambda repository, kind, limit: ([], True, None),
    )

    result = external_sources.collect_registered_sources({
        "external_sources": {
            "github": {
                "enabled": True,
                "paths": [],
                "repositories": ["owner/project"],
                "max_items": 10,
            }
        }
    }, tmp_path / "vault")

    assert result["github"]["status"] == "success"
    assert result["github"]["repositories"] == 1


def test_chatgpt_claude_and_cursor_exports_are_parsed(tmp_path):
    chatgpt = tmp_path / "chatgpt.json"
    chatgpt.write_text(json.dumps([{
        "id": "c1", "title": "Decision", "mapping": {"n1": {"message": {
            "id": "m1", "author": {"role": "user"},
            "content": {"parts": ["Prefer the reversible option"]},
        }}}
    }]), encoding="utf-8")
    claude = tmp_path / "claude.json"
    claude.write_text(json.dumps([{
        "uuid": "c2", "name": "Review", "chat_messages": [{
            "uuid": "m2", "sender": "human", "text": "Keep the release reversible"
        }]
    }]), encoding="utf-8")
    cursor = tmp_path / "cursor.jsonl"
    cursor.write_text(json.dumps({
        "id": "m3", "role": "assistant", "content": "Use the current architecture"
    }) + "\n", encoding="utf-8")

    assert external_sources.parse_chatgpt_export(chatgpt)[0]["content"] == "Prefer the reversible option"
    assert external_sources.parse_claude_web_export(claude)[0]["role"] == "user"
    assert external_sources.parse_cursor_export(cursor)[0]["content"] == "Use the current architecture"


def test_registered_collection_redacts_secrets_and_is_incremental(tmp_path):
    export = tmp_path / "cursor.jsonl"
    export.write_text(json.dumps({
        "id": "m1", "role": "user", "content": "token: ghp_" + "a" * 40
    }) + "\n", encoding="utf-8")
    settings = {"external_sources": {
        "cursor": {"enabled": True, "paths": [str(export)]}
    }}
    vault = tmp_path / "vault"

    first = external_sources.collect_registered_sources(settings, vault)
    second = external_sources.collect_registered_sources(settings, vault)

    assert first["cursor"]["records_written"] == 1
    assert second["cursor"]["records_written"] == 0
    body = (vault / "index.jsonl").read_text(encoding="utf-8")
    assert "ghp_" + "a" * 40 not in body
    assert "[REDACTED]" in body


def test_registered_collection_reports_sanitized_parser_errors(tmp_path):
    export = tmp_path / "conversations.json"
    secret = "ghp_" + "a" * 40
    export.write_text('{"broken":"' + secret, encoding="utf-8")
    settings = {"external_sources": {
        "claude-web": {"enabled": True, "paths": [str(export)]}
    }}

    result = external_sources.collect_registered_sources(settings, tmp_path / "vault")

    source = result["claude-web"]
    assert source["status"] == "partial"
    assert source["error_count"] == 1
    assert source["errors"][0]["file"] == "conversations.json"
    assert source["errors"][0]["error_type"] == "JSONDecodeError"
    assert secret not in json.dumps(source)


def test_source_cli_lists_disabled_defaults_without_collecting(monkeypatch, capsys):
    monkeypatch.setattr(immortal, "load_config", lambda: config.DEFAULT_CONFIG)

    assert immortal.main(["source", "list", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"git", "github", "claude-web", "chatgpt", "cursor"}
    assert all(not item["enabled"] for item in payload.values())


def test_orchestrator_collects_declared_external_sources(monkeypatch):
    calls = []
    payload = {
        "git": {"records_written": 2, "status": "success"},
        "cursor": {"records_written": 1, "status": "success"},
    }
    monkeypatch.setattr(
        orchestrator,
        "run_script",
        lambda script, *args, **kwargs: calls.append((script, args, kwargs)) or (True, json.dumps(payload)),
    )
    monkeypatch.setattr(orchestrator, "log", lambda _message: None)

    ok, result = orchestrator.external_source_collect()

    assert ok is True
    assert result["records_written"] == 3
    assert calls[0][0:2] == ("immortal.py", ("source", "collect", "--json"))


def test_orchestrator_does_not_flatten_partial_external_source_to_success(monkeypatch):
    payload = {
        "git": {"records_written": 2, "status": "success"},
        "github": {"records_written": 1, "status": "partial", "error_count": 1},
        "chatgpt": {"records_written": 0, "status": "disabled"},
    }
    monkeypatch.setattr(
        orchestrator,
        "run_script",
        lambda *_args, **_kwargs: (True, json.dumps(payload)),
    )
    monkeypatch.setattr(orchestrator, "log", lambda _message: None)

    ok, result = orchestrator.external_source_collect()

    assert ok is False
    assert result["records_written"] == 3
    assert result["partial_sources"] == ["github"]


def test_control_center_shows_external_sources_and_mail_without_paths(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "feishu": {"daily_sources": "contacts,messages"},
        "external_sources": {
            "git": {"enabled": True, "paths": ["/private/projects"]},
            "github": {
                "enabled": True,
                "paths": [],
                "repositories": ["owner/project"],
            },
            "chatgpt": {"enabled": False, "paths": []},
        },
    }), encoding="utf-8")
    state_dir = tmp_path / "external_sources"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "generated_at": "2026-07-23T12:00:00Z",
        "sources": {
            "git": {"records_written": 3, "status": "success"},
            "github": {
                "records_written": 5,
                "repositories": 1,
                "status": "success",
            },
        },
    }), encoding="utf-8")

    items = {item["id"]: item for item in ControlData(tmp_path).sources()["items"]}

    assert items["git-history"]["status"] == "success"
    assert items["git-history"]["increment"] == 3
    assert items["github-history"]["status"] == "success"
    assert items["github-history"]["increment"] == 5
    assert items["chatgpt"]["status"] == "skipped"
    assert items["feishu-mail"]["status"] == "skipped"
    assert "/private" not in json.dumps(items, ensure_ascii=False)
