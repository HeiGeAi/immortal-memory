#!/usr/bin/env python3
"""Persistent Feishu Drive mirror worker for Immortal.

This wraps the checkpointed mirror script and can install a user LaunchAgent so
large read-only Feishu exports survive the current Codex terminal session.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_DIR = Path.home() / ".immortal"
MIRROR_DIR = IMMORTAL_DIR / "feishu" / "drive_mirror"
LOG_DIR = MIRROR_DIR / "logs"
PID_FILE = MIRROR_DIR / "worker.pid"
STATE_FILE = MIRROR_DIR / "worker_state.json"
LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCH_AGENT_LABEL = "com.user.immortal.feishu-mirror-worker"
LAUNCH_AGENT_PLIST = LAUNCH_AGENT_DIR / f"{LAUNCH_AGENT_LABEL}.plist"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def bootstrap_target() -> str:
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    return f"gui/{uid}"


def run_cmd(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def worker_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        str(SKILL_DIR / "immortal.py"),
        "feishu-mirror",
        "--mode",
        "download",
        "--actions",
        args.actions,
        "--job-batch",
        str(args.job_batch),
        "--delay",
        str(args.delay),
    ]
    if args.max_jobs:
        cmd.extend(["--max-jobs", str(args.max_jobs)])
    return cmd


def command_run_once(args: argparse.Namespace) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(subprocess.os.getpid()) + "\n", encoding="utf-8")
    write_json(
        STATE_FILE,
        {
            "status": "running",
            "started_at": iso_now(),
            "actions": args.actions,
            "job_batch": args.job_batch,
            "delay": args.delay,
            "max_jobs": args.max_jobs,
        },
    )
    try:
        cmd = worker_command(args)
        result = subprocess.run(cmd)
        write_json(
            STATE_FILE,
            {
                "status": "finished" if result.returncode == 0 else "failed",
                "finished_at": iso_now(),
                "returncode": result.returncode,
                "actions": args.actions,
                "job_batch": args.job_batch,
                "delay": args.delay,
                "max_jobs": args.max_jobs,
            },
        )
        return result.returncode
    finally:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


def make_plist(args: argparse.Namespace) -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    program = [
        sys.executable,
        str(SKILL_DIR / "feishu_mirror_worker.py"),
        "run-once",
        "--actions",
        args.actions,
        "--job-batch",
        str(args.job_batch),
        "--delay",
        str(args.delay),
    ]
    if args.max_jobs:
        program.extend(["--max-jobs", str(args.max_jobs)])
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": program,
        "RunAtLoad": True,
        "EnvironmentVariables": {
            "PATH": os.environ.get(
                "PATH",
                "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            ),
            "HOME": str(Path.home()),
        },
        "StandardOutPath": str(LOG_DIR / "launchd-worker.out.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd-worker.err.log"),
        "WorkingDirectory": str(SKILL_DIR),
        "ProcessType": "Background",
    }


def command_install(args: argparse.Namespace) -> int:
    LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    with LAUNCH_AGENT_PLIST.open("wb") as handle:
        plistlib.dump(make_plist(args), handle, sort_keys=False)
    print(LAUNCH_AGENT_PLIST)
    return 0


def command_start(args: argparse.Namespace) -> int:
    command_install(args)
    target = bootstrap_target()
    run_cmd(["launchctl", "bootout", target, str(LAUNCH_AGENT_PLIST)])
    result = run_cmd(["launchctl", "bootstrap", target, str(LAUNCH_AGENT_PLIST)])
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode
    kick = run_cmd(["launchctl", "kickstart", "-k", f"{target}/{LAUNCH_AGENT_LABEL}"])
    if kick.returncode != 0:
        print(kick.stderr or kick.stdout, file=sys.stderr)
        return kick.returncode
    print(f"started {LAUNCH_AGENT_LABEL}")
    return 0


def command_stop(_args: argparse.Namespace) -> int:
    target = bootstrap_target()
    result = run_cmd(["launchctl", "bootout", target, str(LAUNCH_AGENT_PLIST)])
    if result.returncode != 0 and "No such process" not in (result.stderr or result.stdout):
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode
    print(f"stopped {LAUNCH_AGENT_LABEL}")
    return 0


def command_status(_args: argparse.Namespace) -> int:
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {"status": "unreadable"}
    print("Feishu Mirror Worker")
    print()
    print(f"LaunchAgent: {LAUNCH_AGENT_PLIST} ({'exists' if LAUNCH_AGENT_PLIST.exists() else 'missing'})")
    print(f"State: {state.get('status', 'unknown')}")
    for key in ["started_at", "finished_at", "returncode", "actions", "job_batch", "delay", "max_jobs"]:
        if key in state:
            print(f"{key}: {state[key]}")
    if PID_FILE.exists():
        pid = PID_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        print(f"pid: {pid}")
    result = run_cmd(["launchctl", "print", f"{bootstrap_target()}/{LAUNCH_AGENT_LABEL}"])
    print(f"launchd: {'loaded' if result.returncode == 0 else 'not loaded'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install and run the persistent Feishu mirror worker")
    sub = parser.add_subparsers(dest="command")

    def add_worker_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--actions", default="fetch_doc,export_markdown")
        p.add_argument("--job-batch", type=int, default=20)
        p.add_argument("--delay", type=float, default=0.8)
        p.add_argument("--max-jobs", type=int, default=0)

    run_once = sub.add_parser("run-once", help="Run one checkpointed worker pass")
    add_worker_args(run_once)
    run_once.set_defaults(func=command_run_once)

    install = sub.add_parser("install", help="Install the LaunchAgent without starting it")
    add_worker_args(install)
    install.set_defaults(func=command_install)

    start = sub.add_parser("start", help="Install and start the LaunchAgent")
    add_worker_args(start)
    start.set_defaults(func=command_start)

    sub.add_parser("stop", help="Unload the LaunchAgent").set_defaults(func=command_stop)
    sub.add_parser("status", help="Show worker state").set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
