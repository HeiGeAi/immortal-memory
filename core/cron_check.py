#!/usr/bin/env python3
"""
永生记忆库 — 定时调度健康检查器
检查 LaunchAgent / crontab / 旧 CronCreate 任务的健康状态。
"""

import json
import plistlib
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from config import daily_launch_agent_label


CRON_TASKS_FILE = Path.home() / ".claude/scheduled_tasks.json"
LEGACY_DAILY_LABEL = "com.user.immortal.daily-backup"


def candidate_launch_agent_labels() -> list[str]:
    labels = [daily_launch_agent_label()]
    if LEGACY_DAILY_LABEL not in labels:
        labels.append(LEGACY_DAILY_LABEL)
    return labels


def check_system_cron() -> dict:
    """检查系统 crontab 是否有 immortal 任务。"""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        crontab = result.stdout
        immortal_lines = [l for l in crontab.split("\n") if "immortal" in l.lower() or "daily-backup" in l]
        return {
            "ok": len(immortal_lines) >= 1,
            "task_count": len(immortal_lines),
            "tasks": immortal_lines,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "task_count": 0, "tasks": []}


def check_launch_agent() -> dict:
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
        loaded_stdout = result.stdout
        tasks: list[str] = []
        labels: list[str] = []
        loaded = False
        errors: list[str] = []
        for label in candidate_launch_agent_labels():
            plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            if not plist_path.exists():
                continue
            with plist_path.open("rb") as handle:
                plist = plistlib.load(handle)
            intervals = plist.get("StartCalendarInterval") or []
            if isinstance(intervals, dict):
                intervals = [intervals]
            labels.append(label)
            loaded = loaded or (label in loaded_stdout)
            tasks.extend(
                f"{int(item.get('Hour', 0)):02d}:{int(item.get('Minute', 0)):02d}"
                for item in intervals
                if isinstance(item, dict)
            )
        if not labels:
            return {"ok": False, "task_count": 0, "tasks": [], "error": "plist missing"}
        return {
            "ok": loaded and len(tasks) >= 4,
            "loaded": loaded,
            "labels": labels,
            "task_count": len(tasks),
            "tasks": tasks,
            "error": "; ".join(errors) if errors else "",
        }
    except Exception as e:
        return {"ok": False, "task_count": 0, "tasks": [], "error": str(e)}


def check_cron_create() -> dict:
    """检查 CronCreate 任务（7 天过期）。"""
    if not CRON_TASKS_FILE.exists():
        return {"ok": False, "task_count": 0, "tasks": [], "expiring_soon": []}

    with open(CRON_TASKS_FILE) as f:
        data = json.load(f)

    tasks = data.get("tasks", [])
    immortal_tasks = []
    expiring_soon = []

    now_ms = datetime.now(timezone.utc).timestamp() * 1000

    for t in tasks:
        prompt = t.get("prompt", "")
        if "immortal" in prompt.lower() or "永生" in prompt:
            created_ms = t.get("createdAt", 0)
            age_days = (now_ms - created_ms) / (1000 * 86400)
            days_until_expiry = 7 - age_days
            immortal_tasks.append({
                "id": t.get("id"),
                "cron": t.get("cron"),
                "age_days": round(age_days, 1),
                "days_until_expiry": round(days_until_expiry, 1),
            })
            if days_until_expiry < 2:
                expiring_soon.append(t.get("id"))

    return {
        "ok": len(immortal_tasks) > 0,
        "task_count": len(immortal_tasks),
        "tasks": immortal_tasks,
        "expiring_soon": expiring_soon,
    }


def main():
    print("=== 永生记忆库 定时调度健康检查 ===")
    print()

    launchd = check_launch_agent()
    print(f"[LaunchAgent] {'✓ 健康' if launchd['ok'] else '✗ 异常'}")
    print(f"  已加载: {launchd.get('loaded', False)}")
    if launchd.get("labels"):
        print(f"  Label: {', '.join(launchd.get('labels', []))}")
    print(f"  任务数: {launchd['task_count']}")
    for t in launchd.get("tasks", []):
        print(f"  {t}")
    if launchd.get("error"):
        print(f"  error: {launchd['error']}")
    print()

    sys_cron = check_system_cron()
    print(f"[系统 crontab] {'✓ 存在' if sys_cron['ok'] else '✓ 未启用'}")
    print(f"  任务数: {sys_cron['task_count']}")
    for t in sys_cron.get("tasks", []):
        if t.strip():
            print(f"  {t}")
    print()

    cc = check_cron_create()
    print(f"[CronCreate] {'✓ 健康' if cc['ok'] else '✗ 无任务'}")
    print(f"  任务数: {cc['task_count']}")
    for t in cc.get("tasks", []):
        warn = " ⚠️ 即将过期" if t["days_until_expiry"] < 2 else ""
        print(f"  {t['id']}: cron={t['cron']}, 已运行 {t['age_days']} 天, 还剩 {t['days_until_expiry']} 天{warn}")

    if cc.get("expiring_soon"):
        print()
        print(f"⚠️ {len(cc['expiring_soon'])} 个 CronCreate 即将过期")
        print("  当前 Codex 版以 LaunchAgent 为主；请用 immortal.py health 确认主链路是否正常")

    print()
    if launchd["ok"]:
        print("✓ LaunchAgent 是主调度，无 7 天过期问题，也更适合访问 macOS Keychain")
    elif sys_cron["ok"] and sys_cron["task_count"] >= 4:
        print("⚠️ 当前仍由系统 cron 调度；飞书 keychain 可能在后台不可用")
    else:
        print("✗ 没有可用的系统定时任务")


if __name__ == "__main__":
    main()
