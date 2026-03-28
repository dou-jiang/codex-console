"""
支付核心逻辑 — 生成 Plus/Team 支付链接、无痕打开浏览器、检测订阅状态
"""

import logging
import base64
import json
import re
import subprocess
import sys
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urljoin, unquote

from curl_cffi import requests as cffi_requests

from ...database.models import Account
from .overview import fetch_codex_overview

logger = logging.getLogger(__name__)

PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
ACCOUNT_CHECK_URL = "https://chatgpt.com/backend-api/wham/accounts/check"
TEAM_CHECKOUT_BASE_URL = "https://chatgpt.com/checkout/openai_llc/"
AIMIZY_PAY_URL = "https://team.aimizy.com/pay"
SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
CHECKOUT_LINK_REGEX = re.compile(r"https://chatgpt\.com/checkout/openai_llc/cs_[A-Za-z0-9_-]+", re.IGNORECASE)
CHECKOUT_SESSION_REGEX = re.compile(r"\bcs_[A-Za-z0-9_-]+\b", re.IGNORECASE)
PUBLISHABLE_KEY_REGEX = re.compile(r"\bpk_(?:live|test)_[A-Za-z0-9]+\b", re.IGNORECASE)
_CONNECTIVITY_ERROR_KEYWORDS = (
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


def _build_proxies(proxy: Optional[str]) -> Optional[dict]:
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


def _is_connectivity_error(err: Any) -> bool:
    text = str(err or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in _CONNECTIVITY_ERROR_KEYWORDS)


def _extract_link_from_payload(data) -> Optional[str]:
    """从任意结构中提取 URL 字段。"""
    if isinstance(data, str):
        text = data.strip()
        if text.startswith("/checkout/openai_llc/"):
            return f"https://chatgpt.com{text}"
        if text.startswith("http://") or text.startswith("https://"):
            return text
        normalized = _extract_checkout_link_from_text(text)
        if normalized:
            return normalized
        return None

    if isinstance(data, dict):
        for key in (
            "checkout_url",
            "checkoutUrl",
            "redirect_url",
            "redirectUrl",
            "checkout_link",
            "checkoutLink",
            "short_url",
            "shortUrl",
            "short_link",
            "shortLink",
            "pay_url",
            "payUrl",
            "link",
            "url",
        ):
            value = data.get(key)
            if isinstance(value, str):
                raw = value.strip()
                if raw.startswith("/checkout/openai_llc/"):
                    return f"https://chatgpt.com{raw}"
                if raw.startswith(("http://", "https://")):
                    return raw
                normalized = _extract_checkout_link_from_text(raw)
                if normalized:
                    return normalized

        for value in data.values():
            url = _extract_link_from_payload(value)
            if url:
                return url

    if isinstance(data, list):
        for item in data:
            url = _extract_link_from_payload(item)
            if url:
                return url

    return None


def _build_checkout_link(session_id: str) -> str:
    return TEAM_CHECKOUT_BASE_URL + session_id


def _extract_checkout_session_id(text: str) -> Optional[str]:
    if not text:
        return None
    match = CHECKOUT_SESSION_REGEX.search(text)
    if match:
        return match.group(0)
    return None


def _extract_publishable_key(text: str) -> Optional[str]:
    if not text:
        return None
    match = PUBLISHABLE_KEY_REGEX.search(text)
    if match:
        return match.group(0)
    return None


def _extract_first_string_by_keys(data, keys: tuple[str, ...]) -> Optional[str]:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            nested = _extract_first_string_by_keys(value, keys)
            if nested:
                return nested
        return None
    if isinstance(data, list):
        for item in data:
            nested = _extract_first_string_by_keys(item, keys)
            if nested:
                return nested
    return None


def _extract_publishable_key_from_payload(data) -> Optional[str]:
    direct = _extract_first_string_by_keys(
        data,
        (
            "publishable_key",
            "publishableKey",
            "stripe_publishable_key",
            "stripePublishableKey",
            "pk",
        ),
    )
    if direct:
        parsed = _extract_publishable_key(direct)
        return parsed or direct

    text = str(data or "")
    return _extract_publishable_key(text)


def _build_checkout_bundle_from_payload(data, proxy: Optional[str] = None) -> Dict[str, Optional[str]]:
    checkout_url = _extract_checkout_link_from_payload(data, proxy=proxy)

    session_id = _extract_first_string_by_keys(
        data,
        ("checkout_session_id", "checkoutSessionId", "session_id", "sessionId", "id"),
    )
    if session_id and not str(session_id).startswith("cs_"):
        session_id = _extract_checkout_session_id(str(session_id))
    elif session_id:
        session_id = str(session_id).strip()

    if not session_id:
        session_id = _extract_checkout_session_id(str(checkout_url or "")) or _extract_checkout_session_id(str(data or ""))

    publishable_key = _extract_publishable_key_from_payload(data)
    client_secret = _extract_first_string_by_keys(
        data,
        ("client_secret", "clientSecret", "stripe_client_secret", "stripeClientSecret"),
    )

    return {
        "checkout_url": checkout_url,
        "checkout_session_id": session_id,
        "publishable_key": publishable_key,
        "client_secret": client_secret,
    }


def _is_official_checkout_link(link: Optional[str]) -> bool:
    return isinstance(link, str) and CHECKOUT_LINK_REGEX.search(link.strip()) is not None


def _contains_sensitive_token_in_url(link: Optional[str]) -> bool:
    if not isinstance(link, str):
        return False
    lower = link.lower()
    return (
        "access_token=" in lower
        or "accesstoken=" in lower
        or "?token=" in lower
        or "&token=" in lower
    )


def _extract_checkout_link_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    if raw.startswith("/checkout/openai_llc/"):
        return f"https://chatgpt.com{raw}"

    direct = CHECKOUT_LINK_REGEX.search(raw)
    if direct:
        return direct.group(0)

    decoded = unquote(raw)
    if decoded != raw:
        direct_decoded = CHECKOUT_LINK_REGEX.search(decoded)
        if direct_decoded:
            return direct_decoded.group(0)
        if decoded.startswith("/checkout/openai_llc/"):
            return f"https://chatgpt.com{decoded}"

    session_id = _extract_checkout_session_id(raw) or _extract_checkout_session_id(decoded)
    if session_id:
        return _build_checkout_link(session_id)

    return None


def _normalize_checkout_link(link: Optional[str], proxy: Optional[str] = None) -> Optional[str]:
    """
    将第三方返回的支付链接尽量归一化为 chatgpt 官方 checkout 链接。
    """
    if not isinstance(link, str):
        return None
    current = link.strip()
    if not current:
        return None

    normalized = _extract_checkout_link_from_text(current)
    if normalized:
        return normalized

    proxies = _build_proxies(proxy)
    # 先尝试自动重定向，部分短链依赖多跳 30x 才能落到 checkout。
    try:
        resp_follow = cffi_requests.get(
            current,
            proxies=proxies,
            timeout=25,
            impersonate="chrome110",
            allow_redirects=True,
        )
        for candidate in (
            str(getattr(resp_follow, "url", "") or ""),
            resp_follow.headers.get("Location"),
            resp_follow.headers.get("location"),
            resp_follow.text or "",
        ):
            maybe = _extract_checkout_link_from_text(candidate)
            if maybe:
                return maybe
    except Exception:
        pass

    for _ in range(5):
        try:
            resp = cffi_requests.get(
                current,
                proxies=proxies,
                timeout=20,
                impersonate="chrome110",
                allow_redirects=False,
            )
        except Exception:
            break

        location = resp.headers.get("Location") or resp.headers.get("location")
        if isinstance(location, str) and location.strip():
            next_url = urljoin(current, location.strip())
            normalized_next = _extract_checkout_link_from_text(next_url)
            if normalized_next:
                return normalized_next
            current = next_url
            continue

        body = resp.text or ""
        normalized_body = _extract_checkout_link_from_text(body)
        if normalized_body:
            return normalized_body
        break

    return link


def _extract_checkout_link_from_payload(data, proxy: Optional[str] = None) -> Optional[str]:
    """
    从响应体中提取可直达 chatgpt checkout 的链接。
    """
    if data is None:
        return None

    url = _extract_link_from_payload(data)
    if url:
        return _normalize_checkout_link(url, proxy=proxy)

    if isinstance(data, dict):
        for key in ("checkout_session_id", "session_id", "id"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith("cs_"):
                return _build_checkout_link(value.strip())
            if isinstance(value, dict):
                nested_id = value.get("id")
                if isinstance(nested_id, str) and nested_id.startswith("cs_"):
                    return _build_checkout_link(nested_id.strip())

        for value in data.values():
            nested = _extract_checkout_link_from_payload(value, proxy=proxy)
            if nested:
                return nested

    if isinstance(data, list):
        for item in data:
            nested = _extract_checkout_link_from_payload(item, proxy=proxy)
            if nested:
                return nested

    text = str(data)
    direct = CHECKOUT_LINK_REGEX.search(text)
    if direct:
        return direct.group(0)
    session_id = _extract_checkout_session_id(text)
    if session_id:
        return _build_checkout_link(session_id)
    return None


def generate_aimizy_payment_link(
    account: Account,
    plan_type: str = "plus",
    proxy: Optional[str] = None,
    country: str = "US",
    currency: str = "USD",
) -> str:
    """
    基于 Access Token 生成 aimizy 站内代付短链。
    先尝试常见 API 形态，失败后回退到可直接打开的 pay URL。
    """
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    token = account.access_token.strip()
    if not token:
        raise ValueError("账号 access_token 为空")

    payload = {
        "access_token": token,
        "accessToken": token,
        "token": token,
        "bearer": f"Bearer {token}",
        "plan_type": plan_type,
        "plan": plan_type,
        "country": country,
        "currency": currency,
        "region": country,
    }
    proxies = _build_proxies(proxy)
    common_headers = {
        "Accept": "application/json,text/plain,*/*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    attempts = [
        ("POST", AIMIZY_PAY_URL, {"json": payload, "headers": common_headers}),
        ("POST", AIMIZY_PAY_URL, {"data": payload, "headers": {"Accept": "application/json,text/plain,*/*"}}),
        ("POST", f"{AIMIZY_PAY_URL}/api/generate", {"json": payload, "headers": common_headers}),
        ("POST", f"{AIMIZY_PAY_URL}/api/create", {"json": payload, "headers": common_headers}),
        ("POST", f"{AIMIZY_PAY_URL}/api/pay", {"json": payload, "headers": common_headers}),
        ("POST", f"{AIMIZY_PAY_URL}/api/payment-link", {"json": payload, "headers": common_headers}),
        ("GET", AIMIZY_PAY_URL, {"params": payload, "headers": {"Accept": "application/json,text/plain,*/*"}}),
        ("GET", f"{AIMIZY_PAY_URL}/api/generate", {"params": payload, "headers": {"Accept": "application/json,text/plain,*/*"}}),
    ]

    for method, url, kwargs in attempts:
        try:
            resp = cffi_requests.request(
                method,
                url,
                proxies=proxies,
                timeout=20,
                impersonate="chrome110",
                **kwargs,
            )

            # 某些服务会返回 302 + Location
            location = resp.headers.get("Location") or resp.headers.get("location")
            if isinstance(location, str) and location.startswith(("http://", "https://")):
                candidate = _normalize_checkout_link(location, proxy=proxy)
                if candidate and _is_official_checkout_link(candidate):
                    return candidate

            if resp.status_code >= 400:
                logger.debug(f"aimizy 链接生成尝试失败: {method} {url} -> HTTP {resp.status_code}")
                continue

            content_type = (resp.headers.get("content-type") or "").lower()
            if "application/json" in content_type:
                try:
                    data = resp.json()
                except Exception:
                    data = None
                link = _extract_checkout_link_from_payload(data, proxy=proxy) or _extract_link_from_payload(data)
                if link:
                    candidate = _normalize_checkout_link(link, proxy=proxy)
                    if candidate and _is_official_checkout_link(candidate):
                        return candidate

            text = (resp.text or "").strip()
            if text.startswith(("http://", "https://")):
                candidate = _normalize_checkout_link(text, proxy=proxy)
                if candidate and _is_official_checkout_link(candidate):
                    return candidate

            # 尝试从非 JSON 文本中找 URL
            match = re.search(r"https?://[^\s\"'<>]+", text)
            if match:
                candidate = _normalize_checkout_link(match.group(0), proxy=proxy)
                if candidate and _is_official_checkout_link(candidate):
                    return candidate

            # 再兜底：直接扫描 cs_xxx 并拼接 checkout 链接
            parsed_from_text = _extract_checkout_link_from_text(text)
            if parsed_from_text:
                return parsed_from_text

        except Exception as e:
            logger.debug(f"aimizy 链接生成尝试异常: {method} {url} -> {e}")

    # 最终回退：构造可直接进入站内代付页的链接
    query = urlencode(
        {
            "access_token": token,
            "accessToken": token,
            "plan_type": plan_type,
            "country": country,
            "currency": currency,
        }
    )
    fallback_url = f"{AIMIZY_PAY_URL}?{query}"
    fallback_candidate = _normalize_checkout_link(fallback_url, proxy=proxy)
    if _is_official_checkout_link(fallback_candidate):
        return fallback_candidate
    raise ValueError("未能从代付通道解析到官方 checkout 链接，请先确认账号 token 有效")


_COUNTRY_CURRENCY_MAP = {
    "SG": "SGD",
    "US": "USD",
    "TR": "TRY",
    "JP": "JPY",
    "HK": "HKD",
    "GB": "GBP",
    "EU": "EUR",
    "AU": "AUD",
    "CA": "CAD",
    "IN": "INR",
    "BR": "BRL",
    "MX": "MXN",
}


def _resolve_chatgpt_account_id(account: Account) -> Optional[str]:
    # 优先从当前 token 解析，避免数据库里旧 account_id 导致订阅误判。
    token_account_id = (
        _extract_chatgpt_account_id_from_jwt(account.access_token)
        or _extract_chatgpt_account_id_from_jwt(account.id_token)
    )
    for candidate in (token_account_id, account.account_id, account.workspace_id):
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _decode_jwt_payload(token: Optional[str]) -> Optional[Dict[str, Any]]:
    text = str(token or "").strip()
    if not text or "." not in text:
        return None
    parts = text.split(".")
    if len(parts) < 2:
        return None
    payload_part = parts[1]
    if not payload_part:
        return None
    padding = "=" * (-len(payload_part) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_part + padding)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_auth_claim(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return auth
    auth = payload.get("auth_data")
    if isinstance(auth, dict):
        return auth
    return {}


def _extract_chatgpt_account_id_from_jwt(token: Optional[str]) -> Optional[str]:
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    auth = _extract_auth_claim(payload)
    for key in (
        "chatgpt_account_id",
        "account_id",
        "workspace_id",
    ):
        value = str(auth.get(key) or payload.get(key) or "").strip()
        if value:
            return value
    return None


def _extract_chatgpt_plan_from_jwt(token: Optional[str]) -> Optional[str]:
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    auth = _extract_auth_claim(payload)
    candidates = [
        auth.get("chatgpt_plan_type"),
        auth.get("plan_type"),
        auth.get("subscription_plan"),
        payload.get("chatgpt_plan_type"),
        payload.get("plan_type"),
        payload.get("subscription_plan"),
        payload.get("subscription_tier"),
    ]
    for item in candidates:
        mapped = _map_plan_to_subscription(item if isinstance(item, str) else None)
        if mapped in ("plus", "team", "free"):
            return mapped
    return None


def _collect_plan_candidates(value: Any) -> List[str]:
    candidates: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).strip().lower()
            if key_lower in {
                "plan_type",
                "plan",
                "subscription_plan",
                "subscription_tier",
                "chatgpt_plan_type",
                "tier",
                "workspace_plan_type",
                "product",
            } and isinstance(item, str):
                text = item.strip()
                if text:
                    candidates.append(text)
            if isinstance(item, (dict, list)):
                candidates.extend(_collect_plan_candidates(item))
    elif isinstance(value, list):
        for item in value:
            candidates.extend(_collect_plan_candidates(item))
    return candidates


def _extract_oai_did(cookies_str: str) -> Optional[str]:
    """从 cookie 字符串中提取 oai-device-id"""
    for part in cookies_str.split(";"):
        part = part.strip()
        if part.startswith("oai-did="):
            return part[len("oai-did="):].strip()
    return None


def _resolve_oai_device_id(account: Account) -> str:
    if account.cookies:
        oai_did = _extract_oai_did(account.cookies)
        if oai_did:
            return oai_did
    return str(uuid.uuid4())


def _build_openai_sentinel_token(
    account: Account,
    device_id: str,
    proxy: Optional[str] = None,
) -> Optional[str]:
    """
    生成 openai-sentinel-token（失败时返回 None，不阻断主流程）。
    """
    if not device_id:
        return None

    body = json.dumps({"p": "", "id": device_id, "flow": "authorize_continue"})
    headers = {
        "Origin": "https://sentinel.openai.com",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if account.cookies:
        headers["cookie"] = account.cookies

    try:
        resp = cffi_requests.post(
            SENTINEL_REQ_URL,
            headers=headers,
            data=body,
            proxies=_build_proxies(proxy),
            timeout=20,
            impersonate="chrome110",
        )
        if resp.status_code != 200:
            logger.debug(f"sentinel token 获取失败: HTTP {resp.status_code}")
            return None
        data = resp.json() if resp.content else {}
        token = str(data.get("token") or "").strip()
        if not token:
            logger.debug("sentinel token 为空")
            return None
        return json.dumps(
            {
                "p": "",
                "t": "",
                "c": token,
                "id": device_id,
                "flow": "authorize_continue",
            }
        )
    except Exception as exc:
        logger.debug(f"sentinel token 获取异常: {exc}")
        return None


def _parse_cookie_str(cookies_str: str, domain: str) -> list:
    """将 'key=val; key2=val2' 格式解析为 Playwright cookie 列表"""
    cookies = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": domain,
            "path": "/",
        })
    return cookies


def _open_url_system_browser(url: str) -> bool:
    """回退方案：调用系统浏览器以无痕模式打开"""
    platform = sys.platform
    try:
        if platform == "win32":
            for browser, flag in [("chrome", "--incognito"), ("msedge", "--inprivate")]:
                try:
                    subprocess.Popen(f'start {browser} {flag} "{url}"', shell=True)
                    return True
                except Exception:
                    continue
        elif platform == "darwin":
            subprocess.Popen(["open", "-a", "Google Chrome", "--args", "--incognito", url])
            return True
        else:
            for binary in ["google-chrome", "chromium-browser", "chromium"]:
                try:
                    subprocess.Popen([binary, "--incognito", url])
                    return True
                except FileNotFoundError:
                    continue
    except Exception as e:
        logger.warning(f"系统浏览器无痕打开失败: {e}")
    return False


def _build_checkout_request_headers(account: Account, proxy: Optional[str]) -> Dict[str, str]:
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    device_id = _resolve_oai_device_id(account)
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "zh-CN",
        "oai-device-id": device_id,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if account.cookies:
        headers["cookie"] = account.cookies
    chatgpt_account_id = _resolve_chatgpt_account_id(account)
    if chatgpt_account_id:
        headers["ChatGPT-Account-Id"] = chatgpt_account_id

    sentinel_token = _build_openai_sentinel_token(account, device_id=device_id, proxy=proxy)
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token

    return headers


def _request_checkout_bundle(account: Account, payload: Dict[str, Any], proxy: Optional[str] = None) -> Dict[str, Optional[str]]:
    headers = _build_checkout_request_headers(account, proxy=proxy)
    proxy_candidates: List[Optional[str]] = [proxy]
    if proxy:
        proxy_candidates.append(None)

    last_err: Optional[Exception] = None
    for idx, candidate_proxy in enumerate(proxy_candidates):
        try:
            resp = cffi_requests.post(
                PAYMENT_CHECKOUT_URL,
                headers=headers,
                json=payload,
                proxies=_build_proxies(candidate_proxy),
                timeout=30,
                impersonate="chrome110",
            )
            resp.raise_for_status()
            data = resp.json()
            bundle = _build_checkout_bundle_from_payload(data, proxy=candidate_proxy)
            if bundle.get("checkout_url"):
                if idx > 0:
                    logger.warning(
                        "官方 checkout 代理请求失败后直连成功: account_id=%s email=%s",
                        account.id,
                        account.email,
                    )
                return bundle
            raise ValueError(data.get("detail", "API 未返回可用 checkout 链接"))
        except Exception as exc:
            last_err = exc
            should_retry_direct = (
                idx == 0
                and proxy is not None
                and candidate_proxy is not None
                and _is_connectivity_error(exc)
            )
            if should_retry_direct:
                logger.warning(
                    "官方 checkout 代理请求网络异常，改为直连重试: account_id=%s email=%s error=%s",
                    account.id,
                    account.email,
                    exc,
                )
                continue
            raise

    if last_err:
        raise last_err
    raise RuntimeError("官方 checkout 请求失败")


def generate_plus_checkout_bundle(
    account: Account,
    proxy: Optional[str] = None,
    country: str = "US",
) -> Dict[str, Optional[str]]:
    """生成 Plus checkout 全量信息（含 checkout_session_id/publishable_key）。"""
    currency = _COUNTRY_CURRENCY_MAP.get(country, "USD")
    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "custom",
    }
    return _request_checkout_bundle(account=account, payload=payload, proxy=proxy)


def generate_team_checkout_bundle(
    account: Account,
    workspace_name: str = "MyTeam",
    price_interval: str = "month",
    seat_quantity: int = 5,
    proxy: Optional[str] = None,
    country: str = "US",
) -> Dict[str, Optional[str]]:
    """生成 Team checkout 全量信息（含 checkout_session_id/publishable_key）。"""
    currency = _COUNTRY_CURRENCY_MAP.get(country, "USD")
    payload = {
        "plan_name": "chatgptteamplan",
        "team_plan_data": {
            "workspace_name": workspace_name,
            "price_interval": price_interval,
            "seat_quantity": seat_quantity,
        },
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/?promo_campaign=team-1-month-free#team-pricing",
        "promo_campaign": {
            "promo_campaign_id": "team-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "custom",
    }
    return _request_checkout_bundle(account=account, payload=payload, proxy=proxy)


def generate_plus_link(
    account: Account,
    proxy: Optional[str] = None,
    country: str = "US",
) -> str:
    bundle = generate_plus_checkout_bundle(account=account, proxy=proxy, country=country)
    return str(bundle.get("checkout_url") or "")


def generate_team_link(
    account: Account,
    workspace_name: str = "MyTeam",
    price_interval: str = "month",
    seat_quantity: int = 5,
    proxy: Optional[str] = None,
    country: str = "US",
) -> str:
    bundle = generate_team_checkout_bundle(
        account=account,
        workspace_name=workspace_name,
        price_interval=price_interval,
        seat_quantity=seat_quantity,
        proxy=proxy,
        country=country,
    )
    return str(bundle.get("checkout_url") or "")


def open_url_incognito(url: str, cookies_str: Optional[str] = None) -> bool:
    """用 Playwright 以无痕模式打开 URL，可注入 cookie"""
    import threading
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright 未安装，回退到系统浏览器")
        return _open_url_system_browser(url)

    def _launch():
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, args=["--incognito"])
                ctx = browser.new_context()
                if cookies_str:
                    ctx.add_cookies(_parse_cookie_str(cookies_str, "chatgpt.com"))
                page = ctx.new_page()
                page.goto(url)
                # 保持窗口打开直到用户关闭
                page.wait_for_timeout(300_000)  # 最多等待 5 分钟
        except Exception as e:
            logger.warning(f"Playwright 无痕打开失败: {e}")

    threading.Thread(target=_launch, daemon=True).start()
    return True


def _map_plan_to_subscription(plan: Optional[str]) -> Optional[str]:
    text = (plan or "").strip().lower()
    if not text:
        return None
    if "team" in text or "enterprise" in text:
        return "team"
    if "plus" in text or "pro" in text:
        return "plus"
    if "free" in text or "basic" in text:
        return "free"
    return None


def check_subscription_status_detail(account: Account, proxy: Optional[str] = None) -> Dict[str, Any]:
    """
    检测账号当前订阅状态。
    返回带来源与置信度的详细结果，便于绑卡任务诊断。
    """
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
    }
    chatgpt_account_id = _resolve_chatgpt_account_id(account)
    if chatgpt_account_id:
        headers["ChatGPT-Account-Id"] = chatgpt_account_id
    logger.info(
        "订阅检测上下文: email=%s chatgpt_account_id=%s proxy=%s",
        account.email,
        chatgpt_account_id or "-",
        proxy or "-",
    )
    if account.cookies:
        headers["cookie"] = account.cookies
        oai_did = _extract_oai_did(account.cookies)
        if oai_did:
            headers["oai-device-id"] = oai_did

    successful_sources: List[str] = []
    errors: List[str] = []
    weak_free_source: Optional[str] = None
    explicit_free_source: Optional[str] = None
    explicit_free_value: Optional[str] = None

    def _result(status: str, source: str, confidence: str, note: Optional[str] = None) -> Dict[str, Any]:
        payload = {
            "status": status,
            "source": source,
            "confidence": confidence,
            "errors": errors[:4],
            "successful_sources": successful_sources[:4],
        }
        if note:
            payload["note"] = note
        return payload

    def _analyze_me_payload(data: Any, source_prefix: str) -> Optional[Dict[str, Any]]:
        nonlocal weak_free_source, explicit_free_source, explicit_free_value
        if not isinstance(data, dict):
            return None

        candidates = [
            data.get("plan_type"),
            data.get("plan"),
            data.get("subscription_plan"),
            data.get("subscription_tier"),
            data.get("chatgpt_plan_type"),
        ]
        account_block = data.get("account")
        if isinstance(account_block, dict):
            candidates.extend([
                account_block.get("plan_type"),
                account_block.get("plan"),
                account_block.get("subscription_plan"),
                account_block.get("subscription_tier"),
                account_block.get("chatgpt_plan_type"),
            ])
        subscription_block = data.get("subscription")
        if isinstance(subscription_block, dict):
            candidates.extend([
                subscription_block.get("plan_type"),
                subscription_block.get("plan"),
                subscription_block.get("product"),
                subscription_block.get("tier"),
            ])

        for item in candidates:
            mapped = _map_plan_to_subscription(item)
            if mapped in ("plus", "team"):
                return _result(mapped, f"{source_prefix}.plan", "high")
            if mapped == "free" and not explicit_free_source:
                explicit_free_source = f"{source_prefix}.plan"
                explicit_free_value = str(item)

        orgs = data.get("orgs", {}).get("data", [])
        if isinstance(orgs, list):
            for org in orgs:
                if not isinstance(org, dict):
                    continue
                settings_ = org.get("settings", {})
                if isinstance(settings_, dict):
                    workspace_plan = str(settings_.get("workspace_plan_type") or "").lower()
                    if workspace_plan in ("team", "enterprise"):
                        return _result("team", f"{source_prefix}.org.workspace_plan_type", "high")
                mapped = _map_plan_to_subscription(org.get("plan_type") or org.get("plan"))
                if mapped in ("plus", "team"):
                    return _result(mapped, f"{source_prefix}.org.plan", "high")

        bool_markers = (
            data.get("has_paid_subscription"),
            data.get("has_active_subscription"),
            data.get("is_paid"),
            data.get("is_subscribed"),
        )
        if any(v is True for v in bool_markers):
            return _result("plus", f"{source_prefix}.subscription_flag", "medium")
        if all(v is False for v in bool_markers if v is not None):
            weak_free_source = weak_free_source or f"{source_prefix}.subscription_flag_false"
            explicit_free_value = explicit_free_value or "all_false"
        return None

    def _analyze_usage_payload(data: Any, source_prefix: str) -> Optional[Dict[str, Any]]:
        nonlocal weak_free_source, explicit_free_value
        if not isinstance(data, dict):
            return None
        usage_candidates = [
            data.get("plan_type"),
            data.get("plan"),
            data.get("subscription_plan"),
            data.get("subscription_tier"),
            data.get("chatgpt_plan_type"),
            data.get("tier"),
        ]
        for item in usage_candidates:
            mapped = _map_plan_to_subscription(item)
            if mapped in ("plus", "team"):
                return _result(mapped, f"{source_prefix}.plan", "high")
            if mapped == "free":
                weak_free_source = weak_free_source or f"{source_prefix}.plan"
                explicit_free_value = explicit_free_value or str(item)

        rate_limit = data.get("rate_limit")
        code_review_limit = data.get("code_review_rate_limit")
        if isinstance(rate_limit, dict) or isinstance(code_review_limit, dict):
            weak_free_source = weak_free_source or f"{source_prefix}.rate_limit_only"
        return None

    # 1) me 接口
    try:
        resp = cffi_requests.get(
            "https://chatgpt.com/backend-api/me",
            headers=headers,
            proxies=_build_proxies(proxy),
            timeout=20,
            impersonate="chrome110",
        )
        resp.raise_for_status()
        successful_sources.append("me")
        data = resp.json() if resp.content else {}
        detected = _analyze_me_payload(data, "me")
        if detected:
            return detected
    except Exception as exc:
        errors.append(f"me: {exc}")

    # 1.5) 去掉 ChatGPT-Account-Id 再测一次 me（避免错误 workspace 作用域导致误判 free）
    if chatgpt_account_id:
        try:
            headers_no_scope = dict(headers)
            headers_no_scope.pop("ChatGPT-Account-Id", None)
            resp_no_scope = cffi_requests.get(
                "https://chatgpt.com/backend-api/me",
                headers=headers_no_scope,
                proxies=_build_proxies(proxy),
                timeout=20,
                impersonate="chrome110",
            )
            resp_no_scope.raise_for_status()
            successful_sources.append("me_no_scope")
            data_no_scope = resp_no_scope.json() if resp_no_scope.content else {}
            detected = _analyze_me_payload(data_no_scope, "me.no_scope")
            if detected:
                logger.info(
                    "订阅检测无作用域复核命中: email=%s source=%s confidence=%s",
                    account.email,
                    detected.get("source"),
                    detected.get("confidence"),
                )
                return detected
        except Exception as exc:
            errors.append(f"me_no_scope: {exc}")

    # 2) wham/usage（Cockpit-tools 同款核心）
    try:
        usage_resp = cffi_requests.get(
            "https://chatgpt.com/backend-api/wham/usage",
            headers=headers,
            proxies=_build_proxies(proxy),
            timeout=20,
            impersonate="chrome110",
        )
        usage_resp.raise_for_status()
        successful_sources.append("wham_usage")
        usage_data = usage_resp.json() if usage_resp.content else {}
        detected = _analyze_usage_payload(usage_data, "wham_usage")
        if detected:
            return detected
    except Exception as exc:
        errors.append(f"wham_usage: {exc}")

    # 3) wham/accounts/check（Cockpit-tools 用于账号资料同步的官方口径）
    try:
        account_check_resp = cffi_requests.get(
            ACCOUNT_CHECK_URL,
            headers=headers,
            proxies=_build_proxies(proxy),
            timeout=20,
            impersonate="chrome110",
        )
        account_check_resp.raise_for_status()
        successful_sources.append("wham_accounts_check")
        account_check_data = account_check_resp.json() if account_check_resp.content else {}
        for raw in _collect_plan_candidates(account_check_data):
            mapped = _map_plan_to_subscription(raw)
            if mapped in ("plus", "team"):
                return _result(mapped, "wham_accounts_check.plan", "medium")
            if mapped == "free":
                weak_free_source = weak_free_source or "wham_accounts_check.plan"
                explicit_free_value = explicit_free_value or str(raw)
    except Exception as exc:
        errors.append(f"wham_accounts_check: {exc}")

    # 3.5) 作用域复核：前面仍未命中付费时，去掉 ChatGPT-Account-Id 再测 usage + accounts/check
    if chatgpt_account_id:
        headers_no_scope = dict(headers)
        headers_no_scope.pop("ChatGPT-Account-Id", None)
        try:
            usage_no_scope_resp = cffi_requests.get(
                "https://chatgpt.com/backend-api/wham/usage",
                headers=headers_no_scope,
                proxies=_build_proxies(proxy),
                timeout=20,
                impersonate="chrome110",
            )
            usage_no_scope_resp.raise_for_status()
            successful_sources.append("wham_usage_no_scope")
            usage_no_scope_data = usage_no_scope_resp.json() if usage_no_scope_resp.content else {}
            detected = _analyze_usage_payload(usage_no_scope_data, "wham_usage.no_scope")
            if detected:
                logger.info(
                    "订阅检测 usage 无作用域复核命中: email=%s source=%s confidence=%s",
                    account.email,
                    detected.get("source"),
                    detected.get("confidence"),
                )
                return detected
        except Exception as exc:
            errors.append(f"wham_usage_no_scope: {exc}")

        try:
            account_check_no_scope_resp = cffi_requests.get(
                ACCOUNT_CHECK_URL,
                headers=headers_no_scope,
                proxies=_build_proxies(proxy),
                timeout=20,
                impersonate="chrome110",
            )
            account_check_no_scope_resp.raise_for_status()
            successful_sources.append("wham_accounts_check_no_scope")
            account_check_no_scope_data = account_check_no_scope_resp.json() if account_check_no_scope_resp.content else {}
            for raw in _collect_plan_candidates(account_check_no_scope_data):
                mapped = _map_plan_to_subscription(raw)
                if mapped in ("plus", "team"):
                    return _result(mapped, "wham_accounts_check.no_scope.plan", "medium")
                if mapped == "free":
                    weak_free_source = weak_free_source or "wham_accounts_check.no_scope.plan"
                    explicit_free_value = explicit_free_value or str(raw)
        except Exception as exc:
            errors.append(f"wham_accounts_check_no_scope: {exc}")

    # 4) 概览接口兜底（跨 endpoint 聚合）
    try:
        overview = fetch_codex_overview(account, proxy=proxy)
        successful_sources.append("overview")
        mapped = _map_plan_to_subscription(overview.get("plan_type"))
        if mapped in ("plus", "team"):
            return _result(mapped, f"overview.{overview.get('plan_source') or 'plan'}", "medium")
        if mapped == "free":
            weak_free_source = weak_free_source or f"overview.{overview.get('plan_source') or 'plan'}"
            for err in overview.get("errors") or []:
                if err:
                    errors.append(f"overview: {err}")
    except Exception as exc:
        errors.append(f"overview: {exc}")

    # 5) JWT claim 兜底（不依赖刷新 token）
    jwt_candidates = (
        ("id_token.chatgpt_plan_type", _extract_chatgpt_plan_from_jwt(account.id_token)),
        ("access_token.chatgpt_plan_type", _extract_chatgpt_plan_from_jwt(account.access_token)),
    )
    for source_name, mapped in jwt_candidates:
        if mapped in ("plus", "team"):
            return _result(mapped, source_name, "medium", note="jwt_claim")
        if mapped == "free":
            weak_free_source = weak_free_source or source_name
            explicit_free_value = explicit_free_value or "jwt_free"

    # 6) 明确 free 信号
    if explicit_free_source:
        return _result(
            "free",
            explicit_free_source,
            "high",
            note=f"explicit_free={explicit_free_value or '-'}",
        )

    # 7) 检测信号弱时，优先使用数据库缓存的已订阅状态避免误判为 free
    cached = _map_plan_to_subscription(account.subscription_type)
    if cached in ("plus", "team"):
        return _result(
            cached,
            "db.subscription_type",
            "low",
            note=f"api_ambiguous={weak_free_source or 'no_plan_signal'}",
        )

    # 8) 若所有检测接口都失败，抛错而不是误判 free
    if not successful_sources:
        raise RuntimeError("订阅检测接口全部失败: " + " | ".join(errors[:3]))

    # 9) 仍无有效订阅信号，返回低置信度 free
    return _result(
        "free",
        weak_free_source or "fallback.default_free",
        "low",
        note="no_paid_signal",
    )


def check_subscription_status(account: Account, proxy: Optional[str] = None) -> str:
    """
    兼容旧调用：仅返回 'free' / 'plus' / 'team'。
    """
    detail = check_subscription_status_detail(account, proxy=proxy)
    status = str(detail.get("status") or "free").lower()
    if status not in ("free", "plus", "team"):
        return "free"
    return status
