#!/usr/bin/env python3
"""Build a read-only quality report for the Immortal memory vault.

The report is intentionally a derived layer. It does not edit profile,
people, relationship, or raw Feishu artifacts. Its job is to surface where the
memory base may be split, contaminated, or overconfident.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import people_index as people_layer


HOME = Path.home()
IMMORTAL_DIR = HOME / ".immortal"
PEOPLE_INDEX_FILE = IMMORTAL_DIR / "people" / "people_index.json"
RELATIONSHIP_INDEX_FILE = IMMORTAL_DIR / "relationships" / "relationship_index.json"
OUTPUT_DIR = IMMORTAL_DIR / "quality"
OUTPUT_JSON = OUTPUT_DIR / "latest.json"
OUTPUT_MD = OUTPUT_DIR / "latest.md"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

MEMORY_LAYERS = [
    (IMMORTAL_DIR / "reviewed" / "profile_memories.jsonl", "reviewed_profile"),
    (IMMORTAL_DIR / "feishu" / "distilled" / "profile_memories.jsonl", "profile_memory"),
    (IMMORTAL_DIR / "feishu" / "distilled" / "reference_memories.jsonl", "reference_memory"),
    (IMMORTAL_DIR / "feishu" / "distilled" / "memories.jsonl", "distilled_memory"),
]

USER_CANONICAL = people_layer.USER_CANONICAL
BIBI_CANONICAL = people_layer.BIBI_CANONICAL
TAOZI_CANONICAL = people_layer.TAOZI_CANONICAL
MAMA_CANONICAL = people_layer.MAMA_CANONICAL
SHUSHU_CANONICAL = "错误账号"

CONFIRMED_IDENTITY_RULES = [
    {
        "id": "user",
        "canonical": USER_CANONICAL,
        "aliases": ["用户本人", "用户本人", "Configured User", "用户本人"],
        "expected_category": "self",
        "must_exist": True,
    },
    {
        "id": "partner",
        "canonical": BIBI_CANONICAL,
        "aliases": ["协作账号", "协作者A", "协作账号"],
        "expected_not_category": "self",
        "must_exist": True,
    },
    {
        "id": "taozi",
        "canonical": TAOZI_CANONICAL,
        "aliases": ["协作者B", "协作者B"],
        "expected_not_category": "self",
        "must_exist": True,
    },
    {
        "id": "mama",
        "canonical": MAMA_CANONICAL,
        "aliases": ["协作者C", "协作者C", "同事代理账号"],
        "expected_not_category": "self",
        "must_exist": True,
    },
    {
        "id": "shushu",
        "canonical": SHUSHU_CANONICAL,
        "aliases": ["错误账号", "错误账号", "错误账号"],
        "expected_not_category": "self",
        "must_exist": True,
    },
]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
STATUS_LABELS = {
    "ok": "可用",
    "attention": "需要关注",
    "warn": "输入不完整",
}
PROFILE_CONTAMINATION_RE = re.compile(
    r"(候选人|简历|面试|录用决定|协作账号账号|协作账号账号|公众号矩阵|第三方观点|"
    r"给直接管理者|客户无法拒绝的报价方案|送别视频|口播脚本|游戏通关视频)"
)
FIRST_PERSON_RE = re.compile(r"(^|[。！？\n])\s*(我|我们|咱们)")
OTHER_SUBJECT_RE = re.compile(
    r"(^|\n|\s)(协作者A|协作账号|协作者L|协作者L|郭协作者H|协作者H|协作者I|候选人)"
    r"(认为|指出|提出|表示|反馈|负责|用|的|在|将|需|需要|奖金|签字)"
)
ORG_FACT_RE = re.compile(
    r"^(参赛要求|工程质量|一等奖|AI 产品提成|热点 skill 开发|问题表现|解决方案|稀缺内容考量|offer 合同)"
)
KEYWORD_ONLY_RE = re.compile(r"(`[^`]+`\s*){5,}|([、,，]\s*){8,}")
VERB_HINT_RE = re.compile(
    r"(负责|承接|主导|对接|支持|协助|决定|要求|提出|反馈|交付|培训|运维|报价|确权|审稿|管理|培养)"
)


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
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def compact(text: Any, limit: int = 180) -> str:
    return people_layer.compact(str(text or ""), limit)


def int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def row_text(row: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(row.get("statement") or row.get("evidence") or ""),
            people_layer.source_title(row),
        ]
    )


def source_key(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("url") or source.get("title") or source.get("raw_id") or people_layer.row_key(row))


def row_sample(row: dict[str, Any], *, limit: int = 150) -> dict[str, Any]:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return {
        "memory_id": str(row.get("memory_id") or people_layer.row_key(row)),
        "statement": compact(row.get("statement") or row.get("evidence") or "", limit),
        "source_title": compact(source.get("title") or "unknown source", 90),
        "source_kind": str(source.get("source") or ""),
        "origin": str(row.get("_origin") or ""),
    }


def reviewed_self_profile_contamination_reason(
    row: dict[str, Any],
    *,
    text: str,
    canonical_people: list[str],
) -> str:
    if row.get("_origin") != "reviewed_profile" or str(row.get("focus") or "") != "self_profile":
        return ""
    statement = str(row.get("statement") or row.get("evidence") or "").strip()
    if FIRST_PERSON_RE.search(statement):
        return ""
    has_user = USER_CANONICAL in canonical_people or any(alias in text for alias in people_layer.USER_ALIASES)
    projects = set(str(project) for project in row.get("projects") or [])
    if OTHER_SUBJECT_RE.search(statement):
        return "other_person_subject"
    if ORG_FACT_RE.search(statement):
        return "org_or_event_fact"
    if (("partner_brand" in projects or PROFILE_CONTAMINATION_RE.search(text)) and not has_user):
        return "brand_or_third_party_context_without_owner"
    return ""


def edge_sample(edge: dict[str, Any]) -> dict[str, Any]:
    evidence = edge.get("evidence") if isinstance(edge.get("evidence"), list) else []
    first = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
    return {
        "edge_id": str(edge.get("id") or ""),
        "source": str(edge.get("source_label") or edge.get("source") or ""),
        "target": str(edge.get("target_label") or edge.get("target") or ""),
        "kind": str(edge.get("kind") or ""),
        "relation_type": str(edge.get("relation_type") or ""),
        "relation_label": str(edge.get("relation_label") or edge.get("label") or ""),
        "count": int_value(edge.get("count")),
        "score": round(float_value(edge.get("score")), 4),
        "latest_date": str(edge.get("latest_date") or ""),
        "statement": compact(first.get("statement") or "", 150),
        "source_title": compact(first.get("source_title") or "", 90),
    }


def make_issue(
    issues: list[dict[str, Any]],
    *,
    issue_id: str,
    area: str,
    severity: str,
    title: str,
    detail: str,
    suggested_action: str,
    evidence: list[dict[str, Any]] | None = None,
) -> None:
    issues.append(
        {
            "id": issue_id,
            "area": area,
            "severity": severity,
            "title": title,
            "detail": detail,
            "suggested_action": suggested_action,
            "evidence": evidence or [],
        }
    )


def make_observation(
    observations: list[dict[str, Any]],
    *,
    observation_id: str,
    area: str,
    severity: str,
    title: str,
    detail: str,
    suggested_action: str,
    evidence: list[dict[str, Any]] | None = None,
) -> None:
    observations.append(
        {
            "id": observation_id,
            "area": area,
            "severity": severity,
            "title": title,
            "detail": detail,
            "suggested_action": suggested_action,
            "evidence": evidence or [],
        }
    )


def load_memory_layers() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sources = {}
    seen: set[str] = set()
    for path, origin in MEMORY_LAYERS:
        layer_rows = load_jsonl(path)
        sources[origin] = {
            "path": str(path),
            "exists": path.exists(),
            "rows": len(layer_rows),
        }
        for row in layer_rows:
            key = people_layer.row_key(row)
            row = dict(row)
            row["_origin"] = origin
            row["_origin_path"] = str(path)
            dedupe_key = f"{origin}:{key}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rows.append(row)
    return rows, sources


def check_identity_rules(people: list[dict[str, Any]], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    people_by_name = {str(person.get("name") or ""): person for person in people if isinstance(person, dict)}
    raw_names = set(people_by_name)

    self_people = [person for person in people if person.get("category") == "self"]
    if len(self_people) != 1:
        make_issue(
            issues,
            issue_id="self_person_count",
            area="identity",
            severity="high",
            title="用户本人档案数量异常",
            detail=f"当前 category=self 的人物数量为 {len(self_people)}，应只有 1 个。",
            suggested_action="检查 people_index.py 的 CATEGORY_BY_NAME 和别名归一规则。",
            evidence=[{"name": str(person.get("name") or "")} for person in self_people[:8]],
        )

    for rule in CONFIRMED_IDENTITY_RULES:
        canonical = rule["canonical"]
        person = people_by_name.get(canonical)
        if rule.get("must_exist") and not person:
            make_issue(
                issues,
                issue_id=f"missing_confirmed_person:{rule['id']}",
                area="identity",
                severity="high" if rule["id"] == "user" else "medium",
                title=f"确认人物缺失：{canonical}",
                detail="用户已经确认过这个人物或别名组，人物索引里应该保留。",
                suggested_action="重跑 people index；若仍缺失，检查 people_index.py 的 ALIASES/CANONICAL_ALIASES。",
            )
            continue

        split_aliases = [alias for alias in rule["aliases"] if alias in raw_names and alias != canonical]
        if split_aliases:
            make_issue(
                issues,
                issue_id=f"alias_split:{rule['id']}",
                area="identity",
                severity="high",
                title=f"别名被拆成人物：{canonical}",
                detail="同一个人出现了独立人物卡，会污染人物档案和关系图谱。",
                suggested_action="把这些别名统一归入 canonical，再重建 people/relationships。",
                evidence=[{"alias": alias} for alias in split_aliases],
            )

        if not person:
            continue
        aliases = set(str(alias) for alias in person.get("aliases") or [])
        missing_aliases = [alias for alias in rule["aliases"] if alias not in aliases]
        if missing_aliases:
            make_issue(
                issues,
                issue_id=f"missing_alias_display:{rule['id']}",
                area="identity",
                severity="medium",
                title=f"人物卡别名不完整：{canonical}",
                detail="已确认别名没有完整显示，会降低后续人工检查和搜索可读性。",
                suggested_action="补齐 CANONICAL_ALIASES 并重建人物索引。",
                evidence=[{"missing_alias": alias} for alias in missing_aliases],
            )

        category = str(person.get("category") or "")
        if rule.get("expected_category") and category != rule["expected_category"]:
            make_issue(
                issues,
                issue_id=f"wrong_category:{rule['id']}",
                area="identity",
                severity="high",
                title=f"人物分类异常：{canonical}",
                detail=f"当前 category={category or 'missing'}，预期为 {rule['expected_category']}。",
                suggested_action="修正 CATEGORY_BY_NAME 后重建人物索引。",
            )
        if rule.get("expected_not_category") and category == rule["expected_not_category"]:
            make_issue(
                issues,
                issue_id=f"not_user_marked_self:{rule['id']}",
                area="identity",
                severity="high",
                title=f"非用户人物被标成用户本人：{canonical}",
                detail="这会直接污染长期画像和看板主语。",
                suggested_action="修正 CATEGORY_BY_NAME，并用 profile-auto-review 重新剪掉旧污染。",
            )

        if canonical != USER_CANONICAL and int_value(person.get("memory_count")) <= 2:
            make_observation(
                observations,
                observation_id=f"low_evidence_confirmed:{rule['id']}",
                area="people",
                severity="low",
                title=f"确认人物证据偏少：{canonical}",
                detail=f"当前只有 {int_value(person.get('memory_count'))} 条人物相关记忆，先保留为低置信档案。",
                suggested_action="继续通过自动采集补证据，不需要用户手动审阅。",
            )

    user_alias_misses = []
    mama_misses = []
    partner_brand_hits = []
    contamination_hits = []
    for row in rows:
        text = row_text(row)
        canonical_people = people_layer.row_people(row)
        raw_people = [str(person).strip().lstrip("@") for person in row.get("people") or []]
        if "用户本人" in text and USER_CANONICAL not in canonical_people:
            user_alias_misses.append(row_sample(row))
        if people_layer.MAMA_ROLE_RE.search(text) and MAMA_CANONICAL not in canonical_people:
            mama_misses.append(row_sample(row))
        if (
            people_layer.BIBI_BRAND_CONTEXT_RE.search(text)
            and BIBI_CANONICAL in canonical_people
            and not people_layer.BIBI_PERSON_CONTEXT_RE.search(text)
        ):
            partner_brand_hits.append(row_sample(row))
        contamination_reason = reviewed_self_profile_contamination_reason(
            row,
            text=text,
            canonical_people=canonical_people,
        )
        if contamination_reason:
            sample = row_sample(row)
            sample["reason"] = contamination_reason
            contamination_hits.append(sample)
        if "同事代理账号" in raw_people and MAMA_CANONICAL not in canonical_people:
            mama_misses.append(row_sample(row))

    if user_alias_misses:
        make_issue(
            issues,
            issue_id="user_alias_extraction_miss:user",
            area="identity",
            severity="medium",
            title="用户本人别名可能漏抽取",
            detail=f"发现 {len(user_alias_misses)} 条文本包含“用户本人”，但人物字段没有归到用户本人。",
            suggested_action="补强 Feishu 蒸馏层人物抽取词表，或在 people_index row_people 中兜底识别。",
            evidence=user_alias_misses[:6],
        )
    if mama_misses:
        make_issue(
            issues,
            issue_id="mama_role_alias_miss",
            area="identity",
            severity="high",
            title="同事代理账号没有归到协作者C",
            detail=f"发现 {len(mama_misses)} 条商务代理语境没有进入协作者C档案。",
            suggested_action="优先检查 people_index.py 的 MAMA_ROLE_RE 和 row_people 规则。",
            evidence=mama_misses[:6],
        )
    if partner_brand_hits:
        make_issue(
            issues,
            issue_id="partner_brand_as_person",
            area="identity",
            severity="medium",
            title="协作账号品牌语境可能被当成人",
            detail=f"发现 {len(partner_brand_hits)} 条账号/品牌语境仍进入协作账号人物证据。",
            suggested_action="继续收紧 BIBI_BRAND_CONTEXT_RE，不把账号素材当作协作账号本人行为。",
            evidence=partner_brand_hits[:6],
        )
    if contamination_hits:
        make_issue(
            issues,
            issue_id="reviewed_profile_contamination",
            area="profile",
            severity="high",
            title="长期画像存在第三方或协作账号账号污染风险",
            detail=f"发现 {len(contamination_hits)} 条 reviewed self_profile 缺少用户主语，但含第三方/账号语境。",
            suggested_action="用 profile-auto-review 重新剪掉不符合当前归因规则的 reviewed rows。",
            evidence=contamination_hits[:6],
        )

    metrics = {
        "people_count": len(people),
        "self_count": len(self_people),
        "confirmed_rules": len(CONFIRMED_IDENTITY_RULES),
        "user_alias_miss_count": len(user_alias_misses),
        "mama_role_miss_count": len(mama_misses),
        "partner_brand_ambiguity_count": len(partner_brand_hits),
        "reviewed_profile_contamination_count": len(contamination_hits),
    }
    return issues, observations, metrics


def check_relationships(index: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    edges = index.get("edges") if isinstance(index.get("edges"), list) else []
    summary = index.get("summary") if isinstance(index.get("summary"), dict) else {}
    person_edges = [edge for edge in edges if isinstance(edge, dict) and edge.get("kind") == "person_person"]
    project_edges = [edge for edge in edges if isinstance(edge, dict) and edge.get("kind") == "person_project"]
    weak_edges = [edge for edge in edges if isinstance(edge, dict) and edge.get("relation_type") == "co_mention_weak"]
    top_person_score = max((float_value(edge.get("score")) for edge in person_edges), default=0.0)
    top_project_score = max((float_value(edge.get("score")) for edge in project_edges), default=0.0)
    top_weak_score = max((float_value(edge.get("score")) for edge in weak_edges), default=0.0)
    ratio = round(top_project_score / top_person_score, 2) if top_person_score else 0.0

    weak_threshold = max(80.0, top_person_score * 0.8)
    weak_high = sorted(
        [edge for edge in weak_edges if float_value(edge.get("score")) >= weak_threshold],
        key=lambda edge: float_value(edge.get("score")),
        reverse=True,
    )
    if weak_high:
        make_issue(
            issues,
            issue_id="weak_project_edge_high_score",
            area="relationship",
            severity="high",
            title="弱共现关系分数过高",
            detail=f"有 {len(weak_high)} 条弱共现边进入高分区，最高分 {top_weak_score:.1f}，会把图谱排序顶歪。",
            suggested_action="弱共现边应作为低置信热度保留，不进入强关系排序；后续可给 co_mention_weak 设置独立分数上限。",
            evidence=[edge_sample(edge) for edge in weak_high[:6]],
        )

    overdominant = sorted(
        [
            edge for edge in project_edges
            if top_person_score and float_value(edge.get("score")) > top_person_score * 3
        ],
        key=lambda edge: float_value(edge.get("score")),
        reverse=True,
    )
    if overdominant:
        make_issue(
            issues,
            issue_id="project_edge_overdominates_person_graph",
            area="relationship",
            severity="medium",
            title="人物-项目边压过人物关系",
            detail=f"最强项目边约为最强人物边的 {ratio:.1f} 倍，项目标签热度不应和真实协作关系混排。",
            suggested_action="看板继续分开展示人物关系和项目关联热度；算法层后续可分口径评分。",
            evidence=[edge_sample(edge) for edge in overdominant[:6]],
        )

    single_evidence = []
    low_authority = []
    keyword_only = []
    source_repetition = []
    for edge in person_edges:
        count = int_value(edge.get("count"))
        source_layers = edge.get("source_layers") if isinstance(edge.get("source_layers"), dict) else {}
        evidence = edge.get("evidence") if isinstance(edge.get("evidence"), list) else []
        statements = [str(item.get("statement") or "") for item in evidence if isinstance(item, dict)]
        source_titles = [str(item.get("source_title") or "") for item in evidence if isinstance(item, dict)]

        if count <= 1 and not source_layers.get("reviewed_profile"):
            single_evidence.append(edge)
        if count >= 2 and not (source_layers.get("reviewed_profile") or source_layers.get("profile_memory")):
            low_authority.append(edge)
        if any(KEYWORD_ONLY_RE.search(statement) and not VERB_HINT_RE.search(statement) for statement in statements):
            keyword_only.append(edge)
        if count >= 4 and source_titles and len(set(source_titles)) <= 1:
            source_repetition.append(edge)

    if single_evidence:
        make_observation(
            observations,
            observation_id="single_evidence_person_relation",
            area="relationship",
            severity="low",
            title="单证据人物关系已降噪",
            detail=f"有 {len(single_evidence)} 条人物关系只有一条证据且不是 reviewed 来源，已标为低置信。",
            suggested_action="保留边数据，但不进入默认强关系摘要；后续自动补证据。",
            evidence=[edge_sample(edge) for edge in single_evidence[:6]],
        )
    if low_authority:
        make_observation(
            observations,
            observation_id="low_authority_person_relation",
            area="relationship",
            severity="low",
            title="人物关系来源权威性偏低",
            detail=f"有 {len(low_authority)} 条人物关系主要来自 reference/distilled 层，缺少 reviewed/profile 支撑。",
            suggested_action="继续自动沉淀；等更多 reviewed/profile 证据出现后再提高置信。",
            evidence=[edge_sample(edge) for edge in low_authority[:6]],
        )
    if keyword_only:
        make_issue(
            issues,
            issue_id="keyword_only_relation_evidence",
            area="relationship",
            severity="medium",
            title="关系证据像关键词列表",
            detail=f"有 {len(keyword_only)} 条关系证据缺少明确动作，可能是标签或素材清单。",
            suggested_action="关系层需要优先使用含动作、职责或决策的证据。",
            evidence=[edge_sample(edge) for edge in keyword_only[:6]],
        )
    if source_repetition:
        make_issue(
            issues,
            issue_id="same_source_repetition",
            area="relationship",
            severity="low",
            title="同源重复抬高关系分",
            detail=f"有 {len(source_repetition)} 条关系的多条证据来自同一标题，可能有重复放大。",
            suggested_action="后续评分按 unique source 做折扣。",
            evidence=[edge_sample(edge) for edge in source_repetition[:6]],
        )

    self_identity_edges = []
    for edge in project_edges:
        source = str(edge.get("source_label") or "")
        target = str(edge.get("target_label") or "")
        if (
            (source == USER_CANONICAL and target == "主账号")
            or (source == BIBI_CANONICAL and target == "协作账号")
        ):
            self_identity_edges.append(edge)
    if self_identity_edges:
        make_observation(
            observations,
            observation_id="project_self_identity_heat",
            area="relationship",
            severity="low",
            title="账号/身份项目边是热度，不是真协作关系",
            detail="主账号、协作账号这类项目标签会高频出现，应解释为身份/账号热度。",
            suggested_action="看板文案使用“项目关联热度”，不要把它称为强关系。",
            evidence=[edge_sample(edge) for edge in self_identity_edges[:4]],
        )

    skipped = summary.get("skipped") if isinstance(summary.get("skipped"), dict) else {}
    metrics = {
        "total_edges": len(edges),
        "person_edges": len(person_edges),
        "project_edges": len(project_edges),
        "weak_edges": len(weak_edges),
        "weak_edges_top_score": round(top_weak_score, 4),
        "top_person_edge_score": round(top_person_score, 4),
        "top_project_edge_score": round(top_project_score, 4),
        "project_vs_person_score_ratio": ratio,
        "strong_person_edges": int_value(summary.get("strong_person_edges")),
        "low_confidence_person_edges": int_value(summary.get("low_confidence_person_edges")),
        "skipped_by_reason": skipped,
    }
    return issues, observations, metrics


def check_inputs(people_index: dict[str, Any], relationship_index: dict[str, Any], sources: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not PEOPLE_INDEX_FILE.exists() or not people_index.get("people"):
        make_issue(
            issues,
            issue_id="missing_people_index",
            area="input",
            severity="high",
            title="人物索引缺失",
            detail="质量层需要 people_index.json 才能判断身份合并质量。",
            suggested_action="先运行 immortal.py people。",
        )
    if not RELATIONSHIP_INDEX_FILE.exists() or not relationship_index.get("edges"):
        make_issue(
            issues,
            issue_id="missing_relationship_index",
            area="input",
            severity="high",
            title="关系索引缺失",
            detail="质量层需要 relationship_index.json 才能判断图谱噪音。",
            suggested_action="先运行 immortal.py relationships。",
        )
    for origin, source in sources.items():
        if not source.get("exists"):
            make_issue(
                issues,
                issue_id=f"missing_source:{origin}",
                area="input",
                severity="low",
                title=f"输入层缺失：{origin}",
                detail="这不会阻塞质量报告，但会降低覆盖面。",
                suggested_action="如果该层本来应该存在，补跑对应 clean/distill/profile-auto-review 流程。",
            )
    return issues


def score_and_status(issues: list[dict[str, Any]]) -> tuple[int, str, dict[str, int]]:
    counts = Counter(str(issue.get("severity") or "low") for issue in issues)
    score = 100 - counts["high"] * 15 - counts["medium"] * 7 - counts["low"] * 2
    score = max(0, min(100, score))
    if counts["high"]:
        status = "attention"
    elif counts["medium"] >= 3:
        status = "attention"
    elif counts["medium"] or counts["low"]:
        status = "ok"
    else:
        status = "ok"
    return score, status, dict(counts)


def sort_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        issues,
        key=lambda issue: (
            SEVERITY_ORDER.get(str(issue.get("severity") or "low"), 9),
            str(issue.get("area") or ""),
            str(issue.get("id") or ""),
        ),
    )


def build_report() -> dict[str, Any]:
    people_index = read_json(PEOPLE_INDEX_FILE, {})
    relationship_index = read_json(RELATIONSHIP_INDEX_FILE, {})
    memory_rows, memory_sources = load_memory_layers()
    people = people_index.get("people") if isinstance(people_index.get("people"), list) else []

    input_issues = check_inputs(people_index, relationship_index, memory_sources)
    identity_issues, identity_observations, identity_metrics = check_identity_rules(people, memory_rows)
    relationship_issues, relationship_observations, relationship_metrics = check_relationships(relationship_index)
    issues = sort_issues(input_issues + identity_issues + relationship_issues)
    observations = sort_issues(identity_observations + relationship_observations)
    score, status, severity_counts = score_and_status(issues)

    if input_issues and not people_index.get("people"):
        status = "warn"

    top_issues = issues[:10]
    report = {
        "version": "0.1",
        "generated_at": now_local(),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "score": score,
        "issue_count": len(issues),
        "severity_counts": severity_counts,
        "top_issues": top_issues,
        "issues": issues,
        "observation_count": len(observations),
        "top_observations": observations[:10],
        "observations": observations,
        "identity": {
            "metrics": identity_metrics,
            "issue_count": len(identity_issues),
            "observation_count": len(identity_observations),
        },
        "relationships": {
            "metrics": relationship_metrics,
            "issue_count": len(relationship_issues),
            "observation_count": len(relationship_observations),
        },
        "inputs": {
            "people_index": {
                "path": str(PEOPLE_INDEX_FILE),
                "exists": PEOPLE_INDEX_FILE.exists(),
                "people": len(people),
                "generated_at": people_index.get("generated_at", ""),
            },
            "relationship_index": {
                "path": str(RELATIONSHIP_INDEX_FILE),
                "exists": RELATIONSHIP_INDEX_FILE.exists(),
                "edges": len(relationship_index.get("edges") or []),
                "generated_at": relationship_index.get("generated_at", ""),
            },
            "memory_layers": memory_sources,
        },
        "recommendation": recommendation_for(status, issues),
    }
    return report


def recommendation_for(status: str, issues: list[dict[str, Any]]) -> str:
    if status == "warn":
        return "先补齐 people/relationships 输入层，再看质量结论。"
    high_ids = {str(issue.get("id") or "") for issue in issues if issue.get("severity") == "high"}
    if "weak_project_edge_high_score" in high_ids:
        return "当前系统可用，但关系图谱需要继续把弱共现和强关系分口径展示、分口径评分。"
    if any(issue.get("area") == "identity" and issue.get("severity") == "high" for issue in issues):
        return "优先修身份归一，身份污染会影响人物卡、关系图谱和长期画像。"
    if issues:
        return "系统可用，持续让自动任务补证据；Codex 只需要处理质量层标出的异常。"
    return "质量层未发现明显问题。"


def issue_line(issue: dict[str, Any]) -> str:
    severity = str(issue.get("severity") or "low").upper()
    return f"- [{severity}] {issue.get('title')}：{issue.get('detail')} 建议：{issue.get('suggested_action')}"


def render_markdown(report: dict[str, Any]) -> str:
    relationships = report.get("relationships", {}).get("metrics", {})
    identity = report.get("identity", {}).get("metrics", {})
    severity_counts = report.get("severity_counts") or {}
    lines = [
        "# 记忆质量报告",
        "",
        f"Generated: {report.get('generated_at')}",
        "",
        "## 总览",
        f"- 状态：{report.get('status_label')} ({report.get('status')})",
        f"- 质量分：{report.get('score')}/100",
        f"- 问题数：{report.get('issue_count')}（high {severity_counts.get('high', 0)} / medium {severity_counts.get('medium', 0)} / low {severity_counts.get('low', 0)}）",
        f"- 观察项：{report.get('observation_count', 0)}",
        f"- 建议：{report.get('recommendation')}",
        "",
        "## 身份质量",
        f"- 人物数：{identity.get('people_count', 0)}",
        f"- 用户本人档案数：{identity.get('self_count', 0)}",
        f"- 用户本人漏抽取样本：{identity.get('user_alias_miss_count', 0)}",
        f"- 商务代理归因异常样本：{identity.get('mama_role_miss_count', 0)}",
        f"- 协作账号品牌/人物歧义样本：{identity.get('partner_brand_ambiguity_count', 0)}",
        "",
        "## 关系质量",
        f"- 总关系边：{relationships.get('total_edges', 0)}",
        f"- 人物关系边：{relationships.get('person_edges', 0)}",
        f"- 人物-项目边：{relationships.get('project_edges', 0)}",
        f"- 弱共现边：{relationships.get('weak_edges', 0)}，最高分 {relationships.get('weak_edges_top_score', 0)}",
        f"- 最强项目边 / 最强人物边：{relationships.get('project_vs_person_score_ratio', 0)}x",
        "",
        "## Top 问题",
    ]
    if report.get("top_issues"):
        lines.extend(issue_line(issue) for issue in report["top_issues"])
    else:
        lines.append("- 暂无")
    lines.extend(["", "## 观察项"])
    if report.get("top_observations"):
        lines.extend(issue_line(issue) for issue in report["top_observations"])
    else:
        lines.append("- 暂无")
    lines.extend(["", "## 输入文件"])
    inputs = report.get("inputs") or {}
    for key in ["people_index", "relationship_index"]:
        item = inputs.get(key) or {}
        lines.append(f"- {key}: {item.get('path')} exists={item.get('exists')}")
    for origin, source in (inputs.get("memory_layers") or {}).items():
        lines.append(f"- {origin}: {source.get('path')} rows={source.get('rows')} exists={source.get('exists')}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Immortal memory quality report")
    parser.add_argument("--json", default=str(OUTPUT_JSON), help="output JSON path")
    parser.add_argument("--md", default=str(OUTPUT_MD), help="output Markdown path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report()
    json_path = Path(args.json).expanduser()
    md_path = Path(args.md).expanduser()
    write_json_atomic(json_path, report)
    write_text_atomic(md_path, render_markdown(report))
    print(f"quality_json={json_path}")
    print(f"quality_md={md_path}")
    print(
        "summary="
        f"status={report.get('status')} "
        f"score={report.get('score')} "
        f"issues={report.get('issue_count')} "
        f"high={(report.get('severity_counts') or {}).get('high', 0)} "
        f"medium={(report.get('severity_counts') or {}).get('medium', 0)} "
        f"low={(report.get('severity_counts') or {}).get('low', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
