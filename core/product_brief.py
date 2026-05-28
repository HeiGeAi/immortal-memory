#!/usr/bin/env python3
"""Generate the product-level operating brief for the Immortal skill."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import configured_vault_dir, load_config, owner_display_name


IMMORTAL_DIR = configured_vault_dir()
OUTPUT_DIR = IMMORTAL_DIR / "product"
OUTPUT_JSON = OUTPUT_DIR / "goal.json"
OUTPUT_MD = OUTPUT_DIR / "goal.md"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


PATHS = {
    "index": IMMORTAL_DIR / "index.jsonl",
    "state": IMMORTAL_DIR / "orchestrator_state.json",
    "quality": IMMORTAL_DIR / "quality" / "latest.json",
    "digest": IMMORTAL_DIR / "digests" / "latest.json",
    "people": IMMORTAL_DIR / "people" / "people_index.json",
    "profile": IMMORTAL_DIR / "profile.json",
    "nuwa": IMMORTAL_DIR / "profile_nuwa.json",
    "feishu_clean": IMMORTAL_DIR / "feishu" / "clean" / "coverage.json",
    "feishu_distilled": IMMORTAL_DIR / "feishu" / "distilled" / "coverage.json",
    "agent_entry": IMMORTAL_DIR / "agent" / "ENTRY.md",
    "latest_agent_context": IMMORTAL_DIR / "agent" / "latest-context.md",
    "latest_task_session": IMMORTAL_DIR / "sessions" / "latest.json",
    "dashboard": IMMORTAL_DIR / "dashboard.html",
    "timeline": IMMORTAL_DIR / "timeline.html",
}


def now_local() -> str:
    return datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def count_index_records(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except Exception:
        return 0


def file_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "bytes": 0, "modified_at": ""}
    return {
        "exists": True,
        "path": str(path),
        "bytes": path.stat().st_size,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ).isoformat(timespec="seconds"),
    }


def counter_subset(counters: dict[str, Any], prefixes: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in counters.items():
        if key.startswith(prefixes):
            try:
                out[key] = int(value)
            except Exception:
                continue
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0]))[:20])


def latest_task_sessions() -> list[dict[str, Any]]:
    sessions_dir = IMMORTAL_DIR / "sessions"
    sessions: list[dict[str, Any]] = []
    if not sessions_dir.exists():
        return sessions
    for manifest in sessions_dir.glob("*/manifest.json"):
        data = read_json(manifest, {})
        sessions.append(
            {
                "goal": data.get("query") or manifest.parent.name,
                "mode": data.get("mode") or "",
                "path": str(manifest.parent),
                "generated_at": data.get("generated_at") or file_status(manifest)["modified_at"],
                "expires_at": data.get("expires_at") or "",
                "returncode": data.get("returncode"),
            }
        )
    return sorted(sessions, key=lambda item: item.get("generated_at") or "", reverse=True)[:12]


def build_goal() -> dict[str, Any]:
    config = load_config()
    state = read_json(PATHS["state"], {})
    quality = read_json(PATHS["quality"], {})
    digest = read_json(PATHS["digest"], {})
    people = read_json(PATHS["people"], {})
    profile = read_json(PATHS["profile"], {})
    nuwa = read_json(PATHS["nuwa"], {})
    feishu_clean = read_json(PATHS["feishu_clean"], {})
    feishu_distilled = read_json(PATHS["feishu_distilled"], {})
    clean_counters = feishu_clean.get("counters") if isinstance(feishu_clean.get("counters"), dict) else {}
    distilled_counters = feishu_distilled.get("counters") if isinstance(feishu_distilled.get("counters"), dict) else {}
    digest_summary = digest.get("summary") if isinstance(digest.get("summary"), dict) else {}

    total_records = int(state.get("total_records") or digest_summary.get("total_records") or 0)
    if not total_records:
        total_records = count_index_records(PATHS["index"])

    return {
        "generated_at": now_local(),
        "owner": owner_display_name(config),
        "product_name": "Immortal Memory / 赛博永生",
        "one_sentence": "一个本地优先的个人记忆操作系统：先保全数字痕迹，再自动蒸馏成可信画像，最后通过 Agent Bridge 让 Codex、Claude Code 和其他 AI 按任务调用。",
        "true_jobs": [
            "防丢失：AI 对话、文件、文档、会议、飞书内容先进入本地保险库，能恢复、能审计、能迁移。",
            "防污染：区分本人视角、他人说法、项目事实、引用材料和敏感信息，避免把非本人语料写进长期画像。",
            "可调用：通过 agent/ENTRY.md 和 agent-context 输出任务级上下文，而不是让用户反复解释自己。",
            "可调用：按写稿、审稿、商务、项目管理等场景生成短期任务上下文；稳定高频流程才显式晋升为角色 skill。",
            "可兼容：产品内核独立于 Codex，Codex skill、Claude Code skill、未来 MCP/HTTP 都只是适配器。",
            "可开源：发布空壳版本到 GitHub，不带任何用户私有 vault、画像、飞书数据、角色证据、日志或密钥。",
        ],
        "current_state": {
            "total_records": total_records,
            "quality_status": quality.get("status") or "missing",
            "quality_score": quality.get("score"),
            "quality_issues": quality.get("issue_count"),
            "people_count": len(people.get("people") or []),
            "reviewed_memories": len(profile.get("reviewed_profile_memories") or []),
            "mental_models": len(nuwa.get("mental_models") or []),
            "task_sessions": latest_task_sessions(),
            "last_collect": state.get("last_collect"),
            "last_feishu_collect": state.get("last_feishu_collect"),
            "last_quality": state.get("last_quality"),
        },
        "source_coverage": {
            "feishu_clean": counter_subset(clean_counters, ("raw_source:", "clean_source:", "candidate_source:")),
            "feishu_distilled": distilled_counters,
            "daily_sources": (config.get("feishu") or {}).get("daily_sources", ""),
        },
        "stable_entrypoints": {
            "health": "python3 ~/.codex/skills/immortal/immortal.py health",
            "daily_run": "python3 ~/.codex/skills/immortal/immortal.py run",
            "agent_entry": "python3 ~/.codex/skills/immortal/immortal.py agent-entry",
            "agent_entry_url": "http://127.0.0.1:8765/agent-entry",
            "agent_context": "python3 ~/.codex/skills/immortal/immortal.py agent-context \"当前任务\" --print",
            "task_compile": "python3 ~/.codex/skills/immortal/immortal.py task-compile \"当前任务\" --mode auto",
            "context_pack": "python3 ~/.codex/skills/immortal/immortal.py context \"当前任务\"",
            "task_compiler": "http://127.0.0.1:8765/agent-factory",
            "agent_factory_command": "python3 ~/.codex/skills/immortal/immortal.py agent-factory",
            "daily_automation": "python3 ~/.codex/skills/immortal/immortal.py daily-status",
            "dashboard": str(PATHS["dashboard"]),
            "timeline": str(PATHS["timeline"]),
            "package": "python3 ~/.codex/skills/immortal/immortal.py package",
            "oss_export": "python3 ~/.codex/skills/immortal/oss_export.py --output ~/Desktop/immortal-memory-open-source --force",
            "oss_repo": str(Path.home() / "Desktop" / "immortal-memory-open-source"),
            "restore_guide": "python3 ~/.codex/skills/immortal/immortal.py restore-guide",
        },
        "product_decisions": [
            "产品不再定义为单一 Codex skill，而是独立本地产品；skill 只是 Codex 适配器。",
            "开源仓库采用空壳发布：core + adapters + docs + installer + smoke test，不包含任何真实用户数据。",
            "看板只做观察层，不再要求用户审阅候选记忆。",
            "其他 agent 默认接入 agent/ENTRY.md，再按当前任务生成 task-local context；不要直接读取完整原始库。",
            "任务上下文生成器不再默认安装长期 skill，默认生成短期任务上下文，用完自动清理。",
            "profile/profile_nuwa/reviewed 记忆是当前可信画像主链路，digital-soul.md 视为历史兼容层。",
            "自动任务默认只合并到 reviewed/profile 层，不直接改写旧 digital-soul，避免一次错误污染全局人格。",
            "会议和妙记属于高价值语料源，进入每日飞书源，并支持历史回填。",
            "完整替代本人不能作为无监督自动决策上线；可作为有证据、有边界、有日志的代理层逐步接管重复判断。",
        ],
        "next_build_priorities": [
            "把 GitHub 空壳仓库作为主发布形态，完成 README、架构文档、隐私边界、安装器、CI 和 smoke test。",
            "把 Codex/Claude Code 适配器进一步瘦身，确保所有工具都通过统一 Agent Bridge 调用 core。",
            "把会议/妙记纳入日常增量采集，并继续扩大飞书云文档镜像覆盖。",
            "把 agent/ENTRY.md、agent-context 和 task-compile 固化为其他 agent 的默认了解入口，少贴长上下文。",
            "只为高频稳定流程保留显式 role-distill 晋升能力，不让日常自动化生成长期 skill。",
            "把备份目标从本机导出升级到稳定外部存储：NAS/云盘/对象存储优先，GitHub 私库只放小型索引和加密快照。",
        ],
        "files": {name: file_status(path) for name, path in PATHS.items()},
    }


def render_markdown(goal: dict[str, Any]) -> str:
    lines = [
        "# 赛博永生产品目标",
        "",
        f"Generated: {goal['generated_at']}",
        f"Owner: {goal['owner']}",
        "",
        "## 一句话",
        goal["one_sentence"],
        "",
        "## 真正要解决的事",
    ]
    lines.extend(f"- {item}" for item in goal["true_jobs"])
    state = goal["current_state"]
    lines.extend(
        [
            "",
            "## 当前状态",
            f"- 总记录：{state.get('total_records'):,}",
            f"- 质量：{state.get('quality_status')} / {state.get('quality_score')} / issues={state.get('quality_issues')}",
            f"- 人物档案：{state.get('people_count')}",
            f"- reviewed 记忆：{state.get('reviewed_memories')}",
            f"- mental models：{state.get('mental_models')}",
            f"- 最近采集：{state.get('last_collect') or 'unknown'}",
            f"- 最近飞书采集：{state.get('last_feishu_collect') or 'unknown'}",
            "",
            "## 稳定入口",
        ]
    )
    for key, value in goal["stable_entrypoints"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## 产品决策"])
    lines.extend(f"- {item}" for item in goal["product_decisions"])
    lines.extend(["", "## 下一步优先级"])
    lines.extend(f"- {item}" for item in goal["next_build_priorities"])
    sessions = state.get("task_sessions") or []
    lines.extend(["", "## 最近任务上下文"])
    if sessions:
        for session in sessions:
            lines.append(
                f"- {session.get('goal')} / {session.get('mode')} / "
                f"returncode={session.get('returncode')} / {session.get('path')}"
            )
    else:
        lines.append("- 暂无任务上下文")
    lines.extend(["", "## 飞书源覆盖"])
    daily_sources = (goal.get("source_coverage") or {}).get("daily_sources") or ""
    lines.append(f"- daily_sources: `{daily_sources}`")
    for key, value in ((goal.get("source_coverage") or {}).get("feishu_clean") or {}).items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    goal = build_goal()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(goal, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    OUTPUT_MD.write_text(render_markdown(goal), encoding="utf-8")
    print(f"product_goal_json={OUTPUT_JSON}")
    print(f"product_goal_md={OUTPUT_MD}")
    print(f"quality={goal['current_state'].get('quality_status')} score={goal['current_state'].get('quality_score')}")
    print(f"records={goal['current_state'].get('total_records')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
