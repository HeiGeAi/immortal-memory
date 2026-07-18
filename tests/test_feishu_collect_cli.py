import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "core" / "feishu_collect.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("immortal_feishu_collect", MODULE_PATH)
feishu_collect = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(feishu_collect)


class FeishuCollectCliTests(unittest.TestCase):
    def test_collect_chats_uses_current_lark_cli_command_and_response_shape(self):
        collector = object.__new__(feishu_collect.Collector)
        collector.args = SimpleNamespace(
            chat_page_size=100,
            max_chats=0,
            chat_page_limit=0,
            page_delay=0,
        )
        collector.chats = []
        collector.errors = []
        collector.conn = SimpleNamespace(commit=lambda: None)
        collector.error = lambda source, message: collector.errors.append((source, message))
        collector.add_records = lambda records: None

        response = {
            "data": {
                "chats": [{"chat_id": "oc_test", "name": "test chat"}],
                "has_more": False,
            }
        }
        with patch.object(feishu_collect, "run_lark", return_value=(True, response, "")) as run_lark, patch.object(
            feishu_collect, "mark_seen", return_value=False
        ):
            collector.collect_chats()

        self.assertEqual(
            run_lark.call_args.args[0],
            [
                "im",
                "+chat-list",
                "--as",
                "user",
                "--page-size",
                "100",
                "--sort",
                "create_time",
                "--format",
                "json",
            ],
        )
        self.assertEqual(collector.chats, [{"chat_id": "oc_test", "name": "test chat"}])
        self.assertEqual(collector.errors, [])
