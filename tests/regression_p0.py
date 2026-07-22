#!/usr/bin/env python3
"""P0 回归验收：对应《关键事故反馈》第 7 节的 8 个场景。

每个场景在独立的临时 HOME 下运行，绝不触碰当前用户的 ~/.immortal。
运行方式：

    python3 tests/regression_p0.py [--evidence-dir DIR]

退出码 0 = 全部通过；1 = 存在失败场景。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core" / "immortal.py"
ADAPTER_PREFLIGHT = REPO / "adapters" / "claude-code" / "skills" / "immortal-memory" / "preflight.sh"

RESULTS: list[dict] = []
EVIDENCE_DIR: Path


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def make_home(tag: str) -> Path:
    home = Path(tempfile.mkdtemp(prefix=f"immortal-regress-{tag}-"))
    return home


def run(home: Path, args: list[str], tag: str, step: str, timeout: int = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("IMMORTAL_DIR", None)
    proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    evidence = EVIDENCE_DIR / tag
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{step}.txt").write_text(
        f"$ HOME={home} {' '.join(args)}\n"
        f"exit_code: {proc.returncode}\n\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n",
        encoding="utf-8",
    )
    return proc


def core_cmd(*args: str) -> list[str]:
    return [sys.executable, str(CORE), *args]


def record(tag: str, title: str, checks: list[tuple[str, bool, str]]) -> None:
    ok = all(passed for _, passed, _ in checks)
    RESULTS.append({"scenario": tag, "title": title, "ok": ok, "checks": [
        {"name": name, "ok": passed, "detail": detail} for name, passed, detail in checks
    ]})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {tag} {title}")
    for name, passed, detail in checks:
        submark = "ok" if passed else "FAIL"
        print(f"       - {submark}: {name}  {detail}")


def seed_real_vault(home: Path, *, records: int = 5, last_collect: datetime | None = None) -> Path:
    """Create a vault with real (non-smoke) records and a chosen last_collect."""
    vault = home / ".immortal"
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    stamp = (last_collect or now_utc()).isoformat()
    with (vault / "index.jsonl").open("w", encoding="utf-8") as handle:
        for idx in range(records):
            handle.write(json.dumps({
                "id": f"real-{idx}",
                "source": "codex",
                "project": "demo-project",
                "session_id": f"s{idx}",
                "timestamp": stamp,
                "type": "chat",
                "role": "user",
                "content": f"真实工作记录 {idx}：讨论了排版器渲染问题并拍板了方案。",
            }, ensure_ascii=False) + "\n")
    (vault / "orchestrator_state.json").write_text(json.dumps({
        "total_records": records,
        "collect_count": 3,
        "last_collect": stamp,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (vault / "sources.json").write_text(json.dumps({
        "sources": [{"name": "codex", "path": str(home / ".codex" / "sessions")}]
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return vault


def scenario_1_smoke_shows_unprotected() -> Path:
    tag, title = "S1", "新用户只按 README 跑 smoke：必须明确显示尚未受真实记忆保护"
    home = make_home("s1")
    run(home, core_cmd("init", "--owner-display-name", "Test User", "--alias", "tester"), tag, "01-init")
    proc = run(home, core_cmd("train", "--smoke"), tag, "02-train-smoke", timeout=600)
    checks = [
        ("train --smoke 正常完成", proc.returncode == 0, f"exit={proc.returncode}"),
        ("输出含 DEMO ONLY 横幅", "DEMO ONLY" in proc.stdout, ""),
        ("输出明示未受保护", "尚未受到真实记忆保护" in proc.stdout, ""),
        ("输出给出生产就绪三步", "生产就绪三步" in proc.stdout, ""),
    ]
    record(tag, title, checks)
    return home


def scenario_2_scheduler_missing_fails(home_smoke: Path) -> None:
    tag, title = "S2", "调度器未安装：health 与 preflight 必须报错，不得静默"
    proc = run(home_smoke, core_cmd("health", "--max-age-hours", "36"), tag, "01-health")
    pf = run(home_smoke, core_cmd("preflight", "--json"), tag, "02-preflight")
    try:
        payload = json.loads(pf.stdout)
    except json.JSONDecodeError:
        payload = {}
    checks = [
        ("health 退出码非零", proc.returncode != 0, f"exit={proc.returncode}"),
        ("health 输出含系统定时 FAIL", any(
            "系统定时" in line and line.strip().startswith("FAIL") for line in proc.stdout.splitlines()
        ), ""),
        ("preflight 报 daily_scheduler_absent", "daily_scheduler_absent" in (payload.get("reasons") or []), ""),
        ("preflight 报 loss_protection unprotected", payload.get("loss_protection") == "unprotected", ""),
    ]
    record(tag, title, checks)


def scenario_3_smoke_only_context_refused(home_smoke: Path) -> None:
    tag, title = "S3", "vault 只有 smoke 记录：agent-context 必须返回 smoke_only/unavailable"
    proc = run(home_smoke, core_cmd("agent-context", "最近一个月做了什么", "--print"), tag, "01-agent-context")
    context_json = home_smoke / ".immortal" / "agent" / "latest-context.json"
    payload = json.loads(context_json.read_text(encoding="utf-8")) if context_json.exists() else {}
    pf = run(home_smoke, core_cmd("preflight", "--json"), tag, "02-preflight")
    pf_payload = json.loads(pf.stdout) if pf.stdout.strip().startswith("{") else {}
    checks = [
        ("agent-context 退出码 3", proc.returncode == 3, f"exit={proc.returncode}"),
        ("stdout 含 context_status=unavailable", "context_status=unavailable" in proc.stdout, ""),
        ("latest-context.json 机器可读 unavailable", payload.get("context_status") == "unavailable", ""),
        ("未生成假 context 正文", payload.get("context_md") in (None, ""), str(payload.get("context_md"))),
        ("preflight vault_status=smoke_only", pf_payload.get("vault_status") == "smoke_only", ""),
        ("preflight 退出码 3", pf.returncode == 3, f"exit={pf.returncode}"),
    ]
    record(tag, title, checks)


def scenario_4_coverage_gap() -> None:
    tag, title = "S4", "查询最近 30 天但最后采集在 45 天前：必须报告时间覆盖缺口"
    home = make_home("s4")
    run(home, core_cmd("init", "--owner-display-name", "Test User"), tag, "01-init")
    seed_real_vault(home, records=5, last_collect=now_utc() - timedelta(days=45))
    since = (now_utc() - timedelta(days=30)).strftime("%Y-%m-%d")
    pf = run(home, core_cmd("preflight", "最近一个月的工作", "--since", since, "--json"), tag, "02-preflight")
    payload = json.loads(pf.stdout) if pf.stdout.strip().startswith("{") else {}
    ac = run(home, core_cmd("agent-context", "最近一个月的工作", "--since", since, "--print"), tag, "03-agent-context", timeout=600)
    context_json = home / ".immortal" / "agent" / "latest-context.json"
    ac_payload = json.loads(context_json.read_text(encoding="utf-8")) if context_json.exists() else {}
    checks = [
        ("preflight 退出码 2 (degraded)", pf.returncode == 2, f"exit={pf.returncode}"),
        ("query_coverage=no_overlap", payload.get("query_coverage") == "no_overlap", str(payload.get("query_coverage"))),
        ("vault_status=stale", payload.get("vault_status") == "stale", str(payload.get("vault_status"))),
        ("coverage_gap_hours ≈ 45 天", 1000 < (payload.get("coverage_gap_hours") or 0) < 1200, str(payload.get("coverage_gap_hours"))),
        ("context 包内显式标注 DEGRADED", "CONTEXT STATUS: DEGRADED" in ac.stdout, ""),
        ("context payload 机器可读 degraded", ac_payload.get("context_status") == "degraded", str(ac_payload.get("context_status"))),
    ]
    record(tag, title, checks)


def scenario_5_core_missing_diagnosis() -> None:
    tag, title = "S5", "Core 被删但 Skill 仍在：必须给出明确诊断而不是普通报错"
    home = make_home("s5")
    skill_dir = home / ".claude" / "skills" / "immortal-memory"
    skill_dir.mkdir(parents=True)
    shutil.copy2(ADAPTER_PREFLIGHT, skill_dir / "preflight.sh")
    proc = run(home, ["sh", str(skill_dir / "preflight.sh")], tag, "01-adapter-preflight")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {}
    checks = [
        ("退出码 3", proc.returncode == 3, f"exit={proc.returncode}"),
        ("core_status=missing", payload.get("core_status") == "missing", ""),
        ("context_status=unavailable", payload.get("context_status") == "unavailable", ""),
        ("禁止自动重建的指令在场", "Do NOT reinstall" in (payload.get("agent_instruction") or ""), ""),
    ]
    record(tag, title, checks)


def scenario_6_export_same_disk_warning() -> None:
    tag, title = "S6", "export 位于 vault 内/同盘：必须出现风险警告"
    home = make_home("s6")
    run(home, core_cmd("init", "--owner-display-name", "Test User"), tag, "01-init")
    seed_real_vault(home, records=5)
    proc = run(home, core_cmd("export"), tag, "02-export-default")
    exports_dir = home / ".immortal" / "exports"
    manifests = sorted(exports_dir.glob("immortal-export-*/manifest.json"))
    manifest = json.loads(manifests[-1].read_text(encoding="utf-8")) if manifests else {}
    state = json.loads((home / ".immortal" / "orchestrator_state.json").read_text(encoding="utf-8"))
    checks = [
        ("stdout 出现 WARNING 块", "WARNING" in proc.stdout, ""),
        ("stdout 明示不具备灾难恢复能力", "不具备灾难恢复能力" in proc.stdout, ""),
        ("manifest.storage_location=internal_vault", manifest.get("storage_location") == "internal_vault", str(manifest.get("storage_location"))),
        ("manifest.warnings 含 export_inside_vault", any(
            str(w).startswith("export_inside_vault") for w in (manifest.get("warnings") or [])
        ), ""),
        ("state 记录 export 落点", state.get("last_portable_export_location") == "internal_vault", str(state.get("last_portable_export_location"))),
    ]
    record(tag, title, checks)


def scenario_7_restore_drill() -> None:
    tag, title = "S7", "从外部 export 恢复到全新 vault：文件数、哈希、索引和 context 查询全部通过"
    src_home = make_home("s7src")
    run(src_home, core_cmd("init", "--owner-display-name", "Test User"), tag, "01-init-src")
    seed_real_vault(src_home, records=6, last_collect=now_utc() - timedelta(hours=1))
    external_target = EVIDENCE_DIR / "S7" / "external-export"
    proc_export = run(src_home, core_cmd("export", "--output-dir", str(external_target)), tag, "02-export-external")
    export_dirs = sorted(external_target.glob("immortal-export-*"))
    export_dir = export_dirs[-1] if export_dirs else None
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8")) if export_dir else {}

    proc_check = run(src_home, core_cmd("restore-check", str(export_dir), "--json"), tag, "03-restore-check")
    check_payload = json.loads(proc_check.stdout) if proc_check.stdout.strip().startswith("{") else {}

    dst_home = make_home("s7dst")
    dst_vault = dst_home / ".immortal"
    dst_vault.mkdir(parents=True)
    for item in export_dir.iterdir():
        if item.name == "manifest.json":
            continue
        target = dst_vault / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    # 按 restore-guide 的新机器步骤，恢复后重新绑定 vault 路径
    config_path = dst_vault / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["vault_dir"] = str(dst_vault)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    pf = run(dst_home, core_cmd("preflight", "--json"), tag, "04-preflight-restored")
    pf_payload = json.loads(pf.stdout) if pf.stdout.strip().startswith("{") else {}
    ac = run(dst_home, core_cmd("agent-context", "排版器渲染问题的结论", "--print"), tag, "05-agent-context-restored", timeout=600)
    files_expected = int((manifest.get("totals") or {}).get("files") or 0)
    checks = [
        ("外部导出成功", proc_export.returncode == 0, f"exit={proc_export.returncode}"),
        ("restore-check 全过 (ok=true)", bool(check_payload.get("ok")), ""),
        ("哈希校验文件数与 manifest 一致", check_payload.get("checked_files") == files_expected and files_expected > 0,
         f"checked={check_payload.get('checked_files')} expected={files_expected}"),
        ("恢复后 vault_status=healthy", pf_payload.get("vault_status") == "healthy", str(pf_payload.get("vault_status"))),
        ("恢复后 context 查询成功", ac.returncode == 0, f"exit={ac.returncode}"),
        ("恢复后索引记录数正确", pf_payload.get("real_record_count") == 6, str(pf_payload.get("real_record_count"))),
    ]
    record(tag, title, checks)


def scenario_8_source_checkout_is_not_an_install() -> None:
    tag, title = "S8", "只恢复出 Git 仓库：必须区分源码存在与产品已安装可用"
    home = make_home("s8")
    # 适配器还在（模拟 Skill 幸存），core 未安装，vault 为空；源码 checkout 本身就是本仓库
    skill_dir = home / ".claude" / "skills" / "immortal-memory"
    skill_dir.mkdir(parents=True)
    shutil.copy2(ADAPTER_PREFLIGHT, skill_dir / "preflight.sh")
    adapter = run(home, ["sh", str(skill_dir / "preflight.sh")], tag, "01-adapter-preflight")
    adapter_payload = json.loads(adapter.stdout) if adapter.stdout.strip().startswith("{") else {}
    # 直接从源码 checkout 跑 preflight：程序能跑，但必须报告 vault 缺失、不可用
    pf = run(home, core_cmd("preflight", "--json"), tag, "02-preflight-from-source")
    pf_payload = json.loads(pf.stdout) if pf.stdout.strip().startswith("{") else {}
    checks = [
        ("适配器判定 core_status=missing", adapter_payload.get("core_status") == "missing", ""),
        ("适配器退出码 3", adapter.returncode == 3, f"exit={adapter.returncode}"),
        ("源码可跑但报告 vault_status=missing", pf_payload.get("vault_status") == "missing", str(pf_payload.get("vault_status"))),
        ("源码可跑但报告 context_status=unavailable", pf_payload.get("context_status") == "unavailable", ""),
        ("源码路径 preflight 退出码 3", pf.returncode == 3, f"exit={pf.returncode}"),
    ]
    record(tag, title, checks)


def main() -> int:
    global EVIDENCE_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-dir",
        default=None,
        help="Persist evidence to this directory; the default uses an isolated temporary directory",
    )
    args = parser.parse_args()
    EVIDENCE_DIR = Path(
        args.evidence_dir
        or tempfile.mkdtemp(prefix="immortal-evidence-p0-")
    )
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"repo: {REPO}")
    print(f"evidence: {EVIDENCE_DIR}")
    print()

    home_smoke = scenario_1_smoke_shows_unprotected()
    scenario_2_scheduler_missing_fails(home_smoke)
    scenario_3_smoke_only_context_refused(home_smoke)
    scenario_4_coverage_gap()
    scenario_5_core_missing_diagnosis()
    scenario_6_export_same_disk_warning()
    scenario_7_restore_drill()
    scenario_8_source_checkout_is_not_an_install()

    summary = {
        "generated_at": now_utc().isoformat(),
        "repo": str(REPO),
        "passed": sum(1 for item in RESULTS if item["ok"]),
        "failed": sum(1 for item in RESULTS if not item["ok"]),
        "results": RESULTS,
    }
    (EVIDENCE_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(f"Passed: {summary['passed']}/8  Failed: {summary['failed']}/8")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
