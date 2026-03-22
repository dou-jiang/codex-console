from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.scheduler.service import validate_plan_payload
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


def test_validate_refill_config_requires_failure_threshold():
    with pytest.raises(ValueError, match="max_consecutive_failures"):
        validate_plan_payload(
            task_type="cpa_refill",
            trigger_type="interval",
            config={"target_valid_count": 50, "max_refill_count": 10},
        )
