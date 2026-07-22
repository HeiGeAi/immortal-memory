#!/usr/bin/env python3
"""Hash-only 凭证形态扫描器：只输出模式名、位置、计数和不可逆哈希，绝不输出候选原文。

用途：
1. 修复前后对 index.jsonl / 导出文件做敏感形态基线对比（审计 P1-8）。
2. export 前的出口检查（export_restore 调用）。

输出承诺：报告中不包含 token 原文、可复原片段或完整上下文行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

# 检测模式与 redact_common 的替换模式同源，但这里只做识别不做替换。
# 键为模式名，值为编译好的正则。
DETECT_PATTERNS: dict[str, re.Pattern] = {
    "sk_key": re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    "github_token": re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b"),
    "github_pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "getnote_key": re.compile(r"\bgk_live_[A-Za-z0-9._\-]{10,}"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    "google_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}"),
    "url_credential": re.compile(r"https?://[^@\s/:]+:[^@\s/]+@"),
}

# 快速预筛子串：一行不含任何触发子串就跳过正则，让 1GB 级 index 扫描保持在分钟内
PREFILTER = ("sk-", "gh", "AKIA", "gk_live_", "xox", "AIza", "eyJ", "://")
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024


def value_hash(value: str) -> str:
    """不可逆哈希（截断 sha256），用于跨报告对比同一候选而不暴露原文。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def value_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_text_shapes(text: str) -> dict[str, int]:
    """Return high-confidence rule counts without returning matched values."""
    if not any(token in text for token in PREFILTER):
        return {}
    counts: dict[str, int] = {}
    for name, pattern in DETECT_PATTERNS.items():
        count = 0
        for match in pattern.finditer(text):
            value = match.group(0)
            if "[REDACTED" not in value:
                count += 1
        if count:
            counts[name] = count
    return dict(sorted(counts.items()))


def _iter_string_values(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _iter_string_values(item, child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_string_values(item, f"{path}[]")
    elif isinstance(value, str):
        yield path, value


def _redact_string(
    value: str,
    *,
    line_no: int,
    field: str,
    unique_hashes: dict[str, set[str]],
    by_pattern: dict[str, int],
    findings: list[dict[str, Any]],
) -> str:
    if not any(token in value for token in PREFILTER):
        return value
    redacted = value
    for name, pattern in DETECT_PATTERNS.items():
        def replace(match: re.Match) -> str:
            candidate = match.group(0)
            if "[REDACTED" in candidate:
                return candidate
            full_digest = value_sha256(candidate)
            digest = full_digest[:16]
            by_pattern[name] = by_pattern.get(name, 0) + 1
            if full_digest not in unique_hashes[name]:
                unique_hashes[name].add(full_digest)
                findings.append(
                    {
                        "pattern": name,
                        "first_seen_line": line_no,
                        "first_seen_field": field,
                        "value_sha256_16": digest,
                        "value_sha256": full_digest,
                        "value_length": len(candidate),
                    }
                )
            return f"[REDACTED:{name}:{digest}]"

        redacted = pattern.sub(replace, redacted)
    return redacted


def _redact_tree(
    value: Any,
    *,
    line_no: int,
    path: str,
    unique_hashes: dict[str, set[str]],
    by_pattern: dict[str, int],
    findings: list[dict[str, Any]],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_tree(
                item,
                line_no=line_no,
                path=f"{path}.{key}" if path else str(key),
                unique_hashes=unique_hashes,
                by_pattern=by_pattern,
                findings=findings,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_tree(
                item,
                line_no=line_no,
                path=f"{path}[]",
                unique_hashes=unique_hashes,
                by_pattern=by_pattern,
                findings=findings,
            )
            for item in value
        ]
    if isinstance(value, str):
        return _redact_string(
            value,
            line_no=line_no,
            field=path,
            unique_hashes=unique_hashes,
            by_pattern=by_pattern,
            findings=findings,
        )
    return value


def redact_jsonl_copy(source: Path, destination: Path) -> dict[str, Any]:
    """Create a deterministic redacted copy while leaving the authority untouched."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".redacting")
    if destination.exists() or temporary.exists():
        raise FileExistsError(destination)
    unique_hashes: dict[str, set[str]] = {name: set() for name in DETECT_PATTERNS}
    by_pattern: dict[str, int] = {}
    findings: list[dict[str, Any]] = []
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            line_no = 0
            while True:
                raw = reader.readline(MAX_JSONL_LINE_BYTES + 1)
                if not raw:
                    break
                line_no += 1
                if len(raw) > MAX_JSONL_LINE_BYTES:
                    raise ValueError(
                        f"index.jsonl line {line_no} exceeds {MAX_JSONL_LINE_BYTES} bytes; refusing secret redaction"
                    )
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        f"index.jsonl line {line_no} is not valid UTF-8; refusing secret redaction"
                    ) from exc
                if not any(token in line for token in PREFILTER):
                    writer.write(raw)
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"index.jsonl line {line_no} is invalid JSON; refusing secret redaction"
                    ) from exc
                redacted = _redact_tree(
                    record,
                    line_no=line_no,
                    path="",
                    unique_hashes=unique_hashes,
                    by_pattern=by_pattern,
                    findings=findings,
                )
                writer.write(
                    (json.dumps(redacted, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                )
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(source, temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "mode": "jsonl-value-redaction-v1",
        "source_file": source.name,
        "export_file": destination.name,
        "source_sha256": file_sha256(source),
        "export_sha256": file_sha256(destination),
        "source_bytes": source.stat().st_size,
        "export_bytes": destination.stat().st_size,
        "total_candidates": sum(by_pattern.values()),
        "unique_candidates": sum(len(values) for values in unique_hashes.values()),
        "by_pattern": dict(sorted(by_pattern.items())),
        "unique_by_pattern": {
            name: len(values)
            for name, values in sorted(unique_hashes.items())
            if values
        },
        "findings": findings,
    }


def scan_file(path: Path) -> dict[str, Any]:
    by_pattern: dict[str, int] = {}
    unique_hashes: dict[str, set] = {name: set() for name in DETECT_PATTERNS}
    findings: list[dict[str, Any]] = []
    total = 0
    invalid_json_lines: list[int] = []
    oversized_lines: list[int] = []
    with path.open("rb") as handle:
        line_no = 0
        while True:
            raw = handle.readline(MAX_JSONL_LINE_BYTES + 1)
            if not raw:
                break
            line_no += 1
            if len(raw) > MAX_JSONL_LINE_BYTES:
                oversized_lines.append(line_no)
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(MAX_JSONL_LINE_BYTES + 1)
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                invalid_json_lines.append(line_no)
                continue
            if not any(token in line for token in PREFILTER):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_json_lines.append(line_no)
                values = [("$raw", line)]
            else:
                values = _iter_string_values(record)
            for field, text in values:
                if not any(token in text for token in PREFILTER):
                    continue
                for name, pattern in DETECT_PATTERNS.items():
                    for match in pattern.finditer(text):
                        value = match.group(0)
                        if "[REDACTED" in value:
                            continue  # 已脱敏占位符不算候选
                        total += 1
                        by_pattern[name] = by_pattern.get(name, 0) + 1
                        full_digest = value_sha256(value)
                        digest = full_digest[:16]
                        if full_digest not in unique_hashes[name]:
                            unique_hashes[name].add(full_digest)
                            findings.append({
                                "pattern": name,
                                "first_seen_line": line_no,
                                "first_seen_field": field,
                                "value_sha256_16": digest,
                                "value_sha256": full_digest,
                                "value_length": len(value),
                            })
    return {
        "file": str(path),
        "total_candidates": total,
        "unique_candidates": sum(len(v) for v in unique_hashes.values()),
        "by_pattern": dict(sorted(by_pattern.items())),
        "unique_by_pattern": {k: len(v) for k, v in sorted(unique_hashes.items()) if v},
        "findings": findings,
        "invalid_json_lines": invalid_json_lines[:100],
        "invalid_json_line_count": len(invalid_json_lines),
        "oversized_lines": oversized_lines[:100],
        "oversized_line_count": len(oversized_lines),
        "scan_complete": not invalid_json_lines and not oversized_lines,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hash-only secret shape scanner (never prints raw values)")
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.path).expanduser()
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 1
    report = scan_file(path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"file: {report['file']}")
        print(f"total_candidates: {report['total_candidates']}")
        print(f"unique_candidates: {report['unique_candidates']}")
        for name, count in report["unique_by_pattern"].items():
            print(f"  {name}: {count} unique")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
