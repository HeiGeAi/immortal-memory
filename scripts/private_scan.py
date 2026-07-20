#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Iterable


DEFAULT_REGEXES = [
    ("openai_key", r"sk-[A-Za-z0-9_-]{20,}"),
    ("generic_token_assignment", r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|app[_-]?secret)\s*[:=]\s*['\"][^'\"]{12,}['\"]"),
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("lark_open_id", r"\bou_[0-9a-fA-F]{32}\b"),
    ("cookie_header", r"(?i)\bCookie\s*:\s*[^\r\n]{12,}"),
    ("bearer_token", r"(?i)\b(?:Authorization\s*:\s*)?Bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ("url_userinfo", r"\bhttps?://[^/\s:@]+:[^/\s@]{8,}@[^/\s]+"),
    ("absolute_home_path", r"(?:/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+)(?:[/\\][^\s'\"<>]*)?"),
]

DEFAULT_PRIVATE_LITERALS = [
    "Blake Xu",
    "小黑子",
    "徐将",
    "晓舟",
    "酱油",
    "小声比比",
    "田洋",
    "曹嵘",
    "com.blakexu.",
    "~/Desktop/claudecode",
    "heige-workbench",
]

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh", ".example"}
ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar", ".tar.gz")
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10000
MAX_ARCHIVE_DEPTH = 2


def iter_files(root: Path):
    if root.is_file() and not root.is_symlink():
        yield root
        return
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_symlink():
            continue
        if path.is_file() and (
            path.suffix in TEXT_SUFFIXES
            or path.name in {"LICENSE", ".gitignore"}
            or _is_archive(path.name)
        ):
            yield path


def _is_archive(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def _scan_text(
    text: str,
    *,
    path: str,
    member: str | None,
    include_private_literals: bool,
    extra_patterns: Iterable[str],
) -> list[dict[str, str | None]]:
    hits: list[dict[str, str | None]] = []
    for name, pattern in DEFAULT_REGEXES:
        if re.search(pattern, text):
            hits.append({"path": path, "member": member, "rule": name})
    if include_private_literals and any(marker in text for marker in DEFAULT_PRIVATE_LITERALS):
        hits.append({"path": path, "member": member, "rule": "private_identity_marker"})
    for pattern in extra_patterns:
        if pattern in text:
            hits.append({"path": path, "member": member, "rule": "private_environment_pattern"})
    return hits


def _read_bounded(handle: BinaryIO, declared_size: int | None = None) -> bytes:
    if declared_size is not None and declared_size > MAX_MEMBER_BYTES:
        raise ValueError("archive_member_too_large")
    payload = handle.read(MAX_MEMBER_BYTES + 1)
    if len(payload) > MAX_MEMBER_BYTES:
        raise ValueError("archive_member_too_large")
    return payload


def _scan_archive_bytes(
    payload: bytes,
    *,
    archive_name: str,
    display_path: str,
    extra_patterns: Iterable[str],
    depth: int,
) -> tuple[list[dict[str, str | None]], list[dict[str, str | None]]]:
    hits: list[dict[str, str | None]] = []
    errors: list[dict[str, str | None]] = []
    if depth > MAX_ARCHIVE_DEPTH:
        return hits, [{"path": display_path, "member": archive_name, "rule": "archive_depth_exceeded"}]

    total = 0
    try:
        if archive_name.lower().endswith((".zip", ".whl")):
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                members = archive.infolist()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive_member_limit_exceeded")
                for info in members:
                    if info.is_dir():
                        continue
                    total += info.file_size
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("archive_expanded_size_exceeded")
                    with archive.open(info, "r") as handle:
                        member_payload = _read_bounded(handle, info.file_size)
                    member_name = info.filename
                    if _is_archive(member_name):
                        nested_hits, nested_errors = _scan_archive_bytes(
                            member_payload,
                            archive_name=member_name,
                            display_path=display_path,
                            extra_patterns=extra_patterns,
                            depth=depth + 1,
                        )
                        for item in nested_hits + nested_errors:
                            item["member"] = f"{member_name}!{item.get('member') or ''}".rstrip("!")
                        hits.extend(nested_hits)
                        errors.extend(nested_errors)
                    else:
                        text = member_payload.decode("utf-8", errors="ignore")
                        hits.extend(
                            _scan_text(
                                text,
                                path=display_path,
                                member=member_name,
                                include_private_literals=True,
                                extra_patterns=extra_patterns,
                            )
                        )
        else:
            with tarfile.open(fileobj=BytesIO(payload), mode="r:*") as archive:
                members = archive.getmembers()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive_member_limit_exceeded")
                for info in members:
                    if not info.isfile():
                        continue
                    total += info.size
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("archive_expanded_size_exceeded")
                    extracted = archive.extractfile(info)
                    if extracted is None:
                        continue
                    with extracted:
                        member_payload = _read_bounded(extracted, info.size)
                    member_name = info.name
                    if _is_archive(member_name):
                        nested_hits, nested_errors = _scan_archive_bytes(
                            member_payload,
                            archive_name=member_name,
                            display_path=display_path,
                            extra_patterns=extra_patterns,
                            depth=depth + 1,
                        )
                        for item in nested_hits + nested_errors:
                            item["member"] = f"{member_name}!{item.get('member') or ''}".rstrip("!")
                        hits.extend(nested_hits)
                        errors.extend(nested_errors)
                    else:
                        text = member_payload.decode("utf-8", errors="ignore")
                        hits.extend(
                            _scan_text(
                                text,
                                path=display_path,
                                member=member_name,
                                include_private_literals=True,
                                extra_patterns=extra_patterns,
                            )
                        )
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
        errors.append({"path": display_path, "member": archive_name, "rule": str(exc)})
    return hits, errors


def scan_paths(paths: Iterable[str | Path], extra_patterns: Iterable[str] = ()) -> dict:
    hits: list[dict[str, str | None]] = []
    errors: list[dict[str, str | None]] = []
    scanner_path = Path(__file__).resolve()
    patterns = tuple(pattern for pattern in extra_patterns if pattern)
    for requested in paths:
        root = Path(requested).expanduser()
        for path in iter_files(root):
            display = str(path)
            if root.is_dir():
                try:
                    display = path.relative_to(root).as_posix()
                except ValueError:
                    pass
            try:
                if _is_archive(path.name):
                    size = path.stat().st_size
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("archive_file_too_large")
                    payload = path.read_bytes()
                    archive_hits, archive_errors = _scan_archive_bytes(
                        payload,
                        archive_name=path.name,
                        display_path=display,
                        extra_patterns=patterns,
                        depth=0,
                    )
                    hits.extend(archive_hits)
                    errors.extend(archive_errors)
                else:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    hits.extend(
                        _scan_text(
                            text,
                            path=display,
                            member=None,
                            include_private_literals=path.resolve() != scanner_path,
                            extra_patterns=patterns,
                        )
                    )
            except (OSError, ValueError) as exc:
                errors.append({"path": display, "member": None, "rule": str(exc)})
    return {"ok": not hits and not errors, "hits": hits, "errors": errors}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    extra = [item.strip() for item in os.environ.get("IMMORTAL_PRIVATE_PATTERNS", "").split(",") if item.strip()]
    result = scan_paths([root], extra)
    findings = [*result["hits"], *result["errors"]]
    if findings:
        for finding in findings:
            location = str(finding["path"])
            if finding.get("member"):
                location += f"!{finding['member']}"
            print(f"{location}: {finding['rule']}")
        return 2
    print("private_scan=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
