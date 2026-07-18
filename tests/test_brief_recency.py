"""brief 近期记录截断测试：配额有限时优先保留最新一天，不被最旧文件挤掉。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import immortal


class BriefRecencyTest(unittest.TestCase):
    def test_recent_day_survives_limit_over_old_bulky_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily"
            daily.mkdir()
            # 旧文件塞满超过 limit 的记录；新文件少量。旧的绝不能把新的挤出去。
            old = daily / "2026-07-16.jsonl"
            with old.open("w", encoding="utf-8") as f:
                for i in range(5000):
                    f.write(json.dumps({"id": f"old{i}", "content": f"旧记录{i}", "source": "codex"}) + "\n")
            new = daily / "2026-07-17.jsonl"
            with new.open("w", encoding="utf-8") as f:
                for i in range(10):
                    f.write(json.dumps({"id": f"new{i}", "content": f"新记录{i}", "source": "codex"}) + "\n")

            with mock.patch.object(immortal, "DAILY_DIR", daily), \
                 mock.patch.object(immortal, "iter_daily_files", return_value=[old, new]):
                records = list(immortal.iter_recent_records(days=2, limit=3000))

            ids = {r["id"] for r in records}
            # 新一天的全部 10 条必须都在
            for i in range(10):
                self.assertIn(f"new{i}", ids)
            # 总数受 limit 约束
            self.assertLessEqual(len(records), 3000)

    def test_limit_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily"
            daily.mkdir()
            f1 = daily / "2026-07-17.jsonl"
            with f1.open("w", encoding="utf-8") as f:
                for i in range(100):
                    f.write(json.dumps({"id": str(i), "content": "x", "source": "codex"}) + "\n")
            with mock.patch.object(immortal, "iter_daily_files", return_value=[f1]):
                records = list(immortal.iter_recent_records(days=1, limit=50))
            self.assertEqual(len(records), 50)


if __name__ == "__main__":
    unittest.main()
