#!/usr/bin/env python3
"""Bridge files and task-local context packs for external agents."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import configured_vault_dir, owner_display_name


SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = configured_vault_dir()
AGENT_DIR = IMMORTAL_DIR / "agent"
ENTRY_MD = AGENT_DIR / "ENTRY.md"
ENTRY_JSON = AGENT_DIR / "entry.json"
LATEST_CONTEXT_MD = AGENT_DIR / "latest-context.md"
LATEST_CONTEXT_JSON = AGENT_DIR / "latest-context.json"
CLAUDE_PROMPT = AGENT_DIR / "claude-code-prompt.txt"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_text(path: Path, max_chars: int = 6000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""
    return text[:max_chars].rstrip()


def file_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "bytes": 0, "modified_at": ""}
    return {
        "path": str(path),
        "exists": True,
        "bytes": path.stat().st_size,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ).isoformat(timespec="seconds"),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def bridge_metadata() -> dict[str, Any]:
    state = read_json(IMMORTAL_DIR / "orchestrator_state.json", {})
    quality = read_json(IMMORTAL_DIR / "quality" / "latest.json", {})
    product = read_json(IMMORTAL_DIR / "product" / "goal.json", {})
    return {
        "generated_at": now_local(),
        "owner": owner_display_name(),
        "total_records": state.get("total_records"),
        "last_collect": state.get("last_collect"),
        "last_feishu_collect": state.get("last_feishu_collect"),
        "quality_status": quality.get("status"),
        "quality_score": quality.get("score"),
        "quality_issues": quality.get("issue_count"),
        "product_goal": product.get("one_sentence"),
        "paths": {
            "entry_md": str(ENTRY_MD),
            "latest_context_md": str(LATEST_CONTEXT_MD),
            "profile_compact": str(IMMORTAL_DIR / "profile_compact.md"),
            "profile_nuwa": str(IMMORTAL_DIR / "profile_nuwa.md"),
            "people_index": str(IMMORTAL_DIR / "people" / "people_index.md"),
            "digest": str(IMMORTAL_DIR / "digests" / "latest.md"),
            "product_goal": str(IMMORTAL_DIR / "product" / "goal.md"),
        },
        "commands": {
            "health": f"python3 {SKILL_DIR / 'immortal.py'} health --max-age-hours 72",
            "agent_context": f"python3 {SKILL_DIR / 'immortal.py'} agent-context \"<当前任务>\" --print",
            "recall": f"python3 {SKILL_DIR / 'immortal.py'} recall \"<主题>\"",
            "context": f"python3 {SKILL_DIR / 'immortal.py'} context \"<当前任务>\"",
        },
        "local_urls": {
            "dashboard": "http://127.0.0.1:8765/",
            "agent_entry": "http://127.0.0.1:8765/agent-entry",
            "agent_factory": "http://127.0.0.1:8765/agent-factory",
        },
    }


def render_entry(meta: dict[str, Any]) -> str:
    owner = meta.get("owner") or "the owner"
    command = meta["commands"]["agent_context"]
    one_sentence = (
        f"请先读取 {ENTRY_MD}，然后针对当前任务运行 "
        f"`python3 {SKILL_DIR / 'immortal.py'} agent-context \"<当前任务>\" --print`，"
        f"把返回内容作为理解{owner}的长期上下文。"
    )
    claude_prompt = (
        f"你现在可以调用{owner}的永生知识库。开始任务前先运行："
        f"`python3 {SKILL_DIR / 'immortal.py'} agent-context \"本次任务\" --print`，"
        "再基于返回的画像、偏好、近期状态和证据继续工作。"
    )
    lines = [
        "# Immortal Agent Entry",
        "",
        f"Generated: {meta.get('generated_at')}",
        f"Owner: {owner}",
        "",
        "## 一句话接入",
        one_sentence,
        "",
        "## Claude Code 直接复制",
        claude_prompt,
        "",
        "## 当前可信状态",
        f"- total_records: {meta.get('total_records')}",
        f"- quality: {meta.get('quality_status')} / {meta.get('quality_score')} / issues={meta.get('quality_issues')}",
        f"- last_collect: {meta.get('last_collect')}",
        f"- last_feishu_collect: {meta.get('last_feishu_collect')}",
        "",
        "## Agent 必须遵守",
        "- 开始任务前先生成 task-local context，不要直接读取完整原始库。",
        "- 只把返回内容当作工作上下文，不要把它当成不可质疑的人设。",
        "- 涉及具体事实、承诺、人物关系时，用 recall 或 evidence 再核一次。",
        "- 不输出密钥、私聊原文、客户隐私；只输出任务需要的结论。",
        "- 可以代理表达风格、偏好和决策启发式，不能声称完整替代本人。",
        "",
        "## 稳定命令",
        f"- health: `{meta['commands']['health']}`",
        f"- agent-context: `{command}`",
        f"- recall: `{meta['commands']['recall']}`",
        f"- raw context: `{meta['commands']['context']}`",
        "",
        "## 稳定文件",
    ]
    for key, value in meta["paths"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## 本地链接"])
    for key, value in meta["local_urls"].items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines).rstrip() + "\n"


def command_entry(_args: argparse.Namespace) -> int:
    meta = bridge_metadata()
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    ENTRY_MD.write_text(render_entry(meta), encoding="utf-8")
    write_json(ENTRY_JSON, meta)
    claude_prompt = (
        f"你现在可以调用{meta.get('owner')}的永生知识库。"
        f"开始任务前运行：python3 {SKILL_DIR / 'immortal.py'} agent-context \"本次任务\" --print"
    )
    CLAUDE_PROMPT.write_text(claude_prompt + "\n", encoding="utf-8")
    print(f"entry_md={ENTRY_MD}")
    print(f"entry_json={ENTRY_JSON}")
    print(f"claude_prompt={CLAUDE_PROMPT}")
    print(f"agent_entry_url=http://127.0.0.1:8765/agent-entry")
    return 0


def command_context(args: argparse.Namespace) -> int:
    query = (args.query or "当前任务").strip()
    cmd = [sys.executable, str(SKILL_DIR / "immortal.py"), "context", query, "--since", args.since]
    if args.with_recall:
        cmd.append("--with-recall")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SKILL_DIR), timeout=args.timeout)
    body = result.stdout.strip()
    if result.stderr.strip():
        body = body + "\n\nSTDERR:\n" + result.stderr.strip()
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    header = [
        "# Immortal Task Context",
        "",
        f"Generated: {now_local()}",
        f"Query: {query}",
        f"Exit code: {result.returncode}",
        "",
        "Use this as task-local context. Do not paste raw vault data.",
        "",
        "---",
        "",
    ]
    content = "\n".join(header) + body + "\n"
    output = Path(args.output).expanduser() if args.output else LATEST_CONTEXT_MD
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    payload = {
        "generated_at": now_local(),
        "query": query,
        "exit_code": result.returncode,
        "context_md": str(output),
        "entry_md": str(ENTRY_MD),
        "files": {
            "context": file_info(output),
            "entry": file_info(ENTRY_MD),
        },
    }
    write_json(LATEST_CONTEXT_JSON, payload)
    print(f"context_md={output}")
    print(f"context_json={LATEST_CONTEXT_JSON}")
    if args.print:
        print()
        print(content)
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create external-agent bridge files and task-local context packs")
    sub = parser.add_subparsers(dest="command")
    entry = sub.add_parser("entry", help="Write the stable external-agent entry file")
    entry.set_defaults(func=command_entry)
    context = sub.add_parser("context", help="Write a task-local context pack for another agent")
    context.add_argument("query", nargs="?", default="当前任务")
    context.add_argument("--since", default="2026-03-01")
    context.add_argument("--with-recall", action="store_true")
    context.add_argument("--output", default="")
    context.add_argument("--timeout", type=int, default=240)
    context.add_argument("--print", action="store_true", help="Also print the generated context to stdout")
    context.set_defaults(func=command_context)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        args = parser.parse_args(["entry"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
