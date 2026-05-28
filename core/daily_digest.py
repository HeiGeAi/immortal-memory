#!/usr/bin/env python3
"""
Daily change digest for the Immortal memory vault.

This script is intentionally read-only for source data. It reads the current
orchestrator state, Feishu coverage files, people index, evidence network,
and backup log tail, then writes the latest digest artifacts under
~/.immortal/digests/.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IMMORTAL_DIR = Path.home() / ".immortal"
STATE_FILE = IMMORTAL_DIR / "orchestrator_state.json"
PEOPLE_INDEX_FILE = IMMORTAL_DIR / "people" / "people_index.json"
RELATIONSHIP_INDEX_FILE = IMMORTAL_DIR / "relationships" / "relationship_index.json"
QUALITY_JSON = IMMORTAL_DIR / "quality" / "latest.json"
FEISHU_DISTILLED_COVERAGE = IMMORTAL_DIR / "feishu" / "distilled" / "coverage.json"
FEISHU_CLEAN_COVERAGE = IMMORTAL_DIR / "feishu" / "clean" / "coverage.json"
BACKUP_LOG = IMMORTAL_DIR / "backup.log"
DIGEST_DIR = IMMORTAL_DIR / "digests"
DIGEST_JSON = DIGEST_DIR / "latest.json"
DIGEST_MD = DIGEST_DIR / "latest.md"


def now_local_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text = f"{text}T00:00:00"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_time(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return str(value) if value else "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def local_date(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return str(value or "")
    if dt.tzinfo is None:
        return dt.date().isoformat()
    return dt.astimezone().date().isoformat()


def file_mtime(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone().replace(microsecond=0).isoformat()


def int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def compact(text: Any, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = redact(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def redact(text: str) -> str:
    patterns = [
        (r"\bcli_[A-Za-z0-9_\-]{8,}\b", "cli_[REDACTED]"),
        (r"(?i)(app secret\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(api\s*key\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(apikey\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(password\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        (r"(?i)(密码\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        (r"sk-[A-Za-z0-9_\-]{12,}", "sk-[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def top_prefixed(counters: dict[str, Any], prefix: str, limit: int = 8) -> list[dict[str, Any]]:
    rows = []
    for key, value in counters.items():
        if key.startswith(prefix):
            rows.append({"name": key[len(prefix):], "count": int_value(value)})
    rows.sort(key=lambda row: row["count"], reverse=True)
    return rows[:limit]


def people_sort_key(person: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(person.get("latest_date") or ""),
        int_value(person.get("memory_count")),
        str(person.get("name") or ""),
    )


def person_snapshot(people: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    snapshot = {}
    for person in people:
        name = str(person.get("name") or "").strip()
        if not name:
            continue
        snapshot[name] = {
            "memory_count": int_value(person.get("memory_count")),
            "latest_date": person.get("latest_date") or "",
            "category": person.get("category") or "",
        }
    return snapshot


def highlight_for(person: dict[str, Any]) -> str:
    latest = str(person.get("latest_date") or "")
    highlights = person.get("highlights") or []
    if not isinstance(highlights, list):
        return ""
    for item in highlights:
        if not isinstance(item, dict):
            continue
        if latest and str(item.get("valid_from") or "") != latest:
            continue
        return compact(item.get("statement"), 150)
    for item in highlights:
        if isinstance(item, dict):
            return compact(item.get("statement"), 150)
    return ""


def summarize_people(people: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    rows = []
    for person in sorted(people, key=people_sort_key, reverse=True)[:limit]:
        rows.append(
            {
                "name": person.get("name") or "",
                "category": person.get("category") or "",
                "latest_date": person.get("latest_date") or "",
                "memory_count": int_value(person.get("memory_count")),
                "highlight": highlight_for(person),
            }
        )
    return rows


def summarize_relationship_edges(edges: list[dict[str, Any]], kind: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = []
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("kind") != kind:
            continue
        rows.append(
            {
                "source": edge.get("source_label") or "",
                "target": edge.get("target_label") or "",
                "kind": edge.get("kind") or "",
                "relation": edge.get("relation_label") or edge.get("label") or "",
                "count": int_value(edge.get("count")),
                "score": edge.get("score") or 0,
                "latest_date": edge.get("latest_date") or "",
                "evidence": compact(((edge.get("evidence") or [{}])[0] or {}).get("statement"), 150),
            }
        )
    rows.sort(key=lambda row: (float(row.get("score") or 0), int_value(row.get("count")), str(row.get("latest_date") or "")), reverse=True)
    return rows[:limit]


def detect_people_changes(
    people: list[dict[str, Any]],
    previous_digest: dict[str, Any],
    fallback_limit: int = 8,
) -> list[dict[str, Any]]:
    previous = previous_digest.get("people_snapshot") if isinstance(previous_digest, dict) else {}
    if not isinstance(previous, dict):
        previous = {}

    changes: list[dict[str, Any]] = []
    current = person_snapshot(people)
    by_name = {str(person.get("name") or ""): person for person in people}

    for name, snapshot in sorted(current.items()):
        old = previous.get(name)
        if not isinstance(old, dict):
            person = by_name.get(name, {})
            changes.append(
                {
                    "name": name,
                    "reason": "new_in_digest_snapshot",
                    "latest_date": snapshot.get("latest_date", ""),
                    "memory_count": snapshot.get("memory_count", 0),
                    "previous_memory_count": None,
                    "highlight": highlight_for(person),
                }
            )
            continue
        old_count = int_value(old.get("memory_count"))
        new_count = int_value(snapshot.get("memory_count"))
        old_date = str(old.get("latest_date") or "")
        new_date = str(snapshot.get("latest_date") or "")
        if old_count != new_count or old_date != new_date:
            person = by_name.get(name, {})
            if old_count != new_count and old_date != new_date:
                reason = "memory_count_and_latest_date_changed"
            elif old_count != new_count:
                reason = "memory_count_changed"
            else:
                reason = "latest_date_changed"
            changes.append(
                {
                    "name": name,
                    "reason": reason,
                    "latest_date": new_date,
                    "memory_count": new_count,
                    "previous_memory_count": old_count,
                    "previous_latest_date": old_date,
                    "highlight": highlight_for(person),
                }
            )

    if changes:
        changes.sort(key=lambda row: (str(row.get("latest_date") or ""), int_value(row.get("memory_count"))), reverse=True)
        return changes[:fallback_limit]

    # First run, or no diff since the last digest: still surface recent evidence.
    recent = summarize_people(people, fallback_limit)
    return [
        {
            "name": row["name"],
            "reason": "recent_person_activity",
            "latest_date": row["latest_date"],
            "memory_count": row["memory_count"],
            "previous_memory_count": row["memory_count"],
            "highlight": row["highlight"],
        }
        for row in recent
    ]


def read_backup_log(path: Path = BACKUP_LOG, limit: int = 120) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "mtime": "",
            "recent_lines": [],
            "recent_warnings": [],
            "last_completion": [],
        }
    tail: deque[str] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line.strip():
                tail.append(redact(line))
    lines = list(tail)
    warning_re = re.compile(r"(失败|错误|警告|Fatal|Timeout|digest failed|failed)", re.I)
    warning_lines = [line for line in lines if warning_re.search(line)]
    warnings = list(dict.fromkeys(warning_lines))[-12:]

    last_completion: list[str] = []
    start = -1
    for idx in range(len(lines) - 1, -1, -1):
        if "编排器完成" in lines[idx]:
            start = idx
            break
    if start >= 0:
        for line in lines[start:start + 12]:
            if last_completion and "编排器启动" in line:
                break
            last_completion.append(line)
            if len(last_completion) >= 8:
                break

    return {
        "path": str(path),
        "exists": True,
        "mtime": file_mtime(path),
        "recent_lines": lines[-20:],
        "recent_warnings": warnings,
        "last_completion": last_completion,
    }


def build_attention(
    state: dict[str, Any],
    clean: dict[str, Any],
    distilled: dict[str, Any],
    quality: dict[str, Any],
    log_info: dict[str, Any],
    people_changes: list[dict[str, Any]],
) -> list[str]:
    items: list[str] = []
    errors = state.get("errors") or []
    if errors:
        items.append("先处理编排器当前错误: " + ", ".join(str(item) for item in errors[:5]))

    last_collect = parse_dt(state.get("last_collect"))
    if last_collect:
        if last_collect.tzinfo is None:
            last_collect = last_collect.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - last_collect.astimezone(timezone.utc)).total_seconds() / 3600
        if age_hours > 30:
            items.append(f"最近采集已超过 {age_hours:.1f} 小时，检查 cron 或手动运行 immortal.py run。")
    else:
        items.append("还没有成功采集时间，先检查 collect/orchestrator 链路。")

    if int_value(state.get("last_run_new_records")) >= 10000:
        items.append("本次新增记录较多，Codex 需要优先关注 brief、timeline 和人物索引的一致性。")

    if int_value(state.get("last_run_feishu_new_records")) > 0 and not state.get("last_feishu_distill"):
        items.append("飞书有新增但缺少 distill 时间，检查 feishu-distill 是否完成。")

    if not clean:
        items.append("Feishu clean coverage 缺失，飞书清洗链路需要补跑。")
    if not distilled:
        items.append("Feishu distilled coverage 缺失，飞书蒸馏链路需要补跑。")

    secret_skipped = int_value((distilled.get("counters") or {}).get("secret_memories_skipped")) if distilled else 0
    if secret_skipped:
        items.append(f"蒸馏层自动跳过 {secret_skipped} 条疑似敏感记忆，保持不进入主画像。")

    if people_changes:
        names = "、".join(str(row.get("name") or "") for row in people_changes[:5] if row.get("name"))
        if names:
            items.append(f"优先查看这些人物的新增/变化证据: {names}。")

    if quality:
        quality_status = str(quality.get("status") or "missing")
        if quality_status == "attention":
            top_titles = [
                str(issue.get("title") or issue.get("id") or "")
                for issue in (quality.get("top_issues") or [])[:3]
                if isinstance(issue, dict)
            ]
            detail = "、".join(title for title in top_titles if title)
            items.append(f"记忆质量层需要 Codex 处理: {detail or '查看 quality/latest.json'}。")
        elif quality_status == "warn":
            items.append("记忆质量层输入不完整，先补齐 people/evidence network 后再判断。")
    else:
        items.append("记忆质量报告缺失，运行 immortal.py quality。")

    recent_warnings = log_info.get("recent_warnings") or []
    if recent_warnings and not errors:
        items.append("backup.log 尾部有历史警告，当前 state 正常；如再次出现再追。")

    if not items:
        items.append("当前链路未见明显错误，关注最新人物变化和飞书覆盖趋势即可。")
    return items[:8]


def build_digest(previous_digest: dict[str, Any] | None = None) -> dict[str, Any]:
    previous_digest = previous_digest or {}
    state = read_json(STATE_FILE, {})
    people_index = read_json(PEOPLE_INDEX_FILE, {})
    relationship_index = read_json(RELATIONSHIP_INDEX_FILE, {})
    quality = read_json(QUALITY_JSON, {})
    clean = read_json(FEISHU_CLEAN_COVERAGE, {})
    distilled = read_json(FEISHU_DISTILLED_COVERAGE, {})
    log_info = read_backup_log()

    people = people_index.get("people") or []
    if not isinstance(people, list):
        people = []
    relationship_summary = relationship_index.get("summary") if isinstance(relationship_index, dict) else {}
    if not isinstance(relationship_summary, dict):
        relationship_summary = {}
    relationship_edges = relationship_index.get("edges") if isinstance(relationship_index, dict) else []
    if not isinstance(relationship_edges, list):
        relationship_edges = []
    clean_counters = clean.get("counters") or {}
    distilled_counters = distilled.get("counters") or {}
    recent_people = summarize_people(people, limit=8)
    possible_changes = detect_people_changes(people, previous_digest, fallback_limit=8)

    current_errors = state.get("errors") or []
    if not isinstance(current_errors, list):
        current_errors = [current_errors]

    digest = {
        "version": "0.1",
        "generated_at": now_local_iso(),
        "sources": {
            "orchestrator_state": {"path": str(STATE_FILE), "exists": STATE_FILE.exists(), "mtime": file_mtime(STATE_FILE)},
            "people_index": {"path": str(PEOPLE_INDEX_FILE), "exists": PEOPLE_INDEX_FILE.exists(), "mtime": file_mtime(PEOPLE_INDEX_FILE)},
            "relationship_index": {
                "path": str(RELATIONSHIP_INDEX_FILE),
                "exists": RELATIONSHIP_INDEX_FILE.exists(),
                "mtime": file_mtime(RELATIONSHIP_INDEX_FILE),
            },
            "quality_report": {
                "path": str(QUALITY_JSON),
                "exists": QUALITY_JSON.exists(),
                "mtime": file_mtime(QUALITY_JSON),
            },
            "feishu_clean_coverage": {
                "path": str(FEISHU_CLEAN_COVERAGE),
                "exists": FEISHU_CLEAN_COVERAGE.exists(),
                "mtime": file_mtime(FEISHU_CLEAN_COVERAGE),
            },
            "feishu_distilled_coverage": {
                "path": str(FEISHU_DISTILLED_COVERAGE),
                "exists": FEISHU_DISTILLED_COVERAGE.exists(),
                "mtime": file_mtime(FEISHU_DISTILLED_COVERAGE),
            },
            "backup_log": {"path": str(BACKUP_LOG), "exists": BACKUP_LOG.exists(), "mtime": file_mtime(BACKUP_LOG)},
        },
        "summary": {
            "recent_collect_time": state.get("last_collect"),
            "recent_collect_time_local": format_time(state.get("last_collect")),
            "total_records": int_value(state.get("total_records")),
            "new_records": int_value(state.get("last_run_new_records")),
            "feishu_new_records": int_value(state.get("last_run_feishu_new_records")),
            "collect_count": int_value(state.get("collect_count")),
            "last_summary": state.get("last_summary"),
            "last_people_index": state.get("last_people_index"),
            "last_relationship_index": state.get("last_relationship_index"),
        },
        "feishu": {
            "last_collect": state.get("last_feishu_collect"),
            "last_clean": state.get("last_feishu_clean"),
            "last_distill": state.get("last_feishu_distill"),
            "clean": {
                "generated_at": clean.get("generated_at", ""),
                "raw_records": int_value(clean_counters.get("raw_feishu_records")),
                "clean_records": int_value(clean_counters.get("clean_records")),
                "valuable_clean_records": int_value(clean_counters.get("valuable_clean_records")),
                "candidate_memories": int_value(clean_counters.get("candidate_memories")),
                "chat_daily_rows": int_value(clean_counters.get("chat_daily_rows")),
                "top_sources": top_prefixed(clean_counters, "clean_source:", 8),
            },
            "distilled": {
                "generated_at": distilled.get("generated_at", ""),
                "candidate_rows_scanned": int_value(distilled_counters.get("candidates_scanned")),
                "candidate_rows_used": int_value(distilled_counters.get("candidates_used")),
                "memories": int_value(distilled_counters.get("memories_written")),
                "profile_memories": int_value(distilled_counters.get("profile_memories_written")),
                "reference_memories": int_value(distilled_counters.get("reference_memories_written")),
                "secret_skipped": int_value(distilled_counters.get("secret_memories_skipped")),
                "top_projects": top_prefixed(distilled_counters, "project:", 8),
                "top_memory_types": top_prefixed(distilled_counters, "memory_type:", 8),
            },
        },
        "people": {
            "count": len(people),
            "generated_at": people_index.get("generated_at", ""),
            "latest_date": max((str(person.get("latest_date") or "") for person in people), default=""),
            "recently_updated": recent_people,
            "possible_new_or_changed": possible_changes,
        },
        "relationships": {
            "generated_at": relationship_index.get("generated_at", "") if isinstance(relationship_index, dict) else "",
            "people_nodes": int_value(relationship_summary.get("people_count")),
            "project_nodes": int_value(relationship_summary.get("project_count")),
            "person_edges": int_value(relationship_summary.get("person_edges")),
            "project_edges": int_value(relationship_summary.get("project_edges")),
            "top_people_edges": summarize_relationship_edges(relationship_edges, "person_person", 5),
            "top_project_edges": summarize_relationship_edges(relationship_edges, "person_project", 5),
            "skipped": relationship_summary.get("skipped") if isinstance(relationship_summary.get("skipped"), dict) else {},
        },
        "quality": {
            "generated_at": quality.get("generated_at", "") if isinstance(quality, dict) else "",
            "status": quality.get("status", "missing") if isinstance(quality, dict) else "missing",
            "status_label": quality.get("status_label", "") if isinstance(quality, dict) else "",
            "score": int_value(quality.get("score")) if isinstance(quality, dict) else 0,
            "issue_count": int_value(quality.get("issue_count")) if isinstance(quality, dict) else 0,
            "severity_counts": quality.get("severity_counts", {}) if isinstance(quality.get("severity_counts"), dict) else {},
            "recommendation": quality.get("recommendation", "") if isinstance(quality, dict) else "",
            "top_issues": [
                {
                    "id": issue.get("id", ""),
                    "area": issue.get("area", ""),
                    "severity": issue.get("severity", ""),
                    "title": issue.get("title", ""),
                    "detail": compact(issue.get("detail"), 180),
                    "suggested_action": compact(issue.get("suggested_action"), 180),
                }
                for issue in (quality.get("top_issues") or [])[:6]
                if isinstance(issue, dict)
            ] if isinstance(quality, dict) else [],
            "relationship_metrics": ((quality.get("relationships") or {}).get("metrics") or {}) if isinstance(quality, dict) else {},
            "identity_metrics": ((quality.get("identity") or {}).get("metrics") or {}) if isinstance(quality, dict) else {},
        },
        "errors": {
            "status": "ok" if not current_errors else "attention",
            "current": [str(item) for item in current_errors],
            "recent_log_warnings": log_info.get("recent_warnings") or [],
        },
        "backup_log": log_info,
        "people_snapshot": person_snapshot(people),
    }
    digest["attention"] = build_attention(state, clean, distilled, quality, log_info, possible_changes)
    return digest


def bullet_people(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- 暂无"]
    lines = []
    for row in rows:
        detail = f"{row.get('name', '')}｜{row.get('latest_date') or '-'}｜{row.get('memory_count', 0)} 条"
        reason = row.get("reason")
        if reason:
            detail += f"｜{reason}"
        highlight = row.get("highlight")
        if highlight:
            detail += f"：{highlight}"
        lines.append(f"- {detail}")
    return lines


def bullet_counter(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "暂无"
    return "；".join(f"{row.get('name')}: {row.get('count')}" for row in rows)


def bullet_relationship_edges(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- 暂无"]
    lines = []
    for row in rows:
        connector = " → " if row.get("kind") == "person_project" else " ↔ "
        detail = (
            f"{row.get('source') or '-'}{connector}{row.get('target') or '-'}"
            f"｜{row.get('relation') or '-'}｜{row.get('count', 0)} 条｜score {row.get('score')}"
        )
        if row.get("evidence"):
            detail += f"：{row.get('evidence')}"
        lines.append(f"- {detail}")
    return lines


def bullet_quality_issues(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- 暂无"]
    lines = []
    for issue in rows:
        lines.append(
            "- "
            f"[{str(issue.get('severity') or 'low').upper()}] "
            f"{issue.get('title') or issue.get('id')}: "
            f"{issue.get('detail') or ''} "
            f"建议：{issue.get('suggested_action') or ''}"
        )
    return lines


def render_markdown(digest: dict[str, Any]) -> str:
    summary = digest["summary"]
    feishu = digest["feishu"]
    people = digest["people"]
    relationships = digest.get("relationships") or {}
    quality = digest.get("quality") or {}
    errors = digest["errors"]
    log_info = digest["backup_log"]

    lines = [
        "# 每日变化摘要",
        "",
        f"Generated: {digest.get('generated_at')}",
        "",
        "## 总览",
        f"- 最近采集时间：{summary.get('recent_collect_time_local')}",
        f"- 总记录：{summary.get('total_records'):,}",
        f"- 本次新增：{summary.get('new_records'):,}",
        f"- 飞书新增：{summary.get('feishu_new_records'):,}",
        f"- 人物数量：{people.get('count')}",
        f"- 关联证据边：人物 {relationships.get('person_edges', 0)}；项目 {relationships.get('project_edges', 0)}",
        f"- 记忆质量：{quality.get('status_label') or quality.get('status') or 'missing'}；分数 {quality.get('score', 0)}/100；问题 {quality.get('issue_count', 0)}",
        "",
        "## 飞书链路",
        f"- 最近飞书采集：{format_time(feishu.get('last_collect'))}",
        f"- Clean 生成：{format_time(feishu['clean'].get('generated_at'))}",
        f"- Distill 生成：{format_time(feishu['distilled'].get('generated_at'))}",
        f"- Clean records：{feishu['clean'].get('clean_records'):,}；候选记忆：{feishu['clean'].get('candidate_memories'):,}",
        f"- Distilled memories：{feishu['distilled'].get('memories'):,}；profile：{feishu['distilled'].get('profile_memories'):,}；reference：{feishu['distilled'].get('reference_memories'):,}",
        f"- 主要来源：{bullet_counter(feishu['clean'].get('top_sources') or [])}",
        "",
        "## 最近更新人物",
        *bullet_people(people.get("recently_updated") or []),
        "",
        "## 可能新增/变化人物",
        *bullet_people(people.get("possible_new_or_changed") or []),
        "",
        "## 关联证据",
        f"- 生成时间：{format_time(relationships.get('generated_at'))}",
        f"- 人物节点：{relationships.get('people_nodes', 0)}；项目节点：{relationships.get('project_nodes', 0)}",
        "- 高信号人物证据：",
        *bullet_relationship_edges(relationships.get("top_people_edges") or []),
        "- 高信号项目证据：",
        *bullet_relationship_edges(relationships.get("top_project_edges") or []),
        "",
        "## 记忆质量",
        f"- 生成时间：{format_time(quality.get('generated_at'))}",
        f"- 状态：{quality.get('status_label') or quality.get('status') or 'missing'}",
        f"- 质量分：{quality.get('score', 0)}/100",
        f"- 问题数：{quality.get('issue_count', 0)}",
        f"- 建议：{quality.get('recommendation') or '暂无'}",
        "- Top 问题：",
        *bullet_quality_issues(quality.get("top_issues") or []),
        "",
        "## 错误状态",
        f"- 当前状态：{errors.get('status')}",
    ]

    current_errors = errors.get("current") or []
    if current_errors:
        lines.extend(f"- 当前错误：{item}" for item in current_errors)
    else:
        lines.append("- 当前错误：none")

    recent_log_warnings = errors.get("recent_log_warnings") or []
    if recent_log_warnings:
        lines.append("- 日志尾部警告：")
        lines.extend(f"  - {line}" for line in recent_log_warnings[-5:])
    else:
        lines.append("- 日志尾部警告：none")

    lines.extend([
        "",
        "## 建议关注项",
        *(f"- {item}" for item in digest.get("attention") or []),
        "",
        "## backup.log 最近信息",
    ])
    last_completion = log_info.get("last_completion") or []
    if last_completion:
        lines.extend(f"- {line}" for line in last_completion)
    else:
        lines.append("- 未找到最近完成块")

    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Immortal daily change digest")
    parser.add_argument("--json", default=str(DIGEST_JSON), help="output JSON path")
    parser.add_argument("--md", default=str(DIGEST_MD), help="output Markdown path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    json_path = Path(args.json).expanduser()
    md_path = Path(args.md).expanduser()
    previous = read_json(json_path, {}) if json_path.exists() else {}
    digest = build_digest(previous)
    write_json_atomic(json_path, digest)
    write_text_atomic(md_path, render_markdown(digest))
    summary = digest["summary"]
    people = digest["people"]
    print(f"digest_json={json_path}")
    print(f"digest_md={md_path}")
    print(
        "summary="
        f"total_records={summary.get('total_records')} "
        f"new_records={summary.get('new_records')} "
        f"feishu_new_records={summary.get('feishu_new_records')} "
        f"people={people.get('count')} "
        f"person_edges={(digest.get('relationships') or {}).get('person_edges')} "
        f"project_edges={(digest.get('relationships') or {}).get('project_edges')} "
        f"quality={(digest.get('quality') or {}).get('status')} "
        f"errors={digest['errors'].get('status')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
