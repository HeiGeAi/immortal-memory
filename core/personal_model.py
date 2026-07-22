#!/usr/bin/env python3
"""Compile a bounded, reviewable Personal Model from Immortal's derived layers.

This module deliberately does not read the raw memory vault.  It only consumes
the already-derived profile and Nuwa report, then keeps user corrections in a
separate append-only ledger added in later iterations of this module.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import configured_vault_dir
from file_utils import atomic_write_json, atomic_write_text
from redact_common import redact


IMMORTAL_DIR = configured_vault_dir()
ALLOWED_SCOPES = {"memory", "persona", "judgment", "boundary"}
CORRECTION_ID_RE = re.compile(r"pmc-[0-9a-f]{32}")


def _paths(vault_dir: Path | None = None) -> dict[str, Path]:
    root = Path(vault_dir) if vault_dir is not None else Path(IMMORTAL_DIR)
    models_dir = root / "models"
    return {
        "root": root,
        "models_dir": models_dir,
        "profile": root / "profile.json",
        "profile_nuwa": root / "profile_nuwa.json",
        "model_json": models_dir / "personal_model.json",
        "model_md": models_dir / "personal_model.md",
        "corrections": models_dir / "personal_model_corrections.jsonl",
        "actions": root / "runtime" / "personal_model_actions.jsonl",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one durable ledger event while serializing local writers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_jsonl_open_handle(handle: Any, payload: dict[str, Any]) -> None:
    """Write one complete event to an already exclusively locked JSONL file."""

    handle.seek(0, os.SEEK_END)
    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _normalize_scope(scope: Any) -> str:
    if not isinstance(scope, str):
        raise ValueError("correction scope must be a string")
    value = scope.strip()
    if value not in ALLOWED_SCOPES:
        raise ValueError("invalid correction scope")
    return value


def _normalize_statement(statement: Any) -> str:
    if not isinstance(statement, str):
        raise ValueError("correction statement must be a string")
    value = statement.strip()
    if not 4 <= len(value) <= 600:
        raise ValueError("correction statement must be 4 to 600 characters")
    if redact(value) != value:
        raise ValueError("sensitive credential is not allowed in correction")
    return value


def _valid_correction_id(correction_id: Any) -> str:
    if not isinstance(correction_id, str):
        raise ValueError("correction id must be a string")
    value = correction_id.strip()
    if not CORRECTION_ID_RE.fullmatch(value):
        raise ValueError("invalid correction id")
    return value


def _replay_correction_lines(lines: Any) -> tuple[list[dict[str, Any]], int, int]:
    """Replay an append-only ledger without mutating past events.

    Invalid historical rows are excluded from the working model and make its
    quality gate attentive. This protects the runtime context from legacy or
    hand-edited unsafe ledger content.
    """

    records: dict[str, dict[str, Any]] = {}
    event_count = 0
    invalid_count = 0
    for sequence, line in enumerate(lines):
        if not line.strip():
            continue
        event_count += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_count += 1
            continue
        if not isinstance(event, dict):
            invalid_count += 1
            continue
        kind = str(event.get("event") or "")
        try:
            correction_id = _valid_correction_id(event.get("id"))
        except ValueError:
            invalid_count += 1
            continue
        if kind == "add":
            if correction_id in records:
                invalid_count += 1
                continue
            try:
                scope = _normalize_scope(event.get("scope"))
                statement = _normalize_statement(event.get("statement"))
            except ValueError:
                invalid_count += 1
                continue
            records[correction_id] = {
                "id": correction_id,
                "scope": scope,
                "statement": statement,
                "created_at": str(event.get("created_at") or ""),
                "active": True,
                "sequence": sequence,
            }
        elif kind == "revoke":
            record = records.get(correction_id)
            if record is None or not record["active"]:
                invalid_count += 1
                continue
            record["active"] = False
            record["revoked_at"] = str(event.get("created_at") or "")
        else:
            invalid_count += 1
    ordered = sorted(records.values(), key=lambda item: int(item["sequence"]))
    for item in ordered:
        item.pop("sequence", None)
    return ordered, event_count, invalid_count


def _replay_corrections(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0, 0
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _replay_correction_lines(handle)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _validation_summary(value: Any) -> dict[str, bool]:
    validation = value if isinstance(value, dict) else {}
    return {
        "cross_domain_recurrence": bool(validation.get("cross_domain_recurrence")),
        "generative_power": bool(validation.get("generative_power")),
        "distinctiveness": bool(validation.get("distinctiveness")),
    }


def _accepted_models(nuwa: dict[str, Any]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for item in _as_list(nuwa.get("mental_models")):
        if item.get("status") != "accepted":
            continue
        domains = [str(domain) for domain in item.get("domains") or [] if str(domain)]
        validation = _validation_summary(item.get("validation"))
        accepted.append(
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or "未命名模型"),
                "source_count": int(item.get("source_count") or 0),
                "domain_count": len(domains),
                "domains": domains,
                "validation": validation,
                "confidence": "high" if all(validation.values()) else "attention",
            }
        )
    return accepted


def _heuristics(nuwa: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _as_list(nuwa.get("decision_heuristics")):
        domains = [str(domain) for domain in item.get("domains") or [] if str(domain)]
        result.append(
            {
                "title": str(item.get("title") or "未命名启发式"),
                "confidence": str(item.get("confidence") or "unknown"),
                "evidence_count": int(item.get("evidence_count") or 0),
                "domain_count": len(domains),
            }
        )
    return result


def _expression_dna(nuwa: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _as_list(nuwa.get("expression_dna")):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        result.append({"name": name, "support": int(item.get("support") or 0)})
    return result


def _quality_gate(
    *,
    nuwa_available: bool,
    mental_models: list[dict[str, Any]],
    boundaries: list[str],
    invalid_correction_events: int,
) -> dict[str, Any]:
    all_validated = bool(mental_models) and all(
        all((model.get("validation") or {}).values()) for model in mental_models
    )
    checks = [
        {
            "name": "Nuwa 派生层可用",
            "ok": nuwa_available,
            "detail": "available" if nuwa_available else "missing",
        },
        {
            "name": "至少 3 条已接受模型",
            "ok": len(mental_models) >= 3,
            "detail": f"{len(mental_models)} accepted",
        },
        {
            "name": "已接受模型通过三重验证",
            "ok": all_validated,
            "detail": "complete" if all_validated else "incomplete",
        },
        {
            "name": "至少 3 条诚实边界",
            "ok": len(boundaries) >= 3,
            "detail": f"{len(boundaries)} boundaries",
        },
        {
            "name": "纠正账本可安全重放",
            "ok": invalid_correction_events == 0,
            "detail": f"{invalid_correction_events} invalid events",
        },
    ]
    return {
        "status": "ok" if all(item["ok"] for item in checks) else "attention",
        "checks": checks,
    }


def build_model(*, vault_dir: Path | None = None) -> dict[str, Any]:
    """Build an in-memory Personal Model without mutating its input layers."""

    paths = _paths(vault_dir)
    nuwa = _read_json(paths["profile_nuwa"])
    profile = _read_json(paths["profile"])
    corrections, correction_event_count, invalid_correction_events = _replay_corrections(
        paths["corrections"]
    )
    mental_models = _accepted_models(nuwa)
    boundaries = [
        str(item).strip()
        for item in (nuwa.get("honest_boundaries") or [])
        if str(item).strip()
    ]
    model = {
        "schema_version": 1,
        "kind": "immortal_personal_model",
        "generated_at": _now_iso(),
        "inputs": {
            "nuwa_generated_at": str(nuwa.get("generated_at") or ""),
            "profile_generated_at": str(profile.get("generated_at") or ""),
            "accepted_model_count": len(mental_models),
            "correction_event_count": correction_event_count,
            "active_correction_count": sum(
                1 for item in corrections if bool(item.get("active"))
            ),
        },
        "mental_models": mental_models,
        "decision_heuristics": _heuristics(nuwa),
        "expression_dna": _expression_dna(nuwa),
        "boundaries": boundaries,
        "corrections": corrections,
        "runtime_contract": [
            "Treat this as a task-local working model, not a permanent identity.",
            "Separate evidence, inference, and uncertainty.",
            "Do not make external commitments or claim to be the owner.",
            "Use original evidence for specific facts.",
        ],
        "quality_gate": _quality_gate(
            nuwa_available=bool(nuwa),
            mental_models=mental_models,
            boundaries=boundaries,
            invalid_correction_events=invalid_correction_events,
        ),
    }
    revision_payload = {
        key: value
        for key, value in model.items()
        if key not in {"generated_at", "revision"}
    }
    model["revision"] = hashlib.sha256(
        json.dumps(revision_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return model


def render_markdown(model: dict[str, Any]) -> str:
    """Render the local-only model, including explicitly supplied corrections."""

    lines = [
        "# Immortal Personal Model",
        "",
        f"Generated: {model.get('generated_at')}",
        f"Revision: {model.get('revision')}",
        f"Quality: {(model.get('quality_gate') or {}).get('status')}",
        "",
        "这是一份任务级工作模型，不是对用户身份的冒充，也不能替代原始证据。",
        "",
        "## Runtime Contract",
        "",
    ]
    lines.extend(f"- {item}" for item in model.get("runtime_contract") or [])
    lines.extend(["", "## Accepted Mental Models", ""])
    for item in model.get("mental_models") or []:
        validation = item.get("validation") or {}
        lines.append(f"- **{item.get('title')}** (`{item.get('id')}`)")
        lines.append(
            "  - sources: "
            f"{item.get('source_count')} / domains: {item.get('domain_count')} / "
            "validation: "
            f"cross-domain={validation.get('cross_domain_recurrence')}, "
            f"generative={validation.get('generative_power')}, "
            f"distinctive={validation.get('distinctiveness')}"
        )
    lines.extend(["", "## Decision Heuristics", ""])
    for item in model.get("decision_heuristics") or []:
        lines.append(
            f"- **{item.get('title')}**: confidence={item.get('confidence')}, "
            f"evidence={item.get('evidence_count')}"
        )
    lines.extend(["", "## Active User Corrections", ""])
    active = [item for item in model.get("corrections") or [] if item.get("active")]
    if not active:
        lines.append("- none")
    for item in active:
        lines.append(f"- [{item.get('scope')}] {item.get('statement')}")
    lines.extend(["", "## Honest Boundaries", ""])
    lines.extend(f"- {item}" for item in model.get("boundaries") or [])
    lines.extend(["", "## Quality Gate", ""])
    for item in (model.get("quality_gate") or {}).get("checks") or []:
        lines.append(
            f"- {'PASS' if item.get('ok') else 'ATTENTION'}: "
            f"{item.get('name')} ({item.get('detail')})"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(model: dict[str, Any], *, vault_dir: Path | None = None) -> None:
    paths = _paths(vault_dir)
    atomic_write_json(paths["model_json"], model)
    atomic_write_text(paths["model_md"], render_markdown(model))


def build_and_write(*, vault_dir: Path | None = None) -> dict[str, Any]:
    model = build_model(vault_dir=vault_dir)
    write_outputs(model, vault_dir=vault_dir)
    return model


def _correction_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.get("id") or ""),
        "scope": str(record.get("scope") or ""),
        "active": bool(record.get("active")),
        "created_at": str(record.get("created_at") or ""),
        "revoked_at": str(record.get("revoked_at") or ""),
    }


def _append_audit(
    paths: dict[str, Path],
    *,
    action: str,
    correction_id: str,
    scope: str,
    revision: str,
) -> None:
    _append_jsonl(
        paths["actions"],
        {
            "at": _now_iso(),
            "action": action,
            "correction_id": correction_id,
            "scope": scope,
            "revision": revision,
        },
    )


def add_correction(
    scope: str,
    statement: str,
    *,
    vault_dir: Path | None = None,
) -> dict[str, Any]:
    normalized_scope = _normalize_scope(scope)
    normalized_statement = _normalize_statement(statement)
    paths = _paths(vault_dir)
    event = {
        "event": "add",
        "id": "pmc-" + uuid.uuid4().hex,
        "scope": normalized_scope,
        "statement": normalized_statement,
        "created_at": _now_iso(),
    }
    _append_jsonl(paths["corrections"], event)
    model = build_and_write(vault_dir=vault_dir)
    _append_audit(
        paths,
        action="correction_added",
        correction_id=event["id"],
        scope=normalized_scope,
        revision=str(model.get("revision") or ""),
    )
    return _correction_summary({**event, "active": True})


def revoke_correction(
    correction_id: str,
    *,
    vault_dir: Path | None = None,
) -> dict[str, Any]:
    normalized_id = _valid_correction_id(correction_id)
    paths = _paths(vault_dir)
    event = {
        "event": "revoke",
        "id": normalized_id,
        "created_at": _now_iso(),
    }
    correction_path = paths["corrections"]
    correction_path.parent.mkdir(parents=True, exist_ok=True)
    with correction_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            corrections, _, _ = _replay_correction_lines(handle)
            record = next((item for item in corrections if item["id"] == normalized_id), None)
            if record is None:
                raise KeyError("correction not found")
            if not record["active"]:
                raise ValueError("correction is already revoked")
            _write_jsonl_open_handle(handle, event)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    model = build_and_write(vault_dir=vault_dir)
    _append_audit(
        paths,
        action="correction_revoked",
        correction_id=normalized_id,
        scope=str(record["scope"]),
        revision=str(model.get("revision") or ""),
    )
    return _correction_summary(
        {
            **record,
            "active": False,
            "revoked_at": event["created_at"],
        }
    )


def metadata(model: dict[str, Any]) -> dict[str, Any]:
    """Return dashboard-safe model metadata without correction bodies."""

    corrections = [
        _correction_summary(item)
        for item in model.get("corrections") or []
        if isinstance(item, dict)
    ]
    return {
        "available": True,
        "status": str((model.get("quality_gate") or {}).get("status") or "attention"),
        "generated_at": str(model.get("generated_at") or ""),
        "revision": str(model.get("revision") or ""),
        "accepted_models": len(model.get("mental_models") or []),
        "heuristics": len(model.get("decision_heuristics") or []),
        "boundaries": len(model.get("boundaries") or []),
        "active_corrections": sum(1 for item in corrections if item.get("active")),
        "corrections": corrections,
        "quality_checks": list((model.get("quality_gate") or {}).get("checks") or []),
    }


def _summary_line(model: dict[str, Any]) -> str:
    status = str((model.get("quality_gate") or {}).get("status") or "attention")
    return (
        "Personal Model built: "
        f"{len(model.get('mental_models') or [])} accepted models, "
        f"{len(model.get('decision_heuristics') or [])} heuristics, "
        f"{metadata(model)['active_corrections']} active corrections, "
        f"quality={status}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build Immortal's local Personal Model")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("build", help="Compile the current derived model")
    sub.add_parser("status", help="Show body-free model metadata")
    correct = sub.add_parser("correct", help="Append a user-confirmed correction")
    correct.add_argument("--scope", required=True, choices=sorted(ALLOWED_SCOPES))
    correct.add_argument("--statement", required=True)
    revoke = sub.add_parser("revoke", help="Append a correction revocation event")
    revoke.add_argument("correction_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "build"
    if command == "status":
        saved = _read_json(_paths()["model_json"])
        if not saved:
            print(json.dumps({"available": False, "status": "missing"}, ensure_ascii=False))
            return 2
        print(json.dumps(metadata(saved), ensure_ascii=False, sort_keys=True))
        return 0 if metadata(saved)["status"] == "ok" else 2
    if command == "correct":
        result = add_correction(args.scope, args.statement)
        model = _read_json(_paths()["model_json"])
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if metadata(model).get("status") == "ok" else 2
    if command == "revoke":
        result = revoke_correction(args.correction_id)
        model = _read_json(_paths()["model_json"])
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if metadata(model).get("status") == "ok" else 2
    model = build_and_write()
    print(_summary_line(model))
    print(f"Wrote: {_paths()['model_json']}")
    print(f"Wrote: {_paths()['model_md']}")
    return 0 if metadata(model)["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
