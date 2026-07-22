# Privacy and Safety

## Default Rule

User data is local. Repository code is public. Vault data is private.

Never commit:

- `~/.immortal/`
- raw chat logs;
- documents;
- meeting transcripts;
- generated profiles;
- generated roles with private evidence;
- connector credentials;
- API keys;
- exported backups.

## Sensitive Output Policy

Agents may use memory to reason, but should only output what the task requires.

Prefer:

- summaries over raw private chat excerpts;
- evidence IDs over full messages;
- scoped task context over full profile dumps.

## Account Guard

Enterprise connectors should confirm account identity before broad collection.
For example, a Feishu/Lark connector should verify the expected user name or
open ID and reject known wrong accounts.

## Publication Checklist

Before pushing to GitHub:

```bash
python3 scripts/private_scan.py .
git status --short
```

Review every generated file manually if the scan reports a hit.
