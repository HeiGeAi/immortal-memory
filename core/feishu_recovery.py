#!/usr/bin/env python3
"""Safe, opt-in Feishu Drive disaster-recovery package primitives.

This module deliberately does not reuse the read-only Feishu Drive mirror.
It begins with local source-export and metadata validation. Remote upload,
download, and recovery-drill operations are added only after this layer can
prove that a redacted portable export is internally consistent.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence

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
        or secret_scan.get("scan_complete") is not True
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
        or receipt.get("source_file") != "index.jsonl"
        or receipt.get("export_file") != "index.jsonl"
        or not _is_sha256(source_index_sha256)
        or not _is_sha256(redacted_index_sha256)
        or not _is_nonnegative_int(redaction_count)
        or not _is_nonnegative_int(receipt.get("source_bytes"))
        or not _is_nonnegative_int(receipt.get("export_bytes"))
    ):
        raise RecoveryError("source_export_redaction_invalid")
    if (
        index_item.get("sha256") != redacted_index_sha256
        or receipt_item.get("sha256") != receipt_sha256
        or index_item.get("size") != (export_path / "index.jsonl").stat().st_size
        or receipt.get("export_bytes") != index_item.get("size")
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


def _secure_file_size_sha256(path: str | Path) -> tuple[int, str]:
    """Hash a stable regular file without loading it into memory."""
    candidate = Path(path).expanduser().absolute()
    descriptor = -1
    try:
        initial = os.lstat(candidate)
        if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
            raise RecoveryError("package_contents_invalid")
        descriptor = os.open(
            str(candidate),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryError("package_contents_invalid")
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
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
        if identity_before != identity_after or total != before.st_size:
            raise RecoveryError("package_contents_invalid")
        return total, digest.hexdigest()
    except RecoveryError:
        raise
    except OSError as exc:
        raise RecoveryError("package_contents_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class SplitDigestWriter:
    """Write ciphertext into sequential parts and bind each part by SHA-256."""

    def __init__(self, parts_dir: str | Path, part_bytes: int) -> None:
        if not _is_nonnegative_int(part_bytes, positive=True):
            raise RecoveryError("part_size_invalid")
        self.parts_dir = Path(parts_dir).expanduser().absolute()
        self.part_bytes = part_bytes
        self._part_number = 0
        self._current: BinaryIO | None = None
        self._current_path: Path | None = None
        self._current_bytes = 0
        self._current_digest: Any = None
        self._parts: list[dict[str, Any]] = []

    def _open_part(self) -> None:
        self._part_number += 1
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.parts_dir, 0o700)
        path = self.parts_dir / f"part-{self._part_number:05d}.gpg"
        descriptor = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        self._current = os.fdopen(descriptor, "wb")
        self._current_path = path
        self._current_bytes = 0
        self._current_digest = hashlib.sha256()

    def _finish_part(self) -> None:
        if self._current is None or self._current_path is None:
            return
        self._current.flush()
        os.fsync(self._current.fileno())
        self._current.close()
        self._parts.append(
            {
                "name": f"{PARTS_DIRNAME}/{self._current_path.name}",
                "bytes": self._current_bytes,
                "sha256": self._current_digest.hexdigest(),
            }
        )
        self._current = None
        self._current_path = None
        self._current_digest = None

    def write(self, data: bytes) -> int:
        remaining = memoryview(data)
        while remaining:
            if self._current is None:
                self._open_part()
            room = self.part_bytes - self._current_bytes
            chunk = remaining[:room]
            self._current.write(chunk)
            self._current_digest.update(chunk)
            self._current_bytes += len(chunk)
            remaining = remaining[len(chunk) :]
            if self._current_bytes == self.part_bytes:
                self._finish_part()
        return len(data)

    def close(self) -> list[dict[str, Any]]:
        self._finish_part()
        return list(self._parts)

    def abort(self) -> None:
        if self._current is not None:
            self._current.close()
        self._current = None
        self._current_path = None
        self._current_digest = None


class _HashingReader:
    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self.digest = hashlib.sha256()
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        value = self.handle.read(size)
        self.digest.update(value)
        self.total += len(value)
        return value


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _add_export_file(
    archive: tarfile.TarFile,
    path: Path,
    arcname: str,
    expected_item: Mapping[str, Any],
) -> None:
    expected_size = expected_item.get("size")
    expected_sha256 = expected_item.get("sha256")
    if not _is_nonnegative_int(expected_size) or not _is_sha256(expected_sha256):
        raise RecoveryError("source_export_manifest_invalid")
    descriptor = -1
    handle: BinaryIO | None = None
    try:
        initial = os.lstat(path)
        if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
            raise RecoveryError("source_export_changed_during_package")
        descriptor = os.open(
            str(path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise RecoveryError("source_export_changed_during_package")
        handle = os.fdopen(descriptor, "rb", closefd=False)
        reader = _HashingReader(handle)
        archive.addfile(_tar_info(arcname, expected_size), reader)
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
        if (
            identity_before != identity_after
            or reader.total != expected_size
            or reader.digest.hexdigest() != expected_sha256
        ):
            raise RecoveryError("source_export_changed_during_package")
    except RecoveryError:
        raise
    except OSError as exc:
        raise RecoveryError("source_export_changed_during_package") from exc
    finally:
        if handle is not None:
            handle.close()
        if descriptor >= 0:
            os.close(descriptor)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            str(temporary),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        body = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        offset = 0
        while offset < len(body):
            offset += os.write(descriptor, body[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class OpenPGPCryptor:
    """GPG adapter that keeps plaintext and ciphertext streaming only."""

    def __init__(
        self,
        gpg_binary: str = "gpg",
        *,
        runner: Callable[..., Any] = subprocess.run,
        process_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.gpg_binary = gpg_binary
        self._runner = runner
        self._process_factory = process_factory

    def assert_recipient(self, fingerprint: str) -> None:
        recipient = require_fingerprint(fingerprint)
        try:
            completed = self._runner(
                [self.gpg_binary, "--batch", "--with-colons", "--list-keys", recipient],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RecoveryError("recipient_public_key_unavailable") from exc
        if getattr(completed, "returncode", 1) != 0:
            raise RecoveryError("recipient_public_key_unavailable")
        primary_fingerprint = ""
        primary_pending = False
        for line in str(getattr(completed, "stdout", "")).splitlines():
            fields = line.split(":")
            tag = fields[0] if fields else ""
            if tag == "pub":
                primary_pending = True
                continue
            if tag == "fpr":
                if primary_pending and len(fields) > 9:
                    primary_fingerprint = fields[9].upper()
                primary_pending = False
                continue
            if tag in {"sub", "uid", "uat"}:
                primary_pending = False
        if primary_fingerprint != recipient:
            raise RecoveryError("recipient_public_key_unavailable")

    def _pump_gpg(
        self,
        argv: Sequence[str],
        write_stdin: Callable[[BinaryIO], None],
        consume_stdout: Callable[[BinaryIO], None],
    ) -> None:
        try:
            process = self._process_factory(
                list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RecoveryError("gpg_stream_failed") from exc
        writer_errors: list[BaseException] = []
        consumer_errors: list[BaseException] = []

        def write_input() -> None:
            try:
                if process.stdin is None:
                    raise RecoveryError("gpg_stream_failed")
                write_stdin(process.stdin)
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
        try:
            if process.stdout is None:
                raise RecoveryError("gpg_stream_failed")
            consume_stdout(process.stdout)
        except BaseException as exc:
            consumer_errors.append(exc)
        finally:
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
        writer.join()
        try:
            returncode = process.wait()
        except OSError:
            returncode = 1
        if writer_errors or consumer_errors or returncode != 0:
            raise RecoveryError("gpg_stream_failed")

    def encrypt(
        self,
        fingerprint: str,
        write_plaintext: Callable[[BinaryIO], None],
        write_ciphertext: Callable[[bytes], int],
    ) -> None:
        recipient = require_fingerprint(fingerprint)

        def consume(ciphertext: BinaryIO) -> None:
            while True:
                block = ciphertext.read(1024 * 1024)
                if not block:
                    return
                write_ciphertext(block)

        self._pump_gpg(
            [
                self.gpg_binary,
                "--batch",
                "--yes",
                "--trust-model",
                "always",
                "--encrypt",
                "--recipient",
                recipient,
                "--output",
                "-",
            ],
            write_plaintext,
            consume,
        )

    def decrypt(
        self,
        part_paths: Sequence[Path],
        consume_plaintext: Callable[[BinaryIO], None],
    ) -> None:
        def write_ciphertext(stream: BinaryIO) -> None:
            for part in part_paths:
                with part.open("rb") as handle:
                    shutil.copyfileobj(handle, stream, length=1024 * 1024)

        self._pump_gpg(
            [self.gpg_binary, "--batch", "--yes", "--decrypt", "--output", "-"],
            write_ciphertext,
            consume_plaintext,
        )


def _package_id(source: Mapping[str, Any]) -> str:
    generated_at = str(source["generated_at"])
    parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    return "immortal-recovery-" + parsed.strftime("%Y%m%d") + "-" + str(
        source["manifest_sha256"]
    )[:8]


def _stream_export_tar(
    output: BinaryIO,
    export_dir: Path,
    manifest_bytes: bytes,
    items: Mapping[str, Mapping[str, Any]],
) -> None:
    with tarfile.open(fileobj=output, mode="w|") as archive:
        archive.addfile(
            _tar_info("export/manifest.json", len(manifest_bytes)),
            io.BytesIO(manifest_bytes),
        )
        for relative in sorted(items):
            _add_export_file(
                archive,
                export_dir / relative,
                "export/" + relative,
                items[relative],
            )


def _build_failure(package_dir: Path, code: str, writer: SplitDigestWriter | None = None) -> dict[str, Any]:
    if writer is not None:
        writer.abort()
    shutil.rmtree(package_dir, ignore_errors=True)
    return {"ok": False, "blockers": [code]}


def build_encrypted_package(
    export_dir: str | Path,
    package_dir: str | Path,
    *,
    recipient_fingerprint: str,
    part_bytes: int = 512 * 1024 * 1024,
    cryptor: Any | None = None,
) -> dict[str, Any]:
    """Create a local package of encrypted split parts from a strict export."""
    destination = Path(package_dir).expanduser().absolute()
    if os.path.lexists(destination):
        return {"ok": False, "blockers": ["package_destination_exists"]}
    writer: SplitDigestWriter | None = None
    try:
        source = inspect_source_export(export_dir)
        source_dir = Path(export_dir).expanduser().absolute()
        manifest_bytes = _read_regular_bytes(source_dir / export_restore.MANIFEST_NAME)
        source_manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(source_manifest, dict):
            raise RecoveryError("source_export_manifest_invalid")
        items = _manifest_items_by_path(source_manifest)
        if _sha256_bytes(manifest_bytes) != source["manifest_sha256"]:
            raise RecoveryError("source_export_changed_during_package")
        destination.mkdir(parents=True, mode=0o700)
        os.chmod(destination, 0o700)
        writer = SplitDigestWriter(destination / PARTS_DIRNAME, part_bytes)
        selected_cryptor = cryptor if cryptor is not None else OpenPGPCryptor()
        fingerprint = require_fingerprint(recipient_fingerprint)
        selected_cryptor.assert_recipient(fingerprint)
        selected_cryptor.encrypt(
            fingerprint,
            lambda stream: _stream_export_tar(stream, source_dir, manifest_bytes, items),
            writer.write,
        )
        parts = writer.close()
        manifest = build_package_manifest(
            package_id=_package_id(source),
            recipient_fingerprint=fingerprint,
            source=source,
            parts=parts,
        )
        _write_private_json(destination / PACKAGE_MANIFEST_NAME, manifest)
        after = inspect_source_export(source_dir)
        if after != source:
            raise RecoveryError("source_export_changed_during_package")
        local = verify_local_package(destination)
        if not local.get("ok"):
            raise RecoveryError("package_local_verification_failed")
        return {
            "ok": True,
            "package_id": manifest["package_id"],
            "package_dir": str(destination),
            "parts": parts,
        }
    except RecoveryError as exc:
        return _build_failure(destination, exc.code, writer)
    except Exception:
        return _build_failure(destination, "encryption_failed", writer)


def _safe_package_tree(package_dir: Path, allowed_files: set[str]) -> None:
    metadata = os.lstat(package_dir)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RecoveryError("package_contents_invalid")
    allowed_directories = {PARTS_DIRNAME}
    for root, directories, files in os.walk(package_dir, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            path = root_path / directory
            relative = path.relative_to(package_dir).as_posix()
            entry = os.lstat(path)
            if (
                relative not in allowed_directories
                or not stat.S_ISDIR(entry.st_mode)
                or stat.S_ISLNK(entry.st_mode)
            ):
                raise RecoveryError("package_contents_invalid")
        for filename in files:
            path = root_path / filename
            relative = path.relative_to(package_dir).as_posix()
            entry = os.lstat(path)
            if (
                relative not in allowed_files
                or not stat.S_ISREG(entry.st_mode)
                or stat.S_ISLNK(entry.st_mode)
            ):
                raise RecoveryError("package_contents_invalid")


def verify_local_package(package_dir: str | Path) -> dict[str, Any]:
    """Verify exact local package bytes before any cloud write or restore action."""
    candidate = Path(package_dir).expanduser().absolute()
    try:
        manifest, _manifest_sha256, _manifest_bytes = _read_json_object(
            candidate / PACKAGE_MANIFEST_NAME
        )
        validate_package_manifest(manifest)
        parts = _validate_parts(manifest["parts"])
        _safe_package_tree(
            candidate,
            {PACKAGE_MANIFEST_NAME, *(part["name"] for part in parts)},
        )
        total_bytes = 0
        for part in parts:
            size, digest = _secure_file_size_sha256(candidate / part["name"])
            if size != part["bytes"] or digest != part["sha256"]:
                raise RecoveryError("package_contents_invalid")
            total_bytes += size
        return {
            "ok": True,
            "package_id": manifest["package_id"],
            "parts": len(parts),
            "bytes": total_bytes,
            "blockers": [],
        }
    except (OSError, RecoveryError, ValueError):
        return {
            "ok": False,
            "package_id": "",
            "parts": 0,
            "bytes": 0,
            "blockers": ["package_contents_invalid"],
        }
