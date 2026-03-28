"""
Web UI 统一鉴权与安全基线工具。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Tuple
from urllib.parse import quote

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse

from ..config.settings import get_settings

DEFAULT_WEBUI_ACCESS_PASSWORD = "admin123"
DEFAULT_WEBUI_SECRET_KEY = "your-secret-key-change-in-production"
# 临时开关：关闭“首次启动强制改密”。
# 恢复时改为 False 即可重新启用原有逻辑。
TEMP_DISABLE_SETUP_PASSWORD_ENFORCE = True


def _safe_value(value: str) -> str:
    return str(value or "").strip()


def build_auth_token(password: str, secret_key: str) -> str:
    secret = _safe_value(secret_key).encode("utf-8")
    pwd = _safe_value(password).encode("utf-8")
    return hmac.new(secret, pwd, hashlib.sha256).hexdigest()


def get_expected_auth_token() -> str:
    settings = get_settings()
    password = settings.webui_access_password.get_secret_value()
    secret_key = settings.webui_secret_key.get_secret_value()
    return build_auth_token(password, secret_key)


def is_default_security_config_active() -> bool:
    if TEMP_DISABLE_SETUP_PASSWORD_ENFORCE:
        return False

    settings = get_settings()
    password = _safe_value(settings.webui_access_password.get_secret_value())
    secret_key = _safe_value(settings.webui_secret_key.get_secret_value())
    return (
        not password
        or password == DEFAULT_WEBUI_ACCESS_PASSWORD
        or not secret_key
        or secret_key == DEFAULT_WEBUI_SECRET_KEY
    )


def build_setup_password_redirect() -> RedirectResponse:
    return RedirectResponse(url="/setup-password", status_code=302)


def build_login_redirect(request: Request) -> RedirectResponse:
    target = quote(request.url.path or "/", safe="/")
    return RedirectResponse(url=f"/login?next={target}", status_code=302)


def is_request_authenticated(request: Request) -> bool:
    cookie = request.cookies.get("webui_auth")
    expected = get_expected_auth_token()
    return bool(cookie) and secrets.compare_digest(cookie, expected)


def require_api_auth(request: Request) -> bool:
    if is_default_security_config_active():
        raise HTTPException(
            status_code=423,
            detail={
                "code": "password_change_required",
                "message": "首次启动请先访问 /setup-password 修改访问密码",
            },
        )
    if not is_request_authenticated(request):
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    return True


def is_websocket_authenticated(websocket: WebSocket) -> bool:
    cookie = websocket.cookies.get("webui_auth")
    expected = get_expected_auth_token()
    return bool(cookie) and secrets.compare_digest(cookie, expected)


def websocket_auth_failure() -> Tuple[int, str]:
    if is_default_security_config_active():
        return 4403, "password_change_required"
    return 4401, "unauthorized"
