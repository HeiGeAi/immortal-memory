---
name: immortal-memory
description: Use this skill whenever the user asks about personal memory, long-term context, writing style, historical decisions, anti-loss backup, digital agent training, task-local context, recall, or using their own corpus inside Codex. This skill connects Codex to the standalone Immortal Memory product through the local agent bridge.
---

# Immortal Memory Adapter

This is a thin Codex adapter for the standalone Immortal Memory product.

The product core is installed outside this skill at:

```text
~/.local/share/immortal-memory/core/
```

Private data lives in:

```text
~/.immortal/
```

## Mandatory Preflight (run this FIRST, every time)

Before claiming any long-term memory is loaded, run the read-only preflight:

```bash
sh ~/.codex/skills/immortal-memory/preflight.sh
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

For tasks involving the user's preferences, history, writing style, relationships,
projects, decisions, or digital agent behavior, run:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --print
```

`agent-context` enforces the same gate internally: it exits with code 3 and
`context_status=unavailable` instead of generating a context pack from an empty
or demo-only vault. Never pass `--force` unless the user explicitly asks for a
debugging override.

Use the returned context as task-local context. Do not read the full raw vault by
default.

## Backup questions

When the user asks "am I backed up / protected?", do not answer from the vault or
Entry file existing. Check, in order:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py preflight --json   # loss_protection field
python3 ~/.local/share/immortal-memory/core/immortal.py backup-status --verify
```

Only report "protected" when preflight shows `loss_protection: protected`
(healthy vault + daily scheduler + external export + passing restore-check).

## Commands

```bash
sh ~/.codex/skills/immortal-memory/preflight.sh
python3 ~/.local/share/immortal-memory/core/immortal.py health
python3 ~/.local/share/immortal-memory/core/immortal.py agent-entry
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<task>" --print
python3 ~/.local/share/immortal-memory/core/immortal.py recall "<topic>"
python3 ~/.local/share/immortal-memory/core/immortal.py agent-factory
```

## Safety

- Recovery scenes stay read-only: when core or vault is abnormal, report status;
  never auto-initialize, rebuild, overwrite, or retrain.
- Summarize sensitive records.
- Verify factual claims with `recall` when the exact source matters.
- Do not claim to fully replace the user.
- Do not expose raw private chats unless explicitly requested and appropriate.
