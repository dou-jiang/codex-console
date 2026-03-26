from pathlib import Path

from apps.worker.main import WorkerRunner, WorkerService, run_worker_loop
from packages.account_store.db import AccountStoreDB
from packages.registration_core.models import AccountIdentity, ExecutionLog


def test_worker_marks_task_completed(monkeypatch, tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker.db'}")
    task = store.tasks.create(task_uuid="t-1", status="pending")
    store.tasks.update(
        task.task_uuid,
        result={
            "request": {
                "email_service_type": "duck_mail",
                "proxy_url": "http://127.0.0.1:8080",
                "email_service_config": {"base_url": "https://mail.example.test"},
            }
        },
    )

    class FakeResult:
        success = True
        error_message = ""
        identity = AccountIdentity(
            email="tester@example.com",
            account_id="acct-1",
            workspace_id="ws-1",
        )
        logs = [ExecutionLog(message="step one"), ExecutionLog(message="step two")]
        source = "register"

    class FakeEngine:
        def __init__(self, email_service, callback_logger=None, task_uuid=None):
            self.email_service = email_service

        def run(self, registration_input):
            assert registration_input.proxy_url == "http://127.0.0.1:8080"
            assert registration_input.email_service_config == {"base_url": "https://mail.example.test"}
            return FakeResult()

    class FakeFactory:
        def create(self, service_type, config=None, name=None):
            assert config == {"base_url": "https://mail.example.test"}
            return object()

    monkeypatch.setattr("apps.worker.main.RegistrationEngine", FakeEngine)
    monkeypatch.setattr("apps.worker.main.EmailProviderFactory", FakeFactory)

    runner = WorkerRunner(store)
    outcome = runner.process_task("t-1")

    refreshed = store.tasks.get("t-1")
    assert outcome["success"] is True
    assert refreshed.status == "completed"
    assert refreshed.result["identity"]["email"] == "tester@example.com"
    assert refreshed.result["identity"]["account_id"] == "acct-1"
    assert refreshed.result["identity"]["workspace_id"] == "ws-1"
    assert refreshed.result["source"] == "register"
    assert refreshed.result["logs"] == ["step one", "step two"]
    log_lines = store.logs.list("t-1")
    assert any("starting task execution" in line for line in log_lines)
    assert any("task completed" in line for line in log_lines)


def test_worker_marks_task_failed_when_request_missing(tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker-fail.db'}")
    store.tasks.create(task_uuid="t-1", status="pending")

    runner = WorkerRunner(store)
    outcome = runner.process_task("t-1")

    refreshed = store.tasks.get("t-1")
    assert outcome["success"] is False
    assert refreshed.status == "failed"


def test_worker_process_next_pending(monkeypatch, tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker-next.db'}")
    first = store.tasks.create(task_uuid="t-1", status="pending")
    second = store.tasks.create(task_uuid="t-2", status="pending")
    store.tasks.update(
        first.task_uuid,
        result={"request": {"email_service_type": "duck_mail"}},
    )
    store.tasks.update(
        second.task_uuid,
        result={"request": {"email_service_type": "duck_mail"}},
    )

    class FakeResult:
        success = True
        error_message = ""
        identity = AccountIdentity(
            email="tester@example.com",
            account_id="acct-1",
            workspace_id="ws-1",
        )
        logs = [ExecutionLog(message="step one"), ExecutionLog(message="step two")]
        source = "register"

    class FakeEngine:
        def __init__(self, email_service, callback_logger=None, task_uuid=None):
            self.email_service = email_service

        def run(self, registration_input):
            return FakeResult()

    class FakeFactory:
        def create(self, service_type, config=None, name=None):
            return object()

    monkeypatch.setattr("apps.worker.main.RegistrationEngine", FakeEngine)
    monkeypatch.setattr("apps.worker.main.EmailProviderFactory", FakeFactory)

    runner = WorkerRunner(store)
    outcome = runner.process_next_pending()

    assert outcome["success"] is True
    assert outcome["task_uuid"] in {"t-1", "t-2"}


def test_worker_marks_task_running_before_execution(monkeypatch, tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker-running.db'}")
    task = store.tasks.create(task_uuid="t-1", status="pending")
    store.tasks.update(
        task.task_uuid,
        result={"request": {"email_service_type": "duck_mail"}},
    )

    class FakeResult:
        success = True
        error_message = ""
        identity = None
        logs = []

    class FakeEngine:
        def __init__(self, email_service, callback_logger=None, task_uuid=None):
            self.task_uuid = task_uuid

        def run(self, registration_input):
            current = store.tasks.get(self.task_uuid)
            assert current.status == "running"
            return FakeResult()

    class FakeFactory:
        def create(self, service_type, config=None, name=None):
            return object()

    monkeypatch.setattr("apps.worker.main.RegistrationEngine", FakeEngine)
    monkeypatch.setattr("apps.worker.main.EmailProviderFactory", FakeFactory)

    runner = WorkerRunner(store)
    outcome = runner.process_task("t-1")

    assert outcome["success"] is True
    assert store.tasks.get("t-1").status == "completed"


def test_worker_captures_engine_exception(tmp_path: Path, monkeypatch):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker-error.db'}")
    task = store.tasks.create(task_uuid="t-1", status="pending")
    store.tasks.update(
        task.task_uuid,
        result={"request": {"email_service_type": "duck_mail"}},
    )

    class FakeEngine:
        def __init__(self, email_service, callback_logger=None, task_uuid=None):
            pass

        def run(self, registration_input):
            raise RuntimeError("boom")

    class FakeFactory:
        def create(self, service_type, config=None, name=None):
            return object()

    monkeypatch.setattr("apps.worker.main.RegistrationEngine", FakeEngine)
    monkeypatch.setattr("apps.worker.main.EmailProviderFactory", FakeFactory)

    runner = WorkerRunner(store)
    outcome = runner.process_task("t-1")

    refreshed = store.tasks.get("t-1")
    assert outcome["success"] is False
    assert refreshed.status == "failed"
    assert refreshed.error_message == "boom"
    assert refreshed.result["error_message"] == "boom"
    assert refreshed.result["logs"] == []
    log_lines = store.logs.list("t-1")
    assert any("starting task execution" in line for line in log_lines)
    assert any("task failed: boom" in line for line in log_lines)


def test_worker_service_run_once_returns_no_pending(tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker-service.db'}")
    service = WorkerService(store)

    outcome = service.run_once()

    assert outcome["success"] is False
    assert outcome["error"] == "no pending tasks"


def test_worker_service_run_once_processes_pending(monkeypatch, tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker-service-next.db'}")
    store.tasks.create(task_uuid="t-1", status="pending")

    class FakeRunner:
        def __init__(self, inner_store):
            self.store = inner_store

        def process_next_pending(self):
            return {"success": True, "task_uuid": "t-1", "status": "completed"}

    monkeypatch.setattr("apps.worker.main.WorkerRunner", FakeRunner)

    service = WorkerService(store)
    outcome = service.run_once()

    assert outcome["success"] is True
    assert outcome["task_uuid"] == "t-1"


def test_worker_service_run_loop_calls_run_once(monkeypatch, tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker-service-loop.db'}")
    service = WorkerService(store)
    outcomes = [
        {"success": False, "error": "no pending tasks"},
        {"success": True, "task_uuid": "t-2", "status": "completed"},
    ]

    def fake_run_once():
        return outcomes.pop(0)

    monkeypatch.setattr(service, "run_once", fake_run_once)

    result = service.run_loop(max_iterations=2, poll_interval_seconds=0)

    assert result == [
        {"success": False, "error": "no pending tasks"},
        {"success": True, "task_uuid": "t-2", "status": "completed"},
    ]


def test_run_worker_loop_uses_service(monkeypatch, tmp_path: Path):
    outcomes = [{"success": True, "task_uuid": "t-1", "status": "completed"}]

    class FakeService:
        def __init__(self, store):
            self.store = store

        def run_loop(self, max_iterations: int, poll_interval_seconds: float):
            assert max_iterations == 1
            assert poll_interval_seconds == 0
            return outcomes

    monkeypatch.setattr("apps.worker.main.WorkerService", FakeService)

    result = run_worker_loop(
        database_url=f"sqlite:///{tmp_path / 'worker-cli.db'}",
        max_iterations=1,
        poll_interval_seconds=0,
    )

    assert result == outcomes
