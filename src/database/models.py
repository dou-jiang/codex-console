"""
SQLAlchemy ORM 模型定义
"""

from datetime import datetime
from typing import Optional, Dict, Any
import json
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import relationship

Base = declarative_base()


class JSONEncodedDict(TypeDecorator):
    """JSON 编码字典类型"""
    impl = Text

    def process_bind_param(self, value: Optional[Dict[str, Any]], dialect):
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: Optional[str], dialect):
        if value is None:
            return None
        return json.loads(value)


class Account(Base):
    """已注册账号表"""
    __tablename__ = 'accounts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password = Column(String(255))  # 注册密码（明文存储）
    access_token = Column(Text)
    refresh_token = Column(Text)
    id_token = Column(Text)
    session_token = Column(Text)  # 会话令牌（优先刷新方式）
    client_id = Column(String(255))  # OAuth Client ID
    account_id = Column(String(255))
    workspace_id = Column(String(255))
    email_service = Column(String(50), nullable=False)  # 'tempmail', 'outlook', 'moe_mail'
    email_service_id = Column(String(255))  # 邮箱服务中的ID
    proxy_used = Column(String(255))
    registered_at = Column(DateTime, default=datetime.utcnow)
    last_refresh = Column(DateTime)  # 最后刷新时间
    expires_at = Column(DateTime)  # Token 过期时间
    status = Column(String(20), default='active')  # 'active', 'expired', 'banned', 'failed'
    extra_data = Column(JSONEncodedDict)  # 额外信息存储
    cpa_uploaded = Column(Boolean, default=False)  # 是否已上传到 CPA
    cpa_uploaded_at = Column(DateTime)  # 上传时间
    source = Column(String(20), default='register')  # 'register' 或 'login'，区分账号来源
    subscription_type = Column(String(20))  # None / 'plus' / 'team'
    subscription_at = Column(DateTime)  # 订阅开通时间
    cookies = Column(Text)  # 完整 cookie 字符串，用于支付请求
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    bind_card_tasks = relationship("BindCardTask", back_populates="account")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'email': self.email,
            'password': self.password,
            'client_id': self.client_id,
            'email_service': self.email_service,
            'account_id': self.account_id,
            'workspace_id': self.workspace_id,
            'registered_at': self.registered_at.isoformat() if self.registered_at else None,
            'last_refresh': self.last_refresh.isoformat() if self.last_refresh else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'status': self.status,
            'proxy_used': self.proxy_used,
            'cpa_uploaded': self.cpa_uploaded,
            'cpa_uploaded_at': self.cpa_uploaded_at.isoformat() if self.cpa_uploaded_at else None,
            'source': self.source,
            'subscription_type': self.subscription_type,
            'subscription_at': self.subscription_at.isoformat() if self.subscription_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class EmailService(Base):
    """邮箱服务配置表"""
    __tablename__ = 'email_services'

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_type = Column(String(50), nullable=False)  # 'outlook', 'moe_mail'
    name = Column(String(100), nullable=False)
    config = Column(JSONEncodedDict, nullable=False)  # 服务配置（加密存储）
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)  # 使用优先级
    last_used = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RegistrationTask(Base):
    """注册任务表"""
    __tablename__ = 'registration_tasks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_uuid = Column(String(36), unique=True, nullable=False, index=True)  # 任务唯一标识
    status = Column(String(20), default='pending')  # 'pending', 'running', 'completed', 'failed', 'cancelled'
    email_service_id = Column(Integer, ForeignKey('email_services.id'), index=True)  # 使用的邮箱服务
    proxy = Column(String(255))  # 使用的代理
    logs = Column(Text)  # 注册过程日志
    result = Column(JSONEncodedDict)  # 注册结果
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    # 关系
    email_service = relationship('EmailService')


class BindCardTask(Base):
    """绑卡任务表"""
    __tablename__ = "bind_card_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    plan_type = Column(String(20), nullable=False)  # plus / team
    workspace_name = Column(String(255))
    price_interval = Column(String(20))
    seat_quantity = Column(Integer)
    country = Column(String(10), default="US")
    currency = Column(String(10), default="USD")
    checkout_url = Column(Text, nullable=False)
    checkout_session_id = Column(String(120))
    publishable_key = Column(String(255))
    client_secret = Column(Text)
    checkout_source = Column(String(50))  # openai_checkout / aimizy_fallback
    bind_mode = Column(String(30), default="semi_auto")  # semi_auto / third_party / local_auto
    status = Column(String(20), default="link_ready")  # link_ready / opened / waiting_user_action / verifying / completed / failed
    last_error = Column(Text)
    opened_at = Column(DateTime)
    last_checked_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    account = relationship("Account", back_populates="bind_card_tasks")


class AppLog(Base):
    """应用日志表（后台日志监控）"""
    __tablename__ = "app_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(20), nullable=False, index=True)
    logger = Column(String(255), nullable=False, index=True)
    module = Column(String(255))
    pathname = Column(String(500))
    lineno = Column(Integer)
    message = Column(Text, nullable=False)
    exception = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level,
            "logger": self.logger,
            "module": self.module,
            "pathname": self.pathname,
            "lineno": self.lineno,
            "message": self.message,
            "exception": self.exception,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Setting(Base):
    """系统设置表"""
    __tablename__ = 'settings'

    key = Column(String(100), primary_key=True)
    value = Column(Text)
    description = Column(Text)
    category = Column(String(50), default='general')  # 'general', 'email', 'proxy', 'openai'
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CpaService(Base):
    """CPA 服务配置表"""
    __tablename__ = 'cpa_services'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)  # 服务名称
    api_url = Column(String(500), nullable=False)  # API URL
    api_token = Column(Text, nullable=False)  # API Token
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)  # 优先级
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Sub2ApiService(Base):
    """Sub2API 服务配置表"""
    __tablename__ = 'sub2api_services'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)  # 服务名称
    api_url = Column(String(500), nullable=False)  # API URL (host)
    api_key = Column(Text, nullable=False)  # x-api-key
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)  # 优先级
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TeamManagerService(Base):
    """Team Manager 服务配置表"""
    __tablename__ = 'tm_services'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)  # 服务名称
    api_url = Column(String(500), nullable=False)  # API URL
    api_key = Column(Text, nullable=False)  # X-API-Key
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)  # 优先级
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Proxy(Base):
    """代理列表表"""
    __tablename__ = 'proxies'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)  # 代理名称
    type = Column(String(20), nullable=False, default='http')  # http, socks5
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String(100))
    password = Column(String(255))
    enabled = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)  # 是否为默认代理
    priority = Column(Integer, default=0)  # 优先级（保留字段）
    last_used = Column(DateTime)  # 最后使用时间
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, include_password: bool = False) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'host': self.host,
            'port': self.port,
            'username': self.username,
            'enabled': self.enabled,
            'is_default': self.is_default or False,
            'priority': self.priority,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_password:
            result['password'] = self.password
        else:
            result['has_password'] = bool(self.password)
        return result

    @property
    def proxy_url(self) -> str:
        """获取完整的代理 URL"""
        if self.type == "http":
            scheme = "http"
        elif self.type == "socks5":
            scheme = "socks5"
        else:
            scheme = self.type

        auth = ""
        if self.username and self.password:
            auth = f"{self.username}:{self.password}@"

        return f"{scheme}://{auth}{self.host}:{self.port}"
