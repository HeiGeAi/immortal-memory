#!/usr/bin/env python3
"""Bounded read-only product data for the Immortal control center."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import personal_model
from redact_common import redact


MODULE_IDS = (
    "overview",
    "runs",
    "sources",
    "memories",
    "profile",
    "agent",
    "backup",
    "diagnostics",
)


class ControlData:
    MEMORY_HIDDEN_HINT = "正文默认隐藏。选择记录后可在本机确认显示。"

    def __init__(
        self,
        immortal_dir: Path,
        *,
        skill_dir: Path | None = None,
        listen_address: str = "127.0.0.1",
        listen_port: int = 8765,
    ) -> None:
        self.immortal_dir = Path(immortal_dir)
        self.skill_dir = Path(skill_dir) if skill_dir else Path(__file__).resolve().parent
        self.listen_address = listen_address
        self.listen_port = int(listen_port)
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def capabilities(self) -> dict[str, Any]:
        availability = {
            "overview": True,
            "runs": True,
            "sources": True,
            "memories": (self.immortal_dir / "index.jsonl").is_file(),
            "profile": True,
            "agent": True,
            "backup": True,
            "diagnostics": True,
        }
        reasons = {
            "memories": "记忆索引尚未生成",
        }
        return {
            "schema_version": 1,
            "modules": [
                {
                    "id": module_id,
                    "available": availability[module_id],
                    "reason": "" if availability[module_id] else reasons.get(module_id, "能力不可用"),
                }
                for module_id in MODULE_IDS
            ],
            "actions": ["run", "health", "backup_verify", "profile_refresh"],
        }

    def readiness(self) -> tuple[int, dict[str, Any]]:
        runtime_dir = self.immortal_dir / "runtime"
        checks = {
            "vault_readable": self.immortal_dir.is_dir() and os.access(self.immortal_dir, os.R_OK),
            "runtime_writable": runtime_dir.is_dir() and os.access(runtime_dir, os.W_OK),
        }
        ready = all(checks.values())
        return (
            200 if ready else 503,
            {"status": "ready" if ready else "not_ready", "checks": checks},
        )

    @staticmethod
    def _automation_task_metadata(task: dict[str, Any]) -> dict[str, Any]:
        """Expose scheduling evidence without returning task input or execution output."""
        return {
            key: task.get(key)
            for key in (
                "id",
                "kind",
                "trigger",
                "status",
                "created_at",
                "started_at",
                "finished_at",
            )
        }

    def automation_status(self, runtime: Any) -> dict[str, Any]:
        """Return the bounded AutomationTasks dashboard model.

        Automation task params may contain a user's goal.  Command lines, task
        params, summaries and errors are intentionally omitted from this API.
        """
        state = runtime.status()
        tasks = [
            self._automation_task_metadata(task)
            for task in runtime.list_tasks()
            if isinstance(task, dict)
        ]
        current = next((task for task in tasks if task.get("status") == "running"), None)
        return {
            "paused": bool(state.get("paused")),
            "paused_at": state.get("paused_at"),
            "queued_count": int(state.get("queued_count") or 0),
            "running_count": int(state.get("running_count") or 0),
            "current": current,
            "recent": [
                task for task in tasks
                if task.get("status") in {"success", "attention", "failed", "canceled"}
            ][:8],
            "queued": [task for task in tasks if task.get("status") == "queued"][:8],
        }

    def automation_task_metadata(self, task: dict[str, Any]) -> dict[str, Any]:
        return self._automation_task_metadata(task)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
        values = query.get(key) or []
        return str(values[0]) if values else default

    @staticmethod
    def _memory_id(record: dict[str, Any]) -> str:
        existing = str(record.get("id") or "").strip()
        if existing and re.fullmatch(r"[A-Za-z0-9._:@+-]{1,180}", existing):
            return existing
        basis = "|".join(
            (
                str(record.get("source") or ""),
                str(record.get("timestamp") or ""),
                str(record.get("content") or "")[:400],
            )
        )
        return "memory-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]

    def _iter_memories(self):
        index_path = self.immortal_dir / "index.jsonl"
        try:
            handle = index_path.open(encoding="utf-8", errors="ignore")
        except OSError:
            return
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record

    def _sqlite_connection(self):
        db_path = self.immortal_dir / "search_index.db"
        if not db_path.is_file():
            return None
        try:
            connection = sqlite3.connect(
                f"file:{db_path}?mode=ro",
                uri=True,
                timeout=2,
            )
            connection.execute("pragma query_only=on")
            connection.execute("select 1 from docs limit 1")
            return connection
        except sqlite3.Error:
            try:
                connection.close()
            except (UnboundLocalError, sqlite3.Error):
                pass
            return None

    def _sqlite_index_state(self, connection) -> tuple[bool, int]:
        try:
            row = connection.execute(
                "select value from meta where key='last_size'"
            ).fetchone()
            indexed_bytes = int(row[0]) if row else 0
            source_bytes = (self.immortal_dir / "index.jsonl").stat().st_size
        except (OSError, TypeError, ValueError, sqlite3.Error):
            return False, 0
        return indexed_bytes >= source_bytes, max(0, source_bytes - indexed_bytes)

    def _sqlite_memories(
        self,
        *,
        q: str,
        source: str,
        since: str,
        until: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any] | None:
        connection = self._sqlite_connection()
        if connection is None:
            return None
        joins = ""
        clauses = []
        params: list[Any] = []
        use_fts = False
        try:
            if q:
                has_fts = connection.execute(
                    "select 1 from sqlite_master where type='table' and name='docs_fts'"
                ).fetchone()
                if has_fts and len(q) >= 3:
                    joins = " join docs_fts on docs_fts.rowid=d.rowid"
                    clauses.append("docs_fts match ?")
                    params.append('"' + q.replace('"', '""') + '"')
                    use_fts = True
                else:
                    clauses.append("d.content like ?")
                    params.append("%" + q + "%")
            if source:
                clauses.append("d.source=?")
                params.append(source)
            if since:
                clauses.append("d.ts>=?")
                params.append(since)
            if until:
                clauses.append("d.ts<=?")
                params.append(until)
            where = " where " + " and ".join(clauses) if clauses else ""
            count_sql = f"select count(*) from docs d{joins}{where}"
            total = int(connection.execute(count_sql, params).fetchone()[0])
            rows = connection.execute(
                "select d.rec_id,d.ts,d.source,d.role,d.project,d.content "
                f"from docs d{joins}{where} order by d.ts desc,d.rowid desc limit ? offset ?",
                [*params, limit, offset],
            ).fetchall()
            fresh, lag_bytes = self._sqlite_index_state(connection)
        except sqlite3.Error:
            connection.close()
            return None
        connection.close()
        items = []
        for rec_id, timestamp, row_source, _role, _project, _content in rows:
            items.append(
                {
                    "id": str(rec_id or ""),
                    "timestamp": str(timestamp or ""),
                    "source": str(row_source or ""),
                    "kind": str(_role or ""),
                    "sensitivity": "internal",
                    "content_state": "hidden",
                    "reveal_required": True,
                    "reveal_hint": self.MEMORY_HIDDEN_HINT,
                }
            )
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < total,
            "backend": "sqlite",
            "index_fresh": fresh,
            "index_lag_bytes": lag_bytes,
            "query_mode": "fts" if use_fts else ("like" if q else "index"),
        }

    def _sqlite_memory_detail(
        self,
        memory_id: str,
        *,
        reveal: bool,
    ) -> dict[str, Any] | None:
        connection = self._sqlite_connection()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "select rec_id,ts,source,role,project,content "
                "from docs where rec_id=? order by rowid desc limit 1",
                (memory_id,),
            ).fetchone()
        except sqlite3.Error:
            connection.close()
            return None
        connection.close()
        if not row:
            raise KeyError("memory not found")
        rec_id, timestamp, source, role, project, content = row
        base = {
            "id": str(rec_id or ""),
            "timestamp": str(timestamp or ""),
            "source": str(source or ""),
            "kind": str(role or ""),
            "sensitivity": "internal",
            "project": str(project or ""),
            "role": str(role or ""),
            "content_state": "revealed" if reveal else "hidden",
            "reveal_required": not reveal,
            "reveal_hint": self.MEMORY_HIDDEN_HINT,
        }
        if reveal:
            base["content"] = redact(str(content or ""))
        return base

    def memories(self, query: dict[str, list[str]]) -> dict[str, Any]:
        allowed = {"q", "source", "kind", "since", "until", "limit", "offset"}
        unknown = set(query) - allowed
        if unknown:
            raise ValueError(f"unsupported query parameter: {sorted(unknown)[0]}")
        try:
            limit = min(50, max(1, int(self._query_value(query, "limit", "20"))))
            offset = max(0, int(self._query_value(query, "offset", "0")))
        except ValueError as exc:
            raise ValueError("limit and offset must be integers") from exc
        q = self._query_value(query, "q").strip().casefold()
        source = self._query_value(query, "source").strip()
        kind = self._query_value(query, "kind").strip()
        since = self._query_value(query, "since").strip()
        until = self._query_value(query, "until").strip()
        if not kind:
            indexed = self._sqlite_memories(
                q=q,
                source=source,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )
            if indexed is not None:
                return indexed
        matched: list[dict[str, Any]] = []
        for record in self._iter_memories() or []:
            timestamp = str(record.get("timestamp") or "")
            record_source = str(record.get("source") or "")
            record_kind = str(record.get("type") or record.get("kind") or "")
            content = str(record.get("content") or "")
            if source and record_source != source:
                continue
            if kind and record_kind != kind:
                continue
            if since and timestamp < since:
                continue
            if until and timestamp > until:
                continue
            if q and q not in " ".join(
                (
                    content,
                    record_source,
                    record_kind,
                    str(record.get("project") or ""),
                )
            ).casefold():
                continue
            matched.append(
                {
                    "id": self._memory_id(record),
                    "timestamp": timestamp,
                    "source": record_source,
                    "kind": record_kind,
                    "sensitivity": str(record.get("sensitivity") or "internal"),
                    "content_state": "hidden",
                    "reveal_required": True,
                    "reveal_hint": self.MEMORY_HIDDEN_HINT,
                }
            )
        matched.sort(key=lambda item: (item["timestamp"], item["id"]), reverse=True)
        return {
            "items": matched[offset : offset + limit],
            "total": len(matched),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(matched),
            "backend": "jsonl",
            "index_fresh": True,
            "index_lag_bytes": 0,
            "query_mode": "scan",
        }

    def memory_detail(self, memory_id: str, *, reveal: bool = False) -> dict[str, Any]:
        requested = str(memory_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:@+-]{1,180}", requested):
            raise ValueError("invalid memory id")
        indexed = self._sqlite_memory_detail(requested, reveal=reveal)
        if indexed is not None:
            return indexed
        for record in self._iter_memories() or []:
            if self._memory_id(record) != requested:
                continue
            base = {
                "id": requested,
                "timestamp": str(record.get("timestamp") or ""),
                "source": str(record.get("source") or ""),
                "kind": str(record.get("type") or record.get("kind") or ""),
                "sensitivity": str(record.get("sensitivity") or "internal"),
                "project": str(record.get("project") or ""),
                "role": str(record.get("role") or ""),
                "content_state": "revealed" if reveal else "hidden",
                "reveal_required": not reveal,
                "reveal_hint": self.MEMORY_HIDDEN_HINT,
            }
            if reveal:
                base["content"] = redact(str(record.get("content") or ""))
            return {
                **base,
            }
        raise KeyError("memory not found")

    @staticmethod
    def _freshness(value: str, *, stale_hours: int = 48) -> str:
        if not value:
            return "unknown"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
        except ValueError:
            return "unknown"
        return "fresh" if age <= stale_hours * 3600 else "stale"

    @staticmethod
    def _source_status(value: Any, *, has_success: bool = False) -> str:
        status = str(value or "").strip().lower()
        if status in {"partial", "error", "failed", "rate_limited", "account_protected", "permission_denied"}:
            return status
        if status in {"disabled", "skipped", "not_due"}:
            return "skipped"
        if status in {"ok", "success", "healthy"} or has_success:
            return "success"
        return "unknown"

    def _newest_feishu_source_backup(self, source_prefix: str = "feishu-") -> str:
        """Return the newest Feishu source metadata timestamp, without reading content."""
        sources = self._read_json(self.immortal_dir / "sources.json")
        newest_value = ""
        newest_time: datetime | None = None
        for source in sources.get("sources") or []:
            if not isinstance(source, dict) or not str(source.get("type") or "").startswith(source_prefix):
                continue
            value = str(source.get("last_backup") or "").strip()
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                parsed = parsed.astimezone(timezone.utc)
            except ValueError:
                continue
            if newest_time is None or parsed > newest_time:
                newest_value = value
                newest_time = parsed
        return newest_value

    def sources(self) -> dict[str, Any]:
        state = self._read_json(self.immortal_dir / "orchestrator_state.json")
        web = self._read_json(self.immortal_dir / "web" / "state.json")
        config = self._read_json(self.immortal_dir / "config.json")
        feishu_config = config.get("feishu") if isinstance(config.get("feishu"), dict) else {}
        external_config = config.get("external_sources") if isinstance(config.get("external_sources"), dict) else {}
        external_state = self._read_json(self.immortal_dir / "external_sources" / "state.json")
        external_results = (
            external_state.get("sources") if isinstance(external_state.get("sources"), dict) else {}
        )
        external_last = str(external_state.get("generated_at") or "")
        feishu_daily_sources = {
            item.strip()
            for item in str(feishu_config.get("daily_sources") or "").split(",")
            if item.strip()
        }
        feishu_last = self._newest_feishu_source_backup() or str(state.get("last_feishu_collect") or "")
        feishu_mail_last = self._newest_feishu_source_backup("feishu-mail")
        source_specs = [
            {
                "id": "local",
                "label": "本地会话",
                "last": str(state.get("last_collect") or ""),
                "status": self._source_status("", has_success=bool(state.get("last_collect"))),
                "increment": int(state.get("last_run_new_records") or 0),
                "errors": int("collect failed" in (state.get("errors") or [])),
                "evidence": "orchestrator_state.json",
            },
            {
                "id": "feishu",
                "label": "飞书",
                "last": feishu_last,
                "status": self._source_status(
                    state.get("last_feishu_status"),
                    has_success=bool(feishu_last),
                ),
                "increment": int(state.get("last_run_feishu_new_records") or 0),
                "errors": sum("feishu" in str(item) for item in (state.get("errors") or [])),
                "evidence": "sources.json + orchestrator_state.json",
            },
            {
                "id": "feishu-mail",
                "label": "飞书邮件（可选）",
                "last": feishu_mail_last,
                "status": self._source_status("", has_success=bool(feishu_mail_last))
                if "mail" in feishu_daily_sources
                else "skipped",
                "increment": 0,
                "errors": 0,
                "evidence": "config.json + sources.json",
            },
            *[
                {
                    "id": source_id,
                    "label": label,
                    "last": external_last if enabled else "",
                    "status": self._source_status(
                        result.get("status"),
                        has_success=bool(external_last),
                    )
                    if enabled
                    else "skipped",
                    "increment": int(result.get("records_written") or 0),
                    "errors": int(result.get("error_count") or 0),
                    "evidence": "config.json + external_sources/state.json",
                }
                for kind, source_id, label in (
                    ("git", "git-history", "Git 本地历史"),
                    ("github", "github-history", "GitHub PR / Issue"),
                    ("claude-web", "claude-web", "Claude Web 导出"),
                    ("chatgpt", "chatgpt", "ChatGPT 导出"),
                    ("cursor", "cursor", "Cursor 导出"),
                )
                for configured in [
                    external_config.get(kind) if isinstance(external_config.get(kind), dict) else {}
                ]
                for enabled in [bool(configured.get("enabled") and configured.get("paths"))]
                for result in [
                    external_results.get(kind) if isinstance(external_results.get(kind), dict) else {}
                ]
            ],
            {
                "id": "web",
                "label": "网页访问",
                "last": str(state.get("last_web_collect") or ""),
                "status": self._source_status(
                    web.get("status"),
                    has_success=bool(state.get("last_web_collect")),
                ),
                "increment": int(state.get("last_run_web_new_records") or 0),
                "errors": sum("web" in str(item) for item in (state.get("errors") or [])),
                "evidence": "web/state.json",
            },
            {
                "id": "obsidian",
                "label": "Obsidian 阅读层",
                "last": str(state.get("last_obsidian_sync") or ""),
                "status": self._source_status(
                    state.get("last_obsidian_sync_status"),
                    has_success=bool(state.get("last_obsidian_sync")),
                ),
                "increment": 0,
                "errors": sum("obsidian" in str(item) for item in (state.get("errors") or [])),
                "evidence": "orchestrator_state.json",
            },
            {
                "id": "backup",
                "label": "便携备份",
                "last": str(state.get("last_portable_export") or ""),
                "status": self._source_status(
                    state.get("last_portable_restore_check_status"),
                    has_success=bool(state.get("last_portable_export")),
                ),
                "increment": int(state.get("last_portable_export_files") or 0),
                "errors": sum("portable" in str(item) for item in (state.get("errors") or [])),
                "evidence": "exports/*/manifest.json",
            },
        ]
        items = [
            {
                "id": spec["id"],
                "label": spec["label"],
                "status": spec["status"],
                "last_attempt": spec["last"],
                "last_success": spec["last"] if spec["status"] == "success" else "",
                "increment": spec["increment"],
                "error_count": spec["errors"],
                "evidence": spec["evidence"],
                "freshness": self._freshness(spec["last"]),
            }
            for spec in source_specs
        ]
        return {"items": items, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    @staticmethod
    def _file_metadata(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {"exists": False, "updated_at": "", "bytes": 0}
        try:
            stat = path.stat()
        except OSError:
            return {"exists": False, "updated_at": "", "bytes": 0}
        return {
            "exists": True,
            "updated_at": datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(timespec="seconds"),
            "bytes": stat.st_size,
        }

    def _agent_contexts(self, limit: int = 10) -> list[dict[str, Any]]:
        sessions_dir = self.immortal_dir / "sessions"
        try:
            session_dirs = [
                path
                for path in sessions_dir.iterdir()
                if path.is_dir() and (path / "manifest.json").is_file()
            ]
        except OSError:
            return []
        session_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        items = []
        for path in session_dirs[: max(1, min(20, limit))]:
            manifest = self._read_json(path / "manifest.json")
            returncode = int(manifest.get("returncode") or 0)
            items.append(
                {
                    "slug": path.name,
                    "goal": str(manifest.get("query") or path.name)[:160],
                    "mode": str(manifest.get("mode") or ""),
                    "generated_at": str(
                        manifest.get("generated_at")
                        or self._file_metadata(path / "manifest.json")["updated_at"]
                    ),
                    "status": "ready" if returncode == 0 else "attention",
                }
            )
        return items

    def personal_model_status(self) -> dict[str, Any]:
        model_path = self.immortal_dir / "models" / "personal_model.json"
        markdown_path = self.immortal_dir / "models" / "personal_model.md"
        model = self._read_json(model_path)
        if not model:
            return {
                "available": False,
                "status": "missing",
                "generated_at": "",
                "revision": "",
                "accepted_models": 0,
                "heuristics": 0,
                "boundaries": 0,
                "active_corrections": 0,
                "corrections": [],
                "quality_checks": [],
                "content_available": False,
            }
        status = personal_model.metadata(model)
        status["content_available"] = markdown_path.is_file()
        return status

    def personal_model_detail(self, *, reveal: bool = False) -> dict[str, Any]:
        status = self.personal_model_status()
        result = {
            **status,
            "content_state": "hidden",
            "reveal_required": True,
            "reveal_hint": "模型正文默认隐藏。确认后仅在本机抽屉中显示。",
        }
        if not reveal:
            return result
        markdown_path = self.immortal_dir / "models" / "personal_model.md"
        try:
            content = redact(markdown_path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            content = ""
        result.update(
            {
                "content_state": "revealed",
                "reveal_required": False,
                "content": content,
            }
        )
        return result

    def agent_status(self) -> dict[str, Any]:
        entry = self._file_metadata(self.immortal_dir / "agent" / "ENTRY.md")
        contexts = self._agent_contexts()
        return {
            "available": bool(entry["exists"]),
            "entry": entry,
            "personal_model": self.personal_model_status(),
            "contexts": contexts,
            "supported_actions": [
                {
                    "id": "create_context",
                    "fields": ["goal", "mode"],
                    "modes": ["auto", "answer", "code", "research", "plan"],
                }
            ],
        }

    def agent_context_detail(self, slug: str) -> dict[str, Any]:
        safe_slug = str(slug or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", safe_slug):
            raise ValueError("invalid context id")
        manifest_path = self.immortal_dir / "sessions" / safe_slug / "manifest.json"
        manifest = self._read_json(manifest_path)
        if not manifest:
            raise KeyError("context not found")
        return {
            "slug": safe_slug,
            "goal": str(manifest.get("query") or safe_slug)[:160],
            "mode": str(manifest.get("mode") or ""),
            "mode_label": str(manifest.get("mode_label") or ""),
            "generated_at": str(manifest.get("generated_at") or ""),
            "status": "ready" if int(manifest.get("returncode") or 0) == 0 else "attention",
            "artifacts": {
                name: self._file_metadata(manifest_path.parent / name)
                for name in ("TASK_CONTEXT.md", "SYSTEM_PROMPT.md", "manifest.json")
            },
        }

    def backups(self) -> dict[str, Any]:
        state = self._read_json(self.immortal_dir / "orchestrator_state.json")
        manifests: list[Path] = []
        try:
            manifests = list((self.immortal_dir / "exports").glob("*/manifest.json"))
        except OSError:
            pass
        configured = state.get("last_portable_export_dir")
        if configured:
            configured_manifest = Path(str(configured)).expanduser() / "manifest.json"
            if configured_manifest not in manifests:
                manifests.append(configured_manifest)
        manifests = [path for path in manifests if path.is_file()]
        manifests.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        items = []
        for manifest_path in manifests[:20]:
            manifest = self._read_json(manifest_path)
            if not manifest:
                continue
            export_dir = manifest_path.parent
            risks = []
            try:
                same_disk = export_dir.stat().st_dev == self.immortal_dir.stat().st_dev
            except OSError:
                same_disk = False
            if same_disk:
                risks.append("same_disk")
            is_latest = configured and export_dir == Path(str(configured)).expanduser()
            restore_status = (
                str(state.get("last_portable_restore_check_status") or "")
                if is_latest
                else ""
            )
            verification = manifest.get("verification")
            verified = (
                restore_status == "ok"
                or (
                    isinstance(verification, dict)
                    and bool(verification.get("ok"))
                )
            )
            if not verified:
                risks.append("not_verified")
            secret_scan = manifest.get("secret_scan")
            secret_count = (
                int(secret_scan.get("unique_candidates") or 0)
                if isinstance(secret_scan, dict)
                else 0
            )
            warnings = [str(item) for item in (manifest.get("warnings") or [])]
            if secret_count or any("secret_shapes_present" in item for item in warnings):
                risks.append("secret_shapes_present")
            generated_at = str(manifest.get("generated_at") or "")
            if self._freshness(generated_at, stale_hours=168) == "stale":
                risks.append("stale")
            totals = manifest.get("totals") if isinstance(manifest.get("totals"), dict) else {}
            items.append(
                {
                    "id": export_dir.name,
                    "generated_at": generated_at,
                    "files": int(totals.get("files") or 0),
                    "bytes": int(totals.get("bytes") or 0),
                    "verified": verified,
                    "restore_check_status": restore_status or "unknown",
                    "status": "attention" if risks else "healthy",
                    "risks": risks,
                    "manifest": self._file_metadata(manifest_path),
                }
            )
        return {
            "items": items,
            "actions": [{"id": "verify", "label": "校验最新备份"}],
            "restore_available": False,
            "delete_available": False,
        }

    def diagnostics(self) -> dict[str, Any]:
        try:
            version = (self.skill_dir / "VERSION").read_text(
                encoding="utf-8",
                errors="ignore",
            ).strip()
        except OSError:
            version = "unknown"
        ready_status, readiness = self.readiness()
        return {
            "version": version or "unknown",
            "service_started_at": self.started_at,
            "listen_address": self.listen_address,
            "listen_port": self.listen_port,
            "python": platform.python_version(),
            "scheduler": {
                "status": "see_overview",
                "detail": "调度器证据由 overview 实时探针提供",
            },
            "dependencies": {
                **readiness["checks"],
                "index_available": (self.immortal_dir / "index.jsonl").is_file(),
                "profile_available": (self.immortal_dir / "profile.json").is_file(),
                "agent_entry_available": (
                    self.immortal_dir / "agent" / "ENTRY.md"
                ).is_file(),
            },
            "ready": ready_status == 200,
        }
