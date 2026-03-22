from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc

from ...database import crud
from ...database.models import ScheduledRun
from ...database.session import get_db
from ...scheduler.schemas import (
    ScheduledPlanCreate,
    ScheduledPlanListResponse,
    ScheduledPlanResponse,
    ScheduledPlanUpdate,
    ScheduledRunResponse,
)
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


def _resolve_plan_update_payload(plan, request: ScheduledPlanUpdate) -> dict:
    return {
        "name": request.name if request.name is not None else plan.name,
        "task_type": request.task_type if request.task_type is not None else plan.task_type,
        "cpa_service_id": request.cpa_service_id if request.cpa_service_id is not None else plan.cpa_service_id,
        "trigger_type": request.trigger_type if request.trigger_type is not None else plan.trigger_type,
        "cron_expression": request.cron_expression if request.cron_expression is not None else plan.cron_expression,
        "interval_value": request.interval_value if request.interval_value is not None else plan.interval_value,
        "interval_unit": request.interval_unit if request.interval_unit is not None else plan.interval_unit,
        "config": request.config if request.config is not None else plan.config,
        "enabled": request.enabled if request.enabled is not None else plan.enabled,
    }


class _PlanLike:
    def __init__(self, payload: dict):
        self.trigger_type = payload["trigger_type"]
        self.cron_expression = payload["cron_expression"]
        self.interval_value = payload["interval_value"]
        self.interval_unit = payload["interval_unit"]


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


@router.put("/{plan_id}", response_model=ScheduledPlanResponse)
async def update_scheduled_plan(plan_id: int, request: ScheduledPlanUpdate):
    with get_db() as db:
        plan = crud.get_scheduled_plan_by_id(db, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")

        merged = _resolve_plan_update_payload(plan, request)
        try:
            validate_plan_payload(
                task_type=merged["task_type"],
                trigger_type=merged["trigger_type"],
                config=merged["config"],
                cron_expression=merged["cron_expression"],
                interval_value=merged["interval_value"],
                interval_unit=merged["interval_unit"],
            )
            if crud.get_cpa_service_by_id(db, merged["cpa_service_id"]) is None:
                raise ValueError(f"cpa service {merged['cpa_service_id']} does not exist")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        update_values = request.model_dump(exclude_unset=True)

        should_recompute = (
            merged["enabled"]
            and (
                request.enabled is True and plan.enabled is False
                or plan.next_run_at is None
                or any(
                    getattr(request, field) is not None
                    for field in ("trigger_type", "cron_expression", "interval_value", "interval_unit")
                )
            )
        )
        if merged["enabled"]:
            if should_recompute:
                update_values["next_run_at"] = _to_scheduler_naive_time(compute_next_run_at(_PlanLike(merged)))
            if request.enabled is True:
                update_values["auto_disabled_reason"] = None
        else:
            update_values["next_run_at"] = None

        updated = crud.update_scheduled_plan(db, plan_id, **update_values)
        if updated is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")
        return ScheduledPlanResponse.model_validate(updated)


@router.get("", response_model=ScheduledPlanListResponse)
async def list_scheduled_plans(
    enabled: bool | None = Query(None),
    cpa_service_id: int | None = Query(None, ge=1),
):
    with get_db() as db:
        plans = crud.get_scheduled_plans(
            db,
            enabled=enabled,
            cpa_service_id=cpa_service_id,
        )
        return ScheduledPlanListResponse(
            items=[ScheduledPlanResponse.model_validate(plan) for plan in plans],
            total=len(plans),
        )


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


@router.post("/{plan_id}/enable", response_model=ScheduledPlanResponse)
async def enable_scheduled_plan(plan_id: int):
    with get_db() as db:
        plan = crud.get_scheduled_plan_by_id(db, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")

        try:
            next_run_at = _to_scheduler_naive_time(compute_next_run_at(plan))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        updated = crud.update_scheduled_plan(
            db,
            plan_id,
            enabled=True,
            next_run_at=next_run_at,
            auto_disabled_reason=None,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")
        return ScheduledPlanResponse.model_validate(updated)


@router.post("/{plan_id}/disable", response_model=ScheduledPlanResponse)
async def disable_scheduled_plan(plan_id: int):
    with get_db() as db:
        plan = crud.get_scheduled_plan_by_id(db, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")

        updated = crud.update_scheduled_plan(
            db,
            plan_id,
            enabled=False,
            next_run_at=None,
            auto_disabled_reason=None,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")
        return ScheduledPlanResponse.model_validate(updated)


@router.post("/{plan_id}/run")
async def run_scheduled_plan(plan_id: int, request: Request):
    with get_db() as db:
        plan = crud.get_scheduled_plan_by_id(db, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="定时计划不存在")

    scheduler_engine = getattr(request.app.state, "scheduler_engine", None)
    if scheduler_engine is None:
        raise HTTPException(status_code=500, detail="scheduler engine is not initialized")

    run_id = scheduler_engine.trigger_plan_now(plan_id)
    if run_id is None:
        raise HTTPException(status_code=409, detail="计划正在运行")

    return {"success": True, "plan_id": plan_id, "run_id": int(run_id)}
