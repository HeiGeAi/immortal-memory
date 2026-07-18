"""备份校验 fail-closed 测试：strict 模式下任何异常都不允许返回成功。"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import export_restore


def write_file(base: Path, relpath: str, content: bytes) -> dict:
    path = base / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "relpath": relpath,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def write_manifest(base: Path, items: list, warnings: list | None = None) -> None:
    manifest = {
        "generated_at": "2026-07-17T00:00:00+00:00",
        "items": items,
        "totals": {"files": len(items), "bytes": sum(i.get("size", 0) for i in items if isinstance(i, dict))},
        "warnings": warnings or [],
    }
    (base / export_restore.MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )


class RestoreCheckStrictTest(unittest.TestCase):
    def make_export(self, tmp: str) -> Path:
        base = Path(tmp) / "export"
        base.mkdir()
        return base

    def test_strict_rejects_unsafe_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.make_export(tmp)
            good = write_file(base, "a.txt", b"hello")
            write_manifest(base, [good, {"relpath": "../outside", "size": 1, "sha256": "x"}])
            result = export_restore.restore_check(base, strict=True)
            self.assertFalse(result["ok"])

    def test_strict_rejects_duplicate_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.make_export(tmp)
            good = write_file(base, "a.txt", b"hello")
            write_manifest(base, [good, dict(good)])
            result = export_restore.restore_check(base, strict=True)
            self.assertFalse(result["ok"])

    def test_strict_rejects_manifest_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.make_export(tmp)
            good = write_file(base, "a.txt", b"hello")
            write_manifest(base, [good], warnings=["missing: timeline.html"])
            result = export_restore.restore_check(base, strict=True)
            self.assertFalse(result["ok"])
            self.assertTrue(any("manifest_warning" in w for w in result["warnings"]))

    def test_strict_rejects_invalid_manifest_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.make_export(tmp)
            good = write_file(base, "a.txt", b"hello")
            write_manifest(base, [good, "not-a-dict"])
            result = export_restore.restore_check(base, strict=True)
            self.assertFalse(result["ok"])

    def test_strict_rejects_extra_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.make_export(tmp)
            good = write_file(base, "a.txt", b"hello")
            write_manifest(base, [good])
            (base / "extra.bin").write_bytes(b"stray")
            result = export_restore.restore_check(base, strict=True)
            self.assertFalse(result["ok"])

    def test_restore_check_rejects_size_and_sha256_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.make_export(tmp)
            good = write_file(base, "a.txt", b"hello")
            (base / "a.txt").write_bytes(b"tampered!")
            write_manifest(base, [good])
            result = export_restore.restore_check(base, strict=False)
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["mismatched"]), 1)

    def test_clean_export_passes_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self.make_export(tmp)
            items = [write_file(base, "a.txt", b"hello"), write_file(base, "sub/b.txt", b"world")]
            write_manifest(base, items)
            result = export_restore.restore_check(base, strict=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["checked_files"], 2)


class BackupStatusTrustTest(unittest.TestCase):
    def _status(self, tmp: str, verify: bool) -> dict:
        vault = Path(tmp) / "vault"
        exports = vault / export_restore.EXPORTS_DIRNAME
        base = exports / f"{export_restore.EXPORT_PREFIX}20260717T000000Z"
        base.mkdir(parents=True)
        items = [write_file(base, "a.txt", b"hello")]
        write_manifest(base, items)
        return export_restore.get_backup_status(vault, verify=verify)

    def test_status_without_verify_is_not_reported_as_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status(tmp, verify=False)
            self.assertEqual(status.get("trust_level"), "manifest_only")

    def test_status_verify_checks_every_manifest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status(tmp, verify=True)
            self.assertEqual(status.get("trust_level"), "verified")
            self.assertEqual(status["check"]["checked_files"], 1)


if __name__ == "__main__":
    unittest.main()
