"""Minimum account persistence boundary for the phase 1 migration."""

from src.database import crud


class AccountStore:
    """Thin wrapper over the current account CRUD surface."""

    def __init__(self, db_manager):
        self._db_manager = db_manager

    def create(self, **kwargs):
        with self._db_manager.session_scope() as db:
            return crud.create_account(db, **kwargs)
