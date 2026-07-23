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

## Automation Boundary

Local automation is opt-in and limited to declared fixed handlers. It never
executes arbitrary shell commands, performs cloud upload, or triggers external
actions or notifications on its own. Configure and initiate any connector or
other outward-facing behavior separately on the local machine.

## Controlled Source Boundary

External source adapters read only explicitly registered paths. GitHub PR and
Issue collection is read-only. Registration never grants permission to upload,
modify, merge, comment, or publish through the source account.

## Sensitive Output Policy

Agents may use memory to reason, but should only output what the task requires.

Prefer:

- summaries over raw private chat excerpts;
- evidence IDs over full messages;
- scoped task context over full profile dumps.

## Personal Model Boundary

Personal Model snapshots and user corrections are local derived artifacts.
They must not be uploaded to a remote service by default, embedded into a
public Skill, or treated as a user's identity. The dashboard shows metadata
without correction bodies until a deliberate local reveal. Correction audit
events contain identifiers and scopes, never the correction text.

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

## Portable Recovery Export

A portable recovery export preserves the private fact layer for recovery and
is not a sanitized sharing artifact. If its manifest reports secret shapes,
do not upload or share it. Offsite copies require separate encryption,
versioning, manifest verification, and a real restore drill.
