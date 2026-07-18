"""归因审计熔断测试：records 缺失导致高隔离时，--apply 必须拒绝写回并非零退出。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import profile_attribution_audit as audit


class CircuitBreakerTest(unittest.TestCase):
    def test_low_coverage_high_quarantine_trips(self):
        # records 覆盖率 0（文件缺失）+ 隔离率 85%：必须熔断
        self.assertTrue(audit.should_circuit_break(records_coverage=0.0, quarantine_rate=0.85))

    def test_healthy_run_does_not_trip(self):
        # records 全命中 + 零隔离：正常，不熔断
        self.assertFalse(audit.should_circuit_break(records_coverage=1.0, quarantine_rate=0.0))

    def test_high_coverage_high_quarantine_does_not_trip(self):
        # records 命中充足但确实隔离多（真实脏数据）：不熔断，让隔离生效
        self.assertFalse(audit.should_circuit_break(records_coverage=0.95, quarantine_rate=0.7))

    def test_low_coverage_low_quarantine_does_not_trip(self):
        # records 少但隔离也少（小样本正常）：不熔断
        self.assertFalse(audit.should_circuit_break(records_coverage=0.3, quarantine_rate=0.1))

    def test_boundary_exact_thresholds_do_not_trip(self):
        # 边界值恰好等于阈值：不熔断（严格小于/大于才触发）
        self.assertFalse(audit.should_circuit_break(records_coverage=0.5, quarantine_rate=0.5))


if __name__ == "__main__":
    unittest.main()
