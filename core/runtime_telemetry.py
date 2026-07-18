#!/usr/bin/env python3
"""Structured, local-only runtime telemetry for the Immortal orchestrator."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


SCHEMA_VERSION = 1


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_time(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class RuntimeTelemetry:
    """Persist one current run and a bounded history using atomic JSON writes."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        history_limit: int = 100,
        stale_after_seconds: int = 300,
        clock: Callable[[], datetime] = _default_clock,
        pid_exists: Callable[[int], bool] = _default_pid_exists,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.current_path = self.runtime_dir / "current_run.json"
        self.history_path = self.runtime_dir / "run_history.jsonl"
        self.history_limit = max(1, int(history_limit))
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        self.clock = clock
        self.pid_exists = pid_exists
        self._lock = threading.RLock()

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_current(self, value: dict[str, Any]) -> dict[str, Any]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.current_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.current_path)
        return value

    def start_run(
        self,
        *,
        trigger: str = "unknown",
        pid: Optional[int] = None,
        stage_total: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            value: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "run_id": uuid.uuid4().hex,
                "trigger": str(trigger or "unknown"),
                "status": "running",
                "started_at": now,
                "updated_at": now,
                "finished_at": None,
                "pid": int(pid if pid is not None else os.getpid()),
                "current_stage": "",
                "stage_index": 0,
                "stage_total": max(0, int(stage_total)),
                "stages": [],
                "results": {},
                "error": "",
            }
            return self._write_current(value)

    def start_stage(self, stage_id: str, label: str) -> dict[str, Any]:
        with self._lock:
            current = self._read_json(self.current_path)
            if not current:
                raise RuntimeError("cannot start a stage without an active run")
            now = self._now()
            stages = current.setdefault("stages", [])
            stage = next((item for item in stages if item.get("id") == stage_id), None)
            if stage is None:
                stage = {
                    "id": stage_id,
                    "label": label,
                    "status": "running",
                    "started_at": now,
                    "finished_at": None,
                    "elapsed_seconds": None,
                    "summary": "",
                    "error": "",
                }
                stages.append(stage)
            else:
                stage.update(
                    {
                        "label": label,
                        "status": "running",
                        "started_at": now,
                        "finished_at": None,
                        "elapsed_seconds": None,
                        "summary": "",
                        "error": "",
                    }
                )
            current["current_stage"] = stage_id
            current["stage_index"] = stages.index(stage) + 1
            current["stage_total"] = max(int(current.get("stage_total") or 0), len(stages))
            current["updated_at"] = now
            return self._write_current(current)

    def heartbeat(
        self,
        *,
        results: Optional[dict[str, Any]] = None,
        summary: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            current = self._read_json(self.current_path)
            if not current:
                return {}
            current["updated_at"] = self._now()
            if results:
                current.setdefault("results", {}).update(results)
            if summary and current.get("current_stage"):
                for stage in current.get("stages") or []:
                    if stage.get("id") == current["current_stage"]:
                        stage["summary"] = summary
                        break
            return self._write_current(current)

    def finish_stage(
        self,
        stage_id: str,
        *,
        status: str,
        summary: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            current = self._read_json(self.current_path)
            if not current:
                raise RuntimeError("cannot finish a stage without an active run")
            now = self._now()
            for stage in current.get("stages") or []:
                if stage.get("id") != stage_id:
                    continue
                stage["status"] = status
                stage["summary"] = summary
                stage["error"] = error
                stage["finished_at"] = now
                started = _parse_time(stage.get("started_at"))
                stage["elapsed_seconds"] = (
                    max(0.0, round((self.clock().astimezone(timezone.utc) - started).total_seconds(), 2))
                    if started
                    else None
                )
                break
            current["updated_at"] = now
            return self._write_current(current)

    def finish_run(
        self,
        *,
        status: str,
        results: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            current = self._read_json(self.current_path)
            if not current:
                raise RuntimeError("cannot finish a run that has not started")
            now = self._now()
            current["status"] = status
            current["updated_at"] = now
            current["finished_at"] = now
            current["current_stage"] = ""
            current["error"] = error
            if results:
                current.setdefault("results", {}).update(results)
            self._write_current(current)
            self._append_history(current)
            return current

    def _append_history(self, value: dict[str, Any]) -> None:
        previous = list(reversed(self.read_history(limit=self.history_limit)))
        previous.append(value)
        previous = previous[-self.history_limit :]
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.history_path.with_suffix(".jsonl.tmp")
        body = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in previous)
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(self.history_path)

    def read_current(self) -> dict[str, Any]:
        with self._lock:
            current = self._read_json(self.current_path)
            if not current or current.get("status") != "running":
                return current
            pid = int(current.get("pid") or 0)
            now = self.clock().astimezone(timezone.utc)
            updated = _parse_time(current.get("updated_at"))
            if not self.pid_exists(pid):
                current["status"] = "failed"
                current["error"] = "运行进程已退出，遥测未正常结束"
                current["updated_at"] = self._now()
                current["finished_at"] = current["updated_at"]
                self._write_current(current)
                self._append_history(current)
            elif not updated or (now - updated).total_seconds() > self.stale_after_seconds:
                current["status"] = "stale"
                current["error"] = "运行心跳已超时，请检查编排器进程"
                current["updated_at"] = self._now()
                self._write_current(current)
            return current

    def read_history(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        rows.reverse()
        return rows[: max(0, int(limit))] if limit is not None else rows
