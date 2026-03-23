from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...core.pipeline.experiments import (
    PIPELINE_KEYS,
    build_experiment_overview,
    build_experiment_pairs,
    build_experiment_step_summary,
    create_experiment_tasks,
)
from ...database.session import get_db
from ...database.models import ExperimentBatch
from ..task_manager import task_manager

router = APIRouter()


class ExperimentCreateRequest(BaseModel):
    count: int = Field(..., ge=1)
    email_service_type: str
    email_service_id: int | None = None
    email_service_config: dict[str, Any] | None = None
    proxy_strategy: dict[str, Any] | None = None
    concurrency: int = Field(default=1, ge=1, le=50)
    mode: str = "parallel"


@router.post("")
async def create_registration_experiment(request: ExperimentCreateRequest):
    with get_db() as db:
        batch, tasks = create_experiment_tasks(
            db,
            count=request.count,
            email_service_type=request.email_service_type,
            email_service_id=request.email_service_id,
            email_service_config=request.email_service_config,
            proxy_strategy=request.proxy_strategy,
            concurrency=request.concurrency,
            mode=request.mode,
        )

    task_manager.init_experiment(
        batch.id,
        status=batch.status,
        total_pairs=request.count,
        total_tasks=len(tasks),
        pipelines=list(PIPELINE_KEYS),
    )

    return {
        "id": batch.id,
        "status": batch.status,
        "count": request.count,
        "total_pairs": request.count,
        "total_tasks": len(tasks),
        "pipelines": list(PIPELINE_KEYS),
        "tasks": [
            {
                "task_uuid": task.task_uuid,
                "pair_key": task.pair_key,
                "pipeline_key": task.pipeline_key,
                "experiment_batch_id": task.experiment_batch_id,
                "status": task.status,
            }
            for task in tasks
        ],
    }


@router.get("/{experiment_id}")
async def get_registration_experiment(experiment_id: int):
    with get_db() as db:
        batch = _get_batch_or_404(db, experiment_id)
        return build_experiment_overview(db, batch=batch)


@router.get("/{experiment_id}/pairs")
async def get_registration_experiment_pairs(experiment_id: int):
    with get_db() as db:
        batch = _get_batch_or_404(db, experiment_id)
        return {
            "experiment_id": batch.id,
            "pairs": build_experiment_pairs(db, batch=batch),
        }


@router.get("/{experiment_id}/steps")
async def get_registration_experiment_steps(experiment_id: int):
    with get_db() as db:
        batch = _get_batch_or_404(db, experiment_id)
        return {
            "experiment_id": batch.id,
            "steps": build_experiment_step_summary(db, batch=batch),
        }


def _get_batch_or_404(db, experiment_id: int) -> ExperimentBatch:
    batch = db.query(ExperimentBatch).filter(ExperimentBatch.id == experiment_id).first()
    if batch is None:
        raise HTTPException(status_code=404, detail="实验批次不存在")
    return batch
