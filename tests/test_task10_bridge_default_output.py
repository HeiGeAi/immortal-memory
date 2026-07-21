from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest import mock

import agent_bridge
import context_compiler as context_compiler_module
from test_context_compiler import claim, compiler, preview


def test_bridge_default_output_path_still_contains_verified_markdown(
    tmp_path, monkeypatch
):
    instance = compiler(
        tmp_path / "vault",
        claims=[claim("clm_fact", "approved canonical context")],
    )
    reviewed = preview(instance)
    original_safe_read = context_compiler_module.safe_read_text
    observed = {"verified_body": "", "path": None}

    def replace_after_verified_read(path):
        body = original_safe_read(path)
        candidate = Path(path)
        if candidate.name == "TASK_CONTEXT.md" and body and observed["path"] is None:
            observed["verified_body"] = body
            observed["path"] = candidate
            candidate.unlink()
            candidate.write_text(
                "# replacement after verification\n", encoding="utf-8"
            )
        return body

    monkeypatch.setattr(
        context_compiler_module, "safe_read_text", replace_after_verified_read
    )
    metadata = tmp_path / "bridge-result.json"
    latest_delivery = tmp_path / "latest-context.md"
    args = argparse.Namespace(
        query="approved task",
        since="2026-07-01",
        with_recall=False,
        timeout=1,
        output="",
        print=False,
        force=False,
        mode="reviewer",
        preview_only=False,
        preview_id=reviewed["preview_id"],
        preview_hash=reviewed["preview_hash"],
        exclude_item_id=[],
        ttl_seconds=900,
        metadata_output=str(metadata),
    )
    preflight = {"context_status": "ready", "vault_status": "healthy"}
    original_file_info = agent_bridge.file_info

    def reject_context_reopen(path):
        if Path(path).name in {"TASK_CONTEXT.md", "latest-context.md"}:
            raise AssertionError("verified Context paths must not be reopened for stat")
        return original_file_info(path)

    with mock.patch.object(agent_bridge, "ContextCompiler", lambda _vault: instance), \
         mock.patch.object(agent_bridge, "IMMORTAL_DIR", tmp_path / "vault"), \
         mock.patch.object(agent_bridge, "LATEST_CONTEXT_MD", latest_delivery), \
         mock.patch.object(agent_bridge, "LATEST_CONTEXT_JSON", tmp_path / "latest.json"), \
         mock.patch.object(agent_bridge, "gather_preflight", return_value=preflight), \
         mock.patch.object(
             agent_bridge, "file_info", side_effect=reject_context_reopen
         ), \
         mock.patch.object(
             agent_bridge, "_authoritative_runtime_available", return_value=True
         ):
        code = agent_bridge.command_context(args)

    assert code == 0
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "context_markdown" not in payload
    assert observed["path"] is not None
    assert observed["path"].read_text(encoding="utf-8") != observed["verified_body"]
    assert latest_delivery.read_text(encoding="utf-8") == observed["verified_body"]
    assert payload["delivered_context_md"] == str(latest_delivery)
    assert payload["context_markdown_hash"] == instance._hash_text(
        observed["verified_body"]
    )
