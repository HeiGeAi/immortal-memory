---
name: immortal-memory
description: Use when a task depends on the user's personal memory, writing style, project history, preferences, relationships, past decisions, or digital agent training. Connects Claude Code to the standalone Immortal Memory product.
---

# Immortal Memory Adapter

Before doing a task that depends on user-specific context, create a reviewable
preview:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --mode reviewer
```

Read the returned `context_json`. A preview is not task context. After reviewing
its selection, compile that exact preview:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --mode reviewer --preview-id "<preview_id>" --preview-hash "<preview_hash>" --print
```

Continue only when the command reports `lifecycle_status=compiled`. Use the
printed pack as task-local memory. Do not read the full raw vault by default.

Useful commands:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py health
python3 ~/.local/share/immortal-memory/core/immortal.py recall "<topic>"
python3 ~/.local/share/immortal-memory/core/immortal.py agent-entry
```
