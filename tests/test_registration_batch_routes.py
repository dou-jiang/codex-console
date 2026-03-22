import pytest

from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "registration-batch-routes.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_create_and_update_registration_task_persist_email_address(temp_db):
    task = crud.create_registration_task(
        temp_db,
        task_uuid="task-1",
        email_address="first@gmail.com",
    )

    assert task.email_address == "first@gmail.com"

    updated = crud.update_registration_task(
        temp_db,
        "task-1",
        email_address="second@gmail.com",
    )

    assert updated.email_address == "second@gmail.com"
