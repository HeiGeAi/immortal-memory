#!/usr/bin/env python3
"""Fail-closed compatibility boundary for the v1.1 Judgment Store.

The legacy private cards implementation is intentionally not shipped. Task 8
replaces this boundary with the event-backed public Judgment Store.
"""

from __future__ import annotations

import argparse


UNAVAILABLE = "not_available_until_v11"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Judgment Store compatibility command (available after the v1.1 migration)"
    )
    parser.add_argument("action", nargs="?", choices=("build", "list", "stats"), default="build")
    parser.add_argument("extra", nargs="?", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(
        f"cards={UNAVAILABLE} action={args.action} "
        "reason=event_backed_judgment_store_not_migrated"
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
