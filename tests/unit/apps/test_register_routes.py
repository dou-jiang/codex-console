from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app


def test_create_register_task(tmp_path: Path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    client = TestClient(app)

    response = client.post(
        "/tasks/register",
        json={"email_service_type": "duck_mail"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["task_uuid"]


def test_get_register_task(tmp_path: Path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    client = TestClient(app)

    created = client.post(
        "/tasks/register",
        json={"email_service_type": "duck_mail"},
    ).json()

    response = client.get(f"/tasks/{created['task_uuid']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_uuid"] == created["task_uuid"]
    assert payload["status"] == "pending"
    assert payload["error_message"] == ""


def test_run_register_task(monkeypatch, tmp_path: Path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    client = TestClient(app)

    created = client.post(
        "/tasks/register",
        json={"email_service_type": "duck_mail"},
    ).json()

    class FakeRunner:
        def __init__(self, store):
            self.store = store

        def process_task(self, task_uuid: str):
            self.store.tasks.update(task_uuid, status="completed")
            return {"success": True, "status": "completed"}

    monkeypatch.setattr("apps.api.routes.tasks.WorkerRunner", FakeRunner)

    response = client.post(f"/tasks/{created['task_uuid']}/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["status"] == "completed"


def test_run_next_pending_task(monkeypatch, tmp_path: Path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    client = TestClient(app)

    client.post("/tasks/register", json={"email_service_type": "duck_mail"})

    class FakeRunner:
        def __init__(self, store):
            self.store = store

        def process_next_pending(self):
            pending = self.store.tasks.list_pending(limit=1)
            task_uuid = pending[0].task_uuid
            self.store.tasks.update(task_uuid, status="completed")
            return {"success": True, "status": "completed", "task_uuid": task_uuid}

    monkeypatch.setattr("apps.api.routes.tasks.WorkerRunner", FakeRunner)

    response = client.post("/tasks/run-next")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["status"] == "completed"
    assert payload["task_uuid"]


def test_list_register_tasks(tmp_path: Path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    client = TestClient(app)

    first = client.post("/tasks/register", json={"email_service_type": "duck_mail"}).json()
    second = client.post("/tasks/register", json={"email_service_type": "tempmail"}).json()

    response = client.get("/tasks")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    task_ids = {item["task_uuid"] for item in payload["items"]}
    assert first["task_uuid"] in task_ids
    assert second["task_uuid"] in task_ids
