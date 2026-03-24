from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.registration_batch_stats import STAGE_ORDER, build_batch_stats_compare
from ...database import crud
from ...database.models import (
    RegistrationBatchStageStat,
    RegistrationBatchStat,
    RegistrationBatchStepStat,
)
from ...database.session import get_db

router = APIRouter()

_STAGE_ORDER_INDEX: dict[str, int] = {key: index for index, key in enumerate(STAGE_ORDER)}


class BatchStatCompareRequest(BaseModel):
    left_id: int = Field(..., ge=1)
    right_id: int = Field(..., ge=1)


def _format_datetime(value) -> str | None:
    return value.isoformat() if value else None


def _serialize_batch_stat(stat: RegistrationBatchStat) -> dict[str, Any]:
    return {
        "id": stat.id,
        "batch_id": stat.batch_id,
        "status": stat.status,
        "mode": stat.mode,
        "pipeline_key": stat.pipeline_key,
        "email_service_type": stat.email_service_type,
        "email_service_id": stat.email_service_id,
        "proxy_strategy_snapshot": stat.proxy_strategy_snapshot,
        "config_snapshot": stat.config_snapshot,
        "target_count": stat.target_count,
        "finished_count": stat.finished_count,
        "success_count": stat.success_count,
        "failed_count": stat.failed_count,
        "total_duration_ms": stat.total_duration_ms,
        "avg_duration_ms": stat.avg_duration_ms,
        "started_at": _format_datetime(stat.started_at),
        "completed_at": _format_datetime(stat.completed_at),
        "created_at": _format_datetime(stat.created_at),
    }


def _step_sort_key(row: RegistrationBatchStepStat) -> tuple[int, str, int]:
    order_value = int(row.step_order) if row.step_order is not None else 9999
    return (order_value, str(row.step_key), int(row.id or 0))


def _stage_sort_key(row: RegistrationBatchStageStat) -> tuple[int, str, int]:
    stage_key = str(row.stage_key)
    return (_STAGE_ORDER_INDEX.get(stage_key, len(STAGE_ORDER)), stage_key, int(row.id or 0))


def _serialize_step_stat(row: RegistrationBatchStepStat) -> dict[str, Any]:
    return {
        "step_key": row.step_key,
        "step_order": row.step_order,
        "sample_count": row.sample_count,
        "success_count": row.success_count,
        "avg_duration_ms": row.avg_duration_ms,
        "p50_duration_ms": row.p50_duration_ms,
        "p90_duration_ms": row.p90_duration_ms,
    }


def _serialize_stage_stat(row: RegistrationBatchStageStat) -> dict[str, Any]:
    return {
        "stage_key": row.stage_key,
        "sample_count": row.sample_count,
        "avg_duration_ms": row.avg_duration_ms,
        "p50_duration_ms": row.p50_duration_ms,
        "p90_duration_ms": row.p90_duration_ms,
    }


@router.get("")
async def list_registration_batch_stats(
    status: str | None = Query(None),
    pipeline_key: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    with get_db() as db:
        total = crud.count_registration_batch_stats(
            db,
            status=status,
            pipeline_key=pipeline_key,
        )
        stats = crud.list_registration_batch_stats(
            db,
            status=status,
            pipeline_key=pipeline_key,
            offset=offset,
            limit=limit,
        )

        return {
            "total": total,
            "items": [_serialize_batch_stat(stat) for stat in stats],
        }


@router.get("/{stat_id}")
async def get_registration_batch_stat(stat_id: int):
    with get_db() as db:
        stat = crud.get_registration_batch_stat_by_id(db, stat_id)
        if stat is None:
            raise HTTPException(status_code=404, detail="批量统计不存在")

        step_stats = sorted(stat.step_stats, key=_step_sort_key)
        stage_stats = sorted(stat.stage_stats, key=_stage_sort_key)

        payload = _serialize_batch_stat(stat)
        payload["step_stats"] = [_serialize_step_stat(row) for row in step_stats]
        payload["stage_stats"] = [_serialize_stage_stat(row) for row in stage_stats]
        return payload


@router.post("/compare")
async def compare_registration_batch_stats(request: BatchStatCompareRequest):
    with get_db() as db:
        left = crud.get_registration_batch_stat_by_id(db, request.left_id)
        right = crud.get_registration_batch_stat_by_id(db, request.right_id)
        if left is None or right is None:
            raise HTTPException(status_code=404, detail="批量统计不存在")
        return build_batch_stats_compare(left, right)
