from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Iterable, Protocol

from sqlalchemy import asc

from ..database import crud
from ..database.models import ScheduledPlan
from ..database.session import get_db
from .time_utils import SCHEDULER_TZ


class DuePlan(Protocol):
    id: int
    cpa_service_id: int


class SchedulerRepo(Protocol):
    def get_due_enabled_plans(self, now: datetime) -> Iterable[DuePlan]:
        ...

    def create_skipped_run(self, plan_id: int, trigger_source: str, reason: str):
        ...


class SchedulerRepository:
    """Minimal DB-backed repository for scheduler engine dispatch queries."""

    def get_due_enabled_plans(self, now: datetime) -> list[ScheduledPlan]:
        query_now = now.replace(tzinfo=None)
        with get_db() as db:
            return (
                db.query(ScheduledPlan)
                .filter(ScheduledPlan.enabled.is_(True))
                .filter(ScheduledPlan.next_run_at.is_not(None))
                .filter(ScheduledPlan.next_run_at <= query_now)
                .order_by(asc(ScheduledPlan.next_run_at), asc(ScheduledPlan.id))
                .all()
            )

    def create_skipped_run(self, plan_id: int, trigger_source: str, reason: str):
        with get_db() as db:
            run = crud.create_scheduled_run(
                db,
                plan_id=plan_id,
                trigger_source=trigger_source,
                status="skipped",
            )
            crud.finish_scheduled_run(
                db,
                run_id=run.id,
                status="skipped",
                error_message=reason,
                finished_at=datetime.utcnow(),
            )
            return run


def now_in_scheduler_tz() -> datetime:
    return datetime.now(SCHEDULER_TZ)


class SchedulerEngine:
    def __init__(self, repo: SchedulerRepo | None = None, poll_seconds: int = 15):
        self.repo = repo or SchedulerRepository()
        self.poll_seconds = poll_seconds
        self._started = False
        self._state_lock = Lock()
        self._plan_locks: set[int] = set()
        self._cpa_locks: set[int] = set()

    def start(self) -> bool:
        with self._state_lock:
            if self._started:
                return False
            self._started = True
            return True

    def stop(self) -> bool:
        with self._state_lock:
            if not self._started:
                return False
            self._started = False
            return True

    def dispatch_due_plans_once(self) -> None:
        for plan in self.repo.get_due_enabled_plans(now_in_scheduler_tz()):
            if plan.id in self._plan_locks:
                self.repo.create_skipped_run(plan.id, trigger_source="scheduled", reason="plan already running")
                continue
            if plan.cpa_service_id in self._cpa_locks:
                self.repo.create_skipped_run(plan.id, trigger_source="scheduled", reason="cpa already busy")
                continue

    def trigger_plan_now(self, plan_id: int) -> bool:
        """Task 3 placeholder; manual trigger execution is implemented in later tasks."""
        return False


scheduler_engine = SchedulerEngine()
