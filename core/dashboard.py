#!/usr/bin/env python3
"""
永生记忆库 — 看板生成器 v0.2
人类友好的全功能 HTML 看板：
  - 总览统计（数据量、活跃度、采集状态）
  - 数字人格预览（含目录跳转）
  - 摘要全文展示（可点击展开）
  - 关键词搜索（前端预加载的轻量索引）
  - 30 天趋势图
  - 数据源详情（按 Agent 分组）
"""

from __future__ import annotations

import json
import re
import html
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from export_restore import get_backup_status


IMMORTAL_DIR = Path.home() / ".immortal"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
STATE_FILE = IMMORTAL_DIR / "orchestrator_state.json"
SOURCES_FILE = IMMORTAL_DIR / "sources.json"
SOUL_FILE = IMMORTAL_DIR / "digital-soul.md"
AGENT_ENTRY_FILE = IMMORTAL_DIR / "agent" / "ENTRY.md"
LATEST_AGENT_CONTEXT_FILE = IMMORTAL_DIR / "agent" / "latest-context.md"
SUMMARY_DIR = IMMORTAL_DIR / "summaries"
OUTPUT_FILE = IMMORTAL_DIR / "dashboard.html"
FEISHU_CLEAN_COVERAGE = IMMORTAL_DIR / "feishu" / "clean" / "coverage.json"
FEISHU_DISTILLED_COVERAGE = IMMORTAL_DIR / "feishu" / "distilled" / "coverage.json"
FEISHU_PROFILE_MERGE = IMMORTAL_DIR / "feishu" / "distilled" / "profile_merge_proposal.md"
REVIEWED_PROFILE_MEMORIES = IMMORTAL_DIR / "reviewed" / "profile_memories.jsonl"
REVIEWED_PROFILE_MD = IMMORTAL_DIR / "reviewed" / "profile_memories.md"
PROFILE_REVIEW_STATE = IMMORTAL_DIR / "reviewed" / "profile_review_state.json"
PEOPLE_INDEX_FILE = IMMORTAL_DIR / "people" / "people_index.json"
RELATIONSHIP_INDEX_FILE = IMMORTAL_DIR / "relationships" / "relationship_index.json"
QUALITY_FILE = IMMORTAL_DIR / "quality" / "latest.json"
DIGEST_FILE = IMMORTAL_DIR / "digests" / "latest.json"
FEEDBACK_FILE = IMMORTAL_DIR / "feedback" / "latest.json"
DASHBOARD_EXCLUDED_SAMPLE_TERMS = {"错误账号"}


def fmt_bytes(size: int | float | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def parse_iso(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def format_local_time(value: str | None) -> str:
    dt = parse_iso(value)
    if not dt:
        return value or "missing"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def backup_display(status: dict[str, Any]) -> dict[str, str | bool | int]:
    latest = status.get("latest_export") or {}
    totals = latest.get("totals") if isinstance(latest, dict) else {}
    warnings = status.get("warnings") or (status.get("check") or {}).get("warnings") or []
    files = int((totals or {}).get("files") or 0)
    size = int((totals or {}).get("bytes") or 0)
    generated = latest.get("generated_at") or ""
    if not status.get("ok"):
        return {
            "ok": False,
            "title": "未生成",
            "time": "missing",
            "files": files,
            "size": fmt_bytes(size),
            "detail": ", ".join(map(str, warnings[:3])) or "no portable export found",
            "path": latest.get("export_dir") or "",
        }
    return {
        "ok": True,
        "title": format_local_time(generated),
        "time": generated,
        "files": files,
        "size": fmt_bytes(size),
        "detail": f"{files:,} files · {fmt_bytes(size)} · {status.get('mode', 'manifest-only')}",
        "path": latest.get("export_dir") or "",
    }


def load_index_stats():
    """扫描索引，提取统计 + 搜索索引（采样）。"""
    if not INDEX_FILE.exists():
        return None

    sources = Counter()
    types = Counter()
    roles = Counter()
    dates = Counter()
    user_topics = Counter()
    total = 0

    # 采样数据（用户真实发言中的代表性句子，作为前端搜索源）
    search_samples = []
    sample_quota_per_day = 10  # 每天最多采样 10 条
    daily_sample_count = Counter()

    for line in open(INDEX_FILE, "r", encoding="utf-8"):
        try:
            r = json.loads(line.strip())
            total += 1
            sources[r.get("source", "?")] += 1
            types[r.get("type", "?")] += 1
            roles[r.get("role", "?")] += 1
            ts = r.get("timestamp", "")
            date = ts[:10] if ts else ""
            if date:
                dates[date] += 1

            # 采样真实用户发言
            if r.get("role") == "user" and date:
                content = r.get("content", "").strip()
                if any(term in content for term in DASHBOARD_EXCLUDED_SAMPLE_TERMS):
                    continue
                if 30 < len(content) < 500 and not content.startswith(("File ", "Result ", "/", "{")) and "tool with" not in content[:80]:
                    if daily_sample_count[date] < sample_quota_per_day:
                        search_samples.append({
                            "date": date,
                            "source": r.get("source", ""),
                            "content": content[:300],
                        })
                        daily_sample_count[date] += 1
        except:
            continue

    return {
        "total": total,
        "sources": dict(sources.most_common(20)),
        "types": dict(types.most_common(10)),
        "roles": dict(roles),
        "dates": dict(sorted(dates.items(), reverse=True)),
        "search_samples": search_samples,
    }


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def load_summaries():
    """加载所有摘要，最新在前。"""
    summaries = []
    if SUMMARY_DIR.exists():
        for f in sorted(SUMMARY_DIR.glob("*.md"), reverse=True):
            try:
                content = f.read_text(encoding="utf-8")
                summaries.append({
                    "date": f.stem,
                    "content": content,
                })
            except:
                continue
    return summaries


def load_soul():
    if SOUL_FILE.exists():
        return SOUL_FILE.read_text(encoding="utf-8")
    return ""


def load_agent_entry():
    if AGENT_ENTRY_FILE.exists():
        return AGENT_ENTRY_FILE.read_text(encoding="utf-8", errors="ignore")
    return (
        "# Immortal Agent Entry\n\n"
        "Agent 接入文件还未生成。运行：\n\n"
        "`python3 ~/.codex/skills/immortal/immortal.py agent-entry`\n"
    )


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_feishu_metrics():
    clean = read_json(FEISHU_CLEAN_COVERAGE)
    distilled = read_json(FEISHU_DISTILLED_COVERAGE)
    review_state = read_json(PROFILE_REVIEW_STATE)
    clean_counters = clean.get("counters", {})
    distilled_counters = distilled.get("counters", {})
    proposal = ""
    if FEISHU_PROFILE_MERGE.exists():
        proposal = FEISHU_PROFILE_MERGE.read_text(encoding="utf-8", errors="ignore")
    preview_lines = proposal.splitlines()[:160]
    proposal_ids = set(re.findall(r"`([a-f0-9]{24})`", proposal))
    checked_ids = set(re.findall(r"^-\s*\[[xX]\]\s+`([a-f0-9]{24})`", proposal, re.M))
    reviewed_count = 0
    reviewed_ids = set()
    if REVIEWED_PROFILE_MEMORIES.exists():
        try:
            for line in REVIEWED_PROFILE_MEMORIES.open("r", encoding="utf-8", errors="ignore"):
                reviewed_count += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                memory_id = row.get("memory_id") if isinstance(row, dict) else None
                if memory_id:
                    reviewed_ids.add(str(memory_id))
        except Exception:
            reviewed_count = 0
            reviewed_ids = set()
    reviewed_preview = ""
    if REVIEWED_PROFILE_MD.exists():
        reviewed_preview = "\n".join(REVIEWED_PROFILE_MD.read_text(encoding="utf-8", errors="ignore").splitlines()[:180])
    rejected_count = 0
    rejected_ids = set()
    if isinstance(review_state, dict) and isinstance(review_state.get("rejected"), dict):
        rejected_count = len(review_state["rejected"])
        rejected_ids = set(str(memory_id) for memory_id in review_state["rejected"])
    pending_review = len(proposal_ids - checked_ids - rejected_ids - reviewed_ids)
    return {
        "clean_records": clean_counters.get("records_written", clean_counters.get("clean_records", 0)),
        "candidate_memories": clean_counters.get("candidate_memories_written", clean_counters.get("candidate_memories", 0)),
        "chat_daily": clean_counters.get("chat_daily_rows", clean_counters.get("chat_daily_written", 0)),
        "memories": distilled_counters.get("memories_written", 0),
        "profile_memories": distilled_counters.get("profile_memories_written", 0),
        "reference_memories": distilled_counters.get("reference_memories_written", 0),
        "secret_skipped": distilled_counters.get("secret_memories_skipped", 0),
        "proposal_candidates": len(proposal_ids),
        "proposal_checked": len(checked_ids),
        "reviewed_profile": reviewed_count,
        "review_rejected": rejected_count,
        "pending_review": pending_review,
        "generated_at": distilled.get("generated_at", ""),
        "proposal_path": str(FEISHU_PROFILE_MERGE),
        "reviewed_path": str(REVIEWED_PROFILE_MD),
        "review_url": "http://127.0.0.1:8765/",
        "review_command": "python3 ~/.codex/skills/immortal/immortal.py profile-auto-review",
        "audit_command": "python3 ~/.codex/skills/immortal/immortal.py profile-review --open",
        "proposal_size_kb": FEISHU_PROFILE_MERGE.stat().st_size / 1024 if FEISHU_PROFILE_MERGE.exists() else 0,
        "proposal_preview": "\n".join(preview_lines),
        "reviewed_preview": reviewed_preview,
    }


def load_people_index():
    data = read_json(PEOPLE_INDEX_FILE)
    if not isinstance(data, dict):
        data = {}
    people = data.get("people")
    if not isinstance(people, list):
        people = []
    categories = Counter(str(person.get("category") or "other") for person in people if isinstance(person, dict))
    memory_total = sum(int(person.get("memory_count") or 0) for person in people if isinstance(person, dict))
    latest = ""
    for person in people:
        if not isinstance(person, dict):
            continue
        date = str(person.get("latest_date") or "")
        if date > latest:
            latest = date
    return {
        "generated_at": data.get("generated_at", ""),
        "people": people,
        "count": len(people),
        "memory_total": memory_total,
        "categories": dict(categories),
        "latest_date": latest,
        "path": str(PEOPLE_INDEX_FILE),
    }


def load_relationship_index():
    data = read_json(RELATIONSHIP_INDEX_FILE)
    if not isinstance(data, dict):
        data = {}
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        nodes = []
    edges = data.get("edges")
    if not isinstance(edges, list):
        edges = []
    summary = data.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    people_nodes = [node for node in nodes if isinstance(node, dict) and node.get("kind") == "person"]
    project_nodes = [node for node in nodes if isinstance(node, dict) and node.get("kind") == "project"]
    person_edges = [edge for edge in edges if isinstance(edge, dict) and edge.get("kind") == "person_person"]
    project_edges = [edge for edge in edges if isinstance(edge, dict) and edge.get("kind") == "person_project"]
    latest = ""
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        date = str(edge.get("latest_date") or "")
        if date > latest:
            latest = date
    return {
        "generated_at": data.get("generated_at", ""),
        "basis": data.get("basis", ""),
        "nodes": nodes,
        "edges": edges,
        "people_nodes": len(people_nodes),
        "project_nodes": len(project_nodes),
        "person_edges": len(person_edges),
        "project_edges": len(project_edges),
        "latest_date": latest,
        "summary": summary,
        "path": str(RELATIONSHIP_INDEX_FILE),
    }


def relationship_edge_key(edge: dict[str, Any]) -> tuple[float, int, str]:
    try:
        score = float(edge.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        count = int(edge.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    return (score, count, str(edge.get("latest_date") or ""))


def relationship_evidence(edge: dict[str, Any]) -> str:
    evidence = edge.get("evidence") if isinstance(edge.get("evidence"), list) else []
    first = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
    return str(first.get("statement") or "暂无证据摘要")


def render_relationship_summary(index: dict[str, Any]) -> str:
    edges = [edge for edge in index.get("edges", []) if isinstance(edge, dict)]
    strong_people = [edge for edge in edges if edge.get("kind") == "person_person"]
    strong_projects = [
        edge for edge in edges
        if edge.get("kind") == "person_project" and edge.get("relation_type") != "co_mention_weak"
    ]
    strong_people.sort(key=relationship_edge_key, reverse=True)
    strong_projects.sort(key=relationship_edge_key, reverse=True)

    def card(edge: dict[str, Any], connector: str) -> str:
        source = html.escape(str(edge.get("source_label") or edge.get("source") or "-"))
        target = html.escape(str(edge.get("target_label") or edge.get("target") or "-"))
        relation = html.escape(str(edge.get("relation_label") or edge.get("label") or "关系"))
        count = int(edge.get("count") or 0)
        latest = html.escape(str(edge.get("latest_date") or "-"))
        evidence = html.escape(relationship_evidence(edge))
        return f"""
        <article class="relationship-summary-card">
            <div class="relationship-summary-title">{source} <span>{connector}</span> {target}</div>
            <div class="relationship-summary-meta">{relation} · {count:,} 条证据 · 最近 {latest}</div>
            <div class="relationship-summary-evidence">{evidence}</div>
        </article>
        """

    people_html = "\n".join(card(edge, "↔") for edge in strong_people[:4]) or '<div class="network-empty">暂无高信号人物证据</div>'
    project_html = "\n".join(card(edge, "→") for edge in strong_projects[:4]) or '<div class="network-empty">暂无高信号项目证据</div>'
    skipped = (index.get("summary") or {}).get("skipped") if isinstance(index.get("summary"), dict) else {}
    skipped_total = sum(int(v or 0) for v in skipped.values()) if isinstance(skipped, dict) else 0
    return f"""
    <section class="relationship-summary">
        <div class="relationship-summary-head">
            <div>
                <h3>关联证据摘要</h3>
                <p>优先展示能解释人物、项目、交付或内容协作的高信号证据；弱共现只作为辅助线索。</p>
            </div>
            <div class="relationship-summary-skip">已过滤噪音 {skipped_total:,} 条</div>
        </div>
        <div class="relationship-summary-grid">
            <div>
                <div class="relationship-summary-label">人物证据</div>
                <div class="relationship-summary-list">{people_html}</div>
            </div>
            <div>
                <div class="relationship-summary-label">项目证据</div>
                <div class="relationship-summary-list">{project_html}</div>
            </div>
        </div>
    </section>
    """


def load_digest():
    data = read_json(DIGEST_FILE)
    return data if isinstance(data, dict) else {}


def load_quality():
    data = read_json(QUALITY_FILE)
    return data if isinstance(data, dict) else {}


def load_feedback():
    data = read_json(FEEDBACK_FILE)
    return data if isinstance(data, dict) else {}


def format_digest_time(value: str) -> str:
    if not value:
        return "-"
    text = str(value)
    if "T" in text:
        return text.split("T", 1)[0] + " " + text.split("T", 1)[1][:8]
    return text


def reason_label(reason: str) -> str:
    return {
        "new_in_digest_snapshot": "新增人物",
        "memory_count_and_latest_date_changed": "记忆数与日期变化",
        "memory_count_changed": "记忆数变化",
        "latest_date_changed": "最新日期变化",
        "recent_person_activity": "近期活跃",
    }.get(str(reason or ""), str(reason or "近期变化"))


def render_digest_people(rows: list[dict[str, Any]], *, include_reason: bool = False) -> str:
    if not rows:
        return '<div class="digest-empty">暂无人物变化</div>'
    output = []
    for row in rows[:8]:
        name = html.escape(str(row.get("name") or "未命名人物"))
        date = html.escape(str(row.get("latest_date") or "-"))
        count = int(row.get("memory_count") or 0)
        highlight = html.escape(str(row.get("highlight") or "暂无摘要"))
        reason = reason_label(str(row.get("reason") or "")) if include_reason else str(row.get("category") or "")
        reason_html = f'<span>{html.escape(reason)}</span>' if reason else ""
        output.append(
            f"""
            <div class="digest-person-row">
                <div>
                    <b>{name}</b>
                    <p>{highlight}</p>
                </div>
                <div class="digest-person-meta">
                    {reason_html}
                    <span>{date}</span>
                    <span>{count:,} 条</span>
                </div>
            </div>
            """
        )
    return "\n".join(output)


def render_digest_attention(items: list[str]) -> str:
    if not items:
        return '<li>当前链路未见明显问题。</li>'
    return "\n".join(f"<li>{html.escape(str(item))}</li>" for item in items[:8])


def quality_label(status: str) -> str:
    return {
        "ok": "可用",
        "attention": "需要 Codex 处理",
        "warn": "输入不完整",
        "missing": "未生成",
    }.get(str(status or ""), str(status or "未知"))


def render_quality_issue(issue: dict[str, Any]) -> str:
    severity = html.escape(str(issue.get("severity") or "low").upper())
    title = html.escape(str(issue.get("title") or issue.get("id") or "质量项"))
    detail = html.escape(str(issue.get("detail") or ""))
    action = html.escape(str(issue.get("suggested_action") or ""))
    return f"""
    <div class="quality-issue">
        <div class="quality-issue-top">
            <span class="quality-pill">{severity}</span>
            <b>{title}</b>
        </div>
        <p>{detail}</p>
        <em>{action}</em>
    </div>
    """


def render_quality_observation(item: dict[str, Any]) -> str:
    title = html.escape(str(item.get("title") or item.get("id") or "观察项"))
    detail = html.escape(str(item.get("detail") or ""))
    action = html.escape(str(item.get("suggested_action") or ""))
    return f"""
    <div class="quality-issue quality-observation">
        <div class="quality-issue-top">
            <span class="quality-pill">观察</span>
            <b>{title}</b>
        </div>
        <p>{detail}</p>
        <em>{action}</em>
    </div>
    """


def render_quality_panel(quality: dict[str, Any]) -> str:
    if not quality:
        status = "missing"
        score = 0
        issue_count = 0
        recommendation = "质量层还没有生成，自动任务下一轮会补齐；也可以由 Codex 手动运行 immortal.py quality。"
        issues_html = '<div class="digest-empty">暂无质量报告</div>'
        observations_html = ""
        identity_metrics = {}
        relationship_metrics = {}
    else:
        status = str(quality.get("status") or "missing")
        score = int(quality.get("score") or 0)
        issue_count = int(quality.get("issue_count") or 0)
        recommendation = str(quality.get("recommendation") or "")
        issues = quality.get("top_issues") if isinstance(quality.get("top_issues"), list) else []
        issues_html = "\n".join(render_quality_issue(issue) for issue in issues[:5] if isinstance(issue, dict))
        if not issues_html:
            issues_html = '<div class="digest-empty">暂无需要 Codex 处理的质量项</div>'
        observations = quality.get("top_observations") if isinstance(quality.get("top_observations"), list) else []
        observations_html = "\n".join(render_quality_observation(item) for item in observations[:4] if isinstance(item, dict))
        identity_metrics = ((quality.get("identity") or {}).get("metrics") or {}) if isinstance(quality.get("identity"), dict) else {}
        relationship_metrics = ((quality.get("relationships") or {}).get("metrics") or {}) if isinstance(quality.get("relationships"), dict) else {}
    status_text = quality_label(status)
    return f"""
    <div class="quality-panel">
        <div class="digest-head">
            <div>
                <h3>记忆质量</h3>
                <p>身份归一、长期画像污染、关联证据噪音和弱共现膨胀都在后台自动巡检；这里直接展示结论和 Codex 处理项。</p>
            </div>
            <div class="digest-badge quality-status-{html.escape(status)}">{html.escape(status_text)}</div>
        </div>
        <div class="digest-grid">
            <div class="digest-card">
                <div class="k">质量分</div>
                <div class="v">{score}</div>
                <div class="t">满分 100</div>
            </div>
            <div class="digest-card">
                <div class="k">待处理项</div>
                <div class="v">{issue_count:,}</div>
                <div class="t">{html.escape(recommendation or "暂无异常")}</div>
            </div>
            <div class="digest-card">
                <div class="k">身份规则</div>
                <div class="v">{int(identity_metrics.get("confirmed_rules") or 0):,}</div>
                <div class="t">用户本人档案 {int(identity_metrics.get("self_count") or 0):,}</div>
            </div>
            <div class="digest-card">
                <div class="k">弱证据噪音</div>
                <div class="v">{int(relationship_metrics.get("weak_edges") or 0):,}</div>
                <div class="t">最高分 {html.escape(str(relationship_metrics.get("weak_edges_top_score") or 0))}</div>
            </div>
            <div class="digest-card">
                <div class="k">证据成熟度</div>
                <div class="v">{int(relationship_metrics.get("strong_person_edges") or 0):,}</div>
                <div class="t">低置信 {int(relationship_metrics.get("low_confidence_person_edges") or 0):,} 条，自动补证据</div>
            </div>
        </div>
        <div class="quality-issues">{issues_html}</div>
        {f'<div class="quality-observation-list">{observations_html}</div>' if observations_html else ''}
    </div>
    """


def render_feedback_panel(feedback: dict[str, Any]) -> str:
    if not feedback:
        return """
        <div class="quality-panel">
            <div class="digest-head">
                <div>
                    <h3>运行反馈</h3>
                    <p>还没有生成反馈报告。自动任务下一轮会生成，也可以运行 immortal.py feedback。</p>
                </div>
                <div class="digest-badge quality-status-missing">MISSING</div>
            </div>
        </div>
        """
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    quality = feedback.get("quality") if isinstance(feedback.get("quality"), dict) else {}
    feishu = feedback.get("feishu") if isinstance(feedback.get("feishu"), dict) else {}
    errors = feedback.get("errors") if isinstance(feedback.get("errors"), dict) else {}
    people = feedback.get("people") if isinstance(feedback.get("people"), dict) else {}
    status = html.escape(str(feedback.get("status") or "missing"))
    status_label = html.escape(str(feedback.get("status_label") or "未知"))
    score = int(quality.get("score") or 0)
    issue_count = int(quality.get("issue_count") or 0)
    top_issues = quality.get("top_issues") if isinstance(quality.get("top_issues"), list) else []
    issue_html = "\n".join(f"<li>{html.escape(str(item))}</li>" for item in top_issues[:5]) or "<li>暂无质量问题。</li>"
    attention = feedback.get("attention") if isinstance(feedback.get("attention"), list) else []
    attention_html = "\n".join(f"<li>{html.escape(str(item))}</li>" for item in attention[:6]) or "<li>暂无提醒。</li>"
    recent_people = people.get("recently_updated") if isinstance(people.get("recently_updated"), list) else []
    people_html = "\n".join(
        f"<li>{html.escape(str(item.get('name') or '未命名'))}｜{html.escape(str(item.get('latest_date') or '-'))}｜{int(item.get('memory_count') or 0):,} 条</li>"
        for item in recent_people[:8]
        if isinstance(item, dict)
    ) or "<li>暂无最近更新人物。</li>"
    return f"""
    <div class="quality-panel">
        <div class="digest-head">
            <div>
                <h3>运行反馈</h3>
                <p>自动任务每轮完成后生成的可读反馈。它回答：有没有抓、有没有蒸馏、质量是否需要 Codex 处理。</p>
            </div>
            <div class="digest-badge quality-status-{status}">{status_label}</div>
        </div>
        <div class="digest-grid">
            <div class="digest-card"><div class="k">本次新增</div><div class="v">{int(summary.get('new_records') or 0):,}</div><div class="t">总记录 {int(summary.get('total_records') or 0):,}</div></div>
            <div class="digest-card"><div class="k">飞书新增</div><div class="v">{int(summary.get('feishu_new_records') or 0):,}</div><div class="t">最近 {html.escape(str(feishu.get('last_collect') or '-'))}</div></div>
            <div class="digest-card"><div class="k">质量分</div><div class="v">{score:,}</div><div class="t">问题 {issue_count} 个</div></div>
            <div class="digest-card"><div class="k">错误状态</div><div class="v" style="font-size:22px">{html.escape(str(errors.get('status') or '-')).upper()}</div><div class="t">通知已接入</div></div>
        </div>
        <div class="digest-columns">
            <div class="panel">
                <div class="panel-header">质量关注</div>
                <div class="panel-body"><ul class="digest-attention">{issue_html}</ul></div>
            </div>
            <div class="panel">
                <div class="panel-header">最近更新人物</div>
                <div class="panel-body"><ul class="digest-attention">{people_html}</ul></div>
            </div>
        </div>
        <div class="panel" style="margin-top:16px">
            <div class="panel-header">提醒</div>
            <div class="panel-body"><ul class="digest-attention">{attention_html}</ul></div>
        </div>
        <div class="cmd-tip">python3 ~/.codex/skills/immortal/immortal.py feedback --notify</div>
    </div>
    """


def render_lifeline_panel(
    *,
    total: int,
    last_collect: str,
    collect_count: int,
    feishu: dict[str, Any],
    people_index: dict[str, Any],
    quality: dict[str, Any],
    digest: dict[str, Any],
    state: dict[str, Any],
    backup: dict[str, Any],
) -> str:
    errors = state.get("errors") or []
    digest_status = ((digest.get("errors") or {}).get("status") if isinstance(digest, dict) else "") or "missing"
    quality_status = str(quality.get("status") or "missing") if isinstance(quality, dict) else "missing"
    quality_score = int(quality.get("score") or 0) if isinstance(quality, dict) else 0
    new_records = int((digest.get("summary") or {}).get("new_records") or state.get("last_run_new_records") or 0)
    feishu_new = int((digest.get("summary") or {}).get("feishu_new_records") or state.get("last_run_feishu_new_records") or 0)
    recent_collect = str((digest.get("summary") or {}).get("recent_collect_time_local") or last_collect)
    backup_info = backup_display(backup)
    status_text = "正常" if not errors and digest_status == "ok" and quality_status in {"ok", "attention"} and backup_info["ok"] else "需要关注"
    return f"""
    <section class="lifeline-hero">
        <div>
            <div class="section-kicker">IMMORTAL SKILL CONTROL</div>
            <h2>防丢失控制台</h2>
            <p>这套系统的主线不是关系热度，而是把 AI 对话、文件、文档、飞书语料和本地产出持续保存、清洗、蒸馏，并在需要时召回成 Codex 可用上下文。</p>
        </div>
        <div class="lifeline-status">
            <span>{html.escape(status_text)}</span>
            <b>{quality_score}</b>
            <em>记忆质量分</em>
        </div>
    </section>
    <div class="lifeline-grid">
        <div class="lifeline-card primary">
            <div class="k">最近采集</div>
            <div class="v">{html.escape(recent_collect)}</div>
            <div class="t">累计采集 {int(collect_count):,} 次</div>
        </div>
        <div class="lifeline-card">
            <div class="k">总记忆记录</div>
            <div class="v">{total:,}</div>
            <div class="t">本次新增 {new_records:,}</div>
        </div>
        <div class="lifeline-card">
            <div class="k">飞书沉淀</div>
            <div class="v">{int(feishu.get('clean_records') or 0):,}</div>
            <div class="t">本次飞书新增 {feishu_new:,}</div>
        </div>
        <div class="lifeline-card">
            <div class="k">人物档案</div>
            <div class="v">{int(people_index.get('count') or 0):,}</div>
            <div class="t">最近证据 {html.escape(str(people_index.get('latest_date') or '-'))}</div>
        </div>
        <div class="lifeline-card">
            <div class="k">自动链路</div>
            <div class="v">{html.escape(str(digest_status).upper())}</div>
            <div class="t">错误：{html.escape(', '.join(errors) if errors else 'none')}</div>
        </div>
        <div class="lifeline-card backup {'primary' if backup_info['ok'] else 'attention'}">
            <div class="k">便携备份</div>
            <div class="v">{html.escape(str(backup_info['title']))}</div>
            <div class="t">{html.escape(str(backup_info['detail']))}</div>
        </div>
    </div>
    <div class="lifeline-actions">
        <a class="factory-action" href="#agent"><b>Agent 接入</b><code>给 Claude Code / Codex / 其他本地 Agent 的统一入口</code></a>
        <a class="factory-action" href="#factory"><b>任务上下文生成器</b><code>主看板内打开按钮式采集、清洗、短期上下文编译工作流</code></a>
        <div><b>体检</b><code>python3 ~/.codex/skills/immortal/immortal.py doctor</code></div>
        <div><b>新鲜度</b><code>python3 ~/.codex/skills/immortal/immortal.py health</code></div>
        <div><b>立即备份</b><code>python3 ~/.codex/skills/immortal/immortal.py export</code></div>
        <div><b>恢复校验</b><code>python3 ~/.codex/skills/immortal/immortal.py restore-check "{html.escape(str(backup_info['path'] or '<export-path>'))}"</code></div>
        <div><b>立即采集</b><code>python3 ~/.codex/skills/immortal/immortal.py run</code></div>
        <div><b>召回上下文</b><code>python3 ~/.codex/skills/immortal/immortal.py context "当前任务"</code></div>
    </div>
    """


def md_to_html(md: str) -> str:
    """简单的 Markdown → HTML 转换。"""
    lines = md.split("\n")
    out = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h1>{html.escape(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h2>{html.escape(stripped[3:])}</h2>")
        elif stripped.startswith("### "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h3>{html.escape(stripped[4:])}</h3>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            text = html.escape(stripped[2:])
            text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
            out.append(f"<li>{text}</li>")
        elif stripped.startswith(">"):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<blockquote>{html.escape(stripped[1:].strip())}</blockquote>")
        elif stripped.startswith("---"):
            if in_list: out.append("</ul>"); in_list = False
            out.append("<hr>")
        elif stripped == "":
            if in_list: out.append("</ul>"); in_list = False
            out.append("")
        else:
            if in_list: out.append("</ul>"); in_list = False
            text = html.escape(stripped)
            text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
            out.append(f"<p>{text}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def generate_html():
    stats = load_index_stats()
    state = load_state()
    summaries = load_summaries()
    soul = load_soul()
    agent_entry = load_agent_entry()
    feishu = load_feishu_metrics()
    people_index = load_people_index()
    relationship_index = load_relationship_index()
    digest = load_digest()
    quality = load_quality()
    feedback = load_feedback()
    backup = get_backup_status(IMMORTAL_DIR)

    if not stats:
        return "<html><body>记忆库为空，请先运行采集。</body></html>"

    total = stats["total"]
    user_count = stats["roles"].get("user", 0)
    assistant_count = stats["roles"].get("assistant", 0)

    # 30 天趋势数据
    trend_data = []
    for date in sorted(stats["dates"].keys())[-30:]:
        trend_data.append([date, stats["dates"][date]])

    # 数据源分组
    source_icons = {
        "claude-code-conversation": "💬",
        "claude-code-memory": "🧠",
        "claude-code-skill": "⚡",
        "claude-code-file-history": "📄",
        "claude-code-paste-cache": "📋",
        "desktop-output": "🖥️",
        "codex-conversation": "🤖",
        "codex-memory": "🧠",
        "codex-skill": "⚡",
        "codex-output": "📦",
        "autoclaw-skill": "🔧",
        "hermes-conversation": "📨",
        "hermes-memory": "🧠",
        "hermes-skill": "⚡",
    }
    source_groups = {
        "Claude Code": [s for s in stats["sources"] if s.startswith("claude-code")],
        "Codex": [s for s in stats["sources"] if s.startswith("codex")],
        "Hermes": [s for s in stats["sources"] if s.startswith("hermes")],
        "其他": [s for s in stats["sources"] if not s.startswith(("claude-code", "codex", "hermes"))],
    }

    # 状态信息
    last_collect = state.get("last_collect", "未知")
    if last_collect and "T" in str(last_collect):
        last_collect = last_collect.split("T")[0] + " " + last_collect.split("T")[1][:8]
    collect_count = state.get("collect_count", 0)

    # 数字人格目录
    soul_toc = []
    for line in soul.split("\n"):
        m = re.match(r'^## (.+)', line.strip())
        if m:
            soul_toc.append(m.group(1).strip())

    # JSON 数据
    summaries_json = json.dumps(summaries, ensure_ascii=False)
    soul_html = md_to_html(soul)
    agent_entry_html = md_to_html(agent_entry)
    feishu_preview_html = md_to_html(feishu["reviewed_preview"] or feishu["proposal_preview"])
    relationship_summary_html = render_relationship_summary(relationship_index)
    quality_panel_html = render_quality_panel(quality)
    feedback_panel_html = render_feedback_panel(feedback)
    lifeline_panel_html = render_lifeline_panel(
        total=total,
        last_collect=last_collect,
        collect_count=int(collect_count or 0),
        feishu=feishu,
        people_index=people_index,
        quality=quality,
        digest=digest,
        state=state,
        backup=backup,
    )
    trend_json = json.dumps(trend_data)
    samples_json = json.dumps(stats["search_samples"], ensure_ascii=False)
    people_json = json.dumps(people_index["people"], ensure_ascii=False)
    people_categories_json = json.dumps(people_index["categories"], ensure_ascii=False)
    relationships_json = json.dumps(
        {
            "generated_at": relationship_index["generated_at"],
            "basis": relationship_index["basis"],
            "nodes": relationship_index["nodes"],
            "edges": relationship_index["edges"],
            "summary": relationship_index["summary"],
            "path": relationship_index["path"],
        },
        ensure_ascii=False,
    )
    digest_json = json.dumps(digest, ensure_ascii=False)

    soul_size_kb = len(soul) / 1024
    soul_lines = len(soul.split("\n"))
    total_sources = len(stats["sources"])
    latest_agent_context_status = (
        f"{fmt_bytes(LATEST_AGENT_CONTEXT_FILE.stat().st_size)} · {format_local_time(datetime.fromtimestamp(LATEST_AGENT_CONTEXT_FILE.stat().st_mtime, timezone.utc).isoformat())}"
        if LATEST_AGENT_CONTEXT_FILE.exists()
        else "missing"
    )
    agent_handoff_prompt = (
        "请先读取 ~/.immortal/agent/ENTRY.md，然后针对当前任务运行："
        "python3 ~/.codex/skills/immortal/immortal.py agent-context \"<当前任务>\" --print，"
        "把返回内容作为理解用户本人的长期上下文。"
    )

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>用户本人 · 赛博永生记忆库</title>
<style>
:root {{
    color-scheme: dark;
    --bg: #05070a;
    --bg-soft: #091015;
    --panel: rgba(13, 22, 27, .86);
    --panel-strong: rgba(18, 32, 39, .94);
    --line: rgba(126, 241, 218, .18);
    --line-strong: rgba(126, 241, 218, .34);
    --text: #e7f6f3;
    --muted: #83a19c;
    --faint: #49635f;
    --cyan: #70f5df;
    --green: #8ef77e;
    --amber: #f2c96b;
    --red: #ff786b;
    --blue: #71a7ff;
    --ink: #020506;
    --radius: 8px;
    --shadow: 0 24px 70px rgba(0, 0, 0, .45);
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
html, body {{
    min-height: 100%;
    background:
        linear-gradient(rgba(112, 245, 223, .035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(112, 245, 223, .03) 1px, transparent 1px),
        radial-gradient(circle at 16% 0%, rgba(112, 245, 223, .10), transparent 32%),
        linear-gradient(140deg, #030507 0%, #071014 42%, #0d0d10 100%);
    background-size: 34px 34px, 34px 34px, auto, auto;
    color: var(--text);
    font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    letter-spacing: 0;
    line-height: 1.58;
}}
body::before {{
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    background: repeating-linear-gradient(180deg, rgba(255,255,255,.025) 0 1px, transparent 1px 5px);
    mix-blend-mode: overlay;
    opacity: .22;
}}
body > * {{ position: relative; z-index: 1; }}
.header {{
    min-height: 270px;
    padding: 34px clamp(18px, 4vw, 58px) 28px;
    display: grid;
    grid-template-columns: minmax(0, 1.1fr) minmax(320px, .9fr);
    gap: 28px;
    align-items: end;
    border-bottom: 1px solid var(--line);
    overflow: hidden;
}}
.header::after {{
    content: "";
    position: absolute;
    right: clamp(20px, 7vw, 120px);
    top: 28px;
    width: 360px;
    height: 210px;
    border: 1px solid rgba(112,245,223,.20);
    background:
        linear-gradient(90deg, transparent 47%, rgba(112,245,223,.18) 50%, transparent 53%),
        linear-gradient(transparent 47%, rgba(112,245,223,.16) 50%, transparent 53%);
    background-size: 48px 48px;
    transform: skewX(-9deg) rotate(-2deg);
    opacity: .62;
}}
.brand-kicker {{
    color: var(--cyan);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .18em;
    text-transform: uppercase;
    margin-bottom: 12px;
}}
.header h1 {{
    max-width: 820px;
    font-size: clamp(40px, 7vw, 88px);
    line-height: .92;
    font-weight: 760;
    letter-spacing: 0;
}}
.header .subtitle {{
    max-width: 650px;
    margin-top: 16px;
    color: #b7cbc7;
    font-size: 15px;
}}
.header .badge {{
    display: inline-flex;
    align-items: center;
    gap: 7px;
    margin-left: 10px;
    transform: translateY(-10px);
    color: var(--ink);
    background: var(--green);
    border-radius: 999px;
    padding: 5px 10px;
    font-size: 12px;
    font-weight: 800;
}}
.header .badge::before {{
    content: "";
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--ink);
    box-shadow: 0 0 0 4px rgba(2,5,6,.12);
}}
.hero-console {{
    align-self: stretch;
    min-height: 190px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    background: rgba(3, 9, 11, .72);
    box-shadow: var(--shadow);
    padding: 18px;
    backdrop-filter: blur(18px);
}}
.console-top {{
    display: flex;
    justify-content: space-between;
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .12em;
    border-bottom: 1px solid var(--line);
    padding-bottom: 10px;
    margin-bottom: 14px;
}}
.console-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
}}
.console-cell {{
    min-height: 68px;
    padding: 12px;
    border: 1px solid rgba(255,255,255,.07);
    background: rgba(255,255,255,.025);
    border-radius: var(--radius);
}}
.console-cell .v {{ font-size: 24px; font-weight: 780; color: var(--cyan); line-height: 1; }}
.console-cell .l {{ margin-top: 7px; font-size: 12px; color: var(--muted); }}
.tabs {{
    position: sticky;
    top: 0;
    z-index: 100;
    display: flex;
    gap: 8px;
    padding: 10px clamp(12px, 4vw, 58px);
    background: rgba(5, 8, 10, .82);
    border-bottom: 1px solid var(--line);
    backdrop-filter: blur(18px);
    overflow-x: auto;
}}
.tab {{
    min-height: 38px;
    padding: 9px 14px;
    cursor: pointer;
    color: var(--muted);
    border: 1px solid transparent;
    border-radius: var(--radius);
    font-size: 13px;
    white-space: nowrap;
    transition: color .18s, border-color .18s, background .18s;
}}
.tab:hover {{ color: var(--text); border-color: rgba(112,245,223,.16); }}
.tab.active {{
    color: var(--cyan);
    border-color: var(--line-strong);
    background: rgba(112,245,223,.07);
}}
.container {{ max-width: 1320px; margin: 0 auto; padding: 30px clamp(16px, 4vw, 42px) 56px; }}
.section {{ display: none; }}
.section.active {{ display: block; }}

/* Stats */
.stats-row {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
.lifeline-hero {{
    min-height: 260px;
    display: grid;
    grid-template-columns: minmax(0, 1.2fr) minmax(240px, .55fr);
    gap: 20px;
    align-items: end;
    margin-bottom: 16px;
    padding: clamp(20px, 4vw, 34px);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    background:
        linear-gradient(120deg, rgba(112,245,223,.12), transparent 44%),
        linear-gradient(180deg, rgba(15,26,31,.92), rgba(5,10,13,.94));
    box-shadow: var(--shadow);
    overflow: hidden;
}}
.lifeline-hero h2 {{
    margin: 0;
    font-size: clamp(34px, 6vw, 72px);
    line-height: .95;
    font-weight: 840;
}}
.lifeline-hero p {{
    max-width: 720px;
    margin-top: 14px;
    color: #bdd2cd;
    font-size: 15px;
}}
.lifeline-status {{
    justify-self: end;
    width: min(260px, 100%);
    padding: 18px;
    border: 1px solid rgba(142,247,126,.34);
    border-radius: var(--radius);
    background: rgba(0,0,0,.24);
    text-align: right;
}}
.lifeline-status span {{
    color: var(--green);
    font-size: 12px;
    font-weight: 800;
}}
.lifeline-status b {{
    display: block;
    margin-top: 8px;
    color: var(--green);
    font-size: clamp(42px, 7vw, 74px);
    line-height: .9;
}}
.lifeline-status em {{
    display: block;
    margin-top: 8px;
    color: var(--muted);
    font-size: 12px;
    font-style: normal;
}}
.lifeline-grid {{
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 14px;
}}
.lifeline-card {{
    min-width: 0;
    min-height: 132px;
    padding: 15px;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: linear-gradient(180deg, rgba(18,31,37,.86), rgba(7,14,17,.86));
}}
.lifeline-card.primary {{
    border-color: rgba(142,247,126,.34);
}}
.lifeline-card.attention {{
    border-color: rgba(255,120,107,.46);
}}
.lifeline-card .k {{
    color: var(--muted);
    font-size: 11px;
    font-weight: 800;
    letter-spacing: .10em;
    text-transform: uppercase;
}}
.lifeline-card .v {{
    margin-top: 12px;
    color: var(--cyan);
    font-size: clamp(18px, 2.6vw, 30px);
    font-weight: 820;
    line-height: 1.05;
    overflow-wrap: anywhere;
}}
.lifeline-card .t {{
    margin-top: 10px;
    color: var(--muted);
    font-size: 12px;
    overflow-wrap: anywhere;
}}
.lifeline-actions {{
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 22px;
}}
.lifeline-actions div {{
    min-width: 0;
    padding: 12px;
    border: 1px solid rgba(255,255,255,.075);
    border-radius: var(--radius);
    background: rgba(255,255,255,.026);
}}
.lifeline-actions b {{
    display: block;
    margin-bottom: 7px;
    color: var(--amber);
    font-size: 12px;
}}
.lifeline-actions code {{
    display: block;
    color: #c7dfda;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 11px;
    line-height: 1.45;
    white-space: normal;
    overflow-wrap: anywhere;
}}
.stat-card {{
    min-height: 118px;
    padding: 16px;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: linear-gradient(180deg, rgba(21, 34, 40, .82), rgba(8, 16, 19, .82));
    box-shadow: 0 18px 40px rgba(0,0,0,.24);
}}
.stat-card .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .12em; }}
.stat-card .value {{ font-size: clamp(22px, 3vw, 34px); font-weight: 780; margin-top: 12px; line-height: 1; }}
.stat-card .value.blue {{ color: var(--blue); }}
.stat-card .value.green {{ color: var(--green); }}
.stat-card .value.purple {{ color: var(--cyan); }}
.stat-card .value.orange {{ color: var(--amber); }}

/* Soul card */
.soul-banner {{
    position: relative;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    padding: 22px;
    margin-bottom: 18px;
    background:
        linear-gradient(120deg, rgba(112,245,223,.13), transparent 46%),
        rgba(9, 16, 20, .86);
    overflow: hidden;
}}
.soul-banner::after {{
    content: "MEMORY CORE";
    position: absolute;
    right: 18px;
    bottom: 10px;
    color: rgba(112,245,223,.11);
    font-size: clamp(34px, 7vw, 82px);
    font-weight: 900;
    line-height: .85;
}}
.soul-banner h2 {{ font-size: 18px; margin-bottom: 6px; }}
.soul-banner p {{ color: var(--muted); font-size: 13px; margin-bottom: 14px; }}
.soul-banner .stats {{ display: flex; gap: 10px; flex-wrap: wrap; position: relative; z-index: 1; }}
.soul-banner .s {{ min-width: 118px; padding: 10px 12px; border: 1px solid rgba(255,255,255,.08); background: rgba(0,0,0,.18); border-radius: var(--radius); }}
.soul-banner .s .v {{ font-size: 19px; font-weight: 780; color: var(--cyan); }}
.soul-banner .s .l {{ font-size: 11px; color: var(--muted); }}

/* Grid */
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }}
@media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
.panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; box-shadow: 0 18px 46px rgba(0,0,0,.24); }}
.panel-header {{ padding: 13px 16px; border-bottom: 1px solid var(--line); font-weight: 750; font-size: 12px; color: #c9e7e2; letter-spacing: .12em; text-transform: uppercase; }}
.panel-body {{ padding: 16px; }}

/* Trend chart */
.trend {{ display: flex; align-items: flex-end; gap: 5px; height: 160px; padding: 12px 0 2px; }}
.trend-bar {{
    flex: 1;
    background: linear-gradient(to top, rgba(112,245,223,.95), rgba(142,247,126,.9));
    border-radius: 4px 4px 0 0;
    min-height: 3px;
    cursor: pointer;
    transition: transform .18s, filter .18s;
    position: relative;
    box-shadow: 0 0 22px rgba(112,245,223,.12);
}}
.trend-bar:hover {{ transform: translateY(-4px); filter: brightness(1.18); }}
.trend-bar:hover::after {{ content: attr(data-tooltip); position: absolute; bottom: 108%; left: 50%; transform: translateX(-50%); background: #03100f; border: 1px solid var(--line-strong); color: var(--text); padding: 5px 8px; border-radius: 6px; font-size: 11px; white-space: nowrap; }}

/* Source */
.source-group {{ margin-bottom: 14px; }}
.source-group:last-child {{ margin-bottom: 0; }}
.source-group-header {{ display: flex; justify-content: space-between; font-size: 12px; font-weight: 750; color: var(--cyan); margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }}
.source-item {{ display: flex; align-items: center; gap: 10px; padding: 7px 0; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,.04); }}
.source-icon {{ width: 26px; color: var(--amber); text-align: center; }}
.source-name {{ flex: 1; }}
.source-count {{ color: var(--muted); font-size: 12px; }}

/* Type bars */
.type-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
.type-name {{ width: 118px; font-size: 12px; color: var(--muted); }}
.type-bar-bg {{ flex: 1; height: 9px; background: rgba(255,255,255,.06); border-radius: 999px; overflow: hidden; }}
.type-bar-fill {{ height: 100%; background: linear-gradient(90deg, var(--cyan), var(--green)); }}
.type-count {{ width: 116px; font-size: 11px; color: var(--muted); text-align: right; }}

/* Summary cards */
.summary-list {{ display: flex; flex-direction: column; gap: 12px; }}
.summary-card {{ border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,10,12,.72); overflow: hidden; }}
.summary-head {{ padding: 12px 16px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; user-select: none; }}
.summary-head:hover {{ background: rgba(112,245,223,.06); }}
.summary-head .date {{ font-weight: 750; font-size: 14px; color: var(--cyan); }}
.summary-head .arrow {{ color: var(--muted); transition: transform 0.2s; }}
.summary-card.open .summary-head .arrow {{ transform: rotate(90deg); }}
.summary-body {{ display: none; padding: 0 16px 16px; font-size: 13px; color: #b8ccc8; white-space: pre-wrap; line-height: 1.7; }}
.summary-card.open .summary-body {{ display: block; }}

/* Search */
.search-box {{ position: relative; margin-bottom: 16px; }}
.search-input {{ width: 100%; padding: 15px 16px; background: rgba(5,10,12,.82); border: 1px solid var(--line); border-radius: var(--radius); color: var(--text); font-size: 14px; outline: none; }}
.search-input:focus {{ border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(112,245,223,.08); }}
.search-hint {{ font-size: 12px; color: var(--muted); margin-top: 8px; }}
.search-results {{ display: flex; flex-direction: column; gap: 8px; }}
.search-result {{ padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }}
.search-result .meta {{ font-size: 11px; color: var(--muted); margin-bottom: 4px; }}
.search-result .text {{ font-size: 13px; }}
.search-result .text mark {{ background: var(--amber); color: #06100f; padding: 1px 3px; border-radius: 2px; }}
.cmd-tip {{ background: #020607; border: 1px solid var(--line); padding: 12px 14px; border-radius: var(--radius); font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; color: #b9d7d1; margin-top: 12px; overflow-x: auto; }}

/* People */
.people-hero {{
    min-height: 220px;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(300px, 420px);
    gap: 18px;
    align-items: end;
    margin-bottom: 16px;
    padding: 24px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    background:
        linear-gradient(120deg, rgba(112,245,223,.16), transparent 46%),
        linear-gradient(180deg, rgba(14,26,31,.88), rgba(5,10,12,.88));
    position: relative;
    overflow: hidden;
}}
.people-hero::after {{
    content: "PEOPLE";
    position: absolute;
    right: 16px;
    bottom: -6px;
    color: rgba(112,245,223,.08);
    font-size: clamp(58px, 13vw, 150px);
    font-weight: 920;
    line-height: .8;
}}
.people-hero.agent-hero::after {{ content: "AGENT"; }}
.people-hero.factory-hero::after {{ content: "FACTORY"; }}
.people-hero.timeline-hero::after {{ content: "TIMELINE"; }}
.people-hero.network-hero::after {{ content: "GRAPH"; }}
.section-kicker {{
    color: var(--cyan);
    font-size: 11px;
    font-weight: 800;
    letter-spacing: .16em;
    text-transform: uppercase;
    margin-bottom: 10px;
}}
.people-hero h2 {{
    position: relative;
    z-index: 1;
    margin: 0 0 10px;
    font-size: clamp(34px, 6vw, 72px);
    line-height: .9;
    font-weight: 840;
}}
.people-hero p {{
    position: relative;
    z-index: 1;
    max-width: 720px;
    color: #b7cbc7;
    font-size: 14px;
}}
.people-stats {{
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-columns: 1fr;
    gap: 10px;
}}
.people-stats div {{
    min-height: 58px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 11px 13px;
    border: 1px solid rgba(255,255,255,.08);
    border-radius: var(--radius);
    background: rgba(0,0,0,.18);
}}
.people-stats b {{
    color: var(--cyan);
    font-size: 24px;
    line-height: 1;
}}
.people-stats span {{
    color: var(--muted);
    font-size: 12px;
    text-align: right;
}}
.people-toolbar {{
    display: grid;
    grid-template-columns: minmax(240px, 1fr) 170px 150px;
    gap: 10px;
    margin-bottom: 10px;
}}
.people-search, .people-select {{
    min-height: 42px;
    width: 100%;
    padding: 9px 12px;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    color: var(--text);
    background: rgba(5,10,12,.82);
    outline: none;
}}
.people-search:focus, .people-select:focus {{
    border-color: var(--cyan);
    box-shadow: 0 0 0 3px rgba(112,245,223,.08);
}}
.people-meta {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
    color: var(--muted);
    font-size: 12px;
}}
.people-grid {{
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
}}
.person-card {{
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: linear-gradient(180deg, rgba(15, 25, 29, .90), rgba(6, 11, 13, .94));
    box-shadow: 0 18px 42px rgba(0,0,0,.24);
    overflow: hidden;
}}
.person-card-clickable {{
    cursor: pointer;
    transition: transform .18s, border-color .18s, box-shadow .18s;
}}
.person-card-clickable:hover,
.person-card-clickable:focus-visible {{
    transform: translateY(-2px);
    border-color: var(--line-strong);
    box-shadow: 0 24px 52px rgba(0,0,0,.32), 0 0 0 1px rgba(112,245,223,.12);
    outline: none;
}}
.person-card.self {{
    border-color: rgba(142,247,126,.48);
    box-shadow: 0 0 0 1px rgba(142,247,126,.14), 0 18px 42px rgba(0,0,0,.24);
}}
.person-head {{
    display: grid;
    grid-template-columns: 54px minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    padding: 16px;
    border-bottom: 1px solid var(--line);
}}
.avatar {{
    width: 54px;
    height: 54px;
    border: 1px solid var(--line-strong);
    border-radius: 8px;
    display: grid;
    place-items: center;
    color: var(--ink);
    background: linear-gradient(135deg, var(--cyan), var(--green));
    font-size: 20px;
    font-weight: 900;
}}
.person-name {{
    font-size: 20px;
    line-height: 1.15;
    font-weight: 820;
    overflow-wrap: anywhere;
}}
.person-sub {{
    margin-top: 4px;
    color: var(--muted);
    font-size: 12px;
    overflow-wrap: anywhere;
}}
.person-count {{
    min-width: 78px;
    text-align: right;
}}
.person-count b {{
    display: block;
    color: var(--cyan);
    font-size: 24px;
    line-height: 1;
}}
.person-count span {{
    color: var(--muted);
    font-size: 11px;
}}
.person-body {{
    padding: 16px;
}}
.person-intro {{
    color: #d9ebe7;
    font-size: 14px;
    margin-bottom: 13px;
    overflow-wrap: anywhere;
}}
.person-profile-grid {{
    display: grid;
    gap: 9px;
    margin-bottom: 13px;
}}
.person-profile-line {{
    min-width: 0;
    padding: 10px 11px;
    border: 1px solid rgba(255,255,255,.075);
    border-left: 2px solid rgba(112,245,223,.45);
    border-radius: var(--radius);
    background: rgba(255,255,255,.028);
}}
.person-profile-line b {{
    display: block;
    margin-bottom: 4px;
    color: var(--cyan);
    font-size: 11px;
    letter-spacing: .08em;
    text-transform: uppercase;
}}
.person-profile-line span {{
    display: block;
    color: #d8ebe7;
    font-size: 13px;
    line-height: 1.55;
    overflow-wrap: anywhere;
}}
.pill-row {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 12px;
}}
.pill {{
    max-width: 100%;
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 999px;
    color: #b8d2cc;
    background: rgba(255,255,255,.035);
    padding: 4px 8px;
    font-size: 11px;
    overflow-wrap: anywhere;
}}
.pill.hot {{
    color: var(--ink);
    background: var(--cyan);
    border-color: var(--cyan);
    font-weight: 800;
}}
.person-section-title {{
    margin: 12px 0 7px;
    color: var(--amber);
    font-size: 12px;
    font-weight: 800;
    letter-spacing: .08em;
    text-transform: uppercase;
}}
.highlight-list {{
    display: flex;
    flex-direction: column;
    gap: 8px;
}}
.highlight {{
    padding: 9px 10px;
    border-left: 2px solid var(--line-strong);
    background: rgba(0,0,0,.16);
    color: #bfd4cf;
    font-size: 12px;
    overflow-wrap: anywhere;
}}
.highlight-meta {{
    margin-top: 5px;
    color: var(--faint);
    font-size: 11px;
}}
.co-list {{
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
}}
.empty-people {{
    border: 1px dashed var(--line-strong);
    border-radius: var(--radius);
    padding: 26px;
    color: var(--muted);
    text-align: center;
    grid-column: 1 / -1;
}}
.person-detail-backdrop {{
    position: fixed;
    inset: 0;
    z-index: 180;
    background: rgba(0, 0, 0, .50);
    opacity: 0;
    pointer-events: none;
    transition: opacity .18s ease;
}}
.person-detail-backdrop.open {{
    opacity: 1;
    pointer-events: auto;
}}
.person-detail {{
    position: fixed;
    top: 0;
    right: 0;
    z-index: 190;
    width: min(720px, 100vw);
    height: 100vh;
    border-left: 1px solid var(--line-strong);
    background:
        linear-gradient(135deg, rgba(112,245,223,.11), transparent 34%),
        linear-gradient(180deg, rgba(10, 18, 22, .98), rgba(3, 7, 9, .98));
    box-shadow: -28px 0 70px rgba(0,0,0,.48);
    transform: translateX(104%);
    transition: transform .22s ease;
    overflow-y: auto;
    overflow-x: hidden;
    overscroll-behavior: contain;
}}
.person-detail.open {{ transform: translateX(0); }}
.person-detail-content {{
    min-height: 100%;
    padding: 22px clamp(16px, 4vw, 28px) 34px;
}}
.person-detail-top {{
    display: grid;
    grid-template-columns: 62px minmax(0, 1fr) auto;
    gap: 14px;
    align-items: center;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--line);
}}
.person-detail-title {{
    min-width: 0;
}}
.person-detail-title h3 {{
    margin: 0;
    font-size: clamp(26px, 5vw, 42px);
    line-height: 1;
    font-weight: 850;
    overflow-wrap: anywhere;
}}
.person-detail-title p {{
    margin-top: 7px;
    color: var(--muted);
    font-size: 13px;
    overflow-wrap: anywhere;
}}
.person-detail-close {{
    width: 38px;
    height: 38px;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    color: var(--text);
    background: rgba(0,0,0,.20);
    cursor: pointer;
    font-size: 22px;
    line-height: 1;
}}
.person-detail-close:hover,
.person-detail-close:focus-visible {{
    border-color: var(--line-strong);
    color: var(--cyan);
    outline: none;
}}
.person-detail-intro {{
    margin: 18px 0 16px;
    color: #d8ebe7;
    font-size: 15px;
    overflow-wrap: anywhere;
}}
.person-detail-stats {{
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 18px;
}}
.person-detail-stat {{
    min-width: 0;
    padding: 12px;
    border: 1px solid rgba(255,255,255,.08);
    border-radius: var(--radius);
    background: rgba(255,255,255,.028);
}}
.person-detail-stat b {{
    display: block;
    color: var(--cyan);
    font-size: 22px;
    line-height: 1;
    overflow-wrap: anywhere;
}}
.person-detail-stat span {{
    display: block;
    margin-top: 7px;
    color: var(--muted);
    font-size: 11px;
}}
.person-detail-section {{
    padding: 16px 0;
    border-top: 1px solid rgba(126,241,218,.13);
}}
.person-detail-section h4 {{
    margin: 0 0 10px;
    color: var(--amber);
    font-size: 12px;
    font-weight: 850;
    letter-spacing: .10em;
    text-transform: uppercase;
}}
.detail-metric-list {{
    display: grid;
    gap: 8px;
}}
.detail-metric {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    min-width: 0;
    padding: 8px 0;
    border-bottom: 1px solid rgba(255,255,255,.055);
}}
.detail-metric:last-child {{ border-bottom: 0; }}
.detail-metric span:first-child {{
    color: #c7dfda;
    overflow-wrap: anywhere;
}}
.detail-metric span:last-child {{
    color: var(--cyan);
    font-size: 12px;
    font-weight: 800;
    white-space: nowrap;
}}
.detail-section-text {{
    color: #c8ddd8;
    font-size: 13px;
    overflow-wrap: anywhere;
}}
.detail-section-items {{
    display: grid;
    gap: 9px;
}}
.detail-section-item {{
    padding-left: 10px;
    border-left: 2px solid rgba(112,245,223,.35);
    color: #c7dfda;
    font-size: 13px;
    overflow-wrap: anywhere;
}}
.detail-section-item strong {{
    display: block;
    margin-bottom: 3px;
    color: var(--text);
}}
.person-highlight-list {{
    display: grid;
    gap: 10px;
}}
.person-highlight-detail {{
    padding: 11px 0 11px 12px;
    border-left: 2px solid var(--line-strong);
    color: #bfd4cf;
    font-size: 13px;
    overflow-wrap: anywhere;
}}
.person-highlight-detail .highlight-meta {{
    overflow-wrap: anywhere;
}}

/* Relationship network */
.network-toolbar {{
    display: grid;
    grid-template-columns: minmax(260px, 1fr) 180px 160px;
    gap: 10px;
    margin-bottom: 10px;
}}
.network-search, .network-select {{
    min-height: 42px;
    width: 100%;
    padding: 9px 12px;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    color: var(--text);
    background: rgba(5,10,12,.82);
    outline: none;
}}
.network-search:focus, .network-select:focus {{
    border-color: var(--cyan);
    box-shadow: 0 0 0 3px rgba(112,245,223,.08);
}}
.network-meta {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
    color: var(--muted);
    font-size: 12px;
}}
.network-grid {{
    display: grid;
    grid-template-columns: minmax(300px, .82fr) minmax(0, 1.18fr);
    gap: 14px;
    align-items: start;
}}
.network-panel {{
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: linear-gradient(180deg, rgba(15, 25, 29, .90), rgba(6, 11, 13, .94));
    box-shadow: 0 18px 42px rgba(0,0,0,.24);
    overflow: hidden;
}}
.network-panel-head {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 13px 14px;
    border-bottom: 1px solid var(--line);
    color: #c9e7e2;
    font-size: 12px;
    font-weight: 800;
    letter-spacing: .10em;
    text-transform: uppercase;
}}
.network-panel-body {{
    padding: 14px;
}}
.network-node-cloud {{
    display: grid;
    gap: 8px;
}}
.network-node {{
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    padding: 10px 11px;
    border: 1px solid rgba(255,255,255,.07);
    border-radius: var(--radius);
    background: rgba(255,255,255,.026);
}}
.network-node b {{
    display: block;
    color: var(--text);
    font-size: 13px;
    overflow-wrap: anywhere;
}}
.network-node span {{
    color: var(--muted);
    font-size: 11px;
}}
.network-node em {{
    color: var(--cyan);
    font-style: normal;
    font-size: 12px;
    font-weight: 800;
    white-space: nowrap;
}}
.network-edge-list {{
    display: grid;
    gap: 10px;
}}
.network-edge {{
    min-width: 0;
    padding: 12px;
    border: 1px solid rgba(126,241,218,.16);
    border-radius: var(--radius);
    background: rgba(0,0,0,.16);
}}
.network-edge.person_person {{
    border-color: rgba(112,245,223,.26);
}}
.network-edge.person_project {{
    border-color: rgba(142,247,126,.22);
}}
.network-edge-head {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: start;
    margin-bottom: 8px;
}}
.network-edge-title {{
    color: var(--text);
    font-size: 15px;
    font-weight: 820;
    line-height: 1.3;
    overflow-wrap: anywhere;
}}
.network-edge-score {{
    color: var(--ink);
    background: var(--cyan);
    border-radius: 999px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 850;
    white-space: nowrap;
}}
.confidence-badge {{
    display: inline-flex;
    max-width: 100%;
    align-items: center;
    margin-top: 7px;
    padding: 4px 8px;
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 999px;
    color: #b8d2cc;
    background: rgba(255,255,255,.035);
    font-size: 11px;
    font-weight: 800;
    overflow-wrap: anywhere;
}}
.confidence-high {{
    color: var(--ink);
    border-color: rgba(142,247,126,.7);
    background: var(--green);
}}
.confidence-medium {{
    color: var(--cyan);
    border-color: rgba(112,245,223,.28);
    background: rgba(112,245,223,.08);
}}
.confidence-low {{
    color: var(--amber);
    border-color: rgba(242,201,107,.32);
    background: rgba(242,201,107,.07);
}}
.network-edge-meta {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 8px;
}}
.network-edge-evidence {{
    padding-left: 10px;
    border-left: 2px solid rgba(112,245,223,.36);
    color: #bfd4cf;
    font-size: 12px;
    overflow-wrap: anywhere;
}}
.network-edge-evidence .highlight-meta {{
    margin-top: 5px;
}}
.person-detail .network-edge-list {{
    gap: 9px;
}}
.person-detail .network-edge {{
    padding: 10px;
}}
.person-detail .network-edge-title {{
    font-size: 13px;
}}
.person-detail .network-edge-score {{
    font-size: 10px;
}}
.network-empty {{
    border: 1px dashed var(--line-strong);
    border-radius: var(--radius);
    padding: 24px;
    color: var(--muted);
    text-align: center;
}}
.relationship-summary {{
    margin-bottom: 16px;
    padding: 16px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    background:
        linear-gradient(120deg, rgba(112,245,223,.10), transparent 46%),
        rgba(5, 10, 12, .78);
}}
.relationship-summary-head {{
    display: flex;
    justify-content: space-between;
    gap: 14px;
    align-items: flex-start;
    margin-bottom: 14px;
}}
.relationship-summary-head h3 {{
    margin: 0;
    color: var(--text);
    font-size: 18px;
}}
.relationship-summary-head p {{
    margin-top: 5px;
    color: var(--muted);
    font-size: 13px;
}}
.relationship-summary-skip {{
    min-width: 128px;
    padding: 8px 10px;
    border: 1px solid rgba(242,201,107,.28);
    border-radius: var(--radius);
    color: var(--amber);
    background: rgba(242,201,107,.06);
    font-size: 12px;
    font-weight: 800;
    text-align: right;
}}
.relationship-summary-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
}}
.relationship-summary-label {{
    margin-bottom: 8px;
    color: var(--cyan);
    font-size: 12px;
    font-weight: 850;
    letter-spacing: .10em;
    text-transform: uppercase;
}}
.relationship-summary-list {{
    display: grid;
    gap: 9px;
}}
.relationship-summary-card {{
    min-width: 0;
    padding: 12px;
    border: 1px solid rgba(255,255,255,.08);
    border-radius: var(--radius);
    background: rgba(255,255,255,.028);
}}
.relationship-summary-title {{
    color: var(--text);
    font-size: 14px;
    font-weight: 820;
    line-height: 1.35;
    overflow-wrap: anywhere;
}}
.relationship-summary-title span {{
    color: var(--cyan);
}}
.relationship-summary-meta {{
    margin-top: 5px;
    color: var(--muted);
    font-size: 12px;
}}
.relationship-summary-evidence {{
    margin-top: 8px;
    padding-left: 9px;
    border-left: 2px solid rgba(112,245,223,.34);
    color: #bfd4cf;
    font-size: 12px;
    overflow-wrap: anywhere;
}}

/* Soul */
.soul-toc {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 20px; }}
.soul-toc a {{ background: rgba(112,245,223,.07); border: 1px solid var(--line); color: var(--cyan); padding: 6px 10px; border-radius: var(--radius); font-size: 12px; text-decoration: none; }}
.soul-toc a:hover {{ background: rgba(112,245,223,.16); }}
.soul-content, .feishu-preview {{ border: 1px solid var(--line); border-radius: var(--radius); background: rgba(5,10,12,.72); padding: 22px; }}
.soul-content h1 {{ font-size: 22px; margin: 16px 0 12px; }}
.soul-content h2, .feishu-preview h2 {{ font-size: 18px; margin: 22px 0 10px; padding-bottom: 8px; border-bottom: 1px solid var(--line); color: var(--cyan); }}
.soul-content h3, .feishu-preview h3 {{ font-size: 15px; margin: 14px 0 8px; color: var(--amber); }}
.soul-content p {{ margin-bottom: 8px; font-size: 14px; }}
.soul-content ul {{ margin: 8px 0 8px 20px; }}
.soul-content li, .feishu-preview li {{ margin-bottom: 5px; font-size: 13px; }}
.soul-content blockquote, .feishu-preview blockquote {{ border-left: 3px solid var(--cyan); padding-left: 12px; color: #b8ccc8; margin: 8px 0; font-size: 13px; }}
.soul-content hr, .feishu-preview hr {{ border: none; border-top: 1px solid var(--line); margin: 20px 0; }}
.feishu-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
.feishu-card {{ min-height: 112px; padding: 15px; border: 1px solid var(--line); border-radius: var(--radius); background: linear-gradient(180deg, rgba(17,31,35,.88), rgba(5,11,12,.88)); }}
.feishu-card .k {{ color: var(--muted); font-size: 11px; letter-spacing: .12em; text-transform: uppercase; }}
.feishu-card .n {{ margin-top: 12px; color: var(--cyan); font-size: 27px; font-weight: 790; line-height: 1; }}
.feishu-card.warn .n {{ color: var(--amber); }}
.feishu-card.debug {{ display: none; }}
.feishu-path {{ margin: 12px 0 20px; padding: 12px 14px; border: 1px solid var(--line); border-radius: var(--radius); color: #b8ccc8; background: rgba(0,0,0,.2); font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; overflow-x: auto; }}
.feishu-debug {{
    margin-top: 14px;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: rgba(0,0,0,.16);
    overflow: hidden;
}}
.feishu-debug summary {{
    cursor: pointer;
    padding: 12px 14px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 800;
    letter-spacing: .10em;
    text-transform: uppercase;
}}
.feishu-debug[open] summary {{
    border-bottom: 1px solid var(--line);
    color: var(--cyan);
}}
.feishu-debug-body {{
    display: grid;
    gap: 10px;
    padding: 12px;
}}
.digest-panel {{
    padding: 16px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    background:
        linear-gradient(120deg, rgba(112,245,223,.10), transparent 46%),
        rgba(5, 10, 12, .78);
    margin-bottom: 18px;
}}
.digest-head {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
    margin-bottom: 14px;
}}
.digest-head h3 {{
    margin: 0;
    font-size: 18px;
    color: var(--text);
}}
.digest-head p {{
    margin-top: 5px;
    color: var(--muted);
    font-size: 13px;
}}
.digest-badge {{
    min-width: 120px;
    padding: 8px 10px;
    border: 1px solid rgba(112,245,223,.22);
    border-radius: var(--radius);
    background: rgba(112,245,223,.08);
    color: var(--cyan);
    font-size: 12px;
    font-weight: 800;
    text-align: right;
}}
.digest-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 10px;
    margin-bottom: 14px;
}}
.digest-card {{
    min-height: 88px;
    padding: 12px;
    border: 1px solid rgba(255,255,255,.08);
    border-radius: var(--radius);
    background: rgba(255,255,255,.028);
}}
.digest-card .k {{
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .12em;
}}
.digest-card .v {{
    margin-top: 10px;
    color: var(--cyan);
    font-size: 24px;
    font-weight: 820;
    line-height: 1;
}}
.digest-card .t {{
    margin-top: 6px;
    color: #b8ccc8;
    font-size: 12px;
    overflow-wrap: anywhere;
}}
.digest-columns {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 12px;
}}
.digest-list {{
    display: grid;
    gap: 10px;
}}
.digest-person-row {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    padding: 11px 12px;
    border: 1px solid rgba(255,255,255,.06);
    border-radius: var(--radius);
    background: rgba(255,255,255,.028);
}}
.digest-person-row b {{
    color: var(--text);
    font-size: 13px;
}}
.digest-person-row p {{
    margin-top: 4px;
    color: #bfd4cf;
    font-size: 12px;
    overflow-wrap: anywhere;
}}
.digest-person-meta {{
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 4px;
    color: var(--muted);
    font-size: 11px;
    text-align: right;
    white-space: nowrap;
}}
.digest-attention {{
    display: grid;
    gap: 8px;
    color: #bfd4cf;
    font-size: 13px;
    line-height: 1.65;
    padding-left: 18px;
}}
.digest-attention li {{
    overflow-wrap: anywhere;
}}
.digest-empty {{
    color: var(--muted);
    font-size: 13px;
}}
.quality-panel {{
    padding: 16px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    background:
        linear-gradient(120deg, rgba(142,247,126,.10), transparent 44%),
        rgba(5, 10, 12, .78);
    margin-bottom: 18px;
}}
.quality-status-attention {{
    border-color: rgba(242,201,107,.30);
    background: rgba(242,201,107,.08);
    color: var(--amber);
}}
.quality-status-warn,
.quality-status-missing {{
    border-color: rgba(255,120,107,.30);
    background: rgba(255,120,107,.08);
    color: var(--red);
}}
.quality-issues {{
    display: grid;
    gap: 9px;
}}
.quality-observation-list {{
    display: grid;
    gap: 9px;
    margin-top: 10px;
}}
.quality-issue {{
    padding: 11px 12px;
    border: 1px solid rgba(255,255,255,.07);
    border-radius: var(--radius);
    background: rgba(255,255,255,.028);
}}
.quality-issue-top {{
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 6px;
}}
.quality-issue-top b {{
    color: var(--text);
    font-size: 13px;
    overflow-wrap: anywhere;
}}
.quality-pill {{
    min-width: 46px;
    padding: 2px 6px;
    border: 1px solid rgba(242,201,107,.28);
    border-radius: 999px;
    color: var(--amber);
    background: rgba(242,201,107,.06);
    font-size: 10px;
    font-weight: 850;
    text-align: center;
}}
.quality-observation {{
    border-color: rgba(255,255,255,.06);
    background: rgba(255,255,255,.02);
}}
.quality-observation .quality-pill {{
    border-color: rgba(112,245,223,.22);
    color: var(--cyan);
    background: rgba(112,245,223,.06);
}}
.quality-issue p {{
    color: #bfd4cf;
    font-size: 12px;
    overflow-wrap: anywhere;
}}
.quality-issue em {{
    display: block;
    margin-top: 5px;
    color: var(--muted);
    font-size: 12px;
    font-style: normal;
    overflow-wrap: anywhere;
}}
.auto-cta {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; align-items: center; margin-bottom: 18px; padding: 16px; border: 1px solid var(--line-strong); border-radius: var(--radius); background: linear-gradient(120deg, rgba(112,245,223,.11), rgba(142,247,126,.045)); }}
.auto-cta h3 {{ margin: 0 0 4px; font-size: 17px; color: var(--text); }}
.auto-cta p {{ margin: 0; color: var(--muted); font-size: 13px; }}
.review-actions {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
.review-command {{ display: inline-flex; align-items: center; min-height: 38px; padding: 8px 12px; border: 1px solid var(--line); border-radius: var(--radius); color: #b9d7d1; background: rgba(0,0,0,.24); font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }}
.factory-action {{ display: block; text-decoration: none; color: inherit; border-color: rgba(142,247,126,.45) !important; background: linear-gradient(120deg, rgba(142,247,126,.14), rgba(112,245,223,.08)) !important; }}
.factory-action b {{ color: var(--lime) !important; }}
.agent-grid {{
    display: grid;
    grid-template-columns: minmax(0, .95fr) minmax(0, 1.05fr);
    gap: 16px;
    align-items: start;
}}
.agent-card {{
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: linear-gradient(180deg, rgba(18,31,37,.82), rgba(5,10,12,.86));
    padding: 16px;
    min-width: 0;
}}
.agent-card h3 {{
    margin: 0 0 8px;
    color: var(--text);
    font-size: 17px;
}}
.agent-card p {{
    color: var(--muted);
    font-size: 13px;
}}
.handoff-box {{
    margin-top: 12px;
    border: 1px solid rgba(142,247,126,.34);
    border-radius: var(--radius);
    background: rgba(142,247,126,.07);
    padding: 14px;
}}
.handoff-box b {{
    display: block;
    color: var(--green);
    font-size: 12px;
    letter-spacing: .12em;
    text-transform: uppercase;
    margin-bottom: 8px;
}}
.handoff-copy {{
    width: 100%;
    min-height: 118px;
    resize: vertical;
    border: 1px solid rgba(255,255,255,.08);
    border-radius: var(--radius);
    background: rgba(0,0,0,.28);
    color: #d9f4ee;
    padding: 12px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 12px;
    line-height: 1.65;
}}
.agent-command-list {{
    display: grid;
    gap: 8px;
    margin-top: 12px;
}}
.agent-command-list code {{
    display: block;
    border: 1px solid rgba(255,255,255,.07);
    border-radius: var(--radius);
    background: rgba(0,0,0,.24);
    color: #bdd6d0;
    padding: 9px 10px;
    font-size: 12px;
    overflow-wrap: anywhere;
}}
.entry-preview {{
    max-height: 620px;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: rgba(2, 7, 8, .62);
    padding: 16px;
}}
.entry-preview h1, .entry-preview h2, .entry-preview h3 {{
    color: var(--cyan);
    margin: 16px 0 8px;
}}
.entry-preview h1:first-child {{
    margin-top: 0;
}}
.entry-preview p, .entry-preview li {{
    color: #c0d7d2;
    font-size: 13px;
}}
.entry-preview ul {{
    padding-left: 18px;
}}
.embedded-shell {{
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: rgba(3, 9, 11, .62);
    overflow: hidden;
    box-shadow: var(--shadow);
}}
.factory-frame {{
    display: block;
    width: 100%;
    min-height: 86vh;
    border: 0;
    background: #050809;
}}
.timeline-shell {{
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: rgba(3, 9, 11, .62);
    overflow: hidden;
    box-shadow: var(--shadow);
}}
.timeline-frame {{
    display: block;
    width: 100%;
    min-height: 76vh;
    border: 0;
    background: #05070a;
}}

.footer {{ text-align: center; padding: 22px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--line); margin-top: 40px; }}
@media (max-width: 1120px) {{
    .header {{ grid-template-columns: 1fr; min-height: auto; }}
    .header::after {{ opacity: .22; }}
	    .stats-row {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
	    .lifeline-hero {{ grid-template-columns: 1fr; min-height: auto; }}
	    .lifeline-status {{ justify-self: stretch; text-align: left; }}
	    .lifeline-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
	    .lifeline-actions {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .feishu-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .agent-grid {{ grid-template-columns: 1fr; }}
    .people-hero {{ grid-template-columns: 1fr; min-height: auto; }}
    .people-grid {{ grid-template-columns: 1fr; }}
    .network-grid {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 680px) {{
    .header {{ padding: 28px 18px; }}
    .header h1 {{ font-size: 42px; }}
    .hero-console {{ min-height: auto; }}
	    .console-grid, .stats-row, .feishu-grid, .lifeline-grid, .lifeline-actions {{ grid-template-columns: 1fr; }}
    .container {{ padding: 22px 14px 40px; }}
    .people-toolbar {{ grid-template-columns: 1fr; }}
    .people-meta {{ flex-direction: column; }}
    .network-toolbar {{ grid-template-columns: 1fr; }}
    .network-meta {{ flex-direction: column; }}
    .relationship-summary-head {{ flex-direction: column; }}
    .relationship-summary-grid {{ grid-template-columns: 1fr; }}
    .relationship-summary-skip {{ text-align: left; }}
    .network-edge-head {{ grid-template-columns: 1fr; }}
    .network-edge-score {{ justify-self: start; }}
    .person-head {{ grid-template-columns: 46px minmax(0, 1fr); }}
    .person-count {{ grid-column: 1 / -1; min-width: 0; text-align: left; }}
    .person-detail {{ width: 100vw; border-left: 0; }}
    .person-detail-top {{ grid-template-columns: 50px minmax(0, 1fr) 38px; gap: 10px; }}
    .person-detail-stats {{ grid-template-columns: 1fr; }}
    .detail-metric {{ grid-template-columns: 1fr; gap: 3px; }}
    .detail-metric span:last-child {{ white-space: normal; }}
    .type-row {{ align-items: flex-start; flex-direction: column; gap: 6px; }}
    .type-name, .type-count {{ width: auto; }}
    .type-bar-bg {{ width: 100%; }}
    .digest-grid, .digest-columns {{ grid-template-columns: 1fr; }}
    .digest-head {{ flex-direction: column; }}
    .digest-person-row {{ grid-template-columns: 1fr; }}
    .digest-person-meta {{ align-items: flex-start; text-align: left; white-space: normal; }}
    .auto-cta {{ grid-template-columns: 1fr; }}
    .review-actions {{ justify-content: flex-start; }}
}}
</style>
</head>
<body>

<div class="header">
    <div>
        <div class="brand-kicker">IMMORTAL MEMORY TERMINAL</div>
        <h1>用户本人 · 赛博永生<br>记忆库 <span class="badge">LIVE</span></h1>
        <div class="subtitle">同一套本地记忆中枢：自动备份、飞书清洗、画像蒸馏、人物档案、上下文召回都在这里汇总。</div>
    </div>
    <div class="hero-console">
        <div class="console-top"><span>CORE STATUS</span><span>{last_collect}</span></div>
        <div class="console-grid">
        <div class="console-cell"><div class="v">{total:,}</div><div class="l">总记忆记录</div></div>
        <div class="console-cell"><div class="v">{feishu['clean_records']:,}</div><div class="l">飞书清洗记录</div></div>
        <div class="console-cell"><div class="v">{people_index['count']:,}</div><div class="l">人物档案</div></div>
        <div class="console-cell"><div class="v">{int(quality.get('score') or 0) if quality else 0}</div><div class="l">记忆质量分</div></div>
        </div>
    </div>
</div>

	<div class="tabs">
	    <div class="tab active" data-tab="lifeline">防丢失</div>
	    <div class="tab" data-tab="feedback">运行反馈</div>
	    <div class="tab" data-tab="feishu">自动沉淀</div>
	    <div class="tab" data-tab="search">搜索</div>
	    <div class="tab" data-tab="summaries">每日摘要</div>
	    <div class="tab" data-tab="people">人物档案</div>
	    <div class="tab" data-tab="soul">数字分身</div>
	    <div class="tab" data-tab="agent">Agent 接入</div>
	    <div class="tab" data-tab="factory">任务上下文生成器</div>
	    <div class="tab" data-tab="overview">概览</div>
	    <div class="tab" data-tab="sources">数据源</div>
	    <div class="tab" data-tab="timeline">时间线</div>
	    <div class="tab" data-tab="network">关联证据</div>
	</div>

<div class="container">

	<!-- Lifeline Tab -->
	<div class="section active" id="lifeline">
	    {lifeline_panel_html}
	    {quality_panel_html}
	</div>

	<!-- Feedback Tab -->
	<div class="section" id="feedback">
	    {feedback_panel_html}
	</div>
	
	<!-- People Tab -->
	<div class="section" id="people">
    <div class="people-hero">
        <div>
            <div class="section-kicker">PEOPLE MEMORY INDEX</div>
	            <h2>人物档案</h2>
	            <p>这里展示记忆库自动整理出来的人物介绍和可用结论。人物档案服务于召回和长期画像，原始证据与关联网络只作为支撑材料。</p>
        </div>
        <div class="people-stats">
            <div><b>{people_index['count']:,}</b><span>人物</span></div>
            <div><b>{people_index['memory_total']:,}</b><span>人物相关记忆</span></div>
            <div><b>{people_index['latest_date'] or '-'}</b><span>最近证据</span></div>
        </div>
    </div>
    <div class="people-toolbar">
        <input class="people-search" id="people-search" placeholder="搜索人物、介绍、项目、代表性记忆">
        <select class="people-select" id="people-category">
            <option value="all">全部人物</option>
            <option value="self">用户本人</option>
            <option value="team">团队协作</option>
            <option value="business">商务协作</option>
            <option value="customer">客户/外部</option>
            <option value="other">其他</option>
        </select>
        <select class="people-select" id="people-sort">
            <option value="rank">默认排序</option>
            <option value="count">记忆数</option>
            <option value="latest">最近更新</option>
            <option value="name">姓名</option>
        </select>
    </div>
    <div class="people-meta">
        <span id="people-count-line">0 人</span>
        <span>索引文件：{html.escape(people_index['path'])}</span>
    </div>
    <div class="people-grid" id="people-grid"></div>
    <div class="person-detail-backdrop" id="person-detail-backdrop" aria-hidden="true"></div>
    <aside class="person-detail" id="person-detail" aria-hidden="true" aria-label="人物详情">
        <div class="person-detail-content" id="person-detail-content"></div>
    </aside>
</div>

	<!-- Evidence Network Tab -->
	<div class="section" id="network">
	    <div class="people-hero network-hero">
	        <div>
	            <div class="section-kicker">EVIDENCE NETWORK</div>
	            <h2>关联证据</h2>
	            <p>这是辅助证据层，用来解释人物、项目和记忆为什么被关联。它不作为永生记忆库主线结论，使用时应结合召回结果和原始证据判断。</p>
	        </div>
	        <div class="people-stats">
	            <div><b>{relationship_index['people_nodes']:,}</b><span>人物节点</span></div>
	            <div><b>{relationship_index['project_nodes']:,}</b><span>项目节点</span></div>
	            <div><b>{relationship_index['person_edges'] + relationship_index['project_edges']:,}</b><span>证据边</span></div>
	        </div>
	    </div>
    <div class="network-toolbar">
        <input class="network-search" id="network-search" placeholder="搜索人物、项目、关系类型、证据">
        <select class="network-select" id="network-kind">
            <option value="person_person" selected>人物关系</option>
            <option value="person_project">人物-项目</option>
            <option value="all">全部关系</option>
        </select>
        <select class="network-select" id="network-sort">
            <option value="score">默认排序</option>
            <option value="count">关系次数</option>
            <option value="latest">最近更新</option>
            <option value="name">名称</option>
        </select>
    </div>
    <div class="network-meta">
        <span id="network-count-line">0 条证据</span>
        <span>索引文件：{html.escape(relationship_index['path'])}</span>
    </div>
    {relationship_summary_html}
    <div class="network-grid">
        <div class="network-panel">
            <div class="network-panel-head"><span>核心节点</span><span id="network-node-count">0</span></div>
            <div class="network-panel-body">
                <div class="network-node-cloud" id="network-node-cloud"></div>
            </div>
        </div>
        <div class="network-panel">
            <div class="network-panel-head"><span>证据边</span><span id="network-edge-count">0</span></div>
            <div class="network-panel-body">
                <div class="network-edge-list" id="network-edge-list"></div>
            </div>
        </div>
    </div>
</div>

<!-- Overview Tab -->
<div class="section" id="overview">
    <div class="stats-row">
        <div class="stat-card"><div class="label">总记录数</div><div class="value blue">{total:,}</div></div>
        <div class="stat-card"><div class="label">用户发言</div><div class="value green">{user_count:,}</div></div>
        <div class="stat-card"><div class="label">AI 回复</div><div class="value purple">{assistant_count:,}</div></div>
        <div class="stat-card"><div class="label">活跃天数</div><div class="value orange">{len(stats['dates'])}</div></div>
        <div class="stat-card"><div class="label">采集次数</div><div class="value blue">{collect_count}</div></div>
        <div class="stat-card"><div class="label">质量状态</div><div class="value green" style="font-size:18px">{html.escape(quality_label(str(quality.get('status') or 'missing')))}</div></div>
    </div>
    {quality_panel_html}

    <div class="soul-banner">
        <h2>Digital Soul v2.0</h2>
        <p>从 {total:,} 条记录中自动蒸馏的认知模型。飞书新数据会自动清洗、蒸馏、归因并进入长期画像与人物档案层，不需要人工逐条确认。</p>
        <div class="stats">
            <div class="s"><div class="v">{soul_size_kb:.0f} KB</div><div class="l">人格文件</div></div>
            <div class="s"><div class="v">{soul_lines}</div><div class="l">行</div></div>
            <div class="s"><div class="v">{len(soul_toc)}</div><div class="l">章节</div></div>
            <div class="s"><div class="v">{feishu['memories']:,}</div><div class="l">飞书蒸馏</div></div>
        </div>
    </div>

    <div class="panel" style="margin-bottom:24px">
        <div class="panel-header">30 天活跃度趋势</div>
        <div class="panel-body">
            <div class="trend" id="trend"></div>
        </div>
    </div>

    <div class="grid">
        <div class="panel">
            <div class="panel-header">数据类型分布</div>
            <div class="panel-body" id="types-panel"></div>
        </div>
        <div class="panel">
            <div class="panel-header">角色分布</div>
            <div class="panel-body">
                <div style="display:flex;gap:24px;flex-wrap:wrap">
                    <div><div style="font-size:24px;font-weight:780;color:var(--blue)">{user_count:,}</div><div style="font-size:11px;color:var(--muted)">用户</div></div>
                    <div><div style="font-size:24px;font-weight:780;color:var(--green)">{assistant_count:,}</div><div style="font-size:11px;color:var(--muted)">AI 助手</div></div>
                    <div><div style="font-size:24px;font-weight:780;color:var(--amber)">{stats['roles'].get('system', 0):,}</div><div style="font-size:11px;color:var(--muted)">系统</div></div>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- Feishu Tab -->
<div class="section" id="feishu">
    <div class="digest-panel">
        <div class="digest-head">
            <div>
	                <h3>每日变化摘要</h3>
	                <p>每轮自动任务完成后生成，只展示新增量、人物变化、沉淀状态和需要 Codex 处理的异常。</p>
            </div>
            <div class="digest-badge">{html.escape((digest.get("errors") or {}).get("status", "unknown")).upper()}</div>
        </div>
        <div class="digest-grid">
            <div class="digest-card">
                <div class="k">最近采集</div>
                <div class="v" style="font-size:16px">{html.escape(str((digest.get("summary") or {}).get("recent_collect_time_local") or format_digest_time(state.get("last_collect"))))}</div>
                <div class="t">collect #{int((digest.get("summary") or {}).get("collect_count") or collect_count):,}</div>
            </div>
            <div class="digest-card">
                <div class="k">本次新增</div>
                <div class="v">{int((digest.get("summary") or {}).get("new_records") or state.get("last_run_new_records") or 0):,}</div>
                <div class="t">总记录 {int((digest.get("summary") or {}).get("total_records") or total):,}</div>
            </div>
            <div class="digest-card">
                <div class="k">飞书新增</div>
                <div class="v">{int((digest.get("summary") or {}).get("feishu_new_records") or state.get("last_run_feishu_new_records") or 0):,}</div>
                <div class="t">已清洗 {int(((digest.get("feishu") or {}).get("clean") or {}).get("clean_records") or feishu['clean_records']):,}</div>
            </div>
            <div class="digest-card">
                <div class="k">人物档案</div>
                <div class="v">{int(((digest.get("people") or {}).get("count") or people_index['count'])):,}</div>
                <div class="t">最近证据 {html.escape(str((digest.get("people") or {}).get("latest_date") or people_index['latest_date'] or "-"))}</div>
            </div>
            <div class="digest-card">
	                <div class="k">关联证据</div>
                <div class="v">{int(((digest.get("relationships") or {}).get("person_edges") or relationship_index['person_edges'])) + int(((digest.get("relationships") or {}).get("project_edges") or relationship_index['project_edges'])):,}</div>
                <div class="t">人物 {int(((digest.get("relationships") or {}).get("person_edges") or relationship_index['person_edges'])):,} / 项目 {int(((digest.get("relationships") or {}).get("project_edges") or relationship_index['project_edges'])):,}</div>
            </div>
            <div class="digest-card">
                <div class="k">记忆质量</div>
                <div class="v">{int(((digest.get("quality") or {}).get("score") or quality.get("score") or 0)):,}</div>
                <div class="t">{html.escape(quality_label(str(((digest.get("quality") or {}).get("status") or quality.get("status") or "missing"))))}</div>
            </div>
        </div>
        {quality_panel_html}
        <div class="digest-columns">
            <div class="panel">
                <div class="panel-header">最近更新人物</div>
                <div class="panel-body digest-list">
                    {render_digest_people((digest.get("people") or {}).get("recently_updated") or [])}
                </div>
            </div>
            <div class="panel">
                <div class="panel-header">关注项</div>
                <div class="panel-body">
                    <ul class="digest-attention">
                        {render_digest_attention(digest.get("attention") or [])}
                    </ul>
                </div>
            </div>
        </div>
    </div>
    <div class="feishu-grid">
        <div class="feishu-card"><div class="k">清洗记录</div><div class="n">{feishu['clean_records']:,}</div></div>
        <div class="feishu-card"><div class="k">结构化记忆</div><div class="n">{feishu['memories']:,}</div></div>
        <div class="feishu-card"><div class="k">参考记忆</div><div class="n">{feishu['reference_memories']:,}</div></div>
        <div class="feishu-card warn"><div class="k">敏感项跳过</div><div class="n">{feishu['secret_skipped']:,}</div></div>
        <div class="feishu-card"><div class="k">长期画像</div><div class="n">{feishu['reviewed_profile']:,}</div></div>
        <div class="feishu-card"><div class="k">人物索引</div><div class="n">{people_index['count']:,}</div></div>
	        <div class="feishu-card"><div class="k">证据边</div><div class="n">{relationship_index['person_edges'] + relationship_index['project_edges']:,}</div></div>
        <div class="feishu-card"><div class="k">聊天日表</div><div class="n">{feishu['chat_daily']:,}</div></div>
    </div>
    <div class="auto-cta">
        <div>
            <h3>全自动长期记忆沉淀</h3>
	            <p>采集、清洗、蒸馏、身份归因、人物索引、关联证据和看板刷新都由后台编排器完成。日常只看结果，过程细节收进调试区。</p>
        </div>
        <div class="digest-badge">AUTO</div>
    </div>
    <div class="panel">
        <div class="panel-header">已合并长期画像</div>
        <div class="panel-body">
            <div class="feishu-preview">{feishu_preview_html}</div>
            <details class="feishu-debug">
                <summary>调试信息</summary>
                <div class="feishu-debug-body">
                    <div class="feishu-path">长期画像文件：{html.escape(feishu['reviewed_path'])}</div>
                    <div class="feishu-path">自动识别文件：{html.escape(feishu['proposal_path'])}</div>
                    <div class="review-actions">
                        <span class="review-command">{html.escape(feishu['review_command'])}</span>
                        <span class="review-command">python3 ~/.codex/skills/immortal/immortal.py people</span>
                        <span class="review-command">python3 ~/.codex/skills/immortal/immortal.py relationships</span>
                    </div>
                    <div class="feishu-grid" style="margin:0">
                        <div class="feishu-card"><div class="k">待归因素材</div><div class="n">{feishu['candidate_memories']:,}</div></div>
                        <div class="feishu-card"><div class="k">画像蒸馏</div><div class="n">{feishu['profile_memories']:,}</div></div>
                        <div class="feishu-card"><div class="k">自动识别项</div><div class="n">{feishu['proposal_candidates']:,}</div></div>
                        <div class="feishu-card"><div class="k">已自动归档</div><div class="n">{feishu['proposal_checked']:,}</div></div>
                    </div>
                </div>
            </details>
        </div>
    </div>
</div>

<!-- Soul Tab -->
<div class="section" id="soul">
    <div class="soul-toc" id="soul-toc"></div>
    <div class="soul-content">{soul_html}</div>
</div>

<!-- Agent Entry Tab -->
<div class="section" id="agent">
    <div class="people-hero agent-hero">
        <div>
            <div class="section-kicker">AGENT BRIDGE</div>
            <h2>Agent 接入</h2>
            <p>这是给 Claude Code、Codex 和其他本地 Agent 的统一入口。外部 Agent 不直接读完整原始库，只按当前任务生成上下文包。</p>
        </div>
        <div class="people-stats">
            <div><b>{total:,}</b><span>可召回记录</span></div>
            <div><b>{int(quality.get('score') or 0)}</b><span>质量分</span></div>
            <div><b>{html.escape(latest_agent_context_status)}</b><span>最近上下文包</span></div>
        </div>
    </div>
    <div class="agent-grid">
        <div class="agent-card">
            <h3>复制给其他 Agent 的一句话</h3>
            <p>本地有 shell / 文件权限的 Agent 用这段就够了。它会先读取入口，再为当前任务生成专用上下文。</p>
            <div class="handoff-box">
                <b>HANDOFF PROMPT</b>
                <textarea class="handoff-copy" readonly>{html.escape(agent_handoff_prompt)}</textarea>
            </div>
            <div class="agent-command-list">
                <code>python3 ~/.codex/skills/immortal/immortal.py agent-entry</code>
                <code>python3 ~/.codex/skills/immortal/immortal.py agent-context "当前任务" --print</code>
                <code>python3 ~/.codex/skills/immortal/immortal.py recall "主题"</code>
            </div>
        </div>
        <div class="agent-card">
            <h3>当前入口内容</h3>
            <div class="entry-preview">{agent_entry_html}</div>
        </div>
    </div>
</div>

<!-- Task Context Compiler Tab -->
<div class="section" id="factory">
    <div class="people-hero factory-hero">
        <div>
            <div class="section-kicker">TASK CONTEXT COMPILER</div>
            <h2>任务上下文生成器</h2>
            <p>采集、清洗、健康检查、短期任务上下文生成都在主看板内完成。这个页面仍调用白名单任务接口，避免任意命令执行。</p>
        </div>
        <div class="people-stats">
            <div><b>{len((digest.get('roles') or [])) if isinstance(digest, dict) else 0}</b><span>摘要角色</span></div>
            <div><b>{people_index['count']:,}</b><span>人物档案</span></div>
            <div><b>{html.escape(str((digest.get("errors") or {}).get("status", "unknown")).upper())}</b><span>自动链路</span></div>
        </div>
    </div>
    <div class="embedded-shell">
        <iframe class="factory-frame" src="/agent-factory?embed=1" title="任务上下文生成器"></iframe>
    </div>
</div>

<!-- Search Tab -->
<div class="section" id="search">
    <div class="search-box">
        <input type="text" class="search-input" id="search-input" placeholder="在记忆中搜索…（如：飞书、写作、招聘）">
    </div>
    <div class="search-hint">前端搜索基于采样数据（每日 10 条用户发言）。要全量搜索 {total:,} 条记录，请运行下方命令：</div>
    <div class="cmd-tip">python3 ~/.codex/skills/immortal/immortal.py recall "你的关键词"</div>
    <div class="search-results" id="search-results" style="margin-top:16px"></div>
</div>

<!-- Summaries Tab -->
<div class="section" id="summaries">
    <div class="summary-list" id="summary-list"></div>
</div>

<!-- Sources Tab -->
<div class="section" id="sources">
    <div class="grid">
        <div class="panel" style="grid-column: 1 / -1">
            <div class="panel-header">📡 数据源详情</div>
            <div class="panel-body" id="sources-panel"></div>
        </div>
    </div>
</div>

<!-- Timeline Tab -->
<div class="section" id="timeline">
    <div class="people-hero timeline-hero">
        <div>
            <div class="section-kicker">TIMELINE MEMORY VIEW</div>
            <h2>时间线</h2>
            <p>这是同一套记忆库按日期展开的浏览视图，用来回看每天产生了什么记录。它已经并入主看板，不再作为第二个项目入口。</p>
        </div>
        <div class="people-stats">
            <div><b>{total:,}</b><span>总记录</span></div>
            <div><b>{len(stats['dates'])}</b><span>活跃天数</span></div>
            <div><b>{last_collect}</b><span>最后采集</span></div>
        </div>
    </div>
    <div class="timeline-shell">
        <iframe class="timeline-frame" src="/timeline?embed=1" title="用户本人 · 赛博永生记忆库时间线"></iframe>
    </div>
</div>

</div>

<div class="footer">用户本人 · 赛博永生记忆库 v0.3 · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>

<script>
const TREND = {trend_json};
const SUMMARIES = {summaries_json};
const SAMPLES = {samples_json};
const TYPES = {json.dumps(stats['types'])};
const SOURCES = {json.dumps(stats['sources'])};
const SOURCE_ICONS = {json.dumps(source_icons)};
const SOURCE_GROUPS = {json.dumps(source_groups)};
const SOUL_TOC = {json.dumps(soul_toc, ensure_ascii=False)};
const PEOPLE = {people_json};
const PEOPLE_CATEGORIES = {people_categories_json};
const RELATIONSHIPS = {relationships_json};
const DIGEST = {digest_json};
const TOTAL = {total};

// Tabs
function activateTab(name, pushHash = true) {{
    const tab = document.querySelector(`.tab[data-tab="${{name}}"]`);
    const section = document.getElementById(name);
    if (!tab || !section) return false;
    document.querySelectorAll('.tab[data-tab]').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    tab.classList.add('active');
    section.classList.add('active');
    if (pushHash) history.replaceState(null, '', `#${{name}}`);
    return true;
}}
document.querySelectorAll('.tab[data-tab]').forEach(tab => {{
    tab.onclick = () => activateTab(tab.dataset.tab);
}});
document.querySelectorAll('a[href^="#"]').forEach(link => {{
    link.onclick = event => {{
        const name = link.getAttribute('href').slice(1);
        if (activateTab(name)) event.preventDefault();
    }};
}});
if (window.location.hash) {{
    activateTab(window.location.hash.slice(1), false);
}}

function escapeHtml(value) {{
    return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

function personInitial(name) {{
    const clean = String(name || '').replace(/[（(].*$/, '').trim();
    return clean.slice(0, 2) || '人';
}}

function categoryLabel(value) {{
    return {{
        self: '用户本人',
        team: '团队协作',
        business: '商务协作',
        customer: '客户/外部',
        other: '其他'
    }}[value] || value || '其他';
}}

function relationKindLabel(value) {{
    return {{
        person_person: '人物关系',
        person_project: '人物-项目'
    }}[value] || value || '关系';
}}

function metricName(item) {{
    if (item == null) return '';
    if (typeof item !== 'object') return String(item);
    return item.label || item.name || item.title || item.id || item.type || item.key || '';
}}

function metricCount(item) {{
    if (!item || typeof item !== 'object') return '';
    return item.count ?? item.value ?? item.total ?? '';
}}

function renderMetricList(items, emptyText = '暂无') {{
    const rows = Array.isArray(items) ? items : [];
    if (!rows.length) return `<div class="detail-section-text">${{escapeHtml(emptyText)}}</div>`;
    return `<div class="detail-metric-list">${{rows.map(item => {{
        const name = metricName(item) || '未命名';
        const count = metricCount(item);
        const suffix = count === '' ? '' : `<span>${{escapeHtml(count)}}</span>`;
        return `<div class="detail-metric"><span>${{escapeHtml(name)}}</span>${{suffix}}</div>`;
    }}).join('')}}</div>`;
}}

function detailFieldText(value) {{
    if (value == null) return '';
    if (Array.isArray(value)) return value.map(detailFieldText).filter(Boolean).join(' / ');
    if (typeof value === 'object') {{
        return value.text || value.content || value.summary || value.description || value.statement || value.value || '';
    }}
    return String(value);
}}

function normalizeExtraSections(value) {{
    if (!value) return [];
    if (Array.isArray(value)) return value.map(section => {{
        if (section && typeof section === 'object') return section;
        return {{ title: '补充信息', text: String(section ?? '') }};
    }});
    if (typeof value === 'object') {{
        return Object.entries(value).map(([title, content]) => {{
            if (content && typeof content === 'object' && !Array.isArray(content)) {{
                return {{ title, ...content }};
            }}
            if (Array.isArray(content)) return {{ title, items: content }};
            return {{ title, text: String(content ?? '') }};
        }});
    }}
    return [{{ title: '补充信息', text: String(value) }}];
}}

function renderExtraSectionItem(item) {{
    if (item == null) return '';
    if (typeof item !== 'object') {{
        return `<div class="detail-section-item">${{escapeHtml(item)}}</div>`;
    }}
    const title = item.title || item.label || item.name || item.heading || '';
    const text = detailFieldText(item.text ?? item.content ?? item.summary ?? item.description ?? item.statement ?? item.value);
    const meta = [item.date, item.valid_from, item.memory_type_label || item.memory_type, item.source_title].filter(Boolean).join(' · ');
    const body = text || Object.entries(item)
        .filter(([key]) => !['title', 'label', 'name', 'heading', 'date', 'valid_from', 'memory_type_label', 'memory_type', 'source_title'].includes(key))
        .map(([key, value]) => `${{key}}: ${{detailFieldText(value)}}`)
        .filter(line => !line.endsWith(': '))
        .join(' / ');
    return `<div class="detail-section-item">
        ${{title ? `<strong>${{escapeHtml(title)}}</strong>` : ''}}
        <div>${{escapeHtml(body || '暂无内容')}}</div>
        ${{meta ? `<div class="highlight-meta">${{escapeHtml(meta)}}</div>` : ''}}
    </div>`;
}}

function confidenceLabel(person) {{
    if (person.confidence_label) return person.confidence_label;
    if (person.confidence === 'low') return '证据偏少';
    if (person.confidence === 'medium') return '证据中等';
    return '';
}}

function confidenceView(value) {{
    return {{
        high: {{ label: '稳定档案', detail: '证据成熟', tone: 'high' }},
        medium: {{ label: '继续补强', detail: '已有可用线索，系统继续补证据', tone: 'medium' }},
        low: {{ label: '自动补证据', detail: '线索档案，系统继续自动补强', tone: 'low' }}
    }}[value] || {{ label: '未标注', detail: '等待更多证据', tone: 'unknown' }};
}}

function confidenceBadge(value, detail = '') {{
    const view = confidenceView(value);
    const text = detail || view.detail;
    return `<span class="confidence-badge confidence-${{escapeHtml(view.tone)}}">${{escapeHtml(view.label)}}${{text ? ' · ' + escapeHtml(text) : ''}}</span>`;
}}

function profileField(person, key, fallback = '') {{
    const profile = person && typeof person.profile === 'object' ? person.profile : {{}};
    return profile[key] || fallback || '';
}}

function renderProfileLines(person, keys) {{
    const labels = {{
        role_summary: '角色摘要',
        relationship_to_user: '与用户本人关系',
        work_context: '工作上下文',
        evidence_maturity: '证据成熟度',
        current_line: '当前判断'
    }};
    const rows = keys.map(key => {{
        const value = profileField(person, key);
        if (!value) return '';
        return `<div class="person-profile-line"><b>${{escapeHtml(labels[key] || key)}}</b><span>${{escapeHtml(value)}}</span></div>`;
    }}).filter(Boolean).join('');
    return rows ? `<div class="person-profile-grid">${{rows}}</div>` : '';
}}

function relationConfidenceView(edge) {{
    const value = edge.confidence || 'high';
    const detail = edge.confidence_label || (value === 'low' ? '低置信关系，自动补证据' : value === 'medium' ? '项目热度，分口径比较' : '高置信关系');
    return confidenceBadge(value, detail);
}}

function sourceLayerLabel(id) {{
    return {{
        reviewed_profile: '长期画像层',
        profile_memory: '画像蒸馏层',
        reference_memory: '参考记忆',
        distilled_memory: '全部蒸馏记忆'
    }}[id] || id;
}}

function sourceLayerText(sourceLayers) {{
    const entries = Object.entries(sourceLayers || {{}}).slice(0, 4);
    if (!entries.length) return '来源层级：暂无';
    return '来源层级：' + entries.map(([key, count]) => `${{sourceLayerLabel(key)}} ${{count}}`).join(' / ');
}}

function relationshipPersonNodeIds(person) {{
    const names = new Set([person.name, ...(person.aliases || [])]
        .filter(Boolean)
        .map(value => String(value).trim()));
    return (RELATIONSHIPS.nodes || [])
        .filter(node => {{
            if (!node || node.kind !== 'person') return false;
            return names.has(String(node.label || '').trim()) || names.has(String(node.key || '').trim());
        }})
        .map(node => node.id);
}}

function relatedEdgesForPerson(person) {{
    const ids = new Set(relationshipPersonNodeIds(person));
    if (!ids.size) return [];
    return (RELATIONSHIPS.edges || [])
        .filter(edge => ids.has(edge.source) || ids.has(edge.target))
        .sort((a, b) => {{
            const kindRankA = a.kind === 'person_person' ? 0 : 1;
            const kindRankB = b.kind === 'person_person' ? 0 : 1;
            return kindRankA - kindRankB
                || Number(b.score || 0) - Number(a.score || 0)
                || Number(b.count || 0) - Number(a.count || 0)
                || String(b.latest_date || '').localeCompare(String(a.latest_date || ''));
        }});
}}

function renderPersonRelationshipEdges(person) {{
    const ids = new Set(relationshipPersonNodeIds(person));
    const edges = relatedEdgesForPerson(person)
        .filter(edge => edge.kind === 'person_person' || edge.relation_type !== 'co_mention_weak')
        .slice(0, 8);
    if (!edges.length) return '<div class="network-empty">暂无关联证据</div>';
    return `<div class="network-edge-list">${{edges.map(edge => renderNetworkEdge(edge, ids)).join('')}}</div>`;
}}

function renderExtraSections(sections, fallbackTitle) {{
    return normalizeExtraSections(sections).map(section => {{
        const title = section.title || section.heading || section.name || fallbackTitle;
        const text = detailFieldText(section.text ?? section.content ?? section.summary ?? section.description ?? section.body);
        const items = Array.isArray(section.items) ? section.items : (Array.isArray(section.children) ? section.children : []);
        return `<section class="person-detail-section">
            <h4>${{escapeHtml(title || fallbackTitle)}}</h4>
            ${{text ? `<div class="detail-section-text">${{escapeHtml(text)}}</div>` : ''}}
            ${{items.length ? `<div class="detail-section-items">${{items.map(renderExtraSectionItem).join('')}}</div>` : ''}}
        </section>`;
    }}).join('');
}}

function renderPersonDetail(person) {{
    const aliases = (person.aliases || []).map(alias => `<span class="pill">${{escapeHtml(alias)}}</span>`).join('');
    const co = (person.co_mentions || []).map(item =>
        `<span class="pill">${{escapeHtml(item.name || metricName(item))}}${{metricCount(item) !== '' ? ' · ' + escapeHtml(metricCount(item)) : ''}}</span>`
    ).join('');
    const highlights = (person.highlights || []).map(item =>
        `<div class="person-highlight-detail">
            <div>${{escapeHtml(item.statement || item.text || item.content || '')}}</div>
            <div class="highlight-meta">${{escapeHtml([item.valid_from, item.memory_type_label || item.memory_type, item.source_title, item.origin].filter(Boolean).join(' · ') || '-')}}</div>
        </div>`
    ).join('');
    const relationshipEdges = renderPersonRelationshipEdges(person);
    const personConfidence = confidenceView(person.confidence || 'high');
    const profileLines = renderProfileLines(person, ['role_summary', 'relationship_to_user', 'work_context', 'evidence_maturity', 'current_line']);
    return `<div class="person-detail-top">
        <div class="avatar">${{escapeHtml(personInitial(person.name))}}</div>
        <div class="person-detail-title">
            <h3>${{escapeHtml(person.name || '未命名人物')}}</h3>
            <p>${{escapeHtml(categoryLabel(person.category))}} · ${{escapeHtml(personConfidence.label)}}${{person.latest_date ? ' · 最近证据 ' + escapeHtml(person.latest_date) : ''}}</p>
        </div>
        <button class="person-detail-close" type="button" aria-label="关闭人物详情">×</button>
    </div>
    ${{profileLines || `<div class="person-detail-intro">${{escapeHtml(person.intro || '暂无人物介绍')}}</div>`}}
    ${{confidenceLabel(person) ? `<div class="detail-section-text" style="margin:-6px 0 14px">${{confidenceBadge(person.confidence || 'high', confidenceLabel(person))}}</div>` : ''}}
    <div class="person-detail-stats">
        <div class="person-detail-stat"><b>${{Number(person.memory_count || 0).toLocaleString('zh-CN')}}</b><span>人物相关记忆</span></div>
        <div class="person-detail-stat"><b>${{(person.top_projects || []).length}}</b><span>关联项目</span></div>
        <div class="person-detail-stat"><b>${{(person.highlights || []).length}}</b><span>代表证据</span></div>
    </div>
    <section class="person-detail-section">
        <h4>身份别名</h4>
        <div class="pill-row">${{aliases || '<span class="pill">暂无</span>'}}</div>
    </section>
    ${{renderExtraSections(person.overview_sections, '扩展概览')}}
    <section class="person-detail-section">
	        <h4>关联证据网络</h4>
        ${{relationshipEdges}}
    </section>
    <section class="person-detail-section">
        <h4>参与项目</h4>
        ${{renderMetricList(person.top_projects)}}
    </section>
    <section class="person-detail-section">
        <h4>相关人物</h4>
        <div class="co-list">${{co || '<span class="pill">暂无</span>'}}</div>
    </section>
    <section class="person-detail-section">
        <h4>关键证据</h4>
        <div class="person-highlight-list">${{highlights || '<div class="person-highlight-detail">暂无代表性证据</div>'}}</div>
    </section>
    <section class="person-detail-section">
        <h4>记忆类型</h4>
        ${{renderMetricList(person.memory_types)}}
    </section>
    <section class="person-detail-section">
        <h4>来源分布</h4>
        ${{renderMetricList(person.source_layers)}}
    </section>
    ${{renderExtraSections(person.detail_sections, '扩展详情')}}`;
}}

function openPersonDetail(person) {{
    const detail = document.getElementById('person-detail');
    const content = document.getElementById('person-detail-content');
    const backdrop = document.getElementById('person-detail-backdrop');
    if (!detail || !content || !backdrop || !person) return;
    content.innerHTML = renderPersonDetail(person);
    detail.classList.add('open');
    backdrop.classList.add('open');
    detail.setAttribute('aria-hidden', 'false');
    backdrop.setAttribute('aria-hidden', 'false');
    content.querySelector('.person-detail-close')?.focus({{ preventScroll: true }});
    content.querySelector('.person-detail-close')?.addEventListener('click', closePersonDetail);
}}

function closePersonDetail() {{
    const detail = document.getElementById('person-detail');
    const backdrop = document.getElementById('person-detail-backdrop');
    detail?.classList.remove('open');
    backdrop?.classList.remove('open');
    detail?.setAttribute('aria-hidden', 'true');
    backdrop?.setAttribute('aria-hidden', 'true');
}}

function renderPeople() {{
    const q = document.getElementById('people-search').value.trim().toLowerCase();
    const category = document.getElementById('people-category').value;
    const sort = document.getElementById('people-sort').value;
    let rows = PEOPLE.filter(person => {{
        if (category !== 'all' && person.category !== category) return false;
        if (!q) return true;
        const hay = [
            person.name,
            person.intro,
            ...(person.aliases || []),
            ...(person.top_projects || []).map(x => x.label || x.id),
            ...(person.memory_types || []).map(x => x.label || x.id),
            ...(person.source_layers || []).map(x => x.label || x.id),
            ...(person.co_mentions || []).map(x => x.name),
            ...(person.highlights || []).map(x => x.statement),
            ...normalizeExtraSections(person.overview_sections).map(x => detailFieldText(x.text ?? x.content ?? x.summary ?? x.description ?? x.body)),
            ...normalizeExtraSections(person.detail_sections).map(x => detailFieldText(x.text ?? x.content ?? x.summary ?? x.description ?? x.body))
        ].join(' ').toLowerCase();
        return hay.includes(q);
    }});
    const categoryRank = {{ self: 0, team: 1, business: 2, customer: 3, other: 4 }};
    rows.sort((a, b) => {{
        if (sort === 'count') return Number(b.memory_count || 0) - Number(a.memory_count || 0);
        if (sort === 'latest') return String(b.latest_date || '').localeCompare(String(a.latest_date || ''));
        if (sort === 'name') return String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN');
        return (categoryRank[a.category] ?? 9) - (categoryRank[b.category] ?? 9)
            || Number(b.memory_count || 0) - Number(a.memory_count || 0)
            || String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN');
    }});
    document.getElementById('people-count-line').textContent = `${{rows.length}} 人 / ${{PEOPLE.length}} 人`;
    const grid = document.getElementById('people-grid');
    if (!rows.length) {{
        grid.innerHTML = '<div class="empty-people">没有匹配人物</div>';
        return;
    }}
    grid.innerHTML = rows.map(person => {{
        const personIndex = PEOPLE.indexOf(person);
        const projects = (person.top_projects || []).slice(0, 5).map(project =>
            `<span class="pill hot">${{escapeHtml(project.label || project.id)}} · ${{project.count}}</span>`
        ).join('');
        const types = (person.memory_types || []).slice(0, 4).map(type =>
            `<span class="pill">${{escapeHtml(type.label || type.id)}} · ${{type.count}}</span>`
        ).join('');
        const co = (person.co_mentions || []).slice(0, 6).map(item =>
            `<span class="pill">${{escapeHtml(item.name)}} · ${{item.count}}</span>`
        ).join('');
        const highlights = (person.highlights || []).slice(0, 4).map(item =>
            `<div class="highlight">
                <div>${{escapeHtml(item.statement)}}</div>
                <div class="highlight-meta">${{escapeHtml(item.valid_from || '-')}} · ${{escapeHtml(item.memory_type_label || item.memory_type || '-')}} · ${{escapeHtml(item.source_title || '-')}}</div>
            </div>`
        ).join('');
        const confidence = confidenceLabel(person);
        const view = confidenceView(person.confidence || 'high');
        const profileSummary = renderProfileLines(person, ['role_summary', 'relationship_to_user', 'current_line']);
        return `<article class="person-card person-card-clickable ${{person.category === 'self' ? 'self' : ''}}" data-person-index="${{personIndex}}" tabindex="0" role="button" aria-label="打开 ${{escapeHtml(person.name || '人物')}} 详情">
            <div class="person-head">
                <div class="avatar">${{escapeHtml(personInitial(person.name))}}</div>
                <div>
                    <div class="person-name">${{escapeHtml(person.name)}}</div>
                    <div class="person-sub">${{escapeHtml(categoryLabel(person.category))}} · ${{escapeHtml(view.label)}}${{person.aliases?.length ? ' · ' + escapeHtml(person.aliases.join(' / ')) : ''}}</div>
                </div>
                <div class="person-count"><b>${{Number(person.memory_count || 0).toLocaleString('zh-CN')}}</b><span>条记忆</span></div>
            </div>
            <div class="person-body">
                ${{profileSummary || `<div class="person-intro">${{escapeHtml(person.intro || '')}}</div>`}}
                ${{confidence ? `<div style="margin-bottom:10px">${{confidenceBadge(person.confidence || 'high', confidence)}}</div>` : ''}}
                <div class="person-section-title">关联项目</div>
                <div class="pill-row">${{projects || '<span class="pill">暂无</span>'}}</div>
                <div class="person-section-title">信息类型</div>
                <div class="pill-row">${{types || '<span class="pill">暂无</span>'}}</div>
                <div class="person-section-title">相关人物</div>
                <div class="co-list">${{co || '<span class="pill">暂无</span>'}}</div>
                <div class="person-section-title">代表性记忆</div>
                <div class="highlight-list">${{highlights || '<div class="highlight">暂无代表性证据</div>'}}</div>
            </div>
        </article>`;
    }}).join('');
}}

document.getElementById('people-grid')?.addEventListener('click', event => {{
    const card = event.target.closest('.person-card-clickable');
    if (!card) return;
    openPersonDetail(PEOPLE[Number(card.dataset.personIndex)]);
}});

document.getElementById('people-grid')?.addEventListener('keydown', event => {{
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const card = event.target.closest('.person-card-clickable');
    if (!card) return;
    event.preventDefault();
    openPersonDetail(PEOPLE[Number(card.dataset.personIndex)]);
}});

document.getElementById('person-detail-backdrop')?.addEventListener('click', closePersonDetail);
document.addEventListener('keydown', event => {{
    if (event.key === 'Escape') closePersonDetail();
}});

['people-search', 'people-category', 'people-sort'].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.oninput = () => {{
        closePersonDetail();
        renderPeople();
    }};
}});
renderPeople();

function networkNodeById() {{
    const map = new Map();
    (RELATIONSHIPS.nodes || []).forEach(node => map.set(node.id, node));
    return map;
}}

function edgeSearchText(edge) {{
    return [
        edge.source_label,
        edge.target_label,
        edge.label,
        edge.relation_label,
        edge.relation_type,
        relationKindLabel(edge.kind),
        ...Object.keys(edge.projects || {{}}),
        ...(edge.evidence || []).map(item => [
            item.statement,
            item.source_title,
            item.memory_type_label,
            item.origin_label
        ].filter(Boolean).join(' '))
    ].filter(Boolean).join(' ').toLowerCase();
}}

function renderNetworkNodes(edges) {{
    const nodes = networkNodeById();
    const degree = new Map();
    edges.forEach(edge => {{
        degree.set(edge.source, (degree.get(edge.source) || 0) + Number(edge.count || 0));
        degree.set(edge.target, (degree.get(edge.target) || 0) + Number(edge.count || 0));
    }});
    const ranked = Array.from(degree.entries())
        .map(([id, count]) => ({{ node: nodes.get(id), count }}))
        .filter(item => item.node)
        .sort((a, b) => b.count - a.count || String(a.node.label || '').localeCompare(String(b.node.label || ''), 'zh-CN'))
        .slice(0, 18);
    document.getElementById('network-node-count').textContent = String(ranked.length);
    const cloud = document.getElementById('network-node-cloud');
    if (!ranked.length) {{
        cloud.innerHTML = '<div class="network-empty">暂无匹配节点</div>';
        return;
    }}
    cloud.innerHTML = ranked.map(item => `<div class="network-node">
        <div>
            <b>${{escapeHtml(item.node.label || item.node.key || '未命名节点')}}</b>
            <span>${{escapeHtml(item.node.kind === 'project' ? '项目' : categoryLabel(item.node.category))}}</span>
        </div>
        <em>${{Number(item.count || 0).toLocaleString('zh-CN')}}</em>
    </div>`).join('');
}}

function renderNetworkEdge(edge, focusNodeIds = null) {{
    const evidence = (edge.evidence || [])[0] || {{}};
    const projectTags = Object.entries(edge.projects || {{}}).slice(0, 5).map(([project, count]) =>
        `<span class="pill hot">${{escapeHtml(project)}} · ${{escapeHtml(count)}}</span>`
    ).join('');
    const typeTags = Object.entries(edge.memory_types || {{}}).slice(0, 4).map(([type, count]) =>
        `<span class="pill">${{escapeHtml(type)}} · ${{escapeHtml(count)}}</span>`
    ).join('');
    const evidenceCount = edge.evidence_count || edge.count || (edge.evidence || []).length || 0;
    let sourceLabel = edge.source_label || edge.source;
    let targetLabel = edge.target_label || edge.target;
    const hasFocus = focusNodeIds && typeof focusNodeIds.has === 'function';
    if (hasFocus && focusNodeIds.has(edge.target) && !focusNodeIds.has(edge.source)) {{
        sourceLabel = edge.target_label || edge.target;
        targetLabel = edge.source_label || edge.source;
    }}
    const title = `${{sourceLabel}} ${{edge.kind === 'person_project' ? '→' : '↔'}} ${{targetLabel}}`;
    return `<article class="network-edge ${{escapeHtml(edge.kind || '')}}">
        <div class="network-edge-head">
            <div>
                <div class="network-edge-title">${{escapeHtml(title)}}</div>
                <div class="person-sub">${{escapeHtml(edge.relation_label || edge.label || relationKindLabel(edge.kind))}}${{edge.latest_date ? ' · 最近证据 ' + escapeHtml(edge.latest_date) : ''}}</div>
                ${{relationConfidenceView(edge)}}
            </div>
            <div class="network-edge-score">${{Number(edge.score || 0).toFixed(1)}}</div>
        </div>
        <div class="network-edge-meta">
            <span class="pill">${{escapeHtml(relationKindLabel(edge.kind))}} · ${{Number(edge.count || 0).toLocaleString('zh-CN')}} 条</span>
            <span class="pill">证据 ${{Number(evidenceCount || 0).toLocaleString('zh-CN')}} 条</span>
            <span class="pill">${{escapeHtml(sourceLayerText(edge.source_layers))}}</span>
            ${{projectTags}}
            ${{typeTags}}
        </div>
        <div class="network-edge-evidence">
            <div>${{escapeHtml(evidence.statement || '暂无证据摘要')}}</div>
            <div class="highlight-meta">${{escapeHtml([evidence.valid_from, evidence.memory_type_label || evidence.memory_type, evidence.source_title, evidence.origin_label].filter(Boolean).join(' · ') || '-')}}</div>
        </div>
    </article>`;
}}

function renderNetwork() {{
    const q = document.getElementById('network-search').value.trim().toLowerCase();
    const kind = document.getElementById('network-kind').value;
    const sort = document.getElementById('network-sort').value;
    let edges = (RELATIONSHIPS.edges || []).filter(edge => {{
        if (kind !== 'all' && edge.kind !== kind) return false;
        if (!q) return true;
        return edgeSearchText(edge).includes(q);
    }});
    edges.sort((a, b) => {{
        if (sort === 'count') return Number(b.count || 0) - Number(a.count || 0);
        if (sort === 'latest') return String(b.latest_date || '').localeCompare(String(a.latest_date || ''));
        if (sort === 'name') return String(a.source_label || '').localeCompare(String(b.source_label || ''), 'zh-CN')
            || String(a.target_label || '').localeCompare(String(b.target_label || ''), 'zh-CN');
        return Number(b.score || 0) - Number(a.score || 0)
            || Number(b.count || 0) - Number(a.count || 0)
            || String(b.latest_date || '').localeCompare(String(a.latest_date || ''));
    }});
    document.getElementById('network-count-line').textContent = `${{edges.length}} 条关系 / ${{(RELATIONSHIPS.edges || []).length}} 条`;
    document.getElementById('network-edge-count').textContent = String(Math.min(edges.length, 60));
    renderNetworkNodes(edges);
    const list = document.getElementById('network-edge-list');
    if (!edges.length) {{
        list.innerHTML = '<div class="network-empty">没有匹配关系</div>';
        return;
    }}
    list.innerHTML = edges.slice(0, 60).map(edge => renderNetworkEdge(edge)).join('');
}}

['network-search', 'network-kind', 'network-sort'].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.oninput = renderNetwork;
}});
renderNetwork();

// Trend chart
const trend = document.getElementById('trend');
if (TREND.length > 0) {{
    const max = Math.max(...TREND.map(d => d[1]));
    TREND.forEach(([date, count]) => {{
        const bar = document.createElement('div');
        bar.className = 'trend-bar';
        bar.style.height = (count / max * 100) + '%';
        bar.dataset.tooltip = `${{date.slice(5)}} : ${{count}}`;
        trend.appendChild(bar);
    }});
}}

// Types panel
const typesPanel = document.getElementById('types-panel');
Object.entries(TYPES).forEach(([t, c]) => {{
    const pct = (c / TOTAL * 100).toFixed(1);
    typesPanel.innerHTML += `<div class="type-row"><span class="type-name">${{t}}</span><div class="type-bar-bg"><div class="type-bar-fill" style="width:${{pct}}%"></div></div><span class="type-count">${{c.toLocaleString()}} (${{pct}}%)</span></div>`;
}});

// Sources panel
const sourcesPanel = document.getElementById('sources-panel');
Object.entries(SOURCE_GROUPS).forEach(([groupName, sourceNames]) => {{
    if (!sourceNames.length) return;
    const groupTotal = sourceNames.reduce((s, n) => s + (SOURCES[n] || 0), 0);
    let html = `<div class="source-group"><div class="source-group-header"><span>${{groupName}}</span><span>${{groupTotal.toLocaleString()}}</span></div>`;
    sourceNames.forEach(n => {{
        const cnt = SOURCES[n] || 0;
        const icon = SOURCE_ICONS[n] || '📁';
        html += `<div class="source-item"><span class="source-icon">${{icon}}</span><span class="source-name">${{n}}</span><span class="source-count">${{cnt.toLocaleString()}}</span></div>`;
    }});
    html += '</div>';
    sourcesPanel.innerHTML += html;
}});

// Summaries
const summaryList = document.getElementById('summary-list');
SUMMARIES.forEach(s => {{
    const card = document.createElement('div');
    card.className = 'summary-card';
    card.innerHTML = `<div class="summary-head"><span class="date">${{s.date}}</span><span class="arrow">›</span></div><div class="summary-body">${{s.content.replace(/[<>&]/g, c => ({{'<':'&lt;','>':'&gt;','&':'&amp;'}})[c])}}</div>`;
    card.querySelector('.summary-head').onclick = () => card.classList.toggle('open');
    summaryList.appendChild(card);
}});

// Soul TOC
const tocEl = document.getElementById('soul-toc');
SOUL_TOC.forEach(t => {{
    const a = document.createElement('a');
    a.href = '#';
    a.textContent = t;
    a.onclick = (e) => {{
        e.preventDefault();
        const headers = document.querySelectorAll('.soul-content h2');
        for (const h of headers) {{
            if (h.textContent.includes(t)) {{
                h.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
                break;
            }}
        }}
    }};
    tocEl.appendChild(a);
}});

const timelineFrame = document.querySelector('.timeline-frame');
if (timelineFrame && window.location.protocol === 'file:') {{
    timelineFrame.src = 'timeline.html?embed=1';
}}
const factoryFrame = document.querySelector('.factory-frame');
if (factoryFrame && window.location.protocol === 'file:') {{
    factoryFrame.src = 'http://127.0.0.1:8765/agent-factory?embed=1';
}}

// Search
const searchInput = document.getElementById('search-input');
const searchResults = document.getElementById('search-results');
function search(q) {{
    if (!q || q.length < 2) {{
        searchResults.innerHTML = '';
        return;
    }}
    const ql = q.toLowerCase();
    const matched = SAMPLES.filter(s => s.content.toLowerCase().includes(ql)).slice(0, 50);
    if (matched.length === 0) {{
        searchResults.innerHTML = '<div style="color:var(--text2);font-size:13px;padding:20px;text-align:center">采样数据中无匹配，请用命令行搜索全量。</div>';
        return;
    }}
    searchResults.innerHTML = matched.map(s => {{
        const reg = new RegExp(`(${{q.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&')}})`, 'gi');
        const highlighted = s.content.replace(/[<>&]/g, c => ({{'<':'&lt;','>':'&gt;','&':'&amp;'}})[c]).replace(reg, '<mark>$1</mark>');
        return `<div class="search-result"><div class="meta">${{s.date}} · ${{s.source}}</div><div class="text">${{highlighted}}</div></div>`;
    }}).join('');
}}
searchInput.oninput = e => search(e.target.value);
</script>

</body>
</html>"""

    OUTPUT_FILE.write_text(html_content, encoding="utf-8")
    return html_content


if __name__ == "__main__":
    html_content = generate_html()
    print(f"看板已生成: {OUTPUT_FILE}")
    print(f"文件大小: {len(html_content):,} 字符 ({len(html_content)/1024:.1f} KB)")

    import sys
    if "--open" in sys.argv:
        import webbrowser
        webbrowser.open(f"file://{OUTPUT_FILE}")
        print("已在浏览器中打开")
