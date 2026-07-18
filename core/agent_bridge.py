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
from command_hints import cli_command
from preflight import STATUS_DEGRADED, STATUS_UNAVAILABLE, gather_preflight, render_summary


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
    preflight = gather_preflight()
    return {
        "generated_at": now_local(),
        "owner": owner_display_name(),
        "vault_status": preflight["vault_status"],
        "context_status": preflight["context_status"],
        "loss_protection": preflight["loss_protection"],
        "real_record_count": preflight["real_record_count"],
        "total_records": state.get("total_records"),
        "last_collect": state.get("last_collect"),
        "last_feishu_collect": state.get("last_feishu_collect"),
        "quality_status": quality.get("status"),
        "quality_score": quality.get("score"),
        "quality_issues": quality.get("issue_count"),
        "paths": {
            "entry_md": str(ENTRY_MD),
            "latest_context_md": str(LATEST_CONTEXT_MD),
            "profile_compact": str(IMMORTAL_DIR / "profile_compact.md"),
            "profile_nuwa": str(IMMORTAL_DIR / "profile_nuwa.md"),
            "people_index": str(IMMORTAL_DIR / "people" / "people_index.md"),
            # 2026-06-14：digest/product 已停用，改注入判断力卡片盒（纠正即记忆）
            "cards": str(IMMORTAL_DIR / "cards" / "cards.md"),
            "cards_compact": str(IMMORTAL_DIR / "cards" / "cards_compact.md"),
        },
        "commands": {
            "health": cli_command("health", "--max-age-hours", "72"),
            "agent_context": cli_command("agent-context", "<当前任务>", "--print"),
            "recall": cli_command("recall", "<主题>"),
            "context": cli_command("context", "<当前任务>"),
            "cards": cli_command("cards", "list"),
        },
        "local_urls": {
            "agent_entry": "http://127.0.0.1:8765/agent-entry",
            "agent_factory": "http://127.0.0.1:8765/agent-factory",
        },
    }


def render_entry(meta: dict[str, Any]) -> str:
    owner = meta.get("owner") or "the owner"
    command = meta["commands"]["agent_context"]
    one_sentence = (
        f"请先读取 {ENTRY_MD}，然后针对当前任务运行 "
        f"`{command}`，"
        f"把返回内容作为理解{owner}的长期上下文。"
    )
    current_task_command = cli_command("agent-context", "本次任务", "--print")
    claude_prompt = (
        f"你现在可以调用{owner}的永生知识库。开始任务前先运行："
        f"`{current_task_command}`，"
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
        f"- vault_status: {meta.get('vault_status')}"
        + ("　⚠️ DEMO ONLY：尚未接入真实数据，未受记忆保护" if meta.get("vault_status") == "smoke_only" else ""),
        f"- context_status: {meta.get('context_status')}",
        f"- loss_protection: {meta.get('loss_protection')}",
        f"- real_record_count: {meta.get('real_record_count')}",
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
    current_task_command = cli_command("agent-context", "本次任务", "--print")
    claude_prompt = (
        f"你现在可以调用{meta.get('owner')}的永生知识库。"
        f"开始任务前运行：{current_task_command}"
    )
    CLAUDE_PROMPT.write_text(claude_prompt + "\n", encoding="utf-8")
    print(f"entry_md={ENTRY_MD}")
    print(f"entry_json={ENTRY_JSON}")
    print(f"claude_prompt={CLAUDE_PROMPT}")
    print(f"agent_entry_url=http://127.0.0.1:8765/agent-entry")
    return 0


def command_context(args: argparse.Namespace) -> int:
    query = (args.query or "当前任务").strip()

    preflight = gather_preflight(query=query, since=args.since)
    if preflight["context_status"] == STATUS_UNAVAILABLE and not getattr(args, "force", False):
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": now_local(),
            "query": query,
            "context_status": STATUS_UNAVAILABLE,
            "context_md": None,
            "preflight": preflight,
        }
        write_json(LATEST_CONTEXT_JSON, payload)
        print(render_summary(preflight))
        print()
        print(f"context_status={STATUS_UNAVAILABLE}")
        print(f"context_json={LATEST_CONTEXT_JSON}")
        print("Refusing to generate a context pack from an empty or demo-only vault. Use --force to override for debugging.")
        return 3

    cmd = [sys.executable, str(SKILL_DIR / "immortal.py"), "context", query, "--since", args.since]
    if args.with_recall:
        cmd.append("--with-recall")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SKILL_DIR), timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output.decode("utf-8", "replace") if isinstance(exc.output, bytes) else (exc.output or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = subprocess.CompletedProcess(
            cmd,
            1,
            stdout=stdout,
            stderr=(stderr + f"\ncontext generation timed out after {args.timeout}s").strip(),
        )
    body = result.stdout.strip()
    if result.stderr.strip():
        body = body + "\n\nSTDERR:\n" + result.stderr.strip()
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    context_status = preflight["context_status"]
    header = [
        "# Immortal Task Context",
        "",
        f"Generated: {now_local()}",
        f"Query: {query}",
        f"Exit code: {result.returncode}",
        f"Context status: {context_status}",
        "",
        "Use this as task-local context. Do not paste raw vault data.",
    ]
    if context_status != "ready":
        header.extend(
            [
                "",
                f"## ⚠️ CONTEXT STATUS: {context_status.upper()}",
                "",
                f"- vault_status: {preflight['vault_status']}",
                f"- last_real_collect_at: {preflight['last_real_collect_at'] or 'never'}",
                f"- query_coverage: {preflight['query_coverage']}"
                + (
                    f" (gap {preflight['coverage_gap_hours']:.0f}h)"
                    if preflight.get("coverage_gap_hours")
                    else ""
                ),
                f"- reasons: {'; '.join(preflight['reasons']) or 'none'}",
                "",
                "长期记忆覆盖不完整。引用其中的历史结论前，先向用户说明这一缺口。",
            ]
        )
    header.extend(["", "---", ""])
    content = "\n".join(header) + body + "\n"
    output = Path(args.output).expanduser() if args.output else LATEST_CONTEXT_MD
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    payload = {
        "generated_at": now_local(),
        "query": query,
        "exit_code": result.returncode,
        "context_status": context_status,
        "context_md": str(output),
        "entry_md": str(ENTRY_MD),
        "preflight": preflight,
        "files": {
            "context": file_info(output),
            "entry": file_info(ENTRY_MD),
        },
    }
    write_json(LATEST_CONTEXT_JSON, payload)
    print(f"context_md={output}")
    print(f"context_json={LATEST_CONTEXT_JSON}")
    print(f"context_status={context_status}")
    if context_status == STATUS_DEGRADED:
        print("WARNING: context is degraded; see the CONTEXT STATUS block inside the pack.")
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
    context.add_argument("--force", action="store_true", help="Generate a context pack even when preflight reports the vault as unavailable (debugging only)")
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
