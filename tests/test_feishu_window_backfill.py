"""飞书增量窗口停摆兜底测试：连续停摆超过 --days 后，恢复运行必须回补缺口，不漏采。"""

from __future__ import annotations

import argparse
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import feishu_collect
from feishu_collect import LOCAL_TZ


def make_args(days=3, since=None, until=None, all_=False):
    return argparse.Namespace(days=days, since=since, until=until, all=all_)


class WindowBackfillTest(unittest.TestCase):
    def test_no_gap_uses_default_window(self):
        # 上次窗口结束就在昨天，无停摆：起点应为默认 now-days，不被兜底提前
        now = datetime.now(LOCAL_TZ)
        recent_end = (now - timedelta(hours=12)).isoformat()
        start, end = feishu_collect.window_from_args(make_args(days=3), recent_end)
        # 默认起点 ≈ now-3d，兜底不生效
        self.assertLess(abs((start - (end - timedelta(days=3))).total_seconds()), 3600)

    def test_stall_beyond_days_backfills_from_last_window_end(self):
        # 上次窗口结束在 10 天前，--days 3：起点必须回退到 10 天前（覆盖缺口），而非只 now-3d
        now = datetime.now(LOCAL_TZ)
        stale_end = (now - timedelta(days=10)).isoformat()
        start, end = feishu_collect.window_from_args(make_args(days=3), stale_end)
        gap_from_stale = abs((start - (now - timedelta(days=10))).total_seconds())
        self.assertLess(gap_from_stale, 2 * 3600)  # 起点贴着 last_window_end（含 1h 重叠）
        self.assertLess(start, now - timedelta(days=3))  # 明显早于默认窗口

    def test_explicit_since_overrides_backfill(self):
        # 显式 --since 时兜底不介入，用户意图优先
        stale_end = (datetime.now(LOCAL_TZ) - timedelta(days=30)).isoformat()
        start, _ = feishu_collect.window_from_args(make_args(days=3, since="2026-07-15"), stale_end)
        self.assertEqual(start.strftime("%Y-%m-%d"), "2026-07-15")

    def test_all_flag_ignores_backfill(self):
        stale_end = (datetime.now(LOCAL_TZ) - timedelta(days=5)).isoformat()
        start, _ = feishu_collect.window_from_args(make_args(all_=True), stale_end)
        self.assertEqual(start.year, 2020)

    def test_missing_last_window_end_falls_back_to_default(self):
        start, end = feishu_collect.window_from_args(make_args(days=3), None)
        self.assertLess(abs((start - (end - timedelta(days=3))).total_seconds()), 3600)


if __name__ == "__main__":
    unittest.main()
