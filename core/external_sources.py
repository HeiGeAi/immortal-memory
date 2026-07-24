#!/usr/bin/env python3
"""Read-only imports for external sources registered by an explicit local path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from index_writer import append_jsonl_records
from maintenance_gate import writer_access
from redact_common import redact_tree


SUPPORTED_KINDS = ("git", "github", "claude-web", "chatgpt", "cursor")


def iso_utc(value: Any = None) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    text = str(value or "").strip()
    return text or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def register_source(settings: dict[str, Any], kind: str, path: str | Path) -> dict[str, Any]:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported source kind: {kind}")
    raw = str(path).strip()
    if kind == "github" and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw):
        source = settings.setdefault("external_sources", {}).setdefault(kind, {})
        repositories = [str(item).strip() for item in source.get("repositories") or [] if str(item).strip()]
        if raw not in repositories:
            repositories.append(raw)
        source.update({"enabled": True, "repositories": repositories})
        return settings
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"source path does not exist: {resolved}")
    if kind in {"claude-web", "chatgpt"} and resolved.is_file():
        vault = Path(str(settings.get("vault_dir") or Path.home() / ".immortal")).expanduser()
        import_dir = vault / "imports" / kind
        import_dir.mkdir(parents=True, exist_ok=True)
        imported = import_dir / resolved.name
        shutil.copy2(resolved, imported)
        resolved = imported.resolve()
    source = settings.setdefault("external_sources", {}).setdefault(kind, {})
    paths = [str(Path(item).expanduser().resolve()) for item in source.get("paths") or []]
    if str(resolved) not in paths:
        paths.append(str(resolved))
    source.update({"enabled": True, "paths": paths})
    return settings


def unregister_source(settings: dict[str, Any], kind: str, path: str | Path) -> dict[str, Any]:
    raw = str(path).strip()
    if kind == "github" and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw):
        external = settings.get("external_sources") if isinstance(settings.get("external_sources"), dict) else {}
        source = external.get(kind) if isinstance(external.get(kind), dict) else {}
        source["repositories"] = [item for item in source.get("repositories") or [] if str(item) != raw]
        source["enabled"] = bool(source.get("paths") or source.get("repositories"))
        return settings
    resolved = str(Path(path).expanduser().resolve())
    external = settings.get("external_sources") if isinstance(settings.get("external_sources"), dict) else {}
    source = external.get(kind) if isinstance(external.get(kind), dict) else {}
    source["paths"] = [
        item for item in source.get("paths") or []
        if str(Path(item).expanduser().resolve()) != resolved
    ]
    source["enabled"] = bool(source["paths"] or source.get("repositories"))
    return settings


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def discover_git_repositories(root: str | Path, *, max_depth: int = 4) -> list[Path]:
    resolved = Path(root).expanduser().resolve()
    if (resolved / ".git").exists():
        return [resolved]
    repositories: set[Path] = set()
    for current, dirs, _files in os.walk(resolved, followlinks=False):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(resolved).parts)
        except ValueError:
            dirs[:] = []
            continue
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = [name for name in dirs if name not in {".git", "node_modules", ".venv", "venv"}]
        if (current_path / ".git").exists() and _inside(current_path, resolved):
            repositories.add(current_path.resolve())
            dirs[:] = []
    return sorted(repositories)


def _connection(vault: Path) -> sqlite3.Connection:
    state_dir = vault / "external_sources"
    state_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_dir / "state.sqlite3")
    connection.execute(
        "create table if not exists seen (source text not null, item_key text not null, "
        "first_seen_at text not null, primary key(source,item_key))"
    )
    return connection


def _record(*, source: str, record_type: str, timestamp: Any, content: str,
            item_id: str, role: str = "", project: str = "", session_id: str = "",
            metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    record_id = hashlib.sha256(f"{source}|{item_id}".encode()).hexdigest()[:24]
    return {
        "id": f"{source}-{record_id}", "source": source, "project": project,
        "session_id": session_id, "timestamp": iso_utc(timestamp), "type": record_type,
        "role": role, "content": content, "metadata": metadata or {}, "_dedup_key": item_id,
    }


def _append(vault: Path, records: Iterable[dict[str, Any]]) -> int:
    clean = [redact_tree(item) for item in records]
    if not clean:
        return 0
    by_day: dict[str, list[dict[str, Any]]] = {}
    for item in clean:
        try:
            day = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
        except ValueError:
            day = datetime.now().astimezone().strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(item)
    with writer_access(vault):
        append_jsonl_records(vault / "index.jsonl", clean, maintenance_held=True)
        daily = vault / "daily"
        daily.mkdir(parents=True, exist_ok=True)
        for day, items in by_day.items():
            with (daily / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
                for item in items:
                    handle.write(json.dumps({k: v for k, v in item.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
    return len(clean)


def _collect_records(vault: Path, records: Iterable[dict[str, Any]]) -> int:
    connection = _connection(vault)
    pending: list[dict[str, Any]] = []
    try:
        for item in records:
            cursor = connection.execute(
                "insert or ignore into seen(source,item_key,first_seen_at) values(?,?,?)",
                (item["source"], item["_dedup_key"], iso_utc()),
            )
            if cursor.rowcount:
                pending.append(item)
        written = _append(vault, pending)
        connection.commit()
        return written
    finally:
        connection.close()


def _git_records(repo: Path, max_commits: int) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["git", "log", f"--max-count={max(1, int(max_commits))}", "--date=iso-strict",
         "--format=%H%x1f%aI%x1f%an%x1f%ae%x1f%B%x1e"],
        cwd=repo, text=True, capture_output=True, timeout=60,
    )
    if result.returncode:
        return []
    records = []
    for block in result.stdout.split("\x1e"):
        fields = block.strip().split("\x1f", 4)
        if len(fields) != 5:
            continue
        commit, timestamp, author_name, author_email, message = fields
        records.append(_record(
            source="git-history", record_type="git-commit", timestamp=timestamp,
            content=message.strip(), item_id=f"{repo.resolve()}|{commit}", role="author",
            project=repo.name, session_id=commit,
            metadata={"repository": repo.name, "commit": commit,
                      "author_name": author_name, "author_email": author_email},
        ))
    return records


def collect_git_history(vault: str | Path, roots: Iterable[str | Path], *, max_commits: int = 200) -> dict[str, Any]:
    repositories: set[Path] = set()
    for root in roots:
        repositories.update(discover_git_repositories(root))
    records = [item for repo in sorted(repositories) for item in _git_records(repo, max_commits)]
    return {"repositories": len(repositories), "records_written": _collect_records(Path(vault).expanduser(), records), "status": "success"}


def github_slug_from_remote(remote: str) -> str:
    match = re.match(r"(?:https://github\.com/|git@github\.com:)([^/\s]+/[^/\s]+?)(?:\.git)?$", str(remote or "").strip())
    return match.group(1) if match else ""


def _github_remote(repo: Path) -> str:
    result = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=repo,
                            text=True, capture_output=True, timeout=15)
    return github_slug_from_remote(result.stdout) if result.returncode == 0 else ""


def _gh_list_with_error(repository: str, kind: str, limit: int) -> tuple[list[dict[str, Any]], bool, dict[str, str] | None]:
    command = "pr" if kind == "pull-request" else "issue"
    fields = "number,title,body,state,createdAt,updatedAt,url"
    last_error: dict[str, str] | None = None
    for _attempt in range(2):
        try:
            result = subprocess.run(
                ["gh", command, "list", "--repo", repository, "--state", "all", "--limit", str(limit), "--json", fields],
                text=True, capture_output=True, timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = {"error_type": type(exc).__name__, "message": str(redact_tree(str(exc)))[:240]}
            continue
        if result.returncode:
            if kind == "issue" and "disabled issues" in result.stderr.lower():
                return [], True, None
            last_error = {
                "error_type": "gh_command_failed",
                "message": str(redact_tree(result.stderr.strip() or f"exit {result.returncode}"))[:240],
            }
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            last_error = {"error_type": "JSONDecodeError", "message": str(exc)[:240]}
            continue
        return (data if isinstance(data, list) else []), True, None
    return [], False, last_error


def _gh_list(repository: str, kind: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    items, ok, _error = _gh_list_with_error(repository, kind, limit)
    return items, ok


def github_items_to_records(repository: str, kind: str, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for item in items:
        if not isinstance(item, dict) or not item.get("number"):
            continue
        updated = str(item.get("updatedAt") or item.get("createdAt") or "")
        title, body = str(item.get("title") or "").strip(), str(item.get("body") or "").strip()
        records.append(_record(
            source="github-history", record_type=kind, timestamp=updated,
            content=title + (f"\n\n{body}" if body else ""),
            item_id=f"{repository}|{kind}|{item['number']}|{updated}", role="author",
            project=repository, session_id=f"{kind}-{item['number']}",
            metadata={"repository": repository, "number": int(item["number"]),
                      "state": str(item.get("state") or ""), "url": str(item.get("url") or "")},
        ))
    return records


def collect_github_history(
    vault: str | Path,
    roots: Iterable[str | Path],
    *,
    repositories: Iterable[str] = (),
    max_items: int = 50,
) -> dict[str, Any]:
    local_repositories: set[Path] = set()
    for root in roots:
        local_repositories.update(discover_git_repositories(root))
    slugs = {str(value).strip() for value in repositories if str(value).strip()}
    for repo in sorted(local_repositories):
        slug = _github_remote(repo)
        if slug:
            slugs.add(slug)
    records, errors, error_details = [], 0, []
    for slug in sorted(slugs):
        for kind in ("pull-request", "issue"):
            items, ok, error = _gh_list_with_error(slug, kind, max(1, int(max_items)))
            if ok:
                records.extend(github_items_to_records(slug, kind, items))
            else:
                errors += 1
                error_details.append({"repository": slug, "kind": kind, **(error or {})})
    return {"repositories": len(slugs), "records_written": _collect_records(Path(vault).expanduser(), records),
            "status": "partial" if errors else "success", "error_count": errors,
            "errors": error_details}


def parse_chatgpt_export(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    records = []
    for conversation in data if isinstance(data, list) else []:
        if not isinstance(conversation, dict):
            continue
        cid, title = str(conversation.get("id") or conversation.get("conversation_id") or ""), str(conversation.get("title") or "")
        mapping = conversation.get("mapping") if isinstance(conversation.get("mapping"), dict) else {}
        for node in mapping.values():
            message = node.get("message") if isinstance(node, dict) and isinstance(node.get("message"), dict) else None
            if not message:
                continue
            content = message.get("content") if isinstance(message.get("content"), dict) else {}
            text = "\n".join(part for part in content.get("parts") or [] if isinstance(part, str)).strip()
            if not text:
                continue
            mid = str(message.get("id") or hashlib.sha256(text.encode()).hexdigest()[:16])
            author = message.get("author") if isinstance(message.get("author"), dict) else {}
            records.append(_record(source="chatgpt-conversation", record_type="conversation-message",
                timestamp=message.get("create_time") or conversation.get("create_time"), content=text,
                item_id=f"{cid}|{mid}", role=str(author.get("role") or ""), project=title,
                session_id=cid, metadata={"conversation_title": title}))
    return sorted(records, key=lambda item: item["timestamp"])


def parse_claude_web_export(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    records = []
    for conversation in data if isinstance(data, list) else []:
        if not isinstance(conversation, dict):
            continue
        cid, title = str(conversation.get("uuid") or ""), str(conversation.get("name") or "")
        for message in conversation.get("chat_messages") or []:
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or "").strip()
            if not text:
                text = "\n".join(str(block.get("text") or "") for block in message.get("content") or []
                                 if isinstance(block, dict) and block.get("type") == "text").strip()
            if not text:
                continue
            sender = str(message.get("sender") or "")
            role = "user" if sender in {"human", "user"} else "assistant" if sender == "assistant" else sender
            mid = str(message.get("uuid") or hashlib.sha256(text.encode()).hexdigest()[:16])
            records.append(_record(source="claude-web-conversation", record_type="conversation-message",
                timestamp=message.get("created_at") or conversation.get("created_at"), content=text,
                item_id=f"{cid}|{mid}", role=role, project=title, session_id=cid,
                metadata={"conversation_title": title}))
    return sorted(records, key=lambda item: item["timestamp"])


def parse_cursor_export(path: str | Path) -> list[dict[str, Any]]:
    export = Path(path).expanduser()
    records = []
    with export.open(encoding="utf-8", errors="ignore") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            if not content:
                content = "\n".join(str(block.get("text") or "") for block in message.get("content") or []
                                    if isinstance(block, dict) and block.get("type") == "text")
            text = str(content or "").strip()
            if not text:
                continue
            parts = export.parts
            project = str(item.get("workspace") or item.get("project") or "")
            session = str(item.get("session_id") or "")
            if "agent-transcripts" in parts:
                marker = parts.index("agent-transcripts")
                project = project or (parts[marker - 1] if marker else "")
                session = session or export.parent.name
            records.append(_record(source="cursor-conversation", record_type="conversation-message",
                timestamp=item.get("timestamp"), content=text,
                item_id=str(item.get("id") or f"{export.resolve()}:{line_no}"),
                role=str(item.get("role") or ""), project=project, session_id=session,
                metadata={"import_file": export.name}))
    return records


def _registered_files(source: dict[str, Any], kind: str) -> list[Path]:
    files: set[Path] = set()
    for configured in source.get("paths") or []:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            files.add(path)
        elif path.is_dir():
            pattern = "conversations.json" if kind in {"chatgpt", "claude-web"} else "*.jsonl"
            files.update(candidate.resolve() for candidate in path.rglob(pattern)
                         if candidate.is_file() and _inside(candidate, path))
    return sorted(files)


def collect_registered_sources(settings: dict[str, Any], vault: str | Path) -> dict[str, Any]:
    vault_path = Path(vault).expanduser()
    external = settings.get("external_sources") if isinstance(settings.get("external_sources"), dict) else {}
    result: dict[str, Any] = {}
    for kind, collector, limit_key, default in (
        ("git", collect_git_history, "max_commits", 200),
        ("github", collect_github_history, "max_items", 50),
    ):
        source = external.get(kind) if isinstance(external.get(kind), dict) else {}
        enabled = bool(source.get("enabled"))
        paths = source.get("paths") or []
        registered_repositories = (source.get("repositories") or []) if kind == "github" else []
        if not enabled or (not paths and not registered_repositories):
            result[kind] = {"repositories": 0, "records_written": 0, "status": "disabled"}
            continue
        kwargs = {limit_key: int(source.get(limit_key) or default)}
        if kind == "github":
            kwargs["repositories"] = registered_repositories
        result[kind] = collector(vault_path, paths, **kwargs)
    for kind, parser in (("claude-web", parse_claude_web_export), ("chatgpt", parse_chatgpt_export), ("cursor", parse_cursor_export)):
        source = external.get(kind) if isinstance(external.get(kind), dict) else {}
        if not source.get("enabled") or not source.get("paths"):
            result[kind] = {"files": 0, "records_written": 0, "status": "disabled"}
            continue
        files, records, errors, error_details = _registered_files(source, kind), [], 0, []
        for path in files:
            try:
                records.extend(parser(path))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors += 1
                error_details.append({
                    "file": path.name,
                    "error_type": type(exc).__name__,
                    "message": str(redact_tree(str(exc)))[:240],
                })
        result[kind] = {"files": len(files), "records_written": _collect_records(vault_path, records),
                        "status": "partial" if errors else "success", "error_count": errors,
                        "errors": error_details}
    state_dir = vault_path / "external_sources"
    state_dir.mkdir(parents=True, exist_ok=True)
    temporary = state_dir / "state.json.tmp"
    temporary.write_text(json.dumps({"generated_at": iso_utc(), "sources": result}, ensure_ascii=False,
                                    indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(state_dir / "state.json")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect explicitly registered external sources")
    parser.add_argument("--vault", default=str(Path.home() / ".immortal"))
    parser.add_argument("--git-root", action="append", default=[])
    parser.add_argument("--max-commits", type=int, default=200)
    args = parser.parse_args(argv)
    print(json.dumps(collect_git_history(args.vault, args.git_root, max_commits=args.max_commits),
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
