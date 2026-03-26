"""Shared helpers for creating and executing migrated registration tasks."""

from packages.account_store.db import AccountStoreDB
from packages.registration_core.models import RegistrationInput
from apps.worker.main import WorkerRunner


def create_register_task_record(
    store: AccountStoreDB,
    *,
    email_service_type: str,
    proxy_url: str | None = None,
    email_service_config: dict | None = None,
):
    task_uuid = __import__("uuid").uuid4().hex
    task = store.tasks.create(
        task_uuid=task_uuid,
        status="pending",
        proxy=proxy_url,
    )
    request_payload = RegistrationInput(
        email_service_type=email_service_type,
        proxy_url=proxy_url,
        email_service_config=email_service_config,
    )
    task = store.tasks.update(
        task_uuid,
        result={
            "request": {
                "email_service_type": request_payload.email_service_type,
                "proxy_url": request_payload.proxy_url,
                "email_service_config": request_payload.email_service_config,
            }
        },
    )
    return task


def run_task_once(database_url: str, task_uuid: str) -> dict:
    store = AccountStoreDB(database_url=database_url)
    runner = WorkerRunner(store)
    return runner.process_task(task_uuid)
