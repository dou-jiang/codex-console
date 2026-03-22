import asyncio
from contextlib import contextmanager

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.web.routes import registration as registration_routes

from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "registration-batch-routes.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

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

    monkeypatch.setattr(registration_routes, "get_db", _get_db)
    return temp_db


def test_create_and_update_registration_task_persist_email_address(temp_db):
    task = crud.create_registration_task(
        temp_db,
        task_uuid="task-1",
        email_address="first@gmail.com",
    )

    assert task.email_address == "first@gmail.com"

    updated = crud.update_registration_task(
        temp_db,
        "task-1",
        email_address="second@gmail.com",
    )

    assert updated.email_address == "second@gmail.com"


class FakeTaskManager:
    def __init__(self):
        self._status = {}

    def init_batch(self, batch_id, total, **extra):
        self._status[batch_id] = {"status": "running", "total": total, **extra}

    def update_batch_status(self, batch_id, **kwargs):
        self._status.setdefault(batch_id, {}).update(kwargs)

    def get_batch_status(self, batch_id):
        return self._status.get(batch_id)

    def is_batch_cancelled(self, batch_id):
        return self._status.get(batch_id, {}).get("cancelled", False)


def test_start_batch_registration_accepts_zero_and_queues_unlimited_runner(route_db, monkeypatch):
    monkeypatch.setattr(registration_routes, "task_manager", FakeTaskManager())
    background = BackgroundTasks()

    response = asyncio.run(
        registration_routes.start_batch_registration(
            registration_routes.BatchRegistrationRequest(count=0, email_service_type="tempmail"),
            background,
        )
    )

    assert response.count == 0
    assert response.is_unlimited is True
    assert response.tasks == []
    assert background.tasks[0].func is registration_routes.run_unlimited_batch_registration


def test_start_batch_registration_rejects_counts_outside_zero_to_500():
    with pytest.raises(HTTPException):
        asyncio.run(
            registration_routes.start_batch_registration(
                registration_routes.BatchRegistrationRequest(count=501, email_service_type="tempmail"),
                BackgroundTasks(),
            )
        )


def test_get_batch_status_includes_unlimited_metadata(monkeypatch):
    registration_routes.batch_tasks["batch-1"] = {
        "total": 0,
        "completed": 3,
        "success": 1,
        "failed": 2,
        "cancelled": False,
        "finished": False,
        "current_index": 3,
        "is_unlimited": True,
        "consecutive_failures": 2,
        "max_consecutive_failures": 10,
        "stop_reason": None,
        "domain_stats": [],
    }

    result = asyncio.run(registration_routes.get_batch_status("batch-1"))

    assert result["is_unlimited"] is True
    assert result["consecutive_failures"] == 2
    assert result["max_consecutive_failures"] == 10
    assert result["stop_reason"] is None
