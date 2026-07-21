from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest


_TASK10_MODULES = {
    "test_agent_bridge_cors.py",
    "test_runtime_resilience.py",
    "test_task10_bridge_default_output.py",
}


def _fingerprint(path: Path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    digest = None
    if stat.S_ISREG(metadata.st_mode):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
    return (metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, digest)


def _live_derived_paths():
    import agent_bridge
    import agent_bridge_server
    import task_compile

    return {
        agent_bridge.ENTRY_MD,
        agent_bridge.ENTRY_JSON,
        agent_bridge.LATEST_CONTEXT_MD,
        agent_bridge.LATEST_CONTEXT_JSON,
        agent_bridge.CLAUDE_PROMPT,
        agent_bridge_server.AUDIT_LOG,
        agent_bridge_server.AUDIT_LATEST,
        task_compile.LATEST_MD,
        task_compile.LATEST_JSON,
    }


@pytest.fixture(scope="session", autouse=True)
def live_derived_session_sentinel():
    paths = _live_derived_paths()
    before = {path: _fingerprint(path) for path in paths}
    yield
    after = {path: _fingerprint(path) for path in paths}
    changed = [str(path) for path in before if before[path] != after[path]]
    assert not changed, (
        "test session changed live Immortal derived paths: " + ", ".join(changed)
    )


@pytest.fixture(autouse=True)
def task10_live_derived_write_sentinel(request, monkeypatch):
    if Path(str(request.fspath)).name not in _TASK10_MODULES:
        yield
        return

    import agent_bridge
    import agent_bridge_server
    import task_compile

    live_paths = _live_derived_paths()
    canonical_live = {
        agent_bridge._canonical_system_alias_path(path) for path in live_paths
    }
    before = {path: _fingerprint(path) for path in live_paths}
    original_safe_write = agent_bridge._safe_write_text
    original_write_latest = task_compile.write_latest
    original_audit_event = agent_bridge_server.audit_event

    def guarded_safe_write(path, content):
        canonical = agent_bridge._canonical_system_alias_path(Path(path))
        if canonical in canonical_live:
            raise AssertionError(
                "Task10 test attempted to write a live Immortal derived path: "
                + str(path)
            )
        return original_safe_write(path, content)

    def guarded_write_latest(session_dir, manifest, context_text):
        targets = {
            agent_bridge._canonical_system_alias_path(task_compile.LATEST_MD),
            agent_bridge._canonical_system_alias_path(task_compile.LATEST_JSON),
        }
        if targets.intersection(canonical_live):
            raise AssertionError(
                "Task10 test attempted to write live task session latest files"
            )
        return original_write_latest(session_dir, manifest, context_text)

    def guarded_audit_event(payload):
        targets = {
            agent_bridge._canonical_system_alias_path(agent_bridge_server.AUDIT_LOG),
            agent_bridge._canonical_system_alias_path(
                agent_bridge_server.AUDIT_LATEST
            ),
        }
        if targets.intersection(canonical_live):
            raise AssertionError(
                "Task10 test attempted to write live Agent Bridge audit files"
            )
        return original_audit_event(payload)

    monkeypatch.setattr(agent_bridge, "_safe_write_text", guarded_safe_write)
    monkeypatch.setattr(task_compile, "write_latest", guarded_write_latest)
    monkeypatch.setattr(agent_bridge_server, "audit_event", guarded_audit_event)
    yield

    after = {path: _fingerprint(path) for path in live_paths}
    assert after == before, "Task10 test changed a live Immortal derived file"
