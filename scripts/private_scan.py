#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


DEFAULT_REGEXES = [
    ("openai_key", r"sk-[A-Za-z0-9_-]{20,}"),
    ("generic_token_assignment", r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|app[_-]?secret)\s*[:=]\s*['\"][^'\"]{12,}['\"]"),
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh", ".example"}


def iter_files(root: Path):
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore"}):
            yield path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    extra = [item.strip() for item in os.environ.get("IMMORTAL_PRIVATE_PATTERNS", "").split(",") if item.strip()]
    hits = []
    for path in iter_files(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name, pattern in DEFAULT_REGEXES:
            if re.search(pattern, text):
                hits.append(f"{path.relative_to(root)}: {name}")
        for pattern in extra:
            if pattern in text:
                hits.append(f"{path.relative_to(root)}: {pattern}")
    if hits:
        print("\n".join(hits))
        return 2
    print("private_scan=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
