"""Render CLI commands that work in both source and wheel installations."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = RUNTIME_DIR.parent


def cli_prefix() -> str:
    """Return a runnable command prefix for the current installation mode."""
    if (SOURCE_ROOT / "install.py").is_file():
        return "%s %s" % (
            shlex.quote(sys.executable),
            shlex.quote(str(RUNTIME_DIR / "immortal.py")),
        )
    return "immortal-memory"


def cli_command(*arguments: object) -> str:
    """Render a shell-safe command from already separated argv values."""
    parts = [cli_prefix()]
    parts.extend(shlex.quote(str(argument)) for argument in arguments)
    return " ".join(parts)
