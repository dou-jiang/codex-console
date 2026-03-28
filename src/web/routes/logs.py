"""
后台日志 API
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_

from ...core.db_logs import cleanup_database_logs
from ...core.timezone_utils import to_shanghai_iso, utcnow_naive
from ...database.models import AppLog
from ...database.session import get_db


router = APIRouter()


def _serialize_log_row(row: AppLog) -> dict:
    payload = row.to_dict()
    payload["created_at"] = to_shanghai_iso(row.created_at)
    return payload


class CleanupLogsRequest(BaseModel):
    retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    max_rows: int = Field(default=50000, ge=1000, le=5000000)


@router.get("")
def list_logs(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=500, description="每页数量"),
    level: Optional[str] = Query(None, description="日志级别"),
    logger_name: Optional[str] = Query(None, description="logger 名称关键词"),
    keyword: Optional[str] = Query(None, description="消息关键词"),
    since_minutes: Optional[int] = Query(None, ge=1, le=10080, description="仅返回最近 N 分钟"),
):
    with get_db() as db:
        query = db.query(AppLog)

        if level:
            query = query.filter(AppLog.level == level.upper())

        if logger_name:
            query = query.filter(AppLog.logger.ilike(f"%{logger_name.strip()}%"))

        if keyword:
            pattern = f"%{keyword.strip()}%"
            query = query.filter(
                or_(
                    AppLog.message.ilike(pattern),
                    AppLog.logger.ilike(pattern),
                    AppLog.module.ilike(pattern),
                )
            )

        if since_minutes:
            since_at = utcnow_naive() - timedelta(minutes=since_minutes)
            query = query.filter(AppLog.created_at >= since_at)

        total = query.count()
        offset = (page - 1) * page_size
        rows = (
            query.order_by(AppLog.created_at.desc(), AppLog.id.desc())
            .offset(offset)
            .limit(page_size)
            .all()
        )

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "logs": [_serialize_log_row(row) for row in rows],
        }


@router.get("/stats")
def log_stats():
    with get_db() as db:
        total = db.query(func.count(AppLog.id)).scalar() or 0
        latest = db.query(func.max(AppLog.created_at)).scalar()
        grouped = db.query(AppLog.level, func.count(AppLog.id)).group_by(AppLog.level).all()

    level_counts = {str(level or "UNKNOWN"): int(count or 0) for level, count in grouped}
    return {
        "total": int(total),
        "latest_at": to_shanghai_iso(latest),
        "levels": level_counts,
    }


@router.post("/cleanup")
def cleanup_logs(request: CleanupLogsRequest):
    result = cleanup_database_logs(
        retention_days=request.retention_days,
        max_rows=request.max_rows,
    )
    return {"success": True, **result}


@router.delete("")
def clear_logs(confirm: bool = Query(False, description="确认清空日志")):
    """
    清空日志表（硬删除）。
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="请传入 confirm=true 以确认清空日志")

    with get_db() as db:
        deleted = db.query(AppLog).delete(synchronize_session=False)
        db.commit()

    return {
        "success": True,
        "deleted_total": int(deleted or 0),
        "remaining": 0,
    }
