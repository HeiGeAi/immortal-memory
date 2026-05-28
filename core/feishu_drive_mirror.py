#!/usr/bin/env python3
"""Mirror Feishu Drive/Wiki/Docs resources into the Immortal vault.

The mirror is read-only and checkpointed. It stores metadata, fetched document
content, exported online documents, downloaded files, and a coverage report
under ~/.immortal/feishu/drive_mirror/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IMMORTAL_DIR = Path.home() / ".immortal"
MIRROR_DIR = IMMORTAL_DIR / "feishu" / "drive_mirror"
DB_FILE = MIRROR_DIR / "inventory.sqlite3"
LOG_DIR = MIRROR_DIR / "logs"
OBJECT_DIR = MIRROR_DIR / "objects"
DOC_DIR = MIRROR_DIR / "docs"
EXPORT_DIR = MIRROR_DIR / "exports"
FILE_DIR = MIRROR_DIR / "files"
REPORT_DIR = MIRROR_DIR / "reports"
FAILURES_JSONL = MIRROR_DIR / "failures.jsonl"
COVERAGE_JSON = MIRROR_DIR / "coverage.json"
MANIFEST_JSON = MIRROR_DIR / "manifest.json"
LARK_CLI_CANDIDATES = [
    Path("/opt/homebrew/bin/lark-cli"),
    Path("/usr/local/bin/lark-cli"),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_name(value: str, fallback: str = "untitled") -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", value or "").strip(" ._")
    value = re.sub(r"\s+", " ", value)
    return value[:120] or fallback


def ensure_dirs() -> None:
    for path in [MIRROR_DIR, LOG_DIR, OBJECT_DIR, DOC_DIR, EXPORT_DIR, FILE_DIR, REPORT_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_lark_cli() -> str:
    for candidate in LARK_CLI_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("lark-cli")
    if found:
        return found
    raise FileNotFoundError("lark-cli not found")


def lark_env() -> dict[str, str]:
    allowed = {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "SHELL", "TERM", "TMPDIR"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_") or key.startswith("LARK_") or key.startswith("FEISHU_")
    }
    env.setdefault("PATH", "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    env.setdefault("HOME", str(Path.home()))
    return env


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[{[]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
        return {"items": value}
    raise json.JSONDecodeError("no JSON object found", text, 0)


class MirrorDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.setup()

    def setup(self) -> None:
        self.conn.executescript(
            """
            create table if not exists objects (
              object_key text primary key,
              source text not null,
              token text,
              obj_type text,
              title text,
              parent_key text,
              space_id text,
              node_token text,
              discovered_at text not null,
              updated_at text not null,
              raw_json text not null
            );
            create table if not exists jobs (
              job_key text primary key,
              object_key text not null,
              action text not null,
              status text not null,
              attempts integer not null default 0,
              last_error text,
              output_path text,
              updated_at text not null
            );
            create table if not exists runs (
              run_id text primary key,
              started_at text not null,
              finished_at text,
              mode text,
              args_json text,
              counters_json text,
              errors_json text
            );
            """
        )
        self.conn.commit()

    def upsert_object(self, obj: dict[str, Any]) -> bool:
        key = obj["object_key"]
        existed = self.conn.execute("select 1 from objects where object_key=?", (key,)).fetchone() is not None
        now = iso_now()
        self.conn.execute(
            """
            insert into objects(object_key, source, token, obj_type, title, parent_key, space_id, node_token, discovered_at, updated_at, raw_json)
            values(?,?,?,?,?,?,?,?,?,?,?)
            on conflict(object_key) do update set
              source=excluded.source,
              token=excluded.token,
              obj_type=excluded.obj_type,
              title=excluded.title,
              parent_key=excluded.parent_key,
              space_id=excluded.space_id,
              node_token=excluded.node_token,
              updated_at=excluded.updated_at,
              raw_json=excluded.raw_json
            """,
            (
                key,
                obj.get("source", ""),
                obj.get("token", ""),
                obj.get("obj_type", ""),
                obj.get("title", ""),
                obj.get("parent_key", ""),
                obj.get("space_id", ""),
                obj.get("node_token", ""),
                now,
                now,
                json.dumps(obj.get("raw", obj), ensure_ascii=False, sort_keys=True),
            ),
        )
        self.conn.commit()
        return not existed

    def ensure_job(self, object_key: str, action: str) -> None:
        job_key = f"{object_key}|{action}"
        self.conn.execute(
            """
            insert or ignore into jobs(job_key, object_key, action, status, attempts, updated_at)
            values(?,?,?,?,?,?)
            """,
            (job_key, object_key, action, "pending", 0, iso_now()),
        )
        self.conn.commit()

    def pending_jobs(self, actions: set[str] | None = None, limit: int = 0) -> list[sqlite3.Row]:
        params: list[Any] = []
        where = "where j.status in ('pending','error')"
        if actions:
            placeholders = ",".join("?" for _ in actions)
            where += f" and j.action in ({placeholders})"
            params.extend(sorted(actions))
        sql = (
            "select j.*, o.token, o.obj_type, o.title, o.source, o.raw_json "
            "from jobs j join objects o on o.object_key=j.object_key "
            f"{where} order by j.updated_at asc"
        )
        if limit:
            sql += " limit ?"
            params.append(limit)
        return list(self.conn.execute(sql, params))

    def mark_job(self, job_key: str, status: str, *, error: str = "", output_path: str = "") -> None:
        self.conn.execute(
            """
            update jobs
            set status=?, attempts=attempts+1, last_error=?, output_path=?, updated_at=?
            where job_key=?
            """,
            (status, error[:2000], output_path, iso_now(), job_key),
        )
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        counters: dict[str, Any] = {}
        counters["objects"] = self.conn.execute("select count(*) from objects").fetchone()[0]
        counters["jobs"] = self.conn.execute("select count(*) from jobs").fetchone()[0]
        for row in self.conn.execute("select obj_type, count(*) c from objects group by obj_type"):
            counters[f"object_type:{row['obj_type'] or 'unknown'}"] = row["c"]
        for row in self.conn.execute("select source, count(*) c from objects group by source"):
            counters[f"source:{row['source'] or 'unknown'}"] = row["c"]
        for row in self.conn.execute("select action, status, count(*) c from jobs group by action,status"):
            counters[f"job:{row['action']}:{row['status']}"] = row["c"]
        return counters


class FeishuMirror:
    def __init__(self, args: argparse.Namespace):
        ensure_dirs()
        self.args = args
        self.cli = find_lark_cli()
        self.db = MirrorDB(DB_FILE)
        self.run_id = str(uuid.uuid4())
        self.counters: Counter[str] = Counter()
        self.errors: list[dict[str, Any]] = []
        self.log_file = LOG_DIR / f"run-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.log"

    def log(self, message: str) -> None:
        line = f"[{iso_now()}] {message}"
        print(line, flush=True)
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def error(self, stage: str, detail: str, **fields: Any) -> None:
        payload = {"timestamp": iso_now(), "stage": stage, "detail": detail, **fields}
        self.errors.append(payload)
        append_jsonl(FAILURES_JSONL, payload)
        self.log(f"ERROR {stage}: {detail[:240]}")

    def run_lark(self, argv: list[str], timeout: int = 120, cwd: Path | None = None) -> dict[str, Any]:
        cmd = [self.cli, *argv]
        env = lark_env()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
        text = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0:
            raise RuntimeError(text.strip()[:4000])
        data = extract_json(text)
        if data.get("code") not in (None, 0):
            raise RuntimeError(json.dumps(data, ensure_ascii=False)[:4000])
        return data.get("data") if isinstance(data.get("data"), dict) else data

    def verify_account(self) -> None:
        result = subprocess.run(
            [self.cli, "auth", "status", "--verify"],
            capture_output=True,
            text=True,
            timeout=60,
            env=lark_env(),
        )
        text = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0:
            raise RuntimeError(f"cannot verify lark-cli auth status: {text.strip()[:1000]}")
        body = extract_json(text)
        user_name = str(body.get("userName") or "")
        user_open_id = str(body.get("userOpenId") or "")
        expected_name = self.args.expected_user_name
        expected_open_id = self.args.expected_user_open_id
        reject_name = self.args.reject_user_name
        if expected_name and expected_name not in user_name:
            raise RuntimeError(
                f"lark-cli is authenticated as {user_name}, expected name containing {expected_name}. "
                "Refusing to mirror the wrong Feishu account."
            )
        if expected_open_id and expected_open_id != user_open_id:
            raise RuntimeError(
                f"lark-cli is authenticated as {user_open_id}, expected {expected_open_id}. "
                "Refusing to mirror the wrong Feishu account."
            )
        if reject_name and reject_name in user_name:
            raise RuntimeError(
                f"lark-cli is authenticated as rejected account {user_name}. "
                "Refusing to mirror the wrong Feishu account."
            )
        self.log(f"verified Feishu account: {user_name} ({user_open_id})")

    def write_object_raw(self, obj: dict[str, Any]) -> None:
        key = obj["object_key"].replace("/", "_").replace("|", "_")
        write_json(OBJECT_DIR / f"{key}.json", obj)

    def add_object(self, obj: dict[str, Any]) -> None:
        self.write_object_raw(obj)
        is_new = self.db.upsert_object(obj)
        if is_new:
            self.counters["objects_new"] += 1
        self.counters["objects_seen"] += 1
        obj_type = (obj.get("obj_type") or "").lower()
        token = obj.get("token") or ""
        key = obj["object_key"]
        if obj_type == "docx" and token:
            self.db.ensure_job(key, "fetch_doc")
            self.db.ensure_job(key, "export_markdown")
            self.db.ensure_job(key, "export_docx")
        elif obj_type == "doc" and token:
            self.db.ensure_job(key, "export_docx")
        elif obj_type == "sheet" and token:
            self.db.ensure_job(key, "export_xlsx")
        elif obj_type == "bitable" and token:
            self.db.ensure_job(key, "export_base")
        elif obj_type == "file" and token:
            self.db.ensure_job(key, "download_file")

    def discover_wiki_spaces(self) -> list[dict[str, Any]]:
        spaces: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params = {"page_size": 50}
            if page_token:
                params["page_token"] = page_token
            try:
                data = self.run_lark(
                    ["wiki", "spaces", "list", "--as", "user", "--params", json.dumps(params, ensure_ascii=False), "--format", "json"],
                    timeout=90,
                )
            except Exception as exc:
                self.error("wiki_spaces", str(exc))
                break
            for item in data.get("items") or []:
                if isinstance(item, dict):
                    spaces.append(item)
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                break
            time.sleep(self.args.delay)
        if not any(str(s.get("space_type")) == "my_library" or str(s.get("space_id")) == "my_library" for s in spaces):
            spaces.append({"space_id": "my_library", "name": "my_library", "space_type": "my_library"})
        self.counters["wiki_spaces"] = len(spaces)
        write_json(REPORT_DIR / "wiki_spaces.json", spaces)
        return spaces

    def discover_wiki_nodes(self, space: dict[str, Any]) -> None:
        space_id = str(space.get("space_id") or "my_library")
        root_name = safe_name(str(space.get("name") or space_id), space_id)
        queue: list[tuple[str, str]] = [("", "")]
        visited_parents: set[str] = set()
        while queue:
            parent_token, parent_key = queue.pop(0)
            visit_key = f"{space_id}|{parent_token}"
            if visit_key in visited_parents:
                continue
            visited_parents.add(visit_key)
            page_token = ""
            while True:
                params: dict[str, Any] = {"space_id": space_id, "page_size": 50}
                if parent_token:
                    params["parent_node_token"] = parent_token
                if page_token:
                    params["page_token"] = page_token
                try:
                    data = self.run_lark(
                        ["wiki", "nodes", "list", "--as", "user", "--params", json.dumps(params, ensure_ascii=False), "--format", "json"],
                        timeout=90,
                    )
                except Exception as exc:
                    self.error("wiki_nodes", str(exc), space_id=space_id, parent_node_token=parent_token)
                    break
                for item in data.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    token = str(item.get("obj_token") or item.get("node_token") or "")
                    node_token = str(item.get("node_token") or "")
                    obj_type = str(item.get("obj_type") or "wiki").lower()
                    title = str(item.get("title") or "")
                    object_key = f"wiki:{space_id}:{node_token or token}"
                    obj = {
                        "object_key": object_key,
                        "source": "wiki",
                        "token": token,
                        "obj_type": obj_type,
                        "title": title,
                        "parent_key": parent_key,
                        "space_id": space_id,
                        "node_token": node_token,
                        "space_name": root_name,
                        "raw": item,
                    }
                    self.add_object(obj)
                    self.counters["wiki_nodes"] += 1
                    if item.get("has_child") and node_token:
                        queue.append((node_token, object_key))
                page_token = data.get("page_token") or ""
                if not data.get("has_more") or not page_token:
                    break
                time.sleep(self.args.delay)

    def discover_drive_search(self) -> None:
        sorts = ["edit_time", "create_time"]
        # Feishu Search v2 currently allows at most 10 doc_types per request.
        # `shortcut` is a pointer rather than primary content, so skip it here.
        doc_types = "doc,sheet,bitable,mindnote,file,wiki,docx,folder,catalog,slides"
        for sort in sorts:
            page_token = ""
            page = 0
            while True:
                page += 1
                argv = [
                    "drive", "+search",
                    "--as", "user",
                    "--query", self.args.query,
                    "--doc-types", doc_types,
                    "--sort", sort,
                    "--page-size", "20",
                    "--format", "json",
                ]
                if page_token:
                    argv.extend(["--page-token", page_token])
                try:
                    data = self.run_lark(argv, timeout=90)
                except Exception as exc:
                    self.error("drive_search", str(exc), sort=sort)
                    break
                items = data.get("items") or data.get("docs") or data.get("results") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    token = self.item_token(item)
                    obj_type = self.item_type(item)
                    title = self.item_title(item)
                    if not token:
                        continue
                    self.add_object(
                        {
                            "object_key": f"drive:{obj_type}:{token}",
                            "source": f"drive_search:{sort}",
                            "token": token,
                            "obj_type": obj_type,
                            "title": title,
                            "parent_key": "",
                            "space_id": "",
                            "node_token": "",
                            "raw": item,
                        }
                    )
                    self.counters["drive_search_items"] += 1
                page_token = data.get("page_token") or ""
                if not data.get("has_more") or not page_token:
                    break
                if self.args.search_page_limit and page >= self.args.search_page_limit:
                    break
                time.sleep(self.args.delay)

    @staticmethod
    def item_token(item: dict[str, Any]) -> str:
        candidates = [
            item.get("token"),
            item.get("file_token"),
            item.get("doc_token"),
            item.get("obj_token"),
            item.get("wiki_token"),
        ]
        for nested_key in ["document", "doc", "file", "wiki", "entity"]:
            nested = item.get(nested_key)
            if isinstance(nested, dict):
                candidates.extend([nested.get("token"), nested.get("file_token"), nested.get("doc_token"), nested.get("obj_token")])
        for value in candidates:
            if value:
                return str(value)
        return ""

    @staticmethod
    def item_type(item: dict[str, Any]) -> str:
        candidates = [item.get("type"), item.get("doc_type"), item.get("docs_type"), item.get("obj_type"), item.get("file_type")]
        for nested_key in ["document", "doc", "file", "wiki", "entity"]:
            nested = item.get(nested_key)
            if isinstance(nested, dict):
                candidates.extend([nested.get("type"), nested.get("doc_type"), nested.get("docs_type"), nested.get("obj_type"), nested.get("file_type")])
        for value in candidates:
            if value:
                value = str(value).lower()
                aliases = {"docx": "docx", "doc": "doc", "sheet": "sheet", "bitable": "bitable", "file": "file"}
                return aliases.get(value, value)
        return "unknown"

    @staticmethod
    def item_title(item: dict[str, Any]) -> str:
        for key in ["title", "name", "file_name"]:
            if item.get(key):
                return str(item.get(key))
        for nested_key in ["document", "doc", "file", "wiki", "entity"]:
            nested = item.get(nested_key)
            if isinstance(nested, dict):
                for key in ["title", "name", "file_name"]:
                    if nested.get(key):
                        return str(nested.get(key))
        return ""

    def output_base(self, row: sqlite3.Row, suffix: str) -> Path:
        title = safe_name(str(row["title"] or row["token"] or row["object_key"]))
        obj_type = safe_name(str(row["obj_type"] or "unknown"))
        token_hash = hashlib.sha1(str(row["object_key"]).encode("utf-8")).hexdigest()[:10]
        return Path(obj_type) / f"{title}__{token_hash}{suffix}"

    def run_jobs(self) -> None:
        actions = set(self.args.actions.split(",")) if self.args.actions else None
        while True:
            jobs = self.db.pending_jobs(actions, limit=self.args.job_batch)
            if not jobs:
                break
            for row in jobs:
                if self.args.max_jobs and self.counters["jobs_attempted"] >= self.args.max_jobs:
                    return
                self.counters["jobs_attempted"] += 1
                try:
                    output = self.run_one_job(row)
                    self.db.mark_job(row["job_key"], "done", output_path=str(output) if output else "")
                    self.counters[f"job_done:{row['action']}"] += 1
                except Exception as exc:
                    attempts = int(row["attempts"] or 0) + 1
                    status = "dead" if attempts >= self.args.max_attempts else "error"
                    self.db.mark_job(row["job_key"], status, error=str(exc))
                    self.error("job", str(exc), action=row["action"], object_key=row["object_key"])
                    self.counters[f"job_{status}:{row['action']}"] += 1
                time.sleep(self.args.delay)

    def run_one_job(self, row: sqlite3.Row) -> Path | None:
        action = row["action"]
        token = str(row["token"] or "")
        obj_type = str(row["obj_type"] or "").lower()
        if not token:
            raise RuntimeError("missing token")
        if action == "fetch_doc":
            path = DOC_DIR / self.output_base(row, ".json")
            if path.exists() and not self.args.overwrite:
                return path
            data = self.run_lark(
                ["docs", "+fetch", "--api-version", "v2", "--as", "user", "--doc", token, "--doc-format", "markdown", "--detail", "simple", "--format", "json"],
                timeout=180,
            )
            write_json(path, data)
            return path
        if action.startswith("export_"):
            if action == "export_markdown":
                ext = "markdown"
                suffix = ".md"
            elif action == "export_docx":
                ext = "docx"
                suffix = ".docx"
            elif action == "export_xlsx":
                ext = "xlsx"
                suffix = ".xlsx"
            elif action == "export_base":
                ext = "base"
                suffix = ".base"
            else:
                raise RuntimeError(f"unknown export action {action}")
            out_dir_rel = Path("exports") / obj_type
            out_dir = MIRROR_DIR / out_dir_rel
            out_dir.mkdir(parents=True, exist_ok=True)
            expected = out_dir / self.output_base(row, suffix).name
            if expected.exists() and not self.args.overwrite:
                return expected
            argv = [
                "drive", "+export",
                "--as", "user",
                "--token", token,
                "--doc-type", "docx" if obj_type == "docx" else obj_type,
                "--file-extension", ext,
                "--output-dir", str(out_dir_rel),
                "--file-name", expected.name,
            ]
            if self.args.overwrite:
                argv.append("--overwrite")
            self.run_lark(argv, timeout=300, cwd=MIRROR_DIR)
            return expected if expected.exists() else out_dir
        if action == "download_file":
            path_rel = Path("files") / self.output_base(row, "")
            path = MIRROR_DIR / path_rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not self.args.overwrite:
                return path
            argv = ["drive", "+download", "--as", "user", "--file-token", token, "--output", str(path_rel)]
            if self.args.overwrite:
                argv.append("--overwrite")
            self.run_lark(argv, timeout=300, cwd=MIRROR_DIR)
            return path
        raise RuntimeError(f"unknown action {action}")

    def write_reports(self) -> None:
        counters = {**self.db.stats(), **dict(self.counters)}
        failures = sum(1 for _ in FAILURES_JSONL.open(encoding="utf-8", errors="ignore")) if FAILURES_JSONL.exists() else 0
        coverage = {
            "generated_at": iso_now(),
            "mirror_dir": str(MIRROR_DIR),
            "db": str(DB_FILE),
            "failures": failures,
            "counters": counters,
            "notes": [
                "Mirror scope is current Feishu user/app visible resources, not guaranteed tenant-wide admin export.",
                "All operations are read-only: wiki/drive/docs list, fetch, export, and download.",
                "Jobs are checkpointed in SQLite and can be resumed safely.",
            ],
        }
        write_json(COVERAGE_JSON, coverage)
        manifest_items = []
        for base in [OBJECT_DIR, DOC_DIR, EXPORT_DIR, FILE_DIR, REPORT_DIR]:
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                stat = path.stat()
                manifest_items.append({
                    "relpath": path.relative_to(MIRROR_DIR).as_posix(),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    "sha256": sha256_file(path) if stat.st_size <= self.args.hash_limit_mb * 1024 * 1024 else "",
                })
        write_json(MANIFEST_JSON, {"generated_at": iso_now(), "items": manifest_items, "totals": {"files": len(manifest_items), "bytes": sum(i["size"] for i in manifest_items)}})

    def run(self) -> int:
        self.log(f"Feishu drive mirror start: mode={self.args.mode}")
        self.verify_account()
        self.db.conn.execute(
            "insert into runs(run_id, started_at, mode, args_json) values(?,?,?,?)",
            (self.run_id, iso_now(), self.args.mode, json.dumps(vars(self.args), ensure_ascii=False, sort_keys=True)),
        )
        self.db.conn.commit()
        try:
            if self.args.mode in {"inventory", "all"}:
                if self.args.include_wiki:
                    for space in self.discover_wiki_spaces():
                        self.log(f"discover wiki space: {space.get('name') or space.get('space_id')}")
                        self.discover_wiki_nodes(space)
                if self.args.include_drive_search:
                    self.log("discover drive search")
                    self.discover_drive_search()
            if self.args.mode in {"download", "all"}:
                self.run_jobs()
            self.write_reports()
            self.db.conn.execute(
                "update runs set finished_at=?, counters_json=?, errors_json=? where run_id=?",
                (iso_now(), json.dumps(dict(self.counters), ensure_ascii=False, sort_keys=True), json.dumps(self.errors[-200:], ensure_ascii=False), self.run_id),
            )
            self.db.conn.commit()
            self.log(f"Feishu drive mirror done: {json.dumps(dict(self.counters), ensure_ascii=False)}")
            return 0 if not self.errors else 1
        except KeyboardInterrupt:
            self.write_reports()
            self.log("interrupted")
            return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mirror Feishu Drive/Wiki/Docs resources into ~/.immortal")
    parser.add_argument("--mode", choices=["inventory", "download", "all"], default="inventory")
    parser.add_argument("--include-wiki", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-drive-search", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--query", default="", help="Drive search query; blank browses by filter where API allows it")
    parser.add_argument("--search-page-limit", type=int, default=0, help="0 means no local limit")
    parser.add_argument("--actions", default="", help="Comma-separated job actions to run")
    parser.add_argument("--job-batch", type=int, default=25)
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--hash-limit-mb", type=int, default=200)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--expected-user-name", default="")
    parser.add_argument("--expected-user-open-id", default="")
    parser.add_argument("--reject-user-name", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return FeishuMirror(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
