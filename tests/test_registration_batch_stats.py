from datetime import datetime, timedelta

import pytest

from src.core.registration_batch_stats import STEP_STAGE_MAP, build_batch_stats_compare, finalize_batch_statistics
from src.database import crud
from src.database.models import Base, RegistrationBatchStat
from src.database.session import DatabaseSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "registration-batch-stats.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _seed_task_and_steps(
    db,
    *,
    task_uuid: str,
    status: str,
    total_duration_ms: int | None,
    step_rows: list[dict],
):
    task = crud.create_registration_task(
        db,
        task_uuid=task_uuid,
        pipeline_key="current_pipeline",
    )
    crud.update_registration_task(
        db,
        task_uuid,
        status=status,
        total_duration_ms=total_duration_ms,
    )

    for row in step_rows:
        crud.create_pipeline_step_run(
            db,
            task_uuid=task.task_uuid,
            pipeline_key="current_pipeline",
            step_key=row["step_key"],
            step_order=row["step_order"],
            status=row.get("status", "completed"),
            duration_ms=row.get("duration_ms"),
        )


def test_step_stage_map_contains_approved_stage_keys():
    assert STEP_STAGE_MAP["create_email"] == "signup_prepare"
    assert STEP_STAGE_MAP["validate_signup_otp"] == "signup_otp"
    assert STEP_STAGE_MAP["create_account"] == "create_account"
    assert STEP_STAGE_MAP["submit_login_password"] == "login_prepare"
    assert STEP_STAGE_MAP["validate_login_otp"] == "login_otp"
    assert STEP_STAGE_MAP["exchange_oauth_token"] == "token_exchange"


def test_finalize_batch_statistics_builds_snapshot_for_completed_batch(temp_db):
    start_at = datetime(2026, 3, 24, 10, 0, 0)
    end_at = start_at + timedelta(minutes=2)

    _seed_task_and_steps(
        temp_db,
        task_uuid="task-1",
        status="completed",
        total_duration_ms=1500,
        step_rows=[
            {"step_key": "create_email", "step_order": 1, "duration_ms": 120},
            {"step_key": "send_signup_otp", "step_order": 2, "duration_ms": 210},
            {"step_key": "exchange_oauth_token", "step_order": 9, "duration_ms": 600},
        ],
    )
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-2",
        status="failed",
        total_duration_ms=900,
        step_rows=[
            {"step_key": "create_email", "step_order": 1, "duration_ms": 160},
            {
                "step_key": "send_signup_otp",
                "step_order": 2,
                "status": "failed",
                "duration_ms": None,
            },
        ],
    )

    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-001",
            "status": "completed",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 2,
            "task_uuids": ["task-1", "task-2"],
            "started_at": start_at,
            "completed_at": end_at,
            "email_service_type": "tempmail",
            "email_service_id": None,
            "proxy_strategy_snapshot": {"mode": "none"},
            "config_snapshot": {"concurrency": 2},
        },
    )

    assert stat.batch_id == "batch-001"
    assert stat.status == "completed"
    assert stat.finished_count == 2
    assert stat.success_count == 1
    assert stat.failed_count == 1
    assert stat.total_duration_ms == 2400
    assert stat.avg_duration_ms == 1200.0

    step_by_key = {row.step_key: row for row in stat.step_stats}
    assert step_by_key["create_email"].sample_count == 2
    assert step_by_key["create_email"].success_count == 2
    assert step_by_key["create_email"].avg_duration_ms == 140.0

    stage_by_key = {row.stage_key: row for row in stat.stage_stats}
    assert stage_by_key["signup_prepare"].sample_count == 2
    assert stage_by_key["signup_prepare"].avg_duration_ms == 140.0
    assert stage_by_key["signup_otp"].sample_count == 2
    assert stage_by_key["signup_otp"].avg_duration_ms == 210.0
    assert stage_by_key["token_exchange"].sample_count == 1


def test_finalize_batch_statistics_keeps_cancelled_batch_snapshot(temp_db):
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-c1",
        status="cancelled",
        total_duration_ms=None,
        step_rows=[
            {"step_key": "create_email", "step_order": 1, "duration_ms": 111},
        ],
    )

    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-cancelled",
            "status": "cancelled",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 3,
            "task_uuids": ["task-c1"],
        },
    )

    assert stat.status == "cancelled"
    assert stat.finished_count == 1
    assert stat.success_count == 0
    assert stat.failed_count == 0


def test_finalize_batch_statistics_stage_stats_follow_approved_order(temp_db):
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-order-1",
        status="completed",
        total_duration_ms=2000,
        step_rows=[
            {"step_key": "validate_login_otp", "step_order": 11, "duration_ms": 80},
            {"step_key": "create_account", "step_order": 7, "duration_ms": 120},
            {"step_key": "create_email", "step_order": 1, "duration_ms": 100},
            {"step_key": "send_signup_otp", "step_order": 4, "duration_ms": 90},
            {"step_key": "exchange_oauth_token", "step_order": 14, "duration_ms": 60},
            {"step_key": "submit_login_password", "step_order": 10, "duration_ms": 110},
        ],
    )

    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-stage-order",
            "status": "completed",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 1,
            "task_uuids": ["task-order-1"],
        },
    )

    assert [item.stage_key for item in stat.stage_stats] == [
        "signup_prepare",
        "signup_otp",
        "create_account",
        "login_prepare",
        "login_otp",
        "token_exchange",
    ]


def test_finalize_batch_statistics_is_idempotent_by_batch_id(temp_db):
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-idem-1",
        status="completed",
        total_duration_ms=1000,
        step_rows=[
            {"step_key": "create_email", "step_order": 1, "duration_ms": 100},
        ],
    )

    context = {
        "batch_id": "batch-idempotent",
        "status": "completed",
        "mode": "pipeline",
        "pipeline_key": "current_pipeline",
        "target_count": 1,
        "task_uuids": ["task-idem-1"],
    }

    first = finalize_batch_statistics(temp_db, batch_context=context)

    crud.update_registration_task(
        temp_db,
        "task-idem-1",
        status="failed",
        total_duration_ms=3333,
    )

    second = finalize_batch_statistics(temp_db, batch_context=context)

    assert first.id == second.id
    assert second.success_count == 1
    assert (
        temp_db.query(RegistrationBatchStat)
        .filter(RegistrationBatchStat.batch_id == "batch-idempotent")
        .count()
    ) == 1


def test_build_batch_stats_compare_aligns_missing_steps_and_stages(temp_db):
    left = crud.create_registration_batch_stat(
        temp_db,
        batch_id="left-batch",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=2,
        finished_count=2,
        success_count=2,
        failed_count=0,
        total_duration_ms=2000,
        avg_duration_ms=1000,
    )
    right = crud.create_registration_batch_stat(
        temp_db,
        batch_id="right-batch",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=2,
        finished_count=2,
        success_count=1,
        failed_count=1,
        total_duration_ms=3000,
        avg_duration_ms=1500,
    )

    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=left.id,
        step_key="create_email",
        step_order=1,
        sample_count=2,
        success_count=2,
        avg_duration_ms=100,
        p50_duration_ms=100,
        p90_duration_ms=100,
    )
    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=right.id,
        step_key="send_signup_otp",
        step_order=2,
        sample_count=2,
        success_count=1,
        avg_duration_ms=400,
        p50_duration_ms=400,
        p90_duration_ms=400,
    )

    crud.create_registration_batch_stage_stat(
        temp_db,
        batch_stat_id=left.id,
        stage_key="signup_prepare",
        sample_count=2,
        avg_duration_ms=200,
        p50_duration_ms=200,
        p90_duration_ms=200,
    )
    crud.create_registration_batch_stage_stat(
        temp_db,
        batch_stat_id=right.id,
        stage_key="signup_otp",
        sample_count=2,
        avg_duration_ms=500,
        p50_duration_ms=500,
        p90_duration_ms=500,
    )

    compare = build_batch_stats_compare(left, right)

    step_diffs = {row["step_key"]: row for row in compare["step_diffs"]}
    assert step_diffs["create_email"]["left"]["avg_duration_ms"] == 100
    assert step_diffs["create_email"]["right"]["avg_duration_ms"] is None
    assert step_diffs["send_signup_otp"]["left"]["avg_duration_ms"] is None
    assert step_diffs["send_signup_otp"]["right"]["avg_duration_ms"] == 400

    stage_diffs = {row["stage_key"]: row for row in compare["stage_diffs"]}
    assert stage_diffs["signup_prepare"]["left"]["avg_duration_ms"] == 200
    assert stage_diffs["signup_prepare"]["right"]["avg_duration_ms"] is None
    assert stage_diffs["signup_otp"]["left"]["avg_duration_ms"] is None
    assert stage_diffs["signup_otp"]["right"]["avg_duration_ms"] == 500

    assert compare["summary_diff"]["success_count"] == -1
    assert compare["summary_diff"]["avg_duration_ms"] == 500.0


def test_build_batch_stats_compare_stage_diffs_follow_approved_order(temp_db):
    left = crud.create_registration_batch_stat(
        temp_db,
        batch_id="left-ordered",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=1,
        finished_count=1,
        success_count=1,
        failed_count=0,
        total_duration_ms=1000,
        avg_duration_ms=1000,
    )
    right = crud.create_registration_batch_stat(
        temp_db,
        batch_id="right-ordered",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=1,
        finished_count=1,
        success_count=1,
        failed_count=0,
        total_duration_ms=1000,
        avg_duration_ms=1000,
    )

    crud.create_registration_batch_stage_stat(
        temp_db,
        batch_stat_id=left.id,
        stage_key="token_exchange",
        sample_count=1,
        avg_duration_ms=30,
        p50_duration_ms=30,
        p90_duration_ms=30,
    )
    crud.create_registration_batch_stage_stat(
        temp_db,
        batch_stat_id=left.id,
        stage_key="signup_prepare",
        sample_count=1,
        avg_duration_ms=10,
        p50_duration_ms=10,
        p90_duration_ms=10,
    )
    crud.create_registration_batch_stage_stat(
        temp_db,
        batch_stat_id=right.id,
        stage_key="login_prepare",
        sample_count=1,
        avg_duration_ms=20,
        p50_duration_ms=20,
        p90_duration_ms=20,
    )

    compare = build_batch_stats_compare(left, right)

    assert [item["stage_key"] for item in compare["stage_diffs"]] == [
        "signup_prepare",
        "login_prepare",
        "token_exchange",
    ]
