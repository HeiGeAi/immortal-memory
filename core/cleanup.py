#!/usr/bin/env python3
"""
永生记忆库 — 磁盘清理工具
1. 删除 codex-output 中的构建产物（svg/wasm/map/哈希命名文件等）
2. 压缩 30 天前的 daily/ 归档
3. 清理 backup.log 旧日志
"""

import gzip
import json
import re
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta


IMMORTAL_DIR = Path.home() / ".immortal"
FILES_DIR = IMMORTAL_DIR / "files"
DAILY_DIR = IMMORTAL_DIR / "daily"
LOG_FILE = IMMORTAL_DIR / "backup.log"
EXPORTS_DIR = IMMORTAL_DIR / "exports"
SESSIONS_DIR = IMMORTAL_DIR / "sessions"
STATE_FILE = IMMORTAL_DIR / "orchestrator_state.json"


SKIP_EXTENSIONS = {
    ".svg", ".wasm", ".map", ".lock", ".project_a",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}


def cleanup_codex_output(dry_run: bool = False) -> dict:
    """清理 codex-output 中的垃圾文件。"""
    target = FILES_DIR / "codex-output"
    if not target.exists():
        return {"deleted": 0, "freed_bytes": 0}

    deleted = 0
    freed = 0

    for f in target.rglob("*"):
        if not f.is_file():
            continue

        should_delete = False
        ext = f.suffix.lower()

        if ext in SKIP_EXTENSIONS:
            should_delete = True
        # 哈希文件名 + 非文本扩展名
        elif re.fullmatch(r'[a-f0-9]{16}', f.stem):
            text_exts = {".md", ".txt", ".py", ".js", ".ts", ".html", ".json"}
            if ext not in text_exts:
                should_delete = True
        # 大文件（>5MB）一般是无价值的二进制
        elif f.stat().st_size > 5 * 1024 * 1024 and ext not in {".md", ".txt", ".py", ".js", ".ts", ".html"}:
            should_delete = True

        if should_delete:
            size = f.stat().st_size
            if not dry_run:
                f.unlink()
            deleted += 1
            freed += size

    # 清理空目录
    if not dry_run:
        for d in sorted(target.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass

    return {"deleted": deleted, "freed_bytes": freed}


def compress_old_daily(days: int = 30, dry_run: bool = False) -> dict:
    """压缩 N 天前的 daily 归档。"""
    if not DAILY_DIR.exists():
        return {"compressed": 0, "saved_bytes": 0}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    compressed = 0
    saved = 0

    for f in DAILY_DIR.glob("*.jsonl"):
        if f.stem >= cutoff:
            continue
        gz_path = f.with_suffix(".jsonl.gz")
        if gz_path.exists():
            continue

        original_size = f.stat().st_size
        if dry_run:
            compressed += 1
            saved += original_size // 2  # 估算
            continue

        with open(f, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)

        new_size = gz_path.stat().st_size
        f.unlink()
        compressed += 1
        saved += (original_size - new_size)

    return {"compressed": compressed, "saved_bytes": saved}


def trim_log(max_lines: int = 5000) -> dict:
    """裁剪日志文件。"""
    if not LOG_FILE.exists():
        return {"trimmed": False, "kept_lines": 0}

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if len(lines) <= max_lines:
        return {"trimmed": False, "kept_lines": len(lines)}

    kept = lines[-max_lines:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.writelines(kept)

    return {"trimmed": True, "kept_lines": len(kept)}


def prune_exports(keep: int = 3, dry_run: bool = False, yes: bool = False) -> dict:
    """Prune old portable exports, retaining the newest N export directories."""
    if not EXPORTS_DIR.exists():
        return {"deleted": 0, "freed_bytes": 0, "kept": 0, "skipped": "exports_missing"}
    exports = sorted(
        [path for path in EXPORTS_DIR.glob("immortal-export-*") if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    keep = max(1, int(keep))
    victims = exports[keep:]
    deleted = 0
    freed = 0
    for path in victims:
        size = dir_size(path)
        freed += size
        deleted += 1
        if not dry_run and yes:
            shutil.rmtree(path)
    return {"deleted": deleted, "freed_bytes": freed, "kept": min(len(exports), keep)}


def prune_task_sessions(max_age_hours: int = 168, dry_run: bool = False) -> dict:
    """Remove expired short-lived task context sessions."""
    if not SESSIONS_DIR.exists():
        return {"deleted": 0, "freed_bytes": 0, "skipped": "sessions_missing"}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    deleted = 0
    freed = 0
    for path in sorted(SESSIONS_DIR.iterdir()):
        if not path.is_dir():
            continue
        remove = False
        manifest = path / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                expires_raw = str(data.get("expires_at") or "")
                if expires_raw:
                    expires = datetime.fromisoformat(expires_raw)
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    remove = expires.astimezone(timezone.utc) < now
            except Exception:
                remove = False
        if not remove:
            remove = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff
        if remove:
            size = dir_size(path)
            deleted += 1
            freed += size
            if not dry_run:
                shutil.rmtree(path)
    return {"deleted": deleted, "freed_bytes": freed}


def dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def format_bytes(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def record_cleanup_run(total_saved: int) -> None:
    """Update orchestrator state so status reflects manual cleanup runs."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except Exception:
        state = {}
    state["last_cleanup"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    state["last_cleanup_saved_bytes"] = total_saved
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    dry_run = "--dry-run" in sys.argv
    prune_old_exports = "--prune-exports" in sys.argv
    yes = "--yes" in sys.argv
    keep_exports = 3
    for arg in sys.argv:
        if arg.startswith("--keep-exports="):
            try:
                keep_exports = int(arg.split("=", 1)[1])
            except ValueError:
                keep_exports = 3

    print("=== 永生记忆库 磁盘清理 ===")
    if dry_run:
        print("（dry-run 模式，不实际删除）")
    print()

    # 1. 清理 codex 垃圾
    print("[1/5] 清理 codex-output 垃圾文件...")
    r1 = cleanup_codex_output(dry_run)
    print(f"  删除文件: {r1['deleted']} 个")
    print(f"  释放空间: {format_bytes(r1['freed_bytes'])}")
    print()

    # 2. 压缩老归档
    print("[2/5] 压缩 30 天前的 daily 归档...")
    r2 = compress_old_daily(30, dry_run)
    print(f"  压缩文件: {r2['compressed']} 个")
    print(f"  节省空间: {format_bytes(r2['saved_bytes'])}")
    print()

    # 3. 裁剪日志
    print("[3/5] 裁剪日志...")
    r3 = trim_log()
    if r3["trimmed"]:
        print(f"  保留最近 {r3['kept_lines']} 行")
    else:
        print(f"  日志已经够短（{r3['kept_lines']} 行），无需裁剪")
    print()

    r4 = {"deleted": 0, "freed_bytes": 0}
    print("[4/5] 清理过期任务上下文...")
    r_sessions = prune_task_sessions(168, dry_run=dry_run)
    print(f"  删除会话: {r_sessions.get('deleted', 0)} 个")
    print(f"  释放空间: {format_bytes(r_sessions.get('freed_bytes', 0))}")
    print()

    print("[5/5] 便携备份清理...")
    if prune_old_exports:
        if not dry_run and not yes:
            print("  已跳过：删除备份需要同时传 --yes")
        else:
            r4 = prune_exports(keep_exports, dry_run=dry_run, yes=yes)
            action = "可释放" if dry_run else "释放"
            print(f"  保留最近: {r4.get('kept', 0)} 个")
            print(f"  删除备份: {r4.get('deleted', 0)} 个")
            print(f"  {action}空间: {format_bytes(r4.get('freed_bytes', 0))}")
    else:
        print("  默认不删除备份。需要清理时运行：cleanup.py --prune-exports --dry-run")
    print()

    total_saved = r1['freed_bytes'] + r2['saved_bytes'] + r_sessions.get('freed_bytes', 0) + r4.get('freed_bytes', 0)
    print(f"==> 总共释放空间: {format_bytes(total_saved)}")
    if not dry_run:
        record_cleanup_run(total_saved)


if __name__ == "__main__":
    main()
