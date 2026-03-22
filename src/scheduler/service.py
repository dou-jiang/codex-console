from __future__ import annotations

from typing import Any

from croniter import croniter


def validate_trigger_payload(
    trigger_type: str,
    *,
    cron_expression: str | None = None,
    interval_value: int | None = None,
    interval_unit: str | None = None,
) -> None:
    if trigger_type == "cron":
        if not cron_expression:
            raise ValueError("cron_expression is required")
        if not croniter.is_valid(cron_expression):
            raise ValueError("cron_expression is invalid")
        return

    if trigger_type == "interval":
        if not interval_value:
            raise ValueError("interval_value is required")
        if interval_value <= 0:
            raise ValueError("interval_value must be positive")
        if interval_unit not in {"minutes", "hours"}:
            raise ValueError("interval_unit must be one of minutes/hours")
        return

    raise ValueError("trigger_type must be cron or interval")


def validate_plan_payload(
    *,
    task_type: str,
    trigger_type: str,
    config: dict[str, Any],
    cron_expression: str | None = None,
    interval_value: int | None = None,
    interval_unit: str | None = None,
) -> None:
    if not isinstance(config, dict):
        raise ValueError("config must be an object")

    if trigger_type not in {"cron", "interval"}:
        raise ValueError("trigger_type must be cron or interval")

    if task_type == "cpa_refill" and not config.get("max_consecutive_failures"):
        raise ValueError("max_consecutive_failures is required")
