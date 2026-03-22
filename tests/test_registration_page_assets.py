from pathlib import Path

from tests_runtime.app_js_harness import run_app_js_scenario


def test_registration_template_contains_unlimited_mode_and_domain_stats_container():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    assert '<option value="unlimited">无限注册</option>' in template
    assert 'id="batch-consecutive-failures"' in template
    assert 'id="batch-domain-stats"' in template


def test_registration_template_nav_contains_scheduled_tasks_link():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    assert 'href="/scheduled-tasks"' in template


def test_app_js_posts_count_zero_for_unlimited_mode():
    result = run_app_js_scenario("unlimited_mode_request")
    assert result["batch_count_display"] == "none"
    assert result["request_payload"]["count"] == 0
    assert result["saved_active_task"]["mode"] == "unlimited"


def test_app_js_renders_unlimited_progress_without_domain_stats_until_finished():
    result = run_app_js_scenario("unlimited_progress_running")
    assert result["progress_text"] == "5/∞"
    assert result["progress_percent"] == "运行中"
    assert result["progress_bar_indeterminate"] is True
    assert result["consecutive_failures_text"] == "3/10"
    assert result["domain_stats_display"] == "none"
    assert result["domain_stats_html"] == ""


def test_app_js_renders_unlimited_final_domain_stats_with_rate_columns():
    result = run_app_js_scenario("unlimited_progress_finished")
    assert result["progress_text"] == "8/∞"
    assert result["progress_percent"] == "已结束"
    assert result["progress_bar_indeterminate"] is True
    assert "gmail.com" in result["domain_stats_html"]
    assert "成功率" in result["domain_stats_html"]
    assert "失败率" in result["domain_stats_html"]
    assert "75.00%" in result["domain_stats_html"]
    assert "25.00%" in result["domain_stats_html"]


def test_app_js_restore_unlimited_task_uses_batch_endpoint():
    result = run_app_js_scenario("restore_unlimited_task")
    assert result["api_get_paths"] == ["/registration/batch/batch-unlimited-01"]
    assert result["batch_progress_display"] == "block"
