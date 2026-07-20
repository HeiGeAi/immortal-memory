import hashlib
import json
import shutil
from pathlib import Path

import pytest

import immortal
from claim_store import ClaimStore
from model_migration import (
    MigrationError,
    legacy_claim_id,
    migrate_legacy_profile,
)
from model_types import new_claim


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def seed_evidence(vault: Path, *evidence_ids: str) -> None:
    write_jsonl(
        vault / "index.jsonl",
        [
            {
                "id": evidence_id,
                "timestamp": "2026-07-01T00:00:00+00:00",
                "source": "codex",
                "content": "authoritative evidence " + evidence_id,
            }
            for evidence_id in evidence_ids
        ],
    )


def legacy_row(
    memory_id: str,
    evidence_id: str,
    *,
    statement: str = "偏好短段落",
    speaker: str = "owner",
    **extra,
) -> dict:
    return {
        "memory_id": memory_id,
        "statement": statement,
        "focus": "self_profile",
        "memory_type": "preference",
        "evidence_ids": [evidence_id],
        "speaker": speaker,
        "sensitivity": "internal",
        **extra,
    }


def current_rows(vault: Path) -> list[dict]:
    path = vault / "model" / "claims" / "current.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def event_rows(vault: Path) -> list[dict]:
    path = vault / "model" / "claims" / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_migration_is_idempotent_deterministic_and_keeps_legacy_file(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    legacy = tmp_path / "reviewed" / "profile_memories.jsonl"
    write_jsonl(legacy, [legacy_row("mem-1", "ev-1")])
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()

    first = migrate_legacy_profile(tmp_path)
    second = migrate_legacy_profile(tmp_path)

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["skipped"] == 1
    assert first["confirmed"] == second["confirmed"] == 0
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before
    assert len(event_rows(tmp_path)) == 1
    event = event_rows(tmp_path)[0]
    assert event["event_id"].startswith("evt_migrate_")
    assert event["idempotency_key"].startswith("claim:public:v1:")
    assert event["actor"] == {"kind": "migration", "id": "legacy-profile-v1"}
    assert event["migration_source"].startswith(
        "reviewed/profile_memories.jsonl#sha256:"
    )

    clone = tmp_path / "clone"
    seed_evidence(clone, "ev-1")
    write_jsonl(
        clone / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    migrate_legacy_profile(clone)
    comparable = {
        key: value
        for key, value in event_rows(tmp_path)[0].items()
        if key not in {"seq", "occurred_at"}
    }
    clone_comparable = {
        key: value
        for key, value in event_rows(clone)[0].items()
        if key not in {"seq", "occurred_at"}
    }
    assert clone_comparable == comparable


def test_other_speaker_stays_candidate_quoted_external_view(tmp_path):
    seed_evidence(tmp_path, "ev-2")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [
            legacy_row(
                "mem-2",
                "ev-2",
                statement="他认为用户做事很激进",
                speaker="other",
            )
        ],
    )

    migrate_legacy_profile(tmp_path)

    row = current_rows(tmp_path)[0]
    assert row["speaker"]["kind"] == "other"
    assert row["source_kind"] == "quoted"
    assert row["status"] == "candidate"
    assert row["claim_type"] == "external_view"


@pytest.mark.parametrize(
    ("category", "expected_speaker", "expected_source"),
    [
        ("self_direct", "owner", "direct"),
        ("about_owner_from_other", "other", "quoted"),
    ],
)
def test_legacy_attribution_category_controls_speaker_without_explicit_field(
    tmp_path,
    category,
    expected_speaker,
    expected_source,
):
    seed_evidence(tmp_path, "ev-attribution")
    row = legacy_row("mem-attribution", "ev-attribution")
    row.pop("speaker")
    row["attribution"] = {
        "category": category,
        "actor": "person-1" if expected_speaker == "other" else "owner",
    }
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [row],
    )

    migrate_legacy_profile(tmp_path)

    claim = current_rows(tmp_path)[0]
    assert claim["speaker"]["kind"] == expected_speaker
    assert claim["source_kind"] == expected_source
    assert claim["claim_type"] == (
        "external_view" if expected_speaker == "other" else "preference"
    )


def test_legacy_nuwa_accepted_is_only_an_inferred_candidate(tmp_path):
    seed_evidence(tmp_path, "ev-nuwa")
    (tmp_path / "profile_nuwa.json").write_text(
        json.dumps(
            {
                "mental_models": [
                    {
                        "id": "legacy-1",
                        "title": "证据优先",
                        "summary": "先核对证据再下结论",
                        "status": "accepted",
                        "evidence_ids": ["ev-nuwa"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = migrate_legacy_profile(tmp_path)

    assert report["created"] == 1
    assert report["confirmed"] == 0
    claim = current_rows(tmp_path)[0]
    assert claim["status"] == "candidate"
    assert claim["source_kind"] == "inferred"
    assert claim["claim_type"] == "lesson"


def test_missing_evidence_is_counted_and_never_forged_into_claim(tmp_path):
    seed_evidence(tmp_path, "ev-present")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-missing", "ev-does-not-exist")],
    )

    report = migrate_legacy_profile(tmp_path)

    assert report["created"] == 0
    assert report["source_broken"] == 1
    assert report["broken_ids"] == ["ev-does-not-exist"]
    assert not (tmp_path / "model" / "claims" / "events.jsonl").exists()


@pytest.mark.parametrize(
    "extra",
    [
        {"pinned": True},
        {"origin": "fallback"},
        {"focus": "reference_material"},
    ],
)
def test_pinned_fallback_and_reference_rows_are_not_user_declared(
    tmp_path,
    extra,
):
    seed_evidence(tmp_path, "ev-static")
    row = legacy_row("mem-static", "ev-static")
    row.update(extra)
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [row],
    )

    report = migrate_legacy_profile(tmp_path)

    assert report["created"] == 0
    assert report["excluded"] == 1
    assert not (tmp_path / "model" / "claims" / "events.jsonl").exists()


def test_dry_run_has_zero_writes_and_reports_real_counts(tmp_path):
    seed_evidence(tmp_path, "ev-1", "ev-2")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1"), legacy_row("mem-2", "ev-2")],
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    report = migrate_legacy_profile(
        tmp_path,
        dry_run=True,
        checkpoint_every=1,
    )

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert report["created"] == 2
    assert report["dry_run"] is True
    assert before == after
    assert not (tmp_path / "model").exists()


def test_interrupted_run_restarts_from_deterministic_event_without_duplicates(
    tmp_path,
    monkeypatch,
):
    seed_evidence(tmp_path, "ev-1", "ev-2", "ev-3")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [
            legacy_row("mem-1", "ev-1"),
            legacy_row("mem-2", "ev-2"),
            legacy_row("mem-3", "ev-3"),
        ],
    )
    import model_migration

    def interrupt(created):
        if created == 2:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(model_migration, "after_claim_commit", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        migrate_legacy_profile(tmp_path, checkpoint_every=1)

    monkeypatch.setattr(model_migration, "after_claim_commit", lambda created: None)
    resumed = migrate_legacy_profile(tmp_path, checkpoint_every=1)
    final = migrate_legacy_profile(tmp_path, checkpoint_every=1)

    assert resumed["created"] == 1
    assert resumed["checkpoint_resumed"] is True
    assert final["created"] == 0
    assert len(event_rows(tmp_path)) == 3
    assert len({row["event_id"] for row in event_rows(tmp_path)}) == 3


def test_malformed_source_fails_before_any_migration_write(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    source = tmp_path / "reviewed" / "profile_memories.jsonl"
    source.parent.mkdir()
    source.write_text(
        json.dumps(legacy_row("mem-1", "ev-1"), ensure_ascii=False)
        + "\n"
        + "{bad json\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "malformed_legacy_source"
    assert not (tmp_path / "model").exists()


def test_unsafe_legacy_symlink_fails_closed(tmp_path):
    outside = tmp_path / "outside.jsonl"
    write_jsonl(outside, [legacy_row("mem-1", "ev-1")])
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    (reviewed / "profile_memories.jsonl").symlink_to(outside)

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "unsafe_path"
    assert not (tmp_path / "model").exists()


def test_malformed_current_state_fails_closed_without_repair(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    current = tmp_path / "model" / "claims" / "current.jsonl"
    current.parent.mkdir(parents=True)
    current.write_text("{bad current\n", encoding="utf-8")
    before = current.read_bytes()

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "malformed_current_state"
    assert current.read_bytes() == before
    assert not (tmp_path / "model" / "claims" / "events.jsonl").exists()


def test_checkpoint_configuration_and_source_change_fail_closed(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    source = tmp_path / "reviewed" / "profile_memories.jsonl"
    write_jsonl(source, [legacy_row("mem-1", "ev-1")])

    with pytest.raises(MigrationError) as invalid:
        migrate_legacy_profile(tmp_path, checkpoint_every=0)
    assert invalid.value.code == "invalid_checkpoint_every"

    migrate_legacy_profile(tmp_path, checkpoint_every=1)
    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(legacy_row("mem-2", "ev-1"), ensure_ascii=False) + "\n"
        )
    with pytest.raises(MigrationError) as changed:
        migrate_legacy_profile(tmp_path, checkpoint_every=1)
    assert changed.value.code == "migration_source_changed"


def test_checkpoint_progress_must_match_prepared_and_committed_claims(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    report = migrate_legacy_profile(tmp_path, checkpoint_every=1)
    checkpoint = Path(report["checkpoint"])
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    original = dict(payload)
    payload["candidate_ids"] = payload["candidate_ids"] + ["clm_unknown"]
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationError) as ahead:
        migrate_legacy_profile(tmp_path, checkpoint_every=1)
    assert ahead.value.code == "malformed_checkpoint"

    checkpoint.write_text(json.dumps(original), encoding="utf-8")
    shutil.rmtree(tmp_path / "model" / "claims")
    with pytest.raises(MigrationError) as missing:
        migrate_legacy_profile(tmp_path, checkpoint_every=1)
    assert missing.value.code == "checkpoint_state_mismatch"


def test_existing_deterministic_claim_id_requires_exact_migration_provenance(
    tmp_path,
):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    store = ClaimStore(tmp_path)
    forged = new_claim(
        statement="不同正文",
        source_kind="direct",
        evidence_ids=["ev-1"],
        now="1970-01-01T00:00:00+00:00",
    )
    forged["claim_id"] = legacy_claim_id("reviewed", "mem-1")
    store.create(
        forged,
        expected_revision=0,
        request_id="req-forged",
        idempotency_key="idem-forged",
        actor={"kind": "owner", "id": "owner"},
        reason="not a migration",
    )

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "migration_state_conflict"
    assert len(event_rows(tmp_path)) == 1


def test_tampered_first_migration_digest_conflicts_after_legal_transition(
    tmp_path,
):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    migrate_legacy_profile(tmp_path)
    store = ClaimStore(tmp_path)
    claim_id = current_rows(tmp_path)[0]["claim_id"]
    store.transition(
        claim_id,
        "confirmed",
        reason="owner confirmed",
        expected_revision=1,
        request_id="req-confirm-before-tamper",
        idempotency_key="idem-confirm-before-tamper",
        actor={"kind": "owner", "id": "owner"},
    )
    events_path = tmp_path / "model" / "claims" / "events.jsonl"
    rows = event_rows(tmp_path)
    rows[0]["migration_source"] = (
        "reviewed/profile_memories.jsonl#sha256:"
        + ("0" * 64)
        + ";index_sha256:"
        + ("1" * 64)
    )
    write_jsonl(events_path, rows)

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "migration_state_conflict"


def test_tampered_first_migration_index_digest_conflicts(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    migrate_legacy_profile(tmp_path)
    events_path = tmp_path / "model" / "claims" / "events.jsonl"
    rows = event_rows(tmp_path)
    prefix, _index_digest = rows[0]["migration_source"].split(
        ";index_sha256:",
        1,
    )
    rows[0]["migration_source"] = prefix + ";index_sha256:" + ("f" * 64)
    write_jsonl(events_path, rows)

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "migration_state_conflict"


@pytest.mark.parametrize("status", ["confirmed", "rejected"])
def test_rerun_accepts_legal_post_migration_transition(tmp_path, status):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    migrate_legacy_profile(tmp_path)
    store = ClaimStore(tmp_path)
    claim_id = current_rows(tmp_path)[0]["claim_id"]
    store.transition(
        claim_id,
        status,
        reason="owner reviewed migrated candidate",
        expected_revision=1,
        request_id="req-review-" + status,
        idempotency_key="idem-review-" + status,
        actor={"kind": "owner", "id": "owner"},
    )

    rerun = migrate_legacy_profile(tmp_path)

    assert rerun["created"] == 0
    assert rerun["skipped"] == 1
    assert ClaimStore(tmp_path).get(claim_id)["status"] == status


def test_rerun_accepts_legal_correction_after_migration(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    migrate_legacy_profile(tmp_path)
    store = ClaimStore(tmp_path)
    claim_id = current_rows(tmp_path)[0]["claim_id"]
    store.transition(
        claim_id,
        "confirmed",
        reason="owner confirmed",
        expected_revision=1,
        request_id="req-confirm-before-correct",
        idempotency_key="idem-confirm-before-correct",
        actor={"kind": "owner", "id": "owner"},
    )
    replacement = store.correct(
        claim_id,
        "偏好短句和短段落",
        reason="owner corrected precision",
        expected_revision=2,
        request_id="req-correct-migrated",
        idempotency_key="idem-correct-migrated",
        actor={"kind": "owner", "id": "owner"},
    )

    rerun = migrate_legacy_profile(tmp_path)

    assert rerun["created"] == 0
    assert rerun["skipped"] == 1
    assert ClaimStore(tmp_path).get(claim_id)["status"] == "superseded"
    assert ClaimStore(tmp_path).get(replacement["claim_id"])["status"] == "confirmed"


def test_rerun_accepts_reconsidered_migrated_claim(tmp_path):
    seed_evidence(tmp_path, "ev-1", "ev-new")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    migrate_legacy_profile(tmp_path)
    store = ClaimStore(tmp_path)
    claim_id = current_rows(tmp_path)[0]["claim_id"]
    store.transition(
        claim_id,
        "rejected",
        reason="owner rejected",
        expected_revision=1,
        request_id="req-reject-before-reconsider",
        idempotency_key="idem-reject-before-reconsider",
        actor={"kind": "owner", "id": "owner"},
    )
    store.reconsider(
        claim_id,
        evidence_ids=["ev-new"],
        reason="new evidence",
        expected_revision=2,
        request_id="req-reconsider-migrated",
        idempotency_key="idem-reconsider-migrated",
        actor={"kind": "owner", "id": "owner"},
    )

    rerun = migrate_legacy_profile(tmp_path)

    assert rerun["created"] == 0
    assert rerun["skipped"] == 1
    assert ClaimStore(tmp_path).get(claim_id)["status"] == "candidate"
    assert ClaimStore(tmp_path).get(claim_id)["revision"] == 3


def test_bad_sqlite_catalog_becomes_stable_migration_and_cli_json_error(
    tmp_path,
    capsys,
):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    (tmp_path / "search_index.db").write_bytes(b"not sqlite")

    with pytest.raises(MigrationError) as direct:
        migrate_legacy_profile(tmp_path)
    assert direct.value.code == "database_untrusted"

    code = immortal.main(
        ["claims-migrate", "--vault-dir", str(tmp_path), "--json"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code != 0
    assert captured.err == ""
    assert payload["ok"] is False
    assert payload["error_code"] == "database_untrusted"


def test_checkpoint_allows_previously_broken_evidence_to_be_repaired(tmp_path):
    seed_evidence(tmp_path, "ev-present")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [
            legacy_row("mem-missing", "ev-repaired"),
            legacy_row("mem-present", "ev-present"),
        ],
    )

    first = migrate_legacy_profile(tmp_path, checkpoint_every=1)
    assert first["created"] == 1
    assert first["source_broken"] == 1

    seed_evidence(tmp_path, "ev-present", "ev-repaired")
    repaired = migrate_legacy_profile(tmp_path, checkpoint_every=1)

    assert repaired["created"] == 1
    assert repaired["source_broken"] == 0
    assert len(event_rows(tmp_path)) == 2
    checkpoint = json.loads(
        Path(repaired["checkpoint"]).read_text(encoding="utf-8")
    )
    assert checkpoint["candidate_ids"] == sorted(
        [
            legacy_claim_id("reviewed", "mem-missing"),
            legacy_claim_id("reviewed", "mem-present"),
        ]
    )
    assert checkpoint["committed_claim_ids"] == checkpoint["candidate_ids"]


def test_evidence_repair_works_when_initial_batch_has_no_commits(tmp_path):
    seed_evidence(tmp_path)
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-missing", "ev-missing")],
    )

    first = migrate_legacy_profile(tmp_path)
    seed_evidence(tmp_path, "ev-missing")
    repaired = migrate_legacy_profile(tmp_path)

    assert first["created"] == 0
    assert first["source_broken"] == 1
    assert repaired["created"] == 1
    assert repaired["source_broken"] == 0


def test_checkpoint_rejects_unknown_or_uncommitted_claim_ids(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    report = migrate_legacy_profile(tmp_path)
    checkpoint = Path(report["checkpoint"])
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["committed_claim_ids"].append("clm_poisoned")
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "malformed_checkpoint"


@pytest.mark.parametrize("digest_field", ["source_digest", "index_digest"])
def test_checkpoint_rejects_malformed_digest_contract(tmp_path, digest_field):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    report = migrate_legacy_profile(tmp_path)
    checkpoint = Path(report["checkpoint"])
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload[digest_field] = "not-a-sha256"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code in {
        "malformed_checkpoint",
        "migration_source_changed",
    }


def test_checkpoint_rejects_unbound_well_formed_index_digest(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    report = migrate_legacy_profile(tmp_path)
    checkpoint = Path(report["checkpoint"])
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["index_digest"] = "f" * 64
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "malformed_checkpoint"


def test_checkpoint_rejects_extra_observed_index_digest(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    report = migrate_legacy_profile(tmp_path)
    checkpoint = Path(report["checkpoint"])
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["observed_index_digests"].append("f" * 64)
    payload["observed_index_digests"].sort()
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "malformed_checkpoint"


def test_checkpoint_rejects_synchronized_top_and_observed_poisoning(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    report = migrate_legacy_profile(tmp_path)
    checkpoint = Path(report["checkpoint"])
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["index_digest"] = "f" * 64
    payload["observed_index_digests"].append("f" * 64)
    payload["observed_index_digests"].sort()
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path)

    assert captured.value.code == "malformed_checkpoint"


def test_unrelated_index_growth_preserves_historical_claim_binding(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )
    first = migrate_legacy_profile(tmp_path)
    checkpoint = Path(first["checkpoint"])
    original = json.loads(checkpoint.read_text(encoding="utf-8"))
    original_claim_digest = next(
        iter(original["committed_index_digests"].values())
    )

    seed_evidence(tmp_path, "ev-1", "ev-unrelated")
    second = migrate_legacy_profile(tmp_path)
    third = migrate_legacy_profile(tmp_path)
    updated = json.loads(checkpoint.read_text(encoding="utf-8"))

    assert second["created"] == third["created"] == 0
    assert second["index_digest"] != original_claim_digest
    assert updated["index_digest"] == original_claim_digest
    assert updated["observed_index_digests"] == [original_claim_digest]
    assert set(updated["committed_index_digests"].values()) == {
        original_claim_digest
    }


@pytest.mark.parametrize("changed_source", ["reviewed", "nuwa", "index"])
def test_source_replacement_after_snapshot_fails_before_first_write(
    tmp_path,
    monkeypatch,
    changed_source,
):
    seed_evidence(tmp_path, "ev-reviewed", "ev-nuwa")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-reviewed")],
    )
    (tmp_path / "profile_nuwa.json").write_text(
        json.dumps(
            {
                "mental_models": [
                    {
                        "id": "model-1",
                        "summary": "证据优先",
                        "status": "accepted",
                        "evidence_ids": ["ev-nuwa"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    import model_migration

    def replace_source():
        target = {
            "reviewed": tmp_path / "reviewed" / "profile_memories.jsonl",
            "nuwa": tmp_path / "profile_nuwa.json",
            "index": tmp_path / "index.jsonl",
        }[changed_source]
        replacement = target.with_name(target.name + ".replacement")
        replacement.write_bytes(target.read_bytes() + b" ")
        replacement.replace(target)

    monkeypatch.setattr(
        model_migration,
        "after_snapshot_prepared",
        replace_source,
        raising=False,
    )
    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path, checkpoint_every=1)

    assert captured.value.code == "source_changed"
    assert not (tmp_path / "model" / "claims" / "events.jsonl").exists()


def test_source_replacement_during_batch_stops_before_next_claim(
    tmp_path,
    monkeypatch,
):
    seed_evidence(tmp_path, "ev-1", "ev-2")
    source = tmp_path / "reviewed" / "profile_memories.jsonl"
    write_jsonl(
        source,
        [legacy_row("mem-1", "ev-1"), legacy_row("mem-2", "ev-2")],
    )
    import model_migration

    def replace_after_first(created):
        if created != 1:
            return
        replacement = source.with_name(source.name + ".replacement")
        replacement.write_bytes(source.read_bytes() + b" ")
        replacement.replace(source)

    monkeypatch.setattr(model_migration, "after_claim_commit", replace_after_first)
    with pytest.raises(MigrationError) as captured:
        migrate_legacy_profile(tmp_path, checkpoint_every=1)

    assert captured.value.code == "source_changed"
    assert len(event_rows(tmp_path)) == 1


def test_event_and_report_retain_bound_source_digests(tmp_path):
    seed_evidence(tmp_path, "ev-1")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-1", "ev-1")],
    )

    report = migrate_legacy_profile(tmp_path)

    assert len(report["source_digest"]) == 64
    assert len(report["index_digest"]) == 64
    event = event_rows(tmp_path)[0]
    reviewed_digest = report["source_digests"][
        "reviewed/profile_memories.jsonl"
    ]
    assert "sha256:" + reviewed_digest in event["migration_source"]
    assert "index_sha256:" + report["index_digest"] in event["migration_source"]


def test_cli_reports_json_and_nonzero_failure_truthfully(tmp_path, capsys):
    seed_evidence(tmp_path, "ev-cli")
    write_jsonl(
        tmp_path / "reviewed" / "profile_memories.jsonl",
        [legacy_row("mem-cli", "ev-cli")],
    )

    code = immortal.main(
        [
            "claims-migrate",
            "--vault-dir",
            str(tmp_path),
            "--dry-run",
            "--checkpoint-every",
            "1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["created"] == 1
    assert payload["dry_run"] is True

    unsafe = tmp_path / "unsafe"
    unsafe.symlink_to(tmp_path, target_is_directory=True)
    code = immortal.main(
        ["claims-migrate", "--vault-dir", str(unsafe), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["ok"] is False
    assert payload["error_code"] == "unsafe_path"
