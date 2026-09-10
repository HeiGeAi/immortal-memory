---
name: immortal-memory
description: Use when a task depends on the user's personal memory, writing style, project history, preferences, relationships, past decisions, or digital agent training. Connects Claude Code to the standalone Immortal Memory product.
---

# Immortal Memory Adapter

Before doing a task that depends on user-specific context, create a reviewable
preview:

```bash
python3 -B ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --mode reviewer
```

Read the returned `context_json`. A preview is not task context. After reviewing
its selection, compile that exact preview:

```bash
python3 -B ~/.local/share/immortal-memory/core/immortal.py agent-context "<current task>" --mode reviewer --preview-id "<preview_id>" --preview-hash "<preview_hash>" --print
```

`lifecycle_status=compiled` alone does not authorize use. It means the exact
pack is frozen and ready for verification, not that Claude Code has accepted
it for this run.

Verify the compiled metadata fields `context_id`, `content_hash`,
`context_markdown_hash`, `pack_snapshot_hash`, and `stream_version` against the
pack actually loaded for this run. Only after Claude Code has accepted that
exact pack, acknowledge it:

```bash
python3 -B ~/.local/share/immortal-memory/core/immortal.py context-ack "<context_id>" --expected-version "<stream_version>" --content-hash "<content_hash>" --context-markdown-hash "<context_markdown_hash>" --pack-snapshot-hash "<pack_snapshot_hash>" --adapter claude-code --run-ref "<stable opaque run id>"
```

Continue only when the acknowledgement returns `lifecycle_status=consumed`;
then use the printed pack as task-local memory. The run ID must be opaque and
contain no task or session text. Retrying the same acknowledgement returns the
same receipt. Do not read the full raw vault, acknowledge an unused pack, or
write an Outcome automatically. Human manual confirmation remains a separate
fallback.

Useful commands:

```bash
python3 -B ~/.local/share/immortal-memory/core/immortal.py health
python3 -B ~/.local/share/immortal-memory/core/immortal.py recall "<topic>"
python3 -B ~/.local/share/immortal-memory/core/immortal.py agent-entry
python3 -B ~/.local/share/immortal-memory/core/immortal.py context-ack "<context_id>" --expected-version "<stream_version>" --content-hash "<content_hash>" --context-markdown-hash "<context_markdown_hash>" --pack-snapshot-hash "<pack_snapshot_hash>" --adapter claude-code --run-ref "<run id>"
```
