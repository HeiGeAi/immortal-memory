#!/usr/bin/env python3
"""Deterministic, checkpointed migration from legacy profile layers to Claims."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from claim_store import ClaimStore, InvalidTransition
from event_store import (
    EventCorruption,
    EventPathError,
    JsonlEventStore,
    safe_atomic_write_text,
)
from evidence_catalog import EvidenceCatalog, EvidenceCatalogError
from model_types import ModelValidationError, new_claim, validate_claim


MIGRATION_ID = "legacy-profile-v1"
MIGRATION_ACTOR = {"kind": "migration", "id": MIGRATION_ID}
CHECKPOINT_VERSION = 2
DEFAULT_CHECKPOINT_EVERY = 100
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_SOURCE_LINE_BYTES = 1024 * 1024
MAX_SOURCE_RECORDS = 100_000
DETERMINISTIC_TIME = "1970-01-01T00:00:00+00:00"


class MigrationError(RuntimeError):
    """A fail-closed migration error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def after_claim_commit(created: int) -> None:
    """Fault-injection boundary for crash-resume tests."""


def after_snapshot_prepared() -> None:
    """Fault-injection boundary before the first authoritative write."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def _assert_safe_chain(path: Path) -> Path:
    candidate = _absolute(path)
    current = candidate
    while True:
        try:
            metadata = os.lstat(str(current))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise MigrationError(
                "unsafe_path",
                "migration path chain cannot be inspected safely",
            ) from exc
        else:
            if stat.S_ISLNK(metadata.st_mode):
                raise MigrationError(
                    "unsafe_path",
                    "migration does not follow symbolic links",
                )
        parent = current.parent
        if parent == current:
            break
        current = parent
    return candidate


def _read_optional_regular(path: Path) -> Optional[bytes]:
    candidate = _assert_safe_chain(path)
    try:
        before = os.lstat(str(candidate))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MigrationError(
            "source_unreadable",
            "migration source cannot be inspected",
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise MigrationError(
            "unsafe_path",
            "migration sources must be regular files",
        )
    if before.st_size > MAX_SOURCE_BYTES:
        raise MigrationError(
            "migration_source_too_large",
            "migration source exceeds the bounded size limit",
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise MigrationError(
            "source_unreadable",
            "migration source cannot be opened safely",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise MigrationError(
                "unsafe_path",
                "migration source identity changed during open",
            )
        chunks: List[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(descriptor, 1):
            raise MigrationError(
                "migration_source_too_large",
                "migration source exceeds the bounded size limit",
            )
        after = os.lstat(str(candidate))
        final = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            raise MigrationError(
                "source_changed",
                "migration source changed while it was read",
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _signature(metadata: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _capture_binding(path: Path, *, max_bytes: int) -> Dict[str, Any]:
    candidate = _assert_safe_chain(path)
    try:
        before = os.lstat(str(candidate))
    except FileNotFoundError:
        return {
            "path": str(candidate),
            "exists": False,
            "signature": None,
            "digest": hashlib.sha256(b"<missing>").hexdigest(),
        }
    except OSError as exc:
        raise MigrationError(
            "source_unreadable",
            "bound migration source cannot be inspected",
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise MigrationError(
            "unsafe_path",
            "bound migration sources must be regular files",
        )
    if before.st_size > max_bytes:
        raise MigrationError(
            "migration_source_too_large",
            "bound migration source exceeds the size limit",
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise MigrationError(
            "source_unreadable",
            "bound migration source cannot be opened safely",
        ) from exc
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _signature(opened) != _signature(before):
            raise MigrationError(
                "source_changed",
                "bound migration source changed during open",
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise MigrationError(
                    "migration_source_too_large",
                    "bound migration source exceeds the size limit",
                )
            digest.update(chunk)
        after = os.lstat(str(candidate))
        if _signature(after) != _signature(before):
            raise MigrationError(
                "source_changed",
                "bound migration source changed while hashing",
            )
        return {
            "path": str(candidate),
            "exists": True,
            "signature": _signature(before),
            "digest": digest.hexdigest(),
        }
    finally:
        os.close(descriptor)


def _verify_binding(binding: Mapping[str, Any]) -> None:
    path = _assert_safe_chain(Path(str(binding["path"])))
    try:
        metadata = os.lstat(str(path))
    except FileNotFoundError:
        if binding["exists"]:
            raise MigrationError(
                "source_changed",
                "bound migration source disappeared",
            )
        return
    except OSError as exc:
        raise MigrationError(
            "source_changed",
            "bound migration source cannot be revalidated",
        ) from exc
    if (
        not binding["exists"]
        or not stat.S_ISREG(metadata.st_mode)
        or _signature(metadata) != tuple(binding["signature"])
    ):
        raise MigrationError(
            "source_changed",
            "bound migration source identity changed",
        )


def _verify_bindings(bindings: Iterable[Mapping[str, Any]]) -> None:
    for binding in bindings:
        _verify_binding(binding)


def _decode_jsonl(body: Optional[bytes], source_name: str) -> List[Dict[str, Any]]:
    if body is None:
        return []
    rows: List[Dict[str, Any]] = []
    for line_number, raw in enumerate(body.splitlines(), start=1):
        if not raw.strip():
            continue
        if len(raw) > MAX_SOURCE_LINE_BYTES:
            raise MigrationError(
                "migration_source_line_too_large",
                source_name + " contains an oversized line",
            )
        if len(rows) >= MAX_SOURCE_RECORDS:
            raise MigrationError(
                "migration_source_too_many_records",
                source_name + " exceeds the bounded record count",
            )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                "malformed_legacy_source",
                source_name + " has malformed JSON at line " + str(line_number),
            ) from exc
        if not isinstance(value, dict):
            raise MigrationError(
                "malformed_legacy_source",
                source_name + " row must be an object",
            )
        rows.append(value)
    return rows


def _decode_json(body: Optional[bytes], source_name: str) -> Dict[str, Any]:
    if body is None:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "malformed_legacy_source",
            source_name + " is malformed JSON",
        ) from exc
    if not isinstance(value, dict):
        raise MigrationError(
            "malformed_legacy_source",
            source_name + " must contain an object",
        )
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _source_digest(reviewed: Optional[bytes], nuwa: Optional[bytes]) -> str:
    digest = hashlib.sha256()
    for name, body in (
        ("reviewed/profile_memories.jsonl", reviewed),
        ("profile_nuwa.json", nuwa),
    ):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"<missing>" if body is None else body)
        digest.update(b"\0")
    return digest.hexdigest()


def _body_digest(body: Optional[bytes]) -> str:
    return hashlib.sha256(
        b"<missing>" if body is None else body
    ).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _migration_provenance(
    source_name: str,
    *,
    source_digest: str,
    index_digest: str,
) -> str:
    return (
        source_name
        + "#sha256:"
        + source_digest
        + ";index_sha256:"
        + index_digest
    )


def _valid_migration_provenance(
    value: Any,
    *,
    source_name: str,
    source_digest: str,
) -> bool:
    prefix = (
        source_name
        + "#sha256:"
        + source_digest
        + ";index_sha256:"
    )
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    index_digest = value[len(prefix) :]
    return (
        len(index_digest) == 64
        and all(character in "0123456789abcdef" for character in index_digest)
    )


def _stable_digest(kind: str, legacy_id: str) -> str:
    seed = "immortal:" + MIGRATION_ID + ":" + kind + ":" + legacy_id
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def legacy_claim_id(kind: str, legacy_id: str) -> str:
    return "clm_" + _stable_digest(kind, legacy_id)[:32]


def _identity(row: Mapping[str, Any], field: str, source_name: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(
            "malformed_legacy_source",
            source_name + " row requires " + field,
        )
    if len(value) > 512:
        raise MigrationError(
            "malformed_legacy_source",
            source_name + " row has an oversized " + field,
        )
    return value.strip()


def _statement(row: Mapping[str, Any], source_name: str) -> str:
    for field in ("statement", "summary", "description", "title", "rule"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            statement = value.strip()
            if len(statement.encode("utf-8")) > MAX_SOURCE_LINE_BYTES:
                raise MigrationError(
                    "migration_source_line_too_large",
                    source_name + " statement exceeds the bounded size limit",
                )
            return statement
    raise MigrationError(
        "malformed_legacy_source",
        source_name + " row requires a statement",
    )


def _is_static_reference(row: Mapping[str, Any]) -> bool:
    if row.get("pinned") is True:
        return True
    focus = str(row.get("focus") or "").strip().lower()
    if focus == "reference_material":
        return True
    for field in ("origin", "kind", "source_kind"):
        value = str(row.get(field) or "").strip().lower()
        if value in {"pinned", "fallback", "reference", "reference_material"}:
            return True
    source = row.get("source")
    if isinstance(source, Mapping):
        value = str(source.get("kind") or source.get("origin") or "").lower()
        if value in {"pinned", "fallback", "reference", "reference_material"}:
            return True
    return False


def _evidence_ids(
    row: Mapping[str, Any],
    *,
    reviewed_evidence: Optional[Mapping[str, List[str]]] = None,
) -> List[str]:
    values = row.get("evidence_ids")
    if values is None:
        source = row.get("source")
        raw_id = source.get("raw_id") if isinstance(source, Mapping) else None
        values = [raw_id] if isinstance(raw_id, str) and raw_id.strip() else []
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip()
        for value in values
    ):
        raise MigrationError(
            "malformed_legacy_source",
            "legacy evidence_ids must be a list of non-empty strings",
        )
    result = list(dict.fromkeys(value.strip() for value in values))
    samples = row.get("evidence")
    if reviewed_evidence is not None and isinstance(samples, list):
        for sample in samples:
            if not isinstance(sample, Mapping):
                raise MigrationError(
                    "malformed_legacy_source",
                    "Nuwa evidence samples must be objects",
                )
            memory_id = sample.get("memory_id")
            if isinstance(memory_id, str):
                result.extend(reviewed_evidence.get(memory_id, []))
    return list(dict.fromkeys(result))


def _speaker(row: Mapping[str, Any]) -> Tuple[str, str]:
    raw = row.get("speaker")
    if isinstance(raw, Mapping):
        kind = str(raw.get("kind") or "unknown").strip().lower()
        identifier = str(raw.get("id") or kind).strip()
    else:
        kind = str(raw or "").strip().lower()
        identifier = kind
    if not kind:
        attribution = row.get("attribution")
        if isinstance(attribution, Mapping):
            category = str(attribution.get("category") or "").strip().lower()
            category_kind = (
                "owner"
                if category in {"self_direct", "owner_reported"}
                else "other"
                if category
                in {
                    "other_speaker",
                    "other_first_person",
                    "about_owner",
                    "about_owner_from_other",
                }
                else "unknown"
            )
            kind = str(
                attribution.get("speaker")
                or attribution.get("speaker_kind")
                or category_kind
            ).strip().lower()
            identifier = str(
                attribution.get("speaker_id")
                or attribution.get("actor")
                or attribution.get("sender")
                or kind
            ).strip()
    aliases = {
        "self": "owner",
        "user": "owner",
        "assistant": "system",
        "ai": "system",
        "third_party": "other",
        "external": "other",
    }
    kind = aliases.get(kind, kind)
    if kind not in {"owner", "other", "system", "unknown"}:
        kind = "unknown"
    return kind, identifier or kind


def _claim_type(row: Mapping[str, Any], speaker_kind: str) -> str:
    if speaker_kind == "other":
        return "external_view"
    raw = str(row.get("memory_type") or row.get("claim_type") or "").lower()
    aliases = {
        "preference": "preference",
        "value": "value",
        "commitment": "commitment",
        "decision": "decision",
        "lesson": "lesson",
        "relationship": "relationship",
        "style": "style",
        "emotion": "emotion",
        "request": "request",
        "mental_model": "lesson",
    }
    return aliases.get(raw, "fact")


def _privacy(row: Mapping[str, Any]) -> str:
    raw = str(row.get("privacy") or row.get("sensitivity") or "").lower()
    if raw in {"private", "secret"}:
        return "private"
    if raw in {"public"}:
        return "public"
    if raw in {"context_safe"}:
        return "context_safe"
    return "restricted"


def _source_kind(speaker_kind: str, *, nuwa: bool) -> str:
    if nuwa or speaker_kind == "unknown":
        return "inferred"
    if speaker_kind == "other":
        return "quoted"
    if speaker_kind == "system":
        return "observed"
    return "direct"


def _candidate(
    *,
    row: Mapping[str, Any],
    kind: str,
    legacy_id: str,
    evidence_ids: List[str],
    migration_source: str,
    nuwa: bool,
) -> Dict[str, Any]:
    speaker_kind, speaker_id = _speaker(row)
    if nuwa:
        speaker_kind, speaker_id = "system", MIGRATION_ID
    claim = new_claim(
        statement=_statement(row, migration_source),
        source_kind=_source_kind(speaker_kind, nuwa=nuwa),
        evidence_ids=evidence_ids,
        status="candidate",
        claim_type=_claim_type(
            {**dict(row), "memory_type": "mental_model"} if nuwa else row,
            speaker_kind,
        ),
        speaker_kind=speaker_kind,
        speaker_id=speaker_id,
        subject_kind="owner",
        subject_id="owner",
        confidence=0.0,
        role_scope=["general"],
        domain_scope=["general"],
        privacy=_privacy(row),
        now=DETERMINISTIC_TIME,
    )
    claim["claim_id"] = legacy_claim_id(kind, legacy_id)
    validate_claim(claim)
    digest = _stable_digest(kind, legacy_id)
    return {
        "claim": claim,
        "event_id": "evt_migrate_" + digest[:32],
        "request_id": "req_migrate_" + digest[:32],
        "idempotency_key": "idem_migrate_" + digest,
        "migration_source": migration_source,
    }


def _read_checkpoint(
    path: Path,
    *,
    source_digest: str,
) -> Optional[Dict[str, Any]]:
    body = _read_optional_regular(path)
    if body is None:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "malformed_checkpoint",
            "migration checkpoint is malformed",
        ) from exc
    valid_lists = True
    for field in ("candidate_ids", "committed_claim_ids"):
        items = value.get(field) if isinstance(value, dict) else None
        if (
            not isinstance(items, list)
            or any(not isinstance(item, str) or not item for item in items)
            or items != sorted(set(items))
        ):
            valid_lists = False
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "candidate_ids",
            "committed_claim_ids",
            "complete",
            "index_digest",
            "migration_id",
            "schema_version",
            "source_digest",
        }
        or value.get("schema_version") != CHECKPOINT_VERSION
        or value.get("migration_id") != MIGRATION_ID
        or not isinstance(value.get("complete"), bool)
        or not _is_sha256(value.get("source_digest"))
        or not _is_sha256(value.get("index_digest"))
        or not valid_lists
        or not set(value.get("committed_claim_ids") or []).issubset(
            set(value.get("candidate_ids") or [])
        )
    ):
        raise MigrationError(
            "malformed_checkpoint",
            "migration checkpoint has an invalid contract",
        )
    if value["source_digest"] != source_digest:
        raise MigrationError(
            "migration_source_changed",
            "legacy sources changed after migration began",
        )
    return value


def _write_checkpoint(
    path: Path,
    *,
    source_digest: str,
    index_digest: str,
    candidate_ids: List[str],
    committed_claim_ids: List[str],
    complete: bool,
) -> None:
    payload = {
        "schema_version": CHECKPOINT_VERSION,
        "migration_id": MIGRATION_ID,
        "source_digest": source_digest,
        "index_digest": index_digest,
        "candidate_ids": sorted(set(candidate_ids)),
        "committed_claim_ids": sorted(set(committed_claim_ids)),
        "complete": complete,
    }
    safe_atomic_write_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def _validate_current_state(vault: Path) -> Dict[str, Dict[str, Any]]:
    root = vault / "model" / "claims"
    events_path = root / "events.jsonl"
    current_path = root / "current.jsonl"
    events_exists = _read_optional_regular(events_path) is not None
    current_body = _read_optional_regular(current_path)
    current_exists = current_body is not None
    if events_exists != current_exists:
        raise MigrationError(
            "malformed_current_state",
            "claim event and current views must both exist",
        )
    if not events_exists:
        return {}
    probe = ClaimStore.__new__(ClaimStore)
    probe.root = root
    probe.events = JsonlEventStore(events_path)
    probe.current_path = current_path
    try:
        projected, _head = probe._replay()
        current = probe._read_current()
    except Exception as exc:
        raise MigrationError(
            "malformed_current_state",
            "claim state cannot be validated",
        ) from exc
    if current is None or _canonical(projected) != _canonical(current):
        raise MigrationError(
            "malformed_current_state",
            "claim current view does not match its event stream",
        )
    return current


def _claim_intent(value: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(value)
    result.pop("based_on_event_seq", None)
    result.pop("stream_version", None)
    return result


def _verify_existing_migration_state(
    vault: Path,
    raw_items: List[Dict[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
) -> None:
    expected = {
        item["claim_id"]: item
        for item in raw_items
        if item["claim_id"] in current
    }
    if not expected:
        return
    events_by_stream: Dict[str, List[Dict[str, Any]]] = {
        claim_id: [] for claim_id in expected
    }
    try:
        for event in JsonlEventStore(
            vault / "model" / "claims" / "events.jsonl"
        ).iter_all():
            stream_id = str(event["stream_id"])
            if stream_id in events_by_stream:
                events_by_stream[stream_id].append(event)
    except Exception as exc:
        raise MigrationError(
            "malformed_current_state",
            "claim events cannot be inspected for migration provenance",
        ) from exc
    for claim_id, item in expected.items():
        events = events_by_stream[claim_id]
        first = events[0] if events else {}
        initial = item.get("initial")
        payload = first.get("payload")
        first_claim = payload.get("claim") if isinstance(payload, Mapping) else None
        valid = (
            initial is not None
            and len(events) >= 1
            and first.get("event_type") == "claim.created"
            and first.get("stream_version") == 1
            and first.get("expected_version") == 0
            and first.get("event_id") == initial["event_id"]
            and first.get("request_id") == initial["request_id"]
            and first.get("idempotency_key")
            == ClaimStore._public_idempotency_key(initial["idempotency_key"])
            and first.get("actor") == MIGRATION_ACTOR
            and _valid_migration_provenance(
                first.get("migration_source"),
                source_name=item["source_name"],
                source_digest=item["source_digest"],
            )
            and isinstance(first_claim, Mapping)
            and _canonical(_claim_intent(first_claim))
            == _canonical(_claim_intent(initial["claim"]))
        )
        if not valid:
            raise MigrationError(
                "migration_state_conflict",
                "existing deterministic Claim does not match migration provenance",
            )


def _prepare_candidates(
    *,
    vault: Path,
    reviewed_rows: List[Dict[str, Any]],
    nuwa: Mapping[str, Any],
    source_digests: Mapping[str, str],
    index_digest: str,
) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    int,
    int,
    List[str],
]:
    reviewed_evidence: Dict[str, List[str]] = {}
    raw_candidates: List[Tuple[Dict[str, Any], str, str, str, bool]] = []
    excluded = 0
    seen: Dict[Tuple[str, str], str] = {}

    for row in reviewed_rows:
        legacy_id = _identity(
            row,
            "memory_id",
            "reviewed/profile_memories.jsonl",
        )
        if _is_static_reference(row):
            excluded += 1
            continue
        statement = _statement(row, "reviewed/profile_memories.jsonl")
        key = ("reviewed", legacy_id)
        canonical = _canonical(row)
        if key in seen:
            if seen[key] != canonical:
                raise MigrationError(
                    "malformed_legacy_source",
                    "duplicate legacy memory ID has conflicting content",
                )
            excluded += 1
            continue
        seen[key] = canonical
        ids = _evidence_ids(row)
        reviewed_evidence[legacy_id] = ids
        raw_candidates.append(
            (
                row,
                "reviewed",
                legacy_id,
                "reviewed/profile_memories.jsonl",
                False,
            )
        )

    models = nuwa.get("mental_models") or []
    if not isinstance(models, list):
        raise MigrationError(
            "malformed_legacy_source",
            "profile_nuwa mental_models must be a list",
        )
    for row in models:
        if not isinstance(row, dict):
            raise MigrationError(
                "malformed_legacy_source",
                "profile_nuwa mental model must be an object",
            )
        if row.get("status") != "accepted":
            continue
        legacy_id = _identity(row, "id", "profile_nuwa.json")
        if _is_static_reference(row):
            excluded += 1
            continue
        _statement(row, "profile_nuwa.json")
        key = ("nuwa", legacy_id)
        canonical = _canonical(row)
        if key in seen:
            if seen[key] != canonical:
                raise MigrationError(
                    "malformed_legacy_source",
                    "duplicate Nuwa model ID has conflicting content",
                )
            excluded += 1
            continue
        seen[key] = canonical
        raw_candidates.append(
            (row, "nuwa", legacy_id, "profile_nuwa.json", True)
        )

    raw_items: List[Dict[str, Any]] = []
    for row, kind, legacy_id, source_name, nuwa_row in raw_candidates:
        ids = _evidence_ids(
            row,
            reviewed_evidence=reviewed_evidence if nuwa_row else None,
        )
        provenance = _migration_provenance(
            source_name,
            source_digest=source_digests[source_name],
            index_digest=index_digest,
        )
        initial: Optional[Dict[str, Any]] = None
        if ids:
            try:
                initial = _candidate(
                    row=row,
                    kind=kind,
                    legacy_id=legacy_id,
                    evidence_ids=ids,
                    migration_source=provenance,
                    nuwa=nuwa_row,
                )
            except (ModelValidationError, ValueError) as exc:
                raise MigrationError(
                    "malformed_legacy_source",
                    "legacy row cannot form a valid candidate Claim",
                ) from exc
        raw_items.append(
            {
                "claim_id": legacy_claim_id(kind, legacy_id),
                "evidence_ids": ids,
                "initial": initial,
                "kind": kind,
                "legacy_id": legacy_id,
                "source_name": source_name,
                "source_digest": source_digests[source_name],
            }
        )

    catalog: Optional[EvidenceCatalog] = None
    if any(item["evidence_ids"] for item in raw_items):
        try:
            catalog = EvidenceCatalog(vault / "index.jsonl")
        except EvidenceCatalogError as exc:
            raise MigrationError(exc.code, str(exc)) from exc

    prepared: List[Dict[str, Any]] = []
    source_broken = 0
    broken_ids: List[str] = []
    resolved_cache: Dict[str, bool] = {}
    for item in raw_items:
        ids = item["evidence_ids"]
        if not ids:
            source_broken += 1
            broken_ids.append(
                item["kind"]
                + ":"
                + item["legacy_id"]
                + ":missing_evidence"
            )
            continue
        available: List[str] = []
        row_broken = False
        for evidence_id in ids:
            if evidence_id not in resolved_cache:
                try:
                    assert catalog is not None
                    ref = catalog.resolve(evidence_id)
                except EvidenceCatalogError as exc:
                    if exc.code == "evidence_not_found":
                        resolved_cache[evidence_id] = False
                    else:
                        raise MigrationError(exc.code, str(exc)) from exc
                else:
                    resolved_cache[evidence_id] = ref.get("status") == "available"
            if resolved_cache[evidence_id]:
                available.append(evidence_id)
            else:
                row_broken = True
                broken_ids.append(evidence_id)
        if row_broken or not available:
            source_broken += 1
            continue
        assert available == ids
        assert item["initial"] is not None
        prepared.append(item["initial"])
    return (
        raw_items,
        prepared,
        excluded,
        source_broken,
        sorted(set(broken_ids)),
    )


def _migrate_legacy_profile(
    vault_dir: Path,
    *,
    dry_run: bool = False,
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
) -> Dict[str, Any]:
    if (
        not isinstance(checkpoint_every, int)
        or isinstance(checkpoint_every, bool)
        or checkpoint_every < 1
    ):
        raise MigrationError(
            "invalid_checkpoint_every",
            "checkpoint_every must be a positive integer",
        )
    vault = _assert_safe_chain(Path(vault_dir))
    reviewed_path = vault / "reviewed" / "profile_memories.jsonl"
    nuwa_path = vault / "profile_nuwa.json"
    checkpoint_path = (
        vault / "model" / "migrations" / MIGRATION_ID / "checkpoint.json"
    )
    index_path = vault / "index.jsonl"

    reviewed_body = _read_optional_regular(reviewed_path)
    nuwa_body = _read_optional_regular(nuwa_path)
    reviewed_binding = _capture_binding(
        reviewed_path,
        max_bytes=MAX_SOURCE_BYTES,
    )
    nuwa_binding = _capture_binding(
        nuwa_path,
        max_bytes=MAX_SOURCE_BYTES,
    )
    index_binding = _capture_binding(
        index_path,
        max_bytes=MAX_EVIDENCE_SOURCE_BYTES,
    )
    if (
        reviewed_binding["digest"] != _body_digest(reviewed_body)
        or nuwa_binding["digest"] != _body_digest(nuwa_body)
    ):
        raise MigrationError(
            "source_changed",
            "legacy source changed while its snapshot was bound",
        )
    bindings = [reviewed_binding, nuwa_binding, index_binding]
    reviewed_rows = _decode_jsonl(
        reviewed_body,
        "reviewed/profile_memories.jsonl",
    )
    nuwa = _decode_json(nuwa_body, "profile_nuwa.json")
    digest = _source_digest(reviewed_body, nuwa_body)
    checkpoint = _read_checkpoint(checkpoint_path, source_digest=digest)
    current = _validate_current_state(vault)
    source_digests = {
        "reviewed/profile_memories.jsonl": reviewed_binding["digest"],
        "profile_nuwa.json": nuwa_binding["digest"],
    }
    (
        raw_items,
        prepared,
        excluded,
        source_broken,
        broken_ids,
    ) = _prepare_candidates(
        vault=vault,
        reviewed_rows=reviewed_rows,
        nuwa=nuwa,
        source_digests=source_digests,
        index_digest=index_binding["digest"],
    )

    after_snapshot_prepared()
    _verify_bindings(bindings)
    existing_ids = set(current)
    candidate_ids = sorted(item["claim_id"] for item in raw_items)
    _verify_existing_migration_state(vault, raw_items, current)
    committed_claim_ids = sorted(set(candidate_ids) & existing_ids)
    if checkpoint and checkpoint["candidate_ids"] != candidate_ids:
        raise MigrationError(
            "malformed_checkpoint",
            "migration checkpoint candidate identities do not match source",
        )
    if checkpoint and not set(checkpoint["committed_claim_ids"]).issubset(
        set(committed_claim_ids)
    ):
        raise MigrationError(
            "checkpoint_state_mismatch",
            "migration checkpoint references an uncommitted Claim",
        )
    planned = [
        item for item in prepared if item["claim"]["claim_id"] not in existing_ids
    ]
    skipped = len(prepared) - len(planned)
    report = {
        "ok": True,
        "created": len(planned) if dry_run else 0,
        "skipped": skipped,
        "excluded": excluded,
        "confirmed": 0,
        "source_broken": source_broken,
        "broken_ids": broken_ids,
        "dry_run": bool(dry_run),
        "checkpoint_resumed": bool(
            checkpoint
            and bool(checkpoint["committed_claim_ids"])
            and not checkpoint["complete"]
        ),
        "source": str(reviewed_path),
        "nuwa_source": str(nuwa_path),
        "checkpoint": str(checkpoint_path),
        "source_digest": digest,
        "source_digests": dict(source_digests),
        "index_digest": index_binding["digest"],
    }
    if dry_run or not raw_items:
        return report

    store: Optional[ClaimStore] = None
    if planned:
        store = ClaimStore(vault)
    created = 0
    for item in prepared:
        claim_id = item["claim"]["claim_id"]
        if claim_id in existing_ids:
            continue
        _verify_bindings(bindings)
        try:
            assert store is not None
            store.create(
                item["claim"],
                expected_revision=0,
                request_id=item["request_id"],
                idempotency_key=item["idempotency_key"],
                actor=MIGRATION_ACTOR,
                reason="legacy profile candidate migration",
                event_id=item["event_id"],
                migration_source=item["migration_source"],
            )
        except InvalidTransition as exc:
            raise MigrationError(exc.code, str(exc)) from exc
        existing_ids.add(claim_id)
        created += 1
        after_claim_commit(created)
        _verify_bindings(bindings)
        committed_claim_ids = sorted(set(candidate_ids) & existing_ids)
        if created % checkpoint_every == 0:
            _write_checkpoint(
                checkpoint_path,
                source_digest=digest,
                index_digest=index_binding["digest"],
                candidate_ids=candidate_ids,
                committed_claim_ids=committed_claim_ids,
                complete=False,
            )
            _verify_bindings(bindings)
    _verify_bindings(bindings)
    committed_claim_ids = sorted(set(candidate_ids) & existing_ids)
    _write_checkpoint(
        checkpoint_path,
        source_digest=digest,
        index_digest=index_binding["digest"],
        candidate_ids=candidate_ids,
        committed_claim_ids=committed_claim_ids,
        complete=True,
    )
    _verify_bindings(bindings)
    report["created"] = created
    report["skipped"] = len(prepared) - created
    return report


def migrate_legacy_profile(
    vault_dir: Path,
    *,
    dry_run: bool = False,
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
) -> Dict[str, Any]:
    try:
        return _migrate_legacy_profile(
            vault_dir,
            dry_run=dry_run,
            checkpoint_every=checkpoint_every,
        )
    except MigrationError:
        raise
    except EvidenceCatalogError as exc:
        raise MigrationError(exc.code, str(exc)) from exc
    except EventPathError as exc:
        raise MigrationError(exc.code, str(exc)) from exc
    except EventCorruption as exc:
        raise MigrationError(exc.code, str(exc)) from exc
    except OSError as exc:
        raise MigrationError(
            "migration_io_error",
            "migration storage operation failed",
        ) from exc
