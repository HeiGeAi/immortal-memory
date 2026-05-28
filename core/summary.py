#!/usr/bin/env python3
"""
永生记忆库 — 每日摘要生成器
从归档数据中生成每日交互摘要，输出到 ~/.immortal/summaries/
"""

import json
import sys
import os
from typing import Optional
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter


IMMORTAL_DIR = Path.home() / ".immortal"
DAILY_DIR = IMMORTAL_DIR / "daily"
SUMMARIES_DIR = IMMORTAL_DIR / "summaries"


def load_daily_records(date: str) -> list:
    """加载指定日期的所有记录。"""
    records = []
    daily_file = DAILY_DIR / f"{date}.jsonl"
    if not daily_file.exists():
        return records
    with open(daily_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return records


def generate_summary(date: str, records: list) -> str:
    """根据记录生成结构化摘要。"""
    if not records:
        return f"# {date} 摘要\n\n无记录。"

    # 按数据源分类
    by_source = {}
    for r in records:
        source = r.get("source", "unknown")
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(r)

    lines = [f"# {date} 交互摘要", ""]

    # 总览
    total = len(records)
    user_msgs = sum(1 for r in records if r.get("role") == "user")
    assistant_msgs = sum(1 for r in records if r.get("role") == "assistant")
    system_msgs = sum(1 for r in records if r.get("role") == "system")
    sources = len(by_source)

    lines.append(f"**总记录**: {total} 条 | **用户消息**: {user_msgs} | **助手回复**: {assistant_msgs} | **系统/文件**: {system_msgs}")
    lines.append(f"**数据源**: {sources} 个")
    lines.append("")

    # 各数据源详情
    source_order = [
        ("claude-code-conversation", "Claude Code 对话"),
        ("codex-conversation", "Codex 对话"),
        ("hermes-conversation", "Hermes 对话"),
        ("claude-code-memory", "Claude 记忆"),
        ("codex-memory", "Codex 记忆"),
        ("hermes-memory", "Hermes 记忆"),
        ("claude-code-file-history", "Claude 文件历史"),
        ("claude-code-paste-cache", "Claude 粘贴输入"),
        ("claude-code-skill", "Claude Skill"),
        ("codex-skill", "Codex Skill"),
        ("hermes-skill", "Hermes Skill"),
        ("autoclaw-skill", "autoClaw Skill"),
        ("desktop-output", "桌面产出"),
        ("codex-output", "Codex 产出"),
    ]

    for source_key, display_name in source_order:
        if source_key not in by_source:
            continue
        source_records = by_source[source_key]
        lines.append(f"## {display_name} ({len(source_records)}条)")
        lines.append("")

        if "conversation" in source_key:
            # 对话类：提取用户说了什么
            sessions = {}
            for r in source_records:
                sid = r.get("session_id", "")[:8]
                if sid not in sessions:
                    sessions[sid] = []
                sessions[sid].append(r)

            lines.append(f"- **会话数**: {len(sessions)}")
            lines.append("")

            for sid, msgs in sessions.items():
                user_msgs_in_session = [m for m in msgs if m.get("role") == "user"]
                if user_msgs_in_session:
                    # 取第一条用户消息作为话题摘要
                    first_user = user_msgs_in_session[0].get("content", "")[:150].replace("\n", " ")
                    project = msgs[0].get("project", "").replace("~", "~")
                    lines.append(f"  - **会话 {sid}** ({project})")
                    lines.append(f"    {first_user}")
                    if len(user_msgs_in_session) > 1:
                        lines.append(f"    *(共 {len(user_msgs_in_session)} 轮对话)*")
                    lines.append("")

            # 使用过的工具
            all_tools = []
            for r in source_records:
                all_tools.extend(r.get("tools_used", []))
            if all_tools:
                tool_counts = Counter(all_tools)
                top_tools = tool_counts.most_common(8)
                lines.append(f"- **工具使用**: {', '.join(f'{t}({c})' for t, c in top_tools)}")
                lines.append("")

        elif "memory" in source_key:
            # 记忆类：列出文件
            for r in source_records:
                fname = r.get("file_name", "")
                size = r.get("file_size", 0)
                lines.append(f"  - {fname} ({size} bytes)")
            lines.append("")

        elif "skill" in source_key:
            # Skill 类：按 skill 分组
            skill_groups = {}
            for r in source_records:
                skill = r.get("skill_name", "unknown")
                if skill not in skill_groups:
                    skill_groups[skill] = 0
                skill_groups[skill] += 1
            for skill, count in sorted(skill_groups.items(), key=lambda x: -x[1]):
                lines.append(f"  - {skill}: {count}个文件")
            lines.append("")

        elif "file-history" in source_key:
            lines.append(f"  文件操作快照: {len(source_records)} 个版本")
            lines.append("")

        elif "output" in source_key:
            # 产出文件：按类型统计
            ext_groups = {}
            for r in source_records:
                fname = r.get("file_name", "")
                ext = Path(fname).suffix.lower() or "other"
                if ext not in ext_groups:
                    ext_groups[ext] = []
                ext_groups[ext].append(fname)
            for ext, files in sorted(ext_groups.items(), key=lambda x: -len(x[1]))[:6]:
                lines.append(f"  - {ext}: {len(files)}个文件")
            lines.append("")

        else:
            for r in source_records[:5]:
                fname = r.get("file_name", "")
                lines.append(f"  - {fname}")
            if len(source_records) > 5:
                lines.append(f"  - ... 等 {len(source_records) - 5} 个")
            lines.append("")

    return "\n".join(lines)


def generate_all_summaries(since: Optional[str] = None):
    """生成所有日期的摘要。"""
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    daily_files = sorted(DAILY_DIR.glob("*.jsonl"))
    generated = 0

    for daily_file in daily_files:
        date = daily_file.stem

        if since and date < since:
            continue

        summary_file = SUMMARIES_DIR / f"{date}.md"
        # 如果摘要已存在且比日文件新，跳过
        if summary_file.exists():
            if summary_file.stat().st_mtime > daily_file.stat().st_mtime:
                continue

        records = load_daily_records(date)
        if not records:
            continue

        summary = generate_summary(date, records)
        summary_file.write_text(summary, encoding="utf-8")
        generated += 1
        print(f"  {date}: {len(records)}条记录 -> 摘要已生成")

    return generated


def main():
    since = None
    if len(sys.argv) > 1 and sys.argv[1] == "--since":
        since = sys.argv[2] if len(sys.argv) > 2 else None
    elif len(sys.argv) > 1 and sys.argv[1] == "--date":
        date = sys.argv[2] if len(sys.argv) > 2 else None
        if date:
            records = load_daily_records(date)
            summary = generate_summary(date, records)
            print(summary)
            return

    print("生成每日摘要...")
    generated = generate_all_summaries(since)
    print(f"\n共生成 {generated} 个摘要文件")


if __name__ == "__main__":
    main()
