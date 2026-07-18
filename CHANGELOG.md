# Changelog

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
