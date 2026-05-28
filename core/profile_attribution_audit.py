#!/usr/bin/env python3
"""Audit and quarantine non-owner material from the reviewed profile layer."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from feishu_distill import infer_attribution, is_profile_review_memory
from profile_merge import render_reviewed_md, write_jsonl_atomic


HOME = Path.home()
IMMORTAL_DIR = HOME / ".immortal"
DEFAULT_REVIEWED = IMMORTAL_DIR / "reviewed" / "profile_memories.jsonl"
DEFAULT_REVIEWED_MD = IMMORTAL_DIR / "reviewed" / "profile_memories.md"
DEFAULT_DISTILLED = IMMORTAL_DIR / "feishu" / "distilled" / "profile_memories.jsonl"
DEFAULT_RECORDS = IMMORTAL_DIR / "feishu" / "clean" / "records.jsonl"
DEFAULT_REPORT_DIR = IMMORTAL_DIR / "quality"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
OWNER_MARKERS = ("用户本人", "用户本人", "Configured User", "用户本人")
FIRST_PERSON_RE = re.compile(r"(^|[。！？\n])\s*(我|我们|咱们)")
OTHER_SUBJECT_RE = re.compile(
    r"(^|\n|\s)(协作者A|协作账号|协作者L|协作者L|郭协作者H|协作者H|协作者I|候选人)"
    r"(认为|指出|提出|表示|反馈|负责|用|的|在|将|需|需要|奖金|签字)"
)
ORG_FACT_RE = re.compile(
    r"^(参赛要求|工程质量|一等奖|AI 产品提成|热点 skill 开发|问题表现|解决方案|稀缺内容考量|offer 合同)"
)
BRAND_OR_THIRD_CONTEXT_RE = re.compile(
    r"(协作账号账号|协作账号账号|公众号矩阵|第三方观点|候选人|简历|面试|录用决定|送别视频|口播脚本|游戏通关视频)"
)


def now_tag() -> str:
    return datetime.now(tz=LOCAL_TZ).strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
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
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def load_records_by_clean_id(records_path: Path, clean_ids: set[str]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not records_path.exists() or not clean_ids:
        return records
    with records_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            clean_id = str(record.get("clean_id") or "")
            if clean_id in clean_ids:
                records[clean_id] = record
                if len(records) >= len(clean_ids):
                    break
    return records


def source_candidate(row: dict[str, Any]) -> dict[str, Any]:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return {
        "candidate_id": source.get("candidate_id", ""),
        "clean_id": source.get("clean_id", ""),
        "raw_id": source.get("raw_id", ""),
        "source": source.get("source", ""),
        "review_bucket": source.get("review_bucket", ""),
        "distill_priority": source.get("distill_priority", ""),
        "title": source.get("title", ""),
        "evidence": row.get("statement", ""),
    }


def enrich_attribution(row: dict[str, Any], records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    enriched = dict(row)
    source = enriched.get("source") if isinstance(enriched.get("source"), dict) else {}
    clean_id = str(source.get("clean_id") or "")
    candidate = source_candidate(enriched)
    record = records.get(clean_id)
    attribution = infer_attribution(candidate, record, str(enriched.get("statement") or ""))
    enriched["attribution"] = attribution
    return enriched


def audit_row(row: dict[str, Any]) -> tuple[str, str]:
    candidate = dict(row)
    contamination_reason = self_profile_contamination_reason(candidate)
    if contamination_reason:
        return "quarantine", contamination_reason
    ok = is_profile_review_memory(candidate)
    reason = candidate.get("profile_review_exclusion") or ""
    category = (candidate.get("attribution") or {}).get("category", "")
    if ok:
        return "keep", "profile_review_pass"
    if row.get("focus") == "self_profile" and category != "self_direct":
        return "quarantine", reason or f"self_profile_not_owner_direct:{category or 'missing'}"
    return "quarantine", reason or "failed_current_profile_rules"


def self_profile_contamination_reason(row: dict[str, Any]) -> str:
    if row.get("focus") != "self_profile":
        return ""
    statement = str(row.get("statement") or row.get("evidence") or "").strip()
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    text = f"{statement}\n{source.get('title') or ''}"
    people = [str(person) for person in row.get("people") or []]
    projects = {str(project) for project in row.get("projects") or []}
    has_owner_marker = any(marker in text for marker in OWNER_MARKERS) or any(
        marker in person for marker in OWNER_MARKERS for person in people
    )
    if FIRST_PERSON_RE.search(statement):
        return ""
    if OTHER_SUBJECT_RE.search(statement):
        return "self_profile_other_person_subject"
    if ORG_FACT_RE.search(statement):
        return "self_profile_org_or_event_fact"
    if ("partner_brand" in projects or BRAND_OR_THIRD_CONTEXT_RE.search(text)) and not has_owner_marker:
        return "self_profile_brand_or_third_party_context_without_owner"
    return ""


def render_quarantine_md(rows: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        "# Profile Attribution Quarantine",
        "",
        f"Generated: {generated_at}",
        "",
        "这些行不是删除，而是从“本人长期画像”剥离出来，保留为审计证据。",
        "",
    ]
    for row in rows:
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        attr = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
        lines.append(f"- `{row.get('memory_id', '')}` {row.get('statement', '')}")
        lines.append(
            f"  - reason: {row.get('quarantine_reason', '')} / attribution: {attr.get('category', '')} / "
            f"source: {source.get('title', '')}"
        )
    return "\n".join(lines).rstrip() + "\n"


def build_summary(name: str, rows: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    enriched = [enrich_attribution(row, records) for row in rows]
    actions = Counter()
    reasons = Counter()
    attribution = Counter()
    focus = Counter()
    examples: list[dict[str, Any]] = []
    for row in enriched:
        action, reason = audit_row(row)
        actions[action] += 1
        reasons[reason] += 1
        attribution[(row.get("attribution") or {}).get("category", "unknown")] += 1
        focus[str(row.get("focus") or "unknown")] += 1
        if action == "quarantine" and len(examples) < 20:
            source = row.get("source") if isinstance(row.get("source"), dict) else {}
            examples.append(
                {
                    "memory_id": row.get("memory_id", ""),
                    "reason": reason,
                    "attribution": row.get("attribution", {}),
                    "focus": row.get("focus", ""),
                    "statement": row.get("statement", ""),
                    "source": source.get("title", ""),
                }
            )
    return {
        "name": name,
        "total": len(rows),
        "actions": dict(sorted(actions.items())),
        "reasons": dict(sorted(reasons.items())),
        "attribution": dict(sorted(attribution.items())),
        "focus": dict(sorted(focus.items())),
        "examples": examples,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit owner attribution in profile memory layers")
    parser.add_argument("--reviewed", type=Path, default=DEFAULT_REVIEWED)
    parser.add_argument("--reviewed-md", type=Path, default=DEFAULT_REVIEWED_MD)
    parser.add_argument("--distilled", type=Path, default=DEFAULT_DISTILLED)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--apply", action="store_true", help="Quarantine failing reviewed rows and rewrite reviewed profile layer")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    generated_at = now_iso()
    tag = now_tag()
    reviewed_rows = load_jsonl(args.reviewed)
    distilled_rows = load_jsonl(args.distilled)
    clean_ids = {
        str((row.get("source") or {}).get("clean_id") or "")
        for row in [*reviewed_rows, *distilled_rows]
        if isinstance(row.get("source"), dict)
    }
    clean_ids.discard("")
    records = load_records_by_clean_id(args.records, clean_ids)

    enriched_reviewed = [enrich_attribution(row, records) for row in reviewed_rows]
    kept: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for row in enriched_reviewed:
        action, reason = audit_row(row)
        if action == "keep":
            kept.append(row)
        else:
            quarantined_row = dict(row)
            quarantined_row["quarantine_reason"] = reason
            quarantined_row["quarantined_at"] = generated_at
            quarantined.append(quarantined_row)

    report = {
        "generated_at": generated_at,
        "records_loaded": len(records),
        "reviewed": build_summary("reviewed", reviewed_rows, records),
        "distilled": build_summary("distilled", distilled_rows, records),
        "apply": bool(args.apply),
        "kept_reviewed": len(kept),
        "quarantined_reviewed": len(quarantined),
    }

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_json = args.report_dir / "profile_attribution_audit.json"
    report_md = args.report_dir / "profile_attribution_audit.md"
    write_json(report_json, report)
    md_lines = [
        "# Profile Attribution Audit",
        "",
        f"Generated: {generated_at}",
        f"Reviewed kept/quarantined: {len(kept)} / {len(quarantined)}",
        "",
        "## Reviewed Actions",
        "",
    ]
    for key, value in report["reviewed"]["actions"].items():
        md_lines.append(f"- {key}: {value}")
    md_lines.extend(["", "## Top Reviewed Reasons", ""])
    for key, value in Counter(report["reviewed"]["reasons"]).most_common(12):
        md_lines.append(f"- {key}: {value}")
    md_lines.extend(["", "## Quarantine Examples", ""])
    for item in report["reviewed"]["examples"]:
        md_lines.append(f"- `{item['memory_id']}` {item['reason']} :: {item['statement']}")
    report_md.write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")

    if args.apply:
        if args.reviewed.exists():
            shutil.copy2(args.reviewed, args.reviewed.with_name(f"{args.reviewed.stem}.before-attribution-audit-{tag}{args.reviewed.suffix}"))
        if args.reviewed_md.exists():
            shutil.copy2(args.reviewed_md, args.reviewed_md.with_name(f"{args.reviewed_md.stem}.before-attribution-audit-{tag}{args.reviewed_md.suffix}"))
        write_jsonl_atomic(args.reviewed, kept)
        args.reviewed_md.write_text(render_reviewed_md(kept), encoding="utf-8")
        quarantine_jsonl = args.reviewed.with_name("profile_memories_quarantine.jsonl")
        quarantine_md = args.reviewed.with_name("profile_memories_quarantine.md")
        quarantine_snapshot = args.reviewed.with_name(f"profile_memories_quarantine-{tag}.jsonl")
        write_jsonl(quarantine_jsonl, quarantined)
        write_jsonl(quarantine_snapshot, quarantined)
        quarantine_md.write_text(render_quarantine_md(quarantined, generated_at), encoding="utf-8")

    print(f"reviewed_total={len(reviewed_rows)}")
    print(f"reviewed_kept={len(kept)}")
    print(f"reviewed_quarantined={len(quarantined)}")
    print(f"records_loaded={len(records)}")
    print(f"report_json={report_json}")
    print(f"report_md={report_md}")
    return 2 if quarantined and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
