"""secret_scan 与 export 出口门测试：报告只含形态计数与哈希，严格模式拒绝带凭证导出。"""

from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import export_restore
import secret_scan

FAKE_SK = "sk-" + "a1B2" * 6
FAKE_AKIA = "AKIA" + "IOSFODNN7EXAMPLE"


class SecretScanTest(unittest.TestCase):
    def test_secret_scan_reports_only_pattern_count_and_value_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sample.jsonl"
            target.write_text(
                json.dumps({"content": f"key {FAKE_SK}"}) + "\n"
                + json.dumps({"content": f"aws {FAKE_AKIA}"}) + "\n"
                + json.dumps({"content": "干净记录"}) + "\n",
                encoding="utf-8",
            )
            report = secret_scan.scan_file(target)
            dumped = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(FAKE_SK, dumped)
            self.assertNotIn(FAKE_AKIA, dumped)
            self.assertEqual(report["unique_by_pattern"].get("sk_key"), 1)
            self.assertEqual(report["unique_by_pattern"].get("aws_key"), 1)
            for finding in report["findings"]:
                self.assertEqual(len(finding["value_sha256_16"]), 16)

    def test_redacted_placeholders_are_not_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clean.jsonl"
            target.write_text(
                json.dumps({"content": "sk-[REDACTED] 与 AKIA 无关文本"}) + "\n",
                encoding="utf-8",
            )
            report = secret_scan.scan_file(target)
            self.assertEqual(report["unique_candidates"], 0)

    def test_json_scan_does_not_join_across_escaped_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "escaped.jsonl"
            escaped = (
                "https://example.test:"
                + chr(10)
                + "not-a-password"
                + chr(64)
                + "example.test"
            )
            target.write_text(
                json.dumps({"content": escaped}) + "\n",
                encoding="utf-8",
            )

            report = secret_scan.scan_file(target)

            self.assertEqual(report["unique_candidates"], 0)

    def test_redacted_copy_preserves_source_and_structural_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.jsonl"
            destination = Path(tmp) / "redacted.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "id": "memory-1",
                        "content": f"secret {FAKE_SK}",
                        "metadata": {"token": FAKE_AKIA},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = source.read_bytes()
            before_sha = hashlib.sha256(before).hexdigest()

            receipt = secret_scan.redact_jsonl_copy(source, destination)

            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(receipt["source_sha256"], before_sha)
            self.assertEqual(receipt["unique_candidates"], 2)
            self.assertEqual(secret_scan.scan_file(destination)["unique_candidates"], 0)
            restored = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(restored["id"], "memory-1")
            self.assertNotIn(FAKE_SK, destination.read_text(encoding="utf-8"))
            self.assertNotIn(FAKE_AKIA, destination.read_text(encoding="utf-8"))

    def test_redacted_copy_rejects_oversized_jsonl_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.jsonl"
            destination = Path(tmp) / "redacted.jsonl"
            source.write_text(json.dumps({"content": "x" * 128}) + "\n", encoding="utf-8")
            original_limit = secret_scan.MAX_JSONL_LINE_BYTES
            secret_scan.MAX_JSONL_LINE_BYTES = 32
            try:
                with self.assertRaises(ValueError):
                    secret_scan.redact_jsonl_copy(source, destination)
            finally:
                secret_scan.MAX_JSONL_LINE_BYTES = original_limit
            self.assertFalse(destination.exists())


class ExportSecretGateTest(unittest.TestCase):
    def _make_vault(self, tmp: str, index_content: str) -> Path:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (vault / "index.jsonl").write_text(index_content, encoding="utf-8")
        (vault / "sources.json").write_text("{}", encoding="utf-8")
        (vault / "orchestrator_state.json").write_text("{}", encoding="utf-8")
        return vault

    def test_export_rejects_unredacted_secret_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._make_vault(tmp, json.dumps({"content": f"leak {FAKE_SK}"}) + "\n")
            with self.assertRaises(export_restore.SecretShapesFound):
                export_restore.create_export(vault, fail_on_secrets=True)
            # fail-closed 时不留下半成品导出目录
            leftovers = [p for p in (vault / "exports").glob("immortal-export-*")] if (vault / "exports").exists() else []
            self.assertEqual(leftovers, [])

    def test_export_without_strict_mode_records_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._make_vault(tmp, json.dumps({"content": f"leak {FAKE_SK}"}) + "\n")
            manifest = export_restore.create_export(vault, fail_on_secrets=False)
            self.assertTrue(any("secret_shapes_present" in w for w in manifest["warnings"]))
            self.assertEqual(manifest["secret_scan"]["unique_candidates"], 1)

    def test_clean_vault_export_has_no_secret_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._make_vault(tmp, json.dumps({"content": "干净记录"}) + "\n")
            manifest = export_restore.create_export(vault, fail_on_secrets=True)
            self.assertEqual(manifest["secret_scan"]["unique_candidates"], 0)

    def test_redacted_export_is_strictly_clean_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_line = json.dumps({"id": "memory-1", "content": f"leak {FAKE_SK}"}) + "\n"
            vault = self._make_vault(tmp, source_line)
            source_before = (vault / "index.jsonl").read_bytes()

            manifest = export_restore.create_export(
                vault,
                Path(tmp) / "external",
                fail_on_secrets=True,
                redact_secrets=True,
            )

            export_dir = Path(manifest["export_dir"])
            self.assertEqual((vault / "index.jsonl").read_bytes(), source_before)
            self.assertEqual(manifest["secret_scan"]["unique_candidates"], 0)
            self.assertEqual(manifest["secret_redaction"]["unique_candidates"], 1)
            self.assertTrue((export_dir / "secret-redaction-receipt.json").is_file())
            self.assertNotIn(FAKE_SK, (export_dir / "index.jsonl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
