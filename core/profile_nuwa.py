#!/usr/bin/env python3
"""Build a Nuwa-style thinking profile from the Immortal profile layer.

This is a derived, reviewable layer. It does not edit digital-soul.md. The goal
is to turn Configured User's preserved traces into a compact operating model: mental
models, decision heuristics, expression DNA, anti-patterns, and honest
boundaries.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


HOME = Path.home()
SKILL_DIR = Path(__file__).resolve().parent
MEMORY_DIR = SKILL_DIR / "references" / "memory"
IMMORTAL_DIR = HOME / ".immortal"
PROFILE_JSON = IMMORTAL_DIR / "profile.json"
REVIEWED_PROFILE_JSONL = IMMORTAL_DIR / "reviewed" / "profile_memories.jsonl"
OUTPUT_JSON = IMMORTAL_DIR / "profile_nuwa.json"
OUTPUT_MD = IMMORTAL_DIR / "profile_nuwa.md"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

LANE_TITLES = {
    "writings": "长期文字与资料",
    "conversations": "对话与会议",
    "expression_dna": "表达 DNA",
    "external_views": "他人反馈与协作视角",
    "decisions": "决策与经验",
    "timeline": "时间线与项目演化",
}

SECTION_LANES = {
    "identity": "writings",
    "company_business": "writings",
    "content_strategy": "expression_dna",
    "communication": "external_views",
    "technical_capability": "writings",
    "decision_principles": "decisions",
    "team_hiring": "external_views",
    "risk_compliance": "decisions",
    "current_projects": "timeline",
}

SECTION_DOMAINS = {
    "identity": "identity",
    "company_business": "business",
    "content_strategy": "content",
    "communication": "collaboration",
    "technical_capability": "technical",
    "decision_principles": "judgment",
    "team_hiring": "team",
    "risk_compliance": "risk",
    "current_projects": "project",
}

MEMORY_TYPE_LANES = {
    "preference": "decisions",
    "decision": "decisions",
    "lesson": "decisions",
    "relationship": "external_views",
    "project_fact": "timeline",
    "commitment": "timeline",
}

MENTAL_MODEL_SPECS = [
    {
        "id": "trace_first_infrastructure",
        "title": "先保全痕迹，再谈智能",
        "summary": "遇到任何长期项目，先把语料、文件、对话、输出和版本保留下来，再做检索、蒸馏和人格化应用。",
        "keywords": ["防丢失", "删库", "沉淀", "保全", "备份", "可恢复", "痕迹", "记忆库", "语料", "AI 对话", "文件", "文档"],
        "application": "数据、知识库、客户材料、AI 对话沉淀、自动化采集和备份方案。",
        "limitation": "不能因为追求全量而无限扩大权限；高敏和高噪声源要先隔离、脱敏、分层。",
    },
    {
        "id": "skillify_workflows",
        "title": "把方法封装成可复用 Skill",
        "summary": "不是只解决一次问题，而是把有效流程沉淀成可触发、可迁移、可复用的 skill 或工作流。",
        "keywords": ["skill", "Skill", "工作流", "封装", "复用", "SOP", "中台", "工具", "标准", "沉淀", "钩子"],
        "application": "写作、审稿、客户交付、数据清洗、Feishu 接入、Codex/Claude 协作。",
        "limitation": "只有稳定重复的流程才值得 skill 化；一次性探索不要过早产品化。",
    },
    {
        "id": "context_before_output",
        "title": "上下文基础设施优先",
        "summary": "让 AI 做事前，先补齐上下文、资料结构、约束和判断标准，否则输出只是通用答案。",
        "keywords": ["Context Infrastructure", "上下文", "语境", "预设提示词", "知识库", "清洗", "AI能理解", "完整了解", "历史上下文"],
        "application": "客户知识库、中台智能体、写作前资料包、项目交接和长期记忆检索。",
        "limitation": "上下文不是越多越好；需要压缩、排序、去噪和来源权重。",
    },
    {
        "id": "independent_judgment_first",
        "title": "AI 前先有自己的预判",
        "summary": "AI 可以提效，但不能替代判断。先形成假设，再用 AI 和证据交叉验证。",
        "keywords": ["独立思考", "独立判断", "预判", "不能完全依赖", "复核", "AI 打分", "判断力", "纠正", "错误"],
        "application": "文章事实核查、客户方案、审稿、招聘判断、技术选型。",
        "limitation": "在陌生领域仍需要外部一手证据，不能把直觉当事实。",
    },
    {
        "id": "mvp_market_first",
        "title": "先跑通 MVP，再扩团队和包装",
        "summary": "先找到真实客户、痛点、可行原型和付费信号，再谈组织、规模化和更大的叙事。",
        "keywords": ["MVP", "先做", "跑起来", "第一个付费客户", "客户痛点", "技术可行性", "市场教育", "成交", "验证"],
        "application": "项目A客户交付、AI 店铺运营、中台产品、内容业务商业化。",
        "limitation": "MVP 不能变成低价劳务；验证的是高价值路径，不是无限让利。",
    },
    {
        "id": "value_pricing",
        "title": "按价值定价，不按劳务定价",
        "summary": "对客户要表达总价值和咨询价值，而不是用工时报价把自己放进劳务位置。",
        "keywords": ["报总价", "咨询的价格", "劳务的价格", "时间换取", "报价", "小生意", "客户无法拒绝", "培训的时间"],
        "application": "商务报价、客户陪跑、AI 培训、定制开发和咨询方案。",
        "limitation": "价值叙事必须有交付证据支撑，否则会变成空包装。",
    },
    {
        "id": "persona_boundaries",
        "title": "账号和人设边界要硬隔离",
        "summary": "主账号、账号边界A、协作账号、外部参考账号等上下文不能混用；方法可以借，身份不能串。",
        "keywords": ["账号边界", "人设", "主账号", "账号边界A", "外部参考账号", "分开", "不能套", "作者身份", "协作账号", "协作账号"],
        "application": "写作、账号运营、人物记忆、风格迁移和对外表达。",
        "limitation": "边界隔离不代表资料不互相启发；要标清来源和用途。",
    },
    {
        "id": "risk_named_and_isolated",
        "title": "先命名风险，再隔离处理",
        "summary": "涉及客户数据、灰色业务、平台权限和安全问题时，先识别风险，再脱敏、代号化、分层存储。",
        "keywords": ["合规", "风险", "灰色", "安全", "隐私", "脱敏", "代号", "服务器", "竞争对手", "红线"],
        "application": "客户资料、Feishu 数据、服务器部署、刷单等敏感业务语境。",
        "limitation": "风险隔离不能替代法律合规判断；高风险事项需要单独确认。",
    },
    {
        "id": "delegate_with_leverage",
        "title": "把可承接工作交出去，自己守关键判断",
        "summary": "用户本人应保留关键判断和一号位推进，把研究、承接、维护、基础开发交给合适的人或 agent。",
        "keywords": ["承接", "不要一直由用户本人亲自扛", "小舟", "新人培养", "负责", "主导", "配合", "招聘", "AI 可辅助", "多 agent"],
        "application": "团队分工、客户维护、agent 并发、招聘和培训。",
        "limitation": "委派前需要清晰验收标准；不能把最终判断也外包出去。",
    },
]

HEURISTIC_SPECS = [
    ("先防丢，再提炼", ["防丢失", "删库", "保全", "备份", "可恢复"], "任何新数据源先进入可恢复仓库，再进入画像或看板。"),
    ("自动审阅优先，人只处理例外", ["自动审阅", "不用审阅", "全自动", "profile-auto-review"], "常规候选记忆由规则和证据门槛处理，用户只看结果。"),
    ("先预判，再看 AI 输出", ["预判", "独立判断", "复核", "不能完全依赖"], "研究、审稿和选型前先写下自己的判断点。"),
    ("报价讲总价值，不讲工时", ["报总价", "咨询的价格", "劳务的价格", "报价"], "客户沟通中先锚定业务收益和总方案。"),
    ("账号上下文硬隔离", ["主账号", "账号边界A", "外部参考账号", "人设", "账号边界"], "写作和记忆调用前先确认使用哪个账号视角。"),
    ("先跑一个能用的版本", ["MVP", "先做", "跑起来", "第一个付费客户"], "项目早期先打通核心路径，再做扩展和美化。"),
    ("敏感内容先脱敏", ["脱敏", "灰色", "安全", "隐私", "代号"], "客户、平台、灰色语义和凭证默认不进入公开输出。"),
    ("把流程变成工具资产", ["Skill", "工作流", "封装", "复用", "钩子"], "重复三次以上的流程优先沉淀为 skill、脚本或模板。"),
]

EXPRESSION_FEATURES = [
    {
        "name": "直接中文，短句，不绕",
        "keywords": ["直接", "短句", "少废话", "不要长报告", "中文", "沟通"],
        "fallback": "用户偏好直接、短句、任务导向的中文协作，不喜欢长篇空泛报告。",
    },
    {
        "name": "先给结论，再给依据和动作",
        "keywords": ["结论", "判断", "下一步", "行动", "直接纠正"],
        "fallback": "输出要能推动任务，不能只停在解释层。",
    },
    {
        "name": "内容上强调普通人利益和商业判断",
        "keywords": ["普通人", "商业判断", "主账号", "翻译复杂事件", "利益"],
        "fallback": "主账号 内容要把复杂事件翻译成普通读者能感知的利益和风险。",
    },
    {
        "name": "排版偏短段落、加粗判断、清晰小标题",
        "keywords": ["加粗", "短段落", "小标题", "排版", "##"],
        "fallback": "写作输出应使用短段落、明确小标题和适度加粗的判断句。",
    },
    {
        "name": "不接受通用 AI 腔",
        "keywords": ["通用", "AI味", "不要", "人设", "风格"],
        "fallback": "避免模板化、过度礼貌、抽象安全话术和泛泛建议。",
    },
]

ANTI_PATTERNS = [
    ("让用户反复手动审阅", ["不用审阅", "全自动", "自动审阅", "自己做清洗"]),
    ("把看板当成产品本体", ["不是 dashboard", "看板", "产品原点", "Skill first"]),
    ("混淆用户本人、协作账号、账号边界A和外部参考账号", ["人设", "主账号", "账号边界A", "外部参考账号", "协作账号"]),
    ("拿 AI 输出替代自己的判断", ["独立判断", "不能完全依赖", "预判", "AI 打分"]),
    ("按工时报价，把咨询价值降成劳务", ["劳务的价格", "报总价", "咨询的价格"]),
    ("高噪声数据直接写入 digital-soul.md", ["digital-soul", "review layer", "飞书", "噪声"]),
]

HONEST_BOUNDARIES = [
    "本报告是规则化蒸馏，不是大模型重新读完全量 416MB 原始资料后的最终人格。",
    "飞书和本地数据会持续变化；结论以本次生成时的 profile/reviewed 层为准。",
    "证据不足的模型会降级为启发式，不应该被当成稳定人格特征。",
    "人物关系和账号风格容易污染画像，必须继续用质量报告和身份规则过滤。",
    "digital-soul.md 当前质量一般，本脚本只生成可复查画像，不直接覆盖它。",
]


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def compact(text: Any, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def source_title(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("title") or row.get("source") or "unknown")


def source_id(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("url") or source.get("raw_id") or source.get("title") or row.get("source") or "")


def load_profile_evidence(profile: dict[str, Any], reviewed_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for section in profile.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id") or "unknown")
        lane = SECTION_LANES.get(section_id, "writings")
        domain = SECTION_DOMAINS.get(section_id, section_id)
        for group in section.get("items") or []:
            if not isinstance(group, dict):
                continue
            source = str(group.get("source") or section_id)
            for item in group.get("items") or []:
                text = compact(item, 500)
                if not text:
                    continue
                evidence.append(
                    {
                        "text": text,
                        "lane": lane,
                        "domain": domain,
                        "source": source,
                        "source_id": f"{section_id}:{source}",
                        "kind": "distilled_memory_file",
                        "weight": 1.0,
                    }
                )
    for row in reviewed_rows:
        statement = compact(row.get("statement") or row.get("evidence") or "", 700)
        if not statement:
            continue
        memory_type = str(row.get("memory_type") or "other")
        focus = str(row.get("focus") or "other")
        projects = row.get("projects") if isinstance(row.get("projects"), list) else []
        domains = [focus] + [str(project) for project in projects[:3]]
        evidence.append(
            {
                "text": statement,
                "lane": MEMORY_TYPE_LANES.get(memory_type, "conversations"),
                "domain": domains[0],
                "domains": domains,
                "source": source_title(row),
                "source_id": source_id(row) or str(row.get("memory_id") or ""),
                "kind": f"reviewed_{memory_type}",
                "memory_id": row.get("memory_id"),
                "confidence": row.get("confidence"),
                "relevance_score": row.get("relevance_score"),
                "weight": float(row.get("confidence") or 0.8) + float(row.get("relevance_score") or 0.7),
            }
        )
    for item in profile.get("recent_profile_facts") or []:
        fact = compact(item.get("fact") or "", 500)
        if fact:
            evidence.append(
                {
                    "text": fact,
                    "lane": "timeline",
                    "domain": "recent",
                    "source": str(item.get("source") or "recent_profile_facts"),
                    "source_id": str(item.get("id") or item.get("timestamp") or ""),
                    "kind": "recent_fact",
                    "weight": 1.2,
                }
            )
    return evidence


def matches_keywords(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def evidence_domains(row: dict[str, Any]) -> list[str]:
    domains = row.get("domains") if isinstance(row.get("domains"), list) else None
    if domains:
        return [str(item) for item in domains if item]
    domain = row.get("domain")
    return [str(domain)] if domain else []


def sample_evidence(rows: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    seen: set[str] = set()
    samples: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: float(item.get("weight") or 0), reverse=True):
        key = str(row.get("source_id") or row.get("text"))
        if key in seen:
            continue
        seen.add(key)
        samples.append(
            {
                "text": compact(row.get("text"), 180),
                "source": compact(row.get("source"), 90),
                "lane": row.get("lane"),
                "kind": row.get("kind"),
                "memory_id": row.get("memory_id"),
            }
        )
        if len(samples) >= limit:
            break
    return samples


def build_mental_models(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for spec in MENTAL_MODEL_SPECS:
        rows = [row for row in evidence if matches_keywords(str(row.get("text") or ""), spec["keywords"])]
        domains = sorted({domain for row in rows for domain in evidence_domains(row)})
        lanes = sorted({str(row.get("lane")) for row in rows if row.get("lane")})
        source_count = len({str(row.get("source_id") or row.get("source")) for row in rows})
        cross_domain = len(domains) >= 2
        generative = len(rows) >= 2
        distinctive = source_count >= 2 and any(row.get("kind") != "distilled_memory_file" for row in rows)
        score = len(rows) + len(domains) * 2 + len(lanes) + source_count
        status = "accepted" if cross_domain and generative and distinctive else "candidate"
        if len(rows) == 0:
            continue
        models.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "summary": spec["summary"],
                "application": spec["application"],
                "limitation": spec["limitation"],
                "score": score,
                "status": status,
                "evidence_count": len(rows),
                "source_count": source_count,
                "domains": domains,
                "lanes": lanes,
                "validation": {
                    "cross_domain_recurrence": cross_domain,
                    "generative_power": generative,
                    "distinctiveness": distinctive,
                },
                "evidence": sample_evidence(rows),
            }
        )
    models.sort(key=lambda item: (item["status"] == "accepted", item["score"]), reverse=True)
    accepted = [model for model in models if model["status"] == "accepted"]
    candidates = [model for model in models if model["status"] != "accepted"]
    selected = (accepted + candidates)[:7]
    if len(selected) < 3:
        return selected
    return selected


def build_heuristics(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    heuristics: list[dict[str, Any]] = []
    for title, keywords, rule in HEURISTIC_SPECS:
        rows = [row for row in evidence if matches_keywords(str(row.get("text") or ""), keywords)]
        if not rows:
            continue
        domains = sorted({domain for row in rows for domain in evidence_domains(row)})
        heuristics.append(
            {
                "title": title,
                "rule": rule,
                "evidence_count": len(rows),
                "domains": domains,
                "confidence": "high" if len(rows) >= 3 and len(domains) >= 2 else "medium",
                "evidence": sample_evidence(rows, limit=2),
            }
        )
    heuristics.sort(key=lambda item: (item["confidence"] == "high", item["evidence_count"]), reverse=True)
    return heuristics[:10]


def load_memory_texts() -> dict[str, str]:
    names = [
        "user_writing_preferences.md",
        "feedback_communication_style.md",
        "feedback_bold_formatting.md",
        "feedback_article_length.md",
        "feedback_punctuation_usage.md",
        "feedback_autonomous_agent.md",
        "feedback_ai_review_independent_judgment.md",
    ]
    texts: dict[str, str] = {}
    for name in names:
        path = MEMORY_DIR / name
        if path.exists():
            texts[name] = strip_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
    return texts


def build_expression_dna(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    memory_texts = load_memory_texts()
    corpus = "\n".join(memory_texts.values()) + "\n" + "\n".join(str(row.get("text") or "") for row in evidence)
    features = []
    for spec in EXPRESSION_FEATURES:
        rows = [row for row in evidence if matches_keywords(str(row.get("text") or ""), spec["keywords"])]
        matched_files = [name for name, text in memory_texts.items() if matches_keywords(text, spec["keywords"])]
        support = len(rows) + len(matched_files)
        if support == 0 and spec.get("fallback"):
            support = 1
        features.append(
            {
                "name": spec["name"],
                "description": spec["fallback"],
                "support": support,
                "sources": matched_files[:3],
                "evidence": sample_evidence(rows, limit=2),
            }
        )
    punctuation = {
        "question_marks": corpus.count("?") + corpus.count("？"),
        "exclamation_marks": corpus.count("!") + corpus.count("！"),
        "first_person_mentions": len(re.findall(r"我|我的|我要|我希望|我认为", corpus)),
        "bold_markers": corpus.count("**") // 2,
    }
    return [{"metrics": punctuation}, *features]


def build_anti_patterns(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    patterns = []
    for title, keywords in ANTI_PATTERNS:
        rows = [row for row in evidence if matches_keywords(str(row.get("text") or ""), keywords)]
        patterns.append(
            {
                "title": title,
                "evidence_count": len(rows),
                "evidence": sample_evidence(rows, limit=2),
            }
        )
    return patterns


def build_research_lanes(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    lanes: dict[str, Any] = {}
    for lane_id, title in LANE_TITLES.items():
        rows = [row for row in evidence if row.get("lane") == lane_id]
        lanes[lane_id] = {
            "title": title,
            "records": len(rows),
            "sources": len({str(row.get("source_id") or row.get("source")) for row in rows}),
            "domains": sorted({domain for row in rows for domain in evidence_domains(row)}),
            "sample": sample_evidence(rows, limit=3),
        }
    return lanes


def quality_gate(report: dict[str, Any]) -> dict[str, Any]:
    models = report.get("mental_models") or []
    accepted = [model for model in models if model.get("status") == "accepted"]
    heuristics = report.get("decision_heuristics") or []
    lanes = report.get("research_lanes") or {}
    covered_lanes = [lane_id for lane_id, lane in lanes.items() if int(lane.get("records") or 0) > 0]
    checks = [
        {
            "name": "3-7 mental models",
            "ok": 3 <= len(models) <= 7,
            "detail": f"{len(models)} models",
        },
        {
            "name": "at least 3 accepted models",
            "ok": len(accepted) >= 3,
            "detail": f"{len(accepted)} accepted",
        },
        {
            "name": "5-10 decision heuristics",
            "ok": 5 <= len(heuristics) <= 10,
            "detail": f"{len(heuristics)} heuristics",
        },
        {
            "name": "six-lane evidence coverage",
            "ok": len(covered_lanes) >= 5,
            "detail": f"{len(covered_lanes)}/6 lanes covered",
        },
        {
            "name": "honest boundaries present",
            "ok": len(report.get("honest_boundaries") or []) >= 3,
            "detail": f"{len(report.get('honest_boundaries') or [])} boundaries",
        },
    ]
    return {
        "status": "ok" if all(item["ok"] for item in checks) else "attention",
        "checks": checks,
    }


def build_report() -> dict[str, Any]:
    profile = read_json(PROFILE_JSON, {})
    reviewed_rows = load_jsonl(REVIEWED_PROFILE_JSONL)
    evidence = load_profile_evidence(profile, reviewed_rows)
    report = {
        "version": "0.1-nuwa-profile",
        "generated_at": now_local(),
        "inputs": {
            "profile_json": str(PROFILE_JSON),
            "profile_generated_at": profile.get("generated_at"),
            "reviewed_profile_memories": len(reviewed_rows),
            "evidence_records": len(evidence),
        },
        "method": {
            "source": "Adapted from huashu-nuwa: six-lane research, triple validation, expression DNA, honest boundaries.",
            "triple_validation": ["cross_domain_recurrence", "generative_power", "distinctiveness"],
        },
        "research_lanes": build_research_lanes(evidence),
        "mental_models": build_mental_models(evidence),
        "decision_heuristics": build_heuristics(evidence),
        "expression_dna": build_expression_dna(evidence),
        "anti_patterns": build_anti_patterns(evidence),
        "honest_boundaries": HONEST_BOUNDARIES,
    }
    report["quality_gate"] = quality_gate(report)
    report["evidence_stats"] = {
        "by_lane": Counter(str(row.get("lane") or "unknown") for row in evidence),
        "by_kind": Counter(str(row.get("kind") or "unknown") for row in evidence),
    }
    return report


def render_bool(value: bool) -> str:
    return "PASS" if value else "LOW"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 用户本人 Nuwa Profile",
        "",
        f"Generated: {report['generated_at']}",
        f"Version: {report['version']}",
        "",
        "这是 Nuwa 风格的画像蒸馏层：不复制人设，提炼用户本人如何判断、表达、取舍和避坑。",
        "它是 `profile.md` 与 reviewed 记忆之上的派生层，不直接覆盖 `digital-soul.md`。",
        "",
        "## Source Inputs",
        "",
    ]
    inputs = report.get("inputs") or {}
    lines.append(f"- profile: {inputs.get('profile_json')} ({inputs.get('profile_generated_at')})")
    lines.append(f"- reviewed profile memories: {inputs.get('reviewed_profile_memories')}")
    lines.append(f"- evidence records: {inputs.get('evidence_records')}")
    lines.append("")

    lines.append("## Six-Lane Research Coverage")
    lines.append("")
    for lane_id, lane in (report.get("research_lanes") or {}).items():
        lines.append(f"### {lane.get('title')} ({lane_id})")
        lines.append("")
        lines.append(f"- records: {lane.get('records')} / sources: {lane.get('sources')}")
        domains = ", ".join(lane.get("domains") or []) or "none"
        lines.append(f"- domains: {domains}")
        for sample in lane.get("sample") or []:
            lines.append(f"- evidence: {sample.get('text')} ({sample.get('source')})")
        lines.append("")

    lines.append("## Core Mental Models")
    lines.append("")
    for model in report.get("mental_models") or []:
        validation = model.get("validation") or {}
        lines.append(f"### {model.get('title')}")
        lines.append("")
        lines.append(model.get("summary") or "")
        lines.append("")
        lines.append(
            "- validation: "
            f"cross-domain={render_bool(bool(validation.get('cross_domain_recurrence')))}, "
            f"generative={render_bool(bool(validation.get('generative_power')))}, "
            f"distinctive={render_bool(bool(validation.get('distinctiveness')))}"
        )
        lines.append(f"- status: {model.get('status')} / evidence: {model.get('evidence_count')} / domains: {', '.join(model.get('domains') or [])}")
        lines.append(f"- use when: {model.get('application')}")
        lines.append(f"- limitation: {model.get('limitation')}")
        for sample in model.get("evidence") or []:
            memory_id = f" `{sample.get('memory_id')}`" if sample.get("memory_id") else ""
            lines.append(f"- evidence{memory_id}: {sample.get('text')} ({sample.get('source')})")
        lines.append("")

    lines.append("## Decision Heuristics")
    lines.append("")
    for item in report.get("decision_heuristics") or []:
        lines.append(f"- **{item.get('title')}**: {item.get('rule')}")
        lines.append(f"  - confidence: {item.get('confidence')} / evidence: {item.get('evidence_count')}")
    lines.append("")

    lines.append("## Expression DNA")
    lines.append("")
    expression = report.get("expression_dna") or []
    metrics = expression[0].get("metrics") if expression and isinstance(expression[0], dict) else {}
    if metrics:
        lines.append(
            "- metrics: "
            f"first-person={metrics.get('first_person_mentions')}, "
            f"bold={metrics.get('bold_markers')}, "
            f"questions={metrics.get('question_marks')}, "
            f"exclamations={metrics.get('exclamation_marks')}"
        )
    for item in expression[1:]:
        lines.append(f"- **{item.get('name')}**: {item.get('description')}")
        sources = ", ".join(item.get("sources") or [])
        if sources:
            lines.append(f"  - sources: {sources}")
    lines.append("")

    lines.append("## Anti-Patterns")
    lines.append("")
    for item in report.get("anti_patterns") or []:
        lines.append(f"- {item.get('title')} (evidence={item.get('evidence_count')})")
    lines.append("")

    lines.append("## Honest Boundaries")
    lines.append("")
    for item in report.get("honest_boundaries") or []:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## Quality Gate")
    lines.append("")
    gate = report.get("quality_gate") or {}
    lines.append(f"Status: {gate.get('status')}")
    lines.append("")
    for check in gate.get("checks") or []:
        mark = "PASS" if check.get("ok") else "FAIL"
        lines.append(f"- {mark}: {check.get('name')} ({check.get('detail')})")
    lines.append("")
    return "\n".join(lines)


def write_outputs(report: dict[str, Any], *, output_json: Path, output_md: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(render_markdown(report), encoding="utf-8")


def summarize(report: dict[str, Any]) -> str:
    accepted = sum(1 for item in report.get("mental_models") or [] if item.get("status") == "accepted")
    gate = report.get("quality_gate") or {}
    return (
        "Nuwa profile built: "
        f"{len(report.get('mental_models') or [])} mental models ({accepted} accepted), "
        f"{len(report.get('decision_heuristics') or [])} heuristics, "
        f"quality={gate.get('status')}, "
        f"evidence={report.get('inputs', {}).get('evidence_records')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a Nuwa-style thinking profile for Configured User")
    parser.add_argument("--show", action="store_true", help="Print generated Markdown")
    parser.add_argument("--json", action="store_true", help="Print generated JSON")
    parser.add_argument("--output-json", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--output-md", type=Path, default=OUTPUT_MD)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report()
    write_outputs(report, output_json=args.output_json, output_md=args.output_md)
    print(summarize(report))
    print(f"Wrote: {args.output_json}")
    print(f"Wrote: {args.output_md}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.show:
        print(args.output_md.read_text(encoding="utf-8", errors="ignore"))
    return 0 if (report.get("quality_gate") or {}).get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
