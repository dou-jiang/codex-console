from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.database import crud
from src.database import session as session_module
from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.web.routes import account_survival as survival_routes
from src.web.routes import api_router


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "account-survival-routes.db"
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

    monkeypatch.setattr(survival_routes, "get_db", _get_db)
    return temp_db


@pytest.fixture
def client(route_db):
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded_accounts(route_db):
    healthy = crud.create_account(
        route_db,
        email="healthy@example.com",
        email_service="tempmail",
        access_token="token-1",
        status="active",
    )
    dead = crud.create_account(
        route_db,
        email="dead@example.com",
        email_service="tempmail",
        refresh_token="refresh-1",
        status="expired",
    )
    warning = crud.create_account(
        route_db,
        email="warning@example.com",
        email_service="tempmail",
        status="active",
    )

    crud.create_registration_task(
        route_db,
        task_uuid="task-healthy",
        email_address=healthy.email,
        pipeline_key="current_pipeline",
    )
    dead_task = crud.create_registration_task(
        route_db,
        task_uuid="task-dead",
        email_address=dead.email,
        pipeline_key="codexgen_pipeline",
    )
    crud.update_registration_task(route_db, dead_task.task_uuid, experiment_batch_id=7)
    warning_task = crud.create_registration_task(
        route_db,
        task_uuid="task-warning",
        email_address=warning.email,
        pipeline_key="current_pipeline",
    )
    crud.update_registration_task(route_db, warning_task.task_uuid, experiment_batch_id=7)

    return {
        "healthy": healthy,
        "dead": dead,
        "warning": warning,
    }


def test_manual_survival_run_returns_scheduled_count(client, seeded_accounts):
    response = client.post(
        "/api/accounts/survival-checks/run",
        json={"experiment_batch_id": 7, "check_stage": "manual"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scheduled"] == 2


def test_survival_checks_list_returns_details(client, seeded_accounts):
    client.post("/api/accounts/survival-checks/run", json={"pipeline_key": "current_pipeline"})

    response = client.get("/api/accounts/survival-checks")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 2
    assert {item["result_level"] for item in body["items"]} == {"healthy", "warning"}


def test_survival_summary_returns_grouped_counts(client, seeded_accounts):
    client.post("/api/accounts/survival-checks/run", json={"account_id": seeded_accounts["healthy"].id})
    client.post("/api/accounts/survival-checks/run", json={"account_id": seeded_accounts["dead"].id})
    client.post("/api/accounts/survival-checks/run", json={"account_id": seeded_accounts["warning"].id})

    response = client.get("/api/accounts/survival-summary")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 3
    assert body["counts"] == {"healthy": 1, "warning": 1, "dead": 1}
    assert body["ratios"]["healthy"] == pytest.approx(1 / 3)
