#!/usr/bin/env python3
"""Bounded, scoped and privacy-safe Context preview compilation."""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from claim_store import ClaimStore
from context_store import ContextStore, ContextStoreError
from event_store import safe_atomic_write_text, safe_read_text
from evidence_catalog import EvidenceCatalog, EvidenceCatalogError
from judgment_store import JudgmentStore
from living_self_service import LivingSelfService
from model_types import (
    CONTEXT_DEFAULT_MAX_BYTES,
    CONTEXT_MODES,
    CONTEXT_SECTIONS,
    DOMAIN_SCOPES,
    ModelValidationError,
    ROLE_SCOPES,
    validate_claim,
    new_context_pack,
    validate_context_pack,
    validate_evidence_ref,
    validate_judgment_card,
    validate_living_self_version,
)
from redact_common import redact as redact_credentials


COMPILER_VERSION = "1.1.0"
POLICY_VERSION = 1
MAX_SECTION_ITEMS = 20
MAX_SUMMARY_CHARS = 500
MAX_SOURCE_RECORDS = 10_000
MAX_EVIDENCE_IDS = 200
SECTION_ORDER = (
    "verified_facts",
    "confirmed_self_models",
    "judgment_cards",
    "counter_evidence",
    "inferences",
    "unknowns",
)
MODE_SCOPES = {
    "auto": (["general"], ["general"]),
    "advisor": (["general"], ["general"]),
    "writer": (["creator"], ["content"]),
    "reviewer": (["work"], ["technical"]),
    "business": (["work"], ["business"]),
    "project": (["work"], ["project"]),
    "custom": (["custom"], ["custom"]),
}
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(cookie\s*[:：=]\s*)\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bou_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?:/Users|/home)/[^/\s]+"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
)


class ContextCompilerError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ContextCompilerError("invalid_timestamp", field + " is invalid")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContextCompilerError("invalid_timestamp", field + " is invalid") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContextCompilerError(
            "invalid_timestamp", field + " must be timezone-aware"
        )
    return result.astimezone(timezone.utc)


def _safe_summary(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextCompilerError("invalid_summary", "context summary is empty")
    result = redact_credentials(value.strip())
    for pattern in SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result[:MAX_SUMMARY_CHARS]


def _unique(values: Sequence[str]) -> List[str]:
    return sorted(set(str(value) for value in values if str(value).strip()))


def _serialized_size(value: Mapping[str, Any]) -> Tuple[int, int]:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(encoded), len(encoded.encode("utf-8"))


def _tokens(value: str) -> set:
    compact = "".join(character.lower() for character in value if not character.isspace())
    result = set(re.findall(r"[a-z0-9_]{2,}", compact))
    result.update(
        compact[index : index + 2]
        for index in range(max(0, len(compact) - 1))
    )
    return result


class ContextCompiler:
    """Compile a safe preview from bounded derived model stores."""

    def __init__(
        self,
        vault_dir: Path,
        *,
        claims: Optional[Any] = None,
        living_self: Optional[Any] = None,
        judgments: Optional[Any] = None,
        evidence: Optional[Any] = None,
        context_store: Optional[ContextStore] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        vault = Path(os.path.abspath(str(vault_dir)))
        self.vault_dir = vault
        self.claims = claims or ClaimStore(vault)
        self.living_self = living_self or LivingSelfService(vault)
        self.judgments = judgments or JudgmentStore(vault)
        self.evidence = evidence or EvidenceCatalog(vault / "index.jsonl")
        self.context_store = context_store or ContextStore(vault, clock=clock)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._compile_authorization_hook: Optional[Callable[[], None]] = None

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception as exc:
            raise ContextCompilerError("invalid_clock", "compiler clock failed") from exc
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ContextCompilerError(
                "invalid_clock", "compiler clock must be timezone-aware"
            )
        return value.astimezone(timezone.utc)

    def _require_index(self) -> None:
        try:
            state = self.evidence.preflight()
        except Exception as exc:
            raise ContextCompilerError(
                "index_unavailable", "evidence index integrity cannot be proven"
            ) from exc
        if (
            not isinstance(state, Mapping)
            or state.get("mode") != "verified_sqlite"
            or state.get("source_state") != "current"
            or state.get("bounded_scan") is not True
            or not isinstance(state.get("indexed_id_count"), int)
        ):
            raise ContextCompilerError(
                "index_unavailable", "verified SQLite evidence index is required"
            )

    def _current_source_revision(self) -> Dict[str, Any]:
        self._require_index()
        try:
            living = self.living_self.current()
            validate_living_self_version(living)
            if living["status"] != "confirmed":
                raise ValueError("Living Self current is not confirmed")
            return {
                "claims_event_seq": int(self.claims.events.watermark()),
                "living_self_version": str(living["version_id"]),
                "judgments_event_seq": int(self.judgments.events.watermark()),
                "compiler_version": COMPILER_VERSION,
                "policy_version": POLICY_VERSION,
            }
        except ContextCompilerError:
            raise
        except Exception as exc:
            raise ContextCompilerError(
                "source_changed", "model authority is unavailable"
            ) from exc

    @staticmethod
    def _hash_text(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _rehash_pack(pack: Dict[str, Any]) -> Dict[str, Any]:
        value = dict(pack)
        value["content_hash"] = ""
        encoded = json.dumps(
            {key: item for key, item in value.items() if key != "content_hash"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        value["content_hash"] = "sha256:" + hashlib.sha256(
            encoded.encode("utf-8")
        ).hexdigest()
        validate_context_pack(value)
        return value

    @staticmethod
    def _provenance(
        sections: Mapping[str, List[Mapping[str, Any]]]
    ) -> Dict[str, List[str]]:
        items = [
            item
            for section in SECTION_ORDER
            for item in sections[section]
        ]
        return {
            "evidence_ids": _unique(
                [
                    evidence_id
                    for item in items
                    for evidence_id in item["evidence_ids"]
                ]
            ),
            "claim_ids": _unique(
                [
                    claim_id
                    for item in items
                    for claim_id in item["claim_ids"]
                ]
            ),
            "self_model_item_ids": [
                item["id"] for item in sections["confirmed_self_models"]
            ],
            "judgment_card_ids": [
                item["id"] for item in sections["judgment_cards"]
            ],
        }

    @staticmethod
    def _render_markdown(pack: Mapping[str, Any]) -> str:
        labels = {
            "verified_facts": "已验证事实",
            "confirmed_self_models": "已确认自我模型",
            "judgment_cards": "判断卡",
            "counter_evidence": "反证",
            "inferences": "系统推断",
            "unknowns": "未知与边界",
        }
        lines = [
            "# Immortal Task Context",
            "",
            "Task: " + str(pack["task"]),
            "Mode: " + str(pack["mode"]),
            "Context ID: " + str(pack["context_id"]),
            "Source revision: "
            + json.dumps(
                pack["source_revision"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "",
            "这是一份短期、可追溯的任务上下文。事实、推断和未知必须分开使用。",
        ]
        for section in SECTION_ORDER:
            lines.extend(["", "## " + labels[section], ""])
            items = pack["sections"][section]
            if not items:
                lines.append("- 无")
                continue
            for item in items:
                lines.extend(
                    [
                        "- " + str(item["summary"]),
                        "  - ID: " + str(item["id"]),
                        "  - Evidence IDs: "
                        + (", ".join(item["evidence_ids"]) or "none"),
                        "  - Claim IDs: "
                        + (", ".join(item["claim_ids"]) or "none"),
                    ]
                )
        return "\n".join(lines).rstrip() + "\n"

    def _build_pack(
        self,
        *,
        record: Mapping[str, Any],
        body: Mapping[str, Any],
        mode: str,
        excluded_item_ids: Sequence[str],
        context_id: str,
    ) -> Dict[str, Any]:
        excluded = set(excluded_item_ids)
        sections = {
            section: [
                dict(item)
                for item in body["sections"][section]
                if item["id"] not in excluded
            ]
            for section in CONTEXT_SECTIONS
        }
        policy = body["compile_policy"]
        pack = new_context_pack(
            task=str(record["task"]),
            mode=mode,
            living_self_version=str(
                record["source_revision"]["living_self_version"]
            ),
            lifecycle_status="compiled",
            availability_status="active",
            max_chars=int(policy["max_chars"]),
            max_bytes=int(policy["max_bytes"]),
            claims_event_seq=int(
                record["source_revision"]["claims_event_seq"]
            ),
            judgments_event_seq=int(
                record["source_revision"]["judgments_event_seq"]
            ),
            compiler_version=str(
                record["source_revision"]["compiler_version"]
            ),
            policy_version=int(record["source_revision"]["policy_version"]),
            preview_hash=str(record["preview_hash"]),
            sections=sections,
            now=str(record["generated_at"]),
            expires_at=str(record["expires_at"]),
        )
        pack["context_id"] = context_id
        pack["provenance"] = self._provenance(sections)
        privacy = dict(record["privacy_policy"])
        privacy["excluded_count"] = int(privacy["excluded_count"]) + len(excluded)
        privacy["reasons"] = sorted(
            set(privacy["reasons"])
            | ({"user_excluded"} if excluded else set())
        )
        pack["privacy_policy"] = privacy
        return self._rehash_pack(pack)

    def _stage_pack(
        self,
        *,
        preview_id: str,
        idempotency_key: str,
        template: Mapping[str, Any],
    ) -> Path:
        key = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        path = (
            self.context_store.root
            / "packs"
            / ".staging"
            / (preview_id + "-" + key + ".json")
        )
        safe_atomic_write_text(
            path,
            json.dumps(
                template,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        return path

    @staticmethod
    def _remove_stage(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _publish_pack(
        self, pack: Mapping[str, Any], markdown: str
    ) -> Tuple[Path, Path]:
        root = (
            self.context_store.root / "packs" / str(pack["context_id"])
        )
        context_path = root / "context.json"
        markdown_path = root / "TASK_CONTEXT.md"
        ready_path = root / "READY.json"
        context_text = (
            json.dumps(
                pack,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        safe_atomic_write_text(context_path, context_text)
        safe_atomic_write_text(markdown_path, markdown)
        ready = {
            "context_id": pack["context_id"],
            "content_hash": pack["content_hash"],
            "context_json_hash": self._hash_text(context_text),
            "context_md_hash": self._hash_text(markdown),
            "source_revision": pack["source_revision"],
        }
        safe_atomic_write_text(
            ready_path,
            json.dumps(
                ready,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        return context_path, markdown_path

    @staticmethod
    def _scope_values(
        value: Optional[List[str]],
        *,
        inferred: List[str],
        allowed: frozenset,
        field: str,
    ) -> List[str]:
        resolved = inferred if value is None else value
        if (
            not isinstance(resolved, list)
            or not resolved
            or any(
                not isinstance(item, str)
                or item not in allowed
                for item in resolved
            )
        ):
            raise ContextCompilerError("invalid_scope", field + " is invalid")
        return _unique(resolved)

    @staticmethod
    def _scope_match(
        item: Mapping[str, Any],
        roles: Sequence[str],
        domains: Sequence[str],
        custom_ids: Sequence[str],
    ) -> Tuple[bool, int]:
        item_roles = set(item.get("role_scope") or ["general"])
        item_domains = set(item.get("domain_scope") or ["general"])
        requested_roles = set(roles)
        requested_domains = set(domains)
        role_exact = bool(item_roles & requested_roles)
        domain_exact = bool(item_domains & requested_domains)
        role_ok = role_exact or "general" in item_roles
        domain_ok = domain_exact or "general" in item_domains
        if "custom" in item_roles or "custom" in item_domains:
            item_custom = set(item.get("custom_scope_ids") or [])
            custom_ok = bool(item_custom) and bool(item_custom & set(custom_ids))
        else:
            custom_ok = True
        return role_ok and domain_ok and custom_ok, int(role_exact) + int(domain_exact)

    @staticmethod
    def _effective(
        item: Mapping[str, Any], now: datetime
    ) -> Tuple[bool, Optional[str]]:
        updated = _parse_time(
            item.get("updated_at") or item.get("last_reviewed_at"),
            "updated_at",
        )
        if updated > now:
            return False, "future"
        valid_from = item.get("valid_from")
        valid_to = item.get("valid_to")
        if valid_from is not None and _parse_time(valid_from, "valid_from") > now:
            return False, "future"
        if valid_to is not None and _parse_time(valid_to, "valid_to") <= now:
            return False, "expired"
        return True, None

    @staticmethod
    def _claim_item(claim: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "kind": "claim",
            "id": claim["claim_id"],
            "revision": claim["revision"],
            "status": "confirmed",
            "source_kind": claim["source_kind"],
            "summary": _safe_summary(claim["statement"]),
            "privacy": claim["privacy"],
            "evidence_ids": _unique(claim["evidence_ids"]),
            "claim_ids": [claim["claim_id"]],
        }

    @staticmethod
    def _self_item(item: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "kind": "self_model",
            "id": item["item_id"],
            "revision": item["revision"],
            "status": "confirmed",
            "source_kind": "self_model",
            "summary": _safe_summary(item["summary"]),
            "privacy": "restricted",
            "evidence_ids": _unique(item["evidence_ids"]),
            "claim_ids": _unique(item["claim_ids"]),
        }

    @staticmethod
    def _judgment_item(card: Mapping[str, Any]) -> Dict[str, Any]:
        summary = str(card["title"]) + "：" + str(card["decision"])
        outcome = card["outcome"]
        if outcome["status"] != "unknown":
            summary += "；结果：" + str(outcome["summary"])
        return {
            "kind": "judgment",
            "id": card["card_id"],
            "revision": int(card.get("revision") or 1),
            "status": "confirmed",
            "source_kind": "judgment",
            "summary": _safe_summary(summary),
            "privacy": card["privacy"],
            "evidence_ids": _unique(card["evidence_ids"]),
            "claim_ids": _unique(card["claim_ids"]),
        }

    def _select_claims(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        roles: Sequence[str],
        domains: Sequence[str],
        custom_ids: Sequence[str],
        now: datetime,
        excluded: List[str],
        reasons: set,
    ) -> List[Dict[str, Any]]:
        ranked = []
        for source in rows:
            try:
                validate_claim(source)
            except ModelValidationError as exc:
                raise ContextCompilerError(
                    "derived_store_invalid", "Claim projection is invalid"
                ) from exc
            claim = dict(source)
            reason = None
            if claim["privacy"] == "private":
                reason = "private"
            elif claim["status"] != "confirmed":
                reason = "unconfirmed"
            else:
                effective, reason = self._effective(claim, now)
                if effective:
                    matches, scope_score = self._scope_match(
                        claim, roles, domains, custom_ids
                    )
                    if not matches:
                        reason = "scope_mismatch"
            if reason is not None:
                excluded.append(str(claim["claim_id"]))
                reasons.add(reason)
                continue
            if claim["source_kind"] not in {
                "direct",
                "quoted",
                "observed",
                "user_declared",
            }:
                excluded.append(str(claim["claim_id"]))
                reasons.add("unconfirmed")
                continue
            timestamp = _parse_time(claim["updated_at"], "updated_at").timestamp()
            ranked.append(
                (
                    (-scope_score, -int(bool(claim["evidence_ids"])), -timestamp, claim["claim_id"]),
                    self._claim_item(claim),
                )
            )
        return [item for _key, item in sorted(ranked, key=lambda pair: pair[0])]

    def _select_self_models(
        self,
        version: Mapping[str, Any],
        *,
        mode: str,
        roles: Sequence[str],
        domains: Sequence[str],
        custom_ids: Sequence[str],
        now: datetime,
        excluded: List[str],
        reasons: set,
    ) -> List[Dict[str, Any]]:
        try:
            validate_living_self_version(version)
        except ModelValidationError as exc:
            raise ContextCompilerError(
                "living_self_unavailable", "Living Self current is invalid"
            ) from exc
        if version["status"] != "confirmed":
            raise ContextCompilerError(
                "living_self_unavailable", "Living Self current is not confirmed"
            )
        if _parse_time(version["generated_at"], "generated_at") > now:
            raise ContextCompilerError(
                "living_self_unavailable", "Living Self current is from the future"
            )
        ranked = []
        for section in sorted(version["sections"]):
            for item in version["sections"][section]:
                item_id = str(item["item_id"])
                if item["status"] != "confirmed":
                    excluded.append(item_id)
                    reasons.add("unconfirmed")
                    continue
                if item["kind"] == "expression_dna" and mode != "writer":
                    excluded.append(item_id)
                    reasons.add("mode_mismatch")
                    continue
                effective, reason = self._effective(item, now)
                if not effective:
                    excluded.append(item_id)
                    reasons.add(str(reason))
                    continue
                matches, scope_score = self._scope_match(
                    item, roles, domains, custom_ids
                )
                if not matches:
                    excluded.append(item_id)
                    reasons.add("scope_mismatch")
                    continue
                timestamp = _parse_time(
                    item["last_reviewed_at"], "last_reviewed_at"
                ).timestamp()
                ranked.append(
                    (
                        (
                            -scope_score,
                            -int(bool(item["evidence_ids"])),
                            -timestamp,
                            item_id,
                        ),
                        self._self_item(item),
                    )
                )
        return [item for _key, item in sorted(ranked, key=lambda pair: pair[0])]

    def _select_judgments(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        task: str,
        now: datetime,
        excluded: List[str],
        reasons: set,
    ) -> List[Dict[str, Any]]:
        task_tokens = _tokens(task)
        ranked = []
        for source in rows:
            try:
                validate_judgment_card(source)
            except ModelValidationError as exc:
                raise ContextCompilerError(
                    "derived_store_invalid", "Judgment projection is invalid"
                ) from exc
            card = dict(source)
            card_id = str(card["card_id"])
            if card["privacy"] == "private":
                excluded.append(card_id)
                reasons.add("private")
                continue
            if card["status"] != "confirmed":
                excluded.append(card_id)
                reasons.add("unconfirmed")
                continue
            updated = _parse_time(card["updated_at"], "updated_at")
            if updated > now:
                excluded.append(card_id)
                reasons.add("future")
                continue
            searchable = " ".join(
                [
                    str(card["title"]),
                    str(card["situation"]),
                    str(card["goal"]),
                    str(card["decision"]),
                    " ".join(card["signals"]),
                ]
            )
            if not task_tokens.intersection(_tokens(searchable)):
                excluded.append(card_id)
                reasons.add("irrelevant")
                continue
            outcome_known = card["outcome"]["status"] != "unknown"
            ranked.append(
                (
                    (
                        -int(outcome_known),
                        -int(bool(card["evidence_ids"])),
                        -updated.timestamp(),
                        card_id,
                    ),
                    self._judgment_item(card),
                )
            )
        return [item for _key, item in sorted(ranked, key=lambda pair: pair[0])]

    @staticmethod
    def _apply_budget(
        candidates: Mapping[str, List[Dict[str, Any]]],
        *,
        max_chars: int,
        max_bytes: int,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], int, int]:
        sections = {name: [] for name in CONTEXT_SECTIONS}
        base_chars, base_bytes = _serialized_size(sections)
        if base_chars > max_chars or base_bytes > max_bytes:
            raise ContextCompilerError(
                "context_budget_too_small", "budget cannot hold empty Context sections"
            )
        for section in SECTION_ORDER:
            for item in candidates[section][:MAX_SECTION_ITEMS]:
                proposal = {name: list(values) for name, values in sections.items()}
                proposal[section].append(item)
                used_chars, used_bytes = _serialized_size(proposal)
                if used_chars <= max_chars and used_bytes <= max_bytes:
                    sections = proposal
        used_chars, used_bytes = _serialized_size(sections)
        return sections, used_chars, used_bytes

    def preview(
        self,
        task: str,
        *,
        mode: str = "auto",
        role_scope: Optional[List[str]] = None,
        domain_scope: Optional[List[str]] = None,
        custom_scope_ids: Optional[List[str]] = None,
        max_chars: int = 24_000,
        max_bytes: int = CONTEXT_DEFAULT_MAX_BYTES,
        ttl_seconds: int = 900,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        actor: Optional[Mapping[str, str]] = None,
        reason: str = "context preview requested",
    ) -> Dict[str, Any]:
        if not isinstance(task, str) or not task.strip():
            raise ContextCompilerError("task_required", "task is required")
        safe_task = _safe_summary(task)
        safe_reason = _safe_summary(reason)
        if mode not in CONTEXT_MODES:
            raise ContextCompilerError("invalid_context_mode", "mode is unsupported")
        if (
            not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or max_chars < 1
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes < 1
        ):
            raise ContextCompilerError("invalid_context_budget", "budget is invalid")
        inferred_roles, inferred_domains = MODE_SCOPES[mode]
        roles = self._scope_values(
            role_scope,
            inferred=inferred_roles,
            allowed=ROLE_SCOPES,
            field="role_scope",
        )
        domains = self._scope_values(
            domain_scope,
            inferred=inferred_domains,
            allowed=DOMAIN_SCOPES,
            field="domain_scope",
        )
        custom_ids = [] if custom_scope_ids is None else _unique(custom_scope_ids)
        if ("custom" in roles or "custom" in domains) and not custom_ids:
            raise ContextCompilerError(
                "custom_scope_id_required", "custom scope requires a stable ID"
            )
        self._require_index()
        now = self._now()
        claim_seq = int(self.claims.events.watermark())
        judgment_seq = int(self.judgments.events.watermark())
        try:
            living = self.living_self.current()
        except Exception as exc:
            raise ContextCompilerError(
                "living_self_unavailable", "Living Self current is unavailable"
            ) from exc
        claim_rows = self.claims.list()
        judgment_rows = self.judgments.list()
        if not isinstance(claim_rows, list) or not isinstance(judgment_rows, list):
            raise ContextCompilerError(
                "derived_store_invalid", "derived model list is invalid"
            )
        if (
            len(claim_rows) > MAX_SOURCE_RECORDS
            or len(judgment_rows) > MAX_SOURCE_RECORDS
        ):
            raise ContextCompilerError(
                "derived_store_limit", "derived source exceeds compiler record limit"
            )
        excluded: List[str] = []
        exclusion_reasons: set = set()
        candidates = {name: [] for name in CONTEXT_SECTIONS}
        candidates["verified_facts"] = self._select_claims(
            claim_rows,
            roles=roles,
            domains=domains,
            custom_ids=custom_ids,
            now=now,
            excluded=excluded,
            reasons=exclusion_reasons,
        )
        candidates["confirmed_self_models"] = self._select_self_models(
            living,
            mode=mode,
            roles=roles,
            domains=domains,
            custom_ids=custom_ids,
            now=now,
            excluded=excluded,
            reasons=exclusion_reasons,
        )
        candidates["judgment_cards"] = self._select_judgments(
            judgment_rows,
            task=task,
            now=now,
            excluded=excluded,
            reasons=exclusion_reasons,
        )
        sections, used_chars, used_bytes = self._apply_budget(
            candidates,
            max_chars=max_chars,
            max_bytes=max_bytes,
        )
        selected_items = [
            item
            for section in SECTION_ORDER
            for item in sections[section]
        ]
        evidence_ids = _unique(
            [
                evidence_id
                for item in selected_items
                for evidence_id in item["evidence_ids"]
            ]
        )
        if len(evidence_ids) > MAX_EVIDENCE_IDS:
            raise ContextCompilerError(
                "evidence_limit", "selected evidence exceeds bounded batch limit"
            )
        try:
            resolved = [
                self.evidence.resolve(evidence_id)
                for evidence_id in evidence_ids
            ]
        except EvidenceCatalogError as exc:
            raise ContextCompilerError(
                "index_unavailable", "selected evidence cannot be verified"
            ) from exc
        try:
            for reference in resolved:
                validate_evidence_ref(reference)
        except (ModelValidationError, TypeError) as exc:
            raise ContextCompilerError(
                "index_unavailable", "evidence projection is invalid"
            ) from exc
        if (
            {item.get("evidence_id") for item in resolved} != set(evidence_ids)
            or any(
                item.get("status") != "available"
                or item.get("privacy") == "private"
                for item in resolved
            )
        ):
            raise ContextCompilerError(
                "index_unavailable", "selected evidence is unavailable or private"
            )
        try:
            current_living = self.living_self.current()
        except Exception as exc:
            raise ContextCompilerError(
                "source_changed", "Living Self changed during preview"
            ) from exc
        if (
            int(self.claims.events.watermark()) != claim_seq
            or int(self.judgments.events.watermark()) != judgment_seq
            or current_living.get("version_id") != living.get("version_id")
            or current_living.get("content_hash") != living.get("content_hash")
        ):
            raise ContextCompilerError(
                "source_changed", "model authority changed during preview"
            )
        source_revision = {
            "claims_event_seq": claim_seq,
            "living_self_version": str(living["version_id"]),
            "judgments_event_seq": judgment_seq,
            "compiler_version": COMPILER_VERSION,
            "policy_version": POLICY_VERSION,
        }
        request = request_id or ("req_" + uuid.uuid4().hex)
        idem = idempotency_key or ("idem_" + uuid.uuid4().hex)
        actor_value = dict(actor or {"kind": "owner", "id": "owner"})
        stored = self.context_store.create_preview(
            task=safe_task,
            mode=mode,
            source_revision=source_revision,
            sections=sections,
            privacy_policy={
                "excluded_count": len(set(excluded)),
                "reasons": sorted(exclusion_reasons),
            },
            ttl_seconds=ttl_seconds,
            expected_version=0,
            request_id=request,
            idempotency_key=idem,
            actor=actor_value,
            reason=safe_reason,
            compile_policy={
                "max_chars": max_chars,
                "max_bytes": max_bytes,
            },
        )
        provenance = {
            "evidence_ids": evidence_ids,
            "claim_ids": _unique(
                [
                    claim_id
                    for item in selected_items
                    for claim_id in item["claim_ids"]
                ]
            ),
            "self_model_item_ids": [
                item["id"] for item in sections["confirmed_self_models"]
            ],
            "judgment_card_ids": [
                item["id"] for item in sections["judgment_cards"]
            ],
        }
        return {
            **stored,
            "budget": {
                "max_chars": max_chars,
                "used_chars": used_chars,
                "max_bytes": max_bytes,
                "used_bytes": used_bytes,
            },
            "sections": sections,
            "provenance": provenance,
        }

    def compile(
        self,
        *,
        preview_id: str,
        preview_hash: str,
        excluded_item_ids: List[str],
        request_id: str,
        idempotency_key: str,
        actor: Mapping[str, str],
        reason: str,
        resolved_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            record = self.context_store.get(preview_id)
            body = self.context_store.load_preview_body(
                preview_id, preview_hash
            )
        except ContextStoreError as exc:
            code = (
                "stale_preview"
                if exc.code
                in {"context_expired", "preview_unavailable", "stale_preview"}
                else exc.code
            )
            raise ContextCompilerError(code, str(exc)) from exc
        if record["availability_status"] != "active":
            raise ContextCompilerError("stale_preview", "preview has expired")
        mode = str(record["mode"])
        if mode == "auto":
            if (
                resolved_mode is None
                or resolved_mode not in CONTEXT_MODES
                or resolved_mode == "auto"
            ):
                raise ContextCompilerError(
                    "unresolved_context_mode",
                    "auto mode must be resolved before compilation",
                )
            mode = resolved_mode
        elif resolved_mode is not None and resolved_mode != mode:
            raise ContextCompilerError(
                (
                    "resolved_mode_conflict"
                    if record["lifecycle_status"] == "compiled"
                    else "invalid_context_mode"
                ),
                "resolved mode conflicts with the approved preview",
            )
        if (
            not isinstance(excluded_item_ids, list)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in excluded_item_ids
            )
            or len(set(excluded_item_ids)) != len(excluded_item_ids)
        ):
            raise ContextCompilerError(
                "stale_preview", "excluded item IDs are invalid"
            )
        excluded = sorted(excluded_item_ids)
        selected = {
            item["id"]
            for section in CONTEXT_SECTIONS
            for item in body["sections"][section]
        }
        if not set(excluded).issubset(selected):
            raise ContextCompilerError(
                "stale_preview", "excluded item is not present in preview"
            )
        if self._compile_authorization_hook is not None:
            self._compile_authorization_hook()
        if self._current_source_revision() != record["source_revision"]:
            raise ContextCompilerError(
                "stale_preview", "preview source revision is no longer current"
            )
        if record["lifecycle_status"] == "compiled" and (
            record["selection"]["excluded_item_ids"] == excluded
        ):
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ContextCompilerError(
                    "invalid_idempotency_key", "idempotency key is required"
                )
            if not isinstance(request_id, str) or not request_id.strip():
                raise ContextCompilerError("invalid_request_id", "request ID is required")
            _safe_summary(reason)
            pack = self._build_pack(
                record=record,
                body=body,
                mode=mode,
                excluded_item_ids=excluded,
                context_id=str(record["context_id"]),
            )
            markdown = self._render_markdown(pack)
            try:
                context_path, markdown_path = self._publish_pack(pack, markdown)
            except OSError as exc:
                raise ContextCompilerError(
                    "pack_publish_failed",
                    "compiled pack is not published and cannot be delivered",
                ) from exc
            loaded = self.load_compiled(str(record["context_id"]))
            return {
                **loaded,
                "context_json": str(context_path),
                "context_md": str(markdown_path),
            }
        template = self._build_pack(
            record=record,
            body=body,
            mode=mode,
            excluded_item_ids=excluded,
            context_id="ctx_pending",
        )
        try:
            stage_path = self._stage_pack(
                preview_id=preview_id,
                idempotency_key=idempotency_key,
                template=template,
            )
        except OSError as exc:
            raise ContextCompilerError(
                "pack_write_failed", "compiled pack staging failed"
            ) from exc
        try:
            try:
                compiled = self.context_store.begin_compile(
                    preview_id,
                    approved_mode=mode,
                    preview_hash=preview_hash,
                    source_revision=record["source_revision"],
                    excluded_item_ids=excluded,
                    expected_version=1,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    actor=actor,
                    reason=_safe_summary(reason),
                )
            except ContextStoreError as exc:
                if exc.code != "version_conflict":
                    raise ContextCompilerError(exc.code, str(exc)) from exc
                winner = self.context_store.get(preview_id)
                if (
                    winner["lifecycle_status"] != "compiled"
                    or winner["mode"] != mode
                    or winner["preview_hash"] != preview_hash
                    or winner["source_revision"] != record["source_revision"]
                    or winner["selection"]["excluded_item_ids"] != excluded
                ):
                    raise ContextCompilerError(exc.code, str(exc)) from exc
                compiled = winner
            except OSError as exc:
                raise ContextCompilerError(
                    "compile_commit_failed", "compile event commit failed"
                ) from exc
            pack = self._build_pack(
                record=record,
                body=body,
                mode=mode,
                excluded_item_ids=excluded,
                context_id=str(compiled["context_id"]),
            )
            markdown = self._render_markdown(pack)
            try:
                context_path, markdown_path = self._publish_pack(pack, markdown)
            except OSError as exc:
                raise ContextCompilerError(
                    "pack_publish_failed",
                    "compiled pack is not published and cannot be delivered",
                ) from exc
        finally:
            self._remove_stage(stage_path)
        loaded = self.load_compiled(str(compiled["context_id"]))
        return {
            **loaded,
            "context_json": str(context_path),
            "context_md": str(markdown_path),
        }

    def _load_verified_snapshot(
        self,
        context_id: str,
        *,
        allowed_lifecycle_statuses: Sequence[str],
    ) -> Dict[str, Any]:
        try:
            record = self.context_store.get(context_id)
        except ContextStoreError as exc:
            raise ContextCompilerError(exc.code, str(exc)) from exc
        if record["lifecycle_status"] not in set(allowed_lifecycle_statuses):
            raise ContextCompilerError(
                "stale_context", "context lifecycle does not permit this operation"
            )
        root = self.context_store.root / "packs" / context_id
        context_path = root / "context.json"
        markdown_path = root / "TASK_CONTEXT.md"
        ready_path = root / "READY.json"
        context_text = safe_read_text(context_path)
        markdown = safe_read_text(markdown_path)
        ready_text = safe_read_text(ready_path)
        if context_text is None or markdown is None or ready_text is None:
            raise ContextCompilerError(
                "context_not_ready", "compiled pack publication is incomplete"
            )
        try:
            pack = json.loads(context_text)
            ready = json.loads(ready_text)
            validate_context_pack(pack)
        except ModelValidationError as exc:
            if (
                exc.code != "context_expired"
                or "compiled" in set(allowed_lifecycle_statuses)
            ):
                raise ContextCompilerError(
                    "context_not_ready", "compiled pack publication is invalid"
                ) from exc
            try:
                self._rehash_pack({**pack, "availability_status": "expired"})
            except (ModelValidationError, TypeError) as nested:
                raise ContextCompilerError(
                    "context_not_ready", "compiled pack publication is invalid"
                ) from nested
        except (json.JSONDecodeError, TypeError) as exc:
            raise ContextCompilerError(
                "context_not_ready", "compiled pack publication is invalid"
            ) from exc
        expected_ready = {
            "context_id": pack["context_id"],
            "content_hash": pack["content_hash"],
            "context_json_hash": self._hash_text(context_text),
            "context_md_hash": self._hash_text(markdown),
            "source_revision": pack["source_revision"],
        }
        if (
            ready != expected_ready
            or pack["context_id"] != context_id
            or pack["lifecycle_status"] != "compiled"
            or pack["task"] != record["task"]
            or pack["mode"] != record["mode"]
            or pack["source_revision"] != record["source_revision"]
            or pack["preview_hash"] != record["preview_hash"]
            or pack["generated_at"] != record["generated_at"]
            or pack["expires_at"] != record["expires_at"]
        ):
            raise ContextCompilerError(
                "context_not_ready", "compiled pack marker does not match content"
            )
        excluded = set(record["selection"]["excluded_item_ids"])
        expected_sections = {
            section: sorted(
                item_id
                for item_id in record["selection"]["section_item_ids"][section]
                if item_id not in excluded
            )
            for section in CONTEXT_SECTIONS
        }
        actual_sections = {
            section: sorted(item["id"] for item in pack["sections"][section])
            for section in CONTEXT_SECTIONS
        }
        if actual_sections != expected_sections:
            raise ContextCompilerError(
                "context_not_ready", "compiled pack items do not match approval"
            )
        return {
            **pack,
            "context_json": str(context_path),
            "context_md": str(markdown_path),
            "context_markdown": markdown,
            "context_markdown_hash": expected_ready["context_md_hash"],
        }

    def load_outcome_snapshot(self, context_id: str) -> Dict[str, Any]:
        """Verify the immutable pack used by a consumed Context.

        This deliberately does not re-check the mutable model source revision:
        feedback belongs to the exact persisted snapshot that the Agent used.
        It does not relax the delivery gate in ``load_compiled``.
        """
        return self._load_verified_snapshot(
            context_id,
            allowed_lifecycle_statuses=("consumed", "outcome_recorded"),
        )

    def load_compiled(self, context_id: str) -> Dict[str, Any]:
        try:
            record = self.context_store.get(context_id)
        except ContextStoreError as exc:
            raise ContextCompilerError(exc.code, str(exc)) from exc
        if (
            record["lifecycle_status"] != "compiled"
            or record["availability_status"] != "active"
        ):
            raise ContextCompilerError(
                "stale_context", "compiled context is not currently usable"
            )
        if self._current_source_revision() != record["source_revision"]:
            raise ContextCompilerError(
                "stale_context", "compiled context source revision is stale"
            )
        return self._load_verified_snapshot(
            context_id,
            allowed_lifecycle_statuses=("compiled",),
        )
