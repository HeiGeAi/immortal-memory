#!/usr/bin/env python3
"""Truth-oriented state aggregation for the local Immortal control center."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from runtime_telemetry import RuntimeTelemetry


STATUS_LABELS = {
    "healthy": "运行健康",
    "running": "正在运行",
    "attention": "需要关注",
    "failed": "运行失败",
    "unknown": "证据不足",
    "stale": "心跳超时",
    "success": "运行成功",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_time(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_hours(value: object, now: datetime) -> Optional[float]:
    parsed = _parse_time(value)
    if not parsed:
        return None
    return max(0.0, (now - parsed).total_seconds() / 3600)


def _signal(
    signal_id: str,
    label: str,
    status: str,
    detail: str,
    *,
    source: str,
    observed_at: str = "",
    evidence: str = "",
) -> dict[str, Any]:
    return {
        "id": signal_id,
        "label": label,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "detail": detail,
        "source": source,
        "observed_at": observed_at,
        "evidence": evidence,
    }


def candidate_scheduler_labels(launch_agents_dir: Optional[Path] = None) -> list[str]:
    directory = launch_agents_dir or (Path.home() / "Library" / "LaunchAgents")
    labels = {"com.immortal.daily-backup", "com.user.immortal.daily-backup"}
    try:
        for path in directory.glob("*.plist"):
            label = path.stem
            lowered = label.lower()
            if "immortal" in lowered and "daily-backup" in lowered:
                labels.add(label)
    except OSError:
        pass
    return sorted(labels)


def default_scheduler_probe() -> dict[str, Any]:
    """Read the installed daily launchd state without mutating it."""
    uid = os.getuid()
    errors: list[str] = []
    for label in candidate_scheduler_labels():
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"status": "unknown", "detail": f"无法读取 LaunchAgent：{exc}"}
        if result.returncode != 0:
            if result.stderr.strip():
                errors.append(result.stderr.strip()[:120])
            continue
        state = "unknown"
        last_exit_code: Optional[int] = None
        for line in result.stdout.splitlines():
            if "state =" in line:
                state = line.split("=", 1)[-1].strip()
            if "last exit code =" in line:
                try:
                    last_exit_code = int(line.split("=", 1)[-1].strip())
                except ValueError:
                    pass
        evidence = f"launchctl print gui/{uid}/{label}"
        if last_exit_code is not None:
            evidence = f"{evidence}; last_exit_code={last_exit_code}"
        if last_exit_code not in (None, 0):
            return {
                "status": "attention",
                "detail": f"LaunchAgent 已加载，但最近一次退出码 {last_exit_code}，请查看运行记录确认影响范围",
                "evidence": evidence,
            }
        return {
            "status": "healthy",
            "detail": f"LaunchAgent 已加载，当前状态 {state}",
            "evidence": evidence,
        }
    return {
        "status": "attention",
        "detail": "每日 LaunchAgent 未加载",
        "evidence": errors[0] if errors else "未发现 daily-backup LaunchAgent",
    }


class ControlCenter:
    def __init__(
        self,
        immortal_dir: Path,
        *,
        skill_dir: Optional[Path] = None,
        clock: Callable[[], datetime] = _now,
        scheduler_probe: Callable[[], dict[str, Any]] = default_scheduler_probe,
        pid_exists: Optional[Callable[[int], bool]] = None,
        service_reachable: bool = False,
    ) -> None:
        self.immortal_dir = Path(immortal_dir)
        self.skill_dir = Path(skill_dir) if skill_dir else Path(__file__).resolve().parent
        self.clock = clock
        self.scheduler_probe = scheduler_probe
        self.service_reachable = service_reachable
        telemetry_kwargs: dict[str, Any] = {"clock": clock}
        if pid_exists is not None:
            telemetry_kwargs["pid_exists"] = pid_exists
        self.telemetry = RuntimeTelemetry(self.immortal_dir / "runtime", **telemetry_kwargs)

    def _run_signal(self, current: dict[str, Any], now: datetime) -> dict[str, Any]:
        if not current:
            return _signal(
                "latest_run",
                "最近运行",
                "unknown",
                "没有结构化运行记录",
                source="runtime/current_run.json",
            )
        status = str(current.get("status") or "unknown")
        finished = current.get("finished_at") or current.get("updated_at") or ""
        age = _age_hours(finished, now)
        if status == "running":
            detail = f"阶段 {current.get('stage_index') or 0}/{current.get('stage_total') or '?'}"
            return _signal(
                "latest_run",
                "最近运行",
                "running",
                detail,
                source="runtime/current_run.json",
                observed_at=str(current.get("updated_at") or ""),
                evidence=f"pid={current.get('pid') or 'unknown'}",
            )
        if status in {"failed", "stale"}:
            return _signal(
                "latest_run",
                "最近运行",
                "failed",
                str(current.get("error") or "最近运行失败"),
                source="runtime/current_run.json",
                observed_at=str(finished),
            )
        if age is None:
            mapped = "healthy" if status == "success" else "attention"
            detail = "运行已结束，但结束时间无法解析"
        elif age > 30:
            mapped = "attention"
            detail = f"最近运行距今 {age:.1f} 小时，证据已陈旧"
        elif status == "success":
            mapped = "healthy"
            detail = f"最近运行成功，距今 {age:.1f} 小时"
        else:
            mapped = "attention"
            detail = str(current.get("error") or "最近运行有需关注项")
        return _signal(
            "latest_run",
            "最近运行",
            mapped,
            detail,
            source="runtime/current_run.json",
            observed_at=str(finished),
        )

    def _feedback_signal(self, now: datetime) -> dict[str, Any]:
        report = _read_json(self.immortal_dir / "feedback" / "latest.json")
        if not report:
            return _signal(
                "feedback",
                "自动反馈",
                "unknown",
                "尚未生成自动反馈报告",
                source="feedback/latest.json",
            )

        generated_at = str(report.get("generated_at") or "")
        age = _age_hours(generated_at, now)
        pipeline_status = str(report.get("status") or "unknown").strip().lower()
        notification = report.get("notification") if isinstance(report.get("notification"), dict) else {}
        notification_status = str(notification.get("status") or "unknown").strip().lower()
        run_status = report.get("run_status")
        evidence = f"pipeline={pipeline_status}; notification={notification_status}"
        if isinstance(run_status, int):
            evidence = f"{evidence}; run_status={run_status}"

        if age is None:
            status = "unknown"
            detail = "自动反馈报告缺少可解析的生成时间"
        elif age > 30:
            status = "attention"
            detail = f"自动反馈距今 {age:.1f} 小时，证据已陈旧"
        elif pipeline_status == "failed":
            status = "failed"
            detail = "自动反馈报告显示运行失败"
        elif notification_status == "failed":
            status = "attention"
            detail = "主流程已结束，但本机通知未送达"
        elif pipeline_status == "partial":
            status = "attention"
            detail = "自动反馈为部分成功，请查看来源和运行记录"
        elif pipeline_status == "attention":
            status = "attention"
            detail = "自动反馈提示需要关注"
        elif pipeline_status == "ok" and notification_status in {"sent", "skipped"}:
            status = "healthy"
            detail = "自动反馈正常，本机通知状态已记录"
        elif pipeline_status == "ok":
            status = "attention"
            detail = "自动反馈正常，但通知投递状态不可核验"
        else:
            status = "unknown"
            detail = "自动反馈状态无法识别"
        return _signal(
            "feedback",
            "自动反馈",
            status,
            detail,
            source="feedback/latest.json",
            observed_at=generated_at,
            evidence=evidence,
        )

    def _backup_signal(self, state: dict[str, Any], now: datetime) -> dict[str, Any]:
        export_value = state.get("last_portable_export_dir")
        if not export_value:
            return _signal(
                "backup",
                "备份可信度",
                "unknown",
                "没有可定位的便携备份",
                source="orchestrator_state.json",
            )
        export_dir = Path(str(export_value)).expanduser()
        manifest = _read_json(export_dir / "manifest.json")
        if not manifest:
            return _signal(
                "backup",
                "备份可信度",
                "failed",
                "最新备份缺少有效 manifest",
                source=str(export_dir / "manifest.json"),
            )
        generated_at = manifest.get("generated_at") or state.get("last_portable_export")
        age = _age_hours(generated_at, now)
        verification = manifest.get("verification") if isinstance(manifest.get("verification"), dict) else {}
        restore_ok = state.get("last_portable_restore_check_status") == "ok"
        verified = bool(verification.get("ok")) or restore_ok
        storage = manifest.get("storage") if isinstance(manifest.get("storage"), dict) else {}
        same_disk = bool(storage.get("same_disk")) or storage.get("external") is False
        if "same_disk" not in storage and "external" not in storage:
            try:
                same_disk = export_dir.stat().st_dev == self.immortal_dir.stat().st_dev
            except OSError:
                same_disk = False
        secret_scan = manifest.get("secret_scan") if isinstance(manifest.get("secret_scan"), dict) else {}
        secret_count = int(secret_scan.get("unique_candidates") or 0)
        warnings = [str(item) for item in manifest.get("warnings") or []]
        if not secret_count and any("secret_shapes_present" in item for item in warnings):
            secret_count = 1
        risks: list[str] = []
        if not verified:
            risks.append("尚未完成恢复校验")
        if same_disk:
            risks.append("仍在同一磁盘")
        if secret_count:
            risks.append(f"发现 {secret_count} 类敏感形态告警")
        if age is not None and age > 168:
            risks.append(f"备份距今 {age / 24:.1f} 天")
        status = "attention" if risks else "healthy"
        if not verified and not risks[1:]:
            status = "unknown"
        totals = manifest.get("totals") if isinstance(manifest.get("totals"), dict) else {}
        detail = "；".join(risks) if risks else f"已校验 {int(totals.get('files') or 0)} 个文件"
        return _signal(
            "backup",
            "备份可信度",
            status,
            detail,
            source=str(export_dir / "manifest.json"),
            observed_at=str(generated_at or ""),
            evidence=f"restore_check={state.get('last_portable_restore_check_status') or 'unknown'}",
        )

    def _layers(self) -> list[dict[str, Any]]:
        definitions = [
            ("index", "记忆索引", self.immortal_dir / "index.jsonl"),
            ("profile", "长期画像", self.immortal_dir / "profile.json"),
            ("quality", "质量报告", self.immortal_dir / "quality" / "latest.json"),
            ("agent_entry", "Agent 入口", self.immortal_dir / "agent" / "ENTRY.md"),
        ]
        rows = []
        for layer_id, label, path in definitions:
            exists = path.exists()
            mtime = (
                datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
                if exists
                else ""
            )
            rows.append(
                {
                    "id": layer_id,
                    "label": label,
                    "exists": exists,
                    "status": "healthy" if exists else "unknown",
                    "path": str(path),
                    "bytes": path.stat().st_size if exists else 0,
                    "updated_at": mtime,
                }
            )
        return rows

    def build_snapshot(self, *, jobs: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        current = self.telemetry.read_current()
        state = _read_json(self.immortal_dir / "orchestrator_state.json")
        quality = _read_json(self.immortal_dir / "quality" / "latest.json")
        scheduler = dict(self.scheduler_probe() or {})
        scheduler.setdefault("status", "unknown")
        scheduler.setdefault("detail", "没有调度证据")
        scheduler_signal = _signal(
            "scheduler",
            "每日调度器",
            str(scheduler["status"]),
            str(scheduler["detail"]),
            source=str(scheduler.get("source") or "launchctl"),
            evidence=str(scheduler.get("evidence") or ""),
        )
        service_signal = _signal(
            "service",
            "控制中心服务",
            "healthy" if self.service_reachable else "unknown",
            "本地 API 已响应" if self.service_reachable else "未提供服务探针证据",
            source="profile_review.py",
        )
        run_signal = self._run_signal(current, now)
        result_signal = _signal(
            "result",
            "最近运行结果",
            run_signal["status"],
            run_signal["detail"],
            source=run_signal["source"],
            observed_at=run_signal["observed_at"],
        )
        backup_signal = self._backup_signal(state, now)
        feedback_signal = self._feedback_signal(now)
        proofs = [
            service_signal,
            scheduler_signal,
            run_signal,
            result_signal,
            feedback_signal,
            backup_signal,
        ]

        if current.get("status") == "running":
            overall = "running"
        elif current.get("status") in {"failed", "stale"}:
            overall = "failed"
        elif any(item["status"] == "failed" for item in proofs):
            overall = "failed"
        elif any(item["status"] == "attention" for item in proofs):
            overall = "attention"
        elif any(item["status"] == "unknown" for item in proofs):
            overall = "unknown"
        else:
            overall = "healthy"

        attention = [
            item
            for item in proofs
            if item["status"] in {"failed", "attention", "unknown"}
        ]
        if self.immortal_dir.joinpath("MAINTENANCE_FREEZE_DESTRUCTIVE").exists():
            attention.append(
                _signal(
                    "maintenance_freeze",
                    "维护冻结",
                    "attention",
                    "破坏性清理已冻结",
                    source="MAINTENANCE_FREEZE_DESTRUCTIVE",
                )
            )

        results = current.get("results") if isinstance(current.get("results"), dict) else {}
        return {
            "schema_version": 1,
            "generated_at": now.isoformat(timespec="seconds"),
            "status": overall,
            "status_label": STATUS_LABELS[overall],
            "version": self._version(),
            "proofs": proofs,
            "scheduler": scheduler_signal,
            "feedback": feedback_signal,
            "current_run": current,
            "history": self.telemetry.read_history(limit=20),
            "metrics": {
                "new_records": int(results.get("new_records") or state.get("last_run_new_records") or 0),
                "feishu_new_records": int(
                    results.get("feishu_new_records") or state.get("last_run_feishu_new_records") or 0
                ),
                "web_new_records": int(
                    results.get("web_new_records") or state.get("last_run_web_new_records") or 0
                ),
                "total_records": int(results.get("total_records") or state.get("total_records") or 0),
            },
            "quality": {
                "status": quality.get("status") or "unknown",
                "score": quality.get("score"),
                "issue_count": quality.get("issue_count"),
                "recommendation": quality.get("recommendation") or "",
            },
            "layers": self._layers(),
            "attention": attention,
            "jobs": list(jobs or []),
            "actions": [
                {"id": "run", "label": "立即运行全流程", "kind": "primary"},
                {"id": "health", "label": "运行健康检查", "kind": "secondary"},
                {"id": "backup_verify", "label": "校验最新备份", "kind": "secondary"},
                {"id": "profile_refresh", "label": "刷新画像", "kind": "secondary"},
            ],
        }

    def _version(self) -> str:
        try:
            return (self.skill_dir / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"
