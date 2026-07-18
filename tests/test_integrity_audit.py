import gzip
import json
import tempfile
import unittest
from pathlib import Path

import integrity_audit


class IntegrityAuditTest(unittest.TestCase):
    def test_reports_fact_layer_drift_without_exposing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "daily"
            daily.mkdir()
            index = root / "index.jsonl"
            rows = [
                {"id": "a", "source": "one", "timestamp": "2026-01-01T00:00:00Z"},
                {"id": "b", "source": "two", "timestamp": "2026-02-01T00:00:00Z"},
                {"id": "a", "source": "one", "timestamp": "2026-01-01T00:00:00Z"},
            ]
            with gzip.open(daily / "2026-01-01.jsonl.gz", "wt", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
                handle.write("{broken\n")
            with index.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(rows[0]) + "\n")
                handle.write(json.dumps({"id": "c", "source": "three", "timestamp": "2026-03-01T00:00:00Z"}) + "\n")

            report = integrity_audit.audit(daily, index)

        self.assertEqual(report["daily"]["valid_rows"], 3)
        self.assertEqual(report["daily"]["malformed_rows"], 1)
        self.assertEqual(report["daily"]["duplicate_ids"], 1)
        self.assertEqual(report["index"]["valid_rows"], 2)
        self.assertEqual(report["comparison"]["daily_only_ids"], 1)
        self.assertEqual(report["comparison"]["index_only_ids"], 1)
        serialized = json.dumps(report)
        self.assertNotIn('"a"', serialized)
        self.assertNotIn('"b"', serialized)
        self.assertNotIn('"c"', serialized)


if __name__ == "__main__":
    unittest.main()
