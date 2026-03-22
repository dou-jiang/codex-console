import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

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
        self._task_status = {}
        self._batch_logs = {}

    def is_cancelled(self, task_uuid):
        return self._task_status.get(task_uuid, {}).get("cancelled", False)

    def update_status(self, task_uuid, status, **kwargs):
        self._task_status.setdefault(task_uuid, {}).update({"status": status, **kwargs})

    def create_log_callback(self, task_uuid, prefix="", batch_id=""):
        return lambda message: None

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

    def add_batch_log(self, batch_id, log_message):
        self._batch_logs.setdefault(batch_id, []).append(log_message)

    def is_batch_cancelled(self, batch_id):
        return self._status.get(batch_id, {}).get("cancelled", False)


@pytest.fixture
def fake_task_manager(monkeypatch, batch_state):
    manager = FakeTaskManager()
    monkeypatch.setattr(registration_routes, "task_manager", manager)
    return manager


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


def test_run_sync_registration_task_persists_email_address_even_on_failure(route_db, fake_task_manager, monkeypatch):
    crud.create_registration_task(route_db, task_uuid="task-1")

    monkeypatch.setattr(
        registration_routes,
        "get_settings",
        lambda: SimpleNamespace(tempmail_base_url="http://mail", tempmail_timeout=1, tempmail_max_retries=0),
    )
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda *args, **kwargs: SimpleNamespace(service_type=SimpleNamespace(value="tempmail")),
    )

    class FakeEngine:
        def __init__(self, **kwargs):
            pass

        def run(self):
            return SimpleNamespace(
                success=False,
                email="failed@gmail.com",
                error_message="boom",
                to_dict=lambda: {"email": "failed@gmail.com"},
            )

    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeEngine)

    registration_routes._run_sync_registration_task("task-1", "tempmail", None, None)

    task = crud.get_registration_task(route_db, "task-1")
    assert task.status == "failed"
    assert task.email_address == "failed@gmail.com"


def test_run_batch_registration_attaches_sorted_domain_stats(route_db, fake_task_manager, monkeypatch):
    task_ids = []
    for name in ["a", "b", "c"]:
        task = crud.create_registration_task(route_db, task_uuid=f"task-{name}")
        task_ids.append(task.task_uuid)

    outcomes = iter([
        ("completed", "one@yahoo.com"),
        ("failed", "two@gmail.com"),
        ("completed", "three@gmail.com"),
    ])

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        status, email = next(outcomes)
        crud.update_registration_task(
            route_db,
            task_uuid,
            status=status,
            email_address=email,
            completed_at=datetime.utcnow(),
            result={"email": email} if status == "completed" else None,
            error_message=None if status == "completed" else "boom",
        )

    monkeypatch.setattr(registration_routes, "run_registration_task", fake_run_registration_task)

    asyncio.run(
        registration_routes.run_batch_registration(
            batch_id="fixed-1",
            task_uuids=task_ids,
            email_service_type="tempmail",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
            interval_min=0,
            interval_max=0,
            concurrency=1,
            mode="parallel",
        )
    )

    stats = registration_routes.batch_tasks["fixed-1"]["domain_stats"]
    assert [row["domain"] for row in stats] == ["yahoo.com", "gmail.com"]


def test_run_batch_pipeline_finalizes_domain_stats_before_marking_cancelled_batch_finished(route_db, fake_task_manager, monkeypatch):
    task_ids = []
    for name in ["a", "b"]:
        task = crud.create_registration_task(route_db, task_uuid=f"cancel-{name}")
        task_ids.append(task.task_uuid)

    monkeypatch.setattr(fake_task_manager, "is_batch_cancelled", lambda batch_id: True)

    observed = {}

    def fake_finalize(batch_id, task_uuids):
        observed["route_finished"] = registration_routes.batch_tasks[batch_id]["finished"]
        observed["manager_finished"] = fake_task_manager.get_batch_status(batch_id).get("finished", False)
        observed["task_uuids"] = list(task_uuids)

    monkeypatch.setattr(registration_routes, "_finalize_batch_domain_stats", fake_finalize)

    asyncio.run(
        registration_routes.run_batch_pipeline(
            batch_id="cancelled-fixed-1",
            task_uuids=task_ids,
            email_service_type="tempmail",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
            interval_min=0,
            interval_max=0,
            concurrency=1,
        )
    )

    assert observed == {
        "route_finished": False,
        "manager_finished": False,
        "task_uuids": task_ids,
    }
    assert registration_routes.batch_tasks["cancelled-fixed-1"]["finished"] is True
    assert fake_task_manager.get_batch_status("cancelled-fixed-1")["finished"] is True
    assert fake_task_manager.get_batch_status("cancelled-fixed-1")["status"] == "cancelled"


def test_run_unlimited_batch_registration_stops_after_eleven_consecutive_failures(route_db, fake_task_manager, monkeypatch):
    outcomes = iter([("failed", f"user{i}@bad.com") for i in range(11)] + [("completed", "late@good.com")])

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        status, email = next(outcomes)
        crud.update_registration_task(
            route_db,
            task_uuid,
            status=status,
            email_address=email,
            completed_at=datetime.utcnow(),
            result={"email": email} if status == "completed" else None,
            error_message=None if status == "completed" else "boom",
        )

    monkeypatch.setattr(registration_routes, "run_registration_task", fake_run_registration_task)

    asyncio.run(
        registration_routes.run_unlimited_batch_registration(
            batch_id="unlimited-1",
            email_service_type="tempmail",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
            interval_min=0,
            interval_max=0,
            concurrency=1,
            mode="parallel",
        )
    )

    state = registration_routes.batch_tasks["unlimited-1"]
    assert state["completed"] == 11
    assert state["failed"] == 11
    assert state["consecutive_failures"] == 11
    assert state["stop_reason"] == "too_many_consecutive_failures"


def test_get_outlook_batch_status_includes_domain_stats_if_present(batch_state):
    registration_routes.batch_tasks["outlook-1"] = {
        "total": 2,
        "completed": 2,
        "success": 1,
        "failed": 1,
        "skipped": 0,
        "current_index": 1,
        "cancelled": False,
        "finished": True,
        "logs": [],
        "domain_stats": [{"domain": "gmail.com", "total": 2, "success": 1, "failed": 1}],
    }

    result = asyncio.run(registration_routes.get_outlook_batch_status("outlook-1"))

    assert result["domain_stats"] == [{"domain": "gmail.com", "total": 2, "success": 1, "failed": 1}]
