"""
Outlook 提供者抽象基类
"""

import abc
import logging
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

from ..base import ProviderType, EmailMessage, ProviderHealth, ProviderStatus
from ..account import OutlookAccount


logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    """提供者配置"""
    timeout: int = 30
    max_retries: int = 3
    proxy_url: Optional[str] = None

    # 健康检查配置
    health_failure_threshold: int = 3
    health_disable_duration: int = 300  # 秒


class OutlookProvider(abc.ABC):
    """
    Outlook 提供者抽象基类
    定义所有提供者必须实现的接口
    """

    def __init__(
        self,
        account: OutlookAccount,
        config: Optional[ProviderConfig] = None,
    ):
        """
        初始化提供者

        Args:
            account: Outlook 账户
            config: 提供者配置
        """
        self.account = account
        self.config = config or ProviderConfig()

        # 健康状态
        self._health = ProviderHealth(provider_type=self.provider_type)

        # 连接状态
        self._connected = False
        self._last_error: Optional[str] = None

    @property
    @abc.abstractmethod
    def provider_type(self) -> ProviderType:
        """获取提供者类型"""
        pass

    @property
    def health(self) -> ProviderHealth:
        """获取健康状态"""
        return self._health

    @property
    def is_healthy(self) -> bool:
        """检查是否健康"""
        return (
            self._health.status == ProviderStatus.HEALTHY
            and not self._health.is_disabled()
        )

    @property
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._connected

    @abc.abstractmethod
    def connect(self) -> bool:
        """
        连接到服务

        Returns:
            是否连接成功
        """
        pass

    @abc.abstractmethod
    def disconnect(self):
        """断开连接"""
        pass

    @abc.abstractmethod
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
        pass

    @abc.abstractmethod
    def test_connection(self) -> bool:
        """
        测试连接是否正常

        Returns:
            连接是否正常
        """
        pass

    def record_success(self):
        """记录成功操作"""
        self._health.record_success()
        self._last_error = None
        logger.debug(f"[{self.account.email}] {self.provider_type.value} 操作成功")

    def record_failure(self, error: str):
        """记录失败操作"""
        self._health.record_failure(error)
        self._last_error = error

        # 检查是否需要禁用
        if self._health.should_disable(self.config.health_failure_threshold):
            self._health.disable(self.config.health_disable_duration)
            logger.warning(
                f"[{self.account.email}] {self.provider_type.value} 已禁用 "
                f"{self.config.health_disable_duration} 秒，原因: {error}"
            )
        else:
            logger.warning(
                f"[{self.account.email}] {self.provider_type.value} 操作失败 "
                f"({self._health.failure_count}/{self.config.health_failure_threshold}): {error}"
            )

    def check_health(self) -> bool:
        """
        检查健康状态

        Returns:
            是否健康可用
        """
        # 检查是否被禁用
        if self._health.is_disabled():
            logger.debug(
                f"[{self.account.email}] {self.provider_type.value} 已被禁用，"
                f"将在 {self._health.disabled_until} 后恢复"
            )
            return False

        return self._health.status in (ProviderStatus.HEALTHY, ProviderStatus.DEGRADED)

    def __enter__(self):
        """上下文管理器入口"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.disconnect()
        return False

    def __str__(self) -> str:
        """字符串表示"""
        return f"{self.__class__.__name__}({self.account.email})"

    def __repr__(self) -> str:
        return self.__str__()
