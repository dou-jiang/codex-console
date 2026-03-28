"""
常量定义
"""

import random
from datetime import datetime
from enum import Enum
from typing import Dict, List, Tuple


# ============================================================================
# 枚举类型
# ============================================================================

class AccountStatus(str, Enum):
    """账户状态"""
    ACTIVE = "active"
    EXPIRED = "expired"
    BANNED = "banned"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EmailServiceType(str, Enum):
    """邮箱服务类型"""
    TEMPMAIL = "tempmail"
    YYDS_MAIL = "yyds_mail"
    OUTLOOK = "outlook"
    MOE_MAIL = "moe_mail"
    TEMP_MAIL = "temp_mail"
    DUCK_MAIL = "duck_mail"
    FREEMAIL = "freemail"
    IMAP_MAIL = "imap_mail"
    CLOUDMAIL = "cloudmail"


# ============================================================================
# 应用常量
# ============================================================================

APP_NAME = "OpenAI/Codex CLI 自动注册系统"
APP_VERSION = "1.1.1"
APP_DESCRIPTION = "自动注册 OpenAI/Codex CLI 账号的系统"

# ============================================================================
# OpenAI OAuth 相关常量
# ============================================================================

# OAuth 参数
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_AUTH_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_SCOPE = "openid email profile offline_access"

# OpenAI API 端点
OPENAI_API_ENDPOINTS = {
    "sentinel": "https://sentinel.openai.com/backend-api/sentinel/req",
    "signup": "https://auth.openai.com/api/accounts/authorize/continue",
    "register": "https://auth.openai.com/api/accounts/user/register",
    "password_verify": "https://auth.openai.com/api/accounts/password/verify",
    "send_otp": "https://auth.openai.com/api/accounts/email-otp/send",
    "validate_otp": "https://auth.openai.com/api/accounts/email-otp/validate",
    "create_account": "https://auth.openai.com/api/accounts/create_account",
    "select_workspace": "https://auth.openai.com/api/accounts/workspace/select",
}

# OpenAI 页面类型（用于判断账号状态）
OPENAI_PAGE_TYPES = {
    "EMAIL_OTP_VERIFICATION": "email_otp_verification",  # 已注册账号，需要 OTP 验证
    "PASSWORD_REGISTRATION": "create_account_password",  # 新账号，需要设置密码
    "LOGIN_PASSWORD": "login_password",  # 登录流程，需要输入密码
}

# ============================================================================
# 邮箱服务相关常量
# ============================================================================

# Tempmail.lol API 端点
TEMPMAIL_API_ENDPOINTS = {
    "create_inbox": "/inbox/create",
    "get_inbox": "/inbox",
}

# 自定义域名邮箱 API 端点
CUSTOM_DOMAIN_API_ENDPOINTS = {
    "get_config": "/api/config",
    "create_email": "/api/emails/generate",
    "list_emails": "/api/emails",
    "get_email_messages": "/api/emails/{emailId}",
    "delete_email": "/api/emails/{emailId}",
    "get_message": "/api/emails/{emailId}/{messageId}",
}

# 邮箱服务默认配置
EMAIL_SERVICE_DEFAULTS = {
    "tempmail": {
        "base_url": "https://api.tempmail.lol/v2",
        "timeout": 30,
        "max_retries": 3,
    },
    "yyds_mail": {
        "base_url": "https://maliapi.215.im/v1",
        "api_key": "",
        "default_domain": "",
        "timeout": 30,
        "max_retries": 3,
    },
    "outlook": {
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
        "smtp_server": "smtp.office365.com",
        "smtp_port": 587,
        "timeout": 30,
    },
    "moe_mail": {
        "base_url": "",  # 需要用户配置
        "api_key_header": "X-API-Key",
        "timeout": 30,
        "max_retries": 3,
    },
    "duck_mail": {
        "base_url": "",
        "default_domain": "",
        "password_length": 12,
        "timeout": 30,
        "max_retries": 3,
    },
    "freemail": {
        "base_url": "",
        "admin_token": "",
        "domain": "",
        "timeout": 30,
        "max_retries": 3,
    },
    "imap_mail": {
        "host": "",
        "port": 993,
        "use_ssl": True,
        "email": "",
        "password": "",
        "timeout": 30,
        "max_retries": 3,
    }
}

# ============================================================================
# 注册流程相关常量
# ============================================================================

# 验证码相关
OTP_CODE_PATTERN = r"(?<!\d)(\d{6})(?!\d)"
OTP_MAX_ATTEMPTS = 40  # 最大轮询次数

# 验证码提取正则（增强版）
# 简单匹配：任意 6 位数字
OTP_CODE_SIMPLE_PATTERN = r"(?<!\d)(\d{6})(?!\d)"
# 语义匹配：带上下文的验证码（如 "code is 123456", "验证码 123456"）
OTP_CODE_SEMANTIC_PATTERN = r'(?:code\s+is|验证码[是为]?\s*[:：]?\s*)(\d{6})'

# OpenAI 验证邮件发件人
OPENAI_EMAIL_SENDERS = [
    "noreply@openai.com",
    "no-reply@openai.com",
    "@openai.com",     # 精确域名匹配
    ".openai.com",     # 子域名匹配（如 otp@tm1.openai.com）
]

# OpenAI 验证邮件关键词
OPENAI_VERIFICATION_KEYWORDS = [
    "verify your email",
    "verification code",
    "验证码",
    "your openai code",
    "code is",
    "one-time code",
]

# 密码生成
PASSWORD_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
DEFAULT_PASSWORD_LENGTH = 12

# 用户信息生成（用于注册）

# 常用英文名
FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles",
    "Emma", "Olivia", "Ava", "Isabella", "Sophia", "Mia", "Charlotte", "Amelia", "Harper", "Evelyn",
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Skyler",
    "Liam", "Noah", "Ethan", "Lucas", "Mason", "Oliver", "Elijah", "Aiden", "Henry", "Sebastian",
    "Grace", "Lily", "Chloe", "Zoey", "Nora", "Aria", "Hazel", "Aurora", "Stella", "Ivy"
]

def generate_random_user_info() -> dict:
    """
    生成随机用户信息

    Returns:
        包含 name 和 birthdate 的字典
    """
    # 随机选择名字
    name = random.choice(FIRST_NAMES)

    # 生成随机生日（18-45岁）
    current_year = datetime.now().year
    birth_year = random.randint(current_year - 45, current_year - 18)
    birth_month = random.randint(1, 12)
    # 根据月份确定天数
    if birth_month in [1, 3, 5, 7, 8, 10, 12]:
        birth_day = random.randint(1, 31)
    elif birth_month in [4, 6, 9, 11]:
        birth_day = random.randint(1, 30)
    else:
        # 2月，简化处理
        birth_day = random.randint(1, 28)

    birthdate = f"{birth_year}-{birth_month:02d}-{birth_day:02d}"

    return {
        "name": name,
        "birthdate": birthdate
    }

# 保留默认值供兼容
DEFAULT_USER_INFO = {
    "name": "Neo",
    "birthdate": "2000-02-20",
}

# ============================================================================
# 代理相关常量
# ============================================================================

PROXY_TYPES = ["http", "socks5", "socks5h"]
DEFAULT_PROXY_CONFIG = {
    "enabled": False,
    "type": "http",
    "host": "127.0.0.1",
    "port": 7890,
}

# ============================================================================
# 数据库相关常量
# ============================================================================

# 数据库表名
DB_TABLE_NAMES = {
    "accounts": "accounts",
    "email_services": "email_services",
    "registration_tasks": "registration_tasks",
    "settings": "settings",
}

# 默认设置
DEFAULT_SETTINGS = [
    # (key, value, description, category)
    ("system.name", APP_NAME, "系统名称", "general"),
    ("system.version", APP_VERSION, "系统版本", "general"),
    ("logs.retention_days", "30", "日志保留天数", "general"),
    ("openai.client_id", OAUTH_CLIENT_ID, "OpenAI OAuth Client ID", "openai"),
    ("openai.auth_url", OAUTH_AUTH_URL, "OpenAI 认证地址", "openai"),
    ("openai.token_url", OAUTH_TOKEN_URL, "OpenAI Token 地址", "openai"),
    ("openai.redirect_uri", OAUTH_REDIRECT_URI, "OpenAI 回调地址", "openai"),
    ("openai.scope", OAUTH_SCOPE, "OpenAI 权限范围", "openai"),
    ("proxy.enabled", "false", "是否启用代理", "proxy"),
    ("proxy.type", "http", "代理类型 (http/socks5)", "proxy"),
    ("proxy.host", "127.0.0.1", "代理主机", "proxy"),
    ("proxy.port", "7890", "代理端口", "proxy"),
    ("registration.max_retries", "3", "最大重试次数", "registration"),
    ("registration.timeout", "120", "超时时间（秒）", "registration"),
    ("registration.default_password_length", "12", "默认密码长度", "registration"),
    ("webui.host", "0.0.0.0", "Web UI 监听主机", "webui"),
    ("webui.port", "8000", "Web UI 监听端口", "webui"),
    ("webui.debug", "true", "调试模式", "webui"),
]

# ============================================================================
# Web UI 相关常量
# ============================================================================

# WebSocket 事件
WEBSOCKET_EVENTS = {
    "CONNECT": "connect",
    "DISCONNECT": "disconnect",
    "LOG": "log",
    "STATUS": "status",
    "ERROR": "error",
    "COMPLETE": "complete",
}

# API 响应状态码
API_STATUS_CODES = {
    "SUCCESS": 200,
    "CREATED": 201,
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "INTERNAL_ERROR": 500,
}

# 分页
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# ============================================================================
# 错误消息
# ============================================================================

ERROR_MESSAGES = {
    # 通用错误
    "DATABASE_ERROR": "数据库操作失败",
    "CONFIG_ERROR": "配置错误",
    "NETWORK_ERROR": "网络连接失败",
    "TIMEOUT": "操作超时",
    "VALIDATION_ERROR": "参数验证失败",

    # 邮箱服务错误
    "EMAIL_SERVICE_UNAVAILABLE": "邮箱服务不可用",
    "EMAIL_CREATION_FAILED": "创建邮箱失败",
    "OTP_NOT_RECEIVED": "未收到验证码",
    "OTP_INVALID": "验证码无效",

    # OpenAI 相关错误
    "OPENAI_AUTH_FAILED": "OpenAI 认证失败",
    "OPENAI_RATE_LIMIT": "OpenAI 接口限流",
    "OPENAI_CAPTCHA": "遇到验证码",

    # 代理错误
    "PROXY_FAILED": "代理连接失败",
    "PROXY_AUTH_FAILED": "代理认证失败",

    # 账户错误
    "ACCOUNT_NOT_FOUND": "账户不存在",
    "ACCOUNT_ALREADY_EXISTS": "账户已存在",
    "ACCOUNT_INVALID": "账户无效",

    # 任务错误
    "TASK_NOT_FOUND": "任务不存在",
    "TASK_ALREADY_RUNNING": "任务已在运行中",
    "TASK_CANCELLED": "任务已取消",
}

# ============================================================================
# 正则表达式
# ============================================================================

REGEX_PATTERNS = {
    "EMAIL": r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    "URL": r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+",
    "IP_ADDRESS": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "OTP_CODE": OTP_CODE_PATTERN,
}

# ============================================================================
# 时间常量
# ============================================================================

TIME_CONSTANTS = {
    "SECOND": 1,
    "MINUTE": 60,
    "HOUR": 3600,
    "DAY": 86400,
    "WEEK": 604800,
}


# ============================================================================
# Microsoft/Outlook 相关常量
# ============================================================================

# Microsoft OAuth2 Token 端点
MICROSOFT_TOKEN_ENDPOINTS = {
    # 旧版 IMAP 使用的端点
    "LIVE": "https://login.live.com/oauth20_token.srf",
    # 新版 IMAP 使用的端点（需要特定 scope）
    "CONSUMERS": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    # Graph API 使用的端点
    "COMMON": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
}

# IMAP 服务器配置
OUTLOOK_IMAP_SERVERS = {
    "OLD": "outlook.office365.com",  # 旧版 IMAP
    "NEW": "outlook.live.com",       # 新版 IMAP
}

# Microsoft OAuth2 Scopes
MICROSOFT_SCOPES = {
    # 旧版 IMAP 不需要特定 scope
    "IMAP_OLD": "",
    # 新版 IMAP 需要的 scope
    "IMAP_NEW": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    # Graph API 需要的 scope
    "GRAPH_API": "https://graph.microsoft.com/.default",
}

# Outlook 提供者默认优先级
OUTLOOK_PROVIDER_PRIORITY = ["imap_new", "imap_old", "graph_api"]
