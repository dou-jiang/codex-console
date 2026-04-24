"""
健康检查和故障切换管理
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from .base import ProviderType, ProviderHealth, ProviderStatus
from .providers.base import OutlookProvider


logger = logging.getLogger(__name__)


class HealthChecker:
    """
    健康检查管理器
    跟踪各提供者的健康状态，管理故障切换
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        disable_duration: int = 300,
        recovery_check_interval: int = 60,
    ):
        """
        初始化健康检查器

        Args:
            failure_threshold: 连续失败次数阈值，超过后禁用
            disable_duration: 禁用时长（秒）
            recovery_check_interval: 恢复检查间隔（秒）
        """
        self.failure_threshold = failure_threshold
        self.disable_duration = disable_duration
        self.recovery_check_interval = recovery_check_interval

        # 提供者健康状态: ProviderType -> ProviderHealth
        self._health_status: Dict[ProviderType, ProviderHealth] = {}
        self._lock = threading.Lock()

        # 初始化所有提供者的健康状态
        for provider_type in ProviderType:
            self._health_status[provider_type] = ProviderHealth(
                provider_type=provider_type
            )

    def get_health(self, provider_type: ProviderType) -> ProviderHealth:
        """获取提供者的健康状态"""
        with self._lock:
            return self._health_status.get(provider_type, ProviderHealth(provider_type=provider_type))

    def record_success(self, provider_type: ProviderType):
        """记录成功操作"""
        with self._lock:
            health = self._health_status.get(provider_type)
            if health:
                health.record_success()
                logger.debug(f"{provider_type.value} 记录成功")

    def record_failure(self, provider_type: ProviderType, error: str):
        """记录失败操作"""
        with self._lock:
            health = self._health_status.get(provider_type)
            if health:
                health.record_failure(error)

                # 检查是否需要禁用
                if health.should_disable(self.failure_threshold):
                    health.disable(self.disable_duration)
                    logger.warning(
                        f"{provider_type.value} 已禁用 {self.disable_duration} 秒，"
                        f"原因: {error}"
                    )

    def is_available(self, provider_type: ProviderType) -> bool:
        """
        检查提供者是否可用

        Args:
            provider_type: 提供者类型

        Returns:
            是否可用
        """
        health = self.get_health(provider_type)

        # 检查是否被禁用
        if health.is_disabled():
            remaining = (health.disabled_until - datetime.now()).total_seconds()
            logger.debug(
                f"{provider_type.value} 已被禁用，剩余 {int(remaining)} 秒"
            )
            return False

        return health.status != ProviderStatus.DISABLED

    def get_available_providers(
        self,
        priority_order: Optional[List[ProviderType]] = None,
    ) -> List[ProviderType]:
        """
        获取可用的提供者列表

        Args:
            priority_order: 优先级顺序，默认为 [IMAP_NEW, IMAP_OLD, GRAPH_API]

        Returns:
            可用的提供者列表
        """
        if priority_order is None:
            priority_order = [
                ProviderType.IMAP_NEW,
                ProviderType.IMAP_OLD,
                ProviderType.GRAPH_API,
            ]

        available = []
        for provider_type in priority_order:
            if self.is_available(provider_type):
                available.append(provider_type)

        return available

    def get_next_available_provider(
        self,
        priority_order: Optional[List[ProviderType]] = None,
    ) -> Optional[ProviderType]:
        """
        获取下一个可用的提供者

        Args:
            priority_order: 优先级顺序

        Returns:
            可用的提供者类型，如果没有返回 None
        """
        available = self.get_available_providers(priority_order)
        return available[0] if available else None

    def force_disable(self, provider_type: ProviderType, duration: Optional[int] = None):
        """
        强制禁用提供者

        Args:
            provider_type: 提供者类型
            duration: 禁用时长（秒），默认使用配置值
        """
        with self._lock:
            health = self._health_status.get(provider_type)
            if health:
                health.disable(duration or self.disable_duration)
                logger.warning(f"{provider_type.value} 已强制禁用")

    def force_enable(self, provider_type: ProviderType):
        """
        强制启用提供者

        Args:
            provider_type: 提供者类型
        """
        with self._lock:
            health = self._health_status.get(provider_type)
            if health:
                health.enable()
                logger.info(f"{provider_type.value} 已启用")

    def get_all_health_status(self) -> Dict[str, Any]:
        """
        获取所有提供者的健康状态

        Returns:
            健康状态字典
        """
        with self._lock:
            return {
                provider_type.value: health.to_dict()
                for provider_type, health in self._health_status.items()
            }

    def check_and_recover(self):
        """
        检查并恢复被禁用的提供者

        如果禁用时间已过，自动恢复提供者
        """
        with self._lock:
            for provider_type, health in self._health_status.items():
                if health.is_disabled():
                    # 检查是否可以恢复
                    if health.disabled_until and datetime.now() >= health.disabled_until:
                        health.enable()
                        logger.info(f"{provider_type.value} 已自动恢复")

    def reset_all(self):
        """重置所有提供者的健康状态"""
        with self._lock:
            for provider_type in ProviderType:
                self._health_status[provider_type] = ProviderHealth(
                    provider_type=provider_type
                )
            logger.info("已重置所有提供者的健康状态")


class FailoverManager:
    """
    故障切换管理器
    管理提供者之间的自动切换
    """

    def __init__(
        self,
        health_checker: HealthChecker,
        priority_order: Optional[List[ProviderType]] = None,
    ):
        """
        初始化故障切换管理器

        Args:
            health_checker: 健康检查器
            priority_order: 提供者优先级顺序
        """
        self.health_checker = health_checker
        self.priority_order = priority_order or [
            ProviderType.IMAP_NEW,
            ProviderType.IMAP_OLD,
            ProviderType.GRAPH_API,
        ]

        # 当前使用的提供者索引
        self._current_index = 0
        self._lock = threading.Lock()

    def get_current_provider(self) -> Optional[ProviderType]:
        """
        获取当前提供者

        Returns:
            当前提供者类型，如果没有可用的返回 None
        """
        available = self.health_checker.get_available_providers(self.priority_order)
        if not available:
            return None

        with self._lock:
            # 尝试使用当前索引
            if self._current_index < len(available):
                return available[self._current_index]
            return available[0]

    def switch_to_next(self) -> Optional[ProviderType]:
        """
        切换到下一个提供者

        Returns:
            下一个提供者类型，如果没有可用的返回 None
        """
        available = self.health_checker.get_available_providers(self.priority_order)
        if not available:
            return None

        with self._lock:
            self._current_index = (self._current_index + 1) % len(available)
            next_provider = available[self._current_index]
            logger.info(f"切换到提供者: {next_provider.value}")
            return next_provider

    def on_provider_success(self, provider_type: ProviderType):
        """
        提供者成功时调用

        Args:
            provider_type: 提供者类型
        """
        self.health_checker.record_success(provider_type)

        # 重置索引到成功的提供者
        with self._lock:
            available = self.health_checker.get_available_providers(self.priority_order)
            if provider_type in available:
                self._current_index = available.index(provider_type)

    def on_provider_failure(self, provider_type: ProviderType, error: str):
        """
        提供者失败时调用

        Args:
            provider_type: 提供者类型
            error: 错误信息
        """
        self.health_checker.record_failure(provider_type, error)

    def get_status(self) -> Dict[str, Any]:
        """
        获取故障切换状态

        Returns:
            状态字典
        """
        current = self.get_current_provider()
        return {
            "current_provider": current.value if current else None,
            "priority_order": [p.value for p in self.priority_order],
            "available_providers": [
                p.value for p in self.health_checker.get_available_providers(self.priority_order)
            ],
            "health_status": self.health_checker.get_all_health_status(),
        }
