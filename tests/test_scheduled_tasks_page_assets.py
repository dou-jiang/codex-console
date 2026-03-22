from fastapi.testclient import TestClient
from pathlib import Path

from src.config.settings import get_settings
from src.web.app import create_app


def test_scheduled_tasks_page_requires_auth_and_renders_script():
    app = create_app()
    with TestClient(app) as client:
        unauthenticated = client.get("/scheduled-tasks", follow_redirects=False)
        assert unauthenticated.status_code == 302
        assert unauthenticated.headers["location"] == "/login?next=/scheduled-tasks"

        password = get_settings().webui_access_password.get_secret_value()
        login_response = client.post(
            "/login",
            data={"password": password, "next": "/scheduled-tasks"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        response = client.get("/scheduled-tasks")
        assert response.status_code == 200
        assert 'id="scheduled-plans-table"' in response.text
        assert '/static/js/scheduled_tasks.js?v={{ static_version }}' not in response.text
        assert "/static/js/scheduled_tasks.js?v=" in response.text


def test_scheduled_tasks_script_defines_escape_html_helper():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "function escapeHtml(" in script
    # 至少要在定义之外被调用一次，否则无法覆盖页面渲染分支
    assert script.count("escapeHtml(") >= 2


def test_scheduled_tasks_template_contains_plan_management_hooks():
    template = Path("templates/scheduled_tasks.html").read_text(encoding="utf-8")
    assert 'id="create-plan-btn"' in template
    assert 'id="plan-form-modal"' in template
    assert 'id="plan-form"' in template
    assert 'id="plan-trigger-type"' in template


def test_scheduled_tasks_script_contains_create_edit_enable_disable_hooks():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "function openCreatePlanModal(" in script
    assert "function openEditPlanModal(" in script
    assert "function submitPlanForm(" in script
    assert "function togglePlanEnabled(" in script
    assert "/scheduled-plans/${planId}/enable" in script
    assert "/scheduled-plans/${planId}/disable" in script
