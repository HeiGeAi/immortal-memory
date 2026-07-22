#!/usr/bin/env python3
"""Portable export and restore checks for the Immortal memory vault.

This module is intentionally metadata-first. The manifest records file paths,
sizes, modification times, and hashes, but it never samples or prints source
content from the memory vault.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


IMMORTAL_DIR = Path.home() / ".immortal"
EXPORTS_DIRNAME = "exports"
MANIFEST_NAME = "manifest.json"
SECRET_REDACTION_RECEIPT = "secret-redaction-receipt.json"
EXPORT_PREFIX = "immortal-export-"

REQUIRED_PATHS = [
    "config.json",
    "daily-backup.sh",
    "profile.md",
    "profile_compact.md",
    "profile.json",
    "digital-soul.md",
    "index.jsonl",
    "sources.json",
    "orchestrator_state.json",
    "backup.log",
    "daily",
    "files",
    "reviewed",
    "feishu/clean",
    "feishu/distilled",
    "feishu/state.json",
    "feishu/state.sqlite3",
    "feishu/log.jsonl",
    "feishu/reports",
    "people",
    "quality",
    "relationships",
    # timeline.html / dashboard.html / brief 已于 2026-06-14 停用并删除；
    # 留在白名单里会让每个新导出都带 missing 警告，strict 校验对完好数据永久 FAIL
    "digests",
    "summaries",
]

RAW_PATHS = [
    "feishu/raw",
    "raw",
]

V11_DERIVED_PATHS = [
    "model/claims/events.jsonl",
    "model/claims/current.jsonl",
    "model/attribution/latest-report.json",
    "model/living-self/current.json",
    "model/living-self/current.md",
    "model/living-self/versions",
    "model/living-self/evaluations",
    "judgment/events.jsonl",
    "judgment/current.jsonl",
    "judgment/evaluations.jsonl",
    "contexts/events.jsonl",
    "contexts/current.jsonl",
    "contexts/packs",
    "outcomes/events.jsonl",
]

V11_REQUIRED_EVENT_PATHS = [
    "model/claims/events.jsonl",
    "judgment/events.jsonl",
    "contexts/events.jsonl",
    "outcomes/events.jsonl",
]

V11_REQUIRED_CURRENT_PATHS = [
    "model/claims/current.jsonl",
    "judgment/current.jsonl",
    "judgment/evaluations.jsonl",
    "contexts/current.jsonl",
]

V11_STREAM_PATHS = {
    "claims": "model/claims/events.jsonl",
    "judgments": "judgment/events.jsonl",
    "contexts": "contexts/events.jsonl",
    "outcomes": "outcomes/events.jsonl",
}

V11_MIGRATION_DIR = Path("model/migrations/v1.1")
MAX_INDEX_LINE_BYTES = 16 * 1024 * 1024
HERMES_SESSIONS_ROOT = Path.home() / ".hermes" / "sessions"
TIMEZONE_EVIDENCE_AUTHORITY = "hermes-session-metadata-v1"

SKIP_DIR_NAMES = {
    EXPORTS_DIRNAME,
    ".git",
    "__pycache__",
}


def vault_path(vault_dir: str | Path | None = None) -> Path:
    return Path(vault_dir).expanduser() if vault_dir else IMMORTAL_DIR


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_stamp() -> str:
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def file_item(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "relpath": relpath(path, root),
        "size": stat.st_size,
        "sha256": sha256_file(path),
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def should_skip_dir(path: Path, source_root: Path, export_root: Path | None = None) -> bool:
    if path.name in SKIP_DIR_NAMES:
        return True
    try:
        if export_root and path.resolve().is_relative_to(export_root.resolve()):
            return True
    except OSError:
        return False
    try:
        if path.resolve().is_relative_to((source_root / EXPORTS_DIRNAME).resolve()):
            return True
    except OSError:
        return False
    return False


def iter_files(root: Path, source_root: Path, export_root: Path | None = None) -> list[Path]:
    if root.is_symlink():
        raise ValueError(f"unsafe symlink in export source: {relpath(root, source_root)}")
    if root.is_file():
        return [root]
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"unsafe symlink in export source: {relpath(path, source_root)}")
        if path.is_dir():
            continue
        if any(should_skip_dir(parent, source_root, export_root) for parent in [path.parent, *path.parents]):
            continue
        files.append(path)
    return files


def collect_source_files(vault: Path, include_raw: bool, warnings: list[str]) -> list[Path]:
    selected: dict[str, Path] = {}
    requested = list(REQUIRED_PATHS)
    if include_raw:
        requested.extend(RAW_PATHS)

    for entry in requested:
        path = vault / entry
        if not path.exists():
            warnings.append(f"missing: {entry}")
            continue
        for file_path in iter_files(path, vault):
            selected[relpath(file_path, vault)] = file_path

    for entry in V11_DERIVED_PATHS:
        path = vault / entry
        if not path.exists():
            continue
        for file_path in iter_files(path, vault):
            selected[relpath(file_path, vault)] = file_path

    return [selected[key] for key in sorted(selected)]


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def new_export_dir(base_output: Path) -> Path:
    base = base_output / f"{EXPORT_PREFIX}{now_stamp()}"
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = Path(f"{base}-{index:03d}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"unable to allocate unique export directory under {base_output}")


def classify_storage_location(target: Path, vault: Path) -> str:
    """Classify an export target relative to the vault's disaster domain."""
    try:
        resolved_target = target.resolve()
        resolved_vault = vault.resolve()
    except OSError:
        return "unknown"
    if resolved_vault == resolved_target or resolved_vault in resolved_target.parents:
        return "internal_vault"
    probe = resolved_target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        if vault.exists() and probe.exists() and os.stat(probe).st_dev == os.stat(resolved_vault).st_dev:
            return "same_disk"
    except OSError:
        return "unknown"
    return "external_disk"


class SecretShapesFound(RuntimeError):
    """出口扫描发现未脱敏凭证形态且调用方要求 fail-closed。"""


def _v11_manifest_metadata(vault: Path, warnings: list[str]) -> dict[str, Any]:
    present = {
        name: (vault / relative).is_file()
        for name, relative in V11_STREAM_PATHS.items()
    }
    if not any(present.values()):
        return {
            "event_heads": {name: 0 for name in V11_STREAM_PATHS},
            "current_watermarks": {
                name: 0 for name in ("claims", "judgments", "contexts")
            },
            "schema_versions": {"model": 0, "events": 0},
        }
    if not all(present.values()):
        missing = [name for name, available in present.items() if not available]
        warnings.append("v11_event_layers_incomplete: " + ",".join(missing))
        return {
            "event_heads": {name: 0 for name in V11_STREAM_PATHS},
            "current_watermarks": {
                name: 0 for name in ("claims", "judgments", "contexts")
            },
            "schema_versions": {"model": 0, "events": 0},
        }
    missing_current = [
        relative
        for relative in V11_REQUIRED_CURRENT_PATHS
        if not (vault / relative).is_file()
    ]
    if missing_current:
        warnings.append(
            "v11_current_layers_incomplete: " + ",".join(missing_current)
        )
    heads = {
        name: _read_event_head_without_lock(vault / relative)
        for name, relative in V11_STREAM_PATHS.items()
    }
    return {
        "event_heads": heads,
        "current_watermarks": {
            name: heads[name] for name in ("claims", "judgments", "contexts")
        },
        "schema_versions": {"model": 1, "events": 1},
    }


def create_export(
    vault_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    include_raw: bool = False,
    fail_on_secrets: bool = False,
    redact_secrets: bool = False,
) -> dict[str, Any]:
    """Create a portable export directory and return its manifest payload."""
    vault = vault_path(vault_dir)
    base_output = Path(output_dir).expanduser() if output_dir else vault / EXPORTS_DIRNAME
    export_dir = new_export_dir(base_output)
    warnings: list[str] = []

    location = classify_storage_location(base_output, vault)
    if location == "internal_vault":
        warnings.append(
            "export_inside_vault: 导出目录位于 vault 内部，vault 被误删时备份会一起消失；请用 --output-dir 指向外置盘或同步目录"
        )
    elif location == "same_disk":
        warnings.append(
            "export_same_disk: 导出目录与 vault 在同一块磁盘上，无法抵御磁盘损坏；建议改用外置盘或同步目录"
        )

    if not vault.exists():
        warnings.append(f"vault_missing: {vault}")

    files = collect_source_files(vault, include_raw, warnings) if vault.exists() else []
    export_dir.mkdir(parents=True, exist_ok=False)

    items: list[dict[str, Any]] = []
    total_bytes = 0
    secret_redaction: dict[str, Any] = {}
    try:
        for source in files:
            relative = relpath(source, vault)
            target = export_dir / relative
            if relative == "index.jsonl" and redact_secrets:
                import secret_scan

                secret_redaction = secret_scan.redact_jsonl_copy(source, target)
            else:
                copy_file(source, target)
            item = file_item(target, export_dir)
            items.append(item)
            total_bytes += item["size"]
        if redact_secrets and secret_redaction:
            receipt_path = export_dir / SECRET_REDACTION_RECEIPT
            write_json_atomic(
                receipt_path,
                {
                    "generated_at": iso_utc(),
                    **secret_redaction,
                },
            )
            receipt_item = file_item(receipt_path, export_dir)
            items.append(receipt_item)
            total_bytes += receipt_item["size"]
    except Exception:
        shutil.rmtree(export_dir, ignore_errors=True)
        raise

    # 出口敏感形态扫描（hash-only，绝不写入原文）。index 清洗完成前默认只告警不阻断，
    # 避免自动导出断流；清洗后调用方应传 fail_on_secrets=True 转为硬门禁。
    secret_summary: dict[str, Any] = {}
    exported_index = export_dir / "index.jsonl"
    if exported_index.is_file():
        import secret_scan

        report = secret_scan.scan_file(exported_index)
        secret_summary = {
            "scanned_file": "index.jsonl",
            "unique_candidates": report["unique_candidates"],
            "unique_by_pattern": report["unique_by_pattern"],
            "scan_complete": report.get("scan_complete", False),
            "invalid_json_line_count": report.get("invalid_json_line_count", 0),
            "oversized_line_count": report.get("oversized_line_count", 0),
        }
        if not report.get("scan_complete", False):
            warnings.append(
                "secret_scan_incomplete: index.jsonl 含无效 JSON、无效 UTF-8 或超大行"
            )
            if fail_on_secrets:
                shutil.rmtree(export_dir, ignore_errors=True)
                raise SecretShapesFound("export aborted: index.jsonl secret scan is incomplete")
        if report["unique_candidates"] > 0:
            warnings.append(
                f"secret_shapes_present: index.jsonl 含 {report['unique_candidates']} 个未脱敏凭证形态候选"
            )
            if fail_on_secrets:
                shutil.rmtree(export_dir, ignore_errors=True)
                raise SecretShapesFound(
                    f"export aborted: {report['unique_candidates']} unique secret-shape candidates in index.jsonl"
                )

    v11_metadata = _v11_manifest_metadata(export_dir, warnings)
    if v11_metadata.get("schema_versions") == {"model": 1, "events": 1}:
        replay_warnings = _v11_projection_warnings(export_dir, v11_metadata)
        warnings.extend(
            "v11_snapshot_invalid: " + warning for warning in replay_warnings
        )
    manifest = {
        "generated_at": iso_utc(),
        "vault_dir": str(vault),
        "export_dir": str(export_dir),
        "storage_location": location,
        "secret_scan": secret_summary,
        "secret_redaction": {
            **secret_redaction,
            "receipt": SECRET_REDACTION_RECEIPT,
        } if secret_redaction else {},
        "include_raw": bool(include_raw),
        "items": items,
        "totals": {
            "files": len(items),
            "bytes": total_bytes,
            "warnings": len(warnings),
        },
        "restore_notes": [
            "Run restore_check(export_path) before trusting or restoring this export.",
            "This export contains file copies plus metadata only; manifest does not sample sensitive content.",
            "Restore is intentionally not automatic in v0.8; copy verified files back into a chosen vault after review.",
        ],
        "warnings": warnings,
        **v11_metadata,
    }
    write_json_atomic(export_dir / MANIFEST_NAME, manifest)
    return manifest


def find_latest_export(vault_dir: str | Path | None = None) -> dict[str, Any]:
    """Find the newest export under the vault exports directory."""
    vault = vault_path(vault_dir)
    exports_dir = vault / EXPORTS_DIRNAME
    candidates: list[Path] = []
    if exports_dir.exists():
        candidates = [
            path
            for path in exports_dir.iterdir()
            if path.is_dir() and path.name.startswith(EXPORT_PREFIX) and (path / MANIFEST_NAME).exists()
        ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    latest = candidates[0] if candidates else None
    manifest = read_manifest(latest / MANIFEST_NAME) if latest else {}
    return {
        "vault_dir": str(vault),
        "exports_dir": str(exports_dir),
        "found": latest is not None,
        "export_dir": str(latest) if latest else "",
        "manifest_path": str(latest / MANIFEST_NAME) if latest else "",
        "generated_at": manifest.get("generated_at", "") if isinstance(manifest, dict) else "",
        "totals": manifest.get("totals", {}) if isinstance(manifest, dict) else {},
    }


def get_backup_status(vault_dir: str | Path | None = None, verify: bool = False) -> dict[str, Any]:
    """Return a compact status summary for the latest portable export."""
    latest = find_latest_export(vault_dir)
    if not latest["found"]:
        return {
            "ok": False,
            "vault_dir": latest["vault_dir"],
            "exports_dir": latest["exports_dir"],
            "latest_export": {},
            "warnings": ["no_export_found"],
        }

    manifest_path = Path(latest["manifest_path"])
    manifest = read_manifest(manifest_path)
    manifest_ok = bool(
        manifest
        and isinstance(manifest.get("items"), list)
        and isinstance(manifest.get("totals"), dict)
        and int((manifest.get("totals") or {}).get("files") or 0) > 0
    )
    if verify:
        check = restore_check(latest["export_dir"], strict=True)
        check["mode"] = "strict-sha256"
    else:
        check = {
            "ok": manifest_ok,
            "mode": "manifest-only",
            "checked_files": 0,
            "missing": [],
            "mismatched": [],
            "warnings": [] if manifest_ok else ["manifest_missing_or_invalid"],
        }
    if verify:
        trust_level = "verified" if check.get("ok") else "failed"
    else:
        trust_level = "manifest_only"
    actual_location = classify_storage_location(
        Path(latest["export_dir"]),
        Path(latest["vault_dir"]),
    )
    return {
        "ok": bool(check.get("ok")),
        "trust_level": trust_level,
        "generated_at": manifest.get("generated_at", ""),
        "storage_location": actual_location,
        "secret_scan": manifest.get("secret_scan", {}),
        "generation_warnings": manifest.get("warnings", []),
        "vault_dir": latest["vault_dir"],
        "exports_dir": latest["exports_dir"],
        "latest_export": latest,
        "mode": "verified" if verify else "manifest-only",
        "check": {
            "ok": check.get("ok"),
            "mode": check.get("mode", "sha256"),
            "checked_files": check.get("checked_files"),
            "missing_files": len(check.get("missing", [])),
            "mismatched_files": len(check.get("mismatched", [])),
            "warnings": check.get("warnings", []),
        },
        "warnings": check.get("warnings", []),
    }


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def migration_backup_gate(
    evidence: dict[str, Any] | None,
    require_external: bool = True,
    *,
    max_age_hours: float = 168,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fail closed unless migration evidence proves a fresh, restorable backup.

    The returned result contains classifications and blocker codes only. It
    intentionally never copies warning bodies or credential candidates into
    the result.
    """
    payload = evidence if isinstance(evidence, dict) else {}
    blockers: list[str] = []

    storage = payload.get("storage")
    if isinstance(storage, dict):
        location = str(storage.get("location") or "unknown")
    else:
        location = str(payload.get("storage_location") or "unknown")
    if require_external and location != "external_disk":
        blockers.append("backup_not_external")

    verification = payload.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    status_check = payload.get("check")
    if not isinstance(status_check, dict):
        status_check = {}
    verification_mode = str(
        verification.get("mode")
        or payload.get("verification_mode")
        or status_check.get("mode")
        or payload.get("mode")
        or ""
    )
    verification_ok = verification.get("ok")
    if verification_ok is None:
        verification_ok = status_check.get("ok")
    if verification_mode != "strict-sha256" or verification_ok is not True:
        blockers.append("verification_not_strict")

    restore_evidence = payload.get("restore_check")
    if not isinstance(restore_evidence, dict):
        restore_evidence = payload.get("check")
    if not isinstance(restore_evidence, dict) or restore_evidence.get("ok") is not True:
        blockers.append("restore_check_missing_or_failed")

    warnings = payload.get("warnings")
    warning_codes = [str(item) for item in warnings] if isinstance(warnings, list) else []
    secret_scan = payload.get("secret_scan")
    secret_count = 0
    secret_scan_valid = isinstance(secret_scan, dict) and "unique_candidates" in secret_scan
    if secret_scan_valid:
        raw_secret_count = secret_scan.get("unique_candidates")
        if type(raw_secret_count) is not int or raw_secret_count < 0:
            secret_scan_valid = False
        else:
            secret_count = raw_secret_count
    if not secret_scan_valid:
        blockers.append("secret_scan_invalid")
    elif secret_count > 0 or any("secret_shapes_present" in item for item in warning_codes):
        blockers.append("secret_shapes_present")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    generated = _parse_utc_timestamp(payload.get("generated_at"))
    age_hours: float | None = None
    if generated is None or max_age_hours <= 0:
        blockers.append("backup_timestamp_invalid")
    else:
        age = current - generated
        age_hours = age.total_seconds() / 3600
        if age < timedelta(0):
            blockers.append("backup_timestamp_invalid")
        elif age > timedelta(hours=max_age_hours):
            blockers.append("backup_stale")

    health = payload.get("health")
    if not isinstance(health, dict) or health.get("ok") is not True:
        blockers.append("health_check_failed")

    index_parity = payload.get("index_parity")
    if not isinstance(index_parity, dict) or index_parity.get("ok") is not True:
        blockers.append("index_parity_failed")

    unique_blockers = list(dict.fromkeys(blockers))
    return {
        "ok": not unique_blockers,
        "blockers": unique_blockers,
        "storage_location": location,
        "verification_mode": verification_mode,
        "backup_age_hours": round(age_hours, 3) if age_hours is not None else None,
        "max_age_hours": max_age_hours,
    }


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _canonical_system_alias_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    for alias_text in ("/var", "/tmp", "/etc"):
        alias = Path(alias_text)
        try:
            if alias.is_symlink() and (absolute == alias or alias in absolute.parents):
                return alias.resolve(strict=True) / absolute.relative_to(alias)
        except (OSError, ValueError):
            continue
    return absolute


def _open_absolute_nofollow(path: Path, *, directory: bool = False) -> int:
    absolute = _canonical_system_alias_path(path)
    parts = absolute.parts
    current_fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= os.O_DIRECTORY
        return os.open(parts[-1], flags, dir_fd=current_fd)
    finally:
        os.close(current_fd)


def _read_json_nofollow(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    fd = -1
    try:
        fd = _open_absolute_nofollow(path)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return {}
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
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
            return {}
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    return payload if isinstance(payload, dict) else {}


def _secure_directory_identity(path: Path) -> tuple[int, int] | None:
    fd = -1
    try:
        fd = _open_absolute_nofollow(path, directory=True)
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            return None
        return metadata.st_dev, metadata.st_ino
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _secure_directory(path: Path) -> bool:
    return _secure_directory_identity(path) is not None


def _secure_file_size_and_sha256(path: Path) -> tuple[int, str] | None:
    fd = -1
    try:
        fd = _open_absolute_nofollow(path)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            return None
        return before.st_size, digest.hexdigest()
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _read_file_generation(
    path: Path,
    *,
    max_bytes: int | None = None,
    capture_body: bool = False,
) -> tuple[bytes | None, str, tuple[int, int, int, int, int]] | None:
    """Read one regular file and bind the returned bytes to one FD generation."""
    descriptor = -1
    try:
        descriptor = _open_absolute_nofollow(path)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None
        if max_bytes is not None and before.st_size > max_bytes:
            return None
        chunks: list[bytes] | None = [] if capture_body else None
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                return None
            if chunks is not None:
                chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity or total != before.st_size:
            return None
        body = b"".join(chunks) if chunks is not None else None
        return body, digest.hexdigest(), before_identity
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_private_json_generation(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], str, tuple[int, int, int, int, int]] | None:
    descriptor = -1
    try:
        descriptor = _open_absolute_nofollow(path)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_mode & 0o777 != 0o600
            or before.st_size > max_bytes
        ):
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if remaining <= 0 or before_identity != after_identity:
            return None
        body = b"".join(chunks)
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            return None
        return value, hashlib.sha256(body).hexdigest(), before_identity
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _valid_aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _load_timezone_contract(
    path: str | Path | None,
    *,
    source_sha256: str,
) -> tuple[dict[str, Any] | None, list[str], str]:
    if path is None:
        return None, [], ""
    candidate = Path(path).expanduser().absolute()
    contract_generation = _read_private_json_generation(
        candidate,
        max_bytes=1024 * 1024,
    )
    if contract_generation is None:
        return None, ["timezone_contract_unsafe"], ""
    value, contract_sha256, _contract_identity = contract_generation
    blockers: list[str] = []
    evidence = value.get("evidence")
    session_ids = value.get("session_ids")
    fold_by_record_id = value.get("fold_by_record_id", {})
    if (
        value.get("schema_version") != 1
        or value.get("source") != "hermes-conversation"
        or value.get("timestamp_semantics") != "local_wall_time"
        or not isinstance(value.get("timezone"), str)
        or not value.get("timezone", "").strip()
        or not isinstance(session_ids, list)
        or not session_ids
        or any(not isinstance(item, str) or not item.strip() for item in session_ids)
        or len(set(session_ids)) != len(session_ids)
        or not isinstance(fold_by_record_id, dict)
        or any(
            not isinstance(record_id, str)
            or not record_id.strip()
            or type(fold) is not int
            or fold not in {0, 1}
            for record_id, fold in (
                fold_by_record_id.items()
                if isinstance(fold_by_record_id, dict)
                else []
            )
        )
        or not isinstance(value.get("verified_by"), str)
        or not value.get("verified_by", "").strip()
        or not _valid_aware_timestamp(value.get("verified_at"))
        or not isinstance(evidence, dict)
        or evidence.get("kind") != "source_metadata"
        or not isinstance(evidence.get("reference"), str)
        or not evidence.get("reference", "").strip()
        or not isinstance(evidence.get("path"), str)
        or not evidence.get("path", "").strip()
        or not isinstance(evidence.get("sha256"), str)
        or len(evidence.get("sha256", "")) != 64
        or any(ch not in "0123456789abcdef" for ch in evidence.get("sha256", ""))
    ):
        blockers.append("timezone_contract_invalid")
    try:
        ZoneInfo(str(value.get("timezone") or ""))
    except ZoneInfoNotFoundError:
        blockers.append("timezone_contract_invalid")
    if isinstance(evidence, dict) and isinstance(evidence.get("path"), str):
        evidence_path = Path(evidence["path"]).expanduser()
        try:
            trusted_root = HERMES_SESSIONS_ROOT.resolve(strict=True)
            resolved_evidence = evidence_path.resolve(strict=True)
            resolved_evidence.relative_to(trusted_root)
        except (FileNotFoundError, OSError, ValueError):
            blockers.append("timezone_evidence_unsafe")
        else:
            evidence_generation = _read_private_json_generation(
                resolved_evidence,
                max_bytes=1024 * 1024,
            )
            if (
                evidence_generation is None
                or evidence_generation[1] != evidence.get("sha256")
            ):
                blockers.append("timezone_evidence_mismatch")
            else:
                source_metadata = evidence_generation[0]
                expected_metadata = {
                    "schema_version": 1,
                    "source": value.get("source"),
                    "timestamp_semantics": value.get("timestamp_semantics"),
                    "timezone": value.get("timezone"),
                    "session_ids": session_ids,
                    "fold_by_record_id": fold_by_record_id,
                    "verified_by": value.get("verified_by"),
                    "verified_at": value.get("verified_at"),
                }
                if (
                    source_metadata.get("authority")
                    != TIMEZONE_EVIDENCE_AUTHORITY
                    or any(
                        source_metadata.get(key) != expected
                        for key, expected in expected_metadata.items()
                    )
                ):
                    blockers.append("timezone_evidence_contract_mismatch")
    if value.get("source_index_sha256") != source_sha256:
        blockers.append("timezone_contract_source_mismatch")
    if blockers:
        return None, list(dict.fromkeys(blockers)), contract_sha256
    return value, [], contract_sha256


def _aware_wall_time(
    raw: str,
    zone: ZoneInfo,
    *,
    fold: int | None = None,
) -> tuple[str | None, str | None]:
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        return None, "invalid_timestamp"
    if naive.tzinfo is not None and naive.utcoffset() is not None:
        return raw, None
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset() and fold not in {0, 1}:
        return None, "ambiguous_wall_time"
    selected = naive.replace(tzinfo=zone, fold=int(fold or 0))
    round_trip = (
        selected.astimezone(timezone.utc)
        .astimezone(zone)
        .replace(tzinfo=None)
    )
    if round_trip != naive:
        return None, "nonexistent_wall_time"
    return selected.isoformat(), None


def _timezone_contract_resolver(
    contract: dict[str, Any],
) -> Callable[[dict[str, Any]], str]:
    """Resolve only the naive Hermes rows covered by one verified contract."""
    from index_integrity import IndexIntegrityError

    source_name = str(contract["source"])
    session_ids = set(contract["session_ids"])
    folds = contract.get("fold_by_record_id", {})
    if not isinstance(folds, dict):
        raise IndexIntegrityError("timezone contract fold map is invalid")
    zone = ZoneInfo(str(contract["timezone"]))

    def resolve(record: dict[str, Any]) -> str:
        if str(record.get("source") or "") != source_name:
            raise IndexIntegrityError("timezone contract does not cover record source")
        if str(record.get("session_id") or "") not in session_ids:
            raise IndexIntegrityError("timezone contract does not cover record session")
        resolved, error = _aware_wall_time(
            str(record.get("timestamp") or ""),
            zone,
            fold=folds.get(str(record.get("id") or "")),
        )
        if error or resolved is None:
            raise IndexIntegrityError(
                f"timezone contract cannot resolve record timestamp: {error or 'unknown'}"
            )
        return resolved

    return resolve


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    write_json_atomic(path, value)
    path.chmod(0o600)


def stage_v11_index(
    vault_dir: str | Path,
    *,
    timezone_contract: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create a schema-v3-safe staging source without changing index.jsonl."""
    vault = vault_path(vault_dir).absolute()
    source = vault / "index.jsonl"
    verified = _secure_file_size_and_sha256(source)
    if verified is None:
        return {
            "ok": False,
            "production_switch_allowed": False,
            "blockers": ["source_index_unsafe_or_missing"],
            "converted": 0,
            "quarantined": 0,
        }
    source_size, source_sha256 = verified
    contract, contract_blockers, contract_sha256 = _load_timezone_contract(
        timezone_contract,
        source_sha256=source_sha256,
    )
    target_root = (
        Path(output_dir).expanduser().absolute()
        if output_dir is not None
        else vault / V11_MIGRATION_DIR
    )
    target_root.mkdir(parents=True, exist_ok=True)
    if not _secure_directory(target_root):
        return {
            "ok": False,
            "production_switch_allowed": False,
            "blockers": ["migration_output_unsafe"],
            "converted": 0,
            "quarantined": 0,
        }
    staging = target_root / "index.staging.jsonl"
    quarantine_path = target_root / "timestamp-quarantine.json"
    receipt_path = target_root / "index-staging-receipt.json"
    temp = staging.with_name(staging.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    converted = 0
    total = 0
    quarantine: list[dict[str, Any]] = []
    stream_digest = hashlib.sha256()
    session_ids = set(contract.get("session_ids", [])) if contract else set()
    folds = contract.get("fold_by_record_id", {}) if contract else {}
    if not isinstance(folds, dict):
        folds = {}
    zone = ZoneInfo(contract["timezone"]) if contract else None
    source_before = os.lstat(source)
    try:
        source_fd = _open_absolute_nofollow(source)
        with os.fdopen(source_fd, "rb", closefd=True) as handle, temp.open("xb") as output:
            temp.chmod(0o600)
            for line_number, raw in enumerate(handle, start=1):
                total += 1
                stream_digest.update(raw)
                reason = ""
                record: dict[str, Any] | None = None
                if len(raw) > MAX_INDEX_LINE_BYTES:
                    reason = "record_too_large"
                else:
                    try:
                        decoded = json.loads(raw)
                        record = decoded if isinstance(decoded, dict) else None
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        record = None
                    if record is None:
                        reason = "invalid_json_record"
                timestamp = record.get("timestamp") if record else None
                parsed: datetime | None = None
                if not reason:
                    try:
                        parsed = datetime.fromisoformat(
                            str(timestamp or "").strip().replace("Z", "+00:00")
                        )
                    except ValueError:
                        reason = "invalid_timestamp"
                if not reason and parsed is not None and (
                    parsed.tzinfo is None or parsed.utcoffset() is None
                ):
                    source_name = str(record.get("source") or "")
                    session_id = str(record.get("session_id") or "")
                    if contract is None or source_name != contract["source"]:
                        reason = "missing_timezone_evidence"
                    elif session_id not in session_ids:
                        reason = "session_outside_verified_contract"
                    else:
                        resolved, conversion_error = _aware_wall_time(
                            str(timestamp),
                            zone,
                            fold=folds.get(str(record.get("id") or "")),
                        )
                        if conversion_error:
                            reason = conversion_error
                        else:
                            assert resolved is not None
                            migrated = dict(record)
                            migrated["timestamp"] = resolved
                            raw = (
                                json.dumps(
                                    migrated,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                + "\n"
                            ).encode("utf-8")
                            converted += 1
                if reason:
                    quarantine.append(
                        {
                            "line_number": line_number,
                            "record_id": str((record or {}).get("id") or ""),
                            "session_id": str((record or {}).get("session_id") or ""),
                            "source": str((record or {}).get("source") or ""),
                            "timestamp": str(timestamp or ""),
                            "reason": reason,
                        }
                    )
                    continue
                output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        source_after = os.lstat(source)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    source_identity_before = (
        source_before.st_dev,
        source_before.st_ino,
        source_before.st_size,
        source_before.st_mtime_ns,
        source_before.st_ctime_ns,
    )
    source_identity_after = (
        source_after.st_dev,
        source_after.st_ino,
        source_after.st_size,
        source_after.st_mtime_ns,
        source_after.st_ctime_ns,
    )
    if (
        source_identity_before != source_identity_after
        or stream_digest.hexdigest() != source_sha256
        or source_size != source_before.st_size
    ):
        temp.unlink(missing_ok=True)
        return {
            "ok": False,
            "production_switch_allowed": False,
            "blockers": ["source_changed_during_migration"],
            "converted": 0,
            "quarantined": 0,
        }
    temp.replace(staging)
    blockers = list(contract_blockers)
    if quarantine:
        blockers.append("unresolved_naive_timestamps")
    blockers = list(dict.fromkeys(blockers))
    quarantine_report = {
        "schema_version": 1,
        "source_index_sha256": source_sha256,
        "count": len(quarantine),
        "items": quarantine,
    }
    _write_private_json(quarantine_path, quarantine_report)
    receipt = {
        "schema_version": 1,
        "source_index_sha256": source_sha256,
        "source_size": source_size,
        "staging_sha256": sha256_file(staging),
        "staging_size": staging.stat().st_size,
        "timezone_contract_sha256": contract_sha256,
        "total_records": total,
        "converted": converted,
        "quarantined": len(quarantine),
        "blockers": blockers,
    }
    _write_private_json(receipt_path, receipt)
    return {
        "ok": not blockers,
        "production_switch_allowed": False,
        "blockers": blockers or ["production_prewarm_pending"],
        "source_sha256": source_sha256,
        "source_size": source_size,
        "staging_source": str(staging),
        "staging_sha256": receipt["staging_sha256"],
        "timezone_contract_sha256": contract_sha256,
        "quarantine_report": str(quarantine_path),
        "receipt": str(receipt_path),
        "total_records": total,
        "converted": converted,
        "quarantined": len(quarantine),
    }


def _ensure_empty_event_layers(vault: Path) -> None:
    from event_store import safe_atomic_write_text

    for relative in [*V11_REQUIRED_EVENT_PATHS, *V11_REQUIRED_CURRENT_PATHS]:
        path = vault / relative
        if not path.exists():
            safe_atomic_write_text(path, "")


def _replay_v11_event_layers(vault: Path) -> None:
    from claim_store import ClaimStore
    from context_store import ContextStore
    from judgment_store import JudgmentStore
    from outcome_store import OutcomeStore

    ClaimStore(vault)
    JudgmentStore(vault)
    contexts = ContextStore(vault)
    OutcomeStore(vault, context_store=contexts).list()


def build_living_self(vault_dir: str | Path) -> dict[str, Any]:
    vault = vault_path(vault_dir).absolute()
    _ensure_empty_event_layers(vault)
    _replay_v11_event_layers(vault)
    from living_self_service import LivingSelfService

    living_self = LivingSelfService(vault)
    try:
        current = living_self.current()
    except (FileNotFoundError, ValueError):
        current = None
    candidate = living_self.build_candidate()
    changed = bool(
        current is None
        or current.get("content_hash") != candidate.get("content_hash")
        or current.get("based_on_claim_seq") != candidate.get("based_on_claim_seq")
    )
    if changed:
        current = living_self.confirm(candidate, reason="v1.1 migration")
    return {
        "ok": True,
        "changed": changed,
        "version_id": current.get("version_id") if current else "",
        "based_on_claim_seq": current.get("based_on_claim_seq") if current else 0,
    }


def run_v11_migration(
    vault_dir: str | Path,
    *,
    timezone_contract: str | Path | None = None,
) -> dict[str, Any]:
    """Build v1.1 layers only after evidence-backed index reconciliation passes."""
    vault = vault_path(vault_dir).absolute()
    if vault.resolve() == IMMORTAL_DIR.resolve():
        return {
            "ok": False,
            "production_switch_allowed": False,
            "model_layers_written": False,
            "blockers": ["staging_vault_required"],
        }
    staged = stage_v11_index(vault, timezone_contract=timezone_contract)
    if not staged.get("ok"):
        return {
            **staged,
            "model_layers_written": False,
            "production_switch_allowed": False,
        }
    source = vault / "index.jsonl"
    verified_source = _secure_file_size_and_sha256(source)
    if (
        verified_source is None
        or verified_source[1] != staged.get("source_sha256")
    ):
        return {
            **staged,
            "ok": False,
            "model_layers_written": False,
            "production_switch_allowed": False,
            "blockers": ["source_changed_after_staging"],
        }
    contract, contract_blockers, contract_sha256 = _load_timezone_contract(
        timezone_contract,
        source_sha256=verified_source[1],
    )
    if contract_blockers or contract_sha256 != staged.get("timezone_contract_sha256"):
        return {
            **staged,
            "ok": False,
            "model_layers_written": False,
            "production_switch_allowed": False,
            "blockers": list(
                dict.fromkeys(
                    [
                        *contract_blockers,
                        *(
                            ["timezone_contract_changed_after_staging"]
                            if contract_sha256
                            != staged.get("timezone_contract_sha256")
                            else []
                        ),
                    ]
                )
            ),
        }
    try:
        from index_integrity import IndexIntegrityError, reconcile_index

        index_reconciliation = reconcile_index(
            source,
            vault / "search_index.db",
            force_rebuild=True,
            timestamp_resolver=(
                _timezone_contract_resolver(contract) if contract is not None else None
            ),
        )
    except (IndexIntegrityError, OSError, sqlite3.DatabaseError):
        return {
            **staged,
            "ok": False,
            "model_layers_written": False,
            "production_switch_allowed": False,
            "blockers": ["strict_index_rebuild_failed"],
            "index_reconciliation": {"ok": False},
        }
    source_after_reconciliation = _secure_file_size_and_sha256(source)
    if (
        source_after_reconciliation is None
        or source_after_reconciliation[1] != staged.get("source_sha256")
    ):
        return {
            **staged,
            "ok": False,
            "model_layers_written": False,
            "production_switch_allowed": False,
            "blockers": ["source_changed_during_index_rebuild"],
            "index_reconciliation": index_reconciliation,
        }
    legacy_result: dict[str, Any] = {"ok": True, "created": 0, "skipped": 0}
    if (
        (vault / "reviewed/profile_memories.jsonl").is_file()
        or (vault / "profile_nuwa.json").is_file()
    ):
        from model_migration import migrate_legacy_profile

        legacy_result = migrate_legacy_profile(vault)
    living_self = build_living_self(vault)
    report = {
        "ok": True,
        "migration_ready": True,
        "production_switch_allowed": False,
        "blockers": ["production_prewarm_pending"],
        "model_layers_written": True,
        "index_staging": staged,
        "index_reconciliation": index_reconciliation,
        "claims_migration": legacy_result,
        "living_self_version_id": living_self["version_id"],
    }
    _write_private_json(vault / V11_MIGRATION_DIR / "migration-report.json", report)
    return report


def verify_index_verification(vault_dir: str | Path) -> dict[str, Any]:
    """Validate the exact source/main/WAL/SHM generation and classify receipt use."""
    from product_data import ProductDataError, ProductIndexIntegrity

    vault = vault_path(vault_dir).absolute()
    receipt_path = vault / "product/index-verification.json"
    before = _read_json_nofollow(receipt_path) if receipt_path.exists() else None
    started = time.perf_counter()
    try:
        with ProductIndexIntegrity(vault).trusted_connection():
            pass
    except (ProductDataError, OSError, ValueError):
        return {
            "ok": False,
            "mode": "failed",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "blockers": ["index_verification_failed"],
        }
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    after = _read_json_nofollow(receipt_path)
    if not after:
        return {
            "ok": False,
            "mode": "failed",
            "elapsed_ms": elapsed,
            "blockers": ["index_verification_receipt_missing"],
        }
    return {
        "ok": True,
        "mode": "receipt_hit" if before == after and before is not None else "generated",
        "elapsed_ms": elapsed,
        "identity": after,
        "source_sha256": sha256_file(vault / "index.jsonl"),
    }


def prewarm_index_verification(vault_dir: str | Path) -> dict[str, Any]:
    """Force one deep validation, then measure a new-process receipt hit."""
    from product_data import _exclusive_lock

    vault = vault_path(vault_dir).absolute()
    receipt_path = vault / "product/index-verification.json"
    lock_path = vault / "product/index-verification.lock"
    receipt_backup: dict[str, Any] | None = None
    if os.path.lexists(receipt_path):
        verified = _secure_file_size_and_sha256(receipt_path)
        try:
            metadata = os.lstat(receipt_path)
        except OSError:
            metadata = None
        if (
            verified is None
            or metadata is None
            or metadata.st_mode & 0o777 != 0o600
            or not _read_json_nofollow(receipt_path)
        ):
            return {
                "ok": False,
                "blockers": ["index_verification_receipt_unsafe"],
            }
        receipt_backup = _read_json_nofollow(receipt_path)
        with _exclusive_lock(lock_path, timeout=30.0, stale_after=60.0):
            current = _secure_file_size_and_sha256(receipt_path)
            if current != verified:
                return {
                    "ok": False,
                    "blockers": ["index_verification_receipt_changed"],
                }
            receipt_path.unlink()
    full = verify_index_verification(vault)
    if not full.get("ok") or full.get("mode") != "generated":
        if receipt_backup:
            _write_private_json(receipt_path, receipt_backup)
        return {
            "ok": False,
            "blockers": ["index_full_validation_failed"],
            "full_validation": full,
        }
    module_dir = str(Path(__file__).resolve().parent)
    script = (
        "import json,sys;"
        f"sys.path.insert(0,{module_dir!r});"
        "import export_restore;"
        "print(json.dumps(export_restore.verify_index_verification(sys.argv[1]),sort_keys=True))"
    )
    process = subprocess.run(
        [sys.executable, "-c", script, str(vault)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    try:
        fresh = json.loads(process.stdout) if process.returncode == 0 else {}
    except json.JSONDecodeError:
        fresh = {}
    if not isinstance(fresh, dict) or not fresh.get("ok") or fresh.get("mode") != "receipt_hit":
        return {
            "ok": False,
            "blockers": ["fresh_process_receipt_hit_failed"],
            "full_validation": full,
            "fresh_process": fresh,
        }
    return {
        "ok": True,
        "blockers": [],
        "full_validation": full,
        "fresh_process": fresh,
    }


def v11_production_switch_gate(
    vault_dir: str | Path,
    migration: dict[str, Any] | None,
    prewarm: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bind migration, published source/DB generation, and receipt hit exactly."""
    from product_data import INDEX_VERIFICATION_VERSION
    from index_integrity import INDEX_SCHEMA_VERSION

    vault = vault_path(vault_dir).absolute()
    staged = migration if isinstance(migration, dict) else {}
    if isinstance(staged.get("index_staging"), dict):
        staged = staged["index_staging"]
    warm = prewarm if isinstance(prewarm, dict) else {}
    full = warm.get("full_validation") if isinstance(warm.get("full_validation"), dict) else {}
    fresh = warm.get("fresh_process") if isinstance(warm.get("fresh_process"), dict) else {}
    blockers: list[str] = []
    if (
        not staged.get("ok")
        or staged.get("quarantined") != 0
        or not isinstance(staged.get("staging_sha256"), str)
        or not isinstance(staged.get("source_sha256"), str)
    ):
        blockers.append("migration_staging_not_ready")
    receipt_path = vault / V11_MIGRATION_DIR / "index-staging-receipt.json"
    supplied_receipt = Path(str(staged.get("receipt") or "")).expanduser().absolute()
    receipt_generation = (
        _read_private_json_generation(receipt_path, max_bytes=1024 * 1024)
        if supplied_receipt == receipt_path.absolute()
        else None
    )
    if receipt_generation is None:
        blockers.append("migration_staging_receipt_unsafe")
    else:
        receipt = receipt_generation[0]
        if (
            receipt.get("schema_version") != 1
            or receipt.get("source_index_sha256") != staged.get("source_sha256")
            or receipt.get("staging_sha256") != staged.get("staging_sha256")
            or receipt.get("quarantined") != staged.get("quarantined")
            or receipt.get("timezone_contract_sha256")
            != staged.get("timezone_contract_sha256")
        ):
            blockers.append("migration_staging_receipt_mismatch")
    if (
        not warm.get("ok")
        or full.get("mode") != "generated"
        or fresh.get("mode") != "receipt_hit"
        or not full.get("ok")
        or not fresh.get("ok")
    ):
        blockers.append("index_prewarm_not_ready")
    full_identity = full.get("identity") if isinstance(full.get("identity"), dict) else {}
    fresh_identity = fresh.get("identity") if isinstance(fresh.get("identity"), dict) else {}
    if not full_identity or full_identity != fresh_identity:
        blockers.append("index_receipt_identity_mismatch")
    if (
        full_identity.get("schema_version") != INDEX_SCHEMA_VERSION
        or full_identity.get("validation_version") != INDEX_VERIFICATION_VERSION
    ):
        blockers.append("index_receipt_schema_invalid")
    source_verified = _secure_file_size_and_sha256(vault / "index.jsonl")
    if source_verified is None:
        blockers.append("published_source_unsafe")
    else:
        source_stat = os.lstat(vault / "index.jsonl")
        source_signature = [
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_stat.st_ctime_ns,
        ]
        if (
            source_verified[1] != staged.get("source_sha256")
            or source_verified[1] != full.get("source_sha256")
            or source_verified[1] != fresh.get("source_sha256")
        ):
            blockers.append("published_source_generation_mismatch")
        if full_identity.get("source_signature") != source_signature:
            blockers.append("published_source_signature_mismatch")
    database_signatures: list[list[int] | None] = []
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(vault / "search_index.db") + suffix)
        if not os.path.lexists(path):
            database_signatures.append(None)
            continue
        verified_file = _secure_file_size_and_sha256(path)
        if verified_file is None:
            blockers.append("published_database_unsafe")
            database_signatures.append(None)
            continue
        metadata = os.lstat(path)
        database_signatures.append(
            [
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ]
        )
    if (
        not database_signatures
        or database_signatures[0] is None
        or full_identity.get("database_signature") != database_signatures
    ):
        blockers.append("published_database_generation_mismatch")
    blockers = list(dict.fromkeys(blockers))
    return {
        "ok": not blockers,
        "production_switch_allowed": not blockers,
        "blockers": blockers,
        "staging_sha256": staged.get("staging_sha256", ""),
        "receipt_identity": full_identity if not blockers else {},
    }


def resolve_export_path(export_path: str | Path) -> Path:
    path = Path(export_path).expanduser()
    if path.is_file() and path.name == MANIFEST_NAME:
        return path.parent
    return path


def _v11_projection_warnings(
    export_dir: Path,
    manifest: dict[str, Any],
) -> list[str]:
    schema_versions = manifest.get("schema_versions")
    if schema_versions is None:
        return []
    if schema_versions == {"model": 0, "events": 0}:
        return []
    if schema_versions != {"model": 1, "events": 1}:
        return ["v11_schema_versions_invalid"]
    required = [
        *V11_REQUIRED_EVENT_PATHS,
        *V11_REQUIRED_CURRENT_PATHS,
        "model/living-self/current.json",
        "model/living-self/current.md",
    ]
    missing = [relative for relative in required if not (export_dir / relative).is_file()]
    if missing:
        return ["v11_required_path_missing: " + relative for relative in missing]
    warnings: list[str] = []
    try:
        actual_heads = {
            name: _read_event_head_without_lock(export_dir / relative)
            for name, relative in V11_STREAM_PATHS.items()
        }
        if manifest.get("event_heads") != actual_heads:
            warnings.append("v11_event_heads_mismatch")
        expected_watermarks = {
            name: actual_heads[name] for name in ("claims", "judgments", "contexts")
        }
        if manifest.get("current_watermarks") != expected_watermarks:
            warnings.append("v11_current_watermarks_mismatch")

        with tempfile.TemporaryDirectory(
            prefix="immortal-v11-replay-",
            dir=str(Path(tempfile.gettempdir()).resolve()),
        ) as temp:
            replay_vault = Path(temp) / "vault"
            for directory in ("model/claims", "judgment", "contexts", "outcomes"):
                source_dir = export_dir / directory
                if source_dir.exists():
                    shutil.copytree(source_dir, replay_vault / directory)
            for relative in V11_REQUIRED_CURRENT_PATHS:
                (replay_vault / relative).unlink(missing_ok=True)

            from claim_store import ClaimStore
            from context_store import ContextStore
            from judgment_store import JudgmentStore
            from outcome_store import OutcomeStore

            ClaimStore(replay_vault)
            JudgmentStore(replay_vault)
            contexts = ContextStore(replay_vault)
            OutcomeStore(replay_vault, context_store=contexts).list()
            comparisons = {
                "claims": ["model/claims/current.jsonl"],
                "judgments": [
                    "judgment/current.jsonl",
                    "judgment/evaluations.jsonl",
                ],
                "contexts": ["contexts/current.jsonl"],
            }
            for name, paths in comparisons.items():
                if any(
                    (replay_vault / relative).read_bytes()
                    != (export_dir / relative).read_bytes()
                    for relative in paths
                ):
                    warnings.append("v11_projection_mismatch: " + name)
    except Exception:
        warnings.append("v11_event_replay_failed")
    return list(dict.fromkeys(warnings))


def _read_event_head_without_lock(path: Path) -> int:
    from event_store import validate_event

    descriptor = _open_absolute_nofollow(path)
    head = 0
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.endswith(b"\n") or len(raw) > 1024 * 1024:
                    raise ValueError("event stream framing is invalid")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("event stream row is invalid")
                validate_event(value)
                if value.get("seq") != line_number:
                    raise ValueError("event stream sequence is invalid")
                head = line_number
    finally:
        os.close(descriptor)
    return head


def restore_check(export_path: str | Path, strict: bool = False) -> dict[str, Any]:
    """Validate exported files against manifest size and sha256 metadata."""
    export_dir = resolve_export_path(export_path)
    manifest_path = export_dir / MANIFEST_NAME
    warnings: list[str] = []
    missing: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    checked_files = 0

    if not _secure_directory(export_dir):
        return {
            "ok": False,
            "export_dir": str(export_dir),
            "manifest_path": str(manifest_path),
            "checked_files": 0,
            "missing": [],
            "mismatched": [],
            "warnings": ["export_path_unsafe"],
        }
    manifest = _read_json_nofollow(manifest_path)
    if not manifest:
        return {
            "ok": False,
            "export_dir": str(export_dir),
            "manifest_path": str(manifest_path),
            "checked_files": 0,
            "missing": [],
            "mismatched": [],
            "warnings": ["manifest_missing_or_invalid"],
        }

    # manifest 自带的生成期 warning 必须进入校验结果：它们代表导出当时就已知的缺口
    for manifest_warning in manifest.get("warnings") or []:
        warnings.append(f"manifest_warning: {manifest_warning}")

    items = manifest.get("items")
    if not isinstance(items, list):
        items = []
        warnings.append("manifest_items_missing_or_invalid")

    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            warnings.append("invalid_item")
            continue
        relative = str(item.get("relpath") or "")
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            warnings.append(f"unsafe_relpath: {relative}")
            continue
        if relative in seen:
            warnings.append(f"duplicate_relpath: {relative}")
            continue
        seen.add(relative)

        path = export_dir / relative
        component = export_dir
        has_symlink = False
        for part in Path(relative).parts:
            component = component / part
            if component.is_symlink():
                has_symlink = True
                break
        if has_symlink:
            mismatched.append({"relpath": relative, "reason": "symlink_not_allowed"})
            continue
        if not path.exists() or not path.is_file():
            missing.append({"relpath": relative, "reason": "missing"})
            continue

        verified = _secure_file_size_and_sha256(path)
        if verified is None:
            mismatched.append({"relpath": relative, "reason": "unsafe_or_changed"})
            continue
        checked_files += 1
        actual_size, actual_hash = verified
        expected_size = item.get("size")
        expected_hash = item.get("sha256")
        problems: dict[str, Any] = {"relpath": relative}
        if expected_size != actual_size:
            problems["size"] = {"expected": expected_size, "actual": actual_size}
        if expected_hash != actual_hash:
            problems["sha256"] = {"expected": expected_hash, "actual": actual_hash}
        if len(problems) > 1:
            mismatched.append(problems)

    if strict:
        extra = []
        for path in sorted(export_dir.rglob("*")):
            if not path.is_file() or path.name == MANIFEST_NAME:
                continue
            relative = relpath(path, export_dir)
            if relative not in seen:
                extra.append(relative)
        if extra:
            warnings.append(f"extra_files: {len(extra)}")

    if not missing and not mismatched:
        warnings.extend(_v11_projection_warnings(export_dir, manifest))

    # strict 模式 fail-closed：任何 warning（unsafe/duplicate/invalid/extra/manifest 自带）都不允许成功，
    # 且必须逐一校验过 manifest 声明的每个安全条目
    strict_ok = not warnings and checked_files == len(seen)
    return {
        "ok": not missing and not mismatched and (not strict or strict_ok),
        "export_dir": str(export_dir),
        "manifest_path": str(manifest_path),
        "generated_at": manifest.get("generated_at", ""),
        "checked_files": checked_files,
        "expected_files": len(seen),
        "missing": missing,
        "mismatched": mismatched,
        "warnings": warnings,
    }


def _capture_export_generation(export_dir: Path) -> dict[str, Any] | None:
    """Capture the exact manifest and declared files as one comparable generation."""
    directory_identity = _secure_directory_identity(export_dir)
    manifest_generation = _read_file_generation(
        export_dir / MANIFEST_NAME,
        max_bytes=16 * 1024 * 1024,
        capture_body=True,
    )
    if directory_identity is None or manifest_generation is None:
        return None
    manifest_body, manifest_sha256, manifest_identity = manifest_generation
    if manifest_body is None:
        return None
    try:
        manifest = json.loads(manifest_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), list):
        return None
    files: dict[str, dict[str, Any]] = {}
    for item in manifest["items"]:
        if not isinstance(item, dict):
            return None
        relative = item.get("relpath")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or relative in files
        ):
            return None
        generation = _read_file_generation(export_dir / relative)
        if generation is None:
            return None
        _body, sha256, identity = generation
        files[relative] = {
            "sha256": sha256,
            "identity": identity,
        }
    return {
        "directory_identity": directory_identity,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "manifest_identity": manifest_identity,
        "files": files,
    }


def restore_export(
    export_path: str | Path,
    destination: str | Path,
    *,
    rebind_vault_config: bool = False,
) -> dict[str, Any]:
    """Restore through an anchored temporary tree, then atomically publish it."""
    export_dir = resolve_export_path(export_path).absolute()
    target = Path(destination).expanduser().absolute()
    generation = _capture_export_generation(export_dir)
    if generation is None:
        return {
            "ok": False,
            "destination": str(target),
            "blockers": ["export_generation_unsafe"],
        }
    check = restore_check(export_dir, strict=True)
    if not check.get("ok"):
        return {
            "ok": False,
            "destination": str(target),
            "blockers": ["strict_restore_check_failed"],
            "check": check,
        }
    if _capture_export_generation(export_dir) != generation:
        return {
            "ok": False,
            "destination": str(target),
            "blockers": ["export_generation_changed"],
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _secure_directory(target.parent):
        return {
            "ok": False,
            "destination": str(target),
            "blockers": ["restore_destination_unsafe"],
        }
    parent_fd = _open_absolute_nofollow(target.parent, directory=True)
    parent_metadata = os.fstat(parent_fd)
    temp_name = f".{target.name}.restore-{os.getpid()}-{time.time_ns()}"
    temp_created = False
    try:
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            os.close(parent_fd)
            return {
                "ok": False,
                "destination": str(target),
                "blockers": ["restore_destination_exists"],
            }
        os.mkdir(temp_name, mode=0o700, dir_fd=parent_fd)
        temp_created = True
        root_fd = os.open(
            temp_name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except Exception:
        if temp_created:
            try:
                _remove_tree_at(parent_fd, temp_name)
            except OSError:
                pass
        os.close(parent_fd)
        return {
            "ok": False,
            "destination": str(target),
            "blockers": ["restore_destination_unsafe"],
        }
    manifest = generation["manifest"]
    copied = 0
    temp_path = target.parent / temp_name
    try:
        for item in manifest["items"]:
            relative = str(item["relpath"])
            copied_generation = _copy_file_into_tree(
                export_dir / relative,
                root_fd,
                Path(relative),
            )
            if copied_generation != generation["files"][relative]:
                raise RuntimeError("export generation changed during copy")
            copied += 1
        copied_manifest = _copy_file_into_tree(
            export_dir / MANIFEST_NAME,
            root_fd,
            Path(MANIFEST_NAME),
        )
        if copied_manifest != {
            "sha256": generation["manifest_sha256"],
            "identity": generation["manifest_identity"],
        }:
            raise RuntimeError("manifest generation changed during copy")
        if _capture_export_generation(export_dir) != generation:
            raise RuntimeError("export generation changed after copy")
        os.fsync(root_fd)
        root_metadata = os.fstat(root_fd)
        if (
            _secure_directory_identity(temp_path)
            != (root_metadata.st_dev, root_metadata.st_ino)
            or _secure_directory_identity(target.parent)
            != (parent_metadata.st_dev, parent_metadata.st_ino)
        ):
            raise RuntimeError("restore destination identity changed")
        source_check = restore_check(temp_path, strict=True)
        if not source_check.get("ok"):
            raise RuntimeError("restored files failed strict verification")
        config_rebound = False
        derived_generation: dict[str, Any] | None = None
        if rebind_vault_config:
            config_path = temp_path / "config.json"
            config = _read_json_nofollow(config_path)
            source_config = generation["files"].get("config.json")
            if not config or not isinstance(source_config, dict):
                raise RuntimeError("restored config is missing or unsafe")
            config["vault_dir"] = str(target)
            _write_private_json(config_path, config)
            config_stat = config_path.stat()
            config_sha256 = sha256_file(config_path)
            receipt_path = temp_path / "restore-config-rebind-receipt.json"
            receipt = {
                "schema_version": 1,
                "kind": "vault_config_rebind",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "destination": str(target),
                "source_manifest_sha256": generation["manifest_sha256"],
                "source_config_sha256": source_config["sha256"],
                "derived_config_sha256": config_sha256,
            }
            _write_private_json(receipt_path, receipt)
            receipt_stat = receipt_path.stat()
            receipt_sha256 = sha256_file(receipt_path)
            derived_manifest = dict(manifest)
            derived_items = []
            config_bound = False
            for original in manifest["items"]:
                item = dict(original)
                if item.get("relpath") == "config.json":
                    item["size"] = config_stat.st_size
                    item["sha256"] = config_sha256
                    config_bound = True
                derived_items.append(item)
            if not config_bound:
                raise RuntimeError("restored manifest does not bind config.json")
            derived_items.append(
                {
                    "relpath": receipt_path.name,
                    "size": receipt_stat.st_size,
                    "sha256": receipt_sha256,
                }
            )
            derived_manifest["items"] = derived_items
            totals = dict(derived_manifest.get("totals") or {})
            totals["files"] = len(derived_items)
            totals["bytes"] = sum(int(item["size"]) for item in derived_items)
            derived_manifest["totals"] = totals
            derived_manifest["vault_dir"] = str(target)
            derived_manifest["export_dir"] = str(target)
            derived_manifest["derived_from"] = {
                "manifest_sha256": generation["manifest_sha256"],
                "operation": "vault_config_rebind",
            }
            _write_private_json(temp_path / MANIFEST_NAME, derived_manifest)
            derived_generation = {
                "manifest_sha256": sha256_file(temp_path / MANIFEST_NAME),
                "config_sha256": config_sha256,
                "receipt_sha256": receipt_sha256,
                "receipt": receipt_path.name,
            }
            config_rebound = True
        restored_check = restore_check(temp_path, strict=True)
        if not restored_check.get("ok"):
            raise RuntimeError("final restored generation failed strict verification")
        if (
            _secure_directory_identity(temp_path)
            != (root_metadata.st_dev, root_metadata.st_ino)
            or _secure_directory_identity(target.parent)
            != (parent_metadata.st_dev, parent_metadata.st_ino)
        ):
            raise RuntimeError("restore destination identity changed")
        os.rename(
            temp_name,
            target.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except Exception:
        try:
            _remove_tree_at(parent_fd, temp_name)
        except OSError:
            pass
        return {
            "ok": False,
            "destination": str(target),
            "blockers": ["restore_copy_or_verification_failed"],
            "copied_files": copied,
        }
    finally:
        os.close(root_fd)
        os.close(parent_fd)
    return {
        "ok": True,
        "destination": str(target),
        "copied_files": copied,
        "config_rebound": config_rebound,
        "source_check": source_check,
        "derived_generation": derived_generation,
        "check": restored_check,
    }


def _copy_file_into_tree(
    source: Path,
    root_fd: int,
    relative: Path,
) -> dict[str, Any]:
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("unsafe restore path")
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        return _copy_file_to_dirfd(source, directory_fd, parts[-1])
    finally:
        os.close(directory_fd)


def _copy_file_to_dirfd(
    source: Path,
    directory_fd: int,
    name: str,
) -> dict[str, Any]:
    source_fd = _open_absolute_nofollow(source)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("restore source is not a regular file")
        digest = hashlib.sha256()
        copied = 0
        destination_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("restore write made no progress")
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity or copied != before.st_size:
            raise OSError("restore source generation changed")
        return {"sha256": digest.hexdigest(), "identity": before_identity}
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        for child in os.listdir(child_fd):
            _remove_tree_at(child_fd, child)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def get_migration_backup_status(
    vault_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Strictly verify the exact export recorded by runtime state."""
    vault = vault_path(vault_dir)
    if not _secure_directory(vault):
        state = {}
    else:
        state = _read_json_nofollow(vault / "orchestrator_state.json")
    raw_export_dir = state.get("last_portable_export_dir") if state else None

    def failed(reason: str) -> dict[str, Any]:
        return {
            "ok": False,
            "trust_level": "failed",
            "generated_at": "",
            "storage_location": "unknown",
            "secret_scan": None,
            "warnings": [reason],
            "mode": "strict-sha256",
            "check": {
                "ok": False,
                "mode": "strict-sha256",
                "checked_files": 0,
                "warnings": [reason],
            },
        }

    if not isinstance(raw_export_dir, str) or not raw_export_dir.strip():
        return failed("state_export_missing")
    requested = Path(raw_export_dir).expanduser()
    if not requested.is_absolute() or not requested.name.startswith(EXPORT_PREFIX):
        return failed("state_export_path_invalid")
    export_identity = _secure_directory_identity(requested)
    if export_identity is None:
        return failed("state_export_path_missing")
    export_dir = _canonical_system_alias_path(requested)

    manifest = _read_json_nofollow(export_dir / MANIFEST_NAME)
    if not manifest:
        return failed("state_export_manifest_missing_or_invalid")
    manifest_export = manifest.get("export_dir")
    manifest_vault = manifest.get("vault_dir")
    manifest_export_path = (
        Path(manifest_export).expanduser()
        if isinstance(manifest_export, str)
        else None
    )
    manifest_vault_path = (
        Path(manifest_vault).expanduser()
        if isinstance(manifest_vault, str)
        else None
    )
    manifest_export_ok = bool(
        manifest_export_path is not None
        and manifest_export_path.is_absolute()
        and _secure_directory_identity(manifest_export_path) == export_identity
    )
    vault_identity = _secure_directory_identity(vault)
    manifest_vault_ok = bool(
        vault_identity is not None
        and manifest_vault_path is not None
        and manifest_vault_path.is_absolute()
        and _secure_directory_identity(manifest_vault_path) == vault_identity
    )

    check = restore_check(export_dir, strict=True)
    check["mode"] = "strict-sha256"
    validation_warnings: list[str] = []
    if not manifest_export_ok:
        validation_warnings.append("manifest_export_dir_mismatch")
    if not manifest_vault_ok:
        validation_warnings.append("manifest_vault_dir_mismatch")
    manifest_items = manifest.get("items")
    manifest_totals = manifest.get("totals")
    if (
        not isinstance(manifest_items, list)
        or not manifest_items
        or not isinstance(manifest_totals, dict)
        or manifest_totals.get("files") != len(manifest_items)
    ):
        validation_warnings.append("manifest_empty_or_inconsistent")
    state_generated = state.get("last_portable_export")
    manifest_generated = manifest.get("generated_at")
    if state_generated != manifest_generated:
        validation_warnings.append("state_manifest_timestamp_mismatch")
    if validation_warnings:
        check["ok"] = False
        check["warnings"] = [*(check.get("warnings") or []), *validation_warnings]

    return {
        "ok": bool(check.get("ok")),
        "trust_level": "verified" if check.get("ok") else "failed",
        "generated_at": manifest_generated or "",
        "storage_location": classify_storage_location(export_dir, vault),
        "secret_scan": manifest.get("secret_scan"),
        "warnings": check.get("warnings", []),
        "mode": "verified",
        "check": check,
        "latest_export": {
            "export_dir": str(export_dir),
            "generated_at": manifest_generated or "",
            "totals": manifest.get("totals", {}),
        },
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Immortal portable export and restore check")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("create-export")
    p_export.add_argument("--vault-dir")
    p_export.add_argument("--output-dir")
    p_export.add_argument("--include-raw", action="store_true")
    p_export.add_argument("--fail-on-secrets", action="store_true", help="Abort export when unredacted secret shapes are detected in index.jsonl")
    p_export.add_argument(
        "--redact-secrets",
        action="store_true",
        help="Redact detected credential shapes only in the exported index copy and write a hash-only receipt",
    )

    p_latest = sub.add_parser("latest")
    p_latest.add_argument("--vault-dir")

    p_status = sub.add_parser("status")
    p_status.add_argument("--vault-dir")
    p_status.add_argument("--verify", action="store_true")

    p_check = sub.add_parser("restore-check")
    p_check.add_argument("export_path")
    p_check.add_argument("--strict", action="store_true")

    p_restore = sub.add_parser("restore-export")
    p_restore.add_argument("export_path")
    p_restore.add_argument("destination")
    p_restore.add_argument(
        "--rebind-vault-config",
        action="store_true",
        help="Bind the restored config.json vault_dir to the destination",
    )

    p_stage_v11 = sub.add_parser("stage-v11-index")
    p_stage_v11.add_argument("--vault-dir", required=True)
    p_stage_v11.add_argument("--timezone-contract")
    p_stage_v11.add_argument("--output-dir")

    p_migrate_v11 = sub.add_parser("v11-migrate")
    p_migrate_v11.add_argument("--vault-dir", required=True)
    p_migrate_v11.add_argument("--timezone-contract")

    p_living_self = sub.add_parser("living-self-build")
    p_living_self.add_argument("--vault-dir", required=True)

    p_prewarm = sub.add_parser("prewarm-index-receipt")
    p_prewarm.add_argument("--vault-dir", required=True)

    args = parser.parse_args()
    if args.command == "create-export":
        try:
            print_json(
                create_export(
                    args.vault_dir,
                    args.output_dir,
                    args.include_raw,
                    fail_on_secrets=args.fail_on_secrets,
                    redact_secrets=args.redact_secrets,
                )
            )
        except SecretShapesFound as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "latest":
        print_json(find_latest_export(args.vault_dir))
        return 0
    if args.command == "status":
        status = get_backup_status(args.vault_dir, verify=args.verify)
        print_json(status)
        return 0 if status.get("ok") else 1
    if args.command == "restore-check":
        result = restore_check(args.export_path, args.strict)
        print_json(result)
        return 0 if result.get("ok") else 1
    if args.command == "restore-export":
        result = restore_export(
            args.export_path,
            args.destination,
            rebind_vault_config=args.rebind_vault_config,
        )
        print_json(result)
        return 0 if result.get("ok") else 1
    if args.command == "stage-v11-index":
        result = stage_v11_index(
            vault_path(args.vault_dir),
            timezone_contract=args.timezone_contract,
            output_dir=args.output_dir,
        )
        print_json(result)
        return 0 if result.get("ok") else 1
    if args.command == "v11-migrate":
        result = run_v11_migration(
            vault_path(args.vault_dir),
            timezone_contract=args.timezone_contract,
        )
        print_json(result)
        return 0 if result.get("ok") else 1
    if args.command == "living-self-build":
        result = build_living_self(vault_path(args.vault_dir))
        print_json(result)
        return 0 if result.get("ok") else 1
    if args.command == "prewarm-index-receipt":
        result = prewarm_index_verification(vault_path(args.vault_dir))
        print_json(result)
        return 0 if result.get("ok") else 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
