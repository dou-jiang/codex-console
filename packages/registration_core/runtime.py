"""Runtime construction helpers for the migrated registration boundary."""

from src.core.register import RegistrationEngine as LegacyRegistrationEngine


def build_legacy_engine(*, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
    """Build the legacy runtime engine behind the migrated boundary."""
    return LegacyRegistrationEngine(
        email_service,
        proxy_url=proxy_url,
        callback_logger=callback_logger,
        task_uuid=task_uuid,
    )
