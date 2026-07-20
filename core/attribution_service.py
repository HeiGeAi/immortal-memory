#!/usr/bin/env python3
"""Deterministic owner attribution and bounded trust reporting."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set


SPEAKER_KINDS = frozenset({"owner", "other", "system", "unknown"})
ROLE_SCOPES = frozenset(
    {"general", "personal", "work", "creator", "family", "custom"}
)
DOMAIN_SCOPES = frozenset(
    {
        "general",
        "business",
        "content",
        "technical",
        "relationship",
        "project",
        "risk",
        "custom",
    }
)
PRIVACY_LEVELS = frozenset(
    {"private", "restricted", "context_safe", "public"}
)
TRANSIENT_TYPES = frozenset({"request", "emotion"})
MAX_SAFE_SUMMARY_CHARS = 200
DEFAULT_MAX_REPORT_SAMPLES = 20
MAX_REPORT_SAMPLES = 50

THIRD_PARTY_QUOTE_RE = re.compile(
    r"(?P<author>[\w\u4e00-\u9fff]{1,32})"
    r"(?:说|表示|认为|指出|反馈|提到|评价)[：:]"
)
ONE_OFF_RE = re.compile(r"(这一次|这次|本次|今天|现在|临时|先暂时)")
REQUEST_RE = re.compile(r"(请|帮我|麻烦|需要|先|立刻|马上|尽快|这一次|这次)")
EMOTION_RE = re.compile(r"(焦虑|开心|高兴|生气|难过|烦躁|沮丧|紧张|疲惫|害怕)")
DURABLE_RE = re.compile(r"(一直|通常|长期|反复|每次|始终|习惯|偏好|原则)")
PREFERENCE_RE = re.compile(r"(偏好|喜欢|习惯|更愿意|不喜欢|不要)")
SECRET_RE = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[a-z0-9._-]{12,}|cookie\s*:|"
    r"api[_ -]?key|access[_ -]?token|secret|password|密码|密钥|凭证|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
RESTRICTED_RE = re.compile(
    r"(客户|合同|报价|付款|发票|法务|绩效|薪资|招聘|商业化|手机号|身份证)"
)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _stable_other_id(value: str) -> str:
    if not value:
        return "other"
    digest = hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:12]
    return "person_" + digest


def _counter_map(counter: Counter) -> Dict[str, int]:
    return {str(key): int(counter[key]) for key in sorted(counter)}


class AttributionService:
    """Classify attribution without promoting third-party text into owner facts."""

    def __init__(
        self,
        owner_aliases: Set[str],
        *,
        auto_confirm_threshold: float = 0.85,
        max_report_samples: int = DEFAULT_MAX_REPORT_SAMPLES,
    ) -> None:
        self.owner_aliases = {
            _clean_text(value).casefold()
            for value in owner_aliases
            if _clean_text(value)
        }
        self.owner_aliases.add("owner")
        self.auto_confirm_threshold = max(
            0.85,
            min(1.0, float(auto_confirm_threshold)),
        )
        if (
            not isinstance(max_report_samples, int)
            or isinstance(max_report_samples, bool)
            or max_report_samples < 0
        ):
            raise ValueError("max_report_samples must be a non-negative integer")
        self.max_report_samples = min(max_report_samples, MAX_REPORT_SAMPLES)

    def _source(self, record: Mapping[str, Any]) -> str:
        source = record.get("source")
        if isinstance(source, Mapping):
            source = source.get("source") or source.get("kind") or ""
        normalized = _clean_text(source).casefold()
        if normalized.startswith("codex"):
            return "codex"
        if normalized.startswith("claude"):
            return "claude"
        if normalized.startswith(("feishu", "lark")):
            return "feishu"
        if normalized.startswith("web"):
            return "web"
        if normalized.startswith("local") or normalized in {
            "obsidian-note",
            "desktop-output",
            "immortal-smoke",
        }:
            return "local"
        return "custom"

    def _quoted_author(self, record: Mapping[str, Any], content: str) -> str:
        explicit = _clean_text(record.get("quoted_author"))
        if explicit and explicit.casefold() not in self.owner_aliases:
            return explicit
        match = THIRD_PARTY_QUOTE_RE.search(content[:240])
        if not match:
            return ""
        author = _clean_text(match.group("author"))
        if author.casefold() in self.owner_aliases or author in {
            "我",
            "我们",
            "用户",
            "本人",
        }:
            return ""
        return author

    def _speaker(
        self,
        record: Mapping[str, Any],
        content: str,
    ) -> tuple:
        quoted_author = self._quoted_author(record, content)
        if quoted_author:
            return (
                {"kind": "other", "id": _stable_other_id(quoted_author)},
                True,
            )
        author = _clean_text(
            record.get("author")
            or record.get("actor")
            or record.get("sender")
            or record.get("speaker")
        )
        role = _clean_text(record.get("role")).casefold()
        if author and author.casefold() in self.owner_aliases:
            return {"kind": "owner", "id": "owner"}, False
        if author:
            return {"kind": "other", "id": _stable_other_id(author)}, False
        if role == "user":
            return {"kind": "owner", "id": "owner"}, False
        if role in {"assistant", "system", "tool", "bot"}:
            return {"kind": "system", "id": "system"}, False
        return {"kind": "unknown", "id": "unknown"}, False

    def _subject(
        self,
        record: Mapping[str, Any],
        speaker: Mapping[str, str],
        content: str,
    ) -> Dict[str, str]:
        explicit = record.get("subject")
        if isinstance(explicit, Mapping):
            kind = _clean_text(explicit.get("kind")).casefold()
            identifier = _clean_text(explicit.get("id"))
            if kind in SPEAKER_KINDS and identifier:
                return {"kind": kind, "id": identifier}
        explicit_text = _clean_text(explicit)
        if explicit_text:
            if explicit_text.casefold() in self.owner_aliases or explicit_text.casefold() in {
                "user",
                "用户",
                "本人",
            }:
                return {"kind": "owner", "id": "owner"}
            return {"kind": "other", "id": _stable_other_id(explicit_text)}
        if speaker["kind"] == "owner":
            return {"kind": "owner", "id": "owner"}
        owner_mentioned = (
            bool(re.search(r"(^|[\s，。！？：:])(你|用户|本人)", content))
            or any(alias in content.casefold() for alias in self.owner_aliases)
        )
        if speaker["kind"] == "other":
            if owner_mentioned:
                return {"kind": "owner", "id": "owner"}
            return {"kind": "other", "id": speaker["id"]}
        if speaker["kind"] == "system":
            return {"kind": "system", "id": "system"}
        return {"kind": "unknown", "id": "unknown"}

    def _claim_type(
        self,
        record: Mapping[str, Any],
        speaker: Mapping[str, str],
        content: str,
    ) -> str:
        if speaker["kind"] == "other":
            return "external_view"
        explicit = _clean_text(record.get("claim_type")).casefold()
        if explicit in {
            "fact",
            "preference",
            "value",
            "commitment",
            "decision",
            "lesson",
            "relationship",
            "style",
            "emotion",
            "request",
            "external_view",
        }:
            return explicit
        if EMOTION_RE.search(content) and (
            ONE_OFF_RE.search(content) or not DURABLE_RE.search(content)
        ):
            return "emotion"
        if ONE_OFF_RE.search(content) and REQUEST_RE.search(content):
            return "request"
        if REQUEST_RE.search(content) and not DURABLE_RE.search(content):
            return "request"
        if PREFERENCE_RE.search(content) or DURABLE_RE.search(content):
            return "preference"
        if re.search(r"(决定|选择|不再|改为)", content):
            return "decision"
        if re.search(r"(承诺|一定会|必须完成)", content):
            return "commitment"
        return "fact"

    def _scope_list(
        self,
        value: Any,
        allowed: frozenset,
    ) -> List[str]:
        if not isinstance(value, list):
            return []
        result: List[str] = []
        for item in value:
            normalized = _clean_text(item).casefold()
            if normalized in allowed and normalized not in result:
                result.append(normalized)
        return result

    def _role_scope(
        self,
        record: Mapping[str, Any],
        content: str,
    ) -> List[str]:
        explicit = self._scope_list(record.get("role_scope"), ROLE_SCOPES)
        if explicit:
            return explicit
        if re.search(r"(家人|父母|孩子|伴侣|家庭)", content):
            return ["family"]
        if re.search(r"(账号|公众号|小红书|文章|写作|内容)", content):
            return ["creator"]
        if re.search(r"(客户|项目|代码|技术|工作|合同|交付|会议)", content):
            return ["work"]
        if re.search(r"(生活|情绪|健康|个人)", content):
            return ["personal"]
        return ["general"]

    def _domain_scope(
        self,
        record: Mapping[str, Any],
        content: str,
    ) -> List[str]:
        explicit = self._scope_list(record.get("domain_scope"), DOMAIN_SCOPES)
        if explicit:
            return explicit
        result: List[str] = []
        for domain, pattern in (
            ("risk", r"(风险|安全|隐私|密钥|审计|回滚)"),
            ("technical", r"(代码|API|数据库|技术|测试|部署|密钥)"),
            ("business", r"(客户|合同|报价|商业|营收|付款)"),
            ("content", r"(账号|公众号|小红书|文章|写作|内容)"),
            ("relationship", r"(关系|伴侣|同事|家人|沟通)"),
            ("project", r"(项目|任务|交付|里程碑)"),
        ):
            if re.search(pattern, content) and domain not in result:
                result.append(domain)
        return result or ["general"]

    def _privacy(self, record: Mapping[str, Any], content: str) -> str:
        if SECRET_RE.search(content):
            return "private"
        explicit = _clean_text(record.get("privacy")).casefold()
        if explicit in PRIVACY_LEVELS:
            return explicit
        if RESTRICTED_RE.search(content):
            return "restricted"
        return "context_safe"

    def _recurrence_score(self, record: Mapping[str, Any]) -> float:
        explicit = record.get("recurrence")
        if (
            isinstance(explicit, (int, float))
            and not isinstance(explicit, bool)
            and 0.0 <= float(explicit) <= 1.0
        ):
            return float(explicit)
        count = record.get("recurrence_count", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 2:
            return 0.0
        if count == 2:
            return 0.7
        return 1.0

    def _source_quality(self, source: str) -> float:
        return {
            "codex": 0.9,
            "claude": 0.9,
            "feishu": 0.85,
            "local": 0.75,
            "web": 0.6,
            "custom": 0.4,
        }[source]

    def classify(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(record, Mapping):
            raise TypeError("record must be a mapping")
        content = _clean_text(record.get("content") or record.get("statement"))
        speaker, third_party_quote = self._speaker(record, content)
        subject = self._subject(record, speaker, content)
        claim_type = self._claim_type(record, speaker, content)
        source = self._source(record)
        source_kind = {
            "owner": "direct",
            "other": "quoted",
            "system": "observed",
            "unknown": "inferred",
        }[speaker["kind"]]
        role_scope = self._role_scope(record, content)
        domain_scope = self._domain_scope(record, content)
        privacy = self._privacy(record, content)
        recurrence = self._recurrence_score(record)
        speaker_score = {
            "owner": 1.0,
            "other": 0.35,
            "system": 0.6,
            "unknown": 0.1,
        }[speaker["kind"]]
        source_quality = self._source_quality(source)
        confidence = (
            speaker_score * 0.4
            + recurrence * 0.3
            + source_quality * 0.3
        )
        basis = {
            "speaker": speaker_score,
            "recurrence": recurrence,
            "source_quality": source_quality,
            "policy_version": 1,
            "explanation": (
                "deterministic attribution policy v1: "
                "speaker 0.4, recurrence 0.3, source quality 0.3"
            ),
        }
        flags: List[str] = []
        if speaker["kind"] == "other":
            flags.append("other_speaker")
        elif speaker["kind"] == "system":
            flags.append("system_speaker")
        elif speaker["kind"] == "unknown":
            flags.append("unknown_speaker")
        if third_party_quote:
            flags.append("third_party_quote")
        if claim_type in TRANSIENT_TYPES:
            flags.append("transient_content")
        if recurrence < 0.7:
            flags.append("insufficient_recurrence")
        if privacy == "private":
            flags.append("private_content")
        auto_confirm_allowed = bool(
            speaker["kind"] == "owner"
            and subject["kind"] == "owner"
            and source_kind == "direct"
            and claim_type not in TRANSIENT_TYPES
            and claim_type != "external_view"
            and privacy != "private"
            and recurrence >= 0.7
            and source_quality >= 0.75
            and confidence >= self.auto_confirm_threshold
            and "third_party_quote" not in flags
        )
        return {
            "speaker": speaker,
            "subject": subject,
            "claim_type": claim_type,
            "source_kind": source_kind,
            "role_scope": role_scope,
            "domain_scope": domain_scope,
            "privacy": privacy,
            "confidence": confidence,
            "confidence_basis": basis,
            "auto_confirm_allowed": auto_confirm_allowed,
            "trust_flags": flags,
        }

    def safe_summary(self, classification: Mapping[str, Any]) -> str:
        flags = ",".join(classification.get("trust_flags") or []) or "none"
        summary = (
            "speaker="
            + str((classification.get("speaker") or {}).get("kind") or "unknown")
            + ";subject="
            + str((classification.get("subject") or {}).get("kind") or "unknown")
            + ";claim="
            + str(classification.get("claim_type") or "unknown")
            + ";source_kind="
            + str(classification.get("source_kind") or "unknown")
            + ";privacy="
            + str(classification.get("privacy") or "unknown")
            + ";confidence="
            + f"{float(classification.get('confidence') or 0.0):.3f}"
            + ";auto="
            + str(bool(classification.get("auto_confirm_allowed"))).lower()
            + ";flags="
            + flags
        )
        return summary[:MAX_SAFE_SUMMARY_CHARS]

    def build_report(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        generated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        speaker_counts: Counter = Counter()
        subject_counts: Counter = Counter()
        claim_counts: Counter = Counter()
        source_kind_counts: Counter = Counter()
        privacy_counts: Counter = Counter()
        trust_flag_counts: Counter = Counter()
        auto_confirm_counts: Counter = Counter()
        samples: List[Dict[str, Any]] = []
        total = 0
        for record in records:
            classification = self.classify(record)
            total += 1
            speaker_counts[classification["speaker"]["kind"]] += 1
            subject_counts[classification["subject"]["kind"]] += 1
            claim_counts[classification["claim_type"]] += 1
            source_kind_counts[classification["source_kind"]] += 1
            privacy_counts[classification["privacy"]] += 1
            for flag in classification["trust_flags"]:
                trust_flag_counts[flag] += 1
            auto_confirm_counts[
                "allowed"
                if classification["auto_confirm_allowed"]
                else "blocked"
            ] += 1
            if len(samples) < self.max_report_samples:
                samples.append(
                    {
                        "ordinal": total,
                        "summary": self.safe_summary(classification),
                    }
                )
        return {
            "schema_version": 1,
            "generated_at": generated_at
            or datetime.now(timezone.utc).isoformat(),
            "total": total,
            "counts": {
                "speaker": _counter_map(speaker_counts),
                "subject": _counter_map(subject_counts),
                "claim_type": _counter_map(claim_counts),
                "source_kind": _counter_map(source_kind_counts),
                "privacy": _counter_map(privacy_counts),
                "trust_flags": _counter_map(trust_flag_counts),
                "auto_confirm": _counter_map(auto_confirm_counts),
            },
            "samples": samples,
            "samples_truncated": total > len(samples),
        }

    def write_latest_report(
        self,
        path: Path,
        records: Iterable[Mapping[str, Any]],
        *,
        generated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        report = self.build_report(records, generated_at=generated_at)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return report
