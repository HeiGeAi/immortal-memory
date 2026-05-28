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
import shutil
from datetime import datetime, timezone
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
    "timeline.html",
    "dashboard.html",
    "brief",
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


def create_export(
    vault_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Create a portable export directory and return its manifest payload."""
    vault = vault_path(vault_dir)
    base_output = Path(output_dir).expanduser() if output_dir else vault / EXPORTS_DIRNAME
    export_dir = new_export_dir(base_output)
    warnings: list[str] = []

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

    manifest = {
        "generated_at": iso_utc(),
        "vault_dir": str(vault),
        "export_dir": str(export_dir),
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
        check = restore_check(latest["export_dir"], strict=False)
    else:
        check = {
            "ok": manifest_ok,
            "mode": "manifest-only",
            "checked_files": 0,
            "missing": [],
            "mismatched": [],
            "warnings": [] if manifest_ok else ["manifest_missing_or_invalid"],
        }
    return {
        "ok": bool(check.get("ok")),
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

    return {
        "ok": not missing and not mismatched and (not strict or not any(w.startswith("extra_files:") for w in warnings)),
        "export_dir": str(export_dir),
        "manifest_path": str(manifest_path),
        "generated_at": manifest.get("generated_at", ""),
        "checked_files": checked_files,
        "expected_files": len(seen),
        "missing": missing,
        "mismatched": mismatched,
        "warnings": warnings,
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
        print_json(create_export(args.vault_dir, args.output_dir, args.include_raw))
        return 0
    if args.command == "latest":
        print_json(find_latest_export(args.vault_dir))
        return 0
    if args.command == "status":
        print_json(get_backup_status(args.vault_dir, verify=args.verify))
        return 0
    if args.command == "restore-check":
        result = restore_check(args.export_path, args.strict)
        print_json(result)
        return 0 if result.get("ok") else 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
