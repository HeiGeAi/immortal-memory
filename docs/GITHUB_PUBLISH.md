# GitHub Publish Guide

## What This Repo Is

This is the public empty shell of Immortal Memory:

- product docs;
- local core code;
- install script;
- Codex adapter;
- Claude Code adapter;
- smoke test;
- private-data scanner.

It is not a dump of any user's memory vault.

## Before Publishing

Run:

```bash
python3 scripts/private_scan.py .
bash scripts/smoke_test.sh
git status --short
```

Confirm these directories are not present:

```text
.immortal/
vault/
data/
exports/
logs/
```

## First Commit

```bash
git add .
git commit -m "Initial Immortal Memory empty shell"
```

## Create GitHub Repo

With GitHub CLI:

```bash
gh repo create immortal-memory --private --source=. --remote=origin --push
```

Switch to public only after reviewing the repository on GitHub.

## Recommended Repository Description

```text
Local-first personal memory layer for AI agents. Capture, distill, and expose task-local context to Codex, Claude Code, and other local agents.
```

## Release Rule

Never publish a release artifact that was produced from a real user's
`~/.immortal/` vault. Release only this empty-shell repository or sanitized
source packages.
