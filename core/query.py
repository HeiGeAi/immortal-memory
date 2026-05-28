#!/usr/bin/env python3
"""
永生记忆库 — 查询工具
支持关键词搜索、时间范围过滤、数据源过滤
"""

import json
import sys
import re
from typing import Optional
from pathlib import Path
from datetime import datetime


IMMORTAL_DIR = Path.home() / ".immortal"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
DAILY_DIR = IMMORTAL_DIR / "daily"


def parse_date(date_str: str) -> str:
    """标准化日期输入。"""
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_str


def search_index(
    keyword: str,
    source: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """在索引文件中搜索记录。"""
    results = []
    keyword_lower = keyword.lower()

    if not INDEX_FILE.exists():
        return results

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            # 数据源过滤
            if source and record.get("source") != source:
                continue

            # 时间范围过滤
            ts = record.get("timestamp", "")
            date_str = ts[:10] if ts else ""
            if since and date_str < since:
                continue
            if until and date_str > until:
                continue

            # 关键词匹配
            content = record.get("content", "").lower()
            project = record.get("project", "").lower()
            if keyword_lower in content or keyword_lower in project:
                results.append(record)
                if len(results) >= limit:
                    break

    return results


def get_timeline(date: Optional[str] = None) -> dict:
    """获取时间线数据。"""
    timeline = {}

    if date:
        # 查看指定日期
        daily_file = DAILY_DIR / f"{date}.jsonl"
        if daily_file.exists():
            records = []
            with open(daily_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        records.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue
            timeline[date] = {
                "total": len(records),
                "user_msgs": sum(1 for r in records if r.get("role") == "user"),
                "assistant_msgs": sum(1 for r in records if r.get("role") == "assistant"),
                "projects": list(set(r.get("project", "") for r in records)),
                "tools": list(set(t for r in records for t in r.get("tools_used", []))),
            }
    else:
        # 列出所有日期
        for daily_file in sorted(DAILY_DIR.glob("*.jsonl")):
            d = daily_file.stem
            count = sum(1 for _ in open(daily_file, "r", encoding="utf-8"))
            timeline[d] = {"total": count}

    return timeline


def format_results(results: list[dict]) -> str:
    """格式化搜索结果。"""
    if not results:
        return "未找到匹配记录。"

    lines = []
    for i, r in enumerate(results, 1):
        ts = r.get("timestamp", "")[:19].replace("T", " ")
        source = r.get("source", "?")
        project = r.get("project", "").replace("~", "~")
        role = r.get("role", "?")
        content = r.get("content", "")
        # 截取预览
        preview = content[:200].replace("\n", " ")
        if len(content) > 200:
            preview += "..."

        role_icon = "U" if role == "user" else "A"
        lines.append(f"[{i}] {ts} [{source}] [{role_icon}] {project}")
        lines.append(f"    {preview}")
        if r.get("tools_used"):
            lines.append(f"    Tools: {', '.join(r['tools_used'][:5])}")
        lines.append("")

    return "\n".join(lines)


def format_timeline(timeline: dict) -> str:
    """格式化时间线。"""
    if not timeline:
        return "暂无归档数据。"

    lines = []
    for date, info in sorted(timeline.items(), reverse=True):
        total = info.get("total", 0)
        if "user_msgs" in info:
            # 详细模式
            lines.append(f"== {date} ==")
            lines.append(f"  总记录: {total} (用户: {info['user_msgs']}, 助手: {info['assistant_msgs']})")
            if info.get("projects"):
                for p in info["projects"][:5]:
                    lines.append(f"  项目: {p.replace('~', '~')}")
            if info.get("tools"):
                lines.append(f"  工具: {', '.join(info['tools'][:8])}")
        else:
            lines.append(f"  {date}: {total}条记录")

    return "\n".join(lines)


def show_status() -> str:
    """显示记忆库状态。"""
    lines = ["== 永生记忆库状态 =="]

    # 总记录数
    total = 0
    if INDEX_FILE.exists():
        total = sum(1 for _ in open(INDEX_FILE, "r", encoding="utf-8"))
    lines.append(f"总记录数: {total}")

    # 日期范围
    daily_files = sorted(DAILY_DIR.glob("*.jsonl"))
    if daily_files:
        earliest = daily_files[0].stem
        latest = daily_files[-1].stem
        lines.append(f"日期范围: {earliest} ~ {latest}")
        lines.append(f"归档天数: {len(daily_files)}")

    # 数据源状态
    sources_file = IMMORTAL_DIR / "sources.json"
    if sources_file.exists():
        with open(sources_file, "r") as f:
            config = json.load(f)
        lines.append("\n数据源:")
        for s in config.get("sources", []):
            status = "启用" if s.get("enabled") else "禁用"
            last = s.get("last_backup", "从未备份")
            if last and last != "从未备份":
                last = last[:19].replace("T", " ")
            lines.append(f"  {s['name']}: {status}, 最近备份: {last}")

    # 存储占用
    total_size = 0
    for f in IMMORTAL_DIR.rglob("*"):
        if f.is_file():
            total_size += f.stat().st_size
    if total_size > 1024 * 1024:
        lines.append(f"\n存储占用: {total_size / 1024 / 1024:.1f} MB")
    else:
        lines.append(f"\n存储占用: {total_size / 1024:.1f} KB")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print(show_status())
        return

    command = sys.argv[1]

    if command == "status":
        print(show_status())

    elif command == "query":
        if len(sys.argv) < 3:
            print("用法: query.py query <关键词> [--source <数据源>] [--since <日期>] [--until <日期>]")
            return
        keyword = sys.argv[2]
        source = None
        since = None
        until = None
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--source" and i + 1 < len(sys.argv):
                source = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--since" and i + 1 < len(sys.argv):
                since = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--until" and i + 1 < len(sys.argv):
                until = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        print(f"搜索: {keyword}")
        results = search_index(keyword, source=source, since=since, until=until)
        print(f"找到 {len(results)} 条结果:\n")
        print(format_results(results))

    elif command == "timeline":
        date = None
        if len(sys.argv) >= 3:
            date = sys.argv[2]
        timeline = get_timeline(date)
        print(format_timeline(timeline))

    else:
        print(f"未知命令: {command}")
        print("可用命令: status, query, timeline")


if __name__ == "__main__":
    main()
