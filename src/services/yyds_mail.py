"""
YYDS Mail 邮箱服务实现
基于 YYDS Mail REST API（/v1/accounts、/v1/token、/v1/messages）
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
from ..config.constants import OTP_CODE_PATTERN, OTP_CODE_SEMANTIC_PATTERN
from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)


class YYDSMailService(BaseEmailService):
    """YYDS Mail 临时邮箱服务"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.YYDS_MAIL, name)

        required_keys = ["base_url", "api_key"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "default_domain": "",
            "timeout": 30,
            "max_retries": 3,
            "proxy_url": None,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")
        self.config["default_domain"] = str(self.config.get("default_domain") or "").strip().lstrip("@")

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
        self._last_used_message_ids: Dict[str, str] = {}

    def _build_headers(
        self,
        *,
        token: Optional[str] = None,
        use_api_key: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif use_api_key:
            headers["X-API-Key"] = str(self.config["api_key"]).strip()

        if extra_headers:
            headers.update(extra_headers)

        return headers

    def _unwrap_payload(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload

        if payload.get("success") is False:
            message = str(payload.get("error") or payload.get("message") or "请求失败").strip()
            raise EmailServiceError(message)

        if "data" in payload:
            return payload.get("data")
        return payload

    def _make_request(
        self,
        method: str,
        path: str,
        *,
        token: Optional[str] = None,
        use_api_key: bool = False,
        **kwargs,
    ) -> Any:
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
                    error_message = str(
                        error_payload.get("error")
                        or error_payload.get("message")
                        or error_payload
                    )
                except Exception:
                    error_message = response.text[:200] or error_message
                raise EmailServiceError(error_message)

            try:
                payload = response.json()
            except Exception:
                payload = {}

            return self._unwrap_payload(payload)
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _generate_local_part(self) -> str:
        prefix = "".join(random.choices(string.ascii_lowercase, k=7))
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=3))
        return f"{prefix}{suffix}"

    def _cache_account(self, account_info: Dict[str, Any]) -> None:
        account_id = str(account_info.get("account_id") or account_info.get("service_id") or "").strip()
        email = str(account_info.get("email") or "").strip().lower()

        if account_id:
            self._accounts_by_id[account_id] = account_info
        if email:
            self._accounts_by_email[email] = account_info

    def _get_cached_account(
        self,
        *,
        email: Optional[str] = None,
        email_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if email_id:
            cached = self._accounts_by_id.get(str(email_id).strip())
            if cached:
                return cached

        if email:
            cached = self._accounts_by_email.get(str(email).strip().lower())
            if cached:
                return cached

        return None

    def _request_temp_token(self, email: str) -> Dict[str, Any]:
        payload = self._make_request(
            "POST",
            "/token",
            json={"address": str(email).strip()},
        )

        resolved_email = str(payload.get("address") or email).strip()
        account_id = str(payload.get("id") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not resolved_email or not token:
            raise EmailServiceError("YYDS Mail 返回的临时 Token 数据不完整")

        account_info = {
            "email": resolved_email,
            "service_id": account_id or resolved_email,
            "id": account_id or resolved_email,
            "account_id": account_id or resolved_email,
            "token": token,
            "created_at": time.time(),
        }
        self._cache_account(account_info)
        return account_info

    def _parse_message_time(self, value: Any) -> Optional[float]:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10**12:
                ts /= 1000.0
            return ts if ts > 0 else None

        text = str(value or "").strip()
        if not text:
            return None

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    def _html_to_text(self, html_content: Any) -> str:
        if isinstance(html_content, list):
            html_content = "\n".join(str(item) for item in html_content if item)
        text = str(html_content or "")
        return unescape(re.sub(r"<[^>]+>", " ", text))

    def _sender_text(self, sender: Any) -> str:
        if isinstance(sender, dict):
            return " ".join(
                str(sender.get(key) or "") for key in ("name", "address")
            ).strip()
        return str(sender or "").strip()

    def _message_search_text(self, summary: Dict[str, Any], detail: Dict[str, Any]) -> str:
        sender_text = self._sender_text(detail.get("from") or summary.get("from"))
        subject = str(detail.get("subject") or summary.get("subject") or "")
        text_body = str(detail.get("text") or "")
        html_body = self._html_to_text(detail.get("html"))
        summary_blob = " ".join(
            str(summary.get(key) or "")
            for key in ("subject", "snippet", "preview")
        ).strip()
        return "\n".join(
            part for part in [sender_text, subject, summary_blob, text_body, html_body] if part
        ).strip()

    def _is_openai_otp_mail(self, content: str) -> bool:
        text = str(content or "").lower()
        if "openai" not in text:
            return False
        keywords = (
            "verification code",
            "verify",
            "one-time code",
            "one time code",
            "security code",
            "your openai code",
            "验证码",
            "code is",
        )
        return any(keyword in text for keyword in keywords)

    def _extract_otp_code(self, content: str, pattern: str) -> Optional[str]:
        text = str(content or "")
        if not text:
            return None

        semantic_match = re.search(OTP_CODE_SEMANTIC_PATTERN, text, re.IGNORECASE)
        if semantic_match:
            return semantic_match.group(1)

        simple_match = re.search(pattern, text)
        if simple_match:
            return simple_match.group(1)
        return None

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        request_config = config or {}
        local_part = str(
            request_config.get("address")
            or request_config.get("prefix")
            or request_config.get("name")
            or self._generate_local_part()
        ).strip()
        domain = str(
            request_config.get("default_domain")
            or request_config.get("domain")
            or self.config.get("default_domain")
            or ""
        ).strip().lstrip("@")

        payload: Dict[str, Any] = {"address": local_part}
        if domain:
            payload["domain"] = domain

        account_response = self._make_request(
            "POST",
            "/accounts",
            json=payload,
            use_api_key=True,
        )

        account_id = str(account_response.get("id") or "").strip()
        email = str(account_response.get("address") or "").strip()
        token = str(account_response.get("token") or "").strip()

        if not account_id or not email or not token:
            raise EmailServiceError("YYDS Mail 返回数据不完整")

        email_info = {
            "email": email,
            "service_id": account_id,
            "id": account_id,
            "account_id": account_id,
            "token": token,
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
        account_info = self._get_cached_account(email=email, email_id=email_id)
        if not account_info:
            try:
                account_info = self._request_temp_token(email)
            except Exception as e:
                logger.warning(f"YYDS Mail 获取临时 Token 失败: {email} - {e}")
                return None

        token = str(account_info.get("token") or "").strip()
        if not token:
            return None

        start_time = time.time()
        seen_message_ids = set()
        last_used_message_id = self._last_used_message_ids.get(str(email).strip().lower())

        while time.time() - start_time < timeout:
            try:
                response = self._make_request(
                    "GET",
                    "/messages",
                    token=token,
                    params={
                        "address": str(email).strip(),
                        "limit": 50,
                    },
                )
                messages = []
                if isinstance(response, dict):
                    messages = response.get("messages") or []
                elif isinstance(response, list):
                    messages = response

                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen_message_ids:
                        continue
                    if last_used_message_id and message_id == last_used_message_id:
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
                    if not self._is_openai_otp_mail(content):
                        continue

                    code = self._extract_otp_code(content, pattern)
                    if code:
                        self._last_used_message_ids[str(email).strip().lower()] = message_id
                        self.update_status(True)
                        return code
            except Exception as e:
                logger.debug(f"YYDS Mail 轮询验证码失败: {e}")

            time.sleep(3)

        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self._accounts_by_email.values())

    def delete_email(self, email_id: str) -> bool:
        account_info = self._get_cached_account(email_id=email_id) or self._get_cached_account(email=email_id)
        if not account_info and email_id and "@" in str(email_id):
            try:
                account_info = self._request_temp_token(str(email_id))
            except Exception:
                account_info = None

        if not account_info:
            return False

        account_id = str(account_info.get("account_id") or account_info.get("service_id") or "").strip()
        token = str(account_info.get("token") or "").strip()
        if not account_id or not token:
            return False

        try:
            self._make_request(
                "DELETE",
                f"/accounts/{account_id}",
                token=token,
            )
            self._accounts_by_id.pop(account_id, None)
            self._accounts_by_email.pop(str(account_info.get("email") or "").strip().lower(), None)
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"YYDS Mail 删除邮箱失败: {e}")
            self.update_status(False, e)
            return False

    def check_health(self) -> bool:
        try:
            self._make_request(
                "GET",
                "/me",
                use_api_key=True,
            )
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"YYDS Mail 健康检查失败: {e}")
            self.update_status(False, e)
            return False

    def get_service_info(self) -> Dict[str, Any]:
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config["base_url"],
            "default_domain": self.config.get("default_domain") or "",
            "cached_accounts": len(self._accounts_by_email),
            "status": self.status.value,
        }
