#!/usr/bin/env python3
"""Sync Immortal daily diaries into Get笔记.

The script reads the already generated Immortal daily summaries and turns each
day into one plain-text Get note. It keeps a local sync state so reruns update
the same note instead of creating duplicates, then adds the note into a Get
knowledge topic.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import configured_vault_dir, load_config, owner_aliases, owner_display_name


IMMORTAL_DIR = configured_vault_dir()
SKILL_DIR = Path(__file__).resolve().parent
DAILY_DIR = IMMORTAL_DIR / "daily"
SUMMARY_DIR = IMMORTAL_DIR / "summaries"
GETNOTE_DIR = IMMORTAL_DIR / "getnote"
DIARY_DIR = GETNOTE_DIR / "diaries"
STATE_FILE = GETNOTE_DIR / "diary_sync_state.json"
LATEST_JSON = GETNOTE_DIR / "latest.json"
GETNOTE_CONFIG = Path.home() / ".getnote" / "config.json"
BASE_URL = "https://openapi.biji.com/open/api/v1"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_TOPIC_NAME = "永生记忆库"
DEFAULT_TOPIC_DESCRIPTION = "由 Immortal Memory 自动同步的每日行动日记。"
DEFAULT_TAGS = ["永生日记", "记忆库", "自动同步"]
LOW_VALUE_UTTERANCES = {
    "好的",
    "好",
    "嗯",
    "嗯好",
    "继续",
    "继续任务",
    "好的继续",
    "可以",
    "收到",
    "执行任务",
}
DECISION_RE = re.compile(r"(我觉得|我认为|我决定|我准备|我要|我想|以后|不要|不用|应该|就按|听取你的建议|修正|优化|同步|自动|开源|上线|打包|备份)")
FOLLOWUP_RE = re.compile(r"(明天|今晚|后续|下一步|继续|记得|需要|还要|待办|自动|每天|同步|清理|补齐)")
DONE_RE = re.compile(r"(已完成|完成|写入|生成|创建|更新|修复|验证|通过|推送|同步|提交|安装完成|健康检查)")


class GetNoteError(RuntimeError):
    pass


def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def today_local() -> str:
    return now_local().date().isoformat()


def yesterday_local() -> str:
    return (now_local().date() - timedelta(days=1)).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_getnote_credentials() -> tuple[str, str]:
    api_key = os.environ.get("GETNOTE_API_KEY", "").strip()
    client_id = os.environ.get("GETNOTE_CLIENT_ID", "").strip()
    if not api_key or not client_id:
        config = read_json(GETNOTE_CONFIG, {})
        api_key = api_key or str(config.get("api_key") or config.get("GETNOTE_API_KEY") or "").strip()
        client_id = client_id or str(config.get("client_id") or config.get("GETNOTE_CLIENT_ID") or "").strip()
    if not api_key or not client_id:
        raise GetNoteError("missing GetNote credentials: set ~/.getnote/config.json or GETNOTE_API_KEY/GETNOTE_CLIENT_ID")
    return api_key, client_id


class GetNoteClient:
    def __init__(self, api_key: str, client_id: str, timeout: int = 30) -> None:
        self.api_key = re.sub(r"^Bearer\s+", "", api_key.strip(), flags=re.I)
        self.client_id = client_id
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        url = BASE_URL + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url += "?" + query
        body = json.dumps(data or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": self.api_key,
                "X-Client-ID": self.client_id,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise GetNoteError(f"GetNote HTTP {exc.code}") from exc
            error = payload.get("error") or {}
            raise GetNoteError(f"GetNote API error {error.get('code')}: {error.get('reason') or error.get('message')}")
        except Exception as exc:
            raise GetNoteError(f"GetNote request failed: {exc}") from exc
        if not payload.get("success", False):
            error = payload.get("error") or {}
            raise GetNoteError(f"GetNote API error {error.get('code')}: {error.get('reason') or error.get('message')}")
        return payload.get("data")

    def list_topics(self, page: int = 1) -> dict[str, Any]:
        return self.request("GET", "/resource/knowledge/list", params={"page": page}) or {}

    def create_topic(self, name: str, description: str = "") -> dict[str, Any]:
        return self.request("POST", "/resource/knowledge/create", data={"name": name, "description": description}) or {}

    def save_note(self, title: str, content: str, tags: list[str]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/resource/note/save",
            data={"title": title, "content": content, "note_type": "plain_text", "tags": tags},
        ) or {}

    def update_note(self, note_id: str | int, title: str, content: str, tags: list[str]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/resource/note/update",
            data={"id": str(note_id), "title": title, "content": content, "tags": tags},
        ) or {}

    def list_notes(self, since_id: str | int = 0) -> dict[str, Any]:
        return self.request("GET", "/resource/note/list", params={"since_id": since_id}) or {}

    def get_note(self, note_id: str | int) -> dict[str, Any]:
        return self.request("GET", "/resource/note/detail", params={"id": note_id}) or {}

    def delete_note(self, note_id: str | int) -> dict[str, Any]:
        return self.request("POST", "/resource/note/delete", data={"note_id": str(note_id)}) or {}

    def batch_add_notes_to_topic(self, topic_id: str, note_ids: list[str]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/resource/knowledge/note/batch-add",
            data={"topic_id": str(topic_id), "note_ids": [str(note_id) for note_id in note_ids]},
        ) or {}


def meaningful_summary_text(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 500:
        return False
    if "无记录" in stripped or "无可用摘要" in stripped:
        return False
    return "**总记录**" in stripped or "## Claude" in stripped or "## Codex" in stripped


def summary_is_meaningful(date: str) -> bool:
    path = SUMMARY_DIR / f"{date}.md"
    if not path.exists():
        return False
    return meaningful_summary_text(path.read_text(encoding="utf-8", errors="ignore"))


def list_summary_dates(*, include_empty: bool = False, include_raw_daily: bool = False) -> list[str]:
    dates = []
    if SUMMARY_DIR.exists():
        for path in SUMMARY_DIR.glob("*.md"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem):
                continue
            if include_empty or meaningful_summary_text(path.read_text(encoding="utf-8", errors="ignore")):
                dates.append(path.stem)
    if include_raw_daily and DAILY_DIR.exists():
        for path in DAILY_DIR.glob("*.jsonl*"):
            stem = path.name.split(".jsonl", 1)[0]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
                dates.append(stem)
    return sorted(set(dates))


def load_daily_records(date: str, limit: int = 20000) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    path = DAILY_DIR / f"{date}.jsonl"
    gz_path = DAILY_DIR / f"{date}.jsonl.gz"
    opener = None
    if path.exists():
        opener = path.open("rt", encoding="utf-8", errors="ignore")
    elif gz_path.exists():
        opener = gzip.open(gz_path, "rt", encoding="utf-8", errors="ignore")
    if opener is None:
        return records
    with opener as handle:
        for line in handle:
            if len(records) >= limit:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                records.append(row)
    return records


def ensure_summary(date: str) -> str:
    summary_path = SUMMARY_DIR / f"{date}.md"
    if summary_path.exists():
        return summary_path.read_text(encoding="utf-8", errors="ignore")
    result = subprocess.run(
        [sys.executable, str(SKILL_DIR / "summary.py"), "--date", date],
        capture_output=True,
        text=True,
        timeout=180,
    )
    text = result.stdout
    if result.returncode == 0 and text.strip():
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(text, encoding="utf-8")
        return text
    return f"# {date} 交互摘要\n\n无可用摘要。"


def parse_stats(summary: str) -> tuple[str, str]:
    stats = ""
    sources = ""
    for line in summary.splitlines():
        if line.startswith("**总记录**"):
            stats = line.replace("**", "").strip()
        elif line.startswith("**数据源**"):
            sources = line.replace("**", "").strip()
    return stats, sources


def clean_topic(text: str) -> str:
    lowered = text.lower()
    if "environment_context" in lowered or "<cwd>" in lowered or "<current_date>" in lowered:
        return ""
    if re.search(r"~/.+ zsh \d{4}-\d{2}-\d{2}", text) or re.search(r"/Users/.+ zsh \d{4}-\d{2}-\d{2}", text):
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.lower().startswith("environment_context"):
        return ""
    if text.startswith("local-command-caveat"):
        return ""
    text = text.replace(str(Path.home()), "~")
    if len(text) > 120:
        text = text[:117].rstrip() + "..."
    return text


def extract_session_topics(summary: str, limit: int = 14) -> list[str]:
    topics: list[str] = []
    pending_session = False
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped.startswith("- **会话 "):
            pending_session = True
            continue
        if pending_session and stripped and not stripped.startswith("*("):
            pending_session = False
            topic = clean_topic(stripped)
            if topic and topic not in topics:
                topics.append(topic)
                if len(topics) >= limit:
                    break
    return topics


def extract_outputs(summary: str, limit: int = 8) -> list[str]:
    outputs: list[str] = []
    capture = False
    for line in summary.splitlines():
        if line.startswith("## ") and ("产出" in line or "文件历史" in line or "Skill" in line):
            capture = True
            continue
        if line.startswith("## ") and capture:
            capture = False
        if capture and line.strip().startswith("- "):
            item = re.sub(r"\s+", " ", line.strip()[2:]).strip()
            if item and item not in outputs:
                outputs.append(item)
                if len(outputs) >= limit:
                    break
    return outputs


def trim_summary(summary: str, max_chars: int = 16000) -> str:
    text = summary.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n> 原始摘要过长，已截断；完整版本保留在本地 Immortal summaries。"


def owner_identity_terms() -> set[str]:
    config = load_config()
    terms = set(owner_aliases(config))
    terms.add(owner_display_name(config))
    feishu = config.get("feishu") if isinstance(config.get("feishu"), dict) else {}
    for key in ("expected_user_name", "expected_user_open_id"):
        value = str(feishu.get(key) or "").strip()
        if value:
            terms.add(value)
    return {term for term in terms if term}


def row_text(row: dict[str, Any]) -> str:
    return str(row.get("content") or row.get("text") or row.get("message") or "").strip()


def feishu_sender(text: str) -> str:
    for line in text.splitlines()[:8]:
        match = re.match(r"Sender:\s*(.+)", line.strip())
        if match:
            return match.group(1).strip()
    return ""


def strip_feishu_message(text: str) -> str:
    lines = text.splitlines()
    if lines and (lines[0].startswith("Feishu global message search result") or lines[0].startswith("Feishu chat:")):
        for idx, line in enumerate(lines):
            if line.startswith("Message type:"):
                return "\n".join(lines[idx + 1 :]).strip()
    return text.strip()


def sanitize_diary_text(text: str, *, max_chars: int = 160) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace(str(Path.home()), "~")
    text = re.sub(r"https?://\S+", lambda m: m.group(0).split("?")[0], text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"`+", "", text)
    text = re.sub(r"[*#>\[\]\(\)]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -：:\t\n")
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def is_low_value_text(text: str) -> bool:
    stripped = sanitize_diary_text(text, max_chars=220)
    if not stripped or stripped in LOW_VALUE_UTTERANCES:
        return True
    lowered = stripped.lower()
    noise_tokens = [
        "api error",
        "usage policy",
        "environment_context",
        "request id:",
        "user has answered your questions",
        "your questions have been answered",
        "base directory for this skill",
        "files mentioned by the user",
        "in app browser",
        "current url:",
        "agents.md instructions",
        "asia/shanghai",
        "knowledge cutoff",
        "the user interrupted",
        "previous tool",
        "current url",
        "extracted text",
        "撤回了一条消息",
        "后续群聊内容",
        "crontab",
        "=====",
        "automation id:",
        "automation memory:",
        "正文字数",
        "排版终审官",
    ]
    if any(token in lowered for token in noise_tokens):
        return True
    if re.search(r"~/.+ zsh \d{4}-\d{2}-\d{2}", stripped) or re.search(r"/Users/.+ zsh \d{4}-\d{2}-\d{2}", stripped):
        return True
    if ("你是谁" in stripped and "你的任务" in stripped) or ("检查项" in stripped and "修正规则" in stripped):
        return True
    if "你的唯一任务是创作" in stripped or "你是账号边界A" in stripped:
        return True
    if stripped.startswith("Automation:"):
        return True
    if stripped.startswith("[Image:") or stripped.startswith("Feishu meeting note document content"):
        return True
    if len(stripped) < 8 and not re.search(r"[\u4e00-\u9fff]{4,}", stripped):
        return True
    return False


def normalize_activity(text: str) -> str:
    rules = [
        (r"他视频后面如果是博客内容", ""),
        (r"codex cli|能打开我的 codex", "排查 Codex CLI 打开问题。"),
        (r"teamsite\.ai|teamco|共享 AI skil", "学习团队共享 AI skill 的做法。"),
        (r"账号边界A服务器搭建|服务器搭建", "推进 账号边界A服务器搭建。"),
        (r"需要装什么插件|什么插件/skill/工具/mcp|评估.*插件", "评估项目需要补哪些插件和工具。"),
        (r"几千块的计算.*浪费时间|浪费时间", "算清楚时间成本"),
        (r"合同.*法务", "合同法务确认"),
        (r"缺失.*每天.*一百天|最近一百天.*补全", "补齐最近一百天日记"),
        (r"小红书自动化|监控博主", "复盘小红书自动化和博主监控方案，确认现有方案被平台限制，仍未真正跑通。"),
        (r"盗号|安全性|安全风险", "审查项目安全风险，重点排查账号、凭证和被盗号风险。"),
        (r"bookwormengr|梁文锋|DeepSeek", "阅读并核查 DeepSeek / 梁文锋相关材料，为后续写成普通人能看懂的文章做资料准备。"),
        (r"claude ?code.*missing|claudecode.*损坏|binary is missing", "排查 Claude Code 桌面端损坏和二进制缺失问题。"),
        (r"vscode.*历史对话|claude code cli.*历史对话", "整理 VSCode 和 Claude Code CLI 历史对话，纳入永生记忆库。"),
        (r"codex.*报错|关机重启.*报错", "排查 Codex 重启后的本地运行报错。"),
        (r"get ?笔记", "推进 Get 笔记与永生记忆库的日记同步链路。"),
        (r"biji\.com/openapi|openapi", "验证 Get 笔记 OpenAPI 入口和本地接入方式。"),
        (r"开源|github", "推进永生记忆库开源空壳版本的脱敏发布。"),
        (r"免费用 GPT 生图|GPT Image-2|example\.com", "继续打磨 GPT 生图站的文章和入口，把免费这件事讲得更像人话。"),
        (r"个人开发者免费做的网站|土豆服务器|升级服务器", "想清楚免费生图站的真实成本和服务器问题。"),
        (r"superpowers", "研究 superpowers 项目，判断它能不能补进自己的工具链。"),
        (r"封面图|章节配图|配图需要", "继续测试文章配图流程。"),
        (r"写生图工具 skill|椅子老板", "回看生图工具 skill 那篇文章，继续补内容生产素材。"),
        (r"附件上传|图片上传|剪贴板", "推进产品里的附件、图片、链接和剪贴板上传能力。"),
        (r"天空中台.*账号密码|账号密码同步", "处理旧系统账号迁移和登录入口问题。"),
        (r"飞书账号登录|扫码登录|手机号登录", "排查飞书登录入口为什么直接走 CLI，而不是扫码或手机号登录。"),
        (r"排序.*tag|按场景筛选|分类.*标签", "继续调整提示词站的筛选标签和分类交互。"),
        (r"image 2|生图能力", "继续测试 Codex 的 Image 2 生图能力。"),
        (r"agent-context|ENTRY\.md|继续推进赛博永生记忆库", "用永生记忆库的 agent-context 接手项目，继续推进记忆库。"),
    ]
    for pattern, replacement in rules:
        if re.search(pattern, text, re.I):
            return replacement
    return text


def clean_diary_item(text: str, *, max_chars: int = 150) -> str:
    text = normalize_activity(sanitize_diary_text(text, max_chars=220))
    text = sanitize_diary_text(text, max_chars=max_chars)
    if not text or is_low_value_text(text):
        return ""
    noisy_patterns = [
        r"选题策划书",
        r"三问回答",
        r"品牌声音",
        r"你的唯一任务",
        r"你是谁",
        r"标题是：",
        r"副标题是：",
        r"画风设定",
        r"尺寸是",
        r"以下文章",
        r"AGENTS\.md",
        r"正文字数",
        r"排序 这个板块下面的 tag",
        r"the user",
        r"previous tool",
        r"plaintext",
        r"聊天记录",
        r"Extracted Text",
        r"撤回了一条消息",
        r"后续群聊内容",
        r"crontab",
        r"=====",
    ]
    if any(re.search(pattern, text, re.I) for pattern in noisy_patterns):
        return ""
    if len(text) > max_chars:
        return ""
    return text


def normalized_owner_message(row: dict[str, Any], owner_terms: set[str]) -> str:
    text = row_text(row)
    source = str(row.get("source") or "")
    role = str(row.get("role") or "")
    if source.startswith("feishu-im"):
        sender = feishu_sender(text)
        if sender and sender not in owner_terms:
            return ""
        text = strip_feishu_message(text)
    elif source.startswith("feishu-vc") or source.startswith("feishu-doc"):
        return ""
    elif role != "user":
        return ""
    if is_low_value_text(text):
        return ""
    return clean_diary_item(text, max_chars=160)


def normalized_assistant_output(row: dict[str, Any]) -> str:
    if str(row.get("role") or "") != "assistant":
        return ""
    text = row_text(row)
    if not DONE_RE.search(text):
        return ""
    lines = [sanitize_diary_text(line, max_chars=180) for line in text.splitlines()]
    lines = [line for line in lines if line and not is_low_value_text(line)]
    for line in lines:
        if DONE_RE.search(line) and not re.search(r"(API|token|secret|key|Authorization)", line, re.I):
            return line
    return ""


def unique_items(items: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = clean_diary_item(item, max_chars=160)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def build_diary_material(summary: str, records: list[dict[str, Any]]) -> dict[str, list[str]]:
    owner_terms = owner_identity_terms()
    session_messages: dict[str, list[str]] = {}
    decisions: list[str] = []
    followups: list[str] = []

    for row in records:
        message = normalized_owner_message(row, owner_terms)
        if message:
            session_id = str(row.get("session_id") or row.get("source") or "unknown")
            session_messages.setdefault(session_id, []).append(message)
            if DECISION_RE.search(message) and len(message) <= 180:
                decisions.append(message)
            if FOLLOWUP_RE.search(message) and len(message) <= 180:
                followups.append(message)

    activities: list[str] = []
    for messages in session_messages.values():
        candidates = [msg for msg in messages if len(msg) >= 12]
        if not candidates:
            continue
        first = candidates[0]
        if first not in activities:
            activities.append(first)

    for topic in extract_session_topics(summary, limit=16):
        clean = clean_diary_item(topic, max_chars=160)
        if clean and clean not in activities:
            activities.append(clean)

    outputs: list[str] = []
    for item in extract_outputs(summary, limit=20):
        if re.match(r"\.[a-z0-9]+:", item.strip(), re.I):
            continue
        if "immortal-blake-writing-review-agent" in item:
            continue
        if item.startswith("immortal:"):
            outputs.append("永生记忆库相关脚本、看板和同步链路继续迭代。")
        else:
            outputs.append(clean_diary_item(item, max_chars=160))

    clean_activities = unique_items(activities, 10)
    activity_keys = {item.lower() for item in clean_activities}
    clean_decisions = [
        item for item in unique_items(decisions, 8)
        if item.lower() not in activity_keys and not re.search(r"[？?]\s*$", item)
    ]
    clean_followups = [item for item in unique_items(followups, 8) if item.lower() not in activity_keys]
    return {
        "activities": clean_activities,
        "decisions": clean_decisions,
        "followups": clean_followups,
        "outputs": unique_items(outputs, 8),
    }


def diary_quality_errors(diary: str) -> list[str]:
    errors: list[str] = []
    forbidden = [
        "由 Immortal Memory",
        "证据索引",
        "记录规模",
        "数据来源",
        "总记录",
        "用户消息",
        "助手回复",
        "系统/文件",
        "本地记录文件",
        "本地摘要文件",
        "原始日志",
        "## 原始每日摘要",
        "## ",
        "> ",
        "`",
        "Feishu global message search result",
        "environment_context",
        "Automation ID:",
        "API Error:",
        "Message type:",
        "Sender:",
        "Request ID:",
        "正文字数",
        "Base directory for this skill",
    ]
    for token in forbidden:
        if token in diary:
            errors.append(f"forbidden token: {token}")
    body = "\n".join(line for line in diary.splitlines() if line.strip() and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", line.strip()))
    if len(body) > 190:
        errors.append("too long")
    if len(body) < 50:
        errors.append("too short")
    return errors


def strip_sentence_end(text: str) -> str:
    return text.strip().rstrip("。！？!?；;，,")


def ensure_sentence(text: str) -> str:
    clean = strip_sentence_end(text)
    if not clean:
        return ""
    return clean + "。"


def short_phrase(text: str, max_chars: int = 42) -> str:
    clean = strip_sentence_end(clean_diary_item(text, max_chars=120) or sanitize_diary_text(text, max_chars=120))
    clean = re.sub(r"^(继续|推进|处理|排查|研究|验证)", "", clean).strip(" ，。")
    if len(clean) > max_chars:
        clean = clean[:max_chars].rstrip(" ，。")
    return clean


def clip_diary_body(text: str, max_chars: int = 200) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rstrip(" ，。；")
    return clipped + "。"


def render_diary(date: str, summary: str, records: list[dict[str, Any]]) -> str:
    material = build_diary_material(summary, records)
    activities = material["activities"]
    decisions = material["decisions"]
    followups = material["followups"]
    topics = [short_phrase(item, 34) for item in activities[:3]]
    topics = [item for item in topics if item]
    judgment = short_phrase(decisions[0], 54) if decisions else ""
    followup = short_phrase(followups[0], 42) if followups else ""

    if len(topics) >= 2:
        body = f"今天主要在弄{topics[0]}、{topics[1]}"
        if len(topics) >= 3:
            body += f"、{topics[2]}"
        body += "。"
    elif topics:
        body = f"今天主要在弄{topics[0]}。"
    elif activities:
        body = "今天事情比较碎，但还是把手边能推进的坑往前推了一点。"
    else:
        body = "今天没有特别成形的大动作，就当是把散掉的记录收回来。"

    if judgment:
        body += f"中间又确认了一点：{judgment}。"
    else:
        body += "这一天比较碎，但主线还是把内容、工具和记忆库整理成以后能继续用的东西。"
    if followup:
        body += f"后面继续接住{followup}。"
    body = clip_diary_body(body, 185)
    return f"{date}\n\n{body}\n"


def ensure_topic(client: GetNoteClient, name: str, description: str, state: dict[str, Any]) -> dict[str, Any]:
    configured = state.get("topic") if isinstance(state.get("topic"), dict) else {}
    if configured.get("id") and configured.get("name") == name:
        return configured
    page = 1
    while page <= 10:
        data = client.list_topics(page)
        topics = data.get("topics") or []
        for topic in topics:
            if str(topic.get("name") or "") == name:
                result = {"id": str(topic.get("id") or topic.get("topic_id")), "name": name}
                state["topic"] = result
                return result
        if not data.get("has_more"):
            break
        page += 1
    created = client.create_topic(name, description)
    result = {"id": str(created.get("id") or created.get("topic_id")), "name": created.get("name") or name}
    state["topic"] = result
    return result


def find_recent_note_by_title(client: GetNoteClient, title: str, max_pages: int = 50) -> str:
    since_id: int | str = 0
    for _ in range(max_pages):
        data = client.list_notes(since_id)
        notes = data.get("notes") or []
        for note in notes:
            if str(note.get("title") or "") == title:
                return str(note.get("id"))
        if not data.get("has_more") or not notes:
            break
        since_id = notes[-1].get("id") or data.get("next_cursor") or since_id
    return ""


def sync_date(
    client: GetNoteClient,
    date: str,
    state: dict[str, Any],
    *,
    topic_name: str,
    topic_description: str,
    tags: list[str],
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    summary = ensure_summary(date)
    records = load_daily_records(date)
    diary = render_diary(date, summary, records)
    diary_path = DIARY_DIR / f"{date}.md"
    write_text(diary_path, diary)
    digest = content_hash(diary)
    title = f"永生日记｜{date}"
    quality_errors = diary_quality_errors(diary)
    if quality_errors:
        return {
            "date": date,
            "title": title,
            "action": "reject-low-quality",
            "note_id": "",
            "topic_id": "",
            "diary_path": str(diary_path),
            "content_chars": len(diary),
            "quality_errors": quality_errors[:8],
        }
    diaries = state.setdefault("diaries", {})
    existing = diaries.get(date) if isinstance(diaries.get(date), dict) else {}
    note_id = str(existing.get("note_id") or "")
    changed = digest != existing.get("content_hash")
    topic = {"id": "", "name": topic_name}
    action = "skip"
    if dry_run:
        action = "dry-run"
    elif note_id and not force and not changed:
        topic = {
            "id": existing.get("topic_id") or (state.get("topic") or {}).get("id") or "",
            "name": existing.get("topic_name") or topic_name,
        }
        action = "skip"
    else:
        topic = ensure_topic(client, topic_name, topic_description, state)
        if not note_id:
            note_id = find_recent_note_by_title(client, title)
        if note_id and (force or changed):
            client.update_note(note_id, title, diary, tags)
            action = "update"
        elif note_id:
            action = "skip"
        else:
            saved = client.save_note(title, diary, tags)
            note_id = str(saved.get("id"))
            action = "create"
        existing_topic_id = str(existing.get("topic_id") or "")
        if topic.get("id") and note_id and (action == "create" or not existing_topic_id):
            client.batch_add_notes_to_topic(str(topic["id"]), [note_id])
        diaries[date] = {
            "date": date,
            "title": title,
            "note_id": note_id,
            "topic_id": topic.get("id") or "",
            "topic_name": topic.get("name") or topic_name,
            "content_hash": digest,
            "diary_path": str(diary_path),
            "action": action,
            "synced_at": now_local().isoformat(timespec="seconds"),
        }
    result = {
        "date": date,
        "title": title,
        "action": action,
        "note_id": note_id,
        "topic_id": topic.get("id") or "",
        "diary_path": str(diary_path),
        "content_chars": len(diary),
    }
    return result


def sync_date_with_retry(
    client: GetNoteClient,
    date: str,
    state: dict[str, Any],
    *,
    topic_name: str,
    topic_description: str,
    tags: list[str],
    force: bool,
    dry_run: bool,
    retries: int,
    rate_limit_sleep: float,
) -> dict[str, Any]:
    attempt = 0
    while True:
        try:
            return sync_date(
                client,
                date,
                state,
                topic_name=topic_name,
                topic_description=topic_description,
                tags=tags,
                force=force,
                dry_run=dry_run,
            )
        except Exception as exc:
            attempt += 1
            message = str(exc)
            is_rate_limit = "qps_bucket_exceeded" in message or "rate" in message.lower() or "限流" in message
            if not is_rate_limit or attempt > retries:
                raise
            time.sleep(max(rate_limit_sleep, 1.0) * attempt)


def resolve_dates(args: argparse.Namespace) -> list[str]:
    if args.date:
        dates = args.date
    elif args.all:
        dates = list_summary_dates()
    elif args.since:
        dates = [date for date in list_summary_dates() if date >= args.since]
    elif args.yesterday:
        dates = [yesterday_local()]
    else:
        dates = [yesterday_local()]
    today = today_local()
    dates = [date for date in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) and (args.include_today or date < today)]
    dates = sorted(set(dates))
    if args.limit:
        dates = dates[-int(args.limit):]
    return dates


def command_status(_args: argparse.Namespace) -> int:
    state = read_json(STATE_FILE, {})
    latest = read_json(LATEST_JSON, {})
    diaries = state.get("diaries") if isinstance(state.get("diaries"), dict) else {}
    available_dates = list_summary_dates()
    synced_dates = {date for date, item in diaries.items() if isinstance(item, dict) and item.get("note_id")}
    pending_dates = [date for date in available_dates if date not in synced_dates]
    empty_synced_dates = [date for date in sorted(synced_dates) if not summary_is_meaningful(date)]
    print("GetNote diary sync")
    print(f"state={STATE_FILE}")
    print(f"topic={(state.get('topic') or {}).get('name') or 'missing'}")
    print(f"diaries={len(diaries)}")
    print(f"available_dates={len(available_dates)}")
    print(f"pending_dates={len(pending_dates)}")
    print(f"empty_synced_dates={len(empty_synced_dates)}")
    if pending_dates:
        print(f"pending_next={', '.join(pending_dates[:8])}")
    if empty_synced_dates:
        print(f"empty_synced_next={', '.join(empty_synced_dates[:8])}")
    print(f"latest_date={latest.get('latest_date') or ''}")
    print(f"latest_status={latest.get('status') or ''}")
    if latest.get("results"):
        for item in latest["results"][-8:]:
            print(f"- {item.get('date')} {item.get('action')} note={item.get('note_id')}")
    return 0


def command_sync(args: argparse.Namespace) -> int:
    dates = resolve_dates(args)
    state = read_json(STATE_FILE, {})
    if args.missing_limit:
        diaries = state.get("diaries") if isinstance(state.get("diaries"), dict) else {}
        synced_dates = {date for date, item in diaries.items() if isinstance(item, dict) and item.get("note_id")}
        dates = [date for date in dates if date not in synced_dates][: args.missing_limit]
    results: list[dict[str, Any]] = []
    if not dates:
        payload = {"status": "skip", "reason": "no dates", "generated_at": now_local().isoformat(timespec="seconds")}
        write_json(LATEST_JSON, payload)
        print("getnote_diary_sync=skip reason=no_dates")
        return 0
    client = None
    if not args.dry_run:
        api_key, client_id = read_getnote_credentials()
        client = GetNoteClient(api_key, client_id, timeout=args.timeout)
    else:
        client = GetNoteClient("dry", "dry", timeout=args.timeout)
    tags = [str(tag)[:10] for tag in (args.tag or DEFAULT_TAGS)][:5]
    quota_hit = False
    for date in dates:
        try:
            item = sync_date_with_retry(
                client,
                date,
                state,
                topic_name=args.topic_name,
                topic_description=args.topic_description,
                tags=tags,
                force=args.force,
                dry_run=args.dry_run,
                retries=args.retries,
                rate_limit_sleep=args.rate_limit_sleep,
            )
            results.append(item)
            if not args.dry_run:
                write_json(STATE_FILE, state)
            print(f"{date}: {item['action']} note={item.get('note_id') or '-'} chars={item['content_chars']}", flush=True)
            if args.delay > 0:
                time.sleep(args.delay)
        except Exception as exc:
            results.append({"date": date, "action": "error", "error": str(exc)})
            print(f"{date}: error {exc}", file=sys.stderr, flush=True)
            if "quota_daily_exceeded" in str(exc):
                quota_hit = True
                break
            if args.delay > 0:
                time.sleep(args.delay)
            if not args.continue_on_error:
                break
    status = "quota_exceeded" if quota_hit else "ok" if all(item.get("action") != "error" for item in results) else "error"
    payload = {
        "status": status,
        "generated_at": now_local().isoformat(timespec="seconds"),
        "latest_date": results[-1].get("date") if results else "",
        "results": results,
        "state_file": str(STATE_FILE),
    }
    if not args.dry_run:
        state["last_getnote_diary_sync"] = payload["generated_at"]
        state["last_getnote_diary_status"] = status
        if results:
            state["last_getnote_diary_date"] = results[-1].get("date")
            state["last_getnote_diary_note_id"] = results[-1].get("note_id") or ""
        write_json(STATE_FILE, state)
    if not args.no_latest and not args.dry_run:
        write_json(LATEST_JSON, payload)
    return 0 if status == "ok" else 1


def command_prune_empty(args: argparse.Namespace) -> int:
    state = read_json(STATE_FILE, {})
    diaries = state.get("diaries") if isinstance(state.get("diaries"), dict) else {}
    targets = [
        date
        for date, item in sorted(diaries.items())
        if isinstance(item, dict) and item.get("note_id") and not summary_is_meaningful(date)
    ]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("getnote_prune_empty=skip reason=no_empty_synced_dates")
        return 0
    if args.dry_run:
        print(f"getnote_prune_empty=dry-run count={len(targets)}")
        for date in targets[:50]:
            item = diaries.get(date) or {}
            print(f"- {date} note={item.get('note_id')}")
        return 0
    api_key, client_id = read_getnote_credentials()
    client = GetNoteClient(api_key, client_id, timeout=args.timeout)
    failures = 0
    quota_hit = False
    for date in targets:
        item = diaries.get(date) or {}
        note_id = item.get("note_id")
        try:
            client.delete_note(note_id)
            diaries.pop(date, None)
            write_json(STATE_FILE, state)
            print(f"{date}: delete note={note_id}", flush=True)
            if args.delay > 0:
                time.sleep(args.delay)
        except Exception as exc:
            failures += 1
            print(f"{date}: error {exc}", file=sys.stderr, flush=True)
            if "quota_daily_exceeded" in str(exc):
                quota_hit = True
                break
            if args.delay > 0:
                time.sleep(args.delay)
            if not args.continue_on_error:
                break
    payload = {
        "status": "quota_exceeded" if quota_hit else "ok" if failures == 0 else "error",
        "generated_at": now_local().isoformat(timespec="seconds"),
        "operation": "prune-empty",
        "attempted": len(targets),
        "failures": failures,
    }
    write_json(GETNOTE_DIR / "prune_latest.json", payload)
    return 0 if payload["status"] == "ok" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Immortal daily diaries to Get笔记")
    sub = parser.add_subparsers(dest="command")
    sync = sub.add_parser("sync", help="Generate and sync diaries")
    sync.add_argument("--date", action="append", default=[], help="Date to sync, YYYY-MM-DD. Repeatable")
    sync.add_argument("--yesterday", action="store_true", help="Sync yesterday in local timezone")
    sync.add_argument("--all", action="store_true", help="Backfill all available summary dates")
    sync.add_argument("--since", default="", help="Backfill summary dates since YYYY-MM-DD")
    sync.add_argument("--include-today", action="store_true")
    sync.add_argument("--limit", type=int, default=0)
    sync.add_argument("--topic-name", default=DEFAULT_TOPIC_NAME)
    sync.add_argument("--topic-description", default=DEFAULT_TOPIC_DESCRIPTION)
    sync.add_argument("--tag", action="append", default=[])
    sync.add_argument("--force", action="store_true", help="Update existing notes even when content hash did not change")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--continue-on-error", action="store_true")
    sync.add_argument("--delay", type=float, default=0.2)
    sync.add_argument("--timeout", type=int, default=30)
    sync.add_argument("--retries", type=int, default=3)
    sync.add_argument("--rate-limit-sleep", type=float, default=8.0)
    sync.add_argument("--missing-limit", type=int, default=0, help="Only sync this many dates that do not have a saved note_id")
    sync.add_argument("--no-latest", action="store_true", help="Do not overwrite latest.json; useful for background backfill")
    sync.set_defaults(func=command_sync)
    status = sub.add_parser("status", help="Show sync status")
    status.set_defaults(func=command_status)
    prune = sub.add_parser("prune-empty", help="Delete synced notes that came from empty/no-record summaries")
    prune.add_argument("--limit", type=int, default=0)
    prune.add_argument("--dry-run", action="store_true")
    prune.add_argument("--continue-on-error", action="store_true")
    prune.add_argument("--delay", type=float, default=3.0)
    prune.add_argument("--timeout", type=int, default=30)
    prune.set_defaults(func=command_prune_empty)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in {"sync", "status", "prune-empty", "-h", "--help"}:
        argv = ["sync", *argv]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        args = parser.parse_args(["sync", "--yesterday"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
