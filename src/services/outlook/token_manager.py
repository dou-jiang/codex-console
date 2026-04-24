"""
Token 管理器
支持多个 Microsoft Token 端点，自动选择合适的端点
"""

import json
import logging
import threading
import time
from typing import Dict, Optional, Any

from curl_cffi import requests as _requests

from .base import ProviderType, TokenEndpoint, TokenInfo
from .account import OutlookAccount


logger = logging.getLogger(__name__)


# 各提供者的 Scope 配置
PROVIDER_SCOPES = {
    ProviderType.IMAP_OLD: "",  # 旧版 IMAP 不需要特定 scope
    ProviderType.IMAP_NEW: "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    ProviderType.GRAPH_API: "https://graph.microsoft.com/.default",
}

# 各提供者的 Token 端点
PROVIDER_TOKEN_URLS = {
    ProviderType.IMAP_OLD: TokenEndpoint.LIVE.value,
    ProviderType.IMAP_NEW: TokenEndpoint.CONSUMERS.value,
    ProviderType.GRAPH_API: TokenEndpoint.COMMON.value,
}


class TokenManager:
    """
    Token 管理器
    支持多端点 Token 获取和缓存
    """

    # Token 缓存: key = (email, provider_type) -> TokenInfo
    _token_cache: Dict[tuple, TokenInfo] = {}
    _cache_lock = threading.Lock()

    # 默认超时时间
    DEFAULT_TIMEOUT = 30
    # Token 刷新提前时间（秒）
    REFRESH_BUFFER = 120

    def __init__(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
        proxy_url: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """
        初始化 Token 管理器

        Args:
            account: Outlook 账户
            provider_type: 提供者类型
            proxy_url: 代理 URL（可选）
            timeout: 请求超时时间
        """
        self.account = account
        self.provider_type = provider_type
        self.proxy_url = proxy_url
        self.timeout = timeout

        # 获取端点和 Scope
        self.token_url = PROVIDER_TOKEN_URLS.get(provider_type, TokenEndpoint.LIVE.value)
        self.scope = PROVIDER_SCOPES.get(provider_type, "")

    def get_cached_token(self) -> Optional[TokenInfo]:
        """获取缓存的 Token"""
        cache_key = (self.account.email.lower(), self.provider_type)
        with self._cache_lock:
            token = self._token_cache.get(cache_key)
            if token and not token.is_expired(self.REFRESH_BUFFER):
                return token
        return None

    def set_cached_token(self, token: TokenInfo):
        """缓存 Token"""
        cache_key = (self.account.email.lower(), self.provider_type)
        with self._cache_lock:
            self._token_cache[cache_key] = token

    def clear_cache(self):
        """清除缓存"""
        cache_key = (self.account.email.lower(), self.provider_type)
        with self._cache_lock:
            self._token_cache.pop(cache_key, None)

    def get_access_token(self, force_refresh: bool = False) -> Optional[str]:
        """
        获取 Access Token

        Args:
            force_refresh: 是否强制刷新

        Returns:
            Access Token 字符串，失败返回 None
        """
        # 检查缓存
        if not force_refresh:
            cached = self.get_cached_token()
            if cached:
                logger.debug(f"[{self.account.email}] 使用缓存的 Token ({self.provider_type.value})")
                return cached.access_token

        # 刷新 Token
        try:
            token = self._refresh_token()
            if token:
                self.set_cached_token(token)
                return token.access_token
        except Exception as e:
            logger.error(f"[{self.account.email}] 获取 Token 失败 ({self.provider_type.value}): {e}")

        return None

    def _refresh_token(self) -> Optional[TokenInfo]:
        """
        刷新 Token

        Returns:
            TokenInfo 对象，失败返回 None
        """
        if not self.account.client_id or not self.account.refresh_token:
            raise ValueError("缺少 client_id 或 refresh_token")

        logger.debug(f"[{self.account.email}] 正在刷新 Token ({self.provider_type.value})...")
        logger.debug(f"[{self.account.email}] Token URL: {self.token_url}")

        # 构建请求体
        data = {
            "client_id": self.account.client_id,
            "refresh_token": self.account.refresh_token,
            "grant_type": "refresh_token",
        }

        # 添加 Scope（如果需要）
        if self.scope:
            data["scope"] = self.scope

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

        proxies = None
        if self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}

        try:
            resp = _requests.post(
                self.token_url,
                data=data,
                headers=headers,
                proxies=proxies,
                timeout=self.timeout,
                impersonate="chrome110",
            )

            if resp.status_code != 200:
                error_body = resp.text
                logger.error(f"[{self.account.email}] Token 刷新失败: HTTP {resp.status_code}")
                logger.debug(f"[{self.account.email}] 错误响应: {error_body[:500]}")

                if "service abuse" in error_body.lower():
                    logger.warning(f"[{self.account.email}] 账号可能被封禁")
                elif "invalid_grant" in error_body.lower():
                    logger.warning(f"[{self.account.email}] Refresh Token 已失效")

                return None

            response_data = resp.json()

            # 解析响应
            token = TokenInfo.from_response(response_data, self.scope)
            logger.info(
                f"[{self.account.email}] Token 刷新成功 ({self.provider_type.value}), "
                f"有效期 {int(token.expires_at - time.time())} 秒"
            )
            return token

        except json.JSONDecodeError as e:
            logger.error(f"[{self.account.email}] JSON 解析错误: {e}")
            return None

        except Exception as e:
            logger.error(f"[{self.account.email}] 未知错误: {e}")
            return None

    @classmethod
    def clear_all_cache(cls):
        """清除所有 Token 缓存"""
        with cls._cache_lock:
            cls._token_cache.clear()
            logger.info("已清除所有 Token 缓存")

    @classmethod
    def get_cache_stats(cls) -> Dict[str, Any]:
        """获取缓存统计"""
        with cls._cache_lock:
            return {
                "cache_size": len(cls._token_cache),
                "entries": [
                    {
                        "email": key[0],
                        "provider": key[1].value,
                    }
                    for key in cls._token_cache.keys()
                ],
            }


def create_token_manager(
    account: OutlookAccount,
    provider_type: ProviderType,
    proxy_url: Optional[str] = None,
    timeout: int = TokenManager.DEFAULT_TIMEOUT,
) -> TokenManager:
    """
    创建 Token 管理器的工厂函数

    Args:
        account: Outlook 账户
        provider_type: 提供者类型
        proxy_url: 代理 URL
        timeout: 超时时间

    Returns:
        TokenManager 实例
    """
    return TokenManager(account, provider_type, proxy_url, timeout)
