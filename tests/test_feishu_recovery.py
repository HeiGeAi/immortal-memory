from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import tarfile

import pytest

import feishu_recovery

from feishu_recovery import (
    LarkDriveClient,
    OpenPGPCryptor,
    RecoveryError,
    build_encrypted_package,
    build_package_manifest,
    inspect_source_export,
    require_fingerprint,
    run_recovery_drill,
    restore_local_package,
    upload_package,
    validate_drill_receipt,
    validate_package_manifest,
    verify_local_package,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_redacted_export(
    export_dir: Path,
    *,
    source_sha256: str = "a" * 64,
    secret_count: int = 2,
) -> dict:
    export_dir.mkdir(parents=True)
    index = export_dir / "index.jsonl"
    index.write_text(
        '{"id":"one","content":"[REDACTED_SECRET]"}\n',
        encoding="utf-8",
    )
    config = export_dir / "config.json"
    config.write_text(
        json.dumps({"vault_dir": "/private/live/vault", "owner": "test"}),
        encoding="utf-8",
    )
    receipt = export_dir / "secret-redaction-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "mode": "jsonl-value-redaction-v1",
                "source_file": "index.jsonl",
                "export_file": "index.jsonl",
                "source_sha256": source_sha256,
                "export_sha256": _sha256(index),
                "source_bytes": 1024,
                "export_bytes": index.stat().st_size,
                "unique_candidates": secret_count,
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "generated_at": "2026-07-22T00:00:00Z",
        "vault_dir": "/private/live/vault",
        "export_dir": str(export_dir),
        "secret_scan": {"unique_candidates": 0, "scan_complete": True},
        "secret_redaction": {"receipt": "secret-redaction-receipt.json"},
        "warnings": [],
        "items": [
            {
                "relpath": "index.jsonl",
                "size": index.stat().st_size,
                "sha256": _sha256(index),
            },
            {
                "relpath": "config.json",
                "size": config.stat().st_size,
                "sha256": _sha256(config),
            },
            {
                "relpath": "secret-redaction-receipt.json",
                "size": receipt.stat().st_size,
                "sha256": _sha256(receipt),
            },
        ],
        "totals": {
            "files": 3,
            "bytes": index.stat().st_size + config.stat().st_size + receipt.stat().st_size,
        },
    }
    (export_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return manifest


def valid_source_descriptor() -> dict:
    return {
        "generated_at": "2026-07-22T00:00:00Z",
        "manifest_sha256": "b" * 64,
        "source_index_sha256": "c" * 64,
        "redacted_index_sha256": "d" * 64,
        "redaction_unique_candidates": 2,
        "files": 3,
        "bytes": 12,
        "content_fidelity": "credential_redacted",
    }


def test_require_fingerprint_accepts_only_full_existing_key_fingerprints():
    assert require_fingerprint("a" * 40) == "A" * 40
    assert require_fingerprint("b" * 64) == "B" * 64

    for value in ("", "alice@example.com", "a" * 39, "g" * 40):
        with pytest.raises(RecoveryError, match="recipient_fingerprint_invalid"):
            require_fingerprint(value)


def test_inspect_source_export_requires_strict_redaction_proof(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)

    inspected = inspect_source_export(export_dir)

    assert inspected == {
        **valid_source_descriptor(),
        "manifest_sha256": _sha256(export_dir / "manifest.json"),
        "source_index_sha256": "a" * 64,
        "redacted_index_sha256": _sha256(export_dir / "index.jsonl"),
        "files": 3,
        "bytes": (export_dir / "index.jsonl").stat().st_size
        + (export_dir / "config.json").stat().st_size
        + (export_dir / "secret-redaction-receipt.json").stat().st_size,
    }
    encoded = json.dumps(inspected, sort_keys=True)
    assert "/private" not in encoded
    assert str(export_dir) not in encoded


def test_inspect_source_export_rejects_unredacted_or_non_strict_export(tmp_path):
    export_dir = tmp_path / "immortal-export-unsafe"
    manifest = write_redacted_export(export_dir)
    manifest["warnings"] = ["secret_shapes_present: 2"]
    manifest["secret_redaction"] = {}
    (export_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    with pytest.raises(RecoveryError, match="source_export_not_strict"):
        inspect_source_export(export_dir)


def test_inspect_source_export_rejects_unbound_redaction_receipt(tmp_path):
    export_dir = tmp_path / "immortal-export-unbound"
    write_redacted_export(export_dir)
    receipt = export_dir / "secret-redaction-receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["export_sha256"] = "f" * 64
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["items"]:
        if item["relpath"] == "secret-redaction-receipt.json":
            item["sha256"] = _sha256(receipt)
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RecoveryError, match="source_export_redaction_binding_invalid"):
        inspect_source_export(export_dir)


def test_package_manifest_has_no_local_path_or_drive_token():
    manifest = build_package_manifest(
        package_id="immortal-recovery-20260722-a1b2c3d4",
        recipient_fingerprint="A" * 40,
        source=valid_source_descriptor(),
        parts=[
            {
                "name": "parts/part-00001.gpg",
                "bytes": 12,
                "sha256": "e" * 64,
            }
        ],
    )

    validate_package_manifest(manifest)
    encoded = json.dumps(manifest, sort_keys=True)
    assert "/private" not in encoded
    assert "folder_token" not in encoded
    assert "A" * 40 not in encoded
    assert manifest["recipient_fingerprint_suffix"] == "AAAAAAAA"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": "value"}),
        lambda payload: payload.update({"schema_version": True}),
        lambda payload: payload["encryption"].update({"part_count": True}),
        lambda payload: payload["parts"].append(
            {
                "name": "parts/part-00003.gpg",
                "bytes": 12,
                "sha256": "f" * 64,
            }
        ),
        lambda payload: payload["parts"][0].update({"name": "../part.gpg"}),
        lambda payload: payload["source"].update({"content_fidelity": "full"}),
    ],
)
def test_validate_package_manifest_fails_closed_for_unsafe_metadata(mutate):
    manifest = build_package_manifest(
        package_id="immortal-recovery-20260722-a1b2c3d4",
        recipient_fingerprint="A" * 40,
        source=valid_source_descriptor(),
        parts=[
            {
                "name": "parts/part-00001.gpg",
                "bytes": 12,
                "sha256": "e" * 64,
            }
        ],
    )
    mutate(manifest)

    with pytest.raises(RecoveryError, match="package_manifest_invalid"):
        validate_package_manifest(manifest)


def test_inspect_source_export_rejects_boolean_secret_scan_count(tmp_path):
    export_dir = tmp_path / "immortal-export-secret-scan-bool"
    manifest = write_redacted_export(export_dir)
    manifest["secret_scan"] = {"unique_candidates": False}
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RecoveryError, match="source_export_redaction_missing"):
        inspect_source_export(export_dir)


def test_inspect_source_export_rejects_incomplete_or_misbound_redaction_metadata(tmp_path):
    export_dir = tmp_path / "immortal-export-incomplete-scan"
    manifest = write_redacted_export(export_dir)
    manifest["secret_scan"]["scan_complete"] = False
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RecoveryError, match="source_export_redaction_missing"):
        inspect_source_export(export_dir)

    manifest["secret_scan"]["scan_complete"] = True
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt_path = export_dir / "secret-redaction-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_file"] = "other.jsonl"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    for item in manifest["items"]:
        if item["relpath"] == "secret-redaction-receipt.json":
            item["sha256"] = _sha256(receipt_path)
            item["size"] = receipt_path.stat().st_size
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RecoveryError, match="source_export_redaction_invalid"):
        inspect_source_export(export_dir)


class CopyCryptor:
    def assert_recipient(self, fingerprint):
        assert fingerprint == "A" * 40

    def encrypt(self, fingerprint, write_plaintext, write_ciphertext):
        plaintext = BytesIO()
        write_plaintext(plaintext)
        write_ciphertext(plaintext.getvalue())


def make_valid_package(tmp_path: Path) -> Path:
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)
    package_dir = tmp_path / "package"
    result = build_encrypted_package(
        export_dir,
        package_dir,
        recipient_fingerprint="A" * 40,
        cryptor=CopyCryptor(),
    )
    assert result["ok"] is True
    return package_dir


def test_build_encrypted_package_splits_and_hashes_all_ciphertext(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)
    package_dir = tmp_path / "package"

    result = build_encrypted_package(
        export_dir,
        package_dir,
        recipient_fingerprint="A" * 40,
        part_bytes=17,
        cryptor=CopyCryptor(),
    )

    assert result["ok"] is True
    assert len(result["parts"]) > 1
    assert verify_local_package(package_dir) == {
        "ok": True,
        "package_id": result["package_id"],
        "parts": len(result["parts"]),
        "bytes": sum(item["bytes"] for item in result["parts"]),
        "blockers": [],
    }
    assert not (package_dir / "export").exists()
    assert (package_dir / "immortal-feishu-recovery.json").stat().st_mode & 0o777 == 0o600
    assert all(
        (package_dir / item["name"]).stat().st_mode & 0o777 == 0o600
        for item in result["parts"]
    )
    manifest_text = (package_dir / "immortal-feishu-recovery.json").read_text(
        encoding="utf-8"
    )
    assert str(export_dir) not in manifest_text
    assert "/private/live/vault" not in manifest_text


def test_build_encrypted_package_cleans_partial_parts_after_cryptor_failure(tmp_path):
    class FailingCryptor(CopyCryptor):
        def encrypt(self, fingerprint, write_plaintext, write_ciphertext):
            write_ciphertext(b"partial")
            raise RuntimeError("injected")

    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)

    result = build_encrypted_package(
        export_dir,
        tmp_path / "package",
        recipient_fingerprint="A" * 40,
        cryptor=FailingCryptor(),
    )

    assert result == {"ok": False, "blockers": ["encryption_failed"]}
    assert not (tmp_path / "package").exists()


def test_verify_local_package_rejects_unlisted_file(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)
    package_dir = tmp_path / "package"
    result = build_encrypted_package(
        export_dir,
        package_dir,
        recipient_fingerprint="A" * 40,
        cryptor=CopyCryptor(),
    )
    assert result["ok"] is True
    (package_dir / "unlisted.txt").write_text("unexpected", encoding="utf-8")

    assert verify_local_package(package_dir) == {
        "ok": False,
        "package_id": "",
        "parts": 0,
        "bytes": 0,
        "blockers": ["package_contents_invalid"],
    }


def test_openpgp_cryptor_requires_exact_primary_public_fingerprint():
    calls = []

    class Completed:
        returncode = 0
        stdout = "pub:u::::::\nfpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:\n"

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Completed()

    cryptor = OpenPGPCryptor(runner=runner)
    cryptor.assert_recipient("a" * 40)

    assert calls == [
        (
            ["gpg", "--batch", "--with-colons", "--list-keys", "A" * 40],
            {"capture_output": True, "text": True, "check": False, "timeout": 30},
        )
    ]


def test_openpgp_cryptor_rejects_a_subkey_or_missing_public_key():
    class Completed:
        returncode = 0
        stdout = "pub:u::::::\nfpr:::::::::BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB:\nsub:u::::::\nfpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:\n"

    cryptor = OpenPGPCryptor(runner=lambda *_args, **_kwargs: Completed())

    with pytest.raises(RecoveryError, match="recipient_public_key_unavailable"):
        cryptor.assert_recipient("A" * 40)


class RecordingDriveRunner:
    def __init__(self):
        self.calls = []
        self.created_folders = 0

    def __call__(self, argv, *, cwd=None):
        self.calls.append((argv, cwd))
        if "+create-folder" in argv:
            self.created_folders += 1
            token = (
                "package_new_12345678"
                if self.created_folders == 1
                else "parts_new_12345678"
            )
            return {"data": {"token": token}}
        if "+status" in argv:
            return {"ok": True, "detection": "exact"}
        return {"data": {"file_token": "file_12345678"}}


def test_upload_requires_explicit_remote_write_confirmation(tmp_path):
    package = make_valid_package(tmp_path)
    runner = RecordingDriveRunner()

    result = upload_package(
        package,
        "parent_12345678",
        vault_dir=tmp_path / "vault",
        client=LarkDriveClient(runner),
        confirm_remote_write=False,
    )

    assert result == {
        "ok": False,
        "package_id": "",
        "receipt": {},
        "blockers": ["remote_write_confirmation_required"],
    }
    assert runner.calls == []


def test_upload_creates_dedicated_folder_uploads_manifest_last_and_runs_exact_status(tmp_path):
    package = make_valid_package(tmp_path)
    runner = RecordingDriveRunner()
    (tmp_path / "vault").mkdir()

    result = upload_package(
        package,
        "parent_12345678",
        vault_dir=tmp_path / "vault",
        client=LarkDriveClient(runner),
        confirm_remote_write=True,
    )

    upload_calls = [argv for argv, _cwd in runner.calls if "+upload" in argv]
    create_calls = [argv for argv, _cwd in runner.calls if "+create-folder" in argv]
    status_calls = [argv for argv, _cwd in runner.calls if "+status" in argv]
    assert result["ok"] is True
    assert len(create_calls) == 2
    assert len(status_calls) == 2
    assert all(
        "/" not in argv[argv.index("--name") + 1]
        for argv in upload_calls
    )
    assert upload_calls[-1][upload_calls[-1].index("--name") + 1] == "immortal-feishu-recovery.json"
    assert "parent_12345678" not in json.dumps(result["receipt"])
    assert "package_new_12345678" not in json.dumps(result["receipt"])
    assert "parts_new_12345678" not in json.dumps(result["receipt"])
    assert "file_12345678" not in json.dumps(result["receipt"])
    persisted = json.loads(
        (tmp_path / "vault" / "recovery" / "feishu" / "receipts" / f"{result['package_id']}.upload.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["remote_package_folder_token"] == "package_new_12345678"
    assert persisted["remote_parts_folder_token"] == "parts_new_12345678"
    assert [item["file_token"] for item in persisted["remote_files"]]
    assert (tmp_path / "vault" / "recovery" / "feishu" / "latest-upload.json").is_file()


@pytest.mark.parametrize(
    "response, expected_blocker",
    [
        ({"data": {}}, "remote_folder_response_invalid"),
        ({"ok": False, "detection": "quick"}, "remote_verification_failed"),
    ],
)
def test_upload_fails_closed_for_invalid_remote_evidence(tmp_path, response, expected_blocker):
    package = make_valid_package(tmp_path)

    class Runner(RecordingDriveRunner):
        def __call__(self, argv, *, cwd=None):
            if "+create-folder" in argv and expected_blocker == "remote_folder_response_invalid":
                self.calls.append((argv, cwd))
                return response
            if "+status" in argv and expected_blocker == "remote_verification_failed":
                self.calls.append((argv, cwd))
                return response
            return super().__call__(argv, cwd=cwd)

    result = upload_package(
        package,
        "parent_12345678",
        vault_dir=tmp_path / "vault",
        client=LarkDriveClient(Runner()),
        confirm_remote_write=True,
    )

    assert result["ok"] is False
    assert result["blockers"] == [expected_blocker]
    assert not list((tmp_path / "vault").rglob("*.upload.json"))


@pytest.mark.parametrize(
    "mode, expected_blocker",
    [
        ("missing_file_token", "remote_file_response_invalid"),
        ("part_upload_raises", "drive_command_failed"),
    ],
)
def test_upload_fails_closed_for_missing_or_failed_part_upload(tmp_path, mode, expected_blocker):
    package = make_valid_package(tmp_path)
    (tmp_path / "vault").mkdir()

    class Runner(RecordingDriveRunner):
        def __call__(self, argv, *, cwd=None):
            if "+upload" in argv and mode == "missing_file_token":
                self.calls.append((argv, cwd))
                return {"data": {}}
            if "+upload" in argv and mode == "part_upload_raises":
                raise RuntimeError("network unavailable")
            return super().__call__(argv, cwd=cwd)

    result = upload_package(
        package,
        "parent_12345678",
        vault_dir=tmp_path / "vault",
        client=LarkDriveClient(Runner()),
        confirm_remote_write=True,
    )

    assert result["ok"] is False
    assert result["blockers"] == [expected_blocker]
    assert not list((tmp_path / "vault").rglob("*.upload.json"))


def test_upload_removes_new_receipt_when_private_receipt_write_fails(tmp_path, monkeypatch):
    package = make_valid_package(tmp_path)
    (tmp_path / "vault").mkdir()
    calls = 0

    def fail_after_first_write(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        return original(path, payload)

    original = feishu_recovery._write_private_json
    monkeypatch.setattr(feishu_recovery, "_write_private_json", fail_after_first_write)

    result = upload_package(
        package,
        "parent_12345678",
        vault_dir=tmp_path / "vault",
        client=LarkDriveClient(RecordingDriveRunner()),
        confirm_remote_write=True,
    )

    assert result["ok"] is False
    assert result["blockers"] == ["receipt_write_failed"]
    assert not list((tmp_path / "vault").rglob("*.upload.json"))


class CopyCryptorWithDecrypt(CopyCryptor):
    def decrypt(self, part_paths, consume_plaintext):
        consume_plaintext(BytesIO(b"".join(path.read_bytes() for path in part_paths)))


def make_remote_receipt(package: Path) -> dict:
    manifest = json.loads(
        (package / "immortal-feishu-recovery.json").read_text(encoding="utf-8")
    )
    remote_files = []
    for index, part in enumerate(manifest["parts"], start=1):
        remote_files.append(
            {
                "package_name": part["name"],
                "remote_name": Path(part["name"]).name,
                "bytes": part["bytes"],
                "sha256": part["sha256"],
                "file_token": f"filepart{index:08d}",
            }
        )
    manifest_path = package / "immortal-feishu-recovery.json"
    remote_files.append(
        {
            "package_name": "immortal-feishu-recovery.json",
            "remote_name": "immortal-feishu-recovery.json",
            "bytes": manifest_path.stat().st_size,
            "sha256": _sha256(manifest_path),
            "file_token": "filemanifest0001",
        }
    )
    return {
        "schema_version": 1,
        "provider": "feishu_drive",
        "package_id": manifest["package_id"],
        "uploaded_at": "2026-07-22T00:00:01Z",
        "package_manifest_sha256": _sha256(manifest_path),
        "source": manifest["source"],
        "remote_package_folder_token": "package_new_12345678",
        "remote_parts_folder_token": "parts_new_12345678",
        "remote_folder_hash": feishu_recovery._remote_folder_hash(
            "package_new_12345678"
        ),
        "remote_files": remote_files,
        "remote_verification": {"ok": True, "mode": "exact", "checks": 2},
    }


class DownloadingClient:
    def __init__(self, package: Path, receipt: dict, *, corrupt_name: str = ""):
        self._sources = {
            item["file_token"]: (item["package_name"], package / item["package_name"])
            for item in receipt["remote_files"]
        }
        self.corrupt_name = corrupt_name
        self.downloads = []

    def download(self, file_token, output):
        package_name, source = self._sources[file_token]
        self.downloads.append(package_name)
        payload = source.read_bytes()
        if package_name == self.corrupt_name:
            payload += b"tampered"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)


def test_recovery_drill_downloads_all_parts_and_runs_real_restore(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)
    package = tmp_path / "package"
    built = build_encrypted_package(
        export_dir,
        package,
        recipient_fingerprint="A" * 40,
        cryptor=CopyCryptorWithDecrypt(),
    )
    assert built["ok"] is True
    receipt = make_remote_receipt(package)
    client = DownloadingClient(package, receipt)
    vault = tmp_path / "vault"
    vault.mkdir()

    result = run_recovery_drill(
        receipt,
        tmp_path / "drill",
        vault_dir=vault,
        client=client,
        cryptor=CopyCryptorWithDecrypt(),
    )

    assert result["ok"] is True, result
    assert result["verification_mode"] == "remote-download-sha256+decrypt-restore"
    assert result["source_index_sha256"] == "a" * 64
    assert set(client.downloads) == {item["package_name"] for item in receipt["remote_files"]}
    assert not (tmp_path / "drill").exists()
    persisted = json.loads(
        (vault / "recovery" / "feishu" / "receipts" / f"{result['package_id']}.drill.json").read_text(
            encoding="utf-8"
        )
    )
    assert "file_token" not in json.dumps(persisted)
    assert "path" not in json.dumps(persisted)
    assert persisted["recovery_drill"] == {"ok": True, "mode": "decrypt-restore"}
    assert persisted["content_fidelity"] == "credential_redacted"
    assert persisted["redaction_unique_candidates"] == 2
    assert validate_drill_receipt(persisted) == persisted


def test_recovery_drill_receipt_rejects_unexpected_or_unbound_fields(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)
    package = tmp_path / "package"
    built = build_encrypted_package(
        export_dir,
        package,
        recipient_fingerprint="A" * 40,
        cryptor=CopyCryptorWithDecrypt(),
    )
    assert built["ok"] is True
    receipt = make_remote_receipt(package)
    vault = tmp_path / "vault"
    vault.mkdir()

    result = run_recovery_drill(
        receipt,
        tmp_path / "drill",
        vault_dir=vault,
        client=DownloadingClient(package, receipt),
        cryptor=CopyCryptorWithDecrypt(),
    )
    assert result["ok"] is True, result
    persisted = json.loads(
        (vault / "recovery" / "feishu" / "latest-drill.json").read_text(
            encoding="utf-8"
        )
    )
    persisted["source_index_sha256"] = "not-a-sha256"

    with pytest.raises(RecoveryError, match="recovery_drill_receipt_invalid"):
        validate_drill_receipt(persisted)


def test_recovery_drill_fails_when_downloaded_part_hash_changes(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)
    package = tmp_path / "package"
    built = build_encrypted_package(
        export_dir,
        package,
        recipient_fingerprint="A" * 40,
        cryptor=CopyCryptorWithDecrypt(),
    )
    assert built["ok"] is True
    receipt = make_remote_receipt(package)
    corrupt_name = receipt["remote_files"][0]["package_name"]
    vault = tmp_path / "vault"
    vault.mkdir()

    result = run_recovery_drill(
        receipt,
        tmp_path / "drill",
        vault_dir=vault,
        client=DownloadingClient(package, receipt, corrupt_name=corrupt_name),
        cryptor=CopyCryptorWithDecrypt(),
    )

    assert result == {
        "ok": False,
        "package_id": receipt["package_id"],
        "blockers": ["remote_part_hash_mismatch"],
    }
    assert not (tmp_path / "drill").exists()
    assert not list(vault.rglob("*.drill.json"))


def test_restore_local_package_rebinds_a_real_new_vault_and_removes_plaintext_stage(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)
    package = tmp_path / "package"
    built = build_encrypted_package(
        export_dir,
        package,
        recipient_fingerprint="A" * 40,
        cryptor=CopyCryptorWithDecrypt(),
    )
    assert built["ok"] is True
    destination = tmp_path / "recovered-vault"

    result = restore_local_package(
        package,
        destination,
        cryptor=CopyCryptorWithDecrypt(),
    )

    assert result["ok"] is True, result
    assert json.loads((destination / "config.json").read_text(encoding="utf-8"))["vault_dir"] == str(
        destination
    )
    assert not list(tmp_path.glob(".immortal-feishu-recovery-*"))


def test_restore_local_package_rejects_a_decrypted_tar_symlink(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_redacted_export(export_dir)
    package = tmp_path / "package"
    built = build_encrypted_package(
        export_dir,
        package,
        recipient_fingerprint="A" * 40,
        cryptor=CopyCryptorWithDecrypt(),
    )
    assert built["ok"] is True

    class MaliciousCryptor(CopyCryptorWithDecrypt):
        def decrypt(self, part_paths, consume_plaintext):
            payload = BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                manifest = b'{"items":[],"totals":{"files":0,"bytes":0},"warnings":[]}'
                info = tarfile.TarInfo("export/manifest.json")
                info.size = len(manifest)
                archive.addfile(info, BytesIO(manifest))
                link = tarfile.TarInfo("export/escape")
                link.type = tarfile.SYMTYPE
                link.linkname = "/private/escape"
                archive.addfile(link)
            consume_plaintext(BytesIO(payload.getvalue()))

    destination = tmp_path / "recovered-vault"
    result = restore_local_package(package, destination, cryptor=MaliciousCryptor())

    assert result == {
        "ok": False,
        "package_id": built["package_id"],
        "blockers": ["recovery_archive_invalid"],
    }
    assert not destination.exists()


def test_recovery_cli_prepares_only_explicit_local_package(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(
        feishu_recovery,
        "build_encrypted_package",
        lambda export_dir, package_dir, **kwargs: calls.append(
            (export_dir, package_dir, kwargs)
        )
        or {"ok": True, "package_id": "immortal-recovery-20260722-a1b2c3d4"},
    )

    code = feishu_recovery.main(
        [
            "prepare",
            "--export-dir",
            str(tmp_path / "export"),
            "--package-dir",
            str(tmp_path / "package"),
            "--recipient",
            "A" * 40,
            "--part-bytes",
            str(1024 * 1024),
        ]
    )

    assert code == 0
    assert calls == [
        (
            str(tmp_path / "export"),
            str(tmp_path / "package"),
            {"recipient_fingerprint": "A" * 40, "part_bytes": 1024 * 1024},
        )
    ]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_recovery_cli_dry_run_never_calls_remote_upload(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(
        feishu_recovery,
        "verify_local_package",
        lambda _package: {
            "ok": True,
            "package_id": "immortal-recovery-20260722-a1b2c3d4",
            "parts": 1,
            "bytes": 123,
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        feishu_recovery,
        "upload_package",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    code = feishu_recovery.main(
        [
            "upload",
            "--package-dir",
            str(tmp_path / "package"),
            "--parent-folder-token",
            "parent_12345678",
            "--dry-run",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert calls == []
    assert payload == {
        "ok": True,
        "package_id": "immortal-recovery-20260722-a1b2c3d4",
        "would_write": True,
        "blockers": [],
    }


def test_recovery_cli_rejects_small_human_part_size(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        feishu_recovery,
        "build_encrypted_package",
        lambda *args, **kwargs: pytest.fail("invalid part size must not build a package"),
    )

    code = feishu_recovery.main(
        [
            "prepare",
            "--export-dir",
            str(tmp_path / "export"),
            "--package-dir",
            str(tmp_path / "package"),
            "--recipient",
            "A" * 40,
            "--part-bytes",
            "1024",
        ]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "blockers": ["part_size_too_small"],
    }
