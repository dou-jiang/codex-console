"""
Outlook 服务基础定义
包含枚举类型和数据类
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List


class ProviderType(str, Enum):
    """Outlook 提供者类型"""
    IMAP_OLD = "imap_old"      # 旧版 IMAP (outlook.office365.com)
    IMAP_NEW = "imap_new"      # 新版 IMAP (outlook.live.com)
    GRAPH_API = "graph_api"    # Microsoft Graph API


class TokenEndpoint(str, Enum):
    """Token 端点"""
    LIVE = "https://login.live.com/oauth20_token.srf"
    CONSUMERS = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    COMMON = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


class IMAPServer(str, Enum):
    """IMAP 服务器"""
    OLD = "outlook.office365.com"
    NEW = "outlook.live.com"


class ProviderStatus(str, Enum):
    """提供者状态"""
    HEALTHY = "healthy"        # 健康
    DEGRADED = "degraded"      # 降级
    DISABLED = "disabled"      # 禁用


@dataclass
class EmailMessage:
    """邮件消息数据类"""
    id: str                                    # 消息 ID
    subject: str                               # 主题
    sender: str                                # 发件人
    recipients: List[str] = field(default_factory=list)  # 收件人列表
    body: str = ""                             # 正文内容
    body_preview: str = ""                     # 正文预览
    received_at: Optional[datetime] = None     # 接收时间
    received_timestamp: int = 0                # 接收时间戳
    is_read: bool = False                      # 是否已读
    has_attachments: bool = False              # 是否有附件
    raw_data: Optional[bytes] = None           # 原始数据（用于调试）

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "subject": self.subject,
            "sender": self.sender,
            "recipients": self.recipients,
            "body": self.body,
            "body_preview": self.body_preview,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "received_timestamp": self.received_timestamp,
            "is_read": self.is_read,
            "has_attachments": self.has_attachments,
        }


@dataclass
class TokenInfo:
    """Token 信息数据类"""
    access_token: str
    expires_at: float              # 过期时间戳
    token_type: str = "Bearer"
    scope: str = ""
    refresh_token: Optional[str] = None

    def is_expired(self, buffer_seconds: int = 120) -> bool:
        """检查 Token 是否已过期"""
        import time
        return time.time() >= (self.expires_at - buffer_seconds)

    @classmethod
    def from_response(cls, data: Dict[str, Any], scope: str = "") -> "TokenInfo":
        """从 API 响应创建"""
        import time
        return cls(
            access_token=data.get("access_token", ""),
            expires_at=time.time() + data.get("expires_in", 3600),
            token_type=data.get("token_type", "Bearer"),
            scope=scope or data.get("scope", ""),
            refresh_token=data.get("refresh_token"),
        )


@dataclass
class ProviderHealth:
    """提供者健康状态"""
    provider_type: ProviderType
    status: ProviderStatus = ProviderStatus.HEALTHY
    failure_count: int = 0                       # 连续失败次数
    last_success: Optional[datetime] = None      # 最后成功时间
    last_failure: Optional[datetime] = None      # 最后失败时间
    last_error: str = ""                         # 最后错误信息
    disabled_until: Optional[datetime] = None    # 禁用截止时间

    def record_success(self):
        """记录成功"""
        self.status = ProviderStatus.HEALTHY
        self.failure_count = 0
        self.last_success = datetime.now()
        self.disabled_until = None

    def record_failure(self, error: str):
        """记录失败"""
        self.failure_count += 1
        self.last_failure = datetime.now()
        self.last_error = error

    def should_disable(self, threshold: int = 3) -> bool:
        """判断是否应该禁用"""
        return self.failure_count >= threshold

    def is_disabled(self) -> bool:
        """检查是否被禁用"""
        if self.disabled_until and datetime.now() < self.disabled_until:
            return True
        return False

    def disable(self, duration_seconds: int = 300):
        """禁用提供者"""
        from datetime import timedelta
        self.status = ProviderStatus.DISABLED
        self.disabled_until = datetime.now() + timedelta(seconds=duration_seconds)

    def enable(self):
        """启用提供者"""
        self.status = ProviderStatus.HEALTHY
        self.disabled_until = None
        self.failure_count = 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "provider_type": self.provider_type.value,
            "status": self.status.value,
            "failure_count": self.failure_count,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "last_error": self.last_error,
            "disabled_until": self.disabled_until.isoformat() if self.disabled_until else None,
        }
