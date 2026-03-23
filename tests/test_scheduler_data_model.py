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
    scheduled_plan_columns = {col["name"] for col in inspector.get_columns("scheduled_plans")}
    assert "config_meta" in scheduled_plan_columns
    scheduled_run_columns = {col["name"] for col in inspector.get_columns("scheduled_runs")}
    assert "task_type" in scheduled_run_columns
    assert "stop_requested_at" in scheduled_run_columns
    assert "stop_requested_by" in scheduled_run_columns
    assert "stop_reason" in scheduled_run_columns
    assert "last_log_at" in scheduled_run_columns
    assert "log_version" in scheduled_run_columns


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
        config_meta={
            "max_cleanup_count": {
                "key_description": "单次最多清理多少个失效账号",
                "value_description": "10-20 更安全",
                "value_type": "number",
            }
        },
    )
    run = crud.create_scheduled_run(
        temp_db,
        plan_id=plan.id,
        trigger_source="manual",
        task_type=plan.task_type,
    )

    listed_plans = crud.get_scheduled_plans(temp_db)

    assert plan.config["max_cleanup_count"] == 20
    assert plan.config_meta["max_cleanup_count"]["value_type"] == "number"
    assert run.status == "running"
    assert run.task_type == "cpa_cleanup"
    assert run.log_version == 0
    assert run.stop_requested_at is None
    assert run.stop_requested_by is None
    assert run.stop_reason is None
    assert run.last_log_at is None
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
    assert refreshed.log_version == 2
    assert refreshed.last_log_at is not None


def test_create_scheduled_run_uses_plan_task_type_by_default(temp_db):
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

    assert run.task_type == "cpa_cleanup"


def test_get_scheduled_run_by_id_returns_existing_or_none(temp_db):
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

    assert crud.get_scheduled_run_by_id(temp_db, run.id) is not None
    assert crud.get_scheduled_run_by_id(temp_db, run.id).id == run.id
    assert crud.get_scheduled_run_by_id(temp_db, 999999) is None


def test_get_scheduled_runs_applies_filters(temp_db):
    cpa1 = crud.create_cpa_service(
        temp_db,
        name="cpa1",
        api_url="https://example.test/api/1",
        api_token="token-1",
    )
    cpa2 = crud.create_cpa_service(
        temp_db,
        name="cpa2",
        api_url="https://example.test/api/2",
        api_token="token-2",
    )
    cleanup_plan = crud.create_scheduled_plan(
        temp_db,
        name="cleanup",
        task_type="cpa_cleanup",
        cpa_service_id=cpa1.id,
        trigger_type="interval",
        interval_value=30,
        interval_unit="minutes",
        config={"max_cleanup_count": 20},
    )
    refill_plan = crud.create_scheduled_plan(
        temp_db,
        name="refill",
        task_type="cpa_refill",
        cpa_service_id=cpa2.id,
        trigger_type="interval",
        interval_value=30,
        interval_unit="minutes",
        config={"max_refill_count": 20},
    )

    run1 = crud.create_scheduled_run(temp_db, plan_id=cleanup_plan.id, trigger_source="manual")
    run2 = crud.create_scheduled_run(temp_db, plan_id=cleanup_plan.id, trigger_source="scheduled")
    run3 = crud.create_scheduled_run(temp_db, plan_id=refill_plan.id, trigger_source="manual")
    crud.finish_scheduled_run(temp_db, run_id=run2.id, status="success")
    crud.mark_scheduled_run_stop_requested(temp_db, run_id=run1.id, requested_by="tester", reason="stop now")

    by_plan = crud.get_scheduled_runs(temp_db, plan_id=cleanup_plan.id)
    by_task_type = crud.get_scheduled_runs(temp_db, task_type="cpa_refill")
    by_status = crud.get_scheduled_runs(temp_db, status="success")
    by_trigger = crud.get_scheduled_runs(temp_db, trigger_source="manual")
    by_cpa = crud.get_scheduled_runs(temp_db, cpa_service_id=cpa1.id)
    by_stop_requested = crud.get_scheduled_runs(temp_db, stop_requested=True)

    assert {r.id for r in by_plan} == {run1.id, run2.id}
    assert [r.id for r in by_task_type] == [run3.id]
    assert [r.id for r in by_status] == [run2.id]
    assert {r.id for r in by_trigger} == {run1.id, run3.id}
    assert {r.id for r in by_cpa} == {run1.id, run2.id}
    assert [r.id for r in by_stop_requested] == [run1.id]


def test_mark_scheduled_run_stop_requested_is_idempotent_and_running_only(temp_db):
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

    first = crud.mark_scheduled_run_stop_requested(
        temp_db,
        run_id=run.id,
        requested_by="alice",
        reason="first reason",
    )
    second = crud.mark_scheduled_run_stop_requested(
        temp_db,
        run_id=run.id,
        requested_by="bob",
        reason="second reason",
    )

    assert first is not None
    assert second is not None
    assert first.stop_requested_at is not None
    assert second.stop_requested_by == "alice"
    assert second.stop_reason == "first reason"

    finished_run = crud.create_scheduled_run(temp_db, plan_id=plan.id, trigger_source="manual")
    crud.finish_scheduled_run(temp_db, run_id=finished_run.id, status="success")

    not_marked = crud.mark_scheduled_run_stop_requested(
        temp_db,
        run_id=finished_run.id,
        requested_by="eve",
        reason="too late",
    )

    assert not_marked is None


def test_get_scheduled_run_log_chunk_returns_incremental_chunk(temp_db):
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
    assert crud.append_scheduled_run_log(temp_db, run.id, "line-1") is True
    assert crud.append_scheduled_run_log(temp_db, run.id, "line-2") is True

    chunk1 = crud.get_scheduled_run_log_chunk(temp_db, run.id, offset=0, limit=6)
    chunk2 = crud.get_scheduled_run_log_chunk(temp_db, run.id, offset=chunk1["next_offset"], limit=100)

    assert chunk1 is not None
    assert chunk1["content"] == "line-1"
    assert chunk1["offset"] == 0
    assert chunk1["has_more"] is True
    assert chunk1["log_version"] == 2
    assert chunk1["last_log_at"] is not None

    assert chunk2 is not None
    assert chunk2["content"] == "\nline-2"
    assert chunk2["has_more"] is False


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


def test_delete_cpa_service_rejects_when_referenced_by_scheduled_plan(temp_db):
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

    deleted = crud.delete_cpa_service(temp_db, service_id=service.id)

    assert deleted is False
    assert crud.get_cpa_service_by_id(temp_db, service.id) is not None
    assert crud.get_scheduled_plans(temp_db, cpa_service_id=service.id) == [plan]
