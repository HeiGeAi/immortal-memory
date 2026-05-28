#!/usr/bin/env python3
"""
永生记忆库 - 数字人格蒸馏器 v0.3
真正从用户的所有真实发言中提取认知模型，而非硬编码模板。

蒸馏策略：
1. 数据清洗：过滤工具结果、系统消息、纯代码等噪音
2. 信号提取：从真实用户发言中识别决策、偏好、判断、知识
3. 聚合归类：按维度聚合并按频次/重要性排序
4. 结构化输出：生成可注入任何 Agent 的人格文件

数据源（按优先级）：
  L3 公理：skill 内置 references/memory/ 或 ~/.codex/memories/ 中已蒸馏的精华（最权威）
  L2 反思：用户对 Agent 的纠正、偏好声明（含信号词）
  L1 观察：用户的所有真实发言（原始素材）
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone


SKILL_DIR = Path(__file__).resolve().parent
MEMORY_DIRS = [
    SKILL_DIR / "references" / "memory",
    Path.home() / ".codex/memories",
    Path.home() / ".claude/projects/-Users-user/memory",
]
INDEX_FILE = Path.home() / ".immortal/index.jsonl"
OUTPUT_FILE = Path.home() / ".immortal/digital-soul.md"


# ============================================================
# 数据清洗
# ============================================================

TOOL_RESULT_PATTERNS = [
    r"^File created successfully",
    r"^File unchanged since",
    r"^The file .+ has been updated",
    r"^Request failed with status",
    r"^The user doesn't want to proceed",
    r"^\d+\s*\t",
    r"^---\s*$",
    r"Result of calling the .+ tool",
    r"^total_tokens:",
    r"^\s*<system-reminder",
    r"^Called the .+ tool",
    r"^Wasted call",
    r"^Output too large",
    r"^✅",
    r"^\[\d+\] ",
]

CODE_INDICATORS = [
    "function ", "const ", "import ", "export ", "class ",
    "def ", "return ", "console.log", "print(",
    "</", "/>", "{{", "}}", "==>",
]

NOISE_INDICATORS = [
    'http://', 'https://',
    '"date":', '"link":', '"title":',
    '\\n', '。”', '。」',
    '收藏 喜欢', '点赞 评论', '赞同 添加评论',
    '阅读量', '阅读：',
    '[Image #', '![',
]


def is_low_quality_signal(s: str) -> bool:
    """判断一个句子是否是低质量信号（引用/噪音）。"""
    for ind in NOISE_INDICATORS:
        if ind in s:
            return True
    # URL 主导
    if s.count("http") >= 1 and len(s) < 150:
        return True
    # 太多引号说明是引用堆叠
    if s.count('"') > 4 or s.count('\\n') > 2:
        return True
    # 含完整网址
    if re.search(r'https?://\S+', s):
        return True
    return False


def is_real_user_message(content: str) -> bool:
    if not content:
        return False
    s = content.strip()
    if len(s) < 10:
        return False

    for pattern in TOOL_RESULT_PATTERNS:
        if re.match(pattern, s):
            return False

    if s.startswith("Called the ") or "tool with the following" in s[:100]:
        return False

    if s.startswith("/Users/") or s.startswith("~/"):
        if "\n" not in s[:200]:
            return False

    code_score = sum(1 for ind in CODE_INDICATORS if ind in s[:500])
    if code_score >= 3 and len(s) < 1000:
        return False

    if s.count("{") > 5 and s.count('"') > 10:
        return False

    return True


# ============================================================
# 信号提取
# ============================================================

CORRECTION_SIGNALS = [
    "不要", "别", "不用", "停止", "改掉", "去掉", "删掉",
    "应该", "必须", "一定要", "记住", "注意",
    "我觉得", "我认为", "我喜欢", "我讨厌", "我反感",
    "更好", "更适合", "比较", "不如",
    "原则", "规则", "标准",
]

PREFERENCE_SIGNALS = [
    "我希望", "我想要", "帮我", "给我", "建议",
    "倾向", "偏好", "习惯",
]

DECISION_SIGNALS = [
    "决定", "选", "用", "采用", "推荐", "建议用",
    "为什么", "因为", "所以",
]

OPINION_SIGNALS = [
    "觉得", "认为", "看", "判断",
    "好", "差", "对", "错", "行", "不行",
    "本质上", "其实", "实际上", "真正",
]


def classify_signal(content: str) -> str:
    """返回信号类型：correction/preference/decision/opinion/none"""
    s = content[:200]
    for sig in CORRECTION_SIGNALS:
        if sig in s:
            return "correction"
    for sig in DECISION_SIGNALS:
        if sig in s:
            return "decision"
    for sig in PREFERENCE_SIGNALS:
        if sig in s:
            return "preference"
    for sig in OPINION_SIGNALS:
        if sig in s:
            return "opinion"
    return "none"


def split_sentences(text: str) -> list:
    """简易中英文分句。"""
    text = re.sub(r'\s+', ' ', text)
    sentences = re.split(r'[。！？\n.!?]\s*', text)
    return [s.strip() for s in sentences if 10 < len(s.strip()) < 250]


def extract_signal_sentences(content: str) -> list:
    """从一段内容中提取带信号的句子。"""
    sentences = split_sentences(content)
    results = []
    for sent in sentences:
        signal = classify_signal(sent)
        if signal != "none":
            results.append((signal, sent))
    return results


# ============================================================
# 主蒸馏流程
# ============================================================

def load_l3_memories() -> dict:
    """加载已蒸馏的记忆文件（L3 公理）。"""
    memories = {}
    for memory_dir in MEMORY_DIRS:
        if not memory_dir.exists():
            continue
        for f in memory_dir.glob("*.md"):
            if f.name in {"MEMORY.md", "memory_summary.md", "raw_memories.md"}:
                continue
            if f.stem in memories:
                continue
            try:
                content = f.read_text(encoding="utf-8")
                # 跳过 frontmatter
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        content = parts[2]
                memories[f.stem] = content.strip()
            except:
                continue
    return memories


def scan_index() -> dict:
    """扫描全量索引，提取信号、统计、关键词。"""
    if not INDEX_FILE.exists():
        return {}

    signals = defaultdict(list)
    project_mentions = Counter()
    people_mentions = Counter()
    tool_mentions = Counter()
    topic_mentions = Counter()

    total_records = 0
    real_user_msgs = 0
    assistant_msgs = 0

    earliest_ts = None
    latest_ts = None

    project_keywords = [
        "项目A", "主账号", "数据大屏", "飞书", "排版工具",
        "账号边界A", "sitec", "siteb", "数据看板", "公众号",
        "自媒体", "OpenClaw", "Claude Code", "Codex",
        "Hermes", "永生记忆库", "immortal", "graphify",
        "OPC", "linux.do",
    ]

    people_pattern = re.compile(r'(鸭哥|协作账号|老胡|半佛|刘润|小蔓|小戈杵|周鸿祎|王路|马斯克|奥特曼|Sam Altman)')

    topic_keywords = [
        "AI", "Agent", "Skill", "MCP", "Cursor", "GitHub",
        "公众号", "文章", "视频", "客户", "项目",
        "蒸馏", "记忆", "上下文", "Context", "embedding",
        "招聘", "助理", "团队",
    ]

    for line in open(INDEX_FILE, encoding="utf-8"):
        try:
            record = json.loads(line.strip())
        except:
            continue

        total_records += 1
        role = record.get("role", "")
        content = record.get("content", "")
        ts = record.get("timestamp", "")

        if ts:
            date = ts[:10]
            if not earliest_ts or date < earliest_ts:
                earliest_ts = date
            if not latest_ts or date > latest_ts:
                latest_ts = date

        if role == "assistant":
            assistant_msgs += 1
            continue

        if role != "user":
            continue

        if not is_real_user_message(content):
            continue

        real_user_msgs += 1

        # 项目/人名/工具/话题统计
        for kw in project_keywords:
            if kw in content:
                project_mentions[kw] += 1
        for kw in topic_keywords:
            if kw in content:
                topic_mentions[kw] += 1
        m = people_pattern.search(content)
        if m:
            people_mentions[m.group()] += 1
        for tool in ["graphify", "immortal", "frontend-design", "context7",
                      "playwright", "minimax", "zai-vision", "khazix-writer",
                      "writing-skill", "lark-base", "lark-doc"]:
            if tool in content:
                tool_mentions[tool] += 1

        # 信号提取（只对中等长度的发言做精细分析）
        if 30 < len(content) < 600:
            for signal_type, sentence in extract_signal_sentences(content):
                signals[signal_type].append(sentence)

    return {
        "total_records": total_records,
        "real_user_msgs": real_user_msgs,
        "assistant_msgs": assistant_msgs,
        "earliest_ts": earliest_ts,
        "latest_ts": latest_ts,
        "projects": dict(project_mentions.most_common(20)),
        "people": dict(people_mentions.most_common(15)),
        "tools": dict(tool_mentions.most_common(15)),
        "topics": dict(topic_mentions.most_common(20)),
        "signals": {
            "correction": signals["correction"][:30],
            "decision": signals["decision"][:30],
            "preference": signals["preference"][:30],
            "opinion": signals["opinion"][:30],
        },
        "signal_counts": {k: len(v) for k, v in signals.items()},
    }


def deduplicate_signals(sentences: list, max_count: int = 15) -> list:
    """去重并按代表性筛选。"""
    seen = set()
    result = []
    # 先按长度排序，优先短小精悍的
    sentences = sorted(sentences, key=lambda s: (is_low_quality_signal(s), len(s)))
    for sent in sentences:
        if is_low_quality_signal(sent):
            continue
        # 去除尾部省略号
        sent = sent.rstrip("…").rstrip(".").strip()
        if len(sent) < 10:
            continue
        # 取前 25 字作为指纹
        key = re.sub(r'\s+', '', sent)[:25]
        if key in seen:
            continue
        seen.add(key)
        result.append(sent)
        if len(result) >= max_count:
            break
    return result


# ============================================================
# 输出生成
# ============================================================

def build_soul():
    """构建数字人格文件。"""
    print("阶段 1/3：扫描索引...")
    stats = scan_index()
    print(f"  总记录 {stats.get('total_records', 0)} 条")
    print(f"  真实用户发言 {stats.get('real_user_msgs', 0)} 条")

    print("阶段 2/3：加载 L3 记忆...")
    l3 = load_l3_memories()
    print(f"  加载 {len(l3)} 个记忆文件")

    print("阶段 3/3：合成数字人格...")

    lines = []
    lines.append("# BLACK BROTHER — Digital Soul v2.0")
    lines.append("")
    lines.append(f"> 自动从 {stats.get('total_records', 0):,} 条记录中蒸馏")
    lines.append(f"> 真实用户发言 {stats.get('real_user_msgs', 0):,} 条")
    lines.append(f"> 活跃时段 {stats.get('earliest_ts', '?')} → {stats.get('latest_ts', '?')}")
    lines.append(f"> 生成时间 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append("你是用户本人（Configured User / 用户本人）的数字分身。以下信息构成你的核心人格。")
    lines.append("回答问题时，始终以用户本人的视角、思维方式和价值观出发，用用户本人的口吻说话。")
    lines.append("")

    # ===== 一、核心身份（来自 L3 已蒸馏记忆，最权威）=====
    lines.append("## 一、核心身份（来自蒸馏记忆）")
    lines.append("")
    if "user_identity" in l3:
        lines.append(l3["user_identity"])
        lines.append("")
    if "user_writing_preferences" in l3:
        lines.append("### 写作偏好")
        lines.append(l3["user_writing_preferences"])
        lines.append("")
    if "user_technical_skills" in l3:
        lines.append("### 技术能力")
        lines.append(l3["user_technical_skills"])
        lines.append("")
    if "user_account_names" in l3:
        lines.append(l3["user_account_names"])
        lines.append("")

    # ===== 二、当前业务（来自 project_* 文件）=====
    lines.append("## 二、当前业务与项目")
    lines.append("")
    project_keys = sorted([k for k in l3 if k.startswith("project_")])
    for key in project_keys:
        title = key.replace("project_", "").replace("_", " ").title()
        lines.append(f"### {title}")
        lines.append(l3[key])
        lines.append("")

    # ===== 三、决策原则（来自 feedback_* 文件，最重要）=====
    lines.append("## 三、决策原则与行为规范")
    lines.append("")
    feedback_keys = sorted([k for k in l3 if k.startswith("feedback_")])
    for key in feedback_keys:
        title = key.replace("feedback_", "").replace("_", " ").title()
        lines.append(f"### {title}")
        lines.append(l3[key])
        lines.append("")

    # ===== 四、外部参考 =====
    lines.append("## 四、外部参考资料")
    lines.append("")
    ref_keys = sorted([k for k in l3 if k.startswith("reference_")])
    for key in ref_keys:
        title = key.replace("reference_", "").replace("_", " ").title()
        lines.append(f"### {title}")
        lines.append(l3[key])
        lines.append("")

    # ===== 五、从原始记录中提取的真实信号 =====
    lines.append("## 五、从原始对话中蒸馏的真实信号")
    lines.append("")
    lines.append(f"以下内容是从 {stats.get('real_user_msgs', 0):,} 条真实用户发言中自动提取的，")
    lines.append("代表用户本人实际表达过的判断、偏好、决策、纠正。")
    lines.append("")

    signals = stats.get("signals", {})

    # 纠正/规则类（最重要）
    if signals.get("correction"):
        lines.append("### 5.1 纠正与规则")
        lines.append("")
        for sent in deduplicate_signals(signals["correction"], 20):
            lines.append(f"- {sent}")
        lines.append("")

    # 决策类
    if signals.get("decision"):
        lines.append("### 5.2 决策与选择（用户本人的判断和选择）")
        lines.append("")
        for sent in deduplicate_signals(signals["decision"], 15):
            lines.append(f"- {sent}")
        lines.append("")

    # 偏好类
    if signals.get("preference"):
        lines.append("### 5.3 偏好与期望")
        lines.append("")
        for sent in deduplicate_signals(signals["preference"], 15):
            lines.append(f"- {sent}")
        lines.append("")

    # 观点类
    if signals.get("opinion"):
        lines.append("### 5.4 观点与判断")
        lines.append("")
        for sent in deduplicate_signals(signals["opinion"], 15):
            lines.append(f"- {sent}")
        lines.append("")

    # ===== 六、人脉网络 =====
    lines.append("## 六、提到过的人物")
    lines.append("")
    for person, count in stats.get("people", {}).items():
        lines.append(f"- **{person}** — 提及 {count} 次")
    lines.append("")

    # ===== 七、项目热度 =====
    lines.append("## 七、项目与话题热度")
    lines.append("")
    lines.append("### 项目热度")
    for proj, count in list(stats.get("projects", {}).items())[:15]:
        lines.append(f"- {proj}: {count} 次")
    lines.append("")
    lines.append("### 常用工具/Skill")
    for tool, count in list(stats.get("tools", {}).items())[:10]:
        lines.append(f"- {tool}: {count} 次")
    lines.append("")
    lines.append("### 关注话题")
    for topic, count in list(stats.get("topics", {}).items())[:15]:
        lines.append(f"- {topic}: {count} 次")
    lines.append("")

    # ===== 八、行为指引 =====
    lines.append("## 八、当你扮演用户本人时")
    lines.append("")
    lines.append("1. **优先级**：第三章（决策原则）> 第五章（真实信号）> 其他章节")
    lines.append("2. **冲突处理**：当不同来源冲突时，以已蒸馏的 L3 记忆为准")
    lines.append("3. **不知道就说不知道**：不在记忆库里的事情不要编造")
    lines.append("4. **风格**：用用户本人的口吻，短句、直接、不用破折号、不用中文书名号式引号、不用 emoji")
    lines.append("5. **思维**：从人的惰性出发，而非 Agent 能力出发；技术服务业务")
    lines.append("6. **检索**：回答前先调用 `~/.codex/skills/immortal/immortal.py recall <关键词>` 检索原始记录")
    lines.append("")

    # ===== 元数据 =====
    lines.append("---")
    lines.append("")
    lines.append("## 蒸馏元数据")
    lines.append("")
    lines.append(f"- 总记录数：{stats.get('total_records', 0):,}")
    lines.append(f"- 真实用户发言：{stats.get('real_user_msgs', 0):,}（占 {stats.get('real_user_msgs', 0) * 100 / max(stats.get('total_records', 1), 1):.1f}%）")
    lines.append(f"- AI 回复：{stats.get('assistant_msgs', 0):,}")
    sc = stats.get("signal_counts", {})
    lines.append(f"- 提取信号：correction {sc.get('correction', 0)} / decision {sc.get('decision', 0)} / preference {sc.get('preference', 0)} / opinion {sc.get('opinion', 0)}")
    lines.append(f"- L3 记忆文件：{len(l3)} 个")
    lines.append(f"- 蒸馏器：distill.py v0.2")
    lines.append("")

    content = "\n".join(lines)
    OUTPUT_FILE.write_text(content, encoding="utf-8")

    return content, stats


if __name__ == "__main__":
    content, stats = build_soul()
    size_kb = len(content) / 1024
    print()
    print(f"已生成: {OUTPUT_FILE}")
    print(f"文件大小: {size_kb:.1f} KB ({len(content):,} 字符, {len(content.splitlines())} 行)")
    print(f"信号提取: correction={stats['signal_counts'].get('correction', 0)}, "
          f"decision={stats['signal_counts'].get('decision', 0)}, "
          f"preference={stats['signal_counts'].get('preference', 0)}, "
          f"opinion={stats['signal_counts'].get('opinion', 0)}")
