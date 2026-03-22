from datetime import datetime

from sqlalchemy import inspect
import pytest

from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "scheduler-data-model.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_scheduler_tables_and_account_columns_exist_after_init(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()

    inspector = inspect(manager.engine)
    assert "scheduled_plans" in inspector.get_table_names()
    assert "scheduled_runs" in inspector.get_table_names()

    account_columns = {col["name"] for col in inspector.get_columns("accounts")}
    assert {"primary_cpa_service_id", "invalidated_at", "invalid_reason"} <= account_columns


def test_create_plan_and_run_round_trip(temp_db):
    service = crud.create_cpa_service(
        temp_db,
        name="main cpa",
        api_url="https://example.test/api",
        api_token="token",
    )

    plan = crud.create_scheduled_plan(
        temp_db,
        name="nightly cleanup",
        task_type="cpa_cleanup",
        cpa_service_id=service.id,
        trigger_type="interval",
        interval_value=30,
        interval_unit="minutes",
        config={"max_cleanup_count": 20},
    )
    run = crud.create_scheduled_run(temp_db, plan_id=plan.id, trigger_source="manual")

    listed_plans = crud.get_scheduled_plans(temp_db)

    assert plan.config["max_cleanup_count"] == 20
    assert run.status == "running"
    assert [row.id for row in listed_plans] == [plan.id]


def test_create_scheduled_plan_rejects_missing_cpa_service(temp_db):
    with pytest.raises(ValueError, match="cpa service"):
        crud.create_scheduled_plan(
            temp_db,
            name="nightly cleanup",
            task_type="cpa_cleanup",
            cpa_service_id=999,
            trigger_type="interval",
            interval_value=30,
            interval_unit="minutes",
            config={"max_cleanup_count": 20},
        )


def test_create_scheduled_run_rejects_missing_plan(temp_db):
    with pytest.raises(ValueError, match="scheduled plan"):
        crud.create_scheduled_run(temp_db, plan_id=999, trigger_source="manual")


def test_append_scheduled_run_log_persists_full_log_history(temp_db):
    service = crud.create_cpa_service(
        temp_db,
        name="main cpa",
        api_url="https://example.test/api",
        api_token="token",
    )
    plan = crud.create_scheduled_plan(
        temp_db,
        name="nightly cleanup",
        task_type="cpa_cleanup",
        cpa_service_id=service.id,
        trigger_type="interval",
        interval_value=30,
        interval_unit="minutes",
        config={"max_cleanup_count": 20},
    )
    run = crud.create_scheduled_run(temp_db, plan_id=plan.id, trigger_source="manual")

    assert crud.append_scheduled_run_log(temp_db, run.id, "first line") is True
    assert crud.append_scheduled_run_log(temp_db, run.id, "second line") is True

    refreshed = temp_db.get(type(run), run.id)
    assert refreshed is not None
    assert refreshed.logs == "first line\nsecond line"


def test_finish_scheduled_run_persists_completion_fields(temp_db):
    service = crud.create_cpa_service(
        temp_db,
        name="main cpa",
        api_url="https://example.test/api",
        api_token="token",
    )
    plan = crud.create_scheduled_plan(
        temp_db,
        name="nightly cleanup",
        task_type="cpa_cleanup",
        cpa_service_id=service.id,
        trigger_type="interval",
        interval_value=30,
        interval_unit="minutes",
        config={"max_cleanup_count": 20},
    )
    run = crud.create_scheduled_run(temp_db, plan_id=plan.id, trigger_source="manual")
    finished_at = datetime(2026, 3, 22, 10, 0, 0)

    finished = crud.finish_scheduled_run(
        temp_db,
        run_id=run.id,
        status="failed",
        summary={"remote_deleted": 3},
        error_message="remote timeout",
        finished_at=finished_at,
    )

    assert finished is not None
    assert finished.status == "failed"
    assert finished.summary == {"remote_deleted": 3}
    assert finished.error_message == "remote timeout"
    assert finished.finished_at == finished_at
