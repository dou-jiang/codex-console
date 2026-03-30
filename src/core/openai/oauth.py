"""
OpenAI OAuth 授权模块
从 main.py 中提取的 OAuth 相关函数
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Optional

from curl_cffi import requests as cffi_requests

from ...config.constants import (
    OAUTH_CLIENT_ID,
    OAUTH_AUTH_URL,
    OAUTH_TOKEN_URL,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
)


def _b64url_no_pad(raw: bytes) -> str:
    """Base64 URL 编码（无填充）"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sha256_b64url_no_pad(s: str) -> str:
    """SHA256 哈希后 Base64 URL 编码"""
    return _b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())


def _random_state(nbytes: int = 16) -> str:
    """生成随机 state"""
    return secrets.token_urlsafe(nbytes)


def _pkce_verifier() -> str:
    """生成 PKCE code_verifier"""
    return secrets.token_urlsafe(64)


def _parse_callback_url(callback_url: str) -> Dict[str, str]:
    """解析回调 URL"""
    candidate = callback_url.strip()
    if not candidate:
        return {"code": "", "state": "", "error": "", "error_description": ""}

    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = f"http://localhost{candidate}"
        elif any(ch in candidate for ch in "/?#") or ":" in candidate:
            candidate = f"http://{candidate}"
        elif "=" in candidate:
            candidate = f"http://localhost/?{candidate}"

    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)

    for key, values in fragment.items():
        if key not in query or not query[key] or not (query[key][0] or "").strip():
            query[key] = values

    def get1(k: str) -> str:
        v = query.get(k, [""])
        return (v[0] or "").strip()

    code = get1("code")
    state = get1("state")
    error = get1("error")
    error_description = get1("error_description")

    if code and not state and "#" in code:
        code, state = code.split("#", 1)

    if not error and error_description:
        error, error_description = error_description, ""

    return {
        "code": code,
        "state": state,
        "error": error,
        "error_description": error_description,
    }


def _jwt_claims_no_verify(id_token: str) -> Dict[str, Any]:
    """解析 JWT ID Token（不验证签名）"""
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try:
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _decode_jwt_segment(seg: str) -> Dict[str, Any]:
    """解码 JWT 片段"""
    raw = (seg or "").strip()
    if not raw:
        return {}
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    try:
        decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _to_int(v: Any) -> int:
    """转换为整数"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _looks_like_organization_id(value: Any) -> bool:
    """判断是否为 OpenAI organization/workspace ID。"""
    text = str(value or "").strip()
    return text.startswith("org-")


def _extract_organization_id_from_items(items: Any) -> str:
    """从 organizations/workspaces 列表中提取 organization_id。"""
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        organization_id = str(item.get("id") or "").strip()
        if _looks_like_organization_id(organization_id):
            return organization_id
    return ""


def _extract_default_organization_id(*payloads: Any) -> str:
    """
    从多个 payload 中提取 organization_id。

    某些 OAuth 响应会把默认组织混在 workspace/default_workspace 字段里，
    这里统一做一层归一化，避免后续把 account_id 错当 workspace_id。
    """
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in (
            "organization_id",
            "organizationId",
            "default_organization_id",
            "defaultOrganizationId",
            "default_workspace_id",
            "defaultWorkspaceId",
            "workspace_id",
            "workspaceId",
            "id",
        ):
            organization_id = str(payload.get(key) or "").strip()
            if _looks_like_organization_id(organization_id):
                return organization_id

        organization_id = _extract_organization_id_from_items(payload.get("organizations"))
        if organization_id:
            return organization_id

        account_payload = payload.get("account")
        if isinstance(account_payload, dict):
            organization_id = _extract_default_organization_id(account_payload)
            if organization_id:
                return organization_id

        auth_payload = payload.get("https://api.openai.com/auth")
        if isinstance(auth_payload, dict):
            organization_id = _extract_default_organization_id(auth_payload)
            if organization_id:
                return organization_id
    return ""


def _resolve_workspace_id(
    *payloads: Any,
    account_id: str = "",
    organization_id: str = "",
) -> str:
    """
    从多个 payload 中解析 workspace_id，并修复 workspace_id 被 account_id 污染的情况。
    """
    normalized_account_id = str(account_id or "").strip()
    normalized_organization_id = str(organization_id or "").strip()

    def _iter_workspace_candidates(payload: Any):
        if not isinstance(payload, dict):
            return
        for key in (
            "workspace_id",
            "workspaceId",
            "default_workspace_id",
            "defaultWorkspaceId",
            "id",
        ):
            value = str(payload.get(key) or "").strip()
            if value:
                yield value

        workspace_payload = payload.get("workspace")
        if isinstance(workspace_payload, dict):
            workspace_id = str(workspace_payload.get("id") or "").strip()
            if workspace_id:
                yield workspace_id

        workspaces = payload.get("workspaces")
        if isinstance(workspaces, list):
            for item in workspaces:
                if not isinstance(item, dict):
                    continue
                workspace_id = str(item.get("id") or "").strip()
                if workspace_id:
                    yield workspace_id

        account_payload = payload.get("account")
        if isinstance(account_payload, dict):
            yield from _iter_workspace_candidates(account_payload)

    for payload in payloads:
        for candidate in _iter_workspace_candidates(payload):
            if (
                normalized_organization_id
                and normalized_account_id
                and candidate == normalized_account_id
                and normalized_organization_id != normalized_account_id
            ):
                continue
            return candidate

    if normalized_organization_id and normalized_organization_id != normalized_account_id:
        return normalized_organization_id
    return ""


def _post_form(
    url: str,
    data: Dict[str, str],
    timeout: int = 30,
    proxy_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    发送 POST 表单请求

    Args:
        url: 请求 URL
        data: 表单数据
        timeout: 超时时间
        proxy_url: 代理 URL

    Returns:
        响应 JSON 数据
    """
    # 构建代理配置
    proxies = None
    if proxy_url:
        proxies = {
            "http": proxy_url,
            "https": proxy_url,
        }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    try:
        # 使用 curl_cffi 发送请求，支持代理和浏览器指纹
        response = cffi_requests.post(
            url,
            data=data,
            headers=headers,
            timeout=timeout,
            proxies=proxies,
            impersonate="chrome"
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"token exchange failed: {response.status_code}: {response.text}"
            )

        return response.json()

    except cffi_requests.RequestsError as e:
        raise RuntimeError(f"token exchange failed: network error: {e}") from e


@dataclass(frozen=True)
class OAuthStart:
    """OAuth 开始信息"""
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str


def generate_oauth_url(
    *,
    redirect_uri: str = OAUTH_REDIRECT_URI,
    scope: str = OAUTH_SCOPE,
    client_id: str = OAUTH_CLIENT_ID
) -> OAuthStart:
    """
    生成 OAuth 授权 URL

    Args:
        redirect_uri: 回调地址
        scope: 权限范围
        client_id: OpenAI Client ID

    Returns:
        OAuthStart 对象，包含授权 URL 和必要参数
    """
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    auth_url = f"{OAUTH_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return OAuthStart(
        auth_url=auth_url,
        state=state,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )


def submit_callback_url(
    *,
    callback_url: str,
    expected_state: str,
    code_verifier: str,
    redirect_uri: str = OAUTH_REDIRECT_URI,
    client_id: str = OAUTH_CLIENT_ID,
    token_url: str = OAUTH_TOKEN_URL,
    proxy_url: Optional[str] = None
) -> str:
    """
    处理 OAuth 回调 URL，获取访问令牌

    Args:
        callback_url: 回调 URL
        expected_state: 预期的 state 值
        code_verifier: PKCE code_verifier
        redirect_uri: 回调地址
        client_id: OpenAI Client ID
        token_url: Token 交换地址
        proxy_url: 代理 URL

    Returns:
        包含访问令牌等信息的 JSON 字符串

    Raises:
        RuntimeError: OAuth 错误
        ValueError: 缺少必要参数或 state 不匹配
    """
    cb = _parse_callback_url(callback_url)
    if cb["error"]:
        desc = cb["error_description"]
        raise RuntimeError(f"oauth error: {cb['error']}: {desc}".strip())

    if not cb["code"]:
        raise ValueError("callback url missing ?code=")
    if not cb["state"]:
        raise ValueError("callback url missing ?state=")
    if cb["state"] != expected_state:
        raise ValueError("state mismatch")

    token_resp = _post_form(
        token_url,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": cb["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        proxy_url=proxy_url
    )

    access_token = (token_resp.get("access_token") or "").strip()
    refresh_token = (token_resp.get("refresh_token") or "").strip()
    id_token = (token_resp.get("id_token") or "").strip()
    expires_in = _to_int(token_resp.get("expires_in"))

    claims = _jwt_claims_no_verify(id_token)
    email = str(claims.get("email") or "").strip()
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()
    organization_id = _extract_default_organization_id(token_resp, claims, auth_claims)
    workspace_id = _resolve_workspace_id(
        token_resp,
        claims,
        auth_claims,
        account_id=account_id,
        organization_id=organization_id,
    )

    now = int(time.time())
    expired_rfc3339 = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0))
    )
    now_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    config = {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "last_refresh": now_rfc3339,
        "email": email,
        "type": "codex",
        "expired": expired_rfc3339,
    }

    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


class OAuthManager:
    """OAuth 管理器"""

    def __init__(
        self,
        client_id: str = OAUTH_CLIENT_ID,
        auth_url: str = OAUTH_AUTH_URL,
        token_url: str = OAUTH_TOKEN_URL,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        scope: str = OAUTH_SCOPE,
        proxy_url: Optional[str] = None
    ):
        self.client_id = client_id
        self.auth_url = auth_url
        self.token_url = token_url
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.proxy_url = proxy_url

    def start_oauth(self) -> OAuthStart:
        """开始 OAuth 流程"""
        return generate_oauth_url(
            redirect_uri=self.redirect_uri,
            scope=self.scope,
            client_id=self.client_id
        )

    def handle_callback(
        self,
        callback_url: str,
        expected_state: str,
        code_verifier: str
    ) -> Dict[str, Any]:
        """处理 OAuth 回调"""
        result_json = submit_callback_url(
            callback_url=callback_url,
            expected_state=expected_state,
            code_verifier=code_verifier,
            redirect_uri=self.redirect_uri,
            client_id=self.client_id,
            token_url=self.token_url,
            proxy_url=self.proxy_url
        )
        return json.loads(result_json)

    def extract_account_info(self, id_token: str) -> Dict[str, Any]:
        """从 ID Token 中提取账户和组织信息"""
        claims = _jwt_claims_no_verify(id_token)
        email = str(claims.get("email") or "").strip()
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()
        organization_id = _extract_default_organization_id(claims, auth_claims)
        workspace_id = _resolve_workspace_id(
            claims,
            auth_claims,
            account_id=account_id,
            organization_id=organization_id,
        )

        return {
            "email": email,
            "account_id": account_id,
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "claims": claims
        }
