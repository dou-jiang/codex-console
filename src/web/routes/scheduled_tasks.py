from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc

from ...database import crud
from ...database.models import ScheduledRun
from ...database.session import get_db
from ...scheduler.schemas import ScheduledPlanCreate, ScheduledPlanResponse, ScheduledRunResponse
from ...scheduler.service import validate_plan_payload
from ...scheduler.time_utils import SCHEDULER_TZ, compute_next_run_at


router = APIRouter()


class ScheduledRunListResponse(BaseModel):
    runs: list[ScheduledRunResponse]


class ScheduledRunLogsResponse(BaseModel):
    run_id: int
    logs: str


def _to_scheduler_naive_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(SCHEDULER_TZ).replace(tzinfo=None)


@router.post("", response_model=ScheduledPlanResponse)
async def create_scheduled_plan(request: ScheduledPlanCreate):
    try:
        validate_plan_payload(
            task_type=request.task_type,
            trigger_type=request.trigger_type,
            config=request.config,
            cron_expression=request.cron_expression,
            interval_value=request.interval_value,
            interval_unit=request.interval_unit,
        )
        next_run_at = _to_scheduler_naive_time(compute_next_run_at(request)) if request.enabled else None

        with get_db() as db:
            plan = crud.create_scheduled_plan(
                db,
                name=request.name,
                task_type=request.task_type,
                cpa_service_id=request.cpa_service_id,
                trigger_type=request.trigger_type,
                cron_expression=request.cron_expression,
                interval_value=request.interval_value,
                interval_unit=request.interval_unit,
                config=request.config,
                enabled=request.enabled,
                next_run_at=next_run_at,
            )
            return ScheduledPlanResponse.model_validate(plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{plan_id}/runs", response_model=ScheduledRunListResponse)
async def list_scheduled_runs(plan_id: int, limit: int = Query(20, ge=1, le=200)):
    with get_db() as db:
        plan = crud.get_scheduled_plan_by_id(db, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")

        runs = (
            db.query(ScheduledRun)
            .filter(ScheduledRun.plan_id == plan_id)
            .order_by(desc(ScheduledRun.started_at), desc(ScheduledRun.id))
            .limit(limit)
            .all()
        )
        return ScheduledRunListResponse(runs=[ScheduledRunResponse.model_validate(run) for run in runs])


@router.get("/runs/{run_id}/logs", response_model=ScheduledRunLogsResponse)
async def get_scheduled_run_logs(run_id: int):
    with get_db() as db:
        run = db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")

        return ScheduledRunLogsResponse(run_id=run.id, logs=run.logs or "")


@router.post("/{plan_id}/run")
async def run_scheduled_plan(plan_id: int, request: Request):
    with get_db() as db:
        plan = crud.get_scheduled_plan_by_id(db, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")

    scheduler_engine = getattr(request.app.state, "scheduler_engine", None)
    if scheduler_engine is None:
        raise HTTPException(status_code=500, detail="scheduler engine is not initialized")

    triggered = bool(scheduler_engine.trigger_plan_now(plan_id))
    if not triggered:
        raise HTTPException(status_code=409, detail="计划正在运行")

    return {"success": True, "plan_id": plan_id}
