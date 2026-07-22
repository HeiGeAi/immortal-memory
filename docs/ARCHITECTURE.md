# Architecture

## System contract

Immortal Memory v1.1 is a local-first, event-backed memory system. The repository contains code and empty templates. The private vault normally lives at `~/.immortal/`. Raw evidence is retained as evidence, while user-facing models are derived, reviewable, and rebuildable.

```text
Source connectors
  -> Raw vault and normalized index
  -> Claim events and current projection
  -> Living Self immutable versions
  -> Judgment events and current projection
  -> Context preview and compiled pack
  -> Outcome events
  -> future Claim and Judgment candidates
```

No derived projection outranks its event stream or source evidence. SQLite is a verified search index, not the source of truth.

## Seven product modules

The local dashboard uses seven stable, deep-linkable modules:

1. **Home** shows newly remembered value, model changes, pending confirmations, recent use, and only health issues that affect use.
2. **Memory** provides paginated evidence by time, person, project, and topic. Original records are immutable.
3. **Self** presents the current Living Self, evidence, confidence, conflicts, and version history.
4. **Judgment** manages candidate, confirmed, outcome-backed, failed, and retired Judgment cards.
5. **Use** previews, filters, compiles, delivers, consumes, and records outcomes for Context Packs.
6. **Trust** explains attribution, weak evidence, privacy exclusions, stale projections, and recent corrections.
7. **System** contains collection, index, backup, diagnostics, service, version, logs, and controlled maintenance.

The canonical entrypoint is `immortal-memory dashboard`. It starts a loopback HTTP service bound only to `127.0.0.1` and renders current `/api/v2` evidence. `immortal-memory agent-factory` is a compatibility alias during v1.1. `immortal-memory dashboard-export` creates only an explicit legacy static HTML snapshot; it is never health proof or the current product surface.

The Living Self inside the Self module has eight sections: identity and commitments, values, expression DNA, mental models, decision heuristics, anti-patterns, tensions, and honest boundaries. These eight sections must not be confused with the seven dashboard modules.

## Authoritative layers

### Claim

A Claim is a typed assertion with source evidence, subject attribution, confidence, role and domain scope, validity time, and one privacy label. Its append-only event stream is authoritative; `current.jsonl` is a replayable view. State transitions use expected revisions and idempotency keys.

Correction never edits an existing Claim in place. It appends correction events, supersedes the old Claim, creates the corrected Claim, and records the relationship. A failed downstream Living Self rebuild marks that projection stale rather than pretending the correction was absorbed.

### Living Self

Living Self deterministically aggregates effective, confirmed, non-private Claims. A confirmed version is immutable and has a content hash, parent version, Claim event watermark, and JSON plus redacted Markdown representation. Restoring history creates a new version pointing to the restored source version. It never rewinds or overwrites history.

### Judgment

A Judgment card records situation, constraints, decision, rationale, expected result, evidence, privacy, scope, and lifecycle. Candidate cards do not silently enter task context. Confirmed cards may be selected when scope, evidence, privacy, and task relevance permit it. Judgment outcome actions can update a card while preserving its history; a Context Outcome does not silently rewrite Judgment authority.

### Context

A Context Pack is a bounded, task-specific compilation. Preview bodies are private and time-limited. Compilation requires the exact preview ID, preview hash, source revisions, expected stream version, and allowed exclusions. The formal pack separates verified facts, confirmed self-model items, judgments, inferences, suggestions, and unknowns. Copying a pack does not mark it consumed; only an explicit consume event does.

### Outcome

Outcome events bind results to an exact Context Pack snapshot and source revisions. They record result summary, signal, evidence, and reason. v1.1 exposes failed outcomes as review signals, but does not auto-create or auto-promote Claim or Judgment candidates. It cannot mutate prior Claims, Judgments, or Context history.

## Event and concurrency model

Claim, Judgment, Context, and Outcome each use an append-only JSONL event stream and rebuildable current view. Every event carries stream version, expected version, request identity, idempotency identity, actor, timestamp, and schema version. Writes use private files, no-follow checks, advisory locks, atomic replacement, and fail-closed validation.

Cross-stream work is intentionally not presented as one transaction. Derived views persist watermarks. A partial failure is visible as stale or blocked and can be replayed safely.

## Search integrity

Recall reads a verified SQLite index. The validation receipt binds the source `index.jsonl`, SQLite database, WAL and SHM identities, schema version, metadata, and validation version. Source-to-database parity compares unique IDs in both directions. A stale or mismatched receipt makes recall unavailable instead of falling back to an unbounded JSONL scan.

The source and database share one source-to-database snapshot lock during validation and query. This contract depends on POSIX `flock` and trustworthy `st_ctime_ns`, so native support is macOS and Linux.

## Migration and rollback

v1.1 follows `audit -> external backup -> isolated restore -> stage -> migrate -> prewarm -> production gate -> switch`. The migration preserves v1.0 files and writes only new derived paths. `index.staging.jsonl` is an audit artifact, while the schema-v3 database is rebuilt from the immutable raw source; a verified timezone contract may resolve a legacy wall-time record only into derived `ts_utc`. A production switch is forbidden when source identity changes, index parity fails, a timestamp lacks trusted timezone evidence, the raw-source or staging-receipt binding fails, event replay fails, or the prewarm receipt is not fresh.

Rollback stops v1.1 services, reinstalls the retained v1.0 package and LaunchAgent configuration, ignores v1.1 derived directories, verifies original vault hashes, and reruns v1.0 health, preflight, restore, and Agent Context checks. Deleting v1.1 directories is not required.

## Encrypted Feishu disaster recovery

Feishu Drive is an optional, user-owned offsite recovery location. It is separate from the read-only Feishu ingestion mirror and has a deliberately narrow state sequence:

```text
local verified credential-redacted export
  -> GPG encrypted parts
  -> confirmed Feishu upload
  -> exact remote download
  -> decrypt and isolated restore
  -> private proof receipt
  -> migration gate
```

The package manifest contains only bounded metadata such as hashes, counts, timestamps, part names, and a public-key fingerprint suffix. Encrypted parts are uploaded before the manifest and are never overwritten. The process requires an existing public GPG key fingerprint to create a package and the matching private key only to run a real restore drill.

Drive synchronization, a successful upload, a local upload receipt, and a green dashboard card without a current download-and-restore drill are not disaster-recovery proof. The migration gate accepts `external_cloud` only when the private drill receipt validates, remote bytes were checked after download, decrypt-and-restore succeeded, the receipt is fresh, and its raw-index hash still matches the current vault.

GitHub release scanning and encrypted private recovery are separate pipelines. Public artifacts are built from a clean Git commit and scanned for private data. Feishu recovery packages are private, encrypted artifacts stored only in a user-owned Drive location and are never release inputs.

## Compatibility

For one release cycle, v1.1 retains v1.0 read paths, health and preflight commands, Agent Bridge behavior, and the legacy Control Center under System. Adapters remain thin. They call the core bridge and never own the data model.
