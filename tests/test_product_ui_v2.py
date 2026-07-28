from __future__ import annotations

import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import profile_review
from control_center import ControlCenter
from control_http import SECURITY_HEADERS
from product_ui import PRODUCT_ASSETS, product_page_html
from profile_review import FactoryStore, ReviewHandler, ReviewStore, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "core" / "product_assets"


def _start_server(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReviewHandler)
    server.store = ReviewStore(
        tmp_path / "proposal.md",
        tmp_path / "memories.jsonl",
        tmp_path / "reviewed.jsonl",
        tmp_path / "review_state.json",
    )
    server.factory = FactoryStore(
        history_path=tmp_path / "runtime" / "control_jobs.json",
        immortal_dir=tmp_path,
        skill_dir=tmp_path,
    )
    server.control_center = ControlCenter(
        tmp_path,
        skill_dir=tmp_path,
        scheduler_probe=lambda: {"status": "unknown", "detail": "test"},
        service_reachable=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_port


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_ui_has_seven_product_modules_and_no_fake_actions():
    page = product_page_html()
    for view in ("home", "memories", "self", "judgments", "use", "trust", "system"):
        assert f'data-view="{view}"' in page
    for label in ("首页", "记忆", "我", "判断", "使用", "信任", "系统"):
        assert label in page
    assert "setTimeout(() => success" not in page
    assert "localStorage.setItem('fake" not in page


def test_shell_uses_external_assets_and_accessible_detail_region():
    page = product_page_html()
    assert 'href="/assets/product.css"' in page
    assert 'src="/assets/app.js"' in page
    assert 'type="module"' in page
    assert "<style" not in page
    assert "<script>" not in page
    assert 'aria-label="证据与详情"' in page
    assert 'href="#main"' in page


def test_assets_are_packaged_and_csp_has_no_inline_escape_hatch():
    for relative in PRODUCT_ASSETS:
        assert (ASSETS / relative).is_file(), relative
    assert "'unsafe-inline'" not in SECURITY_HEADERS["Content-Security-Policy"]
    package_data = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"product_assets/*.css"' in package_data
    assert '"product_assets/*.js"' in package_data
    assert '"product_assets/views/*.js"' in package_data


def test_router_api_and_dialog_contracts_are_real_and_safe():
    router = (ASSETS / "router.js").read_text(encoding="utf-8")
    api = (ASSETS / "api.js").read_text(encoding="utf-8")
    dialog = (ASSETS / "dialog.js").read_text(encoding="utf-8")
    memories = (ASSETS / "views" / "memories.js").read_text(encoding="utf-8")
    system = (ASSETS / "views" / "system.js").read_text(encoding="utf-8")
    assert "AbortController" in router
    assert "response.ok" in api and "ApiError" in api
    assert "textContent" in memories
    assert "innerHTML" not in memories
    assert "/api/v2/memories" in memories
    assert "next_cursor" in memories
    assert "/api/v2/system" in system
    assert 'aria-modal' in dialog
    assert "Escape" in dialog
    assert "shiftKey" in dialog
    assert "alreadyOpen && keyHandler" in dialog


def test_styles_are_archive_specific_accessible_and_static():
    css = (ASSETS / "product.css").read_text(encoding="utf-8")
    for token in (
        "Iowan Old Style",
        "Avenir Next",
        "IBM Plex Mono",
        "@media (max-width: 760px)",
        "@media print",
        "@media (prefers-reduced-motion: reduce)",
        ":focus-visible",
        "min-height: 44px",
        "text-wrap: balance",
    ):
        assert token in css
    assert "@keyframes" not in css


def test_memory_coverage_is_never_presented_as_certain_when_incomplete():
    memories = (ASSETS / "views" / "memories.js").read_text(encoding="utf-8")
    assert "coverage_complete !== false" in memories
    assert "空结果不等于没有相关记忆" in memories
    assert "不能据此判断相关记忆不存在" in memories


def test_system_evidence_uses_human_labels_and_local_times():
    system = (ASSETS / "views" / "system.js").read_text(encoding="utf-8")
    assert 'memory_index: "记忆索引"' in system
    assert 'verified: "已经核验"' in system
    assert "formatTimestamp(content)" in system
    assert "logs.text" in system
    assert "任务可能已经建立" in system
    assert "actions_available" in system


def test_system_ui_shows_cloud_recovery_evidence_without_upload_action():
    page = (ASSETS / "views" / "system.js").read_text(encoding="utf-8")
    for state in ("飞书异地恢复", "未配置", "等待恢复演练", "已验证可恢复", "证据失效"):
        assert state in page
    assert "cloudRecoveryCard" in page
    assert "confirm-remote-write" not in page
    assert "feishu-recovery upload" not in page


def test_system_ui_shows_automation_feedback_as_a_separate_evidence_card():
    page = (ASSETS / "views" / "system.js").read_text(encoding="utf-8")
    for label in ("自动化反馈", "主流程退出码", "通知投递", "不会把主流程成功误作整条自动化成功"):
        assert label in page
    assert "automationFeedbackCard" in page


def test_self_ui_exposes_evidence_versions_and_real_correction():
    self_js = (ASSETS / "views" / "self.js").read_text(encoding="utf-8")
    api_js = (ASSETS / "api.js").read_text(encoding="utf-8")
    for endpoint in ("/api/v2/self", "/api/v2/self/versions", "/actions", "/restore"):
        assert endpoint in self_js
    assert "claim_refs" in self_js
    assert "expected_self_version" in self_js
    assert "Idempotency-Key" in api_js
    assert "X-Immortal-Request-Id" in api_js
    assert "If-Match" in api_js
    assert "createMutationAttempt" in api_js
    assert "derived_update_pending" in self_js
    assert "window.confirm" not in self_js
    assert "window.prompt" not in self_js
    assert "innerHTML" not in self_js


def test_use_ui_has_preview_compile_consume_and_outcome_states():
    page = (ASSETS / "views" / "contexts.js").read_text(encoding="utf-8")
    for endpoint in (
        "/api/v2/contexts/preview",
        "/api/v2/contexts",
        "/consume",
        "/outcomes",
    ):
        assert endpoint in page
    for state in (
        "准备中",
        "预览完成",
        "编译中",
        "可使用",
        "已交给 Agent",
        "待记录结果",
        "结果已记录",
        "失败",
    ):
        assert state in page
    assert "preview_hash" in page
    assert "context_markdown" in page
    assert '"custom"' in page
    assert "custom_scope_ids" in page
    assert "window.confirm" not in page
    assert "window.prompt" not in page
    assert "innerHTML" not in page
    assert "navigator.clipboard.writeText" in page
    assert "已复制，尚未标记为已交给 Agent" in page
    assert "标记为已交给 Agent" in page


def test_home_ui_can_confirm_or_reject_candidate_claims():
    home = (ASSETS / "views" / "home.js").read_text(encoding="utf-8")
    assert "/api/v2/claims/" in home
    assert "确认收录" in home
    assert 'reviewClaim(item, "reject"' in home
    assert "expected_version: item.revision" in home
    assert "createMutationAttempt" in home
    assert "confirmation_summary?.total" in home
    assert "当前展示" in home


def test_judgment_and_trust_are_real_and_coverage_honest():
    judgments = (ASSETS / "views" / "judgments.js").read_text(encoding="utf-8")
    trust = (ASSETS / "views" / "trust.js").read_text(encoding="utf-8")
    assert "/api/v2/judgments" in judgments
    assert "/actions" in judgments
    assert "revision" in judgments
    assert "/api/v2/trust" in trust
    for state in ("complete", "partial", "unknown", "truncated"):
        assert state in trust
    assert 'document.createElement("details")' in trust
    assert "审核候选理解" in trust
    assert 'navigate("home")' in trust
    assert "审核候选判断" in trust
    assert 'navigate("judgments")' in trust
    assert "window.confirm" not in judgments
    assert "innerHTML" not in judgments
    assert "innerHTML" not in trust


def test_app_wires_all_real_product_renderers():
    app = (ASSETS / "app.js").read_text(encoding="utf-8")
    for renderer in ("renderSelf", "renderJudgments", "renderUse", "renderTrust"):
        assert renderer in app
    assert "CONNECTION PENDING" not in app


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_mutation_helper_sends_real_concurrency_and_idempotency_headers():
    module = (ASSETS / "api.js").as_uri()
    script = f'''
      import {{ mutate, createMutationAttempt }} from "{module}";
      globalThis.fetch = async (path, options) => ({{
        ok: true,
        status: 200,
        json: async () => ({{ path, options }}),
      }});
      const result = await mutate("/api/v2/test", {{ expected_version: 7, value: "real" }});
      const headers = result.options.headers;
      if (result.options.method !== "POST") process.exit(2);
      if (headers["Content-Type"] !== "application/json") process.exit(3);
      if (!headers["X-Immortal-Request-Id"] || !headers["Idempotency-Key"]) process.exit(4);
      if (headers["X-Immortal-Request-Id"] !== headers["Idempotency-Key"]) process.exit(5);
      if (headers["If-Match"] !== "7") process.exit(6);
      if (JSON.parse(result.options.body).value !== "real") process.exit(7);
      const attempt = createMutationAttempt();
      const body = {{ expected_version: 8, value: "retry" }};
      const first = attempt.options(body).requestId;
      const retry = attempt.options(body).requestId;
      const changed = attempt.options({{ ...body, value: "changed" }}).requestId;
      if (first !== retry) process.exit(8);
      if (first === changed) process.exit(9);
    '''
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_timestamp_formatter_honors_explicit_timezone_and_dst():
    module = (ASSETS / "format.js").as_uri()
    script = (
        f'import {{ formatTimestamp }} from "{module}";'
        'console.log(formatTimestamp("2026-01-15T00:00:00Z", '
        '{locale:"zh-CN",timeZone:"Asia/Shanghai"}));'
        'console.log(formatTimestamp("2026-07-15T12:00:00Z", '
        '{locale:"zh-CN",timeZone:"America/New_York"}));'
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    first, second = result.stdout.strip().splitlines()
    assert "08:00" in first
    assert "08:00" in second


def test_sensitive_legacy_html_routes_are_no_store(tmp_path):
    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text("<html><body>private snapshot</body></html>")
    agent_entry = tmp_path / "agent" / "ENTRY.md"
    agent_entry.parent.mkdir()
    agent_entry.write_text("private agent context")
    server, base = _start_server(tmp_path)
    try:
        for route in ("/snapshot", "/agent-entry"):
            status, headers, body = _get(base + route)
            assert status == 200
            assert headers["Cache-Control"] == "no-store"
            assert body
    finally:
        server.shutdown()
        server.server_close()


def test_product_routes_redirect_and_assets_are_strict(tmp_path):
    server, base = _start_server(tmp_path)
    try:
        status, headers, body = _get(base + "/")
        assert status == 200
        assert b"IMMORTAL" in body
        assert headers["Cache-Control"] == "no-store"
        assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
        assert len(headers.get_all("Content-Security-Policy")) == 1

        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        request = urllib.request.Request(base + "/review", method="GET")
        with opener.open(request, timeout=5) as response:
            assert response.geturl().endswith("/?view=self&filter=candidate")
        request = urllib.request.Request(base + "/agent-factory", method="GET")
        with opener.open(request, timeout=5) as response:
            assert response.geturl().endswith("/?view=use")

        status, headers, body = _get(base + "/assets/product.css")
        assert status == 200 and headers.get_content_type() == "text/css"
        assert headers["Cache-Control"] == "no-store"
        assert body

        status, api_headers, _ = _get(base + "/api/v2/system")
        assert status in {200, 503}
        assert api_headers["Cache-Control"] == "no-store"

        for target in ("/assets/../profile_review.py", "/assets/%2e%2e/profile_review.py"):
            status, _, _ = _get(base + target)
            assert status in {400, 404}

        status, legacy_headers, legacy_body = _get(base + "/control-center")
        assert status == 200 and b"Immortal Control Center" in legacy_body
        assert "'unsafe-inline'" in legacy_headers["Content-Security-Policy"]
        assert len(legacy_headers.get_all("Content-Security-Policy")) == 1
    finally:
        server.shutdown()
        server.server_close()
