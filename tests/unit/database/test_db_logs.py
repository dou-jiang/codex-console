from contextlib import contextmanager
from pathlib import Path
import logging

from src.core import db_logs
from src.database.models import AppLog
from src.database.session import DatabaseSessionManager


def test_database_log_handler_persists_log_record(monkeypatch, tmp_path: Path):
    manager = DatabaseSessionManager(database_url=f"sqlite:///{tmp_path / 'logs.db'}")
    manager.create_tables()
    manager.migrate_tables()

    @contextmanager
    def fake_get_db():
        with manager.session_scope() as db:
            yield db

    monkeypatch.setattr(db_logs, "get_db", fake_get_db)

    handler = db_logs.DatabaseLogHandler()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=12,
        msg="hello log",
        args=(),
        exc_info=None,
    )

    handler.emit(record)

    with manager.session_scope() as db:
        saved = db.query(AppLog).filter(AppLog.logger == "test.logger").first()
        assert saved is not None
        assert saved.message == "hello log"
        assert saved.created_at is not None
