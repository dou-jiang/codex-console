"""
Token 刷新模块
支持 Session Token 和 OAuth Refresh Token 两种刷新方式
"""

import logging
import json
import time
from http.cookies import SimpleCookie
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta

from curl_cffi import requests as cffi_requests

from ...config.settings import get_settings
from ...config.constants import AccountStatus, OAUTH_CLIENT_ID
from ...database.session import get_db
from ...database import crud
from ...database.models import Account

logger = logging.getLogger(__name__)


@dataclass
class TokenRefreshResult:
    """Token 刷新结果"""
    success: bool
    access_token: str = ""
    refresh_token: str = ""
    expires_at: Optional[datetime] = None
    error_message: str = ""


class TokenRefreshManager:
    """
    Token 刷新管理器
    支持两种刷新方式：
    1. Session Token 刷新（优先）
    2. OAuth Refresh Token 刷新
    """

    # OpenAI OAuth 端点
    SESSION_URL = "https://chatgpt.com/api/auth/session"
    TOKEN_URL = "https://auth.openai.com/oauth/token"

    def __init__(self, proxy_url: Optional[str] = None):
        """
        初始化 Token 刷新管理器

        Args:
            proxy_url: 代理 URL
        """
        self.proxy_url = proxy_url
        self.settings = get_settings()

    def _create_session(self) -> cffi_requests.Session:
        """创建 HTTP 会话"""
        session = cffi_requests.Session(impersonate="chrome120", proxy=self.proxy_url)
        return session

    @staticmethod
    def _extract_session_token_from_cookies(cookies: Optional[str]) -> Optional[str]:
        """从完整 Cookie 字符串中提取 __Secure-next-auth.session-token。"""
        text = str(cookies or "").strip()
        if not text:
            return None
        try:
            jar = SimpleCookie()
            jar.load(text)
            token = jar.get("__Secure-next-auth.session-token")
            value = token.value if token else None
            return value or None
        except Exception:
            return None

    def _create_direct_session(self) -> cffi_requests.Session:
        """创建直连会话（不走代理）。"""
        return cffi_requests.Session(impersonate="chrome120")

    def refresh_by_session_token(
        self,
        session_token: str,
        refresh_token: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> TokenRefreshResult:
        """
        使用 Session Token 刷新

        Args:
            session_token: 会话令牌
            refresh_token: 可选 OAuth Refresh Token（用于一次性会话令牌场景兜底）
            client_id: 可选 OAuth Client ID

        Returns:
            TokenRefreshResult: 刷新结果
        """
        result = TokenRefreshResult(success=False)

        try:
            def _request_once(session: cffi_requests.Session):
                session.cookies.set(
                    "__Secure-next-auth.session-token",
                    session_token,
                    domain=".chatgpt.com",
                    path="/"
                )
                return session.get(
                    self.SESSION_URL,
                    headers={
                        "accept": "application/json",
                        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    },
                    timeout=30
                )

            session = self._create_session()
            response = _request_once(session)

            # 某些一次性/异常 session_token 链路会返回 grant 错误，若提供了 refresh_token 则兜底走 OAuth 刷新
            if response.status_code >= 400:
                try:
                    error_payload = response.json()
                except Exception:
                    error_payload = {}

                error_text = str(error_payload.get("error") or "").lower()
                if (
                    response.status_code == 400
                    and refresh_token
                    and error_text in {"invalid_grant", "unsupported_grant_type", "invalid_request"}
                ):
                    oauth_client_id = client_id or self.settings.openai_client_id or OAUTH_CLIENT_ID
                    response = session.post(
                        self.TOKEN_URL,
                        data={
                            "client_id": oauth_client_id,
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                        },
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        },
                        timeout=30
                    )

            # 代理通道触发地区/风控时，自动回退直连重试一次
            if (
                response.status_code in (401, 403)
                and self.proxy_url
            ):
                body = (response.text or "")[:500].lower()
                if "unsupported_country_region_territory" in body or "request_forbidden" in body:
                    logger.warning("Session token 刷新触发地区限制，尝试直连重试")
                    direct_session = self._create_direct_session()
                    response = _request_once(direct_session)

            if response.status_code != 200:
                result.error_message = f"Session token 刷新失败: HTTP {response.status_code}"
                logger.warning(result.error_message)
                return result

            data = response.json()

            # 提取 access_token
            access_token = data.get("accessToken")
            if not access_token:
                result.error_message = "Session token 刷新失败: 未找到 accessToken"
                logger.warning(result.error_message)
                return result

            # 提取过期时间
            expires_at = None
            expires_str = data.get("expires")
            if expires_str:
                try:
                    expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                except:
                    pass

            result.success = True
            result.access_token = access_token
            result.expires_at = expires_at

            logger.info(f"Session token 刷新成功，过期时间: {expires_at}")
            return result

        except Exception as e:
            result.error_message = f"Session token 刷新异常: {str(e)}"
            logger.error(result.error_message)
            return result

    def refresh_by_oauth_token(
        self,
        refresh_token: str,
        client_id: Optional[str] = None
    ) -> TokenRefreshResult:
        """
        使用 OAuth Refresh Token 刷新

        Args:
            refresh_token: OAuth 刷新令牌
            client_id: OAuth Client ID

        Returns:
            TokenRefreshResult: 刷新结果
        """
        result = TokenRefreshResult(success=False)

        try:
            # 使用配置的 client_id 或默认值
            client_id = client_id or self.settings.openai_client_id

            # 构建请求体
            token_data = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "redirect_uri": self.settings.openai_redirect_uri
            }

            def _request_once(session: cffi_requests.Session):
                return session.post(
                    self.TOKEN_URL,
                    headers={
                        "content-type": "application/x-www-form-urlencoded",
                        "accept": "application/json"
                    },
                    data=token_data,
                    timeout=30
                )

            session = self._create_session()
            response = _request_once(session)

            # 典型场景：代理出口地区受限导致 403，改为直连再试一次
            if response.status_code == 403 and self.proxy_url:
                body = (response.text or "")[:500].lower()
                if "unsupported_country_region_territory" in body:
                    logger.warning("OAuth token 刷新触发地区限制，尝试直连重试")
                    direct_session = self._create_direct_session()
                    response = _request_once(direct_session)

            if response.status_code != 200:
                result.error_message = f"OAuth token 刷新失败: HTTP {response.status_code}"
                logger.warning(f"{result.error_message}, 响应: {response.text[:200]}")
                return result

            data = response.json()

            # 提取令牌
            access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token", refresh_token)
            expires_in = data.get("expires_in", 3600)

            if not access_token:
                result.error_message = "OAuth token 刷新失败: 未找到 access_token"
                logger.warning(result.error_message)
                return result

            # 计算过期时间
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

            result.success = True
            result.access_token = access_token
            result.refresh_token = new_refresh_token
            result.expires_at = expires_at

            logger.info(f"OAuth token 刷新成功，过期时间: {expires_at}")
            return result

        except Exception as e:
            result.error_message = f"OAuth token 刷新异常: {str(e)}"
            logger.error(result.error_message)
            return result

    def refresh_account(self, account: Account) -> TokenRefreshResult:
        """
        刷新账号的 Token

        优先级：
        1. Session Token 刷新
        2. OAuth Refresh Token 刷新

        Args:
            account: 账号对象

        Returns:
            TokenRefreshResult: 刷新结果
        """
        # 优先尝试 Session Token
        if account.session_token:
            logger.info(f"尝试使用 Session Token 刷新账号 {account.email}")
            result = self.refresh_by_session_token(
                account.session_token,
                refresh_token=account.refresh_token,
                client_id=account.client_id
            )
            if result.success:
                return result
            logger.warning(f"Session Token 刷新失败，尝试 OAuth 刷新")

        # 若 session_token 字段为空，但 cookies 里有 next-auth 会话，仍可尝试会话刷新
        cookie_session_token = self._extract_session_token_from_cookies(getattr(account, "cookies", None))
        if cookie_session_token:
            logger.info(f"尝试使用 Cookies 中的 Session Token 刷新账号 {account.email}")
            result = self.refresh_by_session_token(
                cookie_session_token,
                refresh_token=account.refresh_token,
                client_id=account.client_id
            )
            if result.success:
                return result
            logger.warning("Cookies Session Token 刷新失败，尝试 OAuth 刷新")

        # 尝试 OAuth Refresh Token
        if account.refresh_token:
            logger.info(f"尝试使用 OAuth Refresh Token 刷新账号 {account.email}")
            result = self.refresh_by_oauth_token(
                refresh_token=account.refresh_token,
                client_id=account.client_id
            )
            return result

        # 无可用刷新方式
        return TokenRefreshResult(
            success=False,
            error_message="账号没有可用的刷新方式（缺少 session_token 和 refresh_token）"
        )

    def validate_token(self, access_token: str, timeout_seconds: int = 30) -> Tuple[bool, Optional[str]]:
        """
        验证 Access Token 是否有效

        Args:
            access_token: 访问令牌
            timeout_seconds: 请求超时（秒）

        Returns:
            Tuple[bool, Optional[str]]: (是否有效, 错误信息)
        """
        try:
            session = self._create_session()

            # 调用 OpenAI API 验证 token
            response = session.get(
                "https://chatgpt.com/backend-api/me",
                headers={
                    "authorization": f"Bearer {access_token}",
                    "accept": "application/json"
                },
                timeout=max(5, int(timeout_seconds or 30))
            )

            if response.status_code == 200:
                return True, None
            elif response.status_code == 401:
                return False, "Token 无效（401）"
            elif response.status_code == 402:
                return False, "订阅受限（402）"
            elif response.status_code == 403:
                # 403 在当前业务里通常代表工作区/权限受限，但账号可继续使用。
                return True, None
            else:
                return False, f"验证失败: HTTP {response.status_code}"

        except Exception as e:
            return False, f"验证异常: {str(e)}"


def refresh_account_token(account_id: int, proxy_url: Optional[str] = None) -> TokenRefreshResult:
    """
    刷新指定账号的 Token 并更新数据库

    Args:
        account_id: 账号 ID
        proxy_url: 代理 URL

    Returns:
        TokenRefreshResult: 刷新结果
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return TokenRefreshResult(success=False, error_message="账号不存在")

        manager = TokenRefreshManager(proxy_url=proxy_url)
        result = manager.refresh_account(account)

        if result.success:
            # 更新数据库
            update_data = {
                "access_token": result.access_token,
                "last_refresh": datetime.utcnow()
            }

            if result.refresh_token:
                update_data["refresh_token"] = result.refresh_token

            if result.expires_at:
                update_data["expires_at"] = result.expires_at

            crud.update_account(db, account_id, **update_data)

        return result


def validate_account_token(
    account_id: int,
    proxy_url: Optional[str] = None,
    timeout_seconds: int = 30,
) -> Tuple[bool, Optional[str]]:
    """
    验证指定账号的 Token 是否有效

    Args:
        account_id: 账号 ID
        proxy_url: 代理 URL
        timeout_seconds: 验证请求超时（秒）

    Returns:
        Tuple[bool, Optional[str]]: (是否有效, 错误信息)
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return False, "账号不存在"

        if not account.access_token:
            # 无 Token 直接标记为 failed，便于账号管理筛选定位。
            if account.status != AccountStatus.FAILED.value:
                crud.update_account(db, account_id, status=AccountStatus.FAILED.value)
            return False, "账号没有 access_token"

        manager = TokenRefreshManager(proxy_url=proxy_url)
        is_valid, error = manager.validate_token(
            account.access_token,
            timeout_seconds=max(5, int(timeout_seconds or 30)),
        )

        # 验证后回写账号状态，确保列表状态与验证结果一致。
        error_text = str(error or "").lower()
        if is_valid:
            next_status = AccountStatus.ACTIVE.value
        elif (
            "402" in error_text
            or "payment required" in error_text
            or "订阅受限" in error_text
        ):
            # 402 -> 黄色（expired）
            next_status = AccountStatus.EXPIRED.value
        elif (
            "401" in error_text
            or "invalid" in error_text
            or "unauthorized" in error_text
            or "过期" in error_text
            or "expired" in error_text
        ):
            # 401 -> 红色（failed）
            next_status = AccountStatus.FAILED.value
        elif (
            "封禁" in error_text
            or "banned" in error_text
            or "forbidden" in error_text
        ):
            next_status = AccountStatus.BANNED.value
        else:
            next_status = AccountStatus.FAILED.value

        if account.status != next_status:
            crud.update_account(db, account_id, status=next_status)

        return is_valid, error
