"""
team API
参考 team-manage-main 的兑换/邀请流程，先提供可用的最小落地版本。
"""

import base64
import copy
import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, func

from ...config.constants import (
    AccountStatus,
    PoolState,
    RoleTag,
    account_label_to_role_tag,
    normalize_account_label,
    normalize_pool_state,
    normalize_role_tag,
    role_tag_to_account_label,
)
from ...config.settings import get_settings
from ...core.circuit_breaker import allow_request as breaker_allow_request
from ...core.circuit_breaker import record_failure as breaker_record_failure
from ...core.circuit_breaker import record_success as breaker_record_success
from ...core.dynamic_proxy import get_proxy_url_for_task
from ...core.openai.token_refresh import refresh_account_token as do_refresh
from ...database import crud
from ...database.models import Account, TeamInviteRecord, EmailService as EmailServiceModel
from ...database.session import get_db
from ...services import EmailServiceFactory, EmailServiceType as ServiceEmailType

logger = logging.getLogger(__name__)
router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INVITER_POOL_SETTING_KEY = "auto_team_inviter_pool_ids"
BLOCKED_ACCOUNT_STATUSES = {
    AccountStatus.FAILED.value,
    AccountStatus.BANNED.value,
}
MANAGER_ROLE_KEYWORDS = (
    "owner",
    "admin",
    "administrator",
    "manager",
    "billing_admin",
    "billing-owner",
    "team_admin",
)

INVITE_LOCK_HOURS = 24
INVITE_JOINED_LOCK_HOURS = 24 * 7
INVITE_LOCK_STATES = {"pending", "invited", "joined"}
MANAGER_HEALTH_SETTING_KEY = "auto_team_manager_health_v1"
TEAM_POOL_FALLBACK_SETTING_KEY = "auto_team.pull_by_tag_fallback_to_none"
MANAGER_CONCURRENCY_LIMIT = 1
MANAGER_BASE_COOLDOWN_SECONDS = 1.2
MANAGER_AUTH_FREEZE_TRIGGER = 2
MANAGER_AUTH_FREEZE_MINUTES_BASE = 10
MANAGER_AUTH_FREEZE_MINUTES_MAX = 30
MANAGER_FAIL_BLOCK_TRIGGER = 3
TEAM_MANAGER_VERIFY_CACHE_TTL_SECONDS = 600
TEAM_INVITER_CACHE_TTL_SECONDS = 300
TEAM_TEAM_ACCOUNTS_CACHE_TTL_SECONDS = 180
TEAM_CONSOLE_CACHE_TTL_SECONDS = 180
TEAM_MANAGER_VERIFY_TIMEOUT_SECONDS = 4
TEAM_MANAGER_VERIFY_MAX_WORKERS = 4
TEAM_MANAGER_VERIFY_MAX_PER_CALL = 8
TEAM_CONSOLE_ROW_MAX_WORKERS = 4
TEAM_CONSOLE_FETCH_TIMEOUT_SECONDS = 12
TEAM_MANAGER_MAIL_FALLBACK_CACHE_TTL_SECONDS = 600
TEAM_MANAGER_MAIL_FALLBACK_LOOKBACK_HOURS = 72
TEAM_MANAGER_MAIL_FALLBACK_MAX_ROWS = 120
TEAM_CLASSIFY_CACHE_TTL_SECONDS = 25
TEAM_CLASSIFY_INCREMENTAL_MAX_ROWS = 120

_INVITER_SEMAPHORES: Dict[int, asyncio.Semaphore] = {}
_MANAGER_VERIFY_CACHE: Dict[int, Dict[str, Any]] = {}
_MANAGER_MAIL_FALLBACK_CACHE: Dict[int, Dict[str, Any]] = {}
_INVITER_CACHE: Dict[str, Any] = {"expires_at": None, "normal": [], "frozen": []}
_TEAM_ACCOUNTS_CACHE: Dict[str, Any] = {"expires_at": None, "payload": None}
_TEAM_CONSOLE_CACHE: Dict[str, Any] = {"expires_at": None, "payload": None}
_TEAM_CLASSIFY_CACHE: Dict[str, Any] = {
    "expires_at": None,
    "payload": None,
    "marker": {"team_count": 0, "max_updated_at": None},
}


class AutoTeamPreviewRequest(BaseModel):
    target_email: str
    inviter_account_id: Optional[int] = None


class AutoTeamInviteRequest(BaseModel):
    target_email: str
    inviter_account_id: Optional[int] = None
    proxy: Optional[str] = None


class TeamMemberInviteRequest(BaseModel):
    email: str
    proxy: Optional[str] = None


class TeamMemberRevokeRequest(BaseModel):
    email: str
    proxy: Optional[str] = None


class TeamMemberRemoveRequest(BaseModel):
    user_id: str
    proxy: Optional[str] = None


class TeamInviterPoolAddRequest(BaseModel):
    account_ids: List[int]


class TargetPoolConfigRequest(BaseModel):
    fallback_to_none: bool = False


def _get_proxy(request_proxy: Optional[str] = None) -> Optional[str]:
    """获取代理 URL：优先请求参数，其次代理池/动态代理/静态配置。"""
    if request_proxy:
        return request_proxy
    with get_db() as db:
        proxy = crud.get_random_proxy(db)
        if proxy:
            return proxy.proxy_url
    dynamic_proxy = get_proxy_url_for_task()
    if dynamic_proxy:
        return dynamic_proxy
    return get_settings().proxy_url


def _utc_now() -> datetime:
    return datetime.utcnow()


def _is_cache_alive(expires_at: Optional[datetime]) -> bool:
    return bool(expires_at and expires_at > _utc_now())


def _invalidate_team_runtime_caches() -> None:
    _INVITER_CACHE["expires_at"] = None
    _INVITER_CACHE["normal"] = []
    _INVITER_CACHE["frozen"] = []
    _TEAM_ACCOUNTS_CACHE["expires_at"] = None
    _TEAM_ACCOUNTS_CACHE["payload"] = None
    _TEAM_CONSOLE_CACHE["expires_at"] = None
    _TEAM_CONSOLE_CACHE["payload"] = None
    _TEAM_CLASSIFY_CACHE["expires_at"] = None
    _TEAM_CLASSIFY_CACHE["payload"] = None
    _TEAM_CLASSIFY_CACHE["marker"] = {"team_count": 0, "max_updated_at": None}


def _safe_decode_jwt_payload(token: Optional[str]) -> Dict[str, Any]:
    raw = str(token or "").strip()
    if not raw:
        return {}
    try:
        parts = raw.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode((payload + padding).encode("utf-8"))
        data = json.loads(decoded.decode("utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_plan(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "free"
    if "team" in text or "enterprise" in text:
        return "team"
    if "plus" in text:
        return "plus"
    if "pro" in text:
        return "pro"
    if "basic" in text or "free" in text:
        return "free"
    return text


def _normalize_role_text(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _is_manager_role(role_text: Optional[str]) -> bool:
    role = _normalize_role_text(role_text)
    if not role:
        return False
    return any(keyword in role for keyword in MANAGER_ROLE_KEYWORDS)


def _get_cached_manager_verify(account_id: int) -> Optional[Tuple[bool, str]]:
    entry = _MANAGER_VERIFY_CACHE.get(int(account_id))
    if not isinstance(entry, dict):
        return None
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, datetime) or not _is_cache_alive(expires_at):
        _MANAGER_VERIFY_CACHE.pop(int(account_id), None)
        return None
    return bool(entry.get("verified")), str(entry.get("source") or "cache")


def _set_cached_manager_verify(account_id: int, verified: bool, source: str) -> None:
    _MANAGER_VERIFY_CACHE[int(account_id)] = {
        "verified": bool(verified),
        "source": str(source or ""),
        "expires_at": _utc_now() + timedelta(seconds=TEAM_MANAGER_VERIFY_CACHE_TTL_SECONDS),
    }


def _cached_verify_needs_realtime(source: str) -> bool:
    """
    对“兜底保留/鉴权失败”来源的缓存结果不直接复用，避免 401 账号长期残留。
    """
    source_lower = str(source or "").strip().lower()
    if not source_lower:
        return True
    if "history_fallback" in source_lower or "stale_fallback" in source_lower:
        return True
    if "hard_remove_auth" in source_lower:
        return True
    if "http_401" in source_lower or "http_403" in source_lower:
        return True
    if _is_token_invalidated_error(source_lower):
        return True
    return False


def _get_cached_manager_mail_fallback(account_id: int) -> Optional[Tuple[bool, str]]:
    entry = _MANAGER_MAIL_FALLBACK_CACHE.get(int(account_id))
    if not isinstance(entry, dict):
        return None
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, datetime) or not _is_cache_alive(expires_at):
        _MANAGER_MAIL_FALLBACK_CACHE.pop(int(account_id), None)
        return None
    return bool(entry.get("blocked")), str(entry.get("source") or "mail_cache")


def _set_cached_manager_mail_fallback(account_id: int, blocked: bool, source: str) -> None:
    _MANAGER_MAIL_FALLBACK_CACHE[int(account_id)] = {
        "blocked": bool(blocked),
        "source": str(source or ""),
        "expires_at": _utc_now() + timedelta(seconds=TEAM_MANAGER_MAIL_FALLBACK_CACHE_TTL_SECONDS),
    }


def _is_auth_source_for_mail_fallback(source: str) -> bool:
    text = str(source or "").strip().lower()
    if not text:
        return False
    markers = (
        "http_401",
        "http_403",
        "token invalidated",
        "invalid token",
        "token expired",
        "authentication token has been invalidated",
        "please try signing in again",
        "after_refresh",
    )
    return any(marker in text for marker in markers)


def _normalize_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10**12:
            ts = ts / 1000.0
        if ts <= 0:
            return None
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _resolve_temp_mail_config_for_account(db, account: Account) -> Optional[Dict[str, Any]]:
    if str(account.email_service or "").strip().lower() != ServiceEmailType.TEMP_MAIL.value:
        return None

    services = (
        db.query(EmailServiceModel)
        .filter(
            EmailServiceModel.service_type == ServiceEmailType.TEMP_MAIL.value,
            EmailServiceModel.enabled == True,
        )
        .order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc())
        .all()
    )
    if not services:
        return None

    account_domain = ""
    try:
        account_domain = str(account.email or "").split("@", 1)[1].strip().lower()
    except Exception:
        account_domain = ""

    matched = None
    for svc in services:
        cfg = dict(svc.config or {})
        cfg_domain = str(cfg.get("domain") or cfg.get("default_domain") or "").strip().lower()
        if cfg_domain and account_domain and cfg_domain == account_domain:
            matched = svc
            break
    if matched is None:
        matched = services[0]

    cfg = dict((matched.config or {}))
    if "api_url" in cfg and "base_url" not in cfg:
        cfg["base_url"] = cfg.pop("api_url")
    if not cfg.get("base_url") or not cfg.get("admin_password"):
        return None
    return cfg


def _is_openai_deactivated_mail(sender: str, subject: str, body: str) -> bool:
    blob = "\n".join([str(sender or ""), str(subject or ""), str(body or "")]).lower()
    if "openai" not in blob and "tm1.openai.com" not in blob:
        return False
    markers = (
        "access deactivated",
        "deactivating your access",
        "identified activity in chatgpt that is not permitted",
        "trustandsafety@tm1.openai.com",
        "initiate appeal",
    )
    return any(marker in blob for marker in markers)


def _scan_deactivation_mail_fallback(account: Account, *, force: bool = False) -> Tuple[bool, str]:
    """
    邮箱兜底：仅在网络鉴权失败时启用。
    当前优先支持 temp_mail（可直接读取 admin/mails）。
    """
    account_id = int(getattr(account, "id", 0) or 0)
    if account_id <= 0:
        return False, "mail_fallback_skip:no_account_id"

    if not force:
        cached = _get_cached_manager_mail_fallback(account_id)
        if cached is not None:
            return cached

    service_key = str(getattr(account, "email_service", "") or "").strip().lower()
    if service_key != ServiceEmailType.TEMP_MAIL.value:
        source = f"mail_fallback_skip:unsupported_service:{service_key or '-'}"
        _set_cached_manager_mail_fallback(account_id, False, source)
        return False, source

    target_email = str(getattr(account, "email", "") or "").strip().lower()
    if not target_email:
        source = "mail_fallback_skip:missing_email"
        _set_cached_manager_mail_fallback(account_id, False, source)
        return False, source

    try:
        with get_db() as db:
            cfg = _resolve_temp_mail_config_for_account(db, account)
        if not cfg:
            source = "mail_fallback_skip:config_missing"
            _set_cached_manager_mail_fallback(account_id, False, source)
            return False, source

        service = EmailServiceFactory.create(ServiceEmailType.TEMP_MAIL, cfg)
        rows = service.list_emails(limit=TEAM_MANAGER_MAIL_FALLBACK_MAX_ROWS, offset=0)
        if not isinstance(rows, list):
            rows = []

        cutoff = datetime.utcnow() - timedelta(hours=TEAM_MANAGER_MAIL_FALLBACK_LOOKBACK_HOURS)
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_email = str(row.get("email") or row.get("address") or "").strip().lower()
            if row_email and row_email != target_email:
                continue
            raw_data = row.get("raw_data")
            raw_dict = raw_data if isinstance(raw_data, dict) else {}
            sender = str(
                row.get("from")
                or row.get("source")
                or raw_dict.get("source")
                or raw_dict.get("from")
                or ""
            ).strip()
            subject = str(row.get("subject") or raw_dict.get("subject") or "").strip()
            body = str(
                raw_dict.get("text")
                or raw_dict.get("body")
                or raw_dict.get("content")
                or raw_dict.get("html")
                or raw_dict.get("raw")
                or ""
            )

            created_at = _normalize_iso_datetime(
                row.get("created_at")
                or row.get("createdAt")
                or raw_dict.get("created_at")
                or raw_dict.get("createdAt")
                or raw_dict.get("date")
            )
            if created_at and created_at < cutoff:
                continue

            if _is_openai_deactivated_mail(sender, subject, body):
                source = "mail_fallback:openai_access_deactivated"
                _set_cached_manager_mail_fallback(account_id, True, source)
                return True, source

        source = "mail_fallback:no_deactivated_signal"
        _set_cached_manager_mail_fallback(account_id, False, source)
        return False, source
    except Exception as exc:
        source = f"mail_fallback:error:{exc}"
        logger.warning(
            "team管理号邮箱兜底扫描失败: account=%s email=%s err=%s",
            account_id,
            target_email,
            exc,
        )
        _set_cached_manager_mail_fallback(account_id, False, source)
        return False, source


def _get_cached_inviter_accounts(include_frozen: bool) -> Optional[List[Dict[str, Any]]]:
    expires_at = _INVITER_CACHE.get("expires_at")
    if not isinstance(expires_at, datetime) or not _is_cache_alive(expires_at):
        return None
    key = "frozen" if include_frozen else "normal"
    rows = _INVITER_CACHE.get(key)
    if not isinstance(rows, list):
        return None
    return copy.deepcopy(rows)


def _set_cached_inviter_accounts(normal_rows: List[Dict[str, Any]], frozen_rows: List[Dict[str, Any]]) -> None:
    _INVITER_CACHE["normal"] = copy.deepcopy(normal_rows)
    _INVITER_CACHE["frozen"] = copy.deepcopy(frozen_rows)
    _INVITER_CACHE["expires_at"] = _utc_now() + timedelta(seconds=TEAM_INVITER_CACHE_TTL_SECONDS)


def _get_cached_payload(cache_bucket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    expires_at = cache_bucket.get("expires_at")
    payload = cache_bucket.get("payload")
    if not isinstance(expires_at, datetime) or not _is_cache_alive(expires_at):
        return None
    if not isinstance(payload, dict):
        return None
    return copy.deepcopy(payload)


def _set_cached_payload(cache_bucket: Dict[str, Any], payload: Dict[str, Any], ttl_seconds: int) -> None:
    cache_bucket["payload"] = copy.deepcopy(payload)
    cache_bucket["expires_at"] = _utc_now() + timedelta(seconds=max(1, int(ttl_seconds)))


def _infer_account_plan(account: Account) -> str:
    direct = _normalize_plan(getattr(account, "subscription_type", None))
    if direct != "free":
        return direct

    for token in (getattr(account, "access_token", None), getattr(account, "id_token", None)):
        payload = _safe_decode_jwt_payload(token)
        auth = payload.get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            plan = _normalize_plan(auth.get("chatgpt_plan_type"))
            if plan != "free":
                return plan
    return direct


def _resolve_workspace_id(account: Account) -> str:
    value = str(getattr(account, "account_id", "") or "").strip()
    if value:
        return value
    value = str(getattr(account, "workspace_id", "") or "").strip()
    if value:
        return value
    for token in (getattr(account, "access_token", None), getattr(account, "id_token", None)):
        payload = _safe_decode_jwt_payload(token)
        auth = payload.get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            account_id = str(auth.get("chatgpt_account_id") or "").strip()
            if account_id:
                return account_id

    extra = getattr(account, "extra_data", None)
    if isinstance(extra, dict):
        for key in ("workspace_id", "account_id", "chatgpt_account_id"):
            value = str(extra.get(key) or "").strip()
            if value:
                return value
    return ""


def _resolve_account_role_tag(account: Account) -> str:
    role_raw = str(getattr(account, "role_tag", "") or "").strip()
    if role_raw:
        return normalize_role_tag(role_raw)
    return account_label_to_role_tag(getattr(account, "account_label", None))


def _set_account_role_tag(account: Account, role_tag: str) -> str:
    normalized = normalize_role_tag(role_tag)
    account.role_tag = normalized
    account.account_label = role_tag_to_account_label(normalized)
    return normalized


def _resolve_account_manual_pool_state(account: Account) -> Optional[str]:
    text = str(getattr(account, "pool_state_manual", "") or "").strip()
    if not text:
        return None
    return normalize_pool_state(text)


def _resolve_account_pool_state(account: Account) -> str:
    return normalize_pool_state(getattr(account, "pool_state", None))


def _read_pull_fallback_to_none() -> bool:
    with get_db() as db:
        setting = crud.get_setting(db, TEAM_POOL_FALLBACK_SETTING_KEY)
    if not setting or setting.value is None:
        return False
    text = str(setting.value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _build_account_item(account: Account) -> Dict[str, Any]:
    plan = _infer_account_plan(account)
    workspace_id = _resolve_workspace_id(account)
    account_label = normalize_account_label(getattr(account, "account_label", None))
    role_tag = _resolve_account_role_tag(account)
    return {
        "id": account.id,
        "email": account.email,
        "status": account.status,
        "plan": plan,
        "account_label": account_label,
        "role_tag": role_tag,
        "biz_tag": str(getattr(account, "biz_tag", "") or "").strip() or None,
        "pool_state": _resolve_account_pool_state(account),
        "pool_state_manual": _resolve_account_manual_pool_state(account),
        "last_pool_sync_at": account.last_pool_sync_at.isoformat() if getattr(account, "last_pool_sync_at", None) else None,
        "priority": int(getattr(account, "priority", 50) or 50),
        "last_used_at": account.last_used_at.isoformat() if getattr(account, "last_used_at", None) else None,
        "workspace_id": workspace_id,
        "subscription_type": account.subscription_type,
        "last_refresh": account.last_refresh.isoformat() if account.last_refresh else None,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
    }


def _resolve_member_snapshot_from_extra(account: Account) -> Tuple[Optional[int], Optional[int]]:
    extra = getattr(account, "extra_data", None)
    if not isinstance(extra, dict):
        return None, None

    current_members: Optional[int] = None
    max_members: Optional[int] = None
    for key in (
        "team_current_members",
        "current_members",
        "total_current_members",
        "members_count",
        "num_members",
    ):
        if key not in extra:
            continue
        value = _safe_int(extra.get(key), -1)
        if value >= 0:
            current_members = value
            break

    for key in (
        "team_max_members",
        "max_members",
        "total_max_members",
        "seat_limit",
    ):
        if key not in extra:
            continue
        value = _safe_int(extra.get(key), -1)
        if value > 0:
            max_members = value
            break

    return current_members, max_members


def _get_cached_team_member_snapshot_map() -> Dict[int, Tuple[int, int]]:
    payload = _get_cached_payload(_TEAM_CONSOLE_CACHE)
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return {}

    result: Dict[int, Tuple[int, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        account_id = _to_int(row.get("id"), 0)
        if account_id <= 0:
            continue
        current_members = _to_int(row.get("current_members"), -1)
        max_members = _to_int(row.get("max_members"), 6)
        if current_members < 0:
            continue
        if max_members <= 0:
            max_members = 6
        result[account_id] = (current_members, max_members)
    return result


def _sync_team_member_snapshot_to_accounts(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return

    metrics_map: Dict[int, Tuple[int, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        account_id = _to_int(row.get("id"), 0)
        if account_id <= 0:
            continue
        current_members = _to_int(row.get("current_members"), -1)
        if current_members < 0:
            continue
        max_members = _to_int(row.get("max_members"), 6)
        if max_members <= 0:
            max_members = 6
        metrics_map[account_id] = (current_members, max_members)

    if not metrics_map:
        return

    now_iso = datetime.utcnow().isoformat()
    changed = 0
    with get_db() as db:
        account_rows = db.query(Account).filter(Account.id.in_(list(metrics_map.keys()))).all()
        for account in account_rows:
            snapshot = metrics_map.get(int(account.id))
            if not snapshot:
                continue
            current_members, max_members = snapshot
            extra = account.extra_data if isinstance(account.extra_data, dict) else {}
            old_current = _safe_int(extra.get("team_current_members"), -1)
            old_max = _safe_int(extra.get("team_max_members"), -1)
            if old_current == current_members and old_max == max_members:
                continue
            new_extra = dict(extra)
            new_extra["team_current_members"] = current_members
            new_extra["team_max_members"] = max_members
            new_extra["team_member_ratio"] = f"{current_members}/{max_members}"
            new_extra["team_metrics_updated_at"] = now_iso
            account.extra_data = new_extra
            changed += 1
        if changed > 0:
            db.commit()


def _team_classify_item_sort_key(item: Dict[str, Any]) -> Tuple[str, int]:
    updated_text = str(item.get("updated_at") or "")
    account_id = _safe_int(item.get("id"), 0)
    return updated_text, account_id


def _serialize_dt(value: Optional[datetime]) -> Optional[str]:
    if not isinstance(value, datetime):
        return None
    return value.isoformat()


def _audit_pool_state_change(
    *,
    account_id: int,
    account_email: str,
    from_state: str,
    to_state: str,
    reason: str,
    manual_state: Optional[str],
) -> None:
    try:
        with get_db() as db:
            crud.create_operation_audit_log(
                db,
                actor="system",
                action="account.pool_state_auto_sync",
                target_type="account",
                target_id=account_id,
                target_email=account_email,
                payload={
                    "from": from_state,
                    "to": to_state,
                    "reason": reason,
                    "manual_state": manual_state,
                },
            )
    except Exception:
        logger.debug("记录池状态变更审计日志失败: account_id=%s", account_id, exc_info=True)


def _query_team_classify_marker(db) -> Dict[str, Any]:
    max_updated_at, team_count = (
        db.query(func.max(Account.updated_at), func.count(Account.id))
        .filter(func.lower(func.coalesce(Account.subscription_type, "")) == "team")
        .first()
    )
    return {
        "team_count": int(team_count or 0),
        "max_updated_at": _serialize_dt(max_updated_at),
    }


def _is_same_team_marker(left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_count = int(left.get("team_count") or 0)
    right_count = int(right.get("team_count") or 0)
    left_dt = str(left.get("max_updated_at") or "")
    right_dt = str(right.get("max_updated_at") or "")
    return left_count == right_count and left_dt == right_dt


def _set_team_classify_cache(payload: Dict[str, List[Dict[str, Any]]], marker: Dict[str, Any]) -> None:
    _TEAM_CLASSIFY_CACHE["payload"] = copy.deepcopy(payload)
    _TEAM_CLASSIFY_CACHE["marker"] = {
        "team_count": int(marker.get("team_count") or 0),
        "max_updated_at": str(marker.get("max_updated_at") or "") or None,
    }
    _TEAM_CLASSIFY_CACHE["expires_at"] = _utc_now() + timedelta(seconds=TEAM_CLASSIFY_CACHE_TTL_SECONDS)


def _classify_team_account_row(
    account: Account,
    *,
    now: datetime,
    health_state: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    item = _build_account_item(account)
    if item["plan"] != "team":
        return None, None, 0

    row_changed = 0
    has_access_token = bool(str(account.access_token or "").strip())
    has_refresh_token = bool(str(account.refresh_token or "").strip())
    has_session_token = bool(str(account.session_token or "").strip())
    has_workspace = bool(str(item.get("workspace_id") or "").strip())
    can_auth = has_access_token or has_refresh_token or has_session_token
    role_tag = _resolve_account_role_tag(account)
    account_label = role_tag_to_account_label(role_tag)
    item["role_tag"] = role_tag
    item["account_label"] = account_label
    item["has_access_token"] = has_access_token
    item["has_refresh_token"] = has_refresh_token
    item["has_session_token"] = has_session_token
    item["manager_ready"] = bool(has_workspace and can_auth)

    # 兼容同步：role_tag 与 account_label 双写一致
    if str(getattr(account, "account_label", "") or "").strip().lower() != account_label:
        account.account_label = account_label
        row_changed += 1
    if str(getattr(account, "role_tag", "") or "").strip().lower() != role_tag:
        account.role_tag = role_tag
        row_changed += 1

    status_text = str(account.status or "").strip().lower()
    health_entry = _get_manager_health_entry(health_state, int(account.id))
    health_consecutive_fail = _safe_int(health_entry.get("consecutive_fail"), 0)
    health_frozen = _is_manager_frozen(health_entry, now)
    manual_pool_state = _resolve_account_manual_pool_state(account)
    old_pool_state = _resolve_account_pool_state(account)

    auto_pool_state = PoolState.CANDIDATE_POOL.value
    auto_reason = "candidate_default"
    if status_text in BLOCKED_ACCOUNT_STATUSES:
        auto_pool_state = PoolState.BLOCKED.value
        auto_reason = f"status_blocked:{status_text}"
    elif health_consecutive_fail >= MANAGER_FAIL_BLOCK_TRIGGER:
        auto_pool_state = PoolState.BLOCKED.value
        auto_reason = f"fuse_consecutive_fail:{health_consecutive_fail}"
    elif health_frozen:
        auto_pool_state = PoolState.BLOCKED.value
        auto_reason = "health_frozen"
    elif role_tag == RoleTag.PARENT.value and has_workspace and can_auth:
        auto_pool_state = PoolState.TEAM_POOL.value
        auto_reason = "parent_team_ready"

    effective_pool_state = manual_pool_state or auto_pool_state
    item["pool_state_auto"] = auto_pool_state
    item["pool_state_manual"] = manual_pool_state
    item["pool_state"] = effective_pool_state

    if health_consecutive_fail >= MANAGER_FAIL_BLOCK_TRIGGER and old_pool_state != PoolState.BLOCKED.value:
        logger.warning(
            "team管理号触发失败熔断: inviter=%s email=%s consecutive_fail=%s threshold=%s",
            account.id,
            account.email,
            health_consecutive_fail,
            MANAGER_FAIL_BLOCK_TRIGGER,
        )

    if old_pool_state != effective_pool_state:
        logger.info(
            "team账号池状态变更: account_id=%s email=%s from=%s to=%s reason=%s manual=%s",
            account.id,
            account.email,
            old_pool_state,
            effective_pool_state,
            auto_reason,
            manual_pool_state or "-",
        )
        account.pool_state = effective_pool_state
        row_changed += 1
        _audit_pool_state_change(
            account_id=int(account.id),
            account_email=str(account.email or ""),
            from_state=str(old_pool_state or ""),
            to_state=str(effective_pool_state or ""),
            reason=auto_reason,
            manual_state=manual_pool_state,
        )

    previous_sync_at = getattr(account, "last_pool_sync_at", None)
    if not previous_sync_at or (now - previous_sync_at).total_seconds() >= 20:
        account.last_pool_sync_at = now
        row_changed += 1
    item["last_pool_sync_at"] = (
        account.last_pool_sync_at.isoformat()
        if getattr(account, "last_pool_sync_at", None)
        else now.isoformat()
    )

    if effective_pool_state == PoolState.BLOCKED.value:
        item["team_identity"] = "blocked"
        return item, "member", row_changed
    if effective_pool_state == PoolState.TEAM_POOL.value and has_workspace and can_auth:
        item["team_identity"] = "manager"
        return item, "manager", row_changed
    item["team_identity"] = "member"
    return item, "member", row_changed


def _normalize_account_ids(raw: Any) -> List[int]:
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            values = []
        else:
            try:
                parsed = json.loads(text)
                values = parsed if isinstance(parsed, list) else []
            except Exception:
                values = [x.strip() for x in text.split(",") if x.strip()]
    else:
        values = []

    out: List[int] = []
    seen = set()
    for value in values:
        try:
            num = int(value)
        except Exception:
            continue
        if num <= 0 or num in seen:
            continue
        seen.add(num)
        out.append(num)
    return out


def _load_inviter_pool_ids() -> List[int]:
    with get_db() as db:
        setting = crud.get_setting(db, INVITER_POOL_SETTING_KEY)
        if not setting or not str(setting.value or "").strip():
            return []
        return _normalize_account_ids(setting.value)


def _save_inviter_pool_ids(account_ids: List[int]) -> List[int]:
    normalized = _normalize_account_ids(account_ids)
    with get_db() as db:
        crud.set_setting(
            db,
            key=INVITER_POOL_SETTING_KEY,
            value=json.dumps(normalized, ensure_ascii=False),
            description="team邀请手动入池账号ID列表",
            category="team",
        )
    return normalized


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _load_manager_health_state() -> Dict[str, Dict[str, Any]]:
    with get_db() as db:
        setting = crud.get_setting(db, MANAGER_HEALTH_SETTING_KEY)
        raw = str(getattr(setting, "value", "") or "").strip() if setting else ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def _save_manager_health_state(state: Dict[str, Dict[str, Any]]) -> None:
    with get_db() as db:
        crud.set_setting(
            db,
            key=MANAGER_HEALTH_SETTING_KEY,
            value=json.dumps(state, ensure_ascii=False),
            description="Team 邀请管理账号健康度与冻结状态",
            category="team",
        )


def _get_manager_health_entry(state: Dict[str, Dict[str, Any]], account_id: int) -> Dict[str, Any]:
    key = str(int(account_id))
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = {}
        state[key] = entry
    entry.setdefault("success_total", 0)
    entry.setdefault("fail_total", 0)
    entry.setdefault("consecutive_fail", 0)
    entry.setdefault("auth_fail_streak", 0)
    entry.setdefault("frozen_until", None)
    entry.setdefault("next_allowed_at", None)
    entry.setdefault("last_status", None)
    entry.setdefault("last_error", None)
    entry.setdefault("last_success_at", None)
    entry.setdefault("updated_at", None)
    return entry


def _is_manager_frozen(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    current = now or datetime.utcnow()
    frozen_until = _parse_dt(entry.get("frozen_until"))
    return bool(frozen_until and frozen_until > current)


def _manager_wait_seconds(entry: Dict[str, Any], now: Optional[datetime] = None) -> float:
    current = now or datetime.utcnow()
    next_allowed = _parse_dt(entry.get("next_allowed_at"))
    if not next_allowed:
        return 0.0
    remain = (next_allowed - current).total_seconds()
    return float(remain) if remain > 0 else 0.0


def _set_manager_next_allowed(entry: Dict[str, Any], seconds: float) -> None:
    cooldown = max(0.0, float(seconds or 0.0))
    entry["next_allowed_at"] = (datetime.utcnow() + timedelta(seconds=cooldown)).isoformat()


def _compute_manager_health_priority(row: Dict[str, Any], entry: Dict[str, Any]) -> int:
    success_total = _safe_int(entry.get("success_total"), 0)
    fail_total = _safe_int(entry.get("fail_total"), 0)
    consecutive_fail = _safe_int(entry.get("consecutive_fail"), 0)
    auth_fail_streak = _safe_int(entry.get("auth_fail_streak"), 0)
    is_active = str(row.get("status") or "").strip().lower() == AccountStatus.ACTIVE.value
    frozen_penalty = 1000 if _is_manager_frozen(entry) else 0
    base = (success_total * 3) - (fail_total * 2) - (consecutive_fail * 8) - (auth_fail_streak * 12)
    if not is_active:
        base -= 30
    return int(base - frozen_penalty)


def _annotate_manager_health(row: Dict[str, Any], entry: Dict[str, Any]) -> None:
    now = datetime.utcnow()
    success_total = _safe_int(entry.get("success_total"), 0)
    fail_total = _safe_int(entry.get("fail_total"), 0)
    attempts = max(0, success_total + fail_total)
    fail_rate = (float(fail_total) / float(attempts)) if attempts > 0 else 0.5
    row["health_success_total"] = success_total
    row["health_fail_total"] = fail_total
    row["health_total_attempts"] = attempts
    row["health_fail_rate"] = round(fail_rate, 4)
    row["health_consecutive_fail"] = _safe_int(entry.get("consecutive_fail"), 0)
    row["health_auth_fail_streak"] = _safe_int(entry.get("auth_fail_streak"), 0)
    row["health_frozen_until"] = entry.get("frozen_until")
    row["health_frozen"] = _is_manager_frozen(entry, now)
    row["health_next_allowed_at"] = entry.get("next_allowed_at")
    row["health_wait_seconds"] = _manager_wait_seconds(entry, now)
    row["health_priority"] = _compute_manager_health_priority(row, entry)


def _get_manager_cooldown_seconds(account_id: int) -> float:
    state = _load_manager_health_state()
    entry = _get_manager_health_entry(state, account_id)
    return _manager_wait_seconds(entry)


def _update_manager_health_after_invite(
    *,
    account_id: int,
    status_code: int,
    error_text: str,
    success: bool,
) -> Dict[str, Any]:
    state = _load_manager_health_state()
    entry = _get_manager_health_entry(state, account_id)
    now = datetime.utcnow()

    if success:
        entry["success_total"] = _safe_int(entry.get("success_total"), 0) + 1
        entry["consecutive_fail"] = 0
        entry["auth_fail_streak"] = 0
        entry["frozen_until"] = None
        entry["last_success_at"] = now.isoformat()
    else:
        entry["fail_total"] = _safe_int(entry.get("fail_total"), 0) + 1
        entry["consecutive_fail"] = _safe_int(entry.get("consecutive_fail"), 0) + 1
        if int(status_code) in (401, 403):
            auth_streak = _safe_int(entry.get("auth_fail_streak"), 0) + 1
            entry["auth_fail_streak"] = auth_streak
            if auth_streak >= MANAGER_AUTH_FREEZE_TRIGGER:
                freeze_minutes = min(
                    MANAGER_AUTH_FREEZE_MINUTES_MAX,
                    MANAGER_AUTH_FREEZE_MINUTES_BASE * (auth_streak - (MANAGER_AUTH_FREEZE_TRIGGER - 1)),
                )
                entry["frozen_until"] = (now + timedelta(minutes=freeze_minutes)).isoformat()
                logger.warning(
                    "team管理号进入冻结期: inviter=%s auth_fail_streak=%s freeze=%smin",
                    account_id,
                    auth_streak,
                    freeze_minutes,
                )
        elif int(status_code) != 429:
            entry["auth_fail_streak"] = 0

    if int(status_code) == 429:
        cooldown = min(20.0, 2.0 * max(1, _safe_int(entry.get("consecutive_fail"), 1)))
    else:
        cooldown = MANAGER_BASE_COOLDOWN_SECONDS
    _set_manager_next_allowed(entry, cooldown)

    entry["last_status"] = int(status_code)
    entry["last_error"] = str(error_text or "").strip()[:300] or None
    entry["updated_at"] = now.isoformat()
    _save_manager_health_state(state)

    # 失败熔断：同一管理号连续失败达到阈值，自动标记 blocked
    consecutive_fail = _safe_int(entry.get("consecutive_fail"), 0)
    if not success and consecutive_fail >= MANAGER_FAIL_BLOCK_TRIGGER:
        with get_db() as db:
            account = db.query(Account).filter(Account.id == int(account_id)).first()
            if account:
                old_pool_state = _resolve_account_pool_state(account)
                account.pool_state = PoolState.BLOCKED.value
                account.last_pool_sync_at = now
                db.commit()
                if old_pool_state != PoolState.BLOCKED.value:
                    logger.warning(
                        "team管理号自动熔断入 blocked 池: account_id=%s email=%s consecutive_fail=%s threshold=%s",
                        account.id,
                        account.email,
                        consecutive_fail,
                        MANAGER_FAIL_BLOCK_TRIGGER,
                    )
                    try:
                        crud.create_operation_audit_log(
                            db,
                            actor="system",
                            action="account.pool_state_fuse_block",
                            target_type="account",
                            target_id=account.id,
                            target_email=account.email,
                            payload={
                                "from": old_pool_state,
                                "to": PoolState.BLOCKED.value,
                                "consecutive_fail": consecutive_fail,
                                "threshold": MANAGER_FAIL_BLOCK_TRIGGER,
                                "last_error": entry.get("last_error"),
                            },
                        )
                    except Exception:
                        logger.debug("记录熔断审计日志失败: account_id=%s", account.id, exc_info=True)

    _invalidate_team_runtime_caches()
    return entry


def _get_inviter_semaphore(account_id: int) -> asyncio.Semaphore:
    key = int(account_id)
    sem = _INVITER_SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(MANAGER_CONCURRENCY_LIMIT)
        _INVITER_SEMAPHORES[key] = sem
    return sem


def _classify_team_accounts(force: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """
    Team 账号分类:
    - managers: 母号候选（满足基础可邀请条件）
    - members: 子号（Team 成员账号）
    """
    with get_db() as db:
        marker = _query_team_classify_marker(db)
        cache_payload = _TEAM_CLASSIFY_CACHE.get("payload")
        cache_marker = _TEAM_CLASSIFY_CACHE.get("marker")
        cache_expires_at = _TEAM_CLASSIFY_CACHE.get("expires_at")
        if (
            not force
            and isinstance(cache_payload, dict)
            and _is_cache_alive(cache_expires_at)
            and _is_same_team_marker(marker, cache_marker)
        ):
            return copy.deepcopy(cache_payload)

        now = datetime.utcnow()
        health_state = _load_manager_health_state()
        changed = 0
        managers: List[Dict[str, Any]] = []
        members: List[Dict[str, Any]] = []

        # 增量路径：仅处理自上次快照以来发生变更的账号，减少常规刷新成本。
        incremental_used = False
        old_marker_dt = _parse_dt((cache_marker or {}).get("max_updated_at")) if isinstance(cache_marker, dict) else None
        old_team_count = int((cache_marker or {}).get("team_count") or 0) if isinstance(cache_marker, dict) else 0
        new_team_count = int(marker.get("team_count") or 0)
        if (
            not force
            and isinstance(cache_payload, dict)
            and old_marker_dt
            and old_team_count == new_team_count
        ):
            changed_rows = (
                db.query(Account)
                .filter(Account.updated_at.isnot(None))
                .filter(Account.updated_at >= old_marker_dt)
                .order_by(desc(Account.updated_at), desc(Account.id))
                .all()
            )
            if changed_rows and len(changed_rows) <= TEAM_CLASSIFY_INCREMENTAL_MAX_ROWS:
                manager_map: Dict[int, Dict[str, Any]] = {
                    _safe_int(item.get("id"), 0): dict(item)
                    for item in (cache_payload.get("managers") or [])
                    if _safe_int(item.get("id"), 0) > 0
                }
                member_map: Dict[int, Dict[str, Any]] = {
                    _safe_int(item.get("id"), 0): dict(item)
                    for item in (cache_payload.get("members") or [])
                    if _safe_int(item.get("id"), 0) > 0
                }

                for account in changed_rows:
                    account_id = int(getattr(account, "id", 0) or 0)
                    if account_id <= 0:
                        continue
                    manager_map.pop(account_id, None)
                    member_map.pop(account_id, None)

                    item, bucket, row_changed = _classify_team_account_row(
                        account,
                        now=now,
                        health_state=health_state,
                    )
                    changed += int(row_changed or 0)
                    if not item or not bucket:
                        continue
                    if bucket == "manager":
                        manager_map[account_id] = item
                    else:
                        member_map[account_id] = item

                managers = sorted(manager_map.values(), key=_team_classify_item_sort_key, reverse=True)
                members = sorted(member_map.values(), key=_team_classify_item_sort_key, reverse=True)
                incremental_used = True

        if not incremental_used:
            rows = (
                db.query(Account)
                .order_by(desc(Account.updated_at), desc(Account.id))
                .all()
            )
            for account in rows:
                item, bucket, row_changed = _classify_team_account_row(
                    account,
                    now=now,
                    health_state=health_state,
                )
                changed += int(row_changed or 0)
                if not item or not bucket:
                    continue
                if bucket == "manager":
                    managers.append(item)
                else:
                    members.append(item)

        if changed > 0:
            db.commit()

        payload = {"managers": managers, "members": members}
        _set_team_classify_cache(payload, marker)
        if incremental_used:
            logger.info(
                "team分类增量刷新完成: managers=%s members=%s marker=%s",
                len(managers),
                len(members),
                marker,
            )
        return payload


def _list_team_inviter_candidates() -> List[Dict[str, Any]]:
    grouped = _classify_team_accounts()
    result: List[Dict[str, Any]] = []
    for item in grouped.get("managers", []):
        row = dict(item)
        row["in_pool"] = True
        result.append(row)
    return result


def _list_team_inviter_accounts_local(force: bool = False) -> List[Dict[str, Any]]:
    """
    本地快速入池（不依赖网络）：
    - plan=team
    - status=active（绿色）
    - role_tag=parent（母号标签）
    - current_members < 5（优先读取 team-console 缓存，其次账号本地快照）
    """
    _ = force
    cached_member_snapshots = _get_cached_team_member_snapshot_map()
    with get_db() as db:
        rows = (
            db.query(Account)
            .order_by(desc(Account.updated_at), desc(Account.id))
            .all()
        )

    local_rows: List[Dict[str, Any]] = []
    for account in rows:
        item = _build_account_item(account)
        if str(item.get("plan") or "").strip().lower() != "team":
            continue
        if str(item.get("status") or "").strip().lower() != AccountStatus.ACTIVE.value:
            continue
        if normalize_role_tag(item.get("role_tag")) != RoleTag.PARENT.value:
            continue

        account_id = int(item.get("id") or 0)
        current_members: Optional[int] = None
        max_members: Optional[int] = None
        if account_id > 0 and account_id in cached_member_snapshots:
            current_members, max_members = cached_member_snapshots[account_id]
        else:
            current_members, max_members = _resolve_member_snapshot_from_extra(account)

        if current_members is None:
            current_members = 0
        if max_members is None or int(max_members) <= 0:
            max_members = 6
        if int(current_members) >= 5:
            continue

        has_access_token = bool(str(account.access_token or "").strip())
        has_refresh_token = bool(str(account.refresh_token or "").strip())
        has_session_token = bool(str(account.session_token or "").strip())
        has_workspace = bool(str(item.get("workspace_id") or "").strip())
        can_auth = has_access_token or has_refresh_token or has_session_token

        item["has_access_token"] = has_access_token
        item["has_refresh_token"] = has_refresh_token
        item["has_session_token"] = has_session_token
        item["manager_ready"] = bool(has_workspace and can_auth)
        item["team_identity"] = "manager"
        item["manager_verified"] = True
        item["manager_verify_source"] = "local_label_status_plan"
        item["manager_verify_realtime"] = False
        item["pool_confirmed"] = True
        item["fallback_level"] = 0
        item["fallback_note"] = "local_no_network"
        item["current_members"] = int(current_members)
        item["max_members"] = int(max_members)
        item["member_ratio"] = f"{int(current_members)}/{int(max_members)}"
        local_rows.append(item)

    local_rows.sort(
        key=lambda x: (
            int(x.get("priority") or 50),
            -int(x.get("id") or 0),
        )
    )

    return copy.deepcopy(local_rows)


def _load_inviter_history_ids() -> set:
    """
    网络异常兜底：曾成功发起过邀请记录的账号可保留在管理列表中。
    """
    with get_db() as db:
        rows = (
            db.query(TeamInviteRecord.inviter_account_id)
            .filter(TeamInviteRecord.inviter_account_id.isnot(None))
            .filter(TeamInviteRecord.state.in_(["pending", "invited", "joined"]))
            .all()
        )

    ids = set()
    for row in rows:
        value = None
        try:
            value = row[0]
        except Exception:
            value = getattr(row, "inviter_account_id", None)
        try:
            num = int(value)
            if num > 0:
                ids.add(num)
        except Exception:
            continue
    return ids


def _list_team_inviter_accounts(include_frozen: bool = False, force: bool = False) -> List[Dict[str, Any]]:
    if not force:
        cached = _get_cached_inviter_accounts(include_frozen)
        if cached is not None:
            cached_ids = [int(item.get("id") or 0) for item in cached if int(item.get("id") or 0) > 0]
            if not cached_ids:
                return cached
            with get_db() as db:
                rows = db.query(Account.id, Account.status).filter(Account.id.in_(cached_ids)).all()
            blocked_ids = {
                int(getattr(row, "id", row[0]) or 0)
                for row in rows
                if str(getattr(row, "status", row[1]) or "").strip().lower() in BLOCKED_ACCOUNT_STATUSES
            }
            auth_failed_ids = set()
            for item in cached:
                item_id = int(item.get("id") or 0)
                if item_id <= 0:
                    continue
                source_lower = str(item.get("manager_verify_source") or "").strip().lower()
                if (
                    "hard_remove_auth" in source_lower
                    or "http_401" in source_lower
                    or "http_403" in source_lower
                    or _is_token_invalidated_error(source_lower)
                ):
                    auth_failed_ids.add(item_id)
            remove_ids = blocked_ids | auth_failed_ids
            if not remove_ids:
                return cached
            filtered = [item for item in cached if int(item.get("id") or 0) not in remove_ids]
            logger.info(
                "team管理号缓存命中剔除失效账号: removed=%s remain=%s",
                len(cached) - len(filtered),
                len(filtered),
            )
            return filtered

    # 自动入池策略：
    # 仅“可验证为管理角色(owner/admin/manager)”的 Team 账号才允许入池。
    # 网络抖动时，曾有邀请记录的账号可走历史兜底保留。
    strict_candidates = _list_team_inviter_candidates()
    health_state = _load_manager_health_state()
    stale_rows: List[Dict[str, Any]] = []
    if not force:
        stale_key = "frozen" if include_frozen else "normal"
        stale_raw = _INVITER_CACHE.get(stale_key)
        if isinstance(stale_raw, list):
            stale_rows = copy.deepcopy(stale_raw)
    stale_ids = {int(item.get("id") or 0) for item in stale_rows if int(item.get("id") or 0) > 0}
    candidate_ids = [int(item.get("id") or 0) for item in strict_candidates if int(item.get("id") or 0) > 0]
    account_map: Dict[int, Account] = {}
    if candidate_ids:
        with get_db() as db:
            account_rows = db.query(Account).filter(Account.id.in_(candidate_ids)).all()
            account_map = {int(a.id): a for a in account_rows if int(getattr(a, "id", 0) or 0) > 0}
    history_ids = _load_inviter_history_ids()
    proxy_url = _get_proxy()
    all_rows: List[Dict[str, Any]] = []
    verify_results: Dict[int, Tuple[bool, str, bool]] = {}
    status_updates: Dict[int, str] = {}

    to_verify_ids: List[int] = []
    for item in strict_candidates:
        account_id = int(item.get("id") or 0)
        if account_id <= 0:
            continue
        cached_verify = None if force else _get_cached_manager_verify(account_id)
        if cached_verify is not None:
            cached_source = str(cached_verify[1] or "unknown")
            if (not force) and _cached_verify_needs_realtime(cached_source):
                to_verify_ids.append(account_id)
            else:
                verify_results[account_id] = (bool(cached_verify[0]), f"{cached_source}|cache", False)
            continue
        to_verify_ids.append(account_id)

    if to_verify_ids:
        if (not force) and len(to_verify_ids) > TEAM_MANAGER_VERIFY_MAX_PER_CALL:
            logger.info(
                "team管理号快速校验限流: candidates=%s verify_now=%s defer=%s",
                len(to_verify_ids),
                TEAM_MANAGER_VERIFY_MAX_PER_CALL,
                len(to_verify_ids) - TEAM_MANAGER_VERIFY_MAX_PER_CALL,
            )
            to_verify_ids = to_verify_ids[:TEAM_MANAGER_VERIFY_MAX_PER_CALL]
        max_workers = min(max(1, TEAM_MANAGER_VERIFY_MAX_WORKERS), len(to_verify_ids))

        def _verify_one(verify_account_id: int) -> Tuple[int, bool, str]:
            account_obj = account_map.get(verify_account_id)
            if not account_obj:
                return verify_account_id, False, "account_missing"
            try:
                ok, source = _is_verified_team_manager(
                    account=account_obj,
                    proxy_url=proxy_url,
                    timeout_seconds=TEAM_MANAGER_VERIFY_TIMEOUT_SECONDS,
                )
                return verify_account_id, bool(ok), str(source or "unknown")
            except Exception as exc:
                return verify_account_id, False, f"verify_exception:{exc}"

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="team_mgr_verify") as pool:
            future_map = {pool.submit(_verify_one, aid): aid for aid in to_verify_ids}
            for future in as_completed(future_map):
                aid = int(future_map[future])
                try:
                    result_id, ok, source = future.result()
                except Exception as exc:
                    result_id, ok, source = aid, False, f"verify_exception:{exc}"
                verify_results[int(result_id)] = (bool(ok), str(source or "unknown"), True)

    for item in strict_candidates:
        row = dict(item)
        account_id = int(row.get("id") or 0)
        if account_id <= 0:
            continue

        manager_verified = False
        manager_source = "unverified"
        from_realtime = False
        account_obj = account_map.get(account_id)
        if account_id in verify_results:
            manager_verified, manager_source, from_realtime = verify_results[account_id]

        source_lower = str(manager_source or "").lower()
        auth_failed = ("http_401" in source_lower) or ("http_403" in source_lower) or _is_token_invalidated_error(source_lower)
        if (not manager_verified) and account_obj is not None and _is_auth_source_for_mail_fallback(manager_source):
            blocked_by_mail, mail_source = _scan_deactivation_mail_fallback(account_obj, force=force)
            if blocked_by_mail:
                auth_failed = True
                manager_source = f"{manager_source}|{mail_source}"
                source_lower = str(manager_source).lower()
        if auth_failed:
            manager_verified = False
            manager_source = f"{manager_source}|hard_remove_auth"

        # 网络波动兜底：
        # 1) 有历史邀请记录时保持在管理列表，避免页面突降为 0/1。
        if (not manager_verified) and (not auth_failed) and (account_id in history_ids):
            manager_verified = True
            manager_source = "history_fallback"
        # 2) 若该账号曾在旧缓存里出现，且当前失败非“明确非管理角色”，允许短暂沿用。
        source_lower = str(manager_source or "").lower()
        explicit_non_manager = source_lower.startswith("workspace_role:") and (not _is_manager_role(source_lower))
        soft_network_fail = (
            ("error" in source_lower)
            or ("http_401" in source_lower)
            or ("http_403" in source_lower)
            or ("http_429" in source_lower)
            or ("workspace_candidates_http_" in source_lower)
            or ("invites_probe_http_" in source_lower)
        )
        if (
            (not manager_verified)
            and (not auth_failed)
            and (account_id in stale_ids)
            and (not explicit_non_manager)
            and soft_network_fail
        ):
            manager_verified = True
            manager_source = "stale_fallback"

        if account_id in to_verify_ids:
            _set_cached_manager_verify(account_id, manager_verified, manager_source)

        if manager_verified:
            status_updates[account_id] = AccountStatus.ACTIVE.value
        elif auth_failed:
            status_updates[account_id] = AccountStatus.FAILED.value

        if not manager_verified:
            continue

        row["manager_verified"] = True
        row["manager_verify_source"] = manager_source
        row["manager_verify_realtime"] = bool(from_realtime)
        health_entry = _get_manager_health_entry(health_state, account_id)
        _annotate_manager_health(row, health_entry)
        row["pool_confirmed"] = True
        row["fallback_level"] = 0
        row["fallback_note"] = "auto_manager_pool"
        all_rows.append(row)

    all_rows.sort(
        key=lambda x: (
            str(x.get("status") or "") != AccountStatus.ACTIVE.value,
            bool(x.get("health_frozen")),
            0 if normalize_role_tag(x.get("role_tag")) == RoleTag.PARENT.value else 1,
            int(x.get("priority") or 50),
            _parse_dt(x.get("last_used_at")) is not None,
            _parse_dt(x.get("last_used_at")) or datetime.min,
            float(x.get("health_fail_rate") or 0.0),
            int(x.get("health_consecutive_fail") or 0),
            int(x.get("health_fail_total") or 0),
            -int(x.get("health_success_total") or 0),
            -int(x.get("health_priority") or 0),
            -int(x.get("id") or 0),
        )
    )
    normal_rows = [row for row in all_rows if not bool(row.get("health_frozen"))]

    if status_updates:
        changed = 0
        with get_db() as db:
            for aid, next_status in status_updates.items():
                row = db.query(Account).filter(Account.id == int(aid)).first()
                if not row:
                    continue
                curr = str(row.status or "").strip().lower()
                if curr == str(next_status or "").strip().lower():
                    continue
                row.status = str(next_status)
                changed += 1
            if changed > 0:
                db.commit()
        if changed > 0:
            logger.info("team管理号刷新状态同步完成: updated=%s", changed)

    _set_cached_inviter_accounts(normal_rows=normal_rows, frozen_rows=all_rows)
    return copy.deepcopy(all_rows if include_frozen else normal_rows)


def _normalize_email(email: Optional[str]) -> str:
    return str(email or "").strip().lower()


def _expire_stale_invite_records(db) -> int:
    now = datetime.utcnow()
    changed = 0
    rows = (
        db.query(TeamInviteRecord)
        .filter(TeamInviteRecord.state.in_(["pending", "invited"]))
        .all()
    )
    for row in rows:
        ref_time = row.updated_at or row.invited_at or row.created_at
        expired_by_time = bool(ref_time and (now - ref_time) > timedelta(hours=INVITE_LOCK_HOURS))
        expired_by_field = bool(row.expires_at and row.expires_at <= now)
        if expired_by_time or expired_by_field:
            row.state = "expired"
            if not str(row.last_error or "").strip():
                row.last_error = "auto_expired_by_ttl"
            row.expires_at = now
            changed += 1
    if changed:
        db.commit()
    return changed


def _get_locked_target_email_map(db) -> Dict[str, Dict[str, Any]]:
    now = datetime.utcnow()
    _expire_stale_invite_records(db)
    rows = (
        db.query(TeamInviteRecord)
        .filter(TeamInviteRecord.state.in_(list(INVITE_LOCK_STATES)))
        .order_by(desc(TeamInviteRecord.updated_at), desc(TeamInviteRecord.id))
        .all()
    )
    locked: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        email = _normalize_email(row.target_email)
        if not email or email in locked:
            continue
        # 邀请状态统一受 TTL 控制，避免永久锁死
        if row.state in ("pending", "invited", "joined"):
            ref_time = row.updated_at or row.invited_at or row.created_at
            if row.expires_at and row.expires_at <= now:
                continue
            lock_hours = INVITE_JOINED_LOCK_HOURS if row.state == "joined" else INVITE_LOCK_HOURS
            if ref_time and (now - ref_time) > timedelta(hours=lock_hours):
                continue
        locked[email] = {
            "state": str(row.state or "").strip().lower(),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "inviter_email": row.inviter_email,
        }
    return locked


def _upsert_invite_record(
    db,
    *,
    inviter_account: Optional[Account],
    target_email: str,
    workspace_id: Optional[str],
    state: str,
    last_error: Optional[str] = None,
    increment_attempt: bool = False,
) -> TeamInviteRecord:
    now = datetime.utcnow()
    normalized_target = _normalize_email(target_email)
    normalized_state = str(state or "").strip().lower() or "pending"
    inviter_email = str(getattr(inviter_account, "email", "") or "").strip() or None
    inviter_id = getattr(inviter_account, "id", None)
    workspace = str(workspace_id or "").strip() or None

    record = (
        db.query(TeamInviteRecord)
        .filter(func.lower(TeamInviteRecord.target_email) == normalized_target)
        .order_by(desc(TeamInviteRecord.updated_at), desc(TeamInviteRecord.id))
        .first()
    )

    if not record:
        record = TeamInviteRecord(
            inviter_account_id=inviter_id,
            inviter_email=inviter_email,
            target_email=normalized_target,
            workspace_id=workspace,
            state=normalized_state,
            invite_attempts=1,
            invited_at=now if normalized_state in ("pending", "invited", "joined") else None,
            accepted_at=now if normalized_state == "joined" else None,
            expires_at=(now + timedelta(hours=INVITE_LOCK_HOURS)) if normalized_state in ("pending", "invited") else None,
            last_error=str(last_error or "").strip() or None,
        )
        db.add(record)
        db.flush()
        return record

    if inviter_id:
        record.inviter_account_id = inviter_id
    if inviter_email:
        record.inviter_email = inviter_email
    if workspace:
        record.workspace_id = workspace
    record.target_email = normalized_target
    record.state = normalized_state
    record.last_error = str(last_error or "").strip() or None

    if increment_attempt:
        record.invite_attempts = int(record.invite_attempts or 0) + 1
    elif not record.invite_attempts:
        record.invite_attempts = 1

    if normalized_state in ("pending", "invited"):
        record.invited_at = now
        record.accepted_at = None
        record.expires_at = now + timedelta(hours=INVITE_LOCK_HOURS)
    elif normalized_state == "joined":
        record.accepted_at = now
        record.expires_at = now + timedelta(hours=INVITE_JOINED_LOCK_HOURS)
    elif normalized_state in ("failed", "expired"):
        record.expires_at = now

    db.flush()
    return record


def _list_target_email_accounts() -> List[Dict[str, Any]]:
    """
    目标邮箱候选账号（来自账号管理）:
    - 仅子号标签（child），未命中不回退无标签池
    - 仅 free
    - 排除红色状态 failed
    - 排除邀请状态池中的 pending/invited/joined（解决订阅状态异步更新窗口期重复入池）
    """
    fallback_to_none = _read_pull_fallback_to_none()
    with get_db() as db:
        locked_map = _get_locked_target_email_map(db)
        rows = (
            db.query(Account)
            .order_by(desc(Account.updated_at), desc(Account.id))
            .all()
        )

        child_rows: List[Dict[str, Any]] = []
        none_rows: List[Dict[str, Any]] = []
        for account in rows:
            if str(account.status or "").strip().lower() == AccountStatus.FAILED.value:
                continue
            email = str(account.email or "").strip()
            if not email:
                continue
            email_norm = _normalize_email(email)
            lock_info = locked_map.get(email_norm)
            if lock_info:
                continue

            role_tag = _resolve_account_role_tag(account)
            plan = _infer_account_plan(account)
            if plan != "free":
                continue

            row = {
                "id": account.id,
                "email": email,
                "status": account.status,
                "plan": plan,
                "account_label": role_tag_to_account_label(role_tag),
                "role_tag": role_tag,
                "biz_tag": str(getattr(account, "biz_tag", "") or "").strip() or None,
                "subscription_type": account.subscription_type,
                "invite_state": None,
                "updated_at": account.updated_at.isoformat() if account.updated_at else None,
            }
            if role_tag == RoleTag.CHILD.value:
                child_rows.append(row)
            elif role_tag == RoleTag.NONE.value:
                none_rows.append(row)

        if child_rows:
            return child_rows
        if fallback_to_none:
            return none_rows
        return []


def _find_selected_inviter(inviter_id: Optional[int]) -> Dict[str, Any]:
    candidates = _list_team_inviter_accounts()
    if not candidates:
        frozen_candidates = _list_team_inviter_accounts(include_frozen=True)
        if frozen_candidates:
            thaw_times = [
                _parse_dt(item.get("health_frozen_until"))
                for item in frozen_candidates
                if item.get("health_frozen")
            ]
            thaw_times = [x for x in thaw_times if x]
            earliest = min(thaw_times).strftime("%Y-%m-%d %H:%M:%S") if thaw_times else "稍后"
            raise HTTPException(
                status_code=429,
                detail=f"当前可用 Team 管理账号均处于冻结期，请稍后重试（最早解冻: {earliest} UTC）。",
            )
        raise HTTPException(
            status_code=404,
            detail="当前无可用 Team 管理账号（需 team + 管理角色(owner/admin/manager) + 可用 token/workspace）。",
        )

    if inviter_id is None:
        return candidates[0]

    for item in candidates:
        if item["id"] == inviter_id:
            return item
    frozen_candidates = _list_team_inviter_accounts(include_frozen=True)
    for item in frozen_candidates:
        if int(item.get("id") or 0) == int(inviter_id) and bool(item.get("health_frozen")):
            thaw = _parse_dt(item.get("health_frozen_until"))
            thaw_text = thaw.strftime("%Y-%m-%d %H:%M:%S") if thaw else "稍后"
            raise HTTPException(
                status_code=429,
                detail=f"指定管理账号当前处于冻结期，请稍后重试（预计解冻: {thaw_text} UTC）。",
            )
    raise HTTPException(status_code=404, detail=f"指定邀请账号不存在或不可用: {inviter_id}")


def _is_already_member_or_invited(error_text: str) -> bool:
    text = str(error_text or "").lower()
    return any(
        key in text
        for key in (
            "already in workspace",
            "already in team",
            "already a member",
            "already invited",
            "email already exists",
        )
    )


def _safe_json(response) -> Dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return default
        if "." in text:
            return int(float(text))
        return int(text)
    except Exception:
        return default


def _team_api_request(
    *,
    method: str,
    access_token: str,
    workspace_id: str,
    path: str,
    proxy_url: Optional[str],
    payload: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 35,
) -> Tuple[int, Dict[str, Any], str]:
    try:
        url = f"https://chatgpt.com/backend-api/accounts/{workspace_id}{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "chatgpt-account-id": workspace_id,
        }
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            headers["Content-Type"] = "application/json"

        session_kwargs: Dict[str, Any] = {
            "impersonate": "chrome120",
            "timeout": max(3, int(timeout_seconds)),
        }
        if proxy_url:
            session_kwargs["proxy"] = proxy_url
        session = cffi_requests.Session(**session_kwargs)

        method_up = method.upper()
        if method_up == "GET":
            response = session.get(url, headers=headers)
        elif method_up == "POST":
            response = session.post(url, headers=headers, json=payload or {})
        elif method_up == "DELETE":
            if payload:
                response = session.delete(url, headers=headers, json=payload)
            else:
                response = session.delete(url, headers=headers)
        else:
            raise ValueError(f"unsupported method: {method}")

        body = _safe_json(response)
        raw = ""
        if not body:
            try:
                raw = (response.text or "").strip()
            except Exception:
                raw = ""
        return response.status_code, body, raw
    except Exception as exc:
        logger.warning(
            "team_api_request exception: method=%s workspace=%s path=%s proxy=%s err=%s",
            method,
            workspace_id,
            path,
            "on" if proxy_url else "off",
            exc,
        )
        return 599, {}, str(exc)


def _send_team_invite_once(
    *,
    access_token: str,
    workspace_id: str,
    target_email: str,
    proxy_url: Optional[str],
) -> Tuple[int, Dict[str, Any], str]:
    url = f"https://chatgpt.com/backend-api/accounts/{workspace_id}/invites"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "chatgpt-account-id": workspace_id,
    }
    payload = {
        "email_addresses": [target_email],
        "role": "standard-user",
        "resend_emails": True,
    }

    session_kwargs: Dict[str, Any] = {
        "impersonate": "chrome120",
        "timeout": 35,
    }
    if proxy_url:
        session_kwargs["proxy"] = proxy_url
    session = cffi_requests.Session(**session_kwargs)
    response = session.post(url, headers=headers, json=payload)
    body = _safe_json(response)
    raw = ""
    if not body:
        try:
            raw = (response.text or "").strip()
        except Exception:
            raw = ""
    return response.status_code, body, raw


async def _send_team_invite_with_backoff(
    *,
    access_token: str,
    workspace_id: str,
    target_email: str,
    proxy_url: Optional[str],
    inviter_account_id: int,
    max_attempts: int = 3,
) -> Tuple[int, Dict[str, Any], str]:
    status_code = 0
    body: Dict[str, Any] = {}
    raw = ""
    for attempt in range(1, max_attempts + 1):
        status_code, body, raw = _send_team_invite_once(
            access_token=access_token,
            workspace_id=workspace_id,
            target_email=target_email,
            proxy_url=proxy_url,
        )
        if status_code != 429:
            return status_code, body, raw

        wait_seconds = min(18.0, float(2 ** attempt))
        logger.warning(
            "team邀请命中 429，自动退避重试: inviter=%s workspace=%s email=%s attempt=%s/%s wait=%.1fs",
            inviter_account_id,
            workspace_id,
            target_email,
            attempt,
            max_attempts,
            wait_seconds,
        )
        if attempt < max_attempts:
            await asyncio.sleep(wait_seconds)
    return status_code, body, raw


def _fetch_team_workspace_candidates(
    *,
    access_token: str,
    proxy_url: Optional[str],
    timeout_seconds: int = 35,
    return_meta: bool = False,
) -> List[Dict[str, Any]] | Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    从 accounts/check 拉取当前 token 可用的 Team workspace 账号。
    参考 team-manage-main 的账户检测逻辑。
    """
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    session_kwargs: Dict[str, Any] = {
        "impersonate": "chrome120",
        "timeout": max(3, int(timeout_seconds)),
    }
    if proxy_url:
        session_kwargs["proxy"] = proxy_url
    try:
        session = cffi_requests.Session(**session_kwargs)
        resp = session.get(url, headers=headers)
        status_code = int(resp.status_code or 0)
    except Exception as exc:
        logger.warning(
            "fetch team workspace candidates failed: proxy=%s err=%s",
            "on" if proxy_url else "off",
            exc,
        )
        if return_meta:
            return [], {"status_code": 599, "raw": str(exc)[:180]}
        return []
    if status_code != 200:
        if return_meta:
            raw = ""
            try:
                raw = str(resp.text or "").strip()[:180]
            except Exception:
                raw = ""
            return [], {"status_code": status_code, "raw": raw}
        return []

    try:
        payload = resp.json() or {}
    except Exception:
        if return_meta:
            return [], {"status_code": status_code, "raw": "invalid_json"}
        return []
    if not isinstance(payload, dict):
        if return_meta:
            return [], {"status_code": status_code, "raw": "invalid_payload"}
        return []

    accounts_data = payload.get("accounts") or {}
    if not isinstance(accounts_data, dict):
        if return_meta:
            return [], {"status_code": status_code, "raw": "accounts_missing"}
        return []

    candidates: List[Dict[str, Any]] = []
    for account_id, item in accounts_data.items():
        if not isinstance(item, dict):
            continue
        account = item.get("account") or {}
        entitlement = item.get("entitlement") or {}
        if not isinstance(account, dict):
            account = {}
        if not isinstance(entitlement, dict):
            entitlement = {}

        plan = _normalize_plan(
            account.get("plan_type")
            or entitlement.get("subscription_plan")
            or ""
        )
        if plan != "team":
            continue

        candidates.append(
            {
                "account_id": str(account_id or "").strip(),
                "name": str(account.get("name") or "").strip(),
                "is_default": bool(account.get("is_default")),
                "role": str(account.get("account_user_role") or "").strip(),
                "active": bool(entitlement.get("has_active_subscription")),
                "subscription_plan": str(
                    entitlement.get("subscription_plan")
                    or account.get("plan_type")
                    or ""
                ).strip(),
                "expires_at": str(entitlement.get("expires_at") or "").strip(),
                # accounts/check 在不同账号上字段名可能不同，这里做多键兼容
                "current_members": _to_int(
                    account.get("total_current_members")
                    or account.get("current_members")
                    or account.get("members_count")
                    or account.get("num_members"),
                    0,
                ),
                "max_members": _to_int(
                    account.get("total_max_members")
                    or account.get("max_members")
                    or account.get("seat_limit")
                    or 6,
                    6,
                ),
            }
        )

    # 排序：默认 + 活跃 + owner 优先
    candidates.sort(
        key=lambda x: (
            0 if x.get("is_default") else 1,
            0 if x.get("active") else 1,
            0 if str(x.get("role") or "").lower() == "owner" else 1,
        )
    )
    rows = [x for x in candidates if x.get("account_id")]
    if return_meta:
        return rows, {"status_code": status_code}
    return rows


def _pick_workspace_id(
    *,
    preferred_workspace_id: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not candidates:
        return preferred_workspace_id, None

    candidate_map = {str(item.get("account_id") or "").strip(): item for item in candidates}
    if preferred_workspace_id and preferred_workspace_id in candidate_map:
        return preferred_workspace_id, candidate_map[preferred_workspace_id]

    first = candidates[0]
    return str(first.get("account_id") or "").strip(), first


def _is_verified_team_manager(
    *,
    account: Account,
    proxy_url: Optional[str],
    timeout_seconds: int = TEAM_MANAGER_VERIFY_TIMEOUT_SECONDS,
) -> Tuple[bool, str]:
    """
    判定账号是否为 Team 管理号（可邀请）：
    1) 优先用 accounts/check 的 role（owner/admin/manager）
    2) role 缺失时降级探测 /invites 权限（200 视为可管理）
    """
    max_timeout = max(3, int(timeout_seconds or TEAM_MANAGER_VERIFY_TIMEOUT_SECONDS))

    def _probe_once(token: str, *, timeout: int) -> Tuple[bool, str, bool]:
        if not token:
            return False, "no_access_token", False

        preferred_workspace_id = _resolve_workspace_id(account)
        try:
            candidates_result = _fetch_team_workspace_candidates(
                access_token=token,
                proxy_url=proxy_url,
                timeout_seconds=timeout,
                return_meta=True,
            )
            candidates, meta = candidates_result  # type: ignore[misc]
        except Exception as exc:
            return False, f"workspace_candidates_error:{exc}", False

        status_code = int((meta or {}).get("status_code") or 0)
        raw_meta = str((meta or {}).get("raw") or "").strip()
        if not candidates:
            if status_code in (401, 403):
                return False, f"workspace_candidates_http_{status_code}", True
            if status_code and status_code != 200:
                return False, f"workspace_candidates_http_{status_code}:{raw_meta[:80]}", False
            return False, "workspace_candidates_empty", False

        workspace_id, selected = _pick_workspace_id(
            preferred_workspace_id=preferred_workspace_id,
            candidates=candidates,
        )
        if not workspace_id or not selected:
            return False, "workspace_not_selected", False

        role = _normalize_role_text((selected or {}).get("role"))
        if _is_manager_role(role):
            return True, f"workspace_role:{role}", False
        if role:
            return False, f"workspace_role:{role}", False

        try:
            probe_status, _body, raw = _team_api_request(
                method="GET",
                access_token=token,
                workspace_id=workspace_id,
                path="/invites",
                proxy_url=proxy_url,
                timeout_seconds=timeout,
            )
        except Exception as exc:
            return False, f"invites_probe_error:{exc}", False

        if probe_status < 400:
            return True, "invites_probe_ok", False
        if probe_status in (401, 403):
            return False, f"invites_probe_http_{probe_status}", True
        probe_err = str(raw or "").strip()[:80]
        if probe_err:
            return False, f"invites_probe_http_{probe_status}:{probe_err}", False
        return False, f"invites_probe_http_{probe_status}", False

    access_token = str(getattr(account, "access_token", "") or "").strip()
    verified, source, refreshable = _probe_once(access_token, timeout=max_timeout)
    if verified:
        return True, source

    has_refresh_hint = bool(str(getattr(account, "refresh_token", "") or "").strip()) or bool(
        str(getattr(account, "session_token", "") or "").strip()
    )
    if refreshable and has_refresh_hint:
        try:
            refresh_result = do_refresh(account.id, proxy_url=proxy_url)
            if refresh_result.success:
                with get_db() as db:
                    latest = db.query(Account).filter(Account.id == account.id).first()
                    latest_token = str(getattr(latest, "access_token", "") or "").strip() if latest else ""
                verified2, source2, _refreshable2 = _probe_once(latest_token, timeout=max_timeout)
                if verified2:
                    return True, f"{source2}|after_refresh"
                if source2:
                    return False, f"{source2}|after_refresh"
        except Exception as exc:
            logger.warning("管理号校验 refresh 重试异常: account=%s err=%s", account.id, exc)

    return False, source


def _fetch_joined_members(
    *,
    access_token: str,
    workspace_id: str,
    proxy_url: Optional[str],
    timeout_seconds: int = 35,
) -> Tuple[int, List[Dict[str, Any]], str]:
    members: List[Dict[str, Any]] = []
    offset = 0
    limit = 50
    while True:
        status_code, body, raw = _team_api_request(
            method="GET",
            access_token=access_token,
            workspace_id=workspace_id,
            path=f"/users?limit={limit}&offset={offset}",
            proxy_url=proxy_url,
            timeout_seconds=timeout_seconds,
        )
        if status_code >= 400:
            return status_code, members, _extract_error_text(status_code, body, raw)

        items = body.get("items")
        if not isinstance(items, list):
            items = []
        total = _to_int(body.get("total"), len(items))

        for item in items:
            if not isinstance(item, dict):
                continue
            members.append(
                {
                    "user_id": str(item.get("id") or "").strip() or None,
                    "email": str(item.get("email") or "").strip(),
                    "name": str(item.get("name") or "").strip() or None,
                    "role": str(item.get("role") or "").strip() or "standard-user",
                    "added_at": str(item.get("created_time") or "").strip() or None,
                    "status": "joined",
                }
            )

        if len(members) >= total or not items:
            break
        offset += limit

    return 200, members, ""


def _fetch_invited_members(
    *,
    access_token: str,
    workspace_id: str,
    proxy_url: Optional[str],
    timeout_seconds: int = 35,
) -> Tuple[int, List[Dict[str, Any]], str]:
    status_code, body, raw = _team_api_request(
        method="GET",
        access_token=access_token,
        workspace_id=workspace_id,
        path="/invites",
        proxy_url=proxy_url,
        timeout_seconds=timeout_seconds,
    )
    if status_code >= 400:
        return status_code, [], _extract_error_text(status_code, body, raw)

    items = body.get("items")
    if not isinstance(items, list):
        items = []

    invites: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        invites.append(
            {
                "user_id": None,
                "email": str(item.get("email_address") or "").strip(),
                "name": None,
                "role": str(item.get("role") or "").strip() or "standard-user",
                "added_at": str(item.get("created_time") or "").strip() or None,
                "status": "invited",
            }
        )
    return 200, invites, ""


def _compute_team_status(account_status: str, current_members: int, max_members: int) -> str:
    st = str(account_status or "").strip().lower()
    if st in {
        AccountStatus.FAILED.value,
        AccountStatus.EXPIRED.value,
        AccountStatus.BANNED.value,
    }:
        return st
    if max_members > 0 and current_members >= max_members:
        return "full"
    return AccountStatus.ACTIVE.value


def _build_console_row_for_account(
    *,
    account: Account,
    proxy_url: Optional[str],
    include_member_counts: bool = True,
    request_timeout_seconds: int = 35,
) -> Dict[str, Any]:
    base_item = _build_account_item(account)
    workspace_id = str(base_item.get("workspace_id") or "").strip()
    access_token = str(account.access_token or "").strip()

    selected_workspace: Optional[Dict[str, Any]] = None
    candidates: List[Dict[str, Any]] = []
    if access_token:
        candidates = _fetch_team_workspace_candidates(
            access_token=access_token,
            proxy_url=proxy_url,
            timeout_seconds=request_timeout_seconds,
        )
        workspace_id, selected_workspace = _pick_workspace_id(
            preferred_workspace_id=workspace_id,
            candidates=candidates,
        )

    team_name = str((selected_workspace or {}).get("name") or "").strip() or "MyTeam"
    subscription_plan = str((selected_workspace or {}).get("subscription_plan") or "").strip() or "chatgptteamplan"
    expires_at = str((selected_workspace or {}).get("expires_at") or "").strip() or None

    max_members = _to_int((selected_workspace or {}).get("max_members"), 6)
    current_members = _to_int((selected_workspace or {}).get("current_members"), 0)

    if include_member_counts and access_token and workspace_id:
        joined_status, joined, _joined_err = _fetch_joined_members(
            access_token=access_token,
            workspace_id=workspace_id,
            proxy_url=proxy_url,
            timeout_seconds=request_timeout_seconds,
        )
        invited_status, invited, _invited_err = _fetch_invited_members(
            access_token=access_token,
            workspace_id=workspace_id,
            proxy_url=proxy_url,
            timeout_seconds=request_timeout_seconds,
        )
        if joined_status < 400 and invited_status < 400:
            current_members = len(joined) + len(invited)

    status = _compute_team_status(str(account.status or ""), current_members, max_members)
    plan = _normalize_plan(getattr(account, "subscription_type", None)) or "team"
    if plan == "free":
        plan = "team"

    return {
        "id": account.id,
        "email": account.email,
        "account_id": workspace_id or str(account.account_id or "").strip() or str(account.workspace_id or "").strip(),
        "team_name": team_name,
        "current_members": current_members,
        "max_members": max_members,
        "member_ratio": f"{current_members}/{max_members}",
        "subscription_plan": subscription_plan,
        "expires_at": expires_at,
        "status": status,
        "plan": plan,
        "role_tag": _resolve_account_role_tag(account),
        "pool_state": _resolve_account_pool_state(account),
        "priority": int(getattr(account, "priority", 50) or 50),
        "last_used_at": account.last_used_at.isoformat() if getattr(account, "last_used_at", None) else None,
        "workspace_id": workspace_id,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
        "last_refresh": account.last_refresh.isoformat() if account.last_refresh else None,
    }


def _build_console_row_fallback(account: Account) -> Dict[str, Any]:
    return {
        "id": account.id,
        "email": account.email,
        "account_id": _resolve_workspace_id(account),
        "team_name": "MyTeam",
        "current_members": 0,
        "max_members": 6,
        "member_ratio": "0/6",
        "subscription_plan": "chatgptteamplan",
        "expires_at": None,
        "status": str(account.status or "active"),
        "plan": "team",
        "role_tag": _resolve_account_role_tag(account),
        "pool_state": _resolve_account_pool_state(account),
        "priority": int(getattr(account, "priority", 50) or 50),
        "last_used_at": account.last_used_at.isoformat() if getattr(account, "last_used_at", None) else None,
        "workspace_id": _resolve_workspace_id(account),
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
        "last_refresh": account.last_refresh.isoformat() if account.last_refresh else None,
    }


def _build_console_rows_in_parallel(
    *,
    accounts: List[Account],
    proxy_url: Optional[str],
    include_member_counts: bool,
    request_timeout_seconds: int,
) -> List[Dict[str, Any]]:
    if not accounts:
        return []

    max_workers = min(max(1, TEAM_CONSOLE_ROW_MAX_WORKERS), len(accounts))

    def _build(account: Account) -> Tuple[int, Dict[str, Any]]:
        row = _build_console_row_for_account(
            account=account,
            proxy_url=proxy_url,
            include_member_counts=include_member_counts,
            request_timeout_seconds=request_timeout_seconds,
        )
        return int(account.id), row

    if max_workers <= 1:
        rows: List[Dict[str, Any]] = []
        for account in accounts:
            try:
                _account_id, row = _build(account)
                rows.append(row)
            except Exception as exc:
                logger.warning("构建 Team 控制台行失败: account=%s err=%s", account.email, exc)
                rows.append(_build_console_row_fallback(account))
        return rows

    rows_map: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="team_console") as pool:
        future_map = {pool.submit(_build, account): account for account in accounts}
        for future in as_completed(future_map):
            account = future_map[future]
            try:
                account_id, row = future.result()
                rows_map[int(account_id)] = row
            except Exception as exc:
                logger.warning("构建 Team 控制台行失败: account=%s err=%s", account.email, exc)
                rows_map[int(account.id)] = _build_console_row_fallback(account)

    ordered_rows: List[Dict[str, Any]] = []
    for account in accounts:
        ordered_rows.append(rows_map.get(int(account.id)) or _build_console_row_fallback(account))
    return ordered_rows


def _extract_error_text(status_code: int, body: Dict[str, Any], raw_text: str) -> str:
    error_obj = body.get("error")
    if isinstance(error_obj, dict):
        message = error_obj.get("message")
        if message:
            return str(message)
    detail = body.get("detail")
    if detail:
        return str(detail)
    message = body.get("message")
    if message:
        return str(message)
    if raw_text:
        return raw_text[:500]
    return f"邀请失败: HTTP {status_code}"


def _is_workspace_context_error(error_text: str) -> bool:
    text = str(error_text or "").strip().lower()
    if not text:
        return False
    markers = (
        "must use workspace account",
        "workspace account",
        "workspace",
    )
    return any(m in text for m in markers)


def _is_token_invalidated_error(error_text: str) -> bool:
    text = str(error_text or "").strip().lower()
    if not text:
        return False
    markers = (
        "authentication token has been invalidated",
        "token has been invalidated",
        "please try signing in again",
        "invalid token",
        "token expired",
    )
    return any(m in text for m in markers)


def _looks_like_redeem_gateway_error(error_text: str) -> bool:
    """识别“代理网关误返回兑换页”的错误文本。"""
    text = str(error_text or "").strip().lower()
    if not text:
        return False
    markers = (
        "请输入兑换码",
        "兑换码",
        "redeem",
        "coupon",
        "卡密",
        "checkout",
        "开始订阅",
    )
    return any(m.lower() in text for m in markers)


def _get_team_account_by_id_or_raise(account_id: int) -> Account:
    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")
        plan = _infer_account_plan(account)
        if plan != "team":
            raise HTTPException(status_code=400, detail="仅 Team 账号可执行该操作")
        return account


def _resolve_workspace_and_candidates(
    *,
    account: Account,
    access_token: str,
    proxy_url: Optional[str],
) -> Tuple[str, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    preferred_workspace = _resolve_workspace_id(account)
    candidates = _fetch_team_workspace_candidates(
        access_token=access_token,
        proxy_url=proxy_url,
    )
    workspace_id, selected = _pick_workspace_id(
        preferred_workspace_id=preferred_workspace,
        candidates=candidates,
    )
    return workspace_id, candidates, selected


def _retry_with_refresh_on_auth_error(
    *,
    account: Account,
    proxy_url: Optional[str],
    executor,
) -> Tuple[int, Dict[str, Any], str, str]:
    access_token = str(account.access_token or "").strip()
    try:
        status_code, body, raw, used_access_token = executor(access_token)
    except Exception as exc:
        logger.warning("team executor failed before refresh: account=%s err=%s", account.id, exc)
        status_code, body, raw, used_access_token = 599, {"detail": str(exc)}, str(exc), access_token
    error_text = _extract_error_text(
        status_code,
        body if isinstance(body, dict) else {},
        raw if isinstance(raw, str) else "",
    )
    if status_code not in (401, 403) and not _is_token_invalidated_error(error_text):
        return status_code, body, raw, used_access_token

    refresh_result = do_refresh(account.id, proxy_url=proxy_url)
    if not refresh_result.success:
        return status_code, body, raw, used_access_token

    with get_db() as db:
        latest = db.query(Account).filter(Account.id == account.id).first()
        if latest:
            access_token = str(latest.access_token or "").strip()
    if not access_token:
        return status_code, body, raw, used_access_token
    try:
        return (*executor(access_token), access_token)
    except Exception as exc:
        logger.warning("team executor failed after refresh: account=%s err=%s", account.id, exc)
        return 599, {"detail": str(exc)}, str(exc), access_token


@router.get("/inviter-accounts")
def list_inviter_accounts(force: bool = False, local_only: bool = True):
    """列出可用于发送 Team 邀请的母号账号。"""
    if force:
        _invalidate_team_runtime_caches()
    if local_only:
        accounts = _list_team_inviter_accounts_local(force=force)
        pool_mode = "local_fast_no_network"
    else:
        accounts = _list_team_inviter_accounts(force=force)
        pool_mode = "hybrid_auto_manual"
    return {
        "success": True,
        "total": len(accounts),
        "pool_total": len(accounts),
        "pool_mode": pool_mode,
        "accounts": accounts,
    }


@router.get("/inviter-candidates")
def list_inviter_candidates(force: bool = False):
    """列出可手动拉入管理池的 Team 候选账号（仅母号/普通）。"""
    grouped = _classify_team_accounts(force=bool(force))
    candidates = [
        item
        for item in grouped.get("members", [])
        if str(item.get("plan") or "").strip().lower() == "team"
        and bool(item.get("manager_ready"))
        and str(item.get("pool_state") or "").strip().lower() != PoolState.TEAM_POOL.value
        and str(item.get("pool_state") or "").strip().lower() != PoolState.BLOCKED.value
        and normalize_role_tag(item.get("role_tag")) in {RoleTag.PARENT.value, RoleTag.NONE.value}
    ]
    candidates.sort(
        key=lambda x: (
            str(x.get("status") or "") != AccountStatus.ACTIVE.value,
            0 if normalize_role_tag(x.get("role_tag")) == RoleTag.PARENT.value else 1,
            int(x.get("priority") or 50),
            -(int(x.get("id") or 0)),
        )
    )
    return {
        "success": True,
        "total": len(candidates),
        # 候选拉取仅用于手动入池弹窗，避免这里触发一次昂贵的实时网络校验。
        "pool_total": len(grouped.get("managers", [])),
        "pool_mode": "hybrid_auto_manual",
        "message": "可手动拉入 Team 管理池（仅母号/普通）。",
        "accounts": candidates,
    }


@router.post("/inviter-pool/add")
def add_inviter_pool(request: TeamInviterPoolAddRequest):
    """手动拉入 Team 管理池（设置 pool_state_manual=team_pool）。"""
    requested_ids = _normalize_account_ids(request.account_ids)
    added: List[int] = []
    skipped: List[int] = []
    invalid: List[int] = []

    if not requested_ids:
        return {
            "success": True,
            "added": added,
            "skipped": skipped,
            "invalid": invalid,
            "pool_total": len(_list_team_inviter_accounts(force=True)),
            "pool_mode": "hybrid_auto_manual",
            "message": "未提供可处理账号。",
            "accounts": _list_team_inviter_accounts(force=True),
        }

    now = datetime.utcnow()
    with get_db() as db:
        rows = db.query(Account).filter(Account.id.in_(requested_ids)).all()
        row_map = {int(row.id): row for row in rows}
        for account_id in requested_ids:
            account = row_map.get(int(account_id))
            if not account:
                invalid.append(int(account_id))
                continue
            plan = _infer_account_plan(account)
            if plan != "team":
                invalid.append(int(account_id))
                continue
            role_tag = _resolve_account_role_tag(account)
            if role_tag not in {RoleTag.PARENT.value, RoleTag.NONE.value}:
                invalid.append(int(account_id))
                continue
            has_workspace = bool(str(_resolve_workspace_id(account) or "").strip())
            can_auth = bool(
                str(account.access_token or "").strip()
                or str(account.refresh_token or "").strip()
                or str(account.session_token or "").strip()
            )
            if not (has_workspace and can_auth):
                invalid.append(int(account_id))
                continue

            old_manual = _resolve_account_manual_pool_state(account)
            if old_manual == PoolState.TEAM_POOL.value:
                skipped.append(int(account_id))
                continue

            account.pool_state_manual = PoolState.TEAM_POOL.value
            account.pool_state = PoolState.TEAM_POOL.value
            account.last_pool_sync_at = now
            added.append(int(account_id))
            logger.info(
                "team管理池手动拉入: account_id=%s email=%s manual=%s->%s",
                account.id,
                account.email,
                old_manual or "-",
                PoolState.TEAM_POOL.value,
            )

        db.commit()

    _invalidate_team_runtime_caches()
    _classify_team_accounts(force=True)
    accounts = _list_team_inviter_accounts(force=True)
    return {
        "success": True,
        "added": added,
        "skipped": skipped,
        "invalid": invalid,
        "pool_total": len(accounts),
        "pool_mode": "hybrid_auto_manual",
        "message": "手动拉入已执行，系统将持续按规则自动重算池状态。",
        "accounts": accounts,
    }


@router.post("/pool/rebuild")
def rebuild_team_pool():
    """手动重建 Team 池状态（用于脏状态修复）。"""
    grouped = _classify_team_accounts(force=True)
    inviters = _list_team_inviter_accounts(force=True)
    _invalidate_team_runtime_caches()
    logger.info(
        "team池手动重建完成: manager_candidates=%s member_candidates=%s inviter_pool=%s",
        len(grouped.get("managers", [])),
        len(grouped.get("members", [])),
        len(inviters),
    )
    return {
        "success": True,
        "rebuilt_at": datetime.utcnow().isoformat(),
        "manager_candidates_total": len(grouped.get("managers", [])),
        "member_candidates_total": len(grouped.get("members", [])),
        "pool_total": len(inviters),
    }


@router.get("/team-console")
def get_team_console(
    force: bool = False,
    refresh_pool: bool = False,
    sync_members: bool = False,
):
    """
    Team 管理控制台数据（参考 team-manage-main，去除兑换码统计）。
    """
    use_cache = (not force) and (not sync_members)
    if use_cache:
        cached_payload = _get_cached_payload(_TEAM_CONSOLE_CACHE)
        if cached_payload is not None:
            return cached_payload

    started_at = time.perf_counter()
    inviters = _list_team_inviter_accounts(force=bool(refresh_pool))
    manager_ids = [int(item["id"]) for item in inviters if item.get("id") is not None]
    if not manager_ids:
        payload = {
            "success": True,
            "stats": {
                "team_total": 0,
                "available_team": 0,
            },
            "rows": [],
        }
        if use_cache:
            _set_cached_payload(_TEAM_CONSOLE_CACHE, payload, TEAM_CONSOLE_CACHE_TTL_SECONDS)
        return payload

    with get_db() as db:
        account_rows = db.query(Account).filter(Account.id.in_(manager_ids)).all()
    account_map = {row.id: row for row in account_rows}
    ordered_accounts = [account_map[idx] for idx in manager_ids if idx in account_map]

    proxy_url = _get_proxy()
    rows = _build_console_rows_in_parallel(
        accounts=ordered_accounts,
        proxy_url=proxy_url,
        include_member_counts=bool(sync_members),
        request_timeout_seconds=TEAM_CONSOLE_FETCH_TIMEOUT_SECONDS,
    )
    _sync_team_member_snapshot_to_accounts(rows)

    available_team = sum(
        1 for row in rows
        if row.get("status") == AccountStatus.ACTIVE.value
        and _to_int(row.get("current_members"), 0) < _to_int(row.get("max_members"), 6)
    )
    payload = {
        "success": True,
        "stats": {
            "team_total": len(rows),
            "available_team": available_team,
        },
        "rows": rows,
    }
    if use_cache:
        _set_cached_payload(_TEAM_CONSOLE_CACHE, payload, TEAM_CONSOLE_CACHE_TTL_SECONDS)
    logger.info(
        "team-console refreshed: rows=%s available=%s sync_members=%s refresh_pool=%s cost=%.2fs",
        len(rows),
        available_team,
        bool(sync_members),
        bool(refresh_pool),
        time.perf_counter() - started_at,
    )
    return payload


@router.get("/team-accounts")
def list_team_accounts(force: bool = False):
    """列出 Team 母号/子号分类。"""
    if not force:
        cached_payload = _get_cached_payload(_TEAM_ACCOUNTS_CACHE)
        if cached_payload is not None:
            return cached_payload

    grouped = _classify_team_accounts(force=bool(force))
    managers = _list_team_inviter_accounts(force=force)
    manager_ids = {int(item.get("id") or 0) for item in managers if int(item.get("id") or 0) > 0}
    all_team_accounts: List[Dict[str, Any]] = []
    for item in grouped.get("managers", []) + grouped.get("members", []):
        account_id = int(item.get("id") or 0)
        if account_id <= 0:
            continue
        if account_id in manager_ids:
            continue
        row = dict(item)
        row["team_identity"] = "member"
        all_team_accounts.append(row)

    members = sorted(
        all_team_accounts,
        key=lambda x: (
            str(x.get("status") or "") != AccountStatus.ACTIVE.value,
            -(int(x.get("id") or 0)),
        ),
    )
    payload = {
        "success": True,
        "managers_total": len(managers),
        "members_total": len(members),
        "manager_candidates_total": len(grouped.get("managers", [])),
        "managers": managers,
        "members": members,
    }
    _set_cached_payload(_TEAM_ACCOUNTS_CACHE, payload, TEAM_TEAM_ACCOUNTS_CACHE_TTL_SECONDS)
    return payload


@router.get("/target-accounts")
def list_target_accounts():
    """列出目标邮箱候选账号（按标签拉人，支持配置回退无标签池）。"""
    with get_db() as db:
        locked_map = _get_locked_target_email_map(db)
    fallback_to_none = _read_pull_fallback_to_none()
    accounts = _list_target_email_accounts()
    pool_label = RoleTag.CHILD.value if accounts else (RoleTag.NONE.value if fallback_to_none else RoleTag.CHILD.value)
    return {
        "success": True,
        "pool_label": pool_label,
        "fallback_to_unlabeled": fallback_to_none,
        "total": len(accounts),
        "locked_total": len(locked_map),
        "locked_emails": list(locked_map.keys())[:200],
        "accounts": accounts,
    }


@router.get("/target-pool-config")
def get_target_pool_config():
    fallback_to_none = _read_pull_fallback_to_none()
    return {
        "success": True,
        "fallback_to_unlabeled": fallback_to_none,
        "setting_key": TEAM_POOL_FALLBACK_SETTING_KEY,
    }


@router.post("/target-pool-config")
def update_target_pool_config(request: TargetPoolConfigRequest):
    fallback_to_none = bool(request.fallback_to_none)
    with get_db() as db:
        crud.set_setting(
            db,
            key=TEAM_POOL_FALLBACK_SETTING_KEY,
            value="true" if fallback_to_none else "false",
            description="按标签拉人未命中时是否回退到无标签池",
            category="team",
        )
    _invalidate_team_runtime_caches()
    logger.info(
        "team目标池配置更新: fallback_to_unlabeled=%s",
        fallback_to_none,
    )
    return {
        "success": True,
        "fallback_to_unlabeled": fallback_to_none,
    }


@router.post("/team-accounts/{account_id}/refresh")
def refresh_team_account(account_id: int, proxy: Optional[str] = None):
    """
    刷新单个 Team 管理账号的控制台行数据。
    """
    account = _get_team_account_by_id_or_raise(account_id)
    proxy_url = _get_proxy(proxy)
    row = _build_console_row_for_account(
        account=account,
        proxy_url=proxy_url,
        include_member_counts=True,
        request_timeout_seconds=TEAM_CONSOLE_FETCH_TIMEOUT_SECONDS,
    )
    return {
        "success": True,
        "row": row,
    }


@router.get("/team-accounts/{account_id}/members")
def get_team_account_members(account_id: int, proxy: Optional[str] = None):
    """
    读取 Team 已加入成员和待加入成员（邀请中）。
    """
    try:
        account = _get_team_account_by_id_or_raise(account_id)
        proxy_url = _get_proxy(proxy)
        access_token = str(account.access_token or "").strip()
        if not access_token:
            raise HTTPException(status_code=400, detail="该 Team 账号缺少 access_token")

        workspace_id, candidates, _selected = _resolve_workspace_and_candidates(
            account=account,
            access_token=access_token,
            proxy_url=proxy_url,
        )
        candidate_ids = [str(item.get("account_id") or "").strip() for item in candidates if item.get("account_id")]
        if workspace_id and workspace_id not in candidate_ids:
            candidate_ids.insert(0, workspace_id)
        elif workspace_id and workspace_id in candidate_ids:
            candidate_ids = [workspace_id] + [cid for cid in candidate_ids if cid != workspace_id]
        if not candidate_ids and workspace_id:
            candidate_ids = [workspace_id]
        if not candidate_ids:
            raise HTTPException(status_code=400, detail="未找到可用 Team workspace")

        last_error = ""
        for ws_id in candidate_ids:
            def _exec(token: str):
                joined_status, joined, joined_err = _fetch_joined_members(
                    access_token=token,
                    workspace_id=ws_id,
                    proxy_url=proxy_url,
                    timeout_seconds=TEAM_CONSOLE_FETCH_TIMEOUT_SECONDS,
                )
                if joined_status >= 400:
                    body = {"detail": joined_err}
                    return joined_status, body, joined_err, token
                invited_status, invited, invited_err = _fetch_invited_members(
                    access_token=token,
                    workspace_id=ws_id,
                    proxy_url=proxy_url,
                    timeout_seconds=TEAM_CONSOLE_FETCH_TIMEOUT_SECONDS,
                )
                if invited_status >= 400:
                    body = {"detail": invited_err}
                    return invited_status, body, invited_err, token
                return 200, {"joined": joined, "invited": invited}, "", token

            status_code, body, raw, _used_token = _retry_with_refresh_on_auth_error(
                account=account,
                proxy_url=proxy_url,
                executor=_exec,
            )

            if status_code < 400:
                joined = body.get("joined") or []
                invited = body.get("invited") or []
                members = list(joined) + list(invited)
                return {
                    "success": True,
                    "workspace_id": ws_id,
                    "joined_total": len(joined),
                    "invited_total": len(invited),
                    "total": len(members),
                    "joined_members": joined,
                    "invited_members": invited,
                    "members": members,
                }

            last_error = _extract_error_text(status_code, body if isinstance(body, dict) else {}, raw)
            if _is_workspace_context_error(last_error):
                continue
            raise HTTPException(status_code=400, detail=last_error)

        raise HTTPException(status_code=400, detail=last_error or "读取 Team 成员失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("读取 Team 成员异常: account_id=%s err=%s", account_id, exc)
        raise HTTPException(status_code=400, detail=f"读取 Team 成员失败: {exc}")


@router.post("/team-accounts/{account_id}/members/invite")
async def invite_team_member(account_id: int, request: TeamMemberInviteRequest):
    """
    在 Team 管理弹窗中新增成员邀请。
    """
    target_email = str(request.email or "").strip().lower()
    if not EMAIL_RE.match(target_email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    account = _get_team_account_by_id_or_raise(account_id)
    proxy_url = _get_proxy(request.proxy)

    access_token = str(account.access_token or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="邀请账号缺少 access_token")

    workspace_id, candidates, _selected = _resolve_workspace_and_candidates(
        account=account,
        access_token=access_token,
        proxy_url=proxy_url,
    )
    if not workspace_id:
        raise HTTPException(status_code=400, detail="邀请账号缺少可用 workspace_id")

    candidate_ids = [str(item.get("account_id") or "").strip() for item in candidates if item.get("account_id")]
    if workspace_id and workspace_id in candidate_ids:
        candidate_ids = [workspace_id] + [cid for cid in candidate_ids if cid != workspace_id]
    elif workspace_id:
        candidate_ids = [workspace_id] + candidate_ids

    last_error = ""
    for ws_id in candidate_ids:
        def _exec(token: str):
            status_code, body, raw = _send_team_invite_once(
                access_token=token,
                workspace_id=ws_id,
                target_email=target_email,
                proxy_url=proxy_url,
            )
            return status_code, body, raw, token

        status_code, body, raw, _used_token = _retry_with_refresh_on_auth_error(
            account=account,
            proxy_url=proxy_url,
            executor=_exec,
        )
        error_text = _extract_error_text(status_code, body if isinstance(body, dict) else {}, raw)
        if 200 <= status_code < 300:
            return {
                "success": True,
                "message": "邀请已发送",
                "workspace_id": ws_id,
                "response": body,
            }
        if status_code in (409, 422) or _is_already_member_or_invited(error_text):
            return {
                "success": True,
                "message": "目标邮箱已在 Team 内或已存在邀请，本次按成功处理。",
                "workspace_id": ws_id,
                "response": body or {"detail": error_text},
            }
        last_error = error_text
        if _is_workspace_context_error(error_text):
            continue
        raise HTTPException(status_code=400, detail=error_text)

    raise HTTPException(status_code=400, detail=last_error or "发送邀请失败")


@router.post("/team-accounts/{account_id}/members/revoke")
async def revoke_team_member_invite(account_id: int, request: TeamMemberRevokeRequest):
    """
    撤回 Team 邀请（待加入成员）。
    """
    email = str(request.email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    account = _get_team_account_by_id_or_raise(account_id)
    proxy_url = _get_proxy(request.proxy)

    access_token = str(account.access_token or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="账号缺少 access_token")
    workspace_id, _candidates, _selected = _resolve_workspace_and_candidates(
        account=account,
        access_token=access_token,
        proxy_url=proxy_url,
    )
    if not workspace_id:
        raise HTTPException(status_code=400, detail="未找到可用 Team workspace")

    def _exec(token: str):
        status_code, body, raw = _team_api_request(
            method="DELETE",
            access_token=token,
            workspace_id=workspace_id,
            path="/invites",
            proxy_url=proxy_url,
            payload={"email_address": email},
        )
        return status_code, body, raw, token

    status_code, body, raw, _used_token = _retry_with_refresh_on_auth_error(
        account=account,
        proxy_url=proxy_url,
        executor=_exec,
    )
    if 200 <= status_code < 300:
        return {"success": True, "message": "邀请已撤回", "response": body}

    error_text = _extract_error_text(status_code, body if isinstance(body, dict) else {}, raw)
    raise HTTPException(status_code=400, detail=error_text)


@router.post("/team-accounts/{account_id}/members/remove")
async def remove_team_member(account_id: int, request: TeamMemberRemoveRequest):
    """
    移除 Team 已加入成员。
    """
    user_id = str(request.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id 不能为空")

    account = _get_team_account_by_id_or_raise(account_id)
    proxy_url = _get_proxy(request.proxy)

    access_token = str(account.access_token or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="账号缺少 access_token")
    workspace_id, _candidates, _selected = _resolve_workspace_and_candidates(
        account=account,
        access_token=access_token,
        proxy_url=proxy_url,
    )
    if not workspace_id:
        raise HTTPException(status_code=400, detail="未找到可用 Team workspace")

    def _exec(token: str):
        status_code, body, raw = _team_api_request(
            method="DELETE",
            access_token=token,
            workspace_id=workspace_id,
            path=f"/users/{user_id}",
            proxy_url=proxy_url,
        )
        return status_code, body, raw, token

    status_code, body, raw, _used_token = _retry_with_refresh_on_auth_error(
        account=account,
        proxy_url=proxy_url,
        executor=_exec,
    )
    if 200 <= status_code < 300:
        return {"success": True, "message": "成员已移除", "response": body}

    error_text = _extract_error_text(status_code, body if isinstance(body, dict) else {}, raw)
    raise HTTPException(status_code=400, detail=error_text)


@router.post("/preview")
async def preview_auto_team(request: AutoTeamPreviewRequest):
    """预检输入与可用邀请账号。"""
    target_email = str(request.target_email or "").strip()

    if not EMAIL_RE.match(target_email):
        raise HTTPException(status_code=400, detail="目标邮箱格式不正确")

    inviter = _find_selected_inviter(request.inviter_account_id)
    return {
        "success": True,
        "target_email": target_email,
        "inviter": inviter,
        "tips": "当前为 Team 管理和自动邀请：直接按邮箱执行邀请。",
    }


@router.post("/invite")
async def execute_auto_team_invite(request: AutoTeamInviteRequest):
    """执行team 邀请。"""
    target_email = str(request.target_email or "").strip()

    if not EMAIL_RE.match(target_email):
        raise HTTPException(status_code=400, detail="目标邮箱格式不正确")

    invite_allowed, invite_breaker = breaker_allow_request("team_invite")
    if not invite_allowed:
        raise HTTPException(status_code=429, detail=f"team_invite 熔断中，请稍后重试: {invite_breaker}")

    inviter_item = _find_selected_inviter(request.inviter_account_id)
    inviter_account_id = int(inviter_item.get("id") or 0)
    if inviter_account_id <= 0:
        raise HTTPException(status_code=400, detail="邀请账号无效")

    workspace_id = str(inviter_item.get("workspace_id") or "").strip()
    original_workspace_id = workspace_id

    proxy_url = _get_proxy(request.proxy)
    proxy_explicit = bool(str(request.proxy or "").strip())
    if proxy_url:
        proxy_allowed, proxy_breaker = breaker_allow_request("proxy_runtime")
        if not proxy_allowed:
            logger.warning("team邀请代理通道熔断，自动切换直连: info=%s", proxy_breaker)
            proxy_url = None
    semaphore = _get_inviter_semaphore(inviter_account_id)
    if semaphore.locked():
        logger.info("team邀请进入管理号并发队列: inviter=%s limit=%s", inviter_account_id, MANAGER_CONCURRENCY_LIMIT)
    async with semaphore:
        waited = _get_manager_cooldown_seconds(inviter_account_id)
        if waited > 0:
            logger.info(
                "team邀请命中管理号速率队列等待: inviter=%s wait=%.2fs",
                inviter_account_id,
                waited,
            )
            await asyncio.sleep(min(waited, 15.0))

        with get_db() as db:
            account = db.query(Account).filter(Account.id == inviter_item["id"]).first()
            if not account:
                raise HTTPException(status_code=404, detail="邀请账号不存在")

            access_token = str(account.access_token or "").strip()
            if not access_token:
                refresh_hint = bool(str(account.refresh_token or "").strip())
                session_hint = bool(str(account.session_token or "").strip())
                if refresh_hint or session_hint:
                    logger.info(
                        "team邀请账号缺少 access_token，尝试先刷新 token: inviter=%s refresh=%s session=%s",
                        account.email,
                        "yes" if refresh_hint else "no",
                        "yes" if session_hint else "no",
                    )
                    refresh_result = do_refresh(account.id, proxy_url=proxy_url)
                    if refresh_result.success:
                        db.expire(account)
                        db.refresh(account)
                        access_token = str(account.access_token or "").strip()

            if not access_token:
                raise HTTPException(status_code=400, detail="邀请账号缺少可用 access_token（可先在账号管理刷新订阅/令牌）")

            # 先尝试用 token 实时解析 Team workspace，避免账号表里 workspace 过期。
            team_candidates = _fetch_team_workspace_candidates(
                access_token=access_token,
                proxy_url=proxy_url,
            )
            if team_candidates:
                candidate_ids = [str(x.get("account_id") or "").strip() for x in team_candidates if x.get("account_id")]
                if not workspace_id or workspace_id not in candidate_ids:
                    workspace_id = candidate_ids[0]
                    logger.info(
                        "team邀请使用实时 workspace_id: inviter=%s old=%s new=%s",
                        account.email,
                        original_workspace_id or "-",
                        workspace_id,
                    )

            if not workspace_id:
                raise HTTPException(status_code=400, detail="邀请账号缺少可用 workspace_id/account_id")

            # 先落一条 pending 记录，避免目标邮箱在“邀请成功但订阅未同步”窗口期重复出现在候选列表
            account.last_used_at = datetime.utcnow()
            _upsert_invite_record(
                db,
                inviter_account=account,
                target_email=target_email,
                workspace_id=workspace_id,
                state="pending",
                increment_attempt=True,
            )
            db.commit()

            status_code, body, raw = await _send_team_invite_with_backoff(
                access_token=access_token,
                workspace_id=workspace_id,
                target_email=target_email,
                proxy_url=proxy_url,
                inviter_account_id=account.id,
            )

            error_text = _extract_error_text(status_code, body, raw)
            if status_code in (401, 403) or _is_token_invalidated_error(error_text):
                logger.info(
                    "team邀请命中鉴权失效，尝试刷新 token 后重试: inviter=%s email=%s status=%s",
                    account.email,
                    target_email,
                    status_code,
                )
                refresh_result = do_refresh(account.id, proxy_url=proxy_url)
                if refresh_result.success:
                    db.expire(account)
                    db.refresh(account)
                    access_token = str(account.access_token or "").strip()
                    if access_token:
                        status_code, body, raw = await _send_team_invite_with_backoff(
                            access_token=access_token,
                            workspace_id=workspace_id,
                            target_email=target_email,
                            proxy_url=proxy_url,
                            inviter_account_id=account.id,
                        )
                        error_text = _extract_error_text(status_code, body, raw)

            # 命中 workspace 上下文错误时，自动换用 candidates 重试。
            if status_code >= 400 and _is_workspace_context_error(error_text):
                if not team_candidates:
                    team_candidates = _fetch_team_workspace_candidates(
                        access_token=access_token,
                        proxy_url=proxy_url,
                    )
                tried = {workspace_id}
                switched = False
                for item in team_candidates:
                    candidate_id = str(item.get("account_id") or "").strip()
                    if not candidate_id or candidate_id in tried:
                        continue
                    tried.add(candidate_id)
                    logger.warning(
                        "team邀请命中 workspace 错误，自动切换 workspace 重试: inviter=%s from=%s to=%s",
                        account.email,
                        workspace_id,
                        candidate_id,
                    )
                    workspace_id = candidate_id
                    status_code, body, raw = await _send_team_invite_with_backoff(
                        access_token=access_token,
                        workspace_id=workspace_id,
                        target_email=target_email,
                        proxy_url=proxy_url,
                        inviter_account_id=account.id,
                    )
                    error_text = _extract_error_text(status_code, body, raw)
                    switched = True
                    if status_code < 400 or not _is_workspace_context_error(error_text):
                        break
                if switched and status_code in (401, 403):
                    refresh_result = do_refresh(account.id, proxy_url=proxy_url)
                    if refresh_result.success:
                        db.expire(account)
                        db.refresh(account)
                        access_token = str(account.access_token or "").strip()
                        if access_token:
                            status_code, body, raw = await _send_team_invite_with_backoff(
                                access_token=access_token,
                                workspace_id=workspace_id,
                                target_email=target_email,
                                proxy_url=proxy_url,
                                inviter_account_id=account.id,
                            )
                            error_text = _extract_error_text(status_code, body, raw)

            # 兜底：部分代理会把 chatgpt 请求劫持到“兑换码/checkout”页面，
            # 导致自动邀请误报“请输入兑换码”。非显式代理时自动直连重试一次。
            if (
                proxy_url
                and not proxy_explicit
                and status_code >= 400
                and _looks_like_redeem_gateway_error(error_text)
            ):
                logger.warning(
                    "team邀请疑似命中代理兑换页，自动切换直连重试: inviter=%s email=%s proxy=%s err=%s",
                    account.email,
                    target_email,
                    proxy_url,
                    error_text[:160],
                )
                status_code, body, raw = await _send_team_invite_with_backoff(
                    access_token=access_token,
                    workspace_id=workspace_id,
                    target_email=target_email,
                    proxy_url=None,
                    inviter_account_id=account.id,
                )
                if status_code in (401, 403):
                    refresh_result = do_refresh(account.id, proxy_url=None)
                    if refresh_result.success:
                        db.expire(account)
                        db.refresh(account)
                        access_token = str(account.access_token or "").strip()
                        if access_token:
                            status_code, body, raw = await _send_team_invite_with_backoff(
                                access_token=access_token,
                                workspace_id=workspace_id,
                                target_email=target_email,
                                proxy_url=None,
                                inviter_account_id=account.id,
                            )
                error_text = _extract_error_text(status_code, body, raw)

            if 200 <= status_code < 300:
                _upsert_invite_record(
                    db,
                    inviter_account=account,
                    target_email=target_email,
                    workspace_id=workspace_id,
                    state="invited",
                )
                db.commit()
                _update_manager_health_after_invite(
                    account_id=account.id,
                    status_code=status_code,
                    error_text="",
                    success=True,
                )
                breaker_record_success("team_invite")
                if proxy_url:
                    breaker_record_success("proxy_runtime")
                return {
                    "success": True,
                    "message": "邀请已提交，请到目标邮箱查收 Team 邀请邮件。",
                    "target_email": target_email,
                    "inviter": inviter_item,
                    "request_meta": {
                        "workspace_id": workspace_id,
                        "workspace_id_original": original_workspace_id or None,
                        "proxy": "on" if proxy_url else "off",
                        "http_status": status_code,
                    },
                    "response": body,
                }

            if status_code in (409, 422) or _is_already_member_or_invited(error_text):
                final_state = "joined" if ("already a member" in str(error_text or "").lower() or "already in workspace" in str(error_text or "").lower()) else "invited"
                _upsert_invite_record(
                    db,
                    inviter_account=account,
                    target_email=target_email,
                    workspace_id=workspace_id,
                    state=final_state,
                    last_error=error_text,
                )
                db.commit()
                _update_manager_health_after_invite(
                    account_id=account.id,
                    status_code=status_code,
                    error_text=error_text,
                    success=True,
                )
                breaker_record_success("team_invite")
                if proxy_url:
                    breaker_record_success("proxy_runtime")
                return {
                    "success": True,
                    "message": "目标邮箱已在 Team 内或已存在邀请，本次按成功处理。",
                    "target_email": target_email,
                    "inviter": inviter_item,
                    "request_meta": {
                        "workspace_id": workspace_id,
                        "workspace_id_original": original_workspace_id or None,
                        "proxy": "on" if proxy_url else "off",
                        "http_status": status_code,
                    },
                    "response": body or {"detail": error_text},
                }

            _upsert_invite_record(
                db,
                inviter_account=account,
                target_email=target_email,
                workspace_id=workspace_id,
                state="failed",
                last_error=error_text,
            )
            db.commit()
            _update_manager_health_after_invite(
                account_id=account.id,
                status_code=status_code,
                error_text=error_text,
                success=False,
            )
            breaker_record_failure("team_invite", error_text)
            if proxy_url:
                breaker_record_failure("proxy_runtime", error_text)
            raise HTTPException(status_code=400, detail=error_text)

