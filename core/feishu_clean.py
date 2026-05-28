#!/usr/bin/env python3
"""Build a reviewable Feishu clean layer from ~/.immortal/index.jsonl.

This script is intentionally standalone. It streams the raw index, writes clean
JSONL artifacts, and only emits candidate memories for human review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


HOME = Path.home()
DEFAULT_INDEX = HOME / ".immortal/index.jsonl"
DEFAULT_OUTPUT_DIR = HOME / ".immortal/feishu/clean"
DEFAULT_REPORT_DIR = HOME / ".immortal/feishu/reports"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

FEISHU_PREFIX = "feishu-"
CHAT_SOURCES = {"feishu-im", "feishu-im-search"}
MEMORY_SOURCES = {
    "feishu-doc-content",
    "feishu-task",
    "feishu-calendar-event",
    "feishu-vc",
    "feishu-vc-note",
    "feishu-vc-note-content",
    "feishu-vc-recording",
    "feishu-minutes",
    "feishu-minutes-note",
}
NOISE_PATTERNS = [
    re.compile(r"\bjoined \{group_name\}", re.I),
    re.compile(r"\bleft \{group_name\}", re.I),
    re.compile(r"\bnew members can see all chat history\b", re.I),
    re.compile(r"^\[[a-z_ -]+ message\]$", re.I),
]
VALUE_TERMS = [
    "todo",
    "待办",
    "决定",
    "决策",
    "结论",
    "确认",
    "客户",
    "合同",
    "打款",
    "付款",
    "报价",
    "交付",
    "复盘",
    "方案",
    "需求",
    "deadline",
    "下周",
    "明天",
    "今天",
    "会议纪要",
    "okr",
    "sop",
    "授权",
    "负责人",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Feishu clean layer v1")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX, help="raw immortal index JSONL")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for records.jsonl, chat_daily.jsonl, candidate_memories.jsonl, coverage.json",
    )
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR, help="directory for markdown report")
    parser.add_argument("--limit", type=int, default=0, help="maximum Feishu records to inspect; 0 means no limit")
    parser.add_argument(
        "--sample-per-source",
        type=int,
        default=5,
        help="number of clean record samples to keep per source in coverage.json",
    )
    return parser.parse_args()


def stable_id(*parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def write_jsonl_line(handle: Any, obj: dict[str, Any]) -> None:
    handle.write(json_dumps(obj) + "\n")


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number = number / 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)

    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, normalized.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TZ)
            return parsed
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            pass
    return None


def localize(value: Any) -> tuple[str, str]:
    parsed = parse_timestamp(value)
    if not parsed:
        return "", ""
    local = parsed.astimezone(LOCAL_TZ)
    return local.isoformat(), local.date().isoformat()


def compact_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def nested(record: dict[str, Any], *keys: str) -> Any:
    current: Any = record
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def source_title(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    source = record.get("source")
    if source in CHAT_SOURCES:
        chat = nested(metadata, "chat") or nested(metadata, "message", "chat") or {}
        message = nested(metadata, "message") or {}
        return first_nonempty(chat.get("name"), message.get("chat_name"), record.get("session_id"), message.get("chat_id"))
    if source == "feishu-task":
        return first_nonempty(nested(metadata, "task", "summary"), "Feishu task")
    if source == "feishu-calendar-event":
        return first_nonempty(nested(metadata, "event", "summary"), "Feishu calendar event")
    if source == "feishu-vc":
        return first_nonempty(nested(metadata, "meeting", "display_info"), nested(metadata, "meeting", "id"), "Feishu meeting")
    if source == "feishu-vc-note":
        return first_nonempty(nested(metadata, "note", "meeting_id"), nested(metadata, "note", "note_doc_token"), "Feishu meeting note")
    if source == "feishu-vc-note-content":
        return first_nonempty(metadata.get("doc_token"), record.get("session_id"), "Feishu meeting note content")
    if source == "feishu-vc-recording":
        return first_nonempty(nested(metadata, "recording", "meeting_id"), nested(metadata, "recording", "minute_token"), "Feishu meeting recording")
    if source == "feishu-minutes":
        minute = nested(metadata, "minute") or {}
        meta = minute.get("meta_data") if isinstance(minute.get("meta_data"), dict) else {}
        return first_nonempty(minute.get("title"), minute.get("display_info"), meta.get("app_link"), record.get("session_id"), "Feishu Minutes")
    if source == "feishu-minutes-note":
        return first_nonempty(nested(metadata, "note", "title"), nested(metadata, "note", "minute_token"), record.get("session_id"), "Feishu Minutes note")
    if source in {"feishu-doc", "feishu-doc-content"}:
        document = nested(metadata, "document") or {}
        meta = document.get("result_meta") if isinstance(document.get("result_meta"), dict) else {}
        return first_nonempty(
            document.get("title"),
            document.get("title_highlighted"),
            meta.get("title"),
            meta.get("title_highlighted"),
            meta.get("token"),
            record.get("session_id"),
            "Feishu document",
        )
    if source == "feishu-chat":
        return first_nonempty(nested(metadata, "chat", "name"), record.get("session_id"), "Feishu chat")
    return first_nonempty(record.get("type"), source)


def clean_message_text(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    message = nested(metadata, "message") or {}
    text = first_nonempty(message.get("content"), message.get("text"))
    if text:
        return compact_text(text, 4000)
    content = str(record.get("content") or "")
    lines = content.splitlines()
    if len(lines) >= 4:
        return compact_text("\n".join(lines[3:]), 4000)
    return compact_text(content, 4000)


def clean_text(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    if record.get("source") in CHAT_SOURCES:
        return clean_message_text(record, metadata)
    if record.get("source") == "feishu-doc":
        document = nested(metadata, "document") or {}
        meta = document.get("result_meta") if isinstance(document.get("result_meta"), dict) else {}
        parts = [
            f"title: {source_title(record, metadata)}",
            f"url: {first_nonempty(meta.get('url'))}",
            f"type: {first_nonempty(meta.get('doc_types'), document.get('entity_type'))}",
            f"owner: {first_nonempty(meta.get('owner_name'), meta.get('owner_id'))}",
            f"updated: {first_nonempty(meta.get('update_time_iso'), meta.get('last_open_time_iso'))}",
            first_nonempty(document.get("summary_highlighted"), meta.get("summary")),
        ]
        return compact_text("\n".join(part for part in parts if part.strip(": ")), 3000)
    if record.get("source") == "feishu-doc-content":
        document = nested(metadata, "document") or {}
        meta = document.get("result_meta") if isinstance(document.get("result_meta"), dict) else {}
        markdown = first_nonempty(nested(metadata, "fetch", "data", "markdown"), record.get("content"))
        title = source_title(record, metadata)
        url = first_nonempty(meta.get("url"))
        prefix = "\n".join(part for part in [f"title: {title}", f"url: {url}"] if part.strip(": "))
        return compact_text(f"{prefix}\n\n{markdown}", 10000)
    return compact_text(record.get("content"), 8000)


def message_id(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    message = nested(metadata, "message") or {}
    return first_nonempty(message.get("message_id"), message.get("id"), record.get("id"))


def dedup_key(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    source = str(record.get("source") or "")
    if source in CHAT_SOURCES:
        return f"feishu-im|{message_id(record, metadata)}"
    if source in {"feishu-doc", "feishu-doc-content"}:
        token = first_nonempty(
            nested(metadata, "document", "result_meta", "token"),
            nested(metadata, "fetch", "data", "doc_id"),
            record.get("session_id"),
        )
        return f"{source}|{token}"
    return f"{source}|{record.get('id') or stable_id(record.get('timestamp'), record.get('content'))}"


def object_id(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    source = str(record.get("source") or "")
    if source in CHAT_SOURCES:
        return message_id(record, metadata)
    if source in {"feishu-doc", "feishu-doc-content"}:
        return first_nonempty(
            nested(metadata, "document", "result_meta", "token"),
            nested(metadata, "fetch", "data", "doc_id"),
            record.get("session_id"),
        )
    if source == "feishu-task":
        return first_nonempty(nested(metadata, "task", "guid"), record.get("id"))
    if source == "feishu-calendar-event":
        return first_nonempty(nested(metadata, "event", "event_id"), record.get("id"))
    if source == "feishu-vc":
        return first_nonempty(nested(metadata, "meeting", "id"), record.get("id"))
    if source == "feishu-vc-note":
        return first_nonempty(nested(metadata, "note", "meeting_id"), nested(metadata, "note", "note_doc_token"), record.get("id"))
    if source == "feishu-vc-note-content":
        return first_nonempty(metadata.get("doc_token"), record.get("session_id"), record.get("id"))
    if source == "feishu-vc-recording":
        return first_nonempty(nested(metadata, "recording", "meeting_id"), nested(metadata, "recording", "minute_token"), record.get("id"))
    if source in {"feishu-minutes", "feishu-minutes-note"}:
        return first_nonempty(
            nested(metadata, "minute", "token"),
            nested(metadata, "note", "minute_token"),
            record.get("session_id"),
            record.get("id"),
        )
    if source == "feishu-chat":
        return first_nonempty(nested(metadata, "chat", "chat_id"), record.get("session_id"), record.get("id"))
    if source == "feishu-chat-member":
        return first_nonempty(nested(metadata, "member", "member_id"), nested(metadata, "member", "open_id"), record.get("id"))
    if source == "feishu-calendar-list":
        return first_nonempty(nested(metadata, "calendar", "calendar_id"), record.get("id"))
    return first_nonempty(record.get("id"), stable_id(record.get("timestamp"), record.get("content")))


def container_id(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    source = str(record.get("source") or "")
    if source in CHAT_SOURCES:
        return first_nonempty(nested(metadata, "message", "chat_id"), nested(metadata, "chat", "chat_id"), record.get("session_id"))
    if source in {"feishu-doc", "feishu-doc-content"}:
        return first_nonempty(nested(metadata, "document", "result_meta", "url"), record.get("session_id"))
    if source == "feishu-task":
        return first_nonempty(nested(metadata, "task", "url"))
    if source == "feishu-calendar-event":
        return first_nonempty(nested(metadata, "calendar", "calendar_id"))
    if source == "feishu-vc":
        return first_nonempty(nested(metadata, "meeting", "id"))
    if source in {"feishu-vc-note", "feishu-vc-note-content", "feishu-vc-recording"}:
        return first_nonempty(nested(metadata, "note", "meeting_id"), nested(metadata, "recording", "meeting_id"), record.get("session_id"))
    if source in {"feishu-minutes", "feishu-minutes-note"}:
        return first_nonempty(nested(metadata, "minute", "meta_data", "app_link"), nested(metadata, "note", "minute_token"), record.get("session_id"))
    if source == "feishu-chat-member":
        return first_nonempty(nested(metadata, "chat", "chat_id"))
    return first_nonempty(record.get("session_id"))


def actor(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    source = str(record.get("source") or "")
    if source in CHAT_SOURCES:
        sender = nested(metadata, "message", "sender") or {}
        return first_nonempty(sender.get("name"), sender.get("id"), nested(metadata, "message", "sender_name"))
    if source in {"feishu-doc", "feishu-doc-content"}:
        meta = nested(metadata, "document", "result_meta") or {}
        return first_nonempty(meta.get("edit_user_name"), meta.get("owner_name"), meta.get("edit_user_id"), meta.get("owner_id"))
    if source == "feishu-calendar-event":
        return first_nonempty(nested(metadata, "event", "event_organizer", "display_name"), nested(metadata, "event", "event_organizer", "user_id"))
    if source == "feishu-task":
        return first_nonempty(nested(metadata, "task", "creator_name"), nested(metadata, "task", "creator_id"))
    if source == "feishu-vc":
        display = first_nonempty(nested(metadata, "meeting", "display_info"))
        match = re.search(r"组织者：([^|\\n]+)", display)
        return match.group(1).strip() if match else ""
    if source == "feishu-vc-note":
        return first_nonempty(nested(metadata, "note", "creator_id"))
    if source == "feishu-minutes":
        display = first_nonempty(nested(metadata, "minute", "display_info"), nested(metadata, "minute", "meta_data", "description"))
        match = re.search(r"所有者:\s*([^\\n ]+)", display)
        return match.group(1).strip() if match else ""
    return ""


def record_url(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    source = str(record.get("source") or "")
    if source in CHAT_SOURCES:
        return first_nonempty(nested(metadata, "message", "message_app_link"))
    if source in {"feishu-doc", "feishu-doc-content"}:
        return first_nonempty(nested(metadata, "document", "result_meta", "url"))
    if source == "feishu-task":
        return first_nonempty(nested(metadata, "task", "url"))
    if source == "feishu-calendar-event":
        return first_nonempty(nested(metadata, "event", "app_link"), nested(metadata, "event", "vchat", "meeting_url"))
    if source == "feishu-vc":
        return first_nonempty(nested(metadata, "meeting", "meta_data", "app_link"))
    if source == "feishu-minutes":
        return first_nonempty(nested(metadata, "minute", "meta_data", "app_link"))
    if source == "feishu-minutes-note":
        token = first_nonempty(nested(metadata, "note", "minute_token"), record.get("session_id"))
        return f"https://www.feishu.cn/minutes/{token}" if token else ""
    return ""


def score_record(source: str, record_type: str, title: str, text: str, metadata: dict[str, Any]) -> tuple[float, float, list[str]]:
    reasons: list[str] = []
    lower = f"{title}\n{text}".lower()
    text_len = len(text)

    noise = 0.1
    if not text:
        noise += 0.5
        reasons.append("empty text")
    if text_len < 8:
        noise += 0.25
        reasons.append("very short")
    if any(pattern.search(text) for pattern in NOISE_PATTERNS):
        noise += 0.65
        reasons.append("system/noise message")
    if nested(metadata, "message", "deleted") is True:
        noise += 0.5
        reasons.append("deleted message")
    if nested(metadata, "message", "msg_type") == "system":
        noise += 0.45
        reasons.append("system msg_type")
    if nested(metadata, "note", "error") or nested(metadata, "recording", "error"):
        noise += 0.75
        reasons.append("Feishu artifact unavailable")
    if re.fullmatch(r"https?://\S+", text.strip() or ""):
        noise += 0.35
        reasons.append("link only")

    value = 0.05
    if source in {"feishu-doc-content", "feishu-vc-note-content", "feishu-minutes-note"}:
        value += 0.55
        reasons.append("primary content")
    elif source in {"feishu-doc", "feishu-task", "feishu-calendar-event", "feishu-vc", "feishu-vc-note", "feishu-vc-recording", "feishu-minutes"}:
        value += 0.35
        reasons.append("structured Feishu source")
    elif source in CHAT_SOURCES:
        value += 0.08

    if text_len >= 80:
        value += 0.12
    if text_len >= 400:
        value += 0.12
    matched_terms = [term for term in VALUE_TERMS if term.lower() in lower]
    if matched_terms:
        value += min(0.32, 0.06 * len(set(matched_terms)))
        reasons.append("value terms: " + ", ".join(sorted(set(matched_terms))[:5]))
    if record_type in {"task", "calendar_event", "meeting", "document_content", "meeting_note", "meeting_note_content", "minutes", "minutes_note"}:
        value += 0.12

    noise = max(0.0, min(1.0, round(noise, 3)))
    value = max(0.0, min(1.0, round(value - max(0.0, noise - 0.35) * 0.35, 3)))
    return noise, value, reasons[:6]


def make_clean_record(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    local_ts, local_date = localize(raw.get("timestamp"))
    source = str(raw.get("source") or "")
    record_type = str(raw.get("type") or "")
    title = source_title(raw, metadata)
    text = clean_text(raw, metadata)
    noise, value, reasons = score_record(source, record_type, title, text, metadata)
    chat = nested(metadata, "chat") or nested(metadata, "message", "chat") or {}
    message = nested(metadata, "message") or {}
    sender = nested(metadata, "message", "sender") or {}
    return {
        "clean_id": stable_id(source, dedup_key(raw, metadata), raw.get("timestamp")),
        "raw_id": raw.get("id") or "",
        "source": source,
        "type": record_type,
        "timestamp": raw.get("timestamp") or "",
        "local_timestamp": local_ts,
        "local_date": local_date,
        "session_id": first_nonempty(raw.get("session_id"), message.get("chat_id"), chat.get("chat_id")),
        "object_id": object_id(raw, metadata),
        "container_id": container_id(raw, metadata),
        "chat_id": first_nonempty(message.get("chat_id"), chat.get("chat_id"), raw.get("session_id")),
        "chat_name": first_nonempty(chat.get("name"), message.get("chat_name")),
        "actor": actor(raw, metadata),
        "sender": first_nonempty(sender.get("name"), sender.get("id"), message.get("sender_name")),
        "message_id": message_id(raw, metadata) if source in CHAT_SOURCES else "",
        "msg_type": first_nonempty(message.get("msg_type"), message.get("message_type")),
        "title": compact_text(title, 300),
        "text": text,
        "url": record_url(raw, metadata),
        "noise_score": noise,
        "value_score": value,
        "score_reasons": reasons,
        "dedup_key": dedup_key(raw, metadata),
        "metadata_ref": metadata_ref(raw, metadata),
    }


def metadata_ref(raw: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    source = raw.get("source")
    if source in CHAT_SOURCES:
        return {
            "message_app_link": first_nonempty(nested(metadata, "message", "message_app_link")),
            "thread_id": first_nonempty(nested(metadata, "message", "thread_id")),
            "message_position": first_nonempty(nested(metadata, "message", "message_position")),
        }
    if source == "feishu-task":
        return {"url": first_nonempty(nested(metadata, "task", "url")), "complete": nested(metadata, "complete")}
    if source == "feishu-calendar-event":
        return {
            "app_link": first_nonempty(nested(metadata, "event", "app_link")),
            "organizer": first_nonempty(nested(metadata, "event", "event_organizer", "display_name")),
        }
    if source == "feishu-vc":
        return {"app_link": first_nonempty(nested(metadata, "meeting", "meta_data", "app_link"))}
    if source == "feishu-vc-note":
        return {
            "meeting_id": first_nonempty(nested(metadata, "note", "meeting_id")),
            "note_doc_token": first_nonempty(nested(metadata, "note", "note_doc_token")),
            "verbatim_doc_token": first_nonempty(nested(metadata, "note", "verbatim_doc_token")),
        }
    if source == "feishu-vc-note-content":
        return {"doc_token": first_nonempty(metadata.get("doc_token"))}
    if source == "feishu-vc-recording":
        return {
            "meeting_id": first_nonempty(nested(metadata, "recording", "meeting_id")),
            "minute_token": first_nonempty(nested(metadata, "recording", "minute_token")),
        }
    if source == "feishu-minutes":
        return {"app_link": first_nonempty(nested(metadata, "minute", "meta_data", "app_link")), "token": first_nonempty(nested(metadata, "minute", "token"))}
    if source == "feishu-minutes-note":
        return {
            "minute_token": first_nonempty(nested(metadata, "note", "minute_token")),
            "transcript_chars": metadata.get("transcript_chars"),
            "origin": first_nonempty(metadata.get("origin")),
        }
    if source in {"feishu-doc", "feishu-doc-content"}:
        return {"url": first_nonempty(nested(metadata, "document", "result_meta", "url"))}
    return {}


def candidate_kind(source: str) -> str:
    if source in {"feishu-doc", "feishu-doc-content"}:
        return "document_memory_candidate"
    if source == "feishu-task":
        return "task_memory_candidate"
    if source == "feishu-calendar-event":
        return "calendar_memory_candidate"
    if source == "feishu-vc":
        return "meeting_memory_candidate"
    if source in {"feishu-vc-note", "feishu-vc-note-content", "feishu-vc-recording", "feishu-minutes", "feishu-minutes-note"}:
        return "meeting_memory_candidate"
    return "chat_memory_candidate"


def candidate_priority(clean: dict[str, Any]) -> tuple[str, str]:
    source = clean["source"]
    text = f"{clean.get('title', '')}\n{clean.get('text', '')}".lower()
    sender = str(clean.get("sender") or clean.get("actor") or "")
    if source == "feishu-doc-content":
        return "high", "primary_docs"
    if source == "feishu-task":
        return "high", "tasks"
    if source == "feishu-calendar-event":
        return "medium", "timeline"
    if source == "feishu-vc":
        return "medium", "meeting_index"
    if source in {"feishu-vc-note-content", "feishu-minutes-note"}:
        return "high", "meeting_content"
    if source in {"feishu-vc-note", "feishu-vc-recording", "feishu-minutes"}:
        return "medium", "meeting_index"
    if source in CHAT_SOURCES:
        bot_like = sender.startswith("cli_") or "<card>" in text or "根据官方手册" in text or "应用模板中心" in text
        if bot_like:
            return "low", "bot_or_support_chat"
        if any(term in text for term in ["用户本人", "用户本人", "决定", "结论", "客户", "合同", "交付", "报价", "付款"]):
            return "medium", "chat_signal"
        return "low", "chat_keyword_match"
    return "low", "other"


def make_candidate(clean: dict[str, Any]) -> dict[str, Any] | None:
    source = clean["source"]
    high_value_chat = source in CHAT_SOURCES and clean["value_score"] >= 0.42 and clean["noise_score"] < 0.55
    if source not in MEMORY_SOURCES and not high_value_chat:
        return None
    if clean["noise_score"] >= 0.8:
        return None
    priority, bucket = candidate_priority(clean)
    return {
        "candidate_id": stable_id("candidate", clean["clean_id"]),
        "status": "review_candidate",
        "kind": candidate_kind(source),
        "distill_priority": priority,
        "review_bucket": bucket,
        "source": source,
        "clean_id": clean["clean_id"],
        "raw_id": clean["raw_id"],
        "local_date": clean["local_date"],
        "title": clean["title"],
        "evidence": compact_text(clean["text"], 900),
        "why": "; ".join(clean.get("score_reasons") or []) or "structured or high-value Feishu record",
        "confidence": round(max(0.05, min(0.95, clean["value_score"] * (1 - clean["noise_score"] * 0.5))), 3),
        "review_note": "Candidate only. Do not write to digital-soul.md without human review.",
    }


def update_chat_daily(bucket: dict[tuple[str, str], dict[str, Any]], clean: dict[str, Any]) -> None:
    if clean["source"] not in CHAT_SOURCES:
        return
    local_date = clean.get("local_date") or "unknown"
    session_id = clean.get("session_id") or clean.get("chat_id") or "unknown"
    key = (session_id, local_date)
    item = bucket.get(key)
    if item is None:
        item = {
            "session_id": session_id,
            "chat_id": clean.get("chat_id") or "",
            "chat_name": clean.get("chat_name") or "",
            "local_date": local_date,
            "message_count": 0,
            "noise_count": 0,
            "high_value_count": 0,
            "avg_noise_score": 0.0,
            "avg_value_score": 0.0,
            "first_local_timestamp": clean.get("local_timestamp") or "",
            "last_local_timestamp": clean.get("local_timestamp") or "",
            "top_senders": [],
            "high_value_samples": [],
            "_noise_total": 0.0,
            "_value_total": 0.0,
            "_sender_counts": {},
            "sample_clean_ids": [],
        }
        bucket[key] = item
    item["message_count"] += 1
    item["_noise_total"] += clean["noise_score"]
    item["_value_total"] += clean["value_score"]
    local_ts = clean.get("local_timestamp") or ""
    if local_ts:
        if not item["first_local_timestamp"] or local_ts < item["first_local_timestamp"]:
            item["first_local_timestamp"] = local_ts
        if not item["last_local_timestamp"] or local_ts > item["last_local_timestamp"]:
            item["last_local_timestamp"] = local_ts
    sender = clean.get("sender") or clean.get("actor") or ""
    if sender:
        item["_sender_counts"][sender] = item["_sender_counts"].get(sender, 0) + 1
    if clean["noise_score"] >= 0.6:
        item["noise_count"] += 1
    if clean["value_score"] >= 0.42 and clean["noise_score"] < 0.55:
        item["high_value_count"] += 1
        if len(item["high_value_samples"]) < 5:
            item["high_value_samples"].append(
                {
                    "clean_id": clean["clean_id"],
                    "local_timestamp": clean.get("local_timestamp") or "",
                    "sender": sender,
                    "value_score": clean["value_score"],
                    "text_preview": compact_text(clean.get("text"), 260),
                }
            )
    if not item["chat_name"] and clean.get("chat_name"):
        item["chat_name"] = clean["chat_name"]
    if len(item["sample_clean_ids"]) < 5:
        item["sample_clean_ids"].append(clean["clean_id"])


def finalize_chat_daily(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        count = item["message_count"] or 1
        out = dict(item)
        out["avg_noise_score"] = round(out.pop("_noise_total") / count, 3)
        out["avg_value_score"] = round(out.pop("_value_total") / count, 3)
        sender_counts = out.pop("_sender_counts")
        out["top_senders"] = [
            {"sender": sender, "count": count}
            for sender, count in sorted(sender_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:8]
        ]
        rows.append(out)
    return sorted(rows, key=lambda row: (row["local_date"], row["session_id"]))


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)

    records_path = args.output_dir / "records.jsonl"
    chat_daily_path = args.output_dir / "chat_daily.jsonl"
    candidates_path = args.output_dir / "candidate_memories.jsonl"
    coverage_path = args.output_dir / "coverage.json"

    counters: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_chat_messages: set[str] = set()
    chat_daily: dict[tuple[str, str], dict[str, Any]] = {}
    invalid_json = 0
    inspected_feishu = 0

    with args.index.open("r", encoding="utf-8", errors="replace") as index_file, records_path.open(
        "w", encoding="utf-8"
    ) as records_file, candidates_path.open("w", encoding="utf-8") as candidates_file:
        for line_number, line in enumerate(index_file, 1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                invalid_json += 1
                continue
            source = str(raw.get("source") or "")
            if not source.startswith(FEISHU_PREFIX):
                continue
            inspected_feishu += 1
            if args.limit and inspected_feishu > args.limit:
                break
            counters["raw_feishu_records"] += 1
            counters[f"raw_source:{source}"] += 1

            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            key = dedup_key(raw, metadata)
            if source in CHAT_SOURCES:
                if key in seen_chat_messages:
                    counters["deduped_chat_records"] += 1
                    continue
                seen_chat_messages.add(key)

            clean = make_clean_record(raw)
            write_jsonl_line(records_file, clean)
            counters["clean_records"] += 1
            counters[f"clean_source:{source}"] += 1
            if clean["noise_score"] >= 0.6:
                counters["noisy_clean_records"] += 1
            if clean["value_score"] >= 0.42:
                counters["valuable_clean_records"] += 1

            if len(samples[source]) < args.sample_per_source:
                samples[source].append(
                    {
                        "clean_id": clean["clean_id"],
                        "local_date": clean["local_date"],
                        "title": clean["title"],
                        "noise_score": clean["noise_score"],
                        "value_score": clean["value_score"],
                        "text_preview": compact_text(clean["text"], 180),
                    }
                )

            update_chat_daily(chat_daily, clean)
            candidate = make_candidate(clean)
            if candidate:
                write_jsonl_line(candidates_file, candidate)
                counters["candidate_memories"] += 1
                counters[f"candidate_source:{source}"] += 1
                counters[f"candidate_priority:{candidate['distill_priority']}"] += 1
                counters[f"candidate_bucket:{candidate['review_bucket']}"] += 1

    with chat_daily_path.open("w", encoding="utf-8") as handle:
        for row in finalize_chat_daily(chat_daily.values()):
            write_jsonl_line(handle, row)
            counters["chat_daily_rows"] += 1

    coverage = {
        "generated_at": datetime.now(tz=LOCAL_TZ).isoformat(),
        "index": str(args.index),
        "output_dir": str(args.output_dir),
        "report_dir": str(args.report_dir),
        "limit": args.limit,
        "sample_per_source": args.sample_per_source,
        "invalid_json_lines": invalid_json,
        "counters": dict(sorted(counters.items())),
        "samples": dict(sorted(samples.items())),
        "notes": [
            "Input is streamed line by line; the full index is not loaded into memory.",
            "feishu-im and feishu-im-search are deduped by metadata.message.message_id.",
            "candidate_memories.jsonl contains review candidates only and does not update digital-soul.md.",
        ],
    }
    coverage_path.write_text(json_dumps(coverage) + "\n", encoding="utf-8")

    report_path = args.report_dir / f"feishu-clean-{datetime.now(tz=LOCAL_TZ).strftime('%Y%m%d-%H%M%S')}.md"
    report_path.write_text(render_report(coverage, records_path, chat_daily_path, candidates_path), encoding="utf-8")

    print(f"clean_records={counters['clean_records']}")
    print(f"candidate_memories={counters['candidate_memories']}")
    print(f"chat_daily_rows={counters['chat_daily_rows']}")
    print(f"coverage={coverage_path}")
    print(f"report={report_path}")
    return 0


def render_report(coverage: dict[str, Any], records_path: Path, chat_daily_path: Path, candidates_path: Path) -> str:
    counters = coverage["counters"]
    source_lines = []
    priority_lines = []
    bucket_lines = []
    for key, value in counters.items():
        if key.startswith("clean_source:"):
            source_lines.append(f"- `{key.removeprefix('clean_source:')}`: {value}")
        if key.startswith("candidate_priority:"):
            priority_lines.append(f"- `{key.removeprefix('candidate_priority:')}`: {value}")
        if key.startswith("candidate_bucket:"):
            bucket_lines.append(f"- `{key.removeprefix('candidate_bucket:')}`: {value}")
    source_block = "\n".join(source_lines) if source_lines else "- No clean records"
    priority_block = "\n".join(priority_lines) if priority_lines else "- No candidate priority stats"
    bucket_block = "\n".join(bucket_lines) if bucket_lines else "- No candidate bucket stats"
    return f"""# Feishu Clean Layer v1 Report

Generated: {coverage["generated_at"]}

## Outputs

- Clean records: `{records_path}`
- Chat daily aggregation: `{chat_daily_path}`
- Candidate memories: `{candidates_path}`
- Coverage: `{Path(coverage["output_dir"]) / "coverage.json"}`

## Summary

- Raw Feishu records inspected: {counters.get("raw_feishu_records", 0)}
- Clean records written: {counters.get("clean_records", 0)}
- Chat duplicates skipped: {counters.get("deduped_chat_records", 0)}
- Chat daily rows: {counters.get("chat_daily_rows", 0)}
- Candidate memories written: {counters.get("candidate_memories", 0)}
- Invalid JSON lines skipped: {coverage.get("invalid_json_lines", 0)}

## Clean Records By Source

{source_block}

## Candidate Priorities

{priority_block}

## Candidate Review Buckets

{bucket_block}

## Notes

- The index is streamed line by line.
- `feishu-im` and `feishu-im-search` are deduped by message id.
- Candidate memories are review-only and do not write `digital-soul.md`.
"""


if __name__ == "__main__":
    raise SystemExit(main())
