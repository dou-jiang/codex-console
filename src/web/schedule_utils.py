"""计划任务时间计算工具。"""

from datetime import datetime, timedelta, time
from typing import Any, Dict, Optional

from ..core.timezone_utils import utcnow_naive


VALID_SCHEDULE_TYPES = {"interval", "timepoint"}


def parse_time_of_day(value: str) -> time:
    """解析 HH:MM 格式的时间字符串。"""
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception as exc:
        raise ValueError("时间点格式必须为 HH:MM") from exc

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("时间点必须在 00:00-23:59 之间")

    return time(hour=hour, minute=minute)


def parse_start_date(value: Optional[str], now: datetime) -> datetime.date:
    """解析计划开始日期。"""
    if not value:
        return now.date()

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("开始日期格式必须为 YYYY-MM-DD") from exc


def normalize_schedule_config(
    schedule_type: str,
    schedule_config: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """校验并标准化计划配置。"""
    current_time = now or utcnow_naive()
    config = dict(schedule_config or {})

    if schedule_type not in VALID_SCHEDULE_TYPES:
        raise ValueError("计划类型必须为 interval 或 timepoint")

    if schedule_type == "interval":
        interval_minutes = int(config.get("interval_minutes") or 0)
        if interval_minutes < 1:
            raise ValueError("固定间隔必须大于等于 1 分钟")
        return {"interval_minutes": interval_minutes}

    every_n_days = int(config.get("every_n_days") or 0)
    if every_n_days < 1:
        raise ValueError("周期天数必须大于等于 1")

    time_of_day = config.get("time_of_day") or ""
    parsed_time = parse_time_of_day(time_of_day)
    start_date = parse_start_date(config.get("start_date"), current_time)

    return {
        "every_n_days": every_n_days,
        "time_of_day": parsed_time.strftime("%H:%M"),
        "start_date": start_date.isoformat(),
    }


def compute_next_run_at(
    schedule_type: str,
    schedule_config: Dict[str, Any],
    now: Optional[datetime] = None,
    reference_time: Optional[datetime] = None,
) -> datetime:
    """根据计划配置计算下一次执行时间。"""
    current_time = now or utcnow_naive()
    normalized = normalize_schedule_config(schedule_type, schedule_config, current_time)

    if schedule_type == "interval":
        interval_delta = timedelta(minutes=normalized["interval_minutes"])
        candidate = (reference_time or current_time) + interval_delta
        while candidate <= current_time:
            candidate += interval_delta
        return candidate

    every_n_days = normalized["every_n_days"]
    time_of_day = parse_time_of_day(normalized["time_of_day"])
    start_date = parse_start_date(normalized.get("start_date"), current_time)

    candidate = datetime.combine(start_date, time_of_day)
    anchor_time = reference_time or current_time
    while candidate <= anchor_time:
        candidate += timedelta(days=every_n_days)
    while candidate <= current_time:
        candidate += timedelta(days=every_n_days)
    return candidate


def describe_schedule(schedule_type: str, schedule_config: Dict[str, Any]) -> str:
    """生成人类可读的计划描述。"""
    normalized = normalize_schedule_config(schedule_type, schedule_config)
    if schedule_type == "interval":
        return f"每 {normalized['interval_minutes']} 分钟触发"
    return f"每 {normalized['every_n_days']} 天 {normalized['time_of_day']} 触发"
