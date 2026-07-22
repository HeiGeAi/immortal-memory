from __future__ import annotations

import os
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from command_hints import cli_command  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def test_cli_command_shell_quotes_each_argument(self) -> None:
        hostile_path = "/tmp/$(touch /tmp/immortal-command-injection)"
        command = cli_command("restore-check", hostile_path)
        self.assertEqual(shlex.split(command)[-2:], ["restore-check", hostile_path])
        self.assertTrue(command.endswith(shlex.quote(hostile_path)))

    def test_pip_install_exposes_working_cli(self) -> None:
        skill_text = (ROOT / "core" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("immortal-memory train", skill_text)
        self.assertNotIn("~/.codex/skills/immortal/immortal.py", skill_text)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
            venv_dir = tmp_path / "venv"
            create_venv = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(
                create_venv.returncode,
                0,
                msg=f"stdout:\n{create_venv.stdout}\nstderr:\n{create_venv.stderr}",
            )

            if os.name == "nt":
                python = venv_dir / "Scripts" / "python.exe"
                command = venv_dir / "Scripts" / "immortal-memory.exe"
            else:
                python = venv_dir / "bin" / "python"
                command = venv_dir / "bin" / "immortal-memory"

            wheel_dir = tmp_path / "wheelhouse"
            build = subprocess.run(
                [str(python), "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), str(source)],
                text=True,
                capture_output=True,
                timeout=180,
            )
            self.assertEqual(
                build.returncode,
                0,
                msg=f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}",
            )
            wheels = list(wheel_dir.glob("immortal_memory-*.whl"))
            self.assertEqual(len(wheels), 1, msg=f"built wheels: {wheels}")

            install = subprocess.run(
                [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
                text=True,
                capture_output=True,
                timeout=180,
            )
            self.assertEqual(
                install.returncode,
                0,
                msg=f"stdout:\n{install.stdout}\nstderr:\n{install.stderr}",
            )

            env = os.environ.copy()
            env["HOME"] = str(tmp_path / "home")
            init = subprocess.run(
                [str(command), "init", "--owner-display-name", "Packaging Test"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(
                init.returncode,
                0,
                msg=f"stdout:\n{init.stdout}\nstderr:\n{init.stderr}",
            )
            self.assertTrue((tmp_path / "home" / ".immortal" / "config.json").is_file())
            self.assertIn("immortal-memory train --smoke", init.stdout)
            self.assertNotIn("~/.codex/skills/immortal/immortal.py", init.stdout)

            help_result = subprocess.run(
                [str(command), "--help"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(help_result.returncode, 0, msg=help_result.stderr)
            self.assertIsNone(re.search(r"^\s+package\s+", help_result.stdout, re.MULTILINE))

            missing_package = subprocess.run(
                [str(command), "package"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertNotEqual(missing_package.returncode, 0)
            self.assertNotIn("package_tool.py", missing_package.stderr + missing_package.stdout)

            smoke = subprocess.run(
                [str(command), "train", "--smoke"],
                env=env,
                text=True,
                capture_output=True,
                timeout=180,
            )
            self.assertEqual(smoke.returncode, 0, msg=f"stdout:\n{smoke.stdout}\nstderr:\n{smoke.stderr}")

            agent_entry = subprocess.run(
                [str(command), "agent-entry"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(
                agent_entry.returncode,
                0,
                msg=f"stdout:\n{agent_entry.stdout}\nstderr:\n{agent_entry.stderr}",
            )
            entry_text = (tmp_path / "home" / ".immortal" / "agent" / "ENTRY.md").read_text(encoding="utf-8")
            claude_prompt = (
                tmp_path / "home" / ".immortal" / "agent" / "claude-code-prompt.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("immortal-memory agent-context", entry_text)
            self.assertIn("<当前任务>", entry_text)
            self.assertIn("immortal-memory agent-context", claude_prompt)
            self.assertIn("本次任务", claude_prompt)
            self.assertNotIn("site-packages/immortal_memory/immortal.py", entry_text + claude_prompt)

            soul = subprocess.run(
                [str(command), "soul"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertNotEqual(soul.returncode, 0)
            self.assertIn("immortal-memory distill", soul.stdout)
            self.assertNotIn("~/.codex/skills/immortal", soul.stdout + soul.stderr)

            restore_guide = subprocess.run(
                [str(command), "restore-guide"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(restore_guide.returncode, 0, msg=restore_guide.stderr)
            self.assertIn("python3 -m pip install", restore_guide.stdout)
            self.assertIn("immortal-memory restore-check", restore_guide.stdout)
            self.assertNotIn("python3 install.py", restore_guide.stdout)
            self.assertNotIn("~/.codex/skills/immortal/immortal.py", restore_guide.stdout)

            cards = subprocess.run(
                [str(command), "cards", "stats"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(cards.returncode, 0, msg=cards.stderr)
            self.assertEqual(json.loads(cards.stdout)["total"], 0)
            self.assertNotIn("cards.py", cards.stdout + cards.stderr)

            product = subprocess.run(
                [str(command), "product"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(product.returncode, 0, msg=f"stdout:\n{product.stdout}\nstderr:\n{product.stderr}")
            product_goal = json.loads(
                (tmp_path / "home" / ".immortal" / "product" / "goal.json").read_text(encoding="utf-8")
            )
            entrypoints = product_goal["stable_entrypoints"]
            self.assertNotIn("package", entrypoints)
            self.assertNotIn("oss_export", entrypoints)
            self.assertNotIn("~/.codex/skills/immortal/immortal.py", json.dumps(entrypoints))

            index_file = tmp_path / "home" / ".immortal" / "index.jsonl"
            index_file.write_text(
                json.dumps(
                    {
                        "source": "packaging-test",
                        "type": "conversation",
                        "role": "user",
                        "timestamp": "2026-07-15T00:00:00+08:00",
                        "content": "This packaging smoke record is deliberately long enough for the dashboard sample.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            dashboard = subprocess.run(
                [str(command), "dashboard"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(dashboard.returncode, 0, msg=f"stdout:\n{dashboard.stdout}\nstderr:\n{dashboard.stderr}")
            dashboard_html = (tmp_path / "home" / ".immortal" / "dashboard.html").read_text(encoding="utf-8")
            self.assertIn("immortal-memory agent-entry", dashboard_html)
            self.assertNotIn("~/.codex/skills/immortal/immortal.py", dashboard_html)


if __name__ == "__main__":
    unittest.main()
