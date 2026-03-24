from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.database import crud
from src.database import session as session_module
from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.web.routes import api_router
from src.web.routes import registration_batch_stats as stats_routes


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "registration-batch-stats-routes.db"
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

    monkeypatch.setattr(stats_routes, "get_db", _get_db)
    return temp_db


@pytest.fixture
def client(route_db):
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


def _create_batch_stat(
    db,
    *,
    batch_id: str,
    status: str = "completed",
    pipeline_key: str = "current_pipeline",
    created_at: datetime | None = None,
):
    return crud.create_registration_batch_stat(
        db,
        batch_id=batch_id,
        status=status,
        mode="pipeline",
        pipeline_key=pipeline_key,
        target_count=3,
        finished_count=3,
        success_count=2,
        failed_count=1,
        total_duration_ms=900,
        avg_duration_ms=300.0,
        created_at=created_at,
    )


def test_list_batch_stats_orders_newest_first(client, route_db):
    newest = _create_batch_stat(
        route_db,
        batch_id="batch-new",
        created_at=datetime(2026, 1, 2, 10, 0, 0),
    )
    oldest = _create_batch_stat(
        route_db,
        batch_id="batch-old",
        created_at=datetime(2026, 1, 1, 9, 0, 0),
    )

    response = client.get("/api/registration/batch-stats?limit=10&offset=0")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [newest.id, oldest.id]


def test_list_batch_stats_filters_by_status_and_pipeline(client, route_db):
    matching = _create_batch_stat(route_db, batch_id="batch-match")
    _create_batch_stat(route_db, batch_id="batch-other", pipeline_key="codexgen_pipeline")
    _create_batch_stat(route_db, batch_id="batch-failed", status="failed")

    response = client.get(
        "/api/registration/batch-stats?status=completed&pipeline_key=current_pipeline"
    )
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [matching.id]


def test_detail_returns_step_and_stage_stats_sorted(client, route_db):
    stat = _create_batch_stat(route_db, batch_id="batch-detail")
    crud.create_registration_batch_step_stat(
        route_db,
        batch_stat_id=stat.id,
        step_key="submit_signup_email",
        step_order=2,
        sample_count=3,
        success_count=2,
        avg_duration_ms=220.0,
        p50_duration_ms=210,
        p90_duration_ms=260,
    )
    crud.create_registration_batch_step_stat(
        route_db,
        batch_stat_id=stat.id,
        step_key="create_email",
        step_order=1,
        sample_count=3,
        success_count=3,
        avg_duration_ms=120.0,
        p50_duration_ms=110,
        p90_duration_ms=140,
    )
    crud.create_registration_batch_stage_stat(
        route_db,
        batch_stat_id=stat.id,
        stage_key="login_prepare",
        sample_count=3,
        avg_duration_ms=430.0,
        p50_duration_ms=420,
        p90_duration_ms=480,
    )
    crud.create_registration_batch_stage_stat(
        route_db,
        batch_stat_id=stat.id,
        stage_key="signup_prepare",
        sample_count=3,
        avg_duration_ms=310.0,
        p50_duration_ms=300,
        p90_duration_ms=350,
    )

    response = client.get(f"/api/registration/batch-stats/{stat.id}")
    assert response.status_code == 200
    body = response.json()

    assert body["id"] == stat.id
    assert [item["step_key"] for item in body["step_stats"]] == [
        "create_email",
        "submit_signup_email",
    ]
    assert [item["stage_key"] for item in body["stage_stats"]] == [
        "signup_prepare",
        "login_prepare",
    ]


def test_detail_missing_returns_404(client):
    response = client.get("/api/registration/batch-stats/99999")
    assert response.status_code == 404


def test_compare_returns_diffs(client, route_db):
    left = _create_batch_stat(route_db, batch_id="batch-left")
    right = _create_batch_stat(route_db, batch_id="batch-right")
    right.target_count = 5
    right.finished_count = 5
    right.success_count = 4
    right.failed_count = 1
    right.total_duration_ms = 1500
    right.avg_duration_ms = 300.0
    route_db.commit()

    crud.create_registration_batch_step_stat(
        route_db,
        batch_stat_id=left.id,
        step_key="create_email",
        step_order=1,
        sample_count=3,
        success_count=3,
        avg_duration_ms=100.0,
        p50_duration_ms=90,
        p90_duration_ms=140,
    )
    crud.create_registration_batch_step_stat(
        route_db,
        batch_stat_id=right.id,
        step_key="create_email",
        step_order=1,
        sample_count=5,
        success_count=4,
        avg_duration_ms=200.0,
        p50_duration_ms=180,
        p90_duration_ms=260,
    )
    crud.create_registration_batch_stage_stat(
        route_db,
        batch_stat_id=left.id,
        stage_key="signup_prepare",
        sample_count=3,
        avg_duration_ms=300.0,
        p50_duration_ms=280,
        p90_duration_ms=320,
    )
    crud.create_registration_batch_stage_stat(
        route_db,
        batch_stat_id=right.id,
        stage_key="signup_prepare",
        sample_count=5,
        avg_duration_ms=450.0,
        p50_duration_ms=430,
        p90_duration_ms=480,
    )

    response = client.post(
        "/api/registration/batch-stats/compare",
        json={"left_id": left.id, "right_id": right.id},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["left"]["id"] == left.id
    assert body["right"]["id"] == right.id
    assert body["summary_diff"]["target_count"] == 2
    assert body["step_diffs"][0]["step_key"] == "create_email"
    assert body["stage_diffs"][0]["stage_key"] == "signup_prepare"


@pytest.mark.parametrize(
    "left_id,right_id",
    [
        (99999, None),
        (None, 99999),
    ],
)
def test_compare_missing_returns_404(client, route_db, left_id, right_id):
    existing = _create_batch_stat(route_db, batch_id="batch-existing")
    payload = {
        "left_id": left_id if left_id is not None else existing.id,
        "right_id": right_id if right_id is not None else existing.id,
    }

    response = client.post("/api/registration/batch-stats/compare", json=payload)
    assert response.status_code == 404
