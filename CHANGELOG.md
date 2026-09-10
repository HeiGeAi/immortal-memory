# Changelog

## 1.3.4 - 2026-07-31

- Declare the bundled agent metadata and web assets as explicit distribution packages.
- Remove Setuptools package-discovery warnings while preserving the wheel contents.
- Add a regression contract for warning-free package configuration.

## 1.3.3 - 2026-07-28

- Let users bind confirmed and challenged memory references when recording a real Context outcome.
- Surface challenged memory references in the Trust ledger as review signals without automatically mutating Claim or Judgment authority.
- Show the confirmed and challenged reference counts on recorded Context outcomes.

## 1.3.2

- Make the top-level `agent-context` command support the complete preview and compile lifecycle instead of returning preview-only output.
- Stop treating the retired Markdown card cache as the authoritative judgment health signal or injecting it into agent context.
- Keep the advertised agent modes aligned with the modes accepted by the backend and reject unsupported modes before queueing work.
- Point generated agent guidance and adapters at the reviewed Context Store and Judgment Store lifecycle.
- Report the full pending-confirmation count on Home instead of presenting the eight-item display budget as the total.
- Add real review routes to Trust and collapse long evidence categories with native disclosure controls.
- Add an owner-only `learning-review` preview and explicitly confirmed Feishu reminder without duplicating the Claim or Judgment state machines.

## 1.3.1

- Move private identity aliases and categories out of public source code into local configuration, and make owner-only defaults work for every installation.
- Restore correct person extraction, relationship indexing, and quality scoring after sanitized public-code installation.
- Snapshot registered Claude Web and ChatGPT exports into the private vault so macOS background jobs can read them reliably.
- Support explicit GitHub `owner/repository` registration without scanning protected local directories.
- Show explicitly registered GitHub repositories as enabled and healthy in the control center even when no local checkout path is configured.
- Retry transient GitHub and Feishu network failures and persist bounded, secret-redacted error details.
- Treat restricted chats, missing meeting notes, deleted minute resources, and unavailable recordings as visible access-boundary skips instead of system failures.
- Prefer fresh Feishu collector state over stale orchestrator status in automatic feedback.

## 1.3.0

- Add explicit, disabled-by-default imports for Git commit history, GitHub pull requests and issues, Claude Web exports, ChatGPT exports, and Cursor transcripts.
- Keep external collection local, read-only, incremental, secret-redacted, and limited to user-registered paths.
- Surface each external source and Feishu Mail separately in the control center without exposing local paths.
- Include external-source deduplication state in portable recovery exports.
- Run enabled external sources through the normal orchestrator and report partial or failed collection honestly.
- Preserve every v1.1 Living Self, transaction, migration, recovery, and dashboard capability by releasing from the current `main` architecture.

## 1.1.1

- Mark a loaded daily LaunchAgent as requiring attention when its most recent exit code is nonzero, and expose the code as bounded scheduler evidence instead of showing a false healthy state.
- Preserve and display the distinct statuses of the main run, feedback report, and desktop notification so partial source failures or failed notifications cannot be presented as a fully healthy automation.

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
