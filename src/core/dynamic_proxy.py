"""
动态代理获取模块
支持通过外部 API 获取动态代理 URL
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def fetch_dynamic_proxy(api_url: str, api_key: str = "", api_key_header: str = "X-API-Key", result_field: str = "") -> Optional[str]:
    """
    从代理 API 获取代理 URL

    Args:
        api_url: 代理 API 地址，响应应为代理 URL 字符串或含代理 URL 的 JSON
        api_key: API 密钥（可选）
        api_key_header: API 密钥请求头名称
        result_field: 从 JSON 响应中提取代理 URL 的字段路径，支持点号分隔（如 "data.proxy"），留空则使用响应原文

    Returns:
        代理 URL 字符串（如 http://user:pass@host:port），失败返回 None
    """
    try:
        from curl_cffi import requests as cffi_requests

        headers = {}
        if api_key:
            headers[api_key_header] = api_key

        response = cffi_requests.get(
            api_url,
            headers=headers,
            timeout=10,
            impersonate="chrome110"
        )

        if response.status_code != 200:
            logger.warning(f"动态代理 API 返回错误状态码: {response.status_code}")
            return None

        text = response.text.strip()

        # 尝试解析 JSON
        if result_field or text.startswith("{") or text.startswith("["):
            try:
                import json
                data = json.loads(text)
                if result_field:
                    # 按点号路径逐层提取
                    for key in result_field.split("."):
                        if isinstance(data, dict):
                            data = data.get(key)
                        elif isinstance(data, list) and key.isdigit():
                            data = data[int(key)]
                        else:
                            data = None
                        if data is None:
                            break
                    proxy_url = str(data).strip() if data is not None else None
                else:
                    # 无指定字段，尝试常见键名
                    for key in ("proxy", "url", "proxy_url", "data", "ip"):
                        val = data.get(key) if isinstance(data, dict) else None
                        if val:
                            proxy_url = str(val).strip()
                            break
                    else:
                        proxy_url = text
            except (ValueError, AttributeError):
                proxy_url = text
        else:
            proxy_url = text

        if not proxy_url:
            logger.warning("动态代理 API 返回空代理 URL")
            return None

        # 若未包含协议头，默认加 http://
        if not re.match(r'^(http|socks5)://', proxy_url):
            proxy_url = "http://" + proxy_url

        logger.info(f"动态代理获取成功: {proxy_url[:40]}..." if len(proxy_url) > 40 else f"动态代理获取成功: {proxy_url}")
        return proxy_url

    except Exception as e:
        logger.error(f"获取动态代理失败: {e}")
        return None


def get_proxy_url_for_task() -> Optional[str]:
    """
    为注册任务获取代理 URL。
    优先使用动态代理（若启用），否则使用静态代理配置。

    Returns:
        代理 URL 或 None
    """
    from ..config.settings import get_settings
    settings = get_settings()

    # 优先使用动态代理
    if settings.proxy_dynamic_enabled and settings.proxy_dynamic_api_url:
        api_key = settings.proxy_dynamic_api_key.get_secret_value() if settings.proxy_dynamic_api_key else ""
        proxy_url = fetch_dynamic_proxy(
            api_url=settings.proxy_dynamic_api_url,
            api_key=api_key,
            api_key_header=settings.proxy_dynamic_api_key_header,
            result_field=settings.proxy_dynamic_result_field,
        )
        if proxy_url:
            return proxy_url
        logger.warning("动态代理获取失败，回退到静态代理")

    # 使用静态代理
    return settings.proxy_url
