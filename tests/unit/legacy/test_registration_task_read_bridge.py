import asyncio
from datetime import datetime

from src.web.routes.registration import get_task, get_task_logs, list_tasks


class _FakeTask:
    def __init__(self):
        self.id = 1
        self.task_uuid = "task-1"
        self.status = "completed"
        self.email_service_id = None
        self.proxy = "http://127.0.0.1:8080"
        self.logs = "line one\nline two"
        self.result = {
            "request": {"email_service_type": "duck_mail"},
            "success": True,
            "error_message": "",
            "source": "register",
            "logs": ["step one"],
            "identity": {
                "email": "tester@example.com",
                "account_id": "acct-1",
                "workspace_id": "ws-1",
            },
        }
        self.error_message = ""
        self.created_at = datetime.utcnow()
        self.started_at = None
        self.completed_at = None
        self.email_service = None


class _FakeTaskStore:
    def __init__(self):
        self.task = _FakeTask()

    def list(self, status=None, limit=100):
        return [self.task]

    def get(self, task_uuid: str):
        return self.task if task_uuid == self.task.task_uuid else None


class _FakeLogStore:
    def list(self, task_uuid: str):
        return ["line one", "line two"]


class _FakeStore:
    def __init__(self):
        self.tasks = _FakeTaskStore()
        self.logs = _FakeLogStore()


def test_legacy_get_tasks_uses_phase2_store(monkeypatch):
    monkeypatch.setattr("src.web.routes.registration._create_phase2_store", lambda database_url: _FakeStore())
    monkeypatch.setattr("src.web.routes.registration.get_session_manager", lambda: type("Mgr", (), {"database_url": "sqlite:///./tmp/legacy.db"})())

    response = asyncio.run(list_tasks(page=1, page_size=20, status=None))

    assert response.total == 1
    assert response.tasks[0].task_uuid == "task-1"
    assert response.tasks[0].status == "completed"


def test_legacy_get_task_uses_phase2_store(monkeypatch):
    monkeypatch.setattr("src.web.routes.registration._create_phase2_store", lambda database_url: _FakeStore())
    monkeypatch.setattr("src.web.routes.registration.get_session_manager", lambda: type("Mgr", (), {"database_url": "sqlite:///./tmp/legacy.db"})())

    response = asyncio.run(get_task("task-1"))

    assert response.task_uuid == "task-1"
    assert response.status == "completed"
    assert response.result["identity"]["email"] == "tester@example.com"


def test_legacy_get_task_logs_uses_phase2_store(monkeypatch):
    monkeypatch.setattr("src.web.routes.registration._create_phase2_store", lambda database_url: _FakeStore())
    monkeypatch.setattr("src.web.routes.registration.get_session_manager", lambda: type("Mgr", (), {"database_url": "sqlite:///./tmp/legacy.db"})())

    response = asyncio.run(get_task_logs("task-1"))

    assert response["task_uuid"] == "task-1"
    assert response["logs"] == ["line one", "line two"]
    assert response["email"] == "tester@example.com"
