"""Console entry point for the packaged Immortal Memory runtime."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _legacy_migration(command: str) -> int:
    if command == "package":
        payload = {
            "ok": False,
            "code": "legacy_package_removed",
            "migration": (
                "Build the wheel and source archive from a clean tracked Git "
                "commit, then use the published release artifacts."
            ),
            "documentation": "https://github.com/HeiGeAi/immortal-memory#readme",
        }
    else:
        payload = {
            "ok": False,
            "code": "legacy_project_extension_required",
            "migration": (
                "Install a separately audited local project extension and "
                "invoke it with immortal-project."
            ),
            "documentation": "https://github.com/HeiGeAi/immortal-memory#readme",
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"package", "project"}:
        return _legacy_migration(sys.argv[1])
    runtime_dir = str(Path(__file__).resolve().parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)

    from immortal import main as immortal_main

    return immortal_main()


if __name__ == "__main__":
    raise SystemExit(main())
