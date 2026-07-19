#!/usr/bin/env python3
"""Ingest user-authored Markdown notes through a bounded, local-only command."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator

from config import configured_vault_dir, load_config
from obsidian_sync import obsidian_config
from web_capture import append_records, load_existing_dedup, now_iso


NOTES_SUBDIR = "笔记"
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)


def state_path(vault_dir: Path) -> Path:
    return vault_dir / "notes" / "state.json"


def note_files(notes_root: Path) -> Iterator[Path]:
    if not notes_root.is_dir():
        return
    for path in sorted(notes_root.rglob("*.md")):
        if not path.name.startswith("_") and path.is_file():
            yield path


def make_record(obsidian_vault: Path, path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    body = FRONTMATTER.sub("", text, count=1).strip()
    if not body:
        return None
    relative = path.relative_to(obsidian_vault).as_posix()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    dedup_key = f"obsidian-note|{relative}|{digest}"
    title = path.stem
    for line in body.splitlines():
        if line.strip().startswith("#"):
            title = line.strip().lstrip("#").strip() or title
            break
    return {
        "id": f"obsidian-note-{hashlib.sha256(dedup_key.encode()).hexdigest()[:20]}",
        "timestamp": now_iso(),
        "source": "obsidian-note",
        "type": "manual-note",
        "role": "user",
        "project": "notes",
        "title": title[:120],
        "content": body,
        "metadata": {"relative_path": relative, "dedup_key": dedup_key},
        "_dedup_key": dedup_key,
    }


def sync(vault_dir: Path, obsidian_vault: Path, *, dry_run: bool) -> dict[str, Any]:
    existing = load_existing_dedup(vault_dir, "obsidian-note")
    records: list[dict[str, Any]] = []
    scanned = 0
    for path in note_files(obsidian_vault / NOTES_SUBDIR):
        scanned += 1
        record = make_record(obsidian_vault, path)
        if record and record["_dedup_key"] not in existing:
            records.append(record)
    if records and not dry_run:
        append_records(vault_dir, records)
    result = {
        "status": "ok",
        "dry_run": dry_run,
        "generated_at": now_iso(),
        "totals": {
            "scanned": scanned,
            "planned_this_run": len(records),
            "ingested_this_run": 0 if dry_run else len(records),
        },
    }
    if not dry_run:
        target = state_path(vault_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest Markdown files from an Obsidian 笔记 directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sync_parser = sub.add_parser("sync", help="Scan and ingest notes")
    sync_parser.add_argument("--vault-path")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--json", action="store_true")
    status_parser = sub.add_parser("status", help="Read the last persisted ingestion status")
    status_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    vault_dir = configured_vault_dir(config)
    if args.command == "status":
        path = state_path(vault_dir)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"status": "missing"}
    else:
        obsidian = obsidian_config(config)
        path = Path(args.vault_path or obsidian["vault_path"]).expanduser()
        payload = sync(vault_dir, path, dry_run=bool(args.dry_run))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"notes_sync={payload.get('status', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
