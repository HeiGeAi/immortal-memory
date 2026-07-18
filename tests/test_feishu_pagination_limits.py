import sqlite3
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

import feishu_collect


class PaginationContractTest(unittest.TestCase):
    def test_explicit_has_more_false_stops_even_with_token(self):
        self.assertEqual(
            feishu_collect.next_page_token({"has_more": False, "page_token": "stale"}),
            "",
        )

    def test_true_or_missing_has_more_uses_nonempty_token(self):
        self.assertEqual(
            feishu_collect.next_page_token({"has_more": True, "page_token": "next"}),
            "next",
        )
        self.assertEqual(feishu_collect.next_page_token({"page_token": "next"}), "next")
        self.assertEqual(feishu_collect.next_page_token({"has_more": True}), "")


class CollectionLimitTest(unittest.TestCase):
    def test_max_messages_limit_marks_run_partial(self):
        collector = object.__new__(feishu_collect.Collector)
        collector.args = types.SimpleNamespace(
            message_page_size=50,
            message_page_limit=8,
            page_delay=0,
            max_messages=1,
            flush_size=100,
        )
        collector.start = datetime(2026, 7, 18, tzinfo=timezone.utc)
        collector.end = datetime(2026, 7, 19, tzinfo=timezone.utc)
        collector.chats = [
            {"chat_id": "chat-1", "name": "first"},
            {"chat_id": "chat-2", "name": "second"},
        ]
        collector.conn = sqlite3.connect(":memory:")
        collector.conn.execute(
            "create table seen (record_key text primary key, source text not null, first_seen_at text not null)"
        )
        collector.errors = []
        collector.add_records = mock.Mock()
        payload = {
            "data": {
                "messages": [
                    {
                        "message_id": "m1",
                        "content": "one",
                        "create_time": "2026-07-18T01:00:00Z",
                    }
                ],
                "has_more": True,
                "page_token": "next",
            }
        }

        with mock.patch.object(feishu_collect, "run_lark", return_value=(True, payload, "")):
            collector.collect_messages()

        self.assertTrue(collector.errors)
        self.assertIn("max_messages", collector.errors[0]["message"])


if __name__ == "__main__":
    unittest.main()
