"""
Outlook 邮箱服务主类
支持多种 IMAP/API 连接方式，自动故障切换
"""

import logging
import threading
import time
from typing import Optional, Dict, Any, List

from ..base import BaseEmailService, EmailServiceError, EmailServiceStatus, EmailServiceType
from ...config.constants import EmailServiceType as ServiceType
from ...config.settings import get_settings
from .account import OutlookAccount
from .base import ProviderType, EmailMessage
from .email_parser import EmailParser, get_email_parser
from .health_checker import HealthChecker, FailoverManager
from .providers.base import OutlookProvider, ProviderConfig
from .providers.imap_old import IMAPOldProvider
from .providers.imap_new import IMAPNewProvider
from .providers.graph_api import GraphAPIProvider


logger = logging.getLogger(__name__)


# 默认提供者优先级
# IMAP_OLD 最兼容（只需 login.live.com token），IMAP_NEW 次之，Graph API 最后
# 原因：部分 client_id 没有 Graph API 权限，但有 IMAP 权限
DEFAULT_PROVIDER_PRIORITY = [
    ProviderType.IMAP_OLD,
    ProviderType.IMAP_NEW,
    ProviderType.GRAPH_API,
]

# OTP 发送时间的容忍偏差（秒）
# 与 clean 版本保持一致，避免把上一阶段验证码误判为当前验证码。
OTP_TIME_SKEW_SECONDS = 5


def get_email_code_settings() -> dict:
    """获取验证码等待配置"""
    settings = get_settings()
    return {
        "timeout": settings.email_code_timeout,
        "poll_interval": settings.email_code_poll_interval,
    }


class OutlookService(BaseEmailService):
    """
    Outlook 邮箱服务
    支持多种 IMAP/API 连接方式，自动故障切换
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Outlook 服务

        Args:
            config: 配置字典，支持以下键:
                - accounts: Outlook 账户列表
                - provider_priority: 提供者优先级列表
                - health_failure_threshold: 连续失败次数阈值
                - health_disable_duration: 禁用时长（秒）
                - timeout: 请求超时时间
                - proxy_url: 代理 URL
            name: 服务名称
        """
        super().__init__(ServiceType.OUTLOOK, name)

        # 默认配置
        default_config = {
            "accounts": [],
            "provider_priority": [p.value for p in DEFAULT_PROVIDER_PRIORITY],
            "health_failure_threshold": 5,
            "health_disable_duration": 60,
            "timeout": 30,
            "proxy_url": None,
        }

        self.config = {**default_config, **(config or {})}

        # 解析提供者优先级
        self.provider_priority = [
            ProviderType(p) for p in self.config.get("provider_priority", [])
        ]
        if not self.provider_priority:
            self.provider_priority = DEFAULT_PROVIDER_PRIORITY

        # 提供者配置
        self.provider_config = ProviderConfig(
            timeout=self.config.get("timeout", 30),
            proxy_url=self.config.get("proxy_url"),
            health_failure_threshold=self.config.get("health_failure_threshold", 3),
            health_disable_duration=self.config.get("health_disable_duration", 300),
        )

        # 获取默认 client_id（供无 client_id 的账户使用）
        try:
            _default_client_id = get_settings().outlook_default_client_id
        except Exception:
            _default_client_id = "24d9a0ed-8787-4584-883c-2fd79308940a"

        # 解析账户
        self.accounts: List[OutlookAccount] = []
        self._current_account_index = 0
        self._account_lock = threading.Lock()

        # 支持两种配置格式
        if "email" in self.config and "password" in self.config:
            account = OutlookAccount.from_config(self.config)
            if not account.client_id and _default_client_id:
                account.client_id = _default_client_id
            if account.validate():
                self.accounts.append(account)
        else:
            for account_config in self.config.get("accounts", []):
                account = OutlookAccount.from_config(account_config)
                if not account.client_id and _default_client_id:
                    account.client_id = _default_client_id
                if account.validate():
                    self.accounts.append(account)

        if not self.accounts:
            logger.warning("未配置有效的 Outlook 账户")

        # 健康检查器和故障切换管理器
        self.health_checker = HealthChecker(
            failure_threshold=self.provider_config.health_failure_threshold,
            disable_duration=self.provider_config.health_disable_duration,
        )
        self.failover_manager = FailoverManager(
            health_checker=self.health_checker,
            priority_order=self.provider_priority,
        )

        # 邮件解析器
        self.email_parser = get_email_parser()

        # 提供者实例缓存: (email, provider_type) -> OutlookProvider
        self._providers: Dict[tuple, OutlookProvider] = {}
        self._provider_lock = threading.Lock()

        # IMAP 连接限制（防止限流）
        self._imap_semaphore = threading.Semaphore(5)

        # 验证码去重机制（按“时间戳+邮件ID+验证码”指纹）
        self._used_codes: Dict[str, set] = {}
        # 验证码阶段标记（按 otp_sent_at 重置去重，避免“第二封验证码与第一封相同”被误判为旧码）
        self._used_codes_stage_marker: Dict[str, int] = {}

    def _get_provider(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
    ) -> OutlookProvider:
        """
        获取或创建提供者实例

        Args:
            account: Outlook 账户
            provider_type: 提供者类型

        Returns:
            提供者实例
        """
        cache_key = (account.email.lower(), provider_type)

        with self._provider_lock:
            if cache_key not in self._providers:
                provider = self._create_provider(account, provider_type)
                self._providers[cache_key] = provider

            return self._providers[cache_key]

    def _create_provider(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
    ) -> OutlookProvider:
        """
        创建提供者实例

        Args:
            account: Outlook 账户
            provider_type: 提供者类型

        Returns:
            提供者实例
        """
        if provider_type == ProviderType.IMAP_OLD:
            return IMAPOldProvider(account, self.provider_config)
        elif provider_type == ProviderType.IMAP_NEW:
            return IMAPNewProvider(account, self.provider_config)
        elif provider_type == ProviderType.GRAPH_API:
            return GraphAPIProvider(account, self.provider_config)
        else:
            raise ValueError(f"未知的提供者类型: {provider_type}")

    def _get_provider_priority_for_account(self, account: OutlookAccount) -> List[ProviderType]:
        """根据账户是否有 OAuth，返回适合的提供者优先级列表"""
        if account.has_oauth():
            return self.provider_priority
        else:
            # 无 OAuth，直接走旧版 IMAP（密码认证），跳过需要 OAuth 的提供者
            return [ProviderType.IMAP_OLD]

    def _try_providers_for_emails(
        self,
        account: OutlookAccount,
        count: int = 20,
        only_unseen: bool = True,
    ) -> List[EmailMessage]:
        """
        尝试多个提供者获取邮件

        Args:
            account: Outlook 账户
            count: 获取数量
            only_unseen: 是否只获取未读

        Returns:
            邮件列表
        """
        errors = []

        # 根据账户类型选择合适的提供者优先级
        priority = self._get_provider_priority_for_account(account)

        # 按优先级尝试各提供者
        for provider_type in priority:
            # 检查提供者是否可用
            if not self.health_checker.is_available(provider_type):
                logger.debug(
                    f"[{account.email}] {provider_type.value} 不可用，跳过"
                )
                continue

            try:
                provider = self._get_provider(account, provider_type)

                with self._imap_semaphore:
                    with provider:
                        emails = provider.get_recent_emails(count, only_unseen)

                        if emails:
                            # 成功获取邮件
                            self.health_checker.record_success(provider_type)
                            logger.debug(
                                f"[{account.email}] {provider_type.value} 获取到 {len(emails)} 封邮件"
                            )
                            return emails

            except Exception as e:
                error_msg = str(e)
                errors.append(f"{provider_type.value}: {error_msg}")
                self.health_checker.record_failure(provider_type, error_msg)
                logger.warning(
                    f"[{account.email}] {provider_type.value} 获取邮件失败: {e}"
                )

        logger.error(
            f"[{account.email}] 所有提供者都失败: {'; '.join(errors)}"
        )
        return []

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        选择可用的 Outlook 账户

        Args:
            config: 配置参数（未使用）

        Returns:
            包含邮箱信息的字典
        """
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有可用的 Outlook 账户"))
            raise EmailServiceError("没有可用的 Outlook 账户")

        # 轮询选择账户
        with self._account_lock:
            account = self.accounts[self._current_account_index]
            self._current_account_index = (self._current_account_index + 1) % len(self.accounts)

        email_info = {
            "email": account.email,
            "service_id": account.email,
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
        pattern: str = None,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 Outlook 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用
            timeout: 超时时间（秒）
            pattern: 验证码正则表达式（未使用）
            otp_sent_at: OTP 发送时间戳

        Returns:
            验证码字符串
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

        # 获取验证码等待配置
        code_settings = get_email_code_settings()
        actual_timeout = timeout or code_settings["timeout"]
        poll_interval = code_settings["poll_interval"]

        logger.info(
            f"[{email}] 开始获取验证码，超时 {actual_timeout}s，"
            f"提供者优先级: {[p.value for p in self.provider_priority]}"
        )

        # 初始化验证码指纹去重集合
        email_key = str(email or "").strip().lower()
        if email_key not in self._used_codes:
            self._used_codes[email_key] = set()
        used_fingerprints = self._used_codes[email_key]

        # 按 OTP 发送时间重置去重集合，避免不同阶段共用同一验证码时被误跳过
        if otp_sent_at:
            try:
                stage_marker = int(float(otp_sent_at))
                prev_marker = self._used_codes_stage_marker.get(email_key)
                if prev_marker is None or abs(stage_marker - prev_marker) > 3:
                    if used_fingerprints:
                        logger.info(
                            f"[{email}] 检测到新的验证码阶段，重置去重缓存（上一阶段已记 {len(used_fingerprints)} 条指纹）"
                        )
                    used_fingerprints.clear()
                    self._used_codes_stage_marker[email_key] = stage_marker
            except Exception:
                pass

        # 计算最小时间戳（仅容忍小时钟偏差，避免命中上一阶段旧验证码）
        min_timestamp = (otp_sent_at - OTP_TIME_SKEW_SECONDS) if otp_sent_at else 0

        start_time = time.time()
        poll_count = 0

        while time.time() - start_time < actual_timeout:
            poll_count += 1

            # 渐进式邮件检查：前 3 次只检查未读
            only_unseen = poll_count <= 3

            try:
                # 尝试多个提供者获取邮件
                emails = self._try_providers_for_emails(
                    account,
                    count=15,
                    only_unseen=only_unseen,
                )

                if emails:
                    logger.debug(
                        f"[{email}] 第 {poll_count} 次轮询获取到 {len(emails)} 封邮件"
                    )

                    # 从邮件中查找验证码
                    code = self.email_parser.find_verification_code_in_emails(
                        emails,
                        target_email=email,
                        min_timestamp=min_timestamp,
                        used_fingerprints=used_fingerprints,
                    )

                    if code:
                        elapsed = int(time.time() - start_time)
                        logger.info(
                            f"[{email}] 找到验证码: {code}，"
                            f"总耗时 {elapsed}s，轮询 {poll_count} 次"
                        )
                        self.update_status(True)
                        return code

            except Exception as e:
                logger.warning(f"[{email}] 检查出错: {e}")

            # 等待下次轮询
            time.sleep(poll_interval)

        elapsed = int(time.time() - start_time)
        logger.warning(f"[{email}] 验证码超时 ({actual_timeout}s)，共轮询 {poll_count} 次")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """列出所有可用的 Outlook 账户"""
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
        """删除邮箱（Outlook 不支持删除账户）"""
        logger.warning(f"Outlook 服务不支持删除账户: {email_id}")
        return False

    def check_health(self) -> bool:
        """检查 Outlook 服务是否可用"""
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有配置的账户"))
            return False

        # 测试第一个账户的连接
        test_account = self.accounts[0]

        # 尝试任一提供者连接
        for provider_type in self.provider_priority:
            try:
                provider = self._get_provider(test_account, provider_type)
                if provider.test_connection():
                    self.update_status(True)
                    return True
            except Exception as e:
                logger.warning(
                    f"Outlook 健康检查失败 ({test_account.email}, {provider_type.value}): {e}"
                )

        self.update_status(False, EmailServiceError("健康检查失败"))
        return False

    def get_provider_status(self) -> Dict[str, Any]:
        """获取提供者状态"""
        return self.failover_manager.get_status()

    def get_account_stats(self) -> Dict[str, Any]:
        """获取账户统计信息"""
        total = len(self.accounts)
        oauth_count = sum(1 for acc in self.accounts if acc.has_oauth())

        return {
            "total_accounts": total,
            "oauth_accounts": oauth_count,
            "password_accounts": total - oauth_count,
            "accounts": [acc.to_dict() for acc in self.accounts],
            "provider_status": self.get_provider_status(),
        }

    def add_account(self, account_config: Dict[str, Any]) -> bool:
        """添加新的 Outlook 账户"""
        try:
            account = OutlookAccount.from_config(account_config)
            if not account.validate():
                return False

            self.accounts.append(account)
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
                logger.info(f"移除 Outlook 账户: {email}")
                return True
        return False

    def reset_provider_health(self):
        """重置所有提供者的健康状态"""
        self.health_checker.reset_all()
        logger.info("已重置所有提供者的健康状态")

    def force_provider(self, provider_type: ProviderType):
        """强制使用指定的提供者"""
        self.health_checker.force_enable(provider_type)
        # 禁用其他提供者
        for pt in ProviderType:
            if pt != provider_type:
                self.health_checker.force_disable(pt, 60)
        logger.info(f"已强制使用提供者: {provider_type.value}")
