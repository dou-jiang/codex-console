"""
DuckMail 邮箱服务实现
兼容 DuckMail 的 accounts/token/messages 接口模型
"""

import logging
import random
import re
import string
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List, Optional

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN
from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)


class DuckMailService(BaseEmailService):
    """DuckMail 邮箱服务"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.DUCK_MAIL, name)

        required_keys = ["base_url", "default_domain"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "api_key": "",
            "password_length": 12,
            "expires_in": None,
            "timeout": 30,
            "max_retries": 3,
            "proxy_url": None,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")
        self.config["default_domain"] = str(self.config["default_domain"]).strip().lstrip("@")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(
            proxy_url=self.config.get("proxy_url"),
            config=http_config,
        )

        self._accounts_by_id: Dict[str, Dict[str, Any]] = {}
        self._accounts_by_email: Dict[str, Dict[str, Any]] = {}

    def _build_headers(
        self,
        token: Optional[str] = None,
        use_api_key: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        auth_token = token
        if not auth_token and use_api_key and self.config.get("api_key"):
            auth_token = self.config["api_key"]

        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        if extra_headers:
            headers.update(extra_headers)

        return headers

    def _make_request(
        self,
        method: str,
        path: str,
        token: Optional[str] = None,
        use_api_key: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        url = f"{self.config['base_url']}{path}"
        kwargs["headers"] = self._build_headers(
            token=token,
            use_api_key=use_api_key,
            extra_headers=kwargs.get("headers"),
        )

        try:
            response = self.http_client.request(method, url, **kwargs)
            if response.status_code >= 400:
                error_message = f"API 请求失败: {response.status_code}"
                try:
                    error_payload = response.json()
                    error_message = f"{error_message} - {error_payload}"
                except Exception:
                    error_message = f"{error_message} - {response.text[:200]}"
                raise EmailServiceError(error_message)

            try:
                return response.json()
            except Exception:
                return {"raw_response": response.text}
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _generate_local_part(self) -> str:
        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
        return f"{first}{rest}"

    def _generate_password(self) -> str:
        length = max(6, int(self.config.get("password_length") or 12))
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choices(alphabet, k=length))

    def _cache_account(self, account_info: Dict[str, Any]) -> None:
        account_id = str(account_info.get("account_id") or account_info.get("service_id") or "").strip()
        email = str(account_info.get("email") or "").strip().lower()

        if account_id:
            self._accounts_by_id[account_id] = account_info
        if email:
            self._accounts_by_email[email] = account_info

    def _get_account_info(self, email: Optional[str] = None, email_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if email_id:
            cached = self._accounts_by_id.get(str(email_id))
            if cached:
                return cached

        if email:
            cached = self._accounts_by_email.get(str(email).strip().lower())
            if cached:
                return cached

        return None

    def _strip_html(self, html_content: Any) -> str:
        if isinstance(html_content, list):
            html_content = "\n".join(str(item) for item in html_content if item)
        text = str(html_content or "")
        return unescape(re.sub(r"<[^>]+>", " ", text))

    def _parse_message_time(self, value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
        except Exception:
            return None

    def _message_search_text(self, summary: Dict[str, Any], detail: Dict[str, Any]) -> str:
        sender = summary.get("from") or detail.get("from") or {}
        if isinstance(sender, dict):
            sender_text = " ".join(
                str(sender.get(key) or "") for key in ("name", "address")
            ).strip()
        else:
            sender_text = str(sender)

        subject = str(summary.get("subject") or detail.get("subject") or "")
        text_body = str(detail.get("text") or "")
        html_body = self._strip_html(detail.get("html"))
        return "\n".join(part for part in [sender_text, subject, text_body, html_body] if part).strip()

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        request_config = config or {}
        local_part = str(request_config.get("name") or self._generate_local_part()).strip()
        domain = str(request_config.get("default_domain") or request_config.get("domain") or self.config["default_domain"]).strip().lstrip("@")
        address = f"{local_part}@{domain}"
        password = self._generate_password()

        payload: Dict[str, Any] = {
            "address": address,
            "password": password,
        }

        expires_in = request_config.get("expiresIn", request_config.get("expires_in", self.config.get("expires_in")))
        if expires_in is not None:
            payload["expiresIn"] = expires_in

        account_response = self._make_request(
            "POST",
            "/accounts",
            json=payload,
            use_api_key=bool(self.config.get("api_key")),
        )
        token_response = self._make_request(
            "POST",
            "/token",
            json={
                "address": account_response.get("address", address),
                "password": password,
            },
        )

        account_id = str(account_response.get("id") or token_response.get("id") or "").strip()
        resolved_address = str(account_response.get("address") or address).strip()
        token = str(token_response.get("token") or "").strip()

        if not account_id or not resolved_address or not token:
            raise EmailServiceError("DuckMail 返回数据不完整")

        email_info = {
            "email": resolved_address,
            "service_id": account_id,
            "id": account_id,
            "account_id": account_id,
            "token": token,
            "password": password,
            "created_at": time.time(),
            "raw_account": account_response,
        }

        self._cache_account(email_info)
        self.update_status(True)
        return email_info

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        account_info = self._get_account_info(email=email, email_id=email_id)
        if not account_info:
            logger.warning(f"DuckMail 未找到邮箱缓存: {email}, {email_id}")
            return None

        token = account_info.get("token")
        if not token:
            logger.warning(f"DuckMail 邮箱缺少访问 token: {email}")
            return None

        start_time = time.time()
        seen_message_ids = set()

        while time.time() - start_time < timeout:
            try:
                response = self._make_request(
                    "GET",
                    "/messages",
                    token=token,
                    params={"page": 1},
                )
                messages = response.get("hydra:member", [])

                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen_message_ids:
                        continue

                    created_at = self._parse_message_time(message.get("createdAt"))
                    if otp_sent_at and created_at and created_at + 1 < otp_sent_at:
                        continue

                    seen_message_ids.add(message_id)
                    detail = self._make_request(
                        "GET",
                        f"/messages/{message_id}",
                        token=token,
                    )

                    content = self._message_search_text(message, detail)
                    if "openai" not in content.lower():
                        continue

                    match = re.search(pattern, content)
                    if match:
                        self.update_status(True)
                        return match.group(1)
            except Exception as e:
                logger.debug(f"DuckMail 轮询验证码失败: {e}")

            time.sleep(3)

        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self._accounts_by_email.values())

    def delete_email(self, email_id: str) -> bool:
        account_info = self._get_account_info(email_id=email_id) or self._get_account_info(email=email_id)
        if not account_info:
            return False

        token = account_info.get("token")
        account_id = account_info.get("account_id") or account_info.get("service_id")
        if not token or not account_id:
            return False

        try:
            self._make_request(
                "DELETE",
                f"/accounts/{account_id}",
                token=token,
            )
            self._accounts_by_id.pop(str(account_id), None)
            self._accounts_by_email.pop(str(account_info.get("email") or "").lower(), None)
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"DuckMail 删除邮箱失败: {e}")
            self.update_status(False, e)
            return False

    def check_health(self) -> bool:
        try:
            self._make_request(
                "GET",
                "/domains",
                params={"page": 1},
                use_api_key=bool(self.config.get("api_key")),
            )
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"DuckMail 健康检查失败: {e}")
            self.update_status(False, e)
            return False

    def get_email_messages(self, email_id: str, **kwargs) -> List[Dict[str, Any]]:
        account_info = self._get_account_info(email_id=email_id) or self._get_account_info(email=email_id)
        if not account_info or not account_info.get("token"):
            return []
        response = self._make_request(
            "GET",
            "/messages",
            token=account_info["token"],
            params={"page": kwargs.get("page", 1)},
        )
        return response.get("hydra:member", [])

    def get_message_detail(self, email_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        account_info = self._get_account_info(email_id=email_id) or self._get_account_info(email=email_id)
        if not account_info or not account_info.get("token"):
            return None
        return self._make_request(
            "GET",
            f"/messages/{message_id}",
            token=account_info["token"],
        )

    def get_service_info(self) -> Dict[str, Any]:
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config["base_url"],
            "default_domain": self.config["default_domain"],
            "cached_accounts": len(self._accounts_by_email),
            "status": self.status.value,
        }
