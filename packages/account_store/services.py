"""Minimum email-service lookup boundary for the migrated flow."""

from src.database import crud


class EmailServiceStore:
    """Thin wrapper for reading persisted email-service configuration."""

    def __init__(self, db_manager):
        self._db_manager = db_manager

    def get_config(self, service_id: int) -> dict | None:
        with self._db_manager.session_scope() as db:
            service = crud.get_email_service_by_id(db, service_id)
            if not service:
                return None
            return dict(service.config or {})
