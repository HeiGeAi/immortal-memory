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
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


IMMORTAL_DIR = Path.home() / ".immortal"
EXPORTS_DIRNAME = "exports"
MANIFEST_NAME = "manifest.json"
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
    if root.is_file():
        return [root]
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
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


def create_export(
    vault_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    include_raw: bool = False,
    fail_on_secrets: bool = False,
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
    for source in files:
        relative = relpath(source, vault)
        target = export_dir / relative
        copy_file(source, target)
        item = file_item(target, export_dir)
        items.append(item)
        total_bytes += item["size"]

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
        }
        if report["unique_candidates"] > 0:
            warnings.append(
                f"secret_shapes_present: index.jsonl 含 {report['unique_candidates']} 个未脱敏凭证形态候选"
            )
            if fail_on_secrets:
                shutil.rmtree(export_dir, ignore_errors=True)
                raise SecretShapesFound(
                    f"export aborted: {report['unique_candidates']} unique secret-shape candidates in index.jsonl"
                )

    manifest = {
        "generated_at": iso_utc(),
        "vault_dir": str(vault),
        "export_dir": str(export_dir),
        "storage_location": location,
        "secret_scan": secret_summary,
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
        try:
            raw_secret_count = secret_scan.get("unique_candidates")
            if isinstance(raw_secret_count, bool):
                raise ValueError("boolean is not a candidate count")
            secret_count = int(raw_secret_count)
            if secret_count < 0:
                raise ValueError("negative candidate count")
        except (TypeError, ValueError):
            secret_scan_valid = False
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


def resolve_export_path(export_path: str | Path) -> Path:
    path = Path(export_path).expanduser()
    if path.is_file() and path.name == MANIFEST_NAME:
        return path.parent
    return path


def restore_check(export_path: str | Path, strict: bool = False) -> dict[str, Any]:
    """Validate exported files against manifest size and sha256 metadata."""
    export_dir = resolve_export_path(export_path)
    manifest_path = export_dir / MANIFEST_NAME
    warnings: list[str] = []
    missing: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    checked_files = 0

    manifest = read_manifest(manifest_path)
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

        checked_files += 1
        stat = path.stat()
        expected_size = item.get("size")
        expected_hash = item.get("sha256")
        problems: dict[str, Any] = {"relpath": relative}
        if expected_size != stat.st_size:
            problems["size"] = {"expected": expected_size, "actual": stat.st_size}
        actual_hash = sha256_file(path)
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


def get_migration_backup_status(
    vault_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Strictly verify the exact export recorded by runtime state."""
    vault = vault_path(vault_dir)
    state = read_manifest(vault / "orchestrator_state.json")
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
    if (
        not requested.is_absolute()
        or requested.is_symlink()
        or not requested.name.startswith(EXPORT_PREFIX)
    ):
        return failed("state_export_path_invalid")
    try:
        export_dir = requested.resolve(strict=True)
    except OSError:
        return failed("state_export_path_missing")
    if not export_dir.is_dir():
        return failed("state_export_path_invalid")

    manifest = read_manifest(export_dir / MANIFEST_NAME)
    if not manifest:
        return failed("state_export_manifest_missing_or_invalid")
    manifest_export = manifest.get("export_dir")
    manifest_vault = manifest.get("vault_dir")
    try:
        manifest_export_ok = (
            isinstance(manifest_export, str)
            and Path(manifest_export).expanduser().resolve(strict=True) == export_dir
        )
        manifest_vault_ok = (
            isinstance(manifest_vault, str)
            and Path(manifest_vault).expanduser().resolve(strict=True)
            == vault.resolve(strict=True)
        )
    except OSError:
        manifest_export_ok = False
        manifest_vault_ok = False

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

    p_latest = sub.add_parser("latest")
    p_latest.add_argument("--vault-dir")

    p_status = sub.add_parser("status")
    p_status.add_argument("--vault-dir")
    p_status.add_argument("--verify", action="store_true")

    p_check = sub.add_parser("restore-check")
    p_check.add_argument("export_path")
    p_check.add_argument("--strict", action="store_true")

    args = parser.parse_args()
    if args.command == "create-export":
        try:
            print_json(create_export(args.vault_dir, args.output_dir, args.include_raw, fail_on_secrets=args.fail_on_secrets))
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
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
