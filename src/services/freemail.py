"""
Freemail 邮箱服务实现
基于自部署 Cloudflare Worker 临时邮箱服务 (https://github.com/idinging/freemail)
"""

import re
import time
import logging
import random
import string
from typing import Optional, Dict, Any, List

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN

logger = logging.getLogger(__name__)


class FreemailService(BaseEmailService):
    """
    Freemail 邮箱服务
    基于自部署 Cloudflare Worker 的临时邮箱
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Freemail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: Worker 域名地址 (必需)
                - admin_token: Admin Token，对应 JWT_TOKEN (必需)
                - domain: 邮箱域名，如 example.com
                - timeout: 请求超时时间，默认 30
                - max_retries: 最大重试次数，默认 3
            name: 服务名称
        """
        super().__init__(EmailServiceType.FREEMAIL, name)

        required_keys = ["base_url", "admin_token"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = self.config["base_url"].rstrip("/")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        # 缓存 domain 列表
        self._domains = []

    def _get_headers(self) -> Dict[str, str]:
        """构造 admin 请求头"""
        return {
            "Authorization": f"Bearer {self.config['admin_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _make_request(self, method: str, path: str, **kwargs) -> Any:
        """
        发送请求并返回 JSON 数据

        Args:
            method: HTTP 方法
            path: 请求路径（以 / 开头）
            **kwargs: 传递给 http_client.request 的额外参数

        Returns:
            响应 JSON 数据

        Raises:
            EmailServiceError: 请求失败
        """
        url = f"{self.config['base_url']}{path}"
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._get_headers())

        try:
            response = self.http_client.request(method, url, **kwargs)

            if response.status_code >= 400:
                error_msg = f"请求失败: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                self.update_status(False, EmailServiceError(error_msg))
                raise EmailServiceError(error_msg)

            try:
                return response.json()
            except Exception:
                return {"raw_response": response.text}

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _ensure_domains(self):
        """获取并缓存可用域名列表"""
        if not self._domains:
            try:
                domains = self._make_request("GET", "/api/domains")
                if isinstance(domains, list):
                    self._domains = domains
            except Exception as e:
                logger.warning(f"获取 Freemail 域名列表失败: {e}")

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        通过 API 创建临时邮箱

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - service_id: 同 email（用作标识）
        """
        self._ensure_domains()
        
        req_config = config or {}
        domain_index = 0
        target_domain = req_config.get("domain") or self.config.get("domain")
        
        if target_domain and self._domains:
            for i, d in enumerate(self._domains):
                if d == target_domain:
                    domain_index = i
                    break
                    
        prefix = req_config.get("name")
        try:
            if prefix:
                body = {
                    "local": prefix,
                    "domainIndex": domain_index
                }
                resp = self._make_request("POST", "/api/create", json=body)
            else:
                params = {"domainIndex": domain_index}
                length = req_config.get("length")
                if length:
                    params["length"] = length
                resp = self._make_request("GET", "/api/generate", params=params)

            email = resp.get("email")
            if not email:
                raise EmailServiceError(f"创建邮箱失败，未返回邮箱地址: {resp}")

            email_info = {
                "email": email,
                "service_id": email,
                "id": email,
                "created_at": time.time(),
            }

            logger.info(f"成功创建 Freemail 邮箱: {email}")
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
        从 Freemail 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用，保留接口兼容
            timeout: 超时时间（秒）
            pattern: 验证码正则
            otp_sent_at: OTP 发送时间戳（暂未使用）

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info(f"正在从 Freemail 邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_mail_ids: set = set()

        while time.time() - start_time < timeout:
            try:
                mails = self._make_request("GET", "/api/emails", params={"mailbox": email, "limit": 20})
                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                for mail in mails:
                    mail_id = mail.get("id")
                    if not mail_id or mail_id in seen_mail_ids:
                        continue

                    seen_mail_ids.add(mail_id)

                    sender = str(mail.get("sender", "")).lower()
                    subject = str(mail.get("subject", ""))
                    preview = str(mail.get("preview", ""))
                    
                    content = f"{sender}\n{subject}\n{preview}"
                    
                    if "openai" not in content.lower():
                        continue

                    # 尝试直接使用 Freemail 提取的验证码
                    v_code = mail.get("verification_code")
                    if v_code:
                        logger.info(f"从 Freemail 邮箱 {email} 找到验证码: {v_code}")
                        self.update_status(True)
                        return v_code

                    # 如果没有直接提供，通过正则匹配 preview
                    match = re.search(pattern, content)
                    if match:
                        code = match.group(1)
                        logger.info(f"从 Freemail 邮箱 {email} 找到验证码: {code}")
                        self.update_status(True)
                        return code

                    # 如果依然未找到，获取邮件详情进行匹配
                    try:
                        detail = self._make_request("GET", f"/api/email/{mail_id}")
                        full_content = str(detail.get("content", "")) + "\n" + str(detail.get("html_content", ""))
                        match = re.search(pattern, full_content)
                        if match:
                            code = match.group(1)
                            logger.info(f"从 Freemail 邮箱 {email} 找到验证码: {code}")
                            self.update_status(True)
                            return code
                    except Exception as e:
                        logger.debug(f"获取 Freemail 邮件详情失败: {e}")

            except Exception as e:
                logger.debug(f"检查 Freemail 邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待 Freemail 验证码超时: {email}")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出邮箱

        Args:
            **kwargs: 额外查询参数

        Returns:
            邮箱列表
        """
        try:
            params = {
                "limit": kwargs.get("limit", 100),
                "offset": kwargs.get("offset", 0)
            }
            resp = self._make_request("GET", "/api/mailboxes", params=params)
            
            emails = []
            if isinstance(resp, list):
                for mail in resp:
                    address = mail.get("address")
                    if address:
                        emails.append({
                            "id": address,
                            "service_id": address,
                            "email": address,
                            "created_at": mail.get("created_at"),
                            "raw_data": mail
                        })
            self.update_status(True)
            return emails
        except Exception as e:
            logger.warning(f"列出 Freemail 邮箱失败: {e}")
            self.update_status(False, e)
            return []

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱
        """
        try:
            self._make_request("DELETE", "/api/mailboxes", params={"address": email_id})
            logger.info(f"已删除 Freemail 邮箱: {email_id}")
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"删除 Freemail 邮箱失败: {e}")
            self.update_status(False, e)
            return False

    def check_health(self) -> bool:
        """检查服务健康状态"""
        try:
            self._make_request("GET", "/api/domains")
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"Freemail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
