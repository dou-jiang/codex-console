from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.database import crud
from src.database.models import Base, ScheduledRun
from src.database.session import DatabaseSessionManager
from src.database import session as session_module
from src.scheduler import engine as engine_module
from src.scheduler.runners import refresh as refresh_runner
from src.scheduler.runners.refresh import run_refresh_plan


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "scheduler-refresh.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()
    monkeypatch.setattr(session_module, "_db_manager", manager)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _create_refresh_plan_and_run(temp_db, *, refresh_after_days: int = 7, max_refresh_count: int = 10):
    service = crud.create_cpa_service(
        temp_db,
        name="refresh-cpa",
        api_url="https://example.test/v0/management",
        api_token="token",
    )
    plan = crud.create_scheduled_plan(
        temp_db,
        name="refresh",
        task_type="account_refresh",
        cpa_service_id=service.id,
        trigger_type="interval",
        interval_value=1,
        interval_unit="hours",
        config={
            "refresh_after_days": refresh_after_days,
            "max_refresh_count": max_refresh_count,
        },
    )
    run = crud.create_scheduled_run(temp_db, plan_id=plan.id, trigger_source="manual")
    return service, plan, run


def make_account(
    temp_db,
    *,
    status: str = "active",
    primary_cpa_service_id: int,
    registered_days_ago: int,
    last_refresh: datetime | None,
    cpa_uploaded: bool = False,
    cpa_uploaded_at: datetime | None = None,
):
    account = crud.create_account(
        temp_db,
        email=f"{uuid4().hex[:10]}@example.com",
        email_service="tempmail",
        access_token="old-ak",
        refresh_token="old-rk",
    )

    account.status = status
    account.primary_cpa_service_id = primary_cpa_service_id
    account.registered_at = datetime.utcnow() - timedelta(days=registered_days_ago)
    account.last_refresh = last_refresh
    account.cpa_uploaded = cpa_uploaded
    account.cpa_uploaded_at = cpa_uploaded_at
    temp_db.commit()
    temp_db.refresh(account)
    return account


def test_refresh_runner_selects_due_accounts_from_registered_at_when_last_refresh_missing(temp_db):
    due = make_account(
        temp_db,
        status="active",
        primary_cpa_service_id=5,
        registered_days_ago=8,
        last_refresh=None,
    )
    make_account(
        temp_db,
        status="active",
        primary_cpa_service_id=5,
        registered_days_ago=2,
        last_refresh=None,
    )

    eligible = crud.get_due_refresh_accounts(
        temp_db,
        cpa_service_id=5,
        refresh_after_days=7,
        limit=10,
    )

    assert [item.id for item in eligible] == [due.id]


def test_refresh_runner_marks_account_expired_on_refresh_failure(temp_db, monkeypatch):
    service, plan, run = _create_refresh_plan_and_run(temp_db)
    stale_uploaded_at = datetime.utcnow() - timedelta(days=1)
    account = make_account(
        temp_db,
        status="active",
        primary_cpa_service_id=service.id,
        registered_days_ago=8,
        last_refresh=None,
        cpa_uploaded=True,
        cpa_uploaded_at=stale_uploaded_at,
    )

    monkeypatch.setattr(
        refresh_runner,
        "refresh_account_token",
        lambda *a, **k: SimpleNamespace(success=False, error_message="bad refresh"),
    )

    summary = run_refresh_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    refreshed = crud.get_account_by_id(temp_db, account.id)
    assert refreshed.status == "expired"
    assert refreshed.invalid_reason == "refresh_failed"
    assert refreshed.cpa_uploaded is False
    assert refreshed.cpa_uploaded_at is None
    assert summary["refresh_failed"] == 1


def test_refresh_runner_keeps_account_active_when_cpa_upload_fails(temp_db, monkeypatch):
    service, plan, run = _create_refresh_plan_and_run(temp_db)
    stale_uploaded_at = datetime.utcnow() - timedelta(days=1)
    account = make_account(
        temp_db,
        status="active",
        primary_cpa_service_id=service.id,
        registered_days_ago=8,
        last_refresh=None,
        cpa_uploaded=True,
        cpa_uploaded_at=stale_uploaded_at,
    )

    monkeypatch.setattr(
        refresh_runner,
        "refresh_account_token",
        lambda *a, **k: SimpleNamespace(
            success=True,
            access_token="ak",
            refresh_token="rk",
            expires_at=datetime.utcnow(),
        ),
    )
    monkeypatch.setattr(refresh_runner, "check_subscription_status", lambda *a, **k: "plus")
    monkeypatch.setattr(refresh_runner, "upload_account_to_bound_cpa", lambda **_: (False, "upstream down"))

    run_refresh_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    refreshed = crud.get_account_by_id(temp_db, account.id)
    assert refreshed.status == "active"
    assert refreshed.cpa_uploaded is False
    assert refreshed.cpa_uploaded_at is None
    assert refreshed.invalid_reason is None


def test_refresh_runner_marks_account_expired_on_subscription_check_failure(temp_db, monkeypatch):
    service, plan, run = _create_refresh_plan_and_run(temp_db)
    stale_uploaded_at = datetime.utcnow() - timedelta(days=1)
    account = make_account(
        temp_db,
        status="active",
        primary_cpa_service_id=service.id,
        registered_days_ago=8,
        last_refresh=None,
        cpa_uploaded=True,
        cpa_uploaded_at=stale_uploaded_at,
    )

    monkeypatch.setattr(
        refresh_runner,
        "refresh_account_token",
        lambda *a, **k: SimpleNamespace(
            success=True,
            access_token="ak",
            refresh_token="rk",
            expires_at=datetime.utcnow(),
        ),
    )
    monkeypatch.setattr(refresh_runner, "check_subscription_status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("probe failed")))
    monkeypatch.setattr(
        refresh_runner,
        "upload_account_to_bound_cpa",
        lambda **_: (_ for _ in ()).throw(AssertionError("upload should not be called")),
    )

    summary = run_refresh_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    refreshed = crud.get_account_by_id(temp_db, account.id)
    assert refreshed.status == "expired"
    assert refreshed.invalid_reason == "subscription_check_failed"
    assert refreshed.cpa_uploaded is False
    assert refreshed.cpa_uploaded_at is None
    assert summary["subscription_failed"] == 1


def test_refresh_runner_updates_tokens_subscription_and_cpa_state_on_success(temp_db, monkeypatch):
    service, plan, run = _create_refresh_plan_and_run(temp_db)
    account = make_account(
        temp_db,
        status="active",
        primary_cpa_service_id=service.id,
        registered_days_ago=8,
        last_refresh=None,
    )

    monkeypatch.setattr(
        refresh_runner,
        "refresh_account_token",
        lambda *a, **k: SimpleNamespace(
            success=True,
            access_token="new-ak",
            refresh_token="new-rk",
            expires_at=datetime.utcnow() + timedelta(days=10),
        ),
    )
    monkeypatch.setattr(refresh_runner, "check_subscription_status", lambda *a, **k: "plus")
    monkeypatch.setattr(refresh_runner, "upload_account_to_bound_cpa", lambda **_: (True, "ok"))

    summary = run_refresh_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    refreshed = crud.get_account_by_id(temp_db, account.id)
    assert refreshed.status == "active"
    assert refreshed.access_token == "new-ak"
    assert refreshed.refresh_token == "new-rk"
    assert refreshed.last_refresh is not None
    assert refreshed.subscription_type == "plus"
    assert refreshed.subscription_at is not None
    assert refreshed.cpa_uploaded is True
    assert refreshed.cpa_uploaded_at is not None
    assert refreshed.invalid_reason is None
    assert summary["uploaded_success"] == 1


def test_refresh_runner_marks_run_cancelled_and_logs_user_stop_when_stop_requested_mid_loop(temp_db, monkeypatch):
    service, plan, run = _create_refresh_plan_and_run(temp_db, max_refresh_count=5)
    first = make_account(
        temp_db,
        status="active",
        primary_cpa_service_id=service.id,
        registered_days_ago=9,
        last_refresh=None,
    )
    second = make_account(
        temp_db,
        status="active",
        primary_cpa_service_id=service.id,
        registered_days_ago=8,
        last_refresh=None,
    )

    monkeypatch.setattr(
        refresh_runner,
        "refresh_account_token",
        lambda *a, **k: SimpleNamespace(
            success=True,
            access_token="new-ak",
            refresh_token="new-rk",
            expires_at=datetime.utcnow() + timedelta(days=10),
        ),
    )
    monkeypatch.setattr(refresh_runner, "check_subscription_status", lambda *a, **k: "plus")

    stop_requested = {"done": False}

    def _upload_account_to_bound_cpa(**kwargs):
        if not stop_requested["done"]:
            stop_requested["done"] = True
            assert engine_module.request_run_stop(run.id, requested_by="tester", reason="user_requested") is True
        return True, "ok"

    monkeypatch.setattr(refresh_runner, "upload_account_to_bound_cpa", _upload_account_to_bound_cpa)

    summary = run_refresh_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    first_refreshed = crud.get_account_by_id(temp_db, first.id)
    second_refreshed = crud.get_account_by_id(temp_db, second.id)
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert persisted_run.status == "cancelled"
    assert persisted_run.error_message == "user requested stop"
    assert summary["processed"] == 1
    assert first_refreshed is not None
    assert first_refreshed.cpa_uploaded is True
    assert second_refreshed is not None
    assert second_refreshed.last_refresh is None
    assert second_refreshed.cpa_uploaded is False
    assert "收到停止请求" in (persisted_run.logs or "")
    assert "任务已按请求停止" in (persisted_run.logs or "")
