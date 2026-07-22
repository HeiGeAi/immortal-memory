import importlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


index_db = importlib.import_module("index_db")
index_integrity = importlib.import_module("index_integrity")


def record(rec_id, content):
    return {
        "id": rec_id,
        "timestamp": "2026-07-18T12:00:00+08:00",
        "source": "test",
        "role": "user",
        "project": "immortal",
        "content": content,
    }


class IndexDbReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.index_file = self.root / "index.jsonl"
        self.db_file = self.root / "search_index.db"
        self.patchers = [
            mock.patch.object(index_db, "IMMORTAL_DIR", self.root),
            mock.patch.object(index_db, "INDEX_FILE", self.index_file),
            mock.patch.object(index_db, "DB_FILE", self.db_file),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()

    def write_records(self, rows):
        body = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows)
        self.index_file.write_text(body, encoding="utf-8")

    def doc_ids(self):
        with sqlite3.connect(str(self.db_file)) as con:
            return [
                row[0]
                for row in con.execute("SELECT rec_id FROM docs ORDER BY rec_id").fetchall()
            ]

    def test_source_shrink_rebuilds_without_nested_transaction(self):
        self.write_records([record("one", "first"), record("two", "second")])
        self.assertEqual(index_db.sync(), 2)

        self.write_records([record("replacement", "smaller source")])

        self.assertEqual(index_db.sync(), 1)
        self.assertEqual(self.doc_ids(), ["replacement"])

    def test_unterminated_tail_is_retried_after_append_completes_it(self):
        first = json.dumps(record("one", "complete"), ensure_ascii=False) + "\n"
        second = json.dumps(record("two", "completed later"), ensure_ascii=False)
        split = len(second) // 2
        self.index_file.write_text(first + second[:split], encoding="utf-8")

        with self.assertRaisesRegex(
            index_integrity.IndexIntegrityError,
            "malformed JSONL at line 2",
        ):
            index_db.sync()
        self.assertFalse(self.db_file.exists())

        with self.index_file.open("a", encoding="utf-8") as handle:
            handle.write(second[split:] + "\n")

        self.assertEqual(index_db.sync(), 2)
        self.assertEqual(self.doc_ids(), ["one", "two"])

    def test_concurrent_sync_indexes_each_byte_range_once(self):
        self.write_records([record("one", "first")])
        self.assertEqual(index_db.sync(), 1)
        with self.index_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record("two", "second"), ensure_ascii=False) + "\n")
        start_barrier = threading.Barrier(2)

        results = []
        errors = []

        def worker():
            try:
                start_barrier.wait(timeout=5)
                results.append(index_db.sync())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(self.doc_ids(), ["one", "two"])

    def test_date_filter_uses_local_calendar_day(self):
        row = record("boundary", "local calendar boundary")
        row["timestamp"] = "2026-07-17T20:00:00Z"
        local_day = "2026-07-18"
        self.write_records([row])
        with mock.patch.object(index_db, "local_date", return_value=local_day):
            self.assertEqual(index_db.sync(), 1)
            _, rankings = index_db.channels(
                "calendar",
                since=local_day,
                until=local_day,
            )

        self.assertTrue(rankings)
        self.assertEqual(rankings[0][0][1]["id"], "boundary")


if __name__ == "__main__":
    unittest.main()
