from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from src.database.models import Account, AccountSurvivalCheck, RegistrationTask


def classify_survival_result(
    *,
    signal_type: str | None = None,
    status_code: int | None = None,
    error_message: str | None = None,
) -> str:
    dead_signals = {"refresh_invalid", "auth_invalid", "account_expired", "account_failed", "account_banned"}
    healthy_signals = {"refresh_ok", "auth_ok", "probe_ok"}

    if signal_type in dead_signals or status_code in (401, 403):
        return "dead"
    if signal_type in healthy_signals or (status_code is not None and 200 <= int(status_code) < 300):
        return "healthy"
    if error_message:
        return "warning"
    return "warning"


def probe_account_survival(account: Any) -> dict[str, Any]:
    account_status = str(getattr(account, "status", "") or "").lower()
    if account_status == "expired":
        signal_type = "refresh_invalid"
        status_code = 401
    elif account_status in {"failed", "banned"}:
        signal_type = "auth_invalid"
        status_code = 403
    elif any(
        [
            getattr(account, "access_token", None),
            getattr(account, "refresh_token", None),
            getattr(account, "session_token", None),
        ]
    ):
        signal_type = "auth_ok"
        status_code = 200
    else:
        signal_type = "token_missing"
        status_code = None

    result_level = classify_survival_result(signal_type=signal_type, status_code=status_code)
    return {
        "result_level": result_level,
        "signal_type": signal_type,
        "status_code": status_code,
        "latency_ms": 0,
        "detail_json": {
            "account_status": account_status or None,
            "status_code": status_code,
        },
    }


def probe_claimed_account_survival(claimed_check: dict[str, Any]) -> dict[str, Any]:
    account_payload = claimed_check.get("account") or {}
    return probe_account_survival(SimpleNamespace(**account_payload))


def find_accounts_for_survival_checks(
    db: Session,
    *,
    account_id: int | None = None,
    experiment_batch_id: int | None = None,
    pipeline_key: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> list[tuple[Account, RegistrationTask | None]]:
    query = db.query(Account)
    joined_tasks = False

    if account_id is not None:
        query = query.filter(Account.id == account_id)

    if start_at is not None:
        query = query.filter(Account.registered_at >= start_at)

    if end_at is not None:
        query = query.filter(Account.registered_at <= end_at)

    if pipeline_key is not None or experiment_batch_id is not None:
        query = query.join(RegistrationTask, RegistrationTask.email_address == Account.email)
        joined_tasks = True
        if pipeline_key is not None:
            query = query.filter(RegistrationTask.pipeline_key == pipeline_key)
        if experiment_batch_id is not None:
            query = query.filter(RegistrationTask.experiment_batch_id == experiment_batch_id)

    accounts = query.distinct().order_by(Account.id.asc()).all()
    return [(account, _find_latest_task_context(db, account, joined_tasks=joined_tasks, experiment_batch_id=experiment_batch_id, pipeline_key=pipeline_key)) for account in accounts]


def summarize_survival_checks(checks: list[AccountSurvivalCheck]) -> dict[str, Any]:
    counts = {"healthy": 0, "warning": 0, "dead": 0}
    by_pipeline: dict[str, dict[str, int]] = {}

    for check in checks:
        level = str(check.result_level or "warning").lower()
        if level not in counts:
            level = "warning"
        counts[level] += 1

        pipeline = str(check.pipeline_key or "unknown")
        by_pipeline.setdefault(pipeline, {"healthy": 0, "warning": 0, "dead": 0})
        by_pipeline[pipeline][level] += 1

    total = len(checks)
    ratios = {
        key: (value / total) if total else 0.0
        for key, value in counts.items()
    }
    return {
        "total": total,
        "counts": counts,
        "ratios": ratios,
        "by_pipeline": by_pipeline,
    }


def _find_latest_task_context(
    db: Session,
    account: Account,
    *,
    joined_tasks: bool,
    experiment_batch_id: int | None,
    pipeline_key: str | None,
) -> RegistrationTask | None:
    query = db.query(RegistrationTask).filter(RegistrationTask.email_address == account.email)
    if joined_tasks or pipeline_key is not None or experiment_batch_id is not None:
        if pipeline_key is not None:
            query = query.filter(RegistrationTask.pipeline_key == pipeline_key)
        if experiment_batch_id is not None:
            query = query.filter(RegistrationTask.experiment_batch_id == experiment_batch_id)
    return query.order_by(RegistrationTask.created_at.desc(), RegistrationTask.id.desc()).first()
