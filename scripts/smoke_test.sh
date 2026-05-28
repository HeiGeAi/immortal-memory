#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TMP_HOME"' EXIT

HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" init --owner-display-name "Demo User" --alias demo
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" train --smoke --build-role --goal "writing review" --mode writer
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" feedback >/tmp/immortal-memory-feedback.txt
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" agent-entry
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" getnote-status >/tmp/immortal-memory-getnote.txt
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" task-compile "review a product idea" --mode advisor
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" agent-context "review a product idea" --print >/tmp/immortal-memory-context.txt
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" agent-mcp <<'JSONL' >/tmp/immortal-memory-mcp.txt
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"immortal_agent_entry","arguments":{}}}
JSONL
PORT=18799
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" agent-http --host 127.0.0.1 --port "$PORT" --quiet >/tmp/immortal-memory-http.txt 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$TMP_HOME"' EXIT
sleep 1
HOME="$TMP_HOME" python3 - <<PY
import json
import urllib.request

base = "http://127.0.0.1:${PORT}"
health = json.load(urllib.request.urlopen(base + "/health", timeout=5))
assert health["ok"] is True
payload = json.dumps({"task": "review a product idea", "timeout": 120}).encode()
req = urllib.request.Request(base + "/agent-context", data=payload, headers={"Content-Type": "application/json"}, method="POST")
context = json.load(urllib.request.urlopen(req, timeout=180))
assert "context" in context
PY
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" health --max-age-hours 9999 || true
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" agent-audit --limit 10 >/tmp/immortal-memory-audit.txt
test -s "$TMP_HOME/.immortal/agent/ENTRY.md"
test -s "$TMP_HOME/.immortal/agent/latest-context.md"
test -s "$TMP_HOME/.immortal/feedback/latest.md"
test -s "$TMP_HOME/.immortal/sessions/latest.md"
test -s "$TMP_HOME/.immortal/agent/access.log"
test -s "$TMP_HOME/.immortal/dashboard.html"
grep -q "immortal_agent_context" /tmp/immortal-memory-mcp.txt
grep -q "agent_context" /tmp/immortal-memory-audit.txt
grep -q "GetNote diary sync" /tmp/immortal-memory-getnote.txt
echo "smoke_test=ok"
