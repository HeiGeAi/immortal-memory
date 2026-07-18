"""维护冻结门测试：marker 存在时不可逆清理必须跳过，非破坏步骤照常。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import cleanup as cleanup_module
import orchestrator


class MaintenanceFreezeTest(unittest.TestCase):
    def test_cleanup_is_blocked_by_maintenance_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "MAINTENANCE_FREEZE_DESTRUCTIVE"
            marker.touch()
            with mock.patch.object(orchestrator, "FREEZE_MARKER", marker), \
                 mock.patch.object(orchestrator, "log"), \
                 mock.patch.object(orchestrator, "run_script") as run_script:
                # "frozen" 三态：跳过但不得记账为完成（否则解冻后清理被顺延最多 7 天）
                self.assertEqual(orchestrator.cleanup(), "frozen")
                run_script.assert_not_called()

    def test_cleanup_without_marker_runs_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "MAINTENANCE_FREEZE_DESTRUCTIVE"
            with mock.patch.object(orchestrator, "FREEZE_MARKER", marker), \
                 mock.patch.object(orchestrator, "log"), \
                 mock.patch.object(orchestrator, "run_script", return_value=(True, "")) as run_script:
                self.assertTrue(orchestrator.cleanup())
                run_script.assert_called_once()

    def test_non_destructive_steps_continue_during_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "MAINTENANCE_FREEZE_DESTRUCTIVE"
            marker.touch()
            with mock.patch.object(orchestrator, "FREEZE_MARKER", marker), \
                 mock.patch.object(orchestrator, "log"), \
                 mock.patch.object(orchestrator, "run_script", return_value=(True, "")) as run_script:
                orchestrator.summarize()
                run_script.assert_called_once()

    def test_cleanup_dry_run_never_trims_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for sub in ("files", "daily", "exports", "sessions"):
                (tmp_path / sub).mkdir()
            with mock.patch.object(sys, "argv", ["cleanup.py", "--dry-run"]), \
                 mock.patch.object(cleanup_module, "IMMORTAL_DIR", tmp_path), \
                 mock.patch.object(cleanup_module, "FILES_DIR", tmp_path / "files"), \
                 mock.patch.object(cleanup_module, "DAILY_DIR", tmp_path / "daily"), \
                 mock.patch.object(cleanup_module, "LOG_FILE", tmp_path / "backup.log"), \
                 mock.patch.object(cleanup_module, "EXPORTS_DIR", tmp_path / "exports"), \
                 mock.patch.object(cleanup_module, "SESSIONS_DIR", tmp_path / "sessions"), \
                 mock.patch.object(cleanup_module, "STATE_FILE", tmp_path / "orchestrator_state.json"), \
                 mock.patch.object(cleanup_module, "trim_log") as trim:
                cleanup_module.main()
                trim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
