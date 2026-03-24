"""
SQLAlchemy ORM 模型定义
"""

from datetime import datetime
from typing import Optional, Dict, Any
import json
from sqlalchemy import Column, Integer, Float, String, Text, Boolean, DateTime, ForeignKey
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
    primary_cpa_service_id = Column(Integer)  # 主 CPA 服务 ID（用于任务匹配）
    invalidated_at = Column(DateTime)  # 本地判定失效时间
    invalid_reason = Column(String(64))  # 失效原因（如 cpa_cleanup）
    source = Column(String(20), default='register')  # 'register' 或 'login'，区分账号来源
    subscription_type = Column(String(20))  # None / 'plus' / 'team'
    subscription_at = Column(DateTime)  # 订阅开通时间
    cookies = Column(Text)  # 完整 cookie 字符串，用于支付请求
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
            'primary_cpa_service_id': self.primary_cpa_service_id,
            'invalidated_at': self.invalidated_at.isoformat() if self.invalidated_at else None,
            'invalid_reason': self.invalid_reason,
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
    email_address = Column(String(255), index=True)  # 注册时使用的邮箱地址
    pipeline_key = Column(String(64), index=True)  # 流水线标识
    pair_key = Column(String(64), index=True)  # 实验分组键
    experiment_batch_id = Column(Integer, ForeignKey('experiment_batches.id'), index=True)  # 实验批次 ID
    current_step_key = Column(String(64), index=True)  # 当前执行步骤
    assigned_proxy_id = Column(Integer, ForeignKey('proxies.id'), index=True)  # 任务分配代理
    assigned_proxy_url = Column(String(255))  # 任务分配代理 URL
    proxy_check_run_id = Column(Integer, ForeignKey('proxy_check_runs.id'), index=True)  # 预检运行 ID
    total_duration_ms = Column(Integer)  # 总耗时
    pipeline_status = Column(String(20), index=True)  # 流水线状态
    logs = Column(Text)  # 注册过程日志
    result = Column(JSONEncodedDict)  # 注册结果
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    # 关系
    email_service = relationship('EmailService')
    experiment_batch = relationship('ExperimentBatch', back_populates='registration_tasks')


class PipelineStepRun(Base):
    """流水线 Step 执行记录表"""
    __tablename__ = 'pipeline_step_runs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_uuid = Column(String(36), index=True, nullable=False)
    pipeline_key = Column(String(64), nullable=False, index=True)
    step_key = Column(String(64), nullable=False, index=True)
    step_order = Column(Integer, nullable=False)
    step_impl = Column(String(100))
    status = Column(String(20), nullable=False, default='pending')
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    duration_ms = Column(Integer)
    input_summary = Column(Text)
    output_summary = Column(Text)
    error_code = Column(String(64))
    error_message = Column(Text)
    metadata_json = Column(JSONEncodedDict)
    created_at = Column(DateTime, default=datetime.utcnow)


class ExperimentBatch(Base):
    """实验批次表"""
    __tablename__ = 'experiment_batches'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    mode = Column(String(32), nullable=False, default='paired_compare')
    status = Column(String(20), nullable=False, default='pending', index=True)
    pipelines = Column(Text, nullable=False)
    email_service_type = Column(String(50))
    email_service_config_snapshot = Column(JSONEncodedDict)
    proxy_strategy_snapshot = Column(JSONEncodedDict)
    target_count = Column(Integer, default=0)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    registration_tasks = relationship('RegistrationTask', back_populates='experiment_batch')
    survival_checks = relationship('AccountSurvivalCheck', back_populates='experiment_batch')


class RegistrationBatchStat(Base):
    """普通批量注册统计快照主表"""
    __tablename__ = 'registration_batch_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(36), nullable=False, unique=True, index=True)
    status = Column(String(20), nullable=False, index=True)
    mode = Column(String(32), nullable=False)
    pipeline_key = Column(String(64), nullable=False, index=True)
    email_service_type = Column(String(50))
    email_service_id = Column(Integer, ForeignKey('email_services.id'), index=True)
    proxy_strategy_snapshot = Column(JSONEncodedDict)
    config_snapshot = Column(JSONEncodedDict)
    target_count = Column(Integer, default=0)
    finished_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    total_duration_ms = Column(Integer)
    avg_duration_ms = Column(Float)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    email_service = relationship('EmailService')
    step_stats = relationship(
        'RegistrationBatchStepStat',
        back_populates='batch_stat',
        cascade='all, delete-orphan',
        order_by=lambda: (RegistrationBatchStepStat.step_order, RegistrationBatchStepStat.id),
    )
    stage_stats = relationship('RegistrationBatchStageStat', back_populates='batch_stat', cascade='all, delete-orphan')


class RegistrationBatchStepStat(Base):
    """普通批量注册统计快照 Step 聚合表"""
    __tablename__ = 'registration_batch_step_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_stat_id = Column(Integer, ForeignKey('registration_batch_stats.id'), nullable=False, index=True)
    step_key = Column(String(64), nullable=False, index=True)
    step_order = Column(Integer, nullable=False)
    sample_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    avg_duration_ms = Column(Float)
    p50_duration_ms = Column(Integer)
    p90_duration_ms = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

    batch_stat = relationship('RegistrationBatchStat', back_populates='step_stats')


class RegistrationBatchStageStat(Base):
    """普通批量注册统计快照阶段聚合表"""
    __tablename__ = 'registration_batch_stage_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_stat_id = Column(Integer, ForeignKey('registration_batch_stats.id'), nullable=False, index=True)
    stage_key = Column(String(64), nullable=False, index=True)
    sample_count = Column(Integer, default=0)
    avg_duration_ms = Column(Float)
    p50_duration_ms = Column(Integer)
    p90_duration_ms = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

    batch_stat = relationship('RegistrationBatchStat', back_populates='stage_stats')


class ProxyCheckRun(Base):
    """代理预检运行记录表"""
    __tablename__ = 'proxy_check_runs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_type = Column(String(32), nullable=False, index=True)
    scope_id = Column(String(64), index=True)
    status = Column(String(20), nullable=False, default='pending', index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    total_count = Column(Integer, default=0)
    available_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    results = relationship('ProxyCheckResult', back_populates='run', cascade='all, delete-orphan')


class ProxyCheckResult(Base):
    """代理预检结果表"""
    __tablename__ = 'proxy_check_results'

    id = Column(Integer, primary_key=True, autoincrement=True)
    proxy_check_run_id = Column(Integer, ForeignKey('proxy_check_runs.id'), nullable=False, index=True)
    proxy_id = Column(Integer, ForeignKey('proxies.id'), index=True)
    proxy_url = Column(String(255))
    status = Column(String(20), nullable=False, index=True)
    latency_ms = Column(Integer)
    country_code = Column(String(8))
    ip_address = Column(String(64))
    error_message = Column(Text)
    checked_at = Column(DateTime, default=datetime.utcnow)

    run = relationship('ProxyCheckRun', back_populates='results')
    proxy = relationship('Proxy')


class AccountSurvivalCheck(Base):
    """账号存活检查记录表"""
    __tablename__ = 'account_survival_checks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False, index=True)
    task_uuid = Column(String(36), index=True)
    pipeline_key = Column(String(64), index=True)
    experiment_batch_id = Column(Integer, ForeignKey('experiment_batches.id'), index=True)
    check_source = Column(String(16), nullable=False)
    check_stage = Column(String(16), nullable=False)
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    result_level = Column(String(16), nullable=False, index=True)
    signal_type = Column(String(64))
    latency_ms = Column(Integer)
    detail_json = Column(JSONEncodedDict)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship('Account')
    experiment_batch = relationship('ExperimentBatch', back_populates='survival_checks')


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


class ScheduledPlan(Base):
    """定时计划表"""
    __tablename__ = 'scheduled_plans'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    task_type = Column(String(32), nullable=False, index=True)  # cpa_cleanup / cpa_refill / account_refresh
    enabled = Column(Boolean, default=True, nullable=False)
    cpa_service_id = Column(Integer, ForeignKey('cpa_services.id'), nullable=False, index=True)
    trigger_type = Column(String(16), nullable=False)  # cron / interval
    cron_expression = Column(String(120))
    interval_value = Column(Integer)
    interval_unit = Column(String(16))
    config = Column(JSONEncodedDict, nullable=False)
    config_meta = Column(JSONEncodedDict)
    next_run_at = Column(DateTime)
    last_run_started_at = Column(DateTime)
    last_run_finished_at = Column(DateTime)
    last_run_status = Column(String(20))
    last_success_at = Column(DateTime)
    auto_disabled_reason = Column(String(120))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cpa_service = relationship('CpaService')
    runs = relationship('ScheduledRun', back_populates='plan', cascade='all, delete-orphan')


class ScheduledRun(Base):
    """定时计划执行记录表"""
    __tablename__ = 'scheduled_runs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey('scheduled_plans.id'), nullable=False, index=True)
    task_type = Column(String(32), index=True)
    trigger_source = Column(String(16), nullable=False)  # scheduled / manual
    status = Column(String(20), nullable=False, default='running')  # running / success / failed / skipped
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
    stop_requested_at = Column(DateTime)
    stop_requested_by = Column(String(64))
    stop_reason = Column(Text)
    last_log_at = Column(DateTime)
    log_version = Column(Integer, nullable=False, default=0)
    summary = Column(JSONEncodedDict)
    error_message = Column(Text)
    logs = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    plan = relationship('ScheduledPlan', back_populates='runs')


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
    country = Column(String(100))
    city = Column(String(100))
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
            'country': self.country,
            'city': self.city,
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
