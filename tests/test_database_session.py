import sqlite3
from types import SimpleNamespace

from src.database.session import DatabaseSessionManager
from src.database import session as session_module


class _FakeConnection:
    def __init__(self):
        self.statements: list[str] = []
        self.commit_count = 0

    def execute(self, statement):
        self.statements.append(str(statement))
        return SimpleNamespace()

    def commit(self):
        self.commit_count += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, connection: _FakeConnection, dialect_name: str = "postgresql"):
        self._connection = connection
        self.dialect = SimpleNamespace(name=dialect_name)

    def connect(self):
        return self._connection


def _build_manager(connection: _FakeConnection) -> DatabaseSessionManager:
    manager = DatabaseSessionManager.__new__(DatabaseSessionManager)
    manager.database_url = "postgresql://db.example/app"
    manager.engine = _FakeEngine(connection)
    return manager


def test_postgresql_migrate_tables_adds_missing_proxy_location_columns(monkeypatch):
    connection = _FakeConnection()
    manager = _build_manager(connection)
    created_tables = []

    class _FakeInspector:
        def get_columns(self, table_name: str):
            if table_name == "proxies":
                return [{"name": "id"}, {"name": "host"}, {"name": "port"}]
            return [{"name": "id"}]

    monkeypatch.setattr(
        session_module.Base.metadata,
        "create_all",
        lambda bind: created_tables.append(bind),
    )
    monkeypatch.setattr(session_module, "inspect", lambda conn: _FakeInspector())

    manager.migrate_tables()

    assert created_tables == [manager.engine]
    assert any('ALTER TABLE "proxies" ADD COLUMN IF NOT EXISTS "country" VARCHAR(100)' in stmt for stmt in connection.statements)
    assert any('ALTER TABLE "proxies" ADD COLUMN IF NOT EXISTS "city" VARCHAR(100)' in stmt for stmt in connection.statements)


def test_postgresql_migrate_tables_skips_existing_proxy_location_columns(monkeypatch):
    connection = _FakeConnection()
    manager = _build_manager(connection)

    class _FakeInspector:
        def get_columns(self, table_name: str):
            if table_name == "proxies":
                return [
                    {"name": "id"},
                    {"name": "host"},
                    {"name": "port"},
                    {"name": "country"},
                    {"name": "city"},
                ]
            return [{"name": "id"}]

    monkeypatch.setattr(session_module.Base.metadata, "create_all", lambda bind: None)
    monkeypatch.setattr(session_module, "inspect", lambda conn: _FakeInspector())

    manager.migrate_tables()

    assert not any('ALTER TABLE "proxies"' in stmt for stmt in connection.statements)


def test_postgresql_migrate_tables_adds_registration_task_email_column(monkeypatch):
    connection = _FakeConnection()
    manager = _build_manager(connection)

    class _FakeInspector:
        def get_columns(self, table_name: str):
            if table_name == "registration_tasks":
                return [{"name": "id"}, {"name": "task_uuid"}]
            return [{"name": "id"}]

    monkeypatch.setattr(session_module.Base.metadata, "create_all", lambda bind: None)
    monkeypatch.setattr(session_module, "inspect", lambda conn: _FakeInspector())

    manager.migrate_tables()

    assert any(
        'ALTER TABLE "registration_tasks" ADD COLUMN IF NOT EXISTS "email_address" VARCHAR(255)' in stmt
        for stmt in connection.statements
    )


def test_sqlite_migrate_tables_adds_registration_task_email_column(tmp_path):
    db_path = tmp_path / "legacy-registration-tasks.db"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE registration_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_uuid VARCHAR(36) NOT NULL,
                status VARCHAR(20),
                email_service_id INTEGER,
                proxy VARCHAR(255),
                logs TEXT,
                result TEXT,
                error_message TEXT,
                created_at DATETIME,
                started_at DATETIME,
                completed_at DATETIME
            )
            """
        )
        conn.commit()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    manager.migrate_tables()

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('registration_tasks')").fetchall()
        }

    assert "email_address" in columns
