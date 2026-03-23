from pathlib import Path

from tests_runtime.settings_js_harness import run_settings_js_scenario


def test_settings_template_contains_proxy_batch_import_controls():
    template = Path("templates/settings.html").read_text(encoding="utf-8")
    assert 'id="proxy-batch-import-form"' in template
    assert 'id="proxy-import-default-type"' in template
    assert 'id="proxy-filter-keyword"' in template
    assert 'id="batch-delete-proxies-btn"' in template


def test_settings_template_uses_shared_table_shell_for_management_lists():
    template = Path("templates/settings.html").read_text(encoding="utf-8")
    assert template.count("table-shell") >= 4


def test_settings_js_contains_proxy_batch_handlers():
    script = Path("static/js/settings.js").read_text(encoding="utf-8")
    assert "handleProxyBatchImport" in script
    assert "handleApplyProxyFilters" in script
    assert "handleBatchDeleteProxies" in script


def test_settings_js_generates_proxy_filter_query_from_form_state():
    result = run_settings_js_scenario("apply_proxy_filters")

    assert result["api_path"] == "/settings/proxies?keyword=us-west&type=http&enabled=true&is_default=false&location=Seattle"
    assert result["filters"] == {
        "keyword": "us-west",
        "type": "http",
        "enabled": "true",
        "is_default": "false",
        "location": "Seattle",
    }


def test_settings_js_updates_proxy_selection_ui_based_on_selected_rows():
    result = run_settings_js_scenario("proxy_selection_ui")

    assert result["empty_state"] == {
        "select_all_disabled": True,
        "select_all_checked": False,
        "select_all_indeterminate": False,
        "batch_delete_disabled": True,
        "batch_delete_text": "🗑️ 批量删除",
    }
    assert result["after_render"] == {
        "select_all_disabled": False,
        "select_all_checked": False,
        "select_all_indeterminate": False,
        "batch_delete_disabled": True,
        "batch_delete_text": "🗑️ 批量删除",
    }
    assert result["after_one_selected"] == {
        "select_all_disabled": False,
        "select_all_checked": False,
        "select_all_indeterminate": True,
        "batch_delete_disabled": False,
        "batch_delete_text": "🗑️ 批量删除 (1)",
        "selected_ids": [1],
    }
    assert result["after_all_selected"] == {
        "select_all_disabled": False,
        "select_all_checked": True,
        "select_all_indeterminate": False,
        "batch_delete_disabled": False,
        "batch_delete_text": "🗑️ 批量删除 (2)",
        "selected_ids": [1, 2],
    }


def test_settings_js_renders_proxy_import_result_summary_and_details():
    result = run_settings_js_scenario("proxy_import_result")

    assert result["display"] == "block"
    assert "成功导入" in result["html"]
    assert "跳过" in result["html"]
    assert "失败" in result["html"]
    assert "第 2 行" in result["html"]
    assert "duplicate" in result["html"]
    assert "美国-西雅图-001" in result["html"]


def test_settings_js_rendered_proxy_rows_keep_single_item_actions():
    result = run_settings_js_scenario("proxy_row_actions")
    html = result["html"]

    assert 'editProxyItem(7)' in html
    assert 'testProxyItem(7)' in html
    assert 'toggleProxyItem(7, false)' in html
    assert 'handleSetProxyDefault(7)' in html
    assert 'deleteProxyItem(7)' in html
