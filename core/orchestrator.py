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
from pathlib import Path
from datetime import datetime, timezone, timedelta
from config import feishu_daily_sources, feishu_guard_args

IMMORTAL_DIR = Path.home() / ".immortal"
LOG_FILE = IMMORTAL_DIR / "backup.log"
STATE_FILE = IMMORTAL_DIR / "orchestrator_state.json"
LOCK_FILE = IMMORTAL_DIR / "orchestrator.lock"
SKILL_DIR = Path(__file__).resolve().parent
GETNOTE_CONFIG = Path.home() / ".getnote" / "config.json"
GETNOTE_LATEST_JSON = IMMORTAL_DIR / "getnote" / "latest.json"
STABLE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DISTILL_INTERVAL_DAYS = 1  # 每天蒸馏一次（数据变化大的话有意义）
CLEANUP_INTERVAL_DAYS = 7  # 每周清理一次磁盘
PORTABLE_EXPORT_INTERVAL_DAYS = 7  # 每周生成一次可恢复便携包
AUTO_DIGITAL_SOUL_DISTILL = True  # 每日自动蒸馏 digital-soul.md（索引已瘦身，蒸馏快且开销可控）
FEISHU_INTERVAL_HOURS = 20  # 飞书源较重，每天最多跑一轮增量即可
FEISHU_MIRROR_INVENTORY_INTERVAL_HOURS = 24  # 云文档/Wiki 清单每天刷新一轮
FEISHU_MIRROR_DOWNLOAD_INTERVAL_HOURS = 12  # 文档导出限量跑，避免长时间占用 API
PROFILE_ATTRIBUTION_AUDIT_INTERVAL_HOURS = 20  # 每天自动剥离污染画像
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


def log(msg: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def child_env() -> dict:
    env = dict(os.environ)
    existing_path = env.get("PATH", "")
    env["PATH"] = f"{STABLE_PATH}:{existing_path}" if existing_path else STABLE_PATH
    return env


def run_script(name: str, *args, timeout: int = 600) -> tuple:
    """运行脚本，返回 (成功与否, 输出)。"""
    cmd = ["python3", str(SKILL_DIR / name)] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=child_env())
        return (result.returncode == 0, result.stdout + result.stderr)
    except subprocess.TimeoutExpired:
        return (False, f"Timeout after {timeout}s")
    except Exception as e:
        return (False, str(e))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
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
        "last_cleanup": None,
        "collect_count": 0,
        "total_records": 0,
        "errors": [],
    }


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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
    log("=== 阶段 F1: 飞书增量采集 ===")
    args = feishu_daily_args()
    if not args:
        log("飞书采集跳过: 未配置 expected_user_name/open_id；先运行 immortal.py init 绑定账号")
        return True, {"total_new": 0}
    ok, out = run_script("feishu_collect.py", *args, timeout=1800)
    if ok:
        new_records = parse_feishu_collect_output(out)
        log(f"飞书采集完成: 新增 {new_records} 条")
        if "Issues:" in out:
            log(f"飞书采集有非致命问题: {out.split('Issues:', 1)[1].strip()[:300]}")
        return True, {"total_new": new_records}
    log(f"飞书采集失败: {out.strip()[:500]}")
    return False, {"total_new": 0}


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


def profile_attribution_audit():
    log("=== 阶段 F5: 自动剥离长期画像污染 ===")
    ok, out = run_script("profile_attribution_audit.py", "--apply", timeout=600)
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
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ok, out = run_script("summary.py", "--since", today)
    if ok:
        log(f"摘要生成成功")
        return True
    else:
        log(f"摘要失败: {out.strip()[:300]}")
        return False


def update_timeline():
    log("=== 阶段 3: 更新时间线 ===")
    ok, out = run_script("timeline.py")
    if ok:
        log("时间线已更新")
        return True
    else:
        log(f"时间线失败: {out.strip()[:300]}")
        return False


def update_dashboard():
    log("=== 阶段 4: 更新看板 ===")
    ok, out = run_script("dashboard.py")
    if ok:
        log("看板已更新")
        return True
    else:
        log(f"看板更新失败: {out.strip()[:300]}")
        return False


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
    ok, out = run_script("export_restore.py", "create-export", timeout=2400)
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
    ok, out = run_script("export_restore.py", "restore-check", export_dir, timeout=3600)
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
    log("=== 阶段 6: 磁盘清理 ===")
    ok, out = run_script("cleanup.py", timeout=300)
    if ok:
        log("清理完成")
        return True
    else:
        log(f"清理失败: {out.strip()[:300]}")
        return False


def update_total_records(state: dict):
    """更新总记录数。"""
    index_file = IMMORTAL_DIR / "index.jsonl"
    if index_file.exists():
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
        except ProcessLookupError:
            log("发现过期 lock，自动清理")
            LOCK_FILE.unlink(missing_ok=True)
            return acquire_lock()
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
        return
    try:
        run_main()
    finally:
        release_lock()


def run_main():
    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    errors = []

    log(f"========= 编排器启动 (UTC {now.strftime('%Y-%m-%d %H:%M')}) =========")
    log(f"上次采集: {state.get('last_collect') or '从未'}")
    log(f"上次飞书采集: {state.get('last_feishu_collect') or '从未'}")
    log(f"上次蒸馏: {state.get('last_distill') or '从未'}")

    # 阶段 1: 采集
    collect_ok, collect_info = collect()
    if collect_ok:
        state["last_collect"] = now_iso
    else:
        errors.append("collect failed")

    # 阶段 2-3: 摘要 + 时间线（只有采集成功才做）
    if collect_ok:
        if summarize():
            state["last_summary"] = now_iso
        else:
            errors.append("summary failed")
        if not update_timeline():
            errors.append("timeline failed")

    # 飞书增量采集较重，最多每天跑一轮；成功后刷新 clean/distill review layer。
    feishu_new = 0
    feishu_due_hours = hours_since(state.get("last_feishu_collect"))
    if feishu_due_hours >= FEISHU_INTERVAL_HOURS:
        log(f"距上次飞书采集 {feishu_due_hours:.1f} 小时，触发飞书增量")
        feishu_ok, feishu_info = collect_feishu()
        feishu_new = feishu_info.get("total_new", 0)
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

    if profile():
        state["last_profile"] = now_iso
        if profile_nuwa():
            state["last_profile_nuwa"] = now_iso
        else:
            errors.append("profile nuwa failed")
    else:
        errors.append("profile failed")

    # 阶段 5C/5D: 人物索引和关联证据先刷新；状态落盘后再生成 digest，避免摘要滞后一轮。
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

    # 阶段 6: 磁盘清理（每周）
    days_since_cleanup = days_since(state.get("last_cleanup"))
    if days_since_cleanup >= CLEANUP_INTERVAL_DAYS:
        log(f"距上次清理 {days_since_cleanup:.1f} 天，触发清理")
        if cleanup():
            state["last_cleanup"] = now_iso
        else:
            errors.append("cleanup failed")

    # 更新统计
    state["collect_count"] = state.get("collect_count", 0) + 1
    update_total_records(state)
    state["last_run_new_records"] = collect_info.get("total_new", 0)
    state["last_run_feishu_new_records"] = feishu_new
    state["errors"] = errors[-10:]  # 保留最近 10 个错误

    save_state(state)

    if people_index_ok and not daily_digest():
        errors.append("digest failed")
        state["errors"] = errors[-10:]
        save_state(state)

    if quality_ok and product_brief():
        state["last_product_brief"] = now_iso
        state["errors"] = errors[-10:]
        save_state(state)
    elif quality_ok:
        errors.append("product brief failed")
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

    # 阶段 4: 看板放在数据、画像、人物索引和 digest 之后，展示最新记忆层。
    if not update_dashboard():
        errors.append("dashboard failed")
        state["errors"] = errors[-10:]
        save_state(state)

    if agent_entry_refresh():
        state["last_agent_entry"] = now_iso
    else:
        errors.append("agent entry refresh failed")
    state["errors"] = errors[-10:]
    save_state(state)

    getnote_ok, getnote_data = getnote_diary_sync()
    if getnote_ok:
        state["last_getnote_diary_sync"] = getnote_data.get("generated_at") or datetime.now(timezone.utc).isoformat()
        state["last_getnote_diary_status"] = getnote_data.get("status") or "ok"
        state["last_getnote_diary_date"] = getnote_data.get("latest_date") or ""
        results = getnote_data.get("results") if isinstance(getnote_data.get("results"), list) else []
        if results:
            state["last_getnote_diary_note_id"] = results[-1].get("note_id") or ""
    else:
        errors.append("getnote diary sync failed")
        state["last_getnote_diary_status"] = "error"
    state["errors"] = errors[-10:]
    save_state(state)

    getnote_prune_due_hours = hours_since(state.get("last_getnote_prune_empty"))
    if getnote_prune_due_hours >= GETNOTE_PRUNE_EMPTY_INTERVAL_HOURS:
        log(f"距上次 Get 笔记空日记清理 {getnote_prune_due_hours:.1f} 小时，触发分批清理")
        prune_ok, prune_data = getnote_prune_empty()
        state["last_getnote_prune_empty"] = datetime.now(timezone.utc).isoformat()
        state["last_getnote_prune_empty_status"] = prune_data.get("status") or ("ok" if prune_ok else "error")
        if not prune_ok:
            errors.append("getnote prune empty failed")
        state["errors"] = errors[-10:]
        save_state(state)
    else:
        log(
            f"距上次 Get 笔记空日记清理 {getnote_prune_due_hours:.1f} 小时 < "
            f"{GETNOTE_PRUNE_EMPTY_INTERVAL_HOURS} 小时，跳过"
        )

    getnote_backfill_due_hours = hours_since(state.get("last_getnote_backfill"))
    if getnote_backfill_due_hours >= GETNOTE_BACKFILL_INTERVAL_HOURS:
        log(f"距上次 Get 笔记历史补齐 {getnote_backfill_due_hours:.1f} 小时，触发分批补齐")
        backfill_ok, backfill_data = getnote_backfill_history()
        state["last_getnote_backfill"] = datetime.now(timezone.utc).isoformat()
        state["last_getnote_backfill_status"] = backfill_data.get("status") or ("ok" if backfill_ok else "error")
        if not backfill_ok:
            errors.append("getnote backfill failed")
        state["errors"] = errors[-10:]
        save_state(state)
    else:
        log(
            f"距上次 Get 笔记历史补齐 {getnote_backfill_due_hours:.1f} 小时 < "
            f"{GETNOTE_BACKFILL_INTERVAL_HOURS} 小时，跳过"
        )

    log(f"========= 编排器完成 =========")
    log(f"  本次新增: {collect_info.get('total_new', 0)} 条")
    log(f"  飞书新增: {feishu_new} 条")
    log(f"  总记录数: {state['total_records']:,}")
    log(f"  采集次数: {state['collect_count']}")
    if errors:
        log(f"  警告: 错误: {', '.join(errors)}")
    log("")


if __name__ == "__main__":
    main()
