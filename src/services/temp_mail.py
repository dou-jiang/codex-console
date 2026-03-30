"""
Temp-Mail 邮箱服务实现
基于自部署 Cloudflare Worker 临时邮箱服务
接口文档参见 plan/temp-mail.md
"""

import re
import time
import json
import logging
from datetime import datetime, timezone
from email import message_from_string
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as email_policy
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Optional, Dict, Any, List, Tuple

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN, OTP_CODE_SEMANTIC_PATTERN


logger = logging.getLogger(__name__)


class TempMailService(BaseEmailService):
    """
    Temp-Mail 邮箱服务
    基于自部署 Cloudflare Worker 的临时邮箱，admin 模式管理邮箱
    不走代理，不使用 requests 库
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 TempMail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: Worker 域名地址，如 https://mail.example.com (必需)
                - admin_password: Admin 密码，对应 x-admin-auth header (必需)
                - domain: 邮箱域名，如 example.com (必需)
                - enable_prefix: 是否启用前缀，默认 True
                - timeout: 请求超时时间，默认 30
                - max_retries: 最大重试次数，默认 3
            name: 服务名称
        """
        super().__init__(EmailServiceType.TEMP_MAIL, name)

        required_keys = ["base_url", "admin_password", "domain"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "enable_prefix": True,
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}

        # 不走代理，proxy_url=None
        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        # 邮箱缓存：email -> {jwt, address}
        self._email_cache: Dict[str, Dict[str, Any]] = {}
        # 记录每个邮箱上一次成功使用的邮件 ID，避免重复使用旧验证码
        self._last_used_mail_ids: Dict[str, str] = {}
        # /admin/mails 接口对 limit 参数较严格，这里统一限制上限，避免 400 Invalid limit
        self._admin_mails_limit_max = 50

    def _normalize_admin_limit(self, value: Any, default: int = 50) -> int:
        try:
            number = int(value)
        except Exception:
            number = int(default)
        if number <= 0:
            number = int(default)
        return max(1, min(number, int(self._admin_mails_limit_max)))

    def _normalize_offset(self, value: Any, default: int = 0) -> int:
        try:
            number = int(value)
        except Exception:
            number = int(default)
        return max(0, number)

    def _request_admin_mails_with_limit_fallback(
        self,
        *,
        offset: int = 0,
        extra_params: Optional[Dict[str, Any]] = None,
        preferred_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        /admin/mails 在不同部署版本对 limit 校验不一致。
        这里按降级 limit 自动重试，避免直接报 400 Invalid limit。
        """
        offset_value = self._normalize_offset(offset, default=0)
        base_params: Dict[str, Any] = dict(extra_params or {})
        candidate_limits: List[int] = []
        if preferred_limit is not None:
            candidate_limits.append(self._normalize_admin_limit(preferred_limit, default=50))
        candidate_limits.extend([50, 40, 30, 20, 10, 5, 1])
        # 去重且保持顺序
        seen = set()
        limits = []
        for value in candidate_limits:
            v = int(max(1, value))
            if v in seen:
                continue
            seen.add(v)
            limits.append(v)

        last_error: Optional[Exception] = None
        for index, limit_value in enumerate(limits):
            params = dict(base_params)
            params["limit"] = limit_value
            params["offset"] = offset_value
            try:
                return self._make_request("GET", "/admin/mails", params=params)
            except Exception as e:
                last_error = e
                err_text = str(e).lower()
                retryable_invalid_limit = ("invalid limit" in err_text) and (index < len(limits) - 1)
                if retryable_invalid_limit:
                    logger.debug(
                        "TempMail /admin/mails limit=%s 不可用，降级重试下一档",
                        limit_value,
                    )
                    continue
                raise

        if last_error:
            raise last_error
        raise EmailServiceError("admin mails request failed")

    def _decode_mime_header(self, value: str) -> str:
        """解码 MIME 头，兼容 RFC 2047 编码主题。"""
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    def _extract_body_from_message(self, message: Message) -> str:
        """从 MIME 邮件对象中提取可读正文。"""
        parts: List[str] = []

        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue

                content_type = (part.get_content_type() or "").lower()
                if content_type not in ("text/plain", "text/html"):
                    continue

                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace") if payload else ""
                except Exception:
                    try:
                        text = part.get_content()
                    except Exception:
                        text = ""

                if content_type == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                parts.append(text)
        else:
            try:
                payload = message.get_payload(decode=True)
                charset = message.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace") if payload else ""
            except Exception:
                try:
                    body = message.get_content()
                except Exception:
                    body = str(message.get_payload() or "")

            if "html" in (message.get_content_type() or "").lower():
                body = re.sub(r"<[^>]+>", " ", body)
            parts.append(body)

        return unescape("\n".join(part for part in parts if part).strip())

    def _extract_mail_fields(self, mail: Dict[str, Any]) -> Dict[str, str]:
        """统一提取邮件字段，兼容 raw MIME 和不同 Worker 返回格式。"""
        sender = str(
            mail.get("source")
            or mail.get("from")
            or mail.get("from_address")
            or mail.get("fromAddress")
            or ""
        ).strip()
        subject = str(mail.get("subject") or mail.get("title") or "").strip()
        body_text = str(
            mail.get("text")
            or mail.get("body")
            or mail.get("content")
            or mail.get("html")
            or ""
        ).strip()
        raw = str(mail.get("raw") or "").strip()

        if raw:
            try:
                message = message_from_string(raw, policy=email_policy)
                sender = sender or self._decode_mime_header(message.get("From", ""))
                subject = subject or self._decode_mime_header(message.get("Subject", ""))
                parsed_body = self._extract_body_from_message(message)
                if parsed_body:
                    body_text = f"{body_text}\n{parsed_body}".strip() if body_text else parsed_body
            except Exception as e:
                logger.debug(f"解析 TempMail raw 邮件失败: {e}")
                body_text = f"{body_text}\n{raw}".strip() if body_text else raw

        body_text = unescape(re.sub(r"<[^>]+>", " ", body_text))
        return {
            "sender": sender,
            "subject": subject,
            "body": body_text,
            "raw": raw,
        }

    def _is_openai_otp_mail(self, sender: str, subject: str, body: str, raw: str) -> bool:
        """
        判断是否是 OpenAI 验证码邮件。
        只看 openai 关键字容易误命中营销/通知邮件，这里增加 OTP 语义词过滤。
        """
        sender_l = str(sender or "").lower()
        subject_l = str(subject or "").lower()
        body_l = str(body or "").lower()
        raw_l = str(raw or "").lower()
        blob = f"{sender_l}\n{subject_l}\n{body_l}\n{raw_l}"

        if "openai" not in sender_l and "openai" not in blob:
            return False

        otp_keywords = (
            "verification code",
            "verify",
            "one-time code",
            "one time code",
            "otp",
            "log in",
            "login",
            "security code",
            "验证码",
        )
        return any(keyword in blob for keyword in otp_keywords)

    def _extract_otp_code(self, content: str, pattern: str) -> Tuple[Optional[str], bool]:
        """
        提取验证码并返回是否语义命中。
        优先语义匹配（code is 123456），降低误匹配邮件正文中随机 6 位数字的概率。
        """
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

    def _admin_headers(self) -> Dict[str, str]:
        """构造 admin 请求头"""
        headers = {
            "x-admin-auth": self.config["admin_password"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        custom_auth = (self.config.get("custom_auth") or "").strip()
        if custom_auth:
            headers["x-custom-auth"] = custom_auth
        return headers

    def _extract_mails_from_response(self, response: Any) -> List[Dict[str, Any]]:
        """
        从不同返回结构中提取邮件列表。

        兼容以下常见格式：
        - {"results": [...]}
        - {"mails": [...]}
        - {"data": [...]}
        - {"items": [...]}
        - 直接返回 [...]
        """
        if isinstance(response, list):
            return [mail for mail in response if isinstance(mail, dict)]

        if not isinstance(response, dict):
            return []

        for key in ("results", "mails", "data", "items", "list"):
            value = response.get(key)
            if isinstance(value, list):
                return [mail for mail in value if isinstance(mail, dict)]

        return []

    def _mail_appears_for_email(self, mail: Dict[str, Any], email: str) -> bool:
        """判断邮件是否属于指定邮箱。"""
        target = (email or "").strip().lower()
        if not target:
            return False

        candidate_fields = (
            mail.get("address"),
            mail.get("email"),
            mail.get("to"),
            mail.get("to_address"),
            mail.get("toAddress"),
            mail.get("target"),
            mail.get("recipient"),
        )
        for value in candidate_fields:
            text = str(value or "").strip().lower()
            if text and target in text:
                return True

        parsed = self._extract_mail_fields(mail)
        text_blob = "\n".join(
            [
                str(parsed.get("sender") or ""),
                str(parsed.get("subject") or ""),
                str(parsed.get("body") or ""),
                str(parsed.get("raw") or ""),
            ]
        ).lower()
        return target in text_blob

    def _fetch_mails_once(self, email: str, jwt: Optional[str], email_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取一次邮件列表，按新旧接口顺序回退:
        1) /api/mails (Bearer jwt)
        2) /user_api/mails (x-user-token)
        3) /admin/mails (按地址过滤)
        4) /admin/mails (不过滤，客户端二次筛选)
        """
        attempts: List[Dict[str, Any]] = []
        admin_limit = self._normalize_admin_limit(self.config.get("admin_mails_limit", 50), default=50)
        if jwt:
            attempts.extend([
                {
                    "path": "/api/mails",
                    "params": {"limit": 50, "offset": 0},
                    "headers": {
                        "Authorization": f"Bearer {jwt}",
                        "Accept": "application/json",
                    },
                },
                {
                    "path": "/user_api/mails",
                    "params": {"limit": 50, "offset": 0},
                    "headers": {
                        "x-user-token": jwt,
                        "Accept": "application/json",
                    },
                },
            ])

        attempts.append({
            "path": "/admin/mails",
            "params": {"limit": admin_limit, "offset": 0, "address": email},
            "headers": {"Accept": "application/json"},
        })
        if email_id and email_id != email:
            attempts.append({
                "path": "/admin/mails",
                "params": {"limit": admin_limit, "offset": 0, "address": email_id},
                "headers": {"Accept": "application/json"},
            })
        attempts.append({
            "path": "/admin/mails",
            "params": {"limit": admin_limit, "offset": 0},
            "headers": {"Accept": "application/json"},
        })

        for attempt in attempts:
            path = attempt["path"]
            try:
                if path == "/admin/mails":
                    raw_params = dict(attempt["params"] or {})
                    preferred_limit = raw_params.pop("limit", admin_limit)
                    offset = raw_params.pop("offset", 0)
                    response = self._request_admin_mails_with_limit_fallback(
                        offset=offset,
                        extra_params=raw_params,
                        preferred_limit=preferred_limit,
                    )
                else:
                    response = self._make_request(
                        "GET",
                        path,
                        params=attempt["params"],
                        headers=attempt["headers"],
                    )
                mails = self._extract_mails_from_response(response)
                if mails and "address" not in attempt["params"]:
                    mails = [mail for mail in mails if self._mail_appears_for_email(mail, email)]
                if mails:
                    return mails
                logger.debug(f"TempMail 接口 {path} 返回无可用邮件列表: {response}")
            except Exception as e:
                logger.debug(f"TempMail 接口 {path} 读取失败，尝试回退: {e}")

        return []

    def _extract_mail_detail_from_response(self, response: Any) -> Optional[Dict[str, Any]]:
        """从详情接口响应里提取单封邮件对象。"""
        if isinstance(response, dict):
            if response:
                if all(k in response for k in ("subject", "text")) or response.get("raw"):
                    return response
                for key in ("mail", "data", "result", "item"):
                    value = response.get(key)
                    if isinstance(value, dict):
                        return value
        return None

    def _fetch_mail_detail(self, mail_id: str, jwt: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        尝试获取单封邮件详情（部分部署的列表接口只返回摘要，不含正文）。
        """
        if not mail_id:
            return None

        attempts: List[Dict[str, Any]] = []
        if jwt:
            attempts.extend([
                {
                    "path": f"/api/mails/{mail_id}",
                    "headers": {
                        "Authorization": f"Bearer {jwt}",
                        "Accept": "application/json",
                    },
                },
                {
                    "path": f"/user_api/mails/{mail_id}",
                    "headers": {
                        "x-user-token": jwt,
                        "Accept": "application/json",
                    },
                },
            ])
        attempts.append(
            {
                "path": f"/admin/mails/{mail_id}",
                "headers": {"Accept": "application/json"},
            }
        )

        for attempt in attempts:
            try:
                response = self._make_request("GET", attempt["path"], headers=attempt["headers"])
                detail = self._extract_mail_detail_from_response(response)
                if detail:
                    return detail
            except Exception as e:
                logger.debug(f"TempMail 详情接口 {attempt['path']} 读取失败: {e}")
        return None

    def _parse_mail_timestamp(self, value: Any) -> Optional[float]:
        """将邮件时间字段解析为 Unix 时间戳（秒）。"""
        if value is None:
            return None

        if isinstance(value, (int, float)):
            ts = float(value)
            # 兼容毫秒时间戳
            if ts > 10**12:
                ts = ts / 1000.0
            return ts if ts > 0 else None

        text = str(value).strip()
        if not text:
            return None

        if text.isdigit():
            ts = float(text)
            if ts > 10**12:
                ts = ts / 1000.0
            return ts if ts > 0 else None

        try:
            ts = float(text)
            if ts > 10**12:
                ts = ts / 1000.0
            if ts > 0:
                return ts
        except ValueError:
            pass

        iso_text = text
        if iso_text.endswith("Z"):
            iso_text = iso_text[:-1] + "+00:00"

        try:
            dt = datetime.fromisoformat(iso_text)
            if dt.tzinfo is None:
                # Worker 侧常返回无时区时间，默认按 UTC 解析，避免被本机时区误差误判为旧邮件。
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue

        return None

    def _extract_mail_timestamp(self, mail: Dict[str, Any]) -> Optional[float]:
        """从不同字段中提取邮件时间戳。"""
        for key in ("createdAt", "created_at", "date", "created", "timestamp", "time"):
            ts = self._parse_mail_timestamp(mail.get(key))
            if ts is not None:
                return ts
        # 某些 Worker 只在 raw 里带 Date 头，这里做二次解析。
        raw = str(mail.get("raw") or "").strip()
        if raw:
            try:
                message = message_from_string(raw, policy=email_policy)
                date_header = str(message.get("Date") or "").strip()
                if date_header:
                    dt = parsedate_to_datetime(date_header)
                    if dt is not None:
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return dt.timestamp()
            except Exception:
                pass
        return None

    def _extract_mail_id(self, mail: Dict[str, Any]) -> str:
        """提取邮件唯一标识，兼容不同字段；缺失时生成稳定回退 ID。"""
        for key in ("id", "mail_id", "mailId", "_id", "uuid"):
            value = mail.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()

        fallback = "|".join(
            str(mail.get(key) or "").strip()
            for key in ("createdAt", "created_at", "date", "source", "from", "subject", "title")
        )
        return fallback or str(hash(json.dumps(mail, sort_keys=True, ensure_ascii=False)))

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
        base_url = self.config["base_url"].rstrip("/")
        url = f"{base_url}{path}"

        # 合并默认 admin headers
        kwargs.setdefault("headers", {})
        for k, v in self._admin_headers().items():
            kwargs["headers"].setdefault(k, v)

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
            except json.JSONDecodeError:
                return {"raw_response": response.text}

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        通过 admin API 创建临时邮箱

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - jwt: 用户级 JWT token
            - service_id: 同 email（用作标识）
        """
        import random
        import string

        # 生成随机邮箱名
        letters = ''.join(random.choices(string.ascii_lowercase, k=5))
        digits = ''.join(random.choices(string.digits, k=random.randint(1, 3)))
        suffix = ''.join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))
        name = letters + digits + suffix

        domain = self.config["domain"]
        enable_prefix = self.config.get("enable_prefix", True)

        body = {
            "enablePrefix": enable_prefix,
            "name": name,
            "domain": domain,
        }

        try:
            response = self._make_request("POST", "/admin/new_address", json=body)

            address = response.get("address", "").strip()
            jwt = response.get("jwt", "").strip()
            address_id = str(
                response.get("address_id")
                or response.get("id")
                or response.get("addressId")
                or ""
            ).strip()

            if not address:
                raise EmailServiceError(f"API 返回数据不完整: {response}")

            email_info = {
                "email": address,
                "jwt": jwt,
                "address_id": address_id,
                "service_id": address,
                "id": address,
                "created_at": time.time(),
            }

            # 缓存 jwt，供获取验证码时使用
            self._email_cache[address] = email_info

            logger.info(f"成功创建 TempMail 邮箱: {address}")
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
        从 TempMail 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用，保留接口兼容
            timeout: 超时时间（秒）
            pattern: 验证码正则
            otp_sent_at: OTP 发送时间戳（用于过滤旧邮件）

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info(f"正在从 TempMail 邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_mail_ids: set = set()
        last_used_mail_id = self._last_used_mail_ids.get(email)
        unknown_ts_grace_seconds = 15

        # 优先使用用户级 JWT，回退到 admin API
        cached = self._email_cache.get(email, {})
        jwt = cached.get("jwt")
        address_id = (
            str(cached.get("address_id") or "").strip()
            or str(email_id or "").strip()
            or None
        )
        poll_count = 0

        while time.time() - start_time < timeout:
            poll_count += 1
            try:
                mails = self._fetch_mails_once(email=email, jwt=jwt, email_id=address_id)
                if not mails:
                    if poll_count == 1 or poll_count % 5 == 0:
                        logger.info(
                            f"TempMail 轮询[{email}] 第 {poll_count} 次: 暂无邮件（已等待 {int(time.time() - start_time)}s）"
                        )
                    time.sleep(3)
                    continue

                if poll_count == 1 or poll_count % 3 == 0:
                    logger.info(
                        f"TempMail 轮询[{email}] 第 {poll_count} 次: 收到 {len(mails)} 封候选邮件"
                    )

                candidates: List[Dict[str, Any]] = []
                unknown_ts_candidates: List[Dict[str, Any]] = []

                for mail in mails:
                    mail_id = self._extract_mail_id(mail)
                    if mail_id in seen_mail_ids:
                        continue

                    if last_used_mail_id and mail_id == last_used_mail_id:
                        continue

                    seen_mail_ids.add(mail_id)

                    # 过滤发送验证码之前的旧邮件，避免取到上一轮 OTP
                    mail_ts = self._extract_mail_timestamp(mail)
                    if otp_sent_at:
                        if mail_ts is not None and mail_ts + 2 < otp_sent_at:
                            continue

                    parsed = self._extract_mail_fields(mail)
                    sender = parsed["sender"].lower()
                    subject = parsed["subject"]
                    body_text = parsed["body"]
                    raw_text = parsed["raw"]
                    content = f"{sender}\n{subject}\n{body_text}\n{raw_text}".strip()

                    # 只处理 OpenAI 验证码类邮件（避免误命中通知类邮件）
                    if not self._is_openai_otp_mail(sender, subject, body_text, raw_text):
                        continue

                    code, semantic_hit = self._extract_otp_code(content, pattern)
                    if not code:
                        # 部分部署列表接口只含摘要；尝试拉单封详情再匹配一次。
                        detail = self._fetch_mail_detail(mail_id=mail_id, jwt=jwt)
                        if detail:
                            detail_parsed = self._extract_mail_fields(detail)
                            detail_ts = self._extract_mail_timestamp(detail)
                            if detail_ts is not None:
                                mail_ts = detail_ts
                            detail_content = (
                                f"{detail_parsed['sender']}\n"
                                f"{detail_parsed['subject']}\n"
                                f"{detail_parsed['body']}\n"
                                f"{detail_parsed['raw']}"
                            ).strip()
                            if not self._is_openai_otp_mail(
                                detail_parsed["sender"],
                                detail_parsed["subject"],
                                detail_parsed["body"],
                                detail_parsed["raw"],
                            ):
                                continue
                            code, semantic_hit = self._extract_otp_code(detail_content, pattern)

                    if not code:
                        continue

                    candidate = {
                        "mail_id": mail_id,
                        "code": code,
                        "mail_ts": mail_ts,
                        "semantic_hit": bool(semantic_hit),
                        "is_recent": bool(
                            otp_sent_at and (mail_ts is not None) and (mail_ts + 2 >= otp_sent_at)
                        ),
                    }
                    if otp_sent_at and mail_ts is None:
                        unknown_ts_candidates.append(candidate)
                    else:
                        candidates.append(candidate)

                elapsed = time.time() - start_time
                if otp_sent_at and (not candidates) and unknown_ts_candidates and elapsed < unknown_ts_grace_seconds:
                    # 先等一小段时间，优先等待可解析时间戳的新邮件，避免立刻捞到历史旧码。
                    logger.debug(
                        "TempMail 轮询[%s]: 存在无时间戳邮件，等待 %.0fs 后再回退使用",
                        email,
                        unknown_ts_grace_seconds,
                    )
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
                    code = str(best["code"])
                    self._last_used_mail_ids[email] = str(best["mail_id"])
                    logger.info(
                        "从 TempMail 邮箱 %s 找到验证码: %s（mail_id=%s ts=%s semantic=%s）",
                        email,
                        code,
                        best["mail_id"],
                        best.get("mail_ts"),
                        best.get("semantic_hit"),
                    )
                    self.update_status(True)
                    return code

            except Exception as e:
                logger.debug(f"检查 TempMail 邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待 TempMail 验证码超时: {email}")
        return None

    def list_emails(self, limit: int = 50, offset: int = 0, **kwargs) -> List[Dict[str, Any]]:
        """
        列出邮箱

        Args:
            limit: 返回数量上限
            offset: 分页偏移
            **kwargs: 额外查询参数，透传给 admin API

        Returns:
            邮箱列表
        """
        params = {k: v for k, v in kwargs.items() if v is not None}
        raw_limit = params.pop("limit", limit)
        raw_offset = params.pop("offset", offset)
        params["limit"] = self._normalize_admin_limit(raw_limit, default=50)
        params["offset"] = self._normalize_offset(raw_offset, default=0)

        try:
            response = self._request_admin_mails_with_limit_fallback(
                offset=params["offset"],
                extra_params={k: v for k, v in params.items() if k not in ("limit", "offset")},
                preferred_limit=params["limit"],
            )
            mails = response.get("results", [])
            if not isinstance(mails, list):
                raise EmailServiceError(f"API 返回数据格式错误: {response}")

            emails: List[Dict[str, Any]] = []
            for mail in mails:
                address = (mail.get("address") or "").strip()
                mail_id = mail.get("id") or address
                email_info = {
                    "id": mail_id,
                    "service_id": mail_id,
                    "email": address,
                    "subject": mail.get("subject"),
                    "from": mail.get("source"),
                    "created_at": mail.get("createdAt") or mail.get("created_at"),
                    "raw_data": mail,
                }
                emails.append(email_info)

                if address:
                    cached = self._email_cache.get(address, {})
                    self._email_cache[address] = {**cached, **email_info}

            self.update_status(True)
            return emails
        except Exception as e:
            logger.warning(f"列出 TempMail 邮箱失败: {e}")
            self.update_status(False, e)
            return list(self._email_cache.values())

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱

        Note:
            当前 TempMail admin API 文档未见删除地址接口，这里先从本地缓存移除，
            以满足统一接口并避免服务实例化失败。
        """
        removed = False
        emails_to_delete = []

        for address, info in self._email_cache.items():
            candidate_ids = {
                address,
                info.get("id"),
                info.get("service_id"),
            }
            if email_id in candidate_ids:
                emails_to_delete.append(address)

        for address in emails_to_delete:
            self._email_cache.pop(address, None)
            removed = True

        if removed:
            logger.info(f"已从 TempMail 缓存移除邮箱: {email_id}")
            self.update_status(True)
        else:
            logger.info(f"TempMail 缓存中未找到邮箱: {email_id}")

        return removed

    def check_health(self) -> bool:
        """检查服务健康状态"""
        try:
            self._make_request(
                "GET",
                "/admin/mails",
                params={"limit": 1, "offset": 0},
            )
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"TempMail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
