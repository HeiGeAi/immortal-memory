#!/usr/bin/env python3
"""HTTP and MCP transports for the Immortal Agent Bridge.

The durable bridge remains the CLI/filesystem interface in agent_bridge.py.
This module exposes the same small surface through:

- a local HTTP server for browser tools and generic agents;
- a minimal MCP stdio server for MCP-compatible clients.

It intentionally exposes task-local context, not raw vault files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import configured_vault_dir, owner_display_name


SKILL_DIR = Path(__file__).resolve().parent
IMMORTAL_PY = SKILL_DIR / "immortal.py"
AGENT_BRIDGE_PY = SKILL_DIR / "agent_bridge.py"
VAULT_DIR = configured_vault_dir()
AGENT_DIR = VAULT_DIR / "agent"
ENTRY_MD = AGENT_DIR / "ENTRY.md"
ENTRY_JSON = AGENT_DIR / "entry.json"
LATEST_CONTEXT_MD = AGENT_DIR / "latest-context.md"
LATEST_CONTEXT_JSON = AGENT_DIR / "latest-context.json"
AUDIT_LOG = AGENT_DIR / "access.log"
AUDIT_LATEST = AGENT_DIR / "access_latest.json"
SERVER_NAME = "immortal-memory"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return default


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def preview(value: Any, *, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def audit_event(payload: dict[str, Any]) -> None:
    event = {
        "ts": now_iso(),
        "server": SERVER_NAME,
        **payload,
    }
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    AUDIT_LATEST.write_text(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tail_audit(limit: int = 50) -> list[dict[str, Any]]:
    if not AUDIT_LOG.exists():
        return []
    lines = AUDIT_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-max(1, limit):]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def is_loopback_host(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def run_cli(args: list[str], *, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(IMMORTAL_PY), *args],
        capture_output=True,
        text=True,
        cwd=str(SKILL_DIR),
        timeout=timeout,
    )


def ensure_agent_entry() -> dict[str, Any]:
    if not ENTRY_MD.exists() or not ENTRY_JSON.exists():
        subprocess.run(
            [sys.executable, str(AGENT_BRIDGE_PY), "entry"],
            capture_output=True,
            text=True,
            cwd=str(SKILL_DIR),
            timeout=30,
        )
    entry = read_text(ENTRY_MD, "Agent entry is missing.")
    meta = read_json(ENTRY_JSON, {})
    return {
        "ok": bool(entry.strip()) and ENTRY_MD.exists(),
        "entry": entry,
        "metadata": meta,
        "paths": {
            "entry_md": str(ENTRY_MD),
            "entry_json": str(ENTRY_JSON),
        },
    }


def build_context(task: str, *, since: str = "2026-03-01", with_recall: bool = False, timeout: int = 240) -> dict[str, Any]:
    query = (task or "当前任务").strip()
    args = ["agent-context", query, "--since", since, "--timeout", str(timeout), "--print"]
    if with_recall:
        args.append("--with-recall")
    result = run_cli(args, timeout=max(timeout + 10, 30))
    context = read_text(LATEST_CONTEXT_MD, "")
    payload = read_json(LATEST_CONTEXT_JSON, {})
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "task": query,
        "context": context or result.stdout,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "metadata": payload,
        "paths": {
            "context_md": str(LATEST_CONTEXT_MD),
            "context_json": str(LATEST_CONTEXT_JSON),
        },
    }


def recall(query: str, *, source: str | None = None, since: str | None = None, timeout: int = 120) -> dict[str, Any]:
    topic = (query or "").strip()
    if not topic:
        return {"ok": False, "exit_code": 2, "query": topic, "stdout": "", "stderr": "query is required"}
    args = ["recall", topic]
    if source:
        args.extend(["--source", source])
    if since:
        args.extend(["--since", since])
    result = run_cli(args, timeout=timeout)
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "query": topic,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def health_payload() -> dict[str, Any]:
    state = read_json(VAULT_DIR / "orchestrator_state.json", {})
    quality = read_json(VAULT_DIR / "quality" / "latest.json", {})
    return {
        "ok": True,
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "owner": owner_display_name(),
        "vault_dir": str(VAULT_DIR),
        "total_records": state.get("total_records"),
        "last_collect": state.get("last_collect"),
        "quality": {
            "status": quality.get("status"),
            "score": quality.get("score"),
            "issue_count": quality.get("issue_count"),
        },
        "endpoints": {
            "health": "GET /health",
            "agent_entry": "GET /agent-entry or GET /api/agent-entry",
            "agent_context": "POST /agent-context",
            "recall": "POST /recall",
        },
        "mcp_tools": ["immortal_agent_entry", "immortal_agent_context", "immortal_recall"],
    }


class AgentBridgeHTTPHandler(BaseHTTPRequestHandler):
    server_version = "ImmortalAgentBridgeHTTP/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def end_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://127.0.0.1", "http://localhost")):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Immortal-Token")
        super().end_headers()

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, *, content_type: str = "text/plain; charset=utf-8", status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        token = getattr(self.server, "token", "")
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        header_token = self.headers.get("X-Immortal-Token", "")
        return auth == f"Bearer {token}" or header_token == token

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        started = time.monotonic()
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/health", "/api/health"}:
            payload = health_payload()
            self.send_json(payload)
            audit_event(
                {
                    "transport": "http",
                    "action": "health",
                    "method": "GET",
                    "path": parsed.path,
                    "status": 200,
                    "authorized": True,
                    "token_required": bool(getattr(self.server, "token", "")),
                    "remote": self.client_address[0] if self.client_address else "",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            return
        if not self.require_auth():
            audit_event(
                {
                    "transport": "http",
                    "action": "unauthorized",
                    "method": "GET",
                    "path": parsed.path,
                    "status": 401,
                    "authorized": False,
                    "token_required": bool(getattr(self.server, "token", "")),
                    "remote": self.client_address[0] if self.client_address else "",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            return
        if parsed.path == "/agent-entry":
            result = ensure_agent_entry()
            self.send_text(result["entry"], content_type="text/markdown; charset=utf-8")
            audit_event(
                {
                    "transport": "http",
                    "action": "agent_entry",
                    "method": "GET",
                    "path": parsed.path,
                    "status": 200,
                    "authorized": True,
                    "response_chars": len(result.get("entry") or ""),
                    "remote": self.client_address[0] if self.client_address else "",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            return
        if parsed.path == "/api/agent-entry":
            result = ensure_agent_entry()
            self.send_json(result)
            audit_event(
                {
                    "transport": "http",
                    "action": "agent_entry",
                    "method": "GET",
                    "path": parsed.path,
                    "status": 200,
                    "authorized": True,
                    "response_chars": len(result.get("entry") or ""),
                    "remote": self.client_address[0] if self.client_address else "",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            return
        if parsed.path == "/openapi.json":
            self.send_json(openapi_payload())
            audit_event(
                {
                    "transport": "http",
                    "action": "openapi",
                    "method": "GET",
                    "path": parsed.path,
                    "status": 200,
                    "authorized": True,
                    "remote": self.client_address[0] if self.client_address else "",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            return
        self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        audit_event(
            {
                "transport": "http",
                "action": "not_found",
                "method": "GET",
                "path": parsed.path,
                "status": 404,
                "authorized": True,
                "remote": self.client_address[0] if self.client_address else "",
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )

    def do_POST(self) -> None:
        started = time.monotonic()
        parsed = urlparse(self.path)
        if not self.require_auth():
            audit_event(
                {
                    "transport": "http",
                    "action": "unauthorized",
                    "method": "POST",
                    "path": parsed.path,
                    "status": 401,
                    "authorized": False,
                    "token_required": bool(getattr(self.server, "token", "")),
                    "remote": self.client_address[0] if self.client_address else "",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            return
        payload = self.read_payload()
        if parsed.path in {"/agent-context", "/api/agent-context"}:
            task = str(payload.get("task") or payload.get("query") or "当前任务")
            result = build_context(
                task,
                since=str(payload.get("since") or "2026-03-01"),
                with_recall=bool(payload.get("with_recall")),
                timeout=int(payload.get("timeout") or 240),
            )
            self.send_json(result, HTTPStatus.OK if result["ok"] else HTTPStatus.INTERNAL_SERVER_ERROR)
            audit_event(
                {
                    "transport": "http",
                    "action": "agent_context",
                    "method": "POST",
                    "path": parsed.path,
                    "status": 200 if result["ok"] else 500,
                    "authorized": True,
                    "task_hash": stable_hash(task),
                    "task_preview": preview(task),
                    "ok": bool(result["ok"]),
                    "exit_code": result.get("exit_code"),
                    "response_chars": len(result.get("context") or result.get("stdout") or ""),
                    "remote": self.client_address[0] if self.client_address else "",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            return
        if parsed.path in {"/recall", "/api/recall"}:
            query = str(payload.get("query") or "")
            result = recall(
                query,
                source=str(payload.get("source") or "") or None,
                since=str(payload.get("since") or "") or None,
                timeout=int(payload.get("timeout") or 120),
            )
            self.send_json(result, HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST)
            audit_event(
                {
                    "transport": "http",
                    "action": "recall",
                    "method": "POST",
                    "path": parsed.path,
                    "status": 200 if result["ok"] else 400,
                    "authorized": True,
                    "query_hash": stable_hash(query),
                    "query_preview": preview(query),
                    "ok": bool(result["ok"]),
                    "exit_code": result.get("exit_code"),
                    "response_chars": len(result.get("stdout") or result.get("stderr") or ""),
                    "remote": self.client_address[0] if self.client_address else "",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            return
        self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        audit_event(
            {
                "transport": "http",
                "action": "not_found",
                "method": "POST",
                "path": parsed.path,
                "status": 404,
                "authorized": True,
                "remote": self.client_address[0] if self.client_address else "",
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )


def openapi_payload() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Immortal Agent Bridge", "version": SERVER_VERSION},
        "paths": {
            "/health": {"get": {"summary": "Read bridge health"}},
            "/agent-entry": {"get": {"summary": "Read markdown handoff entry"}},
            "/api/agent-entry": {"get": {"summary": "Read handoff entry as JSON"}},
            "/agent-context": {"post": {"summary": "Build task-local context"}},
            "/recall": {"post": {"summary": "Search memory for a topic"}},
        },
    }


def serve_http(args: argparse.Namespace) -> int:
    if not is_loopback_host(args.host) and not args.token and not args.unsafe_no_token:
        print("Refusing to bind a non-loopback HTTP bridge without --token. Use --unsafe-no-token only for trusted local networks.", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), AgentBridgeHTTPHandler)
    server.token = args.token or ""
    server.quiet = bool(args.quiet)
    host, port = server.server_address[:2]
    print(f"Immortal Agent Bridge HTTP: http://{host}:{port}")
    if server.token:
        print("Auth: Bearer token required")
    audit_event(
        {
            "transport": "http",
            "action": "server_start",
            "host": str(host),
            "port": int(port),
            "token_required": bool(server.token),
            "unsafe_no_token": bool(args.unsafe_no_token),
        }
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def mcp_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "immortal_agent_entry",
            "description": "Return the stable Immortal Memory handoff entry for this user.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "immortal_agent_context",
            "description": "Build a task-local context pack from the user's distilled memory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Current task or question."},
                    "since": {"type": "string", "description": "Optional ISO date lower bound.", "default": "2026-03-01"},
                    "with_recall": {"type": "boolean", "default": False},
                    "timeout": {"type": "integer", "default": 240, "minimum": 10, "maximum": 600},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
        },
        {
            "name": "immortal_recall",
            "description": "Search the user's local Immortal Memory vault for a topic.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic to search for."},
                    "source": {"type": "string", "description": "Optional source filter."},
                    "since": {"type": "string", "description": "Optional ISO date lower bound."},
                    "timeout": {"type": "integer", "default": 120, "minimum": 10, "maximum": 600},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    ]


def text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    if name == "immortal_agent_entry":
        result = ensure_agent_entry()
        audit_event(
            {
                "transport": "mcp",
                "action": "tool_call",
                "tool": name,
                "ok": bool(result["ok"]),
                "response_chars": len(result.get("entry") or ""),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
        return text_result(result["entry"], is_error=not result["ok"])
    if name == "immortal_agent_context":
        task = str(arguments.get("task") or "当前任务")
        result = build_context(
            task,
            since=str(arguments.get("since") or "2026-03-01"),
            with_recall=bool(arguments.get("with_recall")),
            timeout=int(arguments.get("timeout") or 240),
        )
        text = result.get("context") or result.get("stdout") or result.get("stderr") or ""
        audit_event(
            {
                "transport": "mcp",
                "action": "tool_call",
                "tool": name,
                "task_hash": stable_hash(task),
                "task_preview": preview(task),
                "ok": bool(result["ok"]),
                "exit_code": result.get("exit_code"),
                "response_chars": len(str(text)),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
        return text_result(str(text), is_error=not result["ok"])
    if name == "immortal_recall":
        query = str(arguments.get("query") or "")
        result = recall(
            query,
            source=str(arguments.get("source") or "") or None,
            since=str(arguments.get("since") or "") or None,
            timeout=int(arguments.get("timeout") or 120),
        )
        text = result.get("stdout") or result.get("stderr") or ""
        audit_event(
            {
                "transport": "mcp",
                "action": "tool_call",
                "tool": name,
                "query_hash": stable_hash(query),
                "query_preview": preview(query),
                "ok": bool(result["ok"]),
                "exit_code": result.get("exit_code"),
                "response_chars": len(str(text)),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
        return text_result(str(text), is_error=not result["ok"])
    audit_event(
        {
            "transport": "mcp",
            "action": "tool_call",
            "tool": name,
            "ok": False,
            "error": "unknown_tool",
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }
    )
    return text_result(f"Unknown tool: {name}", is_error=True)


def jsonrpc_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def jsonrpc_error(message_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def handle_mcp_message(message: dict[str, Any]) -> dict[str, Any] | None:
    method = str(message.get("method") or "")
    message_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if message_id is None and method.startswith("notifications/"):
        return None
    try:
        if method == "initialize":
            protocol = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
            return jsonrpc_result(
                message_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": "Use immortal_agent_context before tasks that depend on the user's history, preferences, relationships, or prior decisions.",
                },
            )
        if method == "ping":
            return jsonrpc_result(message_id, {})
        if method == "tools/list":
            return jsonrpc_result(message_id, {"tools": mcp_tool_definitions()})
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            return jsonrpc_result(message_id, call_mcp_tool(name, arguments))
        if method.startswith("notifications/"):
            return None
        return jsonrpc_error(message_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        return jsonrpc_error(message_id, -32603, "Internal error", {"detail": str(exc)})


def serve_mcp(_args: argparse.Namespace) -> int:
    audit_event({"transport": "mcp", "action": "server_start"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception as exc:
            print(json.dumps(jsonrpc_error(None, -32700, "Parse error", {"detail": str(exc)}), ensure_ascii=False), flush=True)
            continue
        if not isinstance(message, dict):
            print(json.dumps(jsonrpc_error(None, -32600, "Invalid request"), ensure_ascii=False), flush=True)
            continue
        response = handle_mcp_message(message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def command_audit(args: argparse.Namespace) -> int:
    rows = tail_audit(args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("No Agent Bridge audit events yet.")
        return 0
    for item in rows:
        action = item.get("action") or "-"
        transport = item.get("transport") or "-"
        detail = item.get("tool") or item.get("path") or item.get("task_preview") or item.get("query_preview") or ""
        status = item.get("status") or ("ok" if item.get("ok") is True else "attention" if item.get("ok") is False else "")
        print(f"{item.get('ts')}  {transport:<4}  {action:<14}  {status!s:<9}  {detail}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Immortal Agent Bridge over HTTP or MCP stdio")
    sub = parser.add_subparsers(dest="command")
    http = sub.add_parser("http", help="Start the local HTTP bridge")
    http.add_argument("--host", default="127.0.0.1")
    http.add_argument("--port", type=int, default=8799)
    http.add_argument("--token", default="", help="Optional bearer token for non-health endpoints")
    http.add_argument("--quiet", action="store_true")
    http.add_argument("--unsafe-no-token", action="store_true", help="Allow non-loopback HTTP binding without a token")
    http.set_defaults(func=serve_http)
    mcp = sub.add_parser("mcp", help="Start the MCP stdio bridge")
    mcp.set_defaults(func=serve_mcp)
    audit = sub.add_parser("audit", help="Show recent Agent Bridge access events")
    audit.add_argument("--limit", type=int, default=50)
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=command_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        args = parser.parse_args(["http"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
