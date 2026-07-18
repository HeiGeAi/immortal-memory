import contextlib
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import summary


class SummaryReliabilityTest(unittest.TestCase):
    def test_unknown_source_is_included_in_summary(self):
        body = summary.generate_summary(
            "2026-07-18",
            [
                {
                    "source": "feishu-im",
                    "role": "user",
                    "session_id": "chat-1",
                    "content": "customer decision",
                    "project": "/Users/example/project",
                }
            ],
        )

        self.assertIn("feishu-im", body)
        self.assertIn("customer decision", body)
        self.assertNotIn("/Users/example", body)

    def test_gzip_daily_archive_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp)
            path = daily / "2026-07-17.jsonl.gz"
            row = {"source": "test", "content": "from gzip"}
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
            with mock.patch.object(summary, "DAILY_DIR", daily):
                records = summary.load_daily_records("2026-07-17")

        self.assertEqual(records, [row])

    def test_no_arguments_prints_help_without_backfill(self):
        output = io.StringIO()
        with mock.patch.object(summary, "generate_all_summaries") as generate, \
             contextlib.redirect_stdout(output):
            code = summary.main([])

        self.assertEqual(code, 0)
        generate.assert_not_called()
        self.assertIn("usage:", output.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
