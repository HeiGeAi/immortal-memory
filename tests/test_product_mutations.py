import json
import os
import stat
import threading
from datetime import datetime, timezone

import pytest

from claim_store import ClaimStore
from context_compiler import ContextCompiler
from context_store import ContextStore
from judgment_store import JudgmentStore
from living_self_service import LivingSelfService
from model_types import new_claim
from outcome_store import OutcomeStore
from product_mutations import MutationError, ProductMutationCoordinator


class FakeLiving:
    def __init__(self):
        self.current_value = {
            "version_id": "lsv_" + "1" * 32,
            "sections": {
                "values": [{"item_id": "self_1", "claim_ids": ["clm_1", "clm_2"]}]
            },
        }
        self.calls = []
        self.fail_materialize = 0
        self.fail_after_publish = 0
        self.history = {self.current_value["version_id"]: self.current_value}

    def current(self):
        return self.current_value

    def materialize_claim_change(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_materialize:
            self.fail_materialize -= 1
            raise RuntimeError("simulated derived crash")
        self.current_value = {
            "version_id": kwargs["result_version_id"],
            "sections": self.current_value["sections"],
        }
        self.history[self.current_value["version_id"]] = self.current_value
        if self.fail_after_publish:
            self.fail_after_publish -= 1
            raise RuntimeError("simulated crash after current publish")
        return self.current_value

    def load_version(self, version_id):
        return self.history[version_id]


class FakeClaims:
    def __init__(self):
        self.calls = []

    def transition(self, claim_id, status, **kwargs):
        self.calls.append(("transition", claim_id, status, kwargs))
        return {"claim_id": claim_id, "revision": kwargs["expected_revision"] + 1}

    def correct(self, claim_id, statement, **kwargs):
        self.calls.append(("correct", claim_id, statement, kwargs))
        return {"claim_id": "clm_replacement", "revision": 1}

    def reconsider(self, claim_id, **kwargs):
        self.calls.append(("reconsider", claim_id, kwargs))
        return {"claim_id": claim_id, "revision": kwargs["expected_revision"] + 1}


class FakeCompiler:
    def preview(self, **kwargs):
        return {
            "preview_id": "prv_1", "revision": 1,
            "lifecycle_status": "preview", "preview_hash": "sha256:" + "a" * 64,
            "source_revision": {"claim_seq": 1},
            "expires_at": "2026-07-22T00:15:00+00:00",
            "sections": {"verified_facts": [{"id": "clm_1"}]},
        }

    def compile(self, **kwargs):
        return {
            "context_id": "ctx_1", "stream_version": 2,
            "lifecycle_status": "compiled",
            "context_markdown": "# Safe context\n\n- first\n  - nested",
            "context_json": "/Users/private/.immortal/context.json",
            "context_md": "/Users/private/.immortal/context.md",
        }


class EmptyVerifiedEvidence:
    def preflight(self):
        return {
            "mode": "verified_sqlite", "source_state": "current",
            "bounded_scan": True, "indexed_id_count": 0,
        }

    def resolve(self, item):
        raise AssertionError("empty derived stores must not resolve evidence")

def coordinator(tmp_path):
    living = FakeLiving()
    claims = FakeClaims()
    value = ProductMutationCoordinator(
        tmp_path,
        claims=claims,
        living_self=living,
        judgments=object(),
        compiler=object(),
        outcomes=object(),
    )
    return value, claims, living


def metadata(key="idem_1"):
    return {"request_id": "req_1", "idempotency_key": key}


def test_self_action_requires_explicit_claim_membership_and_versions(tmp_path):
    service, claims, living = coordinator(tmp_path)
    result = service.mutate(
        "/api/v2/self/items/self_1/actions",
        {
            "action": "correct",
            "claim_id": "clm_2",
            "expected_self_version": living.current()["version_id"],
            "expected_version": 3,
            "reason": "owner corrected",
            "statement": "corrected statement",
        },
        **metadata(),
    )

    assert result["claim_id"] == "clm_replacement"
    assert result["derived_update_pending"] is False
    assert claims.calls[0][0:2] == ("correct", "clm_2")

    with pytest.raises(MutationError) as caught:
        service.mutate(
            "/api/v2/self/items/self_1/actions",
            {
                "action": "correct",
                "claim_id": "clm_missing",
                "expected_self_version": living.current()["version_id"],
                "expected_version": 1,
                "reason": "bad membership",
                "statement": "corrected statement",
            },
            **metadata("idem_2"),
        )
    assert caught.value.code == "scope_mismatch"


def test_idempotency_ledger_stores_only_digests_and_rejects_changed_intent(tmp_path):
    service, _claims, living = coordinator(tmp_path)
    body = {
        "action": "correct",
        "claim_id": "clm_1",
        "expected_self_version": living.current()["version_id"],
        "expected_version": 1,
        "statement": "private corrected statement",
        "reason": "private reason",
    }
    first = service.mutate(
        "/api/v2/self/items/self_1/actions", body, **metadata("secret-key")
    )
    second = service.mutate(
        "/api/v2/self/items/self_1/actions", body, **metadata("secret-key")
    )
    assert first == second

    text = (tmp_path / "runtime" / "idempotency.json").read_text()
    assert "secret-key" not in text
    assert "private corrected statement" not in text
    assert "private reason" not in text
    assert stat.S_IMODE(os.stat(tmp_path / "runtime" / "idempotency.json").st_mode) == 0o600

    changed = dict(body, statement="other private statement")
    with pytest.raises(MutationError) as caught:
        service.mutate(
            "/api/v2/self/items/self_1/actions",
            changed,
            **metadata("secret-key"),
        )
    assert caught.value.code == "idempotency_conflict"


def test_coordinator_fails_closed_for_symlink_or_wide_ledger(tmp_path):
    service, _claims, living = coordinator(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    os.symlink(outside, runtime / "idempotency.json")
    with pytest.raises(MutationError) as caught:
        service.mutate(
            "/api/v2/self/items/self_1/actions",
            {
                "action": "correct",
                "claim_id": "clm_1",
                "expected_self_version": living.current()["version_id"],
                "expected_version": 1,
                "reason": "test", "statement": "corrected",
            },
            **metadata(),
        )
    assert caught.value.code == "mutation_authority_unavailable"


def test_concurrent_same_intent_returns_one_safe_result(tmp_path):
    service, claims, living = coordinator(tmp_path)
    body = {
        "action": "correct",
        "claim_id": "clm_1",
        "expected_self_version": living.current()["version_id"],
        "expected_version": 1,
        "reason": "test", "statement": "corrected",
    }
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                service.mutate(
                    "/api/v2/self/items/self_1/actions",
                    body,
                    **metadata("same-key"),
                )
            )
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4
    assert all(result == results[0] for result in results)
    # Completed requests are safely replayed through native domain idempotency.
    assert len(claims.calls) == 4


def test_derived_pending_is_recovered_instead_of_cached_completed(tmp_path):
    service, claims, living = coordinator(tmp_path)
    living.fail_materialize = 1
    body = {
        "action": "correct", "claim_id": "clm_1",
        "expected_self_version": living.current()["version_id"],
        "expected_version": 1, "reason": "test", "statement": "corrected",
    }
    first = service.mutate(
        "/api/v2/self/items/self_1/actions", body, **metadata("recover-key")
    )
    retry = service.mutate(
        "/api/v2/self/items/self_1/actions", body, **metadata("recover-key")
    )
    assert first["derived_update_pending"] is True
    assert retry["derived_update_pending"] is False
    assert retry["derived_version_id"] == first["derived_version_id"]
    assert len(living.calls) == 2


def test_pending_prepare_is_readable_and_recoverable_after_restart(tmp_path):
    service, _claims, living = coordinator(tmp_path)
    body = {
        "action": "correct", "claim_id": "clm_1",
        "expected_self_version": living.current()["version_id"],
        "expected_version": 1, "reason": "test", "statement": "corrected",
    }
    original = service._dispatch

    def crash_before_domain(*args, **kwargs):
        raise RuntimeError("simulated crash after intent prepare")

    service._dispatch = crash_before_domain
    with pytest.raises(RuntimeError, match="intent prepare"):
        service.mutate(
            "/api/v2/self/items/self_1/actions",
            body,
            **metadata("prepared-restart"),
        )
    service._dispatch = original

    restarted = ProductMutationCoordinator(
        tmp_path,
        claims=service.claims,
        living_self=service.living_self,
        judgments=object(),
        compiler=object(),
        outcomes=object(),
    )
    result = restarted.mutate(
        "/api/v2/self/items/self_1/actions",
        body,
        **metadata("prepared-restart"),
    )
    assert result["derived_update_pending"] is False


def test_untrusted_route_action_and_result_never_reach_ledger(tmp_path):
    service, _claims, living = coordinator(tmp_path)
    private = "private statement must never be audited"
    with pytest.raises(MutationError):
        service.mutate(
            "/api/v2/self/items/self_1/actions",
            {
                "action": private, "claim_id": "clm_1",
                "expected_self_version": living.current()["version_id"],
                "expected_version": 1, "reason": "x",
            },
            **metadata("bad-action"),
        )
    assert not (tmp_path / "runtime" / "idempotency.json").exists()

    with pytest.raises(MutationError):
        service.mutate(
            "/api/v2/contexts/" + private + "/consume",
            {"expected_version": 1, "reason": "x"},
            **metadata("bad-target"),
        )
    assert not (tmp_path / "runtime" / "idempotency.json").exists()


def test_corrupt_completed_entry_is_not_trusted(tmp_path):
    service, _claims, living = coordinator(tmp_path)
    body = {
        "action": "correct", "claim_id": "clm_1",
        "expected_self_version": living.current()["version_id"],
        "expected_version": 1, "reason": "test", "statement": "corrected",
    }
    service.mutate(
        "/api/v2/self/items/self_1/actions", body, **metadata("corrupt-key")
    )
    path = tmp_path / "runtime" / "idempotency.json"
    value = json.loads(path.read_text())
    value["entries"][0]["result"]["claim_id"] = "private value with spaces"
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)
    with pytest.raises(MutationError) as caught:
        service.mutate(
            "/api/v2/self/items/self_1/actions", body, **metadata("corrupt-key")
        )
    assert caught.value.code == "mutation_authority_unavailable"


@pytest.mark.parametrize("corruption", ("duplicate_key", "nan", "bad_state"))
def test_ledger_rejects_ambiguous_json_and_invalid_state_combinations(
    tmp_path, corruption
):
    service, _claims, living = coordinator(tmp_path)
    body = {
        "action": "correct", "claim_id": "clm_1",
        "expected_self_version": living.current()["version_id"],
        "expected_version": 1, "reason": "test", "statement": "corrected",
    }
    service.mutate(
        "/api/v2/self/items/self_1/actions",
        body,
        **metadata("strict-ledger"),
    )
    path = tmp_path / "runtime" / "idempotency.json"
    if corruption == "duplicate_key":
        text = path.read_text()
        text = text.replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
            1,
        )
        path.write_text(text)
    else:
        value = json.loads(path.read_text())
        if corruption == "nan":
            value["entries"][0]["result"]["revision"] = float("nan")
        else:
            value["entries"][0]["completed_at"] = None
        path.write_text(json.dumps(value, allow_nan=True))
    os.chmod(path, 0o600)

    with pytest.raises(MutationError) as caught:
        service.mutate(
            "/api/v2/self/items/self_1/actions",
            body,
            **metadata("strict-ledger"),
        )
    assert caught.value.code == "mutation_authority_unavailable"


def test_pending_after_derived_publish_recovers_from_parent_snapshot(tmp_path):
    service, _claims, living = coordinator(tmp_path)
    living.fail_after_publish = 1
    parent = living.current()["version_id"]
    body = {
        "action": "correct", "claim_id": "clm_1",
        "expected_self_version": parent, "expected_version": 1, "reason": "test",
        "statement": "corrected",
    }
    first = service.mutate(
        "/api/v2/self/items/self_1/actions", body, **metadata("after-publish")
    )
    assert first["derived_update_pending"] is True
    assert living.current()["version_id"] == first["derived_version_id"]
    retry = service.mutate(
        "/api/v2/self/items/self_1/actions", body, **metadata("after-publish")
    )
    assert retry["derived_update_pending"] is False
    assert retry["derived_version_id"] == first["derived_version_id"]


def test_context_completed_replay_preserves_public_response_without_paths(tmp_path):
    compiler = FakeCompiler()
    first_service = ProductMutationCoordinator(
        tmp_path, claims=object(), living_self=object(), judgments=object(),
        compiler=compiler, outcomes=object(),
    )
    preview_body = {"task": "safe task", "expected_version": 0, "reason": "preview"}
    first = first_service.mutate(
        "/api/v2/contexts/preview", preview_body, **metadata("preview-restart")
    )
    restarted = ProductMutationCoordinator(
        tmp_path, claims=object(), living_self=object(), judgments=object(),
        compiler=compiler, outcomes=object(),
    )
    retry = restarted.mutate(
        "/api/v2/contexts/preview", preview_body, **metadata("preview-restart")
    )
    assert retry == first
    assert retry["preview_hash"].startswith("sha256:")
    assert retry["sections"]["verified_facts"]

    compile_body = {
        "preview_id": "prv_1", "preview_hash": "sha256:" + "a" * 64,
        "excluded_item_ids": [], "expected_version": 1, "reason": "compile",
    }
    compiled = restarted.mutate(
        "/api/v2/contexts", compile_body, **metadata("compile-restart")
    )
    replayed = ProductMutationCoordinator(
        tmp_path, claims=object(), living_self=object(), judgments=object(),
        compiler=compiler, outcomes=object(),
    ).mutate("/api/v2/contexts", compile_body, **metadata("compile-restart"))
    assert replayed == compiled
    assert replayed["context_markdown"] == "# Safe context\n\n- first\n  - nested"
    assert "context_json" not in replayed
    assert "context_md" not in replayed


def test_real_self_claim_action_and_restore_replay_native_authorities(tmp_path):
    vault = tmp_path / "vault"
    claims = ClaimStore(vault)
    claim = new_claim(
        statement="先验证再发布", source_kind="direct", evidence_ids=["ev_real"],
        status="candidate", claim_type="value", privacy="context_safe",
        role_scope=["work"], domain_scope=["technical"],
        now="2026-07-20T00:00:00+00:00",
    )
    claim["claim_id"] = "clm_real"
    created = claims.create(
        claim, expected_revision=0, request_id="seed_create",
        idempotency_key="seed_create", actor={"kind": "owner", "id": "owner"},
        reason="seed",
    )
    confirmed = claims.transition(
        "clm_real", "confirmed", expected_revision=created["revision"],
        request_id="seed_confirm", idempotency_key="seed_confirm",
        actor={"kind": "owner", "id": "owner"}, reason="seed",
    )
    living = LivingSelfService(vault, clock=lambda: "2026-07-22T00:00:00+00:00")
    parent = living.confirm(living.build_candidate(), reason="seed model")
    item_id = parent["sections"]["values"][0]["item_id"]
    service = ProductMutationCoordinator(
        vault, claims=claims, living_self=living, judgments=object(),
        compiler=object(), outcomes=object(),
    )
    body = {
        "action": "correct", "claim_id": "clm_real",
        "expected_self_version": parent["version_id"],
        "expected_version": confirmed["revision"], "reason": "owner corrected",
        "statement": "先隔离再验证和发布",
    }
    first = service.mutate(
        "/api/v2/self/items/%s/actions" % item_id, body, **metadata("real-self")
    )
    retry = ProductMutationCoordinator(
        vault, claims=ClaimStore(vault), living_self=LivingSelfService(vault, clock=lambda: "2026-07-22T00:00:00+00:00"),
        judgments=object(), compiler=object(), outcomes=object(),
    ).mutate("/api/v2/self/items/%s/actions" % item_id, body, **metadata("real-self"))
    assert retry == first
    assert retry["derived_update_pending"] is False

    current = LivingSelfService(vault, clock=lambda: "2026-07-22T00:00:00+00:00").current()
    restore_body = {"expected_version": current["version_id"], "reason": "restore prior self"}
    restored = service.mutate(
        "/api/v2/self/versions/%s/restore" % parent["version_id"],
        restore_body, **metadata("real-restore"),
    )
    restored_retry = service.mutate(
        "/api/v2/self/versions/%s/restore" % parent["version_id"],
        restore_body, **metadata("real-restore"),
    )
    assert restored_retry == restored


def test_real_judgment_create_and_all_action_mappings(tmp_path):
    vault = tmp_path / "vault"
    judgments = JudgmentStore(vault)
    service = ProductMutationCoordinator(
        vault, claims=object(), living_self=object(), judgments=judgments,
        compiler=object(), outcomes=object(),
    )

    def create(key):
        body = {
            "title": "先审计", "situation": "生产升级", "decision": "先读后写",
            "evidence_ids": ["ev_1"], "expected_version": 0, "reason": "create",
        }
        first = service.mutate("/api/v2/judgments", body, **metadata(key))
        assert service.mutate("/api/v2/judgments", body, **metadata(key)) == first
        return first

    confirmed = create("jdg-create-confirm")
    confirm_body = {"action": "confirm", "expected_version": 1, "reason": "confirm"}
    confirmed = service.mutate(
        "/api/v2/judgments/%s/actions" % confirmed["card_id"],
        confirm_body, **metadata("jdg-confirm"),
    )
    assert confirmed["status"] == "confirmed"
    outcome_body = {
        "action": "record_outcome", "expected_version": 2, "reason": "outcome",
        "status": "positive", "summary": "有效", "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    outcome = service.mutate(
        "/api/v2/judgments/%s/actions" % confirmed["card_id"],
        outcome_body, **metadata("jdg-outcome"),
    )
    assert outcome["revision"] == 3
    retired = service.mutate(
        "/api/v2/judgments/%s/actions" % confirmed["card_id"],
        {"action": "retire", "expected_version": 3, "reason": "retire"},
        **metadata("jdg-retire"),
    )
    assert retired["status"] == "retired"

    rejected = create("jdg-create-reject")
    rejected = service.mutate(
        "/api/v2/judgments/%s/actions" % rejected["card_id"],
        {"action": "reject", "expected_version": 1, "reason": "reject"},
        **metadata("jdg-reject"),
    )
    assert rejected["status"] == "rejected"

    corrected = create("jdg-create-correct")
    corrected = service.mutate(
        "/api/v2/judgments/%s/actions" % corrected["card_id"],
        {"action": "correct", "expected_version": 1, "reason": "correct", "changes": {"decision": "先隔离再验证"}},
        **metadata("jdg-correct"),
    )
    assert corrected["revision"] == 2


def test_real_context_preview_compile_consume_and_outcome_replay(tmp_path):
    vault = tmp_path / "vault"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    living = LivingSelfService(vault, clock=lambda: now.isoformat())
    living.confirm(living.build_candidate(), reason="empty model")
    contexts = ContextStore(vault, clock=lambda: now)
    compiler = ContextCompiler(
        vault, claims=ClaimStore(vault), living_self=living,
        judgments=JudgmentStore(vault, clock=lambda: now),
        evidence=EmptyVerifiedEvidence(), context_store=contexts,
        clock=lambda: now,
    )
    outcomes = OutcomeStore(
        vault, context_store=contexts, context_compiler=compiler,
        clock=lambda: now,
    )
    service = ProductMutationCoordinator(
        vault, claims=ClaimStore(vault), living_self=living,
        judgments=JudgmentStore(vault, clock=lambda: now),
        compiler=compiler, outcomes=outcomes,
    )
    preview_body = {
        "task": "评审当前方案", "mode": "reviewer", "expected_version": 0,
        "reason": "preview",
    }
    preview = service.mutate(
        "/api/v2/contexts/preview", preview_body, **metadata("real-preview")
    )
    assert service.mutate(
        "/api/v2/contexts/preview", preview_body, **metadata("real-preview")
    ) == preview
    compile_body = {
        "preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"],
        "excluded_item_ids": [], "expected_version": 1, "reason": "compile",
    }
    compiled = service.mutate(
        "/api/v2/contexts", compile_body, **metadata("real-compile")
    )
    assert compiled["context_markdown"]
    assert "context_json" not in compiled and "context_md" not in compiled
    assert service.mutate(
        "/api/v2/contexts", compile_body, **metadata("real-compile")
    ) == compiled
    context_id = compiled["context_id"]
    consume_body = {"expected_version": 2, "reason": "used by agent"}
    consumed = service.mutate(
        "/api/v2/contexts/%s/consume" % context_id,
        consume_body, **metadata("real-consume"),
    )
    assert consumed["lifecycle_status"] == "consumed"
    assert service.mutate(
        "/api/v2/contexts/%s/consume" % context_id,
        consume_body, **metadata("real-consume"),
    ) == consumed
    outcome_body = {
        "adopted": "yes", "result": "positive", "summary": "方案有效",
        "expected_version": 3, "reason": "record result",
    }
    outcome = service.mutate(
        "/api/v2/contexts/%s/outcomes" % context_id,
        outcome_body, **metadata("real-outcome"),
    )
    assert outcome["outcome_id"]
    assert service.mutate(
        "/api/v2/contexts/%s/outcomes" % context_id,
        outcome_body, **metadata("real-outcome"),
    ) == outcome
