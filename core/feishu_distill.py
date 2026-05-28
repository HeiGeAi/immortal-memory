#!/usr/bin/env python3
"""Build evidence-backed memory candidates from the Feishu clean layer.

This is a review layer, not a write into digital-soul.md. It turns cleaned
Feishu candidates into structured facts, decisions, commitments, project facts,
preferences, relationship notes, and timeline events with evidence pointers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from config import load_config, owner_aliases
except Exception:  # pragma: no cover - keeps packaged/script fallback robust
    load_config = None
    owner_aliases = None


HOME = Path.home()
DEFAULT_CANDIDATES = HOME / ".immortal/feishu/clean/candidate_memories.jsonl"
DEFAULT_RECORDS = HOME / ".immortal/feishu/clean/records.jsonl"
DEFAULT_OUTPUT_DIR = HOME / ".immortal/feishu/distilled"
DEFAULT_REPORT_DIR = HOME / ".immortal/feishu/reports"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_PRIORITIES = {"high", "medium"}


def load_owner_identity() -> tuple[list[str], str]:
    aliases = ["用户本人", "用户本人", "Configured User", "用户本人"]
    open_id = ""
    if load_config is None:
        return aliases, open_id
    try:
        config = load_config()
        if owner_aliases is not None:
            configured_aliases = [str(alias).strip() for alias in owner_aliases(config) if str(alias).strip()]
        else:
            configured_aliases = [str(alias).strip() for alias in config.get("owner_aliases", []) if str(alias).strip()]
        display = str(config.get("owner_display_name") or "").strip()
        name = str(config.get("owner_name") or "").strip()
        aliases = list(dict.fromkeys([*configured_aliases, display, name, *aliases]))
        feishu = config.get("feishu") if isinstance(config.get("feishu"), dict) else {}
        open_id = str(feishu.get("expected_user_open_id") or "").strip()
    except Exception:
        pass
    return aliases, open_id


OWNER_ALIASES, OWNER_FEISHU_OPEN_ID = load_owner_identity()

SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9_\-]{16,}|xox[baprs]-[A-Za-z0-9_\-]+|"
    r"akia[0-9a-z]{16}|AIza[0-9A-Za-z_\-]{20,}|cli_[A-Za-z0-9_\-]{8,}|"
    r"api[_ -]?key\s*[:=]\s*\S+|app\s*(id|secret)\s*[:=：]\s*\S+|"
    r"password\s*[:=：]\s*\S+|密码\s*[:=：]\s*\S+|token\s*[:=：]\s*\S+|"
    r"(access|refresh)[_-]?token\s*[:=：]\s*\S+|bearer\s+[A-Za-z0-9._\-]+)"
)
URL_RE = re.compile(r"https?://[^\s)）]+")
TAG_RE = re.compile(r"<[^>]+>")

PROJECT_TERMS = {
    "immortal": ["永生", "赛博永生", "记忆库", "digital soul", "immortal", "删库", "语料"],
    "project_a": ["项目A", "中台", "智能体", "知识库", "某区域电商", "外部客户A"],
    "main_account": ["用户本人AI", "主账号", "主账号", "用户本人", "用户本人"],
    "machine0": ["账号边界A", "账号边界A"],
    "partner_brand": ["协作账号", "协作账号", "标题 Skill", "写作工作流", "商业化稿件"],
    "feishu": ["飞书", "多维表格", "妙记", "日程", "服务群"],
    "openclaw": ["OpenClaw", "DeskClaw", "龙虾", "Claude Code", "Codex", "Hermes"],
    "content_ops": ["小红书", "私域", "投放", "选题", "公众号", "短视频", "TGIF", "老乡鸡"],
    "business_ops": ["客户", "合同", "报价", "付款", "打款", "商务", "交付", "绩效", "招聘"],
}

STRONG_PROFILE_TERMS = [
    "用户本人",
    "用户本人",
    "Configured User",
    "协作账号",
    "项目A",
    "账号边界A",
    "账号边界A",
    "用户本人AI",
    "主账号",
    "协作账号",
    "永生",
    "记忆库",
    "赛博永生",
    "中台",
    "某区域电商",
    "外部客户A",
    "客户",
    "合同",
    "报价",
    "交付",
    "商务",
    "写作工作流",
    "标题 Skill",
    "Mac mini",
    "Codex",
    "Claude Code",
    "OpenClaw",
    "Hermes",
]

GENERIC_DOC_TITLE_RE = re.compile(
    r"(AI ?科技(日报|早报)|常见问题|配置指南|安装|使用手册|活动说明|公开版|开发者指南|"
    r"产品使用说明|案例合集|Brief|速查|Qclaw|Open code安装|DeskClaw|Claude Code 安装|"
    r"选题报告|直播回放|入驻|全流程指引|PRM|伙伴|小白无门槛宝典|问题速查|"
    r"0门槛入门|无门槛实用指南|相关内容分享)"
)

CORE_DOC_TITLE_RE = re.compile(
    r"(工作流|路线图|账号定位|自我介绍|知识库|总目录|"
    r"客户|合同|价格确认|工作安排|业务发展|运营规划|绩效|招聘|试用期|项目|方案|总表|"
    r"项目A|用户本人|账号边界A|账号边界A|协作账号|协作账号|永生|记忆库|Skill)"
)
MEETING_TITLE_RE = re.compile(r"(会议纪要|智能纪要|文字记录|周例会|视频会议|会议记录)")
RAW_TRANSCRIPT_TITLE_RE = re.compile(r"(文字记录|视频会议|会议记录)")
STRUCTURED_SUMMARY_TITLE_RE = re.compile(
    r"(智能纪要|会议纪要|正确使用方式|自我介绍|账号定位|知识库|总目录|公司业务信息|"
    r"给小舟的对接整理|工作安排|业务发展|运营规划|价格确认|会议决议|会议要点)"
)

PEOPLE_TERMS = [
    "用户本人",
    "用户本人",
    "Configured User",
    "同事代理账号",
    "协作者C",
    "协作者C",
    "协作账号",
    "协作者A",
    "协作账号",
    "协作者L",
    "协作者D",
    "协作者E",
    "协作者F",
    "协作者G",
    "协作者B",
    "协作者H",
    "错误账号",
    "错误账号",
    "错误账号",
    "协作者I",
    "协作者J",
    "协作者K",
    "外部客户A",
    "协作者B",
]
MAMA_ROLE_RE = re.compile(r"@?同事代理账号\s*协作者C|@?同事代理账号")

DECISION_TERMS = ["决定", "明确", "拍板", "采用", "选用", "不再", "转向", "统一", "收口", "替代"]
PREFERENCE_TERMS = ["原则", "最高原则", "要求", "必须", "不要", "不能", "优先", "规范", "标准", "偏好"]
COMMITMENT_TERMS = ["待办", "跟进", "完成", "推进", "下周", "本周", "明天", "后续", "尽快", "负责", "配合"]
LESSON_TERMS = ["复盘", "教训", "经验", "根因", "问题", "卡点", "解决方案", "风险"]
RELATION_TERMS = ["客户", "对接", "主导", "承接", "配合", "负责人", "服务群", "售前", "交付方"]

JUNK_TASK_RE = re.compile(r"^(123123|测试|写明具体任务，，|填写下一步任务，，|填写具体的执行计划，，|剪辑|来自会话：)")
USER_ALIAS_RE = re.compile(r"(用户本人|用户本人|Configured User|用户本人)")
BIBI_ACCOUNT_TITLE_RE = re.compile(r"(协作账号的正确使用方式|协作账号 ·|三篇Claude Code对标文章审稿报告|协作账号.*审稿|协作账号.*文风)")
BIBI_ACCOUNT_STATEMENT_RE = re.compile(
    r"(协作账号.*(内容特色|文风|选题|标题技巧|粉丝IP)|"
    r"零门槛硬糖|强能力不再普惠|准入证逻辑|高质量评论|持续新增信息)"
)
THIRD_PARTY_ONLY_RE = re.compile(r"^(协作者A|协作者D|协作者E|协作者F|协作者G|协作者B|协作者H|错误账号|协作者I|协作者J|协作者K|外部客户A|协作者B)(认为|指出|建议|表示|介绍)")
CANDIDATE_RESUME_RE = re.compile(r"(面试|候选人|曾在.*实习|工作\d+个?月|空窗|转正时被要求|全员被开|离职)")
INSTRUCTIONAL_TOOL_RE = re.compile(r"(安装|卸载|配置|官方文档|使用教程|工作可视化工具使用讨论|Claude及相关工具展开交流)")
FIRST_PERSON_VIEW_RE = re.compile(
    r"(我的|我希望|我认为|我觉得|我感觉|我准备|我现在|我会|我要|我需要|我担心|我得|"
    r"对我来说|刚到公司的下午|我的工作模式|我的脑子|我的决策资源|这会.*我的注意力)"
)
USER_ATTRIBUTION_RE = re.compile(
    r"(用户本人|用户本人|Configured User|用户本人)(?:[：:，,、\s]*(?:明确|曾经|曾|也|再次|已|将会|会)?[：:，,、\s]*)"
    r"(提出|认为|表示|反馈|要求|决定|建议|负责|主导|优先|确认|需要|希望|准备|配置|使用)"
)
USER_DURABLE_REMINDER_RE = re.compile(
    r"(用户本人|用户本人|Configured User|用户本人)(?:[：:，,、\s]*(?:明确|也|再次)?[：:，,、\s]*)提醒"
    r".{0,80}(独立思考|不能完全依赖|风险|注意|别|不要|必须|建议|原则|规范|安全|数据|隐私|灰色|合规)"
)
USER_PERSPECTIVE_TOPIC_RE = re.compile(
    r"(工作原则|沟通偏好|雷区|工作模式|脑力|精力|注意力|决策资源|提前告知|反馈|指导|"
    r"需求|偏好|目标|负责|主导|优先|策略|转型|报价|合同|收费|招聘|中台|Skill|skill)"
)
EXTERNAL_FACT_RE = re.compile(
    r"((用户本人|用户本人|Configured User|用户本人).{0,8}介绍.{0,80}(要求|指标|级别|代理|ARR|保证金|考试|官方|政策|规则)|"
    r"指标要求：.{0,120}(ARR|保证金|考试|销售负责业务线))",
    re.I,
)
ONE_OFF_COORDINATION_RE = re.compile(
    r"(用户本人|用户本人|Configured User|用户本人).{0,8}(提醒|让|叫|通知|安排).{0,40}"
    r"(协作者D|协作者E|协作者F|协作者G|协作者B|协作者H|错误账号|协作者I|协作者J|协作者K).{0,80}"
    r"(查看|购买|处理|发|拉|上传|下载|开通|续费|确认)"
)
MEETING_INTRO_SUMMARY_RE = re.compile(r"^(本次会议|本章节|本段|会议主要|本章节主要|本次讨论)")
SELF_STATEMENT_RE = re.compile(
    r"(我叫|我是|我的|我现在|我一开始|我准备|我希望|我认为|我担心|我的需求|我的工作|"
    r"对我来说|适合我|Configured User\s*徐|用户本人.*(需求|偏好|原则|目标|工作|项目|调整|查看|负责|跟进|推进|确认)|"
    r"用户本人.*(需求|偏好|原则|目标|工作|项目|负责|跟进|推进|确认))",
    re.I,
)
CURRENT_PROJECT_RE = re.compile(
    r"(永生|赛博永生|记忆库|digital soul|Mac mini|Codex|Claude Code|中台|项目A|"
    r"某区域电商|外部客户A|客户|合同|报价|交付|飞书机器人)",
    re.I,
)
COMPANY_CONTEXT_RE = re.compile(
    r"(协作账号|协作账号|账号边界A|账号边界A|用户本人AI|主账号|主账号|账号定位|商务|绩效|"
    r"招聘|写作工作流|标题 Skill|内容SOP|私域|公众号|小红书|商业化|业务|产品|收入|团队|公司)",
    re.I,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Distill Feishu clean candidates into structured memory candidates")
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--include-low", action="store_true", help="include low-priority clean candidates")
    parser.add_argument("--include-secrets", action="store_true", help="write secret-looking memories into main output")
    parser.add_argument("--max-lines-per-doc", type=int, default=12)
    parser.add_argument("--max-memories", type=int, default=0, help="0 means no limit")
    return parser.parse_args()


def stable_id(*parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def redact(text: str) -> str:
    text = SECRET_RE.sub("[SECRET]", text)
    text = re.sub(r"\bcli_[A-Za-z0-9_\-]{8,}\b", "cli_[REDACTED]", text)
    return text


def clean_markup(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    text = re.sub(r"<mention-user[^>]*/>", "", text)
    text = re.sub(r"<image[^>]*/>", "[image]", text)
    text = re.sub(r"<whiteboard[^>]*/>", "[whiteboard]", text)
    text = re.sub(r"<add-ons[^>]*>.*?</add-ons>", "", text, flags=re.S)
    text = TAG_RE.sub(" ", text)
    text = URL_RE.sub("[URL]", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return redact(text).strip()


def owner_alias_pattern() -> re.Pattern:
    parts = [re.escape(alias) for alias in OWNER_ALIASES if alias]
    if not parts:
        parts = [r"用户本人", r"用户本人", r"Configured User", r"用户本人"]
    return re.compile("|".join(parts), re.I)


OWNER_ALIAS_DYNAMIC_RE = owner_alias_pattern()


def is_owner_actor(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if OWNER_FEISHU_OPEN_ID and OWNER_FEISHU_OPEN_ID in text:
        return True
    return bool(OWNER_ALIAS_DYNAMIC_RE.search(text))


def statement_needle(statement: str) -> str:
    text = re.sub(r"\s+", " ", str(statement or "")).strip()
    text = re.sub(r"^[\d一二三四五六七八九十]+[\.、）)]\s*", "", text)
    return text[:80]


def owner_marker_near_statement(raw_text: str, statement: str, *, window: int = 320) -> bool:
    if not raw_text or not statement:
        return False
    needle = statement_needle(statement)
    if len(needle) < 12:
        return False
    raw = str(raw_text)
    pos = raw.find(needle)
    if pos < 0:
        shorter = needle[:36]
        pos = raw.find(shorter) if len(shorter) >= 12 else -1
    if pos < 0:
        return False
    before = raw[max(0, pos - window):pos]
    owner_markers = []
    if OWNER_FEISHU_OPEN_ID:
        owner_markers.append(re.escape(OWNER_FEISHU_OPEN_ID))
    owner_markers.extend(re.escape(alias) for alias in OWNER_ALIASES if alias)
    if not owner_markers:
        return False
    owner_re = re.compile("|".join(owner_markers), re.I)
    speaker_re = re.compile(r"(说话人\s*\d+|协作者D|协作者E|协作者F|协作者G|协作者B|协作者B|错误账号|协作者I|协作者J|协作者K|外部客户A|协作者C|协作者C)")
    owner_hits = list(owner_re.finditer(before))
    if not owner_hits:
        return False
    last_owner = owner_hits[-1].start()
    later_speaker = [match for match in speaker_re.finditer(before) if match.start() > last_owner]
    return not later_speaker


def infer_attribution(candidate: dict[str, Any], record: dict[str, Any] | None, statement: str) -> dict[str, Any]:
    record = record or {}
    actor = str(record.get("actor") or "").strip()
    sender = str(record.get("sender") or "").strip()
    source = str(candidate.get("source") or record.get("source") or "").strip()
    raw_text = str(record.get("text") or candidate.get("evidence") or "")
    title = str(candidate.get("title") or record.get("title") or "")
    owner_actor = is_owner_actor(actor) or is_owner_actor(sender)
    owner_marker = owner_marker_near_statement(raw_text, statement)
    first_person = bool(FIRST_PERSON_VIEW_RE.search(statement or ""))
    user_reported = bool(USER_ATTRIBUTION_RE.search(statement or "") or USER_DURABLE_REMINDER_RE.search(statement or ""))
    user_mentioned = bool(USER_ALIAS_RE.search(statement or "") or OWNER_ALIAS_DYNAMIC_RE.search(statement or ""))

    if source in {"feishu-im", "feishu-im-search"} and (actor or sender):
        if owner_actor:
            category = "self_direct"
            reason = "feishu_message_actor_is_owner"
        elif first_person:
            category = "other_first_person"
            reason = "feishu_message_actor_is_not_owner"
        elif user_mentioned:
            category = "about_owner_from_other"
            reason = "feishu_message_mentions_owner_but_actor_is_not_owner"
        else:
            category = "other_speaker"
            reason = "feishu_message_actor_is_not_owner"
    elif owner_marker:
        category = "self_direct"
        reason = "owner_marker_near_statement"
    elif user_reported:
        category = "owner_reported"
        reason = "statement_reports_owner_view"
    elif first_person:
        category = "unknown_first_person"
        reason = "first_person_without_owner_marker"
    elif user_mentioned:
        category = "about_owner"
        reason = "mentions_owner_without_direct_speech"
    else:
        category = "unknown"
        reason = "no_owner_speaker_evidence"

    return {
        "category": category,
        "reason": reason,
        "actor": actor,
        "sender": sender,
        "source": source,
        "title": title,
    }


def compact(text: str, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def sensitivity(title: str, text: str) -> str:
    haystack = f"{title}\n{text}"
    if SECRET_RE.search(haystack) or re.search(r"(?i)(apikey|api key|secret|token|密码|密钥|凭证)", haystack):
        return "secret"
    if re.search(r"(合同|报价|付款|打款|客户|发票|法务|绩效|薪资|招聘|商业化)", haystack):
        return "confidential"
    return "internal"


def find_projects(text: str) -> list[str]:
    found = []
    lower = text.lower()
    for project, terms in PROJECT_TERMS.items():
        for term in terms:
            if term.lower() in lower:
                found.append(project)
                break
    return found or ["general"]


def find_people(text: str) -> list[str]:
    found = []
    has_mama_role = bool(MAMA_ROLE_RE.search(text))
    for name in PEOPLE_TERMS:
        if name in {"协作账号", "协作账号"} and has_mama_role and name not in text.replace("同事代理账号", ""):
            continue
        if name in text:
            found.append(name)
    return found[:10]


def has_commitment_signal(text: str) -> bool:
    if re.search(r"(\[ \]|待办|todo|下周|本周|明天|后续|尽快|截止|deadline)", text, re.I):
        return True
    if re.search(r"(跟进|推进|配合|负责|完成|提交|交付|对齐|整理|提供|拉建|注册|宣贯)", text) and re.search(
        r"(用户本人|用户本人|协作账号|协作者L|协作者D|协作者E|协作者F|协作者G|协作者B|客户|外部客户A|本周|下周|明天|后续)",
        text,
    ):
        return True
    return False


def classify_type(text: str, source: str, bucket: str) -> str:
    if source == "feishu-calendar-event":
        return "timeline_event"
    if source == "feishu-vc":
        return "meeting_index"
    if source == "feishu-task":
        return "commitment"
    if any(term in text for term in DECISION_TERMS):
        return "decision"
    if any(term in text for term in PREFERENCE_TERMS):
        return "preference"
    if any(term in text for term in LESSON_TERMS):
        return "lesson"
    if any(term in text for term in RELATION_TERMS):
        return "relationship"
    if has_commitment_signal(text):
        return "commitment"
    if bucket in {"primary_docs", "chat_signal"}:
        return "project_fact"
    return "timeline_event"


def is_generic_doc(title: str) -> bool:
    if GENERIC_DOC_TITLE_RE.search(title or ""):
        return True
    return False


def is_core_doc(title: str) -> bool:
    return bool(CORE_DOC_TITLE_RE.search(title or ""))


def is_meeting_doc(title: str) -> bool:
    return bool(MEETING_TITLE_RE.search(title or ""))


def is_raw_transcript(title: str) -> bool:
    title = title or ""
    return bool(RAW_TRANSCRIPT_TITLE_RE.search(title)) and not bool(STRUCTURED_SUMMARY_TITLE_RE.search(title))


def is_structured_summary(title: str) -> bool:
    return bool(STRUCTURED_SUMMARY_TITLE_RE.search(title or ""))


def is_actionable_line(text: str) -> bool:
    if any(term in text for term in DECISION_TERMS + PREFERENCE_TERMS + COMMITMENT_TERMS + LESSON_TERMS + RELATION_TERMS):
        return True
    return False


def statement_quality(statement: str) -> str:
    text = compact(statement, 240)
    if len(text) < 14:
        return "fragment"
    stripped = text.strip(" -*#\t")
    if stripped.endswith(("：", ":")) and len(stripped) < 80:
        return "generic_or_index"
    if re.search(r"(你现在能看到吗|你可以滑动|能听到吗|听得到吗|没 get 到|如果没录|一结束那个都|辛苦|哈哈|嗯嗯|对对对)", text):
        return "transcript_chatter"
    if re.search(r"(本文档包含|纳入候选记忆索引|总目录|目录|点击|上传|复制|粘贴|打开|选择|授权|登录|注册|安装)", text):
        return "generic_or_index"
    if re.search(r"(按我的要求进行修改|优化后文案|正式版|初稿|改稿)", text):
        return "generic_or_index"
    if re.search(r"(不好意思，我刚没听到|好好好|你先讲|我先听着|你说一下|稍等啊)", text):
        return "transcript_chatter"
    if re.match(r"^(对|然后|这个|那个|就是|所以|呃|嗯|啊|那)\b", text):
        return "fragment"
    if len(re.sub(r"[，。！？；、,.!?; ]", "", text)) < 10:
        return "fragment"
    return "ok"


def relevant_to_profile_or_projects(title: str, text: str, bucket: str) -> bool:
    if bucket in {"tasks", "chat_signal", "timeline", "meeting_index"}:
        return True
    if is_generic_doc(title):
        return False
    haystack = f"{title}\n{text}"
    if is_meeting_doc(title):
        return is_actionable_line(text)
    if is_core_doc(title):
        return True
    if not any(term.lower() in haystack.lower() for term in STRONG_PROFILE_TERMS):
        return False
    return True


def split_candidate_lines(text: str) -> list[str]:
    cleaned = clean_markup(text)
    raw_lines = []
    for raw in cleaned.splitlines():
        line = raw.strip(" -*#\t")
        if not line:
            continue
        if line.lower().startswith(("title:", "url:", "record=", "智能纪要由 ai 生成")):
            continue
        if len(line) < 12:
            continue
        if len(line) > 260:
            parts = re.split(r"(?<=[。！？；.!?])\s*", line)
            raw_lines.extend(part.strip(" -*#\t") for part in parts if part.strip())
        else:
            raw_lines.append(line)
    lines = []
    for line in raw_lines:
        if len(line) < 12:
            continue
        if line.count("[image]") >= 2:
            continue
        if re.match(r"^\d+[\.)、]?\s*(上传|点击|选择|打开|授权|登录|注册|进入|复制|粘贴|替换|配置)", line):
            continue
        if re.search(r"(今日核心事件|开源模型激战|泄露后续|欢迎探索|中奖用户|参与资格|活动奖品|news\.|pconline|the hacker news)", line, re.I):
            continue
        lines.append(compact(line, 700))
    return lines


def memory_relevance(candidate: dict[str, Any], statement: str, memory_type: str) -> float:
    title = candidate.get("title") or ""
    bucket = candidate.get("review_bucket") or ""
    haystack = f"{title}\n{statement}"
    score = 0.15
    if bucket == "tasks":
        score += 0.35
    elif bucket == "chat_signal":
        score += 0.25
    elif bucket == "primary_docs":
        score += 0.18
    elif bucket in {"timeline", "meeting_index"}:
        score += 0.1
    if is_core_doc(title):
        score += 0.18
    if is_generic_doc(title):
        score -= 0.35
    if any(term.lower() in haystack.lower() for term in STRONG_PROFILE_TERMS):
        score += 0.2
    if find_people(haystack):
        score += 0.08
    if memory_type in {"decision", "preference", "relationship", "lesson"}:
        score += 0.12
    elif memory_type == "commitment":
        score += 0.08
    if has_commitment_signal(haystack) or any(term in haystack for term in DECISION_TERMS + PREFERENCE_TERMS):
        score += 0.1
    quality = statement_quality(statement)
    if quality != "ok":
        score -= 0.22
    if is_meeting_doc(title) and quality != "ok":
        score -= 0.15
    if is_raw_transcript(title):
        score -= 0.12
    if re.search(r"(说话人\s*\d|Q\d|^\d+[\.)、]?\s|^A:|^Q:)", statement):
        score -= 0.08
    return round(max(0.0, min(1.0, score)), 3)


def memory_focus(candidate: dict[str, Any], statement: str, attribution: dict[str, Any] | None = None) -> str:
    title = candidate.get("title") or ""
    title_and_statement = f"{title}\n{statement}"
    if profile_review_exclusion_reason_from_parts(
        title,
        statement,
        find_projects(title_and_statement),
        find_people(title_and_statement),
        attribution=attribution,
    ):
        return "reference_material"
    attribution_category = (attribution or {}).get("category", "")
    if attribution_category == "self_direct":
        return "self_profile"
    if CURRENT_PROJECT_RE.search(title_and_statement):
        return "current_project"
    if COMPANY_CONTEXT_RE.search(title_and_statement):
        return "company_context"
    return "reference_material"


def text_bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\W+", "", str(text or "").lower())
    if len(normalized) < 2:
        return set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def text_similarity(a: str, b: str) -> float:
    a_set = text_bigrams(a)
    b_set = text_bigrams(b)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / max(1, min(len(a_set), len(b_set)))


def profile_review_exclusion_reason_from_parts(
    title: str,
    statement: str,
    projects: list[str],
    people: list[str],
    attribution: dict[str, Any] | None = None,
) -> str:
    haystack = f"{title}\n{statement}"
    has_user = bool(USER_ALIAS_RE.search(haystack))
    attribution_category = (attribution or {}).get("category", "")
    if attribution_category in {"other_speaker", "other_first_person", "unknown_first_person"}:
        return attribution_category
    user_perspective = is_user_perspective_statement(title, statement, attribution=attribution)
    if MEETING_INTRO_SUMMARY_RE.search(statement or "") and not user_perspective:
        return "meeting_intro_summary_not_profile"
    if EXTERNAL_FACT_RE.search(statement or "") and not user_perspective:
        return "external_fact_not_user_profile"
    if ONE_OFF_COORDINATION_RE.search(statement or "") and not user_perspective:
        return "one_off_coordination_not_profile"
    if BIBI_ACCOUNT_TITLE_RE.search(title or "") and not user_perspective:
        return "partner_account_specific"
    if BIBI_ACCOUNT_STATEMENT_RE.search(statement or "") and not user_perspective:
        return "partner_account_specific"
    if ("partner_brand" in projects or "协作账号" in people) and not user_perspective:
        return "partner_account_specific"
    if THIRD_PARTY_ONLY_RE.search(statement or "") and not user_perspective:
        return "third_party_view_not_about_user"
    if CANDIDATE_RESUME_RE.search(haystack) and not user_perspective:
        return "candidate_resume_or_one_off_person"
    if INSTRUCTIONAL_TOOL_RE.search(haystack) and not user_perspective:
        return "tool_instruction_or_generic_material"
    return ""


def is_user_perspective_statement(
    title: str,
    statement: str,
    attribution: dict[str, Any] | None = None,
) -> bool:
    """Return True when the statement records Configured User's own view, words, or durable working rule.

    Source titles are weak evidence. A document about 协作账号 can still contain
    first-person writing from 协作账号, while a generic report can mention 协作账号
    without being Configured User's perspective. Require explicit Configured User attribution for
    协作账号 account materials.
    """
    attribution_category = (attribution or {}).get("category", "")
    if attribution_category == "self_direct":
        return True
    if attribution_category == "owner_reported":
        return True
    if attribution_category in {"other_speaker", "other_first_person", "unknown_first_person"}:
        return False
    haystack = f"{title}\n{statement}"
    if EXTERNAL_FACT_RE.search(statement or ""):
        return False
    if ONE_OFF_COORDINATION_RE.search(statement or ""):
        return False
    if BIBI_ACCOUNT_TITLE_RE.search(title or "") and not USER_ALIAS_RE.search(statement or ""):
        return False
    if USER_ATTRIBUTION_RE.search(statement or ""):
        return True
    if USER_DURABLE_REMINDER_RE.search(statement or ""):
        return True
    if FIRST_PERSON_VIEW_RE.search(statement or "") and USER_PERSPECTIVE_TOPIC_RE.search(haystack):
        return True
    return False


def profile_review_exclusion_reason(memory: dict[str, Any]) -> str:
    source = memory.get("source", {}) if isinstance(memory.get("source"), dict) else {}
    title = source.get("title", "")
    return profile_review_exclusion_reason_from_parts(
        title,
        memory.get("statement", ""),
        memory.get("projects") or [],
        memory.get("people") or [],
        attribution=memory.get("attribution") if isinstance(memory.get("attribution"), dict) else None,
    )


def base_confidence(candidate: dict[str, Any], memory_type: str, sens: str) -> float:
    source = candidate.get("source", "")
    title = candidate.get("title") or ""
    bucket = candidate.get("review_bucket", "")
    priority = candidate.get("distill_priority", "")
    score = 0.45
    if priority == "high":
        score += 0.22
    elif priority == "medium":
        score += 0.1
    if bucket == "primary_docs":
        score += 0.12
    elif bucket == "tasks":
        score += 0.08
    elif bucket == "chat_signal":
        score += 0.04
    if source == "feishu-vc":
        score -= 0.08
    if is_raw_transcript(title):
        score -= 0.08
    elif is_structured_summary(title):
        score += 0.04
    if memory_type in {"decision", "preference", "commitment"}:
        score += 0.04
    if sens == "secret":
        score -= 0.15
    return round(max(0.05, min(0.95, score)), 3)


def make_memory(
    candidate: dict[str, Any],
    record: dict[str, Any] | None,
    statement: str,
    memory_type: str,
    *,
    status: str = "active",
) -> dict[str, Any]:
    title = candidate.get("title") or (record or {}).get("title") or ""
    source_text = f"{title}\n{statement}"
    sens = sensitivity(title, statement)
    projects = find_projects(source_text)
    people = find_people(source_text)
    clean_id = candidate.get("clean_id", "")
    raw_id = candidate.get("raw_id", "")
    source_url = (record or {}).get("url", "")
    local_date = candidate.get("local_date") or (record or {}).get("local_date") or ""
    evidence = compact(statement, 520)
    memory_type = memory_type
    relevance = memory_relevance(candidate, evidence, memory_type)
    attribution = infer_attribution(candidate, record, evidence)
    focus = memory_focus(candidate, evidence, attribution)
    confidence = base_confidence(candidate, memory_type, sens)
    confidence = round(max(0.05, min(0.95, confidence - max(0.0, 0.5 - relevance) * 0.4)), 3)
    return {
        "memory_id": stable_id(memory_type, title, evidence, local_date, clean_id),
        "review_state": "candidate",
        "memory_type": memory_type,
        "scope": "feishu",
        "projects": projects,
        "people": people,
        "statement": evidence,
        "status": status,
        "focus": focus,
        "confidence": confidence,
        "relevance_score": relevance,
        "statement_quality": statement_quality(evidence),
        "sensitivity": sens,
        "attribution": attribution,
        "valid_from": local_date,
        "source": {
            "candidate_id": candidate.get("candidate_id", ""),
            "clean_id": clean_id,
            "raw_id": raw_id,
            "source": candidate.get("source", ""),
            "review_bucket": candidate.get("review_bucket", ""),
            "distill_priority": candidate.get("distill_priority", ""),
            "title": title,
            "url": source_url,
        },
        "evidence": evidence,
    }


def source_snapshot(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": source.get("candidate_id", ""),
        "clean_id": source.get("clean_id", ""),
        "raw_id": source.get("raw_id", ""),
        "source": source.get("source", ""),
        "review_bucket": source.get("review_bucket", ""),
        "distill_priority": source.get("distill_priority", ""),
        "title": source.get("title", ""),
        "url": source.get("url", ""),
    }


def extract_task_memory(candidate: dict[str, Any], record: dict[str, Any] | None) -> list[dict[str, Any]]:
    title = (candidate.get("title") or "").strip()
    if not title or JUNK_TASK_RE.match(title):
        return []
    evidence = clean_markup(candidate.get("evidence") or "")
    complete = "complete: True" in evidence
    due_match = re.search(r"due_at:[ \t]*([^\n\r ]+)", evidence)
    due = due_match.group(1) if due_match else ""
    status = "done" if complete else "open"
    statement = f"任务：{title}"
    if due:
        statement += f"；截止：{due}"
    statement += f"；状态：{'已完成' if complete else '未完成'}"
    memory = make_memory(candidate, record, statement, "commitment", status=status)
    task_id = candidate.get("object_id") or (record or {}).get("object_id") or (record or {}).get("container_id") or ""
    if not task_id:
        task_id = re.sub(r"\W+", "", title.lower())
    memory["task_key"] = str(task_id)
    memory["task_complete"] = complete
    memory["task_due"] = due
    return [memory]


def extract_timeline_memory(candidate: dict[str, Any], record: dict[str, Any] | None) -> list[dict[str, Any]]:
    title = candidate.get("title") or ""
    if len(title.strip()) < 4:
        return []
    source = candidate.get("source")
    if source == "feishu-vc":
        statement = f"会议记录：{compact(title, 220)}"
        memory_type = "meeting_index"
    else:
        statement = f"日程：{compact(title, 220)}"
        memory_type = "timeline_event"
    return [make_memory(candidate, record, statement, memory_type)]


def extract_doc_or_chat_memories(
    candidate: dict[str, Any],
    record: dict[str, Any] | None,
    *,
    max_lines: int,
) -> list[dict[str, Any]]:
    title = candidate.get("title") or ""
    text = (record or {}).get("text") or candidate.get("evidence") or ""
    if not relevant_to_profile_or_projects(title, text, candidate.get("review_bucket", "")):
        return []
    lines = split_candidate_lines(text)
    memories = []
    for line in lines:
        haystack = f"{title}\n{line}"
        if not relevant_to_profile_or_projects(title, haystack, candidate.get("review_bucket", "")):
            continue
        if candidate.get("review_bucket") == "primary_docs" and not is_actionable_line(haystack):
            continue
        memory_type = classify_type(haystack, candidate.get("source", ""), candidate.get("review_bucket", ""))
        if memory_type == "timeline_event" and candidate.get("review_bucket") == "primary_docs":
            memory_type = "project_fact"
        memory = make_memory(candidate, record, line, memory_type)
        if memory["relevance_score"] < 0.45:
            continue
        memories.append(memory)
        if len(memories) >= max_lines:
            break
    if not memories and candidate.get("review_bucket") == "primary_docs":
        summary = f"飞书文档《{title}》是一个可复用项目/知识素材，已纳入候选记忆索引。"
        memories.append(make_memory(candidate, record, summary, "project_fact"))
    return memories


def dedupe_memories(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for memory in memories:
        norm = re.sub(r"\W+", "", memory["statement"].lower())[:160]
        key = f"{memory['memory_type']}|{norm}"
        existing = by_key.get(key)
        if not existing:
            by_key[key] = memory
            continue
        if memory["confidence"] > existing["confidence"]:
            memory["source"]["also_seen_in"] = [source_snapshot(existing["source"])]
            by_key[key] = memory
        else:
            existing.setdefault("also_seen_count", 1)
            existing["also_seen_count"] += 1
    return sorted(
        by_key.values(),
        key=lambda item: (
            {"self_profile": 0, "current_project": 1, "company_context": 2, "reference_material": 3}.get(item.get("focus"), 9),
            -item.get("relevance_score", 0),
            -item["confidence"],
            item["memory_type"],
            item["valid_from"],
            item["statement"],
        ),
    )


def dedupe_task_memories(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    others: list[dict[str, Any]] = []
    for memory in memories:
        if memory.get("memory_type") != "commitment" or "task_key" not in memory:
            others.append(memory)
            continue
        key = str(memory.get("task_key") or memory["statement"])
        existing = tasks.get(key)
        if not existing:
            memory["status_history"] = [
                {
                    "status": memory.get("status"),
                    "complete": memory.get("task_complete"),
                    "valid_from": memory.get("valid_from"),
                    "source": source_snapshot(memory.get("source", {})),
                }
            ]
            tasks[key] = memory
            continue
        existing.setdefault("status_history", []).append(
            {
                "status": memory.get("status"),
                "complete": memory.get("task_complete"),
                "valid_from": memory.get("valid_from"),
                "source": source_snapshot(memory.get("source", {})),
            }
        )
        existing.setdefault("also_seen_count", 1)
        existing["also_seen_count"] += 1
        existing.setdefault("source", {}).setdefault("also_seen_in", []).append(source_snapshot(memory.get("source", {})))
        replace = False
        if memory.get("task_complete") and not existing.get("task_complete"):
            replace = True
        elif memory.get("task_complete") == existing.get("task_complete"):
            replace = (memory.get("valid_from") or "") >= (existing.get("valid_from") or "")
        if replace:
            history = existing.get("status_history", [])
            also_seen_count = existing.get("also_seen_count")
            also_seen = existing.get("source", {}).get("also_seen_in", [])
            memory["status_history"] = history
            memory["also_seen_count"] = also_seen_count
            memory.setdefault("source", {})["also_seen_in"] = [source_snapshot(source) for source in also_seen]
            tasks[key] = memory
    return others + list(tasks.values())


def load_relevant_records(records_path: Path, clean_ids: set[str]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not records_path.exists() or not clean_ids:
        return records
    with records_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            clean_id = record.get("clean_id")
            if clean_id in clean_ids:
                records[clean_id] = record
                if len(records) >= len(clean_ids):
                    break
    return records


def render_profile_delta(memories: list[dict[str, Any]]) -> str:
    groups = defaultdict(list)
    for memory in memories:
        if memory["confidence"] < 0.72:
            continue
        if memory["sensitivity"] == "secret":
            continue
        if memory.get("statement_quality") != "ok":
            continue
        if memory.get("focus") == "reference_material":
            continue
        if memory["memory_type"] not in {"decision", "preference", "commitment", "project_fact", "lesson", "relationship"}:
            continue
        groups[(memory.get("focus", "other"), memory["memory_type"])].append(memory)

    titles = {
        "decision": "决策",
        "preference": "偏好与原则",
        "commitment": "承诺与待办",
        "project_fact": "项目事实",
        "lesson": "经验教训",
        "relationship": "关系与职责",
    }
    lines = [
        "# Feishu Profile Delta v1",
        "",
        "这些是从飞书 clean layer 生成的待审候选，不会自动写入 digital-soul.md。",
        "",
    ]
    focus_titles = {
        "self_profile": "个人画像",
        "current_project": "当前项目",
        "company_context": "公司上下文",
        "other": "其他",
    }
    for focus in ["self_profile", "current_project", "company_context", "other"]:
        wrote_focus = False
        for memory_type in ["decision", "preference", "commitment", "project_fact", "lesson", "relationship"]:
            rows = groups.get((focus, memory_type), [])[:30]
            if not rows:
                continue
            if not wrote_focus:
                lines.append(f"## {focus_titles.get(focus, focus)}")
                lines.append("")
                wrote_focus = True
            lines.append(f"### {titles[memory_type]}")
            lines.append("")
            for row in rows:
                projects = ",".join(row["projects"])
                source = row["source"]["title"]
                lines.append(f"- {row['statement']}  ")
                lines.append(f"  - evidence: {source} / {row['valid_from']} / {projects}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def is_profile_memory(memory: dict[str, Any]) -> bool:
    if memory.get("sensitivity") == "secret":
        return False
    if memory.get("statement_quality") != "ok":
        return False
    if memory.get("focus") == "reference_material":
        return False
    if memory.get("relevance_score", 0) < 0.45:
        return False
    return memory.get("memory_type") in {"decision", "preference", "commitment", "project_fact", "lesson", "relationship"}


def is_profile_review_memory(memory: dict[str, Any]) -> bool:
    if not is_profile_memory(memory):
        return False
    exclusion = profile_review_exclusion_reason(memory)
    if exclusion:
        memory["profile_review_exclusion"] = exclusion
        return False
    title = memory.get("source", {}).get("title", "")
    attribution = memory.get("attribution") if isinstance(memory.get("attribution"), dict) else {}
    attribution_category = attribution.get("category", "")
    if is_raw_transcript(title) or "文字记录" in title:
        if attribution_category != "self_direct":
            memory["profile_review_exclusion"] = "raw_transcript_without_owner_direct_attribution"
            return False
    user_perspective = is_user_perspective_statement(title, memory.get("statement", ""), attribution=attribution)
    if memory.get("focus") == "self_profile":
        if attribution_category != "self_direct":
            memory["profile_review_exclusion"] = "self_profile_requires_owner_direct_attribution"
            return False
        return memory.get("confidence", 0) >= 0.78 and memory.get("memory_type") in {
            "decision",
            "preference",
            "project_fact",
            "lesson",
            "relationship",
        }
    if memory.get("focus") in {"current_project", "company_context"}:
        if not user_perspective:
            memory["profile_review_exclusion"] = "project_or_account_fact_without_user_perspective"
            return False
        return memory.get("confidence", 0) >= 0.82 and memory.get("memory_type") in {
            "decision",
            "preference",
            "lesson",
            "project_fact",
        }
    return False


def choose_better_review_memory(current: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    current_score = (
        current.get("relevance_score", 0),
        current.get("confidence", 0),
        1 if USER_ALIAS_RE.search(current.get("statement", "")) else 0,
        len(current.get("statement", "")),
    )
    existing_score = (
        existing.get("relevance_score", 0),
        existing.get("confidence", 0),
        1 if USER_ALIAS_RE.search(existing.get("statement", "")) else 0,
        len(existing.get("statement", "")),
    )
    if current_score > existing_score:
        current.setdefault("also_seen_count", existing.get("also_seen_count", 1))
        current["also_seen_count"] = int(current.get("also_seen_count") or 1) + 1
        current.setdefault("source", {}).setdefault("also_seen_in", []).append(source_snapshot(existing.get("source", {})))
        return current
    existing.setdefault("also_seen_count", 1)
    existing["also_seen_count"] += 1
    existing.setdefault("source", {}).setdefault("also_seen_in", []).append(source_snapshot(current.get("source", {})))
    return existing


def review_cluster_key(memory: dict[str, Any]) -> str:
    statement = memory.get("statement", "")
    statement_lower = statement.lower()
    if "知识付费" in statement and "tob" in statement_lower and "交付" in statement:
        return "strategy:not_knowledge_paid_or_tob_delivery"
    if "写稿中台" in statement and "优先" in statement:
        return "strategy:writing_center_first"
    if "二级代理" in statement or "T1" in statement:
        return "strategy:feishu_t1_proxy_first"
    if "第一个付费客户" in statement:
        return "strategy:first_paying_customer_first"
    if "SaaS" in statement and "营销" in statement:
        return "strategy:sales_saas_risk"
    if "点数收费" in statement:
        return "strategy:point_based_pricing"
    if "商务流程" in statement and "收口" in statement:
        return "strategy:business_process_funnel"
    if "Mac mini" in statement and "沉淀对话语料" in statement:
        return "strategy:mac_mini_single_agent_memory"
    norm = re.sub(r"\W+", "", statement.lower())[:90]
    return f"{memory.get('focus')}|{memory.get('memory_type')}|{norm}"


def filter_profile_review_rows(memories: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    counters: Counter[str] = Counter()
    clustered: dict[str, dict[str, Any]] = {}
    for memory in memories:
        if not is_profile_review_memory(memory):
            reason = memory.get("profile_review_exclusion") or "quality_or_threshold"
            counters["profile_review_excluded:" + reason] += 1
            continue
        key = review_cluster_key(memory)
        existing = clustered.get(key)
        if existing:
            clustered[key] = choose_better_review_memory(memory, existing)
            counters["profile_review_deduped"] += 1
        else:
            clustered[key] = memory
    return list(clustered.values()), counters


def render_profile_merge_proposal(memories: list[dict[str, Any]], limit: int = 80) -> str:
    rows, _ = filter_profile_review_rows(memories)
    rows = sorted(
        rows,
        key=lambda item: (
            {"self_profile": 0, "current_project": 1, "company_context": 2}.get(item.get("focus"), 9),
            {"preference": 0, "decision": 1, "lesson": 2, "relationship": 3, "project_fact": 4}.get(
                item.get("memory_type"), 9
            ),
            -item.get("confidence", 0),
            -item.get("relevance_score", 0),
            item.get("statement", ""),
        ),
    )[:limit]
    lines = [
        "# Feishu Profile Merge Proposal v1",
        "",
        "这是进入长期画像前的自动审阅候选稿。它不会自动写入 digital-soul.md。",
        "",
        "默认由 profile-auto-review 自动过滤并合并；人工审计只用于覆盖修正。",
        "",
    ]
    focus_titles = {
        "self_profile": "个人长期画像候选",
        "current_project": "当前项目长期记忆候选",
        "company_context": "公司与内容业务候选",
    }
    type_titles = {
        "decision": "决策",
        "preference": "偏好与原则",
        "project_fact": "项目事实",
        "lesson": "经验教训",
        "relationship": "关系与职责",
    }
    for focus in ["self_profile", "current_project", "company_context"]:
        focus_rows = [row for row in rows if row.get("focus") == focus]
        if not focus_rows:
            continue
        lines.append(f"## {focus_titles.get(focus, focus)}")
        lines.append("")
        for memory_type in ["preference", "decision", "lesson", "relationship", "project_fact"]:
            type_rows = [row for row in focus_rows if row.get("memory_type") == memory_type]
            if not type_rows:
                continue
            lines.append(f"### {type_titles[memory_type]}")
            lines.append("")
            for row in type_rows[:20]:
                source = row["source"].get("title", "")
                lines.append(f"- [ ] `{row['memory_id']}` {row['statement']}")
                lines.append(
                    f"  - confidence: {row['confidence']} / relevance: {row['relevance_score']} / source: {source} / {row['valid_from']}"
                )
            lines.append("")
    if len(lines) <= 7:
        lines.append("暂无达到质量门槛的合并候选。")
    return "\n".join(lines).rstrip() + "\n"


def render_timeline(memories: list[dict[str, Any]]) -> str:
    rows = [m for m in memories if m["memory_type"] in {"timeline_event", "meeting_index"} and m["sensitivity"] != "secret"]
    rows = sorted(rows, key=lambda item: (item.get("valid_from") or "", item["statement"]))
    lines = ["# Feishu Timeline v1", "", "从日程和会议索引生成的待审时间线。", ""]
    current = ""
    for row in rows:
        date = row.get("valid_from") or "unknown"
        if date != current:
            current = date
            lines.append(f"## {date}")
            lines.append("")
        lines.append(f"- {row['statement']}")
    return "\n".join(lines).rstrip() + "\n"


def render_report(coverage: dict[str, Any], paths: dict[str, Path]) -> str:
    counters = coverage["counters"]
    def section(prefix: str) -> str:
        rows = []
        for key, value in counters.items():
            if key.startswith(prefix):
                rows.append(f"- `{key.removeprefix(prefix)}`: {value}")
        return "\n".join(rows) if rows else "- none"

    return f"""# Feishu Distill Layer v1 Report

Generated: {coverage["generated_at"]}

## Outputs

- Memories: `{paths["memories"]}`
- Profile memories: `{paths["profile_memories"]}`
- Reference memories: `{paths["reference_memories"]}`
- Profile delta: `{paths["profile_delta"]}`
- Profile merge proposal: `{paths["profile_merge_proposal"]}`
- Timeline: `{paths["timeline"]}`
- Coverage: `{paths["coverage"]}`

## Summary

- Candidate rows scanned: {counters.get("candidates_scanned", 0)}
- Candidate rows used: {counters.get("candidates_used", 0)}
- Structured memories written: {counters.get("memories_written", 0)}
- Profile memories written: {counters.get("profile_memories_written", 0)}
- Reference memories written: {counters.get("reference_memories_written", 0)}
- Secret-looking memories skipped: {counters.get("secret_memories_skipped", 0)}
- Duplicate memories merged/skipped: {counters.get("duplicates_skipped", 0)}
- Profile review candidates deduped: {counters.get("profile_review_deduped", 0)}

## Memory Types

{section("memory_type:")}

## Sensitivity

{section("sensitivity:")}

## Focus

{section("focus:")}

## Projects

{section("project:")}

## Profile Review Filters

{section("profile_review_excluded:")}

## Notes

- This layer is review-only.
- It does not write `digital-soul.md`.
- Secret-looking content is redacted, marked `secret`, and skipped from main output unless `--include-secrets` is set.
- The profile merge proposal is filtered for 用户本人/用户本人 long-term memory. 协作账号账号资料、旁人观点、候选人履历、工具安装说明和一次性资料会被降到非审阅层。
"""


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)

    priorities = set(DEFAULT_PRIORITIES)
    if args.include_low:
        priorities.add("low")

    candidates_all = load_jsonl(args.candidates)
    candidates = [row for row in candidates_all if row.get("distill_priority") in priorities]
    clean_ids = {row.get("clean_id") for row in candidates if row.get("clean_id")}
    records = load_relevant_records(args.records, clean_ids)

    counters: Counter[str] = Counter()
    raw_memories: list[dict[str, Any]] = []
    for candidate in candidates_all:
        counters["candidates_scanned"] += 1
        if candidate.get("distill_priority") not in priorities:
            counters["candidate_priority_skipped:" + str(candidate.get("distill_priority"))] += 1
            continue
        counters["candidates_used"] += 1
        counters["candidate_bucket:" + str(candidate.get("review_bucket"))] += 1
        record = records.get(candidate.get("clean_id"))
        source = candidate.get("source")
        bucket = candidate.get("review_bucket")
        if source == "feishu-task":
            raw_memories.extend(extract_task_memory(candidate, record))
        elif bucket in {"timeline", "meeting_index"}:
            raw_memories.extend(extract_timeline_memory(candidate, record))
        else:
            raw_memories.extend(extract_doc_or_chat_memories(candidate, record, max_lines=args.max_lines_per_doc))

    raw_memories = dedupe_task_memories(raw_memories)
    memories = dedupe_memories(raw_memories)
    duplicates = max(0, len(raw_memories) - len(memories))
    if args.max_memories:
        memories = memories[: args.max_memories]

    memories_path = args.output_dir / "memories.jsonl"
    profile_memories_path = args.output_dir / "profile_memories.jsonl"
    reference_memories_path = args.output_dir / "reference_memories.jsonl"
    profile_delta_path = args.output_dir / "profile_delta.md"
    profile_merge_proposal_path = args.output_dir / "profile_merge_proposal.md"
    timeline_path = args.output_dir / "timeline.md"
    coverage_path = args.output_dir / "coverage.json"

    profile_memories: list[dict[str, Any]] = []
    reference_memories: list[dict[str, Any]] = []
    with memories_path.open("w", encoding="utf-8") as handle:
        for memory in memories:
            if memory["sensitivity"] == "secret" and not args.include_secrets:
                counters["secret_memories_skipped"] += 1
                continue
            handle.write(dump_json(memory) + "\n")
            counters["memories_written"] += 1
            counters["memory_type:" + memory["memory_type"]] += 1
            counters["sensitivity:" + memory["sensitivity"]] += 1
            counters["focus:" + str(memory.get("focus", "unknown"))] += 1
            counters["statement_quality:" + str(memory.get("statement_quality", "unknown"))] += 1
            for project in memory["projects"]:
                counters["project:" + project] += 1
            if is_profile_memory(memory):
                profile_memories.append(memory)
            elif memory.get("focus") == "reference_material":
                reference_memories.append(memory)

    with profile_memories_path.open("w", encoding="utf-8") as handle:
        for memory in profile_memories:
            handle.write(dump_json(memory) + "\n")
            counters["profile_memories_written"] += 1

    with reference_memories_path.open("w", encoding="utf-8") as handle:
        for memory in reference_memories:
            handle.write(dump_json(memory) + "\n")
            counters["reference_memories_written"] += 1

    counters["duplicates_skipped"] = duplicates
    _review_rows, review_filter_counters = filter_profile_review_rows(profile_memories)
    counters.update(review_filter_counters)
    coverage = {
        "generated_at": datetime.now(tz=LOCAL_TZ).isoformat(),
        "candidates": str(args.candidates),
        "records": str(args.records),
        "output_dir": str(args.output_dir),
        "priorities": sorted(priorities),
        "max_lines_per_doc": args.max_lines_per_doc,
        "counters": dict(sorted(counters.items())),
        "notes": [
            "Structured memory candidates are review-only.",
            "digital-soul.md is not modified.",
            "Secret-looking content is redacted, marked secret, and skipped from main output unless --include-secrets is set.",
        ],
    }
    coverage_path.write_text(dump_json(coverage) + "\n", encoding="utf-8")
    profile_delta_path.write_text(render_profile_delta(profile_memories), encoding="utf-8")
    profile_merge_proposal_path.write_text(render_profile_merge_proposal(profile_memories), encoding="utf-8")
    timeline_path.write_text(render_timeline(memories), encoding="utf-8")

    report_path = args.report_dir / f"feishu-distill-{datetime.now(tz=LOCAL_TZ).strftime('%Y%m%d-%H%M%S')}.md"
    report_path.write_text(
        render_report(
            coverage,
            {
                "memories": memories_path,
                "profile_memories": profile_memories_path,
                "reference_memories": reference_memories_path,
                "profile_delta": profile_delta_path,
                "profile_merge_proposal": profile_merge_proposal_path,
                "timeline": timeline_path,
                "coverage": coverage_path,
            },
        ),
        encoding="utf-8",
    )

    print(f"memories={counters['memories_written']}")
    print(f"profile_memories={counters['profile_memories_written']}")
    print(f"reference_memories={counters['reference_memories_written']}")
    print(f"secret_skipped={counters['secret_memories_skipped']}")
    print(f"duplicates_skipped={counters['duplicates_skipped']}")
    print(f"profile_delta={profile_delta_path}")
    print(f"profile_merge_proposal={profile_merge_proposal_path}")
    print(f"timeline={timeline_path}")
    print(f"coverage={coverage_path}")
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
