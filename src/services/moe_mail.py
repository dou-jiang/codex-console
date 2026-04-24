"""
自定义域名邮箱服务实现
基于 email.md 中的 REST API 接口
"""

import re
import time
import json
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urljoin

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN


logger = logging.getLogger(__name__)


class MeoMailEmailService(BaseEmailService):
    """
    自定义域名邮箱服务
    基于 REST API 接口
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化自定义域名邮箱服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: API 基础地址 (必需)
                - api_key: API 密钥 (必需)
                - api_key_header: API 密钥请求头名称 (默认: X-API-Key)
                - timeout: 请求超时时间 (默认: 30)
                - max_retries: 最大重试次数 (默认: 3)
                - proxy_url: 代理 URL
                - default_domain: 默认域名
                - default_expiry: 默认过期时间（毫秒）
            name: 服务名称
        """
        super().__init__(EmailServiceType.MOE_MAIL, name)

        # 必需配置检查
        required_keys = ["base_url", "api_key"]
        missing_keys = [key for key in required_keys if key not in (config or {})]

        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        # 默认配置
        default_config = {
            "base_url": "",
            "api_key": "",
            "api_key_header": "X-API-Key",
            "timeout": 30,
            "max_retries": 3,
            "proxy_url": None,
            "default_domain": None,
            "default_expiry": 3600000,  # 1小时
        }

        self.config = {**default_config, **(config or {})}

        # 创建 HTTP 客户端
        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(
            proxy_url=self.config.get("proxy_url"),
            config=http_config
        )

        # 状态变量
        self._emails_cache: Dict[str, Dict[str, Any]] = {}
        self._last_config_check: float = 0
        self._cached_config: Optional[Dict[str, Any]] = None

    def _get_headers(self) -> Dict[str, str]:
        """获取 API 请求头"""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # 添加 API 密钥
        api_key_header = self.config.get("api_key_header", "X-API-Key")
        headers[api_key_header] = self.config["api_key"]

        return headers

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """
        发送 API 请求

        Args:
            method: HTTP 方法
            endpoint: API 端点
            **kwargs: 请求参数

        Returns:
            响应 JSON 数据

        Raises:
            EmailServiceError: 请求失败
        """
        url = urljoin(self.config["base_url"], endpoint)

        # 添加默认请求头
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._get_headers())

        try:
            # POST 请求禁用自动重定向，手动处理以保持 POST 方法（避免 HTTP→HTTPS 重定向时被转为 GET）
            if method.upper() == "POST":
                kwargs["allow_redirects"] = False
                response = self.http_client.request(method, url, **kwargs)
                # 处理重定向
                max_redirects = 5
                redirect_count = 0
                while response.status_code in (301, 302, 303, 307, 308) and redirect_count < max_redirects:
                    location = response.headers.get("Location", "")
                    if not location:
                        break
                    import urllib.parse as _urlparse
                    redirect_url = _urlparse.urljoin(url, location)
                    # 307/308 保持 POST，其余（301/302/303）转为 GET
                    if response.status_code in (307, 308):
                        redirect_method = method
                        redirect_kwargs = kwargs
                    else:
                        redirect_method = "GET"
                        # GET 不传 body
                        redirect_kwargs = {k: v for k, v in kwargs.items() if k not in ("json", "data")}
                    response = self.http_client.request(redirect_method, redirect_url, **redirect_kwargs)
                    url = redirect_url
                    redirect_count += 1
            else:
                response = self.http_client.request(method, url, **kwargs)

            if response.status_code >= 400:
                error_msg = f"API 请求失败: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except:
                    error_msg = f"{error_msg} - {response.text[:200]}"

                self.update_status(False, EmailServiceError(error_msg))
                raise EmailServiceError(error_msg)

            # 解析响应
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"raw_response": response.text}

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"API 请求失败: {method} {endpoint} - {e}")

    def get_config(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        获取系统配置

        Args:
            force_refresh: 是否强制刷新缓存

        Returns:
            配置信息
        """
        # 检查缓存
        if not force_refresh and self._cached_config and time.time() - self._last_config_check < 300:
            return self._cached_config

        try:
            response = self._make_request("GET", "/api/config")
            self._cached_config = response
            self._last_config_check = time.time()
            self.update_status(True)
            return response
        except Exception as e:
            logger.warning(f"获取配置失败: {e}")
            return {}

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        创建临时邮箱

        Args:
            config: 配置参数:
                - name: 邮箱前缀（可选）
                - expiryTime: 有效期（毫秒）（可选）
                - domain: 邮箱域名（可选）

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - service_id: 邮箱 ID
            - id: 邮箱 ID（同 service_id）
            - expiry: 过期时间信息
        """
        # 获取默认配置
        sys_config = self.get_config()
        default_domain = self.config.get("default_domain")
        if not default_domain and sys_config.get("emailDomains"):
            # 使用系统配置的第一个域名
            domains = sys_config["emailDomains"].split(",")
            default_domain = domains[0].strip() if domains else None

        # 构建请求参数
        request_config = config or {}
        create_data = {
            "name": request_config.get("name", ""),
            "expiryTime": request_config.get("expiryTime", self.config.get("default_expiry", 3600000)),
            "domain": request_config.get("domain", default_domain),
        }

        # 移除空值
        create_data = {k: v for k, v in create_data.items() if v is not None and v != ""}

        try:
            response = self._make_request("POST", "/api/emails/generate", json=create_data)

            email = response.get("email", "").strip()
            email_id = response.get("id", "").strip()

            if not email or not email_id:
                raise EmailServiceError("API 返回数据不完整")

            email_info = {
                "email": email,
                "service_id": email_id,
                "id": email_id,
                "created_at": time.time(),
                "expiry": create_data.get("expiryTime"),
                "domain": create_data.get("domain"),
                "raw_response": response,
            }

            # 缓存邮箱信息
            self._emails_cache[email_id] = email_info

            logger.info(f"成功创建自定义域名邮箱: {email} (ID: {email_id})")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从自定义域名邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 邮箱 ID（如果不提供，从缓存中查找）
            timeout: 超时时间（秒）
            pattern: 验证码正则表达式
            otp_sent_at: OTP 发送时间戳（自定义域名服务暂不使用此参数）

        Returns:
            验证码字符串，如果超时或未找到返回 None
        """
        # 查找邮箱 ID
        target_email_id = email_id
        if not target_email_id:
            # 从缓存中查找
            for eid, info in self._emails_cache.items():
                if info.get("email") == email:
                    target_email_id = eid
                    break

        if not target_email_id:
            logger.warning(f"未找到邮箱 {email} 的 ID，无法获取验证码")
            return None

        logger.info(f"正在从自定义域名邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_message_ids = set()

        while time.time() - start_time < timeout:
            try:
                # 获取邮件列表
                response = self._make_request("GET", f"/api/emails/{target_email_id}")

                messages = response.get("messages", [])
                if not isinstance(messages, list):
                    time.sleep(3)
                    continue

                for message in messages:
                    message_id = message.get("id")
                    if not message_id or message_id in seen_message_ids:
                        continue

                    seen_message_ids.add(message_id)

                    # 检查是否是目标邮件
                    sender = str(message.get("from_address", "")).lower()
                    subject = str(message.get("subject", ""))

                    # 获取邮件内容
                    message_content = self._get_message_content(target_email_id, message_id)
                    if not message_content:
                        continue

                    content = f"{sender} {subject} {message_content}"

                    # 检查是否是 OpenAI 邮件
                    if "openai" not in sender and "openai" not in content.lower():
                        continue

                    # 提取验证码 过滤掉邮箱
                    email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
                    match = re.search(pattern, re.sub(email_pattern, "", content))
                    if match:
                        code = match.group(1)
                        logger.info(f"从自定义域名邮箱 {email} 找到验证码: {code}")
                        self.update_status(True)
                        return code

            except Exception as e:
                logger.debug(f"检查邮件时出错: {e}")

            # 等待一段时间再检查
            time.sleep(3)

        logger.warning(f"等待验证码超时: {email}")
        return None

    def _get_message_content(self, email_id: str, message_id: str) -> Optional[str]:
        """获取邮件内容"""
        try:
            response = self._make_request("GET", f"/api/emails/{email_id}/{message_id}")
            message = response.get("message", {})

            # 优先使用纯文本内容，其次使用 HTML 内容
            content = message.get("content", "")
            if not content:
                html = message.get("html", "")
                if html:
                    # 简单去除 HTML 标签
                    content = re.sub(r"<[^>]+>", " ", html)

            return content
        except Exception as e:
            logger.debug(f"获取邮件内容失败: {e}")
            return None

    def list_emails(self, cursor: str = None, **kwargs) -> List[Dict[str, Any]]:
        """
        列出所有邮箱

        Args:
            cursor: 分页游标
            **kwargs: 其他参数

        Returns:
            邮箱列表
        """
        params = {}
        if cursor:
            params["cursor"] = cursor

        try:
            response = self._make_request("GET", "/api/emails", params=params)
            emails = response.get("emails", [])

            # 更新缓存
            for email_info in emails:
                email_id = email_info.get("id")
                if email_id:
                    self._emails_cache[email_id] = email_info

            self.update_status(True)
            return emails
        except Exception as e:
            logger.warning(f"列出邮箱失败: {e}")
            self.update_status(False, e)
            return []

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱

        Args:
            email_id: 邮箱 ID

        Returns:
            是否删除成功
        """
        try:
            response = self._make_request("DELETE", f"/api/emails/{email_id}")
            success = response.get("success", False)

            if success:
                # 从缓存中移除
                self._emails_cache.pop(email_id, None)
                logger.info(f"成功删除邮箱: {email_id}")
            else:
                logger.warning(f"删除邮箱失败: {email_id}")

            self.update_status(success)
            return success

        except Exception as e:
            logger.error(f"删除邮箱失败: {email_id} - {e}")
            self.update_status(False, e)
            return False

    def check_health(self) -> bool:
        """检查自定义域名邮箱服务是否可用"""
        try:
            # 尝试获取配置
            config = self.get_config(force_refresh=True)
            if config:
                logger.debug(f"自定义域名邮箱服务健康检查通过，配置: {config.get('defaultRole', 'N/A')}")
                self.update_status(True)
                return True
            else:
                logger.warning("自定义域名邮箱服务健康检查失败：获取配置为空")
                self.update_status(False, EmailServiceError("获取配置为空"))
                return False
        except Exception as e:
            logger.warning(f"自定义域名邮箱服务健康检查失败: {e}")
            self.update_status(False, e)
            return False

    def get_email_messages(self, email_id: str, cursor: str = None) -> List[Dict[str, Any]]:
        """
        获取邮箱中的邮件列表

        Args:
            email_id: 邮箱 ID
            cursor: 分页游标

        Returns:
            邮件列表
        """
        params = {}
        if cursor:
            params["cursor"] = cursor

        try:
            response = self._make_request("GET", f"/api/emails/{email_id}", params=params)
            messages = response.get("messages", [])
            self.update_status(True)
            return messages
        except Exception as e:
            logger.error(f"获取邮件列表失败: {email_id} - {e}")
            self.update_status(False, e)
            return []

    def get_message_detail(self, email_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        """
        获取邮件详情

        Args:
            email_id: 邮箱 ID
            message_id: 邮件 ID

        Returns:
            邮件详情
        """
        try:
            response = self._make_request("GET", f"/api/emails/{email_id}/{message_id}")
            message = response.get("message")
            self.update_status(True)
            return message
        except Exception as e:
            logger.error(f"获取邮件详情失败: {email_id}/{message_id} - {e}")
            self.update_status(False, e)
            return None

    def create_email_share(self, email_id: str, expires_in: int = 86400000) -> Optional[Dict[str, Any]]:
        """
        创建邮箱分享链接

        Args:
            email_id: 邮箱 ID
            expires_in: 有效期（毫秒）

        Returns:
            分享信息
        """
        try:
            response = self._make_request(
                "POST",
                f"/api/emails/{email_id}/share",
                json={"expiresIn": expires_in}
            )
            self.update_status(True)
            return response
        except Exception as e:
            logger.error(f"创建邮箱分享链接失败: {email_id} - {e}")
            self.update_status(False, e)
            return None

    def create_message_share(
        self,
        email_id: str,
        message_id: str,
        expires_in: int = 86400000
    ) -> Optional[Dict[str, Any]]:
        """
        创建邮件分享链接

        Args:
            email_id: 邮箱 ID
            message_id: 邮件 ID
            expires_in: 有效期（毫秒）

        Returns:
            分享信息
        """
        try:
            response = self._make_request(
                "POST",
                f"/api/emails/{email_id}/messages/{message_id}/share",
                json={"expiresIn": expires_in}
            )
            self.update_status(True)
            return response
        except Exception as e:
            logger.error(f"创建邮件分享链接失败: {email_id}/{message_id} - {e}")
            self.update_status(False, e)
            return None

    def get_service_info(self) -> Dict[str, Any]:
        """获取服务信息"""
        config = self.get_config()
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config["base_url"],
            "default_domain": self.config.get("default_domain"),
            "system_config": config,
            "cached_emails_count": len(self._emails_cache),
            "status": self.status.value,
        }