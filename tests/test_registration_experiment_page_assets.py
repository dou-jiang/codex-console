from pathlib import Path


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
