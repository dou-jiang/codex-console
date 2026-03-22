from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.scheduler.engine import SchedulerEngine
from src.web.routes import accounts as accounts_routes
from src.web.routes import scheduled_tasks as scheduled_routes
from src.web.routes import api_router


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "scheduled-tasks-routes.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()

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
    app.state.scheduler_engine = SchedulerEngine()
    app.include_router(api_router, prefix="/api")

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
            "enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["next_run_at"] == "2026-03-23T08:00:00"

    created = crud.get_scheduled_plan_by_id(route_db, body["id"])
    assert created is not None
    assert created.next_run_at == datetime(2026, 3, 23, 8, 0, 0)


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
    client.app.state.scheduler_engine._plan_locks.add(seeded_scheduled_data["plan"].id)

    response = client.post(f"/api/scheduled-plans/{seeded_scheduled_data['plan'].id}/run")

    assert response.status_code == 409


def test_manual_run_route_succeeds_when_plan_is_not_running(client, seeded_scheduled_data):
    response = client.post(f"/api/scheduled-plans/{seeded_scheduled_data['plan'].id}/run")

    assert response.status_code == 200
    assert response.json() == {"success": True, "plan_id": seeded_scheduled_data["plan"].id}


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


def test_list_accounts_can_filter_by_primary_cpa_and_returns_invalid_fields(client, expired_accounts):
    response = client.get("/api/accounts", params={"status": "expired", "primary_cpa_service_id": 7})

    body = response.json()
    assert body["total"] == 1
    assert len(body["accounts"]) == 1
    assert all(account["primary_cpa_service_id"] == 7 for account in body["accounts"])
    assert all(account["email"] != "expired-other@example.com" for account in body["accounts"])
    assert "invalidated_at" in body["accounts"][0]
    assert "invalid_reason" in body["accounts"][0]
