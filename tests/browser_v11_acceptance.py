#!/usr/bin/env python3
"""Real browser, HTTP-contract, and 470k-row performance acceptance for v1.1."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
TESTS = ROOT / "tests"
for candidate in (str(CORE), str(TESTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from export_restore import prewarm_index_verification, sha256_file
from index_integrity import INDEX_SCHEMA_VERSION, ids_sha256
from claim_store import ClaimStore
from living_self_service import LivingSelfService
from model_types import new_claim
from product_data import ProductData
from product_mutations import ProductMutationCoordinator
from test_product_ui_v2 import _start_server
from test_v11_migration import _seed_trusted_index


ROW_COUNT = 470_000
PLAYWRIGHT = (
    Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    / "skills/playwright/scripts/playwright_cli.sh"
)


def _run(command, *, cwd=ROOT, env=None, timeout=300):
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _browser_acceptance(base: str, evidence: Path) -> dict:
    if not PLAYWRIGHT.is_file():
        raise RuntimeError("Playwright CLI is unavailable: " + str(PLAYWRIGHT))
    session = "immortal-v11-" + uuid.uuid4().hex[:10]
    evidence.mkdir(parents=True, exist_ok=True)
    desktop = evidence / "desktop.png"
    mobile = evidence / "mobile.png"
    opened = _run(
        [PLAYWRIGHT, f"-s={session}", "open", base],
        cwd=evidence,
        timeout=120,
    )
    if opened.returncode != 0:
        raise RuntimeError(opened.stdout + opened.stderr)
    code = f"""async (page) => {{
      const failures = [];
      const consoleErrors = [];
      page.on('console', msg => {{ if (msg.type() === 'error') consoleErrors.push(msg.text()); }});
      const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};
      const base = {json.dumps(base)};
      await page.setViewportSize({{width: 1512, height: 982}});
      const response = await page.goto(base + '/?view=home', {{waitUntil: 'networkidle'}});
      assert(response.status() === 200, 'home document failed');
      const csp = response.headers()['content-security-policy'] || '';
      assert(!csp.includes('unsafe-inline'), 'CSP permits unsafe-inline');
      assert(response.headers()['cache-control'] === 'no-store', 'HTML is cacheable');
      await page.waitForSelector('.confirmation-card');
      const homeBeforeConfirm = await (await page.request.get(base + '/api/v2/home')).json();
      assert(homeBeforeConfirm.needs_confirmation.some(item => item.kind === 'claim'), 'candidate Claim is absent from Home');
      await page.locator('.confirmation-card').first().getByRole('button', {{name: '确认收录'}}).click();
      await page.locator('textarea[name="reason"]').fill('浏览器确认这条 Claim');
      await page.locator('#drawer').getByRole('button', {{name: '确认收录'}}).click();
      await page.waitForFunction(() => document.querySelector('#drawer')?.getAttribute('aria-hidden') === 'true');
      const homeAfterConfirm = await (await page.request.get(base + '/api/v2/home')).json();
      assert(!homeAfterConfirm.needs_confirmation.some(item => item.id === 'clm_browser_release'), 'Claim confirmation did not persist');
      const trustBeforeCorrection = await (await page.request.get(base + '/api/v2/trust')).json();
      const views = ['home','memories','self','judgments','use','trust','system'];
      for (const view of views) {{
        await page.locator(`.nav-link[data-view="${{view}}"]`).click();
        await page.waitForFunction(v => location.search.includes(`view=${{v}}`), view);
        assert(await page.locator(`.nav-link[data-view="${{view}}"]`).getAttribute('aria-current') === 'page', `inactive view ${{view}}`);
        await page.reload({{waitUntil: 'networkidle'}});
        assert(page.url().includes(`view=${{view}}`), `refresh lost ${{view}}`);
      }}
      await page.goBack({{waitUntil: 'networkidle'}});
      await page.goForward({{waitUntil: 'networkidle'}});
      assert(page.url().includes('view=system'), 'history navigation failed');

      await page.route('**/api/v2/home', async route => {{
        await page.waitForTimeout(250);
        await route.continue();
      }});
      await page.locator('.nav-link[data-view="home"]').click();
      await page.locator('.nav-link[data-view="system"]').click();
      await page.waitForTimeout(400);
      assert(page.url().includes('view=system'), 'stale response replaced current route');
      await page.unroute('**/api/v2/home');

      await page.locator('.nav-link[data-view="memories"]').click();
      await page.waitForSelector('.memory-card');
      const detailTrigger = page.locator('.memory-card .text-button').first();
      await detailTrigger.focus();
      await detailTrigger.click();
      await page.waitForSelector('#drawer[aria-hidden="false"]');
      assert(await page.locator('#drawer').evaluate(drawer => drawer.contains(document.activeElement)), 'dialog did not receive initial focus');
      assert(await page.locator('.shell').getAttribute('inert') !== null, 'dialog background is not inert');
      assert(await page.locator('#drawer').getAttribute('aria-modal') === 'true', 'dialog is not modal');
      await page.keyboard.press('Tab');
      assert(await page.locator('#drawer').evaluate(drawer => drawer.contains(document.activeElement)), 'dialog focus escaped');
      await page.keyboard.press('Escape');
      assert(await page.locator('#drawer').getAttribute('aria-hidden') === 'true', 'Escape did not close dialog');
      assert(await detailTrigger.evaluate(el => document.activeElement === el), 'focus did not return');

      await page.locator('.nav-link[data-view="self"]').click();
      await page.waitForSelector('.self-card');
      const selfBefore = await (await page.request.get(base + '/api/v2/self')).json();
      await page.locator('.self-card .text-button').first().click();
      await page.waitForSelector('#drawer[aria-hidden="false"]');
      await page.getByRole('button', {{name: '纠正这条理解'}}).click();
      await page.locator('textarea[name="statement"]').fill('浏览器验收要求先隔离、再验证、最后发布');
      await page.locator('textarea[name="reason"]').fill('验证真实纠正链路');
      await page.getByRole('button', {{name: '确认纠正'}}).click();
      await page.waitForFunction(() => document.querySelector('#drawer')?.getAttribute('aria-hidden') === 'true');
      const selfCorrected = await (await page.request.get(base + '/api/v2/self')).json();
      assert(selfCorrected.version_id !== selfBefore.version_id, 'Claim correction did not create a Living Self version');
      assert(JSON.stringify(selfCorrected.sections).includes('先隔离、再验证、最后发布'), 'corrected Claim is absent from Living Self');
      const trustAfterCorrection = await (await page.request.get(base + '/api/v2/trust')).json();
      assert(trustAfterCorrection.categories.recent_correction.count > trustBeforeCorrection.categories.recent_correction.count, 'Trust did not reflect Claim correction');

      await page.getByRole('button', {{name: '查看版本与恢复'}}).click();
      await page.waitForSelector('#drawer[aria-hidden="false"]');
      await page.getByRole('button', {{name: '恢复到这个版本'}}).first().click();
      await page.locator('textarea[name="reason"]').fill('浏览器验收恢复历史版本');
      await page.getByRole('button', {{name: '确认恢复'}}).click();
      await page.waitForFunction(() => document.querySelector('#drawer')?.getAttribute('aria-hidden') === 'true');
      const selfRestored = await (await page.request.get(base + '/api/v2/self')).json();
      const selfVersions = await (await page.request.get(base + '/api/v2/self/versions?limit=20')).json();
      assert(selfRestored.version_id !== selfCorrected.version_id && selfVersions.items.length >= 3, 'Living Self restore did not append a version');

      await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], {{origin: base}});
      await page.locator('.nav-link[data-view="use"]').click();
      await page.getByRole('button', {{name: '准备新的 Context'}}).click();
      await page.locator('textarea[name="task"]').fill('浏览器验收先验证再发布');
      await page.locator('select[name="mode"]').selectOption('reviewer');
      await page.locator('textarea[name="reason"]').fill('验证真实 Context 预览和编译');
      await page.getByRole('button', {{name: '生成预览'}}).click();
      await page.getByRole('button', {{name: '确认并生成 Context'}}).waitFor();
      const exclusions = page.locator('input[name="excluded_item_ids"]');
      assert(await exclusions.count() > 0, 'Context preview has no removable allowed item');
      const excludedId = await exclusions.first().getAttribute('value');
      await exclusions.first().check();
      const compileResponsePromise = page.waitForResponse(response => response.request().method() === 'POST' && response.url() === base + '/api/v2/contexts');
      await page.getByRole('button', {{name: '确认并生成 Context'}}).click();
      const compileResponse = await compileResponsePromise;
      assert(compileResponse.status() === 200, 'Context compilation failed');
      const compiled = await compileResponse.json();
      const contextId = compiled.context_id;
      await page.waitForSelector('#drawer[aria-hidden="false"]');
      await page.getByRole('button', {{name: '复制给 Agent'}}).click();
      await page.getByText('已复制，尚未标记为已交给 Agent').waitFor();
      const copiedDetail = await (await page.request.get(base + '/api/v2/contexts/' + contextId)).json();
      assert(copiedDetail.lifecycle_status === 'compiled', 'clipboard copy consumed Context');
      assert(copiedDetail.excluded_item_ids.includes(excludedId), 'preview exclusion was not persisted');
      await page.getByRole('button', {{name: '标记为已交给 Agent'}}).click();
      await page.waitForFunction(() => document.querySelector('#drawer')?.getAttribute('aria-hidden') === 'true');
      const consumedDetail = await (await page.request.get(base + '/api/v2/contexts/' + contextId)).json();
      assert(consumedDetail.lifecycle_status === 'consumed', 'explicit consume did not persist');
      await page.locator('.context-card .text-button').first().click();
      await page.waitForSelector('#drawer[aria-hidden="false"]');
      await page.locator('select[name="adopted"]').selectOption('yes');
      await page.locator('select[name="result"]').selectOption('positive');
      await page.locator('textarea[name="summary"]').fill('浏览器验收结果有效');
      await page.locator('textarea[name="reason"]').fill('真实浏览器验收');
      await page.getByRole('button', {{name: '记录结果'}}).click();
      await page.waitForFunction(() => document.querySelector('#drawer')?.getAttribute('aria-hidden') === 'true');
      const outcomeDetail = await (await page.request.get(base + '/api/v2/contexts/' + contextId)).json();
      assert(outcomeDetail.lifecycle_status === 'outcome_recorded' && outcomeDetail.outcome, 'outcome did not persist');
      const trust = await page.request.get(base + '/api/v2/trust');
      assert(trust.status() === 200, 'Trust did not reflect persisted state');
      const trustBody = await trust.json();
      assert(trustBody.categories.recent_correction.count > 0 && trustBody.categories.privacy_exclusion.count > 0, 'Trust does not expose correction and Context exclusion');

      const desktopOverflow = await page.locator('body').evaluate(body => body.scrollWidth > document.documentElement.clientWidth);
      assert(!desktopOverflow, 'desktop horizontal overflow');
      await page.screenshot({{path: {json.dumps(str(desktop))}, fullPage: true}});

      await page.setViewportSize({{width: 390, height: 844}});
      await page.emulateMedia({{reducedMotion: 'reduce'}});
      await page.reload({{waitUntil: 'networkidle'}});
      const motionDurations = await page.locator('body, body *').evaluateAll(nodes => nodes.flatMap(node => {{
        const style = getComputedStyle(node);
        return [style.transitionDuration, style.animationDuration].flatMap(value => value.split(',')).map(value => value.trim()).filter(Boolean).map(value => value.endsWith('ms') ? parseFloat(value) / 1000 : parseFloat(value));
      }}));
      assert(motionDurations.every(value => Number.isFinite(value) && value <= 0.001), 'reduced motion leaves a visible transition or animation');
      const mobileOverflow = await page.locator('body').evaluate(body => body.scrollWidth > document.documentElement.clientWidth);
      assert(!mobileOverflow, 'mobile horizontal overflow');
      const mobileNav = await page.locator('.nav-link').evaluateAll(nodes => nodes.map(node => {{
        const box = node.getBoundingClientRect();
        return {{left: box.left, right: box.right, top: box.top, bottom: box.bottom}};
      }}));
      assert(mobileNav.length === 7 && mobileNav.every(box => box.left >= 0 && box.right <= 390 && box.top >= 0 && box.bottom <= 844), 'mobile navigation is clipped or unreachable');
      const touchHeights = await page.locator('button, .nav-link').evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().height));
      assert(touchHeights.length > 0 && touchHeights.every(height => height >= 44), 'touch target under 44px');
      const inputSizes = await page.locator('input, select, textarea').evaluateAll(nodes => nodes.map(node => parseFloat(getComputedStyle(node).fontSize)));
      assert(inputSizes.every(size => size >= 16), 'mobile form control below 16px');
      await page.screenshot({{path: {json.dumps(str(mobile))}, fullPage: true}});

      const api = await page.request.get(base + '/api/v2/system');
      const body = await api.body();
      assert((api.headers()['cache-control'] || '') === 'no-store', 'API is cacheable');
      assert(body.length < 1024 * 1024, 'initial JSON exceeds 1MB');
      assert(consoleErrors.length === 0, 'console errors: ' + consoleErrors.join(' | '));
      return {{views: views.length, desktop: {json.dumps(str(desktop))}, mobile: {json.dumps(str(mobile))}, console_errors: consoleErrors.length, initial_json_bytes: body.length, context_id: contextId, self_version: selfRestored.version_id}};
    }}"""
    try:
        result = _run(
            [PLAYWRIGHT, f"-s={session}", "run-code", code],
            cwd=evidence,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stdout + result.stderr)
        marker = "### Result\n"
        if marker not in result.stdout:
            raise RuntimeError("Playwright result missing\n" + result.stdout)
        payload = result.stdout.split(marker, 1)[1].split("\n### Ran", 1)[0]
        return json.loads(payload)
    finally:
        _run([PLAYWRIGHT, f"-s={session}", "close"], cwd=evidence, timeout=60)


def _restart_persistence_acceptance(
    base: str,
    evidence: Path,
    *,
    context_id: str,
    self_version: str,
) -> dict:
    session = "immortal-v11-restart-" + uuid.uuid4().hex[:10]
    screenshot = evidence / "restart-persistence.png"
    opened = _run(
        [PLAYWRIGHT, f"-s={session}", "open", base + "/?view=use"],
        cwd=evidence,
        timeout=120,
    )
    if opened.returncode != 0:
        raise RuntimeError(opened.stdout + opened.stderr)
    code = f"""async (page) => {{
      const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};
      const base = {json.dumps(base)};
      await page.setViewportSize({{width: 1512, height: 982}});
      await page.goto(base + '/?view=use', {{waitUntil: 'networkidle'}});
      await page.waitForSelector('.context-card');
      const detail = await (await page.request.get(base + '/api/v2/contexts/' + {json.dumps(context_id)})).json();
      assert(detail.lifecycle_status === 'outcome_recorded' && detail.outcome?.summary === '浏览器验收结果有效', 'Context outcome did not survive service restart');
      await page.locator('.nav-link[data-view="self"]').click();
      await page.waitForSelector('.self-card');
      const self = await (await page.request.get(base + '/api/v2/self')).json();
      assert(self.version_id === {json.dumps(self_version)}, 'Living Self version did not survive service restart');
      await page.locator('.nav-link[data-view="trust"]').click();
      await page.waitForSelector('.system-grid');
      await page.screenshot({{path: {json.dumps(str(screenshot))}, fullPage: true}});
      return {{context_id: detail.context_id, self_version: self.version_id, screenshot: {json.dumps(str(screenshot))}}};
    }}"""
    try:
        result = _run(
            [PLAYWRIGHT, f"-s={session}", "run-code", code],
            cwd=evidence,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stdout + result.stderr)
        marker = "### Result\n"
        if marker not in result.stdout:
            raise RuntimeError("Playwright restart result missing\n" + result.stdout)
        payload = result.stdout.split(marker, 1)[1].split("\n### Ran", 1)[0]
        return json.loads(payload)
    finally:
        _run([PLAYWRIGHT, f"-s={session}", "close"], cwd=evidence, timeout=60)


def _contract_acceptance() -> dict:
    nodes = [
        "tests/test_product_http_v2.py",
        "tests/test_product_ui_v2.py::test_use_ui_has_preview_compile_consume_and_outcome_states",
        "tests/test_product_mutations.py::test_real_self_claim_action_and_restore_replay_native_authorities",
        "tests/test_product_mutations.py::test_real_context_preview_compile_consume_and_outcome_replay",
        "tests/test_context_compiler.py::test_compile_rejects_stale_source_before_and_during_authorization",
    ]
    with tempfile.TemporaryDirectory(prefix="immortal-v11-contract-") as base:
        result = _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                *nodes,
                "--basetemp",
                str(Path(base) / "pytest"),
            ],
            env={**os.environ, "PYTHONPATH": str(CORE)},
            timeout=300,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    return {"ok": True, "summary": result.stdout.strip().splitlines()[-1]}


def _complete_evidence_metadata(vault: Path) -> None:
    source = vault / "index.jsonl"
    metadata = source.stat()
    connection = sqlite3.connect(vault / "search_index.db")
    count = int(connection.execute("SELECT count(*) FROM docs").fetchone()[0])
    values = {
        "last_line": count,
        "source_size": metadata.st_size,
        "prefix_sha256": sha256_file(source),
    }
    connection.executemany(
        "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
        [(key, str(value)) for key, value in values.items()],
    )
    connection.commit()
    connection.close()


def _seed_living_self(vault: Path) -> None:
    claims = ClaimStore(vault)
    claim = new_claim(
        statement="浏览器验收先验证再发布",
        source_kind="direct",
        evidence_ids=["m1"],
        status="candidate",
        claim_type="value",
        privacy="context_safe",
        role_scope=["work"],
        domain_scope=["technical"],
        now="2026-07-20T12:00:00+00:00",
    )
    claim["claim_id"] = "clm_browser_release"
    claims.create(
        claim,
        expected_revision=0,
        request_id="browser-claim-create",
        idempotency_key="browser-claim-create",
        actor={"kind": "owner", "id": "owner"},
        reason="seed browser Claim",
    )
    private_claim = new_claim(
        statement="浏览器验收私密证据不得进入 Context",
        source_kind="direct",
        evidence_ids=["m1"],
        status="candidate",
        claim_type="fact",
        privacy="private",
        role_scope=["work"],
        domain_scope=["technical"],
        now="2026-07-20T12:00:00+00:00",
    )
    private_claim["claim_id"] = "clm_browser_private"
    private_created = claims.create(
        private_claim,
        expected_revision=0,
        request_id="browser-private-create",
        idempotency_key="browser-private-create",
        actor={"kind": "owner", "id": "owner"},
        reason="seed private browser Claim",
    )
    claims.transition(
        private_claim["claim_id"],
        "confirmed",
        expected_revision=private_created["revision"],
        request_id="browser-private-confirm",
        idempotency_key="browser-private-confirm",
        actor={"kind": "owner", "id": "owner"},
        reason="confirm private browser Claim",
    )


def _seed_performance_vault(vault: Path, count: int = ROW_COUNT) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    source = vault / "index.jsonl"
    database = vault / "search_index.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE docs(rowid INTEGER PRIMARY KEY, rec_id TEXT, ts TEXT, ts_utc TEXT NOT NULL, source TEXT, role TEXT, project TEXT, content TEXT, source_offset INTEGER NOT NULL, source_length INTEGER NOT NULL, line_number INTEGER NOT NULL, content_sha256 TEXT NOT NULL);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX idx_docs_rec_id ON docs(rec_id);
        CREATE INDEX idx_docs_source ON docs(source);
        CREATE INDEX idx_docs_ts ON docs(ts);
        CREATE INDEX idx_docs_ts_utc_rowid ON docs(ts_utc DESC,rowid DESC);
        CREATE UNIQUE INDEX idx_docs_source_offset ON docs(source_offset);
        CREATE UNIQUE INDEX idx_docs_line_number ON docs(line_number);
        CREATE VIRTUAL TABLE docs_fts USING fts5(content, tokenize='trigram');
        """
    )
    identifiers = []
    offset = 0
    batch = []
    fts_batch = []
    content = "synthetic acceptance memory"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    with source.open("wb") as handle:
        for index in range(1, count + 1):
            record_id = f"perf-{index:06d}"
            identifiers.append(record_id)
            timestamp = "2026-07-20T00:00:00+00:00"
            raw = (
                json.dumps(
                    {
                        "id": record_id,
                        "timestamp": timestamp,
                        "source": "synthetic",
                        "role": "user",
                        "project": "acceptance",
                        "content": content,
                    },
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            handle.write(raw)
            batch.append(
                (
                    index,
                    record_id,
                    timestamp,
                    "2026-07-20T00:00:00.000000Z",
                    "synthetic",
                    "user",
                    "acceptance",
                    content,
                    offset,
                    len(raw),
                    index,
                    content_hash,
                )
            )
            fts_batch.append((index, content))
            offset += len(raw)
            if len(batch) == 10_000:
                connection.executemany("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                connection.executemany("INSERT INTO docs_fts(rowid,content) VALUES(?,?)", fts_batch)
                batch.clear()
                fts_batch.clear()
        if batch:
            connection.executemany("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            connection.executemany("INSERT INTO docs_fts(rowid,content) VALUES(?,?)", fts_batch)
    metadata = source.stat()
    rows = {
        "parity_status": "trusted",
        "last_size": metadata.st_size,
        "source_dev": metadata.st_dev,
        "source_ino": metadata.st_ino,
        "source_mtime_ns": metadata.st_mtime_ns,
        "source_ctime_ns": metadata.st_ctime_ns,
        "indexed_id_count": count,
        "indexed_ids_sha256": ids_sha256(identifiers),
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "last_line": count,
        "source_size": metadata.st_size,
        "prefix_sha256": sha256_file(source),
    }
    connection.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        [(key, str(value)) for key, value in rows.items()],
    )
    connection.commit()
    connection.close()
    receipt = prewarm_index_verification(vault)
    if not receipt.get("ok"):
        raise RuntimeError("performance receipt failed: " + json.dumps(receipt))
    living = LivingSelfService(
        vault,
        clock=lambda: "2026-07-20T12:00:00+00:00",
    )
    living.confirm(living.build_candidate(), reason="performance baseline")


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, int(len(ordered) * 0.95 + 0.999999) - 1)]


def _measure_performance(vault: Path) -> dict:
    server, base = _start_server(vault)
    preview_counter = {"value": 0}

    def request_json(method: str, path: str, body=None, key: str = ""):
        parsed = urlsplit(base)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers.update(
                {
                    "Origin": base,
                    "Content-Type": "application/json",
                    "X-Immortal-Request-Id": key,
                    "Idempotency-Key": key,
                    "If-Match": str(body.get("expected_version", "")),
                }
            )
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"{method} {path} failed: {response.status} {raw[:300]!r}")
        return json.loads(raw)

    def preview_context():
        preview_counter["value"] += 1
        suffix = str(preview_counter["value"])
        return request_json(
            "POST",
            "/api/v2/contexts/preview",
            {
                "task": "470k context preview " + suffix,
                "mode": "reviewer",
                "expected_version": 0,
                "reason": "performance acceptance",
            },
            key="perf-preview-" + suffix,
        )

    operations = {
        "home_ms": lambda: request_json("GET", "/api/v2/home"),
        "memory_list_ms": lambda: request_json("GET", "/api/v2/memories?limit=20"),
        "memory_detail_ms": lambda: request_json("GET", "/api/v2/memories/perf-470000"),
        "self_ms": lambda: request_json("GET", "/api/v2/self"),
        "context_preview_ms": preview_context,
    }
    limits = {
        "home_ms": 500,
        "memory_list_ms": 800,
        "memory_detail_ms": 300,
        "self_ms": 500,
        "context_preview_ms": 5_000,
    }
    results = {}
    try:
        for name, operation in operations.items():
            operation()
            samples = []
            for _ in range(20):
                started = time.perf_counter()
                operation()
                samples.append((time.perf_counter() - started) * 1000)
            results[name] = round(_p95(samples), 3)
            if results[name] >= limits[name]:
                raise RuntimeError(f"{name} P95 {results[name]}ms exceeds {limits[name]}ms")
    finally:
        server.shutdown()
        server.server_close()
    return results


def main() -> int:
    evidence_root = Path(
        os.environ.get(
            "IMMORTAL_BROWSER_EVIDENCE",
            tempfile.mkdtemp(prefix="immortal-v11-browser-evidence-"),
        )
    ).resolve()
    with tempfile.TemporaryDirectory(prefix="immortal-v11-browser-vault-") as raw:
        vault = Path(raw).resolve()
        _seed_trusted_index(vault)
        _complete_evidence_metadata(vault)
        receipt = prewarm_index_verification(vault)
        if not receipt.get("ok"):
            raise RuntimeError("browser vault receipt failed")
        _seed_living_self(vault)
        server, base = _start_server(vault)
        try:
            browser = _browser_acceptance(base, evidence_root)
        finally:
            server.shutdown()
            server.server_close()
        restarted_server, restarted_base = _start_server(vault)
        try:
            persistence = _restart_persistence_acceptance(
                restarted_base,
                evidence_root,
                context_id=browser["context_id"],
                self_version=browser["self_version"],
            )
        finally:
            restarted_server.shutdown()
            restarted_server.server_close()
    contracts = _contract_acceptance()
    with tempfile.TemporaryDirectory(prefix="immortal-v11-performance-") as raw:
        performance_vault = Path(raw).resolve()
        _seed_performance_vault(performance_vault)
        performance = _measure_performance(performance_vault)
    report = {
        "ok": True,
        "browser": browser,
        "restart_persistence": persistence,
        "contracts": contracts,
        "performance_rows": ROW_COUNT,
        "performance_p95_ms": performance,
        "evidence_root": str(evidence_root),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
