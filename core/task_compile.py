#!/usr/bin/env python3
"""Compile short-lived task context sessions from the Immortal vault.

This is the default runtime path for ad hoc "digital agent" work:
generate a task-local context pack, use it for the current task, then let
cleanup remove it later. Persistent Codex skills should be created only by the
explicit role-distill promotion flow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import configured_vault_dir, owner_display_name


SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = configured_vault_dir()
SESSIONS_DIR = IMMORTAL_DIR / "sessions"
LATEST_MD = SESSIONS_DIR / "latest.md"
LATEST_JSON = SESSIONS_DIR / "latest.json"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


MODE_LABELS = {
    "auto": "自动识别",
    "advisor": "决策顾问",
    "writer": "写稿审稿",
    "reviewer": "复核审阅",
    "business": "商业判断",
    "project": "项目推进",
    "shadow": "影子分身",
    "custom": "自定义",
}


def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def slugify(value: str, fallback: str = "task") -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return (text[:48].strip("-") or fallback)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def file_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "bytes": 0}
    return {
        "path": str(path),
        "exists": True,
        "bytes": path.stat().st_size,
        "modified_at": iso(datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ)),
    }


def build_system_prompt(owner: str, query: str, mode: str, context_path: Path) -> str:
    mode_label = MODE_LABELS.get(mode, mode)
    return "\n".join(
        [
            "# Immortal Task Session Prompt",
            "",
            f"Owner: {owner}",
            f"Task: {query}",
            f"Mode: {mode_label}",
            f"Context file: {context_path}",
            "",
            "## How To Use",
            "- Treat this as a short-lived task context, not a permanent identity.",
            "- Read TASK_CONTEXT.md first and use only the task-relevant memory it contains.",
            "- Separate evidence, inference, and uncertainty.",
            "- Do not expose private raw records, credentials, customer secrets, or unrelated chats.",
            "- Do not claim to fully replace the owner. You may assist with style, preferences, and decision heuristics.",
            "- If the task needs a stable reusable workflow, recommend explicit promotion with role-distill later.",
            "",
            "## Output Standard",
            "- Start with the practical answer or decision.",
            "- Keep long-term owner preferences in mind, but verify specific facts with recall when needed.",
            "- Produce an artifact the current task can use immediately.",
            "",
        ]
    )


def build_runbook(session_dir: Path, query: str) -> str:
    return "\n".join(
        [
            "# Immortal Task Session",
            "",
            f"Task: {query}",
            "",
            "## One-line Handoff",
            f"请读取 `{session_dir / 'TASK_CONTEXT.md'}`，只在本次任务中使用这份上下文；任务结束后无需长期保存。",
            "",
            "## Files",
            f"- task context: `{session_dir / 'TASK_CONTEXT.md'}`",
            f"- system prompt: `{session_dir / 'SYSTEM_PROMPT.md'}`",
            f"- manifest: `{session_dir / 'manifest.json'}`",
            "",
            "## Promotion Rule",
            "Only promote this into a persistent Codex skill if the same workflow is used repeatedly and has stable rules.",
            "",
        ]
    )


def write_latest(session_dir: Path, manifest: dict[str, Any], context_text: str) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_MD.write_text(context_text, encoding="utf-8")
    latest_payload = dict(manifest)
    latest_payload["session_dir"] = str(session_dir)
    write_json(LATEST_JSON, latest_payload)


def command_compile(args: argparse.Namespace) -> int:
    query = (args.query or "当前任务").strip()
    mode = args.mode if args.mode in MODE_LABELS else "auto"
    generated = now_local()
    expires = generated + timedelta(hours=float(args.ttl_hours))
    hash_part = hashlib.sha1(f"{query}|{generated.timestamp()}".encode("utf-8")).hexdigest()[:8]
    session_id = f"{generated.strftime('%Y%m%d-%H%M%S')}-{slugify(query)}-{hash_part}"
    session_dir = SESSIONS_DIR / session_id
    context_path = session_dir / "TASK_CONTEXT.md"
    prompt_path = session_dir / "SYSTEM_PROMPT.md"
    runbook_path = session_dir / "README.md"
    manifest_path = session_dir / "manifest.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    if args.cleanup_first:
        cleanup_expired(max_age_hours=float(args.cleanup_max_age_hours), dry_run=False)

    cmd = [
        sys.executable,
        str(SKILL_DIR / "agent_bridge.py"),
        "context",
        query,
        "--since",
        args.since,
        "--output",
        str(context_path),
        "--timeout",
        str(args.timeout),
    ]
    if args.with_recall:
        cmd.append("--with-recall")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SKILL_DIR), timeout=args.timeout + 30)

    owner = owner_display_name()
    context_text = context_path.read_text(encoding="utf-8", errors="ignore") if context_path.exists() else ""
    if result.returncode != 0 and not context_text:
        context_text = "\n".join(
            [
                "# Immortal Task Context",
                "",
                f"Generated: {iso(generated)}",
                f"Query: {query}",
                f"Exit code: {result.returncode}",
                "",
                "Context generation failed before a context file was written.",
                "",
                "STDOUT:",
                result.stdout.strip(),
                "",
                "STDERR:",
                result.stderr.strip(),
                "",
            ]
        )
        context_path.write_text(context_text, encoding="utf-8")

    prompt_path.write_text(build_system_prompt(owner, query, mode, context_path), encoding="utf-8")
    runbook_path.write_text(build_runbook(session_dir, query), encoding="utf-8")

    manifest = {
        "version": 1,
        "kind": "task_session",
        "session_id": session_id,
        "query": query,
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "owner": owner,
        "generated_at": iso(generated),
        "expires_at": iso(expires),
        "ttl_hours": float(args.ttl_hours),
        "returncode": result.returncode,
        "promoted_to_skill": False,
        "files": {
            "TASK_CONTEXT.md": file_info(context_path),
            "SYSTEM_PROMPT.md": file_info(prompt_path),
            "README.md": file_info(runbook_path),
            "manifest.json": {"path": str(manifest_path), "exists": True},
        },
        "source_command": " ".join(cmd),
    }
    write_json(manifest_path, manifest)
    write_latest(session_dir, manifest, context_text)

    print(f"session_dir={session_dir}")
    print(f"context_md={context_path}")
    print(f"system_prompt={prompt_path}")
    print(f"manifest={manifest_path}")
    print(f"latest_md={LATEST_MD}")
    if args.print:
        print()
        print(context_text)
    return result.returncode


def cleanup_expired(max_age_hours: float, dry_run: bool) -> list[Path]:
    removed: list[Path] = []
    if not SESSIONS_DIR.exists():
        return removed
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for path in sorted(SESSIONS_DIR.iterdir()):
        if not path.is_dir():
            continue
        manifest = read_json(path / "manifest.json", {})
        expires_raw = str(manifest.get("expires_at") or "")
        remove = False
        if expires_raw:
            try:
                expires = datetime.fromisoformat(expires_raw)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=LOCAL_TZ)
                remove = expires.astimezone(timezone.utc) < datetime.now(timezone.utc)
            except ValueError:
                remove = False
        if not remove:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            remove = mtime < cutoff
        if remove:
            removed.append(path)
            if not dry_run:
                shutil.rmtree(path)
    return removed


def command_cleanup(args: argparse.Namespace) -> int:
    removed = cleanup_expired(max_age_hours=float(args.max_age_hours), dry_run=args.dry_run)
    for path in removed:
        print(f"{'would_remove' if args.dry_run else 'removed'}={path}")
    print(f"removed_count={0 if args.dry_run else len(removed)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile short-lived task context sessions")
    sub = parser.add_subparsers(dest="command")

    compile_parser = sub.add_parser("compile", help="Compile a short-lived task session")
    compile_parser.add_argument("query", nargs="?", default="当前任务")
    compile_parser.add_argument(
        "--mode",
        default="auto",
        choices=sorted(MODE_LABELS),
        help="Scenario hint for the session prompt",
    )
    compile_parser.add_argument("--since", default="2026-03-01")
    compile_parser.add_argument("--with-recall", action="store_true")
    compile_parser.add_argument("--timeout", type=int, default=240)
    compile_parser.add_argument("--ttl-hours", type=float, default=72)
    compile_parser.add_argument("--cleanup-first", action="store_true")
    compile_parser.add_argument("--cleanup-max-age-hours", type=float, default=168)
    compile_parser.add_argument("--print", action="store_true")
    compile_parser.set_defaults(func=command_compile)

    cleanup_parser = sub.add_parser("cleanup", help="Remove expired task sessions")
    cleanup_parser.add_argument("--max-age-hours", type=float, default=168)
    cleanup_parser.add_argument("--dry-run", action="store_true")
    cleanup_parser.set_defaults(func=command_cleanup)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in {"compile", "cleanup", "-h", "--help"}:
        argv = ["compile", *argv]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        args = parser.parse_args(["compile"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
