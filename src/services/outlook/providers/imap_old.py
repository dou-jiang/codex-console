"""
旧版 IMAP 提供者
使用 outlook.office365.com 服务器和 login.live.com Token 端点
"""

import email
import imaplib
import logging
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import List, Optional

from ..base import ProviderType, EmailMessage
from ..account import OutlookAccount
from ..token_manager import TokenManager
from .base import OutlookProvider, ProviderConfig


logger = logging.getLogger(__name__)


class IMAPOldProvider(OutlookProvider):
    """
    旧版 IMAP 提供者
    使用 outlook.office365.com:993 和 login.live.com Token 端点
    """

    # IMAP 服务器配置
    IMAP_HOST = "outlook.office365.com"
    IMAP_PORT = 993
    # 验证邮件有时会进入垃圾邮件/存档等文件夹，需多文件夹轮询
    SEARCH_MAILBOXES = [
        "INBOX",
        "Junk",
        "Junk Email",
        "Junk E-mail",
        "Spam",
        "Deleted Items",
        "Trash",
        "Clutter",
        "Archive",
    ]

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.IMAP_OLD

    def __init__(
        self,
        account: OutlookAccount,
        config: Optional[ProviderConfig] = None,
    ):
        super().__init__(account, config)

        # IMAP 连接
        self._conn: Optional[imaplib.IMAP4_SSL] = None

        # Token 管理器
        self._token_manager: Optional[TokenManager] = None

    def connect(self) -> bool:
        """
        连接到 IMAP 服务器

        Returns:
            是否连接成功
        """
        if self._connected and self._conn:
            # 检查现有连接
            try:
                self._conn.noop()
                return True
            except Exception:
                self.disconnect()

        try:
            logger.debug(f"[{self.account.email}] 正在连接 IMAP ({self.IMAP_HOST})...")

            # 创建连接
            self._conn = imaplib.IMAP4_SSL(
                self.IMAP_HOST,
                self.IMAP_PORT,
                timeout=self.config.timeout,
            )

            # 尝试 XOAUTH2 认证
            if self.account.has_oauth():
                if self._authenticate_xoauth2():
                    self._connected = True
                    self.record_success()
                    logger.info(f"[{self.account.email}] IMAP 连接成功 (XOAUTH2)")
                    return True
                else:
                    logger.warning(f"[{self.account.email}] XOAUTH2 认证失败，尝试密码认证")

            # 密码认证
            if self.account.password:
                self._conn.login(self.account.email, self.account.password)
                self._connected = True
                self.record_success()
                logger.info(f"[{self.account.email}] IMAP 连接成功 (密码认证)")
                return True

            raise ValueError("没有可用的认证方式")

        except Exception as e:
            self.disconnect()
            self.record_failure(str(e))
            logger.error(f"[{self.account.email}] IMAP 连接失败: {e}")
            return False

    def _authenticate_xoauth2(self) -> bool:
        """
        使用 XOAUTH2 认证

        Returns:
            是否认证成功
        """
        if not self._token_manager:
            self._token_manager = TokenManager(
                self.account,
                ProviderType.IMAP_OLD,
                self.config.proxy_url,
                self.config.timeout,
            )

        # 获取 Access Token
        token = self._token_manager.get_access_token()
        if not token:
            return False

        try:
            # 构建 XOAUTH2 认证字符串
            auth_string = f"user={self.account.email}\x01auth=Bearer {token}\x01\x01"
            self._conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
            return True
        except Exception as e:
            logger.debug(f"[{self.account.email}] XOAUTH2 认证异常: {e}")
            # 清除缓存的 Token
            self._token_manager.clear_cache()
            return False

    def disconnect(self):
        """断开 IMAP 连接"""
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

        self._connected = False

    def get_recent_emails(
        self,
        count: int = 20,
        only_unseen: bool = True,
    ) -> List[EmailMessage]:
        """
        获取最近的邮件

        Args:
            count: 获取数量
            only_unseen: 是否只获取未读

        Returns:
            邮件列表
        """
        if not self._connected:
            if not self.connect():
                return []

        try:
            flag = "UNSEEN" if only_unseen else "ALL"
            emails = []
            seen_keys = set()

            for mailbox in self.SEARCH_MAILBOXES:
                try:
                    status, _ = self._conn.select(mailbox, readonly=True)
                    if status != "OK":
                        continue

                    status, data = self._conn.search(None, flag)
                    if status != "OK" or not data or not data[0]:
                        continue

                    ids = data[0].split()
                    recent_ids = ids[-count:][::-1]  # 倒序，最新的在前

                    for msg_id in recent_ids:
                        try:
                            email_msg = self._fetch_email(msg_id)
                            if not email_msg:
                                continue

                            dedupe_key = email_msg.id or f"{mailbox}:{msg_id.decode(errors='ignore')}"
                            if dedupe_key in seen_keys:
                                continue

                            seen_keys.add(dedupe_key)
                            emails.append(email_msg)
                        except Exception as e:
                            logger.warning(
                                f"[{self.account.email}] 解析邮件失败 ({mailbox}, ID: {msg_id}): {e}"
                            )
                except Exception as e:
                    logger.debug(f"[{self.account.email}] 跳过邮箱文件夹 {mailbox}: {e}")

            return emails

        except Exception as e:
            self.record_failure(str(e))
            logger.error(f"[{self.account.email}] 获取邮件失败: {e}")
            return []

    def _fetch_email(self, msg_id: bytes) -> Optional[EmailMessage]:
        """
        获取并解析单封邮件

        Args:
            msg_id: 邮件 ID

        Returns:
            EmailMessage 对象，失败返回 None
        """
        status, data = self._conn.fetch(msg_id, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            return None

        # 获取原始邮件内容
        raw = b""
        for part in data:
            if isinstance(part, tuple) and len(part) > 1:
                raw = part[1]
                break

        if not raw:
            return None

        return self._parse_email(raw)

    @staticmethod
    def _parse_email(raw: bytes) -> EmailMessage:
        """
        解析原始邮件

        Args:
            raw: 原始邮件数据

        Returns:
            EmailMessage 对象
        """
        # 移除 BOM
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]

        msg = email.message_from_bytes(raw)

        # 解析邮件头
        subject = IMAPOldProvider._decode_header(msg.get("Subject", ""))
        sender = IMAPOldProvider._decode_header(msg.get("From", ""))
        to = IMAPOldProvider._decode_header(msg.get("To", ""))
        delivered_to = IMAPOldProvider._decode_header(msg.get("Delivered-To", ""))
        x_original_to = IMAPOldProvider._decode_header(msg.get("X-Original-To", ""))
        date_str = IMAPOldProvider._decode_header(msg.get("Date", ""))

        # 提取正文
        body = IMAPOldProvider._extract_body(msg)

        # 解析日期
        received_timestamp = 0
        received_at = None
        try:
            if date_str:
                received_at = parsedate_to_datetime(date_str)
                received_timestamp = int(received_at.timestamp())
        except Exception:
            pass

        # 构建收件人列表
        recipients = [r for r in [to, delivered_to, x_original_to] if r]

        return EmailMessage(
            id=msg.get("Message-ID", ""),
            subject=subject,
            sender=sender,
            recipients=recipients,
            body=body,
            received_at=received_at,
            received_timestamp=received_timestamp,
            is_read=False,  # 搜索的是未读邮件
            raw_data=raw[:500] if len(raw) > 500 else raw,
        )

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
                parts.append(str(chunk))

        return "".join(parts).strip()

    @staticmethod
    def _extract_body(msg) -> str:
        """提取邮件正文"""
        import html as html_module
        import re

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

    def test_connection(self) -> bool:
        """
        测试 IMAP 连接

        Returns:
            连接是否正常
        """
        try:
            with self:
                self._conn.select("INBOX", readonly=True)
                self._conn.search(None, "ALL")
            return True
        except Exception as e:
            logger.warning(f"[{self.account.email}] IMAP 连接测试失败: {e}")
            return False
