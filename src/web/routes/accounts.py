"""
账号管理 API 路由
"""
import io
import json
import logging
import re
import zipfile
import base64
import time
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, HTTPException, Query, Body, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, func, or_

from ...config.constants import (
    AccountLabel,
    AccountStatus,
    RoleTag,
    account_label_to_role_tag,
    normalize_account_label,
    normalize_pool_state,
    normalize_role_tag,
    role_tag_to_account_label,
)
from ...config.settings import get_settings
from ...core.openai.overview import fetch_codex_overview
from ...core.openai.token_refresh import refresh_account_token as do_refresh
from ...core.openai.token_refresh import validate_account_token as do_validate
from ...core.upload.cpa_upload import generate_token_json, batch_upload_to_cpa, upload_to_cpa
from ...core.upload.team_manager_upload import upload_to_team_manager, batch_upload_to_team_manager
from ...core.upload.sub2api_upload import batch_upload_to_sub2api, upload_to_sub2api

from ...core.dynamic_proxy import get_proxy_url_for_task
from ...database import crud
from ...database.models import Account
from ...database.session import get_db
from ..task_manager import task_manager
from ..services.accounts_service import get_role_tag_counts as _service_get_role_tag_counts
from ..services.accounts_service import stream_accounts as _service_stream_accounts

logger = logging.getLogger(__name__)
router = APIRouter()

CURRENT_ACCOUNT_SETTING_KEY = "codex.current_account_id"
OVERVIEW_EXTRA_DATA_KEY = "codex_overview"
OVERVIEW_CARD_REMOVED_KEY = "codex_overview_card_removed"
OVERVIEW_CACHE_TTL_SECONDS = 300  # 5 分钟
PAID_SUBSCRIPTION_TYPES = ("plus", "team")
INVALID_ACCOUNT_STATUSES = (
    AccountStatus.FAILED.value,
    AccountStatus.EXPIRED.value,
    AccountStatus.BANNED.value,
)

ACCOUNT_ASYNC_TASK_MAX_KEEP = 300
ACCOUNT_ASYNC_EXECUTOR_MAX_WORKERS = 6
ACCOUNT_BATCH_REFRESH_ASYNC_MAX_WORKERS = 8
ACCOUNT_BATCH_REFRESH_RETRY_ATTEMPTS = 2
ACCOUNT_BATCH_REFRESH_RETRY_BASE_DELAY_SECONDS = 1.0
ACCOUNT_BATCH_VALIDATE_ASYNC_MAX_WORKERS = 8
ACCOUNT_BATCH_VALIDATE_SYNC_MAX_WORKERS = 12
ACCOUNT_BATCH_VALIDATE_RETRY_ATTEMPTS = 2
ACCOUNT_BATCH_VALIDATE_RETRY_BASE_DELAY_SECONDS = 0.8
ACCOUNT_BATCH_VALIDATE_HTTP_TIMEOUT_SECONDS = 18
ACCOUNT_OVERVIEW_REFRESH_MAX_WORKERS = 8
ACCOUNT_OVERVIEW_REFRESH_RETRY_ATTEMPTS = 2
ACCOUNT_OVERVIEW_REFRESH_RETRY_BASE_DELAY_SECONDS = 1.0
QUICK_REFRESH_TASK_WAIT_TIMEOUT_SECONDS = 40 * 60
QUICK_REFRESH_TASK_POLL_INTERVAL_SECONDS = 1.2
_account_async_tasks: Dict[str, Dict[str, Any]] = {}
_account_async_tasks_lock = threading.Lock()
_account_async_executor = ThreadPoolExecutor(
    max_workers=ACCOUNT_ASYNC_EXECUTOR_MAX_WORKERS,
    thread_name_prefix="account_async",
)


def _get_proxy(request_proxy: Optional[str] = None) -> Optional[str]:
    """获取代理 URL，策略与注册流程一致：代理列表 → 动态代理 → 静态配置"""
    if request_proxy:
        return request_proxy
    with get_db() as db:
        proxy = crud.get_random_proxy(db)
        if proxy:
            return proxy.proxy_url
    proxy_url = get_proxy_url_for_task()
    if proxy_url:
        return proxy_url
    return get_settings().proxy_url


def _apply_status_filter(query, status: Optional[str]):
    """
    统一状态筛选:
    - failed/invalid 视为“无效账号集合”（failed + expired + banned）
    - 其他值按精确状态筛选
    """
    normalized = (status or "").strip().lower()
    if not normalized:
        return query
    if normalized in {"failed", "invalid"}:
        return query.filter(Account.status.in_(INVALID_ACCOUNT_STATUSES))
    return query.filter(Account.status == normalized)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_actor(request: Optional[Request]) -> str:
    if request is None:
        return "system"
    header_keys = ("x-operator", "x-user", "x-username")
    for key in header_keys:
        value = str(request.headers.get(key) or "").strip()
        if value:
            return value[:120]
    client_host = ""
    try:
        client_host = str(getattr(getattr(request, "client", None), "host", "") or "").strip()
    except Exception:
        client_host = ""
    return f"api@{client_host}" if client_host else "api"


def _audit_account_action(
    db,
    *,
    actor: str,
    action: str,
    account: Optional[Account] = None,
    target_id: Optional[int] = None,
    target_email: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        crud.create_operation_audit_log(
            db,
            actor=actor,
            action=action,
            target_type="account",
            target_id=target_id if target_id is not None else getattr(account, "id", None),
            target_email=target_email if target_email is not None else getattr(account, "email", None),
            payload=payload or {},
        )
    except Exception:
        logger.warning("写入账号操作审计日志失败: action=%s", action, exc_info=True)


def _iter_query_in_batches(query, batch_size: int = 200) -> Iterator[Account]:
    """
    分批迭代查询结果，避免一次性 all() 把全量记录加载进内存。
    """
    yield from _service_stream_accounts(query, batch_size=batch_size)


def _cleanup_account_async_tasks_locked():
    """限制内存中的异步任务数量，优先清理已结束的旧任务。"""
    total = len(_account_async_tasks)
    if total <= ACCOUNT_ASYNC_TASK_MAX_KEEP:
        return

    overflow = total - ACCOUNT_ASYNC_TASK_MAX_KEEP
    finished_keys = [
        (task_id, _account_async_tasks[task_id].get("_created_ts", 0))
        for task_id in _account_async_tasks
        if _account_async_tasks[task_id].get("status") in {"completed", "failed", "cancelled"}
    ]
    finished_keys.sort(key=lambda item: item[1])

    removed = 0
    for task_id, _ in finished_keys:
        if removed >= overflow:
            break
        _account_async_tasks.pop(task_id, None)
        removed += 1

    # 如果当前大多是运行中任务，不强制裁剪，避免前端轮询中的任务被提前清理。


def _create_account_async_task(task_type: str, total: int = 0, payload: Optional[dict] = None) -> str:
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "task_type": task_type,
        "status": "pending",
        "message": "任务已创建，等待执行",
        "created_at": _utc_now_iso(),
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
        "pause_requested": False,
        "paused": False,
        "progress": {
            "total": max(0, int(total or 0)),
            "completed": 0,
            "success": 0,
            "failed": 0,
        },
        "result": None,
        "error": None,
        "payload": payload or {},
        "details": [],
        "_created_ts": time.time(),
    }
    with _account_async_tasks_lock:
        _account_async_tasks[task_id] = task
        _cleanup_account_async_tasks_locked()
    task_manager.register_domain_task(
        domain="accounts",
        task_id=task_id,
        task_type=task_type,
        payload=payload or {},
        progress=task["progress"],
    )
    return task_id


def _get_account_async_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _account_async_tasks_lock:
        return _account_async_tasks.get(task_id)


def _get_account_async_task_or_404(task_id: str) -> Dict[str, Any]:
    task = _get_account_async_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def _build_account_async_task_snapshot(task: Dict[str, Any]) -> Dict[str, Any]:
    data = {
        "id": task.get("id"),
        "task_type": task.get("task_type"),
        "status": task.get("status"),
        "message": task.get("message"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "cancel_requested": bool(task.get("cancel_requested")),
        "pause_requested": bool(task.get("pause_requested")),
        "paused": bool(task.get("paused")),
        "progress": task.get("progress") or {},
        "payload": task.get("payload") or {},
        "result": task.get("result"),
        "error": task.get("error"),
        "details": task.get("details") or [],
    }
    return data


def _update_account_async_task(task_id: str, **fields):
    with _account_async_tasks_lock:
        task = _account_async_tasks.get(task_id)
        if not task:
            return
        task.update(fields)
    task_manager.update_domain_task("accounts", task_id, **fields)


def _append_account_async_task_detail(task_id: str, detail: dict, max_items: int = 500):
    with _account_async_tasks_lock:
        task = _account_async_tasks.get(task_id)
        if not task:
            return
        details = task.setdefault("details", [])
        details.append(detail)
        if len(details) > max_items:
            task["details"] = details[-max_items:]
    task_manager.append_domain_task_detail("accounts", task_id, detail, max_items=max_items)


def _set_account_async_task_progress(task_id: str, *, completed: int, success: int, failed: int, total: Optional[int] = None):
    with _account_async_tasks_lock:
        task = _account_async_tasks.get(task_id)
        if not task:
            return
        progress = task.setdefault("progress", {})
        if total is not None:
            progress["total"] = max(0, int(total))
        progress["completed"] = max(0, int(completed))
        progress["success"] = max(0, int(success))
        progress["failed"] = max(0, int(failed))
    payload = {
        "completed": max(0, int(completed)),
        "success": max(0, int(success)),
        "failed": max(0, int(failed)),
    }
    if total is not None:
        payload["total"] = max(0, int(total))
    task_manager.set_domain_task_progress("accounts", task_id, **payload)


def _is_account_async_task_cancel_requested(task_id: str) -> bool:
    local_requested = False
    with _account_async_tasks_lock:
        task = _account_async_tasks.get(task_id)
        local_requested = bool(task and task.get("cancel_requested"))
    return local_requested or task_manager.is_domain_task_cancel_requested("accounts", task_id)


def _is_account_async_task_pause_requested(task_id: str) -> bool:
    local_requested = False
    with _account_async_tasks_lock:
        task = _account_async_tasks.get(task_id)
        local_requested = bool(task and task.get("pause_requested"))
    return local_requested or task_manager.is_domain_task_pause_requested("accounts", task_id)


def _wait_if_account_async_task_paused(task_id: str, running_message: str) -> bool:
    paused_once = False
    while True:
        if _is_account_async_task_cancel_requested(task_id):
            return False
        if not _is_account_async_task_pause_requested(task_id):
            if paused_once:
                _update_account_async_task(
                    task_id,
                    status="running",
                    paused=False,
                    message=running_message,
                )
            return True
        if not paused_once:
            _update_account_async_task(
                task_id,
                status="paused",
                paused=True,
                message="任务已暂停，等待继续",
            )
            paused_once = True
        time.sleep(0.35)


def _run_account_async_task_guard(task_id: str, task_type: str, worker, *args):
    acquired, running, quota = task_manager.try_acquire_domain_slot("accounts", task_id)
    if not acquired:
        reason = f"并发配额已满（running={running}, quota={quota}）"
        _update_account_async_task(
            task_id,
            status="failed",
            finished_at=_utc_now_iso(),
            message=reason,
            error=reason,
            paused=False,
        )
        return
    try:
        worker(task_id, *args)
    except Exception as exc:
        logger.exception("异步任务执行失败: task_id=%s type=%s error=%s", task_id, task_type, exc)
        _update_account_async_task(
            task_id,
            status="failed",
            finished_at=_utc_now_iso(),
            message=f"任务异常: {exc}",
            error=str(exc),
            paused=False,
        )
    finally:
        task_manager.release_domain_slot("accounts", task_id)


# ============== Pydantic Models ==============

class AccountResponse(BaseModel):
    """账号响应模型"""
    id: int
    email: str
    password: Optional[str] = None
    client_id: Optional[str] = None
    email_service: str
    account_id: Optional[str] = None
    workspace_id: Optional[str] = None
    device_id: Optional[str] = None
    registered_at: Optional[str] = None
    last_refresh: Optional[str] = None
    expires_at: Optional[str] = None
    status: str
    proxy_used: Optional[str] = None
    cpa_uploaded: bool = False
    cpa_uploaded_at: Optional[str] = None
    account_label: str = AccountLabel.NONE.value
    role_tag: str = RoleTag.NONE.value
    biz_tag: Optional[str] = None
    pool_state: str = "candidate_pool"
    pool_state_manual: Optional[str] = None
    last_pool_sync_at: Optional[str] = None
    priority: int = 50
    last_used_at: Optional[str] = None
    subscription_type: Optional[str] = None
    subscription_at: Optional[str] = None
    cookies: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class AccountListResponse(BaseModel):
    """账号列表响应"""
    total: int
    accounts: List[AccountResponse]


class AccountUpdateRequest(BaseModel):
    """账号更新请求"""
    status: Optional[str] = None
    metadata: Optional[dict] = None
    cookies: Optional[str] = None  # 完整 cookie 字符串，用于支付请求
    session_token: Optional[str] = None
    role_tag: Optional[str] = None
    biz_tag: Optional[str] = None
    pool_state_manual: Optional[str] = None
    priority: Optional[int] = None


class ManualAccountCreateRequest(BaseModel):
    """手动创建账号请求"""
    email: str
    password: str
    email_service: Optional[str] = "manual"
    status: Optional[str] = AccountStatus.ACTIVE.value
    client_id: Optional[str] = None
    account_id: Optional[str] = None
    workspace_id: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None
    session_token: Optional[str] = None
    cookies: Optional[str] = None
    proxy_used: Optional[str] = None
    source: Optional[str] = "manual"
    account_label: Optional[str] = AccountLabel.NONE.value
    role_tag: Optional[str] = None
    biz_tag: Optional[str] = None
    priority: Optional[int] = 50
    subscription_type: Optional[str] = None
    metadata: Optional[dict] = None


class AccountImportItem(BaseModel):
    """账号导入项（支持按账号详情字段导入）"""
    email: str
    password: Optional[str] = None
    email_service: Optional[str] = "manual"
    status: Optional[str] = AccountStatus.ACTIVE.value
    client_id: Optional[str] = None
    account_id: Optional[str] = None
    workspace_id: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None
    session_token: Optional[str] = None
    cookies: Optional[str] = None
    proxy_used: Optional[str] = None
    source: Optional[str] = "import"
    account_label: Optional[str] = AccountLabel.NONE.value
    role_tag: Optional[str] = None
    biz_tag: Optional[str] = None
    pool_state: Optional[str] = None
    pool_state_manual: Optional[str] = None
    priority: Optional[int] = 50
    subscription_type: Optional[str] = None
    plan_type: Optional[str] = None
    auth_mode: Optional[str] = None
    user_id: Optional[str] = None
    organization_id: Optional[str] = None
    account_name: Optional[str] = None
    account_structure: Optional[str] = None
    tokens: Optional[dict] = None
    quota: Optional[dict] = None
    tags: Optional[Any] = None
    created_at: Optional[Any] = None
    last_used: Optional[Any] = None
    metadata: Optional[dict] = None


class ImportAccountsRequest(BaseModel):
    """批量导入账号请求"""
    accounts: List[dict]
    overwrite: bool = False


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


class BatchUpdateRequest(BaseModel):
    """批量更新请求"""
    ids: List[int]
    status: str


class OverviewRefreshRequest(BaseModel):
    """账号总览刷新请求"""
    ids: List[int] = []
    force: bool = True
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    proxy: Optional[str] = None


class OverviewCardDeleteRequest(BaseModel):
    """账号总览卡片删除（仅从卡片移除，不删除账号）"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


# ============== Helper Functions ==============

def resolve_account_ids(
    db,
    ids: List[int],
    select_all: bool = False,
    status_filter: Optional[str] = None,
    email_service_filter: Optional[str] = None,
    search_filter: Optional[str] = None,
) -> List[int]:
    """当 select_all=True 时查询全部符合条件的 ID，否则直接返回传入的 ids"""
    if not select_all:
        return ids
    query = db.query(Account.id)
    if status_filter:
        query = _apply_status_filter(query, status_filter)
    if email_service_filter:
        query = query.filter(Account.email_service == email_service_filter)
    if search_filter:
        pattern = f"%{search_filter}%"
        query = query.filter(
            (Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern))
        )
    return [row[0] for row in query.all()]


def _resolve_account_role_tag(account: Account) -> str:
    role_value = str(getattr(account, "role_tag", "") or "").strip()
    if role_value:
        normalized_role = normalize_role_tag(role_value)
        if normalized_role != RoleTag.NONE.value:
            return normalized_role
    return account_label_to_role_tag(getattr(account, "account_label", None))


def _resolve_account_pool_state(account: Account) -> str:
    return normalize_pool_state(getattr(account, "pool_state", None))


def _set_account_role_tag(account: Account, role_tag: Optional[str]) -> str:
    normalized_role = normalize_role_tag(role_tag)
    account.role_tag = normalized_role
    account.account_label = role_tag_to_account_label(normalized_role)
    return normalized_role


def account_to_response(account: Account) -> AccountResponse:
    """转换 Account 模型为响应模型"""
    return AccountResponse(
        id=account.id,
        email=account.email,
        password=account.password,
        client_id=account.client_id,
        email_service=account.email_service,
        account_id=account.account_id,
        workspace_id=account.workspace_id,
        device_id=_resolve_account_device_id(account),
        registered_at=account.registered_at.isoformat() if account.registered_at else None,
        last_refresh=account.last_refresh.isoformat() if account.last_refresh else None,
        expires_at=account.expires_at.isoformat() if account.expires_at else None,
        status=account.status,
        proxy_used=account.proxy_used,
        cpa_uploaded=account.cpa_uploaded or False,
        cpa_uploaded_at=account.cpa_uploaded_at.isoformat() if account.cpa_uploaded_at else None,
        account_label=normalize_account_label(getattr(account, "account_label", None)),
        role_tag=_resolve_account_role_tag(account),
        biz_tag=(str(getattr(account, "biz_tag", "") or "").strip() or None),
        pool_state=_resolve_account_pool_state(account),
        pool_state_manual=(str(getattr(account, "pool_state_manual", "") or "").strip() or None),
        last_pool_sync_at=account.last_pool_sync_at.isoformat() if getattr(account, "last_pool_sync_at", None) else None,
        priority=int(getattr(account, "priority", 50) or 50),
        last_used_at=account.last_used_at.isoformat() if getattr(account, "last_used_at", None) else None,
        subscription_type=account.subscription_type,
        subscription_at=account.subscription_at.isoformat() if account.subscription_at else None,
        cookies=account.cookies,
        created_at=account.created_at.isoformat() if account.created_at else None,
        updated_at=account.updated_at.isoformat() if account.updated_at else None,
    )


def _extract_cookie_value(cookies_text: Optional[str], cookie_name: str) -> str:
    text = str(cookies_text or "")
    if not text:
        return ""
    pattern = re.compile(rf"(?:^|;\s*){re.escape(cookie_name)}=([^;]+)")
    match = pattern.search(text)
    return str(match.group(1) or "").strip() if match else ""


def _extract_session_token_from_cookie_text(cookies_text: Optional[str]) -> str:
    """从完整 cookie 字符串中提取 next-auth session token（兼容分片）。"""
    text = str(cookies_text or "")
    if not text:
        return ""

    direct = re.search(r"(?:^|;\s*)__Secure-next-auth\.session-token=([^;]+)", text)
    if direct:
        return str(direct.group(1) or "").strip()

    parts = re.findall(r"(?:^|;\s*)__Secure-next-auth\.session-token\.(\d+)=([^;]+)", text)
    if not parts:
        return ""

    chunk_map = {}
    for idx, value in parts:
        try:
            chunk_map[int(idx)] = str(value or "")
        except Exception:
            continue
    if not chunk_map:
        return ""

    return "".join(chunk_map[i] for i in sorted(chunk_map.keys()))


def _resolve_account_device_id(account: Account) -> str:
    """
    解析账号 device_id（兼容历史数据）:
    1) account.device_id（若模型未来扩展该字段）
    2) cookies 里的 oai-did
    3) extra_data 中的 device_id/oai_did/oai-device-id
    """
    direct = str(getattr(account, "device_id", "") or "").strip()
    if direct:
        return direct

    did_in_cookie = _extract_cookie_value(getattr(account, "cookies", None), "oai-did")
    if did_in_cookie:
        return did_in_cookie

    extra_data = getattr(account, "extra_data", None)
    if isinstance(extra_data, dict):
        for key in ("device_id", "oai_did", "oai-device-id"):
            value = str(extra_data.get(key) or "").strip()
            if value:
                return value
    return ""


def _resolve_account_session_token(account: Account) -> str:
    """解析账号 session_token（优先 DB 字段，其次 cookies 文本）。"""
    db_token = str(getattr(account, "session_token", "") or "").strip()
    if db_token:
        return db_token
    return _extract_session_token_from_cookie_text(getattr(account, "cookies", None))


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _normalize_plan_type(raw_plan: Optional[str]) -> str:
    value = (raw_plan or "").strip().lower()
    if not value:
        return "Basic"
    if "team" in value or "enterprise" in value:
        return "Team"
    if "plus" in value:
        return "Plus"
    if "pro" in value:
        return "Pro"
    if "free" in value or "basic" in value:
        return "Basic"
    return value.capitalize()


def _build_unknown_quota() -> dict:
    return {
        "used": None,
        "total": None,
        "remaining": None,
        "percentage": None,
        "reset_at": None,
        "reset_in_text": "-",
        "status": "unknown",
    }


def _fallback_overview(account: Account, error_message: Optional[str] = None, stale: bool = False) -> dict:
    data = {
        "plan_type": _normalize_plan_type(account.subscription_type),
        "plan_source": "db.subscription_type" if account.subscription_type else "default",
        "hourly_quota": _build_unknown_quota(),
        "weekly_quota": _build_unknown_quota(),
        "code_review_quota": _build_unknown_quota(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources": [],
        "stale": stale,
    }
    if error_message:
        data["error"] = error_message
    return data


def _is_overview_cache_stale(cached_overview: Optional[dict]) -> bool:
    if not isinstance(cached_overview, dict):
        return True
    fetched_at = _parse_iso_datetime(cached_overview.get("fetched_at"))
    if not fetched_at:
        return True
    age = datetime.now(timezone.utc) - fetched_at
    return age > timedelta(seconds=OVERVIEW_CACHE_TTL_SECONDS)


def _get_current_account_id(db) -> Optional[int]:
    setting = crud.get_setting(db, CURRENT_ACCOUNT_SETTING_KEY)
    if not setting or not setting.value:
        return None
    try:
        return int(setting.value)
    except (TypeError, ValueError):
        return None


def _set_current_account_id(db, account_id: int):
    crud.set_setting(
        db,
        key=CURRENT_ACCOUNT_SETTING_KEY,
        value=str(account_id),
        description="当前切换中的 Codex 账号 ID",
        category="accounts",
    )


def _is_overview_card_removed(account: Account) -> bool:
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    return bool(extra_data.get(OVERVIEW_CARD_REMOVED_KEY))


def _set_overview_card_removed(account: Account, removed: bool):
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    merged = dict(extra_data)
    if removed:
        merged[OVERVIEW_CARD_REMOVED_KEY] = True
    else:
        merged.pop(OVERVIEW_CARD_REMOVED_KEY, None)
    account.extra_data = merged


def _write_current_account_snapshot(account: Account) -> Optional[str]:
    """
    写入当前账号快照文件，便于外部流程读取当前账号令牌。
    """
    try:
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        output_file = data_dir / "current_codex_account.json"
        payload = {
            "id": account.id,
            "email": account.email,
            "plan_type": _normalize_plan_type(account.subscription_type),
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
            "session_token": account.session_token,
            "account_id": account.account_id,
            "workspace_id": account.workspace_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(output_file)
    except Exception as exc:
        logger.warning(f"写入 current_codex_account.json 失败: {exc}")
        return None


def _plan_to_subscription_type(plan_type: Optional[str]) -> Optional[str]:
    key = (plan_type or "").strip().lower()
    if key.startswith("team"):
        return "team"
    if key.startswith("plus"):
        return "plus"
    return None


def _normalize_subscription_input(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in ("team", "enterprise"):
        return "team"
    if raw in ("plus", "pro"):
        return "plus"
    if raw in ("free", "basic", "none", "null"):
        return None
    if "team" in raw:
        return "team"
    if "plus" in raw or "pro" in raw:
        return "plus"
    return None


def _is_paid_subscription(value: Optional[str]) -> bool:
    """是否为付费订阅（plus/team）。"""
    normalized = _normalize_subscription_input(value)
    return normalized in PAID_SUBSCRIPTION_TYPES


def _promote_child_label_if_paid(account: Account, subscription_type: Optional[str], *, reason: str) -> bool:
    """
    历史兼容函数：关闭“付费后自动子号升母号”。
    账号标签仅允许手动修改或专用业务入口修改，避免 Team 加入后误升母号。
    """
    _ = account
    _ = subscription_type
    _ = reason
    return False


def _pick_first_text(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _decode_jwt_payload_unverified(token: Optional[str]) -> Dict[str, Any]:
    """
    无签名校验解码 JWT payload，仅用于导入兜底字段提取。
    """
    text = str(token or "").strip()
    if not text or "." not in text:
        return {}
    try:
        parts = text.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload_raw = base64.urlsafe_b64decode((payload_b64 + padding).encode("utf-8"))
        payload = json.loads(payload_raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _get_nested(data: Dict[str, Any], path: List[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _get_account_overview_data(
    db,
    account: Account,
    force_refresh: bool = False,
    proxy: Optional[str] = None,
    allow_network: bool = True,
) -> tuple[dict, bool]:
    updated = False
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    cached = extra_data.get(OVERVIEW_EXTRA_DATA_KEY) if isinstance(extra_data, dict) else None
    cache_stale = _is_overview_cache_stale(cached)

    if not account.access_token:
        if cached:
            stale_cached = dict(cached)
            stale_cached["stale"] = True
            stale_cached["error"] = "missing_access_token"
            return stale_cached, updated
        return _fallback_overview(account, error_message="missing_access_token"), updated

    if not force_refresh and cached and not cache_stale:
        return cached, updated

    # 首屏卡片列表默认走“缓存优先”模式，避免首次进入被远端配额请求阻塞导致网络异常。
    if not allow_network:
        if cached:
            stale_cached = dict(cached)
            if cache_stale:
                stale_cached["stale"] = True
                stale_cached.setdefault("error", "cache_stale")
            return stale_cached, updated
        return _fallback_overview(account, error_message="cache_miss", stale=True), updated

    try:
        overview = fetch_codex_overview(account, proxy=proxy)
        if cached and not force_refresh:
            for key in ("hourly_quota", "weekly_quota", "code_review_quota"):
                if (
                    isinstance(cached.get(key), dict)
                    and isinstance(overview.get(key), dict)
                    and overview[key].get("status") == "unknown"
                    and cached[key].get("status") == "ok"
                ):
                    overview[key] = cached[key]

        # 用高置信度来源同步本地订阅状态，确保 Plus/Team 判断可复用。
        plan_source = str(overview.get("plan_source") or "")
        trusted_plan_sources = (
            "me.",
            "wham_usage.",
            "codex_usage.",
            "id_token.",
            "access_token.",
        )
        if any(plan_source.startswith(prefix) for prefix in trusted_plan_sources):
            current_sub = _normalize_subscription_input(account.subscription_type)
            detected_sub = _plan_to_subscription_type(overview.get("plan_type"))
            # 避免把本地已确认的付费订阅（plus/team）被远端偶发 free/basic 覆盖降级。
            if detected_sub and current_sub != detected_sub:
                account.subscription_type = detected_sub
                account.subscription_at = datetime.utcnow() if detected_sub else None
                _promote_child_label_if_paid(account, detected_sub, reason="overview_detected_paid")
                updated = True
            elif not detected_sub and current_sub in PAID_SUBSCRIPTION_TYPES:
                logger.info(
                    "总览订阅同步跳过降级: email=%s current=%s detected=%s source=%s",
                    account.email,
                    current_sub,
                    detected_sub or "free/basic",
                    plan_source,
                )

        merged_extra = dict(extra_data)
        merged_extra[OVERVIEW_EXTRA_DATA_KEY] = overview
        account.extra_data = merged_extra
        updated = True
        return overview, updated
    except Exception as exc:
        logger.warning(f"刷新账号[{account.email}]总览失败: {exc}")
        if cached:
            stale_cached = dict(cached)
            stale_cached["stale"] = True
            stale_cached["error"] = str(exc)
            return stale_cached, updated
        return _fallback_overview(account, error_message=str(exc), stale=True), updated


# ============== API Endpoints ==============

@router.post("", response_model=AccountResponse)
async def create_manual_account(request: ManualAccountCreateRequest):
    """
    手动新增账号（邮箱 + 密码）。
    """
    email = (request.email or "").strip().lower()
    password = (request.password or "").strip()
    email_service = (request.email_service or "manual").strip() or "manual"
    status = request.status or AccountStatus.ACTIVE.value
    source = (request.source or "manual").strip() or "manual"
    role_tag = normalize_role_tag(
        request.role_tag if request.role_tag is not None else request.account_label
    )
    account_label = role_tag_to_account_label(role_tag)
    subscription_type = _normalize_subscription_input(request.subscription_type)

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if not password:
        raise HTTPException(status_code=400, detail="密码不能为空")
    if status not in [e.value for e in AccountStatus]:
        raise HTTPException(status_code=400, detail="无效的状态值")

    with get_db() as db:
        exists = crud.get_account_by_email(db, email)
        if exists:
            raise HTTPException(status_code=409, detail="该邮箱账号已存在")

        try:
            account = crud.create_account(
                db,
                email=email,
                password=password,
                email_service=email_service,
                status=status,
                source=source,
                client_id=request.client_id,
                account_id=request.account_id,
                workspace_id=request.workspace_id,
                access_token=request.access_token,
                refresh_token=request.refresh_token,
                id_token=request.id_token,
                session_token=request.session_token,
                cookies=request.cookies,
                proxy_used=request.proxy_used,
                extra_data=request.metadata or {},
                account_label=account_label,
                role_tag=role_tag,
                biz_tag=request.biz_tag,
                priority=request.priority if request.priority is not None else 50,
            )
            if subscription_type:
                account.subscription_type = subscription_type
                account.subscription_at = datetime.utcnow()
                _promote_child_label_if_paid(account, subscription_type, reason="manual_create_paid")
                db.commit()
                db.refresh(account)
        except Exception as exc:
            logger.error(f"手动创建账号失败: {exc}")
            raise HTTPException(status_code=500, detail="创建账号失败")

        return account_to_response(account)


@router.post("/import")
async def import_accounts(request: ImportAccountsRequest):
    """
    一键导入账号（账号总览卡片使用）。
    支持按账号详情字段导入；可选覆盖同邮箱已有账号。
    """
    items = request.accounts or []
    if not items:
        raise HTTPException(status_code=400, detail="导入数据为空")

    max_import = 1000
    if len(items) > max_import:
        raise HTTPException(status_code=400, detail=f"单次最多导入 {max_import} 条")

    result = {
        "success": True,
        "total": len(items),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }

    def _safe_text(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    with get_db() as db:
        for index, raw_item in enumerate(items, start=1):
            if not isinstance(raw_item, dict):
                result["failed"] += 1
                result["errors"].append(
                    {"index": index, "email": "-", "error": "导入项必须是 JSON 对象"}
                )
                continue

            try:
                item = AccountImportItem.model_validate(raw_item)
            except Exception as exc:
                result["failed"] += 1
                result["errors"].append(
                    {"index": index, "email": str(raw_item.get("email") or "-"), "error": f"字段格式错误: {exc}"}
                )
                continue

            token_bundle = item.tokens if isinstance(item.tokens, dict) else {}
            access_token = _pick_first_text(item.access_token, token_bundle.get("access_token"), token_bundle.get("accessToken"))
            refresh_token = _pick_first_text(item.refresh_token, token_bundle.get("refresh_token"), token_bundle.get("refreshToken"))
            id_token = _pick_first_text(item.id_token, token_bundle.get("id_token"), token_bundle.get("idToken"))
            session_token = _pick_first_text(
                item.session_token,
                token_bundle.get("session_token"),
                token_bundle.get("sessionToken"),
            )
            client_id = _pick_first_text(item.client_id, token_bundle.get("client_id"), token_bundle.get("clientId"))

            access_claims = _decode_jwt_payload_unverified(access_token)
            id_claims = _decode_jwt_payload_unverified(id_token)

            auth_claims = {}
            for claims in (access_claims, id_claims):
                auth_obj = _get_nested(claims, ["https://api.openai.com/auth"])
                if isinstance(auth_obj, dict):
                    auth_claims = auth_obj
                    break

            account_id_value = _pick_first_text(
                item.account_id,
                raw_item.get("account_id"),
                auth_claims.get("chatgpt_account_id"),
            )
            workspace_id_value = _pick_first_text(
                item.workspace_id,
                raw_item.get("workspace_id"),
                account_id_value,
            )

            if not client_id:
                id_aud = id_claims.get("aud")
                id_aud_first = id_aud[0] if isinstance(id_aud, list) and id_aud else None
                client_id = _pick_first_text(
                    access_claims.get("client_id"),
                    id_aud_first,
                )

            email = str(item.email or "").strip().lower()
            if not email or "@" not in email:
                result["failed"] += 1
                result["errors"].append({"index": index, "email": email or "-", "error": "邮箱格式不正确"})
                continue

            status = str(item.status or AccountStatus.ACTIVE.value).strip().lower()
            if status not in [e.value for e in AccountStatus]:
                status = AccountStatus.ACTIVE.value

            email_service = str(item.email_service or "manual").strip() or "manual"
            source = str(item.source or "import").strip() or "import"
            raw_role_tag = _pick_first_text(
                item.role_tag,
                raw_item.get("role_tag"),
                raw_item.get("registration_type"),
                item.account_label,
                raw_item.get("account_label"),
            )
            role_tag = normalize_role_tag(raw_role_tag) if raw_role_tag is not None else RoleTag.NONE.value
            account_label = role_tag_to_account_label(role_tag)
            biz_tag = _pick_first_text(item.biz_tag, raw_item.get("biz_tag"))
            raw_pool_state = _pick_first_text(item.pool_state, raw_item.get("pool_state"))
            pool_state = normalize_pool_state(raw_pool_state) if raw_pool_state is not None else None
            raw_pool_state_manual = _pick_first_text(item.pool_state_manual, raw_item.get("pool_state_manual"))
            pool_state_manual = normalize_pool_state(raw_pool_state_manual) if raw_pool_state_manual is not None else None
            try:
                priority_value = int(item.priority) if item.priority is not None else 50
            except Exception:
                priority_value = 50
            subscription_type = (
                _normalize_subscription_input(item.subscription_type)
                or _normalize_subscription_input(item.plan_type)
                or _normalize_subscription_input(_pick_first_text(
                    raw_item.get("plan_type"),
                    auth_claims.get("chatgpt_plan_type"),
                ))
            )
            metadata = dict(item.metadata) if isinstance(item.metadata, dict) else {}
            for extra_key in (
                "id",
                "auth_mode",
                "user_id",
                "organization_id",
                "account_name",
                "account_structure",
                "quota",
                "tags",
                "created_at",
                "last_used",
                "usage_updated_at",
                "plan_type",
            ):
                value = raw_item.get(extra_key)
                if value is not None:
                    metadata[extra_key] = value
            if isinstance(token_bundle, dict) and token_bundle:
                metadata["tokens_shape"] = list(token_bundle.keys())

            exists = crud.get_account_by_email(db, email)
            if exists and not request.overwrite:
                result["skipped"] += 1
                continue

            try:
                if exists and request.overwrite:
                    update_payload = {
                        "password": _safe_text(item.password),
                        "email_service": email_service,
                        "status": status,
                        "client_id": _safe_text(client_id),
                        "account_id": _safe_text(account_id_value),
                        "workspace_id": _safe_text(workspace_id_value),
                        "access_token": _safe_text(access_token),
                        "refresh_token": _safe_text(refresh_token),
                        "id_token": _safe_text(id_token),
                        "session_token": _safe_text(session_token),
                        "cookies": item.cookies if item.cookies is not None else None,
                        "proxy_used": _safe_text(item.proxy_used),
                        "source": source,
                        "account_label": account_label,
                        "role_tag": role_tag,
                        "biz_tag": biz_tag,
                        "pool_state": pool_state,
                        "pool_state_manual": pool_state_manual,
                        "priority": priority_value,
                        "extra_data": metadata,
                        "last_refresh": datetime.utcnow(),
                    }
                    clean_update_payload = {k: v for k, v in update_payload.items() if v is not None}
                    account = crud.update_account(db, exists.id, **clean_update_payload)
                    if account is None:
                        raise RuntimeError("更新账号失败")
                    account.subscription_type = subscription_type
                    account.subscription_at = datetime.utcnow() if subscription_type else None
                    _promote_child_label_if_paid(account, subscription_type, reason="import_overwrite_paid")
                    db.commit()
                    result["updated"] += 1
                    continue

                account = crud.create_account(
                    db,
                    email=email,
                    password=_safe_text(item.password),
                    client_id=_safe_text(client_id),
                    session_token=_safe_text(session_token),
                    email_service=email_service,
                    account_id=_safe_text(account_id_value),
                    workspace_id=_safe_text(workspace_id_value),
                    access_token=_safe_text(access_token),
                    refresh_token=_safe_text(refresh_token),
                    id_token=_safe_text(id_token),
                    cookies=item.cookies,
                    proxy_used=_safe_text(item.proxy_used),
                    extra_data=metadata,
                    status=status,
                    source=source,
                    account_label=account_label,
                    role_tag=role_tag,
                    biz_tag=biz_tag,
                    pool_state=pool_state,
                    pool_state_manual=pool_state_manual,
                    priority=priority_value,
                )
                if subscription_type:
                    account.subscription_type = subscription_type
                    account.subscription_at = datetime.utcnow()
                    _promote_child_label_if_paid(account, subscription_type, reason="import_create_paid")
                    db.commit()
                result["created"] += 1
            except Exception as exc:
                result["failed"] += 1
                result["errors"].append({"index": index, "email": email, "error": str(exc)})

    return result


@router.get("", response_model=AccountListResponse)
async def list_accounts(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
    role_tag: Optional[str] = Query(None, description="角色标签筛选：parent/child/none"),
    pool_state: Optional[str] = Query(None, description="池状态筛选：team_pool/candidate_pool/blocked"),
    biz_tag: Optional[str] = Query(None, description="业务标签筛选"),
    search: Optional[str] = Query(None, description="搜索关键词"),
):
    """
    获取账号列表

    支持分页、状态筛选、邮箱服务筛选和搜索
    """
    with get_db() as db:
        # 构建查询
        query = db.query(Account)

        # 状态筛选
        if status:
            query = _apply_status_filter(query, status)

        # 邮箱服务筛选
        if email_service:
            query = query.filter(Account.email_service == email_service)

        # 角色标签筛选
        if role_tag:
            normalized_role = normalize_role_tag(role_tag)
            fallback_label = role_tag_to_account_label(normalized_role)
            query = query.filter(
                or_(
                    func.lower(func.coalesce(Account.role_tag, "")) == normalized_role,
                    and_(
                        func.trim(func.coalesce(Account.role_tag, "")) == "",
                        func.lower(func.coalesce(Account.account_label, "")) == fallback_label,
                    ),
                )
            )

        # 池状态筛选
        if pool_state:
            query = query.filter(Account.pool_state == normalize_pool_state(pool_state))

        # 业务标签筛选
        if biz_tag:
            query = query.filter(Account.biz_tag == str(biz_tag).strip())

        # 搜索
        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                (Account.email.ilike(search_pattern)) |
                (Account.account_id.ilike(search_pattern))
            )

        # 统计总数
        total = query.count()

        # 分页
        offset = (page - 1) * page_size
        accounts = query.order_by(Account.created_at.desc()).offset(offset).limit(page_size).all()

        return AccountListResponse(
            total=total,
            accounts=[account_to_response(acc) for acc in accounts]
        )


@router.get("/overview/cards")
async def list_accounts_overview_cards(
    refresh: bool = Query(False, description="是否强制刷新远端配额"),
    search: Optional[str] = Query(None, description="按邮箱搜索"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
    proxy: Optional[str] = Query(None, description="可选代理地址"),
):
    """
    账号总览卡片数据。
    """
    with get_db() as db:
        query = db.query(Account).filter(
            func.lower(Account.subscription_type).in_(PAID_SUBSCRIPTION_TYPES)
        )
        if search:
            pattern = f"%{search}%"
            query = query.filter((Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern)))
        if status:
            query = _apply_status_filter(query, status)
        if email_service:
            query = query.filter(Account.email_service == email_service)

        ordered_query = query.order_by(Account.created_at.desc())
        accounts = []
        for account in _iter_query_in_batches(ordered_query, batch_size=200):
            if _is_overview_card_removed(account):
                continue
            accounts.append(account)
        current_account_id = _get_current_account_id(db)
        global_proxy = _get_proxy(proxy)
        # 卡片列表接口默认“缓存优先”，避免首次进入或新增卡片后触发全量远端请求造成页面卡死。
        # 需要强制刷新时统一走 /overview/refresh。
        allow_network = False
        if refresh:
            logger.info("overview/cards 接口忽略 refresh 参数，改由 /overview/refresh 执行远端刷新")

        rows = []
        db_updated = False

        for account in accounts:
            account_proxy = (account.proxy_used or "").strip() or global_proxy
            overview, updated = _get_account_overview_data(
                db,
                account,
                force_refresh=refresh,
                proxy=account_proxy,
                allow_network=allow_network,
            )
            db_updated = db_updated or updated

            overview_plan_raw = overview.get("plan_type")
            db_plan_raw = account.subscription_type
            has_db_subscription = bool(str(db_plan_raw or "").strip())
            # 与账号管理保持一致：卡片套餐优先使用 DB 的 subscription_type。
            effective_plan_raw = db_plan_raw if has_db_subscription else overview_plan_raw
            effective_plan_source = (
                "db.subscription_type"
                if has_db_subscription
                else (overview.get("plan_source") or "default")
            )
            if not _is_paid_subscription(effective_plan_raw):
                # Codex 账号管理仅允许 plus/team 账号进入。
                continue

            rows.append(
                {
                    "id": account.id,
                    "email": account.email,
                    "status": account.status,
                    "email_service": account.email_service,
                    "created_at": account.created_at.isoformat() if account.created_at else None,
                    "last_refresh": account.last_refresh.isoformat() if account.last_refresh else None,
                    "current": account.id == current_account_id,
                    "has_access_token": bool(account.access_token),
                    "plan_type": _normalize_plan_type(effective_plan_raw),
                    "plan_source": effective_plan_source,
                    "has_plus_or_team": _plan_to_subscription_type(effective_plan_raw) is not None,
                    "hourly_quota": overview.get("hourly_quota") or _build_unknown_quota(),
                    "weekly_quota": overview.get("weekly_quota") or _build_unknown_quota(),
                    "code_review_quota": overview.get("code_review_quota") or _build_unknown_quota(),
                    "overview_fetched_at": overview.get("fetched_at"),
                    "overview_stale": bool(overview.get("stale")),
                    "overview_error": overview.get("error"),
                }
            )

        if db_updated:
            db.commit()

        return {
            "total": len(rows),
            "current_account_id": current_account_id,
            "cache_ttl_seconds": OVERVIEW_CACHE_TTL_SECONDS,
            "network_mode": "refresh" if allow_network else "cache_only",
            "proxy": global_proxy or None,
            "accounts": rows,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }


@router.get("/overview/cards/addable")
async def list_accounts_overview_addable(
    search: Optional[str] = Query(None, description="按邮箱搜索"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
):
    """读取已从卡片删除的账号，用于“添加账号”里重新添加。"""
    with get_db() as db:
        query = db.query(Account)
        if search:
            pattern = f"%{search}%"
            query = query.filter((Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern)))
        if status:
            query = _apply_status_filter(query, status)
        if email_service:
            query = query.filter(Account.email_service == email_service)

        ordered_query = query.order_by(Account.created_at.desc())
        rows = []
        for account in _iter_query_in_batches(ordered_query, batch_size=200):
            if not _is_overview_card_removed(account):
                continue
            if not _is_paid_subscription(account.subscription_type):
                continue
            rows.append(
                {
                    "id": account.id,
                    "email": account.email,
                    "status": account.status,
                    "email_service": account.email_service,
                    "subscription_type": account.subscription_type or "free",
                    "has_access_token": bool(account.access_token),
                    "created_at": account.created_at.isoformat() if account.created_at else None,
                }
            )

        return {
            "total": len(rows),
            "accounts": rows,
        }


@router.get("/overview/cards/selectable")
async def list_accounts_overview_selectable(
    search: Optional[str] = Query(None, description="按邮箱搜索"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
):
    """读取账号管理中的可选账号，用于账号总览添加/重新添加。"""
    with get_db() as db:
        query = db.query(Account)
        if search:
            pattern = f"%{search}%"
            query = query.filter((Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern)))
        if status:
            query = _apply_status_filter(query, status)
        if email_service:
            query = query.filter(Account.email_service == email_service)

        ordered_query = query.order_by(Account.created_at.desc())
        rows = []
        for account in _iter_query_in_batches(ordered_query, batch_size=200):
            # 仅返回当前未在卡片中的账号（即已从卡片移除）
            if not _is_overview_card_removed(account):
                continue
            if not _is_paid_subscription(account.subscription_type):
                continue
            rows.append(
                {
                    "id": account.id,
                    "email": account.email,
                    "password": account.password or "",
                    "status": account.status,
                    "email_service": account.email_service,
                    "subscription_type": account.subscription_type or "free",
                    "client_id": account.client_id or "",
                    "account_id": account.account_id or "",
                    "workspace_id": account.workspace_id or "",
                    "has_access_token": bool(account.access_token),
                    "created_at": account.created_at.isoformat() if account.created_at else None,
                }
            )

        return {
            "total": len(rows),
            "accounts": rows,
        }


@router.post("/overview/cards/remove")
async def remove_accounts_overview_cards(request: OverviewCardDeleteRequest):
    """从账号总览卡片移除（软删除，不影响账号管理列表）。"""
    with get_db() as db:
        ids = resolve_account_ids(
            db,
            request.ids,
            request.select_all,
            request.status_filter,
            request.email_service_filter,
            request.search_filter,
        )
        removed_count = 0
        missing_ids = []
        for account_id in ids:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                missing_ids.append(account_id)
                continue
            if not _is_overview_card_removed(account):
                removed_count += 1
            _set_overview_card_removed(account, True)

        db.commit()
        return {
            "success": True,
            "removed_count": removed_count,
            "total": len(ids),
            "missing_ids": missing_ids,
        }


@router.post("/overview/cards/{account_id}/restore")
async def restore_accounts_overview_card(account_id: int):
    """恢复单个已删除的总览卡片。"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not _is_paid_subscription(account.subscription_type):
            raise HTTPException(status_code=400, detail="仅 plus/team 账号可进入 Codex 账号管理")

        _set_overview_card_removed(account, False)
        db.commit()
        return {"success": True, "id": account.id, "email": account.email}


@router.post("/overview/cards/{account_id}/attach")
async def attach_accounts_overview_card(account_id: int):
    """从账号管理选择账号附加到总览卡片（已存在时保持幂等）。"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not _is_paid_subscription(account.subscription_type):
            raise HTTPException(status_code=400, detail="仅 plus/team 账号可进入 Codex 账号管理")

        was_removed = _is_overview_card_removed(account)
        _set_overview_card_removed(account, False)
        db.commit()
        return {
            "success": True,
            "id": account.id,
            "email": account.email,
            "already_in_cards": not was_removed,
        }


@router.post("/overview/refresh")
def refresh_accounts_overview(request: OverviewRefreshRequest):
    """
    批量刷新账号总览数据。
    使用线程池并发执行，避免长时间刷新阻塞其他接口。
    """
    started_at = time.monotonic()
    proxy = _get_proxy(request.proxy)
    result = {"success_count": 0, "failed_count": 0, "details": []}

    with get_db() as db:
        ids = resolve_account_ids(
            db,
            request.ids,
            request.select_all,
            request.status_filter,
            request.email_service_filter,
            request.search_filter,
        )
        if not ids:
            # 默认仅刷新“卡片里可见的付费账号”，避免无关账号导致全量阻塞。
            candidates = db.query(Account).filter(
                func.lower(Account.subscription_type).in_(PAID_SUBSCRIPTION_TYPES)
            ).order_by(Account.created_at.desc()).all()
            ids = [acc.id for acc in candidates if not _is_overview_card_removed(acc)]

    logger.info(
        "账号总览刷新开始: target_count=%s force=%s select_all=%s proxy=%s",
        len(ids),
        bool(request.force),
        bool(request.select_all),
        proxy or "-",
    )

    if not ids:
        return result

    worker_count = min(ACCOUNT_OVERVIEW_REFRESH_MAX_WORKERS, max(1, len(ids)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="overview_refresh") as pool:
        future_map = {
            pool.submit(_refresh_overview_account_with_retry, account_id, bool(request.force), proxy): account_id
            for account_id in ids
        }
        for future in as_completed(future_map):
            account_id = future_map[future]
            try:
                detail = future.result()
            except Exception as exc:
                detail = {"id": account_id, "success": False, "error": str(exc)}

            result["details"].append(detail)
            if detail.get("success") is True:
                result["success_count"] += 1
            elif not detail.get("skipped"):
                result["failed_count"] += 1

            if detail.get("success") is True:
                logger.info(
                    "账号总览刷新成功: account_id=%s email=%s plan=%s",
                    detail.get("id"),
                    detail.get("email"),
                    detail.get("plan_type") or "-",
                )
            elif detail.get("skipped"):
                logger.info(
                    "账号总览刷新跳过: account_id=%s email=%s reason=%s",
                    detail.get("id"),
                    detail.get("email"),
                    detail.get("error"),
                )
            else:
                logger.warning(
                    "账号总览刷新失败: account_id=%s email=%s error=%s",
                    detail.get("id"),
                    detail.get("email"),
                    detail.get("error"),
                )

    result["details"].sort(key=lambda item: int(item.get("id") or 0))
    duration = round(time.monotonic() - started_at, 2)
    logger.info(
        "账号总览刷新完成: success=%s failed=%s total=%s workers=%s duration=%.2fs",
        result["success_count"],
        result["failed_count"],
        len(ids),
        worker_count,
        duration,
    )
    return result


@router.get("/current")
async def get_current_account():
    """获取当前已切换的账号"""
    with get_db() as db:
        current_id = _get_current_account_id(db)
        if not current_id:
            return {"current_account_id": None, "account": None}
        account = crud.get_account_by_id(db, current_id)
        if not account:
            return {"current_account_id": None, "account": None}
        return {
            "current_account_id": account.id,
            "account": {
                "id": account.id,
                "email": account.email,
                "status": account.status,
                "email_service": account.email_service,
                "plan_type": _normalize_plan_type(account.subscription_type),
            },
        }


@router.post("/{account_id}/switch")
async def switch_current_account(account_id: int):
    """
    一键切换当前账号。
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        _set_current_account_id(db, account_id)
        snapshot_path = _write_current_account_snapshot(account)

        return {
            "success": True,
            "current_account_id": account_id,
            "email": account.email,
            "snapshot_file": snapshot_path,
        }


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: int):
    """获取单个账号详情"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        return account_to_response(account)


@router.get("/{account_id}/tokens")
async def get_account_tokens(account_id: int):
    """获取账号的 Token 信息"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        resolved_session_token = _resolve_account_session_token(account)
        session_source = "db" if str(account.session_token or "").strip() else ("cookies" if resolved_session_token else "none")

        # 若 DB 为空但 cookies 可解析到 session_token，自动回写，避免后续重复解析。
        if resolved_session_token and not str(account.session_token or "").strip():
            account.session_token = resolved_session_token
            account.last_refresh = datetime.utcnow()
            db.commit()
            db.refresh(account)

        return {
            "id": account.id,
            "email": account.email,
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
            "session_token": resolved_session_token,
            "session_token_source": session_source,
            "device_id": _resolve_account_device_id(account),
            "has_tokens": bool(account.access_token and account.refresh_token),
        }


@router.patch("/{account_id}", response_model=AccountResponse)
async def update_account(account_id: int, request: AccountUpdateRequest, http_request: Request):
    """更新账号状态"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        actor = _resolve_actor(http_request)
        before_snapshot = {
            "status": account.status,
            "role_tag": account.role_tag,
            "account_label": account.account_label,
            "biz_tag": account.biz_tag,
            "pool_state_manual": account.pool_state_manual,
            "priority": account.priority,
            "subscription_type": account.subscription_type,
        }

        update_data = {}
        if request.status:
            if request.status not in [e.value for e in AccountStatus]:
                raise HTTPException(status_code=400, detail="无效的状态值")
            update_data["status"] = request.status

        if request.metadata:
            current_metadata = account.extra_data if isinstance(account.extra_data, dict) else {}
            current_metadata.update(request.metadata)
            update_data["extra_data"] = current_metadata

        if request.cookies is not None:
            # 留空则清空，非空则更新
            update_data["cookies"] = request.cookies or None

        if request.session_token is not None:
            # 留空则清空，非空则更新
            update_data["session_token"] = request.session_token or None
            update_data["last_refresh"] = datetime.utcnow()

        if request.role_tag is not None:
            normalized_role = normalize_role_tag(request.role_tag)
            update_data["role_tag"] = normalized_role
            update_data["account_label"] = role_tag_to_account_label(normalized_role)

        if request.biz_tag is not None:
            update_data["biz_tag"] = str(request.biz_tag).strip() or None

        if request.pool_state_manual is not None:
            text = str(request.pool_state_manual or "").strip()
            update_data["pool_state_manual"] = normalize_pool_state(text) if text else None

        if request.priority is not None:
            try:
                update_data["priority"] = max(0, int(request.priority))
            except Exception:
                raise HTTPException(status_code=400, detail="priority 必须为整数")

        account = crud.update_account(db, account_id, **update_data)
        if update_data:
            after_snapshot = {
                "status": account.status,
                "role_tag": account.role_tag,
                "account_label": account.account_label,
                "biz_tag": account.biz_tag,
                "pool_state_manual": account.pool_state_manual,
                "priority": account.priority,
                "subscription_type": account.subscription_type,
            }
            _audit_account_action(
                db,
                actor=actor,
                action="account.update",
                account=account,
                payload={
                    "fields": sorted(list(update_data.keys())),
                    "before": before_snapshot,
                    "after": after_snapshot,
                },
            )
        return account_to_response(account)


@router.get("/{account_id}/cookies")
async def get_account_cookies(account_id: int):
    """获取账号的 cookie 字符串（仅供支付使用）"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        return {"account_id": account_id, "cookies": account.cookies or ""}


@router.delete("/{account_id}")
async def delete_account(account_id: int):
    """删除单个账号"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        try:
            crud.delete_account(db, account_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")
        return {"success": True, "message": f"账号 {account.email} 已删除"}


@router.post("/batch-delete")
async def batch_delete_accounts(request: BatchDeleteRequest, http_request: Request):
    """批量删除账号"""
    with get_db() as db:
        actor = _resolve_actor(http_request)
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        deleted_count = 0
        errors = []
        deleted_ids: List[int] = []

        for account_id in ids:
            try:
                account = crud.get_account_by_id(db, account_id)
                if account:
                    crud.delete_account(db, account_id)
                    deleted_count += 1
                    deleted_ids.append(int(account_id))
            except Exception as e:
                errors.append(f"ID {account_id}: {str(e)}")

        _audit_account_action(
            db,
            actor=actor,
            action="account.batch_delete",
            target_id=0,
            target_email=None,
            payload={
                "requested_ids": [int(item) for item in ids],
                "deleted_ids": deleted_ids,
                "deleted_count": deleted_count,
                "error_count": len(errors),
                "errors": errors[:50],
            },
        )

        return {
            "success": True,
            "deleted_count": deleted_count,
            "errors": errors if errors else None
        }


@router.post("/batch-update")
async def batch_update_accounts(request: BatchUpdateRequest, http_request: Request):
    """批量更新账号状态"""
    if request.status not in [e.value for e in AccountStatus]:
        raise HTTPException(status_code=400, detail="无效的状态值")

    with get_db() as db:
        actor = _resolve_actor(http_request)
        updated_count = 0
        errors = []
        updated_ids: List[int] = []

        for account_id in request.ids:
            try:
                account = crud.get_account_by_id(db, account_id)
                if account:
                    crud.update_account(db, account_id, status=request.status)
                    updated_count += 1
                    updated_ids.append(int(account_id))
            except Exception as e:
                errors.append(f"ID {account_id}: {str(e)}")

        _audit_account_action(
            db,
            actor=actor,
            action="account.batch_update_status",
            target_id=0,
            target_email=None,
            payload={
                "status": request.status,
                "requested_ids": [int(item) for item in request.ids],
                "updated_ids": updated_ids,
                "updated_count": updated_count,
                "error_count": len(errors),
                "errors": errors[:50],
            },
        )

        return {
            "success": True,
            "updated_count": updated_count,
            "errors": errors if errors else None
        }


class BatchExportRequest(BaseModel):
    """批量导出请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


@router.post("/export/json")
async def export_accounts_json(request: BatchExportRequest):
    """导出账号为 JSON 格式"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        export_data = []
        for acc in accounts:
            export_data.append({
                "email": acc.email,
                "password": acc.password,
                "client_id": acc.client_id,
                "account_id": acc.account_id,
                "workspace_id": acc.workspace_id,
                "access_token": acc.access_token,
                "refresh_token": acc.refresh_token,
                "id_token": acc.id_token,
                "session_token": acc.session_token,
                "email_service": acc.email_service,
                "account_label": normalize_account_label(getattr(acc, "account_label", None)),
                "role_tag": _resolve_account_role_tag(acc),
                "biz_tag": str(getattr(acc, "biz_tag", "") or "").strip() or None,
                "pool_state": _resolve_account_pool_state(acc),
                "priority": int(getattr(acc, "priority", 50) or 50),
                "registered_at": acc.registered_at.isoformat() if acc.registered_at else None,
                "last_refresh": acc.last_refresh.isoformat() if acc.last_refresh else None,
                "expires_at": acc.expires_at.isoformat() if acc.expires_at else None,
                "status": acc.status,
            })

        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accounts_{timestamp}.json"

        # 返回 JSON 响应
        content = json.dumps(export_data, ensure_ascii=False, indent=2)

        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/csv")
async def export_accounts_csv(request: BatchExportRequest):
    """导出账号为 CSV 格式"""
    import csv
    import io

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        # 创建 CSV 内容
        output = io.StringIO()
        writer = csv.writer(output)

        # 写入表头
        writer.writerow([
            "ID", "Email", "Password", "Client ID",
            "Account ID", "Workspace ID",
            "Access Token", "Refresh Token", "ID Token", "Session Token",
            "Email Service", "Account Label", "Role Tag", "Biz Tag", "Pool State", "Priority",
            "Status", "Registered At", "Last Refresh", "Expires At"
        ])

        # 写入数据
        for acc in accounts:
            writer.writerow([
                acc.id,
                acc.email,
                acc.password or "",
                acc.client_id or "",
                acc.account_id or "",
                acc.workspace_id or "",
                acc.access_token or "",
                acc.refresh_token or "",
                acc.id_token or "",
                acc.session_token or "",
                acc.email_service,
                normalize_account_label(getattr(acc, "account_label", None)),
                _resolve_account_role_tag(acc),
                str(getattr(acc, "biz_tag", "") or "").strip(),
                _resolve_account_pool_state(acc),
                int(getattr(acc, "priority", 50) or 50),
                acc.status,
                acc.registered_at.isoformat() if acc.registered_at else "",
                acc.last_refresh.isoformat() if acc.last_refresh else "",
                acc.expires_at.isoformat() if acc.expires_at else ""
            ])

        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accounts_{timestamp}.csv"

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/sub2api")
async def export_accounts_sub2api(request: BatchExportRequest):
    """导出账号为 Sub2Api 格式（所有选中账号合并到一个 JSON 的 accounts 数组中）"""

    def make_account_entry(acc) -> dict:
        expires_at = int(acc.expires_at.timestamp()) if acc.expires_at else 0
        return {
            "name": acc.email,
            "platform": "openai",
            "type": "oauth",
            "credentials": {
                "access_token": acc.access_token or "",
                "chatgpt_account_id": acc.account_id or "",
                "chatgpt_user_id": "",
                "client_id": acc.client_id or "",
                "expires_at": expires_at,
                "expires_in": 863999,
                "model_mapping": {
                    "gpt-5.1": "gpt-5.1",
                    "gpt-5.1-codex": "gpt-5.1-codex",
                    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
                    "gpt-5.2": "gpt-5.2",
                    "gpt-5.2-codex": "gpt-5.2-codex",
                    "gpt-5.3": "gpt-5.3",
                    "gpt-5.3-codex": "gpt-5.3-codex",
                    "gpt-5.4": "gpt-5.4"
                },
                "organization_id": acc.workspace_id or "",
                "refresh_token": acc.refresh_token or ""
            },
            "extra": {},
            "concurrency": 10,
            "priority": 1,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True
        }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = {
            "proxies": [],
            "accounts": [make_account_entry(acc) for acc in accounts]
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        if len(accounts) == 1:
            filename = f"{accounts[0].email}_sub2api.json"
        else:
            filename = f"sub2api_tokens_{timestamp}.json"

        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/codex")
async def export_accounts_codex(request: BatchExportRequest):
    """导出账号为 Codex JSONL 格式（便于迁移/导入）。"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        lines = []
        for acc in accounts:
            lines.append(json.dumps({
                "email": acc.email,
                "password": acc.password or "",
                "client_id": acc.client_id or "",
                "access_token": acc.access_token or "",
                "refresh_token": acc.refresh_token or "",
                "session_token": acc.session_token or "",
                "account_id": acc.account_id or "",
                "workspace_id": acc.workspace_id or "",
                "cookies": acc.cookies or "",
                "type": "codex",
                "source": getattr(acc, "source", None) or "manual",
            }, ensure_ascii=False))

        content = "\n".join(lines)
        filename = f"codex_accounts_{timestamp}.jsonl"
        return StreamingResponse(
            iter([content]),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/cpa")
async def export_accounts_cpa(request: BatchExportRequest):
    """导出账号为 CPA Token JSON 格式（每个账号单独一个 JSON 文件，打包为 ZIP）"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if len(accounts) == 1:
            # 单个账号直接返回 JSON 文件
            acc = accounts[0]
            token_data = generate_token_json(acc)
            content = json.dumps(token_data, ensure_ascii=False, indent=2)
            filename = f"{acc.email}.json"
            return StreamingResponse(
                iter([content]),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

        # 多个账号打包为 ZIP
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for acc in accounts:
                token_data = generate_token_json(acc)
                content = json.dumps(token_data, ensure_ascii=False, indent=2)
                zf.writestr(f"{acc.email}.json", content)

        zip_buffer.seek(0)
        zip_filename = f"cpa_tokens_{timestamp}.zip"
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={zip_filename}"}
        )


@router.get("/stats/summary")
async def get_accounts_stats():
    """获取账号统计信息"""
    with get_db() as db:
        from sqlalchemy import func

        # 总数
        total = db.query(func.count(Account.id)).scalar()

        # 按状态统计
        status_stats = db.query(
            Account.status,
            func.count(Account.id)
        ).group_by(Account.status).all()

        # 按邮箱服务统计
        service_stats = db.query(
            Account.email_service,
            func.count(Account.id)
        ).group_by(Account.email_service).all()

        # 按角色标签统计（Service + Repository 聚合）
        role_counts = _service_get_role_tag_counts(db)

        return {
            "total": total,
            "by_status": {status: count for status, count in status_stats},
            "by_email_service": {service: count for service, count in service_stats},
            "by_role_tag": role_counts,
            "tagged_role_counts": {
                "parent": role_counts["parent"],
                "child": role_counts["child"],
                "total_labeled": role_counts["parent"] + role_counts["child"],
            },
        }


@router.get("/stats/overview")
async def get_accounts_overview():
    """获取账号总览统计信息（用于总览页面）"""
    with get_db() as db:
        total = db.query(func.count(Account.id)).scalar() or 0
        active_count = db.query(func.count(Account.id)).filter(
            Account.status == AccountStatus.ACTIVE.value
        ).scalar() or 0

        with_access_token = db.query(func.count(Account.id)).filter(
            Account.access_token.isnot(None),
            Account.access_token != "",
        ).scalar() or 0
        with_refresh_token = db.query(func.count(Account.id)).filter(
            Account.refresh_token.isnot(None),
            Account.refresh_token != "",
        ).scalar() or 0
        without_access_token = max(total - with_access_token, 0)

        cpa_uploaded_count = db.query(func.count(Account.id)).filter(
            Account.cpa_uploaded.is_(True)
        ).scalar() or 0

        status_stats = db.query(
            Account.status,
            func.count(Account.id),
        ).group_by(Account.status).all()

        service_stats = db.query(
            Account.email_service,
            func.count(Account.id),
        ).group_by(Account.email_service).all()

        source_stats = db.query(
            Account.source,
            func.count(Account.id),
        ).group_by(Account.source).all()

        subscription_stats = db.query(
            Account.subscription_type,
            func.count(Account.id),
        ).group_by(Account.subscription_type).all()

        recent_accounts = db.query(Account).order_by(Account.created_at.desc()).limit(10).all()

        return {
            "total": total,
            "active_count": active_count,
            "token_stats": {
                "with_access_token": with_access_token,
                "with_refresh_token": with_refresh_token,
                "without_access_token": without_access_token,
            },
            "cpa_uploaded_count": cpa_uploaded_count,
            "by_status": {status or "unknown": count for status, count in status_stats},
            "by_email_service": {service or "unknown": count for service, count in service_stats},
            "by_source": {source or "unknown": count for source, count in source_stats},
            "by_subscription": {
                (subscription or "free"): count for subscription, count in subscription_stats
            },
            "recent_accounts": [
                {
                    "id": acc.id,
                    "email": acc.email,
                    "status": acc.status,
                    "email_service": acc.email_service,
                    "source": acc.source,
                    "subscription_type": acc.subscription_type or "free",
                    "created_at": acc.created_at.isoformat() if acc.created_at else None,
                    "last_refresh": acc.last_refresh.isoformat() if acc.last_refresh else None,
                }
                for acc in recent_accounts
            ],
        }


@router.get("/audit-logs")
async def list_account_audit_logs(limit: int = Query(100, ge=1, le=500), action: Optional[str] = Query(None)):
    with get_db() as db:
        rows = crud.list_operation_audit_logs(
            db,
            limit=limit,
            action=action,
            target_type="account",
        )
    return {
        "success": True,
        "items": [row.to_dict() for row in rows],
    }


# ============== Token 刷新相关 ==============

class TokenRefreshRequest(BaseModel):
    """Token 刷新请求"""
    proxy: Optional[str] = None


class BatchRefreshRequest(BaseModel):
    """批量刷新请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


class TokenValidateRequest(BaseModel):
    """Token 验证请求"""
    proxy: Optional[str] = None


class BatchValidateRequest(BaseModel):
    """批量验证请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


def _wait_account_async_task_finished(
    task_id: str,
    timeout_seconds: int = QUICK_REFRESH_TASK_WAIT_TIMEOUT_SECONDS,
    poll_interval: float = QUICK_REFRESH_TASK_POLL_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    started_at = time.monotonic()
    while time.monotonic() - started_at < timeout_seconds:
        task = _get_account_async_task(task_id)
        if task:
            snapshot = _build_account_async_task_snapshot(task)
            status = str(snapshot.get("status") or "").lower()
            if status in {"completed", "failed", "cancelled"}:
                return snapshot
        time.sleep(max(0.2, float(poll_interval)))
    raise TimeoutError(f"等待账号任务超时: {task_id}")


def _wait_payment_op_task_finished(
    op_task_id: str,
    timeout_seconds: int = QUICK_REFRESH_TASK_WAIT_TIMEOUT_SECONDS,
    poll_interval: float = QUICK_REFRESH_TASK_POLL_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    from . import payment as payment_routes

    started_at = time.monotonic()
    while time.monotonic() - started_at < timeout_seconds:
        task = payment_routes._get_payment_op_task(op_task_id)
        if task:
            snapshot = payment_routes._build_payment_op_task_snapshot(task)
            status = str(snapshot.get("status") or "").lower()
            if status in {"completed", "failed", "cancelled"}:
                return snapshot
        time.sleep(max(0.2, float(poll_interval)))
    raise TimeoutError(f"等待支付任务超时: {op_task_id}")


def _task_terminal_error(task_snapshot: Dict[str, Any], default_message: str) -> str:
    status = str(task_snapshot.get("status") or "").lower()
    if status == "completed":
        return ""
    if status == "cancelled":
        return str(task_snapshot.get("message") or "任务已取消")
    return str(task_snapshot.get("error") or task_snapshot.get("message") or default_message)


def has_active_batch_operations() -> bool:
    active_status = {"pending", "running"}
    account_task_types = {"batch_refresh", "batch_validate", "overview_refresh", "quick_refresh"}
    with _account_async_tasks_lock:
        for task in _account_async_tasks.values():
            status = str(task.get("status") or "").lower()
            task_type = str(task.get("task_type") or "").strip().lower()
            if status in active_status and task_type in account_task_types:
                return True

    try:
        from . import payment as payment_routes

        with payment_routes._PAYMENT_OP_TASK_LOCK:
            for task in payment_routes._PAYMENT_OP_TASKS.values():
                status = str(task.get("status") or "").lower()
                task_type = str(task.get("task_type") or "").strip().lower()
                if status in active_status and task_type in {"batch_check_subscription", "quick_refresh"}:
                    return True
    except Exception:
        pass

    return False


def _compact_refresh_result(result: Dict[str, Any]) -> Dict[str, int]:
    return {
        "success_count": int(result.get("success_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "total": int(result.get("total") or 0),
    }


def _compact_validate_result(result: Dict[str, Any]) -> Dict[str, int]:
    return {
        "valid_count": int(result.get("valid_count") or 0),
        "invalid_count": int(result.get("invalid_count") or 0),
        "total": int(result.get("total") or 0),
    }


def run_quick_refresh_workflow(
    *,
    source: str = "manual",
    proxy: Optional[str] = None,
    select_all: bool = True,
    status_filter: Optional[str] = None,
    email_service_filter: Optional[str] = None,
    search_filter: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "ids": [],
        "proxy": proxy,
        "select_all": bool(select_all),
        "status_filter": status_filter,
        "email_service_filter": email_service_filter,
        "search_filter": search_filter,
    }

    # 1) 批量验证
    validate_task = start_batch_validate_async(BatchValidateRequest(**payload))
    validate_task_id = str(validate_task.get("id") or "")
    if not validate_task_id:
        raise RuntimeError("创建批量验证任务失败：缺少 task_id")
    validate_final = _wait_account_async_task_finished(validate_task_id)
    validate_error = _task_terminal_error(validate_final, "批量验证任务失败")
    if validate_error:
        raise RuntimeError(f"批量验证失败: {validate_error}")
    validate_result = _compact_validate_result(validate_final.get("result") or {})

    # 2) 批量检测订阅
    from . import payment as payment_routes

    subscription_task = payment_routes.start_batch_check_subscription_async(
        payment_routes.BatchCheckSubscriptionRequest(**payload)
    )
    subscription_task_id = str(subscription_task.get("id") or "")
    if not subscription_task_id:
        raise RuntimeError("创建批量订阅检测任务失败：缺少 op_task_id")
    subscription_final = _wait_payment_op_task_finished(subscription_task_id)
    subscription_error = _task_terminal_error(subscription_final, "批量订阅检测任务失败")
    if subscription_error:
        raise RuntimeError(f"批量检测订阅失败: {subscription_error}")
    subscription_raw = subscription_final.get("result") or {}
    subscription_result = {
        "success_count": int(subscription_raw.get("success_count") or 0),
        "failed_count": int(subscription_raw.get("failed_count") or 0),
        "total": int(subscription_raw.get("total") or 0),
    }

    return {
        "source": source,
        "finished_at": _utc_now_iso(),
        "validate": validate_result,
        "subscription": subscription_result,
    }


def _is_retryable_refresh_error(error_message: Optional[str]) -> bool:
    text = str(error_message or "").strip().lower()
    if not text:
        return False
    retry_markers = (
        "network_error",
        "network",
        "timeout",
        "timed out",
        "connection",
        "temporarily",
        "too many requests",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    return any(marker in text for marker in retry_markers)


def _refresh_one_account_with_retry(
    account_id: int,
    proxy: Optional[str],
    max_attempts: int = ACCOUNT_BATCH_REFRESH_RETRY_ATTEMPTS,
) -> Dict[str, Any]:
    attempts = max(1, int(max_attempts or 1))
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            refresh_result = do_refresh(account_id, proxy)
        except Exception as exc:
            refresh_result = None
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(ACCOUNT_BATCH_REFRESH_RETRY_BASE_DELAY_SECONDS * attempt)
                continue
            return {
                "id": account_id,
                "success": False,
                "error": last_error,
                "attempts": attempt,
            }

        if refresh_result and refresh_result.success:
            return {
                "id": account_id,
                "success": True,
                "attempts": attempt,
            }

        last_error = str(getattr(refresh_result, "error_message", "") or "刷新失败")
        can_retry = attempt < attempts and _is_retryable_refresh_error(last_error)
        if can_retry:
            time.sleep(ACCOUNT_BATCH_REFRESH_RETRY_BASE_DELAY_SECONDS * attempt)
            continue
        return {
            "id": account_id,
            "success": False,
            "error": last_error,
            "attempts": attempt,
        }

    return {
        "id": account_id,
        "success": False,
        "error": last_error or "刷新失败",
        "attempts": attempts,
    }


def _run_batch_refresh_task(task_id: str, ids: List[int], proxy: Optional[str]):
    total = len(ids)
    success_count = 0
    failed_count = 0
    completed_count = 0

    _update_account_async_task(
        task_id,
        status="running",
        started_at=_utc_now_iso(),
        message=f"开始刷新 Token，共 {total} 个账号",
        paused=False,
    )

    if total <= 0:
        _update_account_async_task(
            task_id,
            status="completed",
            finished_at=_utc_now_iso(),
            message="没有可刷新的账号",
            paused=False,
            result={
                "success_count": 0,
                "failed_count": 0,
                "total": 0,
                "cancelled": False,
            },
        )
        return

    worker_count = min(ACCOUNT_BATCH_REFRESH_ASYNC_MAX_WORKERS, max(1, total))
    _update_account_async_task(task_id, message=f"处理中 0/{total}（并发 {worker_count}）")

    next_index = 0
    running: Dict[Any, int] = {}
    cancelled = False
    pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch_refresh_async")
    try:
        while completed_count < total:
            if not _wait_if_account_async_task_paused(
                task_id,
                f"处理中 {completed_count}/{total}（并发 {worker_count}）",
            ):
                cancelled = True
                break
            if _is_account_async_task_cancel_requested(task_id):
                cancelled = True
                break

            while next_index < total and len(running) < worker_count:
                if not _wait_if_account_async_task_paused(
                    task_id,
                    f"处理中 {completed_count}/{total}（并发 {worker_count}）",
                ):
                    cancelled = True
                    break
                account_id = int(ids[next_index])
                next_index += 1
                future = pool.submit(_refresh_one_account_with_retry, account_id, proxy)
                running[future] = account_id

            if cancelled:
                break
            if not running:
                continue

            done, _ = wait(tuple(running.keys()), timeout=0.6, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                account_id = int(running.pop(future, 0) or 0)
                if account_id <= 0:
                    continue
                try:
                    detail = future.result()
                except Exception as exc:
                    detail = {
                        "id": account_id,
                        "success": False,
                        "error": str(exc),
                        "attempts": 1,
                    }

                completed_count += 1
                if detail.get("success"):
                    success_count += 1
                else:
                    failed_count += 1

                _append_account_async_task_detail(task_id, detail)
                _set_account_async_task_progress(
                    task_id,
                    total=total,
                    completed=completed_count,
                    success=success_count,
                    failed=failed_count,
                )
                _update_account_async_task(
                    task_id,
                    message=f"处理中 {completed_count}/{total}（并发 {worker_count}）",
                )

        if cancelled:
            for future in list(running.keys()):
                future.cancel()
            _update_account_async_task(
                task_id,
                status="cancelled",
                finished_at=_utc_now_iso(),
                message=f"任务已取消，进度 {completed_count}/{total}",
                paused=False,
                result={
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "total": total,
                    "cancelled": True,
                },
            )
            return
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    _update_account_async_task(
        task_id,
        status="completed",
        finished_at=_utc_now_iso(),
        message=f"刷新完成：成功 {success_count}，失败 {failed_count}",
        paused=False,
        result={
            "success_count": success_count,
            "failed_count": failed_count,
            "total": total,
            "cancelled": False,
        },
    )


def _is_retryable_validate_error(error_message: Optional[str]) -> bool:
    text = str(error_message or "").strip().lower()
    if not text:
        return False
    retry_markers = (
        "network_error",
        "network",
        "timeout",
        "timed out",
        "connection",
        "temporarily",
        "too many requests",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "rate limit",
    )
    return any(marker in text for marker in retry_markers)


def _calculate_validate_worker_count(total: int, *, async_mode: bool) -> int:
    """计算批量验证并发数：避免过低并发导致慢，也避免线程过多造成抖动。"""
    safe_total = max(0, int(total or 0))
    if safe_total <= 0:
        return 1
    max_workers = ACCOUNT_BATCH_VALIDATE_ASYNC_MAX_WORKERS if async_mode else ACCOUNT_BATCH_VALIDATE_SYNC_MAX_WORKERS
    if safe_total <= 3:
        return safe_total
    if safe_total <= 10:
        return min(max_workers, max(4, safe_total))
    if safe_total <= 40:
        return min(max_workers, max(6, safe_total // 2))
    return min(max_workers, max(8, safe_total // 3))


def _derive_account_status_from_validate_result(is_valid: bool, error: Optional[str]) -> str:
    """
    将 Token 验证结果映射为账号状态。
    这里与 token_refresh.validate_account_token 的回写规则保持一致，
    便于异步任务在进度明细里即时返回最终状态。
    """
    if bool(is_valid):
        return AccountStatus.ACTIVE.value

    error_text = str(error or "").lower()
    if (
        "402" in error_text
        or "payment required" in error_text
        or "订阅受限" in error_text
    ):
        return AccountStatus.EXPIRED.value
    if (
        "401" in error_text
        or "invalid" in error_text
        or "unauthorized" in error_text
        or "过期" in error_text
        or "expired" in error_text
    ):
        return AccountStatus.FAILED.value
    if (
        "封禁" in error_text
        or "banned" in error_text
        or "forbidden" in error_text
    ):
        return AccountStatus.BANNED.value
    return AccountStatus.FAILED.value


def _validate_one_account_with_retry(
    account_id: int,
    proxy: Optional[str],
    max_attempts: int = ACCOUNT_BATCH_VALIDATE_RETRY_ATTEMPTS,
    timeout_seconds: int = ACCOUNT_BATCH_VALIDATE_HTTP_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    attempts = max(1, int(max_attempts or 1))
    timeout_seconds = max(5, int(timeout_seconds or ACCOUNT_BATCH_VALIDATE_HTTP_TIMEOUT_SECONDS))
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            is_valid, error = do_validate(account_id, proxy, timeout_seconds=timeout_seconds)
            if is_valid:
                return {
                    "id": account_id,
                    "valid": True,
                    "status": _derive_account_status_from_validate_result(True, error),
                    "error": None,
                    "attempts": attempt,
                }
            last_error = str(error or "token_invalid")
            can_retry = attempt < attempts and _is_retryable_validate_error(last_error)
            if can_retry:
                time.sleep(ACCOUNT_BATCH_VALIDATE_RETRY_BASE_DELAY_SECONDS * attempt)
                continue
            return {
                "id": account_id,
                "valid": False,
                "status": _derive_account_status_from_validate_result(False, last_error),
                "error": last_error,
                "attempts": attempt,
            }
        except Exception as exc:
            last_error = str(exc)
            can_retry = attempt < attempts and _is_retryable_validate_error(last_error)
            if can_retry:
                time.sleep(ACCOUNT_BATCH_VALIDATE_RETRY_BASE_DELAY_SECONDS * attempt)
                continue
            return {
                "id": account_id,
                "valid": False,
                "status": _derive_account_status_from_validate_result(False, last_error),
                "error": last_error,
                "attempts": attempt,
            }

    return {
        "id": account_id,
        "valid": False,
        "status": _derive_account_status_from_validate_result(False, last_error or "validation_failed"),
        "error": last_error or "validation_failed",
        "attempts": attempts,
    }


def _run_batch_validate_task(task_id: str, ids: List[int], proxy: Optional[str]):
    started_perf = time.perf_counter()
    total = len(ids)
    valid_count = 0
    invalid_count = 0
    completed_count = 0
    retry_count = 0

    _update_account_async_task(
        task_id,
        status="running",
        started_at=_utc_now_iso(),
        message=f"开始验证 Token，共 {total} 个账号",
        paused=False,
    )

    if total <= 0:
        _update_account_async_task(
            task_id,
            status="completed",
            finished_at=_utc_now_iso(),
            message="没有可验证的账号",
            paused=False,
            result={
                "valid_count": 0,
                "invalid_count": 0,
                "total": 0,
                "cancelled": False,
            },
        )
        return

    worker_count = _calculate_validate_worker_count(total, async_mode=True)
    _update_account_async_task(task_id, message=f"处理中 0/{total}（并发 {worker_count}）")

    next_index = 0
    running: Dict[Any, int] = {}
    cancelled = False
    pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch_validate_async")
    try:
        while completed_count < total:
            if not _wait_if_account_async_task_paused(
                task_id,
                f"处理中 {completed_count}/{total}（并发 {worker_count}）",
            ):
                cancelled = True
                break
            if _is_account_async_task_cancel_requested(task_id):
                cancelled = True
                break

            while next_index < total and len(running) < worker_count:
                if not _wait_if_account_async_task_paused(
                    task_id,
                    f"处理中 {completed_count}/{total}（并发 {worker_count}）",
                ):
                    cancelled = True
                    break
                account_id = int(ids[next_index])
                next_index += 1
                future = pool.submit(_validate_one_account_with_retry, account_id, proxy)
                running[future] = account_id

            if cancelled:
                break
            if not running:
                continue

            done, _ = wait(tuple(running.keys()), timeout=0.6, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                account_id = int(running.pop(future, 0) or 0)
                if account_id <= 0:
                    continue
                try:
                    detail = future.result()
                except Exception as exc:
                    detail = {
                        "id": account_id,
                        "valid": False,
                        "error": str(exc),
                        "attempts": 1,
                    }

                completed_count += 1
                if detail.get("valid"):
                    valid_count += 1
                else:
                    invalid_count += 1
                attempts = int(detail.get("attempts") or 1)
                retry_count += max(0, attempts - 1)

                _append_account_async_task_detail(task_id, detail)
                _set_account_async_task_progress(
                    task_id,
                    total=total,
                    completed=completed_count,
                    success=valid_count,
                    failed=invalid_count,
                )
                _update_account_async_task(
                    task_id,
                    message=f"处理中 {completed_count}/{total}（并发 {worker_count}）",
                )

                if detail.get("valid"):
                    logger.info(
                        "批量验证结果: task_id=%s account_id=%s valid=true attempts=%s",
                        task_id,
                        account_id,
                        detail.get("attempts"),
                    )
                else:
                    logger.warning(
                        "批量验证结果: task_id=%s account_id=%s valid=false attempts=%s error=%s",
                        task_id,
                        account_id,
                        detail.get("attempts"),
                        str(detail.get("error") or "")[:220] or "-",
                    )

        if cancelled:
            for future in list(running.keys()):
                future.cancel()
            _update_account_async_task(
                task_id,
                status="cancelled",
                finished_at=_utc_now_iso(),
                message=f"任务已取消，进度 {completed_count}/{total}",
                paused=False,
                result={
                    "valid_count": valid_count,
                    "invalid_count": invalid_count,
                    "total": total,
                    "cancelled": True,
                    "worker_count": worker_count,
                    "retry_count": retry_count,
                    "duration_ms": int((time.perf_counter() - started_perf) * 1000),
                },
            )
            return
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    _update_account_async_task(
        task_id,
        status="completed",
        finished_at=_utc_now_iso(),
        message=f"验证完成：有效 {valid_count}，无效 {invalid_count}",
        paused=False,
        result={
            "valid_count": valid_count,
            "invalid_count": invalid_count,
            "total": total,
            "cancelled": False,
            "worker_count": worker_count,
            "retry_count": retry_count,
            "duration_ms": int((time.perf_counter() - started_perf) * 1000),
        },
    )
    logger.info(
        "批量验证完成: task_id=%s total=%s valid=%s invalid=%s proxy=%s",
        task_id,
        total,
        valid_count,
        invalid_count,
        proxy or "-",
    )


def _is_retryable_overview_refresh_error(error_message: Optional[str]) -> bool:
    text = str(error_message or "").strip().lower()
    if not text:
        return False
    retry_markers = (
        "network_error",
        "network",
        "timeout",
        "timed out",
        "connection",
        "temporarily",
        "too many requests",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "rate limit",
    )
    return any(marker in text for marker in retry_markers)


def _refresh_overview_account_once(account_id: int, force_refresh: bool, proxy: Optional[str]) -> Dict[str, Any]:
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return {"id": account_id, "success": False, "error": "账号不存在"}

        if (not _is_paid_subscription(account.subscription_type)) or _is_overview_card_removed(account):
            return {
                "id": account.id,
                "email": account.email,
                "success": False,
                "skipped": True,
                "error": "账号不在 Codex 卡片范围内，已跳过",
            }

        account_proxy = (account.proxy_used or "").strip() or proxy
        overview, updated = _get_account_overview_data(
            db,
            account,
            force_refresh=force_refresh,
            proxy=account_proxy,
            allow_network=True,
        )
        if updated:
            db.commit()

        hourly_unknown = overview.get("hourly_quota", {}).get("status") == "unknown"
        weekly_unknown = overview.get("weekly_quota", {}).get("status") == "unknown"
        if hourly_unknown and weekly_unknown:
            return {
                "id": account.id,
                "email": account.email,
                "success": False,
                "error": overview.get("error") or "未获取到配额数据",
            }

        return {
            "id": account.id,
            "email": account.email,
            "success": True,
            "plan_type": overview.get("plan_type"),
        }


def _refresh_overview_account_with_retry(
    account_id: int,
    force_refresh: bool,
    proxy: Optional[str],
    max_attempts: int = ACCOUNT_OVERVIEW_REFRESH_RETRY_ATTEMPTS,
) -> Dict[str, Any]:
    attempts = max(1, int(max_attempts or 1))
    last_detail: Dict[str, Any] = {"id": int(account_id), "success": False, "error": "刷新失败"}

    for attempt in range(1, attempts + 1):
        try:
            detail = _refresh_overview_account_once(account_id, force_refresh, proxy)
        except Exception as exc:
            detail = {"id": int(account_id), "success": False, "error": str(exc)}

        detail["attempts"] = attempt
        if detail.get("success") or detail.get("skipped"):
            return detail

        last_detail = detail
        error_text = str(detail.get("error") or "")
        can_retry = attempt < attempts and _is_retryable_overview_refresh_error(error_text)
        if can_retry:
            time.sleep(ACCOUNT_OVERVIEW_REFRESH_RETRY_BASE_DELAY_SECONDS * attempt)
            continue
        return detail

    return last_detail


def _run_overview_refresh_async_task(task_id: str, ids: List[int], force_refresh: bool, proxy: Optional[str]):
    total = len(ids)
    success_count = 0
    failed_count = 0
    completed_count = 0

    _update_account_async_task(
        task_id,
        status="running",
        started_at=_utc_now_iso(),
        message=f"开始刷新账号总览，共 {total} 个账号",
        paused=False,
    )

    if total <= 0:
        _update_account_async_task(
            task_id,
            status="completed",
            finished_at=_utc_now_iso(),
            message="没有可刷新的总览账号",
            paused=False,
            result={
                "success_count": 0,
                "failed_count": 0,
                "total": 0,
                "cancelled": False,
            },
        )
        return

    worker_count = min(ACCOUNT_OVERVIEW_REFRESH_MAX_WORKERS, max(1, total))
    _update_account_async_task(task_id, message=f"处理中 0/{total}（并发 {worker_count}）")

    next_index = 0
    running: Dict[Any, int] = {}
    cancelled = False
    pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="overview_refresh_async")
    try:
        while completed_count < total:
            if not _wait_if_account_async_task_paused(
                task_id,
                f"处理中 {completed_count}/{total}（并发 {worker_count}）",
            ):
                cancelled = True
                break
            if _is_account_async_task_cancel_requested(task_id):
                cancelled = True
                break

            while next_index < total and len(running) < worker_count:
                if not _wait_if_account_async_task_paused(
                    task_id,
                    f"处理中 {completed_count}/{total}（并发 {worker_count}）",
                ):
                    cancelled = True
                    break
                account_id = int(ids[next_index])
                next_index += 1
                future = pool.submit(_refresh_overview_account_with_retry, account_id, force_refresh, proxy)
                running[future] = account_id

            if cancelled:
                break
            if not running:
                continue

            done, _ = wait(tuple(running.keys()), timeout=0.6, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                account_id = int(running.pop(future, 0) or 0)
                if account_id <= 0:
                    continue
                try:
                    detail = future.result()
                except Exception as exc:
                    detail = {"id": account_id, "success": False, "error": str(exc), "attempts": 1}

                completed_count += 1
                if detail.get("success") is True:
                    success_count += 1
                elif not detail.get("skipped"):
                    failed_count += 1

                _append_account_async_task_detail(task_id, detail)
                _set_account_async_task_progress(
                    task_id,
                    total=total,
                    completed=completed_count,
                    success=success_count,
                    failed=failed_count,
                )
                _update_account_async_task(
                    task_id,
                    message=f"处理中 {completed_count}/{total}（并发 {worker_count}）",
                )

        if cancelled:
            for future in list(running.keys()):
                future.cancel()
            _update_account_async_task(
                task_id,
                status="cancelled",
                finished_at=_utc_now_iso(),
                message=f"任务已取消，进度 {completed_count}/{total}",
                paused=False,
                result={
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "total": total,
                    "cancelled": True,
                },
            )
            return
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    _update_account_async_task(
        task_id,
        status="completed",
        finished_at=_utc_now_iso(),
        message=f"刷新完成：成功 {success_count}，失败 {failed_count}",
        paused=False,
        result={
            "success_count": success_count,
            "failed_count": failed_count,
            "total": total,
            "cancelled": False,
        },
    )


@router.get("/tasks/{task_id}")
def get_account_async_task_status(task_id: str):
    task = _get_account_async_task_or_404(task_id)
    return _build_account_async_task_snapshot(task)


@router.post("/tasks/{task_id}/cancel")
def cancel_account_async_task(task_id: str):
    with _account_async_tasks_lock:
        task = _account_async_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.get("status") in {"completed", "failed", "cancelled"}:
            return {
                "success": True,
                "task_id": task_id,
                "status": task.get("status"),
                "message": "任务已结束，无需取消",
            }
        task["cancel_requested"] = True
        task["pause_requested"] = False
        task["paused"] = False
        task["message"] = "已提交取消请求，等待任务停止"
    task_manager.request_domain_task_cancel("accounts", task_id)
    return {
        "success": True,
        "task_id": task_id,
        "status": "cancelling",
    }


@router.post("/tasks/{task_id}/pause")
def pause_account_async_task(task_id: str):
    with _account_async_tasks_lock:
        task = _account_async_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        status = str(task.get("status") or "").strip().lower()
        if status in {"completed", "failed", "cancelled"}:
            return {
                "success": True,
                "task_id": task_id,
                "status": status,
                "message": "任务已结束，无法暂停",
            }
        task["pause_requested"] = True
        task["paused"] = True
        task["status"] = "paused"
        task["message"] = "任务已暂停，等待继续"
    task_manager.request_domain_task_pause("accounts", task_id)
    return {
        "success": True,
        "task_id": task_id,
        "status": "paused",
        "message": "任务已暂停",
    }


@router.post("/tasks/{task_id}/resume")
def resume_account_async_task(task_id: str):
    with _account_async_tasks_lock:
        task = _account_async_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        status = str(task.get("status") or "").strip().lower()
        if status in {"completed", "failed", "cancelled"}:
            return {
                "success": True,
                "task_id": task_id,
                "status": status,
                "message": "任务已结束，无需继续",
            }
        task["pause_requested"] = False
        task["paused"] = False
        if status == "paused":
            task["status"] = "running"
            task["message"] = "任务已继续执行"
    task_manager.request_domain_task_resume("accounts", task_id)
    return {
        "success": True,
        "task_id": task_id,
        "status": "running",
        "message": "任务已继续执行",
    }


def retry_account_async_task(task_id: str) -> Dict[str, Any]:
    task = _get_account_async_task_or_404(task_id)
    task_type = str(task.get("task_type") or "").strip().lower()
    payload = dict(task.get("payload") or {})

    ids = payload.get("ids")
    if not isinstance(ids, list):
        ids = []
    safe_ids = [int(item) for item in ids if str(item).strip().isdigit()]

    common_kwargs = {
        "ids": safe_ids,
        "proxy": payload.get("proxy"),
        "select_all": bool(payload.get("select_all", False)),
        "status_filter": payload.get("status_filter"),
        "email_service_filter": payload.get("email_service_filter"),
        "search_filter": payload.get("search_filter"),
    }

    if task_type == "batch_refresh":
        return start_batch_refresh_async(BatchRefreshRequest(**common_kwargs))
    if task_type == "batch_validate":
        return start_batch_validate_async(BatchValidateRequest(**common_kwargs))
    if task_type == "overview_refresh":
        return start_overview_refresh_async(
            OverviewRefreshRequest(
                **common_kwargs,
                force=bool(payload.get("force", False)),
            )
        )

    raise HTTPException(status_code=400, detail=f"不支持重试的任务类型: {task_type or '-'}")


@router.post("/batch-refresh/async")
def start_batch_refresh_async(request: BatchRefreshRequest):
    proxy = _get_proxy(request.proxy)
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    task_payload = {
        "ids": [int(item) for item in ids],
        "proxy": proxy,
        "select_all": bool(request.select_all),
        "status_filter": request.status_filter,
        "email_service_filter": request.email_service_filter,
        "search_filter": request.search_filter,
    }
    task_id = _create_account_async_task("batch_refresh", total=len(ids), payload=task_payload)
    if not ids:
        _update_account_async_task(
            task_id,
            status="completed",
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
            message="没有可刷新的账号",
            result={"success_count": 0, "failed_count": 0, "total": 0, "cancelled": False},
        )
    else:
        _account_async_executor.submit(
            _run_account_async_task_guard,
            task_id,
            "batch_refresh",
            _run_batch_refresh_task,
            ids,
            proxy,
        )

    task = _get_account_async_task_or_404(task_id)
    return _build_account_async_task_snapshot(task)


@router.post("/batch-validate/async")
def start_batch_validate_async(request: BatchValidateRequest):
    proxy = _get_proxy(request.proxy)
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    task_payload = {
        "ids": [int(item) for item in ids],
        "proxy": proxy,
        "select_all": bool(request.select_all),
        "status_filter": request.status_filter,
        "email_service_filter": request.email_service_filter,
        "search_filter": request.search_filter,
    }
    task_id = _create_account_async_task("batch_validate", total=len(ids), payload=task_payload)
    if not ids:
        _update_account_async_task(
            task_id,
            status="completed",
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
            message="没有可验证的账号",
            result={"valid_count": 0, "invalid_count": 0, "total": 0, "cancelled": False},
        )
    else:
        _account_async_executor.submit(
            _run_account_async_task_guard,
            task_id,
            "batch_validate",
            _run_batch_validate_task,
            ids,
            proxy,
        )

    task = _get_account_async_task_or_404(task_id)
    return _build_account_async_task_snapshot(task)


@router.post("/overview/refresh/async")
def start_overview_refresh_async(request: OverviewRefreshRequest):
    proxy = _get_proxy(request.proxy)
    with get_db() as db:
        ids = resolve_account_ids(
            db,
            request.ids,
            request.select_all,
            request.status_filter,
            request.email_service_filter,
            request.search_filter,
        )
        if not ids:
            candidates = db.query(Account).filter(
                func.lower(Account.subscription_type).in_(PAID_SUBSCRIPTION_TYPES)
            ).order_by(Account.created_at.desc()).all()
            ids = [acc.id for acc in candidates if not _is_overview_card_removed(acc)]

    task_payload = {
        "ids": [int(item) for item in ids],
        "proxy": proxy,
        "force": bool(request.force),
        "select_all": bool(request.select_all),
        "status_filter": request.status_filter,
        "email_service_filter": request.email_service_filter,
        "search_filter": request.search_filter,
    }
    task_id = _create_account_async_task("overview_refresh", total=len(ids), payload=task_payload)
    if not ids:
        _update_account_async_task(
            task_id,
            status="completed",
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
            message="没有可刷新的总览账号",
            result={"success_count": 0, "failed_count": 0, "total": 0, "cancelled": False},
        )
    else:
        _account_async_executor.submit(
            _run_account_async_task_guard,
            task_id,
            "overview_refresh",
            _run_overview_refresh_async_task,
            ids,
            bool(request.force),
            proxy,
        )

    task = _get_account_async_task_or_404(task_id)
    return _build_account_async_task_snapshot(task)


@router.post("/batch-refresh")
def batch_refresh_tokens(request: BatchRefreshRequest):
    """批量刷新账号 Token（并发执行，避免长轮询阻塞）。"""
    proxy = _get_proxy(request.proxy)

    results = {
        "success_count": 0,
        "failed_count": 0,
        "errors": []
    }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    if not ids:
        return results

    def _refresh_one(account_id: int) -> dict:
        try:
            result = do_refresh(account_id, proxy)
            if result.success:
                return {"id": account_id, "success": True}
            return {"id": account_id, "success": False, "error": result.error_message}
        except Exception as exc:
            return {"id": account_id, "success": False, "error": str(exc)}

    worker_count = min(6, max(1, len(ids)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch_refresh") as pool:
        future_map = {pool.submit(_refresh_one, account_id): account_id for account_id in ids}
        for future in as_completed(future_map):
            item = future.result()
            if item.get("success"):
                results["success_count"] += 1
            else:
                results["failed_count"] += 1
                results["errors"].append({"id": item.get("id"), "error": item.get("error")})

    return results


@router.post("/{account_id}/refresh")
def refresh_account_token(account_id: int, request: Optional[TokenRefreshRequest] = Body(default=None)):
    """刷新单个账号的 Token"""
    proxy = _get_proxy(request.proxy if request else None)
    result = do_refresh(account_id, proxy)

    if result.success:
        return {
            "success": True,
            "message": "Token 刷新成功",
            "expires_at": result.expires_at.isoformat() if result.expires_at else None
        }
    else:
        return {
            "success": False,
            "error": result.error_message
        }


@router.post("/batch-validate")
def batch_validate_tokens(request: BatchValidateRequest):
    """批量验证账号 Token 有效性（并发执行）。"""
    started = time.perf_counter()
    proxy = _get_proxy(request.proxy)

    results = {
        "valid_count": 0,
        "invalid_count": 0,
        "details": [],
        "worker_count": 0,
        "retry_count": 0,
        "duration_ms": 0,
    }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    if not ids:
        results["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return results

    worker_count = _calculate_validate_worker_count(len(ids), async_mode=False)
    results["worker_count"] = worker_count
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch_validate") as pool:
        future_map = {
            pool.submit(
                _validate_one_account_with_retry,
                account_id,
                proxy,
                ACCOUNT_BATCH_VALIDATE_RETRY_ATTEMPTS,
                ACCOUNT_BATCH_VALIDATE_HTTP_TIMEOUT_SECONDS,
            ): account_id
            for account_id in ids
        }
        for future in as_completed(future_map):
            detail = future.result()
            results["details"].append(detail)
            results["retry_count"] += max(0, int(detail.get("attempts") or 1) - 1)
            if detail.get("valid"):
                results["valid_count"] += 1
            else:
                results["invalid_count"] += 1

    results["details"].sort(key=lambda item: int(item.get("id") or 0))
    results["duration_ms"] = int((time.perf_counter() - started) * 1000)
    logger.info(
        "批量验证(同步)完成: total=%s valid=%s invalid=%s workers=%s retries=%s duration_ms=%s proxy=%s",
        len(ids),
        results["valid_count"],
        results["invalid_count"],
        results["worker_count"],
        results["retry_count"],
        results["duration_ms"],
        proxy or "-",
    )
    return results


@router.post("/{account_id}/validate")
def validate_account_token(account_id: int, request: Optional[TokenValidateRequest] = Body(default=None)):
    """验证单个账号的 Token 有效性"""
    proxy = _get_proxy(request.proxy if request else None)
    is_valid, error = do_validate(account_id, proxy)
    next_status = _derive_account_status_from_validate_result(is_valid, error)

    return {
        "id": account_id,
        "valid": is_valid,
        "status": next_status,
        "error": error
    }


# ============== CPA 上传相关 ==============

class CPAUploadRequest(BaseModel):
    """CPA 上传请求"""
    proxy: Optional[str] = None
    cpa_service_id: Optional[int] = None  # 指定 CPA 服务 ID，不传则使用全局配置


class BatchCPAUploadRequest(BaseModel):
    """批量 CPA 上传请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    cpa_service_id: Optional[int] = None  # 指定 CPA 服务 ID，不传则使用全局配置


@router.post("/batch-upload-cpa")
async def batch_upload_accounts_to_cpa(request: BatchCPAUploadRequest):
    """批量上传账号到 CPA"""

    proxy = request.proxy if request.proxy else get_settings().proxy_url

    # 解析指定的 CPA 服务
    cpa_api_url = None
    cpa_api_token = None
    if request.cpa_service_id:
        with get_db() as db:
            svc = crud.get_cpa_service_by_id(db, request.cpa_service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 CPA 服务不存在")
            cpa_api_url = svc.api_url
            cpa_api_token = svc.api_token
            if not request.proxy:
                proxy = getattr(svc, "proxy_url", None) or proxy

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    results = batch_upload_to_cpa(ids, proxy, api_url=cpa_api_url, api_token=cpa_api_token)
    return results


@router.post("/{account_id}/upload-cpa")
async def upload_account_to_cpa(account_id: int, request: Optional[CPAUploadRequest] = Body(default=None)):
    """上传单个账号到 CPA"""

    proxy = request.proxy if request and request.proxy else get_settings().proxy_url
    cpa_service_id = request.cpa_service_id if request else None

    # 解析指定的 CPA 服务
    cpa_api_url = None
    cpa_api_token = None
    if cpa_service_id:
        with get_db() as db:
            svc = crud.get_cpa_service_by_id(db, cpa_service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 CPA 服务不存在")
            cpa_api_url = svc.api_url
            cpa_api_token = svc.api_token
            if not (request and request.proxy):
                proxy = getattr(svc, "proxy_url", None) or proxy

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        if not account.access_token:
            return {
                "success": False,
                "error": "账号缺少 Token，无法上传"
            }

        # 生成 Token JSON
        token_data = generate_token_json(account)

        # 上传
        success, message = upload_to_cpa(token_data, proxy, api_url=cpa_api_url, api_token=cpa_api_token)

        if success:
            account.cpa_uploaded = True
            account.cpa_uploaded_at = datetime.utcnow()
            db.commit()
            return {"success": True, "message": message}
        else:
            return {"success": False, "error": message}


class Sub2ApiUploadRequest(BaseModel):
    """单账号 Sub2API 上传请求"""
    service_id: Optional[int] = None
    concurrency: int = 3
    priority: int = 50


class BatchSub2ApiUploadRequest(BaseModel):
    """批量 Sub2API 上传请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    service_id: Optional[int] = None  # 指定 Sub2API 服务 ID，不传则使用第一个启用的
    concurrency: int = 3
    priority: int = 50


@router.post("/batch-upload-sub2api")
async def batch_upload_accounts_to_sub2api(request: BatchSub2ApiUploadRequest):
    """批量上传账号到 Sub2API"""

    # 解析指定的 Sub2API 服务
    api_url = None
    api_key = None
    target_type = "sub2api"
    if request.service_id:
        with get_db() as db:
            svc = crud.get_sub2api_service_by_id(db, request.service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 Sub2API 服务不存在")
            api_url = svc.api_url
            api_key = svc.api_key
            target_type = getattr(svc, "target_type", "sub2api")
    else:
        with get_db() as db:
            svcs = crud.get_sub2api_services(db, enabled=True)
            if svcs:
                api_url = svcs[0].api_url
                api_key = svcs[0].api_key
                target_type = getattr(svcs[0], "target_type", "sub2api")

    if not api_url or not api_key:
        raise HTTPException(status_code=400, detail="未找到可用的 Sub2API 服务，请先在设置中配置")

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    results = batch_upload_to_sub2api(
        ids, api_url, api_key,
        concurrency=request.concurrency,
        priority=request.priority,
        target_type=target_type,
    )
    return results


@router.post("/{account_id}/upload-sub2api")
async def upload_account_to_sub2api(account_id: int, request: Optional[Sub2ApiUploadRequest] = Body(default=None)):
    """上传单个账号到 Sub2API"""

    service_id = request.service_id if request else None
    concurrency = request.concurrency if request else 3
    priority = request.priority if request else 50

    api_url = None
    api_key = None
    target_type = "sub2api"
    if service_id:
        with get_db() as db:
            svc = crud.get_sub2api_service_by_id(db, service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 Sub2API 服务不存在")
            api_url = svc.api_url
            api_key = svc.api_key
            target_type = getattr(svc, "target_type", "sub2api")
    else:
        with get_db() as db:
            svcs = crud.get_sub2api_services(db, enabled=True)
            if svcs:
                api_url = svcs[0].api_url
                api_key = svcs[0].api_key
                target_type = getattr(svcs[0], "target_type", "sub2api")

    if not api_url or not api_key:
        raise HTTPException(status_code=400, detail="未找到可用的 Sub2API 服务，请先在设置中配置")

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not account.access_token:
            return {"success": False, "error": "账号缺少 Token，无法上传"}

        success, message = upload_to_sub2api(
            [account], api_url, api_key,
            concurrency=concurrency, priority=priority,
            target_type=target_type
        )
        if success:
            return {"success": True, "message": message}
        else:
            return {"success": False, "error": message}


# ============== Team Manager 上传 ==============

class UploadTMRequest(BaseModel):
    service_id: Optional[int] = None


class BatchUploadTMRequest(BaseModel):
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    service_id: Optional[int] = None


@router.post("/batch-upload-tm")
async def batch_upload_accounts_to_tm(request: BatchUploadTMRequest):
    """批量上传账号到 Team Manager"""

    with get_db() as db:
        if request.service_id:
            svc = crud.get_tm_service_by_id(db, request.service_id)
        else:
            svcs = crud.get_tm_services(db, enabled=True)
            svc = svcs[0] if svcs else None

        if not svc:
            raise HTTPException(status_code=400, detail="未找到可用的 Team Manager 服务，请先在设置中配置")

        api_url = svc.api_url
        api_key = svc.api_key

        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    results = batch_upload_to_team_manager(ids, api_url, api_key)
    return results


@router.post("/{account_id}/upload-tm")
async def upload_account_to_tm(account_id: int, request: Optional[UploadTMRequest] = Body(default=None)):
    """上传单账号到 Team Manager"""

    service_id = request.service_id if request else None

    with get_db() as db:
        if service_id:
            svc = crud.get_tm_service_by_id(db, service_id)
        else:
            svcs = crud.get_tm_services(db, enabled=True)
            svc = svcs[0] if svcs else None

        if not svc:
            raise HTTPException(status_code=400, detail="未找到可用的 Team Manager 服务，请先在设置中配置")

        api_url = svc.api_url
        api_key = svc.api_key

        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        success, message = upload_to_team_manager(account, api_url, api_key)

    return {"success": success, "message": message}


# ============== Inbox Code ==============

def _build_inbox_config(db, service_type, email: str) -> dict:
    """根据账号邮箱服务类型从数据库构建服务配置（不传 proxy_url）"""
    from ...database.models import EmailService as EmailServiceModel
    from ...services import EmailServiceType as EST

    if service_type == EST.TEMPMAIL:
        settings = get_settings()
        return {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
        }

    if service_type == EST.MOE_MAIL:
        # 按域名后缀匹配，找不到则取 priority 最小的
        domain = email.split("@")[1] if "@" in email else ""
        services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "moe_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()
        svc = None
        for s in services:
            cfg = s.config or {}
            if cfg.get("default_domain") == domain or cfg.get("domain") == domain:
                svc = s
                break
        if not svc and services:
            svc = services[0]
        if not svc:
            return None
        cfg = svc.config.copy()
        if "api_url" in cfg and "base_url" not in cfg:
            cfg["base_url"] = cfg.pop("api_url")
        return cfg

    # 其余服务类型：直接按 service_type 查数据库
    type_map = {
        EST.TEMP_MAIL: "temp_mail",
        EST.DUCK_MAIL: "duck_mail",
        EST.FREEMAIL: "freemail",
        EST.IMAP_MAIL: "imap_mail",
        EST.OUTLOOK: "outlook",
    }
    db_type = type_map.get(service_type)
    if not db_type:
        return None

    query = db.query(EmailServiceModel).filter(
        EmailServiceModel.service_type == db_type,
        EmailServiceModel.enabled == True
    )
    if service_type == EST.OUTLOOK:
        # 按 config.email 匹配账号 email
        services = query.all()
        svc = next((s for s in services if (s.config or {}).get("email") == email), None)
    else:
        svc = query.order_by(EmailServiceModel.priority.asc()).first()

    if not svc:
        return None
    cfg = svc.config.copy() if svc.config else {}
    if "api_url" in cfg and "base_url" not in cfg:
        cfg["base_url"] = cfg.pop("api_url")
    return cfg


@router.post("/{account_id}/inbox-code")
async def get_account_inbox_code(account_id: int):
    """查询账号邮箱收件箱最新验证码"""
    from ...services import EmailServiceFactory, EmailServiceType

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        try:
            service_type = EmailServiceType(account.email_service)
        except ValueError:
            return {"success": False, "error": "不支持的邮箱服务类型"}

        config = _build_inbox_config(db, service_type, account.email)
        if config is None:
            return {"success": False, "error": "未找到可用的邮箱服务配置"}

        try:
            svc = EmailServiceFactory.create(service_type, config)
            code = svc.get_verification_code(
                account.email,
                email_id=account.email_service_id,
                timeout=12
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

        if not code:
            return {"success": False, "error": "未收到验证码邮件"}

        return {"success": True, "code": code, "email": account.email}
