"""备份校验 fail-closed 测试：strict 模式下任何异常都不允许返回成功。"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from types import SimpleNamespace
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


class ExportSourceBoundaryTest(unittest.TestCase):
    def test_export_rejects_symlinked_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vault = base / "vault"
            vault.mkdir()
            outside = base / "outside.jsonl"
            outside.write_text("{}\n", encoding="utf-8")
            (vault / "index.jsonl").symlink_to(outside)

            with self.assertRaises(ValueError):
                export_restore.create_export(vault, base / "exports")

            self.assertFalse(list((base / "exports").glob("immortal-export-*")))


def test_real_backup_always_requests_strict_restore_check(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(export_restore, "IMMORTAL_DIR", tmp_path / "vault")
    import immortal

    monkeypatch.setattr(immortal, "command_export", lambda _args: 0)
    monkeypatch.setattr(
        immortal,
        "read_json",
        lambda _path, _default: {"last_portable_export_dir": str(tmp_path / "export")},
    )

    def capture(args):
        captured["strict"] = args.strict
        return 0

    monkeypatch.setattr(immortal, "command_restore_check", capture)

    result = immortal.command_backup(SimpleNamespace(redact_secrets=False))

    assert result == 0
    assert captured["strict"] is True


def test_export_restore_includes_v11_event_layers(tmp_path, monkeypatch):
    vault = tmp_path / "source"
    vault.mkdir()
    (vault / "index.jsonl").write_bytes(b"")
    _ = export_restore.run_v11_migration(vault)
    for relative in export_restore.REQUIRED_PATHS:
        path = vault / relative
        if path.exists():
            continue
        if Path(relative).suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / ".keep").write_text("kept\n", encoding="utf-8")
    monkeypatch.setattr(
        export_restore,
        "classify_storage_location",
        lambda _target, _vault: "external_disk",
    )
    manifest = export_restore.create_export(vault, tmp_path / "exports")

    assert manifest["schema_versions"] == {"model": 1, "events": 1}
    assert manifest["event_heads"] == {
        "claims": 0,
        "judgments": 0,
        "contexts": 0,
        "outcomes": 0,
    }
    restored = tmp_path / "restored"
    export_dir = Path(manifest["export_dir"])
    before_paths = sorted(path.relative_to(export_dir) for path in export_dir.rglob("*") if path.is_file())
    restore = export_restore.restore_export(manifest["export_dir"], restored)
    check = export_restore.restore_check(manifest["export_dir"], strict=True)
    after_paths = sorted(path.relative_to(export_dir) for path in export_dir.rglob("*") if path.is_file())
    assert restore["ok"] is True
    assert check["ok"] is True
    assert before_paths == after_paths
    assert (restored / "model/claims/events.jsonl").read_bytes() == (
        vault / "model/claims/events.jsonl"
    ).read_bytes()


def test_v11_restore_rejects_projection_that_does_not_replay_from_events(tmp_path):
    vault = tmp_path / "source"
    vault.mkdir()
    (vault / "index.jsonl").write_bytes(b"")
    _ = export_restore.run_v11_migration(vault)
    (vault / "model/claims/current.jsonl").write_text("{\"tampered\":true}\n", encoding="utf-8")
    manifest = export_restore.create_export(vault, tmp_path / "exports")

    result = export_restore.restore_check(manifest["export_dir"], strict=True)

    assert result["ok"] is False
    assert "v11_projection_mismatch: claims" in result["warnings"]


def test_restore_failure_removes_anchored_temporary_tree(tmp_path, monkeypatch):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    item = write_file(export_dir, "proof.txt", b"proof")
    write_manifest(export_dir, [item])
    original = export_restore._copy_file_into_tree
    calls = []

    def fail_on_manifest(source, root_fd, relative):
        calls.append(relative.as_posix())
        if relative.name == export_restore.MANIFEST_NAME:
            raise OSError("injected copy failure")
        return original(source, root_fd, relative)

    monkeypatch.setattr(export_restore, "_copy_file_into_tree", fail_on_manifest)
    target = tmp_path / "restored"

    result = export_restore.restore_export(export_dir, target)

    assert result["ok"] is False
    assert not target.exists()
    assert not list(tmp_path.glob(".restored.restore-*"))


def test_restore_can_rebind_vault_config_after_strict_copy_verification(tmp_path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    config = write_file(
        export_dir,
        "config.json",
        json.dumps({"vault_dir": "/old/live/vault", "keep": True}).encode(),
    )
    write_manifest(export_dir, [config])
    target = tmp_path / "restored"

    result = export_restore.restore_export(
        export_dir,
        target,
        rebind_vault_config=True,
    )

    assert result["ok"] is True
    assert result["config_rebound"] is True
    assert result["source_check"]["ok"] is True
    assert result["check"]["ok"] is True
    assert result["derived_generation"]["config_sha256"] == hashlib.sha256(
        (target / "config.json").read_bytes()
    ).hexdigest()
    assert result["derived_generation"]["receipt_sha256"] == hashlib.sha256(
        (target / "restore-config-rebind-receipt.json").read_bytes()
    ).hexdigest()
    assert json.loads((target / "config.json").read_text()) == {
        "vault_dir": str(target.absolute()),
        "keep": True,
    }
    assert json.loads((export_dir / "config.json").read_text())["vault_dir"] == (
        "/old/live/vault"
    )
    assert (target / "config.json").stat().st_mode & 0o777 == 0o600
    assert export_restore.restore_check(target, strict=True)["ok"] is True
    derived_manifest = json.loads((target / export_restore.MANIFEST_NAME).read_text())
    assert derived_manifest["derived_from"]["manifest_sha256"] == hashlib.sha256(
        (export_dir / export_restore.MANIFEST_NAME).read_bytes()
    ).hexdigest()


def test_restore_rejects_export_replaced_after_initial_check(tmp_path, monkeypatch):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    original_item = write_file(export_dir, "proof.txt", b"original")
    write_manifest(export_dir, [original_item])
    original_check = export_restore.restore_check
    calls = 0

    def replace_after_check(path, strict=False):
        nonlocal calls
        result = original_check(path, strict=strict)
        calls += 1
        if calls == 1:
            replacement_item = write_file(export_dir, "proof.txt", b"replacement")
            write_manifest(export_dir, [replacement_item])
        return result

    monkeypatch.setattr(export_restore, "restore_check", replace_after_check)
    target = tmp_path / "restored"

    result = export_restore.restore_export(export_dir, target)

    assert result["ok"] is False
    assert result["blockers"] == ["export_generation_changed"]
    assert not target.exists()


def test_large_export_generation_hashes_without_retaining_file_body(tmp_path):
    source = tmp_path / "large.bin"
    content = b"x" * (2 * 1024 * 1024 + 17)
    source.write_bytes(content)

    generation = export_restore._read_file_generation(source)

    assert generation is not None
    body, sha256, identity = generation
    assert body is None
    assert sha256 == hashlib.sha256(content).hexdigest()
    assert identity[2] == len(content)


if __name__ == "__main__":
    unittest.main()
