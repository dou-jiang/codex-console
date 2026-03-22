from pathlib import Path

from tests_runtime.app_js_harness import run_app_js_scenario


def test_registration_template_contains_unlimited_mode_and_domain_stats_container():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    assert '<option value="unlimited">无限注册</option>' in template
    assert 'id="batch-consecutive-failures"' in template
    assert 'id="batch-domain-stats"' in template


def test_app_js_posts_count_zero_for_unlimited_mode():
    result = run_app_js_scenario("unlimited_mode_request")
    assert result["batch_count_display"] == "none"
    assert result["request_payload"]["count"] == 0
    assert result["saved_active_task"]["mode"] == "unlimited"


def test_app_js_renders_unlimited_progress_and_domain_stats():
    result = run_app_js_scenario("unlimited_progress")
    assert result["progress_text"] == "5/∞"
    assert result["progress_percent"] == "运行中"
    assert result["progress_bar_indeterminate"] is True
    assert result["consecutive_failures_text"] == "3/10"
    assert "gmail.com" in result["domain_stats_html"]
