"""
CloudMail 邮箱服务实现
基于 Cloud Mail 官方公开接口 /api/public/*
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


class CloudMailService(BaseEmailService):
    """Cloud Mail 自部署邮箱服务"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.CLOUDMAIL, name)

        required_keys = ["base_url", "admin_email", "admin_password", "domain"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "enable_prefix": True,
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")
        self.config["domain"] = str(self.config["domain"]).strip().lstrip("@")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)
        self._auth_token: str = ""
        self._accounts_by_email: Dict[str, Dict[str, Any]] = {}
        self._accounts_by_id: Dict[str, Dict[str, Any]] = {}
        self._last_used_mail_ids: Dict[str, str] = {}

    def _make_request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.config['base_url']}{path}"
        kwargs.setdefault("headers", {})
        headers = kwargs["headers"]
        headers.setdefault("Accept", "application/json")
        headers.setdefault("Content-Type", "application/json")
        if self._auth_token:
            headers.setdefault("Authorization", self._auth_token)

        try:
            response = self.http_client.request(method, url, **kwargs)
            try:
                payload = response.json()
            except Exception:
                payload = {"raw_response": response.text}

            if response.status_code >= 400:
                detail = payload if payload else response.text[:300]
                raise EmailServiceError(
                    f"请求失败: method={method} path={path} status={response.status_code} detail={detail}"
                )

            if isinstance(payload, dict):
                code = payload.get("code")
                if code not in (None, 200):
                    message = str(payload.get("message") or payload.get("msg") or payload).strip()
                    raise EmailServiceError(
                        f"接口返回异常: method={method} path={path} code={code} message={message}"
                    )
                if "data" in payload:
                    return payload.get("data")
            return payload
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: method={method} path={path} error={e}")

    def _ensure_auth_token(self) -> str:
        if self._auth_token:
            return self._auth_token

        payload = self._make_request(
            "POST",
            "/api/public/genToken",
            json={
                "email": self.config["admin_email"],
                "password": self.config["admin_password"],
            },
        )
        token = str(
            (payload or {}).get("token")
            or (payload or {}).get("accessToken")
            or (payload or {}).get("authorization")
            or ""
        ).strip()
        if not token:
            raise EmailServiceError("CloudMail 登录成功但未返回 token")

        self._auth_token = token
        return token

    def _generate_local_part(self) -> str:
        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
        return f"{first}{rest}"

    def _cache_account(self, account_info: Dict[str, Any]) -> None:
        account_id = str(account_info.get("account_id") or account_info.get("service_id") or "").strip()
        email = str(account_info.get("email") or "").strip().lower()
        if account_id:
            self._accounts_by_id[account_id] = account_info
        if email:
            self._accounts_by_email[email] = account_info

    def _get_account(self, email: Optional[str] = None, email_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if email_id:
            cached = self._accounts_by_id.get(str(email_id).strip())
            if cached:
                return cached
        if email:
            cached = self._accounts_by_email.get(str(email).strip().lower())
            if cached:
                return cached
        return None

    def _parse_email_items(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("records", "list", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

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

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        self._ensure_auth_token()

        request_config = config or {}
        local_part = str(
            request_config.get("name")
            or request_config.get("email_prefix")
            or request_config.get("address")
            or self._generate_local_part()
        ).strip().lower()
        domain = str(
            request_config.get("domain")
            or request_config.get("default_domain")
            or self.config["domain"]
        ).strip().lstrip("@")
        email = f"{local_part}@{domain}"

        add_result = self._make_request(
            "POST",
            "/api/public/addUser",
            json={
                "list": [
                    {
                        "email": email,
                    }
                ]
            },
        )

        account_info = {
            "email": email,
            "service_id": str(
                (add_result or {}).get("accountId")
                or (add_result or {}).get("id")
                or email
            ).strip(),
            "id": str(
                (add_result or {}).get("accountId")
                or (add_result or {}).get("id")
                or email
            ).strip(),
            "account_id": str(
                (add_result or {}).get("accountId")
                or (add_result or {}).get("id")
                or email
            ).strip(),
            "created_at": time.time(),
            "raw_account": add_result,
        }
        self._cache_account(account_info)
        self.update_status(True)
        return account_info

    def list_emails(self, limit: int = 100, offset: int = 0, **kwargs) -> List[Dict[str, Any]]:
        # Cloud Mail 公开接口不提供账号列表，这里返回本进程已创建的邮箱缓存。
        return list(self._accounts_by_email.values())

    def delete_email(self, email_id: str) -> bool:
        account = self._get_account(email_id=email_id)
        if account and account.get("email"):
            self._accounts_by_email.pop(str(account["email"]).strip().lower(), None)
        if account and account.get("account_id"):
            self._accounts_by_id.pop(str(account["account_id"]).strip(), None)
        return bool(account)

    def check_health(self) -> bool:
        try:
            self._ensure_auth_token()
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"CloudMail 健康检查失败: {e}")
            self.update_status(False, e)
            return False

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        self._ensure_auth_token()
        account = self._get_account(email=email, email_id=email_id)
        account_id = str((account or {}).get("account_id") or email_id or "").strip()
        if not account_id:
            for item in self.list_emails(limit=100, offset=0):
                if str(item.get("email") or "").strip().lower() == str(email or "").strip().lower():
                    account_id = str(item.get("account_id") or item.get("id") or "").strip()
                    break

        if not account_id:
            logger.warning(f"CloudMail 未找到邮箱账号: {email}")
            return None

        start_time = time.time()
        seen_ids: set[str] = set()
        last_used_id = self._last_used_mail_ids.get(str(email).strip().lower())

        while time.time() - start_time < timeout:
            try:
                latest = self._make_request(
                    "POST",
                    "/api/public/emailList",
                    json={
                        "toEmail": email,
                        "timeSort": "desc",
                        "type": 0,
                        "isDel": 0,
                        "num": 1,
                        "size": 20,
                    },
                )

                items = latest if isinstance(latest, list) else [latest]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    mail_id = str(item.get("id") or item.get("emailId") or item.get("sort") or "").strip()
                    if not mail_id:
                        mail_id = f"latest:{item.get('subject')}:{item.get('time')}"
                    if mail_id in seen_ids or (last_used_id and mail_id == last_used_id):
                        continue

                    mail_ts = self._parse_message_time(
                        item.get("sendTime")
                        or item.get("createTime")
                        or item.get("time")
                        or item.get("date")
                    )
                    if otp_sent_at and mail_ts and mail_ts + 1 < otp_sent_at:
                        continue

                    seen_ids.add(mail_id)
                    sender = str(item.get("sendEmail") or item.get("from") or item.get("sender") or "").strip()
                    subject = str(item.get("subject") or "").strip()
                    body = unescape(
                        str(item.get("content") or item.get("text") or item.get("html") or "").strip()
                    )
                    searchable = "\n".join([sender, subject, body]).lower()
                    if "openai" not in searchable:
                        continue

                    code = self._extract_otp_code(searchable, pattern)
                    if code:
                        self._last_used_mail_ids[str(email).strip().lower()] = mail_id
                        self.update_status(True)
                        return code
            except Exception as e:
                logger.debug(f"CloudMail 轮询验证码失败: {e}")

            time.sleep(3)

        return None
