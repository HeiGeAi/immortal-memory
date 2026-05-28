#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${IMMORTAL_BRIDGE_URL:-http://127.0.0.1:8799}"
AUTH_HEADER=()

if [ -n "${IMMORTAL_BRIDGE_TOKEN:-}" ]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${IMMORTAL_BRIDGE_TOKEN}")
fi

curl -sS "${AUTH_HEADER[@]}" "${BASE_URL}/health"
printf '\n\n'

curl -sS "${AUTH_HEADER[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"task":"review this product decision"}' \
  "${BASE_URL}/agent-context"
printf '\n'
