"""
Freemail service implementation.
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


class FreemailService(BaseEmailService):
    """Freemail temporary mailbox service."""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.FREEMAIL, name)

        required_keys = ["base_url", "admin_token"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"Missing required config keys: {missing_keys}")

        default_config = {
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        self._domains: List[str] = []
        self._last_used_message_ids: Dict[str, str] = {}

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config['admin_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _make_request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.config['base_url']}{path}"
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._get_headers())

        try:
            response = self.http_client.request(method, url, **kwargs)

            if response.status_code >= 400:
                error_msg = f"Request failed: {response.status_code}"
                try:
                    error_msg = f"{error_msg} - {response.json()}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                self.update_status(False, EmailServiceError(error_msg))
                raise EmailServiceError(error_msg)

            try:
                return response.json()
            except Exception:
                return {"raw_response": response.text}
        except Exception as exc:
            self.update_status(False, exc)
            if isinstance(exc, EmailServiceError):
                raise
            raise EmailServiceError(f"Request failed: {method} {path} - {exc}")

    def _ensure_domains(self) -> None:
        if self._domains:
            return
        try:
            domains = self._make_request("GET", "/api/domains")
            if isinstance(domains, list):
                self._domains = [str(item) for item in domains if item]
        except Exception as exc:
            logger.warning("Failed to load Freemail domains: %s", exc)

    def _strip_html(self, value: Any) -> str:
        return unescape(re.sub(r"<[^>]+>", " ", str(value or "")))

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

    def _extract_message_timestamp(
        self,
        mail: Dict[str, Any],
        detail: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        values: List[Any] = []
        if isinstance(mail, dict):
            values.extend(
                [
                    mail.get("created_at"),
                    mail.get("createdAt"),
                    mail.get("timestamp"),
                    mail.get("received_at"),
                    mail.get("receivedAt"),
                    mail.get("date"),
                ]
            )
        if isinstance(detail, dict):
            values.extend(
                [
                    detail.get("created_at"),
                    detail.get("createdAt"),
                    detail.get("timestamp"),
                    detail.get("received_at"),
                    detail.get("receivedAt"),
                    detail.get("date"),
                ]
            )
        for value in values:
            ts = self._parse_message_time(value)
            if ts is not None:
                return ts
        return None

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
        self._ensure_domains()

        req_config = config or {}
        domain_index = 0
        target_domain = req_config.get("domain") or self.config.get("domain")

        if target_domain and self._domains:
            for index, domain in enumerate(self._domains):
                if domain == target_domain:
                    domain_index = index
                    break

        prefix = req_config.get("name")
        try:
            if prefix:
                resp = self._make_request(
                    "POST",
                    "/api/create",
                    json={"local": prefix, "domainIndex": domain_index},
                )
            else:
                params = {"domainIndex": domain_index}
                length = req_config.get("length")
                if length:
                    params["length"] = length
                resp = self._make_request("GET", "/api/generate", params=params)

            email = str(resp.get("email") or "").strip()
            if not email:
                raise EmailServiceError(f"Freemail create_email missing email: {resp}")

            email_info = {
                "email": email,
                "service_id": email,
                "id": email,
                "created_at": time.time(),
            }
            logger.info("Created Freemail mailbox: %s", email)
            self.update_status(True)
            return email_info
        except Exception as exc:
            self.update_status(False, exc)
            if isinstance(exc, EmailServiceError):
                raise
            raise EmailServiceError(f"Failed to create mailbox: {exc}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        logger.info("Polling Freemail OTP for %s", email)

        start_time = time.time()
        seen_mail_ids: set = set()
        last_used_message_id = self._last_used_message_ids.get(str(email).strip().lower())
        unknown_ts_grace_seconds = 15

        while time.time() - start_time < timeout:
            try:
                mails = self._make_request("GET", "/api/emails", params={"mailbox": email, "limit": 20})
                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                candidates: List[Dict[str, Any]] = []
                unknown_ts_candidates: List[Dict[str, Any]] = []

                for mail in mails:
                    mail_id = str(mail.get("id") or "").strip()
                    if not mail_id or mail_id in seen_mail_ids:
                        continue
                    if last_used_message_id and mail_id == last_used_message_id:
                        continue

                    seen_mail_ids.add(mail_id)

                    message_ts = self._extract_message_timestamp(mail)
                    if otp_sent_at and message_ts and message_ts + 2 < otp_sent_at:
                        continue

                    sender = str(mail.get("sender") or "").lower()
                    subject = str(mail.get("subject") or "")
                    preview = str(mail.get("preview") or "")
                    summary_content = f"{sender}\n{subject}\n{preview}".strip()

                    detail = None
                    try:
                        detail = self._make_request("GET", f"/api/email/{mail_id}")
                    except Exception as exc:
                        logger.debug("Freemail detail fetch failed: mail_id=%s error=%s", mail_id, exc)

                    detail_text = ""
                    if isinstance(detail, dict):
                        detail_text = "\n".join(
                            [
                                str(detail.get("subject") or ""),
                                str(detail.get("content") or ""),
                                self._strip_html(detail.get("html_content")),
                            ]
                        ).strip()

                    content = "\n".join(part for part in [summary_content, detail_text] if part).strip()
                    if not self._is_openai_otp_mail(content):
                        continue

                    detail_ts = self._extract_message_timestamp(mail, detail)
                    if detail_ts is not None:
                        message_ts = detail_ts

                    if isinstance(detail, dict):
                        v_code = str(mail.get("verification_code") or detail.get("verification_code") or "").strip()
                    else:
                        v_code = str(mail.get("verification_code") or "").strip()

                    if v_code:
                        code = v_code
                        semantic_hit = True
                    else:
                        code, semantic_hit = self._extract_otp_code(content, pattern)

                    if not code:
                        continue

                    candidate = {
                        "mail_id": mail_id,
                        "code": code,
                        "mail_ts": message_ts,
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
                            1 if item.get("mail_ts") is not None else 0,
                            float(item.get("mail_ts") or 0.0),
                            1 if item.get("semantic_hit") else 0,
                        ),
                        reverse=True,
                    )[0]
                    self._last_used_message_ids[str(email).strip().lower()] = str(best["mail_id"])
                    logger.info(
                        "Freemail OTP selected: email=%s code=%s mail_id=%s ts=%s semantic=%s",
                        email,
                        best["code"],
                        best["mail_id"],
                        best.get("mail_ts"),
                        best.get("semantic_hit"),
                    )
                    self.update_status(True)
                    return str(best["code"])
            except Exception as exc:
                logger.debug("Freemail polling failed: %s", exc)

            time.sleep(3)

        logger.warning("Freemail OTP timed out: %s", email)
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        try:
            params = {"limit": kwargs.get("limit", 100), "offset": kwargs.get("offset", 0)}
            resp = self._make_request("GET", "/api/mailboxes", params=params)

            emails: List[Dict[str, Any]] = []
            if isinstance(resp, list):
                for mail in resp:
                    address = mail.get("address")
                    if address:
                        emails.append(
                            {
                                "id": address,
                                "service_id": address,
                                "email": address,
                                "created_at": mail.get("created_at"),
                                "raw_data": mail,
                            }
                        )
            self.update_status(True)
            return emails
        except Exception as exc:
            logger.warning("Failed to list Freemail mailboxes: %s", exc)
            self.update_status(False, exc)
            return []

    def delete_email(self, email_id: str) -> bool:
        try:
            self._make_request("DELETE", "/api/mailboxes", params={"address": email_id})
            logger.info("Deleted Freemail mailbox: %s", email_id)
            self.update_status(True)
            return True
        except Exception as exc:
            logger.warning("Failed to delete Freemail mailbox: %s", exc)
            self.update_status(False, exc)
            return False

    def check_health(self) -> bool:
        try:
            self._make_request("GET", "/api/domains")
            self.update_status(True)
            return True
        except Exception as exc:
            logger.warning("Freemail health check failed: %s", exc)
            self.update_status(False, exc)
            return False
