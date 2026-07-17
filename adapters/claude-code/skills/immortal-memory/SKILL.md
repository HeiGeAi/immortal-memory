---
name: immortal-memory
description: Use when a task depends on the user's personal memory, writing style, project history, preferences, relationships, past decisions, or digital agent training. Connects Claude Code to the standalone Immortal Memory product.
---

# Immortal Memory Adapter

## Mandatory Preflight (run this FIRST, every time)

Before claiming any long-term memory is loaded, run the read-only preflight:

```bash
sh ~/.claude/skills/immortal-memory/preflight.sh
```

Interpret the structured result:

- `core_status: missing` — the product core has been deleted or never installed.
  Report exactly that to the user. Do NOT reinstall, initialize, rebuild, or
  retrain automatically; the machine may be mid-recovery.
- `context_status: unavailable` (e.g. `vault_status: smoke_only` or `missing`) —
  the vault holds no real memories. Tell the user they are NOT protected by
  long-term memory. Do not fabricate context from the demo records.
- `context_status: degraded` — memory exists but is stale or has a coverage gap
  (`coverage_gap_hours`). You may proceed, but state the gap to the user before
  relying on historical conclusions.
- `context_status: ready` — proceed normally.

Core existence, a zero exit code, or a generated Markdown file NEVER count as
proof that memory is usable. Only `context_status` does.

## Default Flow (after preflight passes)

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --print
```

`agent-context` enforces the same gate internally: it exits with code 3 and
`context_status=unavailable` instead of generating a context pack from an empty
or demo-only vault. Never pass `--force` unless the user explicitly asks for a
debugging override.

Use the returned context as task-local memory. Do not read the full raw vault by
default.

## Backup questions

When the user asks "am I backed up / protected?", check `loss_protection` from
preflight and verify the latest export; never answer from file existence alone:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py preflight --json
python3 ~/.local/share/immortal-memory/core/immortal.py backup-status --verify
```

## Useful commands

```bash
sh ~/.claude/skills/immortal-memory/preflight.sh
python3 ~/.local/share/immortal-memory/core/immortal.py health
python3 ~/.local/share/immortal-memory/core/immortal.py recall "<topic>"
python3 ~/.local/share/immortal-memory/core/immortal.py agent-entry
```

## Safety

Recovery scenes stay read-only: when core or vault is abnormal, report status;
never auto-initialize, rebuild, overwrite, or retrain.
