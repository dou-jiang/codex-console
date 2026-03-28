"""
新版 IMAP 提供者
使用 outlook.live.com 服务器和 login.microsoftonline.com/consumers Token 端点
"""

import email
import imaplib
import logging
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import List, Optional

from src.services.imap_proxy import create_imap_client
from ..base import ProviderType, EmailMessage
from ..account import OutlookAccount
from ..token_manager import TokenManager
from .base import OutlookProvider, ProviderConfig
from .imap_old import IMAPOldProvider


logger = logging.getLogger(__name__)


class IMAPNewProvider(OutlookProvider):
    """
    新版 IMAP 提供者
    使用 outlook.live.com:993 和 login.microsoftonline.com/consumers Token 端点
    需要 IMAP.AccessAsUser.All scope
    """

    # IMAP 服务器配置
    IMAP_HOST = "outlook.live.com"
    IMAP_PORT = 993

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.IMAP_NEW

    def __init__(
        self,
        account: OutlookAccount,
        config: Optional[ProviderConfig] = None,
    ):
        super().__init__(account, config)

        # IMAP 连接
        self._conn: Optional[imaplib.IMAP4] = None

        # Token 管理器
        self._token_manager: Optional[TokenManager] = None

        # 注意：新版 IMAP 必须使用 OAuth2
        if not account.has_oauth():
            logger.warning(
                f"[{self.account.email}] 新版 IMAP 提供者需要 OAuth2 配置 "
                f"(client_id + refresh_token)"
            )

    def connect(self) -> bool:
        """
        连接到 IMAP 服务器

        Returns:
            是否连接成功
        """
        if self._connected and self._conn:
            try:
                self._conn.noop()
                return True
            except Exception:
                self.disconnect()

        # 新版 IMAP 必须使用 OAuth2，无 OAuth 时静默跳过，不记录健康失败
        if not self.account.has_oauth():
            logger.debug(f"[{self.account.email}] 跳过 IMAP_NEW（无 OAuth）")
            return False

        try:
            logger.debug(f"[{self.account.email}] 正在连接 IMAP ({self.IMAP_HOST})...")

            # 创建连接
            self._conn = create_imap_client(
                self.IMAP_HOST,
                self.IMAP_PORT,
                use_ssl=True,
                timeout=self.config.timeout,
                proxy_url=self.config.proxy_url,
            )

            # XOAUTH2 认证
            if self._authenticate_xoauth2():
                self._connected = True
                self.record_success()
                logger.info(f"[{self.account.email}] 新版 IMAP 连接成功 (XOAUTH2)")
                return True

            return False

        except Exception as e:
            self.disconnect()
            self.record_failure(str(e))
            logger.error(f"[{self.account.email}] 新版 IMAP 连接失败: {e}")
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
                ProviderType.IMAP_NEW,
                self.config.proxy_url,
                self.config.timeout,
            )

        # 获取 Access Token
        token = self._token_manager.get_access_token()
        if not token:
            logger.error(f"[{self.account.email}] 获取 IMAP Token 失败")
            return False

        try:
            conn = self._conn
            if conn is None:
                return False
            assert conn is not None

            # 构建 XOAUTH2 认证字符串
            auth_string = f"user={self.account.email}\x01auth=Bearer {token}\x01\x01"
            conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
            return True
        except Exception as e:
            logger.error(f"[{self.account.email}] XOAUTH2 认证异常: {e}")
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
            conn = self._conn
            if conn is None:
                self.record_failure("IMAP 连接未初始化")
                return []

            # 选择收件箱
            conn.select("INBOX", readonly=True)

            # 搜索邮件
            flag = "UNSEEN" if only_unseen else "ALL"
            status, data = conn.search(None, flag)

            if status != "OK" or not data or not data[0]:
                return []

            # 获取最新的邮件 ID
            ids = data[0].split()
            recent_ids = ids[-count:][::-1]

            emails = []
            for msg_id in recent_ids:
                try:
                    email_msg = self._fetch_email(msg_id)
                    if email_msg:
                        emails.append(email_msg)
                except Exception as e:
                    logger.warning(f"[{self.account.email}] 解析邮件失败 (ID: {msg_id}): {e}")

            return emails

        except Exception as e:
            self.record_failure(str(e))
            logger.error(f"[{self.account.email}] 获取邮件失败: {e}")
            return []

    def _fetch_email(self, msg_id: bytes) -> Optional[EmailMessage]:
        """获取并解析单封邮件"""
        conn = self._conn
        if conn is None:
            return None

        status, data = conn.fetch(msg_id.decode("ascii", errors="ignore"), "(RFC822)")
        if status != "OK" or not data or not data[0]:
            return None

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
        """解析原始邮件"""
        # 使用旧版提供者的解析方法
        return IMAPOldProvider._parse_email(raw)

    def test_connection(self) -> bool:
        """测试 IMAP 连接"""
        try:
            with self:
                conn = self._conn
                if conn is None:
                    return False
                conn.select("INBOX", readonly=True)
                conn.search(None, "ALL")
            return True
        except Exception as e:
            logger.warning(f"[{self.account.email}] 新版 IMAP 连接测试失败: {e}")
            return False
