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
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import configured_vault_dir, owner_display_name
from agent_bridge import redact_external_text
from event_store import safe_read_text


SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = configured_vault_dir()
SESSIONS_DIR = IMMORTAL_DIR / "sessions"
LATEST_MD = SESSIONS_DIR / "latest.md"
LATEST_JSON = SESSIONS_DIR / "latest.json"
BRIDGE_LATEST_JSON = IMMORTAL_DIR / "agent" / "latest-context.json"
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


def _canonical_system_alias_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(str(path.expanduser())))
    for alias_text in ("/var", "/tmp", "/etc"):
        alias = Path(alias_text)
        try:
            if alias.is_symlink() and (absolute == alias or alias in absolute.parents):
                return alias.resolve(strict=True) / absolute.relative_to(alias)
        except (OSError, ValueError):
            continue
    return absolute


def _hash_text(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def file_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "bytes": 0}
    return {
        "path": str(path),
        "exists": True,
        "bytes": path.stat().st_size,
        "modified_at": iso(datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ)),
    }


def verified_file_info(
    path: Path, content: str, content_hash: str
) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": True,
        "bytes": len(content.encode("utf-8")),
        "content_hash": content_hash,
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
    preview_id = str(getattr(args, "preview_id", "") or "").strip()
    preview_hash = str(getattr(args, "preview_hash", "") or "").strip()
    if bool(preview_id) != bool(preview_hash):
        print(
            "preview_approval_incomplete: --preview-id and --preview-hash are required together",
            file=sys.stderr,
        )
        return 2
    if not preview_id:
        if not hasattr(args, "ttl_seconds"):
            args.ttl_seconds = 900
        return command_preview(args)
    raw_query = (args.query or "当前任务").strip()
    request_label = redact_external_text(raw_query, max_chars=500)
    requested_mode = args.mode if args.mode in MODE_LABELS else "auto"
    generated = now_local()
    expires = generated + timedelta(hours=float(args.ttl_hours))
    hash_part = hashlib.sha1(f"{request_label}|{generated.timestamp()}".encode("utf-8")).hexdigest()[:8]
    session_id = f"{generated.strftime('%Y%m%d-%H%M%S')}-{slugify(request_label)}-{hash_part}"
    session_dir = SESSIONS_DIR / session_id
    context_path = session_dir / "TASK_CONTEXT.md"
    prompt_path = session_dir / "SYSTEM_PROMPT.md"
    runbook_path = session_dir / "README.md"
    manifest_path = session_dir / "manifest.json"
    bridge_result_path = session_dir / "bridge-result.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    if args.cleanup_first:
        cleanup_expired(max_age_hours=float(args.cleanup_max_age_hours), dry_run=False)

    cmd = [
        sys.executable,
        str(SKILL_DIR / "agent_bridge.py"),
        "context",
        raw_query,
        "--since",
        args.since,
        "--output",
        str(context_path),
        "--timeout",
        str(args.timeout),
        "--mode",
        args.mode,
        "--metadata-output",
        str(bridge_result_path),
    ]
    if getattr(args, "preview_id", ""):
        cmd.extend(["--preview-id", args.preview_id, "--preview-hash", args.preview_hash])
    for item_id in getattr(args, "exclude_item_id", []) or []:
        cmd.extend(["--exclude-item-id", item_id])
    if args.with_recall:
        cmd.append("--with-recall")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SKILL_DIR), timeout=args.timeout + 30)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output.decode("utf-8", "replace") if isinstance(exc.output, bytes) else (exc.output or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = subprocess.CompletedProcess(
            cmd,
            1,
            stdout=stdout,
            stderr=(stderr + f"\ncontext bridge timed out after {args.timeout + 30}s").strip(),
        )

    bridge_payload = read_json(bridge_result_path, {})
    context_text = None
    try:
        context_text = safe_read_text(_canonical_system_alias_path(context_path))
    except (OSError, RuntimeError, ValueError):
        context_text = None
    expected_markdown_hash = str(
        bridge_payload.get("context_markdown_hash") or ""
    )
    ready = bool(
        result.returncode == 0
        and bridge_payload.get("lifecycle_status") == "compiled"
        and bridge_payload.get("context_id")
        and bridge_payload.get("content_hash")
        and expected_markdown_hash.startswith("sha256:")
        and context_text
        and _hash_text(context_text) == expected_markdown_hash
    )
    if not ready:
        shutil.rmtree(session_dir, ignore_errors=True)
        if result.stdout:
            print(redact_external_text(result.stdout, max_chars=24_000).rstrip())
        if result.stderr:
            print(
                redact_external_text(result.stderr, max_chars=4000).rstrip(),
                file=sys.stderr,
            )
        if result.returncode == 0:
            print("context_not_ready: approved compile did not return a READY context", file=sys.stderr)
        return int(result.returncode or 1)
    authority_task = redact_external_text(bridge_payload.get("task"), max_chars=500)
    authority_mode = str(bridge_payload.get("mode") or "")
    if not authority_task or authority_mode not in MODE_LABELS or authority_mode == "auto":
        shutil.rmtree(session_dir, ignore_errors=True)
        print("context_not_ready: compiled authority task or mode is invalid", file=sys.stderr)
        return 1
    owner = owner_display_name()
    prompt_path.write_text(build_system_prompt(owner, authority_task, authority_mode, context_path), encoding="utf-8")
    runbook_path.write_text(build_runbook(session_dir, authority_task), encoding="utf-8")
    manifest = {
        "version": 1,
        "kind": "task_session",
        "session_id": session_id,
        "query": authority_task,
        "mode": authority_mode,
        "mode_label": MODE_LABELS.get(authority_mode, authority_mode),
        "request_label": request_label,
        "requested_mode": requested_mode,
        "owner": owner,
        "generated_at": iso(generated),
        "expires_at": iso(expires),
        "ttl_hours": float(args.ttl_hours),
        "returncode": result.returncode,
        "runtime": bridge_payload.get("runtime"),
        "preview_id": bridge_payload.get("preview_id"),
        "preview_hash": bridge_payload.get("preview_hash"),
        "context_id": bridge_payload.get("context_id"),
        "content_hash": bridge_payload.get("content_hash"),
        "context_markdown_hash": expected_markdown_hash,
        "source_revision": bridge_payload.get("source_revision"),
        "promoted_to_skill": False,
        "files": {
            "TASK_CONTEXT.md": verified_file_info(
                context_path, context_text, expected_markdown_hash
            ),
            "SYSTEM_PROMPT.md": file_info(prompt_path),
            "README.md": file_info(runbook_path),
            "manifest.json": {"path": str(manifest_path), "exists": True},
            "bridge-result.json": file_info(bridge_result_path),
        },
        "source_command": "agent_bridge context [TASK] --mode " + requested_mode,
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


def command_preview(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        str(SKILL_DIR / "agent_bridge.py"),
        "context",
        (args.query or "当前任务").strip(),
        "--since",
        args.since,
        "--timeout",
        str(args.timeout),
        "--mode",
        args.mode,
        "--ttl-seconds",
        str(args.ttl_seconds),
        "--preview-only",
    ]
    if args.with_recall:
        cmd.append("--with-recall")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(SKILL_DIR),
            timeout=args.timeout + 30,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr=(stderr + "\ncontext preview timed out").strip(),
        )
    if result.stdout:
        print(redact_external_text(result.stdout, max_chars=24_000).rstrip())
    if result.stderr:
        print(redact_external_text(result.stderr, max_chars=4000).rstrip(), file=sys.stderr)
    return int(result.returncode)


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

    def add_context_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("query", nargs="?", default="当前任务")
        target.add_argument(
        "--mode",
        default="auto",
        choices=sorted(MODE_LABELS),
        help="Scenario hint for the session prompt",
        )
        target.add_argument("--since", default="2026-03-01")
        target.add_argument("--with-recall", action="store_true")
        target.add_argument("--timeout", type=int, default=240)

    preview_parser = sub.add_parser("preview", help="Create a reviewable context preview")
    add_context_arguments(preview_parser)
    preview_parser.add_argument("--ttl-seconds", type=int, default=900)
    preview_parser.set_defaults(func=command_preview)

    compile_parser = sub.add_parser("compile", help="Compile a short-lived task session")
    add_context_arguments(compile_parser)
    compile_parser.add_argument("--preview-id", default="")
    compile_parser.add_argument("--preview-hash", default="")
    compile_parser.add_argument("--exclude-item-id", action="append", default=[])
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
    if argv and argv[0] not in {"preview", "compile", "cleanup", "-h", "--help"}:
        argv = ["compile", *argv]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        args = parser.parse_args(["compile"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
