import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import state_store


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


if __name__ == "__main__":
    unittest.main()
