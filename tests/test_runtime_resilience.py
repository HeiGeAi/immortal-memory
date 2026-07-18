import argparse
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_bridge
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
            )
            timeout = subprocess.TimeoutExpired(
                cmd=["python3", "immortal.py"],
                timeout=1,
                output="partial output",
                stderr="still running",
            )
            latest_json = Path(tmp) / "latest-context.json"
            with mock.patch.object(agent_bridge.subprocess, "run", side_effect=timeout), \
                 mock.patch.object(agent_bridge, "LATEST_CONTEXT_JSON", latest_json):
                code = agent_bridge.command_context(args)

            self.assertEqual(code, 1)
            body = output.read_text(encoding="utf-8")
            self.assertIn("timed out", body.lower())
            self.assertIn("Exit code: 1", body)


if __name__ == "__main__":
    unittest.main()
