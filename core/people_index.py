#!/usr/bin/env python3
"""Build a person-facing memory index for the Immortal dashboard.

This is not a review queue. It turns already distilled memory rows into
readable person cards: who the person is in the vault, what they are connected
to, and the highest-signal evidence snippets.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


HOME = Path.home()
IMMORTAL_DIR = HOME / ".immortal"
DISTILLED_DIR = IMMORTAL_DIR / "feishu" / "distilled"
REVIEWED_FILE = IMMORTAL_DIR / "reviewed" / "profile_memories.jsonl"
OUTPUT_DIR = IMMORTAL_DIR / "people"
OUTPUT_JSON = OUTPUT_DIR / "people_index.json"
OUTPUT_MD = OUTPUT_DIR / "people_index.md"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

MEMORY_SOURCES = [
    (REVIEWED_FILE, "reviewed_profile", 4.0),
    (DISTILLED_DIR / "profile_memories.jsonl", "profile_memory", 2.2),
    (DISTILLED_DIR / "reference_memories.jsonl", "reference_memory", 1.5),
    (DISTILLED_DIR / "memories.jsonl", "distilled_memory", 1.0),
]

USER_ALIASES = {"用户本人", "用户本人", "Configured User", "用户本人"}
USER_CANONICAL = "用户本人（用户本人 / Configured User）"
TAOZI_CANONICAL = "协作者B（协作者B）"
BIBI_CANONICAL = "协作账号（协作者A / 协作账号）"
MAMA_CANONICAL = "协作者C（协作者C）"
NON_PERSON_ENTITIES: set[str] = set()
MAMA_ROLE_RE = re.compile(r"@?同事代理账号\s*协作者C|@?同事代理账号")
BIBI_BRAND_CONTEXT_RE = re.compile(
    r"(协作账号[-—_ ]?(账号|品牌|公众号|栏目|文章|视频|内容|矩阵|社群|方案|案例|业务|客户|平台|IP)|"
    r"协作账号[-—_ ]?(账号|品牌|公众号|栏目|文章|视频|内容|矩阵|社群|方案|案例|业务|客户|平台|IP)|"
    r"(账号|品牌|公众号|栏目|文章|视频|内容|矩阵|社群|方案|案例|业务|客户|平台|IP).{0,8}协作账号)"
)
BIBI_PERSON_CONTEXT_RE = re.compile(
    r"(协作者A|协作账号(说|认为|负责|提醒|组织|审稿|反馈|要求|提到|提出|明确|提供|赋能|协助|主导|@)|"
    r"协作账号(提供|赋能|负责|协助|主导|提出|明确)|@协作账号|协作账号@协作账号)"
)

ALIASES = {
    "用户本人": USER_CANONICAL,
    "用户本人": USER_CANONICAL,
    "Configured User": USER_CANONICAL,
    "用户本人": USER_CANONICAL,
    "协作者B": TAOZI_CANONICAL,
    "协作者B": TAOZI_CANONICAL,
    "协作账号": BIBI_CANONICAL,
    "协作者A": BIBI_CANONICAL,
    "协作账号": BIBI_CANONICAL,
    "协作者C": MAMA_CANONICAL,
    "协作者C": MAMA_CANONICAL,
    "同事代理账号": MAMA_CANONICAL,
    "同事代理账号 协作者C": MAMA_CANONICAL,
    "错误账号": "错误账号",
    "错误账号": "错误账号",
}

CANONICAL_ALIASES = {
    USER_CANONICAL: ["用户本人", "用户本人", "Configured User", "用户本人"],
    TAOZI_CANONICAL: ["协作者B", "协作者B"],
    BIBI_CANONICAL: ["协作账号", "协作者A", "协作账号"],
    MAMA_CANONICAL: ["协作者C", "协作者C", "同事代理账号"],
    "错误账号": ["错误账号", "错误账号", "错误账号"],
}

KNOWN_PERSON_HINTS = {
    USER_CANONICAL: "用户本人，也是这套记忆库默认服务的主体。记忆中与 AI 工具、内容业务、客户方案、团队协作、数据沉淀和长期画像高度相关。",
    "协作者F": "团队协作成员，记忆中常出现在执行反馈、技术运维、员工场景观察、客户服务群和日常承接相关记录里。",
    "协作者G": "团队协作成员，记忆中常与中台、客户服务器、技术承接、多 Agent 管理和新人培养相关。",
    "协作者E": "商务协作成员，记忆中常与客户报价、筛客、商务流程、确权和销售沟通相关。",
    "协作者D": "团队协作成员，记忆中常与投流、选题系统、数据中台、额度和内容运营协作相关。",
    "协作者L": "内容与项目协作成员，记忆中常与审稿、内容项目、AI 工具反馈和文章生产流程相关。",
    "外部客户A": "外部客户或项目方，记忆中常与某区域电商、中台方案、AI 应用规划、企业数据与本地部署诉求相关。",
    BIBI_CANONICAL: "协作账号、协作者A和协作账号是同一个人。记忆中常与协作账号账号、内容标准、审稿、商务代理、飞书代理、客户确权和业务策略相关。",
    MAMA_CANONICAL: "协作者C，本名协作者C，曾用飞书名“同事代理账号 协作者C”，是已经离职的同事。记忆中常与商务代理、客户对接、报价、合同、医美项目和送别材料相关。",
    "协作者H": "团队协作成员，记忆中出现在内容、运营或项目协作相关记录里。",
    TAOZI_CANONICAL: "协作者B和协作者B是同一个人。记忆中常出现在团队协作、招聘、绩效、内容运营、会议组织和业务沟通相关记录里。",
    "协作者I": "团队协作成员，记忆中出现在项目协作和内容生产相关记录里。",
    "协作者J": "团队协作成员，记忆中出现在内容生产、编辑协作和工具使用边界相关记录里。",
    "错误账号": "团队协作成员，飞书显示名也可能是“错误账号”。此前曾被误判为非人物；现在按同事保留在人物档案中，等待更多高质量协作记录补充。",
    "协作者K": "记忆库中的低频人物，需要更多证据后再形成稳定介绍。",
}

PROFILE_OVERRIDES = {
    USER_CANONICAL: {
        "role_summary": "记忆库主体与默认决策视角，负责把 AI 工具、内容业务、客户方案和团队协作沉淀成可复用能力。",
        "relationship_to_user": "这是用户本人；所有人物、项目和偏好都以他的视角归档。",
        "work_context": "核心上下文集中在主账号、账号边界A、项目A、永生记忆库、商务交付和内容运营。",
        "current_line": "当前判断：这个档案是长期画像的主轴，后续新增语料应优先区分“用户本人自己的话”和“他人对用户本人的描述”。",
    },
    "协作者F": {
        "role_summary": "日常执行、技术运维和员工场景反馈的承接者。",
        "relationship_to_user": "与用户本人是内部协作关系，经常承接 AI 技术运维、培训需求收集和客户服务群里的执行反馈。",
        "work_context": "常见场景包括 OpenClaw 支持、员工 AI 培训、客户服务群协同、日常运维和具体问题收集。",
        "current_line": "当前判断：这是高价值团队节点，适合持续沉淀成“执行反馈/运维承接/培训落地”的人物档案。",
    },
    "协作者G": {
        "role_summary": "技术承接和中台/客户服务器相关协作成员。",
        "relationship_to_user": "与用户本人围绕技术交付、客户环境、Agent 管理和新人培养协作。",
        "work_context": "常见场景包括客户服务器、中台方案、Mac mini 单点 Agent、OpenClaw/龙虾和技术问题承接。",
        "current_line": "当前判断：这是技术交付链路里的核心协作节点，后续应继续补足客户项目里的具体分工。",
    },
    "协作者E": {
        "role_summary": "商务协作成员，负责筛客、报价、确权和商务流程收口。",
        "relationship_to_user": "与用户本人在商务线索、客户沟通和成交流程上协作。",
        "work_context": "常见场景包括山东客户、新客户筛选、公众号商务码、报价和商务渠道管理。",
        "current_line": "当前判断：适合归入商务流程节点，重点追踪“谁筛客、谁成交、谁交付”的边界。",
    },
    "协作者D": {
        "role_summary": "投流、选题系统和内容运营数据相关协作成员。",
        "relationship_to_user": "与用户本人围绕内容运营、投流、AB Test、数据中台和选题系统协作。",
        "work_context": "常见场景包括公众号/内容投流、选题判断、数据看板、额度和内容运营反馈。",
        "current_line": "当前判断：这是内容运营数据链路里的关键人物，后续应把投流和选题系统的职责继续结构化。",
    },
    "协作者L": {
        "role_summary": "内容生产、审稿和项目协作成员。",
        "relationship_to_user": "与用户本人围绕文章生产、审稿标准、选题流程和 AI 工具反馈协作。",
        "work_context": "常见场景包括协作账号内容、编辑培养、审稿压力分解、文章生产流程和 AI 工具体验反馈。",
        "current_line": "当前判断：这是内容生产链路里的高频节点，后续应区分“审稿标准输出”和“具体项目执行”。",
    },
    BIBI_CANONICAL: {
        "role_summary": "协作账号相关 IP、内容标准、资源和业务策略的重要协作方。",
        "relationship_to_user": "协作账号、协作者A和协作账号按同一人处理；与用户本人存在业务决策、IP 赋能、内容标准和客户资源协作。",
        "work_context": "常见场景包括协作账号账号、内容审稿、商务代理、客户确权、选题标准、微信指数和业务策略。",
        "current_line": "当前判断：这是强人物档案，必须持续区分“协作账号这个人”和“协作账号这个品牌/账号”。",
    },
    MAMA_CANONICAL: {
        "role_summary": "前同事，曾承担同事代理账号和客户对接相关工作。",
        "relationship_to_user": "协作者C、本名协作者C，曾用飞书名“同事代理账号 协作者C”；现在按已离职同事归档。",
        "work_context": "常见场景包括商务代理、客户报价、合同、医美项目、客户沟通和离职/送别材料。",
        "current_line": "当前判断：这是已确认身份合并档案，后续重点保留商务案例和客户交接信息。",
    },
    TAOZI_CANONICAL: {
        "role_summary": "团队协作和管理支持成员，常出现在招聘、绩效、会议组织和业务沟通里。",
        "relationship_to_user": "协作者B和协作者B按同一人处理；与用户本人在团队管理、沟通辅导和业务推进上有关联。",
        "work_context": "常见场景包括招聘、绩效、周例会、业务人员沟通、内容运营和团队协作。",
        "current_line": "当前判断：身份已合并，后续应继续补足她在管理支持和业务协调中的稳定职责。",
    },
    "错误账号": {
        "role_summary": "已确认同事，飞书显示名可能为“错误账号”。",
        "relationship_to_user": "错误账号不是用户本人，也不是误抓账号；按用户本人的同事保留为人物线索。",
        "work_context": "目前证据主要来自项目A、主账号、协作账号和商务交付相关记录，稳定职责还需要自动补证据。",
        "current_line": "当前判断：身份已由用户确认，但高质量协作证据偏少；系统继续自动补强。",
    },
    "外部客户A": {
        "role_summary": "外部客户或项目方，主要关联企业 AI 应用、中台方案和飞书生态迁移诉求。",
        "relationship_to_user": "与用户本人是客户/项目方关系，围绕某区域电商、企业数据和交付方案沟通。",
        "work_context": "常见场景包括钉钉到飞书迁移、本地部署、中台方案、企业数据和 AI 应用规划。",
        "current_line": "当前判断：这是外部客户档案，后续应把客户需求、承诺和交付边界分开记录。",
    },
}

CATEGORY_BY_NAME = {
    USER_CANONICAL: "self",
    "协作者F": "team",
    "协作者G": "team",
    "协作者E": "team",
    "协作者D": "team",
    "协作者L": "team",
    "协作者H": "team",
    TAOZI_CANONICAL: "team",
    "协作者I": "team",
    "协作者J": "team",
    "错误账号": "team",
    MAMA_CANONICAL: "team",
    "外部客户A": "customer",
    BIBI_CANONICAL: "business",
    "协作者K": "other",
}

CATEGORY_LABELS = {
    "self": "用户本人",
    "team": "团队同事",
    "business": "业务协作方",
    "customer": "客户/外部项目方",
    "other": "低频人物",
}

PROJECT_LABELS = {
    "main_account": "主账号",
    "machine0": "账号边界A",
    "project_a": "项目A",
    "immortal": "永生记忆库",
    "feishu": "飞书",
    "openclaw": "OpenClaw / 龙虾",
    "content_ops": "内容运营",
    "business_ops": "商务与交付",
    "partner_brand": "协作账号",
    "general": "通用记忆",
}

SOURCE_LAYER_LABELS = {
    "reviewed_profile": "长期画像层",
    "profile_memory": "画像蒸馏层",
    "reference_memory": "参考记忆",
    "distilled_memory": "全部蒸馏记忆",
}

TYPE_LABELS = {
    "preference": "偏好/原则",
    "decision": "决策",
    "lesson": "经验教训",
    "relationship": "关系/职责",
    "project_fact": "项目事实",
    "commitment": "承诺/待办",
    "timeline_event": "时间线",
    "meeting_index": "会议索引",
}

TYPE_WEIGHT = {
    "relationship": 0.9,
    "decision": 0.75,
    "preference": 0.7,
    "lesson": 0.58,
    "project_fact": 0.45,
    "commitment": 0.35,
    "timeline_event": 0.1,
    "meeting_index": 0.05,
}

LOW_SIGNAL_RE = re.compile(
    r"(本章节主要|本次会议|文字记录|你现在能看到吗|听得到吗|哈哈|嗯嗯|优化后文案|正式版|"
    r"候选人|面试|全员被开|转正时被要求|临时需求|@同事代理账号|@错误账号|"
    r"约定时间|线上加入|等待人员加入|提醒参会|会议记录|录制：|智能纪要：|职业冒险|口播脚本|盾牌|"
    r"游戏通关视频脚本|送别视频|还记得我们一起|客户无法拒绝的报价方案|冒险脚本)"
)
REPRESENTATIVE_NOISE_RE = re.compile(
    r"(`[^`]+`\s*){5,}|(@[\w\u4e00-\u9fff（）()]+[\s,，、]*){4,}|"
    r"^(一|二|三|四|五|六|七|八|九|十)、|^第[一二三四五六七八九十]+[章节部分]|"
    r"给直接管理者|候选记忆索引|已纳入候选|文字记录|会议纪要$"
)
SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9_\-]{16,}|cli_[A-Za-z0-9_\-]{8,}|"
    r"api[_ -]?key\s*[:=]\s*\S+|app\s*(id|secret)\s*[:=：]\s*\S+|"
    r"password\s*[:=：]\s*\S+|密码\s*[:=：]\s*\S+|token\s*[:=：]\s*\S+)"
)


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


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


def redact(text: str) -> str:
    return SECRET_RE.sub("[SECRET]", str(text or ""))


def compact(text: str, limit: int = 260) -> str:
    text = redact(text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", "[URL]", text)
    text = re.sub(r"\s+", " ", text).strip(" -#\t")
    text = re.sub(r"^\[\s*[xX ]\s*\]\s*", "", text)
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def canonical_name(name: str) -> str:
    name = str(name or "").strip().lstrip("@").strip()
    if MAMA_ROLE_RE.fullmatch(name):
        return MAMA_CANONICAL
    return ALIASES.get(name, name)


def source_title(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("title") or "unknown source")


def source_kind(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("source") or "")


def source_url(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("url") or "")


def is_low_signal(row_or_text: dict[str, Any] | str) -> bool:
    if isinstance(row_or_text, dict):
        text = "\n".join(
            [
                str(row_or_text.get("statement") or row_or_text.get("evidence") or ""),
                source_title(row_or_text),
            ]
        )
    else:
        text = str(row_or_text or "")
    return bool(LOW_SIGNAL_RE.search(text))


def is_representative_noise(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return True
    if REPRESENTATIVE_NOISE_RE.search(text):
        return True
    if text.count("@") >= 4:
        return True
    if text.count("`") >= 8:
        return True
    if len(text) < 14:
        return True
    return False


def row_key(row: dict[str, Any]) -> str:
    memory_id = row.get("memory_id")
    if memory_id:
        return str(memory_id)
    return compact(f"{source_title(row)}|{row.get('statement')}", 400)


def row_people(row: dict[str, Any]) -> list[str]:
    people = row.get("people") if isinstance(row.get("people"), list) else []
    statement = str(row.get("statement") or row.get("evidence") or "")
    title = source_title(row)
    row_text = f"{title}\n{statement}"
    output: list[str] = []
    for person in people:
        raw_name = str(person)
        name = canonical_name(raw_name)
        if not name or name in NON_PERSON_ENTITIES:
            continue
        if name == BIBI_CANONICAL and BIBI_BRAND_CONTEXT_RE.search(row_text) and not BIBI_PERSON_CONTEXT_RE.search(row_text):
            continue
        if name == BIBI_CANONICAL and MAMA_ROLE_RE.search(statement):
            continue
        if name not in output:
            output.append(name)
    if MAMA_ROLE_RE.search(statement) or "协作者C" in statement or "协作者C" in row_text:
        if MAMA_CANONICAL not in output:
            output.append(MAMA_CANONICAL)
    if "用户本人" in row_text and USER_CANONICAL not in output:
        output.append(USER_CANONICAL)
    return output


def row_score(row: dict[str, Any], origin_weight: float) -> float:
    score = origin_weight
    score += float(row.get("confidence") or 0) * 1.2
    score += float(row.get("relevance_score") or 0)
    score += TYPE_WEIGHT.get(str(row.get("memory_type") or ""), 0.0)
    if is_low_signal(row):
        score -= 1.35
    if row.get("focus") == "self_profile":
        score += 0.25
    if source_kind(row) == "feishu-doc-content":
        score += 0.08
    return round(score, 4)


def confidence_for_person(person: str, rows: list[dict[str, Any]], highlights: list[dict[str, Any]]) -> tuple[str, str]:
    high_signal = [
        row for row in rows
        if not is_low_signal(row)
    ]
    reviewed_rows = [row for row in rows if row.get("_origin") == "reviewed_profile"]
    if person == "错误账号" and len(high_signal) < 3:
        return "low", "证据偏少：已按同事保留为人物线索，系统会在后续采集和蒸馏中自动补强。"
    if len(rows) <= 2 or len(high_signal) == 0:
        return "low", "证据偏少：目前只保留为人物线索，后续自动采集到更多高质量协作记录后再稳定建档。"
    if len(high_signal) < 3 and not reviewed_rows:
        return "medium", "证据中等：已有可用线索，但仍需要更多高质量记录补强。"
    return "high", ""


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0)
    return text[:10]


def collect_rows() -> tuple[list[dict[str, Any]], Counter[str]]:
    by_key: dict[str, dict[str, Any]] = {}
    counters: Counter[str] = Counter()
    for path, origin, weight in MEMORY_SOURCES:
        for row in load_jsonl(path):
            people = row_people(row)
            if not people:
                continue
            key = row_key(row)
            candidate = dict(row)
            candidate["_origin"] = origin
            candidate["_origin_path"] = str(path)
            candidate["_origin_weight"] = weight
            candidate["_people_canonical"] = people
            candidate["_score"] = row_score(candidate, weight)
            previous = by_key.get(key)
            if not previous or candidate["_score"] > previous["_score"]:
                by_key[key] = candidate
            counters[f"source:{origin}"] += 1
    return list(by_key.values()), counters


def choose_highlights(rows: list[dict[str, Any]], person: str, limit: int = 8) -> list[dict[str, Any]]:
    highlights = []
    fallback = []
    seen = set()
    for row in sorted(rows, key=lambda item: item.get("_score", 0), reverse=True):
        statement = compact(row.get("statement") or row.get("evidence") or "")
        if not statement or statement in seen:
            continue
        if is_representative_noise(statement):
            fallback.append(
                {
                    "memory_id": str(row.get("memory_id") or row_key(row)),
                    "statement": statement,
                    "memory_type": row.get("memory_type") or "",
                    "memory_type_label": TYPE_LABELS.get(str(row.get("memory_type") or ""), str(row.get("memory_type") or "")),
                    "focus": row.get("focus") or "",
                    "valid_from": normalize_date(row.get("valid_from")),
                    "source_title": compact(source_title(row), 120),
                    "source_kind": source_kind(row),
                    "source_url": source_url(row),
                    "origin": row.get("_origin") or "",
                    "origin_label": SOURCE_LAYER_LABELS.get(str(row.get("_origin") or ""), str(row.get("_origin") or "")),
                    "score": row.get("_score") or 0,
                }
            )
            continue
        if any(term in statement for term in NON_PERSON_ENTITIES):
            continue
        seen.add(statement)
        item = {
            "memory_id": str(row.get("memory_id") or row_key(row)),
            "statement": statement,
            "memory_type": row.get("memory_type") or "",
            "memory_type_label": TYPE_LABELS.get(str(row.get("memory_type") or ""), str(row.get("memory_type") or "")),
            "focus": row.get("focus") or "",
            "valid_from": normalize_date(row.get("valid_from")),
            "source_title": compact(source_title(row), 120),
            "source_kind": source_kind(row),
            "source_url": source_url(row),
            "origin": row.get("_origin") or "",
            "origin_label": SOURCE_LAYER_LABELS.get(str(row.get("_origin") or ""), str(row.get("_origin") or "")),
            "score": row.get("_score") or 0,
        }
        if is_low_signal(row):
            fallback.append(item)
            continue
        highlights.append(item)
        if len(highlights) >= limit:
            break
    if not highlights:
        highlights = fallback[: min(limit, 3)]
    return highlights


def intro_for_person(
    person: str,
    memory_count: int,
    project_counts: Counter[str],
    type_counts: Counter[str],
    highlights: list[dict[str, Any]],
    confidence: str = "high",
) -> str:
    base = KNOWN_PERSON_HINTS.get(person)
    top_projects = [
        PROJECT_LABELS.get(project, project)
        for project, _count in project_counts.most_common(4)
        if project and project != "general"
    ]
    top_types = [TYPE_LABELS.get(kind, kind) for kind, _count in type_counts.most_common(3) if kind]
    if not base:
        if top_projects:
            base = f"记忆库中与 {person} 相关的记录主要集中在{ '、'.join(top_projects) }。"
        else:
            base = f"记忆库中有 {memory_count} 条与 {person} 相关的结构化记录。"
    details = []
    if top_projects:
        details.append(f"关联主题：{'、'.join(top_projects)}")
    if top_types:
        details.append(f"主要信息类型：{'、'.join(top_types)}")
    if details:
        base = base.rstrip("。") + "。" + "；".join(details) + "。"
    if confidence == "low":
        if highlights:
            first = highlights[0]
            source = first.get("source_title") or "未知来源"
            date = first.get("valid_from") or "未知日期"
            base += f" 当前线索：{date} 在「{source}」中被提及，系统会继续自动补证据。"
        return compact(base, 420)
    return compact(base, 360)


def count_label(items: list[dict[str, Any]], limit: int = 3) -> str:
    labels = [
        str(item.get("label") or item.get("name") or item.get("id") or "").strip()
        for item in items[:limit]
    ]
    return "、".join(label for label in labels if label)


def evidence_maturity_text(confidence: str, confidence_label: str, memory_count: int, source_layers: list[dict[str, Any]]) -> str:
    layer_text = count_label(source_layers, 2)
    if confidence == "high":
        suffix = f"，来源覆盖 {layer_text}" if layer_text else ""
        return f"稳定档案：已有 {memory_count} 条结构化记忆{suffix}。"
    if confidence == "medium":
        reason = confidence_label or "已有可用线索，但仍需要更多高质量记录补强。"
        return f"继续补强：{reason}"
    reason = confidence_label or "目前只保留为人物线索，后续自动补证据。"
    return f"自动补证据：{reason}"


def infer_role_summary(person: str, category: str, top_projects: list[dict[str, Any]], memory_types: list[dict[str, Any]]) -> str:
    override = PROFILE_OVERRIDES.get(person, {})
    if override.get("role_summary"):
        return str(override["role_summary"])
    category_label = CATEGORY_LABELS.get(category, "记忆库人物")
    projects = count_label(top_projects, 3)
    types = count_label(memory_types, 2)
    if projects and types:
        return f"{category_label}，主要出现在{projects}相关记录里，信息类型集中在{types}。"
    if projects:
        return f"{category_label}，主要出现在{projects}相关记录里。"
    return KNOWN_PERSON_HINTS.get(person) or f"{category_label}，记忆库中已有与 {person} 相关的结构化线索。"


def infer_relationship_to_user(person: str, category: str, co_mentions: list[dict[str, Any]]) -> str:
    override = PROFILE_OVERRIDES.get(person, {})
    if override.get("relationship_to_user"):
        return str(override["relationship_to_user"])
    if person == USER_CANONICAL:
        return "用户本人；所有长期画像都围绕这个主体建立。"
    co_names = {str(item.get("name") or "") for item in co_mentions}
    if USER_CANONICAL in co_names:
        if category == "customer":
            return "与用户本人存在客户/项目方关系，已有共同项目或方案沟通记录。"
        if category == "business":
            return "与用户本人存在业务协作关系，记录中有内容、客户或资源协作。"
        if category == "team":
            return "与用户本人存在内部协作关系，记录中有职责、执行或项目配合。"
        return "与用户本人有直接共现记录，后续自动补足具体关系。"
    if category == "team":
        return "团队同事或内部协作成员；当前与用户本人的直接关系证据仍在补强。"
    if category == "customer":
        return "外部客户或项目方；当前以需求、沟通和交付线索归档。"
    return "记忆库中的人物线索；系统会继续从新增语料中补足与用户本人的关系。"


def infer_work_context(person: str, top_projects: list[dict[str, Any]], memory_types: list[dict[str, Any]], highlights: list[dict[str, Any]]) -> str:
    override = PROFILE_OVERRIDES.get(person, {})
    if override.get("work_context"):
        return str(override["work_context"])
    projects = count_label(top_projects, 4)
    types = count_label(memory_types, 3)
    parts = []
    if projects:
        parts.append(f"关联项目：{projects}")
    if types:
        parts.append(f"主要信息类型：{types}")
    if highlights:
        first = highlights[0]
        source = first.get("source_title") or "未知来源"
        date = first.get("valid_from") or "未知日期"
        parts.append(f"最近高信号来源：{date}「{source}」")
    return "；".join(parts) + "。" if parts else "当前上下文仍偏少，等待后续自动补证据。"


def infer_current_line(person: str, confidence: str, confidence_label: str, highlights: list[dict[str, Any]]) -> str:
    override = PROFILE_OVERRIDES.get(person, {})
    if override.get("current_line"):
        return str(override["current_line"])
    if confidence == "low":
        if highlights:
            first = highlights[0]
            source = first.get("source_title") or "未知来源"
            date = first.get("valid_from") or "未知日期"
            return f"当前判断：{date} 在「{source}」中出现过，先作为线索档案保留，后续自动补证据。"
        return "当前判断：证据偏少，先保留人物线索，后续自动补证据。"
    if confidence == "medium":
        return f"当前判断：{confidence_label or '已有可用线索，但还不适合过度下结论。'}"
    return "当前判断：已形成稳定人物档案，可作为看板和上下文召回的默认人物结论。"


def build_profile(
    person: str,
    category: str,
    confidence: str,
    confidence_label: str,
    memory_count: int,
    top_projects: list[dict[str, Any]],
    memory_types: list[dict[str, Any]],
    source_layers: list[dict[str, Any]],
    co_mentions: list[dict[str, Any]],
    highlights: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "role_summary": compact(infer_role_summary(person, category, top_projects, memory_types), 220),
        "relationship_to_user": compact(infer_relationship_to_user(person, category, co_mentions), 220),
        "work_context": compact(infer_work_context(person, top_projects, memory_types, highlights), 260),
        "evidence_maturity": compact(evidence_maturity_text(confidence, confidence_label, memory_count, source_layers), 220),
        "current_line": compact(infer_current_line(person, confidence, confidence_label, highlights), 260),
    }


def top_counter_items(counter: Counter[str], labels: dict[str, str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
    labels = labels or {}
    return [
        {
            "id": key,
            "label": labels.get(key, key),
            "count": count,
        }
        for key, count in counter.most_common(limit)
    ]


def rows_by_type(rows: list[dict[str, Any]], memory_types: set[str], limit: int = 6) -> list[dict[str, Any]]:
    selected = []
    seen = set()
    for row in sorted(rows, key=lambda item: item.get("_score", 0), reverse=True):
        kind = str(row.get("memory_type") or "")
        if kind not in memory_types:
            continue
        statement = compact(row.get("statement") or row.get("evidence") or "")
        if not statement or statement in seen or is_low_signal(row):
            continue
        seen.add(statement)
        selected.append(
            {
                "memory_id": str(row.get("memory_id") or row_key(row)),
                "statement": statement,
                "memory_type": kind,
                "memory_type_label": TYPE_LABELS.get(kind, kind),
                "valid_from": normalize_date(row.get("valid_from")),
                "source_title": compact(source_title(row), 120),
                "source_kind": source_kind(row),
                "source_url": source_url(row),
                "origin": row.get("_origin") or "",
                "origin_label": SOURCE_LAYER_LABELS.get(str(row.get("_origin") or ""), str(row.get("_origin") or "")),
                "score": row.get("_score") or 0,
            }
        )
        if len(selected) >= limit:
            break
    return selected


def recent_rows(rows: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    dated = [row for row in rows if normalize_date(row.get("valid_from"))]
    dated.sort(key=lambda item: (normalize_date(item.get("valid_from")), item.get("_score", 0)), reverse=True)
    selected = []
    seen = set()
    for row in dated:
        statement = compact(row.get("statement") or row.get("evidence") or "")
        if not statement or statement in seen or is_low_signal(row):
            continue
        seen.add(statement)
        kind = str(row.get("memory_type") or "")
        selected.append(
            {
                "memory_id": str(row.get("memory_id") or row_key(row)),
                "date": normalize_date(row.get("valid_from")),
                "statement": statement,
                "memory_type": kind,
                "memory_type_label": TYPE_LABELS.get(kind, kind),
                "source_title": compact(source_title(row), 120),
                "source_kind": source_kind(row),
                "source_url": source_url(row),
                "origin": row.get("_origin") or "",
                "origin_label": SOURCE_LAYER_LABELS.get(str(row.get("_origin") or ""), str(row.get("_origin") or "")),
            }
        )
        if len(selected) >= limit:
            break
    return selected


def build_sections(
    person: str,
    intro: str,
    profile: dict[str, Any],
    aliases: list[str],
    memory_count: int,
    latest_date: str,
    top_projects: list[dict[str, Any]],
    memory_types: list[dict[str, Any]],
    source_layers: list[dict[str, Any]],
    co_mentions: list[dict[str, Any]],
    highlights: list[dict[str, Any]],
    person_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decision_items = rows_by_type(person_rows, {"decision", "preference", "lesson"}, limit=6)
    recent_items = recent_rows(person_rows, limit=6)
    relationship_items = rows_by_type(person_rows, {"relationship"}, limit=4)
    if not relationship_items:
        relationship_items = highlights[:3]

    overview_sections = [
        {
            "id": "basic_intro",
            "title": "基本介绍",
            "kind": "summary",
            "text": profile.get("role_summary") or intro,
            "facts": [
                {"label": "类别", "value": CATEGORY_BY_NAME.get(person, "other")},
                {"label": "记忆数", "value": memory_count},
                {"label": "最近证据", "value": latest_date},
            ],
        },
        {
            "id": "profile_brief",
            "title": "档案结论",
            "kind": "profile_brief",
            "items": [
                {"label": "与用户本人关系", "value": profile.get("relationship_to_user") or ""},
                {"label": "工作上下文", "value": profile.get("work_context") or ""},
                {"label": "当前判断", "value": profile.get("current_line") or ""},
            ],
        },
        {
            "id": "relationships",
            "title": "关系",
            "kind": "people",
            "items": co_mentions,
            "evidence": relationship_items,
        },
        {
            "id": "related_projects",
            "title": "关联项目",
            "kind": "tags",
            "items": top_projects,
        },
        {
            "id": "representative_memories",
            "title": "代表记忆",
            "kind": "memories",
            "items": highlights[:5],
        },
    ]

    detail_sections = [
        {
            "id": "basic_intro",
            "title": "基本介绍",
            "kind": "profile",
            "text": profile.get("role_summary") or intro,
            "aliases": aliases,
            "category": CATEGORY_BY_NAME.get(person, "other"),
            "memory_count": memory_count,
            "latest_date": latest_date,
        },
        {
            "id": "profile",
            "title": "人物档案",
            "kind": "profile_fields",
            "fields": profile,
        },
        {
            "id": "relationships",
            "title": "关系",
            "kind": "people",
            "items": co_mentions,
            "evidence": relationship_items,
        },
        {
            "id": "related_projects",
            "title": "关联项目",
            "kind": "projects",
            "items": top_projects,
        },
        {
            "id": "important_decisions",
            "title": "重要决策",
            "kind": "memories",
            "items": decision_items,
        },
        {
            "id": "recent_updates",
            "title": "最近动态",
            "kind": "timeline",
            "items": recent_items,
        },
        {
            "id": "evidence_sources",
            "title": "证据来源/代表记忆",
            "kind": "sources",
            "source_layers": source_layers,
            "memory_types": memory_types,
            "items": highlights,
        },
    ]
    return overview_sections, detail_sections


def build_people_index(min_count: int = 1) -> dict[str, Any]:
    rows, counters = collect_rows()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for person in row.get("_people_canonical") or []:
            grouped[person].append(row)

    people = []
    for person, person_rows in grouped.items():
        if len(person_rows) < min_count:
            continue
        project_counts: Counter[str] = Counter()
        type_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        co_mentions: Counter[str] = Counter()
        dates = []
        aliases = []
        seen_aliases = set()

        def add_alias(alias: str) -> None:
            alias = str(alias or "").strip()
            if alias and alias not in seen_aliases:
                seen_aliases.add(alias)
                aliases.append(alias)

        for alias in CANONICAL_ALIASES.get(person, []):
            add_alias(alias)
        for row in person_rows:
            for raw_person in row.get("people") or []:
                if canonical_name(str(raw_person)) == person:
                    add_alias(str(raw_person))
            row_text = f"{source_title(row)}\n{row.get('statement') or row.get('evidence') or ''}"
            if person == MAMA_CANONICAL:
                for alias in CANONICAL_ALIASES[MAMA_CANONICAL]:
                    if alias in row_text:
                        add_alias(alias)
            for project in row.get("projects") or []:
                project_counts[str(project)] += 1
            if row.get("memory_type"):
                type_counts[str(row.get("memory_type"))] += 1
            if row.get("_origin"):
                source_counts[str(row.get("_origin"))] += 1
            if row.get("valid_from"):
                dates.append(normalize_date(row.get("valid_from")))
            for other in row.get("_people_canonical") or []:
                if other != person:
                    co_mentions[other] += 1
        highlights = choose_highlights(person_rows, person)
        latest_date = max(dates) if dates else ""
        top_projects = top_counter_items(project_counts, PROJECT_LABELS, 8)
        memory_types = top_counter_items(type_counts, TYPE_LABELS, 8)
        source_layers = top_counter_items(source_counts, SOURCE_LAYER_LABELS, 6)
        co_mention_items = [
            {"name": name, "count": count}
            for name, count in co_mentions.most_common(8)
        ]
        confidence, confidence_label = confidence_for_person(person, person_rows, highlights)
        intro = intro_for_person(person, len(person_rows), project_counts, type_counts, highlights, confidence)
        category = CATEGORY_BY_NAME.get(person, "other")
        profile = build_profile(
            person=person,
            category=category,
            confidence=confidence,
            confidence_label=confidence_label,
            memory_count=len(person_rows),
            top_projects=top_projects,
            memory_types=memory_types,
            source_layers=source_layers,
            co_mentions=co_mention_items,
            highlights=highlights,
        )
        overview_sections, detail_sections = build_sections(
            person=person,
            intro=intro,
            profile=profile,
            aliases=aliases,
            memory_count=len(person_rows),
            latest_date=latest_date,
            top_projects=top_projects,
            memory_types=memory_types,
            source_layers=source_layers,
            co_mentions=co_mention_items,
            highlights=highlights,
            person_rows=person_rows,
        )
        people.append(
            {
                "name": person,
                "aliases": aliases,
                "category": category,
                "memory_count": len(person_rows),
                "latest_date": latest_date,
                "confidence": confidence,
                "confidence_label": confidence_label,
                "profile": profile,
                "intro": intro,
                "top_projects": top_projects,
                "memory_types": memory_types,
                "source_layers": source_layers,
                "co_mentions": co_mention_items,
                "highlights": highlights,
                "overview_sections": overview_sections,
                "detail_sections": detail_sections,
            }
        )

    category_rank = {"self": 0, "team": 1, "business": 2, "customer": 3, "other": 4}
    people.sort(key=lambda item: (category_rank.get(item["category"], 9), -item["memory_count"], item["name"]))
    return {
        "version": "0.1",
        "generated_at": now_local(),
        "basis": "distilled_structured_memory_layers",
        "source_files": [str(path) for path, _origin, _weight in MEMORY_SOURCES],
        "counters": dict(counters),
        "people": people,
    }


def render_markdown(index: dict[str, Any]) -> str:
    lines = [
        "# People Index",
        "",
        f"Generated: {index.get('generated_at')}",
        "",
        "这个文件是看板的人物档案层，由结构化记忆自动生成。",
        "",
    ]
    for person in index.get("people") or []:
        lines.append(f"## {person['name']}")
        lines.append("")
        lines.append(f"- 类别：{person.get('category')}")
        lines.append(f"- 记忆数：{person.get('memory_count')}")
        if person.get("latest_date"):
            lines.append(f"- 最近日期：{person.get('latest_date')}")
        if person.get("aliases"):
            lines.append(f"- 别名：{'、'.join(person.get('aliases') or [])}")
        profile = person.get("profile") if isinstance(person.get("profile"), dict) else {}
        if profile:
            lines.append(f"- 角色摘要：{profile.get('role_summary') or person.get('intro')}")
            lines.append(f"- 与用户本人关系：{profile.get('relationship_to_user') or '-'}")
            lines.append(f"- 工作上下文：{profile.get('work_context') or '-'}")
            lines.append(f"- 证据成熟度：{profile.get('evidence_maturity') or '-'}")
            lines.append(f"- 当前判断：{profile.get('current_line') or '-'}")
        else:
            lines.append(f"- 介绍：{person.get('intro')}")
        if person.get("highlights"):
            lines.append("- 代表性记忆：")
            for item in person["highlights"][:5]:
                lines.append(
                    f"  - {item.get('statement')}（{item.get('valid_from') or '-'} / {item.get('source_title') or '-'}）"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(index: dict[str, Any], output_json: Path, output_md: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    tmp_json = output_json.with_suffix(output_json.suffix + ".tmp")
    tmp_json.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_json.replace(output_json)
    tmp_md = output_md.with_suffix(output_md.suffix + ".tmp")
    tmp_md.write_text(render_markdown(index), encoding="utf-8")
    tmp_md.replace(output_md)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build person-facing memory index")
    parser.add_argument("--output-json", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--output-md", type=Path, default=OUTPUT_MD)
    parser.add_argument("--min-count", type=int, default=1)
    parser.add_argument("--json", action="store_true", help="Print generated JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    index = build_people_index(min_count=args.min_count)
    write_outputs(index, args.output_json, args.output_md)
    print(f"people={len(index.get('people') or [])}")
    print(f"output_json={args.output_json}")
    print(f"output_md={args.output_md}")
    if args.json:
        print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
