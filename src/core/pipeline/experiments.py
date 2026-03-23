from __future__ import annotations

import uuid
from collections import defaultdict
from math import floor
from typing import Any

from sqlalchemy.orm import Session

from src.database import crud
from src.database.models import AccountSurvivalCheck, ExperimentBatch, PipelineStepRun, RegistrationTask

PIPELINE_KEYS: tuple[str, str] = ("current_pipeline", "codexgen_pipeline")


def build_pairs(count: int) -> list[tuple[str, str]]:
    safe_count = max(0, int(count))
    return [
        (f"pair-{index:04d}", pipeline_key)
        for index in range(1, safe_count + 1)
        for pipeline_key in PIPELINE_KEYS
    ]


def create_experiment_tasks(
    db: Session,
    *,
    count: int,
    email_service_type: str,
    email_service_id: int | None,
    email_service_config: dict[str, Any] | None,
    proxy_strategy: dict[str, Any] | None,
    concurrency: int,
    mode: str,
) -> tuple[ExperimentBatch, list[RegistrationTask]]:
    batch = crud.create_experiment_batch(
        db,
        name=f"experiment-{uuid.uuid4().hex[:8]}",
        mode=mode,
        pipelines=",".join(PIPELINE_KEYS),
        email_service_type=email_service_type,
        email_service_config_snapshot=dict(email_service_config or {}),
        proxy_strategy_snapshot=dict(proxy_strategy or {}),
        target_count=int(count),
        notes=f"email_service_id={email_service_id}, concurrency={int(concurrency)}",
        status="pending",
    )

    tasks: list[RegistrationTask] = []
    for pair_key, pipeline_key in build_pairs(count):
        task = crud.create_registration_task(
            db,
            task_uuid=str(uuid.uuid4()),
            email_service_id=email_service_id,
            pipeline_key=pipeline_key,
        )
        task = crud.update_registration_task(
            db,
            task.task_uuid,
            pair_key=pair_key,
            experiment_batch_id=batch.id,
        ) or task
        tasks.append(task)

    return batch, tasks


def build_experiment_overview(
    db: Session,
    *,
    batch: ExperimentBatch,
    status_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks = _get_experiment_tasks(db, batch.id)
    status = str((status_override or {}).get("status") or batch.status or "pending")

    per_pipeline: dict[str, dict[str, Any]] = {}
    all_durations: list[int] = []
    for pipeline_key in PIPELINE_KEYS:
        pipeline_tasks = [task for task in tasks if task.pipeline_key == pipeline_key]
        finished = [task for task in pipeline_tasks if task.status in ("completed", "failed")]
        successes = [task for task in finished if task.status == "completed"]
        durations = [int(task.total_duration_ms) for task in pipeline_tasks if task.total_duration_ms is not None]
        all_durations.extend(durations)
        per_pipeline[pipeline_key] = {
            "total_tasks": len(pipeline_tasks),
            "finished_tasks": len(finished),
            "success_count": len(successes),
            "success_rate": (len(successes) / len(finished)) if finished else 0.0,
            "avg_duration_ms": _average(durations),
        }

    survival_summary = _build_survival_summary(db, batch.id)
    return {
        "id": batch.id,
        "name": batch.name,
        "status": status,
        "mode": batch.mode,
        "pipelines": per_pipeline,
        "total_pairs": int(batch.target_count or 0),
        "total_tasks": len(tasks),
        "avg_duration_ms": _average(all_durations),
        "survival_summary": survival_summary,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
    }


def build_experiment_pairs(db: Session, *, batch: ExperimentBatch) -> list[dict[str, Any]]:
    tasks = _get_experiment_tasks(db, batch.id)
    grouped: dict[str, dict[str, Any]] = {}

    for task in tasks:
        pair_key = str(task.pair_key or "")
        group = grouped.setdefault(
            pair_key,
            {
                "pair_key": pair_key,
                "tasks": {},
            },
        )
        group["tasks"][str(task.pipeline_key or "")] = {
            "task_uuid": task.task_uuid,
            "status": task.status,
            "pipeline_status": task.pipeline_status,
            "total_duration_ms": task.total_duration_ms,
            "error_message": task.error_message,
            "email_address": task.email_address,
        }

    return [grouped[key] for key in sorted(grouped)]


def build_experiment_step_summary(db: Session, *, batch: ExperimentBatch) -> list[dict[str, Any]]:
    tasks = _get_experiment_tasks(db, batch.id)
    task_uuids = [task.task_uuid for task in tasks]
    if not task_uuids:
        return []

    step_rows = (
        db.query(PipelineStepRun)
        .filter(PipelineStepRun.task_uuid.in_(task_uuids))
        .order_by(PipelineStepRun.step_order.asc(), PipelineStepRun.id.asc())
        .all()
    )
    if not step_rows:
        return []

    grouped: dict[str, dict[str, list[PipelineStepRun]]] = defaultdict(lambda: defaultdict(list))
    for row in step_rows:
        grouped[row.step_key][row.pipeline_key].append(row)

    result: list[dict[str, Any]] = []
    for step_key in sorted(grouped):
        pipeline_stats: dict[str, dict[str, Any]] = {}
        for pipeline_key in PIPELINE_KEYS:
            rows = grouped[step_key].get(pipeline_key, [])
            completed_rows = [row for row in rows if row.status == "completed"]
            durations = [int(row.duration_ms) for row in completed_rows if row.duration_ms is not None]
            pipeline_stats[pipeline_key] = {
                "count": len(rows),
                "success_rate": (len(completed_rows) / len(rows)) if rows else 0.0,
                "avg_duration_ms": _average(durations),
                "p50_duration_ms": _percentile(durations, 0.50),
                "p90_duration_ms": _percentile(durations, 0.90),
            }

        current_avg = pipeline_stats["current_pipeline"]["avg_duration_ms"]
        codexgen_avg = pipeline_stats["codexgen_pipeline"]["avg_duration_ms"]
        current_success = pipeline_stats["current_pipeline"]["success_rate"]
        codexgen_success = pipeline_stats["codexgen_pipeline"]["success_rate"]
        result.append(
            {
                "step_key": step_key,
                "pipelines": pipeline_stats,
                "pipeline_diff": {
                    "avg_duration_ms": _subtract_nullable(codexgen_avg, current_avg),
                    "success_rate": _subtract_nullable(codexgen_success, current_success),
                },
            }
        )

    return result


def _get_experiment_tasks(db: Session, experiment_batch_id: int) -> list[RegistrationTask]:
    return (
        db.query(RegistrationTask)
        .filter(RegistrationTask.experiment_batch_id == experiment_batch_id)
        .order_by(RegistrationTask.pair_key.asc(), RegistrationTask.pipeline_key.asc(), RegistrationTask.id.asc())
        .all()
    )


def _build_survival_summary(db: Session, experiment_batch_id: int) -> dict[str, int]:
    checks = (
        db.query(AccountSurvivalCheck)
        .filter(AccountSurvivalCheck.experiment_batch_id == experiment_batch_id)
        .all()
    )
    summary = {"total": len(checks), "active": 0, "expired": 0, "unknown": 0}
    for check in checks:
        level = str(check.result_level or "").lower()
        if level in ("active", "expired", "unknown"):
            summary[level] += 1
        else:
            summary["unknown"] += 1
    return summary


def _average(values: list[int]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return int(sorted_values[0])

    rank = (len(sorted_values) - 1) * percentile
    lower_index = floor(rank)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    if lower_index == upper_index:
        return int(lower_value)
    weight = rank - lower_index
    return int(round(lower_value + (upper_value - lower_value) * weight))


def _subtract_nullable(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return float(left - right)
