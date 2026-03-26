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
