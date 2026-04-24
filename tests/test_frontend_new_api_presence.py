from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_page_contains_new_api_service_management_ui():
    content = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    assert "add-new-api-service-btn" in content
    assert "new-api-services-table" in content
    assert "new-api-service-edit-modal" in content


def test_index_page_contains_new_api_auto_upload_ui():
    content = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "auto-upload-new-api" in content
    assert "new-api-service-select-group" in content
    assert "new-api-service-select" in content


def test_accounts_page_contains_new_api_batch_upload_entry():
    content = (ROOT / "templates" / "accounts.html").read_text(encoding="utf-8")
    assert "batch-upload-new-api-item" in content
