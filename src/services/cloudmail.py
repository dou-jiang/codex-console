"""
CloudMail 邮箱服务实现
基于 CloudMail Web API
"""

import logging
import random
import re
import string
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN
from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)


class CloudMailService(BaseEmailService):
    """CloudMail 邮箱服务。"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.CLOUDMAIL, name or "cloudmail_service")

        required_keys = ["base_url", "login_email", "login_password"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "timeout": 30,
            "max_retries": 3,
            "proxy_url": None,
            "default_domain": "",
            "poll_interval": 3,
            "login_email": "",
            "login_password": "",
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(
            proxy_url=self.config.get("proxy_url"),
            config=http_config,
        )

        self._domains: List[str] = []
        self._accounts_by_id: Dict[str, Dict[str, Any]] = {}
        self._accounts_by_email: Dict[str, Dict[str, Any]] = {}
        self._authorization_jwt: Optional[str] = None

    def _build_headers(self, authorization: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh",
        }
        if authorization is not None:
            headers["authorization"] = authorization
        return headers

    def _make_request(
        self,
        method: str,
        path: str,
        authorization: Optional[str] = None,
        **kwargs,
    ) -> Any:
        url = f"{self.config['base_url']}{path}"
        headers = self._build_headers(authorization=authorization)
        extra_headers = kwargs.pop("headers", None) or {}
        headers.update(extra_headers)

        try:
            response = self.http_client.request(method, url, headers=headers, **kwargs)

            if response.status_code >= 400:
                message = f"请求失败: {response.status_code}"
                try:
                    message = f"{message} - {response.text[:300]}"
                except Exception:
                    pass
                raise EmailServiceError(message)

            try:
                data = response.json()
            except Exception:
                return {"raw_response": response.text}

            if isinstance(data, dict) and data.get("code") not in (None, 200):
                raise EmailServiceError(f"接口返回异常: {data}")

            return data
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _get_authorization_jwt(self, refresh: bool = False) -> str:
        if not refresh and self._authorization_jwt:
            return self._authorization_jwt

        token = self.login(
            self.config["login_email"],
            self.config["login_password"],
        )
        self._authorization_jwt = token
        return token

    def _cache_account(self, account_info: Dict[str, Any]) -> None:
        account_id = str(account_info.get("accountId") or account_info.get("service_id") or "").strip()
        email = str(account_info.get("email") or "").strip().lower()

        if account_id:
            self._accounts_by_id[account_id] = account_info
        if email:
            self._accounts_by_email[email] = account_info

    def _get_account(self, email: Optional[str] = None, email_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if email_id:
            cached = self._accounts_by_id.get(str(email_id))
            if cached:
                return cached
        if email:
            cached = self._accounts_by_email.get(str(email).strip().lower())
            if cached:
                return cached
        return None

    def _parse_time(self, value: Any) -> Optional[float]:
        if not value:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(text, fmt).timestamp()
            except Exception:
                continue
        return None

    def _generate_local_part(self, length: int = 10) -> str:
        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=max(1, length - 1)))
        return f"{first}{rest}"

    def login(self, email: str, password: str) -> str:
        data = self._make_request(
            "POST",
            "/api/login",
            json={"email": email, "password": password},
            headers={
                "content-type": "application/json",
                "cache-control": "no-cache",
                "pragma": "no-cache",
            },
        )
        token = ((data or {}).get("data") or {}).get("token")
        if not token:
            raise EmailServiceError(f"登录返回异常: {data}")
        return str(token)

    def get_domain_list(self) -> List[str]:
        data = self._make_request(
            "GET",
            "/api/setting/websiteConfig",
            authorization="null",
            headers={
                "cache-control": "no-cache",
                "pragma": "no-cache",
            },
        )
        domains = ((data or {}).get("data") or {}).get("domainList") or []
        return [str(domain).strip() for domain in domains if str(domain).strip()]

    def _ensure_domains(self) -> None:
        if not self._domains:
            try:
                self._domains = self.get_domain_list()
            except Exception as e:
                logger.warning(f"获取 CloudMail 域名列表失败: {e}")

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        req_config = config or {}
        self._ensure_domains()

        domain = str(req_config.get("domain") or self.config.get("default_domain") or "").strip()
        if not domain and self._domains:
            domain = self._domains[0]
        if domain and not domain.startswith("@"):
            domain = f"@{domain}"

        email = str(req_config.get("email") or "").strip()
        if not email:
            prefix = str(req_config.get("name") or req_config.get("prefix") or self._generate_local_part()).strip()
            if not domain:
                raise EmailServiceError("未配置可用域名，无法创建 CloudMail 邮箱")
            email = f"{prefix}{domain}"

        data = self._make_request(
            "POST",
            "/api/account/add",
            authorization=self._get_authorization_jwt(),
            json={"email": email, "token": ""},
            headers={"content-type": "application/json"},
        )

        account_data = (data or {}).get("data") or {}
        account_id = account_data.get("accountId")
        email_address = account_data.get("email") or email
        if not account_id or not email_address:
            raise EmailServiceError(f"创建邮箱失败，返回数据不完整: {data}")

        email_info = {
            "id": str(account_id),
            "service_id": str(account_id),
            "accountId": str(account_id),
            "email": str(email_address),
            "created_at": time.time(),
            "raw_data": account_data,
        }
        self._cache_account(email_info)
        self.update_status(True)
        return email_info

    def get_email_messages(self, email_id: str, **kwargs) -> List[Dict[str, Any]]:
        account = self._get_account(email_id=email_id) or self._get_account(email=email_id)
        if not account:
            return []

        account_id = account.get("accountId") or account.get("service_id")
        if not account_id:
            return []

        params = {
            "accountId": str(account_id),
            "allReceive": str(kwargs.get("allReceive", 0)),
            "emailId": str(kwargs.get("emailId", 0)),
            "timeSort": str(kwargs.get("timeSort", 0)),
            "size": str(kwargs.get("size", 20)),
            "type": str(kwargs.get("type", 0)),
        }

        data = self._make_request(
            "GET",
            "/api/email/list",
            authorization=self._get_authorization_jwt(),
            params=params,
            headers={
                "cache-control": "no-cache",
                "pragma": "no-cache",
            },
        )
        payload = (data or {}).get("data") or {}
        messages = payload.get("list") or []
        return messages if isinstance(messages, list) else []

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        account = self._get_account(email=email, email_id=email_id)
        if not account:
            logger.warning(f"CloudMail 未找到邮箱缓存: {email}, {email_id}")
            return None

        start_time = time.time()
        seen_message_ids = set()
        poll_interval = max(1, int(self.config.get("poll_interval") or 3))

        while time.time() - start_time < timeout:
            try:
                messages = self.get_email_messages(account.get("accountId") or account.get("service_id"))
                messages = sorted(
                    messages,
                    key=lambda item: str(item.get("createTime") or ""),
                    reverse=True,
                )

                for message in messages:
                    message_id = str(message.get("emailId") or "").strip()
                    if not message_id or message_id in seen_message_ids:
                        continue

                    created_at = self._parse_time(message.get("createTime"))
                    if otp_sent_at and created_at and created_at + 1 < otp_sent_at:
                        continue

                    seen_message_ids.add(message_id)

                    sender = str(message.get("sendEmail") or "")
                    subject = str(message.get("subject") or "")
                    text = str(message.get("text") or "")
                    content = str(message.get("content") or "")
                    merged = "\n".join(part for part in [sender, subject, text, content] if part)
                    merged_lower = merged.lower()

                    if "openai" not in merged_lower and "chatgpt" not in merged_lower:
                        continue

                    match = re.search(pattern, merged)
                    if match:
                        self.update_status(True)
                        return match.group(1)
            except Exception as e:
                logger.debug(f"CloudMail 轮询验证码失败: {e}")

            time.sleep(poll_interval)

        logger.warning(f"等待 CloudMail 验证码超时: {email}")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self._accounts_by_email.values())

    def delete_email(self, email_id: str) -> bool:
        account = self._get_account(email_id=email_id) or self._get_account(email=email_id)
        if not account:
            return False

        account_id = account.get("accountId") or account.get("service_id")
        if not account_id:
            return False

        try:
            self._make_request(
                "DELETE",
                "/api/account/delete",
                authorization=self._get_authorization_jwt(),
                params={"accountId": str(account_id)},
            )
            self._accounts_by_id.pop(str(account_id), None)
            self._accounts_by_email.pop(str(account.get("email") or "").lower(), None)
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"CloudMail 删除邮箱失败: {e}")
            self.update_status(False, e)
            return False

    def check_health(self) -> bool:
        try:
            self.get_domain_list()
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"CloudMail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
