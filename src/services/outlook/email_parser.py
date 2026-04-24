"""
邮件解析和验证码提取
"""

import logging
import re
from typing import Optional, List, Dict, Any

from ...config.constants import (
    OTP_CODE_SIMPLE_PATTERN,
    OTP_CODE_SEMANTIC_PATTERN,
    OPENAI_EMAIL_SENDERS,
    OPENAI_VERIFICATION_KEYWORDS,
)
from .base import EmailMessage


logger = logging.getLogger(__name__)


class EmailParser:
    """
    邮件解析器
    用于识别 OpenAI 验证邮件并提取验证码
    """

    def __init__(self):
        # 编译正则表达式
        self._simple_pattern = re.compile(OTP_CODE_SIMPLE_PATTERN)
        self._semantic_pattern = re.compile(OTP_CODE_SEMANTIC_PATTERN, re.IGNORECASE)

    def is_openai_verification_email(
        self,
        email: EmailMessage,
        target_email: Optional[str] = None,
    ) -> bool:
        """
        判断是否为 OpenAI 验证邮件

        Args:
            email: 邮件对象
            target_email: 目标邮箱地址（用于验证收件人）

        Returns:
            是否为 OpenAI 验证邮件
        """
        sender = email.sender.lower()

        # 1. 发件人必须是 OpenAI
        if not any(s in sender for s in OPENAI_EMAIL_SENDERS):
            logger.debug(f"邮件发件人非 OpenAI: {sender}")
            return False

        # 2. 主题或正文包含验证关键词
        subject = email.subject.lower()
        body = email.body.lower()
        combined = f"{subject} {body}"

        if not any(kw in combined for kw in OPENAI_VERIFICATION_KEYWORDS):
            logger.debug(f"邮件未包含验证关键词: {subject[:50]}")
            return False

        # 3. 收件人检查已移除：别名邮件的 IMAP 头中收件人可能不匹配，只靠发件人+关键词判断
        logger.debug(f"识别为 OpenAI 验证邮件: {subject[:50]}")
        return True

    def extract_verification_code(
        self,
        email: EmailMessage,
    ) -> Optional[str]:
        """
        从邮件中提取验证码

        优先级：
        1. 从主题提取（6位数字）
        2. 从正文用语义正则提取（如 "code is 123456"）
        3. 兜底：任意 6 位数字

        Args:
            email: 邮件对象

        Returns:
            验证码字符串，如果未找到返回 None
        """
        # 1. 主题优先
        code = self._extract_from_subject(email.subject)
        if code:
            logger.debug(f"从主题提取验证码: {code}")
            return code

        # 2. 正文语义匹配
        code = self._extract_semantic(email.body)
        if code:
            logger.debug(f"从正文语义提取验证码: {code}")
            return code

        # 3. 兜底：正文任意 6 位数字
        code = self._extract_simple(email.body)
        if code:
            logger.debug(f"从正文兜底提取验证码: {code}")
            return code

        return None

    def _extract_from_subject(self, subject: str) -> Optional[str]:
        """从主题提取验证码"""
        match = self._simple_pattern.search(subject)
        if match:
            return match.group(1)
        return None

    def _extract_semantic(self, body: str) -> Optional[str]:
        """语义匹配提取验证码"""
        match = self._semantic_pattern.search(body)
        if match:
            return match.group(1)
        return None

    def _extract_simple(self, body: str) -> Optional[str]:
        """简单匹配提取验证码"""
        match = self._simple_pattern.search(body)
        if match:
            return match.group(1)
        return None

    def find_verification_code_in_emails(
        self,
        emails: List[EmailMessage],
        target_email: Optional[str] = None,
        min_timestamp: int = 0,
        used_codes: Optional[set] = None,
        used_fingerprints: Optional[set] = None,
    ) -> Optional[str]:
        """
        从邮件列表中查找验证码

        Args:
            emails: 邮件列表
            target_email: 目标邮箱地址
            min_timestamp: 最小时间戳（用于过滤旧邮件）
            used_codes: 兼容旧参数（仅在无邮件 ID/时间时兜底）
            used_fingerprints: 已使用邮件指纹集合，指纹规则为 "时间戳|邮件ID|验证码"

        Returns:
            验证码字符串，如果未找到返回 None
        """
        # 注意：不能用 `or set()`，否则传入空集合时会被替换，导致跨轮询去重失效。
        if used_codes is None:
            used_codes = set()
        if used_fingerprints is None:
            used_fingerprints = set()

        for email in emails:
            # 时间戳过滤
            if min_timestamp > 0 and email.received_timestamp > 0:
                if email.received_timestamp < min_timestamp:
                    logger.debug(f"跳过旧邮件: {email.subject[:50]}")
                    continue

            # 检查是否是 OpenAI 验证邮件
            if not self.is_openai_verification_email(email, target_email):
                continue

            # 提取验证码
            code = self.extract_verification_code(email)
            if code:
                mail_ts = int(email.received_timestamp or 0)
                mail_id = str(email.id or "").strip() or "-"
                fingerprint = f"{mail_ts}|{mail_id}|{code}"

                # 主去重：时间 + 邮件ID + 验证码
                if fingerprint in used_fingerprints:
                    logger.debug(f"跳过已使用验证码指纹: {fingerprint}")
                    continue

                # 兜底去重：如果邮件没有 ID 且没有时间戳，才退回到纯验证码去重
                if mail_id == "-" and mail_ts <= 0 and code in used_codes:
                    logger.debug(f"跳过已使用的验证码(兜底): {code}")
                    continue

                used_fingerprints.add(fingerprint)
                used_codes.add(code)
                logger.info(
                    f"[{target_email or 'unknown'}] 找到验证码: {code}, "
                    f"邮件主题: {email.subject[:30]}"
                )
                return code

        return None

    def filter_emails_by_sender(
        self,
        emails: List[EmailMessage],
        sender_patterns: List[str],
    ) -> List[EmailMessage]:
        """
        按发件人过滤邮件

        Args:
            emails: 邮件列表
            sender_patterns: 发件人匹配模式列表

        Returns:
            过滤后的邮件列表
        """
        filtered = []
        for email in emails:
            sender = email.sender.lower()
            if any(pattern.lower() in sender for pattern in sender_patterns):
                filtered.append(email)
        return filtered

    def filter_emails_by_subject(
        self,
        emails: List[EmailMessage],
        keywords: List[str],
    ) -> List[EmailMessage]:
        """
        按主题关键词过滤邮件

        Args:
            emails: 邮件列表
            keywords: 关键词列表

        Returns:
            过滤后的邮件列表
        """
        filtered = []
        for email in emails:
            subject = email.subject.lower()
            if any(kw.lower() in subject for kw in keywords):
                filtered.append(email)
        return filtered


# 全局解析器实例
_parser: Optional[EmailParser] = None


def get_email_parser() -> EmailParser:
    """获取全局邮件解析器实例"""
    global _parser
    if _parser is None:
        _parser = EmailParser()
    return _parser
