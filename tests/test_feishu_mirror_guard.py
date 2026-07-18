"""账号隔离守卫回归：feishu mirror 的连字符形与空格双词形都必须注入守卫参数。

红线：飞书镜像绝不能对未经守卫校验的登录账号执行（账号身份硬隔离）。
历史 commit 2a9b3a6 修了连字符形，漏了双词形；此测试锁死两条路径同权。
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import immortal


GUARD = ["--expected-user-name", "TestOwner", "--reject-user-name", "Other"]


class MirrorGuardTest(unittest.TestCase):
    def _captured_forwarded(self, dispatch):
        """运行 dispatch 回调，捕获最终传给 feishu_drive_mirror.py 的参数。"""
        captured = {}

        def fake_run_script(script, args):
            captured["script"] = script
            captured["args"] = list(args)
            return 0

        with mock.patch.object(immortal, "run_script", side_effect=fake_run_script), \
             mock.patch.object(immortal, "feishu_guard_args", return_value=list(GUARD)), \
             mock.patch.object(immortal, "load_config", return_value={}), \
             mock.patch.object(immortal, "write_state_key"), \
             mock.patch.object(immortal, "clear_state_error"):
            dispatch()
        return captured

    def test_hyphen_form_injects_guard(self):
        captured = self._captured_forwarded(
            lambda: immortal.command_feishu_mirror(
                argparse.Namespace(feishu_mirror_args=["--mode", "all"])
            )
        )
        self.assertIn("--expected-user-name", captured["args"])
        self.assertIn("--reject-user-name", captured["args"])

    def test_two_word_form_injects_guard(self):
        # 直接驱动分发逻辑：两词形也必须落到 command_feishu_mirror 注入守卫
        captured = self._captured_forwarded(
            lambda: immortal.command_feishu_mirror(
                argparse.Namespace(feishu_mirror_args=["--mode", "all"])
            )
        )
        self.assertIn("--expected-user-name", captured["args"])

    def test_dispatch_two_word_mirror_routes_through_guard(self):
        # 端到端：main 分发 ["feishu","mirror",...] 必须经守卫，不得裸透传
        calls = {}

        def fake_run_script(script, args):
            calls["args"] = list(args)
            return 0

        with mock.patch.object(immortal, "run_script", side_effect=fake_run_script), \
             mock.patch.object(immortal, "feishu_guard_args", return_value=list(GUARD)), \
             mock.patch.object(immortal, "load_config", return_value={}), \
             mock.patch.object(immortal, "write_state_key"), \
             mock.patch.object(immortal, "clear_state_error"), \
             mock.patch.object(sys, "argv", ["immortal.py", "feishu", "mirror", "--mode", "all"]):
            immortal.main()
        self.assertIn("--expected-user-name", calls.get("args", []))

    def test_explicit_guard_not_double_injected(self):
        captured = self._captured_forwarded(
            lambda: immortal.command_feishu_mirror(
                argparse.Namespace(feishu_mirror_args=["--expected-user-name", "Manual", "--mode", "all"])
            )
        )
        self.assertEqual(captured["args"].count("--expected-user-name"), 1)


if __name__ == "__main__":
    unittest.main()
