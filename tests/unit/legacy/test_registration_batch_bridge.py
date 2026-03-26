import asyncio

from fastapi import BackgroundTasks

from src.web.routes.registration import (
    BatchRegistrationRequest,
    OutlookBatchRegistrationRequest,
    start_batch_registration,
    start_outlook_batch_registration,
)


class _FakeTask:
    def __init__(self, task_uuid: str, proxy: str | None = None):
        self.id = 1
        self.task_uuid = task_uuid
        self.status = "pending"
        self.email_service_id = None
        self.proxy = proxy
        self.logs = None
        self.result = None
        self.error_message = None
        self.created_at = None
        self.started_at = None
        self.completed_at = None


def test_legacy_batch_start_uses_phase2_task_creation(monkeypatch):
    created = {}

    def fake_create_register_task_records(
        store,
        *,
        count,
        email_service_type,
        proxy_url=None,
        email_service_config=None,
        email_service_ids=None,
    ):
        created["count"] = count
        created["email_service_type"] = email_service_type
        created["proxy_url"] = proxy_url
        created["email_service_config"] = email_service_config
        created["email_service_ids"] = email_service_ids
        return [_FakeTask(f"task-{index}", proxy=proxy_url) for index in range(count)]

    monkeypatch.setattr("src.web.routes.registration.create_register_task_records", fake_create_register_task_records)
    monkeypatch.setattr("src.web.routes.registration._create_phase2_store", lambda database_url: object())
    monkeypatch.setattr("src.web.routes.registration.get_session_manager", lambda: type("Mgr", (), {"database_url": "sqlite:///./tmp/legacy.db"})())

    background_tasks = BackgroundTasks()
    request = BatchRegistrationRequest(
        count=2,
        email_service_type="duck_mail",
        proxy="http://127.0.0.1:8080",
        email_service_config={"base_url": "https://mail.example.test"},
        interval_min=1,
        interval_max=2,
        concurrency=1,
        mode="pipeline",
    )

    response = asyncio.run(start_batch_registration(request, background_tasks))

    assert response.count == 2
    assert len(response.tasks) == 2
    assert created["count"] == 2
    assert created["email_service_type"] == "duck_mail"
    assert created["proxy_url"] == "http://127.0.0.1:8080"
    assert created["email_service_config"] == {"base_url": "https://mail.example.test"}
    assert len(background_tasks.tasks) == 1


def test_legacy_outlook_batch_uses_phase2_task_creation(monkeypatch):
    created = {}

    def fake_create_register_task_records(
        store,
        *,
        count,
        email_service_type,
        proxy_url=None,
        email_service_config=None,
        email_service_ids=None,
    ):
        created["count"] = count
        created["email_service_type"] = email_service_type
        created["proxy_url"] = proxy_url
        created["email_service_config"] = email_service_config
        created["email_service_ids"] = email_service_ids
        return [_FakeTask(f"task-{service_id}", proxy=proxy_url) for service_id in email_service_ids]

    class FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None

    class FakeDB:
        def query(self, model):
            return FakeQuery()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("src.web.routes.registration.create_register_task_records", fake_create_register_task_records)
    monkeypatch.setattr("src.web.routes.registration._create_phase2_store", lambda database_url: object())
    monkeypatch.setattr("src.web.routes.registration.get_session_manager", lambda: type("Mgr", (), {"database_url": "sqlite:///./tmp/legacy.db"})())
    monkeypatch.setattr("src.web.routes.registration.get_db", lambda: FakeDB())

    background_tasks = BackgroundTasks()
    request = OutlookBatchRegistrationRequest(
        service_ids=[11, 12],
        skip_registered=False,
        proxy="http://127.0.0.1:8080",
        interval_min=1,
        interval_max=2,
        concurrency=1,
        mode="pipeline",
    )

    response = asyncio.run(start_outlook_batch_registration(request, background_tasks))

    assert response.total == 2
    assert response.to_register == 2
    assert created["count"] == 2
    assert created["email_service_type"] == "outlook"
    assert created["proxy_url"] == "http://127.0.0.1:8080"
    assert created["email_service_ids"] == [11, 12]
    assert len(background_tasks.tasks) == 1
