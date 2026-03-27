"""Shared auth helpers for internal API and websocket access."""

from __future__ import annotations

import hmac
import hashlib

from fastapi import HTTPException, Request
from starlette.websockets import WebSocket

from src.config.settings import get_settings


def build_webui_auth_token(password: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).hexdigest()


def _resolve_access_password() -> str:
    return get_settings().webui_access_password.get_secret_value()


def _resolve_secret_key() -> str:
    return get_settings().webui_secret_key.get_secret_value()


def _header_password(headers) -> str:
    return str(headers.get("x-access-password") or "").strip()


def _bearer_password(headers) -> str:
    auth = str(headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def has_api_access_from_request(request: Request) -> bool:
    password = _resolve_access_password()
    if not password:
        return False

    cookie = request.cookies.get("webui_auth")
    expected = build_webui_auth_token(password, _resolve_secret_key())
    if cookie and hmac.compare_digest(cookie, expected):
        return True

    header_password = _header_password(request.headers) or _bearer_password(request.headers)
    return bool(header_password) and secrets_compare(header_password, password)


def has_api_access_from_websocket(websocket: WebSocket) -> bool:
    password = _resolve_access_password()
    if not password:
        return False

    cookie = websocket.cookies.get("webui_auth")
    expected = build_webui_auth_token(password, _resolve_secret_key())
    if cookie and hmac.compare_digest(cookie, expected):
        return True

    header_password = _header_password(websocket.headers) or _bearer_password(websocket.headers)
    return bool(header_password) and secrets_compare(header_password, password)


def secrets_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left or ""), str(right or ""))


def require_api_access(request: Request) -> None:
    if has_api_access_from_request(request):
        return
    raise HTTPException(status_code=401, detail="authentication required")
