"""Bounded recovery of messages from already enabled, accessible Feishu chats."""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone

import feishu_collect as collector
from config import feishu_guard_args, feishu_message_catchup_enabled
from feishu_lock import collector_lock

BUDGET_SECONDS = 1200
TIMEOUT_SECONDS = BUDGET_SECONDS + 300
COVERAGE = "messages from accessible chats only; not documents or meeting notes"


class BudgetExpired(RuntimeError):
    """Do not advance an incomplete window after the request budget expires."""


def run(*, budget_seconds=BUDGET_SECONDS, max_windows=3, now=None):
    now = now or datetime.now(timezone.utc)
    if not feishu_message_catchup_enabled():
        return {"status": "disabled"}
    directory = collector.FEISHU_DIR
    checkpoint = directory / "catchup-messages.json"
    receipt = directory / "catchup-messages-status.json"
    guards = feishu_guard_args()
    with collector_lock(directory) as acquired:
        if not acquired:
            return {"status": "busy"}  # Leave the lock owner's receipt intact.
        result = {
            "coverage": COVERAGE,
            "started_at": now.isoformat(),
            "budget_seconds": budget_seconds,
            "windows_completed": 0,
            "records_written": 0,
        }

        def finish(status, reason=None):
            result.update(status=status, finished_at=datetime.now(timezone.utc).isoformat())
            if reason:
                result["reason"] = reason
            collector.write_json(receipt, result)
            return result

        if not any(flag in guards for flag in ("--expected-user-name", "--expected-user-open-id")):
            return finish("blocked", "expected_account_required")
        try:
            if checkpoint.exists():
                initial = json.loads(checkpoint.read_text(encoding="utf-8"))
            else:
                if not collector.STATE_FILE.exists():
                    return finish("not_initialized", "regular_collection_required")
                legacy = json.loads(collector.STATE_FILE.read_text(encoding="utf-8"))
                initial = {"last_window_end": legacy.get("last_window_end")}
                if not initial["last_window_end"]:
                    return finish("not_initialized", "regular_collection_required")
            cursor = collector.parse_dt(initial.get("last_window_end"))
            if cursor is None or cursor > now:
                return finish("error", "invalid_checkpoint")
        except (AttributeError, OSError, TypeError, ValueError):
            return finish("error", "invalid_checkpoint")

        result.update(
            status="running", cursor_before=cursor.isoformat(), cursor_after=cursor.isoformat(),
            remaining_days=round((now - cursor).total_seconds() / 86400, 3),
        )
        collector.write_json(receipt, result)
        deadline = time.monotonic() + budget_seconds
        original_lark = collector.run_lark

        def bounded_lark(args, *, timeout=60, cwd=None):
            remaining = deadline - time.monotonic()
            if remaining < 2:
                raise BudgetExpired
            # The CLI wrapper can issue at most two attempts.
            return original_lark(args, timeout=min(timeout, max(1, math.floor(remaining / 2))), cwd=cwd)

        status, reason = "ok", None
        durations = []
        try:
            if not checkpoint.exists():
                collector.write_json(checkpoint, initial)
            collector.run_lark = bounded_lark
            for _ in range(max_windows):
                if cursor >= now - timedelta(hours=1):
                    break
                started = time.monotonic()
                if deadline - started < (max(durations) * 1.15 + 5 if durations else 2):
                    status, reason = "budget_limited", "before_next_window"
                    break
                start = cursor - timedelta(hours=1)
                end = min(now, start + timedelta(hours=24))
                args = collector.build_parser().parse_args([
                    *guards, "--sources", "messages", "--since", start.isoformat(),
                    "--until", end.isoformat(), "--chat-page-limit", "20",
                    "--message-page-limit", "200", "--max-messages", "20000",
                ])
                worker = collector.Collector(args, state_file=checkpoint)
                try:
                    worker.run()
                finally:
                    worker.conn.close()
                    result["records_written"] += sum(worker.stats.values())
                if worker.errors or worker.incomplete_window:
                    status, reason = "partial", "incomplete_window"
                    break
                cursor = end
                result.update(cursor_after=cursor.isoformat(),
                              remaining_days=round((now-cursor).total_seconds() / 86400, 3))
                result["windows_completed"] += 1
                durations.append(time.monotonic() - started)
        except BudgetExpired:
            status, reason = "budget_limited", "incomplete_window"
        except RuntimeError as exc:
            status, reason = "error", "collection_failed"
            if str(exc).startswith("AUTH_CHECK_UNAVAILABLE:"):
                status, reason = "auth_unavailable", "identity_verification_unavailable"
            elif "AUTH_FAILURE:" in str(exc):
                status, reason = "auth_failed", "identity_verification_failed"
        except (OSError, ValueError, TypeError):
            status, reason = "error", "collection_failed"
        finally:
            collector.run_lark = original_lark
        return finish(status, reason)


def health(path, *, now=None):
    """Return a three-state summary; never infer complete Feishu coverage."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        status = data["status"]
        if status in {"error", "auth_failed", "blocked"}:
            return "fail", status
        if status == "not_initialized":
            return "warn", status
        if status not in {"running", "ok", "busy", "partial", "budget_limited", "auth_unavailable"}:
            return "fail", "invalid_status"
        active = status == "running"
        stamp = datetime.fromisoformat(data["started_at" if active else "finished_at"].replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        if active:
            budget = data["budget_seconds"]
            if isinstance(budget, bool) or not 0 < budget <= BUDGET_SECONDS:
                return "fail", "invalid_budget"
            return ("warn", "running") if 0 <= age < budget + 300 else ("fail", "over_budget")
        days = float(data["remaining_days"])
        if not 0 <= age < 30 * 3600 or not math.isfinite(days) or days < 0:
            return "fail", "stale_or_invalid"
        return ("ok" if status == "ok" and days < 1 else "warn", f"{status} · {days:.1f} days behind")
    except FileNotFoundError:
        return "warn", "not_run"
    except (OSError, KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return "fail", "invalid_receipt"


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] in {"ok", "busy", "budget_limited", "not_initialized", "disabled"} else 1)
