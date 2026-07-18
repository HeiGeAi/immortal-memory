"""daily-check.sh 测试：子命令崩溃、命令缺失、全部通过三种情形的真实退出码。"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path.home() / ".immortal" / "health-check" / "daily-check.sh"


def write_fake_immortal(tmp: Path, behavior: str) -> Path:
    """生成假 immortal.py：behavior 为 'ok' 全过、'traceback' 崩溃。"""
    fake = tmp / "fake_immortal.py"
    if behavior == "ok":
        fake.write_text(
            "import sys\n"
            "print('Immortal Skill status')\n"
            "print('Last collect: 2099-01-01 00:00:00 CST')\n"
            "print('Errors: none')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
    else:
        fake.write_text(
            "raise RuntimeError('boom from fake immortal')\n",
            encoding="utf-8",
        )
    return fake


def run_script(tmp: Path, immortal_bin: str) -> subprocess.CompletedProcess:
    logdir = tmp / "logs"
    # 遮蔽 osascript，测试时不弹系统通知
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    fake_osascript = bindir / "osascript"
    fake_osascript.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_osascript.chmod(fake_osascript.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["IMMORTAL_BIN"] = immortal_bin
    env["IMMORTAL_HEALTH_LOGDIR"] = str(logdir)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )


class HealthCheckShellTest(unittest.TestCase):
    def test_health_script_fails_on_python_traceback_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = write_fake_immortal(tmp_path, "traceback")
            proc = run_script(tmp_path, str(fake))
            self.assertEqual(proc.returncode, 1)
            log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
            self.assertIn("子命令失败", log)

    def test_health_script_fails_when_command_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc = run_script(tmp_path, str(tmp_path / "no-such-immortal.py"))
            self.assertEqual(proc.returncode, 1)

    def test_health_script_passes_only_when_all_three_commands_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = write_fake_immortal(tmp_path, "ok")
            proc = run_script(tmp_path, str(fake))
            self.assertEqual(proc.returncode, 0)
            log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
            self.assertIn("OK — 全部健康项通过", log)


if __name__ == "__main__":
    unittest.main()
