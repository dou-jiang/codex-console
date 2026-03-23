from __future__ import annotations

from typing import Any

import pytest

from src.database import crud
from src.database.models import Base, ScheduledRun
from src.database.session import DatabaseSessionManager
from src.database import session as session_module
from src.scheduler import engine as engine_module
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


def _create_cleanup_plan_and_run(temp_db, *, max_cleanup_count: int = 10, max_probe_count: int | None = None):
    service = crud.create_cpa_service(
        temp_db,
        name="cleanup-cpa",
        api_url="https://example.test/v0/management",
        api_token="token",
    )
    config = {"max_cleanup_count": max_cleanup_count}
    if max_probe_count is not None:
        config["max_probe_count"] = max_probe_count
    plan = crud.create_scheduled_plan(
        temp_db,
        name="cleanup",
        task_type="cpa_cleanup",
        cpa_service_id=service.id,
        trigger_type="interval",
        interval_value=1,
        interval_unit="hours",
        config=config,
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


def test_cleanup_runner_passes_max_probe_count_to_probe_invalid_accounts(temp_db, monkeypatch):
    _, plan, run = _create_cleanup_plan_and_run(temp_db, max_probe_count=25)

    captured: dict[str, Any] = {}

    def _fake_probe_invalid_accounts(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cleanup_runner, "probe_invalid_accounts", _fake_probe_invalid_accounts)
    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", lambda **_: {"deleted": 0, "failed": 0})

    run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    assert captured["max_probe_count"] == 25
    assert callable(captured["progress_callback"])


def test_cleanup_runner_persists_probe_progress_logs(temp_db, monkeypatch):
    _, plan, run = _create_cleanup_plan_and_run(temp_db, max_probe_count=20)

    def _fake_probe_invalid_accounts(**kwargs):
        kwargs["progress_callback"]("probe candidates loaded (total=200, selected=20)")
        kwargs["progress_callback"]("probe progress (scanned=20/20, invalid=0)")
        return []

    monkeypatch.setattr(cleanup_runner, "probe_invalid_accounts", _fake_probe_invalid_accounts)
    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", lambda **_: {"deleted": 0, "failed": 0})

    run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert "probe candidates loaded (total=200, selected=20)" in (persisted_run.logs or "")
    assert "probe progress (scanned=20/20, invalid=0)" in (persisted_run.logs or "")


def test_cleanup_runner_logs_staged_probe_expire_and_delete_progress(temp_db, monkeypatch):
    service, plan, run = _create_cleanup_plan_and_run(temp_db, max_cleanup_count=250, max_probe_count=250)

    invalid_items = []
    for idx in range(250):
        email = f"user{idx}@example.com"
        account = crud.create_account(temp_db, email=email, email_service="tempmail")
        crud.update_account(temp_db, account.id, primary_cpa_service_id=service.id, status="active")
        invalid_items.append({"email": email, "name": f"{email}.json"})

    def _fake_probe_invalid_accounts(**kwargs):
        kwargs["progress_callback"]("probe candidates loaded (total=250, selected=250)")
        kwargs["progress_callback"]("probe progress (scanned=100/250, invalid=10)")
        kwargs["progress_callback"]("probe progress (scanned=200/250, invalid=25)")
        kwargs["progress_callback"]("probe progress (scanned=250/250, invalid=30)")
        return invalid_items

    monkeypatch.setattr(cleanup_runner, "probe_invalid_accounts", _fake_probe_invalid_accounts)
    monkeypatch.setattr(
        cleanup_runner,
        "delete_invalid_accounts",
        lambda **kwargs: {"deleted": len(kwargs["names"]), "failed": 0},
    )

    summary = run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert summary["local_marked_expired"] == 250
    assert summary["remote_deleted"] == 250
    logs = persisted_run.logs or ""
    assert "cleanup probe progress (scanned=100, invalid=10)" in logs
    assert "cleanup probe progress (scanned=200, invalid=25)" in logs
    assert "cleanup expire progress (processed=100, local_expired=100)" in logs
    assert "cleanup expire progress (processed=200, local_expired=200)" in logs
    assert "cleanup expire progress (processed=250" not in logs
    assert (
        "cleanup delete progress (processed=100, remote_deleted=100, remote_delete_failed=0)"
        in logs
    )
    assert (
        "cleanup delete progress (processed=200, remote_deleted=200, remote_delete_failed=0)"
        in logs
    )
    assert "cleanup delete progress (processed=250" not in logs


def test_cleanup_runner_marks_run_cancelled_and_logs_user_stop_when_stop_requested_mid_loop(temp_db, monkeypatch):
    service, plan, run = _create_cleanup_plan_and_run(temp_db, max_cleanup_count=5)

    invalid_items = []
    for idx in range(5):
        email = f"stop-{idx}@example.com"
        account = crud.create_account(temp_db, email=email, email_service="tempmail")
        crud.update_account(temp_db, account.id, primary_cpa_service_id=service.id, status="active")
        invalid_items.append({"email": email, "name": f"{email}.json"})

    monkeypatch.setattr(cleanup_runner, "probe_invalid_accounts", lambda **_: invalid_items)
    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", lambda **_: {"deleted": 0, "failed": 0})

    original_mark_expired = cleanup_runner.crud.mark_account_expired_by_email_and_cpa
    stop_requested = {"done": False}

    def _mark_account_expired_and_request_stop(db, **kwargs):
        marked = original_mark_expired(db, **kwargs)
        if not stop_requested["done"]:
            stop_requested["done"] = True
            assert engine_module.request_run_stop(run.id, requested_by="tester", reason="user_requested") is True
        return marked

    monkeypatch.setattr(
        cleanup_runner.crud,
        "mark_account_expired_by_email_and_cpa",
        _mark_account_expired_and_request_stop,
    )

    summary = run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert persisted_run is not None
    assert persisted_run.status == "cancelled"
    assert persisted_run.error_message == "user requested stop"
    assert summary["local_marked_expired"] == 1
    assert summary["remote_deleted"] == 0
    assert "收到停止请求" in (persisted_run.logs or "")
    assert "任务已按请求停止" in (persisted_run.logs or "")


def test_cleanup_runner_marks_run_cancelled_when_stop_requested_during_final_delete_batch(temp_db, monkeypatch):
    service, plan, run = _create_cleanup_plan_and_run(temp_db, max_cleanup_count=250)

    invalid_items = []
    for idx in range(250):
        email = f"final-delete-{idx}@example.com"
        account = crud.create_account(temp_db, email=email, email_service="tempmail")
        crud.update_account(temp_db, account.id, primary_cpa_service_id=service.id, status="active")
        invalid_items.append({"email": email, "name": f"{email}.json"})

    monkeypatch.setattr(cleanup_runner, "probe_invalid_accounts", lambda **_: invalid_items)

    delete_batch_sizes: list[int] = []

    def _delete_invalid_accounts(**kwargs):
        names = kwargs["names"]
        delete_batch_sizes.append(len(names))
        if len(names) == 50:
            assert engine_module.request_run_stop(
                run.id,
                requested_by="tester",
                reason="user_requested",
            ) is True
        return {"deleted": len(names), "failed": 0}

    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", _delete_invalid_accounts)

    summary = run_cleanup_plan(plan_id=plan.id, run_id=run.id)

    temp_db.expire_all()
    persisted_run = temp_db.get(ScheduledRun, run.id)
    assert delete_batch_sizes == [100, 100, 50]
    assert persisted_run is not None
    assert persisted_run.status == "cancelled"
    assert persisted_run.error_message == "user requested stop"
    assert summary["remote_deleted"] == 250
    assert "cleanup runner complete" not in (persisted_run.logs or "")
    assert "收到停止请求" in (persisted_run.logs or "")
    assert "任务已按请求停止" in (persisted_run.logs or "")


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
