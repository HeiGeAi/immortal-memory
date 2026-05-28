#!/usr/bin/env python3
"""Merge manually approved profile candidates into the reviewed memory layer.

This script is deliberately conservative. It only accepts candidates whose
checkbox is marked in the review Markdown and whose memory_id exists in the
structured Feishu profile memories JSONL.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


HOME = Path.home()
IMMORTAL_DIR = HOME / ".immortal"
DEFAULT_PROPOSAL = IMMORTAL_DIR / "feishu" / "distilled" / "profile_merge_proposal.md"
DEFAULT_PROFILE_MEMORIES = IMMORTAL_DIR / "feishu" / "distilled" / "profile_memories.jsonl"
DEFAULT_REVIEWED_DIR = IMMORTAL_DIR / "reviewed"
DEFAULT_REVIEWED_FILE = DEFAULT_REVIEWED_DIR / "profile_memories.jsonl"
DEFAULT_REVIEWED_MD = DEFAULT_REVIEWED_DIR / "profile_memories.md"
DEFAULT_LOG = DEFAULT_REVIEWED_DIR / "profile_merge_log.jsonl"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

CHECKED_LINE_RE = re.compile(r"^-\s*\[[xX]\]\s+(.*)$")
ID_RE = re.compile(r"(?:`|id:|memory_id:)?\b([a-f0-9]{24})\b`?")


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(dump_json(row) + "\n")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(dump_json(row) + "\n")


def parse_checked_ids(path: Path) -> tuple[list[str], list[str]]:
    checked: list[str] = []
    malformed: list[str] = []
    if not path.exists():
        return checked, [f"proposal file missing: {path}"]
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = CHECKED_LINE_RE.match(raw.strip())
        if not match:
            continue
        id_match = ID_RE.search(match.group(1))
        if not id_match:
            malformed.append(raw.strip())
            continue
        memory_id = id_match.group(1)
        if memory_id not in checked:
            checked.append(memory_id)
    return checked, malformed


def approval_copy(memory: dict[str, Any], proposal: Path, approved_at: str, approved_by: str) -> dict[str, Any]:
    row = dict(memory)
    row["review_state"] = "approved"
    row["approved_at"] = approved_at
    row["approved_by"] = approved_by
    row["approval_source"] = str(proposal)
    return row


def source_label(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    title = source.get("title") or "unknown source"
    valid_from = row.get("valid_from") or ""
    return f"{title} / {valid_from}".strip(" /")


def render_reviewed_md(rows: list[dict[str, Any]]) -> str:
    focus_titles = {
        "self_profile": "个人长期画像",
        "current_project": "当前项目长期记忆",
        "company_context": "公司与内容业务",
        "other": "其他",
    }
    type_titles = {
        "preference": "偏好与原则",
        "decision": "决策",
        "lesson": "经验教训",
        "relationship": "关系与职责",
        "project_fact": "项目事实",
        "commitment": "承诺与待办",
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("focus", "other"), row.get("memory_type", "other"))].append(row)

    lines = [
        "# 自动长期画像记忆",
        "",
        "这些条目来自后台自动判断或少量调试覆盖，属于长期画像的高可信增量层。",
        "",
    ]
    for focus in ["self_profile", "current_project", "company_context", "other"]:
        wrote_focus = False
        for memory_type in ["preference", "decision", "lesson", "relationship", "project_fact", "commitment", "other"]:
            rows_for_type = grouped.get((focus, memory_type), [])
            if not rows_for_type:
                continue
            if not wrote_focus:
                lines.append(f"## {focus_titles.get(focus, focus)}")
                lines.append("")
                wrote_focus = True
            lines.append(f"### {type_titles.get(memory_type, memory_type)}")
            lines.append("")
            for row in sorted(rows_for_type, key=lambda item: (item.get("valid_from") or "", item.get("statement") or "")):
                lines.append(f"- `{row.get('memory_id')}` {row.get('statement')}")
                lines.append(f"  - source: {source_label(row)}")
                lines.append(
                    f"  - confidence: {row.get('confidence')} / relevance: {row.get('relevance_score')} / approved: {row.get('approved_at')}"
                )
            lines.append("")
    if len(lines) <= 4:
        lines.append("暂无确认条目。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge checked profile candidates into ~/.immortal/reviewed")
    parser.add_argument("--proposal", type=Path, default=DEFAULT_PROPOSAL)
    parser.add_argument("--memories", type=Path, default=DEFAULT_PROFILE_MEMORIES)
    parser.add_argument("--reviewed-file", type=Path, default=DEFAULT_REVIEWED_FILE)
    parser.add_argument("--reviewed-md", type=Path, default=DEFAULT_REVIEWED_MD)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--approved-by", default="manual_checkbox")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checked_ids, malformed = parse_checked_ids(args.proposal)
    candidate_rows = load_jsonl(args.memories)
    reviewed_rows = load_jsonl(args.reviewed_file)
    by_id = {row.get("memory_id"): row for row in candidate_rows if row.get("memory_id")}
    existing_ids = {row.get("memory_id") for row in reviewed_rows if row.get("memory_id")}

    missing = [memory_id for memory_id in checked_ids if memory_id not in by_id]
    approved_at = now_local()
    new_rows = [
        approval_copy(by_id[memory_id], args.proposal, approved_at, args.approved_by)
        for memory_id in checked_ids
        if memory_id in by_id and memory_id not in existing_ids
    ]
    merged_rows = reviewed_rows + new_rows

    if not args.dry_run:
        write_jsonl_atomic(args.reviewed_file, merged_rows)
        args.reviewed_md.parent.mkdir(parents=True, exist_ok=True)
        args.reviewed_md.write_text(render_reviewed_md(merged_rows), encoding="utf-8")
        append_jsonl(
            args.log,
            {
                "timestamp": approved_at,
                "proposal": str(args.proposal),
                "memories": str(args.memories),
                "approved_by": args.approved_by,
                "checked": checked_ids,
                "added": [row.get("memory_id") for row in new_rows],
                "already_present": sorted(existing_ids.intersection(checked_ids)),
                "missing": missing,
                "malformed_checked_lines": malformed,
            },
        )

    print(f"checked={len(checked_ids)}")
    print(f"added={len(new_rows)}")
    print(f"already_present={len(existing_ids.intersection(checked_ids))}")
    print(f"missing={len(missing)}")
    print(f"malformed={len(malformed)}")
    print(f"reviewed_file={args.reviewed_file}")
    print(f"reviewed_md={args.reviewed_md}")
    if missing:
        print("Missing IDs:")
        for memory_id in missing[:20]:
            print(f"  {memory_id}")
    if malformed:
        print("Malformed checked lines:")
        for line in malformed[:10]:
            print(f"  {line[:180]}")
    return 1 if missing or malformed else 0


if __name__ == "__main__":
    raise SystemExit(main())
