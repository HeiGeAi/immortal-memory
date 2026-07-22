#!/usr/bin/env python3
"""Safe, opt-in Feishu Drive disaster-recovery package primitives.

This module deliberately does not reuse the read-only Feishu Drive mirror.
It begins with local source-export and metadata validation. Remote upload,
download, and recovery-drill operations are added only after this layer can
prove that a redacted portable export is internally consistent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import export_restore


PACKAGE_SCHEMA_VERSION = 1
PACKAGE_MANIFEST_NAME = "immortal-feishu-recovery.json"
PARTS_DIRNAME = "parts"
MAX_JSON_BYTES = 8 * 1024 * 1024
FINGERPRINT_RE = re.compile(r"\A(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})\Z")
PACKAGE_ID_RE = re.compile(r"\Aimmortal-recovery-\d{8}-[a-f0-9]{8}\Z")
PART_NAME_RE = re.compile(r"\Aparts/part-(\d{5})\.gpg\Z")
SHA256_RE = re.compile(r"\A[a-f0-9]{64}\Z")

SOURCE_DESCRIPTOR_KEYS = {
    "generated_at",
    "manifest_sha256",
    "source_index_sha256",
    "redacted_index_sha256",
    "redaction_unique_candidates",
    "files",
    "bytes",
    "content_fidelity",
}
PACKAGE_MANIFEST_KEYS = {
    "schema_version",
    "package_id",
    "generated_at",
    "recipient_fingerprint_suffix",
    "encryption",
    "source",
    "parts",
}
PART_KEYS = {"name", "bytes", "sha256"}
ENCRYPTION_KEYS = {"algorithm", "part_count"}


class RecoveryError(ValueError):
    """A stable, content-free recovery boundary failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_regular_bytes(path: str | Path, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    """Read one stable regular file without following its final symlink."""
    candidate = Path(path).expanduser().absolute()
    descriptor = -1
    try:
        before_lstat = os.lstat(candidate)
        if not stat.S_ISREG(before_lstat.st_mode) or stat.S_ISLNK(before_lstat.st_mode):
            raise RecoveryError("private_metadata_unsafe")
        descriptor = os.open(
            str(candidate),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise RecoveryError("private_metadata_unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if remaining <= 0 or identity_before != identity_after:
            raise RecoveryError("private_metadata_unsafe")
        return b"".join(chunks)
    except RecoveryError:
        raise
    except OSError as exc:
        raise RecoveryError("private_metadata_unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_json_object(path: str | Path) -> tuple[dict[str, Any], str, int]:
    raw = _read_regular_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("private_metadata_invalid") from exc
    if not isinstance(value, dict):
        raise RecoveryError("private_metadata_invalid")
    return value, _sha256_bytes(raw), len(raw)


def _is_nonnegative_int(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and value >= (1 if positive else 0)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def require_fingerprint(value: str) -> str:
    """Require a full existing public-key fingerprint, never an email or key body."""
    candidate = str(value or "").strip().upper()
    if FINGERPRINT_RE.fullmatch(candidate) is None:
        raise RecoveryError("recipient_fingerprint_invalid")
    return candidate


def _manifest_items_by_path(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    items = manifest.get("items")
    if not isinstance(items, list):
        raise RecoveryError("source_export_manifest_invalid")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise RecoveryError("source_export_manifest_invalid")
        relative = item.get("relpath")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or relative in result
        ):
            raise RecoveryError("source_export_manifest_invalid")
        result[relative] = item
    return result


def inspect_source_export(export_dir: str | Path) -> dict[str, Any]:
    """Return only cloud-safe proof about a strict credential-redacted export."""
    export_path = Path(export_dir).expanduser().absolute()
    check = export_restore.restore_check(export_path, strict=True)
    if not check.get("ok"):
        raise RecoveryError("source_export_not_strict")
    manifest, manifest_sha256, _manifest_bytes = _read_json_object(
        export_path / export_restore.MANIFEST_NAME
    )
    items = _manifest_items_by_path(manifest)
    redaction = manifest.get("secret_redaction")
    secret_scan = manifest.get("secret_scan")
    if (
        not isinstance(redaction, dict)
        or redaction.get("receipt") != export_restore.SECRET_REDACTION_RECEIPT
        or not isinstance(secret_scan, dict)
        or type(secret_scan.get("unique_candidates")) is not int
        or secret_scan.get("unique_candidates") != 0
    ):
        raise RecoveryError("source_export_redaction_missing")
    index_item = items.get("index.jsonl")
    receipt_item = items.get(export_restore.SECRET_REDACTION_RECEIPT)
    if not isinstance(index_item, dict) or not isinstance(receipt_item, dict):
        raise RecoveryError("source_export_redaction_missing")

    receipt_path = export_path / export_restore.SECRET_REDACTION_RECEIPT
    receipt, receipt_sha256, receipt_bytes = _read_json_object(receipt_path)
    source_index_sha256 = receipt.get("source_sha256")
    redacted_index_sha256 = receipt.get("export_sha256")
    redaction_count = receipt.get("unique_candidates")
    if (
        receipt.get("mode") != "jsonl-value-redaction-v1"
        or not _is_sha256(source_index_sha256)
        or not _is_sha256(redacted_index_sha256)
        or not _is_nonnegative_int(redaction_count)
    ):
        raise RecoveryError("source_export_redaction_invalid")
    if (
        index_item.get("sha256") != redacted_index_sha256
        or receipt_item.get("sha256") != receipt_sha256
        or index_item.get("size") != (export_path / "index.jsonl").stat().st_size
        or receipt_item.get("size") != receipt_bytes
    ):
        raise RecoveryError("source_export_redaction_binding_invalid")

    totals = manifest.get("totals")
    generated_at = manifest.get("generated_at")
    if (
        not isinstance(totals, dict)
        or not _is_nonnegative_int(totals.get("files"), positive=True)
        or not _is_nonnegative_int(totals.get("bytes"))
        or totals.get("files") != len(items)
        or not _is_utc_timestamp(generated_at)
    ):
        raise RecoveryError("source_export_manifest_invalid")
    return {
        "generated_at": generated_at,
        "manifest_sha256": manifest_sha256,
        "source_index_sha256": source_index_sha256,
        "redacted_index_sha256": redacted_index_sha256,
        "redaction_unique_candidates": redaction_count,
        "files": totals["files"],
        "bytes": totals["bytes"],
        "content_fidelity": "credential_redacted",
    }


def _validate_source_descriptor(source: Any) -> dict[str, Any]:
    if not isinstance(source, Mapping) or set(source) != SOURCE_DESCRIPTOR_KEYS:
        raise RecoveryError("package_manifest_invalid")
    descriptor = dict(source)
    if (
        not _is_utc_timestamp(descriptor.get("generated_at"))
        or not all(
            _is_sha256(descriptor.get(key))
            for key in (
                "manifest_sha256",
                "source_index_sha256",
                "redacted_index_sha256",
            )
        )
        or not _is_nonnegative_int(descriptor.get("redaction_unique_candidates"))
        or not _is_nonnegative_int(descriptor.get("files"), positive=True)
        or not _is_nonnegative_int(descriptor.get("bytes"))
        or descriptor.get("content_fidelity") != "credential_redacted"
    ):
        raise RecoveryError("package_manifest_invalid")
    return descriptor


def _validate_parts(parts: Any) -> list[dict[str, Any]]:
    if not isinstance(parts, list) or not parts:
        raise RecoveryError("package_manifest_invalid")
    validated: list[dict[str, Any]] = []
    for expected, item in enumerate(parts, start=1):
        if not isinstance(item, Mapping) or set(item) != PART_KEYS:
            raise RecoveryError("package_manifest_invalid")
        candidate = dict(item)
        match = PART_NAME_RE.fullmatch(str(candidate.get("name") or ""))
        if (
            match is None
            or int(match.group(1)) != expected
            or not _is_nonnegative_int(candidate.get("bytes"), positive=True)
            or not _is_sha256(candidate.get("sha256"))
        ):
            raise RecoveryError("package_manifest_invalid")
        validated.append(candidate)
    return validated


def _validate_package_id(package_id: Any) -> str:
    candidate = str(package_id or "")
    if PACKAGE_ID_RE.fullmatch(candidate) is None:
        raise RecoveryError("package_manifest_invalid")
    return candidate


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def build_package_manifest(
    *,
    package_id: str,
    recipient_fingerprint: str,
    source: Mapping[str, Any],
    parts: Sequence[Mapping[str, Any]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Create a plaintext manifest containing only non-sensitive recovery proof."""
    fingerprint = require_fingerprint(recipient_fingerprint)
    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": _validate_package_id(package_id),
        "generated_at": generated_at or _now_utc(),
        "recipient_fingerprint_suffix": fingerprint[-8:],
        "encryption": {"algorithm": "openpgp", "part_count": len(parts)},
        "source": _validate_source_descriptor(source),
        "parts": _validate_parts(list(parts)),
    }
    validate_package_manifest(manifest)
    return manifest


def validate_package_manifest(manifest: Any) -> None:
    """Fail closed unless a package manifest is safe for unencrypted Drive metadata."""
    try:
        if not isinstance(manifest, Mapping) or set(manifest) != PACKAGE_MANIFEST_KEYS:
            raise RecoveryError("package_manifest_invalid")
        if (
            type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION
        ):
            raise RecoveryError("package_manifest_invalid")
        _validate_package_id(manifest.get("package_id"))
        if not _is_utc_timestamp(manifest.get("generated_at")):
            raise RecoveryError("package_manifest_invalid")
        suffix = manifest.get("recipient_fingerprint_suffix")
        if not isinstance(suffix, str) or re.fullmatch(r"[A-F0-9]{8}", suffix) is None:
            raise RecoveryError("package_manifest_invalid")
        encryption = manifest.get("encryption")
        if (
            not isinstance(encryption, Mapping)
            or set(encryption) != ENCRYPTION_KEYS
            or encryption.get("algorithm") != "openpgp"
        ):
            raise RecoveryError("package_manifest_invalid")
        parts = _validate_parts(manifest.get("parts"))
        if (
            type(encryption.get("part_count")) is not int
            or encryption.get("part_count") != len(parts)
        ):
            raise RecoveryError("package_manifest_invalid")
        _validate_source_descriptor(manifest.get("source"))
    except RecoveryError:
        raise
    except (TypeError, ValueError):
        raise RecoveryError("package_manifest_invalid")
