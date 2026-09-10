"""feedback 报告测试：不再读已停用 digest，失败与通知失败必须以非零退出码传递。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import feedback_report


def now_iso(offset_hours: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()


def make_vault(tmp: str, *, state_errors=None, feishu_errors=None, feishu_age_hours=1.0,
               with_stale_digest=False) -> Path:
    vault = Path(tmp) / "vault"
    (vault / "quality").mkdir(parents=True)
    (vault / "feishu").mkdir(parents=True)
    (vault / "orchestrator_state.json").write_text(json.dumps({
        "total_records": 100,
        "collect_count": 5,
        "last_collect": now_iso(-1),
        "last_run_new_records": 10,
        "last_run_feishu_new_records": 2,
        "errors": state_errors or [],
    }), encoding="utf-8")
    (vault / "quality" / "latest.json").write_text(json.dumps({
        "generated_at": now_iso(-1),
        "status": "ok",
        "score": 100,
        "issue_count": 0,
        "top_issues": [],
    }), encoding="utf-8")
    (vault / "feishu" / "state.json").write_text(json.dumps({
        "last_run_at": now_iso(-feishu_age_hours),
        "last_errors": feishu_errors or [],
    }), encoding="utf-8")
    if with_stale_digest:
        (vault / "digests").mkdir()
        (vault / "digests" / "latest.json").write_text(json.dumps({
            "generated_at": "2026-07-05T00:00:00+00:00",
            "errors": {"status": "error", "current": ["七月五日的旧飞书错误"]},
            "summary": {"total_records": 335242},
        }), encoding="utf-8")
    return vault


class BuildReportTest(unittest.TestCase):
    def test_feedback_ignores_disabled_stale_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp, with_stale_digest=True)
            report = feedback_report.build_report(vault, run_status=0)
            self.assertEqual(report["errors"]["current"], [])
            self.assertNotIn("七月五日的旧飞书错误", json.dumps(report, ensure_ascii=False))
            self.assertEqual(report["status"], "ok")

    def test_stale_quality_input_is_flagged_not_inherited(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            (vault / "quality" / "latest.json").write_text(json.dumps({
                "generated_at": now_iso(-100),
                "status": "error",
                "score": 4,
                "issue_count": 9,
            }), encoding="utf-8")
            report = feedback_report.build_report(vault, run_status=0)
            self.assertTrue(any("quality" in item for item in report["stale_inputs"]))
            self.assertNotEqual(report["status"], "failed")

    def test_feishu_source_errors_yield_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp, feishu_errors=[{"source": "feishu-im", "message": "denied"}])
            report = feedback_report.build_report(vault, run_status=0)
            self.assertEqual(report["status"], "partial")

    def test_fresh_feishu_success_overrides_stale_orchestrator_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            state_path = vault / "orchestrator_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["last_feishu_status"] = "partial"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            report = feedback_report.build_report(vault, run_status=0)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["feishu"]["last_status"], "ok")


class MainExitCodeTest(unittest.TestCase):
    def _main(self, vault: Path, argv_extra: list[str]) -> int:
        argv = ["feedback_report.py", "--vault-dir", str(vault)] + argv_extra
        with mock.patch.object(sys, "argv", argv):
            return feedback_report.main()

    def test_feedback_returns_nonzero_for_failed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            self.assertEqual(self._main(vault, ["--run-status", "1"]), 1)

    def test_feedback_returns_two_for_partial_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            self.assertEqual(self._main(vault, ["--run-status", "2"]), 2)

    def test_feedback_returns_nonzero_when_required_notification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            with mock.patch.object(feedback_report, "send_notification", return_value=(False, "osascript missing")):
                self.assertEqual(self._main(vault, ["--notify"]), 1)

    def test_feedback_persists_bounded_notification_delivery_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            with mock.patch.object(feedback_report, "send_notification", return_value=(False, "osascript missing")):
                self.assertEqual(self._main(vault, ["--notify"]), 1)

            latest = json.loads((vault / "feedback" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                latest["notification"],
                {"requested": True, "status": "failed"},
            )

    def test_feedback_success_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            self.assertEqual(self._main(vault, []), 0)

    def test_partial_feedback_notification_is_marked_for_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp, feishu_errors=[{"source": "feishu-im", "message": "denied"}])
            report = feedback_report.build_report(vault, run_status=0)
            captured = []

            def fake_run(args, **_kwargs):
                captured.append(args)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(feedback_report.subprocess, "run", side_effect=fake_run):
                sent, _detail = feedback_report.send_notification(report)

            self.assertTrue(sent)
            self.assertIn("需要关注", captured[0][-1])


if __name__ == "__main__":
    unittest.main()
