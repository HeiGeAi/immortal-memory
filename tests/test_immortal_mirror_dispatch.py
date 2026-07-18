import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "core" / "immortal.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("immortal_entrypoint", MODULE_PATH)
immortal = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(immortal)


class FeishuMirrorDispatchTests(unittest.TestCase):
    def test_feishu_mirror_shortcut_uses_state_updating_command_handler(self):
        with patch.object(immortal, "command_feishu_mirror", return_value=0) as handler:
            self.assertEqual(immortal.main(["feishu-mirror", "--mode", "inventory"]), 0)

        self.assertEqual(handler.call_args.args[0].feishu_mirror_args, ["--mode", "inventory"])
