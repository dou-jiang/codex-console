"""Phase 1 registration engine that bridges to the legacy implementation."""

from packages.registration_core.models import AccountIdentity, ExecutionLog, RegistrationInput, RegistrationResult
from src.core.register import RegistrationEngine as LegacyRegistrationEngine


class RegistrationEngine:
    """New registration entrypoint with a stable phase-1-friendly signature."""

    def __init__(self, email_service, callback_logger=None, task_uuid: str | None = None):
        self.email_service = email_service
        self.callback_logger = callback_logger
        self.task_uuid = task_uuid

    def run(self, registration_input: RegistrationInput) -> RegistrationResult:
        legacy_engine = LegacyRegistrationEngine(
            self.email_service,
            proxy_url=registration_input.proxy_url,
            callback_logger=self.callback_logger,
            task_uuid=self.task_uuid,
        )
        legacy_result = legacy_engine.run()

        return RegistrationResult(
            success=bool(getattr(legacy_result, "success", False)),
            error_message=str(getattr(legacy_result, "error_message", "") or ""),
            identity=AccountIdentity(
                email=str(getattr(legacy_result, "email", "") or ""),
                account_id=str(getattr(legacy_result, "account_id", "") or ""),
                workspace_id=str(getattr(legacy_result, "workspace_id", "") or ""),
            ),
            logs=[
                ExecutionLog(message=str(message))
                for message in list(getattr(legacy_result, "logs", []) or [])
            ],
        )
