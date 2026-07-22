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
    parser.add_argument("--install-daily", action="store_true")
    args = parser.parse_args()

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

    if args.install_daily:
        subprocess.check_call([sys.executable, str(core_target / "immortal.py"), "daily-install"])

    if args.install_codex_adapter:
        copytree_replace(ROOT / "adapters" / "codex" / "skills" / "immortal-memory", Path.home() / ".codex" / "skills" / "immortal-memory")

    if args.install_claude_adapter:
        copytree_replace(ROOT / "adapters" / "claude-code" / "skills" / "immortal-memory", Path.home() / ".claude" / "skills" / "immortal-memory")

    print(f"Installed core: {core_target}")
    print(f"Command: {wrapper}")
    print("Next:")
    print("  immortal-memory train --smoke --build-role --goal 'writing review' --mode writer")
    print("  immortal-memory agent-entry")
    print("  immortal-memory agent-context 'current task' --print")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
