import sqlite3
import types
import unittest
from unittest import mock

import feishu_collect


def seen_connection():
    con = sqlite3.connect(":memory:")
    con.execute(
        "create table seen (record_key text primary key, source text not null, first_seen_at text not null)"
    )
    return con


class FeishuMailProbeTest(unittest.TestCase):
    def make_collector(self):
        collector = object.__new__(feishu_collect.Collector)
        collector.args = types.SimpleNamespace(mail_max=10)
        collector.conn = seen_connection()
        collector.errors = []
        collector.stats = {}
        collector.add_records = mock.Mock()
        return collector

    def run_probe(self, payload):
        collector = self.make_collector()
        responses = [
            (True, {"data": {"mailbox": {"id": "me"}}}, ""),
            (True, payload, ""),
        ]
        with mock.patch.object(feishu_collect, "run_lark", side_effect=responses):
            collector.collect_mail_probe()
        return collector

    def test_messages_key_is_collected(self):
        collector = self.run_probe(
            {"data": {"messages": [{"message_id": "m1", "subject": "Hello"}]}}
        )

        collector.add_records.assert_called_once()
        self.assertEqual(len(collector.add_records.call_args.args[0]), 1)

    def test_items_key_is_collected(self):
        collector = self.run_probe(
            {"data": {"items": [{"id": "m2", "subject": "World"}]}}
        )

        collector.add_records.assert_called_once()
        self.assertEqual(len(collector.add_records.call_args.args[0]), 1)

    def test_unexpected_shape_reports_error(self):
        collector = self.run_probe({"data": {"messages": {"id": "not-a-list"}}})

        self.assertTrue(collector.errors)
        collector.add_records.assert_not_called()


if __name__ == "__main__":
    unittest.main()
