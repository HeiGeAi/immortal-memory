from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import urllib.request
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(command, **kwargs):
    return subprocess.run(
        [str(part) for part in command],
        capture_output=True,
        text=True,
        timeout=240,
        **kwargs,
    )


def build_wheel(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    build = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ROOT,
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            wheelhouse,
        ],
        cwd=tmp_path,
        env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheelhouse.glob("immortal_memory-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def test_clean_wheel_installs_complete_v11_cli(tmp_path):
    wheel = build_wheel(tmp_path)
    members = wheel_names(wheel)
    required = {
        "immortal_memory/claim_store.py",
        "immortal_memory/context_compiler.py",
        "immortal_memory/context_store.py",
        "immortal_memory/export_restore.py",
        "immortal_memory/immortal.py",
        "immortal_memory/judgment_store.py",
        "immortal_memory/living_self_service.py",
        "immortal_memory/model_migration.py",
        "immortal_memory/outcome_store.py",
        "immortal_memory/product_assets/app.js",
        "immortal_memory/product_assets/views/contexts.js",
    }
    assert required <= members

    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / "bin/python"
    install = _run(
        [python, "-m", "pip", "install", "--no-deps", wheel],
        cwd=tmp_path,
        env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    assert install.returncode == 0, install.stdout + install.stderr
    cli = environment / "bin/immortal-memory"
    assert cli.is_file()
    home = tmp_path / "clean-home"
    home.mkdir()
    runtime_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    runtime_env.update({"HOME": str(home), "IMMORTAL_HOME": str(home / ".immortal")})
    initialized = _run(
        [
            cli,
            "init",
            "--owner-name",
            "packaging-test",
            "--vault-dir",
            home / ".immortal",
        ],
        cwd=tmp_path,
        env=runtime_env,
    )
    assert initialized.returncode == 0
    assert "Immortal config initialized" in initialized.stdout
    trained = _run([cli, "train", "--smoke"], cwd=tmp_path, env=runtime_env)
    assert trained.returncode == 0, trained.stdout + trained.stderr
    marker = json.loads(
        (home / ".immortal" / "product" / "bootstrap-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["state"] == "active"
    assert (home / ".immortal" / "search_index.db").is_file()

    dashboard_env = {**runtime_env, "PYTHONUNBUFFERED": "1"}
    dashboard = subprocess.Popen(
        [cli, "dashboard", "--port", "0"],
        cwd=tmp_path,
        env=dashboard_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name == "posix"),
    )
    output = ""
    try:
        assert dashboard.stdout is not None
        first_line = dashboard.stdout.readline().strip()
        match = re.fullmatch(r"Immortal dashboard: (http://127\.0\.0\.1:\d+/)", first_line)
        assert match, first_line
        with urllib.request.urlopen(match.group(1) + "api/v2/home", timeout=10) as response:
            home_payload = json.loads(response.read())
        assert len(home_payload["remembered_today"]) == 1
        assert "error" not in home_payload
    finally:
        if dashboard.poll() is None:
            if os.name == "posix":
                os.killpg(dashboard.pid, signal.SIGINT)
            else:
                dashboard.terminate()
        try:
            output, _ = dashboard.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(dashboard.pid, signal.SIGKILL)
            else:
                dashboard.kill()
            output, _ = dashboard.communicate(timeout=30)
    assert "Traceback" not in first_line + output

    commands = (
        (("claims-migrate", "--help"), 0, "usage:"),
        (("cards", "stats"), 0, '"total": 0'),
        (("agent-context", "test", "--print"), 3, "context_status=unavailable"),
        (("profile-review", "--help"), 0, "usage:"),
    )
    for command, expected_code, expected_output in commands:
        result = _run([cli, *command], cwd=tmp_path, env=runtime_env)
        output = result.stdout + result.stderr
        assert result.returncode == expected_code, output
        assert expected_output in output
        assert "No such file" not in output
        assert "can't open file" not in output
        assert "ModuleNotFoundError" not in output
        assert "ImportError" not in output
        assert "Traceback" not in output


def test_wheel_contains_feishu_recovery_module_and_no_vault_artifacts(tmp_path):
    wheel = build_wheel(tmp_path)
    names = wheel_names(wheel)

    assert "immortal_memory/feishu_recovery.py" in names
    assert not any(
        ".immortal" in name or "recovery/feishu" in name for name in names
    )
