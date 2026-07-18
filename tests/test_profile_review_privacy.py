from __future__ import annotations

import json

from profile_review import ReviewStore


MEMORY_ID = "a" * 24


def make_store(tmp_path, *, sensitivity="confidential"):
    proposal = tmp_path / "proposal.md"
    memories = tmp_path / "profile_memories.jsonl"
    reviewed = tmp_path / "reviewed.jsonl"
    review_state = tmp_path / "review_state.json"
    audit = tmp_path / "runtime" / "profile_actions.jsonl"
    proposal.write_text(f"- [ ] `{MEMORY_ID}` candidate\n", encoding="utf-8")
    memories.write_text(
        json.dumps(
            {
                "memory_id": MEMORY_ID,
                "statement": "private statement",
                "evidence": "private evidence",
                "sensitivity": sensitivity,
                "focus": "self_profile",
                "memory_type": "preference",
                "source": {"title": "Private source", "url": "https://private.example"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return ReviewStore(
        proposal,
        memories,
        reviewed,
        review_state,
        audit_path=audit,
    )


def test_profile_list_omits_confidential_body(tmp_path):
    store = make_store(tmp_path)

    page = store.list_candidates(limit=20, offset=0)
    encoded = json.dumps(page, ensure_ascii=False)

    assert "private statement" not in encoded
    assert "private evidence" not in encoded
    assert "https://private.example" not in encoded
    assert page["items"][0]["masked"] is True


def test_profile_detail_returns_one_requested_candidate(tmp_path):
    store = make_store(tmp_path)

    detail = store.candidate_detail(MEMORY_ID)

    assert detail["id"] == MEMORY_ID
    assert detail["statement"] == "private statement"
    assert detail["evidence"] == "private evidence"
    assert detail["masked"] is False


def test_profile_list_is_paginated_and_filterable(tmp_path):
    store = make_store(tmp_path, sensitivity="internal")

    page = store.list_candidates(limit=100, offset=0, review_state="pending", query="private")

    assert page["limit"] == 50
    assert page["total"] == 1
    assert page["items"][0]["statement"] == "private statement"
    assert "evidence" not in page["items"][0]


def test_profile_action_audit_contains_no_body(tmp_path):
    store = make_store(tmp_path)

    store.update_review(MEMORY_ID, "approve")
    event = json.loads(store.audit_path.read_text(encoding="utf-8").splitlines()[-1])
    encoded = json.dumps(event, ensure_ascii=False)

    assert event["action"] == "approve"
    assert event["target_id"] == MEMORY_ID
    assert event["result"] == "ok"
    assert event["recoverable"] is True
    assert "private statement" not in encoded
    assert "private evidence" not in encoded
