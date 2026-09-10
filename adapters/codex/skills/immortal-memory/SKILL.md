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

## Default Flow

For tasks involving the user's preferences, history, writing style, relationships,
projects, decisions, or digital agent behavior, first create a reviewable preview:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --mode reviewer
```

Read the returned `context_json`. A preview is not task context. After reviewing
its selection, compile that exact preview:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --mode reviewer --preview-id "<preview_id>" --preview-hash "<preview_hash>" --print
```

Continue only when the command reports `lifecycle_status=compiled`. Use the
printed pack as task-local context. Do not read the full raw vault by default.

## Commands

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py health
python3 ~/.local/share/immortal-memory/core/immortal.py agent-entry
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<task>" --mode reviewer
python3 ~/.local/share/immortal-memory/core/immortal.py recall "<topic>"
python3 ~/.local/share/immortal-memory/core/immortal.py agent-factory
```

## Safety

- Summarize sensitive records.
- Verify factual claims with `recall` when the exact source matters.
- Do not claim to fully replace the user.
- Do not expose raw private chats unless explicitly requested and appropriate.
