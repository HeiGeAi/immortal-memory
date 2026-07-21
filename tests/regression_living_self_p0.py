#!/usr/bin/env python3
"""Living Self release-gate scenarios backed by real authority-layer tests."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = (
    (
        "S1 other speaker cannot auto-confirm owner claim",
        ("tests/test_attribution_service.py::test_other_speaker_cannot_become_owner_direct_fact",),
    ),
    (
        "S2 one-off request cannot become stable preference",
        ("tests/test_living_self_service.py::test_single_transient_claim_does_not_become_mental_model",),
    ),
    (
        "S3 correction supersedes but preserves old claim",
        ("tests/test_claim_store.py::test_correct_supersedes_original_and_preserves_four_event_history",),
    ),
    (
        "S4 role and account scope prevents cross-context use",
        ("tests/test_context_compiler.py::test_preview_excludes_private_wrong_scope_unconfirmed_expired_and_future",),
    ),
    (
        "S5 private evidence never enters Context Pack",
        ("tests/test_model_types.py::test_context_pack_rejects_private_raw_body_and_section_item_overflow",),
    ),
    (
        "S6 inference is labeled and cannot appear under verified facts",
        ("tests/test_model_types.py::test_context_section_kind_status_and_source_kind_must_match",),
    ),
    (
        "S7 stale preview cannot compile",
        ("tests/test_context_store.py::test_expired_preview_cannot_compile_and_availability_is_dynamic",),
    ),
    (
        "S8 failed outcome creates review signal without rewriting model",
        ("tests/regression_living_self_p0.py::test_failed_outcome_is_review_signal_not_model_rewrite",),
    ),
    (
        "S9 service restart preserves claim, context, and outcome state",
        ("tests/regression_living_self_p0.py::test_restart_preserves_claim_context_and_outcome",),
    ),
    (
        "S10 v1.0 vault migrates without changing raw hashes",
        ("tests/test_v11_migration.py::test_v10_vault_migrates_without_source_changes",),
    ),
)


def test_failed_outcome_is_review_signal_not_model_rewrite(tmp_path):
    from living_self_service import LivingSelfService
    from test_outcome_store import _consume, _outcome_store, _record, _seed_compiled

    living = LivingSelfService(
        tmp_path,
        clock=lambda: "2099-07-21T08:00:00+00:00",
    )
    living.confirm(living.build_candidate(), reason="owner baseline")
    before = living.current_path.read_bytes()
    contexts, compiled, _pack = _seed_compiled(tmp_path)
    outcomes = _outcome_store(tmp_path, contexts)
    _consume(outcomes, compiled)
    challenged = {
        "kind": "judgment",
        "id": "jdg_" + "3" * 32,
        "revision": 4,
    }

    outcome = _record(
        outcomes,
        compiled,
        adopted="no",
        result="negative",
        summary="结果失败，需要复核相关判断",
        confirmed_refs=[],
        challenged_refs=[challenged],
    )

    assert outcome["result"] == "negative"
    assert outcome["confirmed_refs"] == []
    assert outcome["challenged_refs"] == [challenged]
    assert living.current_path.read_bytes() == before


def test_restart_preserves_claim_context_and_outcome(tmp_path):
    from claim_store import ClaimStore
    from context_store import ContextStore
    from test_claim_store import claim, create
    from test_outcome_store import _consume, _outcome_store, _record, _seed_compiled

    claims = ClaimStore(tmp_path)
    created = create(claims, claim("clm-restart", "重启后仍应保留"))
    contexts, compiled, _pack = _seed_compiled(tmp_path)
    outcomes = _outcome_store(tmp_path, contexts)
    _consume(outcomes, compiled)
    outcome = _record(outcomes, compiled)
    claims.current_path.unlink()
    contexts.current_path.unlink()

    restarted_claims = ClaimStore(tmp_path)
    restarted_contexts = ContextStore(tmp_path, clock=contexts._clock)
    restarted_outcomes = _outcome_store(tmp_path, restarted_contexts)

    assert restarted_claims.get(created["claim_id"]) == created
    assert restarted_contexts.get(compiled["context_id"])["outcome_id"] == outcome[
        "outcome_id"
    ]
    assert restarted_outcomes.get(compiled["context_id"]) == outcome
    assert restarted_claims.current_path.is_file()
    assert restarted_contexts.current_path.is_file()


def main() -> int:
    passed = 0
    failures = []
    for index, (label, nodes) in enumerate(SCENARIOS, start=1):
        with tempfile.TemporaryDirectory(prefix=f"immortal-living-p0-{index:02d}-") as base:
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                *nodes,
                "--basetemp",
                str(Path(base) / "pytest"),
            ]
            result = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "core")},
                capture_output=True,
                text=True,
            )
        if result.returncode == 0:
            passed += 1
            print(f"[PASS] {label}")
        else:
            failures.append((label, result.stdout + result.stderr))
            print(f"[FAIL] {label}")
    print(f"\nPassed: {passed}/10  Failed: {len(failures)}/10")
    for label, output in failures:
        print(f"\n{label}\n{output[-4000:]}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
