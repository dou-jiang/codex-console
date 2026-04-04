"""
账号管理 API 路由
"""
import io
import asyncio
import json
import logging
import os
import re
import html
import time
import random
import threading
import zipfile
import base64
import hashlib
import shutil
import subprocess
import tempfile
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Body, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func

from ...config.constants import AccountStatus, OPENAI_PAGE_TYPES
from ...config.settings import get_settings
from ...core.openai.overview import fetch_codex_overview, AccountDeactivatedError
from ...core.openai.oauth import OAuthManager
from ...core.openai.token_refresh import refresh_account_token as do_refresh
from ...core.openai.token_refresh import validate_account_token as do_validate
from ...core.openai.browser_bind import _find_chrome_binary
from ...core.upload.cpa_upload import generate_token_json, batch_upload_to_cpa, upload_to_cpa
from ...core.upload.team_manager_upload import upload_to_team_manager, batch_upload_to_team_manager
from ...core.upload.sub2api_upload import batch_upload_to_sub2api, upload_to_sub2api
from ...core.upload.new_api_upload import batch_upload_to_new_api, upload_to_new_api

from ...core.dynamic_proxy import get_proxy_url_for_task
from ...core.timezone_utils import utcnow_naive
from ...core.register import RegistrationEngine, RegistrationResult
from ...database import crud
from ...database.models import Account, EmailService as EmailServiceModel
from ...database.session import get_db
from ...services import EmailServiceFactory, EmailServiceType
from ..task_manager import task_manager

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

_QUICK_REFRESH_WORKFLOW_LOCK = threading.Lock()
_MANUAL_OAUTH_SESSION_LOCK = threading.Lock()
_MANUAL_OAUTH_SESSION_TTL_SECONDS = 1800
_MANUAL_OAUTH_SESSIONS: Dict[int, Dict[str, Any]] = {}
_MANUAL_OAUTH_LISTENER_LOCK = threading.Lock()
_MANUAL_OAUTH_LISTENER: Optional[ThreadingHTTPServer] = None
_MANUAL_OAUTH_LISTENER_THREAD: Optional[threading.Thread] = None


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


def _get_quick_refresh_candidate_ids() -> List[int]:
    with get_db() as db:
        query = (
            db.query(Account.id)
            .filter(func.length(func.trim(func.coalesce(Account.access_token, ""))) > 0)
            .filter(~Account.status.in_((AccountStatus.FAILED.value, AccountStatus.BANNED.value)))
            .order_by(Account.id.asc())
        )
        return [int(row[0]) for row in query.all()]


def has_active_batch_operations() -> bool:
    if _QUICK_REFRESH_WORKFLOW_LOCK.locked():
        return True

    busy_statuses = {"pending", "running", "paused"}
    for domain in ("accounts", "payment"):
        try:
            tasks = task_manager.list_domain_tasks(domain=domain, limit=50)
        except Exception:
            continue
        for task in tasks:
            status = str(task.get("status") or "").strip().lower()
            if status in busy_statuses:
                return True
    return False


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
    subscription_type: Optional[str] = None
    subscription_at: Optional[str] = None
    cookies: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


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


class ManualOAuthStartRequest(BaseModel):
    """手动浏览器 OAuth 启动请求"""
    proxy: Optional[str] = None
    use_desktop_automation: bool = False
    use_current_browser_window: bool = True
    use_edge_attach: bool = True
    use_playwright: bool = False
    headless: bool = False


class ManualOAuthCallbackRequest(BaseModel):
    """手动浏览器 OAuth 回调提交请求"""
    callback_url: str


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
        cockpit_tokens = _build_cockpit_tokens(account, include_account_hint=True)
        cockpit_payload = _build_cockpit_account_export(account)
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
            "organization_id": cockpit_payload.get("organization_id"),
            "auth_mode": "oauth",
            "tokens": cockpit_tokens,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(output_file)
    except Exception as exc:
        logger.warning(f"写入 current_codex_account.json 失败: {exc}")
        return None


def _build_oauth_manager(proxy: Optional[str] = None, redirect_uri: Optional[str] = None) -> OAuthManager:
    settings = get_settings()
    return OAuthManager(
        client_id=settings.openai_client_id,
        auth_url=settings.openai_auth_url,
        token_url=settings.openai_token_url,
        redirect_uri=str(redirect_uri or settings.openai_redirect_uri or "").strip(),
        scope=settings.openai_scope,
        proxy_url=proxy,
    )


def _cleanup_manual_oauth_sessions(now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    expired_ids: List[int] = []
    for account_id, session_data in list(_MANUAL_OAUTH_SESSIONS.items()):
        created_at = session_data.get("created_at")
        if not isinstance(created_at, datetime):
            expired_ids.append(account_id)
            continue
        age = now - created_at.astimezone(timezone.utc)
        if age > timedelta(seconds=_MANUAL_OAUTH_SESSION_TTL_SECONDS):
            expired_ids.append(account_id)
    for account_id in expired_ids:
        _MANUAL_OAUTH_SESSIONS.pop(account_id, None)


def _store_manual_oauth_session(account_id: int, start: Any, proxy: Optional[str] = None) -> Dict[str, Any]:
    created_at = datetime.now(timezone.utc)
    session_data = {
        "auth_url": str(getattr(start, "auth_url", "") or "").strip(),
        "state": str(getattr(start, "state", "") or "").strip(),
        "code_verifier": str(getattr(start, "code_verifier", "") or "").strip(),
        "redirect_uri": str(getattr(start, "redirect_uri", "") or "").strip(),
        "proxy": str(proxy or "").strip() or None,
        "created_at": created_at,
        "status": "pending",
        "last_error": None,
        "completed_at": None,
        "result": None,
        "browser_mode": "external",
        "browser_status": "idle",
        "browser_error": None,
        "browser_current_url": None,
        "browser_started_at": None,
        "browser_closed_at": None,
    }
    with _MANUAL_OAUTH_SESSION_LOCK:
        _cleanup_manual_oauth_sessions(now=created_at)
        _MANUAL_OAUTH_SESSIONS[account_id] = session_data
    return session_data


def _get_manual_oauth_session(account_id: int) -> Optional[Dict[str, Any]]:
    with _MANUAL_OAUTH_SESSION_LOCK:
        _cleanup_manual_oauth_sessions()
        session_data = _MANUAL_OAUTH_SESSIONS.get(account_id)
        return dict(session_data) if session_data else None


def _update_manual_oauth_session(account_id: int, **updates: Any) -> Optional[Dict[str, Any]]:
    with _MANUAL_OAUTH_SESSION_LOCK:
        _cleanup_manual_oauth_sessions()
        current = _MANUAL_OAUTH_SESSIONS.get(account_id)
        if not current:
            return None
        current.update(updates)
        _MANUAL_OAUTH_SESSIONS[account_id] = current
        return dict(current)


def _clear_manual_oauth_session(account_id: int) -> None:
    with _MANUAL_OAUTH_SESSION_LOCK:
        _MANUAL_OAUTH_SESSIONS.pop(account_id, None)


def _manual_oauth_result_payload(account: Account, persisted: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "account": persisted,
        "email": account.email,
        "has_refresh_token": bool(account.refresh_token),
        "has_id_token": bool(account.id_token),
    }


def _build_manual_oauth_redirect_uri(request: Request, account_id: int) -> str:
    return str(request.url_for("manual_oauth_callback_landing", account_id=str(account_id)))


def _find_manual_oauth_account_id_by_state(state: str) -> Optional[int]:
    expected_state = str(state or "").strip()
    if not expected_state:
        return None
    with _MANUAL_OAUTH_SESSION_LOCK:
        _cleanup_manual_oauth_sessions()
        for account_id, session_data in _MANUAL_OAUTH_SESSIONS.items():
            if str(session_data.get("state") or "").strip() == expected_state:
                return int(account_id)
    return None


def _render_manual_oauth_listener_html(title: str, status_line: str, detail: str, status_value: str, account_id: Optional[int]) -> str:
    safe_title = html.escape(title)
    safe_status = html.escape(status_line)
    safe_detail = html.escape(detail)
    safe_state = html.escape(str(status_value or "").upper())
    badge_bg = "#e8f7ee" if status_value == "completed" else "#fff1f0"
    badge_fg = "#0f7b3b" if status_value == "completed" else "#c0392b"
    account_literal = "null" if account_id is None else str(int(account_id))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7fb; color: #172033; margin: 0; }}
    .wrap {{ max-width: 560px; margin: 10vh auto; background: white; border-radius: 16px; padding: 24px; box-shadow: 0 18px 50px rgba(18, 38, 63, 0.12); }}
    .badge {{ display: inline-block; padding: 6px 10px; border-radius: 999px; background: {badge_bg}; color: {badge_fg}; font-size: 12px; font-weight: 600; }}
    h1 {{ font-size: 24px; margin: 14px 0 10px; }}
    p {{ line-height: 1.6; margin: 8px 0; word-break: break-word; }}
    button {{ margin-top: 16px; padding: 10px 16px; border: 0; border-radius: 10px; background: #1663ff; color: white; cursor: pointer; }}
  </style>
</head>
<body>
  <div class="wrap">
    <span class="badge">{safe_state}</span>
    <h1>{safe_title}</h1>
    <p>{safe_status}</p>
    <p>{safe_detail}</p>
    <button type="button" onclick="window.close()">Close Window</button>
  </div>
  <script>
    try {{
      if (window.opener && !window.opener.closed) {{
        window.opener.postMessage({{ type: 'codex-manual-oauth', accountId: {account_literal}, status: '{html.escape(status_value)}' }}, '*');
      }}
    }} catch (e) {{}}
    if ('{html.escape(status_value)}' === 'completed') {{
      setTimeout(() => {{
        try {{
          window.close();
        }} catch (e) {{}}
      }}, 900);
    }}
  </script>
</body>
</html>"""


class _ManualOAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_error(404)
            return

        query = parse_qs(parsed.query, keep_blank_values=True)
        state = str((query.get("state") or [""])[0] or "").strip()
        account_id = _find_manual_oauth_account_id_by_state(state)
        callback_url = f"http://localhost:1455{self.path}"

        if not account_id:
            body = _render_manual_oauth_listener_html(
                "OAuth Failed",
                "No matching OAuth session was found.",
                "Return to the console and restart Browser OAuth Repair.",
                "failed",
                None,
            )
            self._send_html(400, body)
            return

        try:
            payload = _complete_manual_oauth_session(account_id, callback_url)
            body = _render_manual_oauth_listener_html(
                "OAuth Completed",
                "OAuth tokens saved. You can return to the console.",
                f"Account: {payload.get('email') or ''}",
                "completed",
                account_id,
            )
            self._send_html(200, body)
        except LookupError:
            _update_manual_oauth_session(
                account_id,
                status="failed",
                last_error="Account not found",
                completed_at=datetime.now(timezone.utc),
            )
            body = _render_manual_oauth_listener_html(
                "OAuth Failed",
                "The target account no longer exists.",
                "Return to the console and restart Browser OAuth Repair.",
                "failed",
                account_id,
            )
            self._send_html(404, body)
        except Exception as exc:
            _update_manual_oauth_session(
                account_id,
                status="failed",
                last_error=str(exc),
                completed_at=datetime.now(timezone.utc),
            )
            body = _render_manual_oauth_listener_html(
                "OAuth Failed",
                "Automatic callback processing failed.",
                str(exc),
                "failed",
                account_id,
            )
            self._send_html(400, body)

    def log_message(self, format: str, *args):  # noqa: A003
        logger.debug("manual oauth listener: " + format, *args)

    def _send_html(self, status_code: int, body: str) -> None:
        encoded = body.encode("utf-8", errors="replace")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _ensure_manual_oauth_listener() -> None:
    global _MANUAL_OAUTH_LISTENER, _MANUAL_OAUTH_LISTENER_THREAD

    with _MANUAL_OAUTH_LISTENER_LOCK:
        if _MANUAL_OAUTH_LISTENER and _MANUAL_OAUTH_LISTENER_THREAD and _MANUAL_OAUTH_LISTENER_THREAD.is_alive():
            return

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 1455), _ManualOAuthCallbackHandler)
        except OSError as exc:
            raise RuntimeError(f"localhost:1455 unavailable: {exc}") from exc

        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="manual-oauth-listener", daemon=True)
        thread.start()
        _MANUAL_OAUTH_LISTENER = server
        _MANUAL_OAUTH_LISTENER_THREAD = thread
        logger.info("Manual OAuth listener started on http://127.0.0.1:1455/auth/callback")


def _manual_oauth_get_body_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1500) or "")
    except Exception:
        return ""


def _manual_oauth_find_visible_locator(page, selectors: List[str], timeout_ms: int = 800):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 6)
            for idx in range(count):
                candidate = locator.nth(idx)
                if candidate.is_visible(timeout=timeout_ms):
                    return candidate
        except Exception:
            continue
    return None


def _manual_oauth_click_best_effort(page, selectors: List[str]) -> bool:
    locator = _manual_oauth_find_visible_locator(page, selectors)
    if not locator:
        return False
    try:
        locator.click(timeout=3000)
        return True
    except Exception:
        try:
            locator.press("Enter")
            return True
        except Exception:
            return False


def _manual_oauth_fill_email_step(page, email: str) -> bool:
    locator = _manual_oauth_find_visible_locator(
        page,
        [
            "input[type='email']",
            "input[name='email']",
            "input[autocomplete='username']",
            "input[autocomplete='email']",
        ],
    )
    if not locator:
        return False

    try:
        locator.fill(str(email or ""), timeout=3000)
    except Exception:
        return False

    return _manual_oauth_click_best_effort(
        page,
        [
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "button:has-text('继续')",
            "button:has-text('下一步')",
            "button[type='submit']",
        ],
    )


def _manual_oauth_fill_password_step(page, password: str) -> bool:
    locator = _manual_oauth_find_visible_locator(
        page,
        [
            "input[type='password']",
            "input[name='password']",
            "input[autocomplete='current-password']",
        ],
    )
    if not locator:
        return False

    try:
        locator.fill(str(password or ""), timeout=3000)
    except Exception:
        return False

    return _manual_oauth_click_best_effort(
        page,
        [
            "button:has-text('Continue')",
            "button:has-text('Log in')",
            "button:has-text('Sign in')",
            "button:has-text('登录')",
            "button:has-text('继续')",
            "button[type='submit']",
        ],
    )


def _manual_oauth_fill_otp_step(page, code: str) -> bool:
    digits = [ch for ch in str(code or "").strip() if ch.isdigit()]
    if len(digits) != 6:
        return False

    multi_locator = page.locator(
        "input[inputmode='numeric'][maxlength='1'], "
        "input[autocomplete='one-time-code'][maxlength='1']"
    )
    try:
        multi_count = multi_locator.count()
    except Exception:
        multi_count = 0

    if multi_count >= 6:
        try:
            for idx, digit in enumerate(digits[:6]):
                multi_locator.nth(idx).fill(digit, timeout=2000)
        except Exception:
            return False
    else:
        single = _manual_oauth_find_visible_locator(
            page,
            [
                "input[autocomplete='one-time-code']",
                "input[name='code']",
                "input[name*='otp']",
                "input[id*='otp']",
                "input[id*='code']",
                "input[inputmode='numeric']",
            ],
        )
        if not single:
            return False
        try:
            single.fill("".join(digits), timeout=3000)
        except Exception:
            return False

    clicked = _manual_oauth_click_best_effort(
        page,
        [
            "button:has-text('Continue')",
            "button:has-text('Verify')",
            "button:has-text('Submit')",
            "button:has-text('继续')",
            "button:has-text('验证')",
            "button[type='submit']",
        ],
    )
    if not clicked:
        try:
            page.keyboard.press("Enter")
            return True
        except Exception:
            return False
    return True


def _manual_oauth_click_consent_step(page, email: str) -> bool:
    email_text = str(email or "").strip()
    if email_text:
        account_choice = _manual_oauth_find_visible_locator(
            page,
            [
                f"button:has-text('{email_text}')",
                f"[role='button']:has-text('{email_text}')",
                f"text='{email_text}'",
            ],
        )
        if account_choice:
            try:
                account_choice.click(timeout=3000)
                return True
            except Exception:
                pass

    return _manual_oauth_click_best_effort(
        page,
        [
            "button:has-text('Authorize')",
            "button:has-text('Allow')",
            "button:has-text('Continue to Codex')",
            "button:has-text('Continue')",
            "button:has-text('授权')",
            "button:has-text('允许')",
            "button:has-text('继续')",
            "button[type='submit']",
        ],
    )


def _manual_oauth_drive_playwright_page(
    page,
    account: Account,
    email_service,
    otp_state: Dict[str, Any],
) -> str:
    current_url = str(page.url or "")
    body_text = _manual_oauth_get_body_text(page).lower()

    if any(token in current_url.lower() for token in ("email-verification", "email-otp")) or (
        "verification code" in body_text or "one-time code" in body_text or "验证码" in body_text
    ):
        if not otp_state.get("sent_at"):
            otp_state["sent_at"] = time.time()
        code = email_service.get_verification_code(
            email=str(account.email or "").strip(),
            email_id=str(account.email_service_id or "").strip() or None,
            timeout=8,
            otp_sent_at=float(otp_state["sent_at"]),
        )
        if code:
            attempted = otp_state.setdefault("attempted_codes", set())
            if code not in attempted and _manual_oauth_fill_otp_step(page, code):
                attempted.add(code)
                return f"otp:{code}"
        return "wait_otp"

    if _manual_oauth_fill_password_step(page, str(account.password or "").strip()):
        return "password"

    if _manual_oauth_fill_email_step(page, str(account.email or "").strip()):
        return "email"

    if _manual_oauth_click_consent_step(page, str(account.email or "").strip()):
        return "consent"

    return ""


def _find_edge_binary_for_oauth() -> str:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        shutil.which("msedge"),
    ]
    for candidate in candidates:
        path = str(candidate or "").strip()
        if path and Path(path).exists():
            return path
    chrome_fallback = str(_find_chrome_binary() or "").strip()
    return chrome_fallback


def _run_manual_oauth_desktop_worker(
    account_id: int,
    auth_url: str,
    redirect_uri: str,
    proxy: Optional[str] = None,
    headless: bool = False,
    launch_browser: bool = True,
) -> None:
    account: Optional[Account] = None
    email_service = None
    edge_proc = None

    try:
        with get_db() as db:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                _update_manual_oauth_session(
                    account_id,
                    browser_status="failed",
                    browser_error="Account not found",
                )
                return
            if not str(account.email or "").strip() or not str(account.password or "").strip():
                _update_manual_oauth_session(
                    account_id,
                    browser_status="failed",
                    browser_error="Account is missing email or password",
                )
                return
            email_service = _resolve_email_service_for_oauth_backfill(db, account, proxy)
    except Exception as exc:
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error=f"unable to prepare email service: {exc}",
        )
        return

    try:
        import pyautogui
        import pygetwindow as gw
        import pyperclip
    except Exception as exc:
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error=f"desktop automation dependencies missing: {exc}",
        )
        return

    if bool(headless):
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error="desktop automation does not support headless mode",
        )
        return

    edge_binary = str(_find_edge_binary_for_oauth() or "").strip()
    if bool(launch_browser) and not edge_binary:
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error="system Edge/Chrome binary not found",
        )
        return

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.08

    def _window_handle(window_obj) -> Optional[int]:
        try:
            return int(getattr(window_obj, "_hWnd", None) or 0) or None
        except Exception:
            return None

    def _snapshot_window_handles() -> set[int]:
        handles: set[int] = set()
        try:
            for window_obj in gw.getAllWindows():
                handle = _window_handle(window_obj)
                if handle:
                    handles.add(handle)
        except Exception:
            pass
        return handles

    def _focus_edge_window(known_handles: set[int], timeout_seconds: int = 20):
        deadline = time.time() + max(1, int(timeout_seconds))
        fallback_window = None
        while time.time() < deadline:
            try:
                windows = list(gw.getAllWindows())
            except Exception:
                windows = []
            for window_obj in windows:
                title = str(getattr(window_obj, "title", "") or "").strip()
                if not title:
                    continue
                handle = _window_handle(window_obj)
                if fallback_window is None and "edge" in title.lower():
                    fallback_window = window_obj
                if handle and handle in known_handles:
                    continue
                if "edge" in title.lower() or "openai" in title.lower() or "auth" in title.lower():
                    try:
                        if getattr(window_obj, "isMinimized", False):
                            window_obj.restore()
                    except Exception:
                        pass
                    try:
                        window_obj.activate()
                    except Exception:
                        pass
                    time.sleep(1.2)
                    return window_obj
            if fallback_window is not None:
                try:
                    if getattr(fallback_window, "isMinimized", False):
                        fallback_window.restore()
                except Exception:
                    pass
                try:
                    fallback_window.activate()
                except Exception:
                    pass
                time.sleep(1.2)
                return fallback_window
            time.sleep(0.5)
        return None

    def _paste_text(value: str) -> None:
        text = str(value or "")
        if not text:
            return
        try:
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            pyautogui.hotkey("ctrl", "v")
        except Exception:
            pyautogui.write(text, interval=0.03)

    def _press_enter() -> None:
        pyautogui.press("enter")

    def _click_window_point(window_obj, x_ratio: float, y_ratio: float) -> bool:
        if window_obj is None:
            return False
        try:
            left = int(getattr(window_obj, "left", 0))
            top = int(getattr(window_obj, "top", 0))
            width = int(getattr(window_obj, "width", 0))
            height = int(getattr(window_obj, "height", 0))
            if width <= 0 or height <= 0:
                return False
            target_x = left + max(1, min(width - 1, int(width * x_ratio)))
            target_y = top + max(1, min(height - 1, int(height * y_ratio)))
            pyautogui.click(target_x, target_y)
            return True
        except Exception:
            return False

    def _click_continue_button(window_obj) -> bool:
        # The Codex consent page places the primary "Continue" button in the
        # lower-right half of the centered action row.
        attempts = [
            (0.61, 0.79),
            (0.62, 0.80),
            (0.60, 0.78),
        ]
        clicked = False
        for x_ratio, y_ratio in attempts:
            if _click_window_point(window_obj, x_ratio, y_ratio):
                clicked = True
                time.sleep(0.8)
        return clicked

    existing_handles = _snapshot_window_handles()

    _update_manual_oauth_session(
        account_id,
        browser_mode="desktop",
        browser_status="launching" if bool(launch_browser) else "awaiting_browser_window",
        browser_error=None,
        browser_started_at=datetime.now(timezone.utc),
        browser_binary=edge_binary if bool(launch_browser) else "current_edge_window",
        browser_current_url="",
    )

    try:
        if bool(launch_browser):
            edge_args = [edge_binary, "--new-window", auth_url]
            if proxy:
                edge_args.append(f"--proxy-server={proxy}")
            edge_proc = subprocess.Popen(
                edge_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        window_obj = _focus_edge_window(existing_handles, timeout_seconds=40 if not bool(launch_browser) else 25)
        if window_obj is None:
            raise RuntimeError("unable to focus the Edge authorization window")

        _update_manual_oauth_session(
            account_id,
            browser_status="running",
            browser_current_url=auth_url,
        )

        time.sleep(2.5)
        _paste_text(str(account.email or "").strip())
        _press_enter()
        _update_manual_oauth_session(account_id, browser_status="email", browser_current_url=auth_url)

        time.sleep(2.8)
        _paste_text(str(account.password or "").strip())
        _press_enter()
        _update_manual_oauth_session(account_id, browser_status="password", browser_current_url=auth_url)

        otp_sent_at = time.time()
        _update_manual_oauth_session(account_id, browser_status="wait_otp", browser_current_url=auth_url)
        code = email_service.get_verification_code(
            email=str(account.email or "").strip(),
            email_id=str(account.email_service_id or "").strip() or None,
            timeout=75,
            otp_sent_at=otp_sent_at,
        )
        if code:
            window_obj = _focus_edge_window(set(), timeout_seconds=5) or window_obj
            time.sleep(0.8)
            _paste_text(str(code))
            _press_enter()
            _update_manual_oauth_session(account_id, browser_status=f"otp:{code}", browser_current_url=auth_url)
        else:
            _update_manual_oauth_session(account_id, browser_status="wait_otp", browser_error="email OTP not received in time")

        post_otp_deadline = time.time() + 90
        nudged = False
        consent_click_attempted = False
        while time.time() < post_otp_deadline:
            session_data = _get_manual_oauth_session(account_id)
            if not session_data:
                break
            status = str(session_data.get("status") or "pending")
            if status in {"completed", "failed"}:
                break

            if code and not consent_click_attempted and time.time() - otp_sent_at > 9:
                try:
                    window_obj = _focus_edge_window(set(), timeout_seconds=3) or window_obj
                    if _click_continue_button(window_obj):
                        _update_manual_oauth_session(account_id, browser_status="consent_click", browser_current_url=auth_url)
                        consent_click_attempted = True
                except Exception:
                    pass
            elif code and consent_click_attempted and not nudged and time.time() - otp_sent_at > 18:
                try:
                    window_obj = _focus_edge_window(set(), timeout_seconds=3) or window_obj
                    pyautogui.press("tab")
                    pyautogui.press("tab")
                    pyautogui.press("enter")
                    _update_manual_oauth_session(account_id, browser_status="consent", browser_current_url=auth_url)
                    nudged = True
                except Exception:
                    pass
            time.sleep(1.5)

        final_session = _get_manual_oauth_session(account_id) or {}
        final_status = str(final_session.get("status") or "pending")
        if final_status == "completed":
            try:
                window_obj = _focus_edge_window(set(), timeout_seconds=3) or window_obj
                pyautogui.hotkey("alt", "f4")
                time.sleep(0.4)
            except Exception:
                pass
        _update_manual_oauth_session(
            account_id,
            browser_status="completed" if final_status == "completed" else "closed",
            browser_closed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning("Desktop OAuth worker failed: account_id=%s error=%s", account_id, exc)
        _update_manual_oauth_session(
            account_id,
            browser_mode="desktop",
            browser_status="failed",
            browser_error=str(exc),
            browser_closed_at=datetime.now(timezone.utc),
        )
    finally:
        if edge_proc is not None:
            try:
                if edge_proc.poll() is None:
                    edge_proc.terminate()
            except Exception:
                pass


def _start_manual_oauth_desktop_worker(
    account_id: int,
    auth_url: str,
    redirect_uri: str,
    proxy: Optional[str] = None,
    headless: bool = False,
    launch_browser: bool = True,
) -> Dict[str, Any]:
    try:
        import pyautogui  # noqa: F401
        import pygetwindow  # noqa: F401
        import pyperclip  # noqa: F401
    except Exception as exc:
        return {"started": False, "error": f"desktop automation dependencies missing: {exc}"}

    thread = threading.Thread(
        target=_run_manual_oauth_desktop_worker,
        args=(account_id, auth_url, redirect_uri),
        kwargs={"proxy": proxy, "headless": headless, "launch_browser": launch_browser},
        name=f"manual-oauth-desktop-{account_id}",
        daemon=True,
    )
    thread.start()
    return {"started": True, "error": None}


def _run_manual_oauth_edge_attach_worker(
    account_id: int,
    auth_url: str,
    redirect_uri: str,
    proxy: Optional[str] = None,
    headless: bool = False,
) -> None:
    account: Optional[Account] = None
    email_service = None
    edge_proc = None
    user_data_dir = None
    cdp_url = ""

    try:
        with get_db() as db:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                _update_manual_oauth_session(
                    account_id,
                    browser_status="failed",
                    browser_error="Account not found",
                )
                return
            if not str(account.email or "").strip() or not str(account.password or "").strip():
                _update_manual_oauth_session(
                    account_id,
                    browser_status="failed",
                    browser_error="Account is missing email or password",
                )
                return
            email_service = _resolve_email_service_for_oauth_backfill(db, account, proxy)
    except Exception as exc:
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error=f"unable to prepare email service: {exc}",
        )
        return

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error=f"playwright not installed: {exc}",
        )
        return

    edge_binary = str(_find_edge_binary_for_oauth() or "").strip()
    if not edge_binary:
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error="system Edge/Chrome binary not found",
        )
        return

    cdp_port = random.randint(19455, 19999)
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    user_data_dir = tempfile.mkdtemp(prefix=f"codex-edge-oauth-{account_id}-")
    edge_args = [
        edge_binary,
        f"--remote-debugging-port={cdp_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-popup-blocking",
        "--new-window",
        f"--user-data-dir={user_data_dir}",
        "--window-size=1366,900",
        auth_url,
    ]
    if proxy:
        edge_args.append(f"--proxy-server={proxy}")
    if headless:
        edge_args.extend(["--headless=new", "--disable-gpu"])

    _update_manual_oauth_session(
        account_id,
        browser_mode="edge_cdp",
        browser_status="launching",
        browser_error=None,
        browser_started_at=datetime.now(timezone.utc),
        browser_binary=edge_binary,
        browser_current_url="",
    )

    try:
        edge_proc = subprocess.Popen(
            edge_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        cdp_ready = False
        for _ in range(24):
            try:
                with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2) as resp:
                    data = json.loads(resp.read() or b"{}")
                    if data.get("Browser"):
                        cdp_ready = True
                        break
            except Exception:
                time.sleep(0.5)

        if not cdp_ready:
            raise RuntimeError("Edge CDP port not responding")

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            try:
                contexts = list(browser.contexts)
                context = contexts[0] if contexts else browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )

                page = None
                deadline = time.time() + 15
                while time.time() < deadline and page is None:
                    try:
                        pages = list(context.pages)
                    except Exception:
                        pages = []
                    for candidate in reversed(pages):
                        candidate_url = str(getattr(candidate, "url", "") or "")
                        if candidate_url.startswith("http://localhost:1455/auth/callback") or "auth.openai.com" in candidate_url:
                            page = candidate
                            break
                    if page is None:
                        time.sleep(0.5)

                if page is None:
                    page = context.new_page()
                    page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)

                page.set_default_timeout(60000)
                _update_manual_oauth_session(
                    account_id,
                    browser_status="running",
                    browser_error=None,
                    browser_current_url=str(page.url or ""),
                )

                oauth_deadline = time.time() + _MANUAL_OAUTH_SESSION_TTL_SECONDS
                otp_state: Dict[str, Any] = {"sent_at": None, "attempted_codes": set()}
                while time.time() < oauth_deadline:
                    session_data = _get_manual_oauth_session(account_id)
                    if not session_data:
                        break

                    status = str(session_data.get("status") or "pending")
                    if status in {"completed", "failed"}:
                        break

                    if page.is_closed():
                        _update_manual_oauth_session(
                            account_id,
                            browser_status="closed_by_user",
                            browser_current_url="",
                        )
                        return

                    try:
                        current_url = str(page.url or "")
                        if current_url:
                            last_action = _manual_oauth_drive_playwright_page(
                                page,
                                account,
                                email_service,
                                otp_state,
                            )
                            _update_manual_oauth_session(
                                account_id,
                                browser_status=str(last_action or "running"),
                                browser_current_url=current_url,
                            )
                            if current_url.startswith(redirect_uri):
                                page.wait_for_timeout(1200)

                        page.wait_for_timeout(1000)
                    except Exception as page_exc:
                        latest_session = _get_manual_oauth_session(account_id) or {}
                        latest_status = str(latest_session.get("status") or "pending")
                        if latest_status == "completed":
                            break
                        page_exc_text = str(page_exc or "").lower()
                        if "target page, context or browser has been closed" in page_exc_text:
                            _update_manual_oauth_session(
                                account_id,
                                browser_status="closed_by_user",
                                browser_current_url="",
                            )
                            return
                        raise

                final_session = _get_manual_oauth_session(account_id) or {}
                final_status = str(final_session.get("status") or "pending")
                _update_manual_oauth_session(
                    account_id,
                    browser_status="completed" if final_status == "completed" else "closed",
                    browser_current_url=str(page.url or ""),
                    browser_closed_at=datetime.now(timezone.utc),
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Edge OAuth browser worker failed: account_id=%s error=%s", account_id, exc)
        _update_manual_oauth_session(
            account_id,
            browser_mode="edge_cdp",
            browser_status="failed",
            browser_error=str(exc),
            browser_closed_at=datetime.now(timezone.utc),
        )
    finally:
        if edge_proc is not None:
            try:
                edge_proc.terminate()
            except Exception:
                pass
        if user_data_dir:
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            except Exception:
                pass


def _start_manual_oauth_edge_attach_worker(
    account_id: int,
    auth_url: str,
    redirect_uri: str,
    proxy: Optional[str] = None,
    headless: bool = False,
) -> Dict[str, Any]:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as exc:
        return {
            "started": False,
            "error": f"playwright not installed (pip install playwright && playwright install chromium): {exc}",
        }

    thread = threading.Thread(
        target=_run_manual_oauth_edge_attach_worker,
        args=(account_id, auth_url, redirect_uri),
        kwargs={"proxy": proxy, "headless": headless},
        name=f"manual-oauth-edge-cdp-{account_id}",
        daemon=True,
    )
    thread.start()
    return {"started": True, "error": None}


def _run_manual_oauth_playwright_worker(
    account_id: int,
    auth_url: str,
    redirect_uri: str,
    proxy: Optional[str] = None,
    headless: bool = False,
) -> None:
    account: Optional[Account] = None
    email_service = None
    try:
        with get_db() as db:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                _update_manual_oauth_session(
                    account_id,
                    browser_status="failed",
                    browser_error="Account not found",
                )
                return
            if not str(account.email or "").strip() or not str(account.password or "").strip():
                _update_manual_oauth_session(
                    account_id,
                    browser_status="failed",
                    browser_error="Account is missing email or password",
                )
                return
            email_service = _resolve_email_service_for_oauth_backfill(db, account, proxy)
    except Exception as exc:
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error=f"unable to prepare email service: {exc}",
        )
        return

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error=f"playwright not installed: {exc}",
        )
        return

    try:
        launch_kwargs: Dict[str, Any] = {"headless": bool(headless)}
        proxy_server = str(proxy or "").strip()
        if proxy_server:
            launch_kwargs["proxy"] = {"server": proxy_server, "bypass": "localhost,127.0.0.1"}
        chrome_binary = str(_find_chrome_binary() or "").strip()
        if chrome_binary:
            launch_kwargs["executable_path"] = chrome_binary
        launch_kwargs["args"] = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
        ]

        _update_manual_oauth_session(
            account_id,
            browser_status="launching",
            browser_error=None,
            browser_started_at=datetime.now(timezone.utc),
            browser_binary=chrome_binary or None,
            browser_current_url="",
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                page.set_default_timeout(60000)
                page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
                _update_manual_oauth_session(
                    account_id,
                    browser_status="running",
                    browser_error=None,
                    browser_current_url=str(page.url or ""),
                )

                deadline = time.time() + _MANUAL_OAUTH_SESSION_TTL_SECONDS
                otp_state: Dict[str, Any] = {"sent_at": None, "attempted_codes": set()}
                while time.time() < deadline:
                    session_data = _get_manual_oauth_session(account_id)
                    if not session_data:
                        break

                    status = str(session_data.get("status") or "pending")
                    if status in {"completed", "failed"}:
                        break

                    if page.is_closed():
                        _update_manual_oauth_session(
                            account_id,
                            browser_status="closed_by_user",
                            browser_current_url="",
                        )
                        return

                    try:
                        current_url = str(page.url or "")
                        if current_url:
                            last_action = _manual_oauth_drive_playwright_page(
                                page,
                                account,
                                email_service,
                                otp_state,
                            )
                            _update_manual_oauth_session(
                                account_id,
                                browser_status=str(last_action or "running"),
                                browser_current_url=current_url,
                            )
                            if current_url.startswith(redirect_uri):
                                page.wait_for_timeout(1200)

                        page.wait_for_timeout(1000)
                    except Exception as page_exc:
                        latest_session = _get_manual_oauth_session(account_id) or {}
                        latest_status = str(latest_session.get("status") or "pending")
                        if latest_status == "completed":
                            break
                        page_exc_text = str(page_exc or "").lower()
                        if "target page, context or browser has been closed" in page_exc_text:
                            _update_manual_oauth_session(
                                account_id,
                                browser_status="closed_by_user",
                                browser_current_url="",
                            )
                            return
                        raise

                final_session = _get_manual_oauth_session(account_id) or {}
                final_status = str(final_session.get("status") or "pending")
                _update_manual_oauth_session(
                    account_id,
                    browser_status="completed" if final_status == "completed" else "closed",
                    browser_current_url=str(page.url or ""),
                    browser_closed_at=datetime.now(timezone.utc),
                )
            finally:
                browser.close()
    except Exception as exc:
        logger.warning("Playwright OAuth browser worker failed: account_id=%s error=%s", account_id, exc)
        _update_manual_oauth_session(
            account_id,
            browser_status="failed",
            browser_error=str(exc),
            browser_closed_at=datetime.now(timezone.utc),
        )


def _start_manual_oauth_playwright_worker(
    account_id: int,
    auth_url: str,
    redirect_uri: str,
    proxy: Optional[str] = None,
    headless: bool = False,
) -> Dict[str, Any]:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as exc:
        return {
            "started": False,
            "error": f"playwright not installed (pip install playwright && playwright install chromium): {exc}",
        }

    thread = threading.Thread(
        target=_run_manual_oauth_playwright_worker,
        args=(account_id, auth_url, redirect_uri),
        kwargs={"proxy": proxy, "headless": headless},
        name=f"manual-oauth-playwright-{account_id}",
        daemon=True,
    )
    thread.start()
    return {"started": True, "error": None}


def _save_manual_oauth_tokens_to_account(
    db,
    account: Account,
    token_info: Dict[str, Any],
) -> Dict[str, Any]:
    resolved_email = str(token_info.get("email") or "").strip()
    if resolved_email and resolved_email.lower() != str(account.email or "").strip().lower():
        raise ValueError(f"OAuth 授权账号不匹配，当前记录是 {account.email}，本次授权得到的是 {resolved_email}")

    access_token = str(token_info.get("access_token") or "").strip()
    refresh_token = str(token_info.get("refresh_token") or "").strip()
    id_token = str(token_info.get("id_token") or "").strip()
    account_id = str(token_info.get("account_id") or "").strip()
    expired_at = _parse_iso_datetime(str(token_info.get("expired") or "").strip())

    if not access_token:
        raise ValueError("OAuth 回调未返回 access_token")
    if not refresh_token:
        raise ValueError("OAuth 回调未返回 refresh_token")
    if not id_token:
        raise ValueError("OAuth 回调未返回 id_token")

    account.access_token = access_token
    account.refresh_token = refresh_token
    account.id_token = id_token
    account.client_id = str(get_settings().openai_client_id or account.client_id or "").strip() or None
    if account_id:
        account.account_id = account_id
    if expired_at:
        account.expires_at = expired_at.astimezone(timezone.utc).replace(tzinfo=None)
    account.last_refresh = utcnow_naive()

    current_account_id = _get_current_account_id(db)
    db.commit()
    db.refresh(account)

    snapshot_path = None
    if current_account_id == account.id:
        snapshot_path = _write_current_account_snapshot(account)

    return {
        "email": account.email,
        "account_id": account.account_id,
        "workspace_id": account.workspace_id,
        "snapshot_file": snapshot_path,
    }


def _complete_manual_oauth_session(account_id: int, callback_url: str) -> Dict[str, Any]:
    callback_url = str(callback_url or "").strip()
    if not callback_url:
        raise ValueError("callback_url 涓嶅彲涓虹┖")

    session_data = _get_manual_oauth_session(account_id)
    if not session_data:
        raise ValueError("褰撳墠璐﹀彿娌℃湁鍙敤鐨?OAuth 浼氳瘽锛岄渶閲嶆柊鐐瑰嚮 Browser OAuth Repair")

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise LookupError("account_not_found")

        oauth_manager = _build_oauth_manager(
            proxy=session_data.get("proxy"),
            redirect_uri=session_data.get("redirect_uri"),
        )
        token_info = oauth_manager.handle_callback(
            callback_url=callback_url,
            expected_state=str(session_data.get("state") or ""),
            code_verifier=str(session_data.get("code_verifier") or ""),
        )
        persisted = _save_manual_oauth_tokens_to_account(db, account, token_info)
        payload = _manual_oauth_result_payload(account, persisted)
        _update_manual_oauth_session(
            account_id,
            status="completed",
            last_error=None,
            completed_at=datetime.now(timezone.utc),
            result=payload,
        )
        return payload


def run_desktop_oauth_backfill_and_wait(
    account_id: int,
    proxy: Optional[str] = None,
    timeout_seconds: int = 300,
    launch_browser: bool = True,
) -> Dict[str, Any]:
    _ensure_manual_oauth_listener()
    oauth_manager = _build_oauth_manager(proxy=proxy)
    oauth_start = oauth_manager.start_oauth()
    session_data = _store_manual_oauth_session(account_id, oauth_start, proxy=proxy)
    _update_manual_oauth_session(
        account_id,
        browser_mode="desktop",
        browser_status="queued",
        browser_error=None,
    )

    worker_result = _start_manual_oauth_desktop_worker(
        account_id=account_id,
        auth_url=session_data["auth_url"],
        redirect_uri=session_data["redirect_uri"],
        proxy=proxy,
        headless=False,
        launch_browser=launch_browser,
    )
    if not worker_result.get("started"):
        raise RuntimeError(str(worker_result.get("error") or "desktop oauth worker failed to start"))

    deadline = time.time() + max(30, int(timeout_seconds))
    last_error = None
    while time.time() < deadline:
        session_snapshot = _get_manual_oauth_session(account_id)
        if not session_snapshot:
            raise RuntimeError("manual oauth session expired")

        status = str(session_snapshot.get("status") or "").strip().lower()
        if status == "completed":
            return dict(session_snapshot.get("result") or {})
        if status == "failed":
            last_error = str(session_snapshot.get("last_error") or session_snapshot.get("browser_error") or "oauth backfill failed")
            raise RuntimeError(last_error)

        browser_error = str(session_snapshot.get("browser_error") or "").strip()
        if browser_error:
            last_error = browser_error
        time.sleep(1.0)

    raise TimeoutError(last_error or f"oauth backfill timed out after {int(timeout_seconds)}s")


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


def _pick_first_text(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _jwt_b64url_encode(data: Dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _build_synthetic_id_token(account: Any) -> str:
    access_claims = _decode_jwt_payload_unverified(getattr(account, "access_token", None))
    auth_claims = _get_nested(access_claims, ["https://api.openai.com/auth"]) or {}
    profile_claims = _get_nested(access_claims, ["https://api.openai.com/profile"]) or {}

    email = _pick_first_text(
        getattr(account, "email", None),
        profile_claims.get("email") if isinstance(profile_claims, dict) else None,
        access_claims.get("email"),
    ) or f"account-{getattr(account, 'id', 'unknown')}@unknown.local"
    account_id = _pick_first_text(
        getattr(account, "account_id", None),
        auth_claims.get("account_id") if isinstance(auth_claims, dict) else None,
        auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None,
    )
    organization_id = _pick_first_text(
        getattr(account, "workspace_id", None),
        getattr(account, "organization_id", None),
        auth_claims.get("organization_id") if isinstance(auth_claims, dict) else None,
        auth_claims.get("chatgpt_organization_id") if isinstance(auth_claims, dict) else None,
    )
    user_id = _pick_first_text(
        auth_claims.get("chatgpt_user_id") if isinstance(auth_claims, dict) else None,
        auth_claims.get("user_id") if isinstance(auth_claims, dict) else None,
    )
    plan_type = _pick_first_text(
        getattr(account, "subscription_type", None),
        auth_claims.get("chatgpt_plan_type") if isinstance(auth_claims, dict) else None,
    ) or "free"
    now_ts = int(datetime.now(timezone.utc).timestamp())
    exp_ts = access_claims.get("exp") if isinstance(access_claims.get("exp"), int) else now_ts + 86400
    iat_ts = access_claims.get("iat") if isinstance(access_claims.get("iat"), int) else now_ts
    sub_value = _pick_first_text(
        access_claims.get("sub"),
        user_id,
        email,
    ) or email

    payload = {
        "iss": _pick_first_text(access_claims.get("iss"), "https://auth.openai.com"),
        "aud": access_claims.get("aud") if access_claims.get("aud") is not None else "https://api.openai.com/v1",
        "email": email,
        "email_verified": bool(
            profile_claims.get("email_verified") if isinstance(profile_claims, dict) else True
        ),
        "iat": iat_ts,
        "exp": exp_ts,
        "sub": sub_value,
        "https://api.openai.com/auth": {
            "chatgpt_user_id": user_id,
            "chatgpt_plan_type": plan_type,
            "account_id": account_id,
            "organization_id": organization_id,
        },
        "synthetic": True,
        "synthetic_source": "codex-console-export",
    }
    header = {"alg": "none", "typ": "JWT"}
    signature_seed = f"{email}|{account_id or ''}|{organization_id or ''}|{iat_ts}"
    signature = hashlib.md5(signature_seed.encode("utf-8")).hexdigest()
    return f"{_jwt_b64url_encode(header)}.{_jwt_b64url_encode(payload)}.{signature}"


def _datetime_to_unix_timestamp(
    value: Optional[Any], fallback: Optional[Any] = None
) -> int:
    candidate = value or fallback or datetime.now(timezone.utc)
    if isinstance(candidate, (int, float)):
        return int(candidate)
    if isinstance(candidate, str):
        parsed = _parse_iso_datetime(candidate)
        if parsed is not None:
            candidate = parsed
        else:
            candidate = datetime.now(timezone.utc)
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    else:
        candidate = candidate.astimezone(timezone.utc)
    return int(candidate.timestamp())


def _normalize_email_service_config_for_oauth_backfill(
    service_type: EmailServiceType,
    config: Optional[dict],
    proxy_url: Optional[str] = None,
) -> dict:
    normalized = dict(config or {})

    if "api_url" in normalized and "base_url" not in normalized:
        normalized["base_url"] = normalized.pop("api_url")

    if service_type in (EmailServiceType.MOE_MAIL, EmailServiceType.YYDS_MAIL, EmailServiceType.DUCK_MAIL):
        if "domain" in normalized and "default_domain" not in normalized:
            normalized["default_domain"] = normalized.pop("domain")
    elif service_type in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL):
        if "default_domain" in normalized and "domain" not in normalized:
            normalized["domain"] = normalized.pop("default_domain")
    elif service_type == EmailServiceType.LUCKMAIL:
        if "domain" in normalized and "preferred_domain" not in normalized:
            normalized["preferred_domain"] = normalized.pop("domain")

    if (
        proxy_url
        and "proxy_url" not in normalized
        and service_type not in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL)
    ):
        normalized["proxy_url"] = proxy_url

    return normalized


def _resolve_email_service_for_oauth_backfill(db, account: Account, proxy: Optional[str]):
    raw_type = str(account.email_service or "").strip().lower()
    if not raw_type:
        raise RuntimeError("账号缺少 email_service")
    try:
        service_type = EmailServiceType(raw_type)
    except Exception as exc:
        raise RuntimeError(f"不支持的邮箱服务类型: {raw_type}") from exc

    settings = get_settings()
    services = (
        db.query(EmailServiceModel)
        .filter(EmailServiceModel.service_type == service_type.value, EmailServiceModel.enabled == True)
        .order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc())
        .all()
    )

    selected = None
    if services:
        if service_type in (EmailServiceType.OUTLOOK, EmailServiceType.IMAP_MAIL):
            email_lower = str(account.email or "").strip().lower()
            for svc in services:
                cfg_email = str((svc.config or {}).get("email") or "").strip().lower()
                if cfg_email and cfg_email == email_lower:
                    selected = svc
                    break
        if not selected:
            selected = services[0]

    if selected and selected.config:
        config = _normalize_email_service_config_for_oauth_backfill(service_type, selected.config, proxy)
    elif service_type == EmailServiceType.TEMPMAIL:
        config = {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
            "proxy_url": proxy,
        }
    elif service_type == EmailServiceType.YYDS_MAIL:
        api_key = settings.yyds_mail_api_key.get_secret_value() if settings.yyds_mail_api_key else ""
        if not settings.yyds_mail_enabled or not api_key:
            raise RuntimeError("YYDS Mail 未启用或缺少 API Key，无法补齐登录验证码")
        config = {
            "base_url": settings.yyds_mail_base_url,
            "api_key": api_key,
            "default_domain": settings.yyds_mail_default_domain,
            "timeout": settings.yyds_mail_timeout,
            "max_retries": settings.yyds_mail_max_retries,
            "proxy_url": proxy,
        }
    else:
        raise RuntimeError(f"未找到可用邮箱服务配置(type={service_type.value})，无法自动获取登录验证码")

    return EmailServiceFactory.create(service_type, config, name=f"oauth_backfill_{service_type.value}")


def _backfill_real_oauth_tokens_for_account(
    db,
    account: Account,
    proxy: Optional[str] = None,
) -> tuple[bool, str]:
    if str(account.id_token or "").strip() and str(account.refresh_token or "").strip():
        return True, "already_complete"

    email = str(account.email or "").strip()
    password = str(account.password or "").strip()
    if not email or not password:
        return False, "账号缺少邮箱或密码，无法重新登录补齐 OAuth tokens"

    try:
        email_service = _resolve_email_service_for_oauth_backfill(db, account, proxy)
    except Exception as exc:
        logger.warning(
            "补齐 OAuth Tokens 无法创建邮箱服务: account_id=%s email=%s error=%s",
            account.id,
            account.email,
            exc,
        )
        return False, str(exc)

    engine = RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy,
        callback_logger=lambda msg: logger.info("OAuth Tokens 补齐: %s", msg),
        task_uuid=None,
    )
    engine.email = email
    engine.password = password
    engine.email_info = {"service_id": account.email_service_id} if account.email_service_id else {}
    engine._is_existing_account = True

    try:
        did, sen_token = engine._prepare_authorize_flow("补齐 OAuth Tokens")
        if not did:
            return False, "获取 Device ID 失败"

        login_start = engine._submit_login_start(did, sen_token)
        if not login_start.success:
            return False, login_start.error_message or "提交登录入口失败"

        if login_start.page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            password_result = engine._submit_login_password()
            if not password_result.success or not password_result.is_existing_account:
                return False, password_result.error_message or "提交密码后未进入登录验证码页"
        elif login_start.page_type != OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            return False, f"未进入登录验证码页: {login_start.page_type or 'unknown'}"

        result = RegistrationResult(
            success=False,
            email=email,
            password=password,
            access_token=str(account.access_token or "").strip(),
            refresh_token=str(account.refresh_token or "").strip(),
            id_token=str(account.id_token or "").strip(),
            session_token=str(account.session_token or "").strip(),
            account_id=str(account.account_id or "").strip(),
            workspace_id=str(account.workspace_id or "").strip(),
            logs=engine.logs,
            device_id=_resolve_account_device_id(account),
            source="login",
        )
        if not engine._complete_token_exchange(result, require_login_otp=True):
            otp_continue = str(getattr(engine, "_last_validate_otp_continue_url", "") or "").strip().lower()
            if "auth.openai.com/add-phone" in otp_continue:
                return False, "OTP 校验后进入 add-phone 风控页，无法补齐真实 OAuth tokens"
            return False, result.error_message or "登录后补齐 OAuth tokens 失败"

        if not str(result.id_token or "").strip():
            otp_continue = str(getattr(engine, "_last_validate_otp_continue_url", "") or "").strip().lower()
            if "auth.openai.com/add-phone" in otp_continue:
                return False, "OTP 校验后进入 add-phone 风控页，无法补齐真实 OAuth tokens"
            return False, "未获取到真实 id_token"
        if not str(result.refresh_token or "").strip():
            return False, "未获取到真实 refresh_token"

        account.access_token = str(result.access_token or account.access_token or "").strip() or None
        account.refresh_token = str(result.refresh_token or account.refresh_token or "").strip() or None
        account.id_token = str(result.id_token or account.id_token or "").strip() or None
        account.session_token = str(result.session_token or account.session_token or "").strip() or None
        account.account_id = str(result.account_id or account.account_id or "").strip() or None
        account.workspace_id = str(result.workspace_id or account.workspace_id or "").strip() or None
        account.client_id = str(get_settings().openai_client_id or account.client_id or "").strip() or None
        fresh_cookies = str(engine._dump_session_cookies() or "").strip()
        if fresh_cookies:
            account.cookies = fresh_cookies
        account.last_refresh = utcnow_naive()
        db.commit()
        db.refresh(account)
        return True, "ok"
    except Exception as exc:
        logger.warning(
            "补齐 OAuth Tokens 异常: account_id=%s email=%s error=%s",
            account.id,
            account.email,
            exc,
        )
        db.rollback()
        return False, str(exc)


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


def _build_cockpit_tokens(account: Any, include_account_hint: bool = False) -> Dict[str, Any]:
    id_token = _normalize_optional_text(getattr(account, "id_token", None))
    if id_token is None:
        id_token = _build_synthetic_id_token(account)
    tokens: Dict[str, Any] = {
        "id_token": id_token,
        "access_token": str(getattr(account, "access_token", "") or "").strip(),
    }
    refresh_token = _normalize_optional_text(getattr(account, "refresh_token", None))
    if refresh_token is not None:
        tokens["refresh_token"] = refresh_token
    if include_account_hint:
        account_id = _normalize_optional_text(getattr(account, "account_id", None))
        if account_id is not None:
            tokens["account_id"] = account_id
    return tokens


def _build_cockpit_account_export(account: Any) -> Dict[str, Any]:
    access_claims = _decode_jwt_payload_unverified(getattr(account, "access_token", None))
    id_claims = _decode_jwt_payload_unverified(getattr(account, "id_token", None))
    auth_paths = (
        ["https://api.openai.com/auth", "chatgpt_user_id"],
        ["https://api.openai.com/auth", "user_id"],
    )

    def _pick_claim(*paths: List[str]) -> Optional[str]:
        values: List[Any] = []
        for path in paths:
            values.append(_get_nested(id_claims, path))
            values.append(_get_nested(access_claims, path))
        return _pick_first_text(*values)

    email = _pick_first_text(
        getattr(account, "email", None),
        id_claims.get("email"),
        access_claims.get("email"),
    ) or f"account-{getattr(account, 'id', 'unknown')}@unknown.local"
    plan_raw = _pick_first_text(
        getattr(account, "subscription_type", None),
        _get_nested(id_claims, ["https://api.openai.com/auth", "chatgpt_plan_type"]),
        _get_nested(access_claims, ["https://api.openai.com/auth", "chatgpt_plan_type"]),
    )
    account_id = _pick_first_text(
        getattr(account, "account_id", None),
        _get_nested(id_claims, ["https://api.openai.com/auth", "account_id"]),
        _get_nested(id_claims, ["https://api.openai.com/auth", "chatgpt_account_id"]),
        _get_nested(access_claims, ["https://api.openai.com/auth", "account_id"]),
        _get_nested(access_claims, ["https://api.openai.com/auth", "chatgpt_account_id"]),
    )
    organization_id = _pick_first_text(
        getattr(account, "workspace_id", None),
        getattr(account, "organization_id", None),
        _get_nested(id_claims, ["https://api.openai.com/auth", "organization_id"]),
        _get_nested(access_claims, ["https://api.openai.com/auth", "organization_id"]),
    )
    created_at = getattr(account, "created_at", None) or getattr(account, "registered_at", None)
    last_used = (
        getattr(account, "last_used_at", None)
        or getattr(account, "last_refresh", None)
        or getattr(account, "updated_at", None)
        or created_at
    )
    extra_data = getattr(account, "extra_data", None)
    tags = extra_data.get("tags") if isinstance(extra_data, dict) else None
    cockpit_id = account_id or organization_id or f"codex-console-{getattr(account, 'id', 'unknown')}"
    tokens = _build_cockpit_tokens(account)
    payload: Dict[str, Any] = {
        "id": cockpit_id,
        "email": email,
        "auth_mode": "oauth",
        "user_id": _pick_claim(*auth_paths),
        "plan_type": _normalize_plan_type(plan_raw) if plan_raw else None,
        "account_id": account_id,
        "organization_id": organization_id,
        "tokens": tokens,
        "tags": tags if isinstance(tags, list) else None,
        "created_at": _datetime_to_unix_timestamp(created_at),
        "last_used": _datetime_to_unix_timestamp(last_used, fallback=created_at),
        # Flat token mirrors make the export consumable by tools that only read top-level fields.
        "id_token": tokens["id_token"],
        "access_token": tokens["access_token"],
    }
    refresh_token = tokens.get("refresh_token")
    if refresh_token is not None:
        payload["refresh_token"] = refresh_token
    return payload


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
                account.subscription_at = utcnow_naive() if detected_sub else None
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
    except AccountDeactivatedError as exc:
        logger.warning("账号被停用: email=%s err=%s", account.email, exc)
        account.status = AccountStatus.BANNED.value
        merged_extra = dict(extra_data)
        merged_extra[OVERVIEW_EXTRA_DATA_KEY] = _fallback_overview(
            account, error_message="account_deactivated", stale=True
        )
        merged_extra["account_deactivated_at"] = datetime.now(timezone.utc).isoformat()
        account.extra_data = merged_extra
        updated = True
        return merged_extra[OVERVIEW_EXTRA_DATA_KEY], updated
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
            )
            if subscription_type:
                account.subscription_type = subscription_type
                account.subscription_at = utcnow_naive()
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
                        "extra_data": metadata,
                        "last_refresh": utcnow_naive(),
                    }
                    clean_update_payload = {k: v for k, v in update_payload.items() if v is not None}
                    account = crud.update_account(db, exists.id, **clean_update_payload)
                    if account is None:
                        raise RuntimeError("更新账号失败")
                    account.subscription_type = subscription_type
                    account.subscription_at = utcnow_naive() if subscription_type else None
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
                )
                if subscription_type:
                    account.subscription_type = subscription_type
                    account.subscription_at = utcnow_naive()
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

        accounts = [
            account
            for account in query.order_by(Account.created_at.desc()).all()
            if not _is_overview_card_removed(account)
        ]
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

        accounts = query.order_by(Account.created_at.desc()).all()
        rows = []
        for account in accounts:
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

        accounts = query.order_by(Account.created_at.desc()).all()
        rows = []
        for account in accounts:
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
async def refresh_accounts_overview(request: OverviewRefreshRequest):
    """
    批量刷新账号总览数据。
    """
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

        for account_id in ids:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                result["failed_count"] += 1
                result["details"].append({"id": account_id, "success": False, "error": "账号不存在"})
                logger.warning("账号总览刷新失败: account_id=%s error=账号不存在", account_id)
                continue
            if (not _is_paid_subscription(account.subscription_type)) or _is_overview_card_removed(account):
                result["details"].append(
                    {
                        "id": account.id,
                        "email": account.email,
                        "success": False,
                        "error": "账号不在 Codex 卡片范围内，已跳过",
                    }
                )
                continue

            account_proxy = (account.proxy_used or "").strip() or proxy
            overview, updated = _get_account_overview_data(
                db,
                account,
                force_refresh=request.force,
                proxy=account_proxy,
                allow_network=True,
            )
            if updated:
                db.commit()

            if overview.get("hourly_quota", {}).get("status") == "unknown" and overview.get("weekly_quota", {}).get("status") == "unknown":
                result["failed_count"] += 1
                result["details"].append(
                    {
                        "id": account.id,
                        "email": account.email,
                        "success": False,
                        "error": overview.get("error") or "未获取到配额数据",
                    }
                )
                logger.warning(
                    "账号总览刷新失败: account_id=%s email=%s error=%s",
                    account.id,
                    account.email,
                    overview.get("error") or "未获取到配额数据",
                )
            else:
                result["success_count"] += 1
                result["details"].append(
                    {
                        "id": account.id,
                        "email": account.email,
                        "success": True,
                        "plan_type": overview.get("plan_type"),
                    }
                )
                logger.info(
                    "账号总览刷新成功: account_id=%s email=%s plan=%s hourly=%s weekly=%s code_review=%s hourly_source=%s weekly_source=%s",
                    account.id,
                    account.email,
                    overview.get("plan_type") or "-",
                    overview.get("hourly_quota", {}).get("percentage"),
                    overview.get("weekly_quota", {}).get("percentage"),
                    overview.get("code_review_quota", {}).get("percentage"),
                    overview.get("hourly_quota", {}).get("source"),
                    overview.get("weekly_quota", {}).get("source"),
                )

        logger.info(
            "账号总览刷新完成: success=%s failed=%s",
            result["success_count"],
            result["failed_count"],
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
            account.last_refresh = utcnow_naive()
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


@router.post("/{account_id}/oauth/manual/start")
async def start_manual_oauth_for_account(account_id: int, request: ManualOAuthStartRequest, http_request: Request):
    """为指定账号启动浏览器手动 OAuth 授权流程"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="璐﹀彿涓嶅瓨鍦?")

        proxy = _get_proxy(request.proxy)
        try:
            _ensure_manual_oauth_listener()
            oauth_manager = _build_oauth_manager(proxy=proxy)
            oauth_start = oauth_manager.start_oauth()
            session_data = _store_manual_oauth_session(account_id, oauth_start, proxy=proxy)
            browser_worker_result = {"started": False, "error": None}
            browser_mode = "external"
            if bool(request.use_desktop_automation):
                browser_mode = "desktop"
                _update_manual_oauth_session(account_id, browser_mode=browser_mode, browser_status="queued")
                browser_worker_result = _start_manual_oauth_desktop_worker(
                    account_id=account_id,
                    auth_url=session_data["auth_url"],
                    redirect_uri=session_data["redirect_uri"],
                    proxy=proxy,
                    headless=bool(request.headless),
                    launch_browser=not bool(request.use_current_browser_window),
                )
                if not browser_worker_result.get("started"):
                    browser_mode = "external"
                    _update_manual_oauth_session(
                        account_id,
                        browser_mode="external",
                        browser_status="failed",
                        browser_error=browser_worker_result.get("error"),
                    )
            elif bool(request.use_edge_attach):
                browser_mode = "edge_cdp"
                _update_manual_oauth_session(account_id, browser_mode=browser_mode, browser_status="queued")
                browser_worker_result = _start_manual_oauth_edge_attach_worker(
                    account_id=account_id,
                    auth_url=session_data["auth_url"],
                    redirect_uri=session_data["redirect_uri"],
                    proxy=proxy,
                    headless=bool(request.headless),
                )
                if not browser_worker_result.get("started"):
                    browser_mode = "external"
                    _update_manual_oauth_session(
                        account_id,
                        browser_mode="external",
                        browser_status="failed",
                        browser_error=browser_worker_result.get("error"),
                    )
            elif bool(request.use_playwright):
                browser_mode = "playwright"
                _update_manual_oauth_session(account_id, browser_mode=browser_mode, browser_status="queued")
                browser_worker_result = _start_manual_oauth_playwright_worker(
                    account_id=account_id,
                    auth_url=session_data["auth_url"],
                    redirect_uri=session_data["redirect_uri"],
                    proxy=proxy,
                    headless=bool(request.headless),
                )
                if not browser_worker_result.get("started"):
                    browser_mode = "external"
                    _update_manual_oauth_session(
                        account_id,
                        browser_mode="external",
                        browser_status="failed",
                        browser_error=browser_worker_result.get("error"),
                    )
            else:
                _update_manual_oauth_session(
                    account_id,
                    browser_mode="external",
                    browser_status="awaiting_browser",
                    browser_error=None,
                )
            expires_at = session_data["created_at"] + timedelta(seconds=_MANUAL_OAUTH_SESSION_TTL_SECONDS)
            return {
                "success": True,
                "account_id": account.id,
                "email": account.email,
                "auth_url": session_data["auth_url"],
                "redirect_uri": session_data["redirect_uri"],
                "expires_at": expires_at.isoformat(),
                "use_desktop_automation": bool(request.use_desktop_automation),
                "use_current_browser_window": bool(request.use_current_browser_window),
                "use_edge_attach": bool(request.use_edge_attach),
                "playwright_requested": bool(request.use_playwright),
                "browser_mode": browser_mode,
                "browser_worker_started": bool(browser_worker_result.get("started")),
                "browser_worker_error": browser_worker_result.get("error"),
                "playwright_started": bool(browser_worker_result.get("started")) if browser_mode in {"playwright", "edge_cdp"} else False,
                "playwright_error": browser_worker_result.get("error"),
            }
        except Exception as exc:
            logger.warning(
                "启动手动 OAuth 失败: account_id=%s email=%s error=%s",
                account.id,
                account.email,
                exc,
            )
            raise HTTPException(status_code=500, detail=f"启动手动 OAuth 失败: {exc}")


@router.get("/{account_id}/oauth/manual/status")
async def get_manual_oauth_status(account_id: int):
    """鏌ヨ鎵嬪姩 OAuth 浼氳瘽鐘舵€?"""
    session_data = _get_manual_oauth_session(account_id)
    if not session_data:
        return {
            "success": False,
            "status": "missing",
            "last_error": "OAuth session not found or expired",
            "result": None,
        }

    created_at = session_data.get("created_at")
    completed_at = session_data.get("completed_at")
    browser_started_at = session_data.get("browser_started_at")
    browser_closed_at = session_data.get("browser_closed_at")
    return {
        "success": True,
        "status": str(session_data.get("status") or "pending"),
        "last_error": session_data.get("last_error"),
        "result": session_data.get("result"),
        "redirect_uri": session_data.get("redirect_uri"),
        "browser_mode": session_data.get("browser_mode"),
        "browser_status": session_data.get("browser_status"),
        "browser_error": session_data.get("browser_error"),
        "browser_current_url": session_data.get("browser_current_url"),
        "browser_binary": session_data.get("browser_binary"),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
        "completed_at": completed_at.isoformat() if isinstance(completed_at, datetime) else None,
        "browser_started_at": browser_started_at.isoformat() if isinstance(browser_started_at, datetime) else None,
        "browser_closed_at": browser_closed_at.isoformat() if isinstance(browser_closed_at, datetime) else None,
    }


@router.get("/{account_id}/oauth/manual/callback", response_class=HTMLResponse, name="manual_oauth_callback_landing")
async def manual_oauth_callback_landing(account_id: int, http_request: Request):
    """娴忚鍣ㄦ巿鏉冨悗鐨勮嚜鍔ㄥ洖璋冮〉"""
    callback_url = str(http_request.url)
    try:
        payload = _complete_manual_oauth_session(account_id, callback_url)
        title = "OAuth Completed"
        status_line = "OAuth tokens saved. You can return to the console."
        detail = f"Account: {payload.get('email') or ''}"
        status_value = "completed"
    except LookupError:
        _update_manual_oauth_session(
            account_id,
            status="failed",
            last_error="Account not found",
            completed_at=datetime.now(timezone.utc),
        )
        title = "OAuth Failed"
        status_line = "The target account no longer exists."
        detail = "Return to the console and restart Browser OAuth Repair."
        status_value = "failed"
    except Exception as exc:
        _update_manual_oauth_session(
            account_id,
            status="failed",
            last_error=str(exc),
            completed_at=datetime.now(timezone.utc),
        )
        title = "OAuth Failed"
        status_line = "Automatic callback processing failed."
        detail = str(exc)
        status_value = "failed"

    safe_title = html.escape(title)
    safe_status = html.escape(status_line)
    safe_detail = html.escape(detail)
    badge_bg = "#e8f7ee" if status_value == "completed" else "#fff1f0"
    badge_fg = "#0f7b3b" if status_value == "completed" else "#c0392b"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7fb; color: #172033; margin: 0; }}
    .wrap {{ max-width: 560px; margin: 10vh auto; background: white; border-radius: 16px; padding: 24px; box-shadow: 0 18px 50px rgba(18, 38, 63, 0.12); }}
    .badge {{ display: inline-block; padding: 6px 10px; border-radius: 999px; background: {badge_bg}; color: {badge_fg}; font-size: 12px; font-weight: 600; }}
    h1 {{ font-size: 24px; margin: 14px 0 10px; }}
    p {{ line-height: 1.6; margin: 8px 0; word-break: break-word; }}
    button {{ margin-top: 16px; padding: 10px 16px; border: 0; border-radius: 10px; background: #1663ff; color: white; cursor: pointer; }}
  </style>
</head>
<body>
  <div class="wrap">
    <span class="badge">{html.escape(status_value.upper())}</span>
    <h1>{safe_title}</h1>
    <p>{safe_status}</p>
    <p>{safe_detail}</p>
    <button type="button" onclick="window.close()">Close Window</button>
  </div>
  <script>
    try {{
      if (window.opener && !window.opener.closed) {{
        window.opener.postMessage({{ type: 'codex-manual-oauth', accountId: {account_id}, status: '{status_value}' }}, window.location.origin);
      }}
    }} catch (e) {{}}
    if ('{status_value}' === 'completed') {{
      setTimeout(() => {{
        try {{
          window.close();
        }} catch (e) {{}}
      }}, 900);
    }}
  </script>
</body>
</html>"""
    )


@router.post("/{account_id}/oauth/manual/complete")
async def complete_manual_oauth_for_account(account_id: int, request: ManualOAuthCallbackRequest):
    """提交浏览器 OAuth 回调地址，并将真实 OAuth tokens 回填到账号"""
    callback_url = str(request.callback_url or "").strip()
    if not callback_url:
        raise HTTPException(status_code=400, detail="callback_url 不可为空")
    try:
        payload = _complete_manual_oauth_session(account_id, callback_url)
        return {
            "success": True,
            "message": "真实 OAuth tokens 已回填到账号",
            **payload,
        }
    except LookupError:
        _update_manual_oauth_session(
            account_id,
            status="failed",
            last_error="Account not found",
            completed_at=datetime.now(timezone.utc),
        )
        raise HTTPException(status_code=404, detail="账号不存在")
    except ValueError as exc:
        _update_manual_oauth_session(
            account_id,
            status="failed",
            last_error=str(exc),
            completed_at=datetime.now(timezone.utc),
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        _update_manual_oauth_session(
            account_id,
            status="failed",
            last_error=str(exc),
            completed_at=datetime.now(timezone.utc),
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _update_manual_oauth_session(
            account_id,
            status="failed",
            last_error=str(exc),
            completed_at=datetime.now(timezone.utc),
        )
        logger.warning("提交手动 OAuth 回调失败: account_id=%s error=%s", account_id, exc)
        raise HTTPException(status_code=500, detail=f"提交 OAuth 回调失败: {exc}")

    session_data = _get_manual_oauth_session(account_id)
    if not session_data:
        raise HTTPException(status_code=400, detail="当前账号没有可用的 OAuth 会话，需重新点击“浏览器 OAuth 补齐”")

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="璐﹀彿涓嶅瓨鍦?")

        try:
            oauth_manager = _build_oauth_manager(proxy=session_data.get("proxy"))
            token_info = oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=str(session_data.get("state") or ""),
                code_verifier=str(session_data.get("code_verifier") or ""),
            )
            persisted = _save_manual_oauth_tokens_to_account(db, account, token_info)
            _clear_manual_oauth_session(account_id)
            return {
                "success": True,
                "message": "真实 OAuth tokens 已回填到账号",
                "account": persisted,
                "has_refresh_token": bool(account.refresh_token),
                "has_id_token": bool(account.id_token),
            }
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            db.rollback()
            logger.warning(
                "提交手动 OAuth 回调失败: account_id=%s email=%s error=%s",
                account.id,
                account.email,
                exc,
            )
            raise HTTPException(status_code=500, detail=f"提交 OAuth 回调失败: {exc}")


@router.patch("/{account_id}", response_model=AccountResponse)
async def update_account(account_id: int, request: AccountUpdateRequest):
    """更新账号状态"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        update_data = {}
        if request.status:
            if request.status not in [e.value for e in AccountStatus]:
                raise HTTPException(status_code=400, detail="无效的状态值")
            update_data["status"] = request.status

        if request.metadata:
            current_metadata = account.metadata or {}
            current_metadata.update(request.metadata)
            update_data["metadata"] = current_metadata

        if request.cookies is not None:
            # 留空则清空，非空则更新
            update_data["cookies"] = request.cookies or None

        if request.session_token is not None:
            # 留空则清空，非空则更新
            update_data["session_token"] = request.session_token or None
            update_data["last_refresh"] = utcnow_naive()

        account = crud.update_account(db, account_id, **update_data)
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

        crud.delete_account(db, account_id)
        return {"success": True, "message": f"账号 {account.email} 已删除"}


@router.post("/batch-delete")
async def batch_delete_accounts(request: BatchDeleteRequest):
    """批量删除账号"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        deleted_count = 0
        errors = []

        for account_id in ids:
            try:
                account = crud.get_account_by_id(db, account_id)
                if account:
                    crud.delete_account(db, account_id)
                    deleted_count += 1
            except Exception as e:
                errors.append(f"ID {account_id}: {str(e)}")

        return {
            "success": True,
            "deleted_count": deleted_count,
            "errors": errors if errors else None
        }


@router.post("/batch-update")
async def batch_update_accounts(request: BatchUpdateRequest):
    """批量更新账号状态"""
    if request.status not in [e.value for e in AccountStatus]:
        raise HTTPException(status_code=400, detail="无效的状态值")

    with get_db() as db:
        updated_count = 0
        errors = []

        for account_id in request.ids:
            try:
                account = crud.get_account_by_id(db, account_id)
                if account:
                    crud.update_account(db, account_id, status=request.status)
                    updated_count += 1
            except Exception as e:
                errors.append(f"ID {account_id}: {str(e)}")

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


class ManualOAuthStartRequest(BaseModel):
    """手动浏览器 OAuth 启动请求"""
    proxy: Optional[str] = None


class ManualOAuthCallbackRequest(BaseModel):
    """手动浏览器 OAuth 回调提交请求"""
    callback_url: str


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
            "Email Service", "Status", "Registered At", "Last Refresh", "Expires At"
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
    """????? Codex ???????"""
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


@router.post("/export/cockpit")
async def export_accounts_cockpit(request: BatchExportRequest):
    """导出账号为 Cockpit Tools 兼容 JSON 数组。"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()
        unresolved: List[str] = []
        exportable_accounts: List[Account] = []
        runtime_proxy = _get_proxy()

        for acc in accounts:
            has_real_id = bool(str(acc.id_token or "").strip())
            has_refresh = bool(str(acc.refresh_token or "").strip())
            if has_real_id and has_refresh:
                exportable_accounts.append(acc)
                continue

            ok, message = _backfill_real_oauth_tokens_for_account(db, acc, proxy=runtime_proxy)
            if not ok:
                unresolved.append(f"{acc.email}: {message}")
                continue
            exportable_accounts.append(acc)

        if not exportable_accounts:
            raise HTTPException(
                status_code=400,
                detail=(
                    "以下账号仍缺少可用于 Cockpit/官方 Codex 登录的真实 OAuth tokens，请先补齐后再导出: "
                    + " | ".join(unresolved)
                ),
            )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_data = [_build_cockpit_account_export(acc) for acc in exportable_accounts]
        content = json.dumps(export_data, ensure_ascii=False, indent=2)

        if len(exportable_accounts) == 1:
            filename = f"{exportable_accounts[0].email}_cockpit.json"
        else:
            filename = f"cockpit_codex_accounts_{timestamp}.json"

        headers = {"Content-Disposition": f"attachment; filename={filename}"}
        if unresolved:
            warning_prefix = f"已跳过 {len(unresolved)} 个账号，仅导出 {len(exportable_accounts)} 个可用账号: "
            warning_body = " | ".join(unresolved[:5])
            if len(unresolved) > 5:
                warning_body += f" | 还有 {len(unresolved) - 5} 个账号未导出"
            headers["X-Export-Warning"] = warning_prefix + warning_body

        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers=headers
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

        return {
            "total": total,
            "by_status": {status: count for status, count in status_stats},
            "by_email_service": {service: count for service, count in service_stats}
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


@router.post("/batch-refresh")
async def batch_refresh_tokens(request: BatchRefreshRequest, background_tasks: BackgroundTasks):
    """批量刷新账号 Token"""
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

    for account_id in ids:
        try:
            result = do_refresh(account_id, proxy)
            if result.success:
                results["success_count"] += 1
            else:
                results["failed_count"] += 1
                results["errors"].append({"id": account_id, "error": result.error_message})
        except Exception as e:
            results["failed_count"] += 1
            results["errors"].append({"id": account_id, "error": str(e)})

    return results


@router.post("/{account_id}/refresh")
async def refresh_account_token(account_id: int, request: Optional[TokenRefreshRequest] = Body(default=None)):
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


def _run_batch_validate_tokens(request: BatchValidateRequest) -> Dict[str, Any]:
    """Run token validation synchronously so it can be reused by schedulers."""
    proxy = _get_proxy(request.proxy)

    results = {
        "valid_count": 0,
        "invalid_count": 0,
        "details": []
    }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    for account_id in ids:
        try:
            is_valid, error = do_validate(account_id, proxy)
            results["details"].append({
                "id": account_id,
                "valid": is_valid,
                "error": error
            })
            if is_valid:
                results["valid_count"] += 1
            else:
                results["invalid_count"] += 1
        except Exception as e:
            # 异常账号兜底打标 failed，保证前端“失败”筛选可见。
            try:
                with get_db() as db:
                    account = crud.get_account_by_id(db, account_id)
                    if account and account.status != AccountStatus.FAILED.value:
                        crud.update_account(db, account_id, status=AccountStatus.FAILED.value)
            except Exception:
                pass
            results["invalid_count"] += 1
            results["details"].append({
                "id": account_id,
                "valid": False,
                "error": str(e)
            })

    return results


@router.post("/batch-validate")
async def batch_validate_tokens(request: BatchValidateRequest):
    """批量验证账号 Token 有效性"""
    return _run_batch_validate_tokens(request)


def run_quick_refresh_workflow(source: str = "manual") -> Dict[str, Any]:
    if not _QUICK_REFRESH_WORKFLOW_LOCK.acquire(blocking=False):
        raise RuntimeError("quick_refresh_workflow_busy")

    started_at = utcnow_naive()
    try:
        candidate_ids = _get_quick_refresh_candidate_ids()
        proxy = _get_proxy()

        validate_summary: Dict[str, Any] = {
            "total": len(candidate_ids),
            "valid_count": 0,
            "invalid_count": 0,
            "details": [],
        }
        subscription_summary: Dict[str, Any] = {
            "total": 0,
            "success_count": 0,
            "failed_count": 0,
            "details": [],
        }

        if candidate_ids:
            validate_result = _run_batch_validate_tokens(
                BatchValidateRequest(ids=candidate_ids, proxy=proxy, select_all=False)
            )
            validate_summary.update(validate_result or {})
            validate_summary["total"] = len(candidate_ids)

            valid_ids = [
                int(detail.get("id"))
                for detail in (validate_result or {}).get("details", [])
                if detail.get("valid") and detail.get("id") is not None
            ]

            if valid_ids:
                from . import payment as payment_routes

                subscription_result = payment_routes.batch_check_subscription(
                    payment_routes.BatchCheckSubscriptionRequest(
                        ids=valid_ids,
                        proxy=proxy,
                        select_all=False,
                    )
                )
                subscription_summary.update(subscription_result or {})
                subscription_summary["total"] = len(valid_ids)

        finished_at = utcnow_naive()
        duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        return {
            "source": str(source or "manual"),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": duration_ms,
            "candidate_count": len(candidate_ids),
            "proxy_used": proxy,
            "validate": validate_summary,
            "subscription": subscription_summary,
        }
    finally:
        _QUICK_REFRESH_WORKFLOW_LOCK.release()


@router.post("/{account_id}/validate")
async def validate_account_token(account_id: int, request: Optional[TokenValidateRequest] = Body(default=None)):
    """验证单个账号的 Token 有效性"""
    proxy = _get_proxy(request.proxy if request else None)
    is_valid, error = do_validate(account_id, proxy)

    return {
        "id": account_id,
        "valid": is_valid,
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
            account.cpa_uploaded_at = utcnow_naive()
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
    if request.service_id:
        with get_db() as db:
            svc = crud.get_sub2api_service_by_id(db, request.service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 Sub2API 服务不存在")
            api_url = svc.api_url
            api_key = svc.api_key
    else:
        with get_db() as db:
            svcs = crud.get_sub2api_services(db, enabled=True)
            if svcs:
                api_url = svcs[0].api_url
                api_key = svcs[0].api_key

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
    if service_id:
        with get_db() as db:
            svc = crud.get_sub2api_service_by_id(db, service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 Sub2API 服务不存在")
            api_url = svc.api_url
            api_key = svc.api_key
    else:
        with get_db() as db:
            svcs = crud.get_sub2api_services(db, enabled=True)
            if svcs:
                api_url = svcs[0].api_url
                api_key = svcs[0].api_key

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
            target_type="sub2api"
        )
        if success:
            return {"success": True, "message": message}
        else:
            return {"success": False, "error": message}


class NewApiUploadRequest(BaseModel):
    """单账号 new-api 上传请求"""
    service_id: Optional[int] = None


class BatchNewApiUploadRequest(BaseModel):
    """批量 new-api 上传请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    service_id: Optional[int] = None


@router.post("/batch-upload-new-api")
async def batch_upload_accounts_to_new_api(request: BatchNewApiUploadRequest):
    """批量上传账号到 new-api。"""
    with get_db() as db:
        if request.service_id:
            service = crud.get_new_api_service_by_id(db, request.service_id)
        else:
            services = crud.get_new_api_services(db, enabled=True)
            service = services[0] if services else None

        if not service:
            raise HTTPException(status_code=400, detail="未找到可用的 new-api 服务，请先在设置中配置")

        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    return batch_upload_to_new_api(
        ids,
        service.api_url,
        getattr(service, 'username', None),
        getattr(service, 'password', None),
    )


@router.post("/{account_id}/upload-new-api")
async def upload_account_to_new_api(account_id: int, request: Optional[NewApiUploadRequest] = Body(default=None)):
    """上传单个账号到 new-api。"""
    service_id = request.service_id if request else None

    with get_db() as db:
        if service_id:
            service = crud.get_new_api_service_by_id(db, service_id)
        else:
            services = crud.get_new_api_services(db, enabled=True)
            service = services[0] if services else None

        if not service:
            raise HTTPException(status_code=400, detail="未找到可用的 new-api 服务，请先在设置中配置")

        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not account.access_token:
            return {"success": False, "error": "账号缺少 Token，无法上传"}

        success, message = upload_to_new_api(
            [account],
            service.api_url,
            getattr(service, 'username', None),
            getattr(service, 'password', None),
        )
        return {"success": success, "message": message if success else None, "error": None if success else message}


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

    if service_type == EST.YYDS_MAIL:
        settings = get_settings()
        return {
            "base_url": settings.yyds_mail_base_url,
            "api_key": settings.yyds_mail_api_key.get_secret_value() if settings.yyds_mail_api_key else "",
            "default_domain": settings.yyds_mail_default_domain,
            "timeout": settings.yyds_mail_timeout,
            "max_retries": settings.yyds_mail_max_retries,
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
        EST.LUCKMAIL: "luckmail",
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
