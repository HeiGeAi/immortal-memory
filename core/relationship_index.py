#!/usr/bin/env python3
"""Build a relationship knowledge index for the Immortal dashboard.

The people index answers "who is this person?". This layer answers "how are
people, projects, and responsibilities connected?" using the same distilled
memory rows, with low-signal meeting noise filtered out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import people_index as people_layer


HOME = Path.home()
IMMORTAL_DIR = HOME / ".immortal"
PEOPLE_INDEX_FILE = IMMORTAL_DIR / "people" / "people_index.json"
OUTPUT_DIR = IMMORTAL_DIR / "relationships"
OUTPUT_JSON = OUTPUT_DIR / "relationship_index.json"
OUTPUT_MD = OUTPUT_DIR / "relationship_index.md"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

PROJECT_LABELS = people_layer.PROJECT_LABELS
TYPE_LABELS = people_layer.TYPE_LABELS
SOURCE_LAYER_LABELS = people_layer.SOURCE_LAYER_LABELS
USER_CANONICAL = people_layer.USER_CANONICAL

EDGE_TYPE_WEIGHTS = {
    "relationship": 2.2,
    "decision": 1.9,
    "commitment": 1.7,
    "preference": 1.45,
    "lesson": 1.3,
    "project_fact": 1.2,
}

RELATION_PATTERNS = [
    (
        "role_support",
        "支持/承接",
        2.8,
        ("承接", "支持", "运维", "培训", "负责", "主导", "日常需求", "技术支援", "答疑", "接手", "协助"),
    ),
    (
        "business_handoff",
        "商务交接",
        2.6,
        ("商务", "筛客", "报价", "确权", "分润", "客户", "销售", "成交", "合同", "收口", "对接"),
    ),
    (
        "delivery_collaboration",
        "交付协作",
        2.4,
        ("交付", "项目", "中台", "服务器", "部署", "开发", "方案", "需求", "迁移", "搭建"),
    ),
    (
        "content_collaboration",
        "内容协作",
        2.2,
        ("审稿", "选题", "投流", "文章", "内容", "账号", "公众号", "排版", "素材", "发布"),
    ),
    (
        "management_or_guidance",
        "管理/辅导",
        2.1,
        ("辅导", "培养", "标准", "复盘", "反馈", "管理", "考核", "招聘", "沟通技巧", "输出标准"),
    ),
    (
        "customer_relation",
        "客户关系",
        2.0,
        ("外部客户A", "客户", "项目方", "老板", "付费", "报价", "企业版", "钉钉", "飞书生态"),
    ),
]
WEAK_RELATION_TYPE = ("co_mention_weak", "弱共现", 0.45)
WEAK_PROJECT_SCORE_CAP = 24.0
PROJECT_SCORE_CAP = 120.0
AT_MENTION_RE = re.compile(r"@[^\s@，,；;：:（）()]+")
VERB_HINTS = (
    "负责", "承接", "主导", "配合", "对接", "交付", "筛客", "报价", "确权", "审稿", "培训", "运维",
    "调研", "沟通", "反馈", "决定", "安排", "支持", "协助", "输出", "迁移", "部署", "开发", "招聘",
)


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def stable_id(kind: str, value: str) -> str:
    digest = hashlib.sha1(f"{kind}:{value}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def normalize_date(value: Any) -> str:
    return people_layer.normalize_date(value)


def compact(text: Any, limit: int = 220) -> str:
    return people_layer.compact(str(text or ""), limit)


def evidence_from_row(row: dict[str, Any]) -> dict[str, Any]:
    kind = str(row.get("memory_type") or "")
    return {
        "memory_id": str(row.get("memory_id") or people_layer.row_key(row)),
        "statement": compact(row.get("statement") or row.get("evidence") or "", 260),
        "memory_type": kind,
        "memory_type_label": TYPE_LABELS.get(kind, kind),
        "valid_from": normalize_date(row.get("valid_from")),
        "source_title": compact(people_layer.source_title(row), 120),
        "source_kind": people_layer.source_kind(row),
        "source_url": people_layer.source_url(row),
        "origin": row.get("_origin") or "",
        "origin_label": SOURCE_LAYER_LABELS.get(str(row.get("_origin") or ""), str(row.get("_origin") or "")),
        "score": round(float(row.get("_score") or 0), 4),
    }


def eligible_row(row: dict[str, Any]) -> bool:
    if people_layer.is_low_signal(row):
        return False
    kind = str(row.get("memory_type") or "")
    if kind not in EDGE_TYPE_WEIGHTS:
        return False
    statement = compact(row.get("statement") or row.get("evidence") or "", 120)
    if not statement:
        return False
    return True


def row_text(row: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(row.get("statement") or row.get("evidence") or ""),
            people_layer.source_title(row),
        ]
    )


def relation_type_for(row: dict[str, Any]) -> tuple[str, str, float]:
    text = row_text(row)
    for relation_type, label, weight, keywords in RELATION_PATTERNS:
        if any(keyword in text for keyword in keywords):
            return relation_type, label, weight
    return WEAK_RELATION_TYPE


def is_mass_mention_noise(row: dict[str, Any]) -> bool:
    statement = str(row.get("statement") or row.get("evidence") or "")
    schedule_markers = ("大纲阶段执行人", "初稿阶段执行人", "发布阶段执行人", "发布后执行人", "timeline：", "负责人：@")
    if any(marker in statement for marker in schedule_markers) and len(AT_MENTION_RE.findall(statement)) >= 2:
        return True
    mention_count = len(AT_MENTION_RE.findall(statement))
    if mention_count >= 4 and not any(hint in statement for hint in VERB_HINTS):
        return True
    compact_statement = compact(statement, 120)
    if compact_statement.startswith("@") and mention_count >= 3:
        return True
    if len(row.get("_people_canonical") or []) >= 4 and not any(hint in statement for hint in VERB_HINTS):
        return True
    return False


def aliases_for_person(person: str) -> list[str]:
    aliases = [person]
    aliases.extend(people_layer.CANONICAL_ALIASES.get(person, []))
    short = re.sub(r"[（(].*$", "", person).strip()
    if short and short not in aliases:
        aliases.append(short)
    return [alias for alias in dict.fromkeys(aliases) if alias]


def mentioned_people_in_statement(row: dict[str, Any], people: list[str]) -> set[str]:
    statement = str(row.get("statement") or row.get("evidence") or "")
    mentioned: set[str] = set()
    for person in people:
        if any(alias and alias in statement for alias in aliases_for_person(person)):
            mentioned.add(person)
    return mentioned


def row_weight(row: dict[str, Any]) -> float:
    kind = str(row.get("memory_type") or "")
    base = float(row.get("_score") or 0)
    _relation_type, _label, relation_weight = relation_type_for(row)
    return base + EDGE_TYPE_WEIGHTS.get(kind, 0.0) + relation_weight


def add_evidence(edge: dict[str, Any], row: dict[str, Any]) -> None:
    item = evidence_from_row(row)
    if not item["statement"]:
        return
    seen = {existing["memory_id"] for existing in edge["evidence"]}
    if item["memory_id"] in seen:
        return
    edge["evidence"].append(item)
    edge["evidence"].sort(key=lambda evidence: evidence.get("score") or 0, reverse=True)
    edge["evidence"] = edge["evidence"][:6]


def make_edge(source: str, target: str, kind: str, label: str) -> dict[str, Any]:
    return {
        "id": stable_id("edge", f"{kind}:{source}:{target}"),
        "source": source,
        "target": target,
        "kind": kind,
        "label": label,
        "relation_type": "",
        "relation_label": "",
        "count": 0,
        "weight": 0.0,
        "score": 0.0,
        "latest_date": "",
        "memory_types": {},
        "source_layers": {},
        "projects": {},
        "relation_types": {},
        "evidence": [],
    }


def finalize_edge(edge: dict[str, Any]) -> dict[str, Any]:
    count = int(edge.get("count") or 0)
    weight = float(edge.get("weight") or 0)
    edge["weight"] = round(weight, 4)
    relation_types = Counter(edge["relation_types"])
    dominant_relation_type = ""
    if relation_types:
        dominant_relation_type, _count = relation_types.most_common(1)[0]
        edge["relation_type"] = dominant_relation_type
        edge["relation_label"] = next(
            (label for key, label, _weight, _keywords in RELATION_PATTERNS if key == dominant_relation_type),
            WEAK_RELATION_TYPE[1] if dominant_relation_type == WEAK_RELATION_TYPE[0] else dominant_relation_type,
        )
    score = weight * (1 + min(count, 12) / 10)
    if edge.get("kind") == "person_project":
        if dominant_relation_type == WEAK_RELATION_TYPE[0]:
            score = min(score, WEAK_PROJECT_SCORE_CAP)
            edge["confidence"] = "low"
            edge["confidence_label"] = "弱共现热度，不进入强关系判断"
        else:
            score = min(score, PROJECT_SCORE_CAP)
            edge["confidence"] = "medium"
            edge["confidence_label"] = "项目关联热度，与人物关系分开比较"
    elif count <= 1 and not Counter(edge.get("source_layers") or {}).get("reviewed_profile"):
        edge["confidence"] = "low"
        edge["confidence_label"] = "单证据人物关系"
    else:
        edge["confidence"] = "high"
        edge["confidence_label"] = ""
    edge["score"] = round(score, 4)
    edge["memory_types"] = dict(Counter(edge["memory_types"]).most_common(6))
    edge["source_layers"] = dict(Counter(edge["source_layers"]).most_common(4))
    edge["projects"] = dict(Counter(edge["projects"]).most_common(6))
    edge["relation_types"] = dict(relation_types.most_common(6))
    return edge


def node_from_person(person: dict[str, Any]) -> dict[str, Any]:
    name = str(person.get("name") or "")
    return {
        "id": stable_id("person", name),
        "kind": "person",
        "key": name,
        "label": name,
        "category": person.get("category") or "other",
        "aliases": person.get("aliases") or [],
        "memory_count": int(person.get("memory_count") or 0),
        "latest_date": person.get("latest_date") or "",
        "confidence": person.get("confidence") or "high",
        "confidence_label": person.get("confidence_label") or "",
        "intro": compact(person.get("intro") or "", 360),
        "top_projects": person.get("top_projects") or [],
        "top_memory_types": person.get("memory_types") or [],
    }


def node_from_project(project: str, count: int) -> dict[str, Any]:
    return {
        "id": stable_id("project", project),
        "kind": "project",
        "key": project,
        "label": PROJECT_LABELS.get(project, project),
        "memory_count": int(count),
        "category": "project",
    }


def build_relationship_index() -> dict[str, Any]:
    people_index = read_json(PEOPLE_INDEX_FILE, {})
    people = people_index.get("people") if isinstance(people_index.get("people"), list) else []
    people_by_name = {str(person.get("name") or ""): person for person in people if isinstance(person, dict)}
    person_nodes = {name: node_from_person(person) for name, person in people_by_name.items() if name}
    person_ids = {name: node["id"] for name, node in person_nodes.items()}

    rows, counters = people_layer.collect_rows()
    person_edges: dict[tuple[str, str], dict[str, Any]] = {}
    project_edges: dict[tuple[str, str], dict[str, Any]] = {}
    project_counts: Counter[str] = Counter()
    skipped = Counter()

    for row in rows:
        canonical_people = [
            name for name in dict.fromkeys(row.get("_people_canonical") or [])
            if name in person_ids
        ]
        projects = [
            str(project) for project in row.get("projects") or []
            if str(project) and str(project) != "general"
        ]
        if not canonical_people:
            skipped["no_people"] += 1
            continue
        if not eligible_row(row):
            skipped["low_signal_or_low_value"] += 1
            continue
        if is_mass_mention_noise(row):
            skipped["mass_mention_noise"] += 1
            continue

        kind = str(row.get("memory_type") or "")
        origin = str(row.get("_origin") or "")
        date = normalize_date(row.get("valid_from"))
        weight = row_weight(row)
        relation_type, relation_label, _relation_weight = relation_type_for(row)

        if len(canonical_people) >= 2:
            mentioned_people = mentioned_people_in_statement(row, canonical_people)
            if relation_type == WEAK_RELATION_TYPE[0]:
                skipped["weak_co_mention"] += 1
            elif len(mentioned_people) < 2:
                skipped["participant_title_only"] += 1
            else:
                for left, right in combinations(sorted(canonical_people), 2):
                    if left not in mentioned_people or right not in mentioned_people:
                        continue
                    key = (left, right)
                    source_id = person_ids[left]
                    target_id = person_ids[right]
                    edge = person_edges.get(key)
                    if edge is None:
                        edge = make_edge(source_id, target_id, "person_person", relation_label)
                        edge["source_label"] = left
                        edge["target_label"] = right
                        person_edges[key] = edge
                    edge["count"] += 1
                    edge["weight"] += weight
                    if date and date > edge["latest_date"]:
                        edge["latest_date"] = date
                    edge["memory_types"][kind] = edge["memory_types"].get(kind, 0) + 1
                    edge["source_layers"][origin] = edge["source_layers"].get(origin, 0) + 1
                    edge["relation_types"][relation_type] = edge["relation_types"].get(relation_type, 0) + 1
                    for project in projects:
                        edge["projects"][project] = edge["projects"].get(project, 0) + 1
                    add_evidence(edge, row)

        for person in canonical_people:
            for project in projects:
                project_counts[project] += 1
                source_id = person_ids[person]
                target_id = stable_id("project", project)
                key = (person, project)
                edge = project_edges.get(key)
                if edge is None:
                    edge = make_edge(source_id, target_id, "person_project", "关联项目")
                    edge["source_label"] = person
                    edge["target_label"] = PROJECT_LABELS.get(project, project)
                    project_edges[key] = edge
                edge["count"] += 1
                edge["weight"] += weight
                if date and date > edge["latest_date"]:
                    edge["latest_date"] = date
                edge["memory_types"][kind] = edge["memory_types"].get(kind, 0) + 1
                edge["source_layers"][origin] = edge["source_layers"].get(origin, 0) + 1
                edge["relation_types"][relation_type] = edge["relation_types"].get(relation_type, 0) + 1
                edge["projects"][project] = edge["projects"].get(project, 0) + 1
                add_evidence(edge, row)

    finalized_person_edges = [
        finalize_edge(edge) for edge in person_edges.values()
        if int(edge.get("count") or 0) >= 1 and edge.get("evidence")
    ]
    finalized_project_edges = [
        finalize_edge(edge) for edge in project_edges.values()
        if int(edge.get("count") or 0) >= 2 and edge.get("evidence")
    ]
    finalized_person_edges.sort(key=lambda edge: (edge["score"], edge["count"], edge["latest_date"]), reverse=True)
    finalized_project_edges.sort(key=lambda edge: (edge["score"], edge["count"], edge["latest_date"]), reverse=True)

    project_nodes = {
        project: node_from_project(project, count)
        for project, count in project_counts.items()
        if count >= 2
    }

    nodes = list(person_nodes.values()) + list(project_nodes.values())
    node_rank = {node["id"]: idx for idx, node in enumerate(nodes)}
    for node in nodes:
        node["degree"] = 0
    for edge in finalized_person_edges + finalized_project_edges:
        for endpoint in (edge["source"], edge["target"]):
            if endpoint in node_rank:
                nodes[node_rank[endpoint]]["degree"] += int(edge.get("count") or 0)
    nodes.sort(key=lambda node: (node.get("kind") != "person", -int(node.get("degree") or 0), node.get("label") or ""))

    strong_person_edges = [
        edge for edge in finalized_person_edges
        if edge.get("confidence") != "low"
    ]
    low_confidence_person_edges = [
        edge for edge in finalized_person_edges
        if edge.get("confidence") == "low"
    ]

    summary = {
        "people_count": len(person_nodes),
        "project_count": len(project_nodes),
        "person_edges": len(finalized_person_edges),
        "strong_person_edges": len(strong_person_edges),
        "low_confidence_person_edges": len(low_confidence_person_edges),
        "project_edges": len(finalized_project_edges),
        "strongest_people": strong_person_edges[:10],
        "low_confidence_people": low_confidence_person_edges[:10],
        "strongest_projects": finalized_project_edges[:10],
        "skipped": dict(skipped),
    }

    return {
        "version": "0.1",
        "generated_at": now_local(),
        "basis": "people_index_plus_distilled_memory_rows",
        "source_files": [
            str(PEOPLE_INDEX_FILE),
            *[str(path) for path, _origin, _weight in people_layer.MEMORY_SOURCES],
        ],
        "counters": dict(counters),
        "summary": summary,
        "nodes": nodes,
        "edges": finalized_person_edges + finalized_project_edges,
    }


def render_markdown(index: dict[str, Any]) -> str:
    summary = index.get("summary") or {}
    lines = [
        "# Relationship Index",
        "",
        f"Generated: {index.get('generated_at')}",
        "",
        "这个文件是看板的关系知识库层，由结构化记忆自动生成。",
        "",
        "## Summary",
        f"- 人物节点：{summary.get('people_count', 0)}",
        f"- 项目节点：{summary.get('project_count', 0)}",
        f"- 人物关系边：{summary.get('person_edges', 0)}",
        f"- 人物-项目边：{summary.get('project_edges', 0)}",
        "",
        "## Strongest People Relations",
    ]
    strongest_people = summary.get("strongest_people") or []
    if not strongest_people:
        lines.append("- 暂无")
    for edge in strongest_people[:12]:
        lines.append(
            f"- {edge.get('source_label')} ↔ {edge.get('target_label')}："
            f"{edge.get('count')} 条 / score {edge.get('score')} / 最近 {edge.get('latest_date') or '-'}"
        )
        evidence = edge.get("evidence") or []
        if evidence:
            lines.append(f"  - 证据：{evidence[0].get('statement')}（{evidence[0].get('source_title') or '-'}）")

    lines.extend(["", "## Strongest Project Relations"])
    strongest_projects = summary.get("strongest_projects") or []
    if not strongest_projects:
        lines.append("- 暂无")
    for edge in strongest_projects[:12]:
        lines.append(
            f"- {edge.get('source_label')} → {edge.get('target_label')}："
            f"{edge.get('count')} 条 / score {edge.get('score')} / 最近 {edge.get('latest_date') or '-'}"
        )
        evidence = edge.get("evidence") or []
        if evidence:
            lines.append(f"  - 证据：{evidence[0].get('statement')}（{evidence[0].get('source_title') or '-'}）")
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
    parser = argparse.ArgumentParser(description="Build relationship knowledge index")
    parser.add_argument("--output-json", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--output-md", type=Path, default=OUTPUT_MD)
    parser.add_argument("--json", action="store_true", help="Print generated JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    index = build_relationship_index()
    write_outputs(index, args.output_json, args.output_md)
    summary = index.get("summary") or {}
    print(f"nodes={len(index.get('nodes') or [])}")
    print(f"person_edges={summary.get('person_edges', 0)}")
    print(f"project_edges={summary.get('project_edges', 0)}")
    print(f"output_json={args.output_json}")
    print(f"output_md={args.output_md}")
    if args.json:
        print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
