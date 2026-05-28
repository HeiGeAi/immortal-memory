# Architecture

## Layers

```text
Source Connectors
  -> Raw Vault
  -> Normalized Index
  -> Clean Layer
  -> Distilled Memory
  -> Profile / People / Evidence
  -> Agent Context Bridge
  -> Codex / Claude Code / Generic Agents
```

## 1. Source Connectors

Connectors collect from local files, AI conversation logs, documents, calendars,
chat systems, meeting transcripts, and enterprise knowledge bases.

Every connector should write source metadata, timestamps, account identity, and
deduplication keys. A connector should fail partially rather than corrupt the
whole run.

## 2. Raw Vault

The vault is stored outside the repo, usually at `~/.immortal/`.

The vault keeps raw and normalized records so the user can recover data after
tool failures or accidental deletion.

## 3. Clean and Distill

Cleaning removes obvious noise and classifies content:

- user voice;
- other people's voice;
- project fact;
- preference;
- commitment;
- sensitive item;
- reference material.

Distillation turns source records into compact memories with evidence pointers.

## 4. Profile and Evidence

The profile is not a roleplay file. It is a living index of verified preferences,
identity, work context, writing style, relationships, and decision models.

People and relationship indexes are supporting evidence. They should help
agents avoid confusing the user with colleagues or counterparties.

## 5. Agent Context Bridge

Agents should not read the raw vault directly.

The bridge exposes:

- `~/.immortal/agent/ENTRY.md`: stable handoff entry.
- `immortal-memory agent-context "<task>" --print`: task-local context pack.
- `immortal-memory recall "<topic>"`: evidence lookup.

## 6. Adapters

Adapters are intentionally thin:

- Codex skill: tells Codex when and how to call the bridge.
- Claude Code skill: same for Claude Code.
- Generic shell: one command works anywhere.
- Future MCP/server mode: expose the same bridge over a local API.

Adapters should never own the data model. The core product owns the vault.
