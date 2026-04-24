"""
通用工具函数模块
"""

from dataclasses import dataclass, field
import random
import string
import secrets
import hashlib
import base64
import uuid
import re
from urllib.parse import urlparse
from typing import Any, Dict


@dataclass
class FlowState:
    """OpenAI Auth/Registration 流程中的页面状态。"""

    page_type: str = ""
    continue_url: str = ""
    method: str = "GET"
    current_url: str = ""
    source: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


def generate_device_id():
    """生成设备唯一标识（oai-did），UUID v4 格式"""
    return str(uuid.uuid4())


def generate_random_password(length=16):
    """生成符合 OpenAI 要求的随机密码"""
    chars = string.ascii_letters + string.digits + "!@#$%"
    pwd = list(
        random.choice(string.ascii_uppercase)
        + random.choice(string.ascii_lowercase)
        + random.choice(string.digits)
        + random.choice("!@#$%")
        + "".join(random.choice(chars) for _ in range(length - 4))
    )
    random.shuffle(pwd)
    return "".join(pwd)


def generate_random_name():
    """随机生成自然的英文姓名，返回 (first_name, last_name)"""
    first = [
        "James", "Robert", "John", "Michael", "David", "William", "Richard",
        "Mary", "Jennifer", "Linda", "Elizabeth", "Susan", "Jessica", "Sarah",
        "Emily", "Emma", "Olivia", "Sophia", "Liam", "Noah", "Oliver", "Ethan",
    ]
    last = [
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
        "Davis", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Martin",
    ]
    return random.choice(first), random.choice(last)


def generate_random_birthday():
    """生成随机生日字符串，格式 YYYY-MM-DD（20~30岁）"""
    year = random.randint(1996, 2006)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def generate_datadog_trace():
    """生成 Datadog APM 追踪头"""
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def generate_pkce():
    """生成 PKCE code_verifier 和 code_challenge"""
    code_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    )
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def decode_jwt_payload(token):
    """解析 JWT token 的 payload 部分"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        import json
        return json.loads(decoded)
    except Exception:
        return {}


def extract_code_from_url(url):
    """从 URL 中提取 authorization code"""
    if not url or "code=" not in url:
        return None
    try:
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(url).query).get("code", [None])[0]
    except Exception:
        return None


def normalize_page_type(value):
    """将 page.type 归一化为便于分支判断的 snake_case。"""
    return str(value or "").strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")


def normalize_flow_url(url, auth_base="https://auth.openai.com"):
    """将 continue_url / payload.url 归一化成绝对 URL。"""
    value = str(url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("/"):
        return f"{auth_base.rstrip('/')}{value}"
    return value


def infer_page_type_from_url(url):
    """从 URL 推断流程状态，用于服务端未返回 page.type 时兜底。"""
    if not url:
        return ""

    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()

    if "code=" in (parsed.query or ""):
        return "oauth_callback"
    if "chatgpt.com" in host and "/api/auth/callback/" in path:
        return "callback"
    if "create-account/password" in path:
        return "create_account_password"
    if "email-verification" in path or "email-otp" in path:
        return "email_otp_verification"
    if "about-you" in path:
        return "about_you"
    if "log-in/password" in path:
        return "login_password"
    if "sign-in-with-chatgpt" in path and "consent" in path:
        return "consent"
    if "workspace" in path and "select" in path:
        return "workspace_selection"
    if "organization" in path and "select" in path:
        return "organization_selection"
    if "add-phone" in path:
        return "add_phone"
    if "callback" in path:
        return "callback"
    if "chatgpt.com" in host and path in {"", "/"}:
        return "chatgpt_home"
    if path:
        return normalize_page_type(path.strip("/").replace("/", "_"))
    return ""


def extract_flow_state(data=None, current_url="", auth_base="https://auth.openai.com", default_method="GET"):
    """从 API 响应或 URL 中提取统一的流程状态。"""
    raw = data if isinstance(data, dict) else {}
    page = raw.get("page") or {}
    payload = page.get("payload") or {}

    continue_url = normalize_flow_url(
        raw.get("continue_url") or payload.get("url") or "",
        auth_base=auth_base,
    )
    effective_current_url = continue_url if raw and continue_url else current_url
    current = normalize_flow_url(effective_current_url or continue_url, auth_base=auth_base)
    page_type = normalize_page_type(page.get("type")) or infer_page_type_from_url(continue_url or current)
    method = str(raw.get("method") or payload.get("method") or default_method or "GET").upper()

    return FlowState(
        page_type=page_type,
        continue_url=continue_url,
        method=method,
        current_url=current,
        source="api" if raw else "url",
        payload=payload if isinstance(payload, dict) else {},
        raw=raw,
    )


def describe_flow_state(state: FlowState):
    """生成简短的流程状态描述，便于记录日志。"""
    target = state.continue_url or state.current_url or "-"
    return f"page={state.page_type or '-'} method={state.method or '-'} next={target[:80]}..."


def random_delay(low=0.3, high=1.0):
    """随机延迟"""
    import time
    time.sleep(random.uniform(low, high))


def extract_chrome_full_version(user_agent):
    """从 UA 中提取完整的 Chrome 版本号。"""
    if not user_agent:
        return ""
    match = re.search(r"Chrome/([0-9.]+)", user_agent)
    return match.group(1) if match else ""


def _registrable_domain(hostname):
    """粗略提取可注册域名，用于推断 Sec-Fetch-Site。"""
    if not hostname:
        return ""
    host = hostname.split(":")[0].strip(".").lower()
    parts = [part for part in host.split(".") if part]
    if len(parts) <= 2:
        return ".".join(parts)
    return ".".join(parts[-2:])


def infer_sec_fetch_site(url, referer=None, navigation=False):
    """根据目标 URL 和 Referer 推断 Sec-Fetch-Site。"""
    if not referer:
        return "none" if navigation else "same-origin"

    try:
        target = urlparse(url or "")
        source = urlparse(referer or "")

        if not target.scheme or not target.netloc or not source.netloc:
            return "none" if navigation else "same-origin"

        if (target.scheme, target.netloc) == (source.scheme, source.netloc):
            return "same-origin"

        if _registrable_domain(target.hostname) == _registrable_domain(source.hostname):
            return "same-site"
    except Exception:
        pass

    return "cross-site"


def build_sec_ch_ua_full_version_list(sec_ch_ua, chrome_full_version):
    """根据 sec-ch-ua 生成 sec-ch-ua-full-version-list。"""
    if not sec_ch_ua or not chrome_full_version:
        return ""

    entries = []
    for brand, version in re.findall(r'"([^"]+)";v="([^"]+)"', sec_ch_ua):
        full_version = chrome_full_version if brand in {"Chromium", "Google Chrome"} else f"{version}.0.0.0"
        entries.append(f'"{brand}";v="{full_version}"')

    return ", ".join(entries)


def build_browser_headers(
    *,
    url,
    user_agent,
    sec_ch_ua=None,
    chrome_full_version=None,
    accept=None,
    accept_language="en-US,en;q=0.9",
    referer=None,
    origin=None,
    content_type=None,
    navigation=False,
    fetch_mode=None,
    fetch_dest=None,
    fetch_site=None,
    headed=False,
    extra_headers=None,
):
    """构造更接近真实 Chrome 有头浏览器的请求头。"""
    chrome_full = chrome_full_version or extract_chrome_full_version(user_agent)
    full_version_list = build_sec_ch_ua_full_version_list(sec_ch_ua, chrome_full)

    headers = {
        "User-Agent": user_agent or "Mozilla/5.0",
        "Accept-Language": accept_language,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
    }

    if accept:
        headers["Accept"] = accept
    if referer:
        headers["Referer"] = referer
    if origin:
        headers["Origin"] = origin
    if content_type:
        headers["Content-Type"] = content_type
    if sec_ch_ua:
        headers["sec-ch-ua"] = sec_ch_ua
    if chrome_full:
        headers["sec-ch-ua-full-version"] = f'"{chrome_full}"'
        headers["sec-ch-ua-platform-version"] = '"15.0.0"'
    if full_version_list:
        headers["sec-ch-ua-full-version-list"] = full_version_list

    if navigation:
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-User"] = "?1"
        headers["Upgrade-Insecure-Requests"] = "1"
        headers["Cache-Control"] = "max-age=0"
    else:
        headers["Sec-Fetch-Dest"] = fetch_dest or "empty"
        headers["Sec-Fetch-Mode"] = fetch_mode or "cors"

    headers["Sec-Fetch-Site"] = fetch_site or infer_sec_fetch_site(url, referer, navigation=navigation)

    if headed:
        headers.setdefault("Priority", "u=0, i" if navigation else "u=1, i")
        headers.setdefault("DNT", "1")
        headers.setdefault("Sec-GPC", "1")

    if extra_headers:
        for key, value in extra_headers.items():
            if value is not None:
                headers[key] = value

    return headers


def seed_oai_device_cookie(session, device_id):
    """在 ChatGPT / OpenAI 相关域上同步设置 oai-did。"""
    for domain in (
        "chatgpt.com",
        ".chatgpt.com",
        "openai.com",
        ".openai.com",
        "auth.openai.com",
        ".auth.openai.com",
    ):
        try:
            session.cookies.set("oai-did", device_id, domain=domain)
        except Exception:
            continue
