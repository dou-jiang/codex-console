from pathlib import Path


def test_settings_template_contains_proxy_batch_import_controls():
    template = Path("templates/settings.html").read_text(encoding="utf-8")
    assert 'id="proxy-batch-import-form"' in template
    assert 'id="proxy-import-default-type"' in template
    assert 'id="proxy-filter-keyword"' in template
    assert 'id="batch-delete-proxies-btn"' in template


def test_settings_js_contains_proxy_batch_handlers():
    script = Path("static/js/settings.js").read_text(encoding="utf-8")
    assert "handleProxyBatchImport" in script
    assert "handleApplyProxyFilters" in script
    assert "handleBatchDeleteProxies" in script
