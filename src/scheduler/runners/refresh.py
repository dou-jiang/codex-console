from __future__ import annotations

from datetime import datetime
from typing import Any

from ...core.openai.payment import check_subscription_status
from ...core.openai.token_refresh import refresh_account_token
from ...database import crud
from ...database.session import get_db
from ..run_logger import append_run_log, finalize_run
from .refill import upload_account_to_bound_cpa


DEFAULT_REFRESH_AFTER_DAYS = 7
DEFAULT_MAX_REFRESH_COUNT = 100


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def run_refresh_plan(*, plan_id: int, run_id: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "eligible_accounts": 0,
        "processed": 0,
        "refreshed_success": 0,
        "refresh_failed": 0,
        "subscription_failed": 0,
        "upload_attempted": 0,
        "uploaded_success": 0,
        "uploaded_failed": 0,
    }

    try:
        with get_db() as db:
            plan = crud.get_scheduled_plan_by_id(db, plan_id)
            if plan is None:
                raise ValueError(f"scheduled plan {plan_id} does not exist")
            if plan.task_type != "account_refresh":
                raise ValueError(f"plan {plan_id} is not account_refresh")

            config = plan.config or {}
            refresh_after_days = max(
                0,
                _parse_int(config.get("refresh_after_days"), DEFAULT_REFRESH_AFTER_DAYS),
            )
            max_refresh_count = max(
                0,
                _parse_int(config.get("max_refresh_count"), DEFAULT_MAX_REFRESH_COUNT),
            )
            proxy = config.get("proxy")
            cpa_service_id = plan.cpa_service_id

            due_accounts = crud.get_due_refresh_accounts(
                db,
                cpa_service_id=cpa_service_id,
                refresh_after_days=refresh_after_days,
                limit=max_refresh_count,
            )
            due_account_ids = [item.id for item in due_accounts]

        summary["eligible_accounts"] = len(due_account_ids)
        append_run_log(
            run_id,
            (
                "refresh runner start "
                f"(plan_id={plan_id}, eligible={summary['eligible_accounts']}, "
                f"refresh_after_days={refresh_after_days})"
            ),
        )

        for account_id in due_account_ids:
            summary["processed"] += 1

            refresh_result = refresh_account_token(account_id, proxy_url=proxy)
            if not refresh_result.success:
                with get_db() as db:
                    crud.mark_account_expired(db, account_id, reason="refresh_failed")
                summary["refresh_failed"] += 1
                append_run_log(
                    run_id,
                    (
                        "token refresh failed "
                        f"(account_id={account_id}): "
                        f"{getattr(refresh_result, 'error_message', 'unknown error')}"
                    ),
                )
                continue

            summary["refreshed_success"] += 1

            with get_db() as db:
                account = crud.get_account_by_id(db, account_id)
                if account is None:
                    append_run_log(run_id, f"account missing after refresh (account_id={account_id})")
                    continue

                if getattr(refresh_result, "access_token", None):
                    account.access_token = refresh_result.access_token
                if getattr(refresh_result, "refresh_token", None):
                    account.refresh_token = refresh_result.refresh_token
                if getattr(refresh_result, "expires_at", None):
                    account.expires_at = refresh_result.expires_at
                account.last_refresh = datetime.utcnow()
                account.status = "active"
                account.invalidated_at = None
                account.invalid_reason = None
                db.commit()
                db.refresh(account)

                try:
                    subscription = check_subscription_status(account, proxy)
                except Exception as exc:
                    crud.mark_account_expired(db, account_id, reason="subscription_check_failed")
                    summary["subscription_failed"] += 1
                    append_run_log(
                        run_id,
                        f"subscription check failed (account_id={account_id}): {exc}",
                    )
                    continue

                account.subscription_type = subscription
                account.subscription_at = datetime.utcnow()
                db.commit()

                summary["upload_attempted"] += 1
                upload_ok, upload_message = upload_account_to_bound_cpa(
                    db=db,
                    account_id=account_id,
                    cpa_service_id=cpa_service_id,
                )

                if upload_ok:
                    crud.mark_account_active_for_cpa(db, account_id, cpa_service_id)
                    summary["uploaded_success"] += 1
                    append_run_log(
                        run_id,
                        f"refresh/upload succeeded (account_id={account_id}, subscription={subscription})",
                    )
                else:
                    account = crud.get_account_by_id(db, account_id)
                    if account is not None:
                        account.status = "active"
                        account.primary_cpa_service_id = cpa_service_id
                        account.cpa_uploaded = False
                        account.invalidated_at = None
                        account.invalid_reason = None
                        db.commit()
                    summary["uploaded_failed"] += 1
                    append_run_log(
                        run_id,
                        f"cpa upload failed (account_id={account_id}): {upload_message}",
                    )

        append_run_log(
            run_id,
            (
                "refresh runner complete "
                f"(processed={summary['processed']}, refresh_failed={summary['refresh_failed']}, "
                f"subscription_failed={summary['subscription_failed']}, "
                f"uploaded_success={summary['uploaded_success']}, "
                f"uploaded_failed={summary['uploaded_failed']})"
            ),
        )
        finalize_run(run_id, status="success", summary=summary)
        return summary

    except Exception as exc:
        append_run_log(run_id, f"refresh runner failed: {exc}")
        finalize_run(run_id, status="failed", summary=summary, error_message=str(exc))
        raise
