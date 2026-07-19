#!/usr/bin/env python3
"""Read-only fact-layer audit with aggregate output only."""

import argparse
import gzip
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, TextIO, Tuple

from file_utils import atomic_write_json, atomic_write_text
from index_integrity import IndexIntegrityError, report_index_integrity
from ranking_common import local_date


def _display_path(path: Path) -> str:
    value = str(path)
    home = str(Path.home())
    return "~" + value[len(home):] if value == home or value.startswith(home + "/") else value


def _open_lines(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def _empty_stats() -> Dict:
    return {
        "files": 0,
        "rows": 0,
        "valid_rows": 0,
        "malformed_rows": 0,
        "missing_id_rows": 0,
        "unique_ids": 0,
        "duplicate_ids": 0,
        "sources": {},
        "months": {},
    }


def _scan(paths: Iterable[Path]) -> Tuple[Dict, Set[str]]:
    stats = _empty_stats()
    ids: Set[str] = set()
    sources = Counter()
    months = Counter()
    for path in paths:
        stats["files"] += 1
        with _open_lines(path) as handle:
            for line in handle:
                stats["rows"] += 1
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    stats["malformed_rows"] += 1
                    continue
                if not isinstance(row, dict):
                    stats["malformed_rows"] += 1
                    continue
                stats["valid_rows"] += 1
                rec_id = str(row.get("id") or "")
                if not rec_id:
                    stats["missing_id_rows"] += 1
                elif rec_id in ids:
                    stats["duplicate_ids"] += 1
                else:
                    ids.add(rec_id)
                sources[str(row.get("source") or "unknown")] += 1
                day = local_date(str(row.get("timestamp") or ""))
                if day:
                    months[day[:7]] += 1
    stats["unique_ids"] = len(ids)
    stats["sources"] = dict(sorted(sources.items()))
    stats["months"] = dict(sorted(months.items()))
    return stats, ids


def _search_index_summary(index_file: Path, database: Path) -> Dict:
    try:
        report = report_index_integrity(index_file, database)
    except (IndexIntegrityError, OSError) as exc:
        return {
            "status": "error",
            "check_status": "error",
            "integrity_status": "error",
            "database_exists": database.exists(),
            "error": str(exc),
        }
    healthy = (
        bool(report["database_exists"])
        and report["reason"] == "in_sync"
        and int(report["missing_in_sqlite_count"]) == 0
        and int(report["missing_in_jsonl_count"]) == 0
    )
    integrity_status = "healthy" if healthy else "degraded"
    return {
        "status": integrity_status,
        "check_status": "ok",
        "integrity_status": integrity_status,
        "database_exists": bool(report["database_exists"]),
        "reason": report["reason"],
        "jsonl_unique_ids": int(report["jsonl_unique_ids"]),
        "sqlite_ids": int(report["sqlite_ids"]),
        "missing_in_sqlite_count": int(report["missing_in_sqlite_count"]),
        "missing_in_jsonl_count": int(report["missing_in_jsonl_count"]),
    }


def audit(
    daily_dir: Path,
    index_file: Path,
    search_database: Optional[Path] = None,
) -> Dict:
    daily_paths = []
    if daily_dir.exists():
        daily_paths = sorted(
            path
            for path in daily_dir.iterdir()
            if path.is_file()
            and (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz"))
        )
    index_paths = [index_file] if index_file.exists() else []
    daily_stats, daily_ids = _scan(daily_paths)
    index_stats, index_ids = _scan(index_paths)
    database = (
        Path(search_database)
        if search_database is not None
        else index_file.parent / "search_index.db"
    )
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "scope": {
            "daily_dir": _display_path(daily_dir),
            "index_file": _display_path(index_file),
            "content_included": False,
            "record_ids_included": False,
        },
        "daily": daily_stats,
        "index": index_stats,
        "comparison": {
            "shared_ids": len(daily_ids & index_ids),
            "daily_only_ids": len(daily_ids - index_ids),
            "index_only_ids": len(index_ids - daily_ids),
        },
        "search_index": _search_index_summary(index_file, database),
    }


def render_markdown(report: Dict) -> str:
    daily = report["daily"]
    index = report["index"]
    comparison = report["comparison"]
    search_index = report["search_index"]
    lines = [
        "# Immortal Fact-Layer Integrity Audit",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "This report contains aggregate counts only. No record bodies or raw record IDs are included.",
        "",
        "## Totals",
        "",
        "| Layer | Files | Valid rows | Malformed | Missing ID | Unique IDs | Duplicate IDs |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| daily | {daily['files']} | {daily['valid_rows']} | {daily['malformed_rows']} | {daily['missing_id_rows']} | {daily['unique_ids']} | {daily['duplicate_ids']} |",
        f"| index | {index['files']} | {index['valid_rows']} | {index['malformed_rows']} | {index['missing_id_rows']} | {index['unique_ids']} | {index['duplicate_ids']} |",
        "",
        "## Reconciliation",
        "",
        f"- Shared IDs: {comparison['shared_ids']}",
        f"- Daily-only IDs: {comparison['daily_only_ids']}",
        f"- Index-only IDs: {comparison['index_only_ids']}",
        "",
        "## Search index read model",
        "",
        f"- Check status: {search_index['check_status']}",
        f"- Integrity status: {search_index['integrity_status']}",
        f"- Database exists: {search_index['database_exists']}",
    ]
    if search_index["check_status"] == "ok":
        lines.extend(
            [
                f"- Reason: {search_index['reason']}",
                f"- JSONL unique IDs: {search_index['jsonl_unique_ids']}",
                f"- SQLite IDs: {search_index['sqlite_ids']}",
                f"- Missing in SQLite: {search_index['missing_in_sqlite_count']}",
                f"- Missing in JSONL: {search_index['missing_in_jsonl_count']}",
            ]
        )
    else:
        lines.append(f"- Error: {search_index['error']}")
    lines.extend(
        [
            "",
            "## Monthly coverage",
            "",
            "| Month | Daily rows | Index rows |",
            "|---|---:|---:|",
        ]
    )
    months = sorted(set(daily["months"]) | set(index["months"]))
    for month in months:
        lines.append(
            f"| {month} | {daily['months'].get(month, 0)} | {index['months'].get(month, 0)} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-dir", default=str(Path.home() / ".immortal"))
    parser.add_argument("--search-database")
    parser.add_argument("--json-output")
    parser.add_argument("--markdown-output")
    args = parser.parse_args(argv)
    vault = Path(args.vault_dir).expanduser()
    search_database = (
        Path(args.search_database).expanduser()
        if args.search_database
        else vault / "search_index.db"
    )
    report = audit(vault / "daily", vault / "index.jsonl", search_database)
    if args.json_output:
        atomic_write_json(Path(args.json_output).expanduser(), report)
    if args.markdown_output:
        atomic_write_text(Path(args.markdown_output).expanduser(), render_markdown(report))
    if not (args.json_output or args.markdown_output):
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
