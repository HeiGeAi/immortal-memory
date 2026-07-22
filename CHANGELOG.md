# Changelog

## 1.1.0

- Add versioned Claim, Living Self, Judgment, Context, and Outcome authorities.
- Replace the monitoring-first surface with seven evidence-backed product modules.
- Add strict index, migration, backup, restore, privacy, browser, and release gates.
- Rebuild the isolated schema-v3 index from the immutable raw source, resolving legacy Hermes wall time only into derived `ts_utc` through a hash-bound contract and separately binding the raw source and staging receipt at the production gate.
- Retire the unsafe live-runtime `package` workflow in favor of clean-commit release artifacts.
- Move the legacy personal Obsidian `project` workflow behind a separately audited local extension boundary.

## 0.9.0

- Add an evidence-driven local Control Center for runtime heartbeat, stages, outputs, risks, history, and safe operations.
- Persist structured orchestrator telemetry and bounded control-job history across service restarts.
- Keep unknown, stale, partial, and same-disk backup states distinct from healthy.
- Add allowlisted controls for a full run, health check, backup verification, and profile refresh.
- Refine the Control Center with a Nocturne Teal interface, accessible action states, reduced-motion support, and overflow-safe mobile and print layouts.
- Replace generic action buttons with instrument-switch controls that preserve their status lamp and command metadata through loading, success, and failure states.
- Preserve the legacy generated snapshot at `/snapshot`.

## 0.8.0

- Make SQLite synchronization transactional, concurrent-safe, and tolerant of incomplete JSONL tails.
- Make shared orchestrator state and derived profile outputs atomic.
- Recover safely from stale empty or malformed run locks.
- Remove URL userinfo before persistence and validate CORS origins by parsed loopback host.
- Fix Feishu mail extraction, pagination semantics, bounded-collection status, and interrupted mirror resumes.
- Terminate timed-out subprocess trees and preserve bounded context failure artifacts.
- Align recall date filters with the local calendar-day archive contract.
- Preserve the production-readiness preflight, backup, restore, and demo-vault safeguards.
- Add regression coverage for reliability, security, pagination, output integrity, and version governance.
