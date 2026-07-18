#!/usr/bin/env python3
"""Generate the Obsidian reading layer for the Immortal vault."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import configured_vault_dir, load_config


DEFAULT_VAULT_PATH = Path.home() / "Documents" / "Immortal Obsidian Vault"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_jsonl_tail(path: Path, limit: int = 30) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(item)
    return rows[-limit:]


def obsidian_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("obsidian") if isinstance(config.get("obsidian"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "vault_path": str(raw.get("vault_path") or DEFAULT_VAULT_PATH),
    }


def status_path(immortal_dir: Path) -> Path:
    return immortal_dir / "obsidian" / "status.json"


def md_escape(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()


def write_file(path: Path, text: str, *, dry_run: bool, actions: list[str]) -> None:
    actions.append(f"write {path}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def ensure_symlink(link: Path, target: Path, *, dry_run: bool, actions: list[str], conflicts: list[str]) -> None:
    actions.append(f"link {link} -> {target}")
    if dry_run:
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        current = Path(os.readlink(link))
        if current == target:
            return
        link.unlink()
    elif link.exists():
        conflicts.append(f"{link} already exists")
        return
    link.symlink_to(target)


def managed_symlinks(immortal_dir: Path) -> list[tuple[str, Path]]:
    return [
        ("入口/今日反馈.md", immortal_dir / "feedback" / "latest.md"),
        ("入口/今日摘要.md", immortal_dir / "digests" / "latest.md"),
        ("入口/质量报告.md", immortal_dir / "quality" / "latest.md"),
        ("入口/Agent入口.md", immortal_dir / "agent" / "ENTRY.md"),
        ("入口/最新Agent上下文.md", immortal_dir / "agent" / "latest-context.md"),
        ("入口/产品目标.md", immortal_dir / "product" / "goal.md"),
        ("画像/profile.md", immortal_dir / "profile.md"),
        ("画像/profile_compact.md", immortal_dir / "profile_compact.md"),
        ("画像/profile_nuwa.md", immortal_dir / "profile_nuwa.md"),
        ("画像/digital-soul.md", immortal_dir / "digital-soul.md"),
        ("关系/people_index.md", immortal_dir / "people" / "people_index.md"),
        ("关系/relationship_index.md", immortal_dir / "relationships" / "relationship_index.md"),
        ("仪表盘/dashboard.html", immortal_dir / "dashboard.html"),
        ("仪表盘/timeline.html", immortal_dir / "timeline.html"),
        ("每日摘要", immortal_dir / "summaries"),
        ("反馈历史", immortal_dir / "feedback" / "history"),
        ("飞书蒸馏", immortal_dir / "feishu" / "distilled"),
        ("Get笔记日记", immortal_dir / "getnote"),
        ("Agent上下文", immortal_dir / "agent"),
        ("网页正文", immortal_dir / "web" / "pages"),
    ]


def render_entry(immortal_dir: Path) -> str:
    return f"""# 赛博永生记忆库

这是 `~/.immortal` 的 Obsidian 阅读层。事实仓库仍然是 `{immortal_dir}`，这里负责阅读、复核和入口导航。

## 每天先看

- [[入口/今日反馈|今日反馈]]
- [[入口/今日摘要|今日摘要]]
- [[入口/质量报告|质量报告]]
- [[入口/Agent入口|Agent 入口]]
- [[入口/最新Agent上下文|最新 Agent 上下文]]

## 主要索引

- [[网页收录]]
- [[笔记收录]]
- [[人物索引]]
- [[项目索引]]
- [[断链报告]]
- [[每日摘要]]
- [[反馈历史]]
- [[飞书蒸馏]]
- [[Get笔记日记]]
- [[Agent上下文]]

## 使用规则

- Obsidian 只做阅读和复核，不写入事实仓库。
- 唯一例外：`笔记/` 文件夹手写笔记会被 notes-sync 单向摄入事实仓库，见 [[笔记收录]]。
- 需要让 Agent 使用记忆时，先走 [[入口/Agent入口|Agent 入口]]。
- 网页浏览历史默认只收录元信息，正文只来自白名单或手动保存。
"""


def render_rules() -> str:
    return """# 使用规则

- `~/.immortal` 是唯一事实仓库。
- Obsidian 是阅读层，不负责采集、清洗、召回和备份。
- 唯一例外：`笔记/` 文件夹（不含下划线开头文件）由 `immortal.py notes-sync` 单向摄入事实仓库，写完笔记记得跑一遍同步，见 [[笔记收录]]。
- 目录型入口旁边会生成同名 `.md` 索引页，避免点击时创建空 note。
- 发现断链时先运行 `immortal.py obsidian-sync`，再看 [[断链报告]]。
"""


def render_directory_index(title: str, link_name: str, target: Path) -> str:
    return f"""# {title}

对应本地路径：`{target}`

## 入口

- [[{link_name}|打开目录入口]]
- [[00_永生知识库入口|返回总入口]]
"""


def render_web_index(immortal_dir: Path) -> str:
    state = read_json(immortal_dir / "web" / "state.json", {})
    totals = state.get("totals") if isinstance(state, dict) else {}
    pages = read_jsonl_tail(immortal_dir / "web" / "index.jsonl", 40)
    rows = []
    for item in reversed(pages):
        rows.append(
            f"- {md_escape(item.get('saved_at'))[:16]} "
            f"[{md_escape(item.get('title')) or md_escape(item.get('domain'))}]({md_escape(item.get('path'))}) "
            f"`{md_escape(item.get('domain'))}`"
        )
    if not rows:
        rows.append("- 暂无手动网页正文快照。")
    return f"""# 网页收录

## 状态

- 最近扫描：{md_escape(state.get('generated_at')) or 'missing'}
- 状态：{md_escape(state.get('status')) or 'missing'}
- 本轮访问元信息：{int((totals or {}).get('records_written') or 0)}
- 本轮过滤：{int((totals or {}).get('visits_filtered') or 0)}
- 本轮错误：{int((totals or {}).get('errors') or 0)}

## 最近正文快照

{chr(10).join(rows)}

## 命令

```bash
python3 ~/.codex/skills/immortal/immortal.py web-collect
python3 ~/.codex/skills/immortal/immortal.py web-save "https://example.com" --note "为什么要留"
python3 ~/.codex/skills/immortal/immortal.py web-status
```
"""


def render_notes_index(immortal_dir: Path) -> str:
    state = read_json(immortal_dir / "notes" / "state.json", {})
    totals = state.get("totals") if isinstance(state, dict) else {}
    recent = state.get("recent") if isinstance(state, dict) else []
    rows = []
    for item in reversed(recent or []):
        rows.append(
            f"- {md_escape(item.get('timestamp'))[:16]} "
            f"[{md_escape(item.get('title'))}]({md_escape(item.get('path'))})"
        )
    if not rows:
        rows.append("- 暂无已摄入的手写笔记。")
    return f"""# 笔记收录

## 状态

- 最近同步：{md_escape(state.get('generated_at')) or 'missing'}
- 已摄入总数：{int((totals or {}).get('total_ingested') or 0)}
- 本轮新增：{int((totals or {}).get('ingested_this_run') or 0)}
- 本轮扫描：{int((totals or {}).get('scanned') or 0)}

## 最近摄入

{chr(10).join(rows)}

## 命令

```bash
python3 ~/.codex/skills/immortal/immortal.py notes-sync
python3 ~/.codex/skills/immortal/immortal.py notes-status
```

写作规范见 [[笔记/_说明|笔记文件夹说明]]。
"""


def render_people_index(immortal_dir: Path) -> str:
    data = read_json(immortal_dir / "people" / "people_index.json", {})
    people = data.get("people") if isinstance(data, dict) else []
    rows = []
    if isinstance(people, list):
        for item in people[:60]:
            name = md_escape(item.get("name"))
            summary = md_escape(item.get("summary") or item.get("description"))
            count = int(item.get("memory_count") or item.get("count") or 0)
            rows.append(f"- **{name or '未命名'}**，{count} 条记忆。{summary}")
    if not rows:
        rows.append("- 暂无人物索引。")
    return f"""# 人物索引

来源：`~/.immortal/people/people_index.json`

{chr(10).join(rows)}
"""


def render_project_index(immortal_dir: Path) -> str:
    data = read_json(immortal_dir / "relationships" / "relationship_index.json", {})
    nodes = data.get("nodes") if isinstance(data, dict) else []
    rows = []
    if isinstance(nodes, list):
        for item in nodes:
            if str(item.get("kind") or item.get("type") or "") != "project":
                continue
            name = md_escape(item.get("name") or item.get("label"))
            count = int(item.get("memory_count") or item.get("count") or item.get("evidence_count") or 0)
            rows.append(f"- **{name or '未命名项目'}**，{count} 条证据。")
            if len(rows) >= 80:
                break
    if not rows:
        rows.append("- 暂无项目索引。")
    return f"""# 项目索引

来源：`~/.immortal/relationships/relationship_index.json`

{chr(10).join(rows)}
"""


def wiki_targets(text: str) -> list[str]:
    targets: list[str] = []
    for match in re.findall(r"\[\[([^\]]+)\]\]", text):
        target = match.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


def target_exists(vault_path: Path, target: str) -> bool:
    raw = vault_path / target
    candidates = [raw]
    if raw.suffix != ".md":
        candidates.append(vault_path / f"{target}.md")
    for candidate in candidates:
        if candidate.exists() or candidate.is_symlink():
            return True
    return False


def find_broken_links(vault_path: Path, files: list[Path]) -> list[dict[str, str]]:
    broken: list[dict[str, str]] = []
    for path in files:
        if not path.exists() or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for target in wiki_targets(text):
            if not target_exists(vault_path, target):
                broken.append({"file": str(path.relative_to(vault_path)), "target": target})
    return broken


def render_broken_report(broken: list[dict[str, str]]) -> str:
    if not broken:
        body = "当前托管入口页没有发现断链。"
    else:
        rows = [f"- `{item['file']}` -> `[[{item['target']}]]`" for item in broken[:200]]
        body = "\n".join(rows)
    return f"""# 断链报告

生成时间：{now_iso()}

## 结果

{body}
"""


def run_sync(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    immortal_dir = configured_vault_dir(config)
    obsidian = obsidian_config(config)
    vault_path = Path(args.vault_path or obsidian["vault_path"]).expanduser()
    dry_run = bool(args.dry_run)
    actions: list[str] = []
    conflicts: list[str] = []
    managed_files: list[Path] = []

    if not obsidian["enabled"]:
        return {
            "generated_at": now_iso(),
            "status": "disabled",
            "vault_path": str(vault_path),
            "broken_links_count": 0,
            "actions": [],
        }

    files = {
        "00_永生知识库入口.md": render_entry(immortal_dir),
        "入口/使用规则.md": render_rules(),
        "网页收录.md": render_web_index(immortal_dir),
        "笔记收录.md": render_notes_index(immortal_dir),
        "人物索引.md": render_people_index(immortal_dir),
        "项目索引.md": render_project_index(immortal_dir),
        "每日摘要.md": render_directory_index("每日摘要", "每日摘要", immortal_dir / "summaries"),
        "反馈历史.md": render_directory_index("反馈历史", "反馈历史", immortal_dir / "feedback" / "history"),
        "飞书蒸馏.md": render_directory_index("飞书蒸馏", "飞书蒸馏", immortal_dir / "feishu" / "distilled"),
        "Get笔记日记.md": render_directory_index("Get 笔记日记", "Get笔记日记", immortal_dir / "getnote"),
        "Agent上下文.md": render_directory_index("Agent 上下文", "Agent上下文", immortal_dir / "agent"),
        "网页正文.md": render_directory_index("网页正文", "网页正文", immortal_dir / "web" / "pages"),
        "画像.md": render_directory_index("画像", "画像", immortal_dir),
        "关系.md": render_directory_index("关系", "关系", immortal_dir / "relationships"),
        "仪表盘.md": render_directory_index("仪表盘", "仪表盘", immortal_dir),
    }

    for relative, body in files.items():
        path = vault_path / relative
        managed_files.append(path)
        write_file(path, body, dry_run=dry_run, actions=actions)

    for relative, target in managed_symlinks(immortal_dir):
        ensure_symlink(vault_path / relative, target, dry_run=dry_run, actions=actions, conflicts=conflicts)

    broken = [] if dry_run else find_broken_links(vault_path, managed_files)
    report_path = vault_path / "断链报告.md"
    write_file(report_path, render_broken_report(broken), dry_run=dry_run, actions=actions)
    if not dry_run:
        managed_files.append(report_path)
        broken = find_broken_links(vault_path, managed_files)
        report_path.write_text(render_broken_report(broken), encoding="utf-8")

    result = {
        "generated_at": now_iso(),
        "status": "ok" if not broken and not conflicts else "attention",
        "dry_run": dry_run,
        "vault_path": str(vault_path),
        "immortal_dir": str(immortal_dir),
        "files_managed": len(files) + 1,
        "symlinks_managed": len(managed_symlinks(immortal_dir)),
        "broken_links_count": len(broken),
        "broken_links": broken[:200],
        "conflicts": conflicts,
        "actions": actions[:200],
    }
    if not dry_run:
        path = status_path(immortal_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def command_sync(args: argparse.Namespace) -> int:
    result = run_sync(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "obsidian_sync="
            f"{result.get('status')} "
            f"files={result.get('files_managed', 0)} "
            f"symlinks={result.get('symlinks_managed', 0)} "
            f"broken={result.get('broken_links_count', 0)}"
        )
        if args.dry_run:
            for action in result.get("actions", [])[:40]:
                print(action)
    return 0 if result.get("status") in {"ok", "disabled"} else 2


def command_status(args: argparse.Namespace) -> int:
    config = load_config()
    immortal_dir = configured_vault_dir(config)
    path = status_path(immortal_dir)
    result = read_json(path, {})
    if not result:
        result = {"status": "missing", "status_path": str(path)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"obsidian_status={result.get('status', 'missing')}")
        print(f"vault_path={result.get('vault_path', '')}")
        print(f"broken_links={result.get('broken_links_count', 0)}")
        print(f"status_path={path}")
    return 0 if result.get("status") in {"ok", "attention", "disabled"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync the Immortal Obsidian reading layer")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="Generate Obsidian entry pages, indexes, symlinks, and link report")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--vault-path", default="")
    sync.add_argument("--json", action="store_true")
    sync.set_defaults(func=command_sync)
    status = sub.add_parser("status", help="Show latest Obsidian sync status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
