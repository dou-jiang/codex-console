from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from croniter import croniter


SCHEDULER_TZ = ZoneInfo("Asia/Shanghai")


class PlanLike(Protocol):
    trigger_type: str
    cron_expression: str | None
    interval_value: int | None
    interval_unit: str | None


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(SCHEDULER_TZ)

    if now.tzinfo is None:
        return now.replace(tzinfo=SCHEDULER_TZ)

    return now.astimezone(SCHEDULER_TZ)


def compute_next_run_at(plan: PlanLike, now: datetime | None = None) -> datetime:
    current = _normalize_now(now)

    if plan.trigger_type == "cron":
        if not plan.cron_expression:
            raise ValueError("cron_expression is required for cron trigger")
        return croniter(plan.cron_expression, current).get_next(datetime).astimezone(SCHEDULER_TZ)

    if not plan.interval_value or not plan.interval_unit:
        raise ValueError("interval_value and interval_unit are required for interval trigger")

    try:
        delta = timedelta(**{plan.interval_unit: plan.interval_value})
    except TypeError as exc:
        raise ValueError(f"unsupported interval_unit: {plan.interval_unit}") from exc

    return current + delta
