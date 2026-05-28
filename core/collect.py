#!/usr/bin/env python3
from __future__ import annotations
"""
永生记忆库 — 全量采集器 v0.3
采集范围：
  1. Claude Code 对话记录 (~/.claude/projects/*/)
  2. 记忆/上下文文档 (~/.claude/projects/*/memory/)
  3. 文件历史快照 (~/.claude/file-history/)
  4. 粘贴缓存 (~/.claude/paste-cache/)
  5. Skill 定义与资源 (~/.claude/skills/*/)
  6. 桌面产出目录 (~/Desktop/claudecode/)
  7. Codex 对话记录 (~/.codex/sessions/*/rollout-*.jsonl)
  8. Codex 记忆文档 (~/.codex/memories/)
  9. Codex Skill 资源 (~/.codex/skills/*/)
  10. Codex 产出文件 (~/Documents/Codex/)
"""

import json
import os
import re
import sys
import uuid
import hashlib
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path


IMMORTAL_DIR = Path.home() / ".immortal"
DAILY_DIR = IMMORTAL_DIR / "daily"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
SOURCES_FILE = IMMORTAL_DIR / "sources.json"
FILES_DIR = IMMORTAL_DIR / "files"

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"
CLAUDE_FILE_HISTORY = CLAUDE_DIR / "file-history"
CLAUDE_PASTE_CACHE = CLAUDE_DIR / "paste-cache"
CLAUDE_SKILLS = CLAUDE_DIR / "skills"
DESKTOP_OUTPUT = Path.home() / "Desktop" / "claudecode"
CODEX_DIR = Path.home() / ".codex"
CODEX_SESSIONS = CODEX_DIR / "sessions"
CODEX_ARCHIVED = CODEX_DIR / "archived_sessions"
CODEX_MEMORIES = CODEX_DIR / "memories"
CODEX_SKILLS = CODEX_DIR / "skills"
CODEX_STATE_DB = CODEX_DIR / "state_5.sqlite"
CODEX_DOCS_OUTPUT = Path.home() / "Documents" / "Codex"
_DEDUP_CACHE: dict[str, set] | None = None


# ============================================================
# 工具函数
# ============================================================

def decode_project_path(dir_name: str) -> str:
    if not dir_name.startswith("-"):
        return dir_name
    return "/" + dir_name[1:].replace("-", "/")


def get_date_from_timestamp(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return datetime.now().strftime("%Y-%m-%d")


def get_date_from_mtime(path: Path) -> str:
    try:
        dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except OSError:
        return datetime.now().strftime("%Y-%m-%d")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_sources_config() -> dict:
    with open(SOURCES_FILE, "r") as f:
        return json.load(f)


def save_sources_config(config: dict):
    with open(SOURCES_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_existing_dedup_set(source_type: str) -> set:
    """加载指定类型的去重集合。"""
    global _DEDUP_CACHE
    if _DEDUP_CACHE is None:
        _DEDUP_CACHE = {}
        if INDEX_FILE.exists():
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        source = record.get("source") or ""
                        if not source:
                            continue
                        dedup_key = record.get("_dedup_key", "") or legacy_dedup_key(record)
                        if dedup_key:
                            _DEDUP_CACHE.setdefault(source, set()).add(dedup_key)
                    except (json.JSONDecodeError, KeyError):
                        continue
    return _DEDUP_CACHE.setdefault(source_type, set())


def legacy_dedup_key(record: dict) -> str:
    """Rebuild dedup keys for records written before _dedup_key was retained.

    Older versions stripped internal keys before writing index.jsonl. Conversation
    sources are the main high-volume repeat offenders, and their keys can be
    reconstructed from stable record fields.
    """
    source = record.get("source", "")
    session_id = record.get("session_id", "")
    ts = record.get("timestamp", "")
    role = record.get("role", "")
    content = record.get("content", "") or ""
    if source == "claude-code-conversation" and session_id and ts:
        return f"conv|{session_id}|{ts}"
    if source == "codex-conversation" and session_id and ts and role:
        return f"codex-conv|{session_id}|{ts}|{role}|{hashlib.md5(content.encode()).hexdigest()[:8]}"
    if source == "hermes-conversation" and session_id and role and content:
        return f"hermes|{session_id}|{role}|{hashlib.md5(content.encode()).hexdigest()[:10]}"
    return ""


def write_records(records_by_date: dict):
    """将记录写入日文件和全量索引。"""
    for date_str, records in sorted(records_by_date.items()):
        daily_file = DAILY_DIR / f"{date_str}.jsonl"
        with open(daily_file, "a", encoding="utf-8") as f:
            for record in records:
                # 写入前去掉内部去重字段
                clean = {k: v for k, v in record.items() if not k.startswith("_")}
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")

        with open(INDEX_FILE, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


def copy_to_immortal(src: Path, category: str, date_str: str) -> Optional[str]:
    """将文件复制到 ~/.immortal/files/ 下，返回相对路径。"""
    try:
        ext = src.suffix or ".bin"
        h = file_hash(src)
        dest_dir = FILES_DIR / category / date_str
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{h}{ext}"
        if not dest.exists():
            import shutil
            shutil.copy2(src, dest)
        return f"files/{category}/{date_str}/{h}{ext}"
    except (OSError, IOError):
        return None


# ============================================================
# 采集器 1: Claude Code 对话记录
# ============================================================

def extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    if isinstance(tool_input, dict):
                        # 提取工具调用中的关键信息
                        if "file_path" in tool_input:
                            texts.append(f"[Tool: {tool_name}] file: {tool_input['file_path']}")
                        elif "command" in tool_input:
                            texts.append(f"[Tool: {tool_name}] {tool_input['command'][:200]}")
                        else:
                            texts.append(f"[Tool: {tool_name}]")
                    else:
                        texts.append(f"[Tool: {tool_name}]")
                elif block.get("type") == "tool_result":
                    inner = block.get("content", "")
                    if isinstance(inner, list):
                        for ib in inner:
                            if isinstance(ib, dict) and ib.get("type") == "text":
                                texts.append(ib.get("text", "")[:500])
                    elif isinstance(inner, str):
                        texts.append(inner[:500])
        return "\n".join(texts)
    return ""


def collect_conversations(since: Optional[str] = None) -> dict:
    stats = {
        "files_scanned": 0,
        "records_collected": 0,
        "sessions_found": 0,
        "by_project": {},
    }

    existing = load_existing_dedup_set("claude-code-conversation")
    daily_buffers = {}

    for project_dir in sorted(CLAUDE_PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        project_path = decode_project_path(project_dir.name)
        jsonl_files = sorted(project_dir.glob("*.jsonl"))
        stats["sessions_found"] += len(jsonl_files)

        for jsonl_file in jsonl_files:
            stats["files_scanned"] += 1

            if since:
                file_mtime = datetime.fromtimestamp(
                    jsonl_file.stat().st_mtime, tz=timezone.utc
                )
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if file_mtime < since_dt:
                    continue

            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = raw.get("type", "")
                    if msg_type not in ("user", "assistant"):
                        continue

                    message = raw.get("message", {})
                    role = message.get("role", msg_type)
                    content = message.get("content", "")
                    text = extract_text_from_content(content)
                    if not text.strip() or len(text.strip()) < 2:
                        continue

                    ts = raw.get("timestamp", "")
                    session_id = raw.get("sessionId", "")
                    dedup_key = f"conv|{session_id}|{ts}"
                    if dedup_key in existing:
                        continue
                    existing.add(dedup_key)

                    # 提取工具
                    tools = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tools.append(block.get("name", ""))

                    date_str = get_date_from_timestamp(ts)
                    record = {
                        "id": str(uuid.uuid4()),
                        "source": "claude-code-conversation",
                        "project": project_path,
                        "session_id": session_id,
                        "timestamp": ts,
                        "type": "conversation",
                        "role": role,
                        "content": text[:10000],
                        "tools_used": tools,
                        "_dedup_key": dedup_key,
                    }

                    if date_str not in daily_buffers:
                        daily_buffers[date_str] = []
                    daily_buffers[date_str].append(record)
                    stats["records_collected"] += 1
                    stats["by_project"][project_path] = stats["by_project"].get(project_path, 0) + 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 2: 记忆文档 (~/.claude/projects/*/memory/)
# ============================================================

def collect_memory_docs(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0, "by_project": {}}
    existing = load_existing_dedup_set("claude-code-memory")
    daily_buffers = {}

    for memory_dir in sorted(CLAUDE_PROJECTS_DIR.rglob("memory")):
        if not memory_dir.is_dir():
            continue
        project_dir = memory_dir.parent
        project_path = decode_project_path(project_dir.name)

        for doc_file in sorted(memory_dir.rglob("*")):
            if not doc_file.is_file():
                continue
            if doc_file.name.startswith("."):
                continue

            stats["files_scanned"] += 1

            if since:
                mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if mtime < since_dt:
                    continue

            dedup_key = f"mem|{doc_file.relative_to(memory_dir)}|{file_hash(doc_file)}"
            if dedup_key in existing:
                continue
            existing.add(dedup_key)

            # 读取文档内容
            try:
                content = doc_file.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                continue

            date_str = get_date_from_mtime(doc_file)
            relative_path = str(doc_file.relative_to(memory_dir))

            record = {
                "id": str(uuid.uuid4()),
                "source": "claude-code-memory",
                "project": project_path,
                "session_id": "",
                "timestamp": datetime.fromtimestamp(
                    doc_file.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "type": "memory_doc",
                "role": "system",
                "content": content,
                "file_name": relative_path,
                "file_size": doc_file.stat().st_size,
                "_dedup_key": dedup_key,
            }

            if date_str not in daily_buffers:
                daily_buffers[date_str] = []
            daily_buffers[date_str].append(record)
            stats["records_collected"] += 1
            stats["by_project"][project_path] = stats["by_project"].get(project_path, 0) + 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 3: 文件历史快照 (~/.claude/file-history/)
# ============================================================

def collect_file_history(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0}
    existing = load_existing_dedup_set("claude-code-file-history")
    daily_buffers = {}

    if not CLAUDE_FILE_HISTORY.exists():
        return stats

    for session_dir in sorted(CLAUDE_FILE_HISTORY.iterdir()):
        if not session_dir.is_dir():
            continue

        for version_file in sorted(session_dir.iterdir()):
            if not version_file.is_file():
                continue

            stats["files_scanned"] += 1

            if since:
                mtime = datetime.fromtimestamp(version_file.stat().st_mtime, tz=timezone.utc)
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if mtime < since_dt:
                    continue

            dedup_key = f"fh|{session_dir.name}|{version_file.name}|{file_hash(version_file)}"
            if dedup_key in existing:
                continue
            existing.add(dedup_key)

            # 读取文件内容
            is_binary = False
            try:
                content = version_file.read_text(encoding="utf-8", errors="strict")[:20000]
            except (UnicodeDecodeError, ValueError):
                is_binary = True
                content = f"[Binary file, {version_file.stat().st_size} bytes]"

            date_str = get_date_from_mtime(version_file)

            # 复制到 immortal files
            immortal_path = None
            if is_binary:
                immortal_path = copy_to_immortal(version_file, "file-history", date_str)

            record = {
                "id": str(uuid.uuid4()),
                "source": "claude-code-file-history",
                "project": "",
                "session_id": session_dir.name,
                "timestamp": datetime.fromtimestamp(
                    version_file.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "type": "file_snapshot",
                "role": "system",
                "content": content,
                "file_name": version_file.name,
                "file_size": version_file.stat().st_size,
                "is_binary": is_binary,
                "immortal_path": immortal_path or "",
                "_dedup_key": dedup_key,
            }

            if date_str not in daily_buffers:
                daily_buffers[date_str] = []
            daily_buffers[date_str].append(record)
            stats["records_collected"] += 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 4: 粘贴缓存 (~/.claude/paste-cache/)
# ============================================================

def collect_paste_cache(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0}
    existing = load_existing_dedup_set("claude-code-paste-cache")
    daily_buffers = {}

    if not CLAUDE_PASTE_CACHE.exists():
        return stats

    for cache_file in sorted(CLAUDE_PASTE_CACHE.iterdir()):
        if not cache_file.is_file():
            continue

        stats["files_scanned"] += 1

        if since:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc)
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if mtime < since_dt:
                continue

        dedup_key = f"paste|{cache_file.name}|{file_hash(cache_file)}"
        if dedup_key in existing:
            continue
        existing.add(dedup_key)

        try:
            content = cache_file.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue

        date_str = get_date_from_mtime(cache_file)

        record = {
            "id": str(uuid.uuid4()),
            "source": "claude-code-paste-cache",
            "project": "",
            "session_id": "",
            "timestamp": datetime.fromtimestamp(
                cache_file.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "type": "user_input",
            "role": "user",
            "content": content,
            "file_name": cache_file.name,
            "file_size": cache_file.stat().st_size,
            "_dedup_key": dedup_key,
        }

        if date_str not in daily_buffers:
            daily_buffers[date_str] = []
        daily_buffers[date_str].append(record)
        stats["records_collected"] += 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 5: Skill 定义与资源 (~/.claude/skills/*/)
# ============================================================

def collect_skills(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0, "by_skill": {}}
    existing = load_existing_dedup_set("claude-code-skill")
    daily_buffers = {}

    if not CLAUDE_SKILLS.exists():
        return stats

    for skill_dir in sorted(CLAUDE_SKILLS.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name

        for doc_file in sorted(skill_dir.rglob("*")):
            if not doc_file.is_file():
                continue
            if doc_file.name.startswith("."):
                continue
            # 跳过 immortal 自己
            if skill_name == "immortal":
                continue

            stats["files_scanned"] += 1

            if since:
                mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if mtime < since_dt:
                    continue

            dedup_key = f"skill|{skill_name}|{doc_file.relative_to(skill_dir)}|{file_hash(doc_file)}"
            if dedup_key in existing:
                continue
            existing.add(dedup_key)

            # 跳过过大文件
            if doc_file.stat().st_size > 500000:
                content = f"[File too large: {doc_file.stat().st_size} bytes]"
            else:
                try:
                    content = doc_file.read_text(encoding="utf-8", errors="replace")[:30000]
                except OSError:
                    continue

            date_str = get_date_from_mtime(doc_file)
            relative_path = str(doc_file.relative_to(skill_dir))

            record = {
                "id": str(uuid.uuid4()),
                "source": "claude-code-skill",
                "project": "",
                "session_id": "",
                "timestamp": datetime.fromtimestamp(
                    doc_file.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "type": "skill_resource",
                "role": "system",
                "content": content,
                "skill_name": skill_name,
                "file_name": relative_path,
                "file_size": doc_file.stat().st_size,
                "_dedup_key": dedup_key,
            }

            if date_str not in daily_buffers:
                daily_buffers[date_str] = []
            daily_buffers[date_str].append(record)
            stats["records_collected"] += 1
            stats["by_skill"][skill_name] = stats["by_skill"].get(skill_name, 0) + 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 6: 桌面产出目录 (~/Desktop/claudecode/)
# ============================================================

def collect_desktop_output(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0, "by_type": {}}
    existing = load_existing_dedup_set("desktop-output")
    daily_buffers = {}

    if not DESKTOP_OUTPUT.exists():
        return stats

    TEXT_EXTENSIONS = {
        ".md", ".txt", ".py", ".js", ".ts", ".html", ".css", ".json",
        ".yaml", ".yml", ".toml", ".sh", ".bash", ".zsh", ".sql",
        ".go", ".rs", ".java", ".c", ".cpp", ".h", ".jsx", ".tsx",
        ".vue", ".svelte", ".xml", ".csv", ".log", ".env", ".gitignore",
        ".dockerfile", ".makefile", ".cmake",
    }
    SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv", ".next",
        "dist", "build", "build-runtime", ".playwright-cli",
        "_analysis_zip", "_analysis_nested", "analysis",
    }

    for doc_file in sorted(DESKTOP_OUTPUT.rglob("*")):
        if not doc_file.is_file():
            continue
        # 跳过特定目录
        if any(part in SKIP_DIRS for part in doc_file.parts):
            continue
        if doc_file.name.startswith("."):
            continue

        stats["files_scanned"] += 1

        if since:
            mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if mtime < since_dt:
                continue

        dedup_key = f"desk|{doc_file.relative_to(DESKTOP_OUTPUT)}|{file_hash(doc_file)}"
        if dedup_key in existing:
            continue
        existing.add(dedup_key)

        is_text = doc_file.suffix.lower() in TEXT_EXTENSIONS
        content = ""
        is_binary = False

        if is_text and doc_file.stat().st_size < 500000:
            try:
                content = doc_file.read_text(encoding="utf-8", errors="replace")[:30000]
            except OSError:
                content = f"[Cannot read file]"
        else:
            is_binary = True
            ext = doc_file.suffix.lower()
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}:
                content = f"[Image: {doc_file.name}, {doc_file.stat().st_size} bytes]"
            elif ext in {".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav"}:
                content = f"[Media: {doc_file.name}, {doc_file.stat().st_size} bytes]"
            elif ext in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".tar", ".gz"}:
                content = f"[Archive/Doc: {doc_file.name}, {doc_file.stat().st_size} bytes]"
            else:
                content = f"[File: {doc_file.name}, {doc_file.stat().st_size} bytes]"

            # 复制到 immortal files
            copy_to_immortal(doc_file, "desktop-output", get_date_from_mtime(doc_file))

        date_str = get_date_from_mtime(doc_file)
        relative_path = str(doc_file.relative_to(DESKTOP_OUTPUT))

        record = {
            "id": str(uuid.uuid4()),
            "source": "desktop-output",
            "project": "",
            "session_id": "",
            "timestamp": datetime.fromtimestamp(
                doc_file.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "type": "generated_file",
            "role": "system",
            "content": content,
            "file_name": relative_path,
            "file_size": doc_file.stat().st_size,
            "is_binary": is_binary,
            "_dedup_key": dedup_key,
        }

        if date_str not in daily_buffers:
            daily_buffers[date_str] = []
        daily_buffers[date_str].append(record)
        stats["records_collected"] += 1
        file_type = doc_file.suffix.lower() or "other"
        stats["by_type"][file_type] = stats["by_type"].get(file_type, 0) + 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 7: Codex 对话记录 (~/.codex/sessions/*/rollout-*.jsonl)
# ============================================================

def _codex_thread_metadata() -> dict:
    """从 SQLite 读取 Codex threads 元数据。"""
    threads = {}
    if not CODEX_STATE_DB.exists():
        return threads
    try:
        import sqlite3
        conn = sqlite3.connect(str(CODEX_STATE_DB))
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT id, title, cwd, model, datetime(created_at_ms/1000, 'unixepoch') as created, "
            "tokens_used, archived, first_user_message FROM threads"
        ):
            threads[row["id"]] = {
                "title": row["title"] or "",
                "cwd": row["cwd"] or "",
                "model": row["model"] or "",
                "created": row["created"] or "",
                "tokens_used": row["tokens_used"] or 0,
                "archived": bool(row["archived"]),
                "first_user_message": (row["first_user_message"] or "")[:200],
            }
        conn.close()
    except Exception:
        pass
    return threads


def _extract_codex_text(content) -> str:
    """从 Codex 消息 content 中提取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype in ("input_text", "output_text"):
                texts.append(block.get("text", ""))
            elif btype == "tool_call":
                texts.append(f"[Tool: {block.get('name', '?')}]")
            elif btype == "tool_call_output":
                texts.append((block.get("output", "") or "")[:500])
        return "\n".join(texts)
    return ""


def collect_codex_conversations(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0, "sessions_found": 0}
    existing = load_existing_dedup_set("codex-conversation")
    daily_buffers = {}
    thread_meta = _codex_thread_metadata()

    rollout_files = []
    for search_dir in [CODEX_SESSIONS, CODEX_ARCHIVED]:
        if search_dir.exists():
            rollout_files.extend(search_dir.rglob("rollout-*.jsonl"))

    stats["sessions_found"] = len(rollout_files)

    for rollout_file in sorted(rollout_files):
        stats["files_scanned"] += 1

        if since:
            mtime = datetime.fromtimestamp(rollout_file.stat().st_mtime, tz=timezone.utc)
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if mtime < since_dt:
                continue

        # 从文件名提取 thread_id
        fname = rollout_file.name
        thread_id = ""
        # 格式: rollout-2026-03-28T17-50-21-019d33da-...-.jsonl
        parts = fname.replace("rollout-", "").replace(".jsonl", "").split("-")
        # UUID v7 在最后5段: 019d33da-0c2e-7492-b66b-8a23baac2449
        if len(parts) >= 12:
            thread_id = "-".join(parts[-5:])

        meta = thread_meta.get(thread_id, {})
        cwd = meta.get("cwd", "")
        title = meta.get("title", "")

        with open(rollout_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") != "response_item":
                    continue

                payload = obj.get("payload", {})
                role = payload.get("role", "")
                if role not in ("user", "assistant"):
                    continue

                content = payload.get("content", "")
                text = _extract_codex_text(content)
                if not text.strip() or len(text.strip()) < 3:
                    continue

                ts = obj.get("timestamp", "")
                dedup_key = f"codex-conv|{thread_id}|{ts}|{role}|{hashlib.md5(text.encode()).hexdigest()[:8]}"
                if dedup_key in existing:
                    continue
                existing.add(dedup_key)

                # 提取工具调用
                tools = []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_call":
                            tools.append(block.get("name", ""))

                date_str = get_date_from_timestamp(ts)
                record = {
                    "id": str(uuid.uuid4()),
                    "source": "codex-conversation",
                    "project": cwd,
                    "session_id": thread_id,
                    "timestamp": ts,
                    "type": "conversation",
                    "role": role,
                    "content": text[:10000],
                    "tools_used": tools,
                    "thread_title": title,
                    "_dedup_key": dedup_key,
                }

                if date_str not in daily_buffers:
                    daily_buffers[date_str] = []
                daily_buffers[date_str].append(record)
                stats["records_collected"] += 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 8: Codex 记忆文档 (~/.codex/memories/)
# ============================================================

def collect_codex_memories(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0}
    existing = load_existing_dedup_set("codex-memory")
    daily_buffers = {}

    if not CODEX_MEMORIES.exists():
        return stats

    for doc_file in sorted(CODEX_MEMORIES.rglob("*")):
        if not doc_file.is_file() or doc_file.name.startswith("."):
            continue
        # 跳过 .git 内的文件
        if ".git" in doc_file.parts:
            continue

        stats["files_scanned"] += 1

        if since:
            mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if mtime < since_dt:
                continue

        dedup_key = f"codex-mem|{doc_file.relative_to(CODEX_MEMORIES)}|{file_hash(doc_file)}"
        if dedup_key in existing:
            continue
        existing.add(dedup_key)

        try:
            content = doc_file.read_text(encoding="utf-8", errors="replace")[:50000]
        except OSError:
            continue

        date_str = get_date_from_mtime(doc_file)
        record = {
            "id": str(uuid.uuid4()),
            "source": "codex-memory",
            "project": "",
            "session_id": "",
            "timestamp": datetime.fromtimestamp(
                doc_file.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "type": "memory_doc",
            "role": "system",
            "content": content,
            "file_name": str(doc_file.relative_to(CODEX_MEMORIES)),
            "file_size": doc_file.stat().st_size,
            "_dedup_key": dedup_key,
        }

        if date_str not in daily_buffers:
            daily_buffers[date_str] = []
        daily_buffers[date_str].append(record)
        stats["records_collected"] += 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 9: Codex Skill 资源 (~/.codex/skills/*/)
# ============================================================

def collect_codex_skills(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0, "by_skill": {}}
    existing = load_existing_dedup_set("codex-skill")
    daily_buffers = {}

    if not CODEX_SKILLS.exists():
        return stats

    for skill_dir in sorted(CODEX_SKILLS.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name

        for doc_file in sorted(skill_dir.rglob("*")):
            if not doc_file.is_file() or doc_file.name.startswith("."):
                continue

            stats["files_scanned"] += 1

            if since:
                mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if mtime < since_dt:
                    continue

            if doc_file.stat().st_size > 500000:
                continue

            dedup_key = f"codex-skill|{skill_name}|{doc_file.relative_to(skill_dir)}|{file_hash(doc_file)}"
            if dedup_key in existing:
                continue
            existing.add(dedup_key)

            try:
                content = doc_file.read_text(encoding="utf-8", errors="replace")[:30000]
            except OSError:
                continue

            date_str = get_date_from_mtime(doc_file)
            record = {
                "id": str(uuid.uuid4()),
                "source": "codex-skill",
                "project": "",
                "session_id": "",
                "timestamp": datetime.fromtimestamp(
                    doc_file.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "type": "skill_resource",
                "role": "system",
                "content": content,
                "skill_name": skill_name,
                "file_name": str(doc_file.relative_to(skill_dir)),
                "file_size": doc_file.stat().st_size,
                "_dedup_key": dedup_key,
            }

            if date_str not in daily_buffers:
                daily_buffers[date_str] = []
            daily_buffers[date_str].append(record)
            stats["records_collected"] += 1
            stats["by_skill"][skill_name] = stats["by_skill"].get(skill_name, 0) + 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 10: Codex 产出文件 (~/Documents/Codex/)
# ============================================================

def collect_codex_output(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0, "by_type": {}}
    existing = load_existing_dedup_set("codex-output")
    daily_buffers = {}

    TEXT_EXTENSIONS = {
        ".md", ".txt", ".py", ".js", ".ts", ".html", ".css", ".json",
        ".yaml", ".yml", ".toml", ".sh", ".bash", ".zsh", ".sql",
        ".go", ".rs", ".java", ".c", ".cpp", ".h", ".jsx", ".tsx",
        ".vue", ".xml", ".csv", ".log", ".command",
    }
    SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv", ".next",
        "dist", "build", "build-runtime", ".playwright-cli",
        "_analysis_zip", "_analysis_nested", "analysis",
        ".svelte-kit", ".turbo", ".cache", "out", "coverage",
        ".vercel", ".netlify", "public/build",
    }
    # 构建产物/无记忆价值文件，连索引都不要
    SKIP_EXTENSIONS = {
        ".svg", ".wasm", ".map", ".lock", ".project_a",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".min.js", ".min.css",
    }
    # 二进制文件大小阈值（超过则不复制到 files/）
    BINARY_COPY_LIMIT = 1024 * 1024  # 1MB

    if not CODEX_DOCS_OUTPUT.exists():
        return stats

    for doc_file in sorted(CODEX_DOCS_OUTPUT.rglob("*")):
        if not doc_file.is_file():
            continue
        if any(part in SKIP_DIRS for part in doc_file.parts):
            continue
        if doc_file.name.startswith(".") and doc_file.name not in (".env",):
            continue
        if doc_file.suffix.lower() in SKIP_EXTENSIONS:
            continue
        # 哈希文件名（构建产物特征）
        if re.fullmatch(r'[a-f0-9]{16}', doc_file.stem) and not doc_file.suffix.lower() in TEXT_EXTENSIONS:
            continue

        stats["files_scanned"] += 1

        if since:
            mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if mtime < since_dt:
                continue

        dedup_key = f"codex-out|{doc_file.relative_to(CODEX_DOCS_OUTPUT)}|{file_hash(doc_file)}"
        if dedup_key in existing:
            continue
        existing.add(dedup_key)

        is_text = doc_file.suffix.lower() in TEXT_EXTENSIONS
        content = ""
        is_binary = False

        if is_text and doc_file.stat().st_size < 500000:
            try:
                content = doc_file.read_text(encoding="utf-8", errors="replace")[:30000]
            except OSError:
                content = f"[Cannot read file]"
        else:
            is_binary = True
            ext = doc_file.suffix.lower()
            size = doc_file.stat().st_size
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}:
                content = f"[Image: {doc_file.name}, {size} bytes]"
            elif ext in {".pdf",}:
                content = f"[PDF: {doc_file.name}, {size} bytes]"
            elif ext in {".zip", ".tar", ".gz"}:
                content = f"[Archive: {doc_file.name}, {size} bytes]"
            else:
                content = f"[File: {doc_file.name}, {size} bytes]"
            # 只复制小于 1MB 的二进制文件，避免磁盘膨胀
            if size <= BINARY_COPY_LIMIT:
                copy_to_immortal(doc_file, "codex-output", get_date_from_mtime(doc_file))

        date_str = get_date_from_mtime(doc_file)
        record = {
            "id": str(uuid.uuid4()),
            "source": "codex-output",
            "project": "",
            "session_id": "",
            "timestamp": datetime.fromtimestamp(
                doc_file.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "type": "generated_file",
            "role": "system",
            "content": content,
            "file_name": str(doc_file.relative_to(CODEX_DOCS_OUTPUT)),
            "file_size": doc_file.stat().st_size,
            "is_binary": is_binary,
            "_dedup_key": dedup_key,
        }

        if date_str not in daily_buffers:
            daily_buffers[date_str] = []
        daily_buffers[date_str].append(record)
        stats["records_collected"] += 1
        file_type = doc_file.suffix.lower() or "other"
        stats["by_type"][file_type] = stats["by_type"].get(file_type, 0) + 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 主流程
# ============================================================

def format_all_stats(all_stats: dict) -> str:
    lines = []
    total_collected = 0

    for source_name, stats in all_stats.items():
        collected = stats.get("records_collected", 0)
        scanned = stats.get("files_scanned", 0)
        total_collected += collected

        lines.append(f"\n== {source_name} ==")
        lines.append(f"  扫描: {scanned} 文件, 新增: {collected} 条记录")

        if "sessions_found" in stats:
            lines.append(f"  会话数: {stats['sessions_found']}")
        if "by_skill" in stats:
            for skill, count in sorted(stats["by_skill"].items(), key=lambda x: -x[1])[:10]:
                lines.append(f"    {skill}: {count}条")
        if "by_type" in stats:
            for ft, count in sorted(stats["by_type"].items(), key=lambda x: -x[1])[:8]:
                lines.append(f"    {ft}: {count}个文件")

    lines.insert(0, f"总新增记录: {total_collected}")
    return "\n".join(lines)


# ============================================================
# 采集器 11: autoClaw (OpenClaw) Skills (~/.openclaw/skills/)
# ============================================================

AUTOCLAW_DIR = Path.home() / ".openclaw"
AUTOCLAW_SKILLS = AUTOCLAW_DIR / "skills"
AUTOCLAW_WORKSPACE = AUTOCLAW_DIR / "workspace"


def collect_autoclaw_skills(since: Optional[str] = None) -> dict:
    """autoClaw 本地无对话存储，采集 skills 和配置。"""
    stats = {"files_scanned": 0, "records_collected": 0, "by_skill": {}}
    existing = load_existing_dedup_set("autoclaw-skill")
    daily_buffers = {}

    for search_dir in [AUTOCLAW_SKILLS, AUTOCLAW_WORKSPACE]:
        if not search_dir.exists():
            continue
        for skill_dir in sorted(search_dir.rglob("skills")):
            if not skill_dir.is_dir():
                continue
            parent = skill_dir.parent
            for doc_file in sorted(skill_dir.rglob("*")):
                if not doc_file.is_file() or doc_file.name.startswith("."):
                    continue
                if doc_file.stat().st_size > 500000:
                    continue

                stats["files_scanned"] += 1
                if since:
                    mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
                    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                    if mtime < since_dt:
                        continue

                skill_name = parent.name if parent.name != "skills" else doc_file.parent.name
                dedup_key = f"ac-skill|{skill_name}|{doc_file.relative_to(skill_dir)}|{file_hash(doc_file)}"
                if dedup_key in existing:
                    continue
                existing.add(dedup_key)

                try:
                    content = doc_file.read_text(encoding="utf-8", errors="replace")[:30000]
                except OSError:
                    continue

                date_str = get_date_from_mtime(doc_file)
                record = {
                    "id": str(uuid.uuid4()),
                    "source": "autoclaw-skill",
                    "project": "",
                    "session_id": "",
                    "timestamp": datetime.fromtimestamp(
                        doc_file.stat().st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "type": "skill_resource",
                    "role": "system",
                    "content": content,
                    "skill_name": skill_name,
                    "file_name": str(doc_file.relative_to(skill_dir)),
                    "file_size": doc_file.stat().st_size,
                    "_dedup_key": dedup_key,
                }

                if date_str not in daily_buffers:
                    daily_buffers[date_str] = []
                daily_buffers[date_str].append(record)
                stats["records_collected"] += 1
                stats["by_skill"][skill_name] = stats["by_skill"].get(skill_name, 0) + 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 12: Hermes 对话记录 (~/.hermes/sessions/*.json + state.db)
# ============================================================

HERMES_DIR = Path.home() / ".hermes"
HERMES_SESSIONS = HERMES_DIR / "sessions"
HERMES_STATE_DB = HERMES_DIR / "state.db"
HERMES_MEMORIES = HERMES_DIR / "memories"
HERMES_SKILLS = HERMES_DIR / "skills"


def _hermes_sessions_from_db() -> dict:
    """从 Hermes SQLite 读取会话元数据。"""
    sessions = {}
    if not HERMES_STATE_DB.exists():
        return sessions
    try:
        import sqlite3
        conn = sqlite3.connect(str(HERMES_STATE_DB))
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT id, title, model, source, datetime(started_at, 'unixepoch', '+8 hours') as started, "
            "message_count, input_tokens, output_tokens, estimated_cost_usd FROM sessions"
        ):
            sessions[row["id"]] = {
                "title": row["title"] or "",
                "model": row["model"] or "",
                "source": row["source"] or "",
                "started": row["started"] or "",
                "message_count": row["message_count"] or 0,
                "input_tokens": row["input_tokens"] or 0,
                "output_tokens": row["output_tokens"] or 0,
                "cost": row["estimated_cost_usd"] or 0,
            }
        conn.close()
    except Exception:
        pass
    return sessions


def collect_hermes_conversations(since: Optional[str] = None) -> dict:
    """采集 Hermes 对话记录（优先从 JSON session 文件，SQLite 补充元数据）。"""
    stats = {"files_scanned": 0, "records_collected": 0, "sessions_found": 0}
    existing = load_existing_dedup_set("hermes-conversation")
    daily_buffers = {}
    session_meta = _hermes_sessions_from_db()

    if not HERMES_SESSIONS.exists():
        return stats

    session_files = sorted(HERMES_SESSIONS.glob("session_*.json"))
    stats["sessions_found"] = len(session_files)

    for session_file in session_files:
        stats["files_scanned"] += 1

        if since:
            mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=timezone.utc)
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if mtime < since_dt:
                continue

        try:
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("session_id", session_file.stem)
        model = data.get("model", "")
        session_start = data.get("session_start", "")

        for msg in data.get("messages", []):
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue

            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") in ("text",)
                )
            elif isinstance(content, str):
                text = content
            else:
                text = str(content)

            if not text.strip() or len(text.strip()) < 3:
                continue

            ts = msg.get("timestamp", "")
            if not ts and session_start:
                ts = session_start

            dedup_key = f"hermes|{session_id}|{role}|{hashlib.md5(text.encode()).hexdigest()[:10]}"
            if dedup_key in existing:
                continue
            existing.add(dedup_key)

            date_str = get_date_from_timestamp(ts) if ts else get_date_from_mtime(session_file)
            record = {
                "id": str(uuid.uuid4()),
                "source": "hermes-conversation",
                "project": "",
                "session_id": session_id,
                "timestamp": ts or datetime.fromtimestamp(
                    session_file.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "type": "conversation",
                "role": role,
                "content": text[:10000],
                "tools_used": [],
                "model": model,
                "_dedup_key": dedup_key,
            }

            if date_str not in daily_buffers:
                daily_buffers[date_str] = []
            daily_buffers[date_str].append(record)
            stats["records_collected"] += 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 13: Hermes 记忆文档 (~/.hermes/memories/)
# ============================================================

def collect_hermes_memories(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0}
    existing = load_existing_dedup_set("hermes-memory")
    daily_buffers = {}

    if not HERMES_MEMORIES.exists():
        return stats

    for doc_file in sorted(HERMES_MEMORIES.rglob("*")):
        if not doc_file.is_file() or doc_file.name.startswith(".") or doc_file.suffix == ".lock":
            continue

        stats["files_scanned"] += 1
        if since:
            mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if mtime < since_dt:
                continue

        dedup_key = f"h-mem|{doc_file.name}|{file_hash(doc_file)}"
        if dedup_key in existing:
            continue
        existing.add(dedup_key)

        try:
            content = doc_file.read_text(encoding="utf-8", errors="replace")[:30000]
        except OSError:
            continue

        date_str = get_date_from_mtime(doc_file)
        record = {
            "id": str(uuid.uuid4()),
            "source": "hermes-memory",
            "project": "",
            "session_id": "",
            "timestamp": datetime.fromtimestamp(
                doc_file.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "type": "memory_doc",
            "role": "system",
            "content": content,
            "file_name": doc_file.name,
            "file_size": doc_file.stat().st_size,
            "_dedup_key": dedup_key,
        }

        if date_str not in daily_buffers:
            daily_buffers[date_str] = []
        daily_buffers[date_str].append(record)
        stats["records_collected"] += 1

    write_records(daily_buffers)
    return stats


# ============================================================
# 采集器 14: Hermes Skill 资源 (~/.hermes/skills/*/)
# ============================================================

def collect_hermes_skills(since: Optional[str] = None) -> dict:
    stats = {"files_scanned": 0, "records_collected": 0, "by_skill": {}}
    existing = load_existing_dedup_set("hermes-skill")
    daily_buffers = {}

    if not HERMES_SKILLS.exists():
        return stats

    for skill_dir in sorted(HERMES_SKILLS.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name

        for doc_file in sorted(skill_dir.rglob("*")):
            if not doc_file.is_file() or doc_file.name.startswith("."):
                continue
            if doc_file.stat().st_size > 500000:
                continue

            stats["files_scanned"] += 1
            if since:
                mtime = datetime.fromtimestamp(doc_file.stat().st_mtime, tz=timezone.utc)
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if mtime < since_dt:
                    continue

            dedup_key = f"h-skill|{skill_name}|{doc_file.relative_to(skill_dir)}|{file_hash(doc_file)}"
            if dedup_key in existing:
                continue
            existing.add(dedup_key)

            try:
                content = doc_file.read_text(encoding="utf-8", errors="replace")[:30000]
            except OSError:
                continue

            date_str = get_date_from_mtime(doc_file)
            record = {
                "id": str(uuid.uuid4()),
                "source": "hermes-skill",
                "project": "",
                "session_id": "",
                "timestamp": datetime.fromtimestamp(
                    doc_file.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "type": "skill_resource",
                "role": "system",
                "content": content,
                "skill_name": skill_name,
                "file_name": str(doc_file.relative_to(skill_dir)),
                "file_size": doc_file.stat().st_size,
                "_dedup_key": dedup_key,
            }

            if date_str not in daily_buffers:
                daily_buffers[date_str] = []
            daily_buffers[date_str].append(record)
            stats["records_collected"] += 1
            stats["by_skill"][skill_name] = stats["by_skill"].get(skill_name, 0) + 1

    write_records(daily_buffers)
    return stats


def main():
    since = None
    if len(sys.argv) > 1 and sys.argv[1] == "--since":
        since = sys.argv[2] if len(sys.argv) > 2 else None

    print("=" * 50)
    print("永生记忆库 v0.4 — 全量采集")
    print("=" * 50)
    print()

    all_stats = {}
    step = 0
    total_steps = 14

    def run_step(name, func):
        nonlocal step
        step += 1
        print(f"[{step}/{total_steps}] 采集 {name}...")
        result = func(since=since)
        all_stats[name] = result
        print(f"  +{result.get('records_collected', 0)} 条")

    run_step("Claude对话", collect_conversations)
    run_step("Claude记忆", collect_memory_docs)
    run_step("Claude文件历史", collect_file_history)
    run_step("Claude粘贴缓存", collect_paste_cache)
    run_step("Claude Skill", collect_skills)
    run_step("桌面产出", collect_desktop_output)
    run_step("Codex对话", collect_codex_conversations)
    run_step("Codex记忆", collect_codex_memories)
    run_step("Codex Skill", collect_codex_skills)
    run_step("Codex产出", collect_codex_output)
    run_step("autoClaw Skill", collect_autoclaw_skills)
    run_step("Hermes对话", collect_hermes_conversations)
    run_step("Hermes记忆", collect_hermes_memories)
    run_step("Hermes Skill", collect_hermes_skills)

    # 更新 sources.json
    config = load_sources_config()
    now_iso = datetime.now(timezone.utc).isoformat()
    source_map = {
        "claude-code-conversation": "Claude对话",
        "claude-code-memory": "Claude记忆",
        "claude-code-file-history": "Claude文件历史",
        "claude-code-paste-cache": "Claude粘贴缓存",
        "claude-code-skill": "Claude Skill",
        "desktop-output": "桌面产出",
        "codex-conversation": "Codex对话",
        "codex-memory": "Codex记忆",
        "codex-skill": "Codex Skill",
        "codex-output": "Codex产出",
        "autoclaw-skill": "autoClaw Skill",
        "hermes-conversation": "Hermes对话",
        "hermes-memory": "Hermes记忆",
        "hermes-skill": "Hermes Skill",
    }

    existing_sources = {s["name"]: s for s in config["sources"]}
    for src_name, display_name in source_map.items():
        if src_name in existing_sources:
            existing_sources[src_name]["last_backup"] = now_iso
        else:
            config["sources"].append({
                "name": src_name,
                "type": src_name,
                "path": "",
                "enabled": True,
                "last_backup": now_iso,
                "stats": {"display_name": display_name},
            })
    save_sources_config(config)

    print()
    print(format_all_stats(all_stats))
    print()
    print("全量采集完成!")


if __name__ == "__main__":
    main()
