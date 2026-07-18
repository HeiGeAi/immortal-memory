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


def test_page_is_responsive_printable_and_has_no_animation():
    page = control_center_page_html("Immortal Control Center")
    lowered = page.lower()

    assert "@media (max-width: 760px)" in page
    assert "@media print" in page
    assert "animation:" not in lowered
    assert "transition:" not in lowered


def test_page_has_safe_control_labels_and_navigation():
    page = control_center_page_html("Immortal Control Center")

    assert "立即运行全流程" in page
    assert "运行健康检查" in page
    assert "校验最新备份" in page
    assert "刷新画像" in page
    assert 'href="/agent-factory"' in page
    assert 'href="/review"' in page
