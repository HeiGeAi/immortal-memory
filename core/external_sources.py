#!/usr/bin/env python3
"""Controlled, local-only imports for explicitly registered external sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from redact_common import redact_tree


SUPPORTED_KINDS = ("git", "github", "claude-web", "chatgpt", "cursor")


def iso_utc(value: Any = None) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    text = str(value or "").strip()
    if text:
        return text
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def register_source(config: dict[str, Any], kind: str, path: str | Path) -> dict[str, Any]:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported source kind: {kind}")
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"source path does not exist: {resolved}")
    external = config.setdefault("external_sources", {})
    source = external.setdefault(kind, {})
    paths = [str(Path(item).expanduser().resolve()) for item in source.get("paths") or []]
    if str(resolved) not in paths:
        paths.append(str(resolved))
    source["paths"] = paths
    source["enabled"] = True
    return config


def unregister_source(config: dict[str, Any], kind: str, path: str | Path) -> dict[str, Any]:
    resolved = str(Path(path).expanduser().resolve())
    external = config.get("external_sources") if isinstance(config.get("external_sources"), dict) else {}
    source = external.get(kind) if isinstance(external.get(kind), dict) else {}
    source["paths"] = [
        item for item in (source.get("paths") or []) if str(Path(item).expanduser().resolve()) != resolved
    ]
    source["enabled"] = bool(source["paths"])
    return config


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def discover_git_repositories(root: str | Path, *, max_depth: int = 4) -> list[Path]:
    resolved_root = Path(root).expanduser().resolve()
    if (resolved_root / ".git").exists():
        return [resolved_root]
    repositories: set[Path] = set()
    for current, dirs, _files in os.walk(resolved_root, followlinks=False):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(resolved_root).parts)
        except ValueError:
            dirs[:] = []
            continue
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = [name for name in dirs if name not in {".git", "node_modules", ".venv", "venv"}]
        if (current_path / ".git").exists() and _inside(current_path, resolved_root):
            repositories.add(current_path.resolve())
            dirs[:] = []
    return sorted(repositories)


def _state_connection(vault: Path) -> sqlite3.Connection:
    state_dir = vault / "external_sources"
    state_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_dir / "state.sqlite3")
    connection.execute(
        "create table if not exists seen (source text not null, item_key text not null, first_seen_at text not null, primary key(source,item_key))"
    )
    return connection


def _new_record(
    *,
    source: str,
    record_type: str,
    timestamp: str,
    content: str,
    item_id: str,
    role: str = "",
    project: str = "",
    session_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record_id = hashlib.sha256(f"{source}|{item_id}".encode("utf-8")).hexdigest()[:24]
    return {
        "id": f"{source}-{record_id}",
        "source": source,
        "project": project,
        "session_id": session_id,
        "timestamp": iso_utc(timestamp),
        "type": record_type,
        "role": role,
        "content": content,
        "metadata": metadata or {},
        "_dedup_key": item_id,
    }


def _append_records(vault: Path, records: Iterable[dict[str, Any]]) -> int:
    clean_records = [redact_tree(record) for record in records]
    if not clean_records:
        return 0
    vault.mkdir(parents=True, exist_ok=True)
    daily = vault / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[dict[str, Any]]] = {}
    for record in clean_records:
        try:
            parsed = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
            day = parsed.astimezone().strftime("%Y-%m-%d")
        except ValueError:
            day = datetime.now().astimezone().strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(record)
    for day, items in by_day.items():
        with (daily / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps({k: v for k, v in item.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
    with (vault / "index.jsonl").open("a", encoding="utf-8") as handle:
        for item in clean_records:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return len(clean_records)


def _git_records(repo: Path, max_commits: int) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "git",
            "log",
            f"--max-count={max(1, int(max_commits))}",
            "--date=iso-strict",
            "--format=%H%x1f%aI%x1f%an%x1f%ae%x1f%B%x1e",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        return []
    records = []
    for block in result.stdout.split("\x1e"):
        fields = block.strip().split("\x1f", 4)
        if len(fields) != 5:
            continue
        commit, timestamp, author_name, author_email, message = fields
        records.append(
            _new_record(
                source="git-history",
                record_type="git-commit",
                timestamp=timestamp,
                content=message.strip(),
                item_id=f"{repo.resolve()}|{commit}",
                role="author",
                project=repo.name,
                session_id=commit,
                metadata={
                    "repository": repo.name,
                    "commit": commit,
                    "author_name": author_name,
                    "author_email": author_email,
                },
            )
        )
    return records


def collect_git_history(vault: str | Path, roots: Iterable[str | Path], *, max_commits: int = 200) -> dict[str, Any]:
    vault_path = Path(vault).expanduser()
    repositories: set[Path] = set()
    for root in roots:
        repositories.update(discover_git_repositories(root))
    connection = _state_connection(vault_path)
    pending: list[dict[str, Any]] = []
    try:
        for repo in sorted(repositories):
            for record in _git_records(repo, max_commits):
                cursor = connection.execute(
                    "insert or ignore into seen(source,item_key,first_seen_at) values(?,?,?)",
                    (record["source"], record["_dedup_key"], iso_utc()),
                )
                if cursor.rowcount:
                    pending.append(record)
        written = _append_records(vault_path, pending)
        connection.commit()
    finally:
        connection.close()
    return {"repositories": len(repositories), "records_written": written}


def github_slug_from_remote(remote: str) -> str:
    value = str(remote or "").strip()
    match = re.match(r"(?:https://github\.com/|git@github\.com:)([^/\s]+/[^/\s]+?)(?:\.git)?$", value)
    return match.group(1) if match else ""


def github_items_to_records(repository: str, record_type: str, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("number"):
            continue
        number = int(item["number"])
        updated_at = str(item.get("updatedAt") or item.get("createdAt") or "")
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        content = title + (f"\n\n{body}" if body else "")
        records.append(
            _new_record(
                source="github-history",
                record_type=record_type,
                timestamp=updated_at,
                content=content,
                item_id=f"{repository}|{record_type}|{number}|{updated_at}",
                role="author",
                project=repository,
                session_id=f"{record_type}-{number}",
                metadata={
                    "repository": repository,
                    "number": number,
                    "state": str(item.get("state") or ""),
                    "url": str(item.get("url") or ""),
                },
            )
        )
    return records


def _github_remote(repo: Path) -> str:
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=15,
    )
    return github_slug_from_remote(result.stdout) if result.returncode == 0 else ""


def _gh_list(repository: str, kind: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    command = "pr" if kind == "pull-request" else "issue"
    fields = "number,title,body,state,createdAt,updatedAt,url"
    result = subprocess.run(
        ["gh", command, "list", "--repo", repository, "--state", "all", "--limit", str(limit), "--json", fields],
        text=True,
        capture_output=True,
        timeout=90,
    )
    if result.returncode != 0:
        if kind == "issue" and "disabled issues" in result.stderr.lower():
            return [], True
        return [], False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], False
    return (data if isinstance(data, list) else []), True


def collect_github_history(vault: str | Path, roots: Iterable[str | Path], *, max_items: int = 50) -> dict[str, Any]:
    vault_path = Path(vault).expanduser()
    repositories: set[Path] = set()
    for root in roots:
        repositories.update(discover_git_repositories(root))
    records: list[dict[str, Any]] = []
    github_repositories = 0
    errors = 0
    for repo in sorted(repositories):
        slug = _github_remote(repo)
        if not slug:
            continue
        github_repositories += 1
        for kind in ("pull-request", "issue"):
            items, ok = _gh_list(slug, kind, max(1, int(max_items)))
            if not ok:
                errors += 1
                continue
            records.extend(github_items_to_records(slug, kind, items))
    written = _collect_parsed_records(vault_path, records)
    return {
        "repositories": github_repositories,
        "records_written": written,
        "status": "partial" if errors else "success",
        "error_count": errors,
    }


def _collect_parsed_records(vault: Path, records: Iterable[dict[str, Any]]) -> int:
    connection = _state_connection(vault)
    pending: list[dict[str, Any]] = []
    try:
        for record in records:
            cursor = connection.execute(
                "insert or ignore into seen(source,item_key,first_seen_at) values(?,?,?)",
                (record["source"], record["_dedup_key"], iso_utc()),
            )
            if cursor.rowcount:
                pending.append(record)
        written = _append_records(vault, pending)
        connection.commit()
    finally:
        connection.close()
    return written


def parse_chatgpt_export(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    conversations = data if isinstance(data, list) else []
    records: list[dict[str, Any]] = []
    for conversation in conversations:
        if not isinstance(conversation, dict):
            continue
        conversation_id = str(conversation.get("id") or conversation.get("conversation_id") or "")
        title = str(conversation.get("title") or "")
        mapping = conversation.get("mapping") if isinstance(conversation.get("mapping"), dict) else {}
        for node in mapping.values():
            message = node.get("message") if isinstance(node, dict) and isinstance(node.get("message"), dict) else None
            if not message:
                continue
            content = message.get("content") if isinstance(message.get("content"), dict) else {}
            parts = content.get("parts") if isinstance(content.get("parts"), list) else []
            text = "\n".join(part for part in parts if isinstance(part, str)).strip()
            if not text:
                continue
            message_id = str(message.get("id") or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
            author = message.get("author") if isinstance(message.get("author"), dict) else {}
            records.append(
                _new_record(
                    source="chatgpt-conversation",
                    record_type="conversation-message",
                    timestamp=iso_utc(message.get("create_time") or conversation.get("create_time")),
                    content=text,
                    item_id=f"{conversation_id}|{message_id}",
                    role=str(author.get("role") or ""),
                    project=title,
                    session_id=conversation_id,
                    metadata={"conversation_title": title},
                )
            )
    return sorted(records, key=lambda item: item["timestamp"])


def parse_claude_web_export(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    conversations = data if isinstance(data, list) else []
    records: list[dict[str, Any]] = []
    for conversation in conversations:
        if not isinstance(conversation, dict):
            continue
        conversation_id = str(conversation.get("uuid") or "")
        title = str(conversation.get("name") or "")
        messages = conversation.get("chat_messages") if isinstance(conversation.get("chat_messages"), list) else []
        for message in messages:
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or "").strip()
            if not text:
                blocks = message.get("content") if isinstance(message.get("content"), list) else []
                text = "\n".join(
                    str(block.get("text") or "")
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
            if not text:
                continue
            message_id = str(message.get("uuid") or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
            sender = str(message.get("sender") or "")
            role = "user" if sender in {"human", "user"} else "assistant" if sender == "assistant" else sender
            records.append(
                _new_record(
                    source="claude-web-conversation",
                    record_type="conversation-message",
                    timestamp=iso_utc(message.get("created_at") or conversation.get("created_at")),
                    content=text,
                    item_id=f"{conversation_id}|{message_id}",
                    role=role,
                    project=title,
                    session_id=conversation_id,
                    metadata={"conversation_title": title},
                )
            )
    return sorted(records, key=lambda item: item["timestamp"])


def parse_cursor_export(path: str | Path) -> list[dict[str, Any]]:
    export_path = Path(path).expanduser()
    records: list[dict[str, Any]] = []
    with export_path.open(encoding="utf-8", errors="ignore") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            content_value = item.get("content")
            nested_message = item.get("message") if isinstance(item.get("message"), dict) else {}
            nested_blocks = nested_message.get("content") if isinstance(nested_message.get("content"), list) else []
            if not content_value and nested_blocks:
                content_value = "\n".join(
                    str(block.get("text") or "")
                    for block in nested_blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            content_text = str(content_value or "").strip()
            if not content_text:
                continue
            item_id = str(item.get("id") or f"{export_path.resolve()}:{line_no}")
            path_parts = export_path.parts
            project = str(item.get("workspace") or item.get("project") or "")
            if not project and "agent-transcripts" in path_parts:
                marker = path_parts.index("agent-transcripts")
                if marker > 0:
                    project = path_parts[marker - 1]
            session_id = str(item.get("session_id") or "")
            if not session_id and "agent-transcripts" in path_parts:
                session_id = export_path.parent.name
            records.append(
                _new_record(
                    source="cursor-conversation",
                    record_type="conversation-message",
                    timestamp=iso_utc(item.get("timestamp")),
                    content=content_text,
                    item_id=item_id,
                    role=str(item.get("role") or ""),
                    project=project,
                    session_id=session_id,
                    metadata={"import_file": export_path.name},
                )
            )
    return records


def _registered_files(source: dict[str, Any], kind: str) -> list[Path]:
    files: set[Path] = set()
    for configured in source.get("paths") or []:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            files.add(path)
            continue
        if not path.is_dir():
            continue
        pattern = "conversations.json" if kind in {"chatgpt", "claude-web"} else "*.jsonl"
        for candidate in path.rglob(pattern):
            if candidate.is_file() and _inside(candidate, path):
                files.add(candidate.resolve())
    return sorted(files)


def collect_registered_sources(config: dict[str, Any], vault: str | Path) -> dict[str, Any]:
    vault_path = Path(vault).expanduser()
    external = config.get("external_sources") if isinstance(config.get("external_sources"), dict) else {}
    result: dict[str, Any] = {}

    git_source = external.get("git") if isinstance(external.get("git"), dict) else {}
    if git_source.get("enabled") and git_source.get("paths"):
        result["git"] = collect_git_history(
            vault_path,
            git_source.get("paths") or [],
            max_commits=int(git_source.get("max_commits") or 200),
        )
    else:
        result["git"] = {"repositories": 0, "records_written": 0, "status": "disabled"}

    github_source = external.get("github") if isinstance(external.get("github"), dict) else {}
    if github_source.get("enabled") and github_source.get("paths"):
        result["github"] = collect_github_history(
            vault_path,
            github_source.get("paths") or [],
            max_items=int(github_source.get("max_items") or 50),
        )
    else:
        result["github"] = {"repositories": 0, "records_written": 0, "status": "disabled"}

    for kind, parser in (
        ("claude-web", parse_claude_web_export),
        ("chatgpt", parse_chatgpt_export),
        ("cursor", parse_cursor_export),
    ):
        source = external.get(kind) if isinstance(external.get(kind), dict) else {}
        if not source.get("enabled") or not source.get("paths"):
            result[kind] = {"files": 0, "records_written": 0, "status": "disabled"}
            continue
        files = _registered_files(source, kind)
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        for path in files:
            try:
                records.extend(parser(path))
            except (OSError, json.JSONDecodeError, ValueError):
                errors.append(path.name)
        result[kind] = {
            "files": len(files),
            "records_written": _collect_parsed_records(vault_path, records),
            "status": "partial" if errors else "success",
            "error_count": len(errors),
        }

    state = {
        "generated_at": iso_utc(),
        "sources": result,
    }
    state_dir = vault_path / "external_sources"
    state_dir.mkdir(parents=True, exist_ok=True)
    temporary = state_dir / "state.json.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(state_dir / "state.json")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect explicitly registered external sources")
    parser.add_argument("--vault", default=str(Path.home() / ".immortal"))
    parser.add_argument("--git-root", action="append", default=[])
    parser.add_argument("--max-commits", type=int, default=200)
    args = parser.parse_args(argv)
    result = collect_git_history(args.vault, args.git_root, max_commits=args.max_commits)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
