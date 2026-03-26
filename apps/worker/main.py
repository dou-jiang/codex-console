"""Minimal worker entrypoint for the migrated architecture."""

from packages.email_providers.factory import EmailProviderFactory
from packages.registration_core.engine import RegistrationEngine
from packages.registration_core.models import RegistrationInput


class WorkerRunner:
    """Very small worker façade for processing one stored registration task."""

    def __init__(self, store):
        self.store = store
        self.email_provider_factory = EmailProviderFactory()

    def process_task(self, task_uuid: str) -> dict:
        task = self.store.tasks.get(task_uuid)
        if not task:
            return {"success": False, "error": "task not found"}

        request_payload = dict((task.result or {}).get("request") or {})
        email_service_type = str(request_payload.get("email_service_type") or "").strip()
        if not email_service_type:
            self.store.tasks.update(task_uuid, status="failed", error_message="missing request payload")
            return {"success": False, "error": "missing request payload"}

        email_service = self.email_provider_factory.create(
            email_service_type,
            request_payload.get("email_service_config") or {},
        )
        engine = RegistrationEngine(email_service, task_uuid=task_uuid)
        result = engine.run(
            RegistrationInput(
                email_service_type=email_service_type,
                proxy_url=request_payload.get("proxy_url"),
                email_service_config=request_payload.get("email_service_config"),
            )
        )
        new_status = "completed" if result.success else "failed"
        identity = getattr(result, "identity", None)
        self.store.tasks.update(
            task_uuid,
            status=new_status,
            error_message=result.error_message,
            result={
                "request": request_payload,
                "success": result.success,
                "error_message": result.error_message,
                "identity": {
                    "email": getattr(identity, "email", ""),
                    "account_id": getattr(identity, "account_id", ""),
                    "workspace_id": getattr(identity, "workspace_id", ""),
                },
            },
        )
        return {"success": result.success, "status": new_status}


def create_worker():
    return {"status": "idle"}
