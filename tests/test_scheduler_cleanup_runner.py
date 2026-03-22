from __future__ import annotations

from typing import Any

import pytest

from src.database import crud
from src.database.models import Base, ScheduledRun
from src.database.session import DatabaseSessionManager
from src.database import session as session_module
from src.scheduler.runners import cleanup as cleanup_runner
from src.scheduler.runners.cleanup import run_cleanup_plan


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "scheduler-cleanup.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()
    monkeypatch.setattr(session_module, "_db_manager", manager)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _create_cleanup_plan_and_run(temp_db, *, max_cleanup_count: int = 10):
    service = crud.create_cpa_service(
        temp_db,
        name="cleanup-cpa",
        api_url="https://example.test/v0/management",
        api_token="token",
    )
    plan = crud.create_scheduled_plan(
        temp_db,
        name="cleanup",
        task_type="cpa_cleanup",
        cpa_service_id=service.id,
        trigger_type="interval",
        interval_value=1,
        interval_unit="hours",
        config={"max_cleanup_count": max_cleanup_count},
    )
    run = crud.create_scheduled_run(temp_db, plan_id=plan.id, trigger_source="manual")
    return service, plan, run


def test_cleanup_runner_marks_local_accounts_expired_for_matching_primary_cpa(temp_db, monkeypatch):
    service, plan, run = _create_cleanup_plan_and_run(temp_db)

    account = crud.create_account(temp_db, email="a@example.com", email_service="tempmail")
    crud.update_account(temp_db, account.id, primary_cpa_service_id=service.id, status="active")

    monkeypatch.setattr(
        cleanup_runner,
        "probe_invalid_accounts",
        lambda **_: [{"email": "a@example.com", "name": "a@example.com.json"}],
    )
    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", lambda **_: {"deleted": 1, "failed": 0})

    summary = run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    refreshed = crud.get_account_by_id(temp_db, account.id)
    assert refreshed.status == "expired"
    assert refreshed.invalid_reason == "cpa_cleanup"
    assert summary["local_marked_expired"] == 1


def test_cleanup_runner_respects_max_cleanup_count(temp_db, monkeypatch):
    service, plan, run = _create_cleanup_plan_and_run(temp_db, max_cleanup_count=1)

    first = crud.create_account(temp_db, email="first@example.com", email_service="tempmail")
    second = crud.create_account(temp_db, email="second@example.com", email_service="tempmail")
    crud.update_account(temp_db, first.id, primary_cpa_service_id=service.id, status="active")
    crud.update_account(temp_db, second.id, primary_cpa_service_id=service.id, status="active")

    monkeypatch.setattr(
        cleanup_runner,
        "probe_invalid_accounts",
        lambda **_: [
            {"email": "first@example.com", "name": "first@example.com.json"},
            {"email": "second@example.com", "name": "second@example.com.json"},
        ],
    )

    captured: dict[str, Any] = {}

    def _fake_delete_invalid_accounts(*, names, **kwargs):
        captured["names"] = names
        return {"deleted": len(names), "failed": 0}

    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", _fake_delete_invalid_accounts)

    summary = run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    assert crud.get_account_by_id(temp_db, first.id).status == "expired"
    assert crud.get_account_by_id(temp_db, second.id).status == "active"
    assert captured["names"] == ["first@example.com.json"]
    assert summary["local_marked_expired"] == 1


def test_cleanup_runner_does_not_expire_account_for_other_primary_cpa(temp_db, monkeypatch):
    _, plan, run = _create_cleanup_plan_and_run(temp_db)

    account = crud.create_account(temp_db, email="mismatch@example.com", email_service="tempmail")
    crud.update_account(temp_db, account.id, primary_cpa_service_id=999, status="active")

    monkeypatch.setattr(
        cleanup_runner,
        "probe_invalid_accounts",
        lambda **_: [{"email": "mismatch@example.com", "name": "mismatch@example.com.json"}],
    )
    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", lambda **_: {"deleted": 1, "failed": 0})

    summary = run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    refreshed = crud.get_account_by_id(temp_db, account.id)
    assert refreshed.status == "active"
    assert refreshed.invalid_reason is None
    assert summary["local_marked_expired"] == 0


def test_cleanup_runner_persists_success_summary_logs_and_final_status(temp_db, monkeypatch):
    service, plan, run = _create_cleanup_plan_and_run(temp_db)

    account = crud.create_account(temp_db, email="ok@example.com", email_service="tempmail")
    crud.update_account(temp_db, account.id, primary_cpa_service_id=service.id, status="active")

    monkeypatch.setattr(
        cleanup_runner,
        "probe_invalid_accounts",
        lambda **_: [{"email": "ok@example.com", "name": "ok@example.com.json"}],
    )
    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", lambda **_: {"deleted": 1, "failed": 0})

    summary = run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert persisted_run.status == "success"
    assert persisted_run.finished_at is not None
    assert persisted_run.summary == summary
    assert "cleanup runner start" in (persisted_run.logs or "")
    assert "cleanup runner complete" in (persisted_run.logs or "")


def test_cleanup_runner_persists_failure_status_when_probe_raises(temp_db, monkeypatch):
    _, plan, run = _create_cleanup_plan_and_run(temp_db)

    def _raise_probe(**kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(cleanup_runner, "probe_invalid_accounts", _raise_probe)
    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", lambda **_: {"deleted": 0, "failed": 0})

    with pytest.raises(RuntimeError, match="probe failed"):
        run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert persisted_run.status == "failed"
    assert persisted_run.finished_at is not None
    assert persisted_run.error_message == "probe failed"
    assert persisted_run.summary == {
        "invalid_items_found": 0,
        "invalid_items_considered": 0,
        "local_marked_expired": 0,
        "remote_deleted": 0,
        "remote_delete_failed": 0,
    }
    assert "cleanup runner failed: probe failed" in (persisted_run.logs or "")
