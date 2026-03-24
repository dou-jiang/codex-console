from __future__ import annotations

from collections import defaultdict
from math import floor
from typing import Any

from sqlalchemy.orm import Session

from src.database import crud
from src.database.models import (
    PipelineStepRun,
    RegistrationBatchStageStat,
    RegistrationBatchStat,
    RegistrationBatchStepStat,
)

STEP_STAGE_MAP: dict[str, str] = {
    "get_proxy_ip": "signup_prepare",
    "create_email": "signup_prepare",
    "init_signup_session": "signup_prepare",
    "send_signup_otp": "signup_otp",
    "wait_signup_otp": "signup_otp",
    "validate_signup_otp": "signup_otp",
    "create_account": "create_account",
    "init_login_session": "login_prepare",
    "submit_login_email": "login_prepare",
    "submit_login_password": "login_prepare",
    "wait_login_otp": "login_otp",
    "validate_login_otp": "login_otp",
    "select_workspace": "token_exchange",
    "exchange_oauth_token": "token_exchange",
}


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def finalize_batch_statistics(db: Session, *, batch_context: dict[str, Any]) -> RegistrationBatchStat:
    batch_id = str(batch_context.get("batch_id") or "")
    if not batch_id:
        raise ValueError("batch_context.batch_id is required")

    existing = crud.get_registration_batch_stat_by_batch_id(db, batch_id)
    if existing is not None:
        return existing

    task_uuids = [str(item) for item in batch_context.get("task_uuids") or []]
    tasks = crud.get_registration_tasks_by_uuids(db, task_uuids)
    step_rows = crud.get_pipeline_step_runs_by_task_uuids(db, task_uuids)

    finished_tasks = [task for task in tasks if str(task.status or "") in TERMINAL_STATUSES]
    success_count = sum(1 for task in finished_tasks if task.status == "completed")
    failed_count = sum(1 for task in finished_tasks if task.status == "failed")
    task_durations = [int(task.total_duration_ms) for task in finished_tasks if task.total_duration_ms is not None]

    stat = crud.create_registration_batch_stat(
        db,
        batch_id=batch_id,
        status=str(batch_context.get("status") or "completed"),
        mode=str(batch_context.get("mode") or "pipeline"),
        pipeline_key=str(batch_context.get("pipeline_key") or "current_pipeline"),
        email_service_type=batch_context.get("email_service_type"),
        email_service_id=batch_context.get("email_service_id"),
        proxy_strategy_snapshot=dict(batch_context.get("proxy_strategy_snapshot") or {}),
        config_snapshot=dict(batch_context.get("config_snapshot") or {}),
        target_count=int(batch_context.get("target_count") or 0),
        finished_count=len(finished_tasks),
        success_count=success_count,
        failed_count=failed_count,
        total_duration_ms=sum(task_durations) if task_durations else None,
        avg_duration_ms=_average(task_durations),
        started_at=batch_context.get("started_at"),
        completed_at=batch_context.get("completed_at"),
        commit=False,
    )

    for item in _build_step_stats(step_rows):
        crud.create_registration_batch_step_stat(db, batch_stat_id=stat.id, commit=False, **item)

    for item in _build_stage_stats(step_rows):
        crud.create_registration_batch_stage_stat(db, batch_stat_id=stat.id, commit=False, **item)

    db.commit()
    db.refresh(stat)
    return stat


def build_batch_stats_compare(left: RegistrationBatchStat, right: RegistrationBatchStat) -> dict[str, Any]:
    return {
        "left": _batch_stat_to_dict(left),
        "right": _batch_stat_to_dict(right),
        "summary_diff": _build_summary_diff(left, right),
        "step_diffs": _diff_step_stats(left.step_stats, right.step_stats),
        "stage_diffs": _diff_stage_stats(left.stage_stats, right.stage_stats),
    }


def _build_step_stats(step_rows: list[PipelineStepRun]) -> list[dict[str, Any]]:
    grouped: dict[str, list[PipelineStepRun]] = defaultdict(list)
    for row in step_rows:
        grouped[str(row.step_key)].append(row)

    records: list[dict[str, Any]] = []
    for step_key, rows in grouped.items():
        completed_rows = [row for row in rows if row.status == "completed"]
        durations = [int(row.duration_ms) for row in completed_rows if row.duration_ms is not None]
        records.append(
            {
                "step_key": step_key,
                "step_order": min(int(row.step_order) for row in rows),
                "sample_count": len(rows),
                "success_count": len(completed_rows),
                "avg_duration_ms": _average(durations),
                "p50_duration_ms": _percentile(durations, 0.50),
                "p90_duration_ms": _percentile(durations, 0.90),
            }
        )
    return sorted(records, key=lambda item: (int(item["step_order"]), str(item["step_key"])))


def _build_stage_stats(step_rows: list[PipelineStepRun]) -> list[dict[str, Any]]:
    grouped: dict[str, list[PipelineStepRun]] = defaultdict(list)
    for row in step_rows:
        stage_key = STEP_STAGE_MAP.get(str(row.step_key))
        if stage_key:
            grouped[stage_key].append(row)

    records: list[dict[str, Any]] = []
    for stage_key, rows in grouped.items():
        completed_rows = [row for row in rows if row.status == "completed"]
        durations = [int(row.duration_ms) for row in completed_rows if row.duration_ms is not None]
        records.append(
            {
                "stage_key": stage_key,
                "sample_count": len(rows),
                "avg_duration_ms": _average(durations),
                "p50_duration_ms": _percentile(durations, 0.50),
                "p90_duration_ms": _percentile(durations, 0.90),
            }
        )
    return sorted(records, key=lambda item: str(item["stage_key"]))


def _batch_stat_to_dict(stat: RegistrationBatchStat) -> dict[str, Any]:
    return {
        "id": stat.id,
        "batch_id": stat.batch_id,
        "status": stat.status,
        "mode": stat.mode,
        "pipeline_key": stat.pipeline_key,
        "target_count": stat.target_count,
        "finished_count": stat.finished_count,
        "success_count": stat.success_count,
        "failed_count": stat.failed_count,
        "total_duration_ms": stat.total_duration_ms,
        "avg_duration_ms": stat.avg_duration_ms,
        "started_at": stat.started_at.isoformat() if stat.started_at else None,
        "completed_at": stat.completed_at.isoformat() if stat.completed_at else None,
    }


def _build_summary_diff(left: RegistrationBatchStat, right: RegistrationBatchStat) -> dict[str, Any]:
    left_success_rate = _success_rate(left)
    right_success_rate = _success_rate(right)
    return {
        "target_count": _subtract_nullable(right.target_count, left.target_count),
        "success_count": _subtract_nullable(right.success_count, left.success_count),
        "failed_count": _subtract_nullable(right.failed_count, left.failed_count),
        "success_rate": _subtract_nullable(right_success_rate, left_success_rate),
        "total_duration_ms": _subtract_nullable(right.total_duration_ms, left.total_duration_ms),
        "avg_duration_ms": _subtract_nullable(right.avg_duration_ms, left.avg_duration_ms),
    }


def _diff_step_stats(
    left_rows: list[RegistrationBatchStepStat],
    right_rows: list[RegistrationBatchStepStat],
) -> list[dict[str, Any]]:
    left_map = {str(row.step_key): row for row in left_rows}
    right_map = {str(row.step_key): row for row in right_rows}
    keys = sorted(
        set(left_map) | set(right_map),
        key=lambda key: (
            _step_sort_order(left_map.get(key), right_map.get(key)),
            key,
        ),
    )

    result: list[dict[str, Any]] = []
    for step_key in keys:
        left_row = left_map.get(step_key)
        right_row = right_map.get(step_key)
        left_avg = left_row.avg_duration_ms if left_row else None
        right_avg = right_row.avg_duration_ms if right_row else None
        result.append(
            {
                "step_key": step_key,
                "left": _step_side_payload(left_row),
                "right": _step_side_payload(right_row),
                "delta_duration_ms": _subtract_nullable(right_avg, left_avg),
                "delta_rate": _delta_rate(right_avg, left_avg),
            }
        )

    return result


def _diff_stage_stats(
    left_rows: list[RegistrationBatchStageStat],
    right_rows: list[RegistrationBatchStageStat],
) -> list[dict[str, Any]]:
    left_map = {str(row.stage_key): row for row in left_rows}
    right_map = {str(row.stage_key): row for row in right_rows}

    result: list[dict[str, Any]] = []
    for stage_key in sorted(set(left_map) | set(right_map)):
        left_row = left_map.get(stage_key)
        right_row = right_map.get(stage_key)
        left_avg = left_row.avg_duration_ms if left_row else None
        right_avg = right_row.avg_duration_ms if right_row else None
        result.append(
            {
                "stage_key": stage_key,
                "left": _stage_side_payload(left_row),
                "right": _stage_side_payload(right_row),
                "delta_duration_ms": _subtract_nullable(right_avg, left_avg),
                "delta_rate": _delta_rate(right_avg, left_avg),
            }
        )
    return result


def _step_side_payload(row: RegistrationBatchStepStat | None) -> dict[str, Any]:
    if row is None:
        return {
            "sample_count": None,
            "success_count": None,
            "avg_duration_ms": None,
            "p50_duration_ms": None,
            "p90_duration_ms": None,
        }
    return {
        "sample_count": row.sample_count,
        "success_count": row.success_count,
        "avg_duration_ms": row.avg_duration_ms,
        "p50_duration_ms": row.p50_duration_ms,
        "p90_duration_ms": row.p90_duration_ms,
    }


def _stage_side_payload(row: RegistrationBatchStageStat | None) -> dict[str, Any]:
    if row is None:
        return {
            "sample_count": None,
            "avg_duration_ms": None,
            "p50_duration_ms": None,
            "p90_duration_ms": None,
        }
    return {
        "sample_count": row.sample_count,
        "avg_duration_ms": row.avg_duration_ms,
        "p50_duration_ms": row.p50_duration_ms,
        "p90_duration_ms": row.p90_duration_ms,
    }


def _step_sort_order(left: RegistrationBatchStepStat | None, right: RegistrationBatchStepStat | None) -> int:
    for row in (left, right):
        if row is not None and row.step_order is not None:
            return int(row.step_order)
    return 9999


def _success_rate(stat: RegistrationBatchStat) -> float:
    finished = int(stat.finished_count or 0)
    if finished <= 0:
        return 0.0
    return float((stat.success_count or 0) / finished)


def _average(values: list[int]) -> float | None:
    if not values:
        return None
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


def _subtract_nullable(left: float | int | None, right: float | int | None) -> float | int | None:
    if left is None or right is None:
        return None
    if isinstance(left, int) and isinstance(right, int):
        return int(left - right)
    return float(left - right)


def _delta_rate(right: float | None, left: float | None) -> float | None:
    if right is None or left is None or left == 0:
        return None
    return float((right - left) / left)
