from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.scheduler.schemas import ScheduledPlanCreate, ScheduledPlanResponse
from src.scheduler.service import validate_plan_payload, validate_trigger_payload
from src.scheduler.time_utils import compute_next_run_at


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def test_compute_next_run_at_for_cron_uses_asia_shanghai():
    now = datetime(2026, 3, 22, 8, 1, tzinfo=SHANGHAI_TZ)
    plan = SimpleNamespace(trigger_type="cron", cron_expression="0 9 * * *")

    result = compute_next_run_at(plan, now=now)

    assert result == datetime(2026, 3, 22, 9, 0, tzinfo=SHANGHAI_TZ)


def test_compute_next_run_at_for_interval_adds_hours():
    now = datetime(2026, 3, 22, 8, 1, tzinfo=SHANGHAI_TZ)
    plan = SimpleNamespace(trigger_type="interval", interval_value=2, interval_unit="hours")

    assert compute_next_run_at(plan, now=now) == datetime(2026, 3, 22, 10, 1, tzinfo=SHANGHAI_TZ)


def test_compute_next_run_at_rejects_unsupported_trigger_type():
    now = datetime(2026, 3, 22, 8, 1, tzinfo=SHANGHAI_TZ)
    plan = SimpleNamespace(trigger_type="manual", interval_value=2, interval_unit="hours")

    with pytest.raises(ValueError, match="trigger_type"):
        compute_next_run_at(plan, now=now)


def test_compute_next_run_at_rejects_negative_interval_value():
    now = datetime(2026, 3, 22, 8, 1, tzinfo=SHANGHAI_TZ)
    plan = SimpleNamespace(trigger_type="interval", interval_value=-1, interval_unit="hours")

    with pytest.raises(ValueError, match="interval_value"):
        compute_next_run_at(plan, now=now)


def test_compute_next_run_at_rejects_invalid_cron_expression():
    now = datetime(2026, 3, 22, 8, 1, tzinfo=SHANGHAI_TZ)
    plan = SimpleNamespace(trigger_type="cron", cron_expression="not-a-cron")

    with pytest.raises(ValueError, match="cron_expression"):
        compute_next_run_at(plan, now=now)


def test_compute_next_run_at_rejects_unsupported_interval_unit():
    now = datetime(2026, 3, 22, 8, 1, tzinfo=SHANGHAI_TZ)
    plan = SimpleNamespace(trigger_type="interval", interval_value=2, interval_unit="days")

    with pytest.raises(ValueError, match="interval_unit"):
        compute_next_run_at(plan, now=now)


def test_validate_refill_config_requires_failure_threshold():
    with pytest.raises(ValueError, match="max_consecutive_failures"):
        validate_plan_payload(
            task_type="cpa_refill",
            trigger_type="interval",
            config={"target_valid_count": 50, "max_refill_count": 10},
            interval_value=1,
            interval_unit="hours",
        )


def test_validate_cleanup_config_rejects_negative_max_probe_count():
    with pytest.raises(ValueError, match="max_probe_count"):
        validate_plan_payload(
            task_type="cpa_cleanup",
            trigger_type="interval",
            config={"max_cleanup_count": 10, "max_probe_count": -1},
            interval_value=1,
            interval_unit="hours",
        )


def test_validate_trigger_payload_rejects_invalid_cron_expression():
    with pytest.raises(ValueError, match="cron_expression is invalid"):
        validate_trigger_payload(trigger_type="cron", cron_expression="not-a-cron")


def test_validate_trigger_payload_rejects_non_positive_interval_values():
    with pytest.raises(ValueError, match="interval_value must be positive"):
        validate_trigger_payload(trigger_type="interval", interval_value=0, interval_unit="hours")

    with pytest.raises(ValueError, match="interval_value must be positive"):
        validate_trigger_payload(trigger_type="interval", interval_value=-2, interval_unit="hours")


def test_validate_trigger_payload_rejects_unsupported_trigger_type():
    with pytest.raises(ValueError, match="trigger_type"):
        validate_trigger_payload(trigger_type="manual")


def test_validate_plan_payload_rejects_invalid_cron_expression():
    with pytest.raises(ValueError, match="cron_expression is invalid"):
        validate_plan_payload(
            task_type="cpa_cleanup",
            trigger_type="cron",
            cron_expression="invalid",
            config={"max_cleanup_count": 10},
        )


def test_validate_plan_payload_rejects_negative_interval():
    with pytest.raises(ValueError, match="interval_value must be positive"):
        validate_plan_payload(
            task_type="cpa_cleanup",
            trigger_type="interval",
            interval_value=-3,
            interval_unit="hours",
            config={"max_cleanup_count": 10},
        )


def test_validate_plan_payload_rejects_unsupported_trigger_type():
    with pytest.raises(ValueError, match="trigger_type"):
        validate_plan_payload(
            task_type="cpa_cleanup",
            trigger_type="manual",
            config={"max_cleanup_count": 10},
        )


def test_scheduled_plan_create_rejects_invalid_cron_expression():
    with pytest.raises(ValidationError, match="cron_expression is invalid"):
        ScheduledPlanCreate(
            name="bad cron",
            task_type="cpa_cleanup",
            cpa_service_id=1,
            trigger_type="cron",
            cron_expression="bad-cron",
            config={"max_cleanup_count": 10},
        )


def test_scheduled_plan_create_rejects_non_positive_interval():
    with pytest.raises(ValidationError, match="interval_value must be positive"):
        ScheduledPlanCreate(
            name="bad interval",
            task_type="cpa_cleanup",
            cpa_service_id=1,
            trigger_type="interval",
            interval_value=0,
            interval_unit="minutes",
            config={"max_cleanup_count": 10},
        )


def test_scheduled_plan_response_rejects_invalid_last_run_status():
    with pytest.raises(ValidationError):
        ScheduledPlanResponse(
            id=1,
            name="daily",
            task_type="cpa_cleanup",
            cpa_service_id=2,
            trigger_type="cron",
            cron_expression="0 9 * * *",
            config={"max_cleanup_count": 10},
            enabled=True,
            last_run_status="broken",
        )
