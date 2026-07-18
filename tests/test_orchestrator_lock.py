import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import orchestrator


class OrchestratorLockTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.lock_file = self.root / "orchestrator.lock"
        self.log_file = self.root / "backup.log"
        self.patchers = [
            mock.patch.object(orchestrator, "LOCK_FILE", self.lock_file),
            mock.patch.object(orchestrator, "LOG_FILE", self.log_file),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        orchestrator.release_lock()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()

    def make_stale(self, body):
        self.lock_file.write_text(body, encoding="utf-8")
        old = time.time() - 120
        os.utime(self.lock_file, (old, old))

    def test_stale_empty_lock_is_removed_and_reacquired(self):
        self.make_stale("")

        self.assertTrue(orchestrator.acquire_lock())
        self.assertEqual(int(self.lock_file.read_text().split()[0]), os.getpid())

    def test_stale_malformed_lock_is_removed_and_reacquired(self):
        self.make_stale("not-a-pid")

        self.assertTrue(orchestrator.acquire_lock())
        self.assertEqual(int(self.lock_file.read_text().split()[0]), os.getpid())

    def test_live_pid_lock_is_not_removed(self):
        self.lock_file.write_text(f"{os.getpid()} now\n", encoding="utf-8")

        self.assertFalse(orchestrator.acquire_lock())
        self.assertTrue(self.lock_file.exists())


if __name__ == "__main__":
    unittest.main()
