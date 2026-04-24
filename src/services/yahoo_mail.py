"""
Yahoo Mail service.

Modes:
- fixed-inbox mode: reuse one configured Yahoo inbox directly;
- parent-alias mode: use one Yahoo parent inbox to create a Yahoo Mail Plus
  disposable alias, then read OpenAI verification codes from the parent inbox
  by filtering messages for that alias address.
"""

from __future__ import annotations

import email
import imaplib
import logging
import random
import re
import socket
import string
import time
import sys
import os
import json
import urllib.request
import urllib.error
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import (
    EMAIL_SERVICE_DEFAULTS,
    OPENAI_EMAIL_SENDERS,
    OTP_CODE_PATTERN,
    OTP_CODE_SEMANTIC_PATTERN,
)
from ..core.anyauto.utils import generate_random_birthday, generate_random_name


logger = logging.getLogger(__name__)
PLAYWRIGHT_EXTRA_LD_LIBRARY_PATHS = [
    Path("/tmp/pw-deps/extract/usr/lib/x86_64-linux-gnu"),
]
YAHOO_STEALTH_INIT_SCRIPT = r"""
(() => {
  const forcedTimezone = 'America/New_York';
  const forcedTimezoneOffset = 240;
  try {
    delete window.__playwright__binding__;
    delete window.__pwInitScripts;
  } catch (e) {}
  const overrideGetter = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, { get: () => value, configurable: true });
    } catch (e) {}
  };
  try {
    delete Navigator.prototype.webdriver;
  } catch (e) {}
  overrideGetter(Navigator.prototype, 'webdriver', false);
  overrideGetter(navigator, 'webdriver', false);
  overrideGetter(Navigator.prototype, 'platform', 'Win32');
  overrideGetter(Navigator.prototype, 'language', 'en-US');
  overrideGetter(Navigator.prototype, 'languages', ['en-US', 'en']);
  overrideGetter(Navigator.prototype, 'vendor', 'Google Inc.');
  overrideGetter(Navigator.prototype, 'hardwareConcurrency', 8);
  overrideGetter(Navigator.prototype, 'deviceMemory', 8);
  overrideGetter(Navigator.prototype, 'pdfViewerEnabled', true);
  const fakePlugins = [
    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
  ];
  const fakeMimeTypes = [
    { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
    { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
  ];
  overrideGetter(Navigator.prototype, 'plugins', fakePlugins);
  overrideGetter(Navigator.prototype, 'mimeTypes', fakeMimeTypes);
  const fakeUAData = {
    brands: [
      { brand: 'Chromium', version: '145' },
      { brand: 'Google Chrome', version: '145' },
      { brand: 'Not.A/Brand', version: '99' },
    ],
    mobile: false,
    platform: 'Windows',
    getHighEntropyValues: async (hints) => ({
      architecture: 'x86',
      bitness: '64',
      brands: [
        { brand: 'Chromium', version: '145' },
        { brand: 'Google Chrome', version: '145' },
        { brand: 'Not.A/Brand', version: '99' },
      ],
      mobile: false,
      model: '',
      platform: 'Windows',
      platformVersion: '10.0.0',
      uaFullVersion: '145.0.0.0',
      wow64: false,
      fullVersionList: [
        { brand: 'Chromium', version: '145.0.0.0' },
        { brand: 'Google Chrome', version: '145.0.0.0' },
        { brand: 'Not.A/Brand', version: '99.0.0.0' },
      ],
    }),
    toJSON: () => ({
      brands: [
        { brand: 'Chromium', version: '145' },
        { brand: 'Google Chrome', version: '145' },
        { brand: 'Not.A/Brand', version: '99' },
      ],
      mobile: false,
      platform: 'Windows',
    }),
  };
  overrideGetter(Navigator.prototype, 'userAgentData', fakeUAData);
  if (!window.chrome) {
    window.chrome = { runtime: {} };
  } else if (!window.chrome.runtime) {
    window.chrome.runtime = {};
  }
  if (!window.chrome.app) {
    window.chrome.app = {
      InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
      RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      isInstalled: false,
      getDetails: () => null,
      getIsInstalled: () => false,
      runningState: () => 'cannot_run',
    };
  }
  const originalQuery = navigator.permissions && navigator.permissions.query;
  if (originalQuery) {
    navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
    );
  }
  const originalGetParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return originalGetParameter.call(this, parameter);
  };
  if (window.WebGL2RenderingContext) {
    const originalGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(parameter) {
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return originalGetParameter2.call(this, parameter);
    };
  }
  try {
    Object.defineProperty(screen, 'colorDepth', { get: () => 24, configurable: true });
    Object.defineProperty(screen, 'pixelDepth', { get: () => 24, configurable: true });
    Object.defineProperty(screen, 'width', { get: () => 1366, configurable: true });
    Object.defineProperty(screen, 'height', { get: () => 768, configurable: true });
    Object.defineProperty(screen, 'availWidth', { get: () => 1366, configurable: true });
    Object.defineProperty(screen, 'availHeight', { get: () => 728, configurable: true });
  } catch (e) {}
  try {
    Object.defineProperty(window, 'outerWidth', { get: () => 1366, configurable: true });
    Object.defineProperty(window, 'outerHeight', { get: () => 900, configurable: true });
  } catch (e) {}
  try {
    const originalResolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
    Intl.DateTimeFormat.prototype.resolvedOptions = function(...args) {
      const result = originalResolvedOptions.apply(this, args);
      return { ...result, timeZone: forcedTimezone };
    };
  } catch (e) {}
  try {
    const originalGetTimezoneOffset = Date.prototype.getTimezoneOffset;
    Date.prototype.getTimezoneOffset = function() {
      return forcedTimezoneOffset;
    };
  } catch (e) {}
})();
"""


def _classify_yahoo_network_error(exc: Exception, proxy_url: Optional[str] = None) -> Optional[str]:
    text = str(exc or "").strip()
    lower = text.lower()
    if not lower:
        return None
    masked_proxy = str(proxy_url or "").strip()
    if masked_proxy and "@" in masked_proxy:
        try:
            scheme, rest = masked_proxy.split("://", 1)
            creds, host = rest.rsplit("@", 1)
            username = creds.split(":", 1)[0] if ":" in creds else "***"
            masked_proxy = f"{scheme}://{username}:***@{host}"
        except Exception:
            masked_proxy = "***"
    if "err_empty_response" in lower or "proxy connect aborted" in lower:
        return f"Yahoo 登录页网络访问失败，当前代理无法建立稳定连接: {masked_proxy or '-'} | {text}"
    if "err_connection_closed" in lower or "connection reset" in lower:
        return f"Yahoo 登录页连接被对端主动关闭，疑似当前出口被 Yahoo/上游网络拦截: {masked_proxy or '-'} | {text}"
    if "timeout" in lower and "yahoo" in lower:
        return f"Yahoo 登录页访问超时，疑似当前代理/网络不可达: {masked_proxy or '-'} | {text}"
    return None


def _ensure_playwright_stdio_safe() -> None:
    """Windows 某些受限终端/沙箱下，Playwright 的 asyncio 子进程管道会触发 WinError 5。
    在检测到这种环境时，提前把 stdio 切到 DEVNULL，避免 CreatePipe / CreateFile 失败。
    这里必须保持 stdout/stderr 为文本句柄，否则后续异常输出会触发 `a bytes-like object is required, not 'str'`。
    """
    if os.name != "nt":
        return
    try:
        stdin_handle = open(os.devnull, "r", encoding="utf-8", errors="ignore")
        stdout_handle = open(os.devnull, "w", encoding="utf-8", errors="ignore")
        stderr_handle = open(os.devnull, "w", encoding="utf-8", errors="ignore")
    except Exception:
        return

    replacements = {
        "stdin": stdin_handle,
        "stdout": stdout_handle,
        "stderr": stderr_handle,
        "__stdin__": stdin_handle,
        "__stdout__": stdout_handle,
        "__stderr__": stderr_handle,
    }
    for attr, handle in replacements.items():
        try:
            setattr(sys, attr, handle)
        except Exception:
            continue


def _ensure_playwright_runtime_libs() -> None:
    if os.name != "posix":
        return
    current = [item for item in str(os.environ.get("LD_LIBRARY_PATH") or "").split(":") if item]
    changed = False
    for candidate in PLAYWRIGHT_EXTRA_LD_LIBRARY_PATHS:
        candidate_str = str(candidate)
        if candidate.exists() and candidate_str not in current:
            current.insert(0, candidate_str)
            changed = True
    if changed:
        os.environ["LD_LIBRARY_PATH"] = ":".join(current)


class YahooMailService(BaseEmailService):
    """Yahoo inbox provider with browser-backed signup/login support."""

    IMAP_HOST = "imap.mail.yahoo.com"
    IMAP_PORT = 993
    LOGIN_URL = "https://login.yahoo.com/"
    SIGNUP_URL = "https://login.yahoo.com/account/create"
    MAILBOX_URL = "https://mail.yahoo.com/n/folders/1?.src=ym&reason=novation"
    DEFAULT_BROWSER_LOCALE = "en-US"
    DEFAULT_BROWSER_TIMEZONE = "America/New_York"
    DEFAULT_BROWSER_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.YAHOO_MAIL, name)

        defaults = dict(EMAIL_SERVICE_DEFAULTS.get("yahoo_mail") or {})
        merged = {**defaults, **(config or {})}
        merged["parent_email"] = str(merged.get("parent_email") or "").strip().lower()
        merged["parent_password"] = str(merged.get("parent_password") or "").strip()
        merged["parent_app_password"] = str(merged.get("parent_app_password") or "").strip()
        merged["roxy_ws_endpoint"] = str(merged.get("roxy_ws_endpoint") or "").strip()
        merged["prefer_roxy_otp"] = self._to_bool(merged.get("prefer_roxy_otp"), default=True)
        merged["email"] = str(merged.get("email") or "").strip().lower()
        merged["password"] = str(merged.get("password") or "").strip()
        merged["app_password"] = str(merged.get("app_password") or "").strip()
        merged["recovery_email"] = str(merged.get("recovery_email") or "").strip()
        merged["phone_number"] = str(merged.get("phone_number") or "").strip()
        merged["first_name"] = str(merged.get("first_name") or "").strip()
        merged["last_name"] = str(merged.get("last_name") or "").strip()
        merged["username_prefix"] = str(merged.get("username_prefix") or "").strip().lower()
        merged["alias_prefix"] = str(merged.get("alias_prefix") or merged.get("username_prefix") or "monster").strip().lower() or "monster"
        merged["alias_random_length"] = max(1, int(merged.get("alias_random_length") or 4))
        merged["alias_start_counter"] = max(0, int(merged.get("alias_start_counter") or 1))
        merged["domain"] = str(merged.get("domain") or "yahoo.com").strip().lower() or "yahoo.com"
        merged["locale"] = str(
            merged.get("locale") or self.DEFAULT_BROWSER_LOCALE
        ).strip() or self.DEFAULT_BROWSER_LOCALE
        merged["timezone_id"] = str(
            merged.get("timezone_id") or self.DEFAULT_BROWSER_TIMEZONE
        ).strip() or self.DEFAULT_BROWSER_TIMEZONE
        merged["user_agent"] = str(
            merged.get("user_agent") or self.DEFAULT_BROWSER_USER_AGENT
        ).strip() or self.DEFAULT_BROWSER_USER_AGENT
        merged["headless"] = self._to_bool(merged.get("headless"), default=True)
        merged["timeout"] = max(10, int(merged.get("timeout") or 30))
        merged["poll_interval"] = max(2, int(merged.get("poll_interval") or 5))
        merged["max_retries"] = max(1, int(merged.get("max_retries") or 3))
        merged["imap_socket_timeout"] = max(5, int(merged.get("imap_socket_timeout") or 12))
        merged["otp_fetch_timeout"] = max(60, int(merged.get("otp_fetch_timeout") or 210))
        merged["otp_max_attempts"] = max(1, int(merged.get("otp_max_attempts") or 5))
        merged["otp_retry_delay"] = max(1, int(merged.get("otp_retry_delay") or 4))
        merged["roxy_mailbox_dump_on_fail"] = self._to_bool(merged.get("roxy_mailbox_dump_on_fail"), default=True)
        merged["birth_month"] = self._parse_optional_int(merged.get("birth_month"), 1, 12)
        merged["birth_day"] = self._parse_optional_int(merged.get("birth_day"), 1, 31)
        merged["birth_year"] = self._parse_optional_int(merged.get("birth_year"), 1960, 2008)
        merged["proxy_url"] = str(merged.get("proxy_url") or "").strip() or None
        self.config = merged

        self._accounts_by_email: Dict[str, Dict[str, Any]] = {}
        self._accounts_by_id: Dict[str, Dict[str, Any]] = {}
        self._used_codes: Dict[str, set[str]] = {}
        self._used_code_stage_marker: Dict[str, int] = {}

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if not text:
            return bool(default)
        return text in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_optional_int(value: Any, minimum: int, maximum: int) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except Exception:
            return None
        if parsed < minimum or parsed > maximum:
            return None
        return parsed

    @staticmethod
    def _decode_header_value(value: str) -> str:
        if not value:
            return ""
        chunks = []
        for part, charset in decode_header(value):
            if isinstance(part, bytes):
                chunks.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                chunks.append(str(part))
        return " ".join(chunks)

    @staticmethod
    def _generate_password(length: int = 14) -> str:
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*_+-="
        return "".join(random.choice(alphabet) for _ in range(max(10, int(length or 14))))

    def _generate_username_prefix(self, profile: Optional[Dict[str, Any]] = None) -> str:
        configured = str(self.config.get("username_prefix") or "").strip().lower()
        if configured:
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
            return f"{configured}{suffix}"
        profile = profile or {}
        head = str(profile.get("first_name") or self.config.get("first_name") or "alex").lower()
        tail = str(profile.get("last_name") or self.config.get("last_name") or "river").lower()
        digits = "".join(random.choices(string.digits, k=4))
        return f"{re.sub(r'[^a-z0-9]', '', head + tail)[:16]}{digits}"

    def _generate_alias_components(self, profile: Dict[str, Any]) -> Tuple[str, str]:
        alias_prefix = re.sub(r"[^a-z0-9]", "", str(self.config.get("alias_prefix") or self.config.get("username_prefix") or "monster").lower()) or "monster"
        random_len = max(1, int(self.config.get("alias_random_length") or 4))
        start_counter = max(0, int(self.config.get("alias_start_counter") or 1))
        nickname = alias_prefix
        keyword = f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=random_len))}{start_counter}"
        return nickname, keyword

    @staticmethod
    def _extract_best_alias_from_text(
        visible_text: str,
        *,
        domain: str,
        nickname: str,
        keyword: str,
        fallback_alias: str,
    ) -> str:
        domain_text = str(domain or "yahoo.com").strip().lower()
        fallback = str(fallback_alias or "").strip().lower()
        candidates = [
            item.strip().lower()
            for item in re.findall(
                rf"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]*{re.escape(domain_text)})",
                str(visible_text or ""),
            )
        ]
        if not candidates:
            return fallback
        if fallback in candidates:
            return fallback
        for marker in [str(keyword or "").strip().lower(), str(nickname or "").strip().lower()]:
            if not marker:
                continue
            for candidate in candidates:
                if marker in candidate.split("@", 1)[0]:
                    return candidate
        return fallback or candidates[0]

    def _build_child_profile(self) -> Dict[str, Any]:
        first_name = str(self.config.get("first_name") or "").strip()
        last_name = str(self.config.get("last_name") or "").strip()
        birth_year = self.config.get("birth_year")
        birth_month = self.config.get("birth_month")
        birth_day = self.config.get("birth_day")

        if not first_name or not last_name:
            auto_first, auto_last = generate_random_name()
            if not first_name:
                first_name = auto_first
            if not last_name:
                last_name = auto_last

        if birth_year is None or birth_month is None or birth_day is None:
            auto_birthdate = generate_random_birthday()
            auto_year, auto_month, auto_day = [int(part) for part in auto_birthdate.split("-")]
            if birth_year is None:
                birth_year = auto_year
            if birth_month is None:
                birth_month = auto_month
            if birth_day is None:
                birth_day = auto_day

        return {
            "first_name": first_name,
            "last_name": last_name,
            "birth_year": int(birth_year),
            "birth_month": int(birth_month),
            "birth_day": int(birth_day),
        }

    def _cache_account(self, account_info: Dict[str, Any]) -> None:
        email_value = str(account_info.get("email") or "").strip().lower()
        service_id = str(account_info.get("service_id") or email_value).strip()
        if email_value:
            self._accounts_by_email[email_value] = account_info
        if service_id:
            self._accounts_by_id[service_id] = account_info

    def _get_cached_account(self, email_value: Optional[str] = None, email_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if email_id:
            cached = self._accounts_by_id.get(str(email_id).strip())
            if cached:
                return cached
        if email_value:
            cached = self._accounts_by_email.get(str(email_value).strip().lower())
            if cached:
                return cached
        return None

    def _get_parent_seed_credentials(self) -> Tuple[str, str, str]:
        parent_email = str(self.config.get("parent_email") or "").strip().lower()
        parent_password = str(self.config.get("parent_password") or "").strip()
        parent_app_password = str(self.config.get("parent_app_password") or "").strip()
        return parent_email, parent_password, parent_app_password

    def _debug_event(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        try:
            callback = self.config.get("_debug_logger")
            if callable(callback):
                callback(f"[Yahoo OTP] {text}")
                return
        except Exception:
            pass
        logger.info("[Yahoo OTP] %s", text)

    def _extract_otp(self, text: str, pattern: str = OTP_CODE_PATTERN) -> Optional[str]:
        raw = str(text or "")
        if not raw:
            return None
        semantic = re.search(OTP_CODE_SEMANTIC_PATTERN, raw, re.IGNORECASE)
        if semantic:
            return semantic.group(1)
        matched = re.search(pattern or OTP_CODE_PATTERN, raw)
        return matched.group(1) if matched else None

    def _is_openai_mail(self, sender: str, subject: str, body: str) -> bool:
        sender_lower = str(sender or "").lower()
        merged = "\n".join([str(sender or ""), str(subject or ""), str(body or "")]).lower()
        if "openai" not in merged:
            return False
        for allow in OPENAI_EMAIL_SENDERS:
            marker = str(allow or "").lower()
            if marker and marker in sender_lower:
                return True
        return any(token in merged for token in ("verification code", "verify", "one-time code", "otp", "openai"))

    @staticmethod
    def _is_yahoo_signup_mail(sender: str, subject: str, body: str) -> bool:
        sender_lower = str(sender or "").lower()
        merged = "\n".join([str(sender or ""), str(subject or ""), str(body or "")]).lower()
        if "yahoo" not in sender_lower and "yahoo" not in merged:
            return False
        return any(
            token in merged
            for token in (
                "verification code",
                "confirm your account",
                "confirm your recovery",
                "security code",
                "account key",
                "yahoo",
            )
        )

    def _ensure_playwright(self):
        _ensure_playwright_stdio_safe()
        _ensure_playwright_runtime_libs()
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception as exc:
            raise EmailServiceError(
                "playwright not installed (pip install playwright && playwright install chromium)"
            ) from exc
        return sync_playwright

    def _launch_browser(self, headless: Optional[bool] = None):
        browser_provider = str(self.config.get("browser_provider") or "playwright").strip().lower()
        if browser_provider == "roxy":
            raise EmailServiceError("roxy provider should use external window open/attach flow instead of local playwright launch")
        sync_playwright = self._ensure_playwright()
        proxy_server = str(self.config.get("proxy_url") or "").strip()
        headless_value = self.config["headless"] if headless is None else bool(headless)
        locale = str(self.config.get("locale") or self.DEFAULT_BROWSER_LOCALE).strip() or self.DEFAULT_BROWSER_LOCALE
        timezone_id = str(self.config.get("timezone_id") or self.DEFAULT_BROWSER_TIMEZONE).strip() or self.DEFAULT_BROWSER_TIMEZONE
        launch_env = dict(os.environ)
        launch_env.setdefault("LANG", "en_US.UTF-8")
        launch_env.setdefault("LC_ALL", "en_US.UTF-8")
        launch_env["TZ"] = timezone_id
        launch_kwargs: Dict[str, Any] = {
            "headless": bool(headless_value),
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                f"--lang={locale}",
                "--window-size=1366,900",
                "--start-maximized",
                "--disable-popup-blocking",
            ],
            "env": launch_env,
        }
        if proxy_server:
            parsed = urlparse(proxy_server)
            if parsed.scheme and parsed.hostname and parsed.port:
                proxy_config: Dict[str, Any] = {
                    "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                }
                if parsed.username:
                    proxy_config["username"] = unquote(parsed.username)
                if parsed.password:
                    proxy_config["password"] = unquote(parsed.password)
                launch_kwargs["proxy"] = proxy_config
            else:
                launch_kwargs["proxy"] = {"server": proxy_server}
        return sync_playwright(), launch_kwargs

    def _build_browser_context_kwargs(self) -> Dict[str, Any]:
        locale = str(self.config.get("locale") or self.DEFAULT_BROWSER_LOCALE).strip() or self.DEFAULT_BROWSER_LOCALE
        timezone_id = str(self.config.get("timezone_id") or self.DEFAULT_BROWSER_TIMEZONE).strip() or self.DEFAULT_BROWSER_TIMEZONE
        user_agent = str(self.config.get("user_agent") or self.DEFAULT_BROWSER_USER_AGENT).strip() or self.DEFAULT_BROWSER_USER_AGENT
        return {
            "viewport": {"width": 1366, "height": 900},
            "locale": locale,
            "timezone_id": timezone_id,
            "user_agent": user_agent,
            "color_scheme": "light",
            "extra_http_headers": {
                "Accept-Language": f"{locale},en;q=0.9",
            },
        }

    def _build_yahoo_browser_fp_payload(self) -> Dict[str, Any]:
        locale = str(self.config.get("locale") or self.DEFAULT_BROWSER_LOCALE).strip() or self.DEFAULT_BROWSER_LOCALE
        timezone_id = str(self.config.get("timezone_id") or self.DEFAULT_BROWSER_TIMEZONE).strip() or self.DEFAULT_BROWSER_TIMEZONE
        timezone_offset = 240 if timezone_id == "America/New_York" else 0
        return {
            "webdriver": 0,
            "language": locale,
            "colorDepth": 24,
            "deviceMemory": 8,
            "pixelRatio": 1,
            "hardwareConcurrency": 8,
            "timezoneOffset": timezone_offset,
            "timezone": timezone_id,
            "sessionStorage": 1,
            "localStorage": 1,
            "indexedDb": 1,
            "cpuClass": "unknown",
            "platform": "Win32",
            "doNotTrack": "unknown",
            "plugins": {"count": 5, "hash": "d7f1b56f7b8b3d1c3f6f2fd9b6b6f145"},
            "canvas": "canvas winding:yes~canvas",
            "webgl": 1,
            "webglVendorAndRenderer": "Intel Inc.~Intel Iris OpenGL Engine",
            "adBlock": 0,
            "hasLiedLanguages": 0,
            "hasLiedResolution": 0,
            "hasLiedOs": 0,
            "hasLiedBrowser": 0,
            "touchSupport": {"points": 0, "event": 0, "start": 0},
            "fonts": {"count": 28, "hash": "f0b51e9a6d8f3f9c2b1c1b92f8d4c92a"},
            "fontsFlash": "swf object not loaded",
            "audio": "35.74996868677616",
            "resolution": {"w": "1366", "h": "768"},
            "availableResolution": {"w": "1366", "h": "728"},
            "ts": {"render": int(time.time() * 1000)},
        }

    def _inject_yahoo_browser_fp_payload(self, page) -> None:
        payload = self._build_yahoo_browser_fp_payload()
        try:
            page.evaluate(
                """(fp) => {
                    const apply = () => {
                      const raw = JSON.stringify(fp);
                      document.querySelectorAll('input[name="browser-fp-data"]').forEach((el) => {
                        el.value = raw;
                        el.setAttribute('value', raw);
                      });
                    };
                    window.__yahooForcedFp = fp;
                    apply();
                    document.addEventListener('submit', apply, true);
                    window.addEventListener('beforeunload', apply, true);
                }""",
                payload,
            )
        except Exception:
            pass

    def _create_browser_context(self, browser):
        context_kwargs = self._build_browser_context_kwargs()
        try:
            context = browser.new_context(**context_kwargs)
        except Exception:
            context_kwargs.pop("timezone_id", None)
            context = browser.new_context(**context_kwargs)
        try:
            context.add_init_script(YAHOO_STEALTH_INIT_SCRIPT)
        except Exception:
            pass
        return context

    @staticmethod
    def _roxy_request(api_host: str, token: str, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
        base = str(api_host or "http://127.0.0.1:50000").rstrip("/")
        url = f"{base}{path}"
        headers = {"Accept": "application/json", "token": str(token or "").strip()}
        data = None
        filtered = {k: v for k, v in (payload or {}).items() if v not in (None, "", [], {})}
        if str(method or "GET").upper() != "GET":
            headers["Content-Type"] = "application/json"
            data = json.dumps(filtered, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=str(method or "GET").upper())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        if parsed.get("code") not in (None, 0, "0"):
            raise EmailServiceError(f"Roxy API 返回异常: {json.dumps(parsed, ensure_ascii=False)}")
        return parsed

    def _open_roxy_browser(self) -> str:
        api_host = str(self.config.get("roxy_api_host") or "http://127.0.0.1:50000").strip() or "http://127.0.0.1:50000"
        token = str(self.config.get("roxy_token") or "").strip()
        dir_id = str(self.config.get("roxy_dir_id") or "").strip()
        workspace_id = int(self.config.get("roxy_workspace_id") or 0)
        if not dir_id:
            raise EmailServiceError("Roxy 模式缺少 roxy_dir_id，无法打开窗口")
        payload = {
            "workspaceId": workspace_id,
            "dirId": dir_id,
            "forceOpen": bool(self.config.get("roxy_force_open", True)),
            "headless": bool(self.config.get("headless", False)),
        }
        response = self._roxy_request(api_host, token, "POST", "/browser/open", payload)
        ws = str((response.get("data") or {}).get("ws") or "").strip()
        if not ws:
            raise EmailServiceError("Roxy /browser/open 未返回 ws")
        return ws

    @staticmethod
    def _find_first_visible(page, selectors: List[str], timeout_ms: int = 4000):
        deadline = time.time() + max(0.5, timeout_ms / 1000.0)
        while time.time() < deadline:
            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if locator.count() and locator.is_visible():
                        return locator
                except Exception:
                    continue
            time.sleep(0.15)
        return None

    @staticmethod
    def _fill_first(page, selectors: List[str], value: str, timeout_ms: int = 4000) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        locator = YahooMailService._find_first_visible(page, selectors, timeout_ms=timeout_ms)
        if locator is None:
            return False
        try:
            tag_name = str(locator.evaluate("el => el.tagName.toLowerCase()") or "").strip().lower()
            if tag_name == "select":
                try:
                    locator.select_option(label=text)
                except Exception:
                    locator.select_option(value=text)
            else:
                locator.fill(text)
            return True
        except Exception:
            return False

    @staticmethod
    def _click_first(page, selectors: List[str], timeout_ms: int = 4000) -> bool:
        locator = YahooMailService._find_first_visible(page, selectors, timeout_ms=timeout_ms)
        if locator is None:
            return False
        try:
            locator.click()
            return True
        except Exception:
            return False

    @staticmethod
    def _extract_visible_text(page) -> str:
        try:
            return str(page.locator("body").inner_text(timeout=2000) or "")
        except Exception:
            return ""

    @staticmethod
    def _dump_page_debug_artifacts(page, stage: str) -> Optional[Dict[str, str]]:
        try:
            dump_dir = Path("release") / "yahoo_probe"
            dump_dir.mkdir(parents=True, exist_ok=True)
            stamp = f"{int(time.time() * 1000)}_{re.sub(r'[^a-z0-9_-]+', '_', str(stage or 'page').lower())}"
            screenshot_path = dump_dir / f"{stamp}.png"
            html_path = dump_dir / f"{stamp}.html"
            text_path = dump_dir / f"{stamp}.txt"
            json_path = dump_dir / f"{stamp}.json"

            page_text = YahooMailService._extract_visible_text(page)
            try:
                html = page.content()
            except Exception:
                html = ""
            try:
                title = page.title()
            except Exception:
                title = ""
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception:
                pass

            html_path.write_text(str(html or ""), encoding="utf-8")
            text_path.write_text(str(page_text or ""), encoding="utf-8")
            payload = {
                "stage": stage,
                "url": str(getattr(page, 'url', '') or ''),
                "title": str(title or ""),
                "text_head": str(page_text or "")[:2000],
                "html": str(html_path),
                "text": str(text_path),
                "screenshot": str(screenshot_path),
            }
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            payload["json"] = str(json_path)
            return payload
        except Exception:
            return None

    @staticmethod
    def _is_expected_yahoo_login_path(page_url: str) -> bool:
        """Yahoo 正常登录流程里密码页本身就会落在 /account/challenge/password。"""
        path = str(urlparse(str(page_url or "")).path or "").strip().lower()
        return path in {
            "/account/challenge/password",
            "/account/challenge/identifier",
            "/account/challenge/username",
        }

    def _try_resolve_yahoo_challenge_selector(self, page) -> bool:
        """
        某些账号在输入用户名后会先进入 challenge-selector，让用户选择登录方式。
        如果这里还能继续走 password 流程，就不要误判成风控。
        """
        path = str(urlparse(str(page.url or "")).path or "").strip().lower()
        if path != "/account/challenge/challenge-selector":
            return False

        page.wait_for_timeout(800)
        resolution_selectors = [
            'button:has-text("Password")',
            'a:has-text("Password")',
            'button:has-text("Use password")',
            'a:has-text("Use password")',
            'button:has-text("Try another way")',
            'a:has-text("Try another way")',
            'button:has-text("使用密码")',
            'a:has-text("使用密码")',
            'button:has-text("密码")',
            'a:has-text("密码")',
            '[data-challenge*="password" i]',
            '[href*="/account/challenge/password" i]',
        ]

        for selector in resolution_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    locator.click()
                    page.wait_for_timeout(1500)
                    if self._fill_first(page, ['#login-passwd', 'input[name="password"]'], ""):
                        return True
                    if self._is_expected_yahoo_login_path(page.url):
                        return True
            except Exception:
                continue

        try:
            if page.locator('#login-passwd, input[name="password"]').count():
                return True
        except Exception:
            pass
        return False

    def _complete_yahoo_email_identity_challenge(self, page) -> bool:
        """
        Yahoo 有时会在 challenge-selector 中要求“通过邮箱验证码确认身份”。
        这类情况可以复用母号 IMAP/浏览器轮询把验证码取回来自动继续。
        """
        page_text = self._extract_visible_text(page)
        page_text_lower = str(page_text or "").lower()
        path = str(urlparse(str(page.url or "")).path or "").strip().lower()

        identity_markers = (
            "is it really you",
            "verify your identity",
            "keep your account secure",
            "quickly verify your identity",
            "choose a way to verify",
            "选择一种验证方式",
            "验证您的身份",
            "验证身份",
            "确保登录的是您本人",
            "这有助于确保登录的是您本人",
        )
        email_markers = (
            "get a code at",
            "email",
            "security code",
            "verification code",
            "电子邮件",
            "邮箱",
            "验证码",
            "获取验证码",
            "发送验证码",
        )
        if path != "/account/challenge/challenge-selector":
            return False
        if not any(marker in page_text_lower for marker in identity_markers):
            return False
        if not any(marker in page_text_lower for marker in email_markers):
            return False

        # 优先切到 Email 取码，而不是 Push / App 验证。
        self._click_first(
            page,
            [
                'button:has-text("Email")',
                'a:has-text("Email")',
                'button:has-text("电子邮件")',
                'a:has-text("电子邮件")',
                'button:has-text("Get a code")',
                'a:has-text("Get a code")',
                'button:has-text("Send code")',
                'a:has-text("Send code")',
                'button:has-text("邮箱")',
                'a:has-text("邮箱")',
                'button:has-text("获取验证码")',
                'a:has-text("获取验证码")',
                'button:has-text("发送验证码")',
                'a:has-text("发送验证码")',
            ],
        )
        page.wait_for_timeout(1200)
        self._click_first(
            page,
            [
                'button:has-text("Continue")',
                'button:has-text("Verify")',
                'button:has-text("Send code")',
                'button:has-text("Get code")',
                'button:has-text("继续")',
                'button:has-text("验证")',
                'button:has-text("发送验证码")',
                'button[type="submit"]',
            ],
        )
        page.wait_for_timeout(1800)

        code = self._wait_for_parent_signup_code(timeout=max(30, int(self.config.get("timeout") or 30)))
        if not code:
            raise EmailServiceError("Yahoo 身份验证页已选择 Email 验证，但未在母号收件箱收到验证码")

        if not self._fill_signup_verification_code(page, code):
            raise EmailServiceError("Yahoo 身份验证页未找到验证码输入框")

        if not self._click_first(
            page,
            [
                'button:has-text("Verify")',
                'button:has-text("Continue")',
                'button:has-text("Submit")',
                'button:has-text("验证")',
                'button:has-text("继续")',
                'button[type="submit"]',
            ],
        ):
            raise EmailServiceError("Yahoo 身份验证页未找到验证码确认按钮")

        page.wait_for_timeout(2200)
        return True

    def _check_yahoo_blocker(self, page) -> None:
        """
        统一处理 Yahoo 登录阶段的阻断检测。
        对 challenge-selector 先尝试自动切换到密码登录，再决定是否报错。
        """
        if self._try_resolve_yahoo_challenge_selector(page):
            return
        if self._complete_yahoo_email_identity_challenge(page):
            return

        page_text = self._extract_visible_text(page)
        blocker = self._detect_yahoo_blocker(page_text, page.url)
        if blocker:
            self._raise_yahoo_blocker(page, blocker, page_text=page_text)

    @staticmethod
    def _stage_name_from_url(page_url: str) -> str:
        path = str(urlparse(str(page_url or "")).path or "").strip().lower()
        stage = re.sub(r"[^a-z0-9]+", "_", path.strip("/"))
        return stage or "yahoo_blocker"

    def _raise_yahoo_blocker(self, page, blocker: str, *, page_text: Optional[str] = None) -> None:
        text = str(page_text or self._extract_visible_text(page) or "")
        snippet = re.sub(r"\s+", " ", text).strip()[:220]
        artifact = self._dump_page_debug_artifacts(page, self._stage_name_from_url(str(page.url or "")))
        detail = str(blocker or "").strip() or "Yahoo 页面阻断"
        if snippet:
            detail = f"{detail} | 页面摘要: {snippet}"
        if artifact:
            detail = f"{detail} | dump={artifact.get('json')}"
        raise EmailServiceError(detail)

    def _classify_yahoo_fail_page(self, page_text: str, page_url: str) -> Optional[str]:
        path = str(urlparse(str(page_url or "")).path or "").strip().lower()
        if path != "/account/challenge/fail":
            return None
        text = str(page_text or "").lower()
        if "different device" in text or "different browser" in text:
            return "Yahoo challenge/fail：当前出口或浏览器指纹被判定为异常设备，请更换代理或继续降低自动化指纹"
        if "could not sign you in" in text:
            return "Yahoo challenge/fail：Yahoo 拒绝当前登录环境，请更换代理或设备指纹后重试"
        if "try again later" in text or "temporarily unavailable" in text:
            return "Yahoo challenge/fail：当前登录被临时阻断，请稍后或更换代理后重试"
        return "Yahoo 返回 challenge/fail 页面，自动流程已中止"

    def _classify_yahoo_password_page_error(self, page_text: str, page_url: str) -> Optional[str]:
        path = str(urlparse(str(page_url or "")).path or "").strip().lower()
        if path != "/account/challenge/password":
            return None
        text = str(page_text or "").lower()
        if any(token in text for token in ("invalid password", "incorrect password", "wrong password")):
            return "Yahoo 密码验证失败，请确认母号密码是否正确"
        if "too many failed attempts" in text:
            return "Yahoo 密码页提示尝试次数过多，当前账号被暂时限制"
        return None

    def _detect_yahoo_blocker(self, page_text: str, page_url: str) -> Optional[str]:
        text = str(page_text or "").lower()
        url = str(page_url or "").lower()
        fail_page = self._classify_yahoo_fail_page(page_text, page_url)
        if fail_page:
            return fail_page
        password_page_error = self._classify_yahoo_password_page_error(page_text, page_url)
        if password_page_error:
            return password_page_error
        if any(token in text for token in ("captcha", "security challenge", "verify it's you", "challenge required")):
            return "Yahoo 登录/注册遇到 challenge，当前实现不会绕过验证码"
        if "phone number" in text and "required" in text:
            return "Yahoo 注册页要求手机号，请在 Yahoo 服务配置中补充 phone_number"
        path = str(urlparse(url).path or "").strip().lower()
        if self._is_expected_yahoo_login_path(url):
            return None
        if "/account/challenge/" in path or path.endswith("/challenge"):
            return f"Yahoo 返回 challenge 页面，自动流程已中止: {path or url}"
        return None

    def _wait_post_password_result(self, page, timeout_ms: int = 10000) -> None:
        deadline = time.time() + max(2.0, timeout_ms / 1000.0)
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            page_text = self._extract_visible_text(page)
            blocker = self._detect_yahoo_blocker(page_text, page.url)
            if blocker:
                self._raise_yahoo_blocker(page, blocker, page_text=page_text)
            current_url = str(page.url or "").lower()
            if "mail.yahoo.com" in current_url or "/d/folders/" in current_url:
                return
            if not self._is_expected_yahoo_login_path(current_url):
                return

    def _login_yahoo_browser(self, email_value: str, password_value: str, open_mailbox: bool = True):
        if not email_value or not password_value:
            raise EmailServiceError("Yahoo 浏览器登录缺少 email/password")

        playwright_ctx, launch_kwargs = self._launch_browser(headless=self.config.get("headless"))
        pw = playwright_ctx.__enter__()
        playwright_handle = None
        browser = None
        try:
            browser = pw.chromium.launch(**launch_kwargs)
            context = self._create_browser_context(browser)
            page = context.new_page()
            page.set_default_timeout(max(30000, int(self.config["timeout"]) * 1000))
            try:
                page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                classified = _classify_yahoo_network_error(exc, self.config.get("proxy_url"))
                if classified:
                    raise EmailServiceError(classified) from exc
                raise
            self._inject_yahoo_browser_fp_payload(page)

            if not self._fill_first(
                page,
                [
                    '#login-username',
                    '#username',
                    'input[name="username"]',
                    'input[id*="username" i]',
                    'input[autocomplete="username"]',
                    'input[type="email"]',
                    'input[name*="email" i]',
                    'input[aria-label*="email" i]',
                    'input[aria-label*="邮箱" i]',
                    'input[placeholder*="email" i]',
                    'input[placeholder*="邮箱" i]',
                ],
                email_value,
                timeout_ms=8000,
            ):
                artifact = self._dump_page_debug_artifacts(page, "login_username_missing")
                if artifact:
                    raise EmailServiceError(
                        "Yahoo 登录页未找到邮箱输入框"
                        f" | url={artifact.get('url')}"
                        f" | title={artifact.get('title')}"
                        f" | dump={artifact.get('json')}"
                    )
                raise EmailServiceError("Yahoo 登录页未找到邮箱输入框")
            if not self._click_first(
                page,
                [
                    '#login-signin',
                    '#signin',
                    'button[name="signin"]',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("下一页")',
                    'button:has-text("Next")',
                    'button:has-text("Sign in")',
                    'button:has-text("登录")',
                    'button:has-text("继续")',
                ],
                timeout_ms=8000,
            ):
                artifact = self._dump_page_debug_artifacts(page, "login_next_missing")
                if artifact:
                    raise EmailServiceError(
                        "Yahoo 登录页未找到下一步按钮"
                        f" | url={artifact.get('url')}"
                        f" | title={artifact.get('title')}"
                        f" | dump={artifact.get('json')}"
                    )
                raise EmailServiceError("Yahoo 登录页未找到下一步按钮")

            page.wait_for_timeout(1200)
            self._inject_yahoo_browser_fp_payload(page)
            self._check_yahoo_blocker(page)

            if not self._fill_first(page, ['#login-passwd', 'input[name="password"]'], password_value):
                raise EmailServiceError("Yahoo 登录页未找到密码输入框")
            self._inject_yahoo_browser_fp_payload(page)
            if not self._click_first(page, ['#login-signin', 'button[name="verifyPassword"]', 'button[type="submit"]']):
                raise EmailServiceError("Yahoo 密码页未找到登录按钮")

            page.wait_for_timeout(1200)
            self._click_first(page, ['button[name="verifyYidBtn"]', 'button[name="verifyYidSignIn"]'])
            page.wait_for_timeout(800)
            self._wait_post_password_result(page)

            self._check_yahoo_blocker(page)

            if open_mailbox:
                try:
                    page.goto(self.MAILBOX_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:
                    classified = _classify_yahoo_network_error(exc, self.config.get("proxy_url"))
                    if classified:
                        raise EmailServiceError(classified) from exc
                    raise
                page.wait_for_timeout(2200)
                self._check_yahoo_blocker(page)

            playwright_handle = pw
            return playwright_handle, browser, context, page
        except Exception:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            playwright_ctx.__exit__(None, None, None)
            raise

    def _close_browser_session(self, playwright_handle, browser) -> None:
        try:
            if browser:
                browser.close()
        finally:
            try:
                playwright_handle.stop()
            except Exception:
                pass

    def _extract_code_from_page_with_detector(self, page, detector) -> Optional[str]:
        page_text = self._extract_visible_text(page)
        if detector("yahoo", "page", page_text):
            return self._extract_otp(page_text, OTP_CODE_PATTERN)
        return None

    def _poll_parent_code_via_imap(
        self,
        email_value: str,
        password_value: str,
        timeout: int,
    ) -> Optional[str]:
        start = time.time()
        mailbox = None
        seen_ids: set[str] = set()
        try:
            mailbox = self._imap_connect(email_value, password_value)
            mailbox.select("INBOX")
            while time.time() - start < timeout:
                status, data = mailbox.search(None, "UNSEEN")
                if status != "OK":
                    time.sleep(self.config["poll_interval"])
                    continue
                for raw_id in reversed(data[0].split()):
                    mail_id = raw_id.decode()
                    if mail_id in seen_ids:
                        continue
                    seen_ids.add(mail_id)
                    fetch_status, payload = mailbox.fetch(raw_id, "(RFC822)")
                    if fetch_status != "OK" or not payload:
                        continue
                    message = email.message_from_bytes(payload[0][1])
                    sender = self._decode_header_value(message.get("From", ""))
                    subject = self._decode_header_value(message.get("Subject", ""))
                    body = self._extract_mail_body(message)
                    if not self._is_yahoo_signup_mail(sender, subject, body):
                        continue
                    code = self._extract_otp(body, OTP_CODE_PATTERN)
                    if code:
                        return self._dedupe_code(email_value, code, None)
                time.sleep(self.config["poll_interval"])
        except Exception as exc:
            logger.debug("Yahoo parent IMAP code polling failed: %s", exc)
        finally:
            if mailbox:
                try:
                    mailbox.logout()
                except Exception:
                    pass
        return None

    def _poll_parent_code_via_browser(
        self,
        email_value: str,
        password_value: str,
        timeout: int,
    ) -> Optional[str]:
        playwright_handle = None
        browser = None
        try:
            playwright_handle, browser, _context, page = self._login_yahoo_browser(
                email_value=email_value,
                password_value=password_value,
                open_mailbox=True,
            )
            start = time.time()
            while time.time() - start < timeout:
                page.wait_for_timeout(1500)
                self._click_first(
                    page,
                    [
                        'span:has-text("Yahoo")',
                        'a:has-text("Yahoo")',
                        '[title*="Yahoo" i]',
                        '[aria-label*="Yahoo" i]',
                    ],
                )
                page.wait_for_timeout(1200)
                page_text = self._extract_visible_text(page)
                if self._is_yahoo_signup_mail("yahoo", "browser", page_text):
                    code = self._extract_otp(page_text, OTP_CODE_PATTERN)
                    if code:
                        deduped = self._dedupe_code(email_value, code, None)
                        if deduped:
                            return deduped
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                time.sleep(self.config["poll_interval"])
        except Exception as exc:
            logger.debug("Yahoo parent browser code polling failed: %s", exc)
        finally:
            if playwright_handle:
                self._close_browser_session(playwright_handle, browser)
        return None

    def _wait_for_parent_signup_code(self, timeout: int) -> Optional[str]:
        parent_email, parent_password, parent_app_password = self._get_parent_seed_credentials()
        if parent_email and parent_app_password:
            code = self._poll_parent_code_via_imap(parent_email, parent_app_password, timeout)
            if code:
                return code
        if parent_email and parent_password:
            code = self._poll_parent_code_via_browser(parent_email, parent_password, timeout)
            if code:
                return code
        return None

    def _fill_signup_verification_code(self, page, code: str) -> bool:
        if not code:
            return False
        if self._fill_first(
            page,
            [
                'input[name="verificationCode"]',
                'input[name="code"]',
                '#verification-code-field',
                '#verification-code-input',
                'input[id*="verification-code"]',
                'input[autocomplete="one-time-code"]',
            ],
            code,
        ):
            return True

        try:
            inputs = page.locator('input[maxlength="1"], input[inputmode="numeric"]').all()
        except Exception:
            inputs = []
        if inputs:
            digits = list(str(code))
            for idx, digit in enumerate(digits[: len(inputs)]):
                try:
                    inputs[idx].fill(digit)
                except Exception:
                    return False
            return True
        return False

    def _create_alias_with_parent_mailbox(self) -> Dict[str, Any]:
        parent_email, parent_password, parent_app_password = self._get_parent_seed_credentials()
        if not parent_email:
            raise EmailServiceError("Yahoo parent alias mode requires parent_email")
        if not parent_password:
            raise EmailServiceError("Yahoo 母号 alias 模式需要 parent_password，才能登录母号创建临时子地址")

        domain = str(self.config.get("domain") or "yahoo.com").strip().lower()
        self.config["username_prefix"] = str(self.config.get("alias_prefix") or self.config.get("username_prefix") or "monster").strip().lower() or "monster"

        browser_provider = str(self.config.get("browser_provider") or "playwright").strip().lower()
        playwright_handle = None
        browser = None
        try:
            # 登录母号邮箱
            if browser_provider == "roxy":
                ws_endpoint = str(self.config.get("roxy_ws_endpoint") or "").strip() or self._open_roxy_browser()
                self.config["roxy_ws_endpoint"] = ws_endpoint
                sync_playwright = self._ensure_playwright()
                playwright_ctx = sync_playwright()
                pw = playwright_ctx.__enter__()
                browser = pw.chromium.connect_over_cdp(ws_endpoint)
                _context, page = self._select_existing_mail_page(browser)
                page.set_default_timeout(max(30000, int(self.config.get("timeout") or 30) * 1000))
                page.goto(self.MAILBOX_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1800)
                playwright_handle = pw
            else:
                playwright_handle, browser, _context, page = self._login_yahoo_browser(
                    email_value=parent_email,
                    password_value=parent_password,
                    open_mailbox=True,
                )

            created = self._create_and_verify_alias_on_page(
                page,
                domain=domain,
                max_attempts=max(2, int(self.config.get("max_retries") or 3)),
            )
            alias_address = str(created.get("alias_email") or "").strip().lower()
            nickname = str(created.get("nickname") or "").strip().lower()
            keyword = str(created.get("keyword") or "").strip().lower()
            profile = created.get("profile") or {}

            account_info = {
                "email": alias_address,
                "service_id": alias_address,
                "id": alias_address,
                "parent_email": parent_email,
                "mailbox_owner_email": parent_email,
                "mailbox_owner_password": parent_password,
                "mailbox_owner_app_password": parent_app_password,
                "alias_nickname": nickname,
                "alias_keyword": keyword,
                "profile": profile,
                "mode": "parent_alias",
                "created_at": time.time(),
            }
            self._cache_account(account_info)
            return account_info
        finally:
            if playwright_handle:
                self._close_browser_session(playwright_handle, browser)

    def _create_account_with_browser(self) -> Dict[str, Any]:
        parent_email, _parent_password, _parent_app_password = self._get_parent_seed_credentials()
        profile = self._build_child_profile()
        password_value = str(self.config.get("password") or "").strip() or self._generate_password()
        username_prefix = self._generate_username_prefix(profile)
        domain = str(self.config.get("domain") or "yahoo.com").strip().lower()
        recovery_email = str(self.config.get("recovery_email") or "").strip() or parent_email

        playwright_ctx, launch_kwargs = self._launch_browser(headless=self.config.get("headless"))
        pw = playwright_ctx.__enter__()
        browser = None
        try:
            browser = pw.chromium.launch(**launch_kwargs)
            context = self._create_browser_context(browser)
            page = context.new_page()
            page.set_default_timeout(max(30000, int(self.config["timeout"]) * 1000))
            page.goto(self.SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)

            self._fill_first(page, ['#usernamereg-firstName', 'input[name="firstName"]'], str(profile["first_name"]))
            self._fill_first(page, ['#usernamereg-lastName', 'input[name="lastName"]'], str(profile["last_name"]))
            self._fill_first(page, ['#usernamereg-yid', 'input[name="yid"]'], username_prefix)
            self._fill_first(page, ['#usernamereg-password', 'input[name="password"]'], password_value)
            self._fill_first(page, ['#usernamereg-phone', 'input[name="phone"]'], str(self.config.get("phone_number") or ""))
            if recovery_email:
                self._click_first(
                    page,
                    [
                        'button:has-text("Use email instead")',
                        'a:has-text("Use email instead")',
                        'button:has-text("Use email")',
                    ],
                )
                page.wait_for_timeout(500)
                self._fill_first(
                    page,
                    [
                        '#usernamereg-email',
                        'input[name="secondaryEmail"]',
                        'input[name="recoveryEmail"]',
                        'input[type="email"]',
                    ],
                    recovery_email,
                )
            self._fill_first(page, ['#usernamereg-month', 'select[name="mm"]'], str(profile["birth_month"]))
            self._fill_first(page, ['#usernamereg-day', 'input[name="dd"]'], str(profile["birth_day"]))
            self._fill_first(page, ['#usernamereg-year', 'input[name="yyyy"]'], str(profile["birth_year"]))

            if not self._click_first(page, ['#reg-submit-button', 'button[name="signup"]', 'button[type="submit"]']):
                raise EmailServiceError("Yahoo 注册页未找到提交按钮")

            page.wait_for_timeout(2500)
            page_text = self._extract_visible_text(page)
            blocker = self._detect_yahoo_blocker(page_text, page.url)
            if blocker:
                raise EmailServiceError(blocker)

            lower_page_text = page_text.lower()
            if "enter the code" in lower_page_text or "verification code" in lower_page_text:
                code = self._wait_for_parent_signup_code(timeout=max(30, int(self.config.get("timeout") or 30)))
                if not code:
                    raise EmailServiceError("Yahoo child signup is waiting for a code, but no code was received in the parent inbox")
                if not self._fill_signup_verification_code(page, code):
                    raise EmailServiceError("Yahoo child signup code input not found")
                if not self._click_first(
                    page,
                    [
                        'button:has-text("Verify")',
                        'button:has-text("Continue")',
                        '#verification-submit',
                        'button[type="submit"]',
                    ],
                ):
                    raise EmailServiceError("Yahoo child signup confirm button not found")
                page.wait_for_timeout(2500)
                blocker = self._detect_yahoo_blocker(self._extract_visible_text(page), page.url)
                if blocker:
                    raise EmailServiceError(blocker)

            if "verify your phone" in page_text.lower():
                raise EmailServiceError("Yahoo 注册进入手机号/验证码验证阶段，当前实现不会绕过该验证")

            created_email = f"{username_prefix}@{domain}"
            account_info = {
                "email": created_email,
                "service_id": created_email,
                "id": created_email,
                "password": password_value,
                "app_password": "",
                "parent_email": parent_email,
                "mode": "parent_seed",
                "profile": profile,
                "created_at": time.time(),
            }
            self.config["email"] = created_email
            self.config["password"] = password_value
            self._cache_account(account_info)
            return account_info
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            try:
                playwright_ctx.__exit__(None, None, None)
            except Exception:
                pass

    def _imap_connect(self, email_value: str, password_value: str):
        mailbox = imaplib.IMAP4_SSL(
            self.IMAP_HOST,
            self.IMAP_PORT,
            timeout=int(self.config.get("imap_socket_timeout") or 12),
        )
        try:
            mailbox.sock.settimeout(int(self.config.get("imap_socket_timeout") or 12))
        except Exception:
            pass
        mailbox.login(email_value, password_value)
        return mailbox

    @staticmethod
    def _message_timestamp(message_obj) -> Optional[float]:
        raw_date = str(message_obj.get("Date") or "").strip()
        if not raw_date:
            return None
        try:
            parsed = parsedate_to_datetime(raw_date)
        except Exception:
            return None
        try:
            if parsed.tzinfo is not None:
                return parsed.timestamp()
            return parsed.replace(tzinfo=None).timestamp()
        except Exception:
            return None

    @staticmethod
    def _message_is_recent(message_obj, otp_sent_at: Optional[float], slack_seconds: int = 180) -> bool:
        if not otp_sent_at:
            return True
        message_ts = YahooMailService._message_timestamp(message_obj)
        if message_ts is None:
            return True
        return message_ts >= float(otp_sent_at) - max(0, int(slack_seconds))

    @staticmethod
    def _select_existing_mail_page(browser):
        preferred_hosts = ("mail.yahoo.com", "login.yahoo.com")
        contexts = list(getattr(browser, "contexts", []) or [])
        if not contexts:
            raise EmailServiceError("connect_over_cdp 未返回任何 browser context")

        for context in contexts:
            for page in context.pages:
                current_url = str(getattr(page, "url", "") or "")
                if any(host in current_url for host in preferred_hosts):
                    return context, page

        for context in contexts:
            if context.pages:
                return context, context.pages[0]

        context = contexts[0]
        return context, context.new_page()

    def _open_alias_settings_page(self, page) -> bool:
        candidates = [
            "https://mail.yahoo.com/n/settings/13?.src=ym&reason=myc",
            "https://mail.yahoo.com/n/settings/13",
            "https://mail.yahoo.com/n/settings",
            "https://mail.yahoo.com/d/settings/?.src=ym&reason=disp",
            "https://mail.yahoo.com/d/settings/disposable-addresses",
            "https://mail.yahoo.com/d/settings",
        ]
        for url in candidates:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1800)
                page_text = self._extract_visible_text(page).lower()
                if any(token in page_text for token in ("yahoo", "mail", "plus", "地址", "alias", "disposable", "临时")):
                    return True
            except Exception:
                continue

        if not self._click_first(
            page,
            [
                'button[aria-label*="Settings" i]',
                'button[aria-label*="设置" i]',
                'button:has-text("设置")',
                'button:has-text("Settings")',
                'button[aria-label*="偏好设置" i]',
            ],
            timeout_ms=6000,
        ):
            return False
        page.wait_for_timeout(800)
        if self._click_first(
            page,
            [
                'a:has-text("More Settings")',
                'button:has-text("More Settings")',
                'a:has-text("更多设置")',
                'button:has-text("更多设置")',
            ],
            timeout_ms=6000,
        ):
            page.wait_for_timeout(1200)
        return True

    def _verify_alias_present_on_settings_page(
        self,
        page,
        *,
        alias_email: str,
        nickname: str,
        keyword: str,
        dump_stage: str,
    ) -> Tuple[bool, Optional[Dict[str, str]]]:
        page_text = self._extract_visible_text(page)
        lower = page_text.lower()
        alias_lower = str(alias_email or "").strip().lower()
        nickname_lower = str(nickname or "").strip().lower()
        keyword_lower = str(keyword or "").strip().lower()

        if alias_lower and alias_lower in lower:
            self._debug_event(f"alias 强校验命中完整邮箱: {alias_lower}")
            return True, None
        if nickname_lower and keyword_lower and nickname_lower in lower and keyword_lower in lower:
            self._debug_event(f"alias 强校验命中 nickname/keyword: {nickname_lower} / {keyword_lower}")
            return True, None

        artifact = self._dump_page_debug_artifacts(page, dump_stage)
        return False, artifact

    def _create_and_verify_alias_on_page(
        self,
        page,
        *,
        domain: str,
        max_attempts: int = 3,
    ) -> Dict[str, str]:
        last_artifact: Optional[Dict[str, str]] = None
        for attempt in range(1, max_attempts + 1):
            profile = self._build_child_profile()
            nickname, keyword = self._generate_alias_components(profile)
            alias_address = f"{nickname}-{keyword}@{domain}"
            self._debug_event(f"alias 创建尝试 {attempt}/{max_attempts}: {alias_address}")

            if not self._open_alias_settings_page(page):
                last_artifact = self._dump_page_debug_artifacts(page, f"alias_settings_open_failed_{attempt}")
                continue

            self._click_first(
                page,
                [
                    'button:has-text("添加临时邮件地址")',
                    'button[aria-label*="临时邮件地址" i]',
                    'button[aria-label*="Disposable" i]',
                    'button:has-text("Disposable")',
                    'button:has-text("添加")',
                    'button:has-text("Add")',
                    'a:has-text("添加")',
                    'a:has-text("Add")',
                ],
                timeout_ms=4000,
            )
            page.wait_for_timeout(1000)

            nickname_filled = self._fill_first(
                page,
                [
                    'input[placeholder*="昵称" i]',
                    'input[placeholder*="永久昵称" i]',
                    'input[placeholder*="nickname" i]',
                    'input[aria-label*="昵称" i]',
                    'input[aria-label*="永久昵称" i]',
                    'input[aria-label*="nickname" i]',
                    'input[name*="nickname" i]',
                ],
                nickname,
                timeout_ms=5000,
            )
            if nickname_filled:
                self._click_first(
                    page,
                    [
                        'button:has-text("下一个")',
                        'button:has-text("下一步")',
                        'button:has-text("Next")',
                    ],
                    timeout_ms=5000,
                )
                page.wait_for_timeout(1000)

            if not self._fill_first(
                page,
                [
                    'input[placeholder*="关键字" i]',
                    'input[aria-label*="关键字" i]',
                    'input[name*="keyword" i]',
                    'input[name*="alias" i]',
                ],
                keyword,
                timeout_ms=5000,
            ):
                last_artifact = self._dump_page_debug_artifacts(page, f"alias_keyword_missing_{attempt}")
                continue

            self._click_first(
                page,
                [
                    'button:has-text("保存")',
                    'button:has-text("完成")',
                    'button:has-text("Save")',
                    'button:has-text("Done")',
                    'button:has-text("创建")',
                ],
                timeout_ms=5000,
            )
            page.wait_for_timeout(2200)

            visible_text = self._extract_visible_text(page)
            alias_address = self._extract_best_alias_from_text(
                visible_text,
                domain=domain,
                nickname=nickname,
                keyword=keyword,
                fallback_alias=alias_address,
            )

            self._open_alias_settings_page(page)
            page.wait_for_timeout(1200)
            verified, artifact = self._verify_alias_present_on_settings_page(
                page,
                alias_email=alias_address,
                nickname=nickname,
                keyword=keyword,
                dump_stage=f"alias_verify_failed_{attempt}",
            )
            if verified:
                return {
                    "alias_email": alias_address,
                    "nickname": nickname,
                    "keyword": keyword,
                    "profile": profile,
                }
            last_artifact = artifact
            self._debug_event(
                f"alias 强校验失败 attempt={attempt} alias={alias_address} dump={artifact.get('json') if artifact else '-'}"
            )

        detail = "Yahoo alias 创建后强校验失败，未在设置页确认 alias 已生效"
        if last_artifact:
            detail += f" | dump={last_artifact.get('json')}"
        raise EmailServiceError(detail)

    def _prepare_mailbox_search(self, page, target_email_lower: str) -> bool:
        if not target_email_lower:
            return False
        if not self._fill_first(
            page,
            [
                'input[placeholder*="搜索" i]',
                'input[placeholder*="Search" i]',
                'input[aria-label*="搜索" i]',
                'input[aria-label*="Search" i]',
            ],
            target_email_lower,
        ):
            return False
        self._click_first(
            page,
            [
                'button[aria-label*="搜索" i]',
                'button[aria-label*="Search" i]',
                'button:has-text("搜索")',
                'button:has-text("Search")',
            ],
        )
        page.wait_for_timeout(1200)
        return True

    def _poll_openai_code_from_page(
        self,
        page,
        *,
        timeout: int,
        pattern: str,
        otp_sent_at: Optional[float],
        target_email: Optional[str],
        mailbox_email: str,
    ) -> Optional[str]:
        start = time.time()
        target_email_lower = str(target_email or mailbox_email or "").strip().lower()
        search_applied = self._prepare_mailbox_search(page, target_email_lower)
        self._debug_event(
            f"浏览器轮询启动 target={target_email_lower or mailbox_email} timeout={timeout}s "
            f"search_applied={'yes' if search_applied else 'no'}"
        )
        round_idx = 0

        while time.time() - start < timeout:
            round_idx += 1
            page.wait_for_timeout(1500)
            self._click_first(
                page,
                [
                    'span:has-text("OpenAI")',
                    'a:has-text("OpenAI")',
                    '[title*="OpenAI" i]',
                    '[aria-label*="OpenAI" i]',
                ],
            )
            page.wait_for_timeout(1200)
            page_text = self._extract_visible_text(page)
            page_text_lower = page_text.lower()
            error_page = ("chrome-error://" in str(getattr(page, "url", "") or "").lower()) or ("err_timed_out" in page_text_lower)
            alias_visible = (
                (not target_email_lower)
                or (target_email_lower in page_text_lower)
                or search_applied
            )
            if "openai" in page_text_lower and alias_visible:
                code = self._extract_otp(page_text, pattern)
                if code:
                    self._debug_event(
                        f"浏览器轮询命中 OpenAI 文本 source_page={str(getattr(page, 'url', '') or '')[:120]} code_candidate={code}"
                    )
                    deduped = self._dedupe_code(target_email_lower or mailbox_email, code, otp_sent_at)
                    if deduped:
                        return deduped
            if round_idx == 1 or round_idx % 5 == 0:
                snippet = re.sub(r"\s+", " ", page_text)[:180]
                self._debug_event(
                    f"浏览器轮询第 {round_idx} 轮：alias_visible={'yes' if alias_visible else 'no'} "
                    f"openai_visible={'yes' if 'openai' in page_text_lower else 'no'} snippet={snippet}"
                )
            if error_page:
                self._debug_event("浏览器轮询检测到超时/错误页，尝试直接回到 Yahoo Inbox")
                try:
                    page.goto(self.MAILBOX_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            else:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    try:
                        page.goto(self.MAILBOX_URL, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
            if search_applied:
                self._prepare_mailbox_search(page, target_email_lower)
            time.sleep(self.config["poll_interval"])
        return None

    def _get_verification_code_via_imap(
        self,
        mailbox_email: str,
        password_value: str,
        timeout: int,
        pattern: str,
        otp_sent_at: Optional[float],
        target_email: Optional[str] = None,
    ) -> Optional[str]:
        start = time.time()
        mailbox = None
        seen_ids: set[str] = set()
        round_idx = 0
        self._debug_event(
            f"IMAP 轮询启动 mailbox={mailbox_email} target={str(target_email or mailbox_email or '').strip().lower()} "
            f"timeout={timeout}s socket_timeout={self.config.get('imap_socket_timeout')}"
        )
        try:
            mailbox = self._imap_connect(mailbox_email, password_value)
            mailbox.select("INBOX")
            while time.time() - start < timeout:
                round_idx += 1
                message_ids: List[bytes] = []
                for criteria in ("UNSEEN", "ALL"):
                    status, data = mailbox.search(None, criteria)
                    if status != "OK" or not data:
                        continue
                    message_ids = [raw_id for raw_id in reversed(data[0].split()[-30:]) if raw_id]
                    if message_ids:
                        if round_idx == 1 or round_idx % 5 == 0:
                            self._debug_event(
                                f"IMAP 第 {round_idx} 轮 search={criteria} 命中 {len(message_ids)} 封候选邮件"
                            )
                        break
                if not message_ids:
                    if round_idx == 1 or round_idx % 5 == 0:
                        self._debug_event(f"IMAP 第 {round_idx} 轮未发现候选邮件")
                    time.sleep(self.config["poll_interval"])
                    continue

                for raw_id in message_ids:
                    mail_id = raw_id.decode(errors="ignore")
                    if mail_id in seen_ids:
                        continue
                    seen_ids.add(mail_id)
                    try:
                        fetch_status, payload = mailbox.fetch(raw_id, "(RFC822)")
                    except (TimeoutError, socket.timeout, OSError, imaplib.IMAP4.abort) as fetch_exc:
                        self._debug_event(f"IMAP fetch message_id={mail_id} 异常: {fetch_exc}")
                        try:
                            mailbox.logout()
                        except Exception:
                            pass
                        mailbox = self._imap_connect(mailbox_email, password_value)
                        mailbox.select("INBOX")
                        break
                    if fetch_status != "OK" or not payload:
                        continue
                    message = email.message_from_bytes(payload[0][1])
                    sender = self._decode_header_value(message.get("From", ""))
                    subject = self._decode_header_value(message.get("Subject", ""))
                    body = self._extract_mail_body(message)
                    recipient_blob = self._extract_message_addresses(message)
                    target_email_lower = str(target_email or mailbox_email or "").strip().lower()
                    recipient_match = (not target_email_lower) or (target_email_lower in recipient_blob)
                    recent_match = self._message_is_recent(message, otp_sent_at)
                    if not self._is_openai_mail(sender, subject, body):
                        continue
                    if target_email_lower and not recipient_match and not recent_match:
                        self._debug_event(
                            f"IMAP 命中 OpenAI 邮件但收件人/时间不匹配 message_id={mail_id} "
                            f"recipient_match={'yes' if recipient_match else 'no'} recent_match={'yes' if recent_match else 'no'}"
                        )
                        continue
                    code = self._extract_otp(body, pattern)
                    if code:
                        self._debug_event(
                            f"IMAP 命中 OpenAI 验证码 message_id={mail_id} sender={sender[:80]} subject={subject[:120]} code_candidate={code}"
                        )
                        return self._dedupe_code(target_email_lower or mailbox_email, code, otp_sent_at)
                time.sleep(self.config["poll_interval"])
        except Exception as exc:
            self._debug_event(f"IMAP 轮询异常: {exc}")
            logger.debug("Yahoo IMAP OTP polling failed: %s", exc)
        finally:
            if mailbox:
                try:
                    mailbox.logout()
                except Exception:
                    pass
        return None

    def _extract_mail_body(self, message_obj) -> str:
        chunks: List[str] = []
        if message_obj.is_multipart():
            for part in message_obj.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_content_type() not in ("text/plain", "text/html"):
                    continue
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace") if payload else ""
                except Exception:
                    text = ""
                if part.get_content_type() == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                chunks.append(text)
        else:
            try:
                payload = message_obj.get_payload(decode=True)
                charset = message_obj.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace") if payload else ""
            except Exception:
                text = ""
            if "html" in str(message_obj.get_content_type() or "").lower():
                text = re.sub(r"<[^>]+>", " ", text)
            chunks.append(text)
        return "\n".join(part for part in chunks if part).strip()

    def _extract_message_addresses(self, message_obj) -> str:
        header_names = (
            "To",
            "Delivered-To",
            "X-Original-To",
            "Cc",
            "Bcc",
            "Envelope-To",
        )
        values: List[str] = []
        for name in header_names:
            raw = self._decode_header_value(message_obj.get(name, ""))
            if raw:
                values.append(raw)
        return "\n".join(values).lower()

    def _dedupe_code(self, email_value: str, code: str, otp_sent_at: Optional[float]) -> Optional[str]:
        key = str(email_value or "").strip().lower()
        if key not in self._used_codes:
            self._used_codes[key] = set()
        if otp_sent_at:
            marker = int(float(otp_sent_at))
            if self._used_code_stage_marker.get(key) != marker:
                self._used_codes[key].clear()
                self._used_code_stage_marker[key] = marker
        if code in self._used_codes[key]:
            return None
        self._used_codes[key].add(code)
        return code

    def _get_verification_code_via_browser(
        self,
        mailbox_email: str,
        password_value: str,
        timeout: int,
        pattern: str,
        otp_sent_at: Optional[float],
        target_email: Optional[str] = None,
    ) -> Optional[str]:
        playwright_handle = None
        browser = None
        page = None
        try:
            playwright_handle, browser, _context, page = self._login_yahoo_browser(
                email_value=mailbox_email,
                password_value=password_value,
                open_mailbox=True,
            )
            self._debug_event("新启 Yahoo 浏览器轮询 OTP（Roxy / IMAP 未成功后兜底）")
            return self._poll_openai_code_from_page(
                page,
                timeout=timeout,
                pattern=pattern,
                otp_sent_at=otp_sent_at,
                target_email=target_email,
                mailbox_email=mailbox_email,
            )
        except Exception as exc:
            self._debug_event(f"新启 Yahoo 浏览器轮询异常: {exc}")
            logger.debug("Yahoo browser OTP polling failed: %s", exc)
        finally:
            if page is not None and self._to_bool(self.config.get("roxy_mailbox_dump_on_fail"), default=True):
                artifact = self._dump_page_debug_artifacts(page, "otp_timeout_browser_fallback")
                if artifact:
                    self._debug_event(f"浏览器兜底轮询失败，已输出页面证据: {artifact.get('json')}")
            if playwright_handle:
                self._close_browser_session(playwright_handle, browser)
        return None

    def _get_verification_code_via_roxy_browser(
        self,
        ws_endpoint: str,
        timeout: int,
        pattern: str,
        otp_sent_at: Optional[float],
        target_email: Optional[str],
        mailbox_email: str,
    ) -> Optional[str]:
        sync_playwright = self._ensure_playwright()
        playwright_ctx = sync_playwright()
        page = None
        try:
            pw = playwright_ctx.__enter__()
            browser = pw.chromium.connect_over_cdp(str(ws_endpoint or "").strip())
            _context, page = self._select_existing_mail_page(browser)
            page.set_default_timeout(max(30000, int(self.config.get("timeout") or 30) * 1000))
            current_url = str(getattr(page, "url", "") or "")
            if "mail.yahoo.com" not in current_url:
                page.goto(self.MAILBOX_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1800)
            self._debug_event(
                f"优先使用 Roxy 已登录窗口轮询 OTP ws={str(ws_endpoint)[:120]} current_url={str(getattr(page, 'url', '') or '')[:120]}"
            )
            return self._poll_openai_code_from_page(
                page,
                timeout=timeout,
                pattern=pattern,
                otp_sent_at=otp_sent_at,
                target_email=target_email,
                mailbox_email=mailbox_email,
            )
        except Exception as exc:
            self._debug_event(f"Roxy 已登录窗口轮询异常: {exc}")
            logger.debug("Yahoo Roxy browser OTP polling failed: %s", exc)
        finally:
            if page is not None and self._to_bool(self.config.get("roxy_mailbox_dump_on_fail"), default=True):
                artifact = self._dump_page_debug_artifacts(page, "otp_timeout_roxy_mailbox")
                if artifact:
                    self._debug_event(f"Roxy mailbox 轮询失败，已输出页面证据: {artifact.get('json')}")
            try:
                playwright_ctx.__exit__(None, None, None)
            except Exception:
                pass
        return None

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        request_config = {**self.config, **(config or {})}
        existing_email = str(request_config.get("email") or "").strip().lower()
        existing_password = str(request_config.get("password") or "").strip()
        existing_app_password = str(request_config.get("app_password") or "").strip()
        parent_email = str(request_config.get("parent_email") or self.config.get("parent_email") or "").strip().lower()
        parent_password = str(request_config.get("parent_password") or self.config.get("parent_password") or "").strip()
        parent_app_password = str(request_config.get("parent_app_password") or self.config.get("parent_app_password") or "").strip()

        if existing_email:
            if not existing_password:
                raise EmailServiceError("Yahoo 服务配置了 email，但缺少登录 password")
            account_info = {
                "email": existing_email,
                "service_id": existing_email,
                "id": existing_email,
                "password": existing_password,
                "app_password": existing_app_password,
                "mode": "existing",
                "created_at": time.time(),
            }
            self._cache_account(account_info)
            self.update_status(True)
            return account_info

        self.config["parent_email"] = parent_email
        self.config["parent_password"] = parent_password
        self.config["parent_app_password"] = parent_app_password
        if not parent_email:
            raise EmailServiceError("Yahoo 母号模式缺少 parent_email，无法自动创建子邮箱")
        if not parent_app_password and not parent_password:
            raise EmailServiceError("Yahoo 母号模式至少需要 parent_app_password 或 parent_password 之一")

        account_info = self._create_alias_with_parent_mailbox()
        self.update_status(True)
        return account_info

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        account = self._get_cached_account(email_value=email, email_id=email_id)
        target_email = str((account or {}).get("email") or self.config.get("email") or email or "").strip().lower()
        mailbox_owner_email = str(
            (account or {}).get("mailbox_owner_email")
            or self.config.get("parent_email")
            or target_email
            or ""
        ).strip().lower()
        mailbox_owner_password = str(
            (account or {}).get("mailbox_owner_password")
            or (account or {}).get("password")
            or self.config.get("parent_password")
            or self.config.get("password")
            or ""
        ).strip()
        mailbox_owner_app_password = str(
            (account or {}).get("mailbox_owner_app_password")
            or (account or {}).get("app_password")
            or self.config.get("parent_app_password")
            or self.config.get("app_password")
            or ""
        ).strip()
        roxy_ws_endpoint = str(
            (account or {}).get("roxy_ws_endpoint")
            or self.config.get("roxy_ws_endpoint")
            or ""
        ).strip()
        prefer_roxy_otp = self._to_bool(
            (account or {}).get("prefer_roxy_otp")
            if account and "prefer_roxy_otp" in account
            else self.config.get("prefer_roxy_otp"),
            default=True,
        )

        if not target_email:
            raise EmailServiceError("Yahoo 邮箱地址为空，无法轮询验证码")

        if roxy_ws_endpoint and prefer_roxy_otp:
            self._debug_event(
                f"OTP 获取顺序: roxy -> imap -> browser | target={target_email} mailbox_owner={mailbox_owner_email}"
            )
            code = self._get_verification_code_via_roxy_browser(
                ws_endpoint=roxy_ws_endpoint,
                timeout=timeout,
                pattern=pattern,
                otp_sent_at=otp_sent_at,
                target_email=target_email,
                mailbox_email=mailbox_owner_email,
            )
            if code:
                self.update_status(True)
                return code

        if mailbox_owner_app_password:
            code = self._get_verification_code_via_imap(
                mailbox_email=mailbox_owner_email,
                password_value=mailbox_owner_app_password,
                timeout=timeout,
                pattern=pattern,
                otp_sent_at=otp_sent_at,
                target_email=target_email,
            )
            if code:
                self.update_status(True)
                return code

        if mailbox_owner_password:
            code = self._get_verification_code_via_browser(
                mailbox_email=mailbox_owner_email,
                password_value=mailbox_owner_password,
                timeout=timeout,
                pattern=pattern,
                otp_sent_at=otp_sent_at,
                target_email=target_email,
            )
            if code:
                self.update_status(True)
                return code

        if roxy_ws_endpoint and not prefer_roxy_otp:
            self._debug_event(
                f"OTP 获取顺序: imap -> browser -> roxy | target={target_email} mailbox_owner={mailbox_owner_email}"
            )
            code = self._get_verification_code_via_roxy_browser(
                ws_endpoint=roxy_ws_endpoint,
                timeout=timeout,
                pattern=pattern,
                otp_sent_at=otp_sent_at,
                target_email=target_email,
                mailbox_email=mailbox_owner_email,
            )
            if code:
                self.update_status(True)
                return code

        self.update_status(False, EmailServiceError("Yahoo 验证码轮询失败"))
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        if self._accounts_by_email:
            return list(self._accounts_by_email.values())
        if self.config.get("email"):
            return [
                {
                    "id": self.config["email"],
                    "service_id": self.config["email"],
                    "email": self.config["email"],
                }
            ]
        return []

    def delete_email(self, email_id: str) -> bool:
        cached = self._get_cached_account(email_value=email_id, email_id=email_id)
        if not cached:
            return False
        email_value = str(cached.get("email") or "").strip().lower()
        service_id = str(cached.get("service_id") or email_value).strip()
        self._accounts_by_email.pop(email_value, None)
        self._accounts_by_id.pop(service_id, None)
        return True

    def check_health(self) -> bool:
        parent_email, parent_password, parent_app_password = self._get_parent_seed_credentials()
        email_value = str(self.config.get("email") or "").strip().lower()
        password_value = str(self.config.get("password") or "").strip()
        app_password_value = str(self.config.get("app_password") or "").strip()

        try:
            if email_value and app_password_value:
                mailbox = self._imap_connect(email_value, app_password_value)
                try:
                    status, _ = mailbox.select("INBOX")
                    success = status == "OK"
                finally:
                    mailbox.logout()
                self.update_status(success)
                return success

            if email_value and password_value:
                playwright_handle = None
                browser = None
                try:
                    playwright_handle, browser, _context, _page = self._login_yahoo_browser(
                        email_value=email_value,
                        password_value=password_value,
                        open_mailbox=False,
                    )
                    self.update_status(True)
                    return True
                finally:
                    if playwright_handle:
                        self._close_browser_session(playwright_handle, browser)

            if parent_email and parent_app_password:
                mailbox = self._imap_connect(parent_email, parent_app_password)
                try:
                    status, _ = mailbox.select("INBOX")
                    success = status == "OK"
                finally:
                    mailbox.logout()
                self.update_status(success)
                return success

            if parent_email and parent_password:
                playwright_handle = None
                browser = None
                try:
                    playwright_handle, browser, _context, _page = self._login_yahoo_browser(
                        email_value=parent_email,
                        password_value=parent_password,
                        open_mailbox=False,
                    )
                    self.update_status(True)
                    return True
                finally:
                    if playwright_handle:
                        self._close_browser_session(playwright_handle, browser)

            if not email_value and not parent_email:
                raise EmailServiceError("Yahoo 服务未配置固定子邮箱，也未配置母号邮箱")

            # No fixed child account configured yet: treat a valid headless environment as signup-ready.
            self._ensure_playwright()
            self.update_status(True)
            return True
        except Exception as exc:
            logger.warning("Yahoo health check failed: %s", exc)
            self.update_status(False, exc)
            return False

    def get_service_info(self) -> Dict[str, Any]:
        parent_email, _parent_password, parent_app_password = self._get_parent_seed_credentials()
        child_email = self.config.get("email") or ""
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "email": child_email,
            "parent_email": parent_email,
            "domain": self.config.get("domain") or "yahoo.com",
            "headless": bool(self.config.get("headless")),
            "has_app_password": bool(self.config.get("app_password")),
            "has_parent_app_password": bool(parent_app_password),
            "mode": "fixed_child" if child_email else "parent_alias",
            "signup_ready": bool(parent_email) if not child_email else False,
            "status": self.status.value,
        }

    def _has_fixed_inbox_credentials(self, email_value: str, password_value: str, app_password_value: str) -> bool:
        return bool(email_value and (password_value or app_password_value))

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """默认主流程强制走母号 alias；固定 Yahoo 收件箱仅保留为兼容模式。"""
        request_config = {**self.config, **(config or {})}
        existing_email = str(request_config.get("email") or "").strip().lower()
        existing_password = str(request_config.get("password") or "").strip()
        existing_app_password = str(request_config.get("app_password") or "").strip()
        parent_email = str(request_config.get("parent_email") or self.config.get("parent_email") or "").strip().lower()
        parent_password = str(request_config.get("parent_password") or self.config.get("parent_password") or "").strip()
        parent_app_password = str(request_config.get("parent_app_password") or self.config.get("parent_app_password") or "").strip()

        if existing_email:
            if not self._has_fixed_inbox_credentials(existing_email, existing_password, existing_app_password):
                raise EmailServiceError("Yahoo 固定收件箱模式至少需要 password 或 app_password 之一")
            account_info = {
                "email": existing_email,
                "service_id": existing_email,
                "id": existing_email,
                "password": existing_password,
                "app_password": existing_app_password,
                "mode": "existing",
                "created_at": time.time(),
            }
            self._cache_account(account_info)
            self.update_status(True)
            return account_info

        self.config["parent_email"] = parent_email
        self.config["parent_password"] = parent_password
        self.config["parent_app_password"] = parent_app_password
        if not parent_email:
            raise EmailServiceError("Yahoo 母号 alias 模式缺少 parent_email，无法创建临时子地址")
        if not parent_password:
            raise EmailServiceError("Yahoo 母号 alias 模式需要 parent_password，才能登录母号创建临时子地址")

        account_info = self._create_alias_with_parent_mailbox()
        self.update_status(True)
        return account_info

    def check_health(self) -> bool:
        """alias 主流程需能真实登录母号；固定 Yahoo 收件箱允许密码或 IMAP 授权码二选一。"""
        parent_email, parent_password, parent_app_password = self._get_parent_seed_credentials()
        email_value = str(self.config.get("email") or "").strip().lower()
        password_value = str(self.config.get("password") or "").strip()
        app_password_value = str(self.config.get("app_password") or "").strip()

        try:
            if email_value and app_password_value:
                mailbox = self._imap_connect(email_value, app_password_value)
                try:
                    status, _ = mailbox.select("INBOX")
                    success = status == "OK"
                finally:
                    mailbox.logout()
                self.update_status(success)
                return success

            if email_value and password_value:
                playwright_handle = None
                browser = None
                try:
                    playwright_handle, browser, _context, _page = self._login_yahoo_browser(
                        email_value=email_value,
                        password_value=password_value,
                        open_mailbox=False,
                    )
                    self.update_status(True)
                    return True
                finally:
                    if playwright_handle:
                        self._close_browser_session(playwright_handle, browser)

            if parent_email and parent_password:
                playwright_handle = None
                browser = None
                try:
                    playwright_handle, browser, _context, _page = self._login_yahoo_browser(
                        email_value=parent_email,
                        password_value=parent_password,
                        open_mailbox=False,
                    )
                    self.update_status(True)
                    return True
                finally:
                    if playwright_handle:
                        self._close_browser_session(playwright_handle, browser)

            if not email_value and not parent_email:
                raise EmailServiceError("Yahoo 服务未配置固定收件箱，也未配置母号邮箱")

            self.update_status(False, EmailServiceError("Yahoo alias 模式需要母号登录密码，当前配置无法创建临时子地址"))
            return False
        except Exception as exc:
            logger.warning("Yahoo health check failed: %s", exc)
            self.update_status(False, exc)
            return False

    def get_service_info(self) -> Dict[str, Any]:
        parent_email, parent_password, parent_app_password = self._get_parent_seed_credentials()
        child_email = self.config.get("email") or ""
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "email": child_email,
            "parent_email": parent_email,
            "domain": self.config.get("domain") or "yahoo.com",
            "headless": bool(self.config.get("headless")),
            "has_app_password": bool(self.config.get("app_password")),
            "has_parent_app_password": bool(parent_app_password),
            "mode": "fixed_child" if child_email else "parent_alias",
            "signup_ready": bool(parent_email and parent_password) if not child_email else False,
            "status": self.status.value,
        }
