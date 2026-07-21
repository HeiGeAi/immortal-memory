#!/usr/bin/env python3
"""Bridge files and task-local context packs for external agents."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import configured_vault_dir, owner_display_name
from command_hints import cli_command
from context_compiler import ContextCompiler, ContextCompilerError
from event_store import safe_atomic_write_text
from preflight import STATUS_DEGRADED, STATUS_UNAVAILABLE, gather_preflight, render_summary
from redact_common import redact as redact_credentials


SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = configured_vault_dir()
AGENT_DIR = IMMORTAL_DIR / "agent"
ENTRY_MD = AGENT_DIR / "ENTRY.md"
ENTRY_JSON = AGENT_DIR / "entry.json"
LATEST_CONTEXT_MD = AGENT_DIR / "latest-context.md"
LATEST_CONTEXT_JSON = AGENT_DIR / "latest-context.json"
CLAUDE_PROMPT = AGENT_DIR / "claude-code-prompt.txt"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
ACTOR = {"kind": "owner", "id": "owner"}
_PRIVATE_PATHS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?:/Users|/home)/[^\s'\"`]+"),
    re.compile(r"(?i)\b[A-Z]:\\Users\\[^\s'\"`]+"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"\bou_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
)


def _canonical_system_alias_path(path: Path) -> Path:
    """Normalize macOS system aliases without accepting arbitrary symlinks."""
    absolute = Path(os.path.abspath(str(path.expanduser())))
    for alias_text in ("/var", "/tmp", "/etc"):
        alias = Path(alias_text)
        try:
            if alias.is_symlink() and (absolute == alias or alias in absolute.parents):
                return alias.resolve(strict=True) / absolute.relative_to(alias)
        except (OSError, ValueError):
            continue
    return absolute


def _safe_write_text(path: Path, content: str) -> None:
    safe_atomic_write_text(_canonical_system_alias_path(path), content)


def redact_external_text(value: Any, max_chars: int = 4000) -> str:
    """Redact untrusted process/compiler text before persistence or display."""
    text = redact_credentials(str(value or ""))
    for pattern in _PRIVATE_PATHS:
        text = pattern.sub("[REDACTED_PATH]", text)
    return text[:max_chars]


def _redact_external_tree(value: Any) -> Any:
    if isinstance(value, str):
        return redact_external_text(value, max_chars=24_000)
    if isinstance(value, list):
        return [_redact_external_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_external_tree(item) for key, item in value.items()}
    return value


def _metadata_path(args: argparse.Namespace) -> Path | None:
    cached = getattr(args, "_validated_metadata_output", None)
    if cached is not None:
        return cached
    raw = str(getattr(args, "metadata_output", "") or "").strip()
    if not raw:
        args._validated_metadata_output = None
        return None
    path = Path(raw).expanduser()
    lexical = Path(os.path.abspath(str(path)))
    if not path.is_absolute() or path != lexical or ".." in path.parts:
        raise ContextCompilerError(
            "invalid_metadata_output",
            "metadata output must be a canonical absolute path",
        )
    normalized = _canonical_system_alias_path(lexical)
    args._validated_metadata_output = normalized
    return normalized


def _write_context_metadata(
    args: argparse.Namespace, payload: dict[str, Any]
) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    scoped = _metadata_path(args)
    if scoped is not None:
        _safe_write_text(scoped, text)
    if scoped != LATEST_CONTEXT_JSON:
        _safe_write_text(LATEST_CONTEXT_JSON, text)


def _metadata_display_path(args: argparse.Namespace) -> Path:
    return _metadata_path(args) or LATEST_CONTEXT_JSON


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


def _legacy_context(args: argparse.Namespace) -> int:
    query = (args.query or "当前任务").strip()
    safe_query = redact_external_text(query, 500)

    preflight = gather_preflight(query=query, since=args.since)
    if preflight["context_status"] == STATUS_UNAVAILABLE and not getattr(args, "force", False):
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": now_local(),
            "query": safe_query,
            "context_status": STATUS_UNAVAILABLE,
            "context_md": None,
            "preflight": _redact_external_tree(preflight),
        }
        _write_context_metadata(args, payload)
        print(render_summary(preflight))
        print()
        print(f"context_status={STATUS_UNAVAILABLE}")
        print(f"context_json={_metadata_display_path(args)}")
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
    stdout = redact_external_text(result.stdout, max_chars=24_000).strip()
    stderr = redact_external_text(result.stderr, max_chars=4000).strip()
    body = stdout
    if stderr:
        body = body + "\n\nSTDERR:\n" + stderr
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    context_status = preflight["context_status"]
    header = [
        "# Immortal Task Context",
        "",
        f"Generated: {now_local()}",
        f"Query: {safe_query}",
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
    _safe_write_text(output, content)
    payload = {
        "generated_at": now_local(),
        "query": safe_query,
        "exit_code": result.returncode,
        "context_status": context_status,
        "runtime": "legacy_v1",
        "context_md": str(output),
        "entry_md": str(ENTRY_MD),
        "preflight": _redact_external_tree(preflight),
        "files": {
            "context": file_info(output),
            "entry": file_info(ENTRY_MD),
        },
    }
    _write_context_metadata(args, payload)
    print(f"context_md={output}")
    print(f"context_json={_metadata_display_path(args)}")
    print(f"context_status={context_status}")
    if context_status == STATUS_DEGRADED:
        print("WARNING: context is degraded; see the CONTEXT STATUS block inside the pack.")
    if args.print:
        print()
        print(content)
    return result.returncode


def _authoritative_runtime_available() -> bool:
    """Distinguish a pre-v1.1 vault from a broken v1.1 authority."""
    return (IMMORTAL_DIR / "model" / "living-self" / "current.json").is_file()


def _resolve_mode(mode: str, query: str) -> str:
    if mode != "auto":
        return mode
    lowered = query.casefold()
    matches = (
        ("writer", ("写", "文案", "文章", "脚本", "draft")),
        ("reviewer", ("审", "评", "检查", "review", "audit")),
        ("business", ("商业", "客户", "报价", "business")),
        ("project", ("项目", "推进", "交付", "project")),
    )
    for candidate, keywords in matches:
        if any(keyword in lowered for keyword in keywords):
            return candidate
    return "advisor"


def _write_authoritative_error(
    args: argparse.Namespace,
    preflight: dict[str, Any],
    exc: BaseException,
) -> int:
    code = redact_external_text(getattr(exc, "code", "context_compile_failed"), 120)
    detail = redact_external_text(str(exc), 1000)
    output = Path(args.output).expanduser() if args.output else None
    if output is not None:
        _safe_write_text(
            output,
            "\n".join(
                [
                    "# Immortal Task Context",
                    "",
                    "Context compilation failed before a READY pack was available.",
                    "Error code: " + code,
                    "Error: " + detail,
                    "",
                ]
            ),
        )
    payload = {
        "generated_at": now_local(),
        "query": redact_external_text(args.query, 500),
        "context_status": "unavailable",
        "runtime": "living_self_v1.1",
        "error_code": code,
        "error": detail,
        "context_md": str(output) if output is not None else None,
        "preflight": _redact_external_tree(preflight),
    }
    _write_context_metadata(args, payload)
    print("context_status=unavailable")
    print("error_code=" + code)
    print("context_json=" + str(_metadata_display_path(args)))
    return 1


def command_context(args: argparse.Namespace) -> int:
    query = (args.query or "当前任务").strip()
    safe_query = redact_external_text(query, 500)
    try:
        _metadata_path(args)
    except ContextCompilerError as exc:
        print("error_code=" + exc.code)
        return 2
    preflight = gather_preflight(query=query, since=args.since)
    if (
        preflight["context_status"] == STATUS_UNAVAILABLE
        and not getattr(args, "force", False)
    ):
        return _legacy_context(args)
    if not _authoritative_runtime_available():
        if getattr(args, "preview_only", False) or getattr(args, "preview_id", ""):
            error = ContextCompilerError(
                "legacy_runtime_no_preview",
                "legacy v1 vault cannot provide a reviewable authoritative preview",
            )
            return _write_authoritative_error(args, preflight, error)
        return _legacy_context(args)

    compiler = ContextCompiler(IMMORTAL_DIR)
    mode = getattr(args, "mode", "auto")
    preview_id = str(getattr(args, "preview_id", "") or "").strip()
    preview_hash = str(getattr(args, "preview_hash", "") or "").strip()
    approval_supplied = bool(preview_id and preview_hash)
    excluded = list(getattr(args, "exclude_item_id", []) or [])
    try:
        if bool(preview_id) != bool(preview_hash):
            raise ContextCompilerError(
                "preview_approval_incomplete",
                "preview ID and preview hash must be supplied together",
            )
        if preview_id:
            preview = compiler.context_store.get(preview_id)
            if preview["preview_hash"] != preview_hash:
                raise ContextCompilerError(
                    "stale_preview", "preview hash no longer matches authority"
                )
        else:
            nonce = uuid.uuid4().hex
            preview = compiler.preview(
                query,
                mode=mode,
                ttl_seconds=int(getattr(args, "ttl_seconds", 900)),
                request_id="req_bridge_preview_" + nonce,
                idempotency_key="idem_bridge_preview_" + nonce,
                actor=ACTOR,
                reason="Agent Bridge context preview",
            )
            preview_id = str(preview["preview_id"])
            preview_hash = str(preview["preview_hash"])
        if not approval_supplied or getattr(args, "preview_only", False):
            authority_task = str(preview["task"])
            authority_mode = str(preview["mode"])
            payload = {
                "generated_at": now_local(),
                "query": authority_task,
                "task": authority_task,
                "mode": authority_mode,
                "request_label": safe_query,
                "context_status": preflight["context_status"],
                "runtime": "living_self_v1.1",
                "lifecycle_status": "preview",
                "preview_id": preview_id,
                "preview_hash": preview_hash,
                "source_revision": preview["source_revision"],
                "selection": preview.get("selection"),
                "sections": preview.get("sections"),
                "context_md": None,
                "recommended_mode": (
                    _resolve_mode("auto", query)
                    if authority_mode == "auto"
                    else authority_mode
                ),
                "preflight": _redact_external_tree(preflight),
            }
            _write_context_metadata(args, payload)
            print("preview_id=" + preview_id)
            print("preview_hash=" + preview_hash)
            print("context_json=" + str(_metadata_display_path(args)))
            print("lifecycle_status=preview")
            return 0
        preview_mode = str(preview["mode"])
        requested_mode = str(mode)
        if preview_mode == "auto":
            if requested_mode == "auto":
                raise ContextCompilerError(
                    "unresolved_context_mode",
                    "auto preview requires an explicit non-auto approved mode",
                )
            approved_mode = requested_mode
        else:
            if requested_mode not in {"auto", preview_mode}:
                raise ContextCompilerError(
                    "resolved_mode_conflict",
                    "requested mode conflicts with the approved preview",
                )
            approved_mode = preview_mode
        nonce = uuid.uuid4().hex
        compiled = compiler.compile(
            preview_id=preview_id,
            preview_hash=preview_hash,
            excluded_item_ids=excluded,
            request_id="req_bridge_compile_" + nonce,
            idempotency_key="idem_bridge_compile_" + nonce,
            actor=ACTOR,
            reason="Agent Bridge preview approved",
            resolved_mode=approved_mode,
        )
    except (ContextCompilerError, OSError, ValueError) as exc:
        return _write_authoritative_error(args, preflight, exc)

    canonical_md = Path(str(compiled["context_md"]))
    content = canonical_md.read_text(encoding="utf-8")
    output = Path(args.output).expanduser() if args.output else canonical_md
    if output != canonical_md:
        _safe_write_text(output, content)
    payload = {
        "generated_at": now_local(),
        "query": compiled["task"],
        "task": compiled["task"],
        "mode": compiled["mode"],
        "request_label": safe_query,
        "exit_code": 0,
        "context_status": preflight["context_status"],
        "runtime": "living_self_v1.1",
        "lifecycle_status": compiled["lifecycle_status"],
        "availability_status": compiled["availability_status"],
        "preview_id": preview_id,
        "preview_hash": preview_hash,
        "context_id": compiled["context_id"],
        "content_hash": compiled["content_hash"],
        "expires_at": compiled["expires_at"],
        "source_revision": compiled["source_revision"],
        "context_json": compiled["context_json"],
        "context_md": compiled["context_md"],
        "delivered_context_md": str(output),
        "preflight": _redact_external_tree(preflight),
        "files": {
            "context": file_info(canonical_md),
            "delivery": file_info(output),
            "entry": file_info(ENTRY_MD),
        },
    }
    _write_context_metadata(args, payload)
    print("context_id=" + str(compiled["context_id"]))
    print("context_md=" + str(output))
    print("context_json=" + str(_metadata_display_path(args)))
    print("context_status=" + str(preflight["context_status"]))
    if preflight["context_status"] == STATUS_DEGRADED:
        print("WARNING: context is degraded; source revision remains authoritative.")
    if args.print:
        print()
        print(content)
    return 0


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
    context.add_argument("--mode", default="auto", choices=("auto", "advisor", "writer", "reviewer", "business", "project", "custom"))
    context.add_argument("--preview-only", action="store_true")
    context.add_argument("--preview-id", default="")
    context.add_argument("--preview-hash", default="")
    context.add_argument("--exclude-item-id", action="append", default=[])
    context.add_argument("--ttl-seconds", type=int, default=900)
    context.add_argument("--metadata-output", default="")
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
