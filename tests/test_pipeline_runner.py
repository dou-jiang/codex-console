import pytest

from src.core.pipeline.context import PipelineContext
from src.core.pipeline.definitions import PipelineDefinition, StepDefinition
from src.core.pipeline.runner import PipelineRunner
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


def test_runner_records_step_durations_in_order(fake_db):
    calls = []

    def step_one(ctx):
        calls.append("step_one")
        return {"email": "a@example.com"}

    def step_two(ctx):
        calls.append(ctx.email)
        return {"done": True}

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

    rows = (
        fake_db.query(PipelineStepRun)
        .filter(PipelineStepRun.task_uuid == "task-1")
        .order_by(PipelineStepRun.step_order.asc())
        .all()
    )

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


def test_register_and_get_pipeline_definition():
    from src.core.pipeline.registry import get_pipeline, register_pipeline

    pipeline = PipelineDefinition(pipeline_key="lookup-demo", steps=[])
    register_pipeline(pipeline)

    assert get_pipeline("lookup-demo") is pipeline
