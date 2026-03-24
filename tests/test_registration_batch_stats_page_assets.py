from pathlib import Path

from fastapi.testclient import TestClient

from src.config.settings import get_settings
from src.web.app import create_app
from tests_runtime.registration_batch_stats_js_harness import (
    run_registration_batch_stats_js_scenario,
)


def test_registration_batch_stats_template_contains_required_dashboard_containers():
    template_path = Path("templates/registration_batch_stats.html")
    assert template_path.exists()
    template = template_path.read_text(encoding="utf-8")
    assert 'id="batch-stats-list"' in template
    assert 'id="batch-stats-detail"' in template
    assert 'id="batch-stats-compare"' in template


def test_registration_batch_stats_template_references_dashboard_script():
    template_path = Path("templates/registration_batch_stats.html")
    assert template_path.exists()
    template = template_path.read_text(encoding="utf-8")
    assert '/static/js/registration_batch_stats.js' in template


def test_web_app_registers_registration_batch_stats_page_route():
    app_source = Path("src/web/app.py").read_text(encoding="utf-8")
    assert '@app.get("/registration-batch-stats", response_class=HTMLResponse)' in app_source
    assert 'templates.TemplateResponse("registration_batch_stats.html"' in app_source


def test_registration_batch_stats_page_requires_auth_and_renders_script():
    app = create_app()
    with TestClient(app) as client:
        unauthenticated = client.get("/registration-batch-stats", follow_redirects=False)
        assert unauthenticated.status_code == 302
        assert unauthenticated.headers["location"] == "/login?next=/registration-batch-stats"

        password = get_settings().webui_access_password.get_secret_value()
        login_response = client.post(
            "/login",
            data={"password": password, "next": "/registration-batch-stats"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        response = client.get("/registration-batch-stats")
        assert response.status_code == 200
        assert 'id="batch-stats-list"' in response.text
        assert '/static/js/registration_batch_stats.js?v={{ static_version }}' not in response.text
        assert "/static/js/registration_batch_stats.js?v=" in response.text


def test_registration_batch_stats_script_loads_list_detail_and_compare_endpoints():
    script_path = Path("static/js/registration_batch_stats.js")
    assert script_path.exists()
    script = script_path.read_text(encoding="utf-8")
    assert "/registration/batch-stats" in script
    assert "/registration/batch-stats/compare" in script
    assert "loadBatchStatsList" in script
    assert "renderBatchStatsList" in script
    assert "renderBatchStatsDetail" in script
    assert "renderBatchStatsCompare" in script


def test_shared_stylesheet_defines_batch_stats_dashboard_selectors():
    stylesheet = Path("static/css/style.css").read_text(encoding="utf-8")
    assert ".batch-stats-dashboard" in stylesheet
    assert ".batch-stats-layout" in stylesheet
    assert ".batch-stats-list" in stylesheet
    assert ".batch-stats-detail" in stylesheet
    assert ".batch-stats-compare" in stylesheet


def test_registration_batch_stats_script_renders_compare():
    result = run_registration_batch_stats_js_scenario("compare_two")

    assert result["apiGetPaths"] == ["/registration/batch-stats/1"]
    assert result["apiPostPaths"] == ["/registration/batch-stats/compare"]
    assert result["apiPostBodies"] == [{"left_id": 1, "right_id": 2}]
    assert result["toastWarnings"] == []
    assert result["toastErrors"] == []
    assert "batch-002" in result["compareHtml"]
    assert "create_email" in result["compareHtml"]
    assert "12.5%" in result["compareHtml"]


def test_registration_batch_stats_script_prevents_over_selection():
    result = run_registration_batch_stats_js_scenario("prevent_over_selection")

    assert result["toastWarnings"] == ["最多只能选择 2 个批次"]
    assert result["selectedIds"] == [1, 2]
    assert result["thirdChecked"] is False


def test_registration_batch_stats_script_ignores_stale_detail_response():
    result = run_registration_batch_stats_js_scenario("stale_detail_race")

    assert "已选择两个批次" in result["detailPlaceholder"]
    assert "已选择两个批次" in result["detailHtmlAfter"]
    assert "batch-001" not in result["detailHtmlAfter"]
    assert "batch-002" in result["compareHtmlAfter"]
    assert result["toastWarnings"] == []
    assert result["toastErrors"] == []


def test_registration_batch_stats_script_clears_compare_on_two_to_one_transition():
    result = run_registration_batch_stats_js_scenario("compare_to_detail_transition")

    assert "请选择两个批次" in result["compareHtmlImmediate"]
    assert "请选择两个批次" in result["compareHtmlFinal"]
    assert "batch-001" in result["detailHtmlFinal"]
    assert result["toastWarnings"] == []
    assert result["toastErrors"] == []
