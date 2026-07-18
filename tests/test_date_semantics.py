"""日期归属口径测试：统一按本地时区归日（Blake 2026-07-18 拍板）。

历史 daily 文件不迁移；只约束新写入的归日逻辑。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import collect


class LocalDateSemanticsTest(unittest.TestCase):
    def test_utc_evening_timestamp_belongs_to_local_next_day(self):
        # UTC 2026-07-17 20:00 = 本地（UTC+8）2026-07-18 04:00，必须归 07-18
        local_expected = (
            datetime.fromisoformat("2026-07-17T20:00:00+00:00").astimezone().strftime("%Y-%m-%d")
        )
        self.assertEqual(collect.get_date_from_timestamp("2026-07-17T20:00:00Z"), local_expected)

    def test_naive_local_timestamp_is_unchanged(self):
        # Hermes 的 '+8 hours' SQL 产物是 naive 本地字符串，astimezone 恒等，不得双重偏移
        self.assertEqual(collect.get_date_from_timestamp("2026-07-17 10:00:00"), "2026-07-17")

    def test_mtime_date_uses_local_timezone(self):
        # 构造一个已知 UTC 时刻的 mtime：UTC 2026-07-16 22:00 = 本地 07-17 06:00
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            path = Path(handle.name)
        try:
            utc_dt = datetime(2026, 7, 16, 22, 0, 0, tzinfo=timezone.utc)
            os.utime(path, (utc_dt.timestamp(), utc_dt.timestamp()))
            expected = utc_dt.astimezone().strftime("%Y-%m-%d")
            self.assertEqual(collect.get_date_from_mtime(path), expected)
        finally:
            path.unlink(missing_ok=True)

    def test_invalid_timestamp_falls_back_to_local_today(self):
        self.assertEqual(
            collect.get_date_from_timestamp("not-a-timestamp"),
            datetime.now().astimezone().strftime("%Y-%m-%d"),
        )


if __name__ == "__main__":
    unittest.main()
