#!/usr/bin/env python3
"""Read-only readiness preflight for the Immortal Memory vault.

This module answers one question before any agent context is generated:
is the local memory actually usable, or does it only look usable?

It never writes to the vault, never initializes anything, and never
triggers collection. A missing or demo-only vault must surface as a
machine-readable failure instead of a plausible-looking context pack.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import configured_vault_dir


# Above this line count we trust orchestrator state instead of scanning
# index.jsonl, so preflight stays O(1) on large production vaults.
SMOKE_SCAN_LIMIT = 500

SMOKE_SOURCES = {"immortal-smoke"}
SMOKE_TYPES = {"smoke"}

DEFAULT_MAX_AGE_HOURS = 72.0

STATUS_READY = "ready"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"

EXIT_CODES = {STATUS_READY: 0, STATUS_DEGRADED: 2, STATUS_UNAVAILABLE: 3}


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_hours(value: str | None, now: datetime | None = None) -> float | None:
    parsed = parse_iso(value)
    if not parsed:
        return None
    reference = now or datetime.now(timezone.utc)
    return (reference - parsed.astimezone(timezone.utc)).total_seconds() / 3600


def count_records(index_file: Path, state_total: int | None) -> tuple[int, int]:
    """Return (real_record_count, smoke_record_count).

    Small vaults are scanned line by line so smoke-only installs are
    detected exactly. Large vaults trust state and are never smoke-only
    in practice, so a full scan of a multi-GB index is avoided.
    """
    if state_total and state_total > SMOKE_SCAN_LIMIT:
        return state_total, 0
    real = 0
    smoke = 0
    if not index_file.exists():
        return 0, 0
    try:
        with index_file.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    record.get("type") in SMOKE_TYPES
                    or record.get("source") in SMOKE_SOURCES
                ):
                    smoke += 1
                else:
                    real += 1
    except OSError:
        return 0, 0
    return real, smoke


def storage_location(target: Path, vault: Path) -> str:
    """Classify where an export lives relative to the vault."""
    try:
        resolved_target = target.resolve()
        resolved_vault = vault.resolve()
    except OSError:
        return "unknown"
    if resolved_vault == resolved_target or resolved_vault in resolved_target.parents:
        return "internal_vault"
    probe = resolved_target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        if vault.exists() and probe.exists() and os.stat(probe).st_dev == os.stat(resolved_vault).st_dev:
            return "same_disk"
    except OSError:
        return "unknown"
    return "external_disk"


def scheduler_installed(vault: Path) -> bool:
    """LaunchAgent plist or cron marker present for this HOME. Read-only."""
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    if agents_dir.exists():
        for plist in agents_dir.glob("*.plist"):
            name = plist.name.lower()
            if "immortal" in name or "daily-backup" in name:
                return True
    return (vault / "daily-backup.sh").exists() and (vault / "backup.log").exists()


def gather_preflight(
    query: str = "",
    since: str | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    vault_dir: str | Path | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    vault = Path(vault_dir).expanduser() if vault_dir else configured_vault_dir()
    index_file = vault / "index.jsonl"
    state = read_json(vault / "orchestrator_state.json", {})
    sources = read_json(vault / "sources.json", {"sources": []}).get("sources", [])
    quality = read_json(vault / "quality" / "latest.json", {})

    state_total = state.get("total_records") if isinstance(state.get("total_records"), int) else None
    real_records, smoke_records = count_records(index_file, state_total)

    last_collect = state.get("last_collect")
    collect_age = age_hours(last_collect, now)

    # vault_status
    reasons: list[str] = []
    if not vault.exists() or not index_file.exists():
        vault_status = "missing"
        reasons.append("vault_or_index_missing")
    elif real_records == 0 and smoke_records > 0:
        vault_status = "smoke_only"
        reasons.append("only_smoke_records_in_index")
    elif real_records == 0:
        vault_status = "missing"
        reasons.append("index_has_no_records")
    elif collect_age is None:
        vault_status = "stale"
        reasons.append("last_collect_unknown")
    elif collect_age > max_age_hours:
        vault_status = "stale"
        reasons.append(f"last_collect_{collect_age:.1f}h_ago_exceeds_{max_age_hours:.0f}h")
    else:
        vault_status = "healthy"

    # query time coverage
    since_dt = parse_iso(since) if since else None
    collect_dt = parse_iso(last_collect)
    if collect_dt is None:
        query_coverage = "none"
        coverage_gap_hours = None
    else:
        coverage_gap_hours = round(max(0.0, (now - collect_dt.astimezone(timezone.utc)).total_seconds() / 3600), 1)
        if since_dt and collect_dt.astimezone(timezone.utc) < since_dt.astimezone(timezone.utc):
            query_coverage = "no_overlap"
            reasons.append("last_collect_older_than_requested_window")
        elif coverage_gap_hours > max_age_hours:
            query_coverage = "partial"
            reasons.append(f"coverage_gap_{coverage_gap_hours:.0f}h")
        else:
            query_coverage = "ok"

    # backup / restore posture
    export_dir = state.get("last_portable_export_dir")
    export_location = storage_location(Path(str(export_dir)), vault) if export_dir else "none"
    latest_external_export = (
        state.get("last_portable_export")
        if export_dir and export_location == "external_disk"
        else None
    )
    restore_status = state.get("last_portable_restore_check_status")
    last_restore_check = (
        state.get("last_portable_restore_check") if restore_status == "ok" else None
    )

    quality_status = quality.get("status") if isinstance(quality, dict) else None
    quality_score = quality.get("score") if isinstance(quality, dict) else None

    # context_status
    if vault_status in {"missing", "smoke_only"}:
        context_status = STATUS_UNAVAILABLE
    elif vault_status == "stale" or query_coverage in {"no_overlap", "partial", "none"}:
        context_status = STATUS_DEGRADED
    elif quality_status not in {None, "", "ok", "attention"}:
        context_status = STATUS_DEGRADED
        reasons.append(f"quality_status_{quality_status}")
    else:
        context_status = STATUS_READY

    protected = bool(
        vault_status == "healthy"
        and scheduler_installed(vault)
        and latest_external_export
        and last_restore_check
    )
    if not protected:
        if not scheduler_installed(vault):
            reasons.append("daily_scheduler_absent")
        if not latest_external_export:
            reasons.append("no_external_export")
        if not last_restore_check:
            reasons.append("no_passing_restore_check")

    return {
        "generated_at": now.isoformat(),
        "core_status": "ready",
        "vault_dir": str(vault),
        "vault_status": vault_status,
        "context_status": context_status,
        "loss_protection": "protected" if protected else "unprotected",
        "real_record_count": real_records,
        "smoke_record_count": smoke_records,
        "last_real_collect_at": last_collect,
        "collect_age_hours": round(collect_age, 1) if collect_age is not None else None,
        "source_coverage": {
            "configured_sources": len(sources),
            "collect_count": state.get("collect_count"),
        },
        "query": query or None,
        "query_since": since,
        "query_coverage": query_coverage,
        "coverage_gap_hours": coverage_gap_hours,
        "quality_status": quality_status,
        "quality_score": quality_score,
        "latest_export_dir": export_dir,
        "latest_export_location": export_location,
        "latest_external_export": latest_external_export,
        "last_restore_check": last_restore_check,
        "max_age_hours": max_age_hours,
        "reasons": reasons,
    }


def render_summary(payload: dict[str, Any]) -> str:
    lines = [
        "Immortal Preflight",
        "",
        f"context_status: {payload['context_status']}",
        f"vault_status: {payload['vault_status']}",
        f"loss_protection: {payload['loss_protection']}",
        f"real_record_count: {payload['real_record_count']} (smoke: {payload['smoke_record_count']})",
        f"last_real_collect_at: {payload['last_real_collect_at'] or 'never'}",
        f"query_coverage: {payload['query_coverage']}"
        + (
            f" (gap {payload['coverage_gap_hours']:.0f}h)"
            if payload.get("coverage_gap_hours")
            else ""
        ),
        f"latest_external_export: {payload['latest_external_export'] or 'none'}",
        f"last_restore_check: {payload['last_restore_check'] or 'none'}",
    ]
    if payload["reasons"]:
        lines.append("reasons: " + "; ".join(payload["reasons"]))
    if payload["context_status"] == STATUS_UNAVAILABLE:
        lines.extend(
            [
                "",
                "长期记忆当前不可用：这个 vault 尚未沉淀真实数据，你没有受到记忆保护。",
                "不要基于本状态自动初始化、重建或重新训练；把状态如实报告给用户。",
            ]
        )
    elif payload["context_status"] == STATUS_DEGRADED:
        lines.extend(
            [
                "",
                "长期记忆可用但已降级：引用历史结论前先向用户说明覆盖缺口。",
            ]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only readiness preflight for the Immortal vault")
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--since", default=None)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--vault-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    payload = gather_preflight(
        query=args.query,
        since=args.since,
        max_age_hours=args.max_age_hours,
        vault_dir=args.vault_dir,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_summary(payload))
    return EXIT_CODES[payload["context_status"]]


if __name__ == "__main__":
    raise SystemExit(main())
