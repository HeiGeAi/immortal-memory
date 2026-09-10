import unittest
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import agent_bridge_server


class AgentBridgeCorsTest(unittest.TestCase):
    def test_mcp_server_reports_product_version(self):
        expected = (Path(agent_bridge_server.__file__).with_name("VERSION")).read_text(
            encoding="utf-8"
        ).strip()
        response = agent_bridge_server.handle_mcp_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )

        self.assertEqual(response["result"]["serverInfo"]["version"], expected)

    def test_exact_loopback_origins_are_allowed(self):
        self.assertTrue(agent_bridge_server.is_allowed_origin("http://localhost:8799"))
        self.assertTrue(agent_bridge_server.is_allowed_origin("https://127.0.0.1:443"))
        self.assertTrue(agent_bridge_server.is_allowed_origin("http://[::1]:8799"))

    def test_deceptive_or_credentialed_origins_are_rejected(self):
        self.assertFalse(agent_bridge_server.is_allowed_origin("http://localhost.evil.com"))
        self.assertFalse(agent_bridge_server.is_allowed_origin("http://127.0.0.1.evil.test"))
        self.assertFalse(agent_bridge_server.is_allowed_origin("http://user@localhost:8799"))
        self.assertFalse(agent_bridge_server.is_allowed_origin("null"))

    def test_non_http_schemes_are_rejected(self):
        self.assertFalse(agent_bridge_server.is_allowed_origin("file://localhost/tmp"))
        self.assertFalse(agent_bridge_server.is_allowed_origin("javascript://localhost"))

    def test_preview_response_never_reuses_previous_context_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_md = root / "latest-context.md"
            latest_json = root / "latest-context.json"
            stale_md.write_text("# stale private context\n", encoding="utf-8")
            latest_json.write_text(
                json.dumps(
                    {
                        "runtime": "living_self_v1.1",
                        "lifecycle_status": "preview",
                        "preview_id": "prv_one",
                        "preview_hash": "sha256:" + "1" * 64,
                        "context_md": None,
                    }
                ),
                encoding="utf-8",
            )
            def scoped_preview(args, **_kwargs):
                metadata = Path(args[args.index("--metadata-output") + 1])
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text(latest_json.read_text(encoding="utf-8"), encoding="utf-8")
                latest_json.write_text(
                    json.dumps(
                        {
                            "runtime": "living_self_v1.1",
                            "lifecycle_status": "compiled",
                            "context_id": "ctx_other",
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    ["agent_bridge.py"], 0, stdout="preview_id=prv_one\n", stderr=""
                )
            with mock.patch.object(agent_bridge_server, "LATEST_CONTEXT_MD", stale_md), \
                 mock.patch.object(agent_bridge_server, "LATEST_CONTEXT_JSON", latest_json), \
                 mock.patch.object(agent_bridge_server, "AGENT_DIR", root), \
                 mock.patch.object(agent_bridge_server, "run_bridge_cli", side_effect=scoped_preview, create=True):
                result = agent_bridge_server.build_context("review plan")

            self.assertTrue(result["ok"])
            self.assertEqual(result["lifecycle_status"], "preview")
            self.assertEqual(result["context"], "")
            self.assertIsNone(result["paths"]["context_md"])
            self.assertNotIn("stale private context", json.dumps(result))

    def test_legacy_context_uses_request_scoped_output_not_global_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_json = root / "latest-context.json"
            global_md = root / "latest-context.md"
            global_json.write_text("{}", encoding="utf-8")
            global_md.write_text("# wrong global context\n", encoding="utf-8")

            def legacy_run(args, **_kwargs):
                metadata = Path(args[args.index("--metadata-output") + 1])
                output = Path(args[args.index("--output") + 1])
                metadata.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("# request A legacy context\n", encoding="utf-8")
                metadata.write_text(
                    json.dumps(
                        {
                            "runtime": "legacy_v1",
                            "query": "request A",
                            "context_md": str(output),
                        }
                    ),
                    encoding="utf-8",
                )
                global_md.write_text("# request B global context\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with mock.patch.object(agent_bridge_server, "AGENT_DIR", root), \
                 mock.patch.object(agent_bridge_server, "LATEST_CONTEXT_JSON", global_json), \
                 mock.patch.object(agent_bridge_server, "LATEST_CONTEXT_MD", global_md), \
                 mock.patch.object(agent_bridge_server, "run_bridge_cli", side_effect=legacy_run):
                result = agent_bridge_server.build_context("request A")

            self.assertTrue(result["ok"])
            self.assertEqual(result["lifecycle_status"], "legacy")
            self.assertEqual(result["context"], "# request A legacy context\n")
            self.assertNotIn("request B global context", json.dumps(result))

    def test_each_request_reads_only_its_own_preview_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata_paths = []

            def scoped_run(args, **_kwargs):
                task = args[1]
                metadata = Path(args[args.index("--metadata-output") + 1])
                metadata_paths.append(metadata)
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text(
                    json.dumps(
                        {
                            "runtime": "living_self_v1.1",
                            "lifecycle_status": "preview",
                            "task": task,
                            "preview_id": "prv_" + task[-1],
                            "preview_hash": "sha256:" + task[-1] * 64,
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with mock.patch.object(agent_bridge_server, "AGENT_DIR", root), \
                 mock.patch.object(
                     agent_bridge_server, "run_bridge_cli", side_effect=scoped_run
                 ):
                first = agent_bridge_server.build_context("request A")
                second = agent_bridge_server.build_context("request B")

            self.assertEqual(first["task"], "request A")
            self.assertEqual(second["task"], "request B")
            self.assertEqual(first["metadata"]["preview_id"], "prv_A")
            self.assertEqual(second["metadata"]["preview_id"], "prv_B")
            self.assertEqual(len(set(metadata_paths)), 2)

    def test_timeout_returns_redacted_failure_and_cleans_request_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_root = root / "requests" / "req_timeout"
            secret = "Bearer " + "z" * 24

            def timed_out(args, **_kwargs):
                metadata = Path(args[args.index("--metadata-output") + 1])
                output = Path(args[args.index("--output") + 1])
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text('{"partial": true}', encoding="utf-8")
                output.write_text("partial", encoding="utf-8")
                raise subprocess.TimeoutExpired(
                    args,
                    1,
                    output="partial " + secret + " /Users/" + "alice/private.txt",
                    stderr=secret,
                )

            fake_uuid = mock.Mock(hex="timeout")
            with mock.patch.object(agent_bridge_server, "AGENT_DIR", root), \
                 mock.patch.object(agent_bridge_server.uuid, "uuid4", return_value=fake_uuid), \
                 mock.patch.object(
                     agent_bridge_server, "run_bridge_cli", side_effect=timed_out
                 ):
                result = agent_bridge_server.build_context("review " + secret, timeout=1)

            self.assertFalse(result["ok"])
            self.assertEqual(result["exit_code"], 124)
            self.assertEqual(result["error_code"], "bridge_timeout")
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn("/Users/" + "alice", json.dumps(result))
            self.assertFalse(request_root.exists())

    def test_audit_event_redacts_previews_before_writing_access_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_log = root / "access.log"
            latest = root / "access_latest.json"
            token = "Bearer " + "z" * 24
            home = "/Users/" + "alice/private.txt"
            with mock.patch.object(agent_bridge_server, "AGENT_DIR", root), \
                 mock.patch.object(agent_bridge_server, "AUDIT_LOG", access_log), \
                 mock.patch.object(agent_bridge_server, "AUDIT_LATEST", latest):
                agent_bridge_server.audit_event(
                    {
                        "transport": "http",
                        "action": "agent_context",
                        "task_preview": "review " + token + " " + home,
                    }
                )

            persisted = access_log.read_text(encoding="utf-8") + latest.read_text(
                encoding="utf-8"
            )
            self.assertNotIn(token, persisted)
            self.assertNotIn("/Users/" + "alice", persisted)
            self.assertIn("[REDACTED", persisted)

    def test_compiled_response_reads_only_verified_canonical_ready_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_md = root / "latest-context.md"
            latest_json = root / "latest-context.json"
            stale_md.write_text("# stale context\n", encoding="utf-8")
            latest_json.write_text(
                json.dumps(
                    {
                        "runtime": "living_self_v1.1",
                        "lifecycle_status": "compiled",
                        "context_id": "ctx_one",
                        "content_hash": "sha256:" + "2" * 64,
                        "task": "approved task",
                        "mode": "reviewer",
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                ["agent_bridge.py"], 0, stdout="context_id=ctx_one\n", stderr=""
            )
            def scoped_compiled(args, **_kwargs):
                metadata = Path(args[args.index("--metadata-output") + 1])
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text(latest_json.read_text(encoding="utf-8"), encoding="utf-8")
                return completed
            with mock.patch.object(agent_bridge_server, "LATEST_CONTEXT_MD", stale_md), \
                 mock.patch.object(agent_bridge_server, "LATEST_CONTEXT_JSON", latest_json), \
                 mock.patch.object(agent_bridge_server, "AGENT_DIR", root), \
                 mock.patch.object(agent_bridge_server, "run_bridge_cli", side_effect=scoped_compiled), \
                 mock.patch.object(
                     agent_bridge_server,
                     "load_ready_context",
                     return_value=(
                         "# verified canonical context\n",
                         str(root / "packs" / "ctx_one" / "TASK_CONTEXT.md"),
                         str(root / "packs" / "ctx_one" / "context.json"),
                     ),
                 ):
                result = agent_bridge_server.build_context(
                    "mutable request label",
                    mode="reviewer",
                    preview_id="prv_one",
                    preview_hash="sha256:" + "1" * 64,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["context"], "# verified canonical context\n")
            self.assertNotIn("stale context", json.dumps(result))
            self.assertIn("packs/ctx_one/TASK_CONTEXT.md", result["paths"]["context_md"])

    def test_ready_delivery_uses_verified_body_without_reopening_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attacker = root / "attacker.md"
            attacker.write_text("# attacker replacement\n", encoding="utf-8")
            canonical = root / "packs" / "ctx_one" / "TASK_CONTEXT.md"
            canonical.parent.mkdir(parents=True)
            canonical.symlink_to(attacker)
            canonical_json = canonical.parent / "context.json"

            class Compiler:
                def __init__(self, _vault):
                    pass

                def load_compiled(self, _context_id):
                    return {
                        "context_id": "ctx_one",
                        "content_hash": "sha256:" + "2" * 64,
                        "lifecycle_status": "compiled",
                        "context_md": str(canonical),
                        "context_json": str(canonical_json),
                        "context_markdown": "# verified safe-read body\n",
                        "context_markdown_hash": "sha256:"
                        + hashlib.sha256(b"# verified safe-read body\n").hexdigest(),
                    }

            payload = {
                "context_id": "ctx_one",
                "content_hash": "sha256:" + "2" * 64,
                "context_markdown_hash": "sha256:"
                + hashlib.sha256(b"# verified safe-read body\n").hexdigest(),
            }
            with mock.patch.object(agent_bridge_server, "ContextCompiler", Compiler):
                content, context_md, context_json = agent_bridge_server.load_ready_context(
                    payload
                )

            self.assertEqual(content, "# verified safe-read body\n")
            self.assertEqual(context_md, str(canonical))
            self.assertEqual(context_json, str(canonical_json))
            self.assertNotIn("attacker", content)

    def test_invalid_ready_verification_never_falls_back_to_stdout_or_latest_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_md = root / "latest-context.md"
            latest_json = root / "latest-context.json"
            stale_md.write_text("# stale context\n", encoding="utf-8")
            latest_json.write_text(
                json.dumps(
                    {
                        "runtime": "living_self_v1.1",
                        "lifecycle_status": "compiled",
                        "context_id": "ctx_one",
                        "content_hash": "sha256:" + "2" * 64,
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                ["agent_bridge.py"], 0, stdout="context_id=ctx_one\n", stderr=""
            )
            def scoped_invalid(args, **_kwargs):
                metadata = Path(args[args.index("--metadata-output") + 1])
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text(latest_json.read_text(encoding="utf-8"), encoding="utf-8")
                return completed
            with mock.patch.object(agent_bridge_server, "LATEST_CONTEXT_MD", stale_md), \
                 mock.patch.object(agent_bridge_server, "LATEST_CONTEXT_JSON", latest_json), \
                 mock.patch.object(agent_bridge_server, "AGENT_DIR", root), \
                 mock.patch.object(agent_bridge_server, "run_bridge_cli", side_effect=scoped_invalid), \
                 mock.patch.object(
                     agent_bridge_server,
                     "load_ready_context",
                     side_effect=ValueError("READY mismatch"),
                 ):
                result = agent_bridge_server.build_context("review plan")

            self.assertFalse(result["ok"])
            self.assertEqual(result["context"], "")
            self.assertIsNone(result["paths"]["context_md"])
            self.assertIn("READY mismatch", result["stderr"])
            self.assertNotIn("stale context", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
