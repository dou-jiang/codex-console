"""Result assembly helpers for the migrated registration boundary."""

from packages.registration_core.models import AccountIdentity, ExecutionLog, RegistrationResult


def build_registration_result(legacy_result) -> RegistrationResult:
    """Convert the legacy runtime result into the migrated contract."""
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
