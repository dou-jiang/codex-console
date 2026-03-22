import asyncio
from contextlib import contextmanager
from copy import deepcopy

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.web import task_manager as task_manager_module
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


@pytest.fixture
def batch_state():
    original_batch_tasks = deepcopy(registration_routes.batch_tasks)
    original_batch_status = deepcopy(task_manager_module._batch_status)
    original_batch_logs = deepcopy(task_manager_module._batch_logs)
    registration_routes.batch_tasks.clear()
    task_manager_module._batch_status.clear()
    task_manager_module._batch_logs.clear()
    try:
        yield
    finally:
        registration_routes.batch_tasks.clear()
        registration_routes.batch_tasks.update(original_batch_tasks)
        task_manager_module._batch_status.clear()
        task_manager_module._batch_status.update(original_batch_status)
        task_manager_module._batch_logs.clear()
        task_manager_module._batch_logs.update(original_batch_logs)


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

    def init_batch(
        self,
        batch_id,
        total,
        *,
        is_unlimited=False,
        consecutive_failures=0,
        max_consecutive_failures=10,
        stop_reason=None,
        domain_stats=None,
    ):
        self._status[batch_id] = {
            "status": "running",
            "total": total,
            "is_unlimited": is_unlimited,
            "consecutive_failures": consecutive_failures,
            "max_consecutive_failures": max_consecutive_failures,
            "stop_reason": stop_reason,
            "domain_stats": [] if domain_stats is None else list(domain_stats),
        }

    def update_batch_status(self, batch_id, **kwargs):
        self._status.setdefault(batch_id, {}).update(kwargs)

    def get_batch_status(self, batch_id):
        return self._status.get(batch_id)

    def is_batch_cancelled(self, batch_id):
        return self._status.get(batch_id, {}).get("cancelled", False)


def test_start_batch_registration_accepts_zero_and_queues_unlimited_runner(route_db, batch_state, monkeypatch):
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
    batch = registration_routes.batch_tasks[response.batch_id]
    assert batch["is_unlimited"] is True
    assert batch["total"] == 0
    assert batch["consecutive_failures"] == 0
    assert batch["max_consecutive_failures"] == 10
    assert batch["stop_reason"] is None
    assert batch["domain_stats"] == []


def test_start_batch_registration_rejects_counts_outside_zero_to_500():
    with pytest.raises(HTTPException):
        asyncio.run(
            registration_routes.start_batch_registration(
                registration_routes.BatchRegistrationRequest(count=501, email_service_type="tempmail"),
                BackgroundTasks(),
            )
        )
    with pytest.raises(HTTPException):
        asyncio.run(
            registration_routes.start_batch_registration(
                registration_routes.BatchRegistrationRequest(count=-1, email_service_type="tempmail"),
                BackgroundTasks(),
            )
        )


def test_get_batch_status_includes_unlimited_metadata(batch_state):
    background = BackgroundTasks()
    response = asyncio.run(
        registration_routes.start_batch_registration(
            registration_routes.BatchRegistrationRequest(count=0, email_service_type="tempmail"),
            background,
        )
    )

    result = asyncio.run(registration_routes.get_batch_status(response.batch_id))

    assert result["is_unlimited"] is True
    assert result["consecutive_failures"] == 0
    assert result["max_consecutive_failures"] == 10
    assert result["stop_reason"] is None
    assert result["domain_stats"] == []
