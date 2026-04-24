from pathlib import Path
import importlib

web_app = importlib.import_module("src.web.app")


def test_static_asset_version_is_non_empty_string():
    version = web_app._build_static_asset_version(web_app.STATIC_DIR)

    assert isinstance(version, str)
    assert version
    assert version.isdigit()


def test_email_services_template_uses_versioned_static_assets():
    template = Path("templates/email_services.html").read_text(encoding="utf-8")

    assert '/static/css/style.css?v={{ static_version }}' in template
    assert '/static/js/utils.js?v={{ static_version }}' in template
    assert '/static/js/email_services.js?v={{ static_version }}' in template


def test_index_template_uses_versioned_static_assets():
    template = Path("templates/index.html").read_text(encoding="utf-8")

    assert '/static/css/style.css?v={{ static_version }}' in template
    assert '/static/js/utils.js?v={{ static_version }}' in template
    assert '/static/js/app.js?v={{ static_version }}' in template
