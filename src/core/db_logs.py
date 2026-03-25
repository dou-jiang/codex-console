"""
后台日志入库与清理
"""

from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func

from ..config.settings import get_settings
from ..database.models import AppLog
from ..database.session import get_db


_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_SKIP_LOGGER_PREFIXES = (
    "sqlalchemy",
    "uvicorn.access",
    "watchfiles",
)


def _should_skip_record(record: logging.LogRecord) -> bool:
    logger_name = str(record.name or "")
    if not logger_name:
        return False
    for prefix in _SKIP_LOGGER_PREFIXES:
        if logger_name.startswith(prefix):
            return True
    return False


class DatabaseLogHandler(logging.Handler):
    """
    将日志写入 app_logs 表。
    为避免递归写日志，emit 内部不再产生日志。
    """

    def __init__(self, min_level: int = logging.INFO):
        super().__init__(level=min_level)
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False):
            return
        if record.levelno < self.level:
            return
        if _should_skip_record(record):
            return

        message = ""
        exception_text = None
        try:
            self._local.busy = True
            message = record.getMessage()
            if record.exc_info:
                exception_text = "".join(traceback.format_exception(*record.exc_info))[-4000:]
            elif record.exc_text:
                exception_text = str(record.exc_text)[-4000:]

            with get_db() as db:
                db.add(
                    AppLog(
                        level=record.levelname,
                        logger=str(record.name or "root"),
                        module=str(record.module or ""),
                        pathname=str(record.pathname or ""),
                        lineno=int(record.lineno or 0),
                        message=str(message or ""),
                        exception=exception_text,
                        created_at=datetime.utcfromtimestamp(record.created),
                    )
                )
                db.commit()
        except Exception:
            self.handleError(record)
        finally:
            self._local.busy = False


def install_database_log_handler(min_level: int = logging.INFO) -> bool:
    """
    安装数据库日志处理器（全局仅安装一次）。
    Returns:
        是否本次新安装
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return False

        root_logger = logging.getLogger()
        if any(isinstance(handler, DatabaseLogHandler) for handler in root_logger.handlers):
            _INSTALLED = True
            return False

        handler = DatabaseLogHandler(min_level=min_level)
        root_logger.addHandler(handler)
        _INSTALLED = True
        return True


def cleanup_database_logs(
    retention_days: Optional[int] = None,
    max_rows: int = 50000,
) -> Dict[str, Any]:
    """
    清理后台日志：
    1) 删除超过 retention_days 的日志
    2) 若总量超过 max_rows，删除最旧的超量部分
    """
    settings = get_settings()
    keep_days = int(retention_days if retention_days is not None else settings.log_retention_days or 30)
    keep_days = max(1, keep_days)
    max_rows = max(1000, int(max_rows))
    cutoff = datetime.utcnow() - timedelta(days=keep_days)

    deleted_by_age = 0
    deleted_by_limit = 0

    with get_db() as db:
        deleted_by_age = (
            db.query(AppLog)
            .filter(AppLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()

        total = db.query(func.count(AppLog.id)).scalar() or 0
        if total > max_rows:
            overflow = int(total - max_rows)
            overflow_ids = [
                row_id
                for (row_id,) in db.query(AppLog.id)
                .order_by(AppLog.created_at.asc(), AppLog.id.asc())
                .limit(overflow)
                .all()
            ]
            if overflow_ids:
                deleted_by_limit = (
                    db.query(AppLog)
                    .filter(AppLog.id.in_(overflow_ids))
                    .delete(synchronize_session=False)
                )
                db.commit()

        remaining = db.query(func.count(AppLog.id)).scalar() or 0

    return {
        "retention_days": keep_days,
        "max_rows": max_rows,
        "deleted_by_age": int(deleted_by_age or 0),
        "deleted_by_limit": int(deleted_by_limit or 0),
        "deleted_total": int((deleted_by_age or 0) + (deleted_by_limit or 0)),
        "remaining": int(remaining or 0),
    }

