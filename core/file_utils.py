#!/usr/bin/env python3
"""Crash-safe file replacement helpers for derived Immortal outputs."""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def normalize_platform_path(path) -> Path:
    """Return an absolute path with OS-level symlink prefixes resolved.

    macOS 的 /var、/tmp、/etc 是指向 /private 的符号链接（firmlink），
    逐组件 O_NOFOLLOW 打开会在这些组件上失败。这里只规范化操作系统级
    前缀，不跟随 vault 内部的符号链接，供 dir_fd 锚定入口统一使用。
    """
    absolute = os.path.abspath(os.fspath(path))
    if sys.platform == "darwin":
        for prefix in ("/var", "/tmp", "/etc"):
            if absolute == prefix or absolute.startswith(prefix + "/"):
                absolute = "/private" + absolute
                break
    return Path(absolute)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        temp_path = None
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
