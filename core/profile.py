#!/usr/bin/env python3
"""
Build a source-backed user profile for the Immortal Skill.

This is the compact personal information layer used before deeper recall.
It does not replace raw records. It gives Codex a stable map of identity,
accounts, company context, preferences, principles, projects, and gaps.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


SKILL_DIR = Path(__file__).resolve().parent
MEMORY_DIR = SKILL_DIR / "references" / "memory"
IMMORTAL_DIR = Path.home() / ".immortal"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
DAILY_DIR = IMMORTAL_DIR / "daily"
PROFILE_JSON = IMMORTAL_DIR / "profile.json"
PROFILE_MD = IMMORTAL_DIR / "profile.md"
PROFILE_COMPACT_MD = IMMORTAL_DIR / "profile_compact.md"
REVIEWED_PROFILE_JSONL = IMMORTAL_DIR / "reviewed" / "profile_memories.jsonl"


SECTION_SOURCES = [
    {
        "id": "identity",
        "title": "身份与定位",
        "purpose": "确认 Configured User 是谁，以及所有任务默认应站在哪个视角说话。",
        "files": ["user_identity.md", "user_account_names.md"],
    },
    {
        "id": "company_business",
        "title": "公司与商业上下文",
        "purpose": "给客户、产品、团队建议时优先使用这层业务现实。",
        "files": ["project_company_business.md", "project_current_priorities.md", "reference_fde_mode_cresta.md"],
    },
    {
        "id": "content_strategy",
        "title": "内容与账号策略",
        "purpose": "写作、选题、分发、口播、封面和账号边界的默认依据。",
        "files": [
            "project_content_strategy.md",
            "user_writing_preferences.md",
            "feedback_writing_methodology.md",
            "feedback_article_length.md",
            "feedback_bold_formatting.md",
            "feedback_punctuation_usage.md",
        ],
    },
    {
        "id": "communication",
        "title": "协作与沟通偏好",
        "purpose": "Codex 和 Configured User 对话时必须遵守的工作方式。",
        "files": [
            "feedback_communication_style.md",
            "feedback_autonomous_agent.md",
            "feedback_output_directory.md",
        ],
    },
    {
        "id": "technical_capability",
        "title": "技术能力与项目素材",
        "purpose": "判断 Configured User 能做什么、做过什么、哪些经历可以脱敏复用。",
        "files": [
            "user_technical_skills.md",
            "project_machine0_editor.md",
            "project_searchsvc_mcp.md",
            "feedback_frontend_design_skill.md",
            "feedback_avenir_web.md",
        ],
    },
    {
        "id": "decision_principles",
        "title": "判断原则与认知框架",
        "purpose": "让 Agent 按 Configured User 的方式判断，而不是回到通用答案。",
        "files": [
            "feedback_context_infrastructure.md",
            "feedback_context_engineering.md",
            "feedback_team_context_infrastructure.md",
            "feedback_ai_review_independent_judgment.md",
            "feedback_product_design_blindspot.md",
            "feedback_liurun_writing_system.md",
        ],
    },
    {
        "id": "team_hiring",
        "title": "团队与招聘",
        "purpose": "招助理、评估候选人、搭团队时使用。",
        "files": ["feedback_hiring_signals_2026.md"],
    },
    {
        "id": "risk_compliance",
        "title": "合规与红线",
        "purpose": "客户方案、数据接入和平台自动化时先检查风险。",
        "files": ["project_wechat_ecosystem_compliance.md"],
    },
    {
        "id": "current_projects",
        "title": "当前项目与外部事件",
        "purpose": "保留近期需要跟进的项目、域名、成本和供应商变化。",
        "files": ["project_immortal_origin.md", "project_sitec_domain_sunset.md", "project_anthropic_rate_limit_may_2026.md"],
        "pinned_items": [
            "永生 Skill 的第一原点是防删库和防数据丢失：沉淀语料、文件、文档、AI 对话和用户输出，先保全再提炼。",
            "防丢失是第一性原理，有用性是第二层目标，数字分身是第三层目标。",
            "Codex 版入口是 `~/.codex/skills/immortal`，活库是 `~/.immortal/`，默认上下文优先用 `~/.immortal/profile_compact.md`。",
            "当前方向是先把用户画像、项目关系、客户关系、人物关系和判断原则提炼清楚，再做秘书、顾问、史官、影子工作流。",
        ],
    },
]


RAW_PROFILE_TERMS = [
    "完整画像",
    "Configured User认知体系",
    "Configured User（用户本人）完整档案",
    "Configured User（用户本人）超级完整档案",
    "你要记住，我是自媒体作者，也是一家公司的CTO",
]

RECENT_PROFILE_TERMS = [
    "永生",
    "赛博永生",
    "记忆库",
    "digital soul",
    "主账号",
    "主账号",
    "主账号实验室",
    "账号边界A",
    "项目A",
    "协作账号",
    "Claude Code",
    "Codex",
    "OpenClaw",
    "Hermes",
    "飞书",
    "日历",
    "妙记",
    "文档",
    "IM",
    "客户",
    "删库",
    "数据丢失",
    "账号",
    "人设",
]

RECENT_FACT_RULES = [
    {
        "id": "loss_prevention_origin",
        "text": "永生 Skill 的第一原点是防止删库和数据丢失：沉淀用户的语料、文件、文档、AI 对话和个人输出，先保全再提炼。",
        "terms": ["删库", "数据丢失", "语料", "文件", "文档", "AI 的对话"],
    },
    {
        "id": "immortal_scope",
        "text": "永生记忆库必须覆盖所有本地 Agent 轨迹，不只备份对话：Claude Code、Codex、OpenClaw、Hermes、记忆文档、Skill、输入文件、图片、粘贴缓存和桌面产出都属于采集范围。",
        "terms": ["所有 Agent", "Claude", "Codex", "OpenClaw", "Hermes", "产出的文档", "输入的文档", "输入的图片"],
    },
    {
        "id": "codex_migration",
        "text": "当前项目已从 Claude Code 迁移到 Codex 环境，Codex 版 skill 应以 ~/.codex/skills/immortal 为入口，~/.immortal/ 为活库。",
        "terms": ["迁移", "Codex", "Claude Code"],
    },
    {
        "id": "profile_priority",
        "text": "用户当前要求优先把自己的信息完整提炼出来：身份、账号、公司、项目、客户、关系、偏好、判断原则和缺失项都要进入画像层。",
        "terms": ["自己的信息", "全部提炼", "缺失", "获取"],
    },
    {
        "id": "persona_boundary",
        "text": "账号边界是硬规则：主账号、账号边界A、外部参考账号必须分开。外部参考账号只能借方法，不能替换作者身份；账号边界A人设不能套到主账号。",
        "terms": ["主账号", "账号边界A", "外部参考账号", "人设", "另一个账号"],
    },
    {
        "id": "direct_correction",
        "text": "协作硬规则：当发现用户对事实、判断或方向的看法是错的，要立刻明确纠正，并说明错在哪里、为什么错、怎么调整。",
        "terms": ["立刻纠正", "错误", "看法"],
    },
    {
        "id": "feishu_bot_signal",
        "text": "飞书已成为后续数据入口和交互入口候选：用户希望通过飞书机器人窗口与本地 Agent 对话，并长期接入本地模型能力。",
        "terms": ["飞书", "机器人", "本地模型", "永久接入"],
    },
]

RECENT_SCAN_DAYS = 7
COMPACT_NOISE_ITEMS = {
    "双重角色",
    "内容定位",
    "公司信息",
    "业务方向",
    "当前优先事项",
    "当前状态",
    "文字输出规则",
    "微信公众号排版规则",
    "HTML输出规则",
    "短视频口播风格",
    "核心问题",
    "自我修正",
    "协作账号的产品设计方法论",
    "AI成本探索原则",
    "语言风格",
    "Agent交互原则",
    "非编程任务的思考框架",
    "与Configured User现有系统的整合",
    "核心论点",
    "三要素系统",
    "三层架构",
    "关键原则",
    "关键区别",
    "核心Axiom群",
    "立即可用的改进",
    "可复用的文件模板",
    "关键金句",
    "核心矛盾",
    "四个部件",
    "与个人版Context Infrastructure的关系",
    "AI 对话",
    "语料",
    "文件",
    "文档",
    "输入内容",
    "输出内容",
    "生成产物",
    "记忆文件",
    "Skill 资源",
    "关键决策和纠错记录",
}

COMPACT_NOISE_PREFIXES = (
    "Why:",
    "Why：",
    "How to apply:",
    "How to apply：",
)


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


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def read_memory_file(name: str) -> str:
    path = MEMORY_DIR / name
    if not path.exists():
        return ""
    return strip_frontmatter(redact(path.read_text(encoding="utf-8", errors="ignore")))


def important_lines(text: str, limit: int = 18) -> list[str]:
    lines: list[str] = []
    in_code = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line:
            continue
        if line.startswith(("---", ">", "|")):
            continue
        if line.startswith(("#", "##", "###")):
            continue
        if re.fullmatch(r"\*\*[^*]+?\*\*", line):
            value = line.strip("*")
        elif line.startswith(("-", "*")):
            value = line[1:].strip()
        elif re.match(r"^\d+\.", line):
            value = re.sub(r"^\d+\.\s*", "", line)
        elif line.startswith("**") and line.endswith("**"):
            value = line.strip("*")
        else:
            continue
        value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
        value = value.replace("**", "")
        value = value.strip("*").strip()
        value = re.sub(r"\s+", " ", value).strip()
        if 4 <= len(value) <= 220 and value not in lines:
            lines.append(value)
        if len(lines) >= limit:
            break
    return lines


def file_meta(name: str) -> dict:
    path = MEMORY_DIR / name
    if not path.exists():
        return {"file": name, "exists": False}
    return {
        "file": name,
        "exists": True,
        "bytes": path.stat().st_size,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def is_compact_noise(value: str) -> bool:
    if not value:
        return True
    stripped = value.strip()
    if stripped in COMPACT_NOISE_ITEMS:
        return True
    for prefix in COMPACT_NOISE_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


def is_structural_label(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    if stripped in COMPACT_NOISE_ITEMS:
        return True
    if len(stripped) <= 6 and re.fullmatch(r"[\u4e00-\u9fff]+", stripped):
        if not stripped.startswith(("不", "要", "避", "优", "保", "追", "做", "找", "看", "记", "写", "发", "提", "用", "给", "让", "先", "别", "再", "改", "开", "查", "回")):
            return True
    return False


def scan_raw_profile_evidence(limit: int = 12) -> list[dict]:
    if not INDEX_FILE.exists():
        return []
    evidence = []
    seen = set()
    with open(INDEX_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = record.get("content", "")
            if not content:
                continue
            if not any(term in content for term in RAW_PROFILE_TERMS):
                continue
            key = (record.get("source"), record.get("timestamp"), content[:120])
            if key in seen:
                continue
            seen.add(key)
            evidence.append({
                "timestamp": record.get("timestamp", ""),
                "source": record.get("source", ""),
                "role": record.get("role", ""),
                "preview": redact(re.sub(r"\s+", " ", content).strip()[:360]),
            })
            if len(evidence) >= limit:
                break
    return evidence


def scan_recent_profile_evidence(days: int = RECENT_SCAN_DAYS, limit: int = 18) -> list[dict]:
    if not DAILY_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    evidence = []
    seen = set()
    files = []
    for path in DAILY_DIR.glob("*.jsonl*"):
        date_part = path.name.split(".jsonl")[0]
        try:
            day = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            files.append((day, path))
    for _, path in sorted(files, key=lambda item: item[0], reverse=True):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = record.get("content", "")
                if not content:
                    continue
                if record.get("role") not in {"user", "assistant"}:
                    continue
                if not any(term in content for term in RECENT_PROFILE_TERMS):
                    continue
                key = (record.get("source"), record.get("timestamp"), content[:120])
                if key in seen:
                    continue
                seen.add(key)
                evidence.append({
                    "timestamp": record.get("timestamp", ""),
                    "source": record.get("source", ""),
                    "role": record.get("role", ""),
                    "preview": redact(re.sub(r"\s+", " ", content).strip()[:360]),
                })
    evidence.sort(key=lambda item: parse_timestamp(item.get("timestamp", "")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return evidence[:limit]


def scan_recent_profile_facts(days: int = RECENT_SCAN_DAYS) -> list[dict]:
    if not DAILY_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    matched: dict[str, dict] = {}
    files = []
    for path in DAILY_DIR.glob("*.jsonl*"):
        date_part = path.name.split(".jsonl")[0]
        try:
            day = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            files.append((day, path))
    for _, path in sorted(files, key=lambda item: item[0], reverse=True):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("role") != "user":
                    continue
                content = record.get("content", "")
                if not content:
                    continue
                for rule in RECENT_FACT_RULES:
                    if rule["id"] in matched:
                        continue
                    if all(term in content for term in rule["terms"]):
                        matched[rule["id"]] = {
                            "id": rule["id"],
                            "fact": rule["text"],
                            "timestamp": record.get("timestamp", ""),
                            "source": record.get("source", ""),
                        }
    facts = list(matched.values())
    facts.sort(key=lambda item: parse_timestamp(item.get("timestamp", "")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return facts


def load_reviewed_profile_memories() -> list[dict]:
    rows = []
    if not REVIEWED_PROFILE_JSONL.exists():
        return rows
    with REVIEWED_PROFILE_JSONL.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            statement = str(row.get("statement") or "").strip()
            memory_id = str(row.get("memory_id") or "").strip()
            if not statement or not memory_id:
                continue
            rows.append(row)
    rows.sort(
        key=lambda item: (
            {"self_profile": 0, "current_project": 1, "company_context": 2}.get(item.get("focus"), 9),
            {"preference": 0, "decision": 1, "lesson": 2, "relationship": 3, "project_fact": 4, "commitment": 5}.get(
                item.get("memory_type"), 9
            ),
            item.get("valid_from") or "",
            item.get("statement") or "",
        )
    )
    return rows


def compact_item_ok(item: str) -> bool:
    if is_compact_noise(item):
        return False
    if item.startswith(("Why:", "Why：", "How to apply:", "How to apply：")):
        return False
    return True


def build_profile() -> dict:
    sections = []
    missing_files = []
    for spec in SECTION_SOURCES:
        sources = []
        items = []
        pinned = spec.get("pinned_items") or []
        if pinned:
            items.append({
                "source": "_pinned",
                "items": pinned,
            })
        for name in spec["files"]:
            meta = file_meta(name)
            sources.append(meta)
            if not meta["exists"]:
                missing_files.append(name)
                continue
            body = read_memory_file(name)
            extracted = important_lines(body)
            if extracted:
                items.append({
                    "source": name,
                    "items": extracted,
                })
        sections.append({
            "id": spec["id"],
            "title": spec["title"],
            "purpose": spec["purpose"],
            "sources": sources,
            "items": items,
        })

    raw_evidence = scan_raw_profile_evidence()
    recent_evidence = scan_recent_profile_evidence()
    recent_facts = scan_recent_profile_facts()
    reviewed_profile_memories = load_reviewed_profile_memories()
    gaps = [
        "飞书数据已接入自动审阅链路，长期画像只吸收通过过滤并进入 reviewed 层的条目",
        "当前优先事项仍混有 2026-03-31 的旧状态，需要用最近 7 天记录和飞书继续刷新",
        "人物关系、客户关系、项目关系还没有结构化成图谱",
        "digital-soul.md 仍偏资料拼接，后续需要 LLM 做事实、原则、演化三层蒸馏",
        "已有本机定时采集和迁移包，但还需要自动版本快照和离机备份来真正防删库",
    ]
    return {
        "version": "0.8-profile",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "authority_order": [
            "references/memory/*.md",
            "~/.immortal/index.jsonl raw evidence",
            "~/.immortal/digital-soul.md",
            "handover references",
        ],
        "sections": sections,
        "raw_profile_evidence": raw_evidence,
        "recent_profile_evidence": recent_evidence,
        "recent_profile_facts": recent_facts,
        "reviewed_profile_memories": reviewed_profile_memories,
        "known_gaps": gaps,
        "missing_memory_files": missing_files,
    }


def write_outputs(profile: dict) -> None:
    IMMORTAL_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_JSON.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    PROFILE_MD.write_text(render_markdown(profile), encoding="utf-8")
    PROFILE_COMPACT_MD.write_text(render_compact_markdown(profile), encoding="utf-8")


def render_markdown(profile: dict) -> str:
    lines = [
        "# Configured User Profile",
        "",
        f"Generated: {profile['generated_at']}",
        f"Version: {profile['version']}",
        "",
        "This file is the compact personal information layer for Immortal Skill.",
        "Use it before full recall. Use raw records when evidence is needed.",
        "",
        "## Authority Order",
        "",
    ]
    for item in profile["authority_order"]:
        lines.append(f"- {item}")
    lines.append("")

    for section in profile["sections"]:
        lines.append(f"## {section['title']}")
        lines.append("")
        lines.append(section["purpose"])
        lines.append("")
        for source_group in section["items"]:
            lines.append(f"### {source_group['source']}")
            lines.append("")
            for item in source_group["items"]:
                lines.append(f"- {item}")
            lines.append("")

    lines.append("## Raw Profile Evidence")
    lines.append("")
    if profile["raw_profile_evidence"]:
        for record in profile["raw_profile_evidence"]:
            ts = record.get("timestamp", "")[:19].replace("T", " ")
            source = record.get("source", "")
            role = record.get("role", "")
            preview = record.get("preview", "")
            lines.append(f"- {ts} [{source}/{role}] {preview}")
    else:
        lines.append("- No raw profile evidence found in index scan.")
    lines.append("")

    lines.append("## 自动长期画像记忆")
    lines.append("")
    if profile.get("reviewed_profile_memories"):
        for item in profile["reviewed_profile_memories"]:
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            title = source.get("title") or "unknown source"
            valid_from = item.get("valid_from") or ""
            approved_at = item.get("approved_at") or ""
            lines.append(f"- `{item.get('memory_id')}` {item.get('statement')}")
            lines.append(
                f"  - type: {item.get('memory_type')} / focus: {item.get('focus')} / source: {title} / valid_from: {valid_from} / approved: {approved_at}"
            )
    else:
        lines.append("- 暂无自动长期画像记忆。运行 profile-auto-review after feishu-distill。")
    lines.append("")

    lines.append("## Recent Facts")
    lines.append("")
    if profile.get("recent_profile_facts"):
        for item in profile["recent_profile_facts"]:
            ts = item.get("timestamp", "")[:19].replace("T", " ")
            source = item.get("source", "")
            lines.append(f"- {item['fact']} ({ts}, {source})")
    else:
        lines.append("- No recent profile facts extracted.")
    lines.append("")

    lines.append("## Recent Signals")
    lines.append("")
    if profile.get("recent_profile_evidence"):
        for record in profile["recent_profile_evidence"]:
            ts = record.get("timestamp", "")[:19].replace("T", " ")
            source = record.get("source", "")
            role = record.get("role", "")
            preview = record.get("preview", "")
            lines.append(f"- {ts} [{source}/{role}] {preview}")
    else:
        lines.append("- No recent signal evidence found in daily scan.")
    lines.append("")

    lines.append("## Known Gaps")
    lines.append("")
    for gap in profile["known_gaps"]:
        lines.append(f"- {gap}")
    lines.append("")
    return "\n".join(lines)


def render_compact_markdown(profile: dict) -> str:
    max_items = {
        "identity": 14,
        "company_business": 16,
        "content_strategy": 18,
        "communication": 16,
        "technical_capability": 16,
        "decision_principles": 18,
        "team_hiring": 10,
        "risk_compliance": 10,
        "current_projects": 10,
    }
    lines = [
        "# Configured User Profile Compact",
        "",
        f"Generated: {profile['generated_at']}",
        "",
        "Use this as the default Codex personal context. Use profile.md or raw recall when more evidence is needed.",
        "",
    ]
    for section in profile["sections"]:
        lines.append(f"## {section['title']}")
        lines.append("")
        budget = max_items.get(section["id"], 12)
        count = 0
        seen = set()
        for group in section["items"]:
            for item in group["items"]:
                if item in seen:
                    continue
                seen.add(item)
                if not compact_item_ok(item):
                    continue
                lines.append(f"- {item}")
                count += 1
                if count >= budget:
                    break
            if count >= budget:
                break
        if count == 0:
            lines.append("- No compact items extracted.")
        lines.append("")
    reviewed = profile.get("reviewed_profile_memories") or []
    lines.append("## 自动长期画像记忆")
    lines.append("")
    if reviewed:
        for item in reviewed[:36]:
            statement = str(item.get("statement") or "").strip()
            if statement:
                lines.append(f"- {statement}")
    else:
        lines.append("- 暂无自动长期画像记忆。")
    lines.append("")
    lines.append("## Recent Signals")
    lines.append("")
    recent_facts = profile.get("recent_profile_facts") or []
    if recent_facts:
        for item in recent_facts[:10]:
            lines.append(f"- {item['fact']}")
    else:
        lines.append("- No recent profile facts extracted.")
    lines.append("")
    lines.append("## Known Gaps")
    lines.append("")
    for gap in profile["known_gaps"]:
        lines.append(f"- {gap}")
    lines.append("")
    return "\n".join(lines)


def summarize(profile: dict) -> str:
    section_count = len(profile["sections"])
    source_count = 0
    item_count = 0
    for section in profile["sections"]:
        source_count += sum(1 for source in section["sources"] if source.get("exists"))
        item_count += sum(len(group["items"]) for group in section["items"])
    return (
        f"Profile built: {section_count} sections, "
        f"{source_count} source files, {item_count} extracted items, "
        f"{len(profile['raw_profile_evidence'])} raw evidence records, "
        f"{len(profile.get('recent_profile_evidence') or [])} recent signal records, "
        f"{len(profile.get('recent_profile_facts') or [])} recent facts, "
        f"{len(profile.get('reviewed_profile_memories') or [])} reviewed memories."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build user profile from Immortal memory sources")
    parser.add_argument("--show", action="store_true", help="Print generated markdown after writing")
    parser.add_argument("--json", action="store_true", help="Print generated JSON after writing")
    args = parser.parse_args()

    profile = build_profile()
    write_outputs(profile)
    print(summarize(profile))
    print(f"Wrote: {PROFILE_JSON}")
    print(f"Wrote: {PROFILE_MD}")
    print(f"Wrote: {PROFILE_COMPACT_MD}")
    if args.json:
        print(json.dumps(profile, ensure_ascii=False, indent=2))
    if args.show:
        print(PROFILE_MD.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
