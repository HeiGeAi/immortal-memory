#!/usr/bin/env python3
"""
永生记忆库 — 时间线 HTML 生成器
生成可交互的时间线页面，展示 AI 交互历史
"""

import json
import sys
import html
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict


IMMORTAL_DIR = Path.home() / ".immortal"
DAILY_DIR = IMMORTAL_DIR / "daily"
SUMMARIES_DIR = IMMORTAL_DIR / "summaries"
OUTPUT_FILE = IMMORTAL_DIR / "timeline.html"


def load_all_data():
    """加载所有日期的数据。"""
    daily_data = {}

    for daily_file in sorted(DAILY_DIR.glob("*.jsonl")):
        date = daily_file.stem
        records = []
        with open(daily_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
        daily_data[date] = records

    return daily_data


def compute_stats(date: str, records: list) -> dict:
    """计算单日统计。"""
    by_source = Counter()
    user_msgs = 0
    assistant_msgs = 0
    sessions = set()
    tools = Counter()
    files_created = []
    projects = set()

    for r in records:
        source = r.get("source", "")
        by_source[source] += 1
        role = r.get("role", "")
        if role == "user":
            user_msgs += 1
        elif role == "assistant":
            assistant_msgs += 1

        sid = r.get("session_id", "")
        if sid:
            sessions.add(sid)

        for t in r.get("tools_used", []):
            tools[t] += 1

        project = r.get("project", "")
        if project:
            projects.add(project)

    return {
        "date": date,
        "total": len(records),
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "sources": dict(by_source),
        "sessions": len(sessions),
        "tools": dict(tools.most_common(5)),
        "projects": list(projects),
    }


def load_summary(date: str) -> str:
    """加载摘要文本。"""
    summary_file = SUMMARIES_DIR / f"{date}.md"
    if summary_file.exists():
        return summary_file.read_text(encoding="utf-8")
    return ""


def get_user_topics(records: list) -> list:
    """从用户消息中提取话题。"""
    topics = []
    seen = set()
    for r in records:
        if r.get("role") != "user" or "conversation" not in r.get("source", ""):
            continue
        content = r.get("content", "")[:120].replace("\n", " ").strip()
        if content and content not in seen and len(content) > 5:
            # 跳过系统注入的上下文
            if not content.startswith("<") and not content.startswith("1\t"):
                seen.add(content)
                source = r.get("source", "")
                topics.append({"text": content, "source": source})
    return topics[:15]


def generate_html(daily_data: dict) -> str:
    """生成时间线 HTML 页面。"""
    dates = sorted(daily_data.keys(), reverse=True)
    all_stats = {date: compute_stats(date, daily_data[date]) for date in dates}

    # 总统计
    total_records = sum(s["total"] for s in all_stats.values())
    total_days = len(dates)
    total_sessions = sum(s["sessions"] for s in all_stats.values())

    # 按数据源汇总
    all_sources = Counter()
    for s in all_stats.values():
        all_sources.update(s["sources"])

    source_names = {
        "claude-code-conversation": "Claude Code",
        "codex-conversation": "Codex",
        "hermes-conversation": "Hermes",
        "claude-code-memory": "Claude 记忆",
        "codex-memory": "Codex 记忆",
        "hermes-memory": "Hermes 记忆",
        "claude-code-file-history": "文件历史",
        "claude-code-paste-cache": "粘贴输入",
        "claude-code-skill": "Claude Skill",
        "codex-skill": "Codex Skill",
        "hermes-skill": "Hermes Skill",
        "autoclaw-skill": "autoClaw Skill",
        "desktop-output": "桌面产出",
        "codex-output": "Codex 产出",
    }

    # 生成日卡片 HTML
    day_cards = []
    for date in dates:
        stats = all_stats[date]
        topics = get_user_topics(daily_data[date])

        source_bars = []
        colors = {
            "claude-code-conversation": "#70f5df",
            "codex-conversation": "#8ef77e",
            "hermes-conversation": "#f2c96b",
            "claude-code-memory": "#71a7ff",
            "codex-memory": "#14b8a6",
            "hermes-memory": "#f59e0b",
            "claude-code-file-history": "#70f5df",
            "claude-code-skill": "#71a7ff",
            "codex-skill": "#34d399",
            "hermes-skill": "#fb923c",
            "autoclaw-skill": "#ff786b",
            "desktop-output": "#64748b",
            "codex-output": "#4ade80",
            "claude-code-paste-cache": "#94a3b8",
        }

        for source, count in sorted(stats["sources"].items(), key=lambda x: -x[1]):
            pct = (count / stats["total"] * 100) if stats["total"] > 0 else 0
            color = colors.get(source, "#94a3b8")
            name = source_names.get(source, source)
            source_bars.append(
                f'<div class="source-bar"><span class="source-name">{html.escape(name)}</span>'
                f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div></div>'
                f'<span class="source-count">{count}</span></div>'
            )

        topics_html = ""
        if topics:
            topic_items = []
            for t in topics:
                badge = "claude" if "claude" in t["source"] else "codex" if "codex" in t["source"] else "hermes" if "hermes" in t["source"] else "other"
                topic_items.append(
                    f'<div class="topic {badge}">{html.escape(t["text"])[:100]}</div>'
                )
            topics_html = '<div class="topics">' + "".join(topic_items) + "</div>"

        weekday = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        weekday_cn = {"Monday": "周一", "Tuesday": "周二", "Wednesday": "周三",
                      "Thursday": "周四", "Friday": "周五", "Saturday": "周六", "Sunday": "周日"}
        weekday_str = weekday_cn.get(weekday, weekday)

        day_cards.append(f"""
        <div class="day-card" id="day-{date}">
            <div class="day-header">
                <div class="day-date">
                    <span class="date-main">{date}</span>
                    <span class="date-weekday">{weekday_str}</span>
                </div>
                <div class="day-stats">
                    <span class="stat-pill">{stats['total']}条记录</span>
                    <span class="stat-pill">{stats['sessions']}个会话</span>
                    <span class="stat-pill">{stats['user_msgs']}条对话</span>
                </div>
            </div>
            <div class="source-distribution">{''.join(source_bars)}</div>
            {topics_html}
        </div>
        """)

    # 源分布汇总
    source_summary = []
    for source, count in all_sources.most_common():
        name = source_names.get(source, source)
        color = colors.get(source, "#94a3b8")
        source_summary.append(
            f'<div class="legend-item"><span class="legend-dot" style="background:{color}"></span>'
            f'<span class="legend-name">{html.escape(name)}</span>'
            f'<span class="legend-count">{count}</span></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>用户本人 · 赛博永生记忆库 · 时间线</title>
<style>
:root {{
    color-scheme: dark;
    --bg: #05070a;
    --bg-soft: #091015;
    --panel: rgba(13, 22, 27, .86);
    --panel-strong: rgba(18, 32, 39, .94);
    --line: rgba(126, 241, 218, .18);
    --line-strong: rgba(126, 241, 218, .34);
    --text: #e7f6f3;
    --muted: #83a19c;
    --faint: #49635f;
    --cyan: #70f5df;
    --green: #8ef77e;
    --amber: #f2c96b;
    --red: #ff786b;
    --blue: #71a7ff;
    --ink: #020506;
    --radius: 8px;
    --shadow: 0 24px 70px rgba(0, 0, 0, .45);
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
html, body {{
    min-height: 100%;
    background:
        linear-gradient(rgba(112, 245, 223, .035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(112, 245, 223, .03) 1px, transparent 1px),
        radial-gradient(circle at 16% 0%, rgba(112, 245, 223, .10), transparent 32%),
        linear-gradient(140deg, #030507 0%, #071014 42%, #0d0d10 100%);
    background-size: 34px 34px, 34px 34px, auto, auto;
    color: var(--text);
    font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    letter-spacing: 0;
    line-height: 1.58;
}}
body {{
    max-width: 1320px;
    margin: 0 auto;
    padding: 28px clamp(16px, 4vw, 42px) 34px;
}}
body.embedded {{
    max-width: none;
    padding: 28px;
    background: transparent;
}}
body.embedded::before {{ display: none; }}
body.embedded .timeline-head {{ display: none; }}
body.embedded .legend, body.embedded .search-box, body.embedded .day-card {{
    box-shadow: none;
}}
body::before {{
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    background: repeating-linear-gradient(180deg, rgba(255,255,255,.025) 0 1px, transparent 1px 5px);
    mix-blend-mode: overlay;
    opacity: .22;
}}
body > * {{ position: relative; z-index: 1; }}
.timeline-head {{
    display: grid;
    grid-template-columns: minmax(0, 1.15fr) minmax(320px, .85fr);
    gap: 22px;
    align-items: stretch;
    margin-bottom: 18px;
}}
.head-copy {{
    min-height: 178px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    background:
        linear-gradient(120deg, rgba(112,245,223,.13), transparent 46%),
        rgba(9, 16, 20, .86);
    padding: 20px;
    overflow: hidden;
    position: relative;
}}
.head-copy::after {{
    content: "TIMELINE";
    position: absolute;
    right: 16px;
    bottom: 8px;
    color: rgba(112,245,223,.10);
    font-size: clamp(42px, 8vw, 92px);
    font-weight: 900;
    line-height: .85;
}}
.section-kicker {{
    color: var(--cyan);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .18em;
    text-transform: uppercase;
    margin-bottom: 12px;
}}
h1 {{
    max-width: 720px;
    font-size: clamp(30px, 4vw, 54px);
    line-height: 1;
    font-weight: 760;
    letter-spacing: 0;
}}
.subtitle {{ max-width: 620px; color: #b7cbc7; margin-top: 12px; font-size: 14px; }}
.overview {{
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
}}
.overview-card {{
    min-height: 86px;
    padding: 14px;
    border: 1px solid rgba(255,255,255,.07);
    border-radius: var(--radius);
    background: rgba(3, 9, 11, .72);
    box-shadow: 0 18px 40px rgba(0,0,0,.24);
}}
.overview-card .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .12em; }}
.overview-card .value {{ font-size: clamp(24px, 3vw, 34px); font-weight: 780; margin-top: 10px; line-height: 1; }}
.overview-card .value.purple {{ color: var(--cyan); }}
.overview-card .value.green {{ color: var(--green); }}
.overview-card .value.orange {{ color: var(--amber); }}

.legend {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px;
    margin-bottom: 16px;
    box-shadow: 0 18px 46px rgba(0,0,0,.24);
}}
.legend h3 {{ font-size: 12px; color: #c9e7e2; margin-bottom: 10px; text-transform: uppercase; letter-spacing: .12em; }}
.legend-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 6px 12px; }}
.legend-item {{ display: flex; align-items: center; gap: 8px; padding: 4px 0; }}
.legend-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.legend-name {{ font-size: 12px; flex: 1; color: #c7ddd8; }}
.legend-count {{ font-size: 11px; color: var(--muted); }}

.timeline {{ display: flex; flex-direction: column; gap: 1rem; }}
.day-card {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px;
    box-shadow: 0 18px 46px rgba(0,0,0,.22);
    transition: border-color .18s, transform .18s, background .18s;
}}
.day-card:hover {{ border-color: var(--line-strong); background: var(--panel-strong); transform: translateY(-1px); }}
.day-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem; flex-wrap: wrap; gap: 0.5rem; }}
.day-date {{ display: flex; align-items: baseline; gap: 0.6rem; }}
.date-main {{ font-size: 16px; font-weight: 780; color: var(--cyan); }}
.date-weekday {{ font-size: 12px; color: var(--muted); }}
.day-stats {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
.stat-pill {{
    background: rgba(0,0,0,.24);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 4px 8px;
    font-size: 12px;
    color: #b9d7d1;
}}
.source-distribution {{ margin-bottom: 10px; }}
.source-bar {{ display: flex; align-items: center; gap: 9px; margin-bottom: 6px; }}
.source-name {{ font-size: 11px; color: var(--muted); width: 98px; text-align: right; flex-shrink: 0; }}
.bar-track {{ flex: 1; height: 7px; background: rgba(255,255,255,.06); border-radius: 999px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 999px; transition: width 0.3s; box-shadow: 0 0 18px rgba(112,245,223,.12); }}
.source-count {{ font-size: 11px; color: var(--muted); width: 38px; }}
.topics {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.topic {{
    font-size: 12px;
    padding: 5px 8px;
    border-radius: var(--radius);
    background: rgba(255,255,255,.035);
    border: 1px solid rgba(255,255,255,.07);
    color: #c8ddd8;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}
.topic.claude {{ border-color: rgba(112,245,223,.24); background: rgba(112,245,223,.06); }}
.topic.codex {{ border-color: rgba(142,247,126,.24); background: rgba(142,247,126,.06); }}
.topic.hermes {{ border-color: rgba(242,201,107,.24); background: rgba(242,201,107,.06); }}
.topic.other {{ border-color: rgba(255,255,255,.09); }}

.search-box {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 12px;
    margin-bottom: 16px;
    display: flex;
    gap: 10px;
    box-shadow: 0 18px 46px rgba(0,0,0,.20);
}}
.search-box input {{
    flex: 1;
    background: rgba(5,10,12,.82);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 10px 12px;
    color: var(--text);
    font-size: 13px;
    outline: none;
}}
.search-box input:focus {{ border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(112,245,223,.08); }}
.search-box button {{
    background: var(--green);
    color: var(--ink);
    border: none;
    border-radius: var(--radius);
    padding: 10px 14px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 800;
}}
.search-box button:hover {{ opacity: 0.9; }}

footer {{ text-align: center; padding: 22px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--line); margin-top: 34px; }}
@media (max-width: 900px) {{
    body {{ padding: 22px 14px 28px; }}
    .timeline-head {{ grid-template-columns: 1fr; }}
    .overview {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 680px) {{
    .day-header {{ align-items: flex-start; flex-direction: column; }}
    .source-bar {{ align-items: flex-start; flex-direction: column; gap: 5px; }}
    .source-name, .source-count {{ width: auto; text-align: left; }}
    .bar-track {{ width: 100%; }}
    .search-box {{ flex-direction: column; }}
}}
</style>
</head>
<body>

<div class="timeline-head">
    <div class="head-copy">
        <div class="section-kicker">TIMELINE MEMORY VIEW</div>
        <h1>时间线</h1>
        <p class="subtitle">同一套用户本人 · 赛博永生记忆库，按日期展开每天产生的 AI 对话、文件、Skill、飞书与本地产出记录。</p>
    </div>
    <div class="overview">
        <div class="overview-card">
            <div class="label">总记录数</div>
            <div class="value purple">{total_records:,}</div>
        </div>
        <div class="overview-card">
            <div class="label">覆盖天数</div>
            <div class="value green">{total_days}</div>
        </div>
        <div class="overview-card">
            <div class="label">总会话数</div>
            <div class="value orange">{total_sessions:,}</div>
        </div>
    </div>
</div>

<div class="legend">
    <h3>数据源分布</h3>
    <div class="legend-grid">
        {''.join(source_summary)}
    </div>
</div>

<div class="search-box">
    <input type="text" id="searchInput" placeholder="搜索记忆库..." />
    <button onclick="searchTimeline()">搜索</button>
</div>

<div class="timeline" id="timeline">
    {''.join(day_cards)}
</div>

<footer>
    用户本人 · 赛博永生记忆库 · 时间线 v0.4 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}
</footer>

<script>
if (new URLSearchParams(window.location.search).get('embed') === '1') {{
    document.body.classList.add('embedded');
}}
function searchTimeline() {{
    const query = document.getElementById('searchInput').value.toLowerCase();
    const cards = document.querySelectorAll('.day-card');
    cards.forEach(card => {{
        const text = card.textContent.toLowerCase();
        card.style.display = text.includes(query) ? '' : 'none';
    }});
}}
document.getElementById('searchInput').addEventListener('keyup', function(e) {{
    if (e.key === 'Enter') searchTimeline();
    if (this.value === '') {{
        document.querySelectorAll('.day-card').forEach(c => c.style.display = '');
    }}
}});
</script>
</body>
</html>"""


def main():
    should_open = "--open" in sys.argv[1:]
    print("加载归档数据...")
    daily_data = load_all_data()
    print(f"加载了 {len(daily_data)} 天的数据")

    print("生成时间线 HTML...")
    html_content = generate_html(daily_data)

    OUTPUT_FILE.write_text(html_content, encoding="utf-8")
    print(f"时间线页面已生成: {OUTPUT_FILE}")
    print(f"大小: {len(html_content) / 1024:.0f} KB")

    if should_open:
        import subprocess
        subprocess.Popen(["open", str(OUTPUT_FILE)])


if __name__ == "__main__":
    main()
