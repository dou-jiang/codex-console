from __future__ import annotations

from typing import Any

from croniter import croniter


SUPPORTED_TRIGGER_TYPES = {"cron", "interval"}
SUPPORTED_INTERVAL_UNITS = {"minutes", "hours"}


def _validate_optional_non_negative_int(config: dict[str, Any], key: str) -> None:
    if key not in config:
        return

    raw_value = config.get(key)
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a non-negative integer") from exc

    if parsed < 0:
        raise ValueError(f"{key} must be a non-negative integer")


def validate_trigger_payload(
    trigger_type: str,
    *,
    cron_expression: str | None = None,
    interval_value: int | None = None,
    interval_unit: str | None = None,
) -> None:
    if trigger_type not in SUPPORTED_TRIGGER_TYPES:
        raise ValueError("trigger_type must be cron or interval")

    if trigger_type == "cron":
        if not cron_expression:
            raise ValueError("cron_expression is required")
        if not croniter.is_valid(cron_expression):
            raise ValueError("cron_expression is invalid")
        return

    if interval_value is None:
        raise ValueError("interval_value is required")
    if interval_value <= 0:
        raise ValueError("interval_value must be positive")
    if interval_unit not in SUPPORTED_INTERVAL_UNITS:
        raise ValueError("interval_unit must be one of minutes/hours")


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

    validate_trigger_payload(
        trigger_type,
        cron_expression=cron_expression,
        interval_value=interval_value,
        interval_unit=interval_unit,
    )

    if task_type == "cpa_cleanup":
        _validate_optional_non_negative_int(config, "max_cleanup_count")
        _validate_optional_non_negative_int(config, "max_probe_count")

    if task_type == "cpa_refill" and not config.get("max_consecutive_failures"):
        raise ValueError("max_consecutive_failures is required")
