import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from model_types import CONTEXT_SECTIONS


ACTOR = {"kind": "owner", "id": "owner"}
SOURCE_REVISION = {
    "claims_event_seq": 10,
    "living_self_version": "lsv_" + "a" * 32,
    "judgments_event_seq": 4,
    "compiler_version": "1.1.0",
    "policy_version": 1,
}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, *, seconds):
        self.value += timedelta(seconds=seconds)


def sections(*ids):
    result = {name: [] for name in CONTEXT_SECTIONS}
    result["verified_facts"] = [
        {
            "id": item_id,
            "summary": "raw private evidence must not be persisted",
            "privacy": "restricted",
        }
        for item_id in ids
    ]
    return result


def create_preview(store, *, suffix="one", ttl_seconds=300, selected=("item_1",)):
    return store.create_preview(
        task="评审技术方案",
        mode="reviewer",
        source_revision=SOURCE_REVISION,
        sections=sections(*selected),
        privacy_policy={"excluded_count": 1, "reasons": ["private"]},
        ttl_seconds=ttl_seconds,
        expected_version=0,
        request_id="req_preview_" + suffix,
        idempotency_key="idem_preview_" + suffix,
        actor=ACTOR,
        reason="preview requested",
    )


def compile_preview(store, preview, *, suffix="one"):
    return store.begin_compile(
        preview["preview_id"],
        preview_hash=preview["preview_hash"],
        source_revision=preview["source_revision"],
        excluded_item_ids=[],
        expected_version=preview["stream_version"],
        request_id="req_compile_" + suffix,
        idempotency_key="idem_compile_" + suffix,
        actor=ACTOR,
        reason="preview approved",
    )


def test_empty_initialization_is_side_effect_free(tmp_path):
    from context_store import ContextStore

    store = ContextStore(tmp_path)

    assert store.list() == []
    assert not (tmp_path / "contexts").exists()


def test_preview_persists_exact_revision_hash_ttl_and_safe_selection(tmp_path):
    from context_store import ContextStore

    clock = Clock()
    store = ContextStore(tmp_path, clock=clock)
    preview = create_preview(store)

    assert preview["lifecycle_status"] == "preview"
    assert preview["availability_status"] == "active"
    assert preview["revision"] == 1
    assert preview["stream_version"] == 1
    assert preview["source_revision"] == SOURCE_REVISION
    assert preview["selection"]["selected_item_ids"] == ["item_1"]
    assert set(preview["selection"]["section_item_ids"]) == CONTEXT_SECTIONS
    assert preview["preview_hash"].startswith("sha256:")
    assert datetime.fromisoformat(preview["expires_at"]) == (
        clock.value + timedelta(seconds=300)
    )
    restarted = ContextStore(tmp_path, clock=clock)
    assert restarted.get(preview["preview_id"])["preview_hash"] == preview[
        "preview_hash"
    ]
    assert "raw private evidence" not in store.events.path.read_text(
        encoding="utf-8"
    )
    preview_body = json.loads(
        (store.previews_dir / (preview["preview_id"] + ".json")).read_text(
            encoding="utf-8"
        )
    )
    assert (
        preview_body["sections"]["verified_facts"][0]["summary"]
        == "raw private evidence must not be persisted"
    )


def test_expired_preview_cannot_compile_and_availability_is_dynamic(tmp_path):
    from context_store import ContextStore, ContextStoreError

    clock = Clock()
    store = ContextStore(tmp_path, clock=clock)
    preview = create_preview(store, ttl_seconds=1)
    clock.advance(seconds=2)

    assert store.get(preview["preview_id"])["availability_status"] == "expired"
    with pytest.raises(ContextStoreError) as failure:
        compile_preview(store, preview)
    assert failure.value.code == "stale_preview"


@pytest.mark.parametrize("field", ["preview_hash", "source_revision", "excluded"])
def test_compile_rejects_stale_or_invalid_preview_inputs(tmp_path, field):
    from context_store import ContextStore, ContextStoreError

    clock = Clock()
    store = ContextStore(tmp_path, clock=clock)
    preview = create_preview(store)
    kwargs = {
        "preview_hash": preview["preview_hash"],
        "source_revision": preview["source_revision"],
        "excluded_item_ids": [],
    }
    if field == "preview_hash":
        kwargs[field] = "sha256:" + "0" * 64
    elif field == "source_revision":
        kwargs[field] = dict(SOURCE_REVISION, claims_event_seq=11)
    else:
        kwargs["excluded_item_ids"] = ["not_selected"]

    with pytest.raises(ContextStoreError) as failure:
        store.begin_compile(
            preview["preview_id"],
            expected_version=1,
            request_id="req_stale_" + field,
            idempotency_key="idem_stale_" + field,
            actor=ACTOR,
            reason="invalid compile",
            **kwargs,
        )
    assert failure.value.code == "stale_preview"


def test_compiled_context_consumes_and_expired_consumed_accepts_outcome(
    tmp_path,
):
    from context_store import ContextStore

    clock = Clock()
    store = ContextStore(tmp_path, clock=clock)
    preview = create_preview(store, ttl_seconds=1)
    compiled = compile_preview(store, preview)
    consumed = store.consume(
        compiled["context_id"],
        expected_version=2,
        request_id="req_consume",
        idempotency_key="idem_consume",
        actor=ACTOR,
        reason="Agent accepted Context",
    )
    clock.advance(seconds=2)
    outcome = store.mark_outcome_recorded(
        compiled["context_id"],
        expected_version=3,
        request_id="req_outcome",
        idempotency_key="idem_outcome",
        actor=ACTOR,
        reason="outcome linked",
    )

    assert consumed["lifecycle_status"] == "consumed"
    assert outcome["lifecycle_status"] == "outcome_recorded"
    assert outcome["availability_status"] == "expired"


def test_expired_compiled_context_cannot_be_consumed(tmp_path):
    from context_store import ContextStore, ContextStoreError

    clock = Clock()
    store = ContextStore(tmp_path, clock=clock)
    compiled = compile_preview(store, create_preview(store, ttl_seconds=1))
    clock.advance(seconds=2)

    with pytest.raises(ContextStoreError) as failure:
        store.consume(
            compiled["context_id"],
            expected_version=2,
            request_id="req_consume_late",
            idempotency_key="idem_consume_late",
            actor=ACTOR,
            reason="too late",
        )
    assert failure.value.code == "context_expired"


def test_illegal_lifecycle_and_revision_conflicts_fail_closed(tmp_path):
    from context_store import ContextStore, ContextStoreError

    store = ContextStore(tmp_path, clock=Clock())
    preview = create_preview(store)

    with pytest.raises(ContextStoreError) as lifecycle:
        store.consume(
            preview["preview_id"],
            expected_version=1,
            request_id="req_early",
            idempotency_key="idem_early",
            actor=ACTOR,
            reason="not compiled",
        )
    assert lifecycle.value.code == "invalid_transition"

    with pytest.raises(ContextStoreError) as revision:
        store.begin_compile(
            preview["preview_id"],
            preview_hash=preview["preview_hash"],
            source_revision=preview["source_revision"],
            excluded_item_ids=[],
            expected_version=99,
            request_id="req_revision",
            idempotency_key="idem_revision",
            actor=ACTOR,
            reason="wrong revision",
        )
    assert revision.value.code == "version_conflict"


def test_concurrent_same_idempotency_key_converges_to_one_preview(tmp_path):
    from context_store import ContextStore

    clock = Clock()
    store = ContextStore(tmp_path, clock=clock)
    results = []
    failures = []

    def worker():
        try:
            results.append(create_preview(store, suffix="shared"))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len({item["preview_id"] for item in results}) == 1
    assert len(store.list()) == 1
    assert store.events.watermark() == 1


def test_restart_repairs_current_view_from_authority_events(tmp_path):
    from context_store import ContextStore

    clock = Clock()
    store = ContextStore(tmp_path, clock=clock)
    preview = create_preview(store)
    store.current_path.unlink()

    repaired = ContextStore(tmp_path, clock=clock)

    assert repaired.get(preview["preview_id"])["based_on_event_seq"] == 1
    assert repaired.current_path.is_file()


def test_replay_rejects_record_that_does_not_match_authority_intent(tmp_path):
    from context_store import ContextStore
    from event_store import EventCorruption

    store = ContextStore(tmp_path, clock=Clock())
    create_preview(store)
    event = json.loads(store.events.path.read_text(encoding="utf-8"))
    event["payload"]["record"]["selection"]["section_item_ids"][
        "verified_facts"
    ].append("forged")
    event["payload"]["record"]["selection"]["selected_item_ids"].append("forged")
    store.events.path.write_text(
        json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store.current_path.unlink()

    with pytest.raises(EventCorruption) as failure:
        ContextStore(tmp_path, clock=Clock())
    assert failure.value.code == "invalid_context_event"


def test_committed_preview_cache_failure_recovers_on_idempotent_retry(
    tmp_path, monkeypatch
):
    import context_store as context_module

    store = context_module.ContextStore(tmp_path, clock=Clock())
    original = context_module.safe_atomic_write_text
    failed = {"value": False}

    def fail_preview_once(path, content):
        if path.parent.name == "previews" and not failed["value"]:
            failed["value"] = True
            raise OSError("injected preview cache failure")
        return original(path, content)

    monkeypatch.setattr(
        context_module, "safe_atomic_write_text", fail_preview_once
    )
    with pytest.raises(OSError, match="injected preview cache failure"):
        create_preview(store, suffix="cache-retry")
    assert store.events.watermark() == 1

    recovered = create_preview(store, suffix="cache-retry")

    assert store.events.watermark() == 1
    assert (
        store.previews_dir / (recovered["preview_id"] + ".json")
    ).is_file()


def test_same_idempotency_key_cannot_replace_reviewed_preview_body(tmp_path):
    from context_store import ContextStore, ContextStoreError

    store = ContextStore(tmp_path, clock=Clock())
    original = create_preview(store, suffix="body-binding")
    changed = sections("item_1")
    changed["verified_facts"][0]["summary"] = "different private body"

    with pytest.raises(ContextStoreError) as failure:
        store.create_preview(
            task="评审技术方案",
            mode="reviewer",
            source_revision=SOURCE_REVISION,
            sections=changed,
            privacy_policy={"excluded_count": 1, "reasons": ["private"]},
            ttl_seconds=300,
            expected_version=0,
            request_id="req_preview_body-binding",
            idempotency_key="idem_preview_body-binding",
            actor=ACTOR,
            reason="preview requested",
        )

    assert failure.value.code == "idempotency_conflict"
    body = json.loads(
        (store.previews_dir / (original["preview_id"] + ".json")).read_text(
            encoding="utf-8"
        )
    )
    assert (
        body["sections"]["verified_facts"][0]["summary"]
        == "raw private evidence must not be persisted"
    )


def test_future_authority_metadata_fails_closed(tmp_path):
    from context_store import ContextStore, ContextStoreError

    store = ContextStore(tmp_path, clock=Clock())
    preview = create_preview(store)
    earlier = Clock()
    earlier.value -= timedelta(seconds=1)

    with pytest.raises(ContextStoreError) as failure:
        ContextStore(tmp_path, clock=earlier).get(preview["preview_id"])
    assert failure.value.code == "future_timestamp"


def test_load_preview_body_is_hash_bound_and_returns_an_isolated_copy(tmp_path):
    from context_store import ContextStore

    store = ContextStore(tmp_path, clock=Clock())
    preview = create_preview(store)

    first = store.load_preview_body(
        preview["preview_id"], preview["preview_hash"]
    )
    first["sections"]["verified_facts"][0]["summary"] = "mutated caller copy"
    second = store.load_preview_body(
        preview["preview_id"], preview["preview_hash"]
    )

    assert (
        second["sections"]["verified_facts"][0]["summary"]
        == "raw private evidence must not be persisted"
    )


def test_load_preview_body_rejects_wrong_hash_missing_and_tampering(tmp_path):
    from context_store import ContextStore, ContextStoreError

    store = ContextStore(tmp_path, clock=Clock())
    preview = create_preview(store)

    with pytest.raises(ContextStoreError) as wrong_hash:
        store.load_preview_body(
            preview["preview_id"], "sha256:" + "0" * 64
        )
    assert wrong_hash.value.code == "stale_preview"

    cache = store.previews_dir / (preview["preview_id"] + ".json")
    cache.unlink()
    with pytest.raises(ContextStoreError) as missing:
        store.load_preview_body(preview["preview_id"], preview["preview_hash"])
    assert missing.value.code == "preview_unavailable"

    tampered = create_preview(store, suffix="tampered")
    tampered_cache = store.previews_dir / (tampered["preview_id"] + ".json")
    body = json.loads(tampered_cache.read_text(encoding="utf-8"))
    body["sections"]["verified_facts"][0]["summary"] = "tampered"
    tampered_cache.write_text(
        json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ContextStoreError) as changed:
        store.load_preview_body(
            tampered["preview_id"], tampered["preview_hash"]
        )
    assert changed.value.code == "stale_preview"


def test_load_preview_body_rejects_symlink_cache(tmp_path):
    from context_store import ContextStore
    from event_store import EventPathError

    store = ContextStore(tmp_path, clock=Clock())
    preview = create_preview(store)
    cache = store.previews_dir / (preview["preview_id"] + ".json")
    outside = tmp_path / "outside-preview.json"
    outside.write_text(cache.read_text(encoding="utf-8"), encoding="utf-8")
    cache.unlink()
    os.symlink(outside, cache)

    with pytest.raises(EventPathError):
        store.load_preview_body(preview["preview_id"], preview["preview_hash"])


def test_relative_vault_is_bound_at_construction(tmp_path, monkeypatch):
    from context_store import ContextStore

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    store = ContextStore("vault", clock=Clock())
    monkeypatch.chdir(second)

    create_preview(store)

    assert store.root == first / "vault" / "contexts"
    assert not (second / "vault").exists()


def test_context_paths_reject_symlink_root(tmp_path):
    from context_store import ContextStore
    from event_store import EventPathError

    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "contexts")
    store = ContextStore(tmp_path, clock=Clock())

    with pytest.raises(EventPathError):
        create_preview(store)
