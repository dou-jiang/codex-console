from dataclasses import dataclass

from src.scheduler.engine import SchedulerEngine


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
        self.created_runs.append((plan_id, trigger_source, "skipped"))


def test_scheduler_engine_skips_plan_when_same_plan_is_running(monkeypatch):
    repo = FakeRepo(due_plan_ids=[1])
    engine = SchedulerEngine(repo=repo)
    engine._plan_locks.add(1)

    engine.dispatch_due_plans_once()

    assert repo.created_runs == [(1, "scheduled", "skipped")]


def test_scheduler_engine_start_is_idempotent():
    engine = SchedulerEngine(repo=FakeRepo())
    assert engine.start() is True
    assert engine.start() is False


def test_scheduler_engine_skips_plan_when_same_cpa_is_locked():
    repo = FakeRepo(due_plans=[FakePlan(id=2, cpa_service_id=8)])
    engine = SchedulerEngine(repo=repo)
    engine._cpa_locks.add(8)

    engine.dispatch_due_plans_once()

    assert repo.created_runs == [(2, "scheduled", "skipped")]
