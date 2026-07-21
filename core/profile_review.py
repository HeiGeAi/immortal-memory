#!/usr/bin/env python3
"""Local review desk for Feishu-derived long-term profile candidates.

The review desk is intentionally local-only. It writes approvals back to the
Markdown proposal that profile_merge.py already trusts, and stores rejections in
an isolated UI state file so the original candidate layer stays reversible.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
import uuid
import shutil
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from control_center import ControlCenter
from control_center_ui import control_center_page_html
from control_data import ControlData
from control_http import (
    LEGACY_SECURITY_HEADERS,
    SECURITY_HEADERS,
    error_page,
    error_payload,
    is_allowed_host,
)
from control_jobs import JobConflict, live_pid_lock, run_evidence_marker, sanitize_job_output
from process_utils import run_process
from pipeline_capabilities import missing_pipeline_stages
from product_data import ProductData
from product_http import (
    ProductHttpError,
    error_body,
    is_v2_get_target,
    is_v2_post_target,
    read_product_json,
    require_write_metadata,
    route_product_get,
    route_product_post,
)
from product_mutations import ProductMutationCoordinator
from product_ui import PRODUCT_ASSETS, PRODUCT_ASSET_ROOT, product_page_html


HOME = Path.home()
SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = HOME / ".immortal"
DEFAULT_PROPOSAL = IMMORTAL_DIR / "feishu" / "distilled" / "profile_merge_proposal.md"
DEFAULT_PROFILE_MEMORIES = IMMORTAL_DIR / "feishu" / "distilled" / "profile_memories.jsonl"
DEFAULT_REVIEWED_DIR = IMMORTAL_DIR / "reviewed"
DEFAULT_REVIEWED_FILE = DEFAULT_REVIEWED_DIR / "profile_memories.jsonl"
DEFAULT_REVIEW_STATE = DEFAULT_REVIEWED_DIR / "profile_review_state.json"
DEFAULT_DASHBOARD = IMMORTAL_DIR / "dashboard.html"
DEFAULT_TIMELINE = IMMORTAL_DIR / "timeline.html"
DEFAULT_SESSIONS_DIR = IMMORTAL_DIR / "sessions"
DEFAULT_AGENT_ENTRY = IMMORTAL_DIR / "agent" / "ENTRY.md"
DEFAULT_CONTROL_JOBS = IMMORTAL_DIR / "runtime" / "control_jobs.json"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
MEMORY_ID_RE = re.compile(r"`([a-f0-9]{24})`")
CHECK_LINE_RE = re.compile(r"^-\s*\[([ xX])\]\s+`([a-f0-9]{24})`")

JOB_HISTORY_LIMIT = 24
COMMAND_TIMEOUTS = {
    "collect": 3600,
    "clean": 2400,
    "distill": 2400,
    "profile_auto_review": 1200,
    "profile": 900,
    "profile_nuwa": 900,
    "people": 900,
    "relationships": 900,
    "quality": 900,
    "digest": 600,
    "dashboard": 900,
    "task_compile": 600,
    "health": 240,
}

ROLE_MODES = {
    "auto",
    "advisor",
    "writer",
    "reviewer",
    "business",
    "project",
    "shadow",
    "custom",
}


@dataclass(frozen=True)
class StageCommand:
    stage_id: str
    argv: tuple[str, ...]
    timeout: int


class FullPipelineUnavailable(RuntimeError):
    def __init__(self, missing_stages: tuple[str, ...]):
        self.missing_stages = missing_stages
        super().__init__("v1.1 full pipeline unavailable: " + ", ".join(missing_stages))


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


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


def parse_proposal(path: Path) -> tuple[list[str], set[str]]:
    ids: list[str] = []
    checked: set[str] = set()
    if not path.exists():
        return ids, checked
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = CHECK_LINE_RE.match(line.strip())
        if match:
            memory_id = match.group(2)
            ids.append(memory_id)
            if match.group(1).lower() == "x":
                checked.add(memory_id)
            continue
        id_match = MEMORY_ID_RE.search(line)
        if id_match:
            memory_id = id_match.group(1)
            if memory_id not in ids:
                ids.append(memory_id)
    return ids, checked


def set_proposal_checkbox(path: Path, memory_id: str, checked: bool) -> bool:
    if not path.exists():
        return False
    mark = "x" if checked else " "
    changed = False
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(rf"^-\s*\[[ xX]\]\s+(`{re.escape(memory_id)}`.*)$", line)
        if match:
            out.append(f"- [{mark}] {match.group(1)}")
            changed = True
        else:
            out.append(line)
    if changed:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        tmp.replace(path)
    return changed


def load_review_state(path: Path) -> dict[str, Any]:
    state = read_json(path, {})
    if not isinstance(state, dict):
        state = {}
    rejected = state.get("rejected")
    if not isinstance(rejected, dict):
        state["rejected"] = {}
    return state


def save_review_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_local()
    write_json_atomic(path, state)


def as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def source_title(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    return str(source.get("title") or "unknown source")


def file_mtime(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ).isoformat(timespec="seconds")


class ReviewStore:
    def __init__(
        self,
        proposal: Path,
        memories: Path,
        reviewed: Path,
        review_state: Path,
        *,
        audit_path: Path | None = None,
    ) -> None:
        self.proposal = proposal
        self.memories = memories
        self.reviewed = reviewed
        self.review_state = review_state
        if audit_path is None:
            vault = review_state.parent.parent if review_state.parent.name == "reviewed" else review_state.parent
            audit_path = vault / "runtime" / "profile_actions.jsonl"
        self.audit_path = Path(audit_path)
        self.audit_lock = threading.Lock()

    def _candidate_rows(self) -> list[dict[str, Any]]:
        proposal_ids, checked_ids = parse_proposal(self.proposal)
        memory_rows = load_jsonl(self.memories)
        reviewed_rows = load_jsonl(self.reviewed)
        by_id = {row.get("memory_id"): row for row in memory_rows if row.get("memory_id")}
        reviewed_ids = {row.get("memory_id") for row in reviewed_rows if row.get("memory_id")}
        ui_state = load_review_state(self.review_state)
        rejected = ui_state.get("rejected", {})

        if not proposal_ids:
            proposal_ids = [row.get("memory_id") for row in memory_rows[:80] if row.get("memory_id")]

        candidates = []
        for memory_id in proposal_ids:
            row = by_id.get(memory_id)
            if not row:
                continue
            review_state = "pending"
            if memory_id in reviewed_ids:
                review_state = "merged"
            elif memory_id in checked_ids:
                review_state = "selected"
            elif memory_id in rejected:
                review_state = "rejected"
            source = row.get("source") if isinstance(row.get("source"), dict) else {}
            candidates.append(
                {
                    "id": memory_id,
                    "statement": row.get("statement") or "",
                    "evidence": row.get("evidence") or "",
                    "focus": row.get("focus") or "other",
                    "memory_type": row.get("memory_type") or "other",
                    "confidence": row.get("confidence") or 0,
                    "relevance_score": row.get("relevance_score") or 0,
                    "sensitivity": row.get("sensitivity") or "internal",
                    "projects": as_list(row.get("projects")),
                    "people": as_list(row.get("people")),
                    "valid_from": row.get("valid_from") or "",
                    "source_title": source_title(row),
                    "source_kind": source.get("source") or "",
                    "source_url": source.get("url") or "",
                    "review_state": review_state,
                }
            )

        return candidates

    @staticmethod
    def _counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "total": len(candidates),
            "pending": sum(1 for item in candidates if item["review_state"] == "pending"),
            "selected": sum(1 for item in candidates if item["review_state"] == "selected"),
            "rejected": sum(1 for item in candidates if item["review_state"] == "rejected"),
            "merged": sum(1 for item in candidates if item["review_state"] == "merged"),
        }

    @staticmethod
    def _list_item(row: dict[str, Any]) -> dict[str, Any]:
        confidential = row.get("sensitivity") == "confidential"
        item = {
            key: value
            for key, value in row.items()
            if key not in {"evidence", "source_url"}
        }
        item["masked"] = confidential
        if confidential:
            item["statement"] = ""
            item["summary"] = "敏感内容，按需查看"
            item["source_title"] = "敏感来源"
        return item

    def list_candidates(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        review_state: str = "",
        focus: str = "",
        memory_type: str = "",
        sensitivity: str = "",
        query: str = "",
        sort: str = "priority",
    ) -> dict[str, Any]:
        safe_limit = min(50, max(1, int(limit)))
        safe_offset = max(0, int(offset))
        rows = self._candidate_rows()
        counts = self._counts(rows)
        needle = str(query or "").strip().casefold()
        filtered = []
        for row in rows:
            if review_state and row["review_state"] != review_state:
                continue
            if focus and row["focus"] != focus:
                continue
            if memory_type and row["memory_type"] != memory_type:
                continue
            if sensitivity and row["sensitivity"] != sensitivity:
                continue
            if needle and needle not in " ".join(
                (
                    str(row.get("statement") or ""),
                    str(row.get("evidence") or ""),
                    str(row.get("source_title") or ""),
                    " ".join(row.get("projects") or []),
                    " ".join(row.get("people") or []),
                )
            ).casefold():
                continue
            filtered.append(row)
        if sort == "confidence":
            filtered.sort(key=lambda item: (float(item["confidence"]), item["id"]), reverse=True)
        elif sort == "relevance":
            filtered.sort(key=lambda item: (float(item["relevance_score"]), item["id"]), reverse=True)
        elif sort == "date":
            filtered.sort(key=lambda item: (item["valid_from"], item["id"]), reverse=True)
        else:
            rank = {"selected": 0, "pending": 1, "rejected": 2, "merged": 3}
            filtered.sort(
                key=lambda item: (
                    rank.get(item["review_state"], 9),
                    -float(item["relevance_score"]),
                    -float(item["confidence"]),
                    item["id"],
                )
            )
        return {
            "items": [
                self._list_item(row)
                for row in filtered[safe_offset : safe_offset + safe_limit]
            ],
            "total": len(filtered),
            "offset": safe_offset,
            "limit": safe_limit,
            "has_more": safe_offset + safe_limit < len(filtered),
            "counts": counts,
        }

    def candidate_detail(self, memory_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{24}", str(memory_id or "")):
            raise ValueError("invalid memory id")
        row = next((item for item in self._candidate_rows() if item["id"] == memory_id), None)
        if not row:
            raise KeyError("candidate not found")
        return {**row, "masked": False}

    def build_state(self) -> dict[str, Any]:
        page = self.list_candidates(limit=50, offset=0)
        all_memories = load_jsonl(self.memories)
        reviewed_rows = load_jsonl(self.reviewed)
        counts = {
            **page["counts"],
            "reviewed_total": len(
                {row.get("memory_id") for row in reviewed_rows if row.get("memory_id")}
            ),
            "all_profile_memories": len(all_memories),
        }
        return {
            "counts": counts,
            "candidates": page["items"],
            "pagination": {
                "total": page["total"],
                "offset": page["offset"],
                "limit": page["limit"],
                "has_more": page["has_more"],
            },
            "paths": {
                "proposal": str(self.proposal),
                "profile_memories": str(self.memories),
                "reviewed": str(self.reviewed),
                "review_state": str(self.review_state),
            },
            "updated": {
                "proposal": file_mtime(self.proposal),
                "profile_memories": file_mtime(self.memories),
                "reviewed": file_mtime(self.reviewed),
                "review_state": file_mtime(self.review_state),
            },
        }

    def _audit_action(
        self,
        action: str,
        target_id: str,
        *,
        result: str,
        recoverable: bool,
    ) -> None:
        event = {
            "at": now_local(),
            "action": action,
            "target_id": target_id,
            "result": result,
            "recoverable": recoverable,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_lock:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            self.audit_path.chmod(0o600)

    def update_review(self, memory_id: str, action: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{24}", memory_id):
            raise ValueError("invalid memory id")
        proposal_ids, _checked = parse_proposal(self.proposal)
        if memory_id not in proposal_ids:
            raise ValueError("memory id is not in the current proposal")

        state = load_review_state(self.review_state)
        rejected = state.setdefault("rejected", {})

        if action in {"approve", "select"}:
            if not set_proposal_checkbox(self.proposal, memory_id, True):
                raise ValueError("failed to update proposal checkbox")
            rejected.pop(memory_id, None)
        elif action in {"unapprove", "unselect", "pending"}:
            if not set_proposal_checkbox(self.proposal, memory_id, False):
                raise ValueError("failed to update proposal checkbox")
            rejected.pop(memory_id, None)
        elif action == "reject":
            if not set_proposal_checkbox(self.proposal, memory_id, False):
                raise ValueError("failed to update proposal checkbox")
            rejected[memory_id] = now_local()
        elif action == "restore":
            rejected.pop(memory_id, None)
        else:
            raise ValueError("unknown action")

        save_review_state(self.review_state, state)
        canonical_action = {
            "select": "approve",
            "unselect": "unapprove",
            "pending": "unapprove",
        }.get(action, action)
        self._audit_action(
            canonical_action,
            memory_id,
            result="ok",
            recoverable=True,
        )
        return self.build_state()

    def merge(self) -> dict[str, Any]:
        started = time.time()
        merge_cmd = [sys.executable, str(SKILL_DIR / "profile_merge.py")]
        profile_cmd = [sys.executable, str(SKILL_DIR / "profile.py")]
        merge = subprocess.run(merge_cmd, capture_output=True, text=True, timeout=120)
        if merge.returncode != 0:
            self._audit_action("merge", "selected", result="failed", recoverable=True)
            return {
                "ok": False,
                "step": "profile_merge",
                "returncode": merge.returncode,
                "stdout": sanitize_job_output(merge.stdout),
                "stderr": sanitize_job_output(merge.stderr),
            }
        profile = subprocess.run(profile_cmd, capture_output=True, text=True, timeout=180)
        self._audit_action(
            "merge",
            "selected",
            result="ok" if profile.returncode == 0 else "failed",
            recoverable=True,
        )
        return {
            "ok": profile.returncode == 0,
            "step": "profile",
            "returncode": profile.returncode,
            "stdout": sanitize_job_output((merge.stdout + "\n" + profile.stdout).strip()),
            "stderr": sanitize_job_output((merge.stderr + "\n" + profile.stderr).strip()),
            "elapsed_seconds": round(time.time() - started, 2),
            "state": self.build_state(),
        }


def fmt_bytes(size: int | float | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def file_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
        "size": fmt_bytes(path.stat().st_size) if path.exists() else "missing",
        "mtime": file_mtime(path),
    }


def latest_session_dirs() -> list[Path]:
    if not DEFAULT_SESSIONS_DIR.exists():
        return []
    dirs = [path for path in DEFAULT_SESSIONS_DIR.iterdir() if path.is_dir() and (path / "manifest.json").exists()]
    dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return dirs


def session_summary(path: Path) -> dict[str, Any]:
    data = read_json(path / "manifest.json", {})
    if not isinstance(data, dict):
        data = {}
    returncode = int(data.get("returncode") or 0)
    return {
        "slug": path.name,
        "path": str(path),
        "goal": data.get("query") or path.name,
        "mode": data.get("mode") or "",
        "mode_label": data.get("mode_label") or "",
        "skill_name": data.get("session_id") or path.name,
        "generated_at": data.get("generated_at") or file_mtime(path / "manifest.json"),
        "quality": "ok" if returncode == 0 else "attention",
        "evidence_count": 0,
        "installed": False,
        "expires_at": data.get("expires_at") or "",
        "files": {
            "TASK_CONTEXT.md": str(path / "TASK_CONTEXT.md"),
            "SYSTEM_PROMPT.md": str(path / "SYSTEM_PROMPT.md"),
            "manifest.json": str(path / "manifest.json"),
        },
    }


class FactoryStore:
    FULL_PIPELINE_STAGES = (
        "run",
        "claims-migrate",
        "profile-attribution-audit",
        "living-self-build",
        "cards-build",
        "context-preview",
    )
    def __init__(
        self,
        history_path: Path = DEFAULT_CONTROL_JOBS,
        *,
        immortal_dir: Path = IMMORTAL_DIR,
        skill_dir: Path = SKILL_DIR,
        runner: Any = run_process,
    ) -> None:
        self.history_path = Path(history_path)
        self.immortal_dir = Path(immortal_dir)
        self.skill_dir = Path(skill_dir)
        self.runner = runner
        self.lock = threading.Lock()
        saved = read_json(self.history_path, [])
        saved_jobs = saved if isinstance(saved, list) else []
        self.jobs: dict[str, dict[str, Any]] = {
            str(item["id"]): item
            for item in saved_jobs
            if isinstance(item, dict) and item.get("id")
        }
        interrupted = False
        for job in self.jobs.values():
            if job.get("status") in {"queued", "running", "cancel_requested"}:
                job["status"] = "interrupted"
                job["error_code"] = "service_restarted"
                job["error"] = "控制中心服务重启，任务结果无法继续确认"
                job["finished_at"] = now_local()
                interrupted = True
        if interrupted:
            with self.lock:
                self._persist_locked()

    def _persist_locked(self) -> None:
        jobs = sorted(self.jobs.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
        write_json_atomic(self.history_path, jobs[:JOB_HISTORY_LIMIT])

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
            return [self._public_job(item) for item in jobs[:JOB_HISTORY_LIMIT]]

    @staticmethod
    def _public_job(value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        for key in ("stdout", "stderr", "error", "summary"):
            result[key] = sanitize_job_output(str(result.get(key) or ""))
        result["commands"] = [
            sanitize_job_output(str(command))
            for command in (result.get("commands") or [])
        ]
        return result

    def snapshot(self) -> dict[str, Any]:
        state = read_json(IMMORTAL_DIR / "orchestrator_state.json", {})
        quality = read_json(IMMORTAL_DIR / "quality" / "latest.json", {})
        digest = read_json(IMMORTAL_DIR / "digests" / "latest.json", {})
        feishu_clean = read_json(IMMORTAL_DIR / "feishu" / "clean" / "coverage.json", {})
        feishu_distilled = read_json(IMMORTAL_DIR / "feishu" / "distilled" / "coverage.json", {})
        people = read_json(IMMORTAL_DIR / "people" / "people_index.json", {})
        roles = [session_summary(path) for path in latest_session_dirs()[:18]]
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {
            "now": now_local(),
            "state": {
                "last_collect": state.get("last_collect"),
                "last_feishu_collect": state.get("last_feishu_collect"),
                "last_feishu_clean": state.get("last_feishu_clean"),
                "last_feishu_distill": state.get("last_feishu_distill"),
                "last_profile": state.get("last_profile"),
                "last_profile_nuwa": state.get("last_profile_nuwa"),
                "last_role_distill": state.get("last_role_distill"),
                "last_task_compile": state.get("last_task_compile"),
                "total_records": state.get("total_records"),
                "collect_count": state.get("collect_count"),
                "last_run_new_records": state.get("last_run_new_records"),
                "last_run_feishu_new_records": state.get("last_run_feishu_new_records"),
                "errors": state.get("errors") or [],
            },
            "quality": {
                "status": quality.get("status"),
                "score": quality.get("score"),
                "issue_count": quality.get("issue_count"),
                "recommendation": quality.get("recommendation"),
            },
            "digest_status": ((digest.get("errors") or {}).get("status") if isinstance(digest, dict) else None),
            "layers": {
                "index": file_status(IMMORTAL_DIR / "index.jsonl"),
                "profile": file_status(IMMORTAL_DIR / "profile.json"),
                "profile_nuwa": file_status(IMMORTAL_DIR / "profile_nuwa.json"),
                "people": {
                    **file_status(IMMORTAL_DIR / "people" / "people_index.json"),
                    "count": len(people.get("people") or []) if isinstance(people.get("people"), list) else 0,
                },
                "feishu_clean": {
                    **file_status(IMMORTAL_DIR / "feishu" / "clean" / "coverage.json"),
                    "records": (feishu_clean.get("counters") or {}).get("records_written")
                    or (feishu_clean.get("counters") or {}).get("clean_records")
                    or 0,
                },
                "feishu_distilled": {
                    **file_status(IMMORTAL_DIR / "feishu" / "distilled" / "coverage.json"),
                    "memories": (feishu_distilled.get("counters") or {}).get("memories_written") or 0,
                    "profile_memories": (feishu_distilled.get("counters") or {}).get("profile_memories_written") or 0,
                },
            },
            "roles": roles,
            "jobs": jobs[:JOB_HISTORY_LIMIT],
            "commands": {
                "collect": "python3 ~/.codex/skills/immortal/immortal.py run",
                "clean": "python3 ~/.codex/skills/immortal/immortal.py feishu-clean && feishu-distill && profile-auto-review",
                "role": "python3 ~/.codex/skills/immortal/immortal.py task-compile \"目标\" --mode auto",
                "health": "python3 ~/.codex/skills/immortal/immortal.py health --max-age-hours 30",
            },
        }

    def start_job(self, kind: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        if kind not in {
            "collect",
            "clean",
            "full",
            "role",
            "session",
            "health",
            "run",
            "backup_verify",
            "profile_refresh",
        }:
            raise ValueError("unknown factory job kind")
        if kind in {"run", "collect", "full"} and self._orchestrator_is_active():
            raise JobConflict("Immortal 编排器已经运行")
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "created_at": now_local(),
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": None,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "commands": [],
            "summary": "",
            "error": "",
            "error_code": "",
            "body": dict(body),
        }
        with self.lock:
            self.jobs[job_id] = job
            self._persist_locked()
        thread = threading.Thread(target=self._run_job, args=(job_id, kind, body), daemon=True)
        thread.start()
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self._public_job(job) if job else None

    def _update_job(self, job_id: str, **updates: Any) -> None:
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(updates)
                self._persist_locked()

    def _append_output(self, job_id: str, stdout: str = "", stderr: str = "") -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            if stdout:
                job["stdout"] = sanitize_job_output(str(job.get("stdout") or "") + stdout)
            if stderr:
                job["stderr"] = sanitize_job_output(str(job.get("stderr") or "") + stderr)
            self._persist_locked()

    def _run_job(self, job_id: str, kind: str, body: dict[str, Any]) -> None:
        started = time.time()
        before_run = run_evidence_marker(self.immortal_dir / "runtime" / "current_run.json")
        self._update_job(job_id, status="running", started_at=now_local())
        try:
            commands = self._commands_for(kind, body)
            self._update_job(
                job_id,
                commands=[self._display_cmd(command) for command in commands],
            )
            last_code = 0
            attention = False
            completed_stage_ids: list[str] = []
            for command in commands:
                current = self.get_job(job_id) or {}
                if current.get("status") == "cancel_requested":
                    self._update_job(
                        job_id,
                        status="canceled",
                        error_code="canceled_at_safe_boundary",
                        summary="任务已在安全阶段边界停止。",
                    )
                    return
                self._append_output(
                    job_id,
                    stdout=f"$ {self._display_cmd(command)}\n",
                )
                result = self.runner(
                    list(command.argv),
                    capture_output=True,
                    text=True,
                    timeout=command.timeout,
                    cwd=str(self.skill_dir),
                    env={
                        **os.environ,
                        "IMMORTAL_TRIGGER": "control_center",
                        "IMMORTAL_CONTROL_JOB_ID": job_id,
                    },
                )
                last_code = result.returncode
                self._append_output(job_id, stdout=result.stdout, stderr=result.stderr)
                current = self.get_job(job_id) or {}
                if current.get("status") == "cancel_requested":
                    self._update_job(
                        job_id,
                        status="canceled",
                        error_code="canceled_at_safe_boundary",
                        summary="任务已在安全阶段边界停止。",
                        returncode=last_code,
                    )
                    return
                if result.returncode != 0:
                    if result.returncode == 2 and command.stage_id in {
                        "profile-nuwa",
                        "role-distill",
                        "task-compile",
                    }:
                        attention = True
                        continue
                    raise RuntimeError(
                        f"command failed with code {result.returncode}: "
                        f"{self._display_cmd(command)}"
                    )
                completed_stage_ids.append(command.stage_id)
            if kind in {"run", "collect", "full"}:
                after_run = run_evidence_marker(self.immortal_dir / "runtime" / "current_run.json")
                if not after_run or after_run == before_run or after_run[3] != "success":
                    self._update_job(
                        job_id,
                        status="failed",
                        error_code="run_not_observed",
                        error="命令已退出，但没有观察到新的成功运行证据",
                        returncode=last_code,
                    )
                    return
            self._update_job(
                job_id,
                status="attention" if attention else "success",
                returncode=2 if attention else last_code,
                summary=self._success_summary(
                    kind,
                    body,
                    tuple(completed_stage_ids),
                ),
            )
        except FullPipelineUnavailable as exc:
            self._update_job(
                job_id,
                status="failed",
                error_code="capability_unavailable",
                error=sanitize_job_output(str(exc)),
                summary="完整闭环尚不可用，缺少阶段：" + "、".join(exc.missing_stages),
                returncode=1,
            )
        except Exception as exc:
            self._update_job(
                job_id,
                status="failed",
                error_code="command_failed",
                error=sanitize_job_output(str(exc)),
                returncode=1,
            )
        finally:
            self._update_job(job_id, finished_at=now_local(), elapsed_seconds=round(time.time() - started, 2))
            self._cancel_marker(job_id).unlink(missing_ok=True)

    def _commands_for(self, kind: str, body: dict[str, Any]) -> list[StageCommand]:
        python = sys.executable
        immortal = str(self.skill_dir / "immortal.py")
        if kind == "collect":
            return [
                StageCommand(
                    "run",
                    (python, immortal, "run"),
                    COMMAND_TIMEOUTS["collect"],
                )
            ]
        if kind == "clean":
            return [
                StageCommand("feishu-clean", (python, immortal, "feishu-clean"), COMMAND_TIMEOUTS["clean"]),
                StageCommand("feishu-distill", (python, immortal, "feishu-distill"), COMMAND_TIMEOUTS["distill"]),
                StageCommand(
                    "profile-auto-review",
                    (python, immortal, "profile-auto-review", "--reconsider-rejected"),
                    COMMAND_TIMEOUTS["profile_auto_review"],
                ),
                StageCommand("profile", (python, immortal, "profile"), COMMAND_TIMEOUTS["profile"]),
                StageCommand("profile-nuwa", (python, immortal, "profile-nuwa"), COMMAND_TIMEOUTS["profile_nuwa"]),
                StageCommand("people", (python, immortal, "people"), COMMAND_TIMEOUTS["people"]),
                StageCommand("relationships", (python, immortal, "relationships"), COMMAND_TIMEOUTS["relationships"]),
                StageCommand("quality", (python, immortal, "quality"), COMMAND_TIMEOUTS["quality"]),
                StageCommand("digest", (python, immortal, "digest"), COMMAND_TIMEOUTS["digest"]),
                StageCommand(
                    "dashboard",
                    (python, str(self.skill_dir / "dashboard.py")),
                    COMMAND_TIMEOUTS["dashboard"],
                ),
            ]
        if kind == "full":
            goal = str(body.get("goal") or "当前任务").strip()[:120]
            mode = str(body.get("mode") or "auto")
            if mode not in ROLE_MODES:
                mode = "auto"
            missing = missing_pipeline_stages(self.skill_dir)
            if missing:
                raise FullPipelineUnavailable(missing)
            return [
                StageCommand("run", (python, immortal, "run"), COMMAND_TIMEOUTS["collect"]),
                StageCommand(
                    "claims-migrate",
                    (python, immortal, "claims-migrate"),
                    COMMAND_TIMEOUTS["profile"],
                ),
                StageCommand(
                    "profile-attribution-audit",
                    (python, immortal, "profile-attribution-audit", "--apply"),
                    COMMAND_TIMEOUTS["profile"],
                ),
                StageCommand(
                    "living-self-build",
                    (python, immortal, "living-self-build"),
                    COMMAND_TIMEOUTS["profile"],
                ),
                StageCommand(
                    "cards-build",
                    (python, immortal, "cards", "build"),
                    COMMAND_TIMEOUTS["profile"],
                ),
                StageCommand(
                    "context-preview",
                    (python, immortal, "context-preview", goal, "--mode", mode),
                    COMMAND_TIMEOUTS["task_compile"],
                ),
            ]
        if kind in {"role", "session"}:
            goal = str(body.get("goal") or "").strip()
            if not goal:
                raise ValueError("goal is required")
            mode = str(body.get("mode") or "auto")
            if mode not in ROLE_MODES:
                raise ValueError("invalid mode")
            return [
                StageCommand(
                    "task-compile",
                    (python, immortal, "task-compile", goal[:160], "--mode", mode),
                    COMMAND_TIMEOUTS["task_compile"],
                )
            ]
        if kind == "health":
            return [
                StageCommand(
                    "health",
                    (python, immortal, "health", "--max-age-hours", "30"),
                    COMMAND_TIMEOUTS["health"],
                )
            ]
        if kind == "run":
            return [StageCommand("run", (python, immortal, "run"), COMMAND_TIMEOUTS["collect"])]
        if kind == "backup_verify":
            return [
                StageCommand(
                    "backup-verify",
                    (python, immortal, "backup-status", "--verify", "--max-age-hours", "168"),
                    COMMAND_TIMEOUTS["health"],
                )
            ]
        if kind == "profile_refresh":
            return [
                StageCommand("profile", (python, immortal, "profile"), COMMAND_TIMEOUTS["profile"]),
                StageCommand("profile-nuwa", (python, immortal, "profile-nuwa"), COMMAND_TIMEOUTS["profile_nuwa"]),
                StageCommand("quality", (python, immortal, "quality"), COMMAND_TIMEOUTS["quality"]),
            ]
        raise ValueError("unknown factory job kind")

    def _orchestrator_is_active(self) -> bool:
        return live_pid_lock(self.immortal_dir / "orchestrator.lock")

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise KeyError("job not found")
        if job.get("status") not in {"queued", "running"}:
            raise ValueError("job is not cancellable")
        self._update_job(job_id, status="cancel_requested")
        write_json_atomic(
            self._cancel_marker(job_id),
            {"job_id": job_id, "requested_at": now_local()},
        )
        return self.get_job(job_id) or {}

    def _cancel_marker(self, job_id: str) -> Path:
        return self.immortal_dir / "runtime" / "cancel_requests" / f"{job_id}.json"

    def retry_job(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise KeyError("job not found")
        if job.get("status") not in {"failed", "canceled", "interrupted", "attention"}:
            raise ValueError("job is not retryable")
        return self.start_job(str(job.get("kind") or ""), dict(job.get("body") or {}))

    def job_logs(self, job_id: str, *, offset: int = 0, limit: int = 8000) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise KeyError("job not found")
        raw_text = "\n".join(
            part for part in (str(job.get("stdout") or ""), str(job.get("stderr") or "")) if part
        )
        text = sanitize_job_output(raw_text)
        safe_offset = max(0, int(offset))
        safe_limit = min(8000, max(1, int(limit)))
        return {
            "job_id": job_id,
            "offset": safe_offset,
            "next_offset": min(len(text), safe_offset + safe_limit),
            "total": len(text),
            "text": text[safe_offset : safe_offset + safe_limit],
        }

    def _success_summary(
        self,
        kind: str,
        body: dict[str, Any],
        completed_stage_ids: tuple[str, ...] | None = None,
    ) -> str:
        if completed_stage_ids is not None:
            return "已按真实命令完成：" + "、".join(completed_stage_ids) + "。"
        if kind == "collect":
            return "采集与自动链路已完成。"
        if kind == "clean":
            return "清洗、蒸馏、长期画像和看板已刷新。"
        if kind in {"role", "session"}:
            return f"任务上下文已生成：{body.get('goal') or ''}"
        if kind == "full":
            return "完整闭环已完成。"
        if kind == "health":
            return "健康检查已完成。"
        if kind == "run":
            return "Immortal 全流程已完成。"
        if kind == "backup_verify":
            return "最新便携备份校验已完成。"
        if kind == "profile_refresh":
            return "长期画像和质量报告已刷新。"
        return "任务完成。"

    @staticmethod
    def _display_cmd(command: StageCommand) -> str:
        display = []
        for item in command.argv:
            if item == sys.executable:
                display.append("python3")
            else:
                display.append(str(item))
        return " ".join(shlex_quote(part) for part in display)


def shlex_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def is_allowed_local_origin(value: str) -> bool:
    if not value:
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and parsed.username is None and parsed.password is None and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def page_html(title: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #050809;
  --bg2: #0b1113;
  --panel: rgba(12, 19, 22, .86);
  --panel2: rgba(17, 28, 30, .92);
  --line: rgba(128, 237, 207, .18);
  --line2: rgba(128, 237, 207, .38);
  --text: #edf7f4;
  --muted: #90aaa3;
  --faint: #58716b;
  --cyan: #7ff0d4;
  --lime: #c6f66f;
  --amber: #f3c760;
  --red: #ff7970;
  --blue: #8db7ff;
  --ink: #03100e;
  --radius: 8px;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; min-height: 100%; background: var(--bg); color: var(--text); }}
body {{
  font-family: "Avenir Next", "PingFang SC", "Microsoft YaHei", ui-sans-serif, system-ui, sans-serif;
  letter-spacing: 0;
  line-height: 1.55;
  background:
    linear-gradient(rgba(127,240,212,.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(127,240,212,.035) 1px, transparent 1px),
    radial-gradient(circle at 15% 6%, rgba(198,246,111,.12), transparent 28%),
    radial-gradient(circle at 80% 12%, rgba(141,183,255,.10), transparent 32%),
    linear-gradient(140deg, #030506 0%, #071012 48%, #10100d 100%);
  background-size: 34px 34px, 34px 34px, auto, auto, auto;
}}
body:before {{
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  background: repeating-linear-gradient(180deg, rgba(255,255,255,.024) 0 1px, transparent 1px 5px);
  opacity: .24;
  mix-blend-mode: overlay;
}}
button, input, select {{ font: inherit; letter-spacing: 0; }}
.shell {{ position: relative; z-index: 1; min-height: 100vh; display: grid; grid-template-columns: 292px minmax(0, 1fr); }}
.side {{
  min-height: 100vh;
  border-right: 1px solid var(--line);
  background: rgba(3, 8, 9, .62);
  backdrop-filter: blur(18px);
  padding: 24px 18px;
  position: sticky;
  top: 0;
  align-self: start;
}}
.brand-kicker {{ color: var(--cyan); font-size: 11px; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; }}
h1 {{ margin: 10px 0 18px; font-size: 35px; line-height: .98; font-weight: 840; }}
.mini {{ color: var(--muted); font-size: 12px; }}
.metrics {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 22px 0; }}
.metric {{ min-height: 76px; border: 1px solid var(--line); border-radius: var(--radius); padding: 11px; background: rgba(255,255,255,.028); }}
.metric .n {{ font-size: 23px; line-height: 1; font-weight: 820; color: var(--cyan); }}
.metric .l {{ margin-top: 8px; color: var(--muted); font-size: 11px; }}
.filter-title {{ margin: 18px 0 8px; color: #c8dfda; font-size: 11px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }}
.chips {{ display: flex; flex-wrap: wrap; gap: 7px; }}
.chip {{
  border: 1px solid rgba(255,255,255,.08);
  background: rgba(255,255,255,.035);
  color: var(--muted);
  border-radius: 999px;
  min-height: 30px;
  padding: 5px 10px;
  cursor: pointer;
  font-size: 12px;
}}
.chip.active {{ color: var(--ink); background: var(--cyan); border-color: var(--cyan); font-weight: 800; }}
.main {{ min-width: 0; padding: 28px clamp(18px, 3vw, 44px) 48px; }}
.topbar {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: start; margin-bottom: 18px; }}
.headline {{
  min-height: 154px;
  border: 1px solid var(--line2);
  border-radius: var(--radius);
  background:
    linear-gradient(120deg, rgba(127,240,212,.16), transparent 48%),
    rgba(7, 13, 15, .76);
  padding: 22px;
  overflow: hidden;
  position: relative;
}}
.headline:after {{
  content: "REVIEW";
  position: absolute;
  right: 18px;
  bottom: -5px;
  color: rgba(127,240,212,.09);
  font-size: clamp(58px, 10vw, 128px);
  font-weight: 920;
  line-height: .8;
}}
.headline h2 {{ position: relative; z-index: 1; margin: 0 0 10px; font-size: clamp(28px, 5vw, 58px); line-height: .92; }}
.headline p {{ position: relative; z-index: 1; max-width: 680px; margin: 0; color: #b9cbc6; font-size: 14px; }}
.actions {{ display: flex; gap: 9px; flex-wrap: wrap; justify-content: flex-end; }}
.btn {{
  min-height: 38px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  color: var(--text);
  background: rgba(8, 16, 18, .88);
  padding: 8px 12px;
  cursor: pointer;
}}
.btn:hover {{ border-color: var(--line2); background: rgba(127,240,212,.07); }}
.btn.primary {{ color: var(--ink); background: var(--lime); border-color: var(--lime); font-weight: 850; }}
.btn.danger {{ border-color: rgba(255,121,112,.34); color: #ffc2bd; }}
.btn:disabled {{ opacity: .45; cursor: not-allowed; }}
.toolbar {{
  display: grid;
  grid-template-columns: minmax(220px, 1fr) 170px 170px 160px;
  gap: 10px;
  margin-bottom: 14px;
}}
.field, .select {{
  width: 100%;
  min-height: 42px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: rgba(5, 10, 12, .84);
  color: var(--text);
  outline: none;
  padding: 9px 12px;
}}
.field:focus, .select:focus {{ border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(127,240,212,.08); }}
.statusline {{ display: flex; justify-content: space-between; gap: 12px; margin-bottom: 12px; color: var(--muted); font-size: 12px; }}
.cards {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
.card {{
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: linear-gradient(180deg, rgba(15, 25, 27, .90), rgba(6, 11, 12, .92));
  padding: 15px;
  min-height: 258px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-shadow: 0 18px 42px rgba(0,0,0,.22);
}}
.card.selected {{ border-color: rgba(198,246,111,.62); box-shadow: 0 0 0 1px rgba(198,246,111,.18), 0 18px 42px rgba(0,0,0,.26); }}
.card.rejected {{ opacity: .58; }}
.card.merged {{ border-color: rgba(141,183,255,.55); }}
.card-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
.tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.tag {{ border: 1px solid rgba(255,255,255,.08); border-radius: 999px; padding: 3px 8px; font-size: 11px; color: var(--muted); background: rgba(255,255,255,.028); }}
.tag.hot {{ color: var(--ink); background: var(--cyan); border-color: var(--cyan); font-weight: 800; }}
.tag.warn {{ color: #332200; background: var(--amber); border-color: var(--amber); font-weight: 800; }}
.id {{ color: var(--faint); font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; }}
.statement {{ font-size: 15px; color: #f2fbf8; }}
.meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; color: var(--muted); font-size: 12px; }}
.meta b {{ color: #d2e6e1; font-weight: 760; }}
.source {{ color: #bcd0cb; font-size: 12px; border-left: 2px solid var(--line2); padding-left: 9px; }}
.evidence {{ color: var(--muted); font-size: 12px; display: none; }}
.card.open .evidence {{ display: block; }}
.card-actions {{ margin-top: auto; display: flex; flex-wrap: wrap; gap: 8px; }}
.log {{
  display: none;
  margin: 10px 0 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: #020606;
  color: #bcd4cf;
  padding: 12px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 12px;
  white-space: pre-wrap;
  max-height: 220px;
  overflow: auto;
}}
.empty {{
  border: 1px dashed var(--line2);
  border-radius: var(--radius);
  padding: 24px;
  color: var(--muted);
  text-align: center;
}}
@media (max-width: 1100px) {{
  .shell {{ grid-template-columns: 1fr; }}
  .side {{ position: relative; min-height: auto; border-right: none; border-bottom: 1px solid var(--line); }}
  .toolbar {{ grid-template-columns: 1fr 1fr; }}
  .cards {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 680px) {{
  .main {{ padding: 18px 12px 34px; }}
  .topbar {{ grid-template-columns: 1fr; }}
  .actions {{ justify-content: flex-start; }}
  .toolbar {{ grid-template-columns: 1fr; }}
  .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .meta {{ grid-template-columns: 1fr; }}
  h1 {{ font-size: 30px; }}
}}
</style>
</head>
<body>
<div class="shell">
  <aside class="side">
    <div class="brand-kicker">IMMORTAL PROFILE</div>
    <h1>长期画像<br>审阅台</h1>
    <div class="mini" id="pathLine">本地只读候选层</div>
    <div class="metrics">
      <div class="metric"><div class="n" id="mTotal">0</div><div class="l">候选</div></div>
      <div class="metric"><div class="n" id="mSelected">0</div><div class="l">待合并</div></div>
      <div class="metric"><div class="n" id="mMerged">0</div><div class="l">已合并</div></div>
      <div class="metric"><div class="n" id="mRejected">0</div><div class="l">已跳过</div></div>
    </div>
    <div class="filter-title">状态</div>
    <div class="chips" id="stateChips"></div>
    <div class="filter-title">画像层</div>
    <div class="chips" id="focusChips"></div>
    <div class="filter-title">类型</div>
    <div class="chips" id="typeChips"></div>
  </aside>
  <main class="main">
    <div class="topbar">
      <section class="headline">
        <h2>自动审阅后的审计台</h2>
        <p>飞书蒸馏后的候选默认由自动审阅器处理。这里用于抽查、恢复、覆盖批准或跳过，所有合并仍只进入 reviewed/profile 层。</p>
      </section>
      <div class="actions">
        <button class="btn" id="refreshBtn">刷新</button>
        <button class="btn primary" id="mergeBtn">合并并刷新画像</button>
      </div>
    </div>
    <div class="toolbar">
      <input class="field" id="search" placeholder="搜索条目、来源、项目、人名">
      <select class="select" id="sort">
        <option value="priority">优先级</option>
        <option value="confidence">置信度</option>
        <option value="relevance">相关性</option>
        <option value="date">日期</option>
      </select>
      <select class="select" id="sensitivity">
        <option value="all">全部敏感级别</option>
        <option value="internal">internal</option>
        <option value="confidential">confidential</option>
        <option value="public">public</option>
      </select>
      <select class="select" id="density">
        <option value="normal">标准密度</option>
        <option value="compact">紧凑密度</option>
      </select>
    </div>
    <div class="log" id="log"></div>
    <div class="statusline"><span id="shown">0 条</span><span id="updated">未加载</span></div>
    <section class="cards" id="cards"></section>
  </main>
</div>

<script>
const labelMap = {{
  self_profile: '个人画像',
  current_project: '当前项目',
  company_context: '公司业务',
  other: '其他',
  preference: '偏好',
  decision: '决策',
  lesson: '教训',
  relationship: '关系',
  project_fact: '项目事实',
  commitment: '承诺',
  pending: '待处理',
  selected: '待合并',
  rejected: '已跳过',
  merged: '已合并'
}};
const filters = {{ state: 'all', focus: 'all', type: 'all' }};
let state = {{ candidates: [], counts: {{}}, paths: {{}}, updated: {{}} }};

function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}
function fmtNum(n) {{ return Number(n || 0).toLocaleString('zh-CN'); }}
function score(v) {{ return Math.round(Number(v || 0) * 100); }}
function label(v) {{ return labelMap[v] || v || 'unknown'; }}
function setLog(text, isError=false) {{
  const el = document.getElementById('log');
  el.style.display = text ? 'block' : 'none';
  el.style.borderColor = isError ? 'rgba(255,121,112,.55)' : 'var(--line)';
  el.textContent = text || '';
}}

async function api(path, options={{}}) {{
  const res = await fetch(path, {{
    ...options,
    headers: {{ 'Content-Type': 'application/json', ...(options.headers || {{}}) }}
  }});
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}}

async function load() {{
  setLog('');
  state = await api('/api/state');
  renderAll();
}}

function uniqueValues(key) {{
  return [...new Set(state.candidates.map(x => x[key]).filter(Boolean))];
}}

function renderChips(elId, key, values) {{
  const el = document.getElementById(elId);
  const all = ['all', ...values];
  el.innerHTML = all.map(v => `<button class="chip ${{filters[key] === v ? 'active' : ''}}" data-key="${{key}}" data-value="${{esc(v)}}">${{v === 'all' ? '全部' : label(v)}}</button>`).join('');
  el.querySelectorAll('button').forEach(btn => {{
    btn.onclick = () => {{
      filters[btn.dataset.key] = btn.dataset.value;
      renderAll();
    }};
  }});
}}

function filteredCandidates() {{
  const q = document.getElementById('search').value.trim().toLowerCase();
  const sens = document.getElementById('sensitivity').value;
  let rows = state.candidates.filter(item => {{
    if (filters.state !== 'all' && item.review_state !== filters.state) return false;
    if (filters.focus !== 'all' && item.focus !== filters.focus) return false;
    if (filters.type !== 'all' && item.memory_type !== filters.type) return false;
    if (sens !== 'all' && item.sensitivity !== sens) return false;
    if (!q) return true;
    const hay = [item.statement, item.evidence, item.source_title, item.focus, item.memory_type, item.sensitivity, ...(item.projects || []), ...(item.people || [])].join(' ').toLowerCase();
    return hay.includes(q);
  }});
  const sort = document.getElementById('sort').value;
  rows.sort((a, b) => {{
    if (sort === 'confidence') return Number(b.confidence || 0) - Number(a.confidence || 0);
    if (sort === 'relevance') return Number(b.relevance_score || 0) - Number(a.relevance_score || 0);
    if (sort === 'date') return String(b.valid_from || '').localeCompare(String(a.valid_from || ''));
    const rank = {{ selected: 0, pending: 1, rejected: 2, merged: 3 }};
    return (rank[a.review_state] ?? 9) - (rank[b.review_state] ?? 9)
      || Number(b.relevance_score || 0) - Number(a.relevance_score || 0)
      || Number(b.confidence || 0) - Number(a.confidence || 0);
  }});
  return rows;
}}

function renderAll() {{
  const c = state.counts || {{}};
  document.getElementById('mTotal').textContent = fmtNum(c.total);
  document.getElementById('mSelected').textContent = fmtNum(c.selected);
  document.getElementById('mMerged').textContent = fmtNum(c.merged);
  document.getElementById('mRejected').textContent = fmtNum(c.rejected);
  document.getElementById('pathLine').textContent = state.paths?.proposal || '';
  document.getElementById('updated').textContent = state.updated?.proposal ? `proposal 更新：${{state.updated.proposal}}` : '未加载';
  renderChips('stateChips', 'state', ['pending', 'selected', 'rejected', 'merged']);
  renderChips('focusChips', 'focus', uniqueValues('focus'));
  renderChips('typeChips', 'type', uniqueValues('memory_type'));
  renderCards();
}}

function cardActions(item) {{
  if (item.review_state === 'merged') {{
    return `<button class="btn" data-action="unapprove" data-id="${{item.id}}">撤回勾选</button><button class="btn" data-action="toggle" data-id="${{item.id}}">证据</button>`;
  }}
  if (item.review_state === 'selected') {{
    return `<button class="btn" data-action="unapprove" data-id="${{item.id}}">撤回</button><button class="btn danger" data-action="reject" data-id="${{item.id}}">跳过</button><button class="btn" data-action="toggle" data-id="${{item.id}}">证据</button>`;
  }}
  if (item.review_state === 'rejected') {{
    return `<button class="btn" data-action="restore" data-id="${{item.id}}">恢复</button><button class="btn primary" data-action="approve" data-id="${{item.id}}">批准</button><button class="btn" data-action="toggle" data-id="${{item.id}}">证据</button>`;
  }}
  return `<button class="btn primary" data-action="approve" data-id="${{item.id}}">批准</button><button class="btn danger" data-action="reject" data-id="${{item.id}}">跳过</button><button class="btn" data-action="toggle" data-id="${{item.id}}">证据</button>`;
}}

function renderCards() {{
  const rows = filteredCandidates();
  const compact = document.getElementById('density').value === 'compact';
  document.getElementById('shown').textContent = `${{rows.length}} 条 / ${{state.candidates.length}} 条`;
  const el = document.getElementById('cards');
  if (!rows.length) {{
    el.innerHTML = '<div class="empty">没有匹配条目</div>';
    return;
  }}
  el.innerHTML = rows.map(item => {{
    const cls = ['card', item.review_state];
    if (item.review_state === 'selected') cls.push('selected');
    const projects = (item.projects || []).slice(0, 3).map(x => `<span class="tag">${{esc(x)}}</span>`).join('');
    const people = (item.people || []).slice(0, 3).map(x => `<span class="tag">${{esc(x)}}</span>`).join('');
    return `<article class="${{cls.join(' ')}}" data-id="${{item.id}}">
      <div class="card-top">
        <div class="tags">
          <span class="tag hot">${{label(item.review_state)}}</span>
          <span class="tag">${{label(item.focus)}}</span>
          <span class="tag">${{label(item.memory_type)}}</span>
          ${{item.sensitivity === 'confidential' ? '<span class="tag warn">confidential</span>' : `<span class="tag">${{esc(item.sensitivity)}}</span>`}}
        </div>
        <div class="id">${{item.id}}</div>
      </div>
      <div class="statement">${{esc(item.statement)}}</div>
      <div class="meta">
        <div><b>${{score(item.confidence)}}%</b> 置信度</div>
        <div><b>${{score(item.relevance_score)}}%</b> 相关性</div>
        <div><b>${{esc(item.valid_from || '-')}}</b> 日期</div>
        <div><b>${{esc(item.source_kind || '-')}}</b> 来源类型</div>
      </div>
      <div class="source">${{esc(item.source_title)}}${{projects || people ? `<div class="tags" style="margin-top:7px">${{projects}}${{people}}</div>` : ''}}</div>
      ${{compact ? '' : `<div class="evidence">${{esc(item.evidence || item.statement)}}</div>`}}
      <div class="card-actions">${{cardActions(item)}}</div>
    </article>`;
  }}).join('');
  el.querySelectorAll('button[data-action]').forEach(btn => {{
    btn.onclick = () => handleAction(btn.dataset.id, btn.dataset.action);
  }});
}}

async function handleAction(id, action) {{
  if (action === 'toggle') {{
    document.querySelector(`.card[data-id="${{id}}"]`)?.classList.toggle('open');
    return;
  }}
  try {{
    state = await api('/api/review', {{ method: 'POST', body: JSON.stringify({{ id, action }}) }});
    renderAll();
  }} catch (err) {{
    setLog(String(err.message || err), true);
  }}
}}

async function mergeApproved() {{
  const selected = state.counts?.selected || 0;
  if (!selected) {{
    setLog('当前没有待合并条目。默认流程会由 profile-auto-review 自动处理；这里主要用于覆盖修正。');
    return;
  }}
  document.getElementById('mergeBtn').disabled = true;
  setLog('正在合并 reviewed/profile 层...');
  try {{
    const result = await api('/api/merge', {{ method: 'POST', body: JSON.stringify({{}}) }});
    state = result.state || await api('/api/state');
    setLog(`${{result.ok ? '完成' : '失败'}}\\n\\n${{result.stdout || ''}}\\n${{result.stderr || ''}}`.trim(), !result.ok);
    renderAll();
  }} catch (err) {{
    setLog(String(err.message || err), true);
  }} finally {{
    document.getElementById('mergeBtn').disabled = false;
  }}
}}

document.getElementById('refreshBtn').onclick = load;
document.getElementById('mergeBtn').onclick = mergeApproved;
['search', 'sort', 'sensitivity', 'density'].forEach(id => document.getElementById(id).oninput = renderCards);
load().catch(err => setLog(String(err.message || err), true));
</script>
</body>
</html>"""


def factory_page_html(title: str, *, embedded: bool = False) -> str:
    safe_title = html.escape(title)
    body_class = ' class="embedded"' if embedded else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #050809;
  --panel: rgba(10, 16, 17, .88);
  --panel2: rgba(15, 24, 25, .92);
  --line: rgba(132, 239, 213, .18);
  --line2: rgba(132, 239, 213, .42);
  --text: #edf7f4;
  --muted: #91aaa4;
  --cyan: #7ff0d4;
  --lime: #c8f46d;
  --amber: #f2c45d;
  --red: #ff8178;
  --blue: #94bbff;
  --ink: #03100e;
  --radius: 8px;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; min-height: 100%; background: var(--bg); color: var(--text); }}
body {{
  font-family: "Avenir Next", "PingFang SC", "Microsoft YaHei", ui-sans-serif, system-ui, sans-serif;
  letter-spacing: 0;
  line-height: 1.5;
  background:
    linear-gradient(rgba(127,240,212,.042) 1px, transparent 1px),
    linear-gradient(90deg, rgba(127,240,212,.032) 1px, transparent 1px),
    linear-gradient(135deg, #030506 0%, #081113 50%, #0f110c 100%);
  background-size: 32px 32px, 32px 32px, auto;
}}
body:before {{
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  background: repeating-linear-gradient(180deg, rgba(255,255,255,.025) 0 1px, transparent 1px 5px);
  opacity: .22;
}}
button, input, select, textarea {{ font: inherit; letter-spacing: 0; }}
a {{ color: inherit; }}
.app {{ position: relative; z-index: 1; min-height: 100vh; padding: 24px clamp(14px, 3vw, 42px) 46px; }}
.hero {{
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(310px, .92fr);
  gap: 16px;
  margin-bottom: 16px;
}}
.hero-main, .panel, .step-card, .role-card, .job-card {{
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: linear-gradient(180deg, rgba(13, 22, 24, .90), rgba(5, 10, 11, .92));
  box-shadow: 0 20px 52px rgba(0,0,0,.26);
}}
.hero-main {{
  min-height: 238px;
  padding: clamp(20px, 3vw, 34px);
  border-color: var(--line2);
  background:
    linear-gradient(115deg, rgba(127,240,212,.18), transparent 48%),
    linear-gradient(180deg, rgba(15, 26, 27, .92), rgba(5, 9, 10, .94));
  position: relative;
  overflow: hidden;
}}
.hero-main:after {{
  content: "TASK CONTEXT";
  position: absolute;
  right: 18px;
  bottom: -8px;
  color: rgba(127,240,212,.075);
  font-size: clamp(52px, 10vw, 124px);
  font-weight: 920;
  line-height: .8;
  white-space: nowrap;
}}
.kicker {{ color: var(--cyan); font-size: 11px; font-weight: 850; letter-spacing: .18em; text-transform: uppercase; }}
h1 {{ position: relative; z-index: 1; margin: 10px 0 12px; font-size: clamp(34px, 6vw, 76px); line-height: .88; font-weight: 880; }}
.hero-main p {{ position: relative; z-index: 1; max-width: 760px; color: #bcd1cc; margin: 0; font-size: 14px; }}
.hero-side {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
.stat {{
  min-height: 112px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: rgba(255,255,255,.028);
  padding: 15px;
}}
.stat b {{ display: block; font-size: clamp(22px, 3vw, 36px); line-height: 1; color: var(--cyan); }}
.stat span {{ display: block; margin-top: 8px; color: var(--muted); font-size: 12px; }}
.layout {{ display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(330px, .95fr); gap: 16px; }}
.section-title {{ display: flex; justify-content: space-between; align-items: end; gap: 12px; margin: 18px 0 10px; }}
.section-title h2 {{ margin: 0; font-size: 19px; }}
.section-title p {{ margin: 0; color: var(--muted); font-size: 12px; }}
.flow {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
.step-card {{ padding: 16px; min-height: 190px; display: flex; flex-direction: column; gap: 12px; }}
.step-top {{ display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }}
.step-num {{ color: var(--ink); background: var(--cyan); border-radius: 999px; min-width: 28px; height: 28px; display: inline-grid; place-items: center; font-weight: 850; }}
.step-card h3 {{ margin: 0; font-size: 17px; }}
.step-card p {{ margin: 0; color: var(--muted); font-size: 12px; }}
.cmd {{ color: #bdd6d0; background: rgba(0,0,0,.24); border: 1px solid rgba(255,255,255,.06); border-radius: var(--radius); padding: 8px 9px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }}
.btn {{
  min-height: 40px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  color: var(--text);
  background: rgba(6, 13, 14, .88);
  padding: 8px 12px;
  cursor: pointer;
}}
.btn:hover {{ border-color: var(--line2); background: rgba(127,240,212,.07); }}
.btn.primary {{ color: var(--ink); background: var(--lime); border-color: var(--lime); font-weight: 850; }}
.btn.warn {{ color: #281900; background: var(--amber); border-color: var(--amber); font-weight: 850; }}
.btn:disabled {{ opacity: .45; cursor: not-allowed; }}
.step-actions {{ margin-top: auto; display: flex; gap: 8px; flex-wrap: wrap; }}
.panel {{ padding: 16px; }}
.role-form {{ display: grid; grid-template-columns: minmax(0, 1fr) 150px; gap: 10px; margin-bottom: 12px; }}
.input, .select, textarea {{
  width: 100%;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: rgba(2, 7, 8, .74);
  color: var(--text);
  outline: none;
  padding: 10px 12px;
}}
.input:focus, .select:focus, textarea:focus {{ border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(127,240,212,.08); }}
.jobs, .roles {{ display: grid; gap: 10px; }}
.job-card, .role-card {{ padding: 13px; }}
.job-head, .role-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; }}
.pill {{ display: inline-flex; align-items: center; min-height: 24px; border: 1px solid rgba(255,255,255,.08); border-radius: 999px; padding: 3px 8px; color: var(--muted); font-size: 11px; background: rgba(255,255,255,.028); }}
.pill.success {{ color: var(--ink); background: var(--lime); border-color: var(--lime); font-weight: 850; }}
.pill.running {{ color: var(--ink); background: var(--cyan); border-color: var(--cyan); font-weight: 850; }}
.pill.failed {{ color: #fff0ee; background: rgba(255,129,120,.18); border-color: rgba(255,129,120,.42); }}
.pill.attention {{ color: #2b1d00; background: var(--amber); border-color: var(--amber); font-weight: 850; }}
.small {{ color: var(--muted); font-size: 12px; }}
.log {{
  margin-top: 10px;
  display: none;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: #020606;
  color: #bdd6d0;
  padding: 11px;
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 11px;
}}
.role-card h3 {{ margin: 0 0 7px; font-size: 15px; }}
.role-meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.role-files {{ margin-top: 10px; color: var(--muted); font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }}
.top-links {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 16px; position: relative; z-index: 1; }}
.top-links a {{ text-decoration: none; }}
body.embedded .app {{ min-height: auto; padding: 14px; }}
body.embedded .top-links {{ display: none; }}
body.embedded .hero {{ grid-template-columns: minmax(0, 1fr) minmax(280px, .65fr); }}
body.embedded .hero-main {{ min-height: 180px; }}
body.embedded h1 {{ font-size: clamp(30px, 5vw, 58px); }}
@media (max-width: 1080px) {{
  .hero, .layout {{ grid-template-columns: 1fr; }}
  .flow {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 680px) {{
  .app {{ padding: 14px 10px 32px; }}
  .hero-side {{ grid-template-columns: 1fr 1fr; }}
  .role-form {{ grid-template-columns: 1fr; }}
  .job-head, .role-head {{ flex-direction: column; }}
}}
</style>
</head>
<body{body_class}>
<main class="app">
  <section class="hero">
    <div class="hero-main">
      <div class="kicker">IMMORTAL TASK CONTEXT</div>
      <h1>任务上下文<br>生成器</h1>
      <p>先抓取，后清洗，再按当前目标生成一次性上下文包。所有按钮只调用本机白名单命令；原始语料仍在本地 vault，长期 skill 只在高频稳定场景手动晋升。</p>
      <div class="top-links">
        <a class="btn" href="/">返回看板</a>
        <a class="btn" href="/review">审计台</a>
        <a class="btn" href="/timeline?embed=1">时间线</a>
      </div>
    </div>
    <div class="hero-side">
      <div class="stat"><b id="statRecords">-</b><span>总记忆记录</span></div>
      <div class="stat"><b id="statQuality">-</b><span>记忆质量分</span></div>
      <div class="stat"><b id="statRoles">-</b><span>临时上下文</span></div>
      <div class="stat"><b id="statPeople">-</b><span>人物档案</span></div>
    </div>
  </section>

  <section class="layout">
    <div>
      <div class="section-title"><div><h2>按钮工作流</h2><p>长任务会后台执行，可以留在页面看状态。</p></div><button class="btn" id="refreshBtn">刷新状态</button></div>
      <div class="flow">
        <article class="step-card">
          <div class="step-top"><div><span class="step-num">1</span><h3>采集新语料</h3></div><span class="pill" id="collectFresh">-</span></div>
          <p>拉取本地和飞书新增内容，进入可恢复仓库。适合每天或试用前先跑。</p>
          <div class="cmd" id="cmdCollect"></div>
          <div class="step-actions"><button class="btn primary" data-job="collect">开始采集</button></div>
        </article>
        <article class="step-card">
          <div class="step-top"><div><span class="step-num">2</span><h3>清洗入库</h3></div><span class="pill" id="cleanFresh">-</span></div>
          <p>把候选记忆清洗、蒸馏、自动审阅并刷新长期画像，不再要求用户手动勾选。</p>
          <div class="cmd" id="cmdClean"></div>
          <div class="step-actions"><button class="btn primary" data-job="clean">清洗并刷新画像</button></div>
        </article>
        <article class="step-card">
          <div class="step-top"><div><span class="step-num">3</span><h3>一键生成任务上下文</h3></div><span class="pill attention">完整链路</span></div>
          <p>先采集，再生成指定任务的短期上下文包。适合交给 Codex、Claude Code 或其他本地 Agent 继续使用。</p>
          <div class="cmd">collect → clean/distill/profile → task-compile</div>
          <div class="step-actions"><button class="btn warn" data-job="full">一键执行</button></div>
        </article>
        <article class="step-card">
          <div class="step-top"><div><span class="step-num">4</span><h3>健康检查</h3></div><span class="pill" id="healthStatus">-</span></div>
          <p>检查采集、清洗、画像、备份、看板和自动任务是否新鲜。</p>
          <div class="cmd" id="cmdHealth"></div>
          <div class="step-actions"><button class="btn" data-job="health">运行 health</button></div>
        </article>
      </div>

      <div class="section-title"><div><h2>生成任务上下文</h2><p>按目标场景编译短期上下文，不默认安装成 Codex skill。</p></div></div>
      <section class="panel">
        <div class="role-form">
          <input class="input" id="goal" value="当前任务" placeholder="例如：写稿审稿流程 / 客户方案商业判断 / 项目推进 Agent">
          <select class="select" id="mode">
            <option value="auto">自动识别</option>
            <option value="writer">写稿审稿</option>
            <option value="business">商业判断</option>
            <option value="project">项目推进</option>
            <option value="reviewer">复核审阅</option>
            <option value="shadow">影子分身</option>
            <option value="advisor">决策顾问</option>
            <option value="custom">自定义</option>
          </select>
        </div>
        <button class="btn primary" id="buildRoleBtn">生成任务上下文</button>
      </section>
    </div>

    <aside>
      <div class="section-title"><div><h2>任务状态</h2><p>后台 job 输出。</p></div></div>
      <div class="jobs" id="jobs"></div>
      <div class="section-title"><div><h2>最近任务上下文</h2><p>直接读取 ~/.immortal/sessions。</p></div></div>
      <div class="roles" id="roles"></div>
    </aside>
  </section>
</main>

<script>
let snapshot = {{}};
let pollTimer = null;
const statusClass = s => s === 'success' ? 'success' : s === 'running' || s === 'queued' ? 'running' : s === 'failed' ? 'failed' : s === 'attention' ? 'attention' : '';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
const fmt = n => Number(n || 0).toLocaleString('zh-CN');
function shortTime(v) {{
  if (!v) return '-';
  return String(v).replace('T', ' ').slice(0, 19);
}}
async function api(path, options={{}}) {{
  const res = await fetch(path, {{...options, headers: {{'Content-Type': 'application/json', ...(options.headers || {{}})}}}});
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}}
async function refresh() {{
  snapshot = await api('/api/factory/state');
  render();
  const running = (snapshot.jobs || []).some(j => ['queued', 'running'].includes(j.status));
  if (running && !pollTimer) pollTimer = setInterval(refresh, 2200);
  if (!running && pollTimer) {{ clearInterval(pollTimer); pollTimer = null; }}
}}
function render() {{
  const state = snapshot.state || {{}};
  const layers = snapshot.layers || {{}};
  document.getElementById('statRecords').textContent = fmt(state.total_records);
  document.getElementById('statQuality').textContent = snapshot.quality?.score ?? '-';
  document.getElementById('statRoles').textContent = fmt((snapshot.roles || []).length);
  document.getElementById('statPeople').textContent = fmt(layers.people?.count);
  document.getElementById('collectFresh').textContent = shortTime(state.last_collect);
  document.getElementById('cleanFresh').textContent = shortTime(state.last_profile_nuwa || state.last_profile);
  document.getElementById('healthStatus').textContent = snapshot.quality?.status || '-';
  document.getElementById('cmdCollect').textContent = snapshot.commands?.collect || '';
  document.getElementById('cmdClean').textContent = snapshot.commands?.clean || '';
  document.getElementById('cmdHealth').textContent = snapshot.commands?.health || '';
  renderJobs();
  renderRoles();
}}
function renderJobs() {{
  const el = document.getElementById('jobs');
  const jobs = snapshot.jobs || [];
  if (!jobs.length) {{
    el.innerHTML = '<div class="job-card small">还没有本页触发的任务。</div>';
    return;
  }}
  el.innerHTML = jobs.map(job => {{
    const output = [job.stdout, job.stderr, job.error].filter(Boolean).join('\\n').slice(-12000);
    return `<article class="job-card">
      <div class="job-head">
        <div><b>${{esc(job.kind)}}</b><div class="small">${{esc(job.summary || job.id)}} · ${{shortTime(job.created_at)}} · ${{job.elapsed_seconds ?? '-'}}s</div></div>
        <span class="pill ${{statusClass(job.status)}}">${{esc(job.status)}}</span>
      </div>
      <div class="role-meta">${{(job.commands || []).map(c => `<span class="pill">${{esc(c)}}</span>`).join('')}}</div>
      <pre class="log" style="${{output ? 'display:block' : ''}}">${{esc(output)}}</pre>
    </article>`;
  }}).join('');
}}
function renderRoles() {{
  const el = document.getElementById('roles');
  const roles = snapshot.roles || [];
  if (!roles.length) {{
    el.innerHTML = '<div class="role-card small">暂无任务上下文。</div>';
    return;
  }}
  el.innerHTML = roles.map(role => `<article class="role-card">
    <div class="role-head">
      <div><h3>${{esc(role.goal)}}</h3><div class="small">${{esc(role.mode_label || role.mode)}} · ${{shortTime(role.generated_at)}}</div></div>
      <span class="pill ${{role.quality === 'ok' ? 'success' : 'attention'}}">${{esc(role.quality)}}</span>
    </div>
    <div class="role-meta">
      <span class="pill">${{role.expires_at ? '临时' : '未设过期'}}</span>
      <span class="pill">未安装 skill</span>
      <span class="pill">${{esc(role.skill_name || role.slug)}}</span>
    </div>
    <div class="role-files">${{esc(role.files?.['TASK_CONTEXT.md'] || role.path)}}</div>
  </article>`).join('');
}}
async function startJob(kind, payload={{}}) {{
  document.querySelectorAll('button').forEach(btn => btn.disabled = true);
  try {{
    await api('/api/factory/jobs', {{method: 'POST', body: JSON.stringify({{kind, ...payload}})}});
    await refresh();
  }} catch (err) {{
    alert(err.message || String(err));
  }} finally {{
    document.querySelectorAll('button').forEach(btn => btn.disabled = false);
  }}
}}
document.getElementById('refreshBtn').onclick = refresh;
document.querySelectorAll('[data-job]').forEach(btn => {{
  btn.onclick = () => {{
    const kind = btn.dataset.job;
    if (kind === 'full') {{
      startJob('full', {{goal: document.getElementById('goal').value, mode: document.getElementById('mode').value}});
    }} else {{
      startJob(kind);
    }}
  }};
}});
document.getElementById('buildRoleBtn').onclick = () => startJob('role', {{goal: document.getElementById('goal').value, mode: document.getElementById('mode').value}});
refresh().catch(err => alert(err.message || String(err)));
</script>
</body>
</html>"""


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "ImmortalProfileReview/0.1"

    @property
    def store(self) -> ReviewStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def factory(self) -> FactoryStore:
        return self.server.factory  # type: ignore[attr-defined]

    @property
    def control_center(self) -> ControlCenter:
        return self.server.control_center  # type: ignore[attr-defined]

    @property
    def control_data(self) -> ControlData:
        value = getattr(self.server, "control_data", None)
        if value is None:
            value = ControlData(
                self.control_center.immortal_dir,
                skill_dir=self.control_center.skill_dir,
                listen_address=str(self.server.server_address[0]),
                listen_port=int(self.server.server_address[1]),
            )
            self.server.control_data = value  # type: ignore[attr-defined]
        return value

    @property
    def product_data(self) -> ProductData:
        value = getattr(self.server, "product_data", None)
        if value is None:
            value = ProductData(
                self.control_center.immortal_dir,
                control_data=self.control_data,
                control_center=self.control_center,
            )
            self.server.product_data = value  # type: ignore[attr-defined]
        return value

    @property
    def product_mutations(self) -> ProductMutationCoordinator:
        value = getattr(self.server, "product_mutations", None)
        if value is None:
            value = ProductMutationCoordinator(self.control_center.immortal_dir)
            self.server.product_mutations = value  # type: ignore[attr-defined]
        return value

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def end_headers(self) -> None:
        headers = LEGACY_SECURITY_HEADERS if getattr(self, "_legacy_security", False) else SECURITY_HEADERS
        for name, value in headers.items():
            self.send_header(name, value)
        super().end_headers()

    def host_is_allowed(self) -> bool:
        port = int(self.server.server_address[1])
        if is_allowed_host(self.headers.get("Host") or "", port):
            return True
        self.send_json(
            error_payload(
                "host_not_allowed",
                "请求 Host 不在本机控制中心允许范围内。",
                detail="只允许当前端口上的 loopback Host。",
            ),
            status=HTTPStatus.FORBIDDEN,
        )
        return False

    def send_json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_html(self, value: str, status: int = 200) -> None:
        payload = value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_legacy_html(self, value: str, status: int = 200) -> None:
        self._legacy_security = True
        try:
            self.send_html(value, status=status)
        finally:
            self._legacy_security = False

    def send_product_asset(self, request_path: str) -> None:
        raw_name = request_path.removeprefix("/assets/")
        try:
            name = unquote(raw_name, errors="strict")
        except (UnicodeDecodeError, UnicodeEncodeError):
            self.send_json(error_payload("invalid_asset_path", "资源路径无效。"), status=400)
            return
        if (
            not raw_name
            or name not in PRODUCT_ASSETS
            or name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
        ):
            self.send_json(error_payload("asset_not_found", "资源不存在。"), status=404)
            return
        path = PRODUCT_ASSET_ROOT / name
        try:
            payload = path.read_bytes()
        except OSError:
            self.send_json(error_payload("asset_not_found", "资源不存在。"), status=404)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", PRODUCT_ASSETS[name])
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid json body") from exc
        if not isinstance(value, dict):
            raise ValueError("json body must be an object")
        return value

    def do_GET(self) -> None:
        if not self.host_is_allowed():
            return
        if is_v2_get_target(self.path):
            status, payload = route_product_get(
                self.path, lambda: self.product_data
            )
            self.send_json(payload, status=status)
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/assets/"):
            self.send_product_asset(parsed.path)
            return
        if parsed.path == "/healthz":
            self.send_json({"status": "ok"})
            return
        if parsed.path == "/readyz":
            status, payload = self.control_data.readiness()
            self.send_json(payload, status=status)
            return
        if parsed.path == "/api/v1/capabilities":
            self.send_json(self.control_data.capabilities())
            return
        if parsed.path == "/api/v1/overview":
            self.send_json(self.control_center.build_snapshot(jobs=self.factory.list_jobs()))
            return
        if parsed.path == "/api/v1/sources":
            self.send_json(self.control_data.sources())
            return
        if parsed.path == "/api/v1/memories":
            try:
                self.send_json(self.control_data.memories(parse_qs(parsed.query)))
            except ValueError as exc:
                self.send_json(error_payload("invalid_request", str(exc)), status=400)
            return
        if parsed.path.startswith("/api/v1/memories/"):
            memory_id = parsed.path.removeprefix("/api/v1/memories/")
            try:
                self.send_json(self.control_data.memory_detail(memory_id))
            except ValueError as exc:
                self.send_json(error_payload("invalid_memory_id", str(exc)), status=400)
            except KeyError:
                self.send_json(error_payload("memory_not_found", "记忆不存在。"), status=404)
            return
        if parsed.path in {"/api/v1/profile", "/api/v1/profile/candidates"}:
            query = parse_qs(parsed.query)
            try:
                self.send_json(
                    self.store.list_candidates(
                        limit=int(query.get("limit", ["20"])[0]),
                        offset=int(query.get("offset", ["0"])[0]),
                        review_state=str(query.get("state", [""])[0]),
                        focus=str(query.get("focus", [""])[0]),
                        memory_type=str(query.get("type", [""])[0]),
                        sensitivity=str(query.get("sensitivity", [""])[0]),
                        query=str(query.get("q", [""])[0]),
                        sort=str(query.get("sort", ["priority"])[0]),
                    )
                )
            except ValueError as exc:
                self.send_json(error_payload("invalid_request", str(exc)), status=400)
            return
        if parsed.path.startswith("/api/v1/profile/candidates/"):
            candidate_id = parsed.path.removeprefix("/api/v1/profile/candidates/")
            try:
                self.send_json(self.store.candidate_detail(candidate_id))
            except ValueError as exc:
                self.send_json(error_payload("invalid_candidate_id", str(exc)), status=400)
            except KeyError:
                self.send_json(error_payload("candidate_not_found", "画像候选不存在。"), status=404)
            return
        if parsed.path == "/api/v1/agent":
            self.send_json(self.control_data.agent_status())
            return
        if parsed.path.startswith("/api/v1/agent/contexts/"):
            context_id = parsed.path.removeprefix("/api/v1/agent/contexts/")
            try:
                self.send_json(self.control_data.agent_context_detail(context_id))
            except ValueError as exc:
                self.send_json(error_payload("invalid_context_id", str(exc)), status=400)
            except KeyError:
                self.send_json(error_payload("context_not_found", "Agent 上下文不存在。"), status=404)
            return
        if parsed.path == "/api/v1/backups":
            self.send_json(self.control_data.backups())
            return
        if parsed.path == "/api/v1/diagnostics":
            self.send_json(self.control_data.diagnostics())
            return
        if parsed.path == "/api/v1/jobs":
            self.send_json({"items": self.factory.list_jobs()})
            return
        if parsed.path.startswith("/api/v1/jobs/"):
            suffix = parsed.path.removeprefix("/api/v1/jobs/")
            job_id, _, child = suffix.partition("/")
            try:
                if child == "logs":
                    query = parse_qs(parsed.query)
                    self.send_json(
                        self.factory.job_logs(
                            job_id,
                            offset=int(query.get("offset", ["0"])[0]),
                            limit=int(query.get("limit", ["8000"])[0]),
                        )
                    )
                    return
                if child:
                    self.send_json(error_payload("not_found", "请求的资源不存在。"), status=404)
                    return
                job = self.factory.get_job(job_id)
                if not job:
                    self.send_json(error_payload("job_not_found", "任务不存在。"), status=404)
                else:
                    self.send_json(job)
            except (KeyError, ValueError) as exc:
                self.send_json(error_payload("job_not_found", str(exc)), status=404)
            return
        if parsed.path == "/":
            self.send_html(product_page_html("Immortal Memory"))
            return
        if parsed.path == "/control-center":
            self.send_legacy_html(control_center_page_html("Immortal Control Center"))
            return
        if parsed.path == "/snapshot":
            if DEFAULT_DASHBOARD.exists():
                payload = DEFAULT_DASHBOARD.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_html(
                error_page(
                    "Legacy Snapshot 已停用",
                    "旧静态看板已永久退出正式产品，请使用实时控制中心。",
                ),
                status=HTTPStatus.GONE,
            )
            return
        if parsed.path == "/review":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/?view=self&filter=candidate")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path == "/agent-factory":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/?view=use")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path == "/agent-entry":
            if not DEFAULT_AGENT_ENTRY.exists():
                self.send_html(
                    error_page(
                        "Agent Entry 尚未生成",
                        "请通过 Agent 模块或受控 CLI 生成入口，读取页面不会自动执行命令。",
                    ),
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            text = DEFAULT_AGENT_ENTRY.read_text(encoding="utf-8", errors="ignore")
            payload = (
                "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<title>Immortal Agent Entry</title>"
                "<style>body{margin:0;background:#06090a;color:#eaf7f3;font:15px/1.6 ui-sans-serif,system-ui;padding:28px;max-width:980px}"
                "pre{white-space:pre-wrap;background:#0d1517;border:1px solid #21423c;border-radius:8px;padding:18px;overflow:auto}</style>"
                "</head><body><h1>Immortal Agent Entry</h1><pre>"
                + html.escape(text)
                + "</pre></body></html>"
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/timeline":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/?view=memories&mode=timeline")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path == "/api/state":
            self.send_json(self.store.build_state())
            return
        if parsed.path == "/api/factory/state":
            self.send_json(self.factory.snapshot())
            return
        if parsed.path == "/api/control-center/state":
            self.send_json(self.control_center.build_snapshot(jobs=self.factory.list_jobs()))
            return
        if parsed.path.startswith("/api/factory/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            job = self.factory.get_job(job_id)
            if not job:
                self.send_json(error_payload("job_not_found", "任务不存在。"), status=404)
            else:
                self.send_json(job)
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_json(error_payload("not_found", "请求的资源不存在。"), status=404)

    def do_POST(self) -> None:
        if not self.host_is_allowed():
            return
        parsed = urlparse(self.path)
        try:
            if is_v2_post_target(self.path):
                metadata = require_write_metadata(
                    self.headers, int(self.server.server_address[1])
                )
                body = read_product_json(self.headers, self.rfile.read)
                status, payload = route_product_post(
                    self.path,
                    body,
                    metadata,
                    lambda: self.product_mutations,
                )
                self.send_json(payload, status=status)
                return
            if not is_allowed_local_origin(self.headers.get("Origin") or ""):
                self.send_json(error_payload("origin_not_allowed", "请求来源不被允许。"), status=403)
                return
            body = self.read_body()
            if parsed.path == "/api/v1/jobs":
                kind = str(body.get("kind") or "")
                self.send_json(self.factory.start_job(kind, body.get("params") or {}), status=202)
                return
            if parsed.path.startswith("/api/v1/jobs/"):
                suffix = parsed.path.removeprefix("/api/v1/jobs/")
                job_id, _, action = suffix.partition("/")
                if action == "cancel":
                    self.send_json(self.factory.request_cancel(job_id), status=202)
                    return
                if action == "retry":
                    self.send_json(self.factory.retry_job(job_id), status=202)
                    return
                self.send_json(error_payload("not_found", "请求的资源不存在。"), status=404)
                return
            if parsed.path == "/api/v1/profile/merge":
                result = self.store.merge()
                self.send_json(result, status=200 if result.get("ok") else 500)
                return
            if (
                parsed.path.startswith("/api/v1/profile/candidates/")
                and parsed.path.endswith("/actions")
            ):
                candidate_id = parsed.path.removeprefix("/api/v1/profile/candidates/").removesuffix("/actions")
                action = str(body.get("action") or "")
                self.send_json(self.store.update_review(candidate_id, action))
                return
            if parsed.path == "/api/v1/agent/contexts":
                unknown = set(body) - {"goal", "mode"}
                if unknown:
                    raise ValueError(f"unsupported field: {sorted(unknown)[0]}")
                goal = str(body.get("goal") or "").strip()
                if not goal:
                    raise ValueError("goal is required")
                mode = str(body.get("mode") or "auto")
                self.send_json(
                    self.factory.start_job(
                        "session",
                        {"goal": goal[:160], "mode": mode},
                    ),
                    status=202,
                )
                return
            if parsed.path == "/api/v1/backups/verify":
                if body:
                    raise ValueError("backup verify does not accept parameters")
                self.send_json(
                    self.factory.start_job("backup_verify", {}),
                    status=202,
                )
                return
            if parsed.path == "/api/review":
                memory_id = str(body.get("id") or "")
                action = str(body.get("action") or "")
                self.send_json(self.store.update_review(memory_id, action))
                return
            if parsed.path == "/api/merge":
                self.send_json(self.store.merge())
                return
            if parsed.path == "/api/factory/jobs":
                kind = str(body.get("kind") or "")
                self.send_json(self.factory.start_job(kind, body), status=202)
                return
            if parsed.path == "/api/control-center/actions":
                action = str(body.get("action") or "")
                if action not in {"run", "health", "backup_verify", "profile_refresh"}:
                    raise ValueError("unknown control action")
                active = self.factory.list_jobs()
                if action == "run" and any(
                    item.get("kind") == "run" and item.get("status") in {"queued", "running"}
                    for item in active
                ):
                    raise ValueError("Immortal is already running")
                self.send_json(self.factory.start_job(action, {}), status=202)
                return
            self.send_json(error_payload("not_found", "请求的资源不存在。"), status=404)
        except ProductHttpError as exc:
            self.send_json(
                error_body(exc.code, exc.message, retryable=exc.retryable),
                status=exc.status,
            )
        except JobConflict as exc:
            self.send_json(error_payload("job_conflict", str(exc), retryable=True), status=409)
        except Exception as exc:
            self.send_json(error_payload("invalid_request", str(exc)), status=400)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local Immortal profile review desk")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the review desk in the default browser")
    parser.add_argument("--proposal", type=Path, default=DEFAULT_PROPOSAL)
    parser.add_argument("--memories", type=Path, default=DEFAULT_PROFILE_MEMORIES)
    parser.add_argument("--reviewed", type=Path, default=DEFAULT_REVIEWED_FILE)
    parser.add_argument("--review-state", type=Path, default=DEFAULT_REVIEW_STATE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ReviewStore(args.proposal, args.memories, args.reviewed, args.review_state)
    factory = FactoryStore()
    control_center = ControlCenter(IMMORTAL_DIR, skill_dir=SKILL_DIR, service_reachable=True)
    server = ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    server.store = store  # type: ignore[attr-defined]
    server.factory = factory  # type: ignore[attr-defined]
    server.control_center = control_center  # type: ignore[attr-defined]
    server.control_data = ControlData(  # type: ignore[attr-defined]
        IMMORTAL_DIR,
        skill_dir=SKILL_DIR,
        listen_address=args.host,
        listen_port=args.port,
    )
    url = f"http://{args.host}:{args.port}/"
    print(f"Immortal dashboard: {url}")
    print(f"Task context compiler: {url}agent-factory")
    print(f"Profile review audit desk: {url}review")
    print(f"Proposal: {args.proposal}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
