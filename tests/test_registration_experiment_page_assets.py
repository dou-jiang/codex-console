from pathlib import Path

from fastapi.testclient import TestClient

from src.config.settings import get_settings
from src.web.app import create_app
from tests_runtime.registration_experiments_js_harness import (
    run_registration_experiments_js_scenario,
)


def test_registration_experiments_template_contains_required_dashboard_containers():
    template = Path("templates/registration_experiments.html").read_text(encoding="utf-8")
    assert 'id="experiment-summary"' in template
    assert 'id="experiment-step-compare"' in template
    assert 'id="survival-summary"' in template


def test_registration_experiments_template_references_dashboard_script():
    template = Path("templates/registration_experiments.html").read_text(encoding="utf-8")
    assert '/static/js/registration_experiments.js' in template


def test_web_app_registers_registration_experiments_page_route():
    app_source = Path("src/web/app.py").read_text(encoding="utf-8")
    assert '@app.get("/registration-experiments", response_class=HTMLResponse)' in app_source
    assert 'templates.TemplateResponse("registration_experiments.html"' in app_source


def test_registration_experiments_page_requires_auth_and_renders_script():
    app = create_app()
    with TestClient(app) as client:
        unauthenticated = client.get("/registration-experiments", follow_redirects=False)
        assert unauthenticated.status_code == 302
        assert unauthenticated.headers["location"] == "/login?next=/registration-experiments"

        password = get_settings().webui_access_password.get_secret_value()
        login_response = client.post(
            "/login",
            data={"password": password, "next": "/registration-experiments"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        response = client.get("/registration-experiments")
        assert response.status_code == 200
        assert 'id="experiment-summary"' in response.text
        assert '/static/js/registration_experiments.js?v={{ static_version }}' not in response.text
        assert "/static/js/registration_experiments.js?v=" in response.text


def test_registration_experiments_script_loads_summary_steps_and_survival_endpoints():
    script = Path("static/js/registration_experiments.js").read_text(encoding="utf-8")
    assert "/registration/experiments/" in script
    assert "/steps" in script
    assert "/accounts/survival-summary" in script
    assert "renderExperimentSummary" in script
    assert "renderStepComparison" in script
    assert "renderSurvivalSummary" in script


def test_shared_stylesheet_defines_experiment_dashboard_selectors():
    stylesheet = Path("static/css/style.css").read_text(encoding="utf-8")
    assert ".experiment-dashboard" in stylesheet
    assert ".experiment-summary-grid" in stylesheet
    assert ".survival-summary-grid" in stylesheet


def test_registration_experiments_script_renders_dashboard_sections():
    result = run_registration_experiments_js_scenario("load_dashboard")

    assert result["apiGetPaths"] == [
        "/registration/experiments/7",
        "/registration/experiments/7/steps",
        "/accounts/survival-summary?experiment_batch_id=7",
    ]
    assert result["toastWarnings"] == []
    assert result["toastErrors"] == []
    assert "current_pipeline" in result["summaryHtml"]
    assert "总任务数" in result["summaryHtml"]
    assert "create_email" in result["stepHtml"]
    assert "80ms" in result["stepHtml"]
    assert "healthy" in result["survivalHtml"]
    assert "75.0%" in result["survivalHtml"]
