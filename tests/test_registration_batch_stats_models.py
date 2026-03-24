import pytest

from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "registration-batch-stats-models.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_create_registration_batch_stat_persists_children(temp_db):
    batch = crud.create_registration_batch_stat(
        temp_db,
        batch_id="batch-001",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=5,
        finished_count=5,
        success_count=4,
        failed_count=1,
        total_duration_ms=50000,
        avg_duration_ms=12500.0,
    )

    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=batch.id,
        step_key="create_email",
        step_order=1,
        sample_count=4,
        success_count=4,
        avg_duration_ms=120.0,
        p50_duration_ms=110,
        p90_duration_ms=150,
    )
    crud.create_registration_batch_stage_stat(
        temp_db,
        batch_stat_id=batch.id,
        stage_key="signup_prepare",
        sample_count=4,
        avg_duration_ms=450.0,
        p50_duration_ms=420,
        p90_duration_ms=520,
    )

    assert batch.id is not None
    assert batch.step_stats[0].step_key == "create_email"
    assert batch.stage_stats[0].stage_key == "signup_prepare"
