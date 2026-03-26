from pathlib import Path

from apps.worker.main import WorkerRunner
from packages.account_store.db import AccountStoreDB


def test_worker_marks_task_completed(monkeypatch, tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker.db'}")
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
            self.email_service = email_service

        def run(self, registration_input):
            return FakeResult()

    class FakeFactory:
        def create(self, service_type, config=None, name=None):
            return object()

    monkeypatch.setattr("apps.worker.main.RegistrationEngine", FakeEngine)
    monkeypatch.setattr("apps.worker.main.EmailProviderFactory", FakeFactory)

    runner = WorkerRunner(store)
    outcome = runner.process_task("t-1")

    refreshed = store.tasks.get("t-1")
    assert outcome["success"] is True
    assert refreshed.status == "completed"


def test_worker_marks_task_failed_when_request_missing(tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'worker-fail.db'}")
    store.tasks.create(task_uuid="t-1", status="pending")

    runner = WorkerRunner(store)
    outcome = runner.process_task("t-1")

    refreshed = store.tasks.get("t-1")
    assert outcome["success"] is False
    assert refreshed.status == "failed"
