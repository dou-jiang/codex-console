from dataclasses import dataclass
import asyncio

from src.scheduler.engine import SchedulerEngine
from src.web.app import create_app


@dataclass
class FakePlan:
    id: int
    cpa_service_id: int = 1


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
