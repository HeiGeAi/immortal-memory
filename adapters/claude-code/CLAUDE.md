# Immortal Memory

When a task depends on my personal preferences, history, writing style, project
judgment, colleague relationships, or long-term memory, first preview the exact
task context:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --mode reviewer
```

Review the returned `context_json`, then compile the same preview:

```bash
python3 ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --mode reviewer --preview-id "<preview_id>" --preview-hash "<preview_hash>" --print
```

Continue only after `lifecycle_status=compiled`. Do not read the full raw vault.
