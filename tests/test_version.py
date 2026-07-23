import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import immortal


class VersionGovernanceTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_source_versions_and_cli_are_consistent(self):
        version = (self.ROOT / "core" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        pyproject = (self.ROOT / "pyproject.toml").read_text(encoding="utf-8")
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        project_version = re.search(
            r'^version\s*=\s*"([^"]+)"\s*$', pyproject, re.MULTILINE
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                immortal.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(version, "1.3.0")
        self.assertIsNotNone(project_version)
        self.assertEqual(project_version.group(1), version)
        self.assertIn(f"version-v{version}-", readme)
        self.assertEqual(output.getvalue().strip(), "immortal 1.3.0")

        setup_version = subprocess.run(
            [sys.executable, "setup.py", "--version"],
            cwd=self.ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(setup_version, version)

    def test_built_wheel_metadata_and_cli_match_release_version(self):
        version = (self.ROOT / "core" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        with tempfile.TemporaryDirectory(prefix="immortal-version-") as temp:
            temp_path = Path(temp)
            source_dir = temp_path / "source"
            wheel_dir = temp_path / "wheel"
            install_dir = temp_path / "installed"
            shutil.copytree(
                self.ROOT,
                source_dir,
                ignore=shutil.ignore_patterns(
                    ".git", ".pytest_cache", "__pycache__", "build", "*.egg-info"
                ),
            )
            wheel_dir.mkdir()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheel_dir),
                    str(source_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            wheels = list(wheel_dir.glob("immortal_memory-*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                metadata_name = next(
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")
                )
                metadata = archive.read(metadata_name).decode("utf-8")
                packaged_version = archive.read(
                    "immortal_memory/VERSION"
                ).decode("utf-8").strip()
            self.assertIn(f"Version: {version}\n", metadata)
            self.assertEqual(packaged_version, version)

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--target",
                    str(install_dir),
                    str(wheels[0]),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(install_dir)
            cli = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.argv=['immortal-memory', '--version']; "
                    "from immortal_memory.cli import main; main()",
                ],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(cli.stdout.strip(), f"immortal {version}")


if __name__ == "__main__":
    unittest.main()
