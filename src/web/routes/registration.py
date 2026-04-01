"""
注册任务 API 路由
"""

import asyncio
import logging
import uuid
import random
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple, Any

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, ConfigDict, Field

from ...config.constants import (
    RoleTag,
    normalize_role_tag,
    role_tag_to_account_label,
)
from ...database import crud
from ...database.session import get_db
from ...database.models import RegistrationTask, ScheduledRegistrationJob, Proxy
from ...core.register import (
    RegistrationEngine,
    RegistrationResult,
    RegistrationCancelledError,
)
from ...services import EmailServiceFactory, EmailServiceType
from ...config.settings import get_settings, Settings
from ...core.auto_registration import (
    add_auto_registration_log,
    get_auto_registration_inventory,
    get_auto_registration_logs,
    get_auto_registration_state,
    update_auto_registration_state,
)
from ...core.timezone_utils import utcnow_naive
from ..task_manager import task_manager
from ..schedule_utils import normalize_schedule_config, compute_next_run_at, describe_schedule

logger = logging.getLogger(__name__)
router = APIRouter()

# 任务存储（简单的内存存储，生产环境应使用 Redis）
running_tasks: dict = {}
# 批量任务存储
batch_tasks: Dict[str, dict] = {}


def _cancel_batch_tasks(batch_id: str) -> None:
    batch = batch_tasks.get(batch_id)
    if not batch:
        return

    for task_uuid in batch.get("task_uuids", []):
        task_manager.cancel_task(task_uuid)

    auto_state = get_auto_registration_state()
    if auto_state.get("current_batch_id") == batch_id:
        update_auto_registration_state(
            status="cancelling",
            message=f"自动补货取消中: {batch_id}",
        )
        add_auto_registration_log(f"[自动注册] 已提交补货批量任务取消请求: {batch_id}")


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
    cpa_service_ids: List[int] = []
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []
    auto_upload_new_api: bool = False
    new_api_service_ids: List[int] = []
    registration_type: str = RoleTag.CHILD.value


class BatchRegistrationRequest(BaseModel):
    """批量注册请求"""
    count: int = 1
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
    auto_upload_new_api: bool = False
    new_api_service_ids: List[int] = []
    registration_type: str = RoleTag.CHILD.value


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

    model_config = ConfigDict(from_attributes=True)


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
    auto_upload_new_api: bool = False
    new_api_service_ids: List[int] = []
    registration_type: str = RoleTag.CHILD.value


class OutlookBatchRegistrationResponse(BaseModel):
    """Outlook 批量注册响应"""
    batch_id: str
    total: int                   # 总数
    skipped: int                 # 跳过数（已注册）
    to_register: int             # 待注册数
    service_ids: List[int]       # 实际要注册的服务 ID


class ScheduledRegistrationRequest(BaseModel):
    """创建或更新计划注册任务请求"""
    name: str = Field(..., min_length=1, max_length=100)
    enabled: bool = True
    schedule_type: str
    schedule_config: Dict[str, Any]
    registration_config: Dict[str, Any]
    timezone: str = "local"


class ScheduledRegistrationJobResponse(BaseModel):
    """计划注册任务响应"""
    id: int
    job_uuid: str
    name: str
    enabled: bool
    status: str
    schedule_type: str
    schedule_config: Dict[str, Any]
    schedule_description: str
    registration_config: Dict[str, Any]
    timezone: Optional[str] = None
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    run_count: int
    consecutive_failures: int
    is_running: bool
    last_triggered_task_uuid: Optional[str] = None
    last_triggered_batch_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ScheduledRegistrationJobListResponse(BaseModel):
    """计划注册任务列表响应"""
    total: int
    jobs: List[ScheduledRegistrationJobResponse]


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


def scheduled_job_to_response(job: ScheduledRegistrationJob) -> ScheduledRegistrationJobResponse:
    """转换计划任务模型为响应"""
    schedule_config = job.schedule_config or {}
    return ScheduledRegistrationJobResponse(
        id=job.id,
        job_uuid=job.job_uuid,
        name=job.name,
        enabled=job.enabled,
        status=job.status,
        schedule_type=job.schedule_type,
        schedule_config=schedule_config,
        schedule_description=describe_schedule(job.schedule_type, schedule_config),
        registration_config=job.registration_config or {},
        timezone=job.timezone,
        next_run_at=job.next_run_at.isoformat() if job.next_run_at else None,
        last_run_at=job.last_run_at.isoformat() if job.last_run_at else None,
        last_success_at=job.last_success_at.isoformat() if job.last_success_at else None,
        last_error=job.last_error,
        run_count=job.run_count or 0,
        consecutive_failures=job.consecutive_failures or 0,
        is_running=bool(job.is_running),
        last_triggered_task_uuid=job.last_triggered_task_uuid,
        last_triggered_batch_id=job.last_triggered_batch_id,
        created_at=job.created_at.isoformat() if job.created_at else None,
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
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
    elif service_type == EmailServiceType.YYDS_MAIL:
        if 'domain' in normalized and 'default_domain' not in normalized:
            normalized['default_domain'] = normalized.pop('domain')
    elif service_type in (EmailServiceType.TEMP_MAIL, EmailServiceType.CLOUDMAIL, EmailServiceType.FREEMAIL):
        if 'default_domain' in normalized and 'domain' not in normalized:
            normalized['domain'] = normalized.pop('default_domain')
        if service_type == EmailServiceType.CLOUDMAIL and 'api_key' in normalized and 'admin_password' not in normalized:
            normalized['admin_password'] = normalized.pop('api_key')
    elif service_type == EmailServiceType.DUCK_MAIL:
        if 'domain' in normalized and 'default_domain' not in normalized:
            normalized['default_domain'] = normalized.pop('domain')
    elif service_type == EmailServiceType.LUCKMAIL:
        if 'domain' in normalized and 'preferred_domain' not in normalized:
            normalized['preferred_domain'] = normalized.pop('domain')

    if proxy_url and 'proxy_url' not in normalized:
        normalized['proxy_url'] = proxy_url

    return normalized


def _run_sync_registration_task(task_uuid: str, email_service_type: str, proxy: Optional[str], email_service_config: Optional[dict], email_service_id: Optional[int] = None, log_prefix: str = "", batch_id: str = "", auto_upload_cpa: bool = False, cpa_service_ids: List[int] = None, auto_upload_sub2api: bool = False, sub2api_service_ids: List[int] = None, auto_upload_tm: bool = False, tm_service_ids: List[int] = None, auto_upload_new_api: bool = False, new_api_service_ids: List[int] = None, registration_type: str = RoleTag.CHILD.value):
    """
    在线程池中执行的同步注册任务

    这个函数会被 run_in_executor 调用，运行在独立线程中
    """
    with get_db() as db:
        def _mark_cancelled(reason: str = "任务已取消") -> None:
            safe_reason = str(reason or "任务已取消").strip()
            crud.update_registration_task(
                db,
                task_uuid,
                status="cancelled",
                completed_at=utcnow_naive(),
                error_message=safe_reason,
            )
            task_manager.update_status(task_uuid, "cancelled", error=safe_reason)
            task_manager.add_log(task_uuid, f"{log_prefix} [取消] {safe_reason}" if log_prefix else f"[取消] {safe_reason}")
            logger.info("任务 %s 已取消: %s", task_uuid, safe_reason)

        def _raise_if_cancelled(reason: str = "任务已取消") -> None:
            if task_manager.is_cancelled(task_uuid):
                raise RegistrationCancelledError(reason)

        try:
            _raise_if_cancelled("任务在入队后收到取消请求，已跳过执行")

            # 更新任务状态为运行中
            task = crud.update_registration_task(
                db, task_uuid,
                status="running",
                started_at=utcnow_naive()
            )

            if not task:
                logger.error(f"任务不存在: {task_uuid}")
                return

            # 更新 TaskManager 状态
            task_manager.update_status(task_uuid, "running")
            _raise_if_cancelled("任务在启动后收到取消请求，停止执行")

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
            _raise_if_cancelled("任务在准备阶段收到取消请求，停止执行")

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
                    if not settings.tempmail_enabled:
                        raise ValueError("Tempmail.lol 渠道已禁用，请先在邮箱服务页面启用")
                    config = {
                        "base_url": settings.tempmail_base_url,
                        "timeout": settings.tempmail_timeout,
                        "max_retries": settings.tempmail_max_retries,
                        "proxy_url": actual_proxy_url,
                    }
                elif service_type == EmailServiceType.YYDS_MAIL:
                    api_key = settings.yyds_mail_api_key.get_secret_value() if settings.yyds_mail_api_key else ""
                    if not settings.yyds_mail_enabled or not api_key:
                        raise ValueError("YYDS Mail 渠道未启用或未配置 API Key，请先在邮箱服务页面配置")
                    config = {
                        "base_url": settings.yyds_mail_base_url,
                        "api_key": api_key,
                        "default_domain": settings.yyds_mail_default_domain,
                        "timeout": settings.yyds_mail_timeout,
                        "max_retries": settings.yyds_mail_max_retries,
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
                elif service_type == EmailServiceType.TEMP_MAIL:
                    from ...database.models import EmailService as EmailServiceModel

                    db_service = db.query(EmailServiceModel).filter(
                        EmailServiceModel.service_type == "temp_mail",
                        EmailServiceModel.enabled == True
                    ).order_by(EmailServiceModel.priority.asc()).first()

                    if db_service and db_service.config:
                        config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
                        crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                        logger.info(f"使用数据库 Temp-Mail 自部署服务: {db_service.name}")
                    else:
                        config = _normalize_email_service_config(service_type, email_service_config or {}, actual_proxy_url)
                        missing_keys = [key for key in ("base_url", "admin_password", "domain") if not config.get(key)]
                        if missing_keys:
                            raise ValueError("没有可用的 Temp-Mail 自部署服务，请先在邮箱服务页面添加服务")
                elif service_type == EmailServiceType.CLOUDMAIL:
                    from ...database.models import EmailService as EmailServiceModel

                    db_service = db.query(EmailServiceModel).filter(
                        EmailServiceModel.service_type == "cloudmail",
                        EmailServiceModel.enabled == True
                    ).order_by(EmailServiceModel.priority.asc()).first()

                    if db_service and db_service.config:
                        config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
                        crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                        logger.info(f"使用数据库 CloudMail 服务: {db_service.name}")
                    else:
                        config = _normalize_email_service_config(service_type, email_service_config or {}, actual_proxy_url)
                        missing_keys = [key for key in ("base_url", "admin_password", "domain") if not config.get(key)]
                        if missing_keys:
                            raise ValueError("没有可用的 CloudMail 服务，请先在邮箱服务页面添加服务")
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
                elif service_type == EmailServiceType.LUCKMAIL:
                    from ...database.models import EmailService as EmailServiceModel

                    db_service = db.query(EmailServiceModel).filter(
                        EmailServiceModel.service_type == "luckmail",
                        EmailServiceModel.enabled == True
                    ).order_by(EmailServiceModel.priority.asc()).first()

                    if db_service and db_service.config:
                        config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
                        crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                        logger.info(f"使用数据库 LuckMail 服务: {db_service.name}")
                    else:
                        config = _normalize_email_service_config(service_type, email_service_config or {}, actual_proxy_url)
                        if not config.get("api_key"):
                            raise ValueError("没有可用的 LuckMail 服务，请先在邮箱服务中添加并填写 API Key")
                else:
                    config = email_service_config or {}

            email_service = EmailServiceFactory.create(service_type, config)

            # 创建注册引擎 - 使用 TaskManager 的日志回调
            log_callback = task_manager.create_log_callback(task_uuid, prefix=log_prefix, batch_id=batch_id)

            engine = RegistrationEngine(
                email_service=email_service,
                proxy_url=actual_proxy_url,
                callback_logger=log_callback,
                task_uuid=task_uuid,
                check_cancelled=task_manager.create_check_cancelled_callback(task_uuid),
            )

            # 执行注册
            role_tag = normalize_role_tag(registration_type)
            account_label = role_tag_to_account_label(role_tag)
            result = engine.run()
            _raise_if_cancelled("任务在注册流程中收到取消请求，停止后续处理")
            marker = getattr(email_service, "mark_registration_outcome", None)
            marker_context = {}
            try:
                info = getattr(engine, "email_info", None) or {}
                for key in ("service_id", "order_no", "token", "purchase_id", "source"):
                    value = info.get(key) if isinstance(info, dict) else None
                    if value not in (None, ""):
                        marker_context[key] = value
            except Exception:
                marker_context = {}

            if result.success:
                _raise_if_cancelled("任务在注册成功后收到取消请求，跳过后处理")
                # 更新代理使用时间
                update_proxy_usage(db, proxy_id)

                metadata = result.metadata if isinstance(result.metadata, dict) else {}
                metadata["account_label"] = account_label
                metadata["role_tag"] = role_tag
                metadata["registration_type"] = role_tag
                result.metadata = metadata

                # 保存到数据库
                _raise_if_cancelled("任务在保存账户前收到取消请求，停止后处理")
                engine.save_to_database(result, account_label=account_label, role_tag=role_tag)

                if callable(marker) and result.email:
                    try:
                        marker(
                            email=result.email,
                            success=True,
                            context=marker_context,
                        )
                    except Exception as mark_err:
                        logger.warning(f"记录邮箱成功状态失败: {mark_err}")

                # 自动上传到 CPA（可多服务）
                if auto_upload_cpa:
                    _raise_if_cancelled("任务在 CPA 上传前收到取消请求，停止后处理")
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
                                _raise_if_cancelled("任务在 CPA 上传过程中收到取消请求")
                                try:
                                    _svc = crud.get_cpa_service_by_id(db, _sid)
                                    if not _svc:
                                        continue
                                    log_callback(f"[CPA] 正在把账号打包发往服务站: {_svc.name}")
                                    _ok, _msg = upload_to_cpa(token_data, api_url=_svc.api_url, api_token=_svc.api_token)
                                    if _ok:
                                        saved_account.cpa_uploaded = True
                                        saved_account.cpa_uploaded_at = utcnow_naive()
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
                    _raise_if_cancelled("任务在 Sub2API 上传前收到取消请求，停止后处理")
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
                                _raise_if_cancelled("任务在 Sub2API 上传过程中收到取消请求")
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
                    _raise_if_cancelled("任务在 Team Manager 上传前收到取消请求，停止后处理")
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
                                _raise_if_cancelled("任务在 Team Manager 上传过程中收到取消请求")
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

                if auto_upload_new_api:
                    _raise_if_cancelled("任务在 NewAPI 上传前收到取消请求，停止后处理")
                    try:
                        from ...core.upload.new_api_upload import upload_to_new_api
                        from ...database.models import Account as AccountModel
                        saved_account = db.query(AccountModel).filter_by(email=result.email).first()
                        if saved_account and saved_account.access_token:
                            _new_api_ids = new_api_service_ids or []
                            if not _new_api_ids:
                                _new_api_ids = [s.id for s in crud.get_new_api_services(db, enabled=True)]
                            if not _new_api_ids:
                                log_callback("[NewAPI] 无可用 new-api 服务，跳过上传")
                            for _sid in _new_api_ids:
                                _raise_if_cancelled("任务在 NewAPI 上传过程中收到取消请求")
                                try:
                                    _svc = crud.get_new_api_service_by_id(db, _sid)
                                    if not _svc:
                                        continue
                                    log_callback(f"[NewAPI] 正在把账号发往服务站: {_svc.name}")
                                    _ok, _msg = upload_to_new_api(
                                        [saved_account],
                                        _svc.api_url,
                                        getattr(_svc, 'username', None),
                                        getattr(_svc, 'password', None),
                                    )
                                    log_callback(f"[NewAPI] {'成功' if _ok else '失败'}({_svc.name}): {_msg}")
                                except Exception as _e:
                                    log_callback(f"[NewAPI] 异常({_sid}): {_e}")
                    except Exception as new_api_err:
                        log_callback(f"[NewAPI] 上传异常: {new_api_err}")

                # 更新任务状态
                _raise_if_cancelled("任务在收尾前收到取消请求，停止标记成功")
                crud.update_registration_task(
                    db, task_uuid,
                    status="completed",
                    completed_at=utcnow_naive(),
                    result=result.to_dict()
                )

                # 更新 TaskManager 状态
                task_manager.update_status(task_uuid, "completed", email=result.email)

                logger.info(f"注册任务完成: {task_uuid}, 邮箱: {result.email}")
            else:
                if task_manager.is_cancelled(task_uuid):
                    _mark_cancelled("任务在注册失败分支检测到取消请求")
                    return
                if callable(marker) and result.email:
                    try:
                        marker(
                            email=result.email,
                            success=False,
                            reason=result.error_message or "",
                            context=marker_context,
                        )
                    except Exception as mark_err:
                        logger.warning(f"记录邮箱失败状态失败: {mark_err}")

                # 更新任务状态为失败
                crud.update_registration_task(
                    db, task_uuid,
                    status="failed",
                    completed_at=utcnow_naive(),
                    error_message=result.error_message
                )

                # 更新 TaskManager 状态
                task_manager.update_status(task_uuid, "failed", error=result.error_message)

                logger.warning(f"注册任务失败: {task_uuid}, 原因: {result.error_message}")

        except RegistrationCancelledError as cancelled:
            _mark_cancelled(str(cancelled) or "任务已取消")
        except Exception as e:
            logger.error(f"注册任务异常: {task_uuid}, 错误: {e}")

            try:
                with get_db() as db:
                    crud.update_registration_task(
                        db, task_uuid,
                        status="failed",
                        completed_at=utcnow_naive(),
                        error_message=str(e)
                    )

                # 更新 TaskManager 状态
                task_manager.update_status(task_uuid, "failed", error=str(e))
            except:
                pass


async def run_registration_task(task_uuid: str, email_service_type: str, proxy: Optional[str], email_service_config: Optional[dict], email_service_id: Optional[int] = None, log_prefix: str = "", batch_id: str = "", auto_upload_cpa: bool = False, cpa_service_ids: List[int] = None, auto_upload_sub2api: bool = False, sub2api_service_ids: List[int] = None, auto_upload_tm: bool = False, tm_service_ids: List[int] = None, auto_upload_new_api: bool = False, new_api_service_ids: List[int] = None, registration_type: str = RoleTag.CHILD.value):
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
            auto_upload_new_api,
            new_api_service_ids or [],
            registration_type,
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
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_type: str = RoleTag.CHILD.value,
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
            if task_manager.is_batch_cancelled(batch_id) or batch_tasks[batch_id]["cancelled"]:
                with get_db() as db:
                    crud.update_registration_task(
                        db,
                        uuid,
                        status="cancelled",
                        completed_at=utcnow_naive(),
                        error_message="批量任务已取消",
                    )
                task_manager.cancel_task(uuid)
                task_manager.update_status(uuid, "cancelled", error="批量任务已取消")
                return
            await run_registration_task(
                uuid, email_service_type, proxy, email_service_config, email_service_id,
                log_prefix=prefix, batch_id=batch_id,
                auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids or [],
                auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids or [],
                auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids or [],
                auto_upload_new_api=auto_upload_new_api, new_api_service_ids=new_api_service_ids or [],
                registration_type=registration_type,
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
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_type: str = RoleTag.CHILD.value,
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
            if task_manager.is_batch_cancelled(batch_id) or batch_tasks[batch_id]["cancelled"]:
                with get_db() as db:
                    crud.update_registration_task(
                        db,
                        uuid,
                        status="cancelled",
                        completed_at=utcnow_naive(),
                        error_message="批量任务已取消",
                    )
                task_manager.cancel_task(uuid)
                task_manager.update_status(uuid, "cancelled", error="批量任务已取消")
                return
            await run_registration_task(
                uuid, email_service_type, proxy, email_service_config, email_service_id,
                log_prefix=pfx, batch_id=batch_id,
                auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids or [],
                auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids or [],
                auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids or [],
                auto_upload_new_api=auto_upload_new_api, new_api_service_ids=new_api_service_ids or [],
                registration_type=registration_type,
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
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_type: str = RoleTag.CHILD.value,
):
    """根据 mode 分发到并行或流水线执行"""
    if mode == "parallel":
        await run_batch_parallel(
            batch_id, task_uuids, email_service_type, proxy,
            email_service_config, email_service_id, concurrency,
            auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids,
            auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids,
            auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids,
            auto_upload_new_api=auto_upload_new_api, new_api_service_ids=new_api_service_ids,
            registration_type=registration_type,
        )
    else:
        await run_batch_pipeline(
            batch_id, task_uuids, email_service_type, proxy,
            email_service_config, email_service_id,
            interval_min, interval_max, concurrency,
            auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids,
            auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids,
            auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids,
            auto_upload_new_api=auto_upload_new_api, new_api_service_ids=new_api_service_ids,
            registration_type=registration_type,
        )


async def run_auto_registration_batch(plan, settings: Settings) -> str:
    email_service_type = settings.registration_auto_email_service_type
    try:
        EmailServiceType(email_service_type)
    except ValueError as exc:
        raise ValueError(f"自动注册邮箱服务类型无效: {email_service_type}") from exc

    mode = settings.registration_auto_mode or "pipeline"
    if mode not in ("parallel", "pipeline"):
        raise ValueError(f"自动注册模式无效: {mode}")

    interval_min = max(0, int(settings.registration_auto_interval_min))
    interval_max = max(interval_min, int(settings.registration_auto_interval_max))
    concurrency = max(1, int(settings.registration_auto_concurrency))
    email_service_id = int(settings.registration_auto_email_service_id or 0) or None
    proxy = settings.registration_auto_proxy.strip() or None

    batch_id = str(uuid.uuid4())
    task_uuids = []

    with get_db() as db:
        for _ in range(plan.deficit):
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=proxy,
                email_service_id=email_service_id,
            )
            task_uuids.append(task_uuid)

    update_auto_registration_state(
        status="running",
        message=f"自动补货任务运行中: {batch_id}",
        current_batch_id=batch_id,
    )
    add_auto_registration_log(
        f"[自动注册] 已创建补货批量任务 {batch_id}，计划注册 {len(task_uuids)} 个账号"
    )
    logger.info(
        "自动注册批量任务已创建: batch=%s, count=%s, cpa_service_id=%s",
        batch_id,
        len(task_uuids),
        plan.cpa_service_id,
    )

    await run_batch_registration(
        batch_id=batch_id,
        task_uuids=task_uuids,
        email_service_type=email_service_type,
        proxy=proxy,
        email_service_config=None,
        email_service_id=email_service_id,
        interval_min=interval_min,
        interval_max=interval_max,
        concurrency=concurrency,
        mode=mode,
        auto_upload_cpa=True,
        cpa_service_ids=[plan.cpa_service_id],
        auto_upload_sub2api=False,
        sub2api_service_ids=[],
        auto_upload_tm=False,
        tm_service_ids=[],
        auto_upload_new_api=False,
        new_api_service_ids=[],
    )

    batch = batch_tasks.get(batch_id)
    if batch:
        batch_cancelled = bool(batch.get("cancelled"))
        current_auto_state = get_auto_registration_state()
        refreshed_inventory = await asyncio.to_thread(
            get_auto_registration_inventory, settings
        )
        refreshed_ready_count = (
            refreshed_inventory[0]
            if refreshed_inventory
            else current_auto_state.get("current_ready_count")
        )
        refreshed_target_count = (
            refreshed_inventory[1]
            if refreshed_inventory
            else max(1, int(settings.registration_auto_min_ready_auth_files or 1))
        )
        final_status = "cancelled" if batch_cancelled else "idle"
        final_message = (
            f"自动补货批量任务已取消: {batch_id}"
            if batch_cancelled
            else f"自动补货批量任务已完成: {batch_id}"
        )
        final_log_message = (
            f"[自动注册] 补货批量任务已取消：成功 {batch.get('success', 0)}，失败 {batch.get('failed', 0)}"
            if batch_cancelled
            else f"[自动注册] 补货批量任务已完成：成功 {batch.get('success', 0)}，失败 {batch.get('failed', 0)}"
        )
        update_auto_registration_state(
            status=final_status,
            message=final_message,
            current_batch_id=None,
            current_ready_count=refreshed_ready_count,
            target_ready_count=refreshed_target_count,
            last_checked_at=datetime.now(timezone.utc).isoformat(),
        )
        add_auto_registration_log(final_log_message)

    return batch_id


def _validate_registration_request(email_service_type: str):
    """校验邮箱服务类型。"""
    try:
        EmailServiceType(email_service_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"无效的邮箱服务类型: {email_service_type}"
        ) from exc


def _schedule_async_job(background_tasks: Optional[BackgroundTasks], coroutine_func, *args):
    """统一调度后台异步任务。"""
    if background_tasks is not None:
        background_tasks.add_task(coroutine_func, *args)
        return

    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)
    loop.create_task(coroutine_func(*args))


async def _start_single_registration_internal(
    request: RegistrationTaskCreate,
    background_tasks: Optional[BackgroundTasks] = None,
) -> RegistrationTaskResponse:
    """启动单次注册任务。"""
    _validate_registration_request(request.email_service_type)

    task_uuid = str(uuid.uuid4())
    with get_db() as db:
        task = crud.create_registration_task(
            db,
            task_uuid=task_uuid,
            proxy=request.proxy,
        )

    _schedule_async_job(
        background_tasks,
        run_registration_task,
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
        request.auto_upload_new_api,
        request.new_api_service_ids,
        request.registration_type,
    )
    return task_to_response(task)


async def _start_batch_registration_internal(
    request: BatchRegistrationRequest,
    background_tasks: Optional[BackgroundTasks] = None,
) -> BatchRegistrationResponse:
    """启动普通批量注册任务。"""
    if request.count < 1 or request.count > 1000:
        raise HTTPException(status_code=400, detail="注册数量必须在 1-1000 之间")

    _validate_registration_request(request.email_service_type)

    if request.interval_min < 0 or request.interval_max < request.interval_min:
        raise HTTPException(status_code=400, detail="间隔时间参数无效")

    if not 1 <= request.concurrency <= 50:
        raise HTTPException(status_code=400, detail="并发数必须在 1-50 之间")

    if request.mode not in ("parallel", "pipeline"):
        raise HTTPException(status_code=400, detail="模式必须为 parallel 或 pipeline")

    batch_id = str(uuid.uuid4())
    task_uuids = []

    with get_db() as db:
        for _ in range(request.count):
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=request.proxy,
            )
            task_uuids.append(task_uuid)

    with get_db() as db:
        tasks = [crud.get_registration_task(db, item_uuid) for item_uuid in task_uuids]

    _schedule_async_job(
        background_tasks,
        run_batch_registration,
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
        request.auto_upload_new_api,
        request.new_api_service_ids,
        request.registration_type,
    )

    return BatchRegistrationResponse(
        batch_id=batch_id,
        count=request.count,
        tasks=[task_to_response(task) for task in tasks if task],
    )


async def _start_outlook_batch_registration_internal(
    request: OutlookBatchRegistrationRequest,
    background_tasks: Optional[BackgroundTasks] = None,
) -> OutlookBatchRegistrationResponse:
    """启动 Outlook 批量注册任务。"""
    from ...database.models import EmailService as EmailServiceModel
    from ...database.models import Account

    if not request.service_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个 Outlook 账户")

    if request.interval_min < 0 or request.interval_max < request.interval_min:
        raise HTTPException(status_code=400, detail="间隔时间参数无效")

    if not 1 <= request.concurrency <= 50:
        raise HTTPException(status_code=400, detail="并发数必须在 1-50 之间")

    if request.mode not in ("parallel", "pipeline"):
        raise HTTPException(status_code=400, detail="模式必须为 parallel 或 pipeline")

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
                existing_account = db.query(Account).filter(Account.email == email).first()
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
            service_ids=[],
        )

    batch_id = str(uuid.uuid4())
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
        "finished": False,
    }

    _schedule_async_job(
        background_tasks,
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
        request.auto_upload_new_api,
        request.new_api_service_ids,
        request.registration_type,
    )

    return OutlookBatchRegistrationResponse(
        batch_id=batch_id,
        total=len(request.service_ids),
        skipped=skipped_count,
        to_register=len(actual_service_ids),
        service_ids=actual_service_ids,
    )


async def dispatch_registration_config(
    registration_config: Dict[str, Any],
    background_tasks: Optional[BackgroundTasks] = None,
) -> Dict[str, Any]:
    """按统一注册配置分发执行注册任务。"""
    config = dict(registration_config or {})
    reg_mode = config.get('reg_mode') or 'single'
    email_service_type = config.get('email_service_type')
    if not email_service_type:
        raise HTTPException(status_code=400, detail='缺少邮箱服务类型')

    if email_service_type == 'outlook_batch':
        request = OutlookBatchRegistrationRequest(
            service_ids=config.get('service_ids') or [],
            skip_registered=bool(config.get('skip_registered', True)),
            proxy=config.get('proxy'),
            interval_min=int(config.get('interval_min') or 5),
            interval_max=int(config.get('interval_max') or 30),
            concurrency=int(config.get('concurrency') or 1),
            mode=config.get('mode') or 'pipeline',
            auto_upload_cpa=bool(config.get('auto_upload_cpa', False)),
            cpa_service_ids=config.get('cpa_service_ids') or [],
            auto_upload_sub2api=bool(config.get('auto_upload_sub2api', False)),
            sub2api_service_ids=config.get('sub2api_service_ids') or [],
            auto_upload_tm=bool(config.get('auto_upload_tm', False)),
            tm_service_ids=config.get('tm_service_ids') or [],
            auto_upload_new_api=bool(config.get('auto_upload_new_api', False)),
            new_api_service_ids=config.get('new_api_service_ids') or [],
        )
        response = await _start_outlook_batch_registration_internal(request, background_tasks)
        return {
            'kind': 'batch',
            'batch_id': response.batch_id,
            'payload': response.model_dump(),
        }

    _validate_registration_request(email_service_type)

    if reg_mode == 'batch':
        request = BatchRegistrationRequest(
            count=int(config.get('batch_count') or 1),
            email_service_type=email_service_type,
            proxy=config.get('proxy'),
            email_service_config=config.get('email_service_config'),
            email_service_id=config.get('email_service_id'),
            interval_min=int(config.get('interval_min') or 5),
            interval_max=int(config.get('interval_max') or 30),
            concurrency=int(config.get('concurrency') or 1),
            mode=config.get('mode') or 'pipeline',
            auto_upload_cpa=bool(config.get('auto_upload_cpa', False)),
            cpa_service_ids=config.get('cpa_service_ids') or [],
            auto_upload_sub2api=bool(config.get('auto_upload_sub2api', False)),
            sub2api_service_ids=config.get('sub2api_service_ids') or [],
            auto_upload_tm=bool(config.get('auto_upload_tm', False)),
            tm_service_ids=config.get('tm_service_ids') or [],
            auto_upload_new_api=bool(config.get('auto_upload_new_api', False)),
            new_api_service_ids=config.get('new_api_service_ids') or [],
        )
        response = await _start_batch_registration_internal(request, background_tasks)
        return {
            'kind': 'batch',
            'batch_id': response.batch_id,
            'payload': response.model_dump(),
        }

    request = RegistrationTaskCreate(
        email_service_type=email_service_type,
        proxy=config.get('proxy'),
        email_service_config=config.get('email_service_config'),
        email_service_id=config.get('email_service_id'),
        auto_upload_cpa=bool(config.get('auto_upload_cpa', False)),
        cpa_service_ids=config.get('cpa_service_ids') or [],
        auto_upload_sub2api=bool(config.get('auto_upload_sub2api', False)),
        sub2api_service_ids=config.get('sub2api_service_ids') or [],
        auto_upload_tm=bool(config.get('auto_upload_tm', False)),
        tm_service_ids=config.get('tm_service_ids') or [],
        auto_upload_new_api=bool(config.get('auto_upload_new_api', False)),
        new_api_service_ids=config.get('new_api_service_ids') or [],
    )
    response = await _start_single_registration_internal(request, background_tasks)
    return {
        'kind': 'single',
        'task_uuid': response.task_uuid,
        'payload': response.model_dump(),
    }


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
    return await _start_single_registration_internal(request, background_tasks)


@router.post("/batch", response_model=BatchRegistrationResponse)
async def start_batch_registration(
    request: BatchRegistrationRequest,
    background_tasks: BackgroundTasks
):
    """
    启动批量注册任务

    - count: 注册数量 (1-1000)
    - email_service_type: 邮箱服务类型
    - proxy: 代理地址
    - interval_min: 最小间隔秒数
    - interval_max: 最大间隔秒数
    """
    return await _start_batch_registration_internal(request, background_tasks)


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


@router.get("/auto-monitor")
async def get_auto_registration_monitor():
    auto_state = get_auto_registration_state()
    current_batch_id = auto_state.get("current_batch_id")
    batch = batch_tasks.get(current_batch_id) if current_batch_id else None
    logs = get_auto_registration_logs().copy()

    return {
        **auto_state,
        "batch": batch,
        "logs": logs,
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
    _cancel_batch_tasks(batch_id)
    return {"success": True, "message": "批量任务取消请求已提交，正在让它们有序收工"}


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
        result = task.result if isinstance(task.result, dict) else {}
        email = result.get("email")
        service_type = task.email_service.service_type if task.email_service else None
        return {
            "task_uuid": task_uuid,
            "status": task.status,
            "email": email,
            "email_service": service_type,
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

        task_manager.cancel_task(task_uuid)
        task = crud.update_registration_task(
            db,
            task_uuid,
            status="cancelled",
            completed_at=utcnow_naive(),
            error_message="任务取消请求已提交",
        )
        task_manager.update_status(task_uuid, "cancelled", error="任务取消请求已提交")
        task_manager.add_log(task_uuid, "[取消] 任务取消请求已提交，等待后台线程有序退出")

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

        # 今日统计
        today = utcnow_naive().date()
        today_status_stats = db.query(
            RegistrationTask.status,
            func.count(RegistrationTask.id)
        ).filter(
            func.date(RegistrationTask.created_at) == today
        ).group_by(RegistrationTask.status).all()

        today_count = db.query(func.count(RegistrationTask.id)).filter(
            func.date(RegistrationTask.created_at) == today
        ).scalar()

        today_by_status = {status: count for status, count in today_status_stats}
        today_success = int(today_by_status.get("completed", 0))
        today_failed = int(today_by_status.get("failed", 0))
        today_total = int(today_count or 0)
        today_success_rate = round((today_success / today_total) * 100, 1) if today_total > 0 else 0.0

        return {
            "by_status": {status: count for status, count in status_stats},
            "today_count": today_total,
            "today_total": today_total,
            "today_success": today_success,
            "today_failed": today_failed,
            "today_success_rate": today_success_rate,
            "today_by_status": today_by_status,
        }


@router.get("/available-services")
async def get_available_email_services():
    """
    获取可用于注册的邮箱服务列表

    返回所有已启用的邮箱服务，包括：
    - tempmail: 临时邮箱（无需配置）
    - yyds_mail: YYDS Mail 临时邮箱（需 API Key）
    - outlook: 已导入的 Outlook 账户
    - moe_mail: 已配置的自定义域名服务
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...config.settings import get_settings

    settings = get_settings()
    result = {
        "tempmail": {
            "available": bool(settings.tempmail_enabled),
            "count": 1 if settings.tempmail_enabled else 0,
            "services": ([{
                "id": None,
                "name": "Tempmail.lol",
                "type": "tempmail",
                "description": "临时邮箱，自动创建"
            }] if settings.tempmail_enabled else [])
        },
        "yyds_mail": {
            "available": False,
            "count": 0,
            "services": []
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
        "cloudmail": {
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
        },
        "luckmail": {
            "available": False,
            "count": 0,
            "services": []
        }
    }

    yyds_api_key = settings.yyds_mail_api_key.get_secret_value() if settings.yyds_mail_api_key else ""
    if settings.yyds_mail_enabled and yyds_api_key:
        result["yyds_mail"]["available"] = True
        result["yyds_mail"]["count"] = 1
        result["yyds_mail"]["services"].append({
            "id": None,
            "name": "YYDS Mail",
            "type": "yyds_mail",
            "default_domain": settings.yyds_mail_default_domain or None,
            "description": "YYDS Mail API 临时邮箱",
        })

    with get_db() as db:
        yyds_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "yyds_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in yyds_mail_services:
            config = service.config or {}
            result["yyds_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "yyds_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        if yyds_mail_services:
            result["yyds_mail"]["count"] = len(result["yyds_mail"]["services"])
            result["yyds_mail"]["available"] = True
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

        cloudmail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "cloudmail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in cloudmail_services:
            config = service.config or {}
            result["cloudmail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "cloudmail",
                "domain": config.get("domain"),
                "priority": service.priority
            })

        result["cloudmail"]["count"] = len(cloudmail_services)
        result["cloudmail"]["available"] = len(cloudmail_services) > 0

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

        luckmail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "luckmail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in luckmail_services:
            config = service.config or {}
            result["luckmail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "luckmail",
                "project_code": config.get("project_code"),
                "email_type": config.get("email_type"),
                "preferred_domain": config.get("preferred_domain"),
                "priority": service.priority
            })

        result["luckmail"]["count"] = len(luckmail_services)
        result["luckmail"]["available"] = len(luckmail_services) > 0

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
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
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
        auto_upload_new_api=auto_upload_new_api,
        new_api_service_ids=new_api_service_ids,
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
    return await _start_outlook_batch_registration_internal(request, background_tasks)


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
    _cancel_batch_tasks(batch_id)

    return {"success": True, "message": "批量任务取消请求已提交，正在让它们有序收工"}


@router.get("/schedules", response_model=ScheduledRegistrationJobListResponse)
async def list_scheduled_registration_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    enabled: Optional[bool] = Query(None),
):
    """获取计划注册任务列表。"""
    offset = (page - 1) * page_size
    with get_db() as db:
        jobs = crud.get_scheduled_registration_jobs(db, enabled=enabled, skip=offset, limit=page_size)
        total_query = db.query(ScheduledRegistrationJob)
        if enabled is not None:
            total_query = total_query.filter(ScheduledRegistrationJob.enabled == enabled)
        total = total_query.count()
        return ScheduledRegistrationJobListResponse(
            total=total,
            jobs=[scheduled_job_to_response(job) for job in jobs],
        )


@router.get("/schedules/{job_uuid}", response_model=ScheduledRegistrationJobResponse)
async def get_scheduled_registration_job(job_uuid: str):
    """获取计划注册任务详情。"""
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        return scheduled_job_to_response(job)


@router.post("/schedules", response_model=ScheduledRegistrationJobResponse)
async def create_scheduled_registration_job(request: ScheduledRegistrationRequest):
    """创建计划注册任务。"""
    now = utcnow_naive()
    normalized_schedule_config = normalize_schedule_config(request.schedule_type, request.schedule_config, now)
    next_run_at = compute_next_run_at(request.schedule_type, normalized_schedule_config, now)
    registration_config = dict(request.registration_config or {})

    with get_db() as db:
        job = crud.create_scheduled_registration_job(
            db,
            job_uuid=str(uuid.uuid4()),
            name=request.name.strip(),
            enabled=request.enabled,
            status='idle' if request.enabled else 'paused',
            schedule_type=request.schedule_type,
            schedule_config=normalized_schedule_config,
            registration_config=registration_config,
            timezone=request.timezone,
            next_run_at=next_run_at if request.enabled else None,
        )
        return scheduled_job_to_response(job)


@router.put("/schedules/{job_uuid}", response_model=ScheduledRegistrationJobResponse)
async def update_scheduled_registration_job(job_uuid: str, request: ScheduledRegistrationRequest):
    """更新计划注册任务。"""
    now = utcnow_naive()
    normalized_schedule_config = normalize_schedule_config(request.schedule_type, request.schedule_config, now)
    next_run_at = compute_next_run_at(request.schedule_type, normalized_schedule_config, now)

    with get_db() as db:
        existing = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not existing:
            raise HTTPException(status_code=404, detail="计划任务不存在")

        job = crud.update_scheduled_registration_job(
            db,
            job_uuid,
            name=request.name.strip(),
            enabled=request.enabled,
            status='idle' if request.enabled else 'paused',
            schedule_type=request.schedule_type,
            schedule_config=normalized_schedule_config,
            registration_config=dict(request.registration_config or {}),
            timezone=request.timezone,
            next_run_at=next_run_at if request.enabled else None,
            last_error=None,
        )
        return scheduled_job_to_response(job)


@router.post("/schedules/{job_uuid}/enable", response_model=ScheduledRegistrationJobResponse)
async def enable_scheduled_registration_job(job_uuid: str):
    """启用计划注册任务。"""
    now = utcnow_naive()
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        next_run_at = compute_next_run_at(job.schedule_type, job.schedule_config or {}, now)
        updated = crud.update_scheduled_registration_job(
            db,
            job_uuid,
            enabled=True,
            status='idle',
            next_run_at=next_run_at,
        )
        return scheduled_job_to_response(updated)


@router.post("/schedules/{job_uuid}/pause", response_model=ScheduledRegistrationJobResponse)
async def pause_scheduled_registration_job(job_uuid: str):
    """暂停计划注册任务。"""
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        updated = crud.update_scheduled_registration_job(
            db,
            job_uuid,
            enabled=False,
            status='paused',
            next_run_at=None,
            is_running=False,
        )
        return scheduled_job_to_response(updated)


@router.post("/schedules/{job_uuid}/run")
async def run_scheduled_registration_job_now(job_uuid: str, background_tasks: BackgroundTasks):
    """立即执行一次计划注册任务。"""
    now = utcnow_naive()
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        if job.is_running:
            raise HTTPException(status_code=400, detail="计划任务正在执行中")
        if job.enabled:
            next_run_at = compute_next_run_at(job.schedule_type, job.schedule_config or {}, now)
        else:
            next_run_at = None
        claimed = crud.claim_scheduled_registration_job(db, job_uuid, next_run_at, now)
        if not claimed:
            raise HTTPException(status_code=409, detail="计划任务状态已变化，请刷新后重试")

    try:
        result = await dispatch_registration_config(claimed.registration_config or {}, background_tasks)
        with get_db() as db:
            crud.mark_scheduled_registration_job_success(
                db,
                job_uuid,
                utcnow_naive(),
                task_uuid=result.get('task_uuid'),
                batch_id=result.get('batch_id'),
            )
        return {
            'success': True,
            'message': '计划任务已触发执行',
            'task_uuid': result.get('task_uuid'),
            'batch_id': result.get('batch_id'),
        }
    except Exception as exc:
        with get_db() as db:
            crud.mark_scheduled_registration_job_failure(
                db,
                job_uuid,
                str(exc),
                utcnow_naive(),
            )
        raise


@router.delete("/schedules/{job_uuid}")
async def delete_scheduled_registration_job(job_uuid: str):
    """删除计划注册任务。"""
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        if job.is_running:
            raise HTTPException(status_code=400, detail="无法删除执行中的计划任务")
        crud.delete_scheduled_registration_job(db, job_uuid)
        return {'success': True, 'message': '计划任务已删除'}

