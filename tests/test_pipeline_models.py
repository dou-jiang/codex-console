import pytest

from src.database import crud
from src.database.models import Base, RegistrationTask
from src.database.session import DatabaseSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "pipeline-models.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_registration_task_supports_pipeline_metadata(temp_db):
    batch = crud.create_experiment_batch(
        temp_db,
        name="batch-metadata",
        mode="paired_compare",
        pipelines="current_pipeline,codexgen_pipeline",
        email_service_type="tempmail",
        target_count=1,
    )
    proxy = crud.create_proxy(
        temp_db,
        name="proxy-a",
        type="http",
        host="127.0.0.1",
        port=9001,
    )
    proxy_check_run = crud.create_proxy_check_run(
        temp_db,
        scope_type="batch",
        scope_id=str(batch.id),
        status="completed",
    )

    task = RegistrationTask(
        task_uuid="task-1",
        pipeline_key="current_pipeline",
        pair_key="pair-1",
        experiment_batch_id=batch.id,
        current_step_key="create_email",
        assigned_proxy_id=proxy.id,
        assigned_proxy_url="http://127.0.0.1:8080",
        proxy_check_run_id=proxy_check_run.id,
        total_duration_ms=4567,
        pipeline_status="running",
    )
    temp_db.add(task)
    temp_db.commit()
    assert task.pipeline_key == "current_pipeline"
    assert task.pair_key == "pair-1"
    assert task.experiment_batch_id == batch.id
    assert task.experiment_batch.id == batch.id
    assert task.current_step_key == "create_email"
    assert task.assigned_proxy_id == proxy.id
    assert task.assigned_proxy_url == "http://127.0.0.1:8080"
    assert task.proxy_check_run_id == proxy_check_run.id
    assert task.total_duration_ms == 4567
    assert task.pipeline_status == "running"


def test_create_pipeline_step_run_persists_row(temp_db):
    run = crud.create_pipeline_step_run(
        temp_db,
        task_uuid="task-1",
        pipeline_key="current_pipeline",
        step_key="get_proxy_ip",
        step_order=1,
        status="completed",
        duration_ms=123,
    )

    assert run.id is not None
    assert run.task_uuid == "task-1"
    assert run.pipeline_key == "current_pipeline"
    assert run.step_key == "get_proxy_ip"
    assert run.step_order == 1
    assert run.status == "completed"
    assert run.duration_ms == 123


def test_create_experiment_batch_persists_row(temp_db):
    batch = crud.create_experiment_batch(
        temp_db,
        name="batch-a",
        mode="paired_compare",
        pipelines="current_pipeline,codexgen_pipeline",
        email_service_type="tempmail",
        target_count=10,
    )

    assert batch.id is not None
    assert batch.name == "batch-a"
    assert batch.mode == "paired_compare"
    assert batch.status == "pending"
    assert batch.target_count == 10


def test_create_proxy_check_run_and_result_persists_rows(temp_db):
    proxy = crud.create_proxy(
        temp_db,
        name="proxy-result",
        type="http",
        host="127.0.0.1",
        port=9100,
    )
    run = crud.create_proxy_check_run(
        temp_db,
        scope_type="batch",
        scope_id="batch-1",
        status="completed",
        total_count=2,
        available_count=1,
    )

    result = crud.create_proxy_check_result(
        temp_db,
        proxy_check_run_id=run.id,
        proxy_id=proxy.id,
        proxy_url="http://127.0.0.1:8080",
        status="available",
        latency_ms=321,
    )

    assert run.id is not None
    assert result.id is not None
    assert result.proxy_check_run_id == run.id
    assert result.proxy_id == proxy.id
    assert result.status == "available"
    assert result.latency_ms == 321


def test_create_account_survival_check_persists_row(temp_db):
    batch = crud.create_experiment_batch(
        temp_db,
        name="batch-survival",
        mode="paired_compare",
        pipelines="current_pipeline,codexgen_pipeline",
        email_service_type="tempmail",
        target_count=1,
    )
    account = crud.create_account(
        temp_db,
        email="pipeline@example.com",
        email_service="tempmail",
    )

    check = crud.create_account_survival_check(
        temp_db,
        account_id=account.id,
        task_uuid="task-1",
        pipeline_key="current_pipeline",
        experiment_batch_id=batch.id,
        check_source="auto",
        check_stage="24h",
        result_level="healthy",
    )

    assert check.id is not None
    assert check.account_id == account.id
    assert check.pipeline_key == "current_pipeline"
    assert check.experiment_batch_id == batch.id
    assert check.experiment_batch.id == batch.id
    assert check.check_source == "auto"
    assert check.check_stage == "24h"
    assert check.result_level == "healthy"
