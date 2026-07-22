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
set +e
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" task-compile "review a product idea" --mode advisor
TASK_COMPILE_STATUS=$?
set -e
case "$TASK_COMPILE_STATUS" in
  2|3) ;;
  *) exit "$TASK_COMPILE_STATUS" ;;
esac
set +e
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" agent-context "review a product idea" --print >/tmp/immortal-memory-context.txt
CONTEXT_STATUS=$?
set -e
test "$CONTEXT_STATUS" -eq 3
grep -q "context_status=unavailable" /tmp/immortal-memory-context.txt
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
from urllib.error import HTTPError

base = "http://127.0.0.1:${PORT}"
health = json.load(urllib.request.urlopen(base + "/health", timeout=5))
assert health["ok"] is True
payload = json.dumps({"task": "review a product idea", "timeout": 120}).encode()
req = urllib.request.Request(base + "/agent-context", data=payload, headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req, timeout=180)
except HTTPError as exc:
    context = json.load(exc)
    assert exc.code == 500
    assert context["ok"] is False
    assert context["exit_code"] == 3
else:
    raise AssertionError("demo-only vault must not produce a task context")
PY
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
CONTROL_PORT=18765
HOME="$TMP_HOME" python3 "$ROOT/core/profile_review.py" --host 127.0.0.1 --port "$CONTROL_PORT" >/tmp/immortal-memory-control.txt 2>&1 &
CONTROL_PID=$!
trap 'kill "$CONTROL_PID" "$SERVER_PID" 2>/dev/null || true; rm -rf "$TMP_HOME"' EXIT
HOME="$TMP_HOME" python3 - <<PY
import json
import time
import urllib.request

base = "http://127.0.0.1:${CONTROL_PORT}"
for _ in range(30):
    try:
        health = json.load(urllib.request.urlopen(base + "/healthz", timeout=2))
        break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("control center did not start")
assert health == {"status": "ok"}
capabilities = json.load(urllib.request.urlopen(base + "/api/v1/capabilities", timeout=5))
assert {item["id"] for item in capabilities["modules"]} == {
    "overview", "runs", "sources", "memories", "profile", "agent", "backup", "diagnostics"
}
page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
assert 'data-view="home"' in page
assert 'data-view="system"' in page
PY
kill "$CONTROL_PID" 2>/dev/null || true
wait "$CONTROL_PID" 2>/dev/null || true
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" health --max-age-hours 9999 || true
HOME="$TMP_HOME" python3 "$ROOT/core/immortal.py" agent-audit --limit 10 >/tmp/immortal-memory-audit.txt
test -s "$TMP_HOME/.immortal/agent/ENTRY.md"
test -s "$TMP_HOME/.immortal/agent/latest-context.json"
test -s "$TMP_HOME/.immortal/feedback/latest.md"
test -s "$TMP_HOME/.immortal/sessions/latest.md"
test -s "$TMP_HOME/.immortal/agent/access.log"
test -s "$TMP_HOME/.immortal/dashboard.html"
grep -q "immortal_agent_context" /tmp/immortal-memory-mcp.txt
grep -q "agent_context" /tmp/immortal-memory-audit.txt
grep -q "GetNote diary sync" /tmp/immortal-memory-getnote.txt
echo "smoke_test=ok"
