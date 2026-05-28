#!/usr/bin/env python3
"""Build scenario-specific digital role packages from the Immortal vault.

This is the executable bridge between preservation and usable agents:
capture -> clean -> long-term profile -> scenario role -> installable Skill.

The script intentionally does not edit digital-soul.md. It creates an
auditable derived package under ~/.immortal/roles/ and can optionally install a
compact Codex Skill under ~/.codex/skills/.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import load_config, owner_aliases, owner_display_name, slug_prefix


HOME = Path.home()
SKILL_DIR = Path(__file__).resolve().parent
CODEX_SKILLS_DIR = HOME / ".codex" / "skills"
IMMORTAL_DIR = HOME / ".immortal"
PROFILE_JSON = IMMORTAL_DIR / "profile.json"
PROFILE_MD = IMMORTAL_DIR / "profile.md"
PROFILE_NUWA_JSON = IMMORTAL_DIR / "profile_nuwa.json"
PROFILE_NUWA_MD = IMMORTAL_DIR / "profile_nuwa.md"
REVIEWED_PROFILE_JSONL = IMMORTAL_DIR / "reviewed" / "profile_memories.jsonl"
PEOPLE_INDEX_JSON = IMMORTAL_DIR / "people" / "people_index.json"
QUALITY_JSON = IMMORTAL_DIR / "quality" / "latest.json"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
DAILY_DIR = IMMORTAL_DIR / "daily"
ROLES_DIR = IMMORTAL_DIR / "roles"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


MODE_SPECS: dict[str, dict[str, Any]] = {
    "advisor": {
        "label": "决策顾问",
        "purpose": "在信息不完整时，先替用户梳理判断框架、关键风险、行动选项和需要补证据的地方。",
        "keywords": ["决策", "判断", "取舍", "方向", "风险", "下一步", "选择", "方案", "顾问", "建议"],
        "scenarios": ["方向判断", "项目取舍", "合作评估", "风险预判", "下一步行动拆解"],
        "must_do": [
            "先给结论，再给依据和下一步。",
            "把证据、推断和不确定性分开。",
            "出现用户方向明显错误时直接纠正。",
            "优先调用长期画像中的决策原则，而不是给通用建议。",
        ],
        "must_not": [
            "不要把 AI 输出当成最终判断。",
            "不要为了全面而输出大而空的咨询报告。",
            "不要暴露原始私密语料，只摘要证据。",
        ],
        "protocol": [
            "确认当前问题属于战略、执行、风险还是沟通。",
            "从画像中抽取相关心智模型和历史偏好。",
            "列出 1 个主建议、2-3 个备选路径和关键反证。",
            "给出可执行的下一步，必要时标明需要补充的一手证据。",
        ],
    },
    "writer": {
        "label": "写稿审稿 Agent",
        "purpose": "把用户历史判断、表达偏好、账号边界和审稿标准转成可执行的写稿/审稿协作流程。",
        "keywords": [
            "写稿",
            "稿件",
            "审稿",
            "文章",
            "公众号",
            "标题",
            "选题",
            "内容",
            "排版",
            "主账号",
            "商业杂文",
            "小红书",
            "口播",
            "创作",
            "表达",
            "事实核查",
        ],
        "scenarios": ["选题切口", "文章初稿", "标题重做", "审稿挑错", "事实校准", "账号风格统一"],
        "must_do": [
            "先确认使用哪个账号视角，默认优先主账号，避免人设串台。",
            "先抓普通读者利益点和商业判断，再进入文字润色。",
            "审稿时优先指出事实风险、结构问题、缺少高潮、账号风格偏差。",
            "写作时输出可直接使用的正文，而不是只给大纲。",
            "把用户已验证的排版偏好应用到结果：短段落、直接小标题、适度加粗判断。",
        ],
        "must_not": [
            "不要混用外部参考账号、账号边界A、协作账号和主账号 的身份。",
            "不要生成通用 AI 腔行业分析。",
            "不要把未经核实的事实写成确定结论。",
            "不要让用户反复做清洗式审阅；常规判断由 Agent 自动完成。",
        ],
        "protocol": [
            "识别任务：选题、初稿、改稿、审稿、标题或发布前检查。",
            "调用长期画像中的表达 DNA、账号边界、内容方法论和历史审稿原则。",
            "先输出判断：这篇内容要抓住谁、靠什么冲突成立、哪里可能翻车。",
            "再输出正文或审稿清单；审稿清单按严重程度排序。",
            "最后给出可沉淀的标准：本次哪些判断可进入长期写作规则。",
        ],
    },
    "reviewer": {
        "label": "复核审阅 Agent",
        "purpose": "对方案、文章、交付物和自动蒸馏结果做自动审阅，先替用户筛掉明显问题。",
        "keywords": ["审阅", "复核", "检查", "问题", "风险", "质量", "错误", "清洗", "自动审阅", "标准"],
        "scenarios": ["文稿审阅", "记忆候选审核", "方案风险检查", "交付前质量门禁"],
        "must_do": [
            "发现错误方向时直接说错在哪里。",
            "优先给高风险问题，而不是平均用力。",
            "能自动判断的内容自动判断，只把证据不足的例外标出。",
            "每条问题要能对应到证据、规则或明确标准。",
        ],
        "must_not": [
            "不要把所有内容都丢回给用户确认。",
            "不要只给主观评价，必须给可执行修正。",
            "不要把低置信度内容合并进长期画像。",
        ],
        "protocol": [
            "建立审阅标准：身份、事实、结构、表达、风险、可执行性。",
            "按严重度输出问题：P0/P1/P2。",
            "给出自动修正建议和不能自动修正的原因。",
            "将稳定规则沉淀为可复用检查项。",
        ],
    },
    "business": {
        "label": "商业判断 Agent",
        "purpose": "基于用户业务现实和历史判断，辅助客户、产品、报价、团队和交付策略判断。",
        "keywords": ["商业", "客户", "报价", "成交", "合作", "产品", "MVP", "交付", "团队", "项目A", "飞书", "代理"],
        "scenarios": ["客户要不要接", "方案怎么报", "MVP 怎么落", "团队怎么分工", "业务风险"],
        "must_do": [
            "优先使用用户真实业务阶段：早期、精简团队、先找付费客户。",
            "报价讨论先讲总价值，不把自己放到工时劳务位置。",
            "把钩子产品、利润中心和交付能力分开判断。",
            "涉及敏感客户数据时先脱敏和分层。",
        ],
        "must_not": [
            "不要把市场验证不足的方向包装成确定战略。",
            "不要忽略销售、交付和组织能力约束。",
            "不要把灰色或高风险内容写进公开输出。",
        ],
        "protocol": [
            "判断问题属于客户、产品、报价、交付还是团队。",
            "对照用户历史商业原则给出主判断。",
            "列出收益、成本、风险、验证动作。",
            "输出一个最小可执行动作，而不是长期蓝图。",
        ],
    },
    "project": {
        "label": "项目推进 Agent",
        "purpose": "把长期项目上下文转成任务拆解、进度判断、风险提醒和可交接执行包。",
        "keywords": ["项目", "推进", "路线图", "待办", "进度", "实现", "部署", "开发", "架构", "交接"],
        "scenarios": ["项目复盘", "下一步计划", "开发拆解", "交接文档", "风险跟踪"],
        "must_do": [
            "先确认当前项目阶段和已完成事实。",
            "区分阻塞任务、并行任务和后续优化。",
            "保留可验证命令、路径和产物。",
            "保持 Skill first，不把看板当产品本体。",
        ],
        "must_not": [
            "不要在没读现状前重开一套架构。",
            "不要混淆用户未拍板和已经确认的路线。",
            "不要覆盖用户或其他 agent 的未确认改动。",
        ],
        "protocol": [
            "读取当前项目的健康状态、最近产物和路线图。",
            "给出当前关卡、下一步任务和验收标准。",
            "能执行就直接执行，不能执行才说明阻塞。",
            "输出可交接的路径、命令和结果。",
        ],
    },
    "shadow": {
        "label": "影子分身 Agent",
        "purpose": "在用户不想反复审阅时，按长期画像自动做初筛、初判和上下文整理。",
        "keywords": ["分身", "影子", "自动", "全自动", "替我", "我的视角", "不用审阅", "长期画像", "人格"],
        "scenarios": ["自动初筛", "自动清洗", "长期画像更新建议", "按用户视角做预判"],
        "must_do": [
            "默认站在用户本人本人视角，不把其他人的话误当成用户。",
            "自动处理常规低风险判断，并留下审计证据。",
            "高风险、低证据、涉及外部承诺的事项要标明边界。",
            "把结果写成可用于后续任务的上下文，而不是聊天感想。",
        ],
        "must_not": [
            "不要冒充用户做外部承诺。",
            "不要把同事或合作方的观点混进用户长期画像。",
            "不要直接覆盖 digital-soul.md。",
        ],
        "protocol": [
            "识别资料是否来自用户本人、他人评价还是会议纪要。",
            "用身份规则和证据门槛做自动筛选。",
            "输出可采纳结论、待观察结论和拒绝原因。",
            "把高置信结论留作后续画像候选。",
        ],
    },
    "custom": {
        "label": "自定义场景 Agent",
        "purpose": "围绕用户给定目标，从长期记忆中抽取可用原则、证据、表达方式和边界。",
        "keywords": ["目标", "场景", "需求", "能力", "agent", "skill", "蒸馏", "知识库", "角色"],
        "scenarios": ["自定义目标", "同事试用", "知识库问答", "个人方法论复用"],
        "must_do": [
            "紧扣用户给定目标，不做泛化画像展示。",
            "区分稳定原则、场景策略和证据不足的猜测。",
            "输出可运行协议和质量门槛。",
        ],
        "must_not": [
            "不要把全部画像原样塞进角色。",
            "不要暴露私密原文。",
            "不要承诺完全替代本人。",
        ],
        "protocol": [
            "从目标提取关键词和能力边界。",
            "检索画像、reviewed 记忆和近期证据。",
            "生成场景协议、输出规范、反模式和质量门槛。",
        ],
    },
}


ARCHITECTURE = [
    {
        "layer": "L0 Capture / Preserve",
        "description": "本地 Codex/Claude/飞书/文件语料进入 ~/.immortal/daily 与 index.jsonl，先保证可恢复。",
        "artifacts": ["~/.immortal/daily/", "~/.immortal/index.jsonl", "~/.immortal/exports/"],
    },
    {
        "layer": "L1 Clean / Distill",
        "description": "按来源清洗、去噪、结构化，形成候选记忆和可审计证据。",
        "artifacts": ["feishu_clean.py", "feishu_distill.py", "profile_auto_review.py"],
    },
    {
        "layer": "L2 Long-Term Profile",
        "description": "把高权威记忆合并成长期画像和 Nuwa 风格思维模型。",
        "artifacts": ["profile.json", "profile_nuwa.json", "reviewed/profile_memories.jsonl"],
    },
    {
        "layer": "L3 Scenario Role",
        "description": "按用户指定目标抽取场景能力、边界、协议和证据，生成数字角色包。",
        "artifacts": ["role_distill.py", "ROLE.md", "role.json", "evidence.jsonl"],
    },
    {
        "layer": "L4 Runtime Skill",
        "description": "把角色包压缩成可安装 Skill，让 Codex 在具体任务中调用记忆和协议。",
        "artifacts": ["SKILL.md", "~/.codex/skills/<role>/"],
    },
]


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return default


def load_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
                if limit and len(rows) >= limit:
                    break
    return rows


def redact(text: str) -> str:
    patterns = [
        (r"\bcli_[A-Za-z0-9_\-]{8,}\b", "cli_[REDACTED]"),
        (r"(?i)(app secret\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(api\s*key\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(apikey\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(password\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        (r"(?i)(密码\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        (r"sk-[A-Za-z0-9_\-]{12,}", "sk-[REDACTED]"),
        (r"(?i)(authorization\s*[:：]\s*bearer\s+)\S+", r"\1[REDACTED]"),
    ]
    value = str(text or "")
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def compact(text: Any, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = redact(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def normalize_mode(goal: str, mode: str) -> str:
    if mode != "auto":
        return mode
    checks = [
        ("writer", ["写稿", "稿件", "审稿", "文章", "标题", "选题", "公众号", "内容", "创作"]),
        ("reviewer", ["审阅", "复核", "检查", "质量", "错误", "风险"]),
        ("business", ["商业", "客户", "报价", "成交", "合作", "MVP", "交付", "代理"]),
        ("project", ["项目", "推进", "开发", "部署", "架构", "路线图", "待办"]),
        ("shadow", ["分身", "影子", "自动", "全自动", "替我", "不用审阅"]),
        ("advisor", ["决策", "判断", "方向", "选择", "建议", "顾问"]),
    ]
    for name, keywords in checks:
        if any(keyword in goal for keyword in keywords):
            return name
    return "custom"


def make_terms(goal: str, mode: str, target_name: str) -> list[str]:
    spec = MODE_SPECS.get(mode, MODE_SPECS["custom"])
    terms: list[str] = [goal, target_name]
    terms.extend(owner_aliases())
    terms.extend(spec.get("keywords") or [])
    terms.extend(spec.get("scenarios") or [])
    ascii_tokens = re.findall(r"[a-zA-Z0-9_+-]{2,}", goal)
    terms.extend(ascii_tokens)
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", goal)
    for chunk in chinese_chunks:
        terms.append(chunk)
        if len(chunk) <= 8:
            for size in (2, 3, 4):
                for i in range(max(0, len(chunk) - size + 1)):
                    terms.append(chunk[i : i + size])
    seen: set[str] = set()
    cleaned: list[str] = []
    for term in terms:
        value = re.sub(r"\s+", " ", str(term or "")).strip()
        if len(value) < 2:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    return cleaned[:120]


def score_text(text: str, terms: list[str]) -> float:
    if not text:
        return 0.0
    lower = text.lower()
    score = 0.0
    for term in terms:
        key = term.lower()
        if key in lower:
            score += min(4.0, max(1.0, len(term) / 2))
    return score


def source_title(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("title") or row.get("source") or row.get("project") or "unknown")


def source_ref(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("url") or source.get("raw_id") or row.get("id") or row.get("memory_id") or "")


def evidence_item(
    *,
    text: str,
    source: str,
    layer: str,
    kind: str,
    weight: float,
    ref: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "text": compact(text, 520),
        "source": compact(source, 120),
        "ref": compact(ref, 160),
        "layer": layer,
        "kind": kind,
        "weight": round(float(weight), 3),
        "meta": meta or {},
    }


def flatten_nuwa(nuwa: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in nuwa.get("mental_models") or []:
        if not isinstance(model, dict):
            continue
        title = str(model.get("title") or "")
        summary = str(model.get("summary") or "")
        text = f"{title}：{summary} 应用：{model.get('application') or ''} 边界：{model.get('limitation') or ''}"
        rows.append(
            evidence_item(
                text=text,
                source="profile_nuwa.mental_models",
                layer="long_profile",
                kind="mental_model",
                weight=9.0 if model.get("status") == "accepted" else 6.0,
                ref=str(model.get("id") or title),
                meta={
                    "title": title,
                    "status": model.get("status"),
                    "domains": model.get("domains") or [],
                    "evidence_count": model.get("evidence_count"),
                },
            )
        )
        for sample in model.get("evidence") or []:
            if not isinstance(sample, dict):
                continue
            rows.append(
                evidence_item(
                    text=str(sample.get("text") or ""),
                    source=str(sample.get("source") or title),
                    layer="nuwa_evidence",
                    kind=str(sample.get("kind") or "model_sample"),
                    weight=7.5,
                    ref=str(sample.get("memory_id") or ""),
                    meta={"model": title},
                )
            )
    for item in nuwa.get("decision_heuristics") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            evidence_item(
                text=f"{item.get('title') or ''}：{item.get('rule') or ''}",
                source="profile_nuwa.decision_heuristics",
                layer="long_profile",
                kind="decision_heuristic",
                weight=8.0 if item.get("confidence") == "high" else 6.5,
                ref=str(item.get("title") or ""),
                meta={"confidence": item.get("confidence"), "domains": item.get("domains") or []},
            )
        )
    for item in (nuwa.get("expression_dna") or [])[1:]:
        if not isinstance(item, dict):
            continue
        rows.append(
            evidence_item(
                text=f"{item.get('name') or ''}：{item.get('description') or ''}",
                source="profile_nuwa.expression_dna",
                layer="long_profile",
                kind="expression_dna",
                weight=7.0,
                ref=str(item.get("name") or ""),
                meta={"sources": item.get("sources") or []},
            )
        )
    for item in nuwa.get("anti_patterns") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            evidence_item(
                text=f"反模式：{item.get('title') or ''}",
                source="profile_nuwa.anti_patterns",
                layer="long_profile",
                kind="anti_pattern",
                weight=6.5,
                ref=str(item.get("title") or ""),
                meta={"evidence_count": item.get("evidence_count")},
            )
        )
    for boundary in nuwa.get("honest_boundaries") or []:
        rows.append(
            evidence_item(
                text=f"诚实边界：{boundary}",
                source="profile_nuwa.honest_boundaries",
                layer="long_profile",
                kind="boundary",
                weight=7.0,
            )
        )
    return rows


def flatten_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in profile.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id") or "section")
        title = str(section.get("title") or section_id)
        purpose = str(section.get("purpose") or "")
        if purpose:
            rows.append(
                evidence_item(
                    text=f"{title}：{purpose}",
                    source="profile.sections",
                    layer="reference_profile",
                    kind="section_purpose",
                    weight=5.0,
                    ref=section_id,
                )
            )
        for group in section.get("items") or []:
            if not isinstance(group, dict):
                continue
            source = str(group.get("source") or title)
            for item in group.get("items") or []:
                rows.append(
                    evidence_item(
                        text=str(item),
                        source=source,
                        layer="reference_profile",
                        kind=f"profile_{section_id}",
                        weight=6.0 if section_id in {"identity", "content_strategy", "decision_principles"} else 5.0,
                        ref=section_id,
                    )
                )
    for fact in profile.get("recent_profile_facts") or []:
        if isinstance(fact, dict):
            rows.append(
                evidence_item(
                    text=str(fact.get("fact") or ""),
                    source=str(fact.get("source") or "recent_profile_facts"),
                    layer="reference_profile",
                    kind="recent_profile_fact",
                    weight=6.0,
                    ref=str(fact.get("id") or fact.get("timestamp") or ""),
                )
            )
    return rows


def flatten_reviewed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("statement") or row.get("evidence") or "")
        if not text:
            continue
        sensitivity = str(row.get("sensitivity") or "")
        confidence = float(row.get("confidence") or 0.75)
        relevance = float(row.get("relevance_score") or 0.75)
        weight = 7.0 + confidence + relevance
        if sensitivity in {"public", "internal", ""}:
            weight += 0.4
        evidence.append(
            evidence_item(
                text=text,
                source=source_title(row),
                layer="reviewed_memory",
                kind=f"reviewed_{row.get('memory_type') or 'memory'}",
                weight=weight,
                ref=source_ref(row),
                meta={
                    "memory_id": row.get("memory_id"),
                    "focus": row.get("focus"),
                    "projects": row.get("projects") or [],
                    "people": row.get("people") or [],
                    "sensitivity": sensitivity,
                    "confidence": confidence,
                    "relevance_score": relevance,
                },
            )
        )
    return evidence


def flatten_people(people_index: dict[str, Any], target_name: str, goal: str) -> list[dict[str, Any]]:
    people = people_index.get("people") or []
    rows: list[dict[str, Any]] = []
    if not isinstance(people, list):
        return rows
    terms = [target_name, goal, *owner_aliases()]
    for person in people:
        if not isinstance(person, dict):
            continue
        haystack = json.dumps(person, ensure_ascii=False)
        if person.get("category") == "self" or score_text(haystack, terms) > 0:
            rows.append(
                evidence_item(
                    text=str(person.get("intro") or person.get("profile", {}).get("role_summary") or person.get("name") or ""),
                    source="people_index",
                    layer="people_index",
                    kind=f"person_{person.get('category') or 'unknown'}",
                    weight=5.5 if person.get("category") == "self" else 3.5,
                    ref=str(person.get("name") or ""),
                    meta={
                        "name": person.get("name"),
                        "aliases": person.get("aliases") or [],
                        "category": person.get("category"),
                        "memory_count": person.get("memory_count"),
                        "top_projects": person.get("top_projects") or [],
                    },
                )
            )
    return rows[:12]


def open_maybe_gzip(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return path.open("r", encoding="utf-8", errors="ignore")


def recent_daily_files(days: int) -> list[Path]:
    if not DAILY_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=max(1, days) - 1)
    files: list[Path] = []
    for path in DAILY_DIR.glob("*.jsonl*"):
        date_part = path.name.split(".jsonl")[0]
        try:
            day = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            files.append(path)
    return sorted(files, reverse=True)


def search_recent_raw(goal: str, terms: list[str], *, days: int, max_records: int, max_results: int) -> list[dict[str, Any]]:
    files = recent_daily_files(days)
    rows: list[tuple[float, dict[str, Any]]] = []
    scanned = 0
    for path in files:
        if scanned >= max_records:
            break
        try:
            handle = open_maybe_gzip(path)
        except OSError:
            continue
        with handle:
            for line in handle:
                if scanned >= max_records:
                    break
                scanned += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                content = str(record.get("content") or "")
                if len(content.strip()) < 12:
                    continue
                match = score_text(content, terms)
                if match <= 0:
                    continue
                role = str(record.get("role") or "")
                source = str(record.get("source") or "raw")
                weight = 2.0 + min(6.0, match / 3)
                if role == "user":
                    weight += 1.5
                if "feishu" in source:
                    weight += 0.5
                rows.append(
                    (
                        weight + match / 10,
                        evidence_item(
                            text=content,
                            source=source,
                            layer="raw_recent",
                            kind=f"raw_{role or record.get('type') or 'record'}",
                            weight=weight,
                            ref=str(record.get("id") or ""),
                            meta={
                                "timestamp": record.get("timestamp"),
                                "project": record.get("project"),
                                "session_id": record.get("session_id"),
                                "scanned_records": scanned,
                            },
                        ),
                    )
                )
    rows.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in rows[:max_results]]


def dedupe_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("text") or "")
        key = hashlib.sha1(re.sub(r"\s+", "", text).encode("utf-8", errors="ignore")).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def select_evidence(all_rows: list[dict[str, Any]], terms: list[str], *, limit: int) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in all_rows:
        text = str(row.get("text") or "")
        source = str(row.get("source") or "")
        meta = json.dumps(row.get("meta") or {}, ensure_ascii=False)
        match = score_text(" ".join([text, source, meta]), terms)
        base = float(row.get("weight") or 0)
        layer_bonus = {
            "reviewed_memory": 3.0,
            "long_profile": 2.5,
            "reference_profile": 2.0,
            "nuwa_evidence": 1.5,
            "people_index": 1.0,
            "raw_recent": 0.3,
        }.get(str(row.get("layer")), 0.0)
        score = base + layer_bonus + match
        if match > 0 or row.get("kind") in {"mental_model", "decision_heuristic", "expression_dna", "boundary"}:
            new_row = dict(row)
            new_row["match_score"] = round(match, 3)
            new_row["rank_score"] = round(score, 3)
            scored.append((score, new_row))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = dedupe_evidence([item for _, item in scored])

    # Preserve a useful skeleton even when a narrow goal has sparse matches.
    kinds = {row.get("kind") for row in selected[:limit]}
    backfill: list[dict[str, Any]] = []
    for required in ["mental_model", "decision_heuristic", "expression_dna", "boundary"]:
        if required in kinds:
            continue
        for row in all_rows:
            if row.get("kind") == required:
                backfill.append(row)
                break
    selected = dedupe_evidence(backfill + selected)
    return selected[:limit]


def slugify(goal: str, mode: str, target_name: str) -> str:
    prefix_name = slug_prefix()
    if mode in {"writer", "reviewer"} and any(term in goal for term in ["写稿", "稿件", "审稿", "文章", "内容"]):
        return f"{prefix_name}-writing-review-agent"
    if mode == "business":
        prefix = f"{prefix_name}-business-agent"
    elif mode == "project":
        prefix = f"{prefix_name}-project-agent"
    elif mode == "shadow":
        prefix = f"{prefix_name}-shadow-agent"
    elif mode == "advisor":
        prefix = f"{prefix_name}-advisor-agent"
    else:
        prefix = f"{prefix_name}-custom-agent"
    ascii_part = "-".join(re.findall(r"[a-zA-Z0-9]+", f"{target_name} {goal}".lower()))[:42].strip("-")
    digest = hashlib.sha1(f"{target_name}|{mode}|{goal}".encode("utf-8")).hexdigest()[:8]
    if ascii_part and ascii_part not in {"blake", "user"}:
        return f"{prefix}-{ascii_part}-{digest}"[:72].strip("-")
    return f"{prefix}-{digest}"


def quality_gate(package: dict[str, Any]) -> dict[str, Any]:
    sources = package.get("sources") or {}
    evidence = package.get("evidence") or []
    layers = Counter(str(row.get("layer") or "unknown") for row in evidence)
    role = package.get("role") or {}
    checks = [
        {
            "name": "profile_nuwa exists",
            "ok": bool(sources.get("profile_nuwa_json", {}).get("exists")),
            "detail": str(sources.get("profile_nuwa_json", {}).get("path")),
        },
        {
            "name": "reviewed memory available",
            "ok": int(sources.get("reviewed_profile_memories", {}).get("rows") or 0) >= 5,
            "detail": f"{sources.get('reviewed_profile_memories', {}).get('rows', 0)} rows",
        },
        {
            "name": "evidence coverage",
            "ok": len(evidence) >= 12 and len(layers) >= 3,
            "detail": f"{len(evidence)} snippets across {len(layers)} layers",
        },
        {
            "name": "operating protocol present",
            "ok": len(role.get("protocol") or []) >= 3,
            "detail": f"{len(role.get('protocol') or [])} steps",
        },
        {
            "name": "must-do and must-not present",
            "ok": len(role.get("must_do") or []) >= 3 and len(role.get("must_not") or []) >= 3,
            "detail": f"{len(role.get('must_do') or [])} do / {len(role.get('must_not') or [])} not",
        },
        {
            "name": "honest boundaries present",
            "ok": len(role.get("boundaries") or []) >= 4,
            "detail": f"{len(role.get('boundaries') or [])} boundaries",
        },
    ]
    return {
        "status": "ok" if all(item["ok"] for item in checks) else "attention",
        "checks": checks,
        "layer_counts": dict(layers),
    }


def source_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
    }


def build_role_package(args: argparse.Namespace) -> dict[str, Any]:
    goal = args.goal_text.strip()
    mode = normalize_mode(goal, args.mode)
    spec = MODE_SPECS.get(mode, MODE_SPECS["custom"])
    terms = make_terms(goal, mode, args.target_name)

    profile = read_json(PROFILE_JSON, {})
    nuwa = read_json(PROFILE_NUWA_JSON, {})
    quality = read_json(QUALITY_JSON, {})
    people = read_json(PEOPLE_INDEX_JSON, {})
    reviewed = load_jsonl(REVIEWED_PROFILE_JSONL)

    all_evidence: list[dict[str, Any]] = []
    all_evidence.extend(flatten_nuwa(nuwa))
    all_evidence.extend(flatten_profile(profile))
    all_evidence.extend(flatten_reviewed(reviewed))
    all_evidence.extend(flatten_people(people, args.target_name, goal))
    raw_rows: list[dict[str, Any]] = []
    if not args.no_raw_search:
        raw_rows = search_recent_raw(
            goal,
            terms,
            days=args.raw_days,
            max_records=args.max_raw_records,
            max_results=args.raw_results,
        )
        all_evidence.extend(raw_rows)

    selected = select_evidence(all_evidence, terms, limit=args.max_evidence)

    role_slug = args.slug or slugify(goal, mode, args.target_name)
    skill_name = f"immortal-{role_slug}".replace("--", "-")[:80].strip("-")
    boundaries = [
        "这是场景角色，不是完整复制用户本人。",
        "可自动做初筛、初判、初稿和审阅，但外部承诺、法律财务和高风险事项必须标出边界。",
        "输出时只摘要私密证据，不贴原始聊天、凭证、客户敏感信息。",
        "当证据不足时要降级为假设，不把推断写成事实。",
        "长期画像会随每日采集变化，本角色包记录的是生成时刻的快照。",
    ]
    if mode == "writer":
        boundaries.append("写作可自动到 60-70% 草稿/审稿质量，但高质量发布仍需要账号主人的最终判断。")

    package: dict[str, Any] = {
        "version": "0.1-role-distill",
        "generated_at": now_local(),
        "goal": goal,
        "target_name": args.target_name,
        "mode": mode,
        "mode_label": spec["label"],
        "slug": role_slug,
        "skill_name": skill_name,
        "architecture": ARCHITECTURE,
        "sources": {
            "profile_json": source_status(PROFILE_JSON),
            "profile_md": source_status(PROFILE_MD),
            "profile_nuwa_json": source_status(PROFILE_NUWA_JSON),
            "profile_nuwa_md": source_status(PROFILE_NUWA_MD),
            "reviewed_profile_memories": {
                **source_status(REVIEWED_PROFILE_JSONL),
                "rows": len(reviewed),
            },
            "people_index": source_status(PEOPLE_INDEX_JSON),
            "quality_json": {
                **source_status(QUALITY_JSON),
                "status": quality.get("status") if isinstance(quality, dict) else None,
                "score": quality.get("score") if isinstance(quality, dict) else None,
            },
            "raw_recent_search": {
                "enabled": not args.no_raw_search,
                "days": args.raw_days,
                "results": len(raw_rows),
                "max_records": args.max_raw_records,
            },
        },
        "role": {
            "purpose": spec["purpose"],
            "scenarios": spec["scenarios"],
            "protocol": spec["protocol"],
            "must_do": spec["must_do"],
            "must_not": spec["must_not"],
            "boundaries": boundaries,
            "learning_loop": [
                "每天自动采集新增语料，先进入可恢复仓库。",
                "清洗层把原始资料转成候选记忆，并通过自动审阅处理常规候选。",
                "被用户采纳、反复出现或高证据支持的规则进入长期画像。",
                "下一次 role-distill 会把更新后的画像重新编译成场景角色。",
            ],
            "output_contract": [
                "先给结论或主判断。",
                "再给依据、风险和反证。",
                "最后给可执行下一步或可直接使用的产物。",
                "如果用于审稿，问题按严重程度排序。",
            ],
        },
        "retrieval": {
            "terms": terms,
            "recommended_runtime_commands": [
                f"python3 {SKILL_DIR / 'immortal.py'} context \"{goal}\"",
                f"python3 {SKILL_DIR / 'immortal.py'} recall \"{goal}\"",
            ],
        },
        "evidence": selected,
    }
    package["quality_gate"] = quality_gate(package)
    return package


def render_markdown(package: dict[str, Any]) -> str:
    role = package["role"]
    sources = package["sources"]
    gate = package["quality_gate"]
    lines = [
        f"# {package['mode_label']}：{package['goal']}",
        "",
        f"- Generated: {package['generated_at']}",
        f"- Target: {package['target_name']}",
        f"- Mode: {package['mode']} / {package['mode_label']}",
        f"- Skill name: `{package['skill_name']}`",
        f"- Quality: {gate.get('status')}",
        "",
        "## 角色定位",
        "",
        role["purpose"],
        "",
        "## 架构",
        "",
    ]
    for item in package["architecture"]:
        lines.append(f"- **{item['layer']}**：{item['description']}")
    lines.extend(["", "## 适用场景", ""])
    for item in role["scenarios"]:
        lines.append(f"- {item}")
    lines.extend(["", "## 运行协议", ""])
    for i, item in enumerate(role["protocol"], 1):
        lines.append(f"{i}. {item}")
    lines.extend(["", "## 必须做", ""])
    for item in role["must_do"]:
        lines.append(f"- {item}")
    lines.extend(["", "## 绝对不要做", ""])
    for item in role["must_not"]:
        lines.append(f"- {item}")
    lines.extend(["", "## 输出契约", ""])
    for item in role["output_contract"]:
        lines.append(f"- {item}")
    lines.extend(["", "## 学习闭环", ""])
    for item in role["learning_loop"]:
        lines.append(f"- {item}")
    lines.extend(["", "## 诚实边界", ""])
    for item in role["boundaries"]:
        lines.append(f"- {item}")
    lines.extend(["", "## 数据来源", ""])
    lines.append(f"- profile_nuwa: {sources['profile_nuwa_json']['path']} ({'ok' if sources['profile_nuwa_json']['exists'] else 'missing'})")
    lines.append(f"- reviewed profile memories: {sources['reviewed_profile_memories']['rows']} rows")
    lines.append(f"- quality: {sources['quality_json'].get('status')} / score {sources['quality_json'].get('score')}")
    raw = sources["raw_recent_search"]
    lines.append(f"- raw recent search: {'on' if raw['enabled'] else 'off'}, {raw['results']} results, {raw['days']} days")
    lines.extend(["", "## 证据摘录", ""])
    for i, item in enumerate(package["evidence"][:18], 1):
        ref = f" `{item.get('ref')}`" if item.get("ref") else ""
        lines.append(f"{i}. **{item.get('kind')} / {item.get('layer')}**{ref}：{item.get('text')}（{item.get('source')}）")
    lines.extend(["", "## 质量门槛", ""])
    for check in gate.get("checks") or []:
        mark = "PASS" if check.get("ok") else "FAIL"
        lines.append(f"- {mark}: {check.get('name')} ({check.get('detail')})")
    lines.extend(["", "## 使用示例", ""])
    examples = usage_examples(package)
    for item in examples:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def usage_examples(package: dict[str, Any]) -> list[str]:
    mode = package["mode"]
    primary_account = str((load_config().get("primary_account") or "")).strip()
    account_label = primary_account or "主账号"
    if mode == "writer":
        return [
            f"“用这个角色帮我审一篇{account_label}草稿，按事实风险、结构、高潮、标题排序。”",
            f"“根据这些素材，直接写一版 1000 字以内的{account_label}文章。”",
            "“给这个选题出 5 个不超过 18 字的标题，并说明哪个最适合。”",
        ]
    if mode == "business":
        return [
            "“这个客户要不要接？按收益、风险、交付难度给判断。”",
            "“这份报价怎么改，避免把咨询价值说成劳务？”",
        ]
    if mode == "project":
        return [
            "“回顾这个项目现在到哪一关，列出下一步和验收标准。”",
            "“把这个任务拆成可并行推进的子任务。”",
        ]
    if mode == "shadow":
        return [
            "“自动判断这些候选记忆哪些应该进入长期画像，给拒绝理由。”",
            "“站在我的视角，先替我过一遍这个方案。”",
        ]
    return [
        f"“围绕{package['goal']}，按这个角色给我先判断再行动。”",
        "“调用长期记忆，给出可执行方案和证据边界。”",
    ]


def render_skill(package: dict[str, Any]) -> str:
    role = package["role"]
    target = str(package.get("target_name") or "the configured user")
    description = (
        f"{package['mode_label']} for {target}, distilled from the Immortal memory vault. "
        f"Use when the user asks about {package['goal']}, scenario-specific digital role, "
        "writing/review/advice/project decisions, or wants Codex to apply the configured user's long-term memory and judgment style."
    )
    lines = [
        "---",
        f"name: {package['skill_name']}",
        f"description: {description}",
        "---",
        "",
        f"# {package['mode_label']}",
        "",
        f"Goal: {package['goal']}",
        "",
        "This is a scenario role compiled from the Immortal vault. It should apply the role protocol, then use `immortal` recall/context when current task evidence is needed.",
        "",
        "## Runtime Rule",
        "",
        f"- Before major output, run: `python3 {SKILL_DIR / 'immortal.py'} context \"{package['goal']}\"` if live memory context is needed.",
        f"- For focused evidence, run: `python3 {SKILL_DIR / 'immortal.py'} recall \"<task topic>\"`.",
        "- Summarize sensitive evidence; do not paste private raw records or credentials.",
        f"- Do not claim to fully replace {target}. This role can draft, pre-judge, review, and organize context.",
        "",
        "## Purpose",
        "",
        role["purpose"],
        "",
        "## Scenarios",
        "",
    ]
    lines.extend(f"- {item}" for item in role["scenarios"])
    lines.extend(["", "## Protocol", ""])
    lines.extend(f"{i}. {item}" for i, item in enumerate(role["protocol"], 1))
    lines.extend(["", "## Must Do", ""])
    lines.extend(f"- {item}" for item in role["must_do"])
    lines.extend(["", "## Must Not", ""])
    lines.extend(f"- {item}" for item in role["must_not"])
    lines.extend(["", "## Output Contract", ""])
    lines.extend(f"- {item}" for item in role["output_contract"])
    lines.extend(["", "## Boundaries", ""])
    lines.extend(f"- {item}" for item in role["boundaries"])
    lines.extend(["", "## Evidence Snapshot", ""])
    for item in package["evidence"][:10]:
        lines.append(f"- {item.get('kind')} / {item.get('layer')}: {item.get('text')} ({item.get('source')})")
    lines.extend(["", "## Quality Gate", ""])
    lines.append(f"Compiled quality: {package['quality_gate'].get('status')}")
    for check in package["quality_gate"].get("checks") or []:
        mark = "PASS" if check.get("ok") else "FAIL"
        lines.append(f"- {mark}: {check.get('name')} ({check.get('detail')})")
    lines.append("")
    return "\n".join(lines)


def write_outputs(package: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "role_md": output_dir / "ROLE.md",
        "role_json": output_dir / "role.json",
        "skill_md": output_dir / "SKILL.md",
        "evidence_jsonl": output_dir / "evidence.jsonl",
    }
    paths["role_md"].write_text(render_markdown(package), encoding="utf-8")
    paths["role_json"].write_text(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["skill_md"].write_text(render_skill(package), encoding="utf-8")
    with paths["evidence_jsonl"].open("w", encoding="utf-8") as handle:
        for item in package.get("evidence") or []:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return paths


def install_skill(package: dict[str, Any], output_paths: dict[str, Path]) -> Path:
    install_dir = CODEX_SKILLS_DIR / str(package["skill_name"])
    references = install_dir / "references"
    references.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output_paths["skill_md"], install_dir / "SKILL.md")
    shutil.copy2(output_paths["role_json"], references / "role.json")
    shutil.copy2(output_paths["role_md"], references / "ROLE.md")
    shutil.copy2(output_paths["evidence_jsonl"], references / "evidence.jsonl")
    return install_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Distill a scenario-specific digital role from the Immortal vault")
    parser.add_argument("goal", nargs="*", help="目标场景，例如：写稿审稿流程")
    parser.add_argument("--goal", dest="goal_option", default=None, help="目标场景，等价于位置参数")
    parser.add_argument("--target-name", default=None)
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "advisor", "writer", "reviewer", "business", "project", "shadow", "custom"],
    )
    parser.add_argument("--slug", default=None, help="Override output slug")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-evidence", type=int, default=36)
    parser.add_argument("--raw-days", type=int, default=45)
    parser.add_argument("--max-raw-records", type=int, default=120000)
    parser.add_argument("--raw-results", type=int, default=24)
    parser.add_argument("--no-raw-search", action="store_true")
    parser.add_argument("--install-skill", action="store_true", help="Install generated SKILL.md into ~/.codex/skills")
    parser.add_argument("--show", action="store_true", help="Print generated ROLE.md")
    parser.add_argument("--json", action="store_true", help="Print generated role.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.target_name:
        args.target_name = owner_display_name()
    args.goal_text = (args.goal_option or " ".join(args.goal)).strip()
    if not args.goal_text:
        parser.error("missing goal. Example: role-distill \"写稿审稿流程\" --mode writer")

    package = build_role_package(args)
    output_dir = args.output_dir or (ROLES_DIR / str(package["slug"]))
    paths = write_outputs(package, output_dir)
    install_dir = install_skill(package, paths) if args.install_skill else None

    print("Immortal role package built")
    print()
    print(f"Goal: {package['goal']}")
    print(f"Mode: {package['mode']} / {package['mode_label']}")
    print(f"Quality: {package['quality_gate'].get('status')}")
    print(f"Output: {output_dir}")
    for name, path in paths.items():
        print(f"- {name}: {path}")
    if install_dir:
        print(f"- installed_skill: {install_dir}")
    print()
    layer_counts = package["quality_gate"].get("layer_counts") or {}
    print("Evidence layers:")
    for layer, count in sorted(layer_counts.items(), key=lambda item: item[0]):
        print(f"- {layer}: {count}")
    if args.json:
        print(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True))
    if args.show:
        print(paths["role_md"].read_text(encoding="utf-8", errors="ignore"))
    return 0 if package["quality_gate"].get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
