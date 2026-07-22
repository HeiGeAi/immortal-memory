# Privacy and Safety

## Boundary

Repository code is public. Vault data is private and normally stays in `~/.immortal/`. Public release artifacts and private disaster-recovery exports are separate pipelines. A private backup may contain user-authorized private memory on user-controlled external storage. A public GitHub artifact must contain no private history, credentials, local paths, or generated personal models.

Never commit raw chats, documents, meeting transcripts, generated profiles, Claims, Living Self versions, Judgment cards, Context Packs, Outcomes, connector tokens, API keys, logs, local account identifiers, or backup exports.

Do not create a public release from a live installation and do not treat a
personal-name replacement table as a privacy proof. Release input must be a
clean tracked commit. Local workflow extensions, their configuration, and their
generated project views stay outside Git, wheel, source archives, and adapters.

## Privacy labels

All Claims and Judgment cards carry one of these labels:

| Label | Meaning | Context Pack behavior |
|---|---|---|
| `private` | Sensitive local evidence or model content | Body is always excluded |
| `restricted` | Usable only under an explicit matching task scope and policy | Included only after policy and scope checks |
| `context_safe` | Approved for scoped agent context | Still filtered by relevance, evidence, scope, and size budget |
| `public` | Explicitly classified as public material | Still filtered by relevance, evidence, scope, and size budget |

Absence of a label is invalid, not public. Privacy and confidence are independent. High confidence does not authorize disclosure.

Context previews and packs list exclusion reasons and counts without embedding excluded bodies. Generated Markdown uses evidence IDs and summaries instead of raw private messages. The Agent Bridge should provide the smallest task-specific context rather than a full profile dump.

## Attribution and account guard

Every connector records source identity and speaker attribution. Broad collection requires a configured expected account or an explicit one-run authorization. A wrong or unverifiable account fails closed. Content written by colleagues, customers, quoted authors, or an AI must not silently become a Claim about the owner.

Timezone conversion is also an evidence boundary. A naïve timestamp is converted only when a trusted private metadata file, authoritative source fields, and an exact hash prove the timezone. Missing or ambiguous DST evidence is quarantined without record bodies and blocks production migration.

## Local service security

The dashboard binds to loopback by default, rejects non-loopback Host values, requires same-origin writes, uses strict JSON content types and bounded bodies, and sends `Cache-Control: no-store`. Mutations require request ID, idempotency key, expected version, actor, and reason. Content Security Policy must not allow inline script execution.

Files containing events, previews, receipts, contracts, or quarantine metadata are private, reject symlinks, and use atomic writes. Restore verifies the exact export generation before, during, and after copy, then replays event projections before atomically publishing a destination.

## Backup, migration, and deletion

Before migration, create a verified export on a different device or volume. The manifest binds each file hash, event heads, view watermarks, schema versions, and restore replay results. A backup stored only inside the source vault or on the same failure domain is not sufficient loss protection.

When historical memory text contains credential shapes, use backup-copy redaction instead of rewriting the authoritative `index.jsonl`. The export stores a deterministic redacted index and a hash-only `secret-redaction-receipt.json`, then scans the exported index again and runs strict restore verification. This mechanism is deliberately scoped to `index.jsonl`; it does not certify arbitrary binary files or unrelated vault paths as secret-free. Export source paths containing symlinks are rejected rather than followed.

v1.1 migration derives new files and preserves v1.0 sources. A hash-bound timezone contract may transform a verified legacy wall time only in the SQLite `ts_utc` projection, never in the raw JSONL or its `ts` value. Rollback does not require deleting new derived directories. Deletion requests must distinguish original evidence, derived models, indexes, backups, and published artifacts so one action does not falsely imply all copies are gone.

## Public release checklist

Run the privacy scan over exactly the Git-tracked release set:

```bash
git ls-files -z | xargs -0 python3 scripts/private_scan.py
git status --short
git diff --check
```

Then inspect the built source archive and wheel, install the wheel under an isolated `HOME`, run the CLI and privacy scan from a fresh clone, and verify the remote tag and release assets. A green unit test suite does not replace this review.
