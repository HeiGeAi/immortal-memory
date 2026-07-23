#!/usr/bin/env python3
"""Persistent, local-only task state for Immortal automation."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from redact_common import redact
from state_store import mutate_state_atomic


TASK_SPECS = {
    "daily_pipeline": {"params": set(), "requires_approval": False},
    "profile_refresh": {"params": set(), "requires_approval": False},
    "task_context": {"params": {"goal", "mode"}, "requires_approval": False},
}

_TERMINAL_STATUSES = {"success", "attention", "failed", "canceled"}
_TASK_STATUSES = {"queued", "running", *_TERMINAL_STATUSES}
_CONTEXT_MODES = {
    "auto",
    "advisor",
    "writer",
    "reviewer",
    "business",
    "project",
    "shadow",
    "custom",
}
_TRIGGERS = {"agent", "manual", "schedule", "system"}
_TASK_FIELDS = {
    "id", "kind", "trigger", "params", "dedupe_key", "status", "created_at",
    "started_at", "finished_at", "summary", "error",
}
_STATE_FIELDS = {
    "schema_version", "paused", "pause_reason", "paused_at", "tasks", "pending_history", "outbox_claim", "updated_at",
}
_HISTORY_FIELDS = {
    "event",
    "event_id",
    "task_id",
    "kind",
    "trigger",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "summary",
    "error",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AutomationTasks:
    """A small state machine that only represents declared safe task kinds."""

    def __init__(
        self,
        immortal_dir: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        skill_dir: Path | None = None,
    ) -> None:
        self.immortal_dir = Path(immortal_dir)
        self.runtime_dir = self.immortal_dir / "runtime"
        self.state_path = self.runtime_dir / "automation_tasks.json"
        self.history_path = self.runtime_dir / "automation_runs.jsonl"
        self.context_events_path = self.runtime_dir / "automation_context_events.jsonl"
        self.clock = clock
        self.skill_dir = Path(skill_dir) if skill_dir is not None else Path(__file__).resolve().parent

    def _now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _state(self, current: dict[str, Any], now: str) -> dict[str, Any]:
        if not current:
            current = {
                "schema_version": 1,
                "paused": False,
                "pause_reason": "",
                "paused_at": None,
                "tasks": [],
                "pending_history": [],
                "outbox_claim": None,
                "updated_at": now,
            }
        self._validate_state(current)
        current["updated_at"] = now
        return current

    @staticmethod
    def _short(value: str) -> str:
        return str(redact(str(value or "")) or "").strip()[:500]

    @staticmethod
    def _copy_task(task: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(task)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._state({}, self._now())
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("automation state must be an object")
        return self._state(value, str(value.get("updated_at") or self._now()))

    def _history_entry(self, event: str, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "event": event,
            "event_id": uuid.uuid4().hex,
            "task_id": task["id"],
            "kind": task["kind"],
            "trigger": task["trigger"],
            "status": task["status"],
            "created_at": task["created_at"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "summary": self._short(task["summary"]),
            "error": self._short(task["error"]),
        }

    def _append_history(self, entry: dict[str, Any]) -> None:
        entry = {key: value for key, value in entry.items() if key in _HISTORY_FIELDS and value not in (None, "")}
        payload = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.history_path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("failed to append automation history")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)

    def _append_context_event(self, entry: dict[str, Any]) -> None:
        """Append body-free Agent Bridge evidence without touching task state."""
        payload = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.context_events_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.context_events_path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("failed to append automation context event")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)

    def _history_has_event(self, event_id: str) -> bool:
        """Return whether a prior successful append already contains this event."""
        try:
            with self.history_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("event_id") == event_id:
                        return True
        except FileNotFoundError:
            return False
        return False

    @staticmethod
    def _claim_is_stale(value: str, now: datetime) -> bool:
        try:
            claimed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return True
        if claimed_at.tzinfo is None:
            return True
        return (now.astimezone(timezone.utc) - claimed_at.astimezone(timezone.utc)).total_seconds() >= 300

    def _flush_pending_history(self) -> None:
        """Best-effort outbox flush. Events remain durable until an append succeeds."""
        while True:
            pending: dict[str, Any] | None = None

            def claim(state: dict[str, Any]) -> dict[str, Any]:
                nonlocal pending
                state = self._state(state, self._now())
                current_time = self.clock()
                if state["outbox_claim"] is not None and self._claim_is_stale(
                    state["outbox_claim"]["claimed_at"], current_time
                ):
                    state["outbox_claim"] = None
                if state["outbox_claim"] is None and state["pending_history"]:
                    pending = self._copy_task(state["pending_history"][0])
                    state["outbox_claim"] = {
                        "event_id": pending["event_id"],
                        "claimed_at": self._now(),
                    }
                return state

            mutate_state_atomic(self.state_path, claim)
            if pending is None:
                return
            if self._history_has_event(pending["event_id"]):
                def acknowledge_existing(state: dict[str, Any]) -> dict[str, Any]:
                    state = self._state(state, self._now())
                    if state["outbox_claim"] and state["outbox_claim"]["event_id"] == pending["event_id"]:
                        state["pending_history"] = [
                            item for item in state["pending_history"]
                            if item.get("event_id") != pending["event_id"]
                        ]
                        state["outbox_claim"] = None
                    return state

                mutate_state_atomic(self.state_path, acknowledge_existing)
                continue
            try:
                self._append_history(pending)
            except OSError:
                def requeue(state: dict[str, Any]) -> dict[str, Any]:
                    state = self._state(state, self._now())
                    if state["outbox_claim"] and state["outbox_claim"]["event_id"] == pending["event_id"]:
                        state["outbox_claim"] = None
                    return state

                mutate_state_atomic(self.state_path, requeue)
                return

            def acknowledge(state: dict[str, Any]) -> dict[str, Any]:
                state = self._state(state, self._now())
                if state["outbox_claim"] and state["outbox_claim"]["event_id"] == pending["event_id"]:
                    for index, item in enumerate(state["pending_history"]):
                        if item.get("event_id") == pending["event_id"]:
                            state["pending_history"].pop(index)
                            break
                    state["outbox_claim"] = None
                return state

            mutate_state_atomic(self.state_path, acknowledge)

    @staticmethod
    def _validate_text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be 1 to {maximum} characters")
        text = str(redact(value) or "").strip()
        if not text or len(text) > maximum:
            raise ValueError(f"{label} must be 1 to {maximum} characters")
        return text

    @staticmethod
    def _validate_dedupe_key(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("dedupe_key must be 160 characters or fewer")
        text = value.strip()
        if len(text) > 160 or (text and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]*", text)):
            raise ValueError("dedupe_key must be 160 characters or fewer and use safe characters")
        if str(redact(text) or "") != text:
            raise ValueError("dedupe_key must not contain credentials")
        return text

    def _validate_params(self, kind: str, params: dict[str, Any] | None) -> dict[str, Any]:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        allowed = TASK_SPECS[kind]["params"]
        unknown = set(params) - allowed
        if unknown:
            raise ValueError("unsupported automation task parameters")
        if kind != "task_context":
            return {}
        goal = self._validate_text(params.get("goal"), "goal", 160)
        mode = str(params.get("mode") or "auto").strip()
        if mode not in _CONTEXT_MODES:
            raise ValueError("unsupported task context mode")
        return {"goal": goal, "mode": mode}

    def _validate_persisted_task(self, task: Any) -> None:
        try:
            if not isinstance(task, dict) or set(task) != _TASK_FIELDS:
                raise ValueError
            if not isinstance(task["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task["id"]):
                raise ValueError
            kind = task["kind"]
            if not isinstance(kind, str) or kind not in TASK_SPECS:
                raise ValueError
            if task["trigger"] not in _TRIGGERS:
                raise ValueError
            if self._validate_params(kind, task["params"]) != task["params"]:
                raise ValueError
            if self._validate_dedupe_key(task["dedupe_key"]) != task["dedupe_key"]:
                raise ValueError
            if task["status"] not in _TASK_STATUSES:
                raise ValueError
            for name in ("created_at",):
                if not isinstance(task[name], str) or not task[name]:
                    raise ValueError
            for name in ("started_at", "finished_at"):
                if task[name] is not None and (not isinstance(task[name], str) or not task[name]):
                    raise ValueError
            if task["status"] == "queued" and (task["started_at"] is not None or task["finished_at"] is not None):
                raise ValueError
            if task["status"] == "running" and (task["started_at"] is None or task["finished_at"] is not None):
                raise ValueError
            if task["status"] in _TERMINAL_STATUSES and (task["started_at"] is None or task["finished_at"] is None):
                raise ValueError
            for name in ("summary", "error"):
                if not isinstance(task[name], str) or self._short(task[name]) != task[name]:
                    raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid persisted automation task") from None

    def _validate_state(self, state: Any) -> None:
        if not isinstance(state, dict) or set(state) != _STATE_FIELDS:
            raise ValueError("invalid persisted automation state")
        if state["schema_version"] != 1 or not isinstance(state["paused"], bool):
            raise ValueError("invalid persisted automation state")
        if not isinstance(state["pause_reason"], str) or self._short(state["pause_reason"]) != state["pause_reason"]:
            raise ValueError("invalid persisted automation state")
        if state["paused_at"] is not None and not isinstance(state["paused_at"], str):
            raise ValueError("invalid persisted automation state")
        if not isinstance(state["updated_at"], str) or not isinstance(state["tasks"], list) or not isinstance(state["pending_history"], list):
            raise ValueError("invalid persisted automation state")
        for task in state["tasks"]:
            self._validate_persisted_task(task)
        tasks_by_id = {task["id"]: task for task in state["tasks"]}
        seen_events = set()
        for item in state["pending_history"]:
            if not isinstance(item, dict) or set(item) != _HISTORY_FIELDS or item.get("event") not in {"claimed", "finished"}:
                raise ValueError("invalid persisted automation state")
            if not isinstance(item.get("event_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", item["event_id"]):
                raise ValueError("invalid persisted automation state")
            if item["event_id"] in seen_events:
                raise ValueError("invalid persisted automation state")
            seen_events.add(item["event_id"])
            task = tasks_by_id.get(item.get("task_id"))
            if task is None:
                raise ValueError("invalid persisted automation state")
            if item["event"] == "claimed":
                expected = {
                    "event": "claimed",
                    "task_id": task["id"],
                    "kind": task["kind"],
                    "trigger": task["trigger"],
                    "status": "running",
                    "created_at": task["created_at"],
                    "started_at": task["started_at"],
                    "finished_at": None,
                    "summary": "",
                    "error": "",
                }
                if task["started_at"] is None:
                    raise ValueError("invalid persisted automation state")
            else:
                if task["status"] not in _TERMINAL_STATUSES:
                    raise ValueError("invalid persisted automation state")
                expected = self._history_entry("finished", task)
            for key, value in expected.items():
                if key != "event_id" and item.get(key) != value:
                    raise ValueError("invalid persisted automation state")
        claim = state["outbox_claim"]
        if claim is not None:
            if not isinstance(claim, dict) or set(claim) != {"event_id", "claimed_at"}:
                raise ValueError("invalid persisted automation state")
            if not isinstance(claim["event_id"], str) or not isinstance(claim["claimed_at"], str):
                raise ValueError("invalid persisted automation state")
            if claim["event_id"] not in seen_events:
                raise ValueError("invalid persisted automation state")

    def status(self) -> dict[str, Any]:
        self._flush_pending_history()
        state = self._read_state()
        tasks = state["tasks"]
        return {
            "paused": bool(state["paused"]),
            "pause_reason": str(state["pause_reason"] or ""),
            "paused_at": state["paused_at"],
            "queued_count": sum(task.get("status") == "queued" for task in tasks),
            "running_count": sum(task.get("status") == "running" for task in tasks),
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        self._flush_pending_history()
        return [self._copy_task(task) for task in self._read_state()["tasks"] if isinstance(task, dict)]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        for task in self.list_tasks():
            if task.get("id") == task_id:
                return task
        return None

    def pause(self, reason: str) -> dict[str, Any]:
        reason = self._validate_text(reason, "reason", 160)
        now = self._now()

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            state = self._state(state, now)
            state.update({"paused": True, "pause_reason": self._short(reason), "paused_at": now})
            return state

        state = mutate_state_atomic(self.state_path, mutate)
        return {"paused": bool(state["paused"]), "pause_reason": state["pause_reason"], "paused_at": state["paused_at"]}

    def resume(self) -> dict[str, Any]:
        now = self._now()

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            state = self._state(state, now)
            state.update({"paused": False, "pause_reason": "", "paused_at": None})
            return state

        state = mutate_state_atomic(self.state_path, mutate)
        return {"paused": bool(state["paused"]), "pause_reason": state["pause_reason"], "paused_at": state["paused_at"]}

    def enqueue(
        self,
        kind: str,
        *,
        trigger: str,
        params: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        if kind not in TASK_SPECS:
            raise ValueError("unsupported automation task")
        if trigger not in _TRIGGERS:
            raise ValueError("unsupported automation trigger")
        clean_params = self._validate_params(kind, params)
        dedupe_key = self._validate_dedupe_key(dedupe_key)
        now = self._now()
        chosen: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal chosen
            state = self._state(state, now)
            if dedupe_key:
                for task in state["tasks"]:
                    if task.get("kind") == kind and task.get("dedupe_key") == dedupe_key and task.get("status") in {"queued", "running"}:
                        chosen = self._copy_task(task)
                        return state
            chosen = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "trigger": trigger,
                "params": clean_params,
                "dedupe_key": dedupe_key,
                "status": "queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "summary": "",
                "error": "",
            }
            state["tasks"].insert(0, chosen)
            return state

        mutate_state_atomic(self.state_path, mutate)
        return self._copy_task(chosen)

    def commands_for(self, task: dict[str, Any]) -> list[list[str]]:
        """Return the complete fixed local handler list for one declared task."""
        if not isinstance(task, dict):
            raise ValueError("invalid automation task")
        kind = task.get("kind")
        params = task.get("params")
        if kind not in TASK_SPECS or self._validate_params(kind, params) != params:
            raise ValueError("invalid automation task")
        python = sys.executable
        immortal = str(self.skill_dir / "immortal.py")
        if kind == "daily_pipeline":
            return [[python, immortal, "run"], [python, immortal, "agent-entry"]]
        if kind == "profile_refresh":
            return [
                [python, immortal, "profile"],
                [python, immortal, "profile-nuwa"],
                [python, immortal, "personal-model", "build"],
                [python, immortal, "quality"],
                [python, immortal, "agent-entry"],
            ]
        return [[python, immortal, "task-compile", params["goal"], "--mode", params["mode"]]]

    def record_context_event(
        self,
        *,
        kind: str,
        query_length: int,
        exit_code: int,
        artifact: dict[str, Any],
    ) -> None:
        """Record only bounded context-generation metadata, never its body."""
        if kind != "task_context" or not isinstance(query_length, int) or query_length < 0:
            raise ValueError("invalid task context event")
        if not isinstance(exit_code, int) or not isinstance(artifact, dict):
            raise ValueError("invalid task context event")
        safe_artifact = {
            "exists": bool(artifact.get("exists")),
            "bytes": int(artifact.get("bytes") or 0),
            "modified_at": self._short(str(artifact.get("modified_at") or "")),
        }
        self._append_context_event(
            {
                "event": "completed",
                "event_id": uuid.uuid4().hex,
                "kind": kind,
                "created_at": self._now(),
                "query_length": query_length,
                "exit_code": exit_code,
                "artifact": safe_artifact,
            }
        )

    def claim_next(self) -> dict[str, Any] | None:
        self._flush_pending_history()
        now = self._now()
        claimed: dict[str, Any] | None = None

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            state = self._state(state, now)
            if state["paused"]:
                return state
            for task in reversed(state["tasks"]):
                if task.get("status") == "queued":
                    task.update({"status": "running", "started_at": now, "finished_at": None})
                    claimed = self._copy_task(task)
                    state["pending_history"].append(self._history_entry("claimed", claimed))
                    break
            return state

        mutate_state_atomic(self.state_path, mutate)
        self._flush_pending_history()
        return claimed

    def finish(self, task_id: str, *, status: str, summary: str = "", error: str = "") -> dict[str, Any]:
        self._flush_pending_history()
        if status not in _TERMINAL_STATUSES:
            raise ValueError("invalid terminal status")
        now = self._now()
        finished: dict[str, Any] | None = None

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal finished
            state = self._state(state, now)
            for task in state["tasks"]:
                if task.get("id") != task_id:
                    continue
                if task.get("status") != "running":
                    raise ValueError("task must be running before it can finish")
                task.update(
                    {
                        "status": status,
                        "finished_at": now,
                        "summary": self._short(summary),
                        "error": self._short(error),
                    }
                )
                finished = self._copy_task(task)
                state["pending_history"].append(self._history_entry("finished", finished))
                return state
            raise ValueError("automation task not found")

        mutate_state_atomic(self.state_path, mutate)
        assert finished is not None
        self._flush_pending_history()
        return finished
