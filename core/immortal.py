#!/usr/bin/env python3
"""
Immortal Skill command entry for Codex.

This script keeps the user's local traces from becoming a pile of files.
The first priority is loss prevention. The second priority is making the
memory useful inside Codex through brief, recall, and context assembly.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from config import (
    CONFIG_FILE,
    DEFAULT_CONFIG,
    configured_vault_dir,
    daily_launch_agent_label,
    daily_schedule,
    feishu_guard_args,
    load_config,
    owner_aliases,
    owner_display_name,
    save_config,
    slug_prefix,
)
from export_restore import create_export, get_backup_status, restore_check


SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = configured_vault_dir()
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
DAILY_DIR = IMMORTAL_DIR / "daily"
SUMMARY_DIR = IMMORTAL_DIR / "summaries"
STATE_FILE = IMMORTAL_DIR / "orchestrator_state.json"
SOURCES_FILE = IMMORTAL_DIR / "sources.json"
SOUL_FILE = IMMORTAL_DIR / "digital-soul.md"
BACKUP_LOG = IMMORTAL_DIR / "backup.log"
LEGACY_DAILY_LAUNCH_AGENT_LABEL = "com.user.immortal.daily-backup"
PROFILE_MD = IMMORTAL_DIR / "profile.md"
PROFILE_JSON = IMMORTAL_DIR / "profile.json"
PROFILE_COMPACT_MD = IMMORTAL_DIR / "profile_compact.md"
PROFILE_NUWA_MD = IMMORTAL_DIR / "profile_nuwa.md"
PROFILE_NUWA_JSON = IMMORTAL_DIR / "profile_nuwa.json"
PEOPLE_MD = IMMORTAL_DIR / "people" / "people_index.md"
PEOPLE_JSON = IMMORTAL_DIR / "people" / "people_index.json"
RELATIONSHIP_JSON = IMMORTAL_DIR / "relationships" / "relationship_index.json"
RELATIONSHIP_MD = IMMORTAL_DIR / "relationships" / "relationship_index.md"
QUALITY_JSON = IMMORTAL_DIR / "quality" / "latest.json"
QUALITY_MD = IMMORTAL_DIR / "quality" / "latest.md"
DIGEST_JSON = IMMORTAL_DIR / "digests" / "latest.json"
DIGEST_MD = IMMORTAL_DIR / "digests" / "latest.md"
PRODUCT_GOAL_JSON = IMMORTAL_DIR / "product" / "goal.json"
PRODUCT_GOAL_MD = IMMORTAL_DIR / "product" / "goal.md"
DASHBOARD_HTML = IMMORTAL_DIR / "dashboard.html"
TIMELINE_HTML = IMMORTAL_DIR / "timeline.html"
FEISHU_CLEAN_COVERAGE = IMMORTAL_DIR / "feishu" / "clean" / "coverage.json"
FEISHU_DISTILLED_COVERAGE = IMMORTAL_DIR / "feishu" / "distilled" / "coverage.json"
FEISHU_DRIVE_MIRROR_DIR = IMMORTAL_DIR / "feishu" / "drive_mirror"
FEISHU_DRIVE_MIRROR_DB = FEISHU_DRIVE_MIRROR_DIR / "inventory.sqlite3"
FEISHU_DRIVE_MIRROR_COVERAGE = FEISHU_DRIVE_MIRROR_DIR / "coverage.json"
PROFILE_ATTRIBUTION_AUDIT_JSON = IMMORTAL_DIR / "quality" / "profile_attribution_audit.json"
AGENT_ENTRY_MD = IMMORTAL_DIR / "agent" / "ENTRY.md"
AGENT_ENTRY_JSON = IMMORTAL_DIR / "agent" / "entry.json"
GETNOTE_STATE_JSON = IMMORTAL_DIR / "getnote" / "diary_sync_state.json"
GETNOTE_LATEST_JSON = IMMORTAL_DIR / "getnote" / "latest.json"
GETNOTE_CONFIG = Path.home() / ".getnote" / "config.json"
OFFSITE_MARKERS = [
    IMMORTAL_DIR / "backups",
    IMMORTAL_DIR / "exports",
    Path.home() / "Desktop" / "immortal-handover-v0.7",
]


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def fmt_size(path: Path) -> str:
    if not path.exists():
        return "missing"
    size = path.stat().st_size
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GB"


def fmt_bytes(size: int | float | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def local_time(value: str | None) -> str:
    dt = parse_iso(value)
    if not dt:
        return value or "unknown"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def path_mtime(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def age_hours_from_dt(dt: datetime | None) -> float | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600


def age_detail(dt: datetime | None) -> str:
    if not dt:
        return "missing"
    return f"{dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} ({age_hours_from_dt(dt):.1f}h ago)"


def portable_backup_detail(status: dict, *, max_age_hours: float | None = None) -> tuple[bool, str]:
    latest = status.get("latest_export") or {}
    generated_at = latest.get("generated_at") or ""
    dt = parse_iso(generated_at)
    age = age_hours_from_dt(dt)
    totals = latest.get("totals") if isinstance(latest, dict) else {}
    files = int((totals or {}).get("files") or 0)
    bytes_total = int((totals or {}).get("bytes") or 0)
    export_dir = latest.get("export_dir") or ""
    check = status.get("check") or {}
    manifest_ok = bool(status.get("ok")) and files > 0
    fresh_ok = True if max_age_hours is None else bool(age is not None and age <= max_age_hours)
    ok = manifest_ok and fresh_ok
    if not latest or not status.get("ok"):
        warnings = status.get("warnings") or check.get("warnings") or []
        return False, ", ".join(warnings) if warnings else "no portable export found"
    age_text = age_detail(dt)
    mode = status.get("mode") or check.get("mode") or "manifest-only"
    detail = f"{age_text} · {files:,} files · {fmt_bytes(bytes_total)} · {mode} · {export_dir}"
    return ok, detail


def write_state_key(key: str, value) -> None:
    state = read_json(STATE_FILE, {})
    state[key] = value
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def getnote_credentials_present() -> bool:
    if os.environ.get("GETNOTE_API_KEY") and os.environ.get("GETNOTE_CLIENT_ID"):
        return True
    config = read_json(GETNOTE_CONFIG, {})
    return bool(isinstance(config, dict) and config.get("api_key") and config.get("client_id"))


def state_time(state: dict, key: str) -> datetime | None:
    return parse_iso(state.get(key))


def run_script(script: str, args: list[str] | None = None) -> int:
    cmd = [sys.executable, str(SKILL_DIR / script)]
    if args:
        cmd.extend(args)
    return subprocess.call(cmd)


def iter_daily_files(days: int = 2) -> Iterable[Path]:
    if not DAILY_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    files = []
    for path in DAILY_DIR.glob("*.jsonl*"):
        date_part = path.name.split(".jsonl")[0]
        try:
            day = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            files.append(path)
    return sorted(files)


def open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return open(path, "r", encoding="utf-8", errors="ignore")


def iter_recent_records(days: int = 2, limit: int = 3000) -> Iterable[dict]:
    count = 0
    for path in iter_daily_files(days):
        with open_text(path) as f:
            for line in f:
                if count >= limit:
                    return
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += 1
                yield record


def load_latest_summary() -> tuple[str, str]:
    if not SUMMARY_DIR.exists():
        return "", ""
    files = sorted(SUMMARY_DIR.glob("*.md"), reverse=True)
    if not files:
        return "", ""
    latest = files[0]
    return latest.stem, latest.read_text(encoding="utf-8", errors="ignore")


def compact(text: str, n: int = 180) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = redact(text)
    if len(text) <= n:
        return text
    return text[: n - 1] + "..."


def redact(text: str) -> str:
    """Redact obvious secrets before printing raw memory previews."""
    patterns = [
        (r"\bcli_[A-Za-z0-9_\-]{8,}\b", "cli_[REDACTED]"),
        (r"(?i)(app secret\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(api\s*key\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(apikey\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(password\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        (r"(?i)(密码\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        (r"sk-[A-Za-z0-9_\-]{12,}", "sk-[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def is_human_action_text(text: str) -> bool:
    s = text.strip()
    if len(s) < 8:
        return False
    code_markers = [
        "#!/usr/bin/env", "import ", "def ", "class ", "const ", "let ",
        "function ", "return ", "console.log", "```", "Exit code",
    ]
    if sum(marker in s[:500] for marker in code_markers) >= 2:
        return False
    if re.search(r"^\s*\d+\s+(import|def|class|const|let|function)\b", s):
        return False
    if s.count("{") > 5 and s.count('"') > 10:
        return False
    return True


def command_status(_args) -> int:
    state = read_json(STATE_FILE, {})
    sources = read_json(SOURCES_FILE, {"sources": []}).get("sources", [])
    relationships = read_json(RELATIONSHIP_JSON, {})
    relationship_summary = relationships.get("summary") if isinstance(relationships, dict) else {}
    if not isinstance(relationship_summary, dict):
        relationship_summary = {}
    print("Immortal Skill status")
    print()
    print(f"Storage: {IMMORTAL_DIR}")
    print(f"Index: {fmt_size(INDEX_FILE)}")
    print(f"Digital soul: {fmt_size(SOUL_FILE)}")
    print(f"Profile: {fmt_size(PROFILE_MD)}")
    print(f"Compact profile: {fmt_size(PROFILE_COMPACT_MD)}")
    print(
        "Evidence network: "
        f"{fmt_size(RELATIONSHIP_JSON)} "
        f"({relationship_summary.get('person_edges', 0)} person edges, "
        f"{relationship_summary.get('project_edges', 0)} project edges)"
    )
    quality = read_json(QUALITY_JSON, {})
    print(
        "Quality: "
        f"{fmt_size(QUALITY_JSON)} "
        f"({quality.get('status', 'missing')}, score {quality.get('score', '-')})"
    )
    print(f"Sources: {len(sources)}")
    print(f"Total records: {state.get('total_records', 'unknown')}")
    print(f"Collect count: {state.get('collect_count', 'unknown')}")
    print(f"Last collect: {local_time(state.get('last_collect'))}")
    print(f"Last distill: {local_time(state.get('last_distill'))}")
    print(f"Last evidence network: {local_time(state.get('last_relationship_index'))}")
    print(f"Last cleanup: {local_time(state.get('last_cleanup'))}")
    errors = state.get("errors") or []
    print(f"Errors: {', '.join(errors) if errors else 'none'}")
    return 0


def daily_launch_agent_path(label: str | None = None) -> Path:
    value = label or daily_launch_agent_label()
    return Path.home() / "Library" / "LaunchAgents" / f"{value}.plist"


def candidate_daily_launch_agent_labels() -> list[str]:
    labels = [daily_launch_agent_label()]
    if LEGACY_DAILY_LAUNCH_AGENT_LABEL not in labels:
        labels.append(LEGACY_DAILY_LAUNCH_AGENT_LABEL)
    return labels


def check_crontab() -> tuple[bool, list[str]]:
    scheduler_lines: list[str] = []
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        crontab_stdout = result.stdout
    except Exception:
        crontab_stdout = ""
    cron_lines = [
        line for line in crontab_stdout.splitlines()
        if "immortal" in line.lower() or "daily-backup" in line.lower()
    ]
    scheduler_lines.extend([f"cron: {line}" for line in cron_lines])

    loaded_any = False
    launch_intervals_max = 0
    try:
        launchctl = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        launchctl = ""
    for label in candidate_daily_launch_agent_labels():
        plist_path = daily_launch_agent_path(label)
        if not plist_path.exists():
            continue
        try:
            with plist_path.open("rb") as handle:
                plist = plistlib.load(handle)
            intervals = plist.get("StartCalendarInterval") or []
            if isinstance(intervals, dict):
                intervals = [intervals]
            launch_intervals = len(intervals) if isinstance(intervals, list) else 0
            launch_intervals_max = max(launch_intervals_max, launch_intervals)
            loaded = label in launchctl
            loaded_any = loaded_any or loaded
            scheduler_lines.append(
                f"launchd: {label} ({'loaded' if loaded else 'not loaded'}, {launch_intervals} schedules)"
            )
        except Exception as exc:
            scheduler_lines.append(f"launchd: {label} invalid ({exc})")

    ok = len(cron_lines) >= 4 or (loaded_any and launch_intervals_max >= 4)
    return ok, scheduler_lines


def command_doctor(_args) -> int:
    state = read_json(STATE_FILE, {})
    sources = read_json(SOURCES_FILE, {"sources": []}).get("sources", [])
    scheduler_ok, scheduler_lines = check_crontab()
    checks = []
    optional_checks = []

    def add(name: str, ok: bool, detail: str):
        checks.append((name, ok, detail))

    def add_optional(name: str, ok: bool, detail: str):
        optional_checks.append((name, ok, detail))

    add("storage directory", IMMORTAL_DIR.exists(), str(IMMORTAL_DIR))
    add("index file", INDEX_FILE.exists() and INDEX_FILE.stat().st_size > 0, fmt_size(INDEX_FILE))
    add("daily archives", DAILY_DIR.exists() and any(DAILY_DIR.glob("*.jsonl*")), str(DAILY_DIR))
    add("sources config", len(sources) > 0, f"{len(sources)} sources")
    add("digital soul", SOUL_FILE.exists() and SOUL_FILE.stat().st_size > 1024, fmt_size(SOUL_FILE))
    add(
        "profile layer",
        PROFILE_MD.exists() and PROFILE_JSON.exists() and PROFILE_COMPACT_MD.exists(),
        f"{fmt_size(PROFILE_MD)}, {fmt_size(PROFILE_COMPACT_MD)}, {fmt_size(PROFILE_JSON)}",
    )
    nuwa = read_json(PROFILE_NUWA_JSON, {})
    nuwa_gate = nuwa.get("quality_gate") if isinstance(nuwa, dict) else {}
    add_optional(
        "Nuwa profile",
        PROFILE_NUWA_MD.exists()
        and PROFILE_NUWA_JSON.exists()
        and (nuwa_gate or {}).get("status") in {"ok", "attention"},
        f"{fmt_size(PROFILE_NUWA_MD)}, {(nuwa_gate or {}).get('status', 'missing')}",
    )
    relationship_data = read_json(RELATIONSHIP_JSON, {})
    relationship_summary = relationship_data.get("summary") if isinstance(relationship_data, dict) else {}
    if not isinstance(relationship_summary, dict):
        relationship_summary = {}
    add_optional(
        "evidence network",
        RELATIONSHIP_JSON.exists() and RELATIONSHIP_JSON.stat().st_size > 0,
        (
            f"{fmt_size(RELATIONSHIP_JSON)}, "
            f"{relationship_summary.get('person_edges', 0)} person edges, "
            f"{relationship_summary.get('project_edges', 0)} project edges"
        ),
    )
    quality = read_json(QUALITY_JSON, {})
    add_optional(
        "quality report",
        QUALITY_JSON.exists() and QUALITY_JSON.stat().st_size > 0 and quality.get("status") in {"ok", "attention"},
        f"{fmt_size(QUALITY_JSON)}, {quality.get('status', 'missing')}, score {quality.get('score', '-')}",
    )
    add("backup log", BACKUP_LOG.exists(), fmt_size(BACKUP_LOG))
    add("system scheduler", scheduler_ok, f"{len(scheduler_lines)} immortal scheduler entries")
    add("last run errors", not state.get("errors"), ", ".join(state.get("errors") or []) or "none")
    backup_ok, backup_detail = portable_backup_detail(get_backup_status(IMMORTAL_DIR), max_age_hours=168)
    add("portable export", backup_ok, backup_detail)

    last_collect = parse_iso(state.get("last_collect"))
    if last_collect:
        age_hours = (datetime.now(timezone.utc) - last_collect.astimezone(timezone.utc)).total_seconds() / 3600
        add("recent collect", age_hours < 30, f"{age_hours:.1f} hours ago")
    else:
        add("recent collect", False, "never")

    print("Immortal Skill doctor")
    print()
    failures = 0
    for name, ok, detail in checks:
        mark = "OK" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"{mark:4} {name}: {detail}")
    for name, ok, detail in optional_checks:
        mark = "OK" if ok else "WARN"
        print(f"{mark:4} {name}: {detail}")

    if scheduler_lines:
        print()
        print("Scheduler tasks:")
        for line in scheduler_lines:
            print(f"  {line}")

    print()
    if failures:
        print(f"Result: {failures} issue(s) need attention.")
        return 1
    print("Result: memory capture baseline is healthy.")
    return 0


def command_health(args) -> int:
    state = read_json(STATE_FILE, {})
    digest = read_json(DIGEST_JSON, {})
    quality = read_json(QUALITY_JSON, {})
    relationship_data = read_json(RELATIONSHIP_JSON, {})
    relationship_summary = relationship_data.get("summary") if isinstance(relationship_data, dict) else {}
    if not isinstance(relationship_summary, dict):
        relationship_summary = {}
    scheduler_ok, scheduler_lines = check_crontab()
    max_age = float(args.max_age_hours)

    checks: list[tuple[str, bool, str]] = []

    def add_state(label: str, key: str):
        dt = state_time(state, key)
        age = age_hours_from_dt(dt)
        checks.append((label, bool(dt and age is not None and age <= max_age), age_detail(dt)))

    def add_file(label: str, path: Path, *, min_size: int = 1):
        dt = path_mtime(path)
        age = age_hours_from_dt(dt)
        ok = path.exists() and path.stat().st_size >= min_size and age is not None and age <= max_age
        checks.append((label, ok, f"{fmt_size(path)} · {age_detail(dt)}"))

    add_state("采集状态", "last_collect")
    add_state("飞书采集", "last_feishu_collect")
    add_state("飞书清洗", "last_feishu_clean")
    add_state("飞书蒸馏", "last_feishu_distill")
    add_state("长期画像", "last_profile")
    add_state("Nuwa 画像", "last_profile_nuwa")
    add_state("人物索引", "last_people_index")
    add_state("关联证据", "last_relationship_index")
    add_state("质量报告", "last_quality")
    add_state("产品目标", "last_product_brief")
    add_state("画像归因审计", "last_profile_attribution_audit")
    add_state("飞书云文档清单镜像", "last_feishu_mirror_inventory")
    add_state("飞书云文档限量导出", "last_feishu_mirror_download")
    add_state("Agent Entry", "last_agent_entry")
    getnote_enabled = getnote_credentials_present() or GETNOTE_LATEST_JSON.exists()
    if getnote_enabled:
        add_state("Get 笔记日记同步", "last_getnote_diary_sync")
    add_file("Quality JSON", QUALITY_JSON)
    add_file("Quality MD", QUALITY_MD)
    add_file("Profile Attribution Audit", PROFILE_ATTRIBUTION_AUDIT_JSON)
    add_file("Product Goal JSON", PRODUCT_GOAL_JSON)
    add_file("Product Goal MD", PRODUCT_GOAL_MD)
    add_file("Nuwa Profile JSON", PROFILE_NUWA_JSON)
    add_file("Nuwa Profile MD", PROFILE_NUWA_MD)
    add_file("Digest JSON", DIGEST_JSON)
    add_file("Digest MD", DIGEST_MD)
    add_file("主看板", DASHBOARD_HTML, min_size=1024)
    add_file("时间线", TIMELINE_HTML, min_size=1024)
    add_file("Agent Entry MD", AGENT_ENTRY_MD, min_size=1024)
    add_file("Agent Entry JSON", AGENT_ENTRY_JSON)
    if getnote_enabled:
        add_file("Get 笔记同步状态", GETNOTE_LATEST_JSON)
    backup_ok, backup_detail = portable_backup_detail(get_backup_status(IMMORTAL_DIR), max_age_hours=168)
    checks.append(("便携备份", backup_ok, backup_detail))
    restore_dt = state_time(state, "last_portable_restore_check")
    restore_age = age_hours_from_dt(restore_dt)
    restore_ok = bool(
        restore_dt
        and restore_age is not None
        and restore_age <= 168
        and state.get("last_portable_restore_check_status") == "ok"
    )
    checks.append((
        "备份校验",
        restore_ok,
        f"{age_detail(restore_dt)} · files {state.get('last_portable_restore_check_files', 0)} · {state.get('last_portable_restore_check_dir', '') or 'missing'}",
    ))
    add_file("飞书 clean coverage", FEISHU_CLEAN_COVERAGE)
    add_file("飞书 distilled coverage", FEISHU_DISTILLED_COVERAGE)
    add_file("飞书云文档 mirror coverage", FEISHU_DRIVE_MIRROR_COVERAGE)

    errors = state.get("errors") or []
    checks.append(("当前错误", not errors, ", ".join(errors) if errors else "none"))
    checks.append(("系统定时", scheduler_ok, f"{len(scheduler_lines)} scheduler entries"))
    checks.append((
        "关联证据规模",
        bool(relationship_summary.get("person_edges") or relationship_summary.get("project_edges")),
        f"{relationship_summary.get('person_edges', 0)} person edges, {relationship_summary.get('project_edges', 0)} project edges",
    ))
    quality_status = quality.get("status") if isinstance(quality, dict) else ""
    quality_ok = quality_status in {"ok", "attention"}
    checks.append((
        "质量状态",
        quality_ok,
        f"{quality_status or 'missing'} · score {quality.get('score', '-') if isinstance(quality, dict) else '-'} · issues {quality.get('issue_count', '-') if isinstance(quality, dict) else '-'}",
    ))
    digest_status = ((digest.get("errors") or {}).get("status") if isinstance(digest, dict) else "") or "missing"
    checks.append(("Digest 状态", digest_status == "ok", str(digest_status)))
    if getnote_enabled:
        getnote_latest = read_json(GETNOTE_LATEST_JSON, {})
        getnote_results = getnote_latest.get("results") if isinstance(getnote_latest, dict) else []
        latest_note = ""
        if isinstance(getnote_results, list) and getnote_results:
            latest_note = str(getnote_results[-1].get("note_id") or "")
        getnote_status = str(getnote_latest.get("status") or "missing") if isinstance(getnote_latest, dict) else "missing"
        checks.append((
            "Get 笔记同步结果",
            getnote_status == "ok",
            f"{getnote_status} · date {getnote_latest.get('latest_date', '') if isinstance(getnote_latest, dict) else ''} · note {latest_note or 'missing'}",
        ))

    print("Immortal Daily Health")
    print()
    print(f"Window: {max_age:.0f}h")
    print(f"Storage: {IMMORTAL_DIR}")
    print(f"Total records: {state.get('total_records', 'unknown')}")
    print(f"Last collect: {local_time(state.get('last_collect'))}")
    print()

    failures = 0
    for label, ok, detail in checks:
        mark = "OK" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"{mark:4} {label}: {detail}")

    if scheduler_lines:
        print()
        print("Scheduler tasks:")
        for line in scheduler_lines:
            print(f"  {line}")

    print()
    if failures:
        print(f"Result: {failures} health check(s) need Codex attention.")
        return 1
    print("Result: daily automated memory loop is current.")
    return 0


def signal_records(days: int) -> dict[str, list[dict]]:
    buckets = {
        "decisions": [],
        "commitments": [],
        "followups": [],
    }
    patterns = {
        "decisions": re.compile(r"(决定|就用|采用|先做|优先|拍板|选|迁移|部署|接入)"),
        "commitments": re.compile(r"(明天|今天|今晚|本周|下周|记得|提醒|要跟|需要跟|回头|后续)"),
        "followups": re.compile(r"(下一步|继续|推进|修复|优化|检查|复盘|打包|上线|交付)"),
    }
    seen = set()
    for record in iter_recent_records(days=days):
        if record.get("role") != "user":
            continue
        content = record.get("content", "")
        if not content or not is_human_action_text(content):
            continue
        key = re.sub(r"\s+", " ", content.strip())[:220]
        if key in seen:
            continue
        seen.add(key)
        for key, pattern in patterns.items():
            if pattern.search(content):
                buckets[key].append(record)
                break
    return buckets


def command_brief(args) -> int:
    date, summary = load_latest_summary()
    state = read_json(STATE_FILE, {})
    buckets = signal_records(args.days)

    print("Immortal Brief")
    print()
    print(f"Window: last {args.days} day(s)")
    print(f"Last collect: {local_time(state.get('last_collect'))}")
    print(f"Total records: {state.get('total_records', 'unknown')}")
    print()

    if date:
        first_lines = [line for line in summary.splitlines() if line.strip()][:12]
        print(f"Latest daily summary: {date}")
        for line in first_lines:
            print(f"  {line}")
        print()

    sections = [
        ("Likely decisions", buckets["decisions"]),
        ("Potential commitments", buckets["commitments"]),
        ("Follow-up threads", buckets["followups"]),
    ]
    for title, records in sections:
        print(title)
        if not records:
            print("  none found")
            print()
            continue
        for record in records[: args.limit]:
            ts = record.get("timestamp", "")[:19].replace("T", " ")
            source = record.get("source", "?")
            print(f"  - {ts} [{source}] {compact(record.get('content', ''), 160)}")
        print()
    return 0


def command_recall(args) -> int:
    search_args = [args.query, "--since", args.since] if args.since else [args.query]
    if args.source:
        search_args.extend(["--source", args.source])
    return run_script("search.py", search_args)


def command_context(args) -> int:
    state = read_json(STATE_FILE, {})
    print("Immortal Context Pack")
    print()
    print("Use this as task-local context, not as a permanent system prompt.")
    print()
    print("Profile baseline:")
    profile_path = PROFILE_COMPACT_MD if PROFILE_COMPACT_MD.exists() else PROFILE_MD
    if profile_path.exists():
        text = profile_path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines()[: args.profile_lines]:
            print(line)
    elif SOUL_FILE.exists():
        text = SOUL_FILE.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines()[:80]:
            print(line)
    else:
        print("Profile and digital soul are missing. Run immortal.py profile after capture is healthy.")
    print()
    print("Runtime state:")
    print(f"- Last collect: {local_time(state.get('last_collect'))}")
    print(f"- Total records: {state.get('total_records', 'unknown')}")
    print()
    if PROFILE_NUWA_MD.exists():
        print("Nuwa thinking profile excerpt:")
        text = PROFILE_NUWA_MD.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines()[: args.nuwa_lines]:
            print(line)
        print()
    if DIGEST_MD.exists():
        print("Recent digest excerpt:")
        text = DIGEST_MD.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines()[: args.digest_lines]:
            print(line)
        print()
    if PEOPLE_MD.exists():
        print("People index entrypoint:")
        print(f"- Full file: {PEOPLE_MD}")
        text = PEOPLE_MD.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines()[: args.people_lines]:
            print(line)
        print()
    if args.with_recall:
        print("Relevant recall:")
        sys.stdout.flush()
        run_script("search.py", [args.query, "--since", args.since])
    else:
        print("Relevant recall: skipped for speed. Run with --with-recall, or use:")
        print(f"python3 {SKILL_DIR / 'immortal.py'} recall {json.dumps(args.query, ensure_ascii=False)} --since {args.since}")
    return 0


def command_dashboard(_args) -> int:
    path = IMMORTAL_DIR / "dashboard.html"
    if not path.exists():
        run_script("dashboard.py")
    print(path)
    return 0


def write_daily_backup_script(config: dict) -> Path:
    vault = configured_vault_dir(config)
    script = vault / "daily-backup.sh"
    log_path = vault / "backup.log"
    script.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                "# Immortal daily automation entrypoint.",
                f"LOG={json.dumps(str(log_path))}",
                f"CODEX_IMMORTAL={json.dumps(str(SKILL_DIR / 'immortal.py'))}",
                'export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"',
                'echo "[$(date \'+%Y-%m-%d %H:%M:%S\')] === immortal daily run ===" >> "$LOG"',
                'python3 "$CODEX_IMMORTAL" run >> "$LOG" 2>&1',
                "STATUS=$?",
                'python3 "$CODEX_IMMORTAL" feedback --run-status "$STATUS" --notify >> "$LOG" 2>&1',
                "FEEDBACK_STATUS=$?",
                'echo "[$(date \'+%Y-%m-%d %H:%M:%S\')] === finished status=$STATUS feedback_status=$FEEDBACK_STATUS ===" >> "$LOG"',
                'echo "" >> "$LOG"',
                'exit "$STATUS"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def write_daily_launch_agent(config: dict, script: Path) -> Path:
    label = daily_launch_agent_label(config)
    plist_path = daily_launch_agent_path(label)
    intervals = [
        {"Hour": int(item["hour"]), "Minute": int(item["minute"])}
        for item in daily_schedule(config)
    ]
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", str(script)],
        "WorkingDirectory": str(SKILL_DIR),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(configured_vault_dir(config) / "launchd-daily-backup.out.log"),
        "StandardErrorPath": str(configured_vault_dir(config) / "launchd-daily-backup.err.log"),
        "ProcessType": "Background",
    }
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)
    return plist_path


def command_daily_install(_args) -> int:
    config = load_config()
    script = write_daily_backup_script(config)
    plist_path = write_daily_launch_agent(config, script)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True, timeout=10)
    load_result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True, timeout=10)
    print("Immortal daily automation")
    print()
    print(f"Script: {script}")
    print(f"LaunchAgent: {plist_path}")
    print(f"Label: {daily_launch_agent_label(config)}")
    print("Schedule:")
    for item in daily_schedule(config):
        print(f"- {int(item['hour']):02d}:{int(item['minute']):02d}")
    if load_result.returncode == 0:
        print("Status: loaded")
        return 0
    print("Status: plist written, but launchctl load returned:")
    print((load_result.stderr or load_result.stdout or "").strip())
    return 2


def command_daily_uninstall(_args) -> int:
    config = load_config()
    label = daily_launch_agent_label(config)
    plist_path = daily_launch_agent_path(label)
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True, timeout=10)
        plist_path.unlink()
        print(f"Removed LaunchAgent: {plist_path}")
        return 0
    print(f"LaunchAgent not found: {plist_path}")
    return 0


def command_daily_status(_args) -> int:
    ok, lines = check_crontab()
    print("Immortal daily automation status")
    print()
    for line in lines:
        print(f"- {line}")
    if not lines:
        print("- no immortal scheduler found")
    return 0 if ok else 2


def command_feishu_clean(args) -> int:
    return run_script("feishu_clean.py", args.feishu_clean_args)


def command_feishu_distill(args) -> int:
    return run_script("feishu_distill.py", args.feishu_distill_args)


def command_feishu_mirror(args) -> int:
    forwarded = list(args.feishu_mirror_args)
    guard_flags = {"--expected-user-name", "--expected-user-open-id", "--reject-user-name"}
    has_guard = any(part in guard_flags or any(part.startswith(flag + "=") for flag in guard_flags) for part in forwarded)
    if not has_guard:
        forwarded = [*feishu_guard_args(load_config()), *forwarded]
    return run_script("feishu_drive_mirror.py", forwarded)


def command_feishu_mirror_status(args) -> int:
    coverage = read_json(FEISHU_DRIVE_MIRROR_COVERAGE, {})
    counters = coverage.get("counters") if isinstance(coverage, dict) else {}
    print("Feishu Drive Mirror Status")
    print()
    print(f"Mirror: {FEISHU_DRIVE_MIRROR_DIR}")
    print(f"Coverage: {fmt_size(FEISHU_DRIVE_MIRROR_COVERAGE)}")
    print(f"Generated: {local_time(coverage.get('generated_at') if isinstance(coverage, dict) else None)}")
    print(f"Objects: {counters.get('objects', 0) if isinstance(counters, dict) else 0}")
    print(f"Jobs: {counters.get('jobs', 0) if isinstance(counters, dict) else 0}")
    print(f"Failures log rows: {coverage.get('failures', 0) if isinstance(coverage, dict) else 0}")
    print()
    if not FEISHU_DRIVE_MIRROR_DB.exists():
        print("Database: missing")
        return 1
    try:
        conn = sqlite3.connect(FEISHU_DRIVE_MIRROR_DB)
        conn.row_factory = sqlite3.Row
        print("Object types:")
        for row in conn.execute("select obj_type, count(*) c from objects group by obj_type order by c desc"):
            print(f"  - {row['obj_type'] or 'unknown'}: {row['c']}")
        print()
        print("Jobs:")
        open_jobs = 0
        error_jobs = 0
        dead_jobs = 0
        for row in conn.execute("select action, status, count(*) c from jobs group by action,status order by action,status"):
            print(f"  - {row['action']} / {row['status']}: {row['c']}")
            if row["status"] in {"pending", "error"}:
                open_jobs += int(row["c"])
            if row["status"] == "error":
                error_jobs += int(row["c"])
            if row["status"] == "dead":
                dead_jobs += int(row["c"])
        print()
        print(f"Open jobs: {open_jobs}")
        print(f"Error jobs: {error_jobs} (retryable by running feishu-mirror again)")
        print(f"Dead jobs: {dead_jobs} (not retried automatically; inspect failures.jsonl before resetting)")
        if args.json:
            payload = {
                "coverage": coverage,
                "open_jobs": open_jobs,
                "error_jobs": error_jobs,
                "dead_jobs": dead_jobs,
                "db": str(FEISHU_DRIVE_MIRROR_DB),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if open_jobs == 0 and error_jobs == 0 and dead_jobs == 0 else 2
    except Exception as exc:
        print(f"Database error: {exc}")
        return 1


def command_feishu_mirror_worker(args) -> int:
    return run_script("feishu_mirror_worker.py", args.feishu_mirror_worker_args)


def command_cc_worker(args) -> int:
    return run_script("cc_worker.py", args.cc_worker_args)


def command_init(args) -> int:
    config = load_config()
    changed = False

    if args.owner_name is not None:
        config["owner_name"] = args.owner_name.strip()
        changed = True
    if args.owner_display_name is not None:
        config["owner_display_name"] = args.owner_display_name.strip()
        changed = True
    if args.alias:
        config["owner_aliases"] = args.alias
        changed = True
    if args.primary_account is not None:
        config["primary_account"] = args.primary_account.strip()
        changed = True
    if args.vault_dir is not None:
        config["vault_dir"] = str(Path(args.vault_dir).expanduser())
        changed = True

    feishu = config.setdefault("feishu", {})
    if args.feishu_expected_user_name is not None:
        feishu["expected_user_name"] = args.feishu_expected_user_name.strip()
        changed = True
    if args.feishu_expected_user_open_id is not None:
        feishu["expected_user_open_id"] = args.feishu_expected_user_open_id.strip()
        changed = True
    if args.feishu_reject_user_name:
        feishu["reject_user_names"] = args.feishu_reject_user_name
        changed = True

    role_defaults = config.setdefault("role_defaults", {})
    if args.default_goal is not None:
        role_defaults["goal"] = args.default_goal.strip()
        changed = True
    if args.default_mode is not None:
        role_defaults["mode"] = args.default_mode
        changed = True

    if not changed and not CONFIG_FILE.exists():
        changed = True

    path = save_config(config)
    vault = configured_vault_dir(config)
    for rel in [
        "daily",
        "summaries",
        "files",
        "reviewed",
        "roles",
        "sessions",
        "exports",
        "feishu/clean",
        "feishu/distilled",
        "people",
        "relationships",
        "quality",
        "digests",
        "product",
    ]:
        (vault / rel).mkdir(parents=True, exist_ok=True)
    sources = vault / "sources.json"
    if not sources.exists():
        sources.write_text(json.dumps({"sources": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("Immortal config initialized")
    print()
    print(f"Config: {path}")
    print(f"Vault: {vault}")
    print(f"Owner: {owner_display_name(config)}")
    aliases = owner_aliases(config)
    print(f"Aliases: {', '.join(aliases) if aliases else 'missing'}")
    guards = feishu_guard_args(config)
    print(f"Feishu guard: {'configured' if guards else 'not configured'}")
    print()
    print("Next:")
    print("  python3 ~/.codex/skills/immortal/immortal.py train --smoke")
    print("  python3 ~/.codex/skills/immortal/immortal.py agent-factory")
    return 0


def command_train(args) -> int:
    config = load_config()
    vault = configured_vault_dir(config)
    vault.mkdir(parents=True, exist_ok=True)
    state = read_json(STATE_FILE, {})

    def mark_state(name: str) -> None:
        mapping = {
            "collect": "last_collect",
            "smoke": "last_collect",
            "feishu": "last_feishu_collect",
            "feishu-clean": "last_feishu_clean",
            "feishu-distill": "last_feishu_distill",
            "profile-auto-review": "last_profile_auto_review",
            "profile-attribution-audit": "last_profile_attribution_audit",
            "profile": "last_profile",
            "profile-nuwa": "last_profile_nuwa",
            "people": "last_people_index",
            "relationships": "last_relationship_index",
            "quality": "last_quality",
            "digest": "last_digest",
            "product": "last_product_brief",
            "role-distill": "last_role_distill",
            "task-compile": "last_task_compile",
        }
        key = mapping.get(name)
        if key:
            state[key] = datetime.now(timezone.utc).isoformat()

    def refresh_total_records() -> None:
        index_file = vault / "index.jsonl"
        if index_file.exists():
            try:
                state["total_records"] = sum(1 for _ in index_file.open("r", encoding="utf-8", errors="ignore"))
            except OSError:
                pass

    steps: list[tuple[str, list[str], bool]] = []
    if args.smoke:
        smoke_record = {
            "id": "smoke-install-record",
            "source": "immortal-smoke",
            "project": "",
            "session_id": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "smoke",
            "role": "user",
            "content": f"{owner_display_name(config)} initialized Immortal and wants a recoverable personal memory vault.",
        }
        daily_dir = vault / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for target in [vault / "index.jsonl", daily_dir / f"{date_key}.jsonl"]:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(smoke_record, ensure_ascii=False) + "\n")
        mark_state("smoke")
        refresh_total_records()
        print(f"Smoke record written to {vault / 'index.jsonl'}")
    else:
        steps.append(("collect", [sys.executable, str(SKILL_DIR / "collect.py")], True))

    if args.with_feishu:
        guard = feishu_guard_args(config)
        if not guard and not args.allow_current_feishu_account:
            print("Feishu collection skipped: configure expected user in `immortal.py init` or pass --allow-current-feishu-account.")
        else:
            feishu_args = guard or ["--allow-current-account"]
            feishu_args.extend(["--days", str(args.feishu_days), "--max-chats", str(args.feishu_max_chats), "--max-messages", str(args.feishu_max_messages)])
            steps.append(("feishu", [sys.executable, str(SKILL_DIR / "feishu_collect.py"), *feishu_args], False))
            steps.extend(
                [
                    ("feishu-clean", [sys.executable, str(SKILL_DIR / "feishu_clean.py")], False),
                    ("feishu-distill", [sys.executable, str(SKILL_DIR / "feishu_distill.py")], False),
                    ("profile-auto-review", [sys.executable, str(SKILL_DIR / "profile_auto_review.py"), "--reconsider-rejected"], False),
                    ("profile-attribution-audit", [sys.executable, str(SKILL_DIR / "profile_attribution_audit.py"), "--apply"], False),
                ]
            )

    steps.extend(
        [
            ("profile", [sys.executable, str(SKILL_DIR / "profile.py")], False),
            ("profile-nuwa", [sys.executable, str(SKILL_DIR / "profile_nuwa.py")], False),
            ("people", [sys.executable, str(SKILL_DIR / "people_index.py")], False),
            ("relationships", [sys.executable, str(SKILL_DIR / "relationship_index.py")], False),
            ("quality", [sys.executable, str(SKILL_DIR / "quality_report.py")], False),
            ("digest", [sys.executable, str(SKILL_DIR / "daily_digest.py")], False),
            ("product", [sys.executable, str(SKILL_DIR / "product_brief.py")], False),
            ("timeline", [sys.executable, str(SKILL_DIR / "timeline.py")], False),
            ("dashboard", [sys.executable, str(SKILL_DIR / "dashboard.py")], False),
        ]
    )

    role_defaults = config.get("role_defaults") if isinstance(config.get("role_defaults"), dict) else {}
    goal = args.goal or str(role_defaults.get("goal") or "写稿审稿流程")
    mode = args.mode or str(role_defaults.get("mode") or "auto")
    if args.build_role:
        task_cmd = [
            sys.executable,
            str(SKILL_DIR / "task_compile.py"),
            goal,
            "--mode",
            mode,
        ]
        steps.append(("task-compile", task_cmd, False))

    print("Immortal training pipeline")
    print()
    failures: list[str] = []
    attention: list[str] = []
    for name, cmd, required in steps:
        print(f"==> {name}")
        result = subprocess.run(cmd, text=True, cwd=str(SKILL_DIR))
        if result.returncode == 0:
            mark_state(name)
            refresh_total_records()
            continue
        if result.returncode == 2 and name in {"profile-nuwa", "role-distill", "task-compile", "quality", "product"}:
            mark_state(name)
            refresh_total_records()
            attention.append(name)
            continue
        if required:
            failures.append(name)
            break
        attention.append(name)

    print()
    state["errors"] = failures[-10:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if failures:
        print(f"Training failed at: {', '.join(failures)}")
        return 1
    if attention:
        print(f"Training completed with attention: {', '.join(attention)}")
        if args.smoke:
            print("Smoke mode treats attention as expected because the vault has almost no real user data yet.")
            return 0
        return 2
    print("Training completed")
    return 0


def command_package(args) -> int:
    return run_script("package_tool.py", args.package_args)


def command_profile_merge(args) -> int:
    return run_script("profile_merge.py", args.profile_merge_args)


def command_profile_auto_review(args) -> int:
    return run_script("profile_auto_review.py", args.profile_auto_review_args)


def command_profile_nuwa(args) -> int:
    code = run_script("profile_nuwa.py", args.profile_nuwa_args)
    if code in {0, 2}:
        state = read_json(STATE_FILE, {})
        state["last_profile_nuwa"] = datetime.now(timezone.utc).isoformat()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


def command_role_distill(args) -> int:
    code = run_script("role_distill.py", args.role_distill_args)
    if code in {0, 2}:
        state = read_json(STATE_FILE, {})
        state["last_role_distill"] = datetime.now(timezone.utc).isoformat()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


def command_task_compile(args) -> int:
    code = run_script("task_compile.py", args.task_compile_args)
    if code in {0, 2}:
        state = read_json(STATE_FILE, {})
        state["last_task_compile"] = datetime.now(timezone.utc).isoformat()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


def command_profile_review(args) -> int:
    return run_script("profile_review.py", args.profile_review_args)


def command_agent_factory(args) -> int:
    server_args = ["--host", args.host, "--port", str(args.port)]
    if args.open:
        server_args.append("--open")
    print(f"Task context compiler: http://{args.host}:{args.port}/agent-factory")
    return run_script("profile_review.py", server_args)


def command_agent_entry(_args) -> int:
    return run_script("agent_bridge.py", ["entry"])


def command_agent_context(args) -> int:
    bridge_args = ["context", args.query, "--since", args.since, "--timeout", str(args.timeout)]
    if args.with_recall:
        bridge_args.append("--with-recall")
    if args.output:
        bridge_args.extend(["--output", args.output])
    if args.print:
        bridge_args.append("--print")
    return run_script("agent_bridge.py", bridge_args)


def command_agent_http(args) -> int:
    server_args = ["http", "--host", args.host, "--port", str(args.port)]
    if args.token:
        server_args.extend(["--token", args.token])
    if args.quiet:
        server_args.append("--quiet")
    if args.unsafe_no_token:
        server_args.append("--unsafe-no-token")
    print(f"Immortal Agent Bridge HTTP: http://{args.host}:{args.port}")
    return run_script("agent_bridge_server.py", server_args)


def command_agent_mcp(_args) -> int:
    return run_script("agent_bridge_server.py", ["mcp"])


def command_agent_audit(args) -> int:
    audit_args = ["audit", "--limit", str(args.limit)]
    if args.json:
        audit_args.append("--json")
    return run_script("agent_bridge_server.py", audit_args)


def command_people(args) -> int:
    return run_script("people_index.py", args.people_args)


def command_relationships(args) -> int:
    code = run_script("relationship_index.py", args.relationship_args)
    if code == 0:
        state = read_json(STATE_FILE, {})
        state["last_relationship_index"] = datetime.now(timezone.utc).isoformat()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


def command_quality(args) -> int:
    code = run_script("quality_report.py", args.quality_args)
    if code == 0:
        state = read_json(STATE_FILE, {})
        state["last_quality"] = datetime.now(timezone.utc).isoformat()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


def command_digest(_args) -> int:
    return run_script("daily_digest.py")


def command_getnote_diary(args) -> int:
    sync_args = ["sync"]
    for date in getattr(args, "date", []) or []:
        sync_args.extend(["--date", date])
    if getattr(args, "yesterday", False):
        sync_args.append("--yesterday")
    if getattr(args, "all", False):
        sync_args.append("--all")
    if getattr(args, "since", ""):
        sync_args.extend(["--since", args.since])
    if getattr(args, "include_today", False):
        sync_args.append("--include-today")
    if getattr(args, "limit", 0):
        sync_args.extend(["--limit", str(args.limit)])
    if getattr(args, "topic_name", ""):
        sync_args.extend(["--topic-name", args.topic_name])
    if getattr(args, "topic_description", ""):
        sync_args.extend(["--topic-description", args.topic_description])
    for tag in getattr(args, "tag", []) or []:
        sync_args.extend(["--tag", tag])
    if getattr(args, "force", False):
        sync_args.append("--force")
    if getattr(args, "dry_run", False):
        sync_args.append("--dry-run")
    if getattr(args, "continue_on_error", False):
        sync_args.append("--continue-on-error")
    if getattr(args, "delay", None) is not None:
        sync_args.extend(["--delay", str(args.delay)])
    if getattr(args, "timeout", None) is not None:
        sync_args.extend(["--timeout", str(args.timeout)])
    if getattr(args, "retries", None) is not None:
        sync_args.extend(["--retries", str(args.retries)])
    if getattr(args, "rate_limit_sleep", None) is not None:
        sync_args.extend(["--rate-limit-sleep", str(args.rate_limit_sleep)])
    if getattr(args, "missing_limit", 0):
        sync_args.extend(["--missing-limit", str(args.missing_limit)])
    if getattr(args, "no_latest", False):
        sync_args.append("--no-latest")
    code = run_script("getnote_sync.py", sync_args)
    if code == 0:
        latest = read_json(GETNOTE_LATEST_JSON, {})
        state = read_json(STATE_FILE, {})
        now_iso = datetime.now(timezone.utc).isoformat()
        state["last_getnote_diary_sync"] = latest.get("generated_at") or now_iso
        state["last_getnote_diary_status"] = latest.get("status") or "ok"
        state["last_getnote_diary_date"] = latest.get("latest_date") or ""
        results = latest.get("results") if isinstance(latest.get("results"), list) else []
        if results:
            state["last_getnote_diary_note_id"] = results[-1].get("note_id") or ""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


def command_getnote_status(_args) -> int:
    return run_script("getnote_sync.py", ["status"])


def command_getnote_prune_empty(args) -> int:
    prune_args = ["prune-empty"]
    if args.limit:
        prune_args.extend(["--limit", str(args.limit)])
    if args.dry_run:
        prune_args.append("--dry-run")
    if args.continue_on_error:
        prune_args.append("--continue-on-error")
    prune_args.extend(["--delay", str(args.delay), "--timeout", str(args.timeout)])
    return run_script("getnote_sync.py", prune_args)


def add_getnote_sync_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", action="append", default=[], help="Date to sync, YYYY-MM-DD. Repeatable")
    parser.add_argument("--yesterday", action="store_true", help="Sync yesterday in local timezone")
    parser.add_argument("--all", action="store_true", help="Backfill all available daily summaries")
    parser.add_argument("--since", default="", help="Backfill from YYYY-MM-DD")
    parser.add_argument("--include-today", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--topic-name", default="")
    parser.add_argument("--topic-description", default="")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--delay", type=float, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--retries", type=int, default=None)
    parser.add_argument("--rate-limit-sleep", type=float, default=None)
    parser.add_argument("--missing-limit", type=int, default=0)
    parser.add_argument("--no-latest", action="store_true")


def command_feedback(args) -> int:
    feedback_args: list[str] = []
    if args.vault_dir:
        feedback_args.extend(["--vault-dir", args.vault_dir])
    if args.run_status is not None:
        feedback_args.extend(["--run-status", str(args.run_status)])
    if args.notify:
        feedback_args.append("--notify")
    if args.print:
        feedback_args.append("--print")
    return run_script("feedback_report.py", feedback_args)


def command_product(_args) -> int:
    code = run_script("product_brief.py")
    if code == 0:
        write_state_key("last_product_brief", datetime.now(timezone.utc).isoformat())
    return code


def command_export(args) -> int:
    manifest = create_export(
        vault_dir=args.vault_dir,
        output_dir=args.output_dir,
        include_raw=bool(args.include_raw),
    )
    totals = manifest.get("totals") or {}
    write_state_key("last_portable_export", manifest.get("generated_at"))
    write_state_key("last_portable_export_dir", manifest.get("export_dir"))
    write_state_key("last_portable_export_files", totals.get("files"))
    write_state_key("last_portable_export_bytes", totals.get("bytes"))
    print("Immortal portable export")
    print()
    print(f"Export: {manifest.get('export_dir')}")
    print(f"Manifest: {Path(str(manifest.get('export_dir'))) / 'manifest.json'}")
    print(f"Files: {int(totals.get('files') or 0):,}")
    print(f"Size: {fmt_bytes(totals.get('bytes'))}")
    warnings = manifest.get("warnings") or []
    print(f"Warnings: {len(warnings)}")
    for warning in warnings[:12]:
        print(f"  - {warning}")
    if len(warnings) > 12:
        print(f"  - ... {len(warnings) - 12} more")
    print()
    print("Next: run `python3 ~/.codex/skills/immortal/immortal.py restore-check \"<export-path>\"` before trusting a restore.")
    return 0


def command_backup_status(args) -> int:
    status = get_backup_status(args.vault_dir, verify=bool(args.verify))
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if status.get("ok") else 1
    ok, detail = portable_backup_detail(status, max_age_hours=args.max_age_hours)
    latest = status.get("latest_export") or {}
    totals = latest.get("totals") or {}
    print("Immortal Backup Status")
    print()
    print(f"Status: {'OK' if ok else 'FAIL'}")
    print(f"Mode: {status.get('mode', 'manifest-only')}")
    print(f"Latest export: {latest.get('export_dir') or 'missing'}")
    print(f"Generated: {local_time(latest.get('generated_at'))}")
    print(f"Files: {int(totals.get('files') or 0):,}")
    print(f"Size: {fmt_bytes(totals.get('bytes'))}")
    print(f"Detail: {detail}")
    warnings = status.get("warnings") or (status.get("check") or {}).get("warnings") or []
    if warnings:
        print(f"Warnings: {', '.join(map(str, warnings[:8]))}")
    return 0 if ok else 1


def command_restore_guide(args) -> int:
    status = get_backup_status(args.vault_dir, verify=False)
    latest = status.get("latest_export") or {}
    export_dir = latest.get("export_dir") or "<export-dir>"
    print("Immortal Restore Guide")
    print()
    print("1. Copy or mount the export directory on the new machine.")
    print(f"   Latest known export: {export_dir}")
    print()
    print("2. Install the Immortal Codex skill.")
    print("   python3 install.py --owner-display-name \"Your Name\" --alias \"Your Alias\"")
    print()
    print("3. Verify the export before trusting it.")
    print(f"   python3 ~/.codex/skills/immortal/immortal.py restore-check {json.dumps(str(export_dir), ensure_ascii=False)}")
    print()
    print("4. Restore the vault files to ~/.immortal, then rebuild derived views.")
    print("   python3 ~/.codex/skills/immortal/immortal.py train")
    print("   python3 ~/.codex/skills/immortal/immortal.py health")
    print()
    print("5. Reinstall local automation on the new machine.")
    print("   python3 ~/.codex/skills/immortal/immortal.py daily-install")
    print("   python3 ~/.codex/skills/immortal/immortal.py daily-status")
    return 0


def command_restore_check(args) -> int:
    result = restore_check(args.export_path, strict=bool(args.strict))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    print("Immortal Restore Check")
    print()
    print(f"Status: {'OK' if result.get('ok') else 'FAIL'}")
    print(f"Export: {result.get('export_dir')}")
    print(f"Manifest: {result.get('manifest_path')}")
    print(f"Generated: {local_time(result.get('generated_at'))}")
    print(f"Checked files: {int(result.get('checked_files') or 0):,} / {int(result.get('expected_files') or 0):,}")
    missing = result.get("missing") or []
    mismatched = result.get("mismatched") or []
    warnings = result.get("warnings") or []
    print(f"Missing: {len(missing)}")
    print(f"Mismatched: {len(mismatched)}")
    if warnings:
        print(f"Warnings: {', '.join(map(str, warnings[:8]))}")
    for item in missing[:8]:
        print(f"  missing: {item.get('relpath')}")
    for item in mismatched[:8]:
        print(f"  mismatched: {item.get('relpath')}")
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="immortal", description="Codex entry for the Immortal Skill")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current memory library state").set_defaults(func=command_status)
    init = sub.add_parser("init", help="Initialize this installation with the current user's identity and vault config")
    init.add_argument("--owner-name", default=None, help="Legal or internal name for the owner")
    init.add_argument("--owner-display-name", default=None, help="Display name used in generated roles")
    init.add_argument("--alias", action="append", default=[], help="Owner alias; repeat for multiple aliases")
    init.add_argument("--primary-account", default=None, help="Primary writing/work account label")
    init.add_argument("--vault-dir", default=None, help="Override memory vault directory; defaults to ~/.immortal")
    init.add_argument("--feishu-expected-user-name", default=None)
    init.add_argument("--feishu-expected-user-open-id", default=None)
    init.add_argument("--feishu-reject-user-name", action="append", default=[])
    init.add_argument("--default-goal", default=None, help="Default scenario goal for role generation")
    init.add_argument(
        "--default-mode",
        default=None,
        choices=["auto", "advisor", "writer", "reviewer", "business", "project", "shadow", "custom"],
    )
    init.set_defaults(func=command_init)

    train = sub.add_parser("train", help="Run the first-use capture, clean, profile, quality, dashboard, and optional task context pipeline")
    train.add_argument("--smoke", action="store_true", help="Write a tiny local smoke record instead of collecting from real sources")
    train.add_argument("--with-feishu", action="store_true", help="Also collect and distill Feishu/Lark data")
    train.add_argument("--allow-current-feishu-account", action="store_true", help="Allow Feishu collection without configured expected user guard")
    train.add_argument("--feishu-days", type=int, default=7)
    train.add_argument("--feishu-max-chats", type=int, default=5)
    train.add_argument("--feishu-max-messages", type=int, default=200)
    train.add_argument("--build-role", action="store_true", help="Deprecated alias: compile a short-lived task session after profile refresh")
    train.add_argument("--goal", default=None, help="Scenario goal for the optional task session")
    train.add_argument(
        "--mode",
        default=None,
        choices=["auto", "advisor", "writer", "reviewer", "business", "project", "shadow", "custom"],
    )
    train.set_defaults(func=command_train)

    sub.add_parser("doctor", help="Check whether the loss-prevention baseline is healthy").set_defaults(func=command_doctor)
    health = sub.add_parser("health", help="Check whether the daily automated memory loop is current")
    health.add_argument("--max-age-hours", type=float, default=30)
    health.set_defaults(func=command_health)
    sub.add_parser("run", help="Run capture, summary, dashboard, distill, and cleanup orchestration").set_defaults(
        func=lambda args: run_script("orchestrator.py")
    )
    sub.add_parser("backup", help="Alias for run").set_defaults(func=lambda args: run_script("orchestrator.py"))
    sub.add_parser("distill", help="Regenerate digital soul").set_defaults(func=lambda args: run_script("distill.py"))
    sub.add_parser("profile", help="Build structured owner profile from distilled memories and raw evidence").set_defaults(
        func=lambda args: run_script("profile.py")
    )
    sub.add_parser("cleanup", help="Run cleanup").set_defaults(func=lambda args: run_script("cleanup.py"))
    sub.add_parser("cron", help="Check cron health").set_defaults(func=lambda args: run_script("cron_check.py"))
    sub.add_parser("soul", help="Print digital soul").set_defaults(func=lambda args: run_script("soul.py"))
    sub.add_parser("dashboard", help="Print dashboard path, generating it if needed").set_defaults(func=command_dashboard)
    sub.add_parser("daily-install", help="Install or refresh the local daily LaunchAgent automation").set_defaults(func=command_daily_install)
    sub.add_parser("daily-status", help="Show local daily LaunchAgent automation status").set_defaults(func=command_daily_status)
    sub.add_parser("daily-uninstall", help="Remove the configured local daily LaunchAgent automation").set_defaults(func=command_daily_uninstall)

    export = sub.add_parser("export", help="Create a portable export of the recovery-critical memory vault")
    export.add_argument("--vault-dir", default=None)
    export.add_argument("--output-dir", default=None)
    export.add_argument("--include-raw", action="store_true", help="Also include explicitly supported raw source folders")
    export.set_defaults(func=command_export)

    backup_status = sub.add_parser("backup-status", help="Show latest portable export status")
    backup_status.add_argument("--vault-dir", default=None)
    backup_status.add_argument("--verify", action="store_true", help="Run full SHA256 verification instead of manifest-only status")
    backup_status.add_argument("--max-age-hours", type=float, default=168)
    backup_status.add_argument("--json", action="store_true")
    backup_status.set_defaults(func=command_backup_status)

    restore = sub.add_parser("restore-check", help="Verify an exported backup before restore")
    restore.add_argument("export_path")
    restore.add_argument("--strict", action="store_true", help="Also warn on extra files not listed in the manifest")
    restore.add_argument("--json", action="store_true")
    restore.set_defaults(func=command_restore_check)

    restore_guide = sub.add_parser("restore-guide", help="Print the practical new-machine restore runbook")
    restore_guide.add_argument("--vault-dir", default=None)
    restore_guide.set_defaults(func=command_restore_guide)

    feishu = sub.add_parser("feishu", help="Collect Feishu/Lark data into the local memory vault")
    feishu.add_argument("feishu_args", nargs=argparse.REMAINDER)
    feishu.set_defaults(func=lambda args: run_script("feishu_collect.py", args.feishu_args))

    feishu_clean = sub.add_parser("feishu-clean", help="Build reviewable Feishu clean-layer artifacts")
    feishu_clean.add_argument("feishu_clean_args", nargs=argparse.REMAINDER)
    feishu_clean.set_defaults(func=command_feishu_clean)

    feishu_distill = sub.add_parser("feishu-distill", help="Build structured memory candidates from Feishu clean layer")
    feishu_distill.add_argument("feishu_distill_args", nargs=argparse.REMAINDER)
    feishu_distill.set_defaults(func=command_feishu_distill)

    feishu_mirror = sub.add_parser("feishu-mirror", help="Mirror visible Feishu Drive/Wiki/Docs resources read-only")
    feishu_mirror.add_argument("feishu_mirror_args", nargs=argparse.REMAINDER)
    feishu_mirror.set_defaults(func=command_feishu_mirror)

    feishu_mirror_status = sub.add_parser("feishu-mirror-status", help="Show Feishu Drive/Wiki/Docs mirror progress")
    feishu_mirror_status.add_argument("--json", action="store_true")
    feishu_mirror_status.set_defaults(func=command_feishu_mirror_status)

    feishu_mirror_worker = sub.add_parser("feishu-mirror-worker", help="Install/start/status the persistent Feishu mirror LaunchAgent")
    feishu_mirror_worker.add_argument("feishu_mirror_worker_args", nargs=argparse.REMAINDER)
    feishu_mirror_worker.set_defaults(func=command_feishu_mirror_worker)

    cc_worker = sub.add_parser("cc-worker", help="Run bounded low-cost Claude Code worker tasks")
    cc_worker.add_argument("cc_worker_args", nargs=argparse.REMAINDER)
    cc_worker.set_defaults(func=command_cc_worker)

    package = sub.add_parser("package", help="Build a sanitized installable zip for another Codex user")
    package.add_argument("package_args", nargs=argparse.REMAINDER)
    package.set_defaults(func=command_package)

    profile_merge = sub.add_parser("profile-merge", help="Merge checked profile candidates into reviewed long-term memory")
    profile_merge.add_argument("profile_merge_args", nargs=argparse.REMAINDER)
    profile_merge.set_defaults(func=command_profile_merge)

    profile_auto_review = sub.add_parser(
        "profile-auto-review",
        help="Auto-review and merge Feishu profile candidates into reviewed long-term memory",
    )
    profile_auto_review.add_argument("profile_auto_review_args", nargs=argparse.REMAINDER)
    profile_auto_review.set_defaults(func=command_profile_auto_review)

    profile_nuwa = sub.add_parser(
        "profile-nuwa",
        help="Build a Nuwa-style thinking profile from reviewed long-term memory",
    )
    profile_nuwa.add_argument("profile_nuwa_args", nargs=argparse.REMAINDER)
    profile_nuwa.set_defaults(func=command_profile_nuwa)

    distill_profile = sub.add_parser(
        "distill-profile",
        help="Alias for profile-nuwa; distill reviewed memory into mental models and heuristics",
    )
    distill_profile.add_argument("profile_nuwa_args", nargs=argparse.REMAINDER)
    distill_profile.set_defaults(func=command_profile_nuwa)

    role_distill = sub.add_parser(
        "role-distill",
        help="Explicitly promote a high-frequency workflow into a persistent scenario role package",
    )
    role_distill.add_argument("role_distill_args", nargs=argparse.REMAINDER)
    role_distill.set_defaults(func=command_role_distill)

    agent_build = sub.add_parser(
        "agent-build",
        help="Alias for role-distill; explicit persistent role promotion",
    )
    agent_build.add_argument("role_distill_args", nargs=argparse.REMAINDER)
    agent_build.set_defaults(func=command_role_distill)

    task_compile = sub.add_parser(
        "task-compile",
        help="Compile a short-lived task context session from the memory vault",
    )
    task_compile.add_argument("task_compile_args", nargs=argparse.REMAINDER)
    task_compile.set_defaults(func=command_task_compile)

    agent_session = sub.add_parser(
        "agent-session",
        help="Alias for task-compile; use this for ad hoc digital-agent work",
    )
    agent_session.add_argument("task_compile_args", nargs=argparse.REMAINDER)
    agent_session.set_defaults(func=command_task_compile)

    profile_review = sub.add_parser("profile-review", help="Open a local review desk for long-term profile candidates")
    profile_review.add_argument("profile_review_args", nargs=argparse.REMAINDER)
    profile_review.set_defaults(func=command_profile_review)

    agent_factory = sub.add_parser("agent-factory", help="Start the local task context compiler server")
    agent_factory.add_argument("--host", default="127.0.0.1")
    agent_factory.add_argument("--port", type=int, default=8765)
    agent_factory.add_argument("--open", action="store_true")
    agent_factory.set_defaults(func=command_agent_factory)

    sub.add_parser("agent-entry", help="Write the stable one-line entry file for other agents").set_defaults(func=command_agent_entry)

    agent_context = sub.add_parser("agent-context", help="Write a task-local context pack for another agent")
    agent_context.add_argument("query", nargs="?", default="当前任务")
    agent_context.add_argument("--since", default="2026-03-01")
    agent_context.add_argument("--with-recall", action="store_true")
    agent_context.add_argument("--output", default="")
    agent_context.add_argument("--timeout", type=int, default=240)
    agent_context.add_argument("--print", action="store_true")
    agent_context.set_defaults(func=command_agent_context)

    agent_http = sub.add_parser("agent-http", help="Start the local HTTP Agent Bridge")
    agent_http.add_argument("--host", default="127.0.0.1")
    agent_http.add_argument("--port", type=int, default=8799)
    agent_http.add_argument("--token", default="", help="Optional bearer token for non-health endpoints")
    agent_http.add_argument("--quiet", action="store_true")
    agent_http.add_argument("--unsafe-no-token", action="store_true", help="Allow non-loopback HTTP binding without a token")
    agent_http.set_defaults(func=command_agent_http)

    sub.add_parser("agent-mcp", help="Start the MCP stdio Agent Bridge").set_defaults(func=command_agent_mcp)

    agent_audit = sub.add_parser("agent-audit", help="Show recent HTTP/MCP Agent Bridge access events")
    agent_audit.add_argument("--limit", type=int, default=50)
    agent_audit.add_argument("--json", action="store_true")
    agent_audit.set_defaults(func=command_agent_audit)

    people = sub.add_parser("people", help="Build the person-facing memory index")
    people.add_argument("people_args", nargs=argparse.REMAINDER)
    people.set_defaults(func=command_people)

    relationships = sub.add_parser("relationships", help="Build the optional evidence network index")
    relationships.add_argument("relationship_args", nargs=argparse.REMAINDER)
    relationships.set_defaults(func=command_relationships)

    quality = sub.add_parser("quality", help="Build the read-only memory quality report")
    quality.add_argument("quality_args", nargs=argparse.REMAINDER)
    quality.set_defaults(func=command_quality)

    sub.add_parser("digest", help="Generate the latest daily change digest").set_defaults(func=command_digest)
    getnote_diary = sub.add_parser("getnote-diary", help="Generate and sync Immortal daily diaries into Get 笔记")
    add_getnote_sync_args(getnote_diary)
    getnote_diary.set_defaults(func=command_getnote_diary)
    getnote_sync = sub.add_parser("getnote-sync", help="Alias for getnote-diary")
    add_getnote_sync_args(getnote_sync)
    getnote_sync.set_defaults(func=command_getnote_diary)
    sub.add_parser("getnote-status", help="Show Get 笔记 diary sync status").set_defaults(func=command_getnote_status)
    getnote_prune = sub.add_parser("getnote-prune-empty", help="Delete Get 笔记 notes created from empty/no-record summaries")
    getnote_prune.add_argument("--limit", type=int, default=0)
    getnote_prune.add_argument("--dry-run", action="store_true")
    getnote_prune.add_argument("--continue-on-error", action="store_true")
    getnote_prune.add_argument("--delay", type=float, default=3.0)
    getnote_prune.add_argument("--timeout", type=int, default=30)
    getnote_prune.set_defaults(func=command_getnote_prune_empty)
    feedback = sub.add_parser("feedback", help="Generate the latest user-facing automation feedback report")
    feedback.add_argument("--vault-dir", default=None)
    feedback.add_argument("--run-status", type=int, default=None)
    feedback.add_argument("--notify", action="store_true")
    feedback.add_argument("--print", action="store_true")
    feedback.set_defaults(func=command_feedback)
    sub.add_parser("product", help="Generate the product-level operating brief").set_defaults(func=command_product)
    sub.add_parser("goal", help="Alias for product; show what this system is becoming").set_defaults(func=command_product)

    brief = sub.add_parser("brief", help="Generate a local daily brief from recent records")
    brief.add_argument("--days", type=int, default=2)
    brief.add_argument("--limit", type=int, default=8)
    brief.set_defaults(func=command_brief)

    recall = sub.add_parser("recall", help="Search historical traces for a decision or topic")
    recall.add_argument("query")
    recall.add_argument("--source", default=None)
    recall.add_argument("--since", default=None)
    recall.set_defaults(func=command_recall)

    search = sub.add_parser("search", help="Alias for recall")
    search.add_argument("query")
    search.add_argument("--source", default=None)
    search.add_argument("--since", default=None)
    search.set_defaults(func=command_recall)

    context = sub.add_parser("context", help="Build a compact task-local context pack")
    context.add_argument("query")
    context.add_argument("--since", default="2026-03-01")
    context.add_argument("--with-recall", action="store_true", help="Also run full recall search; slower on large vaults")
    context.add_argument("--profile-lines", type=int, default=120)
    context.add_argument("--nuwa-lines", type=int, default=80)
    context.add_argument("--digest-lines", type=int, default=80)
    context.add_argument("--people-lines", type=int, default=80)
    context.set_defaults(func=command_context)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "init":
        parser = build_parser()
        args = parser.parse_args(argv)
        return int(args.func(args) or 0)
    if argv and argv[0] == "train":
        parser = build_parser()
        args = parser.parse_args(argv)
        return int(args.func(args) or 0)
    if argv and argv[0] == "feishu-clean":
        return run_script("feishu_clean.py", argv[1:])
    if argv and argv[0] == "feishu-distill":
        return run_script("feishu_distill.py", argv[1:])
    if argv and argv[0] == "feishu-mirror":
        return run_script("feishu_drive_mirror.py", argv[1:])
    if argv and argv[0] == "feishu-mirror-status":
        return command_feishu_mirror_status(argparse.Namespace(json="--json" in argv[1:]))
    if argv and argv[0] == "feishu-mirror-worker":
        return run_script("feishu_mirror_worker.py", argv[1:])
    if argv and argv[0] == "cc-worker":
        return run_script("cc_worker.py", argv[1:])
    if argv and argv[0] == "package":
        return run_script("package_tool.py", argv[1:])
    if argv and argv[0] == "profile-merge":
        return run_script("profile_merge.py", argv[1:])
    if argv and argv[0] == "profile-auto-review":
        return run_script("profile_auto_review.py", argv[1:])
    if argv and argv[0] in {"profile-nuwa", "distill-profile"}:
        return command_profile_nuwa(argparse.Namespace(profile_nuwa_args=argv[1:]))
    if argv and argv[0] in {"role-distill", "agent-build"}:
        return command_role_distill(argparse.Namespace(role_distill_args=argv[1:]))
    if argv and argv[0] in {"task-compile", "agent-session"}:
        return command_task_compile(argparse.Namespace(task_compile_args=argv[1:]))
    if argv and argv[0] == "profile-review":
        return run_script("profile_review.py", argv[1:])
    if argv and argv[0] == "people":
        return run_script("people_index.py", argv[1:])
    if argv and argv[0] == "relationships":
        return command_relationships(argparse.Namespace(relationship_args=argv[1:]))
    if argv and argv[0] == "quality":
        return command_quality(argparse.Namespace(quality_args=argv[1:]))
    if argv and argv[0] == "digest":
        return run_script("daily_digest.py")
    if len(argv) >= 2 and argv[0] == "feishu" and argv[1] == "clean":
        return run_script("feishu_clean.py", argv[2:])
    if len(argv) >= 2 and argv[0] == "feishu" and argv[1] == "distill":
        return run_script("feishu_distill.py", argv[2:])
    if len(argv) >= 2 and argv[0] == "feishu" and argv[1] == "mirror":
        return run_script("feishu_drive_mirror.py", argv[2:])
    if len(argv) >= 2 and argv[0] == "feishu" and argv[1] == "mirror-status":
        return command_feishu_mirror_status(argparse.Namespace(json="--json" in argv[2:]))
    if len(argv) >= 2 and argv[0] == "feishu" and argv[1] == "mirror-worker":
        return run_script("feishu_mirror_worker.py", argv[2:])
    if argv and argv[0] == "feishu":
        return run_script("feishu_collect.py", argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
