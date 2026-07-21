import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_bridge
import task_compile
import collect
import process_utils


class VolatileFileTest(unittest.TestCase):
    def test_disappearing_file_is_skipped_without_dedup_pollution(self):
        missing = Path("/tmp/immortal-file-that-disappeared")
        existing = set()

        self.assertEqual(collect.file_hash(missing), "")
        self.assertTrue(collect.file_seen(existing, "mem|gone|", "test", missing))
        self.assertEqual(existing, set())


class ProcessGroupTimeoutTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX process-group behavior")
    def test_timeout_terminates_grandchild_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild-survived"
            child_code = (
                "import pathlib,time;"
                "time.sleep(1);"
                f"pathlib.Path({str(marker)!r}).write_text('alive')"
            )
            parent_code = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "time.sleep(30)"
            )

            with self.assertRaises(subprocess.TimeoutExpired):
                process_utils.run_process(
                    [sys.executable, "-c", parent_code],
                    capture_output=True,
                    text=True,
                    timeout=0.2,
                )
            time.sleep(1.2)
            self.assertFalse(marker.exists())


class AgentBridgeTimeoutTest(unittest.TestCase):
    @unittest.skipUnless(Path("/var").is_symlink(), "macOS system alias behavior")
    def test_safe_context_write_normalizes_macos_var_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved_root = Path(tmp).resolve()
            try:
                alias_root = Path("/var") / resolved_root.relative_to("/private/var")
            except ValueError:
                self.skipTest("temporary directory is not below /private/var")
            target = alias_root / "nested" / "context.json"

            agent_bridge._safe_write_text(target, "{}\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "{}\n")

    def test_safe_context_write_rejects_arbitrary_symlink_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual"
            actual.mkdir()
            linked = root / "linked"
            linked.symlink_to(actual, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "safe directory"):
                agent_bridge._safe_write_text(linked / "context.json", "{}\n")

            self.assertFalse((actual / "context.json").exists())

    def test_context_timeout_writes_bounded_failure_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "context.md"
            args = argparse.Namespace(
                query="timeout case",
                since="2026-07-01",
                with_recall=False,
                timeout=1,
                output=str(output),
                print=False,
                json=False,
                force=True,
            )
            timeout = subprocess.TimeoutExpired(
                cmd=["python3", "immortal.py"],
                timeout=1,
                output="partial output",
                stderr=(
                    "still running token="
                    + "sk-"
                    + "a" * 24
                    + " path=/Users/alice/private.log"
                ),
            )
            latest_json = Path(tmp) / "latest-context.json"
            with mock.patch.object(agent_bridge.subprocess, "run", side_effect=timeout), \
                 mock.patch.object(agent_bridge, "LATEST_CONTEXT_JSON", latest_json):
                code = agent_bridge.command_context(args)

            self.assertEqual(code, 1)
            body = output.read_text(encoding="utf-8")
            self.assertIn("timed out", body.lower())
            self.assertIn("Exit code: 1", body)
            self.assertNotIn("sk-" + "a" * 24, body)
            self.assertNotIn("/Users/alice", body)
            self.assertNotIn("sk-" + "a" * 24, latest_json.read_text(encoding="utf-8"))

    def test_ready_context_uses_authoritative_preview_then_compile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical_md = root / "packs" / "ctx_one" / "TASK_CONTEXT.md"
            canonical_json = root / "packs" / "ctx_one" / "context.json"
            canonical_md.parent.mkdir(parents=True)
            canonical_md.write_text("# authoritative\n", encoding="utf-8")
            canonical_json.write_text("{}\n", encoding="utf-8")
            output = root / "delivered.md"
            calls = []

            approved_preview = {
                "preview_id": "prv_one",
                "preview_hash": "sha256:" + "1" * 64,
                "task": "authoritative review task",
                "mode": "reviewer",
                "source_revision": {"claims_event_seq": 1},
                "sections": {"verified_facts": []},
            }

            class FakeContextStore:
                def get(self, preview_id):
                    calls.append(("get", preview_id))
                    return dict(approved_preview)

            class FakeCompiler:
                def __init__(self, vault):
                    calls.append(("init", Path(vault)))
                    self.context_store = FakeContextStore()

                def preview(self, query, **kwargs):
                    calls.append(("preview", query, kwargs))
                    return dict(approved_preview)

                def compile(self, **kwargs):
                    calls.append(("compile", kwargs))
                    return {
                        "context_id": "ctx_one",
                        "preview_id": "prv_one",
                        "preview_hash": "sha256:" + "1" * 64,
                        "lifecycle_status": "compiled",
                        "availability_status": "active",
                        "source_revision": {"claims_event_seq": 1},
                        "content_hash": "sha256:" + "2" * 64,
                        "task": "authoritative review task",
                        "mode": "reviewer",
                        "expires_at": "2026-07-22T00:00:00+00:00",
                        "context_json": str(canonical_json),
                        "context_md": str(canonical_md),
                    }

            args = argparse.Namespace(
                query="review plan",
                since="2026-07-01",
                with_recall=False,
                timeout=1,
                output=str(output),
                print=False,
                force=False,
                mode="reviewer",
                preview_only=False,
                preview_id="",
                preview_hash="",
                exclude_item_id=[],
                ttl_seconds=900,
            )
            latest_json = root / "latest-context.json"
            preflight = {
                "context_status": "ready",
                "vault_status": "healthy",
                "last_real_collect_at": "2026-07-21T00:00:00+00:00",
                "query_coverage": "covered",
                "coverage_gap_hours": 0,
                "reasons": [],
            }
            with mock.patch.object(agent_bridge, "ContextCompiler", FakeCompiler, create=True), \
                 mock.patch.object(agent_bridge, "IMMORTAL_DIR", root), \
                 mock.patch.object(agent_bridge, "AGENT_DIR", root), \
                 mock.patch.object(agent_bridge, "LATEST_CONTEXT_JSON", latest_json), \
                 mock.patch.object(agent_bridge, "gather_preflight", return_value=preflight), \
                 mock.patch.object(agent_bridge, "_authoritative_runtime_available", return_value=True, create=True), \
                 mock.patch.object(agent_bridge.subprocess, "run", side_effect=AssertionError("legacy path used")):
                preview_code = agent_bridge.command_context(args)
                self.assertFalse(output.exists())
                self.assertEqual(
                    json.loads(latest_json.read_text(encoding="utf-8"))[
                        "lifecycle_status"
                    ],
                    "preview",
                )
                args.preview_id = "prv_one"
                args.preview_hash = "sha256:" + "1" * 64
                args.query = "forged writer request label"
                args.mode = "auto"
                code = agent_bridge.command_context(args)

            self.assertEqual(preview_code, 0)
            self.assertEqual(code, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "# authoritative\n")
            payload = json.loads(latest_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["context_id"], "ctx_one")
            self.assertEqual(payload["preview_id"], "prv_one")
            self.assertEqual(payload["runtime"], "living_self_v1.1")
            self.assertEqual(payload["query"], "authoritative review task")
            self.assertEqual(payload["task"], "authoritative review task")
            self.assertEqual(payload["mode"], "reviewer")
            self.assertEqual(payload["request_label"], "forged writer request label")
            compile_call = [item for item in calls if item[0] == "compile"][0]
            self.assertEqual(compile_call[1]["resolved_mode"], "reviewer")
            self.assertEqual(
                [item[0] for item in calls],
                ["init", "preview", "init", "get", "compile"],
            )

    def test_task_compile_parser_exposes_reviewable_preview_and_compile(self):
        parser = task_compile.build_parser()
        preview = parser.parse_args(["preview", "review plan", "--mode", "reviewer"])
        compile_args = parser.parse_args(
            [
                "compile",
                "review plan",
                "--preview-id",
                "prv_one",
                "--preview-hash",
                "sha256:" + "1" * 64,
            ]
        )

        self.assertEqual(preview.command, "preview")
        self.assertEqual(preview.mode, "reviewer")
        self.assertEqual(compile_args.preview_id, "prv_one")
        self.assertEqual(compile_args.preview_hash, "sha256:" + "1" * 64)

    def test_naked_task_compile_returns_preview_without_formal_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            args = task_compile.build_parser().parse_args(
                ["compile", "review plan", "--mode", "reviewer"]
            )
            with mock.patch.object(task_compile, "SESSIONS_DIR", sessions), \
                 mock.patch.object(task_compile, "command_preview", return_value=0) as preview:
                code = task_compile.command_compile(args)

            self.assertEqual(code, 0)
            preview.assert_called_once_with(args)
            self.assertFalse(sessions.exists())

    def test_approved_task_compile_redacts_query_from_all_session_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            latest_md = sessions / "latest.md"
            latest_json = sessions / "latest.json"
            bridge_json = root / "agent" / "latest-context.json"
            bridge_json.parent.mkdir(parents=True)
            bridge_json.write_text(
                json.dumps(
                    {
                        "runtime": "living_self_v1.1",
                        "preview_id": "prv_stale",
                        "preview_hash": "sha256:" + "9" * 64,
                        "context_id": "ctx_stale",
                        "content_hash": "sha256:" + "9" * 64,
                        "source_revision": {"claims_event_seq": 1},
                        "task": "stale global task",
                        "mode": "writer",
                        "lifecycle_status": "compiled",
                    }
                ),
                encoding="utf-8",
            )
            token = "Bearer " + "z" * 24
            email = "alice@example.com"
            home_path = "/Users/alice/secret.txt"
            raw_query = "review " + token + " " + email + " " + home_path
            args = task_compile.build_parser().parse_args(
                [
                    "compile",
                    raw_query,
                    "--mode",
                    "reviewer",
                    "--preview-id",
                    "prv_one",
                    "--preview-hash",
                    "sha256:" + "1" * 64,
                ]
            )

            def bridge_success(cmd, **_kwargs):
                output = Path(cmd[cmd.index("--output") + 1])
                metadata = Path(cmd[cmd.index("--metadata-output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("# safe authoritative context\n", encoding="utf-8")
                metadata.write_text(
                    json.dumps(
                        {
                            "runtime": "living_self_v1.1",
                            "preview_id": "prv_one",
                            "preview_hash": "sha256:" + "1" * 64,
                            "context_id": "ctx_one",
                            "content_hash": "sha256:" + "2" * 64,
                            "source_revision": {"claims_event_seq": 1},
                            "task": "approved business task",
                            "mode": "business",
                            "lifecycle_status": "compiled",
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

            stdout = io.StringIO()
            with mock.patch.object(task_compile, "SESSIONS_DIR", sessions), \
                 mock.patch.object(task_compile, "LATEST_MD", latest_md), \
                 mock.patch.object(task_compile, "LATEST_JSON", latest_json), \
                 mock.patch.object(task_compile, "BRIDGE_LATEST_JSON", bridge_json), \
                 mock.patch.object(task_compile.subprocess, "run", side_effect=bridge_success), \
                 contextlib.redirect_stdout(stdout):
                code = task_compile.command_compile(args)

            self.assertEqual(code, 0)
            persisted = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in sessions.rglob("*")
                if path.is_file()
            )
            path_names = "\n".join(str(path) for path in sessions.rglob("*"))
            combined = persisted + path_names + stdout.getvalue()
            for secret in (token, email, "/Users/alice"):
                self.assertNotIn(secret, combined)
            manifest = json.loads(latest_json.read_text(encoding="utf-8"))
            self.assertEqual(manifest["query"], "approved business task")
            self.assertEqual(manifest["mode"], "business")
            self.assertNotEqual(manifest["request_label"], manifest["query"])

    def test_auto_preview_requires_explicit_non_auto_mode_for_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest_json = root / "latest-context.json"
            preview = {
                "preview_id": "prv_auto",
                "preview_hash": "sha256:" + "3" * 64,
                "task": "auto authority task",
                "mode": "auto",
                "source_revision": {"claims_event_seq": 1},
                "sections": {"verified_facts": []},
            }
            compile_calls = []

            class Store:
                def get(self, _preview_id):
                    return dict(preview)

            class Compiler:
                def __init__(self, _vault):
                    self.context_store = Store()

                def compile(self, **kwargs):
                    compile_calls.append(kwargs)
                    raise AssertionError("compile must not run without explicit mode")

            args = argparse.Namespace(
                query="mutable query must not choose mode",
                since="2026-07-01",
                with_recall=False,
                timeout=1,
                output="",
                print=False,
                force=False,
                mode="auto",
                preview_only=False,
                preview_id="prv_auto",
                preview_hash="sha256:" + "3" * 64,
                exclude_item_id=[],
                ttl_seconds=900,
            )
            preflight = {"context_status": "ready", "vault_status": "healthy"}
            with mock.patch.object(agent_bridge, "ContextCompiler", Compiler), \
                 mock.patch.object(agent_bridge, "IMMORTAL_DIR", root), \
                 mock.patch.object(agent_bridge, "LATEST_CONTEXT_JSON", latest_json), \
                 mock.patch.object(agent_bridge, "gather_preflight", return_value=preflight), \
                 mock.patch.object(agent_bridge, "_authoritative_runtime_available", return_value=True):
                code = agent_bridge.command_context(args)

            self.assertEqual(code, 1)
            self.assertEqual(compile_calls, [])
            payload = json.loads(latest_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["error_code"], "unresolved_context_mode")


if __name__ == "__main__":
    unittest.main()
