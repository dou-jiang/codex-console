"""
数据库 CRUD 操作
"""

from typing import List, Optional, Dict, Any, Union
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, asc, func

from .models import (
    Account,
    EmailService,
    RegistrationTask,
    Setting,
    Proxy,
    CpaService,
    Sub2ApiService,
    ScheduledPlan,
    ScheduledRun,
)


# ============================================================================
# 账户 CRUD
# ============================================================================

def create_account(
    db: Session,
    email: str,
    email_service: str,
    password: Optional[str] = None,
    client_id: Optional[str] = None,
    session_token: Optional[str] = None,
    email_service_id: Optional[str] = None,
    account_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    access_token: Optional[str] = None,
    refresh_token: Optional[str] = None,
    id_token: Optional[str] = None,
    proxy_used: Optional[str] = None,
    expires_at: Optional['datetime'] = None,
    extra_data: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    source: Optional[str] = None
) -> Account:
    """创建新账户"""
    db_account = Account(
        email=email,
        password=password,
        client_id=client_id,
        session_token=session_token,
        email_service=email_service,
        email_service_id=email_service_id,
        account_id=account_id,
        workspace_id=workspace_id,
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        proxy_used=proxy_used,
        expires_at=expires_at,
        extra_data=extra_data or {},
        status=status or 'active',
        source=source or 'register',
        registered_at=datetime.utcnow()
    )
    db.add(db_account)
    db.commit()
    db.refresh(db_account)
    return db_account


def get_account_by_id(db: Session, account_id: int) -> Optional[Account]:
    """根据 ID 获取账户"""
    return db.query(Account).filter(Account.id == account_id).first()


def get_account_by_email(db: Session, email: str) -> Optional[Account]:
    """根据邮箱获取账户"""
    return db.query(Account).filter(Account.email == email).first()


def get_accounts(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    email_service: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None
) -> List[Account]:
    """获取账户列表（支持分页、筛选）"""
    query = db.query(Account)

    if email_service:
        query = query.filter(Account.email_service == email_service)

    if status:
        query = query.filter(Account.status == status)

    if search:
        search_filter = or_(
            Account.email.ilike(f"%{search}%"),
            Account.account_id.ilike(f"%{search}%"),
            Account.workspace_id.ilike(f"%{search}%")
        )
        query = query.filter(search_filter)

    query = query.order_by(desc(Account.created_at)).offset(skip).limit(limit)
    return query.all()


def update_account(
    db: Session,
    account_id: int,
    **kwargs
) -> Optional[Account]:
    """更新账户信息"""
    db_account = get_account_by_id(db, account_id)
    if not db_account:
        return None

    for key, value in kwargs.items():
        if hasattr(db_account, key) and value is not None:
            setattr(db_account, key, value)

    db.commit()
    db.refresh(db_account)
    return db_account


def mark_account_expired_by_email_and_cpa(
    db: Session,
    *,
    email: str,
    cpa_service_id: int,
    reason: str,
) -> int:
    """按邮箱 + 主 CPA 服务标记账号为失效。"""
    if not email:
        return 0

    now = datetime.utcnow()
    updated = (
        db.query(Account)
        .filter(Account.email == email)
        .filter(Account.primary_cpa_service_id == cpa_service_id)
        .update(
            {
                Account.status: "expired",
                Account.invalidated_at: now,
                Account.invalid_reason: reason,
                Account.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return int(updated)


def get_due_refresh_accounts(
    db: Session,
    *,
    cpa_service_id: int,
    refresh_after_days: int,
    limit: int,
) -> List[Account]:
    """获取需要执行刷新任务的账号列表。"""
    safe_days = max(0, int(refresh_after_days))
    safe_limit = max(0, int(limit))
    cutoff = datetime.utcnow() - timedelta(days=safe_days)

    query = (
        db.query(Account)
        .filter(Account.status == "active")
        .filter(Account.primary_cpa_service_id == cpa_service_id)
        .filter(
            or_(
                and_(Account.last_refresh.is_(None), Account.registered_at <= cutoff),
                and_(Account.last_refresh.is_not(None), Account.last_refresh <= cutoff),
            )
        )
        .order_by(
            asc(func.coalesce(Account.last_refresh, Account.registered_at)),
            asc(Account.id),
        )
    )

    if safe_limit > 0:
        query = query.limit(safe_limit)

    return query.all()


def mark_account_expired(db: Session, account_id: int, reason: str) -> Optional[Account]:
    """按账号 ID 标记失效。"""
    account = get_account_by_id(db, account_id)
    if not account:
        return None

    now = datetime.utcnow()
    account.status = "expired"
    account.cpa_uploaded = False
    account.cpa_uploaded_at = None
    account.invalidated_at = now
    account.invalid_reason = reason
    account.updated_at = now
    db.commit()
    db.refresh(account)
    return account


def mark_account_active_for_cpa(
    db: Session,
    account_id: int,
    cpa_service_id: int,
) -> Optional[Account]:
    """标记账号为 active 并绑定主 CPA 服务。"""
    account = get_account_by_id(db, account_id)
    if not account:
        return None

    now = datetime.utcnow()
    account.status = "active"
    account.primary_cpa_service_id = cpa_service_id
    account.cpa_uploaded = True
    account.cpa_uploaded_at = now
    account.invalidated_at = None
    account.invalid_reason = None
    account.updated_at = now
    db.commit()
    db.refresh(account)
    return account


def delete_account(db: Session, account_id: int) -> bool:
    """删除账户"""
    db_account = get_account_by_id(db, account_id)
    if not db_account:
        return False

    db.delete(db_account)
    db.commit()
    return True


def delete_accounts_batch(db: Session, account_ids: List[int]) -> int:
    """批量删除账户"""
    result = db.query(Account).filter(Account.id.in_(account_ids)).delete(synchronize_session=False)
    db.commit()
    return result


def get_accounts_count(
    db: Session,
    email_service: Optional[str] = None,
    status: Optional[str] = None
) -> int:
    """获取账户数量"""
    query = db.query(func.count(Account.id))

    if email_service:
        query = query.filter(Account.email_service == email_service)

    if status:
        query = query.filter(Account.status == status)

    return query.scalar()


# ============================================================================
# 邮箱服务 CRUD
# ============================================================================

def create_email_service(
    db: Session,
    service_type: str,
    name: str,
    config: Dict[str, Any],
    enabled: bool = True,
    priority: int = 0
) -> EmailService:
    """创建邮箱服务配置"""
    db_service = EmailService(
        service_type=service_type,
        name=name,
        config=config,
        enabled=enabled,
        priority=priority
    )
    db.add(db_service)
    db.commit()
    db.refresh(db_service)
    return db_service


def get_email_service_by_id(db: Session, service_id: int) -> Optional[EmailService]:
    """根据 ID 获取邮箱服务"""
    return db.query(EmailService).filter(EmailService.id == service_id).first()


def get_email_services(
    db: Session,
    service_type: Optional[str] = None,
    enabled: Optional[bool] = None,
    skip: int = 0,
    limit: int = 100
) -> List[EmailService]:
    """获取邮箱服务列表"""
    query = db.query(EmailService)

    if service_type:
        query = query.filter(EmailService.service_type == service_type)

    if enabled is not None:
        query = query.filter(EmailService.enabled == enabled)

    query = query.order_by(
        asc(EmailService.priority),
        desc(EmailService.last_used)
    ).offset(skip).limit(limit)

    return query.all()


def update_email_service(
    db: Session,
    service_id: int,
    **kwargs
) -> Optional[EmailService]:
    """更新邮箱服务配置"""
    db_service = get_email_service_by_id(db, service_id)
    if not db_service:
        return None

    for key, value in kwargs.items():
        if hasattr(db_service, key) and value is not None:
            setattr(db_service, key, value)

    db.commit()
    db.refresh(db_service)
    return db_service


def delete_email_service(db: Session, service_id: int) -> bool:
    """删除邮箱服务配置"""
    db_service = get_email_service_by_id(db, service_id)
    if not db_service:
        return False

    db.delete(db_service)
    db.commit()
    return True


# ============================================================================
# 注册任务 CRUD
# ============================================================================

def create_registration_task(
    db: Session,
    task_uuid: str,
    email_service_id: Optional[int] = None,
    proxy: Optional[str] = None,
    email_address: Optional[str] = None,
) -> RegistrationTask:
    """创建注册任务"""
    db_task = RegistrationTask(
        task_uuid=task_uuid,
        email_service_id=email_service_id,
        proxy=proxy,
        email_address=email_address,
        status='pending'
    )
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


def get_registration_task_by_uuid(db: Session, task_uuid: str) -> Optional[RegistrationTask]:
    """根据 UUID 获取注册任务"""
    return db.query(RegistrationTask).filter(RegistrationTask.task_uuid == task_uuid).first()


def get_registration_tasks(
    db: Session,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100
) -> List[RegistrationTask]:
    """获取注册任务列表"""
    query = db.query(RegistrationTask)

    if status:
        query = query.filter(RegistrationTask.status == status)

    query = query.order_by(desc(RegistrationTask.created_at)).offset(skip).limit(limit)
    return query.all()


def update_registration_task(
    db: Session,
    task_uuid: str,
    **kwargs
) -> Optional[RegistrationTask]:
    """更新注册任务状态"""
    db_task = get_registration_task_by_uuid(db, task_uuid)
    if not db_task:
        return None

    for key, value in kwargs.items():
        if hasattr(db_task, key):
            setattr(db_task, key, value)

    db.commit()
    db.refresh(db_task)
    return db_task


def append_task_log(db: Session, task_uuid: str, log_message: str) -> bool:
    """追加任务日志"""
    db_task = get_registration_task_by_uuid(db, task_uuid)
    if not db_task:
        return False

    if db_task.logs:
        db_task.logs += f"\n{log_message}"
    else:
        db_task.logs = log_message

    db.commit()
    return True


def delete_registration_task(db: Session, task_uuid: str) -> bool:
    """删除注册任务"""
    db_task = get_registration_task_by_uuid(db, task_uuid)
    if not db_task:
        return False

    db.delete(db_task)
    db.commit()
    return True


# 为 API 路由添加别名
get_account = get_account_by_id
get_registration_task = get_registration_task_by_uuid


# ============================================================================
# 设置 CRUD
# ============================================================================

def get_setting(db: Session, key: str) -> Optional[Setting]:
    """获取设置"""
    return db.query(Setting).filter(Setting.key == key).first()


def get_settings_by_category(db: Session, category: str) -> List[Setting]:
    """根据分类获取设置"""
    return db.query(Setting).filter(Setting.category == category).all()


def set_setting(
    db: Session,
    key: str,
    value: str,
    description: Optional[str] = None,
    category: str = 'general'
) -> Setting:
    """设置或更新配置项"""
    db_setting = get_setting(db, key)
    if db_setting:
        db_setting.value = value
        db_setting.description = description or db_setting.description
        db_setting.category = category
        db_setting.updated_at = datetime.utcnow()
    else:
        db_setting = Setting(
            key=key,
            value=value,
            description=description,
            category=category
        )
        db.add(db_setting)

    db.commit()
    db.refresh(db_setting)
    return db_setting


def delete_setting(db: Session, key: str) -> bool:
    """删除设置"""
    db_setting = get_setting(db, key)
    if not db_setting:
        return False

    db.delete(db_setting)
    db.commit()
    return True


# ============================================================================
# 代理 CRUD
# ============================================================================

def create_proxy(
    db: Session,
    name: str,
    type: str,
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    enabled: bool = True,
    priority: int = 0,
    country: Optional[str] = None,
    city: Optional[str] = None,
) -> Proxy:
    """创建代理配置"""
    db_proxy = Proxy(
        name=name,
        type=type,
        host=host,
        port=port,
        country=country,
        city=city,
        username=username,
        password=password,
        enabled=enabled,
        priority=priority
    )
    db.add(db_proxy)
    db.commit()
    db.refresh(db_proxy)
    return db_proxy


def get_proxy_by_id(db: Session, proxy_id: int) -> Optional[Proxy]:
    """根据 ID 获取代理"""
    return db.query(Proxy).filter(Proxy.id == proxy_id).first()


def find_proxy_by_host_port(db: Session, host: str, port: int) -> Optional[Proxy]:
    """根据 host + port 查找代理（用于去重）"""
    return db.query(Proxy).filter(and_(Proxy.host == host, Proxy.port == port)).first()


def get_proxies(
    db: Session,
    enabled: Optional[bool] = None,
    skip: int = 0,
    limit: int = 100,
    *,
    keyword: Optional[str] = None,
    type: Optional[str] = None,
    is_default: Optional[bool] = None,
    location: Optional[str] = None,
) -> List[Proxy]:
    """获取代理列表（支持关键字、类型、状态、默认代理、位置筛选）"""
    query = db.query(Proxy)

    if keyword:
        keyword_like = f"%{keyword}%"
        query = query.filter(or_(
            Proxy.name.ilike(keyword_like),
            Proxy.host.ilike(keyword_like),
            Proxy.country.ilike(keyword_like),
            Proxy.city.ilike(keyword_like),
        ))

    if type:
        query = query.filter(Proxy.type == type)

    if enabled is not None:
        query = query.filter(Proxy.enabled == enabled)

    if is_default is not None:
        query = query.filter(Proxy.is_default == is_default)

    if location:
        location_like = f"%{location}%"
        query = query.filter(or_(
            Proxy.country.ilike(location_like),
            Proxy.city.ilike(location_like),
        ))

    query = query.order_by(desc(Proxy.created_at)).offset(skip).limit(limit)
    return query.all()


def get_enabled_proxies(db: Session) -> List[Proxy]:
    """获取所有启用的代理"""
    return db.query(Proxy).filter(Proxy.enabled == True).all()


def update_proxy(
    db: Session,
    proxy_id: int,
    **kwargs
) -> Optional[Proxy]:
    """更新代理配置"""
    db_proxy = get_proxy_by_id(db, proxy_id)
    if not db_proxy:
        return None

    for key, value in kwargs.items():
        if hasattr(db_proxy, key):
            setattr(db_proxy, key, value)

    db.commit()
    db.refresh(db_proxy)
    return db_proxy


def delete_proxy(db: Session, proxy_id: int) -> bool:
    """删除代理配置"""
    db_proxy = get_proxy_by_id(db, proxy_id)
    if not db_proxy:
        return False

    db.delete(db_proxy)
    db.commit()
    return True


def delete_proxies(db: Session, proxy_ids: List[int]) -> int:
    """批量删除代理，忽略不存在的 ID，返回实际删除数量"""
    if not proxy_ids:
        return 0

    deleted = db.query(Proxy).filter(Proxy.id.in_(proxy_ids)).delete(synchronize_session=False)
    db.commit()
    return deleted


def update_proxy_last_used(db: Session, proxy_id: int) -> bool:
    """更新代理最后使用时间"""
    db_proxy = get_proxy_by_id(db, proxy_id)
    if not db_proxy:
        return False

    db_proxy.last_used = datetime.utcnow()
    db.commit()
    return True


def get_random_proxy(db: Session) -> Optional[Proxy]:
    """随机获取一个启用的代理，优先返回 is_default=True 的代理"""
    import random
    # 优先返回默认代理
    default_proxy = db.query(Proxy).filter(Proxy.enabled == True, Proxy.is_default == True).first()
    if default_proxy:
        return default_proxy
    proxies = get_enabled_proxies(db)
    if not proxies:
        return None
    return random.choice(proxies)


def set_proxy_default(db: Session, proxy_id: int) -> Optional[Proxy]:
    """将指定代理设为默认，同时清除其他代理的默认标记"""
    # 清除所有默认标记
    db.query(Proxy).filter(Proxy.is_default == True).update({"is_default": False})
    # 设置新的默认代理
    proxy = db.query(Proxy).filter(Proxy.id == proxy_id).first()
    if proxy:
        proxy.is_default = True
        db.commit()
        db.refresh(proxy)
    return proxy


def get_proxies_count(db: Session, enabled: Optional[bool] = None) -> int:
    """获取代理数量"""
    query = db.query(func.count(Proxy.id))
    if enabled is not None:
        query = query.filter(Proxy.enabled == enabled)
    return query.scalar()


# ============================================================================
# CPA 服务 CRUD
# ============================================================================

def create_cpa_service(
    db: Session,
    name: str,
    api_url: str,
    api_token: str,
    enabled: bool = True,
    priority: int = 0
) -> CpaService:
    """创建 CPA 服务配置"""
    db_service = CpaService(
        name=name,
        api_url=api_url,
        api_token=api_token,
        enabled=enabled,
        priority=priority
    )
    db.add(db_service)
    db.commit()
    db.refresh(db_service)
    return db_service


def get_cpa_service_by_id(db: Session, service_id: int) -> Optional[CpaService]:
    """根据 ID 获取 CPA 服务"""
    return db.query(CpaService).filter(CpaService.id == service_id).first()


def get_cpa_services(
    db: Session,
    enabled: Optional[bool] = None
) -> List[CpaService]:
    """获取 CPA 服务列表"""
    query = db.query(CpaService)
    if enabled is not None:
        query = query.filter(CpaService.enabled == enabled)
    return query.order_by(asc(CpaService.priority), asc(CpaService.id)).all()


def update_cpa_service(
    db: Session,
    service_id: int,
    **kwargs
) -> Optional[CpaService]:
    """更新 CPA 服务配置"""
    db_service = get_cpa_service_by_id(db, service_id)
    if not db_service:
        return None
    for key, value in kwargs.items():
        if hasattr(db_service, key):
            setattr(db_service, key, value)
    db.commit()
    db.refresh(db_service)
    return db_service


def delete_cpa_service(db: Session, service_id: int) -> bool:
    """删除 CPA 服务配置"""
    db_service = get_cpa_service_by_id(db, service_id)
    if not db_service:
        return False

    referenced_plan = db.query(ScheduledPlan).filter(ScheduledPlan.cpa_service_id == service_id).first()
    if referenced_plan is not None:
        return False

    db.delete(db_service)
    db.commit()
    return True


# ============================================================================
# 定时任务 CRUD
# ============================================================================

def create_scheduled_plan(
    db: Session,
    name: str,
    task_type: str,
    cpa_service_id: int,
    trigger_type: str,
    config: Dict[str, Any],
    enabled: bool = True,
    cron_expression: Optional[str] = None,
    interval_value: Optional[int] = None,
    interval_unit: Optional[str] = None,
    next_run_at: Optional[datetime] = None,
) -> ScheduledPlan:
    """创建定时计划"""
    cpa_service = db.query(CpaService).filter(CpaService.id == cpa_service_id).first()
    if not cpa_service:
        raise ValueError(f"cpa service {cpa_service_id} does not exist")

    plan = ScheduledPlan(
        name=name,
        task_type=task_type,
        enabled=enabled,
        cpa_service_id=cpa_service_id,
        trigger_type=trigger_type,
        cron_expression=cron_expression,
        interval_value=interval_value,
        interval_unit=interval_unit,
        config=config,
        next_run_at=next_run_at,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def get_scheduled_plan_by_id(db: Session, plan_id: int) -> Optional[ScheduledPlan]:
    """按 ID 获取定时计划。"""
    return db.query(ScheduledPlan).filter(ScheduledPlan.id == plan_id).first()


def update_scheduled_plan(
    db: Session,
    plan_id: int,
    **kwargs,
) -> Optional[ScheduledPlan]:
    """更新定时计划。"""
    plan = get_scheduled_plan_by_id(db, plan_id)
    if not plan:
        return None

    for key, value in kwargs.items():
        if hasattr(plan, key):
            setattr(plan, key, value)

    db.commit()
    db.refresh(plan)
    return plan


def disable_scheduled_plan(
    db: Session,
    *,
    plan_id: int,
    reason: str,
) -> Optional[ScheduledPlan]:
    """禁用定时计划并记录自动禁用原因。"""
    return update_scheduled_plan(
        db,
        plan_id,
        enabled=False,
        auto_disabled_reason=reason,
    )


def get_scheduled_plans(
    db: Session,
    enabled: Optional[bool] = None,
    cpa_service_id: Optional[int] = None,
) -> List[ScheduledPlan]:
    """获取定时计划列表"""
    query = db.query(ScheduledPlan)

    if enabled is not None:
        query = query.filter(ScheduledPlan.enabled == enabled)

    if cpa_service_id is not None:
        query = query.filter(ScheduledPlan.cpa_service_id == cpa_service_id)

    return query.order_by(asc(ScheduledPlan.id)).all()


def create_scheduled_run(
    db: Session,
    plan_id: int,
    trigger_source: str,
    status: str = 'running',
) -> ScheduledRun:
    """创建定时执行记录"""
    plan = db.query(ScheduledPlan).filter(ScheduledPlan.id == plan_id).first()
    if not plan:
        raise ValueError(f"scheduled plan {plan_id} does not exist")

    run = ScheduledRun(
        plan_id=plan_id,
        trigger_source=trigger_source,
        status=status,
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def append_scheduled_run_log(db: Session, run_id: int, log_message: str) -> bool:
    """追加运行日志"""
    run = db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
    if not run:
        return False

    if run.logs:
        run.logs += f"\n{log_message}"
    else:
        run.logs = log_message

    db.commit()
    return True


def finish_scheduled_run(
    db: Session,
    run_id: int,
    status: str,
    summary: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    finished_at: Optional[datetime] = None,
) -> Optional[ScheduledRun]:
    """结束定时执行记录"""
    run = db.query(ScheduledRun).filter(ScheduledRun.id == run_id).first()
    if not run:
        return None

    run.status = status
    run.summary = summary
    run.error_message = error_message
    run.finished_at = finished_at or datetime.utcnow()

    db.commit()
    db.refresh(run)
    return run


# ============================================================================
# Sub2API 服务 CRUD
# ============================================================================

def create_sub2api_service(
    db: Session,
    name: str,
    api_url: str,
    api_key: str,
    enabled: bool = True,
    priority: int = 0
) -> Sub2ApiService:
    """创建 Sub2API 服务配置"""
    svc = Sub2ApiService(
        name=name,
        api_url=api_url,
        api_key=api_key,
        enabled=enabled,
        priority=priority,
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


def get_sub2api_service_by_id(db: Session, service_id: int) -> Optional[Sub2ApiService]:
    """按 ID 获取 Sub2API 服务"""
    return db.query(Sub2ApiService).filter(Sub2ApiService.id == service_id).first()


def get_sub2api_services(
    db: Session,
    enabled: Optional[bool] = None
) -> List[Sub2ApiService]:
    """获取 Sub2API 服务列表"""
    query = db.query(Sub2ApiService)
    if enabled is not None:
        query = query.filter(Sub2ApiService.enabled == enabled)
    return query.order_by(asc(Sub2ApiService.priority), asc(Sub2ApiService.id)).all()


def update_sub2api_service(db: Session, service_id: int, **kwargs) -> Optional[Sub2ApiService]:
    """更新 Sub2API 服务配置"""
    svc = get_sub2api_service_by_id(db, service_id)
    if not svc:
        return None
    for key, value in kwargs.items():
        setattr(svc, key, value)
    db.commit()
    db.refresh(svc)
    return svc


def delete_sub2api_service(db: Session, service_id: int) -> bool:
    """删除 Sub2API 服务配置"""
    svc = get_sub2api_service_by_id(db, service_id)
    if not svc:
        return False
    db.delete(svc)
    db.commit()
    return True


# ============================================================================
# Team Manager 服务 CRUD
# ============================================================================

def create_tm_service(
    db: Session,
    name: str,
    api_url: str,
    api_key: str,
    enabled: bool = True,
    priority: int = 0,
):
    """创建 Team Manager 服务配置"""
    from .models import TeamManagerService
    svc = TeamManagerService(
        name=name,
        api_url=api_url,
        api_key=api_key,
        enabled=enabled,
        priority=priority,
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


def get_tm_service_by_id(db: Session, service_id: int):
    """按 ID 获取 Team Manager 服务"""
    from .models import TeamManagerService
    return db.query(TeamManagerService).filter(TeamManagerService.id == service_id).first()


def get_tm_services(db: Session, enabled=None):
    """获取 Team Manager 服务列表"""
    from .models import TeamManagerService
    q = db.query(TeamManagerService)
    if enabled is not None:
        q = q.filter(TeamManagerService.enabled == enabled)
    return q.order_by(TeamManagerService.priority.asc(), TeamManagerService.id.asc()).all()


def update_tm_service(db: Session, service_id: int, **kwargs):
    """更新 Team Manager 服务配置"""
    svc = get_tm_service_by_id(db, service_id)
    if not svc:
        return None
    for k, v in kwargs.items():
        setattr(svc, k, v)
    db.commit()
    db.refresh(svc)
    return svc


def delete_tm_service(db: Session, service_id: int) -> bool:
    """删除 Team Manager 服务配置"""
    svc = get_tm_service_by_id(db, service_id)
    if not svc:
        return False
    db.delete(svc)
    db.commit()
    return True
