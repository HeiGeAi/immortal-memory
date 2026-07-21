from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_cli(args: list[str], home: Path, timeout: float = 60):
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": str(ROOT / "core"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from cli import main; raise SystemExit(main())",
            *args,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_stable_error_code(output: str) -> str:
    match = re.search(r'"code"\s*:\s*"([a-z0-9_]+)"', output)
    if not match:
        match = re.search(r"code[\"']?\s*[:=]\s*([a-z0-9_]+)", output)
    assert match is not None, output
    return match.group(1)


def _assert_no_home_side_effects(home: Path, before: set[str], result_output: str):
    after_files = {
        path.relative_to(home).as_posix()
        for path in home.rglob("*")
        if path.is_file() or path.is_dir()
    }
    assert after_files == before
    assert not (home / ".immortal").exists()
    assert str(home) not in result_output


def _run_and_assert_legacy_command(command: str, home: Path):
    marker = home / "before.txt"
    marker.write_text("legacy-command-proof", encoding="utf-8")
    before = {
        path.relative_to(home).as_posix()
        for path in home.rglob("*")
        if path.is_file() or path.is_dir()
    }

    result = _run_cli([command], home)
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    code = _parse_stable_error_code(output)
    assert code.islower()
    assert len(code) >= 8
    lower = output.lower()
    assert "migration" in lower or "迁移" in output
    assert "project.py" not in lower
    assert "package_tool.py" not in lower
    assert "/Users/" not in output
    assert "can't open file" not in lower
    assert "no such file" not in lower
    _assert_no_home_side_effects(home, before, output)
    return output, code


def test_legacy_project_and_package_are_not_in_help(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = _run_cli(["--help"], home)
    output = result.stdout

    assert result.returncode == 0, output + result.stderr
    assert not re.search(r"^\s+project\s+", output, re.MULTILINE)
    assert not re.search(r"^\s+package\s+", output, re.MULTILINE)


def test_legacy_project_and_package_cli_is_stable_and_closed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    first_output, first_code = _run_and_assert_legacy_command("project", home)
    second_output, second_code = _run_and_assert_legacy_command("project", home)
    assert first_code == second_code

    package_output, package_code = _run_and_assert_legacy_command("package", home)
    assert package_output != ""
    assert package_code != ""


def test_legacy_command_migration_notice_shows_for_cli_subcommand_help_calls(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    before = {
        path.relative_to(home).as_posix()
        for path in home.rglob("*")
        if path.is_file() or path.is_dir()
    }
    result = _run_cli(["project", "--help"], home)
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert "stable_machine_code" in output or "code" in output.lower()
    assert ("legacy" in output.lower()) or ("迁移" in output)
    _assert_no_home_side_effects(home, before, output)


def test_packaged_entrypoint_intercepts_legacy_commands_before_home_reads(
    monkeypatch, capsys
):
    import cli

    def reject_read(*_args, **_kwargs):
        raise AssertionError("legacy migration path read a file")

    monkeypatch.setattr(Path, "read_text", reject_read)
    monkeypatch.setattr(sys, "argv", ["immortal-memory", "project"])

    assert cli.main() == 2
    assert "legacy_project_extension_required" in capsys.readouterr().err


def test_source_installer_wrapper_uses_early_entrypoint_and_skips_config_fifo(
    tmp_path,
):
    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "install"
    install = subprocess.run(
        [
            sys.executable,
            str(ROOT / "install.py"),
            "--prefix",
            str(prefix),
            "--owner-display-name",
            "Test Owner",
            "--no-daily",
        ],
        cwd=ROOT,
        env={**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    wrapper = home / ".local/bin/immortal-memory"
    assert "cli.py" in wrapper.read_text(encoding="utf-8")
    assert "immortal.py" not in wrapper.read_text(encoding="utf-8")
    config = home / ".immortal/config.json"
    config.unlink()
    os.mkfifo(config, mode=0o600)

    result = subprocess.run(
        [str(wrapper), "package"],
        env={**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert "legacy_package_removed" in result.stderr


def test_wheel_does_not_bundle_legacy_project_or_package_files(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ROOT,
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
        ],
        cwd=tmp_path,
        env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheelhouse.glob("immortal_memory-*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as archive:
        members = set(archive.namelist())

    assert "immortal_memory/project.py" not in members
    assert "immortal_memory/package_tool.py" not in members
