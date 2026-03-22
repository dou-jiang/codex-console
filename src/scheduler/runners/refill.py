from __future__ import annotations

from datetime import datetime
from typing import Any

from ...core.registration_job import run_registration_job
from ...core.upload.cpa_upload import generate_token_json, upload_to_cpa
from ...database import crud
from ...database.session import get_db
from ..cpa_client import count_valid_accounts
from ..run_logger import append_run_log, finalize_run


AUTO_DISABLE_REASON = "consecutive_failures_reached"


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_refill_target(*, current_valid: int, target_valid: int, max_refill_count: int) -> int:
    needed = max(0, target_valid - current_valid)
    if max_refill_count > 0:
        return min(needed, max_refill_count)
    return needed


def upload_account_to_bound_cpa(*, db, account_id: int, cpa_service_id: int) -> tuple[bool, str]:
    account = crud.get_account_by_id(db, account_id)
    if not account:
        return False, "账号不存在"

    if not account.access_token:
        return False, "账号缺少 Token"

    service = crud.get_cpa_service_by_id(db, cpa_service_id)
    if not service:
        return False, "CPA 服务不存在"

    token_data = generate_token_json(account)
    ok, message = upload_to_cpa(token_data, api_url=service.api_url, api_token=service.api_token)
    if not ok:
        return False, message

    account.cpa_uploaded = True
    account.cpa_uploaded_at = datetime.utcnow()
    account.primary_cpa_service_id = cpa_service_id
    db.commit()
    return True, message


def run_refill_plan(*, plan_id: int, run_id: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "current_valid_count": 0,
        "target_valid_count": 0,
        "refill_target": 0,
        "registered_success": 0,
        "registered_failed": 0,
        "upload_attempted": 0,
        "uploaded_success": 0,
        "uploaded_failed": 0,
        "consecutive_failures": 0,
        "auto_disabled": False,
    }

    try:
        with get_db() as db:
            plan = crud.get_scheduled_plan_by_id(db, plan_id)
            if plan is None:
                raise ValueError(f"scheduled plan {plan_id} does not exist")
            if plan.task_type != "cpa_refill":
                raise ValueError(f"plan {plan_id} is not cpa_refill")

            service = crud.get_cpa_service_by_id(db, plan.cpa_service_id)
            if service is None:
                raise ValueError(f"cpa service {plan.cpa_service_id} does not exist")

            config = plan.config or {}
            target_valid_count = max(0, _parse_int(config.get("target_valid_count"), 0))
            max_refill_count = max(0, _parse_int(config.get("max_refill_count"), 0))
            max_consecutive_failures = max(1, _parse_int(config.get("max_consecutive_failures"), 1))
            email_service_type = str(config.get("email_service_type") or "tempmail")
            email_service_id = config.get("email_service_id")
            email_service_config = config.get("email_service_config")
            proxy = config.get("proxy")

            service_payload = {
                "id": service.id,
                "api_url": service.api_url,
                "api_token": service.api_token,
            }
            cpa_service_id = plan.cpa_service_id

        append_run_log(run_id, f"refill runner start (plan_id={plan_id})")

        current_valid_count = count_valid_accounts(service_payload)
        refill_target = _resolve_refill_target(
            current_valid=max(0, int(current_valid_count)),
            target_valid=target_valid_count,
            max_refill_count=max_refill_count,
        )

        summary["current_valid_count"] = max(0, int(current_valid_count))
        summary["target_valid_count"] = target_valid_count
        summary["refill_target"] = refill_target

        append_run_log(
            run_id,
            (
                "refill target resolved "
                f"(current={summary['current_valid_count']}, target={target_valid_count}, refill_target={refill_target})"
            ),
        )

        consecutive_failures = 0
        while summary["uploaded_success"] < refill_target:
            with get_db() as db:
                job = run_registration_job(
                    db=db,
                    email_service_type=email_service_type,
                    email_service_id=email_service_id,
                    proxy=proxy,
                    email_service_config=email_service_config,
                    auto_upload=False,
                )

                if not job.success:
                    summary["registered_failed"] += 1
                    consecutive_failures += 1
                    summary["consecutive_failures"] = consecutive_failures
                    append_run_log(run_id, f"registration failed: {job.error_message or 'unknown error'}")
                elif not job.account_id:
                    summary["registered_failed"] += 1
                    consecutive_failures += 1
                    summary["consecutive_failures"] = consecutive_failures
                    append_run_log(run_id, "registration returned no account_id")
                else:
                    summary["registered_success"] += 1
                    summary["upload_attempted"] += 1
                    ok, message = upload_account_to_bound_cpa(
                        db=db,
                        account_id=job.account_id,
                        cpa_service_id=cpa_service_id,
                    )
                    if ok:
                        summary["uploaded_success"] += 1
                        consecutive_failures = 0
                        summary["consecutive_failures"] = 0
                        append_run_log(
                            run_id,
                            f"uploaded account to bound cpa (account_id={job.account_id})",
                        )
                    else:
                        summary["uploaded_failed"] += 1
                        consecutive_failures += 1
                        summary["consecutive_failures"] = consecutive_failures
                        append_run_log(
                            run_id,
                            f"upload failed (account_id={job.account_id}): {message}",
                        )

            if consecutive_failures >= max_consecutive_failures:
                with get_db() as db:
                    crud.disable_scheduled_plan(
                        db,
                        plan_id=plan_id,
                        reason=AUTO_DISABLE_REASON,
                    )
                summary["auto_disabled"] = True
                append_run_log(
                    run_id,
                    f"refill plan auto-disabled: {AUTO_DISABLE_REASON}",
                )
                break

        append_run_log(
            run_id,
            (
                "refill runner complete "
                f"(uploaded_success={summary['uploaded_success']}, "
                f"registered_failed={summary['registered_failed']}, "
                f"auto_disabled={summary['auto_disabled']})"
            ),
        )
        target_met = summary["uploaded_success"] >= refill_target
        run_status = "success" if target_met else "failed"
        run_error_message = None
        if run_status == "failed":
            if summary["auto_disabled"]:
                run_error_message = "refill plan auto-disabled before target reached"
            else:
                run_error_message = "refill target not reached"

        finalize_run(
            run_id,
            status=run_status,
            summary=summary,
            error_message=run_error_message,
        )
        return summary

    except Exception as exc:
        append_run_log(run_id, f"refill runner failed: {exc}")
        finalize_run(run_id, status="failed", summary=summary, error_message=str(exc))
        raise
