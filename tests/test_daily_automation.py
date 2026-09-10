from __future__ import annotations

import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent / "core"
sys.path.insert(0, str(SKILL_DIR))

import immortal


def test_daily_script_propagates_feedback_exit_after_successful_main_run(tmp_path, monkeypatch):
    monkeypatch.setattr(immortal, "configured_vault_dir", lambda _config: tmp_path)
    monkeypatch.setattr(immortal, "SKILL_DIR", tmp_path / "skill")

    script = immortal.write_daily_backup_script({})
    body = script.read_text(encoding="utf-8")

    assert 'if [ "$STATUS" -ne 0 ]; then' in body
    assert '  exit "$STATUS"' in body
    assert 'exit "$FEEDBACK_STATUS"' in body
