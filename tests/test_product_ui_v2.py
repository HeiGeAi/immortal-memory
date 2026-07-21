from __future__ import annotations

import threading
import urllib.error
import urllib.request
from pathlib import Path

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
    ):
        assert token in css
    assert "@keyframes" not in css


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
