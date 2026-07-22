from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path

import pytest

from feishu_recovery import (
    OpenPGPCryptor,
    RecoveryError,
    build_encrypted_package,
    build_package_manifest,
    inspect_source_export,
    require_fingerprint,
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
                "relpath": "secret-redaction-receipt.json",
                "size": receipt.stat().st_size,
                "sha256": _sha256(receipt),
            },
        ],
        "totals": {
            "files": 2,
            "bytes": index.stat().st_size + receipt.stat().st_size,
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
        "files": 2,
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
        "files": 2,
        "bytes": (export_dir / "index.jsonl").stat().st_size
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
