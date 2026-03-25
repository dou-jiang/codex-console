"""
支付相关 API 路由
"""

import logging
import os
import re
import uuid
from typing import Optional, List
from datetime import datetime
import time
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import joinedload
from curl_cffi import requests as cffi_requests

from ...database.session import get_db
from ...database.models import Account, BindCardTask, EmailService as EmailServiceModel
from ...config.settings import get_settings
from ...config.constants import OPENAI_PAGE_TYPES
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


def _serialize_bind_card_task(task: BindCardTask) -> dict:
    account_email = task.account.email if task.account else None
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
    bind_mode: str = "semi_auto"  # semi_auto / third_party / local_auto


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


class MarkSubscriptionRequest(BaseModel):
    subscription_type: str  # 'free' / 'plus' / 'team'


class BatchCheckSubscriptionRequest(BaseModel):
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


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
    if bind_mode not in ("semi_auto", "third_party", "local_auto"):
        raise HTTPException(status_code=400, detail="bind_mode 必须为 semi_auto / third_party / local_auto")

    with get_db() as db:
        account = db.query(Account).filter(Account.id == request.account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        logger.info(
            "创建绑卡任务: account_id=%s email=%s plan=%s country=%s mode=%s auto_open=%s",
            account.id, account.email, request.plan_type, request.country, bind_mode, request.auto_open
        )

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
            logger.error(f"创建绑卡任务失败: {e}")
            raise HTTPException(status_code=500, detail=f"创建绑卡任务失败: {str(e)}")

        task = BindCardTask(
            account_id=account.id,
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
        if request.auto_open and bind_mode == "semi_auto" and link:
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
            query = query.join(Account, BindCardTask.account_id == Account.id).filter(
                or_(Account.email.ilike(pattern), Account.account_id.ilike(pattern))
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
        if status in ("plus", "team"):
            account.subscription_type = status
            account.subscription_at = now
        elif status == "free":
            if str(detail.get("confidence") or "").lower() == "high":
                account.subscription_type = None
                account.subscription_at = None

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


@router.post("/bind-card/tasks/{task_id}/mark-user-action")
def mark_bind_card_task_user_action(task_id: int, request: MarkUserActionRequest):
    """
    用户确认“已完成支付”后，自动轮询订阅状态一段时间：
    - 命中 plus/team -> completed
    - 超时未命中 -> paid_pending_sync 或 waiting_user_action
    """
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
        token_refresh_used = False

        while time.monotonic() < deadline:
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
            if status in ("plus", "team"):
                account.subscription_type = status
                account.subscription_at = checked_at
            elif status == "free":
                # 低置信度 free 不覆盖已有订阅，避免误判清空
                if str(detail.get("confidence") or "").lower() == "high":
                    account.subscription_type = None
                    account.subscription_at = None
            task.last_checked_at = checked_at

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
            f"source={detail.get('source') if 'detail' in locals() else 'unknown'}，"
            f"confidence={detail.get('confidence') if 'detail' in locals() else 'unknown'}，"
            f"token_refreshed={token_refresh_used}"
            + (
                f"，note={str(detail.get('note') or '').strip()}"
                if "detail" in locals() and detail and detail.get("note")
                else ""
            )
            + "）。"
        )
        timeout_confidence = str((detail if "detail" in locals() else {}).get("confidence") or "unknown").lower()
        timeout_source = str((detail if "detail" in locals() else {}).get("source") or "unknown")
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
            "checks": checks,
            "subscription_type": last_status,
            "detail": detail if "detail" in locals() else None,
            "token_refresh_used": token_refresh_used,
            "task": _serialize_bind_card_task(task),
            "account_id": account.id,
            "account_email": account.email,
        }


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

    results = {"success_count": 0, "failed_count": 0, "details": []}

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        for account_id in ids:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                results["failed_count"] += 1
                results["details"].append(
                    {"id": account_id, "email": None, "success": False, "error": "账号不存在"}
                )
                continue

            try:
                runtime_proxy = _resolve_runtime_proxy(explicit_proxy, account)
                detail, refreshed = _check_subscription_detail_with_retry(
                    db=db,
                    account=account,
                    proxy=runtime_proxy,
                    allow_token_refresh=True,
                )
                status = str(detail.get("status") or "free").lower()
                confidence = str(detail.get("confidence") or "low").lower()

                if status in ("plus", "team"):
                    account.subscription_type = status
                    account.subscription_at = datetime.utcnow()
                elif status == "free" and confidence == "high":
                    account.subscription_type = None
                    account.subscription_at = None

                db.commit()
                results["success_count"] += 1
                results["details"].append(
                    {
                        "id": account_id,
                        "email": account.email,
                        "success": True,
                        "subscription_type": status,
                        "confidence": confidence,
                        "source": detail.get("source"),
                        "token_refreshed": refreshed,
                    }
                )
            except Exception as e:
                results["failed_count"] += 1
                results["details"].append(
                    {"id": account_id, "email": account.email, "success": False, "error": str(e)}
                )

    return results


@router.post("/accounts/{account_id}/mark-subscription")
def mark_subscription(account_id: int, request: MarkSubscriptionRequest):
    """手动标记账号订阅类型"""
    allowed = ("free", "plus", "team")
    if request.subscription_type not in allowed:
        raise HTTPException(status_code=400, detail=f"subscription_type 必须为 {allowed}")

    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        account.subscription_type = None if request.subscription_type == "free" else request.subscription_type
        account.subscription_at = datetime.utcnow() if request.subscription_type != "free" else None
        db.commit()

    return {"success": True, "subscription_type": request.subscription_type}
