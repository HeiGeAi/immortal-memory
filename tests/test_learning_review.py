from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


def load_module():
    assert importlib.util.find_spec("learning_review") is not None, "learning_review module is missing"
    return importlib.import_module("learning_review")


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return list(self.rows)


def test_empty_vault_is_a_read_only_zero_candidate_review(tmp_path):
    module = load_module()

    report = module.build_review(tmp_path)

    assert report["counts"] == {"total": 0, "claims": 0, "judgments": 0, "visible": 0}
    assert list(tmp_path.rglob("*")) == []


def test_tmp_alias_is_canonicalized_before_opening_event_stores():
    module = load_module()
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        vault = Path(raw)

        report = module.build_review(vault)

        assert report["counts"]["total"] == 0
        assert list(vault.rglob("*")) == []


def test_review_is_bounded_redacted_and_counts_all_candidates(tmp_path):
    module = load_module()
    claims = [
        {
            "claim_id": "clm_%02d" % index,
            "statement": "候选 %d sk-abcdefghijklmnop /Users/example/private" % index,
            "status": "candidate" if index < 3 else "confirmed",
            "revision": 1,
            "updated_at": "2026-07-2%dT08:00:00+00:00" % (index + 1),
        }
        for index in range(4)
    ]
    judgments = [
        {
            "card_id": "jud_01",
            "title": "待确认判断",
            "status": "candidate",
            "revision": 2,
            "updated_at": "2026-07-27T09:00:00+00:00",
        }
    ]
    with mock.patch.object(module, "ClaimStore", return_value=FakeStore(claims)), mock.patch.object(
        module, "JudgmentStore", return_value=FakeStore(judgments)
    ):
        report = module.build_review(
            tmp_path,
            limit=2,
            clock=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
        )

    assert report["counts"] == {"total": 4, "claims": 3, "judgments": 1, "visible": 2}
    assert [item["kind"] for item in report["items"]] == ["judgment", "claim"]
    serialized = json.dumps(report, ensure_ascii=False)
    assert "sk-abcdefghijklmnop" not in serialized
    assert "/Users/example" not in serialized
    assert "evidence" not in serialized


def test_feishu_send_is_owner_only_confirmed_and_shell_free(tmp_path):
    module = load_module()
    report = {
        "generated_at": "2026-07-28T00:00:00+00:00",
        "counts": {"total": 1, "claims": 1, "judgments": 0, "visible": 1},
        "items": [{"kind": "claim", "summary": "候选理解"}],
        "review_url": "http://127.0.0.1:8765/?view=home",
    }
    completed = subprocess.CompletedProcess(
        ["lark-cli"],
        0,
        stdout=json.dumps({"ok": True, "data": {"message_id": "om_test", "chat_id": "oc_test"}}),
        stderr="",
    )
    with mock.patch.object(module.shutil, "which", return_value="/usr/local/bin/lark-cli"), mock.patch.object(
        module.subprocess, "run", return_value=completed
    ) as run:
        receipt = module.send_to_feishu(report, "ou_owner", dry_run=False)

    args = run.call_args.args[0]
    assert args[:3] == ["/usr/local/bin/lark-cli", "im", "+messages-send"]
    assert ["--as", "bot"] == args[3:5]
    assert args[args.index("--user-id") + 1] == "ou_owner"
    assert "--markdown" in args
    assert run.call_args.kwargs.get("shell") is not True
    assert receipt["message_id"] == "om_test"


def test_main_refuses_unconfirmed_remote_write(tmp_path):
    module = load_module()
    with mock.patch.object(module, "send_to_feishu") as send:
        code = module.main(
            [
                "--vault-dir",
                str(tmp_path),
                "--send-feishu",
            ]
        )

    assert code == 2
    send.assert_not_called()


def test_top_level_cli_forwards_learning_review_options():
    import immortal

    args = mock.Mock(
        vault_dir="/tmp/vault",
        limit=5,
        json=True,
        send_feishu=True,
        dry_run=True,
        confirm_remote_write=False,
    )
    with mock.patch.object(immortal, "run_script", return_value=0) as run:
        code = immortal.command_learning_review(args)

    assert code == 0
    run.assert_called_once_with(
        "learning_review.py",
        [
            "--vault-dir",
            "/tmp/vault",
            "--limit",
            "5",
            "--json",
            "--send-feishu",
            "--dry-run",
        ],
    )
