from control_center_ui import control_center_page_html


def test_page_contains_all_real_modules_and_no_legacy_links():
    page = control_center_page_html()

    for view in (
        "overview",
        "runs",
        "sources",
        "memories",
        "profile",
        "agent",
        "backup",
        "diagnostics",
    ):
        assert f'data-view="{view}"' in page
    assert 'href="/timeline"' not in page
    assert 'href="/snapshot"' not in page
    assert 'href="/agent-factory"' not in page
    assert 'href="/review"' not in page
    assert "window.confirm" not in page
    assert 'role="dialog"' in page


def test_page_uses_v1_live_apis_and_controlled_jobs():
    page = control_center_page_html()

    for endpoint in (
        "/api/v1/capabilities",
        "/api/v1/overview",
        "/api/v1/jobs",
        "/api/v1/sources",
        "/api/v1/memories",
        "/api/v1/profile/candidates",
        "/api/v1/agent",
        "/api/v1/backups",
        "/api/v1/diagnostics",
    ):
        assert endpoint in page
    assert "/api/control-center/actions" not in page


def test_page_initial_load_is_capabilities_plus_selected_module():
    page = control_center_page_html()

    assert "capabilities = await api('/api/v1/capabilities')" in page
    assert "await switchView" in page
    assert "const RENDERERS" in page
    assert "Promise.all([api('/api/v1/jobs'), api('/api/v1/overview')])" in page


def test_page_is_responsive_printable_and_motion_respectful():
    page = control_center_page_html()

    assert "@media (max-width: 760px)" in page
    assert "@media print" in page
    assert "@media (prefers-reduced-motion: reduce)" in page
    assert "overflow-x: clip" in page
    assert ".hero-side { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in page
    assert ".drawer[aria-hidden=\"true\"] { display: none; }" in page
    assert "font: 650 clamp(30px, 2.8vw, 44px)/1 var(--mono)" in page


def test_page_has_accessible_feedback_drawer_and_dialog():
    page = control_center_page_html()

    assert 'aria-live="polite"' in page
    assert "aria-busy" in page
    assert 'aria-label="详情面板"' in page
    assert 'aria-modal="true"' in page
    assert "showModal()" in page


def test_controls_use_instrument_structure_and_real_states():
    page = control_center_page_html()

    assert 'class="control primary"' in page
    assert ".control::before" in page
    assert ".control::after" in page
    assert "clip-path: polygon" in page
    assert ".control:disabled" in page
    assert "STAGE BOUNDARY" in page


def test_sensitive_modules_are_server_paginated_and_on_demand():
    page = control_center_page_html()

    assert "memoryOffset" in page
    assert "profileOffset" in page
    assert "openMemory" in page
    assert "openProfile" in page
    assert "MASKED" in page
    assert "SERVER FILTER" in page
