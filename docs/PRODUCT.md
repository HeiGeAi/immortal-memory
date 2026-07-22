# Product Design

## Product Definition

Immortal Memory is a personal memory infrastructure product for AI users.

The user problem is not "I want a dashboard." The real problem is:

- valuable AI conversations and files can disappear;
- different agents do not know the user's history, preferences, or decisions;
- manual memory cleanup does not scale;
- a generic assistant cannot safely act like a user without evidence and boundaries.

The product answer is a local memory system that continuously captures traces,
distills them, and exposes a controlled context bridge to any agent.

## Core Jobs

1. Anti-loss preservation
   Capture local AI conversations, files, documents, meetings, and connector data into a recoverable vault.

2. Evidence-backed distillation
   Separate the user's own voice from other people's messages, project facts, source quotes, and sensitive content.

3. Agent interoperability
   Provide a stable `ENTRY.md` and task-specific `agent-context` command so agents can understand the user without reading raw archives.

4. Role compilation
   Build scenario agents such as writing reviewer, business advisor, project operator, or meeting analyst from distilled memory.

5. User control
   Keep data local by default. Make collection scopes, identity aliases, account guards, exports, and deletion explicit.

## Product Boundaries

This product should not claim to fully replace a person.

It can:

- recall evidence;
- mirror writing preferences;
- pre-judge routine decisions;
- draft and review;
- explain why it reached a conclusion.

It should not:

- make irreversible decisions without user approval;
- expose raw private messages by default;
- silently collect the wrong account;
- treat one hallucinated profile as permanent truth.

## Main Surfaces

- Dashboard: observe health, memory layers, people, timeline, and agent bridge.
- Agent Bridge: one file and one command for all local agents.
- Factory: collect, clean, train, and compile role agents.
- CLI: reliable automation and scripting surface.
- Adapters: thin wrappers for Codex, Claude Code, and future tools.
