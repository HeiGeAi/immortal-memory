# Live Dashboard Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make immortal-memory dashboard the real local seven-module product entrypoint. Retain old static HTML only under an explicitly named compatibility export.

**Architecture:** The CLI forwards host, port, vault, and browser-open options to profile_review.py, which binds the loopback HTTP server and reports the real bound URL. The old dashboard.py generator remains available only through dashboard-export and labels its output as non-live. agent-factory remains a v1.1 compatibility alias for the same real server contract.

**Tech Stack:** Python 3.9–3.12 standard library, argparse, http.server, pytest, clean-wheel install verification, Playwright browser acceptance, GitHub Actions.

---

## File map

| File | Responsibility |
|---|---|
| core/immortal.py | Canonical CLI contract, real-server forwarding, explicit static export. |
| tests/test_control_data.py | CLI red-green tests proving the default never dispatches the static generator. |
| tests/test_packaging.py | Package smoke test for the explicitly named static export. |
| README.md | Public start command and truthful static-export boundary. |
| docs/ARCHITECTURE.md | Canonical dashboard transport and compatibility boundary. |
| docs/PRODUCT.md | Product distinction between live evidence and legacy snapshot. |
| CHANGELOG.md | User-visible change record. |
| docs/releases/v1.1.2.md | Release evidence and known production boundary. |
| core/VERSION, pyproject.toml, README.md, tests/test_version.py | Version governance for 1.1.2. |

### Task 1: Lock the CLI behavior with failing tests

**Files:**

- Modify: tests/test_control_data.py, directly after test_agent_factory_forwards_explicit_vault_to_dashboard.
- Modify: core/immortal.py, command_dashboard, command_agent_factory, and parser registrations.

- [x] **Step 1: Write the live-dashboard forwarding test**

Add:

~~~python
def test_dashboard_forwards_live_server_args_without_static_snapshot(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "isolated-vault"
    captured = {}

    def fake_run_script(script, args=None):
        captured["script"] = script
        captured["args"] = args
        return 0

    monkeypatch.setattr(immortal, "run_script", fake_run_script)
    args = immortal.build_parser().parse_args(
        ["dashboard", "--vault-dir", str(vault), "--port", "0", "--open"]
    )

    assert args.func(args) == 0
    assert captured == {
        "script": "profile_review.py",
        "args": [
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--vault-dir",
            str(vault),
            "--open",
        ],
    }
    assert "dashboard.html" not in capsys.readouterr().out
~~~

- [x] **Step 2: Write the explicit legacy-export test**

Add:

~~~python
def test_dashboard_export_refreshes_and_labels_the_legacy_snapshot(tmp_path, monkeypatch, capsys):
    captured = {}

    def fake_run_script(script, args=None):
        captured["script"] = script
        captured["args"] = args
        return 0

    monkeypatch.setattr(immortal, "IMMORTAL_DIR", tmp_path / "vault")
    monkeypatch.setattr(immortal, "run_script", fake_run_script)
    args = immortal.build_parser().parse_args(["dashboard-export"])

    assert args.func(args) == 0
    assert captured == {"script": "dashboard.py", "args": None}
    output = capsys.readouterr().out
    assert "Legacy dashboard snapshot" in output
    assert "not live" in output
    assert str(tmp_path / "vault" / "dashboard.html") in output
~~~

- [x] **Step 3: Prove the tests are red**

Run:

~~~bash
PYTHONPATH=core python3 -m pytest tests/test_control_data.py::test_dashboard_forwards_live_server_args_without_static_snapshot tests/test_control_data.py::test_dashboard_export_refreshes_and_labels_the_legacy_snapshot -q
~~~

Historical baseline evidence: source at `df724c8` rejected the dashboard live-service arguments and rejected `dashboard-export`, both with exit `2`. The initial task-agent run did not preserve that red test correctly, so this independent historical probe was repeated before marking this step complete.

- [x] **Step 4: Implement one shared server-argument helper**

Replace the old static command with:

~~~python
def dashboard_server_args(args: argparse.Namespace) -> list[str]:
    server_args = ["--host", args.host, "--port", str(args.port)]
    if args.vault_dir:
        server_args.extend(["--vault-dir", args.vault_dir])
    if args.open:
        server_args.append("--open")
    return server_args


def command_dashboard(args) -> int:
    return run_script("profile_review.py", dashboard_server_args(args))


def command_dashboard_export(_args) -> int:
    result = run_script("dashboard.py")
    if result != 0:
        return result
    print(f"Legacy dashboard snapshot (not live): {IMMORTAL_DIR / 'dashboard.html'}")
    return 0
~~~

Replace the agent-factory implementation with:

~~~python
def command_agent_factory(args) -> int:
    return run_script("profile_review.py", dashboard_server_args(args))
~~~

Do not print a CLI-computed URL. profile_review.main owns the socket bind and reports the actual port, including when --port 0 is requested.

- [x] **Step 5: Register canonical dashboard and explicit export**

Replace the old dashboard parser registration with:

~~~python
    dashboard = sub.add_parser(
        "dashboard",
        help="Start the real local seven-module dashboard on loopback",
    )
    dashboard.add_argument(
        "--host",
        default="127.0.0.1",
        choices=("127.0.0.1",),
        help="Loopback address for the private local dashboard",
    )
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument(
        "--vault-dir",
        default="",
        help="Override the dashboard vault for isolated verification",
    )
    dashboard.add_argument("--open", action="store_true")
    dashboard.set_defaults(func=command_dashboard)
    sub.add_parser(
        "dashboard-export",
        help="Refresh and print the legacy static dashboard snapshot; it is not a live service",
    ).set_defaults(func=command_dashboard_export)
~~~

Keep agent-factory and its existing argument names, so existing scripts reach profile_review.py through the shared helper. Its `--host` must use the same `127.0.0.1` restriction, because the local product carries private vault data and does not support a remote bind.

- [x] **Step 6: Prove focused green**

Run:

~~~bash
PYTHONPATH=core python3 -m pytest tests/test_control_data.py::test_agent_factory_forwards_explicit_vault_to_dashboard tests/test_control_data.py::test_dashboard_forwards_live_server_args_without_static_snapshot tests/test_control_data.py::test_dashboard_export_refreshes_and_labels_the_legacy_snapshot -q
~~~

Expected: 3 passed.

- [x] **Step 7: Commit**

~~~bash
git add core/immortal.py tests/test_control_data.py
git commit -m "fix: make dashboard command start the live product"
~~~

### Task 2: Preserve the packaged static export only behind its explicit name

**Files:**

- Modify: tests/test_packaging.py, the static dashboard smoke invocation.

- [x] **Step 1: Change the wheel smoke command**

Replace the static export invocation with:

~~~python
            dashboard = subprocess.run(
                [str(command), "dashboard-export"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(
                dashboard.returncode,
                0,
                msg=f"stdout:\n{dashboard.stdout}\nstderr:\n{dashboard.stderr}",
            )
            self.assertIn("Legacy dashboard snapshot (not live)", dashboard.stdout)
~~~

Leave the existing dashboard.html body assertions in place. They now prove that a user must intentionally call dashboard-export to make a static artifact.

- [x] **Step 2: Run the package contract**

Run:

~~~bash
PYTHONPATH=core python3 -m pytest tests/test_packaging.py -q
~~~

Expected: PASS with no server left running.

- [x] **Step 3: Commit**

~~~bash
git add tests/test_packaging.py
git commit -m "test: name legacy dashboard export explicitly"
~~~

### Task 3: Document the live versus snapshot boundary and release as 1.1.2

**Files:**

- Modify: README.md, docs/ARCHITECTURE.md, docs/PRODUCT.md, CHANGELOG.md.
- Create: docs/releases/v1.1.2.md.
- Modify: core/VERSION, pyproject.toml, tests/test_version.py.

- [x] **Step 1: Make version governance fail first**

In tests/test_version.py, replace hard-coded 1.1.1 expectations with:

~~~python
        self.assertEqual(version, "1.1.2")
        self.assertEqual(output.getvalue().strip(), "immortal 1.1.2")
~~~

- [x] **Step 2: Prove version test red**

Run:

~~~bash
PYTHONPATH=core python3 -m pytest tests/test_version.py::VersionGovernanceTest::test_source_versions_and_cli_are_consistent -q
~~~

Expected: FAIL because source and package version are still 1.1.1.

- [x] **Step 3: Update version and README quick start**

Set core/VERSION and pyproject.toml version to 1.1.2. Change the README badge to v1.1.2, update its wheel glob to immortal_memory-1.1.2, and replace the dashboard section with:

~~~markdown
Open the real local dashboard:

~~~bash
immortal-memory dashboard --open
~~~

The command starts the loopback-only service and opens the actual seven-module product. It is backed by live local APIs, so the System module can show current, partial, failed, or unknown evidence rather than a generated success card.

immortal-memory agent-factory remains a v1.1 compatibility alias for the same local product service. Use immortal-memory dashboard-export only when you intentionally need the legacy static HTML snapshot. That file is not a live dashboard and cannot prove current health.
~~~

- [x] **Step 4: Add architecture and product constraints**

Append after the seven-module list in docs/ARCHITECTURE.md:

~~~markdown
The canonical user entrypoint is immortal-memory dashboard, which starts the loopback HTTP service and renders current /api/v2 evidence. agent-factory is a compatibility alias during v1.1. dashboard-export creates the retained legacy static HTML only on explicit request; it is a snapshot, is never a health proof, and must not be presented as the current product surface.
~~~

Append after the health-state paragraph in docs/PRODUCT.md:

~~~markdown
The product opens through immortal-memory dashboard, not through a generated file. The legacy dashboard-export output is intentionally marked as non-live so a user cannot mistake historical HTML for a running system.
~~~

- [x] **Step 5: Write audited release note and changelog**

Create docs/releases/v1.1.2.md:

~~~markdown
# Immortal Memory v1.1.2

## What changed

- immortal-memory dashboard now starts the real loopback seven-module product service.
- immortal-memory agent-factory remains a compatibility alias for that service during v1.1.
- immortal-memory dashboard-export is the only command that creates the legacy static snapshot and labels it as non-live.

## Why it matters

The default entrypoint no longer directs a user to stale generated HTML. Current collection, API, scheduler, backup, and recovery evidence remain visible through the actual local service, where partial and unknown conditions stay distinct from healthy.

## Boundaries

- The service binds to loopback by default and must not be exposed through a public reverse proxy.
- A static export is for intentional offline reference only and is not proof of health, backup, or recovery.
- Production migration remains blocked until the verified encrypted external recovery drill passes.
~~~

Add a 1.1.2 section at the top of CHANGELOG.md with those same three user-visible changes.

- [x] **Step 6: Prove docs and version green**

Run:

~~~bash
PYTHONPATH=core python3 -m pytest tests/test_version.py tests/test_packaging.py tests/test_control_data.py -q
git diff --check
~~~

Expected: tests pass and git diff --check prints nothing.

- [x] **Step 7: Commit**

~~~bash
git add README.md docs/ARCHITECTURE.md docs/PRODUCT.md docs/releases/v1.1.2.md CHANGELOG.md core/VERSION pyproject.toml tests/test_version.py
git commit -m "docs: release live dashboard entrypoint v1.1.2"
~~~

### Task 4: Verify the real service and release artifact end to end

**Files:**

- Verify only: all changed files above.

- [x] **Step 1: Run full regression**

Run:

~~~bash
PYTHONPATH=core python3 -m pytest -q
~~~

Evidence: `1304 passed in 92.64s` after the first-use bootstrap, legacy gate, package, and UI changes.

- [x] **Step 2: Verify canonical command against an isolated vault**

Start:

~~~bash
TMP_HOME="$(mktemp -d /tmp/immortal-dashboard.XXXXXX)"
HOME="$TMP_HOME" PYTHONPATH=core python3 core/immortal.py dashboard --vault-dir "$TMP_HOME/vault" --port 0
~~~

In a second terminal:

~~~bash
curl --fail --silent --show-error <printed-dashboard-url> | rg "IMMORTAL"
curl --fail --silent --show-error <printed-dashboard-url>/api/v2/system | python3 -m json.tool
~~~

Evidence: a fresh isolated `init -> train --smoke -> dashboard --port 0` returned the product shell, one real remembered record, and a `ready` product status. The server was stopped cleanly with exit `130`.

- [x] **Step 3: Build and scan artifacts**

Run:

~~~bash
BUILD_DIR="$(mktemp -d /tmp/immortal-build.XXXXXX)"
python3 -m build --outdir "$BUILD_DIR"
PYTHONPATH=core python3 core/private_scan.py "$BUILD_DIR"
~~~

Evidence: wheel and sdist built cleanly after explicit asset-package declaration, with `private_scan=ok` and no Setuptools package-discovery warning.

- [x] **Step 4: Verify clean install plus explicit snapshot**

Run:

~~~bash
CLEAN_HOME="$(mktemp -d /tmp/immortal-clean-home.XXXXXX)"
python3 -m venv "$CLEAN_HOME/venv"
WHEEL="$(find "$BUILD_DIR" -maxdepth 1 -name 'immortal_memory-1.1.2-*.whl' | head -n 1)"
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/python" -m pip install "$WHEEL"
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/immortal-memory" init --owner-display-name "Clean Install" --alias "clean"
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/immortal-memory" train --smoke
HOME="$CLEAN_HOME" "$CLEAN_HOME/venv/bin/immortal-memory" dashboard-export
~~~

Evidence: a clean wheel installation created an active first-use marker and trusted SQLite index, served a live dashboard with Home and System APIs healthy, then exported a snapshot only through `dashboard-export`.

- [x] **Step 5: Run browser and performance acceptance**

Run:

~~~bash
IMMORTAL_BROWSER_EVIDENCE="$PWD/output/playwright/v1.1.2-live-dashboard" PYTHONPATH=core python3 tests/browser_v11_acceptance.py
~~~

Evidence: 52 browser contracts passed in 24.89 seconds with zero console errors, seven views, restart persistence, and 470,000-row p95 checks all green.

- [ ] **Step 6: Prepare review without an unapproved release tag**

Run:

~~~bash
git status --short
git log --oneline origin/main..HEAD
git diff --check origin/main...HEAD
~~~

Expected: only intentional commits and no whitespace errors. Do not create a GitHub release tag without explicit release confirmation.

### Task 4a: Make a fresh installation usable without weakening the legacy gate

**Files:**

- Modify: core/immortal.py, pyproject.toml, README.md, docs/ARCHITECTURE.md, docs/releases/v1.1.2.md, CHANGELOG.md.
- Add: tests/test_product_bootstrap.py.
- Modify: tests/test_v11_packaging.py.

- [x] **Step 1: Reproduce the fresh-vault failure**

A clean installed wheel could start the live server, but `/api/v2/home` returned `503 index_unavailable` after the documented `init -> train --smoke` path. This was treated as a product defect, not a successful smoke test.

- [x] **Step 2: Lock safe first-use behavior with red tests**

Tests require a marker only for a vault empty before `init`, a trusted SQLite index after first training, no automatic rebuild for an existing vault, and a clean wheel dashboard whose Home API is usable.

- [x] **Step 3: Implement the first-use marker and index reconciliation**

`init` writes a safe local marker only for a pristine vault. `train` recognizes only that marker, reconciles the trusted derived SQLite index immediately before the product pipeline, records the index update, and leaves any pre-existing vault untouched. The marker writer normalizes macOS system aliases without accepting arbitrary symlinks.

- [x] **Step 4: Verify package behavior and package structure**

The clean-wheel test starts the actual loopback dashboard, calls its Home API, and tears down the entire process group. The package explicitly lists its asset namespaces, eliminating Setuptools discovery warnings while retaining all product assets.
