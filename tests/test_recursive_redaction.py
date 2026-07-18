"""递归脱敏测试：嵌套 metadata、列表、文件名、内部去重字段全部覆盖。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import collect
import feishu_collect
import redact_common

FAKE_SK = "sk-" + "a1B2" * 6              # sk- 形态候选
FAKE_GHP = "ghp_" + "Zx9y" * 6            # GitHub token 形态候选


class RedactTreeTest(unittest.TestCase):
    def test_redact_tree_redacts_nested_dictionary_values(self):
        record = {"metadata": {"message": {"content": f"key is {FAKE_SK}"}}}
        result = redact_common.redact_tree(record)
        self.assertNotIn(FAKE_SK, json.dumps(result))
        self.assertIn("sk-[REDACTED]", result["metadata"]["message"]["content"])

    def test_redact_tree_redacts_list_items_and_file_names(self):
        record = {
            "attachments": [f"report-{FAKE_GHP}.pdf", "normal.txt"],
            "files": ({"name": f"{FAKE_SK}.json"},),
        }
        result = redact_common.redact_tree(record)
        dumped = json.dumps(result)
        self.assertNotIn(FAKE_GHP, dumped)
        self.assertNotIn(FAKE_SK, dumped)
        self.assertIn("normal.txt", dumped)

    def test_redact_tree_preserves_numbers_booleans_and_none(self):
        record = {"count": 42, "ok": True, "missing": None, "ratio": 0.5}
        self.assertEqual(redact_common.redact_tree(record), record)


class CollectWriteTest(unittest.TestCase):
    def test_collect_write_contract_daily_clean_index_keeps_dedup_key(self):
        """契约：daily 全净化；index 保留 _dedup_key 原值（去重消费端依赖），其余 _ 字段两处都删。

        _dedup_key 不脱敏：脱敏会改键值导致与采集端失配、每轮全量重采（79% 膨胀事故同族）。
        键内残留凭证形态由 Task 6 索引重建统一处理。
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daily_dir = tmp_path / "daily"
            daily_dir.mkdir()
            index_file = tmp_path / "index.jsonl"
            dedup_value = "codex-conv|s1|2026-07-17T10:00:00Z|user|abcd1234"
            record = {
                "id": "r1",
                "content": f"正文里的凭证 {FAKE_SK}",
                "_dedup_key": dedup_value,
                "_internal": "x",
            }
            with mock.patch.object(collect, "DAILY_DIR", daily_dir), \
                 mock.patch.object(collect, "INDEX_FILE", index_file):
                collect.write_records({"2026-07-17": [record]})

            daily_text = (daily_dir / "2026-07-17.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("_dedup_key", daily_text)
            self.assertNotIn("_internal", daily_text)
            self.assertNotIn(FAKE_SK, daily_text)

            index_record = json.loads(index_file.read_text(encoding="utf-8"))
            self.assertEqual(index_record["_dedup_key"], dedup_value)  # 原值保留，未被脱敏改写
            self.assertNotIn("_internal", index_record)
            self.assertNotIn(FAKE_SK, index_record["content"])
            self.assertIn("正文里的凭证", index_record["content"])


class FeishuWriteTest(unittest.TestCase):
    def test_feishu_metadata_is_redacted_before_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daily_dir = tmp_path / "daily"
            daily_dir.mkdir()
            index_file = tmp_path / "index.jsonl"
            record = feishu_collect.new_record(
                source="feishu-im",
                record_type="message",
                timestamp="2026-07-17T10:00:00+08:00",
                content="消息正文",
                metadata={"message": {"content": f'{{"text":"token {FAKE_GHP}"}}'}},
            )
            record["_dedup"] = "im|x|y"
            with mock.patch.object(feishu_collect, "DAILY_DIR", daily_dir), \
                 mock.patch.object(feishu_collect, "INDEX_FILE", index_file), \
                 mock.patch.object(feishu_collect, "ensure_dirs"):
                feishu_collect.write_records([record])
            text = index_file.read_text(encoding="utf-8")
            self.assertNotIn(FAKE_GHP, text)
            self.assertNotIn("_dedup", text)
            self.assertIn("消息正文", text)


if __name__ == "__main__":
    unittest.main()
