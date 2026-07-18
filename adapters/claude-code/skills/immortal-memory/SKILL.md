---
name: immortal-memory
description: Use when a task depends on the user's personal memory, writing style, project history, preferences, relationships, past decisions, or digital agent training. Connects Claude Code to the standalone Immortal Memory product.
---

# Immortal Memory Adapter

Before doing a task that depends on user-specific context, run:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --print
```

Use the returned context as task-local memory. Do not read the full raw vault by
default.

Useful commands:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py health
python3 ~/.local/share/immortal-memory/core/immortal.py recall "<topic>"
python3 ~/.local/share/immortal-memory/core/immortal.py agent-entry
```
