import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import state_store


def _hold_state_lock(path, ready, release):
    def delayed_update(current):
        ready.set()
        release.wait(timeout=5)
        current["holder"] = True
        return current

    state_store.mutate_state_atomic(
        Path(path), delayed_update, timeout=1.0, stale_after=0.0
    )


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "orchestrator_state.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_concurrent_writers_preserve_disjoint_keys(self):
        errors = []

        def worker(number):
            try:
                state_store.update_state_atomic(self.path, {f"key_{number}": number})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(number,)) for number in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(state, {f"key_{number}": number for number in range(20)})

    def test_failed_replace_preserves_previous_state(self):
        original = {"stable": True}
        self.path.write_text(json.dumps(original), encoding="utf-8")

        with mock.patch.object(state_store.os, "replace", side_effect=OSError("disk fault")):
            with self.assertRaisesRegex(OSError, "disk fault"):
                state_store.update_state_atomic(self.path, {"new": "value"})

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)
        leftovers = list(self.path.parent.glob(f".{self.path.name}.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_invalid_json_is_not_silently_overwritten(self):
        self.path.write_text("{broken", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            state_store.update_state_atomic(self.path, {"new": "value"})

        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_live_owner_is_not_reclaimed_only_because_lock_is_old(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        holder = context.Process(
            target=_hold_state_lock, args=(str(self.path), ready, release)
        )
        holder.start()
        self.addCleanup(lambda: holder.is_alive() and holder.terminate())
        self.assertTrue(ready.wait(timeout=3), "holder did not acquire lock")

        with self.assertRaises(TimeoutError):
            state_store.update_state_atomic(
                self.path,
                {"contender": True},
                timeout=0.15,
                stale_after=0.0,
            )

        release.set()
        holder.join(timeout=3)
        self.assertFalse(holder.is_alive())
        self.assertEqual(holder.exitcode, 0)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")), {"holder": True}
        )

    def test_incomplete_lock_publication_is_not_reclaimed(self):
        identity_started = threading.Event()
        allow_identity = threading.Event()
        contender_entered = threading.Event()
        release_contender = threading.Event()
        holder_errors = []
        contender_errors = []
        entered = []

        def delayed_process_identity(_pid):
            if threading.current_thread().name == "lock-owner":
                identity_started.set()
                allow_identity.wait(timeout=3)
            return "stable-process-start"

        def holder():
            try:
                state_store.mutate_state_atomic(
                    self.path,
                    lambda current: {**current, "holder": entered.append("holder") or True},
                    timeout=0.15,
                    stale_after=0.0,
                )
            except Exception as exc:
                holder_errors.append(exc)

        def contender_mutator(current):
            entered.append("contender")
            contender_entered.set()
            release_contender.wait(timeout=3)
            current["contender"] = True
            return current

        def contender():
            try:
                state_store.mutate_state_atomic(
                    self.path,
                    contender_mutator,
                    timeout=1.0,
                    stale_after=0.0,
                )
            except Exception as exc:
                contender_errors.append(exc)

        with mock.patch.object(
            state_store,
            "_process_start_identity",
            side_effect=delayed_process_identity,
        ):
            owner = threading.Thread(target=holder, name="lock-owner")
            owner.start()
            self.assertTrue(
                identity_started.wait(timeout=2),
                "owner did not reach lock identity publication",
            )
            competing_writer = threading.Thread(
                target=contender,
                name="lock-contender",
            )
            competing_writer.start()
            self.assertTrue(
                contender_entered.wait(timeout=2),
                "contender did not enter while owner publication was paused",
            )
            try:
                allow_identity.set()
                owner.join(timeout=2)
            finally:
                release_contender.set()
                competing_writer.join(timeout=3)

        self.assertFalse(owner.is_alive())
        self.assertFalse(competing_writer.is_alive())
        self.assertEqual(contender_errors, [])
        self.assertEqual(len(holder_errors), 1)
        self.assertIsInstance(holder_errors[0], TimeoutError)
        self.assertEqual(entered, ["contender"])

    def test_dead_owner_lock_is_reclaimed(self):
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.write_text(
            json.dumps({"pid": 99999999, "created_at": 0}), encoding="utf-8"
        )
        os.utime(lock_path, (0, 0))

        result = state_store.update_state_atomic(
            self.path, {"recovered": True}, timeout=0.5, stale_after=0.0
        )

        self.assertEqual(result, {"recovered": True})
        self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
