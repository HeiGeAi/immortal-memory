#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def copytree_replace(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Immortal Memory")
    parser.add_argument("--prefix", default=str(Path.home() / ".local" / "share" / "immortal-memory"))
    parser.add_argument("--owner-display-name", default="")
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--primary-account", default="")
    parser.add_argument("--install-codex-adapter", action="store_true")
    parser.add_argument("--install-claude-adapter", action="store_true")
    parser.add_argument("--install-daily", action="store_true", help="Enable the daily automated capture loop (recommended)")
    parser.add_argument("--no-daily", action="store_true", help="Explicitly skip daily automation; you will NOT be continuously protected")
    args = parser.parse_args()

    if args.install_daily and args.no_daily:
        parser.error("--install-daily and --no-daily are mutually exclusive")

    prefix = Path(args.prefix).expanduser()
    core_target = prefix / "core"
    copytree_replace(ROOT / "core", core_target)

    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "immortal-memory"
    wrapper.write_text(
        "#!/usr/bin/env sh\n"
        f"exec python3 {str(core_target / 'immortal.py')!r} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    init_cmd = [sys.executable, str(core_target / "immortal.py"), "init"]
    if args.owner_display_name:
        init_cmd.extend(["--owner-display-name", args.owner_display_name])
    for alias in args.alias:
        init_cmd.extend(["--alias", alias])
    if args.primary_account:
        init_cmd.extend(["--primary-account", args.primary_account])
    subprocess.check_call(init_cmd)

    install_daily = args.install_daily
    if not install_daily and not args.no_daily:
        if sys.stdin.isatty():
            answer = input("启用每日自动采集？没有它你不会受到持续保护。 [Y/n] ").strip().lower()
            install_daily = answer in {"", "y", "yes"}
        else:
            print()
            print("!" * 56)
            print("  WARNING: 未选择每日自动采集（--install-daily / --no-daily）。")
            print("  当前安装是一次性的：数据不会持续沉淀，你不受持续保护。")
            print("  启用方式：immortal-memory daily-install")
            print("!" * 56)
            print()

    if install_daily:
        subprocess.check_call([sys.executable, str(core_target / "immortal.py"), "daily-install"])
    elif args.no_daily:
        print("已按 --no-daily 跳过每日自动采集：数据不会持续沉淀，你不受持续保护。")
        print("之后可用 `immortal-memory daily-install` 启用。")

    if args.install_codex_adapter:
        copytree_replace(ROOT / "adapters" / "codex" / "skills" / "immortal-memory", Path.home() / ".codex" / "skills" / "immortal-memory")

    if args.install_claude_adapter:
        copytree_replace(ROOT / "adapters" / "claude-code" / "skills" / "immortal-memory", Path.home() / ".claude" / "skills" / "immortal-memory")

    print(f"Installed core: {core_target}")
    print(f"Command: {wrapper}")
    print("Next (DEMO，验证程序能跑，不代表已受保护):")
    print("  immortal-memory train --smoke")
    print("Production readiness (真正开始保护你的数据):")
    print("  immortal-memory run                                  # 接入真实来源并完成一次真实采集")
    print("  immortal-memory daily-install                        # 启用每日自动采集（如尚未启用）")
    print("  immortal-memory backup --output-dir <外置盘或同步目录>  # 外部备份并自动校验")
    print("  immortal-memory preflight                            # 确认 loss_protection: protected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
