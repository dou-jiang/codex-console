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
    assert payload["task"]["task_uuid"] == payload["task_uuid"]
    assert payload["task"]["status"] == "pending"


def test_create_register_task_with_proxy_and_email_config(tmp_path: Path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    client = TestClient(app)

    response = client.post(
        "/tasks/register",
        json={
            "email_service_type": "duck_mail",
            "proxy_url": "http://127.0.0.1:8080",
            "email_service_config": {"base_url": "https://mail.example.test", "default_domain": "example.test"},
        },
    )

    assert response.status_code == 202
    created = response.json()

    detail = client.get(f"/tasks/{created['task_uuid']}").json()
    assert detail["proxy"] == "http://127.0.0.1:8080"
    assert detail["result"]["request"]["email_service_config"]["default_domain"] == "example.test"


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
    assert payload["task"]["task_uuid"] == created["task_uuid"]
    assert payload["task"]["status"] == "pending"


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
            self.store.logs.append(task_uuid, "worker line one")
            self.store.logs.append(task_uuid, "worker line two")
            self.store.tasks.update(
                task_uuid,
                status="completed",
                result={
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
                },
            )
            return {"success": True, "status": "completed"}

    monkeypatch.setattr("apps.api.routes.tasks.WorkerRunner", FakeRunner)

    response = client.post(f"/tasks/{created['task_uuid']}/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["status"] == "completed"
    assert payload["task"]["task_uuid"] == created["task_uuid"]
    assert payload["task"]["status"] == "completed"
    assert payload["outcome"]["success"] is True

    detail = client.get(f"/tasks/{created['task_uuid']}").json()
    assert detail["result"]["source"] == "register"
    assert detail["result"]["logs"] == ["step one"]
    assert detail["result"]["identity"]["email"] == "tester@example.com"
    assert detail["logs"] == ["worker line one", "worker line two"]


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
    assert payload["task"]["task_uuid"] == payload["task_uuid"]
    assert payload["task"]["status"] == "completed"
    assert payload["outcome"]["success"] is True


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


def test_get_task_logs(tmp_path: Path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    client = TestClient(app)

    created = client.post("/tasks/register", json={"email_service_type": "duck_mail"}).json()
    task_uuid = created["task_uuid"]

    app.state.store.logs.append(task_uuid, "line one")
    app.state.store.logs.append(task_uuid, "line two")

    response = client.get(f"/tasks/{task_uuid}/logs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_uuid"] == task_uuid
    assert payload["logs"] == ["line one", "line two"]
