"""seen 探测/标记语义测试：already_seen 只查不写，支撑「先探测、fetch 成功再标记」的采集顺序。"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import feishu_collect


def make_seen_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "create table seen (record_key text primary key, source text not null, first_seen_at text not null)"
    )
    return conn


class SeenProbeTest(unittest.TestCase):
    def test_already_seen_does_not_write(self):
        conn = make_seen_conn()
        key = "feishu-doc-content|tok123"
        # 探测不落标记：连续两次探测都为 False，标记表保持空
        self.assertFalse(feishu_collect.already_seen(conn, key))
        self.assertFalse(feishu_collect.already_seen(conn, key))
        count = conn.execute("select count(*) from seen").fetchone()[0]
        self.assertEqual(count, 0)

    def test_probe_then_mark_flow(self):
        conn = make_seen_conn()
        key = "feishu-doc-content|tok123"
        # 模拟采集流：探测未见 → fetch 成功 → 标记 → 下次探测已见
        self.assertFalse(feishu_collect.already_seen(conn, key))
        self.assertTrue(feishu_collect.mark_seen(conn, "feishu-doc-content", key))
        self.assertTrue(feishu_collect.already_seen(conn, key))

    def test_failed_fetch_leaves_no_mark(self):
        conn = make_seen_conn()
        key = "feishu-doc-content|tok123"
        # 关键回归：探测未见但 fetch 失败（不调 mark_seen），文档不被毒化，下轮仍可重试
        if not feishu_collect.already_seen(conn, key):
            fetch_ok = False  # 模拟 run_lark 失败
            if fetch_ok:
                feishu_collect.mark_seen(conn, "feishu-doc-content", key)
        self.assertFalse(feishu_collect.already_seen(conn, key))  # 仍未标记，可重试


if __name__ == "__main__":
    unittest.main()
