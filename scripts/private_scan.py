#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
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
    ("absolute_home_path", r"(?:/Users/(?!example\b)[A-Za-z0-9][A-Za-z0-9._-]*|/home/(?!example\b)[A-Za-z0-9][A-Za-z0-9._-]*|[A-Za-z]:\\Users\\(?!example\b)[A-Za-z0-9][A-Za-z0-9._-]*)(?:[/\\][^\s'\"<>]*)?"),
]

PRIVATE_LITERAL_PARTS = [
    ("Blake", " Xu"),
    ("小黑", "子"),
    ("徐", "将"),
    ("晓", "舟"),
    ("酱", "油"),
    ("小声", "比比"),
    ("田", "洋"),
    ("曹", "嵘"),
    ("com.", "blakexu."),
    ("~/Desktop/", "claudecode"),
    ("heige-", "workbench"),
]
DEFAULT_PRIVATE_LITERALS = tuple("".join(parts) for parts in PRIVATE_LITERAL_PARTS)

SKIP_DIRS = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", "node_modules"}
ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar", ".tar.gz")
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10000
MAX_ARCHIVE_DEPTH = 2
DEFAULT_DEADLINE_SECONDS = 15.0


class ScanLimitError(RuntimeError):
    pass


@dataclass
class ScanBudget:
    max_members: int
    max_expanded_bytes: int
    deadline: float
    members: int = 0
    expanded_bytes: int = 0

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ScanLimitError("scan_deadline_exceeded")

    def consume_member(self, size: int) -> int:
        self.check_deadline()
        self.members += 1
        if self.members > self.max_members:
            raise ScanLimitError("archive_member_limit_exceeded")
        self.expanded_bytes += max(0, size)
        if self.expanded_bytes > self.max_expanded_bytes:
            raise ScanLimitError("archive_expanded_size_exceeded")
        return self.members


def iter_files(root: Path):
    if root.is_file() or root.is_symlink():
        yield root
        return
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_symlink():
            yield path
            continue
        if path.is_file():
            yield path


def _is_archive(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def _archive_kind(payload: bytes, name: str = "") -> str | None:
    stream = BytesIO(payload)
    if zipfile.is_zipfile(stream):
        return "zip"
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:*"):
            return "tar"
    except (tarfile.TarError, OSError):
        pass
    if _is_archive(name):
        return "zip" if name.lower().endswith((".zip", ".whl")) else "tar"
    return None


def _matching_rules(
    text: str,
    *,
    include_private_literals: bool,
    extra_patterns: Iterable[str],
) -> list[str]:
    rules = [name for name, pattern in DEFAULT_REGEXES if re.search(pattern, text)]
    if include_private_literals and any(marker in text for marker in DEFAULT_PRIVATE_LITERALS):
        rules.append("private_identity_marker")
    if any(pattern in text for pattern in extra_patterns):
        rules.append("private_environment_pattern")
    return list(dict.fromkeys(rules))


def _scan_text(
    text: str,
    *,
    path: str,
    member: str | None,
    member_sha256_16: str | None = None,
    include_private_literals: bool,
    extra_patterns: Iterable[str],
) -> list[dict[str, str | None]]:
    hits: list[dict[str, str | None]] = []
    for rule in _matching_rules(
        text,
        include_private_literals=include_private_literals,
        extra_patterns=extra_patterns,
    ):
        finding: dict[str, str | None] = {"path": path, "member": member, "rule": rule}
        if member_sha256_16:
            finding["member_sha256_16"] = member_sha256_16
        hits.append(finding)
    return hits


def _safe_member_identity(
    name: str,
    *,
    member_index: int,
    member_prefix: str,
    display_path: str,
    extra_patterns: Iterable[str],
) -> tuple[str, str | None, list[dict[str, str | None]]]:
    rules = _matching_rules(
        name,
        include_private_literals=True,
        extra_patterns=extra_patterns,
    )
    name_hash = hashlib.sha256(name.encode("utf-8", errors="ignore")).hexdigest()[:16]
    safe_name = f"member[{member_index}]" if rules else name
    member_ref = f"{member_prefix}!{safe_name}".lstrip("!")
    hits: list[dict[str, str | None]] = []
    for rule in rules:
        hits.append(
            {
                "path": display_path,
                "member": member_ref,
                "member_sha256_16": name_hash,
                "rule": rule,
            }
        )
    return member_ref, name_hash if rules else None, hits


def _read_bounded(handle: BinaryIO, declared_size: int | None = None) -> bytes:
    if declared_size is not None and declared_size > MAX_MEMBER_BYTES:
        raise ScanLimitError("archive_member_too_large")
    payload = handle.read(MAX_MEMBER_BYTES + 1)
    if len(payload) > MAX_MEMBER_BYTES:
        raise ScanLimitError("archive_member_too_large")
    return payload


def _scan_archive_bytes(
    payload: bytes,
    *,
    archive_kind: str,
    display_path: str,
    extra_patterns: Iterable[str],
    depth: int,
    budget: ScanBudget,
    member_prefix: str = "",
) -> tuple[list[dict[str, str | None]], list[dict[str, str | None]]]:
    hits: list[dict[str, str | None]] = []
    errors: list[dict[str, str | None]] = []
    if depth > MAX_ARCHIVE_DEPTH:
        return hits, [{"path": display_path, "member": member_prefix or None, "rule": "archive_depth_exceeded"}]

    try:
        budget.check_deadline()
        if archive_kind == "zip":
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                for info in archive.infolist():
                    member_index = budget.consume_member(info.file_size)
                    member_ref, name_hash, name_hits = _safe_member_identity(
                        info.filename,
                        member_index=member_index,
                        member_prefix=member_prefix,
                        display_path=display_path,
                        extra_patterns=extra_patterns,
                    )
                    hits.extend(name_hits)
                    if info.is_dir():
                        continue
                    unix_mode = (info.external_attr >> 16) & 0xFFFF
                    file_type = stat.S_IFMT(unix_mode)
                    unsafe_type = bool(
                        file_type
                        and file_type not in {stat.S_IFREG, stat.S_IFDIR}
                    )
                    with archive.open(info, "r") as handle:
                        member_payload = _read_bounded(handle, info.file_size)
                    if unsafe_type:
                        metadata_text = member_payload.decode("utf-8", errors="ignore")
                        metadata_hash = hashlib.sha256(member_payload).hexdigest()[:16]
                        hits.extend(
                            _scan_text(
                                metadata_text,
                                path=display_path,
                                member=member_ref,
                                member_sha256_16=metadata_hash,
                                include_private_literals=True,
                                extra_patterns=extra_patterns,
                            )
                        )
                        errors.append(
                            {
                                "path": display_path,
                                "member": member_ref,
                                "rule": "archive_non_regular_member",
                            }
                        )
                        continue
                    nested_kind = _archive_kind(member_payload, info.filename)
                    if nested_kind:
                        nested_hits, nested_errors = _scan_archive_bytes(
                            member_payload,
                            archive_kind=nested_kind,
                            display_path=display_path,
                            extra_patterns=extra_patterns,
                            depth=depth + 1,
                            budget=budget,
                            member_prefix=member_ref,
                        )
                        hits.extend(nested_hits)
                        errors.extend(nested_errors)
                    else:
                        text = member_payload.decode("utf-8", errors="ignore")
                        hits.extend(
                            _scan_text(
                                text,
                                path=display_path,
                                member=member_ref,
                                member_sha256_16=name_hash,
                                include_private_literals=True,
                                extra_patterns=extra_patterns,
                            )
                        )
        else:
            with tarfile.open(fileobj=BytesIO(payload), mode="r|*") as archive:
                for info in archive:
                    member_index = budget.consume_member(info.size)
                    member_ref, name_hash, name_hits = _safe_member_identity(
                        info.name,
                        member_index=member_index,
                        member_prefix=member_prefix,
                        display_path=display_path,
                        extra_patterns=extra_patterns,
                    )
                    hits.extend(name_hits)
                    if info.isdir():
                        continue
                    if not info.isfile():
                        link_target = str(info.linkname or "")
                        if link_target:
                            target_hash = hashlib.sha256(
                                link_target.encode("utf-8", errors="ignore")
                            ).hexdigest()[:16]
                            hits.extend(
                                _scan_text(
                                    link_target,
                                    path=display_path,
                                    member=member_ref,
                                    member_sha256_16=target_hash,
                                    include_private_literals=True,
                                    extra_patterns=extra_patterns,
                                )
                            )
                        errors.append(
                            {
                                "path": display_path,
                                "member": member_ref,
                                "rule": "archive_non_regular_member",
                            }
                        )
                        continue
                    extracted = archive.extractfile(info)
                    if extracted is None:
                        continue
                    with extracted:
                        member_payload = _read_bounded(extracted, info.size)
                    nested_kind = _archive_kind(member_payload, info.name)
                    if nested_kind:
                        nested_hits, nested_errors = _scan_archive_bytes(
                            member_payload,
                            archive_kind=nested_kind,
                            display_path=display_path,
                            extra_patterns=extra_patterns,
                            depth=depth + 1,
                            budget=budget,
                            member_prefix=member_ref,
                        )
                        hits.extend(nested_hits)
                        errors.extend(nested_errors)
                    else:
                        text = member_payload.decode("utf-8", errors="ignore")
                        hits.extend(
                            _scan_text(
                                text,
                                path=display_path,
                                member=member_ref,
                                member_sha256_16=name_hash,
                                include_private_literals=True,
                                extra_patterns=extra_patterns,
                            )
                        )
    except ScanLimitError as exc:
        errors.append({"path": display_path, "member": member_prefix or None, "rule": str(exc)})
    except RuntimeError:
        errors.append(
            {
                "path": display_path,
                "member": member_prefix or None,
                "rule": "archive_encrypted_or_unreadable",
            }
        )
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError):
        errors.append(
            {
                "path": display_path,
                "member": member_prefix or None,
                "rule": "archive_invalid_or_unreadable",
            }
        )
    return hits, errors


def scan_paths(
    paths: Iterable[str | Path],
    extra_patterns: Iterable[str] = (),
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> dict:
    hits: list[dict[str, str | None]] = []
    errors: list[dict[str, str | None]] = []
    scanner_path = Path(__file__).resolve()
    patterns = tuple(pattern for pattern in extra_patterns if pattern)
    requested_paths = list(paths)
    budget = ScanBudget(
        max_members=MAX_ARCHIVE_MEMBERS,
        max_expanded_bytes=MAX_ARCHIVE_BYTES,
        deadline=time.monotonic() + max(0.0, deadline_seconds),
    )
    scanned_files = 0
    if not requested_paths:
        errors.append({"path": "", "member": None, "rule": "no_scan_targets"})
    for requested in requested_paths:
        root = Path(requested).expanduser()
        if not root.exists() and not root.is_symlink():
            errors.append({"path": str(root), "member": None, "rule": "scan_target_missing"})
            continue
        root_is_directory = root.is_dir() and not root.is_symlink()
        for path in iter_files(root):
            scanned_files += 1
            raw_display = path.name
            if root_is_directory:
                try:
                    raw_display = path.relative_to(root).as_posix()
                except ValueError:
                    raw_display = path.name
            path_rules = _matching_rules(
                raw_display,
                include_private_literals=True,
                extra_patterns=patterns,
            )
            path_hash = hashlib.sha256(
                raw_display.encode("utf-8", errors="ignore")
            ).hexdigest()[:16]
            display = f"path[{scanned_files}]" if path_rules else raw_display
            for rule in path_rules:
                hits.append(
                    {
                        "path": display,
                        "path_sha256_16": path_hash,
                        "member": None,
                        "rule": rule,
                    }
                )
            try:
                budget.check_deadline()
                if path.is_symlink():
                    link_target = os.readlink(path)
                    target_hash = hashlib.sha256(
                        link_target.encode("utf-8", errors="ignore")
                    ).hexdigest()[:16]
                    for rule in _matching_rules(
                        link_target,
                        include_private_literals=True,
                        extra_patterns=patterns,
                    ):
                        hits.append(
                            {
                                "path": display,
                                "path_sha256_16": path_hash,
                                "metadata_sha256_16": target_hash,
                                "member": None,
                                "rule": rule,
                            }
                        )
                    continue
                size = path.stat().st_size
                if size > MAX_ARCHIVE_FILE_BYTES:
                    raise ScanLimitError("file_too_large")
                with path.open("rb") as handle:
                    payload = _read_bounded(handle) if size <= MAX_MEMBER_BYTES else handle.read(MAX_ARCHIVE_FILE_BYTES + 1)
                if len(payload) > MAX_ARCHIVE_FILE_BYTES:
                    raise ScanLimitError("archive_file_too_large")
                kind = _archive_kind(payload, path.name)
                if kind:
                    archive_hits, archive_errors = _scan_archive_bytes(
                        payload,
                        archive_kind=kind,
                        display_path=display,
                        extra_patterns=patterns,
                        depth=0,
                        budget=budget,
                    )
                    hits.extend(archive_hits)
                    errors.extend(archive_errors)
                else:
                    if size > MAX_MEMBER_BYTES:
                        raise ScanLimitError("file_too_large")
                    text = payload.decode("utf-8", errors="ignore")
                    hits.extend(
                        _scan_text(
                            text,
                            path=display,
                            member=None,
                            include_private_literals=path.resolve() != scanner_path,
                            extra_patterns=patterns,
                        )
                    )
            except ScanLimitError as exc:
                errors.append({"path": display, "member": None, "rule": str(exc)})
            except OSError:
                errors.append({"path": display, "member": None, "rule": "file_unreadable"})
    if scanned_files == 0 and not any(error["rule"] == "no_scan_targets" for error in errors):
        errors.append({"path": "", "member": None, "rule": "no_files_scanned"})
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
