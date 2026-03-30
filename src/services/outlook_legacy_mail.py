"""
Outlook 邮箱服务实现
支持 IMAP 协议，XOAUTH2 和密码认证
"""

import imaplib
import email
import re
import time
import threading
import json
import urllib.parse
import urllib.request
import base64
import hashlib
import secrets
import logging
from typing import Optional, Dict, Any, List
from email.header import decode_header
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import (
    OTP_CODE_PATTERN,
    OTP_CODE_SIMPLE_PATTERN,
    OTP_CODE_SEMANTIC_PATTERN,
    OPENAI_EMAIL_SENDERS,
    OPENAI_VERIFICATION_KEYWORDS,
)
from ..config.settings import get_settings


def get_email_code_settings() -> dict:
    """
    获取验证码等待配置

    Returns:
        dict: 包含 timeout 和 poll_interval 的字典
    """
    settings = get_settings()
    return {
        "timeout": settings.email_code_timeout,
        "poll_interval": settings.email_code_poll_interval,
    }


logger = logging.getLogger(__name__)


class OutlookAccount:
    """Outlook 账户信息"""

    def __init__(
        self,
        email: str,
        password: str,
        client_id: str = "",
        refresh_token: str = ""
    ):
        self.email = str(email or "").strip().lower()
        self.password = password
        self.client_id = client_id
        self.refresh_token = refresh_token

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "OutlookAccount":
        """从配置创建账户"""
        return cls(
            email=str(config.get("email", "") or "").strip().lower(),
            password=config.get("password", ""),
            client_id=config.get("client_id", ""),
            refresh_token=config.get("refresh_token", "")
        )

    def has_oauth(self) -> bool:
        """是否支持 OAuth2"""
        return bool(self.client_id and self.refresh_token)

    def validate(self) -> bool:
        """验证账户信息是否有效"""
        return bool(self.email and self.password) or self.has_oauth()


class OutlookIMAPClient:
    """
    Outlook IMAP 客户端
    支持 XOAUTH2 和密码认证
    """

    # Microsoft OAuth2 Token 缓存
    _token_cache: Dict[str, tuple] = {}
    _cache_lock = threading.Lock()

    def __init__(
        self,
        account: OutlookAccount,
        host: str = "outlook.office365.com",
        port: int = 993,
        timeout: int = 20
    ):
        self.account = account
        self.host = host
        self.port = port
        self.timeout = timeout
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    @staticmethod
    def refresh_ms_token(account: OutlookAccount, timeout: int = 15) -> str:
        """刷新 Microsoft access token"""
        if not account.client_id or not account.refresh_token:
            raise RuntimeError("缺少 client_id 或 refresh_token")

        key = account.email.lower()
        with OutlookIMAPClient._cache_lock:
            cached = OutlookIMAPClient._token_cache.get(key)
            if cached and time.time() < cached[1]:
                return cached[0]

        body = urllib.parse.urlencode({
            "client_id": account.client_id,
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
            "redirect_uri": "https://login.live.com/oauth20_desktop.srf",
        }).encode()

        req = urllib.request.Request(
            "https://login.live.com/oauth20_token.srf",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            raise RuntimeError(f"MS OAuth 刷新失败: {e.code}") from e

        token = data.get("access_token")
        if not token:
            raise RuntimeError("MS OAuth 响应无 access_token")

        ttl = int(data.get("expires_in", 3600))
        with OutlookIMAPClient._cache_lock:
            OutlookIMAPClient._token_cache[key] = (token, time.time() + ttl - 120)

        return token

    @staticmethod
    def _build_xoauth2(email_addr: str, token: str) -> bytes:
        """构建 XOAUTH2 认证字符串"""
        return f"user={email_addr}\x01auth=Bearer {token}\x01\x01".encode()

    def connect(self):
        """连接到 IMAP 服务器"""
        self._conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)

        # 优先使用 XOAUTH2 认证
        if self.account.has_oauth():
            try:
                token = self.refresh_ms_token(self.account)
                self._conn.authenticate(
                    "XOAUTH2",
                    lambda _: self._build_xoauth2(self.account.email, token)
                )
                logger.debug(f"使用 XOAUTH2 认证连接: {self.account.email}")
                return
            except Exception as e:
                logger.warning(f"XOAUTH2 认证失败，回退密码认证: {e}")

        # 回退到密码认证
        self._conn.login(self.account.email, self.account.password)
        logger.debug(f"使用密码认证连接: {self.account.email}")

    def _ensure_connection(self):
        """确保连接有效"""
        if self._conn:
            try:
                self._conn.noop()
                return
            except Exception:
                self.close()

        self.connect()

    def get_recent_emails(
        self,
        count: int = 20,
        only_unseen: bool = True,
        timeout: int = 30
    ) -> List[Dict[str, Any]]:
        """
        获取最近的邮件

        Args:
            count: 获取的邮件数量
            only_unseen: 是否只获取未读邮件
            timeout: 超时时间

        Returns:
            邮件列表
        """
        self._ensure_connection()

        flag = "UNSEEN" if only_unseen else "ALL"
        self._conn.select("INBOX", readonly=True)

        _, data = self._conn.search(None, flag)
        if not data or not data[0]:
            return []

        # 获取最新的邮件
        ids = data[0].split()[-count:]
        result = []

        for mid in reversed(ids):
            try:
                _, payload = self._conn.fetch(mid, "(RFC822)")
                if not payload:
                    continue

                raw = b""
                for part in payload:
                    if isinstance(part, tuple) and len(part) > 1:
                        raw = part[1]
                        break

                if raw:
                    result.append(self._parse_email(raw))
            except Exception as e:
                logger.warning(f"解析邮件失败 (ID: {mid}): {e}")

        return result

    @staticmethod
    def _parse_email(raw: bytes) -> Dict[str, Any]:
        """解析邮件内容"""
        # 移除可能的 BOM
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]

        msg = email.message_from_bytes(raw)

        # 解析邮件头
        subject = OutlookIMAPClient._decode_header(msg.get("Subject", ""))
        sender = OutlookIMAPClient._decode_header(msg.get("From", ""))
        date_str = OutlookIMAPClient._decode_header(msg.get("Date", ""))
        to = OutlookIMAPClient._decode_header(msg.get("To", ""))
        delivered_to = OutlookIMAPClient._decode_header(msg.get("Delivered-To", ""))
        x_original_to = OutlookIMAPClient._decode_header(msg.get("X-Original-To", ""))

        # 提取邮件正文
        body = OutlookIMAPClient._extract_body(msg)

        # 解析日期
        date_timestamp = 0
        try:
            if date_str:
                dt = parsedate_to_datetime(date_str)
                date_timestamp = int(dt.timestamp())
        except Exception:
            pass

        return {
            "subject": subject,
            "from": sender,
            "date": date_str,
            "date_timestamp": date_timestamp,
            "to": to,
            "delivered_to": delivered_to,
            "x_original_to": x_original_to,
            "body": body,
            "raw": raw.hex()[:100]  # 存储原始数据的部分哈希用于调试
        }

    @staticmethod
    def _decode_header(header: str) -> str:
        """解码邮件头"""
        if not header:
            return ""

        parts = []
        for chunk, encoding in decode_header(header):
            if isinstance(chunk, bytes):
                try:
                    decoded = chunk.decode(encoding or "utf-8", errors="replace")
                    parts.append(decoded)
                except Exception:
                    parts.append(chunk.decode("utf-8", errors="replace"))
            else:
                parts.append(chunk)

        return "".join(parts).strip()

    @staticmethod
    def _extract_body(msg) -> str:
        """提取邮件正文"""
        import html as html_module

        texts = []
        parts = msg.walk() if msg.is_multipart() else [msg]

        for part in parts:
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")

            # 如果是 HTML，移除标签
            if "<html" in text.lower():
                text = re.sub(r"<[^>]+>", " ", text)

            texts.append(text)

        # 合并并清理文本
        combined = " ".join(texts)
        combined = html_module.unescape(combined)
        combined = re.sub(r"\s+", " ", combined).strip()

        return combined

    def close(self):
        """关闭连接"""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class OutlookService(BaseEmailService):
    """
    Outlook 邮箱服务
    支持多个 Outlook 账户的轮询和验证码获取
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Outlook 服务

        Args:
            config: 配置字典，支持以下键:
                - accounts: Outlook 账户列表，每个账户包含:
                  - email: 邮箱地址
                  - password: 密码
                  - client_id: OAuth2 client_id (可选)
                  - refresh_token: OAuth2 refresh_token (可选)
                - imap_host: IMAP 服务器 (默认: outlook.office365.com)
                - imap_port: IMAP 端口 (默认: 993)
                - timeout: 超时时间 (默认: 30)
                - max_retries: 最大重试次数 (默认: 3)
            name: 服务名称
        """
        super().__init__(EmailServiceType.OUTLOOK, name)

        # 默认配置
        default_config = {
            "accounts": [],
            "imap_host": "outlook.office365.com",
            "imap_port": 993,
            "timeout": 30,
            "max_retries": 3,
            "proxy_url": None,
        }

        self.config = {**default_config, **(config or {})}

        # 解析账户
        self.accounts: List[OutlookAccount] = []
        self._current_account_index = 0
        self._account_locks: Dict[str, threading.Lock] = {}

        # 支持两种配置格式：
        # 1. 单个账户格式：{"email": "xxx", "password": "xxx"}
        # 2. 多账户格式：{"accounts": [{"email": "xxx", "password": "xxx"}]}
        if "email" in self.config and "password" in self.config:
            # 单个账户格式
            account = OutlookAccount.from_config(self.config)
            if account.validate():
                self.accounts.append(account)
                self._account_locks[account.email] = threading.Lock()
            else:
                logger.warning(f"无效的 Outlook 账户配置: {self.config}")
        else:
            # 多账户格式
            for account_config in self.config.get("accounts", []):
                account = OutlookAccount.from_config(account_config)
                if account.validate():
                    self.accounts.append(account)
                    self._account_locks[account.email] = threading.Lock()
                else:
                    logger.warning(f"无效的 Outlook 账户配置: {account_config}")

        if not self.accounts:
            logger.warning("未配置有效的 Outlook 账户")

        # IMAP 连接限制（防止限流）
        self._imap_semaphore = threading.Semaphore(5)

        # 验证码去重机制：email -> set of used codes
        self._used_codes: Dict[str, set] = {}

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        选择可用的 Outlook 账户

        Args:
            config: 配置参数（目前未使用）

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - service_id: 账户邮箱（同 email）
            - account: 账户信息
        """
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有可用的 Outlook 账户"))
            raise EmailServiceError("没有可用的 Outlook 账户")

        # 轮询选择账户
        with threading.Lock():
            account = self.accounts[self._current_account_index]
            self._current_account_index = (self._current_account_index + 1) % len(self.accounts)

        email_info = {
            "email": account.email,
            "service_id": account.email,  # 对于 Outlook，service_id 就是邮箱地址
            "account": {
                "email": account.email,
                "has_oauth": account.has_oauth()
            }
        }

        logger.info(f"选择 Outlook 账户: {account.email}")
        self.update_status(True)
        return email_info

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = None,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 Outlook 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用（对于 Outlook，email 就是标识）
            timeout: 超时时间（秒），默认使用配置值
            pattern: 验证码正则表达式
            otp_sent_at: OTP 发送时间戳，用于过滤旧邮件

        Returns:
            验证码字符串，如果超时或未找到返回 None
        """
        # 查找对应的账户
        account = None
        for acc in self.accounts:
            if acc.email.lower() == email.lower():
                account = acc
                break

        if not account:
            self.update_status(False, EmailServiceError(f"未找到邮箱对应的账户: {email}"))
            return None

        # 从数据库获取验证码等待配置
        code_settings = get_email_code_settings()
        actual_timeout = timeout or code_settings["timeout"]
        poll_interval = code_settings["poll_interval"]

        logger.info(f"[{email}] 开始获取验证码，超时 {actual_timeout}s，OTP发送时间: {otp_sent_at}")

        # 初始化验证码去重集合
        if email not in self._used_codes:
            self._used_codes[email] = set()
        used_codes = self._used_codes[email]

        # 计算最小时间戳（留出 60 秒时钟偏差）
        min_timestamp = (otp_sent_at - 60) if otp_sent_at else 0

        start_time = time.time()
        poll_count = 0

        while time.time() - start_time < actual_timeout:
            poll_count += 1
            loop_start = time.time()

            # 渐进式邮件检查：前 3 次只检查未读，之后检查全部
            only_unseen = poll_count <= 3

            try:
                connect_start = time.time()
                with self._imap_semaphore:
                    with OutlookIMAPClient(
                        account,
                        host=self.config["imap_host"],
                        port=self.config["imap_port"],
                        timeout=10
                    ) as client:
                        connect_elapsed = time.time() - connect_start
                        logger.debug(f"[{email}] IMAP 连接耗时 {connect_elapsed:.2f}s")

                        # 搜索邮件
                        search_start = time.time()
                        emails = client.get_recent_emails(count=15, only_unseen=only_unseen)
                        search_elapsed = time.time() - search_start
                        logger.debug(f"[{email}] 搜索到 {len(emails)} 封邮件（未读={only_unseen}），耗时 {search_elapsed:.2f}s")

                        for mail in emails:
                            # 时间戳过滤
                            mail_ts = mail.get("date_timestamp", 0)
                            if min_timestamp > 0 and mail_ts > 0 and mail_ts < min_timestamp:
                                logger.debug(f"[{email}] 跳过旧邮件: {mail.get('subject', '')[:50]}")
                                continue

                            # 检查是否是 OpenAI 验证邮件
                            if not self._is_openai_verification_mail(mail, email):
                                continue

                            # 提取验证码
                            code = self._extract_code_from_mail(mail, pattern)
                            if code:
                                # 去重检查
                                if code in used_codes:
                                    logger.debug(f"[{email}] 跳过已使用的验证码: {code}")
                                    continue

                                used_codes.add(code)
                                elapsed = int(time.time() - start_time)
                                logger.info(f"[{email}] 找到验证码: {code}，总耗时 {elapsed}s，轮询 {poll_count} 次")
                                self.update_status(True)
                                return code

            except Exception as e:
                loop_elapsed = time.time() - loop_start
                logger.warning(f"[{email}] 检查出错: {e}，循环耗时 {loop_elapsed:.2f}s")

            # 等待下次轮询
            time.sleep(poll_interval)

        elapsed = int(time.time() - start_time)
        logger.warning(f"[{email}] 验证码超时 ({actual_timeout}s)，共轮询 {poll_count} 次")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出所有可用的 Outlook 账户

        Returns:
            账户列表
        """
        return [
            {
                "email": account.email,
                "id": account.email,
                "has_oauth": account.has_oauth(),
                "type": "outlook"
            }
            for account in self.accounts
        ]

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱（对于 Outlook，不支持删除账户）

        Args:
            email_id: 邮箱地址

        Returns:
            False（Outlook 不支持删除账户）
        """
        logger.warning(f"Outlook 服务不支持删除账户: {email_id}")
        return False

    def check_health(self) -> bool:
        """检查 Outlook 服务是否可用"""
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有配置的账户"))
            return False

        # 测试第一个账户的连接
        test_account = self.accounts[0]
        try:
            with self._imap_semaphore:
                with OutlookIMAPClient(
                    test_account,
                    host=self.config["imap_host"],
                    port=self.config["imap_port"],
                    timeout=10
                ) as client:
                    # 尝试列出邮箱（快速测试）
                    client._conn.select("INBOX", readonly=True)
                    self.update_status(True)
                    return True
        except Exception as e:
            logger.warning(f"Outlook 健康检查失败 ({test_account.email}): {e}")
            self.update_status(False, e)
            return False

    def _is_oai_mail(self, mail: Dict[str, Any]) -> bool:
        """判断是否为 OpenAI 相关邮件（旧方法，保留兼容）"""
        combined = f"{mail.get('from', '')} {mail.get('subject', '')} {mail.get('body', '')}".lower()
        keywords = ["openai", "chatgpt", "verification", "验证码", "code"]
        return any(keyword in combined for keyword in keywords)

    def _is_openai_verification_mail(
        self,
        mail: Dict[str, Any],
        target_email: str = None
    ) -> bool:
        """
        严格判断是否为 OpenAI 验证邮件

        Args:
            mail: 邮件信息字典
            target_email: 目标邮箱地址（用于验证收件人）

        Returns:
            是否为 OpenAI 验证邮件
        """
        sender = mail.get("from", "").lower()

        # 1. 发件人必须是 OpenAI
        valid_senders = OPENAI_EMAIL_SENDERS
        if not any(s in sender for s in valid_senders):
            logger.debug(f"邮件发件人非 OpenAI: {sender}")
            return False

        # 2. 主题或正文包含验证关键词
        subject = mail.get("subject", "").lower()
        body = mail.get("body", "").lower()
        verification_keywords = OPENAI_VERIFICATION_KEYWORDS
        combined = f"{subject} {body}"
        if not any(kw in combined for kw in verification_keywords):
            logger.debug(f"邮件未包含验证关键词: {subject[:50]}")
            return False

        # 3. 验证收件人（可选）
        if target_email:
            recipients = f"{mail.get('to', '')} {mail.get('delivered_to', '')} {mail.get('x_original_to', '')}".lower()
            if target_email.lower() not in recipients:
                logger.debug(f"邮件收件人不匹配: {recipients[:50]}")
                return False

        logger.debug(f"识别为 OpenAI 验证邮件: {subject[:50]}")
        return True

    def _extract_code_from_mail(
        self,
        mail: Dict[str, Any],
        fallback_pattern: str = OTP_CODE_PATTERN
    ) -> Optional[str]:
        """
        从邮件中提取验证码

        优先级：
        1. 从主题提取（6位数字）
        2. 从正文用语义正则提取（如 "code is 123456"）
        3. 兜底：任意 6 位数字

        Args:
            mail: 邮件信息字典
            fallback_pattern: 兜底正则表达式

        Returns:
            验证码字符串，如果未找到返回 None
        """
        # 编译正则
        re_simple = re.compile(OTP_CODE_SIMPLE_PATTERN)
        re_semantic = re.compile(OTP_CODE_SEMANTIC_PATTERN, re.IGNORECASE)

        # 1. 主题优先
        subject = mail.get("subject", "")
        match = re_simple.search(subject)
        if match:
            code = match.group(1)
            logger.debug(f"从主题提取验证码: {code}")
            return code

        # 2. 正文语义匹配
        body = mail.get("body", "")
        match = re_semantic.search(body)
        if match:
            code = match.group(1)
            logger.debug(f"从正文语义提取验证码: {code}")
            return code

        # 3. 兜底：任意 6 位数字
        match = re_simple.search(body)
        if match:
            code = match.group(1)
            logger.debug(f"从正文兜底提取验证码: {code}")
            return code

        return None

    def get_account_stats(self) -> Dict[str, Any]:
        """获取账户统计信息"""
        total = len(self.accounts)
        oauth_count = sum(1 for acc in self.accounts if acc.has_oauth())

        return {
            "total_accounts": total,
            "oauth_accounts": oauth_count,
            "password_accounts": total - oauth_count,
            "accounts": [
                {
                    "email": acc.email,
                    "has_oauth": acc.has_oauth()
                }
                for acc in self.accounts
            ]
        }

    def add_account(self, account_config: Dict[str, Any]) -> bool:
        """添加新的 Outlook 账户"""
        try:
            account = OutlookAccount.from_config(account_config)
            if not account.validate():
                return False

            self.accounts.append(account)
            self._account_locks[account.email] = threading.Lock()
            logger.info(f"添加 Outlook 账户: {account.email}")
            return True
        except Exception as e:
            logger.error(f"添加 Outlook 账户失败: {e}")
            return False

    def remove_account(self, email: str) -> bool:
        """移除 Outlook 账户"""
        for i, acc in enumerate(self.accounts):
            if acc.email.lower() == email.lower():
                self.accounts.pop(i)
                self._account_locks.pop(email, None)
                logger.info(f"移除 Outlook 账户: {email}")
                return True
        return False
