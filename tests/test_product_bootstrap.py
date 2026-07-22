from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import immortal
import pytest
from product_data import ProductIndexIntegrity


def _config(vault: Path) -> dict:
    return {"vault_dir": str(vault), "feishu": {}, "role_defaults": {}}


def _patch_config(monkeypatch, vault: Path) -> None:
    config = _config(vault)
    monkeypatch.setattr(immortal, "load_config", lambda: dict(config))
    monkeypatch.setattr(immortal, "save_config", lambda _value: vault / "config.json")
    monkeypatch.setattr(
        immortal,
        "configured_vault_dir",
        lambda value: Path(str(value["vault_dir"])),
    )
    monkeypatch.setattr(immortal, "STATE_FILE", vault / "orchestrator_state.json")


def _record() -> str:
    return json.dumps(
        {
            "id": "first-use-record",
            "source": "test",
            "timestamp": "2026-07-22T00:00:00+00:00",
            "content": "A real first-use record with an explicit UTC timestamp.",
        }
    ) + "\n"


def test_init_marks_only_a_pristine_vault_for_product_bootstrap(tmp_path, monkeypatch):
    pristine = tmp_path / "pristine"
    _patch_config(monkeypatch, pristine)

    args = immortal.build_parser().parse_args(["init", "--vault-dir", str(pristine)])
    assert immortal.command_init(args) == 0

    marker = pristine / "product" / "bootstrap-v1.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "kind": "first_use",
        "schema_version": 1,
        "state": "pending",
    }

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "index.jsonl").write_text(_record(), encoding="utf-8")
    _patch_config(monkeypatch, existing)

    args = immortal.build_parser().parse_args(["init", "--vault-dir", str(existing)])
    assert immortal.command_init(args) == 0
    assert not (existing / "product" / "bootstrap-v1.json").exists()

    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "does-not-exist")
    assert immortal._is_pristine_product_vault(dangling) is False


def test_first_use_bootstrap_builds_a_trusted_product_index(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "index.jsonl").write_text(_record(), encoding="utf-8")
    immortal._write_product_bootstrap(vault, "pending")

    result = immortal._sync_bootstrapped_product_index(vault)

    assert result["mode"] in {"full_rebuild", "incremental", "up_to_date"}
    assert result["added"] == 1
    assert immortal._product_bootstrap_state(vault) == "active"
    with ProductIndexIntegrity(vault).trusted_connection() as (connection, _metadata):
        assert connection.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 1


def test_train_syncs_marked_first_use_vault_before_the_product_pipeline(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    immortal._write_product_bootstrap(vault, "pending")
    _patch_config(monkeypatch, vault)
    calls = []

    monkeypatch.setattr(
        immortal,
        "_sync_bootstrapped_product_index",
        lambda received: calls.append(received) or {"mode": "rebuilt", "added": 1},
    )
    monkeypatch.setattr(
        immortal.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    args = immortal.build_parser().parse_args(["train", "--smoke"])
    assert immortal.command_train(args) == 0
    assert calls == [vault]


def test_train_never_auto_rebuilds_an_unmarked_existing_vault(tmp_path, monkeypatch):
    vault = tmp_path / "legacy"
    vault.mkdir()
    (vault / "index.jsonl").write_text(_record(), encoding="utf-8")
    _patch_config(monkeypatch, vault)
    calls = []

    monkeypatch.setattr(
        immortal,
        "_sync_bootstrapped_product_index",
        lambda received: calls.append(received) or {"mode": "rebuilt", "added": 1},
    )
    monkeypatch.setattr(
        immortal.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    args = immortal.build_parser().parse_args(["train"])
    assert immortal.command_train(args) == 0
    assert calls == []


@pytest.mark.skipif(not Path("/var").is_symlink(), reason="macOS system alias")
def test_product_bootstrap_path_normalizes_macos_var_alias(tmp_path):
    resolved = tmp_path.resolve()
    try:
        alias = Path("/var") / resolved.relative_to("/private/var")
    except ValueError:
        pytest.skip("temporary directory is not under /private/var")

    assert immortal._product_bootstrap_path(alias) == (
        resolved / "product" / "bootstrap-v1.json"
    )
