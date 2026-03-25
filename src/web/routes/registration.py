"""
注册任务 API 路由
"""

import asyncio
import logging
import os
import uuid
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Tuple, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field

from ...database import crud
from ...database.session import get_db
from ...database.models import RegistrationTask, Proxy
from ...core.register import RegistrationEngine, RegistrationResult
from ...services import EmailServiceFactory, EmailServiceType
from ...config.settings import get_settings
from ..task_manager import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()

# 任务存储（简单的内存存储，生产环境应使用 Redis）
running_tasks: dict = {}
# 批量任务存储
batch_tasks: Dict[str, dict] = {}

# 定时注册任务存储（内存态，重启后清空）
scheduled_registration_lock = threading.Lock()
scheduled_registration: Dict[str, Any] = {
    "enabled": False,
    "schedule_cron": "*/30 * * * *",
    "timezone": "Asia/Shanghai",
    "registration_mode": "batch",  # single | batch
    "payload": None,               # 保留请求快照
    "next_run_at": None,
    "last_run_at": None,
    "last_error": None,
    "total_runs": 0,
    "success_runs": 0,
    "failed_runs": 0,
    "skipped_runs": 0,
    "active_task_uuid": None,
    "active_batch_id": None,
}
scheduled_registration_runner: Optional[asyncio.Task] = None


# ============== Proxy Helper Functions ==============

def get_proxy_for_registration(db) -> Tuple[Optional[str], Optional[int]]:
    """
    获取用于注册的代理

    策略：
    1. 优先从代理列表中随机选择一个启用的代理
    2. 如果代理列表为空且启用了动态代理，调用动态代理 API 获取
    3. 否则使用系统设置中的静态默认代理

    Returns:
        Tuple[proxy_url, proxy_id]: 代理 URL 和代理 ID（如果来自代理列表）
    """
    # 先尝试从代理列表中获取
    proxy = crud.get_random_proxy(db)
    if proxy:
        return proxy.proxy_url, proxy.id

    # 代理列表为空，尝试动态代理或静态代理
    from ...core.dynamic_proxy import get_proxy_url_for_task
    proxy_url = get_proxy_url_for_task()
    if proxy_url:
        return proxy_url, None

    return None, None


def update_proxy_usage(db, proxy_id: Optional[int]):
    """更新代理的使用时间"""
    if proxy_id:
        crud.update_proxy_last_used(db, proxy_id)


# ============== Pydantic Models ==============

class RegistrationTaskCreate(BaseModel):
    """创建注册任务请求"""
    email_service_type: str = "tempmail"
    proxy: Optional[str] = None
    email_service_config: Optional[dict] = None
    email_service_id: Optional[int] = None
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = []  # 指定 CPA 服务 ID 列表，空则取第一个启用的
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []  # 指定 Sub2API 服务 ID 列表
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []  # 指定 TM 服务 ID 列表


class BatchRegistrationRequest(BaseModel):
    """批量注册请求"""
    count: int = 100
    email_service_type: str = "tempmail"
    proxy: Optional[str] = None
    email_service_config: Optional[dict] = None
    email_service_id: Optional[int] = None
    interval_min: int = 5
    interval_max: int = 30
    concurrency: int = 1
    mode: str = "pipeline"
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = []
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []


class RegistrationTaskResponse(BaseModel):
    """注册任务响应"""
    id: int
    task_uuid: str
    status: str
    email_service_id: Optional[int] = None
    proxy: Optional[str] = None
    logs: Optional[str] = None
    result: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    class Config:
        from_attributes = True


class BatchRegistrationResponse(BaseModel):
    """批量注册响应"""
    batch_id: str
    count: int
    tasks: List[RegistrationTaskResponse]


class TaskListResponse(BaseModel):
    """任务列表响应"""
    total: int
    tasks: List[RegistrationTaskResponse]


# ============== Outlook 批量注册模型 ==============

class OutlookAccountForRegistration(BaseModel):
    """可用于注册的 Outlook 账户"""
    id: int                      # EmailService 表的 ID
    email: str
    name: str
    has_oauth: bool              # 是否有 OAuth 配置
    is_registered: bool          # 是否已注册
    registered_account_id: Optional[int] = None


class OutlookAccountsListResponse(BaseModel):
    """Outlook 账户列表响应"""
    total: int
    registered_count: int        # 已注册数量
    unregistered_count: int      # 未注册数量
    accounts: List[OutlookAccountForRegistration]


class OutlookBatchRegistrationRequest(BaseModel):
    """Outlook 批量注册请求"""
    service_ids: List[int]
    skip_registered: bool = True
    proxy: Optional[str] = None
    interval_min: int = 5
    interval_max: int = 30
    concurrency: int = 1
    mode: str = "pipeline"
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = []
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []


class OutlookBatchRegistrationResponse(BaseModel):
    """Outlook 批量注册响应"""
    batch_id: str
    total: int                   # 总数
    skipped: int                 # 跳过数（已注册）
    to_register: int             # 待注册数
    service_ids: List[int]       # 实际要注册的服务 ID


class ScheduledRegistrationRequest(BaseModel):
    """定时注册请求"""
    schedule_cron: str = "*/30 * * * *"  # 5 位 Cron: 分 时 日 月 周
    schedule_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)  # 兼容旧参数
    run_immediately: bool = True
    registration_mode: str = "batch"  # single | batch
    email_service_type: str = "tempmail"
    proxy: Optional[str] = None
    email_service_config: Optional[dict] = None
    email_service_id: Optional[int] = None
    count: int = 100
    interval_min: int = 5
    interval_max: int = 30
    concurrency: int = 1
    mode: str = "pipeline"
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = []
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []


class ScheduledRegistrationStatusResponse(BaseModel):
    """定时注册状态响应"""
    enabled: bool
    schedule_cron: str
    timezone: str
    registration_mode: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_error: Optional[str] = None
    total_runs: int = 0
    success_runs: int = 0
    failed_runs: int = 0
    skipped_runs: int = 0
    active_task_uuid: Optional[str] = None
    active_batch_id: Optional[str] = None


# ============== Helper Functions ==============

def task_to_response(task: RegistrationTask) -> RegistrationTaskResponse:
    """转换任务模型为响应"""
    return RegistrationTaskResponse(
        id=task.id,
        task_uuid=task.task_uuid,
        status=task.status,
        email_service_id=task.email_service_id,
        proxy=task.proxy,
        logs=task.logs,
        result=task.result,
        error_message=task.error_message,
        created_at=task.created_at.isoformat() if task.created_at else None,
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
    )


def _normalize_email_service_config(
    service_type: EmailServiceType,
    config: Optional[dict],
    proxy_url: Optional[str] = None
) -> dict:
    """按服务类型兼容旧字段名，避免不同服务的配置键互相污染。"""
    normalized = config.copy() if config else {}

    if 'api_url' in normalized and 'base_url' not in normalized:
        normalized['base_url'] = normalized.pop('api_url')

    if service_type == EmailServiceType.MOE_MAIL:
        if 'domain' in normalized and 'default_domain' not in normalized:
            normalized['default_domain'] = normalized.pop('domain')
    elif service_type in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL):
        if 'default_domain' in normalized and 'domain' not in normalized:
            normalized['domain'] = normalized.pop('default_domain')
    elif service_type == EmailServiceType.DUCK_MAIL:
        if 'domain' in normalized and 'default_domain' not in normalized:
            normalized['default_domain'] = normalized.pop('domain')

    if proxy_url and 'proxy_url' not in normalized:
        normalized['proxy_url'] = proxy_url

    return normalized


def _get_schedule_timezone_name() -> str:
    """获取定时任务使用的时区名称：优先环境变量 TZ，否则 Asia/Shanghai。"""
    tz_name = (os.environ.get("TZ") or "Asia/Shanghai").strip()
    return tz_name or "Asia/Shanghai"


def _get_schedule_timezone():
    """获取时区对象。"""
    tz_name = _get_schedule_timezone_name()
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(f"无效 TZ={tz_name}，将回退到 Asia/Shanghai")
        try:
            return ZoneInfo("Asia/Shanghai")
        except ZoneInfoNotFoundError:
            # 某些精简镜像不包含 tzdata，兜底为固定东八区，确保与默认时区一致
            logger.warning("系统缺少 Asia/Shanghai 时区数据，回退到固定 +08:00")
            return timezone(timedelta(hours=8), name="Asia/Shanghai")


def _now_in_schedule_tz() -> datetime:
    """获取当前调度时区时间。"""
    return datetime.now(_get_schedule_timezone())


def _dt_to_iso(value: Optional[datetime]) -> Optional[str]:
    """将时间转换为 ISO 字符串。"""
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_get_schedule_timezone())
    return value.isoformat()


def _legacy_interval_to_cron(interval_minutes: int) -> str:
    """将旧版 interval（分钟）转换为 5 位 Cron。"""
    if interval_minutes < 1 or interval_minutes > 1440:
        raise ValueError("旧版 interval 必须在 1-1440 分钟之间")

    if interval_minutes <= 59:
        return f"*/{interval_minutes} * * * *"
    if interval_minutes == 60:
        return "0 * * * *"
    if interval_minutes % 60 == 0:
        hours = interval_minutes // 60
        if 1 <= hours <= 23:
            return f"0 */{hours} * * *"
        if hours == 24:
            return "0 0 * * *"

    raise ValueError("旧版 interval 仅支持 1-59、整小时或 24 小时，请改用 5 位 Cron 表达式")


def _cron_to_legacy_interval_minutes(cron_expr: str) -> int:
    """尽力将 Cron 回写到旧字段（仅用于兼容）。"""
    parts = " ".join((cron_expr or "").split()).split(" ")
    if len(parts) != 5:
        return 30

    minute, hour, day, month, weekday = parts
    if hour == "*" and day == "*" and month == "*" and weekday == "*" and minute.startswith("*/"):
        try:
            val = int(minute[2:])
            if 1 <= val <= 59:
                return val
        except ValueError:
            return 30

    if minute == "0" and hour == "*" and day == "*" and month == "*" and weekday == "*":
        return 60

    if minute == "0" and day == "*" and month == "*" and weekday == "*" and hour.startswith("*/"):
        try:
            val = int(hour[2:])
            if 1 <= val <= 23:
                return val * 60
        except ValueError:
            return 30

    if minute == "0" and hour == "0" and day == "*" and month == "*" and weekday == "*":
        return 1440

    return 30


def _parse_cron_field(field: str, min_value: int, max_value: int, field_name: str, is_weekday: bool = False) -> set:
    """解析单个 Cron 字段，支持 *, */n, a, a-b, a-b/n, a,b,c。"""
    values = set()
    tokens = [token.strip() for token in field.split(",") if token.strip()]
    if not tokens:
        raise ValueError(f"{field_name} 字段不能为空")

    for token in tokens:
        step = 1
        base = token
        if "/" in token:
            base, step_str = token.split("/", 1)
            if not step_str.isdigit():
                raise ValueError(f"{field_name} 字段步长无效: {token}")
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"{field_name} 字段步长必须大于 0: {token}")

        def _normalize(v: int) -> int:
            if is_weekday and v == 7:
                return 0
            return v

        def _validate(v: int) -> int:
            nv = _normalize(v)
            upper = 6 if is_weekday else max_value
            lower = min_value
            if nv < lower or nv > upper:
                raise ValueError(f"{field_name} 字段超出范围 {min_value}-{max_value}: {token}")
            return nv

        if base in ("*", ""):
            for raw in range(min_value, max_value + 1, step):
                values.add(_normalize(raw))
            continue

        if "-" in base:
            start_str, end_str = base.split("-", 1)
            if not start_str.isdigit() or not end_str.isdigit():
                raise ValueError(f"{field_name} 字段范围无效: {token}")
            start = int(start_str)
            end = int(end_str)
            if start > end:
                raise ValueError(f"{field_name} 字段范围无效: {token}")
            for raw in range(start, end + 1, step):
                values.add(_validate(raw))
            continue

        if not base.isdigit():
            raise ValueError(f"{field_name} 字段值无效: {token}")

        start = int(base)
        if step == 1:
            values.add(_validate(start))
        else:
            for raw in range(start, max_value + 1, step):
                values.add(_validate(raw))

    return values


def _parse_cron_expression(cron_expr: str) -> Tuple[str, Dict[str, Any]]:
    """解析 5 位 Cron 表达式。"""
    normalized = " ".join((cron_expr or "").split())
    parts = normalized.split(" ")
    if len(parts) != 5:
        raise ValueError("Cron 必须是 5 位（分 时 日 月 周）")

    minute_field, hour_field, day_field, month_field, weekday_field = parts
    parsed = {
        "minute": _parse_cron_field(minute_field, 0, 59, "分钟"),
        "hour": _parse_cron_field(hour_field, 0, 23, "小时"),
        "day": _parse_cron_field(day_field, 1, 31, "日期"),
        "month": _parse_cron_field(month_field, 1, 12, "月份"),
        "weekday": _parse_cron_field(weekday_field, 0, 7, "星期", is_weekday=True),
        "day_any": day_field == "*",
        "weekday_any": weekday_field == "*",
    }
    return normalized, parsed


def _validate_schedule_cron_or_raise(cron_expr: str) -> str:
    """校验 Cron 并返回规范化结果。"""
    try:
        normalized, _ = _parse_cron_expression(cron_expr)
        return normalized
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Cron 表达式无效: {e}")


def _cron_day_matches(dt: datetime, parsed: Dict[str, Any]) -> bool:
    """Cron 的 day/month/week 匹配规则（与标准 cron 一致）。"""
    day_match = dt.day in parsed["day"]
    weekday_now = (dt.weekday() + 1) % 7  # Python: Mon=0, Cron: Sun=0
    weekday_match = weekday_now in parsed["weekday"]

    if parsed["day_any"] and parsed["weekday_any"]:
        return True
    if parsed["day_any"]:
        return weekday_match
    if parsed["weekday_any"]:
        return day_match
    return day_match or weekday_match


def _cron_matches(dt: datetime, parsed: Dict[str, Any]) -> bool:
    return (
        dt.minute in parsed["minute"]
        and dt.hour in parsed["hour"]
        and dt.month in parsed["month"]
        and _cron_day_matches(dt, parsed)
    )


def _get_next_run_from_cron(cron_expr: str, after_dt: Optional[datetime] = None) -> datetime:
    """计算下一次触发时间（调度时区）。"""
    normalized, parsed = _parse_cron_expression(cron_expr)
    tz = _get_schedule_timezone()
    now = (after_dt or _now_in_schedule_tz()).astimezone(tz)
    probe = now.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # 最多搜索 2 年，足够覆盖常见 Cron 场景
    for _ in range(2 * 366 * 24 * 60):
        if _cron_matches(probe, parsed):
            return probe
        probe += timedelta(minutes=1)

    raise RuntimeError(f"无法计算下一次触发时间，请检查 Cron: {normalized}")


def _get_scheduled_registration_status() -> ScheduledRegistrationStatusResponse:
    """获取定时注册状态快照。"""
    with scheduled_registration_lock:
        state = scheduled_registration.copy()

    return ScheduledRegistrationStatusResponse(
        enabled=bool(state.get("enabled")),
        schedule_cron=state.get("schedule_cron") or "*/30 * * * *",
        timezone=state.get("timezone") or _get_schedule_timezone_name(),
        registration_mode=state.get("registration_mode") or "batch",
        payload=state.get("payload") or {},
        next_run_at=_dt_to_iso(state.get("next_run_at")),
        last_run_at=_dt_to_iso(state.get("last_run_at")),
        last_error=state.get("last_error"),
        total_runs=int(state.get("total_runs") or 0),
        success_runs=int(state.get("success_runs") or 0),
        failed_runs=int(state.get("failed_runs") or 0),
        skipped_runs=int(state.get("skipped_runs") or 0),
        active_task_uuid=state.get("active_task_uuid"),
        active_batch_id=state.get("active_batch_id"),
    )


def _save_scheduled_registration_settings(enabled: bool, schedule_cron: str, payload: Optional[dict] = None):
    """将定时注册配置持久化到 settings。"""
    from ...config.settings import update_settings

    cron_normalized = _validate_schedule_cron_or_raise(schedule_cron)
    update_settings(
        registration_schedule_enabled=enabled,
        registration_schedule_interval_minutes=_cron_to_legacy_interval_minutes(cron_normalized),
        registration_schedule_cron=cron_normalized,
        registration_schedule_payload=payload or {},
    )


async def restore_scheduled_registration_from_settings():
    """应用启动时从 settings 恢复定时注册配置。"""
    global scheduled_registration_runner
    settings = get_settings()
    tz_name = _get_schedule_timezone_name()

    enabled = bool(settings.registration_schedule_enabled)
    saved_cron = (getattr(settings, "registration_schedule_cron", "") or "").strip()
    legacy_interval = int(settings.registration_schedule_interval_minutes or 30)
    raw_payload = settings.registration_schedule_payload or {}

    fallback_cron = saved_cron or _legacy_interval_to_cron(legacy_interval)
    payload: dict = {}
    schedule_cron = fallback_cron

    if raw_payload:
        try:
            normalized_payload = dict(raw_payload)
            if not normalized_payload.get("schedule_cron"):
                legacy = normalized_payload.get("schedule_interval_minutes")
                if legacy is not None:
                    normalized_payload["schedule_cron"] = _legacy_interval_to_cron(int(legacy))
                else:
                    normalized_payload["schedule_cron"] = fallback_cron
            request_payload = ScheduledRegistrationRequest(**normalized_payload)
            payload = request_payload.model_dump(exclude_none=True)
            schedule_cron = payload.get("schedule_cron", fallback_cron)
        except Exception as e:
            logger.warning(f"定时注册配置恢复失败，将自动禁用: {e}")
            enabled = False
            payload = {}
            try:
                _save_scheduled_registration_settings(False, fallback_cron, {})
            except Exception:
                pass
    elif enabled:
        logger.warning("定时注册已启用但缺少有效配置，已自动禁用")
        enabled = False
        try:
            _save_scheduled_registration_settings(False, fallback_cron, {})
        except Exception:
            pass

    try:
        schedule_cron = _validate_schedule_cron_or_raise(schedule_cron)
    except HTTPException as e:
        logger.warning(f"定时注册 Cron 无效，将自动禁用: {e.detail}")
        enabled = False
        payload = {}
        schedule_cron = "*/30 * * * *"
        try:
            _save_scheduled_registration_settings(False, schedule_cron, {})
        except Exception:
            pass
    now = _now_in_schedule_tz()
    with scheduled_registration_lock:
        scheduled_registration["enabled"] = enabled and bool(payload)
        scheduled_registration["schedule_cron"] = schedule_cron
        scheduled_registration["timezone"] = tz_name
        scheduled_registration["registration_mode"] = payload.get("registration_mode", "batch") if payload else "batch"
        scheduled_registration["payload"] = payload
        scheduled_registration["next_run_at"] = _get_next_run_from_cron(schedule_cron, now) if (enabled and payload) else None
        scheduled_registration["last_run_at"] = None
        scheduled_registration["last_error"] = None
        scheduled_registration["total_runs"] = 0
        scheduled_registration["success_runs"] = 0
        scheduled_registration["failed_runs"] = 0
        scheduled_registration["skipped_runs"] = 0
        scheduled_registration["active_task_uuid"] = None
        scheduled_registration["active_batch_id"] = None

    if scheduled_registration["enabled"]:
        if not scheduled_registration_runner or scheduled_registration_runner.done():
            scheduled_registration_runner = asyncio.create_task(_scheduled_registration_loop())
        logger.info(
            "已从 settings 恢复定时注册：cron=%s, tz=%s, mode=%s",
            scheduled_registration["schedule_cron"],
            scheduled_registration["timezone"],
            scheduled_registration["registration_mode"],
        )


def _validate_email_service_type(email_service_type: str):
    """验证邮箱服务类型。"""
    try:
        EmailServiceType(email_service_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"无效的邮箱服务类型: {email_service_type}"
        )


def _validate_batch_params(count: int, interval_min: int, interval_max: int, concurrency: int, mode: str):
    """验证批量参数。"""
    if count < 1 or count > 100:
        raise HTTPException(status_code=400, detail="注册数量必须在 1-100 之间")

    if interval_min < 0 or interval_max < interval_min:
        raise HTTPException(status_code=400, detail="间隔时间参数无效")

    if not 1 <= concurrency <= 50:
        raise HTTPException(status_code=400, detail="并发数必须在 1-50 之间")

    if mode not in ("parallel", "pipeline"):
        raise HTTPException(status_code=400, detail="模式必须为 parallel 或 pipeline")


async def _enqueue_single_registration(
    request: RegistrationTaskCreate,
    background_tasks: Optional[BackgroundTasks] = None,
) -> RegistrationTaskResponse:
    """创建并启动单次注册任务。"""
    _validate_email_service_type(request.email_service_type)

    task_uuid = str(uuid.uuid4())
    with get_db() as db:
        task = crud.create_registration_task(
            db,
            task_uuid=task_uuid,
            proxy=request.proxy
        )

    args = (
        task_uuid,
        request.email_service_type,
        request.proxy,
        request.email_service_config,
        request.email_service_id,
        "",
        "",
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
    )

    if background_tasks is not None:
        background_tasks.add_task(run_registration_task, *args)
    else:
        asyncio.create_task(run_registration_task(*args))

    return task_to_response(task)


async def _enqueue_batch_registration(
    request: BatchRegistrationRequest,
    background_tasks: Optional[BackgroundTasks] = None,
) -> BatchRegistrationResponse:
    """创建并启动批量注册任务。"""
    _validate_email_service_type(request.email_service_type)
    _validate_batch_params(
        count=request.count,
        interval_min=request.interval_min,
        interval_max=request.interval_max,
        concurrency=request.concurrency,
        mode=request.mode,
    )

    batch_id = str(uuid.uuid4())
    task_uuids = []

    with get_db() as db:
        for _ in range(request.count):
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=request.proxy
            )
            task_uuids.append(task_uuid)

    with get_db() as db:
        tasks = [crud.get_registration_task(db, task_uuid) for task_uuid in task_uuids]

    args = (
        batch_id,
        task_uuids,
        request.email_service_type,
        request.proxy,
        request.email_service_config,
        request.email_service_id,
        request.interval_min,
        request.interval_max,
        request.concurrency,
        request.mode,
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
    )

    if background_tasks is not None:
        background_tasks.add_task(run_batch_registration, *args)
    else:
        asyncio.create_task(run_batch_registration(*args))

    return BatchRegistrationResponse(
        batch_id=batch_id,
        count=request.count,
        tasks=[task_to_response(task) for task in tasks if task]
    )


def _has_active_scheduled_job() -> bool:
    """检查上一次定时触发的任务是否还在运行。"""
    with scheduled_registration_lock:
        active_task_uuid = scheduled_registration.get("active_task_uuid")
        active_batch_id = scheduled_registration.get("active_batch_id")

    if active_task_uuid:
        with get_db() as db:
            task = crud.get_registration_task(db, active_task_uuid)
            if task and task.status in ("pending", "running"):
                return True

    if active_batch_id:
        batch = batch_tasks.get(active_batch_id)
        if batch and not batch.get("finished", False):
            return True

    return False


async def _run_scheduled_registration_once():
    """执行一次定时注册触发。"""
    with scheduled_registration_lock:
        payload = dict(scheduled_registration.get("payload") or {})
        registration_mode = scheduled_registration.get("registration_mode") or "batch"

    if not payload:
        raise RuntimeError("定时任务配置为空")

    common_kwargs = {
        "email_service_type": payload.get("email_service_type") or "tempmail",
        "proxy": payload.get("proxy"),
        "email_service_config": payload.get("email_service_config"),
        "email_service_id": payload.get("email_service_id"),
        "auto_upload_cpa": bool(payload.get("auto_upload_cpa")),
        "cpa_service_ids": payload.get("cpa_service_ids") or [],
        "auto_upload_sub2api": bool(payload.get("auto_upload_sub2api")),
        "sub2api_service_ids": payload.get("sub2api_service_ids") or [],
        "auto_upload_tm": bool(payload.get("auto_upload_tm")),
        "tm_service_ids": payload.get("tm_service_ids") or [],
    }

    if registration_mode == "single":
        response = await _enqueue_single_registration(RegistrationTaskCreate(**common_kwargs))
        with scheduled_registration_lock:
            scheduled_registration["active_task_uuid"] = response.task_uuid
            scheduled_registration["active_batch_id"] = None
        logger.info(f"定时注册触发成功（单次任务）: {response.task_uuid}")
        return

    if registration_mode == "batch":
        response = await _enqueue_batch_registration(BatchRegistrationRequest(
            **common_kwargs,
            count=int(payload.get("count") or 100),
            interval_min=int(payload.get("interval_min") or 5),
            interval_max=int(payload.get("interval_max") or 30),
            concurrency=int(payload.get("concurrency") or 1),
            mode=payload.get("mode") or "pipeline",
        ))
        with scheduled_registration_lock:
            scheduled_registration["active_task_uuid"] = None
            scheduled_registration["active_batch_id"] = response.batch_id
        logger.info(f"定时注册触发成功（批量任务）: {response.batch_id}")
        return

    raise RuntimeError(f"不支持的定时注册模式: {registration_mode}")


async def _scheduled_registration_loop():
    """后台循环：到点触发注册任务。"""
    global scheduled_registration_runner
    logger.info("定时注册调度器已启动 (tz=%s)", _get_schedule_timezone_name())

    try:
        while True:
            await asyncio.sleep(1)

            with scheduled_registration_lock:
                enabled = bool(scheduled_registration.get("enabled"))
                schedule_cron = scheduled_registration.get("schedule_cron") or "*/30 * * * *"
                next_run_at = scheduled_registration.get("next_run_at")

            if not enabled:
                break

            now = _now_in_schedule_tz()
            if not next_run_at:
                with scheduled_registration_lock:
                    scheduled_registration["next_run_at"] = _get_next_run_from_cron(schedule_cron, now)
                continue

            if now < next_run_at:
                continue

            # 默认一个定时器只维持一条执行链，上一轮还在跑就跳过本轮
            if _has_active_scheduled_job():
                with scheduled_registration_lock:
                    scheduled_registration["next_run_at"] = _get_next_run_from_cron(schedule_cron, now)
                    scheduled_registration["skipped_runs"] = int(scheduled_registration.get("skipped_runs") or 0) + 1
                logger.info("定时注册本轮跳过：上一轮任务仍在执行")
                continue

            with scheduled_registration_lock:
                scheduled_registration["last_run_at"] = now
                scheduled_registration["next_run_at"] = _get_next_run_from_cron(schedule_cron, now)
                scheduled_registration["last_error"] = None

            try:
                await _run_scheduled_registration_once()
                with scheduled_registration_lock:
                    scheduled_registration["total_runs"] = int(scheduled_registration.get("total_runs") or 0) + 1
                    scheduled_registration["success_runs"] = int(scheduled_registration.get("success_runs") or 0) + 1
            except Exception as e:
                err_message = e.detail if isinstance(e, HTTPException) else str(e)
                logger.error(f"定时注册触发失败: {err_message}")
                with scheduled_registration_lock:
                    scheduled_registration["total_runs"] = int(scheduled_registration.get("total_runs") or 0) + 1
                    scheduled_registration["failed_runs"] = int(scheduled_registration.get("failed_runs") or 0) + 1
                    scheduled_registration["last_error"] = err_message
    except asyncio.CancelledError:
        logger.info("定时注册调度器已停止")
        raise
    finally:
        scheduled_registration_runner = None


def _run_sync_registration_task(task_uuid: str, email_service_type: str, proxy: Optional[str], email_service_config: Optional[dict], email_service_id: Optional[int] = None, log_prefix: str = "", batch_id: str = "", auto_upload_cpa: bool = False, cpa_service_ids: List[int] = None, auto_upload_sub2api: bool = False, sub2api_service_ids: List[int] = None, auto_upload_tm: bool = False, tm_service_ids: List[int] = None):
    """
    在线程池中执行的同步注册任务

    这个函数会被 run_in_executor 调用，运行在独立线程中
    """
    with get_db() as db:
        try:
            # 检查是否已取消
            if task_manager.is_cancelled(task_uuid):
                logger.info(f"任务 {task_uuid} 已取消，跳过执行")
                return

            # 更新任务状态为运行中
            task = crud.update_registration_task(
                db, task_uuid,
                status="running",
                started_at=datetime.utcnow()
            )

            if not task:
                logger.error(f"任务不存在: {task_uuid}")
                return

            # 更新 TaskManager 状态
            task_manager.update_status(task_uuid, "running")

            # 确定使用的代理
            # 如果前端传入了代理参数，使用传入的
            # 否则从代理列表或系统设置中获取
            actual_proxy_url = proxy
            proxy_id = None

            if not actual_proxy_url:
                actual_proxy_url, proxy_id = get_proxy_for_registration(db)
                if actual_proxy_url:
                    logger.info(f"任务 {task_uuid} 使用代理: {actual_proxy_url[:50]}...")

            # 更新任务的代理记录
            crud.update_registration_task(db, task_uuid, proxy=actual_proxy_url)

            # 创建邮箱服务
            service_type = EmailServiceType(email_service_type)
            settings = get_settings()

            # 优先使用数据库中配置的邮箱服务
            if email_service_id:
                from ...database.models import EmailService as EmailServiceModel
                db_service = db.query(EmailServiceModel).filter(
                    EmailServiceModel.id == email_service_id,
                    EmailServiceModel.enabled == True
                ).first()

                if db_service:
                    service_type = EmailServiceType(db_service.service_type)
                    config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
                    # 更新任务关联的邮箱服务
                    crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                    logger.info(f"使用数据库邮箱服务: {db_service.name} (ID: {db_service.id}, 类型: {service_type.value})")
                else:
                    raise ValueError(f"邮箱服务不存在或已禁用: {email_service_id}")
            else:
                # 使用默认配置或传入的配置
                if service_type == EmailServiceType.TEMPMAIL:
                    config = {
                        "base_url": settings.tempmail_base_url,
                        "timeout": settings.tempmail_timeout,
                        "max_retries": settings.tempmail_max_retries,
                        "proxy_url": actual_proxy_url,
                    }
                elif service_type == EmailServiceType.MOE_MAIL:
                    # 检查数据库中是否有可用的自定义域名服务
                    from ...database.models import EmailService as EmailServiceModel
                    db_service = db.query(EmailServiceModel).filter(
                        EmailServiceModel.service_type == "moe_mail",
                        EmailServiceModel.enabled == True
                    ).order_by(EmailServiceModel.priority.asc()).first()

                    if db_service and db_service.config:
                        config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
                        crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                        logger.info(f"使用数据库自定义域名服务: {db_service.name}")
                    elif settings.custom_domain_base_url and settings.custom_domain_api_key:
                        config = {
                            "base_url": settings.custom_domain_base_url,
                            "api_key": settings.custom_domain_api_key.get_secret_value() if settings.custom_domain_api_key else "",
                            "proxy_url": actual_proxy_url,
                        }
                    else:
                        raise ValueError("没有可用的自定义域名邮箱服务，请先在设置中配置")
                elif service_type == EmailServiceType.OUTLOOK:
                    # 检查数据库中是否有可用的 Outlook 账户
                    from ...database.models import EmailService as EmailServiceModel, Account
                    # 获取所有启用的 Outlook 服务
                    outlook_services = db.query(EmailServiceModel).filter(
                        EmailServiceModel.service_type == "outlook",
                        EmailServiceModel.enabled == True
                    ).order_by(EmailServiceModel.priority.asc()).all()

                    if not outlook_services:
                        raise ValueError("没有可用的 Outlook 账户，请先在设置中导入账户")

                    # 找到一个未注册的 Outlook 账户
                    selected_service = None
                    for svc in outlook_services:
                        email = svc.config.get("email") if svc.config else None
                        if not email:
                            continue
                        # 检查是否已在 accounts 表中注册
                        existing = db.query(Account).filter(Account.email == email).first()
                        if not existing:
                            selected_service = svc
                            logger.info(f"选择未注册的 Outlook 账户: {email}")
                            break
                        else:
                            logger.info(f"跳过已注册的 Outlook 账户: {email}")

                    if selected_service and selected_service.config:
                        config = selected_service.config.copy()
                        crud.update_registration_task(db, task_uuid, email_service_id=selected_service.id)
                        logger.info(f"使用数据库 Outlook 账户: {selected_service.name}")
                    else:
                        raise ValueError("所有 Outlook 账户都已注册过 OpenAI 账号，请添加新的 Outlook 账户")
                elif service_type == EmailServiceType.DUCK_MAIL:
                    from ...database.models import EmailService as EmailServiceModel

                    db_service = db.query(EmailServiceModel).filter(
                        EmailServiceModel.service_type == "duck_mail",
                        EmailServiceModel.enabled == True
                    ).order_by(EmailServiceModel.priority.asc()).first()

                    if db_service and db_service.config:
                        config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
                        crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                        logger.info(f"使用数据库 DuckMail 服务: {db_service.name}")
                    else:
                        raise ValueError("没有可用的 DuckMail 邮箱服务，请先在邮箱服务页面添加服务")
                elif service_type == EmailServiceType.FREEMAIL:
                    from ...database.models import EmailService as EmailServiceModel

                    db_service = db.query(EmailServiceModel).filter(
                        EmailServiceModel.service_type == "freemail",
                        EmailServiceModel.enabled == True
                    ).order_by(EmailServiceModel.priority.asc()).first()

                    if db_service and db_service.config:
                        config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
                        crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                        logger.info(f"使用数据库 Freemail 服务: {db_service.name}")
                    else:
                        raise ValueError("没有可用的 Freemail 邮箱服务，请先在邮箱服务页面添加服务")
                elif service_type == EmailServiceType.IMAP_MAIL:
                    from ...database.models import EmailService as EmailServiceModel

                    db_service = db.query(EmailServiceModel).filter(
                        EmailServiceModel.service_type == "imap_mail",
                        EmailServiceModel.enabled == True
                    ).order_by(EmailServiceModel.priority.asc()).first()

                    if db_service and db_service.config:
                        config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
                        crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                        logger.info(f"使用数据库 IMAP 邮箱服务: {db_service.name}")
                    else:
                        raise ValueError("没有可用的 IMAP 邮箱服务，请先在邮箱服务中添加")
                else:
                    config = email_service_config or {}

            email_service = EmailServiceFactory.create(service_type, config)

            # 创建注册引擎 - 使用 TaskManager 的日志回调
            log_callback = task_manager.create_log_callback(task_uuid, prefix=log_prefix, batch_id=batch_id)

            engine = RegistrationEngine(
                email_service=email_service,
                proxy_url=actual_proxy_url,
                callback_logger=log_callback,
                task_uuid=task_uuid
            )

            # 执行注册
            result = engine.run()

            if result.success:
                # 更新代理使用时间
                update_proxy_usage(db, proxy_id)

                # 保存到数据库
                engine.save_to_database(result)

                # 自动上传到 CPA（可多服务）
                if auto_upload_cpa:
                    try:
                        from ...core.upload.cpa_upload import upload_to_cpa, generate_token_json
                        from ...database.models import Account as AccountModel
                        saved_account = db.query(AccountModel).filter_by(email=result.email).first()
                        if saved_account and saved_account.access_token:
                            token_data = generate_token_json(saved_account)
                            _cpa_ids = cpa_service_ids or []
                            if not _cpa_ids:
                                # 未指定则取所有启用的服务
                                _cpa_ids = [s.id for s in crud.get_cpa_services(db, enabled=True)]
                            if not _cpa_ids:
                                log_callback("[CPA] 无可用 CPA 服务，跳过上传")
                            for _sid in _cpa_ids:
                                try:
                                    _svc = crud.get_cpa_service_by_id(db, _sid)
                                    if not _svc:
                                        continue
                                    log_callback(f"[CPA] 正在把账号打包发往服务站: {_svc.name}")
                                    _ok, _msg = upload_to_cpa(token_data, api_url=_svc.api_url, api_token=_svc.api_token)
                                    if _ok:
                                        saved_account.cpa_uploaded = True
                                        saved_account.cpa_uploaded_at = datetime.utcnow()
                                        db.commit()
                                        log_callback(f"[CPA] 投递成功，服务站已签收: {_svc.name}")
                                    else:
                                        log_callback(f"[CPA] 上传失败({_svc.name}): {_msg}")
                                except Exception as _e:
                                    log_callback(f"[CPA] 异常({_sid}): {_e}")
                    except Exception as cpa_err:
                        log_callback(f"[CPA] 上传异常: {cpa_err}")

                # 自动上传到 Sub2API（可多服务）
                if auto_upload_sub2api:
                    try:
                        from ...core.upload.sub2api_upload import upload_to_sub2api
                        from ...database.models import Account as AccountModel
                        saved_account = db.query(AccountModel).filter_by(email=result.email).first()
                        if saved_account and saved_account.access_token:
                            _s2a_ids = sub2api_service_ids or []
                            if not _s2a_ids:
                                _s2a_ids = [s.id for s in crud.get_sub2api_services(db, enabled=True)]
                            if not _s2a_ids:
                                log_callback("[Sub2API] 无可用 Sub2API 服务，跳过上传")
                            for _sid in _s2a_ids:
                                try:
                                    _svc = crud.get_sub2api_service_by_id(db, _sid)
                                    if not _svc:
                                        continue
                                    log_callback(f"[Sub2API] 正在把账号发往服务站: {_svc.name}")
                                    _ok, _msg = upload_to_sub2api([saved_account], _svc.api_url, _svc.api_key)
                                    log_callback(f"[Sub2API] {'成功' if _ok else '失败'}({_svc.name}): {_msg}")
                                except Exception as _e:
                                    log_callback(f"[Sub2API] 异常({_sid}): {_e}")
                    except Exception as s2a_err:
                        log_callback(f"[Sub2API] 上传异常: {s2a_err}")

                # 自动上传到 Team Manager（可多服务）
                if auto_upload_tm:
                    try:
                        from ...core.upload.team_manager_upload import upload_to_team_manager
                        from ...database.models import Account as AccountModel
                        saved_account = db.query(AccountModel).filter_by(email=result.email).first()
                        if saved_account and saved_account.access_token:
                            _tm_ids = tm_service_ids or []
                            if not _tm_ids:
                                _tm_ids = [s.id for s in crud.get_tm_services(db, enabled=True)]
                            if not _tm_ids:
                                log_callback("[TM] 无可用 Team Manager 服务，跳过上传")
                            for _sid in _tm_ids:
                                try:
                                    _svc = crud.get_tm_service_by_id(db, _sid)
                                    if not _svc:
                                        continue
                                    log_callback(f"[TM] 正在把账号发往服务站: {_svc.name}")
                                    _ok, _msg = upload_to_team_manager(saved_account, _svc.api_url, _svc.api_key)
                                    log_callback(f"[TM] {'成功' if _ok else '失败'}({_svc.name}): {_msg}")
                                except Exception as _e:
                                    log_callback(f"[TM] 异常({_sid}): {_e}")
                    except Exception as tm_err:
                        log_callback(f"[TM] 上传异常: {tm_err}")

                # 更新任务状态
                crud.update_registration_task(
                    db, task_uuid,
                    status="completed",
                    completed_at=datetime.utcnow(),
                    result=result.to_dict()
                )

                # 更新 TaskManager 状态
                task_manager.update_status(task_uuid, "completed", email=result.email)

                logger.info(f"注册任务完成: {task_uuid}, 邮箱: {result.email}")
            else:
                # 更新任务状态为失败
                crud.update_registration_task(
                    db, task_uuid,
                    status="failed",
                    completed_at=datetime.utcnow(),
                    error_message=result.error_message
                )

                # 更新 TaskManager 状态
                task_manager.update_status(task_uuid, "failed", error=result.error_message)

                logger.warning(f"注册任务失败: {task_uuid}, 原因: {result.error_message}")

        except Exception as e:
            logger.error(f"注册任务异常: {task_uuid}, 错误: {e}")

            try:
                with get_db() as db:
                    crud.update_registration_task(
                        db, task_uuid,
                        status="failed",
                        completed_at=datetime.utcnow(),
                        error_message=str(e)
                    )

                # 更新 TaskManager 状态
                task_manager.update_status(task_uuid, "failed", error=str(e))
            except:
                pass


async def run_registration_task(task_uuid: str, email_service_type: str, proxy: Optional[str], email_service_config: Optional[dict], email_service_id: Optional[int] = None, log_prefix: str = "", batch_id: str = "", auto_upload_cpa: bool = False, cpa_service_ids: List[int] = None, auto_upload_sub2api: bool = False, sub2api_service_ids: List[int] = None, auto_upload_tm: bool = False, tm_service_ids: List[int] = None):
    """
    异步执行注册任务

    使用 run_in_executor 将同步任务放入线程池执行，避免阻塞主事件循环
    """
    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

    # 初始化 TaskManager 状态
    task_manager.update_status(task_uuid, "pending")
    task_manager.add_log(task_uuid, f"{log_prefix} [系统] 任务 {task_uuid[:8]} 已加入队列" if log_prefix else f"[系统] 任务 {task_uuid[:8]} 已加入队列")

    try:
        # 在线程池中执行同步任务（传入 log_prefix 和 batch_id 供回调使用）
        await loop.run_in_executor(
            task_manager.executor,
            _run_sync_registration_task,
            task_uuid,
            email_service_type,
            proxy,
            email_service_config,
            email_service_id,
            log_prefix,
            batch_id,
            auto_upload_cpa,
            cpa_service_ids or [],
            auto_upload_sub2api,
            sub2api_service_ids or [],
            auto_upload_tm,
            tm_service_ids or [],
        )
    except Exception as e:
        logger.error(f"线程池执行异常: {task_uuid}, 错误: {e}")
        task_manager.add_log(task_uuid, f"[错误] 线程池执行异常: {str(e)}")
        task_manager.update_status(task_uuid, "failed", error=str(e))


def _init_batch_state(batch_id: str, task_uuids: List[str]):
    """初始化批量任务内存状态"""
    task_manager.init_batch(batch_id, len(task_uuids))
    batch_tasks[batch_id] = {
        "total": len(task_uuids),
        "completed": 0,
        "success": 0,
        "failed": 0,
        "cancelled": False,
        "task_uuids": task_uuids,
        "current_index": 0,
        "logs": [],
        "finished": False
    }


def _make_batch_helpers(batch_id: str):
    """返回 add_batch_log 和 update_batch_status 辅助函数"""
    def add_batch_log(msg: str):
        batch_tasks[batch_id]["logs"].append(msg)
        task_manager.add_batch_log(batch_id, msg)

    def update_batch_status(**kwargs):
        for key, value in kwargs.items():
            if key in batch_tasks[batch_id]:
                batch_tasks[batch_id][key] = value
        task_manager.update_batch_status(batch_id, **kwargs)

    return add_batch_log, update_batch_status


async def run_batch_parallel(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    concurrency: int,
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
):
    """
    并行模式：所有任务同时提交，Semaphore 控制最大并发数
    """
    _init_batch_state(batch_id, task_uuids)
    add_batch_log, update_batch_status = _make_batch_helpers(batch_id)
    semaphore = asyncio.Semaphore(concurrency)
    counter_lock = asyncio.Lock()
    add_batch_log(f"[系统] 并行模式启动，并发数: {concurrency}，总任务: {len(task_uuids)}")

    async def _run_one(idx: int, uuid: str):
        prefix = f"[任务{idx + 1}]"
        async with semaphore:
            await run_registration_task(
                uuid, email_service_type, proxy, email_service_config, email_service_id,
                log_prefix=prefix, batch_id=batch_id,
                auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids or [],
                auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids or [],
                auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids or [],
            )
        with get_db() as db:
            t = crud.get_registration_task(db, uuid)
            if t:
                async with counter_lock:
                    new_completed = batch_tasks[batch_id]["completed"] + 1
                    new_success = batch_tasks[batch_id]["success"]
                    new_failed = batch_tasks[batch_id]["failed"]
                    if t.status == "completed":
                        new_success += 1
                        add_batch_log(f"{prefix} [成功] 注册成功")
                    elif t.status == "failed":
                        new_failed += 1
                        add_batch_log(f"{prefix} [失败] 注册失败: {t.error_message}")
                    update_batch_status(completed=new_completed, success=new_success, failed=new_failed)

    try:
        await asyncio.gather(*[_run_one(i, u) for i, u in enumerate(task_uuids)], return_exceptions=True)
        if not task_manager.is_batch_cancelled(batch_id):
            add_batch_log(f"[完成] 批量任务完成！成功: {batch_tasks[batch_id]['success']}, 失败: {batch_tasks[batch_id]['failed']}")
            update_batch_status(finished=True, status="completed")
        else:
            update_batch_status(finished=True, status="cancelled")
    except Exception as e:
        logger.error(f"批量任务 {batch_id} 异常: {e}")
        add_batch_log(f"[错误] 批量任务异常: {str(e)}")
        update_batch_status(finished=True, status="failed")
    finally:
        batch_tasks[batch_id]["finished"] = True


async def run_batch_pipeline(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    interval_min: int,
    interval_max: int,
    concurrency: int,
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
):
    """
    流水线模式：每隔 interval 秒启动一个新任务，Semaphore 限制最大并发数
    """
    _init_batch_state(batch_id, task_uuids)
    add_batch_log, update_batch_status = _make_batch_helpers(batch_id)
    semaphore = asyncio.Semaphore(concurrency)
    counter_lock = asyncio.Lock()
    running_tasks_list = []
    add_batch_log(f"[系统] 流水线模式启动，并发数: {concurrency}，总任务: {len(task_uuids)}")

    async def _run_and_release(idx: int, uuid: str, pfx: str):
        try:
            await run_registration_task(
                uuid, email_service_type, proxy, email_service_config, email_service_id,
                log_prefix=pfx, batch_id=batch_id,
                auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids or [],
                auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids or [],
                auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids or [],
            )
            with get_db() as db:
                t = crud.get_registration_task(db, uuid)
                if t:
                    async with counter_lock:
                        new_completed = batch_tasks[batch_id]["completed"] + 1
                        new_success = batch_tasks[batch_id]["success"]
                        new_failed = batch_tasks[batch_id]["failed"]
                        if t.status == "completed":
                            new_success += 1
                            add_batch_log(f"{pfx} [成功] 注册成功")
                        elif t.status == "failed":
                            new_failed += 1
                            add_batch_log(f"{pfx} [失败] 注册失败: {t.error_message}")
                        update_batch_status(completed=new_completed, success=new_success, failed=new_failed)
        finally:
            semaphore.release()

    try:
        for i, task_uuid in enumerate(task_uuids):
            if task_manager.is_batch_cancelled(batch_id) or batch_tasks[batch_id]["cancelled"]:
                with get_db() as db:
                    for remaining_uuid in task_uuids[i:]:
                        crud.update_registration_task(db, remaining_uuid, status="cancelled")
                add_batch_log("[取消] 批量任务已取消")
                update_batch_status(finished=True, status="cancelled")
                break

            update_batch_status(current_index=i)
            await semaphore.acquire()
            prefix = f"[任务{i + 1}]"
            add_batch_log(f"{prefix} 开始注册...")
            t = asyncio.create_task(_run_and_release(i, task_uuid, prefix))
            running_tasks_list.append(t)

            if i < len(task_uuids) - 1 and not task_manager.is_batch_cancelled(batch_id):
                wait_time = random.randint(interval_min, interval_max)
                logger.info(f"批量任务 {batch_id}: 等待 {wait_time} 秒后启动下一个任务")
                await asyncio.sleep(wait_time)

        if running_tasks_list:
            await asyncio.gather(*running_tasks_list, return_exceptions=True)

        if not task_manager.is_batch_cancelled(batch_id):
            add_batch_log(f"[完成] 批量任务完成！成功: {batch_tasks[batch_id]['success']}, 失败: {batch_tasks[batch_id]['failed']}")
            update_batch_status(finished=True, status="completed")
    except Exception as e:
        logger.error(f"批量任务 {batch_id} 异常: {e}")
        add_batch_log(f"[错误] 批量任务异常: {str(e)}")
        update_batch_status(finished=True, status="failed")
    finally:
        batch_tasks[batch_id]["finished"] = True


async def run_batch_registration(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    interval_min: int,
    interval_max: int,
    concurrency: int = 1,
    mode: str = "pipeline",
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
):
    """根据 mode 分发到并行或流水线执行"""
    if mode == "parallel":
        await run_batch_parallel(
            batch_id, task_uuids, email_service_type, proxy,
            email_service_config, email_service_id, concurrency,
            auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids,
            auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids,
            auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids,
        )
    else:
        await run_batch_pipeline(
            batch_id, task_uuids, email_service_type, proxy,
            email_service_config, email_service_id,
            interval_min, interval_max, concurrency,
            auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids,
            auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids,
            auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids,
        )


# ============== API Endpoints ==============

@router.post("/start", response_model=RegistrationTaskResponse)
async def start_registration(
    request: RegistrationTaskCreate,
    background_tasks: BackgroundTasks
):
    """
    启动注册任务

    - email_service_type: 邮箱服务类型 (tempmail, outlook, moe_mail)
    - proxy: 代理地址
    - email_service_config: 邮箱服务配置（outlook 需要提供账户信息）
    """
    return await _enqueue_single_registration(request, background_tasks)


@router.post("/batch", response_model=BatchRegistrationResponse)
async def start_batch_registration(
    request: BatchRegistrationRequest,
    background_tasks: BackgroundTasks
):
    """
    启动批量注册任务

    - count: 注册数量 (1-100)
    - email_service_type: 邮箱服务类型
    - proxy: 代理地址
    - interval_min: 最小间隔秒数
    - interval_max: 最大间隔秒数
    """
    return await _enqueue_batch_registration(request, background_tasks)


@router.get("/batch/{batch_id}")
async def get_batch_status(batch_id: str):
    """获取批量任务状态"""
    if batch_id not in batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    batch = batch_tasks[batch_id]
    return {
        "batch_id": batch_id,
        "total": batch["total"],
        "completed": batch["completed"],
        "success": batch["success"],
        "failed": batch["failed"],
        "current_index": batch["current_index"],
        "cancelled": batch["cancelled"],
        "finished": batch.get("finished", False),
        "progress": f"{batch['completed']}/{batch['total']}"
    }


@router.post("/batch/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    """取消批量任务"""
    if batch_id not in batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    batch = batch_tasks[batch_id]
    if batch.get("finished"):
        raise HTTPException(status_code=400, detail="批量任务已完成")

    batch["cancelled"] = True
    task_manager.cancel_batch(batch_id)
    return {"success": True, "message": "批量任务取消请求已提交，正在让它们有序收工"}


@router.get("/schedule/status", response_model=ScheduledRegistrationStatusResponse)
async def get_scheduled_registration_status():
    """获取定时注册状态。"""
    return _get_scheduled_registration_status()


@router.post("/schedule/start", response_model=ScheduledRegistrationStatusResponse)
async def start_scheduled_registration(request: ScheduledRegistrationRequest):
    """启动（或覆盖）定时注册任务。"""
    global scheduled_registration_runner

    if request.registration_mode not in ("single", "batch"):
        raise HTTPException(status_code=400, detail="定时注册模式必须为 single 或 batch")

    _validate_email_service_type(request.email_service_type)
    if request.registration_mode == "batch":
        _validate_batch_params(
            count=request.count,
            interval_min=request.interval_min,
            interval_max=request.interval_max,
            concurrency=request.concurrency,
            mode=request.mode,
        )

    # 兼容旧参数：schedule_interval_minutes -> schedule_cron
    try:
        if request.schedule_interval_minutes is not None:
            schedule_cron = _legacy_interval_to_cron(int(request.schedule_interval_minutes))
        else:
            schedule_cron = request.schedule_cron
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    schedule_cron = _validate_schedule_cron_or_raise(schedule_cron)
    payload_to_store = request.model_dump(exclude_none=True)
    payload_to_store["schedule_cron"] = schedule_cron
    payload_to_store.pop("schedule_interval_minutes", None)

    now = _now_in_schedule_tz()
    first_run_at = now if request.run_immediately else _get_next_run_from_cron(schedule_cron, now)
    tz_name = _get_schedule_timezone_name()

    with scheduled_registration_lock:
        scheduled_registration["enabled"] = True
        scheduled_registration["schedule_cron"] = schedule_cron
        scheduled_registration["timezone"] = tz_name
        scheduled_registration["registration_mode"] = request.registration_mode
        scheduled_registration["payload"] = payload_to_store
        scheduled_registration["next_run_at"] = first_run_at
        scheduled_registration["last_run_at"] = None
        scheduled_registration["last_error"] = None
        scheduled_registration["total_runs"] = 0
        scheduled_registration["success_runs"] = 0
        scheduled_registration["failed_runs"] = 0
        scheduled_registration["skipped_runs"] = 0
        scheduled_registration["active_task_uuid"] = None
        scheduled_registration["active_batch_id"] = None

    try:
        _save_scheduled_registration_settings(
            enabled=True,
            schedule_cron=schedule_cron,
            payload=payload_to_store,
        )
    except Exception as e:
        logger.error(f"保存定时注册设置失败: {e}")
        raise HTTPException(status_code=500, detail=f"保存定时注册设置失败: {e}")

    # 修改定时配置时，明确重启调度器，避免沿用旧触发链
    old_runner = scheduled_registration_runner
    if old_runner and not old_runner.done():
        old_runner.cancel()
        try:
            await old_runner
        except asyncio.CancelledError:
            pass

    scheduled_registration_runner = asyncio.create_task(_scheduled_registration_loop())

    logger.info(
        "定时注册已启动：cron=%s, tz=%s, mode=%s, first_run_at=%s",
        schedule_cron,
        tz_name,
        request.registration_mode,
        first_run_at.isoformat()
    )
    return _get_scheduled_registration_status()


@router.post("/schedule/stop", response_model=ScheduledRegistrationStatusResponse)
async def stop_scheduled_registration():
    """停止定时注册任务。"""
    global scheduled_registration_runner

    with scheduled_registration_lock:
        schedule_cron = scheduled_registration.get("schedule_cron") or "*/30 * * * *"
        payload = dict(scheduled_registration.get("payload") or {})
        scheduled_registration["enabled"] = False
        scheduled_registration["next_run_at"] = None

    try:
        _save_scheduled_registration_settings(
            enabled=False,
            schedule_cron=schedule_cron,
            payload=payload,
        )
    except Exception as e:
        logger.error(f"保存定时注册设置失败: {e}")
        raise HTTPException(status_code=500, detail=f"保存定时注册设置失败: {e}")

    runner = scheduled_registration_runner
    if runner and not runner.done():
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass

    logger.info("定时注册已停止")
    return _get_scheduled_registration_status()


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
):
    """获取任务列表"""
    with get_db() as db:
        query = db.query(RegistrationTask)

        if status:
            query = query.filter(RegistrationTask.status == status)

        total = query.count()
        offset = (page - 1) * page_size
        tasks = query.order_by(RegistrationTask.created_at.desc()).offset(offset).limit(page_size).all()

        return TaskListResponse(
            total=total,
            tasks=[task_to_response(t) for t in tasks]
        )


@router.get("/tasks/{task_uuid}", response_model=RegistrationTaskResponse)
async def get_task(task_uuid: str):
    """获取任务详情"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task_to_response(task)


@router.get("/tasks/{task_uuid}/logs")
async def get_task_logs(task_uuid: str):
    """获取任务日志"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        logs = task.logs or ""
        return {
            "task_uuid": task_uuid,
            "status": task.status,
            "logs": logs.split("\n") if logs else []
        }


@router.post("/tasks/{task_uuid}/cancel")
async def cancel_task(task_uuid: str):
    """取消任务"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        if task.status not in ["pending", "running"]:
            raise HTTPException(status_code=400, detail="任务已完成或已取消")

        task = crud.update_registration_task(db, task_uuid, status="cancelled")

        return {"success": True, "message": "任务已取消"}


@router.delete("/tasks/{task_uuid}")
async def delete_task(task_uuid: str):
    """删除任务"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        if task.status == "running":
            raise HTTPException(status_code=400, detail="无法删除运行中的任务")

        crud.delete_registration_task(db, task_uuid)

        return {"success": True, "message": "任务已删除"}


@router.get("/stats")
async def get_registration_stats():
    """获取注册统计信息"""
    with get_db() as db:
        from sqlalchemy import func

        # 按状态统计
        status_stats = db.query(
            RegistrationTask.status,
            func.count(RegistrationTask.id)
        ).group_by(RegistrationTask.status).all()

        # 今日注册数
        today = datetime.utcnow().date()
        today_count = db.query(func.count(RegistrationTask.id)).filter(
            func.date(RegistrationTask.created_at) == today
        ).scalar()

        return {
            "by_status": {status: count for status, count in status_stats},
            "today_count": today_count
        }


@router.get("/available-services")
async def get_available_email_services():
    """
    获取可用于注册的邮箱服务列表

    返回所有已启用的邮箱服务，包括：
    - tempmail: 临时邮箱（无需配置）
    - outlook: 已导入的 Outlook 账户
    - moe_mail: 已配置的自定义域名服务
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...config.settings import get_settings

    settings = get_settings()
    result = {
        "tempmail": {
            "available": True,
            "count": 1,
            "services": [{
                "id": None,
                "name": "Tempmail.lol",
                "type": "tempmail",
                "description": "临时邮箱，自动创建"
            }]
        },
        "outlook": {
            "available": False,
            "count": 0,
            "services": []
        },
        "moe_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "temp_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "duck_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "freemail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "imap_mail": {
            "available": False,
            "count": 0,
            "services": []
        }
    }

    with get_db() as db:
        # 获取 Outlook 账户
        outlook_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "outlook",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in outlook_services:
            config = service.config or {}
            result["outlook"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "outlook",
                "has_oauth": bool(config.get("client_id") and config.get("refresh_token")),
                "priority": service.priority
            })

        result["outlook"]["count"] = len(outlook_services)
        result["outlook"]["available"] = len(outlook_services) > 0

        # 获取自定义域名服务
        custom_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "moe_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in custom_services:
            config = service.config or {}
            result["moe_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "moe_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        result["moe_mail"]["count"] = len(custom_services)
        result["moe_mail"]["available"] = len(custom_services) > 0

        # 如果数据库中没有自定义域名服务，检查 settings
        if not result["moe_mail"]["available"]:
            if settings.custom_domain_base_url and settings.custom_domain_api_key:
                result["moe_mail"]["available"] = True
                result["moe_mail"]["count"] = 1
                result["moe_mail"]["services"].append({
                    "id": None,
                    "name": "默认自定义域名服务",
                    "type": "moe_mail",
                    "from_settings": True
                })

        # 获取 TempMail 服务（自部署 Cloudflare Worker 临时邮箱）
        temp_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "temp_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in temp_mail_services:
            config = service.config or {}
            result["temp_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "temp_mail",
                "domain": config.get("domain"),
                "priority": service.priority
            })

        result["temp_mail"]["count"] = len(temp_mail_services)
        result["temp_mail"]["available"] = len(temp_mail_services) > 0

        duck_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "duck_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in duck_mail_services:
            config = service.config or {}
            result["duck_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "duck_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        result["duck_mail"]["count"] = len(duck_mail_services)
        result["duck_mail"]["available"] = len(duck_mail_services) > 0

        freemail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "freemail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in freemail_services:
            config = service.config or {}
            result["freemail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "freemail",
                "domain": config.get("domain"),
                "priority": service.priority
            })

        result["freemail"]["count"] = len(freemail_services)
        result["freemail"]["available"] = len(freemail_services) > 0

        imap_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "imap_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in imap_mail_services:
            config = service.config or {}
            result["imap_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "imap_mail",
                "email": config.get("email"),
                "host": config.get("host"),
                "priority": service.priority
            })

        result["imap_mail"]["count"] = len(imap_mail_services)
        result["imap_mail"]["available"] = len(imap_mail_services) > 0

    return result


# ============== Outlook 批量注册 API ==============

@router.get("/outlook-accounts", response_model=OutlookAccountsListResponse)
async def get_outlook_accounts_for_registration():
    """
    获取可用于注册的 Outlook 账户列表

    返回所有已启用的 Outlook 服务，并检查每个邮箱是否已在 accounts 表中注册
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...database.models import Account

    with get_db() as db:
        # 获取所有启用的 Outlook 服务
        outlook_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "outlook",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        accounts = []
        registered_count = 0
        unregistered_count = 0

        for service in outlook_services:
            config = service.config or {}
            email = config.get("email") or service.name

            # 检查是否已注册（查询 accounts 表）
            existing_account = db.query(Account).filter(
                Account.email == email
            ).first()

            is_registered = existing_account is not None
            if is_registered:
                registered_count += 1
            else:
                unregistered_count += 1

            accounts.append(OutlookAccountForRegistration(
                id=service.id,
                email=email,
                name=service.name,
                has_oauth=bool(config.get("client_id") and config.get("refresh_token")),
                is_registered=is_registered,
                registered_account_id=existing_account.id if existing_account else None
            ))

        return OutlookAccountsListResponse(
            total=len(accounts),
            registered_count=registered_count,
            unregistered_count=unregistered_count,
            accounts=accounts
        )


async def run_outlook_batch_registration(
    batch_id: str,
    service_ids: List[int],
    skip_registered: bool,
    proxy: Optional[str],
    interval_min: int,
    interval_max: int,
    concurrency: int = 1,
    mode: str = "pipeline",
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
):
    """
    异步执行 Outlook 批量注册任务，复用通用并发逻辑

    将每个 service_id 映射为一个独立的 task_uuid，然后调用
    run_batch_registration 的并发逻辑
    """
    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

    # 预先为每个 service_id 创建注册任务记录
    task_uuids = []
    with get_db() as db:
        for service_id in service_ids:
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=proxy,
                email_service_id=service_id
            )
            task_uuids.append(task_uuid)

    # 复用通用并发逻辑（outlook 服务类型，每个任务通过 email_service_id 定位账户）
    await run_batch_registration(
        batch_id=batch_id,
        task_uuids=task_uuids,
        email_service_type="outlook",
        proxy=proxy,
        email_service_config=None,
        email_service_id=None,   # 每个任务已绑定了独立的 email_service_id
        interval_min=interval_min,
        interval_max=interval_max,
        concurrency=concurrency,
        mode=mode,
        auto_upload_cpa=auto_upload_cpa,
        cpa_service_ids=cpa_service_ids,
        auto_upload_sub2api=auto_upload_sub2api,
        sub2api_service_ids=sub2api_service_ids,
        auto_upload_tm=auto_upload_tm,
        tm_service_ids=tm_service_ids,
    )


@router.post("/outlook-batch", response_model=OutlookBatchRegistrationResponse)
async def start_outlook_batch_registration(
    request: OutlookBatchRegistrationRequest,
    background_tasks: BackgroundTasks
):
    """
    启动 Outlook 批量注册任务

    - service_ids: 选中的 EmailService ID 列表
    - skip_registered: 是否自动跳过已注册邮箱（默认 True）
    - proxy: 代理地址
    - interval_min: 最小间隔秒数
    - interval_max: 最大间隔秒数
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...database.models import Account

    # 验证参数
    if not request.service_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个 Outlook 账户")

    if request.interval_min < 0 or request.interval_max < request.interval_min:
        raise HTTPException(status_code=400, detail="间隔时间参数无效")

    if not 1 <= request.concurrency <= 50:
        raise HTTPException(status_code=400, detail="并发数必须在 1-50 之间")

    if request.mode not in ("parallel", "pipeline"):
        raise HTTPException(status_code=400, detail="模式必须为 parallel 或 pipeline")

    # 过滤掉已注册的邮箱
    actual_service_ids = request.service_ids
    skipped_count = 0

    if request.skip_registered:
        actual_service_ids = []
        with get_db() as db:
            for service_id in request.service_ids:
                service = db.query(EmailServiceModel).filter(
                    EmailServiceModel.id == service_id
                ).first()

                if not service:
                    continue

                config = service.config or {}
                email = config.get("email") or service.name

                # 检查是否已注册
                existing_account = db.query(Account).filter(
                    Account.email == email
                ).first()

                if existing_account:
                    skipped_count += 1
                else:
                    actual_service_ids.append(service_id)

    if not actual_service_ids:
        return OutlookBatchRegistrationResponse(
            batch_id="",
            total=len(request.service_ids),
            skipped=skipped_count,
            to_register=0,
            service_ids=[]
        )

    # 创建批量任务
    batch_id = str(uuid.uuid4())

    # 初始化批量任务状态
    batch_tasks[batch_id] = {
        "total": len(actual_service_ids),
        "completed": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "cancelled": False,
        "service_ids": actual_service_ids,
        "current_index": 0,
        "logs": [],
        "finished": False
    }

    # 在后台运行批量注册
    background_tasks.add_task(
        run_outlook_batch_registration,
        batch_id,
        actual_service_ids,
        request.skip_registered,
        request.proxy,
        request.interval_min,
        request.interval_max,
        request.concurrency,
        request.mode,
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
    )

    return OutlookBatchRegistrationResponse(
        batch_id=batch_id,
        total=len(request.service_ids),
        skipped=skipped_count,
        to_register=len(actual_service_ids),
        service_ids=actual_service_ids
    )


@router.get("/outlook-batch/{batch_id}")
async def get_outlook_batch_status(batch_id: str):
    """获取 Outlook 批量任务状态"""
    if batch_id not in batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    batch = batch_tasks[batch_id]
    return {
        "batch_id": batch_id,
        "total": batch["total"],
        "completed": batch["completed"],
        "success": batch["success"],
        "failed": batch["failed"],
        "skipped": batch.get("skipped", 0),
        "current_index": batch["current_index"],
        "cancelled": batch["cancelled"],
        "finished": batch.get("finished", False),
        "logs": batch.get("logs", []),
        "progress": f"{batch['completed']}/{batch['total']}"
    }


@router.post("/outlook-batch/{batch_id}/cancel")
async def cancel_outlook_batch(batch_id: str):
    """取消 Outlook 批量任务"""
    if batch_id not in batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    batch = batch_tasks[batch_id]
    if batch.get("finished"):
        raise HTTPException(status_code=400, detail="批量任务已完成")

    # 同时更新两个系统的取消状态
    batch["cancelled"] = True
    task_manager.cancel_batch(batch_id)

    return {"success": True, "message": "批量任务取消请求已提交，正在让它们有序收工"}
