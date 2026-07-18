import json
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import feishu_drive_mirror
import file_utils
import profile


class AtomicOutputTest(unittest.TestCase):
    def test_failed_replace_preserves_existing_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile_compact.md"
            path.write_text("stable", encoding="utf-8")

            with mock.patch.object(file_utils.os, "replace", side_effect=OSError("disk fault")):
                with self.assertRaisesRegex(OSError, "disk fault"):
                    file_utils.atomic_write_text(path, "new body")

            self.assertEqual(path.read_text(encoding="utf-8"), "stable")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_profile_routes_all_outputs_through_atomic_writer(self):
        payload = {
            "generated_at": "2026-07-18T00:00:00Z",
            "version": "test",
            "authority_order": [],
            "sections": [],
            "raw_profile_evidence": [],
            "recent_profile_evidence": [],
            "recent_profile_facts": [],
            "reviewed_profile_memories": [],
            "known_gaps": [],
            "missing_memory_files": [],
        }
        with mock.patch.object(profile, "atomic_write_text") as writer:
            profile.write_outputs(payload)

        self.assertEqual(writer.call_count, 3)


class MirrorResumeTest(unittest.TestCase):
    def make_mirror(self, root):
        mirror = object.__new__(feishu_drive_mirror.FeishuMirror)
        mirror.args = types.SimpleNamespace(overwrite=False)
        mirror.run_lark = mock.Mock()
        return mirror

    def row(self, action):
        return {
            "action": action,
            "token": "token-1",
            "obj_type": "docx",
            "title": "Document",
            "object_key": "drive:docx:token-1",
        }

    def test_pending_export_overwrites_existing_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = self.make_mirror(root)
            with mock.patch.object(feishu_drive_mirror, "MIRROR_DIR", root):
                expected = root / "exports" / "docx" / mirror.output_base(
                    self.row("export_docx"), ".docx"
                ).name
                expected.parent.mkdir(parents=True)
                expected.write_bytes(b"partial")

                mirror.run_one_job(self.row("export_docx"))

            argv = mirror.run_lark.call_args.args[0]
            self.assertIn("--overwrite", argv)

    def test_pending_download_overwrites_existing_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = self.make_mirror(root)
            with mock.patch.object(feishu_drive_mirror, "MIRROR_DIR", root):
                expected = root / "files" / mirror.output_base(
                    self.row("download_file"), ""
                )
                expected.parent.mkdir(parents=True)
                expected.write_bytes(b"partial")

                mirror.run_one_job(self.row("download_file"))

            argv = mirror.run_lark.call_args.args[0]
            self.assertIn("--overwrite", argv)


if __name__ == "__main__":
    unittest.main()
