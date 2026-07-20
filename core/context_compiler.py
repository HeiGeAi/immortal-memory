#!/usr/bin/env python3
"""Bounded, scoped and privacy-safe Context preview compilation."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from claim_store import ClaimStore
from context_store import ContextStore
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
