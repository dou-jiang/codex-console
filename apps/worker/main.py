"""Minimal worker entrypoint for the migrated architecture."""

import time

from packages.account_store.db import AccountStoreDB
from packages.email_providers.factory import EmailProviderFactory
from packages.registration_core.engine import RegistrationEngine
from packages.registration_core.models import RegistrationInput


class WorkerRunner:
    """Very small worker façade for processing one stored registration task."""

    def __init__(self, store):
        self.store = store
        self.email_provider_factory = EmailProviderFactory()

    def _log_task(self, task_uuid: str, message: str) -> None:
        self.store.logs.append(task_uuid, message)

    def process_task(self, task_uuid: str) -> dict:
        task = self.store.tasks.get(task_uuid)
        if not task:
            return {"success": False, "error": "task not found"}

        request_payload = dict((task.result or {}).get("request") or {})
        email_service_type = str(request_payload.get("email_service_type") or "").strip()
        if not email_service_type:
            self.store.tasks.update(task_uuid, status="failed", error_message="missing request payload")
            return {"success": False, "error": "missing request payload"}

        self._log_task(task_uuid, "starting task execution")
        self.store.tasks.update(task_uuid, status="running", error_message="")
        email_service = self.email_provider_factory.create(
            email_service_type,
            request_payload.get("email_service_config") or {},
        )
        engine = RegistrationEngine(
            email_service,
            callback_logger=lambda message: self._log_task(task_uuid, str(message)),
            task_uuid=task_uuid,
        )
        try:
            result = engine.run(
                RegistrationInput(
                    email_service_type=email_service_type,
                    proxy_url=request_payload.get("proxy_url"),
                    email_service_config=request_payload.get("email_service_config"),
                )
            )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self._log_task(task_uuid, f"task failed: {message}")
            self.store.tasks.update(
                task_uuid,
                status="failed",
                error_message=message,
                result={
                    "request": request_payload,
                    "success": False,
                    "error_message": message,
                    "source": "register",
                    "logs": [],
                    "identity": {
                        "email": "",
                        "account_id": "",
                        "workspace_id": "",
                    },
                },
            )
            return {"success": False, "status": "failed", "error": message}

        new_status = "completed" if result.success else "failed"
        identity = getattr(result, "identity", None)
        logs = [str(getattr(entry, "message", entry)) for entry in list(getattr(result, "logs", []) or [])]
        self.store.tasks.update(
            task_uuid,
            status=new_status,
            error_message=result.error_message,
            result={
                "request": request_payload,
                "success": result.success,
                "error_message": result.error_message,
                "source": str(getattr(result, "source", "register") or "register"),
                "logs": logs,
                "identity": {
                    "email": getattr(identity, "email", ""),
                    "account_id": getattr(identity, "account_id", ""),
                    "workspace_id": getattr(identity, "workspace_id", ""),
                },
            },
        )
        self._log_task(task_uuid, f"task {new_status}")
        return {"success": result.success, "status": new_status}

    def process_next_pending(self) -> dict:
        pending = self.store.tasks.list_pending(limit=1)
        if not pending:
            return {"success": False, "error": "no pending tasks"}

        task = pending[0]
        outcome = self.process_task(task.task_uuid)
        outcome["task_uuid"] = task.task_uuid
        return outcome


class WorkerService:
    """Very small service façade for polling and executing pending tasks."""

    def __init__(self, store):
        self.store = store
        self.runner = WorkerRunner(store)

    def run_once(self) -> dict:
        return self.runner.process_next_pending()

    def run_loop(self, max_iterations: int = 1, poll_interval_seconds: float = 1.0) -> list[dict]:
        outcomes: list[dict] = []
        for index in range(max_iterations):
            outcomes.append(self.run_once())
            if index < max_iterations - 1:
                time.sleep(poll_interval_seconds)
        return outcomes


def create_worker(store=None):
    if store is None:
        return {"status": "idle"}
    return WorkerService(store)


def run_worker_loop(
    database_url: str,
    max_iterations: int = 1,
    poll_interval_seconds: float = 1.0,
):
    store = AccountStoreDB(database_url=database_url)
    service = WorkerService(store)
    return service.run_loop(
        max_iterations=max_iterations,
        poll_interval_seconds=poll_interval_seconds,
    )
