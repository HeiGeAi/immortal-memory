#!/usr/bin/env python3
"""Build and optionally send an owner-only learning review reminder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_bridge import _canonical_system_alias_path, redact_external_text
from claim_store import ClaimStore
from config import configured_vault_dir, load_config
from judgment_store import JudgmentStore


REVIEW_URL = "http://127.0.0.1:8765/?view=home"
OPEN_ID = re.compile(r"^ou_[A-Za-z0-9]+$")


def _summary(value: Any) -> str:
    text = redact_external_text(value, max_chars=180)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("<", "＜").replace(">", "＞") or "内容为空，需在本机核对"


def build_review(
    vault_dir: Path,
    *,
    limit: int = 8,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    vault_dir = _canonical_system_alias_path(Path(vault_dir))
    claims = [row for row in ClaimStore(vault_dir).list() if row.get("status") == "candidate"]
    judgments = [row for row in JudgmentStore(vault_dir).list() if row.get("status") == "candidate"]
    items = [
        {
            "kind": "claim",
            "summary": _summary(row.get("statement")),
            "revision": row.get("revision"),
            "updated_at": str(row.get("updated_at") or ""),
        }
        for row in claims
    ]
    items.extend(
        {
            "kind": "judgment",
            "summary": _summary(row.get("title")),
            "revision": row.get("revision"),
            "updated_at": str(row.get("updated_at") or ""),
        }
        for row in judgments
    )
    items.sort(key=lambda row: (row["updated_at"], row["kind"]), reverse=True)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return {
        "schema_version": 1,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "counts": {
            "total": len(items),
            "claims": len(claims),
            "judgments": len(judgments),
            "visible": min(len(items), limit),
        },
        "items": items[:limit],
        "review_url": REVIEW_URL,
    }


def render_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "### Immortal 学习审核",
        "",
        f"当前有 {counts['total']} 条待确认：候选理解 {counts['claims']} 条，候选判断 {counts['judgments']} 条。",
    ]
    for index, item in enumerate(report.get("items") or [], 1):
        label = "候选理解" if item.get("kind") == "claim" else "候选判断"
        lines.append(f"{index}. 【{label}】{item.get('summary') or '内容为空'}")
    if counts["total"] > counts["visible"]:
        lines.append(f"当前展示 {counts['visible']} 条，其余内容请在本机继续审核。")
    lines.extend(
        [
            "",
            f"[在安装 Immortal 的电脑上打开审核面板]({report['review_url']})",
            "",
            "只有本人确认后的内容才会进入 Living Self 和 Agent Context。",
        ]
    )
    return "\n".join(lines)


def send_to_feishu(
    report: dict[str, Any],
    recipient_open_id: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not OPEN_ID.fullmatch(str(recipient_open_id or "")):
        raise ValueError("configured Feishu owner open_id is invalid")
    executable = shutil.which("lark-cli")
    if not executable:
        raise RuntimeError("lark-cli is not installed")
    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    idempotency = "immortal-review-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    command = [
        executable,
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--user-id",
        recipient_open_id,
        "--markdown",
        render_markdown(report),
        "--idempotency-key",
        idempotency,
    ]
    if dry_run:
        command.append("--dry-run")
    environment = dict(os.environ)
    environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    if completed.returncode != 0:
        detail = redact_external_text(completed.stderr or completed.stdout, max_chars=800)
        raise RuntimeError("Feishu delivery failed: " + detail)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Feishu delivery returned invalid JSON") from exc
    if payload.get("ok") is not True:
        raise RuntimeError("Feishu delivery was not acknowledged")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "dry_run": dry_run,
        "message_id": str(data.get("message_id") or ""),
        "chat_id": str(data.get("chat_id") or ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or send owner-only learning review reminders")
    parser.add_argument("--vault-dir", default=None)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--send-feishu", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-remote-write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run and not args.send_feishu:
        print("--dry-run requires --send-feishu", file=sys.stderr)
        return 2
    if args.send_feishu and not args.dry_run and not args.confirm_remote_write:
        print("Feishu delivery requires --confirm-remote-write", file=sys.stderr)
        return 2
    try:
        config = load_config()
        vault_dir = Path(args.vault_dir).expanduser() if args.vault_dir else configured_vault_dir(config)
        report = build_review(vault_dir, limit=args.limit)
        receipt = None
        if args.send_feishu:
            feishu = config.get("feishu") if isinstance(config.get("feishu"), dict) else {}
            recipient = str(feishu.get("expected_user_open_id") or "")
            receipt = send_to_feishu(report, recipient, dry_run=args.dry_run)
        if args.json:
            print(json.dumps({"review": report, "delivery": receipt}, ensure_ascii=False, indent=2))
        elif receipt is not None:
            print("delivery=dry-run" if receipt["dry_run"] else "message_id=" + receipt["message_id"])
        else:
            print(render_markdown(report))
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(redact_external_text(exc, max_chars=800), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
