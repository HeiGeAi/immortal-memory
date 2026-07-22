from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"


def registered_script_targets(root: Path) -> set[str]:
    targets: set[str] = set()
    for path in (root / "core").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        targets.update(re.findall(r"""run_script\(\s*["']([^"']+\.py)["']""", text))
        targets.update(re.findall(r"""SKILL_DIR\s*/\s*["']([^"']+\.py)["']""", text))
    return targets


def test_every_registered_script_exists_in_source_tree():
    missing = sorted(
        target
        for target in registered_script_targets(ROOT)
        if not (CORE / target).is_file()
    )

    assert missing == []


def test_wheel_contains_every_registered_script(tmp_path):
    venv = tmp_path / "build-venv"
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheelhouse = tmp_path / "wheelhouse"
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as archive:
        members = set(archive.namelist())
    missing = sorted(
        target
        for target in registered_script_targets(ROOT)
        if f"immortal_memory/{target}" not in members
    )
    assert missing == []


def test_installed_compatibility_commands_are_safe_and_closed(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "*.egg-info",
            "__pycache__",
            "build",
            "dist",
            "venv",
        ),
    )
    venv = tmp_path / "venv"
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    cli = venv / ("Scripts/immortal-memory.exe" if os.name == "nt" else "bin/immortal-memory")
    wheelhouse = tmp_path / "wheelhouse"
    built = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheel = next(wheelhouse.glob("immortal_memory-*.whl"))
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    env = {**os.environ, "HOME": str(tmp_path / "home")}
    for command in (
        ("cards", "--help"),
        ("notes-sync", "--help"),
        ("notes-migrate", "--help"),
        ("notes-migrate-reset", "--help"),
        ("migration-preflight", "--help"),
        ("profile-attribution-audit", "--help"),
    ):
        result = subprocess.run(
            [str(cli), *command],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "can't open file" not in result.stdout + result.stderr
        assert "No such file" not in result.stdout + result.stderr

    help_result = subprocess.run(
        [str(cli), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert help_result.returncode == 0
    assert not re.search(r"^\s+project\s+", help_result.stdout, re.MULTILINE)

    obsidian = tmp_path / "obsidian"
    notes = obsidian / "笔记"
    notes.mkdir(parents=True)
    (notes / "sample.md").write_text("# Public-safe note\n\nOnly a dry-run.", encoding="utf-8")
    dry_run = subprocess.run(
        [
            str(cli),
            "notes-sync",
            "--dry-run",
            "--vault-path",
            str(obsidian),
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert '"dry_run": true' in dry_run.stdout
    assert not (tmp_path / "home" / ".immortal" / "index.jsonl").exists()
    if (tmp_path / "home").exists():
        shutil.rmtree(tmp_path / "home")
    assert not (tmp_path / "home").exists()

    ingest = subprocess.run(
        [
            str(cli),
            "notes-sync",
            "--vault-path",
            str(obsidian),
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    index_file = tmp_path / "home" / ".immortal" / "index.jsonl"
    assert index_file.is_file()
    assert '"source": "obsidian-note"' in index_file.read_text(encoding="utf-8")
