"""Console entry point for the packaged Immortal Memory runtime."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    runtime_dir = str(Path(__file__).resolve().parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)

    from immortal import main as immortal_main

    return immortal_main()
