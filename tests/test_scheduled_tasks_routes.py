from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.database import crud
from src.database import session as session_module
from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.scheduler.engine import SchedulerDispatchError, SchedulerEngine
from src.web.routes import accounts as accounts_routes
from src.web.routes import scheduled_tasks as scheduled_routes
from src.web.routes import api_router


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "scheduled-tasks-routes.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()
    monkeypatch.setattr(session_module, "_db_manager", manager)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def route_db(monkeypatch, temp_db):
    @contextmanager
    def _get_db():
        yield temp_db

    monkeypatch.setattr(accounts_routes, "get_db", _get_db)
    monkeypatch.setattr(scheduled_routes, "get_db", _get_db)
    return temp_db


@pytest.fixture
def client(route_db):
    app = FastAPI()

    def _runner(*, run_id: int, **kwargs):
        with session_module.get_db() as db:
            crud.finish_scheduled_run(db, run_id=run_id, status="success", summary={"triggered": True})

    app.state.scheduler_engine = SchedulerEngine(
        runner_map={
            "cpa_cleanup": _runner,
            "cpa_refill": _runner,
            "account_refresh": _runner,
        },
        worker_spawner=lambda fn, _name: fn(),
    )
    app.include_router(api_router, prefix="/api")
    app.include_router(scheduled_routes.runs_router, prefix="/api")

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded_scheduled_data(route_db):
    primary_service = crud.create_cpa_service(
        route_db,
        name="primary",
        api_url="https://cpa.example.com/primary",
        api_token="token-1",
    )
    secondary_service = crud.create_cpa_service(
        route_db,
        name="secondary",
        api_url="https://cpa.example.com/secondary",
        api_token="token-2",
    )

    plan = crud.create_scheduled_plan(
        route_db,
        name="existing cleanup plan",
        task_type="cpa_cleanup",
        cpa_service_id=primary_service.id,
        trigger_type="interval",
        interval_value=1,
        interval_unit="hours",
        config={"max_cleanup_count": 10},
        enabled=True,
    )

    earlier_run = crud.create_scheduled_run(route_db, plan_id=plan.id, trigger_source="scheduled")
    crud.finish_scheduled_run(
        route_db,
        run_id=earlier_run.id,
        status="success",
        summary={"processed": 3},
    )
    latest_run = crud.create_scheduled_run(route_db, plan_id=plan.id, trigger_source="manual")
    earlier_run.started_at = datetime(2026, 3, 22, 8, 0, 0)
    earlier_run.finished_at = datetime(2026, 3, 22, 8, 5, 0)
    latest_run.started_at = datetime(2026, 3, 22, 9, 0, 0)
    route_db.commit()
    route_db.refresh(earlier_run)
    route_db.refresh(latest_run)

    return {
        "plan": plan,
        "primary_service": primary_service,
        "secondary_service": secondary_service,
        "earlier_run": earlier_run,
        "latest_run": latest_run,
    }


@pytest.fixture
def expired_accounts(route_db):
    target = crud.create_account(
        route_db,
        email="expired-target@example.com",
        email_service="tempmail",
        status="expired",
    )
    crud.update_account(
        route_db,
        target.id,
        primary_cpa_service_id=7,
        invalidated_at=datetime(2026, 3, 22, 8, 30, 0),
        invalid_reason="cpa_cleanup",
    )

    other = crud.create_account(
        route_db,
        email="expired-other@example.com",
        email_service="tempmail",
        status="expired",
    )
    crud.update_account(
        route_db,
        other.id,
        primary_cpa_service_id=8,
        invalidated_at=datetime(2026, 3, 22, 9, 30, 0),
        invalid_reason="refresh_failed",
    )


def test_create_scheduled_plan_route_persists_next_run_at(client, route_db, seeded_scheduled_data, monkeypatch):
    monkeypatch.setattr(
        scheduled_routes,
        "compute_next_run_at",
        lambda _request: datetime(2026, 3, 23, 8, 0, 0),
    )

    response = client.post(
        "/api/scheduled-plans",
        json={
            "name": "morning refill",
            "task_type": "cpa_refill",
            "cpa_service_id": seeded_scheduled_data["secondary_service"].id,
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "config": {
                "target_valid_count": 50,
                "max_refill_count": 10,
                "max_consecutive_failures": 3,
                "registration_profile": {},
            },
            "config_meta": {
                "target_valid_count": {
                    "key_description": "目标有效数",
                    "value_description": "达到这个数量后停止补号",
                    "value_type": "number",
                }
            },
            "enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["next_run_at"] == "2026-03-23T08:00:00"
    assert body["config_meta"]["target_valid_count"]["value_type"] == "number"

    created = crud.get_scheduled_plan_by_id(route_db, body["id"])
    assert created is not None
    assert created.next_run_at == datetime(2026, 3, 23, 8, 0, 0)
    assert created.config_meta["target_valid_count"]["key_description"] == "目标有效数"


def test_create_scheduled_plan_route_rejects_invalid_refill_config(client, seeded_scheduled_data):
    response = client.post(
        "/api/scheduled-plans",
        json={
            "name": "invalid refill",
            "task_type": "cpa_refill",
            "cpa_service_id": seeded_scheduled_data["secondary_service"].id,
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "config": {
                "target_valid_count": 50,
                "max_refill_count": 10,
                "registration_profile": {},
            },
            "enabled": True,
        },
    )

    assert response.status_code == 400
    assert "max_consecutive_failures" in response.json()["detail"]


def test_manual_run_route_rejects_when_plan_is_currently_running(client, seeded_scheduled_data):
    plan_id = seeded_scheduled_data["plan"].id
    client.app.state.scheduler_engine._plan_locks.add(plan_id)

    response = client.post(f"/api/scheduled-plans/{plan_id}/run")

    assert response.status_code == 409

    with scheduled_routes.get_db() as db:
        runs = (
            db.query(scheduled_routes.ScheduledRun)
            .filter(scheduled_routes.ScheduledRun.plan_id == plan_id)
            .order_by(scheduled_routes.ScheduledRun.id.desc())
            .all()
        )
        assert len(runs) == 3
        assert runs[0].status == "skipped"
        assert runs[0].trigger_source == "manual"


def test_manual_run_route_succeeds_when_plan_is_not_running(client, seeded_scheduled_data):
    plan_id = seeded_scheduled_data["plan"].id

    response = client.post(f"/api/scheduled-plans/{seeded_scheduled_data['plan'].id}/run")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["plan_id"] == plan_id

    with scheduled_routes.get_db() as db:
        runs = (
            db.query(scheduled_routes.ScheduledRun)
            .filter(scheduled_routes.ScheduledRun.plan_id == plan_id)
            .order_by(scheduled_routes.ScheduledRun.id.desc())
            .all()
        )
        assert len(runs) == 3
        assert runs[0].trigger_source == "manual"
        assert runs[0].status == "success"
        assert runs[0].summary == {"triggered": True}
        if "run_id" in body:
            assert runs[0].id == body["run_id"]


def test_manual_run_route_returns_500_when_dispatch_fails(client, seeded_scheduled_data, monkeypatch):
    def _raise_dispatch_error(_plan_id: int):
        raise SchedulerDispatchError("dispatch failed")

    monkeypatch.setattr(client.app.state.scheduler_engine, "trigger_plan_now", _raise_dispatch_error)

    response = client.post(f"/api/scheduled-plans/{seeded_scheduled_data['plan'].id}/run")

    assert response.status_code == 500


def test_update_scheduled_plan_route_persists_changes_and_recomputes_next_run_at(
    client, route_db, seeded_scheduled_data, monkeypatch
):
    monkeypatch.setattr(
        scheduled_routes,
        "compute_next_run_at",
        lambda _request: datetime(2026, 3, 24, 9, 30, 0),
    )

    response = client.put(
        f"/api/scheduled-plans/{seeded_scheduled_data['plan'].id}",
        json={
            "name": "updated cleanup plan",
            "trigger_type": "cron",
            "cron_expression": "30 9 * * *",
            "interval_value": None,
            "interval_unit": None,
            "config": {"max_cleanup_count": 5},
            "config_meta": {
                "max_cleanup_count": {
                    "key_description": "最多清理数量",
                    "value_description": "建议小批量执行",
                    "value_type": "number",
                }
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "updated cleanup plan"
    assert body["trigger_type"] == "cron"
    assert body["cron_expression"] == "30 9 * * *"
    assert body["next_run_at"] == "2026-03-24T09:30:00"
    assert body["config_meta"]["max_cleanup_count"]["value_description"] == "建议小批量执行"

    route_db.expire_all()
    persisted = crud.get_scheduled_plan_by_id(route_db, seeded_scheduled_data["plan"].id)
    assert persisted is not None
    assert persisted.name == "updated cleanup plan"
    assert persisted.trigger_type == "cron"
    assert persisted.cron_expression == "30 9 * * *"
    assert persisted.next_run_at == datetime(2026, 3, 24, 9, 30, 0)
    assert persisted.config_meta["max_cleanup_count"]["value_type"] == "number"


def test_disable_and_enable_routes_toggle_plan_and_next_run_at(client, route_db, seeded_scheduled_data, monkeypatch):
    plan_id = seeded_scheduled_data["plan"].id
    disable_response = client.post(f"/api/scheduled-plans/{plan_id}/disable")

    assert disable_response.status_code == 200
    disabled_body = disable_response.json()
    assert disabled_body["enabled"] is False
    assert disabled_body["next_run_at"] is None

    route_db.expire_all()
    disabled = crud.get_scheduled_plan_by_id(route_db, plan_id)
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.next_run_at is None

    monkeypatch.setattr(
        scheduled_routes,
        "compute_next_run_at",
        lambda _request: datetime(2026, 3, 24, 10, 0, 0),
    )
    enable_response = client.post(f"/api/scheduled-plans/{plan_id}/enable")

    assert enable_response.status_code == 200
    enabled_body = enable_response.json()
    assert enabled_body["enabled"] is True
    assert enabled_body["next_run_at"] == "2026-03-24T10:00:00"

    route_db.expire_all()
    enabled = crud.get_scheduled_plan_by_id(route_db, plan_id)
    assert enabled is not None
    assert enabled.enabled is True
    assert enabled.next_run_at == datetime(2026, 3, 24, 10, 0, 0)


def test_list_plan_runs_route_returns_latest_runs(client, seeded_scheduled_data):
    response = client.get(f"/api/scheduled-plans/{seeded_scheduled_data['plan'].id}/runs")

    assert response.status_code == 200
    runs = response.json()["runs"]
    assert isinstance(runs, list)
    assert [row["id"] for row in runs] == [
        seeded_scheduled_data["latest_run"].id,
        seeded_scheduled_data["earlier_run"].id,
    ]
    assert runs[0]["trigger_source"] == "manual"
    assert runs[0]["status"] == "running"
    assert runs[1]["trigger_source"] == "scheduled"
    assert runs[1]["status"] == "success"
    assert runs[1]["summary"] == {"processed": 3}


def test_list_scheduled_plans_route_returns_items_and_total(client, seeded_scheduled_data):
    response = client.get("/api/scheduled-plans")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1
    assert isinstance(payload["items"], list)
    assert payload["items"][0]["id"] == seeded_scheduled_data["plan"].id


def test_get_scheduled_run_logs_route_returns_saved_logs(client, route_db, seeded_scheduled_data):
    run = seeded_scheduled_data["latest_run"]
    assert crud.append_scheduled_run_log(route_db, run.id, "manual run started")
    assert crud.append_scheduled_run_log(route_db, run.id, "manual run finished")

    response = client.get(f"/api/scheduled-plans/runs/{run.id}/logs")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run.id,
        "logs": "manual run started\nmanual run finished",
    }


def test_list_scheduled_runs_route_supports_filters(client, route_db, seeded_scheduled_data):
    secondary_plan = crud.create_scheduled_plan(
        route_db,
        name="secondary refill plan",
        task_type="cpa_refill",
        cpa_service_id=seeded_scheduled_data["secondary_service"].id,
        trigger_type="interval",
        interval_value=1,
        interval_unit="hours",
        config={
            "target_valid_count": 20,
            "max_refill_count": 5,
            "max_consecutive_failures": 2,
            "registration_profile": {},
        },
        enabled=True,
    )
    refill_run = crud.create_scheduled_run(route_db, plan_id=secondary_plan.id, trigger_source="scheduled")
    crud.finish_scheduled_run(route_db, run_id=refill_run.id, status="failed", error_message="upstream timeout")
    refill_run.started_at = datetime(2026, 3, 23, 10, 0, 0)
    refill_run.finished_at = datetime(2026, 3, 23, 10, 5, 0)
    route_db.commit()

    response = client.get(
        "/api/scheduled-runs",
        params={
            "task_type": "cpa_cleanup",
            "status": "running",
            "started_from": "2026-03-22T08:30:00",
            "started_to": "2026-03-22T09:30:00",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["id"] == seeded_scheduled_data["latest_run"].id
    assert payload["items"][0]["task_type"] == "cpa_cleanup"
    assert payload["items"][0]["status"] == "running"
    assert payload["items"][0]["can_stop"] is True


def test_get_scheduled_run_detail_route_returns_plan_summary_and_can_stop(client, seeded_scheduled_data):
    run_id = seeded_scheduled_data["latest_run"].id
    response = client.get(f"/api/scheduled-runs/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == run_id
    assert payload["plan_id"] == seeded_scheduled_data["plan"].id
    assert payload["plan_name"] == seeded_scheduled_data["plan"].name
    assert payload["plan_enabled"] is True
    assert payload["task_type"] == "cpa_cleanup"
    assert payload["can_stop"] is True


def test_get_scheduled_run_logs_incremental_route_returns_chunk_and_next_offset(
    client, route_db, seeded_scheduled_data
):
    run = seeded_scheduled_data["latest_run"]
    assert crud.append_scheduled_run_log(route_db, run.id, "abc")
    assert crud.append_scheduled_run_log(route_db, run.id, "def")

    response = client.get(f"/api/scheduled-runs/{run.id}/logs", params={"offset": 4})

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run.id
    assert payload["chunk"] == "def"
    assert payload["next_offset"] == 7
    assert payload["is_running"] is True


def test_stop_scheduled_run_route_marks_running_run(client, route_db, seeded_scheduled_data):
    run_id = seeded_scheduled_data["latest_run"].id

    response = client.post(f"/api/scheduled-runs/{run_id}/stop")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["run_id"] == run_id
    assert payload["status"] == "stopping"

    route_db.expire_all()
    persisted = crud.get_scheduled_run_by_id(route_db, run_id)
    assert persisted is not None
    assert persisted.stop_requested_at is not None
    assert persisted.stop_requested_by == "manual"
    assert persisted.stop_reason == "user_requested"


def test_stop_scheduled_run_route_rejects_finished_run(client, seeded_scheduled_data):
    run_id = seeded_scheduled_data["earlier_run"].id

    response = client.post(f"/api/scheduled-runs/{run_id}/stop")

    assert response.status_code == 409
    assert "运行已结束" in response.json()["detail"]


def test_list_accounts_can_filter_by_primary_cpa_and_returns_invalid_fields(client, expired_accounts):
    response = client.get("/api/accounts", params={"status": "expired", "primary_cpa_service_id": 7})

    body = response.json()
    assert body["total"] == 1
    assert len(body["accounts"]) == 1
    assert all(account["primary_cpa_service_id"] == 7 for account in body["accounts"])
    assert all(account["email"] != "expired-other@example.com" for account in body["accounts"])
    assert "invalidated_at" in body["accounts"][0]
    assert "invalid_reason" in body["accounts"][0]
