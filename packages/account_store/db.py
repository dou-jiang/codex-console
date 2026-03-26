"""Persistence entrypoint for the phase 1 migration."""

from packages.account_store.accounts import AccountStore
from packages.account_store.logs import TaskLogStore
from packages.account_store.tasks import TaskStore
from src.database.session import DatabaseSessionManager


class AccountStoreDB:
    """Owns the minimum persistence boundary needed in phase 1."""

    def __init__(self, database_url: str):
        self.manager = DatabaseSessionManager(database_url)
        self.manager.SessionLocal.configure(expire_on_commit=False)
        self.manager.create_tables()
        self.manager.migrate_tables()
        self.accounts = AccountStore(self.manager)
        self.tasks = TaskStore(self.manager)
        self.logs = TaskLogStore(self.manager)
