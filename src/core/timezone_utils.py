"""
时区工具（统一使用北京时间/上海时区展示）
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


UTC = timezone.utc
if ZoneInfo is not None:
    SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
else:  # 兼容极端环境
    SHANGHAI_TZ = timezone(timedelta(hours=8))


def apply_process_timezone() -> None:
    """
    尝试将进程默认时区设置为 Asia/Shanghai。
    """
    try:
        os.environ.setdefault("TZ", "Asia/Shanghai")
        if hasattr(time, "tzset"):
            time.tzset()
    except Exception:
        # 不阻断主流程
        pass


def now_shanghai() -> datetime:
    return datetime.now(UTC).astimezone(SHANGHAI_TZ)


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_shanghai(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # 历史库里是 naive UTC，按 UTC 解释再转上海
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(SHANGHAI_TZ)


def to_shanghai_iso(dt: datetime | None) -> str | None:
    local_dt = to_shanghai(dt)
    return local_dt.isoformat() if local_dt else None

