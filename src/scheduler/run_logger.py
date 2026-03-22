from __future__ import annotations

from datetime import datetime
from typing import Any

from ..database import crud
from ..database.session import get_db


def append_run_log(run_id: int, message: str) -> bool:
    """Append a log line for a scheduled run."""
    with get_db() as db:
        return crud.append_scheduled_run_log(db, run_id, message)


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
