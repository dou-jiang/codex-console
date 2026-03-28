"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import re
import json
import time
import logging
import secrets
import string
import uuid
from typing import Optional, Dict, Any, Tuple, Callable, List
from dataclasses import dataclass
from datetime import datetime

from curl_cffi import requests as cffi_requests

from .openai.oauth import OAuthManager, OAuthStart
from .http_client import OpenAIHTTPClient, HTTPClientError
from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType
from ..database import crud
from ..database.session import get_db
from ..config.constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
)
from ..config.settings import get_settings


logger = logging.getLogger(__name__)


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    device_id: str = ""  # oai-did
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "device_id": self.device_id,
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            callback_logger: 日志回调函数
            task_uuid: 任务 UUID（用于数据库记录）
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid

        # 创建 HTTP 客户端
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)

        # 创建 OAuth 管理器
        settings = get_settings()
        self.oauth_manager = OAuthManager(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=settings.openai_redirect_uri,
            scope=settings.openai_scope,
            proxy_url=proxy_url  # 传递代理配置
        )
        entry_flow = str(getattr(settings, "registration_entry_flow", "native") or "native").strip().lower()
        # 配置层仅保留 native/abcard；Outlook 邮箱在执行时自动切换 outlook 链路。
        self.registration_entry_flow: str = entry_flow if entry_flow in {"native", "abcard"} else "native"

        # 状态变量
        self.email: Optional[str] = None
        self.inbox_email: Optional[str] = None  # 邮箱服务原始地址（用于收件）
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.device_id: Optional[str] = None  # oai-did
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self._token_acquisition_requires_login: bool = False  # 新注册账号需要二次登录拿 token
        self._create_account_continue_url: Optional[str] = None  # create_account 返回的 continue_url（ABCard链路兜底）
        self._create_account_workspace_id: Optional[str] = None
        self._create_account_account_id: Optional[str] = None
        self._create_account_refresh_token: Optional[str] = None
        self._last_validate_otp_continue_url: Optional[str] = None
        self._last_validate_otp_workspace_id: Optional[str] = None
        self._last_register_password_error: Optional[str] = None
        self._last_otp_validation_code: Optional[str] = None
        self._last_otp_validation_status_code: Optional[int] = None
        self._last_otp_validation_outcome: str = ""  # success/http_non_200/network_timeout/network_error

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(log_message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _dump_session_cookies(self) -> str:
        """导出当前会话 cookies（用于后续支付/绑卡自动化）。"""
        if not self.session:
            return ""
        try:
            cookie_map: dict[str, str] = {}
            order: list[str] = []

            def _push(name: Optional[str], value: Optional[str]):
                key = str(name or "").strip()
                val = str(value or "").strip()
                if not key:
                    return
                if key not in cookie_map:
                    cookie_map[key] = val
                    order.append(key)
                    return
                # 同名 cookie 可能来自不同域/路径：优先保留非空且更长值，避免空值覆盖有效分片。
                prev = str(cookie_map.get(key) or "").strip()
                if (not prev and val) or (val and len(val) > len(prev)):
                    cookie_map[key] = val

            # 1) 常规 requests/curl_cffi 字典接口
            try:
                for key, value in self.session.cookies.items():
                    _push(key, value)
            except Exception:
                pass

            # 2) CookieJar 接口（可拿到分片 cookie）
            try:
                jar = getattr(self.session.cookies, "jar", None)
                if jar is not None:
                    for cookie in jar:
                        _push(getattr(cookie, "name", ""), getattr(cookie, "value", ""))
            except Exception:
                pass

            # 3) 关键 cookie 兜底读取
            for key in (
                "oai-did",
                "oai-client-auth-session",
                "__Secure-next-auth.session-token",
                "_Secure-next-auth.session-token",
            ):
                try:
                    _push(key, self.session.cookies.get(key))
                except Exception:
                    continue

            pairs = [(k, cookie_map.get(k, "")) for k in order if k]
            return "; ".join(f"{k}={v}" for k, v in pairs if k)
        except Exception:
            return ""

    @staticmethod
    def _extract_session_token_from_cookie_jar(cookie_jar) -> str:
        """
        从 CookieJar 中提取 next-auth session token（兼容分片 + 重复域名）。
        """
        if not cookie_jar:
            return ""

        entries: list[tuple[str, str]] = []
        try:
            for key, value in cookie_jar.items():
                entries.append((str(key or "").strip(), str(value or "").strip()))
        except Exception:
            pass

        try:
            jar = getattr(cookie_jar, "jar", None)
            if jar is not None:
                for cookie in jar:
                    entries.append(
                        (
                            str(getattr(cookie, "name", "") or "").strip(),
                            str(getattr(cookie, "value", "") or "").strip(),
                        )
                    )
        except Exception:
            pass

        direct_candidates = [
            val
            for name, val in entries
            if name in ("__Secure-next-auth.session-token", "_Secure-next-auth.session-token") and val
        ]
        if direct_candidates:
            return max(direct_candidates, key=len)

        chunk_map: dict[int, str] = {}
        for name, value in entries:
            if not (
                name.startswith("__Secure-next-auth.session-token.")
                or name.startswith("_Secure-next-auth.session-token.")
            ):
                continue
            if not value:
                continue
            try:
                idx = int(name.rsplit(".", 1)[-1])
            except Exception:
                continue
            prev = chunk_map.get(idx, "")
            if not prev or len(value) > len(prev):
                chunk_map[idx] = value

        if chunk_map:
            return "".join(chunk_map[i] for i in sorted(chunk_map.keys()))
        return ""

    @staticmethod
    def _flatten_set_cookie_headers(response) -> str:
        """
        合并多条 Set-Cookie（包含分片 cookie）。
        """
        try:
            headers = getattr(response, "headers", None)
            if headers is None:
                return ""
            if hasattr(headers, "get_list"):
                values = headers.get_list("set-cookie")
                if values:
                    return " | ".join(str(v or "") for v in values if v is not None)
            if hasattr(headers, "get_all"):
                values = headers.get_all("set-cookie")
                if values:
                    return " | ".join(str(v or "") for v in values if v is not None)
            return str(headers.get("set-cookie") or "")
        except Exception:
            return ""

    @staticmethod
    def _extract_request_cookie_header(response) -> str:
        """
        从响应对象关联的请求头中提取 Cookie。
        对齐 F12 Network -> Request Headers -> Cookie 的观测路径。
        """
        try:
            request_obj = getattr(response, "request", None)
            if request_obj is None:
                return ""
            headers = getattr(request_obj, "headers", None)
            if headers is None:
                return ""

            if hasattr(headers, "get"):
                value = headers.get("cookie") or headers.get("Cookie")
                if value:
                    return str(value)

            try:
                for key, value in dict(headers).items():
                    if str(key or "").strip().lower() == "cookie" and value:
                        return str(value)
            except Exception:
                pass
        except Exception:
            pass
        return ""

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        return ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱，先给新账号整个收件箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            raw_email = str(self.email_info["email"] or "").strip()
            normalized_email = raw_email.lower()

            # 保留原始收件地址，注册链路统一使用规范化邮箱，规避 "Failed to register username"。
            self.inbox_email = raw_email
            self.email = normalized_email
            self.email_info["email"] = normalized_email

            if raw_email and raw_email != normalized_email:
                self._log(f"邮箱规范化: {raw_email} -> {normalized_email}")

            self._log(f"邮箱已就位，地址新鲜出炉: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _start_oauth(self) -> bool:
        """开始 OAuth 流程"""
        try:
            self._log("开始 OAuth 授权流程，去门口刷个脸...")
            self.oauth_start = self.oauth_manager.start_oauth()
            self._log(f"OAuth URL 已备好，通道已经打开: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as e:
            self._log(f"生成 OAuth URL 失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """初始化会话"""
        try:
            self.session = self.http_client.session
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID"""
        if not self.oauth_start:
            return None

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.session:
                    self.session = self.http_client.session

                response = self.session.get(
                    self.oauth_start.auth_url,
                    timeout=20
                )
                did = self.session.cookies.get("oai-did")

                if not did:
                    # 对齐 ABCard：部分环境 cookie 不落盘，尝试从 HTML 文本提取
                    try:
                        m = re.search(r'oai-did["\s:=]+([a-f0-9-]{36})', str(response.text or ""), re.IGNORECASE)
                        if m:
                            did = str(m.group(1) or "").strip()
                            if did:
                                try:
                                    self.session.cookies.set("oai-did", did, domain=".chatgpt.com", path="/")
                                except Exception:
                                    pass
                    except Exception:
                        pass

                if did:
                    self._log(f"Device ID: {did}")
                    return did

                self._log(
                    f"获取 Device ID 失败: 未返回 oai-did Cookie (HTTP {response.status_code}, 第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )
            except Exception as e:
                self._log(
                    f"获取 Device ID 失败: {e} (第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )

            if attempt < max_attempts:
                time.sleep(attempt)
                self.http_client.close()
                self.session = self.http_client.session

        # 对齐 ABCard：无法从响应拿到 did 时，优先复用上次成功 did，再使用 UUID 兜底。
        fallback_did = str(self.device_id or "").strip() or str(uuid.uuid4())
        try:
            if self.session:
                self.session.cookies.set("oai-did", fallback_did, domain=".chatgpt.com", path="/")
        except Exception:
            pass
        self._log(f"未获取到 oai-did，使用兜底 Device ID: {fallback_did}", "warning")
        return fallback_did

    def _check_sentinel(self, did: str) -> Optional[str]:
        """检查 Sentinel 拦截"""
        try:
            sen_token = self.http_client.check_sentinel(did)
            if sen_token:
                self._log(f"Sentinel token 获取成功")
                return sen_token
            self._log("Sentinel 检查失败: 未获取到 token", "warning")
            return None

        except Exception as e:
            self._log(f"Sentinel 检查异常: {e}", "warning")
            return None

    def _submit_auth_start(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        screen_hint: str,
        referer: str,
        log_label: str,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """
        提交授权入口表单

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断
        """
        max_attempts = 3
        current_did = str(did or "").strip()
        current_sen_token = str(sen_token or "").strip() if sen_token else None
        for attempt in range(1, max_attempts + 1):
            try:
                request_body = json.dumps({
                    "username": {
                        "value": self.email,
                        "kind": "email",
                    },
                    "screen_hint": screen_hint,
                })

                headers = {
                    "referer": referer,
                    "accept": "application/json",
                    "content-type": "application/json",
                }

                if current_sen_token:
                    sentinel = json.dumps({
                        "p": "",
                        "t": "",
                        "c": current_sen_token,
                        "id": current_did,
                        "flow": "authorize_continue",
                    })
                    headers["openai-sentinel-token"] = sentinel

                response = self.session.post(
                    OPENAI_API_ENDPOINTS["signup"],
                    headers=headers,
                    data=request_body,
                )

                self._log(f"{log_label}状态: {response.status_code}")

                if response.status_code == 429 and attempt < max_attempts:
                    wait_seconds = min(18, 5 * attempt)
                    self._log(
                        f"{log_label}命中限流 429（第 {attempt}/{max_attempts} 次），{wait_seconds}s 后自动重试...",
                        "warning",
                    )
                    time.sleep(wait_seconds)
                    continue

                # 部分网络/会话边界情况下会返回 409，做自愈重试而非直接失败。
                if response.status_code == 409 and attempt < max_attempts:
                    wait_seconds = min(10, 2 * attempt)
                    self._log(
                        f"{log_label}命中 409（第 {attempt}/{max_attempts} 次），"
                        f"会话上下文可能冲突，{wait_seconds}s 后自动重试...",
                        "warning",
                    )
                    # 尝试刷新 sentinel，避免 token 过期导致冲突。
                    try:
                        refreshed = self._check_sentinel(current_did)
                        if refreshed:
                            current_sen_token = refreshed
                    except Exception:
                        pass
                    # 预热一次授权页，帮助服务端重建登录上下文。
                    try:
                        if self.oauth_start and getattr(self.oauth_start, "auth_url", None):
                            self.session.get(str(self.oauth_start.auth_url), timeout=12)
                    except Exception:
                        pass
                    time.sleep(wait_seconds)
                    continue

                if response.status_code != 200:
                    return SignupFormResult(
                        success=False,
                        error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                    )

                # 解析响应判断账号状态
                try:
                    response_data = response.json()
                    page_type = response_data.get("page", {}).get("type", "")
                    self._log(f"响应页面类型: {page_type}")

                    is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]

                    if is_existing:
                        self._otp_sent_at = time.time()
                        if record_existing_account:
                            self._log(f"检测到已注册账号，将自动切换到登录流程")
                            self._is_existing_account = True
                        else:
                            self._log("登录流程已触发，等待系统自动发送的验证码")

                    return SignupFormResult(
                        success=True,
                        page_type=page_type,
                        is_existing_account=is_existing,
                        response_data=response_data
                    )

                except Exception as parse_error:
                    self._log(f"解析响应失败: {parse_error}", "warning")
                    # 无法解析，默认成功
                    return SignupFormResult(success=True)

            except Exception as e:
                if attempt < max_attempts:
                    self._log(
                        f"{log_label}异常（第 {attempt}/{max_attempts} 次）: {e}，准备重试...",
                        "warning",
                    )
                    time.sleep(2 * attempt)
                    continue
                self._log(f"{log_label}失败: {e}", "error")
                return SignupFormResult(success=False, error_message=str(e))

        return SignupFormResult(success=False, error_message=f"{log_label}失败: 超过最大重试次数")

    def _submit_signup_form(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """提交注册入口表单。"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            log_label="提交注册表单",
            record_existing_account=record_existing_account,
        )

    def _submit_login_start(self, did: str, sen_token: Optional[str]) -> SignupFormResult:
        """提交登录入口表单。"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            log_label="提交登录入口",
            record_existing_account=False,
        )

    def _submit_login_password(self) -> SignupFormResult:
        """提交登录密码，进入邮箱验证码页面。"""
        max_attempts = 3
        password_text = str(self.password or "").strip()
        if not password_text and self.email:
            try:
                with get_db() as db:
                    account = crud.get_account_by_email(db, self.email)
                    db_password = str(getattr(account, "password", "") or "").strip() if account else ""
                    if db_password:
                        self.password = db_password
                        password_text = db_password
                        self._log("登录阶段未发现内存密码，已从账号库回填密码")
            except Exception as e:
                self._log(f"登录阶段尝试回填密码失败: {e}", "warning")

        if not password_text:
            return SignupFormResult(
                success=False,
                error_message="登录密码为空：该邮箱可能是已存在账号但当前任务未持有密码",
            )

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.post(
                    OPENAI_API_ENDPOINTS["password_verify"],
                    headers={
                        "referer": "https://auth.openai.com/log-in/password",
                        "accept": "application/json",
                        "content-type": "application/json",
                    },
                    data=json.dumps({"password": self.password}),
                )

                self._log(f"提交登录密码状态: {response.status_code}")

                if response.status_code == 429 and attempt < max_attempts:
                    wait_seconds = min(18, 5 * attempt)
                    self._log(
                        f"提交登录密码命中限流 429（第 {attempt}/{max_attempts} 次），{wait_seconds}s 后自动重试...",
                        "warning",
                    )
                    time.sleep(wait_seconds)
                    continue

                if response.status_code == 401 and attempt < max_attempts:
                    body = str(response.text or "")
                    if "invalid_username_or_password" in body:
                        wait_seconds = min(12, 3 * attempt)
                        self._log(
                            f"提交登录密码命中 401（第 {attempt}/{max_attempts} 次），"
                            f"疑似密码尚未生效或历史账号密码不一致，{wait_seconds}s 后自动重试...",
                            "warning",
                        )
                        time.sleep(wait_seconds)
                        continue

                if response.status_code != 200:
                    return SignupFormResult(
                        success=False,
                        error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                    )

                response_data = response.json()
                page_type = response_data.get("page", {}).get("type", "")
                self._log(f"登录密码响应页面类型: {page_type}")

                is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
                if is_existing:
                    self._otp_sent_at = time.time()
                    self._log("登录密码校验通过，等待系统自动发送的验证码")

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data,
                )

            except Exception as e:
                if attempt < max_attempts:
                    self._log(
                        f"提交登录密码异常（第 {attempt}/{max_attempts} 次）: {e}，准备重试...",
                        "warning",
                    )
                    time.sleep(2 * attempt)
                    continue
                self._log(f"提交登录密码失败: {e}", "error")
                return SignupFormResult(success=False, error_message=str(e))

        return SignupFormResult(success=False, error_message="提交登录密码失败: 超过最大重试次数")

    def _reset_auth_flow(self) -> None:
        """重置会话，准备重新发起 OAuth 流程。"""
        self.http_client.close()
        self.session = None
        self.oauth_start = None
        self.session_token = None
        self._otp_sent_at = None

    def _prepare_authorize_flow(self, label: str) -> Tuple[Optional[str], Optional[str]]:
        """初始化当前阶段的授权流程，返回 device id 和 sentinel token。"""
        self._log(f"{label}: 先把会话热热身...")
        if not self._init_session():
            return None, None

        self._log(f"{label}: OAuth 流程准备开跑，系好鞋带...")
        if not self._start_oauth():
            return None, None

        self._log(f"{label}: 领取 Device ID 通行证...")
        did = str(self._get_device_id() or "").strip()
        if not did:
            return None, None

        self.device_id = did

        self._log(f"{label}: 解一道 Sentinel POW 小题，答对才给进...")
        sen_token = self._check_sentinel(did)
        if not sen_token:
            return did, None

        self._log(f"{label}: Sentinel 点头放行，继续前进")
        return did, sen_token

    @staticmethod
    def _extract_session_token_from_cookie_text(cookie_text: str) -> str:
        """从 Cookie 文本中提取 next-auth session token（兼容分片）。"""
        text = str(cookie_text or "")
        if not text:
            return ""

        direct = re.search(r"(?:^|[;,]\s*)(?:__|_)Secure-next-auth\.session-token=([^;,]*)", text)
        if direct:
            direct_val = str(direct.group(1) or "").strip().strip('"').strip("'")
            if direct_val:
                return direct_val

        parts = re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=([^;,]*)", text)
        if not parts:
            return ""

        chunk_map = {}
        for idx, value in parts:
            try:
                clean_value = str(value or "").strip().strip('"').strip("'")
                if clean_value:
                    chunk_map[int(idx)] = clean_value
            except Exception:
                continue
        if not chunk_map:
            return ""
        return "".join(chunk_map[i] for i in sorted(chunk_map.keys()))

    def _warmup_chatgpt_session(self) -> None:
        """
        仅预热 chatgpt 首页，避免提前消费一次性 continue_url。
        """
        try:
            self.session.get(
                "https://chatgpt.com/",
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": "https://auth.openai.com/",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                timeout=20,
            )
        except Exception as e:
            self._log(f"chatgpt 首页预热异常: {e}", "warning")

    def _capture_auth_session_tokens(self, result: RegistrationResult, access_hint: Optional[str] = None) -> bool:
        """
        直接通过 /api/auth/session 捕获 session_token + access_token。
        这是 ABCard Phase 1 的关键路径。
        """
        access_token = str(access_hint or "").strip()
        set_cookie_text = ""
        request_cookie_text = ""
        try:
            headers = {
                "accept": "application/json",
                "referer": "https://chatgpt.com/",
                "origin": "https://chatgpt.com",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "cache-control": "no-cache",
                "pragma": "no-cache",
            }
            if access_token:
                headers["authorization"] = f"Bearer {access_token}"
            response = self.session.get(
                "https://chatgpt.com/api/auth/session",
                headers=headers,
                timeout=20,
            )
            set_cookie_text = self._flatten_set_cookie_headers(response)
            request_cookie_text = self._extract_request_cookie_header(response)
            if response.status_code == 200:
                try:
                    data = response.json() or {}
                    access_from_json = str(data.get("accessToken") or "").strip()
                    if access_from_json:
                        access_token = access_from_json
                except Exception:
                    pass
            else:
                self._log(f"/api/auth/session 返回异常状态: {response.status_code}", "warning")
        except Exception as e:
            self._log(f"获取 auth/session 失败: {e}", "warning")

        # 1) 直接从 cookie jar 拿
        session_token = self._extract_session_token_from_cookie_jar(self.session.cookies)

        # 2) 从完整 cookies 文本兜底（含分片）
        if not session_token:
            session_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())

        # 3) 从 set-cookie 兜底（含分片）
        if not session_token and set_cookie_text:
            session_token = self._extract_session_token_from_cookie_text(set_cookie_text)

        # 4) 从请求 Cookie 头兜底（对齐 F12 Network 观测）
        if not session_token and request_cookie_text:
            session_token = self._extract_session_token_from_cookie_text(request_cookie_text)

        # 兜底：已有 access_token 但无 session_token 时，带 Bearer 再请求一次 auth/session
        if (not session_token) and access_token:
            try:
                retry_response = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers={
                        "accept": "application/json",
                        "referer": "https://chatgpt.com/",
                        "origin": "https://chatgpt.com",
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                        "authorization": f"Bearer {access_token}",
                        "cache-control": "no-cache",
                        "pragma": "no-cache",
                    },
                    timeout=20,
                )
                retry_set_cookie = self._flatten_set_cookie_headers(retry_response)
                retry_request_cookie = self._extract_request_cookie_header(retry_response)
                if not session_token:
                    session_token = self._extract_session_token_from_cookie_jar(self.session.cookies)
                if not session_token:
                    session_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())
                if not session_token and retry_set_cookie:
                    session_token = self._extract_session_token_from_cookie_text(retry_set_cookie)
                if not session_token and retry_request_cookie:
                    session_token = self._extract_session_token_from_cookie_text(retry_request_cookie)
            except Exception as e:
                self._log(f"Bearer 兜底换 session_token 失败: {e}", "warning")

        if not session_token:
            cookies_text = self._dump_session_cookies()
            raw_direct_match = re.search(
                r"(?:^|[;,]\s*)(?:__|_)Secure-next-auth\.session-token=([^;,]*)",
                cookies_text,
            )
            raw_direct_len = len(str(raw_direct_match.group(1) or "").strip()) if raw_direct_match else 0
            chunk_count = len(re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=", cookies_text))
            req_cookie_len = len(str(request_cookie_text or "").strip())
            self._log(
                f"auth/session 仍未命中 session_token（raw_direct_len={raw_direct_len}, chunks={chunk_count}, req_cookie_len={req_cookie_len}）",
                "warning",
            )

        # 设备 ID 同步
        did = ""
        try:
            did = str(self.session.cookies.get("oai-did") or "").strip()
        except Exception:
            did = ""
        if did:
            self.device_id = did
            result.device_id = did

        if session_token:
            self.session_token = session_token
            result.session_token = session_token
        if access_token:
            result.access_token = access_token

        self._log(
            "Auth Session 捕获结果: session_token="
            + ("有" if bool(result.session_token) else "无")
            + ", access_token="
            + ("有" if bool(result.access_token) else "无")
        )
        return bool(result.session_token and result.access_token)

    def _bootstrap_chatgpt_signin_for_session(self, result: RegistrationResult) -> bool:
        """
        对齐 ABCard 的补会话路径：
        csrf -> signin/openai -> 跟随跳转 -> auth/session，目标是拿到 session_token。
        """
        self._log("Session Token 还没就位，尝试 ABCard 同款会话桥接...")
        self._warmup_chatgpt_session()
        csrf_token = ""
        auth_url = ""
        try:
            csrf_resp = self.session.get(
                "https://chatgpt.com/api/auth/csrf",
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/auth/login",
                    "origin": "https://chatgpt.com",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                timeout=20,
            )
            if csrf_resp.status_code == 200:
                csrf_token = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()
            else:
                self._log(f"csrf 获取失败: HTTP {csrf_resp.status_code}", "warning")
        except Exception as e:
            self._log(f"csrf 获取异常: {e}", "warning")

        if not csrf_token:
            self._log("csrf token 为空，跳过会话桥接", "warning")
            return False

        try:
            signin_resp = self.session.post(
                "https://chatgpt.com/api/auth/signin/openai",
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": "https://chatgpt.com",
                    "referer": "https://chatgpt.com/auth/login",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                data={
                    "csrfToken": csrf_token,
                    "callbackUrl": "https://chatgpt.com/",
                    "json": "true",
                },
                timeout=20,
            )
            if signin_resp.status_code == 200:
                auth_url = str((signin_resp.json() or {}).get("url") or "").strip()
            else:
                self._log(f"signin/openai 失败: HTTP {signin_resp.status_code}", "warning")
        except Exception as e:
            self._log(f"signin/openai 异常: {e}", "warning")

        if not auth_url:
            self._log("signin/openai 未返回 auth_url，跳过会话桥接", "warning")
            return False

        callback_url = ""
        final_url = auth_url
        try:
            callback_url, final_url = self._follow_chatgpt_auth_redirects(auth_url)
        except Exception as e:
            self._log(f"会话桥接重定向跟踪异常: {e}", "warning")
            callback_url = ""
            final_url = auth_url

        # 若已拿到 callback，补打一跳确保 next-auth callback 被完整执行。
        if callback_url and "error=" not in callback_url:
            try:
                self.session.get(
                    callback_url,
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": "https://chatgpt.com/auth/login",
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                    },
                    allow_redirects=True,
                    timeout=25,
                )
            except Exception as e:
                self._log(f"会话桥接 callback 补跳异常: {e}", "warning")
        elif callback_url and "error=" in callback_url:
            self._log(f"会话桥接回调返回错误参数: {callback_url[:140]}...", "warning")
        else:
            self._log(f"会话桥接未命中 callback，final_url={final_url[:120]}...", "warning")
            # 命中 auth.openai 登录页时，尝试自动登录补会话（对齐 ABCard 的登录态建立思路）。
            if "auth.openai.com/log-in" in str(final_url or "").lower():
                self._log("会话桥接进入登录页，尝试自动登录后继续抓取 session_token...")
                if self._bridge_login_for_session_token(result):
                    return True

        self._warmup_chatgpt_session()
        cookie_text = self._dump_session_cookies()
        direct_token = self._extract_session_token_from_cookie_text(cookie_text)
        has_direct = bool(direct_token)
        chunk_count = len(re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=", cookie_text))
        if direct_token and not result.session_token:
            self.session_token = direct_token
            result.session_token = direct_token
            self._log(f"会话桥接已缓存 session_token（len={len(direct_token)}）")
        self._log(
            f"会话桥接后 cookie 概览: direct={'有' if has_direct else '无'}, chunks={chunk_count}"
        )
        return self._capture_auth_session_tokens(result, access_hint=result.access_token)

    def _bridge_login_for_session_token(self, result: RegistrationResult) -> bool:
        """
        当 chatgpt signin/openai 跳回 auth.openai 登录页时，自动补一次登录流程：
        login -> password -> email otp -> workspace -> auth/session。
        """
        try:
            if not self.email or not self.password:
                self._log("会话桥接自动登录缺少邮箱或密码，无法继续", "warning")
                return False

            did = ""
            try:
                did = str(self.session.cookies.get("oai-did") or "").strip()
            except Exception:
                did = ""
            if not did:
                did = str(uuid.uuid4())
                try:
                    self.session.cookies.set("oai-did", did, domain=".chatgpt.com", path="/")
                except Exception:
                    pass
            self.device_id = did
            result.device_id = result.device_id or did

            sen_token = self._check_sentinel(did)
            login_start_result = self._submit_login_start(did, sen_token)
            if not login_start_result.success:
                self._log(
                    f"会话桥接自动登录入口失败: {login_start_result.error_message}",
                    "warning",
                )
                return False
            page_type = str(login_start_result.page_type or "").strip()
            if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
                self._log("会话桥接自动登录已直达邮箱验证码页，跳过密码提交")
            elif page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
                password_result = self._submit_login_password()
                if not password_result.success:
                    self._log(
                        f"会话桥接自动登录提交密码失败: {password_result.error_message}",
                        "warning",
                    )
                    return False
                if not password_result.is_existing_account:
                    self._log(
                        f"会话桥接自动登录未进入邮箱验证码页: {password_result.page_type or 'unknown'}",
                        "warning",
                    )
                    return False
            else:
                self._log(
                    f"会话桥接自动登录入口返回未知页面: {page_type or 'unknown'}",
                    "warning",
                )
                return False

            if not self._verify_email_otp_with_retry(stage_label="会话桥接登录验证码", max_attempts=3):
                self._log("会话桥接自动登录验证码校验失败", "warning")
                return False

            # OTP 成功后先直接抓一次 auth/session，避免无谓依赖 workspace 流程。
            self._warmup_chatgpt_session()
            if self._capture_auth_session_tokens(result, access_hint=result.access_token):
                self._log("会话桥接自动登录在 OTP 后已命中 session_token")
                return True

            workspace_id = self._get_workspace_id()
            if not workspace_id:
                workspace_id = str(result.workspace_id or "").strip()
                if workspace_id:
                    self._log(f"会话桥接自动登录复用已知 workspace_id: {workspace_id}")
            if not workspace_id:
                self._log("会话桥接自动登录未获取到 workspace_id", "warning")
                return False
            result.workspace_id = workspace_id

            continue_url = self._select_workspace(workspace_id)
            if not continue_url:
                cached_continue = str(self._create_account_continue_url or "").strip()
                if cached_continue:
                    continue_url = cached_continue
                    self._log("会话桥接自动登录未获取到 continue_url，改用 create_account 缓存 continue_url", "warning")
                else:
                    self._log("会话桥接自动登录未获取到 continue_url", "warning")
                    return False

            callback_url, final_url = self._follow_redirects(continue_url)
            self._log(
                f"会话桥接自动登录重定向完成: callback={'有' if callback_url else '无'}, final={str(final_url or '')[:100]}..."
            )

            self._warmup_chatgpt_session()
            return self._capture_auth_session_tokens(result, access_hint=result.access_token)
        except Exception as e:
            self._log(f"会话桥接自动登录异常: {e}", "warning")
            return False

    def _follow_chatgpt_auth_redirects(self, start_url: str) -> Tuple[str, str]:
        """
        对齐 ABCard 的 next-auth 重定向跟踪：
        - 手动跟踪 30x
        - 识别 /api/auth/callback/openai
        Returns:
            (callback_url, final_url)
        """
        import urllib.parse

        current_url = str(start_url or "").strip()
        callback_url = ""
        bridged_header_token = ""
        if not current_url:
            return "", ""

        max_redirects = 12
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        for i in range(max_redirects):
            self._log(f"会话桥接重定向 {i+1}/{max_redirects}: {current_url[:120]}...")
            if "/api/auth/callback/openai" in current_url and not callback_url:
                callback_url = current_url

            resp = self.session.get(
                current_url,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": "https://chatgpt.com/",
                    "user-agent": ua,
                },
                timeout=25,
                allow_redirects=False,
            )

            # 直接从每一跳响应头 Set-Cookie 抓 session_token（对齐 F12 Network 视角）
            set_cookie_text = self._flatten_set_cookie_headers(resp)
            token_from_header = self._extract_session_token_from_cookie_text(set_cookie_text)
            if token_from_header:
                bridged_header_token = token_from_header
                # 同时写入两种命名兼容，避免库在不同平台下键名差异。
                for name in ("__Secure-next-auth.session-token", "_Secure-next-auth.session-token"):
                    for domain in (".chatgpt.com", "chatgpt.com"):
                        try:
                            self.session.cookies.set(name, token_from_header, domain=domain, path="/")
                        except Exception:
                            continue
                self._log(
                    f"会话桥接命中 Set-Cookie session_token（len={len(token_from_header)}）"
                )

            if resp.status_code not in (301, 302, 303, 307, 308):
                break

            location = str(resp.headers.get("Location") or "").strip()
            if not location:
                break
            current_url = urllib.parse.urljoin(current_url, location)

        if callback_url and not str(current_url or "").startswith("https://chatgpt.com/"):
            try:
                self.session.get(
                    "https://chatgpt.com/",
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": current_url,
                        "user-agent": ua,
                    },
                    timeout=20,
                )
            except Exception:
                pass

        self._log(
            f"会话桥接重定向结束: callback={'有' if callback_url else '无'}, "
            f"set_cookie_token={'有' if bool(bridged_header_token) else '无'}, final={current_url[:120]}..."
        )
        return callback_url, current_url

    def _complete_token_exchange(self, result: RegistrationResult, require_login_otp: bool = True) -> bool:
        """在登录态已建立后，补齐 session/access，并尽量获取 OAuth token。"""
        if require_login_otp:
            self._log("等待登录验证码到场，最后这位嘉宾还在路上...")
            self._log("核对登录验证码，验明正身一下...")
            if not self._verify_email_otp_with_retry(stage_label="登录验证码", max_attempts=3):
                result.error_message = "验证码校验失败"
                return False
        else:
            self._log("ABCard 入口链路：跳过二次登录验证码，直接进入 workspace + redirect + auth/session 抓取")

        self._log("摸一下 Workspace ID，看看该坐哪桌...")
        workspace_id = self._get_workspace_id()
        continue_url = ""
        if workspace_id:
            result.workspace_id = workspace_id

            self._log("选择 Workspace，安排个靠谱座位...")
            continue_url = self._select_workspace(workspace_id)
            if not continue_url:
                cached_continue = str(self._create_account_continue_url or "").strip()
                if cached_continue:
                    continue_url = cached_continue
                    self._log("workspace/select 未返回 continue_url，改用 create_account 缓存 continue_url", "warning")
                else:
                    result.error_message = "选择 Workspace 失败"
                    return False
        else:
            cached_continue = str(self._create_account_continue_url or "").strip()
            if cached_continue:
                continue_url = cached_continue
                self._log("未获取到 Workspace ID，改用 create_account 缓存 continue_url 继续链路", "warning")
            else:
                result.error_message = "获取 Workspace ID 失败"
                return False

        self._log("顺着重定向面包屑往前走，别跟丢了...")
        callback_url, final_url = self._follow_redirects(continue_url)
        self._log(
            f"重定向链完成，callback={'有' if callback_url else '无'}，final={final_url[:100]}..."
        )
        self._log("重定向链结束，直接请求 /api/auth/session 抓取 session/access...")
        captured = self._capture_auth_session_tokens(result, access_hint=result.access_token)
        if not captured:
            self._log("直抓未命中，补一次 chatgpt 预热后再抓取...", "warning")
            self._warmup_chatgpt_session()
            captured = self._capture_auth_session_tokens(result, access_hint=result.access_token)
        final_url_lower = str(final_url or "").lower()
        add_phone_gate = ("auth.openai.com/add-phone" in final_url_lower)

        # ABCard 入口常见失败点：被 add-phone 风控页截断，导致拿不到 callback/session。
        if add_phone_gate and (not callback_url) and (not captured):
            self._log("检测到 auth.openai.com/add-phone 风控页，当前链路未完成 OAuth 回调", "warning")
            if (not require_login_otp) and (not self._is_existing_account):
                self._log("ABCard 入口命中 add-phone，回退原生重登链路再试一次...", "warning")
                login_ready, login_error = self._restart_login_flow()
                if not login_ready:
                    result.error_message = f"ABCard 回退原生链路失败: {login_error}"
                    return False
                return self._complete_token_exchange(result, require_login_otp=True)
            result.error_message = "命中 add-phone 风控页，未获取到 session_token"
            return False

        callback_has_error = bool(
            callback_url and ("error=" in callback_url) and ("code=" not in callback_url)
        )
        if callback_url:
            if callback_has_error:
                self._log(f"回调返回错误参数，跳过 OAuth 回调: {callback_url[:140]}...", "warning")
                if not captured:
                    result.error_message = "OAuth 回调返回 access_denied，且未获取到 auth/session"
                    return False
            else:
                self._log("处理 OAuth 回调，准备把 token 请出来...")
                token_info = self._handle_oauth_callback(callback_url)
                if token_info:
                    result.account_id = token_info.get("account_id", "")
                    result.access_token = token_info.get("access_token", "") or result.access_token
                    result.refresh_token = token_info.get("refresh_token", "")
                    result.id_token = token_info.get("id_token", "")
                elif captured:
                    self._log("OAuth 回调失败，但 session/access 已拿到，继续后续流程", "warning")
                else:
                    result.error_message = "处理 OAuth 回调失败"
                    return False
        else:
            if captured:
                self._log("未拿到 callback_url，但 session/access 已拿到，继续后续流程", "warning")
            else:
                result.error_message = "跟随重定向链失败"
                return False

        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"
        result.device_id = result.device_id or str(self.device_id or "")

        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie
            self._log("Session Token 也捞到了，今天这网没白连")

        if not result.access_token or not result.session_token:
            # 再捞一次，避免某些链路里 session 建立稍慢
            self._capture_auth_session_tokens(result, access_hint=result.access_token)
        if not result.session_token:
            # 对齐 ABCard：尝试走 csrf + signin/openai 的会话桥接。
            self._bootstrap_chatgpt_signin_for_session(result)
        if not result.session_token:
            result.session_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())
        if not result.device_id:
            result.device_id = str(self.device_id or self.session.cookies.get("oai-did") or "")

        if not result.access_token:
            result.error_message = "未获取到 access_token"
            return False
        if not result.session_token:
            native_register_flow = (self.registration_entry_flow == "native") and (not self._is_existing_account)
            if native_register_flow:
                # 对齐 K:\1\2 备份：原生注册流程里 session_token 不做阻断。
                self._log(
                    "当前链路未拿到 session_token，先保存账号并标记待补会话（可在账号详情/支付页一键补全）",
                    "warning",
                )
            else:
                # 非原生注册入口仍保持强制，避免后续流程不可用。
                if not self._ensure_session_token_strict(result, max_rounds=2):
                    result.error_message = "未获取到 session_token（强制要求）"
                    self._log(
                        "强制模式未拿到 session_token，本次注册判定失败，请检查网络/代理与登录回调链路",
                        "error",
                    )
                    return False

        return True

    def _complete_token_exchange_native_backup(self, result: RegistrationResult) -> bool:
        """
        原生入口对齐备份版收尾链路：
        登录验证码 -> Workspace -> redirect -> OAuth callback -> token 入袋。
        """
        def _is_registration_gate_url(url: str) -> bool:
            u = str(url or "").strip().lower()
            if not u:
                return False
            return ("auth.openai.com/about-you" in u) or ("auth.openai.com/add-phone" in u)

        self._log("等待登录验证码到场，最后这位嘉宾还在路上...")
        self._log("核对登录验证码，验明正身一下...")
        login_otp_tried_codes: set[str] = set()
        login_otp_ok = self._verify_email_otp_with_retry(
            stage_label="登录验证码",
            max_attempts=1,
            fetch_timeout=120,
            attempted_codes=login_otp_tried_codes,
        )
        if not login_otp_ok:
            self._log("登录验证码首轮未命中，尝试在当前会话原地重发 OTP 后再校验...", "warning")
            resent = self._send_verification_code(referer="https://auth.openai.com/email-verification")
            if resent:
                login_otp_ok = self._verify_email_otp_with_retry(
                    stage_label="登录验证码(原地重发)",
                    max_attempts=2,
                    fetch_timeout=120,
                    attempted_codes=login_otp_tried_codes,
                )

        if not login_otp_ok:
            self._log("登录验证码仍未命中，尝试重触发登录 OTP 后再校验...", "warning")
            if not self._retrigger_login_otp():
                self._log("重触发登录 OTP 失败，尝试完整重登链路后再校验一次...", "warning")
                login_ready, login_error = self._restart_login_flow()
                if not login_ready:
                    result.error_message = f"登录验证码重触发失败，且完整重登失败: {login_error}"
                    return False
            login_otp_ok = self._verify_email_otp_with_retry(
                stage_label="登录验证码(重发)",
                max_attempts=3,
                fetch_timeout=120,
                attempted_codes=login_otp_tried_codes,
            )
            if not login_otp_ok:
                result.error_message = "验证码校验失败"
                return False

        self._log("摸一下 Workspace ID，看看该坐哪桌...")
        workspace_id = str(self._last_validate_otp_workspace_id or "").strip()
        if workspace_id:
            self._log(f"使用 OTP 返回的 Workspace ID: {workspace_id}")
        if not workspace_id:
            workspace_id = str(self._get_workspace_id() or "").strip()
        if workspace_id:
            result.workspace_id = workspace_id

        continue_url = ""
        otp_continue = str(self._last_validate_otp_continue_url or "").strip()
        if otp_continue and _is_registration_gate_url(otp_continue):
            self._log("OTP 返回 continue_url 指向注册门页（about-you/add-phone），本轮收尾忽略该地址", "warning")
            otp_continue = ""

        cached_continue = str(self._create_account_continue_url or "").strip()
        if cached_continue and _is_registration_gate_url(cached_continue):
            self._log("create_account 缓存 continue_url 指向注册门页（about-you/add-phone），本轮收尾忽略该地址", "warning")
            cached_continue = ""

        if workspace_id:
            self._log("选择 Workspace，安排个靠谱座位...")
            continue_url = str(self._select_workspace(workspace_id) or "").strip()
            if not continue_url:
                self._log("workspace/select 未返回 continue_url，尝试 OAuth authorize 兜底", "warning")

        if not continue_url:
            oauth_start_url = str(
                (
                    getattr(self.oauth_start, "auth_url", "")
                    or getattr(self.oauth_start, "url", "")
                    if self.oauth_start
                    else ""
                )
                or ""
            ).strip()
            if oauth_start_url:
                continue_url = oauth_start_url
                self._log("使用 OAuth authorize URL 作为兜底 continue_url", "warning")

        if not continue_url and otp_continue:
            continue_url = otp_continue
            self._log("使用 OTP 返回 continue_url 继续授权链路", "warning")

        if not continue_url and cached_continue:
            continue_url = cached_continue
            self._log("使用 create_account 缓存 continue_url 作为兜底", "warning")

        if not continue_url:
            result.error_message = "获取 continue_url 失败"
            return False

        self._log("顺着重定向面包屑往前走，别跟丢了...")
        callback_url, _final_url = self._follow_redirects(continue_url)
        if not callback_url:
            self._log("未命中 OAuth 回调，尝试 auth/session 兜底抓取 token...", "warning")
            self._capture_auth_session_tokens(result, access_hint=result.access_token)
            if not result.account_id:
                result.account_id = str(self._create_account_account_id or "").strip()
            if not result.workspace_id:
                result.workspace_id = str(workspace_id or self._create_account_workspace_id or "").strip()
            if not result.refresh_token:
                result.refresh_token = str(self._create_account_refresh_token or "").strip()
            if result.access_token:
                result.password = self.password or ""
                result.source = "login" if self._is_existing_account else "register"
                result.device_id = result.device_id or str(self.device_id or "")
                self._log("未命中 callback，已通过 auth/session 兜底拿到 Access Token，继续完成注册", "warning")
                return True

            # 对新注册账号放宽：账号已创建成功时允许“注册成功、token 待补”
            if (not self._is_existing_account) and self._create_account_account_id:
                result.account_id = result.account_id or str(self._create_account_account_id or "").strip()
                result.workspace_id = result.workspace_id or str(workspace_id or self._create_account_workspace_id or "").strip()
                result.refresh_token = result.refresh_token or str(self._create_account_refresh_token or "").strip()
                result.password = self.password or ""
                result.source = "register"
                result.device_id = result.device_id or str(self.device_id or "")
                self._log("回调链路未命中且未抓到 Access Token，但账号已创建成功；按注册成功收尾（token 待后续补齐）", "warning")
                return True

            result.error_message = "跟随重定向链失败"
            return False

        self._log("处理 OAuth 回调，准备把 token 请出来...")
        token_info = self._handle_oauth_callback(callback_url)
        if not token_info:
            if (not self._is_existing_account) and self._create_account_account_id:
                result.account_id = result.account_id or str(self._create_account_account_id or "").strip()
                result.workspace_id = result.workspace_id or str(workspace_id or self._create_account_workspace_id or "").strip()
                result.refresh_token = result.refresh_token or str(self._create_account_refresh_token or "").strip()
                result.password = self.password or ""
                result.source = "register"
                result.device_id = result.device_id or str(self.device_id or "")
                self._log("OAuth 回调处理失败，但账号已创建成功；按注册成功收尾（token 待后续补齐）", "warning")
                return True
            result.error_message = "处理 OAuth 回调失败"
            return False

        result.account_id = token_info.get("account_id", "")
        result.access_token = token_info.get("access_token", "")
        result.refresh_token = token_info.get("refresh_token", "")
        result.id_token = token_info.get("id_token", "")
        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"
        result.device_id = result.device_id or str(self.device_id or "")

        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie
            self._log("Session Token 也捞到了，今天这网没白连")

        return True

    def _complete_token_exchange_outlook(self, result: RegistrationResult) -> bool:
        """
        Outlook 入口链路（迁移版）：
        对齐 codex-console-main-clean 的收尾流程，
        走「登录 OTP -> Workspace -> OAuth callback」主干，避免 ABCard/native 增强链路干扰。
        同时补齐“第二封验证码”重试链路，避免 Outlook 轮询卡死。
        """
        self._log("等待登录验证码到场，最后这位嘉宾还在路上...")
        self._log("核对登录验证码，验明正身一下...")
        login_otp_tried_codes: set[str] = set()
        login_otp_ok = self._verify_email_otp_with_retry(
            stage_label="登录验证码",
            max_attempts=1,
            fetch_timeout=90,
            attempted_codes=login_otp_tried_codes,
        )
        if not login_otp_ok:
            self._log("登录验证码首轮未命中，先尝试当前会话原地重发 OTP 后再校验...", "warning")
            resent = self._send_verification_code(referer="https://auth.openai.com/email-verification")
            if resent:
                login_otp_ok = self._verify_email_otp_with_retry(
                    stage_label="登录验证码(原地重发)",
                    max_attempts=2,
                    fetch_timeout=90,
                    attempted_codes=login_otp_tried_codes,
                )

        if not login_otp_ok:
            self._log("登录验证码仍未命中，尝试重触发登录 OTP 后再校验...", "warning")
            if not self._retrigger_login_otp():
                self._log("重触发登录 OTP 失败，尝试完整重登链路后再校验一次...", "warning")
                login_ready, login_error = self._restart_login_flow()
                if not login_ready:
                    result.error_message = f"登录验证码重触发失败，且完整重登失败: {login_error}"
                    return False

            login_otp_ok = self._verify_email_otp_with_retry(
                stage_label="登录验证码(重发)",
                max_attempts=3,
                fetch_timeout=120,
                attempted_codes=login_otp_tried_codes,
            )
        if not login_otp_ok:
            result.error_message = "验证码校验失败"
            return False

        self._log("摸一下 Workspace ID，看看该坐哪桌...")
        workspace_id = str(self._last_validate_otp_workspace_id or "").strip()
        if workspace_id:
            self._log(f"使用 OTP 返回的 Workspace ID: {workspace_id}")
        if not workspace_id:
            workspace_id = str(self._get_workspace_id() or "").strip()
        if not workspace_id:
            workspace_id = str(self._last_validate_otp_workspace_id or self._create_account_workspace_id or "").strip()
            if workspace_id:
                self._log(f"Workspace ID（缓存）: {workspace_id}", "warning")

        continue_url = ""
        if workspace_id:
            result.workspace_id = workspace_id
            self._log("选择 Workspace，安排个靠谱座位...")
            continue_url = str(self._select_workspace(workspace_id) or "").strip()
            if not continue_url:
                self._log("workspace/select 未返回 continue_url，尝试使用缓存 continue_url", "warning")
        else:
            self._log("未获取到 Workspace ID，尝试直接使用缓存 continue_url", "warning")

        if not continue_url:
            continue_url = str(self._last_validate_otp_continue_url or self._create_account_continue_url or "").strip()
            if continue_url:
                self._log("使用缓存 continue_url 继续授权链路", "warning")

        if not continue_url:
            result.error_message = "获取 Workspace ID 失败"
            return False

        self._log("顺着重定向面包屑往前走，别跟丢了...")
        callback_url, _final_url = self._follow_redirects(continue_url)
        if not callback_url:
            result.error_message = "跟随重定向链失败"
            return False

        self._log("处理 OAuth 回调，准备把 token 请出来...")
        token_info = self._handle_oauth_callback(callback_url)
        if not token_info:
            result.error_message = "处理 OAuth 回调失败"
            return False

        result.account_id = str(token_info.get("account_id") or result.account_id or "").strip()
        result.access_token = str(token_info.get("access_token") or result.access_token or "").strip()
        result.refresh_token = str(token_info.get("refresh_token") or result.refresh_token or "").strip()
        result.id_token = str(token_info.get("id_token") or result.id_token or "").strip()
        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"
        result.device_id = result.device_id or str(self.device_id or "")

        if not result.account_id:
            result.account_id = str(self._create_account_account_id or "").strip()
        if not result.workspace_id:
            result.workspace_id = str(self._create_account_workspace_id or "").strip()
        if not result.refresh_token:
            result.refresh_token = str(self._create_account_refresh_token or "").strip()

        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie
            self._log("Session Token 也捞到了，今天这网没白连")

        if not result.access_token:
            result.error_message = "未获取到 access_token"
            return False

        return True

    def _ensure_session_token_strict(self, result: RegistrationResult, max_rounds: int = 2) -> bool:
        """
        强制确保 session_token 可用。
        - 先走 auth/session 直抓
        - 再走 ABCard 同款会话桥接
        连续多轮失败则返回 False。
        """
        if result.session_token:
            return True

        rounds = max(int(max_rounds), 1)
        for idx in range(rounds):
            self._log(f"强制补会话 round {idx + 1}/{rounds}：尝试补抓 session_token ...")

            self._warmup_chatgpt_session()
            self._capture_auth_session_tokens(result, access_hint=result.access_token)
            if result.session_token:
                self._log("强制补会话成功：auth/session 已拿到 session_token")
                return True

            self._bootstrap_chatgpt_signin_for_session(result)
            if result.session_token:
                self._log("强制补会话成功：桥接链路已拿到 session_token")
                return True

            fallback_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())
            if fallback_token:
                result.session_token = fallback_token
                self.session_token = fallback_token
                self._log("强制补会话成功：cookie 文本兜底命中 session_token")
                return True

            self._log("强制补会话本轮未命中 session_token", "warning")

        return False

    def _capture_native_core_tokens(self, result: RegistrationResult) -> bool:
        """
        原生注册入口的轻量 token 抓取：
        - 不做二次登录
        - 不强依赖 session_token
        - 尽量补齐 account/workspace/access/refresh
        """
        try:
            client_id = str(getattr(self.oauth_manager, "client_id", "") or "").strip()
            if client_id:
                self._log(f"原生入口 token 抓取: Client ID: {client_id}")

            if (not result.account_id) and self._create_account_account_id:
                result.account_id = str(self._create_account_account_id or "").strip()
                self._log(f"原生入口 token 抓取: 复用 create_account Account ID: {result.account_id}")
            if (not result.refresh_token) and self._create_account_refresh_token:
                result.refresh_token = str(self._create_account_refresh_token or "").strip()
                self._log("原生入口 token 抓取: 复用 create_account Refresh Token")

            workspace_id = str(result.workspace_id or "").strip()
            if not workspace_id:
                workspace_id = str(self._create_account_workspace_id or "").strip()
            if not workspace_id:
                workspace_id = str(self._get_workspace_id() or "").strip()
            if workspace_id:
                result.workspace_id = workspace_id
                self._log(f"原生入口 token 抓取: Workspace ID: {workspace_id}")
            else:
                self._log("原生入口 token 抓取: 未获取到 Workspace ID", "warning")

            continue_url = ""
            if workspace_id:
                continue_url = str(self._select_workspace(workspace_id) or "").strip()
            if not continue_url:
                cached_continue = str(self._create_account_continue_url or "").strip()
                if cached_continue:
                    continue_url = cached_continue
                    self._log("原生入口 token 抓取: 使用 create_account 缓存 continue_url", "warning")

            callback_url: Optional[str] = None
            final_url = ""
            if continue_url:
                self._log("原生入口 token 抓取: 跟随重定向链获取 OAuth callback...")
                callback_url, final_url = self._follow_redirects(continue_url)
                self._log(
                    f"原生入口 token 抓取: 重定向完成，callback={'有' if callback_url else '无'}，final={str(final_url)[:100]}..."
                )
            else:
                self._log("原生入口 token 抓取: 未获得 continue_url，跳过 callback 交换", "warning")

            callback_has_error = bool(
                callback_url and ("error=" in callback_url) and ("code=" not in callback_url)
            )
            if callback_url and (not callback_has_error):
                token_info = self._handle_oauth_callback(callback_url)
                if token_info:
                    result.account_id = str(token_info.get("account_id") or result.account_id or "").strip()
                    result.access_token = str(token_info.get("access_token") or result.access_token or "").strip()
                    result.refresh_token = str(token_info.get("refresh_token") or result.refresh_token or "").strip()
                    result.id_token = str(token_info.get("id_token") or result.id_token or "").strip()
                    self._log(
                        "原生入口 token 抓取结果: "
                        f"account_id={'有' if bool(result.account_id) else '无'}, "
                        f"access={'有' if bool(result.access_token) else '无'}, "
                        f"refresh={'有' if bool(result.refresh_token) else '无'}"
                    )
                else:
                    self._log("原生入口 token 抓取: OAuth 回调处理失败", "warning")
            elif callback_has_error:
                self._log(f"原生入口 token 抓取: callback 含 error，跳过 token 交换: {callback_url[:140]}...", "warning")
            else:
                self._log("原生入口 token 抓取: 未命中 callback_url", "warning")

            # 不走重登，仅轻量探测 auth/session 里的 accessToken（不依赖 session_token）。
            if not result.access_token:
                self._capture_access_token_light(result)

            if (not result.account_id) and result.id_token:
                try:
                    account_info = self.oauth_manager.extract_account_info(result.id_token)
                    result.account_id = str(account_info.get("account_id") or "").strip()
                except Exception:
                    pass
            if (not result.account_id) and result.access_token:
                token_acc = self._extract_account_id_from_access_token(result.access_token)
                if token_acc:
                    result.account_id = token_acc
                    self._log(f"原生入口 token 抓取: 从 access_token 解析 Account ID: {token_acc}")
            if not result.workspace_id:
                try:
                    workspace_id_after = str(self._get_workspace_id() or "").strip()
                    if workspace_id_after:
                        result.workspace_id = workspace_id_after
                        self._log(f"原生入口 token 抓取: 二次获取 Workspace ID 成功: {workspace_id_after}")
                except Exception:
                    pass

            missing = []
            if not result.account_id:
                missing.append("Account ID")
            if not result.workspace_id:
                missing.append("Workspace ID")
            if not result.access_token:
                missing.append("Access Token")
            if not result.refresh_token:
                missing.append("Refresh Token")
            if missing:
                self._log(f"原生入口 token 抓取: 未获取字段 -> {', '.join(missing)}", "warning")

            return bool(result.access_token and result.refresh_token)
        except Exception as e:
            self._log(f"原生入口 token 抓取异常: {e}", "warning")
            return False

    def _capture_access_token_light(self, result: RegistrationResult) -> bool:
        """轻量从 /api/auth/session 抓 accessToken（不依赖 session_token）。"""
        try:
            response = self.session.get(
                "https://chatgpt.com/api/auth/session",
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/",
                },
                timeout=20,
            )
            if response.status_code != 200:
                self._log(f"原生入口轻量 auth/session 状态异常: {response.status_code}", "warning")
                return False
            data = response.json() or {}
            access_token = str(data.get("accessToken") or "").strip()
            if access_token:
                result.access_token = access_token
                self._log("原生入口轻量 auth/session 命中 Access Token")
                return True
            self._log("原生入口轻量 auth/session 未命中 Access Token", "warning")
            return False
        except Exception as e:
            self._log(f"原生入口轻量 auth/session 异常: {e}", "warning")
            return False

    def _extract_account_id_from_access_token(self, access_token: str) -> str:
        """从 access_token 的 JWT payload 尝试解析 chatgpt_account_id。"""
        try:
            raw = str(access_token or "").strip()
            if raw.count(".") < 2:
                return ""
            payload = raw.split(".")[1]
            import base64
            pad = "=" * ((4 - (len(payload) % 4)) % 4)
            decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
            claims = json.loads(decoded.decode("utf-8"))
            if not isinstance(claims, dict):
                return ""
            auth_claims = claims.get("https://api.openai.com/auth") or {}
            account_id = str(
                auth_claims.get("chatgpt_account_id")
                or claims.get("chatgpt_account_id")
                or ""
            ).strip()
            return account_id
        except Exception:
            return ""

    def _ensure_native_required_tokens(self, result: RegistrationResult) -> bool:
        """
        原生注册入口要求拿齐：
        Account ID / Workspace ID / Client ID / Access Token / Refresh Token
        """
        try:
            if (not result.account_id) and result.id_token:
                try:
                    account_info = self.oauth_manager.extract_account_info(result.id_token)
                    result.account_id = str(account_info.get("account_id") or "").strip()
                except Exception:
                    pass
            if (not result.account_id) and result.access_token:
                result.account_id = self._extract_account_id_from_access_token(result.access_token)

            if not result.workspace_id:
                result.workspace_id = str(self._get_workspace_id() or "").strip()
            if (not result.refresh_token) and self._create_account_refresh_token:
                result.refresh_token = str(self._create_account_refresh_token or "").strip()

            settings = get_settings()
            client_id = str(
                getattr(settings, "openai_client_id", "")
                or getattr(self.oauth_manager, "client_id", "")
                or ""
            ).strip()

            missing = []
            if not result.account_id:
                missing.append("Account ID")
            if not result.workspace_id:
                missing.append("Workspace ID")
            if not client_id:
                missing.append("Client ID")
            if not result.access_token:
                missing.append("Access Token")
            if not result.refresh_token:
                missing.append("Refresh Token")

            if missing:
                self._log(f"原生入口关键参数缺失: {', '.join(missing)}", "error")
                return False

            self._log(
                "原生入口关键参数校验通过: "
                f"Account ID={result.account_id}, Workspace ID={result.workspace_id}, "
                f"Client ID={client_id}, Access=有, Refresh=有"
            )
            return True
        except Exception as e:
            self._log(f"原生入口关键参数校验异常: {e}", "error")
            return False

    def _restart_login_flow(self) -> Tuple[bool, str]:
        """新注册账号完成建号后，重新发起一次登录流程拿 token。"""
        self._token_acquisition_requires_login = True
        self._log("注册这边忙完了，再走一趟登录把 token 请出来，收个尾...")
        self._reset_auth_flow()

        did, sen_token = self._prepare_authorize_flow("重新登录")
        if not did:
            return False, "重新登录时获取 Device ID 失败"
        if not sen_token:
            return False, "重新登录时 Sentinel POW 验证失败"

        login_start_result = self._submit_login_start(did, sen_token)
        if not login_start_result.success:
            return False, f"重新登录提交邮箱失败: {login_start_result.error_message}"
        if login_start_result.page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            return False, f"重新登录未进入密码页面: {login_start_result.page_type or 'unknown'}"

        password_result = self._submit_login_password()
        if not password_result.success:
            return False, f"重新登录提交密码失败: {password_result.error_message}"
        if not password_result.is_existing_account:
            return False, f"重新登录未进入验证码页面: {password_result.page_type or 'unknown'}"
        return True, ""

    def _retrigger_login_otp(self) -> bool:
        """
        在“登录验证码”阶段重触发 OTP 发送。
        优先复用登录链路（login_start -> login_password），避免误走注册 OTP 流程。
        """
        try:
            did = str(self.device_id or self.session.cookies.get("oai-did") or "").strip()
            if not did:
                did = str(uuid.uuid4())
                try:
                    self.session.cookies.set("oai-did", did, domain=".chatgpt.com", path="/")
                except Exception:
                    pass
                self.device_id = did

            sen_token = self._check_sentinel(did)
            login_start_result = self._submit_login_start(did, sen_token)
            if not login_start_result.success:
                self._log(
                    f"重触发登录 OTP 失败：提交登录入口失败: {login_start_result.error_message}",
                    "warning",
                )
                return False

            page_type = str(login_start_result.page_type or "").strip()
            if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
                self._log("重触发登录 OTP 成功：已直达邮箱验证码页")
                return True

            if page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
                self._log(f"重触发登录 OTP 失败：未进入密码页（{page_type or 'unknown'}）", "warning")
                return False

            password_result = self._submit_login_password()
            if not password_result.success:
                self._log(f"重触发登录 OTP 失败：提交登录密码失败: {password_result.error_message}", "warning")
                return False
            if not password_result.is_existing_account:
                self._log(
                    f"重触发登录 OTP 失败：密码后未进入验证码页（{password_result.page_type or 'unknown'}）",
                    "warning",
                )
                return False

            self._log("重触发登录 OTP 成功：已进入邮箱验证码页")
            return True
        except Exception as e:
            self._log(f"重触发登录 OTP 异常: {e}", "warning")
            return False

    def _register_password(self, did: Optional[str] = None, sen_token: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """注册密码"""
        try:
            self._last_register_password_error = None
            # 生成密码
            password = self._generate_password()
            self.password = password  # 保存密码到实例变量
            self._log(f"生成密码: {password}")

            # 提交密码注册
            register_body = json.dumps({
                "password": password,
                "username": self.email
            })

            response = self.session.post(
                OPENAI_API_ENDPOINTS["register"],
                headers={
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=register_body,
            )

            self._log(f"提交密码状态: {response.status_code}")

            if response.status_code != 200:
                error_text = response.text[:500]
                self._log(f"密码注册失败: {error_text}", "warning")

                # 解析错误信息，判断是否是邮箱已注册
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", {}).get("message", "")
                    error_code = error_json.get("error", {}).get("code", "")
                    normalized_error_msg = str(error_msg or "").strip()
                    normalized_error_code = str(error_code or "").strip()

                    # 检测邮箱已注册的情况
                    if "already" in normalized_error_msg.lower() or "exists" in normalized_error_msg.lower() or normalized_error_code == "user_exists":
                        self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")
                        # 标记此邮箱为已注册状态
                        self._mark_email_as_registered()
                        self._last_register_password_error = "该邮箱可能已在 OpenAI 注册，建议更换邮箱或改走登录流程"
                    elif "failed to register username" in normalized_error_msg.lower():
                        self._last_register_password_error = (
                            "OpenAI 拒绝当前邮箱用户名（可能已占用或触发风控），建议更换邮箱后重试"
                        )
                        if did:
                            self._log("检测到用户名注册失败，尝试登录入口探测邮箱是否已存在...", "warning")
                            try:
                                probe = self._submit_login_start(did, sen_token)
                                if probe.success and probe.page_type in (
                                    OPENAI_PAGE_TYPES["LOGIN_PASSWORD"],
                                    OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
                                ):
                                    self._log("登录入口探测命中：该邮箱大概率已是 OpenAI 账号", "warning")
                                    self._mark_email_as_registered()
                                    self._last_register_password_error = (
                                        "该邮箱已存在 OpenAI 账号。"
                                        "若是刚刚注册中断，请优先使用上一轮任务日志里的“生成密码”走登录续跑；"
                                        "拿不到旧密码再更换邮箱。"
                                    )
                            except Exception as probe_error:
                                self._log(f"登录入口探测失败: {probe_error}", "warning")
                    else:
                        self._last_register_password_error = (
                            f"注册密码接口返回异常: {normalized_error_msg or f'HTTP {response.status_code}'}"
                        )
                except Exception:
                    self._last_register_password_error = f"注册密码接口返回异常: HTTP {response.status_code}"

                return False, None

            return True, password

        except Exception as e:
            self._log(f"密码注册失败: {e}", "error")
            self._last_register_password_error = str(e)
            return False, None

    def _mark_email_as_registered(self):
        """标记邮箱为已注册状态（用于防止重复尝试）"""
        try:
            with get_db() as db:
                # 检查是否已存在该邮箱的记录
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 创建一个失败记录，标记该邮箱已注册过
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 空密码表示未成功注册
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}
                    )
                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _send_verification_code(self, referer: Optional[str] = None) -> bool:
        """发送验证码"""
        try:
            # 记录发送时间戳
            self._otp_sent_at = time.time()
            send_referer = str(referer or "https://auth.openai.com/create-account/password").strip()

            response = self.session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers={
                    "referer": send_referer,
                    "accept": "application/json",
                },
            )

            self._log(f"验证码发送状态: {response.status_code}")
            return response.status_code == 200

        except Exception as e:
            self._log(f"发送验证码失败: {e}", "error")
            return False

    def _get_verification_code(self, timeout: Optional[int] = None) -> Optional[str]:
        """获取验证码"""
        try:
            mailbox_email = str(self.inbox_email or self.email or "").strip()
            self._log(f"正在等待邮箱 {mailbox_email} 的验证码...")

            email_id = self.email_info.get("service_id") if self.email_info else None
            fetch_timeout = int(timeout) if timeout and int(timeout) > 0 else 120
            code = self.email_service.get_verification_code(
                email=mailbox_email,
                email_id=email_id,
                timeout=fetch_timeout,
                pattern=OTP_CODE_PATTERN,
                otp_sent_at=self._otp_sent_at,
            )

            if code:
                self._log(f"成功获取验证码: {code}")
                return code
            else:
                self._log("等待验证码超时", "error")
                return None

        except Exception as e:
            self._log(f"获取验证码失败: {e}", "error")
            return None

    def _validate_verification_code(self, code: str) -> bool:
        """验证验证码"""
        try:
            self._last_otp_validation_code = str(code or "").strip()
            self._last_otp_validation_status_code = None
            self._last_otp_validation_outcome = ""
            code_body = f'{{"code":"{code}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers={
                    "referer": "https://auth.openai.com/email-verification",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=code_body,
            )

            self._log(f"验证码校验状态: {response.status_code}")
            self._last_otp_validation_status_code = int(response.status_code)
            self._last_otp_validation_outcome = "success" if response.status_code == 200 else "http_non_200"
            if response.status_code == 200:
                # 记录 OTP 校验返回中的 continue/workspace 提示，供 native 收尾兜底
                try:
                    import urllib.parse as urlparse
                    payload = response.json() or {}
                    candidates: List[Dict[str, Any]] = []
                    if isinstance(payload, dict):
                        candidates.append(payload)
                        for key in ("data", "result", "next", "payload"):
                            value = payload.get(key)
                            if isinstance(value, dict):
                                candidates.append(value)

                    found_continue = ""
                    found_workspace = ""
                    for item in candidates:
                        if not isinstance(item, dict):
                            continue
                        if not found_workspace:
                            found_workspace = str(
                                item.get("workspace_id")
                                or item.get("workspaceId")
                                or item.get("default_workspace_id")
                                or ((item.get("workspace") or {}).get("id") if isinstance(item.get("workspace"), dict) else "")
                                or ""
                            ).strip()
                        if not found_continue:
                            for key in ("continue_url", "continueUrl", "next_url", "nextUrl", "redirect_url", "redirectUrl", "url"):
                                candidate = str(item.get(key) or "").strip()
                                if not candidate:
                                    continue
                                if candidate.startswith("/"):
                                    candidate = urlparse.urljoin(OPENAI_API_ENDPOINTS["validate_otp"], candidate)
                                found_continue = candidate
                                break
                        if found_workspace and found_continue:
                            break

                    if found_workspace:
                        self._last_validate_otp_workspace_id = found_workspace
                        self._log(f"OTP 校验返回 Workspace ID: {found_workspace}")
                    if found_continue:
                        self._last_validate_otp_continue_url = found_continue
                        self._log(f"OTP 校验返回 continue_url: {found_continue[:100]}...")
                except Exception as parse_err:
                    self._log(f"解析 OTP 校验返回信息失败: {parse_err}", "warning")

            return response.status_code == 200

        except Exception as e:
            err_text = str(e or "").lower()
            if (
                "timed out" in err_text
                or "timeout" in err_text
                or "curl: (28)" in err_text
                or "operation timed out" in err_text
            ):
                self._last_otp_validation_outcome = "network_timeout"
            else:
                self._last_otp_validation_outcome = "network_error"
            self._log(f"验证验证码失败: {e}", "error")
            return False

    def _verify_email_otp_with_retry(
        self,
        stage_label: str = "验证码",
        max_attempts: int = 3,
        fetch_timeout: Optional[int] = None,
        attempted_codes: Optional[set[str]] = None,
    ) -> bool:
        """
        获取并校验验证码（带重试）。
        用于规避邮箱里历史验证码导致的 400（第一次取到旧码，第二次取新码）。
        """
        # 每轮验证码阶段开始前，清理上轮 OTP 校验缓存，避免 continue_url/workspace 被旧阶段污染。
        self._last_validate_otp_continue_url = None
        self._last_validate_otp_workspace_id = None
        if attempted_codes is None:
            attempted_codes = set()
        for attempt in range(1, max_attempts + 1):
            code = (
                self._get_verification_code(timeout=fetch_timeout)
                if fetch_timeout
                else self._get_verification_code()
            )
            if not code:
                if attempt < max_attempts:
                    self._log(
                        f"{stage_label}第 {attempt}/{max_attempts} 次未取到验证码，稍后重试...",
                        "warning",
                    )
                    time.sleep(2)
                    continue
                return False

            if code in attempted_codes:
                allow_same_code_retry = (
                    self._last_otp_validation_code == code
                    and self._last_otp_validation_outcome in {"network_timeout", "network_error"}
                )
                if allow_same_code_retry:
                    self._log(
                        f"{stage_label}第 {attempt}/{max_attempts} 次命中重复验证码 {code}，"
                        f"但上次校验为网络异常（{self._last_otp_validation_outcome}），重试同码...",
                        "warning",
                    )
                    if self._validate_verification_code(code):
                        return True
                    if attempt < max_attempts:
                        time.sleep(2)
                        continue
                    return False

                if attempt < max_attempts:
                    self._log(
                        f"{stage_label}第 {attempt}/{max_attempts} 次命中重复验证码 {code}，等待新邮件...",
                        "warning",
                    )
                    time.sleep(2)
                    continue
                return False

            attempted_codes.add(code)

            if self._validate_verification_code(code):
                return True

            if attempt < max_attempts:
                self._log(
                    f"{stage_label}第 {attempt}/{max_attempts} 次校验未通过，疑似旧验证码，自动重试下一封...",
                    "warning",
                )
                time.sleep(2)

        return False

    def _create_user_account(self) -> bool:
        """创建用户账户"""
        try:
            user_info = generate_random_user_info()
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
            create_account_body = json.dumps(user_info)

            response = self.session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers={
                    "referer": "https://auth.openai.com/about-you",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=create_account_body,
            )

            self._log(f"账户创建状态: {response.status_code}")

            if response.status_code != 200:
                self._log(f"账户创建失败: {response.text[:200]}", "warning")
                return False

            try:
                data = response.json() or {}
                continue_url = str(data.get("continue_url") or "").strip()
                if continue_url:
                    self._create_account_continue_url = continue_url
                    self._log(f"create_account 返回 continue_url，已缓存: {continue_url[:100]}...")
                account_id = str(
                    data.get("account_id")
                    or data.get("chatgpt_account_id")
                    or (data.get("account") or {}).get("id")
                    or ""
                ).strip()
                if account_id:
                    self._create_account_account_id = account_id
                    self._log(f"create_account 返回 account_id，已缓存: {account_id}")
                workspace_id = str(
                    data.get("workspace_id")
                    or data.get("default_workspace_id")
                    or (data.get("workspace") or {}).get("id")
                    or ""
                ).strip()
                if (not workspace_id) and isinstance(data.get("workspaces"), list) and data.get("workspaces"):
                    workspace_id = str((data.get("workspaces")[0] or {}).get("id") or "").strip()
                if workspace_id:
                    self._create_account_workspace_id = workspace_id
                    self._log(f"create_account 返回 workspace_id，已缓存: {workspace_id}")
                refresh_token = str(data.get("refresh_token") or "").strip()
                if refresh_token:
                    self._create_account_refresh_token = refresh_token
                    self._log("create_account 返回 refresh_token，已缓存")
            except Exception:
                pass

            return True

        except Exception as e:
            self._log(f"创建账户失败: {e}", "error")
            return False

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        try:
            def _extract_workspace_id(payload: Any) -> str:
                if not isinstance(payload, dict):
                    return ""
                workspace_id = str(
                    payload.get("workspace_id")
                    or payload.get("default_workspace_id")
                    or ((payload.get("workspace") or {}).get("id") if isinstance(payload.get("workspace"), dict) else "")
                    or ""
                ).strip()
                if workspace_id:
                    return workspace_id
                workspaces = payload.get("workspaces") or []
                if isinstance(workspaces, list) and workspaces:
                    return str((workspaces[0] or {}).get("id") or "").strip()
                return ""

            auth_cookie = str(self.session.cookies.get("oai-client-auth-session") or "").strip()
            if not auth_cookie:
                self._log("未能获取到授权 Cookie，尝试从 auth-info 里取 workspace", "warning")

            # 解码 JWT
            import base64
            import json as json_module
            import urllib.parse as urlparse

            try:
                candidate_payloads: List[str] = []
                if auth_cookie:
                    segments = auth_cookie.split(".")
                    # 对齐 ABCard：优先 JWT payload 段（第 2 段）
                    if len(segments) >= 2 and segments[1]:
                        candidate_payloads.append(segments[1])
                    if segments and segments[0]:
                        candidate_payloads.append(segments[0])
                    # 极端情况下 cookie 可能直接是 JSON 字符串
                    candidate_payloads.append(auth_cookie)

                for payload in candidate_payloads:
                    raw = str(payload or "").strip()
                    if not raw:
                        continue
                    auth_json = None
                    try:
                        pad = "=" * ((4 - (len(raw) % 4)) % 4)
                        decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
                        auth_json = json_module.loads(decoded.decode("utf-8"))
                    except Exception:
                        try:
                            auth_json = json_module.loads(raw)
                        except Exception:
                            auth_json = None

                    workspace_id = _extract_workspace_id(auth_json)
                    if workspace_id:
                        self._log(f"Workspace ID: {workspace_id}")
                        return workspace_id

                # 兜底：从 oai-client-auth-info（URL 编码 JSON）提取 workspace
                auth_info_raw = str(self.session.cookies.get("oai-client-auth-info") or "").strip()
                if auth_info_raw:
                    auth_info_text = auth_info_raw
                    for _ in range(2):
                        decoded = urlparse.unquote(auth_info_text)
                        if decoded == auth_info_text:
                            break
                        auth_info_text = decoded
                    try:
                        auth_info_json = json_module.loads(auth_info_text)
                        workspace_id = _extract_workspace_id(auth_info_json)
                        if workspace_id:
                            self._log(f"Workspace ID (auth-info): {workspace_id}")
                            return workspace_id
                    except Exception as auth_info_err:
                        self._log(f"解析 auth-info Cookie 失败: {auth_info_err}", "warning")

                # 兜底：复用 create_account 缓存
                cached_workspace = str(self._create_account_workspace_id or "").strip()
                if cached_workspace:
                    self._log(f"Workspace ID (create_account缓存): {cached_workspace}")
                    return cached_workspace

                self._log("授权 Cookie 里没有 workspace 信息", "warning")
                return None

            except Exception as e:
                self._log(f"解析授权 Cookie 失败: {e}", "warning")
                return None

        except Exception as e:
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """选择 Workspace"""
        try:
            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers={
                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                data=select_body,
                allow_redirects=False,
            )

            # 兼容 30x：部分环境 continue_url 在 Location 头里。
            location = str(response.headers.get("Location") or "").strip()
            if response.status_code in [301, 302, 303, 307, 308] and location:
                import urllib.parse
                continue_url = urllib.parse.urljoin(OPENAI_API_ENDPOINTS["select_workspace"], location)
                self._log(f"Continue URL (Location): {continue_url[:100]}...")
                return continue_url

            if response.status_code != 200:
                self._log(f"选择 workspace 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = ""
            try:
                continue_url = str((response.json() or {}).get("continue_url") or "").strip()
            except Exception as json_err:
                body_text = str(response.text or "")
                self._log(f"workspace/select 非 JSON 响应，尝试文本兜底解析: {json_err}", "warning")
                # 兜底1：HTML/文本里直接包含 continue_url
                m = re.search(r'"continue_url"\s*:\s*"([^"]+)"', body_text)
                if m:
                    continue_url = str(m.group(1) or "").strip()
                # 兜底2：返回页内含 auth.openai.com/oauth/authorize 链接
                if not continue_url:
                    m2 = re.search(r"https://auth\.openai\.com/[^\s\"'<>]+", body_text)
                    if m2:
                        continue_url = str(m2.group(0) or "").strip()

            if not continue_url:
                if location:
                    import urllib.parse
                    continue_url = urllib.parse.urljoin(OPENAI_API_ENDPOINTS["select_workspace"], location)
                else:
                    self._log("workspace/select 响应里缺少 continue_url", "error")
                    return None

            if continue_url:
                continue_url = continue_url.replace("\\/", "/")
                self._log(f"Continue URL: {continue_url[:100]}...")
                return continue_url

            return None

        except Exception as e:
            self._log(f"选择 Workspace 失败: {e}", "error")
            return None

    def _follow_redirects(self, start_url: str) -> Tuple[Optional[str], str]:
        """手动跟随重定向链，返回 (callback_url, final_url)。"""
        try:
            def _is_oauth_callback(url: str) -> bool:
                try:
                    import urllib.parse as _urlparse

                    parsed = _urlparse.urlparse(url)
                    path = (parsed.path or "").lower()
                    if ("/auth/callback" not in path) and ("/api/auth/callback/openai" not in path):
                        return False
                    query = _urlparse.parse_qs(parsed.query or "", keep_blank_values=True)
                    # 只要带 code 或 error，就认为已经进入回调阶段（避免被本地 503 干扰识别）
                    return bool(query.get("code") or query.get("error"))
                except Exception:
                    return False

            current_url = start_url
            callback_url: Optional[str] = None
            max_redirects = 12

            for i in range(max_redirects):
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")
                if _is_oauth_callback(current_url) and not callback_url:
                    callback_url = current_url
                    self._log(f"命中回调 URL: {current_url[:120]}...")
                    # 已拿到 callback，不再请求本地 callback 地址，避免 503 干扰后续判断
                    break

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                if "/api/auth/callback/openai" in current_url and not callback_url:
                    callback_url = current_url

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                import urllib.parse
                next_url = urllib.parse.urljoin(current_url, location)

                # 命中回调时仅记录，不提前返回；继续跟到底，让 next-auth 充分落 cookie。
                if _is_oauth_callback(next_url) and not callback_url:
                    callback_url = next_url
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    current_url = next_url
                    break

                current_url = next_url

            # 对齐 ABCard：补打一跳 chatgpt 首页，确保 next-auth cookie 完整落地。
            try:
                if not current_url.rstrip("/").endswith("chatgpt.com"):
                    self.session.get(
                        "https://chatgpt.com/",
                        headers={
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "referer": current_url,
                            "user-agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                            ),
                        },
                        timeout=20,
                    )
            except Exception as home_err:
                self._log(f"重定向结束后首页补跳异常: {home_err}", "warning")

            if not callback_url:
                self._log("未能在重定向链中找到回调 URL", "warning")
            return callback_url, current_url

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None, start_url

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._log("处理 OAuth 回调，最后一哆嗦，稳住别抖...")
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )

            self._log("OAuth 授权成功，通关文牒到手")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def run(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._is_existing_account = False
            self._token_acquisition_requires_login = False
            self._otp_sent_at = None
            self._create_account_continue_url = None
            self._create_account_workspace_id = None
            self._create_account_account_id = None
            self._create_account_refresh_token = None
            self._last_validate_otp_continue_url = None
            self._last_validate_otp_workspace_id = None

            self._log("=" * 60)
            self._log("注册流程启动，开始替你敲门")
            self._log("=" * 60)
            self._log(f"注册入口链路配置: {self.registration_entry_flow}")
            configured_entry_flow = self.registration_entry_flow
            service_type_raw = getattr(self.email_service, "service_type", "")
            service_type_value = str(getattr(service_type_raw, "value", service_type_raw) or "").strip().lower()
            effective_entry_flow = configured_entry_flow
            if service_type_value == "outlook":
                self._log("检测到 Outlook 邮箱，自动使用 Outlook 入口链路（无需在设置中选择）")
                effective_entry_flow = "outlook"

            # 1. 检查 IP 地理位置
            self._log("1. 先看看这条网络从哪儿来，别一开局就站错片场...")
            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                self._log(f"IP 检查失败: {location}", "error")
                return result

            self._log(f"IP 位置: {location}")

            # 2. 创建邮箱
            self._log("2. 开个新邮箱，准备收信...")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email

            # 3. 准备首轮授权流程
            did, sen_token = self._prepare_authorize_flow("首次授权")
            if not did:
                result.error_message = "获取 Device ID 失败"
                return result
            result.device_id = did
            if not sen_token:
                result.error_message = "Sentinel POW 验证失败"
                return result

            # 4. 提交注册入口邮箱
            self._log("4. 递上邮箱，看看 OpenAI 这球怎么接...")
            signup_result = self._submit_signup_form(did, sen_token)
            if not signup_result.success:
                result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                return result

            if self._is_existing_account:
                self._log("检测到这是老朋友账号，直接切去登录拿 token，不走弯路")
            else:
                self._log("5. 设置密码，别让小偷偷笑...")
                password_ok, _ = self._register_password(did, sen_token)
                if not password_ok:
                    result.error_message = self._last_register_password_error or "注册密码失败"
                    return result

                self._log("6. 催一下注册验证码出门，邮差该冲刺了...")
                if not self._send_verification_code():
                    result.error_message = "发送验证码失败"
                    return result

                self._log("7. 等验证码飞来，邮箱请注意查收...")
                self._log("8. 对一下验证码，看看是不是本人...")
                if not self._verify_email_otp_with_retry(stage_label="注册验证码", max_attempts=3):
                    result.error_message = "验证验证码失败"
                    return result

                self._log("9. 给账号办个正式户口，名字写档案里...")
                if not self._create_user_account():
                    result.error_message = "创建用户账户失败"
                    return result

                if effective_entry_flow in {"native", "outlook"}:
                    login_ready, login_error = self._restart_login_flow()
                    if not login_ready:
                        result.error_message = login_error
                        return result
                    if effective_entry_flow == "outlook":
                        self._log("注册入口链路: Outlook（迁移版，按朋友版 Outlook 主流程收尾）")
                else:
                    self._log("注册入口链路: ABCard（新账号不重登，直接抓取会话）")

            if effective_entry_flow == "native":
                if not self._complete_token_exchange_native_backup(result):
                    return result
            elif effective_entry_flow == "outlook":
                if not self._complete_token_exchange_outlook(result):
                    return result
            else:
                use_abcard_entry = (effective_entry_flow == "abcard") and (not self._is_existing_account)
                if not self._complete_token_exchange(result, require_login_otp=not use_abcard_entry):
                    return result

            # 10. 完成
            self._log("=" * 60)
            if self._is_existing_account:
                self._log("登录成功，老朋友顺利回家")
            else:
                self._log("注册成功，账号已经稳稳落地，可以开香槟了")
            self._log(f"邮箱: {result.email}")
            self._log(f"Device ID: {result.device_id or '-'}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            result.success = True
            settings = get_settings()
            client_id = str(getattr(settings, "openai_client_id", "") or getattr(self.oauth_manager, "client_id", "") or "").strip()
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now().isoformat(),
                "is_existing_account": self._is_existing_account,
                "token_acquired_via_relogin": self._token_acquisition_requires_login,
                "client_id": client_id,
                "device_id": result.device_id,
                "has_session_token": bool(result.session_token),
                "has_access_token": bool(result.access_token),
                "has_refresh_token": bool(result.refresh_token),
                "registration_entry_flow": configured_entry_flow,
                "registration_entry_flow_effective": effective_entry_flow,
                # 对齐 K:\1\2：原生入口允许无 session_token 成功，但会标记待补。
                "session_token_pending": (effective_entry_flow == "native") and (not bool(result.session_token)),
            }

            return result

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return result

    def save_to_database(
        self,
        result: RegistrationResult,
        account_label: Optional[str] = None,
        role_tag: Optional[str] = None,
    ) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False

        try:
            # 获取默认 client_id
            settings = get_settings()

            with get_db() as db:
                # 保存账户信息
                account = crud.create_account(
                    db,
                    email=result.email,
                    password=result.password,
                    client_id=settings.openai_client_id,
                    session_token=result.session_token,
                    cookies=self._dump_session_cookies(),
                    email_service=self.email_service.service_type.value,
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    account_id=result.account_id,
                    workspace_id=result.workspace_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    id_token=result.id_token,
                    proxy_used=self.proxy_url,
                    extra_data=result.metadata,
                    source=result.source,
                    account_label=account_label,
                    role_tag=role_tag,
                )

                self._log(f"账户已存进数据库，落袋为安，ID: {account.id}")
                return True

        except Exception as e:
            self._log(f"保存到数据库失败: {e}", "error")
            return False
