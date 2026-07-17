#!/bin/sh
# Self-contained adapter preflight. Depends on nothing from the core install,
# so it still gives a structured diagnosis when the core has been deleted.
CORE="$HOME/.local/share/immortal-memory/core/immortal.py"
if [ ! -f "$CORE" ]; then
  cat <<EOF
{
  "core_status": "missing",
  "core_path": "$CORE",
  "context_status": "unavailable",
  "reason": "Immortal Memory core is not installed at the expected path. Long-term memory is NOT available.",
  "agent_instruction": "Report this status to the user. Do NOT reinstall, initialize, rebuild, or retrain automatically; the vault may be mid-recovery."
}
EOF
  exit 3
fi
exec python3 "$CORE" preflight --json "$@"
