# Connectors

Connectors are source adapters. They should be optional and independently
testable.

## Recommended Connector Contract

Each connector should output normalized records with:

- `source`
- `source_id`
- `timestamp`
- `author`
- `account`
- `content`
- `content_type`
- `visibility`
- `dedup_key`
- `raw_path` or evidence pointer

## Scope Levels

1. Smoke: local demo record only.
2. Local: files and AI conversation traces.
3. Bounded enterprise: recent chats, meetings, docs with strict limits.
4. Full mirror: inventory first, then download queue, then coverage report.

## Failure Mode

Connectors should log unavailable scopes and continue. A missing enterprise
permission should not break local capture, profile refresh, or dashboard health.
