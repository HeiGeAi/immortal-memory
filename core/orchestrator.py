#!/usr/bin/env python3
"""
永生记忆库 - 编排器 v0.3
全自动多时段采集 + 增量摘要 + 定期人格蒸馏 + 看板更新

修复 bug：
- last_summary 真正记录
- should_distill 改用 last_distill 时间差判断（不依赖小时）
- 解析新增记录数（从 collect.py 输出中提取）
- 添加错误处理与重试
"""

import sys
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
from config import feishu_daily_sources, feishu_guard_args
from process_utils import run_process
from runtime_telemetry import RuntimeTelemetry
from state_store import read_state as read_shared_state, update_state_atomic

IMMORTAL_DIR = Path.home() / ".immortal"
LOG_FILE = IMMORTAL_DIR / "backup.log"
STATE_FILE = IMMORTAL_DIR / "orchestrator_state.json"
LOCK_FILE = IMMORTAL_DIR / "orchestrator.lock"
# 维护冻结 marker：存在时跳过一切不可逆清理（修复窗口的保险丝，采集/摘要/画像不受影响）
FREEZE_MARKER = IMMORTAL_DIR / "MAINTENANCE_FREEZE_DESTRUCTIVE"
SKILL_DIR = Path(__file__).resolve().parent
GETNOTE_CONFIG = Path.home() / ".getnote" / "config.json"
GETNOTE_LATEST_JSON = IMMORTAL_DIR / "getnote" / "latest.json"
STABLE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
RUNTIME_TELEMETRY = RuntimeTelemetry(IMMORTAL_DIR / "runtime")
_ACTIVE_STAGE = ""
_STAGE_ERROR_COUNT = 0

REQUIRED_FAILURES = {
    "collect failed",
    "external source collect failed",
    "search index sync failed",
    "claims migration failed",
    "context compile failed",
    "cards build failed",
    "portable export failed",
    "portable restore-check failed",
}
REQUIRED_FAILURE_PREFIXES = (
    "required core script missing",
    "index integrity failed",
    "migration failed",
    "export verify failed",
)

REQUIRED_CORE_SCRIPTS = (
    "immortal.py",
    "collect.py",
    "summary.py",
    "distill.py",
    "profile.py",
    "profile_nuwa.py",
    "profile_attribution_audit.py",
    "people_index.py",
    "index_db.py",
    "relationship_index.py",
    "quality_report.py",
    "export_restore.py",
    "agent_bridge.py",
    "cleanup.py",
)

OPTIONAL_CONNECTOR_SCRIPTS = (
    "web_capture.py",
    "feishu_collect.py",
    "feishu_clean.py",
    "feishu_distill.py",
    "profile_auto_review.py",
    "feishu_drive_mirror.py",
    "obsidian_sync.py",
    "getnote_sync.py",
)

INACTIVE_COMPATIBILITY_SCRIPTS = (
    "daily_digest.py",
    "product_brief.py",
)

DISTILL_INTERVAL_DAYS = 1  # 每天蒸馏一次（数据变化大的话有意义）
CLEANUP_INTERVAL_DAYS = 7  # 每周清理一次磁盘
PORTABLE_EXPORT_INTERVAL_DAYS = 3  # 每 3 天生成一次可恢复便携包
AUTO_DIGITAL_SOUL_DISTILL = True  # 每日自动蒸馏 digital-soul.md（索引已瘦身，蒸馏快且开销可控）
FEISHU_INTERVAL_HOURS = 20  # 飞书源较重，每天最多跑一轮增量即可
FEISHU_MIRROR_INVENTORY_INTERVAL_HOURS = 24  # 云文档/Wiki 清单每天刷新一轮
FEISHU_MIRROR_DOWNLOAD_INTERVAL_HOURS = 12  # 文档导出限量跑，避免长时间占用 API
PROFILE_ATTRIBUTION_AUDIT_INTERVAL_HOURS = 20  # 每天自动剥离污染画像
WEB_CAPTURE_INTERVAL_HOURS = 4  # 浏览历史元信息轻量扫描，正文只走白名单或手动保存
OBSIDIAN_SYNC_INTERVAL_HOURS = 4  # Obsidian 阅读层定期刷新入口、索引和断链状态
GETNOTE_BACKFILL_INTERVAL_HOURS = 20  # 历史日记按 Get 笔记额度分批补齐
GETNOTE_BACKFILL_MISSING_LIMIT = 5
GETNOTE_PRUNE_EMPTY_INTERVAL_HOURS = 20  # 自动清理误同步的空日记
GETNOTE_PRUNE_EMPTY_LIMIT = 5
FEISHU_MIRROR_DOWNLOAD_ACTIONS = "fetch_doc,export_markdown,export_docx,export_xlsx,export_base,download_file"
FEISHU_MIRROR_DOWNLOAD_MAX_JOBS = 40
FEISHU_DAILY_BASE_ARGS = [
    "--days", "3",
    "--max-messages", "1000",
    "--max-members", "1000",
    "--chat-page-limit", "20",
    "--message-page-limit", "8",
    "--member-page-limit", "5",
    "--task-page-limit", "40",
    "--vc-page-limit", "6",
    "--meeting-artifact-limit", "80",
    "--meeting-note-doc-content-limit", "40",
    "--minutes-page-limit", "6",
    "--minutes-artifact-limit", "80",
    "--docs-page-limit", "20",
    "--doc-content-limit", "80",
]


def feishu_daily_args() -> list[str]:
    guards = feishu_guard_args()
    if not guards:
        return []
    return [
        *guards,
        "--sources",
        feishu_daily_sources(),
        *FEISHU_DAILY_BASE_ARGS,
    ]


def feishu_mirror_guard_args() -> list[str]:
    guards = feishu_guard_args()
    return guards if guards else []


class ControlJobCanceled(RuntimeError):
    """Raised only at an orchestrator stage boundary."""


def control_job_cancel_requested(
    job_id: str,
    *,
    runtime_dir=None,
) -> bool:
    scoped_job_id = str(job_id or "").strip()
    if not scoped_job_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scoped_job_id):
        return False
    root = Path(runtime_dir) if runtime_dir is not None else RUNTIME_TELEMETRY.runtime_dir
    marker = root / "cancel_requests" / f"{scoped_job_id}.json"
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("job_id") == scoped_job_id


def raise_if_control_job_canceled() -> None:
    job_id = os.environ.get("IMMORTAL_CONTROL_JOB_ID") or ""
    if control_job_cancel_requested(job_id):
        raise ControlJobCanceled(f"控制中心任务 {job_id} 已请求取消")


def log(msg: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def telemetry_stage(stage_id: str, label: str, errors: list) -> None:
    """Finish the previous broad stage and start the next evidence checkpoint."""
    global _ACTIVE_STAGE, _STAGE_ERROR_COUNT
    raise_if_control_job_canceled()
    if _ACTIVE_STAGE:
        added = max(0, len(errors) - _STAGE_ERROR_COUNT)
        status = "attention" if added else "success"
        summary = f"新增 {added} 个需关注项" if added else "阶段完成"
        RUNTIME_TELEMETRY.finish_stage(_ACTIVE_STAGE, status=status, summary=summary)
    RUNTIME_TELEMETRY.start_stage(stage_id, label)
    _ACTIVE_STAGE = stage_id
    _STAGE_ERROR_COUNT = len(errors)


def telemetry_finish_active(errors: list) -> None:
    global _ACTIVE_STAGE, _STAGE_ERROR_COUNT
    if not _ACTIVE_STAGE:
        return
    added = max(0, len(errors) - _STAGE_ERROR_COUNT)
    status = "attention" if added else "success"
    summary = f"新增 {added} 个需关注项" if added else "阶段完成"
    RUNTIME_TELEMETRY.finish_stage(_ACTIVE_STAGE, status=status, summary=summary)
    _ACTIVE_STAGE = ""
    _STAGE_ERROR_COUNT = 0


def telemetry_heartbeat_loop(stop_event: threading.Event, interval: float = 30.0) -> None:
    while not stop_event.wait(interval):
        RUNTIME_TELEMETRY.heartbeat()


def orchestration_status(errors: list[str]) -> tuple[str, int]:
    normalized = {str(item).strip() for item in errors}
    if normalized.intersection(REQUIRED_FAILURES) or any(
        error.startswith(prefix)
        for error in normalized
        for prefix in REQUIRED_FAILURE_PREFIXES
    ):
        return "failed", 1
    if normalized:
        return "attention", 0
    return "success", 0


def preflight_required_scripts(skill_dir: Optional[Path] = None) -> list[str]:
    root = Path(skill_dir) if skill_dir is not None else SKILL_DIR
    return [
        name
        for name in REQUIRED_CORE_SCRIPTS
        if not (root / name).is_file()
    ]


def child_env() -> dict:
    env = dict(os.environ)
    existing_path = env.get("PATH", "")
    env["PATH"] = f"{STABLE_PATH}:{existing_path}" if existing_path else STABLE_PATH
    return env


def run_script(name: str, *args, timeout: int = 600, want_stdout: bool = False) -> tuple:
    """运行脚本，返回 (成功与否, 输出)。

    want_stdout=True 时只返回 stdout（给 --json 脚本用）：默认把 stdout+stderr 合并会让
    任何 stderr 警告污染 JSON 解析，造成 Obsidian 这类阶段"假失败"。日志类调用保持默认合并。
    """
    cmd = ["python3", str(SKILL_DIR / name)] + list(args)
    try:
        result = run_process(cmd, capture_output=True, text=True, timeout=timeout, env=child_env())
        out = result.stdout if want_stdout else (result.stdout + result.stderr)
        return (result.returncode == 0, out)
    except subprocess.TimeoutExpired:
        return (False, f"Timeout after {timeout}s")
    except Exception as e:
        return (False, str(e))


def run_script_rc(name: str, *args, timeout: int = 600) -> tuple:
    """同 run_script，但返回 (真实退出码, 输出)，供需要区分 partial(2) 的调用方使用。"""
    cmd = ["python3", str(SKILL_DIR / name)] + list(args)
    try:
        result = run_process(cmd, capture_output=True, text=True, timeout=timeout, env=child_env())
        return (result.returncode, result.stdout + result.stderr)
    except subprocess.TimeoutExpired:
        return (1, f"Timeout after {timeout}s")
    except Exception as e:
        return (1, str(e))


def load_state() -> dict:
    if STATE_FILE.exists():
        state = read_shared_state(STATE_FILE, {})
        if isinstance(state, dict):
            return state
    return {
        "last_collect": None,
        "last_summary": None,
        "last_distill": None,
        "last_profile": None,
        "last_profile_nuwa": None,
        "last_role_distill": None,
        "last_task_compile": None,
        "last_profile_auto_review": None,
        "last_profile_merge": None,
        "last_people_index": None,
        "last_relationship_index": None,
        "last_quality": None,
        "last_product_brief": None,
        "last_portable_export": None,
        "last_portable_export_dir": None,
        "last_portable_export_files": 0,
        "last_portable_export_bytes": 0,
        "last_feishu_collect": None,
        "last_feishu_clean": None,
        "last_feishu_distill": None,
        "last_getnote_diary_sync": None,
        "last_getnote_backfill": None,
        "last_getnote_prune_empty": None,
        "last_web_collect": None,
        "last_obsidian_sync": None,
        "last_cleanup": None,
        "collect_count": 0,
        "total_records": 0,
        "errors": [],
    }


def save_state(state: dict):
    update_state_atomic(STATE_FILE, state)


def parse_collect_output(output: str) -> dict:
    """解析 collect.py 输出，提取新增记录数。"""
    total_new = 0
    by_source = {}
    current_source = ""

    for line in output.split("\n"):
        # 形如 "[1/14] 采集 Claude对话..."
        m = re.search(r'\[\d+/\d+\]\s*采集\s*(\S+)', line)
        if m:
            current_source = m.group(1)
        # 形如 "+20110 条"
        m = re.search(r'\+(\d+)\s*条', line)
        if m and current_source:
            cnt = int(m.group(1))
            by_source[current_source] = cnt
            total_new += cnt

    return {"total_new": total_new, "by_source": by_source}


def external_source_collect():
    log("=== 阶段 1E: 受控外部来源同步 ===")
    ok, out = run_script("immortal.py", "source", "collect", "--json", timeout=900)
    if not ok:
        log(f"受控外部来源同步失败: {out.strip()[:300]}")
        return False, {"records_written": 0, "sources": {}}
    try:
        sources = json.loads(out)
    except json.JSONDecodeError:
        log("受控外部来源同步返回无效 JSON")
        return False, {"records_written": 0, "sources": {}}
    if not isinstance(sources, dict):
        return False, {"records_written": 0, "sources": {}}
    total = sum(
        int(value.get("records_written") or 0)
        for value in sources.values()
        if isinstance(value, dict)
    )
    partial_sources = sorted(
        name for name, value in sources.items()
        if isinstance(value, dict)
        and str(value.get("status") or "").lower() not in {"success", "disabled", "skipped", "not_due"}
    )
    if partial_sources:
        log(f"受控外部来源存在异常: {', '.join(partial_sources)}")
        return False, {
            "records_written": total,
            "sources": sources,
            "partial_sources": partial_sources,
        }
    log(f"受控外部来源同步成功: 新增 {total} 条")
    return True, {"records_written": total, "sources": sources, "partial_sources": []}


def days_since(iso_str: str) -> float:
    """距离指定 ISO 时间戳过了多少天。"""
    if not iso_str:
        return 999
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return 999


def hours_since(iso_str: str) -> float:
    """距离指定 ISO 时间戳过了多少小时。"""
    if not iso_str:
        return 999
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 999


def collect():
    log("=== 阶段 1: 增量采集 ===")
    ok, out = run_script("collect.py", timeout=900)
    if ok:
        info = parse_collect_output(out)
        log(f"采集成功: 新增 {info['total_new']} 条")
        return True, info
    else:
        log(f"采集失败: {out.strip()[:300]}")
        return False, {"total_new": 0, "by_source": {}}


def web_collect():
    log("=== 阶段 1W: 网页访问元信息扫描 ===")
    ok, out = run_script("web_capture.py", "collect", "--json", timeout=300)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = {"status": "error", "totals": {}, "raw": out.strip()[:500]}
    totals = data.get("totals") if isinstance(data, dict) else {}
    records = int((totals or {}).get("records_written") or 0)
    filtered = int((totals or {}).get("visits_filtered") or 0)
    errors = int((totals or {}).get("errors") or 0)
    if ok and data.get("status") in {"ok", "disabled"}:
        log(f"网页收录完成: 新增 {records} 条，过滤 {filtered} 条")
        return True, data
    log(f"网页收录需关注: status={data.get('status')} records={records} filtered={filtered} errors={errors}")
    return False, data


def parse_feishu_collect_output(output: str) -> int:
    total = 0
    in_section = False
    for line in output.splitlines():
        if line.strip() == "New records:":
            in_section = True
            continue
        if not in_section:
            continue
        m = re.match(r"\s+[\w-]+:\s+(\d+)\s*$", line)
        if m:
            total += int(m.group(1))
        elif line and not line.startswith(" "):
            break
    return total


def collect_feishu():
    """返回 (状态, info)。状态三态：'ok' / 'partial'（部分源失败）/ 'failed'。"""
    log("=== 阶段 F1: 飞书增量采集 ===")
    args = feishu_daily_args()
    if not args:
        log("飞书采集跳过: 未配置 expected_user_name/open_id；先运行 immortal.py init 绑定账号")
        return "ok", {"total_new": 0}
    rc, out = run_script_rc("feishu_collect.py", *args, timeout=1800)
    # rc==2 有两种来源：我们的 partial 语义，以及 argparse 用法错误（也固定退 2）。
    # 只有输出里带完成标记才认 partial，否则按硬失败处理，防止参数漂移被掩盖成部分成功。
    if rc == 0 or (rc == 2 and "Feishu collect finished" in out):
        new_records = parse_feishu_collect_output(out)
        status = "partial" if rc == 2 else "ok"
        log(f"飞书采集{'部分成功' if status == 'partial' else '完成'}: 新增 {new_records} 条")
        if "Issues:" in out:
            log(f"飞书采集源级错误: {out.split('Issues:', 1)[1].strip()[:300]}")
        return status, {"total_new": new_records}
    log(f"飞书采集失败: {out.strip()[:500]}")
    return "failed", {"total_new": 0}


def feishu_clean():
    log("=== 阶段 F2: 飞书清洗 ===")
    ok, out = run_script("feishu_clean.py", timeout=1200)
    if ok:
        log("飞书 clean layer 已更新")
        return True
    log(f"飞书清洗失败: {out.strip()[:500]}")
    return False


def feishu_distill():
    log("=== 阶段 F3: 飞书候选蒸馏 ===")
    ok, out = run_script("feishu_distill.py", timeout=1200)
    if ok:
        log("飞书 review layer 已更新")
        return True
    log(f"飞书候选蒸馏失败: {out.strip()[:500]}")
    return False


def profile_auto_review():
    log("=== 阶段 F4: 自动审阅并合并长期画像候选 ===")
    ok, out = run_script("profile_auto_review.py", "--reconsider-rejected", timeout=600)
    if ok:
        approved = re.search(r"approved=(\d+)", out)
        rejected = re.search(r"auto_rejected=(\d+)", out)
        skipped = re.search(r"skipped_already_reviewed=(\d+)", out)
        log(
            "长期画像自动审阅完成: "
            f"新增批准 {approved.group(1) if approved else '?'} / "
            f"自动跳过 {rejected.group(1) if rejected else '?'} / "
            f"已存在 {skipped.group(1) if skipped else '?'}"
        )
        return True
    log(f"长期画像自动审阅失败: {out.strip()[:500]}")
    return False


def profile_attribution_audit(vault_dir=None):
    log("=== 阶段 F5: 自动剥离长期画像污染 ===")
    args = ["--apply"]
    if vault_dir is not None:
        vault = Path(vault_dir).expanduser().absolute()
        args.extend(
            [
                "--reviewed", str(vault / "reviewed/profile_memories.jsonl"),
                "--reviewed-md", str(vault / "reviewed/profile_memories.md"),
                "--distilled", str(vault / "feishu/distilled/profile_memories.jsonl"),
                "--records", str(vault / "feishu/clean/records.jsonl"),
                "--report-dir", str(vault / "quality"),
                "--evidence-index", str(vault / "index.jsonl"),
                "--trust-report", str(vault / "model/attribution/latest-report.json"),
            ]
        )
    ok, out = run_script("profile_attribution_audit.py", *args, timeout=600)
    if ok:
        total = re.search(r"reviewed_total=(\d+)", out)
        kept = re.search(r"reviewed_kept=(\d+)", out)
        quarantined = re.search(r"reviewed_quarantined=(\d+)", out)
        log(
            "长期画像归因审计完成: "
            f"reviewed {total.group(1) if total else '?'} / "
            f"kept {kept.group(1) if kept else '?'} / "
            f"quarantined {quarantined.group(1) if quarantined else '?'}"
        )
        return True
    log(f"长期画像归因审计失败: {out.strip()[:500]}")
    return False


def claims_migrate(vault_dir):
    vault = Path(vault_dir).expanduser().absolute()
    log("=== v1.1 阶段: 迁移 Claims 权威事件层 ===")
    ok, out = run_script(
        "immortal.py",
        "claims-migrate",
        "--vault-dir",
        str(vault),
        "--json",
        timeout=1800,
    )
    if ok:
        log("Claims 迁移完成")
        return True
    log(f"Claims 迁移失败: {out.strip()[:500]}")
    return False


def living_self_build(vault_dir):
    vault = Path(vault_dir).expanduser().absolute()
    log("=== v1.1 阶段: 构建 Living Self ===")
    ok, out = run_script(
        "export_restore.py",
        "living-self-build",
        "--vault-dir",
        str(vault),
        timeout=600,
    )
    if ok:
        log("Living Self 已构建")
        return True
    log(f"Living Self 构建失败: {out.strip()[:500]}")
    return False


def feishu_mirror_inventory():
    log("=== 阶段 F6: 飞书 Drive/Wiki/云文档清单镜像 ===")
    guards = feishu_mirror_guard_args()
    if not guards:
        log("飞书云文档镜像跳过: 未配置 expected_user_name/open_id")
        return True
    args = [
        *guards,
        "--mode", "inventory",
        "--include-wiki",
        "--include-drive-search",
        "--search-page-limit", "0",
        "--delay", "0.25",
    ]
    ok, out = run_script("feishu_drive_mirror.py", *args, timeout=3600)
    tail = out.strip().splitlines()[-1] if out.strip() else "done"
    if ok:
        log(f"飞书云文档清单镜像完成: {tail[:300]}")
        return True
    log(f"飞书云文档清单镜像失败: {out.strip()[:700]}")
    return False


def feishu_mirror_download():
    log("=== 阶段 F7: 飞书 Drive/Wiki/云文档限量导出 ===")
    guards = feishu_mirror_guard_args()
    if not guards:
        log("飞书云文档导出跳过: 未配置 expected_user_name/open_id")
        return True
    args = [
        *guards,
        "--mode", "download",
        "--actions", FEISHU_MIRROR_DOWNLOAD_ACTIONS,
        "--job-batch", "20",
        "--max-jobs", str(FEISHU_MIRROR_DOWNLOAD_MAX_JOBS),
        "--delay", "0.8",
    ]
    ok, out = run_script("feishu_drive_mirror.py", *args, timeout=3600)
    tail = out.strip().splitlines()[-1] if out.strip() else "done"
    if ok:
        log(f"飞书云文档限量导出完成: {tail[:300]}")
        return True
    log(f"飞书云文档限量导出失败: {out.strip()[:700]}")
    return False


def summarize():
    log("=== 阶段 2: 生成摘要 ===")
    # daily 文件名已统一为本地日；03:03 班次若用 UTC today 会指向前一天、漏掉本地今天的文件
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    ok, out = run_script("summary.py", "--since", today)
    if ok:
        log(f"摘要生成成功")
        return True
    else:
        log(f"摘要失败: {out.strip()[:300]}")
        return False


# update_timeline() / update_dashboard() 已删除（2026-06-17）：
# timeline/dashboard 早于 2026-06-14 停用，这两个包装函数在 run_main 里零调用，
# 是误导性死代码。timeline.py/dashboard.py 本体保留（train/CLI/profile_review 仍承重）。


def distill():
    log("=== 阶段 5: 蒸馏数字人格 ===")
    ok, out = run_script("distill.py", timeout=300)
    if ok:
        soul_file = IMMORTAL_DIR / "digital-soul.md"
        if soul_file.exists():
            size_kb = soul_file.stat().st_size / 1024
            log(f"数字人格已更新: {size_kb:.1f} KB")
        return True
    else:
        log(f"蒸馏失败: {out.strip()[:300]}")
        return False


def profile():
    log("=== 阶段 5B: 更新长期画像 ===")
    ok, out = run_script("profile.py", timeout=300)
    if ok:
        log("长期画像已更新")
        return True
    log(f"长期画像更新失败: {out.strip()[:300]}")
    return False


def profile_nuwa():
    log("=== 阶段 5B2: 更新 Nuwa 风格画像蒸馏 ===")
    ok, out = run_script("profile_nuwa.py", timeout=300)
    if ok:
        log("Nuwa 风格画像已更新")
        return True
    if "quality=attention" in out:
        log(f"Nuwa 风格画像已更新但质量门禁需关注: {out.strip()[:300]}")
        return True
    log(f"Nuwa 风格画像更新失败: {out.strip()[:300]}")
    return False


def people_index():
    log("=== 阶段 5C: 更新人物记忆索引 ===")
    ok, out = run_script("people_index.py", timeout=300)
    if ok:
        matched = re.search(r"people=(\d+)", out)
        log(f"人物记忆索引已更新: {matched.group(1) if matched else '?'} 人")
        return True
    log(f"人物记忆索引更新失败: {out.strip()[:300]}")
    return False


def search_index_sync():
    log("=== 阶段 5C0: 同步控制中心检索索引 ===")
    ok, out = run_script("index_db.py", "sync", timeout=900)
    if ok:
        summary = out.strip().splitlines()[-1] if out.strip() else "索引已同步"
        log(f"控制中心检索索引已同步: {summary[:240]}")
        return True
    log(f"控制中心检索索引同步失败: {out.strip()[:300]}")
    return False


def relationship_index():
    log("=== 阶段 5D: 更新关联证据网络 ===")
    ok, out = run_script("relationship_index.py", timeout=360)
    if ok:
        person_edges = re.search(r"person_edges=(\d+)", out)
        project_edges = re.search(r"project_edges=(\d+)", out)
        log(
            "关联证据网络已更新: "
            f"人物证据 {person_edges.group(1) if person_edges else '?'} 条 / "
            f"项目证据 {project_edges.group(1) if project_edges else '?'} 条"
        )
        return True
    log(f"关联证据网络更新失败: {out.strip()[:300]}")
    return False


def quality_report():
    log("=== 阶段 5E: 更新记忆质量报告 ===")
    ok, out = run_script("quality_report.py", timeout=240)
    if ok:
        summary = out.strip().splitlines()[-1] if out.strip() else "done"
        log(f"记忆质量报告已更新: {summary[:240]}")
        return True
    log(f"记忆质量报告失败: {out.strip()[:300]}")
    return False


def cards_build():
    log("=== 阶段 5H: 构建判断力卡片盒（纠正即记忆）===")
    ok, out = run_script("immortal.py", "cards", "build", timeout=180)
    if ok:
        line = out.strip().splitlines()[-1] if out.strip() else "done"
        log(f"判断力卡片盒已更新: {line[:240]}")
        return True
    log(f"cards build failed: {out.strip()[:300]}")
    return False


def evaluate_v11_production_switch(vault_dir, migration, prewarm) -> dict:
    """Evaluate the final switch against the live vault at execution time."""
    from export_restore import v11_production_switch_gate

    return v11_production_switch_gate(vault_dir, migration, prewarm)


def run_v11_model_stages(vault_dir, migration, prewarm) -> dict:
    """Run the explicit migration-only dependency chain and stop fail-closed."""
    vault = Path(vault_dir).expanduser().absolute()
    switch_gate = evaluate_v11_production_switch(vault, migration, prewarm)
    if (
        not isinstance(switch_gate, dict)
        or switch_gate.get("ok") is not True
        or switch_gate.get("production_switch_allowed") is not True
    ):
        return {
            "ok": False,
            "completed": [],
            "blockers": ["production_switch_gate_required"],
            "switch_gate": switch_gate if isinstance(switch_gate, dict) else {},
        }
    if vault.resolve() != IMMORTAL_DIR.resolve():
        return {
            "ok": False,
            "completed": [],
            "blockers": ["v11_model_stages_require_live_vault"],
        }
    stages = [
        ("profile-attribution-audit", lambda: profile_attribution_audit(vault)),
        ("claims-migrate", lambda: claims_migrate(vault)),
        ("living-self-build", lambda: living_self_build(vault)),
        ("cards build", cards_build),
        ("quality", quality_report),
    ]
    completed = []
    for name, action in stages:
        if not action():
            return {
                "ok": False,
                "completed": completed,
                "blockers": [name + "_failed"],
            }
        completed.append(name)
    return {"ok": True, "completed": completed, "blockers": []}


def daily_digest():
    log("=== 阶段 5F: 生成每日变化摘要 ===")
    ok, out = run_script("daily_digest.py", timeout=120)
    if ok:
        line = out.strip().splitlines()[-1] if out.strip() else "done"
        log(f"每日变化摘要已更新: {line[:240]}")
        return True
    log(f"digest failed: {out.strip()[:300]}")
    return False


def product_brief():
    log("=== 阶段 5F2: 生成产品目标操作台 ===")
    ok, out = run_script("product_brief.py", timeout=120)
    if ok:
        line = out.strip().splitlines()[-1] if out.strip() else "done"
        log(f"产品目标操作台已更新: {line[:240]}")
        return True
    log(f"product brief failed: {out.strip()[:300]}")
    return False


def portable_export():
    log("=== 阶段 5G: 生成便携恢复备份 ===")
    # want_stdout=True：stderr 的任何警告混进输出都会毁掉 JSON 解析（Obsidian 九天假失败同族坑）
    ok, out = run_script("export_restore.py", "create-export", timeout=2400, want_stdout=True)
    if ok:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            log(f"便携备份已生成，但解析输出失败: {out.strip()[:300]}")
            return True, {}
        totals = data.get("totals") or {}
        log(
            "便携备份已生成: "
            f"{data.get('export_dir', '')} · "
            f"{int(totals.get('files') or 0)} files · "
            f"{int(totals.get('bytes') or 0)} bytes"
        )
        return True, data
    log(f"便携备份失败: {out.strip()[:500]}")
    return False, {}


def restore_check_export(export_dir: str):
    log("=== 阶段 5G2: 校验便携恢复备份 ===")
    if not export_dir:
        log("备份校验跳过: export_dir 缺失")
        return False, {}
    ok, out = run_script("export_restore.py", "restore-check", export_dir, timeout=3600, want_stdout=True)
    if ok:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            log(f"备份校验通过，但解析输出失败: {out.strip()[:300]}")
            return True, {"export_dir": export_dir}
        log(
            "备份校验通过: "
            f"{int(data.get('checked_files') or 0)} / "
            f"{int(data.get('expected_files') or 0)} files"
        )
        return True, data
    log(f"备份校验失败: {out.strip()[:700]}")
    return False, {"export_dir": export_dir}


def agent_entry_refresh():
    log("=== 阶段 7: 刷新 Agent Entry ===")
    ok, out = run_script("agent_bridge.py", "entry", timeout=120)
    if ok:
        entry = re.search(r"entry_md=(.+)", out)
        log(f"Agent Entry 已刷新: {entry.group(1).strip() if entry else 'done'}")
        return True
    log(f"Agent Entry 刷新失败: {out.strip()[:500]}")
    return False


def obsidian_sync():
    log("=== 阶段 7A: 刷新 Obsidian 阅读层 ===")
    ok, out = run_script("obsidian_sync.py", "sync", "--json", timeout=180, want_stdout=True)
    try:
        # 只截取输出里的 JSON 段，双保险防任何前后噪音污染解析（曾造成 9 天假失败）
        s = out[out.find("{"): out.rfind("}") + 1] if "{" in out and "}" in out else out
        data = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        data = {"status": "error", "broken_links_count": 0, "raw": out.strip()[:500]}
    broken = int(data.get("broken_links_count") or 0)
    if ok and data.get("status") in {"ok", "disabled"}:
        log(f"Obsidian 阅读层已同步: 断链 {broken} 条")
        return True, data
    log(f"Obsidian 阅读层需关注: status={data.get('status')} broken={broken}")
    return False, data


def getnote_credentials_present() -> bool:
    if os.environ.get("GETNOTE_API_KEY") and os.environ.get("GETNOTE_CLIENT_ID"):
        return True
    if not GETNOTE_CONFIG.exists():
        return False
    try:
        data = json.loads(GETNOTE_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(isinstance(data, dict) and data.get("api_key") and data.get("client_id"))


def getnote_diary_sync():
    log("=== 阶段 7B: 同步 Get 笔记每日行动日记 ===")
    if not getnote_credentials_present():
        log("Get 笔记未配置凭证，跳过日记同步")
        return True, {"status": "skip", "reason": "missing credentials"}
    ok, out = run_script("getnote_sync.py", "sync", "--yesterday", "--continue-on-error", timeout=900)
    if ok:
        try:
            data = json.loads(GETNOTE_LATEST_JSON.read_text(encoding="utf-8"))
        except Exception:
            data = {"status": "ok", "results": []}
        line = out.strip().splitlines()[-1] if out.strip() else "done"
        log(f"Get 笔记日记同步完成: {line[:240]}")
        return True, data
    log(f"Get 笔记日记同步失败: {out.strip()[:500]}")
    return False, {"status": "error", "error": out.strip()[:500]}


def getnote_backfill_history():
    log("=== 阶段 7C: 分批补齐 Get 笔记历史日记 ===")
    if not getnote_credentials_present():
        log("Get 笔记未配置凭证，跳过历史补齐")
        return True, {"status": "skip", "reason": "missing credentials"}
    ok, out = run_script(
        "getnote_sync.py",
        "sync",
        "--all",
        "--missing-limit",
        str(GETNOTE_BACKFILL_MISSING_LIMIT),
        "--continue-on-error",
        "--delay",
        "3",
        "--retries",
        "3",
        "--rate-limit-sleep",
        "12",
        "--no-latest",
        timeout=1200,
    )
    if ok:
        line = out.strip().splitlines()[-1] if out.strip() else "done"
        log(f"Get 笔记历史补齐完成: {line[:240]}")
        return True, {"status": "ok"}
    if "quota_daily_exceeded" in out:
        log("Get 笔记历史补齐遇到每日额度限制，已停止，本任务下次自动继续")
        return True, {"status": "quota_exceeded"}
    log(f"Get 笔记历史补齐失败: {out.strip()[:500]}")
    return False, {"status": "error", "error": out.strip()[:500]}


def getnote_prune_empty():
    log("=== 阶段 7D: 清理 Get 笔记空日记 ===")
    if not getnote_credentials_present():
        log("Get 笔记未配置凭证，跳过空日记清理")
        return True, {"status": "skip", "reason": "missing credentials"}
    ok, out = run_script(
        "getnote_sync.py",
        "prune-empty",
        "--limit",
        str(GETNOTE_PRUNE_EMPTY_LIMIT),
        "--continue-on-error",
        "--delay",
        "3",
        timeout=900,
    )
    if ok:
        line = out.strip().splitlines()[-1] if out.strip() else "done"
        log(f"Get 笔记空日记清理完成: {line[:240]}")
        return True, {"status": "ok"}
    if "quota_daily_exceeded" in out:
        log("Get 笔记空日记清理遇到每日额度限制，已停止，本任务下次自动继续")
        return True, {"status": "quota_exceeded"}
    log(f"Get 笔记空日记清理失败: {out.strip()[:500]}")
    return False, {"status": "error", "error": out.strip()[:500]}


def cleanup():
    """返回 True=完成 / "frozen"=冻结跳过（不记账不报错）/ False=失败。"""
    log("=== 阶段 6: 磁盘清理 ===")
    if FREEZE_MARKER.exists():
        log("检测到维护冻结 marker，跳过不可逆清理（skipped_maintenance_freeze）")
        return "frozen"
    ok, out = run_script("cleanup.py", timeout=300)
    if ok:
        log("清理完成")
        return True
    else:
        log(f"清理失败: {out.strip()[:300]}")
        return False


def update_total_records(state: dict):
    """更新总记录数。优先用 SQLite docs 计数，避免每轮全量逐行读 1GB index.jsonl。"""
    index_file = IMMORTAL_DIR / "index.jsonl"
    if not index_file.exists():
        return
    # 快路径：search_index.db 的 docs count（已 sync 到位时等于源行数）
    try:
        import sqlite3
        db = IMMORTAL_DIR / "search_index.db"
        if db.exists():
            con = sqlite3.connect(str(db))
            count = con.execute("SELECT count(*) FROM docs").fetchone()[0]
            con.close()
            if count and count > 0:
                state["total_records"] = count
                return
    except Exception:
        pass
    # 回退：全量逐行计数（保正确性）
    count = sum(1 for _ in open(index_file, "r"))
    state["total_records"] = count


def acquire_lock() -> bool:
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid_text = LOCK_FILE.read_text(encoding="utf-8", errors="ignore").strip()
            pid = int(pid_text.split()[0]) if pid_text else 0
            if pid:
                os.kill(pid, 0)
                log(f"已有编排器实例在运行，跳过本轮: pid={pid}")
                return False
            age = max(0.0, time.time() - LOCK_FILE.stat().st_mtime)
            if age < 30:
                log("lock 尚未写入 pid，按活跃实例处理以避免重入")
                return False
            log("发现过期空 lock，自动清理")
            LOCK_FILE.unlink(missing_ok=True)
            return acquire_lock()
        except ProcessLookupError:
            log("发现过期 lock，自动清理")
            LOCK_FILE.unlink(missing_ok=True)
            return acquire_lock()
        except (ValueError, IndexError):
            try:
                age = max(0.0, time.time() - LOCK_FILE.stat().st_mtime)
            except OSError:
                age = 0.0
            if age >= 30:
                log("发现过期异常 lock，自动清理")
                LOCK_FILE.unlink(missing_ok=True)
                return acquire_lock()
            log("lock 内容异常但仍新鲜，跳过本轮以避免重入")
            return False
        except Exception:
            log("无法确认 lock 状态，跳过本轮以避免重入")
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def main():
    if not acquire_lock():
        return 0
    global _ACTIVE_STAGE, _STAGE_ERROR_COUNT
    _ACTIVE_STAGE = ""
    _STAGE_ERROR_COUNT = 0
    RUNTIME_TELEMETRY.start_run(
        trigger=os.environ.get("IMMORTAL_TRIGGER") or "schedule",
        pid=os.getpid(),
        stage_total=7,
    )
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=telemetry_heartbeat_loop,
        args=(heartbeat_stop,),
        daemon=True,
        name="immortal-telemetry-heartbeat",
    )
    heartbeat_thread.start()
    try:
        outcome = run_main() or {}
        results = outcome.get("results") if isinstance(outcome, dict) else {}
        errors = outcome.get("errors") if isinstance(outcome, dict) else []
        status = outcome.get("status") if isinstance(outcome, dict) else "success"
        telemetry_finish_active(errors or [])
        RUNTIME_TELEMETRY.finish_run(
            status=str(status or "success"),
            results=results if isinstance(results, dict) else {},
            error="；".join(str(item) for item in (errors or [])),
        )
        return int(outcome.get("exit_code") or (1 if status == "failed" else 0))
    except ControlJobCanceled as exc:
        if _ACTIVE_STAGE:
            RUNTIME_TELEMETRY.finish_stage(_ACTIVE_STAGE, status="canceled", error=str(exc))
            _ACTIVE_STAGE = ""
        RUNTIME_TELEMETRY.finish_run(status="canceled", error=str(exc))
        return 2
    except Exception as exc:
        if _ACTIVE_STAGE:
            RUNTIME_TELEMETRY.finish_stage(_ACTIVE_STAGE, status="failed", error=str(exc))
            _ACTIVE_STAGE = ""
        RUNTIME_TELEMETRY.finish_run(status="failed", error=str(exc))
        raise
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        release_lock()


def run_main():
    missing_scripts = preflight_required_scripts()
    if missing_scripts:
        return {
            "status": "failed",
            "exit_code": 1,
            "errors": [
                f"required core script missing: {name}"
                for name in missing_scripts
            ],
            "results": {},
        }

    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    errors = []

    log(f"========= 编排器启动 (UTC {now.strftime('%Y-%m-%d %H:%M')}) =========")
    log(f"上次采集: {state.get('last_collect') or '从未'}")
    log(f"上次飞书采集: {state.get('last_feishu_collect') or '从未'}")
    log(f"上次网页收录: {state.get('last_web_collect') or '从未'}")
    log(f"上次蒸馏: {state.get('last_distill') or '从未'}")

    # 阶段 1: 采集
    telemetry_stage("collect", "本地增量采集", errors)
    collect_ok, collect_info = collect()
    if collect_ok:
        state["last_collect"] = now_iso
    else:
        errors.append("collect failed")

    telemetry_stage("external", "外部来源同步", errors)
    external_ok, external_info = external_source_collect()
    external_new = int(external_info.get("records_written") or 0)
    if external_ok:
        state["last_external_source_collect"] = now_iso
    else:
        errors.append("external source collect failed")

    web_new = 0
    web_due_hours = hours_since(state.get("last_web_collect"))
    if web_due_hours >= WEB_CAPTURE_INTERVAL_HOURS:
        log(f"距上次网页收录 {web_due_hours:.1f} 小时，触发轻量网页历史扫描")
        web_ok, web_info = web_collect()
        web_totals = web_info.get("totals") if isinstance(web_info, dict) else {}
        web_new = int((web_totals or {}).get("records_written") or 0)
        if web_ok:
            state["last_web_collect"] = now_iso
        else:
            errors.append("web collect failed")
    else:
        log(f"距上次网页收录 {web_due_hours:.1f} 小时 < {WEB_CAPTURE_INTERVAL_HOURS} 小时，跳过")

    # 阶段 2-3: 摘要 + 时间线（只有采集成功才做）
    if collect_ok:
        if summarize():
            state["last_summary"] = now_iso
        else:
            errors.append("summary failed")
        # timeline.html 已停用（2026-06-14 Owner 决策：纯展示物，无人消费）

    # 飞书增量采集较重，最多每天跑一轮；成功后刷新 clean/distill review layer。
    feishu_new = 0
    feishu_due_hours = hours_since(state.get("last_feishu_collect"))
    if feishu_due_hours >= FEISHU_INTERVAL_HOURS:
        log(f"距上次飞书采集 {feishu_due_hours:.1f} 小时，触发飞书增量")
        feishu_status, feishu_info = collect_feishu()
        feishu_ok = feishu_status in ("ok", "partial")
        feishu_new = feishu_info.get("total_new", 0)
        state["last_feishu_status"] = feishu_status
        if feishu_status == "partial":
            errors.append("feishu partial: 部分源采集失败，详见 feishu/state.json last_errors")
        if feishu_ok:
            state["last_feishu_collect"] = now_iso
            if feishu_clean():
                state["last_feishu_clean"] = now_iso
            else:
                errors.append("feishu clean failed")
            if feishu_distill():
                state["last_feishu_distill"] = now_iso
                if profile_auto_review():
                    state["last_profile_auto_review"] = now_iso
                    state["last_profile_merge"] = now_iso
                    if profile_attribution_audit():
                        state["last_profile_attribution_audit"] = now_iso
                    else:
                        errors.append("profile attribution audit failed")
                else:
                    errors.append("profile auto review failed")
            else:
                errors.append("feishu distill failed")
        else:
            errors.append("feishu collect failed")
    else:
        log(f"距上次飞书采集 {feishu_due_hours:.1f} 小时 < {FEISHU_INTERVAL_HOURS} 小时，跳过")

    attribution_audit_due_hours = hours_since(state.get("last_profile_attribution_audit"))
    if attribution_audit_due_hours >= PROFILE_ATTRIBUTION_AUDIT_INTERVAL_HOURS:
        log(f"距上次画像归因审计 {attribution_audit_due_hours:.1f} 小时，触发污染剥离")
        if profile_attribution_audit():
            state["last_profile_attribution_audit"] = now_iso
        else:
            errors.append("profile attribution audit failed")
    else:
        log(
            f"距上次画像归因审计 {attribution_audit_due_hours:.1f} 小时 < "
            f"{PROFILE_ATTRIBUTION_AUDIT_INTERVAL_HOURS} 小时，跳过"
        )

    mirror_inventory_due_hours = hours_since(state.get("last_feishu_mirror_inventory"))
    if mirror_inventory_due_hours >= FEISHU_MIRROR_INVENTORY_INTERVAL_HOURS:
        log(f"距上次飞书云文档清单镜像 {mirror_inventory_due_hours:.1f} 小时，触发 inventory")
        if feishu_mirror_inventory():
            state["last_feishu_mirror_inventory"] = now_iso
        else:
            errors.append("feishu mirror inventory failed")
    else:
        log(
            f"距上次飞书云文档清单镜像 {mirror_inventory_due_hours:.1f} 小时 < "
            f"{FEISHU_MIRROR_INVENTORY_INTERVAL_HOURS} 小时，跳过"
        )

    mirror_download_due_hours = hours_since(state.get("last_feishu_mirror_download"))
    if mirror_download_due_hours >= FEISHU_MIRROR_DOWNLOAD_INTERVAL_HOURS:
        log(f"距上次飞书云文档导出 {mirror_download_due_hours:.1f} 小时，触发限量导出")
        if feishu_mirror_download():
            state["last_feishu_mirror_download"] = now_iso
        else:
            errors.append("feishu mirror download failed")
    else:
        log(
            f"距上次飞书云文档导出 {mirror_download_due_hours:.1f} 小时 < "
            f"{FEISHU_MIRROR_DOWNLOAD_INTERVAL_HOURS} 小时，跳过"
        )

    telemetry_stage("distill", "记忆清洗与蒸馏", errors)
    # 阶段 5: 数字人格蒸馏默认关闭。飞书先进入 review layer，避免噪声直接污染 digital-soul.md。
    days_since_distill = days_since(state.get("last_distill"))
    if AUTO_DIGITAL_SOUL_DISTILL and days_since_distill >= DISTILL_INTERVAL_DAYS:
        log(f"距上次蒸馏 {days_since_distill:.1f} 天，触发数字人格蒸馏")
        if distill():
            state["last_distill"] = now_iso
        else:
            errors.append("distill failed")
    elif AUTO_DIGITAL_SOUL_DISTILL:
        log(f"距上次蒸馏 {days_since_distill:.1f} 天 < {DISTILL_INTERVAL_DAYS} 天，跳过")
    else:
        log("自动 digital-soul 蒸馏已关闭；飞书数据只自动合并到 reviewed/profile layer")

    telemetry_stage("profile", "画像与关系网络", errors)
    if profile():
        state["last_profile"] = now_iso
        if profile_nuwa():
            state["last_profile_nuwa"] = now_iso
        else:
            errors.append("profile nuwa failed")
    else:
        errors.append("profile failed")

    telemetry_stage("quality", "索引与质量检查", errors)
    # 阶段 5C/5D: 人物索引和关联证据先刷新；状态落盘后再生成 digest，避免摘要滞后一轮。
    if search_index_sync():
        state["last_search_index_sync"] = now_iso
    else:
        # Boundary: Task A records this required-stage failure in state and
        # telemetry. Making the scheduler process exit nonzero belongs to the
        # separate Preflight Task B and is intentionally not claimed here.
        errors.append("search index sync failed")

    people_index_ok = people_index()
    if people_index_ok:
        state["last_people_index"] = now_iso
    else:
        errors.append("people index failed")

    relationship_index_ok = False
    if people_index_ok:
        relationship_index_ok = relationship_index()
        if relationship_index_ok:
            state["last_relationship_index"] = now_iso
        else:
            errors.append("relationship index failed")
    else:
        log("人物索引未成功，跳过关联证据网络")

    quality_ok = False
    if relationship_index_ok:
        quality_ok = quality_report()
        if quality_ok:
            state["last_quality"] = now_iso
        else:
            errors.append("quality report failed")
    else:
        log("关联证据网络未成功，跳过记忆质量报告")

    telemetry_stage("backup", "备份与恢复校验", errors)
    # 阶段 6: 磁盘清理（每周）
    days_since_cleanup = days_since(state.get("last_cleanup"))
    if days_since_cleanup >= CLEANUP_INTERVAL_DAYS:
        log(f"距上次清理 {days_since_cleanup:.1f} 天，触发清理")
        cleanup_result = cleanup()
        if cleanup_result is True:
            state["last_cleanup"] = now_iso
        elif cleanup_result == "frozen":
            pass  # 冻结跳过：不记账（解冻后按真实间隔立即补跑），也不算错误
        else:
            errors.append("cleanup failed")

    # 更新统计
    state["collect_count"] = state.get("collect_count", 0) + 1
    update_total_records(state)
    state["last_run_new_records"] = collect_info.get("total_new", 0)
    state["last_run_feishu_new_records"] = feishu_new
    state["last_run_web_new_records"] = web_new
    state["last_run_external_new_records"] = external_new
    state["errors"] = errors[-10:]  # 保留最近 10 个错误

    save_state(state)

    # 每日 digest（日报）已停用（2026-06-14 Owner 决策：无价值，且曾以遥测噪音污染 agent-context）
    # product brief（goal.md）已停用（2026-06-14 Owner 决策：meta 自述，无消费者）

    # 阶段 5H: 构建判断力卡片盒（纠正即记忆），供 agent-context 消费
    if cards_build():
        state["last_cards_build"] = now_iso
    else:
        errors.append("cards build failed")
    state["errors"] = errors[-10:]
    save_state(state)

    days_since_export = days_since(state.get("last_portable_export"))
    if days_since_export >= PORTABLE_EXPORT_INTERVAL_DAYS:
        log(f"距上次便携备份 {days_since_export:.1f} 天，触发便携导出")
        export_ok, export_data = portable_export()
        if export_ok:
            totals = export_data.get("totals") or {}
            state["last_portable_export"] = export_data.get("generated_at") or now_iso
            state["last_portable_export_dir"] = export_data.get("export_dir")
            state["last_portable_export_files"] = totals.get("files", 0)
            state["last_portable_export_bytes"] = totals.get("bytes", 0)
            state["errors"] = errors[-10:]
            save_state(state)
            restore_ok, restore_data = restore_check_export(str(export_data.get("export_dir") or ""))
            state["last_portable_restore_check"] = now_iso if restore_ok else state.get("last_portable_restore_check")
            state["last_portable_restore_check_dir"] = export_data.get("export_dir")
            state["last_portable_restore_check_files"] = restore_data.get("checked_files", 0)
            state["last_portable_restore_check_status"] = "ok" if restore_ok else "failed"
            if not restore_ok:
                errors.append("portable restore-check failed")
            state["errors"] = errors[-10:]
            save_state(state)
        else:
            errors.append("portable export failed")
            state["errors"] = errors[-10:]
            save_state(state)
    else:
        log(f"距上次便携备份 {days_since_export:.1f} 天 < {PORTABLE_EXPORT_INTERVAL_DAYS} 天，跳过")

    # dashboard.html 已停用（2026-06-14 Owner 决策：5MB/天展示物，基本不打开）

    telemetry_stage("sync", "阅读层与 Agent 入口", errors)
    if agent_entry_refresh():
        state["last_agent_entry"] = now_iso
    else:
        errors.append("agent entry refresh failed")
    state["errors"] = errors[-10:]
    save_state(state)

    obsidian_due_hours = hours_since(state.get("last_obsidian_sync"))
    if obsidian_due_hours >= OBSIDIAN_SYNC_INTERVAL_HOURS:
        log(f"距上次 Obsidian 同步 {obsidian_due_hours:.1f} 小时，触发阅读层刷新")
        obsidian_ok, obsidian_data = obsidian_sync()
        if obsidian_ok:
            state["last_obsidian_sync"] = datetime.now(timezone.utc).isoformat()
        state["last_obsidian_sync_status"] = obsidian_data.get("status") or ("ok" if obsidian_ok else "error")
        state["last_obsidian_broken_links"] = int(obsidian_data.get("broken_links_count") or 0)
        if not obsidian_ok:
            errors.append("obsidian sync failed")
        state["errors"] = errors[-10:]
        save_state(state)
    else:
        log(
            f"距上次 Obsidian 同步 {obsidian_due_hours:.1f} 小时 < "
            f"{OBSIDIAN_SYNC_INTERVAL_HOURS} 小时，跳过"
        )

    # Get 笔记日记同步/补齐/清理 已全部暂停（2026-06-14 Owner 决策：止血）。
    # 原因：自动日记内容低质（断句乱码），且曾把 API key 等凭证同步到外部 App。
    # getnote_sync.py 与历史日记保留；凭证仍在时可手动 `immortal.py getnote-diary`，
    # 但不再进每日自动流水线。要彻底恢复需重新评估脱敏与质量。

    log(f"========= 编排器完成 =========")
    log(f"  本次新增: {collect_info.get('total_new', 0)} 条")
    log(f"  受控外部来源新增: {external_new} 条")
    log(f"  网页新增: {web_new} 条")
    log(f"  飞书新增: {feishu_new} 条")
    log(f"  总记录数: {state['total_records']:,}")
    log(f"  采集次数: {state['collect_count']}")
    if errors:
        log(f"  警告: 错误: {', '.join(errors)}")
    log("")
    status, exit_code = orchestration_status(errors)
    return {
        "status": status,
        "exit_code": exit_code,
        "errors": errors,
        "results": {
            "new_records": collect_info.get("total_new", 0),
            "external_new_records": external_new,
            "web_new_records": web_new,
            "feishu_new_records": feishu_new,
            "total_records": state["total_records"],
            "outputs_updated": [
                "index",
                "profile",
                "people",
                "relationships",
                "quality",
                "agent_entry",
            ],
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
