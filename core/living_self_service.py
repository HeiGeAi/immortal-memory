#!/usr/bin/env python3
"""Build a deterministic, evidence-backed Living Self candidate.

SQLite and generated profile files are intentionally absent from this build
path. Confirmed ClaimStore projections are the only authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from claim_store import ClaimStore
from model_types import (
    LIVING_SELF_SECTIONS,
    ModelValidationError,
    validate_living_self_version,
    validate_self_model_item,
)


TRANSIENT_CLAIM_TYPES = frozenset({"emotion", "request"})
NON_MODEL_PRIVACY = frozenset({"private"})

MENTAL_MODEL_RULES = (
    {
        "key": "independent_judgment",
        "title": "先形成独立判断，再使用 AI 交叉验证",
        "summary": "在使用 AI 或工具前先形成自己的预判，再用证据交叉验证。",
        "terms": ("独立预判", "独立判断", "自己的判断", "形成判断", "预判"),
        "application": ["研究、审查、决策和技术选型"],
        "failure_conditions": ["缺少一手证据时，预判不能冒充事实"],
    },
    {
        "key": "trace_before_intelligence",
        "title": "先保全痕迹，再构建智能",
        "summary": "先确保原始记录可恢复，再建立检索、蒸馏和模型层。",
        "terms": ("先保全", "先备份", "防丢", "可恢复", "保全痕迹"),
        "application": ["记忆、资料和自动化数据管线"],
        "failure_conditions": ["不能因追求全量而扩大敏感数据暴露"],
    },
)

STYLE_RULES = (
    {
        "key": "conclusion_first",
        "title": "先给结论",
        "summary": "表达和沟通时先给结论，再补充必要依据。",
        "terms": ("先给结论", "先说结论", "结论先"),
    },
)

TENSION_RULES = (
    {
        "key": "speed_assurance",
        "title": "验证速度与风险保障",
        "summary": "低风险探索追求快速验证，高风险变更要求完整审查。",
        "poles": ["speed", "assurance"],
        "left_terms": ("快速", "先跑", "尽快", "MVP"),
        "right_terms": ("高风险", "完整审查", "充分审查", "安全门"),
        "application": ["按风险等级选择验证深度"],
        "failure_conditions": ["风险等级不清时不能只强调速度或保障一端"],
    },
)

SECTION_BUILDERS = {
    "identity_commitments": "build_identity_commitments",
    "values": "build_values",
    "expression_dna": "build_expression_dna",
    "mental_models": "build_mental_models",
    "decision_heuristics": "build_decision_heuristics",
    "anti_patterns": "build_anti_patterns",
    "tensions": "build_tensions",
    "honest_boundaries": "build_honest_boundaries",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value)).hexdigest()[:32]


def _sorted_unique(values: Iterable[str]) -> List[str]:
    return sorted({value for value in values if isinstance(value, str) and value})


def _matches(statement: str, terms: Sequence[str]) -> bool:
    return any(term.casefold() in statement.casefold() for term in terms)


def _timestamp(claim: Mapping[str, Any]) -> str:
    return str(claim.get("valid_from") or claim.get("created_at"))


def _authority_time(claim: Mapping[str, Any]) -> str:
    return _timestamp(claim).split("T", 1)[0]


def _parsed_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _earliest(values: Iterable[str]) -> str:
    return min(values, key=_parsed_time)


def _latest(values: Iterable[str]) -> str:
    return max(values, key=_parsed_time)


def _is_expired(claim: Mapping[str, Any], now: str) -> bool:
    valid_to = claim.get("valid_to")
    if valid_to is None:
        return False
    try:
        return datetime.fromisoformat(str(valid_to).replace("Z", "+00:00")) <= (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
        )
    except ValueError:
        return True


def _claim_sort_key(claim: Mapping[str, Any]) -> Any:
    return (
        _timestamp(claim),
        str(claim.get("claim_id") or ""),
    )


class LivingSelfService:
    """Build a candidate without mutating the current confirmed version."""

    def __init__(
        self,
        vault_dir: Path,
        *,
        clock: Optional[Callable[[], str]] = None,
    ) -> None:
        self.vault_dir = Path(vault_dir)
        self.claims = ClaimStore(self.vault_dir)
        self.root = self.vault_dir / "model" / "living-self"
        self.current_path = self.root / "current.json"
        self._clock = clock or _utc_now
        self._started_at = self._clock()
        self._generated_at = ""

    def _eligible_claims(
        self,
        claims: Sequence[Mapping[str, Any]],
        now: str,
    ) -> List[Dict[str, Any]]:
        eligible = []
        for source in claims:
            claim = dict(source)
            if claim.get("status") != "confirmed":
                continue
            if claim.get("claim_type") in TRANSIENT_CLAIM_TYPES:
                continue
            if claim.get("privacy") in NON_MODEL_PRIVACY:
                continue
            if _is_expired(claim, now):
                continue
            eligible.append(claim)
        return sorted(eligible, key=_claim_sort_key)

    def _parent_version_id(self) -> Optional[str]:
        if not self.current_path.is_file():
            return None
        try:
            value = json.loads(self.current_path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                return None
            validate_living_self_version(value)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ModelValidationError,
        ):
            return None
        if value.get("status") != "confirmed":
            return None
        version_id = value.get("version_id")
        if isinstance(version_id, str) and version_id.strip():
            return version_id
        return None

    def _item(
        self,
        *,
        kind: str,
        semantic_key: str,
        title: str,
        summary: str,
        claims: Sequence[Mapping[str, Any]],
        application: Sequence[str],
        failure_conditions: Sequence[str],
        recurrence: Optional[int] = None,
        generative_power: str = "untested",
        distinctiveness: str = "medium",
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        ordered = sorted(claims, key=lambda item: str(item["claim_id"]))
        claim_ids = [str(item["claim_id"]) for item in ordered]
        evidence_ids = _sorted_unique(
            evidence_id
            for claim in ordered
            for evidence_id in claim.get("evidence_ids") or []
        )
        counter_ids = _sorted_unique(
            evidence_id
            for claim in ordered
            for evidence_id in claim.get("counter_evidence_ids") or []
        )
        domains = _sorted_unique(
            scope
            for claim in ordered
            for scope in claim.get("domain_scope") or []
        )
        roles = _sorted_unique(
            scope
            for claim in ordered
            for scope in claim.get("role_scope") or []
        )
        domain_recurrence = len(domains) if recurrence is None else recurrence
        time_recurrence = len({_authority_time(claim) for claim in ordered})
        confidence = min(
            0.92,
            0.45
            + min(domain_recurrence, 3) * 0.1
            + min(time_recurrence, 3) * 0.08
            + min(len(evidence_ids), 3) * 0.04,
        )
        confidence = max(0.0, confidence - min(len(counter_ids), 3) * 0.15)
        identity_basis = {
            "kind": kind,
            "semantic_key": semantic_key,
            "claim_ids": claim_ids,
            "evidence_ids": evidence_ids,
            "counter_evidence_ids": counter_ids,
        }
        item = {
            "schema_version": 1,
            "revision": 1,
            "item_id": _stable_id("self_", identity_basis),
            "kind": kind,
            "title": title,
            "summary": summary,
            "evidence_ids": evidence_ids,
            "claim_ids": claim_ids,
            "counter_evidence_ids": counter_ids,
            "confidence": round(confidence, 6),
            "validation": {
                "cross_domain_recurrence": domain_recurrence,
                "generative_power": generative_power,
                "distinctiveness": distinctiveness,
            },
            "application": list(application),
            "failure_conditions": list(failure_conditions),
            "role_scope": roles or ["general"],
            "domain_scope": domains or ["general"],
            "valid_from": _earliest(_timestamp(claim) for claim in ordered),
            "valid_to": None,
            "status": "candidate",
            "last_reviewed_at": self._generated_at,
            "based_on_claim_seq": max(
                int(claim.get("based_on_event_seq") or 0) for claim in ordered
            ),
            "owner_confirmation_ref": None,
        }
        if extra:
            item.update(dict(extra))
        validate_self_model_item(item)
        return item

    def _individual_items(
        self,
        claims: Sequence[Mapping[str, Any]],
        *,
        claim_type: str,
        kind: str,
        application: str,
        failure: str,
        negative_only: bool = False,
    ) -> List[Dict[str, Any]]:
        selected = [
            claim
            for claim in claims
            if claim.get("claim_type") == claim_type
            and (
                not negative_only
                or _matches(
                    str(claim["statement"]),
                    ("不要", "不能", "避免", "别", "不应"),
                )
            )
        ]
        return [
            self._item(
                kind=kind,
                semantic_key=str(claim["claim_id"]),
                title=str(claim["statement"]),
                summary=str(claim["statement"]),
                claims=[claim],
                application=[application],
                failure_conditions=[failure],
                recurrence=len(set(claim.get("domain_scope") or ["general"])),
            )
            for claim in selected
        ]

    def build_identity_commitments(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        return self._individual_items(
            claims,
            claim_type="commitment",
            kind="identity_commitment",
            application="长期承诺与身份一致性检查",
            failure="承诺失效或完成后需要新的 Claim 纠正",
        )

    def build_values(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        return self._individual_items(
            claims,
            claim_type="value",
            kind="value",
            application="价值取舍和优先级判断",
            failure="语境变化时不能把单一价值绝对化",
        )

    def build_expression_dna(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        items = []
        style_claims = [
            claim for claim in claims if claim.get("claim_type") == "style"
        ]
        for rule in STYLE_RULES:
            matched = [
                claim
                for claim in style_claims
                if _matches(str(claim["statement"]), rule["terms"])
            ]
            domains = {
                scope for claim in matched for scope in claim.get("domain_scope") or []
            }
            times = {_authority_time(claim) for claim in matched}
            if len(domains) < 2 or len(times) < 2:
                continue
            items.append(
                self._item(
                    kind="expression_dna",
                    semantic_key=str(rule["key"]),
                    title=str(rule["title"]),
                    summary=str(rule["summary"]),
                    claims=matched,
                    application=["仅用于表达适配，不作为判断依据"],
                    failure_conditions=["任务要求或受众语境优先于风格适配"],
                    recurrence=len(domains),
                )
            )
        return items

    def build_mental_models(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        source_claims = [
            claim
            for claim in claims
            if claim.get("claim_type") in {"lesson", "value", "decision"}
        ]
        items = []
        for rule in MENTAL_MODEL_RULES:
            matched = [
                claim
                for claim in source_claims
                if _matches(str(claim["statement"]), rule["terms"])
            ]
            domains = {
                scope for claim in matched for scope in claim.get("domain_scope") or []
            }
            times = {_authority_time(claim) for claim in matched}
            if len(domains) < 2 or len(times) < 2:
                continue
            items.append(
                self._item(
                    kind="mental_model",
                    semantic_key=str(rule["key"]),
                    title=str(rule["title"]),
                    summary=str(rule["summary"]),
                    claims=matched,
                    application=list(rule["application"]),
                    failure_conditions=list(rule["failure_conditions"]),
                    recurrence=len(domains),
                    generative_power="tested",
                    distinctiveness="high",
                )
            )
        return items

    def build_decision_heuristics(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        return self._individual_items(
            claims,
            claim_type="decision",
            kind="decision_heuristic",
            application="匹配作用域的决策检查",
            failure="新反例或结果证据出现时需要重新评估",
        )

    def build_anti_patterns(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        return self._individual_items(
            claims,
            claim_type="lesson",
            kind="anti_pattern",
            application="行动前检查已知失败模式",
            failure="不能把特定语境的教训扩张为普遍禁令",
            negative_only=True,
        )

    def build_tensions(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        items = []
        for rule in TENSION_RULES:
            left = [
                claim
                for claim in claims
                if _matches(str(claim["statement"]), rule["left_terms"])
            ]
            right = [
                claim
                for claim in claims
                if _matches(str(claim["statement"]), rule["right_terms"])
            ]
            if not left or not right:
                continue
            items.append(
                self._item(
                    kind="tension",
                    semantic_key=str(rule["key"]),
                    title=str(rule["title"]),
                    summary=str(rule["summary"]),
                    claims=left + right,
                    application=list(rule["application"]),
                    failure_conditions=list(rule["failure_conditions"]),
                    recurrence=len(
                        {
                            scope
                            for claim in left + right
                            for scope in claim.get("domain_scope") or []
                        }
                    ),
                    extra={"poles": list(rule["poles"])},
                )
            )
        return items

    def build_honest_boundaries(
        self, claims: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        selected = [
            claim
            for claim in claims
            if claim.get("claim_type") in {"fact", "lesson"}
            and _matches(
                str(claim["statement"]),
                ("不确定", "不知道", "证据不足", "不能保证", "边界"),
            )
        ]
        return [
            self._item(
                kind="honest_boundary",
                semantic_key=str(claim["claim_id"]),
                title=str(claim["statement"]),
                summary=str(claim["statement"]),
                claims=[claim],
                application=["在相关任务中明确未知与证据边界"],
                failure_conditions=["新证据确认后需要通过 Claim correction 更新"],
            )
            for claim in selected
        ]

    def build_candidate(self) -> Dict[str, Any]:
        all_claims = self.claims.list()
        watermark = max(
            (int(claim.get("based_on_event_seq") or 0) for claim in all_claims),
            default=0,
        )
        now = self._started_at
        eligible = self._eligible_claims(all_claims, now)
        timestamp_sources = eligible if eligible else all_claims
        self._generated_at = (
            _latest(
                str(claim.get("updated_at") or claim.get("created_at"))
                for claim in timestamp_sources
            )
            if timestamp_sources
            else now
        )
        sections = {
            section: sorted(
                getattr(self, SECTION_BUILDERS[section])(eligible),
                key=lambda item: str(item["item_id"]),
            )
            for section in sorted(LIVING_SELF_SECTIONS)
        }
        content_hash = _hash(sections)
        candidate = {
            "version_id": "lsv_" + content_hash[7:39],
            "parent_version_id": self._parent_version_id(),
            "status": "candidate",
            "generation_reason": "claim_change",
            "content_hash": content_hash,
            "based_on_claim_seq": watermark,
            "generated_at": self._generated_at,
            "confirmed_at": None,
            "sections": sections,
        }
        validate_living_self_version(candidate)
        return candidate
