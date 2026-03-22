from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from threading import Event, Lock, Thread, current_thread
from typing import Any, Callable, Iterable, Protocol

from sqlalchemy import asc

from ..database import crud
from ..database.models import ScheduledPlan, ScheduledRun
from ..database.session import get_db
from .time_utils import SCHEDULER_TZ, compute_next_run_at


logger = logging.getLogger(__name__)


class SchedulerPlanConflictError(RuntimeError):
    """Raised when a plan trigger is rejected due to lock conflict."""


class SchedulerDispatchError(RuntimeError):
    """Raised when a plan trigger cannot be dispatched."""


@dataclass(frozen=True)
class DispatchResult:
    run_id: int | None
    run_created: bool
    dispatched: bool


class DuePlan(Protocol):
    id: int
    cpa_service_id: int
    task_type: str


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
            finished = crud.finish_scheduled_run(
                db,
                run_id=run.id,
                status="skipped",
                error_message=reason,
                finished_at=datetime.utcnow(),
            )
            if finished is not None:
                crud.update_scheduled_plan(
                    db,
                    plan_id,
                    last_run_started_at=finished.started_at,
                    last_run_finished_at=finished.finished_at,
                    last_run_status="skipped",
                )
                return finished
            return run


def now_in_scheduler_tz() -> datetime:
    return datetime.now(SCHEDULER_TZ)


def _to_scheduler_naive_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(SCHEDULER_TZ).replace(tzinfo=None)


def _default_runner_map() -> dict[str, Callable[..., Any]]:
    from .runners.cleanup import run_cleanup_plan
    from .runners.refill import run_refill_plan
    from .runners.refresh import run_refresh_plan

    return {
        "cpa_cleanup": run_cleanup_plan,
        "cpa_refill": run_refill_plan,
        "account_refresh": run_refresh_plan,
    }


class SchedulerEngine:
    def __init__(
        self,
        repo: SchedulerRepo | None = None,
        poll_seconds: int = 15,
        runner_map: dict[str, Callable[..., Any]] | None = None,
        worker_spawner: Callable[[Callable[[], None], str], None] | None = None,
    ):
        self.repo = repo or SchedulerRepository()
        self.poll_seconds = poll_seconds
        self._runner_map = runner_map or _default_runner_map()
        self._worker_spawner = worker_spawner or self._spawn_worker
        self._started = False
        self._state_lock = Lock()
        self._stop_event = Event()
        self._poll_thread: Thread | None = None
        self._plan_locks: set[int] = set()
        self._cpa_locks: set[int] = set()

    def start(self) -> bool:
        with self._state_lock:
            if self._started:
                return False
            self._started = True
            self._stop_event.clear()
            self._poll_thread = Thread(target=self._poll_forever, name="scheduler-engine-poller", daemon=True)
            self._poll_thread.start()
            return True

    def stop(self) -> bool:
        thread: Thread | None = None
        with self._state_lock:
            if not self._started:
                return False
            self._started = False
            self._stop_event.set()
            thread = self._poll_thread
        if thread and thread.is_alive() and thread is not current_thread():
            thread.join(timeout=max(1.0, float(self.poll_seconds) + 0.5))
        return True

    def dispatch_due_plans_once(self) -> None:
        schedule_now = now_in_scheduler_tz()
        for plan in self.repo.get_due_enabled_plans(schedule_now):
            acquired, reason = self._try_acquire_run_locks(plan.id, plan.cpa_service_id)
            if not acquired:
                self.repo.create_skipped_run(plan.id, trigger_source="scheduled", reason=reason or "busy")
                continue
            dispatch_result = self._dispatch_locked_plan(
                plan_id=plan.id,
                cpa_service_id=plan.cpa_service_id,
                task_type=plan.task_type,
                trigger_source="scheduled",
                schedule_now=schedule_now,
            )
            if not dispatch_result.run_created:
                self.repo.create_skipped_run(plan.id, trigger_source="scheduled", reason="dispatch failed")

    def trigger_plan_now(self, plan_id: int) -> int:
        """Trigger a plan immediately and return run_id when accepted."""
        with get_db() as db:
            plan = crud.get_scheduled_plan_by_id(db, plan_id)
            if plan is None:
                raise SchedulerDispatchError(f"scheduled plan {plan_id} does not exist")
            cpa_service_id = int(plan.cpa_service_id)
            task_type = str(plan.task_type)

        acquired, reason = self._try_acquire_run_locks(plan_id, cpa_service_id)
        if not acquired:
            self.repo.create_skipped_run(
                plan_id,
                trigger_source="manual",
                reason=reason or "busy",
            )
            raise SchedulerPlanConflictError(reason or "plan is busy")

        dispatch_result = self._dispatch_locked_plan(
            plan_id=plan_id,
            cpa_service_id=cpa_service_id,
            task_type=task_type,
            trigger_source="manual",
            schedule_now=None,
        )
        if not dispatch_result.dispatched or dispatch_result.run_id is None:
            raise SchedulerDispatchError(f"failed to dispatch scheduled plan {plan_id}")
        return dispatch_result.run_id

    def _poll_forever(self) -> None:
        while True:
            with self._state_lock:
                if not self._started:
                    return
            try:
                self.dispatch_due_plans_once()
            except Exception:
                logger.exception("scheduler poll loop dispatch failed")
            if self._stop_event.wait(self.poll_seconds):
                return

    def _try_acquire_run_locks(self, plan_id: int, cpa_service_id: int) -> tuple[bool, str | None]:
        with self._state_lock:
            if plan_id in self._plan_locks:
                return False, "plan already running"
            if cpa_service_id in self._cpa_locks:
                return False, "cpa already busy"
            self._plan_locks.add(plan_id)
            self._cpa_locks.add(cpa_service_id)
            return True, None

    def _release_run_locks(self, plan_id: int, cpa_service_id: int) -> None:
        with self._state_lock:
            self._plan_locks.discard(plan_id)
            self._cpa_locks.discard(cpa_service_id)

    def _dispatch_locked_plan(
        self,
        *,
        plan_id: int,
        cpa_service_id: int,
        task_type: str,
        trigger_source: str,
        schedule_now: datetime | None,
    ) -> DispatchResult:
        run_id: int | None = None
        worker_spawned = False
        try:
            run_id = self._create_run_and_mark_started(
                plan_id=plan_id,
                trigger_source=trigger_source,
                schedule_now=schedule_now,
            )
            if run_id is None:
                return DispatchResult(run_id=None, run_created=False, dispatched=False)

            runner = self._runner_map.get(task_type)
            if runner is None:
                self._ensure_run_failed(run_id, f"unsupported task_type: {task_type}")
                self._sync_plan_state_from_run(plan_id, run_id)
                return DispatchResult(run_id=run_id, run_created=True, dispatched=False)

            def _worker() -> None:
                try:
                    runner(plan_id=plan_id, run_id=run_id)
                except Exception as exc:
                    logger.exception("scheduled runner failed (plan_id=%s, run_id=%s)", plan_id, run_id)
                    self._ensure_run_failed(run_id, str(exc))
                finally:
                    self._sync_plan_state_from_run(plan_id, run_id)
                    self._release_run_locks(plan_id, cpa_service_id)

            self._worker_spawner(_worker, f"scheduler-run-{plan_id}-{run_id}")
            worker_spawned = True
            return DispatchResult(run_id=run_id, run_created=True, dispatched=True)
        except Exception:
            logger.exception("failed to dispatch plan (plan_id=%s)", plan_id)
            if run_id is not None:
                self._ensure_run_failed(run_id, "dispatch failed")
                self._sync_plan_state_from_run(plan_id, run_id)
                return DispatchResult(run_id=run_id, run_created=True, dispatched=False)
            return DispatchResult(run_id=None, run_created=False, dispatched=False)
        finally:
            if not worker_spawned:
                self._release_run_locks(plan_id, cpa_service_id)

    def _create_run_and_mark_started(
        self,
        *,
        plan_id: int,
        trigger_source: str,
        schedule_now: datetime | None,
    ) -> int | None:
        with get_db() as db:
            plan = crud.get_scheduled_plan_by_id(db, plan_id)
            if plan is None:
                return None

            run = crud.create_scheduled_run(
                db,
                plan_id=plan_id,
                trigger_source=trigger_source,
                status="running",
            )
            updates: dict[str, Any] = {
                "last_run_started_at": run.started_at,
                "last_run_finished_at": None,
                "last_run_status": "running",
            }
            if trigger_source == "scheduled" and schedule_now is not None:
                updates["next_run_at"] = _to_scheduler_naive_time(compute_next_run_at(plan, now=schedule_now))

            crud.update_scheduled_plan(db, plan_id, **updates)
            return int(run.id)

    def _ensure_run_failed(self, run_id: int, error_message: str) -> None:
        with get_db() as db:
            run = db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
            if run is None or run.status != "running":
                return
            crud.finish_scheduled_run(
                db,
                run_id=run_id,
                status="failed",
                error_message=error_message,
                finished_at=datetime.utcnow(),
            )

    def _sync_plan_state_from_run(self, plan_id: int, run_id: int) -> None:
        with get_db() as db:
            run = db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
            if run is None:
                return
            if run.status == "running":
                run = crud.finish_scheduled_run(
                    db,
                    run_id=run_id,
                    status="failed",
                    error_message="runner did not finalize status",
                    finished_at=datetime.utcnow(),
                )
                if run is None:
                    return

            finished_at = run.finished_at or datetime.utcnow()
            updates: dict[str, Any] = {
                "last_run_finished_at": finished_at,
                "last_run_status": run.status,
            }
            if run.status == "success":
                updates["last_success_at"] = finished_at
            crud.update_scheduled_plan(db, plan_id, **updates)

    @staticmethod
    def _spawn_worker(worker: Callable[[], None], worker_name: str) -> None:
        thread = Thread(target=worker, name=worker_name, daemon=True)
        thread.start()
