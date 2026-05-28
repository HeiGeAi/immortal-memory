# Agent Adapters

## Principle

The product is the local memory core. Each AI tool gets a thin adapter.

An adapter should only answer three questions:

1. When should this agent use memory?
2. Which command should it run?
3. What safety boundaries should it follow?

## Universal Handoff

```text
Read ~/.immortal/agent/ENTRY.md first. Then run:
immortal-memory agent-context "<current task>" --print
Use the returned context as task-local memory. Do not read the raw vault by default.
```

## Codex

Install:

```bash
python3 install.py --install-codex-adapter
```

The adapter is installed to:

```text
~/.codex/skills/immortal-memory/
```

It should trigger when the user asks about personal memory, writing style,
historical decisions, long-term context, backup, or digital agents.

## Claude Code

Install:

```bash
python3 install.py --install-claude-adapter
```

The adapter is installed to:

```text
~/.claude/skills/immortal-memory/
```

Optional global instruction snippet:

```text
When a task depends on my personal preferences, history, writing style, project
judgment, relationships, or long-term memory, run:
immortal-memory agent-context "<current task>" --print
Then continue using the returned context. Do not read the raw vault by default.
```

## Generic Local Agent

Any local agent with shell access can call:

```bash
immortal-memory agent-entry
immortal-memory agent-context "current task" --print
immortal-memory recall "topic"
```

## Future HTTP / MCP Mode

The same bridge can be exposed as a local server:

```text
GET /agent-entry
POST /agent-context { "task": "..." }
POST /recall { "query": "..." }
```

Keep the shell bridge as the stable lowest-level interface.
