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
import re
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


def value_hash(value: str) -> str:
    """不可逆哈希（截断 sha256），用于跨报告对比同一候选而不暴露原文。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def scan_file(path: Path) -> dict[str, Any]:
    by_pattern: dict[str, int] = {}
    unique_hashes: dict[str, set] = {name: set() for name in DETECT_PATTERNS}
    findings: list[dict[str, Any]] = []
    total = 0
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line_no, line in enumerate(handle, 1):
            if not any(token in line for token in PREFILTER):
                continue
            for name, pattern in DETECT_PATTERNS.items():
                for match in pattern.findall(line):
                    # findall 对含组的模式返回组内容，统一转成完整字符串口径
                    value = match if isinstance(match, str) else "".join(match)
                    if "[REDACTED" in value:
                        continue  # 已脱敏占位符不算候选
                    total += 1
                    by_pattern[name] = by_pattern.get(name, 0) + 1
                    digest = value_hash(value)
                    if digest not in unique_hashes[name]:
                        unique_hashes[name].add(digest)
                        findings.append({
                            "pattern": name,
                            "first_seen_line": line_no,
                            "value_sha256_16": digest,
                            "value_length": len(value),
                        })
    return {
        "file": str(path),
        "total_candidates": total,
        "unique_candidates": sum(len(v) for v in unique_hashes.values()),
        "by_pattern": dict(sorted(by_pattern.items())),
        "unique_by_pattern": {k: len(v) for k, v in sorted(unique_hashes.items()) if v},
        "findings": findings,
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
