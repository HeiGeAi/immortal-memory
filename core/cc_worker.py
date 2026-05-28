#!/usr/bin/env python3
"""Low-cost Claude Code worker wrapper for Immortal.

Codex stays the controller. This script delegates bounded read-only analysis to
the local `claude` CLI, which can use the user's cheaper Claude Code model/key
configuration without exposing secrets.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = Path.home() / ".immortal"
MIRROR_DIR = IMMORTAL_DIR / "feishu" / "drive_mirror"

READ_ONLY_TOOLS = "Read,Bash"
READ_ONLY_ALLOWED = ",".join(
    [
        "Read",
        "Bash(ls *)",
        "Bash(cat *)",
        "Bash(tail *)",
        "Bash(rg *)",
        "Bash(find *)",
        "Bash(sqlite3 *)",
        "Bash(python3 -m py_compile *)",
        "Bash(python3 */immortal.py *status*)",
    ]
)


def find_claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path("/opt/homebrew/bin/claude")
    if fallback.exists():
        return str(fallback)
    raise FileNotFoundError("claude CLI not found")


def run_worker(prompt: str, *, budget: float, add_dirs: list[Path], timeout: int, allowed_tools: str) -> int:
    claude = find_claude()
    cmd = [
        claude,
        "-p",
        "--permission-mode",
        "dontAsk",
        "--tools",
        READ_ONLY_TOOLS,
        "--allowedTools",
        allowed_tools,
        "--max-budget-usd",
        f"{budget:.2f}",
    ]
    for path in add_dirs:
        cmd.extend(["--add-dir", str(path)])
    result = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


def command_check(args: argparse.Namespace) -> int:
    claude = find_claude()
    version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=15)
    print((version.stdout or version.stderr).strip())
    prompt = "用一句中文回复：Immortal Claude Code worker 可用。不要读取任何配置文件。"
    return run_worker(
        prompt,
        budget=args.budget,
        add_dirs=[SKILL_DIR],
        timeout=args.timeout,
        allowed_tools="",
    )


def command_mirror_audit(args: argparse.Namespace) -> int:
    prompt = """你是 Immortal 项目的低成本 Claude Code worker。
只做只读审计，不要修改文件，不要读取或打印 ~/.claude/settings.json、token、secret。

请检查：
1. ~/.codex/skills/immortal/feishu_drive_mirror.py 的设计风险；
2. ~/.immortal/feishu/drive_mirror/coverage.json 与 inventory.sqlite3 的当前进度；
3. 下一步最小可执行动作。

输出中文，控制在 800 字以内，优先给结论和命令。"""
    return run_worker(
        prompt,
        budget=args.budget,
        add_dirs=[SKILL_DIR, MIRROR_DIR],
        timeout=args.timeout,
        allowed_tools=READ_ONLY_ALLOWED,
    )


def command_prompt(args: argparse.Namespace) -> int:
    prompt = args.prompt or sys.stdin.read()
    add_dirs = [Path(path).expanduser().resolve() for path in args.add_dir]
    if not add_dirs:
        add_dirs = [SKILL_DIR]
    return run_worker(
        prompt,
        budget=args.budget,
        add_dirs=add_dirs,
        timeout=args.timeout,
        allowed_tools=args.allowed_tools,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded low-cost Claude Code worker tasks")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="Verify the local claude CLI worker")
    check.add_argument("--budget", type=float, default=0.20)
    check.add_argument("--timeout", type=int, default=120)
    check.set_defaults(func=command_check)

    audit = sub.add_parser("mirror-audit", help="Ask Claude Code to audit Feishu mirror status read-only")
    audit.add_argument("--budget", type=float, default=0.50)
    audit.add_argument("--timeout", type=int, default=240)
    audit.set_defaults(func=command_mirror_audit)

    prompt = sub.add_parser("prompt", help="Run a custom bounded prompt via Claude Code")
    prompt.add_argument("prompt", nargs="?")
    prompt.add_argument("--budget", type=float, default=0.50)
    prompt.add_argument("--timeout", type=int, default=300)
    prompt.add_argument("--add-dir", action="append", default=[])
    prompt.add_argument("--allowed-tools", default=READ_ONLY_ALLOWED)
    prompt.set_defaults(func=command_prompt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
