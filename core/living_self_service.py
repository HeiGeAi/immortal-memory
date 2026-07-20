#!/usr/bin/env python3
"""Build a deterministic, evidence-backed Living Self candidate.

SQLite and generated profile files are intentionally absent from this build
path. Confirmed ClaimStore projections are the only authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from claim_store import ClaimStore
from event_store import (
    EventPathError,
    _anchored_parent,
    _exclusive_lock,
    _regular_stat_at,
    safe_atomic_write_text,
    safe_read_text,
    safe_regular_exists,
)
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

VERSION_BASE_FIELDS = frozenset(
    {
        "version_id",
        "parent_version_id",
        "status",
        "generation_reason",
        "content_hash",
        "based_on_claim_seq",
        "generated_at",
        "confirmed_at",
        "sections",
    }
)
VERSION_ID_PATTERN = re.compile(r"\Alsv_[0-9a-f]{32}\Z")
MAX_VERSION_BYTES = 8 * 1024 * 1024


class LivingSelfConflict(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def _try_parse_aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _parsed_time(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _is_effective(claim: Mapping[str, Any], now: str) -> bool:
    current = _try_parse_aware(now)
    valid_from = _try_parse_aware(
        claim.get("valid_from") or claim.get("created_at")
    )
    if current is None or valid_from is None or current < valid_from:
        return False
    raw_valid_to = claim.get("valid_to")
    if raw_valid_to is None:
        return True
    valid_to = _try_parse_aware(raw_valid_to)
    return valid_to is not None and current < valid_to


def _claim_sort_key(claim: Mapping[str, Any]) -> Any:
    return (
        _timestamp(claim),
        str(claim.get("claim_id") or ""),
    )


def _json_text(value: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _require_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be non-empty")
    return reason.strip()


def _safe_unlink(path: Path) -> None:
    try:
        with _anchored_parent(path, create=False) as (parent_fd, name):
            metadata = _regular_stat_at(parent_fd, name)
            if metadata is None:
                return
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except FileNotFoundError:
        return


def _safe_regular_names(directory: Path) -> List[str]:
    probe = directory / ".probe"
    try:
        with _anchored_parent(probe, create=False) as (directory_fd, _name):
            names = []
            for name in os.listdir(directory_fd):
                if not name.endswith(".json"):
                    continue
                metadata = _regular_stat_at(directory_fd, name)
                if metadata is None or not stat.S_ISREG(metadata.st_mode):
                    raise EventPathError(
                        "unsafe_path",
                        "Living Self version must be a regular file",
                    )
                names.append(name)
            return sorted(names)
    except FileNotFoundError:
        return []


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
        self.current_md_path = self.root / "current.md"
        self.versions_dir = self.root / "versions"
        self.lock_path = self.root / ".versions.lock"
        self._clock = clock or _utc_now
        self._generated_at = ""
        self._local_lock = threading.RLock()

    def _ensure_lock_parent(self) -> None:
        for attempt in range(4):
            try:
                with _anchored_parent(self.lock_path, create=True):
                    return
            except EventPathError as exc:
                if not isinstance(exc.__cause__, FileExistsError) or attempt == 3:
                    raise

    @contextmanager
    def _locked(self) -> Any:
        with self._local_lock:
            self._ensure_lock_parent()
            with _exclusive_lock(
                self.lock_path,
                timeout=10.0,
                stale_after=60.0,
            ):
                yield

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
            if not _is_effective(claim, now):
                continue
            eligible.append(claim)
        return sorted(eligible, key=_claim_sort_key)

    def _parent_version_id(self) -> Optional[str]:
        text = safe_read_text(self.current_path)
        if text is None:
            return None
        try:
            value = json.loads(text)
            if not isinstance(value, Mapping):
                return None
            validate_living_self_version(value)
        except (
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
        now = self._clock()
        eligible = self._eligible_claims(all_claims, now)
        if eligible:
            latest_source_time = _latest(
                timestamp
                for claim in eligible
                for timestamp in (
                    str(claim.get("updated_at") or claim.get("created_at")),
                    *(
                        [str(claim["valid_from"])]
                        if claim.get("valid_from") is not None
                        else []
                    ),
                )
            )
            self._generated_at = (
                latest_source_time
                if _parsed_time(latest_source_time) <= _parsed_time(now)
                else now
            )
        else:
            self._generated_at = now
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

    def _new_version_id(self) -> str:
        return "lsv_" + uuid.uuid4().hex

    def _version_paths(self, version_id: str) -> Any:
        if not isinstance(version_id, str) or not VERSION_ID_PATTERN.fullmatch(
            version_id
        ):
            raise ValueError("invalid Living Self version ID")
        base = self.versions_dir / version_id
        return base.with_suffix(".json"), base.with_suffix(".md")

    def _validate_persisted_version(
        self,
        value: Mapping[str, Any],
        *,
        expected_version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("Living Self version must be an object")
        version = json.loads(json.dumps(value, ensure_ascii=False))
        expected_fields = set(VERSION_BASE_FIELDS) | {"reason"}
        if version.get("generation_reason") == "manual_restore":
            expected_fields.add("restored_from")
        if set(version) != expected_fields:
            raise ValueError("Living Self version fields do not match contract")
        if version.get("status") != "confirmed":
            raise ValueError("Living Self history must contain confirmed versions")
        _require_reason(version.get("reason"))
        if version.get("generation_reason") == "manual_restore":
            restored_from = version.get("restored_from")
            if (
                not isinstance(restored_from, str)
                or not VERSION_ID_PATTERN.fullmatch(restored_from)
                or restored_from == version.get("version_id")
            ):
                raise ValueError("restored_from must reference another version")
        if (
            expected_version_id is not None
            and version.get("version_id") != expected_version_id
        ):
            raise ValueError("Living Self filename and version ID differ")
        if not VERSION_ID_PATTERN.fullmatch(str(version.get("version_id") or "")):
            raise ValueError("invalid Living Self version ID")
        try:
            validate_living_self_version(version)
        except ModelValidationError as exc:
            raise ValueError("invalid Living Self version: " + exc.code) from exc
        if version["content_hash"] != _hash(version["sections"]):
            raise ValueError("Living Self content hash mismatch")
        return version

    def _read_json_object(self, path: Path) -> Dict[str, Any]:
        text = safe_read_text(path)
        if text is None:
            raise FileNotFoundError(str(path))
        if len(text.encode("utf-8")) > MAX_VERSION_BYTES:
            raise ValueError("Living Self version exceeds size limit")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Living Self version is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Living Self version must be an object")
        return value

    def _load_version_unlocked(self, version_id: str) -> Dict[str, Any]:
        json_path, markdown_path = self._version_paths(version_id)
        if not safe_regular_exists(json_path):
            raise FileNotFoundError(str(json_path))
        version = self._validate_persisted_version(
            self._read_json_object(json_path),
            expected_version_id=version_id,
        )
        expected_markdown = self._render_markdown(version)
        markdown = safe_read_text(markdown_path)
        if markdown != expected_markdown:
            safe_atomic_write_text(markdown_path, expected_markdown)
        return version

    def _read_current_unlocked(
        self,
        *,
        required: bool,
    ) -> Optional[Dict[str, Any]]:
        text = safe_read_text(self.current_path)
        if text is None:
            if required:
                raise FileNotFoundError(str(self.current_path))
            return None
        current = self._validate_persisted_version(
            self._read_json_object(self.current_path)
        )
        historical = self._load_version_unlocked(current["version_id"])
        if _canonical(current) != _canonical(historical):
            raise ValueError("Living Self current differs from immutable history")
        expected_markdown = self._render_markdown(current)
        if safe_read_text(self.current_md_path) != expected_markdown:
            safe_atomic_write_text(self.current_md_path, expected_markdown)
        return current

    def _render_markdown(self, version: Mapping[str, Any]) -> str:
        lines = [
            "# Living Self",
            "",
            "Version: " + str(version["version_id"]),
            "Status: " + str(version["status"]),
            "Reason: " + str(version["reason"]),
            "Generated: " + str(version["generated_at"]),
            "Confirmed: " + str(version["confirmed_at"]),
            "Claim watermark: " + str(version["based_on_claim_seq"]),
            "",
        ]
        if version.get("restored_from"):
            lines.extend(
                ["Restored from: " + str(version["restored_from"]), ""]
            )
        for section in sorted(LIVING_SELF_SECTIONS):
            lines.extend(["## " + section, ""])
            items = version["sections"][section]
            if not items:
                lines.extend(["No evidence-backed items.", ""])
                continue
            for item in sorted(items, key=lambda row: str(row["item_id"])):
                lines.extend(
                    [
                        "### " + str(item["title"]),
                        "",
                        str(item["summary"]),
                        "",
                        "Item ID: " + str(item["item_id"]),
                        "Status: " + str(item["status"]),
                        "Evidence IDs: "
                        + ", ".join(item.get("evidence_ids") or []),
                        "Counter evidence IDs: "
                        + ", ".join(item.get("counter_evidence_ids") or []),
                        "Claim IDs: " + ", ".join(item.get("claim_ids") or []),
                        "",
                    ]
                )
        return "\n".join(lines)

    def _persist_version_pair(self, version: Mapping[str, Any]) -> None:
        json_path, markdown_path = self._version_paths(str(version["version_id"]))
        if safe_regular_exists(json_path) or safe_regular_exists(markdown_path):
            raise FileExistsError(str(json_path))
        safe_atomic_write_text(json_path, _json_text(version))
        try:
            safe_atomic_write_text(
                markdown_path,
                self._render_markdown(version),
            )
        except OSError:
            pass

    def _publish_current_pair(self, version: Mapping[str, Any]) -> None:
        safe_atomic_write_text(self.current_path, _json_text(version))
        try:
            safe_atomic_write_text(
                self.current_md_path,
                self._render_markdown(version),
            )
        except OSError:
            pass

    def _validate_candidate_unlocked(
        self,
        candidate: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(candidate, Mapping):
            raise ValueError("candidate must be an object")
        value = json.loads(json.dumps(candidate, ensure_ascii=False))
        if set(value) != set(VERSION_BASE_FIELDS):
            raise ValueError("candidate fields do not match contract")
        try:
            validate_living_self_version(value)
        except ModelValidationError as exc:
            raise ValueError("candidate is invalid: " + exc.code) from exc
        if (
            value["status"] != "candidate"
            or value["confirmed_at"] is not None
            or value["generation_reason"] not in {
                "claim_change",
                "scheduled_rebuild",
            }
            or value["content_hash"] != _hash(value["sections"])
            or value["version_id"]
            != "lsv_" + value["content_hash"][7:39]
        ):
            raise ValueError("candidate state is invalid")
        current = self._read_current_unlocked(required=False)
        fresh = self.build_candidate()
        for field in ("sections", "content_hash", "based_on_claim_seq"):
            if value[field] != fresh[field]:
                raise ValueError("candidate does not match current Claim authority")
        parent = value["parent_version_id"]
        if current is None:
            if parent is not None:
                raise ValueError("candidate parent does not exist")
        elif parent != current["version_id"]:
            raise LivingSelfConflict(
                "stale_candidate",
                "candidate parent is not the current Living Self version",
            )
        return fresh

    def _confirmed_from_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        reason: str,
        current: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        confirmed_at = _latest(
            (str(candidate["generated_at"]), str(self._clock()))
        )
        confirmed = dict(candidate)
        confirmed.update(
            {
                "version_id": self._new_version_id(),
                "parent_version_id": (
                    current["version_id"] if current is not None else None
                ),
                "status": "confirmed",
                "confirmed_at": confirmed_at,
                "reason": reason,
            }
        )
        return self._validate_persisted_version(confirmed)

    def confirm(
        self,
        candidate: Mapping[str, Any],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        resolved_reason = _require_reason(reason)
        with self._locked():
            validated = self._validate_candidate_unlocked(candidate)
            current = self._read_current_unlocked(required=False)
            confirmed = self._confirmed_from_candidate(
                validated,
                reason=resolved_reason,
                current=current,
            )
            self._persist_version_pair(confirmed)
            self._publish_current_pair(confirmed)
            return confirmed

    def current(self) -> Dict[str, Any]:
        with self._locked():
            current = self._read_current_unlocked(required=True)
            assert current is not None
            return current

    def versions(self) -> List[Dict[str, Any]]:
        with self._locked():
            versions = [
                self._load_version_unlocked(name[:-5])
                for name in _safe_regular_names(self.versions_dir)
            ]
            return sorted(
                versions,
                key=lambda item: (
                    str(item["confirmed_at"]),
                    str(item["version_id"]),
                ),
            )

    def load_version(self, version_id: str) -> Dict[str, Any]:
        with self._locked():
            return self._load_version_unlocked(version_id)

    @staticmethod
    def _items_by_id(version: Mapping[str, Any]) -> Dict[str, Any]:
        result = {}
        for section, items in version["sections"].items():
            for item in items:
                item_id = str(item["item_id"])
                if item_id in result:
                    raise ValueError("duplicate Living Self item ID")
                result[item_id] = {
                    "section": section,
                    "item": item,
                }
        return result

    def diff(self, from_version_id: str, to_version_id: str) -> Dict[str, Any]:
        with self._locked():
            before = self._items_by_id(
                self._load_version_unlocked(from_version_id)
            )
            after = self._items_by_id(
                self._load_version_unlocked(to_version_id)
            )
            added = [
                {
                    "item_id": item_id,
                    "section": after[item_id]["section"],
                    "item": after[item_id]["item"],
                }
                for item_id in sorted(set(after) - set(before))
            ]
            removed = [
                {
                    "item_id": item_id,
                    "section": before[item_id]["section"],
                    "item": before[item_id]["item"],
                }
                for item_id in sorted(set(before) - set(after))
            ]
            changed = []
            for item_id in sorted(set(before) & set(after)):
                old = before[item_id]
                new = after[item_id]
                if _canonical(old) == _canonical(new):
                    continue
                changed.append(
                    {
                        "item_id": item_id,
                        "from_section": old["section"],
                        "to_section": new["section"],
                        "before": old["item"],
                        "after": new["item"],
                    }
                )
            return {
                "added": added,
                "changed": changed,
                "removed": removed,
            }

    def restore(self, version_id: str, *, reason: str) -> Dict[str, Any]:
        resolved_reason = _require_reason(reason)
        with self._locked():
            source = self._load_version_unlocked(version_id)
            current = self._read_current_unlocked(required=False)
            now = _latest((str(source["generated_at"]), str(self._clock())))
            restored = {
                "version_id": self._new_version_id(),
                "parent_version_id": (
                    current["version_id"] if current is not None else None
                ),
                "status": "confirmed",
                "generation_reason": "manual_restore",
                "content_hash": source["content_hash"],
                "based_on_claim_seq": source["based_on_claim_seq"],
                "generated_at": now,
                "confirmed_at": now,
                "sections": source["sections"],
                "reason": resolved_reason,
                "restored_from": source["version_id"],
            }
            validated = self._validate_persisted_version(restored)
            self._persist_version_pair(validated)
            self._publish_current_pair(validated)
            return validated
