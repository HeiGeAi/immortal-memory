#!/usr/bin/env python3
"""
Collect Feishu/Lark data into the local Immortal memory vault.

This collector is intentionally read-only. It calls lark-cli with user
identity, writes normalized records into ~/.immortal/daily and
~/.immortal/index.jsonl, and keeps its own SQLite dedup state so repeated
runs are safe.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


IMMORTAL_DIR = Path.home() / ".immortal"
DAILY_DIR = IMMORTAL_DIR / "daily"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
SOURCES_FILE = IMMORTAL_DIR / "sources.json"
FEISHU_DIR = IMMORTAL_DIR / "feishu"
LARK_CLI_CANDIDATES = [
    Path("/opt/homebrew/bin/lark-cli"),
    Path("/usr/local/bin/lark-cli"),
]
LOG_FILE = FEISHU_DIR / "log.jsonl"
STATE_FILE = FEISHU_DIR / "state.json"
DB_FILE = FEISHU_DIR / "state.sqlite3"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
SKILL_VERSION = "0.1.0"


SOURCE_DEFS = {
    "feishu-chat": ("feishu-chat", "Feishu chat metadata"),
    "feishu-im": ("feishu-im", "Feishu chat messages"),
    "feishu-im-search": ("feishu-im-search", "Feishu global message search"),
    "feishu-chat-member": ("feishu-chat-member", "Feishu chat members"),
    "feishu-task": ("feishu-task", "Feishu tasks"),
    "feishu-calendar-list": ("feishu-calendar-list", "Feishu calendar containers"),
    "feishu-calendar-event": ("feishu-calendar-event", "Feishu calendar events"),
    "feishu-contact": ("feishu-contact", "Feishu contacts"),
    "feishu-vc": ("feishu-vc", "Feishu meeting records"),
    "feishu-vc-note": ("feishu-vc-note", "Feishu meeting note metadata"),
    "feishu-vc-note-content": ("feishu-vc-note-content", "Feishu meeting note document content"),
    "feishu-vc-recording": ("feishu-vc-recording", "Feishu meeting recording metadata"),
    "feishu-minutes": ("feishu-minutes", "Feishu Minutes metadata"),
    "feishu-minutes-note": ("feishu-minutes-note", "Feishu Minutes summary, chapters, todos, transcript"),
    "feishu-doc": ("feishu-doc", "Feishu docs search metadata"),
    "feishu-doc-content": ("feishu-doc-content", "Feishu document text content"),
    "feishu-mail": ("feishu-mail", "Feishu mail metadata"),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def ensure_dirs() -> None:
    IMMORTAL_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    FEISHU_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def log_event(level: str, event: str, **fields: Any) -> None:
    ensure_dirs()
    payload = {
        "timestamp": iso_now(),
        "level": level,
        "event": event,
        **fields,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        create table if not exists seen (
            record_key text primary key,
            source text not null,
            first_seen_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists runs (
            run_id text primary key,
            started_at text not null,
            finished_at text,
            args_json text,
            stats_json text,
            errors_json text
        )
        """
    )
    return conn


def mark_seen(conn: sqlite3.Connection, source: str, record_key: str) -> bool:
    try:
        conn.execute(
            "insert into seen(record_key, source, first_seen_at) values (?, ?, ?)",
            (record_key, source, iso_now()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def parse_dt(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        d = date.fromisoformat(text)
        t = dt_time.max if end_of_day else dt_time.min
        return datetime.combine(d, t, tzinfo=LOCAL_TZ)
    text = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def window_from_args(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end = parse_dt(args.until, end_of_day=True) or datetime.now(LOCAL_TZ)
    if args.all:
        start = datetime(2020, 1, 1, tzinfo=LOCAL_TZ)
    else:
        start = parse_dt(args.since) or (end - timedelta(days=args.days))
    if start >= end:
        raise ValueError("--since must be earlier than --until")
    return start, end


def iso_local(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).isoformat(timespec="seconds")


def to_unix_seconds(dt: datetime) -> str:
    return str(int(dt.timestamp()))


def normalize_timestamp(value: Any) -> str:
    if value is None:
        return iso_now()
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value = value / 1000
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return iso_now()
    if text.isdigit():
        return normalize_timestamp(int(text))
    if len(text) == 16 and text[4] == "-" and text[7] == "-" and text[10] == " ":
        return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ).isoformat()
    if len(text) == 19 and text[4] == "-" and text[7] == "-" and text[10] == " ":
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ).isoformat()
    try:
        return parse_dt(text).isoformat()  # type: ignore[union-attr]
    except Exception:
        return iso_now()


def date_for_record(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def compact_json(value: Any, limit: int = 4000) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        return text[: limit - 1] + "..."
    return text


def stable_hash(value: Any, length: int = 16) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:length]


def display_text(value: Any) -> str:
    text = str(value or "")
    return html.unescape(text).replace("<b>", "").replace("</b>", "").strip()


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def find_first_key(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if item is not None and str(item).strip():
                return str(item).strip()
        for item in value.values():
            found = find_first_key(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_first_key(item, keys)
            if found:
                return found
    return ""


def ensure_sources_config() -> None:
    config = read_json(SOURCES_FILE, {"sources": []})
    sources = config.setdefault("sources", [])
    by_name = {item.get("name"): item for item in sources if isinstance(item, dict)}
    now = iso_now()
    changed = False
    for name, (source_type, display_name) in SOURCE_DEFS.items():
        if name in by_name:
            continue
        sources.append(
            {
                "name": name,
                "type": source_type,
                "path": "lark-cli --as user",
                "enabled": True,
                "last_backup": now,
                "stats": {"display_name": display_name},
            }
        )
        changed = True
    if changed:
        write_json(SOURCES_FILE, config)


def update_sources_backup(stats: dict[str, Any]) -> None:
    config = read_json(SOURCES_FILE, {"sources": []})
    now = iso_now()
    for item in config.get("sources", []):
        if item.get("name") in SOURCE_DEFS:
            item["last_backup"] = now
            source = item.get("type")
            item.setdefault("stats", {})
            item["stats"]["last_new_records"] = stats.get(source, 0)
    write_json(SOURCES_FILE, config)


def run_lark(args: list[str], *, timeout: int = 60, cwd: Path | None = None) -> tuple[bool, dict[str, Any], str]:
    executable = next((str(path) for path in LARK_CLI_CANDIDATES if path.exists()), "lark-cli")
    cmd = [executable, *args]
    env_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1", "PATH": f"{env_path}:{os.environ.get('PATH', '')}"},
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        return False, {}, f"timeout after {timeout}s: {' '.join(cmd)}"
    except Exception as exc:
        return False, {}, f"{type(exc).__name__}: {exc}"

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    body: dict[str, Any] = {}
    if stdout:
        try:
            body = json.loads(stdout)
        except json.JSONDecodeError:
            body = {"raw_stdout": stdout}

    ok = proc.returncode == 0
    if body.get("ok") is False:
        ok = False
    if body.get("code") not in (None, 0):
        ok = False
    err = stderr
    if not ok and body:
        err = compact_json(body, 2000)
    return ok, body, err


def current_auth_status() -> tuple[bool, dict[str, Any], str]:
    return run_lark(["auth", "status", "--verify"], timeout=30)


def data_part(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data", body)
    return data if isinstance(data, dict) else {"items": data}


def doc_search_meta(item: dict[str, Any]) -> dict[str, Any]:
    meta = item.get("result_meta")
    return meta if isinstance(meta, dict) else {}


def doc_search_token(item: dict[str, Any]) -> str:
    meta = doc_search_meta(item)
    return str(
        meta.get("token")
        or item.get("token")
        or item.get("file_token")
        or item.get("obj_token")
        or meta.get("url")
        or item.get("url")
        or compact_json(item, 200)
    )


def doc_search_url(item: dict[str, Any]) -> str:
    meta = doc_search_meta(item)
    return str(meta.get("url") or item.get("url") or "")


def doc_search_title(item: dict[str, Any]) -> str:
    meta = doc_search_meta(item)
    return str(
        item.get("title_highlighted")
        or item.get("title")
        or item.get("name")
        or meta.get("title")
        or meta.get("doc_title")
        or ""
    )


def doc_search_type(item: dict[str, Any]) -> str:
    meta = doc_search_meta(item)
    return str(
        meta.get("doc_types")
        or item.get("docs_type")
        or item.get("doc_type")
        or item.get("entity_type")
        or item.get("type")
        or ""
    )


def new_record(
    *,
    source: str,
    record_type: str,
    timestamp: str,
    content: str,
    role: str = "system",
    metadata: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "source": source,
        "project": "feishu",
        "session_id": session_id,
        "timestamp": timestamp,
        "type": record_type,
        "role": role,
        "content": content,
        "metadata": metadata or {},
    }


def write_records(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    ensure_dirs()
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        buckets[date_for_record(record.get("timestamp", ""))].append(record)

    for day, items in sorted(buckets.items()):
        daily_file = DAILY_DIR / f"{day}.jsonl"
        with open(daily_file, "a", encoding="utf-8") as daily, open(INDEX_FILE, "a", encoding="utf-8") as index:
            for item in items:
                line = json.dumps(item, ensure_ascii=False)
                daily.write(line + "\n")
                index.write(line + "\n")


class Collector:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.conn = db()
        self.run_id = str(uuid.uuid4())
        self.stats: dict[str, int] = defaultdict(int)
        self.errors: list[dict[str, str]] = []
        self.start, self.end = window_from_args(args)
        self.chats: list[dict[str, Any]] = []
        self.self_user: dict[str, Any] | None = None

    def error(self, source: str, message: str) -> None:
        self.errors.append({"source": source, "message": message[:1000]})
        log_event("error", "collector_error", source=source, message=message[:1000])

    def add_records(self, records: list[dict[str, Any]]) -> None:
        write_records(records)
        for record in records:
            self.stats[record.get("source", "unknown")] += 1

    def start_run(self) -> None:
        ok, body, err = current_auth_status()
        if not ok:
            raise RuntimeError(f"cannot verify lark-cli auth status: {err}")
        user_name = str(body.get("userName") or "")
        user_open_id = str(body.get("userOpenId") or "")
        expected_name = self.args.expected_user_name
        expected_open_id = self.args.expected_user_open_id
        if not expected_name and not expected_open_id and not self.args.allow_current_account:
            raise RuntimeError(
                f"lark-cli is authenticated as {user_name} ({user_open_id}), but no expected account was provided. "
                "Pass --expected-user-name or --expected-user-open-id to prevent wrong-account collection."
            )
        if expected_name and expected_name not in user_name:
            raise RuntimeError(
                f"lark-cli is authenticated as {user_name}, expected name containing {expected_name}. "
                "Refusing to collect from the wrong Feishu account."
            )
        if expected_open_id and expected_open_id != user_open_id:
            raise RuntimeError(
                f"lark-cli is authenticated as {user_open_id}, expected {expected_open_id}. "
                "Refusing to collect from the wrong Feishu account."
            )
        if self.args.reject_user_name and self.args.reject_user_name in user_name:
            raise RuntimeError(
                f"lark-cli is authenticated as rejected account {user_name}. "
                "Refusing to collect from the wrong Feishu account."
            )
        ensure_sources_config()
        self.conn.execute(
            "insert into runs(run_id, started_at, args_json) values (?, ?, ?)",
            (self.run_id, iso_now(), json.dumps(vars(self.args), ensure_ascii=False)),
        )
        self.conn.commit()
        log_event(
            "info",
            "run_started",
            run_id=self.run_id,
            start=iso_local(self.start),
            end=iso_local(self.end),
            sources=self.args.sources,
        )

    def finish_run(self) -> None:
        stats = dict(self.stats)
        self.conn.execute(
            "update runs set finished_at = ?, stats_json = ?, errors_json = ? where run_id = ?",
            (
                iso_now(),
                json.dumps(stats, ensure_ascii=False),
                json.dumps(self.errors, ensure_ascii=False),
                self.run_id,
            ),
        )
        self.conn.commit()
        state = read_json(STATE_FILE, {})
        state.update(
            {
                "version": SKILL_VERSION,
                "last_run_id": self.run_id,
                "last_run_at": iso_now(),
                "last_window_start": iso_local(self.start),
                "last_window_end": iso_local(self.end),
                "last_stats": stats,
                "last_errors": self.errors[-20:],
            }
        )
        write_json(STATE_FILE, state)
        update_sources_backup(stats)
        log_event("info", "run_finished", run_id=self.run_id, stats=stats, errors=len(self.errors))

    def collect_contact_self(self) -> None:
        ok, body, err = run_lark(["contact", "+get-user", "--as", "user", "--format", "json"])
        if not ok:
            self.error("feishu-contact", err)
            return
        user = data_part(body).get("user") or data_part(body).get("data", {}).get("user")
        if not isinstance(user, dict):
            self.error("feishu-contact", "unexpected contact +get-user response")
            return
        self.self_user = user
        key = f"feishu-contact|self|{user.get('open_id') or user.get('union_id') or user.get('name')}"
        if not mark_seen(self.conn, "feishu-contact", key):
            return
        content = "\n".join(
            [
                f"Feishu self contact: {user.get('name') or ''}",
                f"open_id: {user.get('open_id') or ''}",
                f"union_id: {user.get('union_id') or ''}",
                f"tenant_key: {user.get('tenant_key') or ''}",
            ]
        )
        self.add_records(
            [
                new_record(
                    source="feishu-contact",
                    record_type="contact",
                    timestamp=iso_now(),
                    content=content,
                    metadata={"user": user, "kind": "self"},
                )
            ]
        )

    def collect_chats(self) -> None:
        page_token = ""
        page = 0
        records: list[dict[str, Any]] = []
        while True:
            page += 1
            params: dict[str, Any] = {"page_size": self.args.chat_page_size, "sort_type": "ByCreateTimeAsc"}
            if page_token:
                params["page_token"] = page_token
            ok, body, err = run_lark(
                ["im", "chats", "list", "--as", "user", "--params", json.dumps(params), "--format", "json"],
                timeout=60,
            )
            if not ok:
                self.error("feishu-chat", err)
                break
            data = data_part(body)
            items = data.get("items") or []
            if not isinstance(items, list):
                self.error("feishu-chat", "unexpected chats list response")
                break
            for chat in items:
                if not isinstance(chat, dict):
                    continue
                self.chats.append(chat)
                chat_id = chat.get("chat_id", "")
                key = f"feishu-chat|{chat_id}"
                if mark_seen(self.conn, "feishu-chat", key):
                    content = "\n".join(
                        [
                            f"Feishu chat: {chat.get('name') or ''}",
                            f"chat_id: {chat_id}",
                            f"status: {chat.get('chat_status') or ''}",
                            f"external: {chat.get('external')}",
                            f"description: {chat.get('description') or ''}",
                        ]
                    )
                    records.append(
                        new_record(
                            source="feishu-chat",
                            record_type="chat",
                            timestamp=iso_now(),
                            content=content,
                            metadata={"chat": chat},
                            session_id=chat_id,
                        )
                    )
                if self.args.max_chats and len(self.chats) >= self.args.max_chats:
                    self.add_records(records)
                    self.conn.commit()
                    return
            page_token = data.get("page_token") or ""
            has_more = bool(data.get("has_more")) or bool(page_token)
            if not has_more:
                break
            if self.args.chat_page_limit and page >= self.args.chat_page_limit:
                break
            time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()

    def collect_chat_members(self) -> None:
        records: list[dict[str, Any]] = []
        for chat in self.chats:
            chat_id = chat.get("chat_id")
            if not chat_id:
                continue
            page_token = ""
            page = 0
            while True:
                page += 1
                params: dict[str, Any] = {
                    "chat_id": chat_id,
                    "page_size": 100,
                    "member_id_type": "open_id",
                }
                if page_token:
                    params["page_token"] = page_token
                ok, body, err = run_lark(
                    ["im", "chat.members", "get", "--as", "user", "--params", json.dumps(params), "--format", "json"],
                    timeout=60,
                )
                if not ok:
                    self.error("feishu-chat-member", f"{chat.get('name')}: {err}")
                    break
                data = data_part(body)
                items = data.get("items") or []
                for member in items if isinstance(items, list) else []:
                    if not isinstance(member, dict):
                        continue
                    member_id = member.get("member_id", "")
                    key = f"feishu-chat-member|{chat_id}|{member_id}"
                    if not mark_seen(self.conn, "feishu-chat-member", key):
                        continue
                    content = "\n".join(
                        [
                            f"Feishu chat member: {member.get('name') or ''}",
                            f"chat: {chat.get('name') or ''}",
                            f"member_id: {member_id}",
                        ]
                    )
                    records.append(
                        new_record(
                            source="feishu-chat-member",
                            record_type="contact",
                            timestamp=iso_now(),
                            content=content,
                            metadata={"chat": chat, "member": member},
                            session_id=chat_id,
                        )
                    )
                    if self.args.max_members and len(records) >= self.args.max_members:
                        self.add_records(records)
                        self.conn.commit()
                        return
                page_token = data.get("page_token") or ""
                has_more = bool(data.get("has_more")) or bool(page_token)
                if not has_more:
                    break
                if self.args.member_page_limit and page >= self.args.member_page_limit:
                    break
                time.sleep(self.args.page_delay)
            if len(records) >= self.args.flush_size:
                self.add_records(records)
                records = []
                self.conn.commit()
        self.add_records(records)
        self.conn.commit()

    def collect_messages(self) -> None:
        if not self.chats:
            self.collect_chats()
        total = 0
        for chat in self.chats:
            chat_id = chat.get("chat_id")
            chat_name = chat.get("name") or chat_id
            if not chat_id:
                continue
            page_token = ""
            page = 0
            records: list[dict[str, Any]] = []
            while True:
                page += 1
                cmd = [
                    "im",
                    "+chat-messages-list",
                    "--chat-id",
                    chat_id,
                    "--sort",
                    "asc",
                    "--page-size",
                    str(self.args.message_page_size),
                    "--start",
                    iso_local(self.start),
                    "--end",
                    iso_local(self.end),
                    "--format",
                    "json",
                    "--as",
                    "user",
                ]
                if page_token:
                    cmd.extend(["--page-token", page_token])
                ok, body, err = run_lark(cmd, timeout=90)
                if not ok:
                    self.error("feishu-im", f"{chat_name}: {err}")
                    break
                data = data_part(body)
                messages = data.get("messages") or data.get("items") or []
                if not isinstance(messages, list):
                    self.error("feishu-im", f"{chat_name}: unexpected messages response")
                    break
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    message_id = message.get("message_id") or message.get("id")
                    if not message_id:
                        continue
                    key = f"feishu-im|{message_id}"
                    if not mark_seen(self.conn, "feishu-im", key):
                        continue
                    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
                    sender_name = sender.get("name") or sender.get("id") or ""
                    msg_type = message.get("msg_type") or message.get("message_type") or "unknown"
                    text = str(message.get("content") or "").strip()
                    if not text:
                        text = f"[{msg_type} message]"
                    content = "\n".join(
                        [
                            f"Feishu chat: {chat_name}",
                            f"Sender: {sender_name}",
                            f"Message type: {msg_type}",
                            text,
                        ]
                    )
                    timestamp = normalize_timestamp(message.get("create_time") or message.get("create_time_ms"))
                    records.append(
                        new_record(
                            source="feishu-im",
                            record_type="conversation",
                            role="user" if sender.get("sender_type") == "user" else "system",
                            timestamp=timestamp,
                            content=content,
                            metadata={"chat": chat, "message": message},
                            session_id=chat_id,
                        )
                    )
                    total += 1
                    if self.args.max_messages and total >= self.args.max_messages:
                        self.add_records(records)
                        self.conn.commit()
                        return
                if len(records) >= self.args.flush_size:
                    self.add_records(records)
                    records = []
                    self.conn.commit()
                page_token = data.get("page_token") or ""
                has_more = bool(data.get("has_more")) or bool(page_token)
                if not has_more:
                    break
                if self.args.message_page_limit and page >= self.args.message_page_limit:
                    break
                time.sleep(self.args.page_delay)
            self.add_records(records)
            self.conn.commit()

    def collect_message_search(self) -> None:
        page_token = ""
        page = 0
        total = 0
        records: list[dict[str, Any]] = []
        while True:
            page += 1
            cmd = [
                "im",
                "+messages-search",
                "--as",
                "user",
                "--start",
                iso_local(self.start),
                "--end",
                iso_local(self.end),
                "--page-size",
                str(self.args.message_page_size),
                "--format",
                "json",
            ]
            if self.args.search_chat_type:
                cmd.extend(["--chat-type", self.args.search_chat_type])
            if self.args.search_query:
                cmd.extend(["--query", self.args.search_query])
            if page_token:
                cmd.extend(["--page-token", page_token])
            ok, body, err = run_lark(cmd, timeout=90)
            if not ok:
                self.error("feishu-im-search", err)
                break
            data = data_part(body)
            messages = data.get("messages") or data.get("items") or data.get("results") or []
            if not isinstance(messages, list):
                self.error("feishu-im-search", "unexpected messages-search response")
                break
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("message_id") or message.get("id")
                if not message_id:
                    continue
                key = f"feishu-im|{message_id}"
                if not mark_seen(self.conn, "feishu-im-search", key):
                    continue
                sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
                chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
                sender_name = sender.get("name") or sender.get("id") or message.get("sender_name") or ""
                chat_name = chat.get("name") or message.get("chat_name") or message.get("chat_id") or ""
                msg_type = message.get("msg_type") or message.get("message_type") or "unknown"
                text = str(message.get("content") or message.get("text") or "").strip()
                if not text:
                    text = f"[{msg_type} message]"
                content = "\n".join(
                    [
                        f"Feishu global message search result",
                        f"Chat: {chat_name}",
                        f"Sender: {sender_name}",
                        f"Message type: {msg_type}",
                        text,
                    ]
                )
                timestamp = normalize_timestamp(message.get("create_time") or message.get("create_time_ms"))
                records.append(
                    new_record(
                        source="feishu-im-search",
                        record_type="conversation",
                        role="user" if sender.get("sender_type") == "user" else "system",
                        timestamp=timestamp,
                        content=content,
                        metadata={"message": message, "search_chat_type": self.args.search_chat_type},
                        session_id=str(message.get("chat_id") or chat.get("chat_id") or ""),
                    )
                )
                total += 1
                if self.args.max_messages and total >= self.args.max_messages:
                    self.add_records(records)
                    self.conn.commit()
                    return
            if len(records) >= self.args.flush_size:
                self.add_records(records)
                records = []
                self.conn.commit()
            page_token = data.get("page_token") or ""
            has_more = bool(data.get("has_more")) and bool(page_token)
            if not has_more:
                break
            if self.args.message_page_limit and page >= self.args.message_page_limit:
                break
            time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()

    def collect_tasks(self) -> None:
        records: list[dict[str, Any]] = []
        for complete in (False, True):
            cmd = ["task", "+get-my-tasks", "--as", "user", "--format", "json", "--page-all", "--page-limit", str(self.args.task_page_limit)]
            if complete:
                cmd.append("--complete")
            if not self.args.all:
                cmd.extend(["--created_at", iso_local(self.start)])
            ok, body, err = run_lark(cmd, timeout=120)
            if not ok:
                self.error("feishu-task", err)
                continue
            data = data_part(body)
            items = data.get("items") or []
            if not isinstance(items, list):
                self.error("feishu-task", "unexpected task response")
                continue
            for task in items:
                if not isinstance(task, dict):
                    continue
                guid = task.get("guid") or task.get("task_guid") or task.get("id")
                if not guid:
                    continue
                key = f"feishu-task|{guid}|complete={complete}"
                if not mark_seen(self.conn, "feishu-task", key):
                    continue
                summary = task.get("summary") or task.get("title") or ""
                timestamp = normalize_timestamp(task.get("created_at") or task.get("updated_at"))
                content = "\n".join(
                    [
                        f"Feishu task: {summary}",
                        f"complete: {complete}",
                        f"due_at: {task.get('due_at') or ''}",
                        f"url: {task.get('url') or ''}",
                    ]
                )
                records.append(
                    new_record(
                        source="feishu-task",
                        record_type="task",
                        timestamp=timestamp,
                        content=content,
                        metadata={"task": task, "complete": complete},
                    )
                )
        self.add_records(records)
        self.conn.commit()

    def collect_calendar(self) -> None:
        ok, body, err = run_lark(
            ["calendar", "calendars", "list", "--as", "user", "--params", json.dumps({"page_size": 50}), "--format", "json"],
            timeout=60,
        )
        if not ok:
            self.error("feishu-calendar-list", err)
            return
        data = data_part(body)
        calendars = data.get("calendar_list") or data.get("items") or []
        if not isinstance(calendars, list):
            self.error("feishu-calendar-list", "unexpected calendar list response")
            return
        records: list[dict[str, Any]] = []
        for cal in calendars:
            if not isinstance(cal, dict):
                continue
            calendar_id = cal.get("calendar_id")
            key = f"feishu-calendar-list|calendar|{calendar_id}"
            if calendar_id and mark_seen(self.conn, "feishu-calendar-list", key):
                records.append(
                    new_record(
                        source="feishu-calendar-list",
                        record_type="calendar",
                        timestamp=iso_now(),
                        content=f"Feishu calendar: {cal.get('summary') or ''}\nrole: {cal.get('role') or ''}\ntype: {cal.get('type') or ''}",
                        metadata={"calendar": cal},
                        session_id=calendar_id,
                    )
                )
            if not calendar_id:
                continue
            for chunk_start, chunk_end in self.calendar_chunks():
                params = {
                    "calendar_id": calendar_id,
                    "start_time": to_unix_seconds(chunk_start),
                    "end_time": to_unix_seconds(chunk_end),
                }
                ok2, body2, err2 = run_lark(
                    ["calendar", "events", "instance_view", "--as", "user", "--params", json.dumps(params), "--format", "json"],
                    timeout=60,
                )
                if not ok2:
                    self.error("feishu-calendar-event", f"{cal.get('summary')}: {err2}")
                    continue
                event_data = data_part(body2)
                events = event_data.get("items") or []
                if not isinstance(events, list):
                    continue
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    event_id = event.get("event_id")
                    if not event_id:
                        continue
                    key = f"feishu-calendar-event|event|{calendar_id}|{event_id}"
                    if not mark_seen(self.conn, "feishu-calendar-event", key):
                        continue
                    timestamp = self.event_start_iso(event)
                    content = "\n".join(
                        [
                            f"Feishu calendar event: {event.get('summary') or ''}",
                            f"calendar: {cal.get('summary') or ''}",
                            f"start: {self.time_info_text(event.get('start_time'))}",
                            f"end: {self.time_info_text(event.get('end_time'))}",
                            f"organizer: {(event.get('event_organizer') or {}).get('display_name') or ''}",
                            f"location: {(event.get('location') or {}).get('name') or ''}",
                            f"description: {event.get('description') or ''}",
                        ]
                    )
                    records.append(
                        new_record(
                            source="feishu-calendar-event",
                            record_type="calendar_event",
                            timestamp=timestamp,
                            content=content,
                            metadata={"calendar": cal, "event": event},
                            session_id=calendar_id,
                        )
                    )
                if len(records) >= self.args.flush_size:
                    self.add_records(records)
                    records = []
                    self.conn.commit()
        self.add_records(records)
        self.conn.commit()

    def calendar_chunks(self) -> list[tuple[datetime, datetime]]:
        chunks = []
        cur = self.start
        while cur < self.end:
            nxt = min(cur + timedelta(days=39), self.end)
            chunks.append((cur, nxt))
            cur = nxt
        return chunks

    @staticmethod
    def time_info_text(info: Any) -> str:
        if not isinstance(info, dict):
            return ""
        if info.get("date"):
            return str(info.get("date"))
        if info.get("timestamp"):
            return normalize_timestamp(info.get("timestamp"))
        return compact_json(info, 500)

    def event_start_iso(self, event: dict[str, Any]) -> str:
        info = event.get("start_time")
        if isinstance(info, dict):
            if info.get("timestamp"):
                return normalize_timestamp(info.get("timestamp"))
            if info.get("date"):
                return parse_dt(str(info["date"])).isoformat()  # type: ignore[union-attr]
        return iso_now()

    def api_window_chunks(self, max_days: int = 29) -> list[tuple[datetime, datetime]]:
        chunks = []
        cur = self.start
        while cur < self.end:
            nxt = min(cur + timedelta(days=max_days), self.end)
            chunks.append((cur, nxt))
            cur = nxt
        return chunks

    def relative_minutes_output_dir(self) -> str:
        raw = str(self.args.minutes_output_dir or "minutes_artifacts").strip() or "minutes_artifacts"
        path = Path(raw)
        if path.is_absolute():
            try:
                raw = str(path.relative_to(FEISHU_DIR))
            except ValueError:
                raw = "minutes_artifacts"
        parts = [part for part in Path(raw).parts if part not in {"", ".", ".."}]
        return str(Path(*parts)) if parts else "minutes_artifacts"

    def collect_vc(self) -> None:
        meeting_ids: list[str] = []
        seen_meeting_ids: set[str] = set()
        records: list[dict[str, Any]] = []
        for chunk_start, chunk_end in self.api_window_chunks():
            page_token = ""
            page = 0
            while True:
                page += 1
                cmd = [
                    "vc",
                    "+search",
                    "--as",
                    "user",
                    "--start",
                    iso_local(chunk_start),
                    "--end",
                    iso_local(chunk_end),
                    "--page-size",
                    "30",
                    "--format",
                    "json",
                ]
                if page_token:
                    cmd.extend(["--page-token", page_token])
                ok, body, err = run_lark(cmd, timeout=90)
                if not ok:
                    self.error("feishu-vc", err)
                    break
                data = data_part(body)
                items = data.get("items") or []
                if not isinstance(items, list):
                    self.error("feishu-vc", "unexpected vc search response")
                    break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    meeting_id = str(item.get("id") or item.get("meeting_id") or "")
                    if not meeting_id:
                        continue
                    if meeting_id not in seen_meeting_ids:
                        seen_meeting_ids.add(meeting_id)
                        meeting_ids.append(meeting_id)
                    key = f"feishu-vc|meeting|{meeting_id}"
                    if not mark_seen(self.conn, "feishu-vc", key):
                        continue
                    display = display_text(item.get("display_info") or meeting_id)
                    content = "\n".join(
                        [
                            f"Feishu meeting record: {display}",
                            f"meeting_id: {meeting_id}",
                            f"app_link: {(item.get('meta_data') or {}).get('app_link') or ''}",
                        ]
                    )
                    records.append(
                        new_record(
                            source="feishu-vc",
                            record_type="meeting",
                            timestamp=iso_now(),
                            content=content,
                            metadata={"meeting": item},
                            session_id=meeting_id,
                        )
                    )
                page_token = data.get("page_token") or ""
                has_more = bool(data.get("has_more")) and bool(page_token)
                if not has_more:
                    break
                if self.args.vc_page_limit and page >= self.args.vc_page_limit:
                    break
                time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()
        scoped_meeting_ids = self.limit_items(meeting_ids, self.args.meeting_artifact_limit)
        self.collect_vc_notes(scoped_meeting_ids)
        minute_tokens = self.collect_vc_recordings(scoped_meeting_ids)
        if minute_tokens:
            self.collect_minutes_notes(minute_tokens, origin="vc-recording")

    @staticmethod
    def limit_items(items: list[str], limit: int) -> list[str]:
        if limit and len(items) > limit:
            return items[:limit]
        return items

    def collect_vc_notes(self, meeting_ids: list[str]) -> None:
        if not meeting_ids:
            return
        records: list[dict[str, Any]] = []
        doc_tokens: list[str] = []
        for chunk in chunked(meeting_ids, 50):
            ok, body, err = run_lark(
                [
                    "vc",
                    "+notes",
                    "--as",
                    "user",
                    "--meeting-ids",
                    ",".join(chunk),
                    "--output-dir",
                    self.relative_minutes_output_dir(),
                    "--format",
                    "json",
                ],
                timeout=180,
                cwd=FEISHU_DIR,
            )
            data = data_part(body)
            notes = data.get("notes") or []
            if not ok and not notes:
                self.error("feishu-vc-note", err)
                continue
            if not isinstance(notes, list):
                self.error("feishu-vc-note", "unexpected vc notes response")
                continue
            for note in notes:
                if not isinstance(note, dict):
                    continue
                meeting_id = str(note.get("meeting_id") or find_first_key(note, {"meeting_id"}) or "")
                if note.get("error"):
                    self.error("feishu-vc-note", f"{meeting_id or 'unknown'}: {note.get('error')}")
                    continue
                key_id = meeting_id or stable_hash(note)
                key = f"feishu-vc-note|meeting|{key_id}|{stable_hash(note)}"
                if mark_seen(self.conn, "feishu-vc-note", key):
                    note_doc_token = str(note.get("note_doc_token") or "")
                    verbatim_doc_token = str(note.get("verbatim_doc_token") or "")
                    content = "\n".join(
                        [
                            "Feishu meeting note metadata",
                            f"meeting_id: {meeting_id}",
                            f"create_time: {note.get('create_time') or ''}",
                            f"creator_id: {note.get('creator_id') or ''}",
                            f"note_doc_token: {note_doc_token}",
                            f"verbatim_doc_token: {verbatim_doc_token}",
                        ]
                    )
                    records.append(
                        new_record(
                            source="feishu-vc-note",
                            record_type="meeting_note",
                            timestamp=normalize_timestamp(note.get("create_time")),
                            content=content,
                            metadata={"note": note},
                            session_id=meeting_id,
                        )
                    )
                for token_key in ("note_doc_token", "verbatim_doc_token"):
                    token = str(note.get(token_key) or "").strip()
                    if token:
                        doc_tokens.append(token)
                shared = note.get("shared_doc_tokens")
                if isinstance(shared, list):
                    doc_tokens.extend(str(token).strip() for token in shared if str(token).strip())
            if len(records) >= self.args.flush_size:
                self.add_records(records)
                records = []
                self.conn.commit()
            time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()
        self.collect_vc_note_doc_contents(sorted(set(doc_tokens)))

    def collect_vc_note_doc_contents(self, doc_tokens: list[str]) -> None:
        doc_tokens = self.limit_items(doc_tokens, self.args.meeting_note_doc_content_limit)
        if not doc_tokens:
            return
        records: list[dict[str, Any]] = []
        for token in doc_tokens:
            ok, body, err = run_lark(["docs", "+fetch", "--as", "user", "--doc", token, "--format", "json"], timeout=120)
            if not ok:
                self.error("feishu-vc-note-content", f"{token}: {err}")
                continue
            content_text = self.extract_doc_fetch_text(body)
            if not content_text:
                content_text = compact_json(data_part(body), self.args.meeting_note_doc_chars)
            content_text = content_text[: self.args.meeting_note_doc_chars]
            key = f"feishu-vc-note-content|{token}|{stable_hash(content_text)}"
            if not mark_seen(self.conn, "feishu-vc-note-content", key):
                continue
            records.append(
                new_record(
                    source="feishu-vc-note-content",
                    record_type="meeting_note_content",
                    timestamp=iso_now(),
                    content=f"Feishu meeting note document content: {token}\n\n{content_text}",
                    metadata={"doc_token": token, "fetch": body},
                    session_id=token,
                )
            )
            if len(records) >= self.args.flush_size:
                self.add_records(records)
                records = []
                self.conn.commit()
            time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()

    def collect_vc_recordings(self, meeting_ids: list[str]) -> list[str]:
        minute_tokens: list[str] = []
        if not meeting_ids:
            return minute_tokens
        records: list[dict[str, Any]] = []
        for chunk in chunked(meeting_ids, 50):
            ok, body, err = run_lark(
                ["vc", "+recording", "--as", "user", "--meeting-ids", ",".join(chunk), "--format", "json"],
                timeout=180,
            )
            data = data_part(body)
            recordings = data.get("recordings") or []
            if not ok and not recordings:
                self.error("feishu-vc-recording", err)
                continue
            if not isinstance(recordings, list):
                self.error("feishu-vc-recording", "unexpected vc recording response")
                continue
            for recording in recordings:
                if not isinstance(recording, dict):
                    continue
                meeting_id = str(recording.get("meeting_id") or "")
                if recording.get("error"):
                    self.error("feishu-vc-recording", f"{meeting_id}: {recording.get('error')}")
                    continue
                minute_token = str(recording.get("minute_token") or find_first_key(recording, {"minute_token"}) or "")
                if minute_token:
                    minute_tokens.append(minute_token)
                key = f"feishu-vc-recording|{meeting_id}|{minute_token or stable_hash(recording)}"
                if not mark_seen(self.conn, "feishu-vc-recording", key):
                    continue
                content = "\n".join(
                    [
                        "Feishu meeting recording metadata",
                        f"meeting_id: {meeting_id}",
                        f"minute_token: {minute_token}",
                        compact_json(recording, 2000),
                    ]
                )
                records.append(
                    new_record(
                        source="feishu-vc-recording",
                        record_type="meeting_recording",
                        timestamp=iso_now(),
                        content=content,
                        metadata={"recording": recording},
                        session_id=meeting_id,
                    )
                )
            if len(records) >= self.args.flush_size:
                self.add_records(records)
                records = []
                self.conn.commit()
            time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()
        return sorted(set(minute_tokens))

    def collect_minutes(self) -> None:
        records: list[dict[str, Any]] = []
        tokens: list[str] = []
        seen_tokens: set[str] = set()
        for chunk_start, chunk_end in self.api_window_chunks():
            for mode, flag in (("owner", "--owner-ids"), ("participant", "--participant-ids")):
                page_token = ""
                page = 0
                while True:
                    page += 1
                    cmd = [
                        "minutes",
                        "+search",
                        "--as",
                        "user",
                        flag,
                        "me",
                        "--start",
                        iso_local(chunk_start),
                        "--end",
                        iso_local(chunk_end),
                        "--page-size",
                        str(self.args.minutes_page_size),
                        "--format",
                        "json",
                    ]
                    if page_token:
                        cmd.extend(["--page-token", page_token])
                    ok, body, err = run_lark(cmd, timeout=120)
                    if not ok:
                        self.error("feishu-minutes", err)
                        break
                    data = data_part(body)
                    items = data.get("items") or []
                    if not isinstance(items, list):
                        self.error("feishu-minutes", "unexpected minutes search response")
                        break
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        token = str(item.get("token") or find_first_key(item, {"minute_token"}) or "")
                        if not token:
                            continue
                        if token not in seen_tokens:
                            seen_tokens.add(token)
                            tokens.append(token)
                        key = f"feishu-minutes|minute|{token}"
                        if not mark_seen(self.conn, "feishu-minutes", key):
                            continue
                        meta = item.get("meta_data") if isinstance(item.get("meta_data"), dict) else {}
                        display = display_text(item.get("display_info") or token)
                        title = display.splitlines()[0] if display else token
                        content = "\n".join(
                            [
                                f"Feishu Minutes: {title}",
                                f"minute_token: {token}",
                                f"app_link: {meta.get('app_link') or ''}",
                                f"search_mode: {mode}",
                                display,
                            ]
                        )
                        records.append(
                            new_record(
                                source="feishu-minutes",
                                record_type="minutes",
                                timestamp=self.minutes_start_iso(item),
                                content=content,
                                metadata={"minute": item, "search_mode": mode},
                                session_id=token,
                            )
                        )
                    if len(records) >= self.args.flush_size:
                        self.add_records(records)
                        records = []
                        self.conn.commit()
                    page_token = data.get("page_token") or ""
                    has_more = bool(data.get("has_more")) and bool(page_token)
                    if not has_more:
                        break
                    if self.args.minutes_page_limit and page >= self.args.minutes_page_limit:
                        break
                    time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()
        self.collect_minutes_notes(self.limit_items(tokens, self.args.minutes_artifact_limit), origin="minutes-search")

    @staticmethod
    def minutes_start_iso(item: dict[str, Any]) -> str:
        meta = item.get("meta_data") if isinstance(item.get("meta_data"), dict) else {}
        text = display_text(f"{item.get('display_info') or ''}\n{meta.get('description') or ''}")
        match = re.search(r"开始时间:\s*(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)", text)
        if match:
            yyyy, mm, dd, time_text = match.groups()
            return normalize_timestamp(f"{yyyy}-{mm}-{dd} {time_text}")
        return iso_now()

    def collect_minutes_notes(self, minute_tokens: list[str], *, origin: str) -> None:
        tokens = [token for token in dict.fromkeys(minute_tokens) if token]
        if not tokens:
            return
        records: list[dict[str, Any]] = []
        FEISHU_DIR.mkdir(parents=True, exist_ok=True)
        for chunk in chunked(tokens, 20):
            ok, body, err = run_lark(
                [
                    "vc",
                    "+notes",
                    "--as",
                    "user",
                    "--minute-tokens",
                    ",".join(chunk),
                    "--output-dir",
                    self.relative_minutes_output_dir(),
                    "--format",
                    "json",
                ],
                timeout=240,
                cwd=FEISHU_DIR,
            )
            data = data_part(body)
            notes = data.get("notes") or []
            if not ok and not notes:
                self.error("feishu-minutes-note", err)
                continue
            if not isinstance(notes, list):
                self.error("feishu-minutes-note", "unexpected minutes notes response")
                continue
            for note in notes:
                if not isinstance(note, dict):
                    continue
                if note.get("error"):
                    token_for_error = note.get("minute_token") or find_first_key(note, {"minute_token"}) or "unknown"
                    self.error("feishu-minutes-note", f"{token_for_error}: {note.get('error')}")
                    continue
                token = str(note.get("minute_token") or find_first_key(note, {"minute_token"}) or "")
                artifacts = note.get("artifacts") if isinstance(note.get("artifacts"), dict) else {}
                transcript_text = self.read_transcript_file(artifacts.get("transcript_file"))
                content = self.minutes_note_content(note, transcript_text)
                key = f"feishu-minutes-note|{token or stable_hash(note)}|{stable_hash(content)}"
                if not mark_seen(self.conn, "feishu-minutes-note", key):
                    continue
                records.append(
                    new_record(
                        source="feishu-minutes-note",
                        record_type="minutes_note",
                        timestamp=iso_now(),
                        content=content,
                        metadata={
                            "note": note,
                            "origin": origin,
                            "artifact_cwd": str(FEISHU_DIR),
                            "transcript_chars": len(transcript_text),
                        },
                        session_id=token,
                    )
                )
            if len(records) >= self.args.flush_size:
                self.add_records(records)
                records = []
                self.conn.commit()
            time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()

    def read_transcript_file(self, path_value: Any) -> str:
        if not path_value:
            return ""
        path = Path(str(path_value))
        if not path.is_absolute():
            path = FEISHU_DIR / path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            self.error("feishu-minutes-note", f"cannot read transcript {path}: {type(exc).__name__}: {exc}")
            return ""
        return text[: self.args.minutes_transcript_chars]

    def minutes_note_content(self, note: dict[str, Any], transcript_text: str) -> str:
        artifacts = note.get("artifacts") if isinstance(note.get("artifacts"), dict) else {}
        title = display_text(note.get("title") or note.get("minute_token") or "Feishu Minutes")
        chapters = artifacts.get("chapters") if isinstance(artifacts.get("chapters"), list) else []
        todos = artifacts.get("todos") if isinstance(artifacts.get("todos"), list) else []
        chapter_lines = []
        for chapter in chapters[:80]:
            if not isinstance(chapter, dict):
                continue
            chapter_lines.append(
                f"- {display_text(chapter.get('title'))}: {display_text(chapter.get('summary_content'))}"
            )
        todo_lines = []
        for todo in todos[:80]:
            if isinstance(todo, dict):
                todo_lines.append(f"- {display_text(todo.get('content'))}")
            else:
                todo_lines.append(f"- {display_text(todo)}")
        parts = [
            f"Feishu Minutes note: {title}",
            f"minute_token: {note.get('minute_token') or ''}",
            "",
            "Summary:",
            display_text(artifacts.get("summary")),
            "",
            "Todos:",
            "\n".join(todo_lines),
            "",
            "Chapters:",
            "\n".join(chapter_lines),
        ]
        if transcript_text:
            parts.extend(["", "Transcript:", transcript_text])
        return "\n".join(part for part in parts if part is not None).strip()

    def collect_docs_probe(self) -> None:
        page_token = ""
        page = 0
        records: list[dict[str, Any]] = []
        while True:
            page += 1
            cmd = [
                "docs",
                "+search",
                "--as",
                "user",
                "--query",
                self.args.docs_query,
                "--page-size",
                str(self.args.docs_page_size),
                "--format",
                "json",
            ]
            if page_token:
                cmd.extend(["--page-token", page_token])
            ok, body, err = run_lark(cmd, timeout=60)
            if not ok:
                self.error("feishu-doc", err)
                return
            data = data_part(body)
            items = data.get("items") or data.get("docs") or data.get("results") or []
            if not isinstance(items, list):
                self.error("feishu-doc", "unexpected docs search response")
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                token = doc_search_token(item)
                key = f"feishu-doc|{token}"
                if not mark_seen(self.conn, "feishu-doc", key):
                    continue
                title = doc_search_title(item)
                url = doc_search_url(item)
                doc_type = doc_search_type(item)
                meta = doc_search_meta(item)
                records.append(
                    new_record(
                        source="feishu-doc",
                        record_type="document",
                        timestamp=normalize_timestamp(meta.get("update_time") or meta.get("create_time")),
                        content=f"Feishu document metadata: {title}\nurl: {url}\ntype: {doc_type}",
                        metadata={"document": item, "query": self.args.docs_query},
                        session_id=str(token),
                    )
                )
            if len(records) >= self.args.flush_size:
                self.add_records(records)
                records = []
                self.conn.commit()
            page_token = data.get("page_token") or ""
            has_more = bool(data.get("has_more")) and bool(page_token)
            if not has_more:
                break
            if self.args.docs_page_limit and page >= self.args.docs_page_limit:
                break
            time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()

    def collect_doc_contents(self) -> None:
        records: list[dict[str, Any]] = []
        docs: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if record.get("source") != "feishu-doc":
                        continue
                    document = (record.get("metadata") or {}).get("document") or {}
                    if not isinstance(document, dict):
                        continue
                    doc_type = doc_search_type(document).upper()
                    if doc_type not in {"DOC", "DOCX"}:
                        continue
                    token = doc_search_token(document)
                    if not token or token in seen_tokens:
                        continue
                    seen_tokens.add(token)
                    docs.append({"token": token, "document": document})
                    if self.args.doc_content_limit and len(docs) >= self.args.doc_content_limit:
                        break
        except FileNotFoundError:
            return
        for item in docs:
            token = item["token"]
            key = f"feishu-doc-content|{token}"
            if not mark_seen(self.conn, "feishu-doc-content", key):
                continue
            ok, body, err = run_lark(["docs", "+fetch", "--as", "user", "--doc", token, "--format", "json"], timeout=90)
            if not ok:
                self.error("feishu-doc-content", f"{token}: {err}")
                continue
            content_text = self.extract_doc_fetch_text(body)
            if not content_text:
                content_text = compact_json(data_part(body), 8000)
            document = item["document"]
            title = doc_search_title(document)
            url = doc_search_url(document)
            records.append(
                new_record(
                    source="feishu-doc-content",
                    record_type="document_content",
                    timestamp=iso_now(),
                    content=f"Feishu document content: {title}\nurl: {url}\n\n{content_text[: self.args.doc_content_chars]}",
                    metadata={"document": document, "fetch": body},
                    session_id=token,
                )
            )
            if len(records) >= self.args.flush_size:
                self.add_records(records)
                records = []
                self.conn.commit()
            time.sleep(self.args.page_delay)
        self.add_records(records)
        self.conn.commit()

    @staticmethod
    def extract_doc_fetch_text(body: dict[str, Any]) -> str:
        data = data_part(body)
        candidates: list[Any] = [
            data.get("markdown"),
            data.get("content"),
            data.get("text"),
            data.get("document"),
            data.get("blocks"),
        ]
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def collect_mail_probe(self) -> None:
        ok, body, err = run_lark(
            ["mail", "user_mailboxes", "profile", "--as", "user", "--params", json.dumps({"user_mailbox_id": "me"}), "--format", "json"],
            timeout=60,
        )
        if not ok:
            self.error("feishu-mail", err)
            return
        ok2, body2, err2 = run_lark(
            ["mail", "+triage", "--as", "user", "--format", "json", "--max", str(self.args.mail_max)],
            timeout=60,
        )
        if not ok2:
            self.error("feishu-mail", err2)
            return
        data = data_part(body2)
        messages = data.get("messages") or data.get("items") or data if isinstance(data, list) else []
        if not isinstance(messages, list):
            self.error("feishu-mail", "unexpected mail triage response")
            return
        records: list[dict[str, Any]] = []
        for mail in messages:
            if not isinstance(mail, dict):
                continue
            message_id = mail.get("message_id") or mail.get("id")
            if not message_id:
                continue
            key = f"feishu-mail|{message_id}"
            if not mark_seen(self.conn, "feishu-mail", key):
                continue
            records.append(
                new_record(
                    source="feishu-mail",
                    record_type="mail",
                    timestamp=normalize_timestamp(mail.get("date") or mail.get("created_at")),
                    content=f"Feishu mail: {mail.get('subject') or ''}\nfrom: {mail.get('from') or ''}\nto: {mail.get('to') or ''}",
                    metadata={"mail": mail},
                    session_id=str(message_id),
                )
            )
        self.add_records(records)
        self.conn.commit()

    def run(self) -> None:
        self.start_run()
        requested = {s.strip() for s in self.args.sources.split(",") if s.strip()}
        try:
            if "contacts" in requested:
                self.collect_contact_self()
            if "chats" in requested or "messages" in requested or "members" in requested:
                self.collect_chats()
            if "members" in requested:
                self.collect_chat_members()
            if "messages" in requested:
                self.collect_messages()
            if "message-search" in requested:
                self.collect_message_search()
            if "tasks" in requested:
                self.collect_tasks()
            if "calendar" in requested:
                self.collect_calendar()
            if "vc" in requested:
                self.collect_vc()
            if "minutes" in requested:
                self.collect_minutes()
            if "docs" in requested:
                self.collect_docs_probe()
            if "doc-contents" in requested:
                self.collect_doc_contents()
            if "mail" in requested:
                self.collect_mail_probe()
        finally:
            self.finish_run()
            self.conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Feishu/Lark data into ~/.immortal")
    parser.add_argument(
        "--sources",
        default="contacts,chats,members,messages,tasks,calendar,vc,minutes,docs,mail",
        help="Comma-separated sources: contacts,chats,members,messages,message-search,tasks,calendar,vc,minutes,docs,doc-contents,mail",
    )
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days unless --since or --all is provided")
    parser.add_argument("--since", default=None, help="Start date/time, e.g. 2026-05-01 or 2026-05-01T00:00:00+08:00")
    parser.add_argument("--until", default=None, help="End date/time, defaults to now")
    parser.add_argument("--all", action="store_true", help="Use a broad historical window from 2020-01-01")
    parser.add_argument("--max-chats", type=int, default=0, help="Limit chats scanned; 0 means no limit")
    parser.add_argument("--max-messages", type=int, default=0, help="Limit new messages written across all chats; 0 means no limit")
    parser.add_argument("--max-members", type=int, default=0, help="Limit new chat member records; 0 means no limit")
    parser.add_argument("--chat-page-size", type=int, default=100)
    parser.add_argument("--message-page-size", type=int, default=50)
    parser.add_argument("--search-chat-type", default="", help="Optional global message search chat type: group or p2p")
    parser.add_argument("--search-query", default="", help="Optional global message search query")
    parser.add_argument("--chat-page-limit", type=int, default=0, help="Limit chat list pages; 0 means no limit")
    parser.add_argument("--message-page-limit", type=int, default=0, help="Limit message pages per chat; 0 means no limit")
    parser.add_argument("--member-page-limit", type=int, default=0, help="Limit member pages per chat; 0 means no limit")
    parser.add_argument("--task-page-limit", type=int, default=40)
    parser.add_argument("--mail-max", type=int, default=100)
    parser.add_argument("--vc-page-limit", type=int, default=0, help="Limit meeting search pages per API window; 0 means no limit")
    parser.add_argument("--meeting-artifact-limit", type=int, default=80, help="Max meeting IDs per run for note/recording artifact fetch; 0 means no limit")
    parser.add_argument("--meeting-note-doc-content-limit", type=int, default=40, help="Max meeting note doc tokens to fetch per run; 0 means no limit")
    parser.add_argument("--meeting-note-doc-chars", type=int, default=20000, help="Max characters stored per meeting note document")
    parser.add_argument("--minutes-page-size", type=int, default=30)
    parser.add_argument("--minutes-page-limit", type=int, default=0, help="Limit Minutes search pages per API window/mode; 0 means no limit")
    parser.add_argument("--minutes-artifact-limit", type=int, default=80, help="Max minute tokens per run for summary/transcript artifact fetch; 0 means no limit")
    parser.add_argument("--minutes-output-dir", default="minutes_artifacts", help="Relative directory under ~/.immortal/feishu for downloaded minutes artifacts")
    parser.add_argument("--minutes-transcript-chars", type=int, default=60000, help="Max transcript characters stored per minute token")
    parser.add_argument("--docs-query", default="", help="Docs search query. Blank tries visible recent/searchable docs if the API allows it.")
    parser.add_argument("--docs-page-size", type=int, default=20)
    parser.add_argument("--docs-page-limit", type=int, default=40, help="Limit docs search pages; 0 means no limit")
    parser.add_argument("--doc-content-limit", type=int, default=200, help="Max DOC/DOCX documents to fetch content for; 0 means no limit")
    parser.add_argument("--doc-content-chars", type=int, default=12000, help="Max characters stored per fetched document content")
    parser.add_argument("--page-delay", type=float, default=0.2)
    parser.add_argument("--flush-size", type=int, default=200)
    parser.add_argument("--expected-user-name", default="", help="Refuse collection unless lark-cli auth userName contains this text")
    parser.add_argument("--expected-user-open-id", default="", help="Refuse collection unless lark-cli auth userOpenId exactly matches this value")
    parser.add_argument("--reject-user-name", default="", help="Refuse collection if lark-cli auth userName contains this text")
    parser.add_argument("--allow-current-account", action="store_true", help="Collect from the currently authenticated account without an expected account guard")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_dirs()
    collector = Collector(args)
    try:
        collector.run()
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        log_event("error", "fatal_error", message=f"{type(exc).__name__}: {exc}")
        print(f"Fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("Feishu collect finished")
    print(f"Run: {collector.run_id}")
    print(f"Window: {iso_local(collector.start)} -> {iso_local(collector.end)}")
    print("New records:")
    for source, count in sorted(collector.stats.items()):
        print(f"  {source}: {count}")
    if collector.errors:
        print("Issues:")
        for item in collector.errors[:10]:
            print(f"  {item['source']}: {item['message'][:180]}")
        if len(collector.errors) > 10:
            print(f"  ... {len(collector.errors) - 10} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
