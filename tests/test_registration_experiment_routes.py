from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.database import crud
from src.database import session as session_module
from src.database.models import Base, PipelineStepRun, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.web import task_manager as task_manager_module
from src.web.routes import api_router
from src.web.routes import registration_experiments as experiment_routes


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "registration-experiment-routes.db"
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

    monkeypatch.setattr(experiment_routes, "get_db", _get_db)
    return temp_db


@pytest.fixture
def client(route_db):
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_experiment_state():
    if hasattr(task_manager_module, "_experiment_status"):
        original = dict(task_manager_module._experiment_status)
        task_manager_module._experiment_status.clear()
    else:
        original = None
    try:
        yield
    finally:
        if original is not None:
            task_manager_module._experiment_status.clear()
            task_manager_module._experiment_status.update(original)


def _create_experiment(client: TestClient, *, count: int = 2) -> dict:
    response = client.post(
        "/api/registration/experiments",
        json={
            "count": count,
            "email_service_type": "tempmail",
            "email_service_id": None,
            "email_service_config": {"base_url": "https://mail.example.test"},
            "proxy_strategy": {"mode": "shared_pool"},
            "concurrency": 2,
            "mode": "parallel",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_create_experiment_batch_creates_paired_tasks(client):
    body = _create_experiment(client, count=2)

    assert body["total_tasks"] == 4
    assert set(body["pipelines"]) == {"current_pipeline", "codexgen_pipeline"}
    assert len(body["tasks"]) == 4

    pair_groups: dict[str, set[str]] = {}
    for task in body["tasks"]:
        pair_groups.setdefault(task["pair_key"], set()).add(task["pipeline_key"])

    assert set(pair_groups) == {"pair-0001", "pair-0002"}
    assert all(pipelines == {"current_pipeline", "codexgen_pipeline"} for pipelines in pair_groups.values())


def test_experiment_overview_returns_total_and_per_pipeline_summary(client, route_db):
    created = _create_experiment(client, count=2)
    experiment_id = created["id"]

    tasks = (
        route_db.query(RegistrationTask)
        .filter(RegistrationTask.experiment_batch_id == experiment_id)
        .order_by(RegistrationTask.id.asc())
        .all()
    )
    assert len(tasks) == 4

    for index, task in enumerate(tasks, start=1):
        status = "completed" if task.pipeline_key == "current_pipeline" else "failed"
        crud.update_registration_task(
            route_db,
            task.task_uuid,
            status=status,
            pipeline_status=status,
            total_duration_ms=100 * index,
        )

    response = client.get(f"/api/registration/experiments/{experiment_id}")
    assert response.status_code == 200
    body = response.json()

    assert body["id"] == experiment_id
    assert body["total_tasks"] == 4
    assert body["status"] == "pending"
    assert body["survival_summary"] == {"total": 0, "active": 0, "expired": 0, "unknown": 0}
    assert body["pipelines"]["current_pipeline"]["success_rate"] == 1.0
    assert body["pipelines"]["codexgen_pipeline"]["success_rate"] == 0.0
    assert body["pipelines"]["current_pipeline"]["avg_duration_ms"] == pytest.approx(200.0)
    assert body["pipelines"]["codexgen_pipeline"]["avg_duration_ms"] == pytest.approx(300.0)


def test_experiment_pairs_groups_tasks_by_pair(client, route_db):
    created = _create_experiment(client, count=2)
    experiment_id = created["id"]

    tasks = (
        route_db.query(RegistrationTask)
        .filter(RegistrationTask.experiment_batch_id == experiment_id)
        .order_by(RegistrationTask.pair_key.asc(), RegistrationTask.pipeline_key.asc())
        .all()
    )
    for index, task in enumerate(tasks, start=1):
        crud.update_registration_task(
            route_db,
            task.task_uuid,
            status="completed" if task.pipeline_key == "current_pipeline" else "failed",
            pipeline_status="completed" if task.pipeline_key == "current_pipeline" else "failed",
            total_duration_ms=111 * index,
        )

    response = client.get(f"/api/registration/experiments/{experiment_id}/pairs")
    assert response.status_code == 200
    body = response.json()

    assert body["experiment_id"] == experiment_id
    assert len(body["pairs"]) == 2
    assert [item["pair_key"] for item in body["pairs"]] == ["pair-0001", "pair-0002"]
    assert all(set(item["tasks"]) == {"current_pipeline", "codexgen_pipeline"} for item in body["pairs"])


def test_experiment_steps_returns_aggregated_summary(client, route_db):
    created = _create_experiment(client, count=2)
    experiment_id = created["id"]

    tasks = (
        route_db.query(RegistrationTask)
        .filter(RegistrationTask.experiment_batch_id == experiment_id)
        .order_by(RegistrationTask.pair_key.asc(), RegistrationTask.pipeline_key.asc())
        .all()
    )

    durations = {
        "current_pipeline": [100, 200],
        "codexgen_pipeline": [300, 500],
    }
    for task in tasks:
        values = durations[task.pipeline_key]
        duration = values.pop(0)
        route_db.add(
            PipelineStepRun(
                task_uuid=task.task_uuid,
                pipeline_key=task.pipeline_key,
                step_key="create_email",
                step_order=1,
                status="completed",
                duration_ms=duration,
            )
        )
    route_db.commit()

    response = client.get(f"/api/registration/experiments/{experiment_id}/steps")
    assert response.status_code == 200
    body = response.json()

    assert body["experiment_id"] == experiment_id
    assert len(body["steps"]) == 1

    step = body["steps"][0]
    assert step["step_key"] == "create_email"
    assert step["pipelines"]["current_pipeline"]["success_rate"] == 1.0
    assert step["pipelines"]["current_pipeline"]["avg_duration_ms"] == pytest.approx(150.0)
    assert step["pipelines"]["current_pipeline"]["p50_duration_ms"] == 150
    assert step["pipelines"]["current_pipeline"]["p90_duration_ms"] == 190
    assert step["pipelines"]["codexgen_pipeline"]["avg_duration_ms"] == pytest.approx(400.0)
    assert step["pipeline_diff"]["avg_duration_ms"] == pytest.approx(250.0)
