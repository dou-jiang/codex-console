from __future__ import annotations

import re
from typing import Any

from ...database import crud
from ...database.models import ScheduledPlan
from ...database.session import get_db
from ..cpa_client import delete_invalid_accounts, probe_invalid_accounts
from ..engine import ScheduledRunCancelledError, is_run_stop_requested
from ..run_logger import append_run_log, finalize_cancelled_run, finalize_run, raise_if_stop_requested


_PROBE_PROGRESS_PATTERN = re.compile(r"scanned=(\d+)(?:/\d+)?, invalid=(\d+)")
_PROGRESS_EVERY = 100


def _resolve_max_cleanup_count(config: dict[str, Any]) -> int:
    raw_value = config.get("max_cleanup_count", 0)
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _resolve_max_probe_count(config: dict[str, Any]) -> int:
    raw_value = config.get("max_probe_count", 0)
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def run_cleanup_plan(*, plan_id: int, run_id: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "invalid_items_found": 0,
        "invalid_items_considered": 0,
        "local_marked_expired": 0,
        "remote_deleted": 0,
        "remote_delete_failed": 0,
    }

    try:
        with get_db() as db:
            plan = db.query(ScheduledPlan).filter(ScheduledPlan.id == plan_id).first()
            if plan is None:
                raise ValueError(f"scheduled plan {plan_id} does not exist")
            if plan.task_type != "cpa_cleanup":
                raise ValueError(f"plan {plan_id} is not cpa_cleanup")

            service = crud.get_cpa_service_by_id(db, plan.cpa_service_id)
            if service is None:
                raise ValueError(f"cpa service {plan.cpa_service_id} does not exist")

            service_payload = {
                "id": service.id,
                "api_url": service.api_url,
                "api_token": service.api_token,
            }
            cpa_service_id = plan.cpa_service_id
            max_cleanup_count = _resolve_max_cleanup_count(plan.config or {})
            max_probe_count = _resolve_max_probe_count(plan.config or {})

        append_run_log(run_id, f"cleanup runner start (plan_id={plan_id})")
        append_run_log(
            run_id,
            (
                "cleanup probe config "
                f"(max_probe_count={max_probe_count or 'unlimited'}, "
                f"max_cleanup_count={max_cleanup_count or 'unlimited'})"
            ),
        )

        next_probe_progress = _PROGRESS_EVERY

        def _on_probe_progress(message: str) -> None:
            nonlocal next_probe_progress
            append_run_log(run_id, message)

            match = _PROBE_PROGRESS_PATTERN.search(message)
            if match:
                scanned = int(match.group(1))
                invalid = int(match.group(2))
                while scanned >= next_probe_progress:
                    append_run_log(
                        run_id,
                        f"cleanup probe progress (scanned={next_probe_progress}, invalid={invalid})",
                    )
                    next_probe_progress += _PROGRESS_EVERY

            raise_if_stop_requested(run_id, stage="cleanup probe")

        invalid_items = probe_invalid_accounts(
            service=service_payload,
            limit=max_cleanup_count if max_cleanup_count > 0 else None,
            max_probe_count=max_probe_count if max_probe_count > 0 else None,
            progress_callback=_on_probe_progress,
        )
        summary["invalid_items_found"] = len(invalid_items)

        selected_items = invalid_items[:max_cleanup_count] if max_cleanup_count > 0 else invalid_items
        summary["invalid_items_considered"] = len(selected_items)

        remote_names: list[str] = []
        local_processed = 0

        with get_db() as db:
            for item in selected_items:
                raise_if_stop_requested(run_id, stage="cleanup expire")
                email = str(item.get("email") or "").strip()
                if not email:
                    continue

                marked = crud.mark_account_expired_by_email_and_cpa(
                    db,
                    email=email,
                    cpa_service_id=cpa_service_id,
                    reason="cpa_cleanup",
                )
                local_processed += 1
                summary["local_marked_expired"] += marked
                if local_processed % _PROGRESS_EVERY == 0:
                    append_run_log(
                        run_id,
                        (
                            "cleanup expire progress "
                            f"(processed={local_processed}, local_expired={summary['local_marked_expired']})"
                        ),
                    )

                name = str(item.get("name") or "").strip()
                if not name:
                    name = f"{email}.json"
                remote_names.append(name)

        if remote_names:
            remote_processed = 0
            for start in range(0, len(remote_names), _PROGRESS_EVERY):
                raise_if_stop_requested(run_id, stage="cleanup delete")
                batch = remote_names[start:start + _PROGRESS_EVERY]
                delete_result = delete_invalid_accounts(service=service_payload, names=batch)
                summary["remote_deleted"] += int(delete_result.get("deleted", 0) or 0)
                summary["remote_delete_failed"] += int(delete_result.get("failed", 0) or 0)
                remote_processed += len(batch)
                if remote_processed % _PROGRESS_EVERY == 0:
                    append_run_log(
                        run_id,
                        (
                            "cleanup delete progress "
                            f"(processed={remote_processed}, remote_deleted={summary['remote_deleted']}, "
                            f"remote_delete_failed={summary['remote_delete_failed']})"
                        ),
                    )

        append_run_log(
            run_id,
            (
                "cleanup runner complete "
                f"(invalid={summary['invalid_items_found']}, "
                f"local_expired={summary['local_marked_expired']}, "
                f"remote_deleted={summary['remote_deleted']})"
            ),
        )
        finalize_run(run_id, status="success", summary=summary)
        return summary

    except ScheduledRunCancelledError:
        if is_run_stop_requested(run_id):
            finalize_cancelled_run(run_id, summary=summary)
            return summary
        raise
    except Exception as exc:
        append_run_log(run_id, f"cleanup runner failed: {exc}")
        finalize_run(run_id, status="failed", summary=summary, error_message=str(exc))
        raise
