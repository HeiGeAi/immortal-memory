#!/usr/bin/env python3
"""
Soul Agent - personal digital role v0.3
加载数字人格文件，输出可注入任何 Agent 的完整系统提示词。
"""

import sys
from pathlib import Path

SOUL_FILE = Path.home() / ".immortal/digital-soul.md"


if __name__ == "__main__":
    if not SOUL_FILE.exists():
        print("错误：未找到数字人格文件。")
        print("请先运行: python3 ~/.codex/skills/immortal/distill.py")
        sys.exit(1)

    content = SOUL_FILE.read_text(encoding="utf-8")

    print(content)
    print()
    print("---")
    print("使用指南：")
    print("1. 将上方内容作为人格原料，不要整包无脑注入")
    print("2. 搜索相关记忆: python3 ~/.codex/skills/immortal/immortal.py recall <问题>")
    print("3. 重新蒸馏人格: python3 ~/.codex/skills/immortal/distill.py")
