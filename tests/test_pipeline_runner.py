import pytest

from src.core.pipeline.context import PipelineContext
from src.core.pipeline.definitions import PipelineDefinition, StepDefinition
from src.core.pipeline.runner import PipelineRunner
from src.database import crud
from src.database.models import Base, PipelineStepRun
from src.database.session import DatabaseSessionManager


def _build_db(url: str):
    manager = DatabaseSessionManager(url)
    Base.metadata.create_all(bind=manager.engine)
    return manager


@pytest.fixture
def fake_db(tmp_path):
    manager = _build_db(f"sqlite:///{tmp_path / 'pipeline-runner.db'}")
    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def clean_pipeline_registry():
    from src.core.pipeline.registry import PIPELINE_REGISTRY

    PIPELINE_REGISTRY.clear()
    try:
        yield
    finally:
        PIPELINE_REGISTRY.clear()


def _get_step_rows(fake_db, task_uuid: str) -> list[PipelineStepRun]:
    return (
        fake_db.query(PipelineStepRun)
        .filter(PipelineStepRun.task_uuid == task_uuid)
        .order_by(PipelineStepRun.step_order.asc())
        .all()
    )


def test_runner_records_step_durations_in_order(fake_db):
    calls = []

    def step_one(ctx):
        calls.append("step_one")
        return {"email": "a@example.com"}

    def step_two(ctx):
        calls.append(ctx.email)
        return {"metadata": {"done": True}}

    pipeline = PipelineDefinition(
        pipeline_key="demo",
        steps=[
            StepDefinition("create_email", step_one),
            StepDefinition("finish", step_two),
        ],
    )
    ctx = PipelineContext(task_uuid="task-1", pipeline_key="demo")

    PipelineRunner(fake_db).run(pipeline, ctx)

    assert calls == ["step_one", "a@example.com"]
    rows = _get_step_rows(fake_db, "task-1")

    assert [row.step_key for row in rows] == ["create_email", "finish"]
    assert [row.step_order for row in rows] == [1, 2]
    assert all(row.status == "completed" for row in rows)
    assert all(row.duration_ms is not None for row in rows)


def test_runner_applies_payload_into_context(fake_db):
    def set_password(ctx):
        return {"password": "secret"}

    pipeline = PipelineDefinition(
        pipeline_key="demo",
        steps=[StepDefinition("set_password", set_password)],
    )

    ctx = PipelineContext(task_uuid="task-2", pipeline_key="demo")
    result = PipelineRunner(fake_db).run(pipeline, ctx)

    assert result is ctx
    assert result.password == "secret"


def test_runner_updates_registration_task_metadata(fake_db):
    task_uuid = "task-metadata"
    crud.create_registration_task(fake_db, task_uuid=task_uuid)

    pipeline = PipelineDefinition(
        pipeline_key="demo",
        steps=[
            StepDefinition("create_email", lambda _: {"email": "a@example.com"}),
            StepDefinition("finish", lambda _: {"password": "secret"}),
        ],
    )
    ctx = PipelineContext(task_uuid=task_uuid, pipeline_key="demo")

    PipelineRunner(fake_db).run(pipeline, ctx)

    task = crud.get_registration_task_by_uuid(fake_db, task_uuid)
    assert task is not None
    assert task.pipeline_key == "demo"
    assert task.pipeline_status == "completed"
    assert task.current_step_key == "finish"
    assert task.started_at is not None
    assert task.completed_at is not None
    assert task.total_duration_ms is not None


def test_runner_marks_step_and_task_failed_when_step_raises(fake_db):
    task_uuid = "task-fail"
    crud.create_registration_task(fake_db, task_uuid=task_uuid)

    def step_fail(_):
        raise RuntimeError("boom")

    pipeline = PipelineDefinition(
        pipeline_key="demo",
        steps=[
            StepDefinition("create_email", lambda _: {"email": "a@example.com"}),
            StepDefinition("finish", step_fail),
        ],
    )
    ctx = PipelineContext(task_uuid=task_uuid, pipeline_key="demo")

    with pytest.raises(RuntimeError, match="boom"):
        PipelineRunner(fake_db).run(pipeline, ctx)

    rows = _get_step_rows(fake_db, task_uuid)
    assert [row.step_key for row in rows] == ["create_email", "finish"]
    assert rows[0].status == "completed"
    assert rows[1].status == "failed"
    assert rows[1].error_message == "boom"
    assert rows[1].duration_ms is not None

    task = crud.get_registration_task_by_uuid(fake_db, task_uuid)
    assert task is not None
    assert task.pipeline_status == "failed"
    assert task.current_step_key == "finish"
    assert task.error_message == "boom"
    assert task.completed_at is not None


def test_runner_rejects_unknown_payload_keys(fake_db):
    task_uuid = "task-unknown"
    crud.create_registration_task(fake_db, task_uuid=task_uuid)

    pipeline = PipelineDefinition(
        pipeline_key="demo",
        steps=[StepDefinition("bad", lambda _: {"unknown_key": "value"})],
    )
    ctx = PipelineContext(task_uuid=task_uuid, pipeline_key="demo")

    with pytest.raises(ValueError, match="unknown_key"):
        PipelineRunner(fake_db).run(pipeline, ctx)

    assert not hasattr(ctx, "unknown_key")

    rows = _get_step_rows(fake_db, task_uuid)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_message is not None

    task = crud.get_registration_task_by_uuid(fake_db, task_uuid)
    assert task is not None
    assert task.pipeline_status == "failed"


def test_register_and_get_pipeline_definition():
    from src.core.pipeline.registry import get_pipeline, register_pipeline

    pipeline = PipelineDefinition(pipeline_key="lookup-demo", steps=[])
    register_pipeline(pipeline)

    assert get_pipeline("lookup-demo") is pipeline


def test_register_pipeline_rejects_duplicate_pipeline_key():
    from src.core.pipeline.registry import register_pipeline

    register_pipeline(PipelineDefinition(pipeline_key="dup", steps=[]))
    with pytest.raises(ValueError, match="dup"):
        register_pipeline(PipelineDefinition(pipeline_key="dup", steps=[]))
