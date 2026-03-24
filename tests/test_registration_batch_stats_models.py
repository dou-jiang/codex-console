import pytest

from src.database import crud
from src.database.models import Base, RegistrationBatchStat
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
        step_key="send_signup_otp",
        step_order=2,
        sample_count=4,
        success_count=4,
        avg_duration_ms=220.0,
        p50_duration_ms=210,
        p90_duration_ms=260,
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
    assert [row.step_order for row in batch.step_stats] == [1, 2]
    assert [row.step_key for row in batch.step_stats] == ["create_email", "send_signup_otp"]
    assert batch.stage_stats[0].stage_key == "signup_prepare"


def test_create_registration_batch_stats_supports_non_committed_atomic_flow(temp_db):
    batch = crud.create_registration_batch_stat(
        temp_db,
        batch_id="batch-atomic-001",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        commit=False,
    )
    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=batch.id,
        step_key="create_email",
        step_order=1,
        sample_count=1,
        success_count=1,
        avg_duration_ms=100.0,
        p50_duration_ms=100,
        p90_duration_ms=100,
        commit=False,
    )
    crud.create_registration_batch_stage_stat(
        temp_db,
        batch_stat_id=batch.id,
        stage_key="signup_prepare",
        sample_count=1,
        avg_duration_ms=200.0,
        p50_duration_ms=200,
        p90_duration_ms=200,
        commit=False,
    )

    temp_db.rollback()

    assert (
        temp_db.query(RegistrationBatchStat)
        .filter(RegistrationBatchStat.batch_id == "batch-atomic-001")
        .count()
    ) == 0
