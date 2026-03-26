import asyncio

from fastapi import BackgroundTasks

from src.web.routes.registration import RegistrationTaskCreate, start_registration


class _FakeTask:
    id = 1
    task_uuid = "task-1"
    status = "pending"
    email_service_id = None
    proxy = "http://127.0.0.1:8080"
    logs = None
    result = None
    error_message = None
    created_at = None
    started_at = None
    completed_at = None


def test_legacy_start_registration_uses_phase2_chain(monkeypatch):
    created = {}

    def fake_create_register_task_record(store, *, email_service_type, proxy_url=None, email_service_config=None):
        created["email_service_type"] = email_service_type
        created["proxy_url"] = proxy_url
        created["email_service_config"] = email_service_config
        return _FakeTask()

    def fake_run_task_once(database_url: str, task_uuid: str):
        return {"success": True, "task_uuid": task_uuid, "status": "completed"}

    class FakeSessionManager:
        database_url = "sqlite:///./tmp/legacy.db"

    monkeypatch.setattr("src.web.routes.registration.create_register_task_record", fake_create_register_task_record)
    monkeypatch.setattr("src.web.routes.registration.run_task_once", fake_run_task_once)
    monkeypatch.setattr("src.web.routes.registration.get_session_manager", lambda: FakeSessionManager())
    monkeypatch.setattr("src.web.routes.registration._create_phase2_store", lambda database_url: object())

    background_tasks = BackgroundTasks()
    request = RegistrationTaskCreate(
        email_service_type="duck_mail",
        proxy="http://127.0.0.1:8080",
        email_service_config={"base_url": "https://mail.example.test"},
    )

    response = asyncio.run(start_registration(request, background_tasks))

    assert response.task_uuid == "task-1"
    assert created["email_service_type"] == "duck_mail"
    assert created["proxy_url"] == "http://127.0.0.1:8080"
    assert created["email_service_config"] == {"base_url": "https://mail.example.test"}
    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]
    assert task.func is fake_run_task_once
    assert task.args == ("sqlite:///./tmp/legacy.db", "task-1")
