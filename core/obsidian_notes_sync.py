#!/usr/bin/env python3
"""CLI adapter for safe, bounded and recoverable Obsidian note ingestion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

from config import configured_vault_dir, load_config
from notes_ingestion import (
    NoteIngestionLimits,
    ingest_notes,
    read_ingestion_state,
)
from obsidian_sync import obsidian_config


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def configured_limits(
    config: dict[str, Any],
    *,
    max_files: Optional[int] = None,
    max_file_bytes: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
) -> NoteIngestionLimits:
    defaults = NoteIngestionLimits()
    obsidian = config.get("obsidian") if isinstance(config.get("obsidian"), dict) else {}
    raw = obsidian.get("notes_sync") if isinstance(obsidian.get("notes_sync"), dict) else {}
    return NoteIngestionLimits(
        max_files=_positive_int(
            max_files if max_files is not None else raw.get("max_files"),
            defaults.max_files,
        ),
        max_file_bytes=_positive_int(
            max_file_bytes if max_file_bytes is not None else raw.get("max_file_bytes"),
            defaults.max_file_bytes,
        ),
        max_total_bytes=_positive_int(
            max_total_bytes if max_total_bytes is not None else raw.get("max_total_bytes"),
            defaults.max_total_bytes,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest Markdown files from an Obsidian 笔记 directory"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sync_parser = sub.add_parser("sync", help="Scan and ingest notes")
    sync_parser.add_argument("--vault-path")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--json", action="store_true")
    sync_parser.add_argument("--max-files", type=int)
    sync_parser.add_argument("--max-file-bytes", type=int)
    sync_parser.add_argument("--max-total-bytes", type=int)
    status_parser = sub.add_parser(
        "status",
        help="Read the last persisted ingestion status",
    )
    status_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    vault_dir = configured_vault_dir(config)
    if args.command == "status":
        payload = read_ingestion_state(vault_dir)
    else:
        obsidian = obsidian_config(config)
        path = Path(args.vault_path or obsidian["vault_path"]).expanduser()
        payload = ingest_notes(
            vault_dir,
            path,
            dry_run=bool(args.dry_run),
            limits=configured_limits(
                config,
                max_files=args.max_files,
                max_file_bytes=args.max_file_bytes,
                max_total_bytes=args.max_total_bytes,
            ),
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"notes_sync={payload.get('status', 'unknown')}")
        if payload.get("error_code"):
            print(f"error_code={payload['error_code']}")
    return 1 if payload.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
