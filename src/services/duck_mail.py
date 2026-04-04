"""
DuckMail service implementation.
"""

import logging
import random
import re
import string
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN, OTP_CODE_SEMANTIC_PATTERN
from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)


class DuckMailService(BaseEmailService):
    """DuckMail temporary mailbox service."""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.DUCK_MAIL, name)

        required_keys = ["base_url", "default_domain"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"Missing required config keys: {missing_keys}")

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
        self._last_used_message_ids: Dict[str, str] = {}

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
                error_message = f"API request failed: {response.status_code}"
                try:
                    error_message = f"{error_message} - {response.json()}"
                except Exception:
                    error_message = f"{error_message} - {response.text[:200]}"
                raise EmailServiceError(error_message)

            try:
                return response.json()
            except Exception:
                return {"raw_response": response.text}
        except Exception as exc:
            self.update_status(False, exc)
            if isinstance(exc, EmailServiceError):
                raise
            raise EmailServiceError(f"Request failed: {method} {path} - {exc}")

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

    def _get_account_info(
        self,
        email: Optional[str] = None,
        email_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
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

        if text.isdigit():
            ts = float(text)
            if ts > 10**12:
                ts /= 1000.0
            return ts if ts > 0 else None

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    def _message_search_text(self, summary: Dict[str, Any], detail: Dict[str, Any]) -> str:
        sender = summary.get("from") or detail.get("from") or {}
        if isinstance(sender, dict):
            sender_text = " ".join(str(sender.get(key) or "") for key in ("name", "address")).strip()
        else:
            sender_text = str(sender or "")

        subject = str(summary.get("subject") or detail.get("subject") or "")
        text_body = str(detail.get("text") or "")
        html_body = self._strip_html(detail.get("html"))
        return "\n".join(part for part in [sender_text, subject, text_body, html_body] if part).strip()

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
            "otp",
            "code is",
            "验证码",
        )
        return any(keyword in text for keyword in keywords)

    def _extract_otp_code(self, content: str, pattern: str) -> Tuple[Optional[str], bool]:
        text = str(content or "")
        if not text:
            return None, False

        semantic_match = re.search(OTP_CODE_SEMANTIC_PATTERN, text, re.IGNORECASE)
        if semantic_match:
            return semantic_match.group(1), True

        simple_match = re.search(pattern, text)
        if simple_match:
            return simple_match.group(1), False
        return None, False

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        request_config = config or {}
        local_part = str(request_config.get("name") or self._generate_local_part()).strip()
        domain = str(
            request_config.get("default_domain")
            or request_config.get("domain")
            or self.config["default_domain"]
        ).strip().lstrip("@")
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
            raise EmailServiceError("DuckMail create_email returned incomplete data")

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
            logger.warning("DuckMail mailbox cache missing: email=%s email_id=%s", email, email_id)
            return None

        token = account_info.get("token")
        if not token:
            logger.warning("DuckMail mailbox token missing: email=%s", email)
            return None

        start_time = time.time()
        seen_message_ids = set()
        last_used_message_id = self._last_used_message_ids.get(str(email).strip().lower())
        unknown_ts_grace_seconds = 15

        while time.time() - start_time < timeout:
            try:
                response = self._make_request(
                    "GET",
                    "/messages",
                    token=token,
                    params={"page": 1},
                )
                messages = response.get("hydra:member", [])
                candidates: List[Dict[str, Any]] = []
                unknown_ts_candidates: List[Dict[str, Any]] = []

                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen_message_ids:
                        continue
                    if last_used_message_id and message_id == last_used_message_id:
                        continue

                    message_ts = self._parse_message_time(message.get("createdAt"))
                    if otp_sent_at and message_ts and message_ts + 2 < otp_sent_at:
                        continue

                    seen_message_ids.add(message_id)
                    detail = self._make_request("GET", f"/messages/{message_id}", token=token)

                    content = self._message_search_text(message, detail)
                    if not self._is_openai_otp_mail(content):
                        continue

                    detail_ts = self._parse_message_time(
                        detail.get("createdAt") or detail.get("created_at") or detail.get("date")
                    )
                    if detail_ts is not None:
                        message_ts = detail_ts

                    code, semantic_hit = self._extract_otp_code(content, pattern)
                    if not code:
                        continue

                    candidate = {
                        "message_id": message_id,
                        "code": code,
                        "message_ts": message_ts,
                        "semantic_hit": bool(semantic_hit),
                        "is_recent": bool(
                            otp_sent_at and (message_ts is not None) and (message_ts + 2 >= otp_sent_at)
                        ),
                    }
                    if otp_sent_at and message_ts is None:
                        unknown_ts_candidates.append(candidate)
                    else:
                        candidates.append(candidate)

                elapsed = time.time() - start_time
                if otp_sent_at and (not candidates) and unknown_ts_candidates and elapsed < unknown_ts_grace_seconds:
                    time.sleep(3)
                    continue

                all_candidates = candidates + unknown_ts_candidates
                if all_candidates:
                    best = sorted(
                        all_candidates,
                        key=lambda item: (
                            1 if item.get("is_recent") else 0,
                            1 if item.get("message_ts") is not None else 0,
                            float(item.get("message_ts") or 0.0),
                            1 if item.get("semantic_hit") else 0,
                        ),
                        reverse=True,
                    )[0]
                    self._last_used_message_ids[str(email).strip().lower()] = str(best["message_id"])
                    logger.info(
                        "DuckMail OTP selected: email=%s code=%s message_id=%s ts=%s semantic=%s",
                        email,
                        best["code"],
                        best["message_id"],
                        best.get("message_ts"),
                        best.get("semantic_hit"),
                    )
                    self.update_status(True)
                    return str(best["code"])
            except Exception as exc:
                logger.debug("DuckMail polling failed: %s", exc)

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
            self._make_request("DELETE", f"/accounts/{account_id}", token=token)
            self._accounts_by_id.pop(str(account_id), None)
            self._accounts_by_email.pop(str(account_info.get("email") or "").lower(), None)
            self.update_status(True)
            return True
        except Exception as exc:
            logger.warning("DuckMail delete_email failed: %s", exc)
            self.update_status(False, exc)
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
        except Exception as exc:
            logger.warning("DuckMail health check failed: %s", exc)
            self.update_status(False, exc)
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
        return self._make_request("GET", f"/messages/{message_id}", token=account_info["token"])

    def get_service_info(self) -> Dict[str, Any]:
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config["base_url"],
            "default_domain": self.config["default_domain"],
            "cached_accounts": len(self._accounts_by_email),
            "status": self.status.value,
        }
