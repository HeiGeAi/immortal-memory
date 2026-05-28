#!/usr/bin/env python3
"""Automatically review Feishu-derived long-term profile candidates.

The safety boundary is the same as the manual review desk: approved rows only
enter ~/.immortal/reviewed/ and then profile.md/profile_compact.md. This script
does not write to digital-soul.md.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from feishu_distill import (
    filter_profile_review_rows,
    is_profile_review_memory,
    profile_review_exclusion_reason,
)
from profile_merge import render_reviewed_md, write_jsonl_atomic


HOME = Path.home()
SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = HOME / ".immortal"
DEFAULT_PROPOSAL = IMMORTAL_DIR / "feishu" / "distilled" / "profile_merge_proposal.md"
DEFAULT_PROFILE_MEMORIES = IMMORTAL_DIR / "feishu" / "distilled" / "profile_memories.jsonl"
DEFAULT_REVIEWED_DIR = IMMORTAL_DIR / "reviewed"
DEFAULT_REVIEWED_FILE = DEFAULT_REVIEWED_DIR / "profile_memories.jsonl"
DEFAULT_REVIEWED_MD = DEFAULT_REVIEWED_DIR / "profile_memories.md"
DEFAULT_REVIEW_STATE = DEFAULT_REVIEWED_DIR / "profile_review_state.json"
DEFAULT_MERGE_LOG = DEFAULT_REVIEWED_DIR / "profile_merge_log.jsonl"
DEFAULT_AUTO_LOG = DEFAULT_REVIEWED_DIR / "profile_auto_review_log.jsonl"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

PROPOSAL_ID_RE = re.compile(r"`([a-f0-9]{24})`")
CHECK_LINE_RE = re.compile(r"^-\s*\[[ xX]\]\s+(`([a-f0-9]{24})`.*)$")


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(dump_json(row) + "\n")


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


def parse_proposal_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    ids: list[str] = []
    for memory_id in PROPOSAL_ID_RE.findall(path.read_text(encoding="utf-8", errors="ignore")):
        if memory_id not in ids:
            ids.append(memory_id)
    return ids


def load_review_state(path: Path) -> dict[str, Any]:
    state = read_json(path, {})
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("rejected"), dict):
        state["rejected"] = {}
    if not isinstance(state.get("rejection_reasons"), dict):
        state["rejection_reasons"] = {}
    return state


def set_proposal_checkboxes(path: Path, approved_ids: set[str], rejected_ids: set[str]) -> bool:
    if not path.exists():
        return False
    changed = False
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = CHECK_LINE_RE.match(line.strip())
        if not match:
            out.append(line)
            continue
        memory_id = match.group(2)
        if memory_id in approved_ids:
            new_line = f"- [x] {match.group(1)}"
        elif memory_id in rejected_ids:
            new_line = f"- [ ] {match.group(1)}"
        else:
            new_line = line
        if new_line != line:
            changed = True
        out.append(new_line)
    if changed:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        tmp.replace(path)
    return changed


def rejection_reason(row: dict[str, Any], auto_approvable: set[str]) -> str:
    memory_id = str(row.get("memory_id") or "")
    explicit = profile_review_exclusion_reason(row)
    if explicit:
        return explicit
    candidate = dict(row)
    if is_profile_review_memory(candidate) and memory_id not in auto_approvable:
        return "duplicate_or_superseded"
    return candidate.get("profile_review_exclusion") or "quality_or_threshold"


def prune_reviewed_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Drop reviewed rows that no longer pass the current profile-attribution rules."""
    kept: list[dict[str, Any]] = []
    removed: dict[str, str] = {}
    for row in rows:
        memory_id = str(row.get("memory_id") or "")
        candidate = dict(row)
        if is_profile_review_memory(candidate):
            kept.append(row)
            continue
        reason = (
            candidate.get("profile_review_exclusion")
            or profile_review_exclusion_reason(candidate)
            or "quality_or_threshold"
        )
        if memory_id:
            removed[memory_id] = reason
    return kept, removed


def run_merge(args: argparse.Namespace) -> tuple[int, str]:
    cmd = [
        sys.executable,
        str(SKILL_DIR / "profile_merge.py"),
        "--proposal",
        str(args.proposal),
        "--memories",
        str(args.memories),
        "--reviewed-file",
        str(args.reviewed_file),
        "--reviewed-md",
        str(args.reviewed_md),
        "--log",
        str(args.merge_log),
        "--approved-by",
        "auto_review",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return result.returncode, (result.stdout + result.stderr).strip()


def run_profile() -> tuple[int, str]:
    cmd = [sys.executable, str(SKILL_DIR / "profile.py")]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return result.returncode, (result.stdout + result.stderr).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto-review and merge Feishu profile candidates")
    parser.add_argument("--proposal", type=Path, default=DEFAULT_PROPOSAL)
    parser.add_argument("--memories", type=Path, default=DEFAULT_PROFILE_MEMORIES)
    parser.add_argument("--reviewed-file", type=Path, default=DEFAULT_REVIEWED_FILE)
    parser.add_argument("--reviewed-md", type=Path, default=DEFAULT_REVIEWED_MD)
    parser.add_argument("--review-state", type=Path, default=DEFAULT_REVIEW_STATE)
    parser.add_argument("--merge-log", type=Path, default=DEFAULT_MERGE_LOG)
    parser.add_argument("--auto-log", type=Path, default=DEFAULT_AUTO_LOG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-merge", action="store_true", help="Only update proposal/review state")
    parser.add_argument("--no-refresh-profile", action="store_true", help="Do not run profile.py after merge")
    parser.add_argument(
        "--no-prune-reviewed",
        action="store_true",
        help="Do not remove previously reviewed rows that fail the current attribution filters",
    )
    parser.add_argument(
        "--reconsider-rejected",
        action="store_true",
        help="Allow auto-review to approve IDs that were previously rejected",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = now_local()

    proposal_ids = parse_proposal_ids(args.proposal)
    profile_rows = load_jsonl(args.memories)
    reviewed_rows = load_jsonl(args.reviewed_file)
    review_state = load_review_state(args.review_state)

    by_id = {str(row.get("memory_id")): row for row in profile_rows if row.get("memory_id")}
    if args.no_prune_reviewed:
        kept_reviewed_rows = reviewed_rows
        pruned_reviewed: dict[str, str] = {}
    else:
        kept_reviewed_rows, pruned_reviewed = prune_reviewed_rows(reviewed_rows)

    reviewed_ids = {str(row.get("memory_id")) for row in kept_reviewed_rows if row.get("memory_id")}
    previously_rejected = set(review_state.get("rejected", {}).keys())
    review_rows, filter_counters = filter_profile_review_rows(profile_rows)
    auto_approvable = {str(row.get("memory_id")) for row in review_rows if row.get("memory_id")}

    missing = [memory_id for memory_id in proposal_ids if memory_id not in by_id]
    approved: list[str] = []
    rejected: dict[str, str] = {}
    skipped_reviewed: list[str] = []
    skipped_rejected: list[str] = []

    for memory_id in proposal_ids:
        row = by_id.get(memory_id)
        if not row:
            continue
        if memory_id in reviewed_ids:
            skipped_reviewed.append(memory_id)
            continue
        if memory_id in previously_rejected and not args.reconsider_rejected:
            skipped_rejected.append(memory_id)
            continue
        if memory_id in auto_approvable:
            approved.append(memory_id)
            continue
        rejected[memory_id] = rejection_reason(row, auto_approvable)

    if not args.dry_run:
        if pruned_reviewed:
            write_jsonl_atomic(args.reviewed_file, kept_reviewed_rows)
            args.reviewed_md.parent.mkdir(parents=True, exist_ok=True)
            args.reviewed_md.write_text(render_reviewed_md(kept_reviewed_rows), encoding="utf-8")

        reject_checkbox_ids = set(rejected).union(skipped_rejected)
        if set_proposal_checkboxes(args.proposal, set(approved), reject_checkbox_ids):
            proposal_changed = True
        else:
            proposal_changed = False

        rejected_state = review_state.setdefault("rejected", {})
        reasons_state = review_state.setdefault("rejection_reasons", {})
        for memory_id, reason in rejected.items():
            rejected_state[memory_id] = started_at
            reasons_state[memory_id] = reason
        if args.reconsider_rejected:
            for memory_id in approved:
                rejected_state.pop(memory_id, None)
                reasons_state.pop(memory_id, None)
        review_state["decision_owner"] = "codex-auto"
        review_state["decision_note"] = (
            "Auto-review keeps only 用户本人/用户本人 long-term profile, business principles, "
            "and current strategy; noisy Bibi/account-specific/third-party/tool/candidate facts stay out."
        )
        review_state["auto_review"] = {
            "updated_at": started_at,
            "proposal": str(args.proposal),
            "memories": str(args.memories),
            "approved": approved,
            "auto_rejected": rejected,
            "skipped_already_reviewed": skipped_reviewed,
            "skipped_previously_rejected": skipped_rejected,
            "pruned_reviewed": pruned_reviewed,
            "missing": missing,
            "filter_counters": dict(sorted(filter_counters.items())),
        }
        review_state["updated_at"] = started_at
        write_json_atomic(args.review_state, review_state)
    else:
        proposal_changed = False

    merge_code = 0
    merge_output = ""
    profile_code = 0
    profile_output = ""
    if not args.dry_run and not args.no_merge:
        merge_code, merge_output = run_merge(args)
        if merge_code == 0 and not args.no_refresh_profile:
            profile_code, profile_output = run_profile()

    summary = {
        "timestamp": started_at,
        "proposal": str(args.proposal),
        "memories": str(args.memories),
        "proposal_candidates": len(proposal_ids),
        "approved": approved,
        "auto_rejected": rejected,
        "skipped_already_reviewed": skipped_reviewed,
        "skipped_previously_rejected": skipped_rejected,
        "pruned_reviewed": pruned_reviewed,
        "missing": missing,
        "proposal_changed": proposal_changed,
        "dry_run": args.dry_run,
        "merge_returncode": merge_code,
        "profile_returncode": profile_code,
    }
    if not args.dry_run:
        append_jsonl(args.auto_log, summary)

    print(f"proposal_candidates={len(proposal_ids)}")
    print(f"approved={len(approved)}")
    print(f"auto_rejected={len(rejected)}")
    print(f"skipped_already_reviewed={len(skipped_reviewed)}")
    print(f"skipped_previously_rejected={len(skipped_rejected)}")
    print(f"pruned_reviewed={len(pruned_reviewed)}")
    print(f"missing={len(missing)}")
    print(f"proposal_changed={str(proposal_changed).lower()}")
    print(f"review_state={args.review_state}")
    print(f"auto_log={args.auto_log}")
    if merge_output:
        print("merge_output:")
        print(merge_output)
    if profile_output:
        print("profile_output:")
        print(profile_output)
    if missing:
        print("Missing IDs:")
        for memory_id in missing[:20]:
            print(f"  {memory_id}")
    return 1 if missing or merge_code != 0 or profile_code != 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
