"""飞书部分失败语义测试：partial 必须以退出码 2 传递，失败源不得刷新 last_backup。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import feishu_collect
import orchestrator


class FeishuExitCodeTest(unittest.TestCase):
    def test_feishu_returns_partial_exit_code_when_any_requested_source_errors(self):
        self.assertEqual(feishu_collect.run_exit_code([{"source": "feishu-im", "message": "denied"}]), 2)
        self.assertEqual(feishu_collect.run_exit_code([]), 0)


class UpdateSourcesBackupTest(unittest.TestCase):
    def test_feishu_updates_last_backup_only_for_successful_requested_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources_file = Path(tmp) / "sources.json"
            old = "2026-07-01T00:00:00+08:00"
            sources_file.write_text(json.dumps({
                "sources": [
                    {"name": "feishu-chat", "type": "feishu-chat", "last_backup": old},
                    {"name": "feishu-im", "type": "feishu-im", "last_backup": old},
                    {"name": "feishu-task", "type": "feishu-task", "last_backup": old},
                ]
            }), encoding="utf-8")
            stats = {"feishu-chat": 5, "feishu-im": 0}  # feishu-task 本轮未请求
            with mock.patch.object(feishu_collect, "SOURCES_FILE", sources_file):
                feishu_collect.update_sources_backup(stats, failed_sources={"feishu-im"})
            updated = json.loads(sources_file.read_text(encoding="utf-8"))["sources"]
            by_name = {item["name"]: item for item in updated}
            self.assertNotEqual(by_name["feishu-chat"]["last_backup"], old)   # 请求成功：刷新
            self.assertEqual(by_name["feishu-im"]["last_backup"], old)        # 请求但失败：保持旧值
            self.assertEqual(by_name["feishu-task"]["last_backup"], old)      # 未请求：保持旧值


class OrchestratorPartialTest(unittest.TestCase):
    def _run(self, rc: int, out: str = "Feishu collect finished\nNew records:\n  feishu-im: 3\n"):
        with mock.patch.object(orchestrator, "log"), \
             mock.patch.object(orchestrator, "feishu_daily_args", return_value=["--x"]), \
             mock.patch.object(orchestrator, "run_script_rc", return_value=(rc, out)):
            return orchestrator.collect_feishu()

    def test_orchestrator_does_not_mark_partial_feishu_as_complete(self):
        status, _ = self._run(2)
        self.assertEqual(status, "partial")

    def test_orchestrator_full_success_stays_ok(self):
        status, _ = self._run(0)
        self.assertEqual(status, "ok")

    def test_orchestrator_fatal_is_failed(self):
        status, _ = self._run(1)
        self.assertEqual(status, "failed")

    def test_argparse_usage_error_exit_two_is_failed_not_partial(self):
        # argparse 用法错误也固定退 2，但没有完成标记，必须判 failed，不能掩盖成 partial
        status, _ = self._run(2, out="usage: feishu_collect.py [-h] ...\nerror: unrecognized arguments: --x\n")
        self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()
