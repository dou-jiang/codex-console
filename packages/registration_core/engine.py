"""Phase 1 registration engine that bridges to the legacy implementation."""

from packages.registration_core.models import RegistrationInput, RegistrationResult
from packages.registration_core.result_builder import build_registration_result
from packages.registration_core.runtime import build_legacy_engine


class RegistrationEngine:
    """New registration entrypoint with a stable phase-1-friendly signature."""

    def __init__(self, email_service, callback_logger=None, task_uuid: str | None = None):
        self.email_service = email_service
        self.callback_logger = callback_logger
        self.task_uuid = task_uuid

    def run(self, registration_input: RegistrationInput) -> RegistrationResult:
        legacy_engine = build_legacy_engine(
            email_service=self.email_service,
            proxy_url=registration_input.proxy_url,
            callback_logger=self.callback_logger,
            task_uuid=self.task_uuid,
        )
        legacy_result = legacy_engine.run()
        return build_registration_result(legacy_result)
