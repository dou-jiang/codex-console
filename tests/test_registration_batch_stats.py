from datetime import datetime, timedelta

import pytest

from src.core.registration_batch_stats import (
    EXCLUDED_STAGE_STEP_KEYS,
    STEP_STAGE_MAP,
    build_batch_stats_compare,
    finalize_batch_statistics,
)
from src.core.pipeline.steps.codexgen import build_codexgen_pipeline_definition
from src.core.pipeline.steps.current import build_current_pipeline_definition
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


def _live_pipeline_step_keys() -> set[str]:
    current_keys = {step.step_key for step in build_current_pipeline_definition().steps}
    codexgen_keys = {step.step_key for step in build_codexgen_pipeline_definition().steps}
    return current_keys | codexgen_keys


def test_step_stage_map_contains_approved_stage_keys():
    assert STEP_STAGE_MAP["init_auth_session"] == "signup_prepare"
    assert STEP_STAGE_MAP["send_signup_otp"] == "signup_otp"
    assert STEP_STAGE_MAP["create_account_profile"] == "create_account"
    assert STEP_STAGE_MAP["prepare_token_acquisition"] == "login_prepare"
    assert STEP_STAGE_MAP["validate_login_otp"] == "login_otp"
    assert STEP_STAGE_MAP["resolve_consent_and_workspace"] == "token_exchange"


def test_step_stage_map_includes_real_pipeline_step_keys():
    assert STEP_STAGE_MAP["init_auth_session"] == "signup_prepare"
    assert STEP_STAGE_MAP["prepare_authorize_flow"] == "signup_prepare"
    assert STEP_STAGE_MAP["submit_signup_email"] == "signup_prepare"
    assert STEP_STAGE_MAP["register_password"] == "signup_prepare"
    assert STEP_STAGE_MAP["create_account_profile"] == "create_account"
    assert STEP_STAGE_MAP["prepare_token_acquisition"] == "login_prepare"
    assert STEP_STAGE_MAP["resolve_consent_and_workspace"] == "token_exchange"


def test_stage_mapping_explicitly_excludes_non_stage_live_steps():
    assert EXCLUDED_STAGE_STEP_KEYS == {
        "persist_account",
        "schedule_survival_checks",
    }
    assert "persist_account" not in STEP_STAGE_MAP
    assert "schedule_survival_checks" not in STEP_STAGE_MAP


def test_live_pipeline_step_keys_are_fully_and_explicitly_accounted_for():
    live_keys = _live_pipeline_step_keys()
    mapped_keys = set(STEP_STAGE_MAP)
    excluded_keys = set(EXCLUDED_STAGE_STEP_KEYS)

    assert sorted(live_keys - (mapped_keys | excluded_keys)) == []
    assert sorted((mapped_keys | excluded_keys) - live_keys) == []


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


def test_finalize_batch_statistics_aggregates_real_pipeline_stage_mapping(temp_db):
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-real-steps-1",
        status="completed",
        total_duration_ms=1500,
        step_rows=[
            {"step_key": "init_auth_session", "step_order": 1, "duration_ms": 100},
            {"step_key": "prepare_authorize_flow", "step_order": 2, "duration_ms": 120},
            {"step_key": "submit_signup_email", "step_order": 3, "duration_ms": 130},
            {"step_key": "register_password", "step_order": 4, "duration_ms": 150},
            {"step_key": "create_account_profile", "step_order": 5, "duration_ms": 200},
            {"step_key": "prepare_token_acquisition", "step_order": 6, "duration_ms": 220},
            {"step_key": "resolve_consent_and_workspace", "step_order": 7, "duration_ms": 300},
        ],
    )

    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-real-steps",
            "status": "completed",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 1,
            "task_uuids": ["task-real-steps-1"],
        },
    )

    stage_by_key = {row.stage_key: row for row in stat.stage_stats}
    assert stage_by_key["signup_prepare"].sample_count == 1
    assert stage_by_key["signup_prepare"].avg_duration_ms == 500.0
    assert stage_by_key["create_account"].avg_duration_ms == 200.0
    assert stage_by_key["login_prepare"].avg_duration_ms == 220.0
    assert stage_by_key["token_exchange"].avg_duration_ms == 300.0


def test_finalize_batch_statistics_explicitly_ignores_excluded_live_steps_for_stage_stats(temp_db):
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-excluded-steps-1",
        status="completed",
        total_duration_ms=999,
        step_rows=[
            {"step_key": "init_auth_session", "step_order": 1, "duration_ms": 100},
            {"step_key": "persist_account", "step_order": 99, "duration_ms": 200},
            {"step_key": "schedule_survival_checks", "step_order": 100, "duration_ms": 300},
        ],
    )

    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-excluded-live-steps",
            "status": "completed",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 1,
            "task_uuids": ["task-excluded-steps-1"],
        },
    )

    stage_by_key = {row.stage_key: row for row in stat.stage_stats}
    assert set(stage_by_key) == {"signup_prepare"}
    assert stage_by_key["signup_prepare"].avg_duration_ms == 100.0

    step_by_key = {row.step_key: row for row in stat.step_stats}
    assert "persist_account" in step_by_key
    assert "schedule_survival_checks" in step_by_key


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


def test_finalize_batch_statistics_stage_aggregates_per_task_not_per_step_row(temp_db):
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-stage-agg-1",
        status="completed",
        total_duration_ms=500,
        step_rows=[
            {"step_key": "create_email", "step_order": 1, "duration_ms": 100},
            {"step_key": "init_signup_session", "step_order": 2, "duration_ms": 200},
        ],
    )
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-stage-agg-2",
        status="completed",
        total_duration_ms=900,
        step_rows=[
            {"step_key": "create_email", "step_order": 1, "duration_ms": 400},
        ],
    )

    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-stage-agg",
            "status": "completed",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 2,
            "task_uuids": ["task-stage-agg-1", "task-stage-agg-2"],
        },
    )

    stage_by_key = {row.stage_key: row for row in stat.stage_stats}
    signup_prepare = stage_by_key["signup_prepare"]
    assert signup_prepare.sample_count == 2
    assert signup_prepare.avg_duration_ms == 350.0
    assert signup_prepare.p50_duration_ms == 350
    assert signup_prepare.p90_duration_ms == 390


def test_finalize_batch_statistics_returns_stage_stats_in_approved_order_after_refresh(temp_db, monkeypatch):
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-stage-refresh-1",
        status="completed",
        total_duration_ms=1000,
        step_rows=[
            {"step_key": "create_email", "step_order": 1, "duration_ms": 100},
            {"step_key": "exchange_oauth_token", "step_order": 14, "duration_ms": 200},
        ],
    )

    def fake_build_stage_stats(_step_rows):
        return [
            {
                "stage_key": "token_exchange",
                "sample_count": 1,
                "avg_duration_ms": 200.0,
                "p50_duration_ms": 200,
                "p90_duration_ms": 200,
            },
            {
                "stage_key": "signup_prepare",
                "sample_count": 1,
                "avg_duration_ms": 100.0,
                "p50_duration_ms": 100,
                "p90_duration_ms": 100,
            },
        ]

    monkeypatch.setattr("src.core.registration_batch_stats._build_stage_stats", fake_build_stage_stats)

    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-stage-refresh-order",
            "status": "completed",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 1,
            "task_uuids": ["task-stage-refresh-1"],
        },
    )

    assert [item.stage_key for item in stat.stage_stats] == [
        "signup_prepare",
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


def test_finalize_batch_statistics_returns_existing_when_insert_hits_unique_conflict(temp_db, monkeypatch):
    _seed_task_and_steps(
        temp_db,
        task_uuid="task-race-1",
        status="completed",
        total_duration_ms=1000,
        step_rows=[
            {"step_key": "create_email", "step_order": 1, "duration_ms": 120},
        ],
    )

    existing = crud.create_registration_batch_stat(
        temp_db,
        batch_id="batch-race",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=1,
        finished_count=1,
        success_count=1,
        failed_count=0,
        total_duration_ms=1000,
        avg_duration_ms=1000.0,
    )

    real_get = crud.get_registration_batch_stat_by_batch_id
    call_count = {"value": 0}

    def fake_get(db, batch_id):
        call_count["value"] += 1
        if call_count["value"] == 1:
            return None
        return real_get(db, batch_id)

    monkeypatch.setattr("src.core.registration_batch_stats.crud.get_registration_batch_stat_by_batch_id", fake_get)

    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-race",
            "status": "completed",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 1,
            "task_uuids": ["task-race-1"],
        },
    )

    assert stat.id == existing.id
    assert (
        temp_db.query(RegistrationBatchStat)
        .filter(RegistrationBatchStat.batch_id == "batch-race")
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


def test_build_batch_stats_compare_step_ordering_is_stable_when_left_right_swap(temp_db):
    left = crud.create_registration_batch_stat(
        temp_db,
        batch_id="left-step-order",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=1,
        finished_count=1,
        success_count=1,
        failed_count=0,
    )
    right = crud.create_registration_batch_stat(
        temp_db,
        batch_id="right-step-order",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=1,
        finished_count=1,
        success_count=1,
        failed_count=0,
    )

    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=left.id,
        step_key="shared_step",
        step_order=10,
        sample_count=1,
        success_count=1,
        avg_duration_ms=100,
        p50_duration_ms=100,
        p90_duration_ms=100,
    )
    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=left.id,
        step_key="other_step",
        step_order=3,
        sample_count=1,
        success_count=1,
        avg_duration_ms=200,
        p50_duration_ms=200,
        p90_duration_ms=200,
    )
    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=right.id,
        step_key="shared_step",
        step_order=1,
        sample_count=1,
        success_count=1,
        avg_duration_ms=110,
        p50_duration_ms=110,
        p90_duration_ms=110,
    )
    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=right.id,
        step_key="other_step",
        step_order=3,
        sample_count=1,
        success_count=1,
        avg_duration_ms=210,
        p50_duration_ms=210,
        p90_duration_ms=210,
    )

    compare_lr = build_batch_stats_compare(left, right)
    compare_rl = build_batch_stats_compare(right, left)

    assert [item["step_key"] for item in compare_lr["step_diffs"]] == [
        "shared_step",
        "other_step",
    ]
    assert [item["step_key"] for item in compare_rl["step_diffs"]] == [
        "shared_step",
        "other_step",
    ]
