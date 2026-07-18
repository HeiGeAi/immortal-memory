#!/usr/bin/env python3
"""统一密钥/凭证脱敏 —— 永生记忆库所有"把原始记忆吐出去"的出口共用的单一真源。

背景：历史上 redact() 在 7 个文件里各有一份拷贝且模式漂移，只覆盖 sk-，
漏掉 GitHub PAT(ghp_)、AWS(AKIA)、Get笔记(gk_live_)、JWT、URL 内嵌凭证等，
导致 recall 实测能原样吐出明文 GitHub token。本模块收敛为单一真源。

用于：recall 预览、agent-context 注入、导出等所有外发出口。
原则：宁可多脱也不漏；只脱"看起来像凭证"的片段，不动正常正文。
"""

import re

_PATTERNS = [
    # 「键: 值」式
    (r"\bcli_[A-Za-z0-9_\-]{8,}\b", "cli_[REDACTED]"),
    (r"(?i)(app\s*secret\s*[:：=]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
    (r"(?i)(api[_\- ]?key\s*[:：=]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
    (r"(?i)(password\s*[:：=]?\s*)\S+", r"\1[REDACTED]"),
    (r"(?i)(密码\s*[:：=]?\s*)\S+", r"\1[REDACTED]"),
    (r"(?i)\b(token|bearer|secret)(\s*[:：=]\s*)\S{16,}", r"\1\2[REDACTED]"),
    # 厂商特征 token
    (r"sk-[A-Za-z0-9_\-]{12,}", "sk-[REDACTED]"),
    (r"gh[posru]_[A-Za-z0-9]{20,}", "gh_[REDACTED]"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "github_pat_[REDACTED]"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AKIA[REDACTED]"),
    (r"\bgk_live_[A-Za-z0-9._\-]{10,}", "gk_live_[REDACTED]"),
    (r"\bxox[baprs]-[A-Za-z0-9\-]{10,}", "xox-[REDACTED]"),
    (r"\bAIza[0-9A-Za-z_\-]{35}\b", "AIza[REDACTED]"),
    (r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}", "[REDACTED_JWT]"),
    # URL 内嵌凭证 https://user:pass@host
    (r"(https?://)[^@\s/:]+:[^@\s/]+@", r"\1[REDACTED]@"),
    # URL 内嵌单段凭证 https://<token>@host（token 当用户名、无冒号）
    (r"(https?://)[A-Za-z0-9_\-]{16,}@", r"\1[REDACTED]@"),
]

_COMPILED = [(re.compile(p), r) for p, r in _PATTERNS]


def redact(text):
    """对一段文本做凭证脱敏，返回脱敏后的文本。None/空原样返回。"""
    if not text:
        return text
    for rx, replacement in _COMPILED:
        text = rx.sub(replacement, text)
    return text


def redact_tree(value):
    """递归脱敏：str、list、tuple、dict 的所有字符串值（含嵌套 metadata、文件名）。

    数字、布尔、None 及其他类型原样返回；dict 的 key 不动（保持 schema 稳定）。
    这是所有写入路径（daily/index/export）的统一入口，堵住只脱顶层五字段的缺口。
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_tree(item) for item in value)
    if isinstance(value, dict):
        return {key: redact_tree(item) for key, item in value.items()}
    return value


if __name__ == "__main__":
    # 自检：这些都应被脱敏
    samples = [
        "ghp_" + ("example" * 6),
        "AKIA" + "IOSFODNN7EXAMPLE",
        "gk_live_" + "example1234567890.xyz",
        "token: ghp_" + ("0123456789abcdef" * 3),
        "https://" + "ghp_" + ("example" * 5) + "@github.com/example/repo.git",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgN",
        "正常正文：今天写了篇公众号文章，没有任何密钥。",
    ]
    for s in samples:
        print(redact(s))
