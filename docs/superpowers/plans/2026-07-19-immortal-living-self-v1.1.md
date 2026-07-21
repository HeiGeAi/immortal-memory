# Immortal Memory v1.1 Living Self Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and release Immortal Memory v1.1.0 as an evidence-backed, correctable Living Self system with real judgment memory, task-scoped context, outcome feedback, and a seven-module production dashboard.

**Architecture:** Preserve the append-only v1.0 raw vault and CLI, then add rebuildable event-backed claims, Living Self, judgment, context, and outcome layers. Expose them through focused `/api/v2` services and a real local-first UI; keep operations under the System module and switch production only after dual-path migration and browser acceptance.

**Tech Stack:** Python 3.9–3.12 standard library, JSON/JSONL, SQLite FTS, `http.server`, no-build ES modules and CSS, pytest, Playwright browser acceptance, launchd on macOS, GitHub Actions.

---

## Working Rules

- Work only in `/Users/example/.config/superpowers/worktrees/immortal-memory/living-self-v1.1.0`.
- Keep `~/.immortal/` untouched until the explicitly gated production switch in Task 21.
- Follow red, green, refactor for every behavior.
- Run targeted tests before each commit.
- Run `PYTHONPATH=core python3 -m pytest -q` after every milestone.
- Do not merge tasks into one large commit.
- Never delete or rewrite `daily/*.jsonl[.gz]` or `index.jsonl`.
- New writes use a temporary test vault unless the production-switch step in Task 21 explicitly names the live vault.
- A task is incomplete until both spec review and code-quality review pass.
- Every helper used by a test, such as `clock`, `seeded_preview`, or `seeded_compiler`, must be defined in that test module or in `tests/v11_fixtures.py`; no pseudocode-only fixtures may remain in committed tests.

## File Map

### New core modules

| File | Single responsibility |
|---|---|
| `core/model_types.py` | Typed constructors, enums, validation, serialization |
| `core/event_store.py` | Locked append-only JSONL and deterministic replay |
| `core/index_integrity.py` | Source prefix fingerprint, ID reconciliation, staging rebuild |
| `core/evidence_catalog.py` | Stable L0 EvidenceRef resolution and broken-source state |
| `core/claim_store.py` | Claim transitions, current view, correction events |
| `core/model_migration.py` | Checkpointed, dry-run v1 profile-to-Claim migration |
| `core/attribution_service.py` | Speaker, subject, scope, privacy, confidence |
| `core/living_self_service.py` | Eight-section Living Self build, versions, diff, restore |
| `core/judgment_store.py` | Judgment cards and compatibility behavior |
| `core/context_store.py` | Context event lifecycle, preview TTL, current view |
| `core/context_compiler.py` | Preview, scope filtering, privacy, budget, compilation |
| `core/outcome_store.py` | Context use and outcome events |
| `core/product_data.py` | Product-facing bounded read models |
| `core/product_http.py` | `/api/v2` route parsing, request safety, error mapping |
| `core/product_ui.py` | Minimal HTML shell and static asset references |

### New static product assets

```text
core/product_assets/
  product.css
  api.js
  app.js
  router.js
  dialog.js
  views/
    home.js
    memories.js
    self.js
    judgments.js
    contexts.js
    trust.js
    system.js
```

### Existing files to modify

| File | Change |
|---|---|
| `core/profile_review.py` | Delegate `/api/v2` and root UI to new modules |
| `core/control_data.py` | Remain system-only read model |
| `core/control_center_ui.py` | Compatibility wrapper for `product_ui` |
| `core/task_compile.py` | Delegate new preview/compile path |
| `core/agent_bridge.py` | Emit v2 Context Pack and provenance |
| `core/immortal.py` | Register thin CLI commands and remove missing-script path |
| `core/orchestrator.py` | Add derived-layer rebuild stages |
| `core/export_restore.py` | Export and verify new rebuildable layers |
| `core/config.example.json` | Add Living Self and context defaults |
| `core/VERSION` | Bump only in Task 19 |
| `pyproject.toml` | Bump only in Task 19 |
| `README.md` | Document product and real workflows |
| `docs/ARCHITECTURE.md` | Document data layers and boundaries |
| `docs/PRODUCT.md` | Document seven user jobs |
| `docs/PRIVACY.md` | Document claim/context privacy behavior |
| `docs/ROADMAP.md` | Mark v1.1 delivered, retain future scope |

### New tests

| File | Coverage |
|---|---|
| `tests/test_model_types.py` | Schema and enum validation |
| `tests/v11_fixtures.py` | Deterministic clocks, temporary vaults, and reusable seeded stores |
| `tests/test_event_store.py` | locking, replay, crash safety |
| `tests/test_index_db_integrity.py` | middle rewrite detection, staging rebuild, ID parity |
| `tests/test_packaged_command_closure.py` | registered command source and wheel closure |
| `tests/test_orchestrator_release_contract.py` | required-stage failure exit behavior |
| `tests/test_backup_migration_gate.py` | external strict backup requirement |
| `tests/test_private_scan_release_artifacts.py` | source and archive privacy patterns |
| `tests/test_claim_store.py` | transitions and correction history |
| `tests/test_evidence_catalog.py` | stable EvidenceRef and source-broken behavior |
| `tests/test_model_migration.py` | idempotent legacy migration, checkpoint, dry-run |
| `tests/test_attribution_service.py` | speaker and subject isolation |
| `tests/test_living_self_service.py` | model thresholds, versions, diff, restore |
| `tests/test_judgment_store.py` | cards, outcomes, legacy CLI |
| `tests/test_context_compiler.py` | scope, privacy, budget, provenance |
| `tests/test_context_store.py` | preview TTL, revisions, consume and replay |
| `tests/test_outcome_store.py` | consume and outcome transitions |
| `tests/test_product_data.py` | bounded real read models |
| `tests/test_product_http_v2.py` | endpoint contracts and mutation safety |
| `tests/test_product_ui_v2.py` | seven modules and real actions |
| `tests/test_v11_migration.py` | v1.0 compatibility and rebuild |
| `tests/test_v11_packaging.py` | clean wheel and command completeness |
| `tests/regression_living_self_p0.py` | end-to-end P0 trust scenarios |
| `tests/browser_v11_acceptance.py` | desktop, mobile, persistence, console |

## Milestone 0: Repair the v1.0 Truth Gates

### Preflight Task A: Detect middle rewrites and rebuild the SQLite index safely

**Files:**
- Create: `core/index_integrity.py`
- Create: `tests/test_index_db_integrity.py`
- Modify: `core/index_db.py`
- Modify: `core/integrity_audit.py`
- Modify: `tests/test_index_db_reliability.py`

- [ ] **Step 1: Write failing middle-rewrite tests**

```python
import json
import sqlite3

from index_integrity import reconcile_index


def record(record_id: str, text: str) -> str:
    return json.dumps(
        {
            "id": record_id,
            "source": "test",
            "type": "conversation",
            "timestamp": "2026-07-19T00:00:00+00:00",
            "content": text,
        },
        ensure_ascii=False,
    )


def test_middle_insertion_triggers_staging_rebuild(tmp_path):
    source = tmp_path / "index.jsonl"
    db = tmp_path / "search_index.db"
    source.write_text(
        "\n".join([record("a", "A"), record("c", "C")]) + "\n",
        encoding="utf-8",
    )
    assert reconcile_index(source, db)["mode"] == "full_rebuild"

    source.write_text(
        "\n".join([record("a", "A"), record("b", "B"), record("c", "C")]) + "\n",
        encoding="utf-8",
    )
    result = reconcile_index(source, db)

    assert result["mode"] == "full_rebuild"
    assert result["jsonl_unique_ids"] == 3
    assert result["sqlite_ids"] == 3
    assert result["missing_in_sqlite"] == []
    assert result["missing_in_jsonl"] == []


def test_same_size_middle_rewrite_is_not_treated_as_append(tmp_path):
    source = tmp_path / "index.jsonl"
    db = tmp_path / "search_index.db"
    source.write_text(record("a", "AAAA") + "\n", encoding="utf-8")
    reconcile_index(source, db)
    source.write_text(record("b", "BBBB") + "\n", encoding="utf-8")

    result = reconcile_index(source, db)

    assert result["mode"] == "full_rebuild"
    with sqlite3.connect(db) as connection:
        assert connection.execute("select id from docs").fetchall() == [("b",)]


def test_failed_staging_rebuild_preserves_current_db(tmp_path, monkeypatch):
    source = tmp_path / "index.jsonl"
    db = tmp_path / "search_index.db"
    source.write_text(record("a", "A") + "\n", encoding="utf-8")
    reconcile_index(source, db)
    before = db.read_bytes()
    monkeypatch.setattr("index_integrity.verify_id_parity", lambda *_: (_ for _ in ()).throw(RuntimeError("parity failed")))
    source.write_text(record("b", "B") + "\n", encoding="utf-8")
    try:
        reconcile_index(source, db)
    except RuntimeError:
        pass
    assert db.read_bytes() == before
```

- [ ] **Step 2: Run and confirm the current sync logic fails**

```bash
PYTHONPATH=core python3 -m pytest tests/test_index_db_integrity.py -q
```

Expected: missing `index_integrity`, then current offset-only behavior cannot satisfy the tests.

- [ ] **Step 3: Implement prefix fingerprint and staging rebuild**

`index_integrity.py` must expose:

```python
def sha256_prefix(path: Path, length: int) -> str:
    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as handle:
        while remaining:
            block = handle.read(min(8 * 1024 * 1024, remaining))
            if not block:
                raise EOFError(f"{path} ended before {length} bytes")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def source_revision(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    return {
        "size": size,
        "mtime_ns": path.stat().st_mtime_ns,
        "prefix_sha256": sha256_prefix(path, size),
    }
```

`reconcile_index(source, db)` behavior:

1. read `last_size` and `prefix_sha256` from SQLite meta;
2. hash the first `last_size` source bytes;
3. if the stored prefix matches and source only appended, use incremental sync;
4. if size shrank, prefix differs, fingerprint is absent, or ID counts differ, build `<db>.staging`;
5. load every valid JSONL record into staging;
6. reject malformed lines and duplicate IDs with line numbers;
7. compare sorted JSONL unique IDs and SQLite IDs in both directions;
8. run `PRAGMA integrity_check` and FTS count checks;
9. fsync staging and atomically replace the live DB;
10. preserve the old DB until replacement succeeds.

`index_db.sync()` delegates to this logic and never silently scans JSONL as a query fallback.

- [ ] **Step 4: Run integrity and query regression**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_index_db_integrity.py \
  tests/test_index_db_reliability.py \
  tests/test_integrity_audit.py -q
```

Expected: all pass.

- [ ] **Step 5: Run a read-only live reconciliation report**

Do not replace the live DB yet. Add `--report-only --staging-path` support and run:

```bash
PYTHONPATH=core python3 core/index_integrity.py \
  --source /Users/example/.immortal/index.jsonl \
  --database /Users/example/.immortal/search_index.db \
  --report-only
```

Expected before repair: report 471,912 JSONL rows, 470,712 SQLite rows, and 1,200 IDs missing from SQLite.

- [ ] **Step 6: Commit**

```bash
git add \
  core/index_integrity.py \
  core/index_db.py \
  core/integrity_audit.py \
  tests/test_index_db_integrity.py \
  tests/test_index_db_reliability.py
git commit -m "fix: detect and rebuild incomplete search indexes"
```

### Preflight Task B: Close every public command and fail required orchestration

**Files:**
- Create: `tests/test_packaged_command_closure.py`
- Create: `tests/test_orchestrator_release_contract.py`
- Modify: `core/immortal.py`
- Modify: `core/orchestrator.py`
- Create public-safe implementation or remove all entry points for:
  - `core/cards.py`
  - `core/project.py`
  - `core/obsidian_notes_sync.py`

- [ ] **Step 1: Write a failing command-closure scanner**

```python
import ast
import re
from pathlib import Path


def registered_script_targets(root: Path) -> set[str]:
    targets = set()
    for path in (root / "core").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        targets.update(re.findall(r'run_script\(\s*["\\']([^"\\']+\\.py)["\\']', text))
        targets.update(re.findall(r'SKILL_DIR\\s*/\\s*["\\']([^"\\']+\\.py)["\\']', text))
    return targets


def test_every_registered_script_exists_in_source_tree():
    root = Path(__file__).resolve().parents[1]
    missing = sorted(
        target
        for target in registered_script_targets(root)
        if not (root / "core" / target).is_file()
    )
    assert missing == []
```

Extend the test to build a wheel, inspect its ZIP member list, install it into a temporary venv, and run each registered compatibility command with a safe `--help`, `stats`, or dry-run argument.

- [ ] **Step 2: Write a failing required-stage exit test**

```python
def test_required_stage_failure_returns_nonzero(tmp_path, monkeypatch):
    orchestrator = make_orchestrator(tmp_path)
    monkeypatch.setattr(orchestrator, "run_stage", lambda name, *_: {"name": name, "status": "failed"})
    result = orchestrator.run()
    assert result.exit_code != 0
    assert result.status == "failed"
    assert result.telemetry["status"] == "failed"


def test_full_job_summary_matches_the_commands_it_actually_runs(tmp_path):
    factory = FactoryStore(history_path=tmp_path / "jobs.json", skill_dir=CORE)
    commands = factory._commands_for("full", {"goal": "真实闭环"})
    stages = [command_stage(command) for command, _timeout in commands]
    assert stages == [
        "run",
        "claims-migrate",
        "profile-attribution-audit",
        "living-self-build",
        "cards-build",
        "context-preview",
    ]
    summary = factory._success_summary("full", {"goal": "真实闭环"})
    assert "采集、清洗、蒸馏、画像" not in summary
    assert "已按真实命令完成" in summary
```

- [ ] **Step 3: Run and confirm current failures**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_packaged_command_closure.py \
  tests/test_orchestrator_release_contract.py -q
```

Expected: missing `cards.py`, `project.py`, `obsidian_notes_sync.py`, and orchestrator attention incorrectly exits zero.

- [ ] **Step 4: Decide each missing command explicitly**

For each missing live-only script:

1. inspect the live file for private paths, identity, credentials, customer data, and undocumented dependencies;
2. if the capability is in public product scope, write a public-safe implementation with temporary-vault tests;
3. if it is not in public scope, remove the CLI command, orchestrator stage, help text, docs, and adapter reference together;
4. never copy a live file wholesale.

`cards` remains in scope and is replaced by the Task 8 Judgment Store compatibility command. `project` and `notes-sync` remain only if clean-package tests prove public-safe implementations.

- [ ] **Step 5: Make required stage failure authoritative**

Required stage failure must produce:

```python
return RunResult(
    status="failed",
    exit_code=1,
    attention=attention,
    failures=failures,
)
```

Optional connector limitations may remain `attention`, but a missing registered core script, failed index integrity, failed migration, failed context compile, or failed export verification is required and nonzero.

Correct the current `FactoryStore._commands_for("full")` and `_success_summary("full")` together. The UI may not claim capture, clean, distill, profile, or Context stages unless those exact commands ran and succeeded. After Context v2 lands, replace the last stage with explicit preview; never compile an unreviewed preview under a misleading `full` label.

- [ ] **Step 6: Run closure, orchestrator, and package tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_packaged_command_closure.py \
  tests/test_orchestrator_release_contract.py \
  tests/test_packaging.py \
  tests/test_orchestrator_telemetry.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add core tests/test_packaged_command_closure.py tests/test_orchestrator_release_contract.py
git commit -m "fix: close packaged commands and orchestration status"
```

### Preflight Task B2: Replace full-vault Notes reconciliation with a transaction journal

**Files:**
- Create: `core/notes_transactions.py`
- Create: `core/notes_migration.py`
- Create: `tests/test_notes_transaction_journal.py`
- Create: `tests/test_notes_migration_checkpoint.py`
- Modify: `core/notes_ingestion.py`
- Modify: `core/obsidian_notes_sync.py`
- Modify: `core/immortal.py`

- [ ] **Step 1: Write failing transaction-journal tests**

Cover:

- journal is fsynced before either fact append;
- crash before and after each append is recovered by exact offset and exact serialized bytes;
- an offset mismatch or different bytes fails closed without appending;
- the normal sync path never enumerates all daily files or scans the full index;
- append results distinguish `bytes_written=0` from partial or fsynced writes;
- `facts_committed` is false when the first append fails before writing;
- a concurrent file growth cannot exceed or under-report the remaining read budget;
- daily targets reject invalid dates and cannot escape `daily/`.

- [ ] **Step 2: Implement bounded manifest and pending transactions**

Use `notes/manifest.json` and `notes/transactions/<tx_id>.json`. Each prepared transaction records:

```json
{
  "tx_id": "stable transaction id",
  "record_id": "obsidian-note id",
  "daily_relpath": "daily/2026-07-19.jsonl",
  "daily_offset": 123,
  "index_offset": 456,
  "daily_length": 100,
  "index_length": 140,
  "daily_sha256": "hex",
  "index_sha256": "hex",
  "stage": "prepared"
}
```

All journal and manifest state uses atomic replace plus file and parent-directory fsync. Recovery checks only the declared offsets. It never searches the complete destination files.

- [ ] **Step 3: Write failing one-time migration tests**

Cover:

- identical physical duplicates compact to one record;
- same ID with different payload fails closed;
- daily-only and index-only legacy facts reconcile when the source changed or was deleted;
- the catalog is disk-backed and memory use is independent of total fact count;
- max files, max bytes, max seconds and checkpoint resume are authoritative;
- staging rewrites preserve all non-Notes bytes and publish only after hash/count verification;
- interrupted migration leaves production files unchanged and resumes from checkpoint.

- [ ] **Step 4: Implement explicit `notes-migrate`**

The migration is never implicit in `notes-sync`. It builds a temporary SQLite catalog, streams legacy Notes rows, validates strict dates and payload equality, writes staging projections, verifies them, then publishes under the source lock. A completed migration writes its version and evidence to the manifest.

- [ ] **Step 5: Make daily sync fail closed on legacy ambiguity**

If the vault has a non-empty fact layer but no compatible manifest, or an old `notes/state.json` exists without a completed migration marker, return:

```json
{
  "status": "error",
  "error_code": "notes_migration_required"
}
```

Do not scan the full vault, do not auto-migrate, and do not report success.
New-vault initialization creates an empty compatible manifest with `migration_status=not_required`.

- [ ] **Step 6: Verify clean package and bounded production behavior**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_notes_transaction_journal.py \
  tests/test_notes_migration_checkpoint.py \
  tests/test_notes_ingestion.py \
  tests/test_packaged_command_closure.py -q
PYTHONPATH=core python3 -m pytest -q
python3 tests/regression_p0.py
```

Also use an instrumented large temporary vault to prove the normal sync reads no unrelated daily content and no unrelated index bytes.

- [ ] **Step 7: Commit**

```bash
git add core tests docs/superpowers
git commit -m "fix: journal note ingestion transactions"
```

### Preflight Task C: Enforce backup migration and release privacy gates

**Files:**
- Create: `tests/test_backup_migration_gate.py`
- Create: `tests/test_private_scan_release_artifacts.py`
- Modify: `core/export_restore.py`
- Modify: `core/immortal.py`
- Modify: `scripts/private_scan.py`

- [ ] **Step 1: Write failing backup gate tests**

```python
from export_restore import migration_backup_gate


def test_internal_or_manifest_only_backup_blocks_migration(tmp_path):
    manifest = {
        "storage": {"location": "internal_vault"},
        "verification": {"mode": "manifest-only", "ok": True},
        "warnings": ["secret_shapes_present"],
    }
    result = migration_backup_gate(manifest, require_external=True)
    assert result["ok"] is False
    assert set(result["blockers"]) == {
        "backup_not_external",
        "verification_not_strict",
        "secret_shapes_present",
    }


def test_external_strict_restorable_backup_passes(tmp_path):
    manifest = {
        "storage": {"location": "external_disk"},
        "verification": {"mode": "strict-sha256", "ok": True},
        "restore_check": {"ok": True},
        "warnings": [],
    }
    assert migration_backup_gate(manifest, require_external=True)["ok"] is True
```

- [ ] **Step 2: Write failing source and archive scan tests**

```python
@pytest.mark.parametrize(
    "secret",
    [
        "ou_" + "00000000000000000000000000000000",
        "Cook" + "ie: session=abcdefghijklmnopqrstuvwxyz",
        "Authorization: Bea" + "rer abcdefghijklmnopqrstuvwxyz",
        "https://user:pass" + "word@example.com/path",
        "AKIA" + "IOSFODNN7EXAMPLE",
        "/Users/" + "private-owner/.immortal/index.jsonl",
    ],
)
def test_private_scan_detects_release_secrets(tmp_path, secret):
    artifact = tmp_path / "artifact.zip"
    write_zip(artifact, {"payload.txt": secret})
    result = scan_paths([artifact])
    assert result["hits"]
```

- [ ] **Step 3: Confirm current false greens**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_backup_migration_gate.py \
  tests/test_private_scan_release_artifacts.py -q
```

Expected: current backup summary and scanner fail these assertions.

- [ ] **Step 4: Implement fail-closed gates**

`backup-status` preserves manifest storage classification, verification mode, generation warnings, secret candidate counts, and restore evidence. Add:

```text
immortal-memory migration-preflight --require-external-backup --json
```

It exits nonzero for internal vault, same disk, manifest-only, missing strict restore, credential candidates, stale backup, failed health, or index parity failure.

Enhance `private_scan.py` to recurse into `.whl`, `.zip`, `.tar.gz`, and `.tar` without extracting outside a temporary directory. Add patterns for open_id, Cookie, Bearer, AWS access keys, URL userinfo, absolute macOS/Linux/Windows home paths, and private key blocks. Report archive member names, never secret values.

- [ ] **Step 5: Run security and backup tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_backup_migration_gate.py \
  tests/test_private_scan_release_artifacts.py \
  tests/test_secret_scan.py \
  tests/test_recursive_redaction.py \
  tests/test_export_restore.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add \
  core/export_restore.py \
  core/immortal.py \
  scripts/private_scan.py \
  tests/test_backup_migration_gate.py \
  tests/test_private_scan_release_artifacts.py
git commit -m "fix: enforce backup and release privacy gates"
```

- [ ] **Step 7: Run Milestone 0 full baseline**

```bash
PYTHONPATH=core python3 -m pytest -q
python3 tests/regression_p0.py
```

Expected: full suite passes, P0 remains `8/8`, and no product-layer code has started.

## Milestone A: Trusted Data Kernel

### Task 1: Core model contracts

**Files:**
- Create: `core/model_types.py`
- Create: `tests/test_model_types.py`

- [ ] **Step 1: Write failing contract tests**

```python
from model_types import (
    ModelValidationError,
    new_claim,
    new_event,
    new_evidence_ref,
    new_judgment_card,
)


def test_claim_requires_evidence_unless_user_declared():
    try:
        new_claim(statement="偏好短段落", source_kind="direct", evidence_ids=[])
    except ModelValidationError as exc:
        assert exc.code == "evidence_required"
    else:
        raise AssertionError("claim without evidence was accepted")


def test_inferred_claim_cannot_start_confirmed():
    try:
        new_claim(
            statement="遇到风险时先隔离",
            source_kind="inferred",
            evidence_ids=["ev-1"],
            status="confirmed",
        )
    except ModelValidationError as exc:
        assert exc.code == "inferred_claim_requires_review"
    else:
        raise AssertionError("inferred claim started confirmed")


def test_judgment_card_defaults_to_unknown_outcome():
    card = new_judgment_card(
        title="先做只读审计",
        situation="生产升级",
        decision="先审计再变更",
        evidence_ids=["ev-1"],
    )
    assert card["status"] == "candidate"
    assert card["outcome"]["status"] == "unknown"


def test_event_envelope_separates_schema_and_stream_version():
    event = new_event(
        event_type="claim.created",
        stream_id="clm-1",
        stream_version=1,
        expected_version=0,
        request_id="req-1",
        idempotency_key="idem-1",
        actor={"kind": "owner", "id": "owner"},
        payload={"statement": "偏好短段落"},
    )
    assert event["schema_version"] == 1
    assert event["stream_version"] == 1
    assert event["expected_version"] == 0


def test_evidence_ref_never_uses_sqlite_rowid():
    ref = new_evidence_ref(
        evidence_id="ev-raw-1",
        source="codex",
        raw_id="raw-1",
        content_hash="sha256:" + "a" * 64,
        status="available",
        privacy="restricted",
    )
    assert ref["evidence_id"] == "ev-raw-1"
    assert "rowid" not in ref
```

- [ ] **Step 2: Run the tests and confirm red**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_model_types.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'model_types'`.

- [ ] **Step 3: Implement typed constructors and validation**

Create immutable constants and dictionary constructors:

```python
import uuid
from datetime import datetime, timezone
from typing import Optional


CLAIM_STATUSES = {"candidate", "confirmed", "rejected", "superseded"}
SOURCE_KINDS = {"direct", "quoted", "observed", "inferred", "user_declared"}
PRIVACY_LEVELS = {"private", "restricted", "context_safe", "public"}


class ModelValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def new_claim(
    *,
    statement: str,
    source_kind: str,
    evidence_ids: list[str],
    status: str = "candidate",
    claim_type: str = "fact",
    speaker_kind: str = "owner",
    subject_kind: str = "owner",
    confidence: float = 0.0,
    role_scope: Optional[list[str]] = None,
    domain_scope: Optional[list[str]] = None,
    privacy: str = "restricted",
    now: Optional[str] = None,
) -> dict:
    if not statement.strip():
        raise ModelValidationError("statement_required", "statement must not be empty")
    if source_kind not in SOURCE_KINDS:
        raise ModelValidationError("invalid_source_kind", source_kind)
    if not evidence_ids and source_kind != "user_declared":
        raise ModelValidationError("evidence_required", "claim requires evidence")
    if source_kind == "inferred" and status == "confirmed":
        raise ModelValidationError(
            "inferred_claim_requires_review",
            "inferred claim cannot start confirmed",
        )
    if not 0.0 <= confidence <= 1.0:
        raise ModelValidationError("invalid_confidence", str(confidence))
    generated = now or datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "revision": 1,
        "claim_id": "clm_" + uuid.uuid4().hex,
        "subject": {"kind": subject_kind, "id": "owner" if subject_kind == "owner" else ""},
        "speaker": {"kind": speaker_kind, "id": "owner" if speaker_kind == "owner" else ""},
        "claim_type": claim_type,
        "statement": statement.strip(),
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "counter_evidence_ids": [],
        "source_kind": source_kind,
        "confidence": confidence,
        "confidence_basis": {
            "speaker": 0.0,
            "recurrence": 0.0,
            "source_quality": 0.0,
            "explanation": "",
        },
        "role_scope": role_scope or ["general"],
        "domain_scope": domain_scope or ["general"],
        "custom_scope_ids": [],
        "privacy": privacy,
        "valid_from": None,
        "valid_to": None,
        "status": status,
        "created_at": generated,
        "updated_at": generated,
        "based_on_event_seq": 0,
    }
```

Also implement:

- `new_event()` with the full event envelope;
- `new_evidence_ref()`;
- typed refs `{"kind", "id", "revision"}`;
- Living Self version containers;
- `new_judgment_card()`;
- `new_context_pack()` with lifecycle and availability status;
- `new_outcome_event()` with `confirmed_refs` and `challenged_refs`;
- `validate_*()` using the exact fields in the design spec.

Use `typing.Optional` rather than `X | None` where imported modules must support Python 3.9.

- [ ] **Step 4: Run targeted tests**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_model_types.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add core/model_types.py tests/test_model_types.py
git commit -m "feat: define Living Self model contracts"
```

### Task 2: Crash-safe append-only event store

**Files:**
- Create: `core/event_store.py`
- Create: `tests/test_event_store.py`
- Reuse: `core/state_store.py`
- Reuse: `core/file_utils.py`

- [ ] **Step 1: Write failing locking and replay tests**

```python
import json
import threading

from event_store import EventConflict, EventCorruption, JsonlEventStore
from model_types import new_event


def event(event_id, key, payload, expected_version):
    return new_event(
        event_id=event_id,
        event_type="test.changed",
        stream_id="stream-1",
        stream_version=expected_version + 1,
        expected_version=expected_version,
        request_id=event_id,
        idempotency_key=key,
        actor={"kind": "system", "id": "test"},
        payload=payload,
    )


def run_two_concurrent_appends(store, first, second):
    results = []

    def append(value):
        try:
            store.append(value)
            results.append(type("Result", (), {"code": "ok"})())
        except EventConflict as exc:
            results.append(type("Result", (), {"code": exc.code})())

    threads = [threading.Thread(target=append, args=(value,)) for value in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return results


def test_concurrent_appends_preserve_every_event(tmp_path):
    store = JsonlEventStore(tmp_path / "events.jsonl")
    errors = []

    def write(number):
        try:
            store.append({"event_id": f"evt-{number}", "sequence": number})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(number,)) for number in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert {row["event_id"] for row in store.read_all()} == {
        f"evt-{number}" for number in range(20)
    }


def test_replay_rejects_duplicate_event_id(tmp_path):
    path = tmp_path / "events.jsonl"
    row = {"event_id": "evt-1", "kind": "created"}
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    store = JsonlEventStore(path)
    try:
        store.read_all()
    except ValueError as exc:
        assert "duplicate event_id" in str(exc)
    else:
        raise AssertionError("duplicate event was accepted")


def test_same_idempotency_key_with_different_payload_conflicts(tmp_path):
    store = JsonlEventStore(tmp_path / "events.jsonl")
    store.append(event("evt-1", "idem-1", {"value": 1}, expected_version=0))
    try:
        store.append(event("evt-2", "idem-1", {"value": 2}, expected_version=1))
    except EventConflict as exc:
        assert exc.code == "idempotency_conflict"
    else:
        raise AssertionError("conflicting retry was accepted")


def test_expected_version_allows_only_one_concurrent_transition(tmp_path):
    store = JsonlEventStore(tmp_path / "events.jsonl")
    store.append(event("evt-create", "create", {"value": 0}, expected_version=0))
    results = run_two_concurrent_appends(
        store,
        event("evt-a", "idem-a", {"value": 1}, expected_version=1),
        event("evt-b", "idem-b", {"value": 2}, expected_version=1),
    )
    assert sorted(result.code for result in results) == ["ok", "version_conflict"]


def test_partial_tail_is_reported_not_skipped(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event_id":"evt-1"}\n{"event_id":', encoding="utf-8")
    try:
        JsonlEventStore(path).read_all()
    except EventCorruption as exc:
        assert exc.line_number == 2
    else:
        raise AssertionError("partial tail was skipped")
```

- [ ] **Step 2: Confirm red**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_event_store.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement locked append and deterministic replay**

Expose a public cross-platform lock context manager from `state_store.py`; do not introduce `fcntl`:

```python
@contextmanager
def exclusive_lock(
    lock_path: Path,
    *,
    timeout: float = 5.0,
    stale_after: float = 60.0,
):
    fd = _acquire_lock(lock_path, timeout, stale_after)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
```

```python
class JsonlEventStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("event_id is required")
        with exclusive_lock(self.lock_path):
            existing = self.read_all()
            same_key = [
                row for row in existing
                if row.get("idempotency_key") == event.get("idempotency_key")
            ]
            if same_key:
                if canonical_payload(same_key[0]) == canonical_payload(event):
                    return same_key[0]
                raise EventConflict("idempotency_conflict", "key reused with different payload")
            stream = [
                row for row in existing
                if row.get("stream_id") == event.get("stream_id")
            ]
            current_version = max(
                [int(row.get("stream_version") or 0) for row in stream],
                default=0,
            )
            if int(event.get("expected_version") or 0) != current_version:
                raise EventConflict("version_conflict", "stream version changed")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        seen = set()
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            event_id = str(row.get("event_id") or "")
            if not event_id:
                raise ValueError(f"missing event_id at line {number}")
            if event_id in seen:
                raise ValueError(f"duplicate event_id: {event_id}")
            seen.add(event_id)
            rows.append(row)
        return rows
```

`read_all()` rejects malformed nonempty lines, partial tails, missing envelope fields, duplicate event IDs, and non-monotonic stream versions. Add `rebuild_view(projector)` and a crash test showing an event committed before current-view replacement is repaired on replay.

- [ ] **Step 4: Run event and state tests**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_event_store.py tests/test_state_store.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/event_store.py core/state_store.py tests/test_event_store.py tests/test_state_store.py
git commit -m "feat: add crash-safe model event store"
```

### Task 3: Claim store and correction state machine

**Files:**
- Create: `core/claim_store.py`
- Create: `tests/test_claim_store.py`

- [ ] **Step 1: Write failing state-transition tests**

```python
from claim_store import ClaimStore, InvalidTransition


def claim():
    return {
        "claim_id": "clm-1",
        "statement": "先给结论",
        "status": "candidate",
        "source_kind": "direct",
        "evidence_ids": ["ev-1"],
        "privacy": "context_safe",
        "role_scope": ["work"],
        "domain_scope": ["communication"],
        "created_at": "2026-07-19T00:00:00+00:00",
        "updated_at": "2026-07-19T00:00:00+00:00",
        "model_version": 1,
    }


def test_correct_supersedes_original_and_preserves_history(tmp_path):
    store = ClaimStore(tmp_path)
    store.create(claim())
    store.transition(
        "clm-1",
        "confirmed",
        reason="owner confirmed",
        expected_revision=1,
        request_id="req-confirm",
        idempotency_key="idem-confirm",
    )
    corrected = store.correct(
        "clm-1",
        "先给结果，再给必要依据",
        reason="more precise",
        expected_revision=2,
        request_id="req-correct",
        idempotency_key="idem-correct",
    )

    assert store.get("clm-1")["status"] == "superseded"
    assert corrected["status"] == "confirmed"
    assert corrected["supersedes"] == "clm-1"
    assert len(store.events.read_all()) == 4


def test_rejected_claim_requires_new_evidence_to_reconsider(tmp_path):
    store = ClaimStore(tmp_path)
    store.create(claim())
    store.transition(
        "clm-1",
        "rejected",
        reason="wrong speaker",
        expected_revision=1,
        request_id="req-reject",
        idempotency_key="idem-reject",
    )
    try:
        store.reconsider(
            "clm-1",
            evidence_ids=[],
            expected_revision=2,
            request_id="req-reconsider",
            idempotency_key="idem-reconsider",
        )
    except InvalidTransition as exc:
        assert exc.code == "new_evidence_required"
    else:
        raise AssertionError("rejected claim reconsidered without evidence")


def test_concurrent_expected_revision_allows_one_correction(tmp_path):
    store = ClaimStore(tmp_path)
    store.create(claim())
    store.transition(
        "clm-1",
        "confirmed",
        reason="owner",
        expected_revision=1,
        request_id="req-owner",
        idempotency_key="idem-owner",
    )
    results = run_concurrent_corrections(store, claim_id="clm-1", expected_revision=2)
    assert sorted(result.code for result in results) == ["ok", "version_conflict"]


def test_replay_repairs_current_view_after_crash(tmp_path):
    store = ClaimStore(tmp_path)
    store.create(claim())
    store.current_path.unlink()
    repaired = ClaimStore(tmp_path)
    assert repaired.get("clm-1")["statement"] == "先给结论"
    assert repaired.get("clm-1")["based_on_event_seq"] == 1
```

- [ ] **Step 2: Confirm red**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_claim_store.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement the explicit transition map**

```python
ALLOWED_TRANSITIONS = {
    "candidate": {"confirmed", "rejected"},
    "confirmed": {"superseded"},
    "rejected": {"candidate"},
    "superseded": set(),
}


class InvalidTransition(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ClaimStore:
    def __init__(self, vault_dir: Path) -> None:
        root = Path(vault_dir) / "model" / "claims"
        self.events = JsonlEventStore(root / "events.jsonl")
        self.current_path = root / "current.jsonl"

    def _replay(self) -> dict[str, dict]:
        current = {}
        for event in self.events.read_all():
            if event["kind"] in {"claim_created", "claim_transitioned", "claim_corrected"}:
                current[event["claim"]["claim_id"]] = dict(event["claim"])
        return current

    def transition(
        self,
        claim_id: str,
        status: str,
        *,
        reason: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
    ) -> dict:
        claim = self.get(claim_id)
        if int(claim["revision"]) != expected_revision:
            raise InvalidTransition("version_conflict", "claim revision changed")
        if status not in ALLOWED_TRANSITIONS[claim["status"]]:
            raise InvalidTransition(
                "invalid_transition",
                f"{claim['status']} cannot transition to {status}",
            )
        updated = {
            **claim,
            "status": status,
            "revision": expected_revision + 1,
            "updated_at": utc_now(),
        }
        self._append(
            "claim.transitioned",
            updated,
            reason=reason,
            expected_version=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        return updated
```

Implement `create`, `get`, `list`, `transition`, `correct`, `reconsider`, `_append`, and `_write_current`. Every public write requires expected revision, request ID, idempotency key, actor, and reason. Write the current view with `atomic_write_text`; sort by `claim_id` for deterministic rebuilds. On initialization, compare `based_on_event_seq` to the event head and replay when stale or missing.

- [ ] **Step 4: Run claim tests**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_claim_store.py tests/test_event_store.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/claim_store.py tests/test_claim_store.py
git commit -m "feat: add correctable claim state machine"
```

### Task 3A: Stable EvidenceRef catalog

**Files:**
- Create: `core/evidence_catalog.py`
- Create: `tests/test_evidence_catalog.py`

- [ ] **Step 1: Write failing evidence-resolution tests**

```python
from evidence_catalog import EvidenceCatalog


def test_existing_fact_record_resolves_without_sqlite_rowid(tmp_path):
    index = tmp_path / "index.jsonl"
    index.write_text(
        '{"id":"raw-1","source":"codex","timestamp":"2026-07-19T00:00:00Z","content":"事实"}\n',
        encoding="utf-8",
    )
    catalog = EvidenceCatalog(index)
    ref = catalog.resolve("raw-1")
    assert ref["evidence_id"] == "raw-1"
    assert ref["status"] == "available"
    assert ref["content_hash"].startswith("sha256:")
    assert "rowid" not in ref


def test_missing_legacy_raw_id_stays_source_broken(tmp_path):
    catalog = EvidenceCatalog(tmp_path / "missing-index.jsonl")
    ref = catalog.from_legacy(
        source="feishu",
        raw_id="missing",
        timestamp="2026-07-01T00:00:00Z",
        statement="历史候选",
    )
    assert ref["status"] == "source_broken"
    assert ref["evidence_id"].startswith("ev_legacy_")


def test_duplicate_logical_content_keeps_distinct_fact_ids(tmp_path):
    index = tmp_path / "index.jsonl"
    index.write_text(
        "\n".join(
            [
                '{"id":"raw-1","source":"codex","timestamp":"2026-07-18T00:00:00Z","content":"同一句"}',
                '{"id":"raw-2","source":"claude","timestamp":"2026-07-19T00:00:00Z","content":"同一句"}',
            ]
        ) + "\n",
        encoding="utf-8",
    )
    catalog = EvidenceCatalog(index)
    assert catalog.resolve("raw-1")["evidence_id"] != catalog.resolve("raw-2")["evidence_id"]
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_evidence_catalog.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement the bounded fact catalog**

Build an ID-to-safe-metadata catalog from the verified SQLite index and confirm each requested ID against JSONL during migration. Store:

```json
{
  "evidence_id": "raw-1",
  "source": "codex",
  "raw_id": "raw-1",
  "content_hash": "sha256:...",
  "status": "available",
  "observed_at": "...",
  "privacy": "restricted"
}
```

Do not store raw bodies in the catalog. When the source is missing, return a deterministic `source_broken` reference and prevent automatic confirmation.

- [ ] **Step 4: Run evidence and index tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_evidence_catalog.py \
  tests/test_index_db_integrity.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/evidence_catalog.py tests/test_evidence_catalog.py
git commit -m "feat: add stable raw evidence references"
```

### Task 4: Idempotent legacy profile migration

**Files:**
- Create: `core/model_migration.py`
- Create: `tests/test_model_migration.py`
- Modify: `core/immortal.py` at `build_parser()` and `main()`

- [ ] **Step 1: Write failing migration tests**

```python
import json

from model_migration import migrate_legacy_profile


def test_migration_is_idempotent_and_keeps_legacy_file(tmp_path):
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    legacy = reviewed / "profile_memories.jsonl"
    legacy.write_text(
        json.dumps(
            {
                "memory_id": "mem-1",
                "statement": "偏好短段落",
                "focus": "self_profile",
                "evidence_ids": ["ev-1"],
                "speaker": "owner",
                "sensitivity": "internal",
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    first = migrate_legacy_profile(tmp_path)
    second = migrate_legacy_profile(tmp_path)

    assert first["created"] == 1
    assert second["created"] == 0
    assert legacy.is_file()
    assert len((tmp_path / "model/claims/events.jsonl").read_text().splitlines()) == 1


def test_other_speaker_stays_candidate_external_view(tmp_path):
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    (reviewed / "profile_memories.jsonl").write_text(
        json.dumps(
            {
                "memory_id": "mem-2",
                "statement": "他做事很激进",
                "focus": "self_profile",
                "evidence_ids": ["ev-2"],
                "speaker": "other",
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    migrate_legacy_profile(tmp_path)
    row = json.loads((tmp_path / "model/claims/current.jsonl").read_text())
    assert row["source_kind"] == "quoted"
    assert row["status"] == "candidate"
    assert row["claim_type"] == "external_view"


def test_legacy_nuwa_accepted_never_becomes_confirmed(tmp_path):
    (tmp_path / "profile_nuwa.json").write_text(
        json.dumps({"mental_models": [{"id": "legacy-1", "status": "accepted"}]}),
        encoding="utf-8",
    )
    report = migrate_legacy_profile(tmp_path)
    assert report["confirmed"] == 0
    assert all(row["status"] == "candidate" for row in load_current_claims(tmp_path))


def test_dry_run_and_checkpoint_do_not_duplicate_events(tmp_path):
    seed_legacy_profile(tmp_path, count=3)
    dry = migrate_legacy_profile(tmp_path, dry_run=True)
    assert dry["created"] == 3
    assert not (tmp_path / "model/claims/events.jsonl").exists()
    first = migrate_legacy_profile(tmp_path, checkpoint_every=1)
    second = migrate_legacy_profile(tmp_path, checkpoint_every=1)
    assert first["created"] == 3
    assert second["created"] == 0
```

- [ ] **Step 2: Confirm red**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_model_migration.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement deterministic migration IDs**

```python
def legacy_claim_id(memory_id: str) -> str:
    digest = hashlib.sha256(f"legacy-profile:{memory_id}".encode("utf-8")).hexdigest()
    return "clm_" + digest[:32]


def migrate_legacy_profile(
    vault_dir: Path,
    *,
    dry_run: bool = False,
    checkpoint_every: int = 100,
) -> dict:
    vault = Path(vault_dir)
    source = vault / "reviewed" / "profile_memories.jsonl"
    store = ClaimStore(vault)
    existing = {row["claim_id"] for row in store.list()}
    created = 0
    skipped = 0
    for row in load_jsonl(source):
        claim_id = legacy_claim_id(str(row.get("memory_id") or ""))
        if claim_id in existing:
            skipped += 1
            continue
        speaker = normalize_speaker(row)
        evidence_refs = EvidenceCatalog(vault / "index.jsonl").resolve_legacy_row(row)
        claim = from_legacy_row(
            row,
            claim_id=claim_id,
            speaker=speaker,
            evidence_refs=evidence_refs,
            status="candidate",
        )
        if not dry_run:
            store.create(claim, event_id="evt_migrate_" + claim_id[4:])
        created += 1
    return {
        "created": created,
        "skipped": skipped,
        "confirmed": 0,
        "source_broken": source_broken,
        "source": str(source),
    }
```

Register:

```text
immortal-memory claims-migrate [--vault-dir PATH] [--json]
```

The CLI prints counts and exits nonzero on malformed current state. It must not delete or modify the legacy source.

- [ ] **Step 4: Run migration and CLI tests**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_model_migration.py tests/test_version.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/model_migration.py core/immortal.py tests/test_model_migration.py
git commit -m "feat: migrate reviewed memory into claims"
```

### Task 5: Attribution and trust report

**Files:**
- Create: `core/attribution_service.py`
- Create: `tests/test_attribution_service.py`
- Modify: `core/profile_attribution_audit.py`

- [ ] **Step 1: Write failing contamination tests**

```python
from attribution_service import AttributionService


def test_other_speaker_cannot_become_owner_direct_fact():
    service = AttributionService(owner_aliases={"owner", "小黑" + "子"})
    result = service.classify(
        {
            "role": "assistant",
            "author": "同事A",
            "content": "你做事太激进了",
            "source": "feishu",
        }
    )
    assert result["speaker"]["kind"] == "other"
    assert result["source_kind"] == "quoted"
    assert result["auto_confirm_allowed"] is False
    assert "other_speaker" in result["trust_flags"]


def test_owner_instruction_is_not_long_term_preference_without_recurrence():
    service = AttributionService(owner_aliases={"owner"})
    result = service.classify(
        {
            "role": "user",
            "author": "owner",
            "content": "这一次先写详细一点",
            "source": "codex",
        }
    )
    assert result["claim_type"] == "request"
    assert result["confidence"] < 0.7
    assert result["auto_confirm_allowed"] is False
```

- [ ] **Step 2: Confirm red**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_attribution_service.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement deterministic attribution rules**

Implement:

```python
class AttributionService:
    def __init__(self, owner_aliases: set[str]) -> None:
        self.owner_aliases = {value.casefold() for value in owner_aliases if value}

    def classify(self, record: dict[str, Any]) -> dict[str, Any]:
        speaker = self._speaker(record)
        claim_type = self._claim_type(record)
        quoted = speaker["kind"] != "owner"
        flags = []
        if quoted:
            flags.append("other_speaker")
        if claim_type in {"request", "emotion"}:
            flags.append("transient_content")
        confidence = self._confidence(record, speaker, claim_type)
        return {
            "speaker": speaker,
            "subject": self._subject(record, speaker),
            "claim_type": "external_view" if quoted else claim_type,
            "source_kind": "quoted" if quoted else "direct",
            "role_scope": self._role_scope(record),
            "domain_scope": self._domain_scope(record),
            "privacy": self._privacy(record),
            "confidence": confidence,
            "auto_confirm_allowed": (
                speaker["kind"] == "owner"
                and claim_type not in {"request", "emotion"}
                and confidence >= 0.85
            ),
            "trust_flags": flags,
        }
```

Generate `~/.immortal/model/attribution/latest-report.json` with bounded samples and counts. Do not include raw confidential bodies.

- [ ] **Step 4: Run attribution and legacy audit tests**

Run:

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_attribution_service.py \
  tests/test_attribution_circuit.py \
  tests/test_profile_review_privacy.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/attribution_service.py core/profile_attribution_audit.py tests/test_attribution_service.py
git commit -m "feat: enforce owner attribution boundaries"
```

- [ ] **Step 6: Run Milestone A full suite**

```bash
PYTHONPATH=core python3 -m pytest -q
python3 tests/regression_p0.py
```

Expected: existing suite plus new tests pass, P0 remains `8/8`.

## Milestone B: Living Self and Judgment

### Task 6: Living Self build thresholds and eight-section model

**Files:**
- Create: `core/living_self_service.py`
- Create: `tests/test_living_self_service.py`
- Modify: `core/profile_nuwa.py`

- [ ] **Step 1: Write failing model-threshold tests**

```python
from living_self_service import LivingSelfService


def test_single_transient_claim_does_not_become_mental_model(tmp_path):
    service = LivingSelfService(tmp_path)
    service.claims.create(
        claim(
            "这一次先快速做",
            claim_type="request",
            evidence_ids=["ev-1"],
            status="confirmed",
            domain_scope=["project"],
        )
    )
    model = service.build_candidate()
    assert model["mental_models"] == []


def test_cross_time_cross_domain_claims_form_candidate_model(tmp_path):
    service = LivingSelfService(tmp_path)
    service.claims.create(claim("先独立预判再问 AI", "ev-1", "business", "2026-03-01"))
    service.claims.create(claim("代码审查先形成自己的判断", "ev-2", "technical", "2026-06-01"))
    model = service.build_candidate()
    item = model["mental_models"][0]
    assert item["validation"]["cross_domain_recurrence"] == 2
    assert item["status"] == "candidate"
    assert item["evidence_ids"] == ["ev-1", "ev-2"]


def test_conflicting_confirmed_claims_become_tension(tmp_path):
    service = LivingSelfService(tmp_path)
    service.claims.create(claim("先快速验证", "ev-1", "project", "2026-03-01"))
    service.claims.create(claim("高风险变更先完整审查", "ev-2", "risk", "2026-04-01"))
    model = service.build_candidate()
    assert model["tensions"][0]["poles"] == ["speed", "assurance"]
```

- [ ] **Step 2: Confirm red**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_living_self_service.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement rule-backed model builders**

Implement `LivingSelfService` with eight explicit builders:

```python
SECTION_BUILDERS = {
    "identity_commitments": build_identity_commitments,
    "values": build_values,
    "expression_dna": build_expression_dna,
    "mental_models": build_mental_models,
    "decision_heuristics": build_decision_heuristics,
    "anti_patterns": build_anti_patterns,
    "tensions": build_tensions,
    "honest_boundaries": build_honest_boundaries,
}


class LivingSelfService:
    def build_candidate(self) -> dict[str, Any]:
        claims = [row for row in self.claims.list() if row["status"] == "confirmed"]
        sections = {
            name: builder(claims)
            for name, builder in SECTION_BUILDERS.items()
        }
        return {
            "schema_version": 1,
            "version_id": version_id(sections),
            "status": "candidate",
            "generated_at": utc_now(),
            **sections,
        }
```

Each model item includes `claim_ids`, `evidence_ids`, `counter_evidence_ids`, confidence, role/domain scope, validation, application, failure conditions, status, time bounds, and model version. Initial builders must be deterministic and source-backed; do not call an LLM.

Change `profile_nuwa.py` into a compatibility exporter that reads `living-self/current.json` when available and clearly labels legacy rule output when it is not.

- [ ] **Step 4: Run targeted tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_living_self_service.py \
  tests/test_file_outputs.py \
  tests/test_quality_relationships.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/living_self_service.py core/profile_nuwa.py tests/test_living_self_service.py
git commit -m "feat: build evidence-backed Living Self"
```

### Task 7: Living Self versions, diff, confirm, and restore

**Files:**
- Modify: `core/living_self_service.py`
- Modify: `tests/test_living_self_service.py`

- [ ] **Step 1: Add failing version tests**

```python
def test_confirm_creates_immutable_version_and_current_pointer(tmp_path):
    service = LivingSelfService(tmp_path)
    candidate = service.build_candidate()
    confirmed = service.confirm(candidate, reason="reviewed")
    version_path = tmp_path / "model/living-self/versions" / f"{confirmed['version_id']}.json"
    assert version_path.is_file()
    assert service.current()["version_id"] == confirmed["version_id"]


def test_restore_creates_new_version_instead_of_overwriting_history(tmp_path):
    service = seeded_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="first")
    second = service.confirm(changed_candidate(service), reason="second")
    restored = service.restore(first["version_id"], reason="rollback")
    assert restored["version_id"] not in {first["version_id"], second["version_id"]}
    assert restored["restored_from"] == first["version_id"]
    assert len(service.versions()) == 3


def test_diff_lists_added_changed_and_removed_items(tmp_path):
    service = seeded_service(tmp_path)
    first = service.confirm(service.build_candidate(), reason="first")
    second = service.confirm(changed_candidate(service), reason="second")
    diff = service.diff(first["version_id"], second["version_id"])
    assert set(diff) == {"added", "changed", "removed"}
```

- [ ] **Step 2: Confirm red**

Run:

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_living_self_service.py::test_confirm_creates_immutable_version_and_current_pointer \
  tests/test_living_self_service.py::test_restore_creates_new_version_instead_of_overwriting_history \
  tests/test_living_self_service.py::test_diff_lists_added_changed_and_removed_items -q
```

Expected: missing methods or assertions fail.

- [ ] **Step 3: Implement immutable version operations**

```python
def confirm(self, candidate: dict, *, reason: str) -> dict:
    confirmed = {
        **candidate,
        "version_id": new_version_id(),
        "status": "confirmed",
        "confirmed_at": utc_now(),
        "reason": reason,
    }
    atomic_write_json(self.versions_dir / f"{confirmed['version_id']}.json", confirmed)
    atomic_write_json(self.current_path, confirmed)
    atomic_write_text(self.current_md_path, render_living_self(confirmed))
    return confirmed
```

Implement `versions`, `load_version`, `diff`, and `restore`. Reject restore when the version does not exist. Render Markdown from structured fields and evidence IDs, not raw evidence bodies.

- [ ] **Step 4: Run all Living Self tests**

```bash
PYTHONPATH=core python3 -m pytest tests/test_living_self_service.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/living_self_service.py tests/test_living_self_service.py
git commit -m "feat: version and restore Living Self models"
```

### Task 8: Judgment store and real `cards` compatibility command

**Files:**
- Create: `core/judgment_store.py`
- Create: `tests/test_judgment_store.py`
- Modify: `core/immortal.py` at `command_cards()` and parser registration
- Modify: `core/orchestrator.py` at card stage registration
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: Write failing judgment and packaging tests**

```python
from judgment_store import JudgmentStore


def test_card_result_is_event_backed_and_persistent(tmp_path):
    store = JudgmentStore(tmp_path)
    card = store.create(
        title="先做只读审计",
        situation="生产升级",
        decision="先审计再修改",
        evidence_ids=["ev-1"],
    )
    store.transition(card["card_id"], "confirmed", reason="owner confirmed")
    updated = store.record_outcome(
        card["card_id"],
        status="positive",
        summary="避免了错误归因",
        observed_at="2026-07-19T08:00:00+00:00",
    )
    assert updated["outcome"]["status"] == "positive"
    assert JudgmentStore(tmp_path).get(card["card_id"])["outcome"]["status"] == "positive"


def test_cards_list_never_prints_private_body(tmp_path, capsys):
    store = JudgmentStore(tmp_path)
    store.create(
        title="敏感项目",
        situation="客户私密事项",
        decision="使用代号",
        evidence_ids=["ev-secret"],
        privacy="private",
    )
    assert store.cli_list(limit=10) == 0
    assert "客户私密事项" not in capsys.readouterr().out
```

Extend packaging smoke:

```python
cards = subprocess.run(
    [str(command), "cards", "stats"],
    env=env,
    text=True,
    capture_output=True,
    timeout=60,
)
self.assertEqual(cards.returncode, 0, msg=cards.stderr)
self.assertNotIn("cards.py", cards.stdout + cards.stderr)
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_judgment_store.py \
  tests/test_packaging.py::PackagingTest::test_pip_install_exposes_working_cli -q
```

Expected: missing module and clean-package `cards` failure.

- [ ] **Step 3: Implement judgment events and thin CLI**

Implement `JudgmentStore.create`, `get`, `list`, `transition`, `correct`, `record_outcome`, `cli_build`, `cli_list`, and `cli_stats`. Use the same event and deterministic current-view pattern as ClaimStore.

Replace the missing script dispatch:

```python
def command_cards(args) -> int:
    store = JudgmentStore(configured_vault_dir())
    if args.action == "build":
        return store.cli_build()
    if args.action == "list":
        return store.cli_list(limit=int(args.extra or 20))
    return store.cli_stats()
```

The orchestrator invokes `immortal.py cards build`, not a missing `cards.py`.

- [ ] **Step 4: Run judgment, packaging, and orchestrator tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_judgment_store.py \
  tests/test_packaging.py \
  tests/test_orchestrator_telemetry.py \
  tests/test_immortal_mirror_dispatch.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add \
  core/judgment_store.py \
  core/immortal.py \
  core/orchestrator.py \
  tests/test_judgment_store.py \
  tests/test_packaging.py
git commit -m "feat: restore judgment cards as a packaged service"
```

- [ ] **Step 6: Run Milestone B full suite**

```bash
PYTHONPATH=core python3 -m pytest -q
```

Expected: full suite passes.

## Milestone C: Context and Outcome Loop

### Task 8A: Context event store, preview TTL, and lifecycle recovery

**Files:**
- Create: `core/context_store.py`
- Create: `tests/test_context_store.py`

- [ ] **Step 1: Write failing lifecycle tests**

```python
from context_store import ContextStore, ContextStoreError


def test_preview_persists_revision_hash_and_ttl(tmp_path):
    store = ContextStore(tmp_path)
    preview = store.create_preview(
        task="评审技术方案",
        source_revision={
            "claims_event_seq": 10,
            "living_self_version": "lsv-1",
            "judgments_event_seq": 4,
            "compiler_version": "1.1.0",
            "policy_version": 1,
        },
        sections={"verified_facts": []},
        ttl_seconds=300,
        request_id="req-1",
        idempotency_key="idem-1",
    )
    assert preview["lifecycle_status"] == "preview"
    assert preview["availability_status"] == "active"
    assert preview["preview_hash"]
    assert ContextStore(tmp_path).get(preview["preview_id"])["preview_hash"] == preview["preview_hash"]


def test_expired_preview_cannot_compile(tmp_path, clock):
    store = ContextStore(tmp_path, clock=clock)
    preview = seeded_preview(store, ttl_seconds=1)
    clock.advance(seconds=2)
    try:
        store.begin_compile(
            preview["preview_id"],
            preview_hash=preview["preview_hash"],
            source_revision=preview["source_revision"],
            excluded_item_ids=[],
            request_id="req-compile",
            idempotency_key="idem-compile",
        )
    except ContextStoreError as exc:
        assert exc.code == "stale_preview"
    else:
        raise AssertionError("expired preview compiled")


def test_expired_consumed_context_still_accepts_outcome(tmp_path, clock):
    store = ContextStore(tmp_path, clock=clock)
    context = seeded_compiled_context(store, ttl_seconds=1)
    store.consume(
        context["context_id"],
        expected_version=2,
        request_id="req-consume",
        idempotency_key="idem-consume",
    )
    clock.advance(seconds=2)
    assert store.get(context["context_id"])["availability_status"] == "expired"
    result = store.mark_outcome_recorded(
        context["context_id"],
        expected_version=3,
        request_id="req-outcome",
        idempotency_key="idem-outcome",
    )
    assert result["lifecycle_status"] == "outcome_recorded"


def test_restart_repairs_current_view_from_events(tmp_path):
    store = ContextStore(tmp_path)
    preview = seeded_preview(store)
    store.current_path.unlink()
    repaired = ContextStore(tmp_path)
    assert repaired.get(preview["preview_id"])["based_on_event_seq"] == 1
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_context_store.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement the authoritative Context stream**

Use:

```text
~/.immortal/contexts/events.jsonl
~/.immortal/contexts/current.jsonl
~/.immortal/contexts/previews/<preview-id>.json
~/.immortal/contexts/packs/<context-id>/context.json
~/.immortal/contexts/packs/<context-id>/TASK_CONTEXT.md
```

`ContextStore` owns:

- create preview;
- verify preview hash, source revisions, TTL, excluded ID subset;
- transition preview to compiled;
- consume;
- mark outcome recorded;
- calculate availability without changing lifecycle history;
- replay and repair current view;
- idempotency and expected revision.

Provenance stores Claim event seq, Living Self version and content hash, Judgment event seq, compiler version, policy version, selected typed refs, and final content hash.

Do not delete expired event history or consumed packs. Cleanup may remove expired preview bodies only after writing a tombstone that preserves ID, hash, revisions, times, and lifecycle.

- [ ] **Step 4: Run Context store and event tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_context_store.py \
  tests/test_event_store.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/context_store.py tests/test_context_store.py
git commit -m "feat: persist Context lifecycle and preview revisions"
```

### Task 9: Context preview with scope and privacy filtering

**Files:**
- Create: `core/context_compiler.py`
- Create: `tests/test_context_compiler.py`
- Reuse: `core/context_store.py`

- [ ] **Step 1: Write failing preview tests**

```python
from context_compiler import ContextCompiler


def test_preview_separates_verified_inferred_and_unknown(tmp_path):
    compiler = seeded_compiler(tmp_path)
    preview = compiler.preview("评审一个客户技术方案", mode="reviewer")
    assert preview["lifecycle_status"] == "preview"
    assert preview["sections"]["verified_facts"]
    assert preview["sections"]["inferences"]
    assert "unknowns" in preview["sections"]


def test_preview_excludes_private_and_wrong_scope_items(tmp_path):
    compiler = seeded_compiler(tmp_path)
    add_claim(tmp_path, "家庭私密", privacy="private", role_scope=["family"])
    add_claim(tmp_path, "内容账号风格", privacy="context_safe", role_scope=["creator"])
    preview = compiler.preview(
        "评审客户技术方案",
        mode="reviewer",
        role_scope=["work"],
        domain_scope=["technical"],
    )
    encoded = json.dumps(preview, ensure_ascii=False)
    assert "家庭私密" not in encoded
    assert "内容账号风格" not in encoded
    assert preview["privacy_policy"]["excluded_count"] == 2
    assert set(preview["privacy_policy"]["reasons"]) == {"private", "scope_mismatch"}


def test_preview_is_bounded(tmp_path):
    compiler = seeded_compiler(tmp_path, claim_count=200)
    preview = compiler.preview("技术评审", mode="reviewer", max_chars=4000)
    assert preview["budget"]["used_chars"] <= 4000
    assert all(len(items) <= 20 for items in preview["sections"].values())
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_context_compiler.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement bounded deterministic preview**

```python
from typing import Any, Optional


class ContextCompiler:
    def preview(
        self,
        task: str,
        *,
        mode: str = "auto",
        role_scope: Optional[list[str]] = None,
        domain_scope: Optional[list[str]] = None,
        max_chars: int = 24000,
    ) -> dict[str, Any]:
        selected, excluded = self._select(
            task,
            role_scope=role_scope or infer_role_scope(task, mode),
            domain_scope=domain_scope or infer_domain_scope(task, mode),
        )
        sections = self._sections(selected)
        sections = apply_budget(sections, max_chars=max_chars, per_section_limit=20)
        return self.store.create_preview(
            task=task,
            mode=mode,
            sections=sections,
            excluded=excluded,
            max_chars=max_chars,
            source_revision=self._source_revision(),
            ttl_seconds=900,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
```

Ranking order:

1. exact domain and role match;
2. confirmed over candidate;
3. evidence-backed over inferred;
4. relevant judgment card with outcome;
5. recency only after evidence strength;
6. deterministic ID tie-break.

The compiler must never read unbounded raw daily files. Use ClaimStore, Living Self current, JudgmentStore, EvidenceCatalog, and bounded verified SQLite keyset queries. If index integrity is not proven, raise `index_unavailable`; do not scan JSONL.

- [ ] **Step 4: Run compiler tests**

```bash
PYTHONPATH=core python3 -m pytest tests/test_context_compiler.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/context_compiler.py tests/test_context_compiler.py
git commit -m "feat: preview scoped private-safe context"
```

### Task 10: Compile, stale-preview protection, and Agent Bridge integration

**Files:**
- Modify: `core/context_compiler.py`
- Modify: `core/task_compile.py`
- Modify: `core/agent_bridge.py`
- Modify: `tests/test_context_compiler.py`
- Modify: `tests/test_brief_recency.py`
- Modify: `tests/regression_p0.py`

- [ ] **Step 1: Add failing compile tests**

```python
def test_compile_rejects_stale_source_revision(tmp_path):
    compiler = seeded_compiler(tmp_path)
    preview = compiler.preview("评审技术方案")
    add_confirmed_claim(tmp_path, "新主张", evidence_id="ev-new")
    try:
        compiler.compile(
            preview_id=preview["preview_id"],
            preview_hash=preview["preview_hash"],
            excluded_item_ids=[],
        )
    except ContextCompileError as exc:
        assert exc.code == "stale_preview"
    else:
        raise AssertionError("stale source revision compiled")


def test_compiled_markdown_labels_evidence_inference_and_unknown(tmp_path):
    compiler = seeded_compiler(tmp_path)
    preview = compiler.preview("评审技术方案")
    compiled = compiler.compile(
        preview_id=preview["preview_id"],
        preview_hash=preview["preview_hash"],
        excluded_item_ids=[],
    )
    markdown = Path(compiled["context_md"]).read_text(encoding="utf-8")
    assert "## 已验证事实" in markdown
    assert "## 系统推断" in markdown
    assert "## 未知与边界" in markdown
    assert "Evidence IDs" in markdown
    assert compiled["lifecycle_status"] == "compiled"
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_context_compiler.py::test_compile_rejects_stale_source_revision \
  tests/test_context_compiler.py::test_compiled_markdown_labels_evidence_inference_and_unknown -q
```

Expected: missing compile behavior.

- [ ] **Step 3: Implement hash-bound compilation**

```python
def preview_hash(payload: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"preview_hash", "generated_at"}
    }
    body = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def compile(
    self,
    *,
    preview_id: str,
    preview_hash: str,
    excluded_item_ids: list[str],
) -> dict[str, Any]:
    preview = self.store.require_compilable_preview(
        preview_id,
        preview_hash=preview_hash,
        current_source_revision=self._source_revision(),
        excluded_item_ids=excluded_item_ids,
    )
    context_id = "ctx_" + uuid.uuid4().hex
    compiled = build_compiled_context(
        preview,
        context_id=context_id,
        excluded_item_ids=excluded_item_ids,
    )
    return self.store.commit_compiled(compiled)
```

The server reloads the stored preview and permits only removal of IDs that were present in that preview. It never accepts client-supplied section bodies.

Make `task_compile.py` a compatibility CLI for preview and compile. Make `agent_bridge.py context` call the new compiler after existing preflight. Preserve v1 P0 semantics for missing, smoke-only, and stale vaults. Redact stderr before it can enter any Context metadata or Markdown.

- [ ] **Step 4: Run context and P0 tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_context_compiler.py \
  tests/test_brief_recency.py \
  tests/test_agent_bridge_cors.py -q
python3 tests/regression_p0.py
```

Expected: tests pass and P0 remains `8/8`.

- [ ] **Step 5: Commit**

```bash
git add \
  core/context_compiler.py \
  core/task_compile.py \
  core/agent_bridge.py \
  tests/test_context_compiler.py \
  tests/test_brief_recency.py \
  tests/regression_p0.py
git commit -m "feat: compile traceable task context packs"
```

### Task 11: Context consumption and outcome feedback

**Files:**
- Create: `core/outcome_store.py`
- Create: `tests/test_outcome_store.py`
- Modify: `core/context_compiler.py`

- [ ] **Step 1: Write failing outcome tests**

```python
from outcome_store import OutcomeStore


def test_context_must_be_consumed_before_outcome(tmp_path):
    store = OutcomeStore(tmp_path)
    store.register_context(
        {"context_id": "ctx-1", "lifecycle_status": "compiled", "stream_version": 2}
    )
    try:
        store.record_outcome(
            "ctx-1",
            adopted="yes",
            result="positive",
            summary="有效",
            expected_version=2,
            request_id="req-outcome",
            idempotency_key="idem-outcome",
        )
    except ValueError as exc:
        assert "consumed" in str(exc)
    else:
        raise AssertionError("outcome recorded before consume")


def test_outcome_challenges_items_without_auto_rewriting_them(tmp_path):
    store = OutcomeStore(tmp_path)
    store.register_context(
        {"context_id": "ctx-1", "lifecycle_status": "compiled", "stream_version": 2}
    )
    store.consume(
        "ctx-1",
        expected_version=2,
        request_id="req-consume",
        idempotency_key="idem-consume",
    )
    outcome = store.record_outcome(
        "ctx-1",
        adopted="partial",
        result="negative",
        summary="判断卡不适用于该客户",
        challenged_refs=[{"kind": "judgment", "id": "jdg-1", "revision": 1}],
        expected_version=3,
        request_id="req-outcome",
        idempotency_key="idem-outcome",
    )
    assert outcome["challenged_refs"] == [
        {"kind": "judgment", "id": "jdg-1", "revision": 1}
    ]
    assert store.context("ctx-1")["lifecycle_status"] == "outcome_recorded"
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_outcome_store.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement context and outcome events**

```python
from typing import Optional


class OutcomeStore:
    def consume(
        self,
        context_id: str,
        *,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
    ) -> dict:
        context = self.context(context_id)
        if context["lifecycle_status"] != "compiled":
            raise ValueError("only compiled context can be consumed")
        return self.contexts.consume(
            context_id,
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )

    def record_outcome(
        self,
        context_id: str,
        *,
        adopted: str,
        result: str,
        summary: str,
        confirmed_refs: Optional[list[dict]] = None,
        challenged_refs: Optional[list[dict]] = None,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
    ) -> dict:
        if self.context(context_id)["lifecycle_status"] != "consumed":
            raise ValueError("context must be consumed before outcome")
        event = new_outcome_event(
            context_id=context_id,
            adopted=adopted,
            result=result,
            summary=summary,
            confirmed_refs=confirmed_refs or [],
            challenged_refs=challenged_refs or [],
        )
        self.outcomes.append(event)
        self.contexts.mark_outcome_recorded(
            context_id,
            expected_version=expected_version,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        return event
```

Outcome events create trust-page review suggestions; they do not directly mutate confirmed Claims, Living Self items, or Judgment Cards.

- [ ] **Step 4: Run outcome and context tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_outcome_store.py \
  tests/test_context_compiler.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/outcome_store.py core/context_compiler.py tests/test_outcome_store.py
git commit -m "feat: close the context outcome loop"
```

- [ ] **Step 6: Run Milestone C full suite**

```bash
PYTHONPATH=core python3 -m pytest -q
python3 tests/regression_p0.py
```

Expected: full suite passes and P0 is `8/8`.

## Milestone D: Real Product API and Dashboard

### Task 12: Bounded product read models

**Files:**
- Create: `core/product_data.py`
- Create: `tests/test_product_data.py`
- Modify: `core/control_data.py`

- [ ] **Step 1: Write failing home and memory tests**

```python
from product_data import ProductData


def test_home_leads_with_memory_value_not_machine_metrics(tmp_path):
    data = seeded_product_data(tmp_path)
    home = data.home()
    assert list(home)[:5] == [
        "remembered_today",
        "understanding_changes",
        "needs_confirmation",
        "latest_context_use",
        "latest_outcome",
    ]
    assert "system_health" in home


def test_memory_list_is_cursor_bounded_and_body_free(tmp_path):
    data = seeded_product_data(tmp_path, memory_count=80)
    page = data.memories({"limit": ["50"]})
    assert len(page["items"]) == 50
    assert page["next_cursor"]
    assert all("content" not in item for item in page["items"])
    assert all("summary" in item for item in page["items"])


def test_self_item_detail_exposes_ids_not_raw_private_evidence(tmp_path):
    data = seeded_product_data(tmp_path)
    detail = data.self_item("self-1")
    assert detail["evidence_ids"] == ["ev-private"]
    assert "private body" not in json.dumps(detail)


def test_memory_api_refuses_untrusted_index_instead_of_scanning_jsonl(tmp_path):
    data = seeded_product_data(tmp_path)
    data.index_integrity.mark_untrusted("id parity failed")
    try:
        data.memories({"limit": ["20"]})
    except ProductDataError as exc:
        assert exc.code == "index_unavailable"
    else:
        raise AssertionError("untrusted index was queried")


def test_keyset_cursor_has_no_duplicates_or_gaps(tmp_path):
    data = seeded_product_data(tmp_path, memory_count=120)
    seen = []
    cursor = ""
    while True:
        page = data.memories({"limit": ["17"], "cursor": [cursor]})
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 120
    assert len(set(seen)) == 120
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_product_data.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement product-facing bounded methods**

```python
class ProductData:
    def home(self) -> dict[str, Any]:
        return {
            "remembered_today": self._remembered_today(limit=8),
            "understanding_changes": self._understanding_changes(limit=8),
            "needs_confirmation": self._needs_confirmation(limit=8),
            "latest_context_use": self._latest_context_use(),
            "latest_outcome": self._latest_outcome(),
            "system_health": self.control_center.snapshot()["summary"],
        }
```

Implement:

- `home`
- `memories`
- `memory_detail`
- `self_model`
- `self_item`
- `self_versions`
- `self_diff`
- `judgments`
- `judgment_detail`
- `contexts`
- `context_detail`
- `trust`
- `system`

All list methods accept bounded `limit` and opaque keyset cursor. The cursor encodes the last stable sort tuple plus a signed schema marker, not an offset. `system()` delegates to existing Control Center and ControlData instead of duplicating probes.

No v2 method calls `ControlData._iter_memories()` or scans JSONL. The response includes index coverage metadata for person, project, and topic filters so partial derived coverage is never presented as full-vault coverage.

- [ ] **Step 4: Run product and control-data tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_product_data.py \
  tests/test_control_data.py \
  tests/test_control_modules.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/product_data.py core/control_data.py tests/test_product_data.py
git commit -m "feat: add bounded Living Self read models"
```

### Task 13: `/api/v2` GET contracts

**Files:**
- Create: `core/product_http.py`
- Create: `tests/test_product_http_v2.py`
- Modify: `core/profile_review.py` at `ReviewHandler.do_GET`
- Modify: `tests/test_control_center_http.py`

- [ ] **Step 1: Write failing route tests**

```python
def test_v2_get_routes_return_real_product_data(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        for path in (
            "/api/v2/home",
            "/api/v2/memories",
            "/api/v2/self",
            "/api/v2/self/versions",
            "/api/v2/judgments",
            "/api/v2/contexts",
            "/api/v2/trust",
            "/api/v2/system",
        ):
            status, payload = get_json(base + path)
            assert status == 200, (path, payload)
    finally:
        stop(server)


def test_unknown_v2_route_uses_stable_error(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        status, payload = get_json(base + "/api/v2/unknown")
    finally:
        stop(server)
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["retryable"] is False
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_product_http_v2.py -q
```

Expected: routes return 404.

- [ ] **Step 3: Implement an isolated v2 router**

```python
class ProductRouter:
    def get(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict]:
        if path == "/api/v2/home":
            return 200, self.data.home()
        if path == "/api/v2/memories":
            return 200, self.data.memories(query)
        if match := re.fullmatch(r"/api/v2/memories/([^/]+)", path):
            return 200, self.data.memory_detail(unquote(match.group(1)))
        if path == "/api/v2/self":
            return 200, self.data.self_model()
        if path == "/api/v2/self/versions":
            return 200, self.data.self_versions()
        if path == "/api/v2/judgments":
            return 200, self.data.judgments(query)
        if path == "/api/v2/contexts":
            return 200, self.data.contexts(query)
        if path == "/api/v2/trust":
            return 200, self.data.trust()
        if path == "/api/v2/system":
            return 200, self.data.system()
        raise ProductHttpError(404, "not_found", "未找到接口")
```

Add all detail and diff routes from the design. `ReviewHandler` delegates any `/api/v2/` GET before legacy routing. Map domain exceptions to the stable error protocol.

- [ ] **Step 4: Run HTTP v2 and legacy HTTP tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_product_http_v2.py \
  tests/test_control_center_http.py \
  tests/test_control_http.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/product_http.py core/profile_review.py tests/test_product_http_v2.py tests/test_control_center_http.py
git commit -m "feat: expose Living Self read APIs"
```

### Task 14: `/api/v2` mutation safety and idempotency

**Files:**
- Create: `core/product_mutations.py`
- Modify: `core/product_http.py`
- Modify: `core/profile_review.py` at `ReviewHandler.do_POST`
- Modify: `core/living_self_service.py`
- Create: `tests/test_product_mutations.py`
- Modify: `tests/test_living_self_service.py`
- Modify: `tests/test_product_http_v2.py`

- [x] **Step 1: Add failing write-contract tests**

```python
def test_write_requires_local_origin_request_id_and_idempotency(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        status, payload = post_json(
            base + "/api/v2/self/items/self-1/actions",
            {"action": "correct"},
            headers={},
        )
    finally:
        stop(server)
    assert status == 400
    assert payload["error"]["code"] == "request_metadata_required"


def test_retried_correction_returns_same_result(tmp_path):
    server, base = start_v2_server(tmp_path)
    headers = {
        "Origin": base,
        "X-Immortal-Request-Id": "req-1",
        "Idempotency-Key": "idem-1",
    }
    body = {
        "action": "correct",
        "statement": "修正后的主张",
        "reason": "更准确",
        "expected_version": 1,
    }
    try:
        first_status, first = post_json(
            base + "/api/v2/self/items/self-1/actions", body, headers=headers
        )
        second_status, second = post_json(
            base + "/api/v2/self/items/self-1/actions", body, headers=headers
        )
    finally:
        stop(server)
    assert first_status == second_status == 200
    assert first["claim_id"] == second["claim_id"]


def test_wrong_port_origin_content_type_and_large_body_are_rejected(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        wrong_origin_status, _ = post_json(
            base + "/api/v2/self/items/self-1/actions",
            {"action": "correct", "expected_version": 1},
            headers={
                "Origin": "http://127.0.0.1:1",
                "X-Immortal-Request-Id": "req-wrong-origin",
                "Idempotency-Key": "idem-wrong-origin",
            },
        )
        wrong_type_status, _ = post_raw(
            base + "/api/v2/self/items/self-1/actions",
            b"{}",
            content_type="text/plain",
        )
        large_status, _ = post_raw(
            base + "/api/v2/self/items/self-1/actions",
            b"x" * (MAX_REQUEST_BYTES + 1),
            content_type="application/json",
        )
    finally:
        stop(server)
    assert wrong_origin_status == 403
    assert wrong_type_status == 415
    assert large_status == 413


def test_stale_expected_version_returns_conflict(tmp_path):
    server, base = start_v2_server(tmp_path)
    headers = write_headers(base, key="stale")
    try:
        status, payload = post_json(
            base + "/api/v2/self/items/self-1/actions",
            {"action": "correct", "expected_version": 0},
            headers=headers,
        )
    finally:
        stop(server)
    assert status == 409
    assert payload["error"]["code"] == "version_conflict"
```

- [x] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_product_http_v2.py::test_write_requires_local_origin_request_id_and_idempotency \
  tests/test_product_http_v2.py::test_retried_correction_returns_same_result -q
```

Expected: missing safety enforcement.

- [x] **Step 3: Implement explicit mutation routes**

Implement:

```text
POST /api/v2/self/items/{id}/actions
POST /api/v2/self/versions/{id}/restore
POST /api/v2/judgments
POST /api/v2/judgments/{id}/actions
POST /api/v2/contexts/preview
POST /api/v2/contexts
POST /api/v2/contexts/{id}/consume
POST /api/v2/contexts/{id}/outcomes
```

Use:

```python
def require_write_metadata(headers, expected_origin: str) -> RequestMetadata:
    origin = headers.get("Origin", "")
    request_id = headers.get("X-Immortal-Request-Id", "")
    idempotency_key = headers.get("Idempotency-Key", "")
    if origin != expected_origin:
        raise ProductHttpError(403, "origin_not_allowed", "写操作只接受当前服务同源请求")
    if not request_id or not idempotency_key:
        raise ProductHttpError(
            400,
            "request_metadata_required",
            "写操作需要本地 Origin、request ID 和幂等键",
        )
    return RequestMetadata(request_id, idempotency_key)
```

Before parsing:

- require `Content-Type: application/json`;
- reject `Content-Length` above 256 KiB;
- reject missing or invalid `expected_version`;
- compare exact scheme, host, and port;
- map internal exceptions to stable, redacted errors;
- never return filesystem paths, commands, stderr, or raw exception text.

Persist idempotency results under `~/.immortal/runtime/idempotency.json` with
`mutate_state_atomic` or a stricter anchored equivalent. Audit writes record
IDs, action, target, timestamp, status, and error code, never request bodies or
private statements.

The mutation coordinator must be stricter than the legacy state helper where
needed: the ledger and lock are anchored `0600` regular files and fail closed
on symlinks, unsafe permissions, corruption, or lock timeout. Store only a
canonical request digest and safe IDs, never a raw idempotency key or body.
Hold one coordinator lock across prepare, domain commit or recovery, and
completion. Native Claim, Judgment, Context, and Outcome event idempotency
remains authoritative.

The ledger parser rejects duplicate JSON keys, non-standard numeric constants,
and inconsistent pending, completed, or failed field combinations. A prepared
entry with no domain result remains readable after restart. Public compiled
Context markdown preserves its line structure while path and credential
redaction remains active.

Self item actions require explicit `claim_id`, `expected_self_version`, and the
Claim revision in integer `expected_version`. Verify that the item belongs to
the current Living Self snapshot and that the Claim belongs to the item; never
choose the first Claim or batch multiple Claim streams implicitly. After the
Claim event, materialize exactly one new Living Self version or return an
explicit stale-derived result.

The item action contract supports `correct` only. Living Self contains confirmed
Claims, while the Claim state machine cannot confirm, reject, or reconsider an
already confirmed Claim. Reject those three action names with stable
`invalid_transition`. A future candidate-Claim review queue needs its own read
model and endpoint; do not expose impossible controls on a confirmed Self item.

Living Self restore uses the current Living Self `version_id` as
`expected_version`. Preallocate the result version ID in the pending ledger and
pass it with the expected parent into a recoverable Living Self materialization
method. Test crashes after intent prepare, immutable version publication,
current publication, and before ledger completion. Retrying must converge on
the same result ID; a third-party current version causes `version_conflict`.

- [x] **Step 4: Run all HTTP tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_product_http_v2.py \
  tests/test_control_center_http.py \
  tests/test_control_http.py -q
```

Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add \
  core/living_self_service.py \
  core/outcome_store.py \
  core/product_http.py \
  core/product_mutations.py \
  core/profile_review.py \
  tests/test_living_self_service.py \
  tests/test_outcome_store.py \
  tests/test_product_http_v2.py \
  tests/test_product_mutations.py
git commit -m "feat: secure product mutations"
```

### Task 15: Product UI shell, System, and Memories

**Files:**
- Create: `core/product_ui.py`
- Create: `core/product_assets/product.css`
- Create: `core/product_assets/api.js`
- Create: `core/product_assets/app.js`
- Create: `core/product_assets/router.js`
- Create: `core/product_assets/dialog.js`
- Create: `core/product_assets/views/system.js`
- Create: `core/product_assets/views/memories.js`
- Create: `tests/test_product_ui_v2.py`
- Modify: `core/profile_review.py`
- Modify: `core/product_http.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_control_center_ui.py`

- [x] **Step 1: Write failing UI structure tests**

```python
from product_ui import product_page_html


def test_ui_has_seven_product_modules_and_no_fake_actions():
    page = product_page_html()
    for view in ("home", "memories", "self", "judgments", "use", "trust", "system"):
        assert f'data-view="{view}"' in page
    assert "首页" in page
    assert "记忆" in page
    assert "我" in page
    assert "判断" in page
    assert "使用" in page
    assert "信任" in page
    assert "系统" in page
    assert "setTimeout(() => success" not in page
    assert "localStorage.setItem('fake" not in page


def test_home_and_memory_use_v2_apis_and_real_detail():
    page = product_page_html()
    assert 'href="/assets/product.css"' in page
    assert 'src="/assets/app.js"' in page
    assert 'type="module"' in page
    assert "<style" not in page
    assert "<script>" not in page
    assert 'aria-label="证据与详情"' in page


def test_assets_are_packaged_and_csp_has_no_inline_escape_hatch():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "core/product_assets/product.css",
        "core/product_assets/app.js",
        "core/product_assets/views/system.js",
        "core/product_assets/views/memories.js",
    ):
        assert (root / relative).is_file()
    assert "'unsafe-inline'" not in SECURITY_HEADERS["Content-Security-Policy"]
```

- [x] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_product_ui_v2.py -q
```

Expected: missing module.

- [x] **Step 3: Implement semantic shell and real Home/Memories renderers**

Create the semantic shell:

```python
def product_page_html(title: str = "Immortal Memory") -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="/assets/product.css">
</head>
<body>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <aside class="rail" aria-label="产品导航">
    <div class="brand"><span>IMMORTAL</span><small>MEMORY</small></div>
    <nav>{NAVIGATION_HTML}</nav>
    <div id="global-health" aria-live="polite"></div>
  </aside>
  <main id="main" tabindex="-1">
    <header id="topbar"></header>
    <section id="view" aria-live="polite" aria-busy="false"></section>
  </main>
  <aside id="drawer" aria-label="证据与详情" aria-hidden="true"></aside>
  <div id="toast" role="status" aria-live="polite"></div>
  <script type="module" src="/assets/app.js"></script>
</body>
</html>"""
```

Implement `renderSystem`, `renderMemories`, opaque keyset cursor pagination, query filters, detail dialog, loading, empty, and error states as ES modules. `api.js` must fetch server state after every mutation; no optimistic fake success. `router.js` uses History API and a render generation or `AbortController` so a stale request cannot overwrite the active page.

`profile_review.py` serves:

```text
/ -> product_page_html()
/control-center -> existing control_center_page_html()
/review -> 302 /?view=self&filter=candidate
/agent-factory -> 302 /?view=use
/assets/<allowlisted path> -> packaged local asset
```

Static paths are allowlisted and reject traversal. Set correct MIME types and `Cache-Control: no-store`.

Add `product_assets/**/*.css` and `product_assets/**/*.js` to package data, then verify them in a built wheel. Preserve the old Control Center implementation for one release cycle.

- [x] **Step 4: Run UI tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_product_ui_v2.py \
  tests/test_control_center_ui.py -q
```

Expected: all pass after updating legacy assertions to the new seven-module contract.

- [x] **Step 5: Commit**

```bash
git add \
  core/product_ui.py \
  core/product_assets \
  core/profile_review.py \
  core/product_http.py \
  pyproject.toml \
  tests/test_product_ui_v2.py \
  tests/test_control_center_ui.py
git commit -m "feat: replace ops homepage with memory product"
```

### Task 16: Self, Judgment, Use, Trust, and System UI

**Files:**
- Create: `core/product_assets/views/home.js`
- Create: `core/product_assets/views/self.js`
- Create: `core/product_assets/views/judgments.js`
- Create: `core/product_assets/views/contexts.js`
- Create: `core/product_assets/views/trust.js`
- Modify: `core/product_assets/app.js`
- Modify: `core/product_assets/api.js`
- Modify: `core/product_assets/dialog.js`
- Modify: `core/product_assets/product.css`
- Modify: `tests/test_product_ui_v2.py`

- [x] **Step 0: Close read contracts required by real interactions**

Before adding interactive controls, extend the bounded read models so the UI
never guesses concurrency authority or relies on a transient mutation response:

- Self item detail exposes bounded `claim_refs` with the authoritative Claim
  revision required by `correct`, and the top-level Self response reports when
  its 50-item view is truncated;
- Context detail exposes the immutable preview approval fields after refresh,
  including `preview_hash`, expiry, sections, budget, provenance, and privacy;
- compiled, consumed, and outcome-recorded Context detail exposes the verified
  immutable pack snapshot and the exact `context_markdown` delivered to the
  Agent, without rebuilding it from the current model.

Tests must prove the read response is bounded, redacted, version-complete, and
fail-closed when the immutable Context snapshot cannot be verified.

- [x] **Step 1: Add failing interaction contract tests**

```python
def test_self_ui_exposes_evidence_versions_and_real_correction():
    root = Path(__file__).resolve().parents[1] / "core/product_assets"
    self_js = (root / "views/self.js").read_text(encoding="utf-8")
    api_js = (root / "api.js").read_text(encoding="utf-8")
    assert "/api/v2/self" in self_js
    assert "/api/v2/self/versions" in self_js
    assert "correctSelfItem" in self_js
    assert "restoreSelfVersion" in self_js
    assert "Idempotency-Key" in api_js
    assert "If-Match" in api_js


def test_use_ui_has_preview_compile_consume_and_outcome_states():
    page = (
        Path(__file__).resolve().parents[1]
        / "core/product_assets/views/contexts.js"
    ).read_text(encoding="utf-8")
    for endpoint in (
        "/api/v2/contexts/preview",
        "/api/v2/contexts",
        "/consume",
        "/outcomes",
    ):
        assert endpoint in page
    for state in (
        "准备中",
        "预览完成",
        "编译中",
        "可使用",
        "已交给 Agent",
        "待记录结果",
        "结果已记录",
        "失败",
    ):
        assert state in page


def test_ui_is_responsive_accessible_and_motion_safe():
    page = (
        Path(__file__).resolve().parents[1]
        / "core/product_assets/product.css"
    ).read_text(encoding="utf-8")
    assert "@media (max-width: 760px)" in page
    assert "@media print" in page
    assert "@media (prefers-reduced-motion: reduce)" in page
    assert ":focus-visible" in page
    assert "aria-busy" in page
    assert "aria-modal" in page
```

- [x] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_product_ui_v2.py -q
```

Expected: new assertions fail.

- [x] **Step 3: Implement remaining real renderers**

Export these renderer functions from the dedicated view modules:

```text
renderSelf
renderSelfItem
renderSelfVersions
renderJudgments
renderJudgmentDetail
renderUse
renderContextPreview
renderContextDetail
renderTrust
renderSystem
```

Add mutation helpers:

```javascript
async function mutate(path, body) {
  const requestId = crypto.randomUUID();
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Immortal-Request-Id': requestId,
      'Idempotency-Key': requestId,
      'If-Match': String(body.expected_version),
    },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new ApiError(payload.error);
  return payload;
}
```

Implement actual confirmation dialogs inside the app, not `window.confirm`. After confirm/correct/reject/restore/compile/consume/outcome, re-fetch the affected API and display the persisted state.

The dialog utility must:

- move focus into the dialog;
- set the background inert;
- trap Tab and Shift+Tab;
- close on Escape;
- return focus to the trigger;
- expose `aria-modal="true"` and a visible title.

Every input has an explicit label. Interactive controls are at least 44px. Mobile input font is at least 16px. Active navigation uses `aria-current`. Home is implemented last so it can aggregate the already-real System, Memories, Self, Judgment, Context, and Trust states.

Button visual states:

- idle;
- hover;
- focus-visible;
- active press;
- disabled with reason;
- pending with text;
- success only after server response;
- failure with retry.

- [x] **Step 4: Run UI and HTTP tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_product_ui_v2.py \
  tests/test_product_http_v2.py \
  tests/test_control_center_http.py -q
```

Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add core/product_assets tests/test_product_ui_v2.py
git commit -m "feat: complete the Living Self product dashboard"
```

- [x] **Step 6: Run Milestone D full suite**

```bash
PYTHONPATH=core python3 -m pytest -q
python3 tests/regression_p0.py
```

Expected: all tests pass and P0 is `8/8`.

## Milestone E: Migration, Packaging, Acceptance, and Release

### Task 17: Export, restore, and v1.1 migration compatibility

**Production migration blocker discovered 2026-07-22:** the read-only live
index audit found 467 `hermes-conversation` rows whose legacy timestamps are
naive, while the other 678,390 rows contain an explicit timezone. Schema v3
must continue to reject naive timestamps. Before rebuilding or switching the
production index, migration must derive the missing offset from authoritative
source metadata or a separately verified source contract. If that evidence is
unavailable, quarantine and report the 467 records and keep the production
switch blocked. Never guess UTC or the host timezone silently. Tests must cover
both an evidence-backed conversion and the fail-closed quarantine path, while
proving that raw source bytes remain unchanged.

The production migration must also prewarm the schema-v3 index verification
receipt after the final staging database is published and before the dashboard
is switched on. The receipt is valid only for the exact source, main database,
WAL/SHM, schema, metadata generation, and validation version. Measure both the
full first-generation verification and a fresh-process receipt hit. A missing,
stale, corrupt, over-permissive, or symlinked receipt keeps the switch blocked.

**Files:**
- Modify: `core/export_restore.py`
- Modify: `core/orchestrator.py`
- Create: `tests/test_v11_migration.py`
- Modify: `tests/test_export_restore.py`
- Modify: `tests/test_integrity_audit.py`

- [x] **Step 1: Write failing migration and restore tests**

```python
def test_v10_vault_migrates_without_raw_changes(tmp_path):
    vault = make_v10_vault(tmp_path)
    before = raw_hashes(vault)
    result = run_v11_migration(vault)
    after = raw_hashes(vault)
    assert result["ok"] is True
    assert before == after
    assert (vault / "model/claims/current.jsonl").is_file()
    assert (vault / "model/living-self/current.json").is_file()


def test_export_restore_includes_v11_event_layers(tmp_path):
    source = make_v11_vault(tmp_path / "source")
    manifest = create_export(source, tmp_path / "exports", include_raw=True)
    restored = tmp_path / "restored"
    restore_export(manifest["export_dir"], restored)
    result = restore_check(manifest["export_dir"], strict=True)
    assert result["ok"] is True
    assert (restored / "model/claims/events.jsonl").read_bytes() == (
        source / "model/claims/events.jsonl"
    ).read_bytes()
```

- [x] **Step 2: Confirm red**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_v11_migration.py \
  tests/test_export_restore.py -q
```

Expected: v1.1 paths are absent from export or migration.

- [x] **Step 3: Add derived layers to export and orchestrator**

Add these optional portable paths:

```python
V11_DERIVED_PATHS = [
    Path("model/claims/events.jsonl"),
    Path("model/claims/current.jsonl"),
    Path("model/attribution/latest-report.json"),
    Path("model/living-self/current.json"),
    Path("model/living-self/current.md"),
    Path("model/living-self/versions"),
    Path("model/living-self/evaluations"),
    Path("judgment/events.jsonl"),
    Path("judgment/current.jsonl"),
    Path("judgment/evaluations.jsonl"),
    Path("contexts/events.jsonl"),
    Path("contexts/current.jsonl"),
    Path("contexts/packs"),
    Path("outcomes/events.jsonl"),
]
```

Claim, Judgment, Context, and Outcome event streams are required after v1.1 migration. Their absence makes strict v1.1 restore fail. Cache-only Markdown previews may be optional.

The manifest records:

```json
{
  "event_heads": {
    "claims": 0,
    "judgments": 0,
    "contexts": 0,
    "outcomes": 0
  },
  "current_watermarks": {
    "claims": 0,
    "judgments": 0,
    "contexts": 0
  },
  "schema_versions": {
    "model": 1,
    "events": 1
  }
}
```

Restore replays each event stream into a temporary current view and compares its hash and watermark with the exported current view. Existing files included in a manifest must pass hash verification. Add orchestrator stages:

```text
claims-migrate
profile-attribution-audit
living-self-build
cards build
quality
```

Do not alter raw files.

- [x] **Step 4: Run migration, restore, and integrity tests**

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_v11_migration.py \
  tests/test_export_restore.py \
  tests/test_integrity_audit.py \
  tests/test_orchestrator_telemetry.py -q
```

Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add \
  core/export_restore.py \
  core/orchestrator.py \
  tests/test_v11_migration.py \
  tests/test_export_restore.py \
  tests/test_integrity_audit.py
git commit -m "feat: migrate and restore Living Self layers"
```

Completion evidence recorded 2026-07-22:

- Task17 targeted suite: 35 passed.
- Full suite: 1214 passed in 68.07 seconds.
- P0 regression: 8/8 passed.
- Tracked-file privacy scan: ok.
- Independent production review: Ready, no remaining P0/P1.
- Live source remained read-only and retained SHA256
  `311d179ffa36f89b1d52c37b0f7831f3c201c72924793fefa161f10fafdd2f39`.
- The 467 unresolved legacy naive timestamps remain quarantined and keep the
  production switch blocked until authoritative timezone evidence exists.

### Task 18: Clean-package completeness and Living Self P0 regression

**Files:**
- Create: `tests/test_v11_packaging.py`
- Create: `tests/regression_living_self_p0.py`
- Modify: `pyproject.toml` package-data only if tests prove a missing artifact
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/ci.example.yml`

- [ ] **Step 1: Write clean wheel and P0 scenarios**

`tests/test_v11_packaging.py` builds and installs a wheel into a temporary venv, then verifies:

```python
for command in (
    ("claims-migrate", "--help"),
    ("cards", "stats"),
    ("agent-context", "test", "--print"),
    ("profile-review", "--help"),
):
    result = subprocess.run([str(cli), *command], env=env, capture_output=True, text=True)
    assert "No such file" not in result.stdout + result.stderr
    assert "can't open file" not in result.stdout + result.stderr
```

`tests/regression_living_self_p0.py` implements these ten scenarios:

```text
S1 other speaker cannot auto-confirm owner claim
S2 one-off request cannot become stable preference
S3 correction supersedes but preserves old claim
S4 role and account scope prevents cross-context use
S5 private evidence never enters Context Pack
S6 inference is labeled and cannot appear under verified facts
S7 stale preview cannot compile
S8 failed outcome creates review signal without rewriting model
S9 service restart preserves claim, context, and outcome state
S10 v1.0 vault migrates without changing raw hashes
```

- [ ] **Step 2: Run and confirm failures**

```bash
PYTHONPATH=core python3 -m pytest tests/test_v11_packaging.py -q
python3 tests/regression_living_self_p0.py
```

Expected: failures reveal any remaining packaging or integrated trust gaps.

- [ ] **Step 3: Fix only the proven package and integration gaps**

Ensure every module imported by CLI is included by setuptools. Do not add private generated data to package data. Add to CI:

```yaml
- name: Living Self P0 regression
  run: python3 tests/regression_living_self_p0.py
```

- [ ] **Step 4: Run package and both P0 suites**

```bash
PYTHONPATH=core python3 -m pytest tests/test_v11_packaging.py -q
python3 tests/regression_p0.py
python3 tests/regression_living_self_p0.py
```

Expected: clean package passes, legacy P0 `8/8`, Living Self P0 `10/10`.

- [ ] **Step 5: Commit**

```bash
git add \
  tests/test_v11_packaging.py \
  tests/regression_living_self_p0.py \
  pyproject.toml \
  .github/workflows/ci.yml \
  docs/ci.example.yml
git commit -m "test: enforce Living Self release gates"
```

### Task 19: Browser acceptance, performance, documentation, and version

**Files:**
- Create: `tests/browser_v11_acceptance.py`
- Modify: `core/VERSION`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/PRODUCT.md`
- Modify: `docs/PRIVACY.md`
- Modify: `docs/ROADMAP.md`
- Modify: `tests/test_version.py`

- [ ] **Step 1: Add failing version and documentation tests**

```python
def test_release_version_is_consistent():
    root = Path(__file__).resolve().parents[1]
    version = (root / "core/VERSION").read_text().strip()
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert version == "1.1.0"
    assert pyproject["project"]["version"] == version
    assert "Living Self" in (root / "README.md").read_text()
```

Browser acceptance launches a temporary real server and checks:

```python
assert page.locator('[data-view="home"]').is_visible()
assert await page.locator("body").evaluate(
    "(body) => body.scrollWidth <= document.documentElement.clientWidth"
)
assert console_errors == []
```

It then:

1. opens each of seven modules;
2. verifies deep link, refresh, back, and forward;
3. rapidly switches pages while one API is delayed and verifies stale response cannot overwrite;
4. opens a memory detail;
5. confirms and corrects a Claim;
6. refreshes and restarts the service, then verifies persistence;
7. restores a Living Self version;
8. previews Context, removes an allowed item, and compiles by preview ID and hash;
9. verifies clipboard copy alone does not mark consumed;
10. explicitly consumes it and records an outcome;
11. verifies Trust reflects the change;
12. exercises dialog focus entry, Tab trap, Escape close, inert background, and focus return;
13. rejects malicious Host, wrong-port Origin, wrong Content-Type, missing request ID, missing idempotency key, stale expected version, and oversized body;
14. runs at 1512×982;
15. runs at 390×844 with 44px touch targets and 16px inputs;
16. runs with reduced motion;
17. verifies CSP lacks `unsafe-inline` and responses use `Cache-Control: no-store`;
18. records API response timing.

- [ ] **Step 2: Run and confirm red**

```bash
PYTHONPATH=core python3 -m pytest tests/test_version.py -q
python3 tests/browser_v11_acceptance.py
```

Expected: version remains 1.0.0 and browser test exposes incomplete flows until Step 3.

- [ ] **Step 3: Complete docs, browser flow, and version bump**

Set:

```text
core/VERSION = 1.1.0
pyproject.toml project.version = 1.1.0
```

Document:

- seven-module product;
- Claim, Living Self, Judgment, Context, Outcome layers;
- correction and version behavior;
- privacy labels;
- exact migration commands;
- rollback;
- production health commands;
- clean installation;
- v1.0 compatibility.

The browser test asserts target performance with seeded 470,000-row SQLite data:

```text
home P95 < 500ms
memory list P95 < 800ms
memory detail P95 < 300ms
self P95 < 500ms
context preview P95 < 5s
initial JSON < 1MB
```

If the test machine cannot sustain the target, capture actual timings and fix query/read-model behavior. Do not weaken thresholds without an evidence-backed spec amendment.

- [ ] **Step 4: Run full pre-production verification**

```bash
PYTHONPATH=core python3 -m pytest -q
python3 tests/regression_p0.py
python3 tests/regression_living_self_p0.py
python3 tests/browser_v11_acceptance.py
python3 scripts/private_scan.py .
git diff --check
```

Expected:

- all pytest tests pass;
- legacy P0 `8/8`;
- Living Self P0 `10/10`;
- browser acceptance passes desktop and mobile;
- `private_scan=ok`;
- no diff errors.

- [ ] **Step 5: Commit**

```bash
git add \
  core/VERSION \
  pyproject.toml \
  README.md \
  docs/ARCHITECTURE.md \
  docs/PRODUCT.md \
  docs/PRIVACY.md \
  docs/ROADMAP.md \
  tests/test_version.py \
  tests/browser_v11_acceptance.py
git commit -m "release: prepare Immortal Memory v1.1.0"
```

### Task 20: Production readiness and isolated migration rehearsal

**Files:**
- Create locally: `tests/evidence-v11/`
- Do not commit private evidence.

- [ ] **Step 0: Reconcile live and public code before migration**

Create a private report that classifies every file:

```bash
diff -qr \
  /Users/example/.codex/skills/immortal \
  /Users/example/.config/superpowers/worktrees/immortal-memory/living-self-v1.1.0/core
```

For each live-only or differing file, record:

- public-safe capability to reimplement;
- private/local customization to preserve outside Git;
- obsolete behavior to remove;
- configuration or data that must never be copied.

Do not overwrite live customizations until every difference has an explicit disposition and rollback copy.

- [ ] **Step 1: Capture authoritative pre-migration evidence**

Run:

```bash
python3 /Users/example/.codex/skills/immortal/immortal.py daily-status
python3 /Users/example/.codex/skills/immortal/immortal.py backup-status --verify
python3 /Users/example/.codex/skills/immortal/immortal.py health --max-age-hours 72
python3 /Users/example/.codex/skills/immortal/immortal.py doctor
launchctl print "gui/$(id -u)/com.blake.immortal.daily-backup"
```

Record command, exit code, timestamp, stdout digest, and relevant file hashes under a private evidence directory. Do not proceed with destructive migration if external restore verification is absent.

Current known precondition failures from 2026-07-19 read-only review:

- `health` exits 1;
- `doctor` exits 1;
- state reports `feishu mirror inventory failed`;
- latest backup is inside the vault or same disk;
- JSONL and SQLite differ by 1,200 IDs.

Re-run and determine whether each is current, stale, or repaired. Clear stale state only through the owning command path. No production switch while index parity or external backup remains false.

- [ ] **Step 2: Create and verify an external portable backup**

Run the current live v1.0 backup to an external destination. Verify manifest file count, every hash, storage classification, and restore check. Preserve the v1.0 install package and LaunchAgent files as rollback artifacts.

Expected:

```text
verification.ok = true
restore_check.ok = true
storage.location = external_disk
```

If no external volume or separately protected destination is available, stop production migration after completing all code, test, and dry-run work. Do not downgrade this requirement to same-disk success.

- [ ] **Step 3: Dry-run v1.1 migration against a copy**

Copy the portable backup into an isolated temporary HOME. Install the built v1.1 wheel there, run:

```bash
immortal-memory claims-migrate --json
immortal-memory profile-attribution-audit
immortal-memory profile-nuwa
immortal-memory cards build
immortal-memory agent-context "评审 Immortal v1.1 上线" --print
```

Verify raw hashes match the pre-migration manifest and all new derived files exist.

- [ ] **Step 4: Complete a v1.0 rollback rehearsal in the isolated HOME**

Install v1.1 into the isolated HOME, generate Claim, Living Self, Judgment, Context, and Outcome events, then reinstall the preserved v1.0 package. Verify:

- raw hashes are unchanged;
- v1.0 CLI, preflight, health, agent-context, and legacy page still run;
- v1.1 event directories remain preserved in the private backup;
- reinstalling v1.1 can replay them again.

- [ ] **Step 5: Produce the production readiness report**

The report must contain:

- live/public file disposition;
- current health and doctor status;
- JSONL/SQLite parity;
- external backup manifest and restore evidence;
- isolated v1.1 migration evidence;
- isolated v1.0 rollback evidence;
- exact feature-branch commit and candidate asset SHA256;
- remaining blockers.

Do not install the feature-branch candidate into live production. Production must consume the final asset rebuilt from the merged main commit in Task 21.

### Task 21: Main-commit asset, production switch, and desensitized GitHub release

**Files:**
- Create: `docs/releases/v1.1.0.md`
- Create locally: sanitized release archive

- [ ] **Step 1: Write release notes from verified evidence**

Release notes include:

- user-visible Living Self changes;
- real Judgment and Context outcome loop;
- seven-module dashboard;
- v1.0 compatibility;
- migration and rollback;
- exact test counts;
- known boundaries;
- no private production numbers or identities.

- [ ] **Step 2: Run staged privacy and release consistency checks**

```bash
git status --short
git diff origin/main...HEAD --stat
git diff origin/main...HEAD
python3 scripts/private_scan.py .
PYTHONPATH=core python3 -m pytest -q
python3 tests/regression_p0.py
python3 tests/regression_living_self_p0.py
python3 tests/browser_v11_acceptance.py
```

Build a branch candidate only for CI and inspection, not for production installation. Inspect wheel and source archive contents. Verify no:

- `~/.immortal`;
- absolute private paths;
- names from private scan;
- `open_id`;
- tokens or cookies;
- session or customer content;
- test evidence directories.

- [ ] **Step 3: Commit final release notes**

```bash
git add docs/releases/v1.1.0.md
git commit -m "docs: add Immortal Memory v1.1.0 release notes"
```

- [ ] **Step 4: Push the feature branch and open a ready PR**

```bash
git push -u origin codex/living-self-v1.1.0
gh pr create \
  --repo HeiGeAi/immortal-memory \
  --base main \
  --head codex/living-self-v1.1.0 \
  --title "release: Immortal Memory v1.1.0 Living Self" \
  --body-file docs/releases/v1.1.0.md
```

Wait for Python 3.9–3.12 CI, legacy P0, Living Self P0, privacy scan, and packaging checks. Fix failures with new reviewed commits.

- [ ] **Step 5: Merge only after remote checks pass**

Use a normal PR merge. Fetch and verify the resulting main commit:

```bash
git fetch origin
git log -1 --oneline origin/main
```

- [ ] **Step 6: Build the single final asset from a clean main clone**

Create a fresh clone at the exact `origin/main` merge commit. Build once:

```bash
python3 -m build
shasum -a 256 dist/*
```

Run all tests, both P0 suites, browser acceptance, command closure, migration, rollback, strict export/restore, and recursive privacy scan against this clone and these exact assets. Install the wheel into a fresh venv and verify every registered command. Record commit SHA, asset member list, asset SHA256, Python versions, and results.

- [ ] **Step 7: Install the exact final wheel into production**

Use the tested main-commit wheel, not a source copy. Preserve v1.0 install and LaunchAgent rollback artifacts. Install, then verify:

```bash
python3 /Users/example/.codex/skills/immortal/immortal.py --version
```

Expected: `1.1.0`.

Rebuild only derived layers. Compare pre/post raw hashes. Start the service and validate:

```text
GET /readyz
GET /api/v2/home
GET /api/v2/memories?limit=10
GET /api/v2/self
GET /api/v2/judgments
GET /api/v2/contexts
GET /api/v2/trust
GET /api/v2/system
```

Each endpoint returns real data or a truthful empty state.

- [ ] **Step 8: Execute reversible live interaction and browser acceptance**

Create one clearly named restricted test Claim, confirm it, correct it, refresh, verify history, restore the previous Living Self version, compile/consume a test Context, and record an Outcome. Use normal events to retire the test namespace.

Run desktop and mobile browser acceptance against `http://127.0.0.1:8765/`. Repeat joint health checks and verify:

- raw hashes unchanged;
- index parity;
- scheduler loaded;
- external backup remains verifiable;
- service ready;
- new APIs respond;
- logs contain no credential or private body;
- v1.0 rollback package remains available.

If any P0 fails, roll back before continuing. Do not tag or publish a failed production asset.

- [ ] **Step 9: Tag the production-verified main merge commit**

```bash
git tag -a v1.1.0 origin/main -m "Immortal Memory v1.1.0"
git push origin v1.1.0
```

Verify:

```bash
git rev-parse v1.1.0^{}
git rev-parse origin/main
```

Expected: identical SHAs.

- [ ] **Step 10: Create and verify GitHub Release**

```bash
gh release create v1.1.0 \
  --repo HeiGeAi/immortal-memory \
  --title "Immortal Memory v1.1.0" \
  --notes-file docs/releases/v1.1.0.md \
  dist/immortal_memory-1.1.0-py3-none-any.whl \
  dist/immortal_memory-1.1.0.tar.gz
```

Verify release tag, asset names, checksums, version output, and that the live installation reports `1.1.0`.

Download the Release assets into a clean directory, verify SHA256 equals the production-installed final assets, rescan archives, install the wheel in a new venv, and execute the command-closure smoke again.

- [ ] **Step 11: Run final requirement-by-requirement audit**

For each of the 12 acceptance statements in the design spec, record:

- requirement;
- authoritative evidence;
- current status;
- exact command, API, browser action, file, commit, CI job, or release URL.

Any missing or indirect evidence keeps the release incomplete.

- [ ] **Step 12: Send final Feishu delivery**

Send:

- concise completion notice;
- GitHub PR and Release URLs;
- production dashboard URL;
- final acceptance report body;
- sanitized release archive;
- known attention items;
- rollback path.

Verify every send returns a `message_id`.

## Final Verification Commands

Run from the clean final main commit:

```bash
PYTHONPATH=core python3 -m pytest -q
python3 tests/regression_p0.py
python3 tests/regression_living_self_p0.py
python3 tests/browser_v11_acceptance.py
python3 scripts/private_scan.py .
python3 -m build
git status --short
git rev-parse HEAD
git rev-parse v1.1.0^{}
```

Run against production:

```bash
python3 /Users/example/.codex/skills/immortal/immortal.py --version
python3 /Users/example/.codex/skills/immortal/immortal.py daily-status
python3 /Users/example/.codex/skills/immortal/immortal.py backup-status --verify
python3 /Users/example/.codex/skills/immortal/immortal.py health --max-age-hours 72
python3 /Users/example/.codex/skills/immortal/immortal.py doctor
launchctl print "gui/$(id -u)/com.blake.immortal.daily-backup"
curl -fsS http://127.0.0.1:8765/readyz
```

The branch is complete only when all commands pass or the acceptance report explicitly identifies a non-release-blocking attention item already allowed by the design.
