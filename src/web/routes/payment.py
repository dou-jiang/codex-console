"""
支付相关 API 路由
"""

import logging
import os
import re
import uuid
import threading
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import Optional, List, Dict, Any, Tuple, Callable
from datetime import datetime
import time
from urllib.parse import urlparse, urlunparse, quote

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from curl_cffi import requests as cffi_requests

from ...database.session import get_db
from ...database import crud
from ...database.models import Account, BindCardTask, EmailService as EmailServiceModel
from ...config.settings import get_settings
from ...config.constants import (
    OPENAI_PAGE_TYPES,
)
from ...services import EmailServiceFactory, EmailServiceType
from ...core.register import RegistrationEngine
from .accounts import resolve_account_ids
from ...core.openai.payment import (
    generate_plus_checkout_bundle,
    generate_team_checkout_bundle,
    generate_aimizy_payment_link,
    open_url_incognito,
    check_subscription_status_detail,
)
from ...core.openai.browser_bind import auto_bind_checkout_with_playwright
from ...core.openai.random_billing import generate_random_billing_profile
from ...core.openai.token_refresh import TokenRefreshManager
from ...core.dynamic_proxy import get_proxy_url_for_task
from ...core.circuit_breaker import allow_request as breaker_allow_request
from ...core.circuit_breaker import record_failure as breaker_record_failure
from ...core.circuit_breaker import record_success as breaker_record_success
from ..task_manager import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()
CHECKOUT_SESSION_REGEX = re.compile(r"\bcs_[A-Za-z0-9_-]+\b", re.IGNORECASE)
THIRD_PARTY_BIND_API_URL_ENV = "BIND_CARD_API_URL"
THIRD_PARTY_BIND_API_KEY_ENV = "BIND_CARD_API_KEY"
THIRD_PARTY_BIND_API_DEFAULT = "https://twilight-river-f148.482091502.workers.dev/"
THIRD_PARTY_BIND_PATH_DEFAULT = "/api/v1/bind-card"
CHECKOUT_CONNECTIVITY_ERROR_KEYWORDS = (
    "failed to connect",
    "could not connect to server",
    "connection refused",
    "timed out",
    "timeout",
    "temporary failure in name resolution",
    "name or service not known",
    "proxy connect",
    "network is unreachable",
    "curl: (7)",
    "curl: (28)",
    "curl: (35)",
    "curl: (56)",
)
REGION_BLOCK_ERROR_KEYWORDS = (
    "unsupported_country_region_territory",
    "country, region, or territory not supported",
    "request_forbidden",
)
CHECKOUT_COUNTRY_CURRENCY_MAP = {
    "US": "USD",
    "GB": "GBP",
    "CA": "CAD",
    "AU": "AUD",
    "SG": "SGD",
    "HK": "HKD",
    "JP": "JPY",
    "TR": "TRY",
    "IN": "INR",
    "BR": "BRL",
    "MX": "MXN",
    "DE": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "EU": "EUR",
}
VENDOR_REDEEM_CODE_REGEX = re.compile(r"^UK(?:-[A-Z0-9]{5}){5}$")
VENDOR_EFUN_FLOW_VERSION = "vendor_efun_flow_20260327d"
VENDOR_BINDCARD_API_URL_ENV = "VENDOR_BINDCARD_API_URL"
VENDOR_BINDCARD_API_KEY_ENV = "VENDOR_BINDCARD_API_KEY"
VENDOR_BINDCARD_API_URL_DEFAULT = "https://card.aimizy.com/api/v1/bindcard"
VENDOR_BINDCARD_API_KEY_DEFAULT = ""
EFUNCARD_BASE_URL_ENV = "EFUNCARD_BASE_URL"
EFUNCARD_API_KEY_ENV = "EFUNCARD_API_KEY"
EFUNCARD_BASE_URL_DEFAULT = "https://card.efuncard.com"
EFUNCARD_API_KEY_DEFAULT = ""
EFUNCARD_CODE_REGEX = re.compile(r"^UK(?:-[A-Z0-9]{5}){5}$")
EFUNCARD_EXPIRY_REGEX = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{2,4})\s*$")
VENDOR_NODE_INSTRUCTION_SPLIT_REGEX = re.compile(r"\s*,\s*")
_VENDOR_PROGRESS_LOCK = threading.Lock()
_VENDOR_PROGRESS_STATE: Dict[int, Dict[str, Any]] = {}
_VENDOR_PROGRESS_LOG_MAX = 400
_VENDOR_FORCE_STOP_AFTER_SECONDS = 120
_PAYMENT_OP_TASK_LOCK = threading.Lock()
_PAYMENT_OP_TASKS: Dict[str, Dict[str, Any]] = {}
_PAYMENT_OP_TASK_MAX_KEEP = 300
_PAYMENT_OP_TASK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="payment_op")
TERMINAL_BIND_TASK_STATUSES = ("completed", "failed", "cancelled")
_BIND_TASK_CREATE_LOCK_GUARD = threading.Lock()
_BIND_TASK_CREATE_LOCKS: Dict[int, threading.Lock] = {}
BIND_TASK_CREATE_LOCK_TIMEOUT_SECONDS = 8.0
PAYMENT_BATCH_SUBSCRIPTION_CHECK_MAX_WORKERS = 8
PAYMENT_BATCH_SUBSCRIPTION_CHECK_RETRY_ATTEMPTS = 2
PAYMENT_BATCH_SUBSCRIPTION_CHECK_RETRY_BASE_DELAY_SECONDS = 0.8
DISABLED_BIND_MODES = ("third_party", "vendor_auto")
ALLOWED_BIND_MODES = ("semi_auto", "local_auto", "vendor_efun")


def _resolve_actor(request: Optional[Request]) -> str:
    if request is None:
        return "system"
    for key in ("x-operator", "x-user", "x-username"):
        value = str(request.headers.get(key) or "").strip()
        if value:
            return value[:120]
    client_host = ""
    try:
        client_host = str(getattr(getattr(request, "client", None), "host", "") or "").strip()
    except Exception:
        client_host = ""
    return f"api@{client_host}" if client_host else "api"


def _is_official_checkout_link(link: Optional[str]) -> bool:
    return isinstance(link, str) and link.startswith("https://chatgpt.com/checkout/openai_llc/")


def _is_checkout_connectivity_error(err: Exception) -> bool:
    text = str(err or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in CHECKOUT_CONNECTIVITY_ERROR_KEYWORDS)


def _is_region_block_error_text(text: Optional[str]) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    return any(token in raw for token in REGION_BLOCK_ERROR_KEYWORDS)


def _normalize_checkout_country(country: Optional[str]) -> str:
    code = str(country or "US").strip().upper()
    if code in CHECKOUT_COUNTRY_CURRENCY_MAP:
        return code
    return "US"


def _normalize_checkout_currency(country: str, currency: Optional[str]) -> str:
    raw = str(currency or "").strip().upper()
    if raw:
        return raw
    return CHECKOUT_COUNTRY_CURRENCY_MAP.get(country, "USD")


def _normalize_proxy_value(proxy: Optional[str]) -> str:
    return str(proxy or "").strip()


def _build_proxy_candidates(
    explicit_proxy: Optional[str],
    account: Optional[Account] = None,
    *,
    include_direct: bool = True,
) -> List[Optional[str]]:
    """
    代理候选顺序：
    1) 显式传入
    2) 账号历史代理（注册时成功线路）
    3) 系统全局代理
    4) 直连（可选）
    """
    candidates: List[Optional[str]] = []
    seen = set()

    account_proxy = _normalize_proxy_value(getattr(account, "proxy_used", None) if account else None)
    settings_proxy = _normalize_proxy_value(get_settings().proxy_url)
    explicit_proxy_norm = _normalize_proxy_value(explicit_proxy)

    for item in (explicit_proxy_norm, account_proxy, settings_proxy):
        if not item or item in seen:
            continue
        candidates.append(item)
        seen.add(item)

    if include_direct:
        candidates.append(None)
    elif not candidates:
        return []

    if not candidates:
        return [None]
    return candidates


def _resolve_runtime_proxy(explicit_proxy: Optional[str], account: Optional[Account] = None) -> Optional[str]:
    """
    选一个首选代理，给非轮询型接口使用。
    """
    for candidate in _build_proxy_candidates(explicit_proxy, account, include_direct=False):
        if candidate:
            return candidate
    try:
        dynamic_proxy = _normalize_proxy_value(get_proxy_url_for_task())
    except Exception:
        dynamic_proxy = ""
    if dynamic_proxy:
        return dynamic_proxy
    return None


def _get_bind_task_create_lock(account_id: int) -> threading.Lock:
    key = int(account_id)
    with _BIND_TASK_CREATE_LOCK_GUARD:
        lock = _BIND_TASK_CREATE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BIND_TASK_CREATE_LOCKS[key] = lock
        return lock


@contextmanager
def _acquire_bind_task_create_lock(account_id: int):
    lock = _get_bind_task_create_lock(account_id)
    acquired = lock.acquire(timeout=BIND_TASK_CREATE_LOCK_TIMEOUT_SECONDS)
    if not acquired:
        raise HTTPException(status_code=429, detail="该账号正在创建绑卡任务，请稍后重试")
    try:
        yield
    finally:
        lock.release()


def _find_active_bind_task_for_account(db, account_id: int) -> Optional[BindCardTask]:
    return (
        db.query(BindCardTask)
        .filter(BindCardTask.account_id == int(account_id))
        .filter(
            or_(
                BindCardTask.status.is_(None),
                ~BindCardTask.status.in_(TERMINAL_BIND_TASK_STATUSES),
            )
        )
        .order_by(BindCardTask.created_at.desc(), BindCardTask.id.desc())
        .first()
    )


def _resolve_efuncard_base_url(request_base_url: Optional[str]) -> str:
    raw = (
        str(request_base_url or "").strip()
        or str(os.getenv(EFUNCARD_BASE_URL_ENV) or "").strip()
        or EFUNCARD_BASE_URL_DEFAULT
    )
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="EfunCard base_url 无效")
    return urlunparse(parsed._replace(path="", params="", query="", fragment="")).rstrip("/")


def _resolve_efuncard_api_key(request_api_key: Optional[str]) -> str:
    token = (
        str(request_api_key or "").strip()
        or str(os.getenv(EFUNCARD_API_KEY_ENV) or "").strip()
        or EFUNCARD_API_KEY_DEFAULT
    )
    if not token:
        raise HTTPException(
            status_code=400,
            detail=f"缺少 EfunCard API Key（可设置环境变量 {EFUNCARD_API_KEY_ENV}）",
        )
    return token


def _normalize_efuncard_code(code: Optional[str]) -> str:
    text = str(code or "").strip().upper()
    if not text:
        raise HTTPException(status_code=400, detail="code 不能为空")
    compact = re.sub(r"[^A-Z0-9]", "", text)
    if not compact.startswith("UK"):
        raise HTTPException(status_code=400, detail="code 格式无效")
    body = compact[2:]
    if len(body) != 25:
        raise HTTPException(status_code=400, detail="code 格式无效")
    normalized = f"UK-{body[0:5]}-{body[5:10]}-{body[10:15]}-{body[15:20]}-{body[20:25]}"
    if not EFUNCARD_CODE_REGEX.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="code 格式无效")
    return normalized


def _parse_efuncard_expiry(expiry_date: Optional[str]) -> Tuple[str, str]:
    text = str(expiry_date or "").strip()
    if not text:
        return "", ""
    match = EFUNCARD_EXPIRY_REGEX.match(text)
    if not match:
        return "", ""
    month_raw = str(match.group(1) or "").strip()
    year_raw = str(match.group(2) or "").strip()
    try:
        month_int = int(month_raw)
    except Exception:
        return "", ""
    if month_int < 1 or month_int > 12:
        return "", ""
    month = f"{month_int:02d}"
    if len(year_raw) == 2:
        year = f"20{year_raw}"
    else:
        year = year_raw[:4]
    if not year.isdigit():
        return "", ""
    return month, year


def _efuncard_request(
    *,
    method: str,
    path: str,
    api_key: str,
    base_url: str,
    proxy: Optional[str],
    payload: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 35,
) -> Dict[str, Any]:
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    session_kwargs: Dict[str, Any] = {
        "impersonate": "chrome120",
        "timeout": max(5, int(timeout_seconds)),
    }
    if proxy:
        session_kwargs["proxy"] = proxy
    session = cffi_requests.Session(**session_kwargs)
    try:
        # 避免继承宿主机 HTTP(S)_PROXY，EFun 仅使用显式配置的代理
        session.trust_env = False
    except Exception:
        pass
    method_up = str(method or "GET").strip().upper()
    if method_up == "POST":
        response = session.post(url, headers=headers, json=payload or {})
    elif method_up == "GET":
        response = session.get(url, headers=headers)
    else:
        raise HTTPException(status_code=400, detail=f"不支持的 method: {method_up}")

    raw = ""
    try:
        body = response.json()
        if not isinstance(body, dict):
            body = {"success": False, "raw": body}
    except Exception:
        try:
            raw = str(response.text or "").strip()
        except Exception:
            raw = ""
        body = {"success": False, "message": raw or "provider_non_json_response"}

    if response.status_code >= 400:
        message = str(
            body.get("message")
            or body.get("error")
            or body.get("msg")
            or raw
            or f"http_{response.status_code}"
        ).strip()
        raise HTTPException(status_code=response.status_code, detail=f"EfunCard 请求失败: {message}")

    if body.get("success") is False:
        message = str(body.get("message") or body.get("error") or body.get("msg") or "provider_failed").strip()
        raise HTTPException(status_code=400, detail=f"EfunCard 返回失败: {message}")

    return body


def _resolve_vendor_bindcard_api_url(request_url: Optional[str]) -> str:
    raw = (
        str(request_url or "").strip()
        or str(os.getenv(VENDOR_BINDCARD_API_URL_ENV) or "").strip()
        or VENDOR_BINDCARD_API_URL_DEFAULT
    )
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="卡商 bindcard API 地址无效")
    path = str(parsed.path or "").strip()
    if not path or path == "/":
        path = "/api/v1/bindcard"
    normalized = parsed._replace(path="/" + path.lstrip("/"), params="", query="", fragment="")
    return urlunparse(normalized)


def _resolve_vendor_bindcard_api_key(request_api_key: Optional[str]) -> str:
    token = (
        str(request_api_key or "").strip()
        or str(os.getenv(VENDOR_BINDCARD_API_KEY_ENV) or "").strip()
        or VENDOR_BINDCARD_API_KEY_DEFAULT
    )
    if not token:
        raise HTTPException(
            status_code=400,
            detail=f"缺少卡商 bindcard API Key（请求传 api_key 或设置环境变量 {VENDOR_BINDCARD_API_KEY_ENV}）",
        )
    return token


def _build_vendor_bindcard_api_candidates(api_url: str) -> List[str]:
    text = str(api_url or "").strip()
    if not text:
        return []
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return []
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = str(parsed.path or "").strip()
    if not path or path == "/":
        path = "/api/v1/bindcard"
    path = "/" + path.lstrip("/")
    lower = path.lower()
    candidates: List[str] = []

    def _append(value: str):
        item = str(value or "").strip()
        if item and item not in candidates:
            candidates.append(item)

    _append(base + path)
    if lower.endswith("/bindcard"):
        _append(base + path[: -len("/bindcard")] + "/bind-card")
    elif lower.endswith("/bind-card"):
        _append(base + path[: -len("/bind-card")] + "/bindcard")
    else:
        _append(base + "/api/v1/bindcard")
        _append(base + "/api/v1/bind-card")
    return candidates


def _normalize_vendor_card_payload(redeem_data: Dict[str, Any]) -> Dict[str, str]:
    data = redeem_data if isinstance(redeem_data, dict) else {}
    card_number = str(data.get("cardNumber") or data.get("card_number") or "").strip()
    cvc = str(data.get("cvv") or data.get("cvc") or "").strip()
    exp_month = str(data.get("exp_month") or data.get("expiryMonth") or "").strip()
    exp_year = str(data.get("exp_year") or data.get("expiryYear") or "").strip()

    if not exp_month or not exp_year:
        exp_date = str(data.get("expiryDate") or "").strip()
        month_fallback, year_fallback = _parse_efuncard_expiry(exp_date)
        exp_month = exp_month or month_fallback
        exp_year = exp_year or year_fallback

    try:
        exp_month_num = int(str(exp_month or "").strip())
        exp_month = f"{exp_month_num:02d}"
    except Exception:
        exp_month = ""

    exp_year_text = re.sub(r"[^0-9]", "", str(exp_year or "").strip())
    if len(exp_year_text) == 2:
        exp_year_text = f"20{exp_year_text}"
    if len(exp_year_text) > 4:
        exp_year_text = exp_year_text[:4]

    return {
        "number": card_number,
        "exp_month": exp_month,
        "exp_year": exp_year_text,
        "cvc": cvc,
    }


def _vendor_country_code_from_text(text: Optional[str], fallback: str = "US") -> str:
    raw = str(text or "").strip().upper()
    if not raw:
        return _normalize_checkout_country(fallback)
    if len(raw) == 2 and raw.isalpha():
        return _normalize_checkout_country(raw)

    mapping = {
        "UNITED KINGDOM": "GB",
        "GREAT BRITAIN": "GB",
        "BRITAIN": "GB",
        "ENGLAND": "GB",
        "UK": "GB",
        "U.K.": "GB",
        "UNITED STATES": "US",
        "USA": "US",
        "U.S.": "US",
        "U.S.A.": "US",
        "HONG KONG": "HK",
        "JAPAN": "JP",
        "SINGAPORE": "SG",
        "CANADA": "CA",
        "AUSTRALIA": "AU",
        "GERMANY": "DE",
        "FRANCE": "FR",
        "SPAIN": "ES",
        "ITALY": "IT",
    }
    for key, code in mapping.items():
        if key in raw:
            return _normalize_checkout_country(code)
    return _normalize_checkout_country(fallback)


def _vendor_proxy_country_label(country_code: str) -> str:
    code = _normalize_checkout_country(country_code)
    mapping = {
        "US": "美国",
        "GB": "英国",
        "CA": "加拿大",
        "AU": "澳大利亚",
        "SG": "新加坡",
        "HK": "香港",
        "JP": "日本",
        "TR": "土耳其",
        "IN": "印度",
        "BR": "巴西",
        "MX": "墨西哥",
        "DE": "德国",
        "FR": "法国",
        "IT": "意大利",
        "ES": "西班牙",
    }
    return mapping.get(code, code)


def _parse_vendor_node_instructions(node_text: Optional[str], fallback_country: str) -> Dict[str, str]:
    cleaned = re.sub(r"\s+", " ", str(node_text or "")).strip(" ,")
    if not cleaned:
        return {}
    parts = [p.strip() for p in VENDOR_NODE_INSTRUCTION_SPLIT_REGEX.split(cleaned) if p and p.strip()]
    if not parts:
        return {}

    country = _vendor_country_code_from_text(parts[-1], fallback=fallback_country)
    postal = parts[-2] if len(parts) >= 2 else ""
    city = parts[-3] if len(parts) >= 3 else ""
    line1 = ", ".join(parts[:-3]) if len(parts) >= 4 else (parts[0] if parts else "")
    if not line1 and len(parts) >= 2:
        line1 = parts[0]

    return {
        "country": country,
        "line1": str(line1 or "").strip(),
        "city": str(city or "").strip(),
        "state": "",
        "postal_code": str(postal or "").strip(),
        "raw": cleaned,
    }


def _build_vendor_billing_payload(
    *,
    account: Account,
    redeem_data: Dict[str, Any],
    country_hint: str,
) -> Tuple[Dict[str, str], str]:
    fallback_country = _normalize_checkout_country(country_hint)
    node_text = str(
        (redeem_data or {}).get("nodeInstructions")
        or (redeem_data or {}).get("groupInstructions")
        or ""
    ).strip()
    parsed = _parse_vendor_node_instructions(node_text, fallback_country)
    source = "efun.nodeInstructions" if parsed else "random_billing"

    try:
        random_profile = generate_random_billing_profile(country=fallback_country, proxy=None)
    except Exception:
        random_profile = {}

    random_name = str((random_profile or {}).get("billing_name") or "").strip()
    random_line1 = str((random_profile or {}).get("address_line1") or "").strip()
    random_city = str((random_profile or {}).get("address_city") or "").strip()
    random_state = str((random_profile or {}).get("address_state") or "").strip()
    random_postal = str((random_profile or {}).get("postal_code") or "").strip()
    random_country = _normalize_checkout_country((random_profile or {}).get("country_code") or fallback_country)

    email = str(getattr(account, "email", "") or "").strip()
    email_name = str(email.split("@")[0] if "@" in email else email).replace(".", " ").replace("_", " ").strip()
    fallback_name = re.sub(r"\s+", " ", email_name).title() if email_name else "Card Holder"

    billing = {
        "name": random_name or fallback_name,
        "email": email,
        "country": _normalize_checkout_country(parsed.get("country") or random_country or fallback_country),
        "state": str(parsed.get("state") or random_state or "").strip(),
        "city": str(parsed.get("city") or random_city or "").strip(),
        "line1": str(parsed.get("line1") or random_line1 or "").strip(),
        "postal_code": str(parsed.get("postal_code") or random_postal or "").strip(),
    }
    return billing, source


def _invoke_vendor_bindcard_api(
    *,
    api_url: str,
    api_key: str,
    payload: Dict[str, Any],
    timeout_seconds: int = 120,
) -> Tuple[Dict[str, Any], str]:
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": "codex-console2/vendor-efun-bindcard",
        "Authorization": f"Bearer {api_key}",
    }
    candidates = _build_vendor_bindcard_api_candidates(api_url)
    if not candidates:
        raise RuntimeError("卡商 bindcard API 地址无效")
    errors: List[str] = []
    for endpoint in candidates:
        try:
            session = cffi_requests.Session(
                impersonate="chrome120",
                timeout=max(20, int(timeout_seconds or 120)),
            )
            try:
                session.trust_env = False
            except Exception:
                pass
            resp = session.post(endpoint, headers=headers, json=payload)
            parsed = _parse_third_party_response(resp)
            if resp.status_code >= 400:
                body = str((parsed or {}).get("message") or (parsed or {}).get("error") or (resp.text or ""))[:400]
                errors.append(f"{endpoint} http={resp.status_code} body={body}")
                continue
            if not isinstance(parsed, dict):
                parsed = {"raw": str(parsed)[:1000]}
            parsed["_meta_endpoint"] = endpoint
            return parsed, endpoint
        except Exception as exc:
            errors.append(f"{endpoint} error={exc}")
            continue
    summary = " | ".join(errors[-4:]) if errors else "unknown_error"
    raise RuntimeError(f"卡商 bindcard API 调用失败: {summary}")


def _serialize_bind_card_task(task: BindCardTask) -> dict:
    # 账号删除后仍展示邮箱快照（纯文本），便于历史任务识别
    account_email = task.account.email if task.account else (str(getattr(task, "account_email", "") or "").strip() or None)
    return {
        "id": task.id,
        "account_id": task.account_id,
        "account_email": account_email,
        "plan_type": task.plan_type,
        "workspace_name": task.workspace_name,
        "price_interval": task.price_interval,
        "seat_quantity": task.seat_quantity,
        "country": task.country,
        "currency": task.currency,
        "checkout_url": task.checkout_url,
        "checkout_session_id": task.checkout_session_id,
        "publishable_key": task.publishable_key,
        "has_client_secret": bool(getattr(task, "client_secret", None)),
        "checkout_source": task.checkout_source,
        "bind_mode": task.bind_mode or "semi_auto",
        "status": task.status,
        "last_error": task.last_error,
        "opened_at": task.opened_at.isoformat() if task.opened_at else None,
        "last_checked_at": task.last_checked_at.isoformat() if task.last_checked_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _extract_checkout_session_id_from_url(url: Optional[str]) -> Optional[str]:
    text = str(url or "").strip()
    if not text:
        return None
    match = CHECKOUT_SESSION_REGEX.search(text)
    if match:
        return match.group(0)
    return None


def _resolve_account_device_id(account: Account) -> str:
    """
    兼容解析账号 device id。
    历史模型未包含 device_id 字段，需从 cookies/extra_data 兜底读取。
    """
    direct = str(getattr(account, "device_id", "") or "").strip()
    if direct:
        return direct

    cookies_text = str(getattr(account, "cookies", "") or "")
    if cookies_text:
        match = re.search(r"(?:^|;\s*)oai-did=([^;]+)", cookies_text)
        if match:
            value = str(match.group(1) or "").strip()
            if value:
                return value

    extra_data = getattr(account, "extra_data", None)
    if isinstance(extra_data, dict):
        for key in ("device_id", "oai_did", "oai-device-id"):
            value = str(extra_data.get(key) or "").strip()
            if value:
                return value
    return str(uuid.uuid4())


def _extract_cookie_value(cookies_text: Optional[str], cookie_name: str) -> str:
    text = str(cookies_text or "")
    if not text:
        return ""
    pattern = re.compile(rf"(?:^|;\s*){re.escape(cookie_name)}=([^;]+)")
    match = pattern.search(text)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _extract_session_token_from_cookie_text(cookies_text: Optional[str]) -> str:
    text = str(cookies_text or "")
    if not text:
        return ""

    direct = _extract_cookie_value(text, "__Secure-next-auth.session-token")
    if direct:
        return direct

    # NextAuth 可能把大 token 分片为 .0/.1/.2...
    chunks: dict[int, str] = {}
    for raw in text.split(";"):
        item = str(raw or "").strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        key = str(name or "").strip()
        if not key.startswith("__Secure-next-auth.session-token."):
            continue
        try:
            idx = int(key.rsplit(".", 1)[-1])
        except Exception:
            continue
        chunks[idx] = str(value or "").strip()
    if chunks:
        return "".join(chunks[idx] for idx in sorted(chunks.keys()))
    return ""


def _extract_session_token_from_cookie_jar(cookie_jar) -> str:
    try:
        direct = str(cookie_jar.get("__Secure-next-auth.session-token") or "").strip()
    except Exception:
        direct = ""
    if direct:
        return direct

    chunks: dict[int, str] = {}
    try:
        items = list(cookie_jar.items())
    except Exception:
        items = []
    for key, value in items:
        name = str(key or "").strip()
        if not name.startswith("__Secure-next-auth.session-token."):
            continue
        try:
            idx = int(name.rsplit(".", 1)[-1])
        except Exception:
            continue
        chunks[idx] = str(value or "").strip()
    if chunks:
        return "".join(chunks[idx] for idx in sorted(chunks.keys()))
    return ""


def _extract_session_token_chunks_from_cookie_text(cookies_text: Optional[str]) -> List[int]:
    text = str(cookies_text or "")
    if not text:
        return []
    indices: List[int] = []
    seen = set()
    for raw in text.split(";"):
        item = str(raw or "").strip()
        if not item or "=" not in item:
            continue
        name, _ = item.split("=", 1)
        key = str(name or "").strip()
        if not key.startswith("__Secure-next-auth.session-token."):
            continue
        try:
            idx = int(key.rsplit(".", 1)[-1])
        except Exception:
            continue
        if idx in seen:
            continue
        seen.add(idx)
        indices.append(idx)
    return sorted(indices)


def _mask_secret(value: Optional[str], keep_start: int = 6, keep_end: int = 4) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= keep_start + keep_end + 2:
        return "*" * len(text)
    return f"{text[:keep_start]}...{text[-keep_end:]}"


def _probe_auth_session_context(account: Account, proxy: Optional[str]) -> dict:
    """
    对当前账号做一次实时 session 探测，帮助定位“缺 session token”的根因。
    """
    session = cffi_requests.Session(
        impersonate="chrome120",
        proxy=proxy,
    )
    _seed_cookie_jar_from_text(session, account.cookies)

    device_id = _resolve_account_device_id(account)
    if device_id:
        try:
            session.cookies.set("oai-did", device_id, domain=".chatgpt.com", path="/")
        except Exception:
            pass

    headers = {
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    access_token = str(account.access_token or "").strip()
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        # 先热身主页，提升 next-auth 会话链路稳定性
        session.get(
            "https://chatgpt.com/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://chatgpt.com/",
                "User-Agent": headers["User-Agent"],
            },
            timeout=20,
        )
    except Exception:
        pass

    result = {
        "ok": False,
        "http_status": None,
        "session_token_found": False,
        "session_token_preview": "",
        "access_token_in_session_json": False,
        "access_token_preview": "",
        "error": "",
    }

    try:
        resp = session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=25,
        )
        result["http_status"] = int(getattr(resp, "status_code", 0) or 0)

        session_token = _extract_session_token_from_cookie_jar(getattr(resp, "cookies", None))
        if not session_token:
            session_token = _extract_session_token_from_cookie_jar(getattr(session, "cookies", None))
        if not session_token:
            set_cookie = (
                " | ".join(resp.headers.get_list("set-cookie"))
                if hasattr(resp.headers, "get_list")
                else str(resp.headers.get("set-cookie") or "")
            )
            match_direct = re.search(r"__Secure-next-auth\.session-token=([^;,\s]+)", set_cookie)
            if match_direct:
                session_token = str(match_direct.group(1) or "").strip()
            else:
                chunk_matches = re.findall(r"__Secure-next-auth\.session-token\.(\d+)=([^;,\s]+)", set_cookie)
                if chunk_matches:
                    chunk_map = {int(i): v for i, v in chunk_matches if str(i).isdigit()}
                    if chunk_map:
                        session_token = "".join(chunk_map[idx] for idx in sorted(chunk_map.keys()))

        payload = {}
        try:
            payload = resp.json() if resp.content else {}
        except Exception:
            payload = {}
        session_access = str(payload.get("accessToken") or "").strip()

        result["session_token_found"] = bool(session_token)
        result["session_token_preview"] = _mask_secret(session_token)
        result["access_token_in_session_json"] = bool(session_access)
        result["access_token_preview"] = _mask_secret(session_access)
        result["ok"] = result["http_status"] == 200
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def _force_fetch_nextauth_session_token(
    *,
    access_token: Optional[str],
    cookies_text: Optional[str],
    device_id: Optional[str],
    proxy: Optional[str],
) -> tuple[str, str]:
    """
    尝试通过 /api/auth/session 强制换取 __Secure-next-auth.session-token。
    Returns:
        (session_token, fresh_access_token)
    """
    initial_access = str(access_token or "").strip()
    latest_access = initial_access
    proxy_norm = str(proxy or "").strip()
    proxy_candidates: List[Optional[str]] = [proxy_norm] if proxy_norm else [None]

    for proxy_item in proxy_candidates:
        session = cffi_requests.Session(
            impersonate="chrome120",
            proxy=proxy_item,
        )
        _seed_cookie_jar_from_text(session, cookies_text)

        did = str(device_id or "").strip()
        if did:
            try:
                session.cookies.set("oai-did", did, domain=".chatgpt.com", path="/")
            except Exception:
                pass

        headers = {
            "Accept": "application/json",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        access = latest_access
        if access:
            headers["Authorization"] = f"Bearer {access}"

        try:
            session.get(
                "https://chatgpt.com/",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://chatgpt.com/",
                    "User-Agent": headers["User-Agent"],
                },
                timeout=20,
            )
        except Exception:
            pass

        for _ in range(2):
            resp = session.get(
                "https://chatgpt.com/api/auth/session",
                headers=headers,
                timeout=25,
            )

            token = _extract_session_token_from_cookie_jar(getattr(resp, "cookies", None))
            if not token:
                token = _extract_session_token_from_cookie_jar(getattr(session, "cookies", None))
            if not token:
                set_cookie = (
                    " | ".join(resp.headers.get_list("set-cookie"))
                    if hasattr(resp.headers, "get_list")
                    else str(resp.headers.get("set-cookie") or "")
                )
                match_direct = re.search(r"__Secure-next-auth\.session-token=([^;,\s]+)", set_cookie)
                if match_direct:
                    token = str(match_direct.group(1) or "").strip()
                else:
                    chunk_matches = re.findall(r"__Secure-next-auth\.session-token\.(\d+)=([^;,\s]+)", set_cookie)
                    if chunk_matches:
                        chunk_map = {int(i): v for i, v in chunk_matches if str(i).isdigit()}
                        if chunk_map:
                            token = "".join(chunk_map[idx] for idx in sorted(chunk_map.keys()))

            fresh_access = ""
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                data = {}
            if isinstance(data, dict):
                fresh_access = str(data.get("accessToken") or "").strip()

            if token:
                return token, (fresh_access or access or initial_access)
            if fresh_access:
                access = fresh_access
                latest_access = fresh_access
                headers["Authorization"] = f"Bearer {fresh_access}"

            # 常见地区限制：带代理失败时自动切到下一候选（通常是直连）
            if proxy_item and resp.status_code in (401, 403) and _is_region_block_error_text(resp.text):
                break

    return "", latest_access


def _extract_session_token_from_auth_response(resp, session) -> str:
    token = _extract_session_token_from_cookie_jar(getattr(resp, "cookies", None))
    if token:
        return token
    token = _extract_session_token_from_cookie_jar(getattr(session, "cookies", None))
    if token:
        return token

    set_cookie = (
        " | ".join(resp.headers.get_list("set-cookie"))
        if hasattr(resp.headers, "get_list")
        else str(resp.headers.get("set-cookie") or "")
    )
    match_direct = re.search(r"__Secure-next-auth\.session-token=([^;,\s]+)", set_cookie)
    if match_direct:
        return str(match_direct.group(1) or "").strip()

    chunk_matches = re.findall(r"__Secure-next-auth\.session-token\.(\d+)=([^;,\s]+)", set_cookie)
    if chunk_matches:
        chunk_map = {int(i): v for i, v in chunk_matches if str(i).isdigit()}
        if chunk_map:
            return "".join(chunk_map[idx] for idx in sorted(chunk_map.keys()))
    return ""


def _merge_cookie_text_with_session_jar(cookies_text: Optional[str], session) -> str:
    merged = str(cookies_text or "").strip()
    try:
        items = list(session.cookies.items())
    except Exception:
        items = []
    for name, value in items:
        key = str(name or "").strip()
        val = str(value or "").strip()
        if not key or not val:
            continue
        merged = _upsert_cookie(merged, key, val)
    return merged


def _bootstrap_session_token_by_abcard_bridge(account: Account, proxy: Optional[str]) -> tuple[str, str, str]:
    """
    ABCard 同款 next-auth 会话桥接:
    1) /api/auth/csrf
    2) /api/auth/signin/openai
    3) /api/auth/session
    Returns:
        (session_token, fresh_access_token, merged_cookies_text)
    """
    session = cffi_requests.Session(
        impersonate="chrome120",
        proxy=proxy,
    )
    base_cookies = str(account.cookies or "").strip()
    _seed_cookie_jar_from_text(session, base_cookies)

    device_id = _resolve_account_device_id(account)
    if device_id:
        try:
            session.cookies.set("oai-did", device_id, domain=".chatgpt.com", path="/")
        except Exception:
            pass

    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    common_headers = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/auth/login",
        "Origin": "https://chatgpt.com",
    }

    try:
        session.get(
            "https://chatgpt.com/auth/login",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": ua,
                "Referer": "https://chatgpt.com/",
            },
            timeout=20,
        )
    except Exception:
        pass

    csrf_resp = session.get(
        "https://chatgpt.com/api/auth/csrf",
        headers=common_headers,
        timeout=25,
    )
    if csrf_resp.status_code >= 400:
        raise RuntimeError(f"csrf_failed_http_{csrf_resp.status_code}")

    try:
        csrf_token = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()
    except Exception:
        csrf_token = ""
    if not csrf_token:
        raise RuntimeError("csrf_token_missing")

    signin_resp = session.post(
        "https://chatgpt.com/api/auth/signin/openai",
        headers={
            **common_headers,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "csrfToken": csrf_token,
            "callbackUrl": "https://chatgpt.com/",
            "json": "true",
        },
        timeout=25,
    )
    if signin_resp.status_code >= 400:
        raise RuntimeError(f"signin_openai_failed_http_{signin_resp.status_code}")

    auth_url = ""
    try:
        auth_url = str((signin_resp.json() or {}).get("url") or "").strip()
    except Exception:
        auth_url = ""
    if auth_url:
        try:
            session.get(
                auth_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://chatgpt.com/auth/login",
                    "User-Agent": ua,
                },
                timeout=30,
                allow_redirects=True,
            )
        except Exception:
            pass

    latest_access = str(account.access_token or "").strip()
    session_headers = {
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
        "User-Agent": ua,
    }
    if latest_access:
        session_headers["Authorization"] = f"Bearer {latest_access}"

    session_token = ""
    for _ in range(2):
        resp = session.get(
            "https://chatgpt.com/api/auth/session",
            headers=session_headers,
            timeout=25,
        )
        if resp.status_code >= 400 and not latest_access:
            continue

        token_inner = _extract_session_token_from_auth_response(resp, session)
        if token_inner:
            session_token = token_inner

        try:
            payload = resp.json() if resp.content else {}
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            fresh_access = str(payload.get("accessToken") or "").strip()
            if fresh_access:
                latest_access = fresh_access
                session_headers["Authorization"] = f"Bearer {fresh_access}"

        if session_token:
            break

    merged_cookies = _merge_cookie_text_with_session_jar(base_cookies, session)
    if session_token:
        merged_cookies = _upsert_cookie(merged_cookies, "__Secure-next-auth.session-token", session_token)
    if device_id:
        merged_cookies = _upsert_cookie(merged_cookies, "oai-did", device_id)

    return session_token, latest_access, merged_cookies


def _normalize_email_service_config_for_session_bootstrap(
    service_type: EmailServiceType,
    config: Optional[dict],
    proxy_url: Optional[str] = None,
) -> dict:
    normalized = dict(config or {})

    if "api_url" in normalized and "base_url" not in normalized:
        normalized["base_url"] = normalized.pop("api_url")

    if service_type == EmailServiceType.MOE_MAIL:
        if "domain" in normalized and "default_domain" not in normalized:
            normalized["default_domain"] = normalized.pop("domain")
    elif service_type in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL):
        if "default_domain" in normalized and "domain" not in normalized:
            normalized["domain"] = normalized.pop("default_domain")
    elif service_type == EmailServiceType.DUCK_MAIL:
        if "domain" in normalized and "default_domain" not in normalized:
            normalized["default_domain"] = normalized.pop("domain")

    # IMAP/Outlook 等可按需使用代理；Temp-Mail/Freemail 强制直连。
    if proxy_url and "proxy_url" not in normalized and service_type not in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL):
        normalized["proxy_url"] = proxy_url

    return normalized


def _resolve_email_service_for_account_session_bootstrap(db, account: Account, proxy: Optional[str]):
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
        # Outlook/IMAP 优先匹配同邮箱配置，避免拿错账户。
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
        config = _normalize_email_service_config_for_session_bootstrap(service_type, selected.config, proxy)
    elif service_type == EmailServiceType.TEMPMAIL:
        config = {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
            "proxy_url": proxy,
        }
    else:
        raise RuntimeError(
            f"未找到可用邮箱服务配置(type={service_type.value})，无法自动获取登录验证码"
        )

    service = EmailServiceFactory.create(service_type, config, name=f"session_bootstrap_{service_type.value}")
    return service


def _bootstrap_session_token_by_relogin(db, account: Account, proxy: Optional[str]) -> str:
    """
    二级兜底：用账号邮箱+密码走一次登录链路，自动收 OTP 并补齐 session token。
    """
    email = str(account.email or "").strip()
    password = str(account.password or "").strip()
    if not email or not password:
        logger.info(
            "会话补全登录跳过：账号缺少邮箱或密码 account_id=%s email=%s",
            account.id,
            account.email,
        )
        return ""

    try:
        email_service = _resolve_email_service_for_account_session_bootstrap(db, account, proxy)
    except Exception as exc:
        logger.warning(
            "会话补全登录无法创建邮箱服务: account_id=%s email=%s error=%s",
            account.id,
            account.email,
            exc,
        )
        return ""

    engine = RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy,
        callback_logger=lambda msg: logger.info("会话补全登录: %s", msg),
        task_uuid=None,
    )
    engine.email = email
    engine.password = password
    engine.email_info = {"service_id": account.email_service_id} if account.email_service_id else {}

    try:
        did, sen_token = engine._prepare_authorize_flow("会话补全登录")
        if not did:
            return ""
        if not sen_token:
            # 对齐 ABCard：sentinel 偶发失败时，仍尝试无 sentinel 登录链路，避免卡死。
            logger.warning(
                "会话补全登录 sentinel 缺失，继续尝试无 sentinel 登录: account_id=%s email=%s",
                account.id,
                account.email,
            )

        login_start = engine._submit_login_start(did, sen_token)
        if not login_start.success:
            if _is_region_block_error_text(login_start.error_message):
                logger.warning(
                    "会话补全登录入口地区受限: account_id=%s email=%s proxy=%s error=%s",
                    account.id,
                    account.email,
                    "on" if proxy else "off",
                    login_start.error_message,
                )
            logger.warning(
                "会话补全登录入口失败: account_id=%s email=%s error=%s",
                account.id,
                account.email,
                login_start.error_message,
            )
            return ""

        if login_start.page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            password_result = engine._submit_login_password()
            if not password_result.success or not password_result.is_existing_account:
                logger.warning(
                    "会话补全登录密码阶段失败: account_id=%s email=%s page_type=%s err=%s",
                    account.id,
                    account.email,
                    password_result.page_type,
                    password_result.error_message,
                )
                return ""
        elif login_start.page_type != OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            logger.warning(
                "会话补全登录入口返回未知页面: account_id=%s email=%s page_type=%s",
                account.id,
                account.email,
                login_start.page_type,
            )
            return ""

        engine._log("等待登录验证码到场，最后这位嘉宾还在路上...")
        engine._log("核对登录验证码，验明正身一下...")
        if not engine._verify_email_otp_with_retry(stage_label="会话补全验证码", max_attempts=3):
            logger.warning(
                "会话补全登录验证码阶段失败: account_id=%s email=%s",
                account.id,
                account.email,
            )
            return ""

        fresh_cookies = engine._dump_session_cookies()
        # 兜底拼装关键 cookie，避免个别环境 cookie jar 导出不全。
        try:
            did_cookie = str(engine.session.cookies.get("oai-did") or "").strip() if engine.session else ""
        except Exception:
            did_cookie = ""
        try:
            auth_cookie = str(engine.session.cookies.get("oai-client-auth-session") or "").strip() if engine.session else ""
        except Exception:
            auth_cookie = ""
        if did_cookie:
            fresh_cookies = _upsert_cookie(fresh_cookies, "oai-did", did_cookie)
        if auth_cookie:
            fresh_cookies = _upsert_cookie(fresh_cookies, "oai-client-auth-session", auth_cookie)

        session_token = _extract_session_token_from_cookie_text(fresh_cookies)
        forced_access = str(account.access_token or "").strip()
        if not session_token:
            forced_token, forced_access_new = _force_fetch_nextauth_session_token(
                access_token=forced_access,
                cookies_text=fresh_cookies,
                device_id=did_cookie or _resolve_account_device_id(account),
                proxy=proxy,
            )
            if forced_token:
                session_token = forced_token
                fresh_cookies = _upsert_cookie(fresh_cookies, "__Secure-next-auth.session-token", forced_token)
            if forced_access_new:
                forced_access = forced_access_new

        if not session_token:
            logger.warning("会话补全登录未拿到 session_token: account_id=%s email=%s", account.id, account.email)
            if fresh_cookies:
                account.cookies = fresh_cookies
                if forced_access:
                    account.access_token = forced_access
                account.last_refresh = datetime.utcnow()
                db.commit()
            return ""

        if forced_access:
            account.access_token = forced_access
        if fresh_cookies:
            account.cookies = fresh_cookies
        account.session_token = session_token
        account.last_refresh = datetime.utcnow()
        db.commit()
        db.refresh(account)
        logger.info("会话补全登录成功: account_id=%s email=%s", account.id, account.email)
        return session_token
    except Exception as exc:
        logger.warning("会话补全登录异常: account_id=%s email=%s error=%s", account.id, account.email, exc)
        return ""


def _upsert_cookie(cookies_text: Optional[str], cookie_name: str, cookie_value: str) -> str:
    target_name = str(cookie_name or "").strip()
    target_value = str(cookie_value or "").strip()
    if not target_name:
        return str(cookies_text or "").strip()

    pairs: List[tuple[str, str]] = []
    seen = False
    for item in str(cookies_text or "").split(";"):
        raw = str(item or "").strip()
        if not raw or "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if name == target_name:
            if target_value:
                pairs.append((name, target_value))
            seen = True
        else:
            pairs.append((name, value))

    if not seen and target_value:
        pairs.append((target_name, target_value))

    return "; ".join(f"{k}={v}" for k, v in pairs if k)


def _seed_cookie_jar_from_text(session, cookies_text: Optional[str]) -> None:
    """
    将 account.cookies 中的键值回灌到会话 cookie jar，便于重定向链正确续会话。
    """
    text = str(cookies_text or "").strip()
    if not text:
        return
    for item in text.split(";"):
        raw = str(item or "").strip()
        if not raw or "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        key = str(name or "").strip()
        val = str(value or "").strip()
        if not key:
            continue
        for domain in (".chatgpt.com", "chatgpt.com"):
            try:
                session.cookies.set(key, val, domain=domain, path="/")
            except Exception:
                continue


def _bootstrap_session_token_for_local_auto(db, account: Account, proxy: Optional[str]) -> str:
    """
    尝试为 local_auto 自动补齐 session token（避免 cdp_session_missing）。
    """
    existing = str(account.session_token or "").strip() or _extract_session_token_from_cookie_text(account.cookies)
    if existing:
        if not account.session_token:
            account.session_token = existing
            account.cookies = _upsert_cookie(account.cookies, "__Secure-next-auth.session-token", existing)
            db.commit()
            db.refresh(account)
        return existing

    def _extract_session_from_response(resp, session) -> str:
        token_inner = _extract_session_token_from_cookie_jar(getattr(resp, "cookies", None))
        if token_inner:
            return token_inner
        token_inner = _extract_session_token_from_cookie_jar(getattr(session, "cookies", None))
        if token_inner:
            return token_inner
        set_cookie = (
            " | ".join(resp.headers.get_list("set-cookie"))
            if hasattr(resp.headers, "get_list")
            else str(resp.headers.get("set-cookie") or "")
        )
        match_direct = re.search(r"__Secure-next-auth\.session-token=([^;,\s]+)", set_cookie)
        if match_direct:
            return str(match_direct.group(1) or "").strip()
        chunk_matches = re.findall(r"__Secure-next-auth\.session-token\.(\d+)=([^;,\s]+)", set_cookie)
        if chunk_matches:
            chunk_map = {int(i): v for i, v in chunk_matches if str(i).isdigit()}
            if chunk_map:
                return "".join(chunk_map[idx] for idx in sorted(chunk_map.keys()))
        return ""

    def _request_session_token(
        *,
        proxy_item: Optional[str],
        with_auth: bool,
        with_cookies: bool,
    ) -> str:
        access_token = str(account.access_token or "").strip()
        if with_auth and not access_token:
            return ""

        session = cffi_requests.Session(
            impersonate="chrome120",
            proxy=proxy_item,
        )
        _seed_cookie_jar_from_text(session, account.cookies)

        device_id = _resolve_account_device_id(account)
        if device_id:
            try:
                session.cookies.set("oai-did", device_id, domain=".chatgpt.com")
            except Exception:
                pass

        headers = {
            "Accept": "application/json",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if with_auth:
            headers["Authorization"] = f"Bearer {access_token}"

        # 先热身主页，尽可能让 cookie/session 状态完整再请求 auth/session
        try:
            session.get(
                "https://chatgpt.com/",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://chatgpt.com/",
                    "User-Agent": headers["User-Agent"],
                },
                timeout=25,
            )
        except Exception:
            pass

        if with_cookies and account.cookies:
            try:
                session.headers.update({"cookie": str(account.cookies)})
            except Exception:
                pass

        resp = session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=25,
        )

        token_inner = _extract_session_from_response(resp, session)
        try:
            data = resp.json() if resp.content else {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            fresh_access = str(data.get("accessToken") or "").strip()
            if fresh_access:
                account.access_token = fresh_access

        if token_inner:
            # 若 session 接口返回了新 accessToken，一并回写，避免后续继续用旧 token。
            return token_inner

        # 部分场景 session cookie 在第二次调用才下发，这里补一次重试。
        retry_resp = session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=25,
        )
        token_retry = _extract_session_from_response(retry_resp, session)
        try:
            data = retry_resp.json() if retry_resp.content else {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            fresh_access = str(data.get("accessToken") or "").strip()
            if fresh_access:
                account.access_token = fresh_access
        if token_retry:
            return token_retry
        return ""

    # 按 ABCard 风格：先独立会话 + access token，再尝试带 cookies；每组先代理后直连。
    attempt_matrix = [
        (True, False),   # with_auth, with_cookies
        (True, True),
        (False, True),
    ]
    # 会话补全只走代理网络，避免直连触发地区限制导致 403 卡死。
    proxy_candidates = _build_proxy_candidates(proxy, account, include_direct=False)
    if not proxy_candidates:
        logger.warning(
            "本地自动绑卡会话补全缺少可用代理: account_id=%s email=%s",
            account.id,
            account.email,
        )
        return ""

    errors: List[str] = []
    token = ""
    for with_auth, with_cookies in attempt_matrix:
        for proxy_item in proxy_candidates:
            try:
                token = _request_session_token(
                    proxy_item=proxy_item,
                    with_auth=with_auth,
                    with_cookies=with_cookies,
                )
                if token:
                    break
            except Exception as exc:
                errors.append(
                    f"proxy={'on' if proxy_item else 'off'} auth={with_auth} cookies={with_cookies} err={exc}"
                )
        if token:
            break

    # 一级兜底：ABCard 同款 next-auth 桥接链路（csrf -> signin/openai -> auth/session）。
    if not token:
        for proxy_item in proxy_candidates:
            try:
                bridged_token, bridged_access, bridged_cookies = _bootstrap_session_token_by_abcard_bridge(
                    account=account,
                    proxy=proxy_item,
                )
                if bridged_access:
                    account.access_token = bridged_access
                if bridged_cookies:
                    account.cookies = bridged_cookies
                if bridged_token:
                    token = bridged_token
                    break
            except Exception as exc:
                errors.append(f"abcard_bridge proxy={'on' if proxy_item else 'off'} err={exc}")

    # 若仍失败，尝试刷新 token 后再跑一次核心路径（auth+无cookies）
    if not token and (account.refresh_token or account.session_token):
        try:
            manager = TokenRefreshManager(proxy_url=proxy)
            refresh_result = manager.refresh_account(account)
            if refresh_result.success:
                account.access_token = refresh_result.access_token
                if refresh_result.refresh_token:
                    account.refresh_token = refresh_result.refresh_token
                if refresh_result.expires_at:
                    account.expires_at = refresh_result.expires_at
                account.last_refresh = datetime.utcnow()
                db.commit()
                db.refresh(account)
                for proxy_item in proxy_candidates:
                    try:
                        token = _request_session_token(
                            proxy_item=proxy_item,
                            with_auth=True,
                            with_cookies=False,
                        )
                        if token:
                            break
                    except Exception as exc:
                        errors.append(f"after_refresh proxy={'on' if proxy_item else 'off'} err={exc}")
        except Exception as exc:
            errors.append(f"refresh_failed={exc}")

    if not token:
        # 二级兜底：走一次账号登录链路（含邮箱验证码）自动补会话。
        # 逐个代理候选尝试（显式/账号历史/全局），避免落到受限直连。
        for relogin_proxy in proxy_candidates:
            token = _bootstrap_session_token_by_relogin(
                db=db,
                account=account,
                proxy=relogin_proxy,
            )
            if token:
                break

    if not token:
        if errors:
            logger.warning(
                "本地自动绑卡会话补全失败: account_id=%s email=%s detail=%s",
                account.id,
                account.email,
                " | ".join(errors[-4:]),
            )
        else:
            logger.info(
                "本地自动绑卡会话补全未命中 session token: account_id=%s email=%s",
                account.id,
                account.email,
            )
        return ""

    account.session_token = token
    account.cookies = _upsert_cookie(account.cookies, "__Secure-next-auth.session-token", token)
    db.commit()
    db.refresh(account)
    logger.info(
        "本地自动绑卡会话补全成功: account_id=%s email=%s",
        account.id,
        account.email,
    )
    return token


def _build_official_checkout_url(checkout_session_id: Optional[str]) -> Optional[str]:
    cs_id = str(checkout_session_id or "").strip()
    if not cs_id:
        return None
    return f"https://chatgpt.com/checkout/openai_llc/{cs_id}"


def _mask_card_number(number: Optional[str]) -> str:
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    if not digits:
        return "-"
    if len(digits) <= 8:
        return f"{digits[:2]}****{digits[-2:]}"
    return f"{digits[:4]}****{digits[-4:]}"


def _mark_task_paid_pending_sync(task: BindCardTask, reason: str) -> None:
    now = datetime.utcnow()
    task.status = "paid_pending_sync"
    task.completed_at = None
    task.last_checked_at = now
    task.last_error = reason


def _promote_child_account_to_mother(account: Account, *, reason: str) -> bool:
    """
    历史兼容函数：关闭“订阅命中 plus/team 后自动子号升母号”。
    防止子号加入 Team 后被错误改为母号。
    """
    _ = account
    _ = reason
    return False


def _apply_subscription_result(
    account: Account,
    *,
    status: str,
    checked_at: datetime,
    confidence: Optional[str] = None,
    promote_reason: str = "subscription_upgrade",
) -> bool:
    normalized_status = str(status or "free").strip().lower()
    normalized_confidence = str(confidence or "").strip().lower()

    if normalized_status in ("plus", "team"):
        account.subscription_type = normalized_status
        account.subscription_at = checked_at
        return _promote_child_account_to_mother(account, reason=promote_reason)

    if normalized_status == "free" and normalized_confidence == "high":
        account.subscription_type = None
        account.subscription_at = None

    return False


def _vendor_progress_init(task_id: int) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    started_at_epoch = int(time.time())
    with _VENDOR_PROGRESS_LOCK:
        _VENDOR_PROGRESS_STATE[task_id] = {
            "status": "running",
            "progress": 0,
            "updated_at": now,
            "started_at_epoch": started_at_epoch,
            "force_stop_after_seconds": _VENDOR_FORCE_STOP_AFTER_SECONDS,
            "next_index": 0,
            "logs": [],
            "stop_requested": False,
        }


def _vendor_progress_log(
    task_id: int,
    message: str,
    *,
    progress: Optional[int] = None,
    status: Optional[str] = None,
) -> None:
    text = str(message or "").strip()
    if not text:
        return
    now = datetime.now().strftime("%H:%M:%S")
    with _VENDOR_PROGRESS_LOCK:
        state = _VENDOR_PROGRESS_STATE.setdefault(
            task_id,
            {
                "status": "running",
                "progress": 0,
                "updated_at": now,
                "started_at_epoch": int(time.time()),
                "force_stop_after_seconds": _VENDOR_FORCE_STOP_AFTER_SECONDS,
                "next_index": 0,
                "logs": [],
                "stop_requested": False,
            },
        )
        if progress is not None:
            try:
                state["progress"] = max(0, min(100, int(progress)))
            except Exception:
                pass
        if status:
            state["status"] = status
        state["updated_at"] = now
        idx = int(state.get("next_index") or 0)
        logs = list(state.get("logs") or [])
        logs.append({"index": idx, "time": now, "message": text})
        if len(logs) > _VENDOR_PROGRESS_LOG_MAX:
            logs = logs[-_VENDOR_PROGRESS_LOG_MAX:]
        state["logs"] = logs
        state["next_index"] = idx + 1
        _VENDOR_PROGRESS_STATE[task_id] = state


def _vendor_progress_snapshot(task_id: int, cursor: int = 0) -> Dict[str, Any]:
    with _VENDOR_PROGRESS_LOCK:
        state = dict(_VENDOR_PROGRESS_STATE.get(task_id) or {})
    logs = list(state.get("logs") or [])
    safe_cursor = max(0, int(cursor or 0))
    new_logs = [item for item in logs if int(item.get("index") or 0) >= safe_cursor]
    next_cursor = safe_cursor
    if logs:
        next_cursor = int(logs[-1].get("index") or 0) + 1
    started_at_epoch = int(state.get("started_at_epoch") or 0)
    now_epoch = int(time.time())
    elapsed_seconds = max(0, now_epoch - started_at_epoch) if started_at_epoch > 0 else 0
    force_stop_after_seconds = max(1, int(state.get("force_stop_after_seconds") or _VENDOR_FORCE_STOP_AFTER_SECONDS))
    return {
        "status": str(state.get("status") or "idle"),
        "progress": int(state.get("progress") or 0),
        "updated_at": state.get("updated_at"),
        "started_at_epoch": started_at_epoch if started_at_epoch > 0 else None,
        "elapsed_seconds": elapsed_seconds,
        "force_stop_after_seconds": force_stop_after_seconds,
        "can_force_stop": elapsed_seconds >= force_stop_after_seconds,
        "logs": new_logs,
        "next_cursor": next_cursor,
        "stop_requested": bool(state.get("stop_requested")),
    }


def _vendor_progress_exists(task_id: int) -> bool:
    with _VENDOR_PROGRESS_LOCK:
        return int(task_id) in _VENDOR_PROGRESS_STATE


def _vendor_request_stop(task_id: int) -> bool:
    now = datetime.now().strftime("%H:%M:%S")
    with _VENDOR_PROGRESS_LOCK:
        state = _VENDOR_PROGRESS_STATE.setdefault(
            task_id,
            {
                "status": "running",
                "progress": 0,
                "updated_at": now,
                "started_at_epoch": int(time.time()),
                "force_stop_after_seconds": _VENDOR_FORCE_STOP_AFTER_SECONDS,
                "next_index": 0,
                "logs": [],
                "stop_requested": False,
            },
        )
        already = bool(state.get("stop_requested"))
        state["stop_requested"] = True
        state["updated_at"] = now
        _VENDOR_PROGRESS_STATE[task_id] = state
    return not already


def _vendor_should_stop(task_id: int) -> bool:
    with _VENDOR_PROGRESS_LOCK:
        state = _VENDOR_PROGRESS_STATE.get(task_id) or {}
        return bool(state.get("stop_requested"))


def _vendor_get_latest_active_task_id() -> Optional[int]:
    with _VENDOR_PROGRESS_LOCK:
        items = []
        for raw_task_id, raw_state in (_VENDOR_PROGRESS_STATE or {}).items():
            try:
                task_id = int(raw_task_id)
            except Exception:
                continue
            state = dict(raw_state or {})
            status = str(state.get("status") or "running").strip().lower()
            if status in ("completed", "failed", "cancelled"):
                continue
            updated_at = str(state.get("updated_at") or "")
            items.append((task_id, updated_at))
    if not items:
        return None
    items.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return int(items[0][0])


def _payment_now_iso() -> str:
    return datetime.utcnow().isoformat()


def _cleanup_payment_op_tasks_locked() -> None:
    total = len(_PAYMENT_OP_TASKS)
    if total <= _PAYMENT_OP_TASK_MAX_KEEP:
        return

    overflow = total - _PAYMENT_OP_TASK_MAX_KEEP
    finished = [
        (task_id, _PAYMENT_OP_TASKS[task_id].get("_created_ts", 0))
        for task_id in _PAYMENT_OP_TASKS
        if _PAYMENT_OP_TASKS[task_id].get("status") in {"completed", "failed", "cancelled"}
    ]
    finished.sort(key=lambda item: item[1])
    removed = 0
    for task_id, _ in finished:
        if removed >= overflow:
            break
        _PAYMENT_OP_TASKS.pop(task_id, None)
        removed += 1


def _create_payment_op_task(task_type: str, *, bind_task_id: Optional[int] = None, progress: Optional[dict] = None) -> str:
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "task_type": str(task_type or "unknown"),
        "bind_task_id": int(bind_task_id) if bind_task_id else None,
        "status": "pending",
        "message": "任务已创建，等待执行",
        "created_at": _payment_now_iso(),
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
        "pause_requested": False,
        "paused": False,
        "progress": progress or {},
        "payload": {"bind_task_id": int(bind_task_id) if bind_task_id else None},
        "result": None,
        "error": None,
        "details": [],
        "_created_ts": time.time(),
    }
    with _PAYMENT_OP_TASK_LOCK:
        _PAYMENT_OP_TASKS[task_id] = task
        _cleanup_payment_op_tasks_locked()
    task_manager.register_domain_task(
        domain="payment",
        task_id=task_id,
        task_type=task_type,
        payload={"bind_task_id": int(bind_task_id) if bind_task_id else None},
        progress=task["progress"],
    )
    return task_id


def _get_payment_op_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _PAYMENT_OP_TASK_LOCK:
        return _PAYMENT_OP_TASKS.get(task_id)


def _get_payment_op_task_or_404(task_id: str) -> Dict[str, Any]:
    task = _get_payment_op_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="支付任务不存在")
    return task


def _update_payment_op_task(task_id: str, **fields) -> None:
    with _PAYMENT_OP_TASK_LOCK:
        task = _PAYMENT_OP_TASKS.get(task_id)
        if not task:
            return
        task.update(fields)
    task_manager.update_domain_task("payment", task_id, **fields)


def _set_payment_op_task_progress(task_id: str, **progress_fields) -> None:
    with _PAYMENT_OP_TASK_LOCK:
        task = _PAYMENT_OP_TASKS.get(task_id)
        if not task:
            return
        progress = task.setdefault("progress", {})
        progress.update(progress_fields)
    task_manager.set_domain_task_progress("payment", task_id, **(progress_fields or {}))


def _append_payment_op_task_detail(task_id: str, detail: dict, max_items: int = 400) -> None:
    with _PAYMENT_OP_TASK_LOCK:
        task = _PAYMENT_OP_TASKS.get(task_id)
        if not task:
            return
        details = task.setdefault("details", [])
        details.append(detail)
        if len(details) > max_items:
            task["details"] = details[-max_items:]
    task_manager.append_domain_task_detail("payment", task_id, detail, max_items=max_items)


def _is_payment_op_task_cancel_requested(task_id: str) -> bool:
    local_requested = False
    with _PAYMENT_OP_TASK_LOCK:
        task = _PAYMENT_OP_TASKS.get(task_id)
        local_requested = bool(task and task.get("cancel_requested"))
    return local_requested or task_manager.is_domain_task_cancel_requested("payment", task_id)


def _is_payment_op_task_pause_requested(task_id: str) -> bool:
    local_requested = False
    with _PAYMENT_OP_TASK_LOCK:
        task = _PAYMENT_OP_TASKS.get(task_id)
        local_requested = bool(task and task.get("pause_requested"))
    return local_requested or task_manager.is_domain_task_pause_requested("payment", task_id)


def _wait_if_payment_op_task_paused(task_id: str, running_message: str) -> bool:
    paused_once = False
    while True:
        if _is_payment_op_task_cancel_requested(task_id):
            return False
        if not _is_payment_op_task_pause_requested(task_id):
            if paused_once:
                _update_payment_op_task(
                    task_id,
                    status="running",
                    paused=False,
                    message=running_message,
                )
            return True
        if not paused_once:
            _update_payment_op_task(
                task_id,
                status="paused",
                paused=True,
                message="任务已暂停，等待继续",
            )
            paused_once = True
        time.sleep(0.35)


def _build_payment_op_task_snapshot(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": task.get("id"),
        "task_type": task.get("task_type"),
        "bind_task_id": task.get("bind_task_id"),
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


def _run_payment_op_task_guard(task_id: str, task_type: str, worker, *args) -> None:
    acquired, running, quota = task_manager.try_acquire_domain_slot("payment", task_id)
    if not acquired:
        reason = f"并发配额已满（running={running}, quota={quota}）"
        _update_payment_op_task(
            task_id,
            status="failed",
            finished_at=_payment_now_iso(),
            message=reason,
            error=reason,
            paused=False,
        )
        return
    try:
        worker(task_id, *args)
    except Exception as exc:
        logger.exception("支付异步任务异常: task_id=%s type=%s error=%s", task_id, task_type, exc)
        _update_payment_op_task(
            task_id,
            status="failed",
            finished_at=_payment_now_iso(),
            message=f"任务异常: {exc}",
            error=str(exc),
            paused=False,
        )
    finally:
        task_manager.release_domain_slot("payment", task_id)


def _vendor_fill_input_by_hints(target, hints: List[str], value: str) -> bool:
    try:
        return bool(
            target.evaluate(
                """
                ({ hints, value }) => {
                  const nodes = Array.from(
                    document.querySelectorAll("input, textarea, [contenteditable='true']")
                  );
                  const loweredHints = (hints || []).map(h => String(h || "").toLowerCase());
                  const loweredValue = String(value || "").toLowerCase();
                  const isUrlValue = loweredValue.includes("http") || loweredValue.includes("chatgpt.com/checkout") || loweredValue.includes("cs_live");
                  const isRedeemValue = /[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}/i.test(loweredValue);

                  const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.display === "none" || style.visibility === "hidden") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };

                  const collectLabelText = (el) => {
                    const parts = [];
                    const id = el.getAttribute("id");
                    if (id) {
                      document.querySelectorAll(`label[for="${CSS.escape(id)}"]`).forEach((node) => {
                        const t = (node.innerText || node.textContent || "").trim();
                        if (t) parts.push(t);
                      });
                    }
                    const parentLabel = el.closest("label");
                    if (parentLabel) {
                      const t = (parentLabel.innerText || parentLabel.textContent || "").trim();
                      if (t) parts.push(t);
                    }
                    const labelledBy = el.getAttribute("aria-labelledby");
                    if (labelledBy) {
                      labelledBy.split(/\s+/).forEach((idRef) => {
                        const node = document.getElementById(idRef);
                        const t = (node?.innerText || node?.textContent || "").trim();
                        if (t) parts.push(t);
                      });
                    }
                    const block = el.closest("div, section, form, article");
                    if (block) {
                      const t = (block.innerText || block.textContent || "").trim();
                      if (t) parts.push(t.slice(0, 220));
                    }
                    return parts.join(" ");
                  };

                  const setNativeValue = (el, val) => {
                    if (el && el.isContentEditable) {
                      el.focus();
                      el.textContent = val;
                    } else {
                      const proto = Object.getPrototypeOf(el);
                      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
                      if (descriptor && descriptor.set) {
                        descriptor.set.call(el, val);
                      } else {
                        el.value = val;
                      }
                      if ("selectionStart" in el && typeof el.value === "string") {
                        const len = el.value.length;
                        try {
                          el.setSelectionRange(len, len);
                        } catch (_) {}
                      }
                    }
                    el.dispatchEvent(new Event("input", { bubbles: true }));
                    el.dispatchEvent(new Event("change", { bubbles: true }));
                  };

                  const candidates = [];
                  for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    const attrs = [
                      el.getAttribute("name"),
                      el.getAttribute("id"),
                      el.getAttribute("placeholder"),
                      el.getAttribute("aria-label"),
                      el.getAttribute("data-testid"),
                      el.type,
                      el.className,
                    ].filter(Boolean).join(" ").toLowerCase();
                    const labels = collectLabelText(el).toLowerCase();
                    const haystack = `${attrs} ${labels}`.trim();
                    const scoreBase = loweredHints.reduce((acc, h) => acc + (h && haystack.includes(h) ? 2 : 0), 0);
                    let score = scoreBase;
                    if (isUrlValue) {
                      if (/(checkout|url|link|cs_live|支付链接)/i.test(haystack)) score += 4;
                      if (String(el.getAttribute("type") || "").toLowerCase() === "url") score += 3;
                    }
                    if (isRedeemValue) {
                      if (/(redeem|coupon|code|card|卡密|兑换|卡片|序列号)/i.test(haystack)) score += 4;
                      const maxLen = parseInt(el.getAttribute("maxlength") || "0", 10);
                      if (maxLen >= 16 && maxLen <= 32) score += 2;
                    }
                    candidates.push({ el, haystack, score });
                  }

                  candidates.sort((a, b) => b.score - a.score);
                  for (const item of candidates) {
                    if (item.score <= 0) continue;
                    try {
                      item.el.focus();
                      setNativeValue(item.el, value);
                      return true;
                    } catch (_) {}
                  }

                  // 关键词不命中时的兜底：
                  // checkout: 优先 url/link 风格输入框；redeem: 优先长度较短、常见 code 框。
                  const fallback = candidates.filter((x) => x.el && !x.el.disabled && !x.el.readOnly);
                  const rankFallback = (list) => {
                    return list.sort((a, b) => {
                      const aa = (a.el.getAttribute("placeholder") || "").toLowerCase();
                      const bb = (b.el.getAttribute("placeholder") || "").toLowerCase();
                      let as = 0;
                      let bs = 0;
                      if (isUrlValue) {
                        if (/(http|url|link|checkout|cs_)/i.test(aa)) as += 3;
                        if (/(http|url|link|checkout|cs_)/i.test(bb)) bs += 3;
                      } else if (isRedeemValue) {
                        if (/(code|redeem|coupon|card|兑换|卡片)/i.test(aa)) as += 3;
                        if (/(code|redeem|coupon|card|兑换|卡片)/i.test(bb)) bs += 3;
                        const am = parseInt(a.el.getAttribute("maxlength") || "0", 10);
                        const bm = parseInt(b.el.getAttribute("maxlength") || "0", 10);
                        if (am >= 16 && am <= 32) as += 2;
                        if (bm >= 16 && bm <= 32) bs += 2;
                      }
                      return bs - as;
                    });
                  };
                  const ranked = rankFallback(fallback);
                  for (const item of ranked) {
                    try {
                      item.el.focus();
                      setNativeValue(item.el, value);
                      return true;
                    } catch (_) {}
                  }
                  return false;
                }
                """,
                {"hints": hints, "value": value},
            )
        )
    except Exception:
        return False


def _vendor_fill_input_by_locator(target, hints: List[str], value: str) -> bool:
    lowered_hints = [str(h or "").strip().lower() for h in (hints or []) if str(h or "").strip()]
    lowered_value = str(value or "").strip().lower()
    is_url_value = (
        "http" in lowered_value
        or "chatgpt.com/checkout" in lowered_value
        or "cs_live" in lowered_value
        or "cs_test" in lowered_value
    )
    is_redeem_value = bool(re.search(r"^UK(?:-[A-Z0-9]{5}){5}$", str(value or "").strip(), re.IGNORECASE))

    def _try_fill(item) -> bool:
        try:
            if not item.is_visible():
                return False
        except Exception:
            return False
        try:
            item.scroll_into_view_if_needed(timeout=1200)
        except Exception:
            pass
        try:
            item.click(timeout=1200, force=True)
        except Exception:
            pass
        try:
            item.fill(str(value or ""), timeout=1500)
            return True
        except Exception:
            pass
        try:
            item.press("Control+A", timeout=800)
            item.type(str(value or ""), delay=0, timeout=1500)
            return True
        except Exception:
            return False

    # 1) 先按关键词定位 placeholder / label
    for hint in lowered_hints:
        try:
            regex = re.compile(re.escape(hint), re.IGNORECASE)
        except Exception:
            continue
        for getter in ("placeholder", "label"):
            try:
                locator = target.get_by_placeholder(regex) if getter == "placeholder" else target.get_by_label(regex)
                total = min(locator.count(), 6)
            except Exception:
                continue
            for idx in range(total):
                try:
                    if _try_fill(locator.nth(idx)):
                        return True
                except Exception:
                    continue

    # 2) 再遍历输入框做打分
    try:
        locator = target.locator("input, textarea")
        total = min(locator.count(), 80)
    except Exception:
        total = 0
        locator = None

    scored = []
    for idx in range(total):
        try:
            item = locator.nth(idx)
            if not item.is_visible():
                continue
            meta = item.evaluate(
                """
                (el) => {
                  const low = (v) => String(v || "").toLowerCase();
                  const attrs = [
                    el.getAttribute("name"),
                    el.getAttribute("id"),
                    el.getAttribute("placeholder"),
                    el.getAttribute("aria-label"),
                    el.getAttribute("data-testid"),
                    el.getAttribute("class"),
                    el.getAttribute("type"),
                  ].map(low).join(" ");
                  let blockText = "";
                  try {
                    const b = el.closest("div, section, form, article");
                    if (b) blockText = low((b.innerText || b.textContent || "").slice(0, 180));
                  } catch (_) {}
                  const maxlength = parseInt(el.getAttribute("maxlength") || "0", 10) || 0;
                  const disabled = !!el.disabled;
                  const readonly = !!el.readOnly;
                  return { attrs, blockText, maxlength, disabled, readonly };
                }
                """
            )
            if not isinstance(meta, dict):
                continue
            if bool(meta.get("disabled")):
                continue
            haystack = f"{meta.get('attrs', '')} {meta.get('blockText', '')}".strip()
            score = 0
            for hint in lowered_hints:
                if hint and hint in haystack:
                    score += 2
            if is_url_value:
                if re.search(r"(checkout|session|token|link|url|cs_live|cs_test|支付链接|结账链接)", haystack, re.IGNORECASE):
                    score += 4
            if is_redeem_value:
                if re.search(r"(redeem|cdk|code|coupon|激活|兑换|卡密|卡片|序列号)", haystack, re.IGNORECASE):
                    score += 4
                maxlength = int(meta.get("maxlength") or 0)
                if 16 <= maxlength <= 40:
                    score += 2
            scored.append((score, idx))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    for score, idx in scored:
        if score <= 0:
            continue
        try:
            if _try_fill(locator.nth(idx)):
                return True
        except Exception:
            continue

    # 3) 最后兜底：可见输入框依次尝试
    for idx in range(total):
        try:
            if _try_fill(locator.nth(idx)):
                return True
        except Exception:
            continue

    # 4) contenteditable 兜底
    try:
        c_locator = target.locator("[contenteditable='true']")
        c_total = min(c_locator.count(), 20)
    except Exception:
        c_total = 0
        c_locator = None
    for idx in range(c_total):
        try:
            item = c_locator.nth(idx)
            if not item.is_visible():
                continue
            item.scroll_into_view_if_needed(timeout=1000)
            item.click(timeout=1000, force=True)
            ok = bool(
                item.evaluate(
                    "(el, val) => { el.textContent = String(val || ''); el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); return true; }",
                    str(value or ""),
                )
            )
            if ok:
                return True
        except Exception:
            continue

    return False


def _vendor_fill_locator_item(item, value: str) -> bool:
    try:
        if not item.is_visible():
            return False
    except Exception:
        return False
    try:
        item.scroll_into_view_if_needed(timeout=1200)
    except Exception:
        pass
    try:
        item.click(timeout=1200, force=True)
    except Exception:
        pass
    try:
        item.fill(str(value or ""), timeout=1500)
        return True
    except Exception:
        pass
    try:
        item.press("Control+A", timeout=800)
        item.type(str(value or ""), delay=0, timeout=1500)
        return True
    except Exception:
        return False


def _vendor_fill_redeem_input(target, redeem_code: str) -> bool:
    selectors = (
        "input[placeholder*='卡密']",
        "textarea[placeholder*='卡密']",
        "input[placeholder*='激活码']",
        "textarea[placeholder*='激活码']",
        "input[placeholder*='CODE-']",
        "textarea[placeholder*='CODE-']",
        "input[placeholder*='US-']",
        "textarea[placeholder*='US-']",
        "input[name*='code' i]",
        "textarea[name*='code' i]",
        "input[id*='code' i]",
        "textarea[id*='code' i]",
        "input[name*='redeem' i]",
        "input[id*='redeem' i]",
        "input[name*='cdk' i]",
        "input[id*='cdk' i]",
    )
    for selector in selectors:
        try:
            locator = target.locator(selector)
            total = min(locator.count(), 8)
        except Exception:
            continue
        for idx in range(total):
            try:
                if _vendor_fill_locator_item(locator.nth(idx), redeem_code):
                    return True
            except Exception:
                continue

    # 标签附近输入框兜底（覆盖“卡密 / 激活码”这类文本与输入框分离的结构）
    try:
        ok = bool(
            target.evaluate(
                """
                ({ code }) => {
                  const isVisible = (el) => {
                    if (!el) return false;
                    try {
                      const st = window.getComputedStyle(el);
                      if (!st) return false;
                      if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity || 1) === 0) return false;
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    } catch (_) { return false; }
                  };
                  const all = Array.from(document.querySelectorAll("input, textarea"));
                  const hit = all.find((el) => {
                    if (!isVisible(el)) return false;
                    const attrs = [
                      el.getAttribute("placeholder"),
                      el.getAttribute("aria-label"),
                      el.getAttribute("name"),
                      el.getAttribute("id"),
                    ].join(" ").toLowerCase();
                    if (/(卡密|激活码|redeem|cdk|coupon|code)/i.test(attrs)) return true;
                    const block = el.closest("div, section, form, article");
                    const text = String(block?.innerText || "").toLowerCase();
                    return /(卡密\\s*\\/\\s*激活码|请输入您的卡密兑换码|验证兑换码)/i.test(text);
                  });
                  if (!hit) return false;
                  hit.focus();
                  try {
                    const proto = Object.getPrototypeOf(hit);
                    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
                    if (descriptor && descriptor.set) descriptor.set.call(hit, String(code || ""));
                    else hit.value = String(code || "");
                  } catch (_) {
                    hit.value = String(code || "");
                  }
                  hit.dispatchEvent(new Event("input", { bubbles: true }));
                  hit.dispatchEvent(new Event("change", { bubbles: true }));
                  return true;
                }
                """,
                {"code": str(redeem_code or "")},
            )
        )
        if ok:
            return True
    except Exception:
        pass
    return False


def _vendor_force_fill_first_text_input(target, value: str) -> bool:
    try:
        ok = bool(
            target.evaluate(
                """
                ({ value }) => {
                  const val = String(value || "");
                  const isVisible = (el) => {
                    if (!el) return false;
                    try {
                      const st = window.getComputedStyle(el);
                      if (!st) return false;
                      if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity || 1) === 0) return false;
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    } catch (_) { return false; }
                  };
                  const setVal = (el) => {
                    try {
                      if (el.isContentEditable || String(el.getAttribute("contenteditable") || "").toLowerCase() === "true") {
                        el.focus();
                        el.textContent = val;
                      } else {
                        const proto = Object.getPrototypeOf(el);
                        const descriptor = proto ? Object.getOwnPropertyDescriptor(proto, "value") : null;
                        if (descriptor && descriptor.set) descriptor.set.call(el, val);
                        else el.value = val;
                      }
                      el.dispatchEvent(new Event("input", { bubbles: true }));
                      el.dispatchEvent(new Event("change", { bubbles: true }));
                      return true;
                    } catch (_) {
                      return false;
                    }
                  };

                  // 1) 优先“卡密/激活码/兑换码”文本锚点附近
                  const anchors = Array.from(document.querySelectorAll("label, div, span, p, h1, h2, h3, h4, h5, h6"))
                    .filter((el) => {
                      if (!isVisible(el)) return false;
                      const t = String(el.innerText || el.textContent || "").toLowerCase();
                      return /(卡密\\s*\\/\\s*激活码|验证兑换码|请输入您的卡密兑换码|激活码|兑换码|cdk|redeem|coupon code)/i.test(t);
                    });

                  for (const anchor of anchors.slice(0, 12)) {
                    const box = anchor.closest("div, section, form, article") || anchor.parentElement || document.body;
                    if (!box) continue;
                    const fields = Array.from(box.querySelectorAll("input, textarea, [contenteditable='true'], [role='textbox']"));
                    for (const f of fields) {
                      if (!isVisible(f)) continue;
                      if (setVal(f)) return true;
                    }
                  }

                  // 2) 再尝试页面第一个可见文本输入
                  const fields = Array.from(document.querySelectorAll("input, textarea, [contenteditable='true'], [role='textbox']"));
                  for (const f of fields) {
                    if (!isVisible(f)) continue;
                    const type = String(f.getAttribute("type") || "").toLowerCase();
                    if (["checkbox", "radio", "button", "submit", "hidden"].includes(type)) continue;
                    if (setVal(f)) return true;
                  }
                  return false;
                }
                """,
                {"value": str(value or "")},
            )
        )
        return ok
    except Exception:
        return False


def _vendor_force_fill_first_text_input_any_frame(page, value: str) -> bool:
    try:
        if _vendor_force_fill_first_text_input(page, value):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_force_fill_first_text_input(frame, value):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_fill_redeem_input_any_frame(page, redeem_code: str) -> bool:
    try:
        if _vendor_fill_redeem_input(page, redeem_code):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_fill_redeem_input(frame, redeem_code):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_has_redeem_input_any_frame(page) -> bool:
    selectors = (
        "input[placeholder*='卡密']",
        "textarea[placeholder*='卡密']",
        "input[placeholder*='激活码']",
        "textarea[placeholder*='激活码']",
        "input[name*='code' i], textarea[name*='code' i], input[id*='code' i], textarea[id*='code' i]",
        "input[name*='redeem' i], input[id*='redeem' i], input[name*='cdk' i], input[id*='cdk' i]",
    )
    targets = [page]
    try:
        targets.extend(list(page.frames or []))
    except Exception:
        pass
    for target in targets:
        for selector in selectors:
            try:
                locator = target.locator(selector)
                total = min(locator.count(), 6)
            except Exception:
                continue
            for idx in range(total):
                try:
                    if locator.nth(idx).is_visible():
                        return True
                except Exception:
                    continue
    return False


def _vendor_fill_input_any_frame(page, hints: List[str], value: str) -> bool:
    try:
        if _vendor_fill_input_by_locator(page, hints, value):
            return True
    except Exception:
        pass
    try:
        if _vendor_fill_input_by_hints(page, hints, value):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_fill_input_by_locator(frame, hints, value):
                    return True
            except Exception:
                continue
            try:
                if _vendor_fill_input_by_hints(frame, hints, value):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_collect_input_descriptors(target, limit: int = 14) -> List[str]:
    # 先走 Playwright 定位器（更稳，兼容动态节点）
    try:
        out: List[str] = []
        locator = target.locator("input, textarea, [contenteditable='true']")
        total = min(locator.count(), max(4, int(limit or 14)) * 3)
        for idx in range(total):
            try:
                item = locator.nth(idx)
                if not item.is_visible():
                    continue
                info = item.evaluate(
                    """
                    (el) => {
                      const low = (v) => String(v || "").replace(/\\s+/g, " ").trim();
                      return [
                        String(el.tagName || "").toLowerCase(),
                        low(el.getAttribute("placeholder")),
                        low(el.getAttribute("aria-label")),
                        low(el.getAttribute("name")),
                        low(el.getAttribute("id")),
                        low(el.getAttribute("type")),
                      ].join(":");
                    }
                    """
                )
                text = str(info or "").strip()
                if text and text not in out:
                    out.append(text[:120])
                if len(out) >= max(4, int(limit or 14)):
                    return out
            except Exception:
                continue
        if out:
            return out
    except Exception:
        pass

    try:
        rows = target.evaluate(
            """
            ({ limit }) => {
              const out = [];
              const isVisible = (el) => {
                if (!el) return false;
                try {
                  const st = window.getComputedStyle(el);
                  if (!st) return false;
                  if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity || 1) === 0) return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                } catch (_) { return false; }
              };
              const nodes = Array.from(document.querySelectorAll("input, textarea, [contenteditable='true']"));
              for (const el of nodes) {
                if (!isVisible(el)) continue;
                const tag = String(el.tagName || "").toLowerCase();
                const desc = [
                  el.getAttribute("placeholder") || "",
                  el.getAttribute("aria-label") || "",
                  el.getAttribute("name") || "",
                  el.getAttribute("id") || "",
                  el.getAttribute("type") || "",
                ].join(" ").replace(/\\s+/g, " ").trim();
                const text = `${tag}:${desc || "(no-attrs)"}`.slice(0, 90);
                if (text && !out.includes(text)) out.push(text);
                if (out.length >= Number(limit || 14)) break;
              }
              return out;
            }
            """,
            {"limit": max(4, int(limit or 14))},
        )
        if isinstance(rows, list):
            return [str(x).strip() for x in rows if str(x or "").strip()]
    except Exception:
        pass
    return []


def _vendor_collect_input_descriptors_any_frame(page, limit: int = 18) -> List[str]:
    merged: List[str] = []
    try:
        for text in _vendor_collect_input_descriptors(page, limit=limit):
            if text not in merged:
                merged.append(text)
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                for text in _vendor_collect_input_descriptors(frame, limit=limit):
                    if text not in merged:
                        merged.append(text)
                    if len(merged) >= limit:
                        return merged[:limit]
            except Exception:
                continue
    except Exception:
        pass
    return merged[:limit]


def _normalize_vendor_redeem_code(code: Optional[str]) -> str:
    text = str(code or "").strip().upper()
    compact = re.sub(r"[^A-Z0-9]", "", text)
    if compact.startswith("UK"):
        body = compact[2:]
        if len(body) == 25:
            return f"UK-{body[0:5]}-{body[5:10]}-{body[10:15]}-{body[15:20]}-{body[20:25]}"
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def _vendor_seed_checkout_context(page, checkout_url: str) -> None:
    try:
        page.evaluate(
            """
            ({ checkoutUrl }) => {
              const val = String(checkoutUrl || "");
              if (!val) return false;
              const keys = [
                "checkout",
                "checkout_url",
                "checkoutUrl",
                "payment_link",
                "paymentLink",
                "url",
                "link",
              ];
              for (const key of keys) {
                try { localStorage.setItem(key, val); } catch (_) {}
                try { sessionStorage.setItem(key, val); } catch (_) {}
              }

              const selectors = [
                "input[name='checkout']",
                "input[name='checkout_url']",
                "input[id*='checkout']",
                "input[placeholder*='checkout' i]",
                "textarea[name='checkout']",
                "textarea[name='checkout_url']",
                "textarea[id*='checkout']",
                "textarea[placeholder*='checkout' i]",
              ];
              const setNativeValue = (el, v) => {
                const proto = Object.getPrototypeOf(el);
                const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
                if (descriptor && descriptor.set) {
                  descriptor.set.call(el, v);
                } else {
                  el.value = v;
                }
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
              };
              for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (!el) continue;
                try { setNativeValue(el, val); } catch (_) {}
              }

              window.__VENDOR_CHECKOUT_URL__ = val;
              try {
                document.dispatchEvent(new CustomEvent("checkout:prefill", { detail: { checkoutUrl: val } }));
              } catch (_) {}
              return true;
            }
            """,
            {"checkoutUrl": checkout_url},
        )
    except Exception:
        pass


def _regenerate_vendor_checkout_for_task(
    db,
    *,
    task: BindCardTask,
    account: Account,
    proxy: Optional[str],
) -> Optional[str]:
    req = CheckoutRequestBase(
        account_id=account.id,
        plan_type=str(task.plan_type or "plus"),
        workspace_name=str(task.workspace_name or "MyTeam"),
        price_interval=str(task.price_interval or "month"),
        seat_quantity=int(task.seat_quantity or 5),
        proxy=proxy,
        country=_normalize_checkout_country(task.country),
        currency=_normalize_checkout_currency(_normalize_checkout_country(task.country), task.currency),
    )
    link, source, _fallback_reason, checkout_session_id, publishable_key, client_secret = _generate_checkout_link_for_account(
        account=account,
        request=req,
        proxy=proxy,
    )
    link = str(link or "").strip()
    if not link:
        return None

    task.checkout_url = link
    task.checkout_source = source
    task.checkout_session_id = checkout_session_id
    task.publishable_key = publishable_key
    task.client_secret = client_secret
    task.last_checked_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    return link


def _regenerate_vendor_checkout_for_task_with_retry(
    db,
    *,
    task: BindCardTask,
    account: Account,
    explicit_proxy: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    last_error: Optional[Exception] = None
    for proxy_item in _build_proxy_candidates(explicit_proxy, account, include_direct=True):
        try:
            link = _regenerate_vendor_checkout_for_task(
                db,
                task=task,
                account=account,
                proxy=proxy_item,
            )
            if link:
                return link, proxy_item
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise RuntimeError("未生成到有效 checkout 链接")


def _vendor_click_button_by_hints(target, hints: List[str], exclude_hints: Optional[List[str]] = None) -> bool:
    try:
        return bool(
            target.evaluate(
                """
                ({ hints, excludeHints }) => {
                  const loweredHints = (hints || []).map(h => String(h || "").toLowerCase());
                  const loweredExclude = (excludeHints || []).map(h => String(h || "").toLowerCase());
                  const nodes = Array.from(
                    document.querySelectorAll(
                      "button, input[type='button'], input[type='submit'], a[role='button'], [role='button'], .btn, .button, .ant-btn, .el-button, [onclick]"
                    )
                  );
                  const isDisabled = (el) => {
                    if (!el) return true;
                    try {
                      if (el.disabled) return true;
                      const ariaDisabled = String(el.getAttribute("aria-disabled") || "").toLowerCase();
                      if (ariaDisabled === "true") return true;
                      const cls = String(el.className || "").toLowerCase();
                      if (/(disabled|is-disabled|btn-disabled|ant-btn-disabled)/i.test(cls)) return true;
                    } catch (_) {}
                    return false;
                  };
                  const clickNode = (el) => {
                    if (!el || isDisabled(el)) return false;
                    try {
                      el.scrollIntoView({ behavior: "instant", block: "center", inline: "center" });
                    } catch (_) {}
                    try { el.click(); } catch (_) {}
                    try { el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })); } catch (_) {}
                    return true;
                  };
                  for (const el of nodes) {
                    const text = (el.innerText || el.textContent || el.value || "").trim().toLowerCase();
                    if (!text) continue;
                    if (loweredExclude.some(h => h && text.includes(h))) continue;
                    if (!loweredHints.some(h => text.includes(h))) continue;
                    if (clickNode(el)) return true;
                  }

                  // 兜底：部分站点把可点击控件做成 div/span
                  const loose = Array.from(document.querySelectorAll("div, span, a"));
                  for (const el of loose) {
                    const text = (el.innerText || el.textContent || "").trim().toLowerCase();
                    if (!text || text.length > 24) continue;
                    if (loweredExclude.some(h => h && text.includes(h))) continue;
                    if (!loweredHints.some(h => text.includes(h))) continue;
                    const cls = String(el.className || "").toLowerCase();
                    const role = String(el.getAttribute("role") || "").toLowerCase();
                    const styleCursor = (() => { try { return String(window.getComputedStyle(el).cursor || "").toLowerCase(); } catch (_) { return ""; } })();
                    if (!(role === "button" || /(btn|button|click|action|submit)/i.test(cls) || styleCursor === "pointer")) {
                      continue;
                    }
                    if (clickNode(el)) return true;
                  }
                  return false;
                }
                """,
                {"hints": hints, "excludeHints": exclude_hints or []},
            )
        )
    except Exception:
        return False


def _vendor_click_button_any_frame(page, hints: List[str], exclude_hints: Optional[List[str]] = None) -> bool:
    if _vendor_click_button_by_locator_any_frame(page, hints, exclude_hints):
        return True
    try:
        if _vendor_click_button_by_hints(page, hints, exclude_hints):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_click_button_by_hints(frame, hints, exclude_hints):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_click_button_by_locator(target, hints: List[str], exclude_hints: Optional[List[str]] = None) -> bool:
    lowered_hints = [str(h or "").strip().lower() for h in (hints or []) if str(h or "").strip()]
    lowered_excludes = [str(h or "").strip().lower() for h in (exclude_hints or []) if str(h or "").strip()]
    if not lowered_hints:
        return False

    selectors = (
        "button",
        "[role='button']",
        "a",
        ".btn",
        ".button",
        ".ant-btn",
        ".el-button",
        "[onclick]",
        "div",
        "span",
    )
    for hint in lowered_hints:
        try:
            regex = re.compile(re.escape(hint), re.IGNORECASE)
        except Exception:
            continue
        for selector in selectors:
            try:
                locator = target.locator(selector, has_text=regex)
                total = min(locator.count(), 5)
            except Exception:
                continue
            for idx in range(total):
                try:
                    item = locator.nth(idx)
                    if not item.is_visible():
                        continue
                    text = str(item.inner_text() or "").strip().lower()
                    if not text:
                        continue
                    if any(ex and ex in text for ex in lowered_excludes):
                        continue
                    item.scroll_into_view_if_needed(timeout=1000)
                    item.click(timeout=1200, force=True)
                    return True
                except Exception:
                    continue
    return False


def _vendor_click_button_by_locator_any_frame(page, hints: List[str], exclude_hints: Optional[List[str]] = None) -> bool:
    try:
        if _vendor_click_button_by_locator(page, hints, exclude_hints):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_click_button_by_locator(frame, hints, exclude_hints):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_click_button_near_input_value(
    target,
    *,
    input_value: str,
    hints: List[str],
    exclude_hints: Optional[List[str]] = None,
) -> bool:
    try:
        return bool(
            target.evaluate(
                """
                ({ inputValue, hints, excludeHints }) => {
                  const normalized = String(inputValue || "").toLowerCase().replace(/[^a-z0-9]/g, "");
                  if (!normalized) return false;
                  const loweredHints = (hints || []).map(h => String(h || "").toLowerCase()).filter(Boolean);
                  const loweredExclude = (excludeHints || []).map(h => String(h || "").toLowerCase()).filter(Boolean);
                  const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
                  const txt = (el) => String(el?.innerText || el?.textContent || el?.value || "").trim().toLowerCase();
                  const isDisabled = (el) => {
                    if (!el) return true;
                    try {
                      if (el.disabled) return true;
                      const ad = String(el.getAttribute("aria-disabled") || "").toLowerCase();
                      if (ad === "true") return true;
                      const cls = String(el.className || "").toLowerCase();
                      if (/(disabled|is-disabled|btn-disabled|ant-btn-disabled)/i.test(cls)) return true;
                    } catch (_) {}
                    return false;
                  };
                  const clickNode = (el) => {
                    if (!el || isDisabled(el)) return false;
                    try { el.scrollIntoView({ behavior: "instant", block: "center", inline: "center" }); } catch (_) {}
                    try { el.click(); } catch (_) {}
                    try { el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })); } catch (_) {}
                    return true;
                  };
                  const matchButtonText = (text) => {
                    const low = String(text || "").toLowerCase();
                    if (!low) return false;
                    if (loweredExclude.some(h => h && low.includes(h))) return false;
                    return loweredHints.some(h => h && low.includes(h));
                  };

                  const allInputs = Array.from(document.querySelectorAll("input, textarea"));
                  const nearInputs = allInputs.filter((el) => {
                    try {
                      const v = norm(el.value);
                      return !!v && (v.includes(normalized) || normalized.includes(v));
                    } catch (_) {
                      return false;
                    }
                  });
                  if (!nearInputs.length) return false;

                  for (const inputEl of nearInputs) {
                    const containers = [];
                    let node = inputEl;
                    for (let i = 0; i < 5 && node; i += 1) {
                      node = node.parentElement;
                      if (!node) break;
                      containers.push(node);
                      if (node.matches && (node.matches("form, section, article") || node.getAttribute("role") === "dialog")) break;
                    }
                    for (const box of containers) {
                      const btns = Array.from(
                        box.querySelectorAll("button, input[type='button'], input[type='submit'], [role='button'], a, .btn, .button, .ant-btn, .el-button, [onclick]")
                      );
                      for (const btn of btns) {
                        if (!matchButtonText(txt(btn))) continue;
                        if (clickNode(btn)) return true;
                      }
                    }
                  }
                  return false;
                }
                """,
                {
                    "inputValue": str(input_value or ""),
                    "hints": hints,
                    "excludeHints": exclude_hints or [],
                },
            )
        )
    except Exception:
        return False


def _vendor_click_button_near_input_value_any_frame(
    page,
    *,
    input_value: str,
    hints: List[str],
    exclude_hints: Optional[List[str]] = None,
) -> bool:
    try:
        if _vendor_click_button_near_input_value(
            page,
            input_value=input_value,
            hints=hints,
            exclude_hints=exclude_hints,
        ):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_click_button_near_input_value(
                    frame,
                    input_value=input_value,
                    hints=hints,
                    exclude_hints=exclude_hints,
                ):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_has_input_by_hints(target, hints: List[str]) -> bool:
    try:
        return bool(
            target.evaluate(
                """
                ({ hints }) => {
                  const loweredHints = (hints || []).map(h => String(h || "").toLowerCase()).filter(Boolean);
                  const nodes = Array.from(document.querySelectorAll("input, textarea"));
                  const norm = (v) => String(v || "").toLowerCase();
                  for (const el of nodes) {
                    const txt = [
                      el.getAttribute("name"),
                      el.getAttribute("id"),
                      el.getAttribute("placeholder"),
                      el.getAttribute("aria-label"),
                      el.getAttribute("data-placeholder"),
                    ].map(norm).join(" ");
                    if (!txt) continue;
                    if (loweredHints.some(h => txt.includes(h))) return true;
                  }
                  return false;
                }
                """,
                {"hints": hints},
            )
        )
    except Exception:
        return False


def _vendor_has_input_any_frame(page, hints: List[str]) -> bool:
    try:
        if _vendor_has_input_by_hints(page, hints):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_has_input_by_hints(frame, hints):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_has_button_by_hints(target, hints: List[str], exclude_hints: Optional[List[str]] = None) -> bool:
    try:
        return bool(
            target.evaluate(
                """
                ({ hints, excludeHints }) => {
                  const loweredHints = (hints || []).map(h => String(h || "").toLowerCase()).filter(Boolean);
                  const loweredExclude = (excludeHints || []).map(h => String(h || "").toLowerCase()).filter(Boolean);
                  const nodes = Array.from(
                    document.querySelectorAll("button, input[type='button'], input[type='submit'], [role='button'], a, .btn, .button, .ant-btn, .el-button, [onclick]")
                  );
                  for (const el of nodes) {
                    const text = String(el.innerText || el.textContent || el.value || "").trim().toLowerCase();
                    if (!text) continue;
                    if (loweredExclude.some(h => h && text.includes(h))) continue;
                    if (loweredHints.some(h => text.includes(h))) return true;
                  }
                  return false;
                }
                """,
                {"hints": hints, "excludeHints": exclude_hints or []},
            )
        )
    except Exception:
        return False


def _vendor_has_button_any_frame(page, hints: List[str], exclude_hints: Optional[List[str]] = None) -> bool:
    try:
        if _vendor_has_button_by_hints(page, hints, exclude_hints):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_has_button_by_hints(frame, hints, exclude_hints):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_has_text_by_locator(target, hints: List[str]) -> bool:
    lowered_hints = [str(h or "").strip().lower() for h in (hints or []) if str(h or "").strip()]
    if not lowered_hints:
        return False
    for hint in lowered_hints:
        try:
            regex = re.compile(re.escape(hint), re.IGNORECASE)
            locator = target.get_by_text(regex)
            total = min(locator.count(), 5)
            for idx in range(total):
                try:
                    if locator.nth(idx).is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _vendor_has_text_any_frame_by_locator(page, hints: List[str]) -> bool:
    try:
        if _vendor_has_text_by_locator(page, hints):
            return True
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                if _vendor_has_text_by_locator(frame, hints):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _vendor_collect_button_texts(target, limit: int = 16) -> List[str]:
    try:
        rows = target.evaluate(
            """
            ({ limit }) => {
              const isVisible = (el) => {
                if (!el) return false;
                try {
                  const st = window.getComputedStyle(el);
                  if (!st) return false;
                  if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity || 1) === 0) return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                } catch (_) {
                  return false;
                }
              };
              const nodes = Array.from(
                document.querySelectorAll("button, input[type='button'], input[type='submit'], [role='button'], a, .btn, .button, .ant-btn, .el-button, [onclick]")
              );
              const out = [];
              for (const el of nodes) {
                if (!isVisible(el)) continue;
                const t = String(el.innerText || el.textContent || el.value || "").replace(/\\s+/g, " ").trim();
                if (!t || t.length > 26) continue;
                if (!out.includes(t)) out.push(t);
                if (out.length >= Number(limit || 16)) break;
              }
              return out;
            }
            """,
            {"limit": max(4, int(limit or 16))},
        )
        if isinstance(rows, list):
            return [str(x).strip() for x in rows if str(x or "").strip()]
    except Exception:
        pass
    return []


def _vendor_collect_button_texts_any_frame(page, limit: int = 20) -> List[str]:
    merged: List[str] = []
    try:
        for text in _vendor_collect_button_texts(page, limit=limit):
            if text not in merged:
                merged.append(text)
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                for text in _vendor_collect_button_texts(frame, limit=limit):
                    if text not in merged:
                        merged.append(text)
                    if len(merged) >= limit:
                        return merged[:limit]
            except Exception:
                continue
    except Exception:
        pass
    return merged[:limit]


def _vendor_is_stage2_ready_any_frame(page) -> bool:
    if _vendor_has_text_any_frame_by_locator(
        page,
        ["填写测试信息", "获取 checkout session", "直接输入 token", "开始测试", "下一步开始测试"],
    ):
        return True

    has_checkout_input = _vendor_has_input_any_frame(
        page,
        ["checkout", "checkout session", "checkout_url", "cs_live", "cs_id", "token", "链接", "支付链接", "结账链接"],
    )
    has_stage2_controls = _vendor_has_button_any_frame(
        page,
        ["直接输入token", "直接输入 token", "access token 生成", "获取token", "获取 token", "获取链接", "开始测试", "下一步开始测试"],
        exclude_hints=["验证兑换码", "订单查询", "历史查询"],
    )
    return bool(has_checkout_input and has_stage2_controls)


def _vendor_detect_stage_by_dom(target) -> int:
    try:
        value = target.evaluate(
            """
            () => {
              const textOf = (el) => String((el && (el.innerText || el.textContent)) || "").replace(/\\s+/g, " ").trim().toLowerCase();
              const isVisible = (el) => {
                if (!el) return false;
                try {
                  const st = window.getComputedStyle(el);
                  if (!st) return false;
                  if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity || 1) === 0) return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                } catch (_) {
                  return false;
                }
              };
              const pickByActiveClass = () => {
                const selectors = [
                  ".active", ".is-active", ".current", ".is-current",
                  ".ant-steps-item-process", ".step.active", ".steps .active"
                ];
                for (const selector of selectors) {
                  const nodes = Array.from(document.querySelectorAll(selector));
                  for (const node of nodes) {
                    if (!isVisible(node)) continue;
                    const t = textOf(node);
                    if (!t) continue;
                    if (/(验证兑换码|兑换码|cdk|redeem|step\\s*1|\\b1\\b)/i.test(t)) return 1;
                    if (/(填写信息|填写测试信息|checkout|token|step\\s*2|\\b2\\b)/i.test(t)) return 2;
                    if (/(测试处理|处理中|testing|step\\s*3|\\b3\\b)/i.test(t)) return 3;
                    if (/(完成|success|done|step\\s*4|\\b4\\b)/i.test(t)) return 4;
                  }
                }
                return 0;
              };

              const activeStage = pickByActiveClass();
              if (activeStage > 0) return activeStage;

              const body = textOf(document.body);
              if (!body) return 0;
              if (/(填写测试信息|获取 checkout session|直接输入 token|开始测试)/i.test(body)) return 2;
              if (/(测试处理|处理中|执行中|请稍候|正在测试)/i.test(body)) return 3;
              if (/(验证兑换码|激活cdk|兑换码验证)/i.test(body)) return 1;
              return 0;
            }
            """
        )
        return int(value or 0)
    except Exception:
        return 0


def _vendor_detect_stage_by_locator(target) -> int:
    try:
        if _vendor_has_text_by_locator(target, ["测试处理", "处理中", "执行中", "正在测试"]):
            return 3
        if _vendor_has_text_by_locator(target, ["填写测试信息", "获取 checkout session", "直接输入 token", "开始测试"]):
            return 2
        if _vendor_has_text_by_locator(target, ["验证兑换码", "请输入您的卡密兑换码", "激活码"]):
            return 1
        if _vendor_has_text_by_locator(target, ["完成", "测试完成", "订阅成功"]):
            return 4
    except Exception:
        pass
    return 0


def _vendor_detect_stage_any_frame(page) -> int:
    detected = 0
    try:
        detected = max(detected, _vendor_detect_stage_by_dom(page))
    except Exception:
        pass
    try:
        detected = max(detected, _vendor_detect_stage_by_locator(page))
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                detected = max(detected, _vendor_detect_stage_by_dom(frame))
            except Exception:
                continue
    except Exception:
        pass
    try:
        for frame in list(page.frames or []):
            try:
                detected = max(detected, _vendor_detect_stage_by_locator(frame))
            except Exception:
                continue
    except Exception:
        pass
    return int(detected or 0)


def _vendor_wait_stage_any_frame(
    page,
    expected_stage: int,
    *,
    timeout_ms: int = 12000,
    poll_ms: int = 500,
) -> bool:
    deadline = time.monotonic() + max(1.0, float(timeout_ms) / 1000.0)
    while time.monotonic() < deadline:
        stage = _vendor_detect_stage_any_frame(page)
        if int(stage or 0) == int(expected_stage):
            return True
        try:
            page.wait_for_timeout(max(100, int(poll_ms)))
        except Exception:
            time.sleep(max(0.1, float(poll_ms) / 1000.0))
    return False


def _run_vendor_auto_bind_task(
    task_id: int,
    *,
    redeem_code: str,
    checkout_override: Optional[str],
    api_url: Optional[str],
    api_key: Optional[str],
    timeout_seconds: int,
) -> None:
    _vendor_progress_log(task_id, "卡商模式开始执行", progress=3, status="running")
    _vendor_progress_log(task_id, f"流程版本: {VENDOR_EFUN_FLOW_VERSION}", progress=3, status="running")
    try:
        with get_db() as db:
            task = (
                db.query(BindCardTask)
                .options(joinedload(BindCardTask.account))
                .filter(BindCardTask.id == task_id)
                .first()
            )
            if not task:
                _vendor_progress_log(task_id, "任务不存在", progress=100, status="failed")
                return
            account = task.account
            if not account:
                task.status = "failed"
                task.last_error = "任务关联账号不存在"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, "任务关联账号不存在", progress=100, status="failed")
                return

            if _vendor_should_stop(task_id):
                task.status = "failed"
                task.last_error = "用户手动停止卡商订阅任务"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, "任务已停止", progress=100, status="cancelled")
                return

            checkout_url = str(checkout_override or "").strip()
            checkout_session_id = str(getattr(task, "checkout_session_id", "") or "").strip()
            checkout_proxy = _resolve_runtime_proxy(None, account)
            if not checkout_proxy:
                task.status = "failed"
                task.last_error = "未配置可用代理，vendor_efun 模式要求生成 Checkout 阶段走代理"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(
                    task_id,
                    "未配置可用代理，无法生成 Checkout（vendor_efun 要求 checkout 阶段走代理）",
                    progress=100,
                    status="failed",
                )
                return

            _vendor_progress_log(task_id, "卡商模式：强制按账号生成最新 Checkout", progress=10)
            try:
                refreshed_link = _regenerate_vendor_checkout_for_task(
                    db,
                    task=task,
                    account=account,
                    proxy=checkout_proxy,
                )
                checkout_url = str(refreshed_link or "").strip()
                checkout_session_id = str(getattr(task, "checkout_session_id", "") or "").strip() or _extract_checkout_session_id_from_url(checkout_url) or ""
                _vendor_progress_log(
                    task_id,
                    "最新 Checkout 已生成并回填（proxy=on，后续步骤走直连）",
                    progress=16,
                )
            except Exception as regen_exc:
                task.status = "failed"
                task.last_error = f"强制生成 checkout 失败: {regen_exc}"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, f"强制生成 checkout 失败: {regen_exc}", progress=100, status="failed")
                return

            if not checkout_url:
                task.status = "failed"
                task.last_error = "强制生成 checkout 为空"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, "强制生成 checkout 为空", progress=100, status="failed")
                return

            if _vendor_should_stop(task_id):
                task.status = "failed"
                task.last_error = "用户手动停止卡商订阅任务"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, "任务已停止", progress=100, status="cancelled")
                return

            access_token = str(getattr(account, "access_token", "") or "").strip()
            if not access_token:
                task.status = "failed"
                task.last_error = "账号缺少 access_token，无法调用卡商 bindcard 接口"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, "账号缺少 access_token，无法执行卡商接口提交", progress=100, status="failed")
                return

            _vendor_progress_log(task_id, "调用 EFun 接口开卡", progress=22)
            try:
                redeem_body = _efuncard_request(
                    method="POST",
                    path="/api/external/redeem",
                    api_key=_resolve_efuncard_api_key(None),
                    base_url=_resolve_efuncard_base_url(None),
                    proxy=None,
                    payload={"code": redeem_code},
                )
                redeem_data = redeem_body.get("data") if isinstance(redeem_body, dict) else {}
                if not isinstance(redeem_data, dict):
                    redeem_data = {}
            except Exception as redeem_exc:
                task.status = "failed"
                task.last_error = f"EFun 开卡失败: {redeem_exc}"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, f"EFun 开卡失败: {redeem_exc}", progress=100, status="failed")
                return

            card_payload = _normalize_vendor_card_payload(redeem_data)
            if not card_payload.get("number") or not card_payload.get("exp_month") or not card_payload.get("exp_year") or not card_payload.get("cvc"):
                task.status = "failed"
                task.last_error = "EFun 开卡返回缺少卡号/有效期/CVC"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, "EFun 开卡返回缺少卡号/有效期/CVC", progress=100, status="failed")
                return

            masked_card = _mask_card_number(card_payload.get("number"))
            _vendor_progress_log(task_id, f"EFun 开卡成功，卡片: {masked_card}", progress=30)

            billing_payload, billing_source = _build_vendor_billing_payload(
                account=account,
                redeem_data=redeem_data,
                country_hint=str(task.country or "US"),
            )
            _vendor_progress_log(task_id, f"账单信息已生成（source={billing_source}）", progress=36)

            proxy_country = _vendor_proxy_country_label(billing_payload.get("country"))
            bind_payload: Dict[str, Any] = {
                "acc_token": access_token,
                "plan_type": str(task.plan_type or "plus").strip().lower() or "plus",
                "card": {
                    "number": card_payload["number"],
                    "exp_month": card_payload["exp_month"],
                    "exp_year": card_payload["exp_year"],
                    "cvc": card_payload["cvc"],
                },
                "billing": {
                    "name": str(billing_payload.get("name") or "").strip(),
                    "email": str(billing_payload.get("email") or account.email or "").strip(),
                    "country": _normalize_checkout_country(billing_payload.get("country")),
                    "state": str(billing_payload.get("state") or "").strip(),
                    "city": str(billing_payload.get("city") or "").strip(),
                    "line1": str(billing_payload.get("line1") or "").strip(),
                    "postal_code": str(billing_payload.get("postal_code") or "").strip(),
                },
                "proxy_mode": "system",
                "proxy_country": proxy_country,
            }
            if checkout_url:
                bind_payload["checkout_url"] = checkout_url
            if checkout_session_id:
                bind_payload["session_id"] = checkout_session_id

            _vendor_progress_log(task_id, "提交卡商 bindcard 接口", progress=43)
            try:
                resolved_api_url = _resolve_vendor_bindcard_api_url(api_url)
                resolved_api_key = _resolve_vendor_bindcard_api_key(api_key)
                vendor_response, used_endpoint = _invoke_vendor_bindcard_api(
                    api_url=resolved_api_url,
                    api_key=resolved_api_key,
                    payload=bind_payload,
                    timeout_seconds=min(max(int(timeout_seconds or 180), 60), 300),
                )
            except Exception as submit_exc:
                task.status = "failed"
                task.last_error = f"卡商接口提交失败: {submit_exc}"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, f"卡商接口提交失败: {submit_exc}", progress=100, status="failed")
                return

            assessment = _assess_third_party_submission_result(vendor_response if isinstance(vendor_response, dict) else {})
            assess_state = str(assessment.get("state") or "pending").strip().lower()
            assess_reason = str(assessment.get("reason") or "").strip()
            snapshot = assessment.get("snapshot") if isinstance(assessment.get("snapshot"), dict) else {}
            payment_status = str(snapshot.get("payment_status") or "").strip().lower()
            checkout_status = str(snapshot.get("checkout_status") or "").strip().lower()
            logger.info(
                "卡商EFun接口提交结果: task_id=%s account_id=%s endpoint=%s state=%s payment_status=%s checkout_status=%s reason=%s",
                task.id,
                account.id,
                used_endpoint,
                assess_state,
                payment_status or "-",
                checkout_status or "-",
                assess_reason or "-",
            )
            if assess_state == "failed":
                task.status = "failed"
                task.last_error = f"卡商接口返回失败: {assess_reason or 'unknown'}"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, f"卡商接口返回失败: {assess_reason or 'unknown'}", progress=100, status="failed")
                return

            _mark_task_paid_pending_sync(
                task,
                (
                    f"卡商接口已受理（state={assess_state}, payment_status={payment_status or '-'}, reason={assess_reason or '-'}），开始同步订阅。"
                ),
            )
            db.commit()
            _vendor_progress_log(task_id, "卡商接口提交成功，开始同步订阅", progress=70)

            if _vendor_should_stop(task_id):
                task.status = "failed"
                task.last_error = "用户手动停止卡商订阅任务"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                _vendor_progress_log(task_id, "任务已停止", progress=100, status="cancelled")
                return

            detail, refreshed = _check_subscription_detail_with_retry(
                db=db,
                account=account,
                proxy=None,
                allow_token_refresh=True,
            )
            sub_status = str(detail.get("status") or "free").lower()
            source = str(detail.get("source") or "unknown")
            confidence = str(detail.get("confidence") or "low")
            now = datetime.utcnow()
            task.last_checked_at = now

            if sub_status in ("plus", "team"):
                _apply_subscription_result(
                    account,
                    status=sub_status,
                    checked_at=now,
                    confidence=confidence,
                    promote_reason="vendor_sync_paid",
                )
                task.status = "completed"
                task.completed_at = now
                task.last_error = None
                db.commit()
                _vendor_progress_log(
                    task_id,
                    f"订阅同步完成: {sub_status.upper()} (source={source}, confidence={confidence}, token_refreshed={refreshed})",
                    progress=100,
                    status="completed",
                )
                return

            if assess_state == "pending" and _is_third_party_challenge_pending(assessment):
                task.status = "waiting_user_action"
                task.last_error = (
                    f"卡商接口返回挑战态（reason={assess_reason or 'requires_action'}），请稍后点击“同步订阅”确认。"
                )
                task.last_checked_at = now
                db.commit()
                _vendor_progress_log(task_id, "卡商接口进入挑战态，等待人工完成后同步订阅", progress=100, status="pending")
                return

            _mark_task_paid_pending_sync(
                task,
                (
                    f"卡商接口已提交，但当前订阅={sub_status}（source={source}, confidence={confidence}）。请稍后点“同步订阅”重试。"
                ),
            )
            db.commit()
            _vendor_progress_log(
                task_id,
                f"订阅同步未命中付费状态（{sub_status}），任务保留在已支付待同步",
                progress=100,
                status="pending",
            )
            return
    except Exception as exc:
        logger.exception("卡商模式执行异常: task_id=%s error=%s", task_id, exc)
        _vendor_progress_log(task_id, f"执行异常: {exc}", progress=100, status="failed")
        with get_db() as db:
            task = db.query(BindCardTask).filter(BindCardTask.id == task_id).first()
            if task:
                task.status = "failed"
                task.last_error = f"卡商模式执行异常: {exc}"
                task.last_checked_at = datetime.utcnow()
                db.commit()

def _resolve_third_party_bind_api_url(request_url: Optional[str]) -> Optional[str]:
    raw = (
        str(request_url or "").strip()
        or str(os.getenv(THIRD_PARTY_BIND_API_URL_ENV) or "").strip()
        or THIRD_PARTY_BIND_API_DEFAULT
    )
    normalized = _normalize_third_party_bind_api_url(raw)
    return normalized or None


def _resolve_third_party_bind_api_key(request_key: Optional[str]) -> Optional[str]:
    token = str(request_key or "").strip() or str(os.getenv(THIRD_PARTY_BIND_API_KEY_ENV) or "").strip()
    return token or None


def _normalize_third_party_bind_api_url(raw_url: Optional[str]) -> Optional[str]:
    text = str(raw_url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path or ""
    if not path or path == "/":
        path = THIRD_PARTY_BIND_PATH_DEFAULT
    path = "/" + path.lstrip("/")
    normalized = parsed._replace(path=path, params="", fragment="")
    return urlunparse(normalized)


def _build_third_party_bind_api_candidates(api_url: str) -> List[str]:
    """
    对第三方地址做容错:
    - 支持只给根域名（自动补 /api/v1/bind-card）
    - 支持给到 /api/v1（自动补 /bind-card）
    - 保留原始路径作为首选
    """
    normalized = _normalize_third_party_bind_api_url(api_url)
    if not normalized:
        return []

    candidates: List[str] = []

    def _append(url: Optional[str]):
        value = str(url or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    _append(normalized)
    parsed = urlparse(normalized)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = (parsed.path or "").rstrip("/")
    lower = path.lower()

    if lower in ("", "/"):
        _append(base + THIRD_PARTY_BIND_PATH_DEFAULT)
    elif lower.endswith("/api/v1"):
        _append(base + path + "/bind-card")
        _append(base + THIRD_PARTY_BIND_PATH_DEFAULT)
    elif not lower.endswith("/bind-card"):
        _append(base + THIRD_PARTY_BIND_PATH_DEFAULT)

    return candidates


def _parse_third_party_response(resp) -> dict:
    if not (resp.content or b""):
        return {"ok": True}

    content_type = (resp.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            data = resp.json()
            if isinstance(data, dict):
                return data
            return {"data": data}
        except Exception:
            pass

    raw = str(resp.text or "").strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = resp.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"raw": raw[:1000]}


def _invoke_third_party_bind_api(
    *,
    api_url: str,
    api_key: Optional[str],
    payload: dict,
    proxy: Optional[str] = None,
) -> tuple[dict, str]:
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": "codex-console2/third-party-bind",
    }
    key = str(api_key or "").strip()
    if key:
        headers["X-API-Key"] = key
        headers["Authorization"] = f"Bearer {key}"

    url_candidates = _build_third_party_bind_api_candidates(api_url)
    if not url_candidates:
        raise RuntimeError("第三方绑卡 API 地址无效")

    proxy_candidates: List[Optional[str]] = []
    for value in (proxy, None):
        if value not in proxy_candidates:
            proxy_candidates.append(value)

    errors: List[str] = []
    for candidate_url in url_candidates:
        for proxy_item in proxy_candidates:
            proxies = {"http": proxy_item, "https": proxy_item} if proxy_item else None
            for attempt in range(1, 3):
                try:
                    resp = cffi_requests.post(
                        candidate_url,
                        headers=headers,
                        json=payload,
                        proxies=proxies,
                        timeout=120,
                        impersonate="chrome110",
                    )

                    if resp.status_code >= 400:
                        body = (resp.text or "")[:500]
                        err = f"{candidate_url} status={resp.status_code} proxy={'on' if proxy_item else 'off'} body={body}"
                        errors.append(err)
                        retryable = resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504)
                        endpoint_maybe_wrong = resp.status_code in (404, 405)
                        if attempt < 2 and retryable:
                            time.sleep(0.6 * attempt)
                            continue
                        if endpoint_maybe_wrong:
                            break
                        raise RuntimeError(f"第三方绑卡请求失败: HTTP {resp.status_code} - {body}")

                    parsed = _parse_third_party_response(resp)
                    if isinstance(parsed, dict):
                        parsed["_meta_endpoint"] = candidate_url
                        parsed["_meta_proxy"] = "on" if proxy_item else "off"
                        parsed["_meta_attempt"] = attempt
                    return parsed, candidate_url
                except Exception as exc:
                    err = f"{candidate_url} proxy={'on' if proxy_item else 'off'} attempt={attempt} error={exc}"
                    errors.append(err)
                    if attempt < 2:
                        time.sleep(0.6 * attempt)
                        continue
    summary = " | ".join(errors[-4:]) if errors else "unknown_error"
    raise RuntimeError(f"第三方绑卡请求失败，已尝试多路由: {summary}")


def _sanitize_third_party_response(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"result": str(payload)[:500]}
    safe: dict = {}
    for key, value in payload.items():
        key_lower = str(key or "").lower()
        if any(token in key_lower for token in ("card", "cvc", "cvv", "number", "profile", "pan")):
            safe[key] = "***"
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        else:
            safe[key] = str(value)[:500]
    return safe


def _extract_third_party_status_snapshot(payload: dict) -> dict:
    """从第三方返回体中提取支付状态快照（兼容 data/result 嵌套）。"""
    if not isinstance(payload, dict):
        return {}

    blocks = [payload]
    data_block = payload.get("data")
    if isinstance(data_block, dict):
        blocks.append(data_block)
        nested_result = data_block.get("result")
        if isinstance(nested_result, dict):
            blocks.append(nested_result)
    top_result = payload.get("result")
    if isinstance(top_result, dict):
        blocks.append(top_result)

    def _pick(*keys: str) -> str:
        for block in blocks:
            for key in keys:
                value = block.get(key)
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    return text
        return ""

    return {
        "payment_status": _pick("payment_status"),
        "checkout_status": _pick("checkout_status"),
        "setup_intent_status": _pick("setup_intent_status"),
        "payment_intent_status": _pick("payment_intent_status"),
        "submission_attempt_state": _pick("submission_attempt_state"),
        "next_action_type": _pick("next_action_type"),
        "failure_reason": _pick("failure_reason", "reason"),
        "status": _pick("status", "state"),
        "code": _pick("code"),
        "message": _pick("message", "error", "detail"),
        "task_id": _pick("task_id", "request_id", "job_id"),
        "checkout_session_id": _pick("checkout_session_id", "session_id"),
    }


def _assess_third_party_submission_result(payload: dict) -> dict:
    """
    第三方绑卡结果三态判定:
    - success: 已明确支付成功（如 payment_status=paid）
    - pending: 已提交但仍需用户挑战/等待异步处理
    - failed: 明确失败
    """
    snapshot = _extract_third_party_status_snapshot(payload)
    payment_status = snapshot.get("payment_status", "").strip().lower()
    checkout_status = snapshot.get("checkout_status", "").strip().lower()
    setup_intent_status = snapshot.get("setup_intent_status", "").strip().lower()
    payment_intent_status = snapshot.get("payment_intent_status", "").strip().lower()
    submission_state = snapshot.get("submission_attempt_state", "").strip().lower()
    next_action_type = snapshot.get("next_action_type", "").strip().lower()
    failure_reason = snapshot.get("failure_reason", "").strip().lower()
    status_text = snapshot.get("status", "").strip().lower()
    message = snapshot.get("message", "").strip()
    success_flag = payload.get("success") if isinstance(payload, dict) else None

    # 1) 明确成功信号（你提供的口径：payment_status=paid）
    if payment_status in ("paid", "succeeded", "success"):
        return {"state": "success", "reason": "", "snapshot": snapshot}
    if checkout_status in ("paid", "complete", "completed"):
        return {"state": "success", "reason": "", "snapshot": snapshot}

    # 2) 明确失败信号
    fail_tokens = ("fail", "error", "invalid", "denied", "forbidden", "declined", "reject", "cancel")
    if success_flag is False:
        reason = message or failure_reason or "third_party_success_false"
        return {"state": "failed", "reason": reason[:300], "snapshot": snapshot}
    if payment_status in ("failed", "canceled", "cancelled", "expired", "void"):
        reason = failure_reason or message or f"payment_status={payment_status}"
        return {"state": "failed", "reason": reason[:300], "snapshot": snapshot}
    if any(token in status_text for token in fail_tokens):
        reason = message or failure_reason or f"status={status_text}"
        return {"state": "failed", "reason": reason[:300], "snapshot": snapshot}
    if any(token in failure_reason for token in fail_tokens):
        reason = failure_reason or message or "failure_reason"
        return {"state": "failed", "reason": reason[:300], "snapshot": snapshot}
    low_message = message.lower()
    if low_message and any(token in low_message for token in fail_tokens):
        return {"state": "failed", "reason": message[:300], "snapshot": snapshot}

    # 3) 常见 pending 场景
    pending_signals = (
        payment_status in ("unpaid", "pending", "processing", "requires_action", "unknown"),
        checkout_status in ("open", "pending", "processing"),
        setup_intent_status in ("requires_action", "processing", "requires_confirmation"),
        payment_intent_status in ("requires_action", "processing", "unknown", "requires_confirmation"),
        submission_state in ("unknown", "pending", "processing"),
        bool(next_action_type),
        bool(snapshot.get("task_id")),
    )
    if any(pending_signals):
        reason = failure_reason or message or "pending_confirmation"
        return {"state": "pending", "reason": reason[:300], "snapshot": snapshot}

    # 4) 兜底: success=true 且无明确 paid，也视为 pending（仅代表“受理成功”）
    if success_flag is True:
        return {"state": "pending", "reason": message[:300], "snapshot": snapshot}

    # 5) 其他未知返回，按 pending 处理，交给后续轮询 + 订阅校验收敛
    return {"state": "pending", "reason": message[:300], "snapshot": snapshot}


def _is_third_party_challenge_pending(assessment: dict) -> bool:
    """
    判定第三方是否已进入“需要人工挑战”的 pending 状态。
    常见信号: requires_action / intent_confirmation_challenge / 3DS / hcaptcha。
    """
    if not isinstance(assessment, dict):
        return False
    snapshot = assessment.get("snapshot") if isinstance(assessment.get("snapshot"), dict) else {}
    reason = str(assessment.get("reason") or "").strip().lower()
    next_action_type = str(snapshot.get("next_action_type") or "").strip().lower()
    setup_intent_status = str(snapshot.get("setup_intent_status") or "").strip().lower()
    payment_intent_status = str(snapshot.get("payment_intent_status") or "").strip().lower()
    failure_reason = str(snapshot.get("failure_reason") or "").strip().lower()

    tokens = (
        reason,
        next_action_type,
        setup_intent_status,
        payment_intent_status,
        failure_reason,
    )
    challenge_keywords = (
        "requires_action",
        "intent_confirmation_challenge",
        "authentication_required",
        "3ds",
        "challenge",
        "hcaptcha",
    )
    for text in tokens:
        if any(keyword in text for keyword in challenge_keywords):
            return True
    return False


def _build_third_party_status_api_candidates(api_url: str) -> List[str]:
    normalized = _normalize_third_party_bind_api_url(api_url)
    if not normalized:
        return []
    parsed = urlparse(normalized)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = (parsed.path or "").rstrip("/")
    candidates: List[str] = []

    def _append(item: str):
        value = str(item or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    # 优先和 bind-card 同前缀的常见状态接口
    if path.endswith("/bind-card"):
        prefix = path[: -len("/bind-card")]
        _append(base + prefix + "/bind-card/status")
        _append(base + prefix + "/bind-card/result")
        _append(base + prefix + "/bind-card/query")
        _append(base + prefix + "/payment-status")
        _append(base + prefix + "/checkout-status")
        _append(base + prefix + "/status")

    # 通用 fallback
    _append(base + "/api/v1/bind-card/status")
    _append(base + "/api/v1/bind-card/result")
    _append(base + "/api/v1/bind-card/query")
    _append(base + "/api/v1/payment-status")
    _append(base + "/api/v1/status")
    return candidates


def _poll_third_party_bind_status(
    *,
    api_url: str,
    api_key: Optional[str],
    checkout_session_id: str,
    proxy: Optional[str],
    timeout_seconds: int,
    interval_seconds: int,
    status_hints: Optional[dict] = None,
) -> dict:
    """
    轮询第三方状态接口（若服务支持）：
    返回 {"state": success/pending/failed/unsupported, "endpoint": ..., "snapshot": ...}
    """
    if timeout_seconds <= 0:
        return {"state": "unsupported", "reason": "poll_disabled"}

    endpoints = _build_third_party_status_api_candidates(api_url)
    if not endpoints:
        return {"state": "unsupported", "reason": "no_status_endpoint"}

    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": "codex-console2/third-party-bind-status",
    }
    key = str(api_key or "").strip()
    if key:
        headers["X-API-Key"] = key
        headers["Authorization"] = f"Bearer {key}"

    deadline = time.monotonic() + max(timeout_seconds, 0)
    last_assess: Optional[dict] = None
    last_endpoint = ""
    attempts = 0
    visited = False

    while time.monotonic() < deadline:
        attempts += 1
        hints = status_hints if isinstance(status_hints, dict) else {}
        hint_task_id = str(
            hints.get("task_id")
            or hints.get("request_id")
            or hints.get("job_id")
            or ""
        ).strip()
        query_payload = {
            "checkout_session_id": checkout_session_id,
            "session_id": checkout_session_id,
            "cs_id": checkout_session_id,
        }
        if hint_task_id:
            query_payload.update(
                {
                    "task_id": hint_task_id,
                    "request_id": hint_task_id,
                    "job_id": hint_task_id,
                    "id": hint_task_id,
                }
            )
        for endpoint in endpoints:
            for proxy_item in (proxy, None):
                proxies = {"http": proxy_item, "https": proxy_item} if proxy_item else None
                # 一些服务用 GET + query，一些服务用 POST + body，这里都试一次
                request_variants = (
                    ("GET", {"params": query_payload}),
                    ("POST", {"json": query_payload}),
                )
                for method, extra in request_variants:
                    try:
                        visited = True
                        if method == "GET":
                            resp = cffi_requests.get(
                                endpoint,
                                headers=headers,
                                proxies=proxies,
                                timeout=25,
                                impersonate="chrome110",
                                **extra,
                            )
                        else:
                            resp = cffi_requests.post(
                                endpoint,
                                headers=headers,
                                proxies=proxies,
                                timeout=25,
                                impersonate="chrome110",
                                **extra,
                            )
                        if resp.status_code in (404, 405):
                            continue
                        if resp.status_code >= 400:
                            continue
                        data = _parse_third_party_response(resp)
                        assess = _assess_third_party_submission_result(data if isinstance(data, dict) else {})
                        assess["endpoint"] = endpoint
                        assess["proxy"] = "on" if proxy_item else "off"
                        assess["attempt"] = attempts
                        last_assess = assess
                        last_endpoint = endpoint
                        state = str(assess.get("state") or "").lower()
                        if state in ("success", "failed"):
                            return assess
                    except Exception:
                        continue
        time.sleep(max(interval_seconds, 2))

    if not visited:
        return {"state": "unsupported", "reason": "status_endpoint_unavailable"}
    if last_assess:
        return last_assess
    return {"state": "pending", "reason": "status_pending_timeout", "endpoint": last_endpoint}


def _refresh_account_token_for_subscription_check(account: Account, proxy: Optional[str]) -> tuple[bool, Optional[str]]:
    """
    刷新账号 Access Token（优先 session_token，其次 refresh_token）。
    """
    manager = TokenRefreshManager(proxy_url=proxy)
    refresh_result = manager.refresh_account(account)
    # 代理通道遇到地区限制时，再做一次直连兜底，避免“检测订阅”被 403 卡住。
    if (
        not refresh_result.success
        and proxy
        and "unsupported_country_region_territory" in str(refresh_result.error_message or "").lower()
    ):
        logger.warning(
            "订阅检测 token 刷新遇到地区限制，尝试直连重试: account_id=%s email=%s",
            account.id,
            account.email,
        )
        manager = TokenRefreshManager(proxy_url=None)
        refresh_result = manager.refresh_account(account)

    if not refresh_result.success:
        return False, refresh_result.error_message or "token_refresh_failed"

    if refresh_result.access_token:
        account.access_token = refresh_result.access_token
    if refresh_result.refresh_token:
        account.refresh_token = refresh_result.refresh_token
    if refresh_result.expires_at:
        account.expires_at = refresh_result.expires_at
    account.last_refresh = datetime.utcnow()
    return True, None


def _check_subscription_detail_with_retry(
    db,
    account: Account,
    proxy: Optional[str],
    allow_token_refresh: bool,
) -> tuple[dict, bool]:
    """
    订阅检测 + 一次 token 刷新重试：
    - 检测异常时尝试刷新 token 后重试
    - 检测到 free 且低置信度时，也尝试刷新 token 后重试
    Returns:
        (detail, refreshed)
    """
    refreshed = False

    try:
        detail = check_subscription_status_detail(account, proxy)
    except Exception as first_exc:
        if not allow_token_refresh:
            raise
        ok, err = _refresh_account_token_for_subscription_check(account, proxy)
        if not ok:
            raise RuntimeError(f"{first_exc}; token刷新失败: {err}")
        db.commit()
        refreshed = True
        detail = check_subscription_status_detail(account, proxy)
        detail = dict(detail or {})
        detail["token_refreshed"] = True
        return detail, refreshed

    status = str((detail or {}).get("status") or "free").lower()
    confidence = str((detail or {}).get("confidence") or "low").lower()
    source = str((detail or {}).get("source") or "").lower()
    should_refresh_on_free = (
        confidence != "high"
        or source.startswith("wham_usage.")
    )
    if allow_token_refresh and status == "free" and should_refresh_on_free:
        ok, err = _refresh_account_token_for_subscription_check(account, proxy)
        if ok:
            db.commit()
            refreshed = True
            detail = check_subscription_status_detail(account, proxy)
            detail = dict(detail or {})
            detail["token_refreshed"] = True
            return detail, refreshed
        logger.warning(
            "订阅检测触发token刷新但失败: account_id=%s email=%s err=%s",
            account.id,
            account.email,
            err,
        )

    # 代理环境下若仍为 free，增加一次直连复核，降低地区/线路噪音影响。
    if proxy and status == "free":
        try:
            direct_detail = check_subscription_status_detail(account, proxy=None)
            direct_status = str((direct_detail or {}).get("status") or "free").lower()
            direct_conf = str((direct_detail or {}).get("confidence") or "low").lower()
            logger.info(
                "订阅检测直连复核: account_id=%s email=%s status=%s source=%s confidence=%s",
                account.id,
                account.email,
                direct_status,
                (direct_detail or {}).get("source"),
                direct_conf,
            )
            if direct_status in ("plus", "team"):
                direct_detail = dict(direct_detail or {})
                direct_detail["checked_without_proxy"] = True
                return direct_detail, refreshed
            if confidence != "high":
                direct_detail = dict(direct_detail or {})
                direct_detail["checked_without_proxy"] = True
                return direct_detail, refreshed
        except Exception as direct_exc:
            logger.warning(
                "订阅检测直连复核失败: account_id=%s email=%s error=%s",
                account.id,
                account.email,
                direct_exc,
            )

    return detail, refreshed


def _is_retryable_subscription_check_error(error_message: Optional[str]) -> bool:
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


def _batch_check_subscription_one(
    account_id: int,
    explicit_proxy: Optional[str],
    max_attempts: int = PAYMENT_BATCH_SUBSCRIPTION_CHECK_RETRY_ATTEMPTS,
) -> Dict[str, Any]:
    subscription_allowed, subscription_breaker = breaker_allow_request("subscription_check")
    if not subscription_allowed:
        return {
            "id": account_id,
            "email": None,
            "success": False,
            "error": f"subscription_check 熔断中，稍后重试: {subscription_breaker}",
            "attempts": 0,
            "breaker": subscription_breaker,
        }

    attempts = max(1, int(max_attempts or 1))
    last_error = ""
    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return {
                "id": account_id,
                "email": None,
                "success": False,
                "error": "账号不存在",
                "attempts": 1,
            }

        for attempt in range(1, attempts + 1):
            runtime_proxy: Optional[str] = None
            try:
                runtime_proxy = _resolve_runtime_proxy(explicit_proxy, account)
                requested_proxy = bool(str(runtime_proxy or "").strip())
                if requested_proxy:
                    proxy_allowed, _proxy_breaker = breaker_allow_request("proxy_runtime")
                    if not proxy_allowed:
                        runtime_proxy = None
                using_proxy = bool(str(runtime_proxy or "").strip())

                detail, refreshed = _check_subscription_detail_with_retry(
                    db=db,
                    account=account,
                    proxy=runtime_proxy,
                    allow_token_refresh=True,
                )
                status = str(detail.get("status") or "free").lower()
                confidence = str(detail.get("confidence") or "low").lower()
                before_subscription = str(account.subscription_type or "free")

                _apply_subscription_result(
                    account,
                    status=status,
                    checked_at=datetime.utcnow(),
                    confidence=confidence,
                    promote_reason="batch_check_subscription",
                )

                db.commit()
                if before_subscription != status:
                    try:
                        crud.create_operation_audit_log(
                            db,
                            actor="system",
                            action="account.subscription_auto_detect",
                            target_type="account",
                            target_id=account.id,
                            target_email=account.email,
                            payload={
                                "before": before_subscription,
                                "after": status,
                                "confidence": confidence,
                                "source": detail.get("source"),
                                "task": "batch_check_subscription",
                            },
                        )
                    except Exception:
                        logger.debug("记录订阅自动检测审计日志失败: account_id=%s", account.id, exc_info=True)
                breaker_record_success("subscription_check")
                if using_proxy:
                    breaker_record_success("proxy_runtime")
                return {
                    "id": account_id,
                    "email": account.email,
                    "success": True,
                    "subscription_type": status,
                    "confidence": confidence,
                    "source": detail.get("source"),
                    "token_refreshed": refreshed,
                    "attempts": attempt,
                }
            except Exception as exc:
                db.rollback()
                last_error = str(exc)
                breaker_record_failure("subscription_check", last_error)
                if bool(str(runtime_proxy or explicit_proxy or getattr(account, "proxy_used", "") or "").strip()):
                    breaker_record_failure("proxy_runtime", last_error)
                can_retry = attempt < attempts and _is_retryable_subscription_check_error(last_error)
                if can_retry:
                    time.sleep(PAYMENT_BATCH_SUBSCRIPTION_CHECK_RETRY_BASE_DELAY_SECONDS * attempt)
                    continue
                return {
                    "id": account_id,
                    "email": account.email,
                    "success": False,
                    "error": last_error,
                    "attempts": attempt,
                }

    return {
        "id": account_id,
        "email": None,
        "success": False,
        "error": last_error or "subscription_check_failed",
        "attempts": attempts,
    }


def _run_batch_check_subscription_async_task(
    op_task_id: str,
    ids: List[int],
    explicit_proxy: Optional[str],
) -> None:
    total = len(ids)
    success_count = 0
    failed_count = 0
    completed_count = 0

    _update_payment_op_task(
        op_task_id,
        status="running",
        started_at=_payment_now_iso(),
        message=f"开始检测订阅，共 {total} 个账号",
        paused=False,
    )
    _set_payment_op_task_progress(
        op_task_id,
        total=total,
        completed=0,
        success=0,
        failed=0,
    )

    if total <= 0:
        _update_payment_op_task(
            op_task_id,
            status="completed",
            finished_at=_payment_now_iso(),
            message="没有可检测的账号",
            paused=False,
            result={"success_count": 0, "failed_count": 0, "total": 0, "cancelled": False, "details": []},
        )
        return

    worker_count = min(PAYMENT_BATCH_SUBSCRIPTION_CHECK_MAX_WORKERS, max(1, total))
    _update_payment_op_task(op_task_id, message=f"处理中 0/{total}（并发 {worker_count}）")

    next_index = 0
    running: Dict[Any, int] = {}
    details: List[Dict[str, Any]] = []
    cancelled = False
    pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="payment_sub_check_async")
    try:
        while completed_count < total:
            if not _wait_if_payment_op_task_paused(
                op_task_id,
                f"处理中 {completed_count}/{total}（并发 {worker_count}）",
            ):
                cancelled = True
                break
            if _is_payment_op_task_cancel_requested(op_task_id):
                cancelled = True
                break

            while next_index < total and len(running) < worker_count:
                if not _wait_if_payment_op_task_paused(
                    op_task_id,
                    f"处理中 {completed_count}/{total}（并发 {worker_count}）",
                ):
                    cancelled = True
                    break
                account_id = int(ids[next_index])
                next_index += 1
                future = pool.submit(_batch_check_subscription_one, account_id, explicit_proxy)
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
                        "email": None,
                        "success": False,
                        "error": str(exc),
                        "attempts": 1,
                    }

                completed_count += 1
                if detail.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
                details.append(detail)
                _append_payment_op_task_detail(op_task_id, detail)
                _set_payment_op_task_progress(
                    op_task_id,
                    total=total,
                    completed=completed_count,
                    success=success_count,
                    failed=failed_count,
                )
                _update_payment_op_task(
                    op_task_id,
                    message=f"处理中 {completed_count}/{total}（并发 {worker_count}）",
                )

        if cancelled:
            for future in list(running.keys()):
                future.cancel()
            _update_payment_op_task(
                op_task_id,
                status="cancelled",
                finished_at=_payment_now_iso(),
                message=f"任务已取消，进度 {completed_count}/{total}",
                paused=False,
                result={
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "total": total,
                    "cancelled": True,
                    "details": details,
                },
            )
            with get_db() as db:
                crud.create_operation_audit_log(
                    db,
                    actor="system",
                    action="payment.batch_check_subscription.cancelled",
                    target_type="batch_operation",
                    target_id=op_task_id,
                    payload={
                        "total": total,
                        "completed": completed_count,
                        "success_count": success_count,
                        "failed_count": failed_count,
                    },
                )
            return
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    _update_payment_op_task(
        op_task_id,
        status="completed",
        finished_at=_payment_now_iso(),
        message=f"检测完成：成功 {success_count}，失败 {failed_count}",
        paused=False,
        result={
            "success_count": success_count,
            "failed_count": failed_count,
            "total": total,
            "cancelled": False,
            "details": details,
        },
    )
    with get_db() as db:
        crud.create_operation_audit_log(
            db,
            actor="system",
            action="payment.batch_check_subscription.completed",
            target_type="batch_operation",
            target_id=op_task_id,
            payload={
                "total": total,
                "completed": completed_count,
                "success_count": success_count,
                "failed_count": failed_count,
            },
        )


def _generate_checkout_link_for_account(
    account: Account,
    request: "CheckoutRequestBase",
    proxy: Optional[str],
) -> tuple[str, str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    if request.plan_type not in ("plus", "team"):
        raise HTTPException(status_code=400, detail="plan_type 必须为 plus 或 team")

    # 优先官方 checkout，保证直接落到 chatgpt.com 绑卡页面。
    source = "openai_checkout"
    fallback_reason = None
    checkout_session_id: Optional[str] = None
    publishable_key: Optional[str] = None
    client_secret: Optional[str] = None
    request.country = _normalize_checkout_country(request.country)
    request.currency = _normalize_checkout_currency(request.country, getattr(request, "currency", None))
    try:
        if request.plan_type == "plus":
            bundle = generate_plus_checkout_bundle(
                account=account,
                proxy=proxy,
                country=request.country,
            )
        else:
            bundle = generate_team_checkout_bundle(
                account=account,
                workspace_name=request.workspace_name,
                price_interval=request.price_interval,
                seat_quantity=request.seat_quantity,
                proxy=proxy,
                country=request.country,
            )
        link = str(bundle.get("checkout_url") or "")
        checkout_session_id = str(bundle.get("checkout_session_id") or "").strip() or None
        publishable_key = str(bundle.get("publishable_key") or "").strip() or None
        client_secret = str(bundle.get("client_secret") or "").strip() or None
    except Exception as direct_err:
        if _is_checkout_connectivity_error(direct_err):
            logger.warning(
                "官方 checkout 网络连接失败，不回退 aimizy: account_id=%s email=%s error=%s",
                account.id,
                account.email,
                direct_err,
            )
            raise HTTPException(
                status_code=502,
                detail=f"官方 checkout 网络连接失败，请检查代理或网络后重试: {direct_err}",
            )
        # 官方接口失败时，回退到 aimizy 渠道（仍会尝试归一化为官方 checkout 链接）。
        source = "aimizy_fallback"
        fallback_reason = str(direct_err)
        logger.warning(f"官方 checkout 生成失败，回退 aimizy: {direct_err}")
        link = generate_aimizy_payment_link(
            account=account,
            plan_type=request.plan_type,
            proxy=proxy,
            country=request.country,
            currency=request.currency,
        )

    if not isinstance(link, str) or not link.strip():
        raise ValueError("未获取到支付链接，请检查账号 Token/Cookies 是否有效")

    if not checkout_session_id:
        checkout_session_id = _extract_checkout_session_id_from_url(link)

    return link, source, fallback_reason, checkout_session_id, publishable_key, client_secret


# ============== Pydantic Models ==============

class CheckoutRequestBase(BaseModel):
    account_id: int
    plan_type: str  # 'plus' or 'team'
    workspace_name: str = "MyTeam"
    price_interval: str = "month"
    seat_quantity: int = 5
    proxy: Optional[str] = None
    country: str = "US"
    currency: Optional[str] = "USD"


class GenerateLinkRequest(CheckoutRequestBase):
    auto_open: bool = False  # 生成后是否自动无痕打开


class CreateBindCardTaskRequest(CheckoutRequestBase):
    auto_open: bool = False
    bind_mode: str = "semi_auto"  # semi_auto / local_auto / vendor_efun
    custom_checkout_url: Optional[str] = None


class OpenIncognitoRequest(BaseModel):
    url: str
    account_id: Optional[int] = None  # 可选，用于注入账号 cookie


class SyncBindCardTaskRequest(BaseModel):
    proxy: Optional[str] = None


class MarkUserActionRequest(BaseModel):
    proxy: Optional[str] = None
    timeout_seconds: int = Field(default=180, ge=30, le=300)
    interval_seconds: int = Field(default=10, ge=5, le=30)


class ThirdPartyCardRequest(BaseModel):
    number: str
    exp_month: str
    exp_year: str
    cvc: str


class ThirdPartyProfileRequest(BaseModel):
    name: str
    email: Optional[str] = None
    country: str = "US"
    line1: str
    city: str
    state: str
    postal: str


class ThirdPartyAutoBindRequest(BaseModel):
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    proxy: Optional[str] = None
    timeout_seconds: int = Field(default=120, ge=30, le=300)
    interval_seconds: int = Field(default=10, ge=5, le=30)
    third_party_poll_timeout_seconds: int = Field(default=60, ge=0, le=300)
    third_party_poll_interval_seconds: int = Field(default=6, ge=2, le=30)
    card: ThirdPartyCardRequest
    profile: ThirdPartyProfileRequest


class LocalAutoBindRequest(BaseModel):
    proxy: Optional[str] = None
    browser_timeout_seconds: int = Field(default=180, ge=60, le=600)
    post_submit_wait_seconds: int = Field(default=90, ge=30, le=300)
    verify_timeout_seconds: int = Field(default=180, ge=30, le=300)
    verify_interval_seconds: int = Field(default=10, ge=5, le=30)
    headless: bool = False
    card: ThirdPartyCardRequest
    profile: ThirdPartyProfileRequest


class VendorAutoBindRequest(BaseModel):
    redeem_code: str
    checkout_url: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout_seconds: int = Field(default=180, ge=60, le=900)


class EfunRequestBase(BaseModel):
    code: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    proxy: Optional[str] = None


class EfunThreeDSVerifyRequest(EfunRequestBase):
    minutes: int = Field(default=30, ge=1, le=1440)


class MarkSubscriptionRequest(BaseModel):
    subscription_type: str  # 'free' / 'plus' / 'team'


class BatchCheckSubscriptionRequest(BaseModel):
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


class BindTaskFailStatsRequest(BaseModel):
    account_ids: List[int] = []


class SaveSessionTokenRequest(BaseModel):
    session_token: str
    merge_cookie: bool = True


# ============== 支付链接生成 ==============


@router.get("/random-billing")
def get_random_billing_profile(
    country: str = Query("US", description="国家代码，如 US/GB/CA"),
    proxy: Optional[str] = Query(None, description="可选代理"),
):
    """
    按国家随机生成账单资料。
    优先 meiguodizhi，失败自动降级到本地模板。
    """
    try:
        # 随机地址仅使用显式传入代理；不再默认继承系统代理配置。
        proxy_url = _normalize_proxy_value(proxy) or None
        profile = generate_random_billing_profile(country=country, proxy=proxy_url)
        return {
            "success": True,
            "profile": profile,
        }
    except Exception as exc:
        logger.error("随机账单资料生成失败: country=%s error=%s", country, exc)
        raise HTTPException(status_code=500, detail=f"随机账单资料生成失败: {exc}")


# ============== EFun 卡商接口 ==============

@router.post("/efun/redeem")
def efuncard_redeem(request: EfunRequestBase):
    base_url = _resolve_efuncard_base_url(request.base_url)
    api_key = _resolve_efuncard_api_key(request.api_key)
    code = _normalize_efuncard_code(request.code)
    proxy = _normalize_proxy_value(request.proxy) or None

    body = _efuncard_request(
        method="POST",
        path="/api/external/redeem",
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
        payload={"code": code},
    )
    data = body.get("data") if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}

    expiry = str(data.get("expiryDate") or "").strip()
    exp_month, exp_year = _parse_efuncard_expiry(expiry)
    card_number = str(data.get("cardNumber") or "").strip()
    cvv = str(data.get("cvv") or "").strip()
    masked = _mask_card_number(card_number) if card_number else "-"

    logger.info("EfunCard 开卡成功: code=%s card=%s status=%s", code, masked, data.get("status"))
    return {
        "success": True,
        "provider": "efuncard",
        "card": {
            "card_number": card_number,
            "exp_month": exp_month,
            "exp_year": exp_year,
            "cvc": cvv,
            "expiry_raw": expiry,
            "masked": masked,
            "code": str(data.get("code") or code).strip(),
            "card_id": data.get("cardId"),
            "status": str(data.get("status") or "").strip(),
            "created_at": data.get("createdAt"),
        },
        "raw": data,
    }


@router.post("/efun/cancel")
def efuncard_cancel(request: EfunRequestBase):
    base_url = _resolve_efuncard_base_url(request.base_url)
    api_key = _resolve_efuncard_api_key(request.api_key)
    code = _normalize_efuncard_code(request.code)
    proxy = _normalize_proxy_value(request.proxy) or None

    body = _efuncard_request(
        method="POST",
        path="/api/external/cards/cancel",
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
        payload={"code": code},
    )
    data = body.get("data") if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}
    logger.info("EfunCard 销卡完成: code=%s status=%s refund=%s", code, data.get("status"), data.get("refundAmount"))
    return {"success": True, "provider": "efuncard", "data": data}


@router.post("/efun/query")
def efuncard_query(request: EfunRequestBase):
    base_url = _resolve_efuncard_base_url(request.base_url)
    api_key = _resolve_efuncard_api_key(request.api_key)
    code = _normalize_efuncard_code(request.code)
    proxy = _normalize_proxy_value(request.proxy) or None

    encoded_code = quote(code, safe="")
    body = _efuncard_request(
        method="GET",
        path=f"/api/external/cards/query/{encoded_code}",
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
    )
    data = body.get("data") if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}
    return {"success": True, "provider": "efuncard", "data": data}


@router.post("/efun/billing")
def efuncard_billing(request: EfunRequestBase):
    base_url = _resolve_efuncard_base_url(request.base_url)
    api_key = _resolve_efuncard_api_key(request.api_key)
    code = _normalize_efuncard_code(request.code)
    proxy = _normalize_proxy_value(request.proxy) or None

    encoded_code = quote(code, safe="")
    body = _efuncard_request(
        method="GET",
        path=f"/api/external/billing/{encoded_code}",
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
    )
    data = body.get("data") if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}
    return {"success": True, "provider": "efuncard", "data": data}


@router.post("/efun/3ds/verify")
def efuncard_verify_3ds(request: EfunThreeDSVerifyRequest):
    base_url = _resolve_efuncard_base_url(request.base_url)
    api_key = _resolve_efuncard_api_key(request.api_key)
    code = _normalize_efuncard_code(request.code)
    proxy = _normalize_proxy_value(request.proxy) or None

    body = _efuncard_request(
        method="POST",
        path="/api/external/3ds/verify",
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
        payload={"code": code, "minutes": int(request.minutes)},
    )
    data = body.get("data") if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}
    return {"success": True, "provider": "efuncard", "data": data}


@router.get("/accounts/{account_id}/session-diagnostic")
def get_account_session_diagnostic(
    account_id: int,
    probe: bool = Query(True, description="是否执行一次实时会话探测"),
    proxy: Optional[str] = Query(None, description="会话探测代理"),
):
    """
    会话诊断：
    - 账号是否具备 access/session/device 基础条件
    - cookies 中 session token 是否为分片形式
    - 可选实时请求 /api/auth/session 验证会话可用性
    """
    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        access_token = str(account.access_token or "").strip()
        refresh_token = str(account.refresh_token or "").strip()
        session_token_db = str(account.session_token or "").strip()
        cookies_text = str(account.cookies or "")
        device_id = _resolve_account_device_id(account)
        session_token_cookie = _extract_session_token_from_cookie_text(cookies_text)
        session_chunk_indices = _extract_session_token_chunks_from_cookie_text(cookies_text)
        resolved_session_token = session_token_db or session_token_cookie

        notes: List[str] = []
        if not access_token:
            notes.append("缺少 access_token（无法走 auth/session 探测授权头）")
        if not resolved_session_token:
            notes.append("未发现 session_token（DB 与 cookies 都为空）")
        if session_chunk_indices and not session_token_cookie:
            notes.append("发现 session 分片但未能拼接，请检查 cookies 原文完整性")
        if not device_id:
            notes.append("缺少 oai-did（会话建立成功率会下降）")

        probe_result = None
        if probe:
            probe_proxy = _resolve_runtime_proxy(proxy, account)
            probe_result = _probe_auth_session_context(account, probe_proxy)
            if not probe_result.get("ok"):
                notes.append(
                    "实时探测未通过："
                    + (
                        str(probe_result.get("error") or "").strip()
                        or f"http_status={probe_result.get('http_status')}"
                    )
                )

        recommendation = "会话完整，可直接执行全自动绑卡"
        if not resolved_session_token and access_token:
            recommendation = "建议先用 access_token 预热 /api/auth/session，再执行全自动"
        elif not access_token and not resolved_session_token:
            recommendation = "账号会话信息不足，建议重新登录一次并回写 cookies/session_token"
        elif probe_result and (not probe_result.get("session_token_found")):
            recommendation = "建议检查代理线路与账号登录态，必要时切直连重试"
        can_login_bootstrap = bool(str(account.password or "").strip()) and bool(str(account.email_service or "").strip())
        if (not resolved_session_token) and can_login_bootstrap:
            recommendation = "可尝试后端自动登录补会话（账号密码+邮箱验证码）后再执行全自动"

        return {
            "success": True,
            "diagnostic": {
                "account_id": account.id,
                "email": account.email,
                "token_state": {
                    "has_access_token": bool(access_token),
                    "access_token_len": len(access_token),
                    "access_token_preview": _mask_secret(access_token),
                    "has_refresh_token": bool(refresh_token),
                    "refresh_token_len": len(refresh_token),
                    "has_session_token_db": bool(session_token_db),
                    "session_token_db_len": len(session_token_db),
                    "session_token_db_preview": _mask_secret(session_token_db),
                    "has_session_token_cookie": bool(session_token_cookie),
                    "session_token_cookie_len": len(session_token_cookie),
                    "session_token_cookie_preview": _mask_secret(session_token_cookie),
                    "resolved_session_token_len": len(resolved_session_token),
                    "resolved_session_token_preview": _mask_secret(resolved_session_token),
                },
                "cookie_state": {
                    "has_cookies": bool(cookies_text.strip()),
                    "cookies_len": len(cookies_text),
                    "has_oai_did": bool(_extract_cookie_value(cookies_text, "oai-did")),
                    "resolved_oai_did": _mask_secret(device_id),
                    "session_chunk_count": len(session_chunk_indices),
                    "session_chunk_indices": session_chunk_indices,
                },
                "bootstrap_capability": {
                    "can_login_bootstrap": can_login_bootstrap,
                    "has_password": bool(str(account.password or "").strip()),
                    "email_service_type": str(account.email_service or ""),
                    "email_service_mailbox_id": str(account.email_service_id or ""),
                },
                "probe": probe_result,
                "notes": notes,
                "recommendation": recommendation,
                "checked_at": datetime.utcnow().isoformat(),
            },
        }


@router.post("/accounts/{account_id}/session-bootstrap")
def bootstrap_account_session_token(
    account_id: int,
    proxy: Optional[str] = Query(None, description="会话补全代理"),
):
    """
    主动触发一次会话补全：
    1) 先走 API 级 session 探测补全
    2) 失败后自动走账号登录链路（邮箱验证码）补全
    """
    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        runtime_proxy = _resolve_runtime_proxy(proxy, account)
        token = _bootstrap_session_token_for_local_auto(db, account, runtime_proxy)
        if not token:
            return {
                "success": False,
                "message": "会话补全未命中 session_token",
                "account_id": account.id,
                "email": account.email,
            }

        return {
            "success": True,
            "message": "会话补全成功",
            "account_id": account.id,
            "email": account.email,
            "session_token_len": len(str(token or "")),
            "session_token_preview": _mask_secret(token),
        }


@router.post("/accounts/{account_id}/session-token")
def save_account_session_token(
    account_id: int,
    request: SaveSessionTokenRequest,
):
    """
    手动写入 session_token（ABCard 兜底模式）。
    """
    token = str(request.session_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="session_token 不能为空")

    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        account.session_token = token
        if request.merge_cookie:
            account.cookies = _upsert_cookie(account.cookies, "__Secure-next-auth.session-token", token)
        account.last_refresh = datetime.utcnow()
        db.commit()
        db.refresh(account)

        logger.info(
            "手动写入 session_token: account_id=%s email=%s token_len=%s merge_cookie=%s",
            account.id,
            account.email,
            len(token),
            bool(request.merge_cookie),
        )
        return {
            "success": True,
            "account_id": account.id,
            "email": account.email,
            "session_token_len": len(token),
            "session_token_preview": _mask_secret(token),
            "message": "session_token 已保存",
        }


@router.post("/generate-link")
def generate_payment_link(request: GenerateLinkRequest):
    """生成 Plus 或 Team 支付链接，可选自动无痕打开"""
    with get_db() as db:
        account = db.query(Account).filter(Account.id == request.account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        proxy = _resolve_runtime_proxy(request.proxy, account)

        try:
            link, source, fallback_reason, checkout_session_id, publishable_key, client_secret = _generate_checkout_link_for_account(
                account=account,
                request=request,
                proxy=proxy,
            )
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"生成支付链接失败: {e}")
            raise HTTPException(status_code=500, detail=f"生成链接失败: {str(e)}")

    opened = False
    if request.auto_open and link:
        cookies_str = account.cookies if account else None
        opened = open_url_incognito(link, cookies_str)

    return {
        "success": True,
        "link": link,
        "is_official_checkout": _is_official_checkout_link(link),
        "plan_type": request.plan_type,
        "country": _normalize_checkout_country(request.country),
        "currency": _normalize_checkout_currency(_normalize_checkout_country(request.country), request.currency),
        "auto_opened": opened,
        "source": source,
        "fallback_reason": fallback_reason,
        "checkout_session_id": checkout_session_id,
        "publishable_key": publishable_key,
        "has_client_secret": bool(client_secret),
    }


@router.post("/open-incognito")
def open_browser_incognito(request: OpenIncognitoRequest):
    """后端以无痕模式打开指定 URL，可注入账号 cookie"""
    if not request.url:
        raise HTTPException(status_code=400, detail="URL 不能为空")

    cookies_str = None
    if request.account_id:
        with get_db() as db:
            account = db.query(Account).filter(Account.id == request.account_id).first()
            if account:
                cookies_str = account.cookies

    success = open_url_incognito(request.url, cookies_str)
    if success:
        return {"success": True, "message": "已在无痕模式打开浏览器"}
    return {"success": False, "message": "未找到可用的浏览器，请手动复制链接"}


# ============== 绑卡任务（A 方案） ==============

@router.post("/bind-card/tasks")
def create_bind_card_task(request: CreateBindCardTaskRequest):
    """创建绑卡任务（从账号管理中选择账号）"""
    bind_mode = str(request.bind_mode or "semi_auto").strip().lower()
    if bind_mode in DISABLED_BIND_MODES:
        raise HTTPException(status_code=403, detail=f"bind_mode={bind_mode} 已被禁用")
    if bind_mode not in ALLOWED_BIND_MODES:
        raise HTTPException(status_code=400, detail="bind_mode 必须为 semi_auto / local_auto / vendor_efun")

    with _acquire_bind_task_create_lock(request.account_id):
        with get_db() as db:
            account = db.query(Account).filter(Account.id == request.account_id).first()
            if not account:
                raise HTTPException(status_code=404, detail="账号不存在")

            active_task = _find_active_bind_task_for_account(db, account.id)
            if active_task:
                logger.info(
                    "拒绝重复创建绑卡任务: account_id=%s active_task_id=%s active_status=%s",
                    account.id,
                    active_task.id,
                    active_task.status,
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"账号已有进行中的绑卡任务（task_id={active_task.id}, status={active_task.status or 'pending'}），请先完成或取消后再创建",
                )

            logger.info(
                "创建绑卡任务: account_id=%s email=%s plan=%s country=%s mode=%s auto_open=%s",
                account.id, account.email, request.plan_type, request.country, bind_mode, request.auto_open
            )

            proxy = _resolve_runtime_proxy(request.proxy, account)
            checkout_request = request
            custom_checkout = str(request.custom_checkout_url or "").strip()
            if bind_mode == "vendor_auto" and custom_checkout:
                parsed_custom = urlparse(custom_checkout)
                if parsed_custom.scheme not in ("http", "https") or not parsed_custom.netloc:
                    raise HTTPException(status_code=400, detail="自定义 Checkout 链接格式无效")
                link = custom_checkout
                source = "vendor_custom"
                fallback_reason = None
                checkout_session_id = _extract_checkout_session_id_from_url(link)
                publishable_key = None
                client_secret = None
            else:
                try:
                    link, source, fallback_reason, checkout_session_id, publishable_key, client_secret = _generate_checkout_link_for_account(
                        account=account,
                        request=checkout_request,
                        proxy=proxy,
                    )
                except HTTPException:
                    raise
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                except Exception as e:
                    logger.error(f"创建绑卡任务失败: {e}")
                    raise HTTPException(status_code=500, detail=f"创建绑卡任务失败: {str(e)}")

            task = BindCardTask(
                account_id=account.id,
                account_email=account.email,
                plan_type=request.plan_type,
                workspace_name=request.workspace_name if request.plan_type == "team" else None,
                price_interval=request.price_interval if request.plan_type == "team" else None,
                seat_quantity=request.seat_quantity if request.plan_type == "team" else None,
                country=_normalize_checkout_country(request.country),
                currency=_normalize_checkout_currency(_normalize_checkout_country(request.country), request.currency),
                checkout_url=link,
                checkout_session_id=checkout_session_id,
                publishable_key=publishable_key,
                client_secret=client_secret,
                checkout_source=source,
                bind_mode=bind_mode,
                status="link_ready",
            )
            db.add(task)
            db.commit()
            db.refresh(task)

            logger.info(
                "绑卡任务已创建: task_id=%s account_id=%s plan=%s source=%s status=%s",
                task.id, task.account_id, task.plan_type, source, task.status
            )

            opened = False
            if request.auto_open and bind_mode in ("semi_auto", "vendor_efun") and link:
                opened = open_url_incognito(link, account.cookies if account else None)
                if opened:
                    task.status = "opened"
                    task.opened_at = datetime.utcnow()
                    db.commit()
                    db.refresh(task)
                    logger.info("绑卡任务自动打开成功: task_id=%s mode=%s", task.id, bind_mode)
                else:
                    logger.warning("绑卡任务自动打开失败: task_id=%s mode=%s", task.id, bind_mode)

            return {
                "success": True,
                "task": _serialize_bind_card_task(task),
                "link": link,
                "is_official_checkout": _is_official_checkout_link(link),
                "source": source,
                "fallback_reason": fallback_reason,
                "auto_opened": opened,
                "checkout_session_id": checkout_session_id,
                "publishable_key": publishable_key,
                "has_client_secret": bool(client_secret),
            }


@router.get("/bind-card/tasks")
def list_bind_card_tasks(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: Optional[str] = Query(None, description="状态筛选"),
    search: Optional[str] = Query(None, description="按邮箱搜索"),
):
    """绑卡任务列表"""
    with get_db() as db:
        query = db.query(BindCardTask).options(joinedload(BindCardTask.account))
        if status:
            query = query.filter(BindCardTask.status == status)
        if search:
            pattern = f"%{search}%"
            query = query.outerjoin(Account, BindCardTask.account_id == Account.id).filter(
                or_(
                    Account.email.ilike(pattern),
                    Account.account_id.ilike(pattern),
                    BindCardTask.account_email.ilike(pattern),
                )
            )

        total = query.count()
        offset = (page - 1) * page_size
        tasks = query.order_by(BindCardTask.created_at.desc()).offset(offset).limit(page_size).all()

        # 自动收敛任务状态：如果账号已是 plus/team，任务自动标记完成。
        now = datetime.utcnow()
        changed = False
        changed_count = 0
        for task in tasks:
            account = task.account
            sub_type = str(getattr(account, "subscription_type", "") or "").lower()
            if sub_type in ("plus", "team") and task.status != "completed":
                task.status = "completed"
                task.completed_at = task.completed_at or getattr(account, "subscription_at", None) or now
                task.last_error = None
                task.last_checked_at = now
                changed = True
                changed_count += 1

        if changed:
            db.commit()
            logger.info("绑卡任务状态自动收敛完成: updated_count=%s", changed_count)

        return {
            "total": total,
            "tasks": [_serialize_bind_card_task(task) for task in tasks],
        }


@router.post("/bind-card/tasks/fail-stats")
def get_bind_card_task_fail_stats(request: BindTaskFailStatsRequest):
    account_ids = []
    for item in (request.account_ids or []):
        try:
            account_id = int(item)
        except Exception:
            continue
        if account_id > 0:
            account_ids.append(account_id)

    with get_db() as db:
        query = (
            db.query(
                BindCardTask.account_id.label("account_id"),
                func.count(BindCardTask.id).label("fail_count"),
            )
            .filter(BindCardTask.account_id.isnot(None))
            .filter(BindCardTask.status.in_(("failed", "cancelled")))
        )
        if account_ids:
            query = query.filter(BindCardTask.account_id.in_(account_ids))

        rows = query.group_by(BindCardTask.account_id).all()

    stats = []
    for row in rows:
        try:
            aid = int(getattr(row, "account_id", 0) or 0)
        except Exception:
            aid = 0
        try:
            fail_count = int(getattr(row, "fail_count", 0) or 0)
        except Exception:
            fail_count = 0
        if aid <= 0:
            continue
        stats.append({"account_id": aid, "fail_count": max(0, fail_count)})

    return {"success": True, "stats": stats}


@router.post("/bind-card/tasks/{task_id}/open")
def open_bind_card_task(task_id: int):
    """打开绑卡任务对应的 checkout 链接"""
    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        if not task.checkout_url:
            raise HTTPException(status_code=400, detail="任务缺少 checkout 链接")

        cookies_str = task.account.cookies if task.account else None
        logger.info("打开绑卡任务: task_id=%s account_id=%s", task.id, task.account_id)
        opened = open_url_incognito(task.checkout_url, cookies_str)
        if opened:
            if str(task.status or "") not in ("paid_pending_sync", "completed"):
                task.status = "opened"
            task.opened_at = datetime.utcnow()
            task.last_error = None
            db.commit()
            db.refresh(task)
            logger.info("绑卡任务打开成功: task_id=%s", task.id)
            return {"success": True, "task": _serialize_bind_card_task(task)}

        task.last_error = "未找到可用的浏览器"
        db.commit()
        logger.warning("绑卡任务打开失败: task_id=%s reason=%s", task.id, task.last_error)
        raise HTTPException(status_code=500, detail="未找到可用的浏览器，请手动复制链接")


@router.post("/bind-card/tasks/{task_id}/auto-bind-third-party")
def auto_bind_bind_card_task_third_party(task_id: int, request: ThirdPartyAutoBindRequest):
    """
    通过第三方 API 自动提交绑卡（A+B 方案）。
    A: 三态判定（success/pending/failed）
    B: 尝试轮询第三方状态接口，能确认 paid 就标记 paid_pending_sync（等待订阅同步）
    """
    raise HTTPException(status_code=403, detail="第三方自动绑卡模式已禁用")
    third_party_response_safe: dict = {}
    api_url_for_log = ""
    third_party_assessment: dict = {}
    third_party_status_poll: dict = {}
    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        account = task.account
        if not account:
            raise HTTPException(status_code=404, detail="任务关联账号不存在")

        checkout_session_id = str(task.checkout_session_id or "").strip() or _extract_checkout_session_id_from_url(task.checkout_url)
        publishable_key = str(task.publishable_key or "").strip()
        if not checkout_session_id:
            raise HTTPException(status_code=400, detail="任务缺少 checkout_session_id，请重新创建任务")
        if not publishable_key:
            raise HTTPException(status_code=400, detail="任务缺少 publishable_key，请重新创建任务")

        api_url = _resolve_third_party_bind_api_url(request.api_url)
        api_key = _resolve_third_party_bind_api_key(request.api_key)
        if not api_url:
            raise HTTPException(status_code=400, detail=f"缺少第三方 API 地址（request.api_url 或环境变量 {THIRD_PARTY_BIND_API_URL_ENV}）")
        api_url_for_log = api_url

        proxy = _resolve_runtime_proxy(request.proxy, account)
        payload = {
            "checkout_session_id": checkout_session_id,
            "publishable_key": publishable_key,
            "client_secret": str(getattr(task, "client_secret", "") or "").strip() or None,
            "checkout_url": str(task.checkout_url or "").strip() or None,
            "plan_type": str(task.plan_type or "").strip().lower(),
            "country": "US",
            "currency": "USD",
            "card": {
                "number": str(request.card.number or "").strip(),
                "exp_month": str(request.card.exp_month or "").strip().zfill(2),
                "exp_year": str(request.card.exp_year or "").strip()[-2:],
                "cvc": str(request.card.cvc or "").strip(),
            },
            "profile": {
                "name": str(request.profile.name or "").strip(),
                "email": str(request.profile.email or account.email or "").strip(),
                "country": str(request.profile.country or "US").strip().upper(),
                "line1": str(request.profile.line1 or "").strip(),
                "city": str(request.profile.city or "").strip(),
                "state": str(request.profile.state or "").strip(),
                "postal": str(request.profile.postal or "").strip(),
            },
        }

        if not payload["card"]["number"] or not payload["card"]["cvc"]:
            raise HTTPException(status_code=400, detail="卡号/CVC 不能为空")

        logger.info(
            "第三方自动绑卡提交开始: task_id=%s account_id=%s mode=third_party api_url=%s has_api_key=%s cs_id=%s card=%s",
            task.id,
            account.id,
            api_url,
            "yes" if api_key else "no",
            checkout_session_id[:24] + "...",
            _mask_card_number(payload["card"]["number"]),
        )

        task.bind_mode = "third_party"
        task.status = "verifying"
        task.last_error = None
        task.last_checked_at = datetime.utcnow()
        db.commit()

        try:
            third_party_response, used_endpoint = _invoke_third_party_bind_api(
                api_url=api_url,
                api_key=api_key,
                payload=payload,
                proxy=proxy,
            )
            third_party_response_safe = _sanitize_third_party_response(third_party_response)
            third_party_assessment = _assess_third_party_submission_result(third_party_response)
            assess_state = str(third_party_assessment.get("state") or "pending").lower()
            assess_reason = str(third_party_assessment.get("reason") or "").strip()
            assess_snapshot = third_party_assessment.get("snapshot") if isinstance(third_party_assessment, dict) else {}
            payment_status = str((assess_snapshot or {}).get("payment_status") or "").lower()
            checkout_status = str((assess_snapshot or {}).get("checkout_status") or "").lower()
            setup_intent_status = str((assess_snapshot or {}).get("setup_intent_status") or "").lower()
            logger.info(
                "第三方自动绑卡提交评估: task_id=%s account_id=%s endpoint=%s state=%s payment_status=%s checkout_status=%s setup_intent_status=%s reason=%s",
                task.id,
                account.id,
                used_endpoint,
                assess_state,
                payment_status or "-",
                checkout_status or "-",
                setup_intent_status or "-",
                assess_reason or "-",
            )

            if assess_state == "failed":
                task.status = "failed"
                task.last_error = f"第三方返回失败: {assess_reason or 'unknown'}"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                logger.warning(
                    "第三方自动绑卡返回业务失败: task_id=%s account_id=%s endpoint=%s reason=%s response=%s",
                    task.id,
                    account.id,
                    used_endpoint,
                    assess_reason,
                    third_party_response_safe,
                )
                raise HTTPException(status_code=400, detail=f"第三方返回失败: {assess_reason or 'unknown'}")

            paid_hint_status = payment_status or "paid"
            paid_hint_reason = assess_reason or "third_party_paid_signal"

            # B 方案：若提交后仍 pending，尝试轮询第三方状态接口确认是否已 paid。
            if assess_state == "pending":
                # 若第三方明确返回 challenge/requires_action，直接切换待用户完成，避免无意义轮询超时。
                if _is_third_party_challenge_pending(third_party_assessment):
                    task.status = "waiting_user_action"
                    task.last_checked_at = datetime.utcnow()
                    hint_reason = assess_reason or "requires_action"
                    hint_payment_status = payment_status or "unknown"
                    task.last_error = (
                        f"第三方已受理并进入挑战流程（payment_status={hint_payment_status}, reason={hint_reason}）。"
                        "请打开 checkout 页面完成 challenge 后点击“我已完成支付”或“同步订阅”。"
                    )
                    db.commit()
                    db.refresh(task)
                    logger.info(
                        "第三方自动绑卡检测到挑战态，转人工继续: task_id=%s account_id=%s payment_status=%s reason=%s",
                        task.id,
                        account.id,
                        hint_payment_status,
                        hint_reason,
                    )
                    return {
                        "success": True,
                        "verified": False,
                        "pending": True,
                        "need_user_action": True,
                        "subscription_type": str(account.subscription_type or "free"),
                        "task": _serialize_bind_card_task(task),
                        "account_id": account.id,
                        "account_email": account.email,
                        "third_party": {
                            "submitted": True,
                            "api_url": used_endpoint,
                            "assessment": third_party_assessment,
                            "poll": {},
                            "response": third_party_response_safe,
                        },
                    }

                third_party_status_poll = _poll_third_party_bind_status(
                    api_url=used_endpoint,
                    api_key=api_key,
                    checkout_session_id=checkout_session_id,
                    proxy=proxy,
                    timeout_seconds=request.third_party_poll_timeout_seconds,
                    interval_seconds=request.third_party_poll_interval_seconds,
                    status_hints=assess_snapshot or {},
                )
                poll_state = str((third_party_status_poll or {}).get("state") or "pending").lower()
                poll_reason = str((third_party_status_poll or {}).get("reason") or "").strip()
                poll_snapshot = (third_party_status_poll or {}).get("snapshot") or {}
                poll_payment_status = str((poll_snapshot or {}).get("payment_status") or "").lower()
                logger.info(
                    "第三方自动绑卡状态轮询: task_id=%s account_id=%s endpoint=%s state=%s payment_status=%s reason=%s",
                    task.id,
                    account.id,
                    str((third_party_status_poll or {}).get("endpoint") or used_endpoint),
                    poll_state,
                    poll_payment_status or "-",
                    poll_reason or "-",
                )

                if poll_state == "failed":
                    task.status = "failed"
                    task.last_error = f"第三方状态失败: {poll_reason or 'unknown'}"
                    task.last_checked_at = datetime.utcnow()
                    db.commit()
                    raise HTTPException(status_code=400, detail=f"第三方状态失败: {poll_reason or 'unknown'}")

                if poll_state != "success":
                    task.status = "waiting_user_action"
                    task.last_checked_at = datetime.utcnow()
                    hint_reason = poll_reason or assess_reason or "pending_confirmation"
                    hint_payment_status = poll_payment_status or payment_status or "unknown"
                    task.last_error = (
                        f"第三方已受理，等待支付最终状态（payment_status={hint_payment_status}, reason={hint_reason}）。"
                        "如页面要求 challenge，请完成后点击“我已完成支付”或“同步订阅”。"
                    )
                    db.commit()
                    db.refresh(task)
                    return {
                        "success": True,
                        "verified": False,
                        "pending": True,
                        "need_user_action": True,
                        "subscription_type": str(account.subscription_type or "free"),
                        "task": _serialize_bind_card_task(task),
                        "account_id": account.id,
                        "account_email": account.email,
                        "third_party": {
                            "submitted": True,
                            "api_url": used_endpoint,
                            "assessment": third_party_assessment,
                            "poll": third_party_status_poll,
                            "response": third_party_response_safe,
                        },
                    }

                # poll 成功视为支付确认，不阻塞在订阅轮询
                paid_hint_status = poll_payment_status or payment_status or "paid"
                paid_hint_reason = poll_reason or assess_reason or "third_party_paid_signal"

            logger.info(
                "第三方自动绑卡提交成功: task_id=%s account_id=%s endpoint=%s response_keys=%s",
                task.id,
                account.id,
                used_endpoint,
                ",".join(sorted(third_party_response_safe.keys())),
            )
            api_url_for_log = used_endpoint
            _mark_task_paid_pending_sync(
                task,
                (
                    f"支付已确认（payment_status={paid_hint_status}, reason={paid_hint_reason}）。"
                    "等待订阅状态同步，可点击“同步订阅”刷新。"
                ),
            )
            db.commit()
            db.refresh(task)
            return {
                "success": True,
                "verified": False,
                "paid_confirmed": True,
                "subscription_type": str(account.subscription_type or "free"),
                "task": _serialize_bind_card_task(task),
                "account_id": account.id,
                "account_email": account.email,
                "third_party": {
                    "submitted": True,
                    "api_url": api_url_for_log,
                    "assessment": third_party_assessment,
                    "poll": third_party_status_poll,
                    "response": third_party_response_safe,
                },
            }
        except HTTPException:
            raise
        except Exception as exc:
            task.status = "failed"
            task.last_error = f"第三方绑卡提交失败: {exc}"
            task.last_checked_at = datetime.utcnow()
            db.commit()
            logger.warning("第三方自动绑卡提交失败: task_id=%s error=%s", task.id, exc)
            raise HTTPException(status_code=500, detail=str(exc))


@router.post("/bind-card/tasks/{task_id}/auto-bind-local")
def auto_bind_bind_card_task_local(task_id: int, request: LocalAutoBindRequest):
    """
    本地自动绑卡（参考 ABCard 的浏览器自动化流程）。
    - 成功信号后标记 paid_pending_sync（等待订阅同步）
    - challenge/超时等待用户完成时，回到 waiting_user_action
    """
    browser_result: dict = {}
    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        account = task.account
        if not account:
            raise HTTPException(status_code=404, detail="任务关联账号不存在")

        checkout_session_id = str(task.checkout_session_id or "").strip() or _extract_checkout_session_id_from_url(task.checkout_url)
        checkout_url = (
            _build_official_checkout_url(checkout_session_id)
            or str(task.checkout_url or "").strip()
        )
        if not checkout_url:
            raise HTTPException(status_code=400, detail="任务缺少 checkout 链接，请重新创建任务")

        card_number = str(request.card.number or "").strip()
        card_cvc = str(request.card.cvc or "").strip()
        if not card_number or not card_cvc:
            raise HTTPException(status_code=400, detail="卡号/CVC 不能为空")

        task.bind_mode = "local_auto"
        task.status = "verifying"
        task.last_error = None
        task.last_checked_at = datetime.utcnow()
        db.commit()

        logger.info(
            "本地自动绑卡执行开始: task_id=%s account_id=%s email=%s checkout=%s headless=%s card=%s",
            task.id,
            account.id,
            account.email,
            checkout_url[:80],
            bool(request.headless),
            _mask_card_number(card_number),
        )
        runtime_proxy = _resolve_runtime_proxy(request.proxy, account)
        resolved_device_id = _resolve_account_device_id(account)
        if not resolved_device_id:
            logger.warning("本地自动绑卡缺少 oai-did: task_id=%s account_id=%s email=%s", task.id, account.id, account.email)
        resolved_session_token = str(account.session_token or "").strip() or _extract_session_token_from_cookie_text(
            account.cookies
        )
        if not resolved_session_token and not runtime_proxy:
            task.status = "failed"
            task.last_checked_at = datetime.utcnow()
            task.last_error = (
                "当前账号缺少 session_token，且未检测到可用代理。"
                "请在设置中配置代理（或为本次任务传入 proxy）后重试全自动。"
            )
            db.commit()
            logger.warning(
                "本地自动绑卡会话补全阻断: task_id=%s account_id=%s email=%s reason=no_proxy",
                task.id,
                account.id,
                account.email,
            )
            raise HTTPException(status_code=400, detail=task.last_error)
        if not resolved_session_token:
            resolved_session_token = _bootstrap_session_token_for_local_auto(
                db=db,
                account=account,
                proxy=runtime_proxy,
            )
        if not resolved_session_token:
            task.status = "failed"
            task.last_checked_at = datetime.utcnow()
            task.last_error = (
                "会话补全未拿到 session_token。"
                "请先在支付页执行“会话诊断/自动补会话”，"
                "或手动粘贴 session_token 后再重试全自动绑卡。"
            )
            db.commit()
            logger.warning(
                "本地自动绑卡缺少 session_token，已阻断执行: task_id=%s account_id=%s email=%s",
                task.id,
                account.id,
                account.email,
            )
            raise HTTPException(status_code=400, detail=task.last_error)

        try:
            browser_result = auto_bind_checkout_with_playwright(
                checkout_url=checkout_url,
                cookies_str=str(account.cookies or ""),
                session_token=resolved_session_token,
                access_token=str(account.access_token or ""),
                device_id=resolved_device_id,
                card_number=card_number,
                exp_month=str(request.card.exp_month or ""),
                exp_year=str(request.card.exp_year or ""),
                cvc=card_cvc,
                billing_name=str(request.profile.name or "").strip(),
                billing_country=str(request.profile.country or "US").strip().upper(),
                billing_line1=str(request.profile.line1 or "").strip(),
                billing_city=str(request.profile.city or "").strip(),
                billing_state=str(request.profile.state or "").strip(),
                billing_postal=str(request.profile.postal or "").strip(),
                proxy=runtime_proxy,
                timeout_seconds=request.browser_timeout_seconds,
                post_submit_wait_seconds=request.post_submit_wait_seconds,
                headless=bool(request.headless),
            )
        except Exception as exc:
            task.status = "failed"
            task.last_error = f"本地自动绑卡执行异常: {exc}"
            task.last_checked_at = datetime.utcnow()
            db.commit()
            logger.warning("本地自动绑卡执行异常: task_id=%s account_id=%s error=%s", task.id, account.id, exc)
            raise HTTPException(status_code=500, detail=f"本地自动绑卡执行失败: {exc}")

        success = bool(browser_result.get("success"))
        need_user_action = bool(browser_result.get("need_user_action"))
        pending = bool(browser_result.get("pending"))
        stage = str(browser_result.get("stage") or "").strip()
        message = str(browser_result.get("error") or browser_result.get("message") or "").strip()
        current_url = str(browser_result.get("current_url") or "").strip()

        if not success:
            if "playwright not installed" in message.lower():
                task.status = "failed"
                task.last_checked_at = datetime.utcnow()
                task.last_error = (
                    "本地自动绑卡环境缺少 Playwright/Chromium。"
                    "请先执行: pip install playwright && playwright install chromium"
                )
                db.commit()
                logger.warning(
                    "本地自动绑卡环境缺失: task_id=%s account_id=%s error=%s",
                    task.id,
                    account.id,
                    message or "-",
                )
                raise HTTPException(status_code=400, detail=task.last_error)

            if need_user_action or pending:
                if stage in ("cdp_session_missing", "navigate_checkout"):
                    hint = (
                        "自动会话未建立（checkout 重定向到首页）。"
                        "请先在“支付页-半自动”打开一次官方 checkout 完成登录态预热，"
                        "随后再次执行“全自动”。"
                    )
                else:
                    hint = (
                        f"本地自动绑卡已执行到 {stage or 'unknown'}，当前需要人工继续完成（{message or 'challenge_or_pending'}）。"
                        "请在支付页完成后点击“我已完成支付”或“同步订阅”。"
                    )
                manual_opened = False
                if stage in ("cdp_challenge", "challenge"):
                    try:
                        manual_opened = open_url_incognito(checkout_url, str(account.cookies or ""))
                    except Exception:
                        manual_opened = False
                    if manual_opened:
                        hint += " 已自动为你打开手动验证窗口。"
                task.status = "waiting_user_action"
                task.last_checked_at = datetime.utcnow()
                task.last_error = hint
                db.commit()
                db.refresh(task)
                logger.info(
                    "本地自动绑卡需人工继续: task_id=%s account_id=%s stage=%s msg=%s url=%s",
                    task.id,
                    account.id,
                    stage or "-",
                    message or "-",
                    current_url[:100] if current_url else "-",
                )
                return {
                    "success": True,
                    "verified": False,
                    "pending": True,
                    "need_user_action": True,
                    "subscription_type": str(account.subscription_type or "free"),
                    "task": _serialize_bind_card_task(task),
                    "account_id": account.id,
                    "account_email": account.email,
                    "manual_opened": manual_opened,
                    "local_auto": browser_result,
                }

            task.status = "failed"
            task.last_checked_at = datetime.utcnow()
            task.last_error = f"本地自动绑卡失败: {message or 'unknown_error'}"
            db.commit()
            logger.warning(
                "本地自动绑卡失败: task_id=%s account_id=%s stage=%s msg=%s",
                task.id,
                account.id,
                stage or "-",
                message or "-",
            )
            raise HTTPException(status_code=400, detail=task.last_error)

        logger.info(
            "本地自动绑卡浏览器阶段成功: task_id=%s account_id=%s stage=%s url=%s",
            task.id,
            account.id,
            stage or "-",
            current_url[:100] if current_url else "-",
        )
        _mark_task_paid_pending_sync(
            task,
            (
                f"本地自动绑卡已完成支付提交（stage={stage or 'complete'}）。"
                "等待订阅状态同步，可点击“同步订阅”刷新。"
            ),
        )
        db.commit()
        db.refresh(task)
        return {
            "success": True,
            "verified": False,
            "paid_confirmed": True,
            "subscription_type": str(account.subscription_type or "free"),
            "task": _serialize_bind_card_task(task),
            "account_id": account.id,
            "account_email": account.email,
            "local_auto": browser_result,
        }


@router.post("/bind-card/tasks/{task_id}/auto-bind-vendor")
def auto_bind_bind_card_task_vendor(task_id: int, request: VendorAutoBindRequest):
    """
    卡商全自动模式（接口版）：
    - 先强制按账号生成最新 checkout（生成阶段走代理）
    - 再调用 EFun 开卡接口获取卡信息
    - 提交 card.aimizy.com /api/v1/bindcard
    - 最后同步订阅状态
    """
    redeem_code = _normalize_vendor_redeem_code(request.redeem_code)
    if not redeem_code:
        raise HTTPException(status_code=400, detail="兑换码不能为空")
    if not VENDOR_REDEEM_CODE_REGEX.fullmatch(redeem_code):
        raise HTTPException(status_code=400, detail="兑换码格式错误")

    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        account = task.account
        if not account:
            raise HTTPException(status_code=404, detail="任务关联账号不存在")

        checkout_override = str(request.checkout_url or "").strip()
        if checkout_override:
            parsed = urlparse(checkout_override)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise HTTPException(status_code=400, detail="自定义 Checkout 链接格式无效")
            task.checkout_url = checkout_override
            task.checkout_session_id = _extract_checkout_session_id_from_url(checkout_override)

        task.bind_mode = "vendor_efun"
        task.status = "verifying"
        task.last_error = None
        task.last_checked_at = datetime.utcnow()
        db.commit()
        db.refresh(task)

    _vendor_progress_init(task_id)
    _vendor_progress_log(
        task_id,
        "已开始卡商接口订阅流程（EFun 开卡 + bindcard 提交 + 订阅同步）",
        progress=8,
        status="running",
    )

    thread = threading.Thread(
        target=_run_vendor_auto_bind_task,
        kwargs={
            "task_id": task_id,
            "redeem_code": redeem_code,
            "checkout_override": str(request.checkout_url or "").strip() or None,
            "api_url": str(request.api_url or "").strip() or None,
            "api_key": str(request.api_key or "").strip() or None,
            "timeout_seconds": int(request.timeout_seconds),
        },
        daemon=True,
    )
    thread.start()

    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        return {
            "success": True,
            "pending": True,
            "task": _serialize_bind_card_task(task) if task else {"id": task_id},
            "progress": _vendor_progress_snapshot(task_id, 0),
        }


@router.get("/bind-card/tasks/{task_id}/vendor-progress")
def get_bind_card_task_vendor_progress(task_id: int, cursor: int = Query(0, ge=0)):
    snapshot = _vendor_progress_snapshot(task_id, cursor)
    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        if not task:
            if _vendor_progress_exists(task_id):
                # 兼容多实例/多数据库文件场景：进度仍在，但任务查询暂未命中时继续返回进度
                return {
                    "success": True,
                    "task": {
                        "id": task_id,
                        "status": str(snapshot.get("status") or "running"),
                        "bind_mode": "vendor_efun",
                        "account_email": None,
                    },
                    "progress": snapshot,
                    "task_missing": True,
                }
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        return {
            "success": True,
            "task": _serialize_bind_card_task(task),
            "progress": snapshot,
        }


@router.post("/bind-card/vendor-stop-active")
def stop_active_bind_card_task_vendor():
    task_id = _vendor_get_latest_active_task_id()
    if not task_id:
        raise HTTPException(status_code=404, detail="当前没有进行中的卡商任务")
    return stop_bind_card_task_vendor(task_id)


@router.post("/bind-card/tasks/{task_id}/vendor-stop")
@router.post("/bind-card/tasks/{task_id}/stop-vendor")
@router.post("/bind-card/tasks/{task_id}/stop")
@router.post("/bind-card/tasks/{task_id}/cancel")
def stop_bind_card_task_vendor(task_id: int):
    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        if not task:
            if _vendor_progress_exists(task_id):
                requested_now = _vendor_request_stop(task_id)
                if requested_now:
                    _vendor_progress_log(task_id, "已收到停止指令，正在停止任务...", progress=99, status="cancelled")
                else:
                    _vendor_progress_log(task_id, "停止指令已存在，等待任务退出", progress=99, status="cancelled")
                return {
                    "success": True,
                    "stopped": True,
                    "task_missing": True,
                    "task": {"id": task_id, "bind_mode": "vendor_efun", "status": "cancelled"},
                    "progress": _vendor_progress_snapshot(task_id, 0),
                }
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        if str(task.bind_mode or "").strip().lower() not in ("vendor_auto", "vendor_efun"):
            raise HTTPException(status_code=400, detail="仅卡商模式任务支持停止")

        task_status = str(task.status or "").lower()
        if task_status in ("completed", "failed"):
            return {
                "success": True,
                "stopped": False,
                "message": "任务已结束，无需停止",
                "task": _serialize_bind_card_task(task),
                "progress": _vendor_progress_snapshot(task_id, 0),
            }

        requested_now = _vendor_request_stop(task_id)
        now = datetime.utcnow()
        task.status = "failed"
        task.last_error = "用户手动停止卡商订阅任务"
        task.last_checked_at = now
        db.commit()
        db.refresh(task)

        if requested_now:
            _vendor_progress_log(task_id, "已收到停止指令，正在停止任务...", progress=99, status="cancelled")
        else:
            _vendor_progress_log(task_id, "停止指令已存在，等待任务退出", progress=99, status="cancelled")

        return {
            "success": True,
            "stopped": True,
            "task": _serialize_bind_card_task(task),
            "progress": _vendor_progress_snapshot(task_id, 0),
        }


@router.post("/bind-card/tasks/{task_id}/sync-subscription")
def sync_bind_card_task_subscription(task_id: int, request: SyncBindCardTaskRequest):
    """同步任务账号订阅状态，并回写到账号管理"""
    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        account = task.account
        if not account:
            raise HTTPException(status_code=404, detail="任务关联账号不存在")

        proxy = _resolve_runtime_proxy(request.proxy, account)
        now = datetime.utcnow()
        try:
            detail, refreshed = _check_subscription_detail_with_retry(
                db=db,
                account=account,
                proxy=proxy,
                allow_token_refresh=True,
            )
            status = str(detail.get("status") or "free").lower()
            source = str(detail.get("source") or "unknown")
            confidence = str(detail.get("confidence") or "low")
            logger.info(
                "绑卡任务同步订阅: task_id=%s account_id=%s status=%s source=%s confidence=%s token_refreshed=%s",
                task.id, account.id, status, source, confidence, refreshed
            )
        except Exception as exc:
            task.status = "failed"
            task.last_error = str(exc)
            task.last_checked_at = now
            db.commit()
            logger.warning("绑卡任务同步订阅失败: task_id=%s error=%s", task.id, exc)
            raise HTTPException(status_code=500, detail=f"订阅检测失败: {exc}")

        # 仅在高置信度 free 时清空；低置信度 free 不覆盖已有订阅
        _apply_subscription_result(
            account,
            status=status,
            checked_at=now,
            confidence=detail.get("confidence"),
            promote_reason="bind_task_sync_subscription",
        )

        task.last_checked_at = now
        if status in ("plus", "team"):
            task.status = "completed"
            task.completed_at = now
            task.last_error = None
        else:
            task.completed_at = None
            note = str(detail.get("note") or "").strip()
            confidence_text = str(detail.get("confidence") or "unknown")
            confidence_lower = confidence_text.lower()
            source_text = str(detail.get("source") or "unknown")
            task_status_now = str(task.status or "")
            has_checkout_context = bool(task.checkout_session_id or task.checkout_url)
            should_keep_paid_pending = (
                task_status_now == "paid_pending_sync"
                or (
                    status == "free"
                    and confidence_lower != "high"
                    and has_checkout_context
                    and task_status_now in ("waiting_user_action", "verifying", "link_ready")
                )
            )
            if should_keep_paid_pending:
                task.status = "paid_pending_sync"
                task.last_error = (
                    f"已确认支付，订阅暂未同步（当前: {status}, source={source_text}, "
                    f"confidence={confidence_text}"
                    + (f", note={note}" if note else "")
                    + "）。可稍后再次点击“同步订阅”。"
                )
            else:
                task.status = "waiting_user_action"
                task.last_error = None
                if confidence_lower != "high":
                    task.last_error = (
                        f"订阅判定低置信度（source={source_text}, "
                        f"confidence={confidence_text}"
                        + (f", note={note}" if note else "")
                        + "），请稍后重试。"
                    )

        db.commit()
        db.refresh(task)

        return {
            "success": True,
            "subscription_type": status,
            "detail": detail,
            "task": _serialize_bind_card_task(task),
            "account_id": account.id,
            "account_email": account.email,
        }


def _execute_mark_user_action(
    task_id: int,
    request: MarkUserActionRequest,
    cancel_checker: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    with get_db() as db:
        task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        account = task.account
        if not account:
            raise HTTPException(status_code=404, detail="任务关联账号不存在")

        proxy = _resolve_runtime_proxy(request.proxy, account)
        timeout_seconds = int(request.timeout_seconds)
        interval_seconds = int(request.interval_seconds)
        logger.info(
            "绑卡任务开始验证: task_id=%s account_id=%s timeout=%ss interval=%ss",
            task.id, account.id, timeout_seconds, interval_seconds
        )

        previous_status = str(task.status or "")
        now = datetime.utcnow()
        task.status = "verifying"
        task.last_error = None
        task.last_checked_at = now
        db.commit()

        deadline = time.monotonic() + timeout_seconds
        checks = 0
        last_status = "free"
        last_detail: Dict[str, Any] = {}
        token_refresh_used = False

        while time.monotonic() < deadline:
            if callable(cancel_checker) and cancel_checker():
                fallback_status = previous_status if previous_status in ("paid_pending_sync", "waiting_user_action", "link_ready") else "waiting_user_action"
                task.status = fallback_status
                task.last_error = "用户取消了验证任务"
                task.last_checked_at = datetime.utcnow()
                task.completed_at = None
                db.commit()
                db.refresh(task)
                return {
                    "success": True,
                    "verified": False,
                    "cancelled": True,
                    "checks": checks,
                    "subscription_type": last_status,
                    "detail": last_detail or None,
                    "token_refresh_used": token_refresh_used,
                    "task": _serialize_bind_card_task(task),
                    "account_id": account.id,
                    "account_email": account.email,
                }

            checks += 1
            try:
                detail, refreshed = _check_subscription_detail_with_retry(
                    db=db,
                    account=account,
                    proxy=proxy,
                    allow_token_refresh=not token_refresh_used,
                )
                if refreshed:
                    token_refresh_used = True
                status = str(detail.get("status") or "free").lower()
                last_status = status
                last_detail = detail if isinstance(detail, dict) else {}
                logger.info(
                    "绑卡任务验证轮询: task_id=%s attempt=%s status=%s source=%s confidence=%s token_refreshed=%s",
                    task.id, checks, status, detail.get("source"), detail.get("confidence"), bool(detail.get("token_refreshed"))
                )
            except Exception as exc:
                failed_at = datetime.utcnow()
                task.status = "failed"
                task.last_error = f"订阅检测失败: {exc}"
                task.last_checked_at = failed_at
                db.commit()
                logger.warning("绑卡任务验证失败: task_id=%s attempt=%s error=%s", task.id, checks, exc)
                raise HTTPException(status_code=500, detail=f"订阅检测失败: {exc}")

            checked_at = datetime.utcnow()
            _apply_subscription_result(
                account,
                status=status,
                checked_at=checked_at,
                confidence=detail.get("confidence"),
                promote_reason="bind_task_verify_polling",
            )
            task.last_checked_at = checked_at

            if callable(progress_callback):
                try:
                    progress_callback(
                        {
                            "checks": checks,
                            "timeout_seconds": timeout_seconds,
                            "interval_seconds": interval_seconds,
                            "status": status,
                            "source": detail.get("source"),
                            "confidence": detail.get("confidence"),
                        }
                    )
                except Exception:
                    pass

            if status in ("plus", "team"):
                task.status = "completed"
                task.completed_at = checked_at
                task.last_error = None
                db.commit()
                db.refresh(task)
                logger.info(
                    "绑卡任务验证成功: task_id=%s attempts=%s status=%s source=%s",
                    task.id, checks, status, detail.get("source")
                )
                return {
                    "success": True,
                    "verified": True,
                    "cancelled": False,
                    "checks": checks,
                    "subscription_type": status,
                    "detail": detail,
                    "token_refresh_used": token_refresh_used,
                    "task": _serialize_bind_card_task(task),
                    "account_id": account.id,
                    "account_email": account.email,
                }

            db.commit()
            if time.monotonic() + interval_seconds >= deadline:
                break
            time.sleep(interval_seconds)

        timeout_msg = (
            f"在 {timeout_seconds} 秒内未检测到订阅变更（当前: {last_status}，"
            f"source={last_detail.get('source') or 'unknown'}，"
            f"confidence={last_detail.get('confidence') or 'unknown'}，"
            f"token_refreshed={token_refresh_used}"
            + (
                f"，note={str(last_detail.get('note') or '').strip()}"
                if last_detail and last_detail.get("note")
                else ""
            )
            + "）。"
        )
        timeout_confidence = str(last_detail.get("confidence") or "unknown").lower()
        timeout_source = str(last_detail.get("source") or "unknown")
        should_keep_paid_pending = (
            previous_status == "paid_pending_sync"
            or (last_status == "free" and timeout_confidence != "high")
        )
        if should_keep_paid_pending:
            task.status = "paid_pending_sync"
            task.last_error = (
                timeout_msg
                + "支付可能已完成但订阅同步延迟"
                + f"（source={timeout_source}, confidence={timeout_confidence}）。"
                + "请稍后点“同步订阅”重试。"
            )
        else:
            task.status = "waiting_user_action"
            task.last_error = timeout_msg + "请稍后点击“同步订阅”重试。"
        task.last_checked_at = datetime.utcnow()
        task.completed_at = None
        db.commit()
        db.refresh(task)
        logger.warning(
            "绑卡任务验证超时: task_id=%s attempts=%s last_status=%s last_error=%s",
            task.id, checks, last_status, task.last_error
        )

        return {
            "success": True,
            "verified": False,
            "cancelled": False,
            "checks": checks,
            "subscription_type": last_status,
            "detail": last_detail or None,
            "token_refresh_used": token_refresh_used,
            "task": _serialize_bind_card_task(task),
            "account_id": account.id,
            "account_email": account.email,
        }


def _run_mark_user_action_async_task(op_task_id: str, bind_task_id: int, payload: Dict[str, Any]) -> None:
    timeout_seconds = int(payload.get("timeout_seconds") or 180)
    interval_seconds = int(payload.get("interval_seconds") or 10)
    _update_payment_op_task(
        op_task_id,
        status="running",
        started_at=_payment_now_iso(),
        message=f"开始验证订阅（最长 {timeout_seconds} 秒）",
    )
    _set_payment_op_task_progress(
        op_task_id,
        checks=0,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )

    request = MarkUserActionRequest(
        proxy=payload.get("proxy"),
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )

    result = _execute_mark_user_action(
        bind_task_id,
        request,
        cancel_checker=lambda: _is_payment_op_task_cancel_requested(op_task_id),
        progress_callback=lambda progress: _set_payment_op_task_progress(op_task_id, **(progress or {})),
    )
    _append_payment_op_task_detail(
        op_task_id,
        {
            "checked_at": _payment_now_iso(),
            "subscription_type": result.get("subscription_type"),
            "verified": bool(result.get("verified")),
            "cancelled": bool(result.get("cancelled")),
            "checks": int(result.get("checks") or 0),
        },
    )
    final_status = "cancelled" if result.get("cancelled") else "completed"
    _update_payment_op_task(
        op_task_id,
        status=final_status,
        finished_at=_payment_now_iso(),
        message=(
            "任务已取消"
            if final_status == "cancelled"
            else ("验证成功" if result.get("verified") else "验证完成，未命中订阅")
        ),
        result=result,
    )


@router.post("/bind-card/tasks/{task_id}/mark-user-action/async")
def start_mark_bind_card_task_user_action_async(task_id: int, request: MarkUserActionRequest):
    payload = {
        "proxy": request.proxy,
        "timeout_seconds": int(request.timeout_seconds),
        "interval_seconds": int(request.interval_seconds),
    }
    op_task_id = _create_payment_op_task(
        "mark_user_action",
        bind_task_id=task_id,
        progress={
            "checks": 0,
            "timeout_seconds": int(request.timeout_seconds),
            "interval_seconds": int(request.interval_seconds),
        },
    )
    task_manager.update_domain_task(
        "payment",
        op_task_id,
        payload={
            "bind_task_id": int(task_id),
            "proxy": request.proxy,
            "timeout_seconds": int(request.timeout_seconds),
            "interval_seconds": int(request.interval_seconds),
        },
    )
    _PAYMENT_OP_TASK_EXECUTOR.submit(
        _run_payment_op_task_guard,
        op_task_id,
        "mark_user_action",
        _run_mark_user_action_async_task,
        task_id,
        payload,
    )
    return _build_payment_op_task_snapshot(_get_payment_op_task_or_404(op_task_id))


@router.get("/ops/tasks/{op_task_id}")
def get_payment_op_task(op_task_id: str):
    return _build_payment_op_task_snapshot(_get_payment_op_task_or_404(op_task_id))


@router.post("/ops/tasks/{op_task_id}/cancel")
def cancel_payment_op_task(op_task_id: str):
    with _PAYMENT_OP_TASK_LOCK:
        task = _PAYMENT_OP_TASKS.get(op_task_id)
        if not task:
            raise HTTPException(status_code=404, detail="支付任务不存在")
        if task.get("status") in {"completed", "failed", "cancelled"}:
            return {
                "success": True,
                "task_id": op_task_id,
                "status": task.get("status"),
                "message": "任务已结束，无需取消",
            }
        task["cancel_requested"] = True
        task["pause_requested"] = False
        task["paused"] = False
        task["message"] = "已提交取消请求，等待任务结束"
    task_manager.request_domain_task_cancel("payment", op_task_id)
    return {"success": True, "task_id": op_task_id, "status": "cancelling"}


@router.post("/ops/tasks/{op_task_id}/pause")
def pause_payment_op_task(op_task_id: str):
    with _PAYMENT_OP_TASK_LOCK:
        task = _PAYMENT_OP_TASKS.get(op_task_id)
        if not task:
            raise HTTPException(status_code=404, detail="支付任务不存在")
        status = str(task.get("status") or "").strip().lower()
        if status in {"completed", "failed", "cancelled"}:
            return {
                "success": True,
                "task_id": op_task_id,
                "status": status,
                "message": "任务已结束，无法暂停",
            }
        task["pause_requested"] = True
        task["paused"] = True
        task["status"] = "paused"
        task["message"] = "任务已暂停，等待继续"
    task_manager.request_domain_task_pause("payment", op_task_id)
    return {"success": True, "task_id": op_task_id, "status": "paused", "message": "任务已暂停"}


@router.post("/ops/tasks/{op_task_id}/resume")
def resume_payment_op_task(op_task_id: str):
    with _PAYMENT_OP_TASK_LOCK:
        task = _PAYMENT_OP_TASKS.get(op_task_id)
        if not task:
            raise HTTPException(status_code=404, detail="支付任务不存在")
        status = str(task.get("status") or "").strip().lower()
        if status in {"completed", "failed", "cancelled"}:
            return {
                "success": True,
                "task_id": op_task_id,
                "status": status,
                "message": "任务已结束，无需继续",
            }
        task["pause_requested"] = False
        task["paused"] = False
        if status == "paused":
            task["status"] = "running"
            task["message"] = "任务已继续执行"
    task_manager.request_domain_task_resume("payment", op_task_id)
    return {"success": True, "task_id": op_task_id, "status": "running", "message": "任务已继续执行"}


def retry_payment_op_task(op_task_id: str) -> Dict[str, Any]:
    task = _get_payment_op_task_or_404(op_task_id)
    task_type = str(task.get("task_type") or "").strip().lower()
    payload = dict(task.get("payload") or {})

    if task_type == "batch_check_subscription":
        return start_batch_check_subscription_async(
            BatchCheckSubscriptionRequest(
                ids=[int(item) for item in (payload.get("ids") or []) if str(item).strip().isdigit()],
                proxy=payload.get("proxy"),
                select_all=bool(payload.get("select_all", False)),
                status_filter=payload.get("status_filter"),
                email_service_filter=payload.get("email_service_filter"),
                search_filter=payload.get("search_filter"),
            )
        )

    if task_type == "mark_user_action":
        bind_task_id = int(payload.get("bind_task_id") or task.get("bind_task_id") or 0)
        if bind_task_id <= 0:
            raise HTTPException(status_code=400, detail="mark_user_action 缺少 bind_task_id，无法重试")
        return start_mark_bind_card_task_user_action_async(
            bind_task_id,
            MarkUserActionRequest(
                proxy=payload.get("proxy"),
                timeout_seconds=int(payload.get("timeout_seconds") or 180),
                interval_seconds=int(payload.get("interval_seconds") or 10),
            ),
        )

    raise HTTPException(status_code=400, detail=f"不支持重试的支付任务类型: {task_type or '-'}")


@router.post("/bind-card/tasks/{task_id}/mark-user-action")
def mark_bind_card_task_user_action(task_id: int, request: MarkUserActionRequest):
    """
    用户确认“已完成支付”后，自动轮询订阅状态一段时间：
    - 命中 plus/team -> completed
    - 超时未命中 -> paid_pending_sync 或 waiting_user_action
    """
    return _execute_mark_user_action(task_id, request)


@router.delete("/bind-card/tasks/{task_id}")
def delete_bind_card_task(task_id: int):
    """删除绑卡任务"""
    with get_db() as db:
        task = db.query(BindCardTask).filter(BindCardTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="绑卡任务不存在")
        logger.info("删除绑卡任务: task_id=%s account_id=%s", task.id, task.account_id)
        db.delete(task)
        db.commit()
    return {"success": True, "task_id": task_id}


# ============== 订阅状态 ==============

@router.post("/accounts/batch-check-subscription")
def batch_check_subscription(request: BatchCheckSubscriptionRequest):
    """批量检测账号订阅状态"""
    explicit_proxy = _normalize_proxy_value(request.proxy)
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
    results = {"success_count": 0, "failed_count": 0, "details": []}
    if not ids:
        return results

    worker_count = min(PAYMENT_BATCH_SUBSCRIPTION_CHECK_MAX_WORKERS, max(1, len(ids)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="payment_sub_check") as pool:
        future_map = {
            pool.submit(_batch_check_subscription_one, int(account_id), explicit_proxy): int(account_id)
            for account_id in ids
        }
        for future in as_completed(future_map):
            try:
                detail = future.result()
            except Exception as exc:
                detail = {
                    "id": future_map.get(future),
                    "email": None,
                    "success": False,
                    "error": str(exc),
                    "attempts": 1,
                }
            if detail.get("success"):
                results["success_count"] += 1
            else:
                results["failed_count"] += 1
            results["details"].append(detail)
    return results


@router.post("/accounts/batch-check-subscription/async")
def start_batch_check_subscription_async(request: BatchCheckSubscriptionRequest):
    """异步批量检测账号订阅状态（并发执行）。"""
    explicit_proxy = _normalize_proxy_value(request.proxy)
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    op_task_id = _create_payment_op_task(
        "batch_check_subscription",
        progress={"total": len(ids), "completed": 0, "success": 0, "failed": 0},
    )
    task_manager.update_domain_task(
        "payment",
        op_task_id,
        payload={
            "ids": [int(item) for item in ids],
            "proxy": explicit_proxy,
            "select_all": bool(request.select_all),
            "status_filter": request.status_filter,
            "email_service_filter": request.email_service_filter,
            "search_filter": request.search_filter,
        },
    )

    if not ids:
        _update_payment_op_task(
            op_task_id,
            status="completed",
            started_at=_payment_now_iso(),
            finished_at=_payment_now_iso(),
            message="没有可检测的账号",
            result={"success_count": 0, "failed_count": 0, "total": 0, "cancelled": False, "details": []},
        )
    else:
        _PAYMENT_OP_TASK_EXECUTOR.submit(
            _run_payment_op_task_guard,
            op_task_id,
            "batch_check_subscription",
            _run_batch_check_subscription_async_task,
            ids,
            explicit_proxy,
        )
    return _build_payment_op_task_snapshot(_get_payment_op_task_or_404(op_task_id))


@router.post("/accounts/{account_id}/mark-subscription")
def mark_subscription(account_id: int, request: MarkSubscriptionRequest, http_request: Request):
    """手动标记账号订阅类型"""
    allowed = ("free", "plus", "team")
    if request.subscription_type not in allowed:
        raise HTTPException(status_code=400, detail=f"subscription_type 必须为 {allowed}")

    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        actor = _resolve_actor(http_request)
        before = {
            "subscription_type": account.subscription_type,
            "subscription_at": account.subscription_at.isoformat() if account.subscription_at else None,
        }

        now = datetime.utcnow()
        _apply_subscription_result(
            account,
            status=request.subscription_type,
            checked_at=now,
            confidence="high",
            promote_reason="manual_mark_subscription",
        )
        db.commit()
        crud.create_operation_audit_log(
            db,
            actor=actor,
            action="account.subscription_manual_mark",
            target_type="account",
            target_id=account.id,
            target_email=account.email,
            payload={
                "before": before,
                "after": {
                    "subscription_type": account.subscription_type,
                    "subscription_at": account.subscription_at.isoformat() if account.subscription_at else None,
                },
            },
        )

    return {"success": True, "subscription_type": request.subscription_type}


