import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event

import pytest

from src.database import crud
from src.database import session as session_module
from src.database.models import Base, ScheduledRun
from src.database.session import DatabaseSessionManager
from src.scheduler import engine as engine_module
from src.scheduler import run_logger
from src.scheduler.engine import SchedulerDispatchError, SchedulerEngine, SchedulerPlanConflictError
from src.web.app import create_app


@dataclass
class FakePlan:
    id: int
    cpa_service_id: int = 1
    task_type: str = "cpa_cleanup"


class FakeRepo:
    def __init__(self, due_plan_ids=None, due_plans=None):
        if due_plans is None:
            due_plans = [FakePlan(id=plan_id, cpa_service_id=plan_id) for plan_id in (due_plan_ids or [])]
        self._due_plans = list(due_plans)
        self.created_runs = []

    def get_due_enabled_plans(self, now):
        return list(self._due_plans)

    def create_skipped_run(self, plan_id, trigger_source, reason):
        self.created_runs.append((plan_id, trigger_source, "skipped", reason))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "scheduler-engine.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()
    monkeypatch.setattr(session_module, "_db_manager", manager)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _create_plan(temp_db, *, task_type: str = "cpa_cleanup", due: bool = True):
    service = crud.create_cpa_service(
        temp_db,
        name=f"svc-{task_type}",
        api_url="https://cpa.example.com/api",
        api_token="token",
    )
    next_run_at = datetime.utcnow() - timedelta(minutes=5) if due else datetime.utcnow() + timedelta(hours=1)
    return crud.create_scheduled_plan(
        temp_db,
        name=f"plan-{task_type}",
        task_type=task_type,
        cpa_service_id=service.id,
        trigger_type="interval",
        interval_value=1,
        interval_unit="hours",
        config={"max_consecutive_failures": 3},
        enabled=True,
        next_run_at=next_run_at,
    )


def test_scheduler_engine_skips_plan_when_same_plan_is_running(monkeypatch):
    repo = FakeRepo(due_plan_ids=[1])
    engine = SchedulerEngine(repo=repo)
    engine._plan_locks.add(1)

    engine.dispatch_due_plans_once()

    assert repo.created_runs == [(1, "scheduled", "skipped", "plan already running")]


def test_scheduler_engine_start_is_idempotent():
    engine = SchedulerEngine(repo=FakeRepo())
    assert engine.start() is True
    assert engine.start() is False


def test_scheduler_engine_skips_plan_when_same_cpa_is_locked():
    repo = FakeRepo(due_plans=[FakePlan(id=2, cpa_service_id=8)])
    engine = SchedulerEngine(repo=repo)
    engine._cpa_locks.add(8)

    engine.dispatch_due_plans_once()

    assert repo.created_runs == [(2, "scheduled", "skipped", "cpa already busy")]


def test_scheduler_engine_dispatches_due_plan_to_matching_runner_and_records_run(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=True)
    calls = []

    def _cleanup_runner(*, plan_id: int, run_id: int):
        calls.append((plan_id, run_id))
        with session_module.get_db() as db:
            crud.finish_scheduled_run(db, run_id=run_id, status="success", summary={"dispatched": True})

    engine = SchedulerEngine(
        runner_map={"cpa_cleanup": _cleanup_runner},
        worker_spawner=lambda fn, _name: fn(),
    )

    engine.dispatch_due_plans_once()

    assert calls
    assert calls[0][0] == plan.id

    temp_db.expire_all()
    runs = temp_db.query(ScheduledRun).filter(ScheduledRun.plan_id == plan.id).all()
    assert len(runs) == 1
    assert runs[0].trigger_source == "scheduled"
    assert runs[0].status == "success"
    assert runs[0].summary == {"dispatched": True}

    persisted_plan = crud.get_scheduled_plan_by_id(temp_db, plan.id)
    assert persisted_plan is not None
    assert persisted_plan.last_run_status == "success"
    assert persisted_plan.last_run_started_at is not None
    assert persisted_plan.last_run_finished_at is not None
    assert persisted_plan.next_run_at is not None
    assert persisted_plan.next_run_at > datetime.utcnow() - timedelta(minutes=1)


def test_scheduler_engine_manual_trigger_creates_run_and_dispatches_runner(temp_db):
    plan = _create_plan(temp_db, task_type="account_refresh", due=False)
    calls = []

    def _refresh_runner(*, plan_id: int, run_id: int):
        calls.append((plan_id, run_id))
        with session_module.get_db() as db:
            crud.finish_scheduled_run(db, run_id=run_id, status="success", summary={"manual": True})

    engine = SchedulerEngine(
        runner_map={"account_refresh": _refresh_runner},
        worker_spawner=lambda fn, _name: fn(),
    )

    run_id = engine.trigger_plan_now(plan.id)

    assert isinstance(run_id, int)
    assert calls == [(plan.id, run_id)]

    temp_db.expire_all()
    run = temp_db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
    assert run is not None
    assert run.trigger_source == "manual"
    assert run.status == "success"
    assert run.summary == {"manual": True}


def test_scheduler_engine_request_run_stop_marks_running_run(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)
    engine = SchedulerEngine(
        runner_map={"cpa_cleanup": lambda **kwargs: None},
        worker_spawner=lambda _fn, _name: None,
    )

    run_id = engine.trigger_plan_now(plan.id)
    updated = engine.request_run_stop(run_id)

    assert updated is True
    assert engine.is_run_stop_requested(run_id) is True
    temp_db.expire_all()
    run = temp_db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
    assert run is not None
    assert run.stop_requested_at is not None


def test_scheduler_module_stop_helpers_work_without_engine_instance(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)
    with session_module.get_db() as db:
        run = crud.create_scheduled_run(db, plan_id=plan.id, trigger_source="manual", status="running")

    assert engine_module.is_run_stop_requested(run.id) is False
    assert engine_module.request_run_stop(run.id, requested_by="tester", reason="stop now") is True
    assert engine_module.is_run_stop_requested(run.id) is True


def test_scheduler_engine_request_run_stop_rejects_finished_or_missing_runs(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)
    with session_module.get_db() as db:
        run = crud.create_scheduled_run(db, plan_id=plan.id, trigger_source="manual", status="success")
        crud.finish_scheduled_run(db, run_id=run.id, status="success")

    engine = SchedulerEngine()
    assert engine.request_run_stop(run.id) is False
    assert engine.request_run_stop(run.id + 9999) is False
    assert engine.is_run_stop_requested(run.id) is False
    assert engine.is_run_stop_requested(run.id + 9999) is False


def test_scheduler_engine_request_run_stop_rejects_non_running_states(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)
    with session_module.get_db() as db:
        skipped = crud.create_scheduled_run(db, plan_id=plan.id, trigger_source="manual", status="skipped")
        failed = crud.create_scheduled_run(db, plan_id=plan.id, trigger_source="manual", status="failed")
        cancelled = crud.create_scheduled_run(db, plan_id=plan.id, trigger_source="manual", status="cancelled")
        skipped_id = skipped.id
        failed_id = failed.id
        cancelled_id = cancelled.id

    engine = SchedulerEngine()
    assert engine.request_run_stop(skipped_id) is False
    assert engine.request_run_stop(failed_id) is False
    assert engine.request_run_stop(cancelled_id) is False
    assert engine.is_run_stop_requested(skipped_id) is False
    assert engine.is_run_stop_requested(failed_id) is False
    assert engine.is_run_stop_requested(cancelled_id) is False


def test_scheduler_engine_dispatch_persists_task_type_on_created_run(temp_db):
    plan = _create_plan(temp_db, task_type="account_refresh", due=False)

    engine = SchedulerEngine(
        runner_map={"account_refresh": lambda **kwargs: None},
        worker_spawner=lambda fn, _name: fn(),
    )

    run_id = engine.trigger_plan_now(plan.id)

    temp_db.expire_all()
    run = temp_db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
    assert run is not None
    assert run.task_type == "account_refresh"


def test_scheduler_engine_manual_trigger_rejects_running_plan(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)
    engine = SchedulerEngine()
    engine._plan_locks.add(plan.id)

    with pytest.raises(SchedulerPlanConflictError):
        engine.trigger_plan_now(plan.id)

    temp_db.expire_all()
    runs = temp_db.query(ScheduledRun).filter(ScheduledRun.plan_id == plan.id).all()
    assert len(runs) == 1
    assert runs[0].status == "skipped"
    assert runs[0].trigger_source == "manual"


def test_scheduler_engine_manual_trigger_raises_dispatch_error_on_internal_failure(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)

    engine = SchedulerEngine(
        runner_map={"cpa_cleanup": lambda **kwargs: None},
        worker_spawner=lambda _fn, _name: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )

    with pytest.raises(SchedulerDispatchError):
        engine.trigger_plan_now(plan.id)

    temp_db.expire_all()
    runs = temp_db.query(ScheduledRun).filter(ScheduledRun.plan_id == plan.id).all()
    assert len(runs) == 1
    assert runs[0].status == "failed"


def test_scheduler_engine_dispatch_failure_does_not_create_extra_skipped_run(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=True)
    engine = SchedulerEngine(
        runner_map={"cpa_cleanup": lambda **kwargs: None},
        worker_spawner=lambda _fn, _name: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )

    engine.dispatch_due_plans_once()

    temp_db.expire_all()
    runs = (
        temp_db.query(ScheduledRun)
        .filter(ScheduledRun.plan_id == plan.id)
        .order_by(ScheduledRun.id.asc())
        .all()
    )
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].trigger_source == "scheduled"


def test_scheduler_engine_runner_cancellation_marks_run_cancelled_and_updates_plan(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)
    allow_raise = Event()

    def _cleanup_runner(*, plan_id: int, run_id: int):
        allow_raise.wait(timeout=1.0)
        raise engine_module.ScheduledRunCancelledError("stop requested")

    engine = SchedulerEngine(runner_map={"cpa_cleanup": _cleanup_runner})

    run_id = engine.trigger_plan_now(plan.id)
    assert engine.request_run_stop(run_id) is True
    allow_raise.set()

    deadline = datetime.utcnow() + timedelta(seconds=2)
    run = None
    persisted_plan = None
    while datetime.utcnow() < deadline:
        temp_db.expire_all()
        run = temp_db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
        persisted_plan = crud.get_scheduled_plan_by_id(temp_db, plan.id)
        if (
            run is not None
            and run.status == "cancelled"
            and persisted_plan is not None
            and persisted_plan.last_run_status == "cancelled"
        ):
            break
    assert run is not None
    assert run.status == "cancelled"
    assert run.error_message == "user requested stop"
    assert run.finished_at is not None

    assert persisted_plan is not None
    assert persisted_plan.last_run_status == "cancelled"
    assert persisted_plan.last_run_finished_at is not None


def test_scheduler_engine_runner_cancellation_overrides_failed_status_from_runner(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)

    def _cleanup_runner(*, plan_id: int, run_id: int):
        engine_module.request_run_stop(run_id, requested_by="runner", reason="stop requested")
        with session_module.get_db() as db:
            crud.finish_scheduled_run(db, run_id=run_id, status="failed", error_message="runner generic fail")
        raise engine_module.ScheduledRunCancelledError("stop requested")

    engine = SchedulerEngine(
        runner_map={"cpa_cleanup": _cleanup_runner},
        worker_spawner=lambda fn, _name: fn(),
    )

    run_id = engine.trigger_plan_now(plan.id)

    temp_db.expire_all()
    run = temp_db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
    assert run is not None
    assert run.status == "cancelled"
    assert run.error_message == "user requested stop"

    persisted_plan = crud.get_scheduled_plan_by_id(temp_db, plan.id)
    assert persisted_plan is not None
    assert persisted_plan.last_run_status == "cancelled"


def test_scheduler_engine_runner_cancellation_without_stop_request_is_not_user_cancelled(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)

    def _cleanup_runner(*, plan_id: int, run_id: int):
        raise engine_module.ScheduledRunCancelledError("stop requested")

    engine = SchedulerEngine(
        runner_map={"cpa_cleanup": _cleanup_runner},
        worker_spawner=lambda fn, _name: fn(),
    )

    run_id = engine.trigger_plan_now(plan.id)

    temp_db.expire_all()
    run = temp_db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
    assert run is not None
    assert run.status == "failed"
    assert run.error_message == "stop requested"

    persisted_plan = crud.get_scheduled_plan_by_id(temp_db, plan.id)
    assert persisted_plan is not None
    assert persisted_plan.last_run_status == "failed"


def test_run_logger_append_log_uses_logged_at_for_last_log_timestamp(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=False)
    with session_module.get_db() as db:
        run = crud.create_scheduled_run(db, plan_id=plan.id, trigger_source="manual", status="running")
        run_id = run.id

    logged_at = datetime(2025, 1, 2, 3, 4, 5)
    assert run_logger.append_run_log(run_id, "hello", logged_at=logged_at) is True

    temp_db.expire_all()
    persisted = temp_db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
    assert persisted is not None
    assert persisted.logs == "hello"
    assert persisted.last_log_at == logged_at


def test_scheduler_engine_skipped_run_updates_plan_summary_fields(temp_db):
    plan = _create_plan(temp_db, task_type="cpa_cleanup", due=True)
    engine = SchedulerEngine()
    engine._plan_locks.add(plan.id)

    engine.dispatch_due_plans_once()

    temp_db.expire_all()
    runs = temp_db.query(ScheduledRun).filter(ScheduledRun.plan_id == plan.id).all()
    assert len(runs) == 1
    assert runs[0].status == "skipped"

    persisted_plan = crud.get_scheduled_plan_by_id(temp_db, plan.id)
    assert persisted_plan is not None
    assert persisted_plan.last_run_status == "skipped"
    assert persisted_plan.last_run_started_at is not None
    assert persisted_plan.last_run_finished_at is not None


def test_scheduler_engine_start_launches_single_background_poll_loop(monkeypatch):
    repo = FakeRepo()
    engine = SchedulerEngine(repo=repo, poll_seconds=60)
    called = Event()

    def _dispatch_once():
        called.set()

    monkeypatch.setattr(engine, "dispatch_due_plans_once", _dispatch_once)

    assert engine.start() is True
    assert called.wait(timeout=1.0)

    first_thread = engine._poll_thread
    assert first_thread is not None
    assert first_thread.is_alive()
    assert engine.start() is False
    assert engine._poll_thread is first_thread

    assert engine.stop() is True
    first_thread.join(timeout=1.0)
    assert not first_thread.is_alive()


def test_create_app_startup_shutdown_uses_isolated_scheduler_engine_instances(monkeypatch):
    monkeypatch.setattr("src.database.init_db.initialize_database", lambda: None)

    app_one = create_app()
    app_two = create_app()

    engine_one = app_one.state.scheduler_engine
    engine_two = app_two.state.scheduler_engine

    assert engine_one is not engine_two
    assert engine_one._started is False
    assert engine_two._started is False

    for handler in app_one.router.on_startup:
        asyncio.run(handler())

    assert engine_one._started is True
    assert engine_two._started is False

    for handler in app_one.router.on_shutdown:
        asyncio.run(handler())

    assert engine_one._started is False
    assert engine_two._started is False

    for handler in app_two.router.on_startup:
        asyncio.run(handler())

    assert engine_one._started is False
    assert engine_two._started is True

    for handler in app_two.router.on_shutdown:
        asyncio.run(handler())

    assert engine_one._started is False
    assert engine_two._started is False
