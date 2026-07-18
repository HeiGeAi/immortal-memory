from control_center_ui import control_center_page_html


def test_page_contains_truth_sections_and_live_api():
    page = control_center_page_html("Immortal Control Center")

    assert "运行证明" in page
    assert "当前运行" in page
    assert "本轮产出" in page
    assert "风险与建议" in page
    assert "最近运行" in page
    assert "/api/control-center/state" in page
    assert "/api/control-center/actions" in page


def test_page_is_responsive_printable_and_motion_respectful():
    page = control_center_page_html("Immortal Control Center")

    assert "@media (max-width: 760px)" in page
    assert "@media print" in page
    assert "@media (prefers-reduced-motion: reduce)" in page


def test_page_has_accessible_motion_and_action_feedback():
    page = control_center_page_html("Immortal Control Center")

    assert 'aria-live="polite"' in page
    assert "aria-busy" in page
    assert "is-loading" in page
    assert "@keyframes beacon" in page
    assert "@keyframes stage-flow" in page


def test_page_contains_long_evidence_without_horizontal_overflow():
    page = control_center_page_html("Immortal Control Center")

    assert "overflow-wrap: anywhere" in page
    assert "overflow-x: clip" in page


def test_print_view_removes_navigation_and_controls():
    page = control_center_page_html("Immortal Control Center")

    assert ".nav, .control-panel { display: none; }" in page


def test_control_buttons_use_instrument_switch_structure():
    page = control_center_page_html("Immortal Control Center")

    assert 'class="button instrument primary action"' in page
    assert 'class="instrument-light"' in page
    assert 'class="instrument-copy"' in page
    assert "<small>RUN · 7 STAGES</small>" in page
    assert 'class="link"' in page
    assert 'class="link instrument"' not in page


def test_instrument_buttons_define_visual_and_accessible_states():
    page = control_center_page_html("Immortal Control Center")

    assert ".instrument-light" in page
    assert "clip-path: polygon" in page
    assert ".instrument.is-loading .instrument-light" in page
    assert ".instrument.is-success .instrument-light" in page
    assert ".instrument.is-error .instrument-light" in page
    assert ".instrument:disabled .instrument-light" in page


def test_button_state_updates_labels_without_replacing_instrument_dom():
    page = control_center_page_html("Immortal Control Center")

    assert "button.querySelector('.instrument-copy strong')" in page
    assert "button.querySelector('.instrument-copy small')" in page
    assert "button.textContent = label" not in page


def test_page_has_safe_control_labels_and_navigation():
    page = control_center_page_html("Immortal Control Center")

    assert "立即运行全流程" in page
    assert "运行健康检查" in page
    assert "校验最新备份" in page
    assert "刷新画像" in page
    assert 'href="/agent-factory"' in page
    assert 'href="/review"' in page
