from __future__ import annotations

import json
import subprocess

import feishu_collect


def test_run_lark_retries_transient_network_failure(monkeypatch):
    calls = []

    def run(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                args=[], returncode=1,
                stdout=json.dumps({"ok": False, "error": {"type": "network", "subtype": "timeout"}}),
                stderr="",
            )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps({"ok": True}), stderr="")

    monkeypatch.setattr(feishu_collect.subprocess, "run", run)

    ok, body, error = feishu_collect.run_lark(["auth", "status"])

    assert ok is True
    assert body == {"ok": True}
    assert error == ""
    assert len(calls) == 2
