"""
数据库 CRUD 操作
"""

from typing import List, Optional, Dict, Any, Union
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, asc, func

from ..config.constants import (
    PoolState,
    account_label_to_role_tag,
    normalize_account_label,
    normalize_pool_state,
    normalize_role_tag,
    role_tag_to_account_label,
)
from .models import (
    Account,
    EmailService,
    RegistrationTask,
    Setting,
    Proxy,
    CpaService,
    Sub2ApiService,
    BindCardTask,
    TeamInviteRecord,
    OperationAuditLog,
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
    cookies: Optional[str] = None,
    proxy_used: Optional[str] = None,
    expires_at: Optional['datetime'] = None,
    extra_data: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    account_label: Optional[str] = None,
    role_tag: Optional[str] = None,
    biz_tag: Optional[str] = None,
    pool_state: Optional[str] = None,
    pool_state_manual: Optional[str] = None,
    priority: Optional[int] = None,
    last_used_at: Optional['datetime'] = None,
) -> Account:
    """创建新账户"""
    normalized_role_tag = normalize_role_tag(
        role_tag if role_tag is not None else account_label_to_role_tag(account_label)
    )
    normalized_account_label = role_tag_to_account_label(normalized_role_tag)
    normalized_pool_state = normalize_pool_state(pool_state) if pool_state is not None else PoolState.CANDIDATE_POOL.value
    normalized_pool_state_manual = (
        normalize_pool_state(pool_state_manual) if pool_state_manual is not None and str(pool_state_manual).strip() else None
    )

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
        cookies=cookies,
        proxy_used=proxy_used,
        expires_at=expires_at,
        extra_data=extra_data or {},
        status=status or 'active',
        source=source or 'register',
        account_label=normalized_account_label,
        role_tag=normalized_role_tag,
        biz_tag=(str(biz_tag).strip() or None) if biz_tag is not None else None,
        pool_state=normalized_pool_state,
        pool_state_manual=normalized_pool_state_manual,
        priority=int(priority) if priority is not None else 50,
        last_used_at=last_used_at,
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
        if key == "role_tag" and value is not None:
            normalized_role = normalize_role_tag(value)
            db_account.role_tag = normalized_role
            db_account.account_label = role_tag_to_account_label(normalized_role)
            continue

        if key == "account_label" and value is not None:
            normalized_label = normalize_account_label(value)
            db_account.account_label = normalized_label
            db_account.role_tag = account_label_to_role_tag(normalized_label)
            continue

        if key in ("pool_state", "pool_state_manual"):
            if value is None:
                setattr(db_account, key, None)
            elif str(value).strip():
                setattr(db_account, key, normalize_pool_state(value))
            else:
                setattr(db_account, key, None)
            continue

        if key == "biz_tag":
            db_account.biz_tag = str(value).strip() or None if value is not None else None
            continue

        if key == "priority" and value is not None:
            try:
                db_account.priority = int(value)
            except Exception:
                db_account.priority = 50
            continue

        if hasattr(db_account, key) and value is not None:
            setattr(db_account, key, value)

    # 兜底双写：保证旧字段 account_label 与 role_tag 始终一致
    role_value = normalize_role_tag(getattr(db_account, "role_tag", None))
    db_account.role_tag = role_value
    db_account.account_label = role_tag_to_account_label(role_value)

    db.commit()
    db.refresh(db_account)
    return db_account


def delete_account(db: Session, account_id: int) -> bool:
    """删除账户"""
    def _detach_bind_card_tasks(snapshot_email: str):
        linked_tasks = db.query(BindCardTask).filter(BindCardTask.account_id == account_id).all()
        for task in linked_tasks:
            if not str(getattr(task, "account_email", "") or "").strip():
                task.account_email = snapshot_email
            task.account_id = None

    def _detach_team_invite_records(snapshot_email: str):
        linked_records = db.query(TeamInviteRecord).filter(TeamInviteRecord.inviter_account_id == account_id).all()
        for record in linked_records:
            if not str(getattr(record, "inviter_email", "") or "").strip():
                record.inviter_email = snapshot_email
            record.inviter_account_id = None

    db_account = get_account_by_id(db, account_id)
    if not db_account:
        return False

    try:
        # 正常路径：保留绑卡任务历史，先解绑再删账号
        _detach_bind_card_tasks(db_account.email)
        _detach_team_invite_records(db_account.email)
        db.flush()
        db.delete(db_account)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        err = str(e).lower()
        need_retry_with_migration = (
            "bind_card_tasks" in err and
            "account_id" in err and
            ("not null" in err or "constraint failed" in err or "foreign key" in err)
        )
        if not need_retry_with_migration:
            raise

        # 旧库结构兜底：先跑迁移，再重试一次删除
        from .session import get_session_manager
        get_session_manager().migrate_tables()

        db_account = get_account_by_id(db, account_id)
        if not db_account:
            return False
        try:
            _detach_bind_card_tasks(db_account.email)
            _detach_team_invite_records(db_account.email)
            db.flush()
            db.delete(db_account)
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise


def delete_accounts_batch(db: Session, account_ids: List[int]) -> int:
    """批量删除账户"""
    deleted = 0
    for account_id in account_ids:
        if delete_account(db, account_id):
            deleted += 1
    return deleted


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
    proxy: Optional[str] = None
) -> RegistrationTask:
    """创建注册任务"""
    db_task = RegistrationTask(
        task_uuid=task_uuid,
        email_service_id=email_service_id,
        proxy=proxy,
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
# 操作审计日志
# ============================================================================

def create_operation_audit_log(
    db: Session,
    *,
    actor: Optional[str],
    action: str,
    target_type: str,
    target_id: Optional[Union[str, int]] = None,
    target_email: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> OperationAuditLog:
    row = OperationAuditLog(
        actor=(str(actor or "").strip() or "system"),
        action=str(action or "").strip() or "unknown_action",
        target_type=str(target_type or "").strip() or "unknown_target",
        target_id=(str(target_id).strip() if target_id is not None else None),
        target_email=(str(target_email or "").strip() or None),
        payload=dict(payload or {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_operation_audit_logs(
    db: Session,
    *,
    limit: int = 100,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
) -> List[OperationAuditLog]:
    safe_limit = max(1, min(500, int(limit or 100)))
    query = db.query(OperationAuditLog)
    if action:
        query = query.filter(OperationAuditLog.action == str(action).strip())
    if target_type:
        query = query.filter(OperationAuditLog.target_type == str(target_type).strip())
    return query.order_by(desc(OperationAuditLog.id)).limit(safe_limit).all()


# ============================================================================
# 代理 CRUD
# ============================================================================

def _ensure_single_default_proxy(db: Session) -> Optional[Proxy]:
    """
    保证代理表中“有且仅有一个默认代理”：
    - 没有默认时，自动使用最早创建（ID 最小）的代理作为默认
    - 有多个默认时，仅保留最早的一个
    """
    proxies = db.query(Proxy).order_by(asc(Proxy.id)).all()
    if not proxies:
        return None

    default_proxies = [proxy for proxy in proxies if bool(proxy.is_default)]
    keeper = default_proxies[0] if default_proxies else proxies[0]
    changed = False

    if not keeper.is_default:
        keeper.is_default = True
        changed = True

    for proxy in proxies:
        should_default = proxy.id == keeper.id
        if bool(proxy.is_default) != should_default:
            proxy.is_default = should_default
            changed = True

    if changed:
        db.commit()
        db.refresh(keeper)

    return keeper


def create_proxy(
    db: Session,
    name: str,
    type: str,
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    enabled: bool = True,
    priority: int = 0
) -> Proxy:
    """创建代理配置"""
    db_proxy = Proxy(
        name=name,
        type=type,
        host=host,
        port=port,
        username=username,
        password=password,
        enabled=enabled,
        priority=priority
    )
    db.add(db_proxy)
    db.commit()
    db.refresh(db_proxy)

    # 统一默认代理策略：首次添加自动默认，多代理保持“第一个默认”直到手动切换。
    _ensure_single_default_proxy(db)
    db.refresh(db_proxy)
    return db_proxy


def get_proxy_by_id(db: Session, proxy_id: int) -> Optional[Proxy]:
    """根据 ID 获取代理"""
    return db.query(Proxy).filter(Proxy.id == proxy_id).first()


def get_proxies(
    db: Session,
    enabled: Optional[bool] = None,
    skip: int = 0,
    limit: int = 100
) -> List[Proxy]:
    """获取代理列表"""
    _ensure_single_default_proxy(db)
    query = db.query(Proxy)

    if enabled is not None:
        query = query.filter(Proxy.enabled == enabled)

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
    _ensure_single_default_proxy(db)
    return True


def update_proxy_last_used(db: Session, proxy_id: int) -> bool:
    """更新代理最后使用时间"""
    db_proxy = get_proxy_by_id(db, proxy_id)
    if not db_proxy:
        return False

    db_proxy.last_used = datetime.utcnow()
    db.commit()
    return True


def get_random_proxy(db: Session) -> Optional[Proxy]:
    """获取一个启用代理：优先默认代理，否则使用最早启用的代理。"""
    _ensure_single_default_proxy(db)

    # 优先返回启用状态下的默认代理
    default_proxy = (
        db.query(Proxy)
        .filter(Proxy.enabled == True, Proxy.is_default == True)
        .order_by(asc(Proxy.id))
        .first()
    )
    if default_proxy:
        return default_proxy

    # 默认代理不可用时，回退到最早启用的代理（稳定而可预期）
    return (
        db.query(Proxy)
        .filter(Proxy.enabled == True)
        .order_by(asc(Proxy.id))
        .first()
    )


def set_proxy_default(db: Session, proxy_id: int) -> Optional[Proxy]:
    """将指定代理设为默认，同时清除其他代理的默认标记"""
    proxy = db.query(Proxy).filter(Proxy.id == proxy_id).first()
    if not proxy:
        return None

    db.query(Proxy).filter(Proxy.id != proxy_id, Proxy.is_default == True).update({"is_default": False})
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
    proxy_url: Optional[str] = None,
    enabled: bool = True,
    priority: int = 0
) -> CpaService:
    """创建 CPA 服务配置"""
    db_service = CpaService(
        name=name,
        api_url=api_url,
        api_token=api_token,
        proxy_url=proxy_url,
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
    db.delete(db_service)
    db.commit()
    return True


# ============================================================================
# Sub2API 服务 CRUD
# ============================================================================

def create_sub2api_service(
    db: Session,
    name: str,
    api_url: str,
    api_key: str,
    target_type: str = 'sub2api',
    enabled: bool = True,
    priority: int = 0
) -> Sub2ApiService:
    """创建 Sub2API 服务配置"""
    svc = Sub2ApiService(
        name=name,
        api_url=api_url,
        api_key=api_key,
        target_type=target_type,
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
