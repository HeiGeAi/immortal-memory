"""便携导出自动回收测试：保留最近 N 份、至少留 1 份、dry-run 零删除、少于 N 份不删。"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import cleanup


def make_exports(root: Path, count: int) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    dirs = []
    for i in range(count):
        d = root / f"immortal-export-2026070{i}T000000Z"
        d.mkdir()
        (d / "manifest.json").write_text("{}", encoding="utf-8")
        (d / "index.jsonl").write_text("x" * 1000, encoding="utf-8")
        # 递增 mtime 保证时间序稳定
        ts = 1_700_000_000 + i * 100
        import os
        os.utime(d, (ts, ts))
        dirs.append(d)
    return dirs


class PruneExportsTest(unittest.TestCase):
    def test_keeps_newest_n_deletes_older(self):
        with tempfile.TemporaryDirectory() as tmp:
            exports = Path(tmp) / "exports"
            make_exports(exports, 5)
            with mock.patch.object(cleanup, "EXPORTS_DIR", exports):
                result = cleanup.prune_exports(keep=3, dry_run=False, yes=True)
            self.assertEqual(result["deleted"], 2)
            self.assertEqual(result["kept"], 3)
            remaining = sorted(p.name for p in exports.glob("immortal-export-*"))
            self.assertEqual(len(remaining), 3)
            # 保留的是最新 3 份（下标 2,3,4）
            self.assertIn("immortal-export-20260704T000000Z", remaining)
            self.assertNotIn("immortal-export-20260700T000000Z", remaining)

    def test_fewer_than_keep_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            exports = Path(tmp) / "exports"
            make_exports(exports, 2)
            with mock.patch.object(cleanup, "EXPORTS_DIR", exports):
                result = cleanup.prune_exports(keep=3, dry_run=False, yes=True)
            self.assertEqual(result["deleted"], 0)
            self.assertEqual(len(list(exports.glob("immortal-export-*"))), 2)

    def test_dry_run_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            exports = Path(tmp) / "exports"
            make_exports(exports, 5)
            with mock.patch.object(cleanup, "EXPORTS_DIR", exports):
                result = cleanup.prune_exports(keep=3, dry_run=True, yes=False)
            self.assertEqual(result["deleted"], 2)  # 报告可删数
            self.assertEqual(len(list(exports.glob("immortal-export-*"))), 5)  # 实际未删

    def test_keep_floor_is_one(self):
        # keep=0 也至少留 1 份，绝不清空所有备份
        with tempfile.TemporaryDirectory() as tmp:
            exports = Path(tmp) / "exports"
            make_exports(exports, 3)
            with mock.patch.object(cleanup, "EXPORTS_DIR", exports):
                result = cleanup.prune_exports(keep=0, dry_run=False, yes=True)
            self.assertGreaterEqual(len(list(exports.glob("immortal-export-*"))), 1)


if __name__ == "__main__":
    unittest.main()
