---
name: immortal
description: Personal cyber-immortality memory skill for Codex. Use when the user mentions 永生, 赛博永生, 记忆库, 数字分身, digital soul, immortal, anti-loss backup, preserving AI conversations, personal corpus capture, memory distillation, recall, context, or building a scenario-specific agent from their own data.
---

# Immortal

Immortal is a local-first personal memory skill for Codex.

The priority order is:

1. Preserve traces so important AI conversations, files, documents, and outputs are not lost.
2. Clean and distill those traces into a searchable long-term memory vault.
3. Compile scenario-specific digital agents from the user's own memory when needed.

The live data vault is `~/.immortal/`. The skill folder contains tools and
workflows only; it should not contain private user data.

## First Run

Initialize the local owner profile:

```bash
python3 ~/.codex/skills/immortal/immortal.py init \
  --owner-display-name "Your Name" \
  --alias "Your Alias" \
  --primary-account "Main Account"
```

Run a safe smoke training pass:

```bash
python3 ~/.codex/skills/immortal/immortal.py train --smoke --build-role --goal "写稿审稿流程" --mode writer
```

Generate the stable handoff entry for other agents:

```bash
python3 ~/.codex/skills/immortal/immortal.py agent-entry
cat ~/.immortal/agent/ENTRY.md
```

Install the daily automation after the smoke pass:

```bash
python3 ~/.codex/skills/immortal/immortal.py daily-install
python3 ~/.codex/skills/immortal/immortal.py daily-status
```

Open the local control surface:

```bash
python3 ~/.codex/skills/immortal/profile_review.py --host 127.0.0.1 --port 8765
```

Then open `http://127.0.0.1:8765/agent-factory`.

## Core Commands

```bash
python3 ~/.codex/skills/immortal/immortal.py doctor
python3 ~/.codex/skills/immortal/immortal.py health
python3 ~/.codex/skills/immortal/immortal.py run
python3 ~/.codex/skills/immortal/immortal.py train
python3 ~/.codex/skills/immortal/immortal.py daily-install
python3 ~/.codex/skills/immortal/immortal.py agent-entry
python3 ~/.codex/skills/immortal/immortal.py task-compile "task" --mode auto
python3 ~/.codex/skills/immortal/immortal.py agent-context "task" --print
python3 ~/.codex/skills/immortal/immortal.py agent-factory
python3 ~/.codex/skills/immortal/immortal.py goal
python3 ~/.codex/skills/immortal/immortal.py recall "topic"
python3 ~/.codex/skills/immortal/immortal.py context "task"
python3 ~/.codex/skills/immortal/immortal.py task-compile "target task" --mode auto
python3 ~/.codex/skills/immortal/immortal.py role-distill "stable repeated workflow" --mode auto --install-skill
python3 ~/.codex/skills/immortal/immortal.py backup-status
python3 ~/.codex/skills/immortal/immortal.py export
python3 ~/.codex/skills/immortal/immortal.py restore-guide
```

## Feishu / Lark

For a real Feishu collection, bind the expected account first. Do not collect a
large workspace until the account guard is correct.

```bash
python3 ~/.codex/skills/immortal/immortal.py init \
  --feishu-expected-user-name "Your Feishu Name" \
  --feishu-reject-user-name "Wrong Account Name"

python3 ~/.codex/skills/immortal/immortal.py train \
  --with-feishu \
  --feishu-days 7 \
  --feishu-max-chats 5 \
  --feishu-max-messages 200
```

For cloud document mirroring:

```bash
python3 ~/.codex/skills/immortal/immortal.py feishu-mirror --mode inventory --include-wiki --include-drive-search
python3 ~/.codex/skills/immortal/immortal.py feishu-mirror --mode download --actions fetch_doc,export_markdown --max-jobs 20 --delay 0.5
python3 ~/.codex/skills/immortal/immortal.py feishu-mirror-status
```

## Operating Rules

- Keep private data in `~/.immortal/`, not in the skill folder.
- Treat backups and restore checks as the base requirement before digital role work.
- Use `agent-context`, `recall`, and `context` for task-local evidence instead of pasting raw vault files.
- Other local agents should start from `~/.immortal/agent/ENTRY.md`, then run `agent-context` for the current task.
- Summarize sensitive records; do not expose credentials, customer secrets, or raw private chats.
- A generated role can draft, review, and pre-judge. It must not claim to fully replace the person.
