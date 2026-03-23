from __future__ import annotations

from datetime import datetime
from typing import Any

from ..database import crud
from ..database.session import get_db

USER_REQUESTED_STOP_ERROR_MESSAGE = "user requested stop"
USER_STOP_REQUESTED_LOG = "收到停止请求"
USER_STOP_COMPLETED_LOG = "任务已按请求停止"


def append_run_log(run_id: int, message: str, *, logged_at: datetime | None = None) -> bool:
    """Append a log line for a scheduled run."""
    with get_db() as db:
        return crud.append_scheduled_run_log(db, run_id, message, logged_at=logged_at)


def finalize_run(
    run_id: int,
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    error_message: str | None = None,
    finished_at: datetime | None = None,
) -> bool:
    """Persist final run status payload."""
    with get_db() as db:
        run = crud.finish_scheduled_run(
            db,
            run_id=run_id,
            status=status,
            summary=summary,
            error_message=error_message,
            finished_at=finished_at,
        )
    return run is not None


def finalize_cancelled_run(
    run_id: int,
    *,
    summary: dict[str, Any] | None = None,
    finished_at: datetime | None = None,
) -> bool:
    """Persist final cancelled state for cooperative user stop."""
    return finalize_run(
        run_id,
        status="cancelled",
        summary=summary,
        error_message=USER_REQUESTED_STOP_ERROR_MESSAGE,
        finished_at=finished_at,
    )


def raise_if_stop_requested(run_id: int, *, stage: str | None = None) -> None:
    """Raise the shared cancellation error when a user stop has been requested."""
    from .engine import ScheduledRunCancelledError, is_run_stop_requested

    if not is_run_stop_requested(run_id):
        return

    if stage:
        append_run_log(run_id, f"{USER_STOP_REQUESTED_LOG}（{stage}）")
    else:
        append_run_log(run_id, USER_STOP_REQUESTED_LOG)
    append_run_log(run_id, USER_STOP_COMPLETED_LOG)
    raise ScheduledRunCancelledError(USER_REQUESTED_STOP_ERROR_MESSAGE)
