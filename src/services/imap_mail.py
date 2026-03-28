"""
IMAP 邮箱服务
支持 Gmail / QQ / 163 / Yahoo / Outlook 等标准 IMAP 协议邮件服务商。
仅用于接收验证码，支持在可用时通过代理建立 IMAP 连接。
"""

import email as email_module
import imaplib
import re
import time
import logging
from email.header import decode_header
from typing import Any, Dict, Optional

from src.services.imap_proxy import create_imap_client
from .base import BaseEmailService, EmailServiceError
from ..config.constants import (
    EmailServiceType,
    OPENAI_EMAIL_SENDERS,
    OTP_CODE_SEMANTIC_PATTERN,
    OTP_CODE_PATTERN,
)

logger = logging.getLogger(__name__)


class ImapMailService(BaseEmailService):
    """标准 IMAP 邮箱服务（仅接收验证码，支持代理连接）"""

    def __init__(self, config: Optional[Dict[str, Any]] = None, name: str | None = None):
        super().__init__(EmailServiceType.IMAP_MAIL, name or "")

        cfg = config or {}
        required_keys = ["host", "email", "password"]
        missing_keys = [k for k in required_keys if not cfg.get(k)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        self.host: str = str(cfg["host"]).strip()
        self.port: int = int(cfg.get("port", 993))
        self.use_ssl: bool = bool(cfg.get("use_ssl", True))
        self.email_addr: str = str(cfg["email"]).strip()
        self.password: str = str(cfg["password"])
        self.timeout: int = int(cfg.get("timeout", 30))
        self.max_retries: int = int(cfg.get("max_retries", 3))
        self.proxy_url: Optional[str] = cfg.get("proxy_url") or None

    def _connect(self) -> imaplib.IMAP4:
        """建立 IMAP 连接并登录，返回 mail 对象"""
        mail = create_imap_client(
            self.host,
            self.port,
            use_ssl=self.use_ssl,
            timeout=self.timeout,
            proxy_url=self.proxy_url,
        )
        if not self.use_ssl:
            mail.starttls()
        mail.login(self.email_addr, self.password)
        return mail

    def _decode_str(self, value) -> str:
        """解码邮件头部字段"""
        if value is None:
            return ""
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(str(part))
        return " ".join(decoded)

    def _get_text_body(self, msg) -> str:
        """提取邮件纯文本内容"""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode(charset, errors="replace")
        else:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(charset, errors="replace")
        return body

    def _is_openai_sender(self, from_addr: str) -> bool:
        """判断发件人是否为 OpenAI"""
        from_lower = from_addr.lower()
        for sender in OPENAI_EMAIL_SENDERS:
            if sender.startswith("@") or sender.startswith("."):
                if sender in from_lower:
                    return True
            else:
                if sender in from_lower:
                    return True
        return False

    def _extract_otp(self, text: str) -> Optional[str]:
        """从文本中提取 6 位验证码，优先语义匹配，回退简单匹配"""
        match = re.search(OTP_CODE_SEMANTIC_PATTERN, text, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(OTP_CODE_PATTERN, text)
        if match:
            return match.group(1)
        return None

    def create_email(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """IMAP 模式不创建新邮箱，直接返回配置中的固定地址"""
        self.update_status(True)
        return {
            "email": self.email_addr,
            "service_id": self.email_addr,
            "id": self.email_addr,
        }

    def get_verification_code(
        self,
        email: str,
        email_id: Optional[str] = None,
        timeout: int = 60,
        pattern: Optional[str] = None,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """轮询 IMAP 收件箱，获取 OpenAI 验证码"""
        start_time = time.time()
        seen_ids: set[str] = set()
        mail_client: imaplib.IMAP4 | None = None

        try:
            mail_client = self._connect()
            mail_client.select("INBOX")

            while time.time() - start_time < timeout:
                try:
                    # 搜索所有未读邮件
                    status, data = mail_client.search(None, "UNSEEN")
                    if status != "OK" or not data or not data[0]:
                        time.sleep(3)
                        continue

                    msg_ids = data[0].split()
                    for msg_id in reversed(msg_ids):  # 最新的优先
                        id_str = msg_id.decode()
                        if id_str in seen_ids:
                            continue
                        seen_ids.add(id_str)

                        # 获取邮件
                        status, msg_data = mail_client.fetch(msg_id.decode("ascii", errors="ignore"), "(RFC822)")
                        if status != "OK" or not msg_data:
                            continue

                        first_part = msg_data[0]
                        if not isinstance(first_part, tuple) or len(first_part) < 2:
                            continue

                        raw = first_part[1]
                        if not isinstance(raw, (bytes, bytearray)):
                            continue
                        msg = email_module.message_from_bytes(raw)

                        # 检查发件人
                        from_addr = self._decode_str(msg.get("From", ""))
                        if not self._is_openai_sender(from_addr):
                            continue

                        # 提取验证码
                        body = self._get_text_body(msg)
                        code = self._extract_otp(body)
                        if code:
                            # 标记已读
                            mail_client.store(msg_id.decode("ascii", errors="ignore"), "+FLAGS", "\\Seen")
                            self.update_status(True)
                            logger.info(f"IMAP 获取验证码成功: {code}")
                            return code

                except imaplib.IMAP4.error as e:
                    logger.debug(f"IMAP 搜索邮件失败: {e}")
                    # 尝试重新连接
                    try:
                        mail_client.select("INBOX")
                    except Exception:
                        pass

                time.sleep(3)

        except Exception as e:
            logger.warning(f"IMAP 连接/轮询失败: {e}")
            self.update_status(False, e)
        finally:
            if mail_client:
                try:
                    mail_client.logout()
                except Exception:
                    pass

        return None

    def check_health(self) -> bool:
        """尝试 IMAP 登录并选择收件箱"""
        mail = None
        try:
            mail = self._connect()
            status, _ = mail.select("INBOX")
            return status == "OK"
        except Exception as e:
            logger.warning(f"IMAP 健康检查失败: {e}")
            return False
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

    def list_emails(self, **kwargs) -> list[Dict[str, str]]:
        """IMAP 单账号模式，返回固定地址"""
        return [{"email": self.email_addr, "id": self.email_addr}]

    def delete_email(self, email_id: str) -> bool:
        """IMAP 模式无需删除逻辑"""
        return True
