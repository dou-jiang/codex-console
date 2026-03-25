"""
通用工具函数
"""

import os
import sys
import json
import time
import random
import string
import secrets
import hashlib
import logging
import base64
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union, Callable
from pathlib import Path

from ..config.constants import PASSWORD_CHARSET, DEFAULT_PASSWORD_LENGTH
from ..config.settings import get_settings
from .timezone_utils import SHANGHAI_TZ


class ShanghaiTimeFormatter(logging.Formatter):
    """
    强制日志 asctime 输出为上海时间，避免容器/服务器时区差异。
    """

    def formatTime(self, record, datefmt=None):  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone(SHANGHAI_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(
    log_level: str = "INFO",
    log_file: Optional[str] = None,
    log_format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
) -> logging.Logger:
    """
    配置日志系统

    Args:
        log_level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: 日志文件路径，如果不指定则只输出到控制台
        log_format: 日志格式

    Returns:
        根日志记录器
    """
    # 设置日志级别
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    # 配置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # 清除现有的处理器
    root_logger.handlers.clear()

    # 创建格式化器
    formatter = ShanghaiTimeFormatter(log_format)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(numeric_level)
    root_logger.addHandler(console_handler)

    # 文件处理器（如果指定了日志文件）
    if log_file:
        # 确保日志目录存在
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(numeric_level)
        root_logger.addHandler(file_handler)

    return root_logger


def generate_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    """
    生成随机密码

    Args:
        length: 密码长度

    Returns:
        随机密码字符串
    """
    if length < 4:
        length = 4

    # 确保密码包含至少一个大写字母、一个小写字母和一个数字
    password = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
    ]

    # 添加剩余字符
    password.extend(secrets.choice(PASSWORD_CHARSET) for _ in range(length - 3))

    # 随机打乱
    secrets.SystemRandom().shuffle(password)

    return ''.join(password)


def generate_random_string(length: int = 8) -> str:
    """
    生成随机字符串（仅字母）

    Args:
        length: 字符串长度

    Returns:
        随机字符串
    """
    chars = string.ascii_letters
    return ''.join(secrets.choice(chars) for _ in range(length))


def generate_uuid() -> str:
    """生成 UUID 字符串"""
    return str(uuid.uuid4())


def get_timestamp() -> int:
    """获取当前时间戳（秒）"""
    return int(time.time())


def format_datetime(dt: Optional[datetime] = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    格式化日期时间

    Args:
        dt: 日期时间对象，如果为 None 则使用当前时间
        fmt: 格式字符串

    Returns:
        格式化后的字符串
    """
    if dt is None:
        dt = datetime.now()
    return dt.strftime(fmt)


def parse_datetime(dt_str: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:
    """
    解析日期时间字符串

    Args:
        dt_str: 日期时间字符串
        fmt: 格式字符串

    Returns:
        日期时间对象，如果解析失败返回 None
    """
    try:
        return datetime.strptime(dt_str, fmt)
    except (ValueError, TypeError):
        return None


def human_readable_size(size_bytes: int) -> str:
    """
    将字节大小转换为人类可读的格式

    Args:
        size_bytes: 字节大小

    Returns:
        人类可读的字符串
    """
    if size_bytes < 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0

    while size_bytes >= 1024 and unit_index < len(units) - 1:
        size_bytes /= 1024
        unit_index += 1

    return f"{size_bytes:.2f} {units[unit_index]}"


def retry_with_backoff(
    func: Callable,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,)
) -> Any:
    """
    带有指数退避的重试装饰器/函数

    Args:
        func: 要重试的函数
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟（秒）
        backoff_factor: 退避因子
        exceptions: 要捕获的异常类型

    Returns:
        函数的返回值

    Raises:
        最后一次尝试的异常
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return func()
        except exceptions as e:
            last_exception = e

            # 如果是最后一次尝试，直接抛出异常
            if attempt == max_retries:
                break

            # 计算延迟时间
            delay = min(base_delay * (backoff_factor ** attempt), max_delay)

            # 添加随机抖动
            delay *= (0.5 + random.random())

            # 记录日志
            logger = logging.getLogger(__name__)
            logger.warning(
                f"尝试 {func.__name__} 失败 (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                f"等待 {delay:.2f} 秒后重试..."
            )

            time.sleep(delay)

    # 所有重试都失败，抛出最后一个异常
    raise last_exception


class RetryDecorator:
    """重试装饰器类"""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        exceptions: tuple = (Exception,)
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.exceptions = exceptions

    def __call__(self, func: Callable) -> Callable:
        """装饰器调用"""
        def wrapper(*args, **kwargs):
            def func_to_retry():
                return func(*args, **kwargs)

            return retry_with_backoff(
                func_to_retry,
                max_retries=self.max_retries,
                base_delay=self.base_delay,
                max_delay=self.max_delay,
                backoff_factor=self.backoff_factor,
                exceptions=self.exceptions
            )

        return wrapper


def validate_email(email: str) -> bool:
    """
    验证邮箱地址格式

    Args:
        email: 邮箱地址

    Returns:
        是否有效
    """
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def validate_url(url: str) -> bool:
    """
    验证 URL 格式

    Args:
        url: URL

    Returns:
        是否有效
    """
    pattern = r"^https?://[^\s/$.?#].[^\s]*$"
    return bool(re.match(pattern, url))


def sanitize_filename(filename: str) -> str:
    """
    清理文件名，移除不安全的字符

    Args:
        filename: 原始文件名

    Returns:
        清理后的文件名
    """
    # 移除危险字符
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # 移除控制字符
    filename = ''.join(char for char in filename if ord(char) >= 32)
    # 限制长度
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:255 - len(ext)] + ext
    return filename


def read_json_file(filepath: str) -> Optional[Dict[str, Any]]:
    """
    读取 JSON 文件

    Args:
        filepath: 文件路径

    Returns:
        JSON 数据，如果读取失败返回 None
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError) as e:
        logging.getLogger(__name__).warning(f"读取 JSON 文件失败: {filepath} - {e}")
        return None


def write_json_file(filepath: str, data: Dict[str, Any], indent: int = 2) -> bool:
    """
    写入 JSON 文件

    Args:
        filepath: 文件路径
        data: 要写入的数据
        indent: 缩进空格数

    Returns:
        是否成功
    """
    try:
        # 确保目录存在
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)

        return True
    except (IOError, TypeError) as e:
        logging.getLogger(__name__).error(f"写入 JSON 文件失败: {filepath} - {e}")
        return False


def get_project_root() -> Path:
    """
    获取项目根目录

    Returns:
        项目根目录 Path 对象
    """
    # 当前文件所在目录
    current_dir = Path(__file__).parent

    # 向上查找直到找到项目根目录（包含 pyproject.toml 或 setup.py）
    for parent in [current_dir] + list(current_dir.parents):
        if (parent / "pyproject.toml").exists() or (parent / "setup.py").exists():
            return parent

    # 如果找不到，返回当前目录的父目录
    return current_dir.parent


def get_data_dir() -> Path:
    """
    获取数据目录

    Returns:
        数据目录 Path 对象
    """
    settings = get_settings()
    if not settings.database_url.startswith("sqlite"):
        data_dir = Path(os.environ.get("APP_DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    data_dir = Path(settings.database_url).parent

    # 如果 database_url 是 SQLite URL，提取路径
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url[10:]  # 移除 "sqlite:///"
        data_dir = Path(db_path).parent

    # 确保目录存在
    data_dir.mkdir(parents=True, exist_ok=True)

    return data_dir


def get_logs_dir() -> Path:
    """
    获取日志目录

    Returns:
        日志目录 Path 对象
    """
    settings = get_settings()
    log_file = Path(settings.log_file)
    log_dir = log_file.parent

    # 确保目录存在
    log_dir.mkdir(parents=True, exist_ok=True)

    return log_dir


def format_duration(seconds: int) -> str:
    """
    格式化持续时间

    Args:
        seconds: 秒数

    Returns:
        格式化的持续时间字符串
    """
    if seconds < 60:
        return f"{seconds}秒"

    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{seconds}秒"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{minutes}分"

    days, hours = divmod(hours, 24)
    return f"{days}天{hours}小时"


def mask_sensitive_data(data: Union[str, Dict, List], mask_char: str = "*") -> Union[str, Dict, List]:
    """
    掩码敏感数据

    Args:
        data: 要掩码的数据
        mask_char: 掩码字符

    Returns:
        掩码后的数据
    """
    if isinstance(data, str):
        # 如果是邮箱，掩码中间部分
        if "@" in data:
            local, domain = data.split("@", 1)
            if len(local) > 2:
                masked_local = local[0] + mask_char * (len(local) - 2) + local[-1]
            else:
                masked_local = mask_char * len(local)
            return f"{masked_local}@{domain}"

        # 如果是 token 或密钥，掩码大部分内容
        if len(data) > 10:
            return data[:4] + mask_char * (len(data) - 8) + data[-4:]
        return mask_char * len(data)

    elif isinstance(data, dict):
        masked_dict = {}
        for key, value in data.items():
            # 敏感字段名
            sensitive_keys = ["password", "token", "secret", "key", "auth", "credential"]
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                masked_dict[key] = mask_sensitive_data(value, mask_char)
            else:
                masked_dict[key] = value
        return masked_dict

    elif isinstance(data, list):
        return [mask_sensitive_data(item, mask_char) for item in data]

    return data


def calculate_md5(data: Union[str, bytes]) -> str:
    """
    计算 MD5 哈希

    Args:
        data: 要哈希的数据

    Returns:
        MD5 哈希字符串
    """
    if isinstance(data, str):
        data = data.encode('utf-8')

    return hashlib.md5(data).hexdigest()


def calculate_sha256(data: Union[str, bytes]) -> str:
    """
    计算 SHA256 哈希

    Args:
        data: 要哈希的数据

    Returns:
        SHA256 哈希字符串
    """
    if isinstance(data, str):
        data = data.encode('utf-8')

    return hashlib.sha256(data).hexdigest()


def base64_encode(data: Union[str, bytes]) -> str:
    """Base64 编码"""
    if isinstance(data, str):
        data = data.encode('utf-8')

    return base64.b64encode(data).decode('utf-8')


def base64_decode(data: str) -> str:
    """Base64 解码"""
    try:
        decoded = base64.b64decode(data)
        return decoded.decode('utf-8')
    except (base64.binascii.Error, UnicodeDecodeError):
        return ""


class Timer:
    """计时器上下文管理器"""

    def __init__(self, name: str = "操作"):
        self.name = name
        self.start_time = None
        self.elapsed = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.elapsed = time.time() - self.start_time
        logger = logging.getLogger(__name__)
        logger.debug(f"{self.name} 耗时: {self.elapsed:.2f} 秒")

    def get_elapsed(self) -> float:
        """获取经过的时间（秒）"""
        if self.elapsed is not None:
            return self.elapsed
        if self.start_time is not None:
            return time.time() - self.start_time
        return 0.0
