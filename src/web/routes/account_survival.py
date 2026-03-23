from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ...core.account_survival import (
    find_accounts_for_survival_checks,
    probe_account_survival,
    summarize_survival_checks,
)
from ...database import crud
from ...database.models import AccountSurvivalCheck
from ...database.session import get_db

router = APIRouter()


class SurvivalRunRequest(BaseModel):
    account_id: int | None = None
    experiment_batch_id: int | None = None
    pipeline_key: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    check_stage: str = "manual"


@router.post("/survival-checks/run")
async def run_survival_checks(request: SurvivalRunRequest):
    scheduled = 0
    with get_db() as db:
        for account, task in find_accounts_for_survival_checks(
            db,
            account_id=request.account_id,
            experiment_batch_id=request.experiment_batch_id,
            pipeline_key=request.pipeline_key,
            start_at=request.start_at,
            end_at=request.end_at,
        ):
            result = probe_account_survival(account)
            crud.create_account_survival_check(
                db,
                account_id=account.id,
                task_uuid=task.task_uuid if task else None,
                pipeline_key=(task.pipeline_key if task else request.pipeline_key),
                experiment_batch_id=(task.experiment_batch_id if task else request.experiment_batch_id),
                check_source="manual",
                check_stage=request.check_stage,
                result_level=result["result_level"],
                signal_type=result.get("signal_type"),
                latency_ms=result.get("latency_ms"),
                detail_json=result.get("detail_json"),
            )
            scheduled += 1

    return {"scheduled": scheduled}


@router.get("/survival-checks")
async def list_survival_checks(
    account_id: int | None = None,
    experiment_batch_id: int | None = None,
    pipeline_key: str | None = None,
):
    with get_db() as db:
        rows = _query_survival_checks(
            db,
            account_id=account_id,
            experiment_batch_id=experiment_batch_id,
            pipeline_key=pipeline_key,
        )
        return {
            "total": len(rows),
            "items": [_survival_check_to_dict(item) for item in rows],
        }


@router.get("/survival-summary")
async def get_survival_summary(
    account_id: int | None = None,
    experiment_batch_id: int | None = None,
    pipeline_key: str | None = None,
):
    with get_db() as db:
        rows = _query_survival_checks(
            db,
            account_id=account_id,
            experiment_batch_id=experiment_batch_id,
            pipeline_key=pipeline_key,
        )
        return summarize_survival_checks(rows)


def _query_survival_checks(
    db,
    *,
    account_id: int | None,
    experiment_batch_id: int | None,
    pipeline_key: str | None,
) -> list[AccountSurvivalCheck]:
    query = db.query(AccountSurvivalCheck).order_by(AccountSurvivalCheck.checked_at.desc(), AccountSurvivalCheck.id.desc())
    if account_id is not None:
        query = query.filter(AccountSurvivalCheck.account_id == account_id)
    if experiment_batch_id is not None:
        query = query.filter(AccountSurvivalCheck.experiment_batch_id == experiment_batch_id)
    if pipeline_key is not None:
        query = query.filter(AccountSurvivalCheck.pipeline_key == pipeline_key)
    return query.all()


def _survival_check_to_dict(row: AccountSurvivalCheck) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "task_uuid": row.task_uuid,
        "pipeline_key": row.pipeline_key,
        "experiment_batch_id": row.experiment_batch_id,
        "check_source": row.check_source,
        "check_stage": row.check_stage,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "result_level": row.result_level,
        "signal_type": row.signal_type,
        "latency_ms": row.latency_ms,
        "detail_json": row.detail_json or {},
    }
