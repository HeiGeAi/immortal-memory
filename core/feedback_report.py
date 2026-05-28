#!/usr/bin/env python3
"""Generate a concise user-facing feedback report after an Immortal run."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import configured_vault_dir, load_config


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def local_time(value: str | None) -> str:
    dt = parse_iso(value)
    if not dt:
        return value or "unknown"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def top_issue_line(issue: dict[str, Any]) -> str:
    title = str(issue.get("title") or issue.get("id") or "未命名问题").strip()
    detail = str(issue.get("detail") or "").strip()
    severity = str(issue.get("severity") or "").strip().upper()
    prefix = f"[{severity}] " if severity else ""
    return f"{prefix}{title}: {detail}" if detail else f"{prefix}{title}"


def build_report(vault: Path, run_status: int | None = None) -> dict[str, Any]:
    digest_path = vault / "digests" / "latest.json"
    state_path = vault / "orchestrator_state.json"
    digest = read_json(digest_path, {})
    state = read_json(state_path, {})
    summary = digest.get("summary") if isinstance(digest.get("summary"), dict) else {}
    feishu = digest.get("feishu") if isinstance(digest.get("feishu"), dict) else {}
    quality = digest.get("quality") if isinstance(digest.get("quality"), dict) else {}
    errors = digest.get("errors") if isinstance(digest.get("errors"), dict) else {}
    people = digest.get("people") if isinstance(digest.get("people"), dict) else {}

    issue_count = int_value(quality.get("issue_count"))
    quality_status = str(quality.get("status") or "missing")
    error_status = str(errors.get("status") or "missing")
    current_errors = errors.get("current") if isinstance(errors.get("current"), list) else []
    top_issues = quality.get("top_issues") if isinstance(quality.get("top_issues"), list) else []
    attention = digest.get("attention") if isinstance(digest.get("attention"), list) else []

    if run_status is not None and run_status != 0:
        status = "failed"
        status_label = "运行失败"
    elif current_errors or error_status not in {"ok", "missing"}:
        status = "failed"
        status_label = "存在错误"
    elif quality_status == "attention" or issue_count > 0:
        status = "attention"
        status_label = "需要关注"
    else:
        status = "ok"
        status_label = "正常"

    recently_updated = people.get("recently_updated") if isinstance(people.get("recently_updated"), list) else []
    recent_people = [
        {
            "name": str(item.get("name") or ""),
            "latest_date": str(item.get("latest_date") or ""),
            "memory_count": int_value(item.get("memory_count")),
        }
        for item in recently_updated[:8]
        if isinstance(item, dict)
    ]

    report = {
        "version": "0.1",
        "generated_at": now_local().isoformat(timespec="seconds"),
        "status": status,
        "status_label": status_label,
        "run_status": run_status,
        "summary": {
            "generated_at": digest.get("generated_at"),
            "recent_collect_time": summary.get("recent_collect_time"),
            "recent_collect_time_local": summary.get("recent_collect_time_local"),
            "total_records": int_value(summary.get("total_records")),
            "new_records": int_value(summary.get("new_records")),
            "feishu_new_records": int_value(summary.get("feishu_new_records")),
            "collect_count": int_value(summary.get("collect_count")),
        },
        "feishu": {
            "last_collect": feishu.get("last_collect") or state.get("last_feishu_collect"),
            "last_clean": (feishu.get("clean") or {}).get("generated_at") if isinstance(feishu.get("clean"), dict) else None,
            "last_distill": (feishu.get("distilled") or {}).get("generated_at") if isinstance(feishu.get("distilled"), dict) else None,
            "clean_records": int_value((feishu.get("clean") or {}).get("clean_records") if isinstance(feishu.get("clean"), dict) else None),
            "distilled_memories": int_value((feishu.get("distilled") or {}).get("memories") if isinstance(feishu.get("distilled"), dict) else None),
        },
        "quality": {
            "status": quality_status,
            "status_label": str(quality.get("status_label") or status_label),
            "score": int_value(quality.get("score")),
            "issue_count": issue_count,
            "top_issues": [top_issue_line(item) for item in top_issues[:5] if isinstance(item, dict)],
            "recommendation": str(quality.get("recommendation") or ""),
        },
        "errors": {
            "status": error_status,
            "current": current_errors[:8],
            "recent_log_warnings": (errors.get("recent_log_warnings") or [])[:5],
        },
        "people": {
            "count": int_value(people.get("count")),
            "recently_updated": recent_people,
        },
        "attention": [str(item) for item in attention[:8]],
        "paths": {
            "digest_md": str(vault / "digests" / "latest.md"),
            "dashboard": str(vault / "dashboard.html"),
            "timeline": str(vault / "timeline.html"),
            "quality_json": str(vault / "quality" / "latest.json"),
        },
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    feishu = report["feishu"]
    quality = report["quality"]
    errors = report["errors"]
    people = report["people"]
    lines = [
        "# 永生记忆库运行反馈",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## 结论",
        f"- 状态：{report['status_label']}",
        f"- 本次新增：{summary['new_records']:,} 条",
        f"- 飞书新增：{summary['feishu_new_records']:,} 条",
        f"- 总记录：{summary['total_records']:,} 条",
        f"- 质量：{quality['status_label']}；{quality['score']}/100；问题 {quality['issue_count']} 个",
        f"- 错误：{errors['status']}",
        "",
        "## 自动化链路",
        f"- 最近采集：{local_time(summary.get('recent_collect_time') or summary.get('recent_collect_time_local'))}",
        f"- 采集次数：{summary['collect_count']:,}",
        f"- 飞书最近采集：{local_time(feishu.get('last_collect'))}",
        f"- 飞书 Clean：{local_time(feishu.get('last_clean'))}；clean records {feishu['clean_records']:,}",
        f"- 飞书 Distill：{local_time(feishu.get('last_distill'))}；distilled memories {feishu['distilled_memories']:,}",
        "",
        "## 质量关注",
    ]
    if quality["top_issues"]:
        lines.extend(f"- {item}" for item in quality["top_issues"])
    else:
        lines.append("- 暂无质量问题。")
    if quality["recommendation"]:
        lines.append(f"- 建议：{quality['recommendation']}")
    lines.extend(["", "## 最近更新人物"])
    recent_people = people.get("recently_updated") or []
    if recent_people:
        for item in recent_people:
            lines.append(f"- {item['name']}｜{item['latest_date']}｜{item['memory_count']:,} 条")
    else:
        lines.append("- 暂无。")
    lines.extend(["", "## 提醒"])
    attention = report.get("attention") or []
    if attention:
        lines.extend(f"- {item}" for item in attention)
    else:
        lines.append("- 暂无。")
    if errors.get("current"):
        lines.extend(["", "## 当前错误"])
        lines.extend(f"- {item}" for item in errors["current"])
    if errors.get("recent_log_warnings"):
        lines.extend(["", "## 近期日志警告"])
        lines.extend(f"- {item}" for item in errors["recent_log_warnings"])
    lines.extend(
        [
            "",
            "## 文件",
            f"- Digest：{report['paths']['digest_md']}",
            f"- Dashboard：{report['paths']['dashboard']}",
            f"- Timeline：{report['paths']['timeline']}",
            f"- Quality：{report['paths']['quality_json']}",
            "",
        ]
    )
    return "\n".join(lines)


def apple_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def send_notification(report: dict[str, Any]) -> tuple[bool, str]:
    summary = report["summary"]
    quality = report["quality"]
    if report["status"] == "failed":
        title = "永生记忆库运行异常"
        message = f"自动任务未完全成功；质量 {quality['score']}/100，问题 {quality['issue_count']} 个。"
    elif report["status"] == "attention":
        title = "永生记忆库已更新：需要关注"
        message = f"新增 {summary['new_records']:,} 条，飞书 {summary['feishu_new_records']:,} 条，质量 {quality['score']}/100。"
    else:
        title = "永生记忆库已更新"
        message = f"新增 {summary['new_records']:,} 条，飞书 {summary['feishu_new_records']:,} 条，总记录 {summary['total_records']:,}。"
    script = f"display notification {apple_string(message)} with title {apple_string(title)}"
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
    except Exception as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "osascript failed").strip()
    return True, "sent"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Immortal run feedback")
    parser.add_argument("--vault-dir", default=None)
    parser.add_argument("--run-status", type=int, default=None)
    parser.add_argument("--notify", action="store_true", help="Send a local macOS notification")
    parser.add_argument("--print", action="store_true", help="Print the full markdown report")
    args = parser.parse_args()

    config = load_config()
    vault = Path(args.vault_dir).expanduser() if args.vault_dir else configured_vault_dir(config)
    report = build_report(vault, run_status=args.run_status)
    markdown = render_markdown(report)

    feedback_dir = vault / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    latest_json = feedback_dir / "latest.json"
    latest_md = feedback_dir / "latest.md"
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    history_md = feedback_dir / f"{stamp}.md"
    write_json(latest_json, report)
    latest_md.write_text(markdown, encoding="utf-8")
    history_md.write_text(markdown, encoding="utf-8")

    notification_status = "skipped"
    if args.notify:
        ok, detail = send_notification(report)
        notification_status = detail if ok else f"failed: {detail}"

    print("Immortal feedback report")
    print(f"Status: {report['status_label']}")
    print(f"Summary: new={report['summary']['new_records']} feishu_new={report['summary']['feishu_new_records']} total={report['summary']['total_records']} quality={report['quality']['score']} issues={report['quality']['issue_count']}")
    print(f"Report: {latest_md}")
    print(f"Notification: {notification_status}")
    if args.print:
        print()
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
