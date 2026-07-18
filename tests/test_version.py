import contextlib
import io
import unittest
from pathlib import Path

import immortal


class VersionGovernanceTest(unittest.TestCase):
    def test_cli_version_matches_version_file(self):
        version_path = Path(__file__).resolve().parent.parent / "core" / "VERSION"
        version = version_path.read_text(encoding="utf-8").strip()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                immortal.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(version, "0.8.0")
        self.assertIn(version, output.getvalue())


if __name__ == "__main__":
    unittest.main()
