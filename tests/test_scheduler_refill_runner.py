from __future__ import annotations

import pytest

from src.core.registration_job import RegistrationJobResult
from src.database import crud
from src.database.models import Base, ScheduledRun
from src.database.session import DatabaseSessionManager
from src.database import session as session_module
from src.scheduler import engine as engine_module
from src.scheduler.runners import refill as refill_runner
from src.scheduler.runners.refill import run_refill_plan


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "scheduler-refill.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()
    monkeypatch.setattr(session_module, "_db_manager", manager)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _create_refill_plan_and_run(
    temp_db,
    *,
    target_valid_count: int = 3,
    max_refill_count: int = 5,
    max_consecutive_failures: int = 3,
):
    service = crud.create_cpa_service(
        temp_db,
        name="refill-cpa",
        api_url="https://example.test/v0/management",
        api_token="token",
    )
    plan = crud.create_scheduled_plan(
        temp_db,
        name="refill",
        task_type="cpa_refill",
        cpa_service_id=service.id,
        trigger_type="interval",
        interval_value=1,
        interval_unit="hours",
        config={
            "target_valid_count": target_valid_count,
            "max_refill_count": max_refill_count,
            "max_consecutive_failures": max_consecutive_failures,
            "email_service_type": "tempmail",
        },
    )
    run = crud.create_scheduled_run(temp_db, plan_id=plan.id, trigger_source="manual")
    return service, plan, run


def test_refill_runner_requires_cpa_upload_for_success(temp_db, monkeypatch):
    _, plan, run = _create_refill_plan_and_run(temp_db, target_valid_count=3, max_refill_count=2)

    monkeypatch.setattr(refill_runner, "count_valid_accounts", lambda *a, **k: 2)
    monkeypatch.setattr(
        refill_runner,
        "run_registration_job",
        lambda **_: RegistrationJobResult(success=True, account_id=11),
    )
    monkeypatch.setattr(refill_runner, "upload_account_to_bound_cpa", lambda **_: (True, "ok"))

    summary = run_refill_plan(plan_id=plan.id, run_id=run.id)

    assert summary["uploaded_success"] == 1


def test_refill_runner_auto_disables_plan_after_consecutive_failures(temp_db, monkeypatch):
    _, plan, run = _create_refill_plan_and_run(
        temp_db,
        target_valid_count=2,
        max_refill_count=5,
        max_consecutive_failures=2,
    )

    monkeypatch.setattr(refill_runner, "count_valid_accounts", lambda *a, **k: 0)
    monkeypatch.setattr(
        refill_runner,
        "run_registration_job",
        lambda **_: RegistrationJobResult(success=False, error_message="boom"),
    )

    summary = run_refill_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    refreshed_plan = crud.get_scheduled_plan_by_id(temp_db, plan.id)
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert refreshed_plan is not None
    assert refreshed_plan.enabled is False
    assert refreshed_plan.auto_disabled_reason == "consecutive_failures_reached"
    assert persisted_run is not None
    assert persisted_run.status == "failed"
    assert persisted_run.summary is not None
    assert persisted_run.summary["auto_disabled"] is True
    assert summary["auto_disabled"] is True


def test_refill_runner_marks_run_failed_when_registration_succeeds_but_cpa_upload_fails(temp_db, monkeypatch):
    _, plan, run = _create_refill_plan_and_run(
        temp_db,
        target_valid_count=1,
        max_refill_count=1,
        max_consecutive_failures=1,
    )

    monkeypatch.setattr(refill_runner, "count_valid_accounts", lambda *a, **k: 0)
    monkeypatch.setattr(
        refill_runner,
        "run_registration_job",
        lambda **_: RegistrationJobResult(success=True, account_id=11),
    )
    monkeypatch.setattr(refill_runner, "upload_account_to_bound_cpa", lambda **_: (False, "upstream down"))

    summary = run_refill_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert persisted_run.status == "failed"
    assert summary["registered_success"] == 1
    assert summary["uploaded_failed"] == 1
    assert summary["uploaded_success"] == 0
    assert summary["auto_disabled"] is True


def test_refill_runner_logs_target_resolution_and_progress_summaries(temp_db, monkeypatch):
    _, plan, run = _create_refill_plan_and_run(
        temp_db,
        target_valid_count=45,
        max_refill_count=45,
        max_consecutive_failures=50,
    )

    account_counter = {"value": 0}

    monkeypatch.setattr(refill_runner, "count_valid_accounts", lambda *a, **k: 0)

    def _run_registration_job(**kwargs):
        account_counter["value"] += 1
        return RegistrationJobResult(success=True, account_id=account_counter["value"])

    monkeypatch.setattr(refill_runner, "run_registration_job", _run_registration_job)
    monkeypatch.setattr(refill_runner, "upload_account_to_bound_cpa", lambda **_: (True, "ok"))

    summary = run_refill_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert summary["uploaded_success"] == 45
    assert "refill target resolved" in (persisted_run.logs or "")
    assert "refill progress" in (persisted_run.logs or "")


def test_refill_runner_marks_run_cancelled_and_logs_user_stop_when_stop_requested_mid_loop(temp_db, monkeypatch):
    _, plan, run = _create_refill_plan_and_run(
        temp_db,
        target_valid_count=3,
        max_refill_count=3,
        max_consecutive_failures=5,
    )

    account_counter = {"value": 0}
    stop_requested = {"done": False}

    monkeypatch.setattr(refill_runner, "count_valid_accounts", lambda *a, **k: 0)

    def _run_registration_job(**kwargs):
        account_counter["value"] += 1
        return RegistrationJobResult(success=True, account_id=account_counter["value"])

    def _upload_account_to_bound_cpa(**kwargs):
        if not stop_requested["done"]:
            stop_requested["done"] = True
            assert engine_module.request_run_stop(run.id, requested_by="tester", reason="user_requested") is True
        return True, "ok"

    monkeypatch.setattr(refill_runner, "run_registration_job", _run_registration_job)
    monkeypatch.setattr(refill_runner, "upload_account_to_bound_cpa", _upload_account_to_bound_cpa)

    summary = run_refill_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert persisted_run.status == "cancelled"
    assert persisted_run.error_message == "user requested stop"
    assert summary["uploaded_success"] == 1
    assert "收到停止请求" in (persisted_run.logs or "")
    assert "任务已按请求停止" in (persisted_run.logs or "")
