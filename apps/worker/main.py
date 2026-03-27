"""Minimal worker entrypoint for the migrated architecture."""

import argparse
import inspect
from pathlib import Path
import time

from packages.account_store.db import AccountStoreDB
from packages.email_providers.factory import EmailProviderFactory
from packages.registration_core.engine import RegistrationEngine
from packages.registration_core.models import RegistrationInput


class WorkerRunner:
    """Very small worker façade for processing one stored registration task."""

    def __init__(self, store, log_flush_threshold: int = 10):
        self.store = store
        self.email_provider_factory = EmailProviderFactory()
        self.log_flush_threshold = max(1, int(log_flush_threshold))
        self._log_buffers: dict[str, list[str]] = {}

    def _flush_task_logs(self, task_uuid: str) -> None:
        lines = self._log_buffers.get(task_uuid) or []
        if not lines:
            return
        self.store.logs.append_many(task_uuid, lines)
        self._log_buffers[task_uuid] = []

    def _log_task(self, task_uuid: str, message: str, *, flush: bool = False) -> None:
        buffer = self._log_buffers.setdefault(task_uuid, [])
        buffer.append(str(message))
        if flush or len(buffer) >= self.log_flush_threshold:
            self._flush_task_logs(task_uuid)

    def process_task(self, task_uuid: str) -> dict:
        task = self.store.tasks.get(task_uuid)
        if not task:
            return {"success": False, "error": "task not found"}

        request_payload = dict((task.result or {}).get("request") or {})
        email_service_type = str(request_payload.get("email_service_type") or "").strip()
        if not email_service_type:
            self.store.tasks.update(task_uuid, status="failed", error_message="missing request payload")
            return {"success": False, "error": "missing request payload"}

        email_service_config = request_payload.get("email_service_config") or {}
        if not email_service_config and getattr(task, "email_service_id", None):
            service_config = self.store.services.get_config(task.email_service_id)
            if service_config:
                email_service_config = service_config

        self._log_task(task_uuid, "starting task execution")
        self.store.tasks.update(task_uuid, status="running", error_message="")
        email_service = self.email_provider_factory.create(
            email_service_type,
            email_service_config,
        )
        engine_kwargs = {
            "callback_logger": lambda message: self._log_task(task_uuid, str(message)),
            "task_uuid": task_uuid,
        }
        if "persist_task_logs" in inspect.signature(RegistrationEngine).parameters:
            engine_kwargs["persist_task_logs"] = False

        engine = RegistrationEngine(
            email_service,
            **engine_kwargs,
        )
        try:
            result = engine.run(
                RegistrationInput(
                    email_service_type=email_service_type,
                    proxy_url=request_payload.get("proxy_url"),
                    email_service_config=email_service_config,
                )
            )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self._log_task(task_uuid, f"task failed: {message}", flush=True)
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
            self._flush_task_logs(task_uuid)
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
        self._log_task(task_uuid, f"task {new_status}", flush=True)
        self._flush_task_logs(task_uuid)
        return {"success": result.success, "status": new_status}

    def process_next_pending(self) -> dict:
        task = self.store.tasks.claim_next_pending()
        if not task:
            return {"success": False, "error": "no pending tasks"}
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

    def run_loop(
        self,
        max_iterations: int = 1,
        poll_interval_seconds: float = 1.0,
        max_idle_cycles: int | None = None,
    ) -> list[dict]:
        outcomes: list[dict] = []
        idle_cycles = 0
        for index in range(max_iterations):
            outcome = self.run_once()
            outcomes.append(outcome)
            if outcome.get("error") == "no pending tasks":
                idle_cycles += 1
                if max_idle_cycles is not None and idle_cycles >= max_idle_cycles:
                    break
            else:
                idle_cycles = 0
            if index < max_iterations - 1:
                time.sleep(poll_interval_seconds)
        return outcomes


class WorkerLock:
    """Simple single-instance file lock for the worker loop."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import os

            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self._fd is not None:
            import os

            os.close(self._fd)
            self._fd = None
        if self.path.exists():
            self.path.unlink()


def create_worker(store=None):
    if store is None:
        return {"status": "idle"}
    return WorkerService(store)


def run_worker_loop(
    database_url: str,
    max_iterations: int = 1,
    poll_interval_seconds: float = 1.0,
    max_idle_cycles: int | None = None,
    lock_path: str | None = None,
):
    worker_lock = WorkerLock(lock_path) if lock_path else None
    if worker_lock and not worker_lock.acquire():
        return [{"success": False, "error": "worker already running"}]

    store = AccountStoreDB(database_url=database_url)
    service = WorkerService(store)
    try:
        return service.run_loop(
            max_iterations=max_iterations,
            poll_interval_seconds=poll_interval_seconds,
            max_idle_cycles=max_idle_cycles,
        )
    finally:
        if worker_lock:
            worker_lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the migrated worker loop.")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--max-iterations", type=int, default=1)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--max-idle-cycles", type=int, default=None)
    parser.add_argument("--lock-path", default=None)
    args = parser.parse_args(argv)

    run_worker_loop(
        database_url=args.database_url,
        max_iterations=args.max_iterations,
        poll_interval_seconds=args.poll_interval_seconds,
        max_idle_cycles=args.max_idle_cycles,
        lock_path=args.lock_path,
    )
    return 0
